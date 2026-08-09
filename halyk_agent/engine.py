from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .expressions import evaluate
from .io import Ledger
from .models import CovenantPlan, CovenantResult, FactValue


OUTPUT_QUANTUM = Decimal("0.01")


def round_output(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANTUM, rounding=ROUND_HALF_UP)


def compare(actual: Decimal, operator: str, threshold: Decimal) -> bool:
    if operator == "<=":
        return actual <= threshold
    if operator == ">=":
        return actual >= threshold
    if operator == "<":
        return actual < threshold
    if operator == ">":
        return actual > threshold
    raise ValueError(f"Unsupported operator: {operator}")


def _resolve_fact(
    name: str,
    spec: dict[str, Any],
    ledger: Ledger,
    resolved: dict[str, FactValue],
    excluded_txn_id: str | None = None,
) -> FactValue:
    fact_type = spec.get("type")
    if fact_type == "literal":
        return FactValue(
            name=name,
            value=Decimal(str(spec["value"])),
            provenance=[{"type": "document", "source": spec.get("source", "fact-pack"), "note": spec.get("note", "")}],
        )
    if fact_type == "txn":
        total = Decimal("0")
        provenance: list[dict[str, Any]] = []
        for txn_id in spec.get("ids", []):
            txn = ledger.by_id.get(txn_id)
            if not txn:
                raise ValueError(f"Transaction not found: {txn_id}")
            if txn_id == excluded_txn_id:
                amount = Decimal("0")
            elif txn.amount is None:
                if "fallback" not in spec:
                    raise ValueError(f"Missing amount without fallback: {txn_id}")
                amount = Decimal(str(spec["fallback"]))
            else:
                amount = txn.amount
            if spec.get("absolute", True):
                amount = abs(amount)
            total += amount
            provenance.append(
                {
                    "type": "transaction",
                    "txn_id": txn.txn_id,
                    "amount": None if txn.amount is None else str(txn.amount),
                    "used_amount": str(amount),
                    "currency": txn.currency,
                    "counterparty": txn.counterparty,
                    "description": txn.description,
                    "excluded_for_counterfactual": txn_id == excluded_txn_id,
                }
            )
        return FactValue(name=name, value=total, provenance=provenance)
    if fact_type == "expression":
        value = evaluate(spec["expression"], {k: v.value for k, v in resolved.items()})
        return FactValue(
            name=name,
            value=value,
            provenance=[{"type": "calculation", "expression": spec["expression"]}],
        )
    raise ValueError(f"Unsupported fact type for {name}: {fact_type!r}")


def calculate(
    scenario_id: str,
    plan: CovenantPlan,
    ledger: Ledger,
    excluded_txn_id: str | None = None,
) -> tuple[Decimal, str, dict[str, FactValue], str, bool]:
    resolved: dict[str, FactValue] = {}
    pending = dict(plan.facts)
    while pending:
        progressed = False
        for name, spec in list(pending.items()):
            try:
                resolved[name] = _resolve_fact(name, spec, ledger, resolved, excluded_txn_id)
            except Exception as exc:
                if spec.get("type") == "expression" and "Unknown fact" in str(exc):
                    continue
                raise
            del pending[name]
            progressed = True
        if not progressed:
            raise ValueError(f"Unresolvable fact dependency cycle: {sorted(pending)}")
    raw = evaluate(plan.expression, {k: v.value for k, v in resolved.items()})
    decision_value = round_output(raw) if plan.compare_round == 2 else raw
    base_status = "COMPLIANT" if compare(decision_value, plan.operator, plan.threshold) else "BREACH"
    status = base_status
    override_applied = False
    if plan.status_override:
        condition = evaluate(
            str(plan.status_override["when"]),
            {key: value.value for key, value in resolved.items()},
        )
        if condition != 0:
            status = str(plan.status_override["status"])
            override_applied = True
    return raw, status, resolved, base_status, override_applied


def execute_plan(scenario_id: str, plan: CovenantPlan, ledger: Ledger) -> CovenantResult:
    raw, status, facts, base_status, override_applied = calculate(scenario_id, plan, ledger)
    decision_value = round_output(raw) if plan.compare_round == 2 else raw
    evidence: str | None = None
    counterfactuals: list[dict[str, Any]] = []
    if status == "BREACH":
        for candidate in plan.evidence_candidates:
            counterfactual_raw, counterfactual_status, _, _, _ = calculate(
                scenario_id, plan, ledger, excluded_txn_id=candidate
            )
            counterfactuals.append(
                {
                    "txn_id": candidate,
                    "status_without_transaction": counterfactual_status,
                    "actual_without_transaction": str(round_output(counterfactual_raw)),
                    "flips_verdict": counterfactual_status != status,
                }
            )
            if counterfactual_status != status:
                evidence = candidate
                break
    return CovenantResult(
        scenario_id=scenario_id,
        clause=plan.clause,
        status=status,
        actual=round_output(raw),
        evidence_txn_id=evidence,
        raw_actual=raw,
        threshold=plan.threshold,
        operator=plan.operator,
        facts=facts,
        source_documents=plan.source_documents,
        rationale=plan.rationale,
        decision_value=decision_value,
        compare_round=plan.compare_round,
        evidence_counterfactuals=counterfactuals,
        base_status=base_status,
        status_override_applied=override_applied,
        status_override=plan.status_override,
    )
