# Verification

Updated 10 November 2026 after the investigation setup, case copy and stage transition pass. The approved folio and document layout remain in place. These are local verification results.

The default remains `zai-org/GLM-5.3-Flash`, using the official endpoint and model-specific low-effort settings checked 5 November. The release includes the public source-file workflow, the final interface refinement and durable extraction reservations. No live inference or 50,000-invoice acceptance is claimed.

## Release checks

Command: `make -C backend release NODE=/path/to/node22`, using Node.js 22.23.2 and Python 3.11.

| Check | Result |
| --- | --- |
| Backend regression tests | 221 passed |
| Frontend workflow VM/static groups | 96 passed |
| Bounded pagination regression groups | 13 passed |
| Interaction regression groups | 8 passed |
| Isolated frontend build groups | 12 passed |
| Python compilation and JavaScript syntax | Passed |
| Frontend static build | Passed |
| Locked frontend install, `npm ci --ignore-scripts` | Previously passed; dependency inputs unchanged, not rerun in this pass |
| Container startup/configuration checks | Included in full suite; filesystem and identity changes mocked |

The startup tests verify fixed-mount handling, symlink rejection, no child traversal, supplementary-group clearing, UID/GID drop before execution and failure paths that never start the API. Docker defaults to a non-root user; Railway's root-owned mount is prepared by the launcher before the application starts.

An earlier release attempt exposed a test-fixture cleanup race: its inbox worker had not been stopped before temporary storage was removed. Teardown now stops both workers before deleting test storage; the complete release rerun passed. No application safeguard was disabled to make the test pass.

## Extraction quota correction

A rejected public upload could previously roll back an extraction reservation after provider dispatch. Reservations now commit before the call, outside the attachment transaction, and remain accounted if the attachment later fails. Four added regressions cover exhausted-cap rejection across restart, committed quota and no SQLite write lock during dispatch, stale versions after dispatch, and four exact concurrent retries across two service instances producing one call. The tests use controlled provider responses only. No schema migration or HTTP API change was needed.

Seventeen investigator tests exercise actual model-selected tool loops using controlled TogetherAI protocol responses, not a live LLM. They cover different investigation paths, exact inspected citations, cross-case and write-tool rejection, hostile document instructions, unreviewed evidence and accounting holds, quota reservations, failure recovery, concurrent same-key requests, interrupted-run reads at capacity and stale invoice/shared-PO/integration state. A separate HTTP test exercises the loop through the authenticated route and recovers its result without another provider dispatch. These are protocol and safety regressions, not a model-accuracy evaluation.

## Representative HTTP walkthrough

`make walkthrough` starts an isolated local HTTP service with the 36-case public pack and imports one additional invoice from the downloadable source bundle, for 37 records. Existing workspace data is untouched.

- Authenticated ZIP download, CSV intake, duplicate reimport, recorded reply and actual PO upload passed.
- Approval remained blocked before source review. After review, the differing invoice/PO references remained explicit.
- The real investigator tool loop completed three model rounds and retained six tool calls, using controlled provider responses—not live inference. Retrying recovered the same run without another dispatch; investigation did not mutate the invoice or approve it.
- A human correction invalidated the old finding. Repeated approval committed once; the handoff retained original PO bytes, the original invoice reference and the approved correction separately.
- The under-covered existing case stayed blocked. Restart retained the approved case, investigation history and unpaid payment state; SQLite integrity was `ok`.

The source bundle contains fictional invoices, readable POs and instructions, not a stored agent report. It demonstrates a repeatable review-to-handoff workflow, not client outcomes or production capacity.

## Bounded HTTP stress exercise

A temporary private database was populated through local HTTP with 5,000 fabricated invoices. No user database, provider credential or live account was used.

- Read all 50 pages; 32 reads at concurrency 8 completed.
- Sixteen same-key approval requests committed one approval.
- Approved handoff retained the original PO bytes, source amount and unpaid state.
- Unauthorized and cross-origin reads were rejected; SQLite integrity was `ok`.
- Observed maximum page response: 6 ms. Maximum concurrent-read response: 61 ms.

An earlier run encountered a connection reset during the parallel-read burst. Python 3.11's default five-connection accept backlog was raised to 32 and regression-covered; the subsequent complete release exercise passed without client retries.

These are local observations, not a service-level guarantee. Only one case received document evidence; this was not a 5,000-document, twenty-user, provider-quota or hosted-networking test.

## Interface and publication checks

An earlier paging checkpoint imported 246 fictional records into a temporary public database containing the then-current six cases, for 252 actual test records. It used no stored user data or provider account. At 1280×720, the document remained 720px tall, only 10 rows were rendered, and paging stayed above the footer while the list scrolled internally. The 50-row option stayed bounded; a direct jump to page six returned just the final two records. At 390×844 and 320×740, document height matched viewport height, width did not overflow and paging stayed above the footer. A shorter 1280×600 desktop window also remained 600px tall, with a 255px list and paging above the footer. Returning from an opened case restored page three, its 745px list scroll and selected-row focus. These are historical local UI observations, not client workload or capacity claims.

Thirteen additional regressions exercise real browser paging functions with controlled API responses: offset/size bounds, input completion and page jumps, shrinking totals, query resets, stale/session responses, failure recovery, focused controls during quiet refresh, approval-shortened page refill, draft confirmation before navigation and restored list focus/scroll. The existing 85 workflow groups, nine build groups, 203 backend tests, representative walkthrough and 5,000-invoice HTTP exercise passed in the release run. Frontend checks/build are rerun for final presentation-only adjustments.

The preceding visual checkpoint inspected desktop and phone sign-in, the worklist and source review. A later 320×640 check retained a 320px document width with no header overlap. At 1440×900, expanded Checks showed four passing comparisons and one reference conflict; a supported correction reached Handoff while remaining unpaid. The final browser approval was not completed in that rerun.

Regressions cover unchanged-refresh DOM/focus retention, dirty-edit protection, deliberate-only motion, reduced-motion and missing-animation fallbacks, repeated provider errors and recovery, and one visible copy of each conflict. The inline Investigation section additionally covers explicit consent, stored-run recovery, stale action suppression, model-content escaping, source links, and retention of unfinished review forms. Agent-only setup downloads retain active provider configuration without enabling model extraction on every upload. A manually recorded reply now advances to one Attach PO action instead of repeating the request; its file chooser performs no Gmail sync or send.

The final isolated browser pass inspected the refined worklist and evidence review at 1280px, 390px and 320px widths, plus handoff at 768px. At 320px, measured document width and scroll width were both 320px and the header retained single-line account actions. Phone sign-in also retained a 320px document width, 272px-wide submit control and readable fields. Keyboard arrow navigation preserved an unfinished correction and kept exactly one stage panel visible. A supported correction reached the confirmation dialog and approved handoff, with the original amount and unpaid state still visible. Its ZIP downloaded through the browser. At 1280×720 the primary handoff download was above the fixed footer. The preview used temporary records and no provider credentials; no live browser agent run is claimed. Reduced-motion behavior is regression-covered, not an operating-system preference test. Frontend regressions and the static build were rerun after the final handoff-layout adjustment.

The refinement uses locally rendered document materials, file-index tabs, restrained row feedback and a bounded native dialog entrance. It adds no runtime dependency, remote visual asset, looping effect or new authority. Impeccable polish/Operate/craft guidance, Taste minimalist guidance and Awesome DESIGN.md informed the pass; no installed Impeccable launcher or detector run is claimed.

A bounded source-publication scan found no obvious provider keys or private-key material among publication candidates. Databases, backups, credentials, generated builds and private engagement/design notes remain excluded. This is not a security penetration test or a secret-scanning guarantee.

## Interaction checkpoint

The local-only interaction layer adds directional case entrances, exact-source emphasis, retained-node source expansion, progressive native disclosure motion, request-specific pending labels, non-reflowing notifications, guarded Cmd/Ctrl K search and PO file selection by picker/drop. It contains no network, extraction or approval tool and introduces no runtime dependency. Success notices expire after seven seconds except while hovered/focused; errors remain until dismissed.

The isolated browser pass verified source expansion with an edited customer field, cancelled keyboard navigation preserving that draft, exact-source focus, source review advancing to Handoff, real picker selection without submission, the attachment leave guard and accepted navigation focusing search. A supported reference correction advanced to the explicit approval dialog and approved billing handoff, retaining the original amount and unpaid state. The evidence desk and attachment controls were inspected at 390px and 320px; document scroll width matched viewport width at both sizes. The fixed footer remained clear of the attachment action and primary desktop handoff download. Drag/drop assignment, invalid-selection retention, pending-label restoration and reduced/unsupported animation paths additionally have VM regression coverage; operating-system drag/drop and reduced-motion preference changes were not exercised in the browser. No provider keys or existing workspace records were used.

The subsequent visual craft pass adds locally authored paper grain and a decorative SVG handoff packet, more detailed CSS 3D cover/metal/hinge materials, sharper ledger rules and a narrow-phone case header. The packet appears only with supported handoff evidence and carries no document data or approval stamp. Filing feedback is invoked only after a confirmed human approval, not on render/polling. Added checks cover those conditions, finite/reduced-motion feedback, and unchanged, script-free, local SVG build delivery. No dependency or backend authority was added.

Browser inspection covered sign-in at 1280×720, 721×640 and 320×640, plus scrolled short-landscape access at 844×390. The local vector assets both returned HTTP 200. The supported handoff remained usable on desktop, then approved through the explicit dialog while retaining the original $15,600 amount and unpaid state. At 320px the customer had its own full-width header line, invoice amount remained readable, handoff actions and the illustration stayed in normal flow above the reserved footer, and document width/scroll width were both 320px. Reduced-motion fallback is regression-covered, not an operating-system preference test. Final frontend checks/build were rerun after the narrow-tab spacing adjustment; no provider credentials were used.

The preceding visual pass included 92 workflow, 13 pagination, seven interaction and 12 build groups. The final customer-copy/case-pack pass described below supersedes those counts. No additional scale, live-model or hosted acceptance claim follows from interface changes.

## Final customer view and representative cases

The browser title is exactly InvoiceClear; AI Agent for Invoice Resolution is the subname at sign-in and in the workspace wordmark. Public navigation omits Connections. Unconfigured investigation has no customer setup action or empty strip, while saved findings, pending requests and real errors remain inspectable. The permanent sign-in access caption is removed; sharing warnings appear only at entry/import/upload. Three added frontend groups cover these visibility and retention boundaries; the final total is 95 workflow, 13 pagination, seven interaction and 12 build groups.

The fresh pack contains 36 fictional invoices across eight legal accounts, 35 two-page POs and 16 retained manual messages: six missing, four unreviewed, fourteen mismatched, ten ready and two constructed approved allocations. Six new backend tests verify exact rounded billing-line totals and net terms; retained original bytes and source-linked fields; expected customer, currency, reference, revision and one-cent failures; genuine scoped reservations and release on reopening; zero seed provider calls/results; empty-only startup, unchanged occupied public/legacy records and empty private workspaces; and bounded form-feed TXT pagination. The complete 209-test backend suite passed. A stale walkthrough recipient expectation was updated to match the new reserved address; the complete release rerun passed, including the 37-record walkthrough and independent 5,000-invoice exercise.

The pack is a representative exception dataset, not 50,000 imported documents, a production-scale validation, measured client outcomes or evidence of the original client's industry. Three obsolete public PO fixture files were replaced with matching current-account files; their prior contents were backed up outside the repository. No stored user database was inspected, reset or replaced. Use a distinct new public database path to display the current pack.

## Complete recording artifacts

The current `staffing-36-v3` source ZIP contains 45 fixed files: all 36 invoice-entry rows, one separate import case, 38 readable two-page PDFs (35 retained plus three incoming), a revision manifest, 64 source billing lines, 16 retained correspondence records and two guides. The CSV template matches the separate import row exactly. All 38 PDFs / 76 pages were rendered with Poppler; contact sheets and representative full-size pages were visually inspected without clipped text or footer overlap. ReportLab 4.4.9 is authoring-only: production serves the committed PDFs and uses the existing bounded PDF parser. No runtime dependency was added.

Parity regressions compare every retained attachment byte/hash, two-page extracted source and four suggested fields with the matching ZIP PDF and manifest. Full-ledger CSV import into an empty private workspace creates 36 source invoices, a repeated import skips all 36, and neither import manufactures documents, reviews, correspondence or approvals. Exact known source rows receive rich fictional provenance; altered and unrelated rows do not. Occupied public/legacy and private-startup rules remain unchanged.

Prepared CSV retains its original thirteen columns and one-row-per-active-approval contract, appending frozen source context, structured billing lines, approval identity and source disclosure. Rich-source handoffs include a separate line-item CSV plus the original approved PDF. Both source-seeded and CSV-imported cases retain source disclosure in the billing brief/note. Five additional artifact tests cover both modes, modified-row rejection, exact original PDFs and frozen snapshots, new-column formula protection, escaped HTML and legacy records without invented line items.

The complete release rerun passed: 216 backend tests; 96 workflow, 13 paging, seven interaction and 12 build groups; the 37-invoice HTTP walkthrough; and the 5,000-record stress exercise. The walkthrough imports and reviews the actual PDF, verifies duplicate full-ledger import, retains one approval and checks the PDF, line-item CSV, disclosure and source lines in its handoff. Controlled agent protocol responses were used, not a live TogetherAI request. One earlier release attempt could not bind its temporary HTTP servers under the sandbox; after localhost permission was granted, all HTTP checks passed without application changes to bypass safeguards.

The expanded billing brief was inspected in the browser at 1280px and 320px. At 320px, both the document and scroll width were 320px, with both tables inside a 272px content width. The main app layout/motion was not changed. Temporary preview servers and tabs were closed. Use the separate `invoiceclear-public-202611-artifacts.sqlite3` path for this edition; saved databases and existing TXT attachments are not silently replaced.

## Public repository and product-name checkpoint

The visible subname is AI Agent for Invoice Resolution; the browser title remains InvoiceClear. The approved artwork, finite motion and case workflow were retained. At 320×640, sign-in kept a 320px document width and its submit control ended at 586px above the 591px footer. Public and private worklists were inspected at 320px. Allowing the longer workspace subname to wrap kept its right edge inside the brand area and clear of private Connections/Sign out controls, without horizontal overflow.

The publication candidate root now matches the inspected Charge reference: frontend/, backend/, README.md and .gitignore. Operating guides moved to backend/docs, with a portable backend/Makefile; existing local root shortcuts and private notes remain ignored on disk. Four publication regressions verify local README/guide links, the backend Makefile from a pathname containing spaces, backend-only provider setup and publication exclusions. The public README and About fields describe the supported PO workflow, not autonomous payment collection or measured client ROI.

`make -C backend release` passed 220 backend tests, 96 workflow, 13 pagination, seven interaction and 12 build groups, the 37-invoice walkthrough and the 5,000-record local HTTP exercise. Maximum page/concurrent read observations were 8/58 ms in this run, not a hosted latency guarantee. All frontend groups and the build passed again after the final wrapping correction. A final read-only publication scan found no matching common private-key, GitHub-token, AWS-key or sk-prefixed key patterns in the candidate text files; this limited scan is not a secret-scanning guarantee. No secret files or user database was read. Temporary preview tabs and servers were closed.

No GitHub repository, remote metadata, commit, push or deployment was created. TogetherAI, mounted-volume and hosted acceptance still require the user's actual configuration and live checks.

## Investigation setup and interface checkpoint

A backend key alone enabled investigation in an isolated HTTP test. Sign-in, queue, case and readiness requests made zero model calls. A confirmed investigation completed with controlled provider responses and an identical retry returned the saved run. The 37-invoice walkthrough and 5,000-record HTTP exercise passed, with one committed approval across 16 same-key requests in the latter.

The local browser check opened an invoice, switched from PO review to correspondence and back, and inspected the case at 1280×720 and 320×640. The phone layout had no horizontal overflow. The TogetherAI evidence confirmation appeared and was dismissed. No live provider request was made. Interaction regressions cover reduced motion, rapid stage switching and unavailable or throwing animation APIs.

## Still requires deployment acceptance

The Docker image was not built locally because the Docker daemon was unavailable. Startup tests do not replace a real container/volume check.

Vercel/Railway deployment, mounted-volume startup and ownership, restart persistence, off-site recovery, live QuickBooks/Gmail/TogetherAI behavior, provider approval and production twenty-user operation remain unverified here. No measured savings or faster-payment claim is made.

Follow [RELEASE.md](RELEASE.md) for hosted and permitted-account acceptance, and [PUBLISHING.md](PUBLISHING.md) for the manual GitHub upload.
