"""
comment_annotator.py — LLM 驱动的代码注释生成模块
====================================================

职责
----
对 CodeGraph 中每个 CLASS / FUNCTION / METHOD 节点的 code_text，
调用 LLM 生成结构化的自然语言注释，并写入节点的 comment 字段。

注释格式（统一输出结构，便于后续检索模块解析）
----------------------------------------------
  [功能]    一句话描述该代码块的核心职责
  [参数]    每个参数的类型与含义（无参数则省略）
  [返回值]  返回值的类型与含义（void/None 则省略）
  [异常]    可能抛出的异常（无则省略）
  [副作用]  对外部状态的修改（无则省略）

设计决策
--------
1. 后端解耦：通过 LLMBackend 抽象基类支持多种 LLM 后端，
   目前内置 AnthropicBackend（调用 claude-sonnet-4-20250514）
   和 MockBackend（用于测试，不消耗 API 配额）。

2. 批量并发：使用 ThreadPoolExecutor 并发调用 API，
   可通过 max_workers 控制并发度（默认 4，避免触发限流）。

3. 断点续传：跳过 comment 字段已非空的节点，
   支持对大型仓库分批运行、中途中断后继续。

4. 节点过滤：默认只标注 CLASS/FUNCTION/METHOD（跳过 MODULE），
   可通过 annotate_types 参数自定义。

5. 长代码截断：超过 max_code_chars 的代码块只取前后各半，
   避免超出 LLM 上下文窗口。

6. 错误隔离：单个节点调用失败时写入 "[注释生成失败]" 占位，
   不影响其他节点的处理。

用量估算（psf/requests 规模，593个函数/方法）
-------------------------------------------
  - 每次调用约 200~400 tokens（输入+输出）
  - 总计约 120K~240K tokens
  - 阿里云 qwen-plus 费率下约 ¥0.1~0.2（可接受）

阿里云 DashScope 配置
---------------------
  接口地址 : https://dashscope.aliyuncs.com/compatible-mode/v1  （公共节点，国内直连）
  环境变量 : DASHSCOPE_API_KEY
  默认模型 : qwen-plus（综合性价比最优）
  可选模型 : qwen-turbo（更快）/ qwen-max（更强）

后端优先级（get_default_backend 自动选择）
-----------------------------------------
  1. DashScopeBackend  — 阿里云百炼（DASHSCOPE_API_KEY）★ 优先
  2. AnthropicBackend  — Anthropic Claude（ANTHROPIC_API_KEY）
  3. OpenAIBackend     — OpenAI（OPENAI_API_KEY）
  4. MockBackend       — 测试用，不消耗配额
"""

from __future__ import annotations

import os
import logging
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional, Set

from .graph_schema import CodeGraph, CodeNode, NodeType

logger = logging.getLogger(__name__)


# ===========================================================================
# LLM 后端抽象层
# ===========================================================================

class LLMBackend(ABC):
    """所有 LLM 后端的抽象基类，只需实现 generate() 方法。"""

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """
        调用 LLM 生成单条回复。

        Parameters
        ----------
        prompt : 完整的用户 prompt 字符串

        Returns
        -------
        str : LLM 的回复文本
        """
        ...

class UniAPIBackend(LLMBackend):
    """
    使用中国科技云 Uni-API（OpenAI 兼容接口）。
    默认模型 deepseek-v3:671b，API Key 从环境变量 UNI_API_KEY 获取。
    """

    API_URL = "https://uni-api.cstcloud.cn/v1/chat/completions"
    MODEL   = "deepseek-v4-flash"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ):
        import os
        self.api_key = api_key or os.environ.get("UNI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "未找到 Uni-API Key。\n"
                "请设置环境变量 UNI_API_KEY，\n"
                "或在构造时传入 UniAPIBackend(api_key='...')"
            )
        self.model   = model or self.MODEL
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        import json
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model":      self.model,
            "max_tokens": 512,
            "messages":   [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL,
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Uni-API 错误 {e.code}: {body}") from e

class DeepSeekAPIBackend(LLMBackend):
    """
    使用 DeepSeek 官方 API（OpenAI 兼容接口）。

    默认模型 deepseek-v4-flash，API Key 从环境变量 DEEPSEEK_API_KEY 获取。
    """

    API_URL = "https://api.deepseek.com/chat/completions"
    MODEL = "deepseek-v4-flash"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ):
        import os

        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "未找到 DeepSeek API Key。\n"
                "请设置环境变量 DEEPSEEK_API_KEY，\n"
                "或在构造时传入 DeepSeekAPIBackend(api_key='...')"
            )

        self.model = model or self.MODEL
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        import json
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model": self.model,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek API 错误 {e.code}: {body}") from e



class AnthropicBackend(LLMBackend):
    """
    调用 Anthropic API（claude-sonnet-4-20250514）的后端。

    依赖：无需安装额外库，使用 Python 标准库 urllib 直接发 HTTP 请求。
    API Key 从环境变量 ANTHROPIC_API_KEY 读取，或在构造时传入。

    Usage
    -----
    import os
    backend = AnthropicBackend(api_key=os.environ["ANTHROPIC_API_KEY"])
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    MODEL   = "claude-haiku-4-5-20251001"   # 速度快、成本低，适合批量标注

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 30,
    ):
        import os
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "未找到 Anthropic API Key。\n"
                "请设置环境变量 ANTHROPIC_API_KEY=sk-ant-xxx，\n"
                "或在构造时传入 AnthropicBackend(api_key='sk-ant-xxx')"
            )
        self.model   = model or self.MODEL
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        import json
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model":      self.model,
            "max_tokens": 512,
            "messages":   [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL,
            data=payload,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["content"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Anthropic API 错误 {e.code}: {body}") from e


class OpenAIBackend(LLMBackend):
    """
    调用 OpenAI API（gpt-4o-mini 等）的后端。
    同样使用标准库实现，无需 openai 包。

    Usage
    -----
    backend = OpenAIBackend(api_key="sk-...")
    """

    API_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        timeout: int = 30,
    ):
        import os
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ValueError("未找到 OPENAI_API_KEY 环境变量")
        self.model   = model
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        import json
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model":      self.model,
            "max_tokens": 512,
            "messages":   [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL,
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI API 错误 {e.code}: {body}") from e


class DashScopeBackend(LLMBackend):
    """
    阿里云百炼 DashScope 后端（OpenAI 兼容接口）。★ 默认优先使用

    使用阿里云百炼的公共接入点，无需额外安装任何依赖。
    API Key 从环境变量 DASHSCOPE_API_KEY 读取，或在构造时传入。

    获取 API Key : https://bailian.console.aliyun.com/ → 密钥管理 → 创建 API Key
    接入点文档   : https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope
    公共接入点   : https://dashscope.aliyuncs.com/compatible-mode/v1  （北京，国内直连）

    Usage
    -----
    # 方式一：自动读取环境变量 DASHSCOPE_API_KEY
    backend = DashScopeBackend()

    # 方式二：显式传入，指定模型
    backend = DashScopeBackend(api_key="sk-xxx", model="qwen-coder-plus")

    可选模型（代码理解场景推荐）
    ----------------------------
    qwen-plus        : 综合性价比最优（默认）
    qwen-coder-plus  : 专为代码优化，注释质量更好（推荐用于本项目）
    qwen-turbo       : 响应最快，适合大批量标注
    qwen-max         : 能力最强，适合复杂代码
    qwen-long        : 超长上下文，适合大文件
    """

    # 阿里云百炼公共接入点（北京地域，国内直连，无需特殊网络）
    # 参考文档：https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope
    API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    MODEL   = "qwen-plus"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ):
        import os
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "未找到阿里云 DashScope API Key。\n"
                "请前往 https://bailian.console.aliyun.com/ 创建 API Key，\n"
                "然后设置环境变量: DASHSCOPE_API_KEY=sk-xxx\n"
                "或在构造时传入: DashScopeBackend(api_key='sk-xxx')"
            )
        self.model   = model or self.MODEL
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        import json
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model":      self.model,
            "max_tokens": 512,
            "messages":   [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL,
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DashScope API 错误 {e.code}: {body}") from e

def get_default_backend(verbose: bool = True) -> LLMBackend:
    """
    按优先级自动选择可用的 LLM 后端。

    优先级：DashScope（阿里云）> Anthropic > OpenAI > MockBackend（兜底）

    Parameters
    ----------
    verbose : 是否打印选择结果（默认 True）

    Returns
    -------
    LLMBackend : 第一个可用后端的实例

    Usage
    -----
    backend = get_default_backend()
    annotator = CommentAnnotator(backend)
    """
    import os

    candidates = [
        ("Uni-API（CSTCloud）",  "UNI_API_KEY",        UniAPIBackend),
        ("DashScope（阿里云）", "DASHSCOPE_API_KEY",  DashScopeBackend),
        ("Anthropic",           "ANTHROPIC_API_KEY",  AnthropicBackend),
        ("OpenAI",              "OPENAI_API_KEY",      OpenAIBackend),
    ]

    for name, env_var, cls in candidates:
        key = os.environ.get(env_var, "")
        if key:
            if verbose:
                logger.info("自动选择后端: %s（%s 已设置）", name, env_var)
            try:
                return cls(api_key=key)
            except Exception as e:
                logger.warning("后端 %s 初始化失败: %s，尝试下一个", name, e)

    logger.warning(
        "未找到任何 API Key（DASHSCOPE_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY），"
        "使用 MockBackend（不生成真实注释）。"
    )
    return MockBackend()


class MockBackend(LLMBackend):
    """
    用于单元测试的 Mock 后端，不调用真实 API。
    返回一段包含节点信息的伪注释，验证调用流程是否正确。
    """

    def generate(self, prompt: str) -> str:
        # 从 prompt 里提取节点名（简单解析，仅用于测试）
        lines = prompt.splitlines()
        name_line = next((l for l in lines if "节点名" in l or "名称" in l), "")
        node_name = name_line.split(":")[-1].strip() if name_line else "unknown"
        return (
            f"[功能] {node_name} 的核心功能（Mock生成）\n"
            f"[参数] 见源码\n"
            f"[返回值] 见源码"
        )


# ===========================================================================
# 注释生成配置
# ===========================================================================

@dataclass
class AnnotatorConfig:
    """
    控制注释生成行为的配置项。

    Attributes
    ----------
    annotate_types   : 需要生成注释的节点类型集合
                       默认跳过 MODULE（文件级节点，通常注释价值低）
    max_workers      : 并发线程数，建议 2~8，避免触发 API 限流
    max_code_chars   : 单个节点 code_text 的最大字符数（超出则截断）
                       Anthropic claude-haiku 上下文约 200K tokens，
                       但单次请求建议不超过 4000 字符以控制成本
    skip_if_exists   : True 时跳过已有 comment 的节点（断点续传）
    retry_on_failure : 调用失败时的重试次数
    retry_delay_s    : 重试间隔（秒）
    rate_limit_delay : 每次 API 调用后的最小间隔（秒），防止限流
                       Anthropic Tier-1 限制约 50 RPM，设 0.5s 较安全
    """
    annotate_types:   Set[NodeType] = field(
        default_factory=lambda: {NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD}
    )
    max_workers:      int   = 4
    max_code_chars:   int   = 3000
    skip_if_exists:   bool  = True
    retry_on_failure: int   = 2
    retry_delay_s:    float = 1.0
    rate_limit_delay: float = 0.3


# ===========================================================================
# Prompt 模板
# ===========================================================================

ANNOTATION_PROMPT_TEMPLATE = """\
你是一个专业的代码审查工程师。请对以下 Python 代码块生成简洁的结构化注释。

代码信息：
- 节点类型：{node_type}
- 节点名称：{name}
- 所在文件：{file}
- 行号范围：{start_line} ~ {end_line}

代码内容：
```python
{code_text}
```

请严格按照以下格式输出，不要包含任何多余的解释或 Markdown 标记：

[功能] <一句话描述该代码块的核心职责，不超过50字>
[参数] <逐行列出每个参数名及其含义，无参数则写"无">
[返回值] <返回值类型与含义，无返回值则写"无">
[异常] <可能抛出的异常类型及触发条件，无则写"无">
[副作用] <对外部状态（文件/网络/全局变量）的修改，无则写"无">
"""


def _build_prompt(node: CodeNode, max_code_chars: int) -> str:
    """构建发送给 LLM 的 prompt，对过长代码进行截断。"""
    code = node.code_text
    if len(code) > max_code_chars:
        half = max_code_chars // 2
        code = (
            code[:half]
            + f"\n\n... [代码过长，已截断 {len(code) - max_code_chars} 字符] ...\n\n"
            + code[-half:]
        )
    return ANNOTATION_PROMPT_TEMPLATE.format(
        node_type  = node.type.value,
        name       = node.qualified_name,
        file       = node.file,
        start_line = node.start_line,
        end_line   = node.end_line,
        code_text  = code,
    )


# ===========================================================================
# 主模块：CommentAnnotator
# ===========================================================================

class CommentAnnotator:
    """
    批量为 CodeGraph 中的节点生成 LLM 注释，写入 node.comment 字段。

    快速上手
    --------
    >>> import os
    >>> from code_graph_builder.comment_annotator import CommentAnnotator, AnthropicBackend
    >>> backend = AnthropicBackend(api_key=os.environ["ANTHROPIC_API_KEY"])
    >>> annotator = CommentAnnotator(backend)
    >>> annotator.annotate(graph)          # 原地修改，所有节点写入 comment

    使用 Mock 后端（测试用，不消耗 API）
    ------------------------------------
    >>> from code_graph_builder.comment_annotator import MockBackend
    >>> annotator = CommentAnnotator(MockBackend())
    >>> annotator.annotate(graph)

    断点续传（已有注释的节点跳过）
    --------------------------------
    >>> cfg = AnnotatorConfig(skip_if_exists=True)
    >>> annotator.annotate(graph, config=cfg)

    只标注指定节点列表
    ------------------
    >>> node_ids = ["src/models.py::User", "src/models.py::User.save"]
    >>> annotator.annotate_nodes(graph, node_ids)
    """

    def __init__(self, backend: LLMBackend):
        self.backend = backend

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def annotate(
        self,
        graph: CodeGraph,
        config: Optional[AnnotatorConfig] = None,
    ) -> AnnotationResult:
        """
        对 graph 中所有符合条件的节点批量生成注释。
        注释直接写入各节点的 comment 字段（原地修改）。

        Returns
        -------
        AnnotationResult : 包含成功数、跳过数、失败数的统计信息
        """
        config = config or AnnotatorConfig()

        # 筛选需要处理的节点
        targets = [
            node for node in graph.iter_nodes()
            if node.type in config.annotate_types
            and node.code_text.strip()                       # 跳过无源码的节点
            and not (config.skip_if_exists and node.comment) # 断点续传
        ]

        logger.info(
            "CommentAnnotator: 待标注节点 %d 个（共 %d 个节点）",
            len(targets), graph.stats()["total_nodes"],
        )

        return self._run_batch(graph, targets, config)

    def annotate_nodes(
        self,
        graph: CodeGraph,
        node_ids: List[str],
        config: Optional[AnnotatorConfig] = None,
    ) -> "AnnotationResult":
        """
        只对指定 node_id 列表生成注释（用于增量更新或调试单个节点）。
        """
        config  = config or AnnotatorConfig()
        targets = []
        for nid in node_ids:
            node = graph.get_node(nid)
            if node is None:
                logger.warning("节点不存在，跳过: %s", nid)
                continue
            if not node.code_text.strip():
                logger.warning("节点无 code_text，跳过: %s", nid)
                continue
            targets.append(node)

        return self._run_batch(graph, targets, config)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _run_batch(
        self,
        graph: CodeGraph,
        targets: List[CodeNode],
        config: AnnotatorConfig,
    ) -> "AnnotationResult":
        """并发调用 LLM，将结果写回图节点。"""
        result = AnnotationResult(total=len(targets))
        t0     = time.perf_counter()

        def process(node: CodeNode) -> tuple[str, str, bool]:
            """返回 (node_id, comment_text, success)"""
            prompt  = _build_prompt(node, config.max_code_chars)
            comment = self._call_with_retry(prompt, config)
            success = not comment.startswith("[注释生成失败]")
            return node.id, comment, success

        with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
            futures = {executor.submit(process, node): node for node in targets}
            done = 0
            for future in as_completed(futures):
                done += 1
                node = futures[future]
                try:
                    node_id, comment, success = future.result()
                    # 写回图节点
                    graph_node = graph.get_node(node_id)
                    if graph_node is not None:
                        graph_node.comment = comment
                        # 同步更新 NetworkX 图的节点属性
                        graph._g.nodes[node_id]["comment"] = comment
                    if success:
                        result.succeeded += 1
                        logger.debug("✓ [%d/%d] %s", done, len(targets), node_id)
                    else:
                        result.failed += 1
                        logger.warning("✗ [%d/%d] %s: %s", done, len(targets), node_id, comment)
                except Exception as e:
                    result.failed += 1
                    logger.error("异常 [%d/%d] %s: %s", done, len(targets), node.id, e)

                # 进度日志（每 50 个节点报告一次）
                if done % 50 == 0 or done == len(targets):
                    elapsed = time.perf_counter() - t0
                    logger.info(
                        "进度 %d/%d | 成功=%d 失败=%d | 耗时=%.1fs",
                        done, len(targets), result.succeeded, result.failed, elapsed,
                    )
                # 限流间隔
                if config.rate_limit_delay > 0:
                    time.sleep(config.rate_limit_delay)

        result.elapsed_s = time.perf_counter() - t0
        logger.info(
            "CommentAnnotator 完成: 成功=%d 失败=%d 跳过=%d 耗时=%.1fs",
            result.succeeded, result.failed, result.skipped, result.elapsed_s,
        )
        return result

    def _call_with_retry(self, prompt: str, config: AnnotatorConfig) -> str:
        """带重试的单次 LLM 调用。失败时返回占位字符串。"""
        last_error = ""
        for attempt in range(1 + config.retry_on_failure):
            try:
                return self.backend.generate(prompt)
            except Exception as e:
                last_error = str(e)
                if attempt < config.retry_on_failure:
                    logger.debug("第 %d 次重试... 原因: %s", attempt + 1, e)
                    time.sleep(config.retry_delay_s * (attempt + 1))
        return f"[注释生成失败] {last_error[:100]}"


# ===========================================================================
# 结果统计数据类
# ===========================================================================

@dataclass
class AnnotationResult:
    """annotate() 调用的统计结果。"""
    total:     int   = 0
    succeeded: int   = 0
    failed:    int   = 0
    skipped:   int   = 0
    elapsed_s: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.succeeded / max(self.total, 1)

    def __repr__(self) -> str:
        return (
            f"AnnotationResult(total={self.total}, succeeded={self.succeeded}, "
            f"failed={self.failed}, skipped={self.skipped}, "
            f"elapsed={self.elapsed_s:.1f}s, rate={self.success_rate:.1%})"
        )