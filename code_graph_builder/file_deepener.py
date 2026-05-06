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

注意：
  - 这里不是全仓库调用链闭包。
  - 它只做“当前文件 + 相邻类文件”的局部扩展。
  - 因此返回的是局部调用链证据，不保证覆盖全仓库所有 caller。
"""

from __future__ import annotations

import ast
import os
import logging
import time
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Iterable
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
class MethodSummary:
    """与当前 issue 相关的方法摘要。"""

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
            "signature": self.signature,
            "docstring": self.docstring,
            "code_preview": self.code_preview,
        }


@dataclass
class DeepenResult:
    """深化操作的结果统计。"""

    file_rel:        str            = ""
    new_node_ids:    List[str]      = field(default_factory=list)
    new_edge_count:  int            = 0
    call_edge_count: int            = 0
    method_count:    int            = 0
    imported_files:  List[str]      = field(default_factory=list)
    elapsed_ms:      float          = 0.0

    # 新增：issue 相关 method 摘要
    method_summaries: List[MethodSummary] = field(default_factory=list)

    # 新增：因为相邻类被额外 deepen 的文件
    neighbor_deepened_files: List[str] = field(default_factory=list)

    # 新增：局部调用链关系说明
    relation_summary: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file_rel": self.file_rel,
            "new_node_ids": self.new_node_ids,
            "new_edge_count": self.new_edge_count,
            "call_edge_count": self.call_edge_count,
            "method_count": self.method_count,
            "imported_files": self.imported_files,
            "elapsed_ms": self.elapsed_ms,
            "method_summaries": [m.to_dict() for m in self.method_summaries],
            "neighbor_deepened_files": self.neighbor_deepened_files,
            "relation_summary": self.relation_summary,
        }


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
    """从函数体中收集所有被调用的函数名。"""
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

    def deepen(
        self,
        file_rel: str,
        issue_query: str = "",
        top_methods: int = 5,
        expand_neighbor_classes: bool = True,
        max_neighbor_files: int = 3,
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

        # Step 1+2：更新已有节点 + 创建 METHOD 节点
        self._extract_methods(file_rel, tree, source_lines, result)

        # Step 3：更新已有 FUNCTION 节点的 code_text
        self._update_function_code(file_rel, tree, source_lines)

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
                result.new_node_ids.append(method_id)
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

    # ------------------------------------------------------------------
    # 更新 FUNCTION 节点的 code_text
    # ------------------------------------------------------------------

    def _update_function_code(
        self,
        file_rel: str,
        tree: ast.Module,
        source_lines: List[str],
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

    # ------------------------------------------------------------------
    # CALLS 边
    # ------------------------------------------------------------------

    def _build_calls(
        self,
        file_rel: str,
        tree: ast.Module,
        result: DeepenResult,
    ) -> None:
        graph = self.graph

        # 全局名称索引：函数名/方法名 -> 节点候选
        # 注意：这是轻量解析，不做完整 Python name binding。
        name_index: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for node in graph.iter_nodes():
            if node.type in (NodeType.FUNCTION, NodeType.METHOD):
                name_index[node.name].append((node.id, node.file))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            caller_id = self._find_func_node(file_rel, node)
            if caller_id is None:
                continue

            callee_names = _collect_call_names(node.body)
            for cname in callee_names:
                if cname not in name_index:
                    continue

                candidates = name_index[cname]

                # 优先同文件候选，避免跨文件同名方法过度连边。
                same_file = [nid for nid, f in candidates if f == file_rel]
                chosen = same_file if same_file else [nid for nid, _ in candidates]

                for callee_id in chosen:
                    if callee_id == caller_id:
                        continue
                    if not graph.has_edge(caller_id, callee_id, EdgeType.CALLS):
                        graph.add_edge(CodeEdge(
                            src=caller_id,
                            dst=callee_id,
                            relation_type=EdgeType.CALLS,
                        ))
                        result.new_edge_count += 1
                        result.call_edge_count += 1

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
        """
        method_scores = self._rank_methods_by_embedding(
            file_rel=file_rel,
            issue_query=issue_query,
            top_k=top_methods,
        )

        if not method_scores:
            logger.info("文件 %s 未找到可用于 issue embedding 的 METHOD", file_rel)
            return

        selected_method_ids = [nid for nid, _ in method_scores]

        if expand_neighbor_classes:
            neighbor_files = self._find_neighbor_class_files_for_methods(
                selected_method_ids,
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
                if sub_result.method_count or sub_result.new_node_ids:
                    result.neighbor_deepened_files.append(nf)
                    result.new_edge_count += sub_result.new_edge_count
                    result.call_edge_count += sub_result.call_edge_count

        # 邻接类文件 deepen 后，CALLS 边可能增加。
        # 这里重新生成 summary，让关系信息包含刚刚扩展出来的边。
        summaries = self._build_method_summaries(
            selected_method_ids=selected_method_ids,
            scores=dict(method_scores),
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
    # Method summary
    # ------------------------------------------------------------------

    def _build_method_summaries(
        self,
        selected_method_ids: List[str],
        scores: Dict[str, float],
    ) -> List[MethodSummary]:
        graph = self.graph
        selected_set = set(selected_method_ids)
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

            relation_notes: List[str] = []

            for callee_id in calls:
                if callee_id in selected_set:
                    relation_notes.append(
                        f"{self._format_node_ref(mid)} calls {self._format_node_ref(callee_id)}"
                    )

            for caller_id in called_by:
                if caller_id in selected_set:
                    relation_notes.append(
                        f"{self._format_node_ref(caller_id)} calls {self._format_node_ref(mid)}"
                    )

            summary = MethodSummary(
                node_id=mid,
                name=node.name,
                qualified_name=node.qualified_name,
                file=node.file,
                start_line=node.start_line,
                end_line=node.end_line,
                similarity=float(scores.get(mid, 0.0)),
                parent_class_id=parent_class.id if parent_class else "",
                parent_class_name=parent_class.name if parent_class else "",
                calls=calls_named,
                called_by=called_by_named,
                relation_notes=sorted(set(relation_notes)),
                signature=node.signature,
                docstring=node.docstring,
                code_preview=_safe_preview(node.code_text, max_chars=500),
            )
            summaries.append(summary)

        summaries.sort(key=lambda m: m.similarity, reverse=True)
        return summaries

    def _build_relation_summary(
        self,
        summaries: List[MethodSummary],
    ) -> List[str]:
        notes: Set[str] = set()

        for s in summaries:
            for note in s.relation_notes:
                notes.add(note)

        # 如果 selected methods 之间没有直接 CALLS 边，也给出一跳信息。
        if not notes:
            for s in summaries:
                if s.calls:
                    notes.add(
                        f"{s.qualified_name} calls: {', '.join(s.calls[:5])}"
                    )
                if s.called_by:
                    notes.add(
                        f"{s.qualified_name} is called by: {', '.join(s.called_by[:5])}"
                    )

        return sorted(notes)

    def _format_node_ref(self, node_id: str) -> str:
        node = self.graph.get_node(node_id)
        if node is None:
            return node_id
        if node.qualified_name:
            return f"{node.qualified_name} ({node.file}:{node.start_line}-{node.end_line})"
        return f"{node.name} ({node.file}:{node.start_line}-{node.end_line})"


def graph_depth_is_full(graph: CodeGraph, file_rel: str) -> bool:
    return graph.get_file_depth(file_rel) == "full"