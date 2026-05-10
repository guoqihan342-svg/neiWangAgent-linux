"""
Knowledge MCP Server v0.1 — 三层预理解模型

职责：代码知识库构建与检索
v0.1: 简化版，支持基本文件索引和搜索
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


class KnowledgeMCPServer:
    """知识库 MCP Server - 三层预热模型"""

    def __init__(self):
        self.tools = {
            "knowledge_index_codebase": {
                "description": "索引代码库，构建知识库",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string", "description": "代码仓库路径"},
                        "layer": {
                            "type": "string",
                            "enum": ["summary", "hotspot", "deep"],
                            "description": "预理解层级"
                        }
                    },
                    "required": ["repo_path"]
                }
            },
            "knowledge_search": {
                "description": "搜索知识库获取上下文",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索查询"},
                        "limit": {"type": "integer", "default": 10}
                    },
                    "required": ["query"]
                }
            },
            "knowledge_get_summary": {
                "description": "获取代码库摘要（第一层）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string"}
                    },
                    "required": ["repo_path"]
                }
            },
            "knowledge_rebuild_if_stale": {
                "description": "检查并重建过期知识",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "triggers": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    }
                }
            },
        }

    def handle_request(self, method: str, params: dict | None = None) -> Any:
        if method == "tools/list":
            return [{"name": k, **v} for k, v in self.tools.items()]
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            return self._call_tool(tool_name, arguments)
        return {"error": f"Unknown method: {method}"}

    def _call_tool(self, name: str, args: dict) -> Any:
        handler = {
            "knowledge_index_codebase": self._index_codebase,
            "knowledge_search": self._search,
            "knowledge_get_summary": self._get_summary,
            "knowledge_rebuild_if_stale": self._rebuild_if_stale,
        }.get(name)

        if handler:
            try:
                result = handler(**args)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    def _index_codebase(self, repo_path: str, layer: str = "summary") -> dict:
        """索引代码库"""
        p = Path(repo_path)
        if not p.exists():
            return {"error": f"路径不存在: {repo_path}"}

        result = {"layer": layer, "files_indexed": 0, "language_stats": {}}

        if layer == "summary":
            patterns = ["*.py", "*.java", "*.vue", "*.js", "*.ts", "*.yaml", "*.yml"]
            for pattern in patterns:
                for f in p.rglob(pattern):
                    if ".git" not in str(f) and "node_modules" not in str(f):
                        result["files_indexed"] += 1
                        ext = f.suffix
                        result["language_stats"][ext] = result["language_stats"].get(ext, 0) + 1
            result["message"] = f"Summary 层索引完成: {result['files_indexed']} 个文件"

        elif layer == "hotspot":
            result["message"] = "Hotspot 层: 核心模块分析（v0.1 简化）"

        elif layer == "deep":
            result["message"] = "Deep 层: 深度索引（v0.1 简化）"

        return result

    def _search(self, query: str, limit: int = 10) -> dict:
        """搜索知识库"""
        return {
            "query": query,
            "results": [],
            "message": f"v0.1: 搜索 [{query}]，结果数 0（需 warmup 构建索引）"
        }

    def _get_summary(self, repo_path: str) -> dict:
        """获取代码库摘要"""
        return self._index_codebase(repo_path, "summary")

    def _rebuild_if_stale(self, triggers: list | None = None) -> dict:
        """检查并重建知识"""
        return {"stale": False, "rebuilt": False, "message": "知识库未过期"}


def main():
    """MCP stdio 入口"""
    server = KnowledgeMCPServer()
    print("Knowledge MCP Server started", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            req_id = request.get("id")
            method = request.get("method", "")

            if method == "initialize":
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "knowledge-mcp", "version": "0.1.0"},
                        "capabilities": {"tools": {}},
                    }
                }
            elif method == "notifications/initialized":
                continue  # 不需要响应
            else:
                result = server.handle_request(method, request.get("params", {}))
                response = {"jsonrpc": "2.0", "id": req_id, "result": result}

            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        except json.JSONDecodeError:
            continue
        except Exception as e:
            err = {"jsonrpc": "2.0", "id": req_id if req_id else None,
                   "error": {"code": -32603, "message": str(e)}}
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
