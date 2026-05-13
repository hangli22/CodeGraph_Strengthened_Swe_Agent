## 原初运行评测
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
  --mode retrieval \
  --model_name openai/deepseek-v4-flash \
  --api_base https://uni-api.cstcloud.cn/v1 \
  --subset lite \
  --split test \
  --slice 29:30 \
  --output_dir ./results/retrieval_lihang_11_2 \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers 1 \
  --step_limit 60 \
  --use_docker \
  --docker_image sweagent-multipy:latest \
  --redo

python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --split test \
  --predictions_path ./results/retrieval_lihang_11/preds.json \
  --max_workers 1 \
  --run_id retrieval_lihang_11_10_30


echo 'export DASHSCOPE_API_KEY="sk-2a0fc7cda034418288b275fa8265b428"' >> ~/.bashrc
source ~/.bashrc

以上为每次运行时的指令，可调整的参数：
--mode为使用的模式，baseline为基准模式，retrieval为检索模式
--slice后面为运行的实例编号，20:30表示第20个到第29个
model有deepseek-v3:671b，deepseek-v4-flash

## 看message的指令
python - <<'PY'
import json
from pathlib import Path

iid = "django__django-11630"
traj = Path(f"./results/retrieval_no_bm25_issue_focus_smoke_0_1/{iid}/{iid}.traj.json")
data = json.loads(traj.read_text(encoding="utf-8"))
out_path = traj.parent / f"message_300_600_{iid}.md"
CONTENT_LIMIT = 800
EXTRA_LIMIT = 2000
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

## 新评测指令
BATCH='python mini_swe_agent_integration/run_swebench_batch.py --mode retrieval --model_name openai/deepseek-v4-flash --api_base https://uni-api.cstcloud.cn/v1 --subset lite --split test --slice 20:30 --output_dir ./results/retrieval_lihang_11 --repos_dir ./repos --cache_dir ./cache --workers 1 --step_limit 60 --use_docker --docker_image sweagent-multipy:latest --redo'
python run_and_analyse.py \
  --batch-cmd "$BATCH" \
  --running-log running.md \
  --analyse-log annlyse_result.md

## 评测指令
python -m swebench.harness.run_evaluation   --dataset_name princeton-nlp/SWE-bench_Lite   --split test   --predictions_path ./results/retrieval_server_0_10/preds.json  --max_workers 1   --run_id retrieval_0_11

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

### 将slice --20:21中的文件移动到cache
dst="/home/hangli22/CodeAgent/files/cache/django__django-11630"
src="/mnt/c/Users/hl-pc/Desktop/毕设/CodeAgent/仓库与构建存储_10个slice一组/特别保留：slice --20/cache"
mkdir -p "$dst"
shopt -s dotglob nullglob
mv "$src"/* "$dst"/


### 文件夹从wsl移动到windows
src="/home/hangli22/CodeAgent/files/repos"
dst="/mnt/c/Users/hl-pc/Desktop/毕设/CodeAgent/仓库与构建存储_10个slice一组/repos/slice--10_20"
mkdir -p "$dst"
mv "$src" "$dst"/

### 将文件夹从windows移动到wsl
dst="/home/hangli22/CodeAgent/files/cache/django__django-11630"
src="/mnt/c/Users/hl-pc/Desktop/毕设/CodeAgent/仓库与构建存储_10个slice一组/特别保留：slice --20"
mkdir -p "$dst"
mv "$src" "$dst"/

### 预先准备repos
python mini_swe_agent_integration/prepare_repos.py \
  --subset lite \
  --split test \
  --slice 50:60 \
  --repos_dir ./repos \
  --workers 1

### 移动repos
repos专用：
python mini_swe_agent_integration/prepare_repos.py \
  --subset lite \
  --split test \
  --slice 70:100 \
  --repos_dir ./repos \
  --workers 1


注意是repos还是cache
cd ~/save/repos
cd ~/save/cache
cd ~/CodeAgent/files

cd ~/save/repos/slice_10_20
cd ~/save/cache/slice_10_20

将repos下复制到~/save/repos/slice_{num1}_{num2}，而非移动
rsync -a --info=progress2 /home/hangli22/CodeAgent/files/repos/ ~/save/repos/slice_60_70/
rsync -a --info=progress2 /home/hangli22/CodeAgent/files/cache/ ~/save/cache/slice_10_20/

反之，将~/save/repos/slice_{num1}_{num2}下复制到repos/cache，而非移动
rsync -a --info=progress2 ~/save/repos/slice_10_20/ /home/hangli22/CodeAgent/files/repos/
rsync -a --info=progress2 ~/save/cache/slice_20_30/ /home/hangli22/CodeAgent/files/cache/

！！ 不要忘了：
rm -rf repos

将cache文件夹中的前10条复制到~/save/cache/slice_{num1}_{num2}:

src="/home/hangli22/CodeAgent/files/cache"
dst="$HOME/save/cache/slice_10_20"
mkdir -p "$dst"
find "$src" -mindepth 1 -maxdepth 1 -type d | sort | head -10 | while read -r dir; do
  rsync -a --info=progress2 "$dir" "$dst/"
done

将后10条：
src="/home/hangli22/CodeAgent/files/cache"
dst="$HOME/save/cache/slice_20_30"
mkdir -p "$dst"
find "$src" -mindepth 1 -maxdepth 1 -type d | sort | tail -10 | while read -r dir; do
  rsync -a --info=progress2 "$dir" "$dst/"
done

# 服务器~/.bashrc配置文件：
wsl用户名称切换：
su - codeagent
登录服务器：
ssh root@<your-server-ip>

cat >> ~/.bashrc <<'EOF'

## ===== CodeAgent / SWE-agent environment =====
export UNI_API_KEY="d5f5ec7134e1d0d2f3cb5a04edcc367e9f05316a5fb0fb855f50f6f5fb93a275"
export DASHSCOPE_API_KEY="sk-2a0fc7cda034418288b275fa8265b428"

## Python UTF-8
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

## pip behavior
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_DEFAULT_TIMEOUT=60
export PIP_RETRIES=3

## Hugging Face cache for SWE-bench
export HF_HOME="$HOME/CodeAgent/hf_cache"

## Hugging Face offline mode.
## 只有确认 SWE-bench 数据集已经下载缓存后，再取消下面两行注释。
## export HF_DATASETS_OFFLINE=1
## export HF_HUB_OFFLINE=1
EOF

# 服务器ip:服务器公网ip:
8.136.135.101
#
## 
ds_api_key:sk-75b3c972d7d741c8af75dda6bd943f5b
## 测试连通性：
export DEEPSEEK_API_KEY="sk-75b3c972d7d741c8af75dda6bd943f5b"

python - <<'PY'
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

resp = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[
        {"role": "user", "content": "Say OK only."}
    ],
    temperature=0,
)

print(resp.choices[0].message.content)
PY

## ds 测试指令：(有报错)
初始29.15元
去run_and_annalyse查看新指令

## 操作服务器的指令：
连接服务器：
ssh root@8.136.135.101 
rsync -a --info=progress2 本地目录 root@服务器IP:/root/CodeAgent/files/repos/
例如：
rsync -a --info=progress2 ~/save/repos/slice_10_20/ root@8.136.135.101:/root/CodeAgent/files/repos/


这些参数可以在运行脚本前用环境变量覆盖：
服务器 SSH 地址：
SERVER=root@8.136.135.101

服务器端项目目录：
REMOTE_PROJECT=/root/CodeAgent/files

本地预构建数据根目录：
LOCAL_SAVE_ROOT=$HOME/save
默认要求：
~/save/repos/slice_20_30/
~/save/cache/slice_20_30/

本地保存服务器结果的目录：
LOCAL_RESULT_ROOT=$HOME/save/server_results

服务器结果目录名前缀：
RUN_PREFIX=retrieval_server

，例如会生成：

results/retrieval_server_20_30/
MODE=retrieval
MODEL_NAME=openai/deepseek-v4-flash
API_BASE=https://uni-api.cstcloud.cn/v1
SUBSET=lite
SPLIT=test
WORKERS=1
STEP_LIMIT=60
DOCKER_IMAGE=sweagent-multipy:latest

这些对应 run_swebench_batch.py 的运行参数。脚本内部会把它们拼成 --batch-cmd 传给 run_and_analyse.py；你的 run_and_analyse.py 本身支持通过 --batch-cmd 覆盖默认 batch 命令，并会继续执行 harness 评测与生成 summary。

SSH_OPTS=""

额外 SSH 参数，比如指定密钥：
SSH_OPTS="-i ~/.ssh/id_ed25519"


chmod +x ~/server_swe_batch.sh
之后再运行:
## 上传 slice 并让服务器后台运行
### 新版-用ds跑：
LLM_BACKEND=deepseek \
MODEL_NAME=openai/deepseek-v4-flash \
~/server_swe_batch.sh run 0 20

用uni跑：


~/server_swe_batch.sh submit 20 30
作用：
上传 ~/save/repos/slice_20_30/ 到服务器 repos/
上传 ~/save/cache/slice_20_30/ 到服务器 cache/
服务器 nohup 后台运行 slice 20:30

## 服务器多线程实验：
假设你复制了：
/root/CodeAgent/files_ds2

那么第二个实验可以这样启动：
REMOTE_PROJECT=/root/CodeAgent/files_ds2 \
RUN_PREFIX=retrieval_server_ds2 \
LLM_BACKEND=deepseek \
MODEL_NAME=openai/deepseek-v4-flash \
~/server_swe_batch.sh run 20 40

第一个任务仍然在：
/root/CodeAgent/files

第二个任务在：
/root/CodeAgent/files_ds2

这样不会互相清空 repos/cache。



## 查看服务器任务状态
~/server_swe_batch.sh status 20 30
会显示 PID、是否还在运行、最后几行日志。

## 实时查看服务器日志
~/server_swe_batch.sh tail 20 30
Ctrl + C 不会停止服务器任务。

ssh root@8.136.135.101 "cd /root/CodeAgent/files && tail -120 results/retrieval_server_20_30/running.md"
查看running.md后面内容

## 拉回运行结果
~/server_swe_batch.sh fetch 20 30
结果会保存到本地：
~/save/server_results/slice_20_30/

## 清理服务器端数据
~/server_swe_batch.sh cleanup 20 30
会清理服务器上的：
/root/CodeAgent/files/repos/*
/root/CodeAgent/files/cache/*
/root/CodeAgent/files/results/retrieval_server_20_30
需要手动输入 YES 确认。

## 一键运行：上传、启动、等待、拉回
~/server_swe_batch.sh run 20 30
它会自动：
submit -> 等待任务结束 -> fetch
但不会自动 cleanup，需要你确认结果无误后手动清理。

## 常用示例

### 默认运行：
~/server_swe_batch.sh submit 20 30

### 使用 SSH key：
SSH_OPTS="-i ~/.ssh/id_ed25519" ~/server_swe_batch.sh submit 20 30

### 改模型：
MODEL_NAME="openai/deepseek-v3:671b" ~/server_swe_batch.sh submit 20 30

### 改结果前缀：
RUN_PREFIX=retrieval_v4_flash_server ~/server_swe_batch.sh submit 20 30

### 跑 baseline：
MODE=baseline RUN_PREFIX=baseline_server ~/server_swe_batch.sh submit 20 30



## 新的控制服务器运行的脚本指令参数
Uni 后端

LLM_BACKEND=uni \
API_BASE=https://uni-api.cstcloud.cn/v1 \
MODEL_NAME=openai/deepseek-v4-flash \
~/server_swe_batch.sh run 20 30

需要：

UNI_API_KEY
OPENAI_API_KEY="$UNI_API_KEY"
DeepSeek 官方后端
LLM_BACKEND=deepseek \
API_BASE=https://api.deepseek.com \
MODEL_NAME=openai/deepseek-v4-flash \
~/server_swe_batch.sh run 20 30

需要：

DEEPSEEK_API_KEY

