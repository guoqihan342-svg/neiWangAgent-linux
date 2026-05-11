"""
Orchestrator — 核心状态机编排器 v0.1.4（带日志追踪 + MCP 集成）

实现 Agent 从需求到 MR 的完整流程：
INIT → WARMUP_CHECK → SUMMARY_REFRESH → WORKTREE_GUARD → LOAD_REQUIREMENT →
RETRIEVE_CONTEXT → UNDERSTAND_REQUIREMENT → CLARIFICATION_GATE →
PLAN_IMPLEMENTATION → CREATE_BRANCH → IMPLEMENT → CHANGE_SCOPE_GUARD →
DATABASE_IMPACT_DETECT → PREPARE_COMMIT → COMMIT → PUSH → CREATE_MR → DONE

★ v0.1.4 P1 MCP化:
  - Orchestrator 通过 Git/MR/Knowledge MCP Server 执行操作
  - MR Server Provider 模式 (github/internal_mcp/mock)
  - Knowledge Server 持久化 + 搜索
  - Code Parser 新增 @@PATCH 模式
  - git_server 强制显式 files（禁止 git add -A）
★ v0.1.3 P0修复:
  - 状态分级: CRITICAL(失败→FAILED) / OPTIONAL(失败→降级) / HUMAN(失败→PAUSED)
  - resume: 不创建新run_id,直接驱动状态机
  - 错误恢复: 关键状态不允许跳过
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, date
from pathlib import Path
from typing import Any, Callable

from agent_mcp.config_loader import AppConfig
from agent_mcp.llm_client import LLMClient
from agent_mcp.tracing import get_tracer, Tracer
from agent_mcp.code_parser import parse_code_changes
# ★ P1-6: MCP Server 集成 — Orchestrator 通过 MCP 接口操作 Git/MR/Knowledge
from agent_mcp.git_server import GitMCPServer
from agent_mcp.mr_server import MRMCPServer, get_mr_provider
from agent_mcp.knowledge_server import KnowledgeMCPServer

logger = logging.getLogger(__name__)


# =============================================================================
# 状态常量
# =============================================================================

class State:
    INIT = "000"
    WARMUP_CHECK = "010"
    SUMMARY_REFRESH = "015"
    WORKTREE_GUARD = "018"
    LOAD_REQUIREMENT = "020"
    RETRIEVE_CONTEXT = "030"
    UNDERSTAND_REQUIREMENT = "040"
    CLARIFICATION_GATE = "050"
    ASK_HUMAN = "060"
    WAITING_CLARIFICATION = "070"
    RESUME_WITH_ANSWER = "080"
    PLAN_IMPLEMENTATION = "100"
    CREATE_BRANCH = "110"
    IMPLEMENT = "120"
    CHANGE_SCOPE_GUARD = "125"
    DATABASE_IMPACT_DETECT = "130"
    GENERATE_DB_IMPACT_REPORT = "135"
    GENERATE_MIGRATION_DRAFT = "140"
    PREPARE_COMMIT = "150"
    COMMIT = "160"
    PUSH = "170"
    CREATE_MR = "180"
    DONE = "200"


# ★ P0-5: 状态分级
# CRITICAL: 失败必须停止，不能跳过
CRITICAL_STATES = {
    State.CREATE_BRANCH,
    State.IMPLEMENT,
    State.CHANGE_SCOPE_GUARD,
    State.PREPARE_COMMIT,
    State.COMMIT,
    State.PUSH,
    State.CREATE_MR,
}

# OPTIONAL: 失败可降级跳过
OPTIONAL_STATES = {
    State.SUMMARY_REFRESH,
    State.DATABASE_IMPACT_DETECT,
    State.GENERATE_DB_IMPACT_REPORT,
    State.GENERATE_MIGRATION_DRAFT,
}

# HUMAN: 失败后暂停等待人工
HUMAN_STATES = {
    State.CLARIFICATION_GATE,
    State.ASK_HUMAN,
}


TRANSITIONS: dict[str, str | None] = {
    State.INIT: State.WARMUP_CHECK,
    State.WARMUP_CHECK: State.SUMMARY_REFRESH,
    State.SUMMARY_REFRESH: State.WORKTREE_GUARD,
    State.WORKTREE_GUARD: State.LOAD_REQUIREMENT,
    State.LOAD_REQUIREMENT: State.RETRIEVE_CONTEXT,
    State.RETRIEVE_CONTEXT: State.UNDERSTAND_REQUIREMENT,
    State.UNDERSTAND_REQUIREMENT: State.CLARIFICATION_GATE,
    State.CLARIFICATION_GATE: State.PLAN_IMPLEMENTATION,
    State.ASK_HUMAN: State.WAITING_CLARIFICATION,
    State.WAITING_CLARIFICATION: None,
    State.RESUME_WITH_ANSWER: State.RETRIEVE_CONTEXT,
    State.PLAN_IMPLEMENTATION: State.CREATE_BRANCH,
    State.CREATE_BRANCH: State.IMPLEMENT,
    State.IMPLEMENT: State.CHANGE_SCOPE_GUARD,
    State.CHANGE_SCOPE_GUARD: State.DATABASE_IMPACT_DETECT,
    State.DATABASE_IMPACT_DETECT: State.PREPARE_COMMIT,
    State.GENERATE_DB_IMPACT_REPORT: State.PREPARE_COMMIT,
    State.GENERATE_MIGRATION_DRAFT: State.PREPARE_COMMIT,
    State.PREPARE_COMMIT: State.COMMIT,
    State.COMMIT: State.PUSH,
    State.PUSH: State.CREATE_MR,
    State.CREATE_MR: State.DONE,
    State.DONE: None,
}

STATE_NAMES: dict[str, str] = {
    State.INIT: "初始化",
    State.WARMUP_CHECK: "检查知识库",
    State.SUMMARY_REFRESH: "刷新摘要",
    State.WORKTREE_GUARD: "工作区保护检查",
    State.LOAD_REQUIREMENT: "加载需求",
    State.RETRIEVE_CONTEXT: "检索上下文",
    State.UNDERSTAND_REQUIREMENT: "理解需求",
    State.CLARIFICATION_GATE: "澄清判断",
    State.ASK_HUMAN: "生成问题",
    State.WAITING_CLARIFICATION: "等待回复",
    State.RESUME_WITH_ANSWER: "恢复执行",
    State.PLAN_IMPLEMENTATION: "生成实施计划",
    State.CREATE_BRANCH: "创建分支",
    State.IMPLEMENT: "修改代码",
    State.CHANGE_SCOPE_GUARD: "变更范围检查",
    State.DATABASE_IMPACT_DETECT: "数据库影响检测",
    State.GENERATE_DB_IMPACT_REPORT: "生成影响报告",
    State.GENERATE_MIGRATION_DRAFT: "生成迁移草稿",
    State.PREPARE_COMMIT: "准备提交",
    State.COMMIT: "执行提交",
    State.PUSH: "推送代码",
    State.CREATE_MR: "创建 MR",
    State.DONE: "完成",
}


class RunState:
    """一次运行的持久化状态"""

    def __init__(self, run_id: str, requirement: str = ""):
        self.run_id = run_id
        self.requirement = requirement
        self.current_state = State.INIT
        self.status = "running"
        self.files_changed: list[str] = []
        self.lines_changed = 0
        self.branch_name = ""
        self.commit_hash = ""
        self.mr_url = ""
        self.errors: list[str] = []
        self.transcript: list[dict] = []
        self.created_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "requirement": self.requirement,
            "current_state": self.current_state, "status": self.status,
            "files_changed": self.files_changed, "lines_changed": self.lines_changed,
            "branch_name": self.branch_name, "commit_hash": self.commit_hash,
            "mr_url": self.mr_url, "errors": self.errors,
            "transcript": self.transcript, "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunState":
        rs = cls(data["run_id"], data.get("requirement", ""))
        rs.current_state = data.get("current_state", State.INIT)
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


class Orchestrator:
    """核心编排器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = LLMClient(config)
        self.run_state: RunState | None = None
        self.tracer: Tracer = get_tracer()
        # ★ P1-6: 实例化 MCP Server（直接模式，不走 stdio 子进程）
        self._git_mcp = GitMCPServer()
        self._mr_mcp = MRMCPServer()
        self._knowledge_mcp = KnowledgeMCPServer()
        self._handlers: dict[str, Callable] = {
            State.INIT: self._handle_init,
            State.WARMUP_CHECK: self._handle_warmup_check,
            State.SUMMARY_REFRESH: self._handle_summary_refresh,
            State.WORKTREE_GUARD: self._handle_worktree_guard,
            State.LOAD_REQUIREMENT: self._handle_load_requirement,
            State.RETRIEVE_CONTEXT: self._handle_retrieve_context,
            State.UNDERSTAND_REQUIREMENT: self._handle_understand_requirement,
            State.CLARIFICATION_GATE: self._handle_clarification_gate,
            State.ASK_HUMAN: self._handle_ask_human,
            State.PLAN_IMPLEMENTATION: self._handle_plan_implementation,
            State.CREATE_BRANCH: self._handle_create_branch,
            State.IMPLEMENT: self._handle_implement,
            State.CHANGE_SCOPE_GUARD: self._handle_change_scope_guard,
            State.DATABASE_IMPACT_DETECT: self._handle_database_impact_detect,
            State.PREPARE_COMMIT: self._handle_prepare_commit,
            State.COMMIT: self._handle_commit,
            State.PUSH: self._handle_push,
            State.CREATE_MR: self._handle_create_mr,
            State.DONE: self._handle_done,
        }

    # ==================================================================
    # ★ P1-6: MCP 工具调用辅助方法
    # ==================================================================

    @staticmethod
    def _mcp_call(server, tool_name: str, **args) -> dict:
        """
        调用 MCP Server 的工具方法并解析响应。

        参数：
            server:     MCP Server 实例（如 GitMCPServer）
            tool_name:  工具名称（如 "git_create_branch"）
            **args:     工具参数

        返回：
            dict: 解析后的 JSON 结果

        异常：
            RuntimeError: 当 MCP 工具返回 isError=True 时
        """
        resp = server._call_tool(tool_name, args)
        content_list = resp.get("content", [])
        text = content_list[0]["text"] if content_list else "{}"
        if resp.get("isError"):
            raise RuntimeError(f"MCP 工具 [{tool_name}] 失败: {text}")
        return json.loads(text)

    # ==================================================================
    # ★ P0-4: 分离 run / resume / _drive_state_machine
    # ==================================================================

    def run(self, requirement_text: str) -> dict:
        """创建新运行并驱动状态机。"""
        run_id = f"{date.today().strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}"
        self.run_state = RunState(run_id, requirement_text)
        self._ensure_run_dir()
        self.tracer.set_run_id(run_id)
        self.tracer.info("agent.run.start", step="000", detail=requirement_text[:200])
        print(f"\n🚀 Agent 运行 [{run_id}]")
        print(f"📋 {requirement_text[:100]}...\n")
        return self._drive_state_machine()

    def resume(self, run_id: str) -> dict:
        """★ P0-4: 从保存的状态恢复，不创建新run_id。"""
        state_path = Path(f".agent/runs/{run_id}/state.json")
        if not state_path.exists():
            raise FileNotFoundError(f"状态不存在: {state_path}")

        data = json.loads(state_path.read_text())
        self.run_state = RunState.from_dict(data)
        self.tracer.set_run_id(run_id)  # ★ 复用旧run_id

        if self.run_state.status == "completed":
            self.tracer.info("agent.resume.already_done", detail=run_id)
            print(f"✅ [{run_id}] 已完成")
            return self.run_state.to_dict()

        self.tracer.info("agent.resume", step=self.run_state.current_state,
                          detail=f"从 {STATE_NAMES.get(self.run_state.current_state)} 恢复")
        print(f"\n🔄 恢复 [{run_id}] @ {STATE_NAMES.get(self.run_state.current_state)}")
        return self._drive_state_machine()

    def _drive_state_machine(self) -> dict:
        """
        ★ P0-4: 核心状态机驱动逻辑（run 和 resume 共用）。

        ★ P0-5 状态分级错误处理:
          - CRITICAL 状态失败 → FAILED，停止（不允许跳过）
          - OPTIONAL 状态失败 → 记录warning，降级跳过
          - HUMAN 状态失败 → PAUSED，等待人工
        """
        import time as _time

        max_retries = getattr(self.config.runtime, 'max_retries', 3)
        no_retry_states = {State.CLARIFICATION_GATE, State.ASK_HUMAN, State.DONE}

        try:
            while self.run_state.current_state != State.DONE:
                state = self.run_state.current_state
                name = STATE_NAMES.get(state, state)
                self._save_state()

                handler = self._handlers.get(state)
                if handler:
                    success, last_error = self._execute_with_retry(
                        handler, state, name, max_retries, no_retry_states, _time
                    )

                    if not success and last_error:
                        if state in CRITICAL_STATES:
                            # ★ 关键状态失败 → 直接FAILED，禁止跳过
                            self.run_state.errors.append(
                                f"[CRITICAL] [{state}] {name}: {last_error}"
                            )
                            self.run_state.status = "failed"
                            self._save_state()
                            self.tracer.error(
                                "agent.run.critical_failed", step=state,
                                detail=f"关键状态 {name} 失败，停止: {last_error}"
                            )
                            print(f"\n❌ 关键状态 [{state}] {name} 失败，停止执行")
                            return self.run_state.to_dict()

                        elif state in OPTIONAL_STATES:
                            # ★ 可选状态失败 → 降级跳过
                            self.run_state.errors.append(
                                f"[OPTIONAL] [{state}] {name}: {last_error}"
                            )
                            print(f"    ⚠️ [{state}] {name} 失败（可选），降级跳过")
                            self.tracer.warning("agent.run.degraded", step=state,
                                                detail=f"跳过 {name}: {last_error}")

                        elif state in HUMAN_STATES:
                            # ★ 人工状态失败 → PAUSED
                            self.run_state.status = "paused"
                            self._save_state()
                            self.tracer.info("agent.run.paused_human", step=state)
                            return self.run_state.to_dict()

                # 状态自动转换
                next_state = TRANSITIONS.get(state)
                if next_state:
                    self.run_state.current_state = next_state
                elif state == State.DONE:
                    break
                elif state == State.WAITING_CLARIFICATION:
                    self.run_state.status = "paused"
                    self._save_state()
                    return self.run_state.to_dict()

        except Exception as e:
            self.tracer.error("agent.run.error", step=self.run_state.current_state, detail=str(e))
            logger.exception(f"运行异常: {e}")
            self.run_state.errors.append(str(e))
            self.run_state.status = "failed"
            self._save_state()
            raise

        self.run_state.status = "completed"
        self._save_state()
        self.tracer.info("agent.run.done", step="200", detail=self.run_state.mr_url)
        print(f"\n✅ 完成 [{self.run_state.run_id}]")
        return self.run_state.to_dict()

    def _execute_with_retry(self, handler, state, name, max_retries, no_retry_states, _time) -> tuple[bool, any]:
        """执行单个状态处理器，带重试。返回 (success, last_error)。"""
        retries = 0 if state in no_retry_states else max_retries
        for attempt in range(retries + 1):
            try:
                with self.tracer.span(f"state.{name}", step=state):
                    result = handler()
                    if result == "PAUSE":
                        self.run_state.status = "paused"
                        self._save_state()
                        self.tracer.info("agent.run.paused", step=state)
                return True, None
            except Exception as e:
                if attempt < retries:
                    wait = 2 ** attempt
                    self.tracer.warning(
                        f"state.{name}.retry", step=state,
                        detail={"attempt": attempt + 1, "wait": wait, "error": str(e)[:200]}
                    )
                    print(f"    ⏳ 重试 {attempt + 1}/{retries} ({wait}s后)...")
                    _time.sleep(wait)
                else:
                    self.tracer.error(
                        f"state.{name}.failed", step=state,
                        detail={"retries_exhausted": True, "error": str(e)[:200]}
                    )
                    return False, e
    def _handle_init(self):
        """
        [000] 初始化 — 输出项目基本信息。

        日志：记录项目名称和配置摘要
        """
        self.tracer.debug("state.init", step="000",
                          detail={"project": self.config.project.name,
                                  "model": self.config.runtime.llm_model})
        print(f"    项目: {self.config.project.name}")

    def _handle_warmup_check(self):
        """
        [010] 检查知识库是否存在。

        v0.1 简化实现：仅检查 .agent/knowledge/ 目录是否存在。
        v0.2 计划：验证知识库新鲜度，过期则提示重建。

        日志：记录知识库路径和存在状态
        """
        kb_path = Path(".agent/knowledge")
        exists = kb_path.exists()
        self.tracer.debug("state.warmup_check", step="010",
                          detail={"path": str(kb_path), "exists": exists})
        print(f"    {'✅ 已构建' if exists else '⚠️ 未构建，请先运行 agent warmup'}")

    def _handle_summary_refresh(self):
        """
        [015] 刷新第一层摘要（Summary 层）。

        实际刷新在 warmup 阶段完成，此处仅记录时间戳。
        方案 v4 §4.1：Summary 层每次 run 前自动刷新。

        日志：记录刷新时间戳
        """
        ts = datetime.now().isoformat()
        self.run_state.transcript.append({"state": "SUMMARY_REFRESH", "ts": ts})
        self.tracer.debug("state.summary_refresh", step="015", detail=ts)

    def _handle_worktree_guard(self):
        """
        [018] 工作区保护检查 — 通过 Git MCP Server。

        ★ P3-17: 使用 git_status MCP 工具（替代直接 subprocess）。

        日志：记录文件变更数、状态（clean/dirty）
        """
        require_clean = self.config.git.worktree_policy.require_clean_before_run
        allow_untracked = self.config.git.worktree_policy.allow_untracked

        if not require_clean:
            self.tracer.debug("state.worktree_guard", step="018", detail="策略跳过")
            print("    ⏭️ 跳过工作区检查（策略配置）")
            return

        # ★ P3-17: 通过 Git MCP 获取状态
        status_resp = self._mcp_call(self._git_mcp, "git_status")
        changed = status_resp.get("changed_files", [])
        is_clean = status_resp.get("clean", False)

        if is_clean or not changed:
            self.tracer.debug("state.worktree_guard", step="018",
                              detail={"status": "clean"})
            print("    ✅ 工作区干净")
            return

        untracked_only = all(
            line.startswith("??") for line in changed if line.strip()
        )

        if allow_untracked and untracked_only:
            self.tracer.debug("state.worktree_guard", step="018",
                              detail={"status": "untracked_only", "count": len(changed)})
            print("    ✅ 仅存在未跟踪文件（允许）")
            return

        dirty = [l for l in changed if not l.startswith("??")]
        count = len(dirty)

        self.tracer.warning("state.worktree_guard", step="018",
                            detail={"status": "dirty", "count": count, "files": dirty[:10]})
        raise RuntimeError(
            f"工作区不干净，有 {count} 个未提交修改:\n" +
            "\n".join(f"  {l}" for l in dirty[:10])
        )

    def _handle_load_requirement(self):
        """
        [020] 加载需求 — 记录需求文本基本信息。

        日志：记录需求字符数（用于后续 token 估算）
        """
        length = len(self.run_state.requirement)
        self.tracer.debug("state.load_requirement", step="020",
                          detail={"chars": length})
        print(f"    需求长度: {length} 字符")

    def _handle_retrieve_context(self):
        """
        [030] 检索上下文 — 用 LLM 初步分析需求。

        将需求前 500 字符发送给 LLM，获取初步理解。
        结果存入 run_state.transcript 供 UNDERSTAND_REQUIREMENT 使用。

        v0.2 计划：Knowledge MCP 三层检索。
        """
        self.tracer.debug("state.retrieve_context", step="030",
                          detail={"input_chars": min(500, len(self.run_state.requirement))})
        resp = self.llm.chat_with_system(
            f"分析以下需求，提取：1)目标模块 2)涉及的数据模型 3)需要的接口变更 4)风险点:\n{self.run_state.requirement[:500]}"
        )
        context = self.llm.extract_content(resp)
        self.run_state.transcript.append({
            "state": "RETRIEVE_CONTEXT",
            "context": context,
            "ts": datetime.now().isoformat()
        })
        self.tracer.debug("state.retrieve_context", step="030",
                          detail={"output_chars": len(context)})

    def _handle_understand_requirement(self):
        """
        [040] 理解需求 — 基于检索的上下文做结构化理解。

        从 transcript 中读取 RETRIEVE_CONTEXT 的 LLM 分析结果，
        用 LLM 二次提取为结构化 JSON（模块、数据模型、接口、风险）。

        输出存入 transcript 供后续状态使用。
        """
        # 获取上一步的上下文
        context_text = ""
        for entry in reversed(self.run_state.transcript):
            if entry.get("state") == "RETRIEVE_CONTEXT":
                context_text = entry.get("context", "")
                break

        if not context_text:
            self.tracer.debug("state.understand_requirement", step="040",
                              detail="无上下文，跳过")
            return

        self.tracer.debug("state.understand_requirement.start", step="040")
        resp = self.llm.chat_with_system(
            f"基于以下需求分析，输出结构化JSON（modules/data_models/api_changes/risks）:\n{context_text}\n\n需求原文:\n{self.run_state.requirement[:300]}"
        )
        understanding = self.llm.extract_content(resp)
        self.run_state.transcript.append({
            "state": "UNDERSTAND_REQUIREMENT",
            "understanding": understanding,
            "ts": datetime.now().isoformat()
        })
        self.tracer.info("state.understand_requirement", step="040",
                         detail={"output_chars": len(understanding)})

    def _handle_clarification_gate(self):
        """
        [050] 澄清判断闸门 — 判断需求是否足够清晰。

        流程：
          1. 将需求发送给 LLM
          2. LLM 返回 "CLEAR" → 继续执行
          3. LLM 返回问题列表 → 切换到 ASK_HUMAN 状态

        日志：记录 LLM 判断结果
        """
        self.tracer.debug("state.clarification_gate", step="050")
        resp = self.llm.chat_with_system(
            f"判断需求是否清晰。清晰回复 CLEAR，否则列出问题:\n{self.run_state.requirement}"
        )
        content = self.llm.extract_content(resp)
        is_clear = "CLEAR" in content.upper()

        self.tracer.debug("state.clarification_gate", step="050",
                          detail={"clear": is_clear})

        if is_clear:
            print("    ✅ 需求清晰")
        else:
            # ── 需求不清晰，转向提问 ──
            self.run_state.current_state = State.ASK_HUMAN
            self.tracer.info("state.clarification_gate", step="050",
                             detail="需求不清晰，转向 ASK_HUMAN")

    def _handle_ask_human(self):
        """
        [060] 生成澄清问题 — LLM 列出最多 5 个问题。

        流程：
          1. LLM 分析需求中的模糊点
          2. 生成结构化问题列表
          3. 暂停状态机，等待人类回复

        返回：
            "PAUSE" — 通知主循环暂停

        日志：记录生成的问题
        """
        questions = self.llm.chat_with_system(
            f"为此需求列出最多5个需要澄清的问题:\n{self.run_state.requirement}"
        )
        content = self.llm.extract_content(questions)
        self.tracer.info("state.ask_human", step="060", detail=content)
        print(f"    ❓ 需要澄清:\n{content}")
        return "PAUSE"

    def _handle_plan_implementation(self):
        """
        [100] 生成实施计划 — LLM 规划文件列表和步骤。

        流程：
          1. LLM 分析需求生成计划
          2. 计划持久化到 transcript

        日志：记录生成的计划
        """
        self.tracer.debug("state.plan_implementation.start", step="100")
        print("    📐 生成计划...")

        plan = self.llm.chat_with_system(
            f"为此需求生成实施计划（文件列表+步骤）:\n{self.run_state.requirement}"
        )
        plan_text = self.llm.extract_content(plan)

        # ── 持久化计划 ──
        self.run_state.transcript.append({
            "state": "PLAN",
            "plan": plan_text,
            "ts": datetime.now().isoformat()
        })
        self.tracer.info("state.plan_implementation", step="100",
                         detail=plan_text[:200])
        print(f"    📋 {plan_text[:150]}...")

    def _handle_create_branch(self):
        """
        [110] 创建 Git 分支 — 通过 Git MCP Server。

        ★ P1-6: 使用 git_create_branch MCP 工具（替代直接 subprocess）。

        日志：记录分支名和创建结果
        """
        prefix = self.config.git.branch_prefix  # 默认 "agent/"
        slug = date.today().strftime("%Y%m%d") + "-auto"
        branch = prefix + slug

        # ★ 分支命名校验
        naming = self.config.git.branch_naming
        if not re.match(naming.regex, branch):
            raise RuntimeError(
                f"分支名不符合规范: {branch}\n"
                f"  要求匹配: {naming.regex}"
            )

        self.run_state.branch_name = branch
        self.tracer.debug("state.create_branch.start", step="110",
                          detail={"branch": branch})

        # ★ 通过 Git MCP Server 创建分支
        result = self._mcp_call(self._git_mcp, "git_create_branch",
                                branch_name=branch)

        self.tracer.info("state.create_branch", step="110",
                         detail={"branch": branch, "result": result})
        print(f"    🌿 分支已创建: {branch}")

    def _handle_implement(self):
        """
        [120] 修改代码 — LLM 生成代码变更并写入文件。

        ★ P4-22: 优先使用 @@PATCH 格式（仅变更部分），避免完整文件替换误删内容。

        流程：
          1. LLM 分析需求生成 unified diff patch（@@PATCH 格式）
          2. code_parser 自动检测输出格式（PATCH > FILE > 代码块 > ...）
          3. 安全边界检查
          4. 写入文件
        """
        self.tracer.debug("state.implement.start", step="120")
        print("    ✏️ 生成代码修改...")

        # ★ P4-22: 优先要求 LLM 使用 @@PATCH unified diff 格式
        changes = self.llm.chat_with_system(
            f"为此需求生成代码修改。优先使用 unified diff patch 格式：\n"
            f"  @@PATCH:相对路径@@\n"
            f"  @@ -起始行,行数 +起始行,行数 @@\n"
            f"   保留的上下文行\n"
            f"  -删除的行\n"
            f"  +新增的行\n"
            f"  @@END@@\n\n"
            f"如果是新文件，使用完整内容格式：\n"
            f"  @@FILE:相对路径@@\n"
            f"  完整文件内容\n"
            f"  @@END@@\n\n"
            f"需求:\n{self.run_state.requirement}"
        )
        content = self.llm.extract_content(changes)

        parsed = parse_code_changes(content)

        if not parsed.success:
            self.tracer.warning("state.implement.parse_failed", step="120",
                                detail={"errors": parsed.errors, "raw_len": len(content)})
            print(f"    ⚠️ LLM输出格式无法解析 ({parsed.used_format})")
            print(f"    原始输出(前200字符): {content[:200]}")
            return

        # ── 写入文件 ──
        blocked: list[str] = []
        for cf in parsed.files:
            fpath = cf.path
            if self.config.is_path_denied(fpath):
                blocked.append(fpath)
                self.tracer.warning("state.implement.blocked", step="120",
                                    detail={"path": fpath, "reason": "deny_paths"})
                continue

            pf = Path(fpath)
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(cf.content, encoding="utf-8")
            self.run_state.files_changed.append(fpath)
            self.run_state.lines_changed += cf.line_count

        if blocked:
            print(f"    ⚠️ 已拦截 {len(blocked)} 个禁止文件: {', '.join(blocked)}")

        file_count = len(self.run_state.files_changed)
        self.tracer.info("state.implement", step="120",
                         detail={"files": file_count,
                                 "lines": self.run_state.lines_changed,
                                 "format": parsed.used_format,
                                 "paths": self.run_state.files_changed})
        print(f"    ✅ {file_count} 文件, ~{self.run_state.lines_changed} 行 "
              f"(格式: {parsed.used_format})")

    def _handle_change_scope_guard(self):
        """
        [125] 变更范围护栏 — 检查是否超出允许的变更范围。

        检查项（方案 v4 §8.2）：
          - 文件数是否超过 max_files_changed（默认 20）
          - 行数是否超过 max_lines_changed（默认 800）
          - ★ 是否触碰到 deny_paths 中的受保护文件

        日志：记录文件数、行数、违规文件和是否超限
        """
        from fnmatch import fnmatch

        policy = self.config.change_policy
        n = len(self.run_state.files_changed)
        lines = self.run_state.lines_changed

        violations: list[str] = []

        # ── 检查文件数量 ──
        if n > policy.max_files_changed:
            violations.append(f"文件数 {n} > {policy.max_files_changed}")

        # ── 检查行数 ──
        if lines > int(policy.max_lines_changed):
            violations.append(f"行数 {lines} > {policy.max_lines_changed}")

        # ── ★ 检查 deny_paths（glob 匹配） ──
        all_deny = policy.deny_paths + (policy.deny_path_globs or [])
        for fpath in self.run_state.files_changed:
            for pattern in all_deny:
                if fnmatch(fpath, pattern) or fnmatch(Path(fpath).name, pattern):
                    violations.append(f"禁止路径: {fpath} (匹配: {pattern})")
                    break

        within_limit = len(violations) == 0

        self.tracer.info("state.change_scope_guard", step="125",
                         detail={"files": n, "lines": lines,
                                 "violations": violations, "ok": within_limit})

        if within_limit:
            print(f"    ✅ {n} 文件, ~{lines} 行")
        else:
            print(f"    ⚠️ 违规: {'; '.join(violations)}")
            self.tracer.warning("state.change_scope_guard", step="125",
                                detail=f"超出变更范围: {'; '.join(violations)}")

    def _handle_database_impact_detect(self):
        """
        [130] 数据库影响检测 — 分析代码变更是否涉及数据库。

        v0.1：跳过数据库检测（方案 v4 §2.1: Database MCP 只做 DDL 索引）。
        v0.2 计划：基于 DDL 索引检测表结构变更影响。

        日志：标记跳过
        """
        self.tracer.debug("state.database_impact_detect", step="130",
                          detail="v0.1 跳过")
        print("    ℹ️ v0.1: 跳过数据库检测")

    def _handle_prepare_commit(self):
        """
        [150] 准备提交 — 记录提交前的文件快照。

        将变更文件列表写入 transcript，供 MR 描述生成使用。

        日志：记录文件列表
        """
        self.tracer.debug("state.prepare_commit", step="150",
                          detail={"files": self.run_state.files_changed})
        self.run_state.transcript.append({
            "state": "PREPARE_COMMIT",
            "files": self.run_state.files_changed,
            "ts": datetime.now().isoformat()
        })

    def _handle_commit(self):
        """
        [160] 执行 Git Commit — 通过 Git MCP Server。

        ★ P1-6: 使用 git_commit MCP 工具（替代直接 subprocess）。
        ★ P1-10 联动: files 参数必须显式传入。

        日志：记录 commit SHA、文件列表和消息
        """
        files = self.run_state.files_changed
        if not files:
            raise RuntimeError("没有文件需要提交")

        self.tracer.debug("state.commit.start", step="160",
                          detail={"files": files})

        # ── 生成 Commit Message ──
        msg = f"feat: {self.run_state.requirement[:80]}"

        # ★ 通过 Git MCP Server 提交（显式传入 files）
        result = self._mcp_call(self._git_mcp, "git_commit",
                                message=msg, files=files)
        self.run_state.commit_hash = result.get("sha", "unknown")

        self.tracer.info("state.commit", step="160",
                         detail={"sha": self.run_state.commit_hash,
                                 "files": len(files),
                                 "message": msg})
        print(f"    💾 [{self.run_state.commit_hash}] {msg}")

    def _handle_push(self):
        """
        [170] 推送代码 — 通过 Git MCP Server。

        ★ P1-6: 使用 git_push MCP 工具（替代直接 subprocess）。

        日志：记录推送的分支名和结果
        """
        branch = self.run_state.branch_name
        self.tracer.debug("state.push.start", step="170",
                          detail={"branch": branch})

        # ★ 通过 Git MCP Server 推送（安全校验在 server 端完成）
        result = self._mcp_call(self._git_mcp, "git_push", branch=branch)

        self.tracer.info("state.push", step="170",
                         detail={"branch": branch, "result": result})
        print(f"    🚀 已推送 {branch} → origin")

    def _handle_create_mr(self):
        """
        [180] 创建 Merge Request — 通过 MR MCP Server。

        ★ P1-6: 使用 mr_create MCP 工具（替代直接构造 GitHub URL）。

        日志：记录 MR URL 和描述文件路径
        """
        self.tracer.debug("state.create_mr.start", step="180")

        # ── 解析 repo 名称（owner/repo） ──
        repo = self._resolve_repo_name()

        # ── 生成 MR 描述 ──
        mr_desc = self._gen_mr_desc()
        title = f"[Agent] {self.run_state.requirement[:72]}"

        # ★ 通过 MR MCP Server 创建 MR
        try:
            result = self._mcp_call(
                self._mr_mcp, "mr_create",
                title=title,
                description=mr_desc,
                source_branch=self.run_state.branch_name,
                repo=repo,
            )
            mr_url = result.get("url", "")
        except Exception as e:
            # 降级：如果 MCP 失败，生成手动 MR 描述文件
            self.tracer.warning("state.create_mr.mcp_failed", step="180",
                                detail=str(e))
            mr_url = f"MR 创建失败 ({e})，见 mr_description.md"

        self.run_state.mr_url = mr_url

        # ── 写入 MR 描述文件 ──
        dp = Path(f".agent/runs/{self.run_state.run_id}/mr_description.md")
        dp.write_text(mr_desc, encoding="utf-8")

        self.tracer.info("state.create_mr", step="180",
                         detail={"url": mr_url, "desc_file": str(dp)})
        print(f"    📬 {mr_url}")

    def _resolve_repo_name(self) -> str:
        """
        ★ P3-17: 通过 Git MCP 解析 repo 名。

        从 git remote origin URL 提取 owner/repo。
        """
        try:
            result = self._mcp_call(self._git_mcp, "git_remote_get_url",
                                    remote_name="origin")
            remote_url = result.get("url", "")
            if "github.com" in remote_url:
                m = re.search(r"github\\.com[:/](.+?)(?:\\.git)?$", remote_url)
                if m:
                    return m.group(1)
            return "unknown/unknown"
        except Exception:
            return "unknown/unknown"

    def _handle_done(self):
        """
        [200] 完成 — 输出最终结果。

        日志：记录完成状态和 MR URL
        """
        self.tracer.info("state.done", step="200",
                         detail={"mr_url": self.run_state.mr_url,
                                 "files": len(self.run_state.files_changed),
                                 "commit": self.run_state.commit_hash})
        print(f"    🎉 MR: {self.run_state.mr_url}")

    # ==================================================================
    # 辅助方法
    # ==================================================================

    def _gen_mr_desc(self) -> str:
        """
        生成 MR 描述（Markdown 格式）。

        模板包含（方案 v4 §12.1）：
          - 已执行步骤清单（含勾选框）
          - 未执行步骤（单元测试等）
          - 变更文件列表
          - 需求原文
          - Reviewer 注意事项

        返回：
            str: Markdown 格式的 MR 描述
        """
        files = "\n".join(f"- `{f}`" for f in self.run_state.files_changed)
        return f"""## Agent 自动生成 MR

### 执行情况
- [x] 需求读取
- [x] 上下文检索
- [x] 代码修改
- [x] commit & push
- [ ] 单元测试（未执行）
- [ ] 人工 Review

### 变更文件
{files}

### 需求原文
{self.run_state.requirement}

### Reviewer 注意事项
1. 请检查业务逻辑正确性
2. 请确认边界条件处理
3. 请评估是否需要补充测试

> ⚠️ 此 MR 由 neiWangAgent 自动生成，请人工 Review 后合并
"""

    def _ensure_run_dir(self):
        """
        确保运行目录存在。

        创建路径：.agent/runs/{run_id}/
        """
        Path(f".agent/runs/{self.run_state.run_id}").mkdir(parents=True, exist_ok=True)

    def _save_state(self):
        """
        持久化当前运行状态到 .agent/runs/{run_id}/state.json。

        每次状态转换后自动调用，确保即使崩溃也能恢复。
        """
        if self.run_state:
            self._ensure_run_dir()
            p = Path(f".agent/runs/{self.run_state.run_id}/state.json")
            p.write_text(
                json.dumps(self.run_state.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

    def close(self):
        """
        关闭 LLM 客户端，释放 HTTP 连接。
        """
        self.llm.close()
        self.tracer.debug("agent.shutdown")

    def warmup(self) -> dict:
        """
        知识库预热 — 三层预理解模型（方案 v4 §4.1）。

        ★ P5-27: 增量更新 — 检测文件变更，仅重建受影响层级。

        依次执行：
          1. Summary 层 — 代码库文件索引
          2. Hotspot 层 — 核心模块分析
          3. Deep 层    — 深度代码索引（含 MyBatis/Vue）

        预热结果持久化到 .agent/knowledge/state.json
        """
        from agent_mcp.knowledge_server import KnowledgeMCPServer

        self.tracer.info("agent.warmup.start")
        ks = KnowledgeMCPServer()
        repo = str(Path.cwd())
        result = {"layers": {}, "files_total": 0}

        # ★ P5-27: 检测是否需要增量更新
        invalidation_reason = self._check_knowledge_freshness()
        if invalidation_reason:
            print(f"    📌 {invalidation_reason}")

        # ── Summary 层 ──
        with self.tracer.span("warmup.summary"):
            print("  [summary] 扫描代码库...")
            r = ks._index_codebase(repo, "summary")
            result["layers"]["summary"] = r
            result["files_total"] += r.get("files_indexed", 0)
            print(f"  [summary] {r.get('files_indexed', 0)} 个文件")

        # ── Hotspot 层 ──
        with self.tracer.span("warmup.hotspot"):
            print("  [hotspot] 分析核心模块...")
            r = ks._index_codebase(repo, "hotspot")
            result["layers"]["hotspot"] = r
            print(f"  [hotspot] {r.get('message', '完成')}")

        # ── Deep 层 ──
        with self.tracer.span("warmup.deep"):
            print("  [deep] 深度索引...")
            r = ks._index_codebase(repo, "deep")
            result["layers"]["deep"] = r
            print(f"  [deep] {r.get('message', '完成')}")

        # ── 保存知识库状态 ──
        Path(".agent/knowledge").mkdir(parents=True, exist_ok=True)
        kb_path = Path(".agent/knowledge/state.json")
        result["updated_at"] = datetime.now().isoformat()
        kb_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

        self.tracer.info("agent.warmup.done",
                         detail={"files_total": result["files_total"]})
        print(f"\n✅ 预热完成，共索引 {result['files_total']} 个文件")
        return result

    def _check_knowledge_freshness(self) -> str:
        """
        ★ P5-27: 检测知识库新鲜度。

        检查项（方案 v4 §4.3）：
          - target_branch 是否有新提交
          - 核心模块文件是否变更
          - DDL 文件是否变更
          - Mapper XML 是否变更
        """
        kb_state = Path(".agent/knowledge/state.json")
        if not kb_state.exists():
            return "知识库未构建，将首次构建"

        try:
            prev = json.loads(kb_state.read_text(encoding="utf-8"))
        except Exception:
            return "知识库状态损坏，将重建"

        prev_at = prev.get("updated_at", "")
        # 检查 git 是否有新提交
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-1", "--since", prev_at[:19] if prev_at else "1970-01-01"],
                capture_output=True, text=True
            )
            if result.stdout.strip():
                return f"检测到新提交，将增量更新"
        except Exception:
            pass

        return ""  # 知识库新鲜，不需要提示
