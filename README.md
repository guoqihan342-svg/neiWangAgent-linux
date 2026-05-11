# neiWangAgent v0.2.0

> 本地 MCP Agent — 自动改代码 → commit → push → 创建 MR
> 支持多语言：Java(Spring Boot+MyBatis) / Python(FastAPI) / Go(Gin) / TypeScript / Vue
> ★ v0.2.0: 内网零外网硬编码，全部走环境变量

![Version](https://img.shields.io/badge/version-0.2.0-blue)
![Python](https://img.shields.io/badge/python-3.10+-green)
![Tests](https://img.shields.io/badge/tests-86%2F86-brightgreen)

---

## 这是什么

neiWangAgent 是运行在本地的 AI 编程助手。给需求，自动完成：

1. **理解需求** — LLM 分析需求
2. **检索上下文** — 三层知识库(Summary/Hotspot/Deep) + MyBatis/Vue深度索引
3. **生成代码** — @@PATCH unified diff 优先，支持7种格式自动检测
4. **自审变更** — LLM审查语法/导入/逻辑/安全问题
5. **安全检查** — deny_paths + 命令黑名单(16项) + 路径遍历防护
6. **Git操作** — 创建 `agent/` 分支 → commit → push → 创建 MR
7. **运维闭环** — 每次运行生成 `report.md` + 预算控制

**100% MCP驱动，全部在本地运行。**

---

## 架构

```
CLI (Click) → Orchestrator (18步状态机 + 100% MCP驱动)
                ├── LLM Client     (OpenAI兼容, 预算控制, 代理)
                ├── Code Parser    (7格式 + apply_unified_diff引擎)
                ├── Tracer         (JSON Lines 结构化日志)
                ├── Config Loader  (多语言 + profiles + env解析)
                └── 6个 MCP Server (全部通过MCP接口调用)
                     ├── Knowledge      三层预理解 + MyBatis/Vue深度 + 持久化搜索
                     ├── Git           branch/commit/push(安全校验)
                     ├── MR             Provider模式(github/internal/mock)
                     ├── Database       DDL解析 + PostgreSQL只读验证 + 影响交叉分析
                     ├── Clarification  澄清问答(文件存档)
                     └── Requirement    需求读取/解析
```

---

## 快速开始

```bash
# 安装
pip install -e .

# 内网配置（不硬编码任何URL）
export LLM_BASE_URL="http://your-internal-llm:8080/v1"
export LLM_MODEL="your-model"
export LLM_API_KEY="your-key"

# 自测用（可选）
export GITHUB_TOKEN="ghp_xxx"        # 仅 github-demo profile 需要

# 使用
neiWangAgent init                    # 初始化（默认 internal profile）
neiWangAgent init --profile github-demo  # GitHub+DeepSeek自测
neiWangAgent warmup                  # 构建知识库
neiWangAgent run --task task.md      # 执行任务
neiWangAgent resume 20260511-123456  # 恢复中断（自动加载澄清答案）
```

---

## 版本历史

| 版本 | 日期 | 关键变更 |
|------|------|---------|
| **v0.2.0** | 2026-05-11 | P7运维闭环: Run report/Budget控制/MR增强/Knowledge自动重建 + 安全审查:移除所有硬编码外网URL + **P8代码优化: 去方法级import、硬编码URL清零** |
| v0.1.9 | 2026-05-11 | P6 Agent化: Self-review提交前自审 + Resume答案自动加载 + Dry-run模式 |
| v0.1.8 | 2026-05-11 | P5 填补stub: apply_unified_diff引擎 + DB影响交叉分析 + Knowledge失效检测 |
| v0.1.7 | 2026-05-11 | P4 安全验证: DB只读连接(PostgreSQL metadata_only) + 安全黑名单扩展(16项) |
| v0.1.6 | 2026-05-11 | P3 深度索引: Config profiles + 全MCP化 + MyBatis/Vue深度索引 |
| v0.1.5 | 2026-05-11 | P2 架构增强: 企业内网默认 + 通用API Key + Clarification落文件 + DDL解析 |
| v0.1.4 | 2026-05-11 | P1 MCP化: Orchestrator走MCP + MR Provider + Knowledge持久化 + @@PATCH |
| v0.1.3 | 2026-05-11 | P0 工程修复: pyproject/pip install + init + resume + 状态分级 |

---

## 安全红线

| 操作 | 状态 |
|------|------|
| 读写工作目录（受 deny_paths 限制） | ✅ |
| git commit（必须显式 files，禁止 add -A） | ✅ |
| git push agent/*（禁止 force push） | ✅ |
| 创建 MR（Provider: internal_mcp/github/mock） | ✅ |
| LLM 自审变更 | ✅ |
| push master/main/release/hotfix | ❌ |
| git merge / force push | ❌ |
| INSERT/UPDATE/DELETE/DROP/TRUNCATE | ❌ |
| 操作 .env / .ssh / *.pem / *.key | ❌ |
| sudo / rm -rf / chmod 777 / dd / fork bomb | ❌ |

---

## 环境变量

| 变量 | 说明 | 必填 |
|------|------|------|
| `LLM_BASE_URL` | LLM API 地址 | ✅ |
| `LLM_MODEL` | 模型名 | ✅ |
| `LLM_API_KEY` | API Key | ✅ |
| `GITHUB_TOKEN` | GitHub Token（仅 github profile） | — |
| `https_proxy` | 代理地址 | — |
| `DB_READONLY_ENABLED` | 启用DB只读验证 | — |
| `DB_HOST/PORT/NAME/USER/PASSWORD` | DB连接 | — |

---

## 相关仓库

| 仓库 | 说明 |
|------|------|
| [neiWangAgent](https://github.com/guoqihan342-svg/neiWangAgent) | Windows版 |
| [neiWangAgent-linux](https://github.com/guoqihan342-svg/neiWangAgent-linux) | Linux版 |
