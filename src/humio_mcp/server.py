"""HumioMCP - FastMCP server exposing Humio/LogScale tools."""

import sys
from typing import Annotated
from pydantic import Field
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import EnumMeta

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from humio_mcp import __version__
from humio_mcp.config import AppConfig, build_cluster_enum, load_config
from humio_mcp.humio_client import HumioClient

# ---------------------------------------------------------------------------
# Load config at import time so the Enum is available for tool registration
# ---------------------------------------------------------------------------

_config: AppConfig = load_config()
ClusterEnum: EnumMeta = build_cluster_enum(_config)
_default_cluster = ClusterEnum(_config.default_cluster)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Lifespan: expose config via shared context
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    """Shared application context available to all tools."""

    config: AppConfig


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:  # noqa: ARG001
    """Expose the already-loaded configuration through the lifespan context."""
    yield AppContext(config=_config)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "humio-mcp",
    instructions=(
        "MCP server for querying Humio/LogScale dashboards and executing searches. "
        "Use execute_search to inspect logs for the various deployments of the COM services."
    ),
    lifespan=app_lifespan,
)


def _make_client(config: AppConfig, cluster: str | None) -> HumioClient:
    """Create a HumioClient for the given (or default) cluster/deployment."""
    cluster_cfg = config.get_cluster(cluster)
    return HumioClient(cluster_cfg)


# ---------------------------------------------------------------------------
# Tool: list_dashboards
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_dashboards(
    repo: str,
    ctx: Context[ServerSession, AppContext],
    cluster: ClusterEnum = _default_cluster,  # type: ignore[assignment]
    search_filter: str = "",
) -> str:
    """List all dashboards in a Humio repository/view.

    Args:
        repo: The repository or view name in Humio.
        cluster: (Optional) Cluster name from config. Uses default if empty.
        search_filter: (Optional) Filter dashboards by name substring.

    Returns:
        JSON with dashboard id, name, description for each dashboard.
    """
    config: AppConfig = ctx.request_context.lifespan_context.config
    client = _make_client(config, cluster.value)  # type: ignore[union-attr]
    result = await client.list_dashboards(repo, search_filter or None)
    return result.model_dump_json(indent=2)


# ---------------------------------------------------------------------------
# Tool: get_dashboard_queries
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_dashboard_queries(
    repo: str,
    dashboard_name: str,
    ctx: Context[ServerSession, AppContext],
    cluster: ClusterEnum = _default_cluster,  # type: ignore[assignment]
) -> str:
    """Get all search queries from a specific Humio dashboard.

    Returns the query string, time range, and widget info for each widget
    in the dashboard.

    Args:
        repo: The repository or view name in Humio.
        dashboard_name: The exact name of the dashboard.
        cluster: (Optional) Cluster name from config. Uses default if empty.

    Returns:
        JSON containing each widget's query string, time range, title, and ID.
    """
    config: AppConfig = ctx.request_context.lifespan_context.config
    client = _make_client(config, cluster.value)  # type: ignore[union-attr]
    result = await client.get_dashboard_queries(repo, dashboard_name)
    return result.model_dump_json(indent=2)


# ---------------------------------------------------------------------------
# Tool: execute_search
# ---------------------------------------------------------------------------


@mcp.tool()
async def execute_search(
    query_string: Annotated[str, Field(description="The Humio search query string (e.g. 'error | count()')")],
    ctx: Context[ServerSession, AppContext],
    repo: Annotated[str, Field(description="The repository or view name to search. Most useful logs are in 'computecentral'.")] = "computecentral",
    start: Annotated[str, Field(description="Start time - relative ('24h', '7d') or ISO 8601.")] = "24h",
    end: Annotated[str, Field(description="End time - relative or ISO 8601. Use 'now' for current time.")] = "now",
    cluster: ClusterEnum = _default_cluster,  # type: ignore[assignment]
) -> str:
    """Execute a search query on Humio/LogScale and return results as JSON.

    IMPORTANT: Do not pull large amounts of raw logs. You are strongly encouraged
    to use LogScale's aggregation functions (e.g., `count()`, `groupBy()`, `timechart()`)
    in your `query_string` so that computation happens on the server.

    - Don't assume standard field names.
    - When field names are unknown: run a | head(5) first for a sample event to discover the schema.
    - Scope to a service, e.g. kubernetes.namespace_name = "ccprodusw2-ultra-api".
    - Regex works: kubernetes.namespace_name = /ccprodusw2-ultra/ matches multiple namespaces.
    - The actual application log line is in the log field. Multi-line tracebacks are split into individual events, one
      line per event — grouping them requires matching on trace ID or timestamp proximity.
    - groupBy([field1, field2], function=count()) works well for summarizing.
      Using function=[count(), min(field), max(field)] for multi-aggregates requires the fields to exist on the events —
      missing fields silently drop the aggregate.
    - sort(_count, order=desc) works; sort(field="min(start_time)") does not (the quoted string form caused 400 errors) —
      use sort(min(start_time)) without quotes.
    - | timechart(span=30s, function=count()) - it returns _bucket (epoch ms) and _count. Very efficient for quickly
      seeing when error volumes spiked without pulling raw events.
    - String literals in filter conditions need double quotes: kubernetes.container_name = "istio-proxy".
    - Free-text search works: "some string" (quoted) searches across all fields/rawstring.
    - OR conditions in free-text: (ERROR OR CRITICAL) — works but returns very large result sets;
      narrow with namespace/container filters first.
    - Avoid deeply nested multi-aggregation functions if you're unsure of field availability — they silently return no
      results rather than erroring.
    - Application errors/tracebacks go to stream = "stderr". Access logs go to stream = "stdout". Filtering by
      stream = "stderr" dramatically reduces noise when hunting for exceptions.
    - Supports both relative time ('24h', '7d', '30m') and ISO 8601 ('2024-01-01T00:00:00Z') for start/end.

    Returns:
        JSON with query results including events array and metadata.
    """
    config: AppConfig = ctx.request_context.lifespan_context.config
    client = _make_client(config, cluster.value)  # type: ignore[union-attr]
    result = await client.execute_search(
        repo=repo,
        query_string=query_string,
        start=start,
        end=end,
    )
    return result.model_dump_json(indent=2)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    """Run the HumioMCP server."""
    print(f"HumioMCP v{__version__} - MCP server for Humio/LogScale", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
