"""
feature_extractor.py — 节点结构特征提取（简化版）

在方向三重构后，主流程已不再依赖本模块进行 KNN 检索。
保留此文件是为了向后兼容可能的外部引用。
核心的结构位置计算已移入 StructuralRetriever 内部。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from code_graph_builder.graph_schema import CodeGraph, CodeNode, EdgeType, NodeType
from .retrieval_result import StructuralPosition

# 保留常量定义以兼容外部 import
FEATURE_DIM = 8

IDX_CALL_IN       = 0
IDX_CALL_OUT      = 1
IDX_INHERIT_DEPTH = 2
IDX_SUBCLASSES    = 3
IDX_METHODS       = 4
IDX_IS_ABSTRACT   = 5
IDX_IS_OVERRIDE   = 6
IDX_MODULE_IN     = 7


class FeatureExtractor:
    """
    简化版特征提取器。
    在方向三重构后，仅作为可选的辅助工具存在。
    StructuralRetriever 已内置所需的位置计算逻辑。
    """

    def __init__(self, graph: CodeGraph):
        self.graph = graph
        self._positions: Dict[str, StructuralPosition] = {}
        self._built = False

    def build(self) -> "FeatureExtractor":
        self._compute_positions()
        self._built = True
        return self

    def rebuild(self) -> "FeatureExtractor":
        self._positions.clear()
        self._built = False
        return self.build()

    def get_position(self, node_id: str) -> Optional[StructuralPosition]:
        self._ensure_built()
        return self._positions.get(node_id)

    def get_all_node_ids(self) -> List[str]:
        self._ensure_built()
        return list(self._positions.keys())

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()

    def _compute_positions(self) -> None:
        graph = self.graph
        for node in graph.iter_nodes():
            if node.type == NodeType.MODULE:
                continue
            callers = graph.predecessors(node.id, EdgeType.CALLS)
            callees = graph.successors(node.id, EdgeType.CALLS)
            caller_names = [graph.get_node(c).name for c in callers[:5] if graph.get_node(c)]
            callee_names = [graph.get_node(c).name for c in callees[:5] if graph.get_node(c)]

            depth = self._get_inherit_depth(node.id) if node.type == NodeType.CLASS else 0
            subclasses = len(graph.predecessors(node.id, EdgeType.INHERITS)) \
                if node.type == NodeType.CLASS else 0
            methods = len(graph.successors(node.id, EdgeType.PARENT_CHILD)) \
                if node.type == NodeType.CLASS else 0
            is_override = bool(graph.successors(node.id, EdgeType.OVERRIDES))

            self._positions[node.id] = StructuralPosition(
                call_in_degree=len(callers),
                call_out_degree=len(callees),
                inherit_depth=depth,
                n_subclasses=subclasses,
                n_methods=methods,
                is_overriding=is_override,
                callers=caller_names,
                callees=callee_names,
            )

    def _get_inherit_depth(self, class_id: str) -> int:
        depth = 0
        current = class_id
        visited = {current}
        while True:
            parents = self.graph.successors(current, EdgeType.INHERITS)
            if not parents:
                break
            parent = parents[0]
            if parent in visited:
                break
            visited.add(parent)
            current = parent
            depth += 1
        return depth