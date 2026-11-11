"""Fixed, non-confidential inputs for the public workflow; no workspace writes."""
import io
import zipfile
from pathlib import Path

from .documents import DomainError
from .public_seed import document_relative_path, public_cases

FILES = ("README.md", "CASE_GUIDE.md", "invoices.csv", "all-invoices.csv",
         "purchase-orders.csv", "billing-lines.csv", "correspondence.csv",
         "MVH-2026-2128.pdf", "MVH-CLIN-2026-0801.pdf", "LRL-TEMP-2026-082.pdf") + tuple(
             document_relative_path(item, document)
             for item in public_cases() for document in item["documents"])
DIRECTORY = Path(__file__).resolve().parents[1] / "fixtures" / "public"


def source_files() -> bytes:
    output = io.BytesIO()
    try:
        total = 0
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in FILES:
                with (DIRECTORY / name).open("rb") as source:
                    content = source.read(65_537)
                if len(content) > 65_536:
                    raise OSError("source limit")
                total += len(content)
                if total > 4 * 1024 * 1024:
                    raise OSError("archive limit")
                archive.writestr(name, content)
    except OSError:
        raise DomainError("source_files_unavailable", "Source files are unavailable. Contact the workspace owner.", 503) from None
    return output.getvalue()
