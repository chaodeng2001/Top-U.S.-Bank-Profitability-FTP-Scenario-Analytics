Top U.S. Bank Profitability, FTP & Scenario Analytics
This project compares major U.S. banks using public financial statement data, market-rate data, and a modeled scenario layer. It was built as a finance analytics portfolio project with three goals:

Pull real public bank financial data from SEC EDGAR.
Combine it with FRED interest-rate and macroeconomic data.
Build a public-data-calibrated profitability, FTP, and stress-scenario model for Power BI analysis.
The project is designed to answer practical banking questions:

Which banks performed best on ROA, ROE, net income, and efficiency ratio?
Which banks show stronger expense discipline?
How much modeled profitability comes from loan spreads versus deposit funding value?
Which banks are most sensitive to rate shocks and credit stress?
Key Takeaways
The final report and Power BI dashboard focus on six U.S. banks: JPMorgan Chase, Bank of America, Wells Fargo, Citigroup, U.S. Bancorp, and Capital One.

JPMorgan Chase leads most scale-based measures, including cumulative net income, ROE, and total modeled FTP profitability.
Citigroup performs best on a size-adjusted FTP basis, with the highest FTP net spread income relative to loans.
Capital One has the highest ROA and the most stable modeled net income across the scenario cases.
Wells Fargo and U.S. Bancorp show the largest modeled earnings declines under Credit Stress.
Credit stress is more damaging than rate movement alone because higher provisions directly reduce earnings.
These findings are model outputs based on public data. They are not internal bank forecasts, regulatory stress-test results, or investment recommendations.

Data Sources
The pipeline uses public data only.

SEC EDGAR

SEC Company Facts API
Bank-level XBRL financial statement fields
Filing metadata for traceability, including form type, filing date, accession number, and XBRL concept
FRED

SOFR
Effective Federal Funds Rate
Bank Prime Loan Rate
Treasury 3-Month, 2-Year, and 10-Year rates
CPI
Unemployment rate
GDP
Commercial bank deposits
Loans and leases in bank credit
Delinquency rate on all loans
Real Data vs. Modeled Data
This distinction is important.

The historical files are real public data from SEC EDGAR and FRED:

bank_financials_real_long.csv
bank_financials_real_wide.csv
bank_ratios_real.csv
market_rates.csv
macro_indicators.csv
The scenario file is modeled data:

bank_scenario_model.csv
The scenario model is calibrated from public SEC and FRED data, but it is not actual internal bank data. Every modeled row is labeled as:

MODELED/SCENARIO DATA - calibrated from public SEC EDGAR and FRED data; not actual internal bank data

Methodology
The pipeline starts by downloading SEC Company Facts data for the target banks. It extracts financial statement fields such as net income, interest income, interest expense, noninterest income, noninterest expense, total assets, deposits, loans, equity, and selected capital fields where available.

The SEC data is then organized in two ways:

A long table for source traceability and XBRL lineage.
A wide table for financial ratios and Power BI measures.
Historical ratios are calculated directly from public SEC fields:

ROA = annualized net income / total assets
ROE = annualized net income / total equity
Efficiency ratio = noninterest expense / operating revenue
Expense/assets = annualized noninterest expense / total assets
Funding cost = annualized interest expense / deposits
Loan yield = annualized interest income / loans
The scenario model starts from each bank's latest public SEC baseline and applies scenario assumptions for balance sheet growth, rate shocks, deposit beta, loan yield beta, expense growth, credit loss rate, and capital ratio.

FTP Modeling
FTP means Funds Transfer Pricing in this project.

Because banks do not publicly disclose their internal FTP curves, this project creates a public-data-based FTP estimate. The model assigns:

An internal funding charge to loans.
An internal funding credit to deposits.
This separates modeled profitability into loan spread income and deposit funding value.

Core FTP fields include:

ftp_asset_transfer_rate
ftp_deposit_crediting_rate
ftp_loan_charge
ftp_deposit_credit
ftp_loan_spread_income
ftp_deposit_spread_income
ftp_net_spread_income
ftp_adjusted_net_interest_income
The default FTP setup uses:

SOFR as the short-term funding-rate anchor.
2-Year Treasury as the asset-transfer-rate anchor.
Scenario Design
The model includes four scenarios.

Scenario	Rate Shock	Deposit Beta	Loan Yield Beta	Expense Growth	Credit Loss Rate	Capital Ratio
Base	0 bp	0.35	0.55	3.00%	0.60%	9.50%
Rate Up 100bp	+100 bp	0.45	0.65	3.30%	0.70%	10.00%
Rate Down 100bp	-100 bp	0.25	0.45	2.80%	0.60%	9.50%
Credit Stress	+50 bp	0.50	0.50	4.50%	1.80%	11.00%
Modeled net income is calculated as:

Modeled Net Income =
(Modeled Net Interest Income
 + Modeled Noninterest Income
 - Modeled Noninterest Expense
 - Modeled Provision)
* (1 - Tax Rate)
The model uses a 21% tax rate.

Output Files
Cleaned Power BI-ready outputs are written to data/cleaned/.

File	Purpose
bank_financials_real_long.csv	SEC financial statement data in long format with XBRL lineage
bank_financials_real_wide.csv	SEC bank-quarter table for calculations
bank_ratios_real.csv	Historical profitability, funding, capital, and efficiency ratios
market_rates.csv	FRED market-rate observations
macro_indicators.csv	FRED macroeconomic indicators
bank_scenario_model.csv	Modeled bank-scenario-quarter profitability dataset
scenario_assumptions.csv	Scenario inputs and assumptions
data_dictionary.csv	Field definitions
validation_summary.csv	Data quality and validation checks
dim_bank.csv	Bank dimension table
dim_metric.csv	Metric dimension table
dim_quarter.csv	Quarter dimension table
dim_scenario.csv	Scenario dimension table
dim_rate_series.csv	Rate series dimension table
Raw files are saved separately:

data/raw/sec/
data/raw/fred/
Current Data Coverage
Latest generated output:

Dataset	Rows	Coverage
SEC financials long	2,402	2018Q2 to 2026Q1
SEC financials wide	255	2018Q2 to 2026Q1
SEC ratios	255	2018Q2 to 2026Q1
Market rates	13,121	2018Q2 to 2026Q2
Macro indicators	1,095	2018Q2 to 2026Q2
Scenario model	256	2026Q1 to 2028Q1
Scenario assumptions	4	Four scenarios
The written report uses 2018-2025 for the main historical comparison to avoid mixing full-year results with partial 2026 data.
