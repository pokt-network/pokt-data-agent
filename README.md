# Pocket Network Data MCP and Agent

This repository contains two projects that are highly co-dependent:
- The data endpoints' ([GraphQL](https://data.pocket.network/) and [RPC](https://sauron-api.infra.pocket.network)) Model Context Protocol (MCP).
- The `Pocket Data Agent`, a standalone agent that solves natural language queries, based on LangGraph.

## MCP Quick Setup

**Note:**: For product-specific instructions (like ClaudeCode), check the "MCP Configuration Examples" section

To quickly deploy the MCP just do:

```bash
# Install uv if needed: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

Set required environment variables (e.g. in `.env`):

```env
POCKET_NETWORK_RPC_ENDPOINT=https://sauron-api.infra.pocket.network
POCKET_NETWORK_DATA_ENDPOINT=https://data.pocket.network/
```

Run the serveR:
```bash
uv run mcp_server.py
```

This will create an MCP server with the **Data Tools** and **Introspection Tools** exposed.

### Remote Mode (Streamable HTTP)

Deploy on a server with bearer-token auth:

```bash
MCP_API_KEY=your-secret uv run mcp_server_remote.py
```

## MCP Environment Variables

| Variable | Description | Default | Requiered |
|---|---|---|---|
| `POCKET_NETWORK_RPC_ENDPOINT` | Pocket Network RPC URL | — | ✅ |
| `POCKET_NETWORK_DATA_ENDPOINT` | Pocket Network GraphQL URL | — | ✅ |
| `POCKET_NETWORK_MCP_EXPOSURE` | Tool set to expose (see below) | `endpoints-tools` | ❌ |
| `MCP_API_KEY` | Bearer token for remote mode | — | ✅ (only for remote mode) |
| `MCP_HOST` | Remote bind address | `0.0.0.0` | ❌ |
| `MCP_PORT` | Remote bind port | `8000` | ❌ |
| `MCP_PATH` | Remote URL path | `/mcp` | ❌ |
| `LLM_BASE_URL` | OpenAI-compatible LLM endpoint | `http://localhost:8087` | ❓ only when `POCKET_NETWORK_MCP_EXPOSURE` is set to expose agents |
| `LLM_MODEL` | Selected model name | `local` | ❓ only when `POCKET_NETWORK_MCP_EXPOSURE` is set to expose agents |
| `OPENAI_API_KEY` | API key for LLM endpoint | `not-needed` | ❌ |

## Tool Exposure Modes

Set `POCKET_NETWORK_MCP_EXPOSURE` to control which tools are available:

| Value | Tools |
|---|---|
| `endpoints-tools` | Direct GraphQL/RPC query + introspection tools, best for very powerful models |
| `sub-agents` | Domain-specific sub-agent tools (network usage, tokenomics, rewards, etc.), useful when you need to keep context cleaner |
| `main-agent` | Single main agent tool that auto-routes queries, use this to fully delegate the resolution of the queries |
| `all-agents` | Main agent + all sub-agents (Normally not useful) |
| `everything` | All tools combined (Just for testing, very redundant) |

Keep in mind that enabling more tools loads the context of the agent with more tokens. If you are using a powerful model for your agent, the default `endpoints-tools` is probably the best option. If you are embedding this into a smaller agent using a smaller model, consider using `sub-agents` or `main-agent`, where the later is the most lightweight option.
The intention behind having agents as tools is to help lightweight models to separate context when trying to solve a Pocket Network related question, the agents have a very specific execution prompts that will guide simpler language models in the resolution of queries. So, smaller agents can fully delegate into this tool and then recover the solved query.

## Available Tools

**Data Tools** — direct endpoint access:
- `list_valid_methods` — list available GraphQL/RPC methods by usage partition. The methods are a sub-set of methods, curated by us.
- `get_method_data` — get schema and examples for a given method
- `execute_graphql` — run a GraphQL query
- `execute_rpc` — run an RPC call

**Introspection Tools** — schema discovery for GraphQL based queries:
- `get_field_schema` — get field arguments and return type
- `get_type_info` — get fields for a GraphQL type
- `get_enum_values` — list valid enum values

**Agent Tools** — natural language queries:
- `mainagent` — auto-routes to the best sub-agent, soling the query completely (`WIP`).
- `subagent_NetworkUsage` — network usage and relay data
- `subagent_Tokenomics` — token supply and economics
- `subagent_SettlementRewards` — rewards and settlement data
- `subagent_ServiceEconomics` — service and application metrics
- `subagent_GovernanceAdmin` — governance proposals and params
- `subagent_StakingParticipantState` — staking and validator state
- `subagent_AccountState` — account balances and state


## MCP Configuration Examples

Here we list example configurations for different products/agents. 

### OpenCode

**Local** — add to `opencode.json`:

```json
{
  "mcp": {
    "pokt-data-agent": {
      "type": "local",
      "command": ["uv", "run", "mcp_server.py"],
      "timeout": 360000,
      "environment": {
        "POCKET_NETWORK_RPC_ENDPOINT": "https://sauron-api.infra.pocket.network",
        "POCKET_NETWORK_DATA_ENDPOINT": "https://data.pocket.network/",
        "POCKET_NETWORK_MCP_EXPOSURE": "endpoints-tools"
      }
    }
  }
}
```

**Remote**:

```json
{
  "mcp": {
    "pokt-data-agent": {
      "type": "remote",
      "url": "https://your-server.example.com/mcp",
      "timeout": 360000,
      "oauth": false,
      "headers": {
        "Authorization": "Bearer ${POKT_MCP_API_KEY}"
      }
    }
  }
}
```

### Claude Code

**Local** — add to `~/.claude.json` or `.mcp.json`:

```json
{
  "mcpServers": {
    "pokt-data-agent": {
      "command": "uv",
      "args": ["run", "mcp_server.py"],
      "cwd": "/path/to/pokt-data-agent",
      "timeout": 360000,
      "env": {
        "POCKET_NETWORK_RPC_ENDPOINT": "https://sauron-api.infra.pocket.network",
        "POCKET_NETWORK_DATA_ENDPOINT": "https://data.pocket.network/",
        "POCKET_NETWORK_MCP_EXPOSURE": "endpoints-tools"
      }
    }
  }
}
```

**Remote**:

```json
{
  "mcpServers": {
    "pokt-data-agent": {
      "type": "http",
      "url": "https://your-server.example.com/mcp",
      "timeout": 360000,
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

### HermesAgent

Add to `~/.hermes/config.yaml` under `mcp_servers`:

```yaml
mcp_servers:
  pokt-data-agent:
    command: "uv"
    args: ["run", "mcp_server.py"]
    cwd: "/path/to/pokt-data-agent"
    timeout: 360
    env:
      POCKET_NETWORK_RPC_ENDPOINT": "https://sauron-api.infra.pocket.network"
      POCKET_NETWORK_DATA_ENDPOINT": "https://data.pocket.network/"
      POCKET_NETWORK_MCP_EXPOSURE": "endpoints-tools"
```

For remote deployment:

```yaml
mcp_servers:
  pokt-data-agent:
    url: "https://your-server.example.com/mcp"
    headers:
      Authorization: "Bearer YOUR_API_KEY"
    timeout: 360
```

**Note:** HermesAgent `timeout` is in **seconds** (default: 120). OpenCode and Claude Code use **milliseconds**.


# Contributing 

Contributions are welcome, either in the form of new methods use cases and/or examples.

## Linting

This project uses [Ruff](https://github.com/astral-sh/ruff) for linting and formatting, with pre-commit hooks for automatic checks.

```bash
# Install pre-commit hooks (runs on every commit)
uv run pre-commit install

# Run linting manually
uv run ruff check .

# Fix auto-fixable issues
uv run ruff check --fix .

# Format code
uv run ruff format .
```

A GitHub Actions workflow (`.github/workflows/lint.yml`) runs the same checks on every push and pull request.