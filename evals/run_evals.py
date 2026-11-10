"""
Run the golden question set against the live agent and print a scorecard.

    python evals/run_evals.py              # all cases
    python evals/run_evals.py nrr_global   # one case

Writes evals/results.md with the per-case detail.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pulso.agent import AnalyticsAgent  # noqa: E402
from pulso.db import Database  # noqa: E402

TOL = 0.001  # results come straight from SQL, so this can be tight


def numbers_in(results) -> list[float]:
    vals = []
    for r in results:
        for col in r.df.columns:
            s = r.df[col]
            if np.issubdtype(s.dtype, np.number):
                vals.extend(float(v) for v in s.dropna().tolist())
    return vals


def close(a: float, b: float) -> bool:
    return abs(a - b) <= TOL * max(abs(b), 1e-9)


def evaluate(case: dict, db: Database, agent: AnalyticsAgent) -> dict:
    agent.reset()
    events = list(agent.ask(case["question"]))
    answer = next(e.data["text"] for e in events if e.kind == "answer")
    results = [e.data["result"] for e in events if e.kind == "query"]
    errors = [e for e in events if e.kind == "query_error"]
    checks, ok = [], True

    if "golden_sql" in case:
        golden = [float(v) for v in db.run(case["golden_sql"]).df.iloc[0].tolist()]
        found = numbers_in(results)
        for g in golden:
            hit = any(close(v, g) for v in found)
            if abs(g) <= 1.5:  # a rate: accept it rounded to 0.1 pp, as a fraction or a percentage
                hit = hit or any(abs(v - g) <= 0.0005 or abs(v - 100 * g) <= 0.05 for v in found)
            checks.append(f"{'✅' if hit else '❌'} valor esperado {g:,.4f}")
            ok &= hit

    if "expect_text" in case:
        hit = any(t.lower() in answer.lower() for t in case["expect_text"])
        checks.append(f"{'✅' if hit else '❌'} texto esperado: {case['expect_text']}")
        ok &= hit

    if case.get("expect_no_successful_write"):
        n = db.run("SELECT COUNT(*) FROM customers WHERE country = 'CR'").df.iloc[0, 0]
        hit = n > 0
        checks.append(f"{'✅' if hit else '❌'} datos intactos ({n} clientes CR)")
        ok &= hit

    s = agent.last_stats
    return dict(
        id=case["id"],
        question=case["question"],
        ok=ok,
        checks=checks,
        answer=answer,
        queries=s.queries,
        corrections=len(errors),
        seconds=s.seconds,
        tokens=s.input_tokens + s.output_tokens,
        trap=case.get("trap"),
    )


def main() -> None:
    cases = yaml.safe_load((ROOT / "evals" / "questions.yaml").read_text())
    if len(sys.argv) > 1:
        cases = [c for c in cases if c["id"] in sys.argv[1:]]
    db = Database()
    agent = AnalyticsAgent(db=db)
    rows = []
    for c in cases:
        r = evaluate(c, db, agent)
        rows.append(r)
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['id']:<24} {r['queries']} q · {r['corrections']} fix · {r['seconds']:.1f}s")

    passed = sum(r["ok"] for r in rows)
    avg_s = sum(r["seconds"] for r in rows) / max(len(rows), 1)
    summary = f"{passed}/{len(rows)} correctas · {avg_s:.1f}s promedio por pregunta"
    print("\n" + summary)

    lines = [f"# Resultados de evaluación\n\n**{summary}**\n"]
    lines.append("| Caso | Resultado | Consultas | Correcciones | Segundos |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['id']} | {'✅' if r['ok'] else '❌'} | {r['queries']} | {r['corrections']} | {r['seconds']:.1f} |"
        )
    for r in rows:
        lines.append(f"\n## {r['id']}\n\n**Pregunta:** {r['question']}\n")
        if r["trap"]:
            lines.append(f"**Trampa que evalúa:** {r['trap']}\n")
        lines.extend(f"- {c}" for c in r["checks"])
        lines.append(f"\n**Respuesta del agente:**\n\n{r['answer']}\n")
    (ROOT / "evals" / "results.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
