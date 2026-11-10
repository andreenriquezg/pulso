"""
Pulso web server: serves the UI and streams every agent step to the browser.

    python -m pulso.server            # http://localhost:8000
    python -m pulso.server --port 8100

Standard library only (no web framework): one JSON endpoint for the
"vital signs" overview and one NDJSON stream per question, so the UI can
draw each query, correction and chart the moment it happens.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import uuid
import webbrowser
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .agent import DEFAULT_MODEL, AnalyticsAgent, Event
from .db import DEFAULT_DB, Database, QueryResult

WEB = Path(__file__).resolve().parents[1] / "web"
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
PREVIEW_ROWS = 30
CHART_ROWS = 600


# ----------------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------------
def _cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v if isinstance(v, (int, str)) else str(v)


def frame(df: pd.DataFrame, limit: int) -> dict:
    head = df.head(limit)
    return {
        "columns": [str(c) for c in head.columns],
        "types": ["number" if pd.api.types.is_numeric_dtype(head[c]) else "text" for c in head.columns],
        "rows": [[_cell(v) for v in row] for row in head.itertuples(index=False, name=None)],
        "total": int(len(df)),
    }


def serialize(ev: Event, agent: AnalyticsAgent) -> dict | None:
    d = ev.data
    if ev.kind == "query":
        r: QueryResult = d["result"]
        return {
            "kind": "query",
            "id": r.query_id,
            "purpose": r.purpose,
            "sql": r.sql,
            "ms": round(r.elapsed_ms, 1),
            "truncated": r.truncated,
            "table": frame(r.df, PREVIEW_ROWS),
        }
    if ev.kind == "query_error":
        return {"kind": "query_error", "purpose": d.get("purpose", ""), "sql": d.get("sql", ""),
                "error": d.get("error", ""), "blocked": bool(d.get("blocked"))}
    if ev.kind == "thinking":
        return {"kind": "thinking", "text": d["text"]}
    if ev.kind == "chart":
        spec = d["spec"]
        res = agent.results.get(spec.query_id)
        return {
            "kind": "chart",
            "spec": {"query_id": spec.query_id, "chart_type": spec.chart_type, "x": spec.x, "y": spec.y,
                     "title": spec.title, "color": spec.color, "y_format": spec.y_format},
            "data": frame(res.df, CHART_ROWS) if res is not None else None,
        }
    if ev.kind == "chart_error":
        return {"kind": "chart_error", "error": d.get("error", "")}
    if ev.kind == "answer":
        s = d["stats"]
        return {"kind": "answer", "text": d["text"], "stats": {
            "queries": s.queries, "steps": s.steps, "seconds": round(s.seconds, 2),
            "tokens": s.input_tokens + s.output_tokens, "cached": s.cache_read_tokens}}
    if ev.kind == "error":
        return {"kind": "error", "error": d.get("error", "")}
    return None


# ----------------------------------------------------------------------------
# Overview: the "vital signs" panel, computed from the governed views
# ----------------------------------------------------------------------------
OVERVIEW_SQL = {
    "monthly": """
        SELECT month,
               SUM(mrr_end_usd)                                         AS mrr,
               SUM(churned_customers)::DOUBLE / NULLIF(SUM(customers_start), 0) AS logo_churn,
               SUM(customers_end)                                       AS customers
        FROM monthly_kpis GROUP BY 1 ORDER BY 1""",
    "nrr": """
        SELECT month, SUM(retained_mrr_usd) / SUM(base_mrr_usd) AS nrr
        FROM nrr_12m GROUP BY 1 HAVING SUM(base_mrr_usd) > 0 ORDER BY 1""",
    "cac": """
        SELECT month, SUM(spend_usd) / NULLIF(SUM(new_customers), 0) AS cac
        FROM acquisition GROUP BY 1 ORDER BY 1""",
    "countries": """
        SELECT k.country, c.country_name, SUM(k.mrr_end_usd) AS mrr
        FROM monthly_kpis k JOIN countries c USING (country)
        WHERE k.month = (SELECT MAX(month) FROM monthly_kpis)
        GROUP BY 1, 2 ORDER BY mrr DESC""",
}


def build_overview(db: Database) -> dict:
    q = {k: db.run(sql, purpose=f"overview:{k}").df for k, sql in OVERVIEW_SQL.items()}

    def series(df: pd.DataFrame, col: str) -> list:
        return [{"month": _cell(m), "value": _cell(v)} for m, v in zip(df["month"], df[col])]

    monthly = q["monthly"]
    return {
        "as_of": _cell(monthly["month"].iloc[-1]),
        "first": _cell(monthly["month"].iloc[0]),
        "model": DEFAULT_MODEL,
        "has_key": bool(os.getenv("ANTHROPIC_API_KEY")),
        "vitals": [
            {"id": "mrr", "label": "MRR", "format": "usd", "series": series(monthly, "mrr"), "good": "up"},
            {"id": "nrr", "label": "NRR 12m", "format": "percent", "series": series(q["nrr"], "nrr"), "good": "up"},
            {"id": "churn", "label": "Churn de logos", "format": "percent", "series": series(monthly, "logo_churn"),
             "good": "down"},
            {"id": "cac", "label": "CAC blended", "format": "usd", "series": series(q["cac"], "cac"), "good": "down"},
        ],
        "countries": [
            {"code": r.country, "name": r.country_name, "mrr": _cell(r.mrr)}
            for r in q["countries"].itertuples(index=False)
        ],
        "customers": _cell(monthly["customers"].iloc[-1]),
    }


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Sessions:
    """One agent (and its conversation memory) per browser tab."""

    def __init__(self, db: Database, client_factory=None):
        self.db = db
        self.client_factory = client_factory  # tests inject a scripted client
        self._agents: dict[str, AnalyticsAgent] = {}
        self._lock = threading.Lock()

    def get(self, sid: str) -> AnalyticsAgent:
        with self._lock:
            if sid not in self._agents:
                client = self.client_factory() if self.client_factory else None
                self._agents[sid] = AnalyticsAgent(db=self.db, client=client)
            return self._agents[sid]

    def reset(self, sid: str) -> None:
        with self._lock:
            agent = self._agents.get(sid)
        if agent:
            agent.reset()


def make_handler(db: Database, sessions: Sessions):
    overview_cache: dict = {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Pulso/1.0"

        def log_message(self, fmt, *args):  # quieter console
            if not self.path.startswith("/static"):
                super().log_message(fmt, *args)

        # -- helpers
        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 64_000:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _static(self, rel: str) -> None:
            path = (WEB / rel).resolve()
            if WEB not in path.parents and path != WEB or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        # -- routes
        def do_GET(self):  # noqa: N802
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                return self._static("index.html")
            if route.startswith("/static/"):
                return self._static(route[len("/static/"):])
            if route == "/api/overview":
                if "data" not in overview_cache:
                    overview_cache["data"] = build_overview(db)
                data = dict(overview_cache["data"], has_key=bool(os.getenv("ANTHROPIC_API_KEY")))
                return self._json(data)
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):  # noqa: N802
            route = self.path.split("?", 1)[0]
            body = self._body()
            sid = str(body.get("session") or "default")[:64]
            if route == "/api/reset":
                sessions.reset(sid)
                return self._json({"ok": True})
            if route != "/api/ask":
                return self.send_error(HTTPStatus.NOT_FOUND)

            question = str(body.get("question") or "").strip()[:2000]
            if not question:
                return self._json({"error": "Pregunta vacía."}, 400)

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(payload: dict) -> None:
                self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()

            try:
                if not os.getenv("ANTHROPIC_API_KEY"):
                    emit({"kind": "error", "error": "Falta ANTHROPIC_API_KEY en el entorno del servidor."})
                    return
                agent = sessions.get(sid)
                emit({"kind": "start", "id": uuid.uuid4().hex[:8]})
                for ev in agent.ask(question):
                    payload = serialize(ev, agent)
                    if payload is not None:
                        emit(payload)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:  # surface API/network errors in the UI instead of a dead stream
                try:
                    emit({"kind": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"})
                except OSError:
                    pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Pulso web UI")
    ap.add_argument("--port", type=int, default=int(os.getenv("PULSO_PORT", "8000")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    db = Database(DEFAULT_DB)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(db, Sessions(db)))
    httpd.daemon_threads = True
    url = f"http://localhost:{args.port}"
    print(f"Pulso listo en {url}  (Ctrl+C para salir)")
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Aviso: falta ANTHROPIC_API_KEY; la interfaz cargará pero el agente no podrá responder.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
