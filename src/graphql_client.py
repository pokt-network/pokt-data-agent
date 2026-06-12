"""GraphQL API client for Pocket Network."""

import json
import os
from typing import Any, Tuple

import requests

from src.models import QueryFieldInfo

POCKET_NETWORK_DATA_ENDPOINT = os.getenv("POCKET_NETWORK_DATA_ENDPOINT", None)
if POCKET_NETWORK_DATA_ENDPOINT is None:
    raise ValueError('The "POCKET_NETWORK_DATA_ENDPOINT" enviroment variable is not set.')


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
                error_msg = "GraphQL errors: " + "; ".join([str(err) for err in data["errors"]])
                return False, None, error_msg

            if "data" in data:
                return True, data["data"], None
            else:
                return False, None, "No data returned from API"

        except requests.exceptions.Timeout:
            return False, None, "API request timeout (30s)"
        except requests.exceptions.ConnectionError:
            return False, None, "Connection error to API. Please check the endpoint."
        except requests.exceptions.HTTPError as e:
            return (
                False,
                None,
                f"HTTP error: {e.response.status_code} - {e.response.text}",
            )
        except requests.exceptions.RequestException as e:
            return False, None, f"Request error: {str(e)}"
        except json.JSONDecodeError as e:
            return False, None, f"Invalid JSON response from API: {str(e)}"
        except Exception as e:
            return False, None, f"Unexpected error: {str(e)}"


# Registry of available GraphQL query fields with descriptions
GRAPHQL_REGISTRY = {
    "balances": QueryFieldInfo(
        name="balances",
        description="Queries account balances, not tied to any specific actor (supplier, application, etc.).",
        examples=[
            "# Top accounts by upokt balance (rich list), with last-activity block\n"
            'query { balances(first: 20, orderBy: AMOUNT_DESC, filter: {denom: {equalTo: "upokt"}}) '
            "{ totalCount nodes { accountId amount denom lastUpdatedBlock { height: id timestamp } } } }",
            "# Count accounts active (balance updated) since a date\n"
            'query { balances(filter: {lastUpdatedBlock: {timestamp: {greaterThanOrEqualTo: "2026-06-01T00:00:00Z"}}, '
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
    "eventClaimSettleds": QueryFieldInfo(
        name="eventClaimSettleds",
        description="Returns claims settlement events with relays, computed units, and claimed tokens data",
        fields_notes={
            "block": "The block id, a number representing the block where this data was produced.",
            "supplierId": 'The address of a supplier entity (must start with "pokt1"). This entity serves work requests (relays).',
            "applicationId": 'The address of an application entity (must start with "pokt1"). This entity requests work (relays) from suppliers.',
            "numRelays": "Number of relays performed.",
            "numClaimedComputedUnits": "Number of CUs claimed by the supplier",
            "numEstimatedComputedUnits": "Network estimation of real CUs done by the supplier (after relay mining difficulty calculation)",
            "claimedAmount": "Total upokt requested by the supplier that should be burnt from the application",
        },
    ),
    "eventClaimExpireds": QueryFieldInfo(
        name="eventClaimExpireds",
        description="Returns expired claim events, these are claims that received no proof after the proof window closed. They are result in a penalty (burn) for the supplier that placed the claim.",
        fields_notes={
            "block": "The block id, a number representing the block where this data was produced.",
            "supplierId": 'The address of a supplier entity (must start with "pokt1"). This entity serves work requests (relays).',
            "applicationId": 'The address of an application entity (must start with "pokt1"). This entity requests work (relays) from suppliers.',
            "numRelays": "Number of relays performed.",
            "numClaimedComputedUnits": "Number of CUs claimed by the supplier",
            "numEstimatedComputedUnits": "Network estimation of real CUs done by the supplier (after relay mining difficulty calculation)",
            "claimedAmount": "Total upokt requested by the supplier that should be burnt from the application",
        },
    ),
    "relayByBlockAndServices": QueryFieldInfo(
        name="relayByBlockAndServices",
        description="Query relay data grouped by services with aggregates (claimedUpokt, computedUnits, relays). Usefull to explore the traffic on services at anetwork level.",
        fields_notes={
            "relays": "Number of relays performed.",
            "computedUnits": "Number of CUs claimed by suppliers",
            "claimedUpokt": "Total upokt requested by suppliers.",
        },
        examples=[
            "# Per-service traffic totals in a time window (grouped aggregates)\n"
            "query { relayByBlockAndServices(filter: {block: {timestamp: "
            '{greaterThanOrEqualTo: "2026-06-10T00:00:00Z", lessThan: "2026-06-11T00:00:00Z"}}}) '
            "{ groupedAggregates(groupBy: SERVICE_ID) { keys sum { relays estimatedRelays computedUnits "
            "estimatedComputedUnits claimedUpokt } } } }",
        ],
    ),
    "getMintBreakdownBetweenDates": QueryFieldInfo(
        name="getMintBreakdownBetweenDates",
        description="Provides information on the minting, inflation, reinbourcements, etc. for the whole network in a period. These correspond to the Token Logic Modules implemented in the network.",
        fields_notes={
            "reimbursement": "Total upokt that suppliers claim for reinbourcement. This is used to offset the network global inflation.",
            "inflation": "Total upokt minted on top of the min/burn due to the network global inflation parameter.",
            "mint_burn": 'Total upokt requested to be minted in exchange for service. Note that the total amount burned is not this one, it depends on the network "mint_ratio" parameter.',
        },
    ),
    "getBurnBreakdownBetweenDates": QueryFieldInfo(
        name="getBurnBreakdownBetweenDates",
        description="Provides information on the burning for the whole network in a period.",
        fields_notes={
            "burn_mint": 'Total upokt to be burned in exchange for service. Note that the total amount minted is not this one, it depends on the network "mint_ratio" parameter.'
        },
    ),
    "modToAcctTransfers": QueryFieldInfo(
        name="modToAcctTransfers",
        description='Tracks the rewards payed out by the network. This will provide exact earnings of an account (recipient) with maximum granularity, use this when other queries do not provide enough granularity or you need data for an specific block (otherwise look for "getRewardsByAddress..." methods).',
        fields_notes={
            "amount": "Total upokt transfered.",
            "recipient": "Entity receiving the tokens, a pokt address.",
        },
    ),
    "msgStakeApplications": QueryFieldInfo(
        name="msgStakeApplications",
        description="Provides data for Applications staking events, these are usefull to track if/when an app was staked and also to see when it was loaded with more tokens for requesting traffic (stake tokens are burned due to traffic so they must re-stake from time to time).",
        fields_notes={"stakeAmount": "Total upokt staked/re-staked by the application."},
    ),
    "msgStakeGateways": QueryFieldInfo(
        name="msgStakeGateways",
        description="Provides data for Gateways staking events, these are usefull to track if/when a gateway was staked.",
        fields_notes={"stakeAmount": "Total upokt staked/re-staked by the gateway."},
    ),
    "msgStakeSupplierServices": QueryFieldInfo(
        name="msgStakeSupplierServices",
        description="Provides data for Suppliers staking events with service granularity, these are usefull to track if/when a supplier was staked and in which services. Also to track changes in the staked services. Note that this method wont track unstaking, so accumulatingthis wont provide data on total staked suppliers per service.",
    ),
    "eventRelayMiningDifficultyUpdateds": QueryFieldInfo(
        name="eventRelayMiningDifficultyUpdateds",
        description="Tracks events that update the difficulty of a service in the Pocket Network. The difficulty affects the ratio of claumed CUs to estimated CUS and is the base of the relay mining algorithm. Higher values here means that the service has a higher throughput in relays over time.",
        fields_notes={
            "serviceId": "Service name.",
            "newNumRelaysEma": "Exponential moving average value of relays per session (new value).",
            "prevNumRelaysEma": "Exponential moving average value of relays per session (old value).",
        },
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
            "{ nodes { namespace key value block { height: id } } } }",
            "# Latest values of specific params in a namespace\n"
            'query { params(filter: {namespace: {equalTo: "shared"}, key: {in: ["claim_window_open_offset_blocks", '
            '"claim_window_close_offset_blocks", "proof_window_close_offset_blocks"]}}, orderBy: [BLOCK_ID_DESC], '
            "distinct: [NAMESPACE, KEY]) { nodes { key value blockId } } }",
        ],
    ),
    "blocks": QueryFieldInfo(
        name="blocks",
        description="Returns data of the blockchain blocks. Use this when you need per-block data on network wide metrics or you are tracking specific block metrics (like block generation time, timestamp, size, etc.)",
        fields_notes={
            "timeToBlock": "Time spent generating the block in milliseconds.",
            "size": "Block size in bytes.",
            "hash": "Block hash; use filter {hash: {equalTo: ...}} to look up a block by hash.",
        },
        examples=[
            "# Latest block with network-wide snapshot fields (staked actors, supply, relays)\n"
            "query { blocks(orderBy: ID_DESC, first: 1) { nodes { height: id hash timestamp totalTxs totalRelays "
            "totalComputedUnits stakedValidators stakedSuppliers stakedSuppliersTokens stakedApps stakedAppsTokens "
            "stakedGateways timeToBlock size supplies { nodes { supply { denom amount } } } } } }",
            "# Network totals and averages over a time window (aggregates over blocks)\n"
            'query { blocks(filter: {timestamp: {greaterThanOrEqualTo: "2026-06-10T00:00:00Z", '
            'lessThanOrEqualTo: "2026-06-11T00:00:00Z"}}) { aggregates { sum { totalRelays totalEstimatedRelays '
            "totalComputedUnits totalEstimatedComputedUnits } average { timeToBlock size } } } }",
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
            "supplierServiceConfigs": "Contains data about the servicers in this service.",
        },
        examples=[
            "# Service list with app/supplier counts and latest relay-mining difficulty\n"
            "query { services(first: 20) { totalCount nodes { id name computeUnitsPerRelay ownerId "
            "applicationServices { totalCount } supplierServiceConfigs { totalCount } "
            "relayMiningDifficultyUpdatedEvents(orderBy: BLOCK_ID_DESC, first: 1) { nodes { newNumRelaysEma } } } } }",
            "# Fuzzy search services by id or name\n"
            'query { services(filter: {or: [{id: {includesInsensitive: "eth"}}, '
            '{name: {includesInsensitive: "eth"}}]}) { nodes { id name } } }',
        ],
    ),
    "suppliers": QueryFieldInfo(
        name="suppliers",
        description="Provides visibility on the network suppliers, their owners, stake status (and block of stake/unstake), stake amount, etc.",
        fields_notes={
            "operatorId": "The address of the supplier.",
            "ownerId": "The address of the owner of the stake.",
            "serviceConfigs": "Contains information about the staked services in this supplier.",
        },
        examples=[
            "# Staked suppliers: count and total stake\n"
            "query { suppliers(filter: {stakeStatus: {equalTo: Staked}}) { totalCount "
            "aggregates { sum { stakeAmount } } } }",
            "# Paginated supplier list with owner/operator and staked services\n"
            "query { suppliers(first: 20, offset: 0, orderBy: [STAKE_STATUS_ASC]) { totalCount nodes { id ownerId "
            "operatorId stakeAmount stakeStatus serviceConfigs(first: 5) { totalCount nodes { serviceId } } } } }",
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
            "truncInterval": 'Set to "day" if the user is asking for current supply and use current date and day before for the date limits.',
        },
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
    "getClaimProofsDataByDelegatorsAndTime": QueryFieldInfo(
        name="getClaimProofsDataByDelegatorsAndTime",
        description='Fast method for retrieving rewards received by an address or group of them in the given time with granularity, result returned per-address. The "delegators" in the name are supplier rev_share addresses: addresses configured in a supplier\'s rev_share that receive a share of its reward tokens (NOT validator/consensus delegators, nor owners of the stake).',
        fields_notes={
            "addresses": 'A vector of supplier rev_share addresses to query: ["pokt1....", "pokt1...."]. These are the addresses receiving reward tokens from suppliers.',
        },
        examples=[
            "# Claims vs proofs time-series for specific supplier rev_share addresses\n"
            'query { getClaimProofsDataByDelegatorsAndTime(addresses: ["pokt1..."], '
            'startTs: "2026-06-01T00:00:00Z", endTs: "2026-06-11T00:00:00Z", truncInterval: "day") }',
        ],
    ),
    "getRewardsByAddressesAndTimeGroupByService": QueryFieldInfo(
        name="getRewardsByAddressesAndTimeGroupByService",
        description="Fast method for retrieving rewards received by an address or group of them, dividing the total rewards per service. Usefull for tracking which service is providing most gains.",
        fields_notes={
            "addresses": 'A vector of the rev-share addresses (the ones receiving tokens) to query: ["pokt1....", "pokt1...."].',
        },
        examples=[
            "# Rewards of a group of rev-share addresses (the ones receiving tokens) broken down by service\n"
            'query { getRewardsByAddressesAndTimeGroupByService(addresses: ["pokt1..."], '
            'startTs: "2026-06-01T00:00:00Z", endTs: "2026-06-11T00:00:00Z") }',
        ],
    ),
    "getRewardsBySuppliersAndTimeGroupByAddressAndDate": QueryFieldInfo(
        name="getRewardsBySuppliersAndTimeGroupByAddressAndDate",
        description="Fast method for retrieving rewards received by an address or group of them, from an specific group of suppliers, in the given time with granularity. Usefull for tracking the amount of rewards that an output address received from the selected suppliers.",
        fields_notes={
            "addresses": 'A vector of addresses to query: ["pokt1....", "pokt1...."]. These should be output addresses of the suppliers, i.e. their rev-share addresses (the ones receiving tokens from those suppliers).',
            "supplierAddresses": 'A vector of addresses to query: ["pokt1....", "pokt1...."]. These are the suppliers that are providing rewards to the "addresses".',
        },
    ),
    "getRewardsBySuppliersAndTimeGroupByService": QueryFieldInfo(
        name="getRewardsBySuppliersAndTimeGroupByService",
        description="Fast method for retrieving rewards received by a group of suppliers, dividing the total rewards per service. Usefull for tracking which service is providing most gains to a supplier (track service performance for operator).",
        fields_notes={
            "operatorAddresses": 'A vector of addresses to query: ["pokt1....", "pokt1...."]. These are the suppliers that are providing rewards. They are the address that own the suppliers, not the output addresses.',
        },
    ),
    "getRewardsByDomainsAndTimeGroupByService": QueryFieldInfo(
        name="getRewardsByDomainsAndTimeGroupByService",
        description="Returns the total number of suppliers, rewards (CUs, upokt, relays) stake for a given domain name in each of their staked services.",
        fields_notes={
            "domains": 'A vector of domains to query: ["foo.bar", "example.com", ...]. extracted normally from the staked endpoint data of a supplier.',
        },
    ),
    "getSupplierStatsByDomains": QueryFieldInfo(
        name="getSupplierStatsByDomains",
        description="Returns the total number of suppliers and total stake for a given domain name.",
        fields_notes={
            "pDomains": 'A vector of domains to query: ["foo.bar", "example.com", ...]. extracted normally from the staked endpoint data of a supplier.',
        },
    ),
    "getDataByDelegatorAddressesAndTimes": QueryFieldInfo(
        name="getDataByDelegatorAddressesAndTimes",
        description="Returns the total rewards, relays (claims and proofs) and slashes done by a delegator of a supplier node. A \"delegator\" here is a supplier rev_share address: an address listed in the supplier's service rev_share configuration that receives a share of that service from the supplier's reward tokens. Observed period in timestamps",
        examples=[
            "# Operator rollup (rewards, claims/proofs, slashes) for supplier rev_share addresses in a time window\n"
            'query { getDataByDelegatorAddressesAndTimes(addresses: ["pokt1..."], '
            'startTs: "2026-06-01T00:00:00Z", endTs: "2026-06-11T00:00:00Z") }',
        ],
    ),
    "getDataByDelegatorAddressesAndBlocks": QueryFieldInfo(
        name="getDataByDelegatorAddressesAndBlocks",
        description="Returns the total rewards, relays (claims and proofs) and slashes done by a delegator of a supplier node. A \"delegator\" here is a supplier rev_share address: an address listed in the supplier's service rev_share configuration that receives a share of that service from the supplier's reward tokens. Observed period in block numbers.",
        examples=[
            "# Operator rollup (rewards, claims/proofs, slashes) for supplier rev_share addresses in a block range\n"
            'query { getDataByDelegatorAddressesAndBlocks(addresses: ["pokt1..."], '
            'startHeight: "780000", endHeight: "790000") }',
        ],
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
    "eventApplicationOverserviceds": QueryFieldInfo(
        name="eventApplicationOverserviceds",
        description="Tracks events that burn less stake than expected from the applciation.",
        fields_notes={
            "applicationId": "Address of the ofending application.",
            "supplierId": "Address of the affected supplier.",
            "effectiveBurn": "Total number of tokens burnt.",
            "expectedBurn": "Total number of tokens that should have been burnt.",
        },
    ),
    "eventGatewayUnbondingBegins": QueryFieldInfo(
        name="eventGatewayUnbondingBegins",
        description="Tracks the start of the unbounding of gateways entities.",
    ),
    "eventGatewayUnbondingEnds": QueryFieldInfo(
        name="eventGatewayUnbondingEnds",
        description="Tracks the end of the unbounding of gateways entities, when the staked token are released.",
    ),
    "eventSupplierUnbondingBegins": QueryFieldInfo(
        name="eventSupplierUnbondingBegins",
        description="Tracks the start of the unbounding of supplier entities.",
    ),
    "eventSupplierUnbondingEnds": QueryFieldInfo(
        name="eventSupplierUnbondingEnds",
        description="Tracks the end of the unbounding of supplier entities, when the staked token are released.",
    ),
    "eventApplicationUnbondingBegins": QueryFieldInfo(
        name="eventApplicationUnbondingBegins",
        description="Tracks the start of the unbounding of application entities.",
    ),
    "eventApplicationUnbondingEnds": QueryFieldInfo(
        name="eventApplicationUnbondingEnds",
        description="Tracks the end of the unbounding of application entities, when the staked token are released.",
    ),
    "eventSupplierSlasheds": QueryFieldInfo(
        name="eventSupplierSlasheds",
        description="Tracks events that resulted in stake slashing of a supplier due to offending the protocol (like missing a proof request). Provides slash amoount and resulting stake of the supplier.",
        examples=[
            "# Recent slashing events of a supplier, with penalty and before/after stake\n"
            "query { eventSupplierSlasheds(orderBy: [BLOCK_ID_DESC, SUPPLIER_ID_DESC], "
            'filter: {supplierId: {equalTo: "pokt1..."}}, first: 20) { totalCount nodes { supplierId blockId '
            "proofValidationStatus proofMissingPenalty previousStakeAmount afterStakeAmount sessionId serviceId "
            "applicationId } } }",
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
        },
        examples=[
            "# Transaction history of an address (most recent first)\n"
            'query { transactions(first: 20, offset: 0, filter: {signerAddress: {equalTo: "pokt1..."}}, '
            "orderBy: BLOCK_ID_DESC) { totalCount nodes { id code block { height: id timestamp } gasUsed gasWanted "
            "signerAddress fees amountOfMessages } } }",
            "# Look up a transaction by hash\n"
            'query { transactions(filter: {id: {equalTo: "<TX_HASH_64_CHAR_HEX>"}}, first: 1) '
            "{ nodes { id code codespace block { height: id timestamp } signerAddress fees memo } } }",
            "# Successful vs failed transaction counts in a time window\n"
            "query { valid: transactions(filter: {code: {equalTo: 0}, block: {timestamp: "
            '{greaterThanOrEqualTo: "2026-06-10T00:00:00Z"}}}) { totalCount } '
            "failed: transactions(filter: {code: {notEqualTo: 0}, block: {timestamp: "
            '{greaterThanOrEqualTo: "2026-06-10T00:00:00Z"}}}) { totalCount } }',
        ],
    ),
    "nativeTransfers": QueryFieldInfo(
        name="nativeTransfers",
        description='Tracks native token transfers (MsgSend) between accounts. Filter by senderId and/or recipientId to get the transfer history of a wallet (who sent tokens to whom). This complements "modToAcctTransfers", which only tracks reward payouts from network modules.',
        fields_notes={
            "senderId": 'The address sending the tokens (must start with "pokt1").',
            "recipientId": 'The address receiving the tokens (must start with "pokt1").',
            "amounts": "Transferred amounts.",
            "denom": 'Token denomination, normally "upokt".',
        },
        examples=[
            "# Transfers where an address is sender or recipient, with the enclosing transaction\n"
            "query { nativeTransfers(first: 20, orderBy: BLOCK_ID_DESC, filter: {or: ["
            '{senderId: {equalTo: "pokt1..."}}, {recipientId: {equalTo: "pokt1..."}}]}) '
            "{ totalCount nodes { id senderId recipientId amounts denom block { height: id timestamp } "
            "transaction { id fees code } } } }",
        ],
    ),
    "accounts": QueryFieldInfo(
        name="accounts",
        description='Queries account entities with their balances and last-activity data. Richer than "balances": each account links to the block that last updated its balance, which is useful for account-activity (recency) queries.',
        fields_notes={
            "id": 'The account address (must start with "pokt1").',
        },
        examples=[
            "# Single account with balances and last activity (singular point lookup)\n"
            'query { account(id: "pokt1...") { id balances { nodes { amount denom '
            "lastUpdatedBlock { height: id timestamp } } } } }",
        ],
    ),
    "applications": QueryFieldInfo(
        name="applications",
        description="Provides visibility on the network applications (entities that request and pay for relays): stake amount and status, staked services, gateway delegations, unstaking begin/end blocks and transfer state. Supports totalCount and stake aggregates, e.g. filter {stakeStatus: {equalTo: Staked}} to count staked apps and sum their stake.",
        fields_notes={
            "id": 'The address of the application (must start with "pokt1").',
            "stakeStatus": "One of: Staked, Unstaking, Unstaked.",
            "applicationServices": "Services the application is staked for (nested serviceId).",
            "applicationGateways": "Gateways this application has delegated to (nested gatewayId).",
            "unstakingEndHeight": "Block at which unstaking completes (when unstaking).",
        },
        examples=[
            "# Staked applications: count and total stake\n"
            "query { applications(filter: {stakeStatus: {equalTo: Staked}}) { totalCount "
            "aggregates { sum { stakeAmount } } } }",
            "# Application detail by address (singular point lookup)\n"
            'query { application(id: "pokt1...") { id stakeAmount stakeStatus unstakingEndHeight '
            "applicationServices { nodes { serviceId } } applicationGateways { totalCount nodes { gatewayId } } } }",
        ],
    ),
    "gateways": QueryFieldInfo(
        name="gateways",
        description="Provides visibility on the network gateways: stake amount and status, unstaking begin/end blocks and the applications delegated to them. Supports totalCount and stake aggregates, e.g. filter {stakeStatus: {equalTo: Staked}}.",
        fields_notes={
            "id": 'The address of the gateway (must start with "pokt1").',
            "stakeStatus": "One of: Staked, Unstaking, Unstaked.",
            "applicationGateways": "Applications delegated to this gateway (nested applicationId).",
        },
        examples=[
            "# Staked gateways with stake aggregate and delegated-app counts\n"
            "query { gateways(first: 20, filter: {stakeStatus: {equalTo: Staked}}) { totalCount "
            "aggregates { sum { stakeAmount } } nodes { id stakeAmount "
            "applicationGateways(first: 1) { totalCount } } } }",
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
        description="Tracks the Morse (v0) to Shannon migration accounts: claim status, destination Shannon address and the claimable balance/stake amounts. Use groupedAggregates(groupBy: CLAIMED) to measure migration progress (claimed vs unclaimed). The unclaimed amounts represent un-migrated supply, which is relevant for total supply calculations.",
        fields_notes={
            "id": 'The Morse address (hex string, no "0x" prefix).',
            "claimed": "Whether the account was already claimed on Shannon.",
            "shannonDestAddress": 'Destination Shannon address (starts with "pokt1") once claimed.',
            "unstakedBalanceAmount": "Claimable liquid balance in upokt.",
            "supplierStakeAmount": "Claimable supplier stake in upokt.",
            "applicationStakeAmount": "Claimable application stake in upokt.",
        },
        examples=[
            "# Migration progress: claimed vs unclaimed totals\n"
            "query { morseClaimableAccounts { groupedAggregates(groupBy: CLAIMED) { keys "
            "sum { unstakedBalanceAmount supplierStakeAmount applicationStakeAmount } } } }",
            "# Paginated claimable-accounts list with claim status and destination\n"
            "query { morseClaimableAccounts(first: 20, orderBy: [CLAIMED_DESC, CLAIMED_AT_ID_DESC]) { totalCount "
            "nodes { id shannonDestAddress claimed claimedAtHeight: claimedAtId transactionId "
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
    "servicesPerformanceBetweenTimes": QueryFieldInfo(
        name="servicesPerformanceBetweenTimes",
        description='Compares per-service performance between a current and a previous time window in a single call: relays, computed units, rewards and the relative change. Use it for "which services grew or declined" style questions.',
        fields_notes={
            "endCurrent": "End of the current window (timestamp).",
            "startCurrentAndEndPrevious": "Boundary timestamp: end of the previous window and start of the current one.",
            "startPrevious": "Start of the previous window (timestamp).",
        },
        examples=[
            "# Per-service comparison: last 24h vs the previous 24h\n"
            'query { servicesPerformanceBetweenTimes(endCurrent: "2026-06-11T00:00:00Z", '
            'startCurrentAndEndPrevious: "2026-06-10T00:00:00Z", startPrevious: "2026-06-09T00:00:00Z") }',
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
    "getRewardsByAddressesAndTime": QueryFieldInfo(
        name="getRewardsByAddressesAndTime",
        description="Fast method returning the plain total rewards (upokt) received by a group of addresses in a date range, with no grouping. This is the cheapest rewards rollup; use the GroupByService / GroupByAddressAndDate variants only when a breakdown is needed.",
        fields_notes={
            "addresses": 'A vector of rev-share addresses (the ones receiving tokens) to query: ["pokt1....", "pokt1...."].',
        },
        examples=[
            "# Total rewards of a group of rev-share addresses (the ones receiving tokens) in a date range (single number)\n"
            'query { getRewardsByAddressesAndTime(addresses: ["pokt1..."], '
            'startDate: "2026-06-10T00:00:00Z", endDate: "2026-06-11T00:00:00Z") }',
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
            'query { supplierServiceConfigs(filter: {supplierId: {equalTo: "pokt1..."}}) '
            "{ pageInfo { hasNextPage endCursor } nodes { serviceId revShare endpoints activatedAtId } } }",
        ],
    ),
    "applicationGateways": QueryFieldInfo(
        name="applicationGateways",
        description="Queries application-gateway delegation pairs. Filter by applicationId to see which gateways an application delegated to, or by gatewayId to list the applications delegating to a gateway.",
        examples=[
            "# Gateways an application has delegated to\n"
            'query { applicationGateways(filter: {applicationId: {equalTo: "pokt1..."}}) '
            "{ nodes { gateway { id stakeAmount stakeDenom } } } }",
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
}
