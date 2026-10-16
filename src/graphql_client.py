"""GraphQL API client for Pocket Network."""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, List, Tuple

import requests

from src.models import QueryFieldInfo

POCKET_NETWORK_DATA_ENDPOINT = os.getenv("POCKET_NETWORK_DATA_ENDPOINT", None)
if POCKET_NETWORK_DATA_ENDPOINT is None:
    raise ValueError('The "POCKET_NETWORK_DATA_ENDPOINT" enviroment variable is not set.')


# Longest raw (non-GraphQL) error body passed on, in characters.
MAX_RAW_ERROR_CHARS = 2000


def _graphql_errors(errors: list) -> str:
    """The messages of a GraphQL "errors" list: the API adds a stack trace and the internal SQL in "extensions"."""
    return "GraphQL errors: " + "; ".join(
        [err.get("message", str(err)) if isinstance(err, dict) else str(err) for err in errors]
    )


class PocketNetworkAPIClient:
    """Client for querying the Pocket Network GraphQL API."""

    def __init__(self, endpoint: str = POCKET_NETWORK_DATA_ENDPOINT):
        """
        Initialize the API client.

        Args:
            endpoint: GraphQL endpoint URL
        """
        self.endpoint = endpoint

    def execute_query(self, query: str) -> Tuple[bool, Any, str | None]:
        """
        Execute a GraphQL query against the Pocket Network API.

        Args:
            query: GraphQL query string

        Returns:
            Tuple of (success, result, error_message)
        """
        try:
            headers = {
                "Content-Type": "application/json",
            }

            payload = {"query": query}

            response = requests.post(self.endpoint, json=payload, headers=headers, timeout=30)

            # Raise for HTTP errors
            response.raise_for_status()

            data = response.json()

            # Check for GraphQL errors
            if "errors" in data and data["errors"]:
                return False, None, _graphql_errors(data["errors"])

            if "data" in data:
                # A {range, data} answer that covers nothing fails as the API's coverage error did before the range.
                not_covered = not_covered_error(data["data"])
                if not_covered:
                    return False, None, not_covered
                return True, data["data"], None
            else:
                return False, None, "No data returned from API"

        except requests.exceptions.Timeout:
            return False, None, "API request timeout (30s)"
        except requests.exceptions.ConnectionError:
            return False, None, "Connection error to API. Please check the endpoint."
        except requests.exceptions.HTTPError as e:
            # A query the API cannot validate comes back as HTTP 400 with a GraphQL {"errors": [...]} body.
            try:
                body = e.response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("errors"):
                return False, None, _graphql_errors(body["errors"])
            return (
                False,
                None,
                f"HTTP error: {e.response.status_code} - {e.response.text[:MAX_RAW_ERROR_CHARS]}",
            )
        except requests.exceptions.RequestException as e:
            return False, None, f"Request error: {str(e)}"
        except json.JSONDecodeError as e:
            return False, None, f"Invalid JSON response from API: {str(e)}"
        except Exception as e:
            return False, None, f"Unexpected error: {str(e)}"


# Said for an answer in the {range, data} shape that covers nothing of the range: what the API raised before it
# answered with the range.
NOT_COVERED = "no indexed data covers this range"
# Said for the leading gap of a {range, data} answer: the history the history job has not written yet.
HISTORY_NOT_INDEXED = "the settlement history before it is not indexed yet"
# Errors of the settlement functions for a range their tables do not cover (pocketdex _check_coverage). The answer is
# "not covered yet": another query cannot fix it.
COVERAGE_ERRORS = (
    NOT_COVERED,
    "no settlement height is written yet",
    "before the first written settlement",
    "which are not written (settlement_gaps)",
    "run rebuild_rollups first",
)
# Errors that a corrected query fixes: a field or argument the schema does not have, or a catalog argument out of its
# bounds (pocketdex _validate and the catalog functions); each message says what to change. Of the list-size limits
# (LIST_LIMIT, TOP_LIMIT) only an empty list or N below 1: above the maximum a model "fixes" them by dropping ids or
# lowering N, and then reports a partial total as the full one (final_error_reply ends the run instead).
FIXABLE_QUERY_ERRORS = (
    "Cannot query field",
    "Unknown argument",
    "Unknown type",
    "is not defined by type",
    "of required type",
    "must have a selection of subfields",
    "must not have a selection",
    "Expected value of type",
    "invalid bucket",
    "allows ranges up to",
    "range_start must be earlier than range_end",
    "pass suppliers or owners",
    "pass services, top_by_settled",
)


def is_coverage_error(error: str | None) -> bool:
    """True when the API refused a range the settlement tables do not cover yet."""
    return bool(error) and any(marker in error for marker in COVERAGE_ERRORS)


LIST_LIMIT = re.compile(r"(\w+) must have between 1 and (\d+) elements \(has (\d+)\)")
TOP_LIMIT = re.compile(r"top_by_settled must be between 1 and (\d+) \(is (-?\d+)\)")


def final_error_reply(error: str | None) -> str | None:
    """What to tell the user for an error that no other query fixes, or None for any other error.

    A range the settlement tables do not cover has no number to report: the reply must never present it as zero.
    """
    if not error:
        return None
    # First: a {range, data} answer that covers nothing because it lies in a gap says both (the API errors never do).
    if "which are not written (settlement_gaps)" in error:
        return (
            "Not covered yet: this range overlaps settlement heights that are not written yet (a gap), so there is no "
            f"number to report for it. Try a range outside the gap, or again later. Details: {error}"
        )
    if (
        "before the first written settlement" in error
        or "no settlement height is written yet" in error
        or HISTORY_NOT_INDEXED in error
    ):
        return (
            "Not covered yet: the settlement data for this range is not indexed yet, so there is no number to report "
            f"for it. Try a more recent range. Details: {error}"
        )
    # Nothing covered, and nothing says on which side: before the data, or after the latest indexed block.
    if NOT_COVERED in error:
        return (
            "Not covered yet: no indexed data covers this range yet, so there is no number to report for it. "
            f"Details: {error}"
        )
    if "run rebuild_rollups first" in error:
        return (
            "Not available right now: the settlement summary tables are being rebuilt, so there is no number to "
            "report for this range yet. Try again later."
        )
    # An empty list or a top below 1 is retried first (is_fixable_query_error); it reaches here when the retries could
    # not fill it, e.g. a question with no address.
    match = LIST_LIMIT.search(error)
    if match and int(match.group(3)) < 1:
        return f"The question needs at least one of the {match.group(1)} to look up."
    if match and int(match.group(3)) > int(match.group(2)):
        name, limit, count = match.group(1), match.group(2), match.group(3)
        return (
            f"Too many {name}: {count}, the limit is {limit} per question. Split the question into groups of at most "
            f"{limit} and add the results."
        )
    match = TOP_LIMIT.search(error)
    if match and int(match.group(2)) < 1:
        return "Say how many of the top services to rank (at least one)."
    if match and int(match.group(2)) > int(match.group(1)):
        return f"At most the top {match.group(1)} services can be ranked per question (asked for {match.group(2)})."
    return None


def _is_fixable_limit_error(error: str) -> bool:
    """An empty list or a top_by_settled below 1: a corrected query adds what is missing, it drops nothing."""
    match = LIST_LIMIT.search(error)
    if match and int(match.group(3)) < 1:
        return True
    match = TOP_LIMIT.search(error)
    return bool(match) and int(match.group(2)) < 1


def is_fixable_query_error(error: str | None) -> bool:
    """True when the API refused the query itself, so that a corrected query may succeed."""
    return (
        bool(error)
        and not is_coverage_error(error)
        and (any(marker in error for marker in FIXABLE_QUERY_ERRORS) or _is_fixable_limit_error(error))
    )


def unwrap_range(value: Any) -> Tuple[Any, dict | None]:
    """Split the answer of a settlement function into (data, range).

    pocketdex answers either with the bare JSON (and raises for a range it does not cover), or with
    {"range": {requested_from, requested_to, covered_from, covered_to, gaps}, "data": <the same JSON>}, where
    covered_from and covered_to are None when nothing in the range is covered (data is then None or []). The JSON
    arrives parsed; the old BigFloat totals arrive as strings.
    """
    if isinstance(value, dict) and "data" in value and isinstance(value.get("range"), dict):
        return value["data"], value["range"]
    return value, None


def _time(value: Any) -> datetime | None:
    """A range bound as a UTC datetime (a bound without a zone is UTC), or None."""
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed and parsed.tzinfo is None else parsed


def _covers_nothing(range_: dict) -> bool:
    """Nothing in the range is covered: the covered bounds are None (or, from an earlier pocketdex, inverted).

    data None alone does not say it: a legacy date series over a covered range with no rows is None too. Nor do None
    bounds over an inverted requested range: a legacy function then answers the live function's empty answer (it
    matches nothing), which is not a coverage verdict.
    """
    requested_from, requested_to = _time(range_.get("requested_from")), _time(range_.get("requested_to"))
    if requested_from is not None and requested_to is not None and requested_from > requested_to:
        return False
    covered_from, covered_to = _time(range_.get("covered_from")), _time(range_.get("covered_to"))
    return covered_from is None or covered_to is None or covered_from > covered_to


def _bound(value: Any, unbounded: str) -> str:
    return unbounded if value is None else str(value)


def _ranged_fields(result: Any) -> List[Tuple[str, Any, dict]]:
    """(field or alias, data, range) of each field of a GraphQL result that answers in the {range, data} shape."""
    if not isinstance(result, dict):
        return []
    fields = []
    for name, value in result.items():
        data, range_ = unwrap_range(value)
        if range_ is not None:
            fields.append((name, data, range_))
    return fields


def _field_notes(name: str, data: Any, range_: dict) -> List[str]:
    covered_from, requested_from = range_.get("covered_from"), range_.get("requested_from")
    covered_to, requested_to = range_.get("covered_to"), range_.get("requested_to")
    # range.end_inclusive: true for legacy... (end_date included), false for the catalog; said only when sent.
    flag = range_.get("end_inclusive")
    end = ", included" if flag is True else ", excluded" if flag is False else ""
    notes = []
    covers_nothing = _covers_nothing(range_)
    if covers_nothing:
        notes.append(f"{name}: {NOT_COVERED}, so it has no number to report: say not covered yet, never 0.")
    elif _time(covered_from) is not None and _time(covered_to) is not None:
        # A NULL requested bound asked for all the data on that side.
        if _time(requested_from) is None or _time(covered_from) > _time(requested_from):
            notes.append(
                f"{name}: data since {covered_from} (requested from {_bound(requested_from, 'the start')}): say so "
                "with the result; what comes before it is not covered yet, never 0."
            )
        if _time(requested_to) is None or _time(covered_to) < _time(requested_to):
            notes.append(
                f"{name}: data until {covered_to}{end} (requested to {_bound(requested_to, 'now')}): say so with "
                "the result; what comes after it is not covered yet, never 0."
            )
        if data is None:
            notes.append(
                f"{name}: data null with covered bounds: no rows in a covered range (the function found nothing "
                "there), not a range that is not covered."
            )
    spans = []
    for gap in range_.get("gaps") or []:
        if gap.get("from") is None:
            # The leading gap: the history before the first written settlement, which the history job fills. With
            # data, "data since" already says it.
            if covers_nothing:
                notes.append(f"{name}: no data before {gap.get('to')}: {HISTORY_NOT_INDEXED}, not covered, never 0.")
        else:
            spans.append(f"{gap['from']} to {_bound(gap.get('to'), 'now')}")
    if spans:
        notes.append(
            f"{name}: no data from {', '.join(spans)}: settlement heights which are not written (settlement_gaps), "
            "not covered, never 0."
        )
    return notes


def range_notes(result: Any) -> List[str]:
    """What the answer must say about the part of the range each field covers; empty for the bare-JSON shape."""
    return [note for name, data, range_ in _ranged_fields(result) for note in _field_notes(name, data, range_)]


# Longest result handed to a model, in characters of JSON: what the MCP tool returns and what the sub-agent answer is
# written from.
MAX_RESULT_CHARS = 100_000


def not_covered_error(result: Any) -> str | None:
    """The coverage error for a result whose every field is in the {range, data} shape and covers nothing, else None.

    Whatever data such a field carries (a legacy by-service list of zeros, the zero mint object) is not a number to
    report, so the answer fails as the API's coverage error did.
    """
    fields = _ranged_fields(result)
    if not fields or len(fields) < len(result) or not all(_covers_nothing(r) for _, _, r in fields):
        return None
    return "Settlement range: " + " ".join(note for field in fields for note in _field_notes(*field))


# Rules shared by the settlement catalog functions (get...Json, moneyCoverageJson), which read precomputed
# settlement tables: fast, but they cover settlements from a starting height only and cap the range per bucket.
COVERAGE_NOTE = (
    "The settlement data starts at a height that moves back toward genesis while the history is filled (mainnet: "
    "April 2026 as of October 2026; moneyCoverageJson tells). The API answers in one of two shapes. Bare JSON: a "
    "range that starts before the data, or crosses a gap, is an error. Or "
    '{"range": {"requested_from", "requested_to", "covered_from", "covered_to", "gaps": [{"from", "to"}]}, '
    '"data": <the same JSON>}, answered from the covered part. Only covered_from and covered_to null mean nothing is '
    "covered (data is then null for legacy..., [] for ...Json). data null with covered bounds is no rows in a "
    "covered range (a legacy... series that found nothing), not a range that is not covered. A covered_from later "
    'than requested_from means the data starts there (say "data since <covered_from>"), and a covered_to earlier '
    "than requested_to that it ends there. legacy... ranges include their end (end_date); ...Json ranges and gaps "
    "are [from, to), the end excluded; range.end_inclusive says which when present. The gaps have no data. A range "
    'or gap not covered is "not covered yet", never 0: report it so, and do not retry it.'
)
CATALOG_NOTES = {
    "rangeStart/rangeEnd": "The range [start, end) in UTC with an explicit zone, e.g. 2026-10-01T00:00:00Z. Pass both.",
    "bucket": "Omit for one total per row, or hour (range up to 7 days), day (up to 92 days), week (up to 366 days), month or year. A range longer than the bucket allows is an error that names the bucket to use.",
    "coverage": COVERAGE_NOTE,
    "result": 'A JSON array of rows (in "data" in the {range, data} shape) with snake_case keys; amounts and counts are strings (upokt). "all" in a column means the rows are not split by it. Within a covered range, a missing row means 0.',
}
# The legacy... functions keep the arguments and JSON of the functions they replace, read from the same tables.
LEGACY_NOTES = {"coverage": COVERAGE_NOTE}
# Rules shared by the functions that list rows one by one (getClaimsJson, getActorLifecycleJson,
# getRelayMiningDifficultyJson): newest first, a page at a time.
ROW_NOTES = {
    "rangeStart/rangeEnd": "Optional range [start, end) in UTC with an explicit zone, e.g. 2026-10-01T00:00:00Z; omit both for the whole history.",
    "rowLimit": "Rows per call, newest first: default 100, at most 1000. For the next page pass the last row's keys as the before... arguments; a page shorter than rowLimit is the last one.",
    "result": '{"range": {...}, "data": [rows]}: the rows in "data" with snake_case keys, numbers as strings (amounts in upokt), and the part of the range the data covers in "range". No rows, or a missing row, in a part of the range outside covered_from / covered_to or in a gap is not covered yet, never none or 0.',
}
# Rules shared by the functions that answer the current state (getSupplierFleetList, getApplicationFleetList,
# getGatewayFleetList, getServiceSummaryList, getMorseClaimSummaryList): their rows are selected like a list's fields.
# They take no blockHeight and are cut at 1000 rows with no signal. An offset walk over rows ordered by stake skips or
# repeats the rows whose stake changes between calls, so they are not paged by offset; getSupplierFleetList has its
# own keyset (SUPPLIER_FLEET_PAGING), which is not a snapshot either.
NOW_NOTES = {
    "result": "Rows with camelCase fields to select, e.g. { stakeUpokt supplierCount }; amounts in upokt.",
    "paging": "At most 1000 rows a call, cut with no signal: an answer of 1000 rows is a part of the list, never all of it. Not paged by offset: for counts and sums use the one-total form, and for every row of a long list the entity list (applications, gateways, services) paged by keyset with one pinned blockHeight.",
}
SUPPLIER_FLEET_PAGING = (
    "With rowLimit (up to 1000): one page, largest stake first; for the next page pass the last row's stakeUpokt and "
    "supplierId as afterStakeUpokt and afterSupplierId, until a page comes back short. Never offset, and never more "
    "than 1000 rows without rowLimit (the list is cut with no signal). This keyset is not a snapshot: a supplier whose "
    "stake changes between pages can repeat or go missing. For every supplier in one consistent read, page the "
    "suppliers list by keyset with one pinned blockHeight."
)

# Registry of available GraphQL query fields with descriptions
GRAPHQL_REGISTRY = {
    "balances": QueryFieldInfo(
        name="balances",
        description="Queries account balances, not tied to any specific actor (supplier, application, etc.).",
        fields_notes={
            "lastUpdatedBlockId": "Height of the block that last updated the balance; its time is that block's timestamp (blocks).",
        },
        examples=[
            "# Top accounts by upokt balance (rich list), with last-activity block\n"
            'query { balances(first: 20, orderBy: AMOUNT_DESC, filter: {denom: {equalTo: "upokt"}}) '
            "{ totalCount nodes { accountId amount denom lastUpdatedBlockId } } }",
            "# Count accounts active (balance updated) since a block height (since a date: getActiveAccountsList)\n"
            'query { balances(filter: {lastUpdatedBlockId: {greaterThanOrEqualTo: "950000"}, '
            'denom: {equalTo: "upokt"}}) { totalCount } }',
        ],
    ),
    "getRewardsByDate": QueryFieldInfo(
        name="getRewardsByDate",
        description="Total relays, computed units, and claimed tokens data on a given period for the whole network",
        examples=[
            "# Network relays / computed units / claimed tokens bucketed by interval (e.g. day, hour)\n"
            'query { getRewardsByDate(startDate: "2026-06-01T00:00:00Z", endDate: "2026-06-11T00:00:00Z", '
            'truncInterval: "day") }',
        ],
    ),
    "getTotalSupplyBetweenDates": QueryFieldInfo(
        name="getTotalSupplyBetweenDates",
        description="Total network supply and its composition: staked, unstaked, supplier stakes, apps stakes, etc. on a given period for the whole network",
    ),
    "getDaoBalanceAtHeight": QueryFieldInfo(
        name="getDaoBalanceAtHeight",
        description="DAO holdings in upokt at the given block hieght",
        examples=[
            "# DAO treasury balance in upokt at the latest block (no argument = latest)\n"
            "query { getDaoBalanceAtHeight }",
        ],
    ),
    "params": QueryFieldInfo(
        name="params",
        description="Returns the network configuration parameters. These are usefull to understand the network behaviour (minting, relaying, consensus, etc) at a given time. This method has access all parameter values, for all namespaces (all params in the RPC endpoints).",
        fields_notes={
            "value": "Value of the parameter.",
            "id": 'Compound name of the parameter, formed by: "<namespace>-<key>".',
            "blockId": "The block number when the parameter was set/changed.",
        },
        examples=[
            "# Latest value of every on-chain module parameter (one row per namespace/key)\n"
            "query { params(orderBy: [BLOCK_ID_DESC], distinct: [NAMESPACE, KEY], first: 1000) "
            "{ nodes { namespace key value blockId } } }",
            "# Latest values of specific params in a namespace\n"
            'query { params(filter: {namespace: {equalTo: "shared"}, key: {in: ["claim_window_open_offset_blocks", '
            '"claim_window_close_offset_blocks", "proof_window_close_offset_blocks"]}}, orderBy: [BLOCK_ID_DESC], '
            "distinct: [NAMESPACE, KEY], first: 100) { nodes { key value blockId } } }",
        ],
    ),
    "blocks": QueryFieldInfo(
        name="blocks",
        description="Returns data of the blockchain blocks. Use this when you need per-block data on network wide metrics or you are tracking specific block metrics (like block generation time, timestamp, size, etc.)",
        fields_notes={
            "timeToBlock": "Time spent generating the block in milliseconds.",
            "size": "Block size in bytes.",
            "hash": "Block hash; use filter {hash: {equalTo: ...}} to look up a block by hash.",
            "totals": "Sums and averages over many blocks (relays, compute units, transactions, block time and size): getBlockStatsJson. The supply: getTotalSupplyBetweenDates; one block's supply per denomination: getBlockSuppliesList.",
        },
        examples=[
            "# Latest block with network-wide snapshot fields (staked actors, supply, relays)\n"
            "query { blocks(orderBy: ID_DESC, first: 1) { nodes { height: id hash timestamp totalTxs totalRelays "
            "totalComputedUnits stakedValidators stakedSuppliers stakedSuppliersTokens stakedApps stakedAppsTokens "
            "stakedGateways timeToBlock size } } }",
            "# Look up a single block by height (the block id)\n"
            'query { block(id: "790000") { height: id hash timestamp totalTxs proposerAddress } }',
        ],
    ),
    "authzs": QueryFieldInfo(
        name="authzs",
        description="Tracks administration authorization on accounts. Use this know which account can modify which network parameter",
        fields_notes={
            "granterId": "Address of the account giving access.",
            "granteeId": "Address of the account that was given accerss.",
            "msg": "Permission type provided",
        },
    ),
    "services": QueryFieldInfo(
        name="services",
        description="Provides visibility on the network services, their names, owners, current difficulty, etc.",
        fields_notes={
            "id": "String to be used when the service is requested or referred on-chain.",
            "name": "Name of the service, human readable string.",
            "computeUnitsPerRelay": "Numeber of Compute Units to be consumed per call to this service (known normaly by CUPR)",
            "ownerId": "Address of the account that owns this service (the one that can cahnge the CUPR value and receives owner rewards).",
            "suppliers/applications": "How many suppliers and applications a service has: getServiceSummaryList. Which ones: supplierServiceConfigs or applicationServices filtered by serviceId.",
            "difficulty": "The current relay mining difficulty of a service: getServiceSummaryList (a row for every live service, zeros when it has no supplier or application now); its history: getRelayMiningDifficultyJson.",
        },
        examples=[
            "# Service list, with the suppliers, applications and current difficulty of every service in the same request\n"
            "query { services(first: 20, orderBy: ID_ASC) { totalCount nodes { id name computeUnitsPerRelay ownerId } } "
            "getServiceSummaryList { serviceId suppliers stakedSuppliers applications numRelaysEma difficultyUpdatedHeight } }",
            "# Fuzzy search services by id or name\n"
            'query { services(filter: {or: [{id: {includesInsensitive: "eth"}}, '
            '{name: {includesInsensitive: "eth"}}]}, first: 20) { nodes { id name } } }',
        ],
    ),
    "suppliers": QueryFieldInfo(
        name="suppliers",
        description="Provides visibility on the network suppliers, their owners, stake status (and block of stake/unstake), stake amount, etc.",
        fields_notes={
            "operatorId": "The address of the supplier.",
            "ownerId": "The address of the owner of the stake.",
            "services": "The services a supplier is staked in: supplierServiceConfigs filtered by supplierId.",
            "total stake": "The count and total stake of a group of suppliers (staked, of an owner, of a domain): getSupplierFleetList.",
        },
        examples=[
            "# Staked suppliers: count (their total stake: getSupplierFleetList)\n"
            "query { suppliers(filter: {stakeStatus: {equalTo: Staked}}) { totalCount } }",
            "# Supplier list, first page, with owner/operator and stake\n"
            "query { suppliers(first: 100, orderBy: ID_ASC) { totalCount pageInfo { hasNextPage endCursor } "
            "nodes { id ownerId operatorId stakeAmount stakeStatus } } }",
            "# Next page of a walk over several pages: after = the previous page's endCursor, the same orderBy, and on "
            "every page the same blockHeight (the _metadata lastProcessedHeight read once before the first page); stop "
            "when hasNextPage is false, and the rows must add up to totalCount\n"
            'query { suppliers(first: 100, orderBy: ID_ASC, blockHeight: "964638", after: "<endCursor>") { totalCount '
            "pageInfo { hasNextPage endCursor } nodes { id ownerId operatorId stakeAmount stakeStatus } } }",
        ],
    ),
    "getComputeUnitsToTokensMultiplierEvolution": QueryFieldInfo(
        name="getComputeUnitsToTokensMultiplierEvolution",
        description="Tracks the changes of the CUTTM over time, a critical tokenomics parameter that stabilices the cost of the compute unit to the target USD value.",
    ),
    "getRelaysByServicePerPointJson": QueryFieldInfo(
        name="getRelaysByServicePerPointJson",
        description="Returns a JSON string containing the total traffic per service of the network in the provided dates and truncated as requested. Usefull for network-wide traffic analisys.",
        examples=[
            "# Per-service traffic time-series (JSON string result)\n"
            'query { getRelaysByServicePerPointJson(startTimestamp: "2026-06-01T00:00:00Z", '
            'endTimestamp: "2026-06-11T00:00:00Z", truncInterval: "day") }',
        ],
    ),
    "getAmountOfBlocksAndSuppliersByTimes": QueryFieldInfo(
        name="getAmountOfBlocksAndSuppliersByTimes",
        description="Returns a JSON with suppliers per service. Use this to get average of suppliers per service on a given date range. The returned value will be the SUM of the number of suppliers and blocks observed in each day, between the provided dates. To obtain the average amount of suppliers in a service, divide the reported number by the amount of blocks returned in the query.",
        fields_notes={
            "blocks": "Total number of blocks in the time range.",
            "suppliers_staked": 'Sum of the number of suppliers staked at each block time. Divide this by "blocks" to get the average amount of suppliers in the service for the selected period.',
            "computeUnitsPerRelay": "Numeber of Compute Units to be consumed per call to this service (known normaly by CUPR)",
            "service_id": "ID of the service.",
        },
        examples=[
            "# Sum of blocks and suppliers-staked per service in a date range (divide to get the average)\n"
            'query { getAmountOfBlocksAndSuppliersByTimes(startDate: "2026-06-01T00:00:00Z", '
            'endDate: "2026-06-11T00:00:00Z") }',
        ],
    ),
    "getSupplyCompositionBetweenDates": QueryFieldInfo(
        name="getSupplyCompositionBetweenDates",
        description="Returns a JSON list containing the detailed supply composition (staked in apps/services/gateways/suppliers, DAO treasury, wrapped pokt, etc) along with the total supply, the date and block of the data. Usefull for network-wide supply evolution analisys, total staked tokens analysis, total migrated tokens, etc.",
        fields_notes={
            "truncInterval": 'Granularity of the returned series ("day", "month", "year"). Set to "day" if the user is asking for current supply and use current date and day before for the date limits. For long spans (multiple years) prefer "month" or "year" so a single query returns one point per interval instead of one per day.',
        },
        examples=[
            "# Current supply breakdown and total supply (one snapshot)\n"
            'query { getSupplyCompositionBetweenDates(startDate: "2026-06-24T00:00:00Z", '
            'endDate: "2026-06-25T00:00:00Z", truncInterval: "day") }',
            "# Supply breakdown AND total supply over the last 12 months\n"
            "# in monthly intervals with a single query (one point per month, not per day)\n"
            'query { getSupplyCompositionBetweenDates(startDate: "2025-06-25T00:00:00Z", '
            'endDate: "2026-06-25T00:00:00Z", truncInterval: "month") }',
        ],
    ),
    "getTotalSupplyByDay": QueryFieldInfo(
        name="getTotalSupplyByDay",
        description="Returns supply by day for quick analysis, contains shannon_supply, unstaked_balance_amount, supplier_stake_amount, application_stake_amount and total_supply.",
        examples=[
            "# Daily supply series for a date range\n"
            'query { getTotalSupplyByDay(startDate: "2026-06-01T00:00:00Z", endDate: "2026-06-11T00:00:00Z") }',
        ],
    ),
    "getClaimProofsDataByTime": QueryFieldInfo(
        name="getClaimProofsDataByTime",
        description="Provides data on the claim-proof data between the provided dates and the given granualirity. Usefull for global analysis of total claims/proofs/expirations denominated in computed units, relays and upokt",
        examples=[
            "# Network-wide claims vs proofs time-series\n"
            'query { getClaimProofsDataByTime(startTs: "2026-06-01T00:00:00Z", endTs: "2026-06-11T00:00:00Z", '
            'truncInterval: "day") }',
        ],
    ),
    "getRewardsByDomainsAndTimeGroupByService": QueryFieldInfo(
        name="getRewardsByDomainsAndTimeGroupByService",
        description="Returns the total number of suppliers, rewards (CUs, upokt, relays) stake for a given domain name in each of their staked services.",
        fields_notes={
            "domains": 'A vector of domains to query: ["foo.bar", "example.com", ...]. extracted normally from the staked endpoint data of a supplier.',
            "endTs/startTs": "These only respect DAY granularity, it will ignore any hours-min-sec passed, it is a DATE field, not a DATETIME",
        },
    ),
    "getOverservicedByAddressesAndTime": QueryFieldInfo(
        name="getOverservicedByAddressesAndTime",
        description="Returns the total overservicing done by an application or a supplier. Overservice occus when the appplication pays less to the supplier than expected. This is usefull for tracking suppliers that are being too optimistic or by app that are close to be zeroed in stake. A high overservicing is bad for the network.",
        examples=[
            "# Overservicing time-series for a group of addresses\n"
            'query { getOverservicedByAddressesAndTime(addresses: ["pokt1..."], '
            'startTs: "2026-06-01T00:00:00Z", endTs: "2026-06-11T00:00:00Z", truncInterval: "day") }',
        ],
    ),
    "transactions": QueryFieldInfo(
        name="transactions",
        description="Queries indexed transactions. Filter by signer address, block height or transaction hash (the id field). Use this for the transaction history of an account, the transactions in a block, or looking up a single transaction by hash. Order by BLOCK_ID_DESC to get the most recent first.",
        fields_notes={
            "id": "The transaction hash, a 64-character hex string.",
            "code": "Result code of the transaction: 0 means success, any other value means it failed.",
            "signerAddress": 'The address (must start with "pokt1") that signed the transaction. Filter by this for an account transaction history.',
            "fees": "Fees paid by the transaction, in upokt.",
            "gasUsed": "Gas consumed by the transaction.",
            "amountOfMessages": "Number of messages contained in the transaction.",
            "blockId": "Height of the block holding the transaction; its time is that block's timestamp (blocks).",
            "counts": "Successful and failed transactions in a time window: getBlockStatsJson.",
        },
        examples=[
            "# Transaction history of an address (most recent first; older ones: the same query with after: <endCursor>)\n"
            'query { transactions(first: 20, filter: {signerAddress: {equalTo: "pokt1..."}}, '
            "orderBy: [BLOCK_ID_DESC, ID_DESC]) { totalCount pageInfo { hasNextPage endCursor } nodes { id code blockId "
            "gasUsed gasWanted signerAddress fees amountOfMessages } } }",
            "# Look up a transaction by hash\n"
            'query { transactions(filter: {id: {equalTo: "<TX_HASH_64_CHAR_HEX>"}}, first: 1) '
            "{ nodes { id code codespace blockId signerAddress fees memo } } }",
        ],
    ),
    "nativeTransfers": QueryFieldInfo(
        name="nativeTransfers",
        description="Tracks native token transfers (MsgSend) between accounts. Filter by senderId and/or recipientId to get the transfer history of a wallet (who sent tokens to whom); with the block time and the transaction's fees and code in the same call: getNativeTransfersJson. Reward payouts from network modules are not transfers: use getIncomeJson for them.",
        fields_notes={
            "senderId": 'The address sending the tokens (must start with "pokt1").',
            "recipientId": 'The address receiving the tokens (must start with "pokt1").',
            "amounts": "Transferred amounts.",
            "denom": 'Token denomination, normally "upokt".',
            "transactionId": "Hash of the enclosing transaction; its fees and code are in transactions (filter id).",
        },
        examples=[
            "# Transfers where an address is sender or recipient, with the enclosing transaction\n"
            "query { nativeTransfers(first: 20, orderBy: BLOCK_ID_DESC, filter: {or: ["
            '{senderId: {equalTo: "pokt1..."}}, {recipientId: {equalTo: "pokt1..."}}]}) '
            "{ totalCount nodes { id senderId recipientId amounts denom blockId transactionId } } }",
        ],
    ),
    "getNativeTransfersJson": QueryFieldInfo(
        name="getNativeTransfersJson",
        description="Native token transfers (MsgSend) of up to 200 addresses, newest first, each with its block time and its transaction's code, codespace, fees, signer and memo, in one call. direction: sent (the sender is one of addresses), received (the recipient is) or omitted for both. Optional time range. Use it instead of nativeTransfers when the answer needs the time, fees or code of the transaction.",
        fields_notes={
            "addresses": "Required: 1 to 200 pokt1... addresses.",
            **ROW_NOTES,
            "before...": "beforeHeight and beforeId: the height and id of the last row of the previous page; pass both or neither.",
        },
        examples=[
            "# Transfers a wallet sent or received, with their fees, code and time\n"
            'query { getNativeTransfersJson(addresses: ["pokt1..."], rowLimit: 20) }',
            "# Only what a wallet received in a time range\n"
            'query { getNativeTransfersJson(addresses: ["pokt1..."], direction: "received", '
            'rangeStart: "2026-10-01T00:00:00Z", rangeEnd: "2026-10-08T00:00:00Z") }',
            "# Next page: the height and id of the last row of the previous page\n"
            'query { getNativeTransfersJson(addresses: ["pokt1..."], rowLimit: 20, beforeHeight: "964073", '
            'beforeId: "964073-finalize_block-17563") }',
        ],
    ),
    "accounts": QueryFieldInfo(
        name="accounts",
        description='Queries account entities by address. Their balances are in "balances" (filter accountId), each with the height of the block that last updated it (lastUpdatedBlockId), which is useful for account-activity (recency) queries.',
        fields_notes={
            "id": 'The account address (must start with "pokt1").',
        },
        examples=[
            "# Single account (point lookup) with its balances and last activity\n"
            'query { account(id: "pokt1...") { id } balances(filter: {accountId: {equalTo: "pokt1..."}}, first: 10) '
            "{ nodes { amount denom lastUpdatedBlockId } } }",
        ],
    ),
    "applications": QueryFieldInfo(
        name="applications",
        description="Provides visibility on the network applications (entities that request and pay for relays): stake amount and status, staked services, gateway delegations, unstaking begin/end blocks and transfer state. Supports totalCount, e.g. filter {stakeStatus: {equalTo: Staked}} to count staked apps; their total stake: getApplicationFleetList.",
        fields_notes={
            "id": 'The address of the application (must start with "pokt1").',
            "stakeStatus": "One of: Staked, Unstaking, Unstaked.",
            "services": "Services the application is staked for: applicationServices filtered by applicationId.",
            "gateways": "Gateways this application has delegated to: applicationGateways filtered by applicationId.",
            "unstakingEndHeight": "Block at which unstaking completes (when unstaking).",
        },
        examples=[
            "# Staked applications: count (their total stake: getApplicationFleetList)\n"
            "query { applications(filter: {stakeStatus: {equalTo: Staked}}) { totalCount } }",
            "# Application detail by address (point lookup), with its services and gateways\n"
            'query { application(id: "pokt1...") { id stakeAmount stakeStatus unstakingEndHeight } '
            'applicationServices(filter: {applicationId: {equalTo: "pokt1..."}}, first: 100) { nodes { serviceId } } '
            'applicationGateways(filter: {applicationId: {equalTo: "pokt1..."}}, first: 100) { totalCount nodes { gatewayId } } }',
        ],
    ),
    "gateways": QueryFieldInfo(
        name="gateways",
        description="Provides visibility on the network gateways: stake amount and status, unstaking begin/end blocks and the applications delegated to them. Supports totalCount, e.g. filter {stakeStatus: {equalTo: Staked}}; for their total stake and their delegated applications use getGatewayFleetList.",
        fields_notes={
            "id": 'The address of the gateway (must start with "pokt1").',
            "stakeStatus": "One of: Staked, Unstaking, Unstaked.",
            "applications": "How many applications delegate to each gateway: getGatewayFleetList; which ones: applicationGateways filtered by gatewayId, or getGatewayDelegationsList.",
        },
        examples=[
            "# Staked gateways with their stake\n"
            "query { gateways(first: 20, filter: {stakeStatus: {equalTo: Staked}}) { totalCount "
            "nodes { id stakeAmount } } }",
        ],
    ),
    "validators": QueryFieldInfo(
        name="validators",
        description='Provides visibility on the consensus validators: stake amount and status, commission, min self delegation, description (moniker, website) and signer account. Use this for validator identity and stake queries; for the live bonded set prefer the RPC method "get_active_validators".',
        fields_notes={
            "id": 'Validator operator address (starts with "poktvaloper").',
            "signerId": 'Account address operating the validator (starts with "pokt1").',
            "commission": "Validator commission configuration.",
        },
        examples=[
            "# Validator list with stake, commission and identity\n"
            "query { validators(first: 20) { totalCount nodes { id signerId description commission "
            "minSelfDelegation stakeAmount stakeStatus } } }",
        ],
    ),
    "morseClaimableAccounts": QueryFieldInfo(
        name="morseClaimableAccounts",
        description="Tracks the Morse (v0) to Shannon migration accounts: claim status, destination Shannon address and the claimable balance/stake amounts, one account per row. For migration progress (claimed vs unclaimed counts and amounts) use getMorseClaimSummaryList. The unclaimed amounts represent un-migrated supply, which is relevant for total supply calculations.",
        fields_notes={
            "id": 'The Morse address (hex string, no "0x" prefix).',
            "claimed": "Whether the account was already claimed on Shannon.",
            "shannonDestAddress": 'Destination Shannon address (starts with "pokt1") once claimed.',
            "unstakedBalanceAmount": "Claimable liquid balance in upokt.",
            "supplierStakeAmount": "Claimable supplier stake in upokt.",
            "applicationStakeAmount": "Claimable application stake in upokt.",
        },
        examples=[
            "# Claimable-accounts list with claim status and destination (next page: after: <endCursor>)\n"
            "query { morseClaimableAccounts(first: 20, orderBy: [CLAIMED_DESC, CLAIMED_AT_ID_DESC, ID_ASC]) { totalCount "
            "pageInfo { hasNextPage endCursor } nodes { id shannonDestAddress claimed claimedAtHeight: claimedAtId transactionId "
            "unstakedBalanceAmount supplierStakeAmount applicationStakeAmount } } }",
        ],
    ),
    "getLatestBlocksByDay": QueryFieldInfo(
        name="getLatestBlocksByDay",
        description='Returns the latest block of each day in the provided date range, with the per-day snapshot fields of the block (staked validators/suppliers/apps/gateways and their tokens, supply, etc.). This is the cheapest way to build daily evolution series of staked actors; prefer it over paging the "blocks" table.',
        examples=[
            "# One block snapshot per day (daily evolution of staked actors and supply)\n"
            'query { getLatestBlocksByDay(startDate: "2026-06-01T00:00:00Z", endDate: "2026-06-11T00:00:00Z") }',
        ],
    ),
    "getSuppliersStakedAndBlocksByPointJson": QueryFieldInfo(
        name="getSuppliersStakedAndBlocksByPointJson",
        description='Returns a JSON time-series of suppliers staked per service (amount and tokens) truncated at the requested interval. Time-series counterpart of "getAmountOfBlocksAndSuppliersByTimes" (which only returns totals for the range).',
        examples=[
            "# Suppliers staked per service over time (JSON string result)\n"
            'query { getSuppliersStakedAndBlocksByPointJson(startTimestamp: "2026-06-01T00:00:00Z", '
            'endTimestamp: "2026-06-11T00:00:00Z", truncInterval: "day") }',
        ],
    ),
    "getProducedBlocksByValidator": QueryFieldInfo(
        name="getProducedBlocksByValidator",
        description='Returns the blocks produced by a validator since the given block id. Combine with "getMissingValidatorBlocks" to compute validator uptime.',
        fields_notes={
            "fromId": "Starting block id (a number).",
            "validatorAddress": 'The validator consensus address in hex (NOT the "poktvaloper..." bech32 form).',
        },
        examples=[
            "# Validator uptime: produced vs missed blocks since a height\n"
            'query { producedBlocks: getProducedBlocksByValidator(fromId: "790000", '
            'validatorAddress: "<VALIDATOR_HEX_ADDRESS>") '
            'missedBlocks: getMissingValidatorBlocks(fromId: "790000", '
            'validatorAddress: "<VALIDATOR_HEX_ADDRESS>") }',
        ],
    ),
    "getMissingValidatorBlocks": QueryFieldInfo(
        name="getMissingValidatorBlocks",
        description='Returns the block ids that the validator missed (did not sign) since the given block id. Combine with "getProducedBlocksByValidator" to compute validator uptime.',
        fields_notes={
            "fromId": "Starting block id (a number).",
            "validatorAddress": 'The validator consensus address in hex (NOT the "poktvaloper..." bech32 form).',
        },
        examples=[
            "# Block ids a validator missed since a height\n"
            'query { getMissingValidatorBlocks(fromId: "790000", validatorAddress: "<VALIDATOR_HEX_ADDRESS>") }',
        ],
    ),
    "supplierServiceConfigs": QueryFieldInfo(
        name="supplierServiceConfigs",
        description="Queries supplier-service configuration pairs: which suppliers are staked in which services, with rev-share, endpoints and activation block. Filter by supplierId to list the services of a supplier, or by serviceId to list the suppliers serving a service.",
        fields_notes={
            "supplierId": 'The address of the supplier (must start with "pokt1").',
            "serviceId": "The service identifier string.",
            "revShare": "Revenue share configuration of the supplier in this service.",
            "endpoints": "The endpoints (URLs/domains) the supplier exposes for this service.",
        },
        examples=[
            "# All service configs of a supplier (cursor-paginated), with rev-share and endpoints\n"
            'query { supplierServiceConfigs(filter: {supplierId: {equalTo: "pokt1..."}}, first: 100) '
            "{ pageInfo { hasNextPage endCursor } nodes { serviceId revShare endpoints activatedAtId } } }",
        ],
    ),
    "applicationGateways": QueryFieldInfo(
        name="applicationGateways",
        description="Queries application-gateway delegation pairs. Filter by applicationId to see which gateways an application delegated to, or by gatewayId to list the applications delegating to a gateway.",
        examples=[
            "# Gateways an application has delegated to\n"
            'query { applicationGateways(filter: {applicationId: {equalTo: "pokt1..."}}, first: 100) '
            "{ nodes { gatewayId } } }",
            "# The gateways of an application with their stake, in one request (every gateway's stake from getGatewayFleetList)\n"
            'query { applicationGateways(filter: {applicationId: {equalTo: "pokt1..."}}, first: 100) { nodes { gatewayId } } '
            "getGatewayFleetList { gatewayId stakeStatus stakeUpokt } }",
        ],
    ),
    "applicationServices": QueryFieldInfo(
        name="applicationServices",
        description="Queries application-service pairs: which applications are staked for which services. Filter by serviceId to list the applications using a service.",
        examples=[
            "# Applications staked for a given service\n"
            'query { applicationServices(filter: {serviceId: {equalTo: "eth"}}, first: 20) '
            "{ totalCount nodes { applicationId } } }",
        ],
    ),
    # --------------------------------------------------------------------------------------------------------------
    # Functions that replace the relations, aggregates and event tables the API no longer serves.
    # --------------------------------------------------------------------------------------------------------------
    "getSupplierFleetList": QueryFieldInfo(
        name="getSupplierFleetList",
        description="Suppliers now, one list at a time: the suppliers given, the suppliers of some owners, the suppliers that share revenue with some operators (rev_share addresses), or those with an endpoint on some domains; or every supplier when none is given. Kept to stakeStatuses (Staked, Unstaking, Unstaked) and services when given. bySupplier true (default) = one row per supplier (owner, status, stake, number of services); false = one row for the whole group: supplierCount and the summed stakeUpokt. Use it for counts and total stake of suppliers.",
        fields_notes={
            "suppliers/owners/operators/domains": "Up to 200 addresses or domains in one of these lists; give at most one. owners = the suppliers they own now; operators = the Staked suppliers whose service configs share revenue with those addresses now.",
            "total": "bySupplier: false = one row for the whole group (supplierCount, stakeUpokt) instead of one per supplier.",
            "maxStakeUpokt": "Keeps the suppliers whose stake is below it.",
            "serviceDomains": "With services: the endpoint root domains of each supplier's live configs for those services.",
            **NOW_NOTES,
            "paging": SUPPLIER_FLEET_PAGING,
        },
        examples=[
            "# Staked suppliers: count and total stake\n"
            'query { getSupplierFleetList(stakeStatuses: ["Staked"], bySupplier: false) { supplierCount stakeUpokt } }',
            "# Staked suppliers with an endpoint on some domains: count and total stake\n"
            'query { getSupplierFleetList(domains: ["example.com"], stakeStatuses: ["Staked"], bySupplier: false) '
            "{ supplierCount stakeUpokt } }",
            "# The suppliers of an owner, largest stake first, one page (next: afterStakeUpokt and afterSupplierId)\n"
            'query { getSupplierFleetList(owners: ["pokt1..."], rowLimit: 1000) { supplierId stakeStatus stakeUpokt '
            "serviceCount } }",
            "# Staked suppliers of a service with a stake below 100k POKT, with their domains for that service, one page\n"
            'query { getSupplierFleetList(services: ["eth"], stakeStatuses: ["Staked"], maxStakeUpokt: "100000000000", '
            "rowLimit: 100) { supplierId stakeUpokt serviceDomains } }",
            "# Next page: the last row's stakeUpokt and supplierId\n"
            'query { getSupplierFleetList(stakeStatuses: ["Staked"], rowLimit: 100, afterStakeUpokt: "60000000000", '
            'afterSupplierId: "pokt1...") { supplierId stakeUpokt serviceCount } }',
        ],
    ),
    "getStakedSuppliersList": QueryFieldInfo(
        name="getStakedSuppliersList",
        description="Suppliers now, one row each, ordered by supplier id: owner, operator, stake, and the service ids and endpoint root domains of their live service configs. stakeStatuses (Staked, Unstaking, Unstaked) picks the statuses (default Staked); a supplier that is not Staked shows the configs it had when it last closed them. Use it for a page of suppliers with their services, in any status. For a fleet by stake, with filters, use getSupplierFleetList.",
        fields_notes={
            "rowLimit": "Rows per call: default and most 1000. For the next page pass the last row's supplierId as afterSupplierId; a page shorter than rowLimit is the last one. Not first / offset.",
            "result": "Rows with camelCase fields to select: supplierId ownerId operatorId stakeUpokt serviceIds domains; amounts in upokt.",
        },
        examples=[
            "# Staked suppliers with their services and domains, first page\n"
            "query { getStakedSuppliersList(rowLimit: 100) { supplierId ownerId stakeUpokt serviceIds domains } }",
            "# Suppliers in any status with their services, next page after a supplier\n"
            'query { getStakedSuppliersList(stakeStatuses: ["Staked", "Unstaking", "Unstaked"], rowLimit: 100, '
            'afterSupplierId: "pokt1...") { supplierId stakeUpokt serviceIds } }',
        ],
    ),
    "getStakedDomainServicesList": QueryFieldInfo(
        name="getStakedDomainServicesList",
        description="The live service configs of the Staked suppliers counted per (endpoint root domain, service): configs = the configs whose endpoints are on that domain. totalConfigs and totalSuppliers (every live config of the Staked suppliers and their distinct suppliers) come on every row of the first page, to check that a read over all pages is complete. Use it for which domains serve which services and how many configs each has.",
        fields_notes={
            "rowLimit": "Rows per call, ordered by domain then serviceId: default and most 1000. For the next page pass the last row's domain and serviceId as afterDomain and afterServiceId; a page shorter than rowLimit is the last one.",
            "result": "Rows with camelCase fields to select: domain serviceId configs totalConfigs totalSuppliers.",
        },
        examples=[
            "# Configs per domain and service, first page, with the totals to check completeness\n"
            "query { getStakedDomainServicesList(rowLimit: 1000) { domain serviceId configs totalConfigs totalSuppliers } }",
        ],
    ),
    "getStakedSupplierServicesList": QueryFieldInfo(
        name="getStakedSupplierServicesList",
        description="The service configs of the Staked suppliers now, one row per (supplier, service): the supplier's owner and stake, the service, the endpoint root domains of that config and the height from which the chain serves it. services keeps those services (up to 200; omitted = all). Use it for the services of the staked suppliers, or the staked suppliers of some services with their stake and domains. Only Staked suppliers; for one supplier in any status use supplierServiceConfigs filtered by supplierId.",
        fields_notes={
            "rowLimit": "Rows per call, ordered by supplierId then serviceId: default and most 1000. For the next page pass the last row's supplierId and serviceId as afterSupplierId and afterServiceId; a page shorter than rowLimit is the last one.",
            "result": "Rows with camelCase fields to select: supplierId serviceId ownerId stakeUpokt domains activatedAtHeight; amounts in upokt.",
        },
        examples=[
            "# Staked suppliers of a service with their stake and endpoint domains\n"
            'query { getStakedSupplierServicesList(services: ["eth"], rowLimit: 1000) { supplierId ownerId stakeUpokt '
            "domains activatedAtHeight } }",
            "# Next page: the last row's supplierId and serviceId\n"
            'query { getStakedSupplierServicesList(services: ["eth"], rowLimit: 1000, afterSupplierId: "pokt1...", '
            'afterServiceId: "eth") { supplierId serviceId stakeUpokt } }',
        ],
    ),
    "getGatewayFleetList": QueryFieldInfo(
        name="getGatewayFleetList",
        description="Gateways now: the gateways given (up to 200; omitted = every gateway), kept to stakeStatuses. byGateway true (default) = one row per gateway: stake status, stake, the applications delegating to it now (delegatedApplications), and the delegate and undelegate messages it ever received (delegationsEver, undelegationsEver), largest stake first; false = one row for all (gatewayCount, the summed stakeUpokt). Use it for counts and total stake of gateways and for how many applications delegate to each.",
        fields_notes={
            "total": "byGateway: false = one row for all (gatewayCount, stakeUpokt) instead of one per gateway.",
            **NOW_NOTES,
        },
        examples=[
            "# Staked gateways: count and total stake\n"
            'query { getGatewayFleetList(stakeStatuses: ["Staked"], byGateway: false) { gatewayCount stakeUpokt } }',
            "# Staked gateways with their stake and the applications delegating to each\n"
            'query { getGatewayFleetList(stakeStatuses: ["Staked"]) { gatewayId stakeUpokt delegatedApplications } }',
        ],
    ),
    "getGatewayDelegationsList": QueryFieldInfo(
        name="getGatewayDelegationsList",
        description="Some gateways (up to 200) and the applications delegating to them at a height (atHeight omitted = now): each gateway's stake and status, and each delegated application's stake, status and service ids, as of that height. One row per (gateway, application); a gateway with no delegation has one row with null application columns.",
        fields_notes={
            "atHeight": "A block height (omitted = the last height the indexer processed); for a date, getHeightAt(atTime) gives the height. Every row carries the height it was read at (height): pass it as atHeight on the next pages, so the pages are one read.",
            "rowLimit": "Rows per call, ordered by gatewayId then applicationId. For the next page pass the last row's gatewayId and applicationId as afterGatewayId and afterApplicationId; a page shorter than rowLimit is the last one.",
            "result": "Rows with camelCase fields to select: gatewayId gatewayStakeUpokt gatewayStakeStatus applicationId stakeUpokt stakeStatus serviceIds height; amounts in upokt.",
        },
        examples=[
            "# The applications delegating to a gateway now, with their stake and services, and the height read\n"
            'query { getGatewayDelegationsList(gateways: ["pokt1..."], rowLimit: 100) { gatewayId applicationId stakeUpokt '
            "stakeStatus serviceIds height } }",
            "# Next page: the first page's height as atHeight, and the last row's gatewayId and applicationId\n"
            'query { getGatewayDelegationsList(gateways: ["pokt1..."], rowLimit: 100, atHeight: "950000", '
            'afterGatewayId: "pokt1...", afterApplicationId: "pokt1...") { gatewayId applicationId stakeUpokt height } }',
        ],
    ),
    "getActiveAccountsList": QueryFieldInfo(
        name="getActiveAccountsList",
        description="Accounts active since each of some times (up to 20): accounts = the upokt balances last updated in a block at or after the time (null when beyondTip: no indexed block that late yet), and height = the newest block before the time. Use it for how many accounts were active since a date, in one request.",
        fields_notes={
            "sinceTimes": "Times in UTC with an explicit zone, e.g. 2026-10-01T00:00:00Z.",
            "result": "Rows with camelCase fields to select: sinceTime height beyondTip accounts.",
        },
        examples=[
            "# Accounts active since a date, and since a week before it\n"
            'query { getActiveAccountsList(sinceTimes: ["2026-10-13T00:00:00Z", "2026-10-06T00:00:00Z"]) '
            "{ sinceTime height beyondTip accounts } }",
        ],
    ),
    "getBlockSuppliesList": QueryFieldInfo(
        name="getBlockSuppliesList",
        description="The supply at a block height: one row per denomination with its amount in that denomination's own base unit (upokt for upokt, mact for mact; not converted). No row for a height without recorded supply.",
        fields_notes={
            "height": "A block height; for a date, getHeightAt(atTime) gives it.",
            "result": "Rows with camelCase fields to select: denom amount.",
        },
        examples=[
            '# Supply per denomination at a block\nquery { getBlockSuppliesList(height: "964000") { denom amount } }',
        ],
    ),
    "getHeightAt": QueryFieldInfo(
        name="getHeightAt",
        description="The last indexed block at or before a time: its height and blockTime (null when no indexed block is that early). beyondTip is true when the time is after the newest indexed block. Use it to turn a date into a height for the lists and functions that take a height.",
        fields_notes={
            "atTime": "A time in UTC with an explicit zone, e.g. 2026-10-01T00:00:00Z.",
            "since a date": "The first block at or after a date is the returned height + 1, unless blockTime equals the date.",
        },
        examples=[
            "# Height of the last block before a date\n"
            'query { getHeightAt(atTime: "2026-10-13T00:00:00Z") { height blockTime beyondTip } }',
        ],
    ),
    "getApplicationFleetList": QueryFieldInfo(
        name="getApplicationFleetList",
        description="Applications now: the applications given (up to 200), kept to those staked for some services and to those delegating to some gateways, and to stakeStatuses; none given = every application. byApplication true (default) = one row per application (status, stake, number of services and gateways); false = one row for all: applicationCount and the summed stakeUpokt. Use it for counts and total stake of applications.",
        fields_notes={
            "total": "byApplication: false = one row for all (applicationCount, stakeUpokt) instead of one per application.",
            **NOW_NOTES,
        },
        examples=[
            "# Staked applications: count and total stake\n"
            'query { getApplicationFleetList(stakeStatuses: ["Staked"], byApplication: false) '
            "{ applicationCount stakeUpokt } }",
            "# Applications delegating to a gateway, with their stake\n"
            'query { getApplicationFleetList(gateways: ["pokt1..."]) { applicationId stakeStatus stakeUpokt } }',
        ],
    ),
    "getServiceSummaryList": QueryFieldInfo(
        name="getServiceSummaryList",
        description="Services now: per service, the suppliers configured for it (suppliers) and how many of them are Staked (stakedSuppliers), the applications staked for it (applications) and how many are Staked (stakedApplications), and its current relay mining difficulty from its newest update (numRelaysEma, targetHashHex, difficultyUpdatedHeight; null when it never had one). services = those (up to 200); omitted = every live service (zeros when it has no supplier or application now). owners (up to 200) keeps the services those addresses own now.",
        fields_notes=NOW_NOTES,
        examples=[
            "# Suppliers, applications and current difficulty per service\n"
            'query { getServiceSummaryList(services: ["eth", "base"]) { serviceId stakedSuppliers suppliers '
            "stakedApplications applications numRelaysEma difficultyUpdatedHeight } }",
            "# The services an address owns, with their suppliers and applications\n"
            'query { getServiceSummaryList(owners: ["pokt1..."]) { serviceId suppliers stakedSuppliers applications } }',
        ],
    ),
    "getRelayMiningDifficultyJson": QueryFieldInfo(
        name="getRelayMiningDifficultyJson",
        description="Relay mining difficulty history of some services (up to 200): each update with the relays EMA (exponential moving average of relays per session) and the target hash before and after it. The difficulty affects the ratio of claimed CUs to estimated CUs; a higher EMA means the service has a higher relay throughput. The current difficulty of every service: getServiceSummaryList.",
        fields_notes={
            **ROW_NOTES,
            "before...": "beforeHeight and beforeServiceId: the height and service_id of the last row of the previous page.",
        },
        examples=[
            '# Newest difficulty update of a service\nquery { getRelayMiningDifficultyJson(services: ["eth"], rowLimit: 1) }',
            "# Difficulty updates of some services in a time range\n"
            'query { getRelayMiningDifficultyJson(services: ["eth", "base"], rangeStart: "2026-06-01T00:00:00Z", '
            'rangeEnd: "2026-06-11T00:00:00Z", rowLimit: 1000) }',
        ],
    ),
    "getBlockStatsJson": QueryFieldInfo(
        name="getBlockStatsJson",
        description="Block statistics over a time range, as one total or per bucket: blocks and the first and last height, transactions (total, successful, failed), relays and compute units (claimed and estimated), the time between blocks in milliseconds (average, minimum, maximum) and the average block size in bytes. Use it for network totals and averages over many blocks, and for successful vs failed transaction counts.",
        fields_notes={
            "rangeStart/rangeEnd": "The range [start, end) in UTC with an explicit zone, e.g. 2026-10-01T00:00:00Z.",
            "bucket": CATALOG_NOTES["bucket"],
            "result": '{"range": {...}, "data": [rows]}: one row per bucket in "data" with snake_case keys, numbers as strings. A bucket outside covered_from / covered_to or in a gap is not covered yet, never 0.',
        },
        examples=[
            "# Network totals and averages over a time window (one row)\n"
            'query { getBlockStatsJson(rangeStart: "2026-06-10T00:00:00Z", rangeEnd: "2026-06-11T00:00:00Z") }',
            "# Successful vs failed transactions per day\n"
            'query { getBlockStatsJson(rangeStart: "2026-06-01T00:00:00Z", rangeEnd: "2026-06-11T00:00:00Z", '
            'bucket: "day") }',
        ],
    ),
    "getActorLifecycleJson": QueryFieldInfo(
        name="getActorLifecycleJson",
        description="Stake history of suppliers, applications or gateways, one row per event, newest first: stake and unstake messages (with the stake), unbonding begin and end (reason, unbonding end height), for suppliers each slash (service, application, session, penalty, stake before and after it, proof validation status), and for applications and gateways delegations to one another. Use it to track if and when an actor was staked, re-staked, unstaked or slashed.",
        fields_notes={
            "kind": "supplier, application or gateway (required).",
            "addresses/owners/operators": "addresses = those actors (up to 200); for suppliers also owners (the suppliers they own now) or operators = the Staked suppliers whose service configs share revenue with those addresses now, with all its events. Give one list.",
            "events": "Omit for all, or a list of: stake, unstake, unbonding_begin, unbonding_end, slash (suppliers), delegate, undelegate (applications and gateways).",
            **ROW_NOTES,
            "before...": "beforeHeight and beforeId: the height and id of the last row of the previous page.",
        },
        examples=[
            "# Recent slashes of a supplier, with penalty and before/after stake\n"
            'query { getActorLifecycleJson(kind: "supplier", addresses: ["pokt1..."], events: ["slash"], rowLimit: 20) }',
            "# Stake history (stake, unstake, unbonding) of an application\n"
            'query { getActorLifecycleJson(kind: "application", addresses: ["pokt1..."], '
            'events: ["stake", "unstake", "unbonding_begin", "unbonding_end"], rowLimit: 100) }',
            "# Next page of the slashes: the height and id of the last row of the previous page\n"
            'query { getActorLifecycleJson(kind: "supplier", addresses: ["pokt1..."], events: ["slash"], rowLimit: 20, '
            'beforeHeight: "964073", beforeId: "964073-finalize_block-17563") }',
        ],
    ),
    "getCreatedClaimsJson": QueryFieldInfo(
        name="getCreatedClaimsJson",
        description="Claims created (MsgCreateClaim) by up to 20 suppliers, newest first, whether or not they settled yet: application, service, session, session end height, relays, compute units and claimed upokt as claimed, and the transaction. fromSessionEndHeight keeps the claims of sessions that end at or after that height. For settled and expired claims use getClaimsJson.",
        fields_notes={
            **ROW_NOTES,
            "before...": "beforeHeight and beforeId: the height and id of the last row of the previous page.",
        },
        examples=[
            "# Claims a supplier created for sessions ending at or after a height\n"
            'query { getCreatedClaimsJson(suppliers: ["pokt1..."], fromSessionEndHeight: "964000", rowLimit: 100) }',
        ],
    ),
    "getCreatedClaimsCountList": QueryFieldInfo(
        name="getCreatedClaimsCountList",
        description="Claims created (MsgCreateClaim) counted per supplier, for up to 20 suppliers, in an optional time range: one row per supplier with claims (0 when none) and the first and last height counted. fromSessionEndHeight counts only the claims of sessions that end at or after that height.",
        fields_notes={
            "rangeStart/rangeEnd": "Optional range [start, end) in UTC with an explicit zone; omit both for the whole history.",
            "result": "Rows with camelCase fields to select: supplierId claims firstHeight lastHeight.",
        },
        examples=[
            "# How many claims some suppliers created for sessions ending at or after a height\n"
            'query { getCreatedClaimsCountList(suppliers: ["pokt1..."], fromSessionEndHeight: "964000") '
            "{ supplierId claims firstHeight lastHeight } }",
        ],
    ),
    "getClaimsJson": QueryFieldInfo(
        name="getClaimsJson",
        description="Claims one by one, newest first: settled ones with relays, estimated relays, claimed and estimated compute units, claimed and settled upokt, minted and overservicing amounts and whether a proof backed them; expired ones (no valid proof in time, a penalty for the supplier) with their expiration_reason. statuses = settled, expired (omitted = both). Filters, combined: suppliers (up to 200), owners or operators; applications and services (up to 200 each). For totals use getSupplierEarningsJson, getApplicationSpendJson, getServiceUsageJson or getSupplierPenaltiesJson.",
        fields_notes={
            "suppliers/owners/operators": "suppliers = those (up to 200); owners = the suppliers they own now; operators = the Staked suppliers whose service configs share revenue with those addresses now, with all its claims. Give at most one.",
            **ROW_NOTES,
            "before...": "beforeHeight and beforeEventIdx: the height and event_idx of the last row of the previous page.",
            "owner": "supplier_owner_id is the owner the settlement recorded (null for expired claims); current_owner_id is the supplier's owner now.",
            "coverage": COVERAGE_NOTE,
        },
        examples=[
            "# Newest claims of a supplier in a time range\n"
            'query { getClaimsJson(suppliers: ["pokt1..."], rangeStart: "2026-06-10T00:00:00Z", '
            'rangeEnd: "2026-06-11T00:00:00Z", rowLimit: 100) }',
            "# Expired claims of a supplier\n"
            'query { getClaimsJson(suppliers: ["pokt1..."], statuses: ["expired"], rowLimit: 100) }',
            "# Next page: the height and event_idx of the last row of the previous page\n"
            'query { getClaimsJson(suppliers: ["pokt1..."], statuses: ["expired"], rowLimit: 100, '
            'beforeHeight: "964073", beforeEventIdx: 17563) }',
        ],
    ),
    "getMorseClaimSummaryList": QueryFieldInfo(
        name="getMorseClaimSummaryList",
        description="Morse (v0) to Shannon migration now: one row for the claimed Morse accounts and one for the unclaimed, each with the number of accounts and the sums of their liquid balance and of the supplier and application stakes they carry. The unclaimed amounts are supply not yet migrated from Morse to Shannon.",
        fields_notes=NOW_NOTES,
        examples=[
            "# Migration progress: claimed vs unclaimed totals\n"
            "query { getMorseClaimSummaryList { claimed accounts unstakedBalanceUpokt supplierStakeUpokt "
            "applicationStakeUpokt } }",
        ],
    ),
    # --------------------------------------------------------------------------------------------------------------
    # Settlement catalog: precomputed settlement (money) tables. See CATALOG_NOTES for the rules every one follows.
    # --------------------------------------------------------------------------------------------------------------
    "getIncomeJson": QueryFieldInfo(
        name="getIncomeJson",
        description="Income received by any address (supplier rev_share/shareholder addresses, service owners, the DAO, validators, delegators) in a time range: one row per role, optionally split by the supplier that generated it, by service, by reason (relay / global mint) and by address. Use it for every 'how much did these addresses earn' question: totals, per service, per supplier, per day.",
        fields_notes={
            **CATALOG_NOTES,
            "addresses": "Required: 1 to 200 receiving addresses.",
            "byAddress": "true (default) = one row per address; false = one total for the whole list.",
            "bySupplier/byService/byReason": "Split by the supplier that generated the income, by service, or by reason (family column: relay / global).",
            "amount_upokt": "upokt received. There is one row per role (e.g. rev_share, source_owner): add the rows for the address total.",
        },
        examples=[
            "# Total income of a group of addresses in a week (add the rows: one per role)\n"
            'query { getIncomeJson(addresses: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", byAddress: false) }',
            "# Income per service and per day\n"
            'query { getIncomeJson(addresses: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", bucket: "day", byService: true) }',
            "# Finest grain: per hour, per supplier and per reason (hour allows up to 7 days)\n"
            'query { getIncomeJson(addresses: ["pokt1..."], rangeStart: "2026-10-04T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", bucket: "hour", bySupplier: true, byReason: true) }',
        ],
    ),
    "legacyRewardsByAddressesAndTimeGroupByService": QueryFieldInfo(
        name="legacyRewardsByAddressesAndTimeGroupByService",
        description="Rewards received by a group of addresses, split by service, with the gross rewards, relays and compute units of the claims that paid them. Same arguments and JSON as the former getRewardsByAddressesAndTimeGroupByService, read from the settlement tables; gross_rewards, relays and compute units count each claim once (the former function counted a claim once per transfer). For net income only, getIncomeJson(byService: true) is the same number.",
        fields_notes={
            **LEGACY_NOTES,
            "addresses": 'A vector of the rev-share addresses (the ones receiving tokens) to query: ["pokt1....", "pokt1...."].',
            "net_rewards": "upokt the addresses received from the service.",
            "gross_rewards": "upokt claimed by the claims that paid the addresses.",
        },
        examples=[
            "# Rewards of a group of rev-share addresses broken down by service\n"
            'query { legacyRewardsByAddressesAndTimeGroupByService(addresses: ["pokt1..."], '
            'startTs: "2026-09-28T00:00:00Z", endTs: "2026-10-05T00:00:00Z") }',
        ],
    ),
    "legacyRewardsBySuppliersAndTimeGroupByAddressAndDate": QueryFieldInfo(
        name="legacyRewardsBySuppliersAndTimeGroupByAddressAndDate",
        description="Rewards received by a group of addresses from a specific group of suppliers, per address and per interval. Same arguments and JSON as the former getRewardsBySuppliersAndTimeGroupByAddressAndDate, read from the settlement tables.",
        fields_notes={
            **LEGACY_NOTES,
            "addresses": 'A vector of addresses to query: ["pokt1....", "pokt1...."]. These should be output addresses of the suppliers, i.e. their rev-share addresses (the ones receiving tokens from those suppliers).',
            "supplierAddresses": 'A vector of addresses to query: ["pokt1....", "pokt1...."]. These are the suppliers that are providing rewards to the "addresses".',
            "truncInterval": 'Interval of each point: "hour", "day", "week", "month" (any range length).',
        },
        examples=[
            "# Daily rewards an address received from given suppliers\n"
            'query { legacyRewardsBySuppliersAndTimeGroupByAddressAndDate(addresses: ["pokt1..."], '
            'supplierAddresses: ["pokt1..."], startDate: "2026-09-28T00:00:00Z", endDate: "2026-10-05T00:00:00Z", '
            'truncInterval: "day") }',
        ],
    ),
    "getSupplierEarningsJson": QueryFieldInfo(
        name="getSupplierEarningsJson",
        description="What suppliers earned: claimed and settled upokt, overservicing loss, relays, estimated relays, compute units and settled claims (with and without a proof), optionally per service, per application and per supplier. Use it for a supplier's or an operator's performance per service.",
        fields_notes={
            **CATALOG_NOTES,
            "suppliers": "Supplier (operator) addresses, up to 200; omit both suppliers and owners for every supplier (then use bySupplier: false).",
            "owners": "Owner addresses in place of suppliers: the suppliers they own now.",
            "services": "Up to 200 services: only the claims of those services.",
            "byService/byApplication/bySupplier": "Split by service, by application, or per supplier (bySupplier defaults to true when suppliers are given). With byApplication, gateway_ids = the application's gateways now.",
            "claimed_upokt": "upokt the suppliers claimed; settled_upokt is what was paid after overservicing.",
            "operators": "Addresses that share revenue with suppliers (delegators): the Staked suppliers whose service configs list them in rev_share now. One address per call for one row per address.",
        },
        examples=[
            "# The earnings of the suppliers a delegator address shares revenue with, in a week\n"
            'query { getSupplierEarningsJson(operators: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", bySupplier: false) }',
            "# A supplier's earnings per service in a week\n"
            'query { getSupplierEarningsJson(suppliers: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", byService: true, bySupplier: false) }',
            "# All suppliers of an owner, per day\n"
            'query { getSupplierEarningsJson(owners: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", bucket: "day", bySupplier: false) }',
            "# What every supplier earned on one service in a day\n"
            'query { getSupplierEarningsJson(services: ["eth"], rangeStart: "2026-10-04T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", bySupplier: false) }',
        ],
    ),
    "getSupplierProofsJson": QueryFieldInfo(
        name="getSupplierProofsJson",
        description="Proof activity of suppliers: claims settled with and without a proof, proofs submitted, validated and invalid (by reason), each counted in the block of its own event. Omit suppliers (and use bySupplier: false) for the whole network.",
        fields_notes={
            **CATALOG_NOTES,
            "suppliers": "Supplier addresses, up to 200; omit for every supplier.",
            "owners": "Owner addresses in place of suppliers.",
            "operators": "Addresses that share revenue with suppliers (delegators), as in getSupplierEarningsJson.",
        },
        examples=[
            "# Network-wide proofs per day\n"
            'query { getSupplierProofsJson(rangeStart: "2026-09-28T00:00:00Z", rangeEnd: "2026-10-05T00:00:00Z", '
            'bucket: "day", bySupplier: false) }',
        ],
    ),
    "getSupplierPenaltiesJson": QueryFieldInfo(
        name="getSupplierPenaltiesJson",
        description="Penalties of suppliers: expired claims (by reason), discarded claims and slashes, with their amounts.",
        fields_notes={
            **CATALOG_NOTES,
            "suppliers": "Supplier addresses, up to 200.",
            "owners": "Owner addresses in place of suppliers.",
            "operators": "Addresses that share revenue with suppliers (delegators), as in getSupplierEarningsJson; kind slashed holds their slashes.",
        },
        examples=[
            "# Expired claims, discards and slashes of a supplier, per service\n"
            'query { getSupplierPenaltiesJson(suppliers: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", byService: true) }',
        ],
    ),
    "getSupplierDistributionJson": QueryFieldInfo(
        name="getSupplierDistributionJson",
        description="How what a supplier generated was paid out: to each rev_share/shareholder address, the DAO, the service owner, and the stakers in one row, optionally by reason (relay / global mint).",
        fields_notes={
            **CATALOG_NOTES,
            "suppliers": "Required unless owners is given: supplier addresses, up to 200.",
            "owners": "Owner addresses in place of suppliers.",
        },
        examples=[
            "# Payout split of a supplier in a week\n"
            'query { getSupplierDistributionJson(suppliers: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z") }',
        ],
    ),
    "getApplicationSpendJson": QueryFieldInfo(
        name="getApplicationSpendJson",
        description="What applications paid (burned_upokt), the overserviced work they did not pay, reimbursements, relays, compute units and claims, optionally per service, per supplier and per application.",
        fields_notes={
            **CATALOG_NOTES,
            "applications": "Required: 1 to 200 application addresses.",
        },
        examples=[
            "# An application's spend per service in a week\n"
            'query { getApplicationSpendJson(applications: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", byService: true) }',
        ],
    ),
    "getGatewaySpendJson": QueryFieldInfo(
        name="getGatewaySpendJson",
        description="What the applications delegated to each gateway spent, by the delegation in force at each settlement, optionally per application and per service.",
        fields_notes={
            **CATALOG_NOTES,
            "gateways": "Required: 1 to 200 gateway addresses.",
        },
        examples=[
            "# A gateway's delegated spend per day\n"
            'query { getGatewaySpendJson(gateways: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", bucket: "day") }',
        ],
    ),
    "getServiceUsageJson": QueryFieldInfo(
        name="getServiceUsageJson",
        description="Network traffic and value per service from settled claims: claimed and settled upokt, overservicing loss, relays, estimated relays, compute units, estimated compute units and claims. Pass the services, or topBySettled for the N services that settled the most. Use it for 'which services are most used / most profitable' questions.",
        fields_notes={
            **CATALOG_NOTES,
            "services": 'Service ids, e.g. ["eth", "base"].',
            "topBySettled": "1 to 200: the N services that settled the most upokt (rank_by_settled). Pass services or topBySettled (or both).",
            "settled_upokt": "upokt paid for the service after overservicing; claimed_upokt is what suppliers claimed.",
        },
        examples=[
            "# Every service in two consecutive weeks, to compare them (growth or decline): the same call twice in one query\n"
            'query { current: getServiceUsageJson(topBySettled: 200, rangeStart: "2026-10-05T00:00:00Z", rangeEnd: "2026-10-12T00:00:00Z") '
            'previous: getServiceUsageJson(topBySettled: 200, rangeStart: "2026-09-28T00:00:00Z", rangeEnd: "2026-10-05T00:00:00Z") }',
            "# Top 20 services by settled value yesterday\n"
            'query { getServiceUsageJson(rangeStart: "2026-10-04T00:00:00Z", rangeEnd: "2026-10-05T00:00:00Z", '
            "topBySettled: 20) }",
            "# Daily traffic of given services\n"
            'query { getServiceUsageJson(services: ["eth", "base"], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", bucket: "day") }',
        ],
    ),
    "getSupplyFlowsJson": QueryFieldInfo(
        name="getSupplyFlowsJson",
        description="Network supply flows from settlement: burn, relay mint (mint_equals_burn), mint_ratio_unminted, overservicing loss, global mint and reimbursement, optionally by receiving role, plus slashes. Use it for burn and mint questions.",
        fields_notes={
            **CATALOG_NOTES,
            "flow": "burn = upokt burned from applications; mint_equals_burn = upokt minted for relays; global_mint = global inflation mint; reimbursement = upokt the applications are reimbursed.",
            "byRole": "Split each flow by the role that received it.",
        },
        examples=[
            "# Burn and mint flows yesterday\n"
            'query { getSupplyFlowsJson(rangeStart: "2026-10-04T00:00:00Z", rangeEnd: "2026-10-05T00:00:00Z") }',
        ],
    ),
    "legacyMintBreakdownBetweenDates": QueryFieldInfo(
        name="legacyMintBreakdownBetweenDates",
        description="Provides information on the minting, inflation, reinbourcements, etc. for the whole network in a period. These correspond to the Token Logic Modules implemented in the network. Same arguments and JSON as the former getMintBreakdownBetweenDates, read from the settlement tables.",
        fields_notes={
            **LEGACY_NOTES,
            "reimbursement": "Total upokt that suppliers claim for reinbourcement. This is used to offset the network global inflation.",
            "inflation": "Total upokt minted on top of the min/burn due to the network global inflation parameter.",
            "mint_burn": 'Total upokt requested to be minted in exchange for service. Note that the total amount burned is not this one, it depends on the network "mint_ratio" parameter.',
        },
        examples=[
            "# Mint breakdown of a day\n"
            'query { legacyMintBreakdownBetweenDates(startDate: "2026-10-04T00:00:00Z", '
            'endDate: "2026-10-05T00:00:00Z") }',
        ],
    ),
    "getValidatorRewardsJson": QueryFieldInfo(
        name="getValidatorRewardsJson",
        description="Validator (consensus) rewards: commission, self-delegation reward, what went to delegators, the number of distributions, and the delegated stake seen per validator (average, minimum, maximum; for an APR).",
        fields_notes={
            **CATALOG_NOTES,
            "validators": 'Validator operator addresses ("poktvaloper..."), up to 200; omit for every validator.',
            "byValidator": "One row per validator; false = one total (the stake columns are then NULL).",
        },
        examples=[
            "# Rewards of every validator in a week\n"
            'query { getValidatorRewardsJson(rangeStart: "2026-09-28T00:00:00Z", rangeEnd: "2026-10-05T00:00:00Z") }',
        ],
    ),
    "getDelegatorIncomeJson": QueryFieldInfo(
        name="getDelegatorIncomeJson",
        description="Income of validator (consensus) delegators: what they received, optionally per validator. Not supplier rev_share 'delegators' (use getIncomeJson for those).",
        fields_notes={
            **CATALOG_NOTES,
            "delegators": "Delegator addresses (pokt1...), up to 200; omit for every delegator.",
            "validators": "Keep only the income from these validators (poktvaloper...).",
        },
        examples=[
            "# What a delegator received per validator in a week\n"
            'query { getDelegatorIncomeJson(delegators: ["pokt1..."], rangeStart: "2026-09-28T00:00:00Z", '
            'rangeEnd: "2026-10-05T00:00:00Z", byValidator: true) }',
        ],
    ),
    "getAppAutoUnstakesJson": QueryFieldInfo(
        name="getAppAutoUnstakesJson",
        description="Applications the chain unstaked because their stake fell below the minimum. Reads the indexed events, so it is not limited by the settlement coverage.",
        fields_notes={
            "rangeStart/rangeEnd": CATALOG_NOTES["rangeStart/rangeEnd"],
            "bucket": CATALOG_NOTES["bucket"],
            "applications": "Application addresses, up to 200; omit for every application.",
        },
        examples=[
            "# Auto-unstaked applications per week in a quarter\n"
            'query { getAppAutoUnstakesJson(rangeStart: "2026-07-01T00:00:00Z", rangeEnd: "2026-10-01T00:00:00Z", '
            'bucket: "week", byApplication: false) }',
        ],
    ),
    "getParamHistoryJson": QueryFieldInfo(
        name="getParamHistoryJson",
        description="Governance parameter history: each version of a parameter whose value differs from the previous one, with previous_value, its height and block time. Reads the indexed params, so it is not limited by the settlement coverage.",
        fields_notes={
            "rangeStart/rangeEnd": CATALOG_NOTES["rangeStart/rangeEnd"],
            "namespaces/keys": 'Filter by module namespace (e.g. ["tokenomics"]) and/or key; omit for all.',
        },
        examples=[
            "# Every change of the tokenomics params in a year\n"
            'query { getParamHistoryJson(namespaces: ["tokenomics"], rangeStart: "2025-10-01T00:00:00Z", '
            'rangeEnd: "2026-10-01T00:00:00Z") }',
        ],
    ),
    "moneyCoverageJson": QueryFieldInfo(
        name="moneyCoverageJson",
        description="Which part of a time range the settlement catalog covers: the first and last settlement heights written in the range, the settlement heights missing, and the recorded gaps. Use it to tell the user from when data is available.",
        fields_notes={
            "rangeStart/rangeEnd": CATALOG_NOTES["rangeStart/rangeEnd"],
            "gaps": "Height ranges with no settlement data written yet (not covered).",
        },
        examples=[
            "# Coverage of a month\n"
            'query { moneyCoverageJson(rangeStart: "2026-09-01T00:00:00Z", rangeEnd: "2026-10-01T00:00:00Z") }',
        ],
    ),
}
