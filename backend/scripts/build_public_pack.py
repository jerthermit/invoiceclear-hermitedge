"""Rebuild fixed public PDF/CSV assets. Authoring-only: requires ReportLab.

Run with an authoring Python that has reportlab; it is not a server dependency.
Only the allowlisted fixture directory is written. No database/provider is used.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from invoiceclear.public_seed import (FIXTURE, PROVENANCE, document_relative_path,
                                     document_text, public_cases, recording_case)

OUTPUT = ROOT / "backend" / "fixtures" / "public"
FIELDS = ("invoice_number", "customer", "customer_email", "amount", "currency",
          "issue_date", "due_date", "po_reference", "source_ref")
INK, VIOLET, MUTED, RULE = (colors.HexColor(value) for value in
                           ("#25212B", "#593B8B", "#69616F", "#DDD8E2"))
PAGE_W, PAGE_H = 612, 792
LEFT, RIGHT = 48, 564


def paragraph(pdf, text, x, top, width, *, size=9, bold=False, color=INK, align=TA_LEFT):
    style = ParagraphStyle("text", fontName="Helvetica-Bold" if bold else "Helvetica",
                           fontSize=size, leading=size * 1.45, textColor=color,
                           alignment=align, spaceAfter=0)
    p = Paragraph(escape(str(text)), style)
    _, height = p.wrap(width, 600)
    if top - height < 68:
        raise ValueError("Document content would overlap footer")
    p.drawOn(pdf, x, top - height)
    return top - height


def write_pdf(path, text):
    pages = text.split("\f")
    if len(pages) != 2:
        raise ValueError("Every authored PO must have exactly two pages")
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H), invariant=1,
                        pageCompression=1)
    pdf.setTitle("Purchase order - " + path.stem)
    pdf.setAuthor("Fictional public workflow record")
    pdf.setSubject(PROVENANCE)
    for number, content in enumerate(pages, 1):
        lines = [line.strip() for line in content.strip().splitlines()]
        reference = next((line.split(": ", 1)[1] for line in text.splitlines()
                          if line.startswith("PO Number: ")), path.stem)
        buyer = next((line.split(": ", 1)[1] for line in text.splitlines()
                      if line.startswith("Customer: ")), "Purchasing")
        paragraph(pdf, buyer, LEFT, 747, 360, size=12, bold=True)
        paragraph(pdf, "PURCHASING", RIGHT - 100, 747, 100, size=8,
                  color=MUTED, align=TA_RIGHT)
        pdf.setStrokeColor(VIOLET)
        pdf.setLineWidth(1.4)
        pdf.line(LEFT, 715, RIGHT, 715)
        paragraph(pdf, "Purchase order" if number == 1 else "Billing schedule",
                  LEFT, 689, 380, size=25, bold=True)
        paragraph(pdf, reference, LEFT, 646, 380, size=11, color=VIOLET)
        paragraph(pdf, f"{number} of 2", RIGHT - 70, 646, 70, size=9,
                  color=MUTED, align=TA_RIGHT)
        y = 611
        for line in lines:
            if line == "PURCHASE ORDER" or line.startswith("PURCHASE ORDER "):
                continue
            if not line:
                y -= 10
                continue
            heading = line.isupper() and len(line) < 65
            if heading:
                pdf.setStrokeColor(RULE)
                pdf.setLineWidth(.5)
                pdf.line(LEFT, y + 5, RIGHT, y + 5)
                y -= 7
            y = paragraph(pdf, line, LEFT, y, RIGHT - LEFT,
                          size=9 if not heading else 8, bold=heading,
                          color=VIOLET if heading else INK) - (6 if heading else 3)
        pdf.setStrokeColor(RULE)
        pdf.line(LEFT, 55, RIGHT, 55)
        pdf.setFont("Helvetica", 7)
        pdf.setFillColor(MUTED)
        pdf.drawString(LEFT, 41, "Fictional workflow record. Not a client document or payment receipt.")
        pdf.drawRightString(RIGHT, 41, f"{number} / 2")
        pdf.showPage()
    pdf.save()


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build():
    cases = public_cases()
    recording = recording_case()
    write_csv(OUTPUT / "invoices.csv", FIELDS, [recording["entry"]])
    write_csv(OUTPUT / "all-invoices.csv", FIELDS, [item["entry"] for item in cases])
    manifest, line_rows, correspondence = [], [], []
    for item in cases + [recording]:
        for doc in item["documents"] if item is not recording else ():
            relative = document_relative_path(item, doc)
            write_pdf(OUTPUT / relative, document_text(item, doc))
            manifest.append(dict(invoice_number=item["entry"]["invoice_number"],
                                 filename=relative, po_number=doc["number"],
                                 revision=doc["revision"], reviewed=str(doc["reviewed"]).lower(),
                                 customer=doc["customer"] or item["entry"]["customer"],
                                 currency=doc["currency"] or item["entry"]["currency"],
                                 authorized_ceiling=doc["amount"] or item["entry"]["amount"],
                                 fixture=FIXTURE, disclosure=PROVENANCE))
        for index, line in enumerate(item["lines"], 1):
            line_rows.append(dict(invoice_number=item["entry"]["invoice_number"],
                                  customer=item["entry"]["customer"], branch=item["branch"],
                                  department=item["department"], service_period=item["service_period"],
                                  line_number=index, currency=item["entry"]["currency"], **line))
        for index, reply in enumerate(item["replies"], 1):
            correspondence.append(dict(invoice_number=item["entry"]["invoice_number"],
                                       sequence=index, contact=item["contact"],
                                       email=item["entry"]["customer_email"],
                                       source="manually retained correspondence", body=reply,
                                       fixture=FIXTURE, disclosure=PROVENANCE))
    for name in ("MVH-2026-2128", "MVH-CLIN-2026-0801", "LRL-TEMP-2026-082"):
        write_pdf(OUTPUT / (name + ".pdf"), (OUTPUT / (name + ".txt")).read_text())
    write_csv(OUTPUT / "purchase-orders.csv", tuple(manifest[0]), manifest)
    write_csv(OUTPUT / "billing-lines.csv", tuple(line_rows[0]), line_rows)
    write_csv(OUTPUT / "correspondence.csv", tuple(correspondence[0]), correspondence)
    print(json.dumps({"invoices": len(cases), "pdf_purchase_orders": len(manifest) + 3,
                      "billing_lines": len(line_rows), "customer_replies": len(correspondence),
                      "directory": str(OUTPUT)}))


if __name__ == "__main__":
    build()
