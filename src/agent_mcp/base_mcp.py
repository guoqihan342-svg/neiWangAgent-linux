"""
BaseMCPServer — MCP Server 基类

提供 stdio JSON-RPC 通信的通用实现。
所有 MCP Server 继承此类，只需定义 tools 和 handler，
无需重复编写 stdio 循环和 JSON-RPC 协议处理。

用法：
    class MyServer(BaseMCPServer):
        name = "my-mcp"
        version = "0.1.0"

        def __init__(self):
            super().__init__()
            self.tools = { ... }

        def _call_tool(self, name, args):
            ...  # 实现工具调用逻辑

    if __name__ == "__main__":
        MyServer().run_stdio()
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from typing import Any


class BaseMCPServer(ABC):
    """
    MCP Server 抽象基类。

    子类需要：
      1. 设置 name / version 类属性
      2. 在 __init__ 中定义 self.tools 字典
      3. 实现 _call_tool(name, args) 方法

    基类自动处理：
      - stdio newline-delimited JSON 读写
      - initialize / notifications/initialized 握手
      - tools/list 响应
      - JSON 解析错误处理
      - 统一异常捕获和 JSON-RPC error 响应
    """

    # 子类覆盖
    name: str = "base-mcp"
    version: str = "0.1.0"

    def __init__(self):
        self.tools: dict[str, dict] = {}

    # ── 子类必须实现 ──

    @abstractmethod
    def _call_tool(self, name: str, args: dict) -> Any:
        """
        执行工具调用。子类必须实现。

        参数：
            name: 工具名
            args: 参数字典

        返回：
            任意可 JSON 序列化的结果，或包含 content 列表的 dict。
            如果格式不是 {"content": [...]}，基类会自动包装。
        """
        ...

    # ── 可选覆盖 ──

    def handle_request(self, method: str, params: dict | None = None) -> Any:
        """
        处理任意 JSON-RPC 请求。默认支持：
          - tools/list
          - tools/call

        子类可覆盖以添加自定义方法。
        """
        if method == "tools/list":
            return [{"name": k, **v} for k, v in self.tools.items()]
        elif method == "tools/call":
            params = params or {}
            return self._call_tool(params.get("name", ""), params.get("arguments", {}))
        return {"error": f"Unknown method: {method}"}

    # ── stdio 循环 ──

    def run_stdio(self) -> None:
        """
        启动 stdio JSON-RPC 主循环。

        从 stdin 逐行读取 JSON-RPC 请求，
        处理后写回 stdout（newline-delimited JSON）。

        永不返回（直到 stdin EOF 或进程被杀死）。
        """
        req_id = None
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
                req_id = request.get("id")
                method = request.get("method", "")

                if method == "initialize":
                    # ── MCP 握手 ──
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "serverInfo": {
                                "name": self.name,
                                "version": self.version,
                            },
                            "capabilities": {"tools": {}},
                        },
                    }

                elif method == "notifications/initialized":
                    continue  # 不需要响应

                else:
                    # ── 业务方法 ──
                    result = self.handle_request(method, request.get("params", {}))
                    # 统一包装为 content 格式
                    if not isinstance(result, dict) or "content" not in result:
                        result = {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, ensure_ascii=False, default=str),
                                }
                            ]
                        }
                    response = {"jsonrpc": "2.0", "id": req_id, "result": result}

                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()

            except json.JSONDecodeError:
                continue  # 忽略格式错误的行
            except Exception as e:
                error_response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": str(e)},
                }
                try:
                    sys.stdout.write(json.dumps(error_response) + "\n")
                    sys.stdout.flush()
                except Exception:
                    pass  # 写入失败就算了，避免死循环
