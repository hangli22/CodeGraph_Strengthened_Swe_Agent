"""
semantic_retriever.py — 基于注释 Embedding 的语义检索
=======================================================
对应论文 3.3.2：相似度检索（语义特征部分）

核心思想：
  Issue 描述是自然语言，node.comment 也是自然语言，
  两者在语义 embedding 空间中的距离比"自然语言 vs 源码"近得多。
  因此用 comment embedding 做语义检索比直接对源码做 embedding 效果更好。

  语义检索回答的问题是："哪些节点的功能描述与 query 在语义上最接近？"

冷启动处理
----------
  若节点的 comment 字段为空（未调用 LLM 标注），
  自动降级为对 code_text 的前 500 字符做 embedding，保证可用性。

Embedding 后端
--------------
  EmbeddingBackend 抽象基类，内置三种实现：

  DashScopeEmbeddingBackend（默认优先）
    - 调用阿里云百炼 text-embedding-v3
    - 接入点：https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings
    - 读取 DASHSCOPE_API_KEY 环境变量
    - 支持批量请求（最多 25 条/次），显著减少 API 调用次数

  TFIDFEmbeddingBackend（纯本地，无需 API）
    - 基于 sklearn TF-IDF + SVD 的伪 embedding
    - 维度 64，语义质量低于神经 embedding，但完全离线
    - 适用于：快速调试、无网络环境、大规模批量预处理

  MockEmbeddingBackend（测试用）
    - 返回随机向量，不消耗 API，用于单元测试

向量索引
--------
  同 structural_retriever，使用 sklearn NearestNeighbors（余弦距离），
  可无缝替换为 FAISS。
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.neighbors import NearestNeighbors

from code_graph_builder.graph_schema import CodeGraph, NodeType
from .retrieval_result import RetrievalResult, RetrievalResponse


# ===========================================================================
# Embedding 后端抽象层
# ===========================================================================

class EmbeddingBackend(ABC):
    """所有 embedding 后端的抽象基类。"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """embedding 向量维度。"""
        ...

    @abstractmethod
    def embed_batch(self, texts: List[str]) -> np.ndarray:
        """
        批量 embedding。

        Parameters
        ----------
        texts : 文本列表

        Returns
        -------
        np.ndarray, shape (len(texts), dim)
        """
        ...

    def embed(self, text: str) -> np.ndarray:
        """单条 embedding（默认调用 embed_batch）。"""
        return self.embed_batch([text])[0]


class DashScopeEmbeddingBackend(EmbeddingBackend):
    """
    阿里云百炼 text-embedding-v3 后端。

    获取 API Key：https://bailian.console.aliyun.com/
    接入点文档：https://help.aliyun.com/zh/model-studio/

    特性：
    - 向量维度 1024（text-embedding-v3 默认）
    - 支持批量请求（每批最多 25 条）
    - 无需安装额外包，使用标准库 urllib
    """

    API_URL   = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    MODEL     = "text-embedding-v3"
    EMBED_DIM = 1024
    BATCH_SIZE = 25   # API 限制每次最多 25 条

    def __init__(self, api_key: Optional[str] = None, timeout: int = 60):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "未找到 DASHSCOPE_API_KEY。\n"
                "请设置环境变量或传入 DashScopeEmbeddingBackend(api_key='sk-xxx')"
            )
        self.timeout = timeout

    @property
    def dim(self) -> int:
        return self.EMBED_DIM

    def embed_batch(self, texts: List[str]) -> np.ndarray:
        """分批调用 API，合并结果。"""
        import json, urllib.request, urllib.error

        results = []
        for i in range(0, len(texts), self.BATCH_SIZE):
            batch = texts[i: i + self.BATCH_SIZE]
            payload = json.dumps({
                "model": self.MODEL,
                "input": batch,
                "encoding_format": "float",
            }).encode("utf-8")

            req = urllib.request.Request(
                self.API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    # 按 index 排序确保顺序正确
                    items = sorted(data["data"], key=lambda x: x["index"])
                    batch_vecs = [item["embedding"] for item in items]
                    results.extend(batch_vecs)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"DashScope Embedding API 错误 {e.code}: {body}") from e

        return np.array(results, dtype=np.float32)


class TFIDFEmbeddingBackend(EmbeddingBackend):
    """
    基于 TF-IDF + SVD 的本地 embedding（无需 API，完全离线）。

    语义质量低于神经 embedding，但适用于：
    - 无网络的调试环境
    - 快速验证检索流程
    - 极大规模预处理（不受 API 限流约束）

    fit() 必须在 embed_batch() 之前调用，传入语料库文本。
    SemanticRetriever 会自动处理这一步。
    """

    def __init__(self, n_components: int = 64):
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import TruncatedSVD
        self._vectorizer = TfidfVectorizer(
            max_features=5000,
            analyzer="word",
            token_pattern=r"[a-zA-Z_][a-zA-Z0-9_]*",
        )
        self._svd = TruncatedSVD(n_components=n_components, random_state=42)
        self._n_components = n_components
        self._fitted = False

    @property
    def dim(self) -> int:
        return self._n_components

    def fit(self, corpus: List[str]) -> None:
        """在语料库上训练 TF-IDF 模型。"""
        if not corpus:
            return
        tfidf = self._vectorizer.fit_transform(corpus)
        # SVD 的 n_components 不能超过特征数
        n_comp = min(self._n_components, tfidf.shape[1] - 1, tfidf.shape[0] - 1)
        if n_comp < 1:
            n_comp = 1
        if n_comp != self._n_components:
            from sklearn.decomposition import TruncatedSVD
            self._svd = TruncatedSVD(n_components=n_comp, random_state=42)
            self._n_components = n_comp
        self._svd.fit(tfidf)
        self._fitted = True

    def embed_batch(self, texts: List[str]) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("请先调用 fit(corpus) 训练 TF-IDF 模型")
        tfidf = self._vectorizer.transform(texts)
        vecs  = self._svd.transform(tfidf).astype(np.float32)
        # L2 归一化
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms


class MockEmbeddingBackend(EmbeddingBackend):
    """
    随机向量 embedding（仅用于单元测试）。
    使用固定 seed 保证可复现。
    """

    def __init__(self, dim: int = 64, seed: int = 42):
        self._dim  = dim
        self._rng  = np.random.RandomState(seed)

    @property
    def dim(self) -> int:
        return self._dim

    def embed_batch(self, texts: List[str]) -> np.ndarray:
        # 基于文本哈希生成确定性向量（同文本 → 同向量）
        vecs = []
        for text in texts:
            seed = hash(text) % (2**31)
            rng  = np.random.RandomState(seed)
            vec  = rng.randn(self._dim).astype(np.float32)
            vec /= (np.linalg.norm(vec) + 1e-8)
            vecs.append(vec)
        return np.stack(vecs)


def get_default_embedding_backend() -> EmbeddingBackend:
    """
    按优先级自动选择可用的 embedding 后端：
    DashScope → TF-IDF（兜底）
    """
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if key:
        try:
            backend = DashScopeEmbeddingBackend(api_key=key)
            import logging
            logging.getLogger(__name__).info(
                "SemanticRetriever: 使用 DashScopeEmbeddingBackend"
            )
            return backend
        except Exception:
            pass
    import logging
    logging.getLogger(__name__).warning(
        "SemanticRetriever: 未找到 DASHSCOPE_API_KEY，降级为 TFIDFEmbeddingBackend"
    )
    return TFIDFEmbeddingBackend()


# ===========================================================================
# SemanticRetriever
# ===========================================================================

class SemanticRetriever:
    """
    基于 comment embedding 的语义检索器。

    Usage
    -----
    # 自动选择后端
    retriever = SemanticRetriever(graph)
    retriever.build()

    # 指定后端
    retriever = SemanticRetriever(graph, backend=DashScopeEmbeddingBackend())
    retriever.build()

    # 检索
    response = retriever.search("处理 HTTP 重定向时出现异常", top_k=5)
    """

    def __init__(
        self,
        graph:   CodeGraph,
        backend: Optional[EmbeddingBackend] = None,
        target_types: Optional[List[NodeType]] = None,
    ):
        self.graph    = graph
        self.backend  = backend   # None → build() 时自动选择
        self.target_types = target_types or [
            NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD
        ]
        self._node_ids:  List[str] = []
        self._texts:     List[str] = []   # 对应每个节点的检索文本
        self._matrix:    Optional[np.ndarray] = None
        self._nn:        Optional[NearestNeighbors] = None
        self._built = False

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def build(self) -> "SemanticRetriever":
        """
        对所有目标节点生成 embedding，构建向量索引。
        """
        # 延迟初始化后端（允许在 build 前修改 backend 属性）
        if self.backend is None:
            self.backend = get_default_embedding_backend()

        # 如果是 TF-IDF，需要先 fit
        self._collect_nodes()
        if not self._node_ids:
            self._built = True
            return self

        if isinstance(self.backend, TFIDFEmbeddingBackend):
            self.backend.fit(self._texts)

        # 批量 embedding
        self._matrix = self.backend.embed_batch(self._texts)

        # 构建近邻索引
        self._nn = NearestNeighbors(metric="cosine", algorithm="brute")
        self._nn.fit(self._matrix)
        self._built = True
        return self

    def search(
        self,
        query: str,
        top_k: int = 5,
    ) -> RetrievalResponse:
        """
        以自然语言字符串为 query 检索语义最相关的节点。

        Parameters
        ----------
        query : issue 描述、函数名、或任意自然语言
        top_k : 返回结果数量
        """
        self._ensure_built()
        t0 = time.perf_counter()

        if self._nn is None or len(self._node_ids) == 0:
            return RetrievalResponse(
                query=query, results=[], total_nodes=0,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        query_vec = self.backend.embed(query)
        k = min(top_k + 5, len(self._node_ids))
        distances, indices = self._nn.kneighbors(
            query_vec.reshape(1, -1), n_neighbors=k
        )
        similarities = 1.0 - distances[0]

        results = []
        for sim, idx in zip(similarities, indices[0]):
            nid  = self._node_ids[idx]
            node = self.graph.get_node(nid)
            if node is None:
                continue

            sem_reason = self._explain_semantic_match(
                query, self._texts[idx], float(sim)
            )

            results.append(RetrievalResult(
                node_id        = nid,
                node_name      = node.name,
                qualified_name = node.qualified_name,
                node_type      = node.type.value,
                file           = node.file,
                start_line     = node.start_line,
                end_line       = node.end_line,
                code_text      = node.code_text,
                comment        = node.comment,
                structural_score = 0.0,
                semantic_score   = float(max(0.0, sim)),
                final_score      = float(max(0.0, sim)),
                semantic_reason  = sem_reason,
            ))
            if len(results) >= top_k:
                break

        return RetrievalResponse(
            query       = query,
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

    def _collect_nodes(self) -> None:
        """收集目标节点，决定每个节点用 comment 还是 code_text 做 embedding。"""
        for node in self.graph.iter_nodes():
            if node.type not in self.target_types:
                continue
            # 优先用 comment；冷启动时降级为 code_text 前 500 字符
            text = node.comment.strip() if node.comment.strip() \
                else node.code_text[:500].strip()
            if not text:
                continue
            self._node_ids.append(node.id)
            self._texts.append(text)

    @staticmethod
    def _explain_semantic_match(query: str, node_text: str, score: float) -> str:
        """
        生成语义关联的自然语言说明。
        简单策略：找 query 和节点文本的共同关键词。
        """
        def keywords(text: str):
            import re
            words = re.findall(r"[a-zA-Z\u4e00-\u9fff]+", text.lower())
            stopwords = {"的", "了", "是", "在", "有", "和", "与", "或",
                         "self", "return", "def", "class", "pass", "the", "a", "an",
                         "is", "are", "to", "of", "for", "in", "that", "this"}
            return {w for w in words if w not in stopwords and len(w) > 1}

        q_kw = keywords(query)
        n_kw = keywords(node_text)
        common = q_kw & n_kw

        if common:
            kw_str = "、".join(sorted(common)[:4])
            return f"与 query 共享关键词：{kw_str}（语义相似度 {score:.2f}）"
        elif score > 0.7:
            return f"语义高度相关（相似度 {score:.2f}），功能描述与 query 高度匹配"
        elif score > 0.4:
            return f"语义中等相关（相似度 {score:.2f}）"
        else:
            return f"语义弱相关（相似度 {score:.2f}）"