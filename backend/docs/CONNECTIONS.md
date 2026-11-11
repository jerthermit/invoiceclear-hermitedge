# QuickBooks Online and Gmail

Use a private workspace for connected records. Demo mode never sends email or changes accounting data. Start with a QuickBooks sandbox and a Gmail test mailbox; no client records are needed for the first connected walkthrough.

## Configure once

1. Create an Intuit developer app for QuickBooks Online. Register the exact backend callback `http://localhost:8100/api/connections/qbo/callback` locally, or the HTTPS Railway equivalent. Use development keys and `QBO_ENVIRONMENT=sandbox` first.
2. Create a Google Cloud web OAuth client, enable Gmail API, add your mailbox as an allowed test user and register `/api/connections/gmail/callback` on the same backend origin. Requested access is sending plus read-only mailbox access; the app reads only its recorded request threads and sent-message receipts.
3. Set private workspace credentials and the variables listed in `backend/.env.example`. `INVOICECLEAR_PUBLIC_URL` is the backend origin; `INVOICECLEAR_FRONTEND_URL` is the frontend origin returned to after consent. The application does not automatically read `.env` files; export variables or set them in Railway.
4. Generate `INVOICECLEAR_TOKEN_KEY` once with `.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`. Store it securely outside Git and retain it with recovery credentials. Losing/changing this key makes existing grants unreadable; reconnect rather than replacing stored records. OAuth client secrets and TogetherAI keys stay on the backend.
5. Choose the existing QuickBooks **invoice custom field** used for customer PO references. Set its numeric `DefinitionId` as `QBO_PO_FIELD_ID`. This is not QuickBooks' vendor PurchaseOrder entity. The field must be available and exposed through the API in your company; the app does not invent a mapping or alter another field when it is missing.

Open Connections, connect the company and mailbox through their own consent screens, then Sync invoices. Import is paginated; use Sync next batch where offered. Invoices without usable source details/billing email are skipped, not given fake contact details. Synced amounts/references remain immutable intake records; observed QuickBooks balance/reference is shown separately.

Connections is available in the private workspace, not the public customer view. Deployment settings remain in backend configuration and this guide. Check access is read-only; Check extraction uses one bounded TogetherAI request and may incur a charge. Results reset after restart or an account/configuration change. These checks do not replace the full permitted workflow below. Public investigation appears only when enabled or when a saved finding/error needs review; missing configuration is not presented as a customer task.

## Connected walkthrough

1. In your QuickBooks sandbox, use a customer with a permitted real test-mailbox address and an unpaid invoice whose PO custom field is missing or wrong.
2. Sync and open that invoice in InvoiceClear. Review the request's recipient and full text, then explicitly send through Gmail.
3. In Connections, explicitly Start capture to check open linked invoices' confirmed request threads while the browser is closed. Reply from the test customer mailbox in the same thread with a readable PDF/TXT PO. The server rotates through at most 10 cases per pass, waiting 60 seconds between passes; it does not check every invoice each minute. Alternatively, Check replies on one case. With capture off, an unedited open linked case checks once per minute while the page is visible. Intake retains mailbox provenance and does not equate a matching From header with cryptographic sender verification.
4. Review the document's extracted fields against its source. Propose a supported reference correction if needed. Missing/unreviewed/conflicting evidence cannot authorize preparation.
5. Approve and download the billing handoff. Separately confirm Update PO in QuickBooks. Only the mapped reference field is eligible; source amounts and payment state are never written. The PO file remains in the handoff and is **not** automatically uploaded into QuickBooks.
6. Refresh the source and verify the actual remote reference and balance. Zero/partial balance reflects the accounting record, not independent evidence of funds received.

The handoff includes a printable HTML billing brief alongside the billing note, frozen approval JSON and original PO. History shows recorded decisions and provider receipts. Before deploying, run `make release`, verify `/api/health` and persistent storage, test a blocked case, and verify retained records/sign-in after restart. Test off-site recovery separately; access checks alone do not verify delivery.

Capture defaults off and requires both original company/mailbox grants. Its opt-in and cursor survive a normal restart. Changed account, workspace or extraction configuration requires fresh consent. The worker reads only recorded request threads, not unrelated inbox messages; pending/unknown actions and prepared cases remain held. Last capture shows actual last-pass counts and errors. A new attachment uses the configured extractor, including TogetherAI charges and document disclosure when selected, but never becomes reviewed or approved automatically. Stopping may leave an in-flight case check to finish. Keep one backend replica on the persistent volume.

Re-authorizing the same company or mailbox also requires capture opt-in again; normal token refresh does not. Intake rotates across threads, messages and attachments, retaining unreadable-message holds and file errors while allowing later evidence through. Retry uses the original Gmail thread. If a captured reply changes the case during your save, your unfinished fields remain until you explicitly Review latest.

## Failure and release boundaries

Expired grants are refreshed server-side; revoked grants require reconnection. Stored access/refresh tokens are encrypted. Company/mailbox changes cannot silently rebind old invoice or message records.

A timeout during sending is an unknown result, not a failed send that can be retried blindly. Verify Gmail's receipt before another send. A pending/unknown external action holds local changes to its case until its outcome is reconciled. Stale QuickBooks source records or missing custom-field authority block approval/update instead of overwriting another user's work.

Public OAuth release is separate from the local test: Google classifies Gmail read-only access as restricted and describes verification/security-assessment requirements; Intuit has its own production app requirements. Do not present successful fixture tests as live provider approval or deployed-client results.

Official references: [Intuit OAuth](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0), [QuickBooks Invoice API](https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/invoice), [Google web OAuth](https://developers.google.com/identity/protocols/oauth2/web-server), [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [Gmail send](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send).
