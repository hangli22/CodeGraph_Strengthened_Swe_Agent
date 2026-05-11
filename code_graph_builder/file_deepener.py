"""
file_deepener.py — 单文件按需深化
===================================
对应方向二第二阶段：Agent 运行时按需深化

对指定文件做完整 AST 解析，补充方法级节点和调用/重写边，
将骨架图的该文件区域升级为完整解析状态。

增强版能力：
  1. deepen 时对当前文件内 METHOD 节点做完整 embedding
  2. 根据当前 issue query 找出最相关的 method
  3. 从相关 method 所属类出发，寻找代码图上的相邻类
  4. 对相邻类所在文件按需 deepen
  5. 基于 CALLS 边返回 method summary 列表，并说明 method 之间的调用关系
  6. CALLS 边从“纯名字匹配”升级为“调用形式识别 + 轻量解析 + 置信度/证据”
  7. method summary 改为“多数短摘要 + 少数完整 code_preview”

注意：
  - 这里不是全仓库调用链闭包。
  - 它只做“当前文件 + 相邻类文件”的局部扩展。
  - 因此返回的是局部调用链证据，不保证覆盖全仓库所有 caller。
  - CALLS 解析仍是轻量静态分析，不是完整 Python 类型系统。

  why relevant 可以考虑如何优化



硬伤一：deepen_file 输出仍然过长

两次 deepen_file 都触发了 Output too long：

tests/model_fields/test_charfield.py -> 16334 chars
django/db/models/fields/__init__.py -> 17514 chars

这会污染上下文，尤其是大文件 django/db/models/fields/__init__.py 一次深化新增 211 个方法节点，输出太大。

这说明 deepen_file 现在虽然能给 method summary，但对大文件仍然太“豪放”。建议继续压缩：

1. 新增方法列表最多显示 20 个，不要全量展开。
2. method_summaries 默认显示 5 个。
3. 大文件默认不展示 code_preview，除非 top similarity 很高。
4. 对 tests 文件默认只显示匹配到的 test 方法，不显示太多关系提示。

否则 agent 很容易被大量无关 method 名干扰。


这两个参数控制是否对邻居进行深化
expand_neighbor_classes=False
max_neighbor_files=0



"""

from __future__ import annotations

import ast
import os
import logging
import time
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Iterable, Any
from collections import defaultdict

import numpy as np

from .graph_schema import (
    CodeGraph, CodeNode, CodeEdge, NodeType, EdgeType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------

@dataclass
class CallEdgeInfo:
    """轻量 CALLS 解析结果。

    这些字段会尽量写入图边属性；如果当前 CodeGraph 不支持边属性，
    也会保存在 FileDeepener 的运行时缓存里，用于本次 method summary 展示。
    """

    src: str
    dst: str
    call_expr: str
    resolution_kind: str
    confidence: float
    evidence: str = ""
    is_high_confidence: bool = False

    def to_dict(self) -> dict:
        return {
            "src": self.src,
            "dst": self.dst,
            "call_expr": self.call_expr,
            "resolution_kind": self.resolution_kind,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "is_high_confidence": self.is_high_confidence,
        }


@dataclass
class MethodSummary:
    """与当前 issue 相关的方法摘要。

    similarity 是 issue_query embedding 与 method embedding 文本之间的余弦相似度。
    code_preview 只对最相关的少数方法保留；其他方法只返回短摘要，避免上下文膨胀。
    """

    node_id: str
    name: str
    qualified_name: str
    file: str
    start_line: int
    end_line: int
    similarity: float = 0.0

    parent_class_id: str = ""
    parent_class_name: str = ""

    calls: List[str] = field(default_factory=list)
    called_by: List[str] = field(default_factory=list)
    relation_notes: List[str] = field(default_factory=list)

    # 新增：更适合 agent 阅读的短摘要字段
    short_summary: str = "" # 这些如何生成?
    why_relevant: str = "" # 这里考虑要不要优化
    high_confidence_calls: List[str] = field(default_factory=list)
    call_edges: List[dict] = field(default_factory=list)
    unresolved_calls: List[str] = field(default_factory=list)
    has_full_preview: bool = False

    signature: str = ""
    docstring: str = ""
    code_preview: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "similarity": self.similarity,
            "parent_class_id": self.parent_class_id,
            "parent_class_name": self.parent_class_name,
            "calls": self.calls,
            "called_by": self.called_by,
            "relation_notes": self.relation_notes,
            "short_summary": self.short_summary,
            "why_relevant": self.why_relevant,
            "high_confidence_calls": self.high_confidence_calls,
            "call_edges": self.call_edges,
            "unresolved_calls": self.unresolved_calls,
            "has_full_preview": self.has_full_preview,
            "signature": self.signature,
            "docstring": self.docstring,
            "code_preview": self.code_preview,
        }


@dataclass
class DeepenResult:
    """深化操作的结果统计。"""

    file_rel:        str            = ""
    new_node_ids:    List[str]      = field(default_factory=list)

    # 新增：deepen 中已有但文本发生变化的节点。
    # 典型包括：
    #   - CLASS：骨架 class 节点被补充完整 code_text
    #   - FUNCTION：顶层函数被补充完整 code_text/signature/docstring
    #   - METHOD：如果重复/补充 deepen 已存在 method，也会更新
    updated_node_ids: List[str]    = field(default_factory=list)

    new_edge_count:  int            = 0
    call_edge_count: int            = 0
    method_count:    int            = 0
    imported_files:  List[str]      = field(default_factory=list)
    elapsed_ms:      float          = 0.0

    # deepen 会更新已有 CLASS/FUNCTION 节点的 code_text/signature/docstring，
    # 也会新增 METHOD 节点。因此默认认为检索文本发生变化。
    text_changed:    bool           = True

    method_summaries: List[MethodSummary] = field(default_factory=list)
    neighbor_deepened_files: List[str] = field(default_factory=list)
    relation_summary: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file_rel": self.file_rel,
            "new_node_ids": self.new_node_ids,
            "updated_node_ids": self.updated_node_ids,
            "new_edge_count": self.new_edge_count,
            "call_edge_count": self.call_edge_count,
            "method_count": self.method_count,
            "imported_files": self.imported_files,
            "elapsed_ms": self.elapsed_ms,
            "text_changed": self.text_changed,
            "method_summaries": [m.to_dict() for m in self.method_summaries],
            "neighbor_deepened_files": self.neighbor_deepened_files,
            "relation_summary": self.relation_summary,
        }


@dataclass
class _CallResolution:
    """内部使用：一次 ast.Call 被解析到的候选目标。"""

    callee_id: str
    call_expr: str
    resolution_kind: str
    confidence: float
    evidence: str


@dataclass
class _FunctionContext:
    """内部使用：当前函数/方法的轻量作用域信息。"""

    file_rel: str
    caller_id: str
    current_class_id: str = ""
    current_class_name: str = ""
    import_aliases: Dict[str, str] = field(default_factory=dict)
    from_import_aliases: Dict[str, str] = field(default_factory=dict)
    local_var_types: Dict[str, str] = field(default_factory=dict)
    self_attr_types: Dict[str, str] = field(default_factory=dict)
    param_types: Dict[str, str] = field(default_factory=dict)
    return_type: str = ""


@dataclass
class _CallIndexes:
    """内部使用：为了轻量解析 CALLS 构建的索引。"""

    name_index: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))
    file_function_index: Dict[Tuple[str, str], List[str]] = field(default_factory=lambda: defaultdict(list))
    class_name_index: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))
    method_index: Dict[Tuple[str, str], List[str]] = field(default_factory=lambda: defaultdict(list))
    function_return_types: Dict[Tuple[str, str], str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _rel_path(abs_path: str, repo_root: str) -> str:
    return os.path.relpath(abs_path, repo_root).replace("\\", "/")


def _extract_source(source_lines: List[str], node: ast.AST) -> str:
    start = node.lineno - 1
    end = node.end_lineno
    return "".join(source_lines[start:end])


def _collect_call_names(func_body: List[ast.stmt]) -> List[str]:
    """兼容旧逻辑：从函数体中收集所有被调用的函数名。

    新版 _build_calls 已不再依赖这个函数做主要连边；它保留在这里，
    方便外部代码或旧测试仍然可以调用。
    """
    names: Set[str] = set()

    class _Collector(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            self.generic_visit(node)

    collector = _Collector()
    for stmt in func_body:
        collector.visit(stmt)
    return sorted(names)


def _extract_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args_str = ast.unparse(node.args)
        ret_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"({args_str}){ret_str}"
    except Exception:
        return "()"


def _extract_docstring(body: list) -> str:
    if not body:
        return ""
    first = body[0]
    if isinstance(first, ast.Expr):
        val = first.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            return val.value.strip().split("\n")[0]
    return ""


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-12:
        return 0.0
    return float(np.dot(a, b) / denom)


def _safe_preview(text: str, max_chars: int = 500) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n..."


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def _attr_chain(node: ast.AST) -> str:
    """把 ast.Name / ast.Attribute / super().x 等尽量还原成 dotted string。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _attr_chain(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return f"{_attr_chain(node.func)}()"
    return _safe_unparse(node)


def _annotation_name(node: Optional[ast.AST]) -> str:
    """抽取类型注解里的简短类型名。

    Examples:
      User -> User
      Optional[User] -> User
      list[User] -> User
      module.User -> User
    """
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        # Optional[User] / list[User] / Union[A, B]：取第一个看起来像类型名的内部元素。
        inner = node.slice
        if isinstance(inner, ast.Tuple):
            for elt in inner.elts:
                name = _annotation_name(elt)
                if name:
                    return name
        return _annotation_name(inner)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split(".")[-1].strip("'\"")
    return ""


def _method_embedding_text(node: CodeNode, parent_class: Optional[CodeNode] = None) -> str:
    """
    构造 METHOD 的完整 embedding 文本。

    这里比骨架 embedding 更完整：
      - method qualified name
      - file
      - class name
      - signature
      - docstring
      - method code_text
    """
    parts: List[str] = []

    parts.append(f"method {node.qualified_name or node.name}")
    parts.append(f"file {node.file}")

    if parent_class is not None:
        parts.append(f"class {parent_class.name}")
        if parent_class.signature:
            parts.append(f"class_bases {parent_class.signature}")
        if parent_class.docstring:
            parts.append(f"class_docstring {parent_class.docstring}")

    if node.signature:
        parts.append(f"signature {node.signature}")
    if node.docstring:
        parts.append(f"docstring {node.docstring}")
    if node.code_text:
        parts.append("code")
        parts.append(node.code_text[:1600])

    return "\n".join(parts).strip()


def _append_unique(items: List[str], value: str) -> None:
    """向 list 追加唯一值，保持插入顺序。"""
    if value and value not in items:
        items.append(value)


def _extend_unique(items: List[str], values: Iterable[str]) -> None:
    """向 list 批量追加唯一值，保持插入顺序。"""
    for value in values:
        _append_unique(items, value)

# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class FileDeepener:
    """
    将骨架图中的某个文件升级为完整解析状态。

    Usage
    -----
    deepener = FileDeepener(graph, graph.repo_root, embedding_backend=backend)
    result = deepener.deepen(
        "requests/models.py",
        issue_query="Session redirect should preserve method",
    )
    print(result.method_summaries)
    """

    MAX_DEEPEN_FILES = 20

    # method summary：默认返回更多短摘要，只给最相关少数方法保留完整 code_preview。
    DEFAULT_SUMMARY_METHODS = 5
    DEFAULT_FULL_PREVIEW_METHODS = 0

    def __init__(
        self,
        graph: CodeGraph,
        repo_root: str,
        embedding_backend: Optional[object] = None,
    ):
        self.graph = graph
        self.repo_root = os.path.abspath(repo_root)

        # embedding_backend 采用鸭子类型：
        # 只要求它有 embed(text) 或 embed_batch(texts) 方法。
        self.embedding_backend = embedding_backend

        # 运行时 CALLS 边元数据缓存。
        # key = (src, dst)，value = CallEdgeInfo。
        # 如果图对象不支持边属性，summary 仍可从这里展示 resolution_kind/confidence/evidence。
        self._call_edge_info_by_pair: Dict[Tuple[str, str], CallEdgeInfo] = {}
        self._unresolved_calls_by_caller: Dict[str, List[str]] = defaultdict(list)

    def deepen(
        self,
        file_rel: str,
        issue_query: str = "",
        top_methods: int = 5,
        expand_neighbor_classes: bool = False,
        max_neighbor_files: int = 0,
        _visited_files: Optional[Set[str]] = None,
    ) -> DeepenResult:
        """
        对指定文件做完整 AST 解析，返回 DeepenResult。

        操作内容：
        1. 更新已有 CLASS/FUNCTION 节点的 code_text
        2. 创建 METHOD 节点 + PARENT_CHILD / SIBLING 边
        3. 分析调用关系，创建 CALLS 边
        4. 检测方法重写，创建 OVERRIDES 边
        5. 标记文件为 "full" 深度
        6. 若提供 issue_query：
           - 对当前文件内 METHOD 做 embedding
           - 找出与 issue 最相似的 method
           - 可选：深化相邻类所在文件
           - 返回 method summary 和局部调用关系
        """
        t0 = time.perf_counter()
        result = DeepenResult(file_rel=file_rel)
        graph = self.graph

        if _visited_files is None:
            _visited_files = set()
        if file_rel in _visited_files:
            return result
        _visited_files.add(file_rel)

        depth = graph.get_file_depth(file_rel)

        # 如果文件尚未 full，则先执行真正的 AST deepen。
        if depth != "full":
            self._deepen_ast_only(file_rel, result)

        # 一跳 import 信息仍然返回，方便外层工具展示。
        module_id = f"{file_rel}::MODULE"
        imported_ids = graph.successors(module_id, EdgeType.IMPORTS)
        for imp_id in imported_ids:
            imp_node = graph.get_node(imp_id)
            if imp_node and graph.get_file_depth(imp_node.file) == "skeleton":
                result.imported_files.append(imp_node.file)

        # 如果没有 issue_query，只做普通 deepen。
        if issue_query.strip():
            self._analyze_issue_related_methods(
                file_rel=file_rel,
                issue_query=issue_query,
                result=result,
                top_methods=top_methods,
                expand_neighbor_classes=expand_neighbor_classes,
                max_neighbor_files=max_neighbor_files,
                visited_files=_visited_files,
            )

        result.elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "深化完成 %s: +%d 节点 +%d 边 call_edges=%d summaries=%d (%.0fms)",
            file_rel,
            len(result.new_node_ids),
            result.new_edge_count,
            result.call_edge_count,
            len(result.method_summaries),
            result.elapsed_ms,
        )
        return result

    # ------------------------------------------------------------------
    # 原始 AST 深化流程
    # ------------------------------------------------------------------

    def _deepen_ast_only(self, file_rel: str, result: DeepenResult) -> None:
        graph = self.graph

        deepened = graph.get_deepened_files()
        if len(deepened) >= self.MAX_DEEPEN_FILES:
            logger.warning("已达到最大深化数 %d，跳过 %s", self.MAX_DEEPEN_FILES, file_rel)
            return

        abs_path = os.path.join(self.repo_root, file_rel)
        if not os.path.isfile(abs_path):
            logger.warning("文件不存在: %s", abs_path)
            return

        try:
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=abs_path)
        except SyntaxError as e:
            logger.warning("解析失败 %s: %s", file_rel, e)
            return

        source_lines = source.splitlines(keepends=True)

        # Step 1+2：更新已有 CLASS 节点 + 创建/更新 METHOD 节点
        self._extract_methods(file_rel, tree, source_lines, result)

        # Step 3：更新已有 FUNCTION 节点的 code_text/signature/docstring
        self._update_function_code(file_rel, tree, source_lines, result)

        # Step 4：CALLS 边
        self._build_calls(file_rel, tree, result)

        # Step 5：OVERRIDES 边
        self._build_overrides(result)

        # Step 6：标记深度
        graph.set_file_depth(file_rel, "full")

    # ------------------------------------------------------------------
    # 提取 METHOD 节点
    # ------------------------------------------------------------------

    def _extract_methods(
        self,
        file_rel: str,
        tree: ast.Module,
        source_lines: List[str],
        result: DeepenResult,
    ) -> None:
        graph = self.graph

        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            class_id = f"{file_rel}::{node.name}"
            if not graph.has_node(class_id):
                continue

            # 更新 CLASS 节点的 code_text 为完整源码
            full_code = _extract_source(source_lines, node)
            graph.update_node_attr(class_id, code_text=full_code)
            _append_unique(result.updated_node_ids, class_id)

            method_ids: List[str] = []
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue

                method_id = f"{file_rel}::{node.name}.{item.name}"
                signature = _extract_func_signature(item)
                docstring = _extract_docstring(item.body)
                code_text = _extract_source(source_lines, item)

                if graph.has_node(method_id):
                    # 已存在，更新完整信息
                    graph.update_node_attr(
                        method_id,
                        code_text=code_text,
                        signature=signature,
                        docstring=docstring,
                        start_line=item.lineno,
                        end_line=item.end_lineno or item.lineno,
                    )
                    _append_unique(result.updated_node_ids, method_id)
                    method_ids.append(method_id)
                    continue

                method_node = CodeNode(
                    id=method_id,
                    type=NodeType.METHOD,
                    name=item.name,
                    qualified_name=f"{node.name}.{item.name}",
                    file=file_rel,
                    start_line=item.lineno,
                    end_line=item.end_lineno or item.lineno,
                    code_text=code_text,
                    signature=signature,
                    docstring=docstring,
                )
                graph.add_node(method_node)
                _append_unique(result.new_node_ids, method_id)
                result.method_count += 1

                # PARENT_CHILD 边：CLASS -> METHOD
                if not graph.has_edge(class_id, method_id, EdgeType.PARENT_CHILD):
                    graph.add_edge(CodeEdge(
                        src=class_id,
                        dst=method_id,
                        relation_type=EdgeType.PARENT_CHILD,
                    ))
                    result.new_edge_count += 1

                method_ids.append(method_id)

            # SIBLING 边：同一类内相邻方法
            for i in range(len(method_ids) - 1):
                a, b = method_ids[i], method_ids[i + 1]
                if not graph.has_edge(a, b, EdgeType.SIBLING):
                    graph.add_edge(CodeEdge(src=a, dst=b, relation_type=EdgeType.SIBLING))
                    result.new_edge_count += 1

    def _update_function_code(
        self,
        file_rel: str,
        tree: ast.Module,
        source_lines: List[str],
        result: DeepenResult,
    ) -> None:
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_id = f"{file_rel}::{node.name}"
                if self.graph.has_node(func_id):
                    full_code = _extract_source(source_lines, node)
                    self.graph.update_node_attr(
                        func_id,
                        code_text=full_code,
                        signature=_extract_func_signature(node),
                        docstring=_extract_docstring(node.body),
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                    )
                    _append_unique(result.updated_node_ids, func_id)

    # ------------------------------------------------------------------
    # CALLS 边：调用形式识别 + 轻量解析
    # ------------------------------------------------------------------

    def _build_calls(
        self,
        file_rel: str,
        tree: ast.Module,
        result: DeepenResult,
    ) -> None:
        indexes = self._build_call_indexes(tree, file_rel)
        import_aliases, from_import_aliases = self._collect_import_aliases(tree)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            caller_id = self._find_func_node(file_rel, node)
            if caller_id is None:
                continue

            parent_class = self._get_parent_class(caller_id)
            ctx = _FunctionContext(
                file_rel=file_rel,
                caller_id=caller_id,
                current_class_id=parent_class.id if parent_class else "",
                current_class_name=parent_class.name if parent_class else "",
                import_aliases=import_aliases,
                from_import_aliases=from_import_aliases,
                return_type=_annotation_name(node.returns),
            )
            self._collect_function_type_hints(node, ctx, indexes)

            for call in self._iter_calls_in_body(node.body):
                resolutions = self._resolve_call(call, ctx, indexes)
                if resolutions:
                    for res in resolutions:
                        self._add_call_edge(
                            caller_id=caller_id,
                            callee_id=res.callee_id,
                            call_expr=res.call_expr,
                            resolution_kind=res.resolution_kind,
                            confidence=res.confidence,
                            evidence=res.evidence,
                            result=result,
                        )
                else:
                    self._remember_unresolved_call(caller_id, _safe_unparse(call))

    def _build_call_indexes(self, tree: ast.Module, file_rel: str) -> _CallIndexes:
        graph = self.graph
        indexes = _CallIndexes()

        for node in graph.iter_nodes():
            if node.type in (NodeType.FUNCTION, NodeType.METHOD, NodeType.CLASS):
                indexes.name_index[node.name].append(node.id)
            if node.type == NodeType.FUNCTION:
                indexes.file_function_index[(node.file, node.name)].append(node.id)
            if node.type == NodeType.CLASS:
                indexes.class_name_index[node.name].append(node.id)
            if node.type == NodeType.METHOD:
                parent_class = self._get_parent_class(node.id)
                if parent_class:
                    indexes.method_index[(parent_class.id, node.name)].append(node.id)

        # 当前文件 AST 中的返回值注解：用于 x = factory(); x.foo() 的轻量推断。
        for item in ast.walk(tree):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.returns is not None:
                ret = _annotation_name(item.returns)
                if ret:
                    indexes.function_return_types[(file_rel, item.name)] = ret

        return indexes

    def _collect_import_aliases(self, tree: ast.Module) -> Tuple[Dict[str, str], Dict[str, str]]:
        """收集 import alias 和 from import alias。

        import_aliases:
          import numpy as np -> {"np": "numpy"}
          import os.path -> {"os": "os"}

        from_import_aliases:
          from .utils import normalize as norm -> {"norm": ".utils.normalize"}
        """
        import_aliases: Dict[str, str] = {}
        from_import_aliases: Dict[str, str] = {}

        for stmt in ast.iter_child_nodes(tree):
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    local = alias.asname or alias.name.split(".")[0]
                    import_aliases[local] = alias.name
            elif isinstance(stmt, ast.ImportFrom):
                module_prefix = "." * stmt.level + (stmt.module or "")
                for alias in stmt.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    qname = f"{module_prefix}.{alias.name}" if module_prefix else alias.name
                    from_import_aliases[local] = qname

        return import_aliases, from_import_aliases

    def _collect_function_type_hints(
        self,
        func: ast.FunctionDef | ast.AsyncFunctionDef,
        ctx: _FunctionContext,
        indexes: _CallIndexes,
    ) -> None:
        """收集当前函数内部的轻量类型线索。

        覆盖范围：
          - 参数类型注解：def f(x: ClassName)
          - 变量类型注解：x: ClassName
          - 构造赋值：x = ClassName(...)
          - self 属性构造赋值：self.x = ClassName(...)
          - 返回值注解的工厂赋值：x = make_user(); make_user -> User
        """
        for arg in list(func.args.args) + list(func.args.kwonlyargs):
            typ = _annotation_name(arg.annotation)
            if typ:
                ctx.param_types[arg.arg] = typ
                ctx.local_var_types[arg.arg] = typ

        # *args / **kwargs 也可能带注解，但一般不用于 obj.method 推断。
        for arg in [func.args.vararg, func.args.kwarg]:
            if arg is None:
                continue
            typ = _annotation_name(arg.annotation)
            if typ:
                ctx.param_types[arg.arg] = typ
                ctx.local_var_types[arg.arg] = typ

        for stmt in ast.walk(func):
            if isinstance(stmt, ast.AnnAssign):
                typ = _annotation_name(stmt.annotation)
                if not typ:
                    continue
                if isinstance(stmt.target, ast.Name):
                    ctx.local_var_types[stmt.target.id] = typ
                elif self._is_self_attr(stmt.target):
                    attr = stmt.target.attr  # type: ignore[attr-defined]
                    ctx.self_attr_types[attr] = typ

            elif isinstance(stmt, ast.Assign):
                inferred = self._infer_expr_type(stmt.value, ctx, indexes)
                if not inferred:
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        ctx.local_var_types[target.id] = inferred
                    elif self._is_self_attr(target):
                        attr = target.attr  # type: ignore[attr-defined]
                        ctx.self_attr_types[attr] = inferred

    def _infer_expr_type(
        self,
        expr: ast.AST,
        ctx: _FunctionContext,
        indexes: _CallIndexes,
    ) -> str:
        # x = ClassName(...)
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Name):
                name = expr.func.id
                if name in indexes.class_name_index:
                    return name
                ret = indexes.function_return_types.get((ctx.file_rel, name), "")
                if ret:
                    return ret
            elif isinstance(expr.func, ast.Attribute):
                # module.ClassName(...) / imported.ClassName(...)
                if expr.func.attr in indexes.class_name_index:
                    return expr.func.attr
        return ""

    def _iter_calls_in_body(self, body: List[ast.stmt]) -> Iterable[ast.Call]:
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call):
                    yield node

    def _resolve_call(
        self,
        call: ast.Call,
        ctx: _FunctionContext,
        indexes: _CallIndexes,
    ) -> List[_CallResolution]:
        func = call.func
        call_expr = _safe_unparse(call)

        if isinstance(func, ast.Name):
            return self._resolve_name_call(func.id, call_expr, ctx, indexes)

        if isinstance(func, ast.Attribute):
            return self._resolve_attribute_call(func, call_expr, ctx, indexes)

        return []

    def _resolve_name_call(
        self,
        name: str,
        call_expr: str,
        ctx: _FunctionContext,
        indexes: _CallIndexes,
    ) -> List[_CallResolution]:
        # ClassName()：构造函数调用。优先连 __init__，没有 __init__ 时连 CLASS 节点。
        if name in indexes.class_name_index:
            return self._resolve_constructor_call(name, call_expr, ctx, indexes)

        # 同文件直接函数调用：foo() -> 当前文件 foo。
        same_file_funcs = indexes.file_function_index.get((ctx.file_rel, name), [])
        if same_file_funcs:
            return [
                _CallResolution(
                    callee_id=nid,
                    call_expr=call_expr,
                    resolution_kind="same_file_direct_function",
                    confidence=0.90,
                    evidence=f"direct call {name}() resolved to same-file function",
                )
                for nid in same_file_funcs
            ]

        # from import alias：from .utils import normalize; normalize(...)
        if name in ctx.from_import_aliases:
            candidates = self._find_imported_candidates(
                imported_qname=ctx.from_import_aliases[name],
                attr_name=name,
                indexes=indexes,
            )
            if candidates:
                return [
                    _CallResolution(
                        callee_id=nid,
                        call_expr=call_expr,
                        resolution_kind="from_import_alias",
                        confidence=0.85,
                        evidence=f"{name} imported from {ctx.from_import_aliases[name]}",
                    )
                    for nid in candidates[:3]
                ]
            self._remember_unresolved_call(ctx.caller_id, f"{call_expr} [from_import_alias external/unknown]")
            return []

        # import alias 本身被调用：rare，但可能是 imported callable alias。
        if name in ctx.import_aliases:
            self._remember_unresolved_call(
                ctx.caller_id,
                f"{call_expr} [import_alias {name} -> {ctx.import_aliases[name]} treated as external/unknown]",
            )
            return []

        # 参数/局部变量本身被调用：通常是 callback/factory，无法安全解析。
        if name in ctx.local_var_types:
            self._remember_unresolved_call(
                ctx.caller_id,
                f"{call_expr} [callable variable of inferred type {ctx.local_var_types[name]}]",
            )
            return []

        # 低置信 fallback：沿用旧逻辑，但弱化。优先同文件已经在前面处理；这里只取少量全图同名候选。
        candidates = [nid for nid in indexes.name_index.get(name, []) if nid != ctx.caller_id]
        candidates = [nid for nid in candidates if self._node_type_name(nid) in {"FUNCTION", "METHOD"}]
        if candidates:
            return [
                _CallResolution(
                    callee_id=nid,
                    call_expr=call_expr,
                    resolution_kind="global_name_fallback",
                    confidence=0.25,
                    evidence=f"fallback name match for {name}(); low confidence",
                )
                for nid in candidates[:3]
            ]

        return []

    def _resolve_attribute_call(
        self,
        func: ast.Attribute,
        call_expr: str,
        ctx: _FunctionContext,
        indexes: _CallIndexes,
    ) -> List[_CallResolution]:
        attr = func.attr
        base = func.value
        base_chain = _attr_chain(base)

        # self.foo()
        if isinstance(base, ast.Name) and base.id == "self" and ctx.current_class_id:
            resolved = self._resolve_method_on_class_chain(
                class_id=ctx.current_class_id,
                method_name=attr,
                include_parents=True,
                indexes=indexes,
            )
            if resolved:
                return [
                    _CallResolution(
                        callee_id=nid,
                        call_expr=call_expr,
                        resolution_kind="self_method",
                        confidence=1.00 if i == 0 else 0.92,
                        evidence=f"self.{attr} resolved from current class/ancestor {ctx.current_class_name}",
                    )
                    for i, nid in enumerate(resolved[:3])
                ]

        # cls.foo()
        if isinstance(base, ast.Name) and base.id == "cls" and ctx.current_class_id:
            resolved = self._resolve_method_on_class_chain(
                class_id=ctx.current_class_id,
                method_name=attr,
                include_parents=True,
                indexes=indexes,
            )
            if resolved:
                return [
                    _CallResolution(
                        callee_id=nid,
                        call_expr=call_expr,
                        resolution_kind="cls_method",
                        confidence=0.95 if i == 0 else 0.88,
                        evidence=f"cls.{attr} resolved from current class/ancestor {ctx.current_class_name}",
                    )
                    for i, nid in enumerate(resolved[:3])
                ]

        # super().foo()
        if self._is_super_call(base) and ctx.current_class_id:
            resolved = self._resolve_parent_method(
                class_id=ctx.current_class_id,
                method_name=attr,
                indexes=indexes,
            )
            if resolved:
                return [
                    _CallResolution(
                        callee_id=nid,
                        call_expr=call_expr,
                        resolution_kind="super_method",
                        confidence=0.95,
                        evidence=f"super().{attr} resolved through INHERITS from {ctx.current_class_name}",
                    )
                    for nid in resolved[:3]
                ]

        # self.x.foo()：如果 self.x = ClassName(...) 或 self.x: ClassName，可解析到 ClassName.foo。
        self_attr_name = self._extract_self_attr_name(base)
        if self_attr_name and self_attr_name in ctx.self_attr_types:
            typ = ctx.self_attr_types[self_attr_name]
            resolved = self._resolve_method_by_class_name(
                class_name=typ,
                method_name=attr,
                preferred_file=ctx.file_rel,
                indexes=indexes,
            )
            if resolved:
                return [
                    _CallResolution(
                        callee_id=nid,
                        call_expr=call_expr,
                        resolution_kind="self_attr_inferred_type",
                        confidence=0.80,
                        evidence=f"self.{self_attr_name} inferred as {typ}; resolved .{attr}",
                    )
                    for nid in resolved[:3]
                ]

        # obj.foo()：如果 obj = ClassName(...) 或 obj: ClassName，可解析到 ClassName.foo。
        if isinstance(base, ast.Name) and base.id in ctx.local_var_types:
            typ = ctx.local_var_types[base.id]
            resolved = self._resolve_method_by_class_name(
                class_name=typ,
                method_name=attr,
                preferred_file=ctx.file_rel,
                indexes=indexes,
            )
            if resolved:
                return [
                    _CallResolution(
                        callee_id=nid,
                        call_expr=call_expr,
                        resolution_kind="object_inferred_type",
                        confidence=0.78,
                        evidence=f"{base.id} inferred as {typ}; resolved .{attr}",
                    )
                    for nid in resolved[:3]
                ]

        # ClassName.foo()
        if isinstance(base, ast.Name) and base.id in indexes.class_name_index:
            resolved = self._resolve_method_by_class_name(
                class_name=base.id,
                method_name=attr,
                preferred_file=ctx.file_rel,
                indexes=indexes,
            )
            if resolved:
                return [
                    _CallResolution(
                        callee_id=nid,
                        call_expr=call_expr,
                        resolution_kind="class_method_or_static_method",
                        confidence=0.88,
                        evidence=f"{base.id}.{attr} resolved as class/static method candidate",
                    )
                    for nid in resolved[:3]
                ]

        # module.foo()：通过 import alias 解析。外部库调用不强行连到项目内部。
        if isinstance(base, ast.Name) and base.id in ctx.import_aliases:
            imported_module = ctx.import_aliases[base.id]
            candidates = self._find_imported_candidates(
                imported_qname=f"{imported_module}.{attr}",
                attr_name=attr,
                indexes=indexes,
            )
            if candidates:
                return [
                    _CallResolution(
                        callee_id=nid,
                        call_expr=call_expr,
                        resolution_kind="import_alias_attribute",
                        confidence=0.85,
                        evidence=f"{base.id} -> {imported_module}; resolved .{attr}",
                    )
                    for nid in candidates[:3]
                ]
            self._remember_unresolved_call(
                ctx.caller_id,
                f"{call_expr} [import_alias_attribute {base.id}->{imported_module}; external/unknown]",
            )
            return []

        # imported_symbol.foo()：from .models import User; User.objects 这类先做类名解析已经处理，剩下大多不安全。
        if isinstance(base, ast.Name) and base.id in ctx.from_import_aliases:
            imported_qname = ctx.from_import_aliases[base.id]
            candidates = self._find_imported_candidates(
                imported_qname=f"{imported_qname}.{attr}",
                attr_name=attr,
                indexes=indexes,
            )
            if candidates:
                return [
                    _CallResolution(
                        callee_id=nid,
                        call_expr=call_expr,
                        resolution_kind="from_import_alias_attribute",
                        confidence=0.78,
                        evidence=f"{base.id} imported from {imported_qname}; resolved .{attr}",
                    )
                    for nid in candidates[:3]
                ]

        # 低置信 fallback：只对同文件同名方法/函数连少量边。
        same_file_candidates = [
            nid for nid in indexes.name_index.get(attr, [])
            if nid != ctx.caller_id and self._node_file(nid) == ctx.file_rel
        ]
        same_file_candidates = [
            nid for nid in same_file_candidates
            if self._node_type_name(nid) in {"FUNCTION", "METHOD"}
        ]
        if same_file_candidates:
            return [
                _CallResolution(
                    callee_id=nid,
                    call_expr=call_expr,
                    resolution_kind="same_file_attribute_name_fallback",
                    confidence=0.35,
                    evidence=f"attribute call {base_chain}.{attr} fallback to same-file name match; low confidence",
                )
                for nid in same_file_candidates[:3]
            ]

        self._remember_unresolved_call(
            ctx.caller_id,
            f"{call_expr} [unresolved attribute call base={base_chain}]",
        )
        return []

    def _resolve_constructor_call(
        self,
        class_name: str,
        call_expr: str,
        ctx: _FunctionContext,
        indexes: _CallIndexes,
    ) -> List[_CallResolution]:
        class_ids = self._prefer_nodes_by_file(indexes.class_name_index.get(class_name, []), ctx.file_rel)
        resolutions: List[_CallResolution] = []

        for class_id in class_ids[:3]:
            init_methods = indexes.method_index.get((class_id, "__init__"), [])
            if init_methods:
                for mid in init_methods[:1]:
                    resolutions.append(_CallResolution(
                        callee_id=mid,
                        call_expr=call_expr,
                        resolution_kind="constructor_init",
                        confidence=0.90,
                        evidence=f"{class_name}() resolved to {class_name}.__init__",
                    ))
            else:
                # 没有 __init__ 节点时连 CLASS 节点，保留构造关系。
                resolutions.append(_CallResolution(
                    callee_id=class_id,
                    call_expr=call_expr,
                    resolution_kind="constructor_class",
                    confidence=0.75,
                    evidence=f"{class_name}() resolved to CLASS node; __init__ not available",
                ))

        return resolutions

    def _resolve_method_by_class_name(
        self,
        class_name: str,
        method_name: str,
        preferred_file: str,
        indexes: _CallIndexes,
    ) -> List[str]:
        class_ids = self._prefer_nodes_by_file(indexes.class_name_index.get(class_name, []), preferred_file)
        out: List[str] = []
        seen: Set[str] = set()
        for class_id in class_ids:
            for mid in self._resolve_method_on_class_chain(class_id, method_name, True, indexes):
                if mid not in seen:
                    seen.add(mid)
                    out.append(mid)
        return out

    def _resolve_method_on_class_chain(
        self,
        class_id: str,
        method_name: str,
        include_parents: bool,
        indexes: _CallIndexes,
    ) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()

        def add_methods(cid: str) -> None:
            for mid in indexes.method_index.get((cid, method_name), []):
                if mid not in seen:
                    seen.add(mid)
                    out.append(mid)

        add_methods(class_id)
        if include_parents:
            for parent_id in self._iter_parent_classes(class_id):
                add_methods(parent_id)
        return out

    def _resolve_parent_method(
        self,
        class_id: str,
        method_name: str,
        indexes: _CallIndexes,
    ) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()
        for parent_id in self._iter_parent_classes(class_id):
            for mid in indexes.method_index.get((parent_id, method_name), []):
                if mid not in seen:
                    seen.add(mid)
                    out.append(mid)
        return out

    def _iter_parent_classes(self, class_id: str) -> Iterable[str]:
        seen: Set[str] = set()
        frontier = list(self.graph.successors(class_id, EdgeType.INHERITS))
        while frontier:
            cid = frontier.pop(0)
            if cid in seen:
                continue
            seen.add(cid)
            yield cid
            frontier.extend(self.graph.successors(cid, EdgeType.INHERITS))

    def _find_imported_candidates(
        self,
        imported_qname: str,
        attr_name: str,
        indexes: _CallIndexes,
    ) -> List[str]:
        """根据 import qname 找项目内部候选。

        这是轻量启发式：优先匹配 qualified_name 后缀，其次匹配文件路径片段，
        最后才回退到同名候选。外部库通常不会命中项目内部文件，因此不会乱连。
        """
        q = imported_qname.strip(".")
        q_parts = [p for p in q.split(".") if p]
        candidates = [
            nid for nid in indexes.name_index.get(attr_name, [])
            if self._node_type_name(nid) in {"FUNCTION", "METHOD", "CLASS"}
        ]
        if not candidates:
            return []

        def score(nid: str) -> int:
            node = self.graph.get_node(nid)
            if node is None:
                return 0
            qual = (node.qualified_name or node.name or "").replace(":", ".")
            file_as_mod = (node.file or "").replace("/", ".").replace(".py", "")
            s = 0
            if q and qual.endswith(q):
                s += 5
            if q_parts and node.name == q_parts[-1]:
                s += 2
            if len(q_parts) >= 2 and q_parts[-2] in file_as_mod:
                s += 3
            if any(part and part in file_as_mod for part in q_parts[:-1]):
                s += 1
            return s

        scored = [(nid, score(nid)) for nid in candidates]
        scored = [(nid, s) for nid, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [nid for nid, _ in scored]

    def _add_call_edge(
        self,
        caller_id: str,
        callee_id: str,
        call_expr: str,
        resolution_kind: str,
        confidence: float,
        evidence: str,
        result: DeepenResult,
    ) -> None:
        if callee_id == caller_id:
            return

        info = CallEdgeInfo(
            src=caller_id,
            dst=callee_id,
            call_expr=call_expr,
            resolution_kind=resolution_kind,
            confidence=float(confidence),
            evidence=evidence,
            is_high_confidence=float(confidence) >= 0.75,
        )
        self._remember_call_edge_info(info)

        if not self.graph.has_edge(caller_id, callee_id, EdgeType.CALLS):
            self.graph.add_edge(CodeEdge(
                src=caller_id,
                dst=callee_id,
                relation_type=EdgeType.CALLS,
            ))
            result.new_edge_count += 1
            result.call_edge_count += 1

        self._try_attach_edge_metadata(info)

    def _remember_call_edge_info(self, info: CallEdgeInfo) -> None:
        key = (info.src, info.dst)
        old = self._call_edge_info_by_pair.get(key)
        if old is None or info.confidence > old.confidence:
            self._call_edge_info_by_pair[key] = info

    def _remember_unresolved_call(self, caller_id: str, note: str) -> None:
        if note and note not in self._unresolved_calls_by_caller[caller_id]:
            self._unresolved_calls_by_caller[caller_id].append(note)

    def _try_attach_edge_metadata(self, info: CallEdgeInfo) -> None:
        """尽量把 CALLS 元信息写进图边。

        不同版本 CodeGraph 的边属性 API 可能不同，所以这里做防御式降级：
        写不进去也不影响主流程，summary 仍可使用运行时缓存。
        """
        attrs = {
            "confidence": info.confidence,
            "resolution_kind": info.resolution_kind,
            "evidence": info.evidence,
            "call_expr": info.call_expr,
        }

        try:
            if hasattr(self.graph, "update_edge_attr"):
                self.graph.update_edge_attr(info.src, info.dst, EdgeType.CALLS, **attrs)
                return
        except Exception:
            pass

        # 兼容 graph 内部直接暴露 networkx 的常见命名。
        for attr_name in ("graph", "_graph", "nx_graph", "g"):
            try:
                nxg = getattr(self.graph, attr_name, None)
                if nxg is None or not hasattr(nxg, "has_edge"):
                    continue
                if not nxg.has_edge(info.src, info.dst):
                    continue
                data = nxg.get_edge_data(info.src, info.dst)
                if isinstance(data, dict):
                    # DiGraph: data 就是 attr dict；MultiDiGraph: data 是 key->attr dict。
                    if "relation_type" in data or "type" in data:
                        data.update(attrs)
                    else:
                        for edge_attrs in data.values():
                            if isinstance(edge_attrs, dict):
                                edge_attrs.update(attrs)
                return
            except Exception:
                continue

    def _is_super_call(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
        )

    def _is_self_attr(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )

    def _extract_self_attr_name(self, node: ast.AST) -> str:
        if self._is_self_attr(node):
            return node.attr  # type: ignore[attr-defined]
        return ""

    def _prefer_nodes_by_file(self, node_ids: List[str], preferred_file: str) -> List[str]:
        same = [nid for nid in node_ids if self._node_file(nid) == preferred_file]
        other = [nid for nid in node_ids if self._node_file(nid) != preferred_file]
        return same + other

    def _node_file(self, node_id: str) -> str:
        node = self.graph.get_node(node_id)
        return node.file if node else ""

    def _node_type_name(self, node_id: str) -> str:
        node = self.graph.get_node(node_id)
        if node is None:
            return ""
        return getattr(node.type, "name", str(node.type))

    def _find_func_node(
        self,
        file_rel: str,
        ast_func: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> Optional[str]:
        target_line = ast_func.lineno
        for node in self.graph.iter_nodes():
            if (
                node.file == file_rel
                and node.name == ast_func.name
                and node.start_line == target_line
                and node.type in (NodeType.FUNCTION, NodeType.METHOD)
            ):
                return node.id
        return None

    # ------------------------------------------------------------------
    # OVERRIDES 边
    # ------------------------------------------------------------------

    def _build_overrides(self, result: DeepenResult) -> None:
        graph = self.graph

        for nid in result.new_node_ids:
            node = graph.get_node(nid)
            if node is None or node.type != NodeType.METHOD:
                continue

            parent_classes = graph.predecessors(nid, EdgeType.PARENT_CHILD)
            for class_id in parent_classes:
                super_classes = graph.successors(class_id, EdgeType.INHERITS)
                for super_id in super_classes:
                    super_methods = graph.successors(super_id, EdgeType.PARENT_CHILD)
                    for sm_id in super_methods:
                        sm_node = graph.get_node(sm_id)
                        if sm_node and sm_node.name == node.name:
                            if not graph.has_edge(nid, sm_id, EdgeType.OVERRIDES):
                                graph.add_edge(CodeEdge(
                                    src=nid,
                                    dst=sm_id,
                                    relation_type=EdgeType.OVERRIDES,
                                ))
                                result.new_edge_count += 1

    # ------------------------------------------------------------------
    # Issue 相关 METHOD 分析
    # ------------------------------------------------------------------

    def _analyze_issue_related_methods(
        self,
        file_rel: str,
        issue_query: str,
        result: DeepenResult,
        top_methods: int,
        expand_neighbor_classes: bool,
        max_neighbor_files: int,
        visited_files: Set[str],
    ) -> None:
        """
        对当前文件内 METHOD 做 embedding 相似度排序，
        然后可选深化相邻类所在文件，
        最后生成 method summary。

        新版 summary 策略：
          - 返回更多 method 的短摘要，默认至少 12 个。
          - 只对最相关的 2-3 个 method 返回完整 code_preview。
        """
        summary_method_count = max(top_methods, self.DEFAULT_SUMMARY_METHODS)
        full_preview_count = min(self.DEFAULT_FULL_PREVIEW_METHODS, max(1, top_methods))

        method_scores = self._rank_methods_by_embedding(
            file_rel=file_rel,
            issue_query=issue_query,
            top_k=summary_method_count,
        )

        if not method_scores:
            logger.info("文件 %s 未找到可用于 issue embedding 的 METHOD", file_rel)
            return

        selected_method_ids = [nid for nid, _ in method_scores]
        expansion_seed_ids = selected_method_ids[:max(1, top_methods)]

        if expand_neighbor_classes:
            neighbor_files = self._find_neighbor_class_files_for_methods(
                expansion_seed_ids,
                limit=max_neighbor_files,
            )

            for nf in neighbor_files:
                if nf == file_rel:
                    continue
                if nf in visited_files:
                    continue
                if graph_depth_is_full(self.graph, nf):
                    continue
                if len(self.graph.get_deepened_files()) >= self.MAX_DEEPEN_FILES:
                    break

                logger.info("因相关 method 的相邻类，额外深化文件: %s", nf)
                sub_result = self.deepen(
                    nf,
                    issue_query="",
                    top_methods=0,
                    expand_neighbor_classes=False,
                    max_neighbor_files=0,
                    _visited_files=visited_files,
                )
                if sub_result.method_count or sub_result.new_node_ids or sub_result.updated_node_ids:
                    _append_unique(result.neighbor_deepened_files, nf)

                    # 合并子 deepen 的节点变化，保证外层 retrieval_tools 能统一更新索引。
                    _extend_unique(result.new_node_ids, sub_result.new_node_ids)
                    _extend_unique(result.updated_node_ids, sub_result.updated_node_ids)

                    result.method_count += sub_result.method_count
                    result.new_edge_count += sub_result.new_edge_count
                    result.call_edge_count += sub_result.call_edge_count

                    # 子 deepen 的 imported files 也合并，方便输出进一步深化提示。
                    _extend_unique(result.imported_files, sub_result.imported_files)

        # 邻接类文件 deepen 后，CALLS 边可能增加。
        # 这里重新生成 summary，让关系信息包含刚刚扩展出来的边。
        summaries = self._build_method_summaries(
            selected_method_ids=selected_method_ids,
            scores=dict(method_scores),
            full_preview_count=full_preview_count,
        )

        result.method_summaries = summaries
        result.relation_summary = self._build_relation_summary(summaries)

    def _rank_methods_by_embedding(
        self,
        file_rel: str,
        issue_query: str,
        top_k: int = 5,
    ) -> List[Tuple[str, float]]:
        """
        对当前文件 METHOD 做完整 embedding，并按与 issue_query 的余弦相似度排序。

        如果没有 embedding_backend，则返回空。
        """
        if top_k <= 0:
            return []

        if self.embedding_backend is None:
            logger.warning(
                "未提供 embedding_backend，无法对 METHOD 做 issue 相似度排序: %s",
                file_rel,
            )
            return []

        method_nodes = [
            node for node in self.graph.iter_nodes(NodeType.METHOD)
            if node.file == file_rel and node.code_text.strip()
        ]
        if not method_nodes:
            return []

        texts: List[str] = []
        method_ids: List[str] = []

        for method in method_nodes:
            parent_class = self._get_parent_class(method.id)
            text = _method_embedding_text(method, parent_class)
            if not text:
                continue
            method_ids.append(method.id)
            texts.append(text)

        if not texts:
            return []

        try:
            query_vec = self._embed_one(issue_query)
            method_vecs = self._embed_many(texts)
        except Exception as e:
            logger.warning("METHOD embedding 排序失败 %s: %s", file_rel, e)
            return []

        scores: List[Tuple[str, float]] = []
        for nid, vec in zip(method_ids, method_vecs):
            sim = _cosine_sim(query_vec, vec)
            if math.isnan(sim):
                sim = 0.0
            scores.append((nid, sim))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def _embed_one(self, text: str) -> np.ndarray:
        backend = self.embedding_backend
        if backend is None:
            raise RuntimeError("embedding_backend is None")

        if hasattr(backend, "embed"):
            return np.asarray(backend.embed(text), dtype=np.float32)

        if hasattr(backend, "embed_batch"):
            arr = backend.embed_batch([text])
            return np.asarray(arr[0], dtype=np.float32)

        raise TypeError("embedding_backend must provide embed() or embed_batch()")

    def _embed_many(self, texts: List[str]) -> np.ndarray:
        backend = self.embedding_backend
        if backend is None:
            raise RuntimeError("embedding_backend is None")

        if hasattr(backend, "embed_batch"):
            return np.asarray(backend.embed_batch(texts), dtype=np.float32)

        if hasattr(backend, "embed"):
            return np.vstack([
                np.asarray(backend.embed(t), dtype=np.float32)
                for t in texts
            ])

        raise TypeError("embedding_backend must provide embed() or embed_batch()")

    # ------------------------------------------------------------------
    # 相邻类与局部扩展
    # ------------------------------------------------------------------

    def _find_neighbor_class_files_for_methods(
        self,
        method_ids: List[str],
        limit: int = 3,
    ) -> List[str]:
        """
        从相关 method 所属类出发，寻找代码图上的相邻类文件。

        当前定义的相邻类：
          1. 父类：class --INHERITS--> parent
          2. 子类：child --INHERITS--> class
          3. 同文件其他 CLASS
          4. 调用边连到的 METHOD 所属类
          5. 调用当前 METHOD 的 METHOD 所属类
        """
        graph = self.graph
        files: List[str] = []
        seen_files: Set[str] = set()

        def add_file_from_node_id(nid: str) -> None:
            node = graph.get_node(nid)
            if node is None:
                return
            if node.file and node.file not in seen_files:
                seen_files.add(node.file)
                files.append(node.file)

        for mid in method_ids:
            method = graph.get_node(mid)
            if method is None:
                continue

            parent_class = self._get_parent_class(mid)
            if parent_class is None:
                continue

            class_id = parent_class.id

            # 父类
            for pid in graph.successors(class_id, EdgeType.INHERITS):
                add_file_from_node_id(pid)

            # 子类
            for cid in graph.predecessors(class_id, EdgeType.INHERITS):
                add_file_from_node_id(cid)

            # 同文件其他类
            for node in graph.iter_nodes(NodeType.CLASS):
                if node.file == parent_class.file and node.id != class_id:
                    add_file_from_node_id(node.id)

            # 当前 method 调用的 method 所属类
            for callee_id in graph.successors(mid, EdgeType.CALLS):
                callee_class = self._get_parent_class(callee_id)
                if callee_class:
                    add_file_from_node_id(callee_class.id)

            # 调用当前 method 的 method 所属类
            for caller_id in graph.predecessors(mid, EdgeType.CALLS):
                caller_class = self._get_parent_class(caller_id)
                if caller_class:
                    add_file_from_node_id(caller_class.id)

        return files[:max(0, limit)]

    def _get_parent_class(self, method_id: str) -> Optional[CodeNode]:
        parents = self.graph.predecessors(method_id, EdgeType.PARENT_CHILD)
        for pid in parents:
            node = self.graph.get_node(pid)
            if node and node.type == NodeType.CLASS:
                return node
        return None

    # ------------------------------------------------------------------
    # Method summary：多数短摘要 + 少数完整 preview
    # ------------------------------------------------------------------

    def _build_method_summaries(
        self,
        selected_method_ids: List[str],
        scores: Dict[str, float],
        full_preview_count: int = 0,
    ) -> List[MethodSummary]:
        graph = self.graph
        selected_set = set(selected_method_ids)
        full_preview_set = set(selected_method_ids[:max(0, full_preview_count)])
        summaries: List[MethodSummary] = []

        # 为了描述关系，不只看 selected 之间的边，也看一跳 caller/callee。
        for mid in selected_method_ids:
            node = graph.get_node(mid)
            if node is None or node.type != NodeType.METHOD:
                continue

            parent_class = self._get_parent_class(mid)

            calls = graph.successors(mid, EdgeType.CALLS)
            called_by = graph.predecessors(mid, EdgeType.CALLS)

            calls_named = [self._format_node_ref(nid) for nid in calls]
            called_by_named = [self._format_node_ref(nid) for nid in called_by]

            outgoing_infos = self._get_call_infos(mid, calls)
            incoming_infos = self._get_incoming_call_infos(called_by, mid)

            high_conf = [
                self._format_call_info(info)
                for info in outgoing_infos
                if info.confidence >= 0.75
            ]

            relation_notes: List[str] = []

            for info in outgoing_infos:
                if info.dst in selected_set:
                    relation_notes.append(self._format_call_info(info))

            for info in incoming_infos:
                if info.src in selected_set:
                    relation_notes.append(self._format_call_info(info))

            similarity = float(scores.get(mid, 0.0))
            why_relevant = self._build_why_relevant(node, similarity)
            short_summary = self._build_short_method_summary(
                node=node,
                parent_class=parent_class,
                similarity=similarity,
                high_confidence_calls=high_conf,
            )
            has_full_preview = mid in full_preview_set

            summary = MethodSummary(
                node_id=mid,
                name=node.name,
                qualified_name=node.qualified_name,
                file=node.file,
                start_line=node.start_line,
                end_line=node.end_line,
                similarity=similarity,
                parent_class_id=parent_class.id if parent_class else "",
                parent_class_name=parent_class.name if parent_class else "",
                calls=calls_named,
                called_by=called_by_named,
                relation_notes=sorted(set(relation_notes)),
                short_summary=short_summary,
                why_relevant=why_relevant,
                high_confidence_calls=high_conf[:8],
                call_edges=[info.to_dict() for info in sorted(outgoing_infos, key=lambda x: x.confidence, reverse=True)[:12]],
                unresolved_calls=self._unresolved_calls_by_caller.get(mid, [])[:8],
                has_full_preview=has_full_preview,
                signature=node.signature,
                docstring=node.docstring,
                code_preview=_safe_preview(node.code_text, max_chars=700) if has_full_preview else "",
            )
            summaries.append(summary)

        summaries.sort(key=lambda m: m.similarity, reverse=True)
        return summaries

    def _get_call_infos(self, src: str, dsts: List[str]) -> List[CallEdgeInfo]:
        infos: List[CallEdgeInfo] = []
        for dst in dsts:
            info = self._call_edge_info_by_pair.get((src, dst))
            if info is None:
                info = CallEdgeInfo(
                    src=src,
                    dst=dst,
                    call_expr="",
                    resolution_kind="existing_call_edge_unknown_resolution",
                    confidence=0.50,
                    evidence="CALLS edge existed before this deepen run or graph metadata was unavailable",
                    is_high_confidence=False,
                )
            infos.append(info)
        return infos

    def _get_incoming_call_infos(self, srcs: List[str], dst: str) -> List[CallEdgeInfo]:
        infos: List[CallEdgeInfo] = []
        for src in srcs:
            info = self._call_edge_info_by_pair.get((src, dst))
            if info is None:
                info = CallEdgeInfo(
                    src=src,
                    dst=dst,
                    call_expr="",
                    resolution_kind="existing_call_edge_unknown_resolution",
                    confidence=0.50,
                    evidence="CALLS edge existed before this deepen run or graph metadata was unavailable",
                    is_high_confidence=False,
                )
            infos.append(info)
        return infos

    def _format_call_info(self, info: CallEdgeInfo) -> str:
        src = self._format_node_ref(info.src)
        dst = self._format_node_ref(info.dst)
        expr = f" via `{info.call_expr}`" if info.call_expr else ""
        return (
            f"{src} -> {dst}{expr} "
            f"[{info.resolution_kind}, conf={info.confidence:.2f}; {info.evidence}]"
        )

    def _build_why_relevant(self, node: CodeNode, similarity: float) -> str:
        parts = [f"issue_embedding_similarity={similarity:.3f}"]
        if node.name:
            parts.append(f"method_name={node.name}")
        if node.signature:
            parts.append(f"signature_present")
        if node.docstring:
            parts.append(f"docstring_present")
        return "; ".join(parts)

    def _build_short_method_summary(
        self,
        node: CodeNode,
        parent_class: Optional[CodeNode],
        similarity: float,
        high_confidence_calls: List[str],
    ) -> str:
        class_part = f" class={parent_class.name}" if parent_class else ""
        sig = f" signature={node.signature}" if node.signature else ""
        calls = ""
        if high_confidence_calls:
            calls = f" high_conf_calls={len(high_confidence_calls)}"
        return (
            f"{node.qualified_name or node.name} "
            f"({node.file}:{node.start_line}-{node.end_line})"
            f" sim={similarity:.3f}{class_part}{sig}{calls}"
        )

    def _build_relation_summary(
        self,
        summaries: List[MethodSummary],
    ) -> List[str]:
        if not summaries:
            return []

        notes: List[str] = []
        selected_names = [
            f"{s.qualified_name}({s.file}:{s.start_line}-{s.end_line}, sim={s.similarity:.3f})"
            for s in summaries[:8]
        ]
        notes.append(
            "Issue-relevant method map: " + "; ".join(selected_names)
        )

        high_conf_edges: List[str] = []
        selected_ids = {s.node_id for s in summaries}
        for s in summaries:
            for edge in s.call_edges:
                if edge.get("confidence", 0.0) >= 0.75:
                    dst = edge.get("dst", "")
                    # 优先展示 selected 内部关系；如果不足，也展示高置信一跳关系。
                    if dst in selected_ids or len(high_conf_edges) < 8:
                        high_conf_edges.append(
                            f"{self._format_node_ref(edge.get('src', ''))} -> "
                            f"{self._format_node_ref(dst)} via `{edge.get('call_expr', '')}` "
                            f"[{edge.get('resolution_kind')}, conf={edge.get('confidence'):.2f}]"
                        )

        if high_conf_edges:
            notes.append(
                "High-confidence local call evidence: " + " | ".join(sorted(set(high_conf_edges))[:10])
            )
        else:
            fallback_edges: List[str] = []
            for s in summaries[:5]:
                if s.calls:
                    fallback_edges.append(
                        f"{s.qualified_name} has outgoing CALLS candidates: {', '.join(s.calls[:4])}"
                    )
                if s.called_by:
                    fallback_edges.append(
                        f"{s.qualified_name} has incoming CALLS candidates: {', '.join(s.called_by[:4])}"
                    )
            if fallback_edges:
                notes.append(
                    "Low/unknown-confidence local call candidates: " + " | ".join(fallback_edges[:8])
                )

        unresolved: List[str] = []
        for s in summaries[:8]:
            for item in s.unresolved_calls[:3]:
                unresolved.append(f"{s.qualified_name}: {item}")
        if unresolved:
            notes.append(
                "Unresolved calls worth checking manually: " + " | ".join(unresolved[:10])
            )

        # 给 agent 的编辑提示：把结构信号转成操作建议。
        notes.append(
            "Agent hint: before editing a selected method, inspect high-confidence callees/callers, "
            "sibling methods in the same class, and overridden/parent methods when self/super calls appear. "
            "Treat low-confidence/global-name fallback edges as hints, not facts."
        )

        return notes

    def _format_node_ref(self, node_id: str) -> str:
        node = self.graph.get_node(node_id)
        if node is None:
            return node_id
        if node.qualified_name:
            return f"{node.qualified_name} ({node.file}:{node.start_line}-{node.end_line})"
        return f"{node.name} ({node.file}:{node.start_line}-{node.end_line})"


def graph_depth_is_full(graph: CodeGraph, file_rel: str) -> bool:
    return graph.get_file_depth(file_rel) == "full"
