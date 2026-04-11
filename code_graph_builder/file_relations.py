"""
file_relations.py — 文件结构关系构建
对应论文 3.2.2：文件结构关系构建

职责
----
1. 遍历仓库中所有 .py 文件，为每个文件创建 MODULE 节点。
2. 解析每个文件的顶层 import / from...import 语句，
   建立 MODULE → MODULE 的 IMPORTS 边（模块间跨文件依赖）。
3. 将文件内定义的顶层函数/类与其所在文件建立 CONTAINS 边
   （文件 → 函数/类，模块归属边）——顶层节点由 ast_relations 模块负责创建，
   本模块只负责建立归属边，避免重复解析。

设计说明
--------
- import 解析仅进行静态路径推断（不执行代码），将 dotted 模块名映射为相对路径。
- 第三方库或标准库的 import 目标不在仓库内，会被静默跳过（只建仓库内边）。
- 相对 import（from . import x）会根据当前文件所在包目录推断目标路径。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import List, Optional, Set

from .graph_schema import CodeGraph, CodeNode, CodeEdge, NodeType, EdgeType


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _rel_path(abs_path: str, repo_root: str) -> str:
    """返回相对于仓库根目录的路径，统一使用正斜杠。"""
    return os.path.relpath(abs_path, repo_root).replace("\\", "/")


def _module_id(file_rel: str) -> str:
    """根据相对路径生成 MODULE 节点 id。"""
    return f"{file_rel}::MODULE"


def _dotted_to_relpath(dotted: str, repo_root: str) -> Optional[str]:
    """
    将 dotted 模块名（如 'mypackage.utils'）转换为仓库内的相对路径。
    优先匹配 <dotted_as_path>.py，其次匹配 <dotted_as_path>/__init__.py。
    若不在仓库内则返回 None。
    """
    candidate_path = dotted.replace(".", os.sep)
    py_file = os.path.join(repo_root, candidate_path + ".py")
    if os.path.isfile(py_file):
        return _rel_path(py_file, repo_root)
    init_file = os.path.join(repo_root, candidate_path, "__init__.py")
    if os.path.isfile(init_file):
        return _rel_path(init_file, repo_root)
    return None


def _resolve_relative_import(
    level: int, module: Optional[str], current_file: str, repo_root: str
) -> Optional[str]:
    """
    解析相对 import（from . import x / from .. import y）。
    level: import 前的点数（1=当前包，2=上级包，…）
    """
    current_dir = os.path.dirname(os.path.abspath(current_file))
    base_dir = current_dir
    for _ in range(level - 1):
        base_dir = os.path.dirname(base_dir)

    if module:
        candidate = os.path.join(base_dir, module.replace(".", os.sep))
        py_file = candidate + ".py"
        if os.path.isfile(py_file):
            return _rel_path(py_file, repo_root)
        init_file = os.path.join(candidate, "__init__.py")
        if os.path.isfile(init_file):
            return _rel_path(init_file, repo_root)
    else:
        # from . import x —— 目标是包自身的 __init__.py
        init_file = os.path.join(base_dir, "__init__.py")
        if os.path.isfile(init_file):
            return _rel_path(init_file, repo_root)
    return None


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class FileRelationBuilder:
    """
    构建文件结构关系：MODULE 节点 + IMPORTS 边 + CONTAINS 边（归属）。

    Usage
    -----
    builder = FileRelationBuilder(repo_root="/path/to/repo")
    builder.build(graph)
    """

    def __init__(self, repo_root: str, exclude_dirs: Optional[Set[str]] = None):
        """
        Parameters
        ----------
        repo_root    : 仓库根目录绝对路径
        exclude_dirs : 需要跳过的目录名集合（如 {'.git', 'node_modules', '__pycache__'}）
        """
        self.repo_root = os.path.abspath(repo_root)
        self.exclude_dirs: Set[str] = exclude_dirs or {
            ".git", "__pycache__", ".tox", ".venv", "venv", "env",
            "node_modules", "dist", "build", ".eggs", "*.egg-info",
        }

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def build(self, graph: CodeGraph) -> None:
        """
        遍历仓库所有 .py 文件，向 graph 中写入：
          - MODULE 节点
          - IMPORTS 边（跨文件依赖）
        CONTAINS 边（模块→函数/类）由 ast_relations 模块在建立 AST 节点时一并写入。
        """
        py_files = self._collect_py_files()
        # 第一轮：创建所有 MODULE 节点
        for abs_path in py_files:
            self._add_module_node(abs_path, graph)
        # 第二轮：解析 import，建立 IMPORTS 边
        for abs_path in py_files:
            self._parse_imports(abs_path, graph)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _collect_py_files(self) -> List[str]:
        result: List[str] = []
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            # 剪枝：跳过排除目录
            dirnames[:] = [
                d for d in dirnames
                if d not in self.exclude_dirs and not d.endswith(".egg-info")
            ]
            for fname in filenames:
                if fname.endswith(".py"):
                    result.append(os.path.join(dirpath, fname))
        return result

    def _add_module_node(self, abs_path: str, graph: CodeGraph) -> None:
        rel = _rel_path(abs_path, self.repo_root)
        node_id = _module_id(rel)
        if graph.has_node(node_id):
            return
        # 读取源码以计算行数
        try:
            code = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            line_count = code.count("\n") + 1
        except OSError:
            code = ""
            line_count = 0

        node = CodeNode(
            id=node_id,
            type=NodeType.MODULE,
            name=os.path.basename(abs_path),
            qualified_name=rel.replace("/", ".").removesuffix(".py"),
            file=rel,
            start_line=1,
            end_line=line_count,
            code_text="",  # 文件级节点不存储全文，节省内存
            # 但可以考虑存储注释
        )
        graph.add_node(node)

    def _parse_imports(self, abs_path: str, graph: CodeGraph) -> None:
        rel = _rel_path(abs_path, self.repo_root)
        src_id = _module_id(rel)

        try:
            source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=abs_path)
        except SyntaxError:
            return

        for node in ast.walk(tree):
            dst_rel: Optional[str] = None

            if isinstance(node, ast.Import):
                # import a.b.c
                for alias in node.names:
                    dst_rel = _dotted_to_relpath(alias.name, self.repo_root)
                    if dst_rel:
                        self._add_import_edge(src_id, dst_rel, graph)

            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    # 相对 import
                    dst_rel = _resolve_relative_import(
                        node.level, node.module, abs_path, self.repo_root
                    )
                elif node.module:
                    dst_rel = _dotted_to_relpath(node.module, self.repo_root)

                if dst_rel:
                    self._add_import_edge(src_id, dst_rel, graph)

    def _add_import_edge(self, src_id: str, dst_rel: str, graph: CodeGraph) -> None:
        dst_id = _module_id(dst_rel)
        # 确保目标节点存在（若目标文件尚未处理，补充创建）
        if not graph.has_node(dst_id):
            dst_abs = os.path.join(self.repo_root, dst_rel)
            if os.path.isfile(dst_abs):
                self._add_module_node(dst_abs, graph)
            else:
                return  # 目标文件不存在，跳过

        if not graph.has_edge(src_id, dst_id, EdgeType.IMPORTS):
            graph.add_edge(CodeEdge(src=src_id, dst=dst_id, relation_type=EdgeType.IMPORTS))
