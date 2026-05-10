"""
Clarification MCP Server v0.1 — 澄清沟通

职责：当需求不清晰时生成问题，支持人工回复后恢复执行
"""

import json
import sys
from pathlib import Path


class ClarificationMCPServer:
    def __init__(self):
        self.tools = {
            "clarification_ask": {
                "description": "向用户提问以澄清需求",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "questions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "需要澄清的问题列表"
                        },
                        "run_id": {"type": "string", "description": "运行 ID"},
                    },
                    "required": ["questions"]
                }
            },
            "clarification_get_answers": {
                "description": "获取用户的回复",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                    },
                    "required": ["run_id"]
                }
            },
            "clarification_save_answers": {
                "description": "保存用户回复",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "answers": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["run_id", "answers"]
                }
            },
        }

    def handle_request(self, method: str, params=None):
        if method == "tools/list":
            return [{"name": k, **v} for k, v in self.tools.items()]
        elif method == "tools/call":
            return self._call_tool(params.get("name", ""), params.get("arguments", {}))
        return {"error": f"Unknown method: {method}"}

    def _call_tool(self, name: str, args: dict):
        handler = {
            "clarification_ask": self._ask,
            "clarification_get_answers": self._get_answers,
            "clarification_save_answers": self._save_answers,
        }.get(name)
        if handler:
            try:
                result = handler(**args)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown: {name}"}], "isError": True}

    def _ask(self, questions: list[str], run_id: str = "") -> dict:
        qa_path = Path(f".agent/runs/{run_id}/clarification.json") if run_id else None
        data = {
            "questions": questions,
            "status": "waiting",
            "run_id": run_id or "unknown",
        }
        if qa_path:
            qa_path.parent.mkdir(parents=True, exist_ok=True)
            qa_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return data

    def _get_answers(self, run_id: str) -> dict:
        qa_path = Path(f".agent/runs/{run_id}/clarification.json")
        if qa_path.exists():
            data = json.loads(qa_path.read_text())
            if data.get("answers"):
                return {"status": "answered", "answers": data["answers"]}
            return {"status": "waiting", "questions": data.get("questions", [])}
        return {"status": "not_found", "run_id": run_id}

    def _save_answers(self, run_id: str, answers: list[str]) -> dict:
        qa_path = Path(f".agent/runs/{run_id}/clarification.json")
        qa_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"status": "answered", "answers": answers, "run_id": run_id}
        qa_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return {"saved": True, "run_id": run_id}


def main():
    server = ClarificationMCPServer()
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
                                      "serverInfo": {"name": "clarification-mcp", "version": "0.1.0"},
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
