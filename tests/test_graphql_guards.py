import time
import unittest
from unittest import mock

from graphql import GraphQLError, parse

from src.graphql_client import GRAPHQL_REGISTRY
from src.graphql_validator import (
    CATALOG_FUNCTIONS,
    LIVE_REWARD_FIELDS,
    MAX_FIRST,
    MAX_VISITED_FIELDS,
    check_query_guards,
    validate_graphql_query,
)
from src.tools_data import MAX_RESULT_CHARS, execute_graphql

# Mirror of pocketdex CATALOG_FUNCTIONS (src/mappings/dbFunctions/settlement/functions.ts, branch
# feat/money-range-in-response at 2e88668). Update both when pocketdex adds or drops a catalog function.
POCKETDEX_CATALOG_FUNCTIONS = [
    "money_coverage",
    "get_application_spend",
    "get_gateway_spend",
    "get_supplier_earnings",
    "get_supplier_distribution",
    "get_income",
    "get_validator_rewards",
    "get_delegator_income",
    "get_supply_flows",
    "get_supplier_penalties",
    "get_service_usage",
    "get_app_auto_unstakes",
    "get_supplier_proofs",
    "get_param_history",
]


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

    def test_live_reward_functions_are_refused(self):
        args = '(addresses: ["pokt1x"], startDate: "2026-10-01T00:00:00Z", endDate: "2026-10-02T00:00:00Z")'
        for name in LIVE_REWARD_FIELDS:
            legacy = "legacy" + name[len("get") :]
            for query in (
                f"{{ {name}{args} }}",
                f"{{ x: {name}{args} }}",
                f"{{ ...F }} fragment F on Query {{ {name}{args} }}",
                f"{{ ... on Query {{ y: {name}{args} }} }}",
                f'{{ block(id: "1") {{ id }} ...F }} fragment F on Query {{ ...G }} '
                f"fragment G on Query {{ {name}{args} }}",
            ):
                with self.subTest(query=query):
                    error = guard(query)
                    self.assertIn(f'"{name}" scans the raw payout tables', error)
                    self.assertIn(f"use {legacy} (same arguments and JSON", error)

    def test_catalog_functions_mirror_pocketdex(self):
        camel = {n.split("_")[0] + "".join(w.title() for w in n.split("_")[1:]) for n in POCKETDEX_CATALOG_FUNCTIONS}
        self.assertEqual(CATALOG_FUNCTIONS, camel)

    def test_catalog_row_variants_are_refused(self):
        for query in (
            '{ getIncomeList(addresses: ["pokt1x"], rangeStart: "2026-10-01T00:00:00Z") { amountUpokt } }',
            '{ x: moneyCoverageList(rangeStart: "2026-10-01T00:00:00Z") { settlements } }',
            '{ ...F } fragment F on Query { getSupplyFlowsList(rangeStart: "2026-10-01T00:00:00Z") { flow } }',
            '{ ... on Query { getParamHistoryList(rangeStart: "2026-10-01T00:00:00Z") { key } } }',
        ):
            with self.subTest(query=query):
                self.assertRegex(guard(query), r"Use (getIncome|moneyCoverage|getSupplyFlows|getParamHistory)Json,")

    def test_legacy_twins_and_other_reward_functions_pass(self):
        for name in [n.replace("get", "legacy", 1) for n in LIVE_REWARD_FIELDS] + [
            "getRewardsByDate",
            "getRewardsByDomainsAndTimeGroupByService",
        ]:
            with self.subTest(name=name):
                self.assertIsNone(guard(f'{{ {name}(startDate: "2026-10-01T00:00:00Z") }}'))

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

    def test_nested_fragment_fan_out_is_cut_short(self):
        # Each fragment spreads the next one in two nested fields: 2^30 field visits without a limit.
        n = 30
        query = "{ ...F0 } " + " ".join(
            f'fragment F{i} on Query {{ a: block(id: "1") {{ ...F{i + 1} }} b: block(id: "2") {{ ...F{i + 1} }} }}'
            for i in range(n)
        ).replace(f"...F{n}", "id")
        start = time.monotonic()
        with self.assertRaisesRegex(GraphQLError, f"more than {MAX_VISITED_FIELDS} fields"):
            guard(query)
        self.assertLess(time.monotonic() - start, 1)

    def test_long_fragment_chain_is_refused(self):
        n = 2000
        query = "{ ...F0 } " + " ".join(f"fragment F{i} on Query {{ ...F{i + 1} }}" for i in range(n))
        query += f' fragment F{n} on Query {{ block(id: "1") {{ id }} }}'
        with self.assertRaisesRegex(GraphQLError, "too deeply nested"):
            guard(query)

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
    def test_live_reward_function_is_not_sent(self, client):
        query = '{ total: getRewardsByAddressesAndTime(addresses: ["pokt1x"]) }'
        success, result, error = execute_graphql.func(query=query)
        self.assertEqual((success, result), (False, None))
        self.assertIn("use legacyRewardsByAddressesAndTime", error)
        client.return_value.execute_query.assert_not_called()

    def test_sub_agent_validation_refuses_live_reward_functions(self):
        query = '{ getMintBreakdownBetweenDates(startDate: "2026-10-01T00:00:00Z", endDate: "2026-10-02T00:00:00Z") }'
        ok, _, error = validate_graphql_query(query, "legacyMintBreakdownBetweenDates")
        self.assertFalse(ok)
        self.assertIn("use legacyMintBreakdownBetweenDates", error)

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
    def test_deeply_nested_query_is_refused(self, client):
        query = "{" + "a{" * 600 + "b" + "}" * 600 + "}"
        self.assertEqual(execute_graphql.func(query=query), (False, None, "Query too deeply nested"))
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
