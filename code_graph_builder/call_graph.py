"""
call_graph.py — 函数调用图构建
对应论文 3.2.4：函数调用图构建

职责
----
1. 对每个函数/方法节点，静态分析其函数体内的调用表达式（ast.Call）。
2. 将被调用函数名映射到 graph 中已有的节点 id（仓库内可解析调用）。
3. 建立 CALLS 边：caller_id → callee_id。

设计说明
--------
- 工具选型：Python 内置 ast 模块（避免对 pycg 的外部依赖）。
- 解析策略：
    * 直接调用：foo()     → 在同文件或全局范围内查找名为 foo 的节点
    * 属性调用：obj.bar() → 查找名为 bar 的 METHOD 节点（类方法匹配）
    * 限制：动态调用（getattr、__call__）、运行时多态无法静态解析，
            不在本模块范围内，不影响静态结构关系的完整性。
- 解析顺序：优先匹配同文件节点，再全仓库广播匹配（模糊匹配，
            可能产生少量误报，但对 SWE-Agent 的检索场景可接受）。
"""

from __future__ import annotations

import ast
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .graph_schema import CodeGraph, CodeEdge, EdgeType, NodeType


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _rel_path(abs_path: str, repo_root: str) -> str:
    return os.path.relpath(abs_path, repo_root).replace("\\", "/")


def _collect_call_names(func_body: List[ast.stmt]) -> List[str]:
    """
    从函数体语句列表中收集所有被调用的函数名（直接名或属性最末级名）。
    返回去重后的列表。
    """
    names: Set[str] = set()

    class CallCollector(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            self.generic_visit(node)

    collector = CallCollector()
    for stmt in func_body:
        collector.visit(stmt)
    return list(names)


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class CallGraphBuilder:
    """
    基于 AST 静态分析，建立函数/方法之间的 CALLS 边。

    Usage
    -----
    builder = CallGraphBuilder(repo_root="/path/to/repo")
    builder.build(graph)   # graph 中已包含 FUNCTION/METHOD 节点
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
        1. 建立 name → [node_id, ...] 的全局名称索引（用于快速查找被调用者）。
        2. 遍历所有 FUNCTION/METHOD 节点，解析调用，写入 CALLS 边。
        """
        name_index = self._build_name_index(graph)
        self._resolve_calls(graph, name_index)

    # ------------------------------------------------------------------
    # 构建名称索引
    # ------------------------------------------------------------------

    def _build_name_index(
        self, graph: CodeGraph
    ) -> Dict[str, List[Tuple[str, str]]]:
        """
        返回 {short_name: [(node_id, file_rel), ...]} 字典。
        同名函数/方法可能存在于多个文件，全部保留。
        """
        index: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for node in graph.iter_nodes():
            if node.type in (NodeType.FUNCTION, NodeType.METHOD):
                index[node.name].append((node.id, node.file))
        return dict(index)

    # ------------------------------------------------------------------
    # 解析调用关系
    # ------------------------------------------------------------------

    def _resolve_calls(
        self,
        graph: CodeGraph,
        name_index: Dict[str, List[Tuple[str, str]]],
    ) -> None:
        for abs_path in self._collect_py_files():
            self._process_file(abs_path, graph, name_index)

    def _collect_py_files(self) -> List[str]:
        result: List[str] = []
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            dirnames[:] = [d for d in dirnames if d not in self.exclude_dirs
                           and not d.endswith(".egg-info")]
            for fname in filenames:
                if fname.endswith(".py"):
                    result.append(os.path.join(dirpath, fname))
        return result

    def _process_file(
        self,
        abs_path: str,
        graph: CodeGraph,
        name_index: Dict[str, List[Tuple[str, str]]],
    ) -> None:
        rel = _rel_path(abs_path, self.repo_root)
        try:
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            tree   = ast.parse(source, filename=abs_path)
        except SyntaxError:
            return

        # 遍历文件内所有函数/方法定义
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # 定位对应的 graph 节点（通过 file + name 精确查找）
            caller_id = self._find_node_id(rel, node, graph)
            if caller_id is None:
                continue

            # 收集该函数体内的调用名
            callee_names = _collect_call_names(node.body)

            for callee_name in callee_names:
                if callee_name not in name_index:
                    continue
                candidates = name_index[callee_name]

                # 优先选同文件内的候选
                same_file = [nid for nid, frel in candidates if frel == rel]
                chosen = same_file if same_file else [nid for nid, _ in candidates]

                for callee_id in chosen:
                    if callee_id == caller_id:
                        continue  # 跳过自递归（可按需保留）
                    if not graph.has_edge(caller_id, callee_id, EdgeType.CALLS):
                        graph.add_edge(
                            CodeEdge(src=caller_id, dst=callee_id, relation_type=EdgeType.CALLS)
                        )

    def _find_node_id(
        self, file_rel: str, ast_func: ast.FunctionDef | ast.AsyncFunctionDef,
        graph: CodeGraph,
    ) -> Optional[str]:
        """
        根据文件路径与函数名，在 graph 中定位节点 id。
        通过 start_line 进一步消歧（同名函数不同行）。
        """
        target_line = ast_func.lineno
        for node in graph.iter_nodes():
            if (node.file == file_rel
                    and node.name == ast_func.name
                    and node.start_line == target_line
                    and node.type in (NodeType.FUNCTION, NodeType.METHOD)):
                return node.id
        return None
