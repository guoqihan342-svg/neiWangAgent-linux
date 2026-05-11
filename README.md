# neiWangAgent v0.1.2

> 本地无服务器 MCP Agent — 自动改代码 → commit → push → 创建 MR
> 支持多语言项目：Java / Python / Go / TypeScript / Vue

![Version](https://img.shields.io/badge/version-0.1.2-blue)
![Python](https://img.shields.io/badge/python-3.10+-green)
![License](https://img.shields.io/badge/license-MIT-orange)

---

## 这是什么

neiWangAgent 是一个运行在你本地的 AI 编程助手。给它一个需求描述，它会自动完成：

1. **理解需求** — LLM 分析需求，提取目标模块/数据模型/接口变更
2. **检索上下文** — 三层知识库（Summary/Hotspot/Deep）提供代码背景
3. **生成代码** — 支持6种LLM输出格式自动检测（不再捆死单一格式）
4. **安全检查** — deny_paths 拦截 + 分支regex校验 + 命令黑名单
5. **Git操作** — 自动创建 `agent/` 分支 → commit → push → 创建 Pull Request

**全部在本地运行，不依赖服务器。**

---

## 架构

```
CLI (Click) → Orchestrator (16步状态机 + 错误恢复)
                ├── LLM Client     (DeepSeek/OpenAI兼容, 带代理)
                ├── Code Parser    (6种格式自动检测)
                ├── Tracer         (JSON Lines 结构化日志)
                ├── Config Loader  (多语言项目类型自动适配)
                └── 6个 MCP Server (继承 BaseMCPServer)
                     ├── Knowledge      三层预理解 + 语言检测
                     ├── Requirement    需求读取/解析
                     ├── Git           branch/commit/push
                     ├── MR             GitHub API 创建 PR
                     ├── Clarification  澄清问答
                     └── Database       DDL索引/影响检测
```

---

## 快速开始

```bash
# 安装
pip install -e .

# 配置
export DEEPSEEK_API_KEY="sk-xxx"
export GITHUB_TOKEN="ghp_xxx"        # 创建MR需要

# 使用
neiWangAgent init                    # 初始化项目
neiWangAgent warmup                  # 构建知识库（自动检测语言）
neiWangAgent run --task task.md       # 执行任务
neiWangAgent resume 20260511-123456   # 恢复中断
```

---

## 多语言支持

配置 `project_type` 后自动适配：

| 类型 | 识别 | 保护文件 |
|------|------|---------|
| `java` | Controller/Service/Entity/MyBatis | pom.xml |
| `python` | FastAPI route/SQLAlchemy model | pyproject.toml, .env |
| `go` | gin handler/gorm model | go.mod |
| `typescript` | Next.js page/Prisma model | package.json |
| `generic` | 通用 | .env, *.pem |

---

## 版本历史

| 版本 | 日期 | 关键变更 |
|------|------|---------|
| v0.1.2 | 2026-05-11 | code_parser 6格式检测 + 错误恢复 + 状态级重试 |
| v0.1.1 | 2026-05-11 | BaseMCPServer基类 + 空handler修复 + mr_server代理 + 多语言 |
| v0.1.0 | 2026-05-10 | 初始版本：CLI + 6 MCP Server + 16步状态机 |

---

## 安全红线

| 操作 | 状态 |
|------|------|
| 读写工作目录（受 deny_paths 限制） | ✅ |
| git push agent/* | ✅ |
| 创建 Pull Request | ✅ |
| push master/main/release/hotfix | ❌ |
| git merge / force push | ❌ |
| 执行 INSERT/UPDATE/DELETE/DROP | ❌ |
| 操作 .env / .ssh / *.pem / *.key | ❌ |
| sudo / rm -rf / kubectl | ❌ |

---

## 目录结构

```
├── config.yaml              # 配置文件
├── pyproject.toml           # pip install
├── Makefile                 # Linux版构建
├── scripts/install.sh       # Linux版安装脚本
├── src/agent_mcp/
│   ├── base_mcp.py          # MCP Server 基类
│   ├── code_parser.py       # ★ LLM输出6格式检测
│   ├── tracing.py           # ★ 结构化日志系统
│   ├── cli.py               # CLI 入口
│   ├── orchestrator.py      # 核心状态机（16步+重试）
│   ├── llm_client.py        # LLM 客户端
│   ├── mcp_client.py        # MCP stdio 客户端
│   ├── config_loader.py     # 配置加载（多语言）
│   ├── knowledge_server.py  # Knowledge MCP
│   ├── requirement_server.py
│   ├── git_server.py
│   ├── mr_server.py         # ★ 支持代理+重试
│   ├── clarification_server.py
│   └── database_server.py
├── business-docs/
├── .agent/
│   ├── logs/agent.log       # ★ JSON结构化日志
│   ├── knowledge/
│   └── runs/
└── tests/
```

---

## 相关仓库

| 仓库 | 说明 |
|------|------|
| [neiWangAgent](https://github.com/guoqihan342-svg/neiWangAgent) | Windows版 |
| [neiWangAgent-linux](https://github.com/guoqihan342-svg/neiWangAgent-linux) | Linux版（推荐） |

---

## 环境变量

- `DEEPSEEK_API_KEY` — LLM API Key（必填）
- `GITHUB_TOKEN` / `GITHUB_PAT` — GitHub Token（创建MR需要）
- `https_proxy` — 代理地址（WSL环境自动读取）
