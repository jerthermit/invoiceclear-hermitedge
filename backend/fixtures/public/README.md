# Public source files

These are fictional, permitted recording/evaluation records, not client files or business outcomes. Companies, contacts, authorizations and correspondence are constructed for this edition. They do not establish the original client's industry, invoice volume, savings or production capacity. The application performs real imports, document retention, checks, human approvals and downloads on these inputs. Agent investigation requires the configured TogetherAI account; no model result is bundled here.

## One complete case

1. Import `invoices.csv` to add `STA-2026-2101`. Reimporting skips the same invoice without overwriting it.
2. Open the case and inspect the request. For an offline walkthrough, copy the request; this is not a Gmail send receipt.
3. Record this customer reply manually: "Please use MVH-2026-2128 for August clinical staffing. The ledger reference MVH-2026-2181 was transposed. The attached PO authorizes up to 320 hours at USD 75.00. This is authorization, not payment confirmation."
4. Attach `MVH-2026-2128.pdf`. Approval remains blocked until staff confirm the source fields.
5. Confirm the PO details against the source. With TogetherAI enabled, choose Investigate once: the agent can inspect the invoice, reply, PO and deterministic checks, then propose the next step. Its result is not approval. Without a key, continue the source review manually; no simulated agent report appears.
6. Review the reference difference, save the supported correction to `MVH-2026-2128`, then approve the billing handoff.
7. Download the handoff and inspect its brief, original PO and approval record. The original invoice reference and amount stay intact, and payment remains unpaid.

## Complete source bundle

- `invoices.csv` is the separate 2101 recording case. The browser's CSV template contains this exact row.
- `all-invoices.csv` contains all 36 original invoice-entry records. Reimporting into a fresh current public workspace skips all 36 existing rows. In an empty private workspace it imports invoice facts only: no attachments, reviews, replies or approvals are manufactured.
- `purchase-orders/` contains the exact 35 PDFs retained by the fresh public cases, grouped by invoice. `purchase-orders.csv` maps their revision, legal customer, currency and authorized ceiling. Keep every revision; never replace the latest with an older convenient document.
- The three PDFs at the ZIP root are incoming documents for 2101, 1041 and 1042. They are intentionally not already attached to those missing-document cases.
- `billing-lines.csv` contains source quantities, rates and cent-rounded amounts for the 36-case ledger plus 2101. `correspondence.csv` contains the 16 retained case messages. It is reference material, not evidence of a Gmail send or automatic reply import.
- Four missing-document cases intentionally have no supplied PO. Their holds are part of the workflow, not missing bundle files.

## Outputs after approval

Export prepared downloads one CSV row per active approved handoff, retaining the original columns and appending billing context, structured source lines, approval identity and provenance. This is not an invoice-entry import file.

Each case handoff ZIP contains the print-styled billing brief, billing note, frozen approval JSON, its exact approved original PDF, and a separate `billing-lines.csv` when the retained source has line detail. All refer to that one case. Source amounts and payment status stay unchanged; this package is not a payment receipt or confirmation of an accounting update.

## Rebuild the authored pack

The repository commits the PDFs and CSVs; deployment does not require ReportLab. To regenerate them after an intentional source edit, use an authoring Python with `reportlab==4.4.9` and run `python backend/scripts/build_public_pack.py` from the project root, then run the public-file and seed tests. The three retained TXT files are authoring sources for the incoming PDFs, not duplicate ZIP downloads. The generator uses invariant PDF metadata and never opens a workspace database or provider connection.

## Existing cases

- `MVH-CLIN-2026-0801.pdf` supplies the missing PO for `STA-2026-1041` in the current pack.
- `LRL-TEMP-2026-082.pdf` supplies the missing PO for `STA-2026-1042` in the current pack.
- `STA-2026-1043` demonstrates a reference conflict with an already retained PO.
- `STA-2026-1044` must remain blocked: USD 9800.00 exceeds the separate USD 8000.00 PO. A verbal assurance or model recommendation does not increase it.
- `STA-2026-1045` has supported evidence ready for handoff approval.
- `STA-2026-1046` requires human source review before its fields support approval.

Existing records are never reset for recording. If you already completed a case, inspect its handoff or deliberately reopen it through the normal review control. These files demonstrate a representative workflow, not a 50,000-invoice capacity benchmark or a live accounting/mail integration.

The current `staffing-36-v3` pack is inserted only into an empty public workspace. A populated public database, including the earlier six-case edition, keeps its original records and receives no replacement pack on restart. These current matching source files must not be substituted for an older case with a different legal customer. To preview this edition, the host must explicitly choose a distinct new public database path. Private workspaces remain empty. `CASE_GUIDE.md` maps the current cases, review states, arithmetic and assumptions.
