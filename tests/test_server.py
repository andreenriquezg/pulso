"""Web server tests: overview numbers, the NDJSON event stream and static file safety (no API key needed)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from pulso.server import Sessions, build_overview, make_handler
from test_agent_loop import FakeClient, resp, text, tool


def scripted():
    return FakeClient(
        [
            resp(tool("t1", "run_sql", sql="SELECT month, SUM(mrr) FROM monthly_kpis GROUP BY 1", purpose="MRR")),
            resp(tool("t2", "run_sql", sql="SELECT month, SUM(mrr_end_usd) AS mrr FROM monthly_kpis GROUP BY 1 ORDER BY 1",
                      purpose="MRR mensual")),
            resp(tool("t3", "make_chart", query_id="q1", chart_type="line", x="month", y=["mrr"], title="MRR", y_format="usd")),
            resp(text("El MRR de octubre es **$453,945**."), stop="end_turn"),
        ]
    )


@pytest.fixture()
def base_url(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, Sessions(db, client_factory=scripted)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10)


def test_overview_uses_governed_aggregation(db):
    ov = build_overview(db)
    vitals = {v["id"]: v for v in ov["vitals"]}
    assert ov["as_of"] == "2026-10-01"
    # MRR matches the semantic layer summed across countries
    expected = db.run("SELECT SUM(mrr_end_usd) AS m FROM monthly_kpis WHERE month = DATE '2026-10-01'").df["m"][0]
    assert vitals["mrr"]["series"][-1]["value"] == pytest.approx(expected)
    # global NRR is re-weighted by base MRR, never an average of country rates
    nrr = db.run(
        "SELECT SUM(retained_mrr_usd) / SUM(base_mrr_usd) AS n FROM nrr_12m WHERE month = DATE '2026-10-01'"
    ).df["n"][0]
    assert vitals["nrr"]["series"][-1]["value"] == pytest.approx(nrr)
    assert sum(c["mrr"] for c in ov["countries"]) == pytest.approx(expected)


def test_ask_streams_every_step(base_url):
    with post(f"{base_url}/api/ask", {"session": "s1", "question": "¿Cómo va el MRR?"}) as r:
        assert r.headers["Content-Type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in r.read().decode("utf-8").splitlines() if line.strip()]
    kinds = [e["kind"] for e in events]
    assert kinds == ["start", "query_error", "query", "chart", "answer"]
    query = events[2]
    assert query["id"] == "q1" and query["table"]["columns"] == ["month", "mrr"]
    assert query["table"]["rows"][0][0] == "2024-11-01"  # dates serialized as ISO strings
    chart = events[3]
    assert chart["spec"]["y"] == ["mrr"] and chart["data"]["total"] == 24
    assert events[-1]["stats"]["queries"] == 2


def test_overview_endpoint_and_index(base_url):
    with urllib.request.urlopen(f"{base_url}/api/overview", timeout=10) as r:
        ov = json.loads(r.read())
    assert ov["has_key"] is True and len(ov["vitals"]) == 4
    with urllib.request.urlopen(f"{base_url}/", timeout=10) as r:
        assert b"Pulso" in r.read()


def test_static_files_cannot_escape_web_dir(base_url):
    for path in ("/static/../pulso/db.py", "/static/..%2Fdata%2Fpulso.duckdb", "/static/nope.js"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base_url + path, timeout=10)
        assert e.value.code == 404
