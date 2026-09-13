import asyncio
import json
import logging
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import Counter, Histogram, generate_latest
from starlette.responses import Response

app = FastAPI(title="Observability Lab")


# ---------- Prometheus metrics ----------

REQUESTS = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    ["method", "path", "status"],
)

ERRORS = Counter(
    "http_errors_total",
    "Total number of HTTP 5xx responses",
    ["method", "path"],
)

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)


# ---------- Logging ----------

logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO)


def log_json(level: str, message: str, trace_id: str | None = None, **extra):
    record = {
        "level": level,
        "message": message,
        "trace_id": trace_id,
        **extra,
    }

    print(json.dumps(record), flush=True)


# ---------- OpenTelemetry ----------

FastAPIInstrumentor.instrument_app(app)

tracer = trace.get_tracer(__name__)


def current_trace_id() -> str:
    span = trace.get_current_span()
    context = span.get_span_context()

    if context.is_valid:
        return format(context.trace_id, "032x")

    return str(uuid.uuid4())


# ---------- Middleware ----------

@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    start = time.perf_counter()

    response = None

    try:
        response = await call_next(request)
        return response

    finally:
        duration = time.perf_counter() - start

        path = request.url.path
        method = request.method
        status = response.status_code if response else 500
        trace_id = current_trace_id()

        REQUESTS.labels(
            method=method,
            path=path,
            status=status,
        ).inc()

        REQUEST_DURATION.labels(
            method=method,
            path=path,
        ).observe(duration)

        if status >= 500:
            ERRORS.labels(
                method=method,
                path=path,
            ).inc()

        log_json(
            "INFO" if status < 500 else "ERROR",
            "request completed",
            trace_id=trace_id,
            method=method,
            path=path,
            status=status,
            duration_seconds=round(duration, 4),
        )


# ---------- Endpoints ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", encoding="utf-8") as file:
        return file.read()


@app.get("/error")
async def create_error():
    trace_id = current_trace_id()

    log_json(
        "ERROR",
        "intentional error",
        trace_id=trace_id,
    )

    return JSONResponse(
        status_code=500,
        content={
            "error": "intentional error",
            "trace_id": trace_id,
        },
    )


@app.get("/delay")
async def create_delay(seconds: float = 2):
    seconds = max(1, min(seconds, 3))

    trace_id = current_trace_id()

    log_json(
        "INFO",
        "starting intentional delay",
        trace_id=trace_id,
        seconds=seconds,
    )

    await asyncio.sleep(seconds)

    return {
        "message": "delayed response",
        "seconds": seconds,
        "trace_id": trace_id,
    }


@app.get("/load")
async def create_load(request: Request, requests: int = 200):
    trace_id = current_trace_id()

    log_json(
        "INFO",
        "starting load generation",
        trace_id=trace_id,
        requests=requests,
    )

    url = str(request.base_url).rstrip("/") + "/health"

    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            *[
                client.get(url)
                for _ in range(requests)
            ]
        )

    return {
        "message": "load generated",
        "requests": requests,
        "trace_id": trace_id,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(
        generate_latest(),
        media_type="text/plain",
    )