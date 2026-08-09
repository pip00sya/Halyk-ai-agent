from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .io import Ledger, read_json
from .runner import find_required_file, preflight_agent, run_agent
from .scoring import score_submission
from .validation import validate_submission


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Halyk covenant analysis agent")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Analyze a dataset and produce submission.json")
    run.add_argument("--data", type=Path, required=True)
    run.add_argument("--output", type=Path, default=Path("submission.json"))
    run.add_argument("--artifacts", type=Path, default=None)
    run.add_argument("--team", default=os.environ.get("HALYK_TEAM", "CHANGE_ME_TEAM"))
    run.add_argument("--contact-email", default=os.environ.get("HALYK_CONTACT_EMAIL", "CHANGE_ME_EMAIL"))
    run.add_argument("--mode", choices=["auto", "public", "llm"], default="auto")
    run.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.6"))
    run.add_argument("--reasoning-effort", choices=["low", "medium", "high", "xhigh", "max"], default="high")
    run.add_argument("--single-pass", action="store_true", help="Disable the independent LLM review pass")
    run.add_argument("--workers", type=int, default=int(os.environ.get("HALYK_LLM_WORKERS", "3")), help="Parallel scenario planners (1-6)")

    preflight = sub.add_parser("preflight", help="Check dataset and runtime readiness without calling an LLM")
    preflight.add_argument("--data", type=Path, required=True)
    preflight.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    preflight.add_argument("--team", default=os.environ.get("HALYK_TEAM", "CHANGE_ME_TEAM"))
    preflight.add_argument("--contact-email", default=os.environ.get("HALYK_CONTACT_EMAIL", "CHANGE_ME_EMAIL"))
    preflight.add_argument("--mode", choices=["auto", "public", "llm"], default="auto")

    validate = sub.add_parser("validate", help="Validate a submission against a dataset template")
    validate.add_argument("--data", type=Path, required=True)
    validate.add_argument("--submission", type=Path, required=True)

    score = sub.add_parser("score", help="Score a public submission locally")
    score.add_argument("--submission", type=Path, required=True)
    score.add_argument("--ground-truth", type=Path, required=True)
    score.add_argument("--details", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith("-"):
        argv.insert(0, "run")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        report = run_agent(
            data_dir=args.data,
            output_path=args.output,
            team=args.team,
            contact_email=args.contact_email,
            mode=args.mode,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            review=not args.single_pass,
            workers=args.workers,
            artifacts_dir=args.artifacts,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "preflight":
        report = preflight_agent(
            data_dir=args.data,
            mode=args.mode,
            team=args.team,
            contact_email=args.contact_email,
            artifacts_dir=args.artifacts,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["hard_gates_passed"] else 1
    if args.command == "validate":
        template_path = find_required_file(args.data.resolve(), "submission_template.json")
        ledger_path = find_required_file(args.data.resolve(), "master_ledger_2025.csv")
        ledger = Ledger.from_csv(ledger_path)
        validate_submission(read_json(args.submission), read_json(template_path), set(ledger.by_id))
        print("VALID")
        return 0
    if args.command == "score":
        result = score_submission(read_json(args.submission), read_json(args.ground_truth))
        if not args.details:
            result = {key: result[key] for key in ("score", "points", "max_points")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 2
