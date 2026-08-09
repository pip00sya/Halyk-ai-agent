from __future__ import annotations

from typing import Any


def _truth_cells(truth: dict[str, Any]) -> dict[str, Any]:
    if "scenarios" in truth:
        return {
            scenario_id: scenario["covenants"]
            for scenario_id, scenario in truth["scenarios"].items()
        }
    if "answers" in truth:
        return truth["answers"]
    raise ValueError("Unsupported ground-truth structure")


def score_submission(submission: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    expected = _truth_cells(truth)
    answers = submission.get("answers", {})
    details: list[dict[str, Any]] = []
    total = 0.0
    max_total = 0.0
    for scenario_id, covenants in expected.items():
        for clause, key in covenants.items():
            max_total += 1.0
            got = answers.get(scenario_id, {}).get(clause)
            cell_score = 0.0
            status_ok = isinstance(got, dict) and got.get("status") == key["status"]
            relative_error = None
            if status_ok:
                cell_score += 0.50
                try:
                    expected_actual = float(key["actual"])
                    actual = float(got["actual"])
                    denominator = abs(expected_actual) if expected_actual != 0 else 1.0
                    relative_error = abs(actual - expected_actual) / denominator
                    scale = max(0.0, 1.0 - relative_error / 0.05)
                except (KeyError, TypeError, ValueError):
                    scale = 0.0
                cell_score += 0.30 * scale
                if key.get("evidence_txn_id") is None:
                    cell_score += 0.20 * scale
                elif got.get("evidence_txn_id") == key["evidence_txn_id"]:
                    cell_score += 0.20
            total += cell_score
            details.append(
                {
                    "scenario_id": scenario_id,
                    "clause": clause,
                    "score": round(cell_score, 6),
                    "status_ok": status_ok,
                    "relative_error": relative_error,
                    "evidence_ok": isinstance(got, dict) and got.get("evidence_txn_id") == key.get("evidence_txn_id"),
                }
            )
    return {
        "score": round(total / max_total if max_total else 0.0, 8),
        "points": round(total, 6),
        "max_points": max_total,
        "cells": details,
    }

