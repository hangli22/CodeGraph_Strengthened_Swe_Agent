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
    struct_ret = _load_cached("structural_retriever", _load_structural_retriever)
    sem_ret = _load_cached("semantic_retriever", _load_semantic_retriever)
    bm25_ret = _load_cached("bm25_retriever", _load_bm25_retriever)

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

    # 关键修改：
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

    # 更新语义索引：为新节点计算 embedding
    if result.new_node_ids and sem_ret.backend is not None:
        new_texts = []
        for nid in result.new_node_ids:
            node = graph.get_node(nid)
            if node:
                comment = getattr(node, "comment", "") or ""
                signature = getattr(node, "signature", "") or ""
                docstring = getattr(node, "docstring", "") or ""
                code_text = getattr(node, "code_text", "") or ""

                if comment.strip():
                    text = comment.strip()
                elif signature or docstring:
                    text = node.skeleton_embedding_text()
                else:
                    text = code_text[:500].strip()
                new_texts.append(text or nid)
            else:
                new_texts.append(nid)

        try:
            new_embeddings = sem_ret.backend.embed_batch(new_texts)
            sem_ret.add_nodes(result.new_node_ids, new_texts, new_embeddings)
        except Exception as e:
            logger.warning(
                "新节点 embedding 失败: %s（检索仍可用，但新节点暂无语义索引）",
                e,
            )

    # 更新结构索引：
    # 当前结构检索是粗粒度关系检索，不依赖旧 feature_matrix。
    # deepen 后可能新增方法节点和 CALLS 边，因此 rebuild 可刷新结构索引。
    if hasattr(struct_ret, "rebuild"):
        struct_ret.rebuild()

    # 更新 BM25 索引：
    # - 如果 deepen 只是新增节点，可以 add_nodes(result.new_node_ids)。
    # - 如果 deepen 会修改已有节点文本/comment/method_names/code_text，则必须 rebuild。
    # 当前 FileDeepener 会补充已有 CLASS/FUNCTION 节点 code_text/signature/docstring，
    # 因此默认保守 rebuild，保证 BM25 不读旧文本。
    try:
        bm25_text_changed = bool(getattr(result, "text_changed", True))
        if bm25_text_changed and hasattr(bm25_ret, "rebuild"):
            bm25_ret.rebuild()
        elif result.new_node_ids and hasattr(bm25_ret, "add_nodes"):
            bm25_ret.add_nodes(result.new_node_ids)
    except Exception as e:
        logger.warning("BM25 索引更新失败: %s（检索仍可用，但 BM25 可能未包含深化内容）", e)

    # 生成汇总
    remaining = MAX_DEEPEN_FILES - len(graph.get_deepened_files())

    lines = [
        f"[deepen_file] 文件 {file_rel} 已深化为完整解析",
        "",
    ]

    if deepen_issue_query:
        preview = deepen_issue_query.replace("\n", " ")
        if len(preview) > 240:
            preview = preview[:240].rstrip() + "..."
        lines.append(f"Issue/query context: {preview}")
    else:
        lines.append("Issue/query context: <empty>，未生成 issue-related method summary")

    lines += [
        "",
        f"新增 {result.method_count} 个方法节点:",
    ]

    for nid in result.new_node_ids[:10]:
        node = graph.get_node(nid)
        if node:
            sig = getattr(node, "signature", "") or ""
            docstring = getattr(node, "docstring", "") or ""
            doc = f" — {docstring}" if docstring else ""
            lines.append(f"  {node.name}{sig}{doc}")

    if len(result.new_node_ids) > 10:
        lines.append(f"  ... 共 {len(result.new_node_ids)} 个")

    lines.append("")
    lines.append(f"新增 {result.new_edge_count} 条关系边 (CALLS: {result.call_edge_count})")

    if result.imported_files:
        lines.append("")
        lines.append("关联文件（可进一步深化）:")
        for imp_file in result.imported_files[:8]:
            lines.append(f"  - {imp_file}")

    if result.neighbor_deepened_files:
        lines.append("")
        lines.append("因相邻类额外深化的文件:")
        for nf in result.neighbor_deepened_files[:8]:
            lines.append(f"  - {nf}")

    if result.method_summaries:
        lines.append("")
        lines.append("Issue 相关方法摘要:")
        for i, summary in enumerate(result.method_summaries[:12], 1):
            lines.append(
                f"[{i}] {summary.qualified_name} "
                f"({summary.file}:{summary.start_line}-{summary.end_line}) "
                f"sim={summary.similarity:.3f}"
            )
            if summary.short_summary:
                lines.append(f"    摘要: {summary.short_summary}")
            if summary.why_relevant:
                lines.append(f"    相关性: {summary.why_relevant}")
            if summary.high_confidence_calls:
                lines.append("    高置信调用:")
                for call in summary.high_confidence_calls[:3]:
                    lines.append(f"      - {call}")
            if summary.has_full_preview and summary.code_preview:
                lines.append("    code_preview:")
                lines.append(summary.code_preview)
    else:
        if deepen_issue_query:
            lines.append("")
            lines.append(
                "Issue 相关方法摘要: <empty>。"
                "可能原因：该文件没有 METHOD 节点、embedding_backend 不可用、"
                "或 method embedding 排序失败。"
            )

    if result.relation_summary:
        lines.append("")
        lines.append("局部关系提示:")
        for item in result.relation_summary[:5]:
            lines.append(f"  - {item}")

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
            "name": "search_semantic",
            "description": (
                "【语义检索】根据自然语言描述，找到功能语义最匹配的函数、类或文件节点。\n"
                "适用场景：知道 bug 症状、报错信息、参数名或行为描述，但不知道具体实现位置时。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言检索查询，例如 bug 症状、参数名、错误信息或目标行为。",
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
                "【混合检索（推荐首选）】根据自然语言 query 找到最相关的函数、类或文件节点。\n"
                "当前推荐作为定位入口：先用 search_hybrid 找 seed nodes，再用 search_structural 对已知 node_id 做关系扩展。\n"
                "每个结果会尽量包含节点ID、文件路径、行号、结构关系依据、语义关联说明和 BM25 词法命中信息。\n"
                "注意：初始状态为骨架图；如果需要方法级细节或调用边，请用 deepen_file 深化相关文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言检索查询。",
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
                "【文件深化】对指定文件进行完整 AST 解析，补充方法级节点和函数调用关系。\n"
                "初始代码图是骨架级，主要包含模块、类名、函数签名、导入关系和继承关系；"
                "当你需要查看方法级细节、调用边或完整源码片段时，应深化相关文件。\n"
                "深化后：\n"
                "- 创建方法节点和更细粒度函数节点\n"
                "- 分析函数调用关系\n"
                "- 自动更新语义索引\n"
                "- 自动更新 BM25 词法索引\n"
                "- 刷新结构关系索引\n"
                "- 返回新增方法列表和可进一步深化的关联文件\n"
                "- 如果存在 issue_query 或 issue_focus/query_focus，可返回 issue-related method summary\n\n"
                "每次任务最多深化 20 个文件。建议只深化与 issue 最相关的文件。\n"
                "推荐工作流：search_hybrid → deepen_file → bash 读源码 → search_structural 扩展相关节点。"
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

