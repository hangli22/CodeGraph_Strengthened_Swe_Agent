"""
run_swebench_batch.py — 无 Docker 的 SWE-bench 批量评测脚本
=============================================================

同时支持：
  - 实验组（RetrievalAgent + RetrievalModel，带检索工具，function calling 模式）
  - Baseline 组（DefaultAgent + LitellmModel，纯文本 mswea_bash_command 模式）

关键设计：
  - 两种模式使用完全不同的 prompt 风格，避免协议冲突
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
# Prompt：Baseline 模式（文本格式，mswea_bash_command）
# ===========================================================================

_BASELINE_SYSTEM_TEMPLATE = (
    "You are a software engineer that fixes issues in code repositories.\n"
    "\n"
    "You interact with the system exclusively through bash commands in a special markdown code block.\n"
    "Every response you produce MUST contain exactly ONE bash code block with language `mswea_bash_command`.\n"
    "The code block must contain exactly ONE command, or commands connected with && or ||.\n"
    "Include a THOUGHT section before your command where you briefly explain why you are taking this action.\n"
    "Do NOT output multiple code blocks. Do NOT output plain-text shell commands outside the code block.\n"
    "\n"
    "<format_example>\n"
    "THOUGHT: I need to locate the relevant source code before editing.\n"
    "\n"
    + _TICK3 + "mswea_bash_command\n"
    "grep -rn \"target_symbol\" . --include=\"*.py\" | head -20\n"
    + _TICK3 + "\n"
    "</format_example>\n"
    "\n"
    "Failure to follow these rules will cause your response to be rejected.\n"
    "\n"
    "## Available Command Interface\n"
    "\n"
    "Use bash commands for:\n"
    "   - Reading files with grep, head, tail, nl, sed -n\n"
    "   - Editing tracked source files\n"
    "   - Running reproduction scripts or targeted checks\n"
    "   - Git operations such as git diff and git status\n"
    "   - Submitting your final answer\n"
    "\n"
    "## Required Workflow\n"
    "\n"
    "1. Start with grep or other focused bash search commands to locate code related to the issue.\n"
    "2. Inspect relevant source files and relevant tests if available.\n"
    "3. Read actual source code before editing.\n"
    "4. If search results or the issue mention relevant tests, inspect those tests before editing; use tests/errors/issue text to infer expected behavior.\n"
    "5. Create a reproduction script if practical, but do not get stuck on environment setup.\n"
    "6. Edit tracked source files to implement a minimal fix.\n"
    "7. After every source-code edit, run git diff and inspect the exact change.\n"
    "8. Before submitting, run git diff after the final edit and confirm it is non-empty.\n"
    "9. Submit only with command: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    "\n"
    "## Reading Rules\n"
    "\n"
    "- Do NOT cat large source files directly.\n"
    "- Use `grep -n pattern file.py | head -20` to locate relevant definitions or references.\n"
    "- Use `nl -ba file.py | sed -n 'start,endp'` to read focused line ranges.\n"
    "- If output is too long or truncated, your next action MUST narrow the read: use a smaller `sed -n` line range, grep the exact symbol, or read around the specific function/class. Do not repeat another broad read.\n"
    "- Prefer reading around functions/classes found by focused search results.\n"
    "- Before editing a function, read the complete function body and nearby helper functions with line numbers.\n"
    "- When reading a function, continue reading until you have seen the full body, not only the docstring or header.\n"
    "- After locating a function definition, read a focused but complete range, usually 80-120 lines from the definition or until the next top-level def/class, instead of only reading the docstring-sized prefix.\n"
    "\n"
    "## Investigation Rules\n"
    "\n"
    "- Use tests, error messages, unexpected values, and failing outputs to infer expected behavior before editing.\n"
    "- When a bug involves an option or parameter, do not assume accepting the parameter is sufficient; "
    "verify the semantic behavior controlled by that option.\n"
    "- For bugs in recursive, nested, or compositional logic, inspect helper functions that combine sub-results "
    "before editing the top-level recursive function.\n"
    "- For bugs involving pipelines, trees, graphs, operators, matrices, or nested models, inspect how child "
    "results are padded, aligned, sliced, stacked, or merged.\n"
    "- Prefer targeted edits to the helper that constructs the incorrect intermediate result, rather than adding "
    "broad early returns to public entry points or top-level recursive functions.\n"
    "- Do not bypass existing custom hooks, overrides, NotImplemented paths, or special-case methods unless the "
    "issue specifically requires it.\n"
    "- Before fixing edge cases involving empty inputs, shapes, dtypes, exceptions, or options, determine the expected output/behavior from issue text, tests, or existing code conventions.\n"
    "- Avoid broad early returns until you understand the function's input forms, helper flow, and return conventions.\n"
    "- Before changing a function/method signature, default value, or forwarding a new keyword argument, inspect the directly affected call/inheritance chain: callers, callees, parent-class methods, overridden methods, and super() targets when applicable.\n"
    "\n"
    "## Failure Handling Rules\n"
    "\n"
    "- If a bash command fails because importing the target project fails, do NOT repeat the same import-based command.\n"
    "- Do not run the same failing command more than once unless you changed the environment or changed the command substantially.\n"
    "- After one import/build-related failure, do NOT submit. Switch to static inspection using grep, sed -n, nl -ba, and relevant tests/source code, then edit if needed.\n"
    "- If local reproduction is blocked by missing compiled extensions or dependency problems, do not spend many steps fixing the environment and do not submit immediately. Inspect source and tests, make a minimal source-only fix, then verify with git diff.\n"
    "- If a command output is truncated, your next reading command should narrow the range or grep for exact definitions.\n"
    "- Do not modify unrelated dependency, compatibility, build, or packaging code just to make local imports/tests work unless the issue specifically asks for it.\n"
    "- If a source edit fails due to shell quoting, Python syntax, indentation, or unmatched parentheses, do not retry with another quoted one-liner; the next source edit MUST use heredoc Python.\n"
    "\n"
    "## Editing Rules\n"
    "\n"
    "- Editing tracked source files is allowed and required. For source edits, use a heredoc Python script or patch-style edit with exact old/new text.\n"
    "- Do NOT use `python3 -c`, `sed -i`, `echo > file`, `cat > file`, or `printf > file` to edit tracked source files.\n"
    "- If any source edit command fails due to shell quoting, syntax, indentation, or unmatched parentheses, your next edit MUST use heredoc Python.\n"
    "- Heredoc edit template: python3 <<'PY' ... Path(file).read_text() ... assert old in text ... Path(file).write_text(...) ... PY\n"
    "- Before replacing text in a Python edit script, assert that the old text exists with: assert old in text.\n"
    "- Do not insert executable code before a function docstring. If adding logic at the start of a function, place it after the docstring.\n"
    "- After every source-code modification, run git diff to inspect the exact change.\n"
    "- If git diff is empty, you have not changed tracked source code and must not submit.\n"
    "\n"
    "## Submission Rules\n"
    "\n"
    "- Before submitting, you MUST run git diff after the final edit and confirm it is non-empty, relevant, minimal, and not destructive.\n"
    "- Never submit after only seeing that an edit command returned code 0. First inspect the resulting diff.\n"
    "- If submission is rejected or local reproduction failed before any source edit, do NOT submit again. Your next action must inspect more source code, inspect relevant tests, or edit a tracked source file.\n"
    "- The submission command must be issued alone, not combined with any other command.\n"
    "- Submit only with: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    "\n"
    "## Command-Formatting Rules\n"
    "\n"
    "- Every response must contain exactly one `mswea_bash_command` code block.\n"
    "- The code block must contain exactly one command, or commands connected with && or ||.\n"
    "- Include a THOUGHT section before the command.\n"
    "- Do NOT use markdown code blocks with any language other than `mswea_bash_command`.\n"
    "- Do NOT merely describe code changes. You must actually modify tracked source files.\n"
    "- Directory or environment variable changes are not persistent. Every action runs in a new subshell.\n"
    "\n"
    "## Useful command examples\n"
    "\n"
    "### Search source:\n"
    + _TICK3 + "mswea_bash_command\n"
    "grep -rn \"target_symbol\" . --include=\"*.py\" | head -20\n"
    + _TICK3 + "\n"
    "\n"
    "### Read focused file range:\n"
    + _TICK3 + "mswea_bash_command\n"
    "nl -ba filename.py | sed -n '10,120p'\n"
    + _TICK3 + "\n"
    "\n"
    "### Safe heredoc edit:\n"
    + _TICK3 + "mswea_bash_command\n"
    "python3 <<'PY'\n"
    "from pathlib import Path\n"
    "path = Path('filename.py')\n"
    "text = path.read_text()\n"
    "old = 'old_text'\n"
    "new = 'new_text'\n"
    "assert old in text\n"
    "path.write_text(text.replace(old, new))\n"
    "PY\n"
    + _TICK3 + "\n"
    "\n"
    "### Inspect diff:\n"
    + _TICK3 + "mswea_bash_command\n"
    "git diff\n"
    + _TICK3 + "\n"
    "\n"
    "### Final submission:\n"
    + _TICK3 + "mswea_bash_command\n"
    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    + _TICK3 + "\n"
)

_BASELINE_INSTANCE_TEMPLATE = (
    "Please solve this issue: {{task}}\n"
    "\n"
    "<system_information>\n"
    "{{system}} {{release}} {{version}} {{machine}}\n"
    "</system_information>\n"
    "\n"
    "Start by using grep or other focused bash search commands to find code related to this issue. "
    "Then inspect relevant source files and tests if available, read the source, implement a fix, and verify it works.\n"
    "\n"
    "When done, submit with exactly this command:\n"
    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
)

# ===========================================================================
# Prompt：Retrieval 模式（function calling，无 mswea_bash_command）
# ===========================================================================

_RETRIEVAL_SYSTEM_TEMPLATE = (
    "You are a software engineer that fixes issues in code repositories.\n"
    "\n"
    "You interact with the system exclusively through tool calls. Every response you produce "
    "MUST contain exactly one tool call. Do NOT output markdown code blocks or plain text commands.\n"
    "When using a tool, the assistant message content should be empty or null. "
    "Do not include explanations, plans, summaries, or natural language in content. "
    "Your entire action must be represented by the tool call.\n"
    "\n"
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
    "classes, and functions. Use this as your FIRST step to locate relevant code.\n"
    "\n"
    "3. **deepen_file** - Fully parse a specific file into the code graph. "
    "Use this after search_hybrid identifies a promising source file and you need function-level "
    "or method-level details. Budget: 20 files max.\n"
    "\n"
    "4. **search_semantic** - Find functions, classes, or files by natural language description similarity.\n"
    "\n"
    "5. **search_structural** - Coarse-grained graph relation search over known node IDs. "
    "Use this only when you already know a relevant node ID from search_hybrid, search_semantic, "
    "or a previous result. This tool is relationship-based, not a free-text search tool.\n"
    "\n"
    "## Required Workflow\n"
    "\n"
    "1. Start with search_hybrid to locate code related to the issue.\n"
    "2. Use deepen_file on the most relevant source file when function-level detail is needed.\n"
    "3. Read actual source code with bash before editing.\n"
    "4. If search results or the issue mention relevant tests, inspect those tests before editing; use tests/errors/issue text to infer expected behavior.\n"
    "5. Create a reproduction script if practical, but do not get stuck on environment setup.\n"
    "6. Edit tracked source files to implement a minimal fix.\n"
    "7. After every source-code edit, run git diff and inspect the exact change.\n"
    "8. Before submitting, run git diff after the final edit and confirm it is non-empty.\n"
    "9. Submit only with bash command: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    "\n"
    "## Reading Rules\n"
    "\n"
    "- Do NOT cat large source files directly.\n"
    "- Use `grep -n pattern file.py | head -20` to locate relevant definitions or references.\n"
    "- Use `nl -ba file.py | sed -n 'start,endp'` to read focused line ranges.\n"
    "- If output is too long or truncated, your next action MUST narrow the read: use a smaller `sed -n` line range, grep the exact symbol, or read around the specific function/class. Do not repeat another broad read.\n"
    "- Prefer reading around functions/classes returned by search results.\n"
    "- Before editing a function, read the complete function body and nearby helper functions with line numbers.\n"
    "- When reading a function, continue reading until you have seen the full body, not only the docstring or header.\n"
    "- After locating a function definition, read a focused but complete range, usually 80-120 lines from the definition or until the next top-level def/class, instead of only reading the docstring-sized prefix.\n"
    "\n"
    "## Investigation Rules\n"
    "\n"
    "- Use tests, error messages, unexpected values, and failing outputs to infer expected behavior before editing.\n"
    "- When a bug involves an option or parameter, do not assume accepting the parameter is sufficient; "
    "verify the semantic behavior controlled by that option.\n"
    "- For bugs in recursive, nested, or compositional logic, inspect helper functions that combine sub-results "
    "before editing the top-level recursive function.\n"
    "- For bugs involving pipelines, trees, graphs, operators, matrices, or nested models, inspect how child "
    "results are padded, aligned, sliced, stacked, or merged.\n"
    "- Prefer targeted edits to the helper that constructs the incorrect intermediate result, rather than adding "
    "broad early returns to public entry points or top-level recursive functions.\n"
    "- Do not bypass existing custom hooks, overrides, NotImplemented paths, or special-case methods unless the "
    "issue specifically requires it.\n"
    "- Before fixing edge cases involving empty inputs, shapes, dtypes, exceptions, or options, determine the expected output/behavior from issue text, tests, or existing code conventions.\n"
    "- Avoid broad early returns until you understand the function's input forms, helper flow, and return conventions.\n"
    "-Before changing a function/method signature, default value, or forwarding a new keyword argument, inspect the directly affected call/inheritance chain: callers, callees, parent-class methods, overridden methods, and super() targets when applicable.\n"
    "\n"
    "## Failure Handling Rules\n"
    "\n"
    "- If a bash command fails because importing the target project fails, do NOT repeat the same import-based command.\n"
    "- Do not run the same failing command more than once unless you changed the environment or changed the command substantially.\n"
    "- After one import/build-related failure, do NOT submit. Switch to static inspection using grep, sed -n, nl -ba, and relevant tests/source code, then edit if needed.\n"
    "- If local reproduction is blocked by missing compiled extensions or dependency problems, do not spend many steps fixing the environment and do not submit immediately. Inspect source and tests, make a minimal source-only fix, then verify with git diff.\n"
    "- If a command output is truncated, your next reading command should narrow the range or grep for exact definitions.\n"
    "- Do not modify unrelated dependency, compatibility, build, or packaging code just to make local imports/tests work unless the issue specifically asks for it.\n"
    "- If a source edit fails due to shell quoting, Python syntax, indentation, or unmatched parentheses, do not retry with another quoted one-liner; the next source edit MUST use heredoc Python.\n"
    "\n"
    "## Editing Rules\n"
    "\n"
    "- Editing tracked source files is allowed and required. For source edits, use a heredoc Python script or patch-style edit with exact old/new text.\n"
    "- Do NOT use `python3 -c`, `sed -i`, `echo > file`, `cat > file`, or `printf > file` to edit tracked source files.\n"
    "- If any source edit command fails due to shell quoting, syntax, indentation, or unmatched parentheses, your next edit MUST use heredoc Python.\n"
    "- Heredoc edit template: python3 <<'PY' ... Path(file).read_text() ... assert old in text ... Path(file).write_text(...) ... PY\n"    "- Before replacing text in a Python edit script, assert that the old text exists with: assert old in text.\n"
    "- Do not insert executable code before a function docstring. If adding logic at the start of a function, place it after the docstring.\n"
    "- After every source-code modification, run git diff to inspect the exact change.\n"
    "- If git diff is empty, you have not changed tracked source code and must not submit.\n"
    "\n"
    "## Submission Rules\n"
    "\n"
    "- Before submitting, you MUST run git diff after the final edit and confirm it is non-empty, relevant, minimal, and not destructive.\n"
    "- Never submit after only seeing that an edit command returned code 0. First inspect the resulting diff.\n"
    "- If submission is rejected or local reproduction failed before any source edit, do NOT submit again. Your next action must inspect more source code, inspect relevant tests, or edit a tracked source file.\n"
    "- The submission command must be issued alone, not combined with any other command.\n"
    "- Submit only with: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    "\n"
    "## Tool-Calling Rules\n"
    "\n"
    "- Call exactly ONE tool per turn.\n"
    "- Do NOT output code blocks, markdown, explanations, summaries, or plain text instructions.\n"
    "- Assistant message content must be empty or null when making a tool call.\n"
)


_RETRIEVAL_INSTANCE_TEMPLATE = (
    "Please solve this issue: {{task}}\n"
    "\n"
    "<system_information>\n"
    "{{system}} {{release}} {{version}} {{machine}}\n"
    "</system_information>\n"
    "\n"
    "Start by using search_hybrid to find code related to this issue. "
    "Then deepen the most relevant source files when needed, inspect related tests if available, "
    "read the source, implement a fix, and verify it works.\n"
    "\n"
    """When done, submit with: bash({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})\n"""
)


# ===========================================================================
# 仓库管理
# ===========================================================================

def prepare_repo(instance: dict, repos_dir: str) -> str:
    """
    克隆并 checkout 到 base_commit，返回本地仓库路径。
    幂等：已存在则直接 checkout；不存在则 clone。
    """
    instance_id = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]

    repo_path = os.path.join(repos_dir, instance_id)

    if os.path.exists(os.path.join(repo_path, ".git")):
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
                "[%s] 已存在仓库 checkout 失败，删除后重新 clone：%s",
                instance_id,
                e,
            )
            _safe_rmtree(repo_path)

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


# ===========================================================================
# 单个 instance 处理
# ===========================================================================

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
) -> dict:
    """处理单个 instance，返回摘要字典。"""
    instance_id = instance["instance_id"]
    logger.info("[%s] 开始处理 (mode=%s)", instance_id, mode)
    t0 = time.perf_counter()

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

    # ── 预构建代码图（仅实验组）──
    instance_cache_dir = os.path.join(cache_dir, instance_id)
    if mode == "retrieval":
        try:
            from mini_swe_agent_integration.prebuild import build_and_save
            from mini_swe_agent_integration.retrieval_tools import clear_retrieval_cache

            # 每个 instance 必须使用独立 cache_dir。
            os.environ["CODE_GRAPH_CACHE_DIR"] = instance_cache_dir

            # 关键：切换 instance/repo/cache_dir 后，必须清空进程内检索缓存。
            # 否则 retrieval_tools._cache 可能继续持有上一个 instance 的 graph/retriever。
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

            # build_and_save 可能直接命中磁盘缓存。为了保险，构建/命中缓存后再清一次，
            # 确保 agent 第一次 search 时从当前 instance_cache_dir 重新加载。
            clear_retrieval_cache()

        except Exception as e:
            logger.error("[%s] 预构建失败: %s", instance_id, e)
            logger.warning("[%s] 预构建失败，将继续运行，但检索工具可能不可用", instance_id)

    # ── 初始化模型和 Agent ──
    from minisweagent.environments.local import LocalEnvironment

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

    if mode == "retrieval":
        # 实验组：function calling 模式
        from mini_swe_agent_integration.retrieval_model import RetrievalModel
        from mini_swe_agent_integration.retrieval_agent import RetrievalAgent

        model = RetrievalModel(
            model_name=model_name,
            model_kwargs=model_kwargs,
            cost_tracking="ignore_errors",
        )
        agent = RetrievalAgent(
            model,
            LocalEnvironment(cwd=repo_path),
            system_template=_RETRIEVAL_SYSTEM_TEMPLATE,
            instance_template=_RETRIEVAL_INSTANCE_TEMPLATE,
            step_limit=step_limit,
            cost_limit=cost_limit,
            output_path=traj_path,
        )
    else:
        # 对照组：文本格式模式
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.models.litellm_model import LitellmModel

        model = LitellmModel(
            model_name=model_name,
            model_kwargs=model_kwargs,
            cost_tracking="ignore_errors",
        )
        agent = DefaultAgent(
            model,
            LocalEnvironment(cwd=repo_path),
            system_template=_BASELINE_SYSTEM_TEMPLATE,
            instance_template=_BASELINE_INSTANCE_TEMPLATE,
            step_limit=step_limit,
            cost_limit=cost_limit,
            output_path=traj_path,
        )

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

        # submission 为空时用 git diff 兜底
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
        agent.save(
            traj_path,
            {
                "info": {
                    "exit_status": exit_status,
                    "submission": submission,
                    "mode": mode,
                    **extra_info,
                },
                "instance_id": instance_id,
            },
        )

    update_preds_file(output_dir, instance_id, model_name, submission)

    elapsed = time.perf_counter() - t0
    retrieval_stats = getattr(agent, "retrieval_call_counts", {})
    summary = {
        "instance_id": instance_id,
        "mode": mode,
        "exit_status": exit_status,
        "n_steps": agent.n_calls,
        "cost": agent.cost,
        "elapsed_s": round(elapsed, 1),
        "retrieval_stats": retrieval_stats,
        "has_patch": bool(submission and submission.strip()),
    }
    logger.info(
        "[%s] 完成 | exit=%s steps=%d cost=$%.4f elapsed=%.0fs patch=%s retrieval=%s",
        instance_id,
        exit_status,
        agent.n_calls,
        agent.cost,
        elapsed,
        summary["has_patch"],
        retrieval_stats,
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
    step_limit: int = 50,
    cost_limit: float = 3.0,
    redo: bool = False,
    alpha: float = 0.4,
    beta: float = 0.6,
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
        description="SWE-bench 批量评测（无 Docker，本地环境）",
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
    parser.add_argument("--step_limit", type=int, default=50)
    parser.add_argument("--cost_limit", type=float, default=3.0)
    parser.add_argument("--redo", action="store_true")

    parser.add_argument("--alpha", type=float, default=0.4)
    parser.add_argument("--beta", type=float, default=0.6)

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
    )


if __name__ == "__main__":
    main()