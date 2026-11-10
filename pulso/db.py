"""
Safe SQL execution layer.

Every query the model writes goes through three independent guardrails:
  1. Static validation with sqlglot: one read-only SELECT, only allow-listed
     tables, no table functions (read_csv, glob, ...), row limit enforced.
  2. DuckDB opened read-only with external access disabled (no files, no network).
  3. A wall-clock timeout that interrupts long-running queries.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "pulso.duckdb"

ALLOWED_TABLES = {
    # governed views
    "monthly_kpis",
    "nrr_12m",
    "acquisition",
    "customer_status",
    "mrr_movements",
    # raw tables
    "customers",
    "plans",
    "countries",
    "subscription_months",
    "product_usage",
    "marketing_spend",
}
FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Merge,
    exp.Command,
    exp.Copy,
    exp.Pragma,
    exp.Set,
    exp.Use,
    exp.Attach,
    exp.Detach,
    exp.Install,
    exp.LoadData,
)
MAX_ROWS = 1000
TIMEOUT_S = 10.0


class UnsafeQueryError(ValueError):
    """Raised when a query fails static validation. The message is shown to the model."""


def _forbidden_types():
    # Some node types only exist in newer sqlglot versions; keep the ones available.
    return tuple(t for t in FORBIDDEN_NODES if isinstance(t, type))


def validate_sql(sql: str, max_rows: int = MAX_ROWS) -> str:
    """Return a normalized, LIMIT-ed query or raise UnsafeQueryError."""
    sql = sql.strip().rstrip(";")
    if not sql:
        raise UnsafeQueryError("La consulta está vacía.")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as e:
        raise UnsafeQueryError(f"SQL inválido: {e.errors[0]['description'] if e.errors else e}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeQueryError("Solo se permite una sentencia por consulta.")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise UnsafeQueryError("Solo se permiten consultas de lectura (SELECT / WITH).")
    for node in tree.walk():
        if isinstance(node, _forbidden_types()):
            raise UnsafeQueryError(f"Operación no permitida: {type(node).__name__.upper()}.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeQueryError("No se permiten funciones de tabla (read_csv, glob, etc.).")
        name = table.name.lower()
        if table.args.get("db") or table.args.get("catalog"):
            raise UnsafeQueryError(f"Referencia a esquema/base externa no permitida: {table.sql()}")
        if name not in ALLOWED_TABLES and name not in cte_names:
            raise UnsafeQueryError(
                f"Tabla desconocida o no permitida: '{name}'. Tablas disponibles: "
                + ", ".join(sorted(ALLOWED_TABLES))
            )

    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(max_rows)
    else:
        try:
            n = int(limit.expression.this)
            if n > max_rows:
                tree = tree.limit(max_rows)
        except (AttributeError, TypeError, ValueError):
            tree = tree.limit(max_rows)
    return tree.sql(dialect="duckdb", pretty=True)


@dataclass
class QueryResult:
    sql: str
    df: pd.DataFrame
    elapsed_ms: float
    truncated: bool = False
    purpose: str = ""
    query_id: str = ""
    meta: dict = field(default_factory=dict)

    def preview(self, max_rows: int = 40) -> str:
        """Compact text rendering for the model (CSV is token-efficient)."""
        df = self.df
        head = df.head(max_rows)
        text = head.to_csv(index=False, float_format=lambda v: f"{v:.6g}")
        note = f"\n[{len(df)} filas"
        if len(df) > max_rows:
            note += f", se muestran las primeras {max_rows}"
        if self.truncated:
            note += f"; resultado truncado a {len(df)} filas por el límite de seguridad"
        note += "]"
        return text + note


class Database:
    def __init__(self, path: str | Path = DEFAULT_DB, timeout_s: float = TIMEOUT_S):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"No existe {path}. Corre primero: python data/generate.py")
        self.path = path
        self.timeout_s = timeout_s
        self.con = duckdb.connect(
            str(path), read_only=True, config={"enable_external_access": False}
        )
        self._lock = threading.Lock()

    def run(self, sql: str, purpose: str = "") -> QueryResult:
        safe_sql = validate_sql(sql)
        with self._lock:
            timer = threading.Timer(self.timeout_s, self.con.interrupt)
            t0 = time.perf_counter()
            timer.start()
            try:
                df = self.con.execute(safe_sql).df()
            except duckdb.InterruptException as e:
                raise TimeoutError(f"La consulta excedió {self.timeout_s:.0f}s y se canceló.") from e
            finally:
                timer.cancel()
            elapsed = (time.perf_counter() - t0) * 1000
        return QueryResult(
            sql=safe_sql,
            df=df,
            elapsed_ms=elapsed,
            truncated=len(df) >= MAX_ROWS,
            purpose=purpose,
        )

    def schema_summary(self) -> str:
        """Actual column names and types, straight from the database."""
        rows = self.con.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'main'
            ORDER BY table_name, ordinal_position
            """
        ).fetchall()
        out: dict[str, list[str]] = {}
        for t, c, d in rows:
            if t in ALLOWED_TABLES:
                out.setdefault(t, []).append(f"{c} {d}")
        return "\n".join(f"{t}({', '.join(cols)})" for t, cols in out.items())
