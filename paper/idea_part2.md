## 创新点二：BM25 + Semantic + Structural 的多信号融合检索

不要只说“用了三种检索”，要说：

针对 issue 中同时存在自然语言行为描述和精确代码符号的特点，本文设计融合 BM25、语义相似度和结构扩展的多信号检索方法。BM25 用于保留函数名、类名、文件名和错误信息等符号线索，语义检索用于捕获行为描述相似性，结构扩展用于发现入口节点周边的调用、继承、包含和兄弟节点关系。

BM25 处理 issue 中的精确符号；
Semantic 处理行为描述；
Structural 处理代码关系；
Issue Focus 把初始任务线索注入多轮检索。

所以你的检索是：

lexical + semantic + structural + task focus

它相对前沿工作的特色是：

不是单纯向量 RAG；
不是单纯图邻域检索；
而是面向 issue-to-code localization 的符号、语义、结构三路融合。
