from __future__ import annotations

import unittest


class RefinementTests(unittest.TestCase):
    def test_controller_assigns_finding_ids_and_blocks_unresolved_critical(self) -> None:
        from scripts.optim_plans_core import RefinementLedger

        ledger = RefinementLedger()
        finding = ledger.add_finding("critical", ["REQ-001"], "missing nonce", "add nonce")
        self.assertEqual(finding["id"], "F-001")
        self.assertFalse(ledger.converged())
        ledger.disposition("F-001", "changed", "PLAN_v2 covers nonce")
        self.assertTrue(ledger.converged())

    def test_reviewer_and_criticizer_items_are_bounded(self) -> None:
        from scripts.optim_plans_core import ContractError, RefinementLedger

        ledger = RefinementLedger(max_items_per_round=3)
        for index in range(3):
            ledger.add_finding("high", ["REQ-001"], f"review {index}", "fix")
        with self.assertRaises(ContractError):
            ledger.add_finding("high", ["REQ-001"], "review 3", "fix")

        ledger = RefinementLedger(max_items_per_round=3)
        for index in range(3):
            ledger.add_criticizer_question(f"Q{index}")
        with self.assertRaises(ContractError):
            ledger.add_criticizer_question("Q3")


if __name__ == "__main__":
    unittest.main()
