"""
test_agents.py — 多 Agent 系统测试 v0.3.0

测试覆盖:
    1. Tool 创建和 schema 生成
    2. CodeAgentTool 代码执行（安全模式）
    3. BaseAgent 工具调用和解析
    4. 各 Agent 的子类实例化
    5. AgentPipeline 组装和执行
    6. 工具函数（search_code, read_file 等）
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_mcp.agents import (
    Tool, CodeAgentTool, create_tool,
    BaseAgent, ResearchAgent, PlanAgent, CodeAgent, ReviewAgent, GitAgent,
    AgentPipeline,
    _search_code, _read_file, _list_files, _get_context,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_llm_client():
    """模拟 LLM 客户端"""
    client = MagicMock()
    client.chat.return_value = {
        "content": json.dumps({"test": "ok"}),
    }
    return client


@pytest.fixture
def sample_tool():
    """示例工具"""
    return Tool(
        name="echo",
        description="回显输入",
        inputs={"text": {"type": "string", "description": "要回显的文本"}},
        forward=lambda text: f"ECHO: {text}",
    )


@pytest.fixture
def temp_project(tmp_path):
    """临时项目目录，包含测试文件"""
    # 创建目录结构
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "main.py").write_text("def hello():\n    return 'world'\n")
    (src_dir / "utils.py").write_text("import os\n\ndef helper():\n    pass\n")
    (src_dir / "config.yaml").write_text("key: value\n")
    return tmp_path


# =============================================================================
# Tool 测试
# =============================================================================

class TestTool:
    """测试 Tool 基础功能"""

    def test_tool_creation(self):
        """创建工具"""
        tool = Tool(
            name="test",
            description="测试工具",
            inputs={"x": {"type": "integer", "description": "数字"}},
            forward=lambda x: str(x * 2),
        )
        assert tool.name == "test"
        assert tool.description == "测试工具"

    def test_tool_to_openai_schema(self):
        """生成 OpenAI schema"""
        tool = Tool(
            name="read",
            description="读取文件",
            inputs={
                "path": {"type": "string", "description": "文件路径"},
                "encoding": {"type": "string", "description": "编码", "required": False},
            },
            forward=lambda path, encoding="utf-8": path,
        )
        schema = tool.to_openai_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "read"
        assert schema["function"]["description"] == "读取文件"
        params = schema["function"]["parameters"]
        assert "path" in params["properties"]
        assert "encoding" in params["properties"]
        # encoding 标记了 required=False，不在 required 中
        assert "encoding" not in params.get("required", [])

    def test_tool_execute(self, sample_tool):
        """执行工具"""
        result = sample_tool.execute(text="hello")
        assert result == "ECHO: hello"

    def test_tool_execute_error(self):
        """工具执行失败应返回错误信息"""
        tool = Tool(
            name="fail",
            description="会失败",
            inputs={},
            forward=lambda: (_ for _ in ()).throw(Exception("崩溃")),
        )
        result = tool.execute()
        assert "Error" in result or "崩溃" in result

    def test_create_tool_factory(self):
        """create_tool 工厂函数"""
        tool = create_tool(
            "add",
            "加法",
            {"a": {"type": "integer", "description": "第一个数"}, "b": {"type": "integer", "description": "第二个数"}},
            lambda a, b: str(int(a) + int(b)),
        )
        assert isinstance(tool, Tool)
        assert tool.execute(a="1", b="2") == "3"


class TestCodeAgentTool:
    """测试 CodeAgentTool"""

    def test_safe_code_execution(self):
        """安全代码执行"""
        tool = CodeAgentTool(allow_dangerous=False)
        result = tool._execute_code("print('hello')")
        assert "hello" in result

    def test_dangerous_code_blocked(self):
        """危险代码应被拦截"""
        tool = CodeAgentTool(allow_dangerous=False)
        result = tool._execute_code("import os; os.system('ls')")
        assert "拒绝执行" in result

    def test_dangerous_code_allowed(self):
        """允许危险模式"""
        tool = CodeAgentTool(allow_dangerous=True)
        result = tool._execute_code("print('safe')")
        assert "safe" in result

    def test_timeout(self):
        """超时检测"""
        tool = CodeAgentTool(timeout=1)
        result = tool._execute_code("import time; time.sleep(10)")
        assert "超时" in result

    def test_syntax_error(self):
        """语法错误"""
        tool = CodeAgentTool()
        result = tool._execute_code("this is not valid python")
        assert "exit code" in result or "SyntaxError" in result


# =============================================================================
# BaseAgent 测试
# =============================================================================

class TestBaseAgent:
    """测试 BaseAgent"""

    def test_agent_initialization(self, mock_llm_client):
        """Agent 初始化"""
        class InitAgent(BaseAgent):
            def _build_system_prompt(self): return "test"
            def _parse_output(self, raw): return {}

        agent = InitAgent(
            role="测试员",
            goal="测试",
            llm_client=mock_llm_client,
        )
        assert agent.role == "测试员"
        assert agent.goal == "测试"
        assert agent._iteration == 0

    def test_agent_run_with_llm(self, mock_llm_client):
        """Agent 运行（含模拟 LLM）"""
        class SimpleAgent(BaseAgent):
            def _build_system_prompt(self):
                return "You are a test agent."
            def _parse_output(self, raw_response):
                return json.loads(raw_response)

        agent = SimpleAgent(
            role="测试员",
            goal="解析 JSON",
            llm_client=mock_llm_client,
        )
        result = agent.run("返回 {'status': 'ok'}")

        assert result["success"] is True
        assert result["agent"] == "SimpleAgent"

    def test_agent_run_with_tool(self, mock_llm_client, sample_tool):
        """Agent 使用工具"""
        # 模拟 LLM 返回工具调用
        mock_llm_client.chat.side_effect = [
            {"content": json.dumps({"name": "echo", "arguments": {"text": "hello"}})},
            {"content": json.dumps({"result": "done"})},
        ]

        class ToolAgent(BaseAgent):
            def _build_system_prompt(self):
                return "Use echo tool."
            def _parse_output(self, raw_response):
                return json.loads(raw_response)

        agent = ToolAgent(
            role="工具测试",
            goal="使用工具",
            tools=[sample_tool],
            llm_client=mock_llm_client,
        )
        result = agent.run("test")

        assert result["success"] is True
        assert mock_llm_client.chat.call_count >= 1

    def test_agent_find_tool(self, mock_llm_client, sample_tool):
        """查找工具"""
        class FindToolAgent(BaseAgent):
            def _build_system_prompt(self): return "test"
            def _parse_output(self, raw): return {}

        agent = FindToolAgent(
            role="测试",
            goal="测试",
            tools=[sample_tool],
            llm_client=mock_llm_client,
        )
        found = agent._find_tool("echo")
        assert found is not None
        assert found.name == "echo"

        not_found = agent._find_tool("nonexistent")
        assert not_found is None


# =============================================================================
# 各 Agent 子类测试
# =============================================================================

class TestResearchAgent:
    """测试 ResearchAgent"""

    def test_instantiation(self, mock_llm_client):
        """实例化"""
        agent = ResearchAgent(llm_client=mock_llm_client)
        assert agent.role == "代码研究员"
        assert len(agent.tools) >= 2  # search_code + read_file + list_files

    def test_build_system_prompt(self, mock_llm_client):
        """system prompt 构建"""
        agent = ResearchAgent(llm_client=mock_llm_client)
        prompt = agent._build_system_prompt()
        assert "代码研究员" in prompt
        assert "search_code" in prompt

    def test_run_with_mock(self, mock_llm_client, temp_project):
        """模拟执行"""
        mock_llm_client.chat.return_value = {
            "content": '```json\n{"summary": "测试报告", "related_files": [], "modification_targets": []}\n```',
        }
        agent = ResearchAgent(llm_client=mock_llm_client)
        result = agent.run("测试需求")
        assert result["success"] is True


class TestPlanAgent:
    """测试 PlanAgent"""

    def test_instantiation(self, mock_llm_client):
        agent = PlanAgent(llm_client=mock_llm_client)
        assert agent.role == "技术规划师"

    def test_run_with_mock(self, mock_llm_client):
        mock_llm_client.chat.return_value = {
            "content": '```json\n{"plan_summary": "测试计划", "tasks": [], "execution_order": []}\n```',
        }
        agent = PlanAgent(llm_client=mock_llm_client)
        result = agent.run("规划任务")
        assert result["success"] is True


class TestCodeAgent:
    """测试 CodeAgent"""

    def test_instantiation(self, mock_llm_client):
        agent = CodeAgent(llm_client=mock_llm_client)
        assert agent.role == "高级编码员"
        assert agent.allow_code_execution is True

    def test_run_with_mock(self, mock_llm_client):
        mock_llm_client.chat.return_value = {
            "content": '```json\n{"task_id": "task-001", "changes": [], "verification": "ok"}\n```',
        }
        agent = CodeAgent(llm_client=mock_llm_client)
        result = agent.run("生成代码")
        assert result["success"] is True


class TestReviewAgent:
    """测试 ReviewAgent"""

    def test_instantiation(self, mock_llm_client):
        agent = ReviewAgent(llm_client=mock_llm_client)
        assert agent.role == "代码审查员"

    def test_parse_pass(self, mock_llm_client):
        """解析通过结果"""
        agent = ReviewAgent(llm_client=mock_llm_client)
        result = agent._parse_output('```json\n{"passed": true, "score": 95, "issues": []}\n```')
        assert result["passed"] is True
        assert result["score"] == 95

    def test_parse_fail(self, mock_llm_client):
        """解析未通过结果"""
        agent = ReviewAgent(llm_client=mock_llm_client)
        result = agent._parse_output(
            '```json\n{"passed": false, "score": 40, "issues": [{"severity": "critical", "description": "bug"}], "retry_suggestion": "fix it"}\n```'
        )
        assert result["passed"] is False
        assert len(result["issues"]) == 1


class TestGitAgent:
    """测试 GitAgent"""

    def test_instantiation(self, mock_llm_client):
        agent = GitAgent(llm_client=mock_llm_client)
        assert agent.role == "Git 操作员"

    def test_run_with_mock(self, mock_llm_client):
        mock_llm_client.chat.return_value = {
            "content": '```json\n{"branch": "fix/test", "commit_message": "fix: test", "mr_title": "Fix Test"}\n```',
        }
        agent = GitAgent(llm_client=mock_llm_client)
        result = agent.run("提交代码")
        assert result["success"] is True


# =============================================================================
# AgentPipeline 测试
# =============================================================================

class TestAgentPipeline:
    """测试 AgentPipeline"""

    @pytest.fixture
    def pipeline(self, mock_llm_client):
        """创建并组装流水线"""
        config = MagicMock()
        config.project_root = Path("/tmp/test_project")
        
        pipeline = AgentPipeline(config=config, llm_client=mock_llm_client)
        pipeline.assemble()
        return pipeline, mock_llm_client

    def test_assemble(self, pipeline):
        """组装流水线"""
        p, _ = pipeline
        assert p.research_agent is not None
        assert p.plan_agent is not None
        assert p.code_agent is not None
        assert p.review_agent is not None
        assert p.git_agent is not None

    def test_run_without_assemble_raises(self, mock_llm_client):
        """未组装时运行应报错"""
        config = MagicMock()
        p = AgentPipeline(config=config, llm_client=mock_llm_client)
        with pytest.raises(RuntimeError, match="未组装"):
            p.run("test")

    def test_run_full_pipeline(self, pipeline):
        """完整流水线执行（模拟）"""
        p, mock_llm = pipeline

        # 模拟每个阶段的 LLM 响应
        mock_llm.chat.side_effect = [
            # 研究
            {"content": '```json\n{"summary": "报告", "related_files": [], "modification_targets": []}\n```'},
            # 规划
            {"content": '```json\n{"plan_summary": "计划", "tasks": [], "execution_order": []}\n```'},
            # 编码
            {"content": '```json\n{"task_id": "t1", "changes": [], "verification": "ok"}\n```'},
            # 审查（通过）
            {"content": '```json\n{"passed": true, "score": 90, "issues": []}\n```'},
            # Git
            {"content": '```json\n{"branch": "fix/x", "commit_message": "fix: x"}\n```'},
        ]

        result = p.run("测试需求")

        assert result["success"] is True
        assert "research" in result["stages"]
        assert "plan" in result["stages"]
        assert "code" in result["stages"]
        assert "review" in result["stages"]
        assert "git" in result["stages"]
        assert "流水线执行完成" in result["pipeline_summary"]

    def test_run_retry_on_review_fail(self, pipeline):
        """审查不通过时重试"""
        p, mock_llm = pipeline

        mock_llm.chat.side_effect = [
            # 研究
            {"content": '```json\n{"summary": "ok"}\n```'},
            # 规划
            {"content": '```json\n{"plan_summary": "ok"}\n```'},
            # 编码 (第1次)
            {"content": '```json\n{"task_id": "t1"}\n```'},
            # 审查 (不通过)
            {"content": '```json\n{"passed": false, "issues": [{"severity": "critical"}]}\n```'},
            # 编码 (第2次)
            {"content": '```json\n{"task_id": "t1", "changes": []}\n```'},
            # 审查 (通过)
            {"content": '```json\n{"passed": true, "issues": []}\n```'},
            # Git
            {"content": '```json\n{"branch": "fix/x"}\n```'},
        ]

        result = p.run("测试需求", max_retries=3)

        assert result["success"] is True
        # 至少调用了 7 次（研究+规划+编码+审查+编码+审查+Git）
        assert mock_llm.chat.call_count >= 7


# =============================================================================
# 工具函数测试
# =============================================================================

class TestToolFunctions:
    """测试工具函数实现"""

    def test_read_file(self, temp_project):
        """读取文件"""
        result = _read_file(str(temp_project / "src" / "main.py"))
        assert "def hello()" in result

    def test_read_file_nonexistent(self):
        """读取不存在的文件"""
        result = _read_file("/nonexistent/file.py")
        assert "不存在" in result

    def test_search_code(self, temp_project):
        """搜索代码"""
        result = _search_code("def hello", str(temp_project))
        assert "main.py" in result

    def test_list_files(self, temp_project):
        """列出文件"""
        result = _list_files(str(temp_project / "src"), "*.py")
        assert "main.py" in result
        assert "utils.py" in result

    def test_get_context(self, temp_project):
        """获取上下文"""
        result = _get_context(str(temp_project / "src" / "main.py"), line=1, n=5)
        assert "def hello" in result
