from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .io import Ledger
from .models import DocumentRecord
from .plans import parse_plan, validate_plan


TXN_REFERENCE_RE = re.compile(r"\bTXN-[A-Za-z0-9]+-\d+\b", re.IGNORECASE)


class LLMError(RuntimeError):
    pass


def find_pdftoppm() -> str | None:
    """Return a real Poppler executable, including inside bundled Windows runtimes."""
    configured = os.environ.get("PDFTOPPM")
    candidate = Path(configured).expanduser() if configured else None
    if candidate and candidate.is_file():
        return str(candidate.resolve())

    located = shutil.which("pdftoppm.exe") or shutil.which("pdftoppm") or shutil.which("pdftoppm.cmd")
    path = Path(located).resolve() if located else None
    if path and path.suffix.lower() == ".exe":
        return str(path)

    # Some managed runtimes expose a .cmd shim only inside Codex's process PATH,
    # while a user-opened PowerShell cannot see it. Search both near the shim and
    # in Codex's stable per-user runtime location.
    search_parents = list(path.parents)[:5] if path else []
    search_parents.append(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies")
    for parent in search_parents:
        candidates = (
            parent / "pdftoppm.exe",
            parent / "Library" / "bin" / "pdftoppm.exe",
            parent / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe",
        )
        for executable in candidates:
            if executable.is_file():
                return str(executable.resolve())
    return str(path) if path else None


def plan_response_schema(clauses: list[str]) -> dict[str, Any]:
    """Strict Responses API schema for a complete scenario plan."""
    nullable_string = {"type": ["string", "null"]}
    fact_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "type": {"type": "string", "enum": ["literal", "txn", "expression"]},
            "value": nullable_string,
            "source": nullable_string,
            "note": nullable_string,
            "ids": {"type": "array", "items": {"type": "string"}},
            "absolute": {"type": ["boolean", "null"]},
            "fallback": nullable_string,
            "expression": nullable_string,
        },
        "required": [
            "name", "type", "value", "source", "note", "ids",
            "absolute", "fallback", "expression",
        ],
    }
    override = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "enabled": {"type": "boolean"},
            "when": {"type": "string"},
            "status": {"type": "string", "enum": ["COMPLIANT", "BREACH"]},
            "reason": {"type": "string"},
        },
        "required": ["enabled", "when", "status", "reason"],
    }
    covenant = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "clause": {"type": "string", "enum": clauses},
            "title": {"type": "string"},
            "operator": {"type": "string", "enum": ["<=", ">=", "<", ">"]},
            "threshold": {"type": "string"},
            "expression": {"type": "string"},
            "facts": {"type": "array", "items": fact_item, "minItems": 1},
            "evidence_candidates": {"type": "array", "items": {"type": "string"}},
            "compare_round": {"type": ["integer", "null"], "enum": [2, None]},
            "status_override": override,
            "source_documents": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "rationale": {"type": "string"},
        },
        "required": [
            "clause", "title", "operator", "threshold", "expression", "facts",
            "evidence_candidates", "compare_round", "status_override",
            "source_documents", "rationale",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "covenants": {
                "type": "array",
                "items": covenant,
                "minItems": len(clauses),
                "maxItems": len(clauses),
            }
        },
        "required": ["covenants"],
    }


def normalize_plan_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert the strict list-based fact schema into the internal fact-map contract.

    Dict facts remain accepted for backwards-compatible tests and custom transports.
    """
    normalized = json.loads(json.dumps(raw))
    covenants = normalized.get("covenants")
    if not isinstance(covenants, list):
        return normalized
    for covenant in covenants:
        if not isinstance(covenant, dict):
            continue
        raw_facts = covenant.get("facts")
        if isinstance(raw_facts, list):
            facts: dict[str, dict[str, Any]] = {}
            for item in raw_facts:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    continue
                fact_type = item.get("type")
                spec: dict[str, Any] = {"type": fact_type}
                if fact_type == "literal":
                    spec.update(value=item.get("value"), source=item.get("source"))
                    if item.get("note"):
                        spec["note"] = item["note"]
                elif fact_type == "txn":
                    spec["ids"] = item.get("ids", [])
                    spec["absolute"] = True if item.get("absolute") is None else item.get("absolute")
                    if item.get("fallback") is not None:
                        spec["fallback"] = item["fallback"]
                elif fact_type == "expression":
                    spec["expression"] = item.get("expression")
                facts[item["name"]] = spec
            covenant["facts"] = facts
        override = covenant.get("status_override")
        if isinstance(override, dict) and "enabled" in override:
            covenant["status_override"] = (
                {
                    "when": override.get("when"),
                    "status": override.get("status"),
                    "reason": override.get("reason"),
                }
                if override.get("enabled") else None
            )
    return normalized


class OpenAIResponsesClient:
    """Small dependency-free Responses API client.

    The API key is read only at call time and is never written to disk.
    """

    def __init__(
        self,
        model: str = "gpt-5.6",
        reasoning_effort: str = "high",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 300,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.timeout = timeout
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is required for an unknown/private dataset")
        self._usage_lock = threading.Lock()
        self._usage: dict[str, Any] = {
            "calls": 0,
            "failed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "latency_ms": 0,
        }

    def respond(
        self,
        prompt: str,
        images: list[Path] | None = None,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "halyk_response",
    ) -> str:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image in images or []:
            payload = base64.b64encode(image.read_bytes()).decode("ascii")
            content.append({"type": "input_image", "image_url": f"data:image/png;base64,{payload}"})
        body = {
            "model": self.model,
            "reasoning": {"effort": self.reasoning_effort},
            "input": [{"role": "user", "content": content}],
            "text": {"verbosity": "medium"},
            "store": False,
        }
        if json_schema is not None:
            body["text"]["format"] = {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": json_schema,
            }
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        last_error: Exception | None = None
        started = time.perf_counter()
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                self._record_usage(raw, started)
                return self._output_text(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = LLMError(f"OpenAI API HTTP {exc.code}: {detail[:1000]}")
                quota_exhausted = exc.code == 429 and any(
                    marker in detail
                    for marker in ("insufficient_quota", "credit_balance_exhausted", "no credits remaining")
                )
                if quota_exhausted or exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
        with self._usage_lock:
            self._usage["failed_calls"] += 1
            self._usage["latency_ms"] += round((time.perf_counter() - started) * 1000)
        raise LLMError(str(last_error or "OpenAI API request failed"))

    def respond_structured(self, prompt: str, schema: dict[str, Any], schema_name: str) -> str:
        return self.respond(prompt, json_schema=schema, schema_name=schema_name)

    def _record_usage(self, response: dict[str, Any], started: float) -> None:
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
        with self._usage_lock:
            self._usage["calls"] += 1
            self._usage["input_tokens"] += int(usage.get("input_tokens") or 0)
            self._usage["output_tokens"] += int(usage.get("output_tokens") or 0)
            self._usage["total_tokens"] += int(usage.get("total_tokens") or 0)
            self._usage["cached_input_tokens"] += int(input_details.get("cached_tokens") or 0)
            self._usage["latency_ms"] += round((time.perf_counter() - started) * 1000)

    def usage_snapshot(self) -> dict[str, Any]:
        with self._usage_lock:
            return dict(self._usage)

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        if response.get("status") == "incomplete":
            reason = (response.get("incomplete_details") or {}).get("reason", "unknown")
            raise LLMError(f"Responses API returned an incomplete result: {reason}")
        if isinstance(response.get("output_text"), str):
            return response["output_text"]
        chunks: list[str] = []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "refusal":
                    raise LLMError(f"Model refused the request: {content.get('refusal', 'no detail')}")
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    chunks.append(content["text"])
        if not chunks:
            raise LLMError("Responses API returned no output text")
        return "\n".join(chunks)


def parse_json_response(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("Model did not return a JSON object") from exc
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested:
            raise LLMError(f"Invalid model JSON: {nested}") from nested
    if not isinstance(value, dict):
        raise LLMError("Model JSON root must be an object")
    return value


def render_pdf_pages(pdf_path: Path, pages: list[int], output_dir: Path) -> list[Path]:
    executable = find_pdftoppm()
    if not executable:
        raise LLMError("pdftoppm is required for vision OCR; install Poppler or set PDFTOPPM")
    rendered: list[Path] = []
    for page_number in pages:
        prefix = output_dir / f"{pdf_path.stem}-p{page_number}"
        subprocess.run(
            [executable, "-f", str(page_number), "-l", str(page_number), "-r", "170", "-png", str(pdf_path), str(prefix)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        matches = sorted(output_dir.glob(f"{prefix.name}-*.png"))
        if not matches:
            raise LLMError(f"No rendered image for {pdf_path.name} page {page_number}")
        rendered.append(matches[-1])
    return rendered


def vision_ocr_record(client: OpenAIResponsesClient, record: DocumentRecord) -> str:
    pages = record.low_text_pages
    if not pages:
        return record.text
    with tempfile.TemporaryDirectory(prefix="halyk-ocr-") as temp:
        images = render_pdf_pages(record.path, pages, Path(temp))
        prompt = (
            "You are the OCR stage of a banking covenant analysis agent. Transcribe every visible "
            "word, table cell, number, percentage, date, account ID, transaction ID and status from "
            "the attached PDF page images. Preserve page order and table row relationships. Do not "
            "infer missing values and do not summarize. Return plain UTF-8 text."
        )
        ocr = client.respond(prompt, images)
    return (
        f"{record.text}\n\n[VISION OCR FOR PAGES {','.join(map(str, pages))}]\n{ocr}"
    ).strip()


class LLMPlanner:
    def __init__(self, client: OpenAIResponsesClient) -> None:
        self.client = client
        self.review_log: dict[str, dict[str, Any]] = {}

    def _respond_plan_json(self, prompt: str, clauses: list[str], suffix: str) -> dict[str, Any]:
        responder = getattr(self.client, "respond_structured", None)
        if callable(responder):
            text = responder(
                prompt,
                plan_response_schema(clauses),
                f"halyk_plan_{suffix.lower()}",
            )
        else:  # Contract-test and custom transport compatibility.
            text = self.client.respond(prompt)
        return normalize_plan_payload(parse_json_response(text))

    def plan_scenario(
        self,
        scenario_id: str,
        account_id: str | None,
        clauses: list[str],
        records: list[DocumentRecord],
        ledger: Ledger,
        review: bool = True,
    ) -> list[Any]:
        excluded_flags = {"obsolete", "draft", "rejected", "prior_period"}
        authoritative = [item for item in records if not excluded_flags.intersection(item.flags)][:12]
        if not authoritative:
            raise LLMError(f"{scenario_id}: no authoritative documents were resolved")
        documents: list[str] = []
        for record in authoritative:
            text = record.text
            if record.low_text_pages:
                try:
                    text = vision_ocr_record(self.client, record)
                except LLMError:
                    text = record.text
            documents.append(
                f"FILE={record.path.name}; TYPE={record.doc_type}; AUTHORITY={record.authority_score}; "
                f"FLAGS={','.join(record.flags) or 'none'}\n{text[:50000]}"
            )
        referenced_txn_ids = {
            match.group(0).upper()
            for record in authoritative
            for match in TXN_REFERENCE_RE.finditer(record.text)
        }
        ledger_context = ledger.context_for_scenario(scenario_id, account_id, referenced_txn_ids)
        txns = [
            {
                "txn_id": txn.txn_id,
                "date": txn.date,
                "account_id": txn.account_id,
                "counterparty": txn.counterparty,
                "description": txn.description,
                "amount": None if txn.amount is None else str(txn.amount),
                "currency": txn.currency,
            }
            for txn in ledger_context
        ]
        prompt = f"""
You are the semantic planning component of an evidence-first bank covenant agent.
Scenario: {scenario_id}; borrower account: {account_id}; required clauses: {clauses}.

Determine the current 2025 agreement, final auditor/AUP decisions, KYC relationships,
missing/off-ledger facts, FX conversions and ledger transactions. Ignore obsolete 2024 copies,
rejected proposals and drafts whenever a later final document exists.

Return JSON only, with this exact shape:
{{
  "covenants": [
    {{
      "clause": "6.1",
      "title": "short title",
      "operator": "<=" or ">=",
      "threshold": "decimal string",
      "expression": "safe arithmetic expression using fact names; allowed: + - * / abs min max",
      "facts": [
        {{"name":"fact_name","type":"txn","ids":["TXN-..."],"absolute":true,"fallback":null,"value":null,"source":null,"note":null,"expression":null}},
        {{"name":"document_fact","type":"literal","value":"decimal string","source":"filename.pdf","note":"page/fact","ids":[],"absolute":null,"fallback":null,"expression":null}},
        {{"name":"derived_fact","type":"expression","expression":"expression using earlier facts","value":null,"source":null,"note":null,"ids":[],"absolute":null,"fallback":null}}
      ],
      "evidence_candidates": ["only transaction IDs whose inclusion/classification can flip the verdict"],
      "compare_round": null,
      "status_override": {{"enabled":false,"when":"0","status":"COMPLIANT","reason":""}},
      "source_documents": ["authoritative filenames"],
      "rationale": "concise audit trail"
    }}
  ]
}}

Rules:
- Produce exactly one plan for every required clause and no other clauses.
- Use Decimal-compatible strings and transaction references; never copy a final answer from a key.
- Expenses in the ledger are negative, but financial magnitudes normally use absolute=true.
- Keep internal precision. compare_round must normally be null; use 2 only if the governing rule
  explicitly bases compliance on a rounded two-decimal ratio.
- expression must always calculate the actual metric requested by the covenant. Never manipulate
  actual to force a verdict. If an authoritative waiver, exception, cure or applicability condition
  changes the verdict while actual remains above/below the limit, set status_override.enabled=true
  and ground its condition in a literal or derived fact. Otherwise keep enabled=false.
- Evidence is not the largest transaction. Include a candidate only when that particular transaction's
  identity, classification or counterfactual removal determines a breach.
- If a ledger amount is blank but an authoritative document supplies it, use txn fallback.
- Every fact item must include every schema field. Use null or [] for fields irrelevant to its type.

DOCUMENTS
{'\n\n=====\n\n'.join(documents)}

LEDGER ROWS
{json.dumps(txns, ensure_ascii=False, indent=2)}

LEDGER CONTEXT POLICY
Rows include the scenario prefix, every row on borrower account {account_id}, and transaction IDs
explicitly cross-referenced by authoritative documents. Do not assume unrelated archive rows are absent;
use scope, dates, counterparties and document rules to select only relevant facts.
""".strip()
        last_error: Exception | None = None
        document_names = {record.path.name for record in authoritative}
        for attempt in range(2):
            request_prompt = prompt
            if attempt:
                request_prompt += (
                    "\n\nYour previous output failed contract or grounding validation. Re-evaluate the source "
                    f"evidence and return valid JSON with exactly these clauses: {clauses}. "
                    f"Validation error: {str(last_error)[:1200]}"
                )
            try:
                raw = self._respond_plan_json(request_prompt, clauses, f"{scenario_id}_draft")
                plans = [parse_plan(item) for item in raw.get("covenants", [])]
                got = {plan.clause for plan in plans}
                if got != set(clauses):
                    raise LLMError(
                        f"{scenario_id}: planner clauses {sorted(got)} != required {sorted(clauses)}"
                    )
                for plan in plans:
                    validate_plan(plan, set(ledger.by_id), document_names)
                if review:
                    review_prompt = f"""
Act as the independent second-pass reviewer of a bank covenant calculation plan.
Check the candidate against the source documents and ledger embedded below. Correct only
material errors: document authority, period, borrower/group scope, accepted versus rejected
reclassification, KYC threshold, FX, missing/off-ledger amounts, formula, comparison operator,
threshold and evidence candidates. Return JSON only in the same shape. Preserve every correct
field. There must be exactly these clauses: {clauses}.

CANDIDATE PLAN
{json.dumps(raw, ensure_ascii=False, indent=2)}

ORIGINAL EVIDENCE PACKAGE
{prompt}
""".strip()
                    try:
                        reviewed = self._respond_plan_json(
                            review_prompt, clauses, f"{scenario_id}_review"
                        )
                        reviewed_plans = [parse_plan(item) for item in reviewed.get("covenants", [])]
                        if {item.clause for item in reviewed_plans} == set(clauses):
                            for reviewed_plan in reviewed_plans:
                                validate_plan(reviewed_plan, set(ledger.by_id), document_names)
                            initial_payload = json.dumps(raw.get("covenants", []), ensure_ascii=False, sort_keys=True)
                            reviewed_payload = json.dumps(reviewed.get("covenants", []), ensure_ascii=False, sort_keys=True)
                            initial_by_clause = {
                                item.get("clause"): item for item in raw.get("covenants", [])
                                if isinstance(item, dict)
                            }
                            reviewed_by_clause = {
                                item.get("clause"): item for item in reviewed.get("covenants", [])
                                if isinstance(item, dict)
                            }
                            changed_clauses = sorted(
                                clause for clause in set(initial_by_clause) | set(reviewed_by_clause)
                                if initial_by_clause.get(clause) != reviewed_by_clause.get(clause)
                            )
                            self.review_log[scenario_id] = {
                                "status": "confirmed" if initial_payload == reviewed_payload else "corrected",
                                "planning_attempt": attempt + 1,
                                "review_enabled": True,
                                "clauses": sorted(clauses),
                                "changed_clauses": changed_clauses,
                            }
                            return reviewed_plans
                    except (LLMError, ValueError) as review_error:
                        self.review_log[scenario_id] = {
                            "status": "review_failed_fallback",
                            "planning_attempt": attempt + 1,
                            "review_enabled": True,
                            "error": str(review_error)[:500],
                            "clauses": sorted(clauses),
                        }
                    if scenario_id not in self.review_log:
                        self.review_log[scenario_id] = {
                            "status": "review_schema_mismatch_fallback",
                            "planning_attempt": attempt + 1,
                            "review_enabled": True,
                            "clauses": sorted(clauses),
                        }
                else:
                    self.review_log[scenario_id] = {
                        "status": "disabled",
                        "planning_attempt": attempt + 1,
                        "review_enabled": False,
                        "clauses": sorted(clauses),
                    }
                return plans
            except (LLMError, ValueError) as exc:
                last_error = exc
        raise LLMError(f"{scenario_id}: planner failed after one repair attempt: {last_error}")
