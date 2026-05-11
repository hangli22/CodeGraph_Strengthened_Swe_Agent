## SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering（2405）：
核心创新在于提出了 Agent-Computer Interface (ACI) 的概念，并系统性地证明：为 LM Agent 专门设计的计算机接口，远比让它们直接使用为人类设计的工具更有效。

## Agentless: Demystifying LLM-based Software Engineering Agents（2411）：
Agentless 采用一种"无 Agent"的简洁方法，核心思想是：不让 LLM 自主决定未来行动，也不让 LLM 操作复杂工具，而是将整个过程分解为三个严格定义的阶段。
定位阶段：分层定位故障位置；修复阶段：生成候选补丁；验证阶段：筛选并选择最终补丁

## GraphCoder: Enhancing Repository-Level Code Completion via Code Context Graph-based Retrieval and Language Model（2405）：
数据库构建 → 代码检索 → 代码生成。CCG 是 GraphCoder 的核心数据结构，定义为语句级别的多重图：

## CodeRAG: Supportive Code Retrieval on Bigraph for Real-World Code Generation（2409）：
识别出四类关键的支持代码：
被调用的 APIs，语义相似的代码，间接相关的代码，外部领域知识
从需求视角出发，通过双图（需求图、依赖-语义代码图）映射找到支持代码，再通过 Agentic 推理动态扩展检索。（比较相似）

## GraphCodeAgent: Dual Graph-Guided LLM Agent for Retrieval-Augmented Repo-Level Code Generation（2504）：
双图引导的 LLM Agent 框架，核心思想是：通过需求图理解隐式子需求，通过代码图捕获结构依赖，再通过 Agent 多跳推理动态检索所有相关代码。

## RepoGraph: Enhancing AI Software Engineering with Repository-level Code Graph（2410）：（可借鉴写法）
- 现有方法的局限：（这里可以借鉴）
RAG 方法：将代码库视为扁平文档，通过语义相似性检索，忽略了代码间的结构化依赖关系 
Agent 方法（如 SWE-agent、OpenDevin）：允许 LLM 自主决定下一步行动，但缺乏对全局仓库结构的把握，容易陷入局部最优，聚焦于特定文件而忽略更广泛的上下文
- 构建行级别代码图，可作为插件插入现有系统
- 以自我为中心的子图检索（ego-graph retrieval）：
以查询关键词（如函数名、类名）为中心节点
提取其 k-hop 邻域子图（1-hop 直接依赖，2-hop 间接依赖）
通过 LLM 摘要整合多跳信息，解决上下文长度限制
- 插件式架构（Plug-in Module） （可以借鉴，具有可迁移性）
RepoGraph 不替换现有框架，而是作为可插拔模块增强它们：
通用性：同时适用于过程式框架（Agentless）和 Agent 框架（SWE-agent）
无需修改 LLM：不微调模型，仅通过增强上下文提升性能
低成本：性能提升不主要依赖增加 token 消耗，而是更精准的上下文选择 

## RGFL: Reasoning Guided Fault Localization for Automated Program Repair Using Large Language Models（2601）：
现有方法在文件级和元素级定位上存在瓶颈：
文件级：Embedding 相似性无法捕捉 bug 报告与代码功能之间的因果关联
元素级：在多个文件同时呈现时，LLM 难以精准识别具体需要修改的类/函数
作者提出：让 LLM 为每个候选文件/元素单独生成结构化推理解释，用自然语言推理而非原始代码相似性来指导定位 。

## Extracting Conceptual Knowledge to Locate Software Issues（2509）：（对论文没帮助）
大规模仓库的核心挑战：在大型应用仓库（如 Expensify，204 万行代码）中，现有方法面临两个根本性困难：
Concern Tangling（关注点纠缠）：关键逻辑被埋藏在大型多功能函数中，难以隔离相关代码
Concern Scattering（关注点分散）：语义相关的逻辑分散在多个文件或模块中，开发者必须将碎片化的功能拼凑起来才能识别根本原因 
从代码库中提取高层"概念知识"（conceptual knowledge），将细粒度功能分解并重组为高层"关注点"（concerns），为 LLM 提供概念级导航指导
离线阶段：概念知识提取与丰富（Conceptual Knowledge Extraction） │
│  • 从代码标识符中提取概念术语                               │
│  • 用 LLM 扩展和解释术语语义                                 │
│  • 构建仓库级概念知识库                                       │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  在线阶段：关注点增强的定位（Concern-Enhanced Localization）   │
│  Step 1: Issue-Specific Term Retrieval（术语检索）            │
│  Step 2: Conceptual Concern Clustering（关注点聚类）            │
│  Step 3: Conceptual Concern Ranking（关注点排序）             │
│  Step 4: Concern-Enhanced Issue Localization（定位增强）      

## LocAgent: Graph-Guided LLM Agents for Code Localization（2503）：（实验包含定位准确率）
Step 1: 关键词提取
    • 将 issue 描述分解为不同类别
    • 提取与问题密切相关的关键词
    
Step 2: 关键词链接到代码实体
    • 调用 SearchEntity 完善和澄清每个提取的关键词
    
Step 3: 生成从故障到失败的逻辑流
    • 识别触发问题的入口点
    • 迭代调用 TraverseGraph 遍历代码库
    • 调用 RetrieveEntity 检索代码内容
    • 调用 SearchEntity 搜索新关键词
    • 基于 issue 和额外上下文生成逻辑流
    
Step 4: 定位目标实体
    •  pinpoint 所有需要修改的可疑代码实体
    • 基于相关性对这些实体进行排序

## SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering(原始论文)

## RepairAgent: An Autonomous, LLM-Based Agent for Program Repair（2403）：

