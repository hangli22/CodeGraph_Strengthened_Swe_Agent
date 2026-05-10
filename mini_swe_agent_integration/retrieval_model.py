"""
retrieval_model.py — 注入检索工具（含 deepen_file）的 LitellmModel 子类
==========================================================================

关键设计：
- 通过 tools 参数注册 bash + 检索工具，让模型使用 function calling
- _parse_actions 解析 tool_calls 响应，产出统一格式的 action 列表
- 只要存在合法 tool_calls，就忽略/清空 assistant 的自然语言 content
- format_error_template 引导模型重新发起 tool call（不提 mswea_bash_command）
- observation_template 对长输出更早截断，减少大文件/宽 grep 对上下文的污染
"""

from __future__ import annotations

import json
import logging
from typing import Any

from jinja2 import StrictUndefined, Template

from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
from minisweagent.models.utils.actions_toolcall import (
    BASH_TOOL,
    format_toolcall_observation_messages,
)

from .retrieval_tools import RETRIEVAL_TOOLS, TOOL_FUNCTIONS

logger = logging.getLogger(__name__)

_ALLOWED_TOOLS = {"bash"} | set(TOOL_FUNCTIONS.keys())


# ── 专用于 tool calling 模式的 format_error_template ──────────────────────
# 当模型没有正确调用工具时，用此模板提示它重试。
# 注意：如果 assistant message 里已经有合法 tool_calls，则不会因为 content 非空而报错。
_TOOLCALL_FORMAT_ERROR_TEMPLATE = """\
Format error: {{error}}

You MUST include one valid tool call in your response.
Available tools: bash, search_hybrid, search_bm25, deepen_file, search_semantic, search_structural.

- Use `bash` for ALL shell operations: read files, edit files, run scripts, git commands, and submit.
- Use `search_hybrid` as the default first retrieval tool.
- Use `search_bm25` for exact symbols, file names, function/method/class names, parameters, and error messages.
- Use `deepen_file` after retrieval identifies a promising file and you need method-level/call-graph details.
- Use `search_structural` only with a known node_id from previous retrieval output.
- Do not use markdown code blocks or plain-text shell commands.
- Natural language content is ignored; the action must be represented by a tool call.

To finish, call: bash({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
"""


# ── 观察模板：渲染 tool call 执行结果 ────────────────────────────────────
# 这里把长输出阈值从 10000 降到 6000，head/tail 各 2500。
# 目的：避免 cat 大文件 / 宽 grep 输出污染上下文。
_TOOLCALL_OBSERVATION_TEMPLATE = """\
{% if output.exception_info -%}
<exception>{{output.exception_info}}</exception>
{% endif -%}
<returncode>{{output.returncode}}</returncode>
{% if output.output | length < 6000 -%}
<output>
{{ output.output -}}
</output>
{%- else -%}
<warning>
Output too long ({{output.output | length}} chars). Showing only the head and tail.

Do NOT repeat the same broad command.
Use more selective commands, for example:
- nl -ba path/to/file.py | sed -n '120,220p'
- grep -n "specific_pattern" path/to/file.py | head -20
- grep -rn "specific_pattern" package/ --include="*.py" | head -30

Prefer reading focused line ranges around functions/classes returned by search results.
</warning>
<output_head>
{{ output.output[:2500] }}
</output_head>
<elided_chars>{{ output.output | length - 5000 }} characters elided</elided_chars>
<output_tail>
{{ output.output[-2500:] }}
</output_tail>
{%- endif -%}
"""


class RetrievalModelConfig(LitellmModelConfig):
    """扩展配置，覆盖 format_error_template 和 observation_template 的默认值。"""

    def __init__(self, **kwargs):
        kwargs.setdefault("format_error_template", _TOOLCALL_FORMAT_ERROR_TEMPLATE)
        kwargs.setdefault("observation_template", _TOOLCALL_OBSERVATION_TEMPLATE)
        super().__init__(**kwargs)


class RetrievalModel(LitellmModel):
    """
    支持 bash + 检索工具的 function-calling 模型。

    API 调用时同时注册 BASH_TOOL 和 RETRIEVAL_TOOLS。
    模型每轮返回 tool_calls，由 _parse_actions 统一解析。

    设计约定：
    - 只要 response 中存在合法 tool_calls，就执行 tool call。
    - 不因为 assistant content 非空而 FormatError。
    - 为减少上下文污染，会在解析到 tool_calls 后将 assistant message content 清为 None。
    """

    def __init__(self, *, config_class=RetrievalModelConfig, **kwargs):
        super().__init__(config_class=config_class, **kwargs)

    def _query(self, messages: list[dict], **kwargs):
        """发送带 tools 参数的请求，启用 function calling。"""
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
        从模型响应中解析 tool_calls，返回统一格式的 action 列表。

        返回格式：
        bash 工具:
            {"command": str, "tool_call_id": str}

        检索工具:
            {"tool_name": str, "args": dict, "tool_call_id": str}

        宽松策略：
        - assistant content 非空但 tool_calls 合法时，不报错；
        - 一旦存在 tool_calls，就清空 response 里的 assistant content；
        - 如果返回多个 tool_calls，只执行第一个合法 tool call，忽略后续 tool call。
        """
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            raise self._format_error(
                "No tool calls found. You MUST call exactly one tool per turn."
            )

        # 关键：清洗原始 API response，避免 extra.response 里保留 assistant content。
        self._clear_response_assistant_content(response)

        first_error = ""

        for tc in tool_calls:
            try:
                action = self._parse_single_tool_call(tc)
            except FormatError as e:
                if not first_error:
                    first_error = self._extract_format_error_text(e)
                continue

            # 只执行第一个合法 tool call。
            return [action]

        raise self._format_error(
            first_error or "Tool calls were present, but none could be parsed as a valid action."
        )

    def _parse_single_tool_call(self, tc) -> dict:
        """
        解析单个 tool_call。

        返回：
          bash:      {"command": str, "tool_call_id": str}
          retrieval: {"tool_name": str, "args": dict, "tool_call_id": str}
        """
        name = tc.function.name
        raw_arguments = tc.function.arguments

        try:
            args = json.loads(raw_arguments)
        except Exception as e:
            raise self._format_error(
                f"Error parsing arguments for '{name}': {e}."
            )

        if name not in _ALLOWED_TOOLS:
            raise self._format_error(
                f"Unknown tool '{name}'. Allowed tools: {sorted(_ALLOWED_TOOLS)}."
            )

        if name == "bash":
            command = args.get("command", "") if isinstance(args, dict) else ""
            if not command:
                raise self._format_error(
                    "bash tool call must include a non-empty 'command' argument."
                )
            return {
                "command": command,
                "tool_call_id": tc.id,
            }

        if not isinstance(args, dict):
            raise self._format_error(
                f"Arguments for retrieval tool '{name}' must be a JSON object."
            )

        return {
            "tool_name": name,
            "args": args,
            "tool_call_id": tc.id,
        }

    def _format_error(self, error: str) -> FormatError:
        """构造统一 FormatError。"""
        return FormatError({
            "role": "user",
            "content": Template(
                self.config.format_error_template,
                undefined=StrictUndefined,
            ).render(
                error=error,
                actions=[],
            ),
            "extra": {"interrupt_type": "FormatError"},
        })

    @staticmethod
    def _extract_format_error_text(exc: FormatError) -> str:
        """
        尽量从 FormatError 中提取可读错误文本。
        如果 mini-swe-agent 的 FormatError 结构变化，也能安全回退。
        """
        try:
            payload = exc.args[0]
            if isinstance(payload, dict):
                return str(payload.get("content", ""))
        except Exception:
            pass
        return str(exc)

    @staticmethod
    def _clear_assistant_content(msg) -> None:
        """
        尝试把 assistant message 的 content 清为 None。

        兼容 LiteLLM 返回对象、OpenAI-like 对象、dict 三种形式。
        清理失败不影响执行。
        """
        if msg is None:
            return

        # 对象形式：msg.content
        try:
            msg.content = None
        except Exception:
            pass

        # dict 形式：msg["content"]
        try:
            msg["content"] = None
        except Exception:
            pass


    @classmethod
    def _clear_response_assistant_content(cls, response) -> None:
        """
        清空 response.choices[0].message.content。

        目的：
        - 避免 tool_calls 存在时 assistant content 污染 trajectory；
        - 避免 extra.response.choices[0].message.content 仍保存原始自然语言；
        - 避免 tool observation 被模型复制进 assistant content 后继续污染上下文。
        """
        if response is None:
            return

        # LiteLLM / OpenAI object style: response.choices[0].message
        try:
            choices = getattr(response, "choices", None)
            if choices:
                msg = choices[0].message
                cls._clear_assistant_content(msg)
        except Exception:
            pass

        # dict style: response["choices"][0]["message"]
        try:
            choices = response.get("choices", [])
            if choices:
                msg = choices[0].get("message")
                cls._clear_assistant_content(msg)
        except Exception:
            pass

    def format_observation_messages(self, message, outputs, template_vars=None):
        """
        将执行结果格式化为 OpenAI tool response 消息。

        每个 output 对应一个：
          {"role": "tool", "tool_call_id": ..., "content": ...}
        """
        actions = message.get("extra", {}).get("actions", [])
        return format_toolcall_observation_messages(
            actions=actions,
            outputs=outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
            multimodal_regex=self.config.multimodal_regex,
        )