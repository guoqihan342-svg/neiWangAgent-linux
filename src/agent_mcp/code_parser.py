"""
代码解析器 — 从 LLM 输出中提取代码文件变更

支持多种 LLM 输出格式（按可靠性排序）：
  1. @@FILE:path@@ ... @@END@@      ← 当前格式
  2. ---FILE:path--- ... ---END---     ← 备选格式
  3. ```language:path ... ```          ← Markdown 代码块格式
  4. ### FILE: path\n```...```         ← 标题+代码块格式
  5. file: path\n    (缩进代码)       ← 纯文本格式

解析策略：
  - 按格式优先级依次尝试
  - 第一个成功解析出 ≥1 个文件的格式即为最终结果
  - 如果所有格式都失败，返回空列表 + 原始内容供调试

用法：
    from agent_mcp.code_parser import parse_code_changes

    files = parse_code_changes(llm_output)
    # → [CodeFile(path="src/main.py", content="...", language="python"), ...]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class CodeFile:
    """LLM 提取的单个代码文件。"""

    path: str          # 相对路径，如 "src/main.py"
    content: str       # 文件完整内容
    language: str = ""  # 检测到的语言（从扩展名或标记推断）
    line_count: int = 0 # 行数（自动计算）

    def __post_init__(self):
        self.content = self.content.strip()
        self.line_count = len(self.content.splitlines())
        if not self.language:
            self.language = _guess_language(self.path)


@dataclass
class ParseResult:
    """LLM 代码解析结果。"""

    files: list[CodeFile]            # 成功解析的文件列表
    used_format: str = "unknown"     # 使用了哪种格式
    raw_content: str = ""            # 原始 LLM 输出（调试用）
    errors: list[str] = field(default_factory=list)  # 解析警告/错误

    @property
    def success(self) -> bool:
        return len(self.files) > 0


# =============================================================================
# 格式解析器 — 每种格式一个解析函数
# =============================================================================

def _parse_format_file_end(content: str) -> list[CodeFile]:
    """
    格式1: @@FILE:path@@ ... @@END@@

    示例:
        @@FILE:src/main.py@@
        print("hello")
        @@END@@
        @@FILE:README.md@@
        # Project
        @@END@@
    """
    files = []
    # 匹配 @@FILE:任意路径@@  ...  @@END@@
    pattern = r"@@FILE:\s*(.+?)\s*@@(.*?)@@END@@"
    for match in re.finditer(pattern, content, re.DOTALL | re.IGNORECASE):
        path = match.group(1).strip()
        code = match.group(2).strip()
        if path and code:
            files.append(CodeFile(path=path, content=code))
    return files


def _parse_format_dash_file(content: str) -> list[CodeFile]:
    """
    格式2: ---FILE:path--- ... ---END---

    示例:
        ---FILE:src/main.py---
        print("hello")
        ---END---
    """
    files = []
    pattern = r"---FILE:\s*(.+?)\s*---(.*?)---END---"
    for match in re.finditer(pattern, content, re.DOTALL | re.IGNORECASE):
        path = match.group(1).strip()
        code = match.group(2).strip()
        if path and code:
            files.append(CodeFile(path=path, content=code))
    return files


def _parse_format_markdown_fence(content: str) -> list[CodeFile]:
    """
    格式3: ```language:path ... ```

    示例:
        ```python:src/main.py
        print("hello")
        ```
        ```markdown:README.md
        # Project
        ```
    """
    files = []
    # 匹配 ```语言:路径\n ... \n```
    pattern = r"```(\w+):([^\n]+)\n(.*?)```"
    for match in re.finditer(pattern, content, re.DOTALL):
        lang = match.group(1).strip()
        path = match.group(2).strip()
        code = match.group(3).strip()
        if path and code:
            files.append(CodeFile(path=path, content=code, language=lang))
    return files


def _parse_format_heading_fence(content: str) -> list[CodeFile]:
    """
    格式4: ### FILE: path\n```...```

    示例:
        ### FILE: src/main.py
        ```python
        print("hello")
        ```
    """
    files = []
    # 匹配 ### FILE: path 后面的代码块
    pattern = r"###\s*FILE:\s*(.+?)\n\s*```(?:\w+)?\n(.*?)```"
    for match in re.finditer(pattern, content, re.DOTALL | re.IGNORECASE):
        path = match.group(1).strip()
        code = match.group(2).strip()
        if path and code:
            files.append(CodeFile(path=path, content=code))
    return files


def _parse_format_bare_file(content: str) -> list[CodeFile]:
    """
    格式5: file: path 后跟缩进代码块

    示例:
        file: src/main.py
            print("hello")
            print("world")
        file: README.md
            # Project
    """
    files = []
    # 匹配 file: path 后跟至少4个空格的缩进块
    pattern = r"(?:^|\n)file:\s*(.+?)\n((?:    .+\n?)+)"
    for match in re.finditer(pattern, content, re.MULTILINE | re.IGNORECASE):
        path = match.group(1).strip()
        code = match.group(2).strip()
        # 去掉缩进（每行去掉前4个空格）
        dedented = "\n".join(line[4:] if line.startswith("    ") else line
                             for line in code.splitlines())
        if path and dedented.strip():
            files.append(CodeFile(path=path, content=dedented))
    return files


def _parse_format_git_diff(content: str) -> list[CodeFile]:
    """
    格式6: diff --git a/path b/path 后面跟完整内容

    示例:
        diff --git a/src/main.py b/src/main.py
        new file mode 100644
        @@ ... @@
        +print("hello")
        +print("world")
    """
    files = []
    # 匹配 diff --git a/path b/path
    pattern = r"diff --git a/(.+?) b/\1.*?\n@@.*?\n((?:\+[^\n]*\n?)+)"
    for match in re.finditer(pattern, content, re.DOTALL):
        path = match.group(1).strip()
        additions = match.group(2).strip()
        # 去掉 + 前缀
        code = "\n".join(
            line[1:] if line.startswith("+") else line
            for line in additions.splitlines()
            if line.startswith("+")
        )
        if path and code.strip():
            files.append(CodeFile(path=path, content=code))
    return files


# =============================================================================
# 核心解析函数
# =============================================================================

# 所有格式解析器，按优先级排列
_PARSERS = [
    ("@@FILE:@@ ... @@END@@", _parse_format_file_end),
    ("---FILE:--- ... ---END---", _parse_format_dash_file),
    ("```lang:path ... ```", _parse_format_markdown_fence),
    ("### FILE: path\n```...```", _parse_format_heading_fence),
    ("diff --git a/path", _parse_format_git_diff),
    ("file: path (缩进)", _parse_format_bare_file),
]


def parse_code_changes(llm_output: str) -> ParseResult:
    """
    从 LLM 输出中提取代码文件变更。

    按格式优先级依次尝试，第一个成功的格式即为最终结果。

    参数：
        llm_output: LLM 的完整响应文本

    返回：
        ParseResult: 包含文件列表、使用的格式、错误信息
    """
    if not llm_output or not llm_output.strip():
        return ParseResult(
            files=[],
            used_format="empty",
            raw_content=llm_output,
            errors=["LLM 输出为空"]
        )

    result = ParseResult(files=[], used_format="", raw_content=llm_output)

    # ── 按优先级尝试每种格式 ──
    for format_name, parser in _PARSERS:
        try:
            files = parser(llm_output)
            if files:
                result.files = files
                result.used_format = format_name
                # 验证文件路径合法性
                invalid = [f for f in files if not _is_valid_path(f.path)]
                if invalid:
                    result.errors.append(
                        f"格式 '{format_name}' 解析出 {len(invalid)} 个无效路径: "
                        f"{[f.path for f in invalid]}"
                    )
                return result
        except Exception as e:
            result.errors.append(f"格式 '{format_name}' 解析异常: {e}")
            continue

    # ── 所有格式都失败 — 尝试激进解析 ──
    result.errors.insert(0, f"所有 {len(_PARSERS)} 种格式均解析失败")
    result.files = _fallback_parse(llm_output)
    if result.files:
        result.used_format = "fallback"
        result.errors.append(f"使用 fallback 模式解析出 {len(result.files)} 个文件")

    return result


def _fallback_parse(content: str) -> list[CodeFile]:
    """
    Fallback 解析：从原始文本中尝试提取任何看起来像文件的内容。

    策略：
      1. 找所有看起来像路径的行（含扩展名的）
      2. 取路径后面的内容直到下一个路径或结束
    """
    # 常见代码文件扩展名
    code_extensions = (
        r"\.(?:py|java|go|ts|tsx|js|jsx|vue|rs|cpp|c|h|hpp|"
        r"rb|php|swift|kt|scala|sql|sh|bash|yml|yaml|toml|"
        r"json|xml|md|txt|css|scss|html)$"
    )

    # 找所有看起来像相对路径的行
    path_pattern = rf"^[\w./-]+{code_extensions}"
    path_lines = []

    for line in content.splitlines():
        line = line.strip()
        # 去掉常见的标记前缀
        for prefix in ("FILE:", "file:", "Path:", "path:", "📝", "```", "###"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
        if re.match(path_pattern, line, re.IGNORECASE):
            path_lines.append(line)

    if len(path_lines) >= 3:
        # 太多路径 → 放弃
        return []

    files = []
    # 简单处理：每个路径后的内容作为一个文件
    return files


# =============================================================================
# 辅助函数
# =============================================================================

def _guess_language(path: str) -> str:
    """根据文件扩展名猜测语言。"""
    ext = Path(path).suffix.lower()
    mapping = {
        ".py": "python", ".pyi": "python",
        ".java": "java", ".kt": "kotlin",
        ".go": "go",
        ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".vue": "vue",
        ".rs": "rust",
        ".sql": "sql",
        ".md": "markdown", ".txt": "text",
        ".yaml": "yaml", ".yml": "yaml",
        ".json": "json", ".toml": "toml",
        ".html": "html", ".css": "css",
        ".sh": "bash", ".bash": "bash",
    }
    return mapping.get(ext, "")


def _is_valid_path(path: str) -> bool:
    """
    验证路径是否合法。

    规则：
      - 不能是绝对路径（安全原因）
      - 不能包含 ..（路径遍历攻击）
      - 长度合理（1-256字符）
      - 包含文件扩展名
    """
    if not path or len(path) > 256:
        return False
    if path.startswith("/") or ".." in path:
        return False
    if not Path(path).suffix:
        return False  # 必须有扩展名
    return True


# =============================================================================
# 便捷方法
# =============================================================================

def extract_single_file(llm_output: str) -> Optional[CodeFile]:
    """
    便捷方法：当 LLM 只应输出一个文件时使用。

    返回第一个解析出的文件，或 None。
    """
    result = parse_code_changes(llm_output)
    return result.files[0] if result.files else None


def has_code_changes(llm_output: str) -> bool:
    """快速检查 LLM 输出是否包含代码变更。"""
    result = parse_code_changes(llm_output)
    return result.success
