"""
Database MCP Server v0.1.5 — 只读文档索引 + DDL 解析

★ P2-14: DDL 解析做实 — 提取表名/列名/类型/主键/索引，持久化到文件

v0.1: 不连接数据库，只做 DDL/MyBatis 文档索引
v0.2: 预留只读连接验证
安全红线: 永远不执行 INSERT/UPDATE/DELETE

持久化结构:
  .agent/knowledge/database/
    tables.jsonl   — 表定义（name, columns, primary_key, indexes, comment）
    columns.jsonl  — 列定义（table, name, type, nullable, default, comment）
    indexes.jsonl  — 索引定义（table, name, columns, unique）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from agent_mcp.base_mcp import BaseMCPServer
from agent_mcp.tracing import get_tracer, Tracer


class DatabaseMCPServer(BaseMCPServer):
    """Database MCP Server — DDL 解析 + 只读索引。"""

    name = "database-mcp"
    version = "0.1.5"

    def __init__(self):
        super().__init__()
        self.tracer: Tracer = get_tracer()
        self.blocked_sql_patterns = [
            r"\bDROP\s+TABLE", r"\bTRUNCATE",
            r"\bDELETE\s+FROM", r"\bINSERT\s+INTO", r"\bUPDATE\b",
        ]
        self.write_enabled = False
        self.tools = {
            "database_index_ddl": {
                "description": "索引 DDL 文件，解析表/列/索引结构",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ddl_dir": {"type": "string", "description": "DDL 文件目录"}
                    },
                    "required": ["ddl_dir"],
                },
            },
            "database_search_schema": {
                "description": "从已解析的索引中搜索数据库表结构",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "表名或列名关键词"},
                    },
                    "required": ["query"],
                },
            },
            "database_detect_risk": {
                "description": "检测代码变更对数据库的影响",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "changed_files": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["changed_files"],
                },
            },
            "database_generate_migration_draft": {
                "description": "生成 migration 草稿（不执行）",
                "inputSchema": {
                    "type": "object",
                    "properties": {"description": {"type": "string"}},
                },
            },
        }

    def _call_tool(self, name: str, args: dict):
        handler = {
            "database_index_ddl": self._index_ddl,
            "database_search_schema": self._search_schema,
            "database_detect_risk": self._detect_risk,
            "database_generate_migration_draft": self._generate_draft,
        }.get(name)
        if handler:
            try:
                start = time.perf_counter()
                result = handler(**args)
                elapsed = time.perf_counter() - start
                self.tracer.info(f"database.{name}", duration=elapsed, ok=True)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                self.tracer.error(f"database.{name}", detail=str(e))
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown: {name}"}], "isError": True}

    # ------------------------------------------------------------------
    # ★ P2-14: DDL 解析
    # ------------------------------------------------------------------

    # DDL 正则模式
    _RE_CREATE_TABLE = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?(\w+)[`\"']?\s*\((.*?)\)\s*"
        r"(?:ENGINE\s*=\s*\w+)?\s*(?:COMMENT\s*=\s*'([^']*)')?\s*;?",
        re.IGNORECASE | re.DOTALL,
    )
    _RE_COLUMN = re.compile(
        r"^\s*[`\"']?(\w+)[`\"']?\s+(\w+(?:\s*\([^)]*\))?)\s*"
        r"(?:NOT\s+NULL\s*)?(?:DEFAULT\s+([^\s,]+))?\s*"
        r"(?:COMMENT\s+'([^']*)')?\s*,?",
        re.IGNORECASE | re.MULTILINE,
    )
    _RE_PRIMARY_KEY = re.compile(
        r"PRIMARY\s+KEY\s*\(([^)]+)\)", re.IGNORECASE
    )
    _RE_INDEX = re.compile(
        r"(?:UNIQUE\s+)?(?:INDEX|KEY)\s+[`\"']?(\w+)[`\"']?\s*\(([^)]+)\)",
        re.IGNORECASE,
    )
    _RE_COMMENT_ON_COLUMN = re.compile(
        r"COMMENT\s+ON\s+COLUMN\s+[`\"']?(\w+)[`\"']?\.[`\"']?(\w+)[`\"']?\s+IS\s+'([^']*)'",
        re.IGNORECASE,
    )

    @staticmethod
    def _db_dir() -> Path:
        """获取数据库索引持久化目录。"""
        d = Path(".agent/knowledge/database")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_ddl(self, ddl_dir: str) -> dict:
        """
        ★ P2-14: 解析 DDL 文件并持久化索引。

        解析内容：
          - CREATE TABLE → 表名、列名/类型/nullable/default/comment
          - PRIMARY KEY / UNIQUE KEY / INDEX
          - COMMENT ON COLUMN（PostgreSQL）
        """
        p = Path(ddl_dir)
        if not p.exists():
            return {"error": f"DDL 目录不存在: {ddl_dir}"}

        tables: list[dict] = []
        columns: list[dict] = []
        indexes: list[dict] = []

        for sql_file in sorted(p.glob("*.sql")):
            try:
                content = sql_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # ── 解析 CREATE TABLE ──
            for table_match in self._RE_CREATE_TABLE.finditer(content):
                table_name = table_match.group(1)
                body = table_match.group(2)
                table_comment = table_match.group(3) or ""

                # 提取主键
                pk_match = self._RE_PRIMARY_KEY.search(body)
                pk_cols = (
                    [c.strip().strip("`\"'") for c in pk_match.group(1).split(",")]
                    if pk_match else []
                )

                # 提取索引
                for idx_match in self._RE_INDEX.finditer(body):
                    idx_name = idx_match.group(1)
                    idx_cols = [
                        c.strip().strip("`\"'")
                        for c in idx_match.group(2).split(",")
                    ]
                    is_unique = "UNIQUE" in idx_match.group(0).upper()
                    indexes.append({
                        "table": table_name,
                        "name": idx_name,
                        "columns": idx_cols,
                        "unique": is_unique,
                    })

                # 提取列定义
                col_matches = self._RE_COLUMN.findall(body)
                for col_match in col_matches:
                    col_name = col_match[0]
                    col_type = col_match[1].strip()
                    is_nullable = "NOT NULL" not in col_match[0]  # 简化判断
                    default_val = col_match[2] if len(col_match) > 2 and col_match[2] else None
                    col_comment = col_match[3] if len(col_match) > 3 and col_match[3] else ""
                    is_pk = col_name in pk_cols

                    columns.append({
                        "table": table_name,
                        "name": col_name,
                        "type": col_type,
                        "nullable": is_nullable,
                        "default": default_val,
                        "comment": col_comment,
                        "primary_key": is_pk,
                    })

                tables.append({
                    "name": table_name,
                    "columns": [c["name"] for c in columns if c["table"] == table_name],
                    "primary_key": pk_cols,
                    "indexes": [i for i in indexes if i["table"] == table_name],
                    "comment": table_comment,
                    "source_file": sql_file.name,
                })

            # ── 解析 PostgreSQL COMMENT ON COLUMN ──
            for cmt_match in self._RE_COMMENT_ON_COLUMN.finditer(content):
                tbl = cmt_match.group(1)
                col = cmt_match.group(2)
                cmt = cmt_match.group(3)
                # 更新已有列的注释
                for c in columns:
                    if c["table"] == tbl and c["name"] == col:
                        c["comment"] = cmt
                        break

        # ── 持久化到文件 ──
        db_dir = self._db_dir()
        db_dir.mkdir(parents=True, exist_ok=True)

        if tables:
            (db_dir / "tables.jsonl").write_text(
                "\n".join(json.dumps(t, ensure_ascii=False) for t in tables) + "\n",
                encoding="utf-8",
            )
        if columns:
            (db_dir / "columns.jsonl").write_text(
                "\n".join(json.dumps(c, ensure_ascii=False) for c in columns) + "\n",
                encoding="utf-8",
            )
        if indexes:
            (db_dir / "indexes.jsonl").write_text(
                "\n".join(json.dumps(i, ensure_ascii=False) for i in indexes) + "\n",
                encoding="utf-8",
            )

        return {
            "ddl_dir": ddl_dir,
            "files": [f.name for f in p.glob("*.sql")],
            "tables_parsed": len(tables),
            "columns_parsed": len(columns),
            "indexes_parsed": len(indexes),
            "table_names": [t["name"] for t in tables],
        }

    def _search_schema(self, query: str) -> dict:
        """
        ★ P2-14: 从持久化索引搜索表结构。

        支持按表名、列名搜索。
        """
        db_dir = self._db_dir()
        query_lower = query.lower()

        # ── 搜索表 ──
        matched_tables: list[dict] = []
        tables_path = db_dir / "tables.jsonl"
        if tables_path.exists():
            for line in tables_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                    if query_lower in t["name"].lower() or query_lower in t.get("comment", "").lower():
                        matched_tables.append(t)
                except json.JSONDecodeError:
                    continue

        # ── 搜索列 ──
        matched_columns: list[dict] = []
        columns_path = db_dir / "columns.jsonl"
        if columns_path.exists():
            for line in columns_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    c = json.loads(line)
                    if (query_lower in c["name"].lower()
                            or query_lower in c.get("comment", "").lower()
                            or query_lower in c["table"].lower()):
                        matched_columns.append(c)
                except json.JSONDecodeError:
                    continue

        if not matched_tables and not matched_columns:
            return {
                "query": query,
                "message": "未找到匹配的表或列，请先运行 database_index_ddl",
            }

        return {
            "query": query,
            "tables": matched_tables[:20],
            "columns": matched_columns[:50],
            "table_count": len(matched_tables),
            "column_count": len(matched_columns),
        }

    def _detect_risk(self, changed_files: list[str]) -> dict:
        """检测代码变更对数据库的影响。"""
        risky = []
        for f in changed_files:
            if any(kw in f.lower() for kw in ["entity", "mapper", "dao", "model", "ddl", "sql"]):
                risky.append(f)
        return {"affected": len(risky) > 0, "risky_files": risky, "message": "请 DBA Review"}

    def _generate_draft(self, description: str = "") -> dict:
        """生成 migration 草稿（不执行）。"""
        draft = (
            f"-- Migration draft (v0.1.5)\n"
            f"-- Description: {description}\n"
            f"-- ⚠️ 此文件由 Agent 生成草稿，请人工审核后执行\n"
        )
        return {"draft": draft, "warning": "⚠️ 此 SQL 不会被执行，需 DBA review 后人工执行"}


if __name__ == "__main__":
    DatabaseMCPServer().run_stdio()
