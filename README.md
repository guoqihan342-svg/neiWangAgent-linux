     1|# neiWangAgent v0.1.5
     2|
     3|> 本地无服务器 MCP Agent — 自动改代码 → commit → push → 创建 MR
     4|> 支持多语言项目：Java / Python / Go / TypeScript / Vue
     5|
     6|![Version](https://img.shields.io/badge/version-0.1.4-blue)
     7|![Python](https://img.shields.io/badge/python-3.10+-green)
     8|![License](https://img.shields.io/badge/license-MIT-orange)
     9|
    10|---
    11|
    12|## 这是什么
    13|
    14|neiWangAgent 是一个运行在你本地的 AI 编程助手。给它一个需求描述，它会自动完成：
    15|
    16|1. **理解需求** — LLM 分析需求，提取目标模块/数据模型/接口变更
    17|2. **检索上下文** — 三层知识库（Summary/Hotspot/Deep）提供代码背景
    18|3. **生成代码** — 支持6种LLM输出格式自动检测（不再捆死单一格式）
    19|4. **安全检查** — deny_paths 拦截 + 分支regex校验 + 命令黑名单
    20|5. **Git操作** — 自动创建 `agent/` 分支 → commit → push → 创建 Pull Request
    21|
    22|**全部在本地运行，不依赖服务器。**
    23|
    24|---
    25|
    26|## 架构
    27|
    28|```
    29|CLI (Click) → Orchestrator (16步状态机 + 错误恢复)
    30|                ├── LLM Client     (DeepSeek/OpenAI兼容, 带代理)
    31|                ├── Code Parser    (6种格式自动检测)
    32|                ├── Tracer         (JSON Lines 结构化日志)
    33|                ├── Config Loader  (多语言项目类型自动适配)
    34|                └── 6个 MCP Server (继承 BaseMCPServer)
    35|                     ├── Knowledge      三层预理解 + 语言检测
    36|                     ├── Requirement    需求读取/解析
    37|                     ├── Git           branch/commit/push
    38|                     ├── MR             GitHub API 创建 PR
    39|                     ├── Clarification  澄清问答
    40|                     └── Database       DDL索引/影响检测
    41|```
    42|
    43|---
    44|
    45|## 快速开始
    46|
    47|```bash
    48|# 安装
    49|pip install -e .
    50|
    51|# 配置
    52|export DEEPSEEK_API_KEY="sk-xxx"
    53|export GITHUB_TOKEN="ghp_xxx"        # 创建MR需要
    54|
    55|# 使用
    56|neiWangAgent init                    # 初始化项目
    57|neiWangAgent warmup                  # 构建知识库（自动检测语言）
    58|neiWangAgent run --task task.md       # 执行任务
    59|neiWangAgent resume 20260511-123456   # 恢复中断
    60|```
    61|
    62|---
    63|
    64|## 多语言支持
    65|
    66|配置 `project_type` 后自动适配：
    67|
    68|| 类型 | 识别 | 保护文件 |
    69||------|------|---------|
    70|| `java` | Controller/Service/Entity/MyBatis | pom.xml |
    71|| `python` | FastAPI route/SQLAlchemy model | pyproject.toml, .env |
    72|| `go` | gin handler/gorm model | go.mod |
    73|| `typescript` | Next.js page/Prisma model | package.json |
    74|| `generic` | 通用 | .env, *.pem |
    75|
    76|---
    77|
    78|## 版本历史
    79|
    80|| 版本 | 日期 | 关键变更 |
    81||------|------|---------|
    82|| v0.1.5 | 2026-05-11 | code_parser 6格式检测 + 错误恢复 + 状态级重试 |
    83|| v0.1.1 | 2026-05-11 | BaseMCPServer基类 + 空handler修复 + mr_server代理 + 多语言 |
    84|| v0.1.0 | 2026-05-10 | 初始版本：CLI + 6 MCP Server + 16步状态机 |
    85|
    86|---
    87|
    88|## 安全红线
    89|
    90|| 操作 | 状态 |
    91||------|------|
    92|| 读写工作目录（受 deny_paths 限制） | ✅ |
    93|| git commit (必须显式 files) | ✅ |
| git push agent/* | ✅ |
    94|| 创建 Pull Request | ✅ |
    95|| push master/main/release/hotfix | ❌ |
    96|| git merge / force push | ❌ |
    97|| 执行 INSERT/UPDATE/DELETE/DROP | ❌ |
    98|| 操作 .env / .ssh / *.pem / *.key | ❌ |
    99|| sudo / rm -rf / kubectl | ❌ |
   100|
   101|---
   102|
   103|## 目录结构
   104|
   105|```
   106|├── config.yaml              # 配置文件
   107|├── pyproject.toml           # pip install
   108|├── Makefile                 # Linux版构建
   109|├── scripts/install.sh       # Linux版安装脚本
   110|├── src/agent_mcp/
   111|│   ├── base_mcp.py          # MCP Server 基类
   112|│   ├── code_parser.py       # ★ LLM输出6格式检测
   113|│   ├── tracing.py           # ★ 结构化日志系统
   114|│   ├── cli.py               # CLI 入口
   115|│   ├── orchestrator.py      # 核心状态机（16步+重试）
   116|│   ├── llm_client.py        # LLM 客户端
   117|│   ├── mcp_client.py        # MCP stdio 客户端
   118|│   ├── config_loader.py     # 配置加载（多语言）
   119|│   ├── knowledge_server.py  # Knowledge MCP
   120|│   ├── requirement_server.py
   121|│   ├── git_server.py
   122|│   ├── mr_server.py         # ★ Provider模式(github/internal/mock)
   123|│   ├── clarification_server.py
   124|│   └── database_server.py
   125|├── business-docs/
   126|├── .agent/
   127|│   ├── logs/agent.log       # ★ JSON结构化日志
   128|│   ├── knowledge/
   129|│   └── runs/
   130|└── tests/
   131|```
   132|
   133|---
   134|
   135|## 相关仓库
   136|
   137|| 仓库 | 说明 |
   138||------|------|
   139|| [neiWangAgent](https://github.com/guoqihan342-svg/neiWangAgent) | Windows版 |
   140|| [neiWangAgent-linux](https://github.com/guoqihan342-svg/neiWangAgent-linux) | Linux版（推荐） |
   141|
   142|---
   143|
   144|## 环境变量
   145|
   146|- `DEEPSEEK_API_KEY` — LLM API Key（必填）
   147|- `GITHUB_TOKEN` / `GITHUB_PAT` — GitHub Token（创建MR需要）
   148|- `https_proxy` — 代理地址（WSL环境自动读取）
   149|