"""
MR MCP Server v0.1.4 — Provider 模式 + 创建合并请求

★ P1-7: Provider 抽象层
  - GithubMRProvider: GitHub API（自测用）
  - InternalMCPMRProvider: 公司内部 MCP（默认）
  - MockMRProvider: 测试用

职责：通过 Provider 创建/查询 Pull Request / Merge Request。
配置: mr.provider = github | internal_mcp | mock
"""
from __future__ import annotations

import json
import os
import time as _time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod

from graphforge.base_mcp import BaseMCPServer

# 从配置读取默认值
try:
    from graphforge.config_loader import load_config
    _config = load_config()
    _DEFAULT_TARGET_BRANCH = _config.git.target_branch
    _DEFAULT_PROVIDER = getattr(_config.mr, 'provider', 'internal_mcp') if hasattr(_config, 'mr') else 'internal_mcp'
except Exception:
    _DEFAULT_TARGET_BRANCH = "main"
    _DEFAULT_PROVIDER = "internal_mcp"


# ============================================================================
# ★ P1-7: MR Provider 抽象层
# ============================================================================

class MRProvider(ABC):
    """MR Provider 抽象基类 — 所有 provider 必须实现此接口。"""

    @abstractmethod
    def create_mr(self, title: str, source_branch: str, repo: str,
                  target_branch: str = "", description: str = "") -> dict:
        """创建 MR/PR，返回 {url, number, created, target_branch}。"""
        ...

    @abstractmethod
    def list_mr(self, repo: str, state: str = "open") -> dict:
        """列出 MR，返回 {pulls: [...]}。"""
        ...


class GithubMRProvider(MRProvider):
    """
    GitHub Pull Request Provider — 通过 GitHub REST API。
    需要 GITHUB_TOKEN 或 GITHUB_PAT 环境变量。
    """

    def __init__(self):
        self._proxy_url = (os.environ.get("https_proxy")
                           or os.environ.get("HTTPS_PROXY")
                           or os.environ.get("http_proxy"))

    def _get_token(self) -> str:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PAT")
        if not token:
            raise ValueError("未设置 GITHUB_TOKEN 或 GITHUB_PAT 环境变量")
        return token

    def _api_request(self, url: str, method: str = "GET", data: bytes | None = None) -> dict:
        token = self._get_token()
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        if data:
            req.add_header("Content-Type", "application/json")

        proxy_handler = (urllib.request.ProxyHandler({"https": self._proxy_url})
                         if self._proxy_url else urllib.request.ProxyHandler({}))
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

    def create_mr(self, title: str, source_branch: str, repo: str,
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

    def list_mr(self, repo: str, state: str = "open") -> dict:
        url = f"https://api.github.com/repos/{repo}/pulls?state={state}&per_page=10"
        pulls = self._api_request(url)
        return {
            "pulls": [{"number": p["number"], "title": p["title"],
                       "url": p["html_url"], "state": p["state"]} for p in pulls]
        }


class InternalMCPMRProvider(MRProvider):
    """
    公司内部 MCP MR Provider — v0.1 占位实现。
    实际接入时替换 create_mr/list_mr 为内部 API 调用。
    """

    def __init__(self):
        self._mcp_endpoint = os.environ.get("INTERNAL_MCP_URL", "")

    def create_mr(self, title: str, source_branch: str, repo: str,
                  target_branch: str = "", description: str = "") -> dict:
        """v0.1 占位：返回模拟数据。实际接入时替换为内部 API。"""
        return {
            "url": f"internal-mr://{repo}/{source_branch}",
            "number": 0,
            "created": True,
            "target_branch": target_branch or _DEFAULT_TARGET_BRANCH,
            "provider": "internal_mcp",
            "note": "v0.1 占位 — 需配置 INTERNAL_MCP_URL 接入实际平台",
        }

    def list_mr(self, repo: str, state: str = "open") -> dict:
        return {
            "pulls": [],
            "provider": "internal_mcp",
            "note": "v0.1 占位 — 需接入实际平台",
        }


class MockMRProvider(MRProvider):
    """Mock Provider — 测试用，不产生任何网络请求。"""

    def __init__(self):
        self._mr_count = 0

    def create_mr(self, title: str, source_branch: str, repo: str,
                  target_branch: str = "", description: str = "") -> dict:
        self._mr_count += 1
        return {
            "url": f"mock-mr://{repo}/mr/{self._mr_count}",
            "number": self._mr_count,
            "created": True,
            "target_branch": target_branch or _DEFAULT_TARGET_BRANCH,
            "provider": "mock",
        }

    def list_mr(self, repo: str, state: str = "open") -> dict:
        return {"pulls": [], "provider": "mock"}


# Provider 工厂
_PROVIDER_MAP = {
    "github": GithubMRProvider,
    "internal_mcp": InternalMCPMRProvider,
    "mock": MockMRProvider,
}


def get_mr_provider(provider_name: str = "") -> MRProvider:
    """根据配置创建 MR Provider 实例。"""
    name = provider_name or _DEFAULT_PROVIDER
    cls = _PROVIDER_MAP.get(name)
    if cls is None:
        raise ValueError(f"未知 MR Provider: {name}，可选: {list(_PROVIDER_MAP.keys())}")
    return cls()


# ============================================================================
# MR MCP Server — 使用 Provider 模式
# ============================================================================

class MRMCPServer(BaseMCPServer):
    """MR MCP Server — 继承 BaseMCPServer，使用 Provider 模式。"""

    name = "mr-mcp"
    version = "0.1.4"

    def __init__(self):
        super().__init__()
        self._provider = get_mr_provider()
        self.tools = {
            "mr_create": {
                "description": "创建合并请求（通过 MR Provider）",
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
        """基类要求的抽象方法。"""
        handler = {"mr_create": self._create, "mr_list": self._list}.get(name)
        if handler:
            try:
                result = handler(**args)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown: {name}"}], "isError": True}

    def _create(self, title: str, source_branch: str, repo: str,
                target_branch: str = "", description: str = "") -> dict:
        """委托给 provider 创建 MR。"""
        return self._provider.create_mr(
            title=title, source_branch=source_branch, repo=repo,
            target_branch=target_branch, description=description
        )

    def _list(self, repo: str, state: str = "open") -> dict:
        """委托给 provider 列出 MR。"""
        return self._provider.list_mr(repo=repo, state=state)


if __name__ == "__main__":
    MRMCPServer().run_stdio()
