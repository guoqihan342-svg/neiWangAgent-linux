"""
test_knowledge_server.py — Knowledge Server 测试

覆盖：
  - 语言检测
  - 文件分类
  - 搜索（从索引）
"""
import pytest
import tempfile
from pathlib import Path
from graphforge.knowledge_server import KnowledgeMCPServer, LANGUAGE_EXTENSIONS


class TestLanguageExtensions:
    """语言扩展名映射。"""

    def test_known_extensions(self):
        assert LANGUAGE_EXTENSIONS[".py"] == "python"
        assert LANGUAGE_EXTENSIONS[".java"] == "java"
        assert LANGUAGE_EXTENSIONS[".go"] == "go"
        assert LANGUAGE_EXTENSIONS[".vue"] == "vue"
        assert LANGUAGE_EXTENSIONS[".ts"] == "typescript"


class TestClassifyFile:
    """文件分类。"""

    def setup_method(self):
        self.ks = KnowledgeMCPServer()

    def test_python_file(self):
        assert self.ks._classify_file(Path("src/main.py")) == "python"

    def test_java_file(self):
        assert self.ks._classify_file(Path("UserController.java")) == "java"

    def test_dockerfile(self):
        assert self.ks._classify_file(Path("Dockerfile")) == "docker"

    def test_unknown(self):
        assert self.ks._classify_file(Path("data.bin")) == "other"


class TestIndexAndSearch:
    """索引和搜索。"""

    def setup_method(self):
        self.ks = KnowledgeMCPServer()

    def test_index_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.ks._index_codebase(tmp, "summary")
            assert result["files_indexed"] == 0

    def test_index_with_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "main.py").write_text("print('hello')")
            (Path(tmp) / "README.md").write_text("# Project")
            result = self.ks._index_codebase(tmp, "summary")
            assert result["files_indexed"] >= 1
            assert "python" in result.get("language_distribution", {})

    def test_search_before_index(self):
        # ★ 确保没有残留 knowledge 索引影响
        import shutil
        kb_dir = Path(".graphforge/knowledge")
        if kb_dir.exists():
            shutil.rmtree(kb_dir)
        result = self.ks._search("test")
        assert result["total_files"] == 0

    def test_language_detect(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app.py").write_text("x=1")
            (Path(tmp) / "model.java").write_text("class X{}")
            result = self.ks._detect_language(tmp)
            assert result["primary_language"] in ("python", "java")


class TestKBStorage:
    """知识库持久化。"""

    def test_kb_dir_created(self):
        ks = KnowledgeMCPServer()
        kb = ks._kb_dir()
        assert kb.exists()
        assert kb.name == "knowledge"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
