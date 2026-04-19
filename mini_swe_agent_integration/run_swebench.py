"""
run_swebench.py — SWE-bench 评测入口脚本
=========================================

用法
----
# 单个 instance
python mini_swe_agent_integration/run_swebench.py \\
    --repo_path /path/to/requests \\
    --instance_id psf__requests-1963 \\
    --problem_statement "Requests raises UnicodeDecodeError..." \\
    --model_name "openai/qwen-plus" \\
    --api_base "https://dashscope.aliyuncs.com/compatible-mode/v1" \\
    --cache_dir /tmp/code_graph_cache \\
    --output_dir ./trajectories

# 仅预构建（不运行 Agent）
python mini_swe_agent_integration/run_swebench.py \\
    --repo_path /repo --instance_id psf__requests-1963 --prebuild_only

评测流程
--------
1. 预构建代码图和索引（幂等，已存在则跳过）
2. 设置 CODE_GRAPH_CACHE_DIR 环境变量
3. 初始化 RetrievalModel + RetrievalAgent
4. 运行 Agent，保存 trajectory
5. 输出 exit_status 和检索工具调用统计
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── System prompt 与 Instance prompt ────────────────────────────────────────
# 基于 mini-swe-agent 官方 prompt，追加检索工具说明段落

SYSTEM_TEMPLATE = """\
You are a helpful assistant that can interact with a computer to solve software engineering tasks.

## Environment
- You have a full Linux shell environment
- The repository is available at the current working directory
- Always use non-interactive flags (-y, -f) for commands
- Avoid interactive tools like vi, nano

## Workflow
1. Analyze the issue and explore relevant code
2. Create a script to reproduce the problem
3. Fix the source code
4. Verify the fix
5. Submit when done: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`

## Code Graph Retrieval Tools
In addition to bash, you have THREE specialized retrieval tools that analyze the code repository structure. \
Use them to find relevant code faster than grep:

### search_hybrid(query, top_k=5) — RECOMMENDED
Combines structural graph analysis + semantic similarity. Use this first when:
- Exploring where a bug might be located (search with issue keywords)
- Finding functions related to an error message
- Starting any investigation without knowing where to look
Results include: structural match reason, semantic relevance, and structural position \
(who calls this function, what it depends on, impact of modifications).

### search_structural(node_id, top_k=5)
Finds functions that play the same role in the call graph as the given node.
Use when you already found a function and want to find similar ones (same caller pattern, \
same inheritance level). node_id format: 'path/to/file.py::ClassName.method_name'

### search_semantic(query, top_k=5)
Finds functions whose descriptions semantically match the query.
Use when you know what the function should do but not its name.

## Command Execution Rules
Every response must include at least one tool call (bash or retrieval tool).
"""

INSTANCE_TEMPLATE = """\
Please fix the following GitHub issue:

{{ task }}

Repository is at: {{ cwd }}

Start by using search_hybrid to find relevant code, then explore and fix the issue.
Submit your fix with: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
"""


def run_instance(
    repo_path:          str,
    instance_id:        str,
    problem_statement:  str,
    model_name:         str,
    cache_dir:          str,
    output_dir:         str        = "./trajectories",
    api_base:           str        = "",
    api_key:            str        = "",
    step_limit:         int        = 50,
    cost_limit:         float      = 5.0,
    prebuild_only:      bool       = False,
) -> dict:
    """
    运行单个 SWE-bench instance。

    Returns
    -------
    dict : 包含 exit_status、retrieval_stats、cost 等信息
    """
    # Step 1：预构建
    from mini_swe_agent_integration.prebuild import build_and_save
    logger.info("=== 预构建阶段 ===")
    build_info = build_and_save(
        repo_path   = repo_path,
        cache_dir   = cache_dir,
        instance_id = instance_id,
    )
    logger.info("预构建信息：%s", json.dumps(build_info, indent=2))

    if prebuild_only:
        logger.info("--prebuild_only 模式，跳过 Agent 运行")
        return build_info

    # Step 2：设置环境变量
    os.environ["CODE_GRAPH_CACHE_DIR"] = cache_dir

    # Step 3：初始化模型和 Agent
    logger.info("=== 初始化 Agent ===")
    from mini_swe_agent_integration.retrieval_model import RetrievalModel
    from mini_swe_agent_integration.retrieval_agent import RetrievalAgent
    from minisweagent.environments.local import LocalEnvironment

    model_kwargs: dict = {}
    if api_base:
        model_kwargs["api_base"] = api_base
    # DashScope 兼容 OpenAI 接口，litellm 需要 api_key 参数
    # 优先使用显式传入的 key，否则读取环境变量 DASHSCOPE_API_KEY
    if api_key:
        model_kwargs["api_key"] = api_key
    elif os.environ.get("DASHSCOPE_API_KEY"):
        model_kwargs["api_key"] = os.environ["DASHSCOPE_API_KEY"]
    model_kwargs["drop_params"] = True   # 跳过不支持的参数（如 Qwen 不支持某些字段）

    output_path = Path(output_dir) / f"{instance_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    model = RetrievalModel(
        model_name   = model_name,
        model_kwargs = model_kwargs,
        cost_tracking = "ignore_errors",
    )

    agent = RetrievalAgent(
        model,
        LocalEnvironment(cwd=repo_path),
        system_template   = SYSTEM_TEMPLATE,
        instance_template = INSTANCE_TEMPLATE,
        step_limit        = step_limit,
        cost_limit        = cost_limit,
        output_path       = output_path,
    )

    # Step 4：运行
    logger.info("=== Agent 开始运行 ===")
    logger.info("instance_id: %s", instance_id)
    logger.info("model:       %s", model_name)
    logger.info("step_limit:  %d  cost_limit: %.1f", step_limit, cost_limit)

    t0 = time.perf_counter()
    result = agent.run(task=problem_statement)
    elapsed = time.perf_counter() - t0

    # Step 5：汇总结果
    summary = {
        "instance_id":     instance_id,
        "exit_status":     result.get("exit_status", ""),
        "cost":            agent.cost,
        "n_steps":         agent.n_calls,
        "elapsed_s":       round(elapsed, 2),
        "retrieval_stats": agent.retrieval_call_counts,
        "trajectory_path": str(output_path),
    }

    logger.info("=== 运行完成 ===")
    logger.info("exit_status:     %s",  summary["exit_status"])
    logger.info("steps / cost:    %d / $%.4f", summary["n_steps"], summary["cost"])
    logger.info("elapsed:         %.1fs", summary["elapsed_s"])
    logger.info("retrieval calls: %s",   summary["retrieval_stats"])
    logger.info("trajectory:      %s",   summary["trajectory_path"])

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="带代码图检索工具的 mini-swe-agent SWE-bench 评测脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo_path",         required=True)
    parser.add_argument("--instance_id",       required=True)
    parser.add_argument("--problem_statement", default="",
                        help="issue 描述文本，未提供时从 stdin 读取")
    parser.add_argument("--model_name",        default="openai/qwen-plus",
                        help="litellm 格式的模型名，如 openai/qwen-plus 或 anthropic/claude-sonnet-4-5-20250929")
    parser.add_argument("--api_base",          default="",
                        help="API 接入点，如 https://dashscope.aliyuncs.com/compatible-mode/v1")
    parser.add_argument("--api_key", default=os.environ.get("DASHSCOPE_API_KEY", ""),
                        help="API Key，默认读取 DASHSCOPE_API_KEY 环境变量")
    parser.add_argument("--cache_dir",         default="/tmp/code_graph_cache")
    parser.add_argument("--output_dir",        default="./trajectories")
    parser.add_argument("--step_limit",        type=int,   default=50)
    parser.add_argument("--cost_limit",        type=float, default=5.0)
    parser.add_argument("--prebuild_only",     action="store_true",
                        help="只预构建缓存，不运行 Agent")
    args = parser.parse_args()

    # 读取 problem_statement
    problem_statement = args.problem_statement
    if not problem_statement and not args.prebuild_only:
        logger.info("从 stdin 读取 problem_statement...")
        problem_statement = sys.stdin.read().strip()

    summary = run_instance(
        repo_path         = args.repo_path,
        instance_id       = args.instance_id,
        problem_statement = problem_statement,
        model_name        = args.model_name,
        cache_dir         = args.cache_dir,
        output_dir        = args.output_dir,
        api_base          = args.api_base,
        api_key           = args.api_key,
        step_limit        = args.step_limit,
        cost_limit        = args.cost_limit,
        prebuild_only     = args.prebuild_only,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()