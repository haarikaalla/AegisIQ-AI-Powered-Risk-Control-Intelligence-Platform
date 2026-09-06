"""
AegisIQ — Stage 8: Pipeline Validation & Testing
====================================================
WHY THIS LAYER EXISTS (AWM M&T rationale):
  A control-testing platform that isn't itself tested has no credibility as
  evidence. This suite checks structural integrity, referential consistency,
  and a handful of "known answer" checks against the injected anomalies from
  the data generator — i.e. we verify the platform actually FINDS the issues
  we deliberately planted (TC-01 drift, FC-01 spike, KC-01 backlog growth),
  not just that the code runs without error.

Run: python validate_pipeline.py   (exits non-zero on any FAIL)
"""
import os
import sys
import pandas as pd

PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "processed")
OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))


def main():
    # --- structural checks ---
    for fname in ["fact_control_test_results.csv", "fact_exceptions.csv", "fact_risk_scores.csv",
                  "fact_anomalies.csv", "remediation_actions.csv", "data_quality_log.csv"]:
        path = os.path.join(PROCESSED_DIR, fname)
        check(f"File exists: {fname}", os.path.exists(path))

    results_df = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_control_test_results.csv"))
    exceptions_df = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_exceptions.csv"))
    risk_df = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_risk_scores.csv"))
    anomalies_df = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_anomalies.csv"))
    catalog_df = pd.read_csv(os.path.join(PROCESSED_DIR, "control_catalog_clean.csv"))

    check("All 13 controls present in catalog", len(catalog_df) == 13, f"found {len(catalog_df)}")
    check("All 13 controls have test results", results_df["control_id"].nunique() == 13,
          f"found {results_df['control_id'].nunique()}")
    check("All 13 controls have risk scores", risk_df["control_id"].nunique() == 13,
          f"found {risk_df['control_id'].nunique()}")
    check("No null pass_rate on populated test rows",
          results_df.loc[results_df["population"] > 0, "pass_rate"].isna().sum() == 0)
    check("Pass rates within [0,1]",
          results_df["pass_rate"].dropna().between(0, 1).all())
    check("Every exception maps to a real control_id",
          exceptions_df["control_id"].isin(catalog_df["control_id"]).all())
    check("Exception status only Open/Closed",
          set(exceptions_df["status"].unique()) <= {"Open", "Closed"})
    check("Risk scores reference valid RAG values",
          set(risk_df["rag_status_latest"].dropna().unique()) <= {"Green", "Amber", "Red", "No Data"})
    check("Residual risk ratings within expected set",
          set(risk_df["residual_risk_rating"].unique()) <= {"Low", "Medium", "High", "Critical"})

    # --- known-answer checks (does the platform find what we planted?) ---
    tc01 = results_df[results_df["control_id"] == "TC-01"].sort_values("period")
    early = tc01[tc01["period"] < "2026-05"]["pass_rate"].mean()
    late = tc01[tc01["period"] >= "2026-05"]["pass_rate"].mean()
    check("TC-01 shows the planted late-period degradation (early avg > late avg)",
          early > late, f"early={early:.3f} late={late:.3f}")

    fc01 = results_df[results_df["control_id"] == "FC-01"].sort_values("period")
    feb = fc01[fc01["period"] == "2026-02"]["pass_rate"].values
    other_months = fc01[fc01["period"] != "2026-02"]["pass_rate"].mean()
    check("FC-01 shows the planted Feb-2026 step-change spike (Feb well below other months' average)",
          len(feb) > 0 and feb[0] < other_months - 0.10,
          f"feb={feb[0] if len(feb) else 'NA'} other_avg={other_months:.3f}")

    kc01 = results_df[results_df["control_id"] == "KC-01"].sort_values("period")
    kc01_early = kc01[kc01["period"] < "2026-06"]["pass_rate"].mean()
    kc01_late = kc01[kc01["period"] >= "2026-06"]["pass_rate"].mean()
    check("KC-01 shows the planted backlog-growth degradation (early avg > late avg)",
          kc01_early > kc01_late, f"early={kc01_early:.3f} late={kc01_late:.3f}")

    # --- does the ML layer actually catch these? ---
    anom_tc01 = anomalies_df[(anomalies_df["control_id"] == "TC-01") & (anomalies_df["anomaly_flagged"])]
    check("ML anomaly detection flags at least one TC-01 drift period", len(anom_tc01) >= 1,
          f"flagged periods={anom_tc01['period'].tolist()}")

    anom_fc01 = anomalies_df[(anomalies_df["control_id"] == "FC-01") & (anomalies_df["anomaly_flagged"])
                              & (anomalies_df["period"] == "2026-02")]
    check("ML anomaly detection flags the FC-01 Feb-2026 spike specifically", len(anom_fc01) >= 1)

    anom_kc01 = anomalies_df[(anomalies_df["control_id"] == "KC-01") & (anomalies_df["anomaly_flagged"])]
    check("ML anomaly detection flags at least one KC-01 backlog period", len(anom_kc01) >= 1,
          f"flagged periods={anom_kc01['period'].tolist()}")

    # --- risk scoring sanity: degraded controls should not be rated Low ---
    kc01_risk = risk_df[risk_df["control_id"] == "KC-01"]["residual_risk_rating"].values[0]
    check("KC-01 (degraded, below threshold) is NOT rated Low residual risk", kc01_risk != "Low",
          f"rated={kc01_risk}")

    # --- GenAI grounding: report exists and has no unresolved grounding warnings ---
    genai_path = os.path.join(OUTPUTS_DIR, "genai_findings_report.md")
    check("GenAI findings report was generated", os.path.exists(genai_path))
    if os.path.exists(genai_path):
        with open(genai_path) as f:
            content = f.read()
        check("GenAI report contains no unresolved grounding warnings", "⚠" not in content)
        check("GenAI report explicitly labels drafts as DRAFT for review", "DRAFT" in content)

    # --- print results ---
    print("=== AegisIQ Pipeline Validation ===\n")
    n_pass = sum(1 for _, s, _ in results if s == "PASS")
    n_fail = sum(1 for _, s, _ in results if s == "FAIL")
    for name, status, detail in results:
        marker = "✅" if status == "PASS" else "❌"
        line = f"{marker} [{status}] {name}"
        if detail:
            line += f"  ({detail})"
        print(line)
    print(f"\n{n_pass}/{len(results)} checks passed.")
    if n_fail:
        print(f"{n_fail} CHECK(S) FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    main()
