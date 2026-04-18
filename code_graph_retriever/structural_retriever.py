"""
structural_retriever.py — 基于结构特征的检索（GRACE 路线）
============================================================
对应论文 3.3.2：相似度检索（结构特征部分）

核心思想（来自 GRACE 论文）：
  代码图中的节点有其"结构角色"——高扇入的工具函数、顶层基类、
  只被一处调用的私有方法……这些角色特征在文本语义空间中并不显现，
  但在结构特征空间中可以被精确度量。

  结构检索回答的问题是："哪些节点在代码图中扮演着与 query 相同的角色？"

检索流程
--------
  1. 由 FeatureExtractor 构建全图特征矩阵
  2. 接收 query（node_id 或外部特征向量）
  3. 用 sklearn NearestNeighbors（余弦相似度）找 Top-K
  4. 构建 RetrievalResult，填写结构匹配原因

原因生成逻辑
------------
对每个检索结果，比较其特征向量与 query 特征向量的各维度差异，
找出"最相似的维度"，生成自然语言描述。
例如：入度相近 → "同为被频繁调用的工具函数"
      继承深度相同 → "位于相同继承层级"

向量搜索后端
------------
  默认使用 sklearn NearestNeighbors（纯 Python，无需额外安装）。
  生产环境可通过 VectorIndex 抽象层替换为 FAISS。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.neighbors import NearestNeighbors

from code_graph_builder.graph_schema import CodeGraph, NodeType
from .feature_extractor import FeatureExtractor, FEATURE_DIM, IDX_CALL_IN, IDX_CALL_OUT, \
    IDX_INHERIT_DEPTH, IDX_SUBCLASSES, IDX_METHODS, IDX_IS_ABSTRACT, IDX_IS_OVERRIDE, IDX_MODULE_IN
from .retrieval_result import RetrievalResult, RetrievalResponse


# 各维度对应的人类可读描述（用于生成原因文本）
_DIM_DESCRIPTIONS = {
    IDX_CALL_IN:       ("调用入度", "被调用频率"),
    IDX_CALL_OUT:      ("调用出度", "调用其他函数的数量"),
    IDX_INHERIT_DEPTH: ("继承层级深度", "在继承链中的位置"),
    IDX_SUBCLASSES:    ("子类数量",   "作为父类的广度"),
    IDX_METHODS:       ("方法数量",   "类的规模"),
    IDX_IS_ABSTRACT:   ("抽象性",     "是否为抽象节点"),
    IDX_IS_OVERRIDE:   ("重写特性",   "是否重写父类方法"),
    IDX_MODULE_IN:     ("模块被依赖度", "所在模块的被引用程度"),
}

# 认定"相似"的最大差异阈值（归一化后）
_SIMILARITY_THRESHOLD = 0.15


class StructuralRetriever:
    """
    基于结构特征向量的代码节点检索器。

    Usage
    -----
    retriever = StructuralRetriever(graph)
    retriever.build()

    # 以某个已知节点为 query
    response = retriever.search_by_node_id("src/utils.py::helper", top_k=5)

    # 以外部特征向量为 query（适用于 query 是仓库外部代码的情况）
    vec = np.array([0.2, 0.8, 0.0, 0.0, 0.0, 0.0, 1.0, 0.3], dtype=np.float32)
    response = retriever.search_by_vector(vec, query_text="外部函数", top_k=5)
    """

    def __init__(self, graph: CodeGraph, extractor: Optional[FeatureExtractor] = None):
        self.graph     = graph
        self.extractor = extractor or FeatureExtractor(graph)
        self._nn:       Optional[NearestNeighbors] = None
        self._node_ids: List[str] = []
        self._matrix:   Optional[np.ndarray] = None
        self._built = False

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def build(self) -> "StructuralRetriever":
        """构建特征索引。"""
        self.extractor.build()
        self._node_ids, self._matrix = self.extractor.get_matrix()

        if len(self._node_ids) == 0:
            self._built = True
            return self

        # 使用余弦距离的近邻搜索
        self._nn = NearestNeighbors(
            metric="cosine",
            algorithm="brute",   # 节点数通常 <10K，brute 最稳定
        )
        self._nn.fit(self._matrix)
        self._built = True
        return self

    def search_by_node_id(
        self,
        node_id: str,
        top_k: int = 5,
        exclude_self: bool = True,
    ) -> RetrievalResponse:
        """
        以图中某个节点为 query，检索结构最相似的 Top-K 节点。

        Parameters
        ----------
        node_id      : 查询节点的 id
        top_k        : 返回结果数量
        exclude_self : 是否从结果中排除 query 节点自身
        """
        self._ensure_built()
        t0 = time.perf_counter()

        vec = self.extractor.get_feature(node_id)
        if vec is None:
            return RetrievalResponse(
                query=node_id, results=[], total_nodes=len(self._node_ids),
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        query_node = self.graph.get_node(node_id)
        query_text = query_node.qualified_name if query_node else node_id
        # 这里的query_text为什么要等于qualified_name

        results = self._search(
            vec, query_text=query_text,
            top_k=top_k + (1 if exclude_self else 0),
            exclude_ids={node_id} if exclude_self else set(),
            query_vec_raw=vec,
        )

        return RetrievalResponse(
            query       = query_text,
            results     = results[:top_k],
            total_nodes = len(self._node_ids),
            elapsed_ms  = (time.perf_counter() - t0) * 1000,
        )

    def search_by_vector(
        self,
        query_vec: np.ndarray,
        query_text: str = "",
        top_k: int = 5,
    ) -> RetrievalResponse:
        """
        以外部特征向量为 query 检索。
        适用于 query 是仓库外部代码片段时（先提取其特征向量再检索）。
        """
        self._ensure_built()
        t0 = time.perf_counter()
        results = self._search(query_vec, query_text=query_text, top_k=top_k,
                               query_vec_raw=query_vec)
        return RetrievalResponse(
            query       = query_text,
            results     = results,
            total_nodes = len(self._node_ids),
            elapsed_ms  = (time.perf_counter() - t0) * 1000,
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()


# query_text并没有被用到
    def _search(
        self,
        query_vec:     np.ndarray,
        query_text:    str,
        top_k:         int,
        exclude_ids:   Optional[set] = None,
        query_vec_raw: Optional[np.ndarray] = None,
    ) -> List[RetrievalResult]:
        if self._nn is None or len(self._node_ids) == 0:
            return []

        exclude_ids = exclude_ids if exclude_ids is not None else set()
        # 多取一些以防 exclude 后不够
        k = min(top_k + len(exclude_ids) + 5, len(self._node_ids))
        qvec = query_vec.reshape(1, -1)

        distances, indices = self._nn.kneighbors(qvec, n_neighbors=k)
        # cosine distance → cosine similarity
        similarities = 1.0 - distances[0]

        results = []
        for sim, idx in zip(similarities, indices[0]):
            nid = self._node_ids[idx]
            if nid in exclude_ids:
                continue
            node = self.graph.get_node(nid)
            if node is None:
                continue

            feat_vec = self._matrix[idx]
            struct_reason = self._explain_structural_match(
                (query_vec_raw if query_vec_raw is not None else query_vec), feat_vec
            )
            pos  = self.extractor.get_position(nid)
            pos_text = pos.to_text() if pos else ""

            results.append(RetrievalResult(
                node_id          = nid,
                node_name        = node.name,
                qualified_name   = node.qualified_name,
                node_type        = node.type.value,
                file             = node.file,
                start_line       = node.start_line,
                end_line         = node.end_line,
                code_text        = node.code_text,
                comment          = node.comment,
                structural_score = float(max(0.0, sim)),
                semantic_score   = 0.0,   # 语义分由 SemanticRetriever 填写
                final_score      = float(max(0.0, sim)),
                structural_reason = struct_reason,
                position_summary = pos_text,
                position         = pos,
            ))
            if len(results) >= top_k:
                break

        return results

# 这里需要修改，改成LLM解释
    @staticmethod
    def _explain_structural_match(
        query_vec: np.ndarray,
        result_vec: np.ndarray,
    ) -> str:
        """
        比较两个特征向量，找出最相似的维度，生成自然语言原因描述。
        """
        diffs  = np.abs(query_vec - result_vec)
        similar_dims = [
            i for i in range(FEATURE_DIM)
            if diffs[i] < _SIMILARITY_THRESHOLD
        ]

        if not similar_dims:
            return "整体结构模式相似"

        reasons = []
        for dim in similar_dims[:3]:   # 最多列出 3 个维度
            name, desc = _DIM_DESCRIPTIONS.get(dim, (f"维度{dim}", ""))
            qval = query_vec[dim]
            rval = result_vec[dim]

            if dim in (IDX_IS_ABSTRACT, IDX_IS_OVERRIDE):
                # 二值特征
                if qval > 0.5:
                    reasons.append(f"同为{desc}节点")
                # else: 同为非抽象，不值得提
            elif dim == IDX_CALL_IN:
                if qval > 0.6:
                    reasons.append("同为被频繁调用的函数")
                elif qval < 0.2:
                    reasons.append("同为调用入度低的函数")
                else:
                    reasons.append(f"{name}相近")
            elif dim == IDX_CALL_OUT:
                if qval > 0.6:
                    reasons.append("同为调用其他函数较多的复杂函数")
                else:
                    reasons.append(f"{name}相近")
            elif dim == IDX_INHERIT_DEPTH:
                reasons.append(f"位于相同继承层级（{name}相近）")
            else:
                reasons.append(f"{name}相近")

        return "；".join(reasons) if reasons else "整体结构拓扑模式相似"