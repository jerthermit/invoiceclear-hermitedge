"""Public tree, documentation and clone commands; no staging or remote writes."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[2]


class PublicationTests(unittest.TestCase):
    def test_readme_and_operating_guide_links_resolve_in_the_public_tree(self):
        paths = [ROOT / "README.md", *sorted((ROOT / "backend/docs").glob("*.md"))]
        self.assertEqual(len(paths), 6)
        for path in paths:
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
                if re.match(r"[a-z]+://|mailto:|#", target):
                    continue
                target = unquote(target.split("#", 1)[0])
                with self.subTest(document=path.name, target=target):
                    self.assertTrue((path.parent / target).is_file())
        source = (ROOT / "README.md").read_text()
        self.assertIn("AI Agent for Invoice Resolution", source)
        self.assertNotIn("](docs/", source)
        self.assertIn("make -C backend setup", source)
        self.assertIn("make -C backend release", source)

    def test_published_makefile_runs_from_backend_without_local_root_shortcut(self):
        result = subprocess.run(["make", "-C", str(ROOT / "backend"), "-n", "run"],
                                text=True, capture_output=True, check=True)
        self.assertIn('cd "' + str(ROOT) + '"', result.stdout)
        self.assertIn('backend/run.py', result.stdout)
        self.assertNotIn('AI Live Commerce System/../', result.stdout)

    def test_deployment_guide_keeps_provider_credentials_on_backend(self):
        guide = (ROOT / "backend/docs/RELEASE.md").read_text()
        self.assertIn("## Manual dashboard walkthrough", guide)
        self.assertIn("Remove `INVOICECLEAR_AGENT_ENABLED=false`", guide)
        self.assertNotRegex(guide, r"(?m)^INVOICECLEAR_AGENT_ENABLED=")
        self.assertIn("TOGETHER_API_KEY", guide)
        self.assertIn("INVOICECLEAR_ALLOWED_ORIGINS", guide)
        self.assertIn("INVOICECLEAR_API_BASE_URL", guide)
        self.assertIn("No `/api` suffix", guide)
        self.assertIn("Never add TogetherAI, QuickBooks or Google secrets to Vercel", guide)

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "Git checkout required for publication exclusions")
    def test_public_candidates_match_reference_root_and_exclude_private_material(self):
        candidates = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                                    cwd=ROOT, text=True, capture_output=True, check=True).stdout.splitlines()
        self.assertEqual({name.split("/", 1)[0] for name in candidates}, {".gitignore", "README.md", "backend", "frontend"})
        excluded = {".env", "backend/.env", "frontend/.env.local", "backend/local.env", ".envrc.local",
                    "backend/data/workspace.sqlite3",
                    "backend/data/workspace.sqlite3-wal", "backup.sqlite", "private.db", "private.db-wal",
                    "backend/backups/.workspace-20261111-123.partial", "backups/manual-copy.zip",
                    "backend/exports/ledger.bak", "backend/exports/ledger.backup",
                    "backend/uploads/customer-po.pdf", "backend/output/handoff.csv",
                    "backend/artifacts/export.zip", "frontend/screenshots/review.png",
                    "frontend/recordings/overview.mov",
                    "frontend/dist/index.html", ".venv/bin/python", "node_modules/test.js", "provider.key",
                    "credentials.json", "client_secret_google.json", "token-cache.json", "auth.json",
                    "backend/secrets/provider.txt", "backend/secrets-local.json", ".codex/config.json",
                    "AGENTS.md", "DESIGN.md", "docs/CASE_STUDY.md", "backend/docs/CASE_STUDY.md", "Makefile"}
        included = {"backend/.env.example", "frontend/.env.example", "backend/Makefile",
                    "backend/fixtures/public/MVH-2026-2128.pdf", "frontend/web/fonts/Inter-LICENSE.txt"}
        result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], cwd=ROOT,
                                input="\n".join(sorted(excluded | included)) + "\n", text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(set(result.stdout.splitlines()), excluded)


if __name__ == "__main__":
    unittest.main()
