"""
inheritance.py — 类继承层次构建
对应论文 3.2.5：类继承层次构建

职责
----
1. 解析每个类定义的 bases 字段，提取直接父类名。
2. 将父类名解析为 graph 中已有的 CLASS 节点 id。
3. 建立 INHERITS 边：子类 → 父类。
4. 建立 OVERRIDES 边：若子类与父类存在同名方法，则子类方法 → 父类方法。

设计说明
--------
- 父类解析顺序：
    1. 同文件内的同名 CLASS 节点
    2. 通过 IMPORTS 边可达的文件内的同名 CLASS 节点
    3. 全仓库广播匹配（模糊，可能误报）
- 不处理 mixin/多重继承的 MRO 顺序，仅记录直接父类关系。
- 内置基类（object、Exception、…）不在仓库内，静默跳过。
- OVERRIDES 边检测：对子类的每个 METHOD，查找父类中是否存在同名 METHOD；
  若存在，建立 OVERRIDES 边。
"""

from __future__ import annotations

import ast
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .graph_schema import CodeGraph, CodeEdge, CodeNode, EdgeType, NodeType


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _rel_path(abs_path: str, repo_root: str) -> str:
    return os.path.relpath(abs_path, repo_root).replace("\\", "/")


def _base_name(node: ast.expr) -> Optional[str]:
    """从 AST 基类表达式中提取最末级名称（如 Base、pkg.Base → 'Base'）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class InheritanceBuilder:
    """
    构建类继承层次：INHERITS 边 + OVERRIDES 边。

    Usage
    -----
    builder = InheritanceBuilder(repo_root="/path/to/repo")
    builder.build(graph)   # graph 中已包含 CLASS/METHOD 节点
    """

    def __init__(self, repo_root: str, exclude_dirs: Optional[Set[str]] = None):
        self.repo_root = os.path.abspath(repo_root)
        self.exclude_dirs: Set[str] = exclude_dirs or {
            ".git", "__pycache__", ".tox", ".venv", "venv", "env",
            "node_modules", "dist", "build", ".eggs",
        }

    def build(self, graph: CodeGraph) -> None:
        """
        两阶段构建：
        1. 建立类名索引，解析 INHERITS 边。
        2. 基于继承关系，推断 OVERRIDES 边。
        """
        class_name_index = self._build_class_index(graph)
        method_index     = self._build_method_index(graph)
        import_map       = self._build_import_map(graph)

        self._resolve_inherits(graph, class_name_index, import_map)
        self._resolve_overrides(graph, method_index)

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------

    def _build_class_index(
        self, graph: CodeGraph
    ) -> Dict[str, List[Tuple[str, str]]]:
        """返回 {class_name: [(node_id, file_rel), ...]}。"""
        index: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for node in graph.iter_nodes(node_type=NodeType.CLASS):
            index[node.name].append((node.id, node.file))
        return dict(index)

    def _build_method_index(
        self, graph: CodeGraph
    ) -> Dict[str, Dict[str, List[str]]]:
        """
        返回 {class_node_id: {method_name: [method_node_id, ...]}}。
        通过 PARENT_CHILD 边推断方法归属。
        """
        index: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
        for edge in graph.iter_edges(EdgeType.PARENT_CHILD):
            method_node = graph.get_node(edge.dst)
            if method_node and method_node.type == NodeType.METHOD:
                index[edge.src][method_node.name].append(edge.dst)
        return dict(index)

    def _build_import_map(self, graph: CodeGraph) -> Dict[str, Set[str]]:
        """
        返回 {file_rel: {imported_file_rel, ...}}，
        基于 IMPORTS 边构建每个文件可访问的外部模块集合。
        """
        imp_map: Dict[str, Set[str]] = defaultdict(set)
        for edge in graph.iter_edges(EdgeType.IMPORTS):
            src_node = graph.get_node(edge.src)
            dst_node = graph.get_node(edge.dst)
            if src_node and dst_node:
                imp_map[src_node.file].add(dst_node.file)
        return dict(imp_map)

    # ------------------------------------------------------------------
    # INHERITS 边
    # ------------------------------------------------------------------

    def _resolve_inherits(
        self,
        graph: CodeGraph,
        class_name_index: Dict[str, List[Tuple[str, str]]],
        import_map: Dict[str, Set[str]],
    ) -> None:
        for abs_path in self._collect_py_files():
            self._process_file_inherits(abs_path, graph, class_name_index, import_map)

    def _process_file_inherits(
        self,
        abs_path: str,
        graph: CodeGraph,
        class_name_index: Dict[str, List[Tuple[str, str]]],
        import_map: Dict[str, Set[str]],
    ) -> None:
        rel = _rel_path(abs_path, self.repo_root)
        try:
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            tree   = ast.parse(source, filename=abs_path)
        except SyntaxError:
            return

        accessible_files: Set[str] = {rel} | import_map.get(rel, set())

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            # 定位子类节点
            child_id = self._find_class_id(rel, node.name, node.lineno, graph)
            if child_id is None:
                continue

            for base_expr in node.bases:
                base_name = _base_name(base_expr)
                if not base_name or base_name in ("object", "ABC", "Exception",
                                                   "BaseException", "type"):
                    continue

                parent_id = self._lookup_class(
                    base_name, accessible_files, class_name_index, rel
                )
                if parent_id is None:
                    continue
                if not graph.has_edge(child_id, parent_id, EdgeType.INHERITS):
                    graph.add_edge(
                        CodeEdge(src=child_id, dst=parent_id, relation_type=EdgeType.INHERITS)
                    )

    def _lookup_class(
        self,
        name: str,
        accessible_files: Set[str],
        class_name_index: Dict[str, List[Tuple[str, str]]],
        current_file: str,
    ) -> Optional[str]:
        """
        按优先级查找父类节点 id：
        1. 当前文件内
        2. 通过 import 可达的文件内
        3. 全仓库广播匹配
        """
        if name not in class_name_index:
            return None
        candidates = class_name_index[name]

        # 优先：当前文件
        for nid, frel in candidates:
            if frel == current_file:
                return nid
        # 次选：import 可达文件
        for nid, frel in candidates:
            if frel in accessible_files:
                return nid
        # 兜底：全仓库第一个匹配
        return candidates[0][0] if candidates else None

    def _find_class_id(
        self, file_rel: str, class_name: str, lineno: int, graph: CodeGraph
    ) -> Optional[str]:
        for node in graph.iter_nodes(node_type=NodeType.CLASS):
            if node.file == file_rel and node.name == class_name and node.start_line == lineno:
                return node.id
        return None

    # ------------------------------------------------------------------
    # OVERRIDES 边
    # ------------------------------------------------------------------

    def _resolve_overrides(
        self,
        graph: CodeGraph,
        method_index: Dict[str, Dict[str, List[str]]],
    ) -> None:
        """
        对每条 INHERITS 边，检查子类与父类是否存在同名方法，
        若存在则建立 OVERRIDES 边：子类方法 → 父类方法。
        """
        for edge in list(graph.iter_edges(EdgeType.INHERITS)):
            child_class_id  = edge.src
            parent_class_id = edge.dst

            child_methods  = method_index.get(child_class_id, {})
            parent_methods = method_index.get(parent_class_id, {})

            for method_name, child_method_ids in child_methods.items():
                if method_name in parent_methods:
                    for child_mid in child_method_ids:
                        for parent_mid in parent_methods[method_name]:
                            if not graph.has_edge(child_mid, parent_mid, EdgeType.OVERRIDES):
                                graph.add_edge(
                                    CodeEdge(
                                        src=child_mid,
                                        dst=parent_mid,
                                        relation_type=EdgeType.OVERRIDES,
                                    )
                                )

    # ------------------------------------------------------------------
    # 文件收集
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
