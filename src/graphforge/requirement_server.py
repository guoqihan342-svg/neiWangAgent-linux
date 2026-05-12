"""
Requirement MCP Server v0.1

职责：读取和解析需求文件
★ 继承 BaseMCPServer
"""

import json
import sys
from pathlib import Path

from graphforge.base_mcp import BaseMCPServer


class RequirementMCPServer(BaseMCPServer):
    """Requirement MCP Server — 继承 BaseMCPServer。"""

    name = "requirement-mcp"
    version = "0.1.0"

    def __init__(self):
        super().__init__()
        self.tools = {
            "requirement_read": {
                "description": "读取需求文件",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "需求文件路径(.md/.txt)"},
                    },
                    "required": ["path"],
                },
            },
            "requirement_parse": {
                "description": "解析需求内容为结构化数据",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "需求文本内容"},
                    },
                    "required": ["content"],
                },
            },
        }

    def _call_tool(self, name: str, args: dict):
        handler = {"requirement_read": self._read, "requirement_parse": self._parse}.get(name)
        if handler:
            try:
                result = handler(**args)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    def _read(self, path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"error": f"文件不存在: {path}"}
        return {
            "path": str(p),
            "content": p.read_text(encoding="utf-8"),
            "size": p.stat().st_size,
        }

    def _parse(self, content: str) -> dict:
        """简单解析需求为结构化数据。"""
        lines = content.strip().split("\n")
        sections = {"title": "", "description": "", "tasks": []}
        for line in lines:
            if line.startswith("# "):
                sections["title"] = line[2:].strip()
            elif line.startswith("- [ ]") or line.startswith("- "):
                sections["tasks"].append(line.strip("- [] "))
            elif line.strip():
                sections["description"] += line + "\n"
        return sections


if __name__ == "__main__":
    RequirementMCPServer().run_stdio()
