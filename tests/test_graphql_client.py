import json
import unittest
from unittest import mock

import requests

from src.graphql_client import MAX_RAW_ERROR_CHARS, PocketNetworkAPIClient


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
