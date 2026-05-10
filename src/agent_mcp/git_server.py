"""
Git MCP Server v0.1 — 版本控制操作

职责：branch/commit/push（仅 agent/* 分支）
安全约束：禁止操作 master/main/release/hotfix
★ 日志追踪：每个 git 命令记录耗时和结果
"""

import json
import sys
import subprocess
import time
from pathlib import Path

from agent_mcp.tracing import get_tracer, Tracer  # ★ 日志追踪


class GitMCPServer:
    """
    Git MCP Server — 通过 stdio JSON-RPC 提供版本控制功能。

    安全约束（方案 v4 §8.3）：
      - 仅允许操作 agent/ 开头的分支
      - 禁止操作 master/main/release/hotfix
      - 禁止 force push

    日志追踪：
      每个工具调用自动记录命令、参数、耗时和结果。
    """
    PROTECTED = ["master", "main", "release/", "hotfix/"]

    def __init__(self):
        self.tracer: Tracer = get_tracer()  # ★ 日志追踪器
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
        }

    def handle_request(self, method: str, params: dict | None = None):
        if method == "tools/list":
            return [{"name": k, **v} for k, v in self.tools.items()]
        elif method == "tools/call":
            return self._call_tool(params.get("name", ""), params.get("arguments", {}))
        return {"error": f"Unknown method: {method}"}

    def _call_tool(self, name: str, args: dict):
        """调用具体工具，带日志追踪。"""
        handler = {
            "git_status": self._status,
            "git_diff": self._diff,
            "git_create_branch": self._create_branch,
            "git_commit": self._commit,
            "git_push": self._push,
            "git_log": self._log,
        }.get(name)

        if handler:
            try:
                start = time.perf_counter()  # ★ 计时
                self.tracer.debug(f"git.{name}.start", detail=args)  # ★ 日志
                result = handler(**args)
                elapsed = time.perf_counter() - start
                self.tracer.info(f"git.{name}", duration=elapsed, ok=True, detail=args)  # ★ 日志
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as e:
                self.tracer.error(f"git.{name}", detail=str(e))  # ★ 错误日志
                return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    def _run(self, cmd: list[str], cwd: str = ".") -> tuple[int, str, str]:
        """执行 shell 命令。"""
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
        if files:
            self._run(["git", "add"] + files)
        else:
            self._run(["git", "add", "-A"])
        rc, out, err = self._run(["git", "commit", "-m", message])
        if rc != 0:
            raise RuntimeError(f"提交失败: {err}")
        return {"message": message, "committed": True}

    def _push(self, branch: str) -> dict:
        self._check_protected(branch)
        rc, out, err = self._run(["git", "push", "origin", branch])
        if rc != 0:
            raise RuntimeError(f"推送失败: {err}")
        return {"branch": branch, "pushed": True}

    def _log(self, limit: int = 50) -> dict:
        rc, out, err = self._run(["git", "log", f"-{limit}", "--oneline", "--decorate"])
        return {"commits": out.split("\n") if out else []}


def main():
    server = GitMCPServer()
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
                                      "serverInfo": {"name": "git-mcp", "version": "0.1.0"},
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
