from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/alerts")
async def alerts(request: Request):
    data = await request.json()

    print("=" * 60, flush=True)
    print(f"ALERT STATUS: {data.get('status')}", flush=True)

    for alert in data.get("alerts", []):
        print(
            f"[{alert['status'].upper()}] "
            f"{alert['labels'].get('alertname')} — "
            f"{alert['annotations'].get('summary', '')}",
            flush=True,
        )

    print("=" * 60, flush=True)

    return {"status": "ok"}