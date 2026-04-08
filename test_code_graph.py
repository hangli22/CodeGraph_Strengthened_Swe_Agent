"""
test_swebench_real.py — 基于真实 SWE-bench 仓库的测试
=======================================================

本文件解决两个问题：
  1. 分析当前模拟仓库与真实 SWE-bench 仓库的差距
  2. 提供两种使用真实数据的测试方案

=============================================================
【差距分析】模拟仓库 vs 真实 SWE-bench 仓库
=============================================================

维度              | 当前模拟仓库              | 真实 SWE-bench 仓库
----------------- | ------------------------- | ------------------------------------------
文件规模          | 5 个 .py 文件             | 平均 ~3000 个非测试文件（最多 5890）
代码行数          | ~50 行                    | 平均 438K 行（最多 886K）
目录深度          | 2 层（utils/）            | 通常 4~6 层（如 django/contrib/admin/...）
包结构            | 无 __init__.py 链         | 完整的 Python 包层次（namespace packages）
AST 复杂度        | 仅简单继承/调用           | 装饰器、metaclass、__init_subclass__、mixin
跨文件导入        | 3 条直接 import           | 百条以上，含相对 import、条件 import、
                  |                           | TYPE_CHECKING 块内的延迟 import
调用图密度        | 7 条 CALLS 边             | 数千条，含高阶函数、回调注册、__call__
继承复杂度        | 单继承                    | 多重继承、mixin 链、ABC、协议类
特殊语法          | 无                        | dataclass、__slots__、property、classmethod
测试文件          | 无                        | tests/ 目录下有数百个测试文件
问题上下文        | 无 issue 信息             | 含 problem_statement、base_commit、patch

关键结论：
  - 模拟仓库只能验证"逻辑正确性"，不能反映"规模压力"下的性能
  - 真实仓库会暴露：图规模爆炸、解析 timeout、特殊语法报错 等问题
  - 建议：模拟仓库用于 CI 快速验证，真实仓库用于集成测试和性能基准

=============================================================
【方案一】使用 HuggingFace datasets 加载 SWE-bench 元数据
          + git clone 对应 base_commit 的仓库快照
=============================================================

安装：pip install datasets gitpython

使用方式（需要网络 + 约 2GB 磁盘）：
    python test_swebench_real.py --mode hf --instance_id psf__requests-1963

=============================================================
【方案二】直接 git clone 真实仓库的特定 commit（推荐，无需HF）
          SWE-bench Lite 中最小的仓库：psf/requests (44 tasks)
=============================================================

使用方式（需要 git + 网络）：
    python test_swebench_real.py --mode git --repo psf/requests
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from code_graph_builder import CodeGraph, CodeGraphBuilder, NodeType, EdgeType
from code_graph_builder.builder import BuildConfig

# ---------------------------------------------------------------------------
# SWE-bench Lite 中真实使用的仓库清单（按规模排序）
# 来源：SWE-bench 论文 Table 1 + Lite 数据集分布
# ---------------------------------------------------------------------------
SWEBENCH_REPOS = {
    "pallets/flask":            {"tasks": 11,  "files_approx": 50,   "lines_approx": 15_000},
    "psf/requests":             {"tasks": 44,  "files_approx": 30,   "lines_approx": 10_000},
    "mwaskom/seaborn":          {"tasks": 22,  "files_approx": 40,   "lines_approx": 20_000},
    "pylint-dev/pylint":        {"tasks": 57,  "files_approx": 200,  "lines_approx": 60_000},
    "pytest-dev/pytest":        {"tasks": 119, "files_approx": 300,  "lines_approx": 80_000},
    "pydata/xarray":            {"tasks": 110, "files_approx": 150,  "lines_approx": 50_000},
    "astropy/astropy":          {"tasks": 95,  "files_approx": 800,  "lines_approx": 200_000},
    "matplotlib/matplotlib":    {"tasks": 184, "files_approx": 700,  "lines_approx": 150_000},
    "sphinx-doc/sphinx":        {"tasks": 187, "files_approx": 400,  "lines_approx": 100_000},
    "scikit-learn/scikit-learn":{"tasks": 229, "files_approx": 500,  "lines_approx": 200_000},
    "sympy/sympy":              {"tasks": 386, "files_approx": 1500, "lines_approx": 500_000},
    "django/django":            {"tasks": 850, "files_approx": 3000, "lines_approx": 500_000},
}

# SWE-bench Lite 中部分 instance_id → base_commit 的真实映射
# 来源：HuggingFace princeton-nlp/SWE-bench_Lite（公开可查）
KNOWN_INSTANCES = {
    "psf__requests-1963": {
        "repo": "psf/requests",
        "base_commit": "110048f9837f8441ea536804115e80b69f400277",
        "problem_statement": (
            "Requests 2.0.0 raise UnicodeDecodeError when server response "
            "has a Content-Type header without charset and body contains non-ascii"
        ),
    },
    "psf__requests-2317": {
        "repo": "psf/requests",
        "base_commit": "b1d43e53b5098a03bc302ca54f59faf9c9df0879",
        "problem_statement": (
            "urllib3 now handles chunked requests differently, "
            "requests should pass Transfer-Encoding header"
        ),
    },
    "pallets__flask-4045": {
        "repo": "pallets/flask",
        "base_commit": "ef9fe8811f7a2a6c3af62eb63d4f7bf9e8bf4aa7",
        "problem_statement": (
            "Flask url_for with SERVER_NAME raises BuildError for relative paths"
        ),
    },
}


# ===========================================================================
# 方案一：HuggingFace datasets 方式（需要网络）
# ===========================================================================

def test_via_huggingface(instance_id: str, work_dir: str) -> None:
    """
    从 HuggingFace 加载 SWE-bench Lite 元数据，
    git clone 对应仓库到 base_commit，然后构建代码图。

    Requirements
    ------------
        pip install datasets gitpython
    """
    try:
        from datasets import load_dataset
        import git
    except ImportError:
        print("请先安装依赖: pip install datasets gitpython")
        return

    print(f"\n{'='*60}")
    print(f"[HF方案] 加载 instance: {instance_id}")
    print(f"{'='*60}")

    # 1. 加载数据集元数据
    dataset = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    instance = next((x for x in dataset if x["instance_id"] == instance_id), None)
    if instance is None:
        print(f"  ✗ 未找到 instance_id={instance_id}")
        return

    repo_slug  = instance["repo"]          # e.g. "psf/requests"
    commit_sha = instance["base_commit"]
    problem    = instance["problem_statement"]
    patch      = instance["patch"]

    print(f"  repo         : {repo_slug}")
    print(f"  base_commit  : {commit_sha[:12]}...")
    print(f"  problem      : {problem[:80]}...")
    print(f"  patch_lines  : {len(patch.splitlines())}")

    # 2. git clone 并 checkout 到 base_commit
    repo_dir = _clone_and_checkout(repo_slug, commit_sha, work_dir)
    if repo_dir is None:
        return

    # 3. 构建代码图并验证
    _build_and_report(repo_dir, instance_id, problem)


# ===========================================================================
# 方案二：直接 git clone（无需 HF，推荐）
# ===========================================================================

def test_via_git(
    repo: str,
    work_dir: str,
    instance_id: Optional[str] = None,
) -> None:
    """
    直接 git clone 指定仓库，可选 checkout 到已知的 base_commit。
    不需要 HuggingFace 账号，只需要能访问 GitHub。

    Parameters
    ----------
    repo         : "owner/repo" 格式，如 "psf/requests"
    work_dir     : 工作目录
    instance_id  : 可选，若在 KNOWN_INSTANCES 中，会 checkout 到 base_commit
    """
    print(f"\n{'='*60}")
    print(f"[Git方案] 克隆仓库: {repo}")
    print(f"{'='*60}")

    # 查找 commit 信息
    commit_sha: Optional[str] = None
    problem: Optional[str] = None
    if instance_id and instance_id in KNOWN_INSTANCES:
        info       = KNOWN_INSTANCES[instance_id]
        commit_sha = info["base_commit"]
        problem    = info["problem_statement"]
        print(f"  base_commit  : {commit_sha[:12]}...")
        print(f"  problem      : {problem[:80]}...")

    repo_dir = _clone_and_checkout(repo, commit_sha, work_dir)
    if repo_dir is None:
        return

    _build_and_report(repo_dir, instance_id or repo, problem)


# ===========================================================================
# 离线方案：使用本地已有的仓库目录（集成到 CI 最方便）
# ===========================================================================

def test_local_repo(repo_path: str, label: str = "local") -> None:
    """
    对已有的本地仓库目录直接构建代码图。
    在 CI/CD 或 Docker 环境中使用，无需网络。

    Usage（集成到 SWE-Agent 流水线）
    ---------------------------------
    # SWE-bench 测评框架会在 Docker 容器内将仓库挂载到 /repo
    test_local_repo("/repo", label="psf__requests-1963")
    """
    print(f"\n{'='*60}")
    print(f"[本地方案] 路径: {repo_path}")
    print(f"{'='*60}")
    _build_and_report(repo_path, label, problem=None)


# ===========================================================================
# 核心：构建代码图并生成报告
# ===========================================================================

def _build_and_report(
    repo_dir: str,
    label: str,
    problem: Optional[str],
) -> CodeGraph:
    """构建多层代码图，输出详细的统计报告。"""

    print(f"\n  构建代码图中...")
    t0 = time.perf_counter()

    builder = CodeGraphBuilder(repo_dir)
    graph   = builder.build()

    elapsed = time.perf_counter() - t0
    stats   = graph.stats()

    # ---- 报告 ----
    print(f"\n  {'─'*50}")
    print(f"  仓库路径   : {repo_dir}")
    print(f"  构建耗时   : {elapsed:.2f}s")
    print(f"  总节点数   : {stats['total_nodes']}")
    print(f"  总边数     : {stats['total_edges']}")
    print(f"  {'─'*50}")

    node_keys = ["nodes_MODULE", "nodes_CLASS", "nodes_FUNCTION", "nodes_METHOD"]
    for k in node_keys:
        print(f"  {k:25s}: {stats.get(k, 0)}")

    print(f"  {'─'*50}")
    edge_keys = [
        "edges_IMPORTS", "edges_CONTAINS", "edges_PARENT_CHILD",
        "edges_SIBLING",  "edges_CALLS",   "edges_INHERITS", "edges_OVERRIDES",
    ]
    for k in edge_keys:
        print(f"  {k:25s}: {stats.get(k, 0)}")

    # ---- 与典型真实规模对比 ----
    print(f"\n  {'─'*50}")
    print(f"  【规模对比】")
    print(f"  {'指标':20s}  {'当前':>10}  {'SWE-bench平均':>15}  {'SWE-bench最大':>15}")
    print(f"  {'─'*65}")
    comparisons = [
        ("MODULE(文件)数",   stats.get("nodes_MODULE",   0), 3010, 5890),
        ("CLASS 数",         stats.get("nodes_CLASS",    0),  500, 5000),
        ("FUNCTION+METHOD数", stats.get("nodes_FUNCTION", 0) +
                              stats.get("nodes_METHOD",   0), 3000, 30000),
        ("CALLS 边数",       stats.get("edges_CALLS",    0), 5000, 50000),
        ("INHERITS 边数",    stats.get("edges_INHERITS", 0),  300, 3000),
    ]
    for name, cur, avg, mx in comparisons:
        bar = "█" * min(int(cur / max(avg, 1) * 20), 40)
        print(f"  {name:20s}  {cur:>10}  {avg:>15}  {mx:>15}   {bar}")

    # ---- 典型节点展示 ----
    print(f"\n  【样本节点（前5个CLASS）】")
    for i, node in enumerate(graph.iter_nodes(NodeType.CLASS)):
        if i >= 5:
            break
        callers  = len(graph.predecessors(node.id, EdgeType.CALLS))
        callees  = len(graph.successors(node.id, EdgeType.CALLS))
        children = len(graph.successors(node.id, EdgeType.PARENT_CHILD))
        parents  = len(graph.successors(node.id, EdgeType.INHERITS))
        print(f"    {node.qualified_name:40s} "
              f"methods={children}  inherits={parents}  "
              f"in_calls={callers}  out_calls={callees}")

    # ---- 问题上下文展示 ----
    if problem:
        print(f"\n  【Issue 描述（前200字）】")
        print(f"  {problem[:200]}")

    # ---- 健壮性检查 ----
    _check_robustness(graph)

    return graph


def _check_robustness(graph: CodeGraph) -> None:
    """检测在真实大型仓库中常见的潜在问题。"""
    print(f"\n  【健壮性检查】")

    # 1. 孤立节点（无任何边相连的节点，可能是解析遗漏）
    isolated = 0
    for node in graph.iter_nodes():
        if node.type == NodeType.MODULE:
            continue
        has_in  = len(graph.predecessors(node.id)) > 0
        has_out = len(graph.successors(node.id)) > 0
        if not has_in and not has_out:
            isolated += 1
    print(f"  孤立节点数   : {isolated} "
          f"{'⚠ 偏高，可能有解析遗漏' if isolated > 50 else '✓'}")

    # 2. code_text 为空的节点（解析时未能提取源码）
    no_text = sum(
        1 for n in graph.iter_nodes()
        if n.type != NodeType.MODULE and len(n.code_text) == 0
    )
    print(f"  无code_text  : {no_text} "
          f"{'⚠' if no_text > 0 else '✓'}")

    # 3. CONTAINS 比率（每个 MODULE 应至少有一条 CONTAINS 边或是空文件）
    modules = list(graph.iter_nodes(NodeType.MODULE))
    modules_with_children = sum(
        1 for m in modules
        if len(graph.successors(m.id, EdgeType.CONTAINS)) > 0
    )
    ratio = modules_with_children / max(len(modules), 1)
    print(f"  有子节点的模块比率: {ratio:.1%} "
          f"{'✓' if ratio > 0.3 else '⚠ 偏低，检查 CONTAINS 边构建'}")

    # 4. 最大出度（调用扇出超高可能是误报）
    max_out = 0
    max_out_node = None
    for node in graph.iter_nodes(NodeType.FUNCTION):
        out = len(graph.successors(node.id, EdgeType.CALLS))
        if out > max_out:
            max_out = out
            max_out_node = node
    if max_out_node:
        print(f"  最大调用扇出 : {max_out} ({max_out_node.qualified_name}) "
              f"{'⚠ 超过100，建议检查误报' if max_out > 100 else '✓'}")

    print(f"  {'─'*50}")


# ===========================================================================
# Git 克隆工具
# ===========================================================================

def _clone_and_checkout(
    repo_slug: str,
    commit_sha: Optional[str],
    work_dir: str,
) -> Optional[str]:
    """
    git clone 并可选 checkout 到指定 commit。
    返回 repo 目录路径，失败返回 None。
    """
    owner, name = repo_slug.split("/")
    repo_dir = os.path.join(work_dir, name)
    url = f"https://github.com/{repo_slug}.git"

    print(f"  git clone {url} ...")
    try:
        subprocess.run(
            ["git", "clone", "--depth=5000", url, repo_dir],
            check=True, capture_output=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  ✗ clone 失败: {e}")
        return None
    except FileNotFoundError:
        print("  ✗ git 命令不存在，请安装 git")
        return None

    if commit_sha:
        try:
            subprocess.run(
                ["git", "-C", repo_dir, "checkout", commit_sha],
                check=True, capture_output=True, timeout=30,
            )
            print(f"  ✓ checkout → {commit_sha[:12]}")
        except subprocess.CalledProcessError:
            # depth=50 可能不含该 commit，降级为 full clone
            print(f"  ⚠ shallow clone 不含目标 commit，尝试 full clone...")
            shutil.rmtree(repo_dir, ignore_errors=True)
            try:
                subprocess.run(
                    ["git", "clone", url, repo_dir],
                    check=True, capture_output=True, timeout=600,
                )
                subprocess.run(
                    ["git", "-C", repo_dir, "checkout", commit_sha],
                    check=True, capture_output=True, timeout=30,
                )
                print(f"  ✓ checkout → {commit_sha[:12]}")
            except subprocess.CalledProcessError as e2:
                print(f"  ✗ checkout 失败: {e2}")
                return None

    return repo_dir


# ===========================================================================
# SWE-bench 集成适配器：在 SWE-Agent 流水线中调用
# ===========================================================================

def build_graph_for_swebench_instance(
    repo_root: str,
    instance_id: str,
    cache_dir: Optional[str] = None,
    ablation_config: Optional[BuildConfig] = None,
) -> CodeGraph:
    """
    SWE-Agent 集成接口。
    在 SWE-bench 的 Docker 容器内，仓库通常挂载于 /repo。
    此函数对 repo_root 构建代码图并可选缓存。

    Parameters
    ----------
    repo_root        : 仓库根目录（Docker 内通常是 /repo）
    instance_id      : SWE-bench instance_id，如 "psf__requests-1963"
    cache_dir        : 图缓存目录（建议 /tmp/code_graphs/），避免重复构建
    ablation_config  : 消融实验配置（对应论文 4.3.3）

    Returns
    -------
    CodeGraph : 已构建的多层代码图

    Example（集成到 SWE-Agent run_instance.py）
    -------------------------------------------
    from test_swebench_real import build_graph_for_swebench_instance

    graph = build_graph_for_swebench_instance(
        repo_root="/repo",
        instance_id=instance["instance_id"],
        cache_dir="/tmp/code_graphs",
    )
    # 之后传入 Section 3.3 的检索模块
    """
    # 尝试从缓存加载
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"{instance_id}.pkl")
        if os.path.isfile(cache_path):
            logger.info("从缓存加载代码图: %s", cache_path)
            return CodeGraph.load_pickle(cache_path)

    # 构建
    config = ablation_config or BuildConfig()
    if cache_dir:
        config.save_path   = os.path.join(cache_dir, f"{instance_id}.pkl")
        config.save_format = "pickle"

    builder = CodeGraphBuilder(repo_root)
    graph   = builder.build(config=config)
    logger.info("代码图构建完成: %s", graph)
    return graph


# ===========================================================================
# 命令行入口
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="基于真实 SWE-bench 仓库测试 code_graph_builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 方案一：HuggingFace（需 pip install datasets gitpython + 网络）
  python test_swebench_real.py --mode hf --instance_id psf__requests-1963

  # 方案二：直接 git clone（只需 git + 网络）
  python test_swebench_real.py --mode git --repo psf/requests

  # 方案三：本地已有仓库（无需网络，最快）
  python test_swebench_real.py --mode local --repo_path /path/to/requests

  # 查看已知 instance 列表
  python test_swebench_real.py --list
        """,
    )
    parser.add_argument("--mode",        choices=["hf", "git", "local"], default="git")
    parser.add_argument("--instance_id", default="psf__requests-1963")
    parser.add_argument("--repo",        default="psf/requests",
                        help="owner/repo 格式，用于 git 模式")
    parser.add_argument("--repo_path",   default="",
                        help="本地仓库目录，用于 local 模式")
    parser.add_argument("--list",        action="store_true",
                        help="列出所有已知 SWE-bench 仓库信息")
    args = parser.parse_args()

    if args.list:
        print("\nSWE-bench Lite 仓库列表（按任务数排序）：")
        print(f"  {'仓库':35s} {'任务数':>6}  {'文件(估)':>10}  {'行数(估)':>12}")
        print("  " + "─"*70)
        for repo, info in sorted(SWEBENCH_REPOS.items(), key=lambda x: x[1]["tasks"]):
            print(f"  {repo:35s} {info['tasks']:>6}  "
                  f"{info['files_approx']:>10,}  {info['lines_approx']:>12,}")
        print()
        print("已知 instance_id：")
        for iid, info in KNOWN_INSTANCES.items():
            print(f"  {iid:35s}  {info['problem_statement'][:50]}...")
        return

    with tempfile.TemporaryDirectory() as tmp:
        if args.mode == "hf":
            test_via_huggingface(args.instance_id, tmp)

        elif args.mode == "git":
            test_via_git(
                repo=args.repo,
                work_dir=tmp,
                instance_id=args.instance_id,
            )

        elif args.mode == "local":
            if not args.repo_path:
                print("--mode local 需要指定 --repo_path")
                return
            test_local_repo(args.repo_path, label=args.instance_id or "local")


if __name__ == "__main__":
    main()

    