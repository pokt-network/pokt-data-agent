import json
import unittest
from unittest import mock

import requests

from src.graphql_client import DEFAULT_ROW_LIMIT, MAX_LIST_ROWS, MAX_RAW_ERROR_CHARS, PocketNetworkAPIClient, page_notes


def _response(status, text):
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()
    return response


class TestClientErrors(unittest.TestCase):
    @mock.patch("src.graphql_client.requests.post")
    def test_http_400_with_graphql_errors_keeps_only_the_messages(self, post):
        # What the API answers for a query it cannot validate (HTTP 400), with the extensions it adds.
        body = {
            "errors": [
                {
                    "message": 'Cannot query field "getIncome" on type "Query".',
                    "extensions": {"exception": {"stacktrace": ["at node_modules/x.js"] * 500}},
                }
            ]
        }
        post.return_value = _response(400, json.dumps(body))
        self.assertEqual(
            PocketNetworkAPIClient("http://api").execute_query("{ getIncome }"),
            (False, None, 'GraphQL errors: Cannot query field "getIncome" on type "Query".'),
        )

    @mock.patch("src.graphql_client.requests.post")
    def test_other_http_errors_are_capped(self, post):
        post.return_value = _response(502, "x" * (MAX_RAW_ERROR_CHARS * 3))
        success, _, error = PocketNetworkAPIClient("http://api").execute_query("{ x }")
        self.assertFalse(success)
        self.assertEqual(error, "HTTP error: 502 - " + "x" * MAX_RAW_ERROR_CHARS)


if __name__ == "__main__":
    unittest.main()


class TestPageNotes(unittest.TestCase):
    def test_a_function_list_at_the_api_cap_is_a_page(self):
        rows = [{"supplierId": f"pokt1{i}", "stakeUpokt": "1"} for i in range(MAX_LIST_ROWS)]
        notes = page_notes(
            '{ getSupplierFleetList(stakeStatuses: ["Staked"]) { supplierId stakeUpokt } }',
            {"getSupplierFleetList": rows},
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("getSupplierFleetList returned 1000 rows, the most this call returns", notes[0])
        self.assertIn("never all of it", notes[0])

    def test_a_first_above_the_cap_does_not_hide_the_cut(self):
        # the API cuts a function list at 1000 rows whatever first asks, with no signal
        rows = [{"supplierId": "p"}] * MAX_LIST_ROWS
        query = '{ getSupplierFleetList(stakeStatuses: ["Staked"], first: 2000) { supplierId } }'
        self.assertEqual(len(page_notes(query, {"getSupplierFleetList": rows})), 1)

    def test_entity_list_pages(self):
        for query, value in (
            (
                "{ s: suppliers(first: 2) { pageInfo { hasNextPage } nodes { id } } }",
                {"pageInfo": {"hasNextPage": True}, "nodes": [{}, {}]},
            ),
            ("{ s: suppliers(first: 5) { totalCount nodes { id } } }", {"totalCount": 12005, "nodes": [{}] * 5}),
            ("{ s: suppliers(first: 2) { edges { node { id } } } }", {"edges": [{}, {}]}),
        ):
            with self.subTest(query=query):
                notes = page_notes(query, {"s": value})
                self.assertEqual(len(notes), 1)
                self.assertTrue(notes[0].startswith("s returned"))

    def test_a_row_function_at_its_row_limit_is_a_page(self):
        data = {"data": [{"height": "1"}] * 20, "range": {"covered_from": "x", "covered_to": "y"}}
        self.assertIn(
            "its rowLimit", page_notes('{ getClaimsJson(suppliers: ["p"], rowLimit: 20) }', {"getClaimsJson": data})[0]
        )
        data = {"data": [{"height": "1"}] * DEFAULT_ROW_LIMIT, "range": {}}
        self.assertEqual(
            len(page_notes('{ getActorLifecycleJson(kind: "supplier") }', {"getActorLifecycleJson": data})), 1
        )

    def test_complete_answers_have_no_page_note(self):
        for query, result in (
            (
                "{ s: suppliers(first: 5) { totalCount pageInfo { hasNextPage } nodes { id } } }",
                {"s": {"totalCount": 3, "pageInfo": {"hasNextPage": False}, "nodes": [{}] * 3}},
            ),
            ('{ getSupplierFleetList(owners: ["p"]) { supplierId } }', {"getSupplierFleetList": [{}] * 999}),
            ('{ getClaimsJson(suppliers: ["p"], rowLimit: 20) }', {"getClaimsJson": {"data": [{}] * 19, "range": {}}}),
            ('{ getIncomeJson(addresses: ["p"]) }', {"getIncomeJson": {"data": [{}] * 5000, "range": {}}}),
            ("{ _metadata { lastProcessedHeight } }", {"_metadata": {"lastProcessedHeight": 1}}),
        ):
            with self.subTest(query=query):
                self.assertEqual(page_notes(query, result), [])

    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_the_mcp_tool_returns_the_page_note(self, client):
        from src.tools_data import execute_graphql

        client.return_value.execute_query.return_value = (
            True,
            {"getSupplierFleetList": [{"supplierId": "p"}] * 1000},
            None,
        )
        success, _, notes = execute_graphql.func(
            query='{ getSupplierFleetList(stakeStatuses: ["Staked"]) { supplierId } }'
        )
        self.assertTrue(success)
        self.assertIn("Page: getSupplierFleetList returned 1000 rows", notes)
