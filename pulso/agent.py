"""
The agent loop: Claude plans, writes SQL, reads results, self-corrects on
errors, optionally charts, and answers. Every intermediate step is emitted as
an event so the UI can show exactly how the answer was built.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from .charts import ChartSpec, ChartSpecError, build_figure
from .db import Database, QueryResult, UnsafeQueryError
from .prompts import TOOLS, build_system_prompt

DEFAULT_MODEL = os.getenv("PULSO_MODEL", "claude-sonnet-5-5")
MAX_STEPS = 8
# The model reasons before answering and that reasoning counts toward max_tokens,
# so leave plenty of room; a turn cut short is retried instead of shown.
MAX_TOKENS = 8192
MAX_RETRIES = 2


# ----------------------------------------------------------------------------
# Events streamed to the UI / CLI
# ----------------------------------------------------------------------------
@dataclass
class Event:
    kind: str  # "thinking" | "query" | "query_error" | "chart" | "chart_error" | "answer" | "error"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunStats:
    steps: int = 0
    queries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    seconds: float = 0.0


def _block_to_dict(block: Any) -> dict:
    """Convert an SDK content block (or a test double) into a plain dict for the message history."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if block.type == "thinking":  # must be sent back unchanged, signature included
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    raise ValueError(f"Bloque desconocido: {block.type}")


class AnalyticsAgent:
    def __init__(self, db: Database | None = None, client: Any = None, model: str = DEFAULT_MODEL):
        self.db = db or Database()
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.system = [
            {
                "type": "text",
                "text": build_system_prompt(self.db.schema_summary()),
                "cache_control": {"type": "ephemeral"},  # semantic layer is cached across turns
            }
        ]
        self.messages: list[dict] = []  # conversation memory for follow-up questions
        self.results: dict[str, QueryResult] = {}
        self.last_stats = RunStats()

    def reset(self) -> None:
        self.messages.clear()
        self.results.clear()

    # ------------------------------------------------------------------ tools
    def _tool_run_sql(self, args: dict) -> tuple[str, bool, Event]:
        sql = args.get("sql", "")
        purpose = args.get("purpose", "")
        try:
            res = self.db.run(sql, purpose=purpose)
        except (UnsafeQueryError, TimeoutError) as e:
            return f"ERROR (bloqueado por guardrail): {e}", True, Event(
                "query_error", {"sql": sql, "purpose": purpose, "error": str(e), "blocked": True}
            )
        except Exception as e:  # DuckDB binder/parser errors -> let the model fix them
            msg = str(e).split("\n")[0][:500]
            return f"ERROR de DuckDB: {msg}", True, Event(
                "query_error", {"sql": sql, "purpose": purpose, "error": msg, "blocked": False}
            )
        qid = f"q{len(self.results) + 1}"
        res.query_id = qid
        self.results[qid] = res
        payload = f"query_id={qid}\n{res.preview()}"
        return payload, False, Event("query", {"result": res})

    def _tool_make_chart(self, args: dict) -> tuple[str, bool, Event]:
        try:
            spec = ChartSpec.from_tool_input(args)
            res = self.results.get(spec.query_id)
            if res is None:
                raise ChartSpecError(f"No existe {spec.query_id}. Disponibles: {list(self.results)}")
            fig = build_figure(res.df, spec)
        except ChartSpecError as e:
            return f"ERROR: {e}", True, Event("chart_error", {"error": str(e), "spec": args})
        return f"Gráfica '{spec.title}' creada.", False, Event("chart", {"figure": fig, "spec": spec})

    def _dispatch(self, name: str, args: dict) -> tuple[str, bool, Event]:
        if name == "run_sql":
            return self._tool_run_sql(args)
        if name == "make_chart":
            return self._tool_make_chart(args)
        return f"ERROR: herramienta desconocida {name}", True, Event("error", {"error": name})

    # ------------------------------------------------------------------- loop
    def ask(self, question: str) -> Iterator[Event]:
        stats = RunStats()
        t0 = time.perf_counter()
        self.messages.append({"role": "user", "content": question})

        retries = 0
        for _ in range(MAX_STEPS + MAX_RETRIES):
            if stats.steps >= MAX_STEPS:
                break
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=self.system,
                tools=TOOLS,
                messages=list(self.messages),
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                stats.input_tokens += getattr(usage, "input_tokens", 0) or 0
                stats.output_tokens += getattr(usage, "output_tokens", 0) or 0
                stats.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

            content = [_block_to_dict(b) for b in response.content]
            has_text = any(b["type"] == "text" and b["text"].strip() for b in content)
            has_tool = any(b["type"] == "tool_use" for b in content)
            # A turn that was cut off (max_tokens) or produced only internal reasoning is
            # unusable: a truncated tool call has incomplete input and an empty answer
            # tells the user nothing. Drop it from the history and ask again.
            cut_off = response.stop_reason == "max_tokens"
            empty = response.stop_reason != "tool_use" and not has_text
            if (cut_off or empty or (response.stop_reason == "tool_use" and not has_tool)) and retries < MAX_RETRIES:
                retries += 1
                continue
            stats.steps += 1
            self.messages.append({"role": "assistant", "content": content})

            if response.stop_reason != "tool_use":
                text = "\n".join(b["text"] for b in content if b["type"] == "text").strip()
                if not text:
                    text = ("No logré redactar una respuesta para esta pregunta. Intenta reformularla "
                            "o hacerla más específica (por ejemplo, un periodo o un país).")
                stats.seconds = time.perf_counter() - t0
                self.last_stats = stats
                yield Event("answer", {"text": text, "stats": stats})
                return

            # narration the model wrote before calling tools (if any)
            for b in content:
                if b["type"] == "text" and b["text"].strip():
                    yield Event("thinking", {"text": b["text"].strip()})

            tool_results = []
            for b in content:
                if b["type"] != "tool_use":
                    continue
                if b["name"] == "run_sql":
                    stats.queries += 1
                payload, is_error, event = self._dispatch(b["name"], b["input"])
                yield event
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": b["id"], "content": payload, "is_error": is_error}
                )
            self.messages.append({"role": "user", "content": tool_results})

        stats.seconds = time.perf_counter() - t0
        self.last_stats = stats
        msg = "No llegué a una respuesta confiable en el número máximo de pasos. Intenta acotar la pregunta."
        self.messages.append({"role": "assistant", "content": [{"type": "text", "text": msg}]})
        yield Event("answer", {"text": msg, "stats": stats})
