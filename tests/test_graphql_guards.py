import threading
import time
import unittest
from unittest import mock

from graphql import GraphQLError, build_schema, parse

from src import graphql_validator
from src.graphql_client import GRAPHQL_REGISTRY
from src.graphql_validator import (
    CATALOG_FUNCTIONS,
    HIDDEN_FIELDS,
    LIVE_REWARD_FIELDS,
    MAX_FIRST,
    MAX_VISITED_FIELDS,
    ROW_FUNCTIONS,
    check_query_guards,
    validate_graphql_query,
)
from src.tools_data import MAX_RESULT_CHARS, execute_graphql

# Mirror of pocketdex CATALOG_FUNCTIONS (src/mappings/dbFunctions/settlement/functions.ts, pocketdex #112 and #113,
# live on mainnet 2026-10-14). Update both when pocketdex adds or drops a catalog function.
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
    "get_claims",
    "get_actor_lifecycle",
    "get_block_stats",
    "get_relay_mining_difficulty",
    "get_created_claims",
    "get_morse_claims",
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
            legacy = "legacy" + name[len("get") :].removesuffix("V2")
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
        self.assertEqual(CATALOG_FUNCTIONS | ROW_FUNCTIONS, camel)
        self.assertFalse(CATALOG_FUNCTIONS & ROW_FUNCTIONS)

    def test_row_function_list_variants_are_refused(self):
        for name in sorted(ROW_FUNCTIONS):
            with self.subTest(name=name):
                error = guard(f"{{ {name}List(rowLimit: 10) {{ height }} }}")
                self.assertIn("no rows cannot say whether the range is not covered", error)
                self.assertIn(f"Use {name}Json: the same rows", error)

    def test_catalog_row_variants_are_refused(self):
        for query in (
            '{ getIncomeList(addresses: ["pokt1x"], rangeStart: "2026-10-01T00:00:00Z") { amountUpokt } }',
            '{ x: moneyCoverageList(rangeStart: "2026-10-01T00:00:00Z") { settlements } }',
            '{ ...F } fragment F on Query { getSupplyFlowsList(rangeStart: "2026-10-01T00:00:00Z") { flow } }',
            '{ ... on Query { getParamHistoryList(rangeStart: "2026-10-01T00:00:00Z") { key } } }',
        ):
            with self.subTest(query=query):
                self.assertRegex(guard(query), r"Use (getIncome|moneyCoverage|getSupplyFlows|getParamHistory)Json,")

    def test_the_orphan_mint_v2_points_to_the_legacy_mint(self):
        error = guard('{ getMintBreakdownBetweenDatesV2(startDate: "2026-10-01T00:00:00Z") }')
        self.assertIn("use legacyMintBreakdownBetweenDates (same arguments", error)

    def test_legacy_twins_and_other_reward_functions_pass(self):
        for name in [n.replace("get", "legacy", 1).removesuffix("V2") for n in LIVE_REWARD_FIELDS] + [
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
            # a connection reached through a fragment
            "{ ...F } fragment F on Query { suppliers { nodes { id } } }",
        ):
            with self.subTest(query=query):
                self.assertIn('needs a literal "first"', guard(query))

    def test_bounded_and_count_queries_pass(self):
        for query in (
            f"{{ suppliers(first: {MAX_FIRST}) {{ nodes {{ id ownerId }} }} }}",
            "{ suppliers(filter: {stakeStatus: {equalTo: Staked}}) { totalCount } }",
            '{ getIncomeJson(addresses: ["pokt1x"], rangeStart: "2026-10-01T00:00:00Z", '
            'rangeEnd: "2026-10-02T00:00:00Z") }',
            '{ block(id: "1") { id timestamp } }',
            # rows through edges, with the page info and the count of the list
            "{ suppliers(first: 5) { totalCount pageInfo { hasNextPage endCursor } edges { cursor node { id } } } }",
            # filters on the entity's own columns, a JSON column and the logic operators
            '{ transactions(first: 5, filter: {or: [{code: {equalTo: 0}}, {signerAddress: {in: ["pokt1x"]}}], '
            'amountOfMessages: {contains: [{type: "/cosmos.bank.v1beta1.MsgSend"}]}}) { nodes { id } } }',
            '{ getSupplierFleetList(stakeStatuses: ["Staked"], bySupplier: false) { stakeUpokt supplierCount } }',
            # a variable as an operator's value is a plain value
            "query ($a: String) { balances(first: 5, filter: {accountId: {equalTo: $a}}) { nodes { id } } }",
            "{ _metadata { lastProcessedHeight indexerHealthy } }",
            '{ __type(name: "Supplier") { fields { name type { name ofType { name } } } } }',
        ):
            with self.subTest(query=query):
                self.assertIsNone(guard(query))

    def test_lists_are_paged_by_keyset_not_offset(self):
        for query in (
            "{ suppliers(first: 100, offset: 100) { nodes { id } } }",
            "query ($o: Int) { transactions(first: 20, offset: $o) { edges { node { id } } } }",
        ):
            with self.subTest(query=query):
                self.assertIn("pages by offset", guard(query))
        for query in (
            "{ suppliers(first: 100, offset: 0) { nodes { id } } }",
            '{ suppliers(first: 100, orderBy: ID_ASC, blockHeight: "964638", after: "abc") '
            "{ totalCount pageInfo { hasNextPage endCursor } nodes { id } } }",
            # a function's rows are not a live list's connection
            '{ getSupplierFleetList(owners: ["pokt1x"], first: 1000, offset: 1000) { supplierId } }',
        ):
            with self.subTest(query=query):
                self.assertIsNone(guard(query))

    def test_aggregates_are_refused(self):
        for query in (
            "{ suppliers(filter: {stakeStatus: {equalTo: Staked}}) { totalCount aggregates { sum { stakeAmount } } } }",
            "{ morseClaimableAccounts { groupedAggregates(groupBy: CLAIMED) { keys } } }",
        ):
            with self.subTest(query=query):
                self.assertIn("is not served: use totalCount for a count, a function that sums", guard(query))

    def test_relations_are_refused(self):
        for query in (
            "{ balances(first: 5) { nodes { accountId lastUpdatedBlock { id timestamp } } } }",
            f"{{ suppliers(first: {MAX_FIRST}) {{ nodes {{ id serviceConfigs(first: 5) "
            "{ nodes { serviceId } } } } }",
            '{ account(id: "pokt1x") { id balances(first: 10) { nodes { amount } } } }',
            "{ applicationGateways(first: 5) { edges { node { gateway { id } } } } }",
            "{ suppliers(first: 5) { ...F } } fragment F on SuppliersConnection { nodes { owner { id } } }",
            '{ supplier(id: "pokt1x") { ... on Supplier { serviceConfigs(first: 1) { totalCount } } } }',
        ):
            with self.subTest(query=query):
                self.assertIn("is a relation to another entity, which is not served", guard(query))

    def test_filters_through_a_relation_are_refused(self):
        for query, message in (
            (
                '{ balances(filter: {lastUpdatedBlock: {timestamp: {greaterThan: "2026-06-01T00:00:00Z"}}}) '
                "{ totalCount } }",
                '"lastUpdatedBlock" holds "timestamp", which is not an operator',
            ),
            (
                '{ suppliers(filter: {serviceConfigs: {some: {serviceId: {equalTo: "eth"}}}}) { totalCount } }',
                '"serviceConfigs" holds "some"',
            ),
            ("{ transactions(filter: {nativeTransfersExist: true}) { totalCount } }", '"nativeTransfersExist" tests'),
            (
                '{ transactions(filter: {and: [{code: {equalTo: 0}}, {block: {timestamp: {greaterThan: "x"}}}]}) '
                "{ totalCount } }",
                '"block" holds "timestamp"',
            ),
            # a misspelled operator reads the same, and the message says which operators exist
            ('{ blocks(filter: {id: {gte: "1"}}) { totalCount } }', '"id" holds "gte", which is not an operator'),
            # a variable could hold any of the above
            ("query ($f: BalanceFilter) { balances(filter: $f) { totalCount } }", '"$f" is a variable'),
            ("query ($b: BlockFilter) { balances(filter: {lastUpdatedBlock: $b}) { totalCount } }", '"$b"'),
            ("query ($f: BalanceFilter) { balances(filter: {or: [$f]}) { totalCount } }", '"$f" is a variable'),
        ):
            with self.subTest(query=query):
                error = guard(query)
                self.assertIn("is refused", error)
                self.assertIn(message, error)

    def test_node_lookups_are_refused(self):
        for query in (
            '{ node(nodeId: "x") { id } }',
            "{ suppliers(first: 5) { nodes { nodeId id } } }",
            '{ supplierByNodeId(nodeId: "x") { id } }',
        ):
            with self.subTest(query=query):
                self.assertIn("is not served: look an entity up by its key", guard(query))

    def test_hidden_entities_point_to_their_replacement(self):
        for name, target in HIDDEN_FIELDS.items():
            with self.subTest(name=name):
                self.assertEqual(
                    guard(f"{{ {name}(first: 5) {{ nodes {{ id }} }} }}"),
                    f'Query refused: "{name}" is leaving the API: use {target}.',
                )
        self.assertIn("event and message tables are hidden", guard("{ msgUnstakeSuppliers(first: 5) { totalCount } }"))
        # an entity's own column of the same shape is not a hidden table
        self.assertIsNone(guard("{ authzs(first: 5) { nodes { granterId msg } } }"))

    def test_self_spreading_fragment_is_refused(self):
        for query in (
            "{ ...F } fragment F on Query { ...F }",
            "{ ...F } fragment F on Query { ...G } fragment G on Query { ...F }",
            # through a nested field (the code-review case; relations are refused, introspection still nests)
            '{ __type(name: "Supplier") { ...F } } fragment F on __Type { name ofType { ...F } }',
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
        query = '{ __type(name: "Supplier") { ...F0 } } ' + " ".join(
            f"fragment F{i} on __Type {{ a: ofType {{ ...F{i + 1} }} b: ofType {{ ...F{i + 1} }} }}" for i in range(n)
        ).replace(f"...F{n}", "name")
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
        query = '{ __type(name: "Supplier") { ...F } } fragment F on __Type { name ofType { ...F } }'
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


# A small stand-in for the API schema: one list, one function, and one function that is about to leave the API.
SCHEMA = build_schema(
    """
    scalar JSON
    type Query {
      suppliers(first: Int, filter: SupplierFilter): SuppliersConnection
      getSupplierFleetJson(owners: [String]): JSON
      getOldStats(owners: [String]): JSON @deprecated(reason: "Use getSupplierFleetJson.")
      eventClaimSettleds(first: Int): SuppliersConnection @deprecated(reason: "Use getClaimsList, live reason.")
    }
    input SupplierFilter { id: StringFilter and: [SupplierFilter] }
    input StringFilter { equalTo: String newOperator: String }
    type SuppliersConnection { totalCount: Int nodes: [Supplier] }
    type Supplier { id: String ownerId: String }
    """
)


REAL_THREAD = threading.Thread


class SyncThread:
    """A thread that runs its target on start(), so that a test sees the refresh it starts."""

    def __init__(self, target, **_):
        self.target = target

    def start(self):
        self.target()


class TestSchemaCheck(unittest.TestCase):
    def setUp(self):
        self.reset()
        self.addCleanup(self.reset)
        patcher = mock.patch("src.graphql_validator._fetch_schema", return_value=SCHEMA)
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)
        thread = mock.patch("src.graphql_validator.threading.Thread", SyncThread)
        thread.start()
        self.addCleanup(thread.stop)

    @staticmethod
    def reset():
        graphql_validator._live, graphql_validator._next_refresh_at, graphql_validator._refreshing = None, 0.0, False

    def test_deprecated_and_unknown_fields_are_refused(self):
        for query, message in (
            ('{ getOldStats(owners: ["pokt1x"]) }', "Query.getOldStats is deprecated. Use getSupplierFleetJson."),
            (
                '{ suppliers(first: 5, filter: {ownerId: {equalTo: "b"}}) { nodes { id } } }',
                "Field 'ownerId' is not defined",
            ),
            ("{ suppliers(first: 5) { nodes { stake } } }", "Cannot query field 'stake'"),
        ):
            with self.subTest(query=query):
                ok, _, error = validate_graphql_query(query, "suppliers")
                self.assertFalse(ok)
                self.assertIn("Query refused by the API schema", error)
                self.assertIn(message, error)

    def test_a_valid_query_passes(self):
        ok, normalized, error = validate_graphql_query(
            '{ suppliers(first: 5, filter: {id: {equalTo: "pokt1x"}}) { totalCount nodes { id ownerId } } }',
            "suppliers",
        )
        self.assertEqual((ok, error), (True, None))
        self.assertIn("suppliers", normalized)

    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_deprecated_field_is_not_sent(self, client):
        success, result, error = execute_graphql.func(query='{ getOldStats(owners: ["pokt1x"]) }')
        self.assertEqual((success, result), (False, None))
        self.assertIn("Use getSupplierFleetJson.", error)
        client.return_value.execute_query.assert_not_called()

    def test_the_guards_use_the_live_schema(self):
        graphql_validator.refresh_schema()
        # the hidden list's replacement as the live deprecation reason says it
        self.assertEqual(
            guard("{ eventClaimSettleds(first: 5) { nodes { id } } }"),
            'Query refused: "eventClaimSettleds" is leaving the API: Use getClaimsList, live reason. '
            "Use getClaimsJson, not getClaimsList.",
        )
        # an operator the live schema has and the fallback list does not
        query = '{ suppliers(first: 5, filter: {id: {newOperator: "a"}}) { nodes { id } } }'
        self.assertIsNone(guard(query))
        self.reset()
        self.assertIn('"id" holds "newOperator", which is not an operator', guard(query))

    def test_schema_is_introspected_once_per_ttl(self):
        for _ in range(3):
            validate_graphql_query("{ suppliers(first: 5) { totalCount } }", "suppliers")
        self.assertEqual(self.fetch.call_count, 1)
        with mock.patch("src.graphql_validator.time.monotonic", return_value=time.monotonic() + 3601):
            validate_graphql_query("{ suppliers(first: 5) { totalCount } }", "suppliers")
        self.assertEqual(self.fetch.call_count, 2)

    def test_failed_introspection_leaves_the_guards_and_is_retried(self):
        self.fetch.side_effect = OSError("introspection is off")
        ok, _, error = validate_graphql_query('{ getOldStats(owners: ["pokt1x"]) }', "suppliers")
        self.assertEqual((ok, error), (True, None))
        validate_graphql_query("{ suppliers(first: 5) { totalCount } }", "suppliers")
        self.assertEqual(self.fetch.call_count, 1)
        self.fetch.side_effect, self.fetch.return_value = None, SCHEMA
        with mock.patch("src.graphql_validator.time.monotonic", return_value=time.monotonic() + 61):
            ok, _, error = validate_graphql_query('{ getOldStats(owners: ["pokt1x"]) }', "suppliers")
        self.assertFalse(ok)
        self.assertIn("deprecated", error)

    def test_a_failed_refresh_keeps_the_schema_it_had(self):
        validate_graphql_query("{ suppliers(first: 5) { totalCount } }", "suppliers")
        self.fetch.side_effect = OSError("introspection is off")
        with mock.patch("src.graphql_validator.time.monotonic", return_value=time.monotonic() + 3601):
            ok, _, error = validate_graphql_query('{ getOldStats(owners: ["pokt1x"]) }', "suppliers")
        self.assertFalse(ok)
        self.assertIn("deprecated", error)

    def test_a_slow_introspection_never_keeps_a_query_waiting(self):
        release, threads = threading.Event(), []

        def slow_fetch():
            release.wait(5)
            return SCHEMA

        def real_thread(**kwargs):
            threads.append(REAL_THREAD(**kwargs))
            return threads[-1]

        self.fetch.side_effect = slow_fetch
        query = '{ getOldStats(owners: ["pokt1x"]) }'
        with mock.patch("src.graphql_validator.threading.Thread", real_thread):
            start = time.monotonic()
            first = validate_graphql_query(query, "suppliers")
            second = validate_graphql_query(query, "suppliers")
            elapsed = time.monotonic() - start
            # one introspection at a time, and both queries answered by the guards meanwhile
            self.assertEqual(len(threads), 1)
            self.assertLess(elapsed, 1)
            self.assertEqual((first[0], second[0]), (True, True))
            release.set()
            threads[0].join(5)
        ok, _, error = validate_graphql_query(query, "suppliers")
        self.assertFalse(ok)
        self.assertIn("deprecated", error)


if __name__ == "__main__":
    unittest.main()
