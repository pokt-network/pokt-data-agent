"""Validator for RPC call descriptors produced by sub-agents.

An RPC call descriptor is a small JSON object with the shape:

    {
        "method": "<method_name>",
        "params": { ... }          # optional
    }

Where `method` must be one of the keys registered in
`src.rpc_client.RPC_METHODS`.
"""

import json
from typing import Any, Dict, Optional, Tuple

from src.rpc_client import RPC_METHODS


def validate_rpc_call(raw: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Validate an RPC call descriptor string produced by an LLM sub-agent.

    Args:
        raw: JSON string describing the RPC call, e.g.
             '{"method": "get_active_validators", "params": {}}'

    Returns:
        Tuple of (is_valid, normalized_descriptor, error_message).
        On success the normalized descriptor is a dict with at least
        ``method`` and ``params`` keys; error_message is None.
        On failure is_valid is False, the dict is empty, and error_message
        describes what went wrong.
    """
    # --- 1. Parse JSON --------------------------------------------------
    try:
        descriptor = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, {}, f"RPC descriptor is not valid JSON: {exc}"

    if not isinstance(descriptor, dict):
        return False, {}, "RPC descriptor must be a JSON object."

    # --- 2. Required 'method' field -------------------------------------
    method = descriptor.get("endpoint_method")
    if not method:
        return False, {}, "RPC descriptor is missing the required 'endpoint_method' field."

    if not isinstance(method, str):
        return False, {}, f"'endpoint_method' must be a string, got {type(method).__name__}."

    if method not in RPC_METHODS:
        available = ", ".join(sorted(RPC_METHODS.keys()))
        return (
            False,
            {},
            f"Unknown RPC endpoint_method '{method}'. Available methods: {available}.",
        )

    # --- 3. Optional 'params' field -------------------------------------
    params = descriptor.get("params", {})
    if not isinstance(params, dict):
        return (
            False,
            {},
            f"'params' must be a JSON object (dict), got {type(params).__name__}.",
        )

    # --- 4. Optional 'params' field -------------------------------------
    path_params = descriptor.get("path_params", {})
    if not isinstance(path_params, dict):
        return (
            False,
            {},
            f"'path_params' must be a JSON object (dict), got {type(path_params).__name__}.",
        )

    # --- 5. Normalise and return ----------------------------------------
    normalized: Dict[str, Any] = {"method": method, "params": params, "path_params": path_params}
    return True, normalized, None
