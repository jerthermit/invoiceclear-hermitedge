<p align="center">
  <img src="frontend/web/hermit-edge-mark.png" alt="Hermit Edge" width="72" />
</p>

<h1 align="center">InvoiceClear</h1>

<p align="center"><strong>AI Agent for Invoice Resolution</strong></p>
<p align="center"><a href="https://invoiceclear.hermitedge.com">Open InvoiceClear</a></p>

InvoiceClear helps billing teams resolve invoices held up by a missing or mismatched purchase order. The agent checks the invoice, PO revisions and correspondence, cites the records it inspected and recommends a next step. A reviewer approves the billing handoff.

## What it does

### Review invoice blockers

- Adds invoices manually, imports CSV batches or syncs linked QuickBooks Online invoices.
- Keeps missing POs, reference conflicts, wrong legal customers, currency differences and insufficient authorization together with their source records.
- Retains original PDFs, document revisions, customer replies and source-linked review fields.
- Accounts for prepared allocations against a shared PO without claiming verified external consumption.
- Keeps the worklist paginated and preserves its position when returning from a case.

### Investigate and prepare the handoff

- Drafts replies and flags cases where the available records cannot support a recommendation.
- Requires human review before extracted fields support approval.
- Records a supported PO-reference correction separately from the original invoice.
- Produces an approved billing brief, original PO, itemized source CSV and frozen approval record.
- Exports active prepared resolutions with billing context, approval identity and provenance.

### Connect a private workspace

- Sends the exact approved request through Gmail and retains the provider receipt.
- Captures replies and attachments from confirmed request threads with opt-in background intake.
- Supports a separately approved update to one configured QuickBooks invoice PO custom field, followed by readback.

## Invoice flow

```text
Import invoice → Investigate blocker → Request or review PO evidence
                                          ↓
                            Correct reference or hold for clarification
                                          ↓
                                Human approval → Billing handoff
```

Missing evidence and failed comparisons stay blocked. Preparing a handoff does not collect money or mark an invoice paid.

## How it stays reliable

- Original amounts, payment state and document bytes stay separate from model suggestions.
- Source evidence, current record versions and human confirmation gate protected actions.
- Durable idempotency and transaction checks prevent duplicate approvals and PO over-allocation.
- Unknown external actions require receipt reconciliation before another attempt.
- Saved agent findings retain inspected sources and tool activity; changed evidence makes them stale.
- Records, audit history and SQLite backups survive application restarts on persistent storage.

## Architecture

```text
Billing team
     ↓
HTML · CSS · JavaScript — Vercel
     ↓
Python API — Railway
     ├── SQLite · persistent volume
     ├── TogetherAI · extraction and read-only investigation
     ├── QuickBooks Online · private connection
     └── Gmail · private connection
```

**Stack:** Python 3.11, JavaScript, HTML, CSS, SQLite, pypdf, cryptography, TogetherAI, QuickBooks Online, Gmail, Vercel and Railway.

## Implementation boundaries

| Area | Public edition |
| --- | --- |
| Records | 36 fictional invoices across eight accounts, 35 retained two-page PDF POs and 16 recorded replies. Two constructed approvals illustrate local shared-PO allocations. |
| Recording files | Matching 36-row CSV, a separate import case, 38 PDFs including three incoming POs, billing lines, correspondence and a revision manifest. |
| AI | A backend TogetherAI key enables case investigation. Each run requires confirmation; there are no prewritten findings. |
| Connections | Gmail sends, reply capture and QuickBooks updates require private mode, provider setup and consent. They are not enabled by supplying only a TogetherAI key. |
| Files | Readable PDF or UTF-8 TXT, up to 5 MB and 20 pages. Image-only scans need readable text; OCR is not included. |
| Payments | Internal handoff approval is not payment, collection, dispute settlement or a confirmed accounting update. |
| Scale | A representative case pack and a bounded 5,000-record local exercise do not validate 50,000 monthly invoices, twenty-user operation or hosted capacity. |
| Access | One shared public workspace; no confidential uploads. Private mode starts empty. This is not multi-tenant SaaS. |

## Run locally

Requirements: Python 3.11+ and Node.js 22.

From the repository root:

```bash
make -C backend setup
make -C backend run
```

Open `http://127.0.0.1:8100`. Public sign-in credentials are filled in. If the port is busy, use `make -C backend run PORT=8101`.

An existing database is never overwritten. To inspect the latest recording pack separately:

```bash
INVOICECLEAR_MODE=demo INVOICECLEAR_DB_PATH=backend/data/invoiceclear-public-202611-artifacts.sqlite3 make -C backend run
```

### Try a complete case

Choose **Import CSV → Download source files**, unzip the bundle and import `invoices.csv`. Open `STA-2026-2101`, record the supplied reply, upload its matching PDF, review the source, correct the reference and approve the handoff. Reimporting skips identical rows.

Then inspect `STA-2026-1059` or `STA-2026-1074`: both must stay blocked by a one-cent authorization shortfall. The [source-file guide](backend/fixtures/public/README.md) maps the documents and cases.

### Enable the AI agent

Set these in the backend environment, never in frontend code:

```dotenv
TOGETHER_API_KEY=your-backend-only-key
```

Remove `INVOICECLEAR_AGENT_ENABLED=false` if it was set during deployment. Restart the backend, open an invoice and choose **Investigate**. A run starts only after confirmation; findings, cited passages, next steps and tool activity are retained.

The configured model is `zai-org/GLM-5.3-Flash`; `TOGETHER_MODEL` can select an available tool-calling model. Account access and live behavior require your own verification. Extraction can stay `INVOICECLEAR_PROVIDER=rules`; use `together` only if model-based extraction is also wanted.

The app does not load `.env` files automatically. Export local variables or set them in Railway. Variable names are documented in [backend/.env.example](backend/.env.example) and [frontend/.env.example](frontend/.env.example).

## Verification

```bash
make -C backend release
```

Runs Python/JavaScript checks, frontend builds and regressions, a 37-invoice HTTP walkthrough and a bounded 5,000-record exercise against temporary databases. Agent checks use controlled protocol responses, not live inference. See [Verification](backend/docs/VERIFICATION.md) for results and limits.

## Deployment

- **Railway:** root `backend/`, Dockerfile builder, default image start command, one replica and a persistent volume at `/data`.
- **Vercel:** root `frontend/`, framework Other, Node.js 22, the checked static build and `dist` output.
- Set the exact Railway origin in frontend `INVOICECLEAR_API_BASE_URL` and the exact Vercel origin in backend `INVOICECLEAR_ALLOWED_ORIGINS`.
- Set `RAILWAY_RUN_UID=0` so the launcher can prepare the mount, then drop to UID/GID 10001. Leave application host/port overrides unset.
- Keep provider credentials on Railway only. Public and private editions need separate services, volumes and credentials.

Follow [Manual deployment](backend/docs/RELEASE.md#manual-dashboard-walkthrough) for dashboard setup and verification.

## Repository layout

```text
frontend/     Interface, local assets, static build and Vercel configuration
backend/      API, integrations, persistence, fixtures, tests and operating guides
README.md     Product, workflow and setup
.gitignore    Publication exclusions
```

Credentials, databases, backups, generated builds, private engagement notes and recordings are excluded. [Publishing](backend/docs/PUBLISHING.md) contains the GitHub About fields and upload instructions.

## Project context

An independently extended, sanitized reconstruction of a billing workflow originating in previously commissioned client work. This edition demonstrates the engineering and review process, not measured payment acceleration or client ROI.

Built by [Emman at Hermit Edge](https://hermitedge.com).

## License and trademarks

Bundled font licenses remain with their files. QuickBooks, Gmail and TogetherAI names belong to their respective owners. Public source visibility does not grant an open-source license.

## Copyright

Copyright © 2026 Emman Ermitaño. All rights reserved.
