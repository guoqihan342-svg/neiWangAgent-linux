"""
config_loader.py — Typed configuration loader for neiWangAgent.

Reads config.yaml from the project root and provides Pydantic v2-validated,
fully typed access to every configuration section.

支持多语言项目：
  - Java (Spring Boot + MyBatis + Vue)
  - Python (FastAPI / Django + React)
  - Go (Gin / Echo + React)
  - TypeScript (Next.js / Express)
  - 通用 (Generic)

Usage:
    from agent_mcp.config_loader import load_config, ConfigLoader

    # One-shot load
    config = load_config()

    # Access typed config
    print(config.project.project_type)   # "java" | "python" | "go" | ...
    print(config.git.commit_message.template)  # commit 消息模板
    print(config.change_policy.deny_paths)     # 禁止修改的文件
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ============================================================================
# 项目类型枚举 — 支持多语言项目（方案 v4 扩展）
# ============================================================================

class ProjectType(str, Enum):
    """
    项目类型枚举。

    每种类型对应不同的文件结构、构建工具、代码索引策略。

    v0.1 支持：
      - java:    Spring Boot + MyBatis/MyBatis-Plus + Vue 前端
      - python:  FastAPI / Django + React / Vue 前端
      - go:      Gin / Echo + React 前端
      - typescript: Next.js / Express + React 前端
      - generic: 通用项目（不限定语言）
    """
    JAVA = "java"
    PYTHON = "python"
    GO = "go"
    TYPESCRIPT = "typescript"
    GENERIC = "generic"


# ============================================================================
# 各语言默认配置 — deny_paths / 源码路径 / 构建工具
# ============================================================================

LANGUAGE_DEFAULTS: dict[ProjectType, dict] = {
    ProjectType.JAVA: {
        "source_dirs": ["src/main/java", "src/main/resources"],
        "test_dirs": ["src/test/java"],
        "frontend_dir": "src/main/frontend",
        "build_tool": "maven",  # maven | gradle
        "orm": ["mybatis", "mybatis_plus"],
        "config_files": ["pom.xml", "application*.yml", "application*.yaml"],
        "deny_paths": [
            "pom.xml", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            "src/main/resources/application*.yml",
            "src/main/resources/application*.yaml",
            "Dockerfile", "docker-compose*.yml",
        ],
    },
    ProjectType.PYTHON: {
        "source_dirs": ["src", "app", "api"],
        "test_dirs": ["tests", "test"],
        "frontend_dir": "frontend",
        "build_tool": "pip",  # pip | poetry | uv
        "orm": ["sqlalchemy", "django_orm"],
        "config_files": ["pyproject.toml", "requirements.txt", ".env"],
        "deny_paths": [
            "pyproject.toml", "requirements.txt", "poetry.lock",
            "Dockerfile", "docker-compose*.yml", ".env", "*.pem", "*.key",
            "alembic/versions/*",
        ],
    },
    ProjectType.GO: {
        "source_dirs": ["cmd", "internal", "pkg"],
        "test_dirs": ["test"],
        "frontend_dir": "web",
        "build_tool": "go_modules",  # go_modules
        "orm": ["gorm"],
        "config_files": ["go.mod", "go.sum"],
        "deny_paths": [
            "go.mod", "go.sum", "Dockerfile", "docker-compose*.yml",
        ],
    },
    ProjectType.TYPESCRIPT: {
        "source_dirs": ["src", "app", "pages", "components"],
        "test_dirs": ["__tests__", "tests", "spec"],
        "frontend_dir": ".",  # 前后端一体
        "build_tool": "npm",  # npm | yarn | pnpm | bun
        "orm": ["prisma", "drizzle", "typeorm"],
        "config_files": ["package.json", "tsconfig.json", "next.config.*"],
        "deny_paths": [
            "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            "tsconfig.json", "Dockerfile", "docker-compose*.yml", ".env",
        ],
    },
    ProjectType.GENERIC: {
        "source_dirs": ["src", "lib"],
        "test_dirs": ["tests", "test"],
        "frontend_dir": "frontend",
        "build_tool": "unknown",
        "orm": [],
        "config_files": [],
        "deny_paths": [
            ".env", "*.pem", "*.key", "Dockerfile", "docker-compose*.yml",
        ],
    },
}


# ============================================================================
# Helper: discover the project root (where config.yaml lives)
# ============================================================================

def _find_project_root() -> Path:
    """Walk upward from this file's directory until we find config.yaml."""
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
    """``project:`` section — 项目基本信息 + 语言类型。"""

    name: str = "neiWangAgent"
    default_branch: str = "main"
    code_platform: str = "internal_custom"  # ★ P2-11: 默认企业内网
    # ★ 新增：项目类型（决定源码路径、ORM、deny_paths 等默认值）
    project_type: ProjectType = ProjectType.GENERIC

    @property
    def lang_defaults(self) -> dict:
        """获取当前项目类型的默认配置。"""
        return LANGUAGE_DEFAULTS.get(self.project_type, LANGUAGE_DEFAULTS[ProjectType.GENERIC])


class RuntimeConfig(BaseModel):
    """``runtime:`` section — LLM 和 MCP 运行时参数。

    ★ P2-11: 默认值改为企业内网环境变量模式
      - llm_base_url 默认读取 LLM_BASE_URL 环境变量
      - llm_model 默认读取 LLM_MODEL 环境变量
      - llm_api_key_env 指定 API Key 环境变量名（默认 LLM_API_KEY）
    """

    mode: str = "local_mcp"
    transport: str = "stdio"
    llm_timeout_seconds: int = Field(default=120, ge=1)
    mcp_timeout_seconds: int = Field(default=30, ge=1)
    # ★ P2-11: 默认走环境变量而非硬编码 DeepSeek
    llm_base_url: str = "${LLM_BASE_URL}"
    llm_model: str = "${LLM_MODEL}"
    # ★ P2-11: API Key 环境变量名（可配置为 LLM_API_KEY / DEEPSEEK_API_KEY 等）
    llm_api_key_env: str = "LLM_API_KEY"
    llm_base_url_env: str = "LLM_BASE_URL"
    # ★ 新增：并发控制
    max_concurrent_mcp_calls: int = Field(default=5, ge=1)
    max_retries: int = Field(default=3, ge=0)


class TaskConfig(BaseModel):
    """``task:`` section — 任务执行策略。"""

    stop_after_create_mr: bool = True
    enable_tests: bool = False
    enable_self_review: bool = False
    max_clarification_rounds: int = Field(default=3, ge=0)
    max_questions_per_round: int = Field(default=5, ge=1)
    # ★ 新增：预算控制
    budget_cents: int = Field(default=20, ge=0)  # 每次运行 LLM 费用上限（分）


class MCPServerSpec(BaseModel):
    """单个 MCP server 的启动配置。"""

    command: str
    args: list[str] = Field(default_factory=list)


class MCPConfig(BaseModel):
    """``mcp:`` section — MCP Server 注册表。"""

    servers: dict[str, MCPServerSpec] = Field(default_factory=dict)


# ============================================================================
# Knowledge — 三层预理解模型（方案 v4 §4）
# ============================================================================


class _KnowledgeLayerSummary(BaseModel):
    """Summary 层：每次 run 前刷新。"""

    auto_refresh_before_run: bool = True
    diff_base_strategy: str = "merge_base"
    target_branch: str = "main"
    max_recent_commits: int = Field(default=50, ge=1)


class _KnowledgeLayerHotspot(BaseModel):
    """Hotspot 层：核心模块分析。"""

    build_on_warmup: bool = True
    auto_refresh_interval: str = "24h"
    max_commits_to_scan: int = Field(default=100, ge=1)
    core_modules: list[str] = Field(default_factory=list)
    blame_key_files: list[str] = Field(default_factory=list)
    key_commit_patterns: list[str] = Field(
        default_factory=lambda: ["schema", "refactor", "API", "migrate"]
    )


class _KnowledgeLayerDeep(BaseModel):
    """Deep 层：深度代码索引。"""

    build_on_first_warmup: bool = True
    rebuild_trigger: str = "manual"


class KnowledgeInvalidation(BaseModel):
    """★ 新增：知识库失效规则（方案 v4 §4.3）"""

    rebuild_summary_when: list[str] = Field(
        default_factory=lambda: ["always_before_run"]
    )
    rebuild_hotspot_when: list[str] = Field(
        default_factory=lambda: [
            "target_branch_changed",
            "files_changed_in_core_modules",
            "ddl_changed",
            "mapper_xml_changed",
            "pom_xml_changed",
            "package_json_changed",
        ]
    )
    rebuild_deep_when: list[str] = Field(
        default_factory=lambda: [
            "new_service_detected",
            "database_schema_changed",
            "route_structure_changed",
            "mybatis_mapping_structure_changed",
        ]
    )


class KnowledgeLayers(BaseModel):
    """三层知识库配置。"""

    summary: _KnowledgeLayerSummary = Field(default_factory=_KnowledgeLayerSummary)
    hotspot: _KnowledgeLayerHotspot = Field(default_factory=_KnowledgeLayerHotspot)
    deep: _KnowledgeLayerDeep = Field(default_factory=_KnowledgeLayerDeep)


class KnowledgeConfig(BaseModel):
    """``knowledge:`` section — 知识库完整配置。"""

    layers: KnowledgeLayers = Field(default_factory=KnowledgeLayers)
    business_docs_dir: str = "./business-docs"
    # ★ 新增
    invalidation: KnowledgeInvalidation = Field(default_factory=KnowledgeInvalidation)
    # ★ 新增：多语言源文件扩展名
    source_extensions: list[str] = Field(
        default_factory=lambda: [".java", ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".vue"]
    )
    doc_extensions: list[str] = Field(
        default_factory=lambda: [".md", ".txt", ".yaml", ".yml", ".json", ".sql"]
    )


# ============================================================================
# Database — v0.1 只读文档索引（方案 v4 §7）
# ============================================================================


class _DBWriteConnection(BaseModel):
    """写连接配置 — v4 明确：永远禁用。"""

    enabled: bool = False
    current_version_behavior: str = "never_execute_write_sql"


class _DBReadonlyVerify(BaseModel):
    """v0.2 预留：只读连接验证。"""

    enabled: bool = False
    default_mode: str = "metadata_only"
    enforce_transaction_read_only: bool = True
    statement_timeout_ms: int = Field(default=5000, ge=100)
    max_rows_limit: int = Field(default=100, ge=1)
    allowed_schemas: list[str] = Field(default_factory=lambda: ["public", "app"])
    denied_tables: list[str] = Field(
        default_factory=lambda: ["user_password", "auth_token", "session", "secret"]
    )
    denied_columns: list[str] = Field(
        default_factory=lambda: ["password", "token", "secret", "id_card", "phone", "email"]
    )


class _DBMigration(BaseModel):
    """Migration 策略 — 只生成草稿，不执行。"""

    allow_generate_draft: bool = True
    allow_execute: bool = False
    draft_output_dir: str = ".agent/runs/{run_id}/database/migration-drafts"
    require_dba_review: bool = True


class DatabaseConfig(BaseModel):
    """``database:`` section — 完整数据库策略。"""

    enabled: bool = False
    mode: str = "local_docs_and_code_only"  # local_docs_and_code_only | readonly_verify
    # ★ 新增：数据库类型
    db_type: str = "postgresql"  # postgresql | mysql
    orm: list[str] = Field(default_factory=lambda: ["mybatis", "mybatis_plus"])
    # ★ 子配置
    write_connection: _DBWriteConnection = Field(default_factory=_DBWriteConnection)
    readonly_verify: _DBReadonlyVerify = Field(default_factory=_DBReadonlyVerify)
    migration: _DBMigration = Field(default_factory=_DBMigration)
    # ★ 文档路径
    ddl_dir: str = "./business-docs/database/ddl"
    data_dictionary_dir: str = "./business-docs/database/data-dictionary"
    # ★ MyBatis 路径
    mapper_xml_paths: list[str] = Field(
        default_factory=lambda: ["src/main/resources/mapper/**/*.xml"]
    )
    entity_paths: list[str] = Field(
        default_factory=lambda: ["src/main/java/**/*Entity.java"]
    )


# ============================================================================
# Git — 完整分支/提交策略（方案 v4 §8）
# ============================================================================


class _BranchNaming(BaseModel):
    """分支命名规范。"""

    template: str = "agent/{yyyyMMdd}-{task_slug}"
    regex: str = "^agent/[0-9]{8}-[a-z0-9][a-z0-9._-]{2,80}$"


class _WorktreePolicy(BaseModel):
    """工作区保护策略。"""

    require_clean_before_run: bool = True
    allow_untracked: bool = False
    on_dirty: str = "stop_and_ask"  # stop_and_ask | auto_stash | ignore


class _CommitMessage(BaseModel):
    """★ 新增：Commit Message 模板。"""

    template: str = "{type}: {summary}"
    regex: str = r"^(feat|fix|refactor|chore|docs|style|perf|revert)(\([A-Za-z0-9._-]+\))?: .{1,100}$"


class _PushPolicy(BaseModel):
    """★ 新增：Push 策略。"""

    allowed_branch_regex: str = "^agent/[A-Za-z0-9._/-]+$"
    denied_branch_regex: str = "^(master|main|release/.*|hotfix/.*)$"


class GitConfig(BaseModel):
    """``git:`` section — 完整版本控制策略。"""

    target_branch: str = "main"
    diff_base_strategy: str = "merge_base"  # ★ 新增
    branch_prefix: str = "agent/"
    branch_naming: _BranchNaming = Field(default_factory=_BranchNaming)
    worktree_policy: _WorktreePolicy = Field(default_factory=_WorktreePolicy)
    commit_message: _CommitMessage = Field(default_factory=_CommitMessage)  # ★ P0修复
    push_policy: _PushPolicy = Field(default_factory=_PushPolicy)  # ★ 新增
    allow_commit: bool = True
    allow_push: bool = True
    allow_create_mr: bool = True
    allow_merge: bool = False
    allow_force_push: bool = False
    protected_branches: list[str] = Field(
        default_factory=lambda: ["master", "main", "release/*", "hotfix/*"]
    )


# ============================================================================
# Change Policy — 变更范围护栏（方案 v4 §8.2）
# ============================================================================


class _ChangeClarificationTriggers(BaseModel):
    """★ 新增：需要澄清的变更场景。"""

    dependency_file_changed: bool = True
    config_file_changed: bool = True
    auth_module_changed: bool = True
    permission_module_changed: bool = True
    database_schema_changed: bool = True
    more_than_max_files_changed: bool = True
    more_than_max_lines_changed: bool = True


class ChangePolicyConfig(BaseModel):
    """``change_policy:`` section — 变更范围护栏。"""

    max_files_changed: int = Field(default=20, ge=1)
    max_lines_changed: int = Field(default=800, ge=1)
    # ★ 扩展：支持 glob 模式匹配
    deny_paths: list[str] = Field(default_factory=list)
    deny_path_globs: list[str] = Field(default_factory=list)  # ★ 新增
    # ★ 新增
    require_clarification_when: _ChangeClarificationTriggers = Field(
        default_factory=_ChangeClarificationTriggers
    )


# ============================================================================
# MR — Merge Request 配置（方案 v4 §12）
# ============================================================================


class MRConfig(BaseModel):
    """★ 新增：``mr:`` section。"""

    provider: str = "internal_mcp"  # ★ P2-11: 默认企业内网 | github | mock
    target_branch: str = "main"
    title_template: str = "[Agent] {task_title}"
    description_template: str = ".agent/templates/mr_description.md"
    # MR 描述中强制标记"未测试"
    require_untested_marker: bool = True


# ============================================================================
# Clarification — 澄清配置（方案 v4 §11）
# ============================================================================


class _ClarificationAskWhen(BaseModel):
    """★ 新增：触发澄清的条件。"""

    requirement_conflict: bool = True
    missing_api_contract: bool = True
    business_rule_ambiguous: bool = True
    permission_or_status_flow_ambiguous: bool = True
    data_model_change_required: bool = True
    database_schema_unclear: bool = True
    enum_value_unclear: bool = True
    unclear_target_module: bool = True
    dependency_file_changed: bool = True
    exceeds_change_scope: bool = True


class ClarificationConfig(BaseModel):
    """★ 新增：``clarification:`` section。"""

    enabled: bool = True
    default_mode: str = "manual_copy"  # manual_copy | clipboard | file
    max_questions_per_round: int = Field(default=5, ge=1)
    max_clarification_rounds: int = Field(default=3, ge=0)
    ask_when: _ClarificationAskWhen = Field(default_factory=_ClarificationAskWhen)


# ============================================================================
# Retrieval Weights — 检索权重（方案 v4 §11）
# ============================================================================


class RetrievalWeights(BaseModel):
    """★ 新增：``retrieval_weights:`` — 不同来源的检索权重。"""

    current_code: float = Field(default=1.00, ge=0, le=2)
    directly_referenced_files: float = Field(default=1.00, ge=0, le=2)
    business_docs: float = Field(default=0.90, ge=0, le=2)
    database_schema_docs: float = Field(default=0.90, ge=0, le=2)
    database_entity_mapping: float = Field(default=0.88, ge=0, le=2)
    java_controller: float = Field(default=0.85, ge=0, le=2)
    java_service: float = Field(default=0.85, ge=0, le=2)
    java_mapper: float = Field(default=0.85, ge=0, le=2)
    java_entity: float = Field(default=0.85, ge=0, le=2)
    vue_route: float = Field(default=0.80, ge=0, le=2)
    vue_api_client: float = Field(default=0.80, ge=0, le=2)
    vue_component: float = Field(default=0.75, ge=0, le=2)
    # ★ 新增：其他语言的检索权重
    python_route: float = Field(default=0.85, ge=0, le=2)
    python_service: float = Field(default=0.85, ge=0, le=2)
    python_model: float = Field(default=0.85, ge=0, le=2)
    go_handler: float = Field(default=0.85, ge=0, le=2)
    go_model: float = Field(default=0.85, ge=0, le=2)
    ts_component: float = Field(default=0.80, ge=0, le=2)
    ts_api_route: float = Field(default=0.85, ge=0, le=2)
    # 通用
    historical_mr_description: float = Field(default=0.65, ge=0, le=2)
    code_comments: float = Field(default=0.55, ge=0, le=2)
    commit_diff_history: float = Field(default=0.45, ge=0, le=2)
    file_path_match: float = Field(default=0.45, ge=0, le=2)
    freshness: float = Field(default=0.40, ge=0, le=2)
    commit_message: float = Field(default=0.20, ge=0, le=2)


# ============================================================================
# Security — 安全边界（方案 v4 §9）
# ============================================================================


class SecurityConfig(BaseModel):
    """``security:`` section — 安全边界配置。"""

    allowed_paths: list[str] = Field(
        default_factory=lambda: [".", "./business-docs", "./.agent"]
    )
    deny_paths: list[str] = Field(
        default_factory=lambda: [
            "~/.ssh", "~/.git-credentials", ".env", "*.pem", "*.key",
            "*.p12", "*.jks", "*.keystore", "id_rsa*",
        ]
    )
    deny_path_globs: list[str] = Field(
        default_factory=lambda: [
            "**/.ssh/**", "**/credentials", "**/*.pem", "**/*.key",
        ]
    )
    blocked_commands: list[str] = Field(
        default_factory=lambda: [
            "sudo", "rm -rf /", "kubectl", "terraform apply",
            "shutdown", "reboot", "mkfs",
        ]
    )
    blocked_command_patterns: list[str] = Field(
        default_factory=lambda: [
            r"rm\s+-rf\s+/", r">\s*/dev/sda", r"mkfs\.", r"dd\s+if=",
        ]
    )
    # ★ 新增：文件操作检查
    check_file_reads: bool = True
    check_file_writes: bool = True
    max_file_size_bytes: int = Field(default=10 * 1024 * 1024, ge=1)  # 10MB


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
    # ★ 新增 section
    mr: MRConfig = Field(default_factory=MRConfig)
    clarification: ClarificationConfig = Field(default_factory=ClarificationConfig)
    retrieval_weights: RetrievalWeights = Field(default_factory=RetrievalWeights)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("git")
    @classmethod
    def _enforce_target_branch_protected(cls, v: GitConfig) -> GitConfig:
        """确保 target_branch 在 protected_branches 中。"""
        if v.target_branch not in v.protected_branches:
            v.protected_branches = list(dict.fromkeys([*v.protected_branches, v.target_branch]))
        return v

    @model_validator(mode="after")
    def _merge_deny_paths(self) -> "AppConfig":
        """合并 change_policy.deny_paths 和项目类型的默认 deny_paths。"""
        lang_defaults = self.project.lang_defaults
        default_deny = lang_defaults.get("deny_paths", [])
        # 项目类型默认值补充到 change_policy（不覆盖已有）
        existing = set(self.change_policy.deny_paths)
        for path in default_deny:
            if path not in existing:
                self.change_policy.deny_paths.append(path)
        return self

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, raw: dict[str, Any]) -> "AppConfig":
        """Construct an ``AppConfig`` from a raw dict (already parsed from YAML)."""
        return cls(**raw)

    # ------------------------------------------------------------------
    # ★ 新增：便捷方法
    # ------------------------------------------------------------------

    def get_deny_paths_flat(self) -> list[str]:
        """获取完整的禁止路径列表（change_policy + security 合并）。"""
        return list(dict.fromkeys(
            self.change_policy.deny_paths + self.security.deny_paths
        ))

    def is_path_denied(self, path: str) -> bool:
        """检查路径是否在禁止列表中（简单 glob 匹配）。"""
        from fnmatch import fnmatch
        for pattern in self.get_deny_paths_flat():
            if fnmatch(path, pattern) or fnmatch(Path(path).name, pattern):
                return True
        return False

    def is_language(self, lang: ProjectType) -> bool:
        """检查当前项目是否属于指定语言类型。"""
        return self.project.project_type == lang


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

    ★ 每个实例独立缓存，避免不同项目配置文件互相污染。
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        if config_path is not None:
            self._config_path = Path(config_path)
        else:
            self._config_path = PROJECT_ROOT / "config.yaml"
        self._cached_config: Optional[AppConfig] = None  # ★ 实例级缓存

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def config(self) -> AppConfig:
        if self._cached_config is None:
            self._cached_config = self._load()
        return self._cached_config

    def reload(self) -> AppConfig:
        self._cached_config = self._load()
        return self._cached_config

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
# Quick smoke-test
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
    print(f"  Project:  {cfg.project.name}  (type={cfg.project.project_type})")
    print(f"  Runtime:  mode={cfg.runtime.mode}  model={cfg.runtime.llm_model}")
    print(f"  MCP servers: {list(cfg.mcp.servers.keys())}")
    print(f"  Knowledge layers: {list(cfg.knowledge.layers.model_dump().keys())}")
    print(f"  Git target: {cfg.git.target_branch}  commit_template={cfg.git.commit_message.template}")
    print(f"  Change: max_files={cfg.change_policy.max_files_changed}  deny_paths={cfg.change_policy.deny_paths}")
    print(f"  MR: provider={cfg.mr.provider}")
    print(f"  Security: blocked_commands={cfg.security.blocked_commands}")
    print(f"  Deny paths (merged): {cfg.get_deny_paths_flat()}")
    print()
