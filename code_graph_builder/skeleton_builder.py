"""
skeleton_builder.py — 骨架图构建器
====================================
对应方向二第一阶段：Prebuild 构建骨架图

构建全仓库轻量骨架图：
  - MODULE 节点 + 模块 docstring
  - import / from-import → IMPORTS 边
  - import aliases / from-import aliases → 写入 MODULE 节点属性与 code_text 摘要
  - 顶层 class：类名、基类、decorators、docstring、method summaries → CLASS 节点 + CONTAINS 边
  - 顶层 function：函数名、签名、decorators、docstring 首行 → FUNCTION 节点 + CONTAINS 边
  - 类继承 → INHERITS 边
  - unresolved_bases → 写入 CLASS 节点属性，避免不确定继承误连

注意：
  - 仍然不创建 METHOD 节点。
  - 仍然不进入函数/方法体提取 CALLS 边。
  - import 提取使用 ast.walk(tree)，会捕获全文件范围内的 import，包括函数体内延迟导入。
  - CLASS.method_names 继续保留，以兼容旧逻辑；新增 method_summaries 作为更结构化的轻量摘要。
"""

from __future__ import annotations

import ast
import os
import logging
from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .graph_schema import (
    CodeGraph, CodeNode, CodeEdge, NodeType, EdgeType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------

@dataclass
class _ParsedFile:
    """单个 Python 文件的一次性解析缓存。"""

    abs_path: str
    rel: str
    source: str = ""
    tree: Optional[ast.Module] = None
    line_count: int = 0
    module_docstring: str = ""
    parse_error: str = ""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _rel_path(abs_path: str, repo_root: str) -> str:
    return os.path.relpath(abs_path, repo_root).replace("\\", "/")


def _module_id(file_rel: str) -> str:
    return f"{file_rel}::MODULE"


def _extract_docstring(body: list) -> str:
    """从 class/function/module body 提取 docstring 首行。"""
    if not body:
        return ""
    first = body[0]
    if isinstance(first, ast.Expr):
        val = first.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            return val.value.strip().split("\n")[0]
    return ""


def _extract_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """提取函数签名字符串，如 '(self, url: str) -> Response'。"""
    try:
        args_str = ast.unparse(node.args)
        ret_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"({args_str}){ret_str}"
    except Exception:
        return "()"


def _extract_class_bases_str(node: ast.ClassDef) -> str:
    """提取类基类列表字符串，如 '(Base1, Base2)'。"""
    try:
        bases = [ast.unparse(b) for b in node.bases]
        return f"({', '.join(bases)})" if bases else ""
    except Exception:
        return ""


def _extract_class_base_names(node: ast.ClassDef) -> List[str]:
    """提取类基类的最末级名称。"""
    base_names = [_base_name_simple(b) for b in node.bases]
    return [b for b in base_names if b]


def _extract_decorators(node: ast.AST) -> List[str]:
    """提取 decorator 文本。失败时跳过单个 decorator。"""
    decorators = getattr(node, "decorator_list", []) or []
    result: List[str] = []
    for dec in decorators:
        try:
            result.append(ast.unparse(dec))
        except Exception:
            continue
    return result


def _extract_method_summaries(class_node: ast.ClassDef) -> List[dict[str, Any]]:
    """提取类体中第一层方法的轻量摘要，不创建 METHOD 节点。"""
    summaries: List[dict[str, Any]] = []
    for item in class_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        summaries.append({
            "name": item.name,
            "line": item.lineno,
            "end_line": item.end_lineno or item.lineno,
            "signature": _extract_func_signature(item),
            "is_async": isinstance(item, ast.AsyncFunctionDef),
            "decorators": _extract_decorators(item),
            "docstring": _extract_docstring(item.body),
        })
    return summaries


def _base_name_simple(expr: ast.expr) -> Optional[str]:
    """从 AST 基类表达式提取最末级名称。"""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _safe_update_node_attrs(graph: CodeGraph, node_id: str, **attrs: Any) -> None:
    """
    兼容性写入节点属性。

    优先使用 graph.update_node_attr；如果当前 CodeGraph/CodeNode 不支持新增字段，
    则尝试 setattr；仍失败则忽略，保证 skeleton 构建不因附加摘要字段中断。
    """
    if not attrs:
        return
    try:
        graph.update_node_attr(node_id, **attrs)
        return
    except Exception:
        pass

    node = graph.get_node(node_id)
    if node is None:
        return
    for key, value in attrs.items():
        try:
            setattr(node, key, value)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class SkeletonBuilder:
    """
    骨架图构建器：只创建 MODULE / CLASS / FUNCTION 层级节点，不创建 METHOD 节点。 

    Usage
    -----
    builder = SkeletonBuilder(repo_root="/path/to/repo")
    graph = CodeGraph(repo_root="/path/to/repo")
    builder.build(graph)
    """

    def __init__(self, repo_root: str, exclude_dirs: Optional[Set[str]] = None):
        self.repo_root = os.path.abspath(repo_root)
        self.exclude_dirs: Set[str] = exclude_dirs or {
            ".git", "__pycache__", ".tox", ".venv", "venv", "env",
            "node_modules", "dist", "build", ".eggs",
        }
        # 暂存 INHERITS 待解析信息：{child_node_id: (file_rel, [base_name, ...])}
        self._pending_inherits: Dict[str, Tuple[str, List[str]]] = {}

    def build(self, graph: CodeGraph) -> None:
        """三阶段构建骨架图。"""
        parsed_files = self._parse_python_files()

        # Phase 1：MODULE 节点
        for parsed in parsed_files:
            self._add_module_node(parsed, graph)

        # Phase 2：IMPORTS 边 + import alias 摘要
        for parsed in parsed_files:
            self._parse_imports(parsed, graph)

        # Phase 3：CLASS / FUNCTION 骨架节点 + CONTAINS 边
        for parsed in parsed_files:
            self._parse_skeleton(parsed, graph)

        # Phase 4：INHERITS 边。只连接高置信继承，无法判定的写入 unresolved_bases。
        self._resolve_inherits(graph)

        stats = graph.stats()
        logger.info(
            "SkeletonBuilder 完成: nodes=%d edges=%d skeleton_files=%d",
            stats["total_nodes"], stats["total_edges"], stats["skeleton_files"],
        )

    # ------------------------------------------------------------------
    # 解析缓存
    # ------------------------------------------------------------------

    def _parse_python_files(self) -> List[_ParsedFile]:
        """一次性读取并解析所有 Python 文件，供后续阶段共享。"""
        parsed_files: List[_ParsedFile] = []
        for abs_path in self._collect_py_files():
            rel = _rel_path(abs_path, self.repo_root)
            parsed = _ParsedFile(abs_path=abs_path, rel=rel)
            try:
                source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=abs_path)
                parsed.source = source
                parsed.tree = tree
                parsed.line_count = source.count("\n") + 1
                parsed.module_docstring = _extract_docstring(tree.body)
            except (SyntaxError, OSError) as e:
                parsed.parse_error = str(e)
                parsed.line_count = 0
                parsed.module_docstring = ""
                logger.debug("解析失败，跳过结构解析 %s: %s", rel, e)
            parsed_files.append(parsed)
        return parsed_files

    # ------------------------------------------------------------------
    # Phase 1：MODULE
    # ------------------------------------------------------------------

    def _add_module_node(self, parsed: _ParsedFile, graph: CodeGraph) -> None:
        rel = parsed.rel
        node_id = _module_id(rel)
        if graph.has_node(node_id):
            return

        node = CodeNode(
            id=node_id,
            type=NodeType.MODULE,
            name=os.path.basename(parsed.abs_path),
            qualified_name=rel.replace("/", ".").removesuffix(".py"),
            file=rel,
            start_line=1,
            end_line=parsed.line_count,
            code_text="",
            docstring=parsed.module_docstring,
        )
        graph.add_node(node)
        graph.set_file_depth(rel, "skeleton")

        if parsed.parse_error:
            _safe_update_node_attrs(graph, node_id, parse_error=parsed.parse_error)

    # ------------------------------------------------------------------
    # Phase 2：IMPORTS + aliases
    # ------------------------------------------------------------------

    def _parse_imports(self, parsed: _ParsedFile, graph: CodeGraph) -> None:
        if parsed.tree is None:
            return

        rel = parsed.rel
        src_id = _module_id(rel)
        import_aliases: Dict[str, str] = {}
        from_import_aliases: Dict[str, str] = {}

        # 使用 ast.walk：有意捕获全文件范围内 import，包括函数体内延迟导入。
        for node in ast.walk(parsed.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound_name = alias.asname or alias.name.split(".")[0]
                    import_aliases[bound_name] = alias.name

                    dst_rel = self._dotted_to_relpath(alias.name)
                    if dst_rel:
                        self._add_import_edge(src_id, dst_rel, graph)

            elif isinstance(node, ast.ImportFrom):
                module_name = self._resolve_import_from_module_name(
                    node.level,
                    node.module,
                    parsed.abs_path,
                )

                dst_rel = None
                if node.level and node.level > 0:
                    dst_rel = self._resolve_relative_import(
                        node.level, node.module, parsed.abs_path
                    )
                elif node.module:
                    dst_rel = self._dotted_to_relpath(node.module)

                if dst_rel:
                    self._add_import_edge(src_id, dst_rel, graph)

                if module_name:
                    for alias in node.names:
                        if alias.name == "*":
                            from_import_aliases["*"] = module_name + ".*"
                            continue
                        bound_name = alias.asname or alias.name
                        from_import_aliases[bound_name] = f"{module_name}.{alias.name}"

        module_summary = self._format_module_import_summary(
            parsed.module_docstring,
            import_aliases,
            from_import_aliases,
        )
        _safe_update_node_attrs(
            graph,
            src_id,
            import_aliases=dict(sorted(import_aliases.items())),
            from_import_aliases=dict(sorted(from_import_aliases.items())),
            code_text=module_summary,
        )

    def _add_import_edge(self, src_id: str, dst_rel: str, graph: CodeGraph) -> None:
        dst_id = _module_id(dst_rel)
        if not graph.has_node(dst_id):
            # build() 已经先为所有 py 文件建立 MODULE 节点。
            # 如果这里还不存在，说明目标不在本轮收集范围内，保守跳过。
            return
        if not graph.has_edge(src_id, dst_id, EdgeType.IMPORTS):
            graph.add_edge(CodeEdge(src=src_id, dst=dst_id, relation_type=EdgeType.IMPORTS))

    def _format_module_import_summary(
        self,
        docstring: str,
        import_aliases: Dict[str, str],
        from_import_aliases: Dict[str, str],
    ) -> str:
        lines: List[str] = []
        if docstring:
            lines.append(f'"""{docstring}"""')
        if import_aliases:
            parts = [f"{k} -> {v}" for k, v in sorted(import_aliases.items())]
            lines.append("# Import aliases: " + "; ".join(parts[:30]))
        if from_import_aliases:
            parts = [f"{k} -> {v}" for k, v in sorted(from_import_aliases.items())]
            lines.append("# From-import aliases: " + "; ".join(parts[:30]))
        return "\n".join(lines).strip()

    def _resolve_import_from_module_name(
        self,
        level: int,
        module: Optional[str],
        current_file: str,
    ) -> str:
        """为 from-import alias 构造尽量稳定的模块名字符串。"""
        if not level:
            return module or ""

        current_dir = os.path.dirname(os.path.abspath(current_file))
        base_dir = current_dir
        for _ in range(level - 1):
            base_dir = os.path.dirname(base_dir)

        if not self._is_within_repo(base_dir):
            return ""

        if module:
            abs_mod = os.path.join(base_dir, module.replace(".", os.sep))
            rel_base = _rel_path(abs_mod, self.repo_root).replace("/", ".")
            return rel_base

        rel_base = _rel_path(base_dir, self.repo_root).replace("/", ".")
        return rel_base if rel_base != "." else ""

    # ------------------------------------------------------------------
    # Phase 3：CLASS / FUNCTION 骨架节点
    # ------------------------------------------------------------------

    def _parse_skeleton(self, parsed: _ParsedFile, graph: CodeGraph) -> None:
        if parsed.tree is None:
            return

        rel = parsed.rel
        module_id = _module_id(rel)
        if not graph.has_node(module_id):
            return

        for node in ast.iter_child_nodes(parsed.tree):
            if isinstance(node, ast.ClassDef):
                self._add_class_skeleton(rel, node, graph)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add_function_skeleton(rel, node, graph)

    def _add_class_skeleton(
        self, file_rel: str, node: ast.ClassDef, graph: CodeGraph
    ) -> None:
        node_id = f"{file_rel}::{node.name}"
        bases_str = _extract_class_bases_str(node)
        base_names = _extract_class_base_names(node)
        docstring = _extract_docstring(node.body)
        decorators = _extract_decorators(node)
        method_summaries = _extract_method_summaries(node)
        method_names = [m["name"] for m in method_summaries]

        code_text = self._format_class_code_text(
            node.name,
            bases_str,
            decorators,
            docstring,
            method_summaries,
        )

        code_node = CodeNode(
            id=node_id,
            type=NodeType.CLASS,
            name=node.name,
            qualified_name=f"{file_rel.replace('/', '.').removesuffix('.py')}.{node.name}",
            file=file_rel,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            code_text=code_text,
            method_names=method_names,
            # 兼容旧逻辑：CLASS.signature 暂时仍保存 bases_str。
            # 中期建议 graph_schema 拆出 class_bases 字段。
            signature=bases_str,
            docstring=docstring,
        )
        graph.add_node(code_node)
        _safe_update_node_attrs(
            graph,
            node_id,
            decorators=decorators,
            class_bases=bases_str,
            base_names=base_names,
            method_summaries=method_summaries,
            unresolved_bases=[],
        )

        module_id = _module_id(file_rel)
        if not graph.has_edge(module_id, node_id, EdgeType.CONTAINS):
            graph.add_edge(CodeEdge(src=module_id, dst=node_id, relation_type=EdgeType.CONTAINS))

        if base_names:
            self._pending_inherits[node_id] = (file_rel, base_names)

    def _format_class_code_text(
        self,
        class_name: str,
        bases_str: str,
        decorators: List[str],
        docstring: str,
        method_summaries: List[dict[str, Any]],
    ) -> str:
        lines: List[str] = []
        for dec in decorators:
            lines.append(f"@{dec}")
        lines.append(f"class {class_name}{bases_str}:")
        if docstring:
            lines.append(f'    """{docstring}"""')
        if method_summaries:
            lines.append("    # Methods:")
            for m in method_summaries[:80]:
                prefix = "async def" if m.get("is_async") else "def"
                decs = m.get("decorators") or []
                dec_text = f" decorators={decs}" if decs else ""
                lines.append(
                    f"    # - {prefix} {m['name']}{m.get('signature', '()')} "
                    f"line={m.get('line', 0)}{dec_text}"
                )
            if len(method_summaries) > 80:
                lines.append(f"    # ... {len(method_summaries) - 80} more methods")
        if len(lines) == 1:
            lines.append("    ...")
        return "\n".join(lines) + "\n"

    def _add_function_skeleton(
        self, file_rel: str, node: ast.FunctionDef | ast.AsyncFunctionDef,
        graph: CodeGraph,
    ) -> None:
        node_id = f"{file_rel}::{node.name}"
        signature = _extract_func_signature(node)
        docstring = _extract_docstring(node.body)
        decorators = _extract_decorators(node)

        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        lines: List[str] = []
        for dec in decorators:
            lines.append(f"@{dec}")
        lines.append(f"{prefix} {node.name}{signature}:")
        if docstring:
            lines.append(f'    """{docstring}"""')
        lines.append("    ...")
        code_text = "\n".join(lines) + "\n"

        code_node = CodeNode(
            id=node_id,
            type=NodeType.FUNCTION,
            name=node.name,
            qualified_name=f"{file_rel.replace('/', '.').removesuffix('.py')}.{node.name}",
            file=file_rel,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            code_text=code_text,
            signature=signature,
            docstring=docstring,
        )
        graph.add_node(code_node)
        _safe_update_node_attrs(
            graph,
            node_id,
            decorators=decorators,
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )

        module_id = _module_id(file_rel)
        if not graph.has_edge(module_id, node_id, EdgeType.CONTAINS):
            graph.add_edge(CodeEdge(src=module_id, dst=node_id, relation_type=EdgeType.CONTAINS))

    # ------------------------------------------------------------------
    # Phase 4：INHERITS 边
    # ------------------------------------------------------------------

    def _resolve_inherits(self, graph: CodeGraph) -> None:
        class_index: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for node in graph.iter_nodes(NodeType.CLASS):
            class_index[node.name].append((node.id, node.file))
        for name in list(class_index.keys()):
            class_index[name].sort(key=lambda x: (x[1], x[0]))

        import_map: Dict[str, Set[str]] = defaultdict(set)
        for edge in graph.iter_edges(EdgeType.IMPORTS):
            src_node = graph.get_node(edge.src)
            dst_node = graph.get_node(edge.dst)
            if src_node and dst_node:
                import_map[src_node.file].add(dst_node.file)

        skip_bases = {"object", "ABC", "Exception", "BaseException", "type"}

        for child_id in sorted(self._pending_inherits.keys()):
            file_rel, base_names = self._pending_inherits[child_id]
            accessible = {file_rel} | import_map.get(file_rel, set())
            unresolved: List[str] = []

            for bname in base_names:
                if bname in skip_bases:
                    continue

                candidates = sorted(class_index.get(bname, []), key=lambda x: (x[1], x[0]))
                parent_id = self._choose_parent_candidate(
                    child_id=child_id,
                    file_rel=file_rel,
                    accessible=accessible,
                    candidates=candidates,
                )

                if parent_id and parent_id != child_id:
                    if not graph.has_edge(child_id, parent_id, EdgeType.INHERITS):
                        graph.add_edge(CodeEdge(
                            src=child_id,
                            dst=parent_id,
                            relation_type=EdgeType.INHERITS,
                        ))
                else:
                    unresolved.append(bname)

            if unresolved:
                _safe_update_node_attrs(graph, child_id, unresolved_bases=unresolved)

        self._pending_inherits.clear()

    def _choose_parent_candidate(
        self,
        child_id: str,
        file_rel: str,
        accessible: Set[str],
        candidates: List[Tuple[str, str]],
    ) -> Optional[str]:
        """
        选择继承父类候选。

        收紧旧逻辑：不再在多个不可判定同名候选中直接取 candidates[0]。
        只在高置信或唯一候选时连边，否则返回 None。
        """
        if not candidates:
            return None

        same_file = [(cid, frel) for cid, frel in candidates if frel == file_rel and cid != child_id]
        if len(same_file) == 1:
            return same_file[0][0]

        accessible_candidates = [
            (cid, frel)
            for cid, frel in candidates
            if frel in accessible and cid != child_id
        ]
        if len(accessible_candidates) == 1:
            return accessible_candidates[0][0]

        non_self_candidates = [(cid, frel) for cid, frel in candidates if cid != child_id]
        if len(non_self_candidates) == 1:
            return non_self_candidates[0][0]

        return None

    # ------------------------------------------------------------------
    # 文件收集与路径工具
    # ------------------------------------------------------------------

    def _collect_py_files(self) -> List[str]:
        result: List[str] = []
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in self.exclude_dirs and not d.endswith(".egg-info")
            )
            for fname in sorted(filenames):
                if fname.endswith(".py"):
                    result.append(os.path.join(dirpath, fname))
        result.sort()
        return result

    def _dotted_to_relpath(self, dotted: str) -> Optional[str]:
        candidate = dotted.replace(".", os.sep)
        py_file = os.path.join(self.repo_root, candidate + ".py")
        if os.path.isfile(py_file) and self._is_within_repo(py_file):
            return _rel_path(py_file, self.repo_root)
        init_file = os.path.join(self.repo_root, candidate, "__init__.py")
        if os.path.isfile(init_file) and self._is_within_repo(init_file):
            return _rel_path(init_file, self.repo_root)
        return None

    def _resolve_relative_import(
        self, level: int, module: Optional[str], current_file: str
    ) -> Optional[str]:
        current_dir = os.path.dirname(os.path.abspath(current_file))
        base_dir = current_dir
        for _ in range(level - 1):
            base_dir = os.path.dirname(base_dir)

        if not self._is_within_repo(base_dir):
            return None

        if module:
            candidate = os.path.join(base_dir, module.replace(".", os.sep))
            py_file = candidate + ".py"
            if os.path.isfile(py_file) and self._is_within_repo(py_file):
                return _rel_path(py_file, self.repo_root)
            init_file = os.path.join(candidate, "__init__.py")
            if os.path.isfile(init_file) and self._is_within_repo(init_file):
                return _rel_path(init_file, self.repo_root)
        else:
            init_file = os.path.join(base_dir, "__init__.py")
            if os.path.isfile(init_file) and self._is_within_repo(init_file):
                return _rel_path(init_file, self.repo_root)
        return None

    def _is_within_repo(self, path: str) -> bool:
        try:
            root = os.path.abspath(self.repo_root)
            target = os.path.abspath(path)
            return os.path.commonpath([root, target]) == root
        except ValueError:
            return False
