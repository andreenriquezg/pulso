-- ============================================================================
-- Pulso semantic layer: governed metric views on top of the raw tables.
-- The agent is told to prefer these views over re-deriving metrics from raw
-- tables, so every answer uses the same definition of MRR, churn, NRR and CAC.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- mrr_movements: one row per customer per month, classifying how their MRR
-- changed vs the previous month (new / expansion / contraction / churn /
-- reactivation / retained). This is the backbone of the MRR bridge.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mrr_movements AS
WITH bounds AS (
    SELECT MAX(month) AS last_month FROM subscription_months
),
first_seen AS (
    SELECT customer_id, MIN(month) AS first_month
    FROM subscription_months
    GROUP BY 1
),
prev AS (
    SELECT customer_id,
           CAST(month + INTERVAL 1 MONTH AS DATE) AS month,
           plan_id  AS prev_plan_id,
           mrr_usd  AS prev_mrr_usd
    FROM subscription_months
),
joined AS (
    SELECT COALESCE(c.customer_id, p.customer_id) AS customer_id,
           COALESCE(c.month, p.month)             AS month,
           p.prev_plan_id,
           c.plan_id,
           COALESCE(p.prev_mrr_usd, 0)            AS prev_mrr_usd,
           COALESCE(c.mrr_usd, 0)                 AS mrr_usd
    FROM subscription_months c
    FULL OUTER JOIN prev p
      ON c.customer_id = p.customer_id AND c.month = p.month
)
SELECT j.customer_id,
       j.month,
       cu.country,
       cu.acquisition_channel,
       j.prev_plan_id,
       j.plan_id,
       j.prev_mrr_usd,
       j.mrr_usd,
       j.mrr_usd - j.prev_mrr_usd AS mrr_change_usd,
       CASE
           WHEN j.prev_mrr_usd = 0 AND j.mrr_usd > 0 AND j.month = f.first_month THEN 'new'
           WHEN j.prev_mrr_usd = 0 AND j.mrr_usd > 0                          THEN 'reactivation'
           WHEN j.prev_mrr_usd > 0 AND j.mrr_usd = 0                          THEN 'churn'
           WHEN j.mrr_usd > j.prev_mrr_usd                                    THEN 'expansion'
           WHEN j.mrr_usd < j.prev_mrr_usd                                    THEN 'contraction'
           ELSE 'retained'
       END AS movement
FROM joined j
JOIN first_seen f USING (customer_id)
JOIN customers cu USING (customer_id)
CROSS JOIN bounds b
WHERE j.month <= b.last_month;


-- ---------------------------------------------------------------------------
-- monthly_kpis: MRR bridge + customer counts + rates, grain = month x country.
-- Additive columns (*_usd, *_customers) can be summed across countries;
-- rates must be RECOMPUTED from the summed components, never averaged.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW monthly_kpis AS
WITH agg AS (
    SELECT month,
           country,
           SUM(prev_mrr_usd)                                                    AS mrr_start_usd,
           SUM(mrr_usd)                                                         AS mrr_end_usd,
           SUM(CASE WHEN movement = 'new'          THEN mrr_change_usd ELSE 0 END)  AS new_mrr_usd,
           SUM(CASE WHEN movement = 'expansion'    THEN mrr_change_usd ELSE 0 END)  AS expansion_mrr_usd,
           SUM(CASE WHEN movement = 'contraction'  THEN -mrr_change_usd ELSE 0 END) AS contraction_mrr_usd,
           SUM(CASE WHEN movement = 'churn'        THEN -mrr_change_usd ELSE 0 END) AS churned_mrr_usd,
           SUM(CASE WHEN movement = 'reactivation' THEN mrr_change_usd ELSE 0 END)  AS reactivation_mrr_usd,
           COUNT(*) FILTER (WHERE prev_mrr_usd > 0)                             AS customers_start,
           COUNT(*) FILTER (WHERE mrr_usd > 0)                                  AS customers_end,
           COUNT(*) FILTER (WHERE movement = 'new')                             AS new_customers,
           COUNT(*) FILTER (WHERE movement = 'churn')                           AS churned_customers,
           COUNT(*) FILTER (WHERE movement = 'reactivation')                    AS reactivated_customers
    FROM mrr_movements
    GROUP BY 1, 2
)
SELECT *,
       mrr_end_usd - mrr_start_usd                                              AS net_new_mrr_usd,
       12 * mrr_end_usd                                                         AS arr_end_usd,
       mrr_end_usd / NULLIF(customers_end, 0)                                   AS arpa_usd,
       churned_customers::DOUBLE / NULLIF(customers_start, 0)                   AS logo_churn_rate,
       (churned_mrr_usd + contraction_mrr_usd) / NULLIF(mrr_start_usd, 0)       AS gross_mrr_churn_rate,
       (churned_mrr_usd + contraction_mrr_usd - expansion_mrr_usd)
           / NULLIF(mrr_start_usd, 0)                                           AS net_mrr_churn_rate
FROM agg;


-- ---------------------------------------------------------------------------
-- nrr_12m: trailing-12-month Net and Gross Revenue Retention, month x country.
-- Base = customers paying 12 months earlier; NRR = their MRR today / their MRR
-- then. GRR caps each customer at their starting MRR (ignores expansion).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW nrr_12m AS
WITH months AS (
    SELECT DISTINCT month FROM subscription_months
),
pairs AS (
    SELECT m.month,
           cu.country,
           b.customer_id,
           b.mrr_usd                  AS mrr_12m_ago_usd,
           COALESCE(cur.mrr_usd, 0)   AS mrr_now_usd
    FROM months m
    JOIN subscription_months b
      ON b.month = CAST(m.month - INTERVAL 12 MONTH AS DATE)
    JOIN customers cu USING (customer_id)
    LEFT JOIN subscription_months cur
      ON cur.customer_id = b.customer_id AND cur.month = m.month
)
SELECT month,
       country,
       COUNT(*)                                             AS base_customers,
       SUM(mrr_12m_ago_usd)                                 AS base_mrr_usd,
       SUM(mrr_now_usd)                                     AS retained_mrr_usd,
       SUM(LEAST(mrr_now_usd, mrr_12m_ago_usd))             AS retained_mrr_capped_usd,
       COUNT(*) FILTER (WHERE mrr_now_usd > 0)              AS retained_customers,
       SUM(mrr_now_usd) / SUM(mrr_12m_ago_usd)              AS nrr,
       SUM(LEAST(mrr_now_usd, mrr_12m_ago_usd)) / SUM(mrr_12m_ago_usd) AS grr
FROM pairs
GROUP BY 1, 2;


-- ---------------------------------------------------------------------------
-- acquisition: new customers, spend and CAC, grain = month x country x channel.
-- 'organico' has no spend; blended CAC must include it in the denominator.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW acquisition AS
WITH nc AS (
    SELECT CAST(date_trunc('month', signup_date) AS DATE) AS month,
           country,
           acquisition_channel AS channel,
           COUNT(*) AS new_customers
    FROM customers
    GROUP BY 1, 2, 3
)
SELECT COALESCE(nc.month, s.month)         AS month,
       COALESCE(nc.country, s.country)     AS country,
       COALESCE(nc.channel, s.channel)     AS channel,
       COALESCE(nc.channel, s.channel) <> 'organico' AS is_paid_channel,
       COALESCE(nc.new_customers, 0)       AS new_customers,
       COALESCE(s.spend_usd, 0)            AS spend_usd,
       COALESCE(s.spend_usd, 0) / NULLIF(COALESCE(nc.new_customers, 0), 0) AS cac_usd
FROM nc
FULL OUTER JOIN marketing_spend s
  ON nc.month = s.month AND nc.country = s.country AND nc.channel = s.channel;


-- ---------------------------------------------------------------------------
-- customer_status: one row per customer with lifecycle facts, for segment and
-- cohort questions (who churns, which channel retains, usage vs churn...).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW customer_status AS
WITH bounds AS (
    SELECT MAX(month) AS last_month FROM subscription_months
),
life AS (
    SELECT customer_id,
           MIN(month)    AS first_month,
           MAX(month)    AS last_active_month,
           COUNT(*)      AS active_months,
           SUM(mrr_usd)  AS lifetime_revenue_usd,
           arg_min(plan_id, month) AS first_plan_id,
           arg_max(plan_id, month) AS last_plan_id,
           arg_max(mrr_usd, month) AS last_mrr_usd
    FROM subscription_months
    GROUP BY 1
),
usage AS (
    SELECT customer_id, AVG(invoices_issued) AS avg_invoices_last_3m
    FROM (
        SELECT customer_id, invoices_issued,
               ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY month DESC) AS rn
        FROM product_usage
    )
    WHERE rn <= 3
    GROUP BY 1
)
SELECT c.*,
       l.first_month,
       l.last_active_month,
       l.last_active_month = b.last_month AS is_active,
       l.first_plan_id,
       l.last_plan_id,
       l.last_mrr_usd,
       l.active_months,
       l.lifetime_revenue_usd,
       u.avg_invoices_last_3m
FROM customers c
JOIN life l USING (customer_id)
LEFT JOIN usage u USING (customer_id)
CROSS JOIN bounds b;
