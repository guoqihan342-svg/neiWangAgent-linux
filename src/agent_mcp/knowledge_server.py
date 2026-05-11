"""
Knowledge MCP Server v0.1 — 三层预理解模型 + 多语言支持

职责：代码知识库构建与检索

三层模型（方案 v4 §4.1）：
  1. Summary 层 — 项目结构概览（文件列表、模块树）
  2. Hotspot 层 — 热点模块分析（频繁变更的文件、核心模块）
  3. Deep 层    — 深度代码索引（函数签名、类结构、依赖关系）

多语言支持：
  - Java:      识别 @Controller/@Service/@Repository、MyBatis XML
  - Python:    识别 FastAPI route、Django view、SQLAlchemy model
  - Go:        识别 gin.HandlerFunc、struct、interface
  - TypeScript:识别 Next.js page、Express route、React component
  - Vue:       识别 .vue SFC、Vue Router
  - Generic:   通用文件扫描

v0.1 简化版：支持基本文件索引、语言检测和搜索。
v0.2 计划：AST 解析、函数签名提取、调用图构建。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from agent_mcp.tracing import get_tracer, Tracer  # ★ 日志追踪
from agent_mcp.base_mcp import BaseMCPServer  # ★ 继承基类


# =============================================================================
# 多语言文件识别规则
# =============================================================================

# 每种语言的文件扩展名 → 文件类型
LANGUAGE_EXTENSIONS: dict[str, str] = {
    # Java / JVM
    ".java": "java",
    ".kt": "kotlin",
    ".scala": "scala",
    ".groovy": "groovy",
    ".xml": "xml",          # MyBatis Mapper / Spring config
    ".properties": "config",
    # Python
    ".py": "python",
    ".pyi": "python_stub",
    # Go
    ".go": "go",
    ".mod": "go_mod",
    # TypeScript / JavaScript / Vue
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".vue": "vue",
    ".svelte": "svelte",
    # Config / Docs
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".txt": "text",
    ".sql": "sql",
    # Build
    ".gradle": "gradle",
    ".pom": "maven",
    ".lock": "lockfile",
    # Docker / CI
    ".dockerfile": "docker",
    ".gitlab-ci.yml": "ci",
    ".github": "ci",
}

# 各语言的核心代码模式（用于 hotspot 分析）
LANGUAGE_PATTERNS: dict[str, dict[str, list[str]]] = {
    "java": {
        "controllers": ["@RestController", "@Controller", "@RequestMapping"],
        "services": ["@Service", "@Component"],
        "repositories": ["@Repository", "extends JpaRepository", "extends CrudRepository"],
        "entities": ["@Entity", "@Table", "@TableName"],
        "configs": ["@Configuration", "@Bean"],
    },
    "python": {
        "routes": ["@app.route", "@router.get", "@router.post", "APIRouter"],
        "models": ["class Base(", "class Model(", "Table(", "Column("],
        "services": ["class Service", "def service_"],
        "schemas": ["class Schema", "BaseModel"],
    },
    "go": {
        "handlers": ["gin.HandlerFunc", "echo.HandlerFunc", "http.HandlerFunc", "func (h *Handler)"],
        "models": ["type struct {", "gorm.Model"],
        "routes": [".GET(", ".POST(", ".PUT(", ".DELETE(", ".Group("],
    },
    "typescript": {
        "components": ["export default function", "export const ", "React.FC", "React.FunctionComponent"],
        "routes": ["export async function GET", "export async function POST", "app.route"],
        "models": ["interface ", "type ", "z.object("],
    },
    "vue": {
        "components": ["<template>", "<script setup>", "export default {"],
        "routes": ["path:", "component:", "createRouter"],
        "stores": ["defineStore", "useStore"],
    },
}


class KnowledgeMCPServer(BaseMCPServer):
    """
    知识库 MCP Server — 三层预热模型 + 多语言支持。

    继承 BaseMCPServer。

    功能：
      - knowledge_index_codebase: 索引代码库（按层级）
      - knowledge_search:         搜索知识库
      - knowledge_stats:          获取索引统计
      - knowledge_language_detect: 检测项目语言类型
    """

    name = "knowledge-mcp"
    version = "0.1.0"

    def __init__(self):
        super().__init__()
        self.tracer: Tracer = get_tracer()  # ★ 日志追踪器

        self.tools = {
            "knowledge_index_codebase": {
                "description": "索引代码库，构建知识库（支持 summary/hotspot/deep 三层）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {
                            "type": "string",
                            "description": "代码仓库路径（绝对路径）",
                        },
                        "layer": {
                            "type": "string",
                            "enum": ["summary", "hotspot", "deep"],
                            "description": "预理解层级：summary=项目概览, hotspot=热点分析, deep=深度索引",
                        },
                        "source_extensions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要索引的源文件扩展名列表，如 ['.java', '.py', '.go']",
                        },
                    },
                    "required": ["repo_path", "layer"],
                },
            },
            "knowledge_search": {
                "description": "在知识库中搜索",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词或正则表达式",
                        },
                        "file_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "限定文件类型，如 ['java', 'python']",
                        },
                    },
                    "required": ["query"],
                },
            },
            "knowledge_stats": {
                "description": "获取知识库索引统计",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "knowledge_language_detect": {
                "description": "自动检测项目的主要编程语言",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {
                            "type": "string",
                            "description": "代码仓库路径",
                        },
                    },
                    "required": ["repo_path"],
                },
            },
        }

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def _call_tool(self, name: str, args: dict):
        """基类要求的抽象方法 — 分派工具调用。"""
        handler = {
            "knowledge_index_codebase": self._index_codebase,
            "knowledge_search": self._search,
            "knowledge_stats": self._stats,
            "knowledge_language_detect": self._detect_language,
        }.get(name)

        if handler:
            try:
                start = time.perf_counter()
                self.tracer.debug(f"knowledge.{name}.start", detail=args)
                result = handler(**args)
                elapsed = time.perf_counter() - start
                self.tracer.info(f"knowledge.{name}", duration=elapsed, ok=True)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                self.tracer.error(f"knowledge.{name}", detail=str(e))
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _index_codebase(
        self,
        repo_path: str,
        layer: str = "summary",
        source_extensions: list[str] | None = None,
    ) -> dict:
        """
        索引代码库（三层预热模型）。

        参数：
            repo_path:          代码仓库绝对路径
            layer:              预理解层级
            source_extensions:  源文件扩展名列表

        返回：
            dict: 索引结果，包含文件数、语言分布、层级详情
        """
        rp = Path(repo_path)
        if not rp.exists():
            return {"error": f"路径不存在: {repo_path}"}

        # ── 默认扩展名 ──
        if source_extensions is None:
            source_extensions = list(LANGUAGE_EXTENSIONS.keys())

        # ── 收集文件 ──
        files: list[Path] = []
        exclude_dirs = {
            ".git", "__pycache__", "node_modules", ".venv", "venv",
            "target", "build", "dist", ".next", ".turbo", ".cache",
            ".idea", ".vscode", ".agent",
        }

        for f in rp.rglob("*"):
            if f.is_file() and not any(d in f.parts for d in exclude_dirs):
                if f.suffix in source_extensions or any(
                    f.name.endswith(ext) for ext in source_extensions
                ):
                    files.append(f)

        # ── 语言分布统计 ──
        lang_dist: dict[str, int] = {}
        for f in files:
            lang = self._classify_file(f)
            lang_dist[lang] = lang_dist.get(lang, 0) + 1

        # ── 按层级构建结果 ──
        if layer == "summary":
            result = self._build_summary(files, lang_dist, rp)
        elif layer == "hotspot":
            result = self._build_hotspot(files, lang_dist, rp)
        elif layer == "deep":
            result = self._build_deep(files, lang_dist, rp)
        else:
            result = {"error": f"未知层级: {layer}"}

        result["files_indexed"] = len(files)
        result["language_distribution"] = lang_dist
        result["layer"] = layer

        # ★ P1-8: 持久化知识库到 .agent/knowledge/
        self._persist_knowledge(result, files, rp, layer)

        return result

    # ------------------------------------------------------------------
    # ★ P1-8: 持久化
    # ------------------------------------------------------------------

    @staticmethod
    def _kb_dir() -> Path:
        """获取知识库持久化目录。"""
        kb = Path(".agent/knowledge")
        kb.mkdir(parents=True, exist_ok=True)
        return kb

    def _persist_knowledge(self, result: dict, files: list[Path],
                           root: Path, layer: str):
        """
        ★ P1-8: 将索引结果持久化到磁盘。

        持久化结构：
          .agent/knowledge/summary.json    — Summary 层结果
          .agent/knowledge/hotspot.json    — Hotspot 层结果
          .agent/knowledge/deep_index.jsonl — Deep 层索引（每行一个文件）
          .agent/knowledge/files_index.jsonl — 所有文件元数据索引
        """
        kb = self._kb_dir()

        # ── 保存层级结果 ──
        layer_file = kb / f"{layer}.json"
        layer_file.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8"
        )

        # ── 构建并覆盖 files_index.jsonl — 所有文件的元数据 ──
        files_idx = kb / "files_index.jsonl"
        lines: list[str] = []
        for f in files:
            try:
                rel = str(f.relative_to(root))
            except ValueError:
                rel = str(f)
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                line_count = len(content.splitlines())
            except Exception:
                content = ""
                line_count = 0
            entry = {
                "path": rel,
                "abs_path": str(f),
                "language": self._classify_file(f),
                "mtime": f.stat().st_mtime,
                "size": f.stat().st_size,
                "lines": line_count,
            }
            lines.append(json.dumps(entry, ensure_ascii=False))

        files_idx.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _load_files_index(self) -> list[dict]:
        """★ P1-8: 加载文件索引。"""
        idx_path = self._kb_dir() / "files_index.jsonl"
        if not idx_path.exists():
            return []
        entries: list[dict] = []
        for line in idx_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def _build_summary(
        self, files: list[Path], lang_dist: dict, root: Path
    ) -> dict:
        """
        Summary 层：项目结构概览。

        输出：
          - 文件总数、按语言分布
          - 目录结构树（前 3 级）
          - 主要语言（按文件数排序）
        """
        # ── 目录树（前 3 级） ──
        tree: dict[str, Any] = {}
        for f in files:
            try:
                rel = f.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts[:3]  # 只取前 3 级
            node = tree
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            # 最后一级是文件名
            node[parts[-1]] = self._classify_file(f)

        # ── 主要语言 ──
        primary_lang = max(lang_dist, key=lang_dist.get) if lang_dist else "unknown"

        return {
            "project_structure": tree,
            "primary_language": primary_lang,
            "top_languages": sorted(lang_dist.items(), key=lambda x: -x[1])[:5],
            "message": f"Summary 层: {len(files)} 个文件, 主要语言: {primary_lang}",
        }

    def _build_hotspot(
        self, files: list[Path], lang_dist: dict, root: Path
    ) -> dict:
        """
        Hotspot 层：热点模块分析。

        输出：
          - 每个语言的核心文件分类（controller/service/model 等）
          - 最近修改的文件（按 mtime）
          - 大型文件（>500行）
        """
        categorized: dict[str, dict[str, list[str]]] = {}
        large_files: list[dict] = []
        recent_files: list[dict] = []

        for f in files:
            try:
                rel = str(f.relative_to(root))
            except ValueError:
                rel = str(f)

            # ── 按模式分类 ──
            lang = self._classify_file(f)
            if lang in LANGUAGE_PATTERNS:
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                for category, patterns in LANGUAGE_PATTERNS[lang].items():
                    for pattern in patterns:
                        if pattern in content:
                            categorized.setdefault(lang, {}).setdefault(
                                category, []
                            ).append(rel)
                            break  # 每个文件只归入第一个匹配类别

            # ── 大型文件（>500行） ──
            try:
                line_count = len(f.read_text(encoding="utf-8", errors="ignore").splitlines())
            except Exception:
                line_count = 0
            if line_count > 500:
                large_files.append({"path": rel, "lines": line_count, "language": lang})

            # ── 最近修改 ──
            mtime = f.stat().st_mtime
            recent_files.append({"path": rel, "mtime": mtime})

        # 最近修改的 top 10
        recent_files.sort(key=lambda x: -x["mtime"])
        recent_top10 = [r["path"] for r in recent_files[:10]]

        return {
            "categorized": categorized,
            "large_files": large_files,
            "recent_files": recent_top10,
            "message": f"Hotspot 层: {len(categorized)} 种语言, "
                       f"{len(large_files)} 个大文件, "
                       f"{len(recent_top10)} 个近期变更",
        }

    def _build_deep(
        self, files: list[Path], lang_dist: dict, root: Path
    ) -> dict:
        """
        Deep 层：深度代码索引。

        ★ P3-18: 新增 Java/MyBatis Mapper→Entity 映射 + Vue 组件/路由分析

        输出：
          - import/dependency 提取
          - 函数/类定义计数
          - Mapper→Entity 映射（Java/MyBatis）
          - Vue 组件树 + 路由分析
          - 文件间引用关系
        """
        imports: dict[str, list[str]] = {}
        definitions: dict[str, int] = {}

        import_patterns = {
            "java": r"^import\\s+([\\w.]+)",
            "python": r"^(?:from|import)\\s+([\\w.]+)",
            "go": r'\"([^\"]+)\"',
            "typescript": r"^(?:import\\s+.*?from\\s+['\\\"]|require\\()['\\\"]([^'\\\"]+)",
        }

        for f in files:
            try:
                rel = str(f.relative_to(root))
            except ValueError:
                rel = str(f)
            lang = self._classify_file(f)

            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            pattern = import_patterns.get(lang)
            if pattern:
                matches = re.findall(pattern, content, re.MULTILINE)
                if matches:
                    imports[rel] = list(set(matches))

            def_count = 0
            if lang in ("java", "typescript", "go"):
                def_count += len(re.findall(r"^\\s*(?:public\\s+)?(?:class|interface|enum)\\s+", content, re.MULTILINE))
            if lang == "python":
                def_count += len(re.findall(r"^\\s*(?:def|class|async def)\\s+", content, re.MULTILINE))
            if lang in ("typescript", "vue"):
                def_count += len(re.findall(r"^\\s*(?:export\\s+)?(?:function|const|class)\\s+", content, re.MULTILINE))

            definitions[rel] = def_count

        # ★ P3-18: MyBatis Mapper → Entity 映射
        mapper_entity = self._build_mapper_entity_mapping(files, root)

        # ★ P3-18: Vue 组件树 + 路由
        vue_analysis = self._build_vue_component_tree(files, root)

        return {
            "imports": imports,
            "definitions": definitions,
            "total_definitions": sum(definitions.values()),
            "mapper_entity_mapping": mapper_entity,
            "vue_analysis": vue_analysis,
            "message": f"Deep 层: {len(imports)} 个文件有依赖, "
                       f"{sum(definitions.values())} 个定义, "
                       f"{len(mapper_entity)} 个 Mapper→Entity 映射, "
                       f"{len(vue_analysis.get('components', []))} 个 Vue 组件",
        }

    # ------------------------------------------------------------------
    # ★ P3-18: Java/MyBatis Mapper → Entity 映射
    # ------------------------------------------------------------------

    def _build_mapper_entity_mapping(
        self, files: list[Path], root: Path
    ) -> dict[str, dict]:
        """
        分析 MyBatis Mapper XML 与 Java Entity 的映射关系。

        策略：
          1. 找到所有 *Mapper.xml 文件
          2. 解析 <resultMap> / <select> / <insert> 中的 parameterType / resultType
          3. 匹配到 Java Entity 类
        """
        mappings: dict[str, dict] = {}
        entity_classes: dict[str, Path] = {}

        # ── 先收集所有 Java Entity ──
        for f in files:
            if f.suffix == ".java":
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                # 匹配 @Entity / @Table / @TableName 注解
                if re.search(r"@(?:Entity|Table|TableName)\\b", content):
                    try:
                        rel = str(f.relative_to(root))
                    except ValueError:
                        rel = str(f)
                    # 提取类名
                    m = re.search(r"class\\s+(\\w+)", content)
                    if m:
                        entity_classes[m.group(1)] = Path(rel)

        # ── 解析 Mapper XML ──
        for f in files:
            if not f.name.endswith("Mapper.xml") and not f.name.endswith(".xml"):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            try:
                rel = str(f.relative_to(root))
            except ValueError:
                rel = str(f)

            # 提取 namespace
            ns_match = re.search(r'<mapper\\s+namespace=\"([^\"]+)\"', content)
            namespace = ns_match.group(1) if ns_match else ""

            # 提取所有 resultType / parameterType 引用
            type_refs = set()
            for type_match in re.finditer(
                r'(?:resultType|parameterType|resultMap)=\"([^\"]+)\"',
                content
            ):
                type_name = type_match.group(1)
                # 去掉包名前缀，只保留类名
                simple_name = type_name.split(".")[-1] if "." in type_name else type_name
                type_refs.add(simple_name)

            # ── 匹配 Entity ──
            mapped_entities = []
            for ref in type_refs:
                if ref in entity_classes:
                    mapped_entities.append({
                        "entity_class": ref,
                        "entity_file": str(entity_classes[ref]),
                    })

            if mapped_entities:
                mappings[rel] = {
                    "mapper_file": rel,
                    "namespace": namespace,
                    "entities": mapped_entities,
                    "type_references": list(type_refs),
                }

        return mappings

    # ------------------------------------------------------------------
    # ★ P3-18: Vue 组件树 + 路由分析
    # ------------------------------------------------------------------

    def _build_vue_component_tree(
        self, files: list[Path], root: Path
    ) -> dict:
        """
        分析 Vue SFC 组件结构和路由定义。

        策略：
          1. 找到所有 .vue 文件
          2. 解析 <script setup> 中的组件名和 props
          3. 找到 router 配置文件，提取路由表
        """
        components: list[dict] = []
        routes: list[dict] = []

        for f in files:
            try:
                rel = str(f.relative_to(root))
            except ValueError:
                rel = str(f)

            # ── Vue SFC 组件 ──
            if f.suffix == ".vue":
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                comp_info: dict = {"file": rel}

                # 提取组件名（从 <script setup> 或 export default）
                name_match = re.search(
                    r"(?:name\\s*:\\s*['\\\"]([^'\\\"]+)['\\\"]|defineComponent\\s*\\(\\s*\\{\\s*name\\s*:\\s*['\\\"]([^'\\\"]+))",
                    content
                )
                if name_match:
                    comp_info["name"] = name_match.group(1) or name_match.group(2)

                # 提取 props
                props_match = re.search(r"(?:props\\s*:\\s*\\{|defineProps\\s*\\(\\s*\\{)", content)
                comp_info["has_props"] = bool(props_match)

                # 提取 emits
                emits_match = re.search(r"(?:emits\\s*:\\s*\\[|defineEmits\\s*\\()", content)
                comp_info["has_emits"] = bool(emits_match)

                # 是否有 <template>
                comp_info["has_template"] = "<template>" in content

                components.append(comp_info)

            # ── Vue Router 配置 ──
            if f.name in ("router.js", "router.ts", "routes.js", "routes.ts"):
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                # 提取路由定义
                for route_match in re.finditer(
                    r"\\{\\s*path\\s*:\\s*['\\\"]([^'\\\"]+)['\\\"].*?component\\s*:\\s*(\\w+)",
                    content, re.DOTALL
                ):
                    routes.append({
                        "path": route_match.group(1),
                        "component": route_match.group(2),
                        "router_file": rel,
                    })

                # 提取嵌套路由
                for child_match in re.finditer(
                    r"path\\s*:\\s*['\\\"]([^'\\\"]+)['\\\"].*?component\\s*:\\s*\\s*\\(\\)\\s*=>\\s*import\\s*\\(['\\\"]([^'\\\"]+)",
                    content
                ):
                    routes.append({
                        "path": child_match.group(1),
                        "component": child_match.group(2).split("/")[-1],
                        "router_file": rel,
                        "lazy_loaded": True,
                    })

        return {
            "components": components,
            "component_count": len(components),
            "routes": routes,
            "route_count": len(routes),
        }

    def _search(
        self,
        query: str,
        file_types: list[str] | None = None,
    ) -> dict:
        """
        ★ P1-8: 搜索知识库 — 从持久化索引中检索。

        支持：
          - 按文件名搜索（query 匹配文件名）
          - 按路径搜索（query 匹配文件路径）
          - 按关键词 grep 搜索（在已索引文件中 grep）
          - 按语言过滤（file_types）
          - 按最近修改时间排序

        返回：
            dict: {query, file_types, matches: [{path, language, mtime, lines, matched_line?}]}
        """
        entries = self._load_files_index()
        if not entries:
            return {
                "query": query,
                "file_types": file_types,
                "matches": [],
                "total_files": 0,
                "message": "知识库未索引，请先运行 agent warmup",
            }

        query_lower = query.lower()
        matches: list[dict] = []

        for entry in entries:
            # ── 语言过滤 ──
            if file_types and entry["language"] not in file_types:
                continue

            path_lower = entry["path"].lower()
            name = Path(entry["path"]).name.lower()

            # ── 文件名匹配 ──
            name_match = query_lower in name

            # ── 路径匹配 ──
            path_match = query_lower in path_lower

            # ── 关键词 grep（在文件内容中搜索） ──
            content_match = None
            if not name_match and not path_match and query_lower:
                try:
                    content = Path(entry["abs_path"]).read_text(
                        encoding="utf-8", errors="ignore"
                    )
                    lines = content.splitlines()
                    for i, line in enumerate(lines, 1):
                        if query_lower in line.lower():
                            content_match = {
                                "line_number": i,
                                "line_text": line[:200],
                            }
                            break
                except Exception:
                    pass

            if name_match or path_match or content_match:
                match = {
                    "path": entry["path"],
                    "language": entry["language"],
                    "mtime": entry["mtime"],
                    "lines": entry["lines"],
                    "match_type": (
                        "filename" if name_match else
                        "path" if path_match else
                        "content"
                    ),
                }
                if content_match:
                    match["matched_line"] = content_match
                matches.append(match)

        # ── 按最近修改时间排序 ──
        matches.sort(key=lambda m: -m["mtime"])

        return {
            "query": query,
            "file_types": file_types,
            "matches": matches[:50],  # 最多返回 50 个
            "total_files": len(entries),
            "total_matches": len(matches),
        }

    def _stats(self) -> dict:
        """获取知识库统计。"""
        return {
            "version": "0.1.0",
            "layers": ["summary", "hotspot", "deep"],
            "supported_languages": list(LANGUAGE_PATTERNS.keys()),
            "supported_extensions": list(LANGUAGE_EXTENSIONS.keys()),
        }

    def _detect_language(self, repo_path: str) -> dict:
        """
        自动检测项目的主要编程语言。

        通过统计文件扩展名分布来判断。

        返回：
            dict: 包含 primary_language、language_distribution、confidence
        """
        rp = Path(repo_path)
        if not rp.exists():
            return {"error": f"路径不存在: {repo_path}"}

        lang_count: dict[str, int] = {}
        exclude_dirs = {
            ".git", "__pycache__", "node_modules", ".venv", "venv",
            "target", "build", "dist", ".next",
        }

        for f in rp.rglob("*"):
            if f.is_file() and not any(d in f.parts for d in exclude_dirs):
                lang = LANGUAGE_EXTENSIONS.get(f.suffix, "other")
                lang_count[lang] = lang_count.get(lang, 0) + 1

        total = sum(lang_count.values())
        if total == 0:
            return {"primary_language": "unknown", "confidence": 0}

        primary = max(lang_count, key=lang_count.get)
        confidence = lang_count[primary] / total

        # ── 映射到项目类型建议 ──
        type_suggestion = self._suggest_project_type(lang_count)

        return {
            "primary_language": primary,
            "confidence": round(confidence, 2),
            "language_distribution": dict(
                sorted(lang_count.items(), key=lambda x: -x[1])[:10]
            ),
            "total_files": total,
            "suggested_project_type": type_suggestion,
        }

    def _suggest_project_type(self, lang_count: dict[str, int]) -> str:
        """根据语言分布建议项目类型。"""
        total = sum(lang_count.values())
        if total == 0:
            return "generic"

        # Java 为主
        java_ratio = (lang_count.get("java", 0) + lang_count.get("xml", 0)) / total
        if java_ratio > 0.3:
            return "java"

        # Python 为主
        python_ratio = lang_count.get("python", 0) / total
        if python_ratio > 0.3:
            return "python"

        # Go 为主
        go_ratio = lang_count.get("go", 0) / total
        if go_ratio > 0.3:
            return "go"

        # TypeScript/JS 为主
        ts_ratio = (
            lang_count.get("typescript", 0)
            + lang_count.get("tsx", 0)
            + lang_count.get("javascript", 0)
            + lang_count.get("jsx", 0)
            + lang_count.get("vue", 0)
        ) / total
        if ts_ratio > 0.3:
            return "typescript"

        return "generic"

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_file(filepath: Path) -> str:
        """
        根据文件扩展名和内容特征分类文件。

        返回语言标识符（如 'java', 'python', 'vue'）。
        """
        # ── 特殊文件名识别 ──
        name = filepath.name.lower()
        if name == "dockerfile":
            return "docker"
        if name in ("makefile", "makefile.am", "makefile.in"):
            return "makefile"

        # ── 扩展名识别 ──
        ext = filepath.suffix.lower()
        return LANGUAGE_EXTENSIONS.get(ext, "other")



if __name__ == "__main__":
    KnowledgeMCPServer().run_stdio()
