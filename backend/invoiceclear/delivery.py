"""Offline billing brief rendered only from the retained approval snapshot."""
import csv
import io
from html import escape

from .documents import decimal_amount


def spreadsheet_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


def billing_lines_csv(snapshot):
    provenance = snapshot.get("source_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    lines = provenance.get("billing_lines")
    if not isinstance(lines, list) or not lines:
        return None
    columns = ("invoice_number", "customer", "line_number", "description", "quantity", "unit_rate", "amount", "currency", "business_account", "service_branch", "department", "service_period", "source_fixture", "source_disclosure", "approval_id")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    for index, line in enumerate(lines, 1):
        if not isinstance(line, dict) or any(not isinstance(line.get(key), str) for key in ("description", "quantity", "unit_rate", "amount")):
            return None
        values = (snapshot.get("invoice_number", ""), snapshot.get("customer", ""), index,
                  line["description"], line["quantity"], line["unit_rate"], line["amount"], snapshot.get("currency", ""),
                  provenance.get("account_code", ""), provenance.get("branch", ""), provenance.get("department", ""),
                  provenance.get("service_period", ""), provenance.get("fixture", ""), provenance.get("disclosure", ""), snapshot.get("id", ""))
        writer.writerow([spreadsheet_cell(value) for value in values])
    return output.getvalue()


def billing_brief(snapshot, attachment, *, linked_attachment=True):
    # Attachment paths are owned by the application, never by an uploaded filename.
    if attachment not in ("purchase-order.pdf", "purchase-order.txt"):
        raise ValueError("Unsupported billing attachment")

    def e(value, empty="—"):
        return escape(str(value), quote=True) if isinstance(value, (str, int)) and str(value) else empty

    def pair(label, value):
        return f"<div><dt>{label}</dt><dd>{e(value)}</dd></div>"

    checks = snapshot.get("checks")
    checks = checks if isinstance(checks, list) else []
    rows = []
    pages = set()
    for check in checks:
        if not isinstance(check, dict):
            continue
        page = check.get("source_page")
        if type(page) is int and 1 <= page <= 20:
            pages.add(page)
        else:
            page = None
        rows.append(f"<tr><th scope=\"row\">{e(check.get('label'))}</th>"
                    f"<td>{e(check.get('status'))}</td><td>{e(page)}</td></tr>")
    checks_table = ("<table><thead><tr><th scope=\"col\">Check at approval</th>"
                    "<th scope=\"col\">Result</th><th scope=\"col\">Source page</th></tr></thead>"
                    f"<tbody>{''.join(rows)}</tbody></table>") if rows else "<p>Checks were not retained in this approval snapshot.</p>"
    notes = "".join(f"<div class=\"note\"><h3>{label}</h3><p>{e(snapshot.get(key))}</p></div>"
                    for key, label in (("correction_note", "Correction reason"), ("note", "Approval note")) if snapshot.get(key))
    provenance = snapshot.get("source_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    intake_labels = {"manual": "Manual entry", "csv": "CSV import", "qbo": "QuickBooks Online", "fixed_demo_seed": "Retained workflow fixture"}
    fixture = provenance.get("intake") == "fixed_demo_seed" or bool(provenance.get("fixture") and provenance.get("disclosure"))
    provenance_note = "<p>Example workflow fixture; not a client outcome.</p>" if fixture else ""
    source_context = "".join(pair(label, provenance[key]) for key, label in
                             (("account_code", "Business account"), ("branch", "Service branch"),
                              ("department", "Department / cost center"), ("service_period", "Service period"),
                              ("terms_days", "Payment terms (days)")) if provenance.get(key))
    source_context = f'<section><h2>Source billing context</h2><dl>{source_context}</dl></section>' if source_context else ""
    line_detail = ""
    if billing_lines_csv(snapshot) is not None:
        line_rows = "".join(f'<tr><th scope="row">{e(line["description"])}</th><td>{e(line["quantity"])}</td><td>{e(line["unit_rate"])}</td><td>{e(line["amount"])}</td></tr>' for line in provenance["billing_lines"])
        line_detail = f'<section><h2>Source invoice lines · {e(snapshot.get("currency"))}</h2><table><thead><tr><th scope="col">Service</th><th scope="col">Quantity</th><th scope="col">Rate</th><th scope="col">Amount</th></tr></thead><tbody>{line_rows}</tbody></table></section>'
    amount = decimal_amount(snapshot["amount_cents"])
    source_pages = ", ".join(str(page) for page in sorted(pages)) if pages else "Not retained"
    attachment_markup = f'<a href="{attachment}">Open the approved purchase order</a>' if linked_attachment else "The approved purchase order is included in the complete handoff ZIP."
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Billing brief · {e(snapshot.get('invoice_number'))}</title>
<style>
:root{{color-scheme:light;--paper:#FCFBF9;--ink:#25212B;--violet:#593B8B;--muted:#69616F;--rule:#DDD8E2}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:960px;margin:0 auto;padding:56px 58px 40px}}header{{display:flex;justify-content:space-between;gap:24px;align-items:baseline;border-bottom:1px solid var(--ink);padding-bottom:18px}}
.wordmark,h1,h2,h3{{font-family:"Space Grotesk","Avenir Next",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}.wordmark{{font-size:22px;font-weight:650;letter-spacing:-.9px}}.eyebrow{{font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--violet);font-weight:650}}
h1{{font-size:42px;font-weight:550;line-height:1.08;letter-spacing:-1.8px;margin:12px 0}}h2{{font-size:22px;line-height:1.25;font-weight:550;letter-spacing:-.5px;margin:0 0 18px}}h3{{font-size:12px;font-weight:650;margin:0 0 4px;color:var(--muted)}}p{{margin:0;overflow-wrap:anywhere}}.lede{{padding:36px 0 30px;display:grid;grid-template-columns:1fr auto;gap:26px;align-items:end}}.customer{{font-size:19px}}.amount{{font-size:25px;font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}}.state{{font-size:12px;color:var(--muted);text-align:right;margin-top:5px}}
section{{padding:26px 0;border-top:1px solid var(--rule);break-inside:avoid}}.action{{border-top:2px solid var(--violet)}}.instruction{{font-size:21px;line-height:1.45;max-width:740px;white-space:pre-wrap}}.references{{display:grid;grid-template-columns:1fr 32px 1fr;gap:16px;align-items:center;margin-top:24px}}.references span{{color:var(--violet);font-size:24px;text-align:center}}.references p{{font-size:19px;font-weight:550}}.note{{margin-top:18px}}.note p{{white-space:pre-wrap}}dl{{display:grid;grid-template-columns:1fr 1fr;gap:20px 36px;margin:0}}dt{{font-size:11px;color:var(--muted);margin-bottom:4px}}dd{{margin:0;overflow-wrap:anywhere}}.wide{{grid-column:1/-1}}.hash{{font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:11px 12px 11px 0;text-align:left;border-bottom:1px solid var(--rule)}}thead th{{font-size:11px;color:var(--muted);font-weight:500}}tbody th{{font-weight:500}}a{{color:var(--violet);text-underline-offset:3px}}.attachment{{margin-top:18px}}.scope{{color:var(--muted);font-size:12px;line-height:1.65}}footer{{margin-top:28px;padding-top:14px;border-top:1px solid var(--ink);font-size:10px;color:var(--muted)}}footer p+p{{margin-top:6px}}
@media(max-width:600px){{main{{padding:28px 24px}}header{{gap:12px}}.lede{{grid-template-columns:1fr}}.amount,.state{{text-align:left}}h1{{font-size:36px}}dl{{grid-template-columns:1fr}}.references{{grid-template-columns:1fr}}.references span{{text-align:left;transform:rotate(90deg);width:24px}}}}
@page{{size:auto;margin:17mm}}@media print{{body{{background:#fff;color:#25212B;font-size:10pt}}main{{max-width:none;padding:0}}header{{padding-bottom:12px}}.lede{{padding:25px 0}}h1{{font-size:30pt}}h2{{font-size:16pt}}section{{padding:18px 0}}.instruction{{font-size:15pt}}a{{color:inherit}}a:after{{content:" (" attr(href) ")";font-size:9pt}}footer{{position:static}}}}
</style></head><body><main>
<header><div class="wordmark">InvoiceClear</div><div class="eyebrow">Billing handoff · Prepared</div></header>
<div class="lede"><div><p class="eyebrow">Invoice</p><h1>{e(snapshot.get('invoice_number'))}</h1><p class="customer">{e(snapshot.get('customer'))}</p></div><div><p class="amount">{e(amount)} {e(snapshot.get('currency'))}</p><p class="state">Source payment status: {e(snapshot.get('payment_status'))}</p></div></div>
<section class="action"><h2>Next billing action</h2><p class="instruction">{e(snapshot.get('billing_instruction'))}</p>
<div class="references"><div><h3>Original invoice PO reference</h3><p>{e(snapshot.get('source_po_reference'), 'Missing')}</p></div><span aria-label="to">→</span><div><h3>Approved PO reference</h3><p>{e(snapshot.get('po_reference'))}</p></div></div>{notes}</section>
{source_context}{line_detail}<section><h2>Approval record</h2><dl>{pair('Reviewed and approved by', snapshot.get('prepared_by'))}{pair('Approved at (UTC)', snapshot.get('prepared_at'))}{pair('Source record', snapshot.get('source_ref'))}{pair('Source intake', intake_labels.get(provenance.get('intake'), 'Retained source record'))}{pair('Customer contact', snapshot.get('customer_email'))}{pair('Approval ID', snapshot.get('id'))}</dl></section>
<section><h2>Evidence checked</h2>{checks_table}<p class="attachment">{attachment_markup}</p><dl style="margin-top:18px">{pair('Original filename', snapshot.get('document_filename'))}{pair('Source pages supporting checks', source_pages)}<div class="wide"><dt>Document SHA-256</dt><dd class="hash">{e(snapshot.get('document_sha256'))}</dd></div></dl></section>
<section class="scope"><p>{e(snapshot.get('scope'))}</p><p>Prepared is an internal approval state. Payment and any external update require their own source records and confirmations.</p></section>
<footer><p>© 2026 Emman Ermitaño. All rights reserved.</p>{provenance_note}</footer>
</main></body></html>
"""
