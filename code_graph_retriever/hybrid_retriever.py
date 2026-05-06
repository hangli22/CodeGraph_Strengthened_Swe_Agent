"""
hybrid_retriever.py — 结构 + 语义融合检索（适配方向三）

结构检索现在基于图关系查询而非向量相似度，
融合逻辑相应调整：结构结果提供关系型信号，语义结果提供内容匹配信号。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from code_graph_builder.graph_schema import CodeGraph
from .structural_retriever import StructuralRetriever, StructuralQueryMode
from .semantic_retriever import SemanticRetriever, EmbeddingBackend
from .retrieval_result import RetrievalResult, RetrievalResponse


class HybridRetriever:
    def __init__(self, graph: CodeGraph, alpha: float = 0.4, beta: float = 0.6,
                 embedding_backend: Optional[EmbeddingBackend] = None):
        assert abs(alpha + beta - 1.0) < 1e-6
        self.graph = graph
        self.alpha = alpha
        self.beta  = beta
        self._structural = StructuralRetriever(graph)
        self._semantic   = SemanticRetriever(graph, backend=embedding_backend)
        self._built = False

    def build(self) -> "HybridRetriever":
        self._structural.build()
        self._semantic.build()
        self._built = True
        return self

    def rebuild_after_deepen(self) -> "HybridRetriever":
        """深化后重建结构索引（语义索引通过 add_nodes 增量更新）。"""
        self._structural.rebuild()
        return self

    def search(self, query: str, top_k: int = 5) -> RetrievalResponse:
        """
        混合检索：先语义检索获取候选，再用结构关系增强排序。
        """
        self._ensure_built()
        t0 = time.perf_counter()
        candidate_k = top_k * 3

        # 语义检索：找到内容相关的候选
        sem_resp = self._semantic.search(query, top_k=candidate_k)

        # 结构增强：对语义最优结果查找结构关联节点
        struct_results: List[RetrievalResult] = []
        if sem_resp.results and self.alpha > 0:
            best_nid = sem_resp.results[0].node_id
            struct_resp = self._structural.search_by_node_id(
                best_nid, top_k=candidate_k, exclude_self=False)
            struct_results = struct_resp.results

        merged = self._merge(struct_results, sem_resp.results, top_k=top_k)
        return RetrievalResponse(
            query=query, results=merged,
            total_nodes=sem_resp.total_nodes,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    def search_by_node(self, node_id: str, top_k: int = 5,
                       mode: StructuralQueryMode = StructuralQueryMode.RELATED
                       ) -> RetrievalResponse:
        """
        以节点为起点的混合检索。
        结构侧使用指定的查询模式，语义侧使用节点注释/代码作为 query。
        """
        self._ensure_built()
        t0 = time.perf_counter()
        candidate_k = top_k * 3

        node = self.graph.get_node(node_id)
        query_text = node.qualified_name if node else node_id

        # 结构检索
        struct_resp = self._structural.search(node_id, mode=mode, top_k=candidate_k)
        struct_results = [r for r in struct_resp.results if r.node_id != node_id]

        # 语义检索
        sem_query = ""
        if node:
            sem_query = node.comment.strip() if node.comment and node.comment.strip() \
                else (node.code_text[:300].strip() if node.code_text else "")
        sem_resp = self._semantic.search(sem_query, top_k=candidate_k) \
            if sem_query else RetrievalResponse(query=query_text)
        sem_results = [r for r in sem_resp.results if r.node_id != node_id]

        merged = self._merge(struct_results, sem_results, top_k=top_k)
        return RetrievalResponse(
            query=query_text, results=merged,
            total_nodes=struct_resp.total_nodes,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()

    def _merge(self, struct_results: List[RetrievalResult],
               sem_results: List[RetrievalResult], top_k: int) -> List[RetrievalResult]:
        """融合结构和语义结果，加权计算最终分数。"""
        by_id: Dict[str, RetrievalResult] = {}

        for r in struct_results:
            by_id[r.node_id] = r

        for r in sem_results:
            if r.node_id in by_id:
                existing = by_id[r.node_id]
                existing.semantic_score = r.semantic_score
                existing.semantic_reason = r.semantic_reason
            else:
                by_id[r.node_id] = r

        for r in by_id.values():
            r.final_score = self.alpha * r.structural_score + self.beta * r.semantic_score

        ranked = sorted(by_id.values(), key=lambda x: x.final_score, reverse=True)
        return ranked[:top_k]