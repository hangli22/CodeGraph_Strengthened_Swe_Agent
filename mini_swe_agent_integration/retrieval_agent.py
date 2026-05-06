"""
retrieval_agent.py — 拦截检索工具调用的 Agent 子类
====================================================

职责：
  - 完全接管 execute_actions：不委托 super()（避免 DefaultAgent 按文本格式处理）
  - bash action → 交给 LocalEnvironment.execute({"command": command_string})
  - retrieval action → 在 Python 层调用 dispatch()
  - 统一格式化输出并追加到对话历史

关键修改（相对原版）：
  - 不再对 all_bash 情况调用 super().execute_actions()
  - bash 通过 self.env.execute({"command": command_str}) 执行
  - 对 minisweagent.exceptions.Submitted 单独放行，让提交信号能正常传递给 agent.run()
"""

from __future__ import annotations

import re
import logging
import time
from typing import Any

from minisweagent.agents.default import DefaultAgent, AgentConfig
from minisweagent.exceptions import Submitted

from .retrieval_tools import dispatch, TOOL_FUNCTIONS

logger = logging.getLogger(__name__)


class RetrievalAgentConfig(AgentConfig):
    """与 AgentConfig 相同，未增加新字段。可按需扩展。"""
    pass


class RetrievalAgent(DefaultAgent):
    """
    在 DefaultAgent 基础上支持 bash + 检索工具并列的 Agent。

    所有 action 都在 execute_actions 中直接处理：
      - {"command": str, "tool_call_id": str}  → self.env.execute({"command": command})
      - {"tool_name": str, "args": dict, ...}  → self._execute_retrieval(action)

    额外策略：
      1. 正常放行 Submitted，让 agent.run() 能得到 exit_status="Submitted"
      2. 记录最近一次 git diff 是否为空，阻止空 patch 提交
      3. 第一次 import-based reproduction 真实执行，让 LLM 看到完整报错
      4. 如果确认某个模块 import 环境失败，后续重复 import-based reproduction 会被阻止
      !!!这个需要修改
    """

    def __init__(self, model, env, *, config_class=RetrievalAgentConfig, **kwargs):
        super().__init__(model, env, config_class=config_class, **kwargs)

        # 统计检索工具调用次数（用于 trajectory 分析）
        self.retrieval_call_counts: dict[str, int] = {
            name: 0 for name in TOOL_FUNCTIONS
        }

        # 记录最近一次 git diff 的状态，用于阻止空 patch 提交
        self._last_git_diff_empty: bool | None = None

        # 记录 import-based reproduction 的环境失败状态。
        # 例：
        # {
        #   "astropy": {
        #       "reason": "...",
        #       "command": "python3 -c ...",
        #       "count": 1,
        #   }
        # }
        self._failed_import_modules: dict[str, dict[str, Any]] = {}

    def execute_actions(self, message: dict) -> list[dict]:
        """
        执行 LLM 返回的全部 actions，返回追加到对话历史的消息列表。

        与原版 DefaultAgent.execute_actions 的区别：
        1. 不依赖 DefaultAgent 的文本解析逻辑
        2. 支持 bash 和检索工具混合调用
        3. bash 命令以 {"command": command} 形式传给 env.execute()
        4. 如果 assistant message 含 tool call/action，则强制清空 content，避免污染上下文
        """
        actions = message.get("extra", {}).get("actions", [])

        if not actions:
            # 无 action（FormatError 通常已经在 model 层抛出，这里做兜底）
            return super().execute_actions(message)

        # 关键兜底：
        # 只要已经解析出 actions，说明这是一个 tool-calling assistant message。
        # assistant content 不应该进入后续上下文或 trajectory。
        self._sanitize_toolcall_assistant_message(message)

        outputs = []

        for action in actions:
            if "tool_name" in action:
                output = self._execute_retrieval(action)

            elif "command" in action:
                output = self._execute_bash(action)

            else:
                output = self._make_output(
                    f"[ERROR] Unknown action format: {action}",
                    returncode=1,
                )

            outputs.append(output)

        # 在格式化 observation 之前再清洗一次，防止执行过程中 message 被外部对象恢复。
        self._sanitize_toolcall_assistant_message(message)

        obs_messages = self.model.format_observation_messages(
            message,
            outputs,
            self.get_template_vars(),
        )

        return self.add_messages(*obs_messages)

    def _execute_bash(self, action: dict) -> Any:
        """
        执行 bash 命令，返回与 LocalEnvironment.execute 相同格式的结果。

        策略：
        1. 空 diff 提交拦截：
        - 提交前必须运行 git diff
        - 最近一次 git diff 不能为空

        2. sed -i 硬性拦截：
        - 禁止使用 sed -i 编辑文件
        - 尤其避免多行 sed 替换静默失败
        - 要求改用 Python pathlib + assert old in text + write_text

        3. import-based reproduction 重复拦截：
        - 第一次 import astropy / from astropy 这类命令真实执行
        - 如果输出表明本地源码 checkout 未 build 或 import 环境失败，记录状态
        - 之后重复 import 同一模块的 reproduction 命令会被阻止，并返回提示 observation
        """
        command = action["command"]
        command_stripped = command.strip()

        logger.info("Bash 执行: %s", command[:200])

        # ------------------------------------------------------------
        # 空 diff 提交拦截：
        # 必须在 env.execute 之前拦截，否则 LocalEnvironment 会直接抛 Submitted。
        # ------------------------------------------------------------
        if "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in command_stripped:
            canonical_submit = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

            if command_stripped != canonical_submit:
                logger.warning("阻止提交：非标准提交命令: %s", command_stripped)
                return self._make_output(
                    "[policy error] Submit command must be exactly:\n"
                    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n\n"
                    "Do not quote it, combine it with other commands, wrap it in printf, "
                    "or use any other variant.\n"
                    "Before submitting, run git diff and confirm it is non-empty.",
                    returncode=1,
                )

            if self._last_git_diff_empty is True:
                logger.warning("阻止提交：最近一次 git diff 为空")
                return self._make_output(
                    "[policy error] Cannot submit because the last git diff was empty."
                    "Your next action must be source inspection or a source edit. Do not submit again until git diff is non-empty.",
                    returncode=1,
                )

            if self._last_git_diff_empty is None:
                logger.warning("阻止提交：提交前没有运行 git diff")
                return self._make_output(
                    "[policy error] Cannot submit before running git diff.\n"
                    "Run git diff first and confirm it is non-empty.",
                    returncode=1,
                )

        # ------------------------------------------------------------
        # sed -i 硬性拦截：
        # sed -i 很容易出现“命令 returncode=0 但没有替换成功”的静默失败，
        # 尤其是多行替换。因此禁止用 sed -i 编辑源码。
        # ------------------------------------------------------------
        if self._is_blocked_sed_i_command(command_stripped):
            logger.warning("阻止 sed -i 编辑命令: %s", command[:200])
            return self._make_output(
                self._make_sed_i_block_message(command),
                returncode=1,
            )

        # ------------------------------------------------------------
        # import-based reproduction 重复拦截：
        # 第一次失败要让模型看到真实错误；第二次起阻止重复浪费。
        # ------------------------------------------------------------
        import_module = self._detect_import_based_reproduction_module(command_stripped)
        if import_module and import_module in self._failed_import_modules:
            failure = self._failed_import_modules[import_module]
            logger.warning(
                "阻止重复 import-based reproduction: module=%s command=%s",
                import_module,
                command[:160],
            )
            return self._make_output(
                "[reproduction blocked]\n"
                f"A previous import-based reproduction using module '{import_module}' already failed "
                "because the local source checkout/import environment appears broken.\n\n"
                "Do not repeat import-based reproduction for this module. Continue by inspecting source code, "
                "applying a minimal source fix, and checking git diff.\n\n"
                f"Previous failure summary:\n{failure.get('reason', '').strip()}\n\n"
                f"Previous failed command:\n{failure.get('command', '').strip()}",
                returncode=1,
            )

        try:
            result = self.env.execute({"command": command})

            output = self._extract_output_text(result)

            # ------------------------------------------------------------
            # 记录 git diff 结果是否为空
            # ------------------------------------------------------------
            if self._is_git_diff_command(command_stripped):
                self._last_git_diff_empty = not bool(output.strip())

                if self._last_git_diff_empty:
                    logger.warning("检测到 git diff 为空")
                else:
                    logger.info("检测到 git diff 非空，允许后续提交")

            # ------------------------------------------------------------
            # 记录 import-based reproduction 失败状态
            # ------------------------------------------------------------
            if import_module:
                failure_reason = self._detect_import_environment_failure(output)
                if failure_reason:
                    self._failed_import_modules[import_module] = {
                        "reason": failure_reason,
                        "command": command,
                        "count": self._failed_import_modules.get(import_module, {}).get("count", 0) + 1,
                    }
                    logger.warning(
                        "记录 import-based reproduction 失败: module=%s reason=%s",
                        import_module,
                        failure_reason[:200],
                    )

            return result

        except Submitted:
            # 这是 mini-swe-agent 的正常提交信号，不是错误。
            # 必须重新抛出，让 agent.run() 得到 exit_status="Submitted"。
            logger.info("检测到任务提交信号: %s", command[:200])
            raise

        except Exception as e:
            logger.exception("Bash 执行异常: %s", command[:100])
            return self._make_output(
                f"[bash execution error] {type(e).__name__}: {e}",
                returncode=1,
                exception_info=f"{type(e).__name__}: {e}",
            )

    def _execute_retrieval(self, action: dict) -> Any:
        """
        在 Python 层执行检索工具调用。

        返回格式与 LocalEnvironment.execute 一致，
        确保 observation_template 能正常渲染。
        """
        tool_name = action["tool_name"]
        args = action.get("args", {})

        logger.info("检索工具调用: %s(%s)", tool_name, args)
        t0 = time.perf_counter()

        try:
            result_text = dispatch(tool_name, args)
            returncode = 0
            exception_info = ""

        except Exception as e:
            result_text = f"[{tool_name} error] {type(e).__name__}: {e}"
            returncode = 1
            exception_info = f"{type(e).__name__}: {e}"
            logger.exception("检索工具异常: %s", tool_name)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info("检索完成: %s 耗时 %.0fms", tool_name, elapsed_ms)

        # 更新统计
        if tool_name in self.retrieval_call_counts:
            self.retrieval_call_counts[tool_name] += 1

        return self._make_output(result_text, returncode, exception_info)

    @staticmethod
    def _make_output(
        output: str,
        returncode: int = 0,
        exception_info: str = "",
    ) -> dict:
        """
        构造与 LocalEnvironment.execute 返回值兼容的输出字典。

        Jinja2 的 . 操作符对 dict 和 object 都适用，
        所以 observation_template 中 output.output / output.returncode 都能正常工作。
        """
        return {
            "output": output,
            "returncode": returncode,
            "exception_info": exception_info,
        }

    @staticmethod
    def _clear_message_content(obj: Any) -> None:
        """
        清空 message-like 对象的 content 字段。

        兼容：
        - dict: obj["content"]
        - object: obj.content
        """
        if obj is None:
            return

        try:
            obj["content"] = None
        except Exception:
            pass

        try:
            obj.content = None
        except Exception:
            pass


    @classmethod
    def _sanitize_toolcall_assistant_message(cls, message: dict) -> None:
        """
        清洗 tool-calling assistant message。

        目标：
        1. message["content"] = None
        2. message["extra"]["response"].choices[0].message.content = None
        3. message["extra"]["response"]["choices"][0]["message"]["content"] = None

        这样既能清理真正进入上下文的 assistant content，
        也能清理 trajectory 里 extra.response 保存的原始 API response。
        """
        if not isinstance(message, dict):
            return

        # 只处理 assistant message。
        if message.get("role") != "assistant":
            return

        extra = message.get("extra", {}) or {}
        actions = extra.get("actions", [])

        # 如果没有 actions，也没有 tool_calls，说明不是已解析成功的 tool-calling 消息。
        has_tool_calls = bool(message.get("tool_calls")) or bool(actions)
        if not has_tool_calls:
            return

        # 清理当前 assistant message 的 content。
        cls._clear_message_content(message)

        # 清理 extra.response 中保存的原始 response。
        response = extra.get("response")
        if response is None:
            return

        # object style: response.choices[0].message
        try:
            choices = getattr(response, "choices", None)
            if choices:
                resp_msg = choices[0].message
                cls._clear_message_content(resp_msg)
        except Exception:
            pass

        # dict style: response["choices"][0]["message"]
        try:
            choices = response.get("choices", [])
            if choices:
                resp_msg = choices[0].get("message")
                cls._clear_message_content(resp_msg)
        except Exception:
            pass

    @staticmethod
    def _extract_output_text(result: Any) -> str:
        """
        从 LocalEnvironment.execute 的返回值中提取 output 文本。

        兼容 dict 或对象两种形式。
        """
        if isinstance(result, dict):
            return str(result.get("output", "") or "")

        return str(getattr(result, "output", "") or "")

    @staticmethod
    def _is_git_diff_command(command: str) -> bool:
        """
        判断是否是 git diff 命令。

        允许：
          git diff
          git diff path/to/file.py
          git diff -- path/to/file.py

        不把 git status / git show 等算作 git diff。
        """
        command = command.strip()

        if not command.startswith("git diff"):
            return False

        # 排除明显不是单纯 diff 检查的复杂命令。
        # 这里保持保守：只要以 git diff 开头，就记录其输出是否为空。
        return True

    @staticmethod
    def _is_blocked_sed_i_command(command: str) -> bool:
        """
        判断是否应该拦截 sed -i 编辑命令。

        拦截目标：
        - sed -i ...
        - sed -i.bak ...
        - sed -E -i ...
        - sed -i -e ...
        - command 中通过 && / ; 串联的 sed -i

        不拦截：
        - sed -n '10,80p' file.py
        - nl -ba file.py | sed -n '10,80p'
        - grep ... | sed ...
        """
        if not command:
            return False

        # 常见 shell 分隔符切分，避免只检查整条命令开头。
        parts = re.split(r"\s*(?:&&|\|\||;)\s*", command)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # 只处理命令片段中以 sed 开头的部分。
            # 例如：
            #   sed -i ...
            #   sed -E -i ...
            #   sed -i.bak ...
            if not re.match(r"^(?:sudo\s+)?sed\b", part):
                continue

            # 检测 sed 参数里是否包含 -i / -i.bak / --in-place 等。
            if re.search(r"(?<!\S)-i(?:\S*)?(?!\S)", part):
                return True

            if re.search(r"(?<!\S)--in-place(?:=\S+)?(?!\S)", part):
                return True

        return False

    @staticmethod
    def _detect_import_based_reproduction_module(command: str) -> str | None:
        """
        检测命令是否属于 import-based reproduction。

        只拦截类似：
          python -c "import astropy..."
          python3 -c "from astropy..."
          python - <<'PY' ... import astropy ...

        不拦截普通 Python 编辑脚本，除非其中明确 import/from 某个项目包。
        """
        lowered = command.lower()

        # 必须是 python 执行类命令，否则不处理
        if not (
            "python -c" in lowered
            or "python3 -c" in lowered
            or "python - <<" in lowered
            or "python3 - <<" in lowered
        ):
            return None

        # 目前先针对 astropy；后续可以扩展到 django/sympy/sklearn 等。
        # 注意：这里是“项目包 import reproduction”，不是所有 Python 命令。
        if re.search(r"\bimport\s+astropy\b", command) or re.search(r"\bfrom\s+astropy\b", command):
            return "astropy"

        return None

    @staticmethod
    def _detect_import_environment_failure(output: str) -> str:
        """
        从命令输出中判断是否是 import 环境失败。

        返回空字符串表示不是需要记录的 import 环境失败。
        """
        if not output:
            return ""

        patterns = [
            "ImportError: cannot import name '_compiler'",
            "You appear to be trying to import astropy from within a source checkout",
            "could not determine astropy package version",
            "ModuleNotFoundError:",
            "ImportError:",
            "without building the extension modules first",
            "python setup.py build_ext --inplace",
            "pip install -e .",
        ]

        matched = [p for p in patterns if p in output]
        if not matched:
            return ""

        # 提取较短摘要，避免把完整 traceback 存进状态
        lines = []
        for line in output.splitlines():
            line_strip = line.strip()
            if not line_strip:
                continue
            if any(p in line_strip for p in patterns):
                lines.append(line_strip)
            if len(lines) >= 8:
                break

        if not lines:
            return matched[0]

        return "\n".join(lines)

    def serialize(self, *extra_dicts) -> dict:
        """在序列化时追加检索工具调用统计和环境失败状态。"""
        data = super().serialize(*extra_dicts)

        data.setdefault("info", {})
        data["info"]["retrieval_stats"] = {
            "call_counts": self.retrieval_call_counts,
            "total_calls": sum(self.retrieval_call_counts.values()),
        }

        data["info"]["blocked_import_reproduction"] = {
            module: {
                "reason": info.get("reason", ""),
                "count": info.get("count", 0),
            }
            for module, info in self._failed_import_modules.items()
        }

        data["info"]["last_git_diff_empty"] = self._last_git_diff_empty

        return data

    @staticmethod
    def _make_sed_i_block_message(command: str) -> str:
        """
        生成给 LLM 的 sed -i 拦截提示。

        目标：
        - 明确告诉模型该命令被硬性拦截；
        - 解释 sed -i 的风险；
        - 给出推荐编辑方式；
        - 要求编辑后运行 git diff。
        """
        return (
            "[policy error] The command was blocked because it uses `sed -i` to edit files.\n\n"
            "Why this is blocked:\n"
            "- `sed -i` can silently do nothing when the pattern does not match.\n"
            "- Multi-line `sed -i` replacements are especially unreliable.\n"
            "- A zero return code from sed does NOT prove that the file was modified.\n\n"
            "Use a safer editing method instead:\n"
            "1. Use a Python script with pathlib to read the target file.\n"
            "2. Define an exact `old` text block and a `new` text block.\n"
            "3. Before replacing, assert that the old text exists: `assert old in text`.\n"
            "4. Write the updated text back with `path.write_text(...)`.\n"
            "5. Immediately run `git diff <file>` to verify the change is non-empty.\n\n"
            "Recommended pattern:\n"
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "path = Path('path/to/file.py')\n"
            "text = path.read_text()\n"
            "old = '''exact old code block'''\n"
            "new = '''new code block'''\n"
            "assert old in text, 'old code block not found'\n"
            "path.write_text(text.replace(old, new))\n"
            "PY\n\n"
            "Blocked command:\n"
            f"{command}"
        )
