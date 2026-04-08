"""
code_graph_builder — 多层代码图构建模块
=========================================
对应论文第 3.2 节：多层代码图构建（3.2.1 ~ 3.2.5）

子模块一览
----------
graph_schema   — 节点/边的统一 Schema 与 CodeGraph 数据结构 (3.2.1)
file_relations — 文件结构关系构建，解析 import 依赖 (3.2.2)
ast_relations  — AST 关系构建，提取函数/类层次 (3.2.3)
call_graph     — 函数调用图构建 (3.2.4)
inheritance    — 类继承层次构建 (3.2.5)
builder        — 统一入口，顺序调用以上模块并汇总为完整 CodeGraph
"""

from .graph_schema import CodeGraph, NodeType, EdgeType, CodeNode, CodeEdge
from .builder import CodeGraphBuilder

__all__ = [
    "CodeGraph",
    "NodeType",
    "EdgeType",
    "CodeNode",
    "CodeEdge",
    "CodeGraphBuilder",
]
