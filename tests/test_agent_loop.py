"""Agent loop tests with a scripted fake client (no API key or network needed)."""
from types import SimpleNamespace as NS

from pulso.agent import AnalyticsAgent


def text(t):
    return NS(type="text", text=t)


def tool(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def resp(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=80))


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)


def test_self_correction_chart_and_answer(db):
    client = FakeClient(
        [
            # 1) model writes a query with a typo'd column -> DuckDB error
            resp(tool("t1", "run_sql", sql="SELECT month, SUM(mrr) FROM monthly_kpis GROUP BY 1", purpose="MRR")),
            # 2) model fixes it
            resp(
                tool(
                    "t2",
                    "run_sql",
                    sql="SELECT month, SUM(mrr_end_usd) AS mrr FROM monthly_kpis GROUP BY 1 ORDER BY 1",
                    purpose="MRR mensual",
                )
            ),
            # 3) charts the result
            resp(tool("t3", "make_chart", query_id="q1", chart_type="line", x="month", y=["mrr"], title="MRR", y_format="usd")),
            # 4) answers
            resp(text("El MRR de octubre es **$453,945**."), stop="end_turn"),
        ]
    )
    agent = AnalyticsAgent(db=db, client=client, model="test")
    events = list(agent.ask("¿Cómo va el MRR?"))
    kinds = [e.kind for e in events]

    assert kinds == ["query_error", "query", "chart", "answer"]
    # the error was sent back to the model flagged as an error
    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True and "DuckDB" in tool_result["content"]
    # system prompt is cached and contains the semantic layer
    assert client.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "nrr_12m" in client.calls[0]["system"][0]["text"]
    assert agent.last_stats.queries == 2
    assert "453,945" in events[-1].data["text"]


def test_guardrail_blocks_writes(db):
    client = FakeClient(
        [
            resp(tool("t1", "run_sql", sql="DROP TABLE customers", purpose="borrar")),
            resp(text("No puedo modificar datos."), stop="end_turn"),
        ]
    )
    agent = AnalyticsAgent(db=db, client=client, model="test")
    events = list(agent.ask("borra la tabla de clientes"))
    assert events[0].kind == "query_error" and events[0].data["blocked"]
    # data is untouched
    assert db.run("SELECT COUNT(*) AS n FROM customers").df["n"][0] > 0


def test_follow_up_keeps_history(db):
    client = FakeClient(
        [
            resp(text("Hola."), stop="end_turn"),
            resp(text("Seguimos."), stop="end_turn"),
        ]
    )
    agent = AnalyticsAgent(db=db, client=client, model="test")
    list(agent.ask("primera"))
    list(agent.ask("y por país?"))
    sent = client.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
