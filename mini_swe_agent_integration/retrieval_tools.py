"""
retrieval_tools.py — 检索工具实现与 Tool Schema 定义
=====================================================

职责
----
1. 定义三个检索工具的 OpenAI function calling JSON schema
   （LLM 通过这些描述决定何时调用哪个工具）
2. 实现三个工具的 Python 函数，从磁盘缓存加载索引并执行检索
3. 提供统一的 dispatch 入口，供 RetrievalAgent 调用

缓存目录结构（由 prebuild.py 生成）
------------------------------------
<cache_dir>/
  code_graph.pkl            ← CodeGraph 对象
  feature_matrix.npy        ← 结构特征矩阵（n_nodes × 8）
  feature_node_ids.json     ← 结构特征矩阵的行对应 node_id 列表
  semantic_embeddings.npy   ← embedding 向量矩阵（n_nodes × dim）
  semantic_node_ids.json    ← embedding 矩阵的行对应 node_id 列表
  semantic_texts.json       ← 各节点用于 embedding 的文本（调试用）

设计说明
--------
- 工具函数每次调用时从磁盘加载缓存，利用 Python 进程缓存（模块级变量）
  避免重复 IO。对 mini-swe-agent 的单进程模型，这是最简单可靠的方案。
- 返回值是纯文本字符串，格式与 bash 命令输出完全一致，
  这样 format_observation_messages 可以直接处理，无需任何修改。
"""

from __future__ import annotations

import json
import logging
import os
import pickle
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ===========================================================================
# 模块级缓存（进程内复用，避免重复磁盘 IO）
# ===========================================================================

_cache: dict = {}   # key → 已加载的对象


def _get_cache_dir() -> str:
    return os.environ.get("CODE_GRAPH_CACHE_DIR", "/tmp/code_graph_cache")


def _load_cached(key: str, loader):
    """懒加载：首次调用时 load，后续直接返回内存缓存。"""
    if key not in _cache:
        _cache[key] = loader()
    return _cache[key]


def _load_graph():
    cache_dir = _get_cache_dir()
    path = os.path.join(cache_dir, "code_graph.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"代码图缓存不存在：{path}\n"
            f"请先运行：python mini_swe_agent_integration/prebuild.py --repo_path /repo"
        )
    with open(path, "rb") as f:
        return pickle.load(f)


def _load_structural_retriever():
    """从缓存加载结构检索器（跳过重新构建图，直接加载矩阵后 fit）。"""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from code_graph_retriever.feature_extractor import FeatureExtractor
    from code_graph_retriever.structural_retriever import StructuralRetriever

    cache_dir = _get_cache_dir()
    graph     = _load_cached("graph", _load_graph)

    matrix_path   = os.path.join(cache_dir, "feature_matrix.npy")
    node_ids_path = os.path.join(cache_dir, "feature_node_ids.json")

    if os.path.exists(matrix_path) and os.path.exists(node_ids_path):
        # 从缓存加载矩阵，快速重建索引
        matrix   = np.load(matrix_path)
        node_ids = json.loads(open(node_ids_path).read())

        extractor = FeatureExtractor(graph)
        extractor.build()   # 重建 position 信息（从图计算，很快）

        retriever = StructuralRetriever(graph, extractor=extractor)
        # 直接注入缓存的矩阵，跳过重新计算特征
        retriever._node_ids = node_ids
        retriever._matrix   = matrix
        retriever._extractor = extractor

        from sklearn.neighbors import NearestNeighbors
        retriever._nn = NearestNeighbors(metric="cosine", algorithm="brute")
        retriever._nn.fit(matrix)
        retriever._built = True
    else:
        # 缓存不完整，重新构建（较慢）
        logger.warning("结构特征缓存不完整，重新构建（这会较慢）")
        extractor = FeatureExtractor(graph).build()
        retriever = StructuralRetriever(graph, extractor=extractor).build()

    return retriever


def _load_semantic_retriever():
    """从缓存加载语义检索器（跳过重新调用 embedding API）。"""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from code_graph_retriever.semantic_retriever import (
        SemanticRetriever, TFIDFEmbeddingBackend, get_default_embedding_backend
    )

    cache_dir = _get_cache_dir()
    graph     = _load_cached("graph", _load_graph)

    emb_path      = os.path.join(cache_dir, "semantic_embeddings.npy")
    node_ids_path = os.path.join(cache_dir, "semantic_node_ids.json")
    texts_path    = os.path.join(cache_dir, "semantic_texts.json")

    if os.path.exists(emb_path) and os.path.exists(node_ids_path):
        matrix   = np.load(emb_path)
        node_ids = json.loads(open(node_ids_path).read())
        texts    = json.loads(open(texts_path).read()) if os.path.exists(texts_path) else [""] * len(node_ids)

        # 选择 embedding 后端（用于对 query 文本做 embedding）
        dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if dashscope_key:
            from code_graph_retriever.semantic_retriever import DashScopeEmbeddingBackend
            backend = DashScopeEmbeddingBackend(api_key=dashscope_key)
            logger.info("语义检索：使用 DashScope embedding 后端")
        else:
            # TF-IDF 降级：需要用缓存的文本重新 fit
            backend = TFIDFEmbeddingBackend(n_components=min(64, max(1, len(texts) - 1)))
            if texts and any(t for t in texts):
                backend.fit(texts)
            logger.warning("语义检索：未找到 DASHSCOPE_API_KEY，使用 TF-IDF 后端（语义质量较低）")

        retriever = SemanticRetriever(graph, backend=backend)
        retriever._node_ids = node_ids
        retriever._texts    = texts
        retriever._matrix   = matrix

        from sklearn.neighbors import NearestNeighbors
        retriever._nn = NearestNeighbors(metric="cosine", algorithm="brute")
        retriever._nn.fit(matrix)
        retriever._built = True
    else:
        # 缓存不完整，重新构建（会重新调用 API，较慢）
        logger.warning("语义 embedding 缓存不完整，重新构建（将调用 embedding API）")
        backend   = get_default_embedding_backend()
        retriever = SemanticRetriever(graph, backend=backend).build()

    return retriever


# ===========================================================================
# 三个工具函数
# ===========================================================================

def search_structural(node_id: str, top_k: int = 5) -> str:
    """
    基于代码图结构特征检索与指定节点角色相似的函数/类。

    参数：node_id（如 src/models.py::User.save），top_k（返回结果数）
    返回：格式化的检索结果文本
    """
    try:
        retriever = _load_cached("structural_retriever", _load_structural_retriever)
        response  = retriever.search_by_node_id(node_id, top_k=top_k)

        if not response.results:
            return (f"[structural_search] 未找到与 '{node_id}' 结构相似的节点。\n"
                    f"（共检索 {response.total_nodes} 个节点，耗时 {response.elapsed_ms:.1f}ms）")

        lines = [
            f"[structural_search] 找到 {len(response.results)} 个结构相似节点",
            f"查询节点: {node_id}",
            f"检索范围: {response.total_nodes} 个节点  耗时: {response.elapsed_ms:.1f}ms",
            "=" * 60,
        ]
        for i, r in enumerate(response.results, 1):
            lines += [
                f"\n[{i}] {r.qualified_name}  [{r.node_type}]",
                f"    文件: {r.file}  行: {r.start_line}~{r.end_line}",
                f"    结构评分: {r.structural_score:.3f}",
                f"    结构匹配依据: {r.structural_reason}",
                f"    结构位置: {r.position_summary}",
            ]
            if r.comment:
                lines.append(f"    功能注释: {r.comment[:120]}")
        return "\n".join(lines)

    except FileNotFoundError as e:
        return f"[structural_search ERROR] {e}"
    except Exception as e:
        logger.exception("structural_search 失败")
        return f"[structural_search ERROR] {type(e).__name__}: {e}"


def search_semantic(query: str, top_k: int = 5) -> str:
    """
    基于自然语言语义检索功能相关的函数/类。

    参数：query（自然语言描述），top_k（返回结果数）
    返回：格式化的检索结果文本
    """
    try:
        retriever = _load_cached("semantic_retriever", _load_semantic_retriever)
        response  = retriever.search(query, top_k=top_k)

        if not response.results:
            return (f"[semantic_search] 未找到与 '{query}' 语义相关的节点。\n"
                    f"（共检索 {response.total_nodes} 个节点，耗时 {response.elapsed_ms:.1f}ms）")

        lines = [
            f"[semantic_search] 找到 {len(response.results)} 个语义相关节点",
            f"查询: {query}",
            f"检索范围: {response.total_nodes} 个节点  耗时: {response.elapsed_ms:.1f}ms",
            "=" * 60,
        ]
        for i, r in enumerate(response.results, 1):
            lines += [
                f"\n[{i}] {r.qualified_name}  [{r.node_type}]",
                f"    文件: {r.file}  行: {r.start_line}~{r.end_line}",
                f"    语义评分: {r.semantic_score:.3f}",
                f"    语义关联: {r.semantic_reason}",
            ]
            if r.comment:
                lines.append(f"    功能注释: {r.comment[:120]}")
        return "\n".join(lines)

    except FileNotFoundError as e:
        return f"[semantic_search ERROR] {e}"
    except Exception as e:
        logger.exception("semantic_search 失败")
        return f"[semantic_search ERROR] {type(e).__name__}: {e}"


def search_hybrid(query: str, top_k: int = 5) -> str:
    """
    结合结构特征和语义相似度的混合检索（推荐优先使用）。

    参数：query（自然语言描述或 issue 关键词），top_k（返回结果数）
    返回：格式化的检索结果文本（含结构和语义双重原因分析）
    """
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        graph             = _load_cached("graph", _load_graph)
        struct_retriever  = _load_cached("structural_retriever", _load_structural_retriever)
        sem_retriever     = _load_cached("semantic_retriever", _load_semantic_retriever)

        from code_graph_retriever.hybrid_retriever import HybridRetriever
        retriever = HybridRetriever(graph)
        retriever._structural = struct_retriever
        retriever._semantic   = sem_retriever
        retriever._built      = True

        response = retriever.search(query, top_k=top_k)

        if not response.results:
            return (f"[hybrid_search] 未找到与 '{query}' 相关的节点。\n"
                    f"（共检索 {response.total_nodes} 个节点，耗时 {response.elapsed_ms:.1f}ms）")

        lines = [
            f"[hybrid_search] 找到 {len(response.results)} 个相关节点",
            f"查询: {query}",
            f"检索范围: {response.total_nodes} 个节点  耗时: {response.elapsed_ms:.1f}ms",
            f"融合权重: 结构 40% + 语义 60%",
            "=" * 60,
        ]
        for i, r in enumerate(response.results, 1):
            lines += [
                f"\n[{i}] {r.qualified_name}  [{r.node_type}]",
                f"    文件: {r.file}  行: {r.start_line}~{r.end_line}",
                f"    综合评分: {r.final_score:.3f}  "
                f"（结构: {r.structural_score:.3f} | 语义: {r.semantic_score:.3f}）",
            ]
            if r.structural_reason:
                lines.append(f"    结构匹配依据: {r.structural_reason}")
            if r.semantic_reason:
                lines.append(f"    语义关联说明: {r.semantic_reason}")
            if r.position_summary:
                lines.append(f"    结构位置: {r.position_summary}")
            if r.comment:
                lines.append(f"    功能注释: {r.comment[:120]}")
        return "\n".join(lines)

    except FileNotFoundError as e:
        return f"[hybrid_search ERROR] {e}"
    except Exception as e:
        logger.exception("hybrid_search 失败")
        return f"[hybrid_search ERROR] {type(e).__name__}: {e}"


# ===========================================================================
# 统一 dispatch 入口
# ===========================================================================

TOOL_FUNCTIONS = {
    "search_structural": search_structural,
    "search_semantic":   search_semantic,
    "search_hybrid":     search_hybrid,
}


def dispatch(tool_name: str, args: dict) -> str:
    """根据工具名和参数分发到对应的检索函数，返回文本输出。"""
    fn = TOOL_FUNCTIONS.get(tool_name)
    if fn is None:
        return f"[ERROR] 未知工具名: {tool_name}，可用工具: {list(TOOL_FUNCTIONS)}"
    return fn(**args)


# ===========================================================================
# Tool Schema（OpenAI function calling 格式）
# ===========================================================================

RETRIEVAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_structural",
            "description": (
                "【结构检索】在代码仓库的调用图中，找到与指定节点扮演相同结构角色的函数/类。\n"
                "适用场景：\n"
                "- 你已定位到某个函数（如 parse_url），想找到仓库中扮演类似角色的其他函数\n"
                "- 寻找具有相同调用模式（高扇入/高扇出）的函数，评估修改影响范围\n"
                "- 通过结构相似性找到可能存在相同 bug 的其他函数\n"
                "注意：此工具关注的是代码图中的拓扑角色，而非功能语义。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": (
                            "目标节点的完整 ID，格式为 'relative/path/to/file.py::ClassName.method_name' "
                            "或 'relative/path/to/file.py::function_name'。"
                            "例如：'requests/models.py::Response.json' 或 'requests/utils.py::parse_url'"
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回最相似的节点数量，默认 5，最大建议 10",
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
                "【语义检索】根据自然语言描述，找到功能语义最匹配的函数/类。\n"
                "适用场景：\n"
                "- 知道 bug 的症状描述，想找到可能相关的函数（如：'HTTP 响应解码'）\n"
                "- 寻找某类功能的实现（如：'URL 参数编码'）\n"
                "- 根据 issue 描述直接检索可能的 bug 位置\n"
                "注意：此工具基于函数注释和功能描述做语义匹配，"
                "建议在有 LLM 生成注释（comment 字段非空）的仓库上使用效果最好。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "自然语言检索查询，可以是：\n"
                            "- issue 描述的关键部分，如 'UnicodeDecodeError HTTP response charset'\n"
                            "- 功能描述，如 '解码 HTTP 响应体'\n"
                            "- 错误关键词，如 'Content-Type without charset'"
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回最相关的节点数量，默认 5",
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
                "【混合检索（推荐）】综合代码图结构分析和语义相似度，找到最相关的函数/类。\n"
                "相比单独的结构或语义检索，混合检索覆盖更全面：\n"
                "- 结构分析找到'扮演相同角色'的节点（可能描述不同但功能相似）\n"
                "- 语义匹配找到'描述相关'的节点（可能结构不同但功能相关）\n"
                "- 每个结果附带结构匹配原因、语义关联说明、结构位置摘要\n\n"
                "适用场景（建议优先使用此工具）：\n"
                "- 分析 issue 时的初步探索：搜索 issue 关键词\n"
                "- 不确定 bug 在哪里时：搜索错误描述\n"
                "- 寻找修复参考时：搜索相关功能描述\n\n"
                "结果中的'结构位置'字段告诉你该函数被谁调用、依赖谁，修改它影响哪些路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "检索查询，可以是自然语言描述、issue 关键词或错误信息。\n"
                            "示例：\n"
                            "- 'HTTP response decoding UnicodeDecodeError Content-Type charset'\n"
                            "- '处理重定向时出现无限循环'\n"
                            "- 'SSL certificate verification'"
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回结果数量，默认 5，建议不超过 8（避免 context 过长）",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
]