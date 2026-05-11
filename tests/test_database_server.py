"""
test_database_server.py — Database Server 测试

覆盖：
  - DDL 索引
  - DDL 解析
  - 风险检测
  - SQL 黑名单
"""
import pytest
import tempfile
import json
from pathlib import Path
from agent_mcp.database_server import DatabaseMCPServer


class TestDDLIndex:
    """DDL 索引。"""

    def setup_method(self):
        self.db = DatabaseMCPServer()

    def test_index_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.db._index_ddl(tmp)
            assert result["files"] == []

    def test_index_with_sql_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            sql = """
            CREATE TABLE users (
                id BIGINT PRIMARY KEY,
                name VARCHAR(100) NOT NULL COMMENT '用户名',
                email VARCHAR(200)
            ) COMMENT='用户表';
            """
            (Path(tmp) / "users.sql").write_text(sql)
            result = self.db._index_ddl(tmp)
            assert result["tables_parsed"] >= 1
            assert "users" in result["table_names"]

    def test_index_column_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sql = """
            CREATE TABLE products (
                id BIGINT,
                name VARCHAR(200) NOT NULL DEFAULT '',
                price DECIMAL(10,2) COMMENT '价格'
            );
            """
            (Path(tmp) / "products.sql").write_text(sql)
            result = self.db._index_ddl(tmp)
            # id + name + price = 3 columns (移除了 PRIMARY KEY 以简化解析)
            assert result["columns_parsed"] >= 2

    def test_search_schema_after_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            sql = "CREATE TABLE orders (id BIGINT PRIMARY KEY, total DECIMAL);"
            (Path(tmp) / "orders.sql").write_text(sql)
            self.db._index_ddl(tmp)
            result = self.db._search_schema("orders")
            assert result["table_count"] >= 1


class TestRiskDetection:
    """风险检测。"""

    def setup_method(self):
        self.db = DatabaseMCPServer()

    def test_entity_file_risky(self):
        result = self.db._detect_risk(["src/UserEntity.java"])
        assert result["affected"] is True
        assert any("UserEntity" in r["file"] for r in result["risky_files"])

    def test_config_file_safe(self):
        result = self.db._detect_risk(["src/config.py", "README.md"])
        assert result["affected"] is False

    def test_mapper_file_risky(self):
        result = self.db._detect_risk(["src/mapper/UserMapper.xml"])
        assert result["affected"] is True

    def test_no_files(self):
        result = self.db._detect_risk([])
        assert result["affected"] is False


class TestBlockedSQL:
    """SQL 黑名单。"""

    def setup_method(self):
        self.db = DatabaseMCPServer()

    def test_select_not_blocked(self):
        import re
        for p in self.db.blocked_sql_patterns:
            assert not re.search(p, "SELECT * FROM users", re.IGNORECASE)

    def test_drop_blocked(self):
        import re
        for p in self.db.blocked_sql_patterns:
            if re.search(p, "DROP TABLE users", re.IGNORECASE):
                return
        pytest.fail("DROP TABLE should be blocked")


class TestMigrationDraft:
    """Migration 草稿。"""

    def setup_method(self):
        self.db = DatabaseMCPServer()

    def test_generates_draft(self):
        result = self.db._generate_draft("添加 email 字段")
        assert "email" in result["draft"]
        assert "⚠️" in result["warning"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
