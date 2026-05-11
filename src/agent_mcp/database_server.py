"""
Database MCP Server v0.1 — 只读文档索引

v0.1: 不连接数据库，只做 DDL/MyBatis 文档索引
v0.2: 预留只读连接验证
安全红线: 永远不执行 INSERT/UPDATE/DELETE
★ 继承 BaseMCPServer
"""

import json
import sys
from pathlib import Path

from agent_mcp.base_mcp import BaseMCPServer


class DatabaseMCPServer(BaseMCPServer):
    """Database MCP Server — 继承 BaseMCPServer。"""

    name = "database-mcp"
    version = "0.1.0"

    def __init__(self):
        super().__init__()
        self.tools = {
            "database_index_ddl": {
                "description": "索引 DDL 文件",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ddl_dir": {"type": "string", "description": "DDL 文件目录"}
                    },
                    "required": ["ddl_dir"]
                }
            },
            "database_search_schema": {
                "description": "搜索数据库表结构",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }
            },
            "database_detect_risk": {
                "description": "检测代码变更对数据库的影响",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "changed_files": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["changed_files"]
                }
            },
            "database_generate_migration_draft": {
                "description": "生成 migration 草稿（不执行）",
                "inputSchema": {
                    "type": "object",
                    "properties": {"description": {"type": "string"}},
                }
            },
        }
        self.blocked_sql_patterns = [
            r"\bDROP\s+TABLE", r"\bTRUNCATE",
            r"\bDELETE\s+FROM", r"\bINSERT\s+INTO", r"\bUPDATE\b",
        ]
        self.write_enabled = False

    def _call_tool(self, name: str, args: dict):
        handler = {
            "database_index_ddl": self._index_ddl,
            "database_search_schema": self._search_schema,
            "database_detect_risk": self._detect_risk,
            "database_generate_migration_draft": self._generate_draft,
        }.get(name)
        if handler:
            try:
                result = handler(**args)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown: {name}"}], "isError": True}

    def _index_ddl(self, ddl_dir: str) -> dict:
        p = Path(ddl_dir)
        if not p.exists():
            return {"error": f"DDL 目录不存在: {ddl_dir}"}
        files = list(p.glob("*.sql"))
        return {"ddl_dir": ddl_dir, "files": [f.name for f in files], "count": len(files)}

    def _search_schema(self, query: str) -> dict:
        return {"query": query, "tables": [], "message": "v0.1: 需先执行 database_index_ddl 构建索引"}

    def _detect_risk(self, changed_files: list[str]) -> dict:
        risky = []
        for f in changed_files:
            if any(kw in f.lower() for kw in ["entity", "mapper", "dao", "model", "ddl", "sql"]):
                risky.append(f)
        return {"affected": len(risky) > 0, "risky_files": risky, "message": "请 DBA Review"}

    def _generate_draft(self, description: str) -> dict:
        draft = (
            f"-- Migration draft (v0.1)\n"
            f"-- Description: {description}\n"
            f"-- ⚠️ 此文件由 Agent 生成草稿，请人工审核后执行\n"
        )
        return {"draft": draft, "warning": "⚠️ 此 SQL 不会被执行，需 DBA review 后人工执行"}


if __name__ == "__main__":
    DatabaseMCPServer().run_stdio()
