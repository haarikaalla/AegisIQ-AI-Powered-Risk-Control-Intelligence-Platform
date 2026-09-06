"""
AegisIQ — Layer 3: Automated Control Testing Engine
======================================================
WHY THIS LAYER EXISTS (AWM M&T rationale):
  Manual control testing samples ~20-30 items per control per cycle. This engine
  re-performs every control's test procedure against 100% of the population,
  every cycle. It doesn't just report a pass rate — it produces a row-level
  exception record for every failure, with a reason code, so root-cause analysis
  (Layer 6 GenAI) has something concrete to reason about, and remediation
  (Layer 7) has a defined population to track to closure.

Outputs (data/processed/):
  - fact_control_test_results.csv   (one row per control per period: population,
                                      pass/fail/exception counts, pass_rate, RAG status)
  - fact_exceptions.csv             (one row per individual failing transaction)

Each `test_<CONTROL_ID>` function is deliberately simple, auditable pandas/logic —
this is "rules-as-code", not a black box. A control owner could read each function
and confirm it matches the test_procedure text in the control catalog.
"""
import os
import numpy as np
import pandas as pd

PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "processed")


def _load(name):
    return pd.read_csv(os.path.join(PROCESSED_DIR, f"{name}_clean.csv"))


def rag_status(pass_rate, threshold):
    if pd.isna(pass_rate):
        return "No Data"
    if pass_rate >= threshold:
        return "Green"
    elif pass_rate >= threshold - 0.03:
        return "Amber"
    else:
        return "Red"


# ----------------------------------------------------------------------------
# TC-01 Allocation Timeliness
# ----------------------------------------------------------------------------
def test_tc01(trades):
    df = trades.copy()
    testable = df[df["dq_flag_null_required_field"] == False].copy()
    testable["period"] = pd.to_datetime(testable["trade_date"]).dt.strftime("%Y-%m")
    testable["result"] = np.where(testable["allocation_lag_minutes"] <= 30, "Pass", "Fail")
    exceptions = testable[testable["result"] == "Fail"].copy()
    exceptions["control_id"] = "TC-01"
    exceptions["identified_date"] = pd.to_datetime(exceptions["trade_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "ALLOC_LATE"
    exceptions["detail"] = ("Allocated " + exceptions["allocation_lag_minutes"].round(0).astype(int).astype(str)
                             + " min after execution (limit 30 min)")
    exceptions["transaction_id"] = exceptions["trade_id"]
    summary = testable.groupby("period").agg(
        population=("trade_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    dq_by_period = df.groupby(pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m"))["dq_flag_null_required_field"].sum()
    summary["dq_exception_count"] = summary["period"].map(dq_by_period).fillna(0).astype(int)
    summary["pass_rate"] = (summary["pass_count"] / (summary["pass_count"] + summary["fail_count"])).round(4)
    summary["control_id"] = "TC-01"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# TC-02 Execution Price Reasonableness
# ----------------------------------------------------------------------------
def test_tc02(trades):
    df = trades.copy()
    testable = df[(df["dq_flag_null_required_field"] == False) & (df["dq_flag_out_of_range"] == False)].copy()
    testable["period"] = pd.to_datetime(testable["trade_date"]).dt.strftime("%Y-%m")
    testable["result"] = np.where(testable["price_dev_bps"].abs() <= 15, "Pass", "Fail")
    exceptions = testable[testable["result"] == "Fail"].copy()
    exceptions["control_id"] = "TC-02"
    exceptions["identified_date"] = pd.to_datetime(exceptions["trade_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "PRICE_OUTLIER"
    exceptions["detail"] = "Execution price deviated " + exceptions["price_dev_bps"].round(1).astype(str) + " bps from benchmark (limit 15bps)"
    exceptions["transaction_id"] = exceptions["trade_id"]
    summary = testable.groupby("period").agg(
        population=("trade_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "TC-02"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# TC-03 Pro-Rata Allocation Fairness (order-block level)
# ----------------------------------------------------------------------------
def test_tc03(trades):
    df = trades.copy()
    df["period"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m")
    block_totals = df.groupby("order_block_id")["quantity"].transform("sum")
    df["pro_rata_expected"] = df["quantity"] / block_totals
    df["alloc_deviation"] = (df["allocation_pct"] - df["pro_rata_expected"]).abs()
    testable = df[df["dq_flag_null_required_field"] == False].copy()
    testable["result"] = np.where(testable["alloc_deviation"] <= 0.02, "Pass", "Fail")
    exceptions = testable[testable["result"] == "Fail"].copy()
    exceptions["control_id"] = "TC-03"
    exceptions["identified_date"] = pd.to_datetime(exceptions["trade_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "ALLOC_UNFAIR"
    exceptions["detail"] = "Allocation deviated " + (exceptions["alloc_deviation"] * 100).round(2).astype(str) + " pts from pro-rata entitlement (limit 2pts)"
    exceptions["transaction_id"] = exceptions["trade_id"]
    summary = testable.groupby("period").agg(
        population=("trade_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "TC-03"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# FC-01 Fee Recalculation Match
# ----------------------------------------------------------------------------
def test_fc01(fees):
    df = fees.copy()
    testable = df[df["dq_flag_null_required_field"] == False].copy()
    testable["result"] = np.where(testable["fee_variance_pct"] <= 0.005, "Pass", "Fail")
    exceptions = testable[testable["result"] == "Fail"].copy()
    exceptions["control_id"] = "FC-01"
    exceptions["identified_date"] = pd.to_datetime(exceptions["period_end_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "FEE_MISMATCH"
    exceptions["detail"] = "Billed fee variance " + (exceptions["fee_variance_pct"] * 100).round(2).astype(str) + "% vs recalculated (limit 0.5%)"
    exceptions["transaction_id"] = exceptions["fee_id"]
    summary = testable.groupby("period").agg(
        population=("fee_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "FC-01"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# FC-02 Invoice Timeliness SLA
# ----------------------------------------------------------------------------
def test_fc02(fees):
    df = fees.copy()
    df["result"] = np.where(df["invoice_lag_bdays"] <= 10, "Pass", "Fail")
    exceptions = df[df["result"] == "Fail"].copy()
    exceptions["control_id"] = "FC-02"
    exceptions["identified_date"] = pd.to_datetime(exceptions["invoice_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "INVOICE_LATE"
    exceptions["detail"] = "Invoice issued " + exceptions["invoice_lag_bdays"].astype(int).astype(str) + " business days after period end (limit 10)"
    exceptions["transaction_id"] = exceptions["fee_id"]
    summary = df.groupby("period").agg(
        population=("fee_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "FC-02"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# KC-01 KYC Refresh SLA Compliance
# ----------------------------------------------------------------------------
def test_kc01(kyc):
    df = kyc.copy()
    df["period"] = pd.to_datetime(df["next_due_date"]).dt.strftime("%Y-%m")
    df["result"] = np.where((df["is_open"] == False) & (df["days_late"] == 0), "Pass", "Fail")
    exceptions = df[df["result"] == "Fail"].copy()
    exceptions["control_id"] = "KC-01"
    exceptions["identified_date"] = pd.to_datetime(exceptions["next_due_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = np.where(exceptions["is_open"], "KYC_OPEN_PAST_DUE", "KYC_LATE")
    days_late_str = exceptions["days_late"].fillna(0).astype(int).astype(str)
    exceptions["detail"] = np.where(
        exceptions["is_open"],
        "Refresh still open past due date " + exceptions["next_due_date"].astype(str),
        days_late_str + " days late vs. due date")
    exceptions["transaction_id"] = exceptions["case_id"]
    summary = df.groupby("period").agg(
        population=("case_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "KC-01"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# KC-02 Documentation Completeness
# ----------------------------------------------------------------------------
def test_kc02(kyc):
    df = kyc.copy()
    df["period"] = pd.to_datetime(df["next_due_date"]).dt.strftime("%Y-%m")
    df["result"] = np.where(df["documents_complete"] == True, "Pass", "Fail")
    exceptions = df[df["result"] == "Fail"].copy()
    exceptions["control_id"] = "KC-02"
    exceptions["identified_date"] = pd.to_datetime(exceptions["next_due_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "KYC_DOCS_INCOMPLETE"
    exceptions["detail"] = "Required KYC documentation incomplete at review closure"
    exceptions["transaction_id"] = exceptions["case_id"]
    summary = df.groupby("period").agg(
        population=("case_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "KC-02"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# NV-01 Price Variance Tolerance
# ----------------------------------------------------------------------------
def test_nv01(nav):
    df = nav.copy()
    df["period"] = pd.to_datetime(df["price_date"]).dt.strftime("%Y-%m")
    df["result"] = np.where(df["variance_bps"] <= 25, "Pass", "Fail")
    exceptions = df[df["result"] == "Fail"].copy()
    exceptions["control_id"] = "NV-01"
    exceptions["identified_date"] = pd.to_datetime(exceptions["price_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "PRICE_BREAK"
    exceptions["detail"] = "Price variance " + exceptions["variance_bps"].round(1).astype(str) + " bps vs independent source (limit 25bps)"
    exceptions["transaction_id"] = exceptions["price_id"]
    summary = df.groupby("period").agg(
        population=("price_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "NV-01"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# NV-02 Break Resolution SLA
# ----------------------------------------------------------------------------
def test_nv02(nav):
    df = nav[nav["has_break"] == True].copy()
    df["period"] = pd.to_datetime(df["price_date"]).dt.strftime("%Y-%m")
    df["result"] = np.where((df["resolution_bdays"].notna()) & (df["resolution_bdays"] <= 3), "Pass", "Fail")
    exceptions = df[df["result"] == "Fail"].copy()
    exceptions["control_id"] = "NV-02"
    exceptions["identified_date"] = pd.to_datetime(exceptions["break_identified_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "BREAK_SLA_MISSED"
    exceptions["detail"] = np.where(
        exceptions["resolution_bdays"].notna(),
        exceptions["resolution_bdays"].astype(int).astype(str) + " business days to resolve (limit 3)",
        "Break unresolved as of extract date")
    exceptions["transaction_id"] = exceptions["price_id"]
    summary = df.groupby("period").agg(
        population=("price_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "NV-02"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# CM-01 Dual Approval Threshold
# ----------------------------------------------------------------------------
def test_cm01(cash):
    df = cash.copy()
    df["period"] = pd.to_datetime(df["movement_date"]).dt.strftime("%Y-%m")
    needs_dual = df["amount"] > 250000
    df["result"] = np.where(~needs_dual | (needs_dual & df["has_dual_approval"]), "Pass", "Fail")
    scoped = df[needs_dual].copy()  # control only applies to movements requiring dual approval
    exceptions = scoped[scoped["result"] == "Fail"].copy()
    exceptions["control_id"] = "CM-01"
    exceptions["identified_date"] = pd.to_datetime(exceptions["movement_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "MISSING_DUAL_APPROVAL"
    exceptions["detail"] = "Movement of $" + exceptions["amount"].round(0).astype(int).astype(str) + " lacks second approver (threshold $250,000)"
    exceptions["transaction_id"] = exceptions["movement_id"]
    summary = scoped.groupby("period").agg(
        population=("movement_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "CM-01"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# CM-02 Custodian Reconciliation Match
# ----------------------------------------------------------------------------
def test_cm02(cash):
    df = cash.copy()
    df["period"] = pd.to_datetime(df["movement_date"]).dt.strftime("%Y-%m")
    df["result"] = np.where(df["custodian_match_flag"] == True, "Pass", "Fail")
    exceptions = df[df["result"] == "Fail"].copy()
    exceptions["control_id"] = "CM-02"
    exceptions["identified_date"] = pd.to_datetime(exceptions["movement_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "CUSTODIAN_BREAK"
    exceptions["detail"] = "Movement does not match custodian statement"
    exceptions["transaction_id"] = exceptions["movement_id"]
    summary = df.groupby("period").agg(
        population=("movement_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "CM-02"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# SM-01 Guideline Band Compliance
# ----------------------------------------------------------------------------
def test_sm01(holdings):
    df = holdings.copy()
    df["period"] = pd.to_datetime(df["as_of_date"]).dt.strftime("%Y-%m")
    df["result"] = np.where(df["within_band"] == True, "Pass", "Fail")
    exceptions = df[df["result"] == "Fail"].copy()
    exceptions["control_id"] = "SM-01"
    exceptions["identified_date"] = pd.to_datetime(exceptions["as_of_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "GUIDELINE_BREACH"
    direction = np.where(exceptions["weight_pct"] > exceptions["guideline_max_pct"], "above max", "below min")
    exceptions["detail"] = exceptions["asset_class"] + " weight " + exceptions["weight_pct"].astype(str) + "% is " + direction + " for " + exceptions["mandate_type"]
    exceptions["transaction_id"] = exceptions["holding_id"]
    summary = df.groupby("period").agg(
        population=("holding_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "SM-01"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


# ----------------------------------------------------------------------------
# SM-02 Periodic Suitability Review
# ----------------------------------------------------------------------------
def test_sm02(suitability):
    df = suitability.copy()
    df["period"] = pd.to_datetime(df["review_due_date"]).dt.strftime("%Y-%m")
    df["result"] = np.where(df["status"] == "OnTime", "Pass", "Fail")
    exceptions = df[df["result"] == "Fail"].copy()
    exceptions["control_id"] = "SM-02"
    exceptions["identified_date"] = pd.to_datetime(exceptions["review_due_date"]).dt.strftime("%Y-%m-%d")
    exceptions["reason_code"] = "SUITABILITY_REVIEW_LATE"
    exceptions["detail"] = "Suitability review completed late vs. annual cycle requirement"
    exceptions["transaction_id"] = exceptions["review_id"]
    summary = df.groupby("period").agg(
        population=("review_id", "count"),
        pass_count=("result", lambda s: (s == "Pass").sum()),
        fail_count=("result", lambda s: (s == "Fail").sum()),
    ).reset_index()
    summary["dq_exception_count"] = 0
    summary["pass_rate"] = (summary["pass_count"] / summary["population"]).round(4)
    summary["control_id"] = "SM-02"
    return summary, exceptions[["control_id", "transaction_id", "period", "reason_code", "detail", "identified_date"]]


def main():
    trades = _load("trades")
    fees = _load("fee_calculations")
    kyc = _load("kyc_cases")
    nav = _load("nav_prices")
    cash = _load("cash_movements")
    holdings = _load("portfolio_holdings")
    suitability = _load("suitability_reviews")
    catalog = _load("control_catalog").set_index("control_id")

    tests = [
        test_tc01(trades), test_tc02(trades), test_tc03(trades),
        test_fc01(fees), test_fc02(fees),
        test_kc01(kyc), test_kc02(kyc),
        test_nv01(nav), test_nv02(nav),
        test_cm01(cash), test_cm02(cash),
        test_sm01(holdings), test_sm02(suitability),
    ]

    summaries = pd.concat([t[0] for t in tests], ignore_index=True)
    exceptions = pd.concat([t[1] for t in tests], ignore_index=True)

    summaries["kri_threshold"] = summaries["control_id"].map(catalog["kri_threshold_pass_rate"])
    summaries["rag_status"] = summaries.apply(lambda r: rag_status(r["pass_rate"], r["kri_threshold"]), axis=1)
    summaries["control_name"] = summaries["control_id"].map(catalog["control_name"])
    summaries["process_name"] = summaries["control_id"].map(catalog["process_name"])
    summaries = summaries.sort_values(["control_id", "period"]).reset_index(drop=True)

    exceptions.insert(0, "exception_id", range(9000001, 9000001 + len(exceptions)))
    exceptions["status"] = "Open"  # closure status is assigned by the remediation workflow (Layer 7)

    summaries.to_csv(os.path.join(PROCESSED_DIR, "fact_control_test_results.csv"), index=False)
    exceptions.to_csv(os.path.join(PROCESSED_DIR, "fact_exceptions.csv"), index=False)

    print("=== AegisIQ Control Testing Engine — Summary ===")
    period_coverage = summaries.groupby("period")["control_id"].nunique()
    well_covered_periods = period_coverage[period_coverage >= 8].index
    latest_period = max(well_covered_periods) if len(well_covered_periods) else summaries["period"].max()
    latest = summaries[summaries["period"] == latest_period]
    print(f"\nLatest tested period: {latest_period}")
    print(latest[["control_id", "control_name", "population", "pass_rate", "kri_threshold", "rag_status"]].to_string(index=False))
    print(f"\nTotal exceptions generated across all controls/periods: {len(exceptions):,}")
    print(f"Total control-test rows (control x period): {len(summaries):,}")
    print(f"\nRAG distribution (latest period): \n{latest['rag_status'].value_counts().to_string()}")


if __name__ == "__main__":
    main()
