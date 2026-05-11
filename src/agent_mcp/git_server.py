"""
Git MCP Server v0.1 — 版本控制操作

职责：branch/commit/push（仅 agent/* 分支）
安全约束：禁止操作 master/main/release/hotfix
★ 日志追踪：每个 git 命令记录耗时和结果
★ 继承 BaseMCPServer：消除重复 stdio 代码
"""

import json
import sys
import subprocess
import time
from pathlib import Path

from agent_mcp.tracing import get_tracer, Tracer
from agent_mcp.base_mcp import BaseMCPServer  # ★ 继承基类


class GitMCPServer(BaseMCPServer):
    """
    Git MCP Server — 通过 stdio JSON-RPC 提供版本控制功能。

    继承 BaseMCPServer，只需定义 tools 和 _call_tool。
    """

    name = "git-mcp"
    version = "0.1.0"
    PROTECTED = ["master", "main", "release/", "hotfix/"]

    def __init__(self):
        super().__init__()
        self.tracer: Tracer = get_tracer()
        self.tools = {
            "git_status": {
                "description": "查看工作区状态",
                "inputSchema": {"type": "object", "properties": {}}
            },
            "git_diff": {
                "description": "查看变更差异",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "target_branch": {"type": "string", "default": "master"}
                    }
                }
            },
            "git_create_branch": {
                "description": "创建 agent 分支",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "branch_name": {"type": "string"},
                    },
                    "required": ["branch_name"]
                }
            },
            "git_commit": {
                "description": "提交变更",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["message"]
                }
            },
            "git_push": {
                "description": "推送分支（仅 agent/*）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "branch": {"type": "string"},
                    },
                    "required": ["branch"]
                }
            },
            "git_log": {
                "description": "查看提交历史",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 50}
                    }
                }
            },
            "git_remote_get_url": {
                "description": "获取远程仓库 URL",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "remote_name": {"type": "string", "default": "origin"}
                    }
                }
            },
        }

    def _call_tool(self, name: str, args: dict):
        """实现基类的抽象方法。"""
        handler = {
            "git_status": self._status,
            "git_diff": self._diff,
            "git_create_branch": self._create_branch,
            "git_commit": self._commit,
            "git_push": self._push,
            "git_log": self._log,
            "git_remote_get_url": self._remote_get_url,
        }.get(name)

        if handler:
            try:
                start = time.perf_counter()
                self.tracer.debug(f"git.{name}.start", detail=args)
                result = handler(**args)
                elapsed = time.perf_counter() - start
                self.tracer.info(f"git.{name}", duration=elapsed, ok=True, detail=args)
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                self.tracer.error(f"git.{name}", detail=str(e))
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    def _run(self, cmd: list[str], cwd: str = ".") -> tuple[int, str, str]:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def _check_protected(self, branch: str) -> None:
        for p in self.PROTECTED:
            if branch == p or branch.startswith(p):
                raise ValueError(f"禁止操作受保护分支: {branch}")

    def _status(self) -> dict:
        rc, out, err = self._run(["git", "status", "--porcelain"])
        lines = out.split("\n") if out else []
        return {"clean": len(lines) == 0, "changed_files": lines}

    def _diff(self, target_branch: str = "master") -> dict:
        rc, out, err = self._run(["git", "diff", "--stat", f"origin/{target_branch}..HEAD"])
        return {"target_branch": target_branch, "diff_stat": out}

    def _create_branch(self, branch_name: str) -> dict:
        if not branch_name.startswith("agent/"):
            raise ValueError(f"分支名必须以 agent/ 开头: {branch_name}")
        rc, out, err = self._run(["git", "checkout", "-b", branch_name])
        if rc != 0:
            raise RuntimeError(f"创建分支失败: {err}")
        return {"branch": branch_name, "created": True}

    def _commit(self, message: str, files: list | None = None) -> dict:
        # ★ P1-10: 必须显式传入 files，禁止默认 git add -A（防止误提交临时文件/日志/生成物）
        if not files:
            raise ValueError("必须显式传入 files 参数，禁止默认 git add -A")
        self._run(["git", "add", "--"] + files)
        rc, out, err = self._run(["git", "commit", "-m", message])
        if rc != 0:
            raise RuntimeError(f"提交失败: {err}")
        # 提取 commit SHA
        import re
        m = re.search(r"\[[^\]]+\s+([a-f0-9]+)", out + err)
        sha = m.group(1) if m else "unknown"
        return {"message": message, "committed": True, "sha": sha, "files": files}

    def _push(self, branch: str, force: bool = False) -> dict:
        self._check_protected(branch)
        # ★ P1-10: 禁止 force push
        if force:
            raise ValueError("禁止 force push（安全策略）")
        # ★ P1-10: 分支名校验（仅允许 agent/ 开头）
        import re as _re
        if not _re.match(r"^agent/[A-Za-z0-9._/-]+$", branch):
            raise ValueError(f"分支名不符合推送策略: {branch}（必须以 agent/ 开头）")
        rc, out, err = self._run(["git", "push", "origin", branch])
        if rc != 0:
            raise RuntimeError(f"推送失败: {err}")
        return {"branch": branch, "pushed": True}

    def _log(self, limit: int = 50) -> dict:
        rc, out, err = self._run(["git", "log", f"-{limit}", "--oneline", "--decorate"])
        return {"commits": out.split("\n") if out else []}

    def _remote_get_url(self, remote_name: str = "origin") -> dict:
        """★ P3-17: 获取远程仓库 URL。"""
        rc, out, err = self._run(["git", "remote", "get-url", remote_name])
        if rc != 0:
            raise RuntimeError(f"获取 remote URL 失败: {err}")
        return {"remote": remote_name, "url": out}


if __name__ == "__main__":
    GitMCPServer().run_stdio()
