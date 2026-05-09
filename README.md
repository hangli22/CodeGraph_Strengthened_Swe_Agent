# CodeGraph_Strengthened_Swe_Agent （之前的文档，待更新）

## code_graph_builder
    将python仓库解析为结构化的图数据

### graph_schema.py
    定义了整个项目的核心数据结构。CodeNode代表图中的一个节点，定义了所需的所有属性；CodeEdge代表节点之间的关系；CodeGraph是基于NetworkX构建的多重有向图，提供节点遍历，邻居查询，序列化接口

### file_relations.py
    处理文件级别的关系，扫描每个python文件生成MODULE节点，根据import语句建立模块之间的IMPORTS边

### ast_relations.py
    对每个文件做AST解析，提取CLASS,FUNCTION,METHOD节点，建立CONTAINS,PARENT_CHILD,SIBLING等结构边

### call_graph.py
    分析函数调用关系，建立CALLS边

### inheritance.py
    提取继承关系，建立INHERITS边，同时分析方法重写，建立OVERRIDES边

### comment_annotator.py
    调用LLM为每个节点生成功能注释，填充node.comment字段，从而支持语义检索

### build.py
    统一入口，按顺序调用上述模块，返回完整的CodeGraph对象。BuildConfig控制各层开关

## code_graph_retriever
    接收构建好的CodeGraph，提供结构、语义检索及融合检索

### retriever_result.py
    定义检索结果，RetrieverResult包含节点基本信息，检索分数，以及原因文本分析；StructuralPosition存储结构的调用入度、出度、继承深度等拓扑信息；RetrievalResponse是完整的检索响应，提供to_agent_text()方法生成可以直接放入LLM prompt的格式化文本

### feature_extractor.py
    从CodeGraph中为每个节点提取结构特征向量，提取后做归一化，消除跨仓库差异

### structural_retriever.py
    实现GRACE路线的结构检索，回答“哪些节点在代码图中扮演着相同的角色”。目前用余弦相似度构建特征向量索引

### semantic_retriever.py
    实现基于注释(node.comment)embedding的语义检索，回答“哪些节点的功能与query在语义上最接近”

### hybrid_retriever.py
    实现二者融合检索

## mini-swe-agent-integration
    把检索能力接入mini-swe-agent，让agent可以主动调用检索工具

### retrieval_tools.py
    定义三个工具函数的实现和JSON Schema，并描述每个工具函数的作用

### retrieval_model.py
    继承LitellmModel，覆盖_query()方法，在API调用时追加三个工具的schema，覆盖_parse_actions()方法，允许检索工具名称通过

### retrieval_agent.py
    继承DefaultAgent，覆盖execute_actions()方法：检测action中是否包含tool_name字段，若是则在python层调用dispatch()执行检索，否则使用原始batch。返回格式与Envrironment.execute完全一致。trajectory序列化时追加检索工具调用统计

### prebuild.py
    在agent启动前预构建代码图和检索索引，保存到磁盘缓存。保存五类文件：图对象(pickle)，结构特征矩阵(numpy),embedding向量矩阵(numpy),节点id列表(json),构建元信息(json)

### run_swebench.py
    单个instance评测入口，串联预构建和Agent运行，输出exit_status和调用工具统计

### run_swebench_batch.py
    批量评测入口，同时支持实验组(--mode retrieval)和baseline组(--mode baseline)，其余参数完全相同以控制变量。使用浅克隆(--depth=1 --filter=blob:none)节省磁盘，每个instance运行后立即删除仓库和缓存。输出标准格式的preds.json，可直接提交sb-cli获取resolve_rate

    












