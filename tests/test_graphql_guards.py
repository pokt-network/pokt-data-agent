import unittest
from unittest import mock

from graphql import GraphQLError, parse

from src.graphql_client import GRAPHQL_REGISTRY
from src.graphql_validator import MAX_FIRST, check_query_guards, validate_graphql_query
from src.tools_data import MAX_RESULT_CHARS, execute_graphql


def guard(query):
    return check_query_guards(parse(query))


class TestQueryGuards(unittest.TestCase):
    def test_raw_payout_tables_are_refused(self):
        for query in (
            "{ modToAcctTransfers(first: 10) { nodes { amount } } }",
            "{ modToAcctTransfersSummarizeds { aggregates { sum { amount } } } }",
            '{ account(id: "pokt1x") { modToAcctTransfersByRecipientId(first: 5) { nodes { amount } } } }',
            "{ x: domainServiceDailyRewards { totalCount } }",
        ):
            with self.subTest(query=query):
                self.assertIn("raw payout table", guard(query))

    def test_connections_need_a_bounded_first(self):
        for query in (
            "{ suppliers { nodes { id } } }",
            "{ suppliers(first: 1001) { nodes { id } } }",
            "{ suppliers(first: 0) { edges { node { id } } } }",
            "query ($n: Int) { suppliers(first: $n) { nodes { id } } }",
            # nested connection, and one reached through a fragment
            "{ suppliers(first: 5) { nodes { serviceConfigs { nodes { serviceId } } } } }",
            "{ suppliers(first: 5) { ...F } } "
            "fragment F on SuppliersConnection { nodes { serviceConfigs { nodes { id } } } }",
        ):
            with self.subTest(query=query):
                self.assertIn('needs a literal "first"', guard(query))

    def test_bounded_and_aggregate_queries_pass(self):
        for query in (
            f"{{ suppliers(first: {MAX_FIRST}) {{ nodes {{ id serviceConfigs(first: 5) "
            "{ nodes { serviceId } } } } }",
            "{ suppliers(filter: {stakeStatus: {equalTo: Staked}}) { totalCount aggregates { sum { stakeAmount } } } }",
            '{ getIncomeJson(addresses: ["pokt1x"], rangeStart: "2026-10-01T00:00:00Z", '
            'rangeEnd: "2026-10-02T00:00:00Z") }',
            '{ block(id: "1") { id timestamp } }',
        ):
            with self.subTest(query=query):
                self.assertIsNone(guard(query))

    def test_self_spreading_fragment_is_refused(self):
        for query in (
            "{ ...F } fragment F on Query { ...F }",
            "{ ...F } fragment F on Query { ...G } fragment G on Query { ...F }",
            # through a nested field (the code-review case)
            '{ supplier(id:"x") { ...F } } fragment F on Supplier { id serviceConfigs(first: 1) '
            "{ nodes { supplier { ...F } } } }",
        ):
            with self.subTest(query=query):
                with self.assertRaises(GraphQLError):
                    guard(query)
        ok, _, error = validate_graphql_query("{ ...F } fragment F on Query { ...F }", "suppliers")
        self.assertFalse(ok)
        self.assertIn("within itself", error)

    def test_a_fragment_spread_twice_is_not_a_cycle(self):
        self.assertIsNone(guard('{ ...F ...F } fragment F on Query { block(id: "1") { id } }'))

    def test_every_registry_example_passes(self):
        for name, info in GRAPHQL_REGISTRY.items():
            for example in info.examples:
                with self.subTest(method=name, example=example.splitlines()[0]):
                    self.assertIsNone(guard(example))

    def test_sub_agent_validation_applies_the_guards(self):
        ok, _, error = validate_graphql_query("{ suppliers { nodes { id } } }", "suppliers")
        self.assertFalse(ok)
        self.assertIn('needs a literal "first"', error)


class TestExecuteGraphqlTool(unittest.TestCase):
    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_refused_query_is_not_sent(self, client):
        success, result, error = execute_graphql.func(query="{ modToAcctTransfers(first: 5) { nodes { id } } }")
        self.assertEqual((success, result), (False, None))
        self.assertIn("raw payout table", error)
        client.return_value.execute_query.assert_not_called()

    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_self_spreading_fragment_is_not_sent(self, client):
        success, result, error = execute_graphql.func(query="{ ...F } fragment F on Query { ...F }")
        self.assertEqual((success, result), (False, None))
        self.assertIn("within itself", error)
        client.return_value.execute_query.assert_not_called()

    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_fragment_reaching_itself_through_a_field_is_not_sent(self, client):
        query = (
            '{ supplier(id:"x") { ...F } } fragment F on Supplier { id serviceConfigs(first: 1) '
            "{ nodes { supplier { ...F } } } }"
        )
        success, result, error = execute_graphql.func(query=query)
        self.assertEqual((success, result), (False, None))
        self.assertIn("within itself", error)
        client.return_value.execute_query.assert_not_called()

    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_large_result_is_truncated_with_a_notice(self, client):
        client.return_value.execute_query.return_value = (True, {"x": "a" * (MAX_RESULT_CHARS + 10)}, None)
        success, result, error = execute_graphql.func(query="{ x: getDaoBalanceAtHeight }")
        self.assertTrue(success)
        self.assertEqual(len(result), MAX_RESULT_CHARS)
        self.assertIn("truncated", error)

    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_small_result_is_returned_as_is(self, client):
        client.return_value.execute_query.return_value = (True, {"x": "1"}, None)
        self.assertEqual(execute_graphql.func(query="{ x: getDaoBalanceAtHeight }"), (True, {"x": "1"}, None))


if __name__ == "__main__":
    unittest.main()
