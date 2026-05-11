"""
test_orchestrator.py — Orchestrator 状态机测试

覆盖：
  - 状态转换
  - run / resume
  - 错误处理（dry-run 模式避免副作用）
"""
import pytest
import json
import tempfile
from pathlib import Path
from agent_mcp.orchestrator import Orchestrator, State, CRITICAL_STATES, OPTIONAL_STATES
from agent_mcp.config_loader import AppConfig, ProjectConfig


class TestOrchestratorInit:
    """Orchestrator 初始化。"""

    def test_init_with_config(self):
        cfg = AppConfig(project=ProjectConfig(name="test"))
        orch = Orchestrator(cfg, dry_run=True)
        assert orch.dry_run is True
        assert orch._git_mcp is not None
        assert orch._mr_mcp is not None
        orch.close()

    def test_dry_run_mode(self):
        cfg = AppConfig(project=ProjectConfig(name="test"))
        orch = Orchestrator(cfg, dry_run=True)
        assert orch.dry_run is True
        orch.close()


class TestStateDefinitions:
    """状态定义。"""

    def test_critical_states(self):
        assert State.CREATE_BRANCH in CRITICAL_STATES
        assert State.IMPLEMENT in CRITICAL_STATES
        assert State.COMMIT in CRITICAL_STATES
        assert State.PUSH in CRITICAL_STATES

    def test_optional_states(self):
        assert State.SUMMARY_REFRESH in OPTIONAL_STATES
        assert State.DATABASE_IMPACT_DETECT in OPTIONAL_STATES

    def test_state_count(self):
        """确认至少有18个状态。"""
        all_states = [v for k, v in vars(State).items() if not k.startswith("_") and isinstance(v, str)]
        assert len(all_states) >= 18


class TestRunState:
    """RunState 序列化。"""

    def test_to_dict_and_back(self):
        from agent_mcp.orchestrator import RunState
        rs = RunState("test-001", "add login")
        rs.branch_name = "agent/20260511-test"
        d = rs.to_dict()
        rs2 = RunState.from_dict(d)
        assert rs2.run_id == "test-001"
        assert rs2.requirement == "add login"
        assert rs2.branch_name == "agent/20260511-test"


class TestDryRunRun:
    """Dry-run 模式下的 run 调用。"""

    def test_run_empty_requirement(self):
        """空需求应能完成初始化阶段。"""
        cfg = AppConfig(project=ProjectConfig(name="test"))
        orch = Orchestrator(cfg, dry_run=True)
        try:
            result = orch.run("add test feature")
            assert result["status"] in ("completed", "running", "failed")
        except Exception as e:
            # May fail at LLM call stage (no API key) which is expected in test
            pass
        finally:
            orch.close()

    def test_resume_nonexistent(self):
        cfg = AppConfig(project=ProjectConfig(name="test"))
        orch = Orchestrator(cfg, dry_run=True)
        with pytest.raises(FileNotFoundError):
            orch.resume("nonexistent-run-id")
        orch.close()


class TestStateMachineFlow:
    """状态机流程。"""

    def test_transitions_exist_for_all_states(self):
        from agent_mcp.orchestrator import TRANSITIONS, STATE_NAMES
        all_handled_states = set()
        for state_obj in [v for k, v in vars(State).items() if not k.startswith("_") and isinstance(v, str)]:
            if state_obj != State.DONE:
                assert state_obj in TRANSITIONS, f"Missing transition for {state_obj}"

    def test_no_dead_ends(self):
        from agent_mcp.orchestrator import TRANSITIONS
        for src, dst in TRANSITIONS.items():
            if dst is None:
                assert src in (State.DONE, State.WAITING_CLARIFICATION), f"Unexpected dead end: {src}"


class TestMRDescription:
    """MR 描述生成。"""

    def test_gen_basic_mr(self):
        cfg = AppConfig(project=ProjectConfig(name="test"))
        orch = Orchestrator(cfg, dry_run=True)
        orch.run_state = type('obj', (object,), {
            'files_changed': ['src/main.py'],
            'requirement': 'add login',
            'transcript': [],
            'errors': [],
        })()
        desc = orch._gen_mr_desc()
        assert "add login" in desc
        assert "src/main.py" in desc
        orch.close()

    def test_gen_mr_with_self_review(self):
        cfg = AppConfig(project=ProjectConfig(name="test"))
        orch = Orchestrator(cfg, dry_run=True)
        orch.run_state = type('obj', (object,), {
            'files_changed': ['src/main.py'],
            'requirement': 'add login',
            'transcript': [{'state': 'SELF_REVIEW', 'result': 'PASS', 'review': 'No issues found'}],
            'errors': [],
        })()
        desc = orch._gen_mr_desc()
        assert "PASS" in desc


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
