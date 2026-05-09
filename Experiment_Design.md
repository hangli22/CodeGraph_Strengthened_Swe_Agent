实验设计：
1. 最终retriever mode VS baseline mode
   使用什么bench，是否全量
   比较正确率、平均步数、平均message长度（字符数）
   论证现阶段检索设计提高了成功率

2. 2阶段骨架retriever mode VS 全量代码图 retriever mode
   只能测部分实例
   比较正确率、运行时间、平均节点数
   论证2阶段骨架正确率在效率和正确率之间取得了平衡

消融实验：
-full
-去掉structural
-去掉semantic
-去掉issue_focus+BM25   
假如验证issue_focus+BM25不起作用，则修改检索流程：
issue_focus+BM25召回之后部分embedding，测试时间效率，token消耗减少

检索定位效果（有效定位率，不依赖于正确率，逐个分析）
-只有structural
-只有semantic
-只有issue_focus+BM25

也可以考虑去掉硬性拦截的效果（当LLM做出不规范指令的时候阻止并给出原因）
考虑去掉在Agent交互到一定步数时进行提醒修改/提交，进行对照



-两阶段构建的轻量代码图
-语义引导的代码图多层扩展（从一个入口（目前是semantic+BM25）开始，扩展两层，找出关联最大的节点）
-LLM解析issue字段和query字段，以后每次查询保留issue字段，防止检索漂移（可以权重逐渐衰减）
-检索结果字段设计，面向agent，涵盖原因等

