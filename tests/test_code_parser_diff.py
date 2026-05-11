"""
test_code_parser_diff.py — Unified Diff 应用引擎测试

★ P5-25: 测试 apply_unified_diff()
"""
import pytest
import tempfile
from pathlib import Path
from agent_mcp.code_parser import apply_unified_diff


class TestApplyUnifiedDiff:
    """unified diff 应用引擎。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_file(self, name, content):
        p = Path(self.tmpdir) / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_add_line(self):
        fp = self._write_file("test.py", "import os\nprint('hello')\n")
        patch = "@@ -1,2 +1,3 @@\n import os\n+import sys\n print('hello')\n"
        result = apply_unified_diff(patch, fp)
        assert result["applied"] is True
        assert result["hunks_applied"] == 1
        content = Path(fp).read_text()
        assert "import sys" in content

    def test_remove_line(self):
        fp = self._write_file("test.py", "import os\nimport sys\nprint('hello')\n")
        patch = "@@ -1,3 +1,2 @@\n import os\n-import sys\n print('hello')\n"
        result = apply_unified_diff(patch, fp)
        assert result["applied"] is True
        content = Path(fp).read_text()
        assert "import sys" not in content

    def test_modify_line(self):
        fp = self._write_file("test.py", "import os\nx = 1\n")
        patch = "@@ -1,2 +1,2 @@\n import os\n-x = 1\n+x = 2\n"
        result = apply_unified_diff(patch, fp)
        assert result["applied"] is True
        content = Path(fp).read_text()
        assert "x = 2" in content
        assert "x = 1" not in content

    def test_file_not_found(self):
        result = apply_unified_diff("@@ -1 +1 @@", "/nonexistent/file.py")
        assert result["applied"] is False
        assert "不存在" in result.get("error", "")

    def test_dry_run(self):
        fp = self._write_file("test.py", "import os\n")
        patch = "@@ -1,1 +1,2 @@\n import os\n+import sys\n"
        result = apply_unified_diff(patch, fp, dry_run=True)
        assert result["applied"] is True
        assert result["dry_run"] is True
        # 原文件不应被修改
        content = Path(fp).read_text()
        assert "import sys" not in content

    def test_empty_patch(self):
        fp = self._write_file("test.py", "content\n")
        result = apply_unified_diff("no hunks here", fp)
        assert result["applied"] is False

    def test_simple_patch_mode(self):
        """无 hunk header 的简单 +/- 模式。"""
        fp = self._write_file("test.py", "import os\nprint('hello')\n")
        patch = "+import sys\n"
        result = apply_unified_diff(patch, fp)
        assert result["applied"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
