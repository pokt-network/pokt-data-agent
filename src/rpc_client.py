"""REST/RPC client for the Pocket Network Sauron API (Cosmos SDK endpoints)."""

import json
import re
import requests
from typing import Any, Dict, Optional, Tuple
import os
from src.models import QueryFieldInfo


POCKET_NETWORK_RPC_ENDPOINT = os.getenv("POCKET_NETWORK_RPC_ENDPOINT", None)
if POCKET_NETWORK_RPC_ENDPOINT is None:
    raise ValueError(
        'The "POCKET_NETWORK_RPC_ENDPOINT" enviroment variable is not set.'
    )

# ---------------------------------------------------------------------------
# Known RPC methods (Cosmos SDK REST paths supported by Sauron)
# ---------------------------------------------------------------------------

# Maps a logical method name to its (http_method, path_template, default_params)
RPC_METHODS = {
    # ------------------------------------------------------------------
    # Cosmos staking
    # ------------------------------------------------------------------
    "get_active_validators": QueryFieldInfo(
        name="get_active_validators",
        description= (
            "Returns the list of active (bonded) validators on the network. "
            "Each validator entry includes operator_address, consensus_pubkey, "
            "status, tokens (staked amount in upokt), delegator_shares, "
            "description (moniker, identity, website, details), "
            "commission rates, and min_self_delegation."
        ),
        method_data={
            "http_method": "GET",
            "path": "/cosmos/staking/v1beta1/validators",
            "default_params": {
                "status": "BOND_STATUS_BONDED",
                "pagination.limit": 1000,
            },
        },
    ),  
    # ------------------------------------------------------------------
    # Cosmos bank
    # ------------------------------------------------------------------
    "get_account_balance": QueryFieldInfo(
        name="get_account_balance",
        description=(
            "Returns the token balances of a given account address. "
            "Provide the bech32 pokt1… address as the path parameter {address}. "
            "Each balance entry contains 'denom' (e.g. 'upokt') and 'amount'."
        ),
        method_data={
            "http_method": "GET",
            "path": "/cosmos/bank/v1beta1/balances/{address}",
            "default_params": {},
        },
    ),
    "get_total_supply": QueryFieldInfo(
        name="get_total_supply",
        description=(
            "Returns the total on-chain token supply for all denominations. "
            "The relevant entry has denom='upokt'. "
            "Useful for a quick snapshot of total circulating supply."
        ),
        method_data={
            "http_method": "GET",
            "path": "/cosmos/bank/v1beta1/supply",
            "default_params": {"pagination.limit": 100},
        },
    ),
    # ------------------------------------------------------------------
    # Application module
    # ------------------------------------------------------------------
    "get_all_applications": QueryFieldInfo(
        name="get_all_applications",
        description=(
            "Returns all staked applications. Each entry includes address, "
            "stake amount (upokt), service_configs (list of service_id), "
            "delegatee_gateway_addresses, and unstake_session_end_height. "
            "Optional filter: delegatee_gateway_address to restrict results "
            "to applications delegated to a specific gateway."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/application/application",
            "default_params": {"pagination.limit": 1000},
        },
    ),
    "get_application": QueryFieldInfo(
        name="get_application",
        description=(
            "Returns the full details of a single application identified by "
            "its bech32 address. Fields: address, stake, service_configs, "
            "delegatee_gateway_addresses, unstake_session_end_height, "
            "pending_transfer."
            "Provide the bech32 pokt1… address as the path parameter {address}. "
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/application/application/{address}",
            "default_params": {},
        },
    ),
    "get_application_params": QueryFieldInfo(
        name="get_application_params",
        description=(
            "Returns the current governance parameters of the application module, "
            "such as max_delegated_gateways and min_stake."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/application/params",
            "default_params": {},
        },
    ),
    # ------------------------------------------------------------------
    # Gateway module
    # ------------------------------------------------------------------
    "get_all_gateways": QueryFieldInfo(
        name="get_all_gateways",
        description=(
            "Returns all staked gateways. Each entry includes address, "
            "stake amount (upokt), and unstake_session_end_height."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/gateway/gateway",
            "default_params": {"pagination.limit": 1000},
        },
    ),
    "get_gateway": QueryFieldInfo(
        name="get_gateway",
        description=(
            "Returns the full details of a single gateway identified by its "
            "bech32 address. Fields: address, stake, unstake_session_end_height."
            "Provide the bech32 pokt1… address as the path parameter {address}. "
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/gateway/gateway/{address}",
            "default_params": {},
        },
    ),
    "get_gateway_params": QueryFieldInfo(
        name="get_gateway_params",
        description=(
            "Returns the current governance parameters of the gateway module, "
            "such as min_stake."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/gateway/params",
            "default_params": {},
        },
    ),
    # ------------------------------------------------------------------
    # Supplier module
    # ------------------------------------------------------------------
    "get_all_suppliers": QueryFieldInfo(
        name="get_all_suppliers",
        description=(
            "Returns all staked suppliers. Each entry includes owner_address, "
            "operator_address, stake amount (upokt), services (with endpoints, "
            "rpc_type, and rev_share configurations). "
            "Optional filters: service_id, operator_address, owner_address. "
            "Set 'dehydrated=true' to receive a smaller payload without endpoint details."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/supplier/supplier",
            "default_params": {"pagination.limit": 1000},
        },
    ),
    "get_supplier": QueryFieldInfo(
        name="get_supplier",
        description=(
            "Returns the full details of a single supplier identified by its "
            "operator_address. Fields: owner_address, operator_address, stake, "
            "services (service_id, endpoints, rev_share). "
            "Set 'dehydrated=true' to receive a smaller payload."
            "Provide the bech32 pokt1… operator_address as the path parameter {operator_address}. "
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/supplier/supplier/{operator_address}",
            "default_params": {},
        },
    ),
    "get_supplier_params": QueryFieldInfo(
        name="get_supplier_params",
        description=(
            "Returns the current governance parameters of the supplier module, "
            "such as min_stake and staking_fee."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/supplier/params",
            "default_params": {},
        },
    ),
    # ------------------------------------------------------------------
    # Service module
    # ------------------------------------------------------------------
    "get_all_services": QueryFieldInfo(
        name="get_all_services",
        description=(
            "Returns all registered services on the network. Each entry includes "
            "id (service identifier string), name (human-readable), "
            "compute_units_per_relay (CUPR), and owner_address."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/service/service",
            "default_params": {"pagination.limit": 1000},
        },
    ),
    "get_service": QueryFieldInfo(
        name="get_service",
        description=(
            "Returns the details of a single service identified by its id string "
            "(e.g. 'eth', 'base', 'solana'). Fields: id, name, "
            "compute_units_per_relay, owner_address."
            "Provide the service id as the path parameter {id}. "
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/service/service/{id}",
            "default_params": {},
        },
    ),
    "get_all_relay_mining_difficulties": QueryFieldInfo(
        name="get_all_relay_mining_difficulties",
        description=(
            "Returns the current relay-mining difficulty for all services. "
            "Each entry includes service_id, block_height (when last updated), "
            "num_relays_ema (exponential moving average of relays per session), "
            "and target_hash. Higher num_relays_ema means higher throughput. "
            "This is the live counterpart to the GraphQL eventRelayMiningDifficultyUpdateds."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/service/relay_mining_difficulty",
            "default_params": {"pagination.limit": 1000},
        },
    ),
    "get_relay_mining_difficulty": QueryFieldInfo(
        name="get_relay_mining_difficulty",
        description=(
            "Returns the current relay-mining difficulty for a single service. "
            "Fields: service_id, block_height, num_relays_ema, target_hash."
            "Provide the service id as the path parameter {serviceId}. "
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/service/relay_mining_difficulty/{serviceId}",
            "default_params": {},
        },
    ),
    "get_service_params": QueryFieldInfo(
        name="get_service_params",
        description=(
            "Returns the current governance parameters of the service module, "
            "such as add_service_fee."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/service/params",
            "default_params": {},
        },
    ),
    # ------------------------------------------------------------------
    # Session module
    # ------------------------------------------------------------------
    "get_session": QueryFieldInfo(
        name="get_session",
        description=(
            "Returns the session for a specific application-service pair at a "
            "given block height. A session groups an application with the set of "
            "suppliers eligible to serve it for that period. "
            "Required params: application_address (bech32), service_id, block_height. "
            "Response includes session_id, session_number, num_blocks_per_session, "
            "session start/end block heights, the application details, and the list "
            "of assigned suppliers."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/session/get_session",
            "default_params": {},
        },
    ),
    "get_session_params": QueryFieldInfo(
        name="get_session_params",
        description=(
            "Returns the current governance parameters of the session module, "
            "such as num_blocks_per_session."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/session/params",
            "default_params": {},
        },
    ),
    # ------------------------------------------------------------------
    # Proof module
    # ------------------------------------------------------------------
    "get_all_claims": QueryFieldInfo(
        name="get_all_claims",
        description=(
            "Returns all pending claims (sessions where a supplier submitted a "
            "claim but the proof window has not yet closed). "
            "Optional filters: supplier_operator_address, session_id, session_end_height. "
            "Each entry includes session_header (application_address, service_id, "
            "session_id, start/end heights), supplier_operator_address, and "
            "root_hash (the Sparse Merkle Sum Trie root)."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/proof/claim",
            "default_params": {"pagination.limit": 1000},
        },
    ),
    "get_claim": QueryFieldInfo(
        name="get_claim",
        description=(
            "Returns the claim for a specific session and supplier operator address. "
            "Fields: session_header, supplier_operator_address, root_hash."
            "Provide the session id as the path parameter {session_id}. "
            "Provide the supplier aaddress id as the path parameter {supplier_operator_address}. "
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/proof/claim/{session_id}/{supplier_operator_address}",
            "default_params": {},
        },
    ),
    "get_all_proofs": QueryFieldInfo(
        name="get_all_proofs",
        description=(
            "Returns all submitted proofs (sessions where the supplier has submitted "
            "a Sparse Merkle Proof). Optional filters: supplier_operator_address, "
            "session_id, session_end_height. "
            "Each entry mirrors the claim structure with the added closest_merkle_proof."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/proof/proof",
            "default_params": {"pagination.limit": 1000},
        },
    ),
    "get_proof": QueryFieldInfo(
        name="get_proof",
        description=(
            "Returns the proof for a specific session and supplier operator address. "
            "Fields: session_header, supplier_operator_address, closest_merkle_proof."
            "Provide the session id as the path parameter {session_id}. "
            "Provide the supplier aaddress id as the path parameter {supplier_operator_address}. "
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/proof/proof/{session_id}/{supplier_operator_address}",
            "default_params": {},
        },
    ),
    "get_proof_params": QueryFieldInfo(
        name="get_proof_params",
        description=(
            "Returns the current governance parameters of the proof module, such as "
            "min_relay_difficulty_bits, proof_request_probability, "
            "proof_requirement_threshold, and proof_missing_penalty."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/proof/params",
            "default_params": {},
        },
    ),
    # ------------------------------------------------------------------
    # Shared module
    # ------------------------------------------------------------------
    "get_shared_params": QueryFieldInfo(
        name="get_shared_params",
        description=(
            "Returns the shared governance parameters that apply across multiple "
            "modules, including: num_blocks_per_session, claim/proof window offsets, "
            "supplier_unbonding_period_sessions, application_unbonding_period_sessions, "
            "gateway_unbonding_period_sessions, compute_units_to_tokens_multiplier "
            "(CUTTM), and compute_unit_cost_granularity."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/shared/params",
            "default_params": {},
        },
    ),
    # ------------------------------------------------------------------
    # Tokenomics module
    # ------------------------------------------------------------------
    "get_tokenomics_params": QueryFieldInfo(
        name="get_tokenomics_params",
        description=(
            "Returns the current tokenomics governance parameters: "
            "dao_reward_address, mint_allocation_percentages (dao, proposer, "
            "supplier, source_owner, application), global_inflation_per_claim, "
            "mint_equals_burn_claim_distribution, and mint_ratio."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/tokenomics/params",
            "default_params": {},
        },
    ),
    # ------------------------------------------------------------------
    # Migration module
    # ------------------------------------------------------------------
    "get_all_morse_claimable_accounts": QueryFieldInfo(
        name="get_all_morse_claimable_accounts",
        description=(
            "Returns all Morse (v0) claimable accounts imported during the "
            "Shannon migration. Each entry contains the Morse address and the "
            "claimable token amounts. Useful for tracking migration progress."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/migration/morse_claimable_account",
            "default_params": {"pagination.limit": 1000},
        },
    ),
    "get_morse_claimable_account": QueryFieldInfo(
        name="get_morse_claimable_account",
        description=(
            "Returns the Morse claimable account record for a specific Morse address. "
            "Useful to check whether a particular Morse account has already been "
            "claimed on Shannon."
            "Provide the Morse address as the path parameter {address} (remove leading \"0x\" from it). "
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/migration/morse_claimable_account/{address}",
            "default_params": {},
        },
    ),
    "get_migration_params": QueryFieldInfo(
        name="get_migration_params",
        description=(
            "Returns the current governance parameters of the migration module, "
            "such as morse_account_claim_is_enabled."
        ),
        method_data={
            "http_method": "GET",
            "path": "/pokt-network/poktroll/migration/params",
            "default_params": {},
        },
    ),
}


class PocketNetworkRPCClient:
    """Client for querying the Pocket Network Sauron REST/RPC API."""

    def __init__(self, endpoint: str = POCKET_NETWORK_RPC_ENDPOINT):
        """
        Initialise the RPC client.

        Args:
            endpoint: Base URL of the Sauron REST API.
        """
        self.endpoint = endpoint.rstrip("/")

    # ------------------------------------------------------------------
    # Generic execution
    # ------------------------------------------------------------------

    @staticmethod
    def _substitute_path_params(path: str, path_params: Dict[str, str]) -> str:
        """
        Substitute path template parameters in a URL path.

        Args:
            path: The path template string (e.g., "/path/{param1}/{param2}").
            path_params: Dictionary mapping parameter names to values.

        Returns:
            The path with all {param} placeholders substituted.

        Raises:
            ValueError: If a required path parameter is missing or if there are
                       unused path_params provided.
        """
        # Find all placeholders in the path
        placeholders = set(re.findall(r"\{(\w+)\}", path))

        # Check for missing required parameters
        missing_params = placeholders - set(path_params.keys())
        if missing_params:
            raise ValueError(
                f"Missing required path parameters: {sorted(missing_params)}"
            )

        # Check for unused parameters
        unused_params = set(path_params.keys()) - placeholders
        if unused_params:
            raise ValueError(
                f"Unused path parameters provided: {sorted(unused_params)}"
            )

        # Substitute all parameters
        result = path
        for param_name, param_value in path_params.items():
            result = result.replace(f"{{{param_name}}}", str(param_value))

        return result

    def execute_query(
        self,
        method_name: str,
        params: Optional[Dict[str, Any]] = None,
        path_params: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, Any, Optional[str|None]]:
        """
        Execute a named RPC call against the Sauron API.

        Args:
            method_name: Logical method name (key in RPC_METHODS).
            params: Optional query-parameter overrides merged on top of the
                    method's default_params. Used for query/POST body parameters only.
            path_params: Optional dictionary of path template parameter substitutions
                        (e.g., {"address": "pokt1..."} for paths like
                        "/cosmos/bank/v1beta1/balances/{address}").
                        Required if the method's path contains placeholders.

        Returns:
            Tuple of (success, result, error_message).

        Raises:
            ValueError: If a required path parameter is missing or if unused
                       path_params are provided.
        """
        method_def = RPC_METHODS.get(method_name)
        if method_def is None:
            return (
                False,
                None,
                f"Unknown RPC method '{method_name}'. "
                f"Available: {list(RPC_METHODS.keys())}",
            )
        
        # Get this method data
        method_data = method_def.method_data

        merged_params = dict(method_data.get("default_params", {}))
        if params:
            merged_params.update(params)

        # Substitute path parameters if the path contains placeholders
        path = method_data["path"]
        if path_params is None:
            path_params = {}

        try:
            path = self._substitute_path_params(path, path_params)
        except ValueError as e:
            return False, None, str(e)

        url = self.endpoint + path
        http_method = method_data.get("http_method", "GET").upper()

        try:
            if http_method == "GET":
                response = requests.get(url, params=merged_params, timeout=30)
            elif http_method == "POST":
                response = requests.post(
                    url,
                    json=merged_params,
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
            else:
                return False, None, f"Unsupported HTTP method: {http_method}"

            response.raise_for_status()
            data = response.json()
            return True, data, None

        except requests.exceptions.Timeout:
            return False, None, "RPC request timeout (30s)"
        except requests.exceptions.ConnectionError:
            return (
                False,
                None,
                "Connection error to RPC endpoint. Please check the endpoint.",
            )
        except requests.exceptions.HTTPError as e:
            return (
                False,
                None,
                f"HTTP error: {e.response.status_code} - {e.response.text}",
            )
        except requests.exceptions.RequestException as e:
            return False, None, f"Request error: {str(e)}"
        except json.JSONDecodeError as e:
            return False, None, f"Invalid JSON response from RPC: {str(e)}"
        except Exception as e:
            return False, None, f"Unexpected error: {str(e)}"

