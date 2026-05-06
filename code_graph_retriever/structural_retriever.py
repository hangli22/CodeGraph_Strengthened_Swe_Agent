"""
structural_retriever.py — 基于粗粒度图关系的结构检索（方向三）

在已有的粗粒度图（文件依赖 + 类继承 + 包结构）上做关系查询，
支持以下查询模式：
  - siblings:       兄弟节点（同类方法、同模块类、同包文件）
  - inheritance:    继承关系链（父类、子类、共父兄弟类）
  - dependencies:   文件级依赖关系（导入/被导入、调用/被调用）
  - co_dependents:  具有相似导入模式的文件
  - related:        综合关联（合并以上所有关系）

输入是一个已知节点 ID，输出是结构上与它关联的节点列表。
不需要全量 deepen，粗粒度层天然具备所需信息。
"""

from __future__ import annotations

import time
from enum import Enum
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Set

import numpy as np

from code_graph_builder.graph_schema import CodeGraph, CodeNode, EdgeType, NodeType
from .retrieval_result import RetrievalResult, RetrievalResponse, StructuralPosition


class StructuralQueryMode(Enum):
    """结构查询模式枚举"""
    SIBLINGS = "siblings"
    INHERITANCE = "inheritance"
    DEPENDENCIES = "dependencies"
    CO_DEPENDENTS = "co_dependents"
    RELATED = "related"


# ─── 关系类型对应的基础分数 ─────────────────────────────────
_SCORE_PARENT_CLASS = 0.92
_SCORE_CHILD_CLASS = 0.88
_SCORE_SIBLING_CLASS = 0.78
_SCORE_SAME_CLASS_METHOD = 0.82
_SCORE_SAME_FILE_CLASS = 0.70
_SCORE_SAME_PACKAGE = 0.45
_SCORE_CALLER = 0.85
_SCORE_CALLEE = 0.80
_SCORE_IMPORTER = 0.70
_SCORE_IMPORTEE = 0.65

# 共依赖相似度阈值
_CO_DEP_THRESHOLD = 0.3


class StructuralRetriever:
    """基于粗粒度图关系的结构检索器"""

    def __init__(self, graph: CodeGraph):
        self.graph = graph
        self._import_profiles: Dict[str, np.ndarray] = {}
        self._file_nodes: Dict[str, List[str]] = {}
        self._all_import_targets: List[str] = []
        self._built = False

    # ─── 生命周期 ─────────────────────────────────────────────

    def build(self) -> "StructuralRetriever":
        """首次构建索引。"""
        self._build_file_index()
        self._build_import_profiles()
        self._built = True
        return self

    def rebuild(self) -> "StructuralRetriever":
        """图变更后重建索引（用于深化后刷新）。"""
        self._import_profiles.clear()
        self._file_nodes.clear()
        self._all_import_targets.clear()
        self._built = False
        return self.build()

    # ─── 主检索接口 ───────────────────────────────────────────

    def search(self, node_id: str, mode: StructuralQueryMode = StructuralQueryMode.RELATED,
               top_k: int = 10) -> RetrievalResponse:
        """
        根据指定模式，检索与 node_id 结构相关的节点。

        Args:
            node_id:  查询节点 ID
            mode:     查询模式（siblings / inheritance / dependencies / co_dependents / related）
            top_k:    返回结果数量上限

        Returns:
            RetrievalResponse 包含排序后的结果列表
        """
        self._ensure_built()
        t0 = time.perf_counter()

        node = self.graph.get_node(node_id)
        if node is None:
            return RetrievalResponse(
                query=node_id, results=[], total_nodes=0,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        query_text = node.qualified_name or node_id

        dispatch = {
            StructuralQueryMode.SIBLINGS: self._find_siblings,
            StructuralQueryMode.INHERITANCE: self._find_inheritance_related,
            StructuralQueryMode.DEPENDENCIES: self._find_dependencies,
            StructuralQueryMode.CO_DEPENDENTS: self._find_co_dependents,
            StructuralQueryMode.RELATED: lambda n: self._find_all_related(n, top_k),
        }
        results = dispatch[mode](node)

        # 去重（保留最高分）+ 排序 + 截断
        deduped = self._deduplicate(results)
        deduped.sort(key=lambda r: r.final_score, reverse=True)
        deduped = deduped[:top_k]

        return RetrievalResponse(
            query=query_text, results=deduped,
            total_nodes=self._count_searchable_nodes(),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    def search_by_node_id(self, node_id: str, top_k: int = 5,
                          exclude_self: bool = True) -> RetrievalResponse:
        """向后兼容接口，供 HybridRetriever 调用。"""
        resp = self.search(node_id, mode=StructuralQueryMode.RELATED,
                           top_k=top_k + (1 if exclude_self else 0))
        if exclude_self:
            resp.results = [r for r in resp.results if r.node_id != node_id][:top_k]
        return resp

    # ─── 查询模式实现 ─────────────────────────────────────────

    def _find_siblings(self, node: CodeNode) -> List[RetrievalResult]:
        """兄弟节点：同类方法、同文件类、同包模块。"""
        results: List[RetrievalResult] = []

        # 1) 同类的其他方法（对 FUNCTION/METHOD 节点）
        if node.type in (NodeType.FUNCTION, NodeType.METHOD):
            parent_ids = self.graph.predecessors(node.id, EdgeType.PARENT_CHILD)
            for pid in parent_ids:
                parent = self.graph.get_node(pid)
                if parent and parent.type == NodeType.CLASS:
                    sibling_ids = self.graph.successors(pid, EdgeType.PARENT_CHILD)
                    for sib_id in sibling_ids:
                        if sib_id == node.id:
                            continue
                        sib = self.graph.get_node(sib_id)
                        if sib:
                            results.append(self._make_result(
                                sib, score=_SCORE_SAME_CLASS_METHOD,
                                reason=f"同属类 {parent.name} 的兄弟方法",
                            ))

        # 2) 同文件的其他类（对 CLASS 节点）
        if node.type == NodeType.CLASS:
            same_file_ids = self._file_nodes.get(node.file, [])
            for nid in same_file_ids:
                if nid == node.id:
                    continue
                n = self.graph.get_node(nid)
                if n and n.type == NodeType.CLASS:
                    results.append(self._make_result(
                        n, score=_SCORE_SAME_FILE_CLASS,
                        reason=f"同文件 {node.file} 内的兄弟类",
                    ))

        # 3) 同包的其他文件中的顶层节点
        pkg = self._get_package(node.file)
        if pkg:
            for file_path, nids in self._file_nodes.items():
                if file_path == node.file:
                    continue
                if self._get_package(file_path) == pkg:
                    for nid in nids[:5]:  # 每文件限 5 个代表
                        n = self.graph.get_node(nid)
                        if n and n.type in (NodeType.CLASS, NodeType.FUNCTION):
                            results.append(self._make_result(
                                n, score=_SCORE_SAME_PACKAGE,
                                reason=f"同包 {pkg}/ 内的兄弟模块节点",
                            ))

        return results

    def _find_inheritance_related(self, node: CodeNode) -> List[RetrievalResult]:
        """继承关系：父类、子类、共父兄弟类。"""
        results: List[RetrievalResult] = []

        # 如果是方法节点，先定位到所属类
        target_class_ids: List[str] = []
        if node.type == NodeType.CLASS:
            target_class_ids = [node.id]
        elif node.type in (NodeType.FUNCTION, NodeType.METHOD):
            parent_ids = self.graph.predecessors(node.id, EdgeType.PARENT_CHILD)
            for pid in parent_ids:
                p = self.graph.get_node(pid)
                if p and p.type == NodeType.CLASS:
                    target_class_ids.append(pid)

        for class_id in target_class_ids:
            class_node = self.graph.get_node(class_id)
            if not class_node:
                continue
            class_name = class_node.name

            # 父类
            parent_class_ids = self.graph.successors(class_id, EdgeType.INHERITS)
            for pid in parent_class_ids:
                p = self.graph.get_node(pid)
                if p:
                    results.append(self._make_result(
                        p, score=_SCORE_PARENT_CLASS,
                        reason=f"{class_name} 的父类",
                    ))
                    # 父类的其他方法（对方法节点有用）
                    if node.type in (NodeType.FUNCTION, NodeType.METHOD):
                        parent_methods = self.graph.successors(pid, EdgeType.PARENT_CHILD)
                        for mid in parent_methods:
                            m = self.graph.get_node(mid)
                            if m and m.name == node.name:
                                # 同名方法 — 可能是被覆写的父方法
                                results.append(self._make_result(
                                    m, score=0.95,
                                    reason=f"父类 {p.name} 中的同名方法（可能被重写）",
                                ))

                    # 共父兄弟类
                    sibling_class_ids = self.graph.predecessors(pid, EdgeType.INHERITS)
                    for sid in sibling_class_ids:
                        if sid == class_id:
                            continue
                        s = self.graph.get_node(sid)
                        if s:
                            results.append(self._make_result(
                                s, score=_SCORE_SIBLING_CLASS,
                                reason=f"与 {class_name} 共享父类 {p.name} 的兄弟类",
                            ))

            # 子类
            child_class_ids = self.graph.predecessors(class_id, EdgeType.INHERITS)
            for cid in child_class_ids:
                c = self.graph.get_node(cid)
                if c:
                    results.append(self._make_result(
                        c, score=_SCORE_CHILD_CLASS,
                        reason=f"{class_name} 的子类",
                    ))

        return results

    def _find_dependencies(self, node: CodeNode) -> List[RetrievalResult]:
        """依赖关系：文件级导入 + 已知调用边。"""
        results: List[RetrievalResult] = []
        file_path = node.file

        # 调用关系（如果已 deepen 则有边）
        callers = self.graph.predecessors(node.id, EdgeType.CALLS)
        for cid in callers:
            c = self.graph.get_node(cid)
            if c and c.id != node.id:
                results.append(self._make_result(
                    c, score=_SCORE_CALLER,
                    reason=f"调用了 {node.name}",
                ))

        callees = self.graph.successors(node.id, EdgeType.CALLS)
        for cid in callees:
            c = self.graph.get_node(cid)
            if c and c.id != node.id:
                results.append(self._make_result(
                    c, score=_SCORE_CALLEE,
                    reason=f"被 {node.name} 调用",
                ))

        # 文件级 import 关系
        seen_files: Set[str] = {file_path}
        for edge in self.graph.iter_edges(EdgeType.IMPORTS):
            src_node = self.graph.get_node(edge.src)
            dst_node = self.graph.get_node(edge.dst)
            if not src_node or not dst_node:
                continue

            if dst_node.file == file_path and src_node.file not in seen_files:
                # 有文件导入了当前文件的内容
                seen_files.add(src_node.file)
                rep = self._get_file_representative(src_node.file)
                if rep:
                    results.append(self._make_result(
                        rep, score=_SCORE_IMPORTER,
                        reason=f"所在文件 {src_node.file} 导入了 {file_path} 的内容",
                    ))
            elif src_node.file == file_path and dst_node.file not in seen_files:
                # 当前文件导入了目标文件
                seen_files.add(dst_node.file)
                rep = self._get_file_representative(dst_node.file)
                if rep:
                    results.append(self._make_result(
                        rep, score=_SCORE_IMPORTEE,
                        reason=f"被 {file_path} 所导入",
                    ))

        return results

    def _find_co_dependents(self, node: CodeNode) -> List[RetrievalResult]:
        """共依赖：具有相似导入模式的文件中的节点。"""
        results: List[RetrievalResult] = []
        file_path = node.file

        if file_path not in self._import_profiles:
            return results

        query_profile = self._import_profiles[file_path]
        query_norm = np.linalg.norm(query_profile)
        if query_norm < 1e-9:
            return results

        scored_files: List[tuple] = []
        for other_file, other_profile in self._import_profiles.items():
            if other_file == file_path:
                continue
            other_norm = np.linalg.norm(other_profile)
            if other_norm < 1e-9:
                continue
            sim = float(np.dot(query_profile, other_profile) / (query_norm * other_norm))
            if sim > _CO_DEP_THRESHOLD:
                scored_files.append((other_file, sim))

        # 按相似度降序
        scored_files.sort(key=lambda x: x[1], reverse=True)

        for other_file, sim in scored_files[:10]:
            rep = self._get_file_representative(other_file)
            if rep:
                results.append(self._make_result(
                    rep, score=sim * 0.85,
                    reason=f"所在文件 {other_file} 与 {file_path} 具有相似的导入模式（余弦相似度 {sim:.2f}）",
                ))

        return results

    def _find_all_related(self, node: CodeNode, top_k: int) -> List[RetrievalResult]:
        """综合关联：合并所有关系类型的结果。"""
        all_results: List[RetrievalResult] = []
        all_results.extend(self._find_siblings(node))
        all_results.extend(self._find_inheritance_related(node))
        all_results.extend(self._find_dependencies(node))
        all_results.extend(self._find_co_dependents(node))
        return all_results

    # ─── 辅助方法 ─────────────────────────────────────────────

    def _build_file_index(self) -> None:
        """构建文件 → 节点列表索引。"""
        self._file_nodes.clear()
        for node in self.graph.iter_nodes():
            if node.type == NodeType.MODULE:
                continue
            if node.file not in self._file_nodes:
                self._file_nodes[node.file] = []
            self._file_nodes[node.file].append(node.id)

    def _build_import_profiles(self) -> None:
        """构建每个文件的导入向量（用于共依赖检测）。"""
        import_targets_set: Set[str] = set()
        file_imports: Dict[str, Set[str]] = {}

        for edge in self.graph.iter_edges(EdgeType.IMPORTS):
            src_node = self.graph.get_node(edge.src)
            dst_node = self.graph.get_node(edge.dst)
            if src_node and dst_node:
                src_file = src_node.file
                dst_file = dst_node.file
                if src_file and dst_file and src_file != dst_file:
                    import_targets_set.add(dst_file)
                    if src_file not in file_imports:
                        file_imports[src_file] = set()
                    file_imports[src_file].add(dst_file)

        self._all_import_targets = sorted(import_targets_set)
        if not self._all_import_targets:
            return

        target_idx = {t: i for i, t in enumerate(self._all_import_targets)}
        dim = len(self._all_import_targets)

        for file_path, imports in file_imports.items():
            vec = np.zeros(dim, dtype=np.float32)
            for imp in imports:
                if imp in target_idx:
                    vec[target_idx[imp]] = 1.0
            self._import_profiles[file_path] = vec

    def _get_file_representative(self, file_path: str) -> Optional[CodeNode]:
        """获取文件中最具代表性的节点（优先选类，其次选函数）。"""
        nids = self._file_nodes.get(file_path, [])
        best: Optional[CodeNode] = None
        for nid in nids:
            n = self.graph.get_node(nid)
            if not n:
                continue
            if n.type == NodeType.CLASS:
                return n  # 类优先
            if best is None and n.type == NodeType.FUNCTION:
                best = n
        return best

    def _make_result(self, node: CodeNode, score: float, reason: str) -> RetrievalResult:
        """构造单个检索结果。"""
        position = self._compute_position(node)
        return RetrievalResult(
            node_id=node.id,
            node_name=node.name,
            qualified_name=node.qualified_name,
            node_type=node.type.value,
            file=node.file,
            start_line=node.start_line,
            end_line=node.end_line,
            code_text=node.code_text,
            comment=node.comment,
            structural_score=float(score),
            semantic_score=0.0,
            final_score=float(score),
            structural_reason=reason,
            position_summary=position.to_text() if position else "",
            position=position,
        )

    def _compute_position(self, node: CodeNode) -> Optional[StructuralPosition]:
        """计算节点的结构位置摘要。"""
        callers = self.graph.predecessors(node.id, EdgeType.CALLS)
        callees = self.graph.successors(node.id, EdgeType.CALLS)

        caller_names = [
            self.graph.get_node(cid).name
            for cid in callers[:5]
            if self.graph.get_node(cid)
        ]
        callee_names = [
            self.graph.get_node(cid).name
            for cid in callees[:5]
            if self.graph.get_node(cid)
        ]

        depth = 0
        if node.type == NodeType.CLASS:
            depth = self._get_inherit_depth(node.id)

        subclasses = len(self.graph.predecessors(node.id, EdgeType.INHERITS)) \
            if node.type == NodeType.CLASS else 0
        methods = len(self.graph.successors(node.id, EdgeType.PARENT_CHILD)) \
            if node.type == NodeType.CLASS else 0
        is_override = bool(self.graph.successors(node.id, EdgeType.OVERRIDES))

        return StructuralPosition(
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
        """计算类的继承深度（到根基类的距离）。"""
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

    @staticmethod
    def _get_package(file_path: str) -> str:
        """从文件路径提取包名（目录部分）。"""
        if not file_path:
            return ""
        parts = PurePosixPath(file_path).parts
        if len(parts) > 1:
            return str(PurePosixPath(*parts[:-1]))
        return ""

    @staticmethod
    def _deduplicate(results: List[RetrievalResult]) -> List[RetrievalResult]:
        """对结果去重，保留每个 node_id 中分数最高的。"""
        best: Dict[str, RetrievalResult] = {}
        for r in results:
            if r.node_id not in best or r.final_score > best[r.node_id].final_score:
                best[r.node_id] = r
        return list(best.values())

    def _count_searchable_nodes(self) -> int:
        """统计可搜索节点总数。"""
        return sum(len(nids) for nids in self._file_nodes.values())

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()