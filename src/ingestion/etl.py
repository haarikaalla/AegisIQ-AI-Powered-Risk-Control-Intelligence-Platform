"""
AegisIQ — Layer 1: Ingestion & Cleaning ETL
=============================================
WHY THIS LAYER EXISTS (AWM M&T rationale):
  A control test result is only as trustworthy as the data it ran against. Before any
  pass/fail determination, M&T must be able to show: what data quality issues existed
  in the source extract, how they were handled, and that handling didn't silently drop
  or mask real exceptions. This module produces (a) clean tables for downstream testing
  and (b) a Data Quality Log that is itself an artifact reviewable by a control owner
  or auditor.

Design choices:
  - Nulls in fields required for a control test are NOT silently dropped — they are
    flagged as "Untestable / Data Quality Exception" so the control testing engine can
    report them as a distinct exception type (a null execution_price is not a "pass").
  - Duplicates are removed on natural key, keeping the first occurrence, with a count
    logged.
  - Out-of-range values (negative quantity, zero price) are flagged, not corrected —
    correcting synthetic-looking bad data would hide real issues from testers.
  - The Jan-2026 timestamp format drift is auto-detected and normalized (a common real
    ETL task: a source system that briefly changed its date format).
"""
import os
import pandas as pd
import numpy as np
import sqlite3

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "raw")
PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "processed")
DB_PATH = os.path.join(PROCESSED_DIR, "aegisiq.db")

dq_log = []  # accumulates data-quality log entries across all tables


def log_dq(table, issue, count, action):
    dq_log.append(dict(table=table, issue=issue, affected_rows=int(count), action_taken=action))


def normalize_timestamp_column(series, colname, table):
    """Detect and normalize mixed '/' vs '-' timestamp formats (Jan-2026 drift scenario)."""
    slash_mask = series.astype(str).str.contains("/")
    n_slash = int(slash_mask.sum())
    if n_slash:
        series = series.astype(str)
        series.loc[slash_mask] = series.loc[slash_mask].str.replace("/", "-", regex=False)
        log_dq(table, f"Timestamp format drift in '{colname}' ('/' vs '-')", n_slash,
               "Normalized to ISO 'YYYY-MM-DD HH:MM:SS' format")
    return pd.to_datetime(series, errors="coerce")


def dedupe(df, key, table):
    n_before = len(df)
    df2 = df.drop_duplicates(subset=[key], keep="first")
    n_removed = n_before - len(df2)
    if n_removed:
        log_dq(table, f"Duplicate rows on key '{key}'", n_removed, "Removed, kept first occurrence")
    return df2


def flag_nulls(df, cols, table):
    df = df.copy()
    df["dq_flag_null_required_field"] = False
    for c in cols:
        n_null = int(df[c].isna().sum())
        if n_null:
            df.loc[df[c].isna(), "dq_flag_null_required_field"] = True
            log_dq(table, f"Missing required field '{c}'", n_null,
                   "Flagged as Data-Quality Exception (not testable, not silently dropped)")
    return df


def flag_out_of_range(df, table):
    df = df.copy()
    df["dq_flag_out_of_range"] = False
    if "quantity" in df.columns:
        mask = df["quantity"] < 0
        n = int(mask.sum())
        if n:
            df.loc[mask, "dq_flag_out_of_range"] = True
            log_dq(table, "Negative quantity", n, "Flagged as Data-Quality Exception")
    if "execution_price" in df.columns:
        mask = df["execution_price"] <= 0
        n = int(mask.sum())
        if n:
            df.loc[mask, "dq_flag_out_of_range"] = True
            log_dq(table, "Zero/negative execution price", n, "Flagged as Data-Quality Exception")
    return df


def clean_trades():
    df = pd.read_csv(os.path.join(RAW_DIR, "trades.csv"))
    df = dedupe(df, "trade_id", "trades")
    df["execution_timestamp"] = normalize_timestamp_column(df["execution_timestamp"], "execution_timestamp", "trades")
    df["allocation_timestamp"] = pd.to_datetime(df["allocation_timestamp"], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = flag_nulls(df, ["quantity", "execution_price"], "trades")
    df = flag_out_of_range(df, "trades")
    # derived field used by control testing: allocation lag in minutes
    df["allocation_lag_minutes"] = (df["allocation_timestamp"] - df["execution_timestamp"]).dt.total_seconds() / 60
    df["price_dev_bps"] = ((df["execution_price"] - df["benchmark_price"]) / df["benchmark_price"]) * 10000
    return df


def clean_fee_calculations():
    df = pd.read_csv(os.path.join(RAW_DIR, "fee_calculations.csv"))
    df = dedupe(df, "fee_id", "fee_calculations")
    df["period_end_date"] = pd.to_datetime(df["period_end_date"])
    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    df = flag_nulls(df, ["billed_fee"], "fee_calculations")
    df["fee_variance_pct"] = np.where(
        df["calculated_fee"] > 0,
        (df["billed_fee"] - df["calculated_fee"]).abs() / df["calculated_fee"],
        np.nan)
    df["invoice_lag_bdays"] = np.busday_count(
        df["period_end_date"].values.astype("datetime64[D]"),
        df["invoice_date"].values.astype("datetime64[D]"))
    return df


def clean_kyc_cases():
    df = pd.read_csv(os.path.join(RAW_DIR, "kyc_cases.csv"))
    df = dedupe(df, "case_id", "kyc_cases")
    df["next_due_date"] = pd.to_datetime(df["next_due_date"])
    df["review_completed_date"] = pd.to_datetime(df["review_completed_date"], errors="coerce")
    df["is_open"] = df["review_completed_date"].isna()
    df["days_late"] = (df["review_completed_date"] - df["next_due_date"]).dt.days
    df["days_late"] = df["days_late"].clip(lower=0)
    return df


def clean_nav_prices():
    df = pd.read_csv(os.path.join(RAW_DIR, "nav_prices.csv"))
    df = dedupe(df, "price_id", "nav_prices")
    df["price_date"] = pd.to_datetime(df["price_date"])
    df["break_identified_date"] = pd.to_datetime(df["break_identified_date"], errors="coerce")
    df["break_resolved_date"] = pd.to_datetime(df["break_resolved_date"], errors="coerce")
    df["variance_bps"] = ((df["price"] - df["independent_source_price"]).abs() / df["independent_source_price"]) * 10000
    df["has_break"] = df["break_identified_date"].notna()
    placeholder = pd.Timestamp("1970-01-01")
    start_dates = df["break_identified_date"].fillna(placeholder).values.astype("datetime64[D]")
    end_dates = df["break_resolved_date"].fillna(placeholder).values.astype("datetime64[D]")
    bdays_between = np.busday_count(start_dates, end_dates)
    df["resolution_bdays"] = np.where(
        df["has_break"] & df["break_resolved_date"].notna(),
        bdays_between,
        np.nan)
    return df


def clean_cash_movements():
    df = pd.read_csv(os.path.join(RAW_DIR, "cash_movements.csv"))
    df = dedupe(df, "movement_id", "cash_movements")
    df["movement_date"] = pd.to_datetime(df["movement_date"])
    df["approver_2"] = df["approver_2"].fillna("")
    df["has_dual_approval"] = df["approver_2"].astype(str).str.strip() != ""
    return df


def clean_portfolio_holdings():
    df = pd.read_csv(os.path.join(RAW_DIR, "portfolio_holdings.csv"))
    df = dedupe(df, "holding_id", "portfolio_holdings")
    df["as_of_date"] = pd.to_datetime(df["as_of_date"])
    df["within_band"] = (df["weight_pct"] >= df["guideline_min_pct"]) & (df["weight_pct"] <= df["guideline_max_pct"])
    return df


def clean_suitability_reviews():
    df = pd.read_csv(os.path.join(RAW_DIR, "suitability_reviews.csv"))
    df = dedupe(df, "review_id", "suitability_reviews")
    df["review_due_date"] = pd.to_datetime(df["review_due_date"])
    df["review_completed_date"] = pd.to_datetime(df["review_completed_date"])
    return df


def clean_control_catalog():
    return pd.read_csv(os.path.join(RAW_DIR, "control_catalog.csv"))


def main():
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    tables = {
        "trades": clean_trades(),
        "fee_calculations": clean_fee_calculations(),
        "kyc_cases": clean_kyc_cases(),
        "nav_prices": clean_nav_prices(),
        "cash_movements": clean_cash_movements(),
        "portfolio_holdings": clean_portfolio_holdings(),
        "suitability_reviews": clean_suitability_reviews(),
        "control_catalog": clean_control_catalog(),
    }

    conn = sqlite3.connect(DB_PATH)
    for name, df in tables.items():
        df.to_csv(os.path.join(PROCESSED_DIR, f"{name}_clean.csv"), index=False)
        df.to_sql(name, conn, if_exists="replace", index=False)
    dq_df = pd.DataFrame(dq_log)
    dq_df.to_csv(os.path.join(PROCESSED_DIR, "data_quality_log.csv"), index=False)
    dq_df.to_sql("data_quality_log", conn, if_exists="replace", index=False)
    conn.close()

    print("=== AegisIQ Ingestion & Cleaning: Data Quality Log ===")
    print(dq_df.to_string(index=False))
    print(f"\nClean tables + DQ log written to {PROCESSED_DIR}")
    print(f"SQLite DB written to {DB_PATH}")
    total_rows = sum(len(df) for df in tables.values())
    print(f"\nTotal rows processed across {len(tables)} tables: {total_rows:,}")


if __name__ == "__main__":
    main()
