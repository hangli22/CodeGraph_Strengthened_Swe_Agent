"""
token_usage.py — 统计 LiteLLM response.usage

统计口径：
- main_agent_tokens：只统计 Agent 主循环模型调用
- 不统计 issue_focus / query_focus
- 不统计 embedding token / cost
"""

from __future__ import annotations

from typing import Any


def _usage_get(usage: Any, key: str) -> int:
    if usage is None:
        return 0

    if isinstance(usage, dict):
        value = usage.get(key, 0)
    else:
        value = getattr(usage, key, 0)

    try:
        return int(value or 0)
    except Exception:
        return 0


class TokenUsageMixin:
    """
    给 LitellmModel / RetrievalModel 混入 token 统计能力。

    只统计 response.usage 中的：
      - prompt_tokens
      - completion_tokens
      - total_tokens
    """

    def _init_token_usage(self) -> None:
        self.token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
            "missing_usage_calls": 0,
        }

    def _record_token_usage(self, response: Any) -> None:
        if not hasattr(self, "token_usage"):
            self._init_token_usage()

        usage = getattr(response, "usage", None)

        # 兼容 dict response
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")

        if usage is None:
            self.token_usage["calls"] += 1
            self.token_usage["missing_usage_calls"] += 1
            return

        prompt_tokens = _usage_get(usage, "prompt_tokens")
        completion_tokens = _usage_get(usage, "completion_tokens")
        total_tokens = _usage_get(usage, "total_tokens")

        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens

        self.token_usage["prompt_tokens"] += prompt_tokens
        self.token_usage["completion_tokens"] += completion_tokens
        self.token_usage["total_tokens"] += total_tokens
        self.token_usage["calls"] += 1

    def get_token_usage(self) -> dict:
        if not hasattr(self, "token_usage"):
            self._init_token_usage()
        return dict(self.token_usage)