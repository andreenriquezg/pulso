"""The semantic layer must be internally consistent, or nothing built on it can be trusted."""
import pytest

from pulso.db import UnsafeQueryError, validate_sql


def q(db, sql):
    return db.run(sql).df


def test_mrr_bridge_closes(db):
    bad = q(
        db,
        """SELECT COUNT(*) AS n FROM monthly_kpis
           WHERE ABS(mrr_start_usd + new_mrr_usd + expansion_mrr_usd - contraction_mrr_usd
                     - churned_mrr_usd + reactivation_mrr_usd - mrr_end_usd) > 0.01""",
    )
    assert bad["n"][0] == 0


def test_mrr_matches_raw_subscriptions(db):
    df = q(
        db,
        """SELECT k.month, k.mrr, s.mrr AS raw
           FROM (SELECT month, SUM(mrr_end_usd) mrr FROM monthly_kpis GROUP BY 1) k
           JOIN (SELECT month, SUM(mrr_usd) mrr FROM subscription_months GROUP BY 1) s USING (month)""",
    )
    assert len(df) == 24
    assert (df["mrr"] - df["raw"]).abs().max() < 0.01


def test_customer_counts_roll_forward(db):
    bad = q(
        db,
        """SELECT COUNT(*) AS n FROM monthly_kpis
           WHERE customers_start + new_customers + reactivated_customers - churned_customers <> customers_end""",
    )
    assert bad["n"][0] == 0


def test_grr_never_exceeds_nrr_or_one(db):
    df = q(db, "SELECT nrr, grr FROM nrr_12m")
    assert (df["grr"] <= df["nrr"] + 1e-9).all()
    assert (df["grr"] <= 1 + 1e-9).all()


def test_spend_reconciles(db):
    df = q(
        db,
        """SELECT (SELECT SUM(spend_usd) FROM acquisition) a, (SELECT SUM(spend_usd) FROM marketing_spend) b""",
    )
    assert abs(df["a"][0] - df["b"][0]) < 0.01


def test_planted_stories_are_detectable(db):
    pe = q(
        db,
        """SELECT SUM(churned_customers)::DOUBLE / SUM(customers_start) AS churn,
                  month BETWEEN '2026-04-01' AND '2026-06-01' AS shock
           FROM monthly_kpis WHERE country = 'PE' AND month >= '2026-01-01' GROUP BY 2""",
    ).set_index("shock")["churn"]
    assert pe[True] > 1.5 * pe[False]

    co = q(
        db,
        """SELECT SUM(spend_usd) / SUM(new_customers) AS cac, month >= '2026-06-01' AS shock
           FROM acquisition WHERE country = 'CO' AND channel = 'paid_social' AND month >= '2026-01-01'
           GROUP BY 2""",
    ).set_index("shock")["cac"]
    assert co[True] > 1.4 * co[False]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "DELETE FROM customers",
        "UPDATE plans SET list_price_usd = 0",
        "CREATE TABLE x AS SELECT 1",
        "SELECT 1; DROP TABLE customers",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM '/etc/passwd'",
        "COPY customers TO 'out.csv'",
        "ATTACH 'other.db'",
        "INSTALL httpfs",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM secret_table",
        "PRAGMA database_list",
    ],
)
def test_guardrail_rejects(sql):
    with pytest.raises(UnsafeQueryError):
        validate_sql(sql)


def test_guardrail_allows_ctes_and_caps_limit():
    out = validate_sql("WITH a AS (SELECT * FROM monthly_kpis) SELECT * FROM a LIMIT 999999")
    assert out.rstrip().endswith("LIMIT 1000")
    assert validate_sql("SELECT 1 AS x").rstrip().endswith("LIMIT 1000")
