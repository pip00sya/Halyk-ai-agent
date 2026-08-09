from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from halyk_agent.io import Ledger, read_json
from halyk_agent.runner import find_required_file, preflight_agent
from halyk_agent.scoring import score_submission
from halyk_agent.validation import validate_submission


PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"
DEFAULT_DATASET = PROJECT_ROOT / "agentic-bank-public"
PRIVATE_DATASET = PROJECT_ROOT / "agentic-bank-hidden"
DEFAULT_SUBMISSION = PROJECT_ROOT / "submission.json"
DEFAULT_ARTIFACTS = PROJECT_ROOT / "artifacts"

DOCUMENTS = {
    "strategy": {
        "title": "Стратегия защиты",
        "description": "90-секундный pitch, отличие от LLM-wrapper и план приватного окна.",
        "path": PROJECT_ROOT / "WINNING_STRATEGY.md",
    },
    "dashboard": {
        "title": "Web-панель",
        "description": "Интерфейс, безопасность, API и сценарии использования.",
        "path": PROJECT_ROOT / "WEB_DASHBOARD.md",
    },
    "analysis": {
        "title": "Полный анализ хакатона",
        "description": "Условия, критерии, стратегия и разбор задачи.",
        "path": PROJECT_ROOT / "HALYK_AI_CHALLENGE_ANALYSIS.md",
    },
    "specification": {
        "title": "Техническое задание",
        "description": "Архитектура агента, контракты и алгоритм работы.",
        "path": PROJECT_ROOT / "TECHNICAL_SPECIFICATION.md",
    },
    "status": {
        "title": "Статус проекта",
        "description": "Что готово, что проверено и какие риски остаются.",
        "path": PROJECT_ROOT / "PROJECT_STATUS.md",
    },
    "checklist": {
        "title": "Чек-лист перед сдачей",
        "description": "Финальная проверка файлов, метаданных и запуска.",
        "path": PROJECT_ROOT / "PRE_SUBMISSION_CHECKLIST.md",
    },
    "readme": {
        "title": "README",
        "description": "Инструкция по установке и запуску решения.",
        "path": PROJECT_ROOT / "README.md",
    },
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def redact(text: str) -> str:
    result = text
    for env_name in ("OPENAI_API_KEY", "API_KEY_21ST"):
        secret = os.environ.get(env_name)
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._state: dict[str, Any] = {
            "status": "idle",
            "message": "Агент готов к запуску",
            "started_at": None,
            "finished_at": None,
            "logs": [],
            "report": load_json_if_exists(DEFAULT_ARTIFACTS / "run_report.json"),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._state["status"] == "running":
                raise ValueError("Агент уже выполняет анализ")

        data_dir = Path(str(payload.get("data_dir") or DEFAULT_DATASET)).expanduser().resolve()
        if not data_dir.is_dir():
            raise ValueError(f"Папка с датасетом не найдена: {data_dir}")

        mode = str(payload.get("mode") or "auto")
        if mode not in {"auto", "public", "llm"}:
            raise ValueError("Неизвестный режим запуска")
        if mode == "llm" and not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("Для LLM-режима задайте OPENAI_API_KEY в окружении сервера")

        team = str(payload.get("team") or "CHANGE_ME_TEAM").strip()
        contact_email = str(payload.get("contact_email") or "CHANGE_ME_EMAIL").strip()
        model = str(payload.get("model") or "gpt-5.6").strip()
        reasoning = str(payload.get("reasoning_effort") or "high")
        if reasoning not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Недопустимый reasoning effort")
        review = bool(payload.get("review", True))

        command = [
            sys.executable,
            str(PROJECT_ROOT / "run_agent.py"),
            "run",
            "--data",
            str(data_dir),
            "--output",
            str(DEFAULT_SUBMISSION),
            "--artifacts",
            str(DEFAULT_ARTIFACTS),
            "--team",
            team,
            "--contact-email",
            contact_email,
            "--mode",
            mode,
            "--model",
            model,
            "--reasoning-effort",
            reasoning,
        ]
        if not review:
            command.append("--single-pass")

        with self._lock:
            self._state = {
                "status": "running",
                "message": "Агент читает документы и проверяет ковенанты",
                "started_at": utc_now(),
                "finished_at": None,
                "mode": mode,
                "data_dir": str(data_dir),
                "logs": [f"Запуск режима {mode}: {data_dir.name}"],
                "report": None,
            }

        thread = threading.Thread(target=self._execute, args=(command,), daemon=True)
        thread.start()
        return self.snapshot()

    def _execute(self, command: list[str]) -> None:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
            with self._lock:
                self._process = process
            assert process.stdout is not None
            for line in process.stdout:
                cleaned = redact(line.rstrip())
                if not cleaned:
                    continue
                with self._lock:
                    self._state["logs"] = (self._state["logs"] + [cleaned])[-250:]
            return_code = process.wait()
            report = load_json_if_exists(DEFAULT_ARTIFACTS / "run_report.json")
            with self._lock:
                self._process = None
                self._state["finished_at"] = utc_now()
                self._state["report"] = report
                if return_code == 0:
                    self._state["status"] = "success"
                    self._state["message"] = "Анализ завершён, submission.json готов"
                else:
                    self._state["status"] = "error"
                    self._state["message"] = f"Агент завершился с кодом {return_code}"
        except Exception as exc:  # UI boundary: surface an actionable error to the operator.
            with self._lock:
                self._process = None
                self._state["status"] = "error"
                self._state["message"] = str(exc)
                self._state["finished_at"] = utc_now()
                self._state["logs"] = (self._state["logs"] + [redact(str(exc))])[-250:]


RUN_MANAGER = RunManager()


def public_score() -> dict[str, Any] | None:
    ground_truth = DEFAULT_DATASET / "ground_truth.json"
    if not DEFAULT_SUBMISSION.exists() or not ground_truth.exists():
        return None
    try:
        score = score_submission(read_json(DEFAULT_SUBMISSION), read_json(ground_truth))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {key: score[key] for key in ("score", "points", "max_points")}


def status_payload() -> dict[str, Any]:
    report = load_json_if_exists(DEFAULT_ARTIFACTS / "run_report.json") or {}
    preflight = load_json_if_exists(DEFAULT_ARTIFACTS / "preflight_report.json")
    quality = load_json_if_exists(DEFAULT_ARTIFACTS / "quality_report.json")
    submission = load_json_if_exists(DEFAULT_SUBMISSION) or {}
    metadata = {
        "team": submission.get("team"),
        "contact_email": submission.get("contact_email"),
        "model": submission.get("model"),
    }
    return {
        "project_root": str(PROJECT_ROOT),
        "default_dataset": str(DEFAULT_DATASET),
        "private_dataset": str(PRIVATE_DATASET) if PRIVATE_DATASET.is_dir() else None,
        "dataset_ready": DEFAULT_DATASET.is_dir(),
        "submission_ready": DEFAULT_SUBMISSION.is_file(),
        "openai_api_key_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "report": report,
        "preflight": preflight,
        "quality": quality,
        "metadata": metadata,
        "public_score": public_score(),
        "documents": [
            {"id": key, "title": value["title"], "description": value["description"]}
            for key, value in DOCUMENTS.items()
            if value["path"].is_file()
        ],
    }


def results_payload() -> dict[str, Any]:
    submission = load_json_if_exists(DEFAULT_SUBMISSION)
    trace = load_json_if_exists(DEFAULT_ARTIFACTS / "decision_trace.json")
    if not submission:
        return {"rows": [], "summary": {}, "planning_mode": None}

    trace_items = (trace or {}).get("results", [])
    trace_map = {
        (item.get("scenario_id"), item.get("clause")): item
        for item in trace_items
        if isinstance(item, dict)
    }
    rows: list[dict[str, Any]] = []
    compliant = 0
    breach = 0
    evidence = 0
    for scenario_id, clauses in submission.get("answers", {}).items():
        for clause, answer in clauses.items():
            detail = trace_map.get((scenario_id, clause), {})
            row = {
                "scenario_id": scenario_id,
                "clause": clause,
                "status": answer.get("status"),
                "base_status": detail.get("base_status"),
                "status_override_applied": detail.get("status_override_applied", False),
                "status_override": detail.get("status_override"),
                "actual": answer.get("actual"),
                "evidence_txn_id": answer.get("evidence_txn_id"),
                "threshold": detail.get("threshold"),
                "operator": detail.get("operator"),
                "raw_actual": detail.get("raw_actual"),
                "decision_value": detail.get("decision_value"),
                "compare_round": detail.get("compare_round"),
                "signed_margin": detail.get("signed_margin"),
                "relative_margin": detail.get("relative_margin"),
                "rationale": detail.get("rationale"),
                "source_documents": detail.get("source_documents", []),
                "facts": detail.get("facts", {}),
                "evidence_counterfactuals": detail.get("evidence_counterfactuals", []),
            }
            rows.append(row)
            compliant += answer.get("status") == "COMPLIANT"
            breach += answer.get("status") == "BREACH"
            evidence += bool(answer.get("evidence_txn_id"))
    return {
        "rows": rows,
        "summary": {
            "total": len(rows),
            "compliant": compliant,
            "breach": breach,
            "evidence": evidence,
        },
        "planning_mode": (trace or {}).get("planning_mode"),
        "metadata": {key: submission.get(key) for key in ("team", "contact_email", "model")},
    }


def validate_current(payload: dict[str, Any]) -> dict[str, Any]:
    data_dir = Path(str(payload.get("data_dir") or DEFAULT_DATASET)).expanduser().resolve()
    submission_path = Path(str(payload.get("submission_path") or DEFAULT_SUBMISSION)).expanduser().resolve()
    template_path = find_required_file(data_dir, "submission_template.json")
    ledger_path = find_required_file(data_dir, "master_ledger_2025.csv")
    ledger = Ledger.from_csv(ledger_path)
    validate_submission(read_json(submission_path), read_json(template_path), set(ledger.by_id))
    return {"valid": True, "message": "Структура и ссылки на транзакции корректны"}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "HalykDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, *, download_name: str | None = None) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".js", ".css", ".html", ".svg", ".md", ".json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if download_name:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", download_name)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Некорректная длина запроса") from exc
        if length > 64_000:
            raise ValueError("Запрос слишком большой")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Ожидался JSON-объект")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._send_json(status_payload())
            return
        if parsed.path == "/api/run":
            self._send_json(RUN_MANAGER.snapshot())
            return
        if parsed.path == "/api/results":
            self._send_json(results_payload())
            return
        if parsed.path == "/api/document":
            doc_id = parse_qs(parsed.query).get("id", [""])[0]
            document = DOCUMENTS.get(doc_id)
            if not document or not document["path"].is_file():
                self._send_json({"error": "Документ не найден"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "id": doc_id,
                "title": document["title"],
                "content": document["path"].read_text(encoding="utf-8"),
            })
            return
        if parsed.path == "/api/download/submission":
            self._send_file(DEFAULT_SUBMISSION, download_name="submission.json")
            return
        if parsed.path == "/api/download/trace":
            self._send_file(DEFAULT_ARTIFACTS / "decision_trace.json", download_name="decision_trace.json")
            return
        if parsed.path == "/api/download/quality":
            self._send_file(DEFAULT_ARTIFACTS / "quality_report.json", download_name="quality_report.json")
            return
        if parsed.path == "/api/download/preflight":
            self._send_file(DEFAULT_ARTIFACTS / "preflight_report.json", download_name="preflight_report.json")
            return
        if parsed.path == "/api/download/plans":
            self._send_file(DEFAULT_ARTIFACTS / "semantic_plans.json", download_name="semantic_plans.json")
            return

        static_files = {
            "/": WEB_ROOT / "index.html",
            "/index.html": WEB_ROOT / "index.html",
            "/styles.css": WEB_ROOT / "styles.css",
            "/app.js": WEB_ROOT / "app.js",
            "/favicon.svg": WEB_ROOT / "favicon.svg",
        }
        path = static_files.get(parsed.path)
        if path:
            self._send_file(path)
            return
        if parsed.path.startswith("/assets/"):
            assets_root = (WEB_ROOT / "assets").resolve()
            asset_path = (WEB_ROOT / parsed.path.lstrip("/")).resolve()
            if asset_path.parent == assets_root:
                self._send_file(asset_path)
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/run":
                self._send_json(RUN_MANAGER.start(payload), HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/validate":
                self._send_json(validate_current(payload))
                return
            if parsed.path == "/api/preflight":
                data_dir = Path(str(payload.get("data_dir") or DEFAULT_DATASET)).expanduser().resolve()
                mode = str(payload.get("mode") or "auto")
                if mode not in {"auto", "public", "llm"}:
                    raise ValueError("Неизвестный режим запуска")
                report = preflight_agent(
                    data_dir=data_dir,
                    mode=mode,
                    team=str(payload.get("team") or "CHANGE_ME_TEAM"),
                    contact_email=str(payload.get("contact_email") or "CHANGE_ME_EMAIL"),
                    artifacts_dir=DEFAULT_ARTIFACTS,
                )
                self._send_json(report)
                return
            self._send_json({"error": "Маршрут не найден"}, HTTPStatus.NOT_FOUND)
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # Keep local dashboard errors visible without exposing a traceback.
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Halyk AI dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (localhost by default)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the dashboard automatically")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Halyk AI Command Center: {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
