## 脚本命令：
### 看所有的meessage内容：
python - <<'PY'
import json
from pathlib import Path

traj = Path("./results/retrieval_docker_smoke/astropy__astropy-7746/astropy__astropy-7746.traj.json")
data = json.loads(traj.read_text())

for i, msg in enumerate(data.get("messages", [])):
    print(f"\n=== message {i} ===")
    print("keys:", list(msg.keys()))
    print("role:", msg.get("role"))
    print("content type:", type(msg.get("content")).__name__)
    content = msg.get("content")
    if isinstance(content, str):
        print("content preview:", content[:300].replace("\n", "\\n"))
    else:
        print("content:", content)

    extra = msg.get("extra")
    if extra:
        print("extra keys:", list(extra.keys()))
        print("extra preview:", str(extra)[:500].replace("\n", "\\n"))
PY
### 读取message并自动写入文件
python - <<'PY'
import json
from pathlib import Path
from pprint import pformat

iid = "astropy__astropy-7746"

traj = Path(f"./results/retrieval_docker_smoke/{iid}/{iid}.traj.json")
data = json.loads(traj.read_text(encoding="utf-8"))

out_path = traj.parent / f"message_{iid}.md"

lines = []
lines.append(f"# Messages for {iid}\n")
lines.append(f"- Source trajectory: `{traj.name}`")
lines.append(f"- Total messages: {len(data.get('messages', []))}\n")

for i, msg in enumerate(data.get("messages", [])):
    lines.append(f"\n---\n")
    lines.append(f"## message {i}\n")

    lines.append(f"**keys:** `{list(msg.keys())}`  ")
    lines.append(f"**role:** `{msg.get('role')}`  ")
    lines.append(f"**content type:** `{type(msg.get('content')).__name__}`\n")

    content = msg.get("content")
    lines.append("### content\n")
    if isinstance(content, str):
        lines.append("```text")
        lines.append(content)
        lines.append("```")
    else:
        lines.append("```text")
        lines.append(str(content))
        lines.append("```")

    extra = msg.get("extra")
    if extra:
        lines.append("\n### extra keys\n")
        lines.append("```text")
        lines.append(str(list(extra.keys())))
        lines.append("```")

        lines.append("\n### extra preview\n")
        lines.append("```text")
        lines.append(pformat(extra, width=120)[:3000])
        lines.append("```")

    # 如果有 tool_calls / function_call，也单独打印，方便判断是否 function calling
    if msg.get("tool_calls"):
        lines.append("\n### tool_calls\n")
        lines.append("```text")
        lines.append(pformat(msg.get("tool_calls"), width=120)[:3000])
        lines.append("```")

    if msg.get("function_call"):
        lines.append("\n### function_call\n")
        lines.append("```text")
        lines.append(pformat(msg.get("function_call"), width=120)[:3000])
        lines.append("```")

out_path.write_text("\n".join(lines), encoding="utf-8")

print(f"Wrote: {out_path}")
PY
### 读取截断的message并将内容写入文件
python - <<'PY'
import json
from pathlib import Path

iid = "django__django-11848"
traj = Path(f"./results/baseline_docker_smoke/{iid}/{iid}.traj.json")
data = json.loads(traj.read_text(encoding="utf-8"))

out_path = traj.parent / f"message_300_600_{iid}.md"

CONTENT_LIMIT = 300
EXTRA_LIMIT = 600

def cut(x, limit):
    s = str(x)
    if len(s) > limit:
        return s[:limit] + "\n...[truncated]..."
    return s

lines = [
    f"# Messages for {iid}",
    f"",
    f"Source: `{traj.name}`",
    f"Total messages: {len(data.get('messages', []))}",
    "",
]

for i, msg in enumerate(data.get("messages", [])):
    content = msg.get("content")
    extra = msg.get("extra")

    lines.append("---")
    lines.append(f"## message {i}")
    lines.append(f"- role: `{msg.get('role')}`")
    lines.append(f"- keys: `{list(msg.keys())}`")
    lines.append(f"- content_type: `{type(content).__name__}`")
    lines.append("")

    lines.append("### content")
    lines.append("```text")
    lines.append(cut(content, CONTENT_LIMIT))
    lines.append("```")

    if extra:
        lines.append("")
        lines.append("### extra")
        lines.append("```text")
        lines.append(cut(extra, EXTRA_LIMIT))
        lines.append("```")

    if msg.get("tool_calls"):
        lines.append("")
        lines.append("### tool_calls")
        lines.append("```text")
        lines.append(cut(msg.get("tool_calls"), EXTRA_LIMIT))
        lines.append("```")

    lines.append("")

out_path.write_text("\n".join(lines), encoding="utf-8")
print(f"Wrote: {out_path}")
PY
### 看实际执行的command:
cd ~/CodeAgent/files

python - <<'PY'
import json
from pathlib import Path

traj = Path("./results/retrieval_struct_test/astropy__astropy-12907/astropy__astropy-12907.traj.json")
data = json.loads(traj.read_text())

for i, msg in enumerate(data.get("messages", [])):
    actions = msg.get("extra", {}).get("actions", [])
    for a in actions:
        if "command" in a:
            cmd = a["command"]
            print(f"\n--- message {i} ---")
            print(cmd)
PY
### 看实际执行过的编辑命令：
python - <<'PY'
import json
from pathlib import Path

traj = Path("./results/retrieval_submit_test/astropy__astropy-12907/astropy__astropy-12907.traj.json")
data = json.loads(traj.read_text())

keywords = ["sed -i", "write_text", "replace(", "python - <<", "git checkout", "git restore", "git clean"]

for i, msg in enumerate(data.get("messages", [])):
    actions = msg.get("extra", {}).get("actions", [])
    for a in actions:
        cmd = a.get("command", "")
        if any(k in cmd for k in keywords):
            print(f"\n--- message {i} ---")
            print(cmd)
PY
### 看命令执行结果：
python - <<'PY'
import json
from pathlib import Path

traj = Path("./results/retrieval_submit_test/astropy__astropy-12907/astropy__astropy-12907.traj.json")
data = json.loads(traj.read_text())
msgs = data.get("messages", [])

for i, msg in enumerate(msgs):
    actions = msg.get("extra", {}).get("actions", [])
    for a in actions:
        cmd = a.get("command", "")
        if "sed -i" in cmd:
            print(f"\n=== sed action at message {i} ===")
            print(cmd)
            # 通常下一条 tool message 是结果
            if i + 1 < len(msgs):
                print("\n--- next message content ---")
                print(str(msgs[i + 1].get("content", ""))[:2000])
PY

## 失败案例分析
### 案例一：
#### 命令执行过程：
1. agent 选择了脆弱的多行 sed -i 替换；
2. sed 没匹配到内容，但返回码仍是 0；
3. agent 把 returncode=0 误认为“修改成功”；
4. 后续验证环境又 build 失败，没能真正运行复现；
5. git diff 为空，但 agent 仍然提交；
6. 你的框架最终把它正确标记为 EmptyPatch。
#### 原因分析：
它暴露的是 agent 的编辑策略问题：
模型用了不可靠的编辑方式；
执行器没有检查文件是否真的变化；
模型没有根据 git diff 为空继续修复；
最后提前提交。
#### 启示：
也许可以优化现有prompt设计：
不要使用多行 sed -i；
多行编辑必须用 Python 脚本；
replace 前必须 assert old in text；
每次修改后验证git diff;
提交前必须 git diff 非空；
如果 git diff 为空，不允许提交；
### 案例二：
#### 命令执行过程：
1. 使用三次search_hybrid才命中，因为结构搜索有问题。并且有一次命中了测试文件
2. 你打印的 trajectory 里，message 10 是有 tool call 的。但是 message 6、8、10、15、17、19、23、25、27 这些
   assistant message 有些都带了普通文本 content。你的框架可能要求“只能 tool call”，所以某次输出被判定格式不合格。
   这个 message 不是原始 SWE-bench 用户问题，而是你的 agent wrapper 插入的纠错提示。
3. 修改完因为环境问题复现失败后，直接提交
#### 原因分析：
模型会按照自己想法，不一定死守规则。例如提交前必须git diff。
#### 启示：
1. 可以考虑手动增加硬性检查。但是能否模型交互过程中间打断？
2. 如何解决复现测试？
3. 可以在 prompt 里加入一条“issue decomposition”要求：
   When a bug involves an option or parameter, do not assume accepting the parameter is sufficient.
   Verify the semantic behavior that the option is supposed to control.
   For read/write formats, check both parsing and output round-trip behavior.
   但是有针对第二条实例的嫌疑
4. 检索父类/兄弟类：
   If a searched symbol appears in a related base class file, deepen/read that file before editing.
5. 让 agent 主动读取新增官方测试，而不是只看原始 issue：
   If tests related to the issue exist or are mentioned by search results, inspect them to infer expected behavior.
   Do not edit tests, but use them to understand the expected behavior.
6. 加入“错误反推”能力：从报错值反推行号逻辑：
   When a parser/converter error shows a header-like token being parsed as data, inspect header/data start line logic.
7. 失败输出摘要器
   当 bash returncode 非 0 时，自动给下一轮追加一个结构化摘要：=
   Last command failed.
   Return code: 1
   Likely failure:
   - test_rst_with_header_rows
   - ValueError: Column wave failed to convert
   - token parsed as data: float64
   Next step: inspect parser/header_rows logic.
   这样模型不会漏看长日志里的关键行。
### 案例三：
#### 命令执行过程：
1. 使用多次相似脚本复现issue，都因为环境问题执行不了
2. 查已有测试只查一个且不够关键
3. 格式错误提醒过于频繁
#### 原因分析：
这种环境问题有特异性，无法解决（除非在线下载，并且安装？但是要增加工具）
#### 启示：
1. 增加限制错误重试的prompt
2. 增加改之前看完整函数的prompt
3. 加“已有自定义逻辑不要绕过”的规则，防止它再把 CompoundModel 提前 return，绕过 _calculate_separability_matrix 一类逻辑。
4. 当 bug 出现在“多个组件组合起来以后才错”的场景时，不要急着改最外层入口函数，而要先检查“负责把子组件结果合并起来”的辅助函数。
### 案例四（12907）：
#### 命令执行过程：
1. 检索、深化、查测试文件
2. 查已有测试只查一个且不够关键
3. 空diff提交
4. cat输出太多
5. sed -i插入多行代码毁掉文件
6. cat内容太少
#### 原因分析：
#### 启示：
1. 加空diff硬性拦截
2. 增加使用减少输出的命令的prompt
3. content存在时置None
4. 尝试增加“之前 astropy import 已失败，不要重复，继续源码分析和 patch 生成”的返回？
5. 尝试硬性拦截sed -i
6. locating后输出到下一个top-level函数或类的完整内容
### 案例五（7746）：
#### 命令执行过程：
1. 先用 search_hybrid 检索，定位到 wcs_pix2world / _array_converter 相关代码和测试线索。
2. 深化并读取了 astropy/wcs/wcs.py，确认 wcs_pix2world 内部调用 _array_converter。
3. 检索结果里有相关测试，但没有充分打开测试看 expected behavior。
4. 多次尝试 import astropy，均因环境/兼容问题失败。
5. 期间多次格式错误、空 diff 提交，被策略拦截。
6. 多次复杂 python3 -c 编辑失败。
7. 最后插入了一个 empty input 判断，git diff 非空后提交。
#### 原因分析：
1. search方向基本对，但没有充分利用测试文件
2. 没有确定empty input的返回类型和预期行为
3. import失败后没有迅速切换到静态源码/测试分析
4. 编辑方式不稳定，使用python -c容易引号炸裂
#### 启示：
1. search命中测试时，必须先看测试再改源码
2. 先确认expected behavior
3. import失败后不要继续import，先用grep/sed/nl静态分析
4. 优先用heredoc/patch-style，避免python -c/多行sed
5. 不仅要求diff非空，还要求diff相关、最小、非破坏性
### 案例六（14182）：
#### 命令执行过程：
1. 开局多次出现 No tool calls found 格式错误。
2. 使用 search_hybrid 检索 RestructuredText output header_rows，命中相关测试 test_read_twoline_ReST。
3. 深化并读取 astropy/io/ascii/rst.py，定位到 RST(FixedWidth) 和 RST.__init__。
4. 未继续查看命中的测试，也未查看父类 FixedWidth.__init__ / 参数传递链。
5. 先跑了一次 git diff，为空。
6. 直接修改 RST.__init__，给它新增 header_rows=None，并传给 super().__init__。
7. 查看 git diff 非空后直接提交。
#### 原因分析：
1. 只看了子类 RST，没有确认父类是否接受 header_rows。
2. 未确认 issue 的 expected behavior 和参数应该在哪一层处理。
3. 没有复现/运行相关测试。
4. diff 非空后过早提交，缺少“diff 合理性”验证。
#### 启示：
1. 新增或转发 keyword 参数前，必须查看目标函数签名和 super() / 父类调用链。

对当前 structural_retriever 最实用的增强方向
可以分两层。
##### 第一层：相邻节点 + 签名，性价比最高
这层很值得做。返回信息可以包括：
- 当前节点签名
- 父类链上的同名方法签名
- 子类覆写方法签名
- 同类其他方法签名
- 当前函数调用到的函数签名
- 当前函数所在类的父类列表
- 关系说明：父类方法 / 子类覆写 / 同类方法 / 被当前函数调用
这已经能显著改善很多实例。
比如这次它看到：
RST.__init__(self)
父类：FixedWidth.__init__(self, delimiter_pad=None, bookend=True)
如果没有 header_rows 或 **kwargs，Agent 就会警觉。

##### 第二层：参数传递链 / super 链，效果更强
更进一步，你可以专门做一个查询模式，比如：
signature_chain
或者：
call_signature
它针对一个节点返回：
当前函数签名
当前函数内部的 calls：
  - super().__init__(...)
  - self.xxx(...)
  - ClassName.xxx(...)
每个 call 的候选目标签名
如果 call 传了 keyword 参数，标记目标是否接收该 keyword

这种对 SWE-bench 修 bug 很实用。
尤其是这些 bug 类型：
新增参数没有传下去
参数传错层
父类不接收参数
默认值没有保留
wrapper 函数漏传 kwargs
子类 override 签名不兼容
### 案例七（14365）：
#### 命令执行过程：
基本规范，但存在import失败后直接提交
#### 原因分析：
#### 启示：
import 失败可以接受，但下一步不该直接提交，而应继续静态分析或编辑。
### 案例八（14995）：
#### 命令执行过程：
1. 先用 search_hybrid 搜 NDDataRef mask propagation，初始检索不太准。
2. 又多次 search / deepen，最后定位到 astropy/nddata/mixins/ndarithmetic.py。
3. 读取 _arithmetic_mask 附近代码，但中间有一次读取范围过大，触发 Output too long。
4. 在尚未修改时尝试提交，被空 diff 策略拦截。
5. 跑 git diff 为空后，又再次尝试提交，再次被拦截。
6. 随后用 echo "大量函数内容" > astropy/nddata/mixins/ndarithmetic.py 一类方式重写文件。
7. git diff 显示灾难性结果：ndarithmetic.py 从约 516 行变成 32 行，大量源码被删除。
8. 之后还重复执行类似危险写入，最终在破坏性 diff 存在的情况下提交。
#### 原因分析：
1. 用 echo ... > file.py 重写源码文件非常危险，直接导致文件被截断。这个比复杂 python3 -c 失败更严重，属于必须硬拦截的操作。
2. 没有充分理解目标函数再改。它只看了 _arithmetic_mask 的一段，没有完整确认 mask is None、operand.mask is None、handle_mask=np.bitwise_or 时应该如何组合。
3. 输出过长后仍继续宽泛 diff。git diff 输出过长已经提示不要重复 broad command，但后续仍重复了大 diff，说明模型没有很好吸收工具反馈。
#### 启示：
1. 必须硬性拦截 > 重写源码文件
尤其是：
echo ... > file.py
cat ... > file.py
printf ... > file.py
这类对 tracked source file 的覆盖写入，应该直接拒绝。
2. 看到输出过长后，下一步必须缩小范围
对 git diff 也一样，应使用：
git diff -- file.py | sed -n '1,120p'
或直接检查统计信息，而不是重复大输出。
3. 这次初始检索不准，是因为 query 过于依赖 mask propagation 这种泛词，导致匹配到了其他 mask 相关模块。更好的做法是把 issue 里的强信号放进查询：handle_mask、np.bitwise_or、operand mask None、NDDataRef arithmetic，并尽早用 grep 精确搜索源码关键词
### 案例九（10914）：
#### 命令执行过程：
换仓库后疑似CodeGraph没有更新新仓库
#### 原因分析：
#### 启示：
更改了缓存加载逻辑
### 案例十（10924）：
#### 命令执行过程：
使用python3 -c导致多层嵌套引号错误
##### 正确流程：
用户 model 定义
    ↓
django.db.models.fields.FilePathField.__init__
    保存 path 参数
    ↓
deconstruct()
    migrations 序列化字段参数
    ↓
formfield()
    创建 forms.FilePathField
    ↓
django.forms.fields.FilePathField
    使用真实路径生成表单 choices / 校验
#### 原因分析：
#### 启示：
已经加了prompt约束，如果不行考虑硬性过滤
##### 增加生命周期结构摘要  重要！！！需要仔细设计流程
要加的是：
给某个类/方法节点，自动生成一段 参数生命周期相关的结构摘要。
它应该回答：
这个类里有哪些和参数存储、序列化、消费有关的方法？
这个参数在哪里被保存到 self.xxx？
在哪里被写入 kwargs？
在哪里被传给其他类/函数？
有没有同名的 form field / serializer field / model field？
给 类节点额外维护一个“方法索引”。
意思是：类节点：django.db.models.fields.FilePathField它包含/拥有的方法：
__init__      -> 对应方法节点 node_id
deconstruct  -> 对应方法节点 node_id
formfield    -> 对应方法节点 node_id
check        -> 如果存在，对应方法节点 node_id
...
也就是从类节点快速跳到关键方法节点。
现在你的图里应该已经有类似：
ClassNode --CONTAINS--> MethodNode
这种边。
但如果每次都让 agent 从一堆 CONTAINS 边里自己找方法，就容易漏。
所以在类节点上缓存一个映射：
method_index
它只是为了方便检索和摘要，不一定是图的核心边。

method_index 存了 node_id 后，检索方式怎么设计？

method_index 的作用不是参与向量相似度，而是作为结构导航索引。

比如类节点：

ClassNode: django.db.models.fields.FilePathField

method_index = {
    "__init__": "node_123",
    "deconstruct": "node_124",
    "formfield": "node_125",
}

它的使用方式是：

向量/语义检索先找到类节点 FilePathField
structural retrieval 拿到这个 class node
根据 method_index 快速取出类内关键方法节点
再读取这些 method node 的签名、行号、analysis metadata
生成结构摘要或候选阅读列表

也就是说：

semantic search 负责找到“哪里相关”
method_index 负责快速回答“这个类内部有哪些重要方法”
analysis metadata 负责回答“参数/属性在这些方法里怎么流动”

它不替代向量检索，而是补充结构检索。
建议不要把 method_index 塞进 semantic 向量检索，而是扩展 structural retrieval 的 mode。

比如新增几种结构查询模式：

class_methods
param_flow
symbol_flow
related_api
通用的 symbol_flow structural mode。

底层存：

method_index
method.signature
method.analysis.param_stores
method.analysis.attr_passes
method.analysis.param_passes
method.analysis.dict_serializes
method.analysis.constructs
method.analysis.returns
same_name_index

检索时：

semantic/hybrid 找节点
从 query 提取 focus symbol
用 method_index 找类内方法
用 analysis 过滤和 focus 相关的方法
扩展到构造/调用/同名类
规则生成 SymbolFlowSummary

1. query token 命中方法签名参数名，最高优先级
2. query token 命中 self.xxx 属性名
3. query token 命中 keyword 参数名或 dict key
4. query token 命中方法名/函数名
5. query token 命中类名
6. 普通自然语言词，低优先级
多个 focus 怎么办？

允许返回 top 2-3 个，不必只选一个。

比如：

focus_candidates:
1. path        score 12
2. FilePathField score 8
3. callable   score 2

然后 symbol_flow 用第一个，必要时把第二个作为 related symbol。

对复杂 issue：

handle_mask np.bitwise_or operand.mask

可以：

primary focus: handle_mask
secondary focus: mask
related symbol: bitwise_or
### 案例十一（11001）：
#### 命令执行过程：
search_hybrid("SQLCompiler.get_order_by") 正确命中 Django 的 compiler.py。
deepen_file 后定位到 SQLCompiler.get_order_by()。
读取了 ordering_parts 和 without_ordering = self.ordering_parts.search(sql).group(1) 相关代码。
用 heredoc Python 修改源码，把多行 SQL 先转成单行再做 ordering_parts.search()。
运行 git diff，看到已有非空 diff。
之后又尝试第二次修正缩进/替换，但失败：Old code not found in file。
尽管第二次修正失败，仍然直接提交。
#### 原因分析：
修改后没有充分检查 diff 是否正确
第一次 diff 已经显示新增了一行 sql_oneline = ...，但需要确认缩进、逻辑是否正确，以及是否只替换了目标位置。
二次修正失败后仍提交
message 20 尝试修正，message 21 明确失败，但 message 22 直接 submit。这是主要流程硬伤。
没有运行测试/复现
对 SQLCompiler 这种逻辑问题，至少应尝试一个最小静态/单元测试或相关测试。没有验证。
patch 质量可疑
只处理 \n，但 multiline SQL 可能还包含多个空白、缩进、换行组合；更核心的问题可能是正则 r'(.*)\s(ASC|DESC)(.*)' 对多行 SQL 的贪婪匹配/换行不匹配导致去重 key 错误。直接 ' '.join(sql.split('\n')) 是比较粗的修法。
没有查看相关测试
没搜索/读取 ORM ordering 或 RawSQL 相关测试，缺少 expected behavior 对照。
#### 启示：
二次编辑失败后禁止提交：如果最后一次编辑/修正失败，下一步必须重新读 diff 或源码，不能直接 submit。
diff 非空不等于可提交：还要确认 diff 相关、最小、缩进正确、逻辑完整。
SQL/正则类 bug 要看测试或构造复现：尤其是 multiline RawSQL，要确认去重 key 如何计算。
修改后如果 old_text 找不到，说明当前源码状态已变化：应重新读取目标片段再继续 patch。
这次没有毁文件，但属于“patch 未充分验证 + 修正失败后仍提交”的流程硬伤。
### 案例十二（11019）：
#### 命令执行过程：
search_hybrid("MediaOrderConflictWarning") 正确命中 django/forms/widgets.py::Media。
deepen_file 后读取了 MediaOrderConflictWarning 和 Media.merge() 附近代码。
中间多次出现 tool-call 格式错误。
在没有修改前先跑 git diff，为空。
随后直接尝试提交，被空 diff 拦截。
之后用 heredoc 修改：
if index > last_insert_index
改成依赖 getattr(path, "_dependencies", []) 的判断。
查看 git diff 非空后直接提交。
#### 原因分析：
没有看测试/复现 issue
这个 issue 明确给了 3 个 media 对象合并的例子，应该优先构造复现或看 Media.merge 相关测试。日志里没有搜索/读取 tests/forms_tests 里的 media 测试。
patch 明显缺乏依据
源码里的 media path 是字符串，不存在稳定的 _dependencies 属性。用 getattr(path, "_dependencies", []) 基本是臆造机制，不能解决真实排序冲突。
没有完整理解算法
Media.merge() 的核心是合并多个列表并检测相对顺序冲突。正确方向通常应理解“3 个及以上列表合并时，局部相邻约束如何处理”，而不是给字符串加不存在的 dependency 属性。
空 diff 后仍尝试提交
被 policy 拦截，说明空 diff guard 有效，但 agent 仍有过早提交倾向。
diff 非空后过早提交
没有验证、没跑测试、没确认逻辑，只要 diff 非空就提交。
#### 启示：
对 issue 自带最小例子的题，必须先复现或至少静态转写为测试思路。
对合并/排序/冲突检测类 bug，要先理解数据结构和算法约束，不能凭字段名猜属性。
diff 非空 只能说明改了文件，不说明修对；submit 前应确认 patch 有源码依据。
可以在 prompt 里补一条：不要引入源码中不存在的协议/属性/钩子，除非 issue 明确要求或已有代码使用。
这次最大硬伤是：没有验证 expected behavior，直接引入虚构 _dependencies 机制并提交。
### 案例十三（11039）：
#### 命令执行过程：
#### 原因分析：
前期检索不够精准
明明 issue 关键词是 sqlmigrate、BEGIN/COMMIT、can_rollback_ddl，但 search 多次跑到事务测试/无关 SQL 类，浪费步骤。
深化了不关键的测试文件
tests/transactions/tests.py 和 issue 有“transaction”词面相关，但不是 sqlmigrate 输出逻辑的核心。
最终定位方式其实靠 grep 更有效
grep -rn "output_transaction" django/core/management/commands/sqlmigrate.py 一下就找到了关键位置。
#### 启示：
对明确命令名/变量名的问题，优先用 grep 精确搜符号，不要反复语义检索。
search 命中不稳定时，要快速切换到源码 grep。
不要被泛词 transaction 带到无关测试；应搜索 sqlmigrate 专属测试。
submit 前最好运行或至少查看相关 sqlmigrate 测试。
这次没有空 diff、危险编辑、毁文件等硬伤；主要是前期绕远 + 验证不足。
### 案例十四（11049）：
错误消息类 bug 要优先看测试和 expected 文案。
patch 应尽量最小：只改 invalid message，不要顺手加 help_text，除非 issue/测试需要。
涉及 _() 文案时，要注意翻译和已有表述风格。
submit 前最好跑相关测试或至少 grep 现有断言。
### 案例十五（11099）：
#### 命令执行过程：
#### 原因分析：
#### 启示：
没有搜索/查看相关测试，也没有运行最小验证。
### 案例十六（11133）：
#### 命令执行过程：
#### 原因分析：
#### 启示：
没有搜索 HttpResponse / make_bytes 相关测试，可能漏掉预期风格或已有覆盖位置。
### 案例十七（11179）：
当 issue 明确给出文件和行号时，应优先 grep/sed 直接看该文件，而不是多轮语义检索。
检索被同名概念如 DeleteModel 带偏时，要及时切换到精确路径/符号搜索。
对行为修复类 patch，submit 前最好看现有测试或构造最小复现。
### 案例十八（11283）：
调查不足：只看了目标函数，没有看报错路径、相关测试、历史迁移测试或 ContentType/Permission 查询行为。
patch 依据不充分：issue 是“models recreated as a proxy”导致迁移失败，核心可能是旧权限、新权限、concrete/proxy content type 之间的冲突；只改 query 条件可能方向对，但没有验证是否覆盖 reverse 路径和自定义 permissions。
没有运行复现/测试：迁移类 bug 最好至少找 auth_tests/migration tests，或构造最小迁移状态验证。
启示
迁移 bug 不能只看迁移文件，还要查相关测试和数据状态预期。
对 RunPython migration，要同时考虑 forward/reverse、默认权限和自定义权限。
git diff 非空不等于正确，尤其是数据迁移逻辑需要复现或测试支撑。
这次主要是验证不足 + 语义链检查不足，不是破坏性流程事故。
### 案例十九（11422）：
硬编码 manage.py 是核心硬伤
manage.py 不一定在当前工作目录，也不一定叫 manage.py，Django 也可能通过其他入口启动。应该追踪实际启动脚本或命令参数，而不是固定加相对路径。
没有查 autoreload 的文件发现机制
问题关键是 iter_all_python_module_files() 为什么漏掉执行入口脚本。应该查看它如何收集 sys.modules、__main__、sys.argv[0] 等，而不是直接改 watched_files()。
没有看测试/复现
issue 有明确复现步骤，应该搜索 autoreload tests，或至少构造 sys.argv[0] / __main__ 文件路径相关逻辑。
空 diff 后仍有过早提交倾向
虽然被 policy 拦截了，但说明 agent 还在“没改就想提交”。
diff 非空后过早提交
只确认改了文件，没有确认逻辑正确、通用、可测试。
启示
对路径/入口脚本类 bug，不能硬编码文件名；要追踪运行时入口来源，如 sys.argv[0]、__main__.__file__ 或已有 reloader 入口逻辑。
对 autoreload 这类机制问题，要优先查看文件收集函数和相关 tests。
diff 非空 不代表 patch 合理，尤其是硬编码路径类改动。
可以加 prompt：不要引入硬编码项目文件名/路径，除非 issue 明确要求；优先使用运行时上下文或已有 API。
这次最大问题是：定位到相关文件后，没有理解机制，直接用硬编码 workaround 提交。
### 案例二十（11564）：
执行过程
search_hybrid("SCRIPT_NAME in STATIC_URL and MEDIA_URL") 命中 get_script_name、django/templatetags/static.py 等相关节点。
Agent 直接深化 django/templatetags/static.py，读取 static()、PrefixNode.handle_simple()、StaticNode.render() 等代码。
中间多次出现 tool-call 格式错误。
未查看 get_script_name()、set_script_prefix()、request/URL prefix 相关机制，也未查看 storage/staticfiles 相关调用。
用 heredoc 在 StaticNode.render() 里硬加：
request = context.get("request", None)
if request and "SCRIPT_NAME" in request.META:
    url = request.META["SCRIPT_NAME"] + url
git diff 非空后直接提交。
原因分析
修错层级：issue 是 STATIC_URL / MEDIA_URL 对 SCRIPT_NAME 的支持，不应只在 {% static %} 模板渲染时补；这会漏掉 Python API static()、storage URL、media prefix 等路径。
没有追踪已有机制：search 已经命中 get_script_name，但 Agent 没读它，也没查 set_script_prefix() / get_script_prefix() 这类 Django 已有 URL prefix 机制。
patch 依赖 request context，覆盖面不对：{% static %} 不一定有 request；而 issue 的语义是部署在 sub-path 下 URL 生成应正确，不该只依赖模板 context。
没有看 MEDIA_URL：issue 同时提到 STATIC_URL 和 MEDIA_URL，但最终只改了 static tag。
没有测试/复现：没有查看 static/media URL 相关测试，也没有验证 SCRIPT_NAME 下输出。
diff 非空后过早提交：patch 有明显语义漏洞，但仍直接提交。
启示
遇到“框架全局配置/环境变量支持”问题，不能只修最表层调用点，要追踪已有全局机制和所有入口。
search 命中 get_script_name 这类核心节点时，必须读取并理解，而不是跳过。
同时涉及 STATIC_URL 和 MEDIA_URL 时，要确认两个路径都覆盖。
不要引入对 request context 的局部依赖，除非 issue 明确要求模板上下文行为。
这次最大硬伤是：局部 workaround 替代框架级修复，且未覆盖 MEDIA_URL / 非模板入口。
### 案例二十一（11583）：
空 diff 后反复提交是硬伤
policy 已明确提示不能在空 diff 下提交，但 agent 多次继续 submit，说明失败反馈没有被吸收。
调查方向一度偏离
issue 是 intermittent ValueError: embedded null byte，关键是 file.stat()/pathlib 可能抛 ValueError。它却花了多步检查 /Users 不存在、resolve(strict=True)，偏向了 FileNotFoundError/路径存在性问题。
输出控制不好
直接遍历 snapshot_files() 打印所有路径，触发 Output too long。对 reloader 这种可能枚举大量文件的逻辑，应限制输出。
重复编辑/不透明编辑
后面连续两次 heredoc 编辑，日志中没有先展示 diff 或确认第一次是否已改，容易造成状态不清。不过最终 diff 看起来是小改动。
验证不足
没有构造 Path/fake object 让 stat() 抛 ValueError 的最小测试，也没查看 autoreload 相关测试。
启示
空 diff 被拒后，下一步必须源码检查或编辑，不能再 submit。
对异常处理 bug，要精准围绕异常抛出点验证，不要偏到无关路径存在性实验。
枚举文件/路径类命令必须限制输出。
修改后要立即 git diff，避免重复编辑导致状态不清。
这类 patch 应补最小验证：模拟 file.stat() 抛 ValueError，确认 snapshot_files() 跳过而不崩。
### 案例二十二（11620）：
修错层级
issue 不是要求修改内置 converter 的 to_python()，而是“用户自定义 path converter 的 to_python() 抛出 Http404 时，DEBUG 下没有 technical response”。这更可能发生在 URL resolving / exception handling 链路，不应该只改 IntConverter。
把 ValueError 语义改坏了
path converter 中 ValueError 的语义通常是“该 pattern 不匹配，继续尝试其他 URL pattern”。把它在 DEBUG 下变成 Http404 会改变 URL resolver 行为。
编辑方式虽然是 heredoc，但替换非常危险
它用全局 replace 修改所有 def to_python(self, value):，然后再替换 return int(value)，属于粗暴字符串替换，容易波及多个 converter。
diff 已显示明显问题仍提交
diff 中出现异常缩进：

+        try:
+                    return int(value)

还引用 django.conf.settings.DEBUG 和 django.http.Http404，但没有导入 django。这是提交前应立即拦截的错误。
没有复现/测试
没有构造自定义 converter 抛 Http404 的 URLconf，也没有查看 technical_404_response / resolver exception handling 相关测试。
启示
对异常处理类 issue，要沿异常传播链定位：converter → URLPattern/URLResolver.resolve → handler/debug response，而不是改异常源头。
不要用全局字符串替换修改多个同名方法；要精确 old/new 块替换。
submit 前不仅要 diff 非空，还要检查缩进、导入、语义是否明显错误。
ValueError 是 converter 匹配失败协议，不能随意改成 Http404。
这次属于典型的**“定位到相似局部节点后，没理解调用链/异常链，直接错层修复并提交”**。
### 案例二十三（11630）：
定位核心实现太晚
issue 明确给了错误码 models.E028，最直接方式应该是：
grep -r "models.E028" django/
但它绕了大量无关搜索。
搜索策略被泛词误导
db_table、validation、multiple models 这些词容易命中数据库 backend validation、测试 helper、DataError 等。真正强信号是 models.E028。
空 diff 后反复提交是硬伤
已经被提示 git diff 为空，仍然多次尝试 submit，说明流程控制失败。
编辑没有 assert old in text
修改时用了：
path.write_text(text.replace(old_text, new_text))
但没有 assert old_text in text。所以替换失败也不会报错，导致“以为改了，实际没改”。
patch 逻辑本身也很可疑
它试图用类似：
'unmanaged_models' in str(...)
来跳过检查，这明显是猜测，不是基于 Django 的 model metadata。issue 说的是“不同 app、不同 model、同表名”的合法场景，需要理解 managed、db_table、app config/model identity 的检查语义，而不是字符串匹配。
启示
遇到明确错误码时，优先全局 grep 错误码，不要先语义搜索泛词。
空 diff 被拒后，下一步必须源码检查或有效编辑，不能继续 submit。
所有字符串替换编辑必须加：
assert old_text in text
否则会出现“静默失败”。
对 model check 类问题，要理解 check 的语义和测试预期，不能用字符串 hack。
这次属于定位慢 + 空提交反复 + 编辑静默失败 + patch 方向猜测的流程硬伤。
### 案例二十四（11742）：
修错层级
issue 是 “Add check to ensure max_length fits longest choice”，通常对应 Django model checks，而不是表单字段运行时校验。应该看 django/db/models/fields 的 _check_* 方法或 django/core/checks 相关测试。
没有利用强信号：Field 是 model Field
描述里写的是 Field.max_length / Field.choices，且“mistake not noticed until database insertion”明显指数据库模型字段，不是 forms choice validation。
没有查看正确测试区域
这类 issue 应优先看 tests/invalid_models_tests、model field checks、max_length/choices 相关测试。它没有查这些，反而在 forms 里走远。
patch 语义不对
在 ChoiceField._set_choices() 中做检查，只影响表单 choice field，而且 ChoiceField 本身未必有 max_length。这不能解决 model field 的 DB 插入前检查问题。
diff 非空后过早提交
只确认有改动，没有确认改动位于正确模块，也没有测试/复现。
启示
遇到 Field.max_length / Field.choices / database insertion 这类关键词，要优先判断是 model field check。
搜索命中 forms 时不能直接开改，要与 issue 场景核对模块层级。
对 “Add check” 类 issue，要寻找已有 _check_* / models.E*** / invalid_models_tests 模式。
可以在检索中加入 focus：model Field, system check, invalid_models_tests, _check_choices, _check_max_length。
这次属于典型的：语义检索找到相似概念，但没做框架层级辨别，导致修错模块。
### 案例二十五（11797）：
定位链路不完整
issue 是“filtering on query result overrides internal query GROUP BY”，关键链路应包括 QuerySet.filter()、Query.chain()/clone()、Query.set_values()、Query.group_by、subquery 编译、where 条件引用等。这里只看了 AggregateQuery.add_subquery()，太局部。
patch 逻辑明显可疑
问题是 GROUP BY 被外部 filter 覆盖；它却在有 group_by 时清空 ordering。ordering 和 GROUP BY 不是同一个问题，修法像是凭相似 ORM 经验猜的。
没有复现 issue 给出的 SQL
issue 给了明确 Python 片段和 good/bad SQL，对 ORM 问题来说应该先复现或至少定位生成 bad SQL 的阶段。
没有查看相关测试
没搜索 aggregation, subquery, group_by, values().annotate().filter() 相关测试，缺少预期行为对照。
diff 非空后过早提交
没有运行任何验证，也没确认 clear_ordering(force_empty=True) 会不会破坏其他 aggregate subquery 行为。
启示
ORM 编译类 bug 不能只靠一个局部函数名修，要沿 Query 状态流追踪 values/annotate/filter/group_by。
issue 给出最小复现时，应优先复现并比较 SQL。
不要把 ordering 问题误当 group_by 问题；修改前要确认变量/状态确实对应 issue。
对 Query/compiler/subquery 这种核心层，要看测试或至少跑最小 SQL 生成验证。
这次属于典型的：检索不准 → 局部猜修 → 未验证提交。
### 案例二十六（11815）：
修错层级
问题发生在 migration 文件生成/序列化阶段，不是模型字段运行时默认值获取阶段。正确调查方向应是 django/db/migrations/serializer.py、MigrationWriter、deconstruct()/serializer registry。
patch 会改变运行时语义
enum default 在模型运行时应该保留 enum 对象或其字段语义；直接返回 self.default.name 可能让实际默认值从 enum value 变成 enum name，反而破坏数据库保存行为。
没有查 migration serializer
issue 明确说 “generated migration file uses value instead of name”，强信号是 migration serialization。Agent 没有 grep serializer、MigrationWriter、serialize()。
空 diff 后反复提交
policy 已提示 diff 为空，但仍多次 submit，属于明确流程硬伤。
验证不足
没有生成 migration 片段，也没查 enum default 相关 migration tests。
启示
看到 “generated migration file” 要优先定位 migration writer/serializer，而不是 runtime Field 默认值逻辑。
“默认值如何序列化”和“默认值如何运行时取值”是两条链，不能混改。
空 diff 被拒后只能继续源码检查或编辑，不能再次提交。
对 enum/default 这类问题，应检查 serializer registry 和 migration tests。
这次属于典型的：关键词命中 Field.default 后未做阶段辨别，导致修错生命周期层级。
### 案例二十七（11848）：
有流程问题，但不算严重硬伤；这次定位基本正确，问题主要是验证不足 + patch 细节可能不完整。
执行过程
search_hybrid("django.utils.http.parse_http_date two digit year") 正确命中 django/utils/http.py::parse_http_date。
deepen_file("django/utils/http.py") 后，用 grep / sed 读取了 parse_http_date() 关键代码。
又查了 tests/utils_tests/test_http.py 里的 HttpDateProcessingTests，说明有看相关测试。
第一次 heredoc 修改失败，原因是 old_text 写错导致 AssertionError。
第二次修改成功，把固定分界：
if year < 70:    year += 2000else:    year += 1900
改成了基于当前年份的 50 年窗口逻辑。
查看 git diff 非空后直接提交。
原因分析
定位正确
issue 就是 parse_http_date 的两位年份解析规则，文件和函数都找对了。
看了相关测试，这是加分项
它没有完全盲改，至少定位到 HttpDateProcessingTests。
验证不足
没有新增/运行针对两位年份 50 年规则的测试，也没有用几个边界年份手动验证。
patch 可能有边界问题
RFC 7231 的意思是：两位年份应解释为“最接近当前日期、且不超过未来 50 年”的年份。它只按当前年份的后两位比较，没有考虑当前日期的月/日，也可能在边界年附近有细节偏差。
diff 非空后略早提交
虽然 patch 方向合理，但最好先跑 tests/utils_tests/test_http.py 相关测试或最小样例。
启示
对 RFC/日期边界问题，必须验证边界条件，不能只看主逻辑。
old_text 匹配失败后应重新复制真实源码块，这次第二次修正了，问题不大。
修改解析规则后，应补充或运行对应测试。
这次没有空 diff、修错文件、危险编辑或毁文件；主要是边界验证不足。
### 案例二十八（11905）：
而是只改了 ForeignKey 构造时的局部默认值，没有追踪 migration rename/state 传递链，最后的 patch 基本没有实质修复。
执行过程
search_hybrid("ForeignKey to_field parameter in migrations") 正确命中 django/db/models/fields/related.py::ForeignKey。
deepen_file("django/db/models/fields/related.py") 后，读取了 ForeignKey.__init__() 中 to_field 初始化逻辑。
看到源码注释已经明确说：构造阶段只是“try set”，真正是否正确要等 contribute_to_class()。
直接把：
to_field = to_field or (to._meta.pk and to._meta.pk.name)
改成等价但更啰嗦的表达式，还加了 “For issue fixing” 注释。
未运行 git diff 前尝试提交，被 policy 拦截。
git diff 非空后直接提交。
原因分析
修错生命周期阶段
issue 是 migration rename PrimaryKey 后，ForeignKey 的 to_field 仍用旧字段名。关键链路应是 migration autodetector / state rendering / rename field 操作，而不是普通运行时 ForeignKey.__init__()。
patch 基本等价
原逻辑：
to_field = to_field or (to._meta.pk and to._meta.pk.name)
新逻辑本质仍是：
if to_field is None:    to_field = to._meta.pk.name
对“rename 后拿旧名”没有真正解决。
忽略源码注释强信号
注释已经说 to_field 在构造时不保证正确，正确性要到 contribute_to_class 阶段。Agent 却仍在构造阶段打补丁。
没有检查 migration 相关代码
没看 django/db/migrations/autodetector.py、state.py、operations/fields.py、RenameField、field.deconstruct() 等。
没有测试/复现
issue 给了模型和迁移场景，应至少搜索 migration tests 或构造 rename PK + FK 的 state 复现。
启示
看到 “migration / renaming field / to_field” 要优先追 migration state 和 autodetector，不要只改 runtime field constructor。
注释里出现 “won’t be guaranteed correct until ...” 是强信号，必须继续追后续生命周期方法。
patch 不能只是重写等价表达式；要确认它改变了出错路径的状态。
submit 前要判断 diff 是否真正影响 issue 链路。
这次属于典型的：找到了相关类，但没有沿 migration/state 链路追踪，局部等价修改后过早提交。
### 案例二十九（11910）：
执行过程
开局一次 tool-call 格式错误。
search_hybrid("ForeignKey to_field parameter in migrations") 正确命中 django/db/models/fields/related.py::ForeignKey。
deepen_file("django/db/models/fields/related.py") 后，读取 ForeignKey.__init__() 的 to_field 初始化代码。

源码注释已经提示：

the to_field during FK construction... won't be guaranteed to be correct until contribute_to_class is called
Agent 没继续查看 contribute_to_class()、deconstruct()、migration autodetector、state、RenameField。

直接把：

to_field = to_field or (to._meta.pk and to._meta.pk.name)

改成几乎等价的：

to_field = (to._meta.pk.name if to._meta.pk else None) if to_field is None else to_field
未 git diff 前尝试提交，被 policy 拦截。
git diff 非空后直接提交。
原因分析
修错阶段
issue 是 migration rename PrimaryKey 后 ForeignKey 的 to_field 保留旧名。关键应在 migration autodetector/state/render/deconstruct 链路，而不是普通运行时 ForeignKey.__init__()。
patch 基本无效
新旧逻辑本质都是“如果 to_field 没传，就用当前 to._meta.pk.name”。它并不能处理 rename 后 migration state 里 to_field 指向旧字段名的问题。
忽略强提示注释
源码已经说明构造阶段不保证正确，应继续追 contribute_to_class() 等后续生命周期，但 Agent 停在了局部。
没有测试/复现
issue 给了完整场景，应至少搜索 migration rename field / foreign key / to_field 相关测试，或静态追踪 RenameField 如何更新 related fields。
启示
看到 “migration + rename field + ForeignKey.to_field” 要优先追 migration state/autodetector，不要只改 model field 构造器。
源码注释出现 “won’t be guaranteed correct until X” 时，必须继续阅读 X。
diff 非空不等于有效修复；要判断是否改变了 issue 的错误链路。
对 rename/migration 类问题，必须查看 RenameField、ProjectState、ModelState、MigrationAutodetector 或相关测试。
这次属于典型的：局部相关节点命中 → 未沿生命周期扩展 → 等价修改 → 过早提交。

## 完成或正在完成的优化改进：
时间问题仍未解决，可以考虑开始过滤文件，或者经过简单检索之后排除一些文件
1. 骨架图构建时间过长（实现）->将原有的[file mudule function method]骨架图去掉method，method在deepen部分按需扩展
2. 拦截机制优化（实现）：agent提交空diff->发出提交信号后检查，如果为空则拦截；多行sed -i硬性拦截；
3. prompt层优化（实现）：禁用多行sed -i修改文件，禁用 echo ... > file.py 重写源码文件，改用heredoc、python脚本;控制一次性打印的信息长度;查看仓库中已有的test文件（如果有的话）;空diff拦截后务必查看要求修改后再提交；不能只看局部函数，参数修改要查看影响到的调用函数……
4. issue中出现关键字（实现），则需要使用BM25查询关键字而非向量搜索->已经实现了调用LLM提取issue关键字，以及BM25检索模块（尚未集成到总体流程中），可以由LLM自主决定使用BM25或是向量搜索
5. LLM自己生成的test文件往往因为环境问题无法运行（基本实现）
6. 骨架图搜索再deepen，只deepen一层，对调用链理解不够（实现）->
    deepen的时候不止单文件，对import此文件的文件也deepen，可以通过查找相似度关系来决定
    deepen时对文件内method做完整embedding，然后找对当前issue关联大的method（可以不止一个），然后要找到该method的调用链（可以通过对看和当前类在代码图上相邻的所有类，然后对相邻类进行deepen，看哪些method调用了当前method），
    最终返回method summary列表，并且说明这些method之间的关系，例如谁调用谁
    单独的issue_focus模块从issue描述中提取关键词并存入cache，之后的检索模块可以取用（函数待修改）

7. 检索结果字段设计（实现）

    node_id	方法节点 ID，例如 path/to/file.py::Class.method。
    name	方法名，例如 get_order_by。
    qualified_name	带类名的限定名，例如 SQLCompiler.get_order_by。
    file	方法所在文件。
    start_line / end_line	方法源码行号范围。
    similarity	当前 issue_query 与该 method embedding 文本的余弦相似度，用来排序 issue 相关方法。
    parent_class_id	所属类节点 ID。
    parent_class_name	所属类名。
    - calls	该方法调用出去的一跳目标，格式化后的节点引用。
    - called_by	一跳调用该方法的上游方法。
    - relation_notes	选中方法之间的局部调用关系说明。
    - short_summary	给 agent 看的短摘要，避免只返回一大段源码。
    - why_relevant	为什么该方法与当前 issue/query 相关。
    high_confidence_calls	高置信度调用边，通常只保留置信度较高的调用关系。
    call_edges	更细的调用边证据，里面包含调用表达式、解析方式、置信度等。
    unresolved_calls	没能解析到目标节点的调用表达式。
    has_full_preview	是否给这个方法保留完整 code_preview。通常只有最相关的少数方法为 True。
    signature	方法签名。
    docstring	方法 docstring 的首行或摘要。
    code_preview	方法源码预览；当前设计是“多数短摘要 + 少数完整 preview”。

8. issue_focus字段设计（实现）

    source_type	来源类型，通常是 "initial_issue" 或 "query"。
    source_text	原始文本，即 issue 原文或当前检索 query。
    exact_symbols	精确代码符号，例如 parse_http_date、FilePathField、_separable。这是最重要的字段之一。
    file_hints	文件路径或文件名提示，例如 django/utils/http.py、separable.py。
    class_hints	类名提示，例如 FilePathField、CompoundModel。
    function_hints	顶层函数名，例如 parse_http_date。
    method_hints	方法名，例如 deconstruct、formfield、__init__。
    parameter_hints	参数名或属性名，例如 allow_files、SCRIPT_NAME。
    behavior_terms	行为描述词，用于补充语义行为，例如 two digit year、nested compound model。
    error_terms	异常名、断言词、错误消息、异常值等，例如 ValueError、AssertionError。
    bm25_queries	LLM 直接生成的短 BM25 query 列表，不是一个超长 query。
    raw_keywords	其他重要关键词，作为兜底召回词。
    raw_llm_response	LLM 原始返回文本，方便调试抽取质量。
    extractor	抽取方式，通常是 "llm"；如果 LLM 失败会变成规则 fallback。
    created_at	创建时间戳。

9. 合适的prompt选择（正在调整）
   引导太多容易引发无效搜索，死循环，最后limit exceed 
   直接使用默认prompt效果较差

10. 每过一定步数提醒agent，防止limit exceed（实现）




## 一些启发：
1. 如果有生命周期摘要，调用链摘要，用处会比较大
2. 可以考虑BM25检索分数0到1，称为S_bm；向量相似度检索0到1，称为S_vec；都是相似度高则分数高。计算结果小则优先级高。
    然后用公式 S_bm+S_vec-S_bm*S_vec？
    或者考虑S_bm+(1-S_bm)^2*(1-S_vec)




