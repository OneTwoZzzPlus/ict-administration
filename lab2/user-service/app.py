#!/usr/bin/env python3

import asyncio
import json
import os
import random
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC = os.getenv("KAFKA_TOPIC", "orders")
INSTANCE = os.getenv("INSTANCE_NAME", "user-service-1")
METADATA_MAX_AGE_MS = int(os.getenv("METADATA_MAX_AGE_MS", "5000"))  # как быстро замечаем смену партиций

ITEMS = ["яблоки", "молоко", "хлеб", "сыр", "кофе", "чай", "печенье", "вода"]
producer: AIOKafkaProducer | None = None


async def connect():
    # Подключаемся к Kafka в фоне, чтобы HTTP-сервер стартовал сразу
    global producer
    while True:
        p = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP, client_id=INSTANCE,
                         metadata_max_age_ms=METADATA_MAX_AGE_MS)
        try:
            print(f"Подключаюсь к Kafka {BOOTSTRAP}...", flush=True)
            await p.start()
            producer = p
            print("Kafka подключена", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            print(f"Kafka {BOOTSTRAP} недоступна ({e}), повтор через 3с", flush=True)
            await p.stop()
            await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(connect())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    if producer:
        await producer.stop()


app = FastAPI(lifespan=lifespan)


@app.post("/orders")
async def create_order():
    if producer is None:
        raise HTTPException(503, "Kafka ещё не подключена")
    order = {
        "order_id": str(uuid.uuid4())[:8],
        "items": random.sample(ITEMS, k=random.randint(1, 3)),
    }
    # key = order_id: одинаковый ключ -> всегда одна партиция
    meta = await producer.send_and_wait(
        TOPIC,
        key=order["order_id"].encode(),
        value=json.dumps(order, ensure_ascii=False).encode(),
    )
    return {"order": order, "topic": meta.topic, "partition": meta.partition, "offset": meta.offset}


@app.get("/api/partitions")
async def partitions():
    if producer is None:
        return {"partitions": []}
    try:
        return {"partitions": sorted(await producer.partitions_for(TOPIC))}
    except Exception:  # noqa: BLE001
        return {"partitions": []}


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).with_name("favicon.svg"), media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE.replace("__INSTANCE__", INSTANCE).replace("__TOPIC__", TOPIC)


PAGE = """<!doctype html>
<meta charset="utf-8"><title>user-service</title>
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
<h2><img alt="" src="/favicon.svg" style="width: 1.5rem;"> user-service (__INSTANCE__)</h2>
<p>топик "__TOPIC__" | партиции: <b id="parts">—</b></p>
<button onclick="create()">Создать заказ</button>
<table>
<thead><tr><th>order_id</th><th>товары</th><th>партиция</th><th>offset</th></tr></thead>
<tbody id="log"></tbody></table>
<script>
async function refreshParts() {
  const r = await (await fetch('/api/partitions')).json();
  document.getElementById('parts').textContent = r.partitions.join(', ') || 'нет';
}
setInterval(refreshParts, 2000); refreshParts();

async function create() {
  const resp = await fetch('/orders', {method: 'POST'});
  if (!resp.ok) { alert('Ошибка: ' + (await resp.json()).detail); return; }
  const r = await resp.json();
  document.getElementById('log').insertAdjacentHTML('afterbegin',
    `<tr><td>${r.order.order_id}</td><td>${r.order.items.join(', ')}</td>
     <td>${r.partition}</td><td>${r.offset}</td></tr>`);
}
</script>
"""