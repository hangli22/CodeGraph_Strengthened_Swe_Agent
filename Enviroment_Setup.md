# CodeGraph Strengthened SWE-Agent：Ubuntu 22.04 云服务器环境配置指南（server4 实战更新版）

本文档用于在 **Ubuntu 22.04 云服务器** 上配置并运行 `CodeGraph_Strengthened_Swe_Agent` 项目。本文特别补充了 server4 配置过程中实际遇到的问题：

- SSH 私钥访问方式；
- 4 个并行进程对应 4 份代码目录；
- apt / pip / Hugging Face / Docker 镜像源配置；
- Dockerfile 内部 apt 源不能一开始用 `https` 的 CA 证书问题；
- Dockerfile 中 wheelhouse 预下载多个 `numpy` / `scipy` 固定版本导致 pip 依赖解析冲突的问题；
- SWE-bench raw requirements 本地缓存补丁；
- Lite / Verified 数据集离线缓存；
- smoke test 与正式运行前检查。

> 安全提醒：不要把真实 API Key 写入文档或发给别人。真实 key 只保存在服务器本机环境变量文件中。

---

## 0. server4 基本信息与目录规范

server4：

```text
IP: 47.96.149.209
OS: Ubuntu 22.04
SSH key: ~/.ssh/sweagent_server4.pem
```

本地 WSL 中先复制私钥：

```bash
mkdir -p ~/.ssh
cp "/mnt/c/Users/hl-pc/Desktop/毕设/CodeAgent/sweagent_server4.pem" ~/.ssh/sweagent_server4.pem
chmod 600 ~/.ssh/sweagent_server4.pem
```

以后所有 SSH / SCP 都要带 `-i`：

```bash
ssh -i ~/.ssh/sweagent_server4.pem root@47.96.149.209
```

```bash
scp -i ~/.ssh/sweagent_server4.pem local_file root@47.96.149.209:/root/
```

server4 上准备 4 份代码，用于 4 个进程并行，编号必须严格对应：

```text
process1 -> /root/CodeAgent1/files1
process2 -> /root/CodeAgent2/files2
process3 -> /root/CodeAgent3/files3
process4 -> /root/CodeAgent4/files4
```

不要混用类似 `/root/CodeAgent1/files2` 这种不匹配目录。

---

## 1. 登录后检查系统资源

```bash
cat /etc/os-release
uname -a
whoami
pwd
df -h
free -h
nproc
ping -c 3 github.com
ping -c 3 mirrors.tuna.tsinghua.edu.cn
```

server4 实测资源：约 99G 磁盘、14G 内存、4 核 CPU，可用于 4 个轻量进程，但首次 Docker / SWE-bench 镜像拉取时仍建议保守运行。

---

## 2. 配置 apt 镜像并安装基础工具

云服务器宿主机可以直接使用清华 apt 镜像。

```bash
cp /etc/apt/sources.list /etc/apt/sources.list.bak.$(date +%Y%m%d_%H%M%S)

cat > /etc/apt/sources.list <<'EOF_APT'
deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ jammy main restricted universe multiverse
deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ jammy-updates main restricted universe multiverse
deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ jammy-backports main restricted universe multiverse
deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ jammy-security main restricted universe multiverse
EOF_APT

apt update
apt install -y \
  git curl wget ca-certificates build-essential \
  gcc g++ make pkg-config \
  unzip zip tree vim nano jq \
  software-properties-common
```

验证：

```bash
git --version
curl --version
gcc --version
make --version
jq --version
```

---

## 3. 用镜像安装 Miniforge

不要直接从 GitHub 下载 Miniforge，容易慢。使用清华镜像：

```bash
cd ~
rm -f Miniforge3-Linux-x86_64.sh Miniforge3-Linux-x86_64.sh.*

wget -O Miniforge3-Linux-x86_64.sh \
  https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/Miniforge3-Linux-x86_64.sh

ls -lh Miniforge3-Linux-x86_64.sh
bash Miniforge3-Linux-x86_64.sh
```

安装时：

```text
Do you accept the license terms? -> yes
Do you wish to update your shell profile? -> yes
```

安装后：

```bash
source ~/.bashrc
conda --version
mamba --version
```

> 注意：每次执行 `source ~/.bashrc` 后，如果要继续使用项目环境，都要重新执行 `conda activate sweagent`。

---

## 4. 创建 Python 环境

```bash
conda create -n sweagent python=3.11 pip -y
conda activate sweagent

python --version
python -m pip --version
which python
```

期望 Python 为 3.11，例如：

```text
/root/miniforge3/envs/sweagent/bin/python
```

---

## 5. 配置 pip 镜像与统一环境变量文件

### 5.1 pip 镜像

```bash
mkdir -p ~/.pip

cat > ~/.pip/pip.conf <<'EOF_PIP'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
timeout = 120
retries = 5
disable-pip-version-check = true
EOF_PIP
```

### 5.2 单独环境变量文件

不要把项目环境变量散落在很多地方，统一写到 `~/.codeagent_env`：

```bash
cat > ~/.codeagent_env <<'EOF_ENV'
# ===== CodeAgent / SWE-agent environment =====

# Python UTF-8
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

# Hugging Face cache
export HF_HOME="$HOME/CodeAgent/hf_cache"

# Hugging Face mirror
export HF_ENDPOINT=https://hf-mirror.com

# pip behavior
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_DEFAULT_TIMEOUT=120
export PIP_RETRIES=5

# SWE-bench raw requirements cache
export SWEBENCH_RAW_REQ_CACHE_DIR="$HOME/CodeAgent/swebench_raw_req_cache"

# LLM / embedding API keys. Replace placeholders locally.
export UNI_API_KEY="你的_UNI_API_KEY"
export DASHSCOPE_API_KEY="你的_DASHSCOPE_API_KEY"
export DEEPSEEK_API_KEY="你的_DEEPSEEK_API_KEY"
EOF_ENV
```

挂到 `~/.bashrc`：

```bash
grep -q 'source ~/.codeagent_env' ~/.bashrc || cat >> ~/.bashrc <<'EOF_BASHRC'

# CodeAgent environment variables
source ~/.codeagent_env
EOF_BASHRC

source ~/.bashrc
conda activate sweagent
```

验证：

```bash
python -m pip config list
python - <<'PY'
import os
for k in [
    "UNI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY",
    "HF_HOME", "HF_ENDPOINT", "SWEBENCH_RAW_REQ_CACHE_DIR",
]:
    print(k, "SET" if os.environ.get(k) else "NOT SET")
PY
```

只确认 `SET / NOT SET`，不要输出真实 key。

---

## 6. 克隆 4 份代码

```bash
source ~/.bashrc
conda activate sweagent

for i in 1 2 3 4; do
  mkdir -p /root/CodeAgent${i}

  if [ -d /root/CodeAgent${i}/files${i}/.git ]; then
    echo "EXISTS: /root/CodeAgent${i}/files${i}"
  else
    echo "CLONE: /root/CodeAgent${i}/files${i}"
    git clone https://github.com/hangli22/CodeGraph_Strengthened_Swe_Agent.git /root/CodeAgent${i}/files${i}
  fi
done
```

检查 4 份代码：

```bash
for i in 1 2 3 4; do
  echo
  echo "===== CodeAgent${i}/files${i} ====="
  cd /root/CodeAgent${i}/files${i}
  git branch --show-current
  git log -1 --oneline
  git status --short
done
```

server4 实测 4 份代码均为：

```text
main
20cf766 增强了多func call解决
```

---

## 7. 安装项目依赖

4 份代码共享同一个 conda 环境，所以通用依赖装一次即可。先在 `files1` 安装：

```bash
source ~/.bashrc
conda activate sweagent

cd /root/CodeAgent1/files1

python -m pip install -U pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install swebench
python -m pip install -e ./mini-swe-agent
```

验证：

```bash
python - <<'PY'
import sys
print("python:", sys.executable)

import minisweagent
print("minisweagent:", minisweagent.__file__)

import datasets
print("datasets ok")

import litellm
print("litellm ok")

import swebench
print("swebench ok")

import networkx
print("networkx ok")
PY
```

然后给 `files2/files3/files4` 也安装本地 editable 的 mini-swe-agent：

```bash
source ~/.bashrc
conda activate sweagent

for i in 2 3 4; do
  echo
  echo "===== install editable mini-swe-agent for files${i} ====="
  cd /root/CodeAgent${i}/files${i}
  python -m pip install -e ./mini-swe-agent
done
```

检查最终指向：

```bash
python - <<'PY'
import minisweagent
print(minisweagent.__file__)
PY
```

由于同一个 conda 环境只能有一个 editable 指向，最后通常会指向 `files4`。如果 4 份代码 commit 一致，这是可以接受的。如果后续单独改某一份代码的 `mini-swe-agent`，需要进入对应目录重新执行：

```bash
python -m pip install -e ./mini-swe-agent
```

---

## 8. 安装 Docker Engine 与配置 Docker registry mirrors

云服务器上使用 Docker Engine，不是 Docker Desktop。

```bash
source ~/.bashrc
conda activate sweagent

apt update
apt install -y docker.io

systemctl enable docker
systemctl start docker

docker version
```

配置 Docker 镜像源：

```bash
mkdir -p /etc/docker

cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%Y%m%d_%H%M%S) 2>/dev/null || true

cat > /etc/docker/daemon.json <<'JSON'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me",
    "https://dockerproxy.com",
    "https://docker.1panel.live"
  ],
  "features": {
    "buildkit": true
  }
}
JSON

systemctl daemon-reload
systemctl restart docker

docker info | sed -n '/Registry Mirrors/,+10p'
```

---

## 9. 重要：修复 Dockerfile 内部 apt 镜像的 CA 证书问题

### 9.1 问题现象

如果在 Dockerfile 里一开始把 apt 源替换成：

```text
https://mirrors.tuna.tsinghua.edu.cn/ubuntu
```

构建时可能报错：

```text
No system certificates available. Try installing ca-certificates.
Certificate verification failed: The certificate is NOT trusted.
Could not handshake: Error in the certificate verification.
E: Unable to locate package ca-certificates
E: Unable to locate package curl
E: Unable to locate package git
```

### 9.2 根本原因

`ubuntu:22.04` 基础镜像在最开始阶段可能还没有可用的 `ca-certificates`。如果 apt 源一开始就是 `https`，apt 在下载包列表时需要验证 TLS 证书，但证书包还没安装，于是 HTTPS 握手失败。包列表更新失败后，后续就会出现 `Unable to locate package`。

所以：**Dockerfile 内部 apt 源在安装 ca-certificates 之前，应该先使用 http 镜像，而不是 https 镜像。**

### 9.3 正确补丁

进入 `files1`：

```bash
source ~/.bashrc
conda activate sweagent

cd /root/CodeAgent1/files1
cp Dockerfile.sweagent-multipy Dockerfile.sweagent-multipy.bak.aptmirror.$(date +%Y%m%d_%H%M%S)
```

在 `FROM ubuntu:22.04` 后插入或修正为 **http** 清华镜像：

```bash
python - <<'PY'
from pathlib import Path

path = Path("Dockerfile.sweagent-multipy")
text = path.read_text(encoding="utf-8")

marker = "FROM ubuntu:22.04\n"
insert = r'''
# Use Tsinghua Ubuntu mirror inside Docker build.
# Important: use http here, not https.
# The base ubuntu image may not have ca-certificates before apt install.
RUN sed -i 's|http://archive.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g; s|http://security.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' /etc/apt/sources.list
'''

if "mirrors.tuna.tsinghua.edu.cn/ubuntu" not in text:
    text = text.replace(marker, marker + insert + "\n", 1)
else:
    text = text.replace(
        "https://mirrors.tuna.tsinghua.edu.cn/ubuntu",
        "http://mirrors.tuna.tsinghua.edu.cn/ubuntu",
    )

path.write_text(text, encoding="utf-8")
print("patched Dockerfile.sweagent-multipy apt mirror to http")
PY
```

检查：

```bash
sed -n '1,25p' Dockerfile.sweagent-multipy
```

应看到 `http://mirrors.tuna.tsinghua.edu.cn/ubuntu`，不是 `https://...`。

---

## 10. 重要：修复 Dockerfile 中 numpy / scipy 多版本 wheelhouse 下载冲突

### 10.1 问题现象

构建 Docker 镜像时，Dockerfile 如果写成：

```bash
python -m pip download -d "${wh}" \
  "numpy==1.21.6" "numpy==1.22.4" "numpy==1.23.5" "numpy==1.24.4"
```

新版 pip 会把它理解为：在同一个解析环境里同时要求安装多个固定版本的 numpy，于是报错：

```text
ERROR: Cannot install numpy==1.21.6 and numpy==1.22.4 because these package versions have conflicting dependencies.
ERROR: ResolutionImpossible
```

`scipy` 多版本同理：

```text
ERROR: Cannot install scipy==1.7.3 and scipy==1.8.1 because these package versions have conflicting dependencies.
```

### 10.2 正确做法

**逐个版本单独 download，并使用 `--no-deps`。** 这样 pip 不会在同一个 resolver 中同时解析多个互斥版本。

在 `Dockerfile.sweagent-multipy` 里把原来的 numpy/scipy 多版本下载块替换为：

```bash
      for pkg in "numpy==1.21.6" "numpy==1.22.4" "numpy==1.23.5" "numpy==1.24.4"; do \
        python -m pip download --no-deps -d "${wh}" "$pkg" \
          || echo "[wheelhouse][warning] ${pkg} download failed for ${pybin}"; \
      done; \
      for pkg in "scipy==1.7.3" "scipy==1.8.1" "scipy==1.9.3" "scipy==1.10.1"; do \
        python -m pip download --no-deps -d "${wh}" "$pkg" \
          || echo "[wheelhouse][warning] ${pkg} download failed for ${pybin}"; \
      done; \
```

可以用以下补丁自动替换：

```bash
cd /root/CodeAgent1/files1
cp Dockerfile.sweagent-multipy Dockerfile.sweagent-multipy.bak.wheelhouse.$(date +%Y%m%d_%H%M%S)

python - <<'PY'
from pathlib import Path

path = Path("Dockerfile.sweagent-multipy")
text = path.read_text(encoding="utf-8")

old = '''      python -m pip download -d "${wh}" \\
        "numpy==1.21.6" "numpy==1.22.4" "numpy==1.23.5" "numpy==1.24.4" \\
        || echo "[wheelhouse][warning] numpy multi-version download partially failed for ${pybin}"; \\
      python -m pip download -d "${wh}" \\
        "scipy==1.7.3" "scipy==1.8.1" "scipy==1.9.3" "scipy==1.10.1" \\
        || echo "[wheelhouse][warning] scipy multi-version download partially failed for ${pybin}"; \\'''

new = '''      for pkg in "numpy==1.21.6" "numpy==1.22.4" "numpy==1.23.5" "numpy==1.24.4"; do \\
        python -m pip download --no-deps -d "${wh}" "$pkg" \\
          || echo "[wheelhouse][warning] ${pkg} download failed for ${pybin}"; \\
      done; \\
      for pkg in "scipy==1.7.3" "scipy==1.8.1" "scipy==1.9.3" "scipy==1.10.1"; do \\
        python -m pip download --no-deps -d "${wh}" "$pkg" \\
          || echo "[wheelhouse][warning] ${pkg} download failed for ${pybin}"; \\
      done; \\'''

if old not in text:
    raise SystemExit("ERROR: target block not found; check Dockerfile content around numpy/scipy download")

path.write_text(text.replace(old, new), encoding="utf-8")
print("patched Dockerfile.sweagent-multipy wheelhouse block")
PY
```

检查：

```bash
grep -n -A14 -B3 "for pkg in.*numpy" Dockerfile.sweagent-multipy
grep -n -A10 -B3 "for pkg in.*scipy" Dockerfile.sweagent-multipy
```

应看到 `for pkg in ...` 和 `--no-deps`。

---

## 11. 清理失败构建缓存并重新构建 Docker 镜像

如果 Docker build 中断或失败，先清理：

```bash
docker builder prune -af
docker image prune -af
docker container prune -f
```

重新构建：

```bash
source ~/.bashrc
conda activate sweagent

cd /root/CodeAgent1/files1

docker build \
  -f Dockerfile.sweagent-multipy \
  -t sweagent-multipy:latest \
  .
```

构建过程中如果看到：

```text
Get: ... https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu ...
```

但没有 `Certificate verification failed`，说明 CA 证书已经正常，只是 deadsnakes PPA 下载慢，可以继续等。

构建成功应看到：

```text
Successfully built ...
Successfully tagged sweagent-multipy:latest
```

验证：

```bash
docker images | grep sweagent-multipy

docker run --rm sweagent-multipy:latest bash -lc '
python3.8 --version || true
python3.9 --version || true
python3.10 --version || true
python3.11 --version || true
git --version
gcc --version
make --version
ls -R /opt/wheelhouse 2>/dev/null | head -80 || true
'
```

挂载测试：

```bash
mkdir -p /tmp/docker_mount_test
echo hello > /tmp/docker_mount_test/a.txt

docker run --rm \
  -v /tmp/docker_mount_test:/workspace/repo \
  -w /workspace/repo \
  sweagent-multipy:latest \
  bash -lc 'pwd && ls -la && cat a.txt'
```

期望：

```text
/workspace/repo
a.txt
hello
```

---

## 12. Hugging Face 镜像、Lite / Verified 缓存与 offline 模式

先在线使用 HF 镜像下载：

```bash
source ~/.bashrc
conda activate sweagent

mkdir -p "$HF_HOME" "$SWEBENCH_RAW_REQ_CACHE_DIR"
cd /root/CodeAgent1/files1

unset HF_DATASETS_OFFLINE
unset HF_HUB_OFFLINE
export HF_ENDPOINT=https://hf-mirror.com
```

清理可能残留的锁文件和不完整缓存：

```bash
find "$HF_HOME" -name "*.lock" -type f -delete
find "$HF_HOME" -name "*.incomplete" -type f -delete
find "$HF_HOME" -name "tmp*" -type d -prune -exec rm -rf {} + 2>/dev/null || true
```

下载 Lite：

```bash
python - <<'PY'
from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
print("len:", len(ds))
print("first:", ds[0]["instance_id"])
PY
```

期望：

```text
len: 300
first: astropy__astropy-12907
```

验证 Lite 离线：

```bash
HF_DATASETS_OFFLINE=1 HF_HUB_OFFLINE=1 python - <<'PY'
from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
print("offline len:", len(ds))
print("first:", ds[0]["instance_id"])
PY
```

下载 Verified：

```bash
unset HF_DATASETS_OFFLINE
unset HF_HUB_OFFLINE
export HF_ENDPOINT=https://hf-mirror.com

python - <<'PY'
from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
print("len:", len(ds))
print("first:", ds[0]["instance_id"])
PY
```

期望：

```text
len: 500
first: astropy__astropy-12907
```

验证 Verified 离线：

```bash
HF_DATASETS_OFFLINE=1 HF_HUB_OFFLINE=1 python - <<'PY'
from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
print("offline len:", len(ds))
print("first:", ds[0]["instance_id"])
PY
```

成功后默认开启 offline。为了避免多行 `EOF` 粘贴失败，可以用单行 `printf`：

```bash
printf '\n# Hugging Face offline mode after Lite / Verified cached\nexport HF_DATASETS_OFFLINE=1\nexport HF_HUB_OFFLINE=1\n' >> ~/.codeagent_env

source ~/.bashrc
conda activate sweagent

python -c 'import os; print("HF_HOME =", os.environ.get("HF_HOME")); print("HF_ENDPOINT =", os.environ.get("HF_ENDPOINT")); print("HF_DATASETS_OFFLINE =", os.environ.get("HF_DATASETS_OFFLINE")); print("HF_HUB_OFFLINE =", os.environ.get("HF_HUB_OFFLINE"))'
```

---

## 13. SWE-bench raw requirements 本地缓存补丁

这一步用于降低 GitHub raw 下载 requirements 抖动导致的评测失败概率。

### 13.1 定位并备份文件

```bash
source ~/.bashrc
conda activate sweagent
cd /root/CodeAgent1/files1

PYFILE=$(python - <<'PY'
import swebench.harness.test_spec.python as p
print(p.__file__)
PY
)

echo "PYFILE=$PYFILE"
cp "$PYFILE" "${PYFILE}.bak.$(date +%Y%m%d_%H%M%S)"
ls -lh "$PYFILE" "$PYFILE".bak.*
```

### 13.2 替换 `get_requirements_by_commit()`

```bash
python - <<'PY'
from pathlib import Path

path = Path("/root/miniforge3/envs/sweagent/lib/python3.11/site-packages/swebench/harness/test_spec/python.py")
text = path.read_text(encoding="utf-8")

start = text.index("def get_requirements_by_commit(")
end = text.index("\ndef clean_requirements(", start)

new_func = r'''def get_requirements_by_commit(repo: str, commit: str) -> str:
    """
    Get requirements.txt from repo at specific commit.

    Local patch:
    - Cache raw.githubusercontent.com requirements files under
      $SWEBENCH_RAW_REQ_CACHE_DIR or /root/CodeAgent/swebench_raw_req_cache.
    - Cache both the main requirements file and recursive -r requirements files.
    - If network download fails but cached file exists, reuse cached file.
    """
    import os
    import re
    import requests

    cache_root = os.environ.get(
        "SWEBENCH_RAW_REQ_CACHE_DIR",
        "/root/CodeAgent/swebench_raw_req_cache",
    )
    os.makedirs(cache_root, exist_ok=True)

    def _safe_cache_name(url: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "__", url)

    def _get_url_text(url: str) -> tuple[int, str]:
        cache_path = os.path.join(cache_root, _safe_cache_name(url))

        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            with open(cache_path, "r", encoding="utf-8") as f:
                return 200, f.read()

        try:
            resp = requests.get(url, headers=HEADERS, timeout=60)
            if resp.status_code == 200:
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(resp.text)
                return resp.status_code, resp.text
            return resp.status_code, resp.text
        except Exception:
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return 200, f.read()
            raise

    for req_path in MAP_REPO_TO_REQS_PATHS[repo]:
        reqs_url = posixpath.join(SWE_BENCH_URL_RAW, repo, commit, req_path)
        status_code, reqs_text = _get_url_text(reqs_url)
        if status_code == 200:
            break
    else:
        raise ValueError(
            f"Could not find requirements.txt at paths {MAP_REPO_TO_REQS_PATHS[repo]} for repo {repo} at commit {commit}"
        )

    lines = reqs_text
    original_req = []
    additional_reqs = []
    req_dir = "/".join(req_path.split("/")[:-1])

    exclude_line = lambda line: any(
        [line.strip().startswith(x) for x in ["-e .", "#", ".[test"]]
    )

    for line in lines.split("\n"):
        if line.strip().startswith("-r"):
            file_name = line[len("-r") :].strip()
            reqs_url = posixpath.join(
                SWE_BENCH_URL_RAW,
                repo,
                commit,
                req_dir,
                file_name,
            )
            status_code, extra_text = _get_url_text(reqs_url)
            if status_code == 200:
                for line_extra in extra_text.split("\n"):
                    if not exclude_line(line_extra):
                        additional_reqs.append(line_extra)
        else:
            if not exclude_line(line):
                original_req.append(line)

    additional_reqs.append("\n".join(original_req))
    all_reqs = "\n".join(additional_reqs)
    return all_reqs
'''

path.write_text(text[:start] + new_func + text[end:], encoding="utf-8")
print("patched:", path)
PY
```

检查：

```bash
python -m py_compile "$PYFILE"

python - <<'PY'
import inspect
import swebench.harness.test_spec.python as p

src = inspect.getsource(p.get_requirements_by_commit)
print("module:", p.__file__)
print("has_cache_patch:", "SWEBENCH_RAW_REQ_CACHE_DIR" in src)
print("has_timeout_60:", "timeout=60" in src)
PY
```

期望：

```text
has_cache_patch: True
has_timeout_60: True
```

### 13.3 预热 requirements 缓存

```bash
source ~/.bashrc
conda activate sweagent
cd /root/CodeAgent1/files1

mkdir -p "$SWEBENCH_RAW_REQ_CACHE_DIR"

python - <<'PY'
import os
import swebench.harness.test_spec.python as p

print("cache dir:", os.environ.get("SWEBENCH_RAW_REQ_CACHE_DIR"))

txt = p.get_requirements_by_commit(
    "matplotlib/matplotlib",
    "28289122be81e0bc0a6ee0c4c5b7343a46ce2e4e",
)

print("ok length:", len(txt))
print(txt[:300])
PY
```

检查缓存：

```bash
find "$SWEBENCH_RAW_REQ_CACHE_DIR" -type f | head -20
du -sh "$SWEBENCH_RAW_REQ_CACHE_DIR"
```

---

## 14. baseline smoke test

先只跑一个实例，验证链路：dataset 离线加载、repo 准备、Docker、LLM 调用。

```bash
source ~/.bashrc
conda activate sweagent

cd /root/CodeAgent1/files1

python mini_swe_agent_integration/run_swebench_batch.py \
  --mode baseline \
  --llm_backend deepseek \
  --model_name openai/deepseek-v4-flash \
  --subset lite \
  --split test \
  --slice 20:21 \
  --output_dir ./results/server4_baseline_smoke_20_21 \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers 1 \
  --step_limit 20 \
  --use_docker \
  --docker_image sweagent-multipy:latest \
  --redo
```

结束后检查：

```bash
ls -lh ./results/server4_baseline_smoke_20_21
find ./results/server4_baseline_smoke_20_21 -maxdepth 2 -type f | head -30
```

---

## 15. 正式运行前健康检查

```bash
source ~/.bashrc
conda activate sweagent
cd /root/CodeAgent1/files1

echo "== Python =="
which python
python --version

echo "== Env =="
echo "HF_HOME=$HF_HOME"
echo "HF_ENDPOINT=$HF_ENDPOINT"
echo "HF_DATASETS_OFFLINE=$HF_DATASETS_OFFLINE"
echo "HF_HUB_OFFLINE=$HF_HUB_OFFLINE"
echo "SWEBENCH_RAW_REQ_CACHE_DIR=$SWEBENCH_RAW_REQ_CACHE_DIR"

echo "== Dataset offline =="
python - <<'PY'
from datasets import load_dataset
print("Lite:", len(load_dataset("princeton-nlp/SWE-bench_Lite", split="test")))
print("Verified:", len(load_dataset("princeton-nlp/SWE-bench_Verified", split="test")))
PY

echo "== SWE-bench patch =="
python - <<'PY'
import inspect
import swebench.harness.test_spec.python as p
src = inspect.getsource(p.get_requirements_by_commit)
print("module:", p.__file__)
print("has_cache_patch:", "SWEBENCH_RAW_REQ_CACHE_DIR" in src)
print("has_timeout_60:", "timeout=60" in src)
PY

echo "== Docker image =="
docker images | grep sweagent-multipy

echo "== Docker mirrors =="
docker info | sed -n '/Registry Mirrors/,+10p'

echo "== Disk =="
df -h
docker system df
```

---

## 16. 预防 Docker eval 镜像问题

正式 harness 评测前，建议先预拉一个 SWE-bench eval 镜像：

```bash
docker pull swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest
```

如果开始下载 layer，并最终出现：

```text
Pull complete
```

或：

```text
Downloaded newer image
```

说明 Docker 拉 SWE-bench eval 镜像基本可用。

第一次评测建议：

```text
--max_workers 1
```

并且不要让两个 `run_evaluation` 同时第一次拉镜像。第一次会大量拉镜像、构建/启动容器，同时跑容易触发网络超时、镜像源限流或磁盘 IO 抖动。

---

## 17. 常见问题总结

### 17.1 `source ~/.bashrc` 后 conda 环境变回 base

正常。每次 source 后重新执行：

```bash
conda activate sweagent
```

### 17.2 Dockerfile 内部 apt 使用 https 清华源导致 CA 错误

错误特征：

```text
No system certificates available
Certificate verification failed
Unable to locate package ca-certificates
```

解决：Dockerfile 内 apt 源先用：

```text
http://mirrors.tuna.tsinghua.edu.cn/ubuntu
```

不要一开始用 `https://...`。

### 17.3 numpy / scipy 多版本 download 冲突

错误特征：

```text
Cannot install numpy==1.21.6 and numpy==1.22.4
ResolutionImpossible
```

解决：逐个版本单独 `pip download --no-deps`。

### 17.4 deadsnakes PPA 慢

如果日志是：

```text
Get: ... https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu ...
```

且没有 CA 证书错误，一般只是网速慢，可以等。

### 17.5 终端无法粘贴多行 EOF

用 `printf` 单行替代。例如写入 HF offline：

```bash
printf '\n# Hugging Face offline mode after Lite / Verified cached\nexport HF_DATASETS_OFFLINE=1\nexport HF_HUB_OFFLINE=1\n' >> ~/.codeagent_env
```

---

## 18. 最小检查清单

正式实验前确认：

- [ ] SSH 使用 `ssh -i ~/.ssh/sweagent_server4.pem root@47.96.149.209`
- [ ] 4 份代码存在：`/root/CodeAgent1/files1` 到 `/root/CodeAgent4/files4`
- [ ] 4 份代码 commit 一致
- [ ] `source ~/.bashrc && conda activate sweagent` 正常
- [ ] `UNI_API_KEY` / `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` 已设置
- [ ] `pip config list` 显示清华 PyPI 镜像
- [ ] `HF_ENDPOINT=https://hf-mirror.com`
- [ ] Lite / Verified 已离线加载成功
- [ ] `HF_DATASETS_OFFLINE=1` 和 `HF_HUB_OFFLINE=1`
- [ ] SWE-bench raw requirements 缓存补丁检查为 True
- [ ] Docker registry mirrors 生效
- [ ] `sweagent-multipy:latest` 构建成功
- [ ] Docker 挂载测试成功
- [ ] baseline smoke test 能正常启动

