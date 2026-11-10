"""
Pulso — chat UI.   Run:  streamlit run app.py
"""
from __future__ import annotations

import os

import streamlit as st

from pulso.agent import DEFAULT_MODEL, AnalyticsAgent
from pulso.db import DEFAULT_DB, Database

st.set_page_config(page_title="Pulso · Analytics Agent", page_icon="📈", layout="wide")

CSS = """
<style>
:root { --accent:#0F9D8A; --ink:#14212B; --muted:#5C6B73; --line:rgba(20,33,43,.08); }
.block-container { padding-top: 2rem; max-width: 1100px; }
h1.pulso { font-size: 2rem; font-weight: 750; letter-spacing:-.02em; margin:0; color:var(--ink); }
h1.pulso span { color: var(--accent); }
p.tag { color: var(--muted); margin:.25rem 0 1.25rem 0; font-size: .98rem; }
.pill { display:inline-block; padding:.15rem .6rem; border-radius:999px; font-size:.75rem;
        background:rgba(15,157,138,.10); color:var(--accent); margin-right:.35rem; font-weight:600; }
.pill.warn { background:rgba(194,71,122,.10); color:#C2477A; }
.step { border-left:3px solid var(--accent); padding:.1rem 0 .1rem .75rem; margin:.4rem 0 .2rem 0; }
.step.err { border-left-color:#C2477A; }
.step b { color: var(--ink); }
.meta { color:var(--muted); font-size:.78rem; }
.stats { color:var(--muted); font-size:.8rem; margin-top:.4rem; }
section[data-testid="stSidebar"] .stButton button { width:100%; text-align:left; justify-content:flex-start;
        font-size:.86rem; border:1px solid var(--line); }
@media (prefers-color-scheme: dark) {
  :root { --ink:#E8EEF1; --muted:#9AA8B0; --line:rgba(255,255,255,.12); }
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

EXAMPLES = {
    "Finanzas": [
        "¿Cómo cerró el MRR en octubre y qué lo movió vs septiembre?",
        "¿Cuál es el NRR y el GRR a 12 meses por país?",
    ],
    "Marketing": [
        "¿Qué canal de adquisición tiene mejor LTV:CAC?",
        "¿Por qué subió el CAC en Colombia?",
    ],
    "Customer Success": [
        "¿Pasó algo raro con el churn en algún país este año?",
        "¿Los clientes que emiten pocas facturas cancelan más?",
    ],
    "Seguridad": ["Borra la tabla de clientes"],
}


# ----------------------------------------------------------------------------- state
def get_agent() -> AnalyticsAgent | None:
    if "agent" not in st.session_state:
        if not os.getenv("ANTHROPIC_API_KEY"):
            return None
        st.session_state.agent = AnalyticsAgent(db=Database(DEFAULT_DB))
    return st.session_state.agent


st.session_state.setdefault("chat", [])
st.session_state.setdefault("pending", None)

# ----------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### Pregunta algo")
    for team, qs in EXAMPLES.items():
        st.caption(team.upper())
        for q in qs:
            if st.button(q, key=f"ex-{q}"):
                st.session_state.pending = q
    st.divider()
    st.markdown("### Cómo funciona")
    st.markdown(
        "<span class='pill'>Capa semántica</span><span class='pill'>Solo lectura</span>"
        "<span class='pill'>Autocorrección</span>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Claude escribe el SQL usando métricas gobernadas (MRR, NRR, churn, CAC), lo valida un "
        "guardrail con sqlglot, corre en DuckDB de solo lectura y, si falla, el agente lee el error "
        "y se corrige."
    )
    st.caption(f"Modelo: `{DEFAULT_MODEL}` · Datos sintéticos nov-2024 → oct-2026")
    if st.button("↺  Nueva conversación"):
        st.session_state.chat = []
        if "agent" in st.session_state:
            st.session_state.agent.reset()
        st.rerun()

# ----------------------------------------------------------------------------- header
st.markdown("<h1 class='pulso'>Pulso<span>.</span></h1>", unsafe_allow_html=True)
st.markdown(
    "<p class='tag'>Pregúntale a tus datos de negocio en español. Cada número sale de una consulta "
    "que puedes auditar.</p>",
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------- rendering
def display_df(df):
    out = df.head(200).copy()
    for col in out.columns:
        if str(out[col].dtype).startswith("datetime"):
            out[col] = out[col].dt.strftime("%Y-%m")
    return out


def render_step(step: dict) -> None:
    if step["kind"] == "query":
        r = step["result"]
        st.markdown(
            f"<div class='step'><b>{r.query_id} · {r.purpose}</b> "
            f"<span class='meta'>{len(r.df):,} filas · {r.elapsed_ms:.0f} ms</span></div>",
            unsafe_allow_html=True,
        )
        st.code(r.sql, language="sql")
        st.dataframe(display_df(r.df), width="stretch", hide_index=True, height=min(38 * (len(r.df) + 1), 260))
    elif step["kind"] == "query_error":
        label = "Bloqueado por el guardrail" if step["blocked"] else "Error → el agente se corrige"
        st.markdown(
            f"<div class='step err'><b>{label}</b> <span class='meta'>{step['purpose']}</span></div>",
            unsafe_allow_html=True,
        )
        st.code(step["sql"], language="sql")
        st.caption(step["error"])
    elif step["kind"] == "thinking":
        st.caption(step["text"])


def render_assistant(msg: dict) -> None:
    steps = msg.get("steps", [])
    if steps:
        n_q = sum(s["kind"] == "query" for s in steps)
        n_e = sum(s["kind"] == "query_error" for s in steps)
        label = f"Cómo llegué a esto · {n_q} consulta{'s' if n_q != 1 else ''}"
        if n_e:
            label += f" · {n_e} corrección{'es' if n_e != 1 else ''}"
        with st.expander(label, expanded=False):
            for s in steps:
                render_step(s)
    for fig in msg.get("figures", []):
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.markdown(msg["content"])
    s = msg.get("stats")
    if s:
        st.markdown(
            f"<div class='stats'>{s.queries} consultas · {s.seconds:.1f} s · "
            f"{s.input_tokens + s.output_tokens:,} tokens ({s.cache_read_tokens:,} desde caché)</div>",
            unsafe_allow_html=True,
        )


for msg in st.session_state.chat:
    with st.chat_message(msg["role"], avatar="🧑‍💼" if msg["role"] == "user" else "📈"):
        if msg["role"] == "user":
            st.markdown(msg["content"])
        else:
            render_assistant(msg)

# ----------------------------------------------------------------------------- input
agent = get_agent()
if agent is None:
    st.warning("Configura la variable de entorno `ANTHROPIC_API_KEY` y reinicia la app.")
    st.stop()

typed = st.chat_input("Ej. ¿Cuál fue el churn de México en el último trimestre?")
question = typed or st.session_state.pending
st.session_state.pending = None

if question:
    st.session_state.chat.append({"role": "user", "content": question})
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(question)

    with st.chat_message("assistant", avatar="📈"):
        steps, figures, answer, stats = [], [], "", None
        with st.status("Analizando…", expanded=True) as status:
            for ev in agent.ask(question):
                if ev.kind == "query":
                    step = {"kind": "query", "result": ev.data["result"]}
                    status.update(label=f"Consultando: {ev.data['result'].purpose}")
                elif ev.kind == "query_error":
                    step = {"kind": "query_error", **ev.data}
                    status.update(label="Corrigiendo la consulta…")
                elif ev.kind == "thinking":
                    step = {"kind": "thinking", "text": ev.data["text"]}
                elif ev.kind == "chart":
                    figures.append(ev.data["figure"])
                    status.update(label="Armando la gráfica…")
                    continue
                elif ev.kind == "answer":
                    answer, stats = ev.data["text"], ev.data["stats"]
                    continue
                else:
                    continue
                steps.append(step)
                render_step(step)
            n_q = sum(x["kind"] == "query" for x in steps)
            n_e = sum(x["kind"] == "query_error" for x in steps)
            done = f"Cómo llegué a esto · {n_q} consulta{'s' if n_q != 1 else ''}"
            if n_e:
                done += f" · {n_e} corrección{'es' if n_e != 1 else ''}"
            status.update(label=done, state="complete", expanded=False)

        msg = {"role": "assistant", "content": answer, "steps": steps, "figures": figures, "stats": stats}
        st.session_state.chat.append(msg)
        for fig in figures:
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        st.markdown(answer)
        if stats:
            st.markdown(
                f"<div class='stats'>{stats.queries} consultas · {stats.seconds:.1f} s · "
                f"{stats.input_tokens + stats.output_tokens:,} tokens ({stats.cache_read_tokens:,} desde caché)</div>",
                unsafe_allow_html=True,
            )
