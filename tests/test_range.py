import json
import re
import unittest
from unittest import mock

import requests

from src.agent import PocketNetworkAgent
from src.graphql_client import (
    NOT_COVERED,
    PocketNetworkAPIClient,
    final_error_reply,
    is_coverage_error,
    is_fixable_query_error,
    not_covered_error,
    range_notes,
    unwrap_range,
)
from src.query_sub_agents import SettlementRewardsAgent
from src.tools_data import MAX_RESULT_CHARS, execute_graphql

# The {range, data} shape of the pocketdex contract (feat/money-range-in-response, 2e88668), with the times of its
# test fixture: coverage starts 2026-09-01 12:00, one settlement gap.
ROW = {
    "bucket_start": "2026-09-01T12:00:00+00:00",
    "bucket_end": "2026-09-03T00:00:00+00:00",
    "address": "pokt1x",
    "role": "shareholder",
    "family": "all",
    "supplier_id": "all",
    "service_id": "all",
    "amount_upokt": "201156529",
    "transfer_count": "12",
}
GAP = {"from": "2026-09-01T23:30:00+00:00", "to": "2026-09-02T00:00:00+00:00"}
PARTIAL_WITH_GAP = {
    "range": {
        "requested_from": "2026-08-31T00:00:00+00:00",
        "requested_to": "2026-09-03T00:00:00+00:00",
        "covered_from": "2026-09-01T12:00:00+00:00",
        "covered_to": "2026-09-02T08:20:00+00:00",
        "gaps": [GAP],
    },
    "data": [ROW],
}
LEGACY_PARTIAL = {
    "range": {
        "requested_from": "2026-08-31T00:00:00+00:00",
        "requested_to": "2026-09-01T13:00:00+00:00",
        "covered_from": "2026-09-01T12:00:00+00:00",
        "covered_to": "2026-09-01T13:00:00+00:00",
        "gaps": [],
    },
    "data": 201156529,
}
COVERED = {
    "range": {
        "requested_from": "2026-09-02T00:00:00+00:00",
        "requested_to": "2026-09-02T06:00:00+00:00",
        "covered_from": "2026-09-02T00:00:00+00:00",
        "covered_to": "2026-09-02T06:00:00+00:00",
        "gaps": [],
    },
    "data": {"reimbursement": 0, "inflation": 123, "mint_burn": 456},
}
# Nothing covered: covered_from / covered_to null; data null for legacy, [] for the catalog _json twins.
NOTHING = {"covered_from": None, "covered_to": None, "gaps": []}
LEGACY_BEFORE = {
    "range": {"requested_from": "2026-08-01T00:00:00+00:00", "requested_to": "2026-08-31T00:00:00+00:00", **NOTHING},
    "data": None,
}
CATALOG_BEFORE = {
    "range": {"requested_from": "2026-08-30T00:00:00+00:00", "requested_to": "2026-08-31T00:00:00+00:00", **NOTHING},
    "data": [],
}
# A range inside the gap: nothing covered, and the gap listed.
CATALOG_IN_GAP = {
    "range": {
        "requested_from": "2026-09-01T23:40:00+00:00",
        "requested_to": "2026-09-01T23:50:00+00:00",
        **NOTHING,
        "gaps": [GAP],
    },
    "data": [],
}
# Before coverage on a database whose history job is still filling: the leading gap is listed, unbounded at its start.
LEGACY_BEFORE_LEADING_GAP = {
    "range": {**LEGACY_BEFORE["range"], "gaps": [{"from": None, "to": "2026-09-01T12:00:00+00:00"}]},
    "data": None,
}
# Swapped dates: a legacy function answers the live function's empty answer with no coverage (not a coverage verdict).
LEGACY_INVERTED = {
    "range": {"requested_from": "2026-10-05T00:00:00+00:00", "requested_to": "2026-10-01T00:00:00+00:00", **NOTHING},
    "data": 0,
}
# The first draft of the contract said "nothing covered" with covered_from after covered_to.
DRAFT_BEFORE = {
    "range": {
        "requested_from": "2026-08-30T00:00:00+00:00",
        "requested_to": "2026-08-31T00:00:00+00:00",
        "covered_from": "2026-09-01T12:00:00+00:00",
        "covered_to": "2026-08-31T00:00:00+00:00",
        "gaps": [],
    },
    "data": [],
}
# A legacy date series over a covered range with no rows: data null (json_agg of nothing), yet covered.
LEGACY_SERIES_EMPTY = {"range": COVERED["range"], "data": None}
# A legacy range of one instant (requested_to / covered_to are the inclusive end_date).
LEGACY_INSTANT = {
    "range": {
        **COVERED["range"],
        "requested_to": "2026-09-02T00:00:00+00:00",
        "covered_to": "2026-09-02T00:00:00+00:00",
    },
    "data": COVERED["data"],
}
# The bare-JSON shape, as data.pocket.network answers on 2026-10-06.
OLD_ROWS = [ROW]
OLD_MINT = {"reimbursement": 726828, "inflation": 726828, "mint_burn": 554224344047}
OLD_TOTAL = "201156529"


def _response(body):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(body).encode()
    return response


class TestUnwrapRange(unittest.TestCase):
    def test_new_shape(self):
        self.assertEqual(unwrap_range(PARTIAL_WITH_GAP), ([ROW], PARTIAL_WITH_GAP["range"]))
        self.assertEqual(unwrap_range(LEGACY_PARTIAL), (201156529, LEGACY_PARTIAL["range"]))
        self.assertEqual(unwrap_range(LEGACY_BEFORE), (None, LEGACY_BEFORE["range"]))

    def test_old_shape(self):
        for value in (OLD_ROWS, OLD_MINT, OLD_TOTAL, None, [], 0):
            with self.subTest(value=value):
                self.assertEqual(unwrap_range(value), (value, None))


class TestRangeNotes(unittest.TestCase):
    def test_partial_range_says_since_and_until_when_and_the_gaps(self):
        notes = range_notes({"getIncomeJson": PARTIAL_WITH_GAP})
        self.assertEqual(len(notes), 3)
        self.assertIn("getIncomeJson: data since 2026-09-01T12:00:00+00:00", notes[0])
        self.assertIn("getIncomeJson: data until 2026-09-02T08:20:00+00:00", notes[1])
        self.assertIn("never 0", notes[1])
        self.assertIn("no data from 2026-09-01T23:30:00+00:00 to 2026-09-02T00:00:00+00:00", notes[2])
        self.assertIn("never 0", notes[2])

    def test_nothing_covered_is_never_zero(self):
        for value in (LEGACY_BEFORE, CATALOG_BEFORE, DRAFT_BEFORE):
            with self.subTest(value=value):
                (note,) = range_notes({"total": value})
                self.assertIn(f"total: {NOT_COVERED}", note)
                self.assertIn("never 0", note)

    def test_a_covered_range_and_the_old_shape_say_nothing(self):
        for result in (
            {"legacyMintBreakdownBetweenDates": COVERED},
            {"legacyMintBreakdownBetweenDates": LEGACY_INSTANT},
            {"getIncomeJson": OLD_ROWS},
            {"legacyMintBreakdownBetweenDates": OLD_MINT},
            {"legacyRewardsByAddressesAndTime": OLD_TOTAL},
            None,
        ):
            with self.subTest(result=result):
                self.assertEqual(range_notes(result), [])

    def test_data_null_over_a_covered_range_is_not_called_uncovered(self):
        (note,) = range_notes({"series": LEGACY_SERIES_EMPTY})
        self.assertIn("found nothing in the covered part", note)
        self.assertNotIn(NOT_COVERED, note)

    def test_unbounded_bounds_are_said_in_words(self):
        value = {
            "range": {**COVERED["range"], "requested_from": None, "requested_to": None},
            "data": COVERED["data"],
        }
        notes = range_notes({"x": value})
        self.assertIn("x: data since 2026-09-02T00:00:00+00:00 (requested from the start)", notes[0])
        self.assertIn("x: data until 2026-09-02T06:00:00+00:00 (requested to now)", notes[1])
        gap = {"range": {**COVERED["range"], "gaps": [{"from": "2026-09-02T05:00:00+00:00", "to": None}]}, "data": 1}
        self.assertIn("no data from 2026-09-02T05:00:00+00:00 to now", range_notes({"x": gap})[0])

    def test_the_leading_gap_says_the_history_is_not_indexed_yet(self):
        notes = range_notes({"x": LEGACY_BEFORE_LEADING_GAP})
        self.assertIn(NOT_COVERED, notes[0])
        self.assertIn("no data before 2026-09-01T12:00:00+00:00: the settlement history", notes[1])

    def test_an_inverted_legacy_range_is_not_a_coverage_verdict(self):
        self.assertEqual(range_notes({"x": LEGACY_INVERTED}), [])

    def test_bounds_without_a_zone_are_utc(self):
        value = {"range": {**COVERED["range"], "requested_from": "2026-09-01T00:00:00"}, "data": COVERED["data"]}
        self.assertIn("data since", range_notes({"x": value})[0])


class TestNotCovered(unittest.TestCase):
    def test_nothing_covered_is_the_coverage_error(self):
        error = not_covered_error({"a": LEGACY_BEFORE, "b": CATALOG_BEFORE})
        self.assertIn(NOT_COVERED, error)
        self.assertTrue(is_coverage_error(error))
        self.assertFalse(is_fixable_query_error(error))
        reply = final_error_reply(error)
        # Nothing says whether the range is before the data or past the latest block: no advice on which way to move.
        self.assertTrue(reply.startswith("Not covered yet: no indexed data covers this range yet"))
        self.assertIsNone(re.search(r"\b0\b|zero", reply.split("Details:")[0]))

    def test_before_coverage_with_the_leading_gap_gets_the_more_recent_range_reply(self):
        reply = final_error_reply(not_covered_error({"total": LEGACY_BEFORE_LEADING_GAP}))
        self.assertIn("Try a more recent range", reply)

    def test_nothing_covered_inside_a_gap_gets_the_gap_reply(self):
        error = not_covered_error({"total": CATALOG_IN_GAP})
        self.assertIn(NOT_COVERED, error)
        self.assertIn("(a gap)", final_error_reply(error))

    def test_a_catalog_range_with_no_start_that_covers_nothing_is_an_error(self):
        # NULL rangeStart = the whole history for the catalog: null coverage is a verdict there.
        value = {"range": {**CATALOG_BEFORE["range"], "requested_from": None}, "data": []}
        self.assertIn(NOT_COVERED, not_covered_error({"getIncomeJson": value}))

    def test_rows_that_report_coverage_are_kept(self):
        # moneyCoverageJson answers its row for a range it covers nothing of: that row is the answer.
        value = {"range": CATALOG_BEFORE["range"], "data": [{"settlements": "0", "missing_heights": "0", "gaps": []}]}
        self.assertIsNone(not_covered_error({"moneyCoverageJson": value}))
        self.assertIn(NOT_COVERED, range_notes({"moneyCoverageJson": value})[0])

    def test_any_covered_field_is_not_an_error(self):
        for result in (
            {"a": LEGACY_BEFORE, "b": LEGACY_PARTIAL},
            {"a": LEGACY_BEFORE, "b": OLD_MINT},
            {"a": PARTIAL_WITH_GAP},
            {"a": LEGACY_SERIES_EMPTY},
            {"a": LEGACY_INVERTED},
            {"a": OLD_ROWS},
            {"a": None},
        ):
            with self.subTest(result=result):
                self.assertIsNone(not_covered_error(result))

    @mock.patch("src.graphql_client.requests.post")
    def test_the_client_fails_an_answer_that_covers_nothing(self, post):
        post.return_value = _response({"data": {"total": LEGACY_BEFORE}})
        success, result, error = PocketNetworkAPIClient("http://api").execute_query("{ total: x }")
        self.assertEqual((success, result), (False, None))
        self.assertTrue(is_coverage_error(error))
        post.return_value = _response({"data": {"total": LEGACY_PARTIAL}})
        self.assertEqual(
            PocketNetworkAPIClient("http://api").execute_query("{ total: x }"), (True, {"total": LEGACY_PARTIAL}, None)
        )


class TestExecuteGraphqlRange(unittest.TestCase):
    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_the_range_is_said_with_the_result(self, client):
        result = {"getIncomeJson": PARTIAL_WITH_GAP}
        client.return_value.execute_query.return_value = (True, result, None)
        success, returned, note = execute_graphql.func(query="{ getIncomeJson }")
        self.assertEqual((success, returned), (True, result))
        self.assertIn("data since 2026-09-01T12:00:00+00:00", note)
        self.assertIn("no data from 2026-09-01T23:30:00+00:00", note)

    @mock.patch("src.graphql_client.requests.post")
    def test_nothing_covered_fails_like_the_coverage_error(self, post):
        post.return_value = _response({"data": {"total": LEGACY_BEFORE}})
        success, returned, error = execute_graphql.func(query="{ total: legacyRewardsByAddressesAndTime }")
        self.assertEqual((success, returned), (False, None))
        self.assertTrue(is_coverage_error(error))

    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_the_old_shape_is_returned_as_is(self, client):
        result = {"legacyMintBreakdownBetweenDates": OLD_MINT}
        client.return_value.execute_query.return_value = (True, result, None)
        self.assertEqual(execute_graphql.func(query="{ legacyMintBreakdownBetweenDates }"), (True, result, None))

    @mock.patch("src.tools_data.PocketNetworkAPIClient")
    def test_a_truncated_result_keeps_the_range_note(self, client):
        value = {"range": PARTIAL_WITH_GAP["range"], "data": [{"x": "a" * MAX_RESULT_CHARS}]}
        client.return_value.execute_query.return_value = (True, {"getIncomeJson": value}, None)
        success, returned, note = execute_graphql.func(query="{ getIncomeJson }")
        self.assertEqual(len(returned), MAX_RESULT_CHARS)
        self.assertIn("truncated", note)
        self.assertIn("data since", note)


def _envelope(query):
    return mock.MagicMock(
        tool_calls=[],
        content=json.dumps({"endpoint_type": "graphql", "endpoint_method": "getIncomeJson", "query": query}),
    )


class TestAgentRange(unittest.TestCase):
    def _run(self, result):
        llm = mock.MagicMock()
        llm.bind_tools.return_value.invoke.return_value = _envelope(
            '{ getIncomeJson(addresses: ["pokt1x"], rangeStart: "2026-08-31T00:00:00Z") }'
        )
        sub_agent = SettlementRewardsAgent(llm)
        sub_agent.graphql_client = PocketNetworkAPIClient("http://api")
        agent = PocketNetworkAgent.__new__(PocketNetworkAgent)
        agent.llm = mock.MagicMock()
        agent.sub_agents = [sub_agent]
        state = {"user_query": "what did pokt1x earn?", "selected_subagent": sub_agent, "agent_notes": ""}
        with mock.patch("src.graphql_client.requests.post", return_value=_response({"data": result})) as post:
            state.update(agent._build_query(state))
        return agent, post, state

    def test_partial_range_reaches_the_answer_notes(self):
        agent, post, state = self._run({"getIncomeJson": PARTIAL_WITH_GAP})
        self.assertIsNone(state.get("error"))
        self.assertEqual(state["query_result"], {"getIncomeJson": PARTIAL_WITH_GAP})
        self.assertIn("Coverage: getIncomeJson: data since 2026-09-01T12:00:00+00:00", state["agent_notes"])
        self.assertIn("Coverage: getIncomeJson: data until 2026-09-02T08:20:00+00:00", state["agent_notes"])
        self.assertIn("Coverage: getIncomeJson: no data from 2026-09-01T23:30:00+00:00", state["agent_notes"])

    def test_nothing_covered_ends_as_not_covered_without_a_retry(self):
        agent, post, state = self._run({"getIncomeJson": CATALOG_BEFORE})
        self.assertEqual(post.call_count, 1)
        self.assertIn(NOT_COVERED, state["error"])
        self.assertTrue(agent._format_refusal(state)["agent_notes"].startswith("Not covered yet"))
        agent.llm.invoke.assert_not_called()

    def test_old_shape_adds_no_coverage_note(self):
        agent, post, state = self._run({"getIncomeJson": OLD_ROWS})
        self.assertIsNone(state.get("error"))
        self.assertNotIn("Coverage:", state["agent_notes"])


if __name__ == "__main__":
    unittest.main()
