# Focused workflow contract

Single finance workspace. Missing and mismatched customer purchase orders only. Manual entry/CSV and PDF/TXT uploads remain usable without integrations. In private mode, QuickBooks Online provides linked invoice records and Gmail carries approved requests and their recorded thread replies. Preparing a resolution changes only its internal case state; immutable intake fields and local unpaid payment state remain unchanged. Separate explicit consent controls email sending and the mapped accounting reference update. Observed QuickBooks balance/payment state is not a payment created by InvoiceClear or independent proof of funds received.

## Runtime and routes

`GET /api/runtime`: `{mode: "demo"|"private", demo_credentials?: {email,password}}`.
`POST /api/session`: `{email,password}` -> `{access_token,expires_at}`. Bearer authentication protects every other business route in both modes. `POST /api/session/logout` revokes the token. `GET /api/health` has no private records.

`GET /api/invoices?view=open|prepared|all&q=&offset=0&limit=50` -> `{invoices:[summary],total,offset,has_more}`.
`GET /api/invoices/{id}` -> full detail (unwrapped).
`POST /api/invoices` accepts `{invoice_number,customer,customer_email,amount,currency,issue_date,due_date,po_reference,source_ref}`. Amount is a decimal string. Returns detail.
`POST /api/invoices/import`: `{csv:string}` -> `{created,duplicates}`. CSV columns match the entry fields. Duplicate invoice number/customer identities must not overwrite existing records; conflicting duplicates reject the entire import.
`POST /api/invoices/{id}/documents`: `{filename,content_base64,expected_version,idempotency_key}` -> detail. PDF/TXT only, max 5 MB; retain original bytes and text pages. Extracted fields are suggestions; upload never implies review. Corrupt, unreadable, encrypted, oversized or scanned-only documents must safely reject or explicitly hold for manual review.
`POST /api/invoices/{id}/actions`: `{action,expected_version,idempotency_key,parameters}` -> `{invoice:detail,duplicate:boolean}`.
`GET /api/invoices/{id}/request`: `{to,subject,body}` (also included in detail).
`GET /api/invoices/{id}/handoff`: authenticated ZIP for an active approved resolution only. Contains a readable billing instruction, frozen approval JSON including source provenance, and the exact approved original PO. Verify document identity and SHA-256; reject missing/damaged evidence. Reopening disables download until another valid approval. This read-only route performs no email, accounting or payment action. ZIP paths are fixed, not user-controlled filenames.
`GET /api/documents/{id}/download`: authenticated original attachment.
`GET /api/export.csv`: one row per active prepared resolution, spreadsheet-safe UTF-8 CSV. Preserve the original thirteen columns and append source intake/disclosure, account, branch, cost center, service period, terms, JSON billing lines and approval identity from the frozen snapshot (legacy approvals fall back only for missing retained fields). Never export as payments or pretend the accounting system was corrected.

`GET /api/public/source-files`: authenticated public-mode-only ZIP with a fixed allowlist of fictional source files and recording instructions. Private mode returns 403. This read-only route imports nothing and never reads workspace records, accepts caller-selected paths or contacts a provider.

`GET /api/invoices/{id}/investigation`: authenticated `{available,reason,latest}`; reads retained local status only, never contacts a model. Full invoice detail includes this object as `investigation`.
`POST /api/invoices/{id}/investigate`: `{expected_version,idempotency_key}` starts one bounded investigation and returns its retained run. Exact retries recover that run; they do not repeat provider calls. The model has no write or approval tool.

`GET /api/connections`: authenticated configuration/connection status; never credentials. `POST /api/connections/{qbo|gmail}/start`: backend-generated OAuth URL. `GET /api/connections/{provider}/callback`: public provider callback protected by one-use, expiring consent state bound to workspace credentials/configuration. `POST /api/connections/{provider}/disconnect`: end local access and attempt provider revocation, preserving case records.
`POST /api/connections/qbo/sync`: `{offset?:integer}` -> bounded invoice-import batch with created/refreshed/skipped counts and pagination. Sync never overwrites a manual invoice or invents a billing address.
`POST /api/invoices/{id}/send-request`: `{expected_version,idempotency_key}` -> confirmed Gmail receipt for the exact server-generated request; requires a linked invoice and explicit user approval.
`POST /api/invoices/{id}/sync-replies`: intake from the app's confirmed request threads only; bounded source messages/documents, exact intended sender correlation, duplicate prevention and manual document review. Mailbox-record correlation is not cryptographic sender authentication.

Reply intake persists fair rotation across request threads, messages and attachments. A malformed message or failed attachment cannot consume every pass indefinitely. Unreadable messages retain a sanitized held receipt with source identities and remain retryable from Gmail; they are not counted as captured customer replies. A missing-PO case with such a hold shows Message needs review in its local worklist. Permanent file errors remain visible while later valid evidence progresses. Re-authorizing even the same account changes its authorization identity and requires fresh capture opt-in; routine access-token refresh does not.
`GET /api/capture`: authenticated local status only; no provider calls. `POST /api/capture`: `{enabled:boolean}`, private-only explicit opt-in for background reply/document intake across open linked invoices with confirmed app request threads. Default off. Maximum 10 cases per pass, with a 60-second idle interval; larger queues take multiple passes. Persistent fair cursor and an OS-held database-specific claim prevent overlapping workers. Pending/unknown actions and prepared cases are excluded and rechecked transactionally. New attachments stay unreviewed. No sending, approval, accounting update or payment is triggered. Grant/account, workspace or extraction-configuration changes disable capture until another opt-in. An in-flight case check may finish after stopping. Status reports last-pass counts, holds and bounded sanitized errors, not cumulative business results.
`POST /api/invoices/{id}/apply-reference`: `{expected_version,idempotency_key}` -> sparse QuickBooks update of the configured invoice PO CustomField only, requiring active reviewed approval, current source/SyncToken, permitted company and verified readback. No payment, amount, line-item or unrelated custom-field changes. Does not upload the PO file to QuickBooks.
`POST /api/invoices/{id}/refresh-accounting`: verified read of the linked source.
`POST /api/invoices/{id}/reconcile-request` and `/reconcile-reference`: receipt-only recovery of pending/unknown provider actions. Never blind resend/repost. Provider receipt absence leaves an explicit hold.

JSON errors: `{error:{code,message}}` with correct 400/401/403/404/409/413/415/429/500 status.

The database is durably bound to its public/private mode before seeding. A conflicting mode or ambiguous legacy provenance stops startup without converting records. Public business writes have aggregate limits of 128 MiB SQLite allocated pages and 50,000 persisted rows across invoices, documents, replies, resolutions, audit, idempotency and agent runs. Crossing either limit rolls back the whole transaction with `public_capacity` (429). Reads, downloads, exact idempotent retries and restart remain available. These are public-host resource limits, not private client capacity or a rate/DoS guarantee. Backups need separate capacity monitoring and off-site retention.

## Detail shape

```
{
 id, invoice_number, customer, customer_email,
 amount_cents, currency, issue_date, due_date,
 po_reference, proposed_po_reference, source_ref,
 payment_status: "unpaid", status, version, blocker, work: {kind,label},
 checks: [{code,label,status:"pass"|"fail"|"hold",invoice_value,document_value,message,document_id,source_page,available_cents?,reserved_cents?}],
 documents: [{id,filename,sha256,pages:[{number,text}],suggested_fields,fields,evidence,review_evidence,reviewed,extraction_method,extraction_error,uploaded_at,reviewed_by,reviewed_at}],
 replies: [{id,text,received_at,source:"manual"}],
 resolution: object|null, billing_instruction: string,
 integration: {accounting:object|null,messages:array},
 request: {to,subject,body}
}
```

HTTP summary fields are the scalar invoice fields above (no documents/replies/checks), plus `blocker` and `work: {kind,label}`. Work projects recorded request/reply, source and receipt holds into one next step; it is not a model decision or a completed payment. The full HTTP detail uses the same projection. Worklist reads never poll providers. Status values: `missing_po`, `needs_review`, `mismatch`, `ready`, `prepared`.

Worklist view/search/count and page selection happen before evidence hydration. Only displayed records run PO checks. Search retains Unicode casefold and literal substring matching across the six original searchable fields. Connected context is loaded in one page-bounded batch, without email bodies, attachment payloads or provider calls.

The browser requests and retains one page of 10, 25 or 50 summaries, replacing rather than appending rows. Previous/Next and validated page entry use the existing offset/limit API. Shrinking totals trigger a bounded refetch of the last valid page. Search/filter/page-size changes reset to the first page; page failures and stale responses cannot overwrite the last usable page. Case navigation retains list position, confirms unfinished edits before changing pages and refills approval-shortened pages before advancing. These are presentation changes, not a new backend capacity claim or record migration.

Local interaction helpers have no network or approval authority. Expanding source text and switching workflow stages preserve retained review fields. Cmd/Ctrl K uses the same guarded queue-return path as Back. Selecting or dropping one nonempty PDF/TXT up to 5 MiB marks an unfinished attachment; it never submits, extracts or calls a provider. Invalid drops preserve an existing selection. Submission still uses the authenticated, versioned document route. Native motion is progressive and reduced-motion aware; notifications acknowledge actual application results, never simulated progress.

Document `evidence` links extraction suggestions to `{page,snippet}` by field; `review_evidence` links the separately confirmed fields to their source. Extraction method is the actual configured `rules` or `together` path, not proof of successful extraction. Until human review, `review_evidence` and `fields` are empty, `reviewed` is false, and `reviewed_by`/`reviewed_at` are null. Upload and review timestamps are retained records, not inferred workflow events.

Full detail adds linked accounting state (company/environment, external ID, observed reference/balance, sync time, source-change/error and verified update receipt) and message receipts/provenance. Provider identities are checked again inside the grant lock at dispatch, not only before a network request.

## Actions

- `review_document`: parameters `{document_id,fields:{po_number,customer,currency,amount},note?:string}`. Human-approved source fields. Require a correction note if changing an extracted non-empty field. Record reviewer, source pages and original suggestions; never change invoice amount/payment.
- `propose_reference`: `{po_reference,note}`. Records an explicit proposed invoice-reference correction, never rewrites the source reference. Note must be 10–500 characters.
- `record_reply`: `{text}`. Records a manual customer reply, not verified email ingestion, and does not approve anything. Text 1–5000 characters. A missing-PO case with a recorded reply advances to Attach PO; it does not repeat the request or trigger Gmail sync. The earlier request remains available in a collapsed disclosure.
- `approve_resolution`: optional `{note}`. Requires reviewed evidence and all deterministic checks passing at the current version. Freeze the billing instruction, correction reason, reviewer/date, source invoice identity/provenance, checks and document hash, exactly once. A prepared case is not paid or externally resolved.
- `reopen`: `{note}`. Reopens an internal prepared resolution for review, preserving prior audit, not undoing an external transaction.

Compare normalized customer identity, PO reference (use explicit proposal if present), exact currency, and PO amount coverage. Account for prepared allocations within this workspace for the same PO/customer/currency; do not claim verified external PO consumption. Re-evaluate under a write transaction at approval. New documents or corrections on a prepared case require reopening.

Connected approval also verifies the current accounting source; changed, partly settled, zero-balance or unavailable sources stay held. Pending/unknown external actions hold local mutations under the same SQLite write transaction. Durable operation records precede network dispatch, retain evidence/receipts and reject conflicting retry intents. Company/mailbox switching cannot rebind existing records. An unknown send is reconciled against its stored Message-ID; do not claim global exactly-once delivery.

Reservation rechecks idempotency keys and equivalent confirmed intents inside the write transaction. Competing confirmations cannot dispatch the same request or approved reference twice; a conflicting key is rejected, and an unconfirmed operation stays held for receipt verification.

## Readiness, history and delivery

`GET /api/readiness` is authenticated: secret-free setup requirements, callbacks, actual storage/backups, quota and last explicit checks. GET never contacts providers. It exposes no keys, client IDs or database paths.

`POST /api/connections/{qbo|gmail|together}/verify` is private-only. QuickBooks reads invoice access without importing; Gmail checks mailbox identity; TogetherAI extracts four supported fields from a fixed non-customer PO. One check at a time, with a per-provider 60-second cooldown. No case, email, accounting update or approval is created. Observations are process-local and tied to current configuration/account; they are not provider app approval or proof of end-to-end delivery.

`GET /api/invoices/{id}/history?limit=20&before=seq` returns `{events,has_more,next_before}`: actual audit sequence, actor, timestamp, summary and allowlisted details only. Reverse chronological, exclusive cursor, limit 1–100. No raw provider/model bodies or invented intake events.

The approved handoff ZIP also includes `billing-brief.html`: escaped, offline and print-styled, with a fixed link to its adjacent original PO. `GET /api/invoices/{id}/brief` downloads a standalone copy with no dead relative attachment link. Both routes require active approval and intact matching document hashes; reopening disables them. Neither route changes business state.

Uploads and checks share 100 attempts and 500,000 accounted tokens per UTC day. Each dispatched attempt reserves 100,000 tokens transactionally; valid positive reported usage reconciles down. Failed/unknown/zero-usage attempts retain the reservation, so five can exhaust capacity before the call limit. This is quota accounting, not billed usage. Empty/oversized model input and missing credentials are held before reservation. Source evidence stays usable for manual review.

Extraction reservations and reconciliation commit separately from document attachment writes; an upload rejected for capacity or a changed version cannot roll back a dispatched attempt. No SQLite write transaction spans extraction network I/O. Uploads serialize per database within the single backend process to preserve exact concurrent retries across service instances, then recheck version, duplicate, prepared-state and external-action gates before storing evidence. This is not a multi-replica extraction guarantee.

## Model boundary

TogetherAI optional via backend `TOGETHER_API_KEY` and `INVOICECLEAR_PROVIDER=together`. Default rule extraction supports labeled PO text. Model suggestions must validate schema, explicit source snippets/pages, number/currency bounds and absence of invented evidence. Missing evidence/provider failure remains unreviewed with a clear error, never silently authorizes approval. Bounded timeout/input/output/daily usage. No model-controlled money, price, invoice payment, external action or status.

## Agent investigation

One bounded agent uses the same backend `TOGETHER_API_KEY` and `TOGETHER_MODEL` as optional extraction. A nonblank key enables **Investigate** at startup unless `INVOICECLEAR_AGENT_ENABLED=false` is set; explicit `true` without a key stops startup. This does not change the extraction provider or start any runs. The interface confirms transmission of saved case records before each new investigation. Public mode supports non-confidential cases only. Disabled or unconfigured investigation has no scripted fallback.

The model dynamically selects `read_invoice`, `list_evidence`, `read_document_page`, `read_reply`, `read_checks` and `read_history`, then submits a proposed result through `finish_investigation`. Read tools are bound to one captured case; no caller-supplied invoice identifier, URL, SQL, external API or write tool is exposed. Failed tool arguments return a bounded error the model may investigate further. Document text, correspondence and history are untrusted source material, never authority.

Each run has at most five model rounds, twelve tool calls, 20,000 returned source bytes and a 20-second investigation deadline. Individual provider calls use at most seven seconds, 1,600 output tokens and a 65,536-byte response; source snapshots are limited to 120,000 bytes and message context to 44,000 bytes. Long source pages have bounded offset reads; unseen portions are not evidence. Timing limits do not constitute a hosted latency guarantee.

Read calls and field extraction share the durable daily provider budget. Each model dispatch reserves one attempt and 100,000 tokens in a committed transaction before network I/O; valid positive reported usage reconciles down. Network failures are not automatically retried and retain the conservative reservation. No database write transaction spans the agent's network call. One investigation runs at a time per workspace process, with a durable unique active case run and a 90-second recovery lease. An active process-local run is not expired by a status read. An expired orphan is projected as interrupted without a write; its interruption is recorded on a later explicit investigation request.

Run results retain `{id,invoice_id,source_version,status,created_at,completed_at,model,summary,next_step,draft,citations,trace,error,stale}`. Status is `running`, `completed`, `abstained`, `failed` or `interrupted`. Trace stores actual tool names, bounded validated arguments, source identities and outcomes—not hidden reasoning or raw provider bodies. Original provenance is retained in the immutable source snapshot. Exact request retries return the existing run, including after restart; reusing a key with another version is rejected. Staleness compares source facts, documents, replies, deterministic checks/PO allocations, recorded integration state and non-agent history, not just invoice version. Changed evidence invalidates actionable findings without overwriting it.

Completed findings require inspected invoice/check tools and exact citations from sources actually returned to the model. Reference-correction or handoff proposals additionally require reviewed current PO evidence and passing applicable deterministic gates, with no unresolved integration hold. A proposal cannot override a failed comparison. Insufficient or unsupported evidence leads to abstention or failure; neither changes business state. Findings and drafts are proposals only: the interface may navigate to existing review or copy a draft, but does not send the draft through Gmail, approve fields, save a correction or update accounting automatically. Existing protected actions and exact approved Gmail requests retain their separate authority model.

## Initial demo

The fixed public pack contains 36 fictional staffing invoices across eight legal accounts: six missing-document cases, four unreviewed cases, fourteen deterministic mismatches, ten ready cases and two constructed prepared allocations. It retains 35 two-page PDF POs and sixteen manual correspondence records. Invoice totals sum individually rounded quantity/rate lines; terms, branch, cost center and service period stay in source provenance and PO schedules. Cases cover transposed/suffix references, wrong legal affiliates/currencies, one-cent shortfalls, superseded and unreviewed revisions, and shared PO reservations. Fictional reviewer Lena Ortiz and the two valid internal approvals are explicitly identified as constructed in the guide/provenance. No verified email, accounting, payment, model result or provider receipt is fabricated.

Seeding occurs only when the public business tables are empty. Occupied public/legacy databases are not replaced, augmented or repaired by a pack revision, and private workspaces start empty. Existing records must not be reset to change presentation. Select a separate database path to inspect the new pack. Reserved .example addresses and explicit fixture provenance remain in records/exports/docs without repeated mode labels in the interface. This small representative pack is not a 50,000-invoice benchmark or evidence of client outcomes.

The public source bundle contains the complete 36-row entry CSV, the exact 35 retained two-page PDFs, a revision manifest, source billing lines and correspondence, plus one separate recording invoice and three incoming matching PDFs. The browser CSV template matches the separate import row. Exact server-known fixture rows retain rich fictional source provenance through CSV import, in either mode; modified or unrelated rows do not inherit it. Imports never attach files, record replies, review sources or approve automatically. Each rich-source handoff additionally includes a spreadsheet-safe billing-lines CSV and a compact source-line/context section in the printable brief. Both imported and seeded fictional sources retain their disclosure in handoff artifacts. It does not add another seeded case, reset existing work or contain a scripted agent report. The container ships these fixed files. Representative workflow acceptance is separate from capacity testing and live provider acceptance.

The browser title is InvoiceClear, with AI Agent for Invoice Resolution as the visible product subname. Public customer navigation omits Connections and deployment controls; private account setup remains available. An unavailable investigator has no empty strip or configuration call to action, but retained findings, active requests and actual errors remain visible. The sign-in caption is removed; public entry/import/upload forms carry a concise sharing warning at the point of record submission. TXT form-feed boundaries are retained as numbered pages, capped at twenty pages and the existing byte/text limits; original bytes remain unchanged.
