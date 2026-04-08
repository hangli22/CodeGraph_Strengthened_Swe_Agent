"""
ast_relations.py — AST 关系构建
对应论文 3.2.3：AST 关系构建

职责
----
1. 使用 Python 内置 ast 模块解析每个 .py 文件。
2. 提取文件中所有 CLASS、FUNCTION、METHOD 节点（含嵌套）。
3. 建立以下边：
   - CONTAINS  : MODULE → CLASS / FUNCTION（文件级归属）
   - PARENT_CHILD : CLASS → METHOD（类与其方法的父子关系）
   - SIBLING   : 同一父节点下相邻同级定义之间的兄弟关系
4. 每个节点的 code_text 字段填充对应的源码原文，供后续 embedding 使用。

节点 id 规则
-----------
  格式：<file_rel>::<qualified_name>
  例如：src/utils.py::MyClass.my_method

  qualified_name 由解析路径拼接而成，如 "ClassName.method_name"，
  对于顶层函数直接使用函数名。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import List, Optional, Set, Tuple

from .graph_schema import (
    CodeGraph, CodeNode, CodeEdge, NodeType, EdgeType,
)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _rel_path(abs_path: str, repo_root: str) -> str:
    return os.path.relpath(abs_path, repo_root).replace("\\", "/")


def _module_id(file_rel: str) -> str:
    return f"{file_rel}::MODULE"


def _node_id(file_rel: str, qualified_name: str) -> str:
    return f"{file_rel}::{qualified_name}"


def _extract_source(source_lines: List[str], node: ast.AST) -> str:
    """从源码行列表中提取 AST 节点对应的原始代码文本。"""
    start = node.lineno - 1          # type: ignore[attr-defined]
    end   = node.end_lineno          # type: ignore[attr-defined]
    return "".join(source_lines[start:end])


# ---------------------------------------------------------------------------
# 核心访问器
# ---------------------------------------------------------------------------

class _ASTVisitor(ast.NodeVisitor):
    """
    递归遍历 AST，收集所有函数/类定义，并记录其父子关系。
    """

    def __init__(self, file_rel: str, source_lines: List[str], graph: CodeGraph):
        self.file_rel     = file_rel
        self.source_lines = source_lines
        self.graph        = graph
        # 当前作用域栈：存储 (qualified_name, node_id, node_type) 三元组
        self._scope_stack: List[Tuple[str, str, NodeType]] = []

    # ------------------------------------------------------------------
    # 通用节点处理
    # ------------------------------------------------------------------

    def _push_scope(self, name: str, node_type: NodeType, ast_node: ast.AST) -> str:
        """
        创建新节点、建立与父节点的归属边，然后入栈，返回节点 id。
        必须在入栈 **前** 先计算父节点关系，入栈后 _scope_stack[-1] 就是自己了。
        """
        parent_qname = self._scope_stack[-1][0] if self._scope_stack else ""
        qualified = f"{parent_qname}.{name}" if parent_qname else name
        node_id   = _node_id(self.file_rel, qualified)

        code_node = CodeNode(
            id=node_id,
            type=node_type,
            name=name,
            qualified_name=qualified,
            file=self.file_rel,
            start_line=ast_node.lineno,           # type: ignore[attr-defined]
            end_line=ast_node.end_lineno,          # type: ignore[attr-defined]
            code_text=_extract_source(self.source_lines, ast_node),
        )
        self.graph.add_node(code_node)

        # ---- 在入栈之前建立父子边 ----
        self._add_parent_edge_for(node_id)

        self._scope_stack.append((qualified, node_id, node_type))
        return node_id

    def _pop_scope(self) -> Tuple[str, str, NodeType]:
        return self._scope_stack.pop()

    def _add_parent_edge_for(self, child_id: str) -> None:
        """
        向当前栈顶（真正的父节点）添加 CONTAINS 或 PARENT_CHILD 边。
        此方法必须在子节点入栈 **前** 调用。
        """
        if not self._scope_stack:
            # 顶层定义 → 由文件 MODULE 节点包含
            parent_id = _module_id(self.file_rel)
            edge_type = EdgeType.CONTAINS
        else:
            _parent_qname, parent_id, parent_type = self._scope_stack[-1]
            if parent_type == NodeType.MODULE:
                edge_type = EdgeType.CONTAINS
            else:
                # CLASS → METHOD / 嵌套函数
                edge_type = EdgeType.PARENT_CHILD

        if not self.graph.has_edge(parent_id, child_id, edge_type):
            self.graph.add_edge(CodeEdge(src=parent_id, dst=child_id, relation_type=edge_type))

    # 保持原接口名可用（向后兼容）
    def _add_parent_edge(self, child_id: str) -> None:
        self._add_parent_edge_for(child_id)

    # ------------------------------------------------------------------
    # 兄弟边：同一父节点下的同级定义
    # ------------------------------------------------------------------

    def _add_sibling_edges(self, sibling_ids: List[str]) -> None:
        """对同一层级的定义列表两两建立 SIBLING 边（相邻对，有向）。"""
        for i in range(len(sibling_ids) - 1):
            a, b = sibling_ids[i], sibling_ids[i + 1]
            if not self.graph.has_edge(a, b, EdgeType.SIBLING):
                self.graph.add_edge(CodeEdge(src=a, dst=b, relation_type=EdgeType.SIBLING))

    # ------------------------------------------------------------------
    # 访问方法
    # ------------------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # _push_scope 内部已建立与父节点的边，并将自身入栈
        self._push_scope(node.name, NodeType.CLASS, node)
        my_qname = self._scope_stack[-1][0]

        child_ids: List[str] = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(item)
                cid = _node_id(self.file_rel, f"{my_qname}.{item.name}")
                child_ids.append(cid)
            else:
                self.generic_visit(item)

        self._add_sibling_edges(child_ids)
        self._pop_scope()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        is_method = bool(self._scope_stack) and self._scope_stack[-1][2] == NodeType.CLASS
        node_type = NodeType.METHOD if is_method else NodeType.FUNCTION

        # _push_scope 内部已建立与父节点的边，并将自身入栈
        self._push_scope(node.name, node_type, node)
        my_qname = self._scope_stack[-1][0]

        inner_ids: List[str] = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(item)
                inner_ids.append(_node_id(self.file_rel, f"{my_qname}.{item.name}"))
            else:
                self.generic_visit(item)

        self._add_sibling_edges(inner_ids)
        self._pop_scope()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    # 阻止 generic_visit 重复递归 ClassDef / FunctionDef
    def generic_visit(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (
                ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef
            )):
                self.visit(child)


# ---------------------------------------------------------------------------
# 公开类
# ---------------------------------------------------------------------------

class ASTRelationBuilder:
    """
    解析仓库所有 .py 文件的 AST，建立：
      - CLASS / FUNCTION / METHOD 节点
      - CONTAINS 边（MODULE → CLASS / FUNCTION）
      - PARENT_CHILD 边（CLASS → METHOD）
      - SIBLING 边（同层级相邻定义）

    Usage
    -----
    builder = ASTRelationBuilder(repo_root="/path/to/repo")
    builder.build(graph)  # graph 中已包含 MODULE 节点（由 FileRelationBuilder 写入）
    """

    def __init__(self, repo_root: str, exclude_dirs: Optional[Set[str]] = None):
        self.repo_root = os.path.abspath(repo_root)
        self.exclude_dirs: Set[str] = exclude_dirs or {
            ".git", "__pycache__", ".tox", ".venv", "venv", "env",
            "node_modules", "dist", "build", ".eggs",
        }

    def build(self, graph: CodeGraph) -> None:
        for abs_path in self._collect_py_files():
            self._process_file(abs_path, graph)

    def _collect_py_files(self) -> List[str]:
        result: List[str] = []
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            dirnames[:] = [d for d in dirnames if d not in self.exclude_dirs
                           and not d.endswith(".egg-info")]
            for fname in filenames:
                if fname.endswith(".py"):
                    result.append(os.path.join(dirpath, fname))
        return result

    def _process_file(self, abs_path: str, graph: CodeGraph) -> None:
        rel = _rel_path(abs_path, self.repo_root)

        # 确保 MODULE 节点存在
        module_id = _module_id(rel)
        if not graph.has_node(module_id):
            return  # 未经 FileRelationBuilder 预处理，跳过

        try:
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            tree   = ast.parse(source, filename=abs_path)
        except SyntaxError:
            return

        source_lines = source.splitlines(keepends=True)
        visitor = _ASTVisitor(
            file_rel=rel,
            source_lines=source_lines,
            graph=graph,
        )

        # 顶层遍历：只处理 ClassDef / FunctionDef / AsyncFunctionDef
        top_level_ids: List[str] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                visitor.visit(node)
                qname = node.name
                nid   = _node_id(rel, qname)
                top_level_ids.append(nid)

        # 顶层同级兄弟边
        visitor._add_sibling_edges(top_level_ids)
