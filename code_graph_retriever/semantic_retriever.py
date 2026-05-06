"""
semantic_retriever.py — 基于注释/骨架 Embedding 的语义检索（支持增量 add_nodes）
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


class EmbeddingBackend(ABC):
    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    def embed_batch(self, texts: List[str]) -> np.ndarray: ...

    def embed(self, text: str) -> np.ndarray:
        return self.embed_batch([text])[0]

class DashScopeEmbeddingBackend(EmbeddingBackend):
    BASE_URL       = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    MODEL          = "text-embedding-v3"
    EMBED_DIM      = 1024
    BATCH_SIZE     = 25
    MAX_TEXT_CHARS = 2000
    BATCH_INTERVAL = 0.2
    MAX_RETRIES    = 3
    RETRY_DELAY    = 1.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
        batch_size: Optional[int] = None,
    ):
        import time as _time
        self._time = _time
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise ValueError("未找到 DASHSCOPE_API_KEY")

        self.model = model or self.MODEL
        self.timeout = timeout
        self.batch_size = min(batch_size or self.BATCH_SIZE, self.BATCH_SIZE)

    @property
    def dim(self) -> int:
        return self.EMBED_DIM

    def embed_batch(self, texts: List[str]) -> np.ndarray:
        from openai import OpenAI

        processed = [
            t[:self.MAX_TEXT_CHARS] if len(t) > self.MAX_TEXT_CHARS else t
            for t in texts
        ]

        if not processed:
            return np.zeros((0, self.dim), dtype=np.float32)

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.BASE_URL,
            timeout=self.timeout,
        )

        results: List[List[float]] = []
        n_batches = (len(processed) + self.batch_size - 1) // self.batch_size

        for batch_idx, i in enumerate(range(0, len(processed), self.batch_size)):
            batch = processed[i: i + self.batch_size]

            if batch_idx > 0:
                self._time.sleep(self.BATCH_INTERVAL)

            try:
                resp = client.embeddings.create(
                    model=self.model,
                    input=batch,
                    encoding_format="float",
                )
            except Exception as e:
                raise RuntimeError(
                    f"DashScope Embedding 调用失败: model={self.model}, "
                    f"base_url={self.BASE_URL}, batch_size={len(batch)}"
                ) from e

            items = sorted(resp.data, key=lambda x: x.index)
            results.extend([item.embedding for item in items])

        return np.array(results, dtype=np.float32)


class TFIDFEmbeddingBackend(EmbeddingBackend):
    def __init__(self, n_components: int = 64):
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import TruncatedSVD
        self._vectorizer = TfidfVectorizer(max_features=5000, analyzer="word",
                                           token_pattern=r"[a-zA-Z_][a-zA-Z0-9_]*")
        self._svd = TruncatedSVD(n_components=n_components, random_state=42)
        self._n_components = n_components
        self._fitted = False

    @property
    def dim(self) -> int:
        return self._n_components

    def fit(self, corpus: List[str]) -> None:
        if not corpus:
            return
        tfidf = self._vectorizer.fit_transform(corpus)
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
            raise RuntimeError("请先调用 fit(corpus)")
        tfidf = self._vectorizer.transform(texts)
        vecs  = self._svd.transform(tfidf).astype(np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms


class MockEmbeddingBackend(EmbeddingBackend):
    def __init__(self, dim: int = 64, seed: int = 42):
        self._dim = dim
        self._rng = np.random.RandomState(seed)

    @property
    def dim(self) -> int:
        return self._dim

    def embed_batch(self, texts: List[str]) -> np.ndarray:
        vecs = []
        for text in texts:
            seed = hash(text) % (2**31)
            rng  = np.random.RandomState(seed)
            vec  = rng.randn(self._dim).astype(np.float32)
            vec /= (np.linalg.norm(vec) + 1e-8)
            vecs.append(vec)
        return np.stack(vecs)


def get_default_embedding_backend() -> EmbeddingBackend:
    import logging
    _logger = logging.getLogger(__name__)
    ds_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if ds_key:
        try:
            backend = DashScopeEmbeddingBackend(api_key=ds_key)
            _logger.info("SemanticRetriever: 使用 DashScopeEmbeddingBackend")
            return backend
        except Exception:
            pass
    _logger.warning("SemanticRetriever: 降级为 TFIDFEmbeddingBackend")
    return TFIDFEmbeddingBackend()


class SemanticRetriever:
    def __init__(self, graph: CodeGraph, backend: Optional[EmbeddingBackend] = None,
                 target_types: Optional[List[NodeType]] = None):
        self.graph    = graph
        self.backend  = backend
        self.target_types = target_types or [NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD]
        self._node_ids:  List[str] = []
        self._texts:     List[str] = []
        self._matrix:    Optional[np.ndarray] = None
        self._nn:        Optional[NearestNeighbors] = None
        self._built = False

    def build(self) -> "SemanticRetriever":
        if self.backend is None:
            self.backend = get_default_embedding_backend()
        self._collect_nodes()
        if not self._node_ids:
            self._built = True
            return self
        if isinstance(self.backend, TFIDFEmbeddingBackend):
            self.backend.fit(self._texts)
        self._matrix = self.backend.embed_batch(self._texts)
        self._nn = NearestNeighbors(metric="cosine", algorithm="brute")
        self._nn.fit(self._matrix)
        self._built = True
        return self

    def add_nodes(
        self,
        new_node_ids: List[str],
        new_texts:    List[str],
        new_embeddings: np.ndarray,
    ) -> None:
        """深化后增量添加新节点到索引。"""
        if len(new_node_ids) == 0:
            return
        self._node_ids.extend(new_node_ids)
        self._texts.extend(new_texts)
        if self._matrix is not None and self._matrix.shape[0] > 0:
            self._matrix = np.vstack([self._matrix, new_embeddings])
        else:
            self._matrix = new_embeddings
        self._nn = NearestNeighbors(metric="cosine", algorithm="brute")
        self._nn.fit(self._matrix)

    def search(self, query: str, top_k: int = 5) -> RetrievalResponse:
        self._ensure_built()
        t0 = time.perf_counter()
        if self._nn is None or len(self._node_ids) == 0:
            return RetrievalResponse(query=query, results=[], total_nodes=0,
                                     elapsed_ms=(time.perf_counter() - t0) * 1000)
        query_vec = self.backend.embed(query)
        k = min(top_k + 5, len(self._node_ids))
        distances, indices = self._nn.kneighbors(query_vec.reshape(1, -1), n_neighbors=k)
        similarities = 1.0 - distances[0]

        results = []
        for sim, idx in zip(similarities, indices[0]):
            nid  = self._node_ids[idx]
            node = self.graph.get_node(nid)
            if node is None:
                continue
            sem_reason = self._explain_semantic_match(query, self._texts[idx], float(sim))
            results.append(RetrievalResult(
                node_id=nid, node_name=node.name, qualified_name=node.qualified_name,
                node_type=node.type.value, file=node.file,
                start_line=node.start_line, end_line=node.end_line,
                code_text=node.code_text, comment=node.comment,
                structural_score=0.0, semantic_score=float(max(0.0, sim)),
                final_score=float(max(0.0, sim)), semantic_reason=sem_reason,
            ))
            if len(results) >= top_k:
                break
        return RetrievalResponse(query=query, results=results, total_nodes=len(self._node_ids),
                                 elapsed_ms=(time.perf_counter() - t0) * 1000)

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()

    def _collect_nodes(self) -> None:
        for node in self.graph.iter_nodes():
            if node.type not in self.target_types:
                continue
            # 优先 comment → 骨架 embedding text → code_text 前 500 字
            text = node.comment.strip() if node.comment.strip() \
                else node.skeleton_embedding_text() if (node.signature or node.docstring or node.method_names) \
                else node.code_text[:500].strip()
            if not text:
                continue
            self._node_ids.append(node.id)
            self._texts.append(text)

    @staticmethod
    def _explain_semantic_match(query: str, node_text: str, score: float) -> str:
        import re
        def keywords(text):
            words = re.findall(r"[a-zA-Z\u4e00-\u9fff]+", text.lower())
            stopwords = {"的", "了", "是", "在", "有", "和", "与", "或",
                         "self", "return", "def", "class", "pass", "the", "a", "an",
                         "is", "are", "to", "of", "for", "in", "that", "this"}
            return {w for w in words if w not in stopwords and len(w) > 1}
        q_kw = keywords(query)
        n_kw = keywords(node_text)
        common = q_kw & n_kw
        if common:
            return f"共享关键词：{'、'.join(sorted(common)[:4])}（相似度 {score:.2f}）"
        elif score > 0.7:
            return f"语义高度相关（{score:.2f}）"
        elif score > 0.4:
            return f"语义中等相关（{score:.2f}）"
        return f"语义弱相关（{score:.2f}）"