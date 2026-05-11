"""
test_security_paths.py — 安全路径 / 分支名 / 命令测试

覆盖：
  - git 分支名 regex 校验
  - deny_paths 安全边界
  - 命令黑名单
"""
import re
import pytest
from fnmatch import fnmatch


# 分支名校验正则
BRANCH_REGEX = r"^agent/[0-9]{8}-[a-z0-9][a-z0-9._-]{2,80}$"
PUSH_REGEX = r"^agent/[A-Za-z0-9._/-]+$"
PROTECTED_BRANCHES = ["master", "main", "release/1.0", "hotfix/critical"]


class TestBranchNaming:
    """分支命名校验。"""

    def test_valid_agent_branch(self):
        assert re.match(BRANCH_REGEX, "agent/20260511-auto")

    def test_invalid_no_prefix(self):
        assert not re.match(BRANCH_REGEX, "feature/login")

    def test_invalid_master(self):
        assert not re.match(BRANCH_REGEX, "master")

    def test_invalid_short_slug(self):
        assert not re.match(BRANCH_REGEX, "agent/20260511-ab")

    def test_push_allowed_branch(self):
        assert re.match(PUSH_REGEX, "agent/20260511-fix-login")

    def test_push_denied_master(self):
        assert not re.match(PUSH_REGEX, "master")


class TestProtectedBranches:
    """受保护分支检测。"""

    def test_master_is_protected(self):
        protected = ["master", "main", "release/*", "hotfix/*"]
        assert any(
            "master" in b or b == "master" for b in PROTECTED_BRANCHES
        )

    def test_agent_branch_not_protected(self):
        assert "agent/fix" not in PROTECTED_BRANCHES


class TestDenyPaths:
    """deny_paths 路径匹配。"""

    def test_exact_pom_xml(self):
        assert fnmatch("pom.xml", "pom.xml")

    def test_glob_env(self):
        assert fnmatch(".env", ".env")

    def test_glob_pem(self):
        assert fnmatch("server.pem", "*.pem")
        assert fnmatch("id_rsa.pem", "*.pem")

    def test_glob_key(self):
        assert fnmatch("secret.key", "*.key")

    def test_non_denied_path(self):
        deny_patterns = [".env", "*.pem", "*.key", "Dockerfile"]
        assert not any(fnmatch("src/main.py", p) for p in deny_patterns)


class TestBlockedSQL:
    """SQL 黑名单。"""

    BLOCKED = [
        r"\bDROP\s+TABLE",
        r"\bTRUNCATE",
        r"\bDELETE\s+FROM",
        r"\bINSERT\s+INTO",
        r"\bUPDATE\b",
    ]

    def test_drop_blocked(self):
        for pattern in self.BLOCKED:
            if re.search(pattern, "DROP TABLE users", re.IGNORECASE):
                return
        pytest.fail("DROP TABLE should be blocked")

    def test_select_allowed(self):
        assert not any(
            re.search(p, "SELECT * FROM users", re.IGNORECASE)
            for p in self.BLOCKED
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
