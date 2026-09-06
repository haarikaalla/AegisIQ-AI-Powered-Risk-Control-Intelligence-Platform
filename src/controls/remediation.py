"""
AegisIQ — Layer 7: Remediation Workflow & Tracking
=====================================================
WHY THIS LAYER EXISTS (AWM M&T rationale):
  A finding without an owned, dated remediation action tracked to closure is not
  a closed loop — this is precisely what internal audit and regulators check for.
  This module takes every exception from Layer 3 and:
    1. Assigns a remediation action: owner (from the control catalog), target
       closure date (based on a severity-driven SLA), and priority.
    2. Simulates realistic closure behavior: older exceptions are mostly closed,
       the most recent ~45 days of exceptions are mostly still open (a genuine
       backlog "tail" — not everything gets fixed instantly), and a small share
       of older items breach their remediation SLA and remain open (chronic
       issues), which is exactly the kind of item a risk committee needs
       flagged.
  This produces the "as of today" open-exception population used by risk scoring
  (Layer 4) and GenAI reporting (Layer 6), instead of treating every historical
  exception as still open (which would overstate residual risk).
"""
import os
import numpy as np
import pandas as pd

PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "processed")
AS_OF_DATE = pd.Timestamp("2026-09-05")  # data extract "as of" date for this cycle

SEVERITY_SLA_DAYS = {
    # remediation SLA in calendar days, by reason code severity tier
    "ALLOC_LATE": 20, "PRICE_OUTLIER": 15, "ALLOC_UNFAIR": 20,
    "FEE_MISMATCH": 15, "INVOICE_LATE": 20,
    "KYC_LATE": 30, "KYC_OPEN_PAST_DUE": 15, "KYC_DOCS_INCOMPLETE": 20,
    "PRICE_BREAK": 10, "BREAK_SLA_MISSED": 10,
    "MISSING_DUAL_APPROVAL": 10, "CUSTODIAN_BREAK": 15,
    "GUIDELINE_BREACH": 20, "SUITABILITY_REVIEW_LATE": 25,
}

OWNER_MAP = {
    "TC-01": "J. Farrow, Trading Ops", "TC-02": "J. Farrow, Trading Ops", "TC-03": "J. Farrow, Trading Ops",
    "FC-01": "M. Cho, Billing Ops", "FC-02": "M. Cho, Billing Ops",
    "KC-01": "R. Alavi, Compliance Ops", "KC-02": "R. Alavi, Compliance Ops",
    "NV-01": "S. Okonjo, Valuation Control", "NV-02": "S. Okonjo, Valuation Control",
    "CM-01": "D. Weiss, Treasury Ops", "CM-02": "D. Weiss, Treasury Ops",
    "SM-01": "A. Petrova, Suitability Oversight", "SM-02": "A. Petrova, Suitability Oversight",
}


def main():
    rng = np.random.default_rng(7)
    exc = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_exceptions.csv"))
    exc["identified_date"] = pd.to_datetime(exc["identified_date"])
    exc["age_days"] = (AS_OF_DATE - exc["identified_date"]).dt.days
    exc["sla_days"] = exc["reason_code"].map(SEVERITY_SLA_DAYS).fillna(15)
    exc["owner"] = exc["control_id"].map(OWNER_MAP)
    exc["target_closure_date"] = exc["identified_date"] + pd.to_timedelta(exc["sla_days"], unit="D")

    # --- simulate closure behavior ---
    # p(closed) rises with age; very recent items (< sla_days) are mostly still "in progress"
    # a small chronic-breach tail (~6%) stays open well past target even when old
    p_closed = np.clip(exc["age_days"] / (exc["sla_days"] * 1.8), 0, 0.97)
    chronic = rng.random(len(exc)) < 0.06
    closed_mask = (rng.random(len(exc)) < p_closed) & (~chronic)

    exc["status"] = np.where(closed_mask, "Closed", "Open")
    # among closed items, actual closure date is somewhere between identified_date and now,
    # weighted toward the target SLA date +/- some variance
    closure_offset_days = np.clip(
        rng.normal(loc=exc["sla_days"], scale=exc["sla_days"] * 0.4), 1, exc["age_days"].clip(lower=1))
    exc["actual_closure_date"] = np.where(
        closed_mask,
        (exc["identified_date"] + pd.to_timedelta(closure_offset_days, unit="D")).dt.strftime("%Y-%m-%d"),
        "")
    exc["sla_breached"] = np.where(
        exc["status"] == "Closed",
        pd.to_datetime(exc["actual_closure_date"]) > exc["target_closure_date"],
        exc["age_days"] > exc["sla_days"])

    exc["remediation_priority"] = np.select(
        [exc["sla_days"] <= 10, exc["sla_days"] <= 20],
        ["High", "Medium"], default="Low"
    )

    exc["identified_date"] = exc["identified_date"].dt.strftime("%Y-%m-%d")
    exc["target_closure_date"] = exc["target_closure_date"].dt.strftime("%Y-%m-%d")

    exc.to_csv(os.path.join(PROCESSED_DIR, "fact_exceptions.csv"), index=False)

    # remediation_actions.csv: one row per OPEN exception = an active tracked action
    open_actions = exc[exc["status"] == "Open"].copy()
    open_actions["action_id"] = ["REM-" + str(9000000 + i) for i in range(len(open_actions))]
    open_actions["days_to_target"] = (pd.to_datetime(open_actions["target_closure_date"]) - AS_OF_DATE).dt.days
    open_actions["status_vs_sla"] = np.where(open_actions["days_to_target"] < 0, "Overdue", "On Track")
    remediation_cols = ["action_id", "exception_id", "control_id", "owner", "reason_code", "detail",
                         "identified_date", "target_closure_date", "days_to_target", "status_vs_sla",
                         "remediation_priority"]
    open_actions[remediation_cols].to_csv(os.path.join(PROCESSED_DIR, "remediation_actions.csv"), index=False)

    print("=== AegisIQ Remediation Workflow ===")
    print(f"As-of date: {AS_OF_DATE.date()}")
    print(f"\nTotal exceptions: {len(exc):,}")
    print(exc["status"].value_counts().to_string())
    print(f"\nOpen remediation actions: {len(open_actions):,}")
    print(f"  Overdue vs. SLA: {(open_actions['status_vs_sla'] == 'Overdue').sum():,}")
    print("\nOpen exceptions by control:")
    print(exc[exc["status"] == "Open"]["control_id"].value_counts().to_string())
    print("\nSLA breach rate among CLOSED items (chronic-lag indicator):")
    closed = exc[exc["status"] == "Closed"]
    if len(closed):
        print(f"  {closed['sla_breached'].mean():.1%}")


if __name__ == "__main__":
    main()
