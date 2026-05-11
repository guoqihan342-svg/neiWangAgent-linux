"""
neiWangAgent CLI — 基于 Click 的命令行入口。

提供 4 个子命令：
    agent init         创建 .agent/ 目录结构、默认 config.yaml、business-docs/README.md
    agent warmup       构建知识库（Knowledge MCP 三层预热）
    agent run --task   完整流程：加载需求 → 状态机 → commit → push → MR
    agent resume       从 .agent/runs/{run_id}/state.json 恢复执行

★ P0-2: 延迟 import config_loader / orchestrator，确保 init 命令在无 config.yaml 时也能执行。
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

# ★ 延迟 import — init 命令不依赖这些模块
# from agent_mcp.config_loader import load_config
# from agent_mcp.orchestrator import Orchestrator
from agent_mcp.tracing import get_tracer  # tracing 无外部依赖，安全

tracer = get_tracer()

# ---------------------------------------------------------------------------
# 常量：项目根目录发现（向上查找 config.yaml）
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    """从当前文件向上查找包含 config.yaml 的目录作为项目根。"""
    start = Path(__file__).resolve().parent
    for directory in [start, *start.parents]:
        if (directory / "config.yaml").is_file():
            return directory
    return Path.cwd()


PROJECT_ROOT: Path = _find_project_root()

# ---------------------------------------------------------------------------
# 默认配置内容（用于 init）
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_YAML = r"""# =============================================================================
# neiWangAgent — Configuration (Profile: internal)
# ★ P3-16: 企业内网默认 — LLM 配置走环境变量
# =============================================================================

project:
  name: "neiWangAgent"
  default_branch: "master"
  code_platform: "internal_custom"  # 企业内网
  project_type: "generic"

runtime:
  mode: "local_mcp"
  transport: "stdio"
  llm_timeout_seconds: 120
  mcp_timeout_seconds: 30
  # ★ 从环境变量读取（不硬编码）
  llm_base_url: "${LLM_BASE_URL}"
  llm_model: "${LLM_MODEL}"
  llm_api_key_env: "LLM_API_KEY"
  max_concurrent_mcp_calls: 5
  max_retries: 3

task:
  stop_after_create_mr: true
  enable_tests: false
  enable_self_review: false
  max_clarification_rounds: 3
  max_questions_per_round: 5
  budget_cents: 20

mcp:
  servers:
    knowledge:
      command: "python"
      args: ["-m", "agent_mcp.knowledge_server"]
    requirement:
      command: "python"
      args: ["-m", "agent_mcp.requirement_server"]
    database:
      command: "python"
      args: ["-m", "agent_mcp.database_server"]
    git:
      command: "python"
      args: ["-m", "agent_mcp.git_server"]
    mr:
      command: "python"
      args: ["-m", "agent_mcp.mr_server"]
    clarification:
      command: "python"
      args: ["-m", "agent_mcp.clarification_server"]

knowledge:
  layers:
    summary:
      auto_refresh_before_run: true
      diff_base_strategy: "merge_base"
      target_branch: "master"
      max_recent_commits: 50
    hotspot:
      build_on_warmup: true
      auto_refresh_interval: "24h"
      max_commits_to_scan: 100
      core_modules: []
      blame_key_files: []
    deep:
      build_on_first_warmup: true
      rebuild_trigger: "manual"
  business_docs_dir: "./business-docs"
  source_extensions: [".java", ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".vue"]

database:
  enabled: false
  mode: "local_docs_and_code_only"
  db_type: "postgresql"
  orm: ["mybatis", "mybatis_plus"]
  write_connection:
    enabled: false
    current_version_behavior: "never_execute_write_sql"
  ddl_dir: "./business-docs/database/ddl"
  data_dictionary_dir: "./business-docs/database/data-dictionary"

git:
  target_branch: "master"
  branch_prefix: "agent/"
  branch_naming:
    template: "agent/{yyyyMMdd}-{task_slug}"
  worktree_policy:
    require_clean_before_run: true
    allow_untracked: false
  allow_commit: true
  allow_push: true
  allow_create_mr: true
  allow_merge: false
  allow_force_push: false
  protected_branches: ["master", "main"]

mr:
  provider: "internal_mcp"  # ★ 企业内网默认
  target_branch: "master"
  title_template: "[Agent] {task_title}"
  require_untested_marker: true

clarification:
  enabled: true
  default_mode: "file"
  max_questions_per_round: 5
  max_clarification_rounds: 3

change_policy:
  max_files_changed: 20
  max_lines_changed: 800
  deny_paths: [".env", "*.pem", "*.key", "Dockerfile", "docker-compose*.yml"]

security:
  allowed_paths: [".", "./business-docs", "./.agent"]
  deny_paths: ["~/.ssh", "~/.git-credentials", ".env", "*.pem", "*.key"]
  blocked_commands: ["sudo", "rm -rf /", "kubectl"]
"""

# GitHub Demo Profile 配置（自测用）
GITHUB_DEMO_CONFIG_YAML = r"""# =============================================================================
# neiWangAgent — Configuration (Profile: github-demo)
# ★ P3-16: GitHub + DeepSeek 自测用
# =============================================================================

project:
  name: "neiWangAgent"
  default_branch: "main"
  code_platform: "github"
  project_type: "generic"

runtime:
  mode: "local_mcp"
  transport: "stdio"
  llm_timeout_seconds: 120
  mcp_timeout_seconds: 30
  llm_base_url: "https://api.deepseek.com/v1"
  llm_model: "deepseek-v4-flash"
  llm_api_key_env: "DEEPSEEK_API_KEY"
  max_concurrent_mcp_calls: 5
  max_retries: 3

task:
  stop_after_create_mr: true
  enable_tests: false
  enable_self_review: false
  max_clarification_rounds: 3
  max_questions_per_round: 5
  budget_cents: 20

mcp:
  servers:
    knowledge:
      command: "python"
      args: ["-m", "agent_mcp.knowledge_server"]
    requirement:
      command: "python"
      args: ["-m", "agent_mcp.requirement_server"]
    database:
      command: "python"
      args: ["-m", "agent_mcp.database_server"]
    git:
      command: "python"
      args: ["-m", "agent_mcp.git_server"]
    mr:
      command: "python"
      args: ["-m", "agent_mcp.mr_server"]
    clarification:
      command: "python"
      args: ["-m", "agent_mcp.clarification_server"]

knowledge:
  layers:
    summary:
      auto_refresh_before_run: true
      diff_base_strategy: "merge_base"
      target_branch: "main"
      max_recent_commits: 50
    hotspot:
      build_on_warmup: true
      auto_refresh_interval: "24h"
      max_commits_to_scan: 100
      core_modules: []
      blame_key_files: []
    deep:
      build_on_first_warmup: true
      rebuild_trigger: "manual"
  business_docs_dir: "./business-docs"
  source_extensions: [".java", ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".vue"]

database:
  enabled: false
  mode: "local_docs_and_code_only"
  db_type: "postgresql"
  write_connection:
    enabled: false
    current_version_behavior: "never_execute_write_sql"

git:
  target_branch: "main"
  branch_prefix: "agent/"
  branch_naming:
    template: "agent/{yyyyMMdd}-{task_slug}"
  worktree_policy:
    require_clean_before_run: true
    allow_untracked: false
  allow_commit: true
  allow_push: true
  allow_create_mr: true
  allow_merge: false
  allow_force_push: false
  protected_branches: ["master", "main"]

mr:
  provider: "github"
  target_branch: "main"
  title_template: "[Agent] {task_title}"
  require_untested_marker: true

clarification:
  enabled: true
  default_mode: "file"
  max_questions_per_round: 5
  max_clarification_rounds: 3

change_policy:
  max_files_changed: 20
  max_lines_changed: 800
  deny_paths: [".env", "*.pem", "*.key"]

security:
  allowed_paths: [".", "./business-docs", "./.agent"]
  deny_paths: ["~/.ssh", "~/.git-credentials", ".env", "*.pem", "*.key"]
  blocked_commands: ["sudo", "rm -rf /", "kubectl"]
"""

DEFAULT_BUSINESS_README = """# business-docs

存放项目业务文档的目录。

## 目录说明

- 本目录用于存放与项目相关的业务需求文档、设计文档、接口说明等。
- agent 在执行 warmup 时将会扫描此目录，将文档内容纳入知识库。
- 支持的文件格式：`.md`、`.txt`、`.yaml`、`.json`。

## 使用方式

1. 将业务文档放入此目录。
2. 运行 `agent warmup` 重新构建知识库。
3. agent 在执行 `agent run` 时将自动引用这些文档作为上下文。
"""


# =============================================================================
# CLI 入口
# =============================================================================

@click.group()
@click.version_option(version="0.1.8", prog_name="neiWangAgent")  # ★ P0-3: 统一版本号
def main() -> None:
    """neiWangAgent — 本地 MCP Agent，自动改代码 → commit → push → 创建 MR。"""
    pass


# =============================================================================
# agent init
# =============================================================================

@main.command("init")
@click.option(
    "--profile", "-p",
    type=click.Choice(["internal", "github-demo"]),
    default="internal",
    help="配置模板: internal(企业内网默认) / github-demo(GitHub+DeepSeek自测)"
)
def cmd_init(profile: str) -> None:
    """初始化项目结构。

    创建以下内容：
        .agent/              工作目录
        .agent/runs/         运行记录目录
        .agent/logs/         日志目录
        config.yaml          默认配置文件（如不存在）
        business-docs/       业务文档目录
        business-docs/README.md

    ★ P3-16: --profile 选择配置模板
      - internal:    企业内网默认（LLM_BASE_URL/LLM_MODEL/LLM_API_KEY 环境变量）
      - github-demo: GitHub + DeepSeek 自测用
    """
    tracer.info("cli.init.start", detail={"profile": profile})
    root = PROJECT_ROOT
    created: list[str] = []
    skipped: list[str] = []

    # --- .agent/ 目录 ---
    agent_dir = root / ".agent"
    if not agent_dir.exists():
        agent_dir.mkdir(parents=True, exist_ok=True)
        created.append(".agent/")
    else:
        skipped.append(".agent/（已存在）")

    # --- .agent/runs/ 目录 ---
    runs_dir = agent_dir / "runs"
    if not runs_dir.exists():
        runs_dir.mkdir(parents=True, exist_ok=True)
        created.append(".agent/runs/")
    else:
        skipped.append(".agent/runs/（已存在）")

    # --- .agent/logs/ 目录（★ 新增：日志存储） ---
    logs_dir = agent_dir / "logs"
    if not logs_dir.exists():
        logs_dir.mkdir(parents=True, exist_ok=True)
        created.append(".agent/logs/")
    else:
        skipped.append(".agent/logs/（已存在）")

    # --- config.yaml ---
    config_path = root / "config.yaml"
    if not config_path.exists():
        # ★ P3-16: 根据 profile 选择配置模板
        config_content = (
            GITHUB_DEMO_CONFIG_YAML if profile == "github-demo"
            else DEFAULT_CONFIG_YAML
        )
        config_path.write_text(config_content, encoding="utf-8")
        created.append("config.yaml")
    else:
        skipped.append("config.yaml（已存在，未覆盖）")

    # --- business-docs/ 目录 ---
    docs_dir = root / "business-docs"
    if not docs_dir.exists():
        docs_dir.mkdir(parents=True, exist_ok=True)
        created.append("business-docs/")
    else:
        skipped.append("business-docs/（已存在）")

    # --- business-docs/README.md ---
    readme_path = docs_dir / "README.md"
    if not readme_path.exists():
        readme_path.write_text(DEFAULT_BUSINESS_README, encoding="utf-8")
        created.append("business-docs/README.md")
    else:
        skipped.append("business-docs/README.md（已存在，未覆盖）")

    # --- 输出结果 ---
    click.echo()
    if created:
        click.secho("✅ 已创建：", fg="green", bold=True)
        for item in created:
            click.echo(f"   {item}")
    if skipped:
        click.secho("⏭️  已跳过：", fg="yellow")
        for item in skipped:
            click.echo(f"   {item}")
    click.echo()

    if not created:
        click.echo("项目结构已就绪，无需初始化。")


# =============================================================================
# agent warmup
# =============================================================================

@main.command("warmup")
def cmd_warmup() -> None:
    """构建知识库（Knowledge MCP 三层预热）。

    依次执行：
        1. Summary 层 — 项目结构概览
        2. Hotspot 层 — 热点模块分析
        3. Deep 层    — 深度代码索引

    预热结果持久化到 .agent/ 目录，供后续 agent run 使用。
    """
    tracer.info("cli.warmup.start")  # ★ 日志
    from agent_mcp.config_loader import load_config  # ★ 延迟 import
    from agent_mcp.orchestrator import Orchestrator
    click.echo()
    click.secho("🔥 开始知识库预热...", fg="cyan", bold=True)

    try:
        config = load_config()
    except FileNotFoundError:
        click.secho("❌ 未找到 config.yaml，请先运行 agent init。", fg="red")
        sys.exit(1)
    except Exception as exc:
        click.secho(f"❌ 加载配置文件失败：{exc}", fg="red")
        sys.exit(1)

    orch = Orchestrator(config)

    try:
        orch.warmup()
        click.secho("✅ 知识库预热完成。", fg="green", bold=True)
    except Exception as exc:
        click.secho(f"❌ 预热过程出错：{exc}", fg="red")
        sys.exit(1)

    click.echo()


# =============================================================================
# agent run
# =============================================================================

@main.command("run")
@click.option(
    "--task", "-t",
    "task_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="需求描述文件路径（.md / .txt / .yaml）。",
)
def cmd_run(task_file: str) -> None:
    """执行完整的 agent 流程。

    读取需求文件 → 状态机驱动执行 → commit → push → 创建 MR。

    流程阶段：
        需求理解 → 澄清问答 → 方案规划 → 代码实现 → 变更检查 → 提交推送 → 创建 MR
    """
    tracer.info("cli.run.start", detail={"task_file": task_file})  # ★ 日志
    from agent_mcp.config_loader import load_config  # ★ 延迟 import
    from agent_mcp.orchestrator import Orchestrator
    task_path = Path(task_file).resolve()
    click.echo()
    click.secho(f"📄 读取需求文件：{task_path}", fg="cyan")

    try:
        requirement_text = task_path.read_text(encoding="utf-8")
    except Exception as exc:
        click.secho(f"❌ 无法读取需求文件：{exc}", fg="red")
        sys.exit(1)

    if not requirement_text.strip():
        click.secho("❌ 需求文件内容为空。", fg="red")
        sys.exit(1)

    try:
        config = load_config()
    except FileNotFoundError:
        click.secho("❌ 未找到 config.yaml，请先运行 agent init。", fg="red")
        sys.exit(1)
    except Exception as exc:
        click.secho(f"❌ 加载配置文件失败：{exc}", fg="red")
        sys.exit(1)

    click.secho("🚀 启动 agent 执行引擎...", fg="cyan", bold=True)

    orch = Orchestrator(config)

    try:
        orch.run(requirement_text)
        click.secho("✅ agent 执行完成。", fg="green", bold=True)
    except Exception as exc:
        click.secho(f"❌ 执行过程出错：{exc}", fg="red")
        sys.exit(1)

    click.echo()


# =============================================================================
# agent resume
# =============================================================================

@main.command("resume")
@click.argument("run_id", required=True)
def cmd_resume(run_id: str) -> None:
    """从之前的运行状态恢复执行。

    RUN_ID 为 .agent/runs/ 下的运行目录名。

    示例：
        agent resume 20260510-001
    """
    tracer.info("cli.resume.start", detail={"run_id": run_id})  # ★ 日志
    from agent_mcp.config_loader import load_config  # ★ 延迟 import
    from agent_mcp.orchestrator import Orchestrator
    state_file = PROJECT_ROOT / ".agent" / "runs" / run_id / "state.json"

    click.echo()
    click.secho(f"🔄 尝试恢复运行：{run_id}", fg="cyan")

    if not state_file.exists():
        click.secho(f"❌ 状态文件不存在：{state_file}", fg="red")
        click.echo("   请检查 run_id 是否正确，或运行 agent init 初始化项目。")
        sys.exit(1)

    try:
        config = load_config()
    except FileNotFoundError:
        click.secho("❌ 未找到 config.yaml，请先运行 agent init。", fg="red")
        sys.exit(1)
    except Exception as exc:
        click.secho(f"❌ 加载配置文件失败：{exc}", fg="red")
        sys.exit(1)

    orch = Orchestrator(config)

    try:
        orch.resume(run_id)
        click.secho(f"✅ 运行 {run_id} 恢复执行完成。", fg="green", bold=True)
    except FileNotFoundError:
        click.secho(f"❌ 未找到运行记录：{run_id}", fg="red")
        sys.exit(1)
    except Exception as exc:
        click.secho(f"❌ 恢复执行失败：{exc}", fg="red")
        sys.exit(1)

    click.echo()


# =============================================================================
# 直接执行入口
# =============================================================================

if __name__ == "__main__":
    main()
