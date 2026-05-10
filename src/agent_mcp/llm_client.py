"""OpenAI-compatible LLM client for neiWangAgent.

Uses httpx for HTTP calls. Supports chat completion with tool calling,
configurable base URL / model via AppConfig, and structured prompting with a
system prompt that describes the agent's state-machine role.

API key is read from the DEEPSEEK_API_KEY environment variable.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from agent_mcp.config_loader import AppConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — describes the agent's role and state-machine flow
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are neiWangAgent, an autonomous coding agent that follows a strict state-machine flow.

States: UNDERSTANDING → PLANNING → IMPLEMENTING → VERIFYING → COMMITTING → CREATING_MR

Your responsibilities at each state:
- UNDERSTANDING: Analyze requirements, read existing code, ask clarifying questions.
- PLANNING: Produce a concrete implementation plan with file paths and change details.
- IMPLEMENTING: Write code, run tests, make edits with precision.
- VERIFYING: Review your own changes, run linters and tests, fix issues.
- COMMITTING: Stage files, write a meaningful commit message, commit.
- CREATING_MR: Push the branch and create a merge request.

Guidelines:
- Always think step by step before acting.
- Transitions must be explicit — announce when you move to the next state.
- Use tools when you need to interact with the filesystem, git, or external systems.
- Be concise and direct. Prefer action over speculation.
- If uncertain, ask before making destructive changes.
"""


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------
class LLMClient:
    """OpenAI-compatible chat-completion client backed by httpx.

    Supports:
        - Chat completions (``/chat/completions``)
        - Tool calling (function-calling / tool-use)
        - Configurable base URL, model, timeout from AppConfig
        - System-prompt helper for structured prompting

    Parameters
    ----------
    config : AppConfig
        A Pydantic :class:`AppConfig` instance (e.g. from
        ``config_loader.load_config()``). Provides typed access to
        ``runtime.llm_base_url``, ``runtime.llm_model``, and
        ``runtime.llm_timeout_seconds``.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

        # --- Resolve settings from typed config ---
        self.base_url: str = config.runtime.llm_base_url.rstrip("/")
        self.model: str = config.runtime.llm_model
        self.timeout: int = config.runtime.llm_timeout_seconds

        self.api_key: str = os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            logger.warning(
                "DEEPSEEK_API_KEY is not set — API calls will fail with 401/403."
            )

        # --- Httpx client ---
        proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("http_proxy")
        client_kwargs = {
            "timeout": httpx.Timeout(self.timeout),
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        self._client = httpx.Client(**client_kwargs)

    # ------------------------------------------------------------------
    # Core chat method
    # ------------------------------------------------------------------
    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Send a chat-completion request.

        Parameters
        ----------
        messages : list[dict]
            List of message objects with ``role`` and ``content`` keys.
        tools : list[dict] or None
            Optional list of tool-definition dicts (OpenAI function-calling format).
        temperature : float
            Sampling temperature (0.0–2.0).
        max_tokens : int
            Maximum tokens in the response.
        **kwargs
            Additional parameters forwarded to the API payload.

        Returns
        -------
        dict
            The full JSON response from the API.

        Raises
        ------
        httpx.HTTPStatusError
            If the server returns a 4xx/5xx status.
        httpx.RequestError
            On network-level failures (DNS, connection, timeout).
        """
        url = f"{self.base_url}/chat/completions"

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        logger.debug(
            "chat: model=%s msgs=%d tools=%d", self.model, len(messages), len(tools or [])
        )

        try:
            response = self._client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError:
            logger.error(
                "HTTP %d from %s: %s",
                response.status_code,
                url,
                response.text[:500],
            )
            raise
        except httpx.RequestError:
            logger.exception("Request failed (%s)", url)
            raise

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------
    def chat_with_system(
        self,
        user_message: str,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Send a single-turn user message with an automatic system prompt.

        Parameters
        ----------
        user_message : str
            The user message content.
        tools : list[dict] or None
            Optional tool definitions.
        system_prompt : str or None
            Override the default system prompt. If ``None``, the built-in
            ``SYSTEM_PROMPT`` is used.

        Returns
        -------
        dict
            Full API response dict.
        """
        if system_prompt is None:
            system_prompt = SYSTEM_PROMPT

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        return self.chat(messages, tools=tools, **kwargs)

    @staticmethod
    def extract_content(response: Dict[str, Any]) -> str:
        """Extract the text content from a chat-completion response.

        Returns an empty string if the response structure is unexpected.
        """
        try:
            return response["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            logger.warning("Could not extract content from response")
            return ""

    @staticmethod
    def extract_tool_calls(response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract tool-call objects from a chat-completion response.

        Returns an empty list if there are no tool calls or the response
        structure is unexpected.
        """
        try:
            message = response["choices"][0]["message"]
            return message.get("tool_calls", [])
        except (KeyError, IndexError, TypeError):
            logger.warning("Could not extract tool_calls from response")
            return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<LLMClient model={self.model!r} base_url={self.base_url!r}>"
