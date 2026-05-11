"""
retrieval_tools.py — 检索工具 + deepen_file 工具
==================================================

提供给 RetrievalAgent / RetrievalModel 的工具函数：

- search_hybrid:     语义 + 结构混合检索，用自然语言 query 找相关节点
- search_semantic:   语义检索，用自然语言 query 找相关节点
- search_structural: 粗粒度结构关系检索，用已知 node_id 做关系扩展
- deepen_file:       按需深化文件，补充方法级节点和调用边

注意：
当前 structural_retriever.py 已改为“基于粗粒度图关系的结构检索”，
不再使用旧版 FeatureExtractor / feature_matrix / NearestNeighbors 结构向量检索。
"""

from __future__ import annotations

import json
import logging
import os
import pickle
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_cache: dict = {}
MAX_DEEPEN_FILES = 20

# 每个工具的进程内调用次数。
# 用于让 initial_issue_focus 在 BM25 多路召回中的权重随检索轮次指数衰减。
_retrieval_call_counts: Dict[str, int] = {}


def _next_retrieval_step(tool_name: str) -> int:
    """
    返回当前工具调用轮次，并递增计数。

    step 从 0 开始：
      - 第一次 search_hybrid 使用 step=0，issue_focus 权重最高；
      - 后续 search_hybrid 逐步增大 step，issue_focus 权重指数衰减。
    """
    step = _retrieval_call_counts.get(tool_name, 0)
    _retrieval_call_counts[tool_name] = step + 1
    return step


def _get_cache_dir() -> str:
    return os.environ.get("CODE_GRAPH_CACHE_DIR", "/tmp/code_graph_cache")


def clear_retrieval_cache() -> None:
    """
    清空 retrieval_tools 的进程内缓存。

    必须在切换 SWE-bench instance / repo / CODE_GRAPH_CACHE_DIR 后调用。
    否则 _cache 中可能仍然保存上一个 instance 的 graph、semantic retriever、
    structural retriever，导致新 instance 使用旧代码图。
    """
    n = len(_cache)
    _cache.clear()
    _retrieval_call_counts.clear()
    logger.info("已清空 retrieval_tools 进程内缓存：%d 项", n)


def _load_cached(key: str, loader):
    if key not in _cache:
        _cache[key] = loader()
    return _cache[key]


def _load_graph():
    """
    从当前 CODE_GRAPH_CACHE_DIR 加载 CodeGraph。

    注意：
    这个函数只负责从磁盘加载；是否复用由 _load_cached 控制。
    切换 instance 前必须调用 clear_retrieval_cache()，否则不会进入这里。
    """
    cache_dir = _get_cache_dir()
    path = os.path.join(cache_dir, "code_graph.pkl")

    if not os.path.exists(path):
        raise FileNotFoundError(f"代码图缓存不存在：{path}\n请先运行 prebuild.py")

    with open(path, "rb") as f:
        graph = pickle.load(f)

    logger.info("加载代码图: %s", path)
    logger.info("代码图 repo_root: %s", getattr(graph, "repo_root", None))

    try:
        stats = graph.stats()
        logger.info(
            "代码图统计: nodes=%s edges=%s",
            stats.get("total_nodes"),
            stats.get("total_edges"),
        )
    except Exception:
        logger.debug("读取代码图统计失败", exc_info=True)

    return graph


def _load_structural_retriever():
    """
    加载粗粒度结构检索器。

    当前 StructuralRetriever 已经是“基于粗粒度图关系”的实现：
      - siblings
      - inheritance
      - dependencies
      - co_dependents
      - related

    因此这里不再加载 feature_matrix.npy / feature_node_ids.json，
    也不再使用 FeatureExtractor / NearestNeighbors。
    """
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from code_graph_retriever.structural_retriever import StructuralRetriever

    graph = _load_cached("graph", _load_graph)
    retriever = StructuralRetriever(graph).build()
    return retriever


def _load_semantic_retriever():
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from code_graph_retriever.semantic_retriever import (
        SemanticRetriever,
        TFIDFEmbeddingBackend,
        get_default_embedding_backend,
        DashScopeEmbeddingBackend,
    )

    cache_dir = _get_cache_dir()
    graph = _load_cached("graph", _load_graph)
    emb_path = os.path.join(cache_dir, "semantic_embeddings.npy")
    node_ids_path = os.path.join(cache_dir, "semantic_node_ids.json")
    texts_path = os.path.join(cache_dir, "semantic_texts.json")

    if os.path.exists(emb_path) and os.path.exists(node_ids_path):
        matrix = np.load(emb_path)
        with open(node_ids_path, "r", encoding="utf-8") as f:
            node_ids = json.load(f)

        if os.path.exists(texts_path):
            with open(texts_path, "r", encoding="utf-8") as f:
                texts = json.load(f)
        else:
            texts = [""] * len(node_ids)

        backend = None

        ds_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if ds_key:
            try:
                backend = DashScopeEmbeddingBackend(api_key=ds_key)
            except Exception:
                logger.warning("DashScopeEmbeddingBackend 初始化失败，回退到 TFIDF", exc_info=True)

        if backend is None:
            backend = TFIDFEmbeddingBackend(
                n_components=min(64, max(1, len(texts) - 1))
            )
            if texts and any(t for t in texts):
                backend.fit(texts)

        retriever = SemanticRetriever(graph, backend=backend)
        retriever._node_ids = node_ids
        retriever._texts = texts
        retriever._matrix = matrix

        from sklearn.neighbors import NearestNeighbors

        retriever._nn = NearestNeighbors(metric="cosine", algorithm="brute")
        retriever._nn.fit(matrix)
        retriever._built = True
    else:
        logger.warning("语义 embedding 缓存不完整，重新构建")
        backend = get_default_embedding_backend()
        retriever = SemanticRetriever(graph, backend=backend).build()

    return retriever


def _load_bm25_retriever():
    """加载 BM25 词法检索器。"""
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from code_graph_retriever.bm25_retriever import BM25Retriever

    graph = _load_cached("graph", _load_graph)
    retriever = BM25Retriever(graph).build()
    return retriever


def _load_hybrid_retriever():
    """
    加载融合检索器，并复用已缓存的 structural / semantic / BM25 分支。

    这样可以避免每次 search_hybrid 都重新构造 BM25 索引，也能保证 deepen_file
    后更新的是同一个进程内 retriever 对象。
    """
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from code_graph_retriever.hybrid_retriever import HybridRetriever

    graph = _load_cached("graph", _load_graph)
    struct_retriever = _load_cached("structural_retriever", _load_structural_retriever)
    sem_retriever = _load_cached("semantic_retriever", _load_semantic_retriever)
    bm25_retriever = _load_cached("bm25_retriever", _load_bm25_retriever)

    retriever = HybridRetriever(graph)
    retriever._structural = struct_retriever
    retriever._semantic = sem_retriever
    retriever._bm25 = bm25_retriever
    retriever._built = True
    return retriever


def _build_bm25_bundle_for_query(query: str, retrieval_step: int = 0):
    """
    在 retrieval_tools 层读取 issue_focus，并构造 BM25 多路查询。

    设计原则：
      - issue_focus 属于 instance/cache 级上下文，不塞进 HybridRetriever 内部。
      - 默认启用 query_focus：每次 search_hybrid 会尝试根据当前 query 更新 query_focus。
      - 如果 LLM 抽取失败，降级为 initial_issue_focus + current_query，不影响检索主流程。
    """
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    try:
        from code_graph_retriever.issue_focus import IssueFocusStore

        store = IssueFocusStore(cache_dir=_get_cache_dir())

        try:
            return store.build_bm25_query_bundle(
                current_query=query,
                include_query_focus=True,
                update_query_focus=True,
                max_queries=12,
                retrieval_step=retrieval_step,
            )
        except Exception as e:
            logger.warning(
                "query_focus 抽取/更新失败，降级为已有 issue_focus + current_query: %s",
                e,
            )
            return store.build_bm25_query_bundle(
                current_query=query,
                include_query_focus=True,
                update_query_focus=False,
                max_queries=12,
                retrieval_step=retrieval_step,
            )
    except Exception as e:
        logger.warning("issue_focus 不可用，BM25 将仅使用当前 query: %s", e)

        class _FallbackBundle:
            current_query = query
            queries = [query] if query and query.strip() else []
            query_groups = {"current_query": queries}
            group_weights = {"current_query": 1.20}

            def to_list(self):
                return self.queries

        return _FallbackBundle()

def _build_deepen_issue_query(explicit_query: str = "") -> str:
    """
    为 deepen_file 构造 issue_query。

    deepen_file 的 method_summary 依赖 issue_query。
    如果 agent 没显式传 issue_query，则从 issue_focus/query_focus 中恢复。
    """
    explicit_query = (explicit_query or "").strip()
    if explicit_query:
        return explicit_query

    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    try:
        from code_graph_retriever.issue_focus import build_deepen_issue_query_from_cache

        q = build_deepen_issue_query_from_cache(
            cache_dir=_get_cache_dir(),
            explicit_query=explicit_query,
        )
        if q.strip():
            return q.strip()

    except Exception as e:
        logger.warning(
            "从 issue_focus 构造 deepen issue_query 失败，将不生成 issue-related method summary: %s",
            e,
        )

    return ""


def _format_bm25_group_summary(bundle: Any) -> str:
    """生成简短 BM25 query bundle 摘要，方便日志和 agent 理解检索依据。"""
    try:
        groups = getattr(bundle, "query_groups", {}) or {}
        parts = []
        for name, qs in groups.items():
            clean = [str(q).strip() for q in (qs or []) if str(q).strip()]
            if clean:
                parts.append(f"{name}:{len(clean)}")
        return ", ".join(parts[:8]) if parts else "current_query only"
    except Exception:
        return "unknown"


# ===========================================================================
# 工具函数
# ===========================================================================

def search_structural(
    node_id: str,
    mode: str = "related",
    top_k: int = 5,
) -> str:
    """
    粗粒度结构关系检索。

    输入必须是已知 node_id，而不是自然语言 query。

    mode 可选：
      - siblings
      - inheritance
      - dependencies
      - co_dependents
      - related
    """
    try:
        import sys

        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        from code_graph_retriever.structural_retriever import StructuralQueryMode

        retriever = _load_cached("structural_retriever", _load_structural_retriever)

        try:
            query_mode = StructuralQueryMode(mode)
        except ValueError:
            valid_modes = [m.value for m in StructuralQueryMode]
            return (
                f"[structural_search ERROR] 无效 mode: {mode!r}\n"
                f"可用 mode: {', '.join(valid_modes)}\n"
                f"说明：search_structural 不是自然语言搜索工具，"
                f"必须输入已知 node_id，并指定关系模式。"
            )

        response = retriever.search(node_id=node_id, mode=query_mode, top_k=top_k)

        if not response.results:
            return (
                f"[structural_search] 未找到与节点 '{node_id}' 相关的结构关系结果。\n"
                f"关系模式: {query_mode.value}\n"
                f"检索范围: {response.total_nodes} 个节点  "
                f"耗时: {response.elapsed_ms:.1f}ms\n"
                f"提示：请确认 node_id 是否来自 search_hybrid/search_semantic 的结果，"
                f"而不是自然语言 query。"
            )

        lines = [
            f"[structural_search] 找到 {len(response.results)} 个粗粒度结构关联节点",
            f"查询节点: {node_id}",
            f"关系模式: {query_mode.value}",
            f"检索范围: {response.total_nodes} 个节点  耗时: {response.elapsed_ms:.1f}ms",
            "=" * 60,
        ]

        for i, r in enumerate(response.results, 1):
            lines += [
                f"\n[{i}] {r.qualified_name}  [{r.node_type}]",
                f"    节点ID: {r.node_id}",
                f"    文件: {r.file}  行: {r.start_line}~{r.end_line}",
                f"    结构评分: {r.structural_score:.3f}",
            ]

            if r.structural_reason:
                lines.append(f"    关系依据: {r.structural_reason}")
            if r.position_summary:
                lines.append(f"    结构位置: {r.position_summary}")
            if r.comment:
                lines.append(f"    功能注释: {r.comment[:120]}")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("structural_search 失败")
        return f"[structural_search ERROR] {type(e).__name__}: {e}"


def search_semantic(query: str, top_k: int = 5) -> str:
    try:
        retriever = _load_cached("semantic_retriever", _load_semantic_retriever)
        response = retriever.search(query, top_k=top_k)

        if not response.results:
            return (
                f"[semantic_search] 未找到与 '{query}' 语义相关的节点。\n"
                f"（共检索 {response.total_nodes} 个节点，耗时 {response.elapsed_ms:.1f}ms）"
            )

        lines = [
            f"[semantic_search] 找到 {len(response.results)} 个语义相关节点",
            f"查询: {query}",
            f"检索范围: {response.total_nodes} 个节点  耗时: {response.elapsed_ms:.1f}ms",
            "=" * 60,
        ]

        for i, r in enumerate(response.results, 1):
            lines += [
                f"\n[{i}] {r.qualified_name}  [{r.node_type}]",
                f"    节点ID: {r.node_id}",
                f"    文件: {r.file}  行: {r.start_line}~{r.end_line}",
                f"    语义评分: {r.semantic_score:.3f}",
            ]

            if r.semantic_reason:
                lines.append(f"    语义关联: {r.semantic_reason}")
            if r.comment:
                lines.append(f"    功能注释: {r.comment[:120]}")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("semantic_search 失败")
        return f"[semantic_search ERROR] {type(e).__name__}: {e}"


def search_bm25(query: str, top_k: int = 10) -> str:
    """
    BM25 词法/符号检索入口。

    用途：
      - 精确匹配文件名、类名、函数名、方法名、参数名、报错词；
      - 使用 issue_focus/query_focus 构造多路短 BM25 query；
      - 返回 BM25 命中的节点及命中 query/group。

    与 search_hybrid 的区别：
      - search_hybrid 会融合 semantic + BM25 + structural；
      - search_bm25 只看词法/符号命中，更适合 exact symbol / file hint / error term 召回。
    """
    try:
        retriever = _load_cached("bm25_retriever", _load_bm25_retriever)

        retrieval_step = _next_retrieval_step("search_bm25")
        bm25_bundle = _build_bm25_bundle_for_query(query, retrieval_step=retrieval_step)

        queries = bm25_bundle.to_list()
        query_groups = getattr(bm25_bundle, "query_groups", {}) or {}
        group_weights = getattr(bm25_bundle, "group_weights", {}) or {}

        if not queries:
            queries = [query.strip()] if query and query.strip() else []

        if not queries:
            return (
                "[bm25_search] query 为空，无法执行 BM25 检索。\n"
                "请提供文件名、符号名、参数名、错误词或简短行为描述。"
            )

        if hasattr(retriever, "search_many"):
            response = retriever.search_many(
                queries,
                top_k=top_k,
                per_query_k=max(top_k * 3, 20),
                query_groups=query_groups,
                group_weights=group_weights,
            )
        else:
            response = retriever.search(query, top_k=top_k)

        if not response.results:
            return (
                f"[bm25_search] 未找到与 '{query}' 词法/符号相关的节点。\n"
                f"BM25查询组: {_format_bm25_group_summary(bm25_bundle)}\n"
                f"BM25 issue_focus 衰减步数: {retrieval_step}"
            )

        lines = [
            f"[bm25_search] 找到 {len(response.results)} 个词法/符号相关节点",
            f"原始查询: {query}",
            f"实际 BM25 queries: {', '.join(queries[:8])}",
            f"BM25查询组: {_format_bm25_group_summary(bm25_bundle)}",
            f"BM25 issue_focus 衰减步数: {retrieval_step}",
            f"检索范围: {response.total_nodes} 个节点  耗时: {response.elapsed_ms:.1f}ms",
            "=" * 60,
        ]

        for i, r in enumerate(response.results, 1):
            bm25_score = float(getattr(r, "bm25_score", getattr(r, "final_score", 0.0)) or 0.0)
            lines += [
                f"\n[{i}] {r.qualified_name}  [{r.node_type}]",
                f"    节点ID: {r.node_id}",
                f"    文件: {r.file}  行: {r.start_line}~{r.end_line}",
                f"    BM25评分: {bm25_score:.3f}",
            ]

            bm25_reason = getattr(r, "bm25_reason", "") or ""
            if bm25_reason:
                lines.append(f"    BM25词法命中: {bm25_reason}")

            hit_queries = getattr(r, "bm25_hit_queries", []) or []
            if hit_queries:
                lines.append(
                    "    BM25命中查询: "
                    + ", ".join(str(x) for x in hit_queries[:5])
                )

            query_group_info = getattr(r, "bm25_query_groups", {}) or {}
            if query_group_info:
                groups = query_group_info.get("groups", [])
                hit_count = query_group_info.get("hit_count", "")
                group_text = ", ".join(str(x) for x in groups[:5])
                if group_text:
                    lines.append(f"    BM25命中分组: {group_text}  hit_count={hit_count}")

            if r.comment:
                lines.append(f"    功能注释: {r.comment[:120]}")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("bm25_search 失败")
        return f"[bm25_search ERROR] {type(e).__name__}: {e}"

def search_hybrid(query: str, top_k: int = 5) -> str:
    """
    混合检索入口。

    当前流程：
      1. semantic search 拿一批候选
      2. retrieval_tools 读取 issue_focus，组装 BM25 多路 query
      3. BM25 多路 search 拿一批候选
      4. 合并 semantic + BM25 候选
      5. 从综合候选里选 top 1~3 个作为 structural expansion seed
      6. structural search_by_node_id(seed)
      7. merge 三路结果

    BM25 不替代 semantic，只负责增强召回；最终仍保留 semantic_score、
    structural_score 和 bm25_score 三路分数。
    """
    try:
        retriever = _load_cached("hybrid_retriever", _load_hybrid_retriever)
        retrieval_step = _next_retrieval_step("search_hybrid")
        bm25_bundle = _build_bm25_bundle_for_query(query, retrieval_step=retrieval_step)
        bm25_queries = bm25_bundle.to_list()
        bm25_query_groups = getattr(bm25_bundle, "query_groups", {}) or {}
        bm25_group_weights = getattr(bm25_bundle, "group_weights", {}) or {}

        response = retriever.search(
            query,
            top_k=top_k,
            bm25_queries=bm25_queries,
            bm25_query_groups=bm25_query_groups,
            bm25_group_weights=bm25_group_weights,
            structural_seed_k=3,
        )

        if not response.results:
            return (
                f"[hybrid_search] 未找到与 '{query}' 相关的节点。\n"
                f"（共检索 {response.total_nodes} 个节点，耗时 {response.elapsed_ms:.1f}ms）\n"
                f"BM25查询组: {_format_bm25_group_summary(bm25_bundle)}"
            )

        lines = [
            f"[hybrid_search] 找到 {len(response.results)} 个相关节点",
            f"查询: {query}",
            f"检索范围: {response.total_nodes} 个节点  耗时: {response.elapsed_ms:.1f}ms",
            "说明: semantic 负责语义候选，BM25(issue_focus+query) 负责词法召回，结构检索负责围绕高置信 seed 扩展关系节点",
            f"BM25查询组: {_format_bm25_group_summary(bm25_bundle)}",
            f"BM25 issue_focus 衰减步数: {retrieval_step}",
            "=" * 60,
        ]

        for i, r in enumerate(response.results, 1):
            bm25_score = float(getattr(r, "bm25_score", 0.0) or 0.0)
            lines += [
                f"\n[{i}] {r.qualified_name}  [{r.node_type}]",
                f"    节点ID: {r.node_id}",
                f"    文件: {r.file}  行: {r.start_line}~{r.end_line}",
                f"    综合评分: {r.final_score:.3f}  "
                f"（结构: {r.structural_score:.3f} | 语义: {r.semantic_score:.3f} | BM25: {bm25_score:.3f}）",
            ]

            if r.structural_reason:
                lines.append(f"    结构关系依据: {r.structural_reason}")
            if r.semantic_reason:
                lines.append(f"    语义关联说明: {r.semantic_reason}")
            bm25_reason = getattr(r, "bm25_reason", "") or ""
            if bm25_reason:
                lines.append(f"    BM25词法命中: {bm25_reason}")
            bm25_hit_queries = getattr(r, "bm25_hit_queries", []) or []
            if bm25_hit_queries:
                hit_q = ", ".join(str(x) for x in bm25_hit_queries[:5])
                lines.append(f"    BM25命中查询: {hit_q}")
            if r.position_summary:
                lines.append(f"    结构位置: {r.position_summary}")
            if r.comment:
                lines.append(f"    功能注释: {r.comment[:120]}")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("hybrid_search 失败")
        return f"[hybrid_search ERROR] {type(e).__name__}: {e}"

def deepen_file(
    file_path: str,
    issue_query: str = "",
    top_methods: int = 5,
    expand_neighbor_classes: bool = True,
    max_neighbor_files: int = 3,
) -> str:
    """
    按需深化文件：完整解析 AST，补充方法级节点和调用关系，更新检索索引。

    Parameters
    ----------
    file_path:
        要深化的文件相对路径。
    issue_query:
        当前 issue 或当前检索 query。可选。
        如果为空，系统会尝试从 issue_focus/query_focus/SWE_ISSUE_TEXT 自动补全。
        只要最终 issue_query 非空，FileDeepener 就会生成 issue-related method_summary。
    top_methods:
        issue-related method summary 的种子方法数量。
    expand_neighbor_classes:
        是否根据相关 method 的相邻类继续深化少量相邻文件。
    max_neighbor_files:
        最多额外深化多少个相邻类文件。
    """
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    graph = _load_cached("graph", _load_graph)

    # semantic retriever 需要提前加载：
    # 1. 给 FileDeepener 提供 embedding_backend；
    # 2. HybridRetriever 内部也会复用同一个 cached semantic retriever。
    sem_ret = _load_cached("semantic_retriever", _load_semantic_retriever)

    # 统一融合检索器。它内部复用 cached structural / semantic / BM25。
    # deepen 后只通过 hybrid_ret.rebuild_after_deepen() 更新三路索引。
    hybrid_ret = _load_cached("hybrid_retriever", _load_hybrid_retriever)

    # 规范化路径
    file_rel = file_path.lstrip("./").replace("\\", "/")
    if "::" in file_rel:
        file_rel = file_rel.split("::")[0]

    # 前置检查
    depth = graph.get_file_depth(file_rel)
    if depth == "full":
        return f"[deepen_file] 文件 {file_rel} 已是完整解析状态，无需深化。"
    if depth == "":
        return f"[deepen_file] 文件 {file_rel} 不在代码图中。请检查路径。"

    deepened = graph.get_deepened_files()
    if len(deepened) >= MAX_DEEPEN_FILES:
        return (
            f"[deepen_file] 已达到最大深化数 ({MAX_DEEPEN_FILES})，无法继续。\n"
            f"已深化文件: {', '.join(deepened[:5])}..."
        )

    # 如果 agent 没传 issue_query，则从 issue_focus/query_focus 中自动恢复。
    # 否则 FileDeepener.deepen(issue_query="") 不会生成 method_summaries。
    deepen_issue_query = _build_deepen_issue_query(issue_query)

    # 执行深化
    from code_graph_builder.file_deepener import FileDeepener

    deepener = FileDeepener(
        graph=graph,
        repo_root=graph.repo_root,
        embedding_backend=sem_ret.backend,
    )

    result = deepener.deepen(
        file_rel=file_rel,
        issue_query=deepen_issue_query,
        top_methods=top_methods,
        expand_neighbor_classes=expand_neighbor_classes,
        max_neighbor_files=max_neighbor_files,
    )

    # 统一更新三路检索索引：
    # - structural：deepen 新增 PARENT_CHILD/SIBLING/CALLS/OVERRIDES，必须 rebuild；
    # - semantic：方案 C，更新已有 CLASS/FUNCTION/METHOD + 追加新增 METHOD；
    # - BM25：已有节点文本变化时 rebuild，只有新增节点时 add_nodes。
    index_update_ok = True
    index_update_error = ""

    try:
        hybrid_ret.rebuild_after_deepen(
            new_node_ids=getattr(result, "new_node_ids", []),
            updated_node_ids=getattr(result, "updated_node_ids", []),
            text_changed=bool(getattr(result, "text_changed", True)),
        )
    except Exception as e:
        index_update_ok = False
        index_update_error = f"{type(e).__name__}: {e}"
        logger.warning(
            "Hybrid 检索索引更新失败: %s（检索仍可用，但可能未包含最新深化内容）",
            e,
            exc_info=True,
        )

    # 生成压缩版汇总：默认返回 5 个 issue-relevant methods，
    # 每个 method summary 最多 3 条 high-confidence call evidence，最多 3 个 imported/neighbor files，
    # 默认不返回 code_preview，避免大文件触发 Output too long。
    remaining = MAX_DEEPEN_FILES - len(graph.get_deepened_files())
    summary_limit = 5
    file_hint_limit = 3
    per_method_call_evidence_limit = 3

    lines = [
        f"[deepen_file] 文件 {file_rel} 已深化为完整解析",
    ]

    if index_update_ok:
        lines.append("索引更新: hybrid semantic/structural/BM25 已刷新")
    else:
        lines.append(f"索引更新: 失败，后续检索可能未包含本次深化内容；错误: {index_update_error}")

    if deepen_issue_query:
        preview = deepen_issue_query.replace("\n", " ")
        if len(preview) > 240:
            preview = preview[:240].rstrip() + "..."
        lines.append(f"Issue/query context: {preview}")
    else:
        lines.append("Issue/query context: <empty>，未生成 issue-related method summary")

    if result.imported_files:
        lines.append("")
        # lines.append(f"关联文件（可进一步深化，最多 {file_hint_limit} 个）:")
        for imp_file in result.imported_files[:file_hint_limit]:
            lines.append(f"  - {imp_file}")
        if len(result.imported_files) > file_hint_limit:
            lines.append(f"  ... 另有 {len(result.imported_files) - file_hint_limit} 个未显示")

    if result.neighbor_deepened_files:
        lines.append("")
        lines.append(f"因相邻类额外深化的文件（最多 {file_hint_limit} 个）:")
        for nf in result.neighbor_deepened_files[:file_hint_limit]:
            lines.append(f"  - {nf}")
        if len(result.neighbor_deepened_files) > file_hint_limit:
            lines.append(f"  ... 另有 {len(result.neighbor_deepened_files) - file_hint_limit} 个未显示")

    if result.method_summaries:
        lines.append("")
        lines.append(f"Issue 相关方法摘要（top {min(summary_limit, len(result.method_summaries))}）:")
        for i, summary in enumerate(result.method_summaries[:summary_limit], 1):
            lines.append(
                f"[{i}] {summary.qualified_name} "
                f"({summary.file}:{summary.start_line}-{summary.end_line}) "
                f"sim={summary.similarity:.3f}"
            )
            if summary.signature:
                lines.append(f"    签名: {summary.signature}")
            if summary.short_summary:
                lines.append(f"    摘要: {summary.short_summary}")
            if summary.why_relevant:
                lines.append(f"    相关性: {summary.why_relevant}")

            if summary.high_confidence_calls:
                lines.append(f"    高置信调用（最多 {per_method_call_evidence_limit} 条）:")
                for call in summary.high_confidence_calls[:per_method_call_evidence_limit]:
                    lines.append(f"      - {call}")

        if len(result.method_summaries) > summary_limit:
            lines.append(
                f"    ... 另有 {len(result.method_summaries) - summary_limit} "
                f"个 issue-relevant method 未显示"
            )
    else:
        if deepen_issue_query:
            lines.append("")
            lines.append(
                "Issue 相关方法摘要: <empty>。"
                "可能原因：该文件没有 METHOD 节点、embedding_backend 不可用、"
                "或 method embedding 排序失败。"
            )

    lines.append("")
    lines.append(
        f"深化预算: 已使用 {len(graph.get_deepened_files())}/{MAX_DEEPEN_FILES}，剩余 {remaining}"
    )

    return "\n".join(lines)


# ===========================================================================
# 统一 dispatch 入口
# ===========================================================================

TOOL_FUNCTIONS = {
    "search_structural": search_structural,
    "search_semantic": search_semantic,
    "search_bm25": search_bm25,
    "search_hybrid": search_hybrid,
    "deepen_file": deepen_file,
}


def dispatch(tool_name: str, args: dict) -> str:
    fn = TOOL_FUNCTIONS.get(tool_name)
    if fn is None:
        raise ValueError(
            f"未知工具名: {tool_name}，可用工具: {list(TOOL_FUNCTIONS)}"
        )

    try:
        return fn(**args)
    except TypeError as e:
        logger.exception("工具参数错误: %s(%s)", tool_name, args)
        raise TypeError(
            f"{tool_name} 参数错误: {e}; 收到参数: {args}"
        ) from e

# ===========================================================================
# Tool Schema
# ===========================================================================
RETRIEVAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_structural",
            "description": (
                "【粗粒度结构关系检索】给定一个已知 node_id，在代码图中查找结构上相关的节点。\n"
                "这个工具不是自然语言搜索工具；必须使用 search_hybrid/search_semantic 等结果中的 node_id 作为输入。\n"
                "支持关系模式：\n"
                "- siblings: 同类方法、同模块类、同包文件中的兄弟节点\n"
                "- inheritance: 父类、子类、共父兄弟类、父类同名方法\n"
                "- dependencies: 文件级导入/被导入关系，以及已知调用/被调用关系\n"
                "- co_dependents: 与当前文件具有相似导入模式的文件代表节点\n"
                "- related: 综合以上所有关系\n"
                "适用场景：已定位一个相关类/函数/文件节点后，扩展上下文、寻找相关实现、父子类、兄弟类或依赖文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": (
                            "已知节点 ID。必须来自 search_hybrid/search_semantic/search_structural 的结果，"
                            "例如结果中的“节点ID”字段。不要传自然语言 query。"
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": [
                            "siblings",
                            "inheritance",
                            "dependencies",
                            "co_dependents",
                            "related",
                        ],
                        "description": (
                            "结构关系模式。默认 related。"
                            "siblings=兄弟节点；inheritance=继承关系；dependencies=依赖关系；"
                            "co_dependents=相似导入模式；related=综合关联。"
                        ),
                        "default": "related",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回数量，默认 5",
                        "default": 5,
                    },
                },
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_bm25",
            "description": (
                "【BM25 词法/符号检索】根据文件名、类名、函数名、方法名、参数名、报错词、"
                "issue_focus/query_focus 抽取出的 exact symbols 和 behavior/error terms 做词法召回。\n"
                "适用场景：issue 中出现明确代码符号、文件路径、参数名、错误消息，"
                "或 search_hybrid 的语义结果过泛时，用它补充精确符号召回。\n"
                "注意：这是词法/符号检索，不是语义理解；如果 query 是纯自然语言行为描述，"
                "通常先用 search_hybrid 或 search_semantic。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "BM25 查询。优先使用文件名、类名、函数名、方法名、参数名、错误词，"
                            "也可以传当前 bug 行为描述，系统会结合 issue_focus/query_focus 生成多路短查询。"
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回数量，默认 10",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_semantic",
            "description": (
                "【纯语义检索】只根据 query embedding 与节点 embedding 的相似度查找类、函数或方法节点。\n"
                "适用场景：query 是自然语言行为描述、症状描述，或你希望绕开 BM25/结构扩展看纯语义近邻。\n"
                "限制：不使用 issue_focus 多路 BM25，不主动扩展调用/继承/导入关系；"
                "如果需要综合定位，优先用 search_hybrid。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言语义查询，例如目标行为、bug 症状、错误现象。",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回数量，默认 5",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hybrid",
            "description": (
                "【综合检索（推荐首选）】根据自然语言 query 定位相关代码节点。\n"
                "内部融合三类信号：\n"
                "1. semantic：根据节点 embedding 匹配行为/语义描述；\n"
                "2. BM25：根据 issue_focus/query_focus/current query 做文件名、符号名、参数名、错误词召回；\n"
                "3. structural：围绕 semantic/BM25 的高置信 seed 做图关系扩展。\n"
                "返回节点ID、文件路径、行号、综合分、语义原因、BM25 命中和结构关系依据。\n"
                "推荐作为第一步定位入口。注意：初始图是骨架级；需要方法体、调用边或完整源码时，继续调用 deepen_file。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言检索查询，例如 bug 症状、目标行为、报错信息、相关符号或参数名。",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回数量，默认 5",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deepen_file",
            "description": (
                "【文件深化】对指定文件进行完整 AST 解析，把骨架图中的该文件升级为完整解析状态。\n"
                "初始代码图主要包含 MODULE、CLASS、顶层 FUNCTION、签名、docstring、导入关系和继承关系；"
                "deepen_file 会：\n"
                "- 更新已有 CLASS/FUNCTION 的完整 code_text/signature/docstring；\n"
                "- 新增或更新 METHOD 节点；\n"
                "- 补充 PARENT_CHILD、SIBLING、CALLS、OVERRIDES 等关系；\n"
                "- 统一刷新 semantic、BM25、structural 三路索引；\n"
                "- 基于 issue_query 或 issue_focus/query_focus 返回 issue-related method summary 和高置信调用证据。\n\n"
                "使用时机：search_hybrid/search_bm25 找到有希望的源码文件后，"
                "如果需要方法级细节、调用关系、重写关系或更准确的结构上下文，就深化该文件。"
                "每个任务最多深化 20 个文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": (
                            "要深化的文件相对路径，如 'requests/models.py' 或 'astropy/modeling/separable.py'。"
                            "可以从 search 结果的“文件”字段获取。如果结果中包含 node_id，"
                            "请只传文件路径部分。"
                        ),
                    },
                    "issue_query": {
                        "type": "string",
                        "description": (
                            "可选。当前 issue、当前检索 query 或与该文件相关的行为描述。"
                            "用于深化后生成 issue-related method summary。"
                            "如果不传，系统会从 issue_focus/query_focus/SWE_ISSUE_TEXT 自动补全。"
                        ),
                    },
                    "top_methods": {
                        "type": "integer",
                        "description": (
                            "可选。返回 issue-related method summary 的种子方法数量，默认 5。"
                            "值越大，method summary 覆盖越多，但输出也会更长。"
                        ),
                        "default": 5,
                    },
                },
                "required": ["file_path"],
            },
        },
    },
]

