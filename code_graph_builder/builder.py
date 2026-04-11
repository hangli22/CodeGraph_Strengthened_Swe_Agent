"""
builder.py — 统一构建入口
对应论文 3.2 整体：多层代码图构建

职责
----
按顺序调用五个子模块，完整构建多层代码图：
  Step 1 (3.2.2) FileRelationBuilder  — MODULE 节点 + IMPORTS 边
  Step 2 (3.2.3) ASTRelationBuilder   — CLASS/FUNCTION/METHOD 节点 + CONTAINS/PARENT_CHILD/SIBLING 边
  Step 3 (3.2.4) CallGraphBuilder     — CALLS 边
  Step 4 (3.2.5) InheritanceBuilder   — INHERITS + OVERRIDES 边

每一步的输出作为下一步的输入，所有结果统一写入同一个 CodeGraph 对象。

对外暴露两个接口：
  build()        — 完整构建并返回 CodeGraph
  build_partial()— 按开关选择性构建（用于消融实验 4.3.3）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Set

from .graph_schema import CodeGraph
from .file_relations import FileRelationBuilder
from .ast_relations import ASTRelationBuilder
from .call_graph import CallGraphBuilder
from .inheritance import InheritanceBuilder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 构建配置（对应消融实验 4.3.3）
# ---------------------------------------------------------------------------

@dataclass
class BuildConfig:
    """
    控制各关系层与注释生成的开关，用于消融实验或快速调试。

    Attributes
    ----------
    enable_file_relations : bool   — 是否构建文件导入关系（MODULE + IMPORTS）
    enable_ast_relations  : bool   — 是否构建 AST 父子/兄弟关系
    enable_call_graph     : bool   — 是否构建函数调用图
    enable_inheritance    : bool   — 是否构建类继承图
    enable_annotation     : bool   — 是否调用 LLM 生成节点注释（comment 字段）
                                     默认 False，需要显式传入 llm_backend 才会生效
    llm_backend           : LLMBackend | None
                                   — 用于生成注释的 LLM 后端实例
                                     None 时即使 enable_annotation=True 也不执行
    annotation_config     : AnnotatorConfig | None
                                   — 注释生成的详细配置（并发数/截断长度等）
                                     None 时使用默认值
    exclude_dirs          : set    — 额外需要排除的目录名
    save_path             : str    — 构建完成后自动保存的路径（None 则不保存）
    save_format           : str    — 保存格式，'json' 或 'pickle'
    """
    enable_file_relations: bool = True
    enable_ast_relations:  bool = True
    enable_call_graph:     bool = True
    enable_inheritance:    bool = True
    enable_annotation:     bool = False        # 默认关闭，避免无意中消耗 API
    llm_backend:           object = None       # LLMBackend 实例，避免循环 import
    annotation_config:     object = None       # AnnotatorConfig 实例
    exclude_dirs:          Set[str] = field(default_factory=set)
    save_path:             Optional[str] = None
    save_format:           str = "pickle"      # 'json' | 'pickle'


# ---------------------------------------------------------------------------
# 主入口类
# ---------------------------------------------------------------------------

class CodeGraphBuilder:
    """
    多层代码图统一构建器。

    快速上手
    --------
    >>> from code_graph_builder import CodeGraphBuilder
    >>> builder = CodeGraphBuilder("/path/to/repo")
    >>> graph = builder.build()
    >>> print(graph)
    CodeGraph(repo='/path/to/repo', nodes=512, edges=1024)

    启用 LLM 注释生成
    -----------------
    >>> import os
    >>> from code_graph_builder import AnthropicBackend
    >>> from code_graph_builder.builder import BuildConfig
    >>> cfg = BuildConfig(
    ...     enable_annotation=True,
    ...     llm_backend=AnthropicBackend(api_key=os.environ["ANTHROPIC_API_KEY"]),
    ... )
    >>> graph = builder.build(config=cfg)
    >>> node = graph.get_node("src/models.py::User")
    >>> print(node.comment)   # 输出 LLM 生成的结构化注释

    使用 Mock 后端（测试用，不消耗 API）
    ------------------------------------
    >>> from code_graph_builder import MockBackend
    >>> cfg = BuildConfig(enable_annotation=True, llm_backend=MockBackend())
    >>> graph = builder.build(config=cfg)

    保存 / 加载（注释一并序列化）
    ------------------------------
    >>> graph.save_pickle("code_graph.pkl")
    >>> from code_graph_builder import CodeGraph
    >>> graph = CodeGraph.load_pickle("code_graph.pkl")

    消融实验（仅 AST + 调用图，不含继承和注释）
    --------------------------------------------
    >>> cfg = BuildConfig(enable_inheritance=False, enable_annotation=False)
    >>> graph = builder.build(config=cfg)
    """

    def __init__(self, repo_root: str):
        import os
        self.repo_root = os.path.abspath(repo_root)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def build(self, config: Optional[BuildConfig] = None) -> CodeGraph:
        """
        完整构建多层代码图。

        Parameters
        ----------
        config : BuildConfig，可选。默认开启所有结构关系层，关闭注释生成。

        Returns
        -------
        CodeGraph : 包含所有节点、多类型边，以及可选 LLM 注释的代码图。
        """
        if config is None:
            config = BuildConfig()

        graph = CodeGraph(repo_root=self.repo_root)
        total_start = time.perf_counter()

        # Step 1：文件结构关系
        if config.enable_file_relations:
            self._run_step(
                "FileRelationBuilder",
                FileRelationBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )

        # Step 2：AST 关系
        if config.enable_ast_relations:
            self._run_step(
                "ASTRelationBuilder",
                ASTRelationBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )

        # Step 3：函数调用图
        if config.enable_call_graph:
            self._run_step(
                "CallGraphBuilder",
                CallGraphBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )

        # Step 4：类继承层次
        if config.enable_inheritance:
            self._run_step(
                "InheritanceBuilder",
                InheritanceBuilder(self.repo_root, exclude_dirs=config.exclude_dirs),
                graph,
            )

        # Step 5：LLM 注释生成（可选）
        if config.enable_annotation and config.llm_backend is not None:
            self._run_annotation(graph, config)
        elif config.enable_annotation and config.llm_backend is None:
            logger.warning(
                "enable_annotation=True 但未提供 llm_backend，跳过注释生成。\n"
                "请在 BuildConfig 中传入 llm_backend=AnthropicBackend(...) 等后端实例。"
            )

        elapsed = time.perf_counter() - total_start
        stats = graph.stats()
        logger.info(
            "CodeGraph built in %.2fs | nodes=%d  edges=%d",
            elapsed, stats["total_nodes"], stats["total_edges"],
        )

        # 自动保存
        if config.save_path:
            if config.save_format == "json":
                graph.save_json(config.save_path)
            else:
                graph.save_pickle(config.save_path)
            logger.info("Graph saved to %s (%s)", config.save_path, config.save_format)

        return graph

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _run_step(name: str, builder_obj, graph: CodeGraph) -> None:
        t0 = time.perf_counter()
        builder_obj.build(graph)
        elapsed = time.perf_counter() - t0
        stats = graph.stats()
        logger.info(
            "[%s] done in %.2fs | nodes=%d  edges=%d",
            name, elapsed, stats["total_nodes"], stats["total_edges"],
        )

    @staticmethod
    def _run_annotation(graph: CodeGraph, config: BuildConfig) -> None:
        """调用 CommentAnnotator 为图中节点生成注释。"""
        # 延迟导入，避免在不需要注释时引入不必要依赖
        from .comment_annotator import CommentAnnotator, AnnotatorConfig

        t0 = time.perf_counter()
        annotator = CommentAnnotator(backend=config.llm_backend)
        ann_cfg   = config.annotation_config or AnnotatorConfig()
        result    = annotator.annotate(graph, config=ann_cfg)
        elapsed   = time.perf_counter() - t0

        logger.info(
            "[CommentAnnotator] done in %.2fs | %s",
            elapsed, result,
        )