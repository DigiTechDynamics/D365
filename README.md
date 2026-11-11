# AP Invoice Agent (D365 F&O-style demo)

An accounts payable agent that reads vendor invoices with AI and then applies **finance controls you can see and change**.
AI reads the document; matching, coding, approvals and audit are deterministic code driven by `config/policy.json`.

> Demo with fictional data (Contoso / USMF-style). Nothing is sent to Dynamics 365: transfers are written to `outbox/`
> in the shape a D365 F&O sandbox integration would use.

## What it does

| Stage | What happens | Where |
|---|---|---|
| Intake | Collects invoices from a shared AP **mailbox** and a **SharePoint** folder (plus manual drops). Screens every file before AI sees it: allowed types checked by content, size limit, identical-file detection. Rejected files are quarantined and logged. | `agent/intake.py`, `agent/sources.py` |
| Capture | Claude reads the PDF into header + lines, with a confidence per field. Document text is treated as data, never instructions. | `agent/extract.py` |
| Classify | PO invoice, overhead (no PO) invoice, or project invoice | `agent/matching.py` |
| Match | PO price and received quantity within tolerance; vendor by tax ID | `agent/matching.py` |
| Charges | Freight capitalized pro-rata into item cost, or expensed, per charge code | `agent/matching.py` |
| Accounting | Proposed entry: main account, Cost Center / Department / Business Unit, project + project category, sales tax, AP credit. Every line says *why* it was coded that way (procurement category, vendor default, vendor posting history, keyword rule, project). | `agent/accounting.py` |
| Validate | Sender domain registered for the vendor, emails asking to change bank details (AP must clear with a recorded call-back), duplicates, vendor status, totals, currency, dates vs payment terms, PO status, project status, required/active dimensions, tax vs tax setup, balanced entry | `agent/validation.py` |
| Route | Approval rules from policy: AP verification, PO buyer, cost center owner, project manager, limit escalation, CFO. Segregation of duties, delegation (out of office), SLA escalation flag. | `agent/routing.py` |
| Approve | Only the assigned person can act; approval limits enforced; corrections and coding changes logged | `agent/pipeline.py` |
| Transfer | Pending vendor invoice (PO) or invoice journal (overhead/project), with the invoice image attached by hash | `agent/pipeline.py` |
| Audit | Append-only log of every agent and human action | `state/audit.jsonl` |

KPIs on the queue include **touchless rate** and **first-time-right postings** (transferred with no field or coding corrections).

## Sample scenarios

The demo mailbox (`demo_sources/mailbox/*.eml`) and SharePoint folder (`demo_sources/sharepoint/AP/Incoming`) contain:

| Arrives as | Shows |
|---|---|
| Email: Fabrikam FIS-77821 | Clean PO invoice, freight capitalized, tax checked - **touchless** and auto-transferred |
| Email: Proseware PW-5530 | Price 11.8% over PO - routed to the PO buyer |
| Email: Northwind NW-2026-1102 | Overhead invoice, faint due date (72% confidence) - AP verifies, then cost center owner approves |
| Email: Adatum ADT-4471 | Sent from a **lookalike domain** asking to **change bank details** - AP must clear both flags with a recorded call-back; then SoD (IT manager requested this vendor, so their deputy approves) and Finance Controller (over limit) |
| Email: Northwind second notice | Re-sent invoice already posted - **blocked as duplicate** |
| SharePoint: Litware LIT-0931 | Project invoice for PRJ-00045 - project manager out of office, delegated |
| Email: "Invoice_8812.html" | **Quarantined** - file type not allowed |
| Email: Fabrikam reminder | Same PDF again - **stopped as an identical file** |
| Email: Proseware question | No attachment - ignored and logged |

## Run it

```bash
pip install -r requirements.txt
python scripts/make_samples.py   # (re)creates sample PDFs, demo mailbox and SharePoint folder
python app.py                    # http://127.0.0.1:5000
```

Click **Check sources & process**, then use **Acting as** (top right) to switch between AP, buyers, managers and the controller. **Reset demo** clears everything so the demo mailbox and folder are read again.

- **Demo mode (no API key):** the bundled samples use reference extractions in `data/demo_extractions/`, so you can run everything offline.
- **Live mode:** set `ANTHROPIC_API_KEY` (or sign in with `ant auth login` and set `INVOICE_AGENT_LIVE=1`). Any invoice PDF that arrives is read by Claude. The model is set in `config/policy.json`.

## Connecting a real mailbox and SharePoint library (Microsoft Graph)

In `config/sources.json`, set a source's `mode` to `"graph"` and fill in the mailbox address or SharePoint site and folder.
Then set `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID` and `GRAPH_CLIENT_SECRET` as environment variables (never in the repo).

Ask your Microsoft 365 admin for an Entra ID app registration with **least privilege**:
- **Mailbox:** `Mail.ReadWrite` (application), limited to the AP mailbox only with an Exchange application access policy /
  RBAC for Applications. The agent reads unread messages with attachments, marks them read and moves them to a
  "Processed by agent" folder.
- **SharePoint:** `Sites.Selected` (application), with write access granted on the Finance site only. Processed files
  are moved from `AP/Incoming` to `AP/Processed`.

The Graph connector is written but has not been run against a live tenant - test it in a non-production tenant first.

## Connecting to a real D365 F&O sandbox (next step)

Replace `data/masterdata.json` with reads from your sandbox (vendors, open POs and receipts, procurement categories,
financial dimensions, projects, posted invoices) and replace the outbox writer with a call to your sandbox's data
entities or the Dynamics 365 ERP MCP server. Use a dedicated service identity with least-privilege roles. Never point it at production without your finance and audit teams' sign-off.
