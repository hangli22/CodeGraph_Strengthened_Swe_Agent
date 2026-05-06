"""
graph_schema.py — 节点/边统一 Schema 与 CodeGraph 数据结构
对应论文 3.2.1：图的整体设计（含骨架/完整两级解析深度支持）
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Iterator

import networkx as nx


class NodeType(str, Enum):
    MODULE   = "MODULE"
    CLASS    = "CLASS"
    FUNCTION = "FUNCTION"
    METHOD   = "METHOD"


class EdgeType(str, Enum):
    CONTAINS     = "CONTAINS"
    IMPORTS      = "IMPORTS"
    PARENT_CHILD = "PARENT_CHILD"
    SIBLING      = "SIBLING"
    CALLS        = "CALLS"
    INHERITS     = "INHERITS"
    OVERRIDES    = "OVERRIDES"


class FileDepth(str, Enum):
    """文件的解析深度状态。"""
    SKELETON = "skeleton"   # 只解析了顶层签名
    FULL     = "full"       # 已完整解析（含方法体、调用关系）


@dataclass
class CodeNode:
    id:             str
    type:           NodeType
    name:           str
    qualified_name: str
    file:           str
    start_line:     int
    end_line:       int
    code_text:      str  = ""
    comment:        str  = ""
    # ── 骨架模式新增字段 ──────────────────────
    method_names:   List[str] = field(default_factory=list)   # CLASS 骨架节点的方法名列表
    signature:      str  = ""   # 函数签名 / 类基类列表
    docstring:      str  = ""   # 提取的 docstring 首行

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CodeNode":
        d = d.copy()
        d["type"] = NodeType(d["type"])
        # 兼容旧数据：缺失的新字段使用默认值
        for key, default in [("method_names", []), ("signature", ""), ("docstring", "")]:
            if key not in d:
                d[key] = default
        return cls(**d)

    def skeleton_embedding_text(self) -> str:
        """生成骨架模式下的紧凑 embedding 文本。"""
        if self.type == NodeType.CLASS:
            parts = [f"class {self.name}{self.signature}"]
            if self.docstring:
                parts.append(f": {self.docstring}")
            if self.method_names:
                parts.append(f". Methods: {', '.join(self.method_names)}")
            return " ".join(parts)
        elif self.type in (NodeType.FUNCTION, NodeType.METHOD):
            parts = [f"def {self.name}{self.signature}"]
            if self.docstring:
                parts.append(f": {self.docstring}")
            return " ".join(parts)
        elif self.type == NodeType.MODULE:
            parts = [f"module {self.name}"]
            if self.docstring:
                parts.append(f": {self.docstring}")
            return " ".join(parts)
        return self.qualified_name


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


class CodeGraph:
    """
    基于 NetworkX DiGraph 的多层代码图。
    支持骨架/完整两级解析深度跟踪。
    """

    def __init__(self, repo_root: str = ""):
        self.repo_root = repo_root
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()
        self._file_depth: Dict[str, str] = {}   # file_rel → "skeleton" | "full"

    # 兼容旧 pickle（缺少 _file_depth 属性时自动初始化）
    def __getattr__(self, name):
        if name == "_file_depth":
            self._file_depth = {}
            return self._file_depth
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # ------------------------------------------------------------------
    # 文件解析深度管理
    # ------------------------------------------------------------------

    def get_file_depth(self, file_rel: str) -> str:
        """返回文件的解析深度：'skeleton' / 'full' / '' (未解析)。"""
        return self._file_depth.get(file_rel, "")

    def set_file_depth(self, file_rel: str, depth: str) -> None:
        self._file_depth[file_rel] = depth

    def get_skeleton_files(self) -> List[str]:
        return [f for f, d in self._file_depth.items() if d == "skeleton"]

    def get_deepened_files(self) -> List[str]:
        return [f for f, d in self._file_depth.items() if d == "full"]

    def get_all_parsed_files(self) -> Dict[str, str]:
        return dict(self._file_depth)

    # ------------------------------------------------------------------
    # 节点操作
    # ------------------------------------------------------------------

    def add_node(self, node: CodeNode) -> None:
        self._g.add_node(node.id, **node.to_dict())

    def get_node(self, node_id: str) -> Optional[CodeNode]:
        if node_id not in self._g:
            return None
        return CodeNode.from_dict(dict(self._g.nodes[node_id]))

    def has_node(self, node_id: str) -> bool:
        return node_id in self._g

    def update_node_attr(self, node_id: str, **kwargs) -> None:
        """更新节点的指定属性（就地修改）。"""
        if node_id in self._g:
            for k, v in kwargs.items():
                self._g.nodes[node_id][k] = v

    def iter_nodes(self, node_type: Optional[NodeType] = None) -> Iterator[CodeNode]:
        for nid, attrs in self._g.nodes(data=True):
            node = CodeNode.from_dict(dict(attrs))
            if node_type is None or node.type == node_type:
                yield node

    # ------------------------------------------------------------------
    # 边操作
    # ------------------------------------------------------------------

    def add_edge(self, edge: CodeEdge) -> None:
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
        if node_id not in self._g:
            return result
        for _, dst, attrs in self._g.out_edges(node_id, data=True):
            if relation_type is None or attrs.get("relation_type") == relation_type.value:
                result.append(dst)
        return result

    def predecessors(self, node_id: str, relation_type: Optional[EdgeType] = None) -> List[str]:
        result = []
        if node_id not in self._g:
            return result
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

        skeleton_count = len(self.get_skeleton_files())
        full_count     = len(self.get_deepened_files())

        return {
            "total_nodes": self._g.number_of_nodes(),
            "total_edges": self._g.number_of_edges(),
            "skeleton_files": skeleton_count,
            "deepened_files": full_count,
            **{f"nodes_{k}": v for k, v in node_counts.items()},
            **{f"edges_{k}": v for k, v in edge_counts.items()},
        }

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def save_pickle(self, path: str) -> None:
        with open(path, "wb") as f:
            pickle.dump({
                "repo_root":  self.repo_root,
                "graph":      self._g,
                "file_depth": self._file_depth,
            }, f)

    @classmethod
    def load_pickle(cls, path: str) -> "CodeGraph":
        with open(path, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, cls):
            # 兼容旧格式：直接 pickle 了 CodeGraph 对象
            return data
        cg = cls(repo_root=data["repo_root"])
        cg._g = data["graph"]
        cg._file_depth = data.get("file_depth", {})
        return cg

    def save_json(self, path: str) -> None:
        nodes = [CodeNode.from_dict(dict(attrs)).to_dict()
                 for _, attrs in self._g.nodes(data=True)]
        edges = [{"src": s, "dst": d, "relation_type": a["relation_type"]}
                 for s, d, a in self._g.edges(data=True)]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "repo_root":  self.repo_root,
                "nodes":      nodes,
                "edges":      edges,
                "file_depth": self._file_depth,
            }, f, ensure_ascii=False, indent=2)

    @classmethod
    def load_json(cls, path: str) -> "CodeGraph":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cg = cls(repo_root=data["repo_root"])
        for nd in data["nodes"]:
            cg.add_node(CodeNode.from_dict(nd))
        for ed in data["edges"]:
            cg.add_edge(CodeEdge.from_dict(ed))
        cg._file_depth = data.get("file_depth", {})
        return cg

    def __repr__(self) -> str:
        s = self.stats()
        return (f"CodeGraph(repo='{self.repo_root}', "
                f"nodes={s['total_nodes']}, edges={s['total_edges']}, "
                f"skeleton={s['skeleton_files']}, deepened={s['deepened_files']})")