# neiWangAgent v0.1

> 本地无服务器 MCP Agent — 自动改代码 → commit → push agent/* → 创建 MR

## 架构

```
CLI (Click) → Orchestrator (状态机) → LLM Client (DeepSeek/OpenAI)
                                    → MCP Client (stdio)
                                         ├── Knowledge MCP
                                         ├── Requirement MCP
                                         ├── Git MCP
                                         ├── MR MCP
                                         ├── Clarification MCP
                                         └── Database MCP
```

## 快速开始

```bash
# 安装
pip install -e .

# 初始化
agent init

# 构建知识库
agent warmup

# 运行（从需求到 MR）
agent run --task requirement.md

# 恢复中断的运行
agent resume <run_id>
```

## 安全红线

- ❌ 不连接生产数据库
- ❌ 不执行 INSERT/UPDATE/DELETE
- ❌ 不推送 master/main/release/hotfix
- ❌ 不自动 merge
- ❌ 不自动 deploy

## 版本路线

| 版本 | 周期 | 能力 |
|------|------|------|
| v0.1 | 已完成 | CLI + 5个MCP Server + 状态机 |
| v0.2 | 计划 | 数据库只读连接 |
| v0.3 | 计划 | commit 风格学习 |

## 环境变量

- `DEEPSEEK_API_KEY` — LLM API Key
- `GITHUB_TOKEN` — GitHub Personal Access Token (用于创建 MR)
