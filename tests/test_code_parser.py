"""
test_code_parser.py — 测试 LLM 代码解析器各格式

覆盖：
  - @@PATCH 格式（P1-9 新增）
  - @@FILE 格式
  - Markdown 代码块格式
  - 空输入 / 边界情况
  - 路径安全校验
"""
import pytest
from graphforge.code_parser import (
    parse_code_changes,
    _is_valid_path,
    _guess_language,
    _parse_format_patch,
    _parse_format_file_end,
    _parse_format_markdown_fence,
)


class TestPathValidation:
    """路径安全校验。"""

    def test_valid_relative_path(self):
        assert _is_valid_path("src/main.py")

    def test_reject_absolute_path(self):
        assert not _is_valid_path("/etc/passwd")

    def test_reject_parent_traversal(self):
        assert not _is_valid_path("../../etc/shadow")

    def test_reject_no_extension(self):
        assert not _is_valid_path("README")

    def test_reject_empty(self):
        assert not _is_valid_path("")

    def test_reject_too_long(self):
        assert not _is_valid_path("a" * 257 + ".py")


class TestLanguageGuessing:
    """语言检测。"""

    def test_python(self):
        assert _guess_language("src/main.py") == "python"

    def test_java(self):
        assert _guess_language("src/UserController.java") == "java"

    def test_typescript(self):
        assert _guess_language("components/App.tsx") == "typescript"

    def test_vue(self):
        assert _guess_language("pages/Home.vue") == "vue"

    def test_markdown(self):
        assert _guess_language("README.md") == "markdown"


class TestParseFormatPatch:
    """@@PATCH 格式（P1-9 新增）。"""

    def test_single_patch(self):
        content = "@@PATCH:src/main.py@@\n@@ -1,5 +1,6 @@\n import os\n+import yaml\n@@END@@"
        files = _parse_format_patch(content)
        assert len(files) == 1
        assert files[0].path == "src/main.py"
        assert files[0].language == "diff"

    def test_multiple_patches(self):
        content = "@@PATCH:a.py@@\n@@ -1 +1 @@\n-old\n+new\n@@END@@\n@@PATCH:b.py@@\n@@ -5 +5 @@\n-old2\n+new2\n@@END@@"
        files = _parse_format_patch(content)
        assert len(files) == 2


class TestParseFormatFile:
    """@@FILE 格式。"""

    def test_single_file(self):
        content = '@@FILE:src/main.py@@\nprint("hello")\n@@END@@'
        files = _parse_format_file_end(content)
        assert len(files) == 1
        assert files[0].path == "src/main.py"
        assert files[0].line_count == 1

    def test_multiple_files(self):
        content = "@@FILE:a.py@@\na_content\n@@END@@\n@@FILE:b.py@@\nb_content\n@@END@@"
        files = _parse_format_file_end(content)
        assert len(files) == 2

    def test_whitespace_handling(self):
        content = "@@FILE:  path/to/file.py  @@\ncontent with spaces\n@@END@@"
        files = _parse_format_file_end(content)
        assert len(files) == 1
        assert files[0].path == "path/to/file.py"


class TestEmptyInput:
    """空输入 / 边界情况。"""

    def test_empty_string(self):
        result = parse_code_changes("")
        assert not result.success
        assert result.used_format == "empty"

    def test_no_code_blocks(self):
        result = parse_code_changes("This is just a text without any code blocks.")
        assert not result.success

    def test_none_handled(self):
        result = parse_code_changes(None)
        assert not result.success


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
