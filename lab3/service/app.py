#!/usr/bin/env python3
import json
import os
import random
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

import psycopg2
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# ---------- конфигурация ----------
WRITE_HOSTS = [h.strip() for h in os.getenv("DB_WRITE_HOSTS", "pg-primary,pg-standby").split(",") if h.strip()]
READ_HOSTS = [h.strip() for h in os.getenv("DB_READ_HOSTS", "pg-standby,pg-primary").split(",") if h.strip()]
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "runtime_sentinel")
DB_USER = os.getenv("DB_USER", "app")
DB_PASSWORD = os.getenv("DB_PASSWORD", "app_password")
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "2"))
WRITE_TIMEOUT_MS = int(os.getenv("DB_WRITE_TIMEOUT_MS", "3000"))  # сколько ждём COMMIT при синхронной репликации

# ---------- состояние процесса ----------
EVENTS = []
EVENTS_LOCK = threading.Lock()
_writer = None
_last_read = None
LOAD = {"running": False, "total": 0, "done": 0, "error": None}
LOAD_LOCK = threading.Lock()


def log(level, msg):
    with EVENTS_LOCK:
        EVENTS.append({"ts": datetime.now().strftime("%H:%M:%S.%f")[:-3], "level": level, "msg": msg})
        del EVENTS[:-200]


def first_line(e):
    text = str(e).strip()
    return text.splitlines()[0] if text else type(e).__name__


def explain(e):
    if "statement timeout" in str(e):
        return (f"standby не подтвердил запись за {WRITE_TIMEOUT_MS} мс: "
                "на primary она уже закоммичена, но реплика её не подтвердила")
    return first_line(e)


@contextmanager
def conn_to(host, options=None):
    kwargs = {"options": options} if options else {}
    conn = psycopg2.connect(
        host=host, port=DB_PORT, dbname=DB_NAME, user=DB_USER,
        password=DB_PASSWORD, connect_timeout=DB_CONNECT_TIMEOUT,
        **kwargs,
    )
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- запись ----------
def find_writer():
    """Первый хост из DB_WRITE_HOSTS, который не в recovery (т.е. primary)."""
    global _writer
    problems = []
    for host in WRITE_HOSTS:
        try:
            with conn_to(host) as c, c.cursor() as cur:
                cur.execute("SELECT pg_is_in_recovery()")
                in_recovery = cur.fetchone()[0]
            if not in_recovery:
                if host != _writer:
                    log("info", f"запись направлена на {host}")
                _writer = host
                return host
            problems.append(f"{host}: standby (только чтение)")
        except Exception as e:  # noqa: BLE001
            problems.append(f"{host}: {first_line(e)}")
    raise RuntimeError("нет доступного primary → " + "; ".join(problems))


PENDING = []  # момент старта каждой записи, которая ещё не завершилась
PENDING_LOCK = threading.Lock()


def write_event(body):
    host = _writer or find_writer()
    marker = time.time()
    with PENDING_LOCK:
        PENDING.append(marker)
    try:
        return _write_event_on(host, body)
    finally:
        with PENDING_LOCK:
            PENDING.remove(marker)


def _write_event_on(host, body):
    global _writer
    try:
        with conn_to(host, options=f"-c statement_timeout={WRITE_TIMEOUT_MS}") as c, c.cursor() as cur:
            cur.execute("SELECT id, hostname FROM hosts ORDER BY random() LIMIT 1")
            host_id, hostname = cur.fetchone()
            cur.execute(
                """INSERT INTO runtime_events
                       (event_id, host_id, source_type, library_name, severity, raw_payload, captured_at)
                   VALUES (%s, %s, 'lab', 'demo', %s, %s::jsonb, now())
                   RETURNING id, captured_at""",
                (str(uuid.uuid4()), host_id, random.randint(0, 5),
                 json.dumps({"body": body}, ensure_ascii=False)),
            )
            rid, captured = cur.fetchone()
        return {"id": str(rid), "node": host, "host": hostname, "captured_at": captured.isoformat()}
    except Exception as e:
        _writer = None  # при следующей записи выберем writer заново
        log("error", f"запись на {host} не прошла: {explain(e)}")
        raise


# ---------- чтение (всегда с standby, если он доступен) ----------
def read_events(limit=50):
    global _last_read
    problems = []
    for host in READ_HOSTS:
        try:
            with conn_to(host) as c, c.cursor() as cur:
                cur.execute("SELECT pg_is_in_recovery()")
                is_replica = cur.fetchone()[0]
                cur.execute(
                    """SELECT e.id, h.hostname, e.severity, e.raw_payload->>'body', e.captured_at
                       FROM runtime_events e
                       LEFT JOIN hosts h ON h.id = e.host_id
                       ORDER BY e.captured_at DESC
                       LIMIT %s""",
                    (limit,),
                )
                rows = [
                    {"id": str(r[0]), "host": r[1], "severity": r[2],
                     "body": r[3], "captured_at": r[4].isoformat()}
                    for r in cur.fetchall()
                ]
                cur.execute("SELECT count(*) FROM runtime_events")
                count = cur.fetchone()[0]
            _last_read = {"host": host, "is_replica": is_replica}
            return {"served_by": host, "is_replica": is_replica, "count": count, "rows": rows}
        except Exception as e:  # noqa: BLE001
            problems.append(f"{host}: {first_line(e)}")
    raise RuntimeError("не могу читать ни с одного хоста → " + "; ".join(problems))


# ---------- нагрузка ----------
def run_load(n):
    log("info", f"нагрузка: {n} событий")
    try:
        for i in range(n):
            write_event(f"load {i} {random.randint(0, 10**9)}")
            LOAD["done"] = i + 1
        log("info", f"нагрузка завершена: {n}")
    except Exception as e:  # noqa: BLE001
        LOAD["error"] = first_line(e)
        log("error", f"нагрузка прервана на {LOAD['done']}/{n}: {LOAD['error']}")
    finally:
        LOAD["running"] = False


# ---------- статус кластера ----------
def node_status(host):
    """Роль узла и его позиция WAL.
    На primary: pg_current_wal_lsn() — куда дошла запись.
    На standby: pg_last_wal_replay_lsn() — докуда применено на реплике."""
    info = {"host": host, "up": False, "role": None, "error": None,
            "lsn": None, "replicas": [], "sync_names": "", "sync_commit": ""}
    try:
        with conn_to(host) as c, c.cursor() as cur:
            cur.execute("SELECT pg_is_in_recovery()")
            in_recovery = cur.fetchone()[0]
            info.update(up=True, role="standby" if in_recovery else "primary")

            if in_recovery:
                cur.execute("SELECT pg_last_wal_replay_lsn()::text")
            else:
                cur.execute("SELECT pg_current_wal_lsn()::text")
            info["lsn"] = cur.fetchone()[0]

            if not in_recovery:
                cur.execute("SHOW synchronous_standby_names")
                info["sync_names"] = cur.fetchone()[0]
                cur.execute("SHOW synchronous_commit")
                info["sync_commit"] = cur.fetchone()[0]
                # Подключения standby к primary (walreceiver): подключён ли и в каком режиме
                cur.execute("SELECT application_name, state, sync_state FROM pg_stat_replication")
                info["replicas"] = [{"app": r[0], "state": r[1], "sync": r[2]} for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        info["error"] = first_line(e)
    return info


def measure_lag(primary_host, standby_host):
    """Отставание в байтах = позиция WAL на primary минус позиция replay на standby.
    Сначала читаем standby, потом primary: тогда standby не может оказаться впереди
    и значение не уйдёт в минус. Не зависит от pg_stat_replication."""
    with conn_to(standby_host) as c, c.cursor() as cur:
        cur.execute("SELECT pg_last_wal_replay_lsn()::text")
        replay = cur.fetchone()[0]
    with conn_to(primary_host) as c, c.cursor() as cur:
        cur.execute(
            "SELECT pg_current_wal_lsn()::text, "
            "pg_wal_lsn_diff(pg_current_wal_lsn(), %s::pg_lsn)::bigint",
            (replay,),
        )
        primary_lsn, lag = cur.fetchone()
    return {"primary_lsn": primary_lsn, "replay_lsn": replay, "lag_bytes": lag}


def collect_status():
    hosts = list(dict.fromkeys(WRITE_HOSTS + READ_HOSTS))
    nodes = [node_status(h) for h in hosts]
    primaries = [n for n in nodes if n["role"] == "primary"]
    standbys = [n for n in nodes if n["role"] == "standby"]
    primary = primaries[0] if primaries else None

    warnings = []
    for n in nodes:
        if not n["up"]:
            warnings.append(f"{n['host']} недоступен: {n['error']}")
    if not primaries:
        warnings.append("нет ни одного primary — записи невозможны")
    if len(primaries) > 1:
        warnings.append(f"SPLIT BRAIN: несколько primary {[p['host'] for p in primaries]}")
    if primary and not primary["replicas"]:
        warnings.append("standby не подключён к primary (pg_stat_replication пуст)")
    if _last_read and not _last_read["is_replica"]:
        warnings.append(f"чтение идёт с primary ({_last_read['host']}): standby недоступен")

    mode = "асинхронный"
    if primary and primary["sync_names"]:
        mode = f"синхронный: {primary['sync_names']}, commit = {primary['sync_commit']}"
        if not any(r["sync"] == "sync" for r in primary["replicas"]):
            warnings.append("синхронный режим включён, но синхронного standby нет: записи будут ждать его")

    with PENDING_LOCK:
        waits = [time.time() - t for t in PENDING]
    if waits and max(waits) >= 1:
        warnings.append(f"запись ждёт подтверждения standby уже {int(max(waits))} с")

    lag = None
    if primary and standbys:
        try:
            lag = measure_lag(primary["host"], standbys[0]["host"])
            standbys[0]["lag_bytes"] = lag["lag_bytes"]
        except Exception as e:  # noqa: BLE001
            lag = {"error": first_line(e)}

    for n in nodes:
        n.setdefault("lag_bytes", None)

    return {
        "writer": _writer,
        "mode": mode,
        "reader": _last_read,
        "nodes": nodes,
        "lag": lag,
        "warnings": warnings,
        "load": dict(LOAD),
        "events": EVENTS[-50:],
    }


# ---------- ждём базу и схему из init-скриптов ----------
def wait_for_db():
    while True:
        try:
            host = find_writer()
            with conn_to(host) as c, c.cursor() as cur:
                cur.execute("SELECT to_regclass('public.runtime_events') IS NOT NULL")
                if cur.fetchone()[0]:
                    log("info", f"база готова, схема на месте, primary = {host}")
                    return
            log("warn", "жду схему из init-скриптов")
        except Exception as e:  # noqa: BLE001
            log("warn", f"жду базу: {first_line(e)}")
        time.sleep(3)


threading.Thread(target=wait_for_db, daemon=True).start()


# ---------- HTTP ----------
@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/api/events")
def api_events():
    try:
        return jsonify(read_events())
    except Exception as e:  # noqa: BLE001
        return jsonify(error=first_line(e)), 503


@app.post("/api/events")
def api_create():
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip() or f"событие {datetime.now():%H:%M:%S}"
    try:
        return jsonify(write_event(body[:500]))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=explain(e)), 503


@app.post("/api/load")
def api_load():
    n = min(max(int(request.args.get("n", 1000)), 1), 20000)
    with LOAD_LOCK:
        if LOAD["running"]:
            return jsonify(error="нагрузка уже идёт"), 409
        LOAD.update(running=True, total=n, done=0, error=None)
    threading.Thread(target=run_load, args=(n,), daemon=True).start()
    return jsonify(started=n)


@app.get("/api/status")
def api_status():
    return jsonify(collect_status())


INDEX_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Replication PostgreSQL</title>
<style>
body{font-family:system-ui,sans-serif;margin:24px;background:#f6f7f9;color:#222}
h1{font-size:20px;margin:0 0 16px}
h2{font-size:16px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 3px #0001}
.scroll{height:260px;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{border-bottom:1px solid #eee;padding:5px 6px;text-align:left;vertical-align:top}
th{position:sticky;top:0;background:#fff}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#e5e7eb}
.primary{background:#d1fae5}.standby{background:#dbeafe}.down{background:#fee2e2}
.warn{background:#fef3c7;border-radius:6px;padding:6px 10px;margin:4px 0;font-size:13px}
.ev{font-family:ui-monospace,monospace;font-size:12px;padding:2px 0}
.ev.error{color:#b91c1c}.ev.warn{color:#b45309}
button{padding:6px 12px;border-radius:6px;border:1px solid #bbb;background:#fff;cursor:pointer}
input{padding:6px;border:1px solid #bbb;border-radius:6px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.row+.row{margin-top:12px}
#body{flex:1;min-width:0}
.big{font-size:28px;font-weight:600}
.muted{font-size:13px;color:#555;margin-top:6px}
.mono{font-family:ui-monospace,monospace;font-size:12px;word-break:break-all}
.status{font-family:ui-monospace,monospace;font-size:13px;margin-top:12px;min-height:18px}
.formula{margin-top:16px;padding:12px 14px;background:#f3f4f6;border-radius:8px}
.formula-title{font-size:12px;color:#555;text-transform:uppercase;letter-spacing:.04em}
.formula-body{font-size:15px;margin-top:6px}
</style></head><body>
<h1>Replication PostgreSQL</h1>

<div class="grid">

  <div class="card">
    <h2>Маршрутизация</h2>
    <div>Запись: <span class="badge primary" id="writer">—</span></div>
    <div style="margin-top:6px">Чтение: <span class="badge standby" id="reader">—</span></div>
    <div style="margin-top:6px">Режим: <span id="mode">—</span></div>
    <div id="warnings"></div>
  </div>

  <div class="card">
    <h2>Действия</h2>
    <div class="row">
      <input id="body" placeholder="текст события (необязательно)">
      <button id="create">Создать запись</button>
    </div>
    <div class="row">
      <input id="n" type="number" value="1000" min="1" max="20000" style="width:90px">
      <button id="loadbtn">Налить нагрузку</button>
    </div>
    <div class="status" id="msg"></div>
  </div>

  <div class="card">
    <h2>Узлы</h2>
    <table>
      <thead><tr><th>хост</th><th>роль</th><th>позиция WAL</th><th>отставание, байт</th></tr></thead>
      <tbody id="nodes"></tbody>
    </table>
  </div>

  <div class="card">
    <h2>Отставание standby</h2>
    <div class="big" id="lag-big">—</div>
    <div class="muted" id="lag-peak"></div>
    <div class="formula">
      <div class="formula-title">Как считается</div>
      <div class="formula-body">отставание = позиция WAL на primary − позиция replay на standby</div>
      <div class="muted">Объём журнала, который primary уже записал, а standby ещё не применил.</div>
    </div>
  </div>

  <div class="card">
    <h2>События сервиса</h2>
    <div class="scroll" id="events"></div>
  </div>

  <div class="card">
    <h2>Записи</h2>
    <div class="muted" id="rec-meta"></div>
    <div class="scroll" style="margin-top:8px">
      <table>
        <thead><tr><th>время</th><th>host</th><th>sev</th><th>текст</th></tr></thead>
        <tbody id="records"></tbody>
      </table>
    </div>
  </div>

</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
const fmtBytes = b => b == null ? '—'
  : b < 1024 ? b + ' B'
  : b < 1048576 ? (b / 1024).toFixed(1) + ' KB'
  : (b / 1048576).toFixed(2) + ' MB';

let peak = 0;
let lastLoadKey = '';

function setMsg(text) { $('#msg').textContent = text; }

async function refreshStatus() {
  try {
    const s = await api('/api/status');

    $('#writer').textContent = s.writer || 'ещё не определён';
    $('#reader').textContent = s.reader
      ? `${s.reader.host} ${s.reader.is_replica ? '(standby)' : '(primary — fallback)'}` : '—';
    $('#mode').textContent = s.mode;
    $('#warnings').innerHTML = s.warnings.map(w => `<div class="warn">⚠ ${esc(w)}</div>`).join('');

    $('#nodes').innerHTML = s.nodes.map(n => `<tr>
      <td>${esc(n.host)}</td>
      <td><span class="badge ${n.up ? n.role : 'down'}">${n.up ? n.role : 'недоступен'}</span></td>
      <td class="mono">${esc(n.lsn || '—')}</td>
      <td>${n.role === 'standby' ? fmtBytes(n.lag_bytes) : '—'}</td>
    </tr>`).join('');

    const lag = s.lag;
    if (lag && lag.lag_bytes != null) {
      peak = Math.max(peak, lag.lag_bytes);
      $('#lag-big').textContent = fmtBytes(lag.lag_bytes);
    } else if (lag && lag.error) {
      $('#lag-big').textContent = 'ошибка';
    } else {
      $('#lag-big').textContent = '—';
    }
    $('#lag-peak').textContent = `пик с открытия страницы: ${fmtBytes(peak)}`;

    $('#events').innerHTML = s.events.slice().reverse()
      .map(e => `<div class="ev ${e.level}">${e.ts} ${esc(e.msg)}</div>`).join('');

    const L = s.load;
    if (L.running) {
      setMsg(`нагрузка: ${L.done}/${L.total}`);
    } else if (L.total) {
      const key = `${L.total}:${L.done}:${L.error || ''}`;
      if (key !== lastLoadKey) {
        lastLoadKey = key;
        setMsg(L.error
          ? `нагрузка прервана ${L.done}/${L.total}: ${L.error}`
          : `нагрузка завершена ${L.done}/${L.total}`);
      }
    }
  } catch (e) {
    $('#warnings').innerHTML = `<div class="warn">статус: ${esc(e.message)}</div>`;
  }
}

async function loadEvents() {
  try {
    const r = await api('/api/events');
    $('#rec-meta').textContent =
      `читаю с ${r.served_by} · ${r.is_replica ? 'standby' : 'primary'} · событий: ${r.count}`;
    $('#records').innerHTML = r.rows.map(x =>
      `<tr><td>${esc(x.captured_at.slice(11, 23))}</td><td>${esc(x.host)}</td>
       <td>${x.severity}</td><td>${esc(x.body)}</td></tr>`
    ).join('');
  } catch (e) {
    $('#rec-meta').textContent = 'ошибка чтения: ' + e.message;
  }
}

async function createEvent() {
  try {
    const r = await api('/api/events', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({body: $('#body').value}),
    });
    $('#body').value = '';
    setMsg(`создано на ${r.node}, host ${r.host}`);
  } catch (e) {
    setMsg(`ошибка записи: ${e.message}`);
  }
  refreshStatus();
  loadEvents();
}

async function startLoad() {
  try {
    const n = $('#n').value;
    await api('/api/load?n=' + n, {method: 'POST'});
    setMsg(`нагрузка: 0/${n}`);
  } catch (e) {
    setMsg(`ошибка нагрузки: ${e.message}`);
  }
  refreshStatus();
}

$('#create').onclick = createEvent;
$('#loadbtn').onclick = startLoad;
setInterval(refreshStatus, 1000);
setInterval(loadEvents, 1000);
refreshStatus();
loadEvents();
</script>
</body></html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)