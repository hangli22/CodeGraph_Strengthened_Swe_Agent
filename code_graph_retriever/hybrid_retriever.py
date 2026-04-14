"""
hybrid_retriever.py — 结构检索 + 语义检索融合
===============================================
对应论文 3.3.2：融合权重设计

融合策略
--------
  final_score = α × structural_score + β × semantic_score
  其中 α + β = 1，默认 α=0.4, β=0.6（语义为主，结构为辅）

  权重设计依据：
  - SWE-bench 任务的 issue 描述是自然语言，语义匹配更直接
  - 结构检索补充"功能相似但描述不同"的节点（如同类工具函数）
  - 两者互补而非竞争，加权融合覆盖更多情况

重排序机制
----------
  对两路独立检索的结果做合并去重，然后按 final_score 排序：
  1. 结构检索拿 top_k × 2 个候选
  2. 语义检索拿 top_k × 2 个候选
  3. 合并去重（取两路分数的最大值）
  4. 按 final_score 降序，取前 top_k

  这样既保留了高结构相似性节点（两路都找到 → final_score 更高），
  也保留了只有一路找到的独特匹配。

对应消融实验（论文 4.3.1）
--------------------------
  可通过设置权重观察不同配置的效果：
  - α=1.0, β=0.0 → 纯结构检索
  - α=0.0, β=1.0 → 纯语义检索
  - α=0.4, β=0.6 → 混合（默认）
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from code_graph_builder.graph_schema import CodeGraph
from .feature_extractor import FeatureExtractor
from .structural_retriever import StructuralRetriever
from .semantic_retriever import SemanticRetriever, EmbeddingBackend
from .retrieval_result import RetrievalResult, RetrievalResponse


class HybridRetriever:
    """
    融合结构检索与语义检索的统一检索器。
    这是对外的主入口，SWE-Agent 直接调用此类。

    Usage
    -----
    # 一键构建（自动选择 embedding 后端）
    retriever = HybridRetriever(graph)
    retriever.build()

    # 以 issue 描述为 query
    response = retriever.search("URL 解析时出现 UnicodeDecodeError", top_k=5)

    # 以仓库内某个节点为 query
    response = retriever.search_by_node("src/utils.py::parse_url", top_k=5)

    # 消融实验：只用结构检索
    retriever = HybridRetriever(graph, alpha=1.0, beta=0.0)
    """

    def __init__(
        self,
        graph:            CodeGraph,
        alpha:            float = 0.4,     # 结构权重
        beta:             float = 0.6,     # 语义权重
        embedding_backend: Optional[EmbeddingBackend] = None,
    ):
        assert abs(alpha + beta - 1.0) < 1e-6, "alpha + beta 必须等于 1.0"
        self.graph  = graph
        self.alpha  = alpha
        self.beta   = beta

        self._extractor  = FeatureExtractor(graph)
        self._structural = StructuralRetriever(graph, extractor=self._extractor)
        self._semantic   = SemanticRetriever(graph, backend=embedding_backend)
        self._built = False

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def build(self) -> "HybridRetriever":
        """构建两路检索器的索引。"""
        self._extractor.build()
        self._structural.build()
        self._semantic.build()
        self._built = True
        return self

    def search(
        self,
        query:  str,
        top_k:  int = 5,
    ) -> RetrievalResponse:
        """
        以自然语言字符串（issue 描述等）为 query 进行混合检索。

        Parameters
        ----------
        query : 自然语言 query，如 issue 标题或描述
        top_k : 最终返回的节点数量
        """
        self._ensure_built()
        t0 = time.perf_counter()

        candidate_k = top_k * 3   # 多取候选，融合后再截断

        # 两路独立检索
        sem_resp    = self._semantic.search(query, top_k=candidate_k)

        # 语义检索结果中找最佳节点，用其做结构检索
        # 若语义检索无结果（冷启动），结构检索返回空
        struct_results: List[RetrievalResult] = []
        if sem_resp.results and self.alpha > 0:
            best_nid = sem_resp.results[0].node_id
            struct_resp = self._structural.search_by_node_id(
                best_nid, top_k=candidate_k, exclude_self=False
            )
            struct_results = struct_resp.results

        # 融合
        merged = self._merge(
            struct_results, sem_resp.results, top_k=top_k
        )

        return RetrievalResponse(
            query       = query,
            results     = merged,
            total_nodes = sem_resp.total_nodes,
            elapsed_ms  = (time.perf_counter() - t0) * 1000,
        )

    def search_by_node(
        self,
        node_id: str,
        top_k:   int = 5,
    ) -> RetrievalResponse:
        """
        以仓库内某个节点为 query，检索与其结构和语义都相似的节点。
        适用于 Agent 已定位到某函数，希望找到类似实现时。

        Parameters
        ----------
        node_id : 目标节点 id，格式为 "file.py::ClassName.method_name"
        top_k   : 返回结果数量
        """
        self._ensure_built()
        t0 = time.perf_counter()

        candidate_k = top_k * 3
        node = self.graph.get_node(node_id)
        query_text = node.qualified_name if node else node_id

        # 结构检索：以该节点的结构特征为 query
        struct_resp = self._structural.search_by_node_id(
            node_id, top_k=candidate_k, exclude_self=True
        )

        # 语义检索：以该节点的 comment 或 code_text 为 query
        sem_query = ""
        if node:
            sem_query = node.comment.strip() if node.comment.strip() \
                else node.code_text[:300].strip()
        sem_resp = self._semantic.search(sem_query, top_k=candidate_k) \
            if sem_query else RetrievalResponse(query=query_text)

        # 过滤掉 query 节点自身
        sem_results = [r for r in sem_resp.results if r.node_id != node_id]

        merged = self._merge(struct_resp.results, sem_results, top_k=top_k)

        return RetrievalResponse(
            query       = query_text,
            results     = merged,
            total_nodes = struct_resp.total_nodes,
            elapsed_ms  = (time.perf_counter() - t0) * 1000,
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()

    def _merge(
        self,
        struct_results: List[RetrievalResult],
        sem_results:    List[RetrievalResult],
        top_k:          int,
    ) -> List[RetrievalResult]:
        """
        合并两路结果，计算 final_score，按分数排序后取 top_k。

        同一节点在两路都出现时：
        - final_score = α × structural_score + β × semantic_score
        - 原因文本合并
        只在一路出现时：
        - 缺失的一路分数记为 0
        - final_score 为对应权重 × 该路分数
        """
        # 以 node_id 为 key 建立字典，保存两路的最优结果
        by_id: Dict[str, RetrievalResult] = {}

        for r in struct_results:
            by_id[r.node_id] = r   # structural_score 已经设好

        for r in sem_results:
            if r.node_id in by_id:
                # 两路都有，合并分数和原因
                existing = by_id[r.node_id]
                existing.semantic_score  = r.semantic_score
                existing.semantic_reason = r.semantic_reason
            else:
                by_id[r.node_id] = r

        # 计算融合分数
        for nid, r in by_id.items():
            r.final_score = (
                self.alpha * r.structural_score
                + self.beta  * r.semantic_score
            )

        # 按 final_score 降序排列
        ranked = sorted(by_id.values(), key=lambda x: x.final_score, reverse=True)
        return ranked[:top_k]