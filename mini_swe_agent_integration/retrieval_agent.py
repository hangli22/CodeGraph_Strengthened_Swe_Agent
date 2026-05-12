"""
retrieval_agent.py — 拦截检索工具调用的 Agent 子类
====================================================

职责：
  - 完全接管 execute_actions：不委托 super() 处理已解析 actions
  - bash action → 交给 LocalEnvironment.execute({"command": command_string})
  - retrieval action → 在 Python 层调用 dispatch()
  - 对 bash action 应用与 baseline_agent.py 对齐的 guardrails
  - 清洗 tool-call assistant message 的自然语言 content
  - 统一格式化输出并追加到对话历史
  - 提交前要求最终 visible working-tree git diff 非空

关键行为：
  - 支持 bash + retrieval 工具
  - 每轮最多一个 action；多 tool call 直接 policy error，不执行任何一个
  - 正常放行 Submitted，让 agent.run() 能得到 exit_status="Submitted"
  - 记录 retrieval 工具调用统计
  - 记录最近一次有效 git diff 是否为空/是否疑似破坏性
  - 收敛控制由 convergence_guard.py 负责
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from minisweagent.agents.default import DefaultAgent, AgentConfig
from minisweagent.exceptions import Submitted

from .retrieval_tools import dispatch, TOOL_FUNCTIONS
from .convergence_guard import ConvergenceGuard

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
      1. 清洗 tool-call assistant content
      2. 对 bash action 应用 guardrails
      3. 提交前要求标准提交命令
      4. 提交前要求运行有效 visible working-tree git diff
      5. 阻止 sed -i 编辑 tracked source files 的高风险做法
      6. 阻止 pytest/pip/test 命令 pipe head/tail 但未开启 pipefail 的做法
      7. 对 /workspace 误用、returncode=0 但输出失败、疑似破坏性 diff 追加 warning
      8. 统计 retrieval 工具调用次数
      9. 通过 ConvergenceGuard 处理多 tool call、步数提醒和收敛控制
    """

    def __init__(self, model, env, *, config_class=RetrievalAgentConfig, **kwargs):
        super().__init__(model, env, config_class=config_class, **kwargs)

        # 统计检索工具调用次数（用于 trajectory 分析）
        self.retrieval_call_counts: dict[str, int] = {
            name: 0 for name in TOOL_FUNCTIONS
        }

        # 最近一次有效 visible working-tree git diff 是否为空：
        #   None = 从未运行有效 git diff
        #   True = 最近一次有效 git diff 为空
        #   False = 最近一次有效 git diff 非空
        self._last_git_diff_empty: bool | None = None

        # 最近一次有效 git diff 是否看起来有破坏性。
        self._last_git_diff_destructive: bool = False

        # 保留字段：兼容旧 serialize / trajectory 分析。
        # 当前不再启用 import-based reproduction 重复拦截，
        # 因为 prompt 已要求 2-3 次失败后停止环境修复。
        self._failed_import_modules: dict[str, dict[str, Any]] = {}

        # 步数提醒 + 收敛控制：
        # - 多 tool call 硬拦截
        # - 每 10 步提醒
        # - 30 步后禁止 broad search
        # - 45 步后只允许 edit / focused test / small range read / git diff
        # - 找到直接错误位置后最多再查 5 步
        self._convergence_guard = ConvergenceGuard(
            make_output=self._make_output,
            is_git_diff_command=self._is_git_diff_command,
            step_notice_interval=10,
        )

    def execute_actions(self, message: dict) -> list[dict]:
        """
        执行 LLM 返回的 action，返回追加到对话历史的消息列表。

        强约束：
          1. 每轮只允许一个 tool call；多 tool call 直接拒绝，不执行任何一个。
          2. 每 10 轮追加 progress notice。
          3. 30 步后禁止 broad search。
          4. 45 步后只允许 edit / focused test / small range read / git diff。
          5. 找到直接错误行后，最多再查 5 步；之后必须编辑或加测试。
        """
        actions = message.get("extra", {}).get("actions", []) or []

        # 如果模型返回 content + tool call，把 content 清空，避免污染上下文/trajectory。
        self._sanitize_toolcall_assistant_message(message)

        if not actions:
            # 无 action 时兜底交给 DefaultAgent。
            # 正常 function-calling 模式下，FormatError 通常已经在 model 层抛出。
            return super().execute_actions(message)

        # ------------------------------------------------------------
        # 多 tool call 硬拦截：
        # 不执行任何一个 action，并返回 policy error。
        # ConvergenceGuard 会尽量让 outputs 数量和 parsed actions 对齐。
        # ------------------------------------------------------------
        multi_tool_outputs = self._convergence_guard.guard_multiple_tool_calls(
            message,
            actions,
        )
        if multi_tool_outputs is not None:
            step_notice = self._convergence_guard.advance_step_and_maybe_make_notice()
            if step_notice:
                self._convergence_guard.append_notice_to_outputs(
                    multi_tool_outputs,
                    step_notice,
                )

            self._sanitize_toolcall_assistant_message(message)
            return self.add_messages(
                *self.model.format_observation_messages(
                    message,
                    multi_tool_outputs,
                    self.get_template_vars(),
                )
            )

        action = actions[0]

        # ------------------------------------------------------------
        # 收敛策略硬拦截：
        # 在执行 action 前判断是否允许。
        # ------------------------------------------------------------
        convergence_guard = self._convergence_guard.guard_action(action)
        if convergence_guard is not None:
            outputs = [convergence_guard]
        else:
            if "tool_name" in action:
                outputs = [self._execute_retrieval(action)]
            elif "command" in action:
                outputs = [self._execute_bash(action)]
            else:
                outputs = [
                    self._make_output(
                        f"[policy error] Unknown action format: {action}",
                        returncode=1,
                    )
                ]

        # 根据 observation 更新“是否已找到直接错误位置”的状态。
        self._convergence_guard.update_after_outputs(action, outputs)

        # 每轮 assistant tool action 计为 1 个交互步。
        step_notice = self._convergence_guard.advance_step_and_maybe_make_notice()
        if step_notice:
            self._convergence_guard.append_notice_to_outputs(outputs, step_notice)

        self._sanitize_toolcall_assistant_message(message)

        return self.add_messages(
            *self.model.format_observation_messages(
                message,
                outputs,
                self.get_template_vars(),
            )
        )

    def _execute_bash(self, action: dict) -> dict:
        """
        执行 bash 命令，返回与 LocalEnvironment.execute 兼容的结果。

        拦截/提示策略基本对齐 baseline_agent.py：
          - 非标准 submit / submit 前 diff 检查
          - sed -i 硬拦截
          - pytest/pip/test | head/tail 没有 pipefail 硬拦截
          - 错误使用 /workspace 作为 repo root：仅提示，不拦截
          - returncode=0 但输出含失败标记：追加 warning
          - git diff 疑似破坏性改动：追加 warning
        """
        command = action["command"]
        command_stripped = command.strip()

        logger.info("Retrieval bash 执行: %s", command[:200])

        # ------------------------------------------------------------
        # 非标准 submit / submit 前 diff 检查
        # ------------------------------------------------------------
        if "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in command_stripped:
            submit_guard = self._guard_submit_command(command_stripped)
            if submit_guard is not None:
                return submit_guard

        # ------------------------------------------------------------
        # sed -i 硬拦截
        # ------------------------------------------------------------
        if self._is_blocked_sed_i_command(command_stripped):
            logger.warning("阻止 sed -i 编辑命令: %s", command[:200])
            return self._make_output(
                self._make_sed_i_block_message(command),
                returncode=1,
            )

        # ------------------------------------------------------------
        # pytest/pip/test | head/tail 没有 pipefail 硬拦截
        # ------------------------------------------------------------
        if self._needs_pipefail(command_stripped) and not self._has_pipefail(command_stripped):
            logger.warning("阻止缺少 pipefail 的重要管道命令: %s", command[:200])
            return self._make_output(
                self._make_pipefail_block_message(command),
                returncode=1,
            )

        # ------------------------------------------------------------
        # 错误使用 /workspace 作为 repo root：仅提示，不拦截
        # ------------------------------------------------------------
        pre_warning = ""
        if self._looks_like_wrong_workspace_root(command_stripped):
            pre_warning = self._make_workspace_root_warning(command)

        try:
            result = self.env.execute({"command": command})
            result = self._normalize_result(result)

            output_text = self._extract_output_text(result)

            # --------------------------------------------------------
            # 记录 git diff 状态
            # --------------------------------------------------------
            if self._is_git_diff_command(command_stripped):
                self._last_git_diff_empty = not bool(output_text.strip())
                self._last_git_diff_destructive = self._looks_like_destructive_diff(output_text)

                if self._last_git_diff_empty:
                    logger.warning("检测到 git diff 为空")
                else:
                    logger.info("检测到 git diff 非空，允许后续提交")

                if self._last_git_diff_destructive:
                    logger.warning("git diff 疑似破坏性改动")

            # 记录是否已经发生过源码编辑。
            # 具体 edit 判断由 ConvergenceGuard 负责，避免 retrieval_agent.py 继续膨胀。
            self._convergence_guard.record_source_edit_if_successful(
                command_stripped,
                result,
            )

            # --------------------------------------------------------
            # output 有失败关键词但 returncode=0：追加 observation warning
            # --------------------------------------------------------
            post_warning = ""
            if self._important_command_returned_zero_but_output_failed(command_stripped, result):
                post_warning += self._make_failed_output_warning()

            # --------------------------------------------------------
            # git diff 疑似清空/大规模删除：warning
            # --------------------------------------------------------
            if self._is_git_diff_command(command_stripped) and self._last_git_diff_destructive:
                post_warning += self._make_destructive_diff_warning()

            if pre_warning or post_warning:
                result["output"] = f"{pre_warning}{result.get('output', '')}{post_warning}"

            return result

        except Submitted:
            # mini-swe-agent 的正常提交信号，不是错误。
            # 必须重新抛出，让 agent.run() 得到 exit_status="Submitted"。
            logger.info("检测到任务提交信号: %s", command[:200])
            raise

        except Exception as e:
            logger.exception("Retrieval bash 执行异常: %s", command[:100])
            return self._make_output(
                f"[bash execution error] {type(e).__name__}: {e}",
                returncode=1,
                exception_info=f"{type(e).__name__}: {e}",
            )

    def _execute_retrieval(self, action: dict) -> dict:
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

        if tool_name in self.retrieval_call_counts:
            self.retrieval_call_counts[tool_name] += 1

        return self._make_output(result_text, returncode, exception_info)

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------

    def _guard_submit_command(self, command: str) -> dict | None:
        canonical_submit = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
        required_diff_command = 'cd "$REPO_ROOT" && git diff'

        if command != canonical_submit:
            logger.warning("阻止提交：非标准提交命令: %s", command)
            return self._make_output(
                "[policy error] Submit command must be exactly:\n"
                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n\n"
                "Do not quote it, combine it with other commands, wrap it in printf, "
                "or use any other variant.\n"
                "The submission command must be issued alone.",
                returncode=1,
            )

        if self._last_git_diff_empty is None:
            logger.warning("阻止提交：提交前没有运行有效 git diff")
            return self._make_output(
                "[policy error] Cannot submit because you have not inspected the final visible working-tree diff.\n\n"
                "Run exactly this command next:\n"
                f"{required_diff_command}\n\n"
                "Do not redirect, pipe, wrap, count, save, or combine the git diff command.\n"
                "Do not use historical diffs such as `git diff HEAD~1`, `git show`, or `git diff HEAD`.\n"
                "The diff output must be visible in the observation.\n"
                "After you inspect a non-empty, relevant, minimal, non-destructive diff, submit with:\n"
                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                returncode=1,
            )

        if self._last_git_diff_empty is True:
            logger.warning("阻止提交：最近一次 git diff 为空")
            return self._make_output(
                "[policy error] Cannot submit because the last visible working-tree diff was empty.\n\n"
                "This means no tracked source-code change is currently visible to git.\n"
                "Your next action must inspect source or edit tracked source files, then run exactly:\n"
                f"{required_diff_command}\n\n"
                "Do not redirect, pipe, wrap, count, save, or combine the git diff command.\n"
                "The diff output must be visible in the observation.",
                returncode=1,
            )

        # 与 baseline 对齐：destructive diff 先不硬拦截，只给 warning。
        # 如果以后要更严格，可以在这里 return policy error。
        return None

    @staticmethod
    def _is_blocked_sed_i_command(command: str) -> bool:
        """
        拦截 sed -i / sed --in-place。

        不拦截：
          sed -n '10,80p'
          nl -ba file.py | sed -n '10,80p'
          grep ... | sed ...
        """
        if not command:
            return False

        parts = re.split(r"\s*(?:&&|\|\||;)\s*", command)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            if not re.match(r"^(?:sudo\s+)?sed\b", part):
                continue

            if re.search(r"(?<!\S)-i(?:\S*)?(?!\S)", part):
                return True

            if re.search(r"(?<!\S)--in-place(?:=\S+)?(?!\S)", part):
                return True

        return False

    @staticmethod
    def _has_pipefail(command: str) -> bool:
        return (
            "set -o pipefail" in command
            or "set -eo pipefail" in command
            or "set -euo pipefail" in command
        )

    @staticmethod
    def _needs_pipefail(command: str) -> bool:
        """
        只拦截重要验证/安装命令通过 head/tail 截断的情况。
        普通 grep | head 不拦截。

        为什么拦截：
        pytest / pip / django test 等命令接 `| head` 或 `| tail` 时，
        shell 默认返回最后一个命令 head/tail 的退出码。
        这会导致测试/安装实际失败，但 observation 显示 returncode=0。
        """
        lowered = command.lower()

        if not re.search(r"\|\s*(head|tail)\b", lowered):
            return False

        important_patterns = [
            # pytest
            r"\bpytest\b",
            r"\bpython(?:3)?\s+-m\s+pytest\b",

            # Django test runners
            r"\bpython(?:3)?\s+-m\s+django\s+test\b",
            r"\bdjango-admin\s+test\b",
            r"\bmanage\.py\s+test\b",
            r"\bpython(?:3)?\s+manage\.py\s+test\b",
            r"\btests/runtests\.py\b",
            r"\bpython(?:3)?\s+tests/runtests\.py\b",

            # install / env validation
            r"\bpip\s+install\b",
            r"\bpython(?:3)?\s+-m\s+pip\s+install\b",

            # tox
            r"\btox\b",
        ]

        return any(re.search(pattern, lowered) for pattern in important_patterns)

    @staticmethod
    def _looks_like_wrong_workspace_root(command: str) -> bool:
        """
        仅提示，不拦截。

        目标：
          发现 agent 把 /workspace 当 repo root 使用。
          正确 repo root 通常是 $REPO_ROOT 或 /workspace/repo。

        不拦截 ls /workspace 这类诊断命令。
        """
        stripped = command.strip()

        # 明显诊断命令，不提示。
        if re.match(r"^ls\s+/?workspace/?\s*$", stripped):
            return False
        if re.match(r"^find\s+/workspace\b", stripped):
            return False

        # 明显错误路径。
        wrong_path_patterns = [
            r"/workspace/django\b",
            r"/workspace/tests\b",
            r"/workspace/setup\.py\b",
            r"/workspace/pyproject\.toml\b",
        ]
        if any(re.search(p, command) for p in wrong_path_patterns):
            return True

        # 在 /workspace 下跑源码测试/git/编辑。
        if re.search(r"cd\s+/workspace\s*&&", command):
            important_after_cd = [
                r"\bgit\s+diff\b",
                r"\bgit\s+status\b",
                r"\bpython(?:3)?\s+tests/runtests\.py\b",
                r"\bpython(?:3)?\s+-m\s+pytest\b",
                r"\bpytest\b",
                r"\bpython(?:3)?\s+setup\.py\b",
                r"\bpython(?:3)?\s+<<['\"]?PY",
                r"\bpython(?:3)?\s+-c\b",
                r"\bgrep\b.*\bdjango/",
                r"\bnl\s+-ba\b.*\bdjango/",
            ]
            return any(re.search(p, command) for p in important_after_cd)

        return False

    @staticmethod
    def _is_git_diff_command(command: str) -> bool:
        """
        只识别“可见 working-tree diff”。

        允许：
        git diff
        git diff path/to/file.py
        git diff -- path/to/file.py
        cd /workspace/repo && git diff
        cd "$REPO_ROOT" && git diff
        bash -c 'git diff'
        bash -lc 'cd "$REPO_ROOT" && git diff'

        不允许记录为有效 final diff：
        git diff HEAD~1
        git diff HEAD
        git show
        git diff --exit-code
        git diff >/dev/null
        git diff 1>/dev/null
        git diff | wc -c
        git diff | head
        git diff > /tmp/diff.txt
        python -c "... git diff ..."
        """
        command = command.strip()
        if not command:
            return False

        # Python/subprocess 包装的 git diff 不算，因为模型没有直接执行可见 diff。
        if re.search(r"\bpython(?:3)?\b.*\bgit\s+diff\b", command, flags=re.DOTALL):
            return False

        # git show 不是 working-tree diff。
        if re.search(r"(^|[;&|]\s*)git\s+show(?:\s|$)", command):
            return False

        # 如果出现管道或重定向，不算有效 final diff。
        # 目标是要求 diff 内容直接出现在 observation 中。
        if re.search(r"(\||>|<|\b1>|\b2>)", command):
            return False

        # bash -c / bash -lc 包装：递归检查内部命令。
        bash_match = re.match(
            r"""^bash\s+-(?:c|lc)\s+(['"])(?P<inner>.*)\1\s*$""",
            command,
            flags=re.DOTALL,
        )
        if bash_match:
            inner = bash_match.group("inner").strip()
            return RetrievalAgent._is_git_diff_command(inner)

        # 支持 `cd ... && git diff`；但不支持 `git diff && echo ...`
        # 因为 final diff 不应该和其他命令组合。
        parts = [part.strip() for part in re.split(r"\s*&&\s*", command) if part.strip()]

        if len(parts) == 1:
            return RetrievalAgent._is_visible_working_tree_git_diff(parts[0])

        if len(parts) == 2:
            cd_part, diff_part = parts
            if re.match(r"^cd\s+.+$", cd_part) and RetrievalAgent._is_visible_working_tree_git_diff(diff_part):
                return True

        return False

    @staticmethod
    def _is_visible_working_tree_git_diff(part: str) -> bool:
        """
        判断单个 command part 是否是可见 working-tree git diff。
        """
        part = part.strip()

        if not re.match(r"^git\s+diff(?:\s|$)", part):
            return False

        # 禁止历史 diff / commit diff。
        forbidden_patterns = [
            r"\bHEAD\b",
            r"\bHEAD~\d*\b",
            r"\bHEAD\^\b",
            r"\b[a-f0-9]{7,40}\b",
            r"\.\.\.?[^\s]+",          # A..B / A...B
            r"--cached\b",
            r"--staged\b",
            r"--exit-code\b",
            r"--quiet\b",
            r"--name-only\b",
            r"--name-status\b",
            r"--stat\b",
            r"--shortstat\b",
            r"--summary\b",
            r"--check\b",
        ]

        if any(re.search(pattern, part) for pattern in forbidden_patterns):
            return False

        # 允许：
        #   git diff
        #   git diff path
        #   git diff -- path
        #
        # 注意：这里不做过度解析，只要没有 forbidden pattern，就视为可见 working-tree diff。
        return True

    @staticmethod
    def _important_command_returned_zero_but_output_failed(command: str, result: dict) -> bool:
        """
        对重要验证/安装命令做二次检查。

        有些命令因为 `| head` / `| tail`、`|| true`、包装脚本等原因，
        可能 returncode=0，但输出里已经包含 ERROR/FAILED/Traceback。
        这种情况应该在 observation 里提醒 LLM：不要把它当作成功。
        """
        returncode = int(result.get("returncode", 0) or 0)
        if returncode != 0:
            return False

        lowered_command = command.lower()
        important_patterns = [
            # pytest
            r"\bpytest\b",
            r"\bpython(?:3)?\s+-m\s+pytest\b",

            # Django test runners
            r"\bpython(?:3)?\s+-m\s+django\s+test\b",
            r"\bdjango-admin\s+test\b",
            r"\bmanage\.py\s+test\b",
            r"\bpython(?:3)?\s+manage\.py\s+test\b",
            r"\btests/runtests\.py\b",
            r"\bpython(?:3)?\s+tests/runtests\.py\b",

            # install / env validation
            r"\bpip\s+install\b",
            r"\bpython(?:3)?\s+-m\s+pip\s+install\b",

            # tox
            r"\btox\b",
        ]

        important = any(re.search(pattern, lowered_command) for pattern in important_patterns)
        if not important:
            return False

        output = str(result.get("output", "") or "")

        failure_markers = [
            "Traceback (most recent call last)",
            "ImportError:",
            "ModuleNotFoundError:",
            "RuntimeError:",
            "django.core.exceptions.ImproperlyConfigured",
            "ERROR:",
            "FAILED",
            "FAILED (",
            "FAILED tests",
            "FAIL:",
            "FAILURES",
            "ERRORS",
            "metadata-generation-failed",
            "Encountered error while generating package metadata",
            "No such file or directory",
            "command not found",
        ]

        return any(marker in output for marker in failure_markers)

    @staticmethod
    def _looks_like_destructive_diff(diff_text: str) -> bool:
        """
        检测明显破坏性 diff：
          - 文件变成空文件
          - 大规模删除且几乎没有新增
          - 删除大量行
        """
        if not diff_text.strip():
            return False

        # Git 空文件 blob hash，常见于文件被清空。
        if "e69de29bb" in diff_text:
            return True

        # hunk 形如 @@ -1,317 +0,0 @@
        if re.search(r"@@\s+-\d+,\d+\s+\+0,0\s+@@", diff_text):
            return True

        deleted = 0
        added = 0
        for line in diff_text.splitlines():
            if line.startswith("---") or line.startswith("+++"):
                continue
            if line.startswith("-"):
                deleted += 1
            elif line.startswith("+"):
                added += 1

        # 保守阈值：删除很多，新增很少。
        if deleted >= 80 and added <= 5:
            return True

        # 删除明显远多于新增。
        if deleted >= 120 and deleted >= 10 * max(added, 1):
            return True

        return False

    # ------------------------------------------------------------------
    # Message/content/result utilities
    # ------------------------------------------------------------------

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
    def _normalize_result(result: Any) -> dict:
        if isinstance(result, dict):
            result.setdefault("output", "")
            result.setdefault("returncode", 0)
            result.setdefault("exception_info", "")
            return result

        return {
            "output": str(getattr(result, "output", "") or ""),
            "returncode": int(getattr(result, "returncode", 0) or 0),
            "exception_info": str(getattr(result, "exception_info", "") or ""),
        }

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

        if message.get("role") != "assistant":
            return

        extra = message.get("extra", {}) or {}
        actions = extra.get("actions", [])

        has_tool_calls = bool(message.get("tool_calls")) or bool(actions)
        if not has_tool_calls:
            return

        cls._clear_message_content(message)

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

    # ------------------------------------------------------------------
    # Warning / policy message builders
    # ------------------------------------------------------------------

    @staticmethod
    def _make_sed_i_block_message(command: str) -> str:
        return (
            "[policy error] The command was blocked because it uses `sed -i` to edit files.\n\n"
            "Why this is blocked:\n"
            "- `sed -i` can silently do nothing when the pattern does not match.\n"
            "- Multi-line `sed -i` replacements are especially unreliable.\n"
            "- A zero return code from sed does NOT prove that the file was modified.\n\n"
            "Use a safer editing method instead:\n"
            "1. Use a Python heredoc script with pathlib to read the target file.\n"
            "2. Define exact `old` and `new` text blocks.\n"
            "3. Before replacing, assert that the old text exists: `assert old in text`.\n"
            "4. Write the updated text back with `path.write_text(...)`.\n"
            "5. Immediately run git diff to verify the change is non-empty.\n\n"
            "Recommended pattern:\n"
            "python3 <<'PY'\n"
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

    @staticmethod
    def _make_pipefail_block_message(command: str) -> str:
        return (
            "[policy error] This command was blocked because it pipes an important "
            "validation/install command through `head` or `tail` without preserving the real exit code.\n\n"
            "Why this is blocked:\n"
            "- Shell pipelines normally return the exit code of the last command.\n"
            "- With `pytest ... | tail`, `python -m django test ... | tail`, or `pip install ... | head`, "
            "the final returncode may be 0 even when the test/install command failed.\n"
            "- This can make the agent incorrectly believe validation succeeded.\n\n"
            "Use `set -o pipefail;` so failures from pytest, Django tests, pip, tox, or tests/runtests.py "
            "are not hidden by head/tail.\n\n"
            "Bad:\n"
            "python -m django test app.tests.TestCase 2>&1 | tail -80\n\n"
            "Good:\n"
            "set -o pipefail; python -m django test app.tests.TestCase 2>&1 | tail -80\n\n"
            "Use the repository's own focused test runner when available; otherwise use the generic framework command with `set -o pipefail;` if piping through head/tail.\n"
            "set -o pipefail; python tests/runtests.py app_label.test_module.TestClass 2>&1 | tail -80\n\n"
            "Blocked command:\n"
            f"{command}"
        )

    @staticmethod
    def _make_workspace_root_warning(command: str) -> str:
        return (
            "[workspace warning] This command appears to use `/workspace` as if it were the repository root.\n"
            "The mounted repository root is usually `$REPO_ROOT` or `/workspace/repo`.\n"
            "For source edits, tests, and git commands, prefer:\n"
            "cd \"$REPO_ROOT\" && ...\n\n"
            "This is a warning only; the command was still executed.\n\n"
        )

    @staticmethod
    def _make_failed_output_warning() -> str:
        return (
            "\n\n[validation warning] This command returned code 0, but the output contains failure markers "
            "such as Traceback, ImportError, ERROR, FAILED, metadata-generation-failed, or No such file or directory.\n"
            "Treat this validation/install command as failed. Do not claim the test/install succeeded based only on returncode 0.\n"
        )

    @staticmethod
    def _make_destructive_diff_warning() -> str:
        return (
            "\n\n[diff warning] The git diff appears potentially destructive. It may have emptied a file "
            "or deleted a large amount of source with little replacement.\n"
            "Do not submit until you inspect the diff carefully and confirm the change is intentional and minimal.\n"
        )

    def serialize(self, *extra_dicts) -> dict:
        """在序列化时追加检索工具调用统计和 guardrail 状态。"""
        data = super().serialize(*extra_dicts)

        data.setdefault("info", {})

        data["info"]["retrieval_stats"] = {
            "call_counts": self.retrieval_call_counts,
            "total_calls": sum(self.retrieval_call_counts.values()),
        }
        try:
            from mini_swe_agent_integration.retrieval_tools import get_deepen_stats
            data["info"]["deepen_stats"] = get_deepen_stats()
        except Exception as e:
            data["info"]["deepen_stats"] = {
                "error": f"{type(e).__name__}: {e}"
            }

        # 兼容旧字段。
        data["info"]["blocked_import_reproduction"] = {
            module: {
                "reason": info.get("reason", ""),
                "count": info.get("count", 0),
            }
            for module, info in self._failed_import_modules.items()
        }

        data["info"]["last_git_diff_empty"] = self._last_git_diff_empty
        data["info"]["last_git_diff_destructive"] = self._last_git_diff_destructive
        data["info"].update(self._convergence_guard.serialize_state())

        data["info"]["retrieval_agent_guardrails"] = {
            "sanitize_toolcall_content": True,
            "block_sed_i": True,
            "require_pipefail_for_test_pipes": True,
            "require_standard_submit_command": True,
            "require_nonempty_git_diff_before_submit": True,
            "warn_wrong_workspace_root": True,
            "warn_returncode_zero_with_failure_markers": True,
            "warn_destructive_git_diff": True,
            "support_retrieval_tools": True,
            **self._convergence_guard.guardrail_flags(),
        }

        return data