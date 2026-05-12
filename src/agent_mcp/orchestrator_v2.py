"""
orchestrator_v2.py — Agent 编排器 v0.3.0（图引擎 + 多 Agent）

替代旧的 20 个硬编码状态处理函数。新架构：

  旧: if/elif 状态机 + 20 个 _handle_xxx 方法
  新: StateGraph(图引擎) + AgentPipeline(5 Agent)

映射关系：
  旧状态              → 新图节点                 → 对应 Agent
  ──────────────────────────────────────────────────────────
  INIT~RETRIEVE_CONTEXT → research_node         → ResearchAgent
  UNDERSTAND~PLAN       → plan_node             → PlanAgent  
  IMPLEMENT~SELF_REVIEW → code_review_loop      → CodeAgent + ReviewAgent
  PREPARE_COMMIT~CREATE_MR → git_node           → GitAgent

使用方式：
  from agent_mcp.orchestrator_v2 import AgentOrchestrator
  orch = AgentOrchestrator(config)
  orch.run("修复登录页面样式错乱")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, Optional

from agent_mcp.config_loader import AppConfig
from agent_mcp.llm_client import LLMClient
from agent_mcp.tracing import get_tracer, Tracer
from agent_mcp.graph import StateGraph
from agent_mcp.agents import AgentPipeline
from agent_mcp.git_server import GitMCPServer
from agent_mcp.mr_server import MRMCPServer
from agent_mcp.knowledge_server import KnowledgeMCPServer

logger = logging.getLogger(__name__)


# =============================================================================
# RunState（保持与旧版兼容）
# =============================================================================

class RunState:
    """一次运行的持久化状态"""

    def __init__(self, run_id: str, requirement: str = ""):
        self.run_id = run_id
        self.requirement = requirement
        self.current_stage = "init"
        self.status = "running"
        self.files_changed: list[str] = []
        self.lines_changed = 0
        self.branch_name = ""
        self.commit_hash = ""
        self.mr_url = ""
        self.errors: list[str] = []
        self.transcript: list[dict] = []
        self.created_at = datetime.now().isoformat()
        # 新字段：Agent 流水线结果
        self.research_result: Optional[dict] = None
        self.plan_result: Optional[dict] = None
        self.code_result: Optional[dict] = None
        self.review_result: Optional[dict] = None
        self.git_result: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "requirement": self.requirement,
            "current_stage": self.current_stage,
            "status": self.status,
            "files_changed": self.files_changed,
            "lines_changed": self.lines_changed,
            "branch_name": self.branch_name,
            "commit_hash": self.commit_hash,
            "mr_url": self.mr_url,
            "errors": self.errors,
            "transcript": self.transcript,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunState":
        rs = cls(data["run_id"], data.get("requirement", ""))
        rs.current_stage = data.get("current_stage", "init")
        rs.status = data.get("status", "running")
        rs.files_changed = data.get("files_changed", [])
        rs.lines_changed = data.get("lines_changed", 0)
        rs.branch_name = data.get("branch_name", "")
        rs.commit_hash = data.get("commit_hash", "")
        rs.mr_url = data.get("mr_url", "")
        rs.errors = data.get("errors", [])
        rs.transcript = data.get("transcript", [])
        rs.created_at = data.get("created_at", "")
        return rs


# =============================================================================
# AgentOrchestrator — 新编排器
# =============================================================================

class AgentOrchestrator:
    """
    v0.3.0 图引擎 + 多 Agent 编排器。

    用 StateGraph 替代旧的 20 个硬编码状态，
    用 AgentPipeline（Research→Plan→Code→Review→Git）替代旧的 _handle_xxx 方法。
    """

    def __init__(self, config: AppConfig, dry_run: bool = False):
        self.config = config
        self.llm = LLMClient(config)
        self.tracer: Tracer = get_tracer()
        self.dry_run = dry_run

        # MCP Server 实例（供 Git 操作使用）
        self._git_mcp = GitMCPServer()
        self._mr_mcp = MRMCPServer()
        self._knowledge_mcp = KnowledgeMCPServer()

        self.run_state: Optional[RunState] = None
        self._graph = self._build_graph()
        self._pipeline: Optional[AgentPipeline] = None

    # ==================================================================
    # 图构建
    # ==================================================================

    def _build_graph(self) -> StateGraph:
        """构建编排状态图。"""
        g = StateGraph("AgentOrchestrator")

        g.add_node("warmup", self._node_warmup, "预热知识库")
        g.add_node("research", self._node_research, "研究代码库")
        g.add_node("plan", self._node_plan, "制定修改计划")
        g.add_node("code_review_loop", self._node_code_review_loop, "编码→审查循环")
        g.add_node("git_commit", self._node_git_commit, "Git 提交推送")
        g.add_node("done", self._node_done, "完成")

        g.add_edge("warmup", "research")
        g.add_edge("research", "plan")

        # 条件边：Plan 可能失败
        g.add_conditional_edges(
            "plan",
            self._router_plan,
            {"ok": "code_review_loop", "fail": "done"},
        )

        g.add_edge("code_review_loop", "git_commit")

        # 条件边：Git 可能失败
        g.add_conditional_edges(
            "git_commit",
            self._router_git,
            {"ok": "done", "fail": "done"},
        )

        g.set_entry_point("warmup")
        g.set_finish_point("done")

        return g.compile()

    # ==================================================================
    # 图节点
    # ==================================================================

    def _node_warmup(self, state: dict) -> dict:
        """预热知识库节点。"""
        self.run_state.current_stage = "warmup"
        logger.info("预热知识库...")
        print("🔥 预热知识库...")

        try:
            project_root = str(self.config.project_root or Path.cwd())
            result = self._knowledge_mcp.index_codebase(project_root)
            state["knowledge_ready"] = True
            state["knowledge_result"] = result
            print("✅ 知识库预热完成")
        except Exception as e:
            logger.warning(f"知识库预热失败: {e}")
            state["knowledge_ready"] = False
            state["knowledge_error"] = str(e)
            print(f"⚠️ 知识库预热失败: {e}（继续执行）")

        return state

    def _node_research(self, state: dict) -> dict:
        """研究 Agent 节点。"""
        self.run_state.current_stage = "research"
        logger.info("研究 Agent 分析代码库...")
        print("\n🔍 [阶段 1/4] 研究 Agent 分析代码库...")

        if self._pipeline is None:
            self._pipeline = AgentPipeline(
                config=self.config,
                llm_client=self.llm,
                verbose=True,
            )
            self._pipeline.assemble()

        requirement = state.get("requirement", self.run_state.requirement)

        research_result = self._pipeline.research_agent.run(
            task=f"分析以下需求涉及的代码库: {requirement}",
            context={
                "requirement": requirement,
                "project_root": str(self.config.project_root or Path.cwd()),
            },
        )

        self.run_state.research_result = research_result
        state["research"] = research_result

        if research_result.get("success"):
            output = research_result.get("output", {})
            files = output.get("related_files", [])
            print(f"✅ 研究完成: 找到 {len(files)} 个相关文件")
            state["research_ok"] = True
        else:
            print("⚠️ 研究未完成，继续执行")
            state["research_ok"] = False

        return state

    def _node_plan(self, state: dict) -> dict:
        """规划 Agent 节点。"""
        self.run_state.current_stage = "plan"
        logger.info("规划 Agent 制定修改计划...")
        print("\n📋 [阶段 2/4] 规划 Agent 制定修改计划...")

        research = state.get("research", {}).get("output", {})

        plan_result = self._pipeline.plan_agent.run(
            task="根据研究报告制定修改计划",
            context={"research": research},
        )

        self.run_state.plan_result = plan_result
        state["plan"] = plan_result

        if plan_result.get("success"):
            tasks = plan_result.get("output", {}).get("tasks", [])
            print(f"✅ 规划完成: {len(tasks)} 个子任务")
            state["plan_ok"] = True
        else:
            print("⚠️ 规划失败")
            state["plan_ok"] = False

        return state

    def _router_plan(self, state: dict) -> str:
        return "ok" if state.get("plan_ok") else "fail"

    def _node_code_review_loop(self, state: dict) -> dict:
        """编码→审查循环节点。"""
        self.run_state.current_stage = "code_review"
        logger.info("编码→审查循环...")
        print("\n💻 [阶段 3/4] 编码→审查循环...")

        requirement = state.get("requirement", self.run_state.requirement)
        plan = state.get("plan", {}).get("output", {})
        max_retries = 3

        code_result = None
        review_result = None

        for attempt in range(1, max_retries + 1):
            print(f"  编码尝试 {attempt}/{max_retries}")

            code_result = self._pipeline.code_agent.run(
                task="根据修改计划生成代码变更",
                context={
                    "plan": plan,
                    "previous_review": review_result.get("output") if review_result else None,
                    "retry_hint": f"第 {attempt} 次尝试，请修正审查问题" if attempt > 1 else "",
                },
            )

            review_result = self._pipeline.review_agent.run(
                task="审查代码修改",
                context={
                    "requirement": requirement,
                    "plan": plan,
                    "code_changes": code_result.get("output", {}),
                },
            )

            review_output = review_result.get("output", {})
            if review_output.get("passed"):
                print(f"  ✅ 审查通过 (第 {attempt} 次尝试)")
                state["review_passed"] = True
                break
            else:
                issues = review_output.get("issues", [])
                critical = sum(1 for i in issues if i.get("severity") == "critical")
                print(f"  ⚠️ 审查未通过: {len(issues)} 个问题 ({critical} 严重)")

        self.run_state.code_result = code_result
        self.run_state.review_result = review_result
        state["code_changes"] = code_result
        state["review"] = review_result

        if not state.get("review_passed"):
            print("⚠️ 审查未通过，但仍继续 Git 阶段")

        return state

    def _node_git_commit(self, state: dict) -> dict:
        """Git 提交节点。"""
        self.run_state.current_stage = "git"
        logger.info("Git Agent 提交代码...")
        print("\n📦 [阶段 4/4] Git Agent 提交代码...")

        if self.dry_run:
            print("🔒 DRY-RUN 模式，跳过实际 Git 操作")
            state["git_ok"] = True
            return state

        requirement = state.get("requirement", self.run_state.requirement)
        code_changes = state.get("code_changes", {}).get("output", {})
        review = state.get("review", {}).get("output", {})

        git_result = self._pipeline.git_agent.run(
            task="生成 commit message 和 MR 信息",
            context={
                "requirement": requirement,
                "code_changes": code_changes,
                "review": review,
            },
        )

        self.run_state.git_result = git_result
        state["git_result"] = git_result

        git_output = git_result.get("output", {})
        if git_output:
            # 通过 MCP 执行 Git 操作
            try:
                branch = git_output.get("branch", "")
                if branch:
                    # 创建分支
                    self._git_mcp.create_branch(self.config.project_root, branch)
                    self.run_state.branch_name = branch
                    print(f"  分支: {branch}")

                # 提交
                commit_msg = git_output.get("commit_message", "")
                files = git_output.get("files_to_commit", [])
                if commit_msg and files:
                    self._git_mcp.commit(self.config.project_root, commit_msg, files)
                    print(f"  Commit: {commit_msg[:80]}")

                    # 推送
                    self._git_mcp.push(self.config.project_root, branch)
                    print(f"  Push: {branch}")

                    # 创建 MR
                    mr_title = git_output.get("mr_title", commit_msg)
                    mr_desc = git_output.get("mr_description", "")
                    mr_result = self._mr_mcp.create_mr(branch, mr_title, mr_desc)
                    self.run_state.mr_url = mr_result.get("url", "")
                    print(f"  MR: {self.run_state.mr_url or '已创建'}")

                state["git_ok"] = True
            except Exception as e:
                logger.error(f"Git 操作失败: {e}")
                print(f"❌ Git 操作失败: {e}")
                state["git_ok"] = False
                state["git_error"] = str(e)
        else:
            print("⚠️ Git Agent 未生成有效输出")
            state["git_ok"] = False

        return state

    def _router_git(self, state: dict) -> str:
        return "ok" if state.get("git_ok") else "fail"

    def _node_done(self, state: dict) -> dict:
        """完成节点。"""
        self.run_state.current_stage = "done"
        self.run_state.status = "completed"
        logger.info("编排完成")
        print("\n✅ Agent 编排完成")

        # 生成 report
        self._generate_report(state)
        return state

    def _generate_report(self, state: dict):
        """生成运行报告。"""
        report_lines = [
            f"# Agent 运行报告",
            f"",
            f"- Run ID: {self.run_state.run_id}",
            f"- 需求: {self.run_state.requirement[:100]}",
            f"- 时间: {datetime.now().isoformat()}",
            f"- 状态: {self.run_state.status}",
            f"",
            f"## 流水线摘要",
        ]

        stages = [
            ("研究", state.get("research", {}).get("success")),
            ("规划", state.get("plan", {}).get("success")),
            ("编码+审查", state.get("review_passed")),
            ("Git", state.get("git_ok")),
        ]
        for name, ok in stages:
            icon = "✅" if ok else "❌"
            report_lines.append(f"- {icon} {name}")

        report_lines.append(f"\n## 输出")
        report_lines.append(f"- 分支: {self.run_state.branch_name or '(dry-run)'}")
        report_lines.append(f"- MR: {self.run_state.mr_url or '(dry-run)'}")

        report_path = Path(f".agent/runs/{self.run_state.run_id}/report.md")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(report_lines))
        logger.info(f"报告已保存: {report_path}")

    # ==================================================================
    # 公共 API（兼容旧接口）
    # ==================================================================

    def run(self, requirement_text: str) -> dict:
        """创建新运行并驱动流水线。"""
        run_id = f"{date.today().strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}"
        self.run_state = RunState(run_id, requirement_text)
        self.tracer.set_run_id(run_id)
        self.tracer.info("agent.run.start", detail=requirement_text[:200])

        print(f"\n🚀 Agent 运行 [{run_id}]")
        print(f"📋 {requirement_text[:100]}...\n")

        # 构建初始状态
        initial_state = {
            "run_id": run_id,
            "requirement": requirement_text,
        }

        # 驱动状态图
        result = self._graph.invoke(
            initial_state,
            checkpoint_dir=Path(f".agent/runs/{run_id}/checkpoints"),
        )

        # 保存 RunState
        self._save_run_state()

        return self.run_state.to_dict()

    def resume(self, run_id: str) -> dict:
        """从断点恢复运行。"""
        state_path = Path(f".agent/runs/{run_id}/state.json")
        if not state_path.exists():
            raise FileNotFoundError(f"状态不存在: {state_path}")

        data = json.loads(state_path.read_text())
        self.run_state = RunState.from_dict(data)
        self.tracer.set_run_id(run_id)

        if self.run_state.status == "completed":
            self.tracer.info("agent.resume.already_done", detail=run_id)
            print(f"✅ [{run_id}] 已完成")
            return self.run_state.to_dict()

        print(f"\n🔄 恢复运行 [{run_id}]")
        print(f"📋 当前阶段: {self.run_state.current_stage}\n")

        # 从当前阶段恢复
        stage_to_node = {
            "init": "warmup",
            "warmup": "research",
            "research": "plan",
            "plan": "code_review_loop",
            "code_review": "git_commit",
            "git": "done",
        }
        current_node = stage_to_node.get(self.run_state.current_stage, "warmup")

        # 构建状态并恢复
        restore_state = {
            "run_id": run_id,
            "requirement": self.run_state.requirement,
            "__current_node__": current_node,
        }

        result = self._graph.resume(
            restore_state,
            checkpoint_dir=Path(f".agent/runs/{run_id}/checkpoints"),
        )

        self._save_run_state()
        return self.run_state.to_dict()

    def warmup(self):
        """知识库预热（供 CLI warmup 命令调用）。"""
        if self.run_state is None:
            self.run_state = RunState("warmup", "知识库预热")

        print("🔥 开始知识库预热...")
        try:
            project_root = str(self.config.project_root or Path.cwd())
            result = self._knowledge_mcp.index_codebase(project_root)
            print(f"✅ 知识库预热完成: {result.get('message', '')}")
        except Exception as e:
            print(f"⚠️ 预热失败: {e}")

    # ==================================================================
    # 内部工具
    # ==================================================================

    def _save_run_state(self):
        """持久化 RunState。"""
        if self.run_state is None:
            return
        state_path = Path(f".agent/runs/{self.run_state.run_id}/state.json")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(self.run_state.to_dict(), ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _mcp_call(server, tool_name: str, **args) -> dict:
        """调用 MCP Server 工具方法。"""
        resp = server._call_tool(tool_name, args)
        content_list = resp.get("content", [])
        text = content_list[0]["text"] if content_list else "{}"
        if resp.get("isError"):
            raise RuntimeError(f"MCP 工具 [{tool_name}] 失败: {text}")
        return json.loads(text)
