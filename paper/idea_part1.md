## 创新点一：面向缺陷修复 Agent 的两阶段代码图表示

不要只说“两阶段构建代码图”，要说：

针对现有 Agent 缺少结构化仓库理解、而完整代码图构建成本较高的问题，本文提出面向缺陷修复 Agent 的两阶段代码图表示方法。该方法首先构建轻量骨架图以支持全仓库级检索和结构导航，再根据 Agent 的局部探索需求执行 method 级按需深化，从而兼顾仓库级覆盖率与局部代码理解粒度。

现有研究已经表明，代码图和知识图能够提升 SWE-bench 中的代码定位与软件修复性能。然而，已有方法多关注一次性定位、固定流程修复或完整图检索。本文进一步关注缺陷修复 Agent 的多轮交互过程，提出轻量骨架图与按需深化相结合的两阶段代码图表示，使 Agent 能够在低成本全局定位与局部 method 级理解之间动态切换。

## 类似工作：（待验证）
宽泛意义上：有人做过类似“粗到细 / 子图检索 / 多阶段检索”
例如 RepoGraph 使用 repository-wide structure 和 subgraph/ego-graph retrieval；GraphCoder 这类 repo-level code completion 工作也有 coarse-to-fine retrieval 思想；RANGER 这类 repository-level agent 也有 dual-stage retrieval pipeline
