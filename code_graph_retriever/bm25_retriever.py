"""
bm25_retriever.py — 基于 BM25 的轻量词法检索器

特点：
  - 不依赖 embedding API
  - 支持 build()
  - 支持 deepen 后 add_nodes()
  - 支持 search_many() 多路 BM25 召回
  - 返回 RetrievalResponse / RetrievalResult，接口风格对齐 semantic_retriever.py
  - BM25 分数单独写入 bm25_score / bm25_reason（若 RetrievalResult 暂未声明字段，则动态挂载）
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
        self._node_id_set: set[str] = set()
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

    def _get_raw_node_attrs(self, node_id: str) -> dict:
        """
        读取 graph 中某个节点的原始属性字典。

        为什么需要这个函数：
        CodeNode dataclass 只声明了稳定核心字段；
        但 SkeletonBuilder / FileDeepener 可能会额外写入一些辅助字段，例如：
            - decorators
            - class_bases
            - base_names
            - method_summaries
            - unresolved_bases
            - import_aliases
            - from_import_aliases
            - parse_error

        CodeNode.from_dict() 为了安全，会过滤未知字段；
        因此 BM25 如果想利用这些额外字段，需要从 graph 的原始 node attrs 中读取。
        """
        try:
            g = getattr(self.graph, "_g", None)
            if g is not None and node_id in g:
                return dict(g.nodes[node_id])
        except Exception:
            pass

        return {}

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

    def rebuild(self) -> "BM25Retriever":
        """
        完整重建 BM25 索引。

        用途：
          - deepen 后如果只是新增节点，可以用 add_nodes()
          - deepen 后如果会修改已有节点文本、comment、method_names、code_text，
            应调用 rebuild()，避免 BM25 索引读到旧文本。
        """
        return self.build()

    def _clear_index(self) -> None:
        self._node_ids = []
        self._node_id_set = set()
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
            self._node_id_set.add(node.id)
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

        注意：
          - 如果 deepen 只是新增节点，使用 add_nodes() 即可。
          - 如果 deepen 会修改已有节点的 code_text/comment/method_names/signature，
            应使用 rebuild()，因为 add_nodes() 不更新已存在节点。
        """
        self._ensure_built()

        if not new_node_ids:
            return

        if new_texts is not None and len(new_texts) != len(new_node_ids):
            raise ValueError("new_texts 与 new_node_ids 长度不一致")

        added = False
        for i, nid in enumerate(new_node_ids):
            node = self.graph.get_node(nid)
            if node is None:
                continue

            if node.type not in self.target_types:
                continue

            # 避免重复添加同一个 node_id。
            # 如果你希望更新已有节点文本，请调用 rebuild()。
            if nid in self._node_id_set:
                continue

            text = new_texts[i] if new_texts is not None else self._node_to_text(node)
            text = (text or "").strip()[: self.max_text_chars]
            if not text:
                continue

            doc_idx = len(self._node_ids)
            self._node_ids.append(nid)
            self._node_id_set.add(nid)
            self._texts.append(text)

            tokens = self._tokenize(text)
            tf = Counter(tokens)
            self._doc_tf.append(tf)
            self._doc_lens.append(sum(tf.values()))

            for term, freq in tf.items():
                self._df[term] = self._df.get(term, 0) + 1
                self._inverted[term].append((doc_idx, freq))

            added = True

        if added and self._doc_lens:
            self._avgdl = sum(self._doc_lens) / len(self._doc_lens)
        elif not self._doc_lens:
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

            result = RetrievalResult(
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
                semantic_score=0.0,
                final_score=normalized,
                semantic_reason="",
            )
            _safe_setattr(result, "bm25_score", normalized)
            _safe_setattr(result, "bm25_raw_score", float(score))
            _safe_setattr(result, "bm25_reason", reason)
            _safe_setattr(result, "bm25_hit_queries", [query])
            _safe_setattr(result, "bm25_query_groups", {})
            results.append(result)

        return RetrievalResponse(
            query=query,
            results=results,
            total_nodes=len(self._node_ids),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    def search_many(
        self,
        queries: Sequence[str],
        top_k: int = 20,
        per_query_k: int = 50,
        query_groups: Optional[Dict[str, List[str]]] = None,
        group_weights: Optional[Dict[str, float]] = None,
    ) -> RetrievalResponse:
        """
        多路 BM25 查询召回并融合。

        用途：
          - current_query
          - issue exact symbols
          - file hints
          - method/class/function hints
          - behavior terms
          - error terms

        融合策略：
          - 每一路 query 独立归一化；
          - 同一个节点命中多路 query 时累加一个轻量 multi-hit bonus；
          - 可通过 group_weights 让 exact_symbols / file_hints 等更重要。
        """
        self._ensure_built()
        t0 = time.perf_counter()

        queries = _dedup_clean(queries)
        if not queries:
            return RetrievalResponse(
                query="",
                results=[],
                total_nodes=len(self._node_ids),
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        group_weights = group_weights or {}
        query_to_groups: Dict[str, List[str]] = defaultdict(list)
        if query_groups:
            for group, qs in query_groups.items():
                for q in qs:
                    q = (q or "").strip()
                    if q:
                        query_to_groups[q].append(group)

        fused: Dict[str, RetrievalResult] = {}
        best_scores: Dict[str, float] = defaultdict(float)
        hit_counts: Dict[str, int] = defaultdict(int)
        hit_queries: Dict[str, List[str]] = defaultdict(list)
        hit_groups: Dict[str, List[str]] = defaultdict(list)

        for q in queries:
            groups = query_to_groups.get(q, [])
            weight = max([group_weights.get(g, 1.0) for g in groups], default=1.0)
            resp = self.search(q, top_k=per_query_k)

            for r in resp.results:
                nid = r.node_id
                bm25_score = _get_bm25_score(r)
                weighted = bm25_score * weight

                hit_counts[nid] += 1
                hit_queries[nid].append(q)
                hit_groups[nid].extend(groups)

                if nid not in fused or weighted > best_scores[nid]:
                    fused[nid] = r
                    best_scores[nid] = weighted

        results: List[RetrievalResult] = []
        max_hit_count = max(hit_counts.values()) if hit_counts else 1

        for nid, r in fused.items():
            # 多路命中给轻量加成，避免单一路弱 query 和强 exact query 完全等价。
            multi_hit_bonus = 0.10 * (hit_counts[nid] - 1) / max(1, max_hit_count - 1)
            score = min(1.0, best_scores[nid] + multi_hit_bonus)

            _safe_setattr(r, "bm25_score", score)
            _safe_setattr(r, "bm25_reason", self._make_many_reason(
                hit_queries=hit_queries[nid],
                hit_groups=hit_groups[nid],
                score=score,
            ))
            _safe_setattr(r, "bm25_hit_queries", _dedup_clean(hit_queries[nid]))
            _safe_setattr(r, "bm25_query_groups", {
                "groups": _dedup_clean(hit_groups[nid]),
                "hit_count": hit_counts[nid],
            })

            # BM25Retriever 自身返回时 final_score 就等于 bm25_score；
            # 到 HybridRetriever 里会重新计算三路融合 final_score。
            r.final_score = score
            results.append(r)

        ranked = sorted(results, key=lambda r: _get_bm25_score(r), reverse=True)[:top_k]

        return RetrievalResponse(
            query=" | ".join(queries),
            results=ranked,
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
        - skeleton_embedding_text()

        额外增强：
        从 graph 原始节点属性中读取 SkeletonBuilder 写入的扩展字段：
        - decorators
        - class_bases
        - base_names
        - method_summaries
        - unresolved_bases

        不专门读取：
        - import_aliases / from_import_aliases
            因为 SkeletonBuilder 已经把 import alias 摘要写入 MODULE.code_text，
            BM25 读取 code_text 时自然会包含这些信息。
        - parse_error
            这是诊断信息，不适合作为代码检索文本。
        """
        parts: List[str] = []

        # ------------------------------------------------------------------
        # 1. CodeNode 核心字段
        # ------------------------------------------------------------------

        if node.name:
            parts.append(node.name)
            parts.append(self._split_identifier(node.name))

        if node.qualified_name:
            parts.append(node.qualified_name)
            parts.append(self._split_identifier(node.qualified_name))

        if node.file:
            parts.append(node.file)
            parts.append(node.file.replace("/", " ").replace("\\", " "))

        if getattr(node, "signature", ""):
            parts.append(node.signature)
            parts.append(self._split_identifier(node.signature))

        if getattr(node, "docstring", ""):
            parts.append(node.docstring)

        if getattr(node, "method_names", None):
            methods = " ".join(str(x) for x in node.method_names)
            parts.append(methods)
            parts.append(self._split_identifier(methods))

        if getattr(node, "comment", ""):
            parts.append(node.comment)

        if getattr(node, "code_text", ""):
            parts.append(node.code_text)

        # 兜底：保持和 semantic_retriever 的骨架文本兼容。
        try:
            skel_text = node.skeleton_embedding_text()
            if skel_text:
                parts.append(skel_text)
        except Exception:
            pass

        # ------------------------------------------------------------------
        # 2. SkeletonBuilder / FileDeepener 写入的扩展字段
        # ------------------------------------------------------------------

        raw_attrs = self._get_raw_node_attrs(node.id)

        # decorators: ["staticmethod", "property", ...]
        decorators = raw_attrs.get("decorators")
        if isinstance(decorators, str):
            parts.append(decorators)
        elif isinstance(decorators, list):
            dec_text = " ".join(str(x) for x in decorators if x)
            if dec_text:
                parts.append(dec_text)
                parts.append(self._split_identifier(dec_text))

        # class_bases: "(BaseClass, Mixin)"
        class_bases = raw_attrs.get("class_bases")
        if isinstance(class_bases, str) and class_bases.strip():
            parts.append(class_bases)
            parts.append(self._split_identifier(class_bases))

        # base_names: ["BaseClass", "Mixin"]
        base_names = raw_attrs.get("base_names")
        if isinstance(base_names, str):
            parts.append(base_names)
            parts.append(self._split_identifier(base_names))
        elif isinstance(base_names, list):
            base_text = " ".join(str(x) for x in base_names if x)
            if base_text:
                parts.append(base_text)
                parts.append(self._split_identifier(base_text))

        # unresolved_bases: ["ExternalBase", "UnknownMixin"]
        # 虽然没有解析成 INHERITS 边，但对 BM25 匹配父类名仍然有价值。
        unresolved_bases = raw_attrs.get("unresolved_bases")
        if isinstance(unresolved_bases, str):
            parts.append(unresolved_bases)
            parts.append(self._split_identifier(unresolved_bases))
        elif isinstance(unresolved_bases, list):
            unresolved_text = " ".join(str(x) for x in unresolved_bases if x)
            if unresolved_text:
                parts.append(unresolved_text)
                parts.append(self._split_identifier(unresolved_text))

        # method_summaries:
        # Skeleton 阶段不创建 METHOD 节点，因此 CLASS 节点中的 method_summaries
        # 对 BM25 很重要。它能让 BM25 在未 deepen 前就命中类里的方法名、签名、docstring。
        method_summaries = raw_attrs.get("method_summaries")
        if isinstance(method_summaries, list):
            method_parts: List[str] = []

            for item in method_summaries[:50]:
                if isinstance(item, dict):
                    name = str(item.get("name", "") or "")
                    signature = str(item.get("signature", "") or "")
                    docstring = str(item.get("docstring", "") or "")

                    if name:
                        method_parts.append(name)
                        method_parts.append(self._split_identifier(name))

                    if signature:
                        method_parts.append(signature)
                        method_parts.append(self._split_identifier(signature))

                    if docstring:
                        method_parts.append(docstring)

                    item_decorators = item.get("decorators", [])
                    if isinstance(item_decorators, str):
                        method_parts.append(item_decorators)
                        method_parts.append(self._split_identifier(item_decorators))
                    elif isinstance(item_decorators, list):
                        dec_text = " ".join(str(x) for x in item_decorators if x)
                        if dec_text:
                            method_parts.append(dec_text)
                            method_parts.append(self._split_identifier(dec_text))

                elif isinstance(item, str):
                    method_parts.append(item)
                    method_parts.append(self._split_identifier(item))

            if method_parts:
                parts.append("\n".join(p for p in method_parts if p and str(p).strip()))
        # ------------------------------------------------------------------
        # 3. 清洗合并
        # ------------------------------------------------------------------
        text = "\n".join(str(p) for p in parts if p and str(p).strip())
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

    @staticmethod
    def _make_many_reason(hit_queries: Sequence[str], hit_groups: Sequence[str], score: float) -> str:
        groups = _dedup_clean(hit_groups)
        queries = _dedup_clean(hit_queries)
        group_part = f"；命中分组：{', '.join(groups[:5])}" if groups else ""
        query_part = f"；命中查询：{', '.join(queries[:3])}" if queries else ""
        return f"BM25多路召回 score={score:.3f}{group_part}{query_part}"


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


def _get_bm25_score(result: RetrievalResult) -> float:
    if hasattr(result, "bm25_score"):
        try:
            return float(getattr(result, "bm25_score") or 0.0)
        except Exception:
            return 0.0

    # 兼容旧结果：BM25Retriever 自身返回时 final_score 就是 BM25 分数。
    try:
        return float(result.final_score or 0.0)
    except Exception:
        return 0.0


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
