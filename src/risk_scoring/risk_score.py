"""
AegisIQ — Layer 4: Composite Risk Scoring Engine
====================================================
WHY THIS LAYER EXISTS (AWM M&T rationale):
  A risk committee doesn't want 147 rows of pass-rate history — it wants to know,
  for each control/process, whether residual risk is Low/Medium/High/Critical and
  why. This engine converts raw KRI/KPI results into a small number of ratings
  using a documented, auditable methodology (weights and rules are visible below,
  not hidden in a model). This mirrors standard operational-risk-and-control
  scoring: Inherent Risk x (1 - Control Effectiveness) -> Residual Risk.

Methodology:
  1. INHERENT RISK (1-5) — static, judgmental input per control, set here from the
     control catalog's risk_category + control_type as a transparent proxy (in a
     real M&T function this comes from a risk & control self-assessment, RCSA).
  2. CONTROL EFFECTIVENESS SCORE (0-100) — derived from:
       - latest-period pass rate vs. threshold (50% weight)
       - 3-period trend direction (20% weight): improving/stable/degrading
       - exception severity/backlog (20% weight): aging & count of open exceptions
       - SLA breach frequency over trailing 6 periods (10% weight)
  3. RESIDUAL RISK = Inherent Risk x (1 - Control Effectiveness/100), scaled to
     Low / Medium / High / Critical bands.
  4. Every score keeps its component inputs alongside it — a risk committee member
     (or GenAI drafting the MI) can see exactly why a control landed where it did.
"""
import os
import numpy as np
import pandas as pd

PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "processed")

# Inherent risk proxy (1-5) by risk_category x control_type — documented assumption,
# not fitted from data. Compliance and Valuation processes carry higher inherent risk
# in AWM given regulatory and client-money sensitivity.
INHERENT_RISK_MAP = {
    ("Compliance", "Preventive"): 4,
    ("Compliance", "Detective"): 4,
    ("Valuation", "Detective"): 4,
    ("Suitability", "Detective"): 4,
    ("Suitability", "Preventive"): 3,
    ("Operational", "Preventive"): 3,
    ("Operational", "Detective"): 3,
}


def inherent_risk(risk_category, control_type):
    return INHERENT_RISK_MAP.get((risk_category, control_type), 3)


def trend_score(pass_rates):
    """Compare latest pass rate to the mean of the prior 2 periods. Returns 0-100."""
    if len(pass_rates) < 2:
        return 70  # neutral/insufficient history
    latest = pass_rates[-1]
    prior_mean = np.mean(pass_rates[max(0, len(pass_rates) - 4):-1]) if len(pass_rates) > 1 else latest
    delta = latest - prior_mean
    if delta >= 0.005:
        return 90   # improving
    elif delta >= -0.01:
        return 70   # stable
    elif delta >= -0.03:
        return 45   # degrading
    else:
        return 20   # sharply degrading


def exception_severity_score(open_exceptions_count, population):
    if population == 0:
        return 70
    rate = open_exceptions_count / population
    if rate < 0.01:
        return 95
    elif rate < 0.03:
        return 75
    elif rate < 0.07:
        return 50
    else:
        return 20


def sla_breach_freq_score(rag_history):
    """Share of trailing periods rated Red or Amber."""
    if len(rag_history) == 0:
        return 70
    breach_share = sum(1 for r in rag_history if r in ("Red", "Amber")) / len(rag_history)
    return round(100 * (1 - breach_share))


def residual_band(score_0_to_20):
    if score_0_to_20 >= 15:
        return "Critical"
    elif score_0_to_20 >= 10:
        return "High"
    elif score_0_to_20 >= 5:
        return "Medium"
    else:
        return "Low"


def main():
    results = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_control_test_results.csv"))
    exceptions = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_exceptions.csv"))
    catalog = pd.read_csv(os.path.join(PROCESSED_DIR, "control_catalog_clean.csv")).set_index("control_id")

    results = results.sort_values(["control_id", "period"])
    rows = []
    for control_id, grp in results.groupby("control_id"):
        grp = grp.sort_values("period")
        latest = grp.iloc[-1]
        trailing6 = grp.tail(6)
        pass_rates = grp["pass_rate"].dropna().tolist()

        cat = catalog.loc[control_id]
        i_risk = inherent_risk(cat["risk_category"], cat["control_type"])

        pass_component = min(100, 100 * (latest["pass_rate"] / cat["kri_threshold_pass_rate"])) if pd.notna(latest["pass_rate"]) else 50
        pass_component = max(0, min(100, pass_component))
        trend_component = trend_score(pass_rates)

        open_exc = exceptions[(exceptions["control_id"] == control_id) & (exceptions["status"] == "Open")]
        exc_component = exception_severity_score(len(open_exc), latest["population"])

        sla_component = sla_breach_freq_score(trailing6["rag_status"].tolist())

        control_effectiveness = (
            0.50 * pass_component + 0.20 * trend_component +
            0.20 * exc_component + 0.10 * sla_component
        )
        control_effectiveness = round(control_effectiveness, 1)

        control_eff_rating = (
            "Effective" if control_effectiveness >= 85 else
            "Needs Improvement" if control_effectiveness >= 65 else
            "Ineffective"
        )

        residual_raw = i_risk * (1 - control_effectiveness / 100) * 5  # scale to ~0-20
        residual_rating = residual_band(residual_raw)

        rows.append(dict(
            control_id=control_id,
            control_name=cat["control_name"],
            process_name=cat["process_name"],
            risk_category=cat["risk_category"],
            period=latest["period"],
            latest_pass_rate=latest["pass_rate"],
            kri_threshold=cat["kri_threshold_pass_rate"],
            inherent_risk_1to5=i_risk,
            pass_component=round(pass_component, 1),
            trend_component=trend_component,
            exception_severity_component=exc_component,
            sla_breach_component=sla_component,
            control_effectiveness_score=control_effectiveness,
            control_effectiveness_rating=control_eff_rating,
            residual_risk_score=round(residual_raw, 2),
            residual_risk_rating=residual_rating,
            open_exceptions=len(open_exc),
            rag_status_latest=latest["rag_status"],
        ))

    scores = pd.DataFrame(rows).sort_values("residual_risk_score", ascending=False)
    scores.to_csv(os.path.join(PROCESSED_DIR, "fact_risk_scores.csv"), index=False)

    print("=== AegisIQ Risk Scoring Engine ===")
    print(scores[["control_id", "control_name", "control_effectiveness_score",
                   "control_effectiveness_rating", "residual_risk_rating", "open_exceptions"]].to_string(index=False))

    print("\nResidual risk distribution:")
    print(scores["residual_risk_rating"].value_counts().to_string())


if __name__ == "__main__":
    main()
