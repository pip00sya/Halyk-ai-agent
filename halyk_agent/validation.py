from __future__ import annotations

import math
from typing import Any


class SubmissionValidationError(ValueError):
    pass


def validate_submission(submission: dict[str, Any], template: dict[str, Any], ledger_ids: set[str]) -> None:
    errors: list[str] = []
    if set(submission) != set(template):
        errors.append(f"Top-level keys must be exactly {sorted(template)}")
    for key in ("team", "contact_email", "model"):
        if not isinstance(submission.get(key), str):
            errors.append(f"{key} must be a string")
    expected_answers = template.get("answers", {})
    answers = submission.get("answers")
    if not isinstance(answers, dict):
        errors.append("answers must be an object")
        answers = {}
    if set(answers) != set(expected_answers):
        errors.append("Scenario keys differ from submission_template.json")
    for scenario_id, expected_clauses in expected_answers.items():
        clauses = answers.get(scenario_id, {})
        if not isinstance(clauses, dict) or set(clauses) != set(expected_clauses):
            errors.append(f"{scenario_id}: covenant keys differ from template")
            continue
        for clause, expected_cell in expected_clauses.items():
            cell = clauses.get(clause)
            prefix = f"{scenario_id}/{clause}"
            if not isinstance(cell, dict) or set(cell) != set(expected_cell):
                errors.append(f"{prefix}: fields differ from template")
                continue
            if cell.get("status") not in {"COMPLIANT", "BREACH"}:
                errors.append(f"{prefix}: invalid status")
            actual = cell.get("actual")
            if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(float(actual)):
                errors.append(f"{prefix}: actual must be a finite number")
            elif float(actual) < 0:
                errors.append(f"{prefix}: actual must be positive or zero")
            evidence = cell.get("evidence_txn_id")
            if evidence is not None and not isinstance(evidence, str):
                errors.append(f"{prefix}: evidence_txn_id must be string or null")
            elif isinstance(evidence, str) and evidence not in ledger_ids:
                errors.append(f"{prefix}: evidence transaction is absent from ledger: {evidence}")
    if errors:
        raise SubmissionValidationError("\n".join(errors))
