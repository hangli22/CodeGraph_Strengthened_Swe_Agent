"""
code_graph_retriever — 代码图检索模块（支持骨架图 + 按需深化）
"""

from .retrieval_result import RetrievalResult, RetrievalResponse, StructuralPosition
from .feature_extractor import FeatureExtractor, FEATURE_DIM
from .structural_retriever import StructuralRetriever, StructuralQueryMode
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
    "RetrievalResult", "RetrievalResponse", "StructuralPosition",
    "FeatureExtractor", "FEATURE_DIM",
    "StructuralRetriever", "StructuralQueryMode",
    "SemanticRetriever", "HybridRetriever",
    "EmbeddingBackend",
    "DashScopeEmbeddingBackend", "TFIDFEmbeddingBackend",
    "MockEmbeddingBackend", "get_default_embedding_backend",
]