"""Configuration loading for HumioMCP."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from enum import EnumMeta

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import]
    except ImportError:
        import tomli as tomllib  # type: ignore[import,no-redef]


@dataclass
class ClusterConfig:
    """Configuration for a single Humio cluster."""

    name: str
    url: str
    token: str
    skip_ssl_verify: bool = False
    com_deployments: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    """Application-level configuration."""

    default_cluster: str
    clusters: dict[str, ClusterConfig] = field(default_factory=dict)

    def get_cluster(self, name: str | None = None) -> ClusterConfig:
        """Get cluster config by name or com_deployment alias, or return the default."""
        cluster_name = name or self.default_cluster

        # Direct cluster name match
        if cluster_name in self.clusters:
            return self.clusters[cluster_name]

        # com_deployment alias match
        for cluster_cfg in self.clusters.values():
            if cluster_name in cluster_cfg.com_deployments:
                return cluster_cfg

        available_clusters = ", ".join(self.clusters.keys()) or "(none)"
        all_deployments = [
            d for c in self.clusters.values() for d in c.com_deployments
        ]
        available_deployments = ", ".join(all_deployments) or "(none)"
        raise ValueError(
            f"Unknown cluster or deployment '{cluster_name}'. "
            f"Clusters: {available_clusters}. "
            f"Deployments: {available_deployments}"
        )


def _sanitize_enum_name(s: str) -> str:
    """Convert an arbitrary string into a valid Python identifier for an Enum member."""
    # Replace hyphens and other non-alphanumeric chars with underscores
    sanitized = re.sub(r"[^a-zA-Z0-9]", "_", s)
    # Ensure it doesn't start with a digit
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized


def build_cluster_enum(config: AppConfig) -> EnumMeta:
    """Build a dynamic str-Enum from config covering all cluster names and com_deployment aliases.

    Enum member values are the original strings (e.g. "qa", "dev-alpha") so that
    pydantic exposes them verbatim in the JSON schema.  Passing any of these values
    to AppConfig.get_cluster() will resolve to the correct underlying cluster.
    """
    members: dict[str, str] = {}

    for cluster_name, cluster_cfg in config.clusters.items():
        key = _sanitize_enum_name(cluster_name)
        members[key] = cluster_name

        for deployment in cluster_cfg.com_deployments:
            dep_key = _sanitize_enum_name(deployment)
            members[dep_key] = deployment

    ClusterEnum = Enum("ClusterEnum", members, type=str)  # type: ignore[misc]
    return ClusterEnum  # type: ignore[return-value]


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load configuration from a TOML file.

    Search order:
      1. Explicit path
      2. HUMIO_MCP_CONFIG env var
      3. ./config.toml
      4. ~/.config/humio-mcp/config.toml
    """
    import os

    if config_path is None:
        config_path = os.environ.get("HUMIO_MCP_CONFIG")

    candidates: list[Path] = []
    if config_path is not None:
        candidates.append(Path(config_path))
    else:
        candidates.append(Path("config.toml"))
        candidates.append(Path.home() / ".config" / "humio-mcp" / "config.toml")

    resolved: Path | None = None
    for p in candidates:
        if p.is_file():
            resolved = p
            break

    if resolved is None:
        searched = ", ".join(str(p) for p in candidates)
        raise FileNotFoundError(
            f"No config.toml found. Searched: {searched}. "
            "Copy config.example.toml to config.toml and fill in your credentials."
        )

    with open(resolved, "rb") as f:
        raw = tomllib.load(f)

    default_cluster = raw.get("default_cluster", "")
    clusters_raw: dict = raw.get("clusters", {})

    clusters: dict[str, ClusterConfig] = {}
    for name, info in clusters_raw.items():
        clusters[name] = ClusterConfig(
            name=name,
            url=info["url"].rstrip("/"),
            token=info["token"],
            skip_ssl_verify=info.get("skip_ssl_verify", False),
            com_deployments=info.get("com_deployments", []),
        )

    if not clusters:
        raise ValueError("No clusters defined in config.toml")

    if default_cluster and default_cluster not in clusters:
        raise ValueError(
            f"default_cluster '{default_cluster}' not found in [clusters]"
        )

    # If no default specified, use the first cluster
    if not default_cluster:
        default_cluster = next(iter(clusters))

    return AppConfig(default_cluster=default_cluster, clusters=clusters)
