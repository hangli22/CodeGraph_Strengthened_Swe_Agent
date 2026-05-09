
## Windows 侧：安装 WSL2 + Ubuntu 22.04
在 PowerShell 管理员里执行：
wsl --install -d Ubuntu-22.04
如果已经装过 WSL，可以检查：
wsl -l -v
你希望看到类似：
NAME            STATE     VERSION
Ubuntu-22.04    Running   2
如果不是 WSL2：
wsl --set-version Ubuntu-22.04 2
微软官方文档说明，现代 Windows 可以用 wsl --install 安装 WSL，新安装的 Linux 发行版默认使用 WSL2；也可以用 wsl -l -v 检查版本。
如果你是旧版 Windows，可能需要手动开启：
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
然后重启。微软手动安装文档也说明 WSL2 需要启用 Virtual Machine Platform。
验证：
wsl --status
wsl -l -v

## Windows 侧：安装 Docker Desktop，并启用 WSL2 后端
安装 Docker Desktop for Windows。安装完成后，打开 Docker Desktop：

页面右上 settings-> 页面左 docker engine修改配置为：
{
  "registry-mirrors": [
    "https://docker.1panel.live",
    "https://hub.rat.dev",
    "https://dockerpull.org"
  ]
}
resources->Advanced可以将 Disk image location修改为 D盘（如果有空间的话）

Settings → General → 勾选 Use WSL 2 based engine。
Settings → Resources → WSL Integration → 开启 Ubuntu-22.04。
Apply & Restart。
Docker 官方文档说明 Docker Desktop on Windows 应使用 WSL2 based engine，并可在 Settings 中启用 WSL integration。
在 WSL 里验证：

docker version
docker info
docker run --rm hello-world

成功的话，hello-world 会打印 Docker 测试信息。

注意：不建议在 WSL 里单独 apt install docker.io。Docker 官方文档也提醒，使用 Docker Desktop WSL2 后端前，最好不要在 WSL distro 内另装 Docker Engine/CLI 造成冲突。

## WSL 内：安装基础系统工具
进入 Ubuntu-22.04：
wsl -d Ubuntu-22.04

更新 apt：

sudo apt update
sudo apt install -y \
  git curl wget ca-certificates build-essential \
  gcc g++ make pkg-config \
  unzip zip tree vim nano \
  jq \
  software-properties-common

验证：
git --version
curl --version
gcc --version
make --version
docker version

## WSL 内：安装 Miniforge / conda / mamba
建议 Miniforge，不建议 Anaconda。Miniforge 是 conda-forge 社区维护的轻量发行版，官方仓库提供 Linux x86_64 安装脚本。

在 WSL 里执行：

cd ~
wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash Miniforge3-Linux-x86_64.sh

安装过程中：
Do you wish to update your shell profile?
选 yes。

然后重新打开 WSL，或者：
source ~/.bashrc

验证：

conda --version
mamba --version
which python

如果 conda: command not found，一般是 shell 没刷新：

source ~/miniforge3/etc/profile.d/conda.sh
conda init bash
source ~/.bashrc

## 创建项目 Python 环境
建议宿主 WSL 的项目运行环境用 Python 3.11：

conda create -n sweagent python=3.11 -y
conda activate sweagent

验证：

python --version
pip --version
which python

应该类似：

Python 3.11.x
/home/<user>/miniforge3/envs/sweagent/bin/python

## 获取项目代码
如果是从 GitHub 克隆：

cd ~/CodeAgent
git clone https://github.com/hangli22/CodeGraph_Strengthened_Swe_Agent.git files
cd files

如果是从 Windows 复制过来，建议复制到 WSL 文件系统：mkdir -p ~/CodeAgent/files
不要长期在 /mnt/c/Users/... 下面跑批量实验。

验证项目结构：
ls

你应该看到类似：

Dockerfile.sweagent-multipy
requirements.txt
mini_swe_agent_integration
mini-swe-agent
code_graph_builder
code_graph_retriever

## 安装项目 Python 依赖
在项目根目录：

cd ~/CodeAgent/files
conda activate sweagent
python -m pip install -U pip setuptools wheel
python -m pip install -r requirements.txt

然后建议安装本地 mini-swe-agent，否则容易出现 ModuleNotFoundError: No module named 'minisweagent'
python -m pip install -e ./mini-swe-agent

验证关键 import，在命令行直接运行：

python - <<'PY'
import sys
print(sys.executable)

import minisweagent
print("minisweagent ok:", minisweagent.__file__)

import datasets
print("datasets ok")

import litellm
print("litellm ok")

import networkx
print("networkx ok")
PY

如果这里 minisweagent 失败，优先检查：

find . -path "*minisweagent*" -maxdepth 5
python -m pip list | grep -i swe

## 配置 API Key
这个项目至少需要两个 key：

UNI_API_KEY       : d5f5ec7134e1d0d2f3cb5a04edcc367e9f05316a5fb0fb855f50f6f5fb93a275
DASHSCOPE_API_KEY : sk-2a0fc7cda034418288b275fa8265b428

推荐 .bashrc 环境变量模板:

可以把这些写进 ~/.bashrc，命令行直接执行：

cat >> ~/.bashrc <<'EOF'

# ===== CodeAgent / SWE-agent environment =====
export UNI_API_KEY="d5f5ec7134e1d0d2f3cb5a04edcc367e9f05316a5fb0fb855f50f6f5fb93a275"
export DASHSCOPE_API_KEY="sk-2a0fc7cda034418288b275fa8265b428"

# Python UTF-8
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

# Hugging Face cache location
export HF_HOME="$HOME/.cache/huggingface"

# pip behavior
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_DEFAULT_TIMEOUT=60
export PIP_RETRIES=3

# Hugging Face offline mode.
# 注意：只有确认 SWE-bench_Lite 已经下载缓存后，再取消下面两行注释。
# export HF_DATASETS_OFFLINE=1
# export HF_HUB_OFFLINE=1
EOF


刷新：

source ~/.bashrc

验证：

python - <<'PY'
import os
for k in [
    "UNI_API_KEY",
    "DASHSCOPE_API_KEY",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "HF_HOME",
]:
    print(k, "SET" if os.environ.get(k) else "NOT SET")
PY


## Hugging Face / SWE-bench 数据集准备
在 WSL 里执行：

cd ~/CodeAgent/files
conda activate sweagent

把 Hugging Face 缓存路径固定到项目外的统一目录：

cat >> ~/.bashrc <<'EOF'

# Hugging Face cache for SWE-bench
export HF_HOME="$HOME/CodeAgent/hf_cache"
EOF

source ~/.bashrc

然后在线预下载 SWE-bench Lite：

python - <<'PY'
from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
print("len:", len(ds))
print("first:", ds[0]["instance_id"])
PY

期望输出：

len: 300
first: astropy__astropy-12907

然后验证离线可用：

HF_DATASETS_OFFLINE=1 HF_HUB_OFFLINE=1 python - <<'PY'
from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
print("offline len:", len(ds))
print("first:", ds[0]["instance_id"])
PY

成功后，以后运行你的脚本时可以直接开离线（也就是修改之前的~/.bashrc）：

export HF_DATASETS_OFFLINE=1
export HF_HUB_OFFLINE=1

## 构建 Docker 镜像 sweagent-multipy:latest

cd ~/CodeAgent/files

docker build \
  -f Dockerfile.sweagent-multipy \
  -t sweagent-multipy:latest \
  .

验证镜像存在：

docker images | grep sweagent-multipy

验证镜像里的多 Python：

docker run --rm sweagent-multipy:latest bash -lc '
python3.8 --version || true
python3.9 --version || true
python3.10 --version || true
python3.11 --version || true
'

验证基础工具：

docker run --rm sweagent-multipy:latest bash -lc '
git --version
gcc --version
make --version
'

因为 Dockerfile 里准备了 wheelhouse，验证：

docker run --rm sweagent-multipy:latest bash -lc '
ls -R /opt/wheelhouse 2>/dev/null | head -80 || echo "no wheelhouse"
'

## 验证 Docker Desktop + WSL volume mount

找一个小目录测试：

cd ~/CodeAgent/files
mkdir -p /tmp/docker_mount_test
echo hello > /tmp/docker_mount_test/a.txt

docker run --rm \
  -v /tmp/docker_mount_test:/workspace/repo \
  -w /workspace/repo \
  sweagent-multipy:latest \
  bash -lc 'pwd && ls -la && cat a.txt'

期望：

/workspace/repo
a.txt
hello

这一步非常重要，因为你的 batch 里会把：

宿主机 repo_path → 容器 /workspace/repo

## 最终验证
在~/.CodeAgent/files目录下，sweagent环境中，运行：

python mini_swe_agent_integration/run_swebench_batch.py \
  --mode baseline \
  --model_name openai/deepseek-v4-flash \
  --api_base https://uni-api.cstcloud.cn/v1 \
  --subset lite \
  --split test \
  --slice 20：21 \
  --output_dir ./results/baseline_docker_smoke \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers 1 \
  --step_limit 60 \
  --use_docker \
  --docker_image sweagent-multipy:latest \
  --redo

不报错，就是成功了！！
