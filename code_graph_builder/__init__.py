"""
code_graph_builder — 多层代码图构建模块（含骨架/完整两级模式）
"""

from .graph_schema import CodeGraph, NodeType, EdgeType, CodeNode, CodeEdge, FileDepth
from .builder import CodeGraphBuilder, BuildConfig
from .skeleton_builder import SkeletonBuilder
from .file_deepener import FileDeepener, DeepenResult
from .comment_annotator import (
    CommentAnnotator,
    AnnotatorConfig,
    AnnotationResult,
    DashScopeBackend,
    AnthropicBackend,
    OpenAIBackend,
    MockBackend,
    LLMBackend,
    get_default_backend,
)

__all__ = [
    "CodeGraph", "NodeType", "EdgeType", "CodeNode", "CodeEdge", "FileDepth",
    "CodeGraphBuilder", "BuildConfig",
    "SkeletonBuilder",
    "FileDeepener", "DeepenResult",
    "CommentAnnotator", "AnnotatorConfig", "AnnotationResult",
    "DashScopeBackend", "AnthropicBackend", "OpenAIBackend",
    "MockBackend", "LLMBackend", "get_default_backend",
]