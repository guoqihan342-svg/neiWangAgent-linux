"""
MR MCP Server v0.1 — 创建合并请求

职责：创建 MR（v0.1: GitHub API）
"""

import json
import os
import sys
import urllib.request
import urllib.error


class MRMCPServer:
    def __init__(self):
        self.tools = {
            "mr_create": {
                "description": "创建合并请求",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "source_branch": {"type": "string"},
                        "target_branch": {"type": "string", "default": "master"},
                        "repo": {"type": "string", "description": "owner/repo"},
                    },
                    "required": ["title", "source_branch", "repo"]
                }
            },
            "mr_list": {
                "description": "列出已创建的 MR",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    },
                    "required": ["repo"]
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
        handler = {"mr_create": self._create, "mr_list": self._list}.get(name)
        if handler:
            try:
                result = handler(**args)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown: {name}"}], "isError": True}

    def _get_token(self) -> str:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PAT")
        if not token:
            raise ValueError("未设置 GITHUB_TOKEN 或 GITHUB_PAT 环境变量")
        return token

    def _create(self, title: str, source_branch: str, repo: str,
                target_branch: str = "master", description: str = "") -> dict:
        token = self._get_token()
        url = f"https://api.github.com/repos/{repo}/pulls"
        data = json.dumps({
            "title": title,
            "head": source_branch,
            "base": target_branch,
            "body": description,
        }).encode()

        req = urllib.request.Request(url, method="POST", data=data)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                return {"url": result.get("html_url", ""), "number": result.get("number"), "created": True}
        except urllib.error.HTTPError as e:
            err = json.loads(e.read())
            return {"error": err.get("message", str(e)), "created": False}

    def _list(self, repo: str, state: str = "open") -> dict:
        token = self._get_token()
        url = f"https://api.github.com/repos/{repo}/pulls?state={state}&per_page=10"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                pulls = json.loads(resp.read())
                return {"pulls": [{"number": p["number"], "title": p["title"], "url": p["html_url"]} for p in pulls]}
        except Exception as e:
            return {"error": str(e), "pulls": []}


def main():
    server = MRMCPServer()
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
                                      "serverInfo": {"name": "mr-mcp", "version": "0.1.0"},
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
