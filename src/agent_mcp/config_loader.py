"""
config_loader.py — Typed configuration loader for neiWangAgent.

Reads config.yaml from the project root and provides Pydantic v2-validated,
fully typed access to every configuration section.

Usage:
    from agent_mcp.config_loader import load_config, ConfigLoader

    # One-shot load
    config = load_config()

    # Or keep a loader instance (caches the parsed config)
    loader = ConfigLoader()
    print(loader.config.runtime.llm_model)
    print(loader.config.git.protected_branches)

    # Reload on demand
    loader.reload()
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Helper: discover the project root (where config.yaml lives)
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    """Walk upward from this file's directory until we find config.yaml.

    Tries ``__file__`` first (works when imported as a module), then falls
    back to ``Path.cwd()`` (works in REPLs / ``python -c``).
    """
    try:
        start = Path(__file__).resolve().parent
    except NameError:
        start = Path.cwd()

    for directory in [start, *start.parents]:
        candidate = directory / "config.yaml"
        if candidate.is_file():
            return directory

    raise FileNotFoundError("config.yaml not found in any ancestor directory")


PROJECT_ROOT: Path = _find_project_root()


# ============================================================================
# Pydantic models — one per YAML section
# ============================================================================


class ProjectConfig(BaseModel):
    """``project:`` section."""

    name: str = "neiWangAgent"
    default_branch: str = "master"
    code_platform: str = "github"


class RuntimeConfig(BaseModel):
    """``runtime:`` section."""

    mode: str = "local_mcp"
    transport: str = "stdio"
    llm_timeout_seconds: int = Field(default=120, ge=1)
    mcp_timeout_seconds: int = Field(default=30, ge=1)
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-v4-flash"


class TaskConfig(BaseModel):
    """``task:`` section."""

    stop_after_create_mr: bool = True
    enable_tests: bool = False
    enable_self_review: bool = False
    max_clarification_rounds: int = Field(default=3, ge=0)
    max_questions_per_round: int = Field(default=5, ge=1)


class MCPServerSpec(BaseModel):
    """A single MCP server entry inside ``mcp.servers``."""

    command: str
    args: list[str] = Field(default_factory=list)


class MCPConfig(BaseModel):
    """``mcp:`` section."""

    servers: dict[str, MCPServerSpec] = Field(default_factory=dict)


# --- Knowledge ---


class _KnowledgeLayerSummary(BaseModel):
    auto_refresh_before_run: bool = True
    diff_base_strategy: str = "merge_base"
    target_branch: str = "master"
    max_recent_commits: int = Field(default=50, ge=1)


class _KnowledgeLayerHotspot(BaseModel):
    build_on_warmup: bool = True
    auto_refresh_interval: str = "24h"
    max_commits_to_scan: int = Field(default=100, ge=1)
    core_modules: list[str] = Field(default_factory=list)
    blame_key_files: list[str] = Field(default_factory=list)


class _KnowledgeLayerDeep(BaseModel):
    build_on_first_warmup: bool = True
    rebuild_trigger: str = "manual"


class KnowledgeLayers(BaseModel):
    """The three knowledge layers."""

    summary: _KnowledgeLayerSummary = Field(default_factory=_KnowledgeLayerSummary)
    hotspot: _KnowledgeLayerHotspot = Field(default_factory=_KnowledgeLayerHotspot)
    deep: _KnowledgeLayerDeep = Field(default_factory=_KnowledgeLayerDeep)


class KnowledgeConfig(BaseModel):
    """``knowledge:`` section."""

    layers: KnowledgeLayers = Field(default_factory=KnowledgeLayers)
    business_docs_dir: str = "./business-docs"


# --- Database ---


class _DBWriteConnection(BaseModel):
    enabled: bool = False
    current_version_behavior: str = "never_execute_write_sql"


class DatabaseConfig(BaseModel):
    """``database:`` section."""

    enabled: bool = False
    mode: str = "local_docs_and_code_only"
    write_connection: _DBWriteConnection = Field(default_factory=_DBWriteConnection)


# --- Git ---


class _BranchNaming(BaseModel):
    template: str = "agent/{yyyyMMdd}-{task_slug}"


class _WorktreePolicy(BaseModel):
    require_clean_before_run: bool = True
    allow_untracked: bool = False


class GitConfig(BaseModel):
    """``git:`` section."""

    target_branch: str = "master"
    branch_prefix: str = "agent/"
    branch_naming: _BranchNaming = Field(default_factory=_BranchNaming)
    worktree_policy: _WorktreePolicy = Field(default_factory=_WorktreePolicy)
    allow_commit: bool = True
    allow_push: bool = True
    allow_create_mr: bool = True
    allow_merge: bool = False
    allow_force_push: bool = False
    protected_branches: list[str] = Field(default_factory=lambda: ["master", "main"])


# --- Change Policy ---


class ChangePolicyConfig(BaseModel):
    """``change_policy:`` section."""

    max_files_changed: int = Field(default=20, ge=1)
    max_lines_changed: int = Field(default=800, ge=1)
    deny_paths: list[str] = Field(default_factory=list)


# --- Security ---


class SecurityConfig(BaseModel):
    """``security:`` section."""

    allowed_paths: list[str] = Field(default_factory=lambda: [".", "./business-docs", "./.agent"])
    deny_paths: list[str] = Field(default_factory=lambda: ["~/.ssh", "~/.git-credentials", ".env", "*.pem", "*.key"])
    blocked_commands: list[str] = Field(default_factory=list)


# ============================================================================
# Top-level aggregate model
# ============================================================================


class AppConfig(BaseModel):
    """Aggregate of every config section — mirrors the shape of ``config.yaml``."""

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    task: TaskConfig = Field(default_factory=TaskConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    change_policy: ChangePolicyConfig = Field(default_factory=ChangePolicyConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    # ------------------------------------------------------------------
    # Top-level convenience validators
    # ------------------------------------------------------------------

    @field_validator("git")
    @classmethod
    def _enforce_target_branch_protected(cls, v: GitConfig) -> GitConfig:
        if v.target_branch not in v.protected_branches:
            # Make sure the target branch is implicitly protected
            v.protected_branches = list(dict.fromkeys([*v.protected_branches, v.target_branch]))
        return v

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, raw: dict[str, Any]) -> "AppConfig":
        """Construct an ``AppConfig`` from a raw dict (already parsed from YAML)."""
        return cls(**raw)


# ============================================================================
# Config loader — the public API
# ============================================================================


class ConfigLoader:
    """Loads ``config.yaml``, validates with Pydantic v2, caches the result.

    Typical usage::

        loader = ConfigLoader()
        cfg = loader.config           # typed AppConfig

        # Reload (e.g. after config was edited at runtime)
        loader.reload()

    If ``config_path`` is omitted the loader walks up from ``src/agent_mcp/``
    to the project root (the directory that contains ``config.yaml``).
    """

    # Cache the fully-validated config (class-level by default so load_config()
    # and instances share it)
    _cached_config: ClassVar[Optional[AppConfig]] = None
    _cached_path: ClassVar[Optional[Path]] = None

    def __init__(self, config_path: str | Path | None = None) -> None:
        if config_path is not None:
            self._config_path = Path(config_path)
        else:
            self._config_path = PROJECT_ROOT / "config.yaml"

    # -- public properties ---------------------------------------------------

    @property
    def config_path(self) -> Path:
        """Absolute path to the YAML file being read."""
        return self._config_path

    @property
    def config(self) -> AppConfig:
        """Return the validated :class:`AppConfig` (lazy-loaded & cached)."""
        if ConfigLoader._cached_config is None or ConfigLoader._cached_path != self._config_path:
            ConfigLoader._cached_config = self._load()
            ConfigLoader._cached_path = self._config_path
        return ConfigLoader._cached_config

    # -- reload --------------------------------------------------------------

    def reload(self) -> AppConfig:
        """Force re-read and re-validate the YAML file."""
        ConfigLoader._cached_config = self._load()
        ConfigLoader._cached_path = self._config_path
        return ConfigLoader._cached_config

    # -- internal ------------------------------------------------------------

    def _load(self) -> AppConfig:
        raw = self._read_yaml()
        return AppConfig.from_yaml(raw)

    def _read_yaml(self) -> dict[str, Any]:
        if not self._config_path.is_file():
            raise FileNotFoundError(f"config.yaml not found at {self._config_path}")

        with open(self._config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if not isinstance(data, dict):
            raise ValueError(f"config.yaml must be a mapping, got {type(data).__name__}")

        return data


# ============================================================================
# Module-level convenience function
# ============================================================================


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """One-shot helper: load and return the typed config.

    Args:
        config_path: Optional explicit path.  Defaults to the config.yaml
            in the project root (auto-discovered).
    """
    loader = ConfigLoader(config_path)
    return loader.config


# ============================================================================
# Quick smoke-test when run as ``python -m agent_mcp.config_loader``
# ============================================================================

if __name__ == "__main__":
    import json
    import sys

    try:
        cfg = load_config()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print("✅ Config loaded successfully\n")
    print(f"  Project:  {cfg.project.name}  (branch={cfg.project.default_branch})")
    print(f"  Runtime:  mode={cfg.runtime.mode}  model={cfg.runtime.llm_model}")
    print(f"  MCP servers: {list(cfg.mcp.servers.keys())}")
    print(f"  Knowledge layers: {list(cfg.knowledge.layers.model_dump().keys())}")
    print(f"  Git target: {cfg.git.target_branch}  protected={cfg.git.protected_branches}")
    print(f"  Change: max_files={cfg.change_policy.max_files_changed}  max_lines={cfg.change_policy.max_lines_changed}")
    print(f"  Security: blocked_commands={cfg.security.blocked_commands}")
    print()
