"""
prebuild.py — Agent 启动前预构建代码图和检索索引
=================================================

用法
----
# 对本地仓库预构建
python mini_swe_agent_integration/prebuild.py \
    --repo_path /path/to/repo \
    --cache_dir /tmp/code_graph_cache

# SWE-bench 场景：仓库挂载于 /repo
python mini_swe_agent_integration/prebuild.py \
    --repo_path /repo \
    --cache_dir /tmp/code_graph_cache \
    --instance_id psf__requests-1963

构建流程
--------
1. 检查缓存是否完整（幂等，存在则跳过）
2. 构建 CodeGraph（AST + 调用图 + 继承图）
3. 计算结构特征矩阵（纯图计算，快）
4. 生成语义 embedding（调用 DashScope API 或 TF-IDF 本地降级）
5. 保存所有文件到 cache_dir

缓存文件
--------
<cache_dir>/
  code_graph.pkl            ← CodeGraph 对象（pickle）
  feature_matrix.npy        ← 结构特征矩阵（numpy）
  feature_node_ids.json     ← 特征矩阵行对应的 node_id 列表
  semantic_embeddings.npy   ← embedding 向量矩阵（numpy）
  semantic_node_ids.json    ← embedding 矩阵行对应的 node_id 列表
  semantic_texts.json       ← 各节点的检索文本（用于 TF-IDF 重建）
  build_info.json           ← 构建时间、仓库路径等元信息
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np

# 确保父目录在 path 中
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def is_cache_complete(cache_dir: str) -> bool:
    """检查缓存是否完整（所有必要文件都存在）。"""
    required = [
        "code_graph.pkl",
        "feature_matrix.npy",
        "feature_node_ids.json",
        "semantic_embeddings.npy",
        "semantic_node_ids.json",
    ]
    return all(os.path.exists(os.path.join(cache_dir, f)) for f in required)


def build_and_save(
    repo_path:  str,
    cache_dir:  str,
    instance_id: str = "",
    force:      bool = False,
) -> dict:
    """
    构建代码图和检索索引，保存到 cache_dir。

    Parameters
    ----------
    repo_path   : 仓库根目录路径
    cache_dir   : 缓存保存目录
    instance_id : SWE-bench instance id（用于日志，可选）
    force       : 即使缓存存在也强制重建

    Returns
    -------
    dict : 构建信息摘要
    """
    os.makedirs(cache_dir, exist_ok=True)

    # 幂等检查
    if not force and is_cache_complete(cache_dir):
        logger.info("缓存已完整，跳过构建：%s", cache_dir)
        info_path = os.path.join(cache_dir, "build_info.json")
        if os.path.exists(info_path):
            return json.loads(open(info_path).read())
        return {"status": "cached", "cache_dir": cache_dir}

    logger.info("开始构建代码图：%s%s", repo_path,
                f"  (instance: {instance_id})" if instance_id else "")
    total_start = time.perf_counter()

    # ── Step 1：构建 CodeGraph ──────────────────────────────────────────
    from code_graph_builder import CodeGraphBuilder
    from code_graph_builder.builder import BuildConfig

    t0  = time.perf_counter()
    cfg = BuildConfig(enable_annotation=False)   # 不调用 LLM 注释（耗时），只构建结构
    graph = CodeGraphBuilder(repo_path).build(config=cfg)
    stats = graph.stats()
    logger.info("CodeGraph 构建完成：%.1fs | nodes=%d edges=%d",
                time.perf_counter() - t0, stats["total_nodes"], stats["total_edges"])

    # 保存图
    graph_path = os.path.join(cache_dir, "code_graph.pkl")
    with open(graph_path, "wb") as f:
        pickle.dump(graph, f)
    logger.info("CodeGraph 已保存：%s", graph_path)

    # ── Step 2：结构特征矩阵 ────────────────────────────────────────────
    from code_graph_retriever.feature_extractor import FeatureExtractor

    t0        = time.perf_counter()
    extractor = FeatureExtractor(graph).build()
    node_ids, matrix = extractor.get_matrix()

    np.save(os.path.join(cache_dir, "feature_matrix.npy"), matrix)
    with open(os.path.join(cache_dir, "feature_node_ids.json"), "w") as f:
        json.dump(node_ids, f)
    logger.info("结构特征矩阵：%.1fs | shape=%s", time.perf_counter() - t0, matrix.shape)

    # ── Step 3：语义 embedding ──────────────────────────────────────────
    from code_graph_retriever.semantic_retriever import (
        SemanticRetriever, TFIDFEmbeddingBackend, get_default_embedding_backend
    )
    from code_graph_builder.graph_schema import NodeType

    # 收集节点文本（与 SemanticRetriever._collect_nodes 逻辑一致）
    target_types = {NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD}
    sem_node_ids = []
    sem_texts    = []
    for node in graph.iter_nodes():
        if node.type not in target_types:
            continue
        text = node.comment.strip() if node.comment.strip() else node.code_text[:500].strip()
        if not text:
            continue
        sem_node_ids.append(node.id)
        sem_texts.append(text)

    logger.info("待 embedding 节点数：%d", len(sem_node_ids))

    t0      = time.perf_counter()
    backend = get_default_embedding_backend()
    logger.info("embedding 后端：%s", type(backend).__name__)

    if isinstance(backend, TFIDFEmbeddingBackend):
        backend.fit(sem_texts)

    # 批量 embedding
    if sem_texts:
        embeddings = backend.embed_batch(sem_texts)
    else:
        # 空仓库兜底
        dim = getattr(backend, "dim", 64)
        embeddings = np.zeros((0, dim), dtype=np.float32)

    np.save(os.path.join(cache_dir, "semantic_embeddings.npy"), embeddings)
    with open(os.path.join(cache_dir, "semantic_node_ids.json"), "w") as f:
        json.dump(sem_node_ids, f)
    with open(os.path.join(cache_dir, "semantic_texts.json"), "w", encoding="utf-8") as f:
        json.dump(sem_texts, f, ensure_ascii=False)
    logger.info("Embedding 完成：%.1fs | shape=%s", time.perf_counter() - t0, embeddings.shape)

    # ── 保存构建元信息 ──────────────────────────────────────────────────
    total_elapsed = time.perf_counter() - total_start
    build_info = {
        "status":       "built",
        "instance_id":  instance_id,
        "repo_path":    repo_path,
        "cache_dir":    cache_dir,
        "graph_stats":  stats,
        "n_semantic":   len(sem_node_ids),
        "embed_dim":    int(embeddings.shape[1]) if embeddings.size else 0,
        "embed_backend": type(backend).__name__,
        "total_elapsed_s": round(total_elapsed, 2),
        "built_at":     time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(os.path.join(cache_dir, "build_info.json"), "w") as f:
        json.dump(build_info, f, indent=2)

    logger.info(
        "预构建完成 | 总耗时 %.1fs | nodes=%d embedding=%d dim=%d",
        total_elapsed, stats["total_nodes"], len(sem_node_ids),
        int(embeddings.shape[1]) if embeddings.size else 0
    )
    return build_info


def main():
    parser = argparse.ArgumentParser(
        description="预构建代码图和检索索引（在 Agent 运行前执行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # SWE-bench 场景（仓库挂载于 /repo）
  python mini_swe_agent_integration/prebuild.py \\
      --repo_path /repo \\
      --cache_dir /tmp/code_graph_cache

  # 本地仓库
  python mini_swe_agent_integration/prebuild.py \\
      --repo_path /path/to/requests \\
      --cache_dir /tmp/code_graph_cache \\
      --instance_id psf__requests-1963

  # 强制重建（忽略已有缓存）
  python mini_swe_agent_integration/prebuild.py \\
      --repo_path /repo --cache_dir /tmp/cache --force
        """,
    )
    parser.add_argument("--repo_path",   required=True, help="仓库根目录路径")
    parser.add_argument("--cache_dir",   default="/tmp/code_graph_cache", help="缓存目录")
    parser.add_argument("--instance_id", default="",    help="SWE-bench instance id（可选，用于日志）")
    parser.add_argument("--force",       action="store_true", help="强制重建，忽略已有缓存")
    args = parser.parse_args()

    info = build_and_save(
        repo_path   = args.repo_path,
        cache_dir   = args.cache_dir,
        instance_id = args.instance_id,
        force       = args.force,
    )
    print(json.dumps(info, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()