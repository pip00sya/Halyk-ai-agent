from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    date: str
    account_id: str
    counterparty: str
    description: str
    amount: Decimal | None
    currency: str


@dataclass
class DocumentRecord:
    path: Path
    sha256: str
    pages: list[str]
    page_char_counts: list[int]
    doc_type: str = "other"
    authority_score: int = 0
    account_ids: list[str] = field(default_factory=list)
    scenario_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.pages)

    @property
    def low_text_pages(self) -> list[int]:
        return [i + 1 for i, count in enumerate(self.page_char_counts) if count < 40]


@dataclass(frozen=True)
class FactValue:
    name: str
    value: Decimal
    provenance: list[dict[str, Any]]


@dataclass
class CovenantPlan:
    clause: str
    title: str
    operator: str
    threshold: Decimal
    expression: str
    facts: dict[str, dict[str, Any]]
    evidence_candidates: list[str] = field(default_factory=list)
    compare_round: int | None = None
    source_documents: list[str] = field(default_factory=list)
    rationale: str = ""
    status_override: dict[str, Any] | None = None

    def trace_value(self) -> dict[str, Any]:
        return {
            "clause": self.clause,
            "title": self.title,
            "operator": self.operator,
            "threshold": str(self.threshold),
            "expression": self.expression,
            "facts": self.facts,
            "evidence_candidates": self.evidence_candidates,
            "compare_round": self.compare_round,
            "source_documents": self.source_documents,
            "rationale": self.rationale,
            "status_override": self.status_override,
        }


@dataclass
class CovenantResult:
    scenario_id: str
    clause: str
    status: str
    actual: Decimal
    evidence_txn_id: str | None
    raw_actual: Decimal
    threshold: Decimal
    operator: str
    facts: dict[str, FactValue]
    source_documents: list[str]
    rationale: str
    decision_value: Decimal
    compare_round: int | None
    evidence_counterfactuals: list[dict[str, Any]] = field(default_factory=list)
    base_status: str | None = None
    status_override_applied: bool = False
    status_override: dict[str, Any] | None = None

    def submission_value(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "actual": float(self.actual),
            "evidence_txn_id": self.evidence_txn_id,
        }

    def trace_value(self) -> dict[str, Any]:
        if self.operator in {"<=", "<"}:
            signed_margin = self.threshold - self.decision_value
        else:
            signed_margin = self.decision_value - self.threshold
        denominator = max(abs(self.threshold), Decimal("1"))
        return {
            "scenario_id": self.scenario_id,
            "clause": self.clause,
            "status": self.status,
            "base_status": self.base_status or self.status,
            "status_override_applied": self.status_override_applied,
            "status_override": self.status_override,
            "actual": str(self.actual),
            "raw_actual": str(self.raw_actual),
            "decision_value": str(self.decision_value),
            "threshold": str(self.threshold),
            "operator": self.operator,
            "compare_round": self.compare_round,
            "signed_margin": str(signed_margin),
            "relative_margin": str(signed_margin / denominator),
            "evidence_txn_id": self.evidence_txn_id,
            "evidence_counterfactuals": self.evidence_counterfactuals,
            "source_documents": self.source_documents,
            "rationale": self.rationale,
            "facts": {
                key: {"value": str(value.value), "provenance": value.provenance}
                for key, value in self.facts.items()
            },
        }
