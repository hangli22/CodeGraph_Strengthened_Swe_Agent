"""
hybrid_retriever.py — 结构 + 语义 + BM25 融合检索（适配方向三）

结构检索现在基于图关系查询而非向量相似度：
  - semantic：提供语义相关候选与 semantic_score
  - BM25：提供词法/符号召回候选与 bm25_score
  - structural：围绕高置信候选做图关系扩展，提供 structural_score

推荐调用方式：
  - retrieval_tools.py 负责读取 issue_focus.json 并组装 bm25_queries / bm25_query_groups
  - HybridRetriever.search(query, bm25_queries=..., bm25_query_groups=...) 只负责融合检索
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from code_graph_builder.graph_schema import CodeGraph
from .structural_retriever import StructuralRetriever, StructuralQueryMode
from .semantic_retriever import SemanticRetriever, EmbeddingBackend
from .bm25_retriever import BM25Retriever
from .retrieval_result import RetrievalResult, RetrievalResponse


class HybridRetriever:
    def __init__(
        self,
        graph: CodeGraph,
        alpha: float = 0.25,
        beta: float = 0.55,
        bm25_weight: float = 0.20,
        embedding_backend: Optional[EmbeddingBackend] = None,
    ):
        """
        参数：
          alpha:
              structural_score 权重。
          beta:
              semantic_score 权重。
          bm25_weight:
              bm25_score 权重。BM25 更偏向召回，因此默认低于 semantic，
              但它会参与 seed 选择和最终排序。
        """
        if alpha < 0 or beta < 0 or bm25_weight < 0:
            raise ValueError("alpha / beta / bm25_weight 必须非负")
        if alpha + beta + bm25_weight <= 0:
            raise ValueError("alpha + beta + bm25_weight 必须大于 0")

        self.graph = graph
        self.alpha = alpha
        self.beta = beta
        self.bm25_weight = bm25_weight

        self._structural = StructuralRetriever(graph)
        self._semantic = SemanticRetriever(graph, backend=embedding_backend)
        self._bm25 = BM25Retriever(graph)

        self._built = False

    def build(self) -> "HybridRetriever":
        self._structural.build()
        self._semantic.build()
        self._bm25.build()
        self._built = True
        return self

    def rebuild_after_deepen(
        self,
        new_node_ids: Optional[List[str]] = None,
        text_changed: bool = True,
    ) -> "HybridRetriever":
        """
        deepen 后更新索引。

        参数：
          new_node_ids:
              deepen 后新增节点 ID。若只是新增节点，可用 BM25 add_nodes()。
          text_changed:
              如果 deepen 会修改已有节点文本、comment、method_names、code_text，
              必须重建 BM25。默认 True，优先保证正确性。

        结构索引始终 rebuild，因为 deepen 通常会新增/修改图关系。
        语义索引按你原有设计：若 semantic_retriever 支持增量 add_nodes，
        可在外部调用；否则建议也在对应流程中处理。
        """
        self._structural.rebuild()

        if text_changed:
            self._bm25.rebuild()
        elif new_node_ids:
            self._bm25.add_nodes(new_node_ids)

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
        混合检索：
          1. semantic search 拿一批候选
          2. BM25 多路 search 拿一批候选
          3. 合并 semantic + BM25 候选
          4. 从综合候选里选 top 1~3 个作为 structural expansion seed
          5. structural search_by_node_id(seed)
          6. merge 三路结果
        """
        self._ensure_built()
        t0 = time.perf_counter()
        candidate_k = max(top_k * 3, top_k)

        # 1. 语义检索：找到内容相关候选
        sem_resp = self._semantic.search(query, top_k=candidate_k)
        sem_results = sem_resp.results

        # 2. BM25 检索：默认使用 current query；如果 retrieval_tools 传入
        # issue_focus 组装后的 bm25_queries，则进行多路召回。
        bm25_results: List[RetrievalResult] = []
        lexical_queries = _dedup_clean(list(bm25_queries or []))
        if not lexical_queries and query.strip():
            lexical_queries = [query.strip()]

        if lexical_queries:
            if len(lexical_queries) == 1 and not bm25_query_groups:
                bm25_resp = self._bm25.search(lexical_queries[0], top_k=max(candidate_k * 2, 20))
            else:
                bm25_resp = self._bm25.search_many(
                    lexical_queries,
                    top_k=max(candidate_k * 2, 20),
                    per_query_k=max(candidate_k * 2, 20),
                    query_groups=bm25_query_groups,
                    group_weights=bm25_group_weights,
                )
            bm25_results = bm25_resp.results

        # 3-5. 从 semantic + BM25 综合候选中选 seed，并做结构扩展
        seed_ids = self._select_structural_seeds(
            sem_results=sem_results,
            bm25_results=bm25_results,
            seed_k=max(1, structural_seed_k),
        )

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

        # 6. 三路融合
        merged = self._merge(
            struct_results=struct_results,
            sem_results=sem_results,
            bm25_results=bm25_results,
            top_k=top_k,
        )

        return RetrievalResponse(
            query=query,
            results=merged,
            total_nodes=sem_resp.total_nodes,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    def search_by_node(
        self,
        node_id: str,
        top_k: int = 5,
        mode: StructuralQueryMode = StructuralQueryMode.RELATED,
    ) -> RetrievalResponse:
        """
        以节点为起点的混合检索。
        结构侧使用指定查询模式，语义/BM25 侧使用节点注释/代码作为 query。
        """
        self._ensure_built()
        t0 = time.perf_counter()
        candidate_k = max(top_k * 3, top_k)

        node = self.graph.get_node(node_id)
        query_text = node.qualified_name if node else node_id

        # 结构检索
        struct_resp = self._structural.search(node_id, mode=mode, top_k=candidate_k)
        struct_results = [r for r in struct_resp.results if r.node_id != node_id]

        # 语义/BM25 查询文本
        sem_query = ""
        if node:
            sem_query = (
                node.comment.strip()
                if node.comment and node.comment.strip()
                else (node.code_text[:300].strip() if node.code_text else "")
            )

        sem_resp = (
            self._semantic.search(sem_query, top_k=candidate_k)
            if sem_query
            else RetrievalResponse(query=query_text)
        )
        sem_results = [r for r in sem_resp.results if r.node_id != node_id]

        bm25_resp = (
            self._bm25.search(sem_query, top_k=candidate_k)
            if sem_query
            else RetrievalResponse(query=query_text)
        )
        bm25_results = [r for r in bm25_resp.results if r.node_id != node_id]

        merged = self._merge(
            struct_results=struct_results,
            sem_results=sem_results,
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
        从 semantic + BM25 综合候选中选结构扩展 seed。

        设计目标：
          - 不再只依赖 semantic top1
          - BM25 负责召回入口，特别是 exact symbol / file hint 命中
          - semantic 与 BM25 重合的候选优先
        """
        candidates: Dict[str, RetrievalResult] = {}
        preliminary_scores: Dict[str, float] = {}

        for rank, r in enumerate(sem_results):
            candidates.setdefault(r.node_id, r)
            semantic_score = _get_float_attr(r, "semantic_score", fallback=getattr(r, "final_score", 0.0))
            # 轻微 rank bonus，避免分数相同时 top 排名丢失
            rank_bonus = 0.02 / (rank + 1)
            preliminary_scores[r.node_id] = preliminary_scores.get(r.node_id, 0.0) + self.beta * semantic_score + rank_bonus

        for rank, r in enumerate(bm25_results):
            candidates.setdefault(r.node_id, r)
            bm25_score = _get_float_attr(r, "bm25_score", fallback=getattr(r, "final_score", 0.0))
            rank_bonus = 0.02 / (rank + 1)
            preliminary_scores[r.node_id] = preliminary_scores.get(r.node_id, 0.0) + self.bm25_weight * bm25_score + rank_bonus

            # semantic + BM25 重合的节点更适合作为结构扩展入口
            if any(sr.node_id == r.node_id for sr in sem_results):
                preliminary_scores[r.node_id] += 0.05

        ranked = sorted(
            candidates.values(),
            key=lambda r: (
                preliminary_scores.get(r.node_id, 0.0) + self._seed_type_bonus(r),
                _get_float_attr(r, "bm25_score", 0.0),
                _get_float_attr(r, "semantic_score", 0.0),
            ),
            reverse=True,
        )

        seeds: List[str] = []
        for r in ranked:
            if r.node_id not in seeds:
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
            semantic_score = _get_float_attr(r, "semantic_score", 0.0)
            bm25_score = _get_float_attr(r, "bm25_score", 0.0)

            # semantic + BM25 同时命中给轻量 overlap bonus
            overlap_bonus = 0.0
            if semantic_score > 0 and bm25_score > 0:
                overlap_bonus += 0.05

            r.final_score = (
                self.alpha * structural_score
                + self.beta * semantic_score
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
