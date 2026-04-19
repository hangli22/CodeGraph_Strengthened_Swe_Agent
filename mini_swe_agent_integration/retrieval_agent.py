"""
retrieval_agent.py — 拦截检索工具调用的 Agent 子类
====================================================

职责
----
继承 DefaultAgent，覆盖 execute_actions 方法：
- 检测 action 中是否包含 tool_name 字段（检索工具调用）
- 若是，在 Python 层调用 dispatch()，不经过 Environment.execute
- 若否（bash 调用），走原始路径 super().execute_actions()

这样做的好处
------------
1. 检索工具完全在 Python 层执行，不占用 bash 的 subprocess 槽位
2. 不需要修改 Environment，只需替换 Agent
3. 检索结果以与 bash 输出相同的格式返回，LLM 感知不到差异
"""

from __future__ import annotations

import logging
import time
from typing import Any

from minisweagent.agents.default import DefaultAgent, AgentConfig

from .retrieval_tools import dispatch, TOOL_FUNCTIONS

logger = logging.getLogger(__name__)


class RetrievalAgentConfig(AgentConfig):
    """与 AgentConfig 相同，未增加新字段。可按需扩展。"""
    pass


class RetrievalAgent(DefaultAgent):
    """
    在 DefaultAgent 基础上支持检索工具调用的 Agent。

    使用方式
    --------
    from mini_swe_agent_integration.retrieval_agent import RetrievalAgent
    from mini_swe_agent_integration.retrieval_model import RetrievalModel
    from minisweagent.environments.local import LocalEnvironment

    agent = RetrievalAgent(
        RetrievalModel(model_name="qwen-plus", ...),
        LocalEnvironment(),
        system_template=...,
        instance_template=...,
    )
    agent.run(task="Fix this issue: ...")
    """

    def __init__(self, model, env, *, config_class=RetrievalAgentConfig, **kwargs):
        super().__init__(model, env, config_class=config_class, **kwargs)
        # 统计检索工具调用次数（用于 trajectory 分析）
        self.retrieval_call_counts: dict[str, int] = {name: 0 for name in TOOL_FUNCTIONS}

    def execute_actions(self, message: dict) -> list[dict]:
        """
        执行 LLM 返回的 actions。

        分流逻辑：
          - 含 tool_name 字段 → 检索工具，在 Python 层处理
          - 含 command 字段  → bash 命令，走原始路径
          - 混合情况（同时有检索和 bash）→ 逐个处理，合并结果
        """
        actions = message.get("extra", {}).get("actions", [])

        if not actions:
            # 没有 action，走原始路径（会触发 FormatError 或空响应处理）
            return super().execute_actions(message)

        # 检查是否全部都是 bash 命令（最常见路径，快速返回）
        all_bash = all("command" in a and "tool_name" not in a for a in actions)
        if all_bash:
            return super().execute_actions(message)

        # 有检索工具调用，逐个处理
        outputs = []
        for action in actions:
            if "tool_name" in action:
                # 检索工具：Python 层执行
                output = self._execute_retrieval(action)
            else:
                # bash 命令：环境执行
                output = self.env.execute(action)
            outputs.append(output)

        return self.add_messages(
            *self.model.format_observation_messages(message, outputs, self.get_template_vars())
        )

    def _execute_retrieval(self, action: dict) -> dict:
        """
        在 Python 层执行检索工具调用，返回与 Environment.execute 完全相同格式的结果字典。

        返回格式（与 LocalEnvironment.execute 一致）：
          {"output": str, "returncode": int, "exception_info": str}
        """
        tool_name = action["tool_name"]
        args      = action.get("args", {})

        logger.info("检索工具调用: %s(%s)", tool_name, args)
        t0 = time.perf_counter()

        try:
            result_text  = dispatch(tool_name, args)
            returncode   = 0
            exception_info = ""
        except Exception as e:
            result_text    = f"[{tool_name} 执行异常] {type(e).__name__}: {e}"
            returncode     = 1
            exception_info = f"{type(e).__name__}: {e}"
            logger.exception("检索工具异常: %s", tool_name)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info("检索完成: %s 耗时 %.0fms", tool_name, elapsed_ms)

        # 更新统计
        if tool_name in self.retrieval_call_counts:
            self.retrieval_call_counts[tool_name] += 1

        return {
            "output":         result_text,
            "returncode":     returncode,
            "exception_info": exception_info,   # 必须存在，observation_template 会引用它
        }

    def serialize(self, *extra_dicts) -> dict:
        """在序列化时追加检索工具调用统计。"""
        data = super().serialize(*extra_dicts)
        data["info"]["retrieval_stats"] = {
            "call_counts": self.retrieval_call_counts,
            "total_calls": sum(self.retrieval_call_counts.values()),
        }
        return data