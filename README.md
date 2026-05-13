# Top-U.S.-Bank-Profitability-FTP-Scenario-Analytics

Python data pipeline for a Power BI portfolio project comparing major U.S. banks and building a clearly labeled profitability, Funds Transfer Pricing, and scenario dataset calibrated from public data.

Target banks:

- JPM
- BAC
- WFC
- C
- USB
- PNC
- TFC
- COF


```

The default setup uses `SOFR` as the short-term funding-rate anchor and `DGS2` as the asset-transfer-rate anchor.

## Real Data vs. Modeled Scenario Data

The files `bank_financials_real_long.csv`, `bank_financials_real_wide.csv`, `bank_ratios_real.csv`, `market_rates.csv`, and `macro_indicators.csv` are built from public sources:

- SEC EDGAR Company Facts API
- FRED series pages and CSV downloads

The file `bank_scenario_model.csv` is not actual internal bank data. It is modeled scenario data calibrated from public EDGAR and FRED baselines. Every row includes:

- `data_type = modeled_scenario_calibrated_from_public_data`
- `scenario_data_label = MODELED/SCENARIO DATA - calibrated from public SEC EDGAR and FRED data; not actual internal bank data`

Use that label prominently in Power BI.

## Financial Modeling / FTP Layer

This project now includes **FTP as Funds Transfer Pricing**, not File Transfer Protocol.

The scenario model includes an industry-style internal profitability layer:

- `ftp_asset_transfer_rate`
- `ftp_deposit_crediting_rate`
- `ftp_loan_charge`
- `ftp_deposit_credit`
- `ftp_customer_loan_spread`
- `ftp_deposit_spread`
- `ftp_loan_spread_income`
- `ftp_deposit_spread_income`
- `ftp_net_spread_income`
- `ftp_adjusted_net_interest_income`

In banking terms, FTP helps separate customer profitability from interest-rate and funding-center effects:

- Loans receive an internal FTP funding charge.
- Deposits receive an internal FTP funding credit.
- Loan spread income shows how much loan yield exceeds the internal transfer rate.
- Deposit spread income shows how much deposit funding value exceeds the customer funding cost.

The FTP rates are modeled from public FRED rate proxies, primarily SOFR and Treasury 2Y, and calibrated to each bank's public EDGAR baseline. This makes the project more directly related to bank financial modeling.

## Data Sources

SEC:

- Company tickers: `https://www.sec.gov/files/company_tickers.json`
- Company facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`

FRED:

- SOFR: `SOFR`
- Effective Fed Funds Rate: `DFF`
- Bank Prime Loan Rate: `DPRIME`
- Treasury 3M: `DGS3MO`
- Treasury 2Y: `DGS2`
- Treasury 10Y: `DGS10`
- CPI: `CPIAUCSL`
- Unemployment rate: `UNRATE`
- GDP: `GDP`
- Commercial bank deposits: `DPSACBW027SBOG`
- Loans and leases in bank credit: `TOTLL`
- Delinquency rate on all loans: `DRALACBN`

