"""OpenAI-compatible LLM client for neiWangAgent.

Uses httpx for HTTP calls. Supports chat completion with tool calling,
configurable base URL / model via AppConfig, and structured prompting with a
system prompt that describes the agent's state-machine role.

★ P7-37: 预算控制 — 跟踪 LLM 费用，超预算自动停止
★ P2-12: 通用 API Key — 支持 LLM_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY

日志追踪：
  每次 LLM 调用自动记录：
    - 请求模型、消息数、工具数
    - 响应 token 用量（如有）
    - 耗时（毫秒级）
    - 累计费用（cents）
    - 成功/失败状态
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from agent_mcp.config_loader import AppConfig
from agent_mcp.tracing import get_tracer, Tracer

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are neiWangAgent, an autonomous coding agent that follows a strict state-machine flow.

States: UNDERSTANDING -> PLANNING -> IMPLEMENTING -> VERIFYING -> COMMITTING -> CREATING_MR

Your responsibilities at each state:
- UNDERSTANDING: Analyze requirements, read existing code, ask clarifying questions.
- PLANNING: Produce a concrete implementation plan with file paths and change details.
- IMPLEMENTING: Write code, run tests, make edits with precision.
- VERIFYING: Review your own changes, run linters and tests, fix issues.
- COMMITTING: Stage files, write a meaningful commit message, commit.
- CREATING_MR: Push the branch and create a merge request.

Guidelines:
- Always think step by step before acting.
- Transitions must be explicit - announce when you move to the next state.
- Use tools when you need to interact with the filesystem, git, or external systems.
- Be concise and direct. Prefer action over speculation.
- If uncertain, ask before making destructive changes.
"""


class LLMClient:
    """OpenAI-compatible chat-completion client backed by httpx."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

        self.base_url: str = config.runtime.llm_base_url.rstrip("/")
        self.model: str = config.runtime.llm_model
        self.timeout: int = config.runtime.llm_timeout_seconds

        api_key_env = getattr(config.runtime, 'llm_api_key_env', 'LLM_API_KEY')
        self.api_key: str = (
            os.environ.get(api_key_env, "")
            or os.environ.get("DEEPSEEK_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", "")
        )
        if not self.api_key:
            logger.warning(f"{api_key_env} is not set - API calls will fail with 401/403.")

        self.tracer: Tracer = get_tracer()

        # P7-37: Budget control
        self._budget_cents: int = getattr(config.task, 'budget_cents', 0)
        self._cost_cents: float = 0.0
        self._input_cost_per_1k: float = 0.00014
        self._output_cost_per_1k: float = 0.00028

        proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
        )
        client_kwargs = {
            "timeout": httpx.Timeout(self.timeout),
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        }
        if proxy:
            self.tracer.debug("llm.proxy_configured",
                              detail={"proxy": proxy[:30] + "..." if len(proxy) > 30 else proxy})
            client_kwargs["proxy"] = proxy
        else:
            self.tracer.debug("llm.no_proxy")

        self._client = httpx.Client(**client_kwargs)
        self.tracer.debug("llm.client_init",
                          detail={"model": self.model, "base_url": self.base_url, "timeout": self.timeout})

    def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        """Send a chat-completion request with budget control."""
        url = f"{self.base_url}/chat/completions"

        # P7-37: Budget check before API call
        if self._budget_cents > 0 and self._cost_cents >= self._budget_cents:
            raise RuntimeError(
                f"LLM budget exhausted: {self._cost_cents:.1f}/{self._budget_cents} cents"
            )

        payload = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, **kwargs}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        total_chars = sum(len(str(m.get("content", ""))) for m in messages)

        self.tracer.debug("llm.request.start",
                          detail={"model": self.model, "messages": len(messages), "chars": total_chars, "tools": len(tools or [])})

        start_time = time.perf_counter()
        try:
            response = self._client.post(url, json=payload)
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            response.raise_for_status()
            data = response.json()

            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            # P7-37: Track cost
            cost = (prompt_tokens * self._input_cost_per_1k + completion_tokens * self._output_cost_per_1k) / 1000
            self._cost_cents += cost * 100  # convert to cents

            self.tracer.info("llm.request",
                             detail={"status": response.status_code, "elapsed_ms": round(elapsed_ms),
                                     "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                                     "total_tokens": usage.get("total_tokens", 0),
                                     "cost_cents": round(self._cost_cents, 2)})

            return data

        except httpx.HTTPStatusError:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self.tracer.error("llm.request", detail={"status": response.status_code, "elapsed_ms": round(elapsed_ms), "error": response.text[:200]})
            logger.error("HTTP %d from %s: %s", response.status_code, url, response.text[:500])
            raise
        except httpx.RequestError:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self.tracer.error("llm.request", detail={"elapsed_ms": round(elapsed_ms), "error": "network_error"})
            logger.exception("Request failed (%s)", url)
            raise

    def chat_with_system(self, user_message, *, tools=None, system_prompt=None, **kwargs):
        """Send a single-turn user message with an automatic system prompt."""
        if system_prompt is None:
            system_prompt = SYSTEM_PROMPT
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}]
        return self.chat(messages, tools=tools, **kwargs)

    @staticmethod
    def extract_content(response):
        try:
            return response["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            logger.warning("Could not extract content from response")
            return ""

    @staticmethod
    def extract_tool_calls(response):
        try:
            message = response["choices"][0]["message"]
            return message.get("tool_calls", [])
        except (KeyError, IndexError, TypeError):
            logger.warning("Could not extract tool_calls from response")
            return []

    @property
    def cost_cents(self) -> float:
        """P7-37: Current accumulated cost in cents."""
        return self._cost_cents

    @property
    def budget_remaining(self) -> float:
        """P7-37: Remaining budget in cents."""
        return max(0, self._budget_cents - self._cost_cents)

    def close(self):
        self._client.close()
        self.tracer.debug("llm.client_closed")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __repr__(self):
        return f"<LLMClient model={self.model!r} base_url={self.base_url!r} cost={self._cost_cents:.1f}c>"
