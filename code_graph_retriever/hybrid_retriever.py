"""
hybrid_retriever.py — 结构 + 语义 + BM25 融合检索（适配方向三）

结构检索现在基于图关系查询而非向量相似度：
  - BM25：提供词法/符号召回候选与 bm25_score
  - structural：围绕高置信候选做图关系扩展，提供 structural_score

推荐调用方式：
  - retrieval_tools.py 负责读取 issue_focus.json 并组装 bm25_queries / bm25_query_groups
  - HybridRetriever.search(query, bm25_queries=..., bm25_query_groups=...) 只负责融合检索


最大问题：HybridRetriever.rebuild_after_deepen() 没有更新 semantic 索引


SemanticRetriever.build() 可能重复收集节点

SemanticRetriever.build() 里直接调用 _collect_nodes()，但从你给出的内容看，build 前没有清空：

self._collect_nodes()

而 _collect_nodes() 是 append 到：

self._node_ids
self._texts

如果 build() 被调用多次，可能会重复加入同一批节点。

这对未来非常危险，因为如果你按我上面建议在 deepen 后重建 semantic，可能导致：

_node_ids 重复；
embedding matrix 重复；
nearest neighbor 中重复结果；
检索分数和 top_k 异常。

**建议：**给 SemanticRetriever 加：

def rebuild(self):
    self._node_ids = []
    self._texts = []
    self._matrix = None
    self._nn = None
    self._built = False
    return self.build()

并且 build() 开头也最好清空一次，避免误用。


HybridRetriever.search_by_node() 对骨架节点不友好

search_by_node() 中 semantic/BM25 的 query 文本只用：

node.comment
else node.code_text[:300]

但是骨架图中 CLASS/FUNCTION 的有效文本很多在：

signature
docstring
method_names
skeleton_embedding_text()

如果 comment 为空，code_text 也可能只是很短的骨架摘要或为空，那么 search_by_node() 的语义/BM25 辅助召回会偏弱。

**建议：**改成：

sem_query = node.comment.strip() or node.skeleton_embedding_text() or node.code_text[:300].strip()

这样和 SemanticRetriever._collect_nodes() 的文本构造逻辑一致。



hybrid_retrieval返回内容过多（一万多个字符），导致发散搜索



"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from code_graph_builder.graph_schema import CodeGraph
from .structural_retriever import StructuralRetriever, StructuralQueryMode
# from .semantic_retriever import SemanticRetriever, EmbeddingBackend
from .bm25_retriever import BM25Retriever
from .retrieval_result import RetrievalResult, RetrievalResponse


class HybridRetriever:
    def __init__(
        self,
        graph: CodeGraph,
        alpha: float = 0.35,
        beta: float = 0.0,
        bm25_weight: float = 0.65,
    ):
        """
        BM25 + structural 消融版 HybridRetriever。

        semantic 已禁用：
          - 不构造 SemanticRetriever
          - 不调用 semantic search
          - 不更新 semantic index
          - final_score 只由 structural_score + bm25_score 融合
        """
        if alpha < 0 or beta < 0 or bm25_weight < 0:
            raise ValueError("alpha / beta / bm25_weight 必须非负")
        if beta != 0:
            raise ValueError("semantic 消融模式下 beta 必须为 0")
        if alpha + bm25_weight <= 0:
            raise ValueError("alpha + bm25_weight 必须大于 0")

        self.graph = graph
        self.alpha = alpha
        self.beta = 0.0
        self.bm25_weight = bm25_weight

        self._structural = StructuralRetriever(graph)
        self._semantic = None
        self._bm25 = BM25Retriever(graph)

        self._built = False

    def build(self) -> "HybridRetriever":
        self._structural.build()
        self._bm25.build()
        self._built = True
        return self

    def rebuild_after_deepen(
        self,
        new_node_ids: Optional[List[str]] = None,
        updated_node_ids: Optional[List[str]] = None,
        text_changed: bool = True,
    ) -> "HybridRetriever":
        """
        deepen 后更新 BM25 + structural 索引。

        semantic 消融版不再维护 semantic index。
        """
        self._ensure_built()

        new_node_ids = new_node_ids or []

        # deepen 会新增/更新 PARENT_CHILD/SIBLING/CALLS/OVERRIDES 等边，结构索引必须重建。
        self._structural.rebuild()

        # BM25：已有节点文本变化时 rebuild；仅新增节点时可增量 add。
        if text_changed:
            self._bm25.rebuild()
        elif new_node_ids:
            self._bm25.add_nodes(new_node_ids)

        self._built = True
        return self

    def search(
        self,
        query: str,
        top_k: int = 5,
        bm25_queries: Optional[Sequence[str]] = None,
        bm25_query_groups: Optional[Dict[str, List[str]]] = None,
        bm25_group_weights: Optional[Dict[str, float]] = None,
        structural_seed_k: int = 3,
    ) -> RetrievalResponse:
        """
        BM25 + structural 混合检索：
        1. BM25 多路召回候选
        2. 从 BM25 候选中选 top seeds
        3. 围绕 seeds 做 structural expansion
        4. merge BM25 + structural
        """
        self._ensure_built()
        t0 = time.perf_counter()
        candidate_k = max(top_k * 3, top_k)

        # 1. BM25 检索：默认使用 current query；
        # 如果 retrieval_tools 传入 issue_focus/query_focus 组装后的 bm25_queries，则多路召回。
        bm25_results: List[RetrievalResult] = []
        lexical_queries = _dedup_clean(list(bm25_queries or []))
        if not lexical_queries and query.strip():
            lexical_queries = [query.strip()]

        if lexical_queries:
            if len(lexical_queries) == 1 and not bm25_query_groups:
                bm25_resp = self._bm25.search(
                    lexical_queries[0],
                    top_k=max(candidate_k * 2, 20),
                )
            else:
                bm25_resp = self._bm25.search_many(
                    lexical_queries,
                    top_k=max(candidate_k * 2, 20),
                    per_query_k=max(candidate_k * 2, 20),
                    query_groups=bm25_query_groups,
                    group_weights=bm25_group_weights,
                )
            bm25_results = bm25_resp.results

        # 2. 只从 BM25 候选中选 structural seeds。
        seed_ids = self._select_structural_seeds(
            sem_results=[],
            bm25_results=bm25_results,
            seed_k=max(1, structural_seed_k),
        )

        # 3. 结构扩展
        struct_results: List[RetrievalResult] = []
        seen_struct = set()
        if self.alpha > 0:
            for seed_id in seed_ids:
                struct_resp = self._structural.search_by_node_id(
                    seed_id,
                    top_k=candidate_k,
                    exclude_self=False,
                )
                for r in struct_resp.results:
                    if r.node_id in seen_struct:
                        continue
                    seen_struct.add(r.node_id)
                    struct_results.append(r)

        # 4. 融合：semantic 结果固定为空。
        merged = self._merge(
            struct_results=struct_results,
            sem_results=[],
            bm25_results=bm25_results,
            top_k=top_k,
        )

        return RetrievalResponse(
            query=query,
            results=merged,
            total_nodes=self._get_total_nodes(),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    def _get_total_nodes(self) -> int:
        try:
            return len(self.graph.nodes)
        except Exception:
            try:
                return int(self.graph.stats().get("total_nodes", 0))
            except Exception:
                return 0

    def _node_query_text(self, node) -> str:
        """
        为 search_by_node 构造语义/BM25 查询文本。

        不能只用 comment/code_text，因为骨架图中 CLASS/FUNCTION 的有效信息
        主要在 signature、docstring、method_names、skeleton_embedding_text。
        """
        if node is None:
            return ""

        parts: List[str] = []

        if getattr(node, "comment", "") and node.comment.strip():
            parts.append(node.comment.strip())

        try:
            skel = node.skeleton_embedding_text()
            if skel and skel.strip():
                parts.append(skel.strip())
        except Exception:
            pass

        if getattr(node, "signature", ""):
            parts.append(f"signature {node.signature}")

        if getattr(node, "docstring", ""):
            parts.append(f"docstring {node.docstring}")

        if getattr(node, "method_names", None):
            methods = ", ".join(str(x) for x in node.method_names if x)
            if methods:
                parts.append(f"methods {methods}")

        if getattr(node, "qualified_name", ""):
            parts.append(node.qualified_name)

        if getattr(node, "file", ""):
            parts.append(f"file {node.file}")

        if getattr(node, "code_text", ""):
            parts.append(node.code_text[:800].strip())

        return "\n".join(p for p in parts if p and str(p).strip()).strip()

    def search_by_node(
        self,
        node_id: str,
        top_k: int = 5,
        mode: StructuralQueryMode = StructuralQueryMode.RELATED,
    ) -> RetrievalResponse:
        """
        以节点为起点的 BM25 + structural 检索。
        """
        self._ensure_built()
        t0 = time.perf_counter()
        candidate_k = max(top_k * 3, top_k)

        node = self.graph.get_node(node_id)
        query_text = node.qualified_name if node else node_id

        # 1. 结构检索
        struct_resp = self._structural.search(node_id, mode=mode, top_k=candidate_k)
        struct_results = [r for r in struct_resp.results if r.node_id != node_id]

        # 2. BM25 query：仍可用节点骨架文本，不属于 semantic retriever
        bm25_query = self._node_query_text(node) if node else ""

        bm25_resp = (
            self._bm25.search(bm25_query, top_k=candidate_k)
            if bm25_query
            else RetrievalResponse(query=query_text)
        )
        bm25_results = [r for r in bm25_resp.results if r.node_id != node_id]

        merged = self._merge(
            struct_results=struct_results,
            sem_results=[],
            bm25_results=bm25_results,
            top_k=top_k,
        )

        return RetrievalResponse(
            query=query_text,
            results=merged,
            total_nodes=struct_resp.total_nodes,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()

    def _select_structural_seeds(
        self,
        sem_results: List[RetrievalResult],
        bm25_results: List[RetrievalResult],
        seed_k: int = 3,
    ) -> List[str]:
        """
        BM25 + structural 消融版 seed 选择。

        semantic 已禁用：
        - structural expansion seeds 只来自 BM25 候选；
        - 优先选择 BM25 分数高、且 METHOD/FUNCTION/CLASS 类型的节点。
        """
        del sem_results  # semantic ablation: intentionally unused

        ranked = sorted(
            bm25_results,
            key=lambda r: (
                _get_float_attr(r, "bm25_score", fallback=getattr(r, "final_score", 0.0)),
                self._seed_type_bonus(r),
            ),
            reverse=True,
        )

        seeds: List[str] = []
        for r in ranked:
            if r.node_id and r.node_id not in seeds:
                seeds.append(r.node_id)
            if len(seeds) >= seed_k:
                break

        return seeds

    @staticmethod
    def _seed_type_bonus(result: RetrievalResult) -> float:
        """
        METHOD / FUNCTION / CLASS 通常比 MODULE 更适合作为结构扩展入口。
        """
        node_type = str(getattr(result, "node_type", "") or "").lower()
        if node_type in {"method", "function"}:
            return 0.04
        if node_type == "class":
            return 0.03
        if node_type == "module":
            return 0.01
        return 0.0

    def _merge(
        self,
        struct_results: List[RetrievalResult],
        sem_results: List[RetrievalResult],
        bm25_results: List[RetrievalResult],
        top_k: int,
    ) -> List[RetrievalResult]:
        """
        融合结构、语义、BM25 结果，加权计算最终分数。
        """
        by_id: Dict[str, RetrievalResult] = {}

        # 结构结果
        for r in struct_results:
            by_id[r.node_id] = r

        # 语义结果
        for r in sem_results:
            if r.node_id in by_id:
                existing = by_id[r.node_id]
                existing.semantic_score = _get_float_attr(r, "semantic_score", fallback=getattr(r, "final_score", 0.0))
                existing.semantic_reason = getattr(r, "semantic_reason", "")
            else:
                # 确保 semantic result 至少有 structural_score/bm25_score 兜底
                _safe_setattr(r, "bm25_score", _get_float_attr(r, "bm25_score", 0.0))
                by_id[r.node_id] = r

        # BM25 结果
        for r in bm25_results:
            bm25_score = _get_float_attr(r, "bm25_score", fallback=getattr(r, "final_score", 0.0))
            if r.node_id in by_id:
                existing = by_id[r.node_id]
                _safe_setattr(existing, "bm25_score", bm25_score)
                _safe_setattr(existing, "bm25_reason", getattr(r, "bm25_reason", ""))
                _safe_setattr(existing, "bm25_hit_queries", getattr(r, "bm25_hit_queries", []))
                _safe_setattr(existing, "bm25_query_groups", getattr(r, "bm25_query_groups", {}))
            else:
                _safe_setattr(r, "bm25_score", bm25_score)
                # BM25 result 自身不应伪装成 semantic result
                r.semantic_score = _get_float_attr(r, "semantic_score", 0.0)
                by_id[r.node_id] = r

        for r in by_id.values():
            structural_score = _get_float_attr(r, "structural_score", 0.0)
            bm25_score = _get_float_attr(r, "bm25_score", 0.0)

            # BM25 同时命中给轻量 overlap bonus
            overlap_bonus = 0.0

            r.final_score = (
                self.alpha * structural_score
                + self.bm25_weight * bm25_score
                + overlap_bonus
            )

        ranked = sorted(by_id.values(), key=lambda x: x.final_score, reverse=True)
        return ranked[:top_k]


# ----------------------------------------------------------------------
# 小工具：保持与旧 RetrievalResult 兼容
# ----------------------------------------------------------------------

def _safe_setattr(obj: Any, name: str, value: Any) -> None:
    try:
        setattr(obj, name, value)
    except Exception:
        # 如果 RetrievalResult 使用 slots/frozen/pydantic 且不允许动态字段，
        # 需要在 retrieval_result.py 中显式新增 bm25_score/bm25_reason 等字段。
        pass


def _get_float_attr(obj: Any, name: str, fallback: float = 0.0) -> float:
    try:
        value = getattr(obj, name, fallback)
        if value is None:
            return float(fallback)
        return float(value)
    except Exception:
        return float(fallback)


def _dedup_clean(items: Sequence[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for x in items:
        if x is None:
            continue
        s = str(x).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(s)
    return result
