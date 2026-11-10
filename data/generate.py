"""
Generate a synthetic, reproducible dataset for a LatAm SaaS business
(cloud accounting & e-invoicing for SMBs) and load it into DuckDB.

The data is fake but behaves like the real thing:
  * 24 months of history (Nov 2024 - Oct 2026), 5 countries, 5 acquisition channels
  * subscriptions with upgrades, downgrades, churn and reactivations
  * product usage (invoices issued) that actually predicts churn
  * marketing spend per country/channel so CAC can be computed

Two "stories" are planted on purpose so the agent has something real to find:
  1. Peru raised prices on its two entry plans in Apr 2026 -> churn spike Apr-Jun 2026.
  2. Colombia's paid social spend jumped from Jun 2026 without more signups -> CAC blew up.

Usage:
    python data/generate.py            # writes data/pulso.duckdb
    python data/generate.py --out x.db
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SEED = 20261110
START = date(2024, 11, 1)
N_MONTHS = 24  # Nov 2024 .. Oct 2026

COUNTRIES = {  # country -> relative size
    "MX": 1.00,
    "CO": 1.15,
    "PE": 0.50,
    "CR": 0.40,
    "DO": 0.35,
}

# channel -> (base new customers / month for size 1.0, target CAC USD, churn multiplier)
CHANNELS = {
    "paid_search": (34, 95.0, 1.10),
    "paid_social": (30, 70.0, 1.40),
    "contadores": (22, 40.0, 0.60),  # accountant partner program (commission per signup)
    "organico": (26, 0.0, 1.00),
    "referidos": (10, 15.0, 0.80),  # referral credit
}

PLANS = [
    # plan_id, name, monthly price USD, base monthly churn, base invoices/month
    ("EMP", "Emprendedor", 15.0, 0.038, 20),
    ("PYM", "Pyme", 35.0, 0.024, 60),
    ("PLU", "Plus", 65.0, 0.017, 150),
    ("PRO", "Pro", 120.0, 0.012, 400),
]
PLAN_IDS = [p[0] for p in PLANS]
PRICE = {p[0]: p[2] for p in PLANS}
BASE_CHURN = {p[0]: p[3] for p in PLANS}
BASE_USAGE = {p[0]: p[4] for p in PLANS}

INDUSTRIES = ["comercio", "servicios", "restaurantes", "construccion", "salud", "manufactura", "tecnologia"]
IND_P = [0.30, 0.25, 0.12, 0.09, 0.08, 0.09, 0.07]
BANDS = ["1-5", "6-20", "21-50", "51+"]
BAND_P = [0.52, 0.30, 0.12, 0.06]
# starting plan probabilities by employee band
PLAN_BY_BAND = {
    "1-5": [0.62, 0.30, 0.07, 0.01],
    "6-20": [0.22, 0.48, 0.24, 0.06],
    "21-50": [0.05, 0.30, 0.45, 0.20],
    "51+": [0.01, 0.12, 0.40, 0.47],
}

PE_PRICE_CHANGE = 17  # month index of Apr 2026
PE_PRICE_FACTOR = 1.20  # +20% on EMP and PYM
CO_SOCIAL_SHOCK = 19  # month index of Jun 2026


def month_at(i: int) -> date:
    y, m = divmod(START.month - 1 + i, 12)
    return date(START.year + y, m + 1, 1)


def price_for(plan: str, country: str, t: int) -> float:
    p = PRICE[plan]
    if country == "PE" and plan in ("EMP", "PYM") and t >= PE_PRICE_CHANGE:
        p *= PE_PRICE_FACTOR
    return round(p, 2)


def simulate(seed: int = SEED):
    rng = np.random.default_rng(seed)
    customers, sub_rows, usage_rows, spend_rows = [], [], [], []
    active: dict[str, dict] = {}  # currently subscribed
    churned: dict[str, dict] = {}  # churned, may reactivate
    next_id = 1

    for t in range(N_MONTHS):
        month = month_at(t)
        growth = 1.025**t
        seasonal = 1.0 + 0.12 * np.cos(2 * np.pi * (month.month - 1) / 12)  # Jan peak (tax season)

        # ---- acquisition + marketing spend ---------------------------------
        for country, size in COUNTRIES.items():
            for channel, (base, cac, _) in CHANNELS.items():
                lam = base * size * growth * seasonal
                n_new = int(rng.poisson(lam))
                if cac > 0:
                    spend = n_new * cac * rng.normal(1.0, 0.08)
                    if country == "CO" and channel == "paid_social" and t >= CO_SOCIAL_SHOCK:
                        spend *= 1.0 + 0.55 + 0.06 * (t - CO_SOCIAL_SHOCK)  # spend up, signups flat
                    spend_rows.append((month, country, channel, round(max(spend, 0.0), 2)))
                for _ in range(n_new):
                    cid = f"C{next_id:05d}"
                    next_id += 1
                    band = rng.choice(BANDS, p=BAND_P)
                    plan = rng.choice(PLAN_IDS, p=PLAN_BY_BAND[band])
                    engagement = float(rng.lognormal(0.0, 0.55))  # latent usage propensity
                    day = int(rng.integers(1, 29))
                    customers.append(
                        dict(
                            customer_id=cid,
                            country=country,
                            industry=rng.choice(INDUSTRIES, p=IND_P),
                            employees_band=band,
                            acquisition_channel=channel,
                            signup_date=date(month.year, month.month, day),
                        )
                    )
                    active[cid] = dict(
                        plan=plan, country=country, channel=channel, eng=engagement, start=t, tenure=0
                    )

        # ---- reactivations -------------------------------------------------
        for cid in list(churned):
            c = churned[cid]
            if t - c["churned_at"] <= 6 and rng.random() < 0.015:
                c["tenure"] = 0
                active[cid] = c
                del churned[cid]

        # ---- monthly lifecycle for active customers -----------------------
        for cid in list(active):
            c = active[cid]
            if c["tenure"] > 0:  # no churn decision in the signup month
                p = BASE_CHURN[c["plan"]] * CHANNELS[c["channel"]][2]
                p *= 1.0 / max(c["eng"], 0.25) ** 0.7  # low usage -> higher churn
                if c["tenure"] <= 3:
                    p *= 1.6  # early-life churn
                if c["country"] == "PE" and c["plan"] in ("EMP", "PYM") and PE_PRICE_CHANGE <= t <= PE_PRICE_CHANGE + 2:
                    p *= 2.4
                if rng.random() < min(p, 0.6):
                    c["churned_at"] = t
                    churned[cid] = c
                    del active[cid]
                    continue
                # plan changes
                idx = PLAN_IDS.index(c["plan"])
                up_p = 0.016 * min(c["eng"], 3.0)
                if idx < 3 and rng.random() < up_p:
                    c["plan"] = PLAN_IDS[idx + 1]
                elif idx > 0 and rng.random() < 0.006:
                    c["plan"] = PLAN_IDS[idx - 1]

            usage = BASE_USAGE[c["plan"]] * c["eng"] * rng.lognormal(0, 0.25)
            if c["tenure"] == 0:
                usage *= 0.5  # partial first month
            sub_rows.append((cid, month, c["plan"], price_for(c["plan"], c["country"], t)))
            usage_rows.append((cid, month, int(round(usage))))
            c["tenure"] += 1

    customers_df = pd.DataFrame(customers)
    plans_df = pd.DataFrame(
        [(p[0], p[1], p[2]) for p in PLANS], columns=["plan_id", "plan_name", "list_price_usd"]
    )
    subs_df = pd.DataFrame(sub_rows, columns=["customer_id", "month", "plan_id", "mrr_usd"])
    usage_df = pd.DataFrame(usage_rows, columns=["customer_id", "month", "invoices_issued"])
    spend_df = pd.DataFrame(spend_rows, columns=["month", "country", "channel", "spend_usd"])
    countries_df = pd.DataFrame(
        [
            ("MX", "México"),
            ("CO", "Colombia"),
            ("PE", "Perú"),
            ("CR", "Costa Rica"),
            ("DO", "República Dominicana"),
        ],
        columns=["country", "country_name"],
    )
    return dict(
        customers=customers_df,
        plans=plans_df,
        countries=countries_df,
        subscription_months=subs_df,
        product_usage=usage_df,
        marketing_spend=spend_df,
    )


def build(out: Path, seed: int = SEED) -> Path:
    tables = simulate(seed)
    if out.exists():
        out.unlink()
    con = duckdb.connect(str(out))
    for name, df in tables.items():
        for col in ("month", "signup_date"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col]).dt.date
        con.register("_df", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _df")
        con.unregister("_df")
    views_sql = (Path(__file__).resolve().parents[1] / "semantic" / "views.sql").read_text(encoding="utf-8")
    con.execute(views_sql)
    con.close()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "pulso.duckdb"))
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    path = build(Path(args.out), args.seed)
    con = duckdb.connect(str(path), read_only=True)
    for (t,) in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY 1"
    ).fetchall():
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:<24}{n:>10,}")
    print(f"\nDatabase written to {path}")
