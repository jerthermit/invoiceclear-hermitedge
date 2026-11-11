"""Fixed fictional public records; neither client results nor a volume benchmark.

All companies, people, correspondence, source batches and authorizations here are
constructed. Reserved .example addresses cannot represent connected mailboxes.
Amounts are sums of individually rounded billing lines. No provider or model is
called, and no accounting, email, payment or investigation receipt is fabricated.
"""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

FIXTURE = "staffing-36-v3"
PROVENANCE = "Fictional public workflow fixture; not a client record, outcome or capacity benchmark."
SEED_TIME = "2026-09-02T14:00:00Z"
REVIEWER = "Lena Ortiz"

ACCOUNTS = {
    "MVH": ("Mercer Valley Home Health LLC", "ap@mercervalleyhomehealth.example", "MVH-04018", "Nadia Park"),
    "LRL": ("Leonard & Reyes Logistics LLC", "billing@leonardreyeslogistics.example", "LRL-08241", "Evan Cole"),
    "AQF": ("A. Quinn Financial Services Inc", "payables@aquinnfinancial.example", "AQF-01296", "Mira Bennett"),
    "FOF": ("Foster & Ortiz Food Processing LLC", "ap@fosterortizfoods.example", "FOF-05730", "Luis Moreno"),
    "THA": ("Thompson-Hale Aerospace Components Inc", "ap@thompsonhaleaerospace.example", "THA-09306", "Dana Reed"),
    "SEH": ("S.E. Howard Hotel Management LLC", "ap@sehowardhotels.example", "SEH-02647", "Priya Shah"),
    "BSU": ("Benton Utility Field Services LLC", "invoices@bentonutilityservices.example", "BSU-06115", "Owen Ellis"),
    "KBD": ("K.B. Duncan Diagnostic Laboratories LLC", "ap@kbduncanlabs.example", "KBD-07392", "Tessa Grant"),
}


def billing_lines(*items):
    lines = []
    for description, quantity, rate in items:
        amount = (Decimal(quantity) * Decimal(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        lines.append({"description": description, "quantity": quantity, "unit_rate": rate, "amount": str(amount)})
    return lines


def total(lines):
    return str(sum((Decimal(line["amount"]) for line in lines), Decimal("0.00")))


def po(number, amount=None, *, reviewed=True, revision=1, customer=None, currency=None, note=""):
    return dict(number=number, amount=amount, reviewed=reviewed, revision=revision,
                customer=customer, currency=currency, note=note)


def case(number, account, branch, department, lines, reference, documents=(), *,
         issued="2026-08-17", terms=30, replies=(), scenario="", currency="USD", proposal=None, prepared=False):
    customer, email, account_code, contact = ACCOUNTS[account]
    issue = date.fromisoformat(issued)
    return {
        "id": "demo-invoice-" + str(number),
        "entry": dict(invoice_number=f"STA-2026-{number}", customer=customer, customer_email=email,
                      amount=total(lines), currency=currency, issue_date=issued,
                      due_date=str(issue + timedelta(days=terms)), po_reference=reference,
                      source_ref=f"HB-{issue:%Y%m%d}-US / {account_code} / {branch} / {department} / Net {terms}"),
        "account": account, "account_code": account_code, "contact": contact,
        "branch": branch, "department": department, "terms": terms,
        "service_period": "2026-08-01 through 2026-08-15" if issue.day < 31 else "2026-08-16 through 2026-08-31",
        "lines": lines, "documents": list(documents), "replies": list(replies),
        "scenario": scenario, "proposal": proposal, "prepared": prepared,
    }


def public_cases():
    """Return fresh values so review/mutation tests cannot alter the next seed."""
    clinical = billing_lines(("Registered nurses / approved regular time", "246", "75.00"))
    warehouse = billing_lines(("Warehouse associates / approved regular time", "420", "30.00"))
    finance = billing_lines(("Reconciliation analysts / approved regular time", "290", "75.00"))
    mixed = billing_lines(("Production associates / regular hours", "271.50", "46.75"),
                          ("Production associates / overtime hours", "44.50", "70.25"))
    laboratory = billing_lines(("Laboratory technicians / regular hours", "218.25", "58.40"),
                              ("Laboratory technicians / overtime hours", "12.75", "87.60"))
    utility = billing_lines(("Field dispatch coordinators / regular hours", "186.50", "52.80"),
                           ("Weekend dispatch coverage / premium hours", "16.25", "79.20"))
    hospitality = billing_lines(("Guest services / regular hours", "327.75", "31.45"),
                               ("Banquet support / overtime hours", "21.50", "47.18"))
    aerospace = billing_lines(("Quality inspectors / regular hours", "184.75", "68.35"),
                             ("Quality inspectors / overtime hours", "17.25", "102.53"))
    return [
        case(1041, "MVH", "Columbus East", "Clinical float pool CC-410", clinical, "", issued="2026-08-16", terms=15, scenario="missing authorization"),
        case(1042, "LRL", "Memphis South DC", "Outbound operations CC-228", warehouse, "", issued="2026-08-16", scenario="missing authorization"),
        case(1043, "AQF", "Phoenix Operations", "Cash reconciliation CC-630", finance, "AQF-2026-071",
             [po("AQF-2026-017", "30000.00")], issued="2026-08-16", scenario="transposed reference"),
        case(1044, "LRL", "Memphis South DC", "Returns operations CC-231", billing_lines(("Returns specialists", "280", "35.00")), "LRL-STAFF-224",
             [po("LRL-STAFF-224", "8000.00")], issued="2026-08-16", scenario="insufficient authorization"),
        case(1045, "MVH", "Columbus North", "Ambulatory nursing CC-416", billing_lines(("Registered nurses", "208", "75.00")), "MVH-RN-826",
             [po("MVH-RN-826", "24000.00")], issued="2026-08-16", scenario="reviewed with headroom"),
        case(1046, "AQF", "Phoenix Operations", "Loan servicing CC-642", billing_lines(("Loan servicing associates", "168", "67.50")), "AQF-CONTRACT-92",
             [po("AQF-CONTRACT-92", "15000.00", reviewed=False)], issued="2026-08-16", scenario="unreviewed authorization"),
        case(1047, "THA", "Wichita Plant 2", "Quality assurance CC-712", aerospace, "THA-QA-2608",
             replies=["Purchasing has the approved timecards for Wichita Plant 2. Dana Reed will send revision 2 after the quality manager signs the added overtime. Please keep STA-2026-1047 on hold until that document arrives."], scenario="reply received, PO still absent"),
        case(1048, "FOF", "Des Moines Cold Store", "Packaging line 4 CC-304", mixed, "FOF-PACK-2608", issued="2026-08-24", terms=45, scenario="missing attachment with source reference"),
        case(1049, "SEH", "Savannah Waterfront", "Banquet operations CC-186", hospitality, "", issued="2026-08-31", terms=45,
             replies=["The venue uses the trading name Howard Waterfront Hotel. The PO must name S.E. Howard Hotel Management LLC as legal buyer. Purchasing is correcting the cover sheet; this message does not authorize a reference change."], scenario="legal buyer clarification awaiting document"),
        case(1050, "KBD", "Raleigh Lab West", "Specimen processing CC-522", laboratory, "KBD-LAB-2608",
             [po("KBD-LAB-2608", "18000.00", reviewed=False)], scenario="unreviewed multiline laboratory PO"),
        case(1051, "BSU", "Tulsa Dispatch", "Storm response CC-814", utility, "BSU-SR-2026-08",
             [po("BSU-SR-2026-08", "15000.00", reviewed=False)], terms=60, scenario="unreviewed net-60 authorization"),
        case(1052, "FOF", "Des Moines Cold Store", "Packaging line 4 CC-304", mixed, "FOF-2026-091",
             [po("FOF-2026-019", "22000.00")], terms=45,
             replies=["Luis Moreno: Please use FOF-2026-019 for packaging line 4. FOF-2026-091 belongs to the maintenance contractor. The approved timecard total is unchanged."], scenario="digits transposed into another department's PO"),
        case(1053, "LRL", "Reno Crossdock", "Night sort CC-243", mixed, "",
             [po("LRL-NIGHT-2608", "22000.00")], scenario="reviewed PO, missing invoice reference"),
        case(1054, "THA", "Wichita Plant 2", "Quality assurance CC-712", aerospace, "THA-QA-2608-R1",
             [po("THA-QA-2608-R2", "19000.00", revision=2)],
             replies=["Revision 2 replaces revision 1 for the August inspection team. Use the complete R2 reference, including the suffix. No amount or payment adjustment is requested."], scenario="revision suffix mismatch"),
        case(1055, "MVH", "Cincinnati South", "Outpatient intake CC-438", laboratory, "MVH-INTAKE-0831",
             [po("MVH-INTAKE-0831", "20000.00", customer="Mercer Valley Medical Services PC")],
             replies=["The attached authorization was raised by Mercer Valley Medical Services PC. The billed legal entity is Mercer Valley Home Health LLC. Please wait for purchasing to reissue it; the common brand is not a legal-entity match."], scenario="affiliate is a different legal customer"),
        case(1056, "LRL", "Reno Crossdock", "Freight scheduling CC-249", utility, "LRL-FREIGHT-088",
             [po("LRL-FREIGHT-088", "18000.00", customer="Leonard & Reyes Warehousing Inc")], scenario="sister-company authorization"),
        case(1057, "KBD", "Buffalo Processing", "Regional accessioning CC-528", laboratory, "KBD-REG-2608",
             [po("KBD-REG-2608", "20000.00", currency="CAD")],
             replies=["Tessa Grant: That CAD purchase order belongs to the Ontario laboratory contract. The Buffalo invoice is denominated in USD. We will send the USD authorization; do not convert the invoice."], scenario="CAD PO against USD invoice"),
        case(1058, "THA", "Charleston Assembly", "Supplier inspection CC-725", aerospace, "THA-SUP-2608",
             [po("THA-SUP-2608", "19000.00", currency="EUR")], terms=45, scenario="EUR authorization against USD invoice"),
        case(1059, "FOF", "Omaha Processing", "Second shift CC-317", mixed, "FOF-S2-2608",
             [po("FOF-S2-2608", "15818.75")],
             replies=["The approved invoice lines total USD 15818.76. The PO ceiling is USD 15818.75. We need a one-cent authorization amendment; rounding the invoice down is not authorized."], scenario="one-cent coverage shortfall"),
        case(1060, "SEH", "Charleston Harbor", "Guest services CC-195", hospitality, "SEH-GUEST-2608",
             [po("SEH-GUEST-2608", str(Decimal(total(hospitality)) - Decimal("1250.50")))], terms=45,
             replies=["The weekend uplift was approved on the timesheets but not on the PO. A change order for USD 1250.50 is still pending. Please retain the current document and wait for the signed increase."], scenario="overtime uplift exceeds ceiling"),
        case(1061, "BSU", "Tulsa Dispatch", "Storm response CC-814", utility, "BSU-STORM-091",
             [po("BSU-STORM-091", "9000.00", revision=1), po("BSU-STORM-091", "16000.00", reviewed=False, revision=2)], terms=60,
             replies=["Owen Ellis: Attached revision 2 raises the storm-response ceiling from USD 9000.00 to USD 16000.00. It supersedes revision 1. Finance still needs to inspect the new pages before using it."], scenario="new higher revision awaits review"),
        case(1062, "KBD", "Raleigh Lab West", "Specimen processing CC-522", laboratory, "KBD-LAB-092",
             [po("KBD-LAB-029", "18000.00", reviewed=False, revision=1), po("KBD-LAB-092", "18000.00", revision=2)],
             replies=["Revision 2 corrects the cover-sheet reference to KBD-LAB-092. Customer, currency and ceiling remain unchanged. Retain revision 1 for the correspondence trail."], scenario="corrected revision reviewed"),
        case(1063, "MVH", "Cincinnati South", "Outpatient intake CC-438", clinical, "MVH-INTAKE-093",
             [po("MVH-INTAKE-093", "24000.00", revision=1), po("MVH-INTAKE-093", "24000.00", revision=2, customer="Mercer Valley Medical Services PC")],
             replies=["The second revision unexpectedly changed the buyer to the professional corporation. Please hold the invoice rather than falling back to revision 1; purchasing is checking which entity contracted the services."], scenario="latest revision introduces legal-customer conflict"),
        case(1064, "THA", "Charleston Assembly", "Supplier inspection CC-725", aerospace, "THA-SUP-094",
             [po("THA-SUP-094", "12000.00", revision=1), po("THA-SUP-094", "19000.00", currency="EUR", revision=2), po("THA-SUP-094", "19000.00", revision=3)], terms=45,
             replies=["Dana Reed: Revision 3 is the current USD authorization. Revision 1 had the old ceiling and revision 2 carried the European currency template. Please retain all three versions."], scenario="three revisions, current evidence passes"),
        case(1065, "BSU", "Fort Worth Field Office", "Meter scheduling CC-822", utility, "BSU-METER-095",
             [po("BSU-METER-095")], terms=60, scenario="exact-cent coverage"),
        case(1066, "KBD", "Buffalo Processing", "Regional accessioning CC-528", laboratory, "KBD-ACC-096",
             [po("KBD-ACC-096", "25000.00")], scenario="reviewed blanket authorization with headroom"),
        case(1067, "AQF", "Salt Lake City Service", "Commercial servicing CC-651", mixed, "AQF-COM-097",
             [po("AQF-COM-097", "22000.00", customer="a. quinn financial services inc")], terms=45, scenario="case-normalized legal customer"),
        case(1068, "THA", "Wichita Plant 2", "Quality assurance CC-712", aerospace, "THA-26-11-2",
             [po("THA-26-112", "19000.00")], scenario="meaningful punctuation in PO reference"),
        case(1069, "SEH", "Savannah Waterfront", "Banquet operations CC-186", hospitality, "SEH-EVENT-089",
             [po("SEH-EVENT-098", "16000.00")], terms=45, proposal="SEH-EVENT-098",
             replies=["Priya Shah: The supported authorization is SEH-EVENT-098. Our earlier email transposed it as SEH-EVENT-089. Please keep the original invoice reference in the audit trail."], scenario="supported correction retained for approval"),
        case(1070, "FOF", "Omaha Processing", "Sanitation support CC-323", utility, "FOF-SAN-099", terms=45,
             replies=["We have the sanitation timesheets and are checking the cost-center allocation.", "Purchasing has not released FOF-SAN-099 yet. Do not treat this correspondence as a PO or an increased ceiling. We will provide the signed document separately."], scenario="two replies do not replace missing source evidence"),
        case(1071, "LRL", "Dallas Distribution", "Peak pick-pack CC-255", billing_lines(("Warehouse associates", "375", "39.50"), ("Approved shift allowance", "1", "25.00")), "LRL-Q3-ALLOC-700",
             [po("LRL-Q3-ALLOC-700", "22150.15")], prepared=True, scenario="first internal allocation of shared PO"),
        case(1072, "LRL", "Dallas Distribution", "Peak pick-pack CC-255", billing_lines(("Warehouse associates", "187.50", "39.00"), ("Approved timecard rounding adjustment", "1", "0.15")), "LRL-Q3-ALLOC-700",
             [po("LRL-Q3-ALLOC-700", "22150.15")], scenario="remaining shared allocation exactly covered"),
        case(1073, "MVH", "Dayton Regional", "Allied health CC-445", billing_lines(("Allied health professionals", "200", "62.50")), "REGIONAL-AUG-810",
             [po("REGIONAL-AUG-810", "18000.00")], prepared=True, scenario="first internal clinical allocation"),
        case(1074, "MVH", "Dayton Regional", "Allied health CC-445", billing_lines(("Allied health professionals", "88", "62.50"), ("Approved timecard rounding adjustment", "1", "0.01")), "REGIONAL-AUG-810",
             [po("REGIONAL-AUG-810", "18000.00")],
             replies=["Nadia Park: The second invoice is USD 5500.01. The two invoices together exceed the USD 18000.00 authorization by one cent. The earlier internal allocation remains valid; purchasing must amend the PO before this one can proceed."], scenario="shared PO residual short by one cent"),
        case(1075, "BSU", "Fort Worth Field Office", "Meter scheduling CC-822", utility, "REGIONAL-AUG-810",
             [po("REGIONAL-AUG-810", "18000.00")], terms=60, scenario="same PO text, separate legal customer"),
        case(1076, "MVH", "Detroit Remote Services", "Cross-border scheduling CC-449", billing_lines(("Cross-border scheduling coordinators", "160", "62.50")), "REGIONAL-AUG-810",
             [po("REGIONAL-AUG-810", "18000.00")], currency="CAD", terms=45, scenario="same buyer and PO text, separate currency authorization"),
    ]


def recording_case():
    """The separate, repeatable import workflow; not another seeded invoice."""
    item = case(2101, "MVH", "Columbus East", "Clinical float pool CC-410",
                billing_lines(("Registered nurses / regular time", "246", "75.00")),
                "MVH-2026-2181", [po("MVH-2026-2128", "24000.00", revision=2)],
                issued="2026-08-16", scenario="imported transposed reference")
    item["entry"]["source_ref"] = "HB-20260816-US / MVH-04018 / Columbus East / Clinical float pool / Net 30"
    return item


def case_provenance(item, intake="fixed_demo_seed", actor="public-fixture-seed"):
    return {"mode": "demo", "intake": intake, "fixture": FIXTURE,
            "actor": actor, "disclosure": PROVENANCE,
            "account_code": item["account_code"], "branch": item["branch"],
            "department": item["department"], "terms_days": item["terms"],
            "service_period": item["service_period"], "billing_lines": item["lines"],
            "scenario": item["scenario"]}


def document_relative_path(item, document):
    """Trusted authored fixture identities only; never caller-selected paths."""
    return f"purchase-orders/{item['entry']['invoice_number']}/{document['number']}-rev{document['revision']}.pdf"


def document_source(item, document):
    import base64
    from .documents import decode_document

    relative = document_relative_path(item, document)
    path = Path(__file__).resolve().parents[1] / "fixtures" / "public" / relative
    return decode_document({"filename": path.name,
                            "content_base64": base64.b64encode(path.read_bytes()).decode()})


def document_text(case_, document):
    """A two-page text purchase order; the form feed is the page boundary."""
    entry = case_["entry"]
    number, revision = document["number"], document["revision"]
    customer = document["customer"] or entry["customer"]
    currency = document["currency"] or entry["currency"]
    amount = document["amount"] or entry["amount"]
    lines = ["PURCHASE ORDER", f"PO Number: {number}", f"Revision: {revision}",
             f"Customer: {customer}", "Supplier: Harborline Workforce Services LLC",
             f"Currency: {currency}", f"Total Amount: {amount}",
             f"Issued: 2026-08-{min(28, 2 + revision * 3):02d}",
             f"Business account: {case_['account_code']}", f"Service branch: {case_['branch']}",
             f"Department / cost center: {case_['department']}",
             f"Service period: {case_['service_period']}", f"Payment terms: Net {case_['terms']} days from valid invoice receipt",
             f"Billing contact: {entry['customer_email']}", "", "AUTHORIZED SERVICES",
             "Temporary staffing for the department above, subject to approved timecards.",
             "The total above is the authorization ceiling; line detail on page 2 is reconciliation context.",
             "Quote the current PO number and revision on each invoice.", "\f",
             f"PURCHASE ORDER {number} / REVISION {revision} / PAGE 2", "SUPPLIER BILLING RECONCILIATION SCHEDULE",
             f"Related invoice: {entry['invoice_number']}", f"Invoice source batch: {entry['source_ref']}"]
    for line in case_["lines"]:
        lines.append(f"{line['description']}: {line['quantity']} units x {entry['currency']} {line['unit_rate']} = {entry['currency']} {line['amount']}")
    lines += [f"Related invoice total (not the PO ceiling): {entry['currency']} {entry['amount']}", "",
              f"Revision control: revision {revision} {'supersedes earlier revisions attached to this case' if revision > 1 else 'is the initial authorization'}.",
              "Retain prior revisions and correspondence with the billing submission.",
              "Invoice quantities, rates and currency remain source records; a PO does not amend them.",
              "This authorization is not evidence of payment."]
    if document["note"]:
        lines.append(document["note"])
    return "\n".join(lines) + "\n"
