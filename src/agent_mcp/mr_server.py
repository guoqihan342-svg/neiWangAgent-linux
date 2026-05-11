"""
MR MCP Server v0.1 — 创建合并请求

职责：通过 GitHub API 创建/查询 Pull Request。
★ 支持代理 + 继承 BaseMCPServer
"""

import json
import os
import sys
import time as _time
import urllib.request
import urllib.error

from agent_mcp.base_mcp import BaseMCPServer

# 从配置读取默认目标分支
try:
    from agent_mcp.config_loader import load_config
    _config = load_config()
    _DEFAULT_TARGET_BRANCH = _config.git.target_branch
except Exception:
    _DEFAULT_TARGET_BRANCH = "main"


class MRMCPServer(BaseMCPServer):
    """MR MCP Server — 继承 BaseMCPServer，只需定义 tools 和 _call_tool。"""

    name = "mr-mcp"
    version = "0.1.0"

    def __init__(self):
        super().__init__()
        self.tools = {
            "mr_create": {
                "description": "创建合并请求",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "MR 标题"},
                        "description": {"type": "string", "description": "MR 描述"},
                        "source_branch": {"type": "string", "description": "源分支"},
                        "target_branch": {
                            "type": "string",
                            "default": _DEFAULT_TARGET_BRANCH,
                            "description": "目标分支",
                        },
                        "repo": {"type": "string", "description": "owner/repo"},
                    },
                    "required": ["title", "source_branch", "repo"],
                },
            },
            "mr_list": {
                "description": "列出已创建的 MR",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    },
                    "required": ["repo"],
                },
            },
        }

    def _call_tool(self, name: str, args: dict):
        """基类要求实现的抽象方法。"""
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

    def _get_proxy_handler(self):
        proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("http_proxy")
        if proxy_url:
            return urllib.request.ProxyHandler({"https": proxy_url})
        return urllib.request.ProxyHandler({})

    def _api_request(self, url: str, method: str = "GET", data: bytes | None = None) -> dict:
        token = self._get_token()
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        if data:
            req.add_header("Content-Type", "application/json")

        proxy_handler = self._get_proxy_handler()
        opener = urllib.request.build_opener(proxy_handler)

        for attempt in range(3):
            try:
                with opener.open(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise
                if attempt < 2:
                    _time.sleep(1 * (attempt + 1))
                    continue
                err_body = json.loads(e.read())
                raise RuntimeError(f"GitHub API {e.code}: {err_body.get('message', str(e))}")
            except urllib.error.URLError as e:
                if attempt < 2:
                    _time.sleep(1 * (attempt + 1))
                    continue
                raise RuntimeError(f"GitHub API 网络错误: {e}")

    def _create(self, title: str, source_branch: str, repo: str,
                target_branch: str = "", description: str = "") -> dict:
        if not target_branch:
            target_branch = _DEFAULT_TARGET_BRANCH
        url = f"https://api.github.com/repos/{repo}/pulls"
        data = json.dumps({
            "title": title, "head": source_branch,
            "base": target_branch, "body": description,
        }).encode()
        result = self._api_request(url, method="POST", data=data)
        return {
            "url": result.get("html_url", ""),
            "number": result.get("number"),
            "created": True,
            "target_branch": target_branch,
        }

    def _list(self, repo: str, state: str = "open") -> dict:
        url = f"https://api.github.com/repos/{repo}/pulls?state={state}&per_page=10"
        try:
            pulls = self._api_request(url)
            return {
                "pulls": [{"number": p["number"], "title": p["title"], "url": p["html_url"], "state": p["state"]} for p in pulls]
            }
        except Exception as e:
            return {"error": str(e), "pulls": []}


if __name__ == "__main__":
    MRMCPServer().run_stdio()
