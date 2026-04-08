"""
graph_schema.py — 节点/边统一 Schema 与 CodeGraph 数据结构
对应论文 3.2.1：图的整体设计

节点 Schema
-----------
  id          : str   — 全局唯一标识符，格式为 "<file>::<qualified_name>"
  type        : NodeType  — 节点类型（MODULE / CLASS / FUNCTION / METHOD）
  name        : str   — 短名称（函数名/类名/文件名）
  qualified_name : str — 完整限定名，如 "mypackage.utils.helper"
  file        : str   — 相对于仓库根目录的文件路径
  start_line  : int   — 源码起始行（1-based）
  end_line    : int   — 源码结束行（1-based）
  code_text   : str   — 对应的源码文本（原始字符串，后续用于 embedding）
  # 添加代码注释字段
  
边 Schema
----------
  src         : str   — 源节点 id
  dst         : str   — 目标节点 id
  relation_type : EdgeType  — 边的语义类型（见 EdgeType 枚举）

EdgeType 枚举说明
-----------------
  CONTAINS        : 模块（文件）包含函数/类（文件结构）
  IMPORTS         : 模块 A import 模块 B（跨文件依赖）
  PARENT_CHILD    : AST 父子关系（类包含方法）
  SIBLING         : AST 兄弟关系（同一父节点的同级节点）
  CALLS           : 函数调用关系（调用者 → 被调用者）
  INHERITS        : 类继承关系（子类 → 父类）
  OVERRIDES       : 方法重写关系（子类方法 → 被重写的父类方法）
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Iterator

import networkx as nx


# ---------------------------------------------------------------------------
# 枚举定义
# ---------------------------------------------------------------------------

class NodeType(str, Enum):
    MODULE   = "MODULE"    # 文件级节点
    CLASS    = "CLASS"     # 类定义
    FUNCTION = "FUNCTION"  # 顶层函数
    METHOD   = "METHOD"    # 类中的方法


class EdgeType(str, Enum):
    # 文件结构关系 (3.2.2)
    CONTAINS     = "CONTAINS"      # 文件 → 函数/类
    IMPORTS      = "IMPORTS"       # 文件 → 被导入文件

    # AST 关系 (3.2.3)
    PARENT_CHILD = "PARENT_CHILD"  # 类 → 方法
    SIBLING      = "SIBLING"       # 同级节点

    # 函数调用图 (3.2.4)
    CALLS        = "CALLS"         # 函数/方法 → 函数/方法

    # 类继承层次 (3.2.5)
    INHERITS     = "INHERITS"      # 子类 → 父类
    OVERRIDES    = "OVERRIDES"     # 子类方法 → 父类方法


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class CodeNode:
    id:             str
    type:           NodeType
    name:           str
    qualified_name: str
    file:           str
    start_line:     int
    end_line:       int
    code_text:      str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CodeNode":
        d = d.copy()
        d["type"] = NodeType(d["type"])
        return cls(**d)


@dataclass
class CodeEdge:
    src:           str
    dst:           str
    relation_type: EdgeType

    def to_dict(self) -> dict:
        d = asdict(self)
        d["relation_type"] = self.relation_type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CodeEdge":
        d = d.copy()
        d["relation_type"] = EdgeType(d["relation_type"])
        return cls(**d)


# ---------------------------------------------------------------------------
# CodeGraph：统一图结构
# ---------------------------------------------------------------------------

class CodeGraph:
    """
    基于 NetworkX DiGraph 的多层代码图。

    提供节点/边的增删查接口，以及序列化（JSON / Pickle）方法。
    每个节点以 CodeNode.id 为键，携带完整 CodeNode 作为属性；
    每条边以 (src, dst, relation_type) 三元组唯一标识。
    """

    def __init__(self, repo_root: str = ""):
        self.repo_root = repo_root
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()

    # ------------------------------------------------------------------
    # 节点操作
    # ------------------------------------------------------------------

    def add_node(self, node: CodeNode) -> None:
        """添加或更新节点（id 相同则覆盖属性）。"""
        self._g.add_node(node.id, **node.to_dict())

    def get_node(self, node_id: str) -> Optional[CodeNode]:
        if node_id not in self._g:
            return None
        return CodeNode.from_dict(dict(self._g.nodes[node_id]))

    def has_node(self, node_id: str) -> bool:
        return node_id in self._g

    def iter_nodes(self, node_type: Optional[NodeType] = None) -> Iterator[CodeNode]:
        for nid, attrs in self._g.nodes(data=True):
            node = CodeNode.from_dict(dict(attrs))
            if node_type is None or node.type == node_type:
                yield node

    # ------------------------------------------------------------------
    # 边操作
    # ------------------------------------------------------------------

    def add_edge(self, edge: CodeEdge) -> None:
        """添加边（允许同一对节点之间存在不同类型的多条边）。"""
        self._g.add_edge(edge.src, edge.dst, relation_type=edge.relation_type.value)

    def has_edge(self, src: str, dst: str, relation_type: EdgeType) -> bool:
        if not self._g.has_edge(src, dst):
            return False
        for _, attrs in self._g[src][dst].items():
            if attrs.get("relation_type") == relation_type.value:
                return True
        return False

    def iter_edges(self, relation_type: Optional[EdgeType] = None) -> Iterator[CodeEdge]:
        for src, dst, attrs in self._g.edges(data=True):
            rt = EdgeType(attrs["relation_type"])
            if relation_type is None or rt == relation_type:
                yield CodeEdge(src=src, dst=dst, relation_type=rt)

    def successors(self, node_id: str, relation_type: Optional[EdgeType] = None) -> List[str]:
        result = []
        for _, dst, attrs in self._g.out_edges(node_id, data=True):
            if relation_type is None or attrs.get("relation_type") == relation_type.value:
                result.append(dst)
        return result

    def predecessors(self, node_id: str, relation_type: Optional[EdgeType] = None) -> List[str]:
        result = []
        for src, _, attrs in self._g.in_edges(node_id, data=True):
            if relation_type is None or attrs.get("relation_type") == relation_type.value:
                result.append(src)
        return result

    # ------------------------------------------------------------------
    # 统计信息
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, int]:
        edge_counts: Dict[str, int] = {}
        for _, _, attrs in self._g.edges(data=True):
            rt = attrs.get("relation_type", "UNKNOWN")
            edge_counts[rt] = edge_counts.get(rt, 0) + 1

        node_counts: Dict[str, int] = {}
        for _, attrs in self._g.nodes(data=True):
            nt = attrs.get("type", "UNKNOWN")
            node_counts[nt] = node_counts.get(nt, 0) + 1

        return {
            "total_nodes": self._g.number_of_nodes(),
            "total_edges": self._g.number_of_edges(),
            **{f"nodes_{k}": v for k, v in node_counts.items()},
            **{f"edges_{k}": v for k, v in edge_counts.items()},
        }

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def save_pickle(self, path: str) -> None:
        """保存为 Pickle 格式（推荐，完整保留 NetworkX 对象）。"""
        with open(path, "wb") as f:
            pickle.dump({"repo_root": self.repo_root, "graph": self._g}, f)

    @classmethod
    def load_pickle(cls, path: str) -> "CodeGraph":
        with open(path, "rb") as f:
            data = pickle.load(f)
        cg = cls(repo_root=data["repo_root"])
        cg._g = data["graph"]
        return cg

    def save_json(self, path: str) -> None:
        """保存为 JSON 格式（可读性好，便于调试）。"""
        nodes = [CodeNode.from_dict(dict(attrs)).to_dict()
                 for _, attrs in self._g.nodes(data=True)]
        edges = [{"src": s, "dst": d, "relation_type": a["relation_type"]}
                 for s, d, a in self._g.edges(data=True)]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"repo_root": self.repo_root, "nodes": nodes, "edges": edges},
                      f, ensure_ascii=False, indent=2)

    @classmethod
    def load_json(cls, path: str) -> "CodeGraph":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cg = cls(repo_root=data["repo_root"])
        for nd in data["nodes"]:
            cg.add_node(CodeNode.from_dict(nd))
        for ed in data["edges"]:
            cg.add_edge(CodeEdge.from_dict(ed))
        return cg

    # 方便调试
    def __repr__(self) -> str:
        s = self.stats()
        return (f"CodeGraph(repo='{self.repo_root}', "
                f"nodes={s['total_nodes']}, edges={s['total_edges']})")
