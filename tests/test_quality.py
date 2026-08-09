from __future__ import annotations

import copy
import json
import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from halyk_agent.documents import DocumentIndex, classify_document, extract_account_ids
from halyk_agent.engine import execute_plan
from halyk_agent.io import Ledger, read_json
from halyk_agent.llm import LLMPlanner, normalize_plan_payload, parse_json_response, plan_response_schema
from halyk_agent.models import CovenantPlan, DocumentRecord, Transaction
from halyk_agent.plans import load_fact_pack, parse_plan, validate_plan
from halyk_agent.runner import PUBLIC_FACT_PACK, preflight_agent
from halyk_agent.validation import SubmissionValidationError, validate_submission


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agentic-bank-public"


def simple_plan(**overrides: object) -> CovenantPlan:
    values = {
        "clause": "6.1",
        "title": "Limit",
        "operator": "<=",
        "threshold": Decimal("100"),
        "expression": "payments",
        "facts": {"payments": {"type": "txn", "ids": ["TXN-T1-0001"]}},
        "evidence_candidates": ["TXN-T1-0001"],
        "compare_round": None,
        "source_documents": ["loan.pdf"],
        "rationale": "Transaction determines the breach.",
    }
    values.update(overrides)
    return CovenantPlan(**values)  # type: ignore[arg-type]


class PlanSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = Ledger([
            Transaction(
                txn_id="TXN-T1-0001",
                date="2025-01-01",
                account_id="ACC-1",
                counterparty="Vendor",
                description="Payment",
                amount=Decimal("-120"),
                currency="USD",
            )
        ])

    def test_plan_rejects_hallucinated_document(self) -> None:
        with self.assertRaisesRegex(ValueError, "source documents are absent"):
            validate_plan(simple_plan(), set(self.ledger.by_id), {"different.pdf"})

    def test_plan_rejects_unrelated_evidence_candidate(self) -> None:
        plan = simple_plan(
            facts={"payments": {"type": "literal", "value": "120", "source": "loan.pdf"}}
        )
        with self.assertRaisesRegex(ValueError, "not used by transaction facts"):
            validate_plan(plan, set(self.ledger.by_id), {"loan.pdf"})

    def test_parse_plan_rejects_nonstandard_comparison_rounding(self) -> None:
        with self.assertRaisesRegex(ValueError, "compare_round"):
            parse_plan({
                "clause": "6.1",
                "title": "x",
                "operator": "<=",
                "threshold": "1",
                "expression": "x",
                "facts": {"x": {"type": "literal", "value": "1", "source": "x.pdf"}},
                "compare_round": 4,
            })

    def test_counterfactual_is_persisted_in_trace(self) -> None:
        result = execute_plan("T1", simple_plan(), self.ledger)
        self.assertEqual(result.status, "BREACH")
        self.assertEqual(result.evidence_txn_id, "TXN-T1-0001")
        trace = result.trace_value()
        self.assertTrue(trace["evidence_counterfactuals"][0]["flips_verdict"])
        self.assertEqual(trace["evidence_counterfactuals"][0]["status_without_transaction"], "COMPLIANT")

    def test_documented_exception_keeps_true_actual_and_overrides_status(self) -> None:
        plan = simple_plan(
            facts={
                "payments": {"type": "txn", "ids": ["TXN-T1-0001"]},
                "waiver_approved": {"type": "literal", "value": "1", "source": "loan.pdf"},
            },
            evidence_candidates=[],
            status_override={
                "when": "waiver_approved",
                "status": "COMPLIANT",
                "reason": "The final waiver permits this excess.",
            },
        )
        validate_plan(plan, set(self.ledger.by_id), {"loan.pdf"})
        result = execute_plan("T1", plan, self.ledger)
        self.assertEqual(result.actual, Decimal("120.00"))
        self.assertEqual(result.base_status, "BREACH")
        self.assertEqual(result.status, "COMPLIANT")
        self.assertTrue(result.status_override_applied)


class AuthorityResolutionTests(unittest.TestCase):
    def test_rejected_current_agreement_is_penalized(self) -> None:
        doc_type, score, flags = classify_document(
            "Loan Agreement 2025. FINAL PROPOSAL NOT APPROVED by the bank."
        )
        self.assertEqual(doc_type, "loan_agreement")
        self.assertIn("rejected", flags)
        self.assertIn("current_period", flags)
        self.assertLess(score, 100)

    def test_prior_period_flag_applies_to_typed_document(self) -> None:
        doc_type, score, flags = classify_document("ДОГОВОР БАНКОВСКОГО ЗАЙМА от 1 января 2024 года")
        self.assertEqual(doc_type, "loan_agreement")
        self.assertIn("prior_period", flags)
        self.assertEqual(score, 70)

    def test_known_nonstandard_account_is_resolved_without_broad_guessing(self) -> None:
        text = "Borrower account TELE-4471; report AR-2025-0116."
        self.assertEqual(extract_account_ids(text, {"TELE-4471"}), ["TELE-4471"])

    def test_ocr_consolidation_does_not_schedule_the_same_pages_twice(self) -> None:
        record = DocumentRecord(
            path=Path("scan.pdf"),
            sha256="0" * 64,
            pages=["", "original text"],
            page_char_counts=[0, 100],
        )
        index = DocumentIndex([record], {"TELE-4471"})
        index.replace_text(record, "Current agreement for account TELE-4471 with enough OCR text.")
        self.assertEqual(record.low_text_pages, [])
        self.assertEqual(record.account_ids, ["TELE-4471"])


class PrivatePlannerContractTests(unittest.TestCase):
    def test_planner_and_independent_review_work_without_network_in_contract_test(self) -> None:
        response = json.dumps({
            "covenants": [{
                "clause": "6.1",
                "title": "Payment limit",
                "operator": "<=",
                "threshold": "100",
                "expression": "payments",
                "facts": {"payments": {"type": "txn", "ids": ["TXN-T1-0001"]}},
                "evidence_candidates": ["TXN-T1-0001"],
                "compare_round": None,
                "source_documents": ["loan.pdf"],
                "rationale": "The payment exceeds the limit.",
            }]
        })

        class FakeClient:
            def respond(self, prompt: str, images: list[Path] | None = None) -> str:
                return response

        ledger = Ledger([
            Transaction("TXN-T1-0001", "2025-01-01", "ACC-1", "Vendor", "Payment", Decimal("-120"), "USD")
        ])
        record = DocumentRecord(
            path=Path("loan.pdf"),
            sha256="0" * 64,
            pages=["Loan Agreement 2025 " * 10],
            page_char_counts=[200],
            doc_type="loan_agreement",
            authority_score=110,
            account_ids=["ACC-1"],
            flags=["current_period"],
        )
        planner = LLMPlanner(FakeClient())  # type: ignore[arg-type]
        plans = planner.plan_scenario("T1", "ACC-1", ["6.1"], [record], ledger, review=True)
        self.assertEqual(len(plans), 1)
        self.assertEqual(planner.review_log["T1"]["status"], "confirmed")

    def test_fenced_json_is_accepted(self) -> None:
        self.assertEqual(parse_json_response("```json\n{\"ok\": true}\n```"), {"ok": True})

    def test_strict_plan_payload_normalizes_fact_list(self) -> None:
        raw = {
            "covenants": [{
                "clause": "6.1",
                "facts": [{
                    "name": "payments", "type": "txn", "value": None, "source": None,
                    "note": None, "ids": ["TXN-T1-0001"], "absolute": True,
                    "fallback": None, "expression": None,
                }],
                "status_override": {
                    "enabled": False, "when": "0", "status": "COMPLIANT", "reason": "",
                },
            }]
        }
        normalized = normalize_plan_payload(raw)
        self.assertEqual(normalized["covenants"][0]["facts"]["payments"]["ids"], ["TXN-T1-0001"])
        self.assertIsNone(normalized["covenants"][0]["status_override"])
        schema = plan_response_schema(["6.1"])
        self.assertFalse(schema["additionalProperties"])


class OperationalGateTests(unittest.TestCase):
    def test_ledger_context_includes_cross_referenced_transaction(self) -> None:
        ledger = Ledger([
            Transaction("TXN-T1-0001", "2025-01-01", "ACC-1", "A", "Primary", Decimal("1"), "USD"),
            Transaction("TXN-X9-0002", "2025-01-02", "ACC-2", "B", "Referenced", Decimal("2"), "USD"),
            Transaction("TXN-Z9-0003", "2025-01-03", "ACC-3", "C", "Noise", Decimal("3"), "USD"),
        ])
        context = ledger.context_for_scenario("T1", "ACC-1", {"TXN-X9-0002"})
        self.assertEqual([item.txn_id for item in context], ["TXN-T1-0001", "TXN-X9-0002"])

    def test_forced_private_preflight_requires_server_api_key(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            report = preflight_agent(
                DATA,
                mode="llm",
                team="Test",
                contact_email="test@example.com",
                artifacts_dir=ROOT / "tmp" / "test_preflight",
            )
        access = next(item for item in report["checks"] if item["id"] == "private_model_access")
        self.assertEqual(access["status"], "fail")
        self.assertFalse(report["hard_gates_passed"])

    def test_submission_rejects_negative_actual(self) -> None:
        template = read_json(DATA / "submission_template.json")
        ledger = Ledger.from_csv(DATA / "master_ledger_2025.csv")
        plans = load_fact_pack(PUBLIC_FACT_PACK, DATA)
        submission = copy.deepcopy(template)
        submission.update(team="test", contact_email="test@example.com", model="test")
        for scenario_id, clauses in template["answers"].items():
            by_clause = {plan.clause: plan for plan in plans[scenario_id]}
            for clause in clauses:
                submission["answers"][scenario_id][clause] = execute_plan(
                    scenario_id, by_clause[clause], ledger
                ).submission_value()
        submission["answers"]["P1"]["6.1"]["actual"] = -1.0
        with self.assertRaises(SubmissionValidationError):
            validate_submission(submission, template, set(ledger.by_id))


if __name__ == "__main__":
    unittest.main()
