import unittest

from src.graphql_client import COVERAGE_NOTE, GRAPHQL_REGISTRY
from src.graphql_validator import RAW_FIELD_PREFIXES
from src.query_sub_agents import ALL_SUBAGENTS


class TestRegistry(unittest.TestCase):
    def test_every_sub_agent_method_is_registered(self):
        for agent in ALL_SUBAGENTS:
            for method in agent.graphql_methods:
                with self.subTest(agent=agent.name, method=method):
                    self.assertIn(method, GRAPHQL_REGISTRY)

    def test_no_raw_payout_table_is_recommended(self):
        self.assertFalse([name for name in GRAPHQL_REGISTRY if name.startswith(RAW_FIELD_PREFIXES)])

    def test_settlement_functions_state_the_coverage_rule(self):
        # The functions that read the settlement tables raise an error outside their coverage; the model must be
        # told to report "not covered yet" rather than 0.
        settlement = [
            name
            for name in GRAPHQL_REGISTRY
            if name.startswith("legacy")
            or name
            in (
                "getIncomeJson",
                "getSupplierEarningsJson",
                "getSupplierProofsJson",
                "getSupplierPenaltiesJson",
                "getSupplierDistributionJson",
                "getApplicationSpendJson",
                "getGatewaySpendJson",
                "getServiceUsageJson",
                "getSupplyFlowsJson",
                "getValidatorRewardsJson",
                "getDelegatorIncomeJson",
            )
        ]
        self.assertEqual(len(settlement), 14)
        for name in settlement:
            with self.subTest(method=name):
                self.assertEqual(GRAPHQL_REGISTRY[name].fields_notes.get("coverage"), COVERAGE_NOTE)


if __name__ == "__main__":
    unittest.main()
