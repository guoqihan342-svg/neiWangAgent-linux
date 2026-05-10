"""
Requirement MCP Server v0.1

职责：读取和解析需求文件
"""

import json
import sys
from pathlib import Path


class RequirementMCPServer:
    def __init__(self):
        self.tools = {
            "requirement_read": {
                "description": "读取需求文件",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "需求文件路径(.md/.txt)"},
                    },
                    "required": ["path"]
                }
            },
            "requirement_parse": {
                "description": "解析需求内容为结构化数据",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "需求文本内容"},
                    },
                    "required": ["content"]
                }
            },
        }

    def handle_request(self, method: str, params: dict | None = None):
        if method == "tools/list":
            return [{"name": k, **v} for k, v in self.tools.items()]
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            return self._call_tool(tool_name, arguments)
        return {"error": f"Unknown method: {method}"}

    def _call_tool(self, name: str, args: dict):
        handler = {
            "requirement_read": self._read,
            "requirement_parse": self._parse,
        }.get(name)
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
        return {"path": str(p), "content": p.read_text(encoding="utf-8"), "size": p.stat().st_size}

    def _parse(self, content: str) -> dict:
        """简单解析需求"""
        lines = content.strip().split("\n")
        sections = {"title": "", "description": "", "tasks": []}
        current_section = "description"
        for line in lines:
            if line.startswith("# "):
                sections["title"] = line[2:].strip()
            elif line.startswith("- [ ]") or line.startswith("- "):
                sections["tasks"].append(line.strip("- [] "))
            elif line.strip():
                sections["description"] += line + "\n"
        return sections


def main():
    server = RequirementMCPServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            req_id = request.get("id")
            method = request.get("method", "")
            if method == "initialize":
                response = {"jsonrpc": "2.0", "id": req_id,
                           "result": {"protocolVersion": "2024-11-05",
                                      "serverInfo": {"name": "requirement-mcp", "version": "0.1.0"},
                                      "capabilities": {"tools": {}}}}
            elif method == "notifications/initialized":
                continue
            else:
                result = server.handle_request(method, request.get("params", {}))
                response = {"jsonrpc": "2.0", "id": req_id, "result": result}
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        except json.JSONDecodeError:
            continue
        except Exception as e:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
