"""
bm25_retriever.py — 基于 BM25 的轻量词法检索器

特点：
  - 不依赖 embedding API
  - 支持 build()
  - 支持 deepen 后 add_nodes()
  - 返回 RetrievalResponse / RetrievalResult，接口风格对齐 semantic_retriever.py
  - 适合作为 search_hybrid 的第一阶段候选召回

BM25 适合匹配：
  - 文件名
  - 类名 / 函数名 / 方法名
  - 参数名
  - 报错关键词
  - issue 中出现的符号名
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Iterable, Tuple

from code_graph_builder.graph_schema import CodeGraph, NodeType
from .retrieval_result import RetrievalResult, RetrievalResponse


class BM25Retriever:
    """
    基于 CodeGraph 节点文本的 BM25 检索器。

    Usage
    -----
    retriever = BM25Retriever(graph)
    retriever.build()
    resp = retriever.search("parse_http_date two digit year", top_k=5)
    """

    def __init__(
        self,
        graph: CodeGraph,
        target_types: Optional[List[NodeType]] = None,
        k1: float = 1.5,
        b: float = 0.75,
        max_text_chars: int = 2000,
        min_token_len: int = 2,
    ):
        self.graph = graph
        self.target_types = target_types or [
            NodeType.MODULE,
            NodeType.CLASS,
            NodeType.FUNCTION,
            NodeType.METHOD,
        ]

        self.k1 = k1
        self.b = b
        self.max_text_chars = max_text_chars
        self.min_token_len = min_token_len

        self._node_ids: List[str] = []
        self._texts: List[str] = []

        # doc index -> term frequency
        self._doc_tf: List[Counter[str]] = []

        # term -> document frequency
        self._df: Dict[str, int] = {}

        # term -> list[(doc_idx, tf)]
        self._inverted: Dict[str, List[Tuple[int, int]]] = defaultdict(list)

        self._doc_lens: List[int] = []
        self._avgdl: float = 0.0
        self._built = False

    # ------------------------------------------------------------------
    # 构建索引
    # ------------------------------------------------------------------

    def build(self) -> "BM25Retriever":
        self._clear_index()
        self._collect_nodes()

        for doc_idx, text in enumerate(self._texts):
            tokens = self._tokenize(text)
            tf = Counter(tokens)
            self._doc_tf.append(tf)
            self._doc_lens.append(sum(tf.values()))

            for term, freq in tf.items():
                self._df[term] = self._df.get(term, 0) + 1
                self._inverted[term].append((doc_idx, freq))

        if self._doc_lens:
            self._avgdl = sum(self._doc_lens) / len(self._doc_lens)
        else:
            self._avgdl = 0.0

        self._built = True
        return self

    def _clear_index(self) -> None:
        self._node_ids = []
        self._texts = []
        self._doc_tf = []
        self._df = {}
        self._inverted = defaultdict(list)
        self._doc_lens = []
        self._avgdl = 0.0
        self._built = False

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
            self._texts.append(text[: self.max_text_chars])

    # ------------------------------------------------------------------
    # 增量添加节点
    # ------------------------------------------------------------------

    def add_nodes(
        self,
        new_node_ids: List[str],
        new_texts: Optional[List[str]] = None,
    ) -> None:
        """
        深化后增量添加新节点到 BM25 索引。

        参数：
          new_node_ids:
              新增节点 ID。
          new_texts:
              可选。若提供，则与 new_node_ids 一一对应；
              若不提供，则从 graph 中读取节点并自动构造文本。
        """
        self._ensure_built()

        if not new_node_ids:
            return

        if new_texts is not None and len(new_texts) != len(new_node_ids):
            raise ValueError("new_texts 与 new_node_ids 长度不一致")

        for i, nid in enumerate(new_node_ids):
            node = self.graph.get_node(nid)
            if node is None:
                continue

            if node.type not in self.target_types:
                continue

            text = new_texts[i] if new_texts is not None else self._node_to_text(node)
            text = (text or "").strip()[: self.max_text_chars]
            if not text:
                continue

            # 避免重复添加同一个 node_id。
            # 简单起见，这里如果已存在则跳过。
            # 如果你希望更新已有节点文本，可以改成 rebuild()。
            if nid in self._node_ids:
                continue

            doc_idx = len(self._node_ids)
            self._node_ids.append(nid)
            self._texts.append(text)

            tokens = self._tokenize(text)
            tf = Counter(tokens)
            self._doc_tf.append(tf)
            self._doc_lens.append(sum(tf.values()))

            for term, freq in tf.items():
                self._df[term] = self._df.get(term, 0) + 1
                self._inverted[term].append((doc_idx, freq))

        if self._doc_lens:
            self._avgdl = sum(self._doc_lens) / len(self._doc_lens)
        else:
            self._avgdl = 0.0

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 5) -> RetrievalResponse:
        self._ensure_built()

        t0 = time.perf_counter()

        if not self._node_ids:
            return RetrievalResponse(
                query=query,
                results=[],
                total_nodes=0,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        q_tokens = self._tokenize(query)
        if not q_tokens:
            return RetrievalResponse(
                query=query,
                results=[],
                total_nodes=len(self._node_ids),
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        scores: Dict[int, float] = defaultdict(float)

        # 对 query token 去重，避免 query 里重复词过度放大。
        for term in sorted(set(q_tokens)):
            postings = self._inverted.get(term)
            if not postings:
                continue

            idf = self._idf(term)

            for doc_idx, tf in postings:
                dl = self._doc_lens[doc_idx] if doc_idx < len(self._doc_lens) else 0
                score = self._bm25_term_score(tf=tf, dl=dl, idf=idf)
                scores[doc_idx] += score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        ranked = ranked[:top_k]

        max_score = ranked[0][1] if ranked else 0.0

        results: List[RetrievalResult] = []
        for doc_idx, score in ranked:
            nid = self._node_ids[doc_idx]
            node = self.graph.get_node(nid)
            if node is None:
                continue

            normalized = self._normalize_score(score, max_score)
            reason = self._explain_bm25_match(query, self._texts[doc_idx], score)

            results.append(
                RetrievalResult(
                    node_id=nid,
                    node_name=node.name,
                    qualified_name=node.qualified_name,
                    node_type=node.type.value,
                    file=node.file,
                    start_line=node.start_line,
                    end_line=node.end_line,
                    code_text=node.code_text,
                    comment=node.comment,
                    structural_score=0.0,
                    semantic_score=normalized,
                    final_score=normalized,
                    semantic_reason=reason,
                )
            )

        return RetrievalResponse(
            query=query,
            results=results,
            total_nodes=len(self._node_ids),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    def candidate_node_ids(self, query: str, top_k: int = 200) -> List[str]:
        """
        返回 BM25 召回的候选 node_id 列表。

        用途：
          - 给 embedding rerank 之前做低成本候选召回。
        """
        resp = self.search(query, top_k=top_k)
        return [r.node_id for r in resp.results]

    # ------------------------------------------------------------------
    # BM25 公式
    # ------------------------------------------------------------------

    def _idf(self, term: str) -> float:
        """
        BM25 IDF.

        使用常见平滑形式：
            log(1 + (N - df + 0.5) / (df + 0.5))
        """
        n_docs = len(self._node_ids)
        df = self._df.get(term, 0)

        if n_docs <= 0 or df <= 0:
            return 0.0

        return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))

    def _bm25_term_score(self, tf: int, dl: int, idf: float) -> float:
        if tf <= 0 or dl <= 0:
            return 0.0

        avgdl = self._avgdl if self._avgdl > 0 else 1.0
        denom = tf + self.k1 * (1.0 - self.b + self.b * dl / avgdl)

        return idf * (tf * (self.k1 + 1.0)) / denom

    @staticmethod
    def _normalize_score(score: float, max_score: float) -> float:
        if max_score <= 1e-12:
            return 0.0
        return float(max(0.0, min(1.0, score / max_score)))

    # ------------------------------------------------------------------
    # 文本构造
    # ------------------------------------------------------------------

    def _node_to_text(self, node) -> str:
        """
        构造 BM25 检索文本。

        BM25 对符号词很敏感，所以这里刻意加入：
          - node.name
          - qualified_name
          - file path
          - signature
          - docstring
          - method_names
          - comment
          - code_text
        """
        parts: List[str] = []

        if node.name:
            parts.append(node.name)
            parts.append(self._split_identifier(node.name))

        if node.qualified_name:
            parts.append(node.qualified_name)
            parts.append(self._split_identifier(node.qualified_name))

        if node.file:
            parts.append(node.file)
            parts.append(node.file.replace("/", " ").replace("\\", " "))

        if node.signature:
            parts.append(node.signature)
            parts.append(self._split_identifier(node.signature))

        if node.docstring:
            parts.append(node.docstring)

        if node.method_names:
            methods = " ".join(node.method_names)
            parts.append(methods)
            parts.append(self._split_identifier(methods))

        if node.comment:
            parts.append(node.comment)

        if node.code_text:
            parts.append(node.code_text)

        # 兜底：保持和 semantic_retriever 的骨架文本兼容。
        try:
            skel_text = node.skeleton_embedding_text()
            if skel_text:
                parts.append(skel_text)
        except Exception:
            pass

        text = "\n".join(p for p in parts if p and p.strip())
        return text.strip()

    # ------------------------------------------------------------------
    # 分词
    # ------------------------------------------------------------------

    def _tokenize(self, text: str) -> List[str]:
        """
        面向代码的轻量 tokenizer。

        能处理：
          - snake_case
          - CamelCase
          - dotted.path
          - 中文词块
          - 数字
        """
        if not text:
            return []

        text = text.lower()

        raw_tokens = re.findall(
            r"[a-zA-Z_][a-zA-Z0-9_]*|[0-9]+|[\u4e00-\u9fff]+",
            text,
        )

        tokens: List[str] = []
        for tok in raw_tokens:
            if len(tok) < self.min_token_len:
                continue

            tokens.append(tok)

            # 进一步拆 identifier。
            for sub in self._split_identifier(tok).split():
                if len(sub) >= self.min_token_len:
                    tokens.append(sub)

        return tokens

    @staticmethod
    def _split_identifier(text: str) -> str:
        """
        将代码符号拆成更容易 BM25 匹配的词。

        examples:
          parse_http_date -> parse http date
          FilePathField   -> file path field
          django.utils.http -> django utils http
        """
        if not text:
            return ""

        # 路径/限定名分隔符
        text = re.sub(r"[./\\:]+", " ", text)

        # snake_case
        text = text.replace("_", " ")

        # CamelCase: FilePathField -> File Path Field
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)

        return text.lower()

    # ------------------------------------------------------------------
    # 解释
    # ------------------------------------------------------------------

    def _explain_bm25_match(self, query: str, node_text: str, score: float) -> str:
        q_tokens = set(self._tokenize(query))
        n_tokens = set(self._tokenize(node_text))
        common = sorted(q_tokens & n_tokens)

        if common:
            return f"BM25共享词：{'、'.join(common[:6])}（BM25 {score:.2f}）"

        return f"BM25词法相关（BM25 {score:.2f}）"
        