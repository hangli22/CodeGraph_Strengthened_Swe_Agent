python mini_swe_agent_integration/run_swebench_batch.py \
  --mode baseline \
  --model_name openai/deepseek-v4-flash \
  --api_base https://uni-api.cstcloud.cn/v1 \
  --subset lite \
  --split test \
  --slice 20:30 \
  --output_dir ./results/baseline_docker \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers 1 \
  --step_limit 60 \
  --use_docker \
  --docker_image sweagent-multipy:latest \
  --redo

python mini_swe_agent_integration/run_swebench_batch.py \
  --mode baseline \
  --model_name openai/deepseek-v4-flash \
  --api_base https://uni-api.cstcloud.cn/v1 \
  --subset lite \
  --split test \
  --slice 10:30 \
  --output_dir ./results/retrieval_lihang_12 \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers 1 \
  --step_limit 60 \
  --use_docker \
  --docker_image sweagent-multipy:latest \
  --redo

以上为每次运行时的指令，可调整的参数：
--mode为使用的模式，baseline为基准模式，retrieval为检索模式
--slice后面为运行的实例编号，20:30表示第20个到第29个
model有deepseek-v3:671b，deepseek-v4-flash

## 看message的指令
python - <<'PY'
import json
from pathlib import Path

iid = "django__django-11910"
traj = Path(f"./results/retrieval_lihang_12/{iid}/{iid}.traj.json")
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

## 评测指令
python -m swebench.harness.run_evaluation   --dataset_name princeton-nlp/SWE-bench_Lite   --split test   --predictions_path ./results/retrieval_zhou_09/preds.json   --max_workers 1   --run_id retrieval_0_11

以上是评测preds的指令，注意路径名字是否正确


## 指令分发 常用命令
使用docker运行完发现repos文件夹没有权限删除：
sudo chown -R $(whoami):$(whoami) repos
将权限改为当前用户，然后再删除
确认属主：
ls -ld repos  

得到各次commit的编号：
git log --pretty=format:"%H  %an  %ad  %s" --date=short -20

切换过去：
git fetch origin
git switch main
git reset --hard <commit_id>

## 跑 SWE-bench harness 评测的必要条件：
能运行：
python -m swebench.harness.run_evaluation --help
如果不能运行，需要在当前 Python/conda 环境里安装 SWE-bench

## 跨文件夹移动文件
### 只移动文件夹中的文件
src="/home/hangli22/CodeAgent/files/cache/django__django-11630"
dst="/mnt/c/Users/hl-pc/Desktop/毕设/CodeAgent/仓库与构建存储_10个slice一组/特别保留：slice --20/cache"
mkdir -p "$dst"
shopt -s dotglob nullglob
mv "$src"/* "$dst"/

### 连文件夹一起移动
src="/home/hangli22/CodeAgent/files/cache/django__django-11630"
dst="/mnt/c/Users/hl-pc/Desktop/毕设/CodeAgent/仓库与构建存储_10个slice一组/特别保留：slice --20"
mkdir -p "$dst"
mv "$src" "$dst"/

## 


export DEEPSEEK_API_KEY="sk-670a8b43bf1c4988889c95cdc5f74ecd"

python mini_swe_agent_integration/run_swebench_batch.py \
  --mode retrieval \
  --llm_backend deepseek \
  --model_name openai/deepseek-v4-flash \
  --subset lite \
  --split test \
  --slice 0:1 \
  --output_dir ./results/smoke_ds_0_1 \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers 1 \
  --step_limit 3 \
  --use_docker \
  --docker_image sweagent-multipy:latest \
  --redo