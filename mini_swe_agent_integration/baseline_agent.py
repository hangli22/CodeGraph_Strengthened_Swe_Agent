"""
baseline_agent.py — Baseline 模式下的 bash-tool Agent

职责：
  - baseline 仍然只允许 bash tool，不提供 retrieval 工具
  - 保留 DefaultAgent 支持多个 bash action 的行为
  - 对每个 bash action 单独应用 guardrails
  - 清洗 tool-call assistant message 的自然语言 content
  - 提交前要求 git diff 非空
"""

from __future__ import annotations

import logging
import re
from typing import Any

from minisweagent.agents.default import DefaultAgent, AgentConfig
from minisweagent.exceptions import Submitted

logger = logging.getLogger(__name__)


class BaselineAgentConfig(AgentConfig):
    """与 AgentConfig 相同，暂不增加字段。"""
    pass


class BaselineAgent(DefaultAgent):
    """
    Baseline agent:
      - 只执行 bash action
      - 允许一轮多个 bash action，尽量贴近 DefaultAgent 行为
      - 清空 tool-call assistant content
      - 拦截危险/不可靠命令
      - 对可疑环境/测试输出追加 warning
      - 提交前要求 git diff 非空
    """

    def __init__(self, model, env, *, config_class=BaselineAgentConfig, **kwargs):
        super().__init__(model, env, config_class=config_class, **kwargs)

        # 最近一次 git diff 是否为空：
        #   None = 从未运行 git diff
        #   True = 最近一次 git diff 为空
        #   False = 最近一次 git diff 非空
        self._last_git_diff_empty: bool | None = None

        # 最近一次 git diff 是否看起来有破坏性。
        self._last_git_diff_destructive: bool = False

    def execute_actions(self, message: dict) -> list[dict]:
        """
        执行 LLM 返回的 actions。

        与 DefaultAgent 的区别：
          1. 清洗 assistant content
          2. 只允许 bash command action
          3. 每个 command 先过 guardrail
          4. 执行结果根据输出追加 warning
        """
        actions = message.get("extra", {}).get("actions", [])

        # baseline 使用 LitellmModel 的 bash tool call。
        # 如果模型返回 content + tool call，把 content 清空，避免污染上下文/trajectory。
        self._sanitize_toolcall_assistant_message(message)

        if not actions:
            return super().execute_actions(message)

        outputs: list[dict] = []

        for action in actions:
            if "command" not in action:
                outputs.append(
                    self._make_output(
                        "[policy error] Baseline mode only supports the bash tool. "
                        "Use bash tool calls only.",
                        returncode=1,
                    )
                )
                continue

            outputs.append(self._execute_bash(action))

        # 格式化 observation 前再次清洗，防止原始 response 恢复 content。
        self._sanitize_toolcall_assistant_message(message)

        return self.add_messages(
            *self.model.format_observation_messages(
                message,
                outputs,
                self.get_template_vars(),
            )
        )

    def _execute_bash(self, action: dict) -> dict:
        command = action["command"]
        command_stripped = command.strip()

        logger.info("Baseline bash 执行: %s", command[:200])

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
        # pytest/pip | head/tail 没有 pipefail 硬拦截
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
            logger.info("检测到任务提交信号: %s", command[:200])
            raise

        except Exception as e:
            logger.exception("Baseline bash 执行异常: %s", command[:100])
            return self._make_output(
                f"[bash execution error] {type(e).__name__}: {e}",
                returncode=1,
                exception_info=f"{type(e).__name__}: {e}",
            )

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

        # 这里不硬拦截 destructive diff，只给 warning。
        # 如果以后想更强，可以在这里 return policy error。
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
            return BaselineAgent._is_git_diff_command(inner)

        # 支持 `cd ... && git diff`；但不支持 `git diff && echo ...`
        # 因为 final diff 不应该和其他命令组合。
        parts = [part.strip() for part in re.split(r"\s*&&\s*", command) if part.strip()]

        if len(parts) == 1:
            return BaselineAgent._is_visible_working_tree_git_diff(parts[0])

        if len(parts) == 2:
            cd_part, diff_part = parts
            if re.match(r"^cd\s+.+$", cd_part) and BaselineAgent._is_visible_working_tree_git_diff(diff_part):
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
    # Message/content utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _make_output(
        output: str,
        returncode: int = 0,
        exception_info: str = "",
    ) -> dict:
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
        if isinstance(result, dict):
            return str(result.get("output", "") or "")
        return str(getattr(result, "output", "") or "")

    @staticmethod
    def _clear_message_content(obj: Any) -> None:
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
        清洗 tool-calling assistant message：
          1. message["content"] = None
          2. extra.response.choices[0].message.content = None
          3. extra.response["choices"][0]["message"]["content"] = None
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

        # object style
        try:
            choices = getattr(response, "choices", None)
            if choices:
                resp_msg = choices[0].message
                cls._clear_message_content(resp_msg)
        except Exception:
            pass

        # dict style
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
        data = super().serialize(*extra_dicts)

        data.setdefault("info", {})
        data["info"]["last_git_diff_empty"] = self._last_git_diff_empty
        data["info"]["last_git_diff_destructive"] = self._last_git_diff_destructive
        data["info"]["baseline_agent_guardrails"] = {
            "sanitize_toolcall_content": True,
            "block_sed_i": True,
            "require_pipefail_for_test_pipes": True,
            "require_standard_submit_command": True,
            "require_nonempty_git_diff_before_submit": True,
            "warn_wrong_workspace_root": True,
            "warn_returncode_zero_with_failure_markers": True,
            "warn_destructive_git_diff": True,
            "allow_multiple_bash_actions": True,
        }

        return data