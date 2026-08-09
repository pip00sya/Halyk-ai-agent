from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from pypdf import PdfReader

from .io import sha256_file
from .models import DocumentRecord


ACCOUNT_RE = re.compile(r"ACC-\d{4,}", re.IGNORECASE)
GENERIC_LEDGER_ID_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]{1,15}-[A-Z0-9-]*\d[A-Z0-9-]*)(?![A-Z0-9])", re.IGNORECASE)
SCENARIO_RE = re.compile(r"TXN-([A-Za-z][A-Za-z0-9]*)-", re.IGNORECASE)
MIN_TEXT_PAGE_CHARS = 40


def extract_account_ids(text: str, known_account_ids: Iterable[str] = ()) -> list[str]:
    """Resolve account IDs from the ledger first, with ACC-* as a safe fallback.

    Private datasets may use domain-specific account formats such as TELE-4471.
    Matching the ledger's exact IDs avoids both a brittle prefix assumption and broad
    identifier regexes that would confuse report numbers with borrower accounts.
    """
    found = {match.upper() for match in ACCOUNT_RE.findall(text)}
    known = {item.strip().upper() for item in known_account_ids if item.strip()}
    if known:
        tokens = {match.upper() for match in GENERIC_LEDGER_ID_RE.findall(text)}
        found.update(tokens & known)
    return sorted(found)


def classify_document(text: str) -> tuple[str, int, list[str]]:
    low = text.lower()
    # Document-level lifecycle markers are banners/titles. Searching the full body would
    # incorrectly reject a final agreement merely because it describes a rejected proposal.
    head = low[:1600]
    flags: list[str] = []
    score = 0
    if any(token in head for token in ["недействующая редакция", "не применяется", "obsolete", "superseded"]):
        flags.append("obsolete")
        score -= 100
    if any(token in head for token in ["черновик", "draft", "for discussion"]):
        flags.append("draft")
        score -= 50
    if any(token in head for token in [
        "отклонено", "отклонён", "не принято", "не согласовано",
        "rejected", "not accepted", "not approved",
    ]):
        flags.append("rejected")
        score -= 70
    if any(token in low for token in ["final", "финальный", "окончательн"]):
        flags.append("final")
        score += 20
    if "2025" in low:
        flags.append("current_period")
        score += 10
    if "2024" in low and "2025" not in low:
        flags.append("prior_period")
        score -= 30
    if "договор банковского займа" in low or "loan agreement" in low:
        return "loan_agreement", score + 100, flags
    if "досье «знай своего клиента»" in low or "know your customer" in low or "kyc-" in low:
        return "kyc", score + 80, flags
    if "аудиторское дело" in low or "independent auditor" in low:
        return "audit", score + 90, flags
    if "agreed-upon procedures" in low or "согласованных процедур" in low:
        return "aup", score + 95, flags
    if "казначейств" in low or "treasury" in low:
        return "treasury_memo", score + 75, flags
    return "other", score, flags


class DocumentIndex:
    def __init__(self, records: list[DocumentRecord], known_account_ids: Iterable[str] = ()) -> None:
        self.records = records
        self.known_account_ids = tuple(sorted({item.strip().upper() for item in known_account_ids if item.strip()}))

    @classmethod
    def build(
        cls,
        documents_dir: Path,
        cache_dir: Path,
        known_account_ids: Iterable[str] = (),
    ) -> "DocumentIndex":
        cache_dir.mkdir(parents=True, exist_ok=True)
        known_accounts = tuple(sorted({item.strip().upper() for item in known_account_ids if item.strip()}))
        records: list[DocumentRecord] = []
        for path in sorted(documents_dir.glob("*.pdf")):
            digest = sha256_file(path)
            cache_file = cache_dir / f"{digest}.json"
            if cache_file.exists():
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                pages = cached["pages"]
            else:
                reader = PdfReader(path)
                pages = [(page.extract_text() or "").strip() for page in reader.pages]
                cache_file.write_text(
                    json.dumps({"source": path.name, "pages": pages}, ensure_ascii=False),
                    encoding="utf-8",
                )
            text = "\n".join(pages)
            doc_type, score, flags = classify_document(text)
            records.append(
                DocumentRecord(
                    path=path,
                    sha256=digest,
                    pages=pages,
                    page_char_counts=[len(re.sub(r"\s+", "", page)) for page in pages],
                    doc_type=doc_type,
                    authority_score=score,
                    account_ids=extract_account_ids(text, known_accounts),
                    scenario_ids=sorted({x.upper() for x in SCENARIO_RE.findall(text)}),
                    flags=flags,
                )
            )
        return cls(records, known_accounts)

    def for_scenario(self, scenario_id: str, account_id: str | None) -> list[DocumentRecord]:
        selected = []
        for record in self.records:
            if scenario_id.upper() in record.scenario_ids:
                selected.append(record)
            elif account_id and account_id.upper() in record.account_ids:
                selected.append(record)
        return sorted(selected, key=lambda item: (item.authority_score, item.path.name), reverse=True)

    def replace_text(self, record: DocumentRecord, text: str) -> None:
        original_page_count = max(1, len(record.pages))
        record.pages = [text] + ["[Vision OCR consolidated in page 1]"] * (original_page_count - 1)
        # OCR has already covered every low-text page. Preserve the original page count for
        # auditability without marking consolidation placeholders as pages needing OCR again.
        record.page_char_counts = [
            len(re.sub(r"\s+", "", text)),
            *([MIN_TEXT_PAGE_CHARS] * (original_page_count - 1)),
        ]
        record.doc_type, record.authority_score, record.flags = classify_document(text)
        record.account_ids = extract_account_ids(text, self.known_account_ids)
        record.scenario_ids = sorted({x.upper() for x in SCENARIO_RE.findall(text)})

    def manifest(self) -> list[dict[str, object]]:
        return [
            {
                "file": record.path.name,
                "sha256": record.sha256,
                "pages": len(record.pages),
                "doc_type": record.doc_type,
                "authority_score": record.authority_score,
                "account_ids": record.account_ids,
                "scenario_ids": record.scenario_ids,
                "flags": record.flags,
                "low_text_pages": record.low_text_pages,
            }
            for record in self.records
        ]
