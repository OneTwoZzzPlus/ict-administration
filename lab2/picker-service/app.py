#!/usr/bin/env python3

import asyncio
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from aiokafka import AIOKafkaConsumer
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC = os.getenv("KAFKA_TOPIC", "orders")
GROUP = os.getenv("KAFKA_GROUP_ID", "pickers")
INSTANCE = os.getenv("INSTANCE_NAME", "picker-1")
PICK_SECONDS = float(os.getenv("PICK_SECONDS", "2"))  # «время сборки» заказа
METADATA_MAX_AGE_MS = int(os.getenv("METADATA_MAX_AGE_MS", "5000"))  # как быстро замечаем смену партиций
STALL_SECONDS = int(os.getenv("STALL_SECONDS", "30"))  # столько без движения -> перезапуск консьюмера
OFFSET_RESET = os.getenv("AUTO_OFFSET_RESET", "earliest")  # откуда читать, если у группы нет offset'ов

processed: deque = deque(maxlen=50)
consumer: AIOKafkaConsumer | None = None
last_tick = time.monotonic()


async def session():
    """Одна жизнь консьюмера: подключились -> читаем, пока всё хорошо."""
    global consumer, last_tick
    c = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id=GROUP,
        client_id=INSTANCE,
        auto_offset_reset=OFFSET_RESET,
        metadata_max_age_ms=METADATA_MAX_AGE_MS,
    )
    try:
        await c.start()
        consumer = c
        print(f"{INSTANCE}: подключён, группа={GROUP}, топик={TOPIC}", flush=True)
        while True:
            last_tick = time.monotonic()  # «я жив» для сторожа
            batch = await c.getmany(timeout_ms=1000, max_records=1)
            # Топик пересоздан/уменьшен: наших партиций больше нет -> пересоздаём консьюмера
            existing = c.partitions_for_topic(TOPIC)
            if existing is not None and any(tp.partition not in existing for tp in c.assignment()):
                raise RuntimeError("партиции топика изменились, переподключаюсь")
            for msgs in batch.values():
                for msg in msgs:
                    order = json.loads(msg.value)
                    await asyncio.sleep(PICK_SECONDS)  # «собираем» заказ
                    processed.appendleft({
                        "order_id": order["order_id"],
                        "items": order["items"],
                        "partition": msg.partition,
                        "offset": msg.offset,
                    })
    finally:
        consumer = None
        try:
            await asyncio.wait_for(c.stop(), 10)  # stop у зависшего консьюмера тоже может виснуть
        except Exception:  # noqa: BLE001, S110
            pass


async def consume():
    """Сторож: перезапускает session() при ошибке или если она зависла."""
    global last_tick
    while True:
        last_tick = time.monotonic()
        task = asyncio.create_task(session())
        try:
            while not task.done():
                await asyncio.sleep(1)
                if time.monotonic() - last_tick > STALL_SECONDS:
                    print(f"Консьюмер завис (>{STALL_SECONDS}с без движения), перезапускаю", flush=True)
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        exc = None if task.cancelled() else task.exception()
        if exc:
            print(f"Ошибка Kafka {BOOTSTRAP}: {exc}, повтор через 3с", flush=True)
        await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(consume())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


@app.get("/api/state")
async def state():
    parts = sorted(tp.partition for tp in consumer.assignment()) if consumer else []
    return {"instance": INSTANCE, "group": GROUP, "assigned_partitions": parts,
            "processed": list(processed)}


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).with_name("favicon.svg"), media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE.replace("__INSTANCE__", INSTANCE).replace("__TOPIC__", TOPIC).replace("__GROUP__", GROUP)


PAGE = """<!doctype html>
<meta charset="utf-8"><title>picker-service</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; margin: 0 auto; padding: 24px; max-width: 800px; background: #f5f6f8; color: #222; }
  h2 { margin-top: 0; }
  button { width: 100%; font-size: 1.1em; padding: .6em 1.4em; border: 0; border-radius: 10px; background: #2563eb; color: #fff; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  table { width: 100%; margin-top: 16px; border-collapse: separate; border-spacing: 0;
          background: #fff; border: 1px solid #e2e4e9; border-radius: 12px; overflow: hidden; }
  th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #eceef2; }
  th { background: #eef0f4; }
  tr:last-child td { border-bottom: 0; }
</style>
<body>
<h2><img alt="" src="/favicon.svg" style="width: 1.5rem;"> picker-service (__INSTANCE__)</h2>
<p>топик "__TOPIC__", группа "__GROUP__"<br>
мои партиции: <b id="parts">—</b> | собрано: <b id="cnt">0</b></p>
<table>
<thead><tr><th>order_id</th><th>товары</th><th>партиция</th><th>offset</th><th>сборщик</th></tr></thead>
<tbody id="log"></tbody></table>
<script>
async function refresh() {
  const s = await (await fetch('/api/state')).json();
  document.getElementById('parts').textContent = s.assigned_partitions.join(', ') || 'нет';
  document.getElementById('cnt').textContent = s.processed.length;
  document.getElementById('log').innerHTML = s.processed.map(o =>
    `<tr><td>${o.order_id}</td><td>${o.items.join(', ')}</td>
     <td>${o.partition}</td><td>${o.offset}</td><td>${s.instance}</td></tr>`).join('');
}
setInterval(refresh, 1000); refresh();
</script>
"""