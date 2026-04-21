"""
retrieval_model.py — 注入检索工具的 LitellmModel 子类
=======================================================

职责
----
继承 LitellmModel，覆盖两个方法：
  _query()         — 在调用 API 时，在 tools 列表里追加三个检索工具 schema
  _parse_actions() — 解析 LLM 返回的 tool call，允许检索工具名称通过

关键设计
--------
原版 LitellmModel._query() 硬编码 tools=[BASH_TOOL]，
且 parse_toolcall_actions() 对非 bash 工具名直接报 FormatError。

我们的修改：
  1. _query: tools=[BASH_TOOL] + RETRIEVAL_TOOLS
  2. _parse_actions: 对检索工具调用，解析 args 并记录工具名；
     对 bash 调用，保持原有 {"command": ..., "tool_call_id": ...} 格式；
     对检索工具调用，生成 {"tool_name": ..., "args": ..., "tool_call_id": ...} 格式。

这样 RetrievalAgent.execute_actions 可以通过 action.get("tool_name") 区分
是 bash 命令还是检索调用。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
from minisweagent.models.utils.actions_toolcall import BASH_TOOL, format_toolcall_observation_messages
from minisweagent.exceptions import FormatError
from jinja2 import StrictUndefined, Template

from .retrieval_tools import RETRIEVAL_TOOLS, TOOL_FUNCTIONS

logger = logging.getLogger(__name__)

# 所有合法的工具名（bash + 三个检索工具）
_ALLOWED_TOOLS = {"bash"} | set(TOOL_FUNCTIONS.keys())


class RetrievalModelConfig(LitellmModelConfig):
    """与 LitellmModelConfig 相同，未增加新字段。"""
    pass


class RetrievalModel(LitellmModel):
    """
    在 LitellmModel 基础上注入检索工具的模型类。

    使用方式
    --------
    model = RetrievalModel(
        model_name="openai/qwen-plus",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model_kwargs={"api_key": "sk-xxx"},
    )
    """

    def __init__(self, *, config_class=RetrievalModelConfig, **kwargs):
        super().__init__(config_class=config_class, **kwargs)

    def _query(self, messages: list[dict], **kwargs):
        import os
        import litellm
        model_kwargs = self.config.model_kwargs | kwargs
        api_base = model_kwargs.get("api_base") or "https://uni-api.cstcloud.cn/v1"
        api_key = model_kwargs.get("api_key") or os.environ.get("UNI_API_KEY", "")
        model_name = self.config.model_name

        if not model_name.startswith("openai/"):
            model_name = f"openai/{model_name}"

        return litellm.completion(
            model=model_name,
            api_base=api_base,
            api_key=api_key,
            messages=messages,
            tools=[BASH_TOOL] + RETRIEVAL_TOOLS,
            tool_choice=model_kwargs.get("tool_choice", "auto"),
            temperature=model_kwargs.get("temperature", 0.0),
            max_tokens=model_kwargs.get("max_tokens"),
        )

    def _parse_actions(self, response) -> list[dict]:
        """
        解析 LLM 返回的 tool calls，支持 bash 和三个检索工具。

        返回格式：
          bash 调用:       {"command": "...", "tool_call_id": "..."}
          检索工具调用:    {"tool_name": "search_hybrid", "args": {...}, "tool_call_id": "..."}
        """
        tool_calls = response.choices[0].message.tool_calls or []

        if not tool_calls:
            raise FormatError({
                "role": "user",
                "content": Template(
                    self.config.format_error_template, undefined=StrictUndefined
                ).render(
                    error="No tool calls found in the response. Every response MUST include at least one tool call.",
                    actions=[],
                ),
                "extra": {"interrupt_type": "FormatError"},
            })

        actions = []
        for tc in tool_calls:
            name = tc.function.name
            error_msg = ""

            # 解析参数
            try:
                args = json.loads(tc.function.arguments)
            except Exception as e:
                error_msg = f"Error parsing tool call arguments: {e}."
                args = {}

            # 检查工具名是否合法
            if name not in _ALLOWED_TOOLS:
                error_msg += f" Unknown tool '{name}'. Allowed tools: {sorted(_ALLOWED_TOOLS)}."

            if error_msg:
                raise FormatError({
                    "role": "user",
                    "content": Template(
                        self.config.format_error_template, undefined=StrictUndefined
                    ).render(actions=[], error=error_msg.strip()),
                    "extra": {"interrupt_type": "FormatError"},
                })

            if name == "bash":
                # bash 工具：保持原格式
                if not isinstance(args, dict) or "command" not in args:
                    raise FormatError({
                        "role": "user",
                        "content": Template(
                            self.config.format_error_template, undefined=StrictUndefined
                        ).render(actions=[], error="Missing 'command' argument in bash tool call."),
                        "extra": {"interrupt_type": "FormatError"},
                    })
                actions.append({"command": args["command"], "tool_call_id": tc.id})
            else:
                # 检索工具：记录工具名和参数
                actions.append({
                    "tool_name":    name,
                    "args":         args,
                    "tool_call_id": tc.id,
                })

        return actions

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]:
        """
        格式化观察消息，与原版完全兼容。
        检索工具和 bash 工具的输出都通过 format_toolcall_observation_messages 处理，
        因为它们的 output 字段结构相同（都是 {"output": str, "returncode": int}）。
        """
        actions = message.get("extra", {}).get("actions", [])
        return format_toolcall_observation_messages(
            actions=actions,
            outputs=outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
            multimodal_regex=self.config.multimodal_regex,
        )