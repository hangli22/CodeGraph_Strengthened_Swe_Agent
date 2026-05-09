"""
run_swebench_batch.py — 无 Docker 的 SWE-bench 批量评测脚本
=============================================================

同时支持：
  - 实验组（RetrievalAgent + RetrievalModel，带检索工具，function calling 模式）
  - Baseline 组（DefaultAgent + LitellmModel，纯文本 mswea_bash_command 模式）
  DefaultAgent需要是继承重实现的defaultAgent，不然不对等

关键设计：
    (如果只有retriever部分不同的话会冲突吗)
  - 两种模式使用不同的 prompt 风格，避免协议冲突
  - Retrieval 模式：prompt 引导模型使用 tool calls，不提 mswea_bash_command
  - Baseline 模式：保留原生 mswea_bash_command 格式
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
from mini_swe_agent_integration.env_manage import prepare_agent_environment


class SuppressEmbeddingHttp(logging.Filter):
    """隐藏 DashScope embedding 的成功 HTTP 日志，保留错误日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not (
            "/v1/embeddings" in msg
            and '"HTTP/1.1 200 OK"' in msg
        )


logging.getLogger("httpx").addFilter(SuppressEmbeddingHttp())

_PREDS_LOCK = threading.Lock()

DATASET_MAPPING = {
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
    "full": "princeton-nlp/SWE-bench",
}

# ===========================================================================
# 用于在 prompt 模板中嵌入三反引号的辅助常量
# ===========================================================================
_TICK3 = "``" + "`"  # 避免在源文件中出现连续三反引号导致 IDE/linter 混乱


# ===========================================================================
# Prompt：Baseline 模式（bash tool calling，无检索工具）
# ===========================================================================

_BASELINE_SYSTEM_TEMPLATE = (
    "You are a software engineer that fixes issues in code repositories.\n"
    "\n"
    "You interact exclusively through the bash tool. Do not output markdown, plain-text commands, explanations, plans, or summaries. Assistant message content must be empty or null.\n"
    "\n"
    "Your step limit is 60, so plan your bash commands carefully.\n"
    "## Available Tool\n"
    "\n"
    "1. **bash** - Execute shell commands. Use this for:\n"
    "   - Reading files with grep, head, tail, nl, sed -n\n"
    "   - Editing tracked source files\n"
    "   - Running reproduction scripts or targeted checks\n"
    "   - Git operations such as git diff and git status\n"
    "   - Submitting your final answer\n"
    "\n"
    "## Required Workflow\n"
    "\n"
    "1. Start with focused bash search commands to locate code, tests, symbols, error messages, or behavior terms related to the issue.\n"
    "2. Inspect the relevant source files and relevant tests if available.\n"
    "3. If 3 consecutive grep/search commands return empty or no new relevant evidence, stop searching that concept and inspect/edit the best candidate source file.\n"
    "3. Once the likely source file and relevant tests have been identified, do not keep broadening into framework internals. After at most 3 additional focused inspection commands, either make a minimal source edit or run a focused reproduction. If the same concept has been searched repeatedly with no new evidence, stop searching and edit the candidate source file.\n"
    "4. If the issue provides a concrete reproduction snippet, run or adapt that focused reproduction within the first few steps before broad source exploration. Otherwise, once the likely source and relevant existing tests are located, run one focused existing test or minimal reproduction if practical.\n"
    "5. If reproduction/testing is blocked by import, build, dependency, or test-runner issues after 2-3 substantially different attempts, stop environment repair and continue with static inspection or a minimal source edit.\n"
    "6. Read actual source code before editing. Use tests, errors, issue text, and existing conventions to infer expected behavior.\n"
    "7. If the source line that directly emits the reported error has been found, and the expected behavior is inferable from issue text or tests, prefer a minimal edit over further broad searches.\n"
    "8. After every source-code edit, inspect the visible working-tree diff with: cd \"$REPO_ROOT\" && git diff\n"
    "9. After editing, rerun the same reproduction or focused test if it previously ran or reached issue-specific behavior. If it was blocked by environment/test-runner issues, do not spend more steps trying to make it runnable; rely on static inspection and git diff.\n"
    "10. Before submitting, inspect the final visible working-tree diff and confirm it is non-empty, relevant, minimal, and not destructive.\n"
    "11. Submit only with bash command: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    "\n"
    "## Reproduction and Environment Rules\n"
    "\n"
    "- Try one focused existing test or minimal reproduction early when practical.\n"
    "- If it is blocked by import/build/test-runner issues after 2-3 substantially different attempts, stop environment repair and continue with static source/test inspection.\n"
    "- Do not modify unrelated dependency, compatibility, build, or packaging code just to make local tests/imports work.\n"
    "- After the fix, rerun the same reproduction/focused test only if it was runnable before or reached issue-specific behavior.\n"
    "- Do not modify unrelated dependency, compatibility, build, or packaging code just to make local tests/imports work.\n"
    "\n"
    "## Reading Rules\n"
    "\n"
    "- Do NOT cat large source files directly.\n"
    "- Use `grep -n pattern file.py | head -20` to locate relevant definitions or references.\n"
    "- Use `nl -ba file.py | sed -n 'start,endp'` to read focused line ranges.\n"
    "- If output is too long or truncated, your next action MUST narrow the read: use a smaller `sed -n` line range, grep the exact symbol, or read around the specific function/class. Do not repeat another broad read.\n"
    "- Prefer reading around functions/classes found by focused search results.\n"
    "- Before editing a function/class, read a focused but complete range with line numbers, usually 80-120 lines from the definition or until the next top-level def/class; do not stop at the docstring or header.\n"
    "\n"
    "## Investigation Rules\n"
    "\n"
    "- Use tests, error messages, unexpected values, and failing outputs to infer expected behavior before editing.\n"
    "- When a bug involves an option or parameter, do not assume accepting the parameter is sufficient; "
    "verify the semantic behavior controlled by that option.\n"
    "- For recursive, nested, compositional, pipeline, tree, graph, operator, matrix, or nested-model bugs, inspect helper functions that combine intermediate results before editing public entry points.\n"
    "- Prefer targeted edits to the helper that constructs the incorrect intermediate result rather than broad early returns.\n"
    "- Do not bypass existing custom hooks, overrides, NotImplemented paths, or special-case methods unless the "
    "issue specifically requires it.\n"
    "- Before fixing edge cases involving empty inputs, shapes, dtypes, exceptions, or options, determine expected behavior from issue text, tests, or existing conventions; avoid broad early returns until you understand input forms and return conventions.\n"
    "- Before changing a function/method signature, default value, or forwarding a new keyword argument, inspect the directly affected call/inheritance chain: callers, callees, parent-class methods, overridden methods, and super() targets when applicable.\n"
    "- Inspect related helpers/callers only when they directly affect the candidate fix. Once the relevant source and tests are found, do not broaden into unrelated framework internals unless the first edit/test fails.\n"
    "\n"
    "## Failure Handling Rules\n"
    "\n"
    "- Do not run the same failing command more than once unless you changed the environment or changed the command substantially.\n"
    "- If a command output is truncated, your next reading command should narrow the range or grep for exact definitions.\n"
    "- Prefer running tests/install commands without `| head` or `| tail`; if you use them, prefix the command with `set -o pipefail;`.\n"
    "- Treat output containing Traceback, ImportError, ERROR, FAILED, or metadata-generation-failed as failure even if returncode is 0.\n"
    "\n"
    "## Editing Rules\n"
    "\n"
    "- Editing tracked source files is allowed and required. For source edits, use a heredoc Python script or patch-style edit with exact old/new text.\n"
    "- Do NOT use `python3 -c`, `sed -i`, `echo > file`, `cat > file`, or `printf > file` to edit tracked source files.\n"
    "- If any source edit command fails due to shell quoting, syntax, indentation, or unmatched parentheses, your next edit MUST use heredoc Python.\n"
    "- Heredoc edit template: python3 <<'PY' ... Path(file).read_text() ... assert old in text ... Path(file).write_text(...) ... PY\n"
    "- Before replacing text in a Python edit script, assert that the old text exists with: assert old in text.\n"
    "- Do not insert executable code before a function docstring. If adding logic at the start of a function, place it after the docstring.\n"
    "- After each source edit, inspect git diff. If git diff is empty, you have not changed tracked source code and must not submit.\n"    
    "\n"
    "## Submission Rules\n"
    "\n"
    "- Before submitting, you MUST inspect the visible working-tree diff with exactly: `cd \"$REPO_ROOT\" && git diff`; confirm it is non-empty, relevant, minimal, and not destructive. Do not use historical diffs such as `git diff HEAD~1` or `git show`, and do not redirect, pipe, count, wrap, or combine the diff command.\n"
    "- Never submit after only seeing that an edit command returned code 0. First inspect the resulting diff.\n"
    "- If submission is rejected or local reproduction failed before any source edit, do NOT submit again. Your next action must inspect more source code, inspect relevant tests, or edit a tracked source file.\n"
    "- The submission command must be issued alone, not combined with any other command.\n"
    "- Submit only with: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    "\n"
    "## Tool-Calling Rules\n"
    "\n"
    "- The bash tool call must contain exactly one shell command, or commands connected with && or ||.\n"
    "- Directory or environment variable changes are not persistent. Every action runs in a new subshell.\n"
    "- Use `cd \"$REPO_ROOT\" && ...` for source edits, tests, and git commands when the repository root matters. Do not assume `/workspace` is the repository root.\n"
    "\n"
    "## Useful bash command examples\n"
    "\n"
    "Search source:\n"
    "grep -rn \"target_symbol\" . --include=\"*.py\" | head -20\n"
    "\n"
    "Read focused file range:\n"
    "nl -ba filename.py | sed -n '10,120p'\n"
    "\n"
    "Safe heredoc edit:\n"
    "python3 <<'PY'\n"
    "from pathlib import Path\n"
    "path = Path('filename.py')\n"
    "text = path.read_text()\n"
    "old = 'old_text'\n"
    "new = 'new_text'\n"
    "assert old in text\n"
    "path.write_text(text.replace(old, new))\n"
    "PY\n"
    "\n"
    "Inspect visible working-tree diff:\n"
    "cd \"$REPO_ROOT\" && git diff\n"
    "\n"
    "Final submission:\n"
    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
)


_BASELINE_INSTANCE_TEMPLATE = (
    "Please solve this issue: {{task}}\n"
    "\n"
    "<system_information>\n"
    "{{system}} {{release}} {{version}} {{machine}}\n"
    "</system_information>\n"
    "\n"
    "Start by using grep or other focused bash search commands through the bash tool to find code related to this issue. "
    "Then inspect relevant source files and tests if available, read the source, implement a fix, and verify it works.\n"
    "\n"
    "When done, submit with this bash command:\n"
    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
)

# ===========================================================================
# Prompt：Retrieval 模式（function calling，无 mswea_bash_command）
# ===========================================================================

# ===========================================================================
# Prompt：Retrieval 模式（function calling，无 mswea_bash_command）
# ===========================================================================

_RETRIEVAL_SYSTEM_TEMPLATE = (
    "You are a software engineer that fixes issues in code repositories.\n"
    "\n"
    "You interact exclusively through tool calls. Every assistant response MUST contain exactly one tool call and no more than one.\n "
    "Do not make parallel tool calls. Do not call multiple functions in the same response.\n "
    "Do not output markdown, plain-text commands, explanations, plans, or summaries.\n "
    "When using a tool, assistant message content must be empty or null.\n"
    "\n"
    "Your step limit is 60, so plan your tool calls carefully.\n"
    "## Available Tools\n"
    "\n"
    "1. **bash** - Execute shell commands. Use this for:\n"
    "   - Reading files with grep, head, tail, nl, sed -n\n"
    "   - Editing tracked source files\n"
    "   - Running reproduction scripts or targeted checks\n"
    "   - Git operations such as git diff and git status\n"
    "   - Submitting your final answer\n"
    "\n"
    "2. **search_hybrid** - Semantic + structural code search. Returns ranked relevant files, "
    "classes, functions, and related code graph context. Use this as your FIRST step to locate relevant code.\n"
    "\n"
    "3. **deepen_file** - Fully parse a specific file into the code graph. "
    "Use this after search_hybrid identifies a promising source file and you need function-level, "
    "method-level, caller/callee, inheritance, or related structural details. Budget: 20 files max.\n"
    "\n"
    "4. **search_semantic** - Find functions, classes, or files by natural language description similarity. "
    "Use this when search_hybrid misses behavior terms, error descriptions, or issue-language clues.\n"
    "\n"
    "5. **search_structural** - Coarse-grained graph relation search over known node IDs. "
    "Use this only when you already know a relevant node ID from search_hybrid, search_semantic, "
    "deepen_file, or a previous result. This tool is relationship-based, not a free-text search tool.\n"
    "\n"
    "## Required Workflow\n"
    "\n"
    "1. Start with `search_hybrid` to locate relevant source files, tests, symbols, error messages, or behavior terms. Use `bash` for exact grep/read commands when needed.\n"
    "2. Inspect the most relevant source code and existing tests. Use `deepen_file` only when function/method-level structural detail is needed.\n"
    "3. If the issue provides a concrete reproduction snippet, run or adapt it within the first few steps before broad source exploration. Otherwise, once the likely source and relevant tests are located, run one focused existing test or minimal reproduction if practical.\n"
    "4. If reproduction/testing is blocked by import, build, dependency, or test-runner issues after 2-3 substantially different attempts, stop environment repair and continue with static inspection or a minimal source edit.\n"
    "5. Implement a minimal source-code fix. Do not keep broadening into unrelated framework internals once the likely source, relevant tests, and directly affected function/class are identified.\n"
    "6. After every source-code edit, inspect the visible working-tree diff with: cd \"$REPO_ROOT\" && git diff\n"
    "7. Rerun the same reproduction or focused test if it previously ran or reached issue-specific behavior. If testing was blocked, rely on static inspection and git diff.\n"
    "8. Submit only after the final visible diff is non-empty, relevant, minimal, and not destructive, using exactly: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    "\n"
    "## Reproduction and Environment Rules\n"
    "\n"
    "- Try one focused existing test or minimal reproduction early when practical.\n"
    "- If it is blocked by import/build/test-runner issues after 2-3 substantially different attempts, stop environment repair and continue with static source/test inspection.\n"
    "- Do not modify unrelated dependency, compatibility, build, or packaging code just to make local tests/imports work.\n"
    "- After the fix, rerun the same reproduction/focused test only if it was runnable before or reached issue-specific behavior.\n"
    "- Do not modify unrelated dependency, compatibility, build, or packaging code just to make local tests/imports work.\n"
    "\n"
    "## Reading Rules\n"
    "\n"
    "- Do NOT cat large source files directly.\n"
    "- Use `grep -n pattern file.py | head -20` to locate relevant definitions or references.\n"
    "- Use `nl -ba file.py | sed -n 'start,endp'` to read focused line ranges.\n"
    "- If output is too long or truncated, your next action MUST narrow the read: use a smaller `sed -n` line range, grep the exact symbol, or read around the specific function/class. Do not repeat another broad read.\n"
    "- Prefer reading around functions/classes found by search results.\n"
    "- Before editing a function/class, read a focused but complete range with line numbers, usually 80-120 lines from the definition or until the next top-level def/class; do not stop at the docstring or header.\n"
    "\n"
    "## Investigation Rules\n"
    "\n"
    "- Use tests, error messages, unexpected values, and failing outputs to infer expected behavior before editing.\n"
    "- When a bug involves an option or parameter, do not assume accepting the parameter is sufficient; "
    "verify the semantic behavior controlled by that option.\n"
    "- For recursive, nested, compositional, pipeline, tree, graph, operator, matrix, or nested-model bugs, inspect helper functions that combine intermediate results before editing public entry points.\n"
    "- Prefer targeted edits to the helper that constructs the incorrect intermediate result rather than broad early returns.\n"
    "- Do not bypass existing custom hooks, overrides, NotImplemented paths, or special-case methods unless the "
    "issue specifically requires it.\n"
    "- Before fixing edge cases involving empty inputs, shapes, dtypes, exceptions, or options, determine expected behavior from issue text, tests, or existing conventions; avoid broad early returns until you understand input forms and return conventions.\n"
    "- Before changing a function/method signature, default value, or forwarding a new keyword argument, inspect the directly affected call/inheritance chain: callers, callees, parent-class methods, overridden methods, and super() targets when applicable.\n"
    "- Inspect related helpers/callers only when they directly affect the candidate fix. Once the relevant source and tests are found, do not broaden into unrelated framework internals unless the first edit/test fails.\n"
    "\n"
    "## Retrieval Rules\n"
    "\n"
    "- Use search_hybrid as the first action.\n"
    "- Use deepen_file only for promising source files, files likely to be edited, or files whose callers/callees/inheritance details are needed for the fix.\n"
    "- Do not rely only on retrieval snippets before editing. Always read actual source code with bash line-numbered ranges before modifying a file.\n"
    "- If retrieval results point to a candidate function/class, use bash to inspect the complete local implementation and nearby helpers before editing.\n"
    "- Use search_structural only with known node IDs; do not use it as a free-text search substitute.\n"
    "- If search_hybrid results are clearly irrelevant, use focused bash grep and/or search_semantic with exact symbols, error terms, or behavior terms from the issue.\n"
    "\n"
    "## Step Budget and Convergence Rules\n"
    "\n"
    "- At step 30 or later, do not perform broad search. Avoid search_hybrid, search_semantic, repeated deepen_file, broad `grep -rn ... .`, broad `grep -rn ... django/`, and full-file reads unless a previous edit or focused test created new evidence.\n"
    "- At step 45 or later, your next action must be one of: make a source edit, run a focused reproduction/test, inspect one small line range with `nl -ba file.py | sed -n 'start,endp'`, or inspect visible git diff with `cd \"$REPO_ROOT\" && git diff`.\n"
    "- Once the source line or function that directly emits the reported error, warning, wrong SQL, wrong value, or failing behavior has been found, you may perform at most 5 additional focused inspection/search actions. After that, make a minimal source edit or add a focused regression test.\n"
    "- If a progress notice says you have reached a convergence stage, follow that notice over earlier broad exploration instructions.\n"
    "## Failure Handling Rules\n"
    "\n"
    "- Do not run the same failing command more than once unless you changed the environment or changed the command substantially.\n"
    "- If a command output is truncated, your next reading command should narrow the range or grep for exact definitions.\n"
    "- Prefer running tests/install commands without `| head` or `| tail`; if you use them, prefix the command with `set -o pipefail;`.\n"
    "- Treat output containing Traceback, ImportError, ERROR, FAILED, or metadata-generation-failed as failure even if returncode is 0.\n"
    "\n"
    "## Editing Rules\n"
    "\n"
    "- Editing tracked source files is allowed and required. For source edits, use a heredoc Python script or patch-style edit with exact old/new text.\n"
    "- Do NOT use `python3 -c`, `sed -i`, `echo > file`, `cat > file`, or `printf > file` to edit tracked source files.\n"
    "- If any source edit command fails due to shell quoting, syntax, indentation, or unmatched parentheses, your next edit MUST use heredoc Python.\n"
    "- Heredoc edit template: python3 <<'PY' ... Path(file).read_text() ... assert old in text ... Path(file).write_text(...) ... PY\n"
    "- Before replacing text in a Python edit script, assert that the old text exists with: assert old in text.\n"
    "- Do not insert executable code before a function docstring. If adding logic at the start of a function, place it after the docstring.\n"
    "- After each source edit, inspect git diff. If git diff is empty, you have not changed tracked source code and must not submit.\n"
    "\n"
    "## Submission Rules\n"
    "\n"
    "- Before submitting, you MUST inspect the visible working-tree diff with exactly: `cd \"$REPO_ROOT\" && git diff`; confirm it is non-empty, relevant, minimal, and not destructive. Do not use historical diffs such as `git diff HEAD~1` or `git show`, and do not redirect, pipe, count, wrap, or combine the diff command.\n"
    "- Never submit after only seeing that an edit command returned code 0. First inspect the resulting diff.\n"
    "- If submission is rejected or local reproduction failed before any source edit, do NOT submit again. Your next action must inspect more source code, inspect relevant tests, or edit a tracked source file.\n"
    "- The submission command must be issued alone, not combined with any other command.\n"
    "- Submit only with: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    "\n"
    "## Tool-Calling Rules\n"
    "\n"
    "- Call exactly ONE tool per turn. Never call two or more tools in the same assistant response.\n"
    "- Parallel tool calls are forbidden. If you need multiple actions, perform them across multiple turns.\n"
    "- The bash tool call must contain exactly one shell command, or one shell command composed with && or ||.\n"
    "- Do not use one assistant response to call both a retrieval tool and bash.\n"
    "- Directory or environment variable changes are not persistent. Every action runs in a new subshell.\n"
    "- Use `cd \"$REPO_ROOT\" && ...` for source edits, tests, and git commands when the repository root matters. Do not assume `/workspace` is the repository root.\n"
    "- Do NOT output code blocks, markdown, explanations, summaries, or plain text instructions.\n"
    "- Assistant message content must be empty or null when making a tool call.\n"
    "\n"
    "## Useful bash command examples\n"
    "\n"
    "Search source:\n"
    "grep -rn \"target_symbol\" . --include=\"*.py\" | head -20\n"
    "\n"
    "Read focused file range:\n"
    "nl -ba filename.py | sed -n '10,120p'\n"
    "\n"
    "Safe heredoc edit:\n"
    "python3 <<'PY'\n"
    "from pathlib import Path\n"
    "path = Path('filename.py')\n"
    "text = path.read_text()\n"
    "old = 'old_text'\n"
    "new = 'new_text'\n"
    "assert old in text\n"
    "path.write_text(text.replace(old, new))\n"
    "PY\n"
    "\n"
    "Inspect visible working-tree diff:\n"
    "cd \"$REPO_ROOT\" && git diff\n"
    "\n"
    "Final submission:\n"
    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
)


_RETRIEVAL_INSTANCE_TEMPLATE = (
    "Please solve this issue: {{task}}\n"
    "\n"
    "<system_information>\n"
    "{{system}} {{release}} {{version}} {{machine}}\n"
    "</system_information>\n"
    "\n"
    "Start by using search_hybrid through the retrieval tool to find code related to this issue. "
    "Then deepen the most relevant source files when needed, inspect relevant source files and tests if available, "
    "read the source, implement a fix, and verify it works.\n"
    "\n"
    "When done, submit with this bash command:\n"
    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
)


def prepare_repo(instance: dict, repos_dir: str) -> str:
    """
    克隆并 checkout 到 base_commit，返回本地仓库路径。

    处理三种情况：
      1. repo_path 是有效 Git 仓库：checkout + clean
      2. repo_path 存在但不是 Git 仓库：删除残留目录后重新 clone
      3. repo_path 不存在：clone
    """
    instance_id = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]

    repo_path = os.path.join(repos_dir, instance_id)
    git_dir = os.path.join(repo_path, ".git")

    if os.path.exists(repo_path) and not os.path.exists(git_dir):
        logger.warning(
            "[%s] repo_path 已存在但不是 Git 仓库，将删除残留目录后重新 clone: %s",
            instance_id,
            repo_path,
        )
        _safe_rmtree(repo_path)
        if os.path.exists(repo_path):
            raise RuntimeError(
                f"残留目录删除失败，无法重新 clone: {repo_path}\n"
                "这通常是 Docker 容器写入了 root-owned 文件。"
                "请手动执行: sudo rm -rf {repo_path}"
            )

    if os.path.exists(git_dir):
        logger.info("[%s] 仓库已存在，checkout %s", instance_id, base_commit[:8])
        try:
            _run_with_retry(
                ["git", "checkout", "-f", base_commit],
                cwd=repo_path,
                max_retries=5,
                retry_delay=3.0,
            )
            _run(["git", "clean", "-fdx"], cwd=repo_path)
            return repo_path
        except Exception as e:
            logger.warning(
                "[%s] 已存在仓库 checkout/clean 失败，删除后重新 clone：%s",
                instance_id,
                e,
            )
            _safe_rmtree(repo_path)
            if os.path.exists(repo_path):
                raise RuntimeError(
                    f"删除损坏仓库失败，无法重新 clone: {repo_path}\n"
                    "这通常是 Docker 容器写入了 root-owned 文件。"
                    f"请手动执行: sudo rm -rf {repo_path}"
                ) from e

    os.makedirs(repos_dir, exist_ok=True)
    clone_url = f"https://github.com/{repo}.git"
    logger.info("[%s] 克隆 %s ...", instance_id, clone_url)

    _run_with_retry(
        ["git", "clone", "--filter=blob:none", clone_url, repo_path],
        max_retries=5,
        retry_delay=5.0,
        cleanup_path=repo_path,
    )

    _run_with_retry(
        ["git", "checkout", "-f", base_commit],
        cwd=repo_path,
        max_retries=5,
        retry_delay=5.0,
    )

    _run(["git", "clean", "-fdx"], cwd=repo_path)
    logger.info("[%s] 仓库准备完成：%s", instance_id, repo_path)
    return repo_path


def _safe_rmtree(path: str) -> None:
    """删除目录。"""
    if not os.path.exists(path):
        return
    import shutil

    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=_onerror)


def _run_with_retry(
    cmd: list[str],
    cwd: str = "",
    max_retries: int = 5,
    retry_delay: float = 3.0,
    cleanup_path: str = "",
) -> subprocess.CompletedProcess:
    """带重试的命令执行。"""
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "运行命令，第 %d/%d 次：%s",
                attempt,
                max_retries,
                " ".join(cmd),
            )
            return _run(cmd, cwd=cwd)
        except Exception as e:
            last_error = e
            logger.warning(
                "命令失败，第 %d/%d 次：%s\n%s",
                attempt,
                max_retries,
                " ".join(cmd),
                e,
            )

            if cleanup_path:
                _safe_rmtree(cleanup_path)

            if attempt < max_retries:
                sleep_s = retry_delay * attempt
                logger.info("等待 %.1f 秒后重试...", sleep_s)
                time.sleep(sleep_s)

    raise RuntimeError(
        f"命令重试 {max_retries} 次后仍失败: {' '.join(cmd)}"
    ) from last_error


def _run(cmd: list[str], cwd: str = "") -> subprocess.CompletedProcess:
    """运行 shell 命令，失败时抛出异常。"""
    result = subprocess.run(
        cmd,
        cwd=cwd or None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"命令失败: {' '.join(cmd)}\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )
    return result


# ===========================================================================
# preds.json 管理
# ===========================================================================

def update_preds_file(output_dir: str, instance_id: str, model_name: str, patch: str) -> None:
    """线程安全地更新 preds.json。"""
    preds_path = os.path.join(output_dir, "preds.json")
    with _PREDS_LOCK:
        preds = {}
        if os.path.exists(preds_path):
            preds = json.loads(open(preds_path, encoding="utf-8").read())
        preds[instance_id] = {
            "model_name_or_path": model_name,
            "instance_id": instance_id,
            "model_patch": patch,
        }
        with open(preds_path, "w", encoding="utf-8") as f:
            json.dump(preds, f, indent=2, ensure_ascii=False)


def load_completed_ids(output_dir: str) -> set[str]:
    """从 preds.json 中读取已完成的 instance_id 集合。"""
    preds_path = os.path.join(output_dir, "preds.json")
    if not os.path.exists(preds_path):
        return set()
    return set(json.loads(open(preds_path, encoding="utf-8").read()).keys())

def process_instance(
    instance: dict,
    mode: str,
    model_name: str,
    api_base: str,
    api_key: str,
    output_dir: str,
    repos_dir: str,
    cache_dir: str,
    step_limit: int,
    cost_limit: float,
    alpha: float,
    beta: float,
    use_docker: bool,
    docker_image: str,
    docker_repo_path: str,
    docker_timeout: int,
    container_timeout: str,
) -> dict:
    """处理单个 instance，返回摘要字典。"""
    instance_id = instance["instance_id"]
    logger.info("[%s] 开始处理 (mode=%s)", instance_id, mode)
    t0 = time.perf_counter()

    agent = None
    agent_env = None
    env_info: dict = {}

    # ── 准备仓库 ──
    try:
        repo_path = prepare_repo(instance, repos_dir)
    except Exception as e:
        logger.error("[%s] 仓库准备失败: %s", instance_id, e)
        update_preds_file(output_dir, instance_id, model_name, "")
        return {
            "instance_id": instance_id,
            "mode": mode,
            "exit_status": "RepoPrepFailed",
            "error": str(e),
            "has_patch": False,
            "retrieval_stats": {},
        }

    # ── 预构建代码图（仅 retrieval 模式）──
    instance_cache_dir = os.path.join(cache_dir, instance_id)

    if mode == "retrieval":
        os.environ["CODE_GRAPH_CACHE_DIR"] = instance_cache_dir
        os.environ["SWE_INSTANCE_ID"] = instance_id
        os.environ["SWE_ISSUE_TEXT"] = instance.get("problem_statement", "")
        try:
            from code_graph_retriever.issue_focus import ensure_issue_focus_for_instance

            focus = ensure_issue_focus_for_instance(
                cache_dir=instance_cache_dir,
                instance_id=instance_id,
                issue_text=instance["problem_statement"],
                api_key=api_key or os.environ.get("UNI_API_KEY", ""),
                model="deepseek-v4-flash",
                force=False,
            )

            logger.info(
                "[%s] issue focus 就绪: symbols=%d files=%d bm25_queries=%d",
                instance_id,
                len(focus.exact_symbols),
                len(focus.file_hints),
                len(focus.bm25_queries),
            )
        except Exception as e:
            logger.warning("[%s] issue focus 抽取失败，将继续运行: %s", instance_id, e)

    if mode == "retrieval":
        try:
            from mini_swe_agent_integration.prebuild import build_and_save
            from mini_swe_agent_integration.retrieval_tools import clear_retrieval_cache

            os.environ["CODE_GRAPH_CACHE_DIR"] = instance_cache_dir

            clear_retrieval_cache()

            build_info = build_and_save(
                repo_path=repo_path,
                cache_dir=instance_cache_dir,
                instance_id=instance_id,
            )

            logger.info(
                "[%s] 代码图缓存就绪: cache_dir=%s repo_path=%s status=%s",
                instance_id,
                instance_cache_dir,
                repo_path,
                build_info.get("status", ""),
            )

            clear_retrieval_cache()

        except Exception as e:
            logger.error("[%s] 预构建失败: %s", instance_id, e)
            update_preds_file(output_dir, instance_id, model_name, "")
            return {
                "instance_id": instance_id,
                "mode": mode,
                "exit_status": "PrebuildFailed",
                "error": str(e),
                "has_patch": False,
                "retrieval_stats": {},
            }

    # ── 初始化模型参数 ──
    model_kwargs: dict = {
        "drop_params": True,
        "temperature": 0.0,
        "timeout": 180,
    }
    if api_base:
        model_kwargs["api_base"] = api_base

    _key = api_key or os.environ.get("UNI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    if _key:
        model_kwargs["api_key"] = _key

    instance_dir = os.path.join(output_dir, instance_id)
    os.makedirs(instance_dir, exist_ok=True)
    traj_path = Path(os.path.join(instance_dir, f"{instance_id}.traj.json"))

    # ── 创建 agent 环境：Docker 或 Local ──
    try:
        agent_env, env_info = prepare_agent_environment(
            instance=instance,
            repo_path=repo_path,
            use_docker=use_docker,
            docker_image=docker_image,
            docker_repo_path=docker_repo_path,
            docker_timeout=docker_timeout,
            container_timeout=container_timeout,
        )
        logger.info("[%s] agent 环境就绪: %s", instance_id, env_info)
    except Exception as e:
        logger.error("[%s] agent 环境准备失败: %s", instance_id, e)
        update_preds_file(output_dir, instance_id, model_name, "")
        return {
            "instance_id": instance_id,
            "mode": mode,
            "exit_status": "EnvPrepFailed",
            "error": str(e),
            "has_patch": False,
            "retrieval_stats": {},
        }

    # ── 初始化 Agent ──
    try:
        if mode == "retrieval":
            from mini_swe_agent_integration.retrieval_model import RetrievalModel
            from mini_swe_agent_integration.retrieval_agent import RetrievalAgent

            model = RetrievalModel(
                model_name=model_name,
                model_kwargs=model_kwargs,
                cost_tracking="ignore_errors",
            )

            agent = RetrievalAgent(
                model,
                agent_env,
                system_template=_RETRIEVAL_SYSTEM_TEMPLATE,
                instance_template=_RETRIEVAL_INSTANCE_TEMPLATE,
                step_limit=step_limit,
                cost_limit=cost_limit,
                output_path=traj_path,
            )

        else:
            from mini_swe_agent_integration.baseline_agent import BaselineAgent
            from minisweagent.models.litellm_model import LitellmModel

            model = LitellmModel(
                model_name=model_name,
                model_kwargs=model_kwargs,
                cost_tracking="ignore_errors",
            )

            agent = BaselineAgent(
                model,
                agent_env,
                system_template=_BASELINE_SYSTEM_TEMPLATE,
                instance_template=_BASELINE_INSTANCE_TEMPLATE,
                step_limit=step_limit,
                cost_limit=cost_limit,
                output_path=traj_path,
            )

    except Exception as e:
        logger.error("[%s] Agent 初始化失败: %s", instance_id, e)
        if agent_env is not None and hasattr(agent_env, "cleanup"):
            try:
                agent_env.cleanup()
            except Exception:
                pass

        update_preds_file(output_dir, instance_id, model_name, "")
        return {
            "instance_id": instance_id,
            "mode": mode,
            "exit_status": "AgentInitFailed",
            "error": str(e),
            "has_patch": False,
            "retrieval_stats": {},
        }

    # ── 运行 Agent ──
    exit_status = ""
    submission = ""
    extra_info = {}

    try:
        result = agent.run(task=instance["problem_statement"])
        exit_status = result.get("exit_status", "")
        submission = result.get("submission", "")

        logger.info(
            "[%s] raw exit_status=%r submission_len=%d",
            instance_id, exit_status, len(submission or ""),
        )

        # submission 为空时用宿主机 git diff 兜底。
        # repo 是 volume mount，所以容器内修改会反映到宿主机 repo_path。
        if exit_status == "Submitted" and not submission.strip():
            try:
                diff_result = _run(["git", "diff"], cwd=repo_path)
                if diff_result.stdout.strip():
                    submission = diff_result.stdout
                    logger.warning(
                        "[%s] submission 为空，已从 git diff 兜底提取 patch",
                        instance_id,
                    )
                else:
                    logger.warning(
                        "[%s] Submitted 但 git diff 为空，记为 EmptyPatch",
                        instance_id,
                    )
                    exit_status = "EmptyPatch"
            except Exception as e:
                logger.warning("[%s] 兜底读取 git diff 失败: %s", instance_id, e)
                exit_status = "EmptyPatch"

    except Exception as e:
        exit_status = type(e).__name__
        submission = ""
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(e)}
        logger.error("[%s] Agent 异常: %s", instance_id, e)

    finally:
        try:
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": submission,
                        "mode": mode,
                        "env_info": env_info,
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
        except Exception as e:
            logger.warning("[%s] 保存 trajectory 失败: %s", instance_id, e)

        if agent_env is not None and hasattr(agent_env, "cleanup"):
            try:
                agent_env.cleanup()
            except Exception as e:
                logger.warning("[%s] 清理 agent 环境失败: %s", instance_id, e)

    update_preds_file(output_dir, instance_id, model_name, submission)

    elapsed = time.perf_counter() - t0
    retrieval_stats = getattr(agent, "retrieval_call_counts", {}) if agent is not None else {}

    summary = {
        "instance_id": instance_id,
        "mode": mode,
        "exit_status": exit_status,
        "n_steps": getattr(agent, "n_calls", 0) if agent is not None else 0,
        "cost": getattr(agent, "cost", 0.0) if agent is not None else 0.0,
        "elapsed_s": round(elapsed, 1),
        "retrieval_stats": retrieval_stats,
        "has_patch": bool(submission and submission.strip()),
        "env_info": env_info,
    }

    logger.info(
        "[%s] 完成 | exit=%s steps=%d cost=$%.4f elapsed=%.0fs patch=%s retrieval=%s env=%s",
        instance_id,
        summary["exit_status"],
        summary["n_steps"],
        summary["cost"],
        elapsed,
        summary["has_patch"],
        retrieval_stats,
        env_info,
    )
    return summary


# ===========================================================================
# 数据集加载与过滤
# ===========================================================================

def load_instances(subset: str, split: str) -> list[dict]:
    """从 HuggingFace 加载 SWE-bench 数据集。"""
    dataset_path = DATASET_MAPPING.get(subset, subset)
    logger.info("加载数据集: %s (split=%s)", dataset_path, split)
    from datasets import load_dataset

    ds = load_dataset(dataset_path, split=split)
    instances = list(ds)
    logger.info("数据集加载完成：共 %d 个 instance", len(instances))
    return instances


def filter_instances(
    instances: list[dict],
    slice_spec: str = "",
    filter_spec: str = "",
) -> list[dict]:
    """按 slice 和 filter 筛选 instance。"""
    if filter_spec:
        before = len(instances)
        instances = [i for i in instances if re.match(filter_spec, i["instance_id"])]
        logger.info("filter: %d -> %d", before, len(instances))

    if slice_spec:
        before = len(instances)
        parts = [int(x) if x else None for x in slice_spec.split(":")]
        instances = instances[slice(*parts)]
        logger.info("slice %s: %d -> %d", slice_spec, before, len(instances))

    return instances


# ===========================================================================
# 批量处理主函数
# ===========================================================================

def run_batch(
    mode: str,
    model_name: str,
    api_base: str,
    api_key: str,
    output_dir: str,
    repos_dir: str,
    cache_dir: str,
    instances: list[dict],
    workers: int = 1,
    step_limit: int = 60,
    cost_limit: float = 3.0,
    redo: bool = False,
    alpha: float = 0.4,
    beta: float = 0.6,
    use_docker: bool = False,
    docker_image: str = "sweagent-multipy:latest",
    docker_repo_path: str = "/workspace/repo",
    docker_timeout: int = 120,
    container_timeout: str = "4h",
) -> list[dict]:
    """批量运行所有 instance，返回摘要列表。"""
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(repos_dir, exist_ok=True)
    if mode == "retrieval":
        os.makedirs(cache_dir, exist_ok=True)

    if not redo:
        completed = load_completed_ids(output_dir)
        if completed:
            logger.info("跳过已完成的 %d 个 instance", len(completed))
            instances = [i for i in instances if i["instance_id"] not in completed]

    logger.info(
        "开始批量评测 | mode=%s model=%s instances=%d workers=%d step_limit=%d",
        mode,
        model_name,
        len(instances),
        workers,
        step_limit,
    )

    summaries = []
    lock = threading.Lock()

    def _process(instance: dict) -> dict:
        summary = process_instance(
            instance=instance,
            mode=mode,
            model_name=model_name,
            api_base=api_base,
            api_key=api_key,
            output_dir=output_dir,
            repos_dir=repos_dir,
            cache_dir=cache_dir,
            step_limit=step_limit,
            cost_limit=cost_limit,
            alpha=alpha,
            beta=beta,
            use_docker=use_docker,
            docker_image=docker_image,
            docker_repo_path=docker_repo_path,
            docker_timeout=docker_timeout,
            container_timeout=container_timeout,
        )
        with lock:
            summaries.append(summary)
            _print_progress(
                summaries,
                len(instances) + len(load_completed_ids(output_dir)),
            )
        return summary

    if workers == 1:
        for instance in instances:
            _process(instance)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_process, inst): inst["instance_id"]
                for inst in instances
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    iid = futures[future]
                    logger.error("[%s] 线程异常: %s", iid, e)

    summary_path = os.path.join(output_dir, "run_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "mode": mode,
                "model_name": model_name,
                "total": len(summaries),
                "instances": summaries,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    logger.info("摘要已保存：%s", summary_path)

    _print_final_stats(summaries, mode)
    return summaries


def _print_progress(summaries: list[dict], total: int) -> None:
    completed = len(summaries)
    has_patch = sum(1 for s in summaries if s.get("has_patch"))
    submitted = sum(1 for s in summaries if s.get("exit_status") == "Submitted")
    logger.info(
        "进度: %d/%d | 有patch: %d | Submitted: %d",
        completed,
        total,
        has_patch,
        submitted,
    )


def _print_final_stats(summaries: list[dict], mode: str) -> None:
    total = len(summaries)
    submitted = sum(1 for s in summaries if s.get("exit_status") == "Submitted")
    empty_patch = sum(1 for s in summaries if s.get("exit_status") == "EmptyPatch")
    has_patch = sum(1 for s in summaries if s.get("has_patch"))
    limits = sum(1 for s in summaries if s.get("exit_status") == "LimitsExceeded")
    total_retrieval = sum(
        sum(s.get("retrieval_stats", {}).values()) for s in summaries
    )

    print("\n" + "=" * 60)
    print(f"批量评测完成 | mode={mode}")
    print(f"  总数:         {total}")
    print(f"  Submitted:    {submitted} ({100 * submitted / max(total, 1):.1f}%)")
    print(f"  EmptyPatch:   {empty_patch} ({100 * empty_patch / max(total, 1):.1f}%)")
    print(f"  有 patch:     {has_patch} ({100 * has_patch / max(total, 1):.1f}%)")
    print(f"  LimitsExceed: {limits}")
    if mode == "retrieval":
        print(
            f"  检索总调用:   {total_retrieval} 次"
            f"（平均 {total_retrieval / max(total, 1):.1f} 次/instance）"
        )
    print("=" * 60)
    print("注意：Submitted 率和有patch率不等于实际解决率。")
    print("      请用 sb-cli 提交 preds.json 获取真实 resolve_rate。")



# ===========================================================================
# 命令行入口
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SWE-bench 批量评测（支持 Local/Docker 环境）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=["retrieval", "baseline"],
        help="retrieval=实验组（带检索工具），baseline=对照组（裸mini-swe-agent）",
    )

    parser.add_argument("--model_name", default="openai/deepseek-v3:671b", help="模型名")
    parser.add_argument("--api_base", default="https://uni-api.cstcloud.cn/v1", help="API 接入点")
    parser.add_argument("--api_key", default="", help="API Key")

    parser.add_argument("--subset", default="lite", choices=["lite", "verified", "full"])
    parser.add_argument("--split", default="test")
    parser.add_argument("--slice", default="", help="实例切片，如 0:50")
    parser.add_argument("--filter", default="", help="按 instance_id 正则过滤")

    parser.add_argument("--output_dir", default="./results/retrieval")
    parser.add_argument("--repos_dir", default="./repos")
    parser.add_argument("--cache_dir", default="./cache")

    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--step_limit", type=int, default=60)
    parser.add_argument("--cost_limit", type=float, default=3.0)
    parser.add_argument("--redo", action="store_true")

    parser.add_argument("--alpha", type=float, default=0.4)
    parser.add_argument("--beta", type=float, default=0.6)

    parser.add_argument("--use_docker", action="store_true", help="使用 DockerEnvironment 运行 agent 命令")
    parser.add_argument("--docker_image", default="sweagent-multipy:latest", help="Docker 基础镜像")
    parser.add_argument("--docker_repo_path", default="/workspace/repo", help="容器内 repo 挂载路径")
    parser.add_argument("--docker_timeout", type=int, default=120, help="容器内单条命令超时时间")
    parser.add_argument("--container_timeout", default="4h", help="容器最长存活时间，如 2h/4h")

    args = parser.parse_args()

    instances = load_instances(args.subset, args.split)
    instances = filter_instances(
        instances,
        slice_spec=args.slice,
        filter_spec=args.filter,
    )

    if not instances:
        logger.error("没有符合条件的 instance，退出")
        sys.exit(1)

    run_batch(
        mode=args.mode,
        model_name=args.model_name,
        api_base=args.api_base,
        api_key=args.api_key,
        output_dir=args.output_dir,
        repos_dir=args.repos_dir,
        cache_dir=args.cache_dir,
        instances=instances,
        workers=args.workers,
        step_limit=args.step_limit,
        cost_limit=args.cost_limit,
        redo=args.redo,
        alpha=args.alpha,
        beta=args.beta,
        use_docker=args.use_docker,
        docker_image=args.docker_image,
        docker_repo_path=args.docker_repo_path,
        docker_timeout=args.docker_timeout,
        container_timeout=args.container_timeout,
    )


if __name__ == "__main__":
    main()