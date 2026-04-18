"""
feature_extractor.py — 节点结构特征提取
=========================================
对应论文 3.3.1：节点特征表示（结构拓扑特征向量）

从 CodeGraph 中为每个 CLASS/FUNCTION/METHOD 节点提取 8 维结构特征向量：

  维度 0: call_in_degree     — 调用入度（被多少函数调用），归一化到 [0,1]
  维度 1: call_out_degree    — 调用出度（调用了多少函数），归一化到 [0,1]
  维度 2: inherit_depth      — 继承层级深度，归一化到 [0,1]
  维度 3: n_subclasses       — 子类数量（CLASS 专用），归一化到 [0,1]
  维度 4: n_methods          — 直接子方法数（CLASS 专用），归一化到 [0,1]
  维度 5: is_abstract        — 是否为抽象方法/类（0.0 或 1.0）
  维度 6: is_overriding      — 是否重写父类方法（0.0 或 1.0）
  维度 7: module_in_degree   — 所在模块被导入次数，归一化到 [0,1]

设计说明
--------
- 归一化方式：在仓库内部做 min-max 归一化，避免跨仓库的量级差异
- MODULE 节点不在检索目标范围内，不计算特征向量
- 所有特征均可从图的边结构中直接计算，不依赖任何外部模型
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import numpy as np

from code_graph_builder.graph_schema import CodeGraph, CodeNode, EdgeType, NodeType
from .retrieval_result import StructuralPosition

# 特征向量维度数
FEATURE_DIM = 8

# 各维度索引常量（便于阅读）
IDX_CALL_IN      = 0
IDX_CALL_OUT     = 1
IDX_INHERIT_DEPTH= 2
IDX_SUBCLASSES   = 3
IDX_METHODS      = 4
IDX_IS_ABSTRACT  = 5
IDX_IS_OVERRIDE  = 6
IDX_MODULE_IN    = 7


class FeatureExtractor:
    """
    从 CodeGraph 中提取节点结构特征，并构建特征矩阵。

    Usage
    -----
    extractor = FeatureExtractor(graph)
    extractor.build()

    # 获取单个节点特征向量
    vec = extractor.get_feature(node_id)

    # 获取所有节点的有序 id 列表和特征矩阵
    node_ids, matrix = extractor.get_matrix()

    # 获取结构位置摘要（用于原因分析）
    pos = extractor.get_position(node_id)
    """

    def __init__(self, graph: CodeGraph):
        self.graph = graph
        # node_id → 原始特征向量（未归一化）
        self._raw: Dict[str, np.ndarray] = {}
        # node_id → 归一化后特征向量
        self._features: Dict[str, np.ndarray] = {}
        # 有序 node_id 列表（与矩阵行顺序一致）
        self._node_ids: List[str] = []
        # 特征矩阵（归一化）
        self._matrix: Optional[np.ndarray] = None
        # 节点结构位置信息
        self._positions: Dict[str, StructuralPosition] = {}
        self._built = False

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def build(self) -> "FeatureExtractor":
        """计算所有节点的特征向量并归一化。链式调用返回 self。"""
        self._compute_raw_features()
        self._normalize()
        self._built = True
        return self

    def get_feature(self, node_id: str) -> Optional[np.ndarray]:
        """返回指定节点的归一化特征向量，不存在则返回 None。"""
        self._ensure_built()
        return self._features.get(node_id)

    def get_matrix(self) -> Tuple[List[str], np.ndarray]:
        """
        返回 (node_ids, feature_matrix)。
        feature_matrix.shape = (n_nodes, FEATURE_DIM)
        """
        self._ensure_built()
        return self._node_ids, self._matrix

    def get_position(self, node_id: str) -> Optional[StructuralPosition]:
        """返回节点的结构位置信息（用于原因分析文本生成）。"""
        self._ensure_built()
        return self._positions.get(node_id)

    def get_all_node_ids(self) -> List[str]:
        self._ensure_built()
        return list(self._node_ids)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()

    def _compute_raw_features(self) -> None:
        """遍历图，计算每个目标节点的原始特征和结构位置。"""
        graph = self.graph

        # 预计算各节点的继承深度（BFS 从根类开始）
        inherit_depth = self._compute_inherit_depths()

        # 预计算模块入度（被多少其他模块 import）
        module_in_deg = self._compute_module_in_degrees()

        for node in graph.iter_nodes():
            if node.type == NodeType.MODULE:
                continue

            nid = node.id

            # 调用入度/出度
            callers = graph.predecessors(nid, EdgeType.CALLS)
            callees = graph.successors(nid, EdgeType.CALLS)
            in_deg  = len(callers)
            out_deg = len(callees)

            # 继承深度
            depth = inherit_depth.get(nid, 0)

            # 子类数（对 CLASS 节点）
            subclasses = len(graph.predecessors(nid, EdgeType.INHERITS)) \
                if node.type == NodeType.CLASS else 0

            # 子方法数（对 CLASS 节点）
            methods = len(graph.successors(nid, EdgeType.PARENT_CHILD)) \
                if node.type == NodeType.CLASS else 0

            # 是否抽象（代码中有 abstractmethod 装饰器或 pass-only 方法体）
            is_abstract = self._is_abstract(node)

            # 是否重写父类方法
            is_override = bool(graph.successors(nid, EdgeType.OVERRIDES))

            # 所在模块的被导入次数
            mod_in = self._get_module_in_degree(node.file, module_in_deg)

            raw = np.array([
                float(in_deg),
                float(out_deg),
                float(depth),
                float(subclasses),
                float(methods),
                float(is_abstract),
                float(is_override),
                float(mod_in),
            ], dtype=np.float32)

            self._raw[nid] = raw

            # 构建结构位置对象
            caller_names = [
                graph.get_node(c).name for c in callers[:10] if graph.get_node(c)
            ]
            callee_names = [
                graph.get_node(c).name for c in callees[:10] if graph.get_node(c)
            ]
            self._positions[nid] = StructuralPosition(
                call_in_degree  = in_deg,
                call_out_degree = out_deg,
                inherit_depth   = depth,
                n_subclasses    = subclasses,
                n_methods       = methods,
                is_overriding   = is_override,
                callers         = caller_names,
                callees         = callee_names,
            )

        self._node_ids = sorted(self._raw.keys())

    def _normalize(self) -> None:
        """在仓库内部做 min-max 归一化，避免跨仓库量级差异。"""
        if not self._raw:
            self._matrix = np.zeros((0, FEATURE_DIM), dtype=np.float32)
            return

        raw_matrix = np.stack(
            [self._raw[nid] for nid in self._node_ids], axis=0
        )  # (N, FEATURE_DIM)

        min_vals = raw_matrix.min(axis=0)
        max_vals = raw_matrix.max(axis=0)
        ranges   = max_vals - min_vals

        # 避免除零：范围为 0 的维度（全仓库相同值）直接置 0
        ranges[ranges == 0] = 1.0
        normed = (raw_matrix - min_vals) / ranges

        self._matrix = normed.astype(np.float32)
        for i, nid in enumerate(self._node_ids):
            self._features[nid] = self._matrix[i]

    def _compute_inherit_depths(self) -> Dict[str, int]:
        """
        用 BFS 从没有父类的根节点出发，计算每个类节点的继承深度。
        CLASS 节点的深度传递给其 METHOD 子节点。
        """
        graph  = self.graph
        depths: Dict[str, int] = {}

        # 找到所有没有父类（INHERITS 出边为 0）的根类
        roots = [
            n.id for n in graph.iter_nodes(NodeType.CLASS)
            if not graph.successors(n.id, EdgeType.INHERITS)
        ]
        queue = list(roots)
        for nid in roots:
            depths[nid] = 0

        visited = set(roots)
        while queue:
            current = queue.pop(0)
            cur_depth = depths[current]
            # 找到继承自 current 的子类
            children = graph.predecessors(current, EdgeType.INHERITS)
            for child in children:
                if child not in visited:
                    depths[child] = cur_depth + 1
                    visited.add(child)
                    queue.append(child)

        # 将 CLASS 深度传递给其直接方法
        result: Dict[str, int] = {}
        for nid, depth in depths.items():
            result[nid] = depth
            methods = graph.successors(nid, EdgeType.PARENT_CHILD)
            for mid in methods:
                result[mid] = depth

        return result

    def _compute_module_in_degrees(self) -> Dict[str, int]:
        """计算每个模块文件被其他模块 import 的次数。"""
        deg: Dict[str, int] = {}
        for edge in self.graph.iter_edges(EdgeType.IMPORTS):
            dst_node = self.graph.get_node(edge.dst)
            if dst_node:
                deg[dst_node.file] = deg.get(dst_node.file, 0) + 1
        return deg

    def _get_module_in_degree(self, file_rel: str, module_in_deg: Dict[str, int]) -> int:
        return module_in_deg.get(file_rel, 0)

    @staticmethod
    def _is_abstract(node: CodeNode) -> bool:
        """
        启发式判断节点是否为抽象方法/类：
        1. 代码中包含 @abstractmethod 装饰器
        2. 方法体只有 pass 或 ... (Ellipsis)
        """
        code = node.code_text
        if not code:
            return False
        if "abstractmethod" in code:
            return True
        # 方法体只有 pass/...
        body_lines = [
            l.strip() for l in code.splitlines()
            if l.strip() and not l.strip().startswith("def ")
            and not l.strip().startswith("class ")
            and not l.strip().startswith("#")
            and not l.strip().startswith('"""')
            and not l.strip().startswith("'''")
        ]
        return len(body_lines) == 1 and body_lines[0] in ("pass", "...")
    