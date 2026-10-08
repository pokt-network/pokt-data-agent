import json
import re
import unittest
from unittest import mock

from src.agent import PocketNetworkAgent
from src.graphql_client import final_error_reply, is_coverage_error, is_fixable_query_error
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
TOP_TOO_HIGH = "GraphQL errors: top_by_settled must be between 1 and 200 (is 1000)"
EMPTY_LIST = "GraphQL errors: services must have between 1 and 200 elements (has 0)"
TOP_ZERO = "GraphQL errors: top_by_settled must be between 1 and 200 (is 0)"
REBUILD = (
    "GraphQL errors: settlement heights 10 to 20 were written with rollup version 1 (current 2): "
    "run rebuild_rollups first"
)
SCHEMA = 'GraphQL errors: Cannot query field "getIncome" on type "Query". Did you mean "getIncomeJson"?'
TIMEOUT = "API request timeout (30s)"


class TestErrorClassification(unittest.TestCase):
    def test_coverage_errors(self):
        for error in (COVERAGE, GAP):
            with self.subTest(error=error):
                self.assertTrue(is_coverage_error(error))
                self.assertFalse(is_fixable_query_error(error))

    def test_fixable_errors(self):
        # An empty list or a top below 1: the corrected query adds what is missing, it drops nothing.
        for error in (BUCKET_CAP, SCHEMA, EMPTY_LIST, TOP_ZERO):
            with self.subTest(error=error):
                self.assertTrue(is_fixable_query_error(error))
                self.assertFalse(is_coverage_error(error))

    def test_list_limits_are_not_fixable(self):
        # A model "fixes" them by dropping ids or lowering N, then reports a partial total as the full one.
        for error in (TOO_MANY_IDS, TOP_TOO_HIGH):
            with self.subTest(error=error):
                self.assertFalse(is_fixable_query_error(error))

    def test_final_replies(self):
        self.assertIn("not indexed yet", final_error_reply(COVERAGE))
        self.assertIn("Try a more recent range", final_error_reply(COVERAGE))
        self.assertIn("(a gap)", final_error_reply(GAP))
        self.assertIn("being rebuilt", final_error_reply(REBUILD))
        self.assertIn("Too many addresses: 201, the limit is 200 per question", final_error_reply(TOO_MANY_IDS))
        self.assertIn("top 200 services", final_error_reply(TOP_TOO_HIGH))
        self.assertEqual(final_error_reply(EMPTY_LIST), "The question needs at least one of the services to look up.")
        self.assertIn("at least one", final_error_reply(TOP_ZERO))
        for error in (BUCKET_CAP, SCHEMA, TIMEOUT, None):
            with self.subTest(error=error):
                self.assertIsNone(final_error_reply(error))

    def test_coverage_replies_never_say_zero(self):
        for error in (COVERAGE, GAP, REBUILD):
            with self.subTest(error=error):
                self.assertIsNone(re.search(r"\b0\b|zero", final_error_reply(error).split("Details:")[0]))

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

    def test_empty_list_and_top_zero_are_retried(self):
        for error in (EMPTY_LIST, TOP_ZERO):
            with self.subTest(error=error):
                self.assertEqual(self._after_execution(error)[0], "retry")

    def test_coverage_limit_and_transport_errors_end_the_run(self):
        for error in (COVERAGE, TOO_MANY_IDS, TOP_TOO_HIGH, TIMEOUT):
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
        self.assertEqual(notes, final_error_reply(COVERAGE))
        agent.llm.invoke.assert_not_called()

    def test_empty_list_reaches_the_user_after_the_retries(self):
        # "What did I earn last week?" with no address: every rebuilt query still has an empty list.
        llm = mock.MagicMock()
        llm.bind_tools.return_value.invoke.return_value = mock.MagicMock(
            tool_calls=[],
            content='{"endpoint_type": "graphql", "endpoint_method": "getIncomeJson", "query": '
            '"{ getIncomeJson(addresses: [], rangeStart: \\"2026-09-28T00:00:00Z\\", '
            'rangeEnd: \\"2026-10-05T00:00:00Z\\") }"}',
        )
        sub_agent = SettlementRewardsAgent(llm)
        sub_agent.graphql_client = mock.MagicMock()
        empty = "GraphQL errors: addresses must have between 1 and 200 elements (has 0)"
        sub_agent.graphql_client.execute_query.return_value = (False, None, empty)

        agent = PocketNetworkAgent.__new__(PocketNetworkAgent)
        agent.llm = mock.MagicMock()
        agent.sub_agents = [sub_agent]
        state = {"user_query": "what did I earn last week?", "selected_subagent": sub_agent, "agent_notes": ""}
        state.update(agent._build_query(state))
        self.assertEqual(sub_agent.graphql_client.execute_query.call_count, 5)
        self.assertEqual(
            agent._format_refusal(state)["agent_notes"], "The question needs at least one of the addresses to look up."
        )
        agent.llm.invoke.assert_not_called()

    def test_empty_list_reply_survives_a_last_attempt_refused_by_the_guards(self):
        # Four attempts send an empty list; the fifth is refused before execution (connection without "first").
        def envelope(method, query):
            return mock.MagicMock(
                tool_calls=[],
                content=json.dumps({"endpoint_type": "graphql", "endpoint_method": method, "query": query}),
            )

        empty_query = '{ getIncomeJson(addresses: [], rangeStart: "2026-09-28T00:00:00Z") }'
        llm = mock.MagicMock()
        llm.bind_tools.return_value.invoke.side_effect = [envelope("getIncomeJson", empty_query)] * 4 + [
            envelope("suppliers", "{ suppliers { nodes { id } } }")
        ]
        sub_agent = SettlementRewardsAgent(llm)
        sub_agent.graphql_client = mock.MagicMock()
        empty = "GraphQL errors: addresses must have between 1 and 200 elements (has 0)"
        sub_agent.graphql_client.execute_query.return_value = (False, None, empty)

        agent = PocketNetworkAgent.__new__(PocketNetworkAgent)
        agent.llm = mock.MagicMock()
        agent.sub_agents = [sub_agent]
        state = {"user_query": "what did I earn last week?", "selected_subagent": sub_agent, "agent_notes": ""}
        state.update(agent._build_query(state))
        self.assertEqual(sub_agent.graphql_client.execute_query.call_count, 4)
        self.assertEqual(
            agent._format_refusal(state)["agent_notes"], "The question needs at least one of the addresses to look up."
        )

    def test_the_most_recent_execution_error_wins(self):
        # Attempt 1 sends an empty list; attempt 2 adds the address and asks for a range not covered yet.
        def envelope(query):
            return mock.MagicMock(
                tool_calls=[],
                content=json.dumps({"endpoint_type": "graphql", "endpoint_method": "getIncomeJson", "query": query}),
            )

        llm = mock.MagicMock()
        llm.bind_tools.return_value.invoke.side_effect = [
            envelope('{ getIncomeJson(addresses: [], rangeStart: "2025-07-01T00:00:00Z") }'),
            envelope('{ getIncomeJson(addresses: ["pokt1x"], rangeStart: "2025-07-01T00:00:00Z") }'),
        ]
        sub_agent = SettlementRewardsAgent(llm)
        sub_agent.graphql_client = mock.MagicMock()
        sub_agent.graphql_client.execute_query.side_effect = [
            (False, None, "GraphQL errors: addresses must have between 1 and 200 elements (has 0)"),
            (False, None, COVERAGE),
        ]

        agent = PocketNetworkAgent.__new__(PocketNetworkAgent)
        agent.llm = mock.MagicMock()
        agent.sub_agents = [sub_agent]
        state = {"user_query": "what did pokt1x earn in July 2025?", "selected_subagent": sub_agent, "agent_notes": ""}
        state.update(agent._build_query(state))
        self.assertEqual(sub_agent.graphql_client.execute_query.call_count, 2)
        self.assertEqual(agent._format_refusal(state)["agent_notes"], final_error_reply(COVERAGE))

    def test_list_limit_is_reported_without_a_retry(self):
        agent = PocketNetworkAgent.__new__(PocketNetworkAgent)
        agent.llm = mock.MagicMock()
        agent.sub_agents = []
        notes = agent._format_refusal({"user_query": "income of 201 addresses", "error": TOO_MANY_IDS})["agent_notes"]
        self.assertIn("split the question".lower(), notes.lower())
        agent.llm.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
