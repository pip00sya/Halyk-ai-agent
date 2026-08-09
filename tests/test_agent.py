from __future__ import annotations

import copy
import unittest
from pathlib import Path

from halyk_agent.engine import execute_plan
from halyk_agent.expressions import ExpressionError, evaluate
from halyk_agent.io import Ledger, read_json
from halyk_agent.plans import load_fact_pack
from halyk_agent.runner import PUBLIC_FACT_PACK
from halyk_agent.scoring import score_submission
from halyk_agent.validation import SubmissionValidationError, validate_submission


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agentic-bank-public"


class PublicBenchmarkTests(unittest.TestCase):
    def test_public_plans_reproduce_all_cells_without_reading_truth(self) -> None:
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
        result = score_submission(submission, read_json(DATA / "ground_truth.json"))
        self.assertEqual(result["score"], 1.0)
        self.assertEqual(result["points"], 36.0)

    def test_submission_validation_rejects_bad_status(self) -> None:
        template = read_json(DATA / "submission_template.json")
        ledger = Ledger.from_csv(DATA / "master_ledger_2025.csv")
        broken = copy.deepcopy(template)
        broken.update(team="x", contact_email="x@y.kz", model="x")
        for scenario in broken["answers"].values():
            for cell in scenario.values():
                cell.update(status="COMPLIANT", actual=0.0, evidence_txn_id=None)
        broken["answers"]["P1"]["6.1"]["status"] = "compliant"
        with self.assertRaises(SubmissionValidationError):
            validate_submission(broken, template, set(ledger.by_id))

    def test_scenario_ids_support_non_numeric_codes(self) -> None:
        from decimal import Decimal
        from halyk_agent.models import Transaction

        ledger = Ledger([
            Transaction("TXN-KC-0001", "2025-01-01", "TELE-4471", "Vendor", "Payment", Decimal("1"), "USD")
        ])
        self.assertEqual(ledger.scenario_ids(), ["KC"])


class ExpressionTests(unittest.TestCase):
    def test_decimal_expression(self) -> None:
        from decimal import Decimal

        value = evaluate("revenue - max(payroll, taxes)", {
            "revenue": Decimal("7204882.16"),
            "payroll": Decimal("1204118.53"),
            "taxes": Decimal("882447.19"),
        })
        self.assertEqual(value, Decimal("6000763.63"))

    def test_unsafe_expression_is_rejected(self) -> None:
        with self.assertRaises(ExpressionError):
            evaluate("__import__('os').system('echo unsafe')", {})


if __name__ == "__main__":
    unittest.main()
