"""Terminal interface: python -m pulso.cli "¿Cuál es el MRR de octubre?" """
from __future__ import annotations

import sys

from .agent import AnalyticsAgent


def main() -> None:
    agent = AnalyticsAgent()
    questions = sys.argv[1:] or [None]
    for q in questions:
        while True:
            question = q or input("\n❓ Pregunta (enter para salir): ").strip()
            if not question:
                return
            for ev in agent.ask(question):
                if ev.kind == "query":
                    r = ev.data["result"]
                    print(f"\n[{r.query_id}] {r.purpose}  ({len(r.df)} filas, {r.elapsed_ms:.0f} ms)\n  {r.sql}")
                elif ev.kind == "query_error":
                    print(f"\n[error → reintentando] {ev.data['error']}")
                elif ev.kind == "chart":
                    print(f"\n[gráfica] {ev.data['spec'].title}")
                elif ev.kind == "answer":
                    s = ev.data["stats"]
                    print("\n" + ev.data["text"])
                    print(f"\n— {s.queries} consultas · {s.seconds:.1f}s · {s.input_tokens + s.output_tokens:,} tokens")
            if q:
                break


if __name__ == "__main__":
    main()
