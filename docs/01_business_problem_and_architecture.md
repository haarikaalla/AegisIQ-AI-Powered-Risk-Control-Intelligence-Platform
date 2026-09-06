# AegisIQ — AI-Powered Risk & Control Intelligence Platform
## Stage 1: Business Problem & Architecture

> **Synthetic project disclaimer**: All data, control names, thresholds, org structures and
> workflows in AegisIQ are fictional and generated for demonstration purposes. Nothing here
> reflects actual Goldman Sachs systems, processes, controls, risk appetite, or proprietary
> information. The design is *inspired by* the kind of work a Monitoring & Testing (M&T) function
> inside an Asset & Wealth Management (AWM) business would plausibly do, based on publicly known
> industry practice (COSO/operational risk frameworks, SOX-style control testing, KRI/KCI
> monitoring, exception management).

---

### 1.1 The business problem

An AWM Monitoring & Testing team is the second-line/first-line-adjacent function that answers one
question, continuously, across hundreds of controls and processes: **"Are our controls actually
working, and is our residual risk within appetite?"** Concretely, M&T teams:

- Execute **recurring control tests** (trade allocation checks, fee billing recalculation,
  suitability reviews, cash movement approvals, valuation breaks, reconciliation breaks, KYC/AML
  refresh SLAs) across many products, desks, and legal entities.
- Track **KRIs/KCIs/KPIs** (e.g., % of trades allocated within SLA, breaks aged >5 days, exception
  backlog) against thresholds, and escalate breaches.
- Investigate **exceptions**, determine **root cause**, and assess whether a failure is isolated or
  indicates a **control design/effectiveness gap**.
- Assign and track **remediation** to closure, with aging and re-testing.
- Produce **management information (MI)** — packs for control owners, risk committees, and audit —
  that turns hundreds of test results into a small number of defensible conclusions.

**Why this is hard at scale**: test volume grows faster than headcount, root-cause analysis is
manually written every cycle in near-identical language, anomalies (a single control quietly
drifting from a 98% pass rate to 91%) are easy to miss in a spreadsheet, and MI decks take days to
assemble by hand before every committee.

**What AegisIQ automates, and what it deliberately leaves to humans:**

| Automated | Left to a human |
|---|---|
| Data ingestion/cleaning from source-system extracts | Final risk rating sign-off |
| Deterministic control testing (rules-as-code) | Judgmental control design assessment |
| KRI/KCI/KPI computation & threshold monitoring | Risk appetite setting |
| Composite risk scoring per control/process | Escalation decisions on borderline cases |
| ML anomaly detection on test-result time series | Root-cause investigation *interviews* |
| GenAI drafting of findings, root cause hypotheses, remediation text, MI narrative | Final approval of any GenAI-drafted content before it leaves the team |

This split matters: AegisIQ is a **decision-support and drafting accelerator**, not an
autonomous risk-rating engine. Every GenAI output is explicitly labeled as a draft for review.

---

### 1.2 Scope: the synthetic AWM process universe

To keep the platform realistic but self-contained, AegisIQ models **6 AWM processes**, each with
its own controls:

1. **Trade Allocation & Execution** — fair allocation across client accounts, execution price
   reasonableness.
2. **Fee Billing & Calculation** — management/performance fee computation accuracy, invoice timing.
3. **Client Onboarding / KYC-AML Refresh** — periodic refresh SLA, document completeness.
4. **NAV / Valuation Oversight** — pricing source validation, break investigation & aging.
5. **Cash & Collateral Movements** — dual approval, breaks vs. custodian statements.
6. **Suitability & Mandate Compliance** — portfolio-vs-mandate guideline breach detection.

Each process has 3–5 **controls**, each control has a **test procedure**, a **frequency**, a
**population** of transactions/events it tests, and produces **pass/fail/exception** results over
time — this is the raw material for everything downstream.

---

### 1.3 Architecture

```
                         ┌───────────────────────────────────────────┐
                         │            SOURCE DATA (synthetic)         │
                         │  trades, fee calcs, KYC cases, NAV prices,  │
                         │  cash movements, mandates, control catalog  │
                         └───────────────────┬───────────────────────┘
                                             │  (raw CSV extracts, "as messy as real life")
                                             ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │  LAYER 1 — INGESTION & CLEANING (Python, pandas)                                 │
 │  src/ingestion/etl.py                                                            │
 │  • schema validation, type coercion, dedup, missing-value handling               │
 │  • writes clean, versioned tables to data/processed/ + loads into SQLite (SQL)   │
 │  WHY: M&T evidence must be defensible — a control test on dirty data is worse    │
 │       than no test. This layer is the audit trail for data quality.             │
 └───────────────────┬──────────────────────────────────────────────────────────┘
                     ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │  LAYER 2 — SQL ANALYTICS & KRI/KCI/KPI ENGINE                                    │
 │  sql/schema.sql, sql/analytics_queries.sql (run against SQLite for reproducibility)│
 │  • control population counts, pass/fail rates, SLA aging, exception backlog      │
 │  WHY: SQL is the lingua franca for reproducible, auditable metric definitions —  │
 │       any tester or auditor can re-run the exact query that produced an MI number.│
 └───────────────────┬──────────────────────────────────────────────────────────┘
                     ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │  LAYER 3 — AUTOMATED CONTROL TESTING ENGINE (Python, rules-as-code)              │
 │  src/controls/control_testing.py                                                 │
 │  • re-performs each control's test logic over the full population (not a sample) │
 │  • outputs pass/fail/exception + reason code per test instance                  │
 │  WHY: 100%-population automated testing (vs. manual sample testing) is the       │
 │       single highest-value shift in modern M&T — it finds every exception,      │
 │       not just the ones in a 25-item sample.                                    │
 └───────────────────┬──────────────────────────────────────────────────────────┘
                     ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │  LAYER 4 — RISK SCORING ENGINE                                                   │
 │  src/risk_scoring/risk_score.py                                                  │
 │  • composite score = f(likelihood, impact, control effectiveness, exception     │
 │    trend, SLA breach severity) → inherent risk, control effectiveness rating,   │
 │    residual risk rating (Low/Medium/High/Critical)                              │
 │  WHY: turns dozens of raw KPIs into the small number of ratings a risk           │
 │       committee actually acts on — and does it consistently, not by feel.       │
 └───────────────────┬──────────────────────────────────────────────────────────┘
                     ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │  LAYER 5 — ML ANOMALY DETECTION                                                  │
 │  src/ml_anomaly/anomaly_detection.py                                             │
 │  • IsolationForest + rolling z-score on control pass-rate / volume / aging       │
 │    time series → flags controls "drifting" before they breach a hard threshold  │
 │  WHY: thresholds catch what's already broken; anomaly detection catches what's  │
 │       *about to* break — genuine forward-looking value, not ML for its own sake.│
 └───────────────────┬──────────────────────────────────────────────────────────┘
                     ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │  LAYER 6 — GENAI RISK ANALYST                                                    │
 │  src/genai/genai_risk_analyst.py                                                 │
 │  • drafts: finding summaries, root-cause hypotheses, remediation                 │
 │    recommendations, and an executive MI narrative — grounded ONLY in the        │
 │    structured outputs of Layers 2–5 (no free-floating hallucination)            │
 │  WHY: this is where analyst hours actually go each cycle — writing up findings   │
 │       in committee-ready language. GenAI drafts it; a human approves it.        │
 └───────────────────┬──────────────────────────────────────────────────────────┘
                     ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │  LAYER 7 — REMEDIATION WORKFLOW & TRACKING                                       │
 │  data/processed/remediation_actions.csv + status/aging logic in control_testing  │
 │  WHY: a finding without a tracked, owned, dated remediation action is not a      │
 │       closed loop — this is what audit checks for.                             │
 └───────────────────┬──────────────────────────────────────────────────────────┘
                     ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │  LAYER 8 — REPORTING & DASHBOARDS                                                │
 │  outputs/AegisIQ_Executive_Report.xlsx (Excel, formula-driven)                   │
 │  dashboards/executive_dashboard.html (Power BI/Tableau-style exec dashboard)     │
 │  WHY: two audiences, two formats — Excel for detail-oriented control owners who  │
 │       want to pivot/filter, a BI-style dashboard for committee-level overview.   │
 └────────────────────────────────────────────────────────────────────────────────┘
```

### 1.4 Technology mapping and rationale

| Tool | Where used | Why this tool specifically |
|---|---|---|
| **Python (pandas)** | Ingestion, cleaning, feature engineering | Handles messy multi-source data, reproducible scripts vs. manual Excel manipulation |
| **SQL (SQLite)** | Canonical metric definitions, KRI/KCI/KPI queries | Auditable, re-runnable, standard for control testing evidence |
| **Excel (openpyxl)** | Executive report workbook | Control owners live in Excel; formulas (not hardcoded numbers) so it recalculates |
| **Power BI/Tableau-style dashboard** | HTML executive dashboard | Committee-level visual overview; built here as a portable HTML/JS artifact standing in for a BI tool, with the same visual grammar (KPI tiles, trend lines, heatmaps) |
| **Machine Learning (scikit-learn)** | Anomaly detection on control metrics | Forward-looking drift detection beyond static thresholds |
| **Generative AI (Claude/Anthropic API)** | Finding summarization, root cause, remediation drafting, MI narrative | Turns structured risk data into committee-ready prose in seconds, grounded in the pipeline's own outputs |

### 1.5 Success metrics for AegisIQ itself

- **Population coverage**: 100% of in-scope transactions tested (vs. typical 5–10% manual sample).
- **Time-to-MI**: automated pack generation in minutes vs. multi-day manual cycle (measured, not just claimed — see Stage 8 validation).
- **Anomaly lead time**: number of monitoring cycles an ML flag precedes a hard threshold breach.
- **Draft acceptance quality**: structural check that every GenAI claim traces to a specific number in the pipeline (see Stage 6 grounding tests).

Next: Stage 2 — dataset & schema design.
