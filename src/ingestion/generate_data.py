"""
AegisIQ — Synthetic Data Generator
===================================
Generates realistic, SYNTHETIC AWM transaction data across 6 processes, with:
  - deliberate data-quality defects (nulls, dupes, out-of-range values, format drift)
  - deliberate control degradations (slow drift, step-change, backlog growth)
so that downstream cleaning, control testing, and anomaly detection have something
real to find. All entities/names/values are fictional.

Run: python generate_data.py --outdir ../../data/raw --seed 42
"""
import argparse
import os
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

RNG_SEED = 42
START = datetime(2025, 9, 1)
END = datetime(2026, 8, 31)


def business_days(start, end):
    return pd.bdate_range(start, end)


def month_index(d, start=START):
    """0-based month offset from START, used to drive gradual degradation curves."""
    return (d.year - start.year) * 12 + (d.month - start.month)


# ----------------------------------------------------------------------------
# 1. TRADES  (Process 1: Trade Allocation & Execution)
# ----------------------------------------------------------------------------
def gen_trades(rng, bdays):
    rows = []
    trade_id = 100000
    desks = ["Equity", "Fixed Income", "Multi-Asset", "Alternatives"]
    segments = ["HNW", "Institutional", "Retail-Advisory", "Family Office"]
    n_months = 12
    for d in bdays:
        n_trades = rng.poisson(55)
        mi = month_index(d)
        # TC-01 allocation timeliness degrades gradually in last 4 months (mi 8..11)
        late_alloc_prob = 0.01 if mi < 8 else 0.01 + (mi - 7) * 0.033  # ramps to ~0.10-0.13
        for _ in range(n_trades):
            trade_id += 1
            desk = rng.choice(desks)
            segment = rng.choice(segments)
            asset_class = rng.choice(["Equity", "Bond", "ETF", "Derivative"], p=[0.45, 0.25, 0.25, 0.05])
            side = rng.choice(["BUY", "SELL"])
            qty = int(rng.integers(100, 20000))
            benchmark_price = round(rng.uniform(10, 500), 4)
            # execution price normally within ~5bps, occasionally a real outlier (TC-02 exceptions)
            if rng.random() < 0.015:
                price_dev = rng.uniform(0.002, 0.01) * rng.choice([-1, 1])  # 20-100bps outlier
            else:
                price_dev = rng.normal(0, 0.0004)  # ~4bps std noise
            execution_price = round(benchmark_price * (1 + price_dev), 4)

            order_block_id = f"BLK{trade_id // 3}"
            exec_ts = d + timedelta(hours=int(rng.integers(9, 15)), minutes=int(rng.integers(0, 59)))
            # allocation timing: normally 5-20 min after execution; late per degradation curve
            if rng.random() < late_alloc_prob:
                alloc_delay_min = rng.integers(45, 240)
            else:
                alloc_delay_min = rng.integers(2, 28)
            alloc_ts = exec_ts + timedelta(minutes=int(alloc_delay_min))

            # allocation_pct is set AFTER the full block is known (post-processing below);
            # placeholder here, overwritten to reflect pro-rata entitlement +/- noise.
            allocation_pct = np.nan

            trader_id = f"TR{rng.integers(1, 25):03d}"
            account_id = f"ACC{rng.integers(1, 900):05d}"

            rows.append(dict(
                trade_id=trade_id, trade_date=d.strftime("%Y-%m-%d"), account_id=account_id,
                client_segment=segment, security_id=f"SEC{rng.integers(1, 400):04d}",
                asset_class=asset_class, side=side, quantity=qty,
                execution_price=execution_price, benchmark_price=benchmark_price,
                order_block_id=order_block_id, allocation_pct=allocation_pct, desk=desk,
                trader_id=trader_id,
                execution_timestamp=exec_ts.strftime("%Y-%m-%d %H:%M:%S"),
                allocation_timestamp=alloc_ts.strftime("%Y-%m-%d %H:%M:%S"),
            ))
    df = pd.DataFrame(rows)

    # --- set allocation_pct as pro-rata share of quantity within each order block,
    #     with small routine noise and a ~3% deliberate fairness-exception rate (TC-03) ---
    block_qty_share = df["quantity"] / df.groupby("order_block_id")["quantity"].transform("sum")
    noise = rng.normal(0, 0.006, size=len(df))  # ~0.6pt routine noise, within the 2pt tolerance
    exception_mask = rng.random(len(df)) < 0.03
    exception_dev = rng.uniform(0.03, 0.09, size=len(df)) * rng.choice([-1, 1], size=len(df))
    allocation_pct = block_qty_share + noise
    allocation_pct = np.where(exception_mask, block_qty_share + exception_dev, allocation_pct)
    df["allocation_pct"] = np.clip(allocation_pct, 0.01, 1.0).round(4)

    # --- inject data quality defects ---
    # 1. duplicates (~0.3%)
    dupe_idx = df.sample(frac=0.003, random_state=42).index
    df = pd.concat([df, df.loc[dupe_idx]], ignore_index=True)
    # 2. missing quantity / execution_price (~1%)
    for col in ["quantity", "execution_price"]:
        null_idx = df.sample(frac=0.01, random_state=hash(col) % 1000).index
        df.loc[null_idx, col] = np.nan
    # 3. out-of-range values: negative quantity, zero price
    bad_idx = df.sample(frac=0.002, random_state=7).index
    df.loc[bad_idx, "quantity"] = -abs(df.loc[bad_idx, "quantity"].fillna(100))
    zero_idx = df.sample(frac=0.001, random_state=11).index
    df.loc[zero_idx, "execution_price"] = 0
    # 4. timestamp format drift for one month (simulate source system change) -> Jan 2026
    jan_mask = df["trade_date"].str.startswith("2026-01")
    jan_idx = df[jan_mask].index
    # rewrite as a different (still parseable-if-you-know-the-format, else messy) style
    df.loc[jan_idx, "execution_timestamp"] = df.loc[jan_idx, "execution_timestamp"].str.replace(
        "-", "/", regex=False)
    return df


# ----------------------------------------------------------------------------
# 2. FEE CALCULATIONS  (Process 2: Fee Billing)
# ----------------------------------------------------------------------------
def gen_fee_calculations(rng):
    rows = []
    fee_id = 500000
    n_accounts = 300
    periods = pd.date_range(START, END, freq="ME")
    for period in periods:
        mi = month_index(period)
        # FC-01 step-change spike in mismatches in Feb 2026 (mi index for Feb 2026 = 5)
        mismatch_prob = 0.02
        if period.strftime("%Y-%m") == "2026-02":
            mismatch_prob = 0.22  # sharp one-month spike (rate table propagation error)
        for acc in range(1, n_accounts + 1):
            fee_id += 1
            account_id = f"ACC{acc:05d}"
            aum = round(rng.uniform(2_000_000, 250_000_000), 2)
            fee_rate = round(rng.choice([0.0035, 0.0045, 0.0060, 0.0075]), 4)
            calculated_fee = round(aum * fee_rate / 12, 2)
            if rng.random() < mismatch_prob:
                billed_fee = round(calculated_fee * rng.uniform(1.01, 1.15), 2)
            else:
                billed_fee = round(calculated_fee * rng.uniform(0.999, 1.001), 2)
            period_end = (period + pd.offsets.MonthEnd(0)).to_pydatetime()
            # FC-02: invoice SLA = 10 business days after period end; occasional lateness
            sla_days = int(rng.integers(2, 9)) if rng.random() < 0.93 else int(rng.integers(11, 22))
            invoice_date = period_end + timedelta(days=sla_days)
            rows.append(dict(
                fee_id=fee_id, account_id=account_id, period=period.strftime("%Y-%m"),
                aum=aum, fee_rate=fee_rate, calculated_fee=calculated_fee, billed_fee=billed_fee,
                period_end_date=period_end.strftime("%Y-%m-%d"),
                invoice_date=invoice_date.strftime("%Y-%m-%d"),
            ))
    df = pd.DataFrame(rows)
    null_idx = df.sample(frac=0.008, random_state=3).index
    df.loc[null_idx, "billed_fee"] = np.nan
    return df


# ----------------------------------------------------------------------------
# 3. KYC CASES  (Process 3: Onboarding / KYC-AML Refresh)
# ----------------------------------------------------------------------------
def gen_kyc_cases(rng):
    rows = []
    case_id = 700000
    n_cases_per_month = 150
    periods = pd.date_range(START, END, freq="ME")
    for period in periods:
        mi = month_index(period)
        # KC-01: backlog worsens progressively in the last quarter (mi 9,10,11 -> Jun/Jul/Aug 2026)
        late_prob = 0.03 if mi < 9 else 0.03 + (mi - 8) * 0.08  # ramps to ~0.27
        for _ in range(n_cases_per_month):
            case_id += 1
            client_id = f"CLI{rng.integers(1, 4000):05d}"
            risk_rating = rng.choice(["Low", "Medium", "High"], p=[0.55, 0.35, 0.10])
            last_review = period - pd.DateOffset(years=1)
            next_due = period + pd.DateOffset(days=int(rng.integers(0, 5)))
            if rng.random() < late_prob:
                completed = next_due + timedelta(days=int(rng.integers(3, 45)))
                status = "Completed-Late"
            else:
                completed = next_due - timedelta(days=int(rng.integers(0, 6)))
                status = "Completed-OnTime"
            # small chance still open past due date (not yet completed)
            if rng.random() < 0.02:
                completed = pd.NaT
                status = "Open"
            docs_complete = rng.random() > 0.04
            rows.append(dict(
                case_id=case_id, client_id=client_id, client_risk_rating=risk_rating,
                last_review_date=last_review.strftime("%Y-%m-%d"),
                next_due_date=next_due.strftime("%Y-%m-%d"),
                review_completed_date=(completed.strftime("%Y-%m-%d") if pd.notna(completed) else ""),
                status=status, documents_complete=bool(docs_complete),
            ))
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# 4. NAV PRICES (Process 4: NAV / Valuation Oversight)
# ----------------------------------------------------------------------------
def gen_nav_prices(rng, bdays):
    rows = []
    price_id = 900000
    n_funds = 20
    prior = {f"FUND{i:03d}": round(rng.uniform(8, 250), 4) for i in range(1, n_funds + 1)}
    for d in bdays:
        for i in range(1, n_funds + 1):
            fund_id = f"FUND{i:03d}"
            price_id += 1
            drift = rng.normal(0, 0.004)
            price = round(prior[fund_id] * (1 + drift), 4)
            # independent source price: normally very close; occasional real break
            if rng.random() < 0.02:
                indep_dev = rng.uniform(0.003, 0.015) * rng.choice([-1, 1])  # 30-150bps break
            else:
                indep_dev = rng.normal(0, 0.0006)
            independent_source_price = round(price * (1 + indep_dev), 4)
            break_flag = abs(independent_source_price - price) / price > 0.0025
            break_identified = d if break_flag else None
            resolved = None
            if break_flag:
                # NV-02: most resolved within 3 bdays, some lag
                lag = int(rng.integers(1, 3)) if rng.random() < 0.85 else int(rng.integers(4, 10))
                resolved = d + timedelta(days=lag)
            rows.append(dict(
                price_id=price_id, fund_id=fund_id, price_date=d.strftime("%Y-%m-%d"),
                price=price, prior_price=prior[fund_id],
                independent_source_price=independent_source_price,
                break_identified_date=(break_identified.strftime("%Y-%m-%d") if break_identified else ""),
                break_resolved_date=(resolved.strftime("%Y-%m-%d") if resolved else ""),
            ))
            prior[fund_id] = price
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# 5. CASH MOVEMENTS (Process 5: Cash & Collateral)
# ----------------------------------------------------------------------------
def gen_cash_movements(rng, bdays):
    rows = []
    movement_id = 1100000
    approvers = [f"APPR{n:03d}" for n in range(1, 15)]
    for d in bdays:
        n_moves = rng.poisson(15)
        for _ in range(n_moves):
            movement_id += 1
            amount = round(rng.lognormal(mean=11.5, sigma=1.2), 2)  # skewed, some large
            movement_type = rng.choice(["Wire Out", "Wire In", "Collateral Pledge", "Collateral Return"])
            approver_1 = rng.choice(approvers)
            # CM-01: dual approval required above $250k; ~4% missing second approver when required
            needs_dual = amount > 250_000
            if needs_dual and rng.random() < 0.04:
                approver_2 = ""
            elif needs_dual:
                approver_2 = rng.choice([a for a in approvers if a != approver_1])
            else:
                approver_2 = rng.choice([a for a in approvers if a != approver_1]) if rng.random() < 0.5 else ""
            custodian_match = rng.random() > 0.015  # ~1.5% custodian break
            rows.append(dict(
                movement_id=movement_id, account_id=f"ACC{rng.integers(1, 900):05d}",
                movement_type=movement_type, amount=amount, movement_date=d.strftime("%Y-%m-%d"),
                approver_1=approver_1, approver_2=approver_2,
                custodian_match_flag=bool(custodian_match),
            ))
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# 6. PORTFOLIO HOLDINGS & SUITABILITY (Process 6)
# ----------------------------------------------------------------------------
def gen_portfolio_holdings(rng):
    rows = []
    holding_id = 1300000
    n_portfolios = 150
    mandate_types = ["Conservative Income", "Balanced Growth", "Aggressive Growth", "Fixed Income Core"]
    guideline_bands = {
        "Conservative Income": {"Equity": (0, 30), "Bond": (50, 90), "Cash": (0, 20)},
        "Balanced Growth": {"Equity": (40, 65), "Bond": (25, 50), "Cash": (0, 15)},
        "Aggressive Growth": {"Equity": (65, 95), "Bond": (0, 25), "Cash": (0, 10)},
        "Fixed Income Core": {"Equity": (0, 10), "Bond": (80, 100), "Cash": (0, 10)},
    }
    for p in range(1, n_portfolios + 1):
        portfolio_id = f"PORT{p:05d}"
        client_id = f"CLI{rng.integers(1, 4000):05d}"
        mandate = rng.choice(mandate_types)
        bands = guideline_bands[mandate]
        # generate each asset-class weight to normally sit inside its guideline band
        # (this is what "in control" actually looks like), with a deliberate ~7%
        # breach rate per holding so SM-01 exceptions are a minority, not the norm.
        for asset_class in ["Equity", "Bond", "Cash"]:
            holding_id += 1
            gmin, gmax = bands[asset_class]
            band_width = gmax - gmin
            if rng.random() < 0.07:
                # deliberate breach, just outside the band
                weight = gmax + rng.uniform(1, 8) if rng.random() < 0.5 else max(0, gmin - rng.uniform(1, 6))
            else:
                # comfortably inside the band (avoid the exact edges)
                margin = band_width * 0.1
                weight = rng.uniform(gmin + margin, max(gmin + margin + 0.1, gmax - margin))
            rows.append(dict(
                holding_id=holding_id, portfolio_id=portfolio_id, client_id=client_id,
                mandate_type=mandate, asset_class=asset_class, weight_pct=round(weight, 2),
                guideline_min_pct=gmin, guideline_max_pct=gmax,
                as_of_date=END.strftime("%Y-%m-%d"),
            ))
    return pd.DataFrame(rows)


def gen_suitability_reviews(rng, holdings_df):
    rows = []
    review_id = 1500000
    portfolios = holdings_df["portfolio_id"].unique()
    for port in portfolios:
        review_id += 1
        due = END - pd.DateOffset(days=int(rng.integers(0, 365)))
        completed_late = rng.random() < 0.06
        completed = due + timedelta(days=int(rng.integers(5, 40))) if completed_late else due - timedelta(days=int(rng.integers(0, 10)))
        rows.append(dict(
            review_id=review_id, portfolio_id=port,
            review_due_date=due.strftime("%Y-%m-%d"),
            review_completed_date=completed.strftime("%Y-%m-%d"),
            status="Late" if completed_late else "OnTime",
        ))
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# CONTROL CATALOG
# ----------------------------------------------------------------------------
def gen_control_catalog():
    rows = [
        dict(control_id="TC-01", process_id="P1", process_name="Trade Allocation & Execution",
             control_name="Allocation Timeliness", control_type="Detective", frequency="Daily",
             test_procedure="Allocation timestamp must be within 30 minutes of execution timestamp.",
             owner="J. Farrow, Trading Ops", risk_category="Operational", kri_threshold_pass_rate=0.97),
        dict(control_id="TC-02", process_id="P1", process_name="Trade Allocation & Execution",
             control_name="Execution Price Reasonableness", control_type="Detective", frequency="Daily",
             test_procedure="Execution price must be within 15bps of benchmark price.",
             owner="J. Farrow, Trading Ops", risk_category="Operational", kri_threshold_pass_rate=0.98),
        dict(control_id="TC-03", process_id="P1", process_name="Trade Allocation & Execution",
             control_name="Pro-Rata Allocation Fairness", control_type="Detective", frequency="Daily",
             test_procedure="Allocation % across an order block must be within 2% of pro-rata entitlement.",
             owner="J. Farrow, Trading Ops", risk_category="Operational", kri_threshold_pass_rate=0.97),
        dict(control_id="FC-01", process_id="P2", process_name="Fee Billing & Calculation",
             control_name="Fee Recalculation Match", control_type="Detective", frequency="Monthly",
             test_procedure="Billed fee must match independently recalculated fee within 0.5%.",
             owner="M. Cho, Billing Ops", risk_category="Operational", kri_threshold_pass_rate=0.97),
        dict(control_id="FC-02", process_id="P2", process_name="Fee Billing & Calculation",
             control_name="Invoice Timeliness SLA", control_type="Detective", frequency="Monthly",
             test_procedure="Invoice must be issued within 10 business days of period end.",
             owner="M. Cho, Billing Ops", risk_category="Operational", kri_threshold_pass_rate=0.95),
        dict(control_id="KC-01", process_id="P3", process_name="Client Onboarding / KYC-AML Refresh",
             control_name="Refresh SLA Compliance", control_type="Preventive", frequency="Monthly",
             test_procedure="KYC/AML periodic refresh must complete on or before the due date.",
             owner="R. Alavi, Compliance Ops", risk_category="Compliance", kri_threshold_pass_rate=0.96),
        dict(control_id="KC-02", process_id="P3", process_name="Client Onboarding / KYC-AML Refresh",
             control_name="Documentation Completeness", control_type="Preventive", frequency="Monthly",
             test_procedure="All required KYC documents must be present and complete at review closure.",
             owner="R. Alavi, Compliance Ops", risk_category="Compliance", kri_threshold_pass_rate=0.98),
        dict(control_id="NV-01", process_id="P4", process_name="NAV / Valuation Oversight",
             control_name="Price Variance Tolerance", control_type="Detective", frequency="Daily",
             test_procedure="Fund price must be within 25bps of an independent source price.",
             owner="S. Okonjo, Valuation Control", risk_category="Valuation", kri_threshold_pass_rate=0.97),
        dict(control_id="NV-02", process_id="P4", process_name="NAV / Valuation Oversight",
             control_name="Break Resolution SLA", control_type="Detective", frequency="Daily",
             test_procedure="Identified pricing breaks must be resolved within 3 business days.",
             owner="S. Okonjo, Valuation Control", risk_category="Valuation", kri_threshold_pass_rate=0.90),
        dict(control_id="CM-01", process_id="P5", process_name="Cash & Collateral Movements",
             control_name="Dual Approval Threshold", control_type="Preventive", frequency="Daily",
             test_procedure="Movements above $250,000 require two distinct approvers.",
             owner="D. Weiss, Treasury Ops", risk_category="Operational", kri_threshold_pass_rate=0.99),
        dict(control_id="CM-02", process_id="P5", process_name="Cash & Collateral Movements",
             control_name="Custodian Reconciliation Match", control_type="Detective", frequency="Daily",
             test_procedure="Cash/collateral movement must match custodian statement (no unresolved break).",
             owner="D. Weiss, Treasury Ops", risk_category="Operational", kri_threshold_pass_rate=0.98),
        dict(control_id="SM-01", process_id="P6", process_name="Suitability & Mandate Compliance",
             control_name="Guideline Band Compliance", control_type="Detective", frequency="Monthly",
             test_procedure="Portfolio asset-class weight must sit within the mandate's guideline band.",
             owner="A. Petrova, Suitability Oversight", risk_category="Suitability", kri_threshold_pass_rate=0.93),
        dict(control_id="SM-02", process_id="P6", process_name="Suitability & Mandate Compliance",
             control_name="Periodic Suitability Review", control_type="Preventive", frequency="Monthly",
             test_procedure="Portfolio suitability review must complete within the required annual cycle.",
             owner="A. Petrova, Suitability Oversight", risk_category="Suitability", kri_threshold_pass_rate=0.95),
    ]
    return pd.DataFrame(rows)


def main(outdir, seed):
    rng = np.random.default_rng(seed)
    bdays = business_days(START, END)
    os.makedirs(outdir, exist_ok=True)

    print("Generating trades...")
    trades = gen_trades(rng, bdays)
    trades.to_csv(os.path.join(outdir, "trades.csv"), index=False)

    print("Generating fee calculations...")
    fees = gen_fee_calculations(rng)
    fees.to_csv(os.path.join(outdir, "fee_calculations.csv"), index=False)

    print("Generating KYC cases...")
    kyc = gen_kyc_cases(rng)
    kyc.to_csv(os.path.join(outdir, "kyc_cases.csv"), index=False)

    print("Generating NAV prices...")
    nav = gen_nav_prices(rng, bdays)
    nav.to_csv(os.path.join(outdir, "nav_prices.csv"), index=False)

    print("Generating cash movements...")
    cash = gen_cash_movements(rng, bdays)
    cash.to_csv(os.path.join(outdir, "cash_movements.csv"), index=False)

    print("Generating portfolio holdings & suitability reviews...")
    holdings = gen_portfolio_holdings(rng)
    holdings.to_csv(os.path.join(outdir, "portfolio_holdings.csv"), index=False)
    suitability = gen_suitability_reviews(rng, holdings)
    suitability.to_csv(os.path.join(outdir, "suitability_reviews.csv"), index=False)

    print("Generating control catalog...")
    catalog = gen_control_catalog()
    catalog.to_csv(os.path.join(outdir, "control_catalog.csv"), index=False)

    print("\nRow counts:")
    for name, df in [("trades", trades), ("fee_calculations", fees), ("kyc_cases", kyc),
                      ("nav_prices", nav), ("cash_movements", cash),
                      ("portfolio_holdings", holdings), ("suitability_reviews", suitability),
                      ("control_catalog", catalog)]:
        print(f"  {name}: {len(df):,} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", default="../../data/raw")
    parser.add_argument("--seed", type=int, default=RNG_SEED)
    args = parser.parse_args()
    main(args.outdir, args.seed)
