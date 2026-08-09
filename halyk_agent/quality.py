from __future__ import annotations

import os
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from .documents import DocumentIndex
from .io import Ledger, sha256_file
from .models import CovenantPlan, CovenantResult
from .llm import find_pdftoppm
from .validation import SubmissionValidationError, validate_submission


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PLACEHOLDER_RE = re.compile(r"^(?:change_me|название|email|team)", re.IGNORECASE)


def _check(
    check_id: str,
    title: str,
    status: str,
    detail: str,
    *,
    hard_gate: bool = False,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "title": title,
        "status": status,
        "hard_gate": hard_gate,
        "detail": detail,
        "metrics": metrics or {},
    }


def _metadata_ready(team: str, contact_email: str) -> tuple[bool, str]:
    team_ok = bool(team.strip()) and not PLACEHOLDER_RE.match(team.strip())
    email_ok = bool(EMAIL_RE.fullmatch(contact_email.strip())) and not PLACEHOLDER_RE.match(contact_email.strip())
    if team_ok and email_ok:
        return True, "Название команды и контактный email заполнены."
    missing = []
    if not team_ok:
        missing.append("team")
    if not email_ok:
        missing.append("contact_email")
    return False, f"Перед сдачей заполните: {', '.join(missing)}."


def build_preflight_report(
    *,
    data_dir: Path,
    template: dict[str, Any],
    ledger: Ledger,
    index: DocumentIndex,
    requested_mode: str,
    public_match: bool,
    team: str,
    contact_email: str,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    answers = template.get("answers")
    template_ok = (
        isinstance(answers, dict)
        and bool(answers)
        and all(isinstance(cells, dict) and bool(cells) for cells in answers.values())
    )
    checks.append(_check(
        "template_contract",
        "Контракт submission",
        "pass" if template_ok else "fail",
        "Шаблон содержит сценарии и ковенанты." if template_ok else "Некорректная структура answers.",
        hard_gate=True,
        metrics={"scenarios": len(answers or {}), "cells": sum(len(x) for x in (answers or {}).values())},
    ))

    ledger_ok = bool(ledger.transactions) and len(ledger.by_id) == len(ledger.transactions)
    checks.append(_check(
        "ledger_integrity",
        "Целостность реестра",
        "pass" if ledger_ok else "fail",
        "txn_id уникальны, обязательные поля CSV прочитаны." if ledger_ok else "Реестр пуст или содержит дубли.",
        hard_gate=True,
        metrics={"transactions": len(ledger.transactions)},
    ))

    currency_counts = dict(sorted(Counter(txn.currency for txn in ledger.transactions).items()))
    missing_amounts = [txn.txn_id for txn in ledger.transactions if txn.amount is None]
    distinct_accounts = sorted({txn.account_id for txn in ledger.transactions})
    nonstandard_accounts = [item for item in distinct_accounts if not re.fullmatch(r"ACC-\d{4,}", item, re.IGNORECASE)]
    checks.append(_check(
        "dataset_profile",
        "Профиль приватных данных",
        "warning" if missing_amounts else "pass",
        (
            f"Найдено {len(missing_amounts)} операций без суммы; агент должен восстановить их из документов."
            if missing_amounts else
            "Валюты, счета и суммы ledger профилированы до семантического анализа."
        ),
        metrics={
            "currencies": currency_counts,
            "missing_amounts": len(missing_amounts),
            "missing_amount_txn_ids": missing_amounts,
            "distinct_accounts": len(distinct_accounts),
            "nonstandard_accounts": nonstandard_accounts,
            "ledger_scenarios": len(ledger.scenario_ids()),
        },
    ))

    document_count = len(index.records)
    page_count = sum(len(item.pages) for item in index.records)
    low_text_pages = sum(len(item.low_text_pages) for item in index.records)
    checks.append(_check(
        "document_archive",
        "Архив доказательств",
        "pass" if document_count else "fail",
        "PDF проиндексированы постранично и зафиксированы SHA-256." if document_count else "PDF-документы не найдены.",
        hard_gate=True,
        metrics={"documents": document_count, "pages": page_count, "low_text_pages": low_text_pages},
    ))

    scenario_ids = set(answers or {})
    missing_ledger = sorted(scenario_id for scenario_id in scenario_ids if not ledger.for_scenario(scenario_id))
    missing_docs = sorted(
        scenario_id for scenario_id in scenario_ids
        if not index.for_scenario(scenario_id, ledger.account_for_scenario(scenario_id))
    )
    coverage_ok = not missing_ledger and not missing_docs
    checks.append(_check(
        "scenario_coverage",
        "Покрытие сценариев",
        "pass" if coverage_ok else "fail",
        "Каждый сценарий связан с транзакциями и документами." if coverage_ok
        else f"Без транзакций: {missing_ledger or 'нет'}; без документов: {missing_docs or 'нет'}.",
        hard_gate=True,
        metrics={"missing_ledger": missing_ledger, "missing_documents": missing_docs},
    ))

    authority_conflicts: list[dict[str, Any]] = []
    excluded_flags = {"obsolete", "draft", "rejected", "prior_period"}
    for scenario_id in sorted(scenario_ids):
        records = index.for_scenario(scenario_id, ledger.account_for_scenario(scenario_id))
        active = [item for item in records if not excluded_flags.intersection(item.flags)]
        for doc_type in {"loan_agreement", "audit", "aup", "kyc", "treasury_memo"}:
            typed = [item for item in active if item.doc_type == doc_type]
            if len(typed) < 2:
                continue
            top_score = max(item.authority_score for item in typed)
            tied = [item.path.name for item in typed if item.authority_score == top_score]
            if len(tied) > 1:
                authority_conflicts.append({"scenario": scenario_id, "doc_type": doc_type, "files": tied})
    checks.append(_check(
        "authority_resolution",
        "Конфликты версий документов",
        "warning" if authority_conflicts else "pass",
        f"Найдено {len(authority_conflicts)} равноприоритетных групп; semantic review должен выбрать финальную версию."
        if authority_conflicts else "Для каждого типа документа определён однозначный приоритет.",
        metrics={"conflicts": authority_conflicts},
    ))

    if requested_mode == "public":
        route = "public" if public_match else "invalid-public"
    elif requested_mode == "llm":
        route = "llm"
    else:
        route = "public" if public_match else "llm"
    route_ok = route != "invalid-public"
    checks.append(_check(
        "dataset_isolation",
        "Изоляция публичного набора",
        "pass" if route_ok else "fail",
        "Публичный fact pack разрешён только при точном SHA-256 совпадении."
        if route_ok else "Режим public запрещён: fingerprint набора не совпал.",
        hard_gate=True,
        metrics={"requested_mode": requested_mode, "resolved_route": route, "public_fingerprint_match": public_match},
    ))

    if route == "llm":
        has_key = bool(os.environ.get("OPENAI_API_KEY"))
        checks.append(_check(
            "private_model_access",
            "Доступ собственного агента к модели",
            "pass" if has_key else "fail",
            "OPENAI_API_KEY доступен только серверному процессу." if has_key
            else "OPENAI_API_KEY не задан в окружении процесса.",
            hard_gate=True,
        ))
        renderer = find_pdftoppm()
        renderer_ok = low_text_pages == 0 or bool(renderer)
        checks.append(_check(
            "vision_ocr_runtime",
            "Vision-OCR для сканов",
            "pass" if renderer_ok else "fail",
            "Рендер PDF доступен либо скан-страниц нет." if renderer_ok
            else f"Найдено {low_text_pages} страниц без текста, но pdftoppm недоступен.",
            hard_gate=True,
            metrics={"pages_requiring_vision": low_text_pages, "renderer": bool(renderer)},
        ))
    else:
        checks.append(_check(
            "private_model_access",
            "Приватный LLM-контур",
            "warning",
            "Сейчас выбран публичный калибровочный маршрут; приватный маршрут будет проверен на боевом наборе.",
        ))

    metadata_ok, metadata_detail = _metadata_ready(team, contact_email)
    checks.append(_check(
        "submission_metadata",
        "Метаданные команды",
        "pass" if metadata_ok else "warning",
        metadata_detail,
        metrics={"team_ready": metadata_ok, "email_format_valid": bool(EMAIL_RE.fullmatch(contact_email.strip()))},
    ))

    failures = [item for item in checks if item["status"] == "fail"]
    return {
        "status": "fail" if failures else "pass",
        "dataset": str(data_dir),
        "requested_mode": requested_mode,
        "resolved_route": route,
        "hard_gates_passed": not failures,
        "checks": checks,
    }


def build_quality_report(
    *,
    submission: dict[str, Any],
    template: dict[str, Any],
    ledger: Ledger,
    index: DocumentIndex,
    plans: dict[str, list[CovenantPlan]],
    results: list[CovenantResult],
    planning_mode: str,
    output_path: Path,
    plan_review: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        validate_submission(submission, template, set(ledger.by_id))
        schema_error = None
    except SubmissionValidationError as exc:
        schema_error = str(exc)
    checks.append(_check(
        "submission_schema",
        "Строгая схема JSON",
        "pass" if schema_error is None else "fail",
        "Ключи, типы, статусы и evidence соответствуют шаблону." if schema_error is None else schema_error,
        hard_gate=True,
    ))

    metadata_ok, metadata_detail = _metadata_ready(
        str(submission.get("team", "")), str(submission.get("contact_email", ""))
    )
    checks.append(_check(
        "submission_metadata",
        "Готовность метаданных",
        "pass" if metadata_ok else "fail",
        metadata_detail,
        hard_gate=True,
    ))

    document_names = {item.path.name for item in index.records}
    missing_sources: list[str] = []
    empty_sources: list[str] = []
    for scenario_id, scenario_plans in plans.items():
        for plan in scenario_plans:
            cell = f"{scenario_id}/{plan.clause}"
            if not plan.source_documents:
                empty_sources.append(cell)
            for source in set(plan.source_documents) - document_names:
                missing_sources.append(f"{cell}:{source}")
    sources_ok = not missing_sources and not empty_sources
    checks.append(_check(
        "source_grounding",
        "Привязка к документам",
        "pass" if sources_ok else "fail",
        "Каждый расчёт ссылается на существующие PDF." if sources_ok
        else f"Пустые ссылки: {empty_sources}; отсутствующие файлы: {missing_sources}.",
        hard_gate=True,
        metrics={"unique_source_documents": len({x for p in plans.values() for c in p for x in c.source_documents})},
    ))

    bad_evidence: list[str] = []
    unresolved_candidates: list[str] = []
    for result in results:
        cell = f"{result.scenario_id}/{result.clause}"
        if result.evidence_txn_id:
            matching = [
                item for item in result.evidence_counterfactuals
                if item["txn_id"] == result.evidence_txn_id and item["flips_verdict"]
            ]
            if not matching:
                bad_evidence.append(cell)
        elif result.status == "BREACH" and result.evidence_counterfactuals:
            if not any(item["flips_verdict"] for item in result.evidence_counterfactuals):
                unresolved_candidates.append(cell)
    evidence_ok = not bad_evidence
    evidence_status = "fail" if bad_evidence else ("warning" if unresolved_candidates else "pass")
    evidence_detail = "Каждый указанный evidence_txn_id контрфактически меняет вердикт."
    if bad_evidence:
        evidence_detail = f"Не подтверждён контрфактом: {bad_evidence}."
    elif unresolved_candidates:
        evidence_detail = f"Кандидаты не меняют вердикт и не попали в submission: {unresolved_candidates}."
    checks.append(_check(
        "counterfactual_evidence",
        "Контрфактическая улика",
        evidence_status,
        evidence_detail,
        hard_gate=True,
        metrics={"evidence_cells": sum(bool(x.evidence_txn_id) for x in results)},
    ))

    invalid_actuals = [
        f"{result.scenario_id}/{result.clause}" for result in results
        if result.actual < 0 or result.actual.as_tuple().exponent < -2
    ]
    checks.append(_check(
        "decimal_precision",
        "Точность вычислений",
        "pass" if not invalid_actuals else "fail",
        "Расчёты выполнены Decimal; в JSON записано не более двух знаков." if not invalid_actuals
        else f"Некорректная точность actual: {invalid_actuals}.",
        hard_gate=True,
    ))

    boundary_cells: list[dict[str, str]] = []
    for result in results:
        if result.operator in {"<=", "<"}:
            signed_margin = result.threshold - result.decision_value
        else:
            signed_margin = result.decision_value - result.threshold
        relative = signed_margin / max(abs(result.threshold), Decimal("1"))
        if abs(relative) <= Decimal("0.01"):
            boundary_cells.append({
                "cell": f"{result.scenario_id}/{result.clause}",
                "relative_margin": str(relative),
            })
    checks.append(_check(
        "boundary_sensitivity",
        "Чувствительность к порогу",
        "warning" if boundary_cells else "pass",
        f"{len(boundary_cells)} ячеек находятся в пределах 1% от порога и требуют особого внимания."
        if boundary_cells else "Нет решений ближе 1% к порогу.",
        metrics={"cells": boundary_cells},
    ))

    low_text_pages = sum(len(item.low_text_pages) for item in index.records)
    private_route = planning_mode == "llm-semantic-planner"
    review_states = {key: value.get("status") for key, value in (plan_review or {}).items()}
    review_fallbacks = sorted(
        key for key, status in review_states.items()
        if status in {"review_failed_fallback", "review_schema_mismatch_fallback", "disabled"}
    )
    private_status = "pass" if private_route and not review_fallbacks else "warning"
    if private_route and review_fallbacks:
        private_detail = f"Semantic planner выполнен, но review требует внимания: {review_fallbacks}."
    elif private_route:
        private_detail = "Semantic planner и независимый review завершены для каждого сценария."
    else:
        private_detail = "Текущий отчёт относится к публичной SHA-256 калибровке, не к приватному прогону."
    checks.append(_check(
        "private_route_proof",
        "Доказательство приватного маршрута",
        private_status,
        private_detail,
        metrics={
            "planning_mode": planning_mode,
            "remaining_low_text_pages": low_text_pages,
            "review_states": review_states,
            "review_attention": review_fallbacks,
        },
    ))

    hard_failures = [item for item in checks if item["hard_gate"] and item["status"] == "fail"]
    warnings = [item for item in checks if item["status"] == "warning"]
    readiness = max(0, 100 - 30 * len(hard_failures) - 4 * len(warnings))
    return {
        "status": "fail" if hard_failures else ("warning" if warnings else "pass"),
        "hard_gates_passed": not hard_failures,
        "readiness_score": readiness,
        "readiness_score_note": "Операционный индикатор QA, не официальный балл хакатона.",
        "submission_sha256": sha256_file(output_path),
        "cells": len(results),
        "checks": checks,
    }
