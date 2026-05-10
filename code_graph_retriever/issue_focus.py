"""
issue_focus.py — Issue Focus 抽取与 BM25 多路查询构造
==================================================

用途：
  1. 在每个 SWE-bench instance 开始时，只调用一次 LLM，从原始 issue 中抽取适合 BM25 的字段。
  2. 将 initial issue focus 存储到 cache/{instance_id}/issue_focus.json。
  3. 后续 search_hybrid/search_bm25/deepen_file 可读取该 cache，生成多路 BM25 查询。
  4. 支持对 agent 后续传入的 query 再抽取 query_focus，并与 initial_issue_focus 分开存储。
     query_focus 是可覆盖更新的。

设计：
  - initial_issue_focus: 来自原始 problem_statement，整个 instance 生命周期内通常不变。
  - query_focus: 来自当前检索 query，可随着 agent 新线索覆盖更新。
  - bm25_queries: 不粗暴拼成长 query，而是生成多路短 query。
"""

from __future__ import annotations

import math
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ===========================================================================
# Uni-API Backend
# ===========================================================================

class UniAPIBackend:
    """
    使用中国科技云 Uni-API（OpenAI 兼容接口）。

    默认模型 deepseek-v4-flash，API Key 从环境变量 UNI_API_KEY 获取。
    接口风格与 comment_annotator.py 中的 UniAPIBackend 保持一致。
    """

    API_URL = "https://uni-api.cstcloud.cn/v1/chat/completions"
    MODEL = "deepseek-v4-flash"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ):
        self.api_key = api_key or os.environ.get("UNI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "未找到 Uni-API Key。\n"
                "请设置环境变量 UNI_API_KEY，\n"
                "或在构造时传入 UniAPIBackend(api_key='...')"
            )
        self.model = model or self.MODEL
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model": self.model,
            "max_tokens": 768,
            "temperature": 0.0,
            "messages": [{"role": "user", "content": prompt}],
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
                return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Uni-API 错误 {e.code}: {body}") from e


# ===========================================================================
# 数据结构
# ===========================================================================

@dataclass
class IssueFocus:
    """
    单份 focus。

    initial_issue_focus 和 query_focus 都使用这个结构。
    """

    source_type: str = ""          # "initial_issue" | "query"
    source_text: str = ""

    exact_symbols: List[str] = field(default_factory=list)
    file_hints: List[str] = field(default_factory=list)
    class_hints: List[str] = field(default_factory=list)
    function_hints: List[str] = field(default_factory=list)
    method_hints: List[str] = field(default_factory=list)
    parameter_hints: List[str] = field(default_factory=list)

    behavior_terms: List[str] = field(default_factory=list)
    error_terms: List[str] = field(default_factory=list)

    bm25_queries: List[str] = field(default_factory=list)
    raw_keywords: List[str] = field(default_factory=list)

    raw_llm_response: str = ""
    extractor: str = "llm"
    created_at: float = field(default_factory=time.time)

    def normalize(self) -> "IssueFocus":
        self.exact_symbols = _dedup_clean(self.exact_symbols)
        self.file_hints = _dedup_clean(self.file_hints)
        self.class_hints = _dedup_clean(self.class_hints)
        self.function_hints = _dedup_clean(self.function_hints)
        self.method_hints = _dedup_clean(self.method_hints)
        self.parameter_hints = _dedup_clean(self.parameter_hints)
        self.behavior_terms = _dedup_clean(self.behavior_terms)
        self.error_terms = _dedup_clean(self.error_terms)
        self.bm25_queries = _dedup_clean(self.bm25_queries)
        self.raw_keywords = _dedup_clean(self.raw_keywords)
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IssueFocus":
        field_names = set(cls.__dataclass_fields__.keys())
        safe = {k: v for k, v in d.items() if k in field_names}
        return cls(**safe).normalize()


@dataclass
class IssueFocusCache:
    """
    issue_focus.json 的整体结构。
    """

    instance_id: str = ""
    initial_issue_focus: Optional[IssueFocus] = None
    query_focus: Optional[IssueFocus] = None

    version: int = 1
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "instance_id": self.instance_id,
            "updated_at": self.updated_at,
            "initial_issue_focus": (
                self.initial_issue_focus.to_dict()
                if self.initial_issue_focus else None
            ),
            "query_focus": (
                self.query_focus.to_dict()
                if self.query_focus else None
            ),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IssueFocusCache":
        initial = d.get("initial_issue_focus")
        query = d.get("query_focus")
        return cls(
            version=int(d.get("version", 1)),
            instance_id=d.get("instance_id", ""),
            updated_at=float(d.get("updated_at", time.time())),
            initial_issue_focus=IssueFocus.from_dict(initial) if isinstance(initial, dict) else None,
            query_focus=IssueFocus.from_dict(query) if isinstance(query, dict) else None,
        )


@dataclass
class BM25QueryBundle:
    """
    给 BM25 使用的多路查询。

    group_weights:
        每个 query group 的 BM25 权重。
        用于控制 initial_issue_focus / query_focus / current_query 的相对影响。
    """

    current_query: str = ""
    queries: List[str] = field(default_factory=list)
    query_groups: Dict[str, List[str]] = field(default_factory=dict)
    group_weights: Dict[str, float] = field(default_factory=dict)

    def to_list(self) -> List[str]:
        return _dedup_clean(self.queries)


# ===========================================================================
# Cache Store
# ===========================================================================

class IssueFocusStore:
    """
    issue_focus.json 的读写封装。

    推荐每个 instance 一个 cache_dir：
        cache/{instance_id}/issue_focus.json
    """

    FILENAME = "issue_focus.json"

    def __init__(
        self,
        cache_dir: str,
        instance_id: str = "",
        backend: Optional[object] = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.instance_id = instance_id
        self.path = self.cache_dir / self.FILENAME
        self.backend = backend

    def load(self) -> IssueFocusCache:
        if not self.path.exists():
            return IssueFocusCache(instance_id=self.instance_id)

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            cache = IssueFocusCache.from_dict(data)
            if self.instance_id and not cache.instance_id:
                cache.instance_id = self.instance_id
            return cache
        except Exception as e:
            logger.warning("读取 issue focus cache 失败，将返回空 cache: %s", e)
            return IssueFocusCache(instance_id=self.instance_id)

    def save(self, cache: IssueFocusCache) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache.updated_at = time.time()
        if self.instance_id and not cache.instance_id:
            cache.instance_id = self.instance_id
        self.path.write_text(
            json.dumps(cache.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def ensure_initial_issue_focus(
        self,
        issue_text: str,
        force: bool = False,
    ) -> IssueFocus:
        """
        保证 initial_issue_focus 存在。

        force=False 时：
          - 如果 cache 中已有 initial_issue_focus，则直接返回，不再调用 LLM。
        force=True 时：
          - 重新调用 LLM 抽取并覆盖。
        """
        cache = self.load()

        if cache.initial_issue_focus is not None and not force:
            return cache.initial_issue_focus

        focus = extract_focus_with_llm(
            text=issue_text,
            source_type="initial_issue",
            backend=self.backend,
        )

        cache.initial_issue_focus = focus
        self.save(cache)

        logger.info(
            "Issue focus 已保存: %s | symbols=%d bm25_queries=%d",
            self.path,
            len(focus.exact_symbols),
            len(focus.bm25_queries),
        )
        return focus

    def update_query_focus(
        self,
        query: str,
        force: bool = True,
    ) -> IssueFocus:
        """
        抽取并覆盖 query_focus。

        这个字段用于 agent 后续某一轮 search 的当前 query。
        它和 initial_issue_focus 分开存储，可以不断被替换。
        """
        cache = self.load()

        if cache.query_focus is not None and not force:
            return cache.query_focus

        focus = extract_focus_with_llm(
            text=query,
            source_type="query",
            backend=self.backend,
        )

        cache.query_focus = focus
        self.save(cache)

        logger.info(
            "Query focus 已更新: %s | symbols=%d bm25_queries=%d",
            self.path,
            len(focus.exact_symbols),
            len(focus.bm25_queries),
        )
        return focus

    def clear_query_focus(self) -> None:
        cache = self.load()
        cache.query_focus = None
        self.save(cache)

    def build_bm25_query_bundle(
        self,
        current_query: str = "",
        include_query_focus: bool = True,
        update_query_focus: bool = False,
        max_queries: int = 12,
        retrieval_step: int = 0,
        issue_start_weight: float = 1.2,
        issue_min_weight: float = 0.3,
        issue_decay_lambda: float = 0.25,
    ) -> BM25QueryBundle:
        """
        从 initial_issue_focus + current_query + query_focus 构造 BM25 多路查询。

        注意：
        - 不把所有字段粗暴拼成一个超长 query；
        - 而是形成多路短 query；
        - initial_issue_focus 的 group 权重会随 retrieval_step 指数衰减；
        - current_query / query_focus 不衰减，用于后期更相信 agent 当前线索。
        """
        if current_query and update_query_focus:
            self.update_query_focus(current_query, force=True)

        cache = self.load()
        initial = cache.initial_issue_focus
        query_focus = cache.query_focus if include_query_focus else None

        groups: Dict[str, List[str]] = {}

        if current_query.strip():
            groups["current_query"] = [current_query.strip()]

        if initial is not None:
            groups["initial_exact_symbols"] = _make_exact_symbol_queries(initial)
            groups["initial_method_class"] = _make_method_class_queries(initial)
            groups["initial_behavior"] = _make_behavior_queries(initial)
            groups["initial_error"] = _make_error_queries(initial)
            groups["initial_bm25_queries"] = initial.bm25_queries

        if query_focus is not None:
            groups["query_exact_symbols"] = _make_exact_symbol_queries(query_focus)
            groups["query_method_class"] = _make_method_class_queries(query_focus)
            groups["query_behavior"] = _make_behavior_queries(query_focus)
            groups["query_error"] = _make_error_queries(query_focus)
            groups["query_bm25_queries"] = query_focus.bm25_queries

        queries: List[str] = []
        for _, qs in groups.items():
            for q in qs:
                q = q.strip()
                if q:
                    queries.append(q)

        queries = _dedup_clean(queries)[:max_queries]

        group_weights = build_bm25_group_weights(
            retrieval_step=retrieval_step,
            issue_start_weight=issue_start_weight,
            issue_min_weight=issue_min_weight,
            issue_decay_lambda=issue_decay_lambda,
        )

        return BM25QueryBundle(
            current_query=current_query,
            queries=queries,
            query_groups=groups,
            group_weights=group_weights,
        )

    def run_bm25_search(
        self,
        bm25_retriever: object,
        current_query: str = "",
        top_k: int = 20,
        per_query_k: int = 50,
        include_query_focus: bool = True,
        update_query_focus: bool = False,
        retrieval_step: int = 0,
    ):
        """
        根据 cache 中的 focus 字段运行 BM25 多路检索。

        bm25_retriever 需要提供：
          - search(query, top_k)
          - search_many(queries, top_k, per_query_k, query_groups, group_weights)

        initial_issue_focus 的权重会随 retrieval_step 指数衰减。
        """
        bundle = self.build_bm25_query_bundle(
            current_query=current_query,
            include_query_focus=include_query_focus,
            update_query_focus=update_query_focus,
            retrieval_step=retrieval_step,
        )
        queries = bundle.to_list()

        if not queries:
            if current_query.strip():
                queries = [current_query.strip()]
            else:
                queries = []

        if hasattr(bm25_retriever, "search_many"):
            return bm25_retriever.search_many(
                queries,
                top_k=top_k,
                per_query_k=per_query_k,
                query_groups=bundle.query_groups,
                group_weights=bundle.group_weights,
            )

        # fallback：多次 search 后按 node_id 去重融合。
        # fallback 无法精确利用 group_weights，只保留旧逻辑。
        fused: Dict[str, Any] = {}
        best_scores: Dict[str, float] = {}

        for q in queries:
            resp = bm25_retriever.search(q, top_k=per_query_k)
            for r in getattr(resp, "results", []):
                nid = r.node_id
                score = float(getattr(r, "final_score", 0.0))
                if nid not in fused or score > best_scores.get(nid, -1.0):
                    fused[nid] = r
                    best_scores[nid] = score

        ranked = sorted(
            fused.values(),
            key=lambda r: float(getattr(r, "final_score", 0.0)),
            reverse=True,
        )[:top_k]

        try:
            from .retrieval_result import RetrievalResponse
            return RetrievalResponse(
                query=" | ".join(queries),
                results=ranked,
                total_nodes=getattr(
                    bm25_retriever,
                    "_node_ids",
                    [],
                ).__len__() if hasattr(bm25_retriever, "_node_ids") else 0,
                elapsed_ms=0.0,
            )
        except Exception:
            return ranked

# ===========================================================================
# LLM 抽取
# ===========================================================================

def extract_focus_with_llm(
    text: str,
    source_type: str,
    backend: Optional[object] = None,
) -> IssueFocus:
    """
    用 LLM 抽取 IssueFocus。

    source_type:
      - "initial_issue"
      - "query"
    """
    text = (text or "").strip()
    if not text:
        return IssueFocus(source_type=source_type, source_text="").normalize()

    backend = backend or UniAPIBackend()

    prompt = _build_focus_prompt(text=text, source_type=source_type)

    try:
        raw = backend.generate(prompt)
        data = _parse_llm_json(raw)
        focus = _focus_from_llm_dict(
            data=data,
            source_type=source_type,
            source_text=text,
            raw_llm_response=raw,
        )
        return focus.normalize()
    except Exception as e:
        logger.warning("LLM focus 抽取失败，降级为规则抽取: %s", e)
        focus = extract_focus_with_rules(text=text, source_type=source_type)
        focus.extractor = "rules_fallback"
        return focus.normalize()


def _build_focus_prompt(text: str, source_type: str) -> str:
    if source_type == "initial_issue":
        task_desc = (
            "You are extracting retrieval focus from a SWE-bench issue. "
            "The fields will be used to build BM25 queries for code retrieval."
        )
    else:
        task_desc = (
            "You are extracting retrieval focus from a search query generated during debugging. "
            "The fields will be used to refine BM25 queries."
        )

    return f"""
{task_desc}

Return ONLY a valid JSON object. Do not include markdown fences. Do not explain.

JSON schema:
{{
  "exact_symbols": ["exact code symbols mentioned, e.g. parse_http_date, FilePathField, _separable"],
  "file_hints": ["file paths or file name hints, e.g. django/utils/http.py, separable.py"],
  "class_hints": ["class names, e.g. FilePathField, CompoundModel"],
  "function_hints": ["top-level function names, e.g. parse_http_date"],
  "method_hints": ["method names, e.g. deconstruct, formfield, __init__"],
  "parameter_hints": ["parameter or attribute names, e.g. allow_files, SCRIPT_NAME"],
  "behavior_terms": ["short behavior phrases useful for BM25, e.g. two digit year, nested compound model"],
  "error_terms": ["exception names, assertion words, error messages, unexpected values"],
  "bm25_queries": [
    "short BM25 query 1",
    "short BM25 query 2",
    "short BM25 query 3"
  ],
  "raw_keywords": ["other important retrieval keywords"]
}}

Rules:
- Keep each list concise.
- Prefer exact code-like tokens when present.
- Do not invent file names or symbols that are not supported by the text.
- bm25_queries should be short, not one huge query.
- Include both exact symbols and behavior words when useful.
- If unsure, leave the field as an empty list.

Text:
{text}
""".strip()


def _parse_llm_json(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()

    # 直接 JSON
    try:
        return json.loads(raw)
    except Exception:
        pass

    # 去掉可能的 markdown fence
    raw2 = re.sub(r"^```(?:json)?", "", raw.strip(), flags=re.IGNORECASE).strip()
    raw2 = re.sub(r"```$", "", raw2.strip()).strip()
    try:
        return json.loads(raw2)
    except Exception:
        pass

    # 截取第一个 {...}
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        sub = raw[start:end + 1]
        return json.loads(sub)

    raise ValueError("LLM output is not valid JSON")


def _focus_from_llm_dict(
    data: Dict[str, Any],
    source_type: str,
    source_text: str,
    raw_llm_response: str,
) -> IssueFocus:
    return IssueFocus(
        source_type=source_type,
        source_text=source_text,
        exact_symbols=_as_str_list(data.get("exact_symbols")),
        file_hints=_as_str_list(data.get("file_hints")),
        class_hints=_as_str_list(data.get("class_hints")),
        function_hints=_as_str_list(data.get("function_hints")),
        method_hints=_as_str_list(data.get("method_hints")),
        parameter_hints=_as_str_list(data.get("parameter_hints")),
        behavior_terms=_as_str_list(data.get("behavior_terms")),
        error_terms=_as_str_list(data.get("error_terms")),
        bm25_queries=_as_str_list(data.get("bm25_queries")),
        raw_keywords=_as_str_list(data.get("raw_keywords")),
        raw_llm_response=raw_llm_response,
        extractor="llm",
    )


# ===========================================================================
# 规则 fallback
# ===========================================================================

def extract_focus_with_rules(text: str, source_type: str = "initial_issue") -> IssueFocus:
    """
    LLM 失败时的规则 fallback。
    """
    text = text or ""

    file_hints = re.findall(
        r"[\w./\\-]+\.py",
        text,
    )

    code_spans = re.findall(r"`([^`]+)`", text)

    snake_symbols = re.findall(
        r"\b[_a-zA-Z][a-zA-Z0-9_]*\b",
        text,
    )

    camel_symbols = [
        s for s in snake_symbols
        if re.search(r"[A-Z]", s) and len(s) >= 3
    ]

    function_like = re.findall(
        r"\b([_a-zA-Z][a-zA-Z0-9_]*)\s*\(",
        text,
    )

    exception_terms = re.findall(
        r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning)\b",
        text,
    )

    exact_symbols: List[str] = []
    exact_symbols.extend(code_spans)
    exact_symbols.extend(function_like)
    exact_symbols.extend([
        s for s in snake_symbols
        if "_" in s or s.startswith("_")
    ])
    exact_symbols.extend(camel_symbols)

    class_hints = [
        s for s in camel_symbols
        if s[:1].isupper()
    ]

    function_hints = [
        s for s in function_like
        if not s[:1].isupper()
    ]

    method_hints = [
        s for s in exact_symbols
        if s in {"__init__", "__str__", "__repr__", "deconstruct", "formfield", "clean", "save"}
    ]

    parameter_hints = [
        s for s in snake_symbols
        if "_" in s and s not in function_hints
    ]

    raw_keywords = _simple_keywords(text)

    bm25_queries = _dedup_clean([
        " ".join(_dedup_clean(exact_symbols)[:8]),
        " ".join(_dedup_clean(class_hints + method_hints + function_hints)[:8]),
        " ".join(raw_keywords[:10]),
    ])

    return IssueFocus(
        source_type=source_type,
        source_text=text,
        exact_symbols=exact_symbols,
        file_hints=file_hints,
        class_hints=class_hints,
        function_hints=function_hints,
        method_hints=method_hints,
        parameter_hints=parameter_hints,
        behavior_terms=raw_keywords[:8],
        error_terms=exception_terms,
        bm25_queries=bm25_queries,
        raw_keywords=raw_keywords,
        extractor="rules",
    ).normalize()


# ===========================================================================
# BM25 Query 构造
# ===========================================================================

def build_bm25_query_bundle_from_cache(
    cache_dir: str,
    current_query: str = "",
    include_query_focus: bool = True,
    update_query_focus: bool = False,
    backend: Optional[object] = None,
    max_queries: int = 12,
    retrieval_step: int = 0,
    issue_start_weight: float = 1.2,
    issue_min_weight: float = 0.3,
    issue_decay_lambda: float = 0.25,
) -> BM25QueryBundle:
    """
    便捷函数：从 cache_dir 加载 issue_focus.json 并构造 BM25 多路 query。

    retrieval_step:
        当前 instance 内第几次检索。
        用于让 initial_issue_focus 的 BM25 group 权重指数衰减。
    """
    store = IssueFocusStore(cache_dir=cache_dir, backend=backend)
    return store.build_bm25_query_bundle(
        current_query=current_query,
        include_query_focus=include_query_focus,
        update_query_focus=update_query_focus,
        max_queries=max_queries,
        retrieval_step=retrieval_step,
        issue_start_weight=issue_start_weight,
        issue_min_weight=issue_min_weight,
        issue_decay_lambda=issue_decay_lambda,
    )


def build_bm25_group_weights(
    retrieval_step: int = 0,
    issue_start_weight: float = 1.2,
    issue_min_weight: float = 0.3,
    issue_decay_lambda: float = 0.25,
) -> Dict[str, float]:
    """
    构造 BM25 多路查询的 group 权重。

    设计目标：
      - current_query 始终保持高权重；
      - query_focus 来自 agent 当前检索意图，保持稳定；
      - initial_issue_focus 来自原始 issue，随检索轮次指数衰减；
      - initial 中的 exact symbols 衰减慢一些，behavior terms 衰减快一些。

    指数衰减公式：
        w(t) = min_w + (start_w - min_w) * exp(-lambda * t)

    retrieval_step:
        当前 instance 内第几次检索。
        建议由 retrieval_tools / agent 层传入 retrieval_call_count。
    """
    t = max(0, int(retrieval_step))

    base_issue_weight = issue_min_weight + (
        issue_start_weight - issue_min_weight
    ) * math.exp(-issue_decay_lambda * t)

    # 不同 initial group 的保留强度。
    # exact symbol 通常长期有用；behavior/query 语义词后期更容易干扰。
    initial_exact = max(0.50, base_issue_weight * 1.10)
    initial_method_class = max(0.40, base_issue_weight * 0.95)
    initial_error = max(0.30, base_issue_weight * 0.85)
    initial_bm25 = max(0.30, base_issue_weight * 0.80)
    initial_behavior = max(0.20, base_issue_weight * 0.60)

    return {
        # 当前 agent 明确发出的检索 query，始终最高优先级之一
        "current_query": 1.20,

        # query_focus 来自当前 query，代表后续局部定位意图，不衰减
        "query_exact_symbols": 1.20,
        "query_method_class": 1.10,
        "query_error": 1.00,
        "query_bm25_queries": 1.00,
        "query_behavior": 0.90,

        # initial_issue_focus 来自原始 issue，随 retrieval_step 指数衰减
        "initial_exact_symbols": initial_exact,
        "initial_method_class": initial_method_class,
        "initial_error": initial_error,
        "initial_bm25_queries": initial_bm25,
        "initial_behavior": initial_behavior,
    }

def _make_exact_symbol_queries(focus: IssueFocus) -> List[str]:
    queries: List[str] = []

    exact = _dedup_clean(
        focus.exact_symbols
        + focus.file_hints
        + focus.parameter_hints
    )
    if exact:
        queries.append(" ".join(exact[:10]))

    if focus.file_hints:
        queries.extend(focus.file_hints[:5])

    return _dedup_clean(queries)


def _make_method_class_queries(focus: IssueFocus) -> List[str]:
    names = _dedup_clean(
        focus.class_hints
        + focus.function_hints
        + focus.method_hints
        + focus.parameter_hints
    )
    queries: List[str] = []
    if names:
        queries.append(" ".join(names[:10]))

    # 单独强化 method/class，因为它们对代码 BM25 很关键。
    for name in _dedup_clean(focus.class_hints + focus.method_hints)[:5]:
        queries.append(name)

    return _dedup_clean(queries)


def _make_behavior_queries(focus: IssueFocus) -> List[str]:
    queries: List[str] = []

    if focus.behavior_terms:
        queries.append(" ".join(focus.behavior_terms[:8]))

    if focus.raw_keywords:
        queries.append(" ".join(focus.raw_keywords[:10]))

    return _dedup_clean(queries)


def _make_error_queries(focus: IssueFocus) -> List[str]:
    if not focus.error_terms:
        return []
    return _dedup_clean([
        " ".join(focus.error_terms[:8]),
        *focus.error_terms[:5],
    ])


# ===========================================================================
# 顶层便捷函数：run_swebench_batch.py 可直接调用
# ===========================================================================

def ensure_issue_focus_for_instance(
    cache_dir: str,
    instance_id: str,
    issue_text: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    force: bool = False,
) -> IssueFocus:
    """
    在每个 instance 开始时调用一次。

    如果 cache/{instance_id}/issue_focus.json 已有 initial_issue_focus，
    且 force=False，则不会再次调用 LLM。
    """
    backend = UniAPIBackend(api_key=api_key, model=model) if (api_key or model) else None
    store = IssueFocusStore(
        cache_dir=cache_dir,
        instance_id=instance_id,
        backend=backend,
    )
    return store.ensure_initial_issue_focus(issue_text, force=force)


def update_query_focus_for_instance(
    cache_dir: str,
    query: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> IssueFocus:
    """
    后续 search_hybrid/search_bm25 如果希望把当前 query 也结构化存储，
    可以调用这个函数覆盖 query_focus。
    """
    backend = UniAPIBackend(api_key=api_key, model=model) if (api_key or model) else None
    store = IssueFocusStore(cache_dir=cache_dir, backend=backend)
    return store.update_query_focus(query, force=True)


# ===========================================================================
# 小工具
# ===========================================================================

def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        result = []
        for x in value:
            if x is None:
                continue
            result.append(str(x))
        return result
    return [str(value)]


def _dedup_clean(items: Sequence[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for x in items:
        if x is None:
            continue
        s = str(x).strip()
        if not s:
            continue
        # 去掉明显的 JSON/null 噪音
        if s.lower() in {"none", "null", "n/a", "unknown"}:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(s)
    return result


def _simple_keywords(text: str) -> List[str]:
    stopwords = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
        "is", "are", "was", "were", "be", "been", "being", "this", "that",
        "these", "those", "it", "its", "as", "by", "from", "at", "not",
        "should", "would", "could", "can", "cannot", "when", "while",
        "if", "then", "than", "into", "using", "use", "used",
    }

    tokens = re.findall(
        r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[\u4e00-\u9fff]+",
        text.lower(),
    )

    result = []
    for tok in tokens:
        if len(tok) < 2:
            continue
        if tok in stopwords:
            continue
        result.append(tok)

    return _dedup_clean(result)[:30]

def build_deepen_issue_query_from_cache(
    cache_dir: str,
    explicit_query: str = "",
    max_symbols: int = 12,
    max_terms: int = 12,
) -> str:
    """
    从 issue_focus.json 构造给 deepen_file 使用的 issue_query。

    这个 query 用于 method embedding 排序，不是 BM25 检索。
    因此优先使用自然语言 source_text，再补少量代码符号/行为词。

    优先级：
      1. explicit_query
      2. query_focus.source_text
      3. initial_issue_focus.source_text
      4. query_focus / initial_issue_focus 的关键字段拼接
      5. 空字符串
    """
    explicit_query = (explicit_query or "").strip()
    if explicit_query:
        return explicit_query

    store = IssueFocusStore(cache_dir=cache_dir)
    cache = store.load()

    initial = cache.initial_issue_focus
    query_focus = cache.query_focus

    parts: List[str] = []

    # 1. 优先使用最近一次 agent query 的原文
    if query_focus and query_focus.source_text.strip():
        parts.append(query_focus.source_text.strip())

    # 2. 再使用原始 issue 原文
    if initial and initial.source_text.strip():
        parts.append(initial.source_text.strip())

    # 3. 补充少量代码符号和行为词
    symbol_parts: List[str] = []
    term_parts: List[str] = []

    for focus in (query_focus, initial):
        if focus is None:
            continue

        symbol_parts.extend(focus.exact_symbols)
        symbol_parts.extend(focus.file_hints)
        symbol_parts.extend(focus.class_hints)
        symbol_parts.extend(focus.function_hints)
        symbol_parts.extend(focus.method_hints)
        symbol_parts.extend(focus.parameter_hints)

        term_parts.extend(focus.behavior_terms)
        term_parts.extend(focus.error_terms)
        term_parts.extend(focus.raw_keywords)

    symbols = _dedup_clean(symbol_parts)[:max_symbols]
    terms = _dedup_clean(term_parts)[:max_terms]

    if symbols:
        parts.append("Related code symbols: " + ", ".join(symbols))
    if terms:
        parts.append("Related behavior/error terms: " + ", ".join(terms))

    return "\n".join(_dedup_clean(parts)).strip()