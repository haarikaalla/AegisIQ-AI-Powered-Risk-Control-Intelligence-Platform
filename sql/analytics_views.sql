-- ============================================================================
-- AegisIQ — Layer 2: SQL Analytics & KRI/KCI/KPI Engine
-- ============================================================================
-- WHY SQL: any control test result that ends up in a committee pack must be
-- reproducible by someone other than the analyst who ran it. These views are
-- the auditable, re-runnable definition of every KRI/KCI/KPI in this platform.
-- Run against data/processed/aegisiq.db (SQLite), populated by etl.py.
-- Populated tables assumed present: trades, fee_calculations, kyc_cases,
-- nav_prices, cash_movements, portfolio_holdings, suitability_reviews,
-- control_catalog. fact_control_test_results is written by control_testing.py
-- (Layer 3) and this file also defines views over it.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 2.1 KRI: TC-01 Allocation Timeliness pass rate by month
-- (raw re-performance in SQL, mirrors what control_testing.py computes
--  independently in Python — shown here as the auditable SQL definition)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_tc01_monthly;
CREATE VIEW vw_tc01_monthly AS
SELECT
    strftime('%Y-%m', trade_date) AS period,
    COUNT(*) AS population,
    SUM(CASE WHEN dq_flag_null_required_field = 0
              AND allocation_lag_minutes <= 30 THEN 1 ELSE 0 END) AS pass_count,
    SUM(CASE WHEN dq_flag_null_required_field = 0
              AND allocation_lag_minutes > 30 THEN 1 ELSE 0 END) AS fail_count,
    SUM(CASE WHEN dq_flag_null_required_field = 1 THEN 1 ELSE 0 END) AS dq_exception_count,
    ROUND(1.0 * SUM(CASE WHEN dq_flag_null_required_field = 0
              AND allocation_lag_minutes <= 30 THEN 1 ELSE 0 END)
        / NULLIF(SUM(CASE WHEN dq_flag_null_required_field = 0 THEN 1 ELSE 0 END), 0), 4) AS pass_rate
FROM trades
GROUP BY 1
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 2.2 KRI: TC-02 Execution Price Reasonableness pass rate by month
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_tc02_monthly;
CREATE VIEW vw_tc02_monthly AS
SELECT
    strftime('%Y-%m', trade_date) AS period,
    COUNT(*) AS population,
    SUM(CASE WHEN dq_flag_null_required_field = 0 AND dq_flag_out_of_range = 0
              AND ABS(price_dev_bps) <= 15 THEN 1 ELSE 0 END) AS pass_count,
    SUM(CASE WHEN dq_flag_null_required_field = 0 AND dq_flag_out_of_range = 0
              AND ABS(price_dev_bps) > 15 THEN 1 ELSE 0 END) AS fail_count,
    ROUND(1.0 * SUM(CASE WHEN dq_flag_null_required_field = 0 AND dq_flag_out_of_range = 0
              AND ABS(price_dev_bps) <= 15 THEN 1 ELSE 0 END)
        / NULLIF(SUM(CASE WHEN dq_flag_null_required_field = 0 AND dq_flag_out_of_range = 0 THEN 1 ELSE 0 END), 0), 4) AS pass_rate
FROM trades
GROUP BY 1
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 2.3 KRI: FC-01 Fee Recalculation Match pass rate by period
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_fc01_monthly;
CREATE VIEW vw_fc01_monthly AS
SELECT
    period,
    COUNT(*) AS population,
    SUM(CASE WHEN dq_flag_null_required_field = 0 AND fee_variance_pct <= 0.005 THEN 1 ELSE 0 END) AS pass_count,
    SUM(CASE WHEN dq_flag_null_required_field = 0 AND fee_variance_pct > 0.005 THEN 1 ELSE 0 END) AS fail_count,
    ROUND(1.0 * SUM(CASE WHEN dq_flag_null_required_field = 0 AND fee_variance_pct <= 0.005 THEN 1 ELSE 0 END)
        / NULLIF(SUM(CASE WHEN dq_flag_null_required_field = 0 THEN 1 ELSE 0 END), 0), 4) AS pass_rate
FROM fee_calculations
GROUP BY 1
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 2.4 KRI: KC-01 KYC Refresh SLA compliance by month due
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kc01_monthly;
CREATE VIEW vw_kc01_monthly AS
SELECT
    strftime('%Y-%m', next_due_date) AS period,
    COUNT(*) AS population,
    SUM(CASE WHEN is_open = 0 AND days_late = 0 THEN 1 ELSE 0 END) AS pass_count,
    SUM(CASE WHEN is_open = 1 OR days_late > 0 THEN 1 ELSE 0 END) AS fail_count,
    ROUND(1.0 * SUM(CASE WHEN is_open = 0 AND days_late = 0 THEN 1 ELSE 0 END) / COUNT(*), 4) AS pass_rate,
    ROUND(AVG(CASE WHEN days_late > 0 THEN days_late END), 1) AS avg_days_late_when_late
FROM kyc_cases
GROUP BY 1
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 2.5 KRI: NV-02 Pricing Break Resolution SLA by month
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_nv02_monthly;
CREATE VIEW vw_nv02_monthly AS
SELECT
    strftime('%Y-%m', price_date) AS period,
    SUM(has_break) AS breaks_identified,
    SUM(CASE WHEN has_break = 1 AND resolution_bdays <= 3 THEN 1 ELSE 0 END) AS resolved_within_sla,
    SUM(CASE WHEN has_break = 1 AND (resolution_bdays > 3 OR resolution_bdays IS NULL) THEN 1 ELSE 0 END) AS breached_sla,
    ROUND(1.0 * SUM(CASE WHEN has_break = 1 AND resolution_bdays <= 3 THEN 1 ELSE 0 END)
        / NULLIF(SUM(has_break), 0), 4) AS pass_rate
FROM nav_prices
GROUP BY 1
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 2.6 Exception backlog / aging view (cross-control), for MI "open items" page
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kyc_open_backlog;
CREATE VIEW vw_kyc_open_backlog AS
SELECT
    case_id, client_id, client_risk_rating, next_due_date,
    CAST(julianday('now') - julianday(next_due_date) AS INTEGER) AS days_overdue
FROM kyc_cases
WHERE is_open = 1
ORDER BY days_overdue DESC;

-- ---------------------------------------------------------------------------
-- 2.7 Cash movement dual-approval exceptions (CM-01) — transaction-level detail
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_cm01_exceptions;
CREATE VIEW vw_cm01_exceptions AS
SELECT movement_id, account_id, movement_type, amount, movement_date, approver_1, approver_2
FROM cash_movements
WHERE amount > 250000 AND has_dual_approval = 0
ORDER BY amount DESC;

-- ---------------------------------------------------------------------------
-- 2.8 Suitability guideline breaches (SM-01) — transaction-level detail
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_sm01_breaches;
CREATE VIEW vw_sm01_breaches AS
SELECT portfolio_id, client_id, mandate_type, asset_class, weight_pct,
       guideline_min_pct, guideline_max_pct,
       ROUND(CASE WHEN weight_pct > guideline_max_pct THEN weight_pct - guideline_max_pct
                  ELSE guideline_min_pct - weight_pct END, 2) AS breach_magnitude_pct
FROM portfolio_holdings
WHERE within_band = 0
ORDER BY breach_magnitude_pct DESC;
