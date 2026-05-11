"""
test_config_loader.py — 配置加载器测试

覆盖：
  - 默认值（P2-11 企业内网默认）
  - 多语言项目类型
  - deny_paths 安全校验（通过 AppConfig 实例方法）
"""
import pytest
from agent_mcp.config_loader import (
    ProjectConfig,
    ProjectType,
    AppConfig,
    LANGUAGE_DEFAULTS,
)


class TestProjectType:
    """项目类型枚举。"""

    def test_all_types_have_defaults(self):
        for pt in ProjectType:
            assert pt in LANGUAGE_DEFAULTS, f"{pt} 缺少默认配置"

    def test_java_defaults(self):
        d = LANGUAGE_DEFAULTS[ProjectType.JAVA]
        assert "pom.xml" in d["deny_paths"]
        assert "src/main/java" in d["source_dirs"]

    def test_python_defaults(self):
        d = LANGUAGE_DEFAULTS[ProjectType.PYTHON]
        assert ".env" in d["deny_paths"]
        assert "pyproject.toml" in d["deny_paths"]

    def test_generic_defaults(self):
        d = LANGUAGE_DEFAULTS[ProjectType.GENERIC]
        assert ".env" in d["deny_paths"]


class TestDefaultValues:
    """★ P2-11: 默认值应为企业内网。"""

    def test_internal_custom_default(self):
        pc = ProjectConfig(name="test")
        assert pc.code_platform == "internal_custom"


class TestDenyPaths:
    """deny_paths 路径拒绝校验（通过 AppConfig 实例方法）。"""

    def test_is_path_denied_exact_match(self):
        cfg = AppConfig(
            project=ProjectConfig(name="test"),
            change_policy={"deny_paths": ["pom.xml", ".env"]},
        )
        assert cfg.is_path_denied("pom.xml")
        assert cfg.is_path_denied(".env")
        assert not cfg.is_path_denied("src/main.py")

    def test_is_path_denied_glob_match(self):
        cfg = AppConfig(
            project=ProjectConfig(name="test"),
            change_policy={"deny_paths": ["*.pem", "*.key"]},
        )
        assert cfg.is_path_denied("server.pem")
        assert cfg.is_path_denied("id_rsa.key")
        assert not cfg.is_path_denied("server.crt")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
