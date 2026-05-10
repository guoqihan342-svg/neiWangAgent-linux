"""
Orchestrator — 核心状态机编排器 v0.1

实现 Agent 从需求到 MR 的完整流程：
INIT → WARMUP_CHECK → ... → DONE
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

logger = logging.getLogger(__name__)


class State:
    """状态常量"""
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


# 状态转换表
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
            "run_id": self.run_id,
            "requirement": self.requirement,
            "current_state": self.current_state,
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
    """
    核心编排器 — 驱动状态机完成从需求到 MR 的全流程。
    v0.1: LLM 作为主要决策引擎。
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = LLMClient(config)
        self.run_state: RunState | None = None
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

    # ── 公开 API ──

    def run(self, requirement_text: str) -> dict:
        """从 INIT 到 DONE 的完整流程"""
        run_id = f"{date.today().strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}"
        self.run_state = RunState(run_id, requirement_text)
        self._ensure_run_dir()

        print(f"\n🚀 Agent 运行 [{run_id}]")
        print(f"📋 {requirement_text[:100]}...\n")

        try:
            while self.run_state.current_state != State.DONE:
                state = self.run_state.current_state
                name = STATE_NAMES.get(state, state)
                print(f"  [{state}] {name}...")
                self._save_state()

                handler = self._handlers.get(state)
                if handler:
                    result = handler()
                    if result == "PAUSE":
                        self.run_state.status = "paused"
                        self._save_state()
                        return self.run_state.to_dict()

                # 自动转换
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
            logger.exception(f"异常: {e}")
            self.run_state.errors.append(str(e))
            self.run_state.status = "failed"
            self._save_state()
            raise

        self.run_state.status = "completed"
        self._save_state()
        print(f"\n✅ 完成 [{run_id}]")
        return self.run_state.to_dict()

    def resume(self, run_id: str) -> dict:
        """从保存的状态恢复"""
        state_path = Path(f".agent/runs/{run_id}/state.json")
        if not state_path.exists():
            raise FileNotFoundError(f"状态不存在: {state_path}")

        data = json.loads(state_path.read_text())
        self.run_state = RunState.from_dict(data)

        if self.run_state.status == "completed":
            print(f"✅ [{run_id}] 已完成")
            return self.run_state.to_dict()

        print(f"\n🔄 恢复 [{run_id}] @ {STATE_NAMES.get(self.run_state.current_state)}")
        return self.run(requirement_text=self.run_state.requirement)

    # ── 状态处理器 ──

    def _handle_init(self):
        print(f"    项目: {self.config.project.name}")

    def _handle_warmup_check(self):
        kb_path = Path(".agent/knowledge")
        print(f"    {'✅' if kb_path.exists() else '⚠️ 未构建'}")

    def _handle_summary_refresh(self):
        self.run_state.transcript.append({"state": "SUMMARY_REFRESH", "ts": datetime.now().isoformat()})

    def _handle_worktree_guard(self):
        require_clean = self.config.git.worktree_policy.require_clean_before_run
        allow_untracked = self.config.git.worktree_policy.allow_untracked
        if not require_clean:
            print("    ⏭️ 跳过工作区检查（策略配置）")
            return
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=True
        )
        lines = result.stdout.strip().splitlines()
        if not lines:
            print("    ✅ 工作区干净")
            return
        if allow_untracked:
            # 过滤掉 ?? 开头（未跟踪）的行
            dirty = [l for l in lines if not l.startswith("??")]
            if not dirty:
                print("    ✅ 仅存在未跟踪文件（允许）")
                return
            raise RuntimeError(
                f"工作区有已跟踪文件的未提交修改:\n" + "\n".join(dirty)
            )
        raise RuntimeError(
            f"工作区不干净，有未提交的修改:\n" + "\n".join(lines)
        )

    def _handle_load_requirement(self):
        print(f"    {len(self.run_state.requirement)} 字符")

    def _handle_retrieve_context(self):
        _ = self.llm.chat_with_system(f"分析需求: {self.run_state.requirement[:500]}")

    def _handle_understand_requirement(self):
        pass

    def _handle_clarification_gate(self):
        resp = self.llm.chat_with_system(
            f"判断需求是否清晰。清晰回复 CLEAR，否则列出问题:\n{self.run_state.requirement}"
        )
        content = self.llm.extract_content(resp)
        if "CLEAR" in content.upper():
            print("    ✅ 清晰")
        else:
            self.run_state.current_state = State.ASK_HUMAN

    def _handle_ask_human(self):
        questions = self.llm.chat_with_system(f"为此需求列出最多5个问题:\n{self.run_state.requirement}")
        print(f"    {self.llm.extract_content(questions)}")
        return "PAUSE"

    def _handle_plan_implementation(self):
        print("    📐 生成计划...")
        plan = self.llm.chat_with_system(
            f"为此需求生成实施计划（文件列表+步骤）:\n{self.run_state.requirement}"
        )
        self.run_state.transcript.append({
            "state": "PLAN", "plan": self.llm.extract_content(plan),
            "ts": datetime.now().isoformat()
        })

    def _handle_create_branch(self):
        prefix = self.config.git.branch_prefix
        slug = date.today().strftime("%Y%m%d") + "-auto"
        branch = prefix + slug
        self.run_state.branch_name = branch
        result = subprocess.run(
            ["git", "checkout", "-b", branch],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"创建分支失败: {result.stderr.strip()}")
        print(f"    🌿 分支已创建: {branch}")

    def _handle_implement(self):
        print("    ✏️ 生成代码修改...")
        changes = self.llm.chat_with_system(
            f"根据需求生成代码修改。对每个文件用 @@FILE:path@@ 标记后跟完整内容:\n{self.run_state.requirement}"
        )
        content = self.llm.extract_content(changes)
        file_blocks = re.split(r"@@FILE:(.+?)@@", content)
        for i in range(1, len(file_blocks), 2):
            if i + 1 < len(file_blocks):
                fpath = file_blocks[i].strip()
                code = file_blocks[i + 1].strip()
                if fpath and code:
                    pf = Path(fpath)
                    pf.parent.mkdir(parents=True, exist_ok=True)
                    pf.write_text(code)
                    self.run_state.files_changed.append(fpath)
                    print(f"    📝 {fpath}")
        print(f"    ✅ {len(self.run_state.files_changed)} 文件")

    def _handle_change_scope_guard(self):
        policy = self.config.change_policy
        n = len(self.run_state.files_changed)
        ok = n <= policy.max_files_changed
        print(f"    {'✅' if ok else '⚠️'} {n} 文件 (限制: {policy.max_files_changed})")

    def _handle_database_impact_detect(self):
        print("    ℹ️ v0.1: 跳过数据库检测")

    def _handle_prepare_commit(self):
        self.run_state.transcript.append({
            "state": "PREPARE_COMMIT", "files": self.run_state.files_changed,
            "ts": datetime.now().isoformat()
        })

    def _handle_commit(self):
        files = self.run_state.files_changed
        if not files:
            raise RuntimeError("没有文件需要提交")
        add_result = subprocess.run(
            ["git", "add", "--"] + files,
            capture_output=True, text=True
        )
        if add_result.returncode != 0:
            raise RuntimeError(f"git add 失败: {add_result.stderr.strip()}")
        msg = f"feat: {self.run_state.requirement[:80]}"
        commit_result = subprocess.run(
            ["git", "commit", "-m", msg],
            capture_output=True, text=True
        )
        if commit_result.returncode != 0:
            raise RuntimeError(f"git commit 失败: {commit_result.stderr.strip()}")
        # 从输出中提取 commit hash
        output = (commit_result.stdout + commit_result.stderr).strip()
        match = re.search(r"\[[^\]]+\s+([a-f0-9]+)", output)
        self.run_state.commit_hash = match.group(1) if match else "unknown"
        print(f"    💾 {msg}")

    def _handle_push(self):
        branch = self.run_state.branch_name
        if not branch.startswith("agent/"):
            raise RuntimeError(
                f"分支名 {branch} 不以 agent/ 开头，禁止推送"
            )
        result = subprocess.run(
            ["git", "push", "origin", branch],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"git push 失败: {result.stderr.strip()}")
        print(f"    🚀 已推送 {branch}")

    def _handle_create_mr(self):
        # 获取远端 URL，生成真实的 MR 链接
        mr_url = ""
        try:
            remote_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, check=True
            )
            remote_url = remote_result.stdout.strip()
            # 解析 remote URL，构造 MR/PR 链接
            # 支持 git@github.com:owner/repo.git 和 https://github.com/owner/repo.git
            r = remote_url
            if "github.com" in r:
                m = re.search(r"github\.com[:\/](.+?)(?:\.git)?$", r)
                if m:
                    repo_path = m.group(1)
                    mr_url = f"https://github.com/{repo_path}/pull/new/{self.run_state.branch_name}"
                else:
                    mr_url = f"MR 链接: {r} (分支: {self.run_state.branch_name})"
            else:
                mr_url = f"MR 链接: {r} (分支: {self.run_state.branch_name})"
        except subprocess.CalledProcessError:
            mr_url = f"MR 链接不可用（无法获取 remote URL），分支: {self.run_state.branch_name}"
        self.run_state.mr_url = mr_url
        # 写 MR 描述文件
        mr_desc = self._gen_mr_desc()
        dp = Path(f".agent/runs/{self.run_state.run_id}/mr_description.md")
        dp.write_text(mr_desc)
        print(f"    📬 {mr_url}")

    def _handle_done(self):
        print(f"    🎉 MR: {self.run_state.mr_url}")

    # ── 辅助 ──

    def _gen_mr_desc(self) -> str:
        files = "\n".join(f"- `{f}`" for f in self.run_state.files_changed)
        return f"""## Agent 自动生成 MR

- [x] 需求读取
- [x] 代码修改
- [x] commit & push
- [ ] 单元测试（未执行）
- [ ] 人工 Review

### 变更文件
{files}

### 需求
{self.run_state.requirement}

> ⚠️ Agent 自动生成，请 Review
"""

    def _ensure_run_dir(self):
        Path(f".agent/runs/{self.run_state.run_id}").mkdir(parents=True, exist_ok=True)

    def _save_state(self):
        if self.run_state:
            self._ensure_run_dir()
            p = Path(f".agent/runs/{self.run_state.run_id}/state.json")
            p.write_text(json.dumps(self.run_state.to_dict(), indent=2, ensure_ascii=False))

    def close(self):
        self.llm.close()

    def warmup(self) -> dict:
        """知识库预热 — 三层预理解模型"""
        from agent_mcp.knowledge_server import KnowledgeMCPServer
        ks = KnowledgeMCPServer()

        repo = str(Path.cwd())
        result = {"layers": {}, "files_total": 0}

        # Summary 层
        print("  [summary] 扫描代码库...")
        r = ks._index_codebase(repo, "summary")
        result["layers"]["summary"] = r
        result["files_total"] += r.get("files_indexed", 0)
        print(f"  [summary] {r.get('files_indexed', 0)} 个文件")

        # Hotspot 层
        print("  [hotspot] 分析核心模块...")
        r = ks._index_codebase(repo, "hotspot")
        result["layers"]["hotspot"] = r
        print(f"  [hotspot] {r.get('message', '完成')}")

        # Deep 层
        print("  [deep] 深度索引...")
        r = ks._index_codebase(repo, "deep")
        result["layers"]["deep"] = r
        print(f"  [deep] {r.get('message', '完成')}")

        # 保存知识库状态
        Path(".agent/knowledge").mkdir(parents=True, exist_ok=True)
        kb_path = Path(".agent/knowledge/state.json")
        kb_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

        print(f"\n✅ 预热完成，共索引 {result['files_total']} 个文件")
        return result
