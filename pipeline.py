"""Build public bank profitability, FTP, and scenario-model CSV files.
How to read the code:
1. Configuration dictionaries define the bank universe, SEC metrics, FRED
   series, and scenario assumptions.
2. SEC functions download and normalize company facts into real financials.
3. FRED functions download public rate and macroeconomic data.
4. Ratio and scenario functions calculate the Power BI-ready model outputs.
5. Validation and writer functions save CSV files and print a run summary.

The historical files are real public data. The scenario and FTP files are
modeled estimates calibrated from public SEC EDGAR and FRED inputs.
"""

from __future__ import annotations

import json
import os
import re
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests


# =============================================================================
# Project paths and run settings
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_SEC_DIR = PROJECT_ROOT / "data" / "raw" / "sec"
RAW_FRED_DIR = PROJECT_ROOT / "data" / "raw" / "fred"
CLEAN_DIR = PROJECT_ROOT / "data" / "cleaned"

SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "bank-peer-profitability-benchmark-scenario-model/1.0 contact@example.com",
)
REQUEST_TIMEOUT = 60

BANK_TICKERS = ["JPM", "BAC", "WFC", "C", "USB", "PNC", "TFC", "COF"]
COMMON_ANALYSIS_START_PERIOD = os.getenv("COMMON_ANALYSIS_START_PERIOD", "2018Q2")
COMMON_ANALYSIS_START_DATE = os.getenv("COMMON_ANALYSIS_START_DATE", "2018-04-03")
COMMON_ANALYSIS_END_PERIOD = os.getenv("COMMON_ANALYSIS_END_PERIOD", "")
COMMON_ANALYSIS_END_DATE = os.getenv("COMMON_ANALYSIS_END_DATE", "")
SHORT_RATE_SERIES_ID = os.getenv("SHORT_RATE_SERIES_ID", "SOFR").strip().upper()
ASSET_TRANSFER_RATE_SERIES_ID = os.getenv("ASSET_TRANSFER_RATE_SERIES_ID", "DGS2").strip().upper()

ACCEPTED_SEC_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A"}
QUARTER_FRAME_RE = re.compile(r"^CY(?P<year>\d{4})Q(?P<quarter>[1-4])(?P<instant>I?)$")
ANNUAL_FRAME_RE = re.compile(r"^CY(?P<year>\d{4})$")


# =============================================================================
# SEC metric mapping
# =============================================================================
#
# SEC filers do not always use the exact same XBRL tag for the same business
# idea. Each metric below has a prioritized list of candidate tags. The first
# available comparable tag becomes the source for that metric row.

METRIC_CANDIDATES = {
    "net_income": {
        "kind": "duration",
        "label": "Net income",
        "candidates": [
            ("us-gaap", "NetIncomeLoss", "USD"),
        ],
    },
    "interest_income": {
        "kind": "duration",
        "label": "Interest income",
        "candidates": [
            ("us-gaap", "InterestIncomeOperating", "USD"),
            ("us-gaap", "InterestAndDividendIncomeOperating", "USD"),
        ],
    },
    "interest_expense": {
        "kind": "duration",
        "label": "Interest expense",
        "candidates": [
            ("us-gaap", "InterestExpenseOperating", "USD"),
            ("us-gaap", "InterestExpense", "USD"),
        ],
    },
    "interest_expense_deposits": {
        "kind": "duration",
        "label": "Interest expense on deposits",
        "candidates": [
            ("us-gaap", "InterestExpenseDeposits", "USD"),
        ],
    },
    "noninterest_income": {
        "kind": "duration",
        "label": "Noninterest income",
        "candidates": [
            ("us-gaap", "NoninterestIncome", "USD"),
            ("us-gaap", "NoninterestIncomeOther", "USD"),
        ],
    },
    "noninterest_expense": {
        "kind": "duration",
        "label": "Noninterest expense",
        "candidates": [
            ("us-gaap", "NoninterestExpense", "USD"),
        ],
    },
    "total_assets": {
        "kind": "instant",
        "label": "Total assets",
        "candidates": [
            ("us-gaap", "Assets", "USD"),
        ],
    },
    "total_deposits": {
        "kind": "instant",
        "label": "Total deposits",
        "candidates": [
            ("us-gaap", "Deposits", "USD"),
        ],
    },
    "total_loans": {
        "kind": "instant",
        "label": "Total loans",
        "candidates": [
            ("us-gaap", "FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss", "USD"),
            ("us-gaap", "LoansAndLeasesReceivableNetReportedAmount", "USD"),
            ("us-gaap", "LoansAndLeasesReceivableNetOfDeferredIncome", "USD"),
            ("us-gaap", "LoansReceivableNet", "USD"),
            ("us-gaap", "FinancingReceivableExcludingAccruedInterestBeforeAllowanceForCreditLoss", "USD"),
        ],
    },
    "total_equity": {
        "kind": "instant",
        "label": "Total equity",
        "candidates": [
            ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD"),
            ("us-gaap", "StockholdersEquity", "USD"),
        ],
    },
    "tier_1_capital": {
        "kind": "instant",
        "label": "Tier 1 capital",
        "candidates": [
            ("us-gaap", "TierOneRiskBasedCapital", "USD"),
            ("us-gaap", "TierOneLeverageCapital", "USD"),
            ("us-gaap", "CommonEquityTierOneCapital", "USD"),
        ],
    },
    "tier_1_capital_ratio": {
        "kind": "instant",
        "label": "Tier 1 capital ratio",
        "candidates": [
            ("us-gaap", "TierOneRiskBasedCapitalToRiskWeightedAssets", "pure"),
            ("us-gaap", "TierOneLeverageCapitalToAverageAssets", "pure"),
        ],
    },
}


# =============================================================================
# FRED rate and macro series
# =============================================================================
#
# The pipeline downloads each series as a public CSV from FRED. The aggregation
# field tells the quarterly scenario engine how to convert daily, weekly,
# monthly, or quarterly observations into one value per quarter.

FRED_SERIES = {
    "SOFR": {
        "indicator": "Secured Overnight Financing Rate",
        "category": "market_rate",
        "frequency": "Daily",
        "units": "Percent",
        "aggregation": "mean",
    },
    "DFF": {
        "indicator": "Effective Federal Funds Rate",
        "category": "market_rate",
        "frequency": "Daily",
        "units": "Percent",
        "aggregation": "mean",
    },
    "DPRIME": {
        "indicator": "Bank Prime Loan Rate",
        "category": "market_rate",
        "frequency": "Daily",
        "units": "Percent",
        "aggregation": "mean",
    },
    "DGS3MO": {
        "indicator": "Treasury 3-Month Constant Maturity",
        "category": "market_rate",
        "frequency": "Daily",
        "units": "Percent",
        "aggregation": "mean",
    },
    "DGS2": {
        "indicator": "Treasury 2-Year Constant Maturity",
        "category": "market_rate",
        "frequency": "Daily",
        "units": "Percent",
        "aggregation": "mean",
    },
    "DGS10": {
        "indicator": "Treasury 10-Year Constant Maturity",
        "category": "market_rate",
        "frequency": "Daily",
        "units": "Percent",
        "aggregation": "mean",
    },
    "CPIAUCSL": {
        "indicator": "Consumer Price Index for All Urban Consumers",
        "category": "macro_indicator",
        "frequency": "Monthly",
        "units": "Index 1982-1984=100",
        "aggregation": "mean",
    },
    "UNRATE": {
        "indicator": "Unemployment Rate",
        "category": "macro_indicator",
        "frequency": "Monthly",
        "units": "Percent",
        "aggregation": "mean",
    },
    "GDP": {
        "indicator": "Gross Domestic Product",
        "category": "macro_indicator",
        "frequency": "Quarterly",
        "units": "Billions of dollars",
        "aggregation": "last",
    },
    "DPSACBW027SBOG": {
        "indicator": "Deposits, All Commercial Banks",
        "category": "macro_indicator",
        "frequency": "Weekly",
        "units": "Billions of dollars",
        "aggregation": "mean",
    },
    "TOTLL": {
        "indicator": "Loans and Leases in Bank Credit, All Commercial Banks",
        "category": "macro_indicator",
        "frequency": "Weekly",
        "units": "Billions of dollars",
        "aggregation": "mean",
    },
    "DRALACBN": {
        "indicator": "Delinquency Rate on All Loans, All Commercial Banks",
        "category": "macro_indicator",
        "frequency": "Quarterly",
        "units": "Percent",
        "aggregation": "last",
    },
}


# =============================================================================
# Power BI metric metadata
# =============================================================================

METRIC_DIMENSION = {
    "net_income": {
        "metric_group": "Profitability",
        "financial_statement_area": "Income statement",
        "metric_sort_order": 10,
        "power_bi_format": "$ billions",
    },
    "interest_income": {
        "metric_group": "Net interest income",
        "financial_statement_area": "Income statement",
        "metric_sort_order": 20,
        "power_bi_format": "$ billions",
    },
    "interest_expense": {
        "metric_group": "Net interest income",
        "financial_statement_area": "Income statement",
        "metric_sort_order": 30,
        "power_bi_format": "$ billions",
    },
    "interest_expense_deposits": {
        "metric_group": "Funding cost",
        "financial_statement_area": "Income statement",
        "metric_sort_order": 40,
        "power_bi_format": "$ billions",
    },
    "noninterest_income": {
        "metric_group": "Fee and other income",
        "financial_statement_area": "Income statement",
        "metric_sort_order": 50,
        "power_bi_format": "$ billions",
    },
    "noninterest_expense": {
        "metric_group": "Expense efficiency",
        "financial_statement_area": "Income statement",
        "metric_sort_order": 60,
        "power_bi_format": "$ billions",
    },
    "total_assets": {
        "metric_group": "Balance sheet",
        "financial_statement_area": "Balance sheet",
        "metric_sort_order": 70,
        "power_bi_format": "$ billions",
    },
    "total_deposits": {
        "metric_group": "Balance sheet",
        "financial_statement_area": "Balance sheet",
        "metric_sort_order": 80,
        "power_bi_format": "$ billions",
    },
    "total_loans": {
        "metric_group": "Balance sheet",
        "financial_statement_area": "Balance sheet",
        "metric_sort_order": 90,
        "power_bi_format": "$ billions",
    },
    "total_equity": {
        "metric_group": "Capital",
        "financial_statement_area": "Balance sheet",
        "metric_sort_order": 100,
        "power_bi_format": "$ billions",
    },
    "tier_1_capital": {
        "metric_group": "Capital",
        "financial_statement_area": "Regulatory capital",
        "metric_sort_order": 110,
        "power_bi_format": "$ billions",
    },
    "tier_1_capital_ratio": {
        "metric_group": "Capital",
        "financial_statement_area": "Regulatory capital",
        "metric_sort_order": 120,
        "power_bi_format": "percent",
    },
}


# =============================================================================
# Scenario assumptions
# =============================================================================
#
# These are deterministic teaching/modeling assumptions. They are not bank
# guidance, regulatory stress-test results, or investment recommendations.

SCENARIO_HORIZON_QUARTERS = 8
MODEL_VERSION = "v1_public_calibrated_deterministic"
SCENARIO_DATA_LABEL = (
    "MODELED/SCENARIO DATA - calibrated from public SEC EDGAR and FRED data; "
    "not actual internal bank data"
)
SCENARIO_DEFINITIONS = [
    {
        "scenario_name": "Base",
        "rate_shock_bps": 0,
        "deposit_beta": 0.35,
        "loan_yield_beta": 0.55,
        "expense_growth_annual": 0.030,
        "credit_loss_rate_annual": 0.006,
        "capital_ratio": 0.095,
        "loan_growth_annual": 0.025,
        "deposit_growth_annual": 0.020,
        "noninterest_income_growth_annual": 0.020,
        "tax_rate": 0.210,
        "description": "Base case holds the latest observed short-rate level and applies moderate balance sheet growth.",
    },
    {
        "scenario_name": "Rate Up 100bp",
        "rate_shock_bps": 100,
        "deposit_beta": 0.45,
        "loan_yield_beta": 0.65,
        "expense_growth_annual": 0.033,
        "credit_loss_rate_annual": 0.007,
        "capital_ratio": 0.100,
        "loan_growth_annual": 0.020,
        "deposit_growth_annual": 0.015,
        "noninterest_income_growth_annual": 0.018,
        "tax_rate": 0.210,
        "description": "Parallel upward short-rate shock with higher deposit pass-through and slightly slower growth.",
    },
    {
        "scenario_name": "Rate Down 100bp",
        "rate_shock_bps": -100,
        "deposit_beta": 0.25,
        "loan_yield_beta": 0.45,
        "expense_growth_annual": 0.028,
        "credit_loss_rate_annual": 0.006,
        "capital_ratio": 0.095,
        "loan_growth_annual": 0.030,
        "deposit_growth_annual": 0.025,
        "noninterest_income_growth_annual": 0.022,
        "tax_rate": 0.210,
        "description": "Down-rate case with slower deposit repricing and modestly stronger balance sheet growth.",
    },
    {
        "scenario_name": "Credit Stress",
        "rate_shock_bps": 50,
        "deposit_beta": 0.50,
        "loan_yield_beta": 0.50,
        "expense_growth_annual": 0.045,
        "credit_loss_rate_annual": 0.018,
        "capital_ratio": 0.110,
        "loan_growth_annual": -0.010,
        "deposit_growth_annual": -0.005,
        "noninterest_income_growth_annual": -0.005,
        "tax_rate": 0.210,
        "description": "Credit stress case with higher losses, tighter funding, slower revenue, and a higher capital target.",
    },
]


# =============================================================================
# General helpers
# =============================================================================

def ensure_directories() -> None:
    """Create raw and cleaned data folders if they do not already exist."""
    for path in [RAW_SEC_DIR, RAW_FRED_DIR, CLEAN_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    """Download JSON from a public endpoint and raise an error if it fails."""
    response = requests.get(url, headers=headers or {}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_text(url: str, headers: dict[str, str] | None = None) -> str:
    """Download text, usually a CSV or raw JSON string, from a public endpoint."""
    response = requests.get(url, headers=headers or {}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def write_text_with_retry(path: Path, text: str, attempts: int = 3) -> None:
    """Write a text file with retries for occasional Windows file-handle issues."""
    for attempt in range(1, attempts + 1):
        try:
            path.write_text(text, encoding="utf-8")
            return
        except OSError:
            if attempt == attempts:
                raise
            time.sleep(0.75 * attempt)


def sec_file_headers() -> dict[str, str]:
    """Return SEC-friendly request headers with a project user agent."""
    return {
        "User-Agent": SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }


def period_from_frame(frame: str) -> str | None:
    """Convert SEC frame labels such as CY2025Q2 or CY2025Q2I to 2025Q2."""
    match = QUARTER_FRAME_RE.match(frame or "")
    if not match:
        return None
    return f"{match.group('year')}Q{match.group('quarter')}"


def annual_year_from_frame(frame: str) -> int | None:
    """Return the calendar year from an SEC annual frame such as CY2025."""
    match = ANNUAL_FRAME_RE.match(frame or "")
    if not match:
        return None
    return int(match.group("year"))


def period_end(period: str) -> str:
    """Return the quarter-end date for a YYYYQ# period label."""
    return pd.Period(period, freq="Q").end_time.date().isoformat()


def period_start(period: str) -> str:
    """Return the quarter-start date for a YYYYQ# period label."""
    return pd.Period(period, freq="Q").start_time.date().isoformat()


def period_sort_value(period: str) -> int | None:
    """Return a numeric key such as 20252 so Power BI can sort quarters."""
    match = re.match(r"^(\d{4})Q([1-4])$", str(period))
    if not match:
        return None
    return int(match.group(1)) * 10 + int(match.group(2))


def filter_to_analysis_window(df: pd.DataFrame) -> pd.DataFrame:
    """Limit cleaned outputs to the shared analysis window used in Power BI.

    Start controls are required. End controls are optional. If no end date or
    end period is provided, the pipeline keeps the latest public observations.
    """
    if df.empty:
        return df

    filtered = df.copy()
    if "date" in filtered.columns:
        start_date = pd.Timestamp(COMMON_ANALYSIS_START_DATE)
        filtered["date"] = pd.to_datetime(filtered["date"], errors="coerce")
        filtered = filtered[filtered["date"] >= start_date].copy()
        if COMMON_ANALYSIS_END_DATE:
            end_date = pd.Timestamp(COMMON_ANALYSIS_END_DATE)
            filtered = filtered[filtered["date"] <= end_date].copy()

    if "period" in filtered.columns:
        start_key = period_sort_value(COMMON_ANALYSIS_START_PERIOD)
        period_keys = filtered["period"].map(period_sort_value)
        filtered = filtered[period_keys.notna() & (period_keys >= start_key)].copy()
        if COMMON_ANALYSIS_END_PERIOD:
            end_key = period_sort_value(COMMON_ANALYSIS_END_PERIOD)
            period_keys = filtered["period"].map(period_sort_value)
            filtered = filtered[period_keys.notna() & (period_keys <= end_key)].copy()

    return filtered.reset_index(drop=True)


def add_period_sort(df: pd.DataFrame) -> pd.DataFrame:
    """Add a reusable chronological sort key for period labels."""
    if "period" not in df.columns:
        return df
    enriched = df.copy()
    enriched["period_sort"] = enriched["period"].map(period_sort_value)
    return enriched


def next_periods_after(period: str, count: int) -> list[str]:
    """Return the next count quarterly period labels after the baseline period."""
    start = pd.Period(period, freq="Q")
    return [str(start + step).replace("Q", "Q") for step in range(1, count + 1)]


def clean_numeric(series: pd.Series) -> pd.Series:
    """Convert a pandas Series to numeric while keeping bad values as NaN."""
    return pd.to_numeric(series, errors="coerce")


def safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Divide while treating zero denominators as missing values."""
    denominator = denominator.where(denominator.abs() > 0)
    return numerator / denominator


def clip_value(value: float, low: float, high: float) -> float:
    """Constrain a model assumption to a reasonable range."""
    if pd.isna(value):
        return value
    return max(low, min(high, float(value)))


def latest_valid(series: pd.Series, default: float | None = None) -> float | None:
    """Return the latest non-null value in a Series."""
    values = series.dropna()
    if values.empty:
        return default
    return float(values.iloc[-1])


def median_valid(series: pd.Series, default: float | None = None) -> float | None:
    """Return the median non-null value in a Series."""
    values = series.dropna()
    if values.empty:
        return default
    return float(values.median())


def quarter_growth(annual_growth: float) -> float:
    """Convert an annual growth assumption into a quarterly growth rate."""
    return (1.0 + annual_growth) ** 0.25 - 1.0


# =============================================================================
# SEC EDGAR download and normalization
# =============================================================================

def download_company_tickers() -> pd.DataFrame:
    """Download the SEC ticker-to-CIK lookup table."""
    url = "https://www.sec.gov/files/company_tickers.json"
    text = get_text(url, headers=sec_file_headers())
    write_text_with_retry(RAW_SEC_DIR / "company_tickers.json", text)
    payload = json.loads(text)
    rows = list(payload.values())
    df = pd.DataFrame(rows)
    df["ticker"] = df["ticker"].str.upper()
    df["cik"] = df["cik_str"].astype(int).astype(str).str.zfill(10)
    return df[["ticker", "title", "cik"]]


def download_company_facts(ticker: str, cik: str) -> dict:
    """Download one bank's SEC Company Facts JSON and save the raw snapshot."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    payload = get_json(url, headers=sec_file_headers())
    write_text_with_retry(RAW_SEC_DIR / f"{ticker}_companyfacts.json", json.dumps(payload, indent=2))
    return payload


def rows_for_concept(
    facts_payload: dict,
    metric: str,
    metric_label: str,
    kind: str,
    namespace: str,
    concept: str,
    expected_unit: str,
    priority: int,
) -> pd.DataFrame:
    """Extract one SEC XBRL concept into normalized bank-metric rows.

    The output is still long-form. Each row keeps source details such as the
    XBRL concept, form type, filing date, and accession number for auditability.
    """
    concept_payload = facts_payload.get("facts", {}).get(namespace, {}).get(concept)
    if not concept_payload:
        return pd.DataFrame()

    units = concept_payload.get("units", {})
    if expected_unit not in units:
        return pd.DataFrame()

    rows: list[dict] = []
    for fact in units[expected_unit]:
        form = fact.get("form")
        frame = fact.get("frame")
        if form not in ACCEPTED_SEC_FORMS:
            continue
        if fact.get("val") is None:
            continue

        frame_period = period_from_frame(frame or "")
        annual_year = annual_year_from_frame(frame or "")

        if kind == "instant":
            if not frame_period or not str(frame).endswith("I"):
                continue
            period_source = "reported_instant_frame"
            period = frame_period
        else:
            if frame_period and not str(frame).endswith("I"):
                period_source = "reported_quarterly_frame"
                period = frame_period
            elif annual_year:
                period_source = "reported_annual_frame"
                period = f"{annual_year}"
            else:
                continue

        rows.append(
            {
                "metric": metric,
                "metric_label": metric_label,
                "period": period,
                "period_end": fact.get("end"),
                "value": fact.get("val"),
                "unit": expected_unit,
                "source_namespace": namespace,
                "source_concept": concept,
                "source_form": form,
                "source_filed": fact.get("filed"),
                "source_frame": frame,
                "source_accession": fact.get("accn"),
                "period_source": period_source,
                "candidate_priority": priority,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["value"] = clean_numeric(df["value"])
    df["source_filed"] = pd.to_datetime(df["source_filed"], errors="coerce")
    return df


def dedupe_candidate_rows(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Keep the most recently filed row for each set of business keys."""
    if df.empty:
        return df
    return (
        df.sort_values(keys + ["source_filed"], ascending=[True] * len(keys) + [False])
        .drop_duplicates(keys, keep="first")
        .reset_index(drop=True)
    )


def add_derived_q4_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Derive missing Q4 duration values from annual less Q1-Q3 when possible."""
    if df.empty:
        return df

    direct = df[df["period_source"] == "reported_quarterly_frame"].copy()
    annual = df[df["period_source"] == "reported_annual_frame"].copy()
    if direct.empty or annual.empty:
        return direct

    direct = dedupe_candidate_rows(
        direct,
        ["metric", "period", "source_namespace", "source_concept", "unit"],
    )
    annual = dedupe_candidate_rows(
        annual,
        ["metric", "period", "source_namespace", "source_concept", "unit"],
    )

    derived_rows: list[dict] = []
    for _, annual_row in annual.iterrows():
        year = str(annual_row["period"])
        q_periods = [f"{year}Q1", f"{year}Q2", f"{year}Q3"]
        q4_period = f"{year}Q4"
        already_has_q4 = (
            direct[
                (direct["period"] == q4_period)
                & (direct["source_namespace"] == annual_row["source_namespace"])
                & (direct["source_concept"] == annual_row["source_concept"])
            ]
            .dropna(subset=["value"])
            .shape[0]
            > 0
        )
        if already_has_q4:
            continue

        q_rows = direct[
            (direct["period"].isin(q_periods))
            & (direct["source_namespace"] == annual_row["source_namespace"])
            & (direct["source_concept"] == annual_row["source_concept"])
        ]
        if q_rows["period"].nunique() != 3:
            continue

        derived = annual_row.to_dict()
        derived["period"] = q4_period
        derived["period_end"] = f"{year}-12-31"
        derived["value"] = float(annual_row["value"]) - float(q_rows["value"].sum())
        derived["source_frame"] = f"CY{year}Q4_DERIVED"
        derived["source_form"] = "10-K-derived"
        derived["period_source"] = "derived_q4_from_reported_fy_less_q1_q3"
        derived_rows.append(derived)

    if derived_rows:
        direct = pd.concat([direct, pd.DataFrame(derived_rows)], ignore_index=True)

    return direct


def extract_metric(facts_payload: dict, metric: str, config: dict) -> pd.DataFrame:
    """Try all configured XBRL candidates for one normalized financial metric."""
    frames: list[pd.DataFrame] = []
    for priority, (namespace, concept, unit) in enumerate(config["candidates"], start=1):
        concept_rows = rows_for_concept(
            facts_payload=facts_payload,
            metric=metric,
            metric_label=config["label"],
            kind=config["kind"],
            namespace=namespace,
            concept=concept,
            expected_unit=unit,
            priority=priority,
        )
        if concept_rows.empty:
            continue
        if config["kind"] == "duration":
            concept_rows = add_derived_q4_rows(concept_rows)
        frames.append(concept_rows)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = (
        combined.sort_values(
            ["metric", "period", "candidate_priority", "source_filed"],
            ascending=[True, True, True, False],
        )
        .drop_duplicates(["metric", "period"], keep="first")
        .reset_index(drop=True)
    )
    return combined


def build_real_financials() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build the historical SEC outputs: long table, wide table, and ratios."""
    ticker_df = download_company_tickers()
    target_banks = ticker_df[ticker_df["ticker"].isin(BANK_TICKERS)].copy()
    missing = sorted(set(BANK_TICKERS) - set(target_banks["ticker"]))
    if missing:
        raise ValueError(f"Missing SEC ticker metadata for: {missing}")

    all_rows: list[pd.DataFrame] = []
    for ticker in BANK_TICKERS:
        bank_row = target_banks[target_banks["ticker"] == ticker].iloc[0]
        cik = bank_row["cik"]
        bank_name = bank_row["title"]
        print(f"Fetching SEC company facts for {ticker} ({cik})...")
        facts_payload = download_company_facts(ticker, cik)
        time.sleep(0.15)

        bank_metric_rows: list[pd.DataFrame] = []
        for metric, config in METRIC_CANDIDATES.items():
            metric_df = extract_metric(facts_payload, metric, config)
            if metric_df.empty:
                continue
            metric_df.insert(0, "cik", cik)
            metric_df.insert(0, "bank_name", bank_name)
            metric_df.insert(0, "bank_ticker", ticker)
            bank_metric_rows.append(metric_df)

        if bank_metric_rows:
            all_rows.append(pd.concat(bank_metric_rows, ignore_index=True))

    real_long = pd.concat(all_rows, ignore_index=True)
    real_long["period_end"] = real_long["period"].map(period_end)
    real_long["source_filed"] = pd.to_datetime(real_long["source_filed"], errors="coerce").dt.date
    real_long["data_type"] = "real_public_sec_edgar"
    real_long = real_long[
        [
            "bank_ticker",
            "bank_name",
            "cik",
            "period",
            "period_end",
            "metric",
            "metric_label",
            "value",
            "unit",
            "source_namespace",
            "source_concept",
            "source_form",
            "source_filed",
            "source_frame",
            "source_accession",
            "period_source",
            "data_type",
        ]
    ].sort_values(["bank_ticker", "period", "metric"])

    real_wide = build_real_wide(real_long)
    real_ratios = build_real_ratios(real_wide)
    return real_long, real_wide, real_ratios


def build_real_wide(real_long: pd.DataFrame) -> pd.DataFrame:
    """Pivot SEC long-form metric rows into one row per bank and quarter."""
    values = real_long.pivot_table(
        index=["bank_ticker", "bank_name", "cik", "period", "period_end"],
        columns="metric",
        values="value",
        aggfunc="first",
    ).reset_index()
    values.columns.name = None

    source_concepts = real_long.pivot_table(
        index=["bank_ticker", "period"],
        columns="metric",
        values="source_concept",
        aggfunc="first",
    ).reset_index()
    source_concepts.columns = [
        f"{column}_source_concept" if column not in {"bank_ticker", "period"} else column
        for column in source_concepts.columns
    ]

    for metric in METRIC_CANDIDATES:
        if metric not in values.columns:
            values[metric] = pd.NA

    wide = values.merge(source_concepts, on=["bank_ticker", "period"], how="left")
    wide["data_type"] = "real_public_sec_edgar_wide"
    return wide.sort_values(["bank_ticker", "period"]).reset_index(drop=True)


def build_real_ratios(real_wide: pd.DataFrame) -> pd.DataFrame:
    """Calculate historical profitability, funding, capital, and efficiency ratios."""
    ratios = real_wide[
        ["bank_ticker", "bank_name", "cik", "period", "period_end"]
    ].copy()

    for column in METRIC_CANDIDATES:
        if column not in real_wide.columns:
            real_wide[column] = pd.NA
        real_wide[column] = clean_numeric(real_wide[column])

    ratios["deposits_to_assets"] = safe_divide(real_wide["total_deposits"], real_wide["total_assets"])
    ratios["loans_to_deposits"] = safe_divide(real_wide["total_loans"], real_wide["total_deposits"])
    ratios["interest_expense_to_deposits_annualized"] = safe_divide(
        real_wide["interest_expense"] * 4, real_wide["total_deposits"]
    )
    ratios["deposit_interest_expense_to_deposits_annualized"] = safe_divide(
        real_wide["interest_expense_deposits"] * 4, real_wide["total_deposits"]
    )
    ratios["net_income_to_assets_annualized"] = safe_divide(
        real_wide["net_income"] * 4, real_wide["total_assets"]
    )
    ratios["noninterest_expense_to_assets_annualized"] = safe_divide(
        real_wide["noninterest_expense"] * 4, real_wide["total_assets"]
    )
    ratios["noninterest_income_to_assets_annualized"] = safe_divide(
        real_wide["noninterest_income"] * 4, real_wide["total_assets"]
    )
    ratios["equity_to_assets"] = safe_divide(real_wide["total_equity"], real_wide["total_assets"])
    ratios["roa_annualized"] = ratios["net_income_to_assets_annualized"]
    ratios["roe_annualized"] = safe_divide(real_wide["net_income"] * 4, real_wide["total_equity"])
    ratios["net_interest_margin_proxy"] = safe_divide(
        (real_wide["interest_income"] - real_wide["interest_expense"]) * 4,
        real_wide["total_assets"],
    )
    ratios["loan_yield_annualized"] = safe_divide(
        real_wide["interest_income"] * 4, real_wide["total_loans"]
    )
    revenue_denominator = (
        real_wide["interest_income"] - real_wide["interest_expense"] + real_wide["noninterest_income"]
    )
    ratios["efficiency_ratio"] = safe_divide(real_wide["noninterest_expense"], revenue_denominator)
    ratios["tier_1_capital_ratio"] = clean_numeric(real_wide["tier_1_capital_ratio"])
    ratios = ratios.replace([float("inf"), -float("inf")], pd.NA)
    ratios["data_type"] = "real_public_sec_edgar_ratio"
    return ratios.sort_values(["bank_ticker", "period"]).reset_index(drop=True)


# =============================================================================
# FRED download and quarterly aggregation
# =============================================================================

def download_fred_series(series_id: str, config: dict) -> pd.DataFrame:
    """Download one public FRED series and return a cleaned long-form table."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    text = get_text(url)
    write_text_with_retry(RAW_FRED_DIR / f"{series_id}.csv", text)
    df = pd.read_csv(StringIO(text))
    df = df.rename(columns={"observation_date": "date", series_id: "value"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = clean_numeric(df["value"])
    df = df.dropna(subset=["date", "value"]).copy()
    df["period"] = df["date"].dt.to_period("Q").astype(str)
    df["series_id"] = series_id
    df["indicator"] = config["indicator"]
    df["category"] = config["category"]
    df["frequency"] = config["frequency"]
    df["units"] = config["units"]
    df["source_url"] = f"https://fred.stlouisfed.org/series/{series_id}"
    df["data_type"] = "real_public_fred"
    return df[
        [
            "date",
            "period",
            "series_id",
            "indicator",
            "category",
            "frequency",
            "value",
            "units",
            "source_url",
            "data_type",
        ]
    ]


def build_fred_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build market-rate, macro-indicator, and quarterly FRED tables."""
    frames = []
    for series_id, config in FRED_SERIES.items():
        print(f"Fetching FRED series {series_id}...")
        frames.append(download_fred_series(series_id, config))
        time.sleep(0.05)

    fred_long = pd.concat(frames, ignore_index=True)
    market_rates = fred_long[fred_long["category"] == "market_rate"].copy()
    macro_indicators = fred_long[fred_long["category"] == "macro_indicator"].copy()
    quarterly = build_quarterly_fred(fred_long)
    return market_rates, macro_indicators, quarterly


def build_quarterly_fred(fred_long: pd.DataFrame) -> pd.DataFrame:
    """Aggregate FRED observations to quarter-level values for scenario modeling."""
    rows: list[dict] = []
    for series_id, series_df in fred_long.groupby("series_id"):
        config = FRED_SERIES[series_id]
        for period, period_df in series_df.groupby("period"):
            if config["aggregation"] == "last":
                chosen = period_df.sort_values("date").iloc[-1]
                value = float(chosen["value"])
                date = chosen["date"]
            else:
                value = float(period_df["value"].mean())
                date = period_df["date"].max()
            rows.append(
                {
                    "period": period,
                    "period_end": period_end(period),
                    "series_id": series_id,
                    "indicator": config["indicator"],
                    "category": config["category"],
                    "quarterly_value": value,
                    "units": config["units"],
                    "aggregation": config["aggregation"],
                    "latest_observation_in_period": date.date().isoformat(),
                }
            )
    return pd.DataFrame(rows)


def build_scenario_assumptions() -> pd.DataFrame:
    """Convert scenario definitions into a clean assumption table."""
    df = pd.DataFrame(SCENARIO_DEFINITIONS)
    df["rate_shock_decimal"] = df["rate_shock_bps"] / 10000.0
    df["data_type"] = "modeled_scenario_assumption"
    return df


# =============================================================================
# Power BI dimension tables
# =============================================================================

def build_dim_bank(real_wide: pd.DataFrame, scenario_model: pd.DataFrame) -> pd.DataFrame:
    """Create one row per bank for Power BI relationships and slicers."""
    frames = [
        real_wide[["bank_ticker", "bank_name", "cik"]],
        scenario_model[["bank_ticker", "bank_name", "cik"]],
    ]
    dim = pd.concat(frames, ignore_index=True).drop_duplicates("bank_ticker")
    dim["bank_sort_order"] = dim["bank_ticker"].map(
        {ticker: idx for idx, ticker in enumerate(BANK_TICKERS, start=1)}
    )
    return dim.sort_values("bank_sort_order").reset_index(drop=True)


def build_dim_metric(real_long: pd.DataFrame) -> pd.DataFrame:
    """Create one row per SEC metric, with grouping and display metadata."""
    dim = real_long[["metric", "metric_label", "unit"]].drop_duplicates("metric").copy()
    dim["metric_group"] = dim["metric"].map(
        lambda metric: METRIC_DIMENSION.get(metric, {}).get("metric_group", "Other")
    )
    dim["financial_statement_area"] = dim["metric"].map(
        lambda metric: METRIC_DIMENSION.get(metric, {}).get("financial_statement_area", "Other")
    )
    dim["metric_sort_order"] = dim["metric"].map(
        lambda metric: METRIC_DIMENSION.get(metric, {}).get("metric_sort_order", 999)
    )
    dim["power_bi_format"] = dim["metric"].map(
        lambda metric: METRIC_DIMENSION.get(metric, {}).get("power_bi_format", "number")
    )
    dim["data_type"] = "dimension_metric"
    return dim.sort_values("metric_sort_order").reset_index(drop=True)


def build_dim_quarter(period_tables: list[pd.DataFrame]) -> pd.DataFrame:
    """Create a unique quarter dimension from every output with a period column."""
    periods = pd.concat(
        [table[["period"]] for table in period_tables if "period" in table.columns],
        ignore_index=True,
    )
    periods["period"] = periods["period"].astype(str).str.strip()
    periods = periods[periods["period"].map(period_sort_value).notna()].drop_duplicates("period")
    periods["year"] = periods["period"].str[:4].astype(int)
    periods["quarter_number"] = periods["period"].str[-1].astype(int)
    periods["quarter_label"] = periods["period"]
    periods["quarter_sort"] = periods["period"].map(period_sort_value).astype(int)
    periods["period_start"] = periods["period"].map(period_start)
    periods["period_end"] = periods["period"].map(period_end)
    periods["data_type"] = "dimension_quarter"
    return periods.sort_values("quarter_sort").reset_index(drop=True)


def build_dim_scenario(assumptions: pd.DataFrame) -> pd.DataFrame:
    """Create one row per scenario for filtering scenario model outputs."""
    columns = [
        "scenario_name",
        "rate_shock_bps",
        "deposit_beta",
        "loan_yield_beta",
        "expense_growth_annual",
        "credit_loss_rate_annual",
        "capital_ratio",
        "description",
    ]
    dim = assumptions[columns].copy()
    dim["scenario_sort_order"] = dim["scenario_name"].map(
        {row["scenario_name"]: idx for idx, row in enumerate(SCENARIO_DEFINITIONS, start=1)}
    )
    dim["data_type"] = "dimension_scenario"
    return dim.sort_values("scenario_sort_order").reset_index(drop=True)


def build_dim_rate_series(market_rates: pd.DataFrame) -> pd.DataFrame:
    """Create one row per market-rate series for rate charts and slicers."""
    columns = ["series_id", "indicator", "category", "frequency", "units", "source_url"]
    dim = market_rates[columns].drop_duplicates("series_id").copy()
    dim["rate_series_sort_order"] = dim["series_id"].map(
        {series_id: idx for idx, series_id in enumerate(FRED_SERIES.keys(), start=1)}
    )
    dim["data_type"] = "dimension_rate_series"
    return dim.sort_values("rate_series_sort_order").reset_index(drop=True)


def latest_quarterly_rate(quarterly_fred: pd.DataFrame, series_id: str, default_percent: float) -> float:
    """Return the latest quarterly FRED rate as a decimal, not a percent.

    A bad or unavailable series should fail loudly because it changes the whole
    scenario model. The default is only a last-resort fallback if the series is
    configured but has no usable data.
    """
    if series_id not in FRED_SERIES:
        available = ", ".join(FRED_SERIES)
        raise ValueError(f"Unknown FRED series '{series_id}'. Available series: {available}")
    if FRED_SERIES[series_id]["category"] != "market_rate":
        market_rates = ", ".join(
            series for series, config in FRED_SERIES.items() if config["category"] == "market_rate"
        )
        raise ValueError(
            f"FRED series '{series_id}' is not a market-rate series. "
            f"Use one of these for scenario rate anchors: {market_rates}"
        )

    subset = quarterly_fred[
        (quarterly_fred["series_id"] == series_id) & quarterly_fred["quarterly_value"].notna()
    ].sort_values("period")
    if subset.empty:
        raise ValueError(
            f"No usable quarterly FRED observations found for '{series_id}'. "
            f"Expected a rate series for the scenario model. Fallback would have been {default_percent}%."
        )
    return float(subset.iloc[-1]["quarterly_value"]) / 100.0


# =============================================================================
# Scenario and FTP model
# =============================================================================

def build_bank_baselines(real_wide: pd.DataFrame, real_ratios: pd.DataFrame) -> pd.DataFrame:
    """Create one public-data baseline row per bank for the scenario model.

    The baseline combines the latest SEC balance-sheet values with recent
    historical ratios. If a bank is missing a specific field, the function uses
    that bank's recent ratios or peer medians rather than hard-coded guesses.
    """
    ratio_cols = [
        "deposits_to_assets",
        "loans_to_deposits",
        "interest_expense_to_deposits_annualized",
        "deposit_interest_expense_to_deposits_annualized",
        "net_income_to_assets_annualized",
        "noninterest_expense_to_assets_annualized",
        "noninterest_income_to_assets_annualized",
        "equity_to_assets",
        "loan_yield_annualized",
        "tier_1_capital_ratio",
    ]

    ratio_work = real_ratios.copy()
    for column in ratio_cols:
        ratio_work[column] = clean_numeric(ratio_work[column])

    peer_defaults = {
        "deposits_to_assets": clip_value(median_valid(ratio_work["deposits_to_assets"], 0.60), 0.20, 0.95),
        "loans_to_deposits": clip_value(median_valid(ratio_work["loans_to_deposits"], 0.65), 0.15, 1.50),
        "funding_cost": clip_value(
            median_valid(ratio_work["deposit_interest_expense_to_deposits_annualized"], 0.025),
            0.000,
            0.120,
        ),
        "loan_yield": clip_value(median_valid(ratio_work["loan_yield_annualized"], 0.065), 0.010, 0.250),
        "noninterest_income_ratio": clip_value(
            median_valid(ratio_work["noninterest_income_to_assets_annualized"], 0.020),
            -0.050,
            0.120,
        ),
        "noninterest_expense_ratio": clip_value(
            median_valid(ratio_work["noninterest_expense_to_assets_annualized"], 0.030),
            0.000,
            0.150,
        ),
        "equity_to_assets": clip_value(median_valid(ratio_work["equity_to_assets"], 0.085), 0.030, 0.250),
        "capital_ratio": clip_value(median_valid(ratio_work["tier_1_capital_ratio"], 0.110), 0.060, 0.250),
    }

    baselines: list[dict] = []
    for ticker, bank_wide in real_wide.sort_values("period").groupby("bank_ticker"):
        bank_ratios = ratio_work[ratio_work["bank_ticker"] == ticker].sort_values("period")
        latest = bank_wide.sort_values("period").iloc[-1]
        recent_ratios = bank_ratios.tail(12)

        deposit_asset_ratio = clip_value(
            median_valid(recent_ratios["deposits_to_assets"], peer_defaults["deposits_to_assets"]),
            0.20,
            0.95,
        )
        loan_deposit_ratio = clip_value(
            median_valid(recent_ratios["loans_to_deposits"], peer_defaults["loans_to_deposits"]),
            0.15,
            1.50,
        )
        funding_cost = median_valid(
            recent_ratios["deposit_interest_expense_to_deposits_annualized"],
            None,
        )
        if funding_cost is None or pd.isna(funding_cost):
            funding_cost = median_valid(
                recent_ratios["interest_expense_to_deposits_annualized"],
                peer_defaults["funding_cost"],
            )
        funding_cost = clip_value(funding_cost, 0.000, 0.120)

        loan_yield = clip_value(
            median_valid(recent_ratios["loan_yield_annualized"], peer_defaults["loan_yield"]),
            0.010,
            0.250,
        )
        noninterest_income_ratio = clip_value(
            median_valid(
                recent_ratios["noninterest_income_to_assets_annualized"],
                peer_defaults["noninterest_income_ratio"],
            ),
            -0.050,
            0.120,
        )
        noninterest_expense_ratio = clip_value(
            median_valid(
                recent_ratios["noninterest_expense_to_assets_annualized"],
                peer_defaults["noninterest_expense_ratio"],
            ),
            0.000,
            0.150,
        )
        equity_to_assets = clip_value(
            median_valid(recent_ratios["equity_to_assets"], peer_defaults["equity_to_assets"]),
            0.030,
            0.250,
        )
        capital_ratio = clip_value(
            latest_valid(recent_ratios["tier_1_capital_ratio"], peer_defaults["capital_ratio"]),
            0.060,
            0.250,
        )

        total_assets = latest_valid(bank_wide["total_assets"], None)
        total_deposits = latest_valid(bank_wide["total_deposits"], None)
        total_loans = latest_valid(bank_wide["total_loans"], None)
        total_equity = latest_valid(bank_wide["total_equity"], None)

        if total_assets is None:
            continue
        if total_deposits is None:
            total_deposits = total_assets * deposit_asset_ratio
        if total_loans is None:
            total_loans = total_deposits * loan_deposit_ratio
        if total_equity is None:
            total_equity = total_assets * equity_to_assets

        latest_interest_income = latest_valid(bank_wide["interest_income"], 0.0)
        latest_interest_expense = latest_valid(bank_wide["interest_expense"], None)
        if latest_interest_expense is None:
            latest_interest_expense = latest_valid(bank_wide["interest_expense_deposits"], 0.0)
        latest_noninterest_income = latest_valid(bank_wide["noninterest_income"], None)
        latest_noninterest_expense = latest_valid(bank_wide["noninterest_expense"], None)
        latest_net_income = latest_valid(bank_wide["net_income"], None)

        baselines.append(
            {
                "bank_ticker": ticker,
                "bank_name": latest["bank_name"],
                "cik": latest["cik"],
                "baseline_period": latest["period"],
                "baseline_period_end": latest["period_end"],
                "baseline_assets": float(total_assets),
                "baseline_deposits": float(total_deposits),
                "baseline_loans": float(total_loans),
                "baseline_equity": float(total_equity),
                "baseline_interest_income": float(latest_interest_income or 0.0),
                "baseline_interest_expense": float(latest_interest_expense or 0.0),
                "baseline_noninterest_income": float(latest_noninterest_income or 0.0),
                "baseline_noninterest_expense": float(latest_noninterest_expense or 0.0),
                "baseline_net_income": float(latest_net_income or 0.0),
                "deposit_asset_ratio": float(deposit_asset_ratio),
                "loan_deposit_ratio": float(loan_deposit_ratio),
                "funding_cost_annualized": float(funding_cost),
                "loan_yield_annualized": float(loan_yield),
                "noninterest_income_to_assets_annualized": float(noninterest_income_ratio),
                "noninterest_expense_to_assets_annualized": float(noninterest_expense_ratio),
                "equity_to_assets": float(equity_to_assets),
                "capital_ratio_anchor": float(capital_ratio),
            }
        )

    return pd.DataFrame(baselines)


def calculate_baseline_nii_adjustment(
    base: pd.Series,
    loans: float,
    deposits: float,
    assets: float,
    latest_short_rate: float,
    latest_asset_transfer_rate: float,
) -> float:
    """Calibrate modeled NII back to the latest public SEC reported NII.

    The scenario model estimates NII from loans, deposits, securities, and
    wholesale funding. This adjustment keeps the starting point anchored to the
    bank's real public baseline instead of letting the proxy formula drift away.
    """
    base_funding_cost = float(base["funding_cost_annualized"])
    base_loan_yield = float(base["loan_yield_annualized"])
    base_nonloan_assets = max(0.0, assets - loans)
    base_wholesale_funding = max(0.0, loans - deposits)
    baseline_modeled_nii = (
        loans * base_loan_yield / 4.0
        - deposits * base_funding_cost / 4.0
        + base_nonloan_assets * (latest_asset_transfer_rate + 0.002) / 4.0
        - base_wholesale_funding * (latest_short_rate + 0.005) / 4.0
    )
    actual_nii = float(base["baseline_interest_income"]) - float(base["baseline_interest_expense"])
    return actual_nii - baseline_modeled_nii


def calculate_ftp_attribution(
    loans: float,
    deposits: float,
    loan_yield: float,
    funding_cost: float,
    ftp_asset_transfer_rate: float,
    ftp_deposit_crediting_rate: float,
) -> dict[str, float]:
    """Calculate the modeled Funds Transfer Pricing attribution fields.

    Loans receive an internal funding charge. Deposits receive an internal
    funding credit. The spread income fields separate customer loan economics
    from deposit funding value.
    """
    ftp_loan_charge = loans * ftp_asset_transfer_rate / 4.0
    ftp_deposit_credit = deposits * ftp_deposit_crediting_rate / 4.0
    customer_loan_interest_income = loans * loan_yield / 4.0
    customer_deposit_interest_expense = deposits * funding_cost / 4.0
    ftp_loan_spread_income = customer_loan_interest_income - ftp_loan_charge
    ftp_deposit_spread_income = ftp_deposit_credit - customer_deposit_interest_expense

    return {
        "ftp_loan_charge": ftp_loan_charge,
        "ftp_deposit_credit": ftp_deposit_credit,
        "ftp_customer_loan_spread": loan_yield - ftp_asset_transfer_rate,
        "ftp_deposit_spread": ftp_deposit_crediting_rate - funding_cost,
        "ftp_loan_spread_income": ftp_loan_spread_income,
        "ftp_deposit_spread_income": ftp_deposit_spread_income,
        "ftp_net_spread_income": ftp_loan_spread_income + ftp_deposit_spread_income,
    }


def calculate_rate_sensitivity_per_100bp(
    loans: float,
    deposits: float,
    nonloan_assets: float,
    wholesale_funding: float,
    loan_yield_beta: float,
    deposit_beta: float,
) -> float:
    """Estimate quarterly NII sensitivity to a 100bp parallel rate shock."""
    return (
        0.01
        * (
            loans * loan_yield_beta
            - deposits * deposit_beta
            + nonloan_assets * 0.35
            - wholesale_funding
        )
        / 4.0
    )


def build_scenario_model(
    real_wide: pd.DataFrame,
    real_ratios: pd.DataFrame,
    quarterly_fred: pd.DataFrame,
    assumptions: pd.DataFrame,
) -> pd.DataFrame:
    """Build modeled bank-quarter-scenario rows calibrated from public data."""
    baselines = build_bank_baselines(real_wide, real_ratios)
    latest_short_rate = latest_quarterly_rate(quarterly_fred, SHORT_RATE_SERIES_ID, 4.0)
    latest_asset_transfer_rate = latest_quarterly_rate(
        quarterly_fred,
        ASSET_TRANSFER_RATE_SERIES_ID,
        4.0,
    )

    rows: list[dict] = []

    for _, base in baselines.iterrows():
        future_periods = next_periods_after(base["baseline_period"], SCENARIO_HORIZON_QUARTERS)
        for _, assumption in assumptions.iterrows():
            loans = float(base["baseline_loans"])
            deposits = float(base["baseline_deposits"])
            assets = float(base["baseline_assets"])
            equity = float(base["baseline_equity"])

            shock = float(assumption["rate_shock_decimal"])
            scenario_short_rate = max(0.0, latest_short_rate + shock)
            deposit_beta = float(assumption["deposit_beta"])
            loan_yield_beta = float(assumption["loan_yield_beta"])
            loan_growth_q = quarter_growth(float(assumption["loan_growth_annual"]))
            deposit_growth_q = quarter_growth(float(assumption["deposit_growth_annual"]))
            asset_growth_q = (loan_growth_q + deposit_growth_q) / 2.0
            expense_growth_q = quarter_growth(float(assumption["expense_growth_annual"]))
            noninterest_income_growth_q = quarter_growth(
                float(assumption["noninterest_income_growth_annual"])
            )

            base_funding_cost = float(base["funding_cost_annualized"])
            base_loan_yield = float(base["loan_yield_annualized"])
            nii_calibration_add = calculate_baseline_nii_adjustment(
                base=base,
                loans=loans,
                deposits=deposits,
                assets=assets,
                latest_short_rate=latest_short_rate,
                latest_asset_transfer_rate=latest_asset_transfer_rate,
            )

            for step, period in enumerate(future_periods, start=1):
                loans *= 1.0 + loan_growth_q
                deposits *= 1.0 + deposit_growth_q
                assets *= 1.0 + asset_growth_q

                funding_gap = loans - deposits
                wholesale_funding = max(0.0, funding_gap)
                nonloan_assets = max(0.0, assets - loans)

                funding_cost = clip_value(
                    base_funding_cost + deposit_beta * (scenario_short_rate - latest_short_rate),
                    0.0,
                    0.20,
                )
                loan_yield = clip_value(
                    base_loan_yield + loan_yield_beta * (scenario_short_rate - latest_short_rate),
                    0.0,
                    0.30,
                )
                securities_yield = clip_value(
                    latest_asset_transfer_rate + 0.002 + 0.35 * shock,
                    0.0,
                    0.20,
                )
                wholesale_funding_cost = clip_value(scenario_short_rate + 0.005, 0.0, 0.20)
                ftp_asset_transfer_rate = clip_value(
                    latest_asset_transfer_rate + 0.005 + 0.60 * shock,
                    0.0,
                    0.20,
                )
                ftp_deposit_crediting_rate = clip_value(
                    scenario_short_rate + 0.002 + 0.35 * shock,
                    0.0,
                    0.20,
                )

                net_interest_income = (
                    loans * loan_yield / 4.0
                    - deposits * funding_cost / 4.0
                    + nonloan_assets * securities_yield / 4.0
                    - wholesale_funding * wholesale_funding_cost / 4.0
                    + nii_calibration_add * (assets / float(base["baseline_assets"]))
                )
                noninterest_income = (
                    assets
                    * float(base["noninterest_income_to_assets_annualized"])
                    / 4.0
                    * ((1.0 + noninterest_income_growth_q) ** step)
                )
                noninterest_expense = (
                    assets
                    * float(base["noninterest_expense_to_assets_annualized"])
                    / 4.0
                    * ((1.0 + expense_growth_q) ** step)
                )
                provision = loans * float(assumption["credit_loss_rate_annual"]) / 4.0
                pretax_income = net_interest_income + noninterest_income - noninterest_expense - provision
                net_income = pretax_income * (1.0 - float(assumption["tax_rate"]))

                required_equity = assets * max(
                    float(assumption["capital_ratio"]),
                    float(base["capital_ratio_anchor"]) * 0.75,
                )
                equity = max(equity + net_income * 0.55, required_equity)

                roa = net_income * 4.0 / assets if assets else pd.NA
                roe = net_income * 4.0 / equity if equity else pd.NA
                total_revenue = net_interest_income + noninterest_income
                efficiency_ratio = noninterest_expense / total_revenue if total_revenue else pd.NA
                capital_ratio = equity / assets if assets else pd.NA

                ftp_metrics = calculate_ftp_attribution(
                    loans=loans,
                    deposits=deposits,
                    loan_yield=loan_yield,
                    funding_cost=funding_cost,
                    ftp_asset_transfer_rate=ftp_asset_transfer_rate,
                    ftp_deposit_crediting_rate=ftp_deposit_crediting_rate,
                )
                ftp_adjusted_net_interest_income = (
                    ftp_metrics["ftp_net_spread_income"]
                    + nonloan_assets * securities_yield / 4.0
                    - wholesale_funding * wholesale_funding_cost / 4.0
                    + nii_calibration_add * (assets / float(base["baseline_assets"]))
                )
                rate_sensitivity_per_100bp = calculate_rate_sensitivity_per_100bp(
                    loans=loans,
                    deposits=deposits,
                    nonloan_assets=nonloan_assets,
                    wholesale_funding=wholesale_funding,
                    loan_yield_beta=loan_yield_beta,
                    deposit_beta=deposit_beta,
                )

                rows.append(
                    {
                        "bank_ticker": base["bank_ticker"],
                        "bank_name": base["bank_name"],
                        "cik": base["cik"],
                        "scenario_name": assumption["scenario_name"],
                        "period": period,
                        "period_end": period_end(period),
                        "forecast_quarter": step,
                        "baseline_period": base["baseline_period"],
                        "baseline_period_end": base["baseline_period_end"],
                        "modeled_assets": assets,
                        "modeled_loan_portfolio": loans,
                        "modeled_deposit_portfolio": deposits,
                        "modeled_funding_cost": funding_cost,
                        "modeled_loan_yield": loan_yield,
                        "modeled_net_interest_income": net_interest_income,
                        "modeled_noninterest_income": noninterest_income,
                        "modeled_noninterest_expense": noninterest_expense,
                        "modeled_provision_credit_loss": provision,
                        "modeled_net_income": net_income,
                        "modeled_equity_capital_base": equity,
                        "modeled_roa": roa,
                        "modeled_roe": roe,
                        "modeled_efficiency_ratio": efficiency_ratio,
                        "modeled_funding_gap": funding_gap,
                        "modeled_funding_gap_to_assets": funding_gap / assets if assets else pd.NA,
                        "modeled_rate_sensitivity": rate_sensitivity_per_100bp,
                        "modeled_capital_ratio": capital_ratio,
                        "ftp_asset_transfer_rate": ftp_asset_transfer_rate,
                        "ftp_deposit_crediting_rate": ftp_deposit_crediting_rate,
                        **ftp_metrics,
                        "ftp_adjusted_net_interest_income": ftp_adjusted_net_interest_income,
                        "ftp_methodology": (
                            "Modeled Funds Transfer Pricing using configurable FRED short-rate "
                            f"({SHORT_RATE_SERIES_ID}) and asset-transfer-rate "
                            f"({ASSET_TRANSFER_RATE_SERIES_ID}) proxies; calibrated from public EDGAR baselines."
                        ),
                        "rate_shock_bps": assumption["rate_shock_bps"],
                        "deposit_beta": deposit_beta,
                        "loan_yield_beta": loan_yield_beta,
                        "expense_growth_annual": assumption["expense_growth_annual"],
                        "credit_loss_rate_annual": assumption["credit_loss_rate_annual"],
                        "capital_ratio_assumption": assumption["capital_ratio"],
                        "scenario_short_rate": scenario_short_rate,
                        "source_rate_series": SHORT_RATE_SERIES_ID,
                        "asset_transfer_rate_series": ASSET_TRANSFER_RATE_SERIES_ID,
                        "data_type": "modeled_scenario_calibrated_from_public_data",
                        "scenario_data_label": SCENARIO_DATA_LABEL,
                        "model_version": MODEL_VERSION,
                    }
                )

    scenario = pd.DataFrame(rows)
    return scenario.sort_values(["bank_ticker", "scenario_name", "period"]).reset_index(drop=True)


# =============================================================================
# Documentation, validation, and output writing
# =============================================================================

def build_data_dictionary(outputs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Create a field-level data dictionary for every cleaned CSV output."""
    definitions = {
        "bank_ticker": "Public ticker symbol for the bank holding company.",
        "bank_name": "SEC company title from company_tickers.json.",
        "cik": "SEC Central Index Key, zero-padded to 10 digits.",
        "bank_sort_order": "Display sort order for the target bank peer group.",
        "period": "Calendar quarter in YYYYQ# format.",
        "period_end": "Calendar quarter end date.",
        "period_sort": "Numeric helper used to sort period labels chronologically in Power BI.",
        "period_start": "Calendar quarter start date.",
        "year": "Calendar year from the period field.",
        "quarter_number": "Calendar quarter number from 1 to 4.",
        "quarter_label": "Display label for the calendar quarter.",
        "quarter_sort": "Numeric helper used to sort quarter labels chronologically.",
        "metric": "Normalized metric name used by the pipeline.",
        "metric_label": "Human-readable metric label.",
        "metric_group": "Business grouping for financial metrics.",
        "financial_statement_area": "Financial statement or analytical area for a metric.",
        "metric_sort_order": "Display sort order for normalized financial metrics.",
        "power_bi_format": "Suggested display format for Power BI visuals.",
        "value": "Metric value as reported or derived from public filings.",
        "unit": "SEC or FRED unit of measure.",
        "source_namespace": "XBRL taxonomy namespace.",
        "source_concept": "XBRL concept used to populate the metric.",
        "source_form": "SEC form used by the selected fact.",
        "source_filed": "SEC filing date for the selected fact.",
        "source_frame": "SEC XBRL frame used for the selected fact.",
        "source_accession": "SEC accession number for the selected fact.",
        "period_source": "Whether the quarter was directly reported or Q4 was derived from FY less Q1-Q3.",
        "data_type": "Label distinguishing real public data, ratios, assumptions, and modeled scenario data.",
        "date": "FRED observation date.",
        "series_id": "FRED series identifier.",
        "indicator": "FRED indicator name.",
        "category": "Market rate or macro indicator category.",
        "frequency": "Native FRED observation frequency.",
        "source_url": "Public source URL.",
        "scenario_name": "Scenario case name.",
        "scenario_sort_order": "Display sort order for scenario names.",
        "forecast_quarter": "Sequential forecast quarter number after the bank baseline period.",
        "baseline_period": "Latest bank public-data quarter used as the model anchor.",
        "baseline_period_end": "End date of the baseline public-data quarter.",
        "modeled_assets": "Modeled total asset base for the scenario quarter.",
        "modeled_loan_portfolio": "Modeled loan portfolio balance calibrated from public loan/deposit history.",
        "modeled_deposit_portfolio": "Modeled deposit portfolio balance calibrated from public deposits/assets history.",
        "modeled_funding_cost": "Annualized modeled funding cost as a decimal.",
        "modeled_loan_yield": "Annualized modeled loan yield as a decimal.",
        "modeled_net_interest_income": "Modeled quarterly net interest income.",
        "modeled_noninterest_income": "Modeled quarterly noninterest income.",
        "modeled_noninterest_expense": "Modeled quarterly noninterest expense.",
        "modeled_provision_credit_loss": "Modeled quarterly provision or credit loss assumption.",
        "modeled_net_income": "Modeled quarterly net income after tax assumption.",
        "modeled_equity_capital_base": "Modeled equity or capital base.",
        "modeled_roa": "Annualized modeled return on assets.",
        "modeled_roe": "Annualized modeled return on equity.",
        "modeled_efficiency_ratio": "Modeled noninterest expense divided by modeled revenue.",
        "modeled_funding_gap": "Modeled loans less modeled deposits.",
        "modeled_funding_gap_to_assets": "Modeled funding gap divided by modeled assets.",
        "modeled_rate_sensitivity": "Estimated quarterly net-interest-income dollar sensitivity to a 100 bp rate shock.",
        "modeled_capital_ratio": "Modeled equity/capital base divided by modeled assets.",
        "ftp_asset_transfer_rate": "Modeled annualized FTP charge rate assigned to loan assets using Treasury 2Y and rate-shock proxies.",
        "ftp_deposit_crediting_rate": "Modeled annualized FTP credit rate assigned to deposit funding using SOFR and rate-shock proxies.",
        "ftp_loan_charge": "Quarterly internal FTP funding charge allocated to modeled loan portfolio.",
        "ftp_deposit_credit": "Quarterly internal FTP funding credit allocated to modeled deposit portfolio.",
        "ftp_customer_loan_spread": "Modeled customer loan yield less FTP asset transfer rate.",
        "ftp_deposit_spread": "Modeled FTP deposit crediting rate less modeled customer funding cost.",
        "ftp_loan_spread_income": "Customer loan interest income less internal FTP loan charge.",
        "ftp_deposit_spread_income": "Internal FTP deposit credit less customer deposit interest expense.",
        "ftp_net_spread_income": "FTP-attributed spread income from lending plus deposits.",
        "ftp_adjusted_net_interest_income": "Modeled NII recast through FTP spread attribution and public-rate curve proxies.",
        "ftp_methodology": "Short description of modeled FTP approach used for scenario rows.",
        "scenario_data_label": "Explicit label that scenario rows are modeled, not actual internal bank data.",
        "source_rate_series": "FRED short-rate series used as the scenario funding-rate anchor.",
        "asset_transfer_rate_series": "FRED rate series used as the FTP asset-transfer-rate anchor.",
        "rate_series_sort_order": "Display sort order for FRED rate series.",
        "check_name": "Validation check identifier.",
        "status": "Validation result status.",
        "severity": "Validation severity.",
        "affected_rows": "Number of rows or cells affected by the validation check.",
        "details": "Human-readable validation result details.",
    }

    rows: list[dict] = []
    for file_name, df in outputs.items():
        for column in df.columns:
            rows.append(
                {
                    "file_name": file_name,
                    "column_name": column,
                    "data_type": str(df[column].dtype),
                    "definition": definitions.get(column, "Pipeline output field used for analysis or validation."),
                }
            )
    return pd.DataFrame(rows).sort_values(["file_name", "column_name"]).reset_index(drop=True)


def add_validation_row(
    rows: list[dict],
    check_name: str,
    file_name: str,
    status: str,
    affected_rows: int,
    details: str,
    severity: str = "info",
) -> None:
    """Append one validation result row to the validation summary list."""
    rows.append(
        {
            "check_name": check_name,
            "file_name": file_name,
            "status": status,
            "severity": severity,
            "affected_rows": affected_rows,
            "details": details,
        }
    )


def build_validation_summary(outputs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Check row counts, missing values, duplicates, and ratio reasonableness."""
    rows: list[dict] = []

    for file_name, df in outputs.items():
        for column in df.columns:
            missing_count = int(df[column].isna().sum())
            add_validation_row(
                rows,
                "missing_values",
                file_name,
                "PASS" if missing_count == 0 else "WARN",
                missing_count,
                f"{column} has {missing_count} missing values.",
                "warning" if missing_count else "info",
            )

    duplicate_specs = {
        "dim_bank.csv": ["bank_ticker"],
        "dim_metric.csv": ["metric"],
        "dim_quarter.csv": ["period"],
        "dim_scenario.csv": ["scenario_name"],
        "dim_rate_series.csv": ["series_id"],
        "bank_financials_real_wide.csv": ["bank_ticker", "period"],
        "bank_ratios_real.csv": ["bank_ticker", "period"],
        "bank_scenario_model.csv": ["bank_ticker", "scenario_name", "period"],
    }
    for file_name, keys in duplicate_specs.items():
        df = outputs[file_name]
        duplicate_count = int(df.duplicated(keys).sum())
        add_validation_row(
            rows,
            "duplicate_key_rows",
            file_name,
            "PASS" if duplicate_count == 0 else "FAIL",
            duplicate_count,
            f"Duplicate rows on keys {keys}.",
            "error" if duplicate_count else "info",
        )

    ratio_checks = [
        ("bank_ratios_real.csv", "deposits_to_assets", -0.01, 1.20),
        ("bank_ratios_real.csv", "loans_to_deposits", -0.01, 2.50),
        ("bank_ratios_real.csv", "interest_expense_to_deposits_annualized", -0.01, 0.30),
        ("bank_ratios_real.csv", "net_income_to_assets_annualized", -0.25, 0.25),
        ("bank_ratios_real.csv", "noninterest_expense_to_assets_annualized", -0.01, 0.30),
        ("bank_ratios_real.csv", "equity_to_assets", -0.01, 0.50),
        ("bank_scenario_model.csv", "modeled_roa", -0.25, 0.25),
        ("bank_scenario_model.csv", "modeled_roe", -2.00, 2.00),
        ("bank_scenario_model.csv", "modeled_efficiency_ratio", -0.50, 3.00),
        ("bank_scenario_model.csv", "modeled_funding_gap_to_assets", -2.00, 2.00),
        ("bank_scenario_model.csv", "modeled_capital_ratio", 0.02, 0.50),
        ("bank_scenario_model.csv", "ftp_asset_transfer_rate", 0.00, 0.25),
        ("bank_scenario_model.csv", "ftp_deposit_crediting_rate", 0.00, 0.25),
        ("bank_scenario_model.csv", "ftp_customer_loan_spread", -0.10, 0.25),
        ("bank_scenario_model.csv", "ftp_deposit_spread", -0.10, 0.25),
    ]
    for file_name, column, low, high in ratio_checks:
        df = outputs[file_name]
        values = clean_numeric(df[column])
        bad = values.notna() & ((values < low) | (values > high))
        bad_count = int(bad.sum())
        add_validation_row(
            rows,
            "unreasonable_ratio_range",
            file_name,
            "PASS" if bad_count == 0 else "WARN",
            bad_count,
            f"{column} outside [{low}, {high}].",
            "warning" if bad_count else "info",
        )

    return pd.DataFrame(rows)


def write_outputs(outputs: dict[str, pd.DataFrame]) -> None:
    """Write every cleaned output DataFrame to data/cleaned as CSV."""
    for file_name, df in outputs.items():
        df.to_csv(CLEAN_DIR / file_name, index=False)


def print_run_summary(outputs: dict[str, pd.DataFrame]) -> None:
    """Print the most important run checks after the pipeline finishes."""
    print("\n=== Pipeline output summary ===")
    end_period = COMMON_ANALYSIS_END_PERIOD or "latest available"
    end_date = COMMON_ANALYSIS_END_DATE or "latest available"
    print(
        "Cleaned outputs filtered to analysis window: "
        f"{COMMON_ANALYSIS_START_PERIOD} to {end_period} / "
        f"{COMMON_ANALYSIS_START_DATE} to {end_date}"
    )
    for file_name, df in outputs.items():
        date_column = "period" if "period" in df.columns else None
        coverage = ""
        if date_column and not df.empty:
            coverage = f" | coverage: {df[date_column].min()} to {df[date_column].max()}"
        print(f"{file_name}: {len(df):,} rows, {len(df.columns):,} columns{coverage}")

    print("\n=== Missing values by output ===")
    for file_name, df in outputs.items():
        missing_total = int(df.isna().sum().sum())
        print(f"{file_name}: {missing_total:,} total missing cells")

    print("\n=== Sample: real SEC long ===")
    print(outputs["bank_financials_real_long.csv"].head(10).to_string(index=False))
    print("\n=== Sample: market rates ===")
    print(outputs["market_rates.csv"].tail(10).to_string(index=False))
    print("\n=== Sample: scenario model ===")
    sample_cols = [
        "bank_ticker",
        "scenario_name",
        "period",
        "modeled_loan_portfolio",
        "modeled_deposit_portfolio",
        "modeled_net_income",
        "modeled_roa",
        "scenario_data_label",
    ]
    print(outputs["bank_scenario_model.csv"][sample_cols].head(12).to_string(index=False))


def main() -> None:
    """Run the full pipeline from raw public downloads to cleaned CSV outputs."""
    ensure_directories()
    real_long, real_wide, real_ratios = build_real_financials()
    market_rates, macro_indicators, quarterly_fred = build_fred_data()
    assumptions = build_scenario_assumptions()
    scenario_model = build_scenario_model(real_wide, real_ratios, quarterly_fred, assumptions)

    real_long = filter_to_analysis_window(real_long)
    real_wide = filter_to_analysis_window(real_wide)
    real_ratios = filter_to_analysis_window(real_ratios)
    market_rates = filter_to_analysis_window(market_rates)
    macro_indicators = filter_to_analysis_window(macro_indicators)
    scenario_model = filter_to_analysis_window(scenario_model)

    real_long = add_period_sort(real_long)
    real_wide = add_period_sort(real_wide)
    real_ratios = add_period_sort(real_ratios)
    market_rates = add_period_sort(market_rates)
    macro_indicators = add_period_sort(macro_indicators)
    scenario_model = add_period_sort(scenario_model)

    dim_bank = build_dim_bank(real_wide, scenario_model)
    dim_metric = build_dim_metric(real_long)
    dim_quarter = build_dim_quarter(
        [real_long, real_wide, real_ratios, market_rates, macro_indicators, scenario_model]
    )
    dim_scenario = build_dim_scenario(assumptions)
    dim_rate_series = build_dim_rate_series(market_rates)

    core_outputs = {
        "dim_bank.csv": dim_bank,
        "dim_metric.csv": dim_metric,
        "dim_quarter.csv": dim_quarter,
        "dim_scenario.csv": dim_scenario,
        "dim_rate_series.csv": dim_rate_series,
        "bank_financials_real_long.csv": real_long,
        "bank_financials_real_wide.csv": real_wide,
        "bank_ratios_real.csv": real_ratios,
        "market_rates.csv": market_rates,
        "macro_indicators.csv": macro_indicators,
        "bank_scenario_model.csv": scenario_model,
        "scenario_assumptions.csv": assumptions,
    }
    validation_summary = build_validation_summary(core_outputs)
    dictionary_inputs = {**core_outputs, "validation_summary.csv": validation_summary}
    data_dictionary = build_data_dictionary(dictionary_inputs)
    outputs = {
        **core_outputs,
        "data_dictionary.csv": data_dictionary,
        "validation_summary.csv": validation_summary,
    }

    write_outputs(outputs)
    print_run_summary(outputs)


if __name__ == "__main__":
    main()
