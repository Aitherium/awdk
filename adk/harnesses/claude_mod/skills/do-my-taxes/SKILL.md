---
name: do-my-taxes
description: >-
  Do My Taxes — personal tax preparation workflow. Personal 1040 tax preparation using document ingestion, transaction categorization, deduction discovery, and CPA-ready output.
---

# Do My Taxes — personal tax preparation workflow

Personal 1040 tax preparation using document ingestion, transaction categorization, deduction discovery, and CPA-ready output.

**⚠️ Important:** This is a draft-preparation tool, not a tax preparer and does not file returns. All output is for CPA review only. Consult a qualified tax professional before filing.

## Prerequisites

- W-2 form(s) from employer(s)
- 1099-NEC, 1099-INT, 1099-DIV, 1099-MISC, 1099-K, or 1099-B forms (if applicable)
- Bank statements (CSV, OFX, or XLSX formats)
- Credit card statements (PDF or CSV)
- Brokerage statements (for capital gains/losses)
- Mortgage statements (for 1098 interest, if itemizing)
- Receipts or records for deductible expenses (optional but helpful)

## Step-by-Step Workflow

### 1. Ingest Documents

Upload your statements and forms:
- Export bank statements as CSV or OFX from your bank
- Download credit card statements (PDF or CSV)
- Collect all W-2 and 1099 forms (PDF or scanned)
- Verify ingestion before proceeding

```bash
# Example ingest call
ingest_batch(
  file_paths="/path/to/statements/",
  document_type="auto",
  account_name="checking"
)
```

### 2. Import Tax Forms

Explicitly extract form data:

- **W-2 income:** Captures gross income, federal withholding, FICA
- **1099-NEC (self-employment):** Self-employment income
- **1099-INT (interest):** Interest income
- **1099-DIV (dividends):** Dividend income
- **1099-MISC:** Miscellaneous income
- **1099-K (payment card):** Payment processor income
- **1099-B (brokerage):** Capital gains/losses
- **1098 (mortgage interest):** Mortgage interest deduction

### 3. Build Ledger

Deduplicate and organize all transactions:

Returns a unified ledger with:
- Deterministic transaction IDs (cryptographic hash)
- Account and date ranges
- Near-duplicate flagging (same day + amount, different merchant)
- Ready for categorization

### 4. Review Ledger Summary

Inspect monthly and account-level totals:

```bash
ledger_summary(year=2025)
```

Returns monthly inflow/outflow breakdown and account summaries to verify data import completeness.

### 5. Categorize Transactions

Review and tag transactions:

- **Load queue:** shows unconfirmed transactions
- **Per-transaction review:** 
  - Select expense category (EXPENSE, DEDUCTION, DONATION, HEALTH, BUSINESS)
  - Check "Save as rule" to auto-apply to future similar merchants
  - Confirm per row
- **Bulk approve:** auto-confirms high-confidence merchant rule matches

### 6. Detect Subscriptions & Bills

Identify recurring charges and price hikes:

Returns:
- Active subscriptions (merchant, cadence, monthly estimate)
- Recent price hikes (old vs. new amount, % increase)
- Zombie subscriptions (forgotten charges)
- Monthly and annual totals

Use the Bills view to review and cancel unwanted subscriptions before year-end for tax deduction potential.

### 7. Build Worksheets (Standard vs. Itemized)

Compare deduction strategies:

```bash
worksheet_1040(
  filing_status="single",
  tax_year=2025,
  state_flat_rate=0.05,
  prior_year_tax=5000,
  prior_agi=80000
)
```

Returns summary:
- Total income
- AGI (adjusted gross income)
- Standard deduction vs. itemized deductions
- Taxable income
- Total tax liability
- Estimated refund or amount owed

### 8. Generate CPA Package

Create final deliverable:

```bash
report_package(
  filing_status="single",
  tax_year=2025,
  state_flat_rate=0.05,
  prior_year_tax=5000,
  prior_agi=80000
)
```

Outputs:
- **DOCX file:** Formatted tax summary with all transactions, categories, and assumptions
- **XLSX file:** Full ledger with merchant rules applied, suitable for import to tax software

## Privacy & Security

All document processing runs locally with privacy-preserving defaults:

- Tax data stored locally
- Workspace-scoped database storage
- Cryptographic transaction IDs prevent accidental re-ingestion duplicates
- Near-duplicates flagged for human review, never auto-deleted

## Caveats

1. **Not legal tax advice.** This is a draft tool. Have a qualified CPA review all output before filing.
2. **Run worksheets with tax_year=2025** to use current standard deduction and bracket constants.
3. **Merchant rules are per-workspace.** Rules created by categorization are private to your workspace.
4. **Near-duplicate handling:** Same-day, same-amount transactions with different merchants are flagged, not auto-merged — you decide.
5. **State and local taxes:** Provide `state_flat_rate` when running worksheets; complex multi-state/local filings need manual review.
6. **Capital gains:** Use `import_broker_csv` for brokerage statements; manually confirm cost basis and holding periods.

## Troubleshooting

- **"No documents ingested":** Ensure files are in CSV, OFX, or PDF format; re-run ingest
- **"All transactions reviewed":** Queue is empty; use bulk approve to confirm high-confidence matches
- **Worksheets show $0 income:** Verify W-2 and 1099 forms were imported explicitly
- **Price hikes not detected:** Bills module requires at least 2 transactions from same merchant; ensure ledger is built

---

**This skill is a template. Adapt it to your tax preparation stack and jurisdiction; test the output against prior-year returns before relying on it for new filings.**
