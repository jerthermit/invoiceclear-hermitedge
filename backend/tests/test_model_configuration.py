"""Pinned TogetherAI request contract; no paid inference or real credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from invoiceclear.config import DEFAULT_TOGETHER_MODEL, Settings
from invoiceclear.documents import together_extract
from invoiceclear.investigator import Investigator
from invoiceclear.service import Service
from test_investigator import Completion, tool


class ModelConfigurationTests(unittest.TestCase):
    def test_agent_uses_official_endpoint_and_low_effort_with_the_pinned_model(self):
        with tempfile.TemporaryDirectory(prefix="invoiceclear-model-config-") as directory:
            settings = Settings(database_path=Path(directory) / "case.sqlite3", web_path=Path(directory),
                                agent_enabled=True, together_key="isolated-key")
            service = Service(settings.database_path)
            agent = Investigator(service, settings)
            invoice = service.list_invoices()["invoices"][0]
            args = {"status": "abstained", "summary": "Further evidence is required.", "next_step": "none", "draft": "", "citations": []}
            def response(request, **kwargs):
                payload = json.loads(request.data)
                self.assertEqual(request.full_url, "https://api.together.ai/v1/chat/completions")
                self.assertEqual(payload["model"], DEFAULT_TOGETHER_MODEL)
                self.assertEqual(payload["reasoning_effort"], "low")
                self.assertEqual(payload["temperature"], 1.0)
                self.assertEqual(payload["top_p"], 0.95)
                self.assertEqual(payload["tool_choice"], "required")
                self.assertNotIn("reasoning", payload)
                return Completion([tool("finish_investigation", args)])
            with patch("invoiceclear.investigator.urllib.request.urlopen", side_effect=response):
                run = agent.investigate(invoice["id"], {"expected_version": invoice["version"], "idempotency_key": "model-contract"})
            self.assertEqual(run["status"], "abstained")
            self.assertEqual(run["model"], DEFAULT_TOGETHER_MODEL)

    def test_extraction_uses_the_same_model_settings_and_rejects_truncation(self):
        pages = [{"number": 1, "text": "PO Number: PO-TEST\nCustomer: Example Staffing\nCurrency: USD\nAmount: 100.00"}]
        fields = {"po_number": "PO-TEST", "customer": "Example Staffing", "currency": "USD", "amount": "100.00"}
        evidence = {name: {"page": 1, "snippet": pages[0]["text"]} for name in fields}
        finish = "stop"
        def response(request, **kwargs):
            payload = json.loads(request.data)
            self.assertEqual(request.full_url, "https://api.together.ai/v1/chat/completions")
            self.assertEqual(payload["model"], DEFAULT_TOGETHER_MODEL)
            self.assertEqual(payload["reasoning_effort"], "low")
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            result = Completion([])
            result.content = json.dumps({"choices": [{"finish_reason": finish, "message": {
                "reasoning_content": "This must never become source evidence.",
                "content": json.dumps({"fields": fields, "evidence": evidence}),
            }}], "usage": {"total_tokens": 100}}).encode()
            return result
        with patch("invoiceclear.documents.urllib.request.urlopen", side_effect=response):
            extracted, _, error, _ = together_extract(pages, "isolated-key", DEFAULT_TOGETHER_MODEL)
            self.assertEqual(extracted, fields)
            self.assertIsNone(error)
            finish = "length"
            extracted, _, error, _ = together_extract(pages, "isolated-key", DEFAULT_TOGETHER_MODEL)
            self.assertEqual(extracted, {})
            self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
