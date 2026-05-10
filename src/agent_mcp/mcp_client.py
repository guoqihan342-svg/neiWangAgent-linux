"""
MCP (Model Context Protocol) stdio client.

Uses JSON-RPC 2.0 over stdio (newline-delimited JSON) to communicate
with MCP server subprocesses. Provides an async API for:
  - Starting/stopping MCP server processes
  - initialize handshake
  - tools/list
  - tools/call
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

MCP_PROTOCOL_VERSION = "2024-11-05"
JSONRPC_VERSION = "2.0"

# Default timeout (seconds) for request-response round-trips
DEFAULT_REQUEST_TIMEOUT = 30.0
# Default timeout for server startup (waiting for first byte / initialize response)
DEFAULT_STARTUP_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class MCPClientError(Exception):
    """Base exception for MCP client errors."""


class MCPTimeoutError(MCPClientError):
    """A request timed out."""


class MCPProtocolError(MCPClientError):
    """Protocol-level error (bad JSON, missing fields, unexpected response)."""


class MCPRPCError(MCPClientError):
    """The server returned a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"JSON-RPC error {code}: {message}")


class MCPServerError(MCPClientError):
    """Server process died or could not be started."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ToolDef:
    """Describes a single tool returned by tools/list."""

    name: str
    description: str = ""
    inputSchema: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "ToolDef":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            inputSchema=data.get("inputSchema", {}),
        )


@dataclass
class CallResult:
    """Result of a tools/call invocation."""

    content: List[Dict[str, Any]] = field(default_factory=list)
    isError: bool = False

    @property
    def text(self) -> str:
        """Concatenated text content blocks."""
        parts: List[str] = []
        for block in self.content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _make_request(method: str, req_id: int, params: Optional[Dict[str, Any]] = None) -> bytes:
    """Build a JSON-RPC 2.0 request message (newline-terminated bytes)."""
    payload: Dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "method": method,
        "id": req_id,
        "params": params or {},
    }
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _make_notification(method: str, params: Optional[Dict[str, Any]] = None) -> bytes:
    """Build a JSON-RPC 2.0 notification (no *id* field)."""
    payload: Dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "method": method,
        "params": params or {},
    }
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MCPStdioClient:
    """Async MCP client that communicates with a server over stdio.

    Typical usage::

        async with MCPStdioClient(["python", "-m", "agent_mcp.my_server"]) as client:
            info = await client.initialize()
            tools = await client.list_tools()
            result = await client.call_tool("my_tool", {"arg": "val"})
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Args:
            command: Command + args to launch the MCP server subprocess.
            request_timeout: Seconds to wait for a JSON-RPC response.
            startup_timeout: Seconds to wait for the server to become ready
                (complete the initialize handshake).
            env: Optional environment-variable overrides for the subprocess.
        """
        self._command = list(command)
        self._request_timeout = request_timeout
        self._startup_timeout = startup_timeout
        self._env = env

        # Runtime state
        self._process: Optional[asyncio.subprocess.Process] = None
        self._next_id: int = 0
        self._pending: Dict[int, asyncio.Future[Dict[str, Any]]] = {}
        self._read_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._closed: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()

        # Server metadata populated after initialize
        self.server_info: Dict[str, Any] = {}
        self.server_capabilities: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "MCPStdioClient":
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the server subprocess and begin reading its stdout."""
        if self._process is not None:
            raise MCPClientError("Client already started")

        logger.info("Starting MCP server: %s", " ".join(self._command))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
        except (OSError, ValueError) as exc:
            raise MCPServerError(f"Failed to start server process: {exc}") from exc

        # Start the reader loop
        self._read_task = asyncio.ensure_future(self._read_loop())
        # Drain stderr in background so pipes don't block
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    async def stop(self) -> None:
        """Terminate the server process and clean up resources."""
        if self._closed:
            return
        self._closed = True

        # Cancel pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPClientError("Client stopped"))
        self._pending.clear()

        # Kill process
        if self._process is not None:
            try:
                self._process.stdin.close()
            except Exception:
                pass
            try:
                self._process.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            self._process = None

        # Cancel reader / stderr tasks
        for task in (self._read_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        self._read_task = None
        self._stderr_task = None

    # ------------------------------------------------------------------
    # MCP protocol methods
    # ------------------------------------------------------------------

    async def initialize(
        self,
        client_name: str = "mcp-stdio-client",
        client_version: str = "1.0.0",
    ) -> Dict[str, Any]:
        """Perform the MCP initialize handshake.

        Returns the server's initialize result dict (serverInfo, protocolVersion, …).
        """
        params: Dict[str, Any] = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": client_name,
                "version": client_version,
            },
        }

        result = await self._call("initialize", params)
        if not isinstance(result, dict):
            raise MCPProtocolError(
                f"initialize response must be a dict, got {type(result).__name__}"
            )

        self.server_info = result.get("serverInfo", {})
        self.server_capabilities = result.get("capabilities", {})

        # Send the "initialized" notification as required by the MCP spec
        await self._send_notification("notifications/initialized")

        return result

    async def list_tools(self) -> List[ToolDef]:
        """Call ``tools/list`` and return parsed tool definitions."""
        result = await self._call("tools/list")
        if not isinstance(result, dict):
            raise MCPProtocolError(
                f"tools/list response must be a dict, got {type(result).__name__}"
            )

        raw_tools: List[Dict[str, Any]] = result.get("tools", [])
        return [ToolDef.from_raw(t) for t in raw_tools]

    async def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> CallResult:
        """Call ``tools/call`` for *name* with the given arguments.

        Returns a :class:`CallResult` that includes the content blocks
        and an error flag.
        """
        params: Dict[str, Any] = {"name": name, "arguments": arguments or {}}
        result = await self._call("tools/call", params)

        if not isinstance(result, dict):
            raise MCPProtocolError(
                f"tools/call response must be a dict, got {type(result).__name__}"
            )

        return CallResult(
            content=result.get("content", []),
            isError=bool(result.get("isError", False)),
        )

    # ------------------------------------------------------------------
    # Low-level JSON-RPC
    # ------------------------------------------------------------------

    async def _call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send a JSON-RPC 2.0 request and wait for the matching response.

        Returns the ``result`` field on success, raises :class:`MCPRPCError`
        on error responses, :class:`MCPTimeoutError` if the response does
        not arrive within *timeout* seconds.
        """
        if self._process is None or self._closed:
            raise MCPClientError("Client is not connected")

        req_id = self._get_next_id()
        fut: asyncio.Future[Dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        data = _make_request(method, req_id, params)
        timeout_s = timeout if timeout is not None else self._request_timeout

        async with self._lock:
            try:
                self._process.stdin.write(data)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._pending.pop(req_id, None)
                if not fut.done():
                    fut.cancel()
                raise MCPServerError("Server process exited unexpectedly") from exc

        try:
            response = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            raise MCPTimeoutError(
                f"Request '{method}' (id={req_id}) timed out after {timeout_s}s"
            )

        if "error" in response:
            err = response["error"]
            raise MCPRPCError(
                code=err.get("code", -1),
                message=err.get("message", "Unknown error"),
                data=err.get("data"),
            )

        return response.get("result")

    async def _send_notification(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._process is None or self._closed:
            raise MCPClientError("Client is not connected")

        data = _make_notification(method, params)
        async with self._lock:
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _read_loop(self) -> None:
        """Read newline-delimited JSON from the server's stdout forever."""
        assert self._process is not None and self._process.stdout is not None

        buf = b""
        while not self._closed:
            try:
                chunk = await self._process.stdout.read(4096)
            except (OSError, ValueError):
                break

            if not chunk:
                # EOF – server exited
                logger.warning("MCP server stdout closed (EOF)")
                break

            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError as exc:
                        logger.warning("Failed to parse JSON from server: %s", exc)
                        continue
                    self._dispatch(msg)

        # Server exited; fail any still-pending futures
        if not self._closed:
            self._closed = True
            for rid, fut in self._pending.items():
                if not fut.done():
                    fut.set_exception(MCPServerError("Server process exited"))
            self._pending.clear()

    async def _drain_stderr(self) -> None:
        """Read stderr and log it, preventing pipe buffer blocking."""
        assert self._process is not None and self._process.stderr is not None
        while not self._closed:
            try:
                chunk = await self._process.stderr.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    logger.info("[server stderr] %s", line.strip())

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message to a pending future (or log it)."""
        # JSON-RPC responses always carry an "id" (number or string).
        # Notifications from server to client have no "id".
        msg_id = msg.get("id")
        if msg_id is not None and isinstance(msg_id, int):
            fut = self._pending.pop(msg_id, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            else:
                logger.debug("Received response for unknown id=%s", msg_id)
        elif msg.get("method") is not None:
            # Server-to-client request or notification – not handled here
            logger.debug("Ignoring server-initiated message: %s", msg.get("method"))
        else:
            logger.debug("Unrecognised message: %s", msg)


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------

async def mcp_connect(
    command: Sequence[str],
    client_name: str = "mcp-stdio-client",
    client_version: str = "1.0.0",
    *,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    env: Optional[Dict[str, str]] = None,
) -> MCPStdioClient:
    """Create, start, and initialize an :class:`MCPStdioClient` in one call.

    Returns a fully-initialized client.  The caller is responsible for
    calling ``await client.stop()`` (or using ``async with``) when done.

    This is the recommended entrypoint for quick usage.
    """
    client = MCPStdioClient(
        command,
        request_timeout=request_timeout,
        startup_timeout=startup_timeout,
        env=env,
    )
    await client.start()
    try:
        await asyncio.wait_for(
            client.initialize(client_name, client_version),
            timeout=startup_timeout,
        )
    except Exception:
        await client.stop()
        raise
    return client
