# CodeGraph Strengthened SWE-Agent 环境配置指南

本文档用于在 **Windows + WSL2 + Docker Desktop + Ubuntu 22.04** 环境下配置并运行 `CodeGraph_Strengthened_Swe_Agent` 项目。

> **安全提醒**  
> 不建议在文档中保存真实 API Key。请将下文中的占位符替换为你自己的密钥，或仅通过本机环境变量配置。
source后要conda activate
环境变量保存在专门的文件中
pip hf-cache docker要配置镜像
还要安装swebench(文档漏掉了)
docker内部pip要使用镜像

---

## 1. Windows 侧：安装 WSL2 + Ubuntu 22.04

在 **管理员 PowerShell** 中执行：

```powershell
wsl --install -d Ubuntu-22.04
```

如果已经安装过 WSL，可以检查：

```powershell
wsl -l -v
```

期望看到类似输出：

```text
NAME            STATE     VERSION
Ubuntu-22.04    Running   2
```

如果不是 WSL2，可以执行：

```powershell
wsl --set-version Ubuntu-22.04 2
```

现代 Windows 可以使用 `wsl --install` 安装 WSL，新安装的 Linux 发行版默认使用 WSL2。也可以用 `wsl -l -v` 检查版本。

如果是旧版 Windows，可能需要手动开启相关功能：

```powershell
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
```

然后重启电脑。

验证：

```powershell
wsl --status
wsl -l -v
```

---

## 2. Windows 侧：安装 Docker Desktop，并启用 WSL2 后端

安装 **Docker Desktop for Windows**。

安装完成后，打开 Docker Desktop。

### 2.1 配置 Docker 镜像源

进入：

```text
Settings → Docker Engine
```

将配置修改为：

```json
{
  "registry-mirrors": [
    "https://docker.1panel.live",
    "https://hub.rat.dev",
    "https://dockerpull.org"
  ]
}
```

然后点击：

```text
Apply & Restart
```

### 2.2 修改 Docker 磁盘位置

如果 D 盘空间更充足，可以进入：

```text
Settings → Resources → Advanced
```

将：

```text
Disk image location
```

修改到 D 盘。

### 2.3 启用 WSL2 后端

进入：

```text
Settings → General
```

勾选：

```text
Use WSL 2 based engine
```

然后进入：

```text
Settings → Resources → WSL Integration
```

开启：

```text
Ubuntu-22.04
```

最后点击：

```text
Apply & Restart
```

Docker 官方文档建议 Docker Desktop on Windows 使用 WSL2 based engine，并在 Settings 中启用 WSL integration。

在 WSL 中验证：

```bash
docker version
docker info
docker run --rm hello-world
```

成功时，`hello-world` 会打印 Docker 测试信息。

> **注意**  
> 不建议在 WSL 中单独执行 `apt install docker.io`。如果已经使用 Docker Desktop 的 WSL2 后端，在 WSL distro 内另装 Docker Engine / CLI 可能造成冲突。

---

## 3. WSL 内：安装基础系统工具

进入 Ubuntu 22.04：

```powershell
wsl -d Ubuntu-22.04
```

更新 apt：

```bash
sudo apt update
```

安装基础工具：

```bash
sudo apt install -y \
  git curl wget ca-certificates build-essential \
  gcc g++ make pkg-config \
  unzip zip tree vim nano \
  jq \
  software-properties-common
```

验证：

```bash
git --version
curl --version
gcc --version
make --version
docker version
```

---

## 4. WSL 内：安装 Miniforge / conda / mamba

建议使用 **Miniforge**，不建议使用 Anaconda。

Miniforge 是 conda-forge 社区维护的轻量发行版，官方仓库提供 Linux x86_64 安装脚本。

执行：

```bash
cd ~
wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash Miniforge3-Linux-x86_64.sh
```

安装过程中，如果出现：

```text
Do you wish to update your shell profile?
```

选择：

```text
yes
```

然后重新打开 WSL，或者执行：

```bash
source ~/.bashrc
```

验证：

```bash
conda --version
mamba --version
which python
```

如果出现：

```text
conda: command not found
```

通常是 shell 没刷新。可以执行：

```bash
source ~/miniforge3/etc/profile.d/conda.sh
conda init bash
source ~/.bashrc
```

---

## 5. 创建项目 Python 环境

建议宿主 WSL 的项目运行环境使用 Python 3.11：

```bash
conda create -n sweagent python=3.11 -y
conda activate sweagent

```
以及记得安装pip

验证：

```bash
python --version
pip --version
which python
```

期望输出类似：

```text
Python 3.11.x
/home/<user>/miniforge3/envs/sweagent/bin/python
```

---

## 6. 获取项目代码

如果从 GitHub 克隆：

```bash
mkdir -p ~/CodeAgent1
cd ~/CodeAgent1
git clone https://github.com/hangli22/CodeGraph_Strengthened_Swe_Agent.git files1
cd files1
```

如果从 Windows 复制项目，建议复制到 WSL 文件系统：

```bash
mkdir -p ~/CodeAgent/files
```

> **建议**  
> 不要长期在 `/mnt/c/Users/...` 下面跑批量实验。  
> 推荐使用 WSL 原生路径，例如 `~/CodeAgent/files`。

验证项目结构：

```bash
ls
```

你应该看到类似：

```text
Dockerfile.sweagent-multipy
requirements.txt
mini_swe_agent_integration
mini-swe-agent
code_graph_builder
code_graph_retriever
```

---

## 7. 安装项目 Python 依赖

进入项目根目录：

```bash
cd ~/CodeAgent/files
conda activate sweagent
```

升级基础构建工具：

```bash
python -m pip install -U pip setuptools wheel
```

安装项目依赖：

```bash
python -m pip install -r requirements.txt
```

建议安装本地 `mini-swe-agent`，否则容易出现：

```text
ModuleNotFoundError: No module named 'minisweagent'
```

安装命令：

```bash
python -m pip install -e ./mini-swe-agent
```

验证关键 import：

```bash
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
```

如果 `minisweagent` 导入失败，优先检查：

```bash
find . -path "*minisweagent*" -maxdepth 5
python -m pip list | grep -i swe
```

---

## 8. 配置 API Key

项目至少需要两个环境变量：

| 环境变量 | 用途 |
|---|---|
| `UNI_API_KEY` | Uni-API / OpenAI-compatible LLM 调用 |
| `DASHSCOPE_API_KEY` | DashScope embedding 调用 |

建议写入 `~/.bashrc`：

```bash
cat >> ~/.bashrc <<'EOF'

# ===== CodeAgent / SWE-agent environment =====
export UNI_API_KEY="<your-uni-api-key>"
export DASHSCOPE_API_KEY="<your-dashscope-api-key>"

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
```

刷新配置：

```bash
source ~/.bashrc
```

验证：

```bash
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
```

---

## 9. Hugging Face / SWE-bench 数据集准备

进入项目目录：

```bash
cd ~/CodeAgent/files
conda activate sweagent
```

建议将 Hugging Face 缓存路径固定到项目外的统一目录：

```bash
cat >> ~/.bashrc <<'EOF'

# Hugging Face cache for SWE-bench
export HF_HOME="$HOME/CodeAgent/hf_cache"
EOF

source ~/.bashrc
```

在线预下载 SWE-bench Lite：

```bash
python - <<'PY'
from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
print("len:", len(ds))
print("first:", ds[0]["instance_id"])
PY
```

期望输出：

```text
len: 300
first: astropy__astropy-12907
```

验证离线可用：

```bash
HF_DATASETS_OFFLINE=1 HF_HUB_OFFLINE=1 python - <<'PY'
from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
print("offline len:", len(ds))
print("first:", ds[0]["instance_id"])
PY
```

成功后，以后运行脚本时可以开启离线模式。

将下面两行写入 `~/.bashrc`：

```bash
export HF_DATASETS_OFFLINE=1
export HF_HUB_OFFLINE=1
```

---

## 10. 构建 Docker 镜像 `sweagent-multipy:latest`

进入项目目录：

```bash
cd ~/CodeAgent/files
```

构建镜像：

```bash
docker build \
  -f Dockerfile.sweagent-multipy \
  -t sweagent-multipy:latest \
  .
```

验证镜像存在：

```bash
docker images | grep sweagent-multipy
```

验证镜像中的多 Python：

```bash
docker run --rm sweagent-multipy:latest bash -lc '
python3.8 --version || true
python3.9 --version || true
python3.10 --version || true
python3.11 --version || true
'
```

验证基础工具：

```bash
docker run --rm sweagent-multipy:latest bash -lc '
git --version
gcc --version
make --version
'
```

如果 Dockerfile 中准备了 wheelhouse，可以验证：

```bash
docker run --rm sweagent-multipy:latest bash -lc '
ls -R /opt/wheelhouse 2>/dev/null | head -80 || echo "no wheelhouse"
'
```

---

## 11. 验证 Docker Desktop + WSL Volume Mount

创建测试目录：

```bash
cd ~/CodeAgent/files
mkdir -p /tmp/docker_mount_test
echo hello > /tmp/docker_mount_test/a.txt
```

运行挂载测试：

```bash
docker run --rm \
  -v /tmp/docker_mount_test:/workspace/repo \
  -w /workspace/repo \
  sweagent-multipy:latest \
  bash -lc 'pwd && ls -la && cat a.txt'
```

期望输出包含：

```text
/workspace/repo
a.txt
hello
```

这一步非常重要，因为 batch 运行时会把：

```text
宿主机 repo_path → 容器 /workspace/repo
```

---

## 12. 最终 Smoke Test

在项目目录下运行：

```bash
cd ~/CodeAgent/files
conda activate sweagent
```

执行 baseline smoke test：

```bash
python mini_swe_agent_integration/run_swebench_batch.py \
  --mode baseline \
  --model_name openai/deepseek-v4-flash \
  --api_base https://uni-api.cstcloud.cn/v1 \
  --subset lite \
  --split test \
  --slice 20:21 \
  --output_dir ./results/baseline_docker_smoke \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers 1 \
  --step_limit 60 \
  --use_docker \
  --docker_image sweagent-multipy:latest \
  --redo
```

> **注意**  
> 原文中的 `--slice 20：21` 使用了中文冒号 `：`，这里已修正为英文冒号 `:`：
>
> ```bash
> --slice 20:21
> ```

如果不报错，说明环境基本配置成功。

---

## 13. 常见问题排查

### 13.1 `conda: command not found`

执行：

```bash
source ~/miniforge3/etc/profile.d/conda.sh
conda init bash
source ~/.bashrc
```

然后重新验证：

```bash
conda --version
```

### 13.2 `ModuleNotFoundError: No module named 'minisweagent'`

在项目根目录执行：

```bash
python -m pip install -e ./mini-swe-agent
```

然后验证：

```bash
python - <<'PY'
import minisweagent
print(minisweagent.__file__)
PY
```

### 13.3 `ModuleNotFoundError: No module named 'datasets'`

通常说明 `requirements.txt` 没装好，重新执行：

```bash
cd ~/CodeAgent/files
conda activate sweagent
python -m pip install -r requirements.txt
```

或单独安装：

```bash
python -m pip install datasets
```

### 13.4 Docker 在 WSL 中不可用

先检查 Docker Desktop 是否启动。

然后确认：

```text
Settings → Resources → WSL Integration → Ubuntu-22.04
```

已经开启。

在 WSL 中验证：

```bash
docker version
docker run --rm hello-world
```

### 13.5 Hugging Face 离线模式加载失败

如果离线加载失败，先关闭离线环境变量，重新在线下载：

```bash
unset HF_DATASETS_OFFLINE
unset HF_HUB_OFFLINE
```

然后执行：

```bash
python - <<'PY'
from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
print(len(ds))
print(ds[0]["instance_id"])
PY
```

确认下载成功后，再开启离线模式。

---

## 14. 推荐目录结构

推荐最终目录大致如下：

```text
/home/<user>/CodeAgent/
├── files/
│   ├── Dockerfile.sweagent-multipy
│   ├── requirements.txt
│   ├── mini_swe_agent_integration/
│   ├── mini-swe-agent/
│   ├── code_graph_builder/
│   ├── code_graph_retriever/
│   ├── repos/
│   ├── cache/
│   └── results/
└── hf_cache/
```

其中：

| 路径 | 作用 |
|---|---|
| `~/CodeAgent/files` | 项目根目录 |
| `~/CodeAgent/files/repos` | SWE-bench 实例仓库 |
| `~/CodeAgent/files/cache` | 项目缓存 |
| `~/CodeAgent/files/results` | 实验输出 |
| `~/CodeAgent/hf_cache` | Hugging Face 数据集缓存 |

---

## 15. 最小检查清单

运行正式实验前，建议确认以下项目：

- [ ] `wsl -l -v` 显示 `Ubuntu-22.04` 且版本为 `2`
- [ ] Docker Desktop 已启动
- [ ] WSL Integration 已启用 `Ubuntu-22.04`
- [ ] WSL 中 `docker run --rm hello-world` 成功
- [ ] `conda activate sweagent` 成功
- [ ] `python --version` 为 Python 3.11.x
- [ ] `python -m pip install -r requirements.txt` 已完成
- [ ] `python -m pip install -e ./mini-swe-agent` 已完成
- [ ] `UNI_API_KEY` 已设置
- [ ] `DASHSCOPE_API_KEY` 已设置
- [ ] SWE-bench Lite 已成功下载
- [ ] Docker 镜像 `sweagent-multipy:latest` 已构建
- [ ] `--slice` 使用英文冒号，例如 `20:21`


另外还有锁机制
以下是一些可能出现的问题

一、预防 GitHub raw requirements 问题
1. 给 SWE-bench harness 打缓存补丁
这一步的目的：
让 get_requirements_by_commit() 下载到的 requirements 文件缓存到本地。以后如果 GitHub raw 抖动，只要之前缓存过，就不会失败。
执行：
PYFILE=$(python - <<'PY'import swebench.harness.test_spec.python as pprint(p.__file__)PY)echo "PYFILE=$PYFILE"cp "$PYFILE" "${PYFILE}.bak.$(date +%Y%m%d_%H%M%S)"
然后替换函数：
python - <<'PY'from pathlib import Pathpath = Path(__import__("swebench.harness.test_spec.python", fromlist=[""]).__file__)text = path.read_text(encoding="utf-8")start = text.index("def get_requirements_by_commit(")end = text.index("\ndef clean_requirements(", start)new_func = r'''def get_requirements_by_commit(repo: str, commit: str) -> str:    """    Get requirements.txt from repo at specific commit.    Local patch:    - Cache raw.githubusercontent.com requirements files under      $SWEBENCH_RAW_REQ_CACHE_DIR or /root/CodeAgent/swebench_raw_req_cache.    - Cache both the main requirements file and recursive -r requirements files.    - If network download fails but cached file exists, reuse cached file.    """    import os    import re    import requests    cache_root = os.environ.get(        "SWEBENCH_RAW_REQ_CACHE_DIR",        "/root/CodeAgent/swebench_raw_req_cache",    )    os.makedirs(cache_root, exist_ok=True)    def _safe_cache_name(url: str) -> str:        return re.sub(r"[^A-Za-z0-9_.-]+", "__", url)    def _get_url_text(url: str) -> tuple[int, str]:        cache_path = os.path.join(cache_root, _safe_cache_name(url))        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:            with open(cache_path, "r", encoding="utf-8") as f:                return 200, f.read()        try:            resp = requests.get(url, headers=HEADERS, timeout=60)            if resp.status_code == 200:                with open(cache_path, "w", encoding="utf-8") as f:                    f.write(resp.text)                return resp.status_code, resp.text            return resp.status_code, resp.text        except Exception:            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:                with open(cache_path, "r", encoding="utf-8") as f:                    return 200, f.read()            raise    for req_path in MAP_REPO_TO_REQS_PATHS[repo]:        reqs_url = posixpath.join(SWE_BENCH_URL_RAW, repo, commit, req_path)        status_code, reqs_text = _get_url_text(reqs_url)        if status_code == 200:            break    else:        raise ValueError(            f"Could not find requirements.txt at paths {MAP_REPO_TO_REQS_PATHS[repo]} for repo {repo} at commit {commit}"        )    lines = reqs_text    original_req = []    additional_reqs = []    req_dir = "/".join(req_path.split("/")[:-1])    exclude_line = lambda line: any(        [line.strip().startswith(x) for x in ["-e .", "#", ".[test"]]    )    for line in lines.split("\n"):        if line.strip().startswith("-r"):            file_name = line[len("-r") :].strip()            reqs_url = posixpath.join(                SWE_BENCH_URL_RAW,                repo,                commit,                req_dir,                file_name,            )            status_code, extra_text = _get_url_text(reqs_url)            if status_code == 200:                for line_extra in extra_text.split("\n"):                    if not exclude_line(line_extra):                        additional_reqs.append(line_extra)        else:            if not exclude_line(line):                original_req.append(line)    additional_reqs.append("\n".join(original_req))    all_reqs = "\n".join(additional_reqs)    return all_reqs'''path.write_text(text[:start] + new_func + text[end+1:], encoding="utf-8")print("patched:", path)PY
检查：
python -m py_compile "$PYFILE"python - <<'PY'import inspectimport swebench.harness.test_spec.python as psrc = inspect.getsource(p.get_requirements_by_commit)print("module:", p.__file__)print("has_cache_patch:", "SWEBENCH_RAW_REQ_CACHE_DIR" in src)print("has_timeout_60:", "timeout=60" in src)PY
正常应该看到：
has_cache_patch: Truehas_timeout_60: True
2. 设置缓存目录
建议写进 ~/.bashrc：
grep -q 'SWEBENCH_RAW_REQ_CACHE_DIR' ~/.bashrc || cat >> ~/.bashrc <<'EOF'# SWE-bench raw GitHub requirements cacheexport SWEBENCH_RAW_REQ_CACHE_DIR=/root/CodeAgent/swebench_raw_req_cacheEOFsource ~/.bashrcconda activate sweagentmkdir -p "$SWEBENCH_RAW_REQ_CACHE_DIR"
3. 预热一个已知容易触发的 requirements 文件
python - <<'PY'import swebench.harness.test_spec.python as ptxt = p.get_requirements_by_commit(    "matplotlib/matplotlib",    "28289122be81e0bc0a6ee0c4c5b7343a46ce2e4e",)print("ok length:", len(txt))print(txt[:300])PY
只要这里成功，以后同类 raw requirements 下载问题会少很多。

二、预防 Docker eval 镜像问题
1. 配置可用 Docker 镜像源
先备份：
sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%Y%m%d_%H%M%S) 2>/dev/null || true
写入镜像源：
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'{  "registry-mirrors": [    "https://docker.1ms.run",    "https://docker.xuanyuan.me",    "https://dockerproxy.com"  ],  "features": {    "buildkit": true  }}JSONsudo systemctl daemon-reloadsudo systemctl restart docker
检查：
docker info | sed -n '/Registry Mirrors/,+10p'
应该能看到：
https://docker.1ms.run/https://docker.xuanyuan.me/https://dockerproxy.com/
2. 预拉一个 SWE-bench eval 镜像测试
docker pull swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest
如果它开始下载 layer，并最终出现：
Pull complete
或者：
Downloaded newer image
说明这台服务器的 Docker 拉 SWE-bench eval 镜像基本可用。

三、正式评测前的一键健康检查
以后每台服务器跑评测前，可以先执行：
cd /root/CodeAgent/filessource ~/.bashrcconda activate sweagentecho "== Python =="which pythonpython --versionecho "== SWE-bench patch =="python - <<'PY'import inspectimport swebench.harness.test_spec.python as psrc = inspect.getsource(p.get_requirements_by_commit)print("module:", p.__file__)print("has_cache_patch:", "SWEBENCH_RAW_REQ_CACHE_DIR" in src)PYecho "== raw GitHub =="python - <<'PY'import swebench.harness.test_spec.python as ptxt = p.get_requirements_by_commit(    "matplotlib/matplotlib",    "28289122be81e0bc0a6ee0c4c5b7343a46ce2e4e",)print("requirements length:", len(txt))PYecho "== Docker mirrors =="docker info | sed -n '/Registry Mirrors/,+10p'echo "== Docker pull test =="docker pull swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latestecho "== Disk =="df -hdocker system df
这几个都通过后，再跑：
/root/miniforge3/envs/sweagent/bin/python -m swebench.harness.run_evaluation \  --dataset_name princeton-nlp/SWE-bench_Lite \  --split test \  --predictions_path results/你的结果目录/preds.json \  --max_workers 1 \  --run_id 你的run_id

四、额外建议：不要让两个 harness 同时第一次拉镜像
如果服务器上还没缓存 SWE-bench eval 镜像，第一次评测时建议：
--max_workers 1
并且不要两个 run_evaluation 同时跑。因为第一次会大量拉镜像、构建/启动容器，两个进程同时跑更容易触发网络超时、镜像源限流、磁盘 IO 抖动。
等常用 eval 镜像拉完之后，再并行会稳很多。

结论：
新服务器从头预防，就做两件事：先给 SWE-bench harness 打 raw requirements 本地缓存补丁；再配置可用 Docker registry mirrors 并预拉一个 swebench/sweb.eval... 镜像测试。