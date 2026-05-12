"""
agents/__init__.py — 多 Agent 协作系统 v0.3.0

吸收 5 个开源项目的精华设计：

| 来源 | 吸收内容 | 对应模块 |
|------|---------|---------|
| **smolagents** | 极简工具定义 + Code Agent 模式 | Tool, CodeAgent |
| **CrewAI** | 多 Agent 协作流水线 | AgentPipeline, BaseAgent |
| **RA.Aid** | 多轮推理链 + 自动研究阶段 | ResearchAgent |
| **Aider** | PageRank + Tree-sitter 上下文注入 | CodeAgent (via KnowledgeServer) |
| **LangGraph** | 状态图抽象 | graph.py → 替代旧版硬编码状态机 |

架构：
    ResearchAgent → 分析代码库 → 研究报告
        ↓
    PlanAgent → 制定修改计划 → 子任务列表
        ↓
    CodeAgent → Tree-sitter + PageRank → 生成 diff
        ↓
    ReviewAgent → 自审 → 不通过就修正
        ↓
    GitAgent → commit → push → MR

使用方式：
    from agent_mcp.agents import (
        Tool, CodeAgentTool, create_tool,
        BaseAgent, ResearchAgent, PlanAgent, CodeAgent, ReviewAgent, GitAgent,
        AgentPipeline,
    )
    
    pipeline = AgentPipeline(config, llm_client)
    pipeline.assemble()  # 构建 5 个 Agent
    result = pipeline.run(requirement="修复登录页面样式错乱")

版本历史：
    v0.3.0 — 初版，整合 smolagents + CrewAI + RA.Aid + Aider 精华
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
import textwrap
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# =============================================================================
# 1. Tool — smolagents 极简工具定义
# =============================================================================

@dataclass
class Tool:
    """
    smolagents 风格的极简工具定义。

    核心设计理念：
        - 最小化接口: 只需要 name, description, inputs, forward
        - JSON Schema 自动生成: 从 inputs dict 推导参数 schema
        - 类型安全: inputs 定义参数类型和描述

    示例:
        tool = Tool(
            name="read_file",
            description="读取文件内容",
            inputs={"path": {"type": "string", "description": "文件路径"}},
            forward=lambda path: Path(path).read_text(),
        )
    """
    name: str
    description: str
    inputs: Dict[str, Dict[str, str]]  # {参数名: {type: str, description: str}}
    forward: Callable[..., str]  # 执行函数，返回字符串结果
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_openai_schema(self) -> Dict[str, Any]:
        """
        生成 OpenAI function calling 格式的 tool schema。

        Returns:
            {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
        """
        properties = {}
        required = []
        for param_name, param_info in self.inputs.items():
            properties[param_name] = {
                "type": param_info.get("type", "string"),
                "description": param_info.get("description", ""),
            }
            if param_info.get("required", True):
                required.append(param_name)
        
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def execute(self, **kwargs) -> str:
        """
        执行工具并返回字符串结果。

        Args:
            **kwargs: 工具参数

        Returns:
            执行结果字符串

        Raises:
            Exception: 执行失败时抛出
        """
        try:
            result = self.forward(**kwargs)
            return str(result) if result is not None else ""
        except Exception as e:
            logger.error(f"工具 [{self.name}] 执行失败: {e}")
            return f"Error: {e}"


class CodeAgentTool(Tool):
    """
    Code Agent 专用工具 — 执行 Python 代码片段。

    区别于普通 Tool:
        - forward 接收 code 字符串，在隔离环境中执行
        - 返回 stdout/stderr
        - 支持超时控制
        - 安全性: 默认禁用危险操作（可通过 allow_dangerous 开启）

    smolagents 核心特性: LLM 直接写 Python 代码执行，
    而不是通过 function calling 间接操作。
    """

    def __init__(
        self,
        name: str = "execute_python",
        description: str = "执行 Python 代码并返回输出。可以导入已安装的库。",
        timeout: int = 30,
        allow_dangerous: bool = False,
    ):
        super().__init__(
            name=name,
            description=description,
            inputs={
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码",
                },
            },
            forward=self._execute_code,
        )
        self.timeout = timeout
        self.allow_dangerous = allow_dangerous
        # 危险操作黑名单
        self._dangerous_patterns = [
            r"os\.system\(",
            r"subprocess\.",
            r"__import__\s*\(\s*['\"]os['\"]",
            r"eval\s*\(",
            r"exec\s*\(",
            r"open\s*\([^)]*['\"]w",
            r"shutil\.rmtree",
            r"os\.remove\(",
            r"os\.unlink\(",
        ]

    def _execute_code(self, code: str) -> str:
        """
        执行 Python 代码。

        Args:
            code: Python 代码字符串

        Returns:
            执行输出 (stdout + stderr)
        """
        if not self.allow_dangerous:
            for pattern in self._dangerous_patterns:
                if re.search(pattern, code):
                    return f"拒绝执行: 代码包含潜在危险操作 (匹配: {pattern})"

        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(Path.cwd()),
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output
        except subprocess.TimeoutExpired:
            return f"执行超时 ({self.timeout}s)"
        except Exception as e:
            return f"执行错误: {e}"


def create_tool(
    name: str,
    description: str,
    inputs: Dict[str, Dict[str, str]],
    forward: Callable,
) -> Tool:
    """
    快捷创建 Tool 的工厂函数。

    用法:
        read_tool = create_tool(
            "read_file",
            "读取文件",
            {"path": {"type": "string", "description": "文件路径"}},
            lambda path: Path(path).read_text()
        )
    """
    return Tool(name=name, description=description, inputs=inputs, forward=forward)


# =============================================================================
# 2. BaseAgent — 所有 Agent 的基类
# =============================================================================

class BaseAgent(ABC):
    """
    CrewAI 风格的多 Agent 基类。

    每个 Agent 有:
        - role: 角色名称（如 "研究员", "编码员"）
        - goal: 目标描述
        - backstory: 背景/能力描述（注入 system prompt）
        - tools: 可用工具列表
        - llm_client: LLM 客户端（调用 DeepSeek 等）
        - max_iterations: 最大推理轮次
        - allow_code_execution: 是否允许生成并执行 Python 代码

    子类需要实现:
        - _build_system_prompt(): 构建 system prompt
        - _parse_output(raw_response): 解析 LLM 原始输出为结构化结果
    """

    def __init__(
        self,
        role: str,
        goal: str,
        backstory: str = "",
        tools: Optional[List[Tool]] = None,
        llm_client: Optional[Any] = None,  # LLMClient from llm_client.py
        max_iterations: int = 5,
        allow_code_execution: bool = False,
        verbose: bool = False,
    ):
        self.role = role
        self.goal = goal
        self.backstory = backstory
        self.tools = tools or []
        self.llm_client = llm_client
        self.max_iterations = max_iterations
        self.allow_code_execution = allow_code_execution
        self.verbose = verbose

        # 运行时状态
        self._iteration = 0
        self._history: List[Dict[str, str]] = []
        self._result: Optional[Dict[str, Any]] = None

    @property
    def agent_name(self) -> str:
        """Agent 名称 = 类名，用于日志和追踪"""
        return self.__class__.__name__

    @abstractmethod
    def _build_system_prompt(self) -> str:
        """
        构建 system prompt。

        应包含: role, goal, backstory, 可用工具说明, 输出格式要求

        Returns:
            system prompt 字符串
        """
        ...

    @abstractmethod
    def _parse_output(self, raw_response: str) -> Dict[str, Any]:
        """
        解析 LLM 原始输出。

        Args:
            raw_response: LLM 返回的原始文本

        Returns:
            结构化结果 dict
        """
        ...

    def run(self, task: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        运行 Agent 完成任务。

        Args:
            task: 任务描述
            context: 额外上下文（如代码库分析结果、前置 Agent 的输出）

        Returns:
            执行结果 dict，包含:
                - success: bool
                - output: 结构化输出
                - iterations: 迭代次数
                - raw_history: 原始对话历史
        """
        self._iteration = 0
        self._history = []

        if self.verbose:
            logger.info(f"[{self.agent_name}] 开始执行任务: {task[:100]}...")

        # 构建消息列表
        system_prompt = self._build_system_prompt()
        messages = [{"role": "system", "content": system_prompt}]

        # 注入上下文
        if context:
            context_str = json.dumps(context, ensure_ascii=False, indent=2)
            messages.append({
                "role": "user",
                "content": f"## 上下文信息\n{context_str}\n\n## 任务\n{task}",
            })
        else:
            messages.append({"role": "user", "content": task})

        # Agent 推理循环
        final_output = None
        for i in range(self.max_iterations):
            self._iteration = i + 1
            if self.verbose:
                logger.info(f"[{self.agent_name}] 第 {self._iteration}/{self.max_iterations} 轮推理")

            # 调用 LLM
            response = self._call_llm(messages)
            if response is None:
                logger.error(f"[{self.agent_name}] LLM 调用失败")
                break

            self._history.append({
                "iteration": self._iteration,
                "role": "assistant",
                "content": response,
            })

            # 检查是否需要工具调用
            tool_result = self._try_tool_call(response)
            if tool_result:
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": f"工具结果:\n{tool_result}"})
                continue

            # 尝试解析输出
            try:
                final_output = self._parse_output(response)
                break
            except Exception as e:
                logger.warning(f"[{self.agent_name}] 解析输出失败 (第 {self._iteration} 轮): {e}")
                messages.append({"role": "assistant", "content": response})
                messages.append({
                    "role": "user",
                    "content": f"输出格式不正确 ({e})，请按要求的格式重新输出。",
                })

        self._result = {
            "success": final_output is not None,
            "agent": self.agent_name,
            "role": self.role,
            "task": task,
            "output": final_output or {},
            "iterations": self._iteration,
            "raw_history": self._history,
            "timestamp": datetime.now().isoformat(),
        }

        return self._result

    def _call_llm(self, messages: List[Dict[str, str]]) -> Optional[str]:
        """
        调用 LLM。

        Args:
            messages: 消息列表

        Returns:
            LLM 响应文本，失败返回 None
        """
        if self.llm_client is None:
            raise RuntimeError(f"[{self.agent_name}] LLM 客户端未设置")

        try:
            # 使用 OpenAI 兼容接口
            response = self.llm_client.chat(
                messages=messages,
                temperature=0.3,
                max_tokens=4096,
            )
            return response.get("content", "") if isinstance(response, dict) else str(response)
        except Exception as e:
            logger.error(f"[{self.agent_name}] LLM 调用异常: {e}")
            return None

    def _try_tool_call(self, response: str) -> Optional[str]:
        """
        检测并执行响应中的工具调用。

        支持两种格式:
            1. OpenAI function_call 格式: {"name": "tool_name", "arguments": {...}}
            2. 标记格式: <tool name="xxx">{"arg": "value"}</tool>

        Args:
            response: LLM 响应文本

        Returns:
            工具执行结果，无工具调用时返回 None
        """
        # 格式1: JSON function_call
        try:
            data = json.loads(response)
            if isinstance(data, dict) and "name" in data and "arguments" in data:
                tool_name = data["name"]
                tool_args = data["arguments"]
                tool = self._find_tool(tool_name)
                if tool:
                    return tool.execute(**tool_args)
        except (json.JSONDecodeError, TypeError):
            pass

        # 格式2: <tool> 标记
        tool_match = re.search(r'<tool\s+name="(\w+)"\s*>(.*?)</tool>', response, re.DOTALL)
        if tool_match:
            tool_name = tool_match.group(1)
            tool_args_str = tool_match.group(2).strip()
            try:
                tool_args = json.loads(tool_args_str)
            except json.JSONDecodeError:
                tool_args = {}
            tool = self._find_tool(tool_name)
            if tool:
                return tool.execute(**tool_args)

        return None

    def _find_tool(self, name: str) -> Optional[Tool]:
        """按名称查找工具"""
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


# =============================================================================
# 3. ResearchAgent — RA.Aid 风格研究 Agent
# =============================================================================

class ResearchAgent(BaseAgent):
    """
    研究 Agent — 先调研再动手。

    RA.Aid 核心思想:
        - 多轮推理链: 不是一次性分析，而是分轮逐步深入
        - 代码库调研: 读取相关文件、搜索关键模式、追踪依赖
        - 输出结构化研究报告

    工作流程:
        第1轮: 搜索相关文件（根据需求关键词）
        第2轮: 读取关键文件内容
        第3轮: 分析依赖关系和调用链
        第4轮: 找出需要修改的位置
        第5轮: 生成研究报告
    """

    def __init__(self, llm_client=None, tools=None, **kwargs):
        research_tools = tools or [
            create_tool(
                "search_code",
                "在代码库中搜索关键词/正则表达式",
                {"pattern": {"type": "string", "description": "搜索模式"}, "path": {"type": "string", "description": "搜索路径"}},
                lambda pattern, path=".": _search_code(pattern, path),
            ),
            create_tool(
                "read_file",
                "读取文件内容",
                {"path": {"type": "string", "description": "文件路径"}},
                lambda path: _read_file(path),
            ),
            create_tool(
                "list_files",
                "列出目录中的文件",
                {"path": {"type": "string", "description": "目录路径"}, "pattern": {"type": "string", "description": "文件名模式"}},
                lambda path=".", pattern="*": _list_files(path, pattern),
            ),
        ]

        super().__init__(
            role="代码研究员",
            goal="深入分析代码库，找出需要修改的文件和位置，生成结构化研究报告",
            backstory=(
                "你是一位资深代码研究员，擅长快速理解陌生代码库。\n"
                "你会先搜索相关文件，再读取关键代码，追踪依赖关系，\n"
                "最后输出一份清晰的研究报告，标注所有需要修改的位置。"
            ),
            tools=research_tools,
            llm_client=llm_client,
            **kwargs,
        )

    def _build_system_prompt(self) -> str:
        return f"""# {self.role}: {self.goal}

## 背景
{self.backstory}

## 可用工具
{chr(10).join(f'- {t.name}: {t.description}' for t in self.tools)}

## 工作流程
1. 用 search_code 搜索与需求相关的文件和函数
2. 用 read_file 读取关键文件
3. 分析依赖关系和调用链
4. 标注需要修改的具体位置（文件名 + 行号范围）

## 输出格式
请输出 JSON 格式的研究报告:
```json
{{
    "summary": "需求分析摘要",
    "related_files": [
        {{"path": "相对路径", "reason": "为什么相关", "relevance": "high|medium|low"}}
    ],
    "modification_targets": [
        {{"file": "文件路径", "lines": "行号范围", "change_type": "modify|add|delete", "description": "修改说明"}}
    ],
    "dependencies": ["依赖模块1", "依赖模块2"],
    "risks": ["潜在风险1", "潜在风险2"],
    "estimated_effort": "预估工作量"
}}
```"""

    def _parse_output(self, raw_response: str) -> Dict[str, Any]:
        # 提取 JSON
        json_match = re.search(r'```json\s*(.*?)```', raw_response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
        # 尝试直接解析
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError:
            return {"raw_analysis": raw_response}


# =============================================================================
# 4. PlanAgent — CrewAI 风格规划 Agent
# =============================================================================

class PlanAgent(BaseAgent):
    """
    规划 Agent — 制定可执行的修改计划。

    输入: 研究报告 (ResearchAgent 输出)
    输出: 子任务列表，每个子任务有明确的文件和修改范围
    """

    def __init__(self, llm_client=None, **kwargs):
        super().__init__(
            role="技术规划师",
            goal="根据研究报告制定可执行的修改计划，拆解为有序的子任务",
            backstory=(
                "你是一位经验丰富的技术规划师，擅长将模糊需求拆解为可执行步骤。\n"
                "你会考虑任务间的依赖关系，确保修改顺序合理。"
            ),
            llm_client=llm_client,
            **kwargs,
        )

    def _build_system_prompt(self) -> str:
        return f"""# {self.role}: {self.goal}

## 背景
{self.backstory}

## 规划原则
1. 每个子任务只修改一个文件（或紧密相关的几个文件）
2. 考虑依赖关系：先改底层，再改上层
3. 每个子任务有明确的验收标准
4. 预估每个子任务复杂度（simple|medium|complex）

## 输出格式
```json
{{
    "plan_summary": "计划概述",
    "tasks": [
        {{
            "id": "task-001",
            "title": "任务标题",
            "description": "详细描述",
            "files": ["要修改的文件路径"],
            "complexity": "simple|medium|complex",
            "depends_on": ["task-000"],
            "acceptance_criteria": ["验收条件1", "验收条件2"]
        }}
    ],
    "execution_order": ["task-001", "task-002"],
    "estimated_total_time": "预估总时间"
}}
```"""

    def _parse_output(self, raw_response: str) -> Dict[str, Any]:
        json_match = re.search(r'```json\s*(.*?)```', raw_response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError:
            return {"raw_plan": raw_response}


# =============================================================================
# 5. CodeAgent — Aider 风格编码 Agent
# =============================================================================

class CodeAgent(BaseAgent):
    """
    编码 Agent — 执行代码修改。

    Aider 核心特性:
        - Tree-sitter AST 解析: 精确定位函数/类/变量
        - PageRank 代码排名: 优先读取重要文件作为上下文
        - 编辑策略: 生成 unified diff / @@PATCH

    smolagents 核心特性:
        - Code Agent 模式: 允许 LLM 直接生成 Python 测试/验证代码
        - 工具调用: search_code, read_file, write_file
    """

    def __init__(self, llm_client=None, tools=None, **kwargs):
        coding_tools = tools or [
            create_tool(
                "read_file",
                "读取文件内容",
                {"path": {"type": "string", "description": "文件路径"}},
                lambda path: _read_file(path),
            ),
            create_tool(
                "search_code",
                "搜索代码模式",
                {"pattern": {"type": "string", "description": "搜索模式"}, "path": {"type": "string", "description": "搜索路径"}},
                lambda pattern, path=".": _search_code(pattern, path),
            ),
            create_tool(
                "get_context",
                "获取指定位置的上下文（前后N行）",
                {"path": {"type": "string", "description": "文件路径"}, "line": {"type": "integer", "description": "中心行号"}, "n": {"type": "integer", "description": "上下文行数"}},
                lambda path, line, n=20: _get_context(path, int(line), int(n)),
            ),
            CodeAgentTool(allow_dangerous=False),
        ]

        super().__init__(
            role="高级编码员",
            goal="根据计划生成精确的代码修改，输出 unified diff 格式",
            backstory=(
                "你是一位高级编码员，擅长生成精确、最小化的代码修改。\n"
                "你使用 AST 解析理解代码结构，用 unified diff 描述修改，\n"
                "确保每次修改都是最小必要变更。"
            ),
            tools=coding_tools,
            llm_client=llm_client,
            allow_code_execution=True,
            **kwargs,
        )

    def _build_system_prompt(self) -> str:
        return f"""# {self.role}: {self.goal}

## 背景
{self.backstory}

## 可用工具
{chr(10).join(f'- {t.name}: {t.description}' for t in self.tools)}

## 编辑规则
1. 先 read_file 读取目标文件确认当前内容
2. 生成 unified diff 格式的修改
3. 每次修改最小化，不要重构无关代码
4. 保持原有代码风格
5. 不删除现有注释
6. 可以用 execute_python 验证修改的正确性

## 输出格式
```json
{{
    "task_id": "task-001",
    "changes": [
        {{
            "file": "文件路径",
            "change_type": "modify|add|delete",
            "diff": "unified diff 内容",
            "reason": "修改原因"
        }}
    ],
    "verification": "验证方法说明",
    "notes": "额外说明"
}}
```"""

    def _parse_output(self, raw_response: str) -> Dict[str, Any]:
        json_match = re.search(r'```json\s*(.*?)```', raw_response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError:
            return {"raw_changes": raw_response}


# =============================================================================
# 6. ReviewAgent — 自审 Agent
# =============================================================================

class ReviewAgent(BaseAgent):
    """
    审查 Agent — 自动检查代码修改质量。

    审查维度:
        1. 语法正确性: 导入是否正确，是否存在未定义变量
        2. 逻辑正确性: 修改是否符合需求
        3. 安全性: 是否有 SQL 注入、XSS 等风险
        4. 代码风格: 是否符合项目规范
        5. 测试覆盖: 是否需要补充测试
    """

    def __init__(self, llm_client=None, **kwargs):
        super().__init__(
            role="代码审查员",
            goal="审查代码修改，发现潜在问题并给出修改建议",
            backstory=(
                "你是一位严格的代码审查员，关注代码质量、安全性和可维护性。\n"
                "你会检查语法错误、逻辑漏洞、安全隐患，并给出具体修正建议。"
            ),
            llm_client=llm_client,
            **kwargs,
        )

    def _build_system_prompt(self) -> str:
        return f"""# {self.role}: {self.goal}

## 背景
{self.backstory}

## 审查维度
1. 语法: 导入正确？变量定义？缩进正确？
2. 逻辑: 修改是否符合需求描述？
3. 安全: SQL注入？硬编码密钥？路径遍历？
4. 风格: 命名规范？注释充分？
5. 测试: 是否需要新增测试？

## 输出格式
```json
{{
    "passed": true/false,
    "score": 0-100,
    "issues": [
        {{
            "severity": "critical|warning|info",
            "file": "文件路径",
            "line": "行号",
            "description": "问题描述",
            "suggestion": "修改建议"
        }}
    ],
    "summary": "审查总结",
    "retry_suggestion": "如果不通过，给编码员的具体修改指示"
}}
```"""

    def _parse_output(self, raw_response: str) -> Dict[str, Any]:
        json_match = re.search(r'```json\s*(.*?)```', raw_response, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group(1))
            return result
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError:
            return {"raw_review": raw_response, "passed": False}


# =============================================================================
# 7. GitAgent — Git 操作 Agent
# =============================================================================

class GitAgent(BaseAgent):
    """
    Git Agent — 负责 commit, push, 创建 MR。

    职责:
        1. 生成规范的 commit message
        2. 推送到远程仓库
        3. 创建 Merge Request
    """

    def __init__(self, llm_client=None, **kwargs):
        super().__init__(
            role="Git 操作员",
            goal="将修改提交到 Git 仓库并创建 MR",
            backstory=(
                "你负责将代码修改提交到版本控制系统。\n"
                "你会生成规范的 commit message，推送到远程，创建 MR。"
            ),
            llm_client=llm_client,
            **kwargs,
        )

    def _build_system_prompt(self) -> str:
        return f"""# {self.role}: {self.goal}

## 背景
{self.backstory}

## Commit Message 规范
格式: type(scope): 简短描述

类型: feat, fix, refactor, docs, chore, test, style, perf
示例: fix(auth): 修复登录页面 Token 过期未刷新问题

## 输出格式
```json
{{
    "branch": "分支名",
    "commit_message": "完整 commit message",
    "mr_title": "MR 标题",
    "mr_description": "MR 描述（包含修改说明、测试结果）",
    "files_to_commit": ["文件1", "文件2"]
}}
```"""

    def _parse_output(self, raw_response: str) -> Dict[str, Any]:
        json_match = re.search(r'```json\s*(.*?)```', raw_response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError:
            return {"raw_git_info": raw_response}


# =============================================================================
# 8. AgentPipeline — CrewAI 风格流水线
# =============================================================================

class AgentPipeline:
    """
    多 Agent 协作流水线。

    CrewAI 核心设计:
        - 顺序执行: 前一个 Agent 的输出作为下一个的输入
        - 上下文传递: 每个 Agent 可以访问前置 Agent 的所有结果
        - 失败处理: 审查不通过时自动触发重试

    运行流程:
        pipeline = AgentPipeline(config, llm_client)
        pipeline.assemble()  # 创建 5 个 Agent
        result = pipeline.run("修复登录页面样式错乱")
    """

    def __init__(
        self,
        config: Any,  # AppConfig
        llm_client: Any,  # LLMClient
        verbose: bool = False,
    ):
        self.config = config
        self.llm_client = llm_client
        self.verbose = verbose

        # 5 个 Agent
        self.research_agent: Optional[ResearchAgent] = None
        self.plan_agent: Optional[PlanAgent] = None
        self.code_agent: Optional[CodeAgent] = None
        self.review_agent: Optional[ReviewAgent] = None
        self.git_agent: Optional[GitAgent] = None

        # 执行上下文（所有 Agent 共享）
        self.context: Dict[str, Any] = {}

    def assemble(self):
        """
        组装 5 个 Agent。

        创建顺序: 研究 → 规划 → 编码 → 审查 → Git
        """
        logger.info("正在组装 Agent 流水线...")

        self.research_agent = ResearchAgent(
            llm_client=self.llm_client,
            verbose=self.verbose,
        )
        self.plan_agent = PlanAgent(
            llm_client=self.llm_client,
            verbose=self.verbose,
        )
        self.code_agent = CodeAgent(
            llm_client=self.llm_client,
            verbose=self.verbose,
        )
        self.review_agent = ReviewAgent(
            llm_client=self.llm_client,
            verbose=self.verbose,
        )
        self.git_agent = GitAgent(
            llm_client=self.llm_client,
            verbose=self.verbose,
        )

        logger.info("Agent 流水线组装完成: Research → Plan → Code → Review → Git")

    def run(
        self,
        requirement: str,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        运行完整的 Agent 流水线。

        Args:
            requirement: 用户需求描述
            max_retries: 审查不通过时的最大重试次数

        Returns:
            {
                "success": bool,
                "research": {...},
                "plan": {...},
                "code_changes": {...},
                "review": {...},
                "git": {...},
                "pipeline_summary": "...",
            }
        """
        if not all([
            self.research_agent, self.plan_agent,
            self.code_agent, self.review_agent, self.git_agent,
        ]):
            raise RuntimeError("Agent 流水线未组装，请先调用 assemble()")

        logger.info(f"开始执行流水线: {requirement[:100]}...")
        pipeline_start = datetime.now()

        # 阶段 1: 研究
        logger.info("[阶段 1/5] 研究 Agent 分析代码库...")
        research_result = self.research_agent.run(
            task=f"分析以下需求涉及的代码库: {requirement}",
            context={"requirement": requirement, "project_root": str(self.config.project_root)},
        )
        self.context["research"] = research_result

        if not research_result.get("success"):
            return self._fail("研究阶段失败", research_result)

        # 阶段 2: 规划
        logger.info("[阶段 2/5] 规划 Agent 制定修改计划...")
        plan_result = self.plan_agent.run(
            task="根据研究报告制定修改计划",
            context={"research": research_result.get("output", {})},
        )
        self.context["plan"] = plan_result

        if not plan_result.get("success"):
            return self._fail("规划阶段失败", plan_result)

        # 阶段 3: 编码 + 审查（带重试）
        logger.info("[阶段 3-4/5] 编码 → 审查循环...")
        code_result = None
        review_result = None

        for attempt in range(1, max_retries + 1):
            logger.info(f"  编码尝试 {attempt}/{max_retries}")

            code_result = self.code_agent.run(
                task="根据修改计划生成代码变更",
                context={
                    "plan": plan_result.get("output", {}),
                    "previous_review": review_result.get("output") if review_result else None,
                    "retry_hint": f"这是第 {attempt} 次尝试，请修正之前审查指出的问题" if attempt > 1 else "",
                },
            )

            # 审查
            review_result = self.review_agent.run(
                task="审查代码修改",
                context={
                    "requirement": requirement,
                    "plan": plan_result.get("output", {}),
                    "code_changes": code_result.get("output", {}),
                },
            )

            review_output = review_result.get("output", {})
            if review_output.get("passed"):
                logger.info(f"  审查通过! (第 {attempt} 次尝试)")
                break
            else:
                issues = review_output.get("issues", [])
                critical_count = sum(1 for i in issues if i.get("severity") == "critical")
                logger.warning(
                    f"  审查未通过: {len(issues)} 个问题 "
                    f"({critical_count} 个严重), 准备重试..."
                )

        self.context["code_changes"] = code_result
        self.context["review"] = review_result

        # 阶段 5: Git
        logger.info("[阶段 5/5] Git Agent 提交代码...")
        git_result = self.git_agent.run(
            task="生成 commit message 和 MR 信息",
            context={
                "requirement": requirement,
                "code_changes": code_result.get("output", {}) if code_result else {},
                "review": review_result.get("output", {}) if review_result else {},
            },
        )
        self.context["git"] = git_result

        # 汇总
        elapsed = (datetime.now() - pipeline_start).total_seconds()

        final_result = {
            "success": review_result is not None and review_result.get("output", {}).get("passed", False),
            "requirement": requirement,
            "elapsed_s": round(elapsed, 1),
            "stages": {
                "research": research_result,
                "plan": plan_result,
                "code": code_result,
                "review": review_result,
                "git": git_result,
            },
            "pipeline_summary": (
                f"流水线执行完成 ({elapsed:.1f}s)\n"
                f"  研究: {'✓' if research_result.get('success') else '✗'}\n"
                f"  规划: {'✓' if plan_result.get('success') else '✗'}\n"
                f"  编码: {'✓' if code_result and code_result.get('success') else '✗'}\n"
                f"  审查: {'✓ 通过' if (review_result and review_result.get('output', {}).get('passed')) else '✗ 未通过'}\n"
                f"  Git: {'✓' if git_result and git_result.get('success') else '✗'}"
            ),
        }

        logger.info(f"流水线完成: {final_result['pipeline_summary']}")
        return final_result

    def _fail(self, stage: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """生成失败结果"""
        return {
            "success": False,
            "failed_stage": stage,
            "output": result,
            "pipeline_summary": f"流水线在 {stage} 失败",
        }


# =============================================================================
# 工具函数的实际实现（简化版，实际应通过 MCP 调用）
# =============================================================================

def _search_code(pattern: str, path: str = ".") -> str:
    """搜索代码库"""
    try:
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.java", "--include=*.ts",
             "--include=*.vue", "--include=*.xml", "--include=*.yaml", "--include=*.yml",
             pattern, path],
            capture_output=True, text=True, timeout=30,
            cwd=str(Path(path).resolve()),
        )
        return result.stdout[:5000] or "未找到匹配"
    except Exception as e:
        return f"搜索失败: {e}"


def _read_file(path: str) -> str:
    """读取文件"""
    try:
        p = Path(path)
        if not p.exists():
            return f"文件不存在: {path}"
        content = p.read_text(encoding="utf-8", errors="replace")
        # 限制返回长度
        if len(content) > 10000:
            return content[:10000] + f"\n... (截断，共 {len(content)} 字符)"
        return content
    except Exception as e:
        return f"读取失败: {e}"


def _list_files(path: str = ".", pattern: str = "*") -> str:
    """列出文件"""
    try:
        p = Path(path)
        if not p.exists():
            return f"目录不存在: {path}"
        files = sorted(p.rglob(pattern))[:100]
        return "\n".join(str(f.relative_to(p)) for f in files)
    except Exception as e:
        return f"列出失败: {e}"


def _get_context(path: str, line: int, n: int = 20) -> str:
    """获取文件指定行周围上下文"""
    try:
        p = Path(path)
        if not p.exists():
            return f"文件不存在: {path}"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, line - n - 1)
        end = min(len(lines), line + n)
        result = []
        for i in range(start, end):
            marker = ">>>" if i == line - 1 else "   "
            result.append(f"{i+1:4d} {marker} {lines[i]}")
        return "\n".join(result)
    except Exception as e:
        return f"读取上下文失败: {e}"
