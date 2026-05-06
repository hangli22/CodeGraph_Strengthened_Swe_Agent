"""
skeleton_builder.py — 骨架图构建器
====================================
对应方向二第一阶段：Prebuild 构建骨架图

只解析每个文件 AST 的 module body 第一层：
  - MODULE 节点 + 模块 docstring
  - import / from-import → IMPORTS 边
  - 顶层 class：类名、基类、docstring、方法名列表 → CLASS 节点 + CONTAINS 边
  - 顶层 function：函数名、签名、docstring 首行 → FUNCTION 节点 + CONTAINS 边
  - 类继承 → INHERITS 边

不做的事：不进入函数/方法体，不提取调用关系，不创建 METHOD 节点。
"""

from __future__ import annotations

import ast
import os
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .graph_schema import (
    CodeGraph, CodeNode, CodeEdge, NodeType, EdgeType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _rel_path(abs_path: str, repo_root: str) -> str:
    return os.path.relpath(abs_path, repo_root).replace("\\", "/")


def _module_id(file_rel: str) -> str:
    return f"{file_rel}::MODULE"


def _extract_docstring(body: list) -> str:
    """从 class/function/module body 提取 docstring 首行。"""
    if not body:
        return ""
    first = body[0]
    if isinstance(first, ast.Expr):
        val = first.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            return val.value.strip().split("\n")[0]
    return ""


def _extract_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """提取函数签名字符串，如 '(self, url: str) -> Response'。"""
    try:
        args_str = ast.unparse(node.args)
        ret_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"({args_str}){ret_str}"
    except Exception:
        return "()"


def _extract_class_bases_str(node: ast.ClassDef) -> str:
    """提取类基类列表字符串，如 '(Base1, Base2)'。"""
    try:
        bases = [ast.unparse(b) for b in node.bases]
        return f"({', '.join(bases)})" if bases else ""
    except Exception:
        return ""


def _extract_method_names(class_node: ast.ClassDef) -> List[str]:
    """提取类体中第一层所有方法名（不递归进入方法体）。"""
    names = []
    for item in class_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(item.name)
    return names


def _base_name_simple(expr: ast.expr) -> Optional[str]:
    """从 AST 基类表达式提取最末级名称。"""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class SkeletonBuilder:
    """
    骨架图构建器：只解析文件顶层结构，不进入函数体。

    Usage
    -----
    builder = SkeletonBuilder(repo_root="/path/to/repo")
    graph = CodeGraph(repo_root="/path/to/repo")
    builder.build(graph)
    """

    def __init__(self, repo_root: str, exclude_dirs: Optional[Set[str]] = None):
        self.repo_root = os.path.abspath(repo_root)
        self.exclude_dirs: Set[str] = exclude_dirs or {
            ".git", "__pycache__", ".tox", ".venv", "venv", "env",
            "node_modules", "dist", "build", ".eggs",
        }
        # 暂存 INHERITS 待解析信息：{child_node_id: (file_rel, [base_name, ...])}
        self._pending_inherits: Dict[str, Tuple[str, List[str]]] = {}

    def build(self, graph: CodeGraph) -> None:
        """三阶段构建骨架图。"""
        py_files = self._collect_py_files()

        # Phase 1：MODULE 节点 + IMPORTS 边
        for abs_path in py_files:
            self._add_module_node(abs_path, graph)
        for abs_path in py_files:
            self._parse_imports(abs_path, graph)

        # Phase 2：CLASS / FUNCTION 骨架节点 + CONTAINS 边
        for abs_path in py_files:
            self._parse_skeleton(abs_path, graph)

        # Phase 3：INHERITS 边
        self._resolve_inherits(graph)

        stats = graph.stats()
        logger.info(
            "SkeletonBuilder 完成: nodes=%d edges=%d skeleton_files=%d",
            stats["total_nodes"], stats["total_edges"], stats["skeleton_files"],
        )

    # ------------------------------------------------------------------
    # Phase 1：MODULE + IMPORTS
    # ------------------------------------------------------------------

    def _add_module_node(self, abs_path: str, graph: CodeGraph) -> None:
        rel = _rel_path(abs_path, self.repo_root)
        node_id = _module_id(rel)
        if graph.has_node(node_id):
            return

        try:
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=abs_path)
            line_count = source.count("\n") + 1
            mod_docstring = _extract_docstring(tree.body)
        except (SyntaxError, OSError):
            line_count = 0
            mod_docstring = ""

        node = CodeNode(
            id=node_id,
            type=NodeType.MODULE,
            name=os.path.basename(abs_path),
            qualified_name=rel.replace("/", ".").removesuffix(".py"),
            file=rel,
            start_line=1,
            end_line=line_count,
            code_text="",
            docstring=mod_docstring,
        )
        graph.add_node(node)
        graph.set_file_depth(rel, "skeleton")

    def _parse_imports(self, abs_path: str, graph: CodeGraph) -> None:
        rel = _rel_path(abs_path, self.repo_root)
        src_id = _module_id(rel)
        try:
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=abs_path)
        except SyntaxError:
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    dst_rel = self._dotted_to_relpath(alias.name)
                    if dst_rel:
                        self._add_import_edge(src_id, dst_rel, graph)
            elif isinstance(node, ast.ImportFrom):
                dst_rel = None
                if node.level and node.level > 0:
                    dst_rel = self._resolve_relative_import(
                        node.level, node.module, abs_path
                    )
                elif node.module:
                    dst_rel = self._dotted_to_relpath(node.module)
                if dst_rel:
                    self._add_import_edge(src_id, dst_rel, graph)

    def _add_import_edge(self, src_id: str, dst_rel: str, graph: CodeGraph) -> None:
        dst_id = _module_id(dst_rel)
        if not graph.has_node(dst_id):
            dst_abs = os.path.join(self.repo_root, dst_rel)
            if os.path.isfile(dst_abs):
                self._add_module_node(dst_abs, graph)
            else:
                return
        if not graph.has_edge(src_id, dst_id, EdgeType.IMPORTS):
            graph.add_edge(CodeEdge(src=src_id, dst=dst_id, relation_type=EdgeType.IMPORTS))

    # ------------------------------------------------------------------
    # Phase 2：CLASS / FUNCTION 骨架节点
    # ------------------------------------------------------------------

    def _parse_skeleton(self, abs_path: str, graph: CodeGraph) -> None:
        rel = _rel_path(abs_path, self.repo_root)
        module_id = _module_id(rel)
        if not graph.has_node(module_id):
            return

        try:
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=abs_path)
        except SyntaxError:
            return

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                self._add_class_skeleton(rel, node, graph)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add_function_skeleton(rel, node, graph)

    def _add_class_skeleton(
        self, file_rel: str, node: ast.ClassDef, graph: CodeGraph
    ) -> None:
        node_id = f"{file_rel}::{node.name}"
        bases_str = _extract_class_bases_str(node)
        docstring = _extract_docstring(node.body)
        method_names = _extract_method_names(node)

        code_text = f"class {node.name}{bases_str}:\n"
        if docstring:
            code_text += f'    """{docstring}"""\n'
        if method_names:
            code_text += f"    # Methods: {', '.join(method_names)}\n"

        code_node = CodeNode(
            id=node_id,
            type=NodeType.CLASS,
            name=node.name,
            qualified_name=f"{file_rel.replace('/', '.').removesuffix('.py')}.{node.name}",
            file=file_rel,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            code_text=code_text,
            method_names=method_names,
            signature=bases_str,
            docstring=docstring,
        )
        graph.add_node(code_node)

        # CONTAINS 边
        module_id = _module_id(file_rel)
        if not graph.has_edge(module_id, node_id, EdgeType.CONTAINS):
            graph.add_edge(CodeEdge(src=module_id, dst=node_id, relation_type=EdgeType.CONTAINS))

        # 记录待解析的继承关系
        base_names = [_base_name_simple(b) for b in node.bases]
        base_names = [b for b in base_names if b]
        if base_names:
            self._pending_inherits[node_id] = (file_rel, base_names)

    def _add_function_skeleton(
        self, file_rel: str, node: ast.FunctionDef | ast.AsyncFunctionDef,
        graph: CodeGraph,
    ) -> None:
        node_id = f"{file_rel}::{node.name}"
        signature = _extract_func_signature(node)
        docstring = _extract_docstring(node.body)

        code_text = f"def {node.name}{signature}:\n"
        if docstring:
            code_text += f'    """{docstring}"""\n'
        code_text += "    ...\n"

        code_node = CodeNode(
            id=node_id,
            type=NodeType.FUNCTION,
            name=node.name,
            qualified_name=f"{file_rel.replace('/', '.').removesuffix('.py')}.{node.name}",
            file=file_rel,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            code_text=code_text,
            signature=signature,
            docstring=docstring,
        )
        graph.add_node(code_node)

        module_id = _module_id(file_rel)
        if not graph.has_edge(module_id, node_id, EdgeType.CONTAINS):
            graph.add_edge(CodeEdge(src=module_id, dst=node_id, relation_type=EdgeType.CONTAINS))

    # ------------------------------------------------------------------
    # Phase 3：INHERITS 边
    # ------------------------------------------------------------------

    def _resolve_inherits(self, graph: CodeGraph) -> None:
        class_index: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for node in graph.iter_nodes(NodeType.CLASS):
            class_index[node.name].append((node.id, node.file))

        import_map: Dict[str, Set[str]] = defaultdict(set)
        for edge in graph.iter_edges(EdgeType.IMPORTS):
            src_node = graph.get_node(edge.src)
            dst_node = graph.get_node(edge.dst)
            if src_node and dst_node:
                import_map[src_node.file].add(dst_node.file)

        skip_bases = {"object", "ABC", "Exception", "BaseException", "type"}

        for child_id, (file_rel, base_names) in self._pending_inherits.items():
            accessible = {file_rel} | import_map.get(file_rel, set())
            for bname in base_names:
                if bname in skip_bases:
                    continue
                candidates = class_index.get(bname, [])
                parent_id = None
                for cid, frel in candidates:
                    if frel == file_rel:
                        parent_id = cid
                        break
                if parent_id is None:
                    for cid, frel in candidates:
                        if frel in accessible:
                            parent_id = cid
                            break
                if parent_id is None and candidates:
                    parent_id = candidates[0][0]
                if parent_id and parent_id != child_id:
                    if not graph.has_edge(child_id, parent_id, EdgeType.INHERITS):
                        graph.add_edge(CodeEdge(
                            src=child_id, dst=parent_id,
                            relation_type=EdgeType.INHERITS,
                        ))

        self._pending_inherits.clear()

    # ------------------------------------------------------------------
    # 文件收集与路径工具
    # ------------------------------------------------------------------

    def _collect_py_files(self) -> List[str]:
        result: List[str] = []
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            dirnames[:] = [d for d in dirnames if d not in self.exclude_dirs
                           and not d.endswith(".egg-info")]
            for fname in filenames:
                if fname.endswith(".py"):
                    result.append(os.path.join(dirpath, fname))
        return result

    def _dotted_to_relpath(self, dotted: str) -> Optional[str]:
        candidate = dotted.replace(".", os.sep)
        py_file = os.path.join(self.repo_root, candidate + ".py")
        if os.path.isfile(py_file):
            return _rel_path(py_file, self.repo_root)
        init_file = os.path.join(self.repo_root, candidate, "__init__.py")
        if os.path.isfile(init_file):
            return _rel_path(init_file, self.repo_root)
        return None

    def _resolve_relative_import(
        self, level: int, module: Optional[str], current_file: str
    ) -> Optional[str]:
        current_dir = os.path.dirname(os.path.abspath(current_file))
        base_dir = current_dir
        for _ in range(level - 1):
            base_dir = os.path.dirname(base_dir)
        if module:
            candidate = os.path.join(base_dir, module.replace(".", os.sep))
            py_file = candidate + ".py"
            if os.path.isfile(py_file):
                return _rel_path(py_file, self.repo_root)
            init_file = os.path.join(candidate, "__init__.py")
            if os.path.isfile(init_file):
                return _rel_path(init_file, self.repo_root)
        else:
            init_file = os.path.join(base_dir, "__init__.py")
            if os.path.isfile(init_file):
                return _rel_path(init_file, self.repo_root)
        return None