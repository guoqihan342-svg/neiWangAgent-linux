"""
test_mcp_base.py — MCP Server 基类测试

覆盖：
  - handle_request: tools/list
  - handle_request: tools/call
  - handle_request: unknown method
"""
import json
import pytest
from graphforge.base_mcp import BaseMCPServer


class MockMCPServer(BaseMCPServer):
    """测试用 MCP Server。"""
    name = "mock-mcp"
    version = "0.1.0"

    def __init__(self):
        super().__init__()
        self.tools = {
            "echo": {
                "description": "回显参数",
                "inputSchema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            },
        }

    def _call_tool(self, name: str, args: dict):
        if name == "echo":
            return {"content": [{"type": "text", "text": args.get("message", "")}]}
        return {"content": [{"type": "text", "text": f"Unknown: {name}"}], "isError": True}


class TestBaseMCPServer:
    """handle_request 测试。"""

    def setup_method(self):
        self.server = MockMCPServer()

    def test_tools_list(self):
        result = self.server.handle_request("tools/list")
        assert len(result) == 1
        assert result[0]["name"] == "echo"

    def test_tools_call_known(self):
        result = self.server.handle_request("tools/call", {
            "name": "echo",
            "arguments": {"message": "hello"},
        })
        assert result["content"][0]["text"] == "hello"

    def test_tools_call_unknown(self):
        result = self.server.handle_request("tools/call", {
            "name": "nonexistent",
            "arguments": {},
        })
        assert result.get("isError") is True

    def test_unknown_method(self):
        result = self.server.handle_request("unknown/method")
        assert "error" in result

    def test_tools_call_no_params(self):
        result = self.server.handle_request("tools/call")
        assert "content" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
