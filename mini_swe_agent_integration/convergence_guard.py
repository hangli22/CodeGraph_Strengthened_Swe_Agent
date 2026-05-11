"""
convergence_guard.py — RetrievalAgent 的步数提醒与收敛控制
=========================================================

职责：
  - 多 tool call 硬拦截
  - 30 步后阻止 broad search
  - 40 步后只允许 edit / focused test / small range read / git diff / submit
  - 45 步后进入终局模式，只允许 edit / focused test / git diff / submit
  - 找到直接错误位置后，最多再允许 5 次 inspection/search
  - 源码修改成功后，提醒完成一个自洽修复批次后 git diff；
    最多连续 2 次 edit 不 diff；diff 合理后 focused test -> submit
  - 记录收敛相关状态，供 trajectory serialize 使用

注意：
  - 这个模块不执行工具，只判断 action 是否允许。
  - 这个模块不依赖 mini-swe-agent 内部类型，只操作 dict。

现在是每次调用工具都返回提醒吗？
只有编辑提醒

增加20步柔和收敛提醒
不要求每次修改后diff，要求完成自洽修改后diff，最多两次edit不diff



"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)


MakeOutputFn = Callable[[str, int, str], dict]
IsGitDiffCommandFn = Callable[[str], bool]


class ConvergenceGuard:
    """
    RetrievalAgent 的收敛控制器。

    Parameters
    ----------
    make_output:
        RetrievalAgent._make_output，用于构造 observation output。
    is_git_diff_command:
        RetrievalAgent._is_git_diff_command，用于判断有效 visible working-tree diff。
    step_notice_interval:
        每多少步检查一次 progress notice。
        注意：25 步不会输出柔和提醒；30/40 是关键收敛提醒；
        45 步后按 step_notice_interval 重复终局提醒。
        57最后提醒

    """

    def __init__(
        self,
        *,
        make_output: MakeOutputFn,
        is_git_diff_command: IsGitDiffCommandFn,
        step_notice_interval: int = 5,
    ):
        self._make_output = make_output
        self._is_git_diff_command = is_git_diff_command

        self.interaction_step_count: int = 0
        self.step_notice_interval: int = step_notice_interval

        self.source_edit_count: int = 0
        self.direct_error_location_found: bool = False
        self.direct_error_location_step: Optional[int] = None
        self.post_error_location_inspection_count: int = 0

    # ------------------------------------------------------------------
    # Multi tool-call guard
    # ------------------------------------------------------------------

    def guard_multiple_tool_calls(self, message: dict, actions: list[dict]) -> list[dict] | None:
        """
        如果一轮 assistant response 中出现多个 tool call，直接拒绝。

        返回：
          - None：没有违反多 tool call 约束
          - list[dict]：policy error outputs，数量与 parsed actions 对齐
        """
        raw_tool_call_count = self.count_raw_tool_calls(message)
        parsed_action_count = len(actions)

        if raw_tool_call_count <= 1 and parsed_action_count <= 1:
            return None

        msg = self.make_multi_tool_call_block_message(
            raw_tool_call_count=raw_tool_call_count,
            parsed_action_count=parsed_action_count,
        )

        # 尽量与 parsed actions 数量对齐，避免 assistant tool_calls 和 tool observations 数量不一致。
        output_count = max(1, parsed_action_count)
        return [
            self._make_output(msg, 1, "")
            for _ in range(output_count)
        ]

    @staticmethod
    def count_raw_tool_calls(message: dict) -> int:
        """
        统计 raw tool_calls 数量。

        需要同时看：
          - message["tool_calls"]
          - message["extra"]["response"].choices[0].message.tool_calls
          - message["extra"]["response"]["choices"][0]["message"]["tool_calls"]

        因为不同模型/SDK 包装形式不同。
        """
        counts: list[int] = []

        try:
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                counts.append(len(tool_calls))
        except Exception:
            pass

        response = (message.get("extra") or {}).get("response")

        try:
            choices = getattr(response, "choices", None)
            if choices:
                raw_msg = choices[0].message
                raw_tool_calls = getattr(raw_msg, "tool_calls", None) or []
                if raw_tool_calls:
                    counts.append(len(raw_tool_calls))
        except Exception:
            pass

        try:
            choices = response.get("choices", []) if response else []
            if choices:
                raw_msg = choices[0].get("message", {}) or {}
                raw_tool_calls = raw_msg.get("tool_calls") or []
                if raw_tool_calls:
                    counts.append(len(raw_tool_calls))
        except Exception:
            pass

        return max(counts) if counts else 0

    @staticmethod
    def make_multi_tool_call_block_message(
        raw_tool_call_count: int,
        parsed_action_count: int,
    ) -> str:
        return (
            "[policy error] Multiple tool calls were detected in one assistant response, "
            "so no tool call was executed.\n\n"
            f"raw_tool_call_count={raw_tool_call_count}, "
            f"parsed_action_count={parsed_action_count}\n\n"
            "You must retry with exactly ONE tool call in the next assistant response.\n"
            "Do not call a retrieval tool and bash in the same turn.\n"
            "Do not make parallel tool calls.\n"
            "If you need multiple actions, perform them across multiple turns.\n"
        )

    def advance_step_and_maybe_make_notice(self) -> str:
        """
        每完成一轮 assistant tool action，交互步数 +1。

        关键提醒：
        - 30 步：禁止 broad search
        - 40 步：strict convergence
        - 45 步及之后：终局模式
        - 57 步：最终提交提醒；如果已有修改，必须马上 submit

        注意：
        - 30 步前不再输出柔和提醒。
        """
        self.interaction_step_count += 1

        if self.step_notice_interval <= 0:
            return ""

        step = self.interaction_step_count

        # 30 步前不提醒。
        if step < 30:
            return ""

        # 30、40、57 是关键单点提醒。
        if step in {30, 40, 57}:
            return self.make_step_notice_message(step)

        # 45 步后进入终局模式，每 step_notice_interval 步重复强提醒。
        # 避免和 57 步单点最终提醒重复。
        if step >= 45 and step % self.step_notice_interval == 0:
            return self.make_step_notice_message(step)

        return ""

    @staticmethod
    def make_step_notice_message(step_count: int) -> str:
        if step_count >= 57:
            stage = (
                "【最终提交提醒】已经到达 57 步，距离 60 步上限极近。\n"
                "如果已经有任何源码修改，必须马上提交最终结果。\n"
                "不要再搜索，不要再读取代码，不要再运行新的测试，不要再扩大修改范围。\n"
                "如果已有 diff 且没有明显语法错误，应立即 submit。\n"
                "如果刚才 focused test 通过、无法运行、或被 policy/environment 阻断，也应立即 submit。\n"
                "只有在完全没有源码修改时，才允许做一次最小源码编辑；编辑后不要再验证，直接提交。"
            )
        elif step_count >= 45:
            stage = (
                "【终局模式】已经到达 45 步或更多，不能再浪费任何一步。。\n"
                "现在只能做收敛动作：最小源码编辑、focused test/reproduction、git diff、最终提交。\n"
                "不要再 retrieval，不要再 broad grep，不要再读整文件，不要再继续查同一概念。\n"
                "如果已经有源码修改，必须按这个顺序推进：git diff -> 最多一次 focused test -> submit。\n"
                "如果 focused test 因环境、依赖、收集错误或 policy 被阻断，不要继续调环境，应该回到 git diff 并提交当前最小修复。"
            )
        elif step_count >= 40:
            stage = (
                "【严格收敛模式】已经到达 40 步。\n"
                "现在必须进入 修改 -> git diff -> focused test -> submit 的闭环。\n"
                "只允许：最小源码编辑、focused test/reproduction、小范围读取、git diff、最终提交。\n"
                "不要再扩大搜索范围，不要再读无关文件，不要再重复验证同一事实。\n"
                "如果已经做过源码修改，下一步优先 git diff。"
            )
        elif step_count >= 30:
            stage = (
                "【强制收敛提醒】已经到达 30 步。\n"
                "从现在开始禁止 broad search。\n"
                "不要再调用 search_hybrid/search_bm25/search_semantic/search_structural/deepen_file。\n"
                "不要再 broad grep，不要再整文件读取。\n"
                "如果已经找到直接报错位置、相关测试或核心函数，必须开始准备最小修改。"
            )
        else:
            stage = (
                "【柔和收敛提醒】已经到达 25 步。\n"
                "请开始收敛到最可能的修复路径。\n"
                "如果已经有候选文件、候选函数或相关测试，不要继续扩大搜索范围。\n"
                "优先选择：小范围读取关键代码、运行 focused reproduction/test、或准备最小源码修改。\n"
                "仍允许必要的检索，但不要重复搜索同一概念。"
            )

        if step_count >= 57:
            next_steps = (
                "最终执行顺序：\n"
                "1. 已有源码修改：立即 submit，不要再验证。\n"
                "2. 已有 diff 但测试失败或被阻断：提交当前最小修复。\n"
                "3. 完全没有源码修改：只允许一次最小编辑，然后立即 submit。\n"
            )
        elif step_count >= 30:
            next_steps = (
                "硬性执行顺序：\n"
                "1. 没有修改：小范围确认后立刻做最小源码编辑。\n"
                "2. 已有源码修改：完成一个自洽修复批次后查看 `git diff`。\n"
                "3. diff 合理：最多运行一次 focused test/reproduction。\n"
                "4. focused test 通过、无法运行、或被 policy/environment 阻断：不要继续消耗步数，提交当前最小修复。\n"
            )
        else:
            next_steps = (
                "建议执行顺序：\n"
                "1. 若仍不确定修复点：只做一次最有价值的小范围确认。\n"
                "2. 若已有候选修复点：构造 focused reproduction/test 或直接做最小源码修改。\n"
                "3. 避免继续 broad search、整文件读取、重复 grep 同一概念。\n"
            )

        return (
            "\n\n[progress notice / 收敛提醒]\n"
            f"已经交互了 {step_count} 步。\n\n"
            f"{stage}\n\n"
            f"{next_steps}"
        )

    @staticmethod
    def append_notice_to_outputs(outputs: list[dict], notice: str) -> None:
        """
        将 step notice 追加到最后一个 observation output 中。

        不单独新增 output，避免破坏 actions/observations 对齐。
        """
        if not notice:
            return

        if not outputs:
            outputs.append({
                "output": notice,
                "returncode": 0,
                "exception_info": "",
            })
            return

        last = outputs[-1]
        last["output"] = f"{last.get('output', '')}{notice}"

    # ------------------------------------------------------------------
    # Convergence policy
    # ------------------------------------------------------------------

    def guard_action(self, action: dict) -> dict | None:
        """
        根据当前 step 和定位状态，阻止低收益探索。

        规则：
          - 30 步后禁止 broad search。
          - 40 步后只允许 edit / focused test / small range read / git diff / submit。
          - 45 步后只允许 edit / focused test / git diff / submit。
          - 找到直接错误行后最多再查 5 步；之后必须 edit / test / diff / submit。
        """
        step = self.interaction_step_count + 1

        if step >= 45 and not self.is_allowed_after_step_45(action):
            return self._make_output(
                self.make_step_45_block_message(action),
                1,
                "",
            )

        if step >= 40 and not self.is_allowed_after_step_40(action):
            return self._make_output(
                self.make_step_40_block_message(action),
                1,
                "",
            )

        if step >= 30 and self.is_broad_search_action(action):
            return self._make_output(
                self.make_step_30_broad_search_block_message(action),
                1,
                "",
            )

        if self.direct_error_location_found:
            if self.is_inspection_or_search_action(action) and not self.is_source_edit_or_test_or_diff_or_submit_action(action):
                if self.post_error_location_inspection_count >= 5:
                    return self._make_output(
                        self.make_direct_error_convergence_block_message(action),
                        1,
                        "",
                    )
                self.post_error_location_inspection_count += 1

        return None

    def update_after_outputs(self, action: dict, outputs: list[dict]) -> None:
        """
        根据本轮 action 和 observation 更新收敛状态。
        """
        for output in outputs:
            self.update_direct_error_location_state(action, output)

    def update_direct_error_location_state(self, action: dict, output: dict) -> None:
        """
        根据 observation 判断是否已经找到了直接错误位置。

        注意：
          - 不因为 tests/ 里的 Error(...) 单独触发。
          - 优先在源码文件 django/ 或 grep 输出中的 django/*.py 行触发。
          - policy error 本身不触发。
        """
        if self.direct_error_location_found:
            return

        text = str(output.get("output", "") or "")
        if not text:
            return

        if text.lstrip().startswith("[policy error]"):
            return

        command = ""
        if "command" in action:
            command = str(action.get("command") or "")

        command_targets_tests = self._command_targets_tests(command)
        command_targets_source = self._command_targets_source(command)
        output_points_to_source = self._output_points_to_source_file(text)

        # 读测试时，不因为 Error(...) / checks.Error(...) 直接触发。
        if command_targets_tests and not command_targets_source and not output_points_to_source:
            return

        strong_patterns = [
            r"\bid=['\"][a-zA-Z_]+\.E\d+['\"]",
            r"\bmodels\.E\d+\b",
            r"\bfields\.E\d+\b",
            r"\badmin\.E\d+\b",
            r"db_table .* is used by multiple models",
            r"is used by multiple models",
            r"Traceback \(most recent call last\)",
            r"AssertionError",
            r"FAILED",
        ]

        if not any(re.search(p, text) for p in strong_patterns):
            return

        # 如果是源码范围读取，或者 grep 输出直接指向源码文件，认为找到直接错误位置。
        if command_targets_source or output_points_to_source:
            self.direct_error_location_found = True
            self.direct_error_location_step = self.interaction_step_count
            self.post_error_location_inspection_count = 0
            logger.info(
                "检测到直接错误/失败位置，step=%s",
                self.direct_error_location_step,
            )

    @staticmethod
    def _command_targets_tests(command: str) -> bool:
        return bool(re.search(r"(^|\s)(tests?/|tests\b)", command))

    @staticmethod
    def _command_targets_source(command: str) -> bool:
        # 当前主要面向 Django；其他仓库则通过 output_points_to_source_file 辅助触发。
        return bool(re.search(r"(^|\s)(django/|django\b)", command))

    @staticmethod
    def _output_points_to_source_file(text: str) -> bool:
        # grep 输出形如 django/core/checks/model_checks.py:45:
        return bool(re.search(r"(^|\n)\.?/?[A-Za-z0-9_./-]+\.py:\d+:", text)) and not bool(
            re.search(r"(^|\n)\.?/?tests?/", text)
        ) or bool(re.search(r"(^|\n)\.?/?django/[A-Za-z0-9_./-]+\.py:\d+:", text))

    def is_allowed_after_step_40(self, action: dict) -> bool:
        """
        40 步后只允许：
          - source edit
          - focused reproduction/test
          - small range read
          - git diff
          - submit
        """
        if "tool_name" in action:
            return False

        command = (action.get("command") or "").strip()
        if not command:
            return False

        return (
            self.is_likely_source_edit_command(command)
            or self.is_focused_test_or_reproduction_command(command)
            or self.is_small_range_read_command(command)
            or self._is_git_diff_command(command)
            or self.is_submit_command(command)
        )

    def is_allowed_after_step_35(self, action: dict) -> bool:
        # 兼容旧调用；实际 strict convergence 已经调整到 40 步。
        return self.is_allowed_after_step_40(action)

    def is_allowed_after_step_45(self, action: dict) -> bool:
        """
        45 步后进入终局模式，只允许：
          - source edit
          - focused reproduction/test
          - git diff
          - submit

        注意：45 步后不再允许 small range read，避免继续探索导致 limit exceed。
        """
        if "tool_name" in action:
            return False

        command = (action.get("command") or "").strip()
        if not command:
            return False

        return (
            self.is_likely_source_edit_command(command)
            or self.is_focused_test_or_reproduction_command(command)
            or self._is_git_diff_command(command)
            or self.is_submit_command(command)
        )

    def is_broad_search_action(self, action: dict) -> bool:
        """
        判断是否是 30 步后应阻止的 broad search。
        """
        if "tool_name" in action:
            tool_name = action.get("tool_name")
            return tool_name in {
                "search_hybrid",
                "search_bm25",
                "search_semantic",
                "search_structural",
                "deepen_file",
            }

        command = (action.get("command") or "").strip()
        if not command:
            return False

        return self.is_broad_grep_command(command) or self.is_broad_read_command(command)

    def is_inspection_or_search_action(self, action: dict) -> bool:
        if "tool_name" in action:
            return action.get("tool_name") in {
                "search_hybrid",
                "search_bm25",
                "search_semantic",
                "search_structural",
                "deepen_file",
            }

        command = (action.get("command") or "").strip()
        if not command:
            return False

        lowered = command.lower()
        return (
            "grep" in lowered
            or "find " in lowered
            or " rg " in f" {lowered} "
            or "nl -ba" in lowered
            or re.search(r"(^|&&)\s*cat\s+", lowered) is not None
            or "sed -n" in lowered
        )

    def is_source_edit_or_test_or_diff_action(self, action: dict) -> bool:
        return self.is_source_edit_or_test_or_diff_or_submit_action(action)

    def is_source_edit_or_test_or_diff_or_submit_action(self, action: dict) -> bool:
        if "tool_name" in action:
            return False

        command = (action.get("command") or "").strip()
        if not command:
            return False

        return (
            self.is_likely_source_edit_command(command)
            or self.is_focused_test_or_reproduction_command(command)
            or self._is_git_diff_command(command)
            or self.is_submit_command(command)
        )

    @staticmethod
    def is_submit_command(command: str) -> bool:
        """
        判断是否是最终提交命令。

        兼容常见 mini-swe-agent/SWE-agent 提交方式。
        """
        c = command.strip()
        return (
            "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in c
            or "submit_final_output" in c.lower()
            or "submit" == c.lower()
        )

    @staticmethod
    def is_broad_grep_command(command: str) -> bool:
        """
        broad grep: 搜索范围太大，30 步后禁止。
        """
        c = command.strip()

        if not re.search(r"\bgrep\b", c):
            return False

        broad_targets = [
            r"\s\.\s",
            r"\s\.$",
            r"\sdjango/?\s",
            r"\sdjango/?$",
            r"\stests/?\s",
            r"\stests/?$",
            r"--include=.*\*\.",
        ]

        if any(re.search(p, c) for p in broad_targets):
            return True

        if re.search(r"\bgrep\s+-[^\n]*[rR][^\n]*\s+.+\s+(\.|django/?|tests/?)\b", c):
            return True

        return False

    @staticmethod
    def is_broad_read_command(command: str) -> bool:
        """
        full-file read: 读整文件，30 步后禁止。
        """
        c = command.strip()

        if re.search(r"(^|&&|\|\|)\s*cat\s+[^|><;&]+\.py\b", c):
            return True

        if re.search(r"\bnl\s+-ba\s+[^|><;&]+\.py\b", c) and "sed -n" not in c:
            return True

        return False

    @staticmethod
    def is_small_range_read_command(command: str) -> bool:
        """
        允许的小范围读取：
          nl -ba file.py | sed -n '10,120p'

        要求范围最多 140 行。
        """
        c = command.strip()

        m = re.search(
            r"nl\s+-ba\s+[^|><;&]+\.py\s*\|\s*sed\s+-n\s+['\"]?(\d+),(\d+)p['\"]?",
            c,
        )
        if not m:
            return False

        start = int(m.group(1))
        end = int(m.group(2))
        return 0 < start <= end and (end - start) <= 140

    @staticmethod
    def is_focused_test_or_reproduction_command(command: str) -> bool:
        """
        允许的 focused test / reproduction。
        """
        c = command.strip()
        lowered = c.lower()

        if re.search(r"\bpytest\b", lowered):
            return "::" in c or re.search(r"\btests?/[^ ]+\.py\b", c) is not None

        if re.search(r"\bpython(?:3)?\s+tests/runtests\.py\b", lowered):
            return True

        if re.search(r"\bpython(?:3)?\s+-m\s+pytest\b", lowered):
            return "::" in c or re.search(r"\btests?/[^ ]+\.py\b", c) is not None

        if re.search(r"\bpython(?:3)?\s+manage\.py\s+test\b", lowered):
            return True

        # heredoc Python reproduction，不一定是 edit。
        if re.search(r"\bpython(?:3)?\s+<<['\"]?PY", c):
            return True

        return False

    @staticmethod
    def is_likely_source_edit_command(command: str) -> bool:
        """
        判断是否像源码编辑命令。

        收紧判断：
          - Path( 和 .replace( 不能单独算 edit。
          - 必须出现明确写入动作。
        """
        c = command.strip()

        if re.search(r"\bpython(?:3)?\s+<<['\"]?PY", c):
            write_markers = [
                ".write_text(",
                ".write_bytes(",
                ".writelines(",
                ".write(",
                "writelines(",
            ]
            if any(marker in c for marker in write_markers):
                return True

            if re.search(r"open\s*\([^)]*,\s*['\"][wax]\+?['\"]", c):
                return True

            return False

        if re.search(r"\bapply_patch\b", c):
            return True

        if re.search(r"\bgit\s+apply\b", c):
            return True

        return False

    def record_source_edit_if_successful(self, command: str, result: dict) -> None:
        if (
            self.is_likely_source_edit_command(command)
            and int(result.get("returncode", 0) or 0) == 0
        ):
            self.source_edit_count += 1

            # 源码修改成功后追加提醒。
            # 注意：这里不再强制“每次修改后立刻 git diff”，
            # 而是提醒模型在完成一个自洽修复批次后 git diff。
            result["output"] = (
                f"{result.get('output', '')}\n\n"
                "[source edit notice / 修改后提醒]\n"
                "源码已经成功修改。不要继续搜索，不要继续读无关文件。\n"
                "如果本次修改已经完成一个自洽修复批次，下一步查看："
                "`cd \"$REPO_ROOT\" && git diff`。\n"
                "如果只是修复刚才 diff 暴露出的明显小错误，允许最多再做 1 次小编辑，然后必须 git diff。\n"
                "不要连续扩大修改范围；不要在已有 diff 后继续探索无关文件。\n"
                "diff 合理后，最多运行一次 focused test/reproduction。\n"
                "如果测试通过、无法运行、或被 policy/environment 阻断，应提交当前最小修复，避免 step limit exceed。\n"
            )

    @staticmethod
    def make_step_30_broad_search_block_message(action: dict) -> str:
        return (
            "[policy error / 强制收敛] 30 步以后禁止继续宽泛搜索。\n\n"
            "你已经消耗了足够多的定位步数。现在继续 search / deepen / broad grep / 整文件读取，"
            "很可能导致 step limit exceed。\n\n"
            "下一步必须更具体，只能选择：\n"
            "1. 读取一个很小的缺失代码范围；\n"
            "2. 运行一个 focused reproduction/test；\n"
            "3. 做最小源码修改；\n"
            "4. 查看 git diff；\n"
            "5. 如果 diff 已经确认合理，提交最终结果。\n\n"
            "不要继续搜索同一概念。不要继续扩大范围。\n"
            "如果已经有源码修改，立刻 git diff -> 最多一次 focused test -> submit。\n\n"
            f"Blocked action: {action}"
        )

    @staticmethod
    def make_step_25_broad_search_block_message(action: dict) -> str:
        # 兼容旧调用；实际策略已经放宽到 30 步。
        return ConvergenceGuard.make_step_30_broad_search_block_message(action)

    @staticmethod
    def make_step_40_block_message(action: dict) -> str:
        return (
            "[policy error / 严格收敛] 40 步以后只允许收敛动作。\n\n"
            "你已经进入后半程。继续探索会显著增加 limit exceed 风险。\n"
            "现在必须围绕当前最可能的修复闭环推进：修改 -> git diff -> focused test -> submit。\n\n"
            "允许动作：\n"
            "1. 最小源码编辑；\n"
            "2. focused reproduction 或 focused test；\n"
            "3. 只读取一个小范围代码片段：`nl -ba file.py | sed -n 'start,endp'`；\n"
            "4. 查看 visible working-tree diff：`cd \"$REPO_ROOT\" && git diff`；\n"
            "5. 提交最终结果。\n\n"
            "禁止动作：\n"
            "- retrieval 工具；\n"
            "- broad grep / broad search；\n"
            "- 整文件读取；\n"
            "- 继续查同一概念；\n"
            "- 在已有 diff 后反复验证。\n\n"
            "如果已经有非空 diff，下一步优先 git diff；如果 diff 已确认，最多一次 focused test 后提交。\n\n"
            f"Blocked action: {action}"
        )

    @staticmethod
    def make_step_35_block_message(action: dict) -> str:
        # 兼容旧调用；实际策略已经放宽到 40 步。
        return ConvergenceGuard.make_step_40_block_message(action)

    @staticmethod
    def make_step_45_block_message(action: dict) -> str:
        return (
            "[policy error / 终局模式] 45 步以后不能再浪费任何一步。\n\n"
            "现在只允许：\n"
            "1. 最小源码编辑；\n"
            "2. focused reproduction 或 focused test；\n"
            "3. git diff；\n"
            "4. submit。\n\n"
            "不再允许小范围读取。不要再补充探索。\n"
            "如果已有非空 diff：请立刻 git diff 确认，然后提交。\n"
            "除非还没有运行过 focused test，否则不要再新增测试尝试。\n"
            "如果 focused test 因环境、依赖、收集错误或 policy 失败，不要继续调环境，应该提交当前最小修复。\n\n"
            f"Blocked action: {action}"
        )

    @staticmethod
    def make_direct_error_convergence_block_message(action: dict) -> str:
        return (
            "[policy error / 必须停止探索] 已经找到直接报错位置或失败行为，"
            "并且又额外进行了 5 次 inspection/search。\n\n"
            "现在继续查同一概念是在浪费步数，极易导致 limit exceed。\n"
            "下一步必须执行以下动作之一：\n"
            "1. 做最小源码修改；\n"
            "2. 添加 focused regression test；\n"
            "3. 如果已经修改，查看 git diff；\n"
            "4. 如果 diff 已确认合理，提交最终结果。\n\n"
            "不要再 grep、不要再 search、不要再 deepen、不要再读整文件。\n"
            "如果已经有源码修改，立刻 git diff -> 最多一次 focused test -> submit。\n\n"
            f"Blocked action: {action}"
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def serialize_state(self) -> dict:
        return {
            "interaction_step_count": self.interaction_step_count,
            "step_notice_interval": self.step_notice_interval,
            "source_edit_count": self.source_edit_count,
            "direct_error_location_found": self.direct_error_location_found,
            "direct_error_location_step": self.direct_error_location_step,
            "post_error_location_inspection_count": self.post_error_location_inspection_count,
        }

    @staticmethod
    def guardrail_flags() -> dict:
        return {
            "soft_step_notice_at_step_20": False,
            "step_notice_before_step_30": False,
            "block_multiple_tool_calls": True,
            "block_broad_search_after_step_30": True,
            "strict_convergence_after_step_40": True,
            "final_convergence_after_step_45": True,
            "force_edit_or_test_after_direct_error_location_5_inspections": True,
            "source_edit_notice_batch_git_diff_test_submit": True,
        }