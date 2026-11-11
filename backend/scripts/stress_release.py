"""Bounded local acceptance exercise; no user database or external providers."""
import base64
import csv
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from invoiceclear.api import create_server
from invoiceclear.config import Settings

with tempfile.TemporaryDirectory(prefix='invoiceclear-stress-db-') as folder:
    settings = Settings(database_path=Path(folder) / 'private.sqlite3', web_path=ROOT / 'frontend/web',
                        mode='private', email='finance@acceptance.example', password='isolated-local-check-only', port=0)
    server = create_server(settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    token = None

    def request(path, data=None, expected=200, authenticated=True, origin=None):
        headers = {}
        if token and authenticated:
            headers['Authorization'] = 'Bearer ' + token
        if origin:
            headers['Origin'] = origin
        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            headers['Content-Type'] = 'application/json'
        started = time.monotonic()
        try:
            response = urlopen(Request(base + path, data=body, headers=headers), timeout=25)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            value = json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw
            assert response.status == expected, (path, response.status, expected, value)
            return value, time.monotonic() - started

    def csv_batch(start, count):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['invoice_number','customer','customer_email','amount','currency','issue_date','due_date','po_reference','source_ref'])
        for number in range(start, start + count):
            writer.writerow([f'IC-{number:05d}', f'Staffing Account {number % 20:02d}', 'ap@acceptance.example', '100.00', 'USD', '2026-08-01', '2026-08-31', f'PO-{number:05d}', f'acceptance:{number}'])
        return output.getvalue()

    try:
        login, _ = request('/api/session', {'email': settings.email, 'password': settings.password})
        token = login['access_token']
        assert request('/api/invoices')[0]['total'] == 0
        for start in range(0, 5000, 500):
            result, _ = request('/api/invoices/import', {'csv': csv_batch(start, 500)})
            assert result == {'created': 500, 'duplicates': 0}
        assert request('/api/invoices/import', {'csv': csv_batch(0, 500)})[0] == {'created': 0, 'duplicates': 500}
        oversized, _ = request('/api/invoices/import', {'csv': csv_batch(6000, 501)}, expected=413)
        assert oversized['error']['code'] == 'csv_too_large'
        assert request('/api/invoices')[0]['total'] == 5000
        identifiers = []
        page_times = []
        for offset in range(0, 5000, 100):
            page, elapsed = request(f'/api/invoices?view=all&offset={offset}&limit=100')
            assert page['total'] == 5000 and len(page['invoices']) == 100
            assert page['has_more'] == (offset < 4900)
            assert all(not set(row) & {'documents','replies','checks','pages'} for row in page['invoices'])
            identifiers.extend(row['id'] for row in page['invoices'])
            page_times.append(elapsed)
        assert len(set(identifiers)) == 5000
        assert request('/api/invoices?q=IC-04999')[0]['total'] == 1
        with ThreadPoolExecutor(max_workers=8) as workers:
            reads = list(workers.map(lambda _: request('/api/invoices?limit=50'), range(32)))
        assert all(result['total'] == 5000 and len(result['invoices']) == 50 for result, _ in reads)
        invoice, _ = request('/api/invoices/' + identifiers[0])
        source_po = f"PO Number: {invoice['po_reference']}\nCustomer: {invoice['customer']}\nCurrency: USD\nAmount: 100.00\n".encode()
        invoice, _ = request(f"/api/invoices/{invoice['id']}/documents", {'filename':'purchase-order.txt', 'content_base64':base64.b64encode(source_po).decode(), 'expected_version':invoice['version'], 'idempotency_key':'stress-upload'})
        document = invoice['documents'][-1]
        result, _ = request(f"/api/invoices/{invoice['id']}/actions", {'action':'review_document', 'parameters':{'document_id':document['id'], 'fields':document['suggested_fields']}, 'expected_version':invoice['version'], 'idempotency_key':'stress-review'})
        invoice = result['invoice']
        approval = {'action':'approve_resolution', 'parameters':{}, 'expected_version':invoice['version'], 'idempotency_key':'stress-same-approval'}
        with ThreadPoolExecutor(max_workers=8) as workers:
            approvals = list(workers.map(lambda _: request(f"/api/invoices/{invoice['id']}/actions", approval)[0], range(16)))
        assert sum(not result['duplicate'] for result in approvals) == 1
        assert len({result['invoice']['resolution']['id'] for result in approvals}) == 1
        current, _ = request(f"/api/invoices/{invoice['id']}")
        assert current['status'] == 'prepared' and current['payment_status'] == 'unpaid' and current['amount_cents'] == 10000
        bundle, _ = request(f"/api/invoices/{invoice['id']}/handoff")
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            assert set(archive.namelist()) == {'billing-note.txt','billing-brief.html','resolution.json','purchase-order.txt'}
            assert archive.read('purchase-order.txt') == source_po
        assert request('/api/invoices', authenticated=False, expected=401)[0]['error']['code'] == 'unauthorized'
        assert request('/api/invoices', origin='https://untrusted.example', expected=403)[0]['error']['code'] == 'origin_forbidden'
        assert request('/api/health', authenticated=False)[0]['status'] == 'ok'
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert connection.execute("SELECT COUNT(*) FROM audit WHERE invoice_id=? AND event='approve_resolution'", (invoice['id'],)).fetchone()[0] == 1
        print(json.dumps({'status':'passed', 'invoice_count':5000, 'page_count':50, 'parallel_read_requests':32, 'read_concurrency':8, 'parallel_same_key_approvals':16, 'committed_approvals':1, 'page_max_ms':round(max(page_times)*1000), 'parallel_read_max_ms':round(max(elapsed for _,elapsed in reads)*1000), 'original_po_bytes_preserved':True, 'payment_status':current['payment_status'], 'sqlite_integrity':'ok'}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
