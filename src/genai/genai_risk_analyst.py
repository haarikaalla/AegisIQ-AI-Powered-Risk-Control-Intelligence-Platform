"""
AegisIQ — Layer 6: GenAI Risk Analyst
========================================
WHY THIS LAYER EXISTS (AWM M&T rationale):
  Every M&T cycle, analysts spend real hours writing up the SAME kind of content:
  "here's what failed, here's our best hypothesis for why, here's what we
  recommend, here's the one-paragraph version for the committee." AegisIQ uses
  GenAI (Claude, via the Anthropic Messages API) to draft all four of these,
  GROUNDED STRICTLY in the structured outputs of Layers 2-5 (risk scores,
  exceptions, anomaly flags) — not on open-ended chat, and not with any
  information the model wasn't given. A human control owner reviews and
  approves every draft before it leaves the team; nothing here auto-publishes.

Grounding discipline (this is the part that matters):
  - Every prompt embeds ONLY the specific numeric facts pulled from
    fact_risk_scores.csv, fact_exceptions.csv, and fact_anomalies.csv for the
    control in question — the model is not asked to recall or infer facts about
    "Goldman Sachs" or any real institution.
  - A lightweight POST-HOC GROUNDING CHECK extracts every number the model's
    output claims and verifies each one appears in the source data package that
    was given to it. Ungrounded numeric claims are flagged, not silently passed
    through — this is a real (if simple) hallucination guardrail, not a promise.
  - Output is always labeled DRAFT — FOR CONTROL OWNER REVIEW.

Two modes:
  - LIVE MODE: if ANTHROPIC_API_KEY is set in the environment, calls the real
    Anthropic Messages API (model: claude-sonnet-4-6... in production this
    would be pinned to an approved model per Anthropic's docs).
  - FALLBACK MODE: if no key is configured, a deterministic template generator
    produces a structurally identical draft from the same grounded data
    package — so the pipeline remains fully runnable in any environment
    (local dev, CI, offline demo) without requiring live credentials.
"""
import os
import re
import json
import pandas as pd

PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "processed")
OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "outputs")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"


def _call_claude(system_prompt, user_prompt, max_tokens=600):
    """Calls the live Anthropic API if a key is present; otherwise returns None
    so the caller falls back to a deterministic template."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import requests
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    except Exception as e:
        print(f"  [info] Using deterministic fallback for this draft ({e}).")
        return None


SYSTEM_PROMPT = (
    "You are a risk and control analyst supporting an Asset & Wealth Management "
    "Monitoring & Testing function. You draft findings, root-cause hypotheses, "
    "remediation recommendations, and executive summaries. STRICT RULES: "
    "(1) Use ONLY the facts given to you in the data package below — never invent "
    "numbers, dates, or claims not present in it. (2) Every draft is a DRAFT for "
    "human control-owner review, never a final determination. (3) Be concise, "
    "specific, and use the register of an internal risk committee memo, not a "
    "chatbot. (4) When proposing root cause, clearly flag it as a hypothesis to "
    "be validated by the process owner, not a confirmed cause."
)


def _grounding_check(text, allowed_numbers):
    """Extract numeric claims from generated text and flag any not traceable to the
    source data package (a simple, honest hallucination guardrail — not a proof of
    correctness, but it catches invented figures). Deliberately excludes list-item
    markers ('1.', '2.') and dates/percentages/IDs that are substrings of an allowed
    value (e.g. '08' inside an allowed '2026-08' period, or '01' inside 'KC-01'),
    since those are formatting artifacts, not independent factual claims."""
    allowed_blob = " | ".join(allowed_numbers)
    # numbers with >=2 significant digits, not immediately followed by '.' + whitespace
    # (which would make them a list marker like "1. " or "2. ")
    candidates = re.findall(r"\d+\.?\d*%?", text)
    ungrounded = []
    for n in candidates:
        if re.fullmatch(r"\d\.", n):  # single-digit list markers "1.", "2.", "3."
            continue
        bare = n.rstrip("%")
        if bare in allowed_numbers or n in allowed_numbers:
            continue
        if bare in allowed_blob:  # substring of a longer allowed value (date/period/ID fragment)
            continue
        if len(bare) <= 1:  # stray single digits aren't meaningful independent claims
            continue
        ungrounded.append(n)
    return ungrounded


class RiskAnalystGenAI:
    def __init__(self):
        self.risk_scores = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_risk_scores.csv"))
        self.exceptions = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_exceptions.csv"))
        self.anomalies = pd.read_csv(os.path.join(PROCESSED_DIR, "fact_anomalies.csv"))
        self.remediation = pd.read_csv(os.path.join(PROCESSED_DIR, "remediation_actions.csv"))
        self.mode_log = []

    def _data_package(self, control_id):
        rs = self.risk_scores[self.risk_scores["control_id"] == control_id].iloc[0]
        exc = self.exceptions[(self.exceptions["control_id"] == control_id) & (self.exceptions["status"] == "Open")]
        anom = self.anomalies[(self.anomalies["control_id"] == control_id) & (self.anomalies["anomaly_flagged"])]
        rem = self.remediation[self.remediation["control_id"] == control_id]
        reason_counts = exc["reason_code"].value_counts().to_dict()
        package = {
            "control_id": control_id,
            "control_name": rs["control_name"],
            "process_name": rs["process_name"],
            "period": rs["period"],
            "latest_pass_rate_pct": round(rs["latest_pass_rate"] * 100, 1),
            "kri_threshold_pct": round(rs["kri_threshold"] * 100, 1),
            "control_effectiveness_score": rs["control_effectiveness_score"],
            "control_effectiveness_rating": rs["control_effectiveness_rating"],
            "residual_risk_rating": rs["residual_risk_rating"],
            "open_exceptions": int(rs["open_exceptions"]),
            "overdue_remediation_actions": int((rem["status_vs_sla"] == "Overdue").sum()) if len(rem) else 0,
            "top_reason_codes": reason_counts,
            "anomaly_periods_flagged": anom["period"].tolist(),
            "anomaly_types": anom["anomaly_type"].unique().tolist(),
        }
        return package, rs, exc, anom, rem

    def _allowed_numbers(self, package):
        nums = set()

        def _add(v):
            if isinstance(v, (int, float)):
                nums.add(str(v))
                nums.add(str(round(v)))
            elif isinstance(v, str):
                nums.add(v)
                for tok in re.findall(r"\d+\.?\d*", v):
                    nums.add(tok)
            elif isinstance(v, list):
                for item in v:
                    _add(item)
            elif isinstance(v, dict):
                for k, vv in v.items():
                    _add(k)
                    _add(vv)

        for v in package.values():
            _add(v)
        return nums

    def draft_finding_and_root_cause_and_remediation(self, control_id):
        package, rs, exc, anom, rem = self._data_package(control_id)
        allowed = self._allowed_numbers(package)

        user_prompt = (
            f"DATA PACKAGE (control {control_id}):\n{json.dumps(package, indent=2, default=str)}\n\n"
            "Produce three sections, each with a clear header:\n"
            "1. FINDING SUMMARY — 2-3 sentences describing what testing found, quoting the "
            "pass rate, threshold, and open exception count.\n"
            "2. ROOT CAUSE HYPOTHESIS — 2-3 sentences, framed as a hypothesis to validate, "
            "grounded in the reason codes and anomaly type given.\n"
            "3. REMEDIATION RECOMMENDATION — 2-3 concrete, actionable recommendations a control "
            "owner could execute, tied to the specific control and reason codes given."
        )

        live_text = _call_claude(SYSTEM_PROMPT, user_prompt)
        if live_text:
            self.mode_log.append((control_id, "LIVE"))
            text = live_text
        else:
            self.mode_log.append((control_id, "OFFLINE_TEMPLATE"))
            text = self._offline_template(package)

        ungrounded = _grounding_check(text, allowed)
        return text, ungrounded, package

    def _offline_template(self, p):
        """Deterministic fallback template used when no ANTHROPIC_API_KEY is configured.
        Structurally mirrors what the live prompt asks for and is built from the same
        grounded data package with no invented facts, so behavior stays consistent
        whether or not a live API key is present."""
        reasons = p["top_reason_codes"]
        top_reason = max(reasons, key=reasons.get) if reasons else "n/a"
        top_reason_n = reasons.get(top_reason, 0)
        anomaly_note = (
            f"An ML-based anomaly scan flagged {len(p['anomaly_periods_flagged'])} period(s) "
            f"({', '.join(map(str, p['anomaly_periods_flagged'])) or 'none'}) as "
            f"{'/'.join(p['anomaly_types']) if p['anomaly_types'] else 'no anomaly pattern'}."
        ) if p["anomaly_periods_flagged"] else "No ML anomaly flags were raised for this control in the periods reviewed."

        finding = (
            f"FINDING SUMMARY\n"
            f"Control {p['control_id']} ({p['control_name']}, {p['process_name']}) recorded a "
            f"{p['latest_pass_rate_pct']}% pass rate in {p['period']} against a "
            f"{p['kri_threshold_pct']}% KRI threshold, rated {p['residual_risk_rating']} residual "
            f"risk with a control effectiveness score of {p['control_effectiveness_score']} "
            f"({p['control_effectiveness_rating']}). There are currently {p['open_exceptions']} "
            f"open exceptions, of which {p['overdue_remediation_actions']} are overdue against "
            f"their remediation SLA."
        )
        root_cause = (
            f"\n\nROOT CAUSE HYPOTHESIS (draft — to validate with process owner)\n"
            f"The dominant exception reason code is '{top_reason}' ({top_reason_n} of open "
            f"exceptions), suggesting the issue is concentrated in a specific failure mode rather "
            f"than dispersed noise. {anomaly_note} This pattern is consistent with either a "
            f"process/staffing capacity issue or an unpropagated system/reference-data change, "
            f"depending on which the process owner confirms drove the '{top_reason}' cases."
        )
        remediation = (
            f"\n\nREMEDIATION RECOMMENDATION (draft — for control owner action)\n"
            f"1. Prioritize closure of the {p['overdue_remediation_actions']} overdue items first, "
            f"given they already breach the remediation SLA.\n"
            f"2. Root-cause the '{top_reason}' cluster specifically — confirm whether it stems from "
            f"a system/reference-data issue (fix at source, one-time) or a capacity/process issue "
            f"(may need a recurring resourcing or process change).\n"
            f"3. Given {p['residual_risk_rating']} residual risk, escalate to the process owner for "
            f"an interim mitigating control (e.g., manual secondary review) until the pass rate "
            f"recovers above the {p['kri_threshold_pct']}% threshold for two consecutive cycles."
        )
        return (
            "**AI-Generated Draft — AegisIQ GenAI Risk Analyst**\n\n" + finding + root_cause + remediation
        )

    def draft_executive_mi_narrative(self):
        top5 = self.risk_scores.sort_values("residual_risk_score", ascending=False).head(5)
        overall_open = self.exceptions[self.exceptions["status"] == "Open"].shape[0]
        overall_overdue = self.remediation[self.remediation["status_vs_sla"] == "Overdue"].shape[0]
        dist = self.risk_scores["residual_risk_rating"].value_counts().to_dict()

        package = {
            "controls_tested": int(len(self.risk_scores)),
            "residual_risk_distribution": dist,
            "total_open_exceptions": int(overall_open),
            "total_overdue_remediation_actions": int(overall_overdue),
            "top5_controls_by_residual_risk": top5[["control_id", "control_name", "residual_risk_rating",
                                                      "control_effectiveness_score"]].to_dict(orient="records"),
        }
        allowed = self._allowed_numbers(package)
        for row in package["top5_controls_by_residual_risk"]:
            for v in row.values():
                allowed.add(str(v))

        user_prompt = (
            f"DATA PACKAGE (portfolio-level M&T summary):\n{json.dumps(package, indent=2, default=str)}\n\n"
            "Draft a 1-paragraph executive narrative (4-6 sentences) suitable for the opening page "
            "of a Monitoring & Testing committee pack. State the overall control environment "
            "condition, the residual risk distribution, and name the top control(s) needing "
            "committee attention, using only the numbers given."
        )
        live_text = _call_claude(SYSTEM_PROMPT, user_prompt, max_tokens=400)
        if live_text:
            self.mode_log.append(("EXEC_NARRATIVE", "LIVE"))
            text = live_text
        else:
            self.mode_log.append(("EXEC_NARRATIVE", "OFFLINE_TEMPLATE"))
            crit_high = dist.get("Critical", 0) + dist.get("High", 0)
            worst = top5.iloc[0]
            text = (
                f"This cycle's Monitoring & Testing program covered {package['controls_tested']} controls "
                f"across the AWM process universe. The residual risk distribution is "
                f"{', '.join(f'{k}: {v}' for k, v in dist.items())}, with {crit_high} control(s) rated "
                f"High or Critical residual risk requiring committee attention. Across all controls, "
                f"{overall_open} exceptions remain open, of which {overall_overdue} have breached their "
                f"remediation SLA and should be escalated. The control most in need of committee focus "
                f"is {worst['control_id']} ({worst['control_name']}), rated {worst['residual_risk_rating']} "
                f"residual risk with a control effectiveness score of {worst['control_effectiveness_score']}. "
                f"Management is directed to review the accompanying findings and remediation detail for "
                f"each Red-rated control before next cycle."
            )
        ungrounded = _grounding_check(text, allowed)
        return text, ungrounded, package


def main():
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    analyst = RiskAnalystGenAI()

    # Draft for the top N highest-residual-risk controls
    top_controls = analyst.risk_scores.sort_values("residual_risk_score", ascending=False).head(6)["control_id"].tolist()

    report_sections = []
    report_sections.append("# AegisIQ — GenAI-Assisted Risk & Control Findings Report\n")
    report_sections.append("*All content below is a DRAFT for control-owner and risk-committee review, "
                            "generated by AegisIQ's GenAI Risk Analyst layer, grounded strictly in the "
                            "structured outputs of the control testing, risk scoring, and anomaly "
                            "detection layers.*\n")

    exec_text, exec_ungrounded, _ = analyst.draft_executive_mi_narrative()
    report_sections.append("## Executive Summary\n\n" + exec_text + "\n")
    if exec_ungrounded:
        report_sections.append(f"\n> ⚠ Grounding check flagged possibly-ungrounded figures: {exec_ungrounded}\n")

    for cid in top_controls:
        text, ungrounded, package = analyst.draft_finding_and_root_cause_and_remediation(cid)
        report_sections.append(f"\n---\n## {cid} — {package['control_name']} ({package['process_name']})\n")
        report_sections.append(text + "\n")
        if ungrounded:
            report_sections.append(f"\n> ⚠ Grounding check flagged possibly-ungrounded figures: {ungrounded}\n")

    report = "\n".join(report_sections)
    out_path = os.path.join(OUTPUTS_DIR, "genai_findings_report.md")
    with open(out_path, "w") as f:
        f.write(report)

    print("=== AegisIQ GenAI Risk Analyst ===")
    modes = pd.DataFrame(analyst.mode_log, columns=["item", "mode"])
    print(modes["mode"].value_counts().to_string())
    print(f"\nReport written to {out_path}")
    print(f"Controls drafted: {top_controls}")


if __name__ == "__main__":
    main()
