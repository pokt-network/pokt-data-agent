"""LangChain introspection tools for querying the Pocket Network GraphQL schema."""

import logging
from typing import Any, Dict

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

import os

_ENDPOINT = os.getenv("POCKET_NETWORK_DATA_ENDPOINT", None)
if _ENDPOINT is None:
    raise ValueError(
        'The "POCKET_NETWORK_DATA_ENDPOINT" enviroment variable is not set.'
    )

_CACHE: Dict[str, Any] = {}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _introspect(query: str) -> Any:
    """Execute a raw GraphQL introspection query and return the data payload."""
    response = requests.post(
        _ENDPOINT,
        json={"query": query},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if "errors" in payload:
        raise RuntimeError(f"Introspection error: {payload['errors']}")
    return payload["data"]


def _resolve_type_name(type_obj: Dict[str, Any]) -> str:
    """Recursively unwrap NON_NULL / LIST wrappers and return a human-readable type string."""
    if type_obj is None:
        return "Unknown"
    kind = type_obj.get("kind")
    name = type_obj.get("name")
    of_type = type_obj.get("ofType")

    if kind == "NON_NULL":
        return f"{_resolve_type_name(of_type)}!"
    if kind == "LIST":
        return f"[{_resolve_type_name(of_type)}]"
    # Named leaf (SCALAR, OBJECT, ENUM, INPUT_OBJECT, INTERFACE, UNION)
    return name or "Unknown"


def _get_type_fields(type_name: str) -> list:
    """Return raw field list for an OBJECT or INPUT_OBJECT type (cached).

    Fetches 4 levels of ofType nesting on both the field type and any args,
    so that deeply wrapped types like [EventClaimSettledsGroupBy!]! are resolved.
    Two separate introspection calls are made to stay within query complexity limits:
    one for field types, one for field args.
    """
    cache_key = f"_raw_type:{type_name}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    type_frag = (
        "name kind ofType { name kind ofType { name kind ofType { name kind } } }"
    )

    # Call 1: field names + their return types
    data = _introspect(
        f'{{ __type(name: "{type_name}") {{ kind '
        f"fields {{ name description type {{ {type_frag} }} }} "
        f"inputFields {{ name description type {{ {type_frag} }} }} "
        f"}} }}"
    )
    type_info = data.get("__type") or {}
    fields = type_info.get("fields") or type_info.get("inputFields") or []

    # Call 2: args for each field (only for OBJECT types; INPUT_OBJECT has no args)
    if type_info.get("kind") == "OBJECT" and fields:
        try:
            args_data = _introspect(
                f'{{ __type(name: "{type_name}") {{ '
                f"fields {{ name args {{ name type {{ {type_frag} }} }} }} "
                f"}} }}"
            )
            args_by_field = {
                f["name"]: f.get("args", [])
                for f in (args_data.get("__type") or {}).get("fields") or []
            }
            for f in fields:
                f["args"] = args_by_field.get(f["name"], [])
        except Exception:
            pass  # args are a nice-to-have; don't fail the whole call

    _CACHE[cache_key] = fields
    return fields


# ---------------------------------------------------------------------------
# Exported tools
# ---------------------------------------------------------------------------


GET_FIELD_SCHEMA_DESCRIPTION = """Return the schema for a top-level GraphQL query field.

Shows the field's arguments (name + type) and the fields available on its
return type (one level deep).  Use this to understand how to call a field
and what data you can request back.

Args:
    field_name: Name of the top-level query field, e.g. "eventClaimSettleds"
"""
GET_FIELD_SCHEMA_NAME = "data_get_field_schema"
@tool(GET_FIELD_SCHEMA_NAME, description=GET_FIELD_SCHEMA_DESCRIPTION)
def get_field_schema(field_name: str) -> str:
    cache_key = f"field_schema:{field_name}"
    if cache_key in _CACHE:
        logger.debug("[tools] cache hit for %s", cache_key)
        return _CACHE[cache_key]

    logger.info("[tools] get_field_schema(%r) – fetching from API", field_name)

    # 1. Get all Query fields (cached separately)
    query_fields_key = "_query_fields"
    if query_fields_key not in _CACHE:
        type_fragment = (
            "name kind ofType { name kind ofType { name kind ofType { name kind } } }"
        )
        data = _introspect(
            f'{{ __type(name: "Query") {{ fields {{ name description '
            f"args {{ name type {{ {type_fragment} }} }} "
            f"type {{ {type_fragment} }} "
            f"}} }} }}"
        )
        _CACHE[query_fields_key] = data["__type"]["fields"]

    all_fields = _CACHE[query_fields_key]
    field = next((f for f in all_fields if f["name"] == field_name), None)

    if not field:
        result = f"Field '{field_name}' not found in Query type. Use list_query_fields() to see available fields."
        _CACHE[cache_key] = result
        return result

    # 2. Format arguments
    args_lines = []
    for arg in field.get("args", []):
        args_lines.append(f"  - {arg['name']}: {_resolve_type_name(arg['type'])}")

    description = field.get("description") or "(no description)"
    return_type_name = _resolve_type_name(field["type"])

    # 3. Resolve return type fields (one level deep)
    # Unwrap to get the bare named type
    raw_type = field["type"]
    while raw_type.get("ofType"):
        raw_type = raw_type["ofType"]
    bare_return_type = raw_type.get("name", "")

    return_fields_lines = []
    if bare_return_type:
        try:
            ret_fields = _get_type_fields(bare_return_type)
            for rf in ret_fields:
                rf_type = _resolve_type_name(rf["type"])
                desc = f"  – {rf['description']}" if rf.get("description") else ""
                args = rf.get("args") or []
                args_str = ""
                if args:
                    args_str = (
                        "("
                        + ", ".join(
                            f"{a['name']}: {_resolve_type_name(a['type'])}"
                            for a in args
                        )
                        + ")"
                    )
                return_fields_lines.append(
                    f"  - {rf['name']}{args_str}: {rf_type}{desc}"
                )
        except Exception as exc:
            return_fields_lines.append(
                f"  (could not resolve return type fields: {exc})"
            )

    args_block = "\n".join(args_lines) if args_lines else "  (none)"
    return_block = (
        "\n".join(return_fields_lines)
        if return_fields_lines
        else "  (scalar or unresolvable)"
    )

    result = (
        f"Field: {field_name}\n"
        f"Description: {description}\n"
        f"Arguments:\n{args_block}\n"
        f"Return type: {return_type_name}\n"
        f"Return type fields ({bare_return_type}):\n{return_block}"
    )

    _CACHE[cache_key] = result
    logger.debug("[tools] get_field_schema(%r) result:\n%s", field_name, result)
    return result

GET_TYPE_INFO_DESCRIPTION = """Return all fields (and their types) for any named GraphQL type.

Works for OBJECT types (e.g. "EventClaimSettled", "Block", "Supplier") and
INPUT_OBJECT types used in filters (e.g. "EventClaimSettledFilter",
"BlockFilter").  Use this to drill into nested types you discovered via
get_field_schema.

Args:
    type_name: Exact GraphQL type name, e.g. "EventClaimSettledFilter"
"""
GET_TYPE_INFO_NAME = "data_get_type_info"
@tool(GET_TYPE_INFO_NAME, description=GET_TYPE_INFO_DESCRIPTION)
def get_type_info(type_name: str) -> str:
    cache_key = f"type:{type_name}"
    if cache_key in _CACHE:
        logger.debug("[tools] cache hit for %s", cache_key)
        return _CACHE[cache_key]

    logger.info("[tools] get_type_info(%r) – fetching from API", type_name)

    type_frag = (
        "name kind ofType { name kind ofType { name kind ofType { name kind } } }"
    )

    # Call 1: field/inputField names + types (no args to keep query small)
    data = _introspect(
        f'{{ __type(name: "{type_name}") {{ name kind description '
        f"fields {{ name description type {{ {type_frag} }} }} "
        f"inputFields {{ name description type {{ {type_frag} }} }} "
        f"}} }}"
    )

    type_info = data.get("__type")
    if not type_info:
        result = f"Type '{type_name}' not found in schema."
        _CACHE[cache_key] = result
        return result

    kind = type_info.get("kind", "UNKNOWN")
    description = type_info.get("description") or "(no description)"
    fields = type_info.get("fields") or type_info.get("inputFields") or []

    # Call 2: args for each field (OBJECT only; INPUT_OBJECT fields have no args)
    args_by_field: Dict[str, list] = {}
    if kind == "OBJECT" and fields:
        try:
            args_data = _introspect(
                f'{{ __type(name: "{type_name}") {{ '
                f"fields {{ name args {{ name type {{ {type_frag} }} }} }} "
                f"}} }}"
            )
            for f in (args_data.get("__type") or {}).get("fields") or []:
                args_by_field[f["name"]] = f.get("args") or []
        except Exception:
            pass

    field_lines = []
    for f in fields:
        type_str = _resolve_type_name(f["type"])
        desc = f"  – {f['description']}" if f.get("description") else ""
        args = args_by_field.get(f["name"], [])
        args_str = ""
        if args:
            args_str = (
                "("
                + ", ".join(
                    f"{a['name']}: {_resolve_type_name(a['type'])}" for a in args
                )
                + ")"
            )
        field_lines.append(f"  - {f['name']}{args_str}: {type_str}{desc}")

    fields_block = "\n".join(field_lines) if field_lines else "  (no fields)"

    result = (
        f"Type: {type_name} ({kind})\n"
        f"Description: {description}\n"
        f"Fields:\n{fields_block}"
    )

    _CACHE[cache_key] = result
    logger.debug(
        "[tools] get_type_info(%r) result length=%d chars", type_name, len(result)
    )
    return result

GET_ENUM_VALUES_DESCRIPTION = """Return all valid values for a GraphQL ENUM type.

Use this when you need to know the allowed values for an enum argument or
filter field, e.g. groupBy keys, proof statuses, settlement reasons.

Args:
    enum_name: Exact GraphQL enum type name, e.g. "EventClaimSettledGroupBy"
"""
GET_ENUM_VALUES_NAME = "data_get_enum_values"
@tool(GET_ENUM_VALUES_NAME, description=GET_ENUM_VALUES_DESCRIPTION)
def get_enum_values(enum_name: str) -> str:
    cache_key = f"enum:{enum_name}"
    if cache_key in _CACHE:
        logger.debug("[tools] cache hit for %s", cache_key)
        return _CACHE[cache_key]

    logger.info("[tools] get_enum_values(%r) – fetching from API", enum_name)

    data = _introspect(
        f'{{ __type(name: "{enum_name}") {{ name kind enumValues {{ name description isDeprecated }} }} }}'
    )

    type_info = data.get("__type")
    if not type_info:
        result = f"Enum '{enum_name}' not found in schema."
        _CACHE[cache_key] = result
        return result

    if type_info.get("kind") != "ENUM":
        result = f"'{enum_name}' is not an ENUM (it is {type_info.get('kind')})."
        _CACHE[cache_key] = result
        return result

    values = type_info.get("enumValues") or []
    value_lines = []
    for v in values:
        deprecated = " [DEPRECATED]" if v.get("isDeprecated") else ""
        desc = f"  – {v['description']}" if v.get("description") else ""
        value_lines.append(f"  - {v['name']}{deprecated}{desc}")

    values_block = "\n".join(value_lines) if value_lines else "  (no values)"

    result = f"Enum: {enum_name}\nValues:\n{values_block}"

    _CACHE[cache_key] = result
    logger.debug("[tools] get_enum_values(%r): %d values", enum_name, len(values))
    return result


# Exported list for use in sub_agents.py
INTROSPECTION_TOOLS = [get_field_schema, get_type_info, get_enum_values]
