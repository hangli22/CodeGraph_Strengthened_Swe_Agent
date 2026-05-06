"""
prebuild.py — 骨架图 + 骨架 embedding 预构建
===============================================
方向二第一阶段：只构建骨架图，embedding 文本来自签名+docstring，
完整解析推迟到 Agent 运行时按需触发。
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

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def is_cache_complete(cache_dir: str) -> bool:
    """
    判断当前 instance 的预构建缓存是否完整。

    当前 structural_retriever.py 已改为粗粒度图关系检索，
    不再依赖旧版结构特征文件：
      - feature_matrix.npy
      - feature_node_ids.json

    因此缓存完整性只要求：
      - code_graph.pkl
      - semantic_embeddings.npy
      - semantic_node_ids.json
      - semantic_texts.json
      - build_info.json
    """
    required = [
        "code_graph.pkl",
        "semantic_embeddings.npy",
        "semantic_node_ids.json",
        "semantic_texts.json",
        "build_info.json",
    ]
    missing = [f for f in required if not os.path.exists(os.path.join(cache_dir, f))]

    if missing:
        logger.info("缓存不完整：%s 缺少 %s", cache_dir, missing)
        return False

    return True


def build_and_save(repo_path: str, cache_dir: str, instance_id: str = "", force: bool = False) -> dict:
    os.makedirs(cache_dir, exist_ok=True)

    if not force and is_cache_complete(cache_dir):
        logger.info("缓存已完整，跳过构建：%s", cache_dir)
        info_path = os.path.join(cache_dir, "build_info.json")
        if os.path.exists(info_path):
            with open(info_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"status": "cached", "cache_dir": cache_dir}

    logger.info("开始构建骨架图：%s%s", repo_path,
                f"  (instance: {instance_id})" if instance_id else "")
    total_start = time.perf_counter()

    # ── Step 1：构建骨架 CodeGraph ────────────────────────────────────
    from code_graph_builder import CodeGraphBuilder
    from code_graph_builder.builder import BuildConfig

    t0 = time.perf_counter()
    cfg = BuildConfig(skeleton_mode=True, enable_annotation=False)
    graph = CodeGraphBuilder(repo_path).build(config=cfg)
    stats = graph.stats()
    logger.info("骨架图构建完成：%.1fs | nodes=%d edges=%d skeleton_files=%d",
                time.perf_counter() - t0, stats["total_nodes"], stats["total_edges"],
                stats.get("skeleton_files", 0))

    # 保存图（使用 pickle dump 整个对象，保留 _file_depth）
    graph_path = os.path.join(cache_dir, "code_graph.pkl")
    with open(graph_path, "wb") as f:
        pickle.dump(graph, f)
    logger.info("CodeGraph 已保存：%s", graph_path)

    # # ── Step 2：结构特征矩阵 ──────────────────────────────────────────
    # from code_graph_retriever.feature_extractor import FeatureExtractor

    # t0 = time.perf_counter()
    # extractor = FeatureExtractor(graph).build()
    # node_ids, matrix = extractor.get_matrix()
    # np.save(os.path.join(cache_dir, "feature_matrix.npy"), matrix)
    # with open(os.path.join(cache_dir, "feature_node_ids.json"), "w", encoding="utf-8") as f:
    #     json.dump(node_ids, f)
    # logger.info("结构特征矩阵：%.1fs | shape=%s", time.perf_counter() - t0, matrix.shape)

    # ── Step 3：骨架 embedding ────────────────────────────────────────
    from code_graph_retriever.semantic_retriever import TFIDFEmbeddingBackend, get_default_embedding_backend
    from code_graph_builder.graph_schema import NodeType

    target_types = {NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD}
    sem_node_ids = []
    sem_texts    = []
    for node in graph.iter_nodes():
        if node.type not in target_types:
            continue
        # 骨架模式：优先用 skeleton_embedding_text()
        text = node.skeleton_embedding_text()
        if not text or text == node.qualified_name:
            text = node.code_text[:500].strip()
        if not text:
            continue
        sem_node_ids.append(node.id)
        sem_texts.append(text)

    logger.info("待 embedding 节点数：%d（骨架模式）", len(sem_node_ids))

    t0      = time.perf_counter()
    backend = get_default_embedding_backend()
    logger.info("embedding 后端：%s", type(backend).__name__)

    if isinstance(backend, TFIDFEmbeddingBackend):
        backend.fit(sem_texts)

    if sem_texts:
        embeddings = backend.embed_batch(sem_texts)
    else:
        dim = getattr(backend, "dim", 64)
        embeddings = np.zeros((0, dim), dtype=np.float32)

    np.save(os.path.join(cache_dir, "semantic_embeddings.npy"), embeddings)
    with open(os.path.join(cache_dir, "semantic_node_ids.json"), "w", encoding="utf-8") as f:
        json.dump(sem_node_ids, f)
    with open(os.path.join(cache_dir, "semantic_texts.json"), "w", encoding="utf-8") as f:
        json.dump(sem_texts, f, ensure_ascii=False)
    logger.info("Embedding 完成：%.1fs | shape=%s", time.perf_counter() - t0, embeddings.shape)

    # ── 构建元信息 ────────────────────────────────────────────────────
    total_elapsed = time.perf_counter() - total_start
    build_info = {
        "status":       "built",
        "build_mode":   "skeleton",
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
    with open(os.path.join(cache_dir, "build_info.json"), "w", encoding="utf-8") as f:
        json.dump(build_info, f, indent=2)

    logger.info("骨架预构建完成 | 总耗时 %.1fs | nodes=%d embedding=%d",
                total_elapsed, stats["total_nodes"], len(sem_node_ids))
    return build_info


def main():
    parser = argparse.ArgumentParser(description="预构建骨架代码图和检索索引")
    parser.add_argument("--repo_path",   required=True)
    parser.add_argument("--cache_dir",   default="/tmp/code_graph_cache")
    parser.add_argument("--instance_id", default="")
    parser.add_argument("--force",       action="store_true")
    args = parser.parse_args()
    info = build_and_save(args.repo_path, args.cache_dir, args.instance_id, args.force)
    print(json.dumps(info, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()