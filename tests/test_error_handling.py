import unittest
from unittest import mock

from src.agent import PocketNetworkAgent
from src.graphql_client import is_coverage_error, is_fixable_query_error
from src.query_sub_agents import SettlementRewardsAgent

# Messages returned by https://data.pocket.network/ on 2026-10-05, as PocketNetworkAPIClient formats them.
COVERAGE = (
    "GraphQL errors: the range starts before the first written settlement (height 703893, "
    "2026-04-07 13:28:34.328+00): earlier settlement heights are not written"
)
GAP = "GraphQL errors: the range overlaps settlement heights 10 to 20, which are not written (settlement_gaps)"
BUCKET_CAP = (
    "GraphQL errors: bucket=hour allows ranges up to 7 days; "
    "use day (up to 92 days), week, month or year for longer ones"
)
TOO_MANY_IDS = "GraphQL errors: addresses must have between 1 and 200 elements (has 201)"
SCHEMA = 'GraphQL errors: Cannot query field "getIncome" on type "Query". Did you mean "getIncomeJson"?'
TIMEOUT = "API request timeout (30s)"


class TestErrorClassification(unittest.TestCase):
    def test_coverage_errors(self):
        for error in (COVERAGE, GAP):
            with self.subTest(error=error):
                self.assertTrue(is_coverage_error(error))
                self.assertFalse(is_fixable_query_error(error))

    def test_fixable_errors(self):
        for error in (BUCKET_CAP, TOO_MANY_IDS, SCHEMA):
            with self.subTest(error=error):
                self.assertTrue(is_fixable_query_error(error))
                self.assertFalse(is_coverage_error(error))

    def test_other_errors_are_neither(self):
        for error in (TIMEOUT, "HTTP error: 502 - Bad Gateway", None, ""):
            with self.subTest(error=error):
                self.assertFalse(is_coverage_error(error))
                self.assertFalse(is_fixable_query_error(error))


class TestSubAgentRetries(unittest.TestCase):
    def setUp(self):
        self.agent = SettlementRewardsAgent(mock.MagicMock())
        self.agent.graphql_client = mock.MagicMock()

    def _after_execution(self, error):
        self.agent.graphql_client.execute_query.return_value = (False, None, error)
        state = {"success": True, "explanation": "", "attempt_count": 1, "endpoint_type": "graphql", "query": "{ x }"}
        state.update(self.agent._node_execute_query(state))
        return self.agent._should_retry(state), state

    def test_fixable_errors_are_retried_with_the_error_as_hint(self):
        for error in (BUCKET_CAP, SCHEMA):
            with self.subTest(error=error):
                decision, state = self._after_execution(error)
                self.assertEqual(decision, "retry")
                self.assertIn(error, self.agent._build_user_message({**state, "user_query": "q"}, attempt=2))

    def test_coverage_and_transport_errors_end_the_run(self):
        for error in (COVERAGE, TIMEOUT):
            with self.subTest(error=error):
                self.assertEqual(self._after_execution(error)[0], "done")

    def test_retries_stop_at_the_limit(self):
        self.agent.graphql_client.execute_query.return_value = (False, None, BUCKET_CAP)
        state = {"success": True, "explanation": "", "attempt_count": 5, "endpoint_type": "graphql", "query": "{ x }"}
        state.update(self.agent._node_execute_query(state))
        self.assertEqual(self.agent._should_retry(state), "done")


class TestMainAgentCoverage(unittest.TestCase):
    def test_coverage_error_is_reported_as_not_covered(self):
        agent = PocketNetworkAgent.__new__(PocketNetworkAgent)
        agent.llm = mock.MagicMock()
        agent.sub_agents = []
        notes = agent._format_refusal({"user_query": "income in July 2025", "error": COVERAGE})["agent_notes"]
        self.assertTrue(notes.startswith("Not covered yet"))
        self.assertIn("not 0", notes)
        agent.llm.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
