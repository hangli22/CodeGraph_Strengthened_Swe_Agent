"""
semantic_retriever.py — 基于注释/骨架 Embedding 的语义检索（支持增量 add_nodes）

SemanticRetriever.add_nodes() 接口不太实用，而且没有去重



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
import logging

logger = logging.getLogger(__name__)

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
    BATCH_SIZE     = 10
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
                )
            except Exception as e:
                err_msg = str(e)
                err_type = type(e).__name__

                # OpenAI SDK 的异常对象通常会带 response / status_code / body 等信息。
                status_code = getattr(e, "status_code", None)
                response = getattr(e, "response", None)
                body = getattr(e, "body", None)

                response_text = ""
                if response is not None:
                    try:
                        response_text = response.text
                    except Exception:
                        response_text = ""

                # 打印当前 batch 的基本信息，方便判断是否是空文本、超长文本或 batch 太大。
                lengths = [len(t) for t in batch]
                empty_count = sum(1 for t in batch if not t.strip())
                preview = batch[0][:300].replace("\n", "\\n") if batch else ""

                logger.error(
                    "DashScope Embedding 原始异常: type=%s status_code=%s error=%s body=%s response=%s "
                    "model=%s base_url=%s batch_size=%d empty_count=%d min_len=%s max_len=%s first_text_preview=%r",
                    err_type,
                    status_code,
                    err_msg,
                    body,
                    response_text,
                    self.model,
                    self.BASE_URL,
                    len(batch),
                    empty_count,
                    min(lengths) if lengths else None,
                    max(lengths) if lengths else None,
                    preview,
                )

                raise RuntimeError(
                    f"DashScope Embedding 调用失败: model={self.model}, "
                    f"base_url={self.BASE_URL}, batch_size={len(batch)}, "
                    f"error_type={err_type}, status_code={status_code}, error={err_msg}"
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
        """
        初次完整构建 semantic 索引。

        注意：
        build 的语义是“从当前 graph 完整构建索引”，不是增量追加。
        因此每次 build 前必须清空旧索引，避免重复收集同一批节点。
        """
        if self.backend is None:
            self.backend = get_default_embedding_backend()

        self._clear_index()
        self._collect_nodes()

        if not self._node_ids:
            self._built = True
            return self

        # TFIDF backend 不能稳定做局部增量；完整 build 时重新 fit。
        if isinstance(self.backend, TFIDFEmbeddingBackend):
            self.backend.fit(self._texts)

        self._matrix = self.backend.embed_batch(self._texts)
        self._refit_nn()
        self._built = True
        return self

    def _clear_index(self) -> None:
        """清空 semantic 索引。"""
        self._node_ids = []
        self._texts = []
        self._matrix = None
        self._nn = None
        self._built = False


    def rebuild(self) -> "SemanticRetriever":
        """
        完整重建 semantic 索引。

        主要用于：
        - fallback；
        - TFIDF backend；
        - 发现增量更新不可靠时。
        """
        self._clear_index()
        return self.build()


    def _refit_nn(self) -> None:
        """根据当前 embedding matrix 重新 fit NearestNeighbors。"""
        if self._matrix is None or self._matrix.shape[0] == 0:
            self._nn = None
            return

        self._nn = NearestNeighbors(metric="cosine", algorithm="brute")
        self._nn.fit(self._matrix)

    def _node_to_text(self, node) -> str:
        """
        构造 semantic embedding 文本。

        优先级：
        1. comment：如果有 LLM 注释，通常语义最浓缩；
        2. skeleton_embedding_text：包含 signature/docstring/method_names；
        3. code_text：深化后完整源码；
        4. qualified_name/name/file：兜底。
        """
        parts: List[str] = []

        if getattr(node, "comment", "") and node.comment.strip():
            parts.append(node.comment.strip())

        # skeleton_embedding_text 会利用 signature / docstring / method_names。
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

        if getattr(node, "code_text", ""):
            parts.append(node.code_text[:1200].strip())

        if getattr(node, "qualified_name", ""):
            parts.append(node.qualified_name)

        if getattr(node, "file", ""):
            parts.append(f"file {node.file}")

        text = "\n".join(p for p in parts if p and str(p).strip()).strip()
        return text

    def add_nodes(
        self,
        new_node_ids: List[str],
        new_texts: Optional[List[str]] = None,
        new_embeddings: Optional[np.ndarray] = None,
    ) -> None:
        """
        增量添加新节点到 semantic 索引。

        兼容旧接口：
        - 如果外部传入 new_texts/new_embeddings，则直接使用；
        - 如果不传，则自动从 graph 构造文本并调用 embedding backend。

        已存在节点不会重复添加；如需更新已有节点，请使用 update_nodes()。
        """
        self._ensure_built()

        if not new_node_ids:
            return

        if new_texts is not None and len(new_texts) != len(new_node_ids):
            raise ValueError("new_texts 与 new_node_ids 长度不一致")

        existing = set(self._node_ids)
        add_ids: List[str] = []
        add_texts: List[str] = []

        for i, nid in enumerate(new_node_ids):
            if nid in existing:
                continue

            node = self.graph.get_node(nid)
            if node is None:
                continue

            if node.type not in self.target_types:
                continue

            text = new_texts[i] if new_texts is not None else self._node_to_text(node)
            text = (text or "").strip()
            if not text:
                continue

            add_ids.append(nid)
            add_texts.append(text)

        if not add_ids:
            return

        if self.backend is None:
            self.backend = get_default_embedding_backend()

        # TFIDF/SVD 不适合局部增量，直接完整重建更安全。
        if isinstance(self.backend, TFIDFEmbeddingBackend):
            self.rebuild()
            return

        if new_embeddings is not None:
            if new_embeddings.shape[0] != len(new_node_ids):
                raise ValueError("new_embeddings 行数必须与 new_node_ids 长度一致")

            # 只取真正新增节点对应的 embedding。
            id_to_emb = {
                nid: new_embeddings[i]
                for i, nid in enumerate(new_node_ids)
            }
            add_embeddings = np.stack([id_to_emb[nid] for nid in add_ids]).astype(np.float32)
        else:
            add_embeddings = self.backend.embed_batch(add_texts)

        self._node_ids.extend(add_ids)
        self._texts.extend(add_texts)

        if self._matrix is not None and self._matrix.shape[0] > 0:
            self._matrix = np.vstack([self._matrix, add_embeddings])
        else:
            self._matrix = add_embeddings

        self._refit_nn()
        self._built = True

    def update_nodes(self, updated_node_ids: List[str]) -> None:
        """
        更新已有节点的 semantic 文本和 embedding。

        用于 deepen 后：
        - CLASS 节点 code_text 从骨架摘要变成完整 class source；
        - FUNCTION 节点 code_text/signature/docstring 被更新；
        - 已有 METHOD 节点被再次 deepen 时也可能更新。
        """
        self._ensure_built()

        if not updated_node_ids:
            return

        if self.backend is None:
            self.backend = get_default_embedding_backend()

        # TFIDF/SVD 的向量空间依赖整体 corpus，局部更新不可靠，直接 rebuild。
        if isinstance(self.backend, TFIDFEmbeddingBackend):
            self.rebuild()
            return

        id_to_idx = {nid: i for i, nid in enumerate(self._node_ids)}

        real_ids: List[str] = []
        real_indices: List[int] = []
        real_texts: List[str] = []

        for nid in updated_node_ids:
            idx = id_to_idx.get(nid)
            if idx is None:
                continue

            node = self.graph.get_node(nid)
            if node is None:
                continue

            if node.type not in self.target_types:
                continue

            text = self._node_to_text(node)
            text = (text or "").strip()
            if not text:
                continue

            real_ids.append(nid)
            real_indices.append(idx)
            real_texts.append(text)

        if not real_ids:
            return

        new_embeddings = self.backend.embed_batch(real_texts)

        if self._matrix is None or self._matrix.shape[0] == 0:
            self._matrix = new_embeddings
        else:
            for row_idx, text, emb in zip(real_indices, real_texts, new_embeddings):
                self._texts[row_idx] = text
                self._matrix[row_idx] = emb

        self._refit_nn()
        self._built = True


    def update_after_deepen(
        self,
        new_node_ids: Optional[List[str]] = None,
        updated_node_ids: Optional[List[str]] = None,
    ) -> None:
        """
        deepen 后的 semantic 增量维护入口。

        行为：
        - updated_node_ids：更新已有 CLASS/FUNCTION/METHOD 的文本和 embedding；
        - new_node_ids：追加新 METHOD 节点；
        - 最后重新 fit NearestNeighbors。

        注意：
        - DashScope/Mock embedding 可以局部更新；
        - TFIDF backend 不适合局部更新，自动 fallback 到 rebuild。
        """
        self._ensure_built()

        new_node_ids = new_node_ids or []
        updated_node_ids = updated_node_ids or []

        if not new_node_ids and not updated_node_ids:
            return

        if self.backend is None:
            self.backend = get_default_embedding_backend()

        if isinstance(self.backend, TFIDFEmbeddingBackend):
            self.rebuild()
            return

        if updated_node_ids:
            self.update_nodes(updated_node_ids)

        if new_node_ids:
            self.add_nodes(new_node_ids)

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

            text = self._node_to_text(node)
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