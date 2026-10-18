#!/usr/bin/env python3

import os
import sys
import time

import yaml
from confluent_kafka.admin import (
    AdminClient,
    AlterConfigOpType,
    ConfigEntry,
    ConfigResource,
    ConfigSource,
    NewPartitions,
    NewTopic,
)

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
CONFIG_PATH = os.getenv("TOPICS_CONFIG", "/config/topics.yaml")
REPLICATION = int(os.getenv("REPLICATION_FACTOR", "1"))
INTERVAL = int(os.getenv("RECONCILE_INTERVAL", "0"))


def log(msg):
    print(msg, flush=True)


def reconcile_topic(admin, name, spec, existing):
    """Return True if succesfully"""
    want_parts = int(spec["partitions"])
    want_cfg = {k: str(v) for k, v in (spec.get("config") or {}).items()}
    changed, ok = False, True

    # 1. topic is not exists -> create topic
    if name not in existing:
        admin.create_topics([NewTopic(name, want_parts, REPLICATION, config=want_cfg)])[name].result()
        log(f"[{name}] created: partitions={want_parts}, config={want_cfg}")
        return True

    # 2. partitions
    have_parts = len(existing[name].partitions)
    if want_parts > have_parts:
        admin.create_partitions([NewPartitions(name, want_parts)])[name].result()
        log(f"[{name}] num partitions: {have_parts} -> {want_parts}")
        changed = True
    elif want_parts < have_parts:
        log(f"[{name}] ERROR: We need  {want_parts} partitions, but there are already {have_parts}. "
            f"Kafka cannot reduce the number of partitions (it can only recreate the topic).")
        ok = False

    # 3. config
    res = ConfigResource(ConfigResource.Type.TOPIC, name)
    current = next(iter(admin.describe_configs([res]).values())).result()
    explicit = {k: e.value for k, e in current.items() if e.source == ConfigSource.DYNAMIC_TOPIC_CONFIG}

    ops = [ConfigEntry(k, v, incremental_operation=AlterConfigOpType.SET)
           for k, v in want_cfg.items() if explicit.get(k) != v]
    ops += [ConfigEntry(k, None, incremental_operation=AlterConfigOpType.DELETE)
            for k in explicit.keys() - want_cfg.keys()]
    if ops:
        upd = ConfigResource(ConfigResource.Type.TOPIC, name, incremental_configs=ops)
        next(iter(admin.incremental_alter_configs([upd]).values())).result()
        log(f"[{name}] config: " + ", ".join(f"{o.name}={'<default>' if o.value is None else o.value}" for o in ops))
        changed = True

    if not changed and ok:
        log(f"[{name}] unchanged")
    return ok


def reconcile(admin):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        desired = yaml.safe_load(f)["topics"]
    existing = admin.list_topics(timeout=10).topics
    ok = True
    for name, spec in desired.items():
        try:
            ok &= reconcile_topic(admin, name, spec, existing)
        except Exception as e:  # noqa: BLE001
            log(f"[{name}] ошибка: {e}")
            ok = False
    return ok


def main():
    admin = AdminClient({"bootstrap.servers": BOOTSTRAP})
    for _ in range(30):  # ждём Kafka
        try:
            admin.list_topics(timeout=5)
            break
        except Exception as e:  # noqa: BLE001
            log(f"Kafka {BOOTSTRAP} недоступна ({e}), повтор через 2с")
            time.sleep(2)

    while True:
        try:
            ok = reconcile(admin)
        except Exception as e:  # noqa: BLE001
            log(f"Ошибка: {e}")
            ok = False
        if INTERVAL <= 0:
            sys.exit(0 if ok else 1)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()