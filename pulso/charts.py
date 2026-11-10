"""Turn a query result + a small chart spec chosen by the model into a Plotly figure."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

PALETTE = ["#0F9D8A", "#3B6FD8", "#E4A23A", "#C2477A", "#7B61C9", "#5C6B73"]
CHART_TYPES = {"line", "bar", "stacked_bar", "area"}
Y_FORMATS = {"number", "usd", "percent"}


class ChartSpecError(ValueError):
    pass


@dataclass
class ChartSpec:
    query_id: str
    chart_type: str
    x: str
    y: list[str]
    title: str
    color: str | None = None
    y_format: str = "number"

    @classmethod
    def from_tool_input(cls, data: dict) -> "ChartSpec":
        y = data.get("y")
        if isinstance(y, str):
            y = [y]
        spec = cls(
            query_id=data.get("query_id", ""),
            chart_type=data.get("chart_type", "line"),
            x=data.get("x", ""),
            y=y or [],
            title=data.get("title", ""),
            color=data.get("color") or None,
            y_format=data.get("y_format", "number"),
        )
        if spec.chart_type not in CHART_TYPES:
            raise ChartSpecError(f"chart_type debe ser uno de {sorted(CHART_TYPES)}")
        if spec.y_format not in Y_FORMATS:
            raise ChartSpecError(f"y_format debe ser uno de {sorted(Y_FORMATS)}")
        if not spec.y:
            raise ChartSpecError("Falta al menos una columna en y.")
        return spec

    def validate_against(self, df: pd.DataFrame) -> None:
        missing = [c for c in [self.x, *self.y, self.color] if c and c not in df.columns]
        if missing:
            raise ChartSpecError(
                f"Columnas inexistentes en {self.query_id}: {missing}. Disponibles: {list(df.columns)}"
            )
        if self.color and len(self.y) > 1:
            raise ChartSpecError("Usa 'color' o varias columnas en 'y', pero no ambas.")


def build_figure(df: pd.DataFrame, spec: ChartSpec) -> go.Figure:
    spec.validate_against(df)
    data = df.copy()
    if len(spec.y) > 1:
        data = data.melt(id_vars=[spec.x], value_vars=spec.y, var_name="serie", value_name="valor")
        y, color = "valor", "serie"
    else:
        y, color = spec.y[0], spec.color

    kw = dict(x=spec.x, y=y, color=color, color_discrete_sequence=PALETTE)
    if spec.chart_type == "line":
        fig = px.line(data, markers=True, **kw)
    elif spec.chart_type == "area":
        fig = px.area(data, **kw)
    else:
        fig = px.bar(data, barmode="stack" if spec.chart_type == "stacked_bar" else "group", **kw)

    tickformat = {"usd": "$,.0f", "percent": ".1%", "number": ",.4~g"}[spec.y_format]
    fig.update_layout(
        title=dict(text=spec.title, x=0, font=dict(size=16)),
        template="plotly_white",
        margin=dict(l=10, r=10, t=50, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1, title=None),
        hovermode="x unified",
        height=380,
    )
    fig.update_yaxes(tickformat=tickformat, title=None, gridcolor="rgba(0,0,0,0.06)")
    fig.update_xaxes(title=None)
    return fig
