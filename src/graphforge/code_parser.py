"""
代码解析器 — 从 LLM 输出中提取代码文件变更

★ v0.1.4 P1-9: 新增 unified diff patch 模式（最高优先级）

支持多种 LLM 输出格式（按可靠性排序）：
  0. @@PATCH:path@@ ... @@END@@      ← ★ 新增: unified diff patch（最高优先级）
  1. @@FILE:path@@ ... @@END@@      ← 完整文件替换
  2. ---FILE:path--- ... ---END---     ← 备选格式
  3. ```language:path ... ```          ← Markdown 代码块格式
  4. ### FILE: path\n```...```         ← 标题+代码块格式
  5. diff --git a/path                 ← Git diff 格式
  6. file: path\n    (缩进代码)       ← 纯文本格式

解析策略：
  - 按格式优先级依次尝试
  - 第一个成功解析出 ≥1 个文件的格式即为最终结果
  - 如果所有格式都失败，返回空列表 + 原始内容供调试
  - ★ Patch 模式: 仅生成变更部分，不对原文件做完整替换

用法：
    from graphforge.code_parser import parse_code_changes

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

def _parse_format_patch(content: str) -> list[CodeFile]:
    """
    ★ P1-9: 格式0: @@PATCH:path@@ ... @@END@@（最高优先级）

    Patch 模式使用 unified diff 格式，仅描述变更部分而非完整文件内容。
    这避免了 LLM 输出完整文件时误删原文件未纳入上下文的内容。

    示例:
        @@PATCH:src/main.py@@
        @@ -10,7 +10,8 @@
         import os
        -import json
        +import json
        +import yaml
         from app import create_app
        @@END@@

    解析策略：
      - 从 unified diff 中提取 + 行作为新增/修改内容
      - v0.1: 简化处理 — 将 patch 内容作为标记后的完整文件内容
        （后续版本实现真正的 diff 应用）
    """
    files = []
    pattern = r"@@PATCH:\s*(.+?)\s*@@(.*?)@@END@@"
    for match in re.finditer(pattern, content, re.DOTALL | re.IGNORECASE):
        path = match.group(1).strip()
        patch_content = match.group(2).strip()
        if path and patch_content:
            # v0.1.4: 将 patch 内容标记为 diff 模式
            # 完整内容替换模式标记为 "full"，patch 模式标记为 "patch"
            files.append(CodeFile(
                path=path,
                content=f"# PATCH MODE — unified diff for {path}\n{patch_content}",
                language="diff"
            ))
    return files


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

# 所有格式解析器，按优先级排列（★ P1-9: patch 模式最高优先级）
_PARSERS = [
    ("@@PATCH:@@ unified diff ... @@END@@", _parse_format_patch),
    ("@@FILE:@@ ... @@END@@", _parse_format_file_end),
    ("---FILE:--- ... ---END---", _parse_format_dash_file),
    ("```lang:path ... ```", _parse_format_markdown_fence),
    ("### FILE: path\\n```...```", _parse_format_heading_fence),
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


# =============================================================================
# ★ P5-25: Unified Diff 应用引擎
# =============================================================================

def apply_unified_diff(patch_text: str, file_path: str, dry_run: bool = False) -> dict:
    """
    将 unified diff patch 应用到实际文件。

    这是从 LLM 生成的 @@PATCH 格式中提取 unified diff 并真正应用到文件的引擎。

    参数：
        patch_text: unified diff 文本（从 @@PATCH:path@@ ... @@END@@ 中提取的）
        file_path:  目标文件路径
        dry_run:    True 时只返回变更预览，不修改文件

    返回：
        dict: {applied: bool, hunks_applied: int, hunks_failed: int,
               errors: [...], dry_run: bool, preview: str|None}

    算法：
        1. 读取原文件内容
        2. 解析 unified diff hunks（@@ -old,count +new,count @@）
        3. 逐 hunk 应用到原文件
        4. 写回文件（非 dry_run）
    """
    pf = Path(file_path)
    if not pf.exists():
        return {"applied": False, "error": f"文件不存在: {file_path}"}

    try:
        original_lines = pf.read_text(encoding="utf-8").splitlines(keepends=True)
    except Exception as e:
        return {"applied": False, "error": f"读取文件失败: {e}"}

    # ── 解析 hunks ──
    hunk_pattern = re.compile(
        r"@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*?)(?=@@\s+-|\Z)",
        re.DOTALL
    )
    hunks = hunk_pattern.findall(patch_text)

    if not hunks:
        # 尝试无 hunk header 的简单格式：只有 + 和 - 行
        return _apply_simple_patch(patch_text, original_lines, pf, dry_run)

    # ── 逐 hunk 应用 ──
    result_lines = list(original_lines)
    offset = 0  # 累计偏移量
    applied = 0
    failed = 0
    errors: list[str] = []

    for hunk in hunks:
        old_start = int(hunk[0]) - 1  # 0-indexed
        old_count = int(hunk[1]) if hunk[1] else 1
        new_start = int(hunk[2]) - 1
        new_count = int(hunk[3]) if hunk[3] else 1
        hunk_body = hunk[4]

        # 解析 hunk body
        added_lines: list[str] = []
        removed_count = 0
        context_before = 0

        for line in hunk_body.split("\n"):
            line = line.rstrip("\r")
            if not line:
                continue
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append(line[1:] + "\n")
            elif line.startswith("-") and not line.startswith("---"):
                removed_count += 1
            elif line.startswith(" "):
                context_before += 1

        # 计算在结果文件中的位置
        pos = old_start + offset
        if pos < 0 or pos > len(result_lines):
            errors.append(f"Hunk 位置越界: old_start={old_start}, offset={offset}, len={len(result_lines)}")
            failed += 1
            continue

        # 删除旧行
        end_pos = min(pos + old_count, len(result_lines))
        del result_lines[pos:end_pos]

        # 插入新行
        for line in added_lines:
            result_lines.insert(pos, line)
            pos += 1

        offset += len(added_lines) - (end_pos - (old_start + offset))
        applied += 1

    if applied == 0:
        return {"applied": False, "hunks_applied": 0, "hunks_failed": failed, "errors": errors}

    if dry_run:
        preview = "".join(result_lines)
        return {
            "applied": True, "dry_run": True,
            "hunks_applied": applied, "hunks_failed": failed,
            "errors": errors,
            "preview": preview[:2000],
            "total_lines": len(result_lines),
        }

    # ── 写回文件 ──
    try:
        pf.write_text("".join(result_lines), encoding="utf-8")
    except Exception as e:
        return {"applied": False, "error": f"写入文件失败: {e}"}

    return {
        "applied": True,
        "hunks_applied": applied,
        "hunks_failed": failed,
        "errors": errors,
        "total_lines": len(result_lines),
    }


def _apply_simple_patch(
    patch_text: str, original_lines: list[str], file_path: Path, dry_run: bool
) -> dict:
    """
    简单 patch 模式：仅有 +/- 行，无 hunk header。

    用于 LLM 输出简化的 patch 格式（只有添加和删除行，没有上下文）。
    """
    additions: list[tuple[int, str]] = []
    deletions: set[int] = set()
    current_line = 0

    for line in patch_text.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("+") and not line.startswith("+++"):
            additions.append((current_line, line[1:] + "\n"))
            current_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions.add(current_line)
        elif line.strip():
            current_line += 1

    result = []
    for i, line in enumerate(original_lines):
        if i not in deletions:
            result.append(line)
        # 在对应位置插入新增行
        for add_pos, add_line in list(additions):
            if add_pos == i:
                result.append(add_line)

    if dry_run:
        return {"applied": True, "dry_run": True, "preview": "".join(result)[:2000]}

    file_path.write_text("".join(result), encoding="utf-8")
    return {"applied": True, "additions": len(additions), "deletions": len(deletions)}
