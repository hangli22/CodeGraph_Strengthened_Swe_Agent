"""
run_swebench_batch.py — 无 Docker 的 SWE-bench 批量评测脚本
=============================================================

同时支持：
  - 实验组（RetrievalAgent + RetrievalModel，带检索工具）
  - Baseline 组（DefaultAgent + LitellmModel，裸 mini-swe-agent）

通过 --mode 参数切换，保证两组除检索工具外完全一致，控制变量严格对齐。

用法
----
# 实验组（带检索工具）
python mini_swe_agent_integration/run_swebench_batch.py \\
    --mode retrieval \\
    --model_name openai/qwen-plus \\
    --api_base https://dashscope.aliyuncs.com/compatible-mode/v1 \\
    --subset lite \\
    --split test \\
    --slice 0:50 \\
    --output_dir ./results/retrieval \\
    --repos_dir ./repos \\
    --cache_dir ./cache \\
    --workers 1

# Baseline 组（裸 mini-swe-agent，完全相同参数）
python mini_swe_agent_integration/run_swebench_batch.py \\
    --mode baseline \\
    --model_name openai/qwen-plus \\
    --api_base https://dashscope.aliyuncs.com/compatible-mode/v1 \\
    --subset lite \\
    --split test \\
    --slice 0:50 \\
    --output_dir ./results/baseline \\
    --repos_dir ./repos \\
    --workers 1

提交评测（sb-cli）
------------------
sb-cli submit swe-bench_lite test \\
    --predictions_path ./results/retrieval/preds.json \\
    --run_id retrieval-qwen-plus-20260418

sb-cli submit swe-bench_lite test \\
    --predictions_path ./results/baseline/preds.json \\
    --run_id baseline-qwen-plus-20260418

preds.json 格式（与官方一致）
------------------------------
{
  "psf__requests-1963": {
    "model_name_or_path": "qwen-plus",
    "instance_id": "psf__requests-1963",
    "model_patch": "diff --git a/..."
  },
  ...
}

SWE-bench Lite 数据集字段说明
-------------------------------
  instance_id       : 唯一标识，如 psf__requests-1963
  repo              : GitHub 仓库，如 psf/requests
  base_commit       : 需要 checkout 的 commit hash
  problem_statement : issue 描述（传给 Agent 的 task）
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

# 线程锁，保护 preds.json 的并发写入
_PREDS_LOCK = threading.Lock()

# SWE-bench 数据集路径映射（与官方一致）
DATASET_MAPPING = {
    "lite":     "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
    "full":     "princeton-nlp/SWE-bench",
}

# 官方 SWE-bench 的工作目录是 /testbed，本地模式用仓库根目录
LOCAL_WORKDIR = ""  # 由 repo_path 决定

# ── System prompt（与 run_swebench.py 保持一致，实验组额外追加工具说明）────

_SYSTEM_TEMPLATE_BASE = """\
You are a helpful assistant that can interact with a computer shell to solve programming tasks.
"""

_INSTANCE_TEMPLATE = """\
<pr_description>
Consider the following PR description:
{{task}}
</pr_description>

<instructions>
# Task Instructions

## Overview

You're a software engineer interacting continuously with a computer by submitting commands.
Your task is to make changes to non-test files in the current directory in order to fix \
the issue described in the PR description in a way that is general and consistent with the codebase.

For each response:
1. Include a THOUGHT section explaining your reasoning
2. Provide one or more tool calls to execute

## Important Boundaries
- MODIFY: Regular source code files in the repository
- DO NOT MODIFY: Tests, configuration files (pyproject.toml, setup.cfg, etc.)

## Recommended Workflow
1. Analyze the codebase by finding and reading relevant files
2. Create a script to reproduce the issue
3. Edit the source code to resolve the issue
4. Verify your fix works by running your script again
5. Test edge cases to ensure your fix is robust

## Submission
When you've completed your work, submit your changes as a git patch.

Step 1: Create the patch file
```
git diff -- path/to/file1 path/to/file2 > patch.txt
```

Step 2: Verify your patch
Inspect patch.txt to confirm it contains your intended changes.

Step 3: Submit (EXACT command required)
```
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt
```
</instructions>
"""

_RETRIEVAL_TOOL_SECTION = """
## Code Graph Retrieval Tools

In addition to bash, you have THREE specialized retrieval tools that analyze the \
repository's call graph and semantic structure. Use them to find relevant code faster:

### search_hybrid(query, top_k=5) — RECOMMENDED, use this first
Combines structural graph analysis + semantic similarity.
- Best for: initial exploration, finding bug locations, searching by issue keywords
- Results include: file location, scores, structural match reason, semantic relevance,
  structural position (who calls this, what it depends on, impact of modifications)

### search_structural(node_id, top_k=5)
Finds functions that play the same structural role in the call graph.
- node_id format: 'path/to/file.py::ClassName.method_name'
- Best for: after finding a relevant function, finding similar ones

### search_semantic(query, top_k=5)
Finds functions by natural language description similarity.
- Best for: when you know what a function should do but not its name
"""


# ===========================================================================
# 仓库管理
# ===========================================================================

def prepare_repo(instance: dict, repos_dir: str) -> str:
    """
    克隆并 checkout 到 base_commit，返回本地仓库路径。
    幂等：已存在则直接返回路径，不重复克隆。
    """
    instance_id = instance["instance_id"]
    repo        = instance["repo"]            # 如 psf/requests
    base_commit = instance["base_commit"]

    # 以 instance_id 作为目录名，避免同仓库不同 commit 冲突
    repo_path = os.path.join(repos_dir, instance_id)

    if os.path.exists(os.path.join(repo_path, ".git")):
        # 已存在：只需 checkout 到正确 commit
        logger.info("[%s] 仓库已存在，checkout %s", instance_id, base_commit[:8])
        _run(["git", "checkout", "-f", base_commit], cwd=repo_path)
        # 清理未追踪文件（避免上次运行的残留影响）
        _run(["git", "clean", "-fdx"], cwd=repo_path)
        return repo_path

    # 不存在：克隆
    os.makedirs(repos_dir, exist_ok=True)
    clone_url = f"https://github.com/{repo}.git"
    logger.info("[%s] 克隆 %s ...", instance_id, clone_url)
    _run(["git", "clone", clone_url, repo_path])
    _run(["git", "checkout", base_commit], cwd=repo_path)
    logger.info("[%s] 仓库准备完成：%s", instance_id, repo_path)
    return repo_path


def _run(cmd: list[str], cwd: str = "") -> subprocess.CompletedProcess:
    """运行 shell 命令，失败时抛出异常。"""
    result = subprocess.run(
        cmd, cwd=cwd or None,
        capture_output=True, text=True,
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
            preds = json.loads(open(preds_path).read())
        preds[instance_id] = {
            "model_name_or_path": model_name,
            "instance_id":        instance_id,
            "model_patch":        patch,
        }
        with open(preds_path, "w") as f:
            json.dump(preds, f, indent=2)


def load_completed_ids(output_dir: str) -> set[str]:
    """从 preds.json 中读取已完成的 instance_id 集合。"""
    preds_path = os.path.join(output_dir, "preds.json")
    if not os.path.exists(preds_path):
        return set()
    return set(json.loads(open(preds_path).read()).keys())


# ===========================================================================
# 单个 instance 处理
# ===========================================================================

def process_instance(
    instance:    dict,
    mode:        str,    # "retrieval" 或 "baseline"
    model_name:  str,
    api_base:    str,
    api_key:     str,
    output_dir:  str,
    repos_dir:   str,
    cache_dir:   str,
    step_limit:  int,
    cost_limit:  float,
    alpha:       float,
    beta:        float,
) -> dict:
    """
    处理单个 instance，返回摘要字典。
    两种 mode 的唯一区别：Agent/Model 类型 + system_template。
    """
    instance_id = instance["instance_id"]
    logger.info("[%s] 开始处理 (mode=%s)", instance_id, mode)
    t0 = time.perf_counter()

    # ── 准备仓库 ────────────────────────────────────────────────────────
    try:
        repo_path = prepare_repo(instance, repos_dir)
    except Exception as e:
        logger.error("[%s] 仓库准备失败: %s", instance_id, e)
        update_preds_file(output_dir, instance_id, model_name, "")
        return {"instance_id": instance_id, "exit_status": "RepoPrepFailed", "error": str(e)}

    # ── 预构建（仅实验组）────────────────────────────────────────────────
    instance_cache_dir = os.path.join(cache_dir, instance_id)
    if mode == "retrieval":
        try:
            from mini_swe_agent_integration.prebuild import build_and_save
            os.environ["CODE_GRAPH_CACHE_DIR"] = instance_cache_dir
            build_and_save(repo_path=repo_path, cache_dir=instance_cache_dir,
                           instance_id=instance_id)
        except Exception as e:
            logger.error("[%s] 预构建失败: %s", instance_id, e)
            # 预构建失败仍继续运行（降级为无检索工具，不中断整体评测）
            logger.warning("[%s] 预构建失败，将以无检索工具模式运行", instance_id)

    # ── 初始化模型和 Agent ───────────────────────────────────────────────
    from minisweagent.environments.local import LocalEnvironment

    model_kwargs: dict = {"drop_params": True, "temperature": 0.0}
    if api_base:
        model_kwargs["api_base"] = api_base
    # API Key：优先参数传入，其次环境变量
    _key = api_key or os.environ.get("DASHSCOPE_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    if _key:
        model_kwargs["api_key"] = _key

    # trajectory 保存路径
    instance_dir = os.path.join(output_dir, instance_id)
    os.makedirs(instance_dir, exist_ok=True)
    traj_path = Path(os.path.join(instance_dir, f"{instance_id}.traj.json"))

    if mode == "retrieval":
        from mini_swe_agent_integration.retrieval_model import RetrievalModel
        from mini_swe_agent_integration.retrieval_agent import RetrievalAgent

        model = RetrievalModel(
            model_name   = model_name,
            model_kwargs = model_kwargs,
            cost_tracking = "ignore_errors",
        )
        system_template = _SYSTEM_TEMPLATE_BASE + _RETRIEVAL_TOOL_SECTION
        agent = RetrievalAgent(
            model,
            LocalEnvironment(cwd=repo_path),
            system_template   = system_template,
            instance_template = _INSTANCE_TEMPLATE,
            step_limit        = step_limit,
            cost_limit        = cost_limit,
            output_path       = traj_path,
        )
    else:
        # baseline：原版 DefaultAgent + LitellmModel
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.models.litellm_model import LitellmModel

        model = LitellmModel(
            model_name   = model_name,
            model_kwargs = model_kwargs,
            cost_tracking = "ignore_errors",
        )
        agent = DefaultAgent(
            model,
            LocalEnvironment(cwd=repo_path),
            system_template   = _SYSTEM_TEMPLATE_BASE,
            instance_template = _INSTANCE_TEMPLATE,
            step_limit        = step_limit,
            cost_limit        = cost_limit,
            output_path       = traj_path,
        )

    # ── 运行 Agent ───────────────────────────────────────────────────────
    exit_status = ""
    submission  = ""
    extra_info  = {}

    try:
        result     = agent.run(task=instance["problem_statement"])
        exit_status = result.get("exit_status", "")
        submission  = result.get("submission", "")
    except Exception as e:
        exit_status = type(e).__name__
        submission  = ""
        extra_info  = {"traceback": traceback.format_exc(), "exception_str": str(e)}
        logger.error("[%s] Agent 异常: %s", instance_id, e)
    finally:
        # 保存 trajectory（追加额外信息）
        agent.save(
            traj_path,
            {"info": {"exit_status": exit_status, "submission": submission,
                      "mode": mode, **extra_info},
             "instance_id": instance_id},
        )

    # ── 写入 preds.json ─────────────────────────────────────────────────
    update_preds_file(output_dir, instance_id, model_name, submission)

    elapsed = time.perf_counter() - t0
    retrieval_stats = getattr(agent, "retrieval_call_counts", {})
    summary = {
        "instance_id":     instance_id,
        "mode":            mode,
        "exit_status":     exit_status,
        "n_steps":         agent.n_calls,
        "cost":            agent.cost,
        "elapsed_s":       round(elapsed, 1),
        "retrieval_stats": retrieval_stats,
        "has_patch":       bool(submission and submission.strip()),
    }
    logger.info(
        "[%s] 完成 | exit=%s steps=%d cost=$%.4f elapsed=%.0fs patch=%s retrieval=%s",
        instance_id, exit_status, agent.n_calls, agent.cost,
        elapsed, summary["has_patch"], retrieval_stats,
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
    ds = load_dataset(dataset_path, split=split, trust_remote_code=True)
    instances = list(ds)
    logger.info("数据集加载完成：共 %d 个 instance", len(instances))
    return instances


def filter_instances(
    instances:   list[dict],
    slice_spec:  str = "",
    filter_spec: str = "",
) -> list[dict]:
    """按 slice 和 filter 筛选 instance。"""
    if filter_spec:
        before = len(instances)
        instances = [i for i in instances if re.match(filter_spec, i["instance_id"])]
        logger.info("filter: %d → %d", before, len(instances))

    if slice_spec:
        before = len(instances)
        parts  = [int(x) if x else None for x in slice_spec.split(":")]
        instances = instances[slice(*parts)]
        logger.info("slice %s: %d → %d", slice_spec, before, len(instances))

    return instances


# ===========================================================================
# 批量处理主函数
# ===========================================================================

def run_batch(
    mode:        str,
    model_name:  str,
    api_base:    str,
    api_key:     str,
    output_dir:  str,
    repos_dir:   str,
    cache_dir:   str,
    instances:   list[dict],
    workers:     int   = 1,
    step_limit:  int   = 50,
    cost_limit:  float = 3.0,
    redo:        bool  = False,
    alpha:       float = 0.4,
    beta:        float = 0.6,
) -> list[dict]:
    """
    批量运行所有 instance，返回摘要列表。

    Parameters
    ----------
    mode        : "retrieval" 或 "baseline"
    workers     : 并行线程数（建议 1，避免 API 限流）
    redo        : True 则重新运行已完成的 instance
    alpha/beta  : 仅实验组有效，结构/语义权重
    """
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(repos_dir,  exist_ok=True)
    if mode == "retrieval":
        os.makedirs(cache_dir, exist_ok=True)

    # 跳过已完成的 instance（幂等）
    if not redo:
        completed = load_completed_ids(output_dir)
        if completed:
            logger.info("跳过已完成的 %d 个 instance", len(completed))
            instances = [i for i in instances if i["instance_id"] not in completed]

    logger.info(
        "开始批量评测 | mode=%s model=%s instances=%d workers=%d step_limit=%d",
        mode, model_name, len(instances), workers, step_limit,
    )

    summaries = []
    lock = threading.Lock()

    def _process(instance):
        summary = process_instance(
            instance    = instance,
            mode        = mode,
            model_name  = model_name,
            api_base    = api_base,
            api_key     = api_key,
            output_dir  = output_dir,
            repos_dir   = repos_dir,
            cache_dir   = cache_dir,
            step_limit  = step_limit,
            cost_limit  = cost_limit,
            alpha       = alpha,
            beta        = beta,
        )
        with lock:
            summaries.append(summary)
            _print_progress(summaries, len(instances) + len(load_completed_ids(output_dir)))
        return summary

    if workers == 1:
        # 单线程：顺序执行，便于调试
        for instance in instances:
            _process(instance)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process, inst): inst["instance_id"]
                       for inst in instances}
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    iid = futures[future]
                    logger.error("[%s] 线程异常: %s", iid, e)

    # 保存摘要
    summary_path = os.path.join(output_dir, "run_summary.json")
    with open(summary_path, "w") as f:
        json.dump({
            "mode":       mode,
            "model_name": model_name,
            "total":      len(summaries),
            "instances":  summaries,
        }, f, indent=2)
    logger.info("摘要已保存：%s", summary_path)

    _print_final_stats(summaries, mode)
    return summaries


def _print_progress(summaries: list[dict], total: int) -> None:
    completed = len(summaries)
    has_patch = sum(1 for s in summaries if s.get("has_patch"))
    submitted = sum(1 for s in summaries if s.get("exit_status") == "Submitted")
    logger.info(
        "进度: %d/%d | 有patch: %d | Submitted: %d",
        completed, total, has_patch, submitted,
    )


def _print_final_stats(summaries: list[dict], mode: str) -> None:
    total     = len(summaries)
    submitted = sum(1 for s in summaries if s.get("exit_status") == "Submitted")
    has_patch = sum(1 for s in summaries if s.get("has_patch"))
    limits    = sum(1 for s in summaries if s.get("exit_status") == "LimitsExceeded")

    total_retrieval = sum(
        sum(s.get("retrieval_stats", {}).values()) for s in summaries
    )

    print("\n" + "=" * 60)
    print(f"批量评测完成 | mode={mode}")
    print(f"  总数:         {total}")
    print(f"  Submitted:    {submitted} ({100*submitted/max(total,1):.1f}%)")
    print(f"  有 patch:     {has_patch} ({100*has_patch/max(total,1):.1f}%)")
    print(f"  LimitsExceed: {limits}")
    if mode == "retrieval":
        print(f"  检索总调用:   {total_retrieval} 次（平均 {total_retrieval/max(total,1):.1f} 次/instance）")
    print("=" * 60)
    print("注意：Submitted 率和有patch率不等于实际解决率。")
    print("      请用 sb-cli 提交 preds.json 获取真实 resolve_rate。")


# ===========================================================================
# 命令行入口
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="SWE-bench 批量评测（无 Docker，本地环境）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 实验组（带检索工具），先跑 10 个调试
  python mini_swe_agent_integration/run_swebench_batch.py \\
      --mode retrieval \\
      --model_name openai/qwen-plus \\
      --api_base https://dashscope.aliyuncs.com/compatible-mode/v1 \\
      --slice 0:10 --output_dir ./results/retrieval_debug

  # Baseline 组（完全相同参数，只改 --mode）
  python mini_swe_agent_integration/run_swebench_batch.py \\
      --mode baseline \\
      --model_name openai/qwen-plus \\
      --api_base https://dashscope.aliyuncs.com/compatible-mode/v1 \\
      --slice 0:10 --output_dir ./results/baseline_debug

  # 提交评测
  sb-cli submit swe-bench_lite test \\
      --predictions_path ./results/retrieval_debug/preds.json \\
      --run_id retrieval-debug-$(date +%%Y%%m%%d)
        """,
    )

    # ── 模式 ──────────────────────────────────────────────────────────────
    parser.add_argument("--mode", required=True, choices=["retrieval", "baseline"],
                        help="retrieval=实验组（带检索工具），baseline=对照组（裸mini-swe-agent）")

    # ── 模型 ──────────────────────────────────────────────────────────────
    parser.add_argument("--model_name", default="openai/qwen-plus",
                        help="litellm 格式模型名，如 openai/qwen-plus")
    parser.add_argument("--api_base",
                        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        help="API 接入点")
    parser.add_argument("--api_key", default="",
                        help="API Key（默认读取 DASHSCOPE_API_KEY 环境变量）")

    # ── 数据集 ────────────────────────────────────────────────────────────
    parser.add_argument("--subset", default="lite",
                        choices=["lite", "verified", "full"],
                        help="SWE-bench 子集")
    parser.add_argument("--split",  default="test",
                        help="数据集分割，通常为 test 或 dev")
    parser.add_argument("--slice",  default="",
                        help="实例切片，如 0:50 表示前 50 个")
    parser.add_argument("--filter", default="",
                        help="按 instance_id 正则过滤，如 psf__requests")

    # ── 路径 ──────────────────────────────────────────────────────────────
    parser.add_argument("--output_dir", default="./results/retrieval",
                        help="结果输出目录（含 preds.json 和 trajectories）")
    parser.add_argument("--repos_dir",  default="./repos",
                        help="仓库克隆目录")
    parser.add_argument("--cache_dir",  default="./cache",
                        help="代码图缓存目录（仅实验组使用）")

    # ── 运行参数 ──────────────────────────────────────────────────────────
    parser.add_argument("--workers",    type=int,   default=1,
                        help="并行线程数（建议 1 避免 API 限流）")
    parser.add_argument("--step_limit", type=int,   default=50,
                        help="每个 instance 的最大步数")
    parser.add_argument("--cost_limit", type=float, default=3.0,
                        help="每个 instance 的最大花费（美元）")
    parser.add_argument("--redo",       action="store_true",
                        help="重新运行已完成的 instance")

    # ── 消融实验参数（仅实验组） ──────────────────────────────────────────
    parser.add_argument("--alpha", type=float, default=0.4,
                        help="结构检索权重（默认 0.4，设为 1.0 为纯结构）")
    parser.add_argument("--beta",  type=float, default=0.6,
                        help="语义检索权重（默认 0.6，设为 1.0 为纯语义）")

    args = parser.parse_args()

    # 加载数据集
    instances = load_instances(args.subset, args.split)
    instances = filter_instances(instances,
                                  slice_spec  = args.slice,
                                  filter_spec = args.filter)

    if not instances:
        logger.error("没有符合条件的 instance，退出")
        sys.exit(1)

    run_batch(
        mode       = args.mode,
        model_name = args.model_name,
        api_base   = args.api_base,
        api_key    = args.api_key,
        output_dir = args.output_dir,
        repos_dir  = args.repos_dir,
        cache_dir  = args.cache_dir,
        instances  = instances,
        workers    = args.workers,
        step_limit = args.step_limit,
        cost_limit = args.cost_limit,
        redo       = args.redo,
        alpha      = args.alpha,
        beta       = args.beta,
    )


if __name__ == "__main__":
    main()