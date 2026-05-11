"""
neiWangAgent — 本地无服务器 MCP Agent v0.1.8

自动改代码 → commit → push → 创建 MR。

架构（方案 v4）：
    CLI (cli.py) → Orchestrator (orchestrator.py) → LLM Client + MCP Servers

支持的 MCP Servers（v0.1）：
    - knowledge_server     三层预理解模型（Summary/Hotspot/Deep）
    - requirement_server   需求读取与解析
    - database_server      数据库文档索引（v0.1 只读）
    - git_server           版本控制（branch/commit/push）
    - mr_server            创建 Merge Request
    - clarification_server 澄清沟通

支持的项目类型（多语言）：
    - java       Spring Boot + MyBatis + Vue
    - python     FastAPI / Django + React
    - go         Gin / Echo + React
    - typescript Next.js / Express + React
    - generic    通用项目

核心特性：
    - 16步状态机驱动（INIT → WARMUP → ... → DONE）
    - 状态分级错误恢复（CRITICAL→STOP / OPTIONAL→降级 / HUMAN→暂停）
    - 真实 Git 操作（无 mock）
    - 结构化日志追踪（JSON Lines + 控制台双通道）
    - 工作区保护 + 变更护栏 + 安全边界
    - 多语言 deny_paths 自动适配

使用方式：
    export LLM_API_KEY='sk-xxx'      # LLM API Key
    agent init          # 初始化项目
    agent warmup        # 构建知识库
    agent run --task task.md   # 执行完整流程
    agent resume <run_id>      # 恢复执行
"""

from agent_mcp._version import __version__  # ★ P0-3: 版本号单一来源

__author__ = "neiWangAgent Team"

# 公开 API
from agent_mcp.config_loader import (
    load_config, ConfigLoader, AppConfig, ProjectType, LANGUAGE_DEFAULTS,
)
from agent_mcp.tracing import get_tracer, Tracer, trace_step
from agent_mcp.orchestrator import (
    Orchestrator, State, STATE_NAMES, RunState,
    CRITICAL_STATES, OPTIONAL_STATES, HUMAN_STATES,
)

__all__ = [
    "__version__",
    "load_config", "ConfigLoader", "AppConfig", "ProjectType", "LANGUAGE_DEFAULTS",
    "get_tracer", "Tracer", "trace_step",
    "Orchestrator", "State", "STATE_NAMES", "RunState",
    "CRITICAL_STATES", "OPTIONAL_STATES", "HUMAN_STATES",
]
