# Release InvoiceClear

The hosted edition uses one workspace with a shared sign-in. Use this guide to reproduce the deployment and record the revision and hosted checks.

The latest recorded local checks are in [VERIFICATION.md](VERIFICATION.md).

## Manual dashboard walkthrough

Use the public edition below for the recording. Publish the prepared source to your GitHub repository first, following [PUBLISHING.md](PUBLISHING.md). No terminal deployment or provider key in source code is required.

### 1. Railway backend

Create a project and add a service from your GitHub repository. Before the final deployment, configure:

| Setting | Value |
| --- | --- |
| Root directory | `backend` |
| Builder | Dockerfile / Docker |
| Dockerfile | `Dockerfile`, relative to that root |
| Start command | Leave the override empty; the image runs `python start.py` |
| Volume | Attach to the backend service, mounted at `/data` |
| Replicas | 1 |
| Optional healthcheck | `/api/health`, timeout 120 seconds |
| Restart policy | On Failure, three retries |

In Railway's Variables editor add:

```dotenv
INVOICECLEAR_MODE=demo
INVOICECLEAR_DB_PATH=/data/invoiceclear-public-202611-artifacts.sqlite3
INVOICECLEAR_PROVIDER=rules
INVOICECLEAR_BACKUPS_ENABLED=true
RAILWAY_RUN_UID=0
```

When ready for live investigation, add `TOGETHER_API_KEY` as a Railway backend secret. Leave `TOGETHER_MODEL` unset to use the configured default. Remove `INVOICECLEAR_AGENT_ENABLED=false` if it was set for the initial deployment. Without a key, **Investigate** remains unavailable; explicit `INVOICECLEAR_AGENT_ENABLED=true` without a key is rejected at startup.

Do not set `INVOICECLEAR_HOST`, `INVOICECLEAR_PORT` or your own `PORT`. The image uses Railway's assigned port. The launcher prepares `/data` itself, then drops privileges; the API does not remain root. The database path must belong to this mounted volume. Reusing an occupied public database preserves its existing records instead of replacing them with new PDFs.

Deploy and generate the service's public HTTPS domain under Networking. Check `https://YOUR-RAILWAY-DOMAIN/api/health`: it must return HTTP 200. Save the backend origin without `/api`. The exact frontend origin is added in step 3; do not use a wildcard to get around CORS.

### 2. Vercel frontend

Add a new project from the same GitHub repository:

| Setting | Value |
| --- | --- |
| Root directory | `frontend` |
| Framework preset | Other |
| Node.js | 22.x |
| Install command | `npm ci --ignore-scripts` |
| Build command | `node scripts/build_frontend.mjs --deploy` |
| Output directory | `dist` |

The repository's `vercel.json` supplies the install/build/output values. In Vercel environment variables set only `INVOICECLEAR_API_BASE_URL` to the actual Railway HTTPS origin, for Production (and Preview only if you intend that origin to be allowed). No `/api` suffix, path, credentials or wildcard. Never add TogetherAI, QuickBooks or Google secrets to Vercel.

Deploy, then copy the actual stable Vercel production URL or your configured custom-domain origin. The backend URL is baked into the frontend build; changing it requires a frontend redeployment. A missing or insecure origin is rejected by the build rather than creating a broken app.

### 3. Link the origins

Return to Railway. Set `INVOICECLEAR_ALLOWED_ORIGINS` to that exact Vercel/custom-domain HTTPS origin, without a trailing path, and apply the deployment. This is necessary even though the frontend already knows the backend URL. Preview URLs need separate explicit authorization; do not allow every Vercel domain.

Open the Vercel URL and sign in with the filled public credentials. The browser title must be InvoiceClear, with AI Agent for Invoice Resolution below the product name. With `TOGETHER_API_KEY` set on Railway, open a case and confirm that **Investigate** is available.

### 4. Hosted acceptance

1. Download the source pack, import `invoices.csv`, open 2101 and upload its matching PDF. Review, correct and approve it, then inspect the handoff and CSV export.
2. Confirm 1059 or 1074 remains blocked by insufficient coverage; a model proposal cannot bypass the hold.
3. Choose Investigate and confirm transmission. Verify a genuine result with exact source citations, or an honest failure/abstention. Agent output is not pre-seeded.
4. Restart the Railway service without changing its volume/path. Confirm the imported case, original PDF and approval remain. Check backups and off-site recovery separately.

Public credentials are intentionally shared. Use no client documents; visitors can consume the bounded TogetherAI budget when it is enabled. Apply account spending controls and monitor storage. QuickBooks/Gmail consent and write actions remain private-only; a TogetherAI key does not connect them.

## Choose the edition

Use separate Railway services and volumes, Vercel projects, databases and credentials when hosting both editions. Never switch an existing demo database into private service.

| Backend variable | Public walkthrough | Private connected workspace |
| --- | --- | --- |
| `INVOICECLEAR_MODE` | `demo` | `private` |
| `INVOICECLEAR_DB_PATH` | `/data/invoiceclear-public-202611-artifacts.sqlite3` | `/data/invoiceclear-private.sqlite3` |
| `INVOICECLEAR_EMAIL`, `INVOICECLEAR_PASSWORD` | Demo defaults are shared publicly by `/api/runtime`; non-confidential permitted records only | Unique workspace email and unique password of at least 16 characters; starts empty |
| `INVOICECLEAR_PROVIDER` | `rules` | `rules` initially; TogetherAI is optional |
| `INVOICECLEAR_BACKUPS_ENABLED` | `true` | `true` |

The public walkthrough must contain no customer documents, credentials or confidential data. External sending and accounting changes are private-only. Both editions require sign-in for business records; public demo credentials are intentionally public.

The 36-case representative pack is installed only in an empty public workspace. Existing public/legacy databases are preserved, not silently upgraded or overwritten. To display the new pack on an existing Railway service, select a separate path such as `/data/invoiceclear-public-202611-artifacts.sqlite3` on the same persistent volume, keeping the old file for recovery. Locally, use the separate-path command in the README. Do not delete stored records merely to refresh walkthrough data.

The database is bound to its mode; starting it in the other mode is rejected without conversion. Legacy records with ambiguous provenance also stop startup: preserve them and inspect a recovery copy rather than deleting them. Public business writes stop atomically at 128 MiB allocated SQLite pages or 50,000 business/audit/idempotency rows; existing records and downloads remain available. Monitor volume and backup capacity separately. This does not provide tenant isolation or general denial-of-service protection.

## Vercel frontend and Railway backend

1. Create a Railway service with repository root `backend/`, Dockerfile builder and the default image start command (`python start.py`). Mount a persistent volume at `/data` and set `RAILWAY_RUN_UID=0`. Railway mounts volumes as root; the launcher changes ownership and permissions of `/data` itself only, clears supplementary groups, then runs the API as UID/GID 10001. Source code stays root-owned. It never recursively changes or deletes stored files. Keep one replica and use an On Failure restart policy with three retries. The optional health check is `/api/health` with a 120-second timeout. Leave `INVOICECLEAR_HOST` and `INVOICECLEAR_PORT` unset so the image binds `0.0.0.0` and uses Railway's assigned `PORT`.
2. Set the chosen edition's variables above in Railway. The app does not load `.env` files automatically; the example files document names only. Configure the exact frontend HTTPS origin as `INVOICECLEAR_ALLOWED_ORIGINS`, with no wildcard, path or query.
3. Create the Vercel project with root `frontend/`, framework Other and Node.js 22. Its `vercel.json` installs the dependency-free lockfile with `npm ci --ignore-scripts`, runs `node scripts/build_frontend.mjs --deploy` and serves `dist`. Set `INVOICECLEAR_API_BASE_URL` to the exact Railway HTTPS origin, without `/api`, credentials, query or fragment. Missing, insecure or loopback deployment origins stop the build before replacing output. The generated page restricts browser API connections to that origin and itself. Redeploy the frontend whenever this build-time value changes.
4. For private connections, set backend `INVOICECLEAR_PUBLIC_URL` to the Railway HTTPS origin and `INVOICECLEAR_FRONTEND_URL` to the exact Vercel HTTPS origin. Complete the setup below, deploy and perform acceptance. No provider secrets belong in Vercel or frontend files.

### Public edition: initial variables

Set these in Railway, replacing the frontend placeholder with your actual HTTPS origin:

```dotenv
INVOICECLEAR_MODE=demo
INVOICECLEAR_DB_PATH=/data/invoiceclear-public-202611-artifacts.sqlite3
INVOICECLEAR_ALLOWED_ORIGINS=https://your-frontend.vercel.app
INVOICECLEAR_PROVIDER=rules
INVOICECLEAR_BACKUPS_ENABLED=true
RAILWAY_RUN_UID=0
```

In Vercel, set only `INVOICECLEAR_API_BASE_URL=https://your-api.up.railway.app`, using the actual backend origin. Provider keys are not required for this public walkthrough. Public credentials are intentionally shared; never upload client material there.

The Docker image includes the fixed public source bundle. After signing in, choose **Import CSV → Download source files**, unzip it and follow its README and case guide. This adds one optional invoice through the actual import, then supports a recorded reply, PO upload, human review, correction and handoff without resetting existing cases. Investigation additionally requires the backend settings below; no agent findings are bundled with the files. Public customer navigation has no deployment-setup controls; use backend configuration and this guide. When investigation is unavailable and has no retained run/error, its section is omitted rather than asking the visitor to configure a provider.

The Docker image has a persistent-path fallback at `/data/invoiceclear.sqlite3`; the explicit edition-specific path above is preferred. A missing volume still cannot provide persistence: confirm `/data` is actually mounted. Existing unreadable/root-owned database files need a deliberate recovery operation, not recursive startup permission changes.

### Agent investigation in either edition

Set `TOGETHER_API_KEY` on the backend and redeploy. The key enables **Investigate** unless `INVOICECLEAR_AGENT_ENABLED=false` is present. The default is `zai-org/GLM-5.3-Flash`, listed for function calling in [TogetherAI's model guide](https://docs.together.ai/docs/inference/recommended-models); `TOGETHER_MODEL` is optional. Keep `INVOICECLEAR_PROVIDER=rules` for document extraction without model calls on upload. Public cases may be investigated, but public mode cannot send email or change QuickBooks. Verify the model with your account before claiming live acceptance.

Each fresh run requires confirmation before transmitting case evidence to TogetherAI. The backend permits at most five model rounds within a 20-second investigation window and retains idempotent run records, sources and tool activity. It shares the existing 100-call/500,000-accounted-token daily provider budget with extraction; these are conservative token reservations, not a dollar billing guarantee. Keep provider account spending controls and host monitoring in place. Do not enable a public key-funded service without accepting that visitors can consume this bounded budget.

After setup, verify a live case investigation, exact cited passages, an insufficient-evidence abstention and a provider failure. Confirm a repeated request recovers the stored run instead of buying another run, and that changed evidence marks the old result stale. No completed local model-mock test is evidence that your provider account or selected model works live.

The new `agent_runs` table is additive; original records are not reset. Retain a pre-upgrade backup. Pre-investigator backups remain valid recovery inputs, but naturally contain no investigation history. Existing immutable daily snapshots are not replaced, so the first snapshot taken before this update may omit later investigations.

Configure Railway settings manually; this repository uses the Dockerfile and does not require a Railway configuration manifest. The volume-startup handling follows [Railway volume permissions](https://docs.railway.com/volumes); Vercel's build and Node configuration follow its [project configuration](https://vercel.com/docs/project-configuration/vercel-json) and [supported Node versions](https://vercel.com/docs/functions/runtimes/node-js/node-js-versions).

## Private provider setup

Generate `INVOICECLEAR_TOKEN_KEY` once in the configured local Python environment:

```sh
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Store the generated key securely outside Git and retain it with recovery credentials. Keep the same key across restarts, releases and restores. Changing or losing it makes existing grants unreadable; reconnect accounts without replacing case records. Workspace credentials and provider configuration also bind grants: retain them for recovery, and expect reconnection if they change.

Start with a QuickBooks sandbox and Gmail test mailbox. In Railway set `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_ENVIRONMENT=sandbox`, `QBO_PO_FIELD_ID`, `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. The PO field ID is the numeric DefinitionId of an existing **invoice custom field** for the customer's PO reference. Register these exact backend callbacks in the provider apps:

- `INVOICECLEAR_PUBLIC_URL/api/connections/qbo/callback`
- `INVOICECLEAR_PUBLIC_URL/api/connections/gmail/callback`

Use the actual backend origin in those callback URLs. Complete company/mailbox consent and Check access in Connections. Follow [CONNECTIONS.md](CONNECTIONS.md) for provider setup, permissions and the full connected walkthrough; configuration or access checks alone do not verify send, reply intake or accounting update.

Rules extraction needs no model key. To opt into TogetherAI extraction, set backend `INVOICECLEAR_PROVIDER=together` and `TOGETHER_API_KEY`; `TOGETHER_MODEL` optionally overrides the pinned default. The default is officially listed, but access and behavior with your account remain unverified. Check extraction uses a fixed non-customer PO and may incur a charge; real documents sent to the model require permission.

Background inbox capture is off until Start capture in private Connections; there is no environment toggle. It checks only confirmed app request threads across open linked cases, up to 10 per pass with a 60-second idle interval. Its account/configuration-bound opt-in and fair cursor survive restart. Changes require another opt-in; stopping may leave one in-flight check to finish. Selected TogetherAI extraction may send received documents to that provider and incur charges. No automatic send, approval, accounting change or payment occurs.

This release adds durable per-item retry progress and stable authorization identities without replacing case records. On upgrade, an existing capture opt-in will need to be enabled again once. Re-authorizing an account also requires fresh opt-in; ordinary token refresh does not. Retain the pre-update snapshot and configuration before starting the new release.

## Acceptance before release

Record the revision, date and actual results. Previous local test counts do not validate a new revision or a live provider.

1. Run `make -C backend release` from the repository root in the configured development environment. It runs `make -C backend check`, `make -C backend walkthrough` and `make -C backend stress`: Python/JavaScript checks, guarded frontend build and regressions against temporary databases; a 37-invoice representative HTTP walkthrough (36 seeded plus one imported) with controlled agent protocol responses; then actual local HTTP for 5,000 fabricated invoices, 50 pages, 32 reads at concurrency 8 and 16 same-key approvals. These verify duplicate prevention, source review, one committed approval, unchanged payment/amount, original handoff bytes, restart persistence, authorization and SQLite integrity. This is bounded local acceptance, not live model accuracy, a 50,000-invoice benchmark, twenty-user or hosted/provider performance evidence.
2. Confirm the intended runtime mode, frontend sign-in/API origin, unauthorized-record rejection and HTTP 200 from `/api/health`. An unavailable database, incomplete schema or backup failure must return 503. In private mode verify an empty initial workspace; in public mode verify fabricated provenance and no connected send/update.
3. Use permitted test records to prove a mismatched/unreviewed/under-covered PO stays blocked, then review a supported PO, approve and inspect the brief/ZIP, original attachment and frozen approval. Preparation must leave payment and source invoice amounts unchanged.
4. Restart the backend with the same volume and configuration. Verify sign-in, records, attachments, approval/history and grant access still work. Confirm a readable backup exists and separately test recovery from an off-site copy.
5. For a connected release, run the sandbox/test-mailbox walkthrough in [CONNECTIONS.md](CONNECTIONS.md): verify the exact send receipt, capture a same-thread reply and attachment with its case closed, repeat/restart without duplicates, stop capture and confirm errors are recoverable, then human review, separate mapped-reference confirmation and remote readback. Check pending/unknown-action reconciliation before any retry. Record live checks separately from fixture results; keep each untested provider marked unverified.

## Persistence, rollback and recovery

The SQLite database contains original attachment bytes, case/history records and encrypted grants. Keep its volume through redeployments. Automatic backup checks run at startup and hourly; one verified snapshot per UTC day is retained in `/data/backups/`. Existing daily snapshots are checked read-only for SQLite integrity and the core workspace schema; a corrupt, empty or invalid snapshot degrades health and is preserved for recovery inspection. Existing daily snapshots are not replaced and old snapshots are not deleted automatically. Check capacity and keep a separate off-site copy; the daily snapshot can omit later changes.

Before an update, retain the last known working release/configuration and a fresh verified SQLite snapshot. Do not copy an actively written database file alone; use SQLite's backup operation or stop the backend first.

For an application rollback, stop new work and redeploy the last known compatible frontend/backend pair with the same volume, database path, credentials and token key. If schema compatibility is uncertain, inspect a restored copy first; do not start older code against the only stored database.

For database recovery, stop the backend. Move the current database and any matching `-wal`/`-shm` sidecars into a recovery folder, preserving them. Verify the chosen snapshot with SQLite `PRAGMA quick_check`, restore it to the configured database path with owner-only permissions and ownership readable/writable by UID 10001, and restart with the preserved credentials/token key. Recheck health, records, attachments and connections. Restoring loses changes after that snapshot; it cannot undo Gmail sends or QuickBooks updates. Reconcile provider receipts and refresh remote state before resuming connected actions.
