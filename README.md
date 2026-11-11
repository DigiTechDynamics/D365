# AP Invoice Agent (D365 F&O-style demo)

An accounts payable agent that reads vendor invoices with AI and then applies **finance controls you can see and change**.
AI reads the document; matching, coding, approvals and audit are deterministic code driven by `config/policy.json`.

> Demo with fictional data (Contoso / USMF-style). Nothing is sent to Dynamics 365: transfers are written to `outbox/`
> in the shape a D365 F&O sandbox integration would use.

## What it does

| Stage | What happens | Where |
|---|---|---|
| Capture | Claude reads the PDF into header + lines, with a confidence per field. Document text is treated as data, never instructions. | `agent/extract.py` |
| Classify | PO invoice, overhead (no PO) invoice, or project invoice | `agent/matching.py` |
| Match | PO price and received quantity within tolerance; vendor by tax ID | `agent/matching.py` |
| Charges | Freight capitalized pro-rata into item cost, or expensed, per charge code | `agent/matching.py` |
| Accounting | Proposed entry: main account, Cost Center / Department / Business Unit, project + project category, sales tax, AP credit. Every line says *why* it was coded that way (procurement category, vendor default, vendor posting history, keyword rule, project). | `agent/accounting.py` |
| Validate | Duplicates, vendor status, totals, currency, dates vs payment terms, PO status, project status, required/active dimensions, tax vs tax setup, balanced entry | `agent/validation.py` |
| Route | Approval rules from policy: AP verification, PO buyer, cost center owner, project manager, limit escalation, CFO. Segregation of duties, delegation (out of office), SLA escalation flag. | `agent/routing.py` |
| Approve | Only the assigned person can act; approval limits enforced; corrections and coding changes logged | `agent/pipeline.py` |
| Transfer | Pending vendor invoice (PO) or invoice journal (overhead/project), with the invoice image attached by hash | `agent/pipeline.py` |
| Audit | Append-only log of every agent and human action | `state/audit.jsonl` |

KPIs on the queue include **touchless rate** and **first-time-right postings** (transferred with no field or coding corrections).

## Sample scenarios (in `inbox/`)

| Invoice | Shows |
|---|---|
| FIS-77821 Fabrikam | Clean PO invoice, freight capitalized, tax checked - **touchless** and auto-transferred |
| PW-5530 Proseware | Price 11.8% over PO - routed to the PO buyer |
| NW-2026-1102 Northwind | Overhead invoice, faint due date (72% confidence) - AP verifies, then cost center owner approves |
| ADT-4471 Adatum | 18,500 overhead invoice - SoD: the IT manager requested this vendor, so their deputy approves, then Finance Controller (over limit) |
| NW-2026-1088 Northwind | Re-sent invoice already posted - **blocked as duplicate** |
| LIT-0931 Litware | Project invoice for PRJ-00045 - project manager out of office, delegated |

## Run it

```bash
pip install -r requirements.txt
python scripts/make_samples.py   # (re)creates the sample PDFs
python app.py                    # http://127.0.0.1:5000
```

Click **Process inbox**, then use **Acting as** (top right) to switch between AP, buyers, managers and the controller.

- **Demo mode (no API key):** the bundled samples use reference extractions in `data/demo_extractions/`, so you can run everything offline.
- **Live mode:** set `ANTHROPIC_API_KEY` (or sign in with `ant auth login` and set `INVOICE_AGENT_LIVE=1`). Drop any invoice PDF into `inbox/` and Claude reads it. The model is set in `config/policy.json`.

## Connecting to a real D365 F&O sandbox (next step)

Replace `data/masterdata.json` with reads from your sandbox (vendors, open POs and receipts, procurement categories,
financial dimensions, projects, posted invoices) and replace the outbox writer with a call to your sandbox's data
entities or the Dynamics 365 ERP MCP server. Use a dedicated service identity with least-privilege roles. Never point it at production without your finance and audit teams' sign-off.
