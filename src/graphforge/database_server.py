"""
Database MCP Server v0.1.7 — 只读文档索引 + DDL 解析 + 只读连接验证

★ P2-14: DDL 解析做实
★ P4-20: PostgreSQL 只读连接验证（metadata_only, READ ONLY transaction）

v0.1: 不连接数据库，只做 DDL/MyBatis 文档索引
v0.2: 只读连接验证
安全红线: 永远不执行 INSERT/UPDATE/DELETE/DROP/TRUNCATE

持久化结构:
  .graphforge/knowledge/database/
    tables.jsonl   — 表定义
    columns.jsonl  — 列定义
    indexes.jsonl  — 索引定义
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import time
from pathlib import Path

from graphforge.base_mcp import BaseMCPServer
from graphforge.tracing import get_tracer, Tracer
from graphforge._version import __version__


class DatabaseMCPServer(BaseMCPServer):
    """Database MCP Server — DDL 解析 + 只读连接验证。"""

    name = "database-mcp"
    version = "0.1.7"

    def __init__(self):
        super().__init__()
        self.tracer: Tracer = get_tracer()
        self.blocked_sql_patterns = [
            r"\bDROP\s+TABLE", r"\bTRUNCATE",
            r"\bDELETE\s+FROM", r"\bINSERT\s+INTO", r"\bUPDATE\b",
            r"\bALTER\s+TABLE", r"\bCREATE\s+TABLE", r"\bGRANT\b",
        ]
        self.write_enabled = False
        self._readonly_config = self._load_readonly_config()
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
            # ★ P4-20: 只读连接验证
            "database_verify_schema": {
                "description": "连接数据库验证表结构（只读, metadata_only）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要验证的表名列表（空=所有已索引表）"},
                    },
                },
            },
        }

    # ------------------------------------------------------------------
    # ★ P4-20: 只读连接配置
    # ------------------------------------------------------------------

    @staticmethod
    def _load_readonly_config() -> dict:
        """从环境变量加载只读连接配置。"""
        return {
            "enabled": os.environ.get("DB_READONLY_ENABLED", "").lower() == "true",
            "host": os.environ.get("DB_HOST", "localhost"),
            "port": int(os.environ.get("DB_PORT", "5432")),
            "dbname": os.environ.get("DB_NAME", ""),
            "user": os.environ.get("DB_USER", ""),
            "password": os.environ.get("DB_PASSWORD", ""),
            "statement_timeout_ms": int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "5000")),
            "max_rows": int(os.environ.get("DB_MAX_ROWS", "100")),
        }

    def _verify_schema(self, tables: list[str] | None = None) -> dict:
        """
        ★ P4-20: 连接 PostgreSQL 验证表结构（只读模式）。

        安全措施：
          - SET TRANSACTION READ ONLY
          - statement_timeout 限制
          - 仅查询 information_schema（元数据）
          - 不查询任何用户表数据
          - 对比 DDL 索引报告差异
        """
        cfg = self._readonly_config
        if not cfg["enabled"]:
            return {
                "verified": False,
                "message": "只读连接未启用（设置 DB_READONLY_ENABLED=true 开启）",
            }

        try:
            psycopg2_spec = importlib.util.find_spec("psycopg2")
            if psycopg2_spec is None:
                return {"verified": False, "error": "psycopg2 未安装（pip install psycopg2-binary）"}

            import psycopg2
            import psycopg2.extras

            conn = psycopg2.connect(
                host=cfg["host"],
                port=cfg["port"],
                dbname=cfg["dbname"],
                user=cfg["user"],
                password=cfg["password"],
                connect_timeout=5,
            )
            conn.set_session(readonly=True, autocommit=True)

            cur = conn.cursor()
            cur.execute(f"SET statement_timeout = '{cfg['statement_timeout_ms']}'")

            # ── 查询 information_schema.columns ──
            if tables:
                placeholders = ",".join(["%s"] * len(tables))
                cur.execute(
                    f"""SELECT table_name, column_name, data_type, is_nullable, column_default
                       FROM information_schema.columns
                       WHERE table_schema = 'public'
                       AND table_name IN ({placeholders})
                       ORDER BY table_name, ordinal_position
                       LIMIT %s""",
                    [*tables, cfg["max_rows"] * len(tables)]
                )
            else:
                cur.execute(
                    """SELECT table_name, column_name, data_type, is_nullable, column_default
                       FROM information_schema.columns
                       WHERE table_schema = 'public'
                       ORDER BY table_name, ordinal_position
                       LIMIT %s""",
                    [cfg["max_rows"] * 50]
                )

            rows = cur.fetchall()
            cur.close()
            conn.close()

            # ── 组织结果 ──
            db_schema: dict[str, list[dict]] = {}
            for row in rows:
                tbl, col, dtype, nullable, default = row
                db_schema.setdefault(tbl, []).append({
                    "column": col,
                    "type": dtype,
                    "nullable": nullable == "YES",
                    "default": str(default) if default else None,
                })

            # ── 对比 DDL 索引 ──
            comparison = self._compare_with_ddl_index(db_schema, tables or [])

            return {
                "verified": True,
                "tables_found": list(db_schema.keys()),
                "table_count": len(db_schema),
                "total_columns": sum(len(c) for c in db_schema.values()),
                "comparison_with_ddl": comparison,
                "mode": "metadata_only (READ ONLY transaction)",
            }

        except Exception as e:
            self.tracer.error("database.verify_schema", detail=str(e))
            return {"verified": False, "error": str(e)}

    def _compare_with_ddl_index(
        self, db_schema: dict[str, list[dict]], requested_tables: list[str]
    ) -> dict:
        """★ P4-20: 对比数据库实际 schema 与 DDL 索引。"""
        db_dir = self._db_dir()
        columns_path = db_dir / "columns.jsonl"
        if not columns_path.exists():
            return {"ddl_index_available": False, "note": "先运行 database_index_ddl"}

        # 加载 DDL 索引
        ddl_columns: dict[str, list[dict]] = {}
        for line in columns_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                ddl_columns.setdefault(c["table"], []).append(c)
            except json.JSONDecodeError:
                continue

        diffs: list[dict] = []
        all_tables = set(db_schema.keys()) | set(ddl_columns.keys())

        for tbl in sorted(all_tables):
            db_cols = {c["column"]: c for c in db_schema.get(tbl, [])}
            ddl_cols = {c["name"]: c for c in ddl_columns.get(tbl, [])}

            missing_in_db = set(ddl_cols.keys()) - set(db_cols.keys())
            extra_in_db = set(db_cols.keys()) - set(ddl_cols.keys())

            if missing_in_db or extra_in_db:
                diffs.append({
                    "table": tbl,
                    "missing_in_db": list(missing_in_db),
                    "extra_in_db": list(extra_in_db),
                    "status": "MISMATCH",
                })

        return {
            "ddl_index_available": True,
            "tables_compared": len(all_tables),
            "mismatches": diffs,
            "mismatch_count": len(diffs),
        }

    def _call_tool(self, name: str, args: dict):
        handler = {
            "database_index_ddl": self._index_ddl,
            "database_search_schema": self._search_schema,
            "database_detect_risk": self._detect_risk,
            "database_generate_migration_draft": self._generate_draft,
            "database_verify_schema": self._verify_schema,
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
        d = Path(".graphforge/knowledge/database")
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
        """
        ★ P5-26: 深度数据库影响检测 — 交叉分析变更文件与 DDL 索引。

        检测策略：
          1. 检查变更文件中的 Entity/Mapper/DAO/DDL/SQL 关键词
          2. 对 Entity 文件：从 DDL 索引中查找对应表
          3. 对 Mapper XML：从 DDL 索引中查找 resultType 对应的表
          4. 对 DDL 文件：直接标记为高风险
          5. 对 SQL 文件：提取表名并交叉验证
        """
        risky_files: list[dict] = []
        affected_tables: set[str] = set()
        high_risk_keywords = {"entity", "mapper", "dao", "model", "ddl", "sql", "repository"}
        medium_risk_keywords = {"service", "controller", "handler"}

        # ── 加载 DDL 索引 ──
        ddl_tables = self._load_ddl_tables()

        for f in changed_files:
            f_lower = f.lower()
            risk_level = "none"
            matched_tables: list[str] = []

            # ── 高风险关键词检测 ──
            if any(kw in f_lower for kw in high_risk_keywords):
                risk_level = "high"
                # 从 DDL 索引查找关联表
                matched_tables = self._find_related_tables(f, ddl_tables)

            elif any(kw in f_lower for kw in medium_risk_keywords):
                risk_level = "medium"

            if risk_level != "none":
                risky_files.append({
                    "file": f,
                    "risk_level": risk_level,
                    "affected_tables": matched_tables,
                })
                affected_tables.update(matched_tables)

        # ── 生成影响报告 ──
        tables_detail = []
        for tbl in sorted(affected_tables):
            if tbl in ddl_tables:
                tables_detail.append({
                    "table": tbl,
                    "columns": ddl_tables[tbl].get("columns", []),
                    "indexes": ddl_tables[tbl].get("indexes", []),
                })

        risk_summary = (
            f"发现 {len(risky_files)} 个文件涉及数据库变更，"
            f"影响 {len(affected_tables)} 个表"
        ) if risky_files else "未检测到数据库影响"

        return {
            "affected": len(risky_files) > 0,
            "risk_summary": risk_summary,
            "risky_files": risky_files,
            "affected_tables": sorted(affected_tables),
            "tables_detail": tables_detail,
            "high_risk_count": sum(1 for r in risky_files if r["risk_level"] == "high"),
            "medium_risk_count": sum(1 for r in risky_files if r["risk_level"] == "medium"),
            "message": "请 DBA Review 变更" if risky_files else "无数据库影响",
        }

    def _load_ddl_tables(self) -> dict[str, dict]:
        """★ P5-26: 从持久化索引加载 DDL 表信息。"""
        db_dir = self._db_dir()
        tables_path = db_dir / "tables.jsonl"
        if not tables_path.exists():
            return {}
        tables: dict[str, dict] = {}
        for line in tables_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                tables[t["name"]] = t
            except json.JSONDecodeError:
                continue
        return tables

    def _find_related_tables(self, file_path: str, ddl_tables: dict[str, dict]) -> list[str]:
        """
        ★ P5-26: 从变更文件路径推断关联的数据库表。

        规则：
          - Entity/Model 文件 → 从类名推断表名（驼峰→下划线）
          - Mapper XML → 解析 resultType/parameterType
          - SQL/DDL 文件 → 提取 CREATE TABLE / ALTER TABLE 中的表名
        """
        related: list[str] = []
        pf = Path(file_path)
        name = pf.stem.lower()

        # ── Java Entity 命名规则：UserEntity → user / User → user ──
        # 去掉 Entity/Model/PO 后缀
        clean = re.sub(r"(entity|model|po|vo|dto)$", "", name, flags=re.IGNORECASE)
        # 驼峰转下划线
        snake = re.sub(r"([A-Z])", r"_\1", clean).lower().lstrip("_")

        for tbl_name in ddl_tables:
            if snake in tbl_name.lower() or tbl_name.lower() in snake:
                related.append(tbl_name)

        # ── Mapper XML 文件 → 解析内容 ──
        if pf.suffix == ".xml" and "mapper" in name:
            try:
                content = pf.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                content = ""
            # 提取 resultType 中的类名 → 映射到表
            type_matches = re.findall(
                r'(?:resultType|parameterType)="[^"]*\.(\w+)"', content
            )
            for type_name in type_matches:
                for tbl_name in ddl_tables:
                    if type_name.lower().replace("entity", "") in tbl_name.lower():
                        related.append(tbl_name)

        return list(set(related))

    def _generate_draft(self, description: str = "") -> dict:
        """
        ★ P5-29: LLM 驱动的 migration 草稿生成。

        基于数据库影响检测结果，生成 SQL migration 草稿。
        当前版本: 返回模板化草稿 + 影响分析上下文。
        """
        # ── 加载 DDL 索引提供上下文 ──
        ddl_tables = self._load_ddl_tables()
        table_summary = "\n".join(
            f"  - {name}: {len(info.get('columns', []))} columns"
            for name, info in list(ddl_tables.items())[:10]
        ) if ddl_tables else "  (DDL 索引为空，请先运行 database_index_ddl)"

        draft = (
            f"-- ============================================================\n"
            f"-- Migration Draft — GraphForge {__version__}\n"
            f"-- Description: {description}\n"
            f"-- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"-- ⚠️ 此文件由 Agent 生成草稿，请人工审核后执行\n"
            f"-- ============================================================\n\n"
            f"-- 当前数据库表概览:\n{table_summary}\n\n"
            f"-- TODO: 根据需求变更填充具体 DDL 语句\n"
            f"-- 示例:\n"
            f"-- ALTER TABLE xxx ADD COLUMN yyy TYPE;\n"
            f"-- CREATE INDEX idx_xxx ON xxx (yyy);\n\n"
            f"-- ============================================================\n"
            f"-- 安全检查清单（执行前确认）:\n"
            f"-- [ ] 已在测试环境验证\n"
            f"-- [ ] 已备份相关表\n"
            f"-- [ ] 无破坏性操作（DROP/TRUNCATE）\n"
            f"-- [ ] DBA 已 Review\n"
            f"-- ============================================================\n"
        )
        return {
            "draft": draft,
            "warning": "⚠️ 此 SQL 不会被执行，需 DBA review 后人工执行",
            "table_count": len(ddl_tables),
        }


if __name__ == "__main__":
    DatabaseMCPServer().run_stdio()
