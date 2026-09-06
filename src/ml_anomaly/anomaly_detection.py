"""
AegisIQ — Layer 5: ML-Based Anomaly Detection on Control Metrics
===================================================================
WHY THIS LAYER EXISTS (AWM M&T rationale):
  Static KRI thresholds (e.g., "pass rate must be >= 97%") only fire once a control
  has ALREADY breached. A control that quietly drifts from 99% to 92% over four
  months crosses no threshold until month four — by which point the underlying
  process issue (e.g., a trading desk understaffed on allocation checks) has been
  live for months. This layer applies two complementary, genuinely useful
  techniques to each control's monthly pass-rate time series:

  1. ROLLING Z-SCORE (statistical, fully explainable): flags a month where the
     pass rate is an unusual number of standard deviations from its own trailing
     window — cheap, transparent, good at catching sudden step-changes (e.g. the
     Feb-2026 FC-01 fee mismatch spike).
  2. ISOLATION FOREST (ML, multivariate): scores each (control, month) observation
     using pass_rate, month-over-month delta, and open-exception rate together —
     better at catching gradual multi-signal drift (e.g. TC-01's slow allocation-
     timeliness decay) that a single-variable z-score can under-react to early on.

  We report BOTH methods per observation and take the union as "flagged" — a
  standard practice for anomaly ensembles, since detectors have different blind
  spots and the goal here is analyst *triage assistance*, not autonomous action.

  DELIBERATELY NOT USED: deep learning / LSTM-based sequence models. With ~12
  monthly points per control, there is nowhere near enough data to train or
  validate a neural sequence model responsibly — using one here would be
  complexity for its own sake, contrary to this project's stated priority of
  risk intelligence over unnecessary AI sophistication.
"""
import os
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "processed")


def rolling_zscore(series, window=4, min_periods=3):
    roll_mean = series.shift(1).rolling(window=window, min_periods=min_periods).mean()
    roll_std = series.shift(1).rolling(window=window, min_periods=min_periods).std()
    z = (series - roll_mean) / roll_std.replace(0, np.nan)
    return z


def main():
    results = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_control_test_results.csv"))
    results = results.sort_values(["control_id", "period"]).reset_index(drop=True)

    # feature engineering per control
    results["pass_rate_pct"] = results["pass_rate"] * 100
    results["mom_delta"] = results.groupby("control_id")["pass_rate"].diff()
    results["exception_rate"] = 1 - results["pass_rate"]
    results["z_score"] = results.groupby("control_id")["pass_rate"].transform(
        lambda s: rolling_zscore(s))

    # Isolation Forest — one model per control (small-N appropriate; contamination
    # set low since most months should be "normal" by construction of this platform)
    if_results = []
    for control_id, grp in results.groupby("control_id"):
        grp = grp.copy()
        feats = grp[["pass_rate", "mom_delta", "exception_rate"]].fillna(0).values
        if len(grp) >= 6:
            model = IsolationForest(n_estimators=200, contamination=0.15, random_state=42)
            grp["if_anomaly_score"] = -model.fit_predict(feats)  # 1 = anomaly, -1->0 normal mapped to 0/1... fix below
            raw_scores = -model.score_samples(feats)  # higher = more anomalous
            grp["if_anomaly_score"] = raw_scores
            preds = model.predict(feats)  # -1 anomaly, 1 normal
            grp["if_flagged"] = preds == -1
        else:
            grp["if_anomaly_score"] = np.nan
            grp["if_flagged"] = False
        if_results.append(grp)
    results = pd.concat(if_results, ignore_index=True)

    results["z_flagged"] = results["z_score"].abs() >= 2.0
    results["anomaly_flagged"] = results["z_flagged"] | results["if_flagged"]

    def classify(row):
        if not row["anomaly_flagged"]:
            return ""
        if row["z_flagged"] and row["mom_delta"] is not None and pd.notna(row["mom_delta"]) and row["mom_delta"] <= -0.03:
            return "Step-change deterioration"
        elif row["if_flagged"] and not row["z_flagged"]:
            return "Gradual multi-signal drift"
        elif row["z_flagged"]:
            return "Statistical outlier (single period)"
        else:
            return "ML-flagged pattern anomaly"

    results["anomaly_type"] = results.apply(classify, axis=1)

    # lead time: for controls with an eventual Red RAG breach, how many periods did
    # the FIRST anomaly flag precede the first Red rating?
    lead_time_rows = []
    for control_id, grp in results.groupby("control_id"):
        grp = grp.sort_values("period").reset_index(drop=True)
        first_red_idx = grp.index[grp["rag_status"] == "Red"]
        first_anom_idx = grp.index[grp["anomaly_flagged"]]
        if len(first_red_idx) and len(first_anom_idx) and first_anom_idx.min() < first_red_idx.min():
            lead = first_red_idx.min() - first_anom_idx.min()
            lead_time_rows.append(dict(control_id=control_id, periods_of_early_warning=int(lead)))
    lead_time_df = pd.DataFrame(lead_time_rows)

    out_cols = ["control_id", "control_name", "period", "population", "pass_rate", "rag_status",
                "mom_delta", "z_score", "z_flagged", "if_anomaly_score", "if_flagged",
                "anomaly_flagged", "anomaly_type"]
    results[out_cols].to_csv(os.path.join(PROCESSED_DIR, "fact_anomalies.csv"), index=False)
    lead_time_df.to_csv(os.path.join(PROCESSED_DIR, "anomaly_lead_time.csv"), index=False)

    print("=== AegisIQ ML Anomaly Detection ===")
    flagged = results[results["anomaly_flagged"]].sort_values(["control_id", "period"])
    print(f"\nTotal (control x period) observations: {len(results)}")
    print(f"Flagged anomalies: {len(flagged)}")
    print("\nFlagged anomalies detail:")
    print(flagged[["control_id", "period", "pass_rate", "rag_status", "anomaly_type"]].to_string(index=False))
    print("\nEarly-warning lead time (periods anomaly flag preceded first Red rating):")
    print(lead_time_df.to_string(index=False) if len(lead_time_df) else "  (none with qualifying lead time)")


if __name__ == "__main__":
    main()
