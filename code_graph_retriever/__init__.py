"""
code_graph_retriever — 代码图检索模块
=======================================
对应论文 3.3 节：结构检索模块

依赖 code_graph_builder 构建的 CodeGraph，提供两路检索能力：
  - 结构检索（GRACE 路线）：基于节点拓扑特征向量
  - 语义检索：基于 node.comment 的 embedding
  - 混合检索：两路加权融合（主入口）

子模块
------
  retrieval_result    — 统一的检索结果数据结构（含原因分析）
  feature_extractor   — 从 CodeGraph 提取结构特征向量
  structural_retriever— 基于结构特征的检索（GRACE 路线）
  semantic_retriever  — 基于 comment embedding 的语义检索
  hybrid_retriever    — 融合两路结果（主入口）

快速上手
--------
  from code_graph_builder import CodeGraphBuilder
  from code_graph_retriever import HybridRetriever

  graph    = CodeGraphBuilder("/path/to/repo").build()
  retriever = HybridRetriever(graph).build()
  response = retriever.search("处理 HTTP 重定向异常", top_k=5)
  print(response.to_agent_text())
"""

from .retrieval_result import RetrievalResult, RetrievalResponse, StructuralPosition
from .feature_extractor import FeatureExtractor, FEATURE_DIM
from .structural_retriever import StructuralRetriever
from .semantic_retriever import (
    SemanticRetriever,
    EmbeddingBackend,
    DashScopeEmbeddingBackend,
    TFIDFEmbeddingBackend,
    MockEmbeddingBackend,
    get_default_embedding_backend,
)
from .hybrid_retriever import HybridRetriever

__all__ = [
    # 数据结构
    "RetrievalResult",
    "RetrievalResponse",
    "StructuralPosition",
    # 特征提取
    "FeatureExtractor",
    "FEATURE_DIM",
    # 检索器
    "StructuralRetriever",
    "SemanticRetriever",
    "HybridRetriever",
    # embedding 后端
    "EmbeddingBackend",
    "DashScopeEmbeddingBackend",
    "TFIDFEmbeddingBackend",
    "MockEmbeddingBackend",
    "get_default_embedding_backend",
]