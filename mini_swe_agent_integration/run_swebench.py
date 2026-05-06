"""
run_swebench.py — SWE-bench 评测入口脚本（骨架图 + 按需深化模式）
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SYSTEM_TEMPLATE = """\
You are a helpful assistant that can interact with a computer to solve software engineering tasks.

## Environment
- You have a full Linux shell environment
- The repository is available at the current working directory
- Always use non-interactive flags (-y, -f) for commands

## Code Graph Retrieval Tools (骨架图 + 按需深化)

The repository has been pre-analyzed into a **skeleton graph** containing:
- All files, classes (with method name lists), and top-level functions
- Import dependencies and class inheritance relationships
- But NOT method-level details or call relationships (those are added on demand)

You have FOUR specialized tools:

### search_hybrid(query, top_k=5) — RECOMMENDED FIRST STEP
Combines structural + semantic search on the skeleton graph.
Use this to find which files/classes are likely related to the issue.

### deepen_file(file_path) — USE AFTER LOCATING A FILE
Fully parses a file: creates method nodes, analyzes call relationships, updates indexes.
After deepening, methods become searchable. Returns new methods and related files.
Budget: max 20 files per task. Workflow: search → deepen → search again for details.

### search_structural(node_id, top_k=5)
Finds nodes with similar structural roles (call patterns, inheritance level).
Best after you've found a specific function and want similar ones.

### search_semantic(query, top_k=5)
Finds functions by natural language description similarity.

## Recommended Workflow
1. search_hybrid with issue keywords → identify candidate files
2. deepen_file on the most promising file(s)
3. search_hybrid or search_structural again for method-level matches
4. Read code, create reproduction script, fix, verify, submit

## Submission"""

INSTANCE_TEMPLATE = """\
Please fix the following GitHub issue:

{{ task }}

Repository is at: {{ cwd }}

Start by using search_hybrid to find relevant code, then deepen_file on promising files.
Submit your fix with: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
"""


def run_instance(repo_path, instance_id, problem_statement, model_name,
                 cache_dir, output_dir="./trajectories", api_base="", api_key="",
                 step_limit=50, cost_limit=5.0, prebuild_only=False):
    from mini_swe_agent_integration.prebuild import build_and_save
    logger.info("=== 预构建阶段（骨架模式）===")
    build_info = build_and_save(repo_path=repo_path, cache_dir=cache_dir, instance_id=instance_id)
    logger.info("预构建信息：%s", json.dumps(build_info, indent=2))

    if prebuild_only:
        return build_info

    os.environ["CODE_GRAPH_CACHE_DIR"] = cache_dir

    logger.info("=== 初始化 Agent ===")
    from mini_swe_agent_integration.retrieval_model import RetrievalModel
    from mini_swe_agent_integration.retrieval_agent import RetrievalAgent
    from minisweagent.environments.local import LocalEnvironment

    model_kwargs: dict = {"drop_params": True}
    if api_base:
        model_kwargs["api_base"] = api_base
    if api_key:
        model_kwargs["api_key"] = api_key
    elif os.environ.get("UNI_API_KEY"):
        model_kwargs["api_key"] = os.environ["UNI_API_KEY"]

    output_path = Path(output_dir) / f"{instance_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    model = RetrievalModel(model_name=model_name, model_kwargs=model_kwargs, cost_tracking="ignore_errors")
    agent = RetrievalAgent(
        model, LocalEnvironment(cwd=repo_path),
        system_template=SYSTEM_TEMPLATE, instance_template=INSTANCE_TEMPLATE,
        step_limit=step_limit, cost_limit=cost_limit, output_path=output_path,
    )

    logger.info("=== Agent 开始运行 ===")
    t0 = time.perf_counter()
    result = agent.run(task=problem_statement)
    elapsed = time.perf_counter() - t0

    summary = {
        "instance_id": instance_id, "exit_status": result.get("exit_status", ""),
        "cost": agent.cost, "n_steps": agent.n_calls, "elapsed_s": round(elapsed, 2),
        "retrieval_stats": agent.retrieval_call_counts, "trajectory_path": str(output_path),
    }
    logger.info("=== 完成 === exit=%s steps=%d cost=$%.4f elapsed=%.1fs retrieval=%s",
                summary["exit_status"], summary["n_steps"], summary["cost"],
                summary["elapsed_s"], summary["retrieval_stats"])
    return summary


def main():
    parser = argparse.ArgumentParser(description="带骨架图+按需深化的 SWE-bench 评测")
    parser.add_argument("--repo_path", required=True)
    parser.add_argument("--instance_id", required=True)
    parser.add_argument("--problem_statement", default="")
    parser.add_argument("--model_name", default="deepseek-v3:671b")
    parser.add_argument("--api_base", default="https://uni-api.cstcloud.cn/v1")
    parser.add_argument("--api_key", default=os.environ.get("UNI_API_KEY", ""))
    parser.add_argument("--cache_dir", default="/tmp/code_graph_cache")
    parser.add_argument("--output_dir", default="./trajectories")
    parser.add_argument("--step_limit", type=int, default=50)
    parser.add_argument("--cost_limit", type=float, default=5.0)
    parser.add_argument("--prebuild_only", action="store_true")
    args = parser.parse_args()

    problem_statement = args.problem_statement
    if not problem_statement and not args.prebuild_only:
        problem_statement = sys.stdin.read().strip()

    summary = run_instance(
        repo_path=args.repo_path, instance_id=args.instance_id,
        problem_statement=problem_statement, model_name=args.model_name,
        cache_dir=args.cache_dir, output_dir=args.output_dir,
        api_base=args.api_base, api_key=args.api_key,
        step_limit=args.step_limit, cost_limit=args.cost_limit,
        prebuild_only=args.prebuild_only,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()