from __future__ import annotations

import copy
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .documents import DocumentIndex
from .engine import execute_plan
from .io import Ledger, read_json, write_json
from .llm import LLMError, LLMPlanner, OpenAIResponsesClient, vision_ocr_record
from .models import CovenantResult
from .plans import load_fact_pack, matching_fact_pack, validate_plan
from .quality import build_preflight_report, build_quality_report
from .validation import validate_submission


PACKAGE_DIR = Path(__file__).resolve().parent
PUBLIC_FACT_PACK = PACKAGE_DIR / "config" / "public_fact_pack.json"


def find_required_file(data_dir: Path, filename: str) -> Path:
    direct = data_dir / filename
    if direct.exists():
        return direct
    matches = list(data_dir.rglob(filename))
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected exactly one {filename} under {data_dir}, found {len(matches)}")
    return matches[0]


def preflight_agent(
    data_dir: Path,
    mode: str = "auto",
    team: str = "CHANGE_ME_TEAM",
    contact_email: str = "CHANGE_ME_EMAIL",
    artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    """Inspect every hard dependency without calling an LLM or creating answers."""
    data_dir = data_dir.resolve()
    template_path = find_required_file(data_dir, "submission_template.json")
    ledger_path = find_required_file(data_dir, "master_ledger_2025.csv")
    documents_dir = template_path.parent / "documents"
    if not documents_dir.exists():
        raise FileNotFoundError(f"Documents directory not found: {documents_dir}")
    artifacts_dir = (artifacts_dir or Path("artifacts")).resolve()
    template = read_json(template_path)
    ledger = Ledger.from_csv(ledger_path)
    index = DocumentIndex.build(
        documents_dir,
        artifacts_dir / "document_text_cache",
        {txn.account_id for txn in ledger.transactions},
    )
    public_match = matching_fact_pack(PUBLIC_FACT_PACK, template_path.parent)
    report = build_preflight_report(
        data_dir=data_dir,
        template=template,
        ledger=ledger,
        index=index,
        requested_mode=mode,
        public_match=public_match,
        team=team,
        contact_email=contact_email,
    )
    write_json(artifacts_dir / "preflight_report.json", report)
    return report


def run_agent(
    data_dir: Path,
    output_path: Path,
    team: str,
    contact_email: str,
    mode: str = "auto",
    model: str = "gpt-5.6",
    reasoning_effort: str = "high",
    review: bool = True,
    workers: int = 3,
    artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    data_dir = data_dir.resolve()
    if workers < 1 or workers > 6:
        raise ValueError("workers must be between 1 and 6")
    template_path = find_required_file(data_dir, "submission_template.json")
    ledger_path = find_required_file(data_dir, "master_ledger_2025.csv")
    documents_dir = template_path.parent / "documents"
    if not documents_dir.exists():
        raise FileNotFoundError(f"Documents directory not found: {documents_dir}")
    template = read_json(template_path)
    ledger = Ledger.from_csv(ledger_path)
    artifacts_dir = (artifacts_dir or output_path.parent / "artifacts").resolve()
    cache_dir = artifacts_dir / "document_text_cache"
    index = DocumentIndex.build(
        documents_dir,
        cache_dir,
        {txn.account_id for txn in ledger.transactions},
    )

    public_match = matching_fact_pack(PUBLIC_FACT_PACK, template_path.parent)
    review_log: dict[str, dict[str, Any]] = {}
    client: OpenAIResponsesClient | None = None
    preflight = build_preflight_report(
        data_dir=data_dir,
        template=template,
        ledger=ledger,
        index=index,
        requested_mode=mode,
        public_match=public_match,
        team=team,
        contact_email=contact_email,
    )
    write_json(artifacts_dir / "preflight_report.json", preflight)
    if not preflight["hard_gates_passed"]:
        failed = [item["title"] for item in preflight["checks"] if item["status"] == "fail"]
        raise ValueError(f"Preflight failed: {', '.join(failed)}")
    if mode == "public" and not public_match:
        raise ValueError("--mode public is allowed only for the fingerprinted public dataset")
    if mode in {"auto", "public"} and public_match:
        plans = load_fact_pack(PUBLIC_FACT_PACK, template_path.parent)
        model_label = "hybrid-deterministic-public-v1"
        planning_mode = "fingerprinted-public-fact-pack"
    else:
        client = OpenAIResponsesClient(model=model, reasoning_effort=reasoning_effort)
        planner = LLMPlanner(client)
        vision_cache = artifacts_dir / "vision_ocr_cache"
        vision_cache.mkdir(parents=True, exist_ok=True)
        for record in index.records:
            if not record.low_text_pages:
                continue
            cache_file = vision_cache / f"{record.sha256}.txt"
            if cache_file.exists():
                ocr_text = cache_file.read_text(encoding="utf-8")
            else:
                try:
                    ocr_text = vision_ocr_record(client, record)
                except LLMError as exc:
                    raise LLMError(f"Vision OCR failed for {record.path.name}: {exc}") from exc
                cache_file.write_text(ocr_text, encoding="utf-8")
            index.replace_text(record, ocr_text)
        planned: dict[str, list[Any]] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="halyk-plan") as pool:
            futures = {}
            for scenario_id, clauses in template["answers"].items():
                account_id = ledger.account_for_scenario(scenario_id)
                records = index.for_scenario(scenario_id, account_id)
                future = pool.submit(
                    planner.plan_scenario,
                    scenario_id,
                    account_id,
                    list(clauses),
                    records,
                    ledger,
                    review,
                )
                futures[future] = scenario_id
            for future in as_completed(futures):
                scenario_id = futures[future]
                planned[scenario_id] = future.result()
        plans = {scenario_id: planned[scenario_id] for scenario_id in template["answers"]}
        model_label = model
        planning_mode = "llm-semantic-planner"
        write_json(artifacts_dir / "plan_review.json", {"scenarios": planner.review_log})
        review_log = planner.review_log

    required_scenarios = set(template["answers"])
    if set(plans) != required_scenarios:
        raise ValueError(f"Plan scenarios differ from template: {sorted(set(plans) ^ required_scenarios)}")

    submission = copy.deepcopy(template)
    submission["team"] = team
    submission["contact_email"] = contact_email
    submission["model"] = model_label
    traces: list[dict[str, Any]] = []
    results: list[CovenantResult] = []
    document_names = {item.path.name for item in index.records}
    semantic_plans = {
        scenario_id: [plan.trace_value() for plan in scenario_plans]
        for scenario_id, scenario_plans in plans.items()
    }
    write_json(
        artifacts_dir / "semantic_plans.json",
        {"planning_mode": planning_mode, "scenarios": semantic_plans},
    )
    for scenario_id, clauses in template["answers"].items():
        scenario_plans = {plan.clause: plan for plan in plans[scenario_id]}
        if set(scenario_plans) != set(clauses):
            raise ValueError(f"{scenario_id}: plan clause mismatch")
        for clause in clauses:
            plan = scenario_plans[clause]
            validate_plan(plan, set(ledger.by_id), document_names)
            result = execute_plan(scenario_id, plan, ledger)
            submission["answers"][scenario_id][clause] = result.submission_value()
            traces.append(result.trace_value())
            results.append(result)

    validate_submission(submission, template, set(ledger.by_id))
    write_json(output_path.resolve(), submission)
    write_json(artifacts_dir / "decision_trace.json", {"planning_mode": planning_mode, "results": traces})
    manifest = index.manifest()
    write_json(artifacts_dir / "document_manifest.json", manifest)
    quality = build_quality_report(
        submission=submission,
        template=template,
        ledger=ledger,
        index=index,
        plans=plans,
        results=results,
        planning_mode=planning_mode,
        output_path=output_path.resolve(),
        plan_review=review_log,
    )
    write_json(artifacts_dir / "quality_report.json", quality)
    report = {
        "status": "ok" if quality["hard_gates_passed"] else "attention_required",
        "planning_mode": planning_mode,
        "model": model_label,
        "data_dir": str(data_dir),
        "output": str(output_path.resolve()),
        "transactions": len(ledger.transactions),
        "documents": len(index.records),
        "pages": sum(len(item.pages) for item in index.records),
        "low_text_pages": sum(len(item.low_text_pages) for item in index.records),
        "scenarios": len(template["answers"]),
        "covenant_cells": sum(len(value) for value in template["answers"].values()),
        "metadata_placeholders": [
            key for key, value in {"team": team, "contact_email": contact_email}.items()
            if not value or value.startswith("CHANGE_ME")
        ],
        "openai_api_key_used": planning_mode == "llm-semantic-planner" and bool(os.environ.get("OPENAI_API_KEY")),
        "llm_second_pass_review": planning_mode == "llm-semantic-planner" and review,
        "planning_workers": workers if planning_mode == "llm-semantic-planner" else 0,
        "llm_usage": client.usage_snapshot() if client is not None else None,
        "quality_status": quality["status"],
        "hard_gates_passed": quality["hard_gates_passed"],
        "readiness_score": quality["readiness_score"],
        "submission_sha256": quality["submission_sha256"],
    }
    write_json(artifacts_dir / "run_report.json", report)
    return report
