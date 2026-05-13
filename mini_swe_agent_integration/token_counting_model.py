from __future__ import annotations

from minisweagent.models.litellm_model import LitellmModel

from .token_usage import TokenUsageMixin


class TokenCountingLitellmModel(TokenUsageMixin, LitellmModel):
    """
    Baseline 模式使用的 LitellmModel 包装类。

    只改统计，不改调用协议、不改 prompt、不改 action 解析。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_token_usage()

    def _query(self, messages: list[dict], **kwargs):
        response = super()._query(messages, **kwargs)
        self._record_token_usage(response)
        return response