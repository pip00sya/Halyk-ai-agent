from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .io import read_json, sha256_file
from .expressions import ExpressionError, referenced_names
from .models import CovenantPlan


def parse_plan(raw: dict[str, Any]) -> CovenantPlan:
    required = {"clause", "title", "operator", "threshold", "expression", "facts"}
    missing = required - set(raw)
    if missing:
        raise ValueError(f"Plan fields missing: {sorted(missing)}")
    if raw["operator"] not in {"<=", ">=", "<", ">"}:
        raise ValueError(f"Invalid covenant operator: {raw['operator']}")
    plan = CovenantPlan(
        clause=str(raw["clause"]),
        title=str(raw["title"]),
        operator=raw["operator"],
        threshold=Decimal(str(raw["threshold"])),
        expression=str(raw["expression"]),
        facts=dict(raw["facts"]),
        evidence_candidates=list(raw.get("evidence_candidates", [])),
        compare_round=raw.get("compare_round"),
        source_documents=list(raw.get("source_documents", [])),
        rationale=str(raw.get("rationale", "")),
        status_override=raw.get("status_override"),
    )
    if not plan.threshold.is_finite():
        raise ValueError("Plan threshold must be finite")
    if plan.compare_round not in {None, 2}:
        raise ValueError("compare_round must be null or 2")
    return plan


def validate_plan(
    plan: CovenantPlan,
    ledger_ids: set[str],
    document_names: set[str],
) -> None:
    """Reject unsafe or hallucinated plans before a calculation reaches submission.json."""
    errors: list[str] = []
    if not plan.clause.strip():
        errors.append("clause is empty")
    if not plan.facts:
        errors.append("facts are empty")
    try:
        used = referenced_names(plan.expression)
    except ExpressionError as exc:
        errors.append(str(exc))
        used = set()
    unknown = used - set(plan.facts)
    if unknown:
        errors.append(f"formula references unknown facts: {sorted(unknown)}")

    if plan.status_override is not None:
        override = plan.status_override
        if not isinstance(override, dict):
            errors.append("status_override must be an object or null")
        else:
            when = override.get("when")
            if not isinstance(when, str):
                errors.append("status_override.when must be an expression")
            else:
                try:
                    override_names = referenced_names(when)
                    missing_override_facts = override_names - set(plan.facts)
                    if missing_override_facts:
                        errors.append(
                            f"status_override references unknown facts: {sorted(missing_override_facts)}"
                        )
                except ExpressionError as exc:
                    errors.append(f"status_override: {exc}")
            if override.get("status") not in {"COMPLIANT", "BREACH"}:
                errors.append("status_override.status must be COMPLIANT or BREACH")
            if not isinstance(override.get("reason"), str) or not override.get("reason", "").strip():
                errors.append("status_override.reason is required")

    txn_fact_ids: set[str] = set()
    literal_sources: set[str] = set()
    known_facts: set[str] = set()
    pending_dependencies: dict[str, set[str]] = {}
    for name, spec in plan.facts.items():
        if not isinstance(name, str) or not name.isidentifier():
            errors.append(f"invalid fact name: {name!r}")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{name}: fact must be an object")
            continue
        fact_type = spec.get("type")
        if fact_type == "literal":
            try:
                value = Decimal(str(spec["value"]))
                if not value.is_finite():
                    raise InvalidOperation
            except (KeyError, InvalidOperation, ValueError):
                errors.append(f"{name}: literal value must be a finite decimal")
            source = spec.get("source")
            if not isinstance(source, str) or not source.strip():
                errors.append(f"{name}: literal source is required")
            elif source not in document_names:
                errors.append(f"{name}: literal source is not in the PDF archive: {source}")
            else:
                literal_sources.add(source)
        elif fact_type == "txn":
            ids = spec.get("ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
                errors.append(f"{name}: transaction ids must be a non-empty string list")
            else:
                txn_fact_ids.update(ids)
                missing = set(ids) - ledger_ids
                if missing:
                    errors.append(f"{name}: unknown transactions: {sorted(missing)}")
            if "absolute" in spec and not isinstance(spec["absolute"], bool):
                errors.append(f"{name}: absolute must be boolean")
            if "fallback" in spec:
                try:
                    fallback = Decimal(str(spec["fallback"]))
                    if not fallback.is_finite():
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    errors.append(f"{name}: fallback must be a finite decimal")
        elif fact_type == "expression":
            expression = spec.get("expression")
            if not isinstance(expression, str):
                errors.append(f"{name}: expression fact requires an expression")
            else:
                try:
                    pending_dependencies[name] = referenced_names(expression)
                except ExpressionError as exc:
                    errors.append(f"{name}: {exc}")
        else:
            errors.append(f"{name}: unsupported fact type {fact_type!r}")
        known_facts.add(name)

    for name, dependencies in pending_dependencies.items():
        missing = dependencies - known_facts
        if missing:
            errors.append(f"{name}: expression references unknown facts: {sorted(missing)}")

    missing_sources = set(plan.source_documents) - document_names
    if not plan.source_documents:
        errors.append("source_documents is empty")
    elif missing_sources:
        errors.append(f"source documents are absent: {sorted(missing_sources)}")
    unlisted_literal_sources = literal_sources - set(plan.source_documents)
    if unlisted_literal_sources:
        errors.append(
            f"literal sources are missing from source_documents: {sorted(unlisted_literal_sources)}"
        )
    bad_candidates = set(plan.evidence_candidates) - ledger_ids
    if bad_candidates:
        errors.append(f"evidence candidates are absent from ledger: {sorted(bad_candidates)}")
    unrelated_candidates = set(plan.evidence_candidates) - txn_fact_ids
    if unrelated_candidates:
        errors.append(f"evidence candidates are not used by transaction facts: {sorted(unrelated_candidates)}")
    if errors:
        raise ValueError(f"Clause {plan.clause} failed plan validation:\n- " + "\n- ".join(errors))


def load_fact_pack(path: Path, data_dir: Path, enforce_fingerprint: bool = True) -> dict[str, list[CovenantPlan]]:
    raw = read_json(path)
    fingerprint = raw.get("dataset_fingerprint", {})
    if enforce_fingerprint:
        for filename, expected in fingerprint.items():
            actual_path = data_dir / filename
            if not actual_path.exists() or sha256_file(actual_path) != expected.lower():
                raise ValueError(f"Fact pack fingerprint mismatch for {filename}")
    scenarios: dict[str, list[CovenantPlan]] = {}
    for scenario_id, scenario in raw["scenarios"].items():
        scenarios[scenario_id] = [parse_plan(item) for item in scenario["covenants"]]
    return scenarios


def matching_fact_pack(path: Path, data_dir: Path) -> bool:
    try:
        load_fact_pack(path, data_dir, enforce_fingerprint=True)
        return True
    except (FileNotFoundError, ValueError):
        return False
