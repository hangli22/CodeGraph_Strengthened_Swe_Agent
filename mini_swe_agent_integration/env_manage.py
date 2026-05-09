"""
env_manage.py — Agent 执行环境管理
=================================

职责：
  - 根据 SWE-bench instance / repo 文件选择 Python 版本
  - 创建 LocalEnvironment 或 DockerEnvironment
  - 为 Docker 模式创建每个 instance 独立容器
  - 将宿主机 repo 挂载到容器内
  - 处理 Docker 容器内代理环境变量
  - 在容器启动后、agent 开始前初始化 venv
  - 自动为 agent 的每条 bash 命令激活 venv

注意：
  - 代码图 prebuild / retrieval_tools 仍然在宿主机进程中运行。
  - agent 的 bash 命令在 Local 或 Docker 环境中运行。
  - Docker 模式下，repo 通过 volume mount 共享，因此容器内修改会反映到宿主机 repo。
"""

from __future__ import annotations

import configparser
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ===========================================================================
# Python 版本选择
# ===========================================================================

def choose_python_version(instance: dict, repo_path: str) -> str:
    """
    为当前 instance 选择 Python 版本。

    优先级：
      1. pyproject.toml requires-python
      2. setup.cfg python_requires
      3. tox.ini envlist
      4. instance["version"] 的简单启发
      5. 默认 3.11

    返回值格式：
      "3.8" / "3.9" / "3.10" / "3.11"

    说明：
      这是一个轻量启发式选择器，不是完整的 Python 版本约束求解器。
      最终论文评测仍建议使用 SWE-bench 官方 harness 统一验证 patch。
    """

    def pick_from_text(text: str) -> str:
        lowered = text.lower()

        # 优先看明确版本；如果出现多个版本，偏向较新的可用版本。
        if "3.11" in lowered or "py311" in lowered:
            return "3.11"
        if "3.10" in lowered or "py310" in lowered:
            return "3.10"
        if "3.9" in lowered or "py39" in lowered:
            return "3.9"
        if "3.8" in lowered or "py38" in lowered:
            return "3.8"
        return ""

    pyproject = os.path.join(repo_path, "pyproject.toml")
    if os.path.exists(pyproject):
        try:
            text = Path(pyproject).read_text(encoding="utf-8", errors="replace")
            m = re.search(r"requires-python\s*=\s*['\"]([^'\"]+)['\"]", text)
            if m:
                picked = pick_from_text(m.group(1))
                if picked:
                    return picked
        except Exception as e:
            logger.debug("读取 pyproject.toml 失败: %s", e)

    setup_cfg = os.path.join(repo_path, "setup.cfg")
    if os.path.exists(setup_cfg):
        try:
            cfg = configparser.ConfigParser()
            cfg.read(setup_cfg, encoding="utf-8")
            for section in ("options", "metadata"):
                if cfg.has_option(section, "python_requires"):
                    picked = pick_from_text(cfg.get(section, "python_requires"))
                    if picked:
                        return picked
        except Exception as e:
            logger.debug("读取 setup.cfg 失败: %s", e)

    tox_ini = os.path.join(repo_path, "tox.ini")
    if os.path.exists(tox_ini):
        try:
            text = Path(tox_ini).read_text(encoding="utf-8", errors="replace")
            picked = pick_from_text(text)
            if picked:
                return picked
        except Exception as e:
            logger.debug("读取 tox.ini 失败: %s", e)

    version = str(instance.get("version", ""))
    picked = pick_from_text(version)
    if picked:
        return picked

    return "3.11"


# ===========================================================================
# Docker proxy/env 辅助
# ===========================================================================

def _rewrite_proxy_for_docker(value: str) -> str:
    """
    将宿主机 localhost/127.0.0.1 代理地址改写为容器可访问的 host.docker.internal。

    例如：
      http://127.0.0.1:7890   -> http://host.docker.internal:7890
      http://localhost:7890   -> http://host.docker.internal:7890
      socks5://127.0.0.1:7890 -> socks5://host.docker.internal:7890
    """
    if not value:
        return value

    value = value.replace("://127.0.0.1:", "://host.docker.internal:")
    value = value.replace("://localhost:", "://host.docker.internal:")
    return value


def _build_docker_env_vars() -> tuple[dict[str, str], list[str]]:
    """
    构造传给 DockerEnvironment 的 env / forward_env。

    env:
      固定传给容器的变量，或经过改写后的代理变量。

    forward_env:
      这里默认不转发代理变量，避免把宿主机 127.0.0.1 直接带进容器。
    """
    env: dict[str, str] = {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }

    proxy_keys = [
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ]
    for key in proxy_keys:
        value = os.environ.get(key)
        if value:
            env[key] = _rewrite_proxy_for_docker(value)

    no_proxy_value = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    extra_no_proxy = "localhost,127.0.0.1,host.docker.internal"
    if no_proxy_value:
        no_proxy_value = f"{no_proxy_value},{extra_no_proxy}"
    else:
        no_proxy_value = extra_no_proxy

    env["NO_PROXY"] = no_proxy_value
    env["no_proxy"] = no_proxy_value

    # pip 镜像源通常不是 localhost 代理，可以安全显式传入。
    for key in ["PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL"]:
        value = os.environ.get(key)
        if value:
            env[key] = value

    # 不再直接 forward HTTP_PROXY/HTTPS_PROXY，避免容器内 localhost 指向错误。
    forward_env: list[str] = []
    return env, forward_env

class ActivatedDockerEnvironment:
    """
    包装 DockerEnvironment，使每条 agent 命令自动激活 venv，并暴露 repo 根目录。

    额外保证：
      - 每条命令前自动 source venv；
      - 设置 REPO_ROOT；
      - 将 repo 根目录加入 PYTHONPATH；
      - 即使 agent cd 到子目录，也能 import 当前 checkout 的源码包。
    """

    def __init__(self, base_env: Any, venv_path: str, repo_root: str):
        self.base_env = base_env
        self.venv_path = venv_path
        self.repo_root = repo_root

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")

        wrapped = (
            f"if [ -f {self.venv_path}/bin/activate ]; then "
            f". {self.venv_path}/bin/activate; "
            f"fi\n"
            f"export REPO_ROOT={self.repo_root}\n"
            f"export PYTHONPATH={self.repo_root}:${{PYTHONPATH:-}}\n"
            f"{command}"
        )

        new_action = dict(action)
        new_action["command"] = wrapped

        return self.base_env.execute(new_action, cwd=cwd, timeout=timeout)

    def cleanup(self):
        return self.base_env.cleanup()

    def serialize(self) -> dict:
        if hasattr(self.base_env, "serialize"):
            data = self.base_env.serialize()
        else:
            data = {"info": {}}

        data.setdefault("info", {})
        data["info"]["venv_path"] = self.venv_path
        data["info"]["repo_root"] = self.repo_root
        data["info"]["environment_wrapper"] = self.__class__.__name__
        return data

    def get_template_vars(self, **kwargs):
        if hasattr(self.base_env, "get_template_vars"):
            return self.base_env.get_template_vars(**kwargs)

        kwargs.setdefault("repo_root", self.repo_root)
        return kwargs

def create_docker_environment_for_instance(
    repo_path: str,
    docker_image: str,
    docker_repo_path: str,
    docker_timeout: int,
    container_timeout: str,
):
    """
    为单个 instance 创建独立 Docker 容器，并把宿主机 repo 挂载到容器内。

    注意：
      - repo_path 是宿主机路径；
      - docker_repo_path 是容器内路径；
      - 默认用宿主机 UID/GID 运行容器，避免在挂载 repo 中生成 root-owned 文件。
    """
    from minisweagent.environments.docker import DockerEnvironment

    abs_repo_path = os.path.abspath(repo_path)

    run_args = [
        "--rm",
        "--add-host", "host.docker.internal:host-gateway",
    ]

    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        run_args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])

    run_args.extend([
        "-v", f"{abs_repo_path}:{docker_repo_path}",
    ])

    env, forward_env = _build_docker_env_vars()
    env.setdefault("HOME", "/tmp")
    env.setdefault("PIP_CACHE_DIR", "/tmp/pip-cache")

    logger.info(
        "创建 DockerEnvironment | image=%s repo=%s -> %s timeout=%s container_timeout=%s user=%s",
        docker_image,
        abs_repo_path,
        docker_repo_path,
        docker_timeout,
        container_timeout,
        f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and hasattr(os, "getgid") else "default",
    )

    return DockerEnvironment(
        image=docker_image,
        cwd=docker_repo_path,
        run_args=run_args,
        env=env,
        forward_env=forward_env,
        timeout=docker_timeout,
        container_timeout=container_timeout,
    )

def read_pyproject_build_requires(repo_path: str) -> list[str]:
    """
    从宿主机 repo 的 pyproject.toml 中读取 [build-system].requires。

    返回示例：
      ["setuptools>=40.6.0", "wheel", "extension-helpers"]

    说明：
      - 这个函数在宿主机 Python 进程中运行；
      - 只负责读取文本配置；
      - 真正 pip install 发生在 Docker 容器内。
    """
    pyproject_path = os.path.join(repo_path, "pyproject.toml")
    if not os.path.exists(pyproject_path):
        return []

    try:
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        build_system = data.get("build-system", {})
        requires = build_system.get("requires", [])

        if not isinstance(requires, list):
            return []

        return [str(x) for x in requires if str(x).strip()]
    except Exception as e:
        logger.warning("读取 pyproject.toml build-system.requires 失败: %s", e)
        return []

def shell_quote_args(args: list[str]) -> str:
    """把参数列表安全拼成 shell 命令参数。"""
    import shlex
    return " ".join(shlex.quote(a) for a in args if a.strip())

def get_default_dependency_constraints(python_version: str) -> list[str]:
    """
    根据 Python 版本生成默认依赖约束。

    目标：
      - 避免旧项目被最新依赖破坏；
      - 尤其避免历史 Astropy / SciPy 类项目装到 NumPy 2.x；
      - 让所有 pip install，包括 requirements、editable install、build requirements，
        都遵循同一套约束。

    说明：
      这不是完整依赖求解器，只是 SWE-bench 批量运行时的保守默认策略。
      官方最终评测仍应以 SWE-bench harness 为准。
    """
    py = str(python_version)

    common = [
        # pytest 9 之后若有 breaking change，不让历史项目无意升级到最新大版本。
        "pytest<9",
        "hypothesis<7",

        # 很多历史项目的 Cython 代码不兼容 Cython 3。
        # 如果项目 pyproject 明确要求 cython==0.29.x，也会被这个约束允许。
        "cython<3",

        # packaging 通常兼容性较好，但也避免无意进入未来大版本。
        "packaging<25",
    ]

    if py == "3.8":
        return common + [
            # Python 3.8 已经是偏 legacy 环境，避免新工具链破坏老项目。
            "pip<25",
            "setuptools<69",
            "wheel<0.46",

            # NumPy 1.25+ 不支持 Python 3.8；SciPy 1.11+ 对 py38 也不稳。
            "numpy<1.25",
            "scipy<1.11",
        ]

    if py == "3.9":
        return common + [
            "pip<25",
            "setuptools<75",
            "wheel<0.46",

            # 关键：避免 NumPy 2.x 破坏旧 Astropy / nddata / bitmask 代码。
            "numpy<2",
            "scipy<1.14",
        ]

    if py == "3.10":
        return common + [
            "pip<25",
            "setuptools<80",
            "wheel<0.46",
            "numpy<2",
            "scipy<1.14",
        ]

    if py == "3.11":
        return common + [
            "pip<25",
            "setuptools<80",
            "wheel<0.46",
            "numpy<2",
            "scipy<1.14",
        ]

    # fallback：保守处理未知版本。
    return common + [
        "pip<25",
        "setuptools<80",
        "wheel<0.46",
        "numpy<2",
        "scipy<1.14",
    ]


def get_default_preinstall_packages(python_version: str) -> dict[str, list[str]]:
    """
    根据 Python 版本返回预安装包集合。

    注意：
      这里故意只写包名，不写版本。
      具体版本由 get_default_dependency_constraints() 统一控制。
    """
    return {
        "base": [
            "pip",
            "setuptools",
            "wheel",
            "packaging",
        ],
        "test": [
            "pytest",
            "hypothesis",
        ],
        "build": [
            "cython",
        ],
        "numeric": [
            "numpy",
            "scipy",
        ],
    }

def initialize_container_repo_environment(
    docker_env,
    repo_path_in_container: str,
    python_version: str,
    build_requires: list[str] | None = None,
) -> tuple[str, str]:
    """
    在容器内为当前 instance 初始化 Python 环境。

    策略：
      1. 每个 instance 创建独立 venv；
      2. 根据 Python 版本生成 constraints 文件；
      3. 所有 pip install 都使用同一个 constraints 文件；
      4. pip 安装优先使用 Docker 镜像内的 /opt/wheelhouse；
      5. wheelhouse 没有合适版本时，再 fallback 到在线 pip；
      6. 普通依赖安装失败只 warning，不阻止 agent 继续运行。
    """
    python_exe = f"python{python_version}"
    venv_path = "/tmp/sweagent-venv"

    build_requires = build_requires or []
    build_requires_cmd = shell_quote_args(build_requires)

    wheelhouse_tag = "py" + python_version.replace(".", "")
    wheelhouse_path = f"/opt/wheelhouse/{wheelhouse_tag}"

    constraints = get_default_dependency_constraints(python_version)
    preinstall = get_default_preinstall_packages(python_version)

    base_packages_cmd = shell_quote_args(preinstall["base"])
    test_packages_cmd = shell_quote_args(preinstall["test"])
    build_packages_cmd = shell_quote_args(preinstall["build"])
    numeric_packages_cmd = shell_quote_args(preinstall["numeric"])

    constraints_text = "\n".join(constraints)

    setup_script = f"""
set -u
cd {repo_path_in_container}

echo "[setup] Python executable: {python_exe}"
{python_exe} --version

echo "[setup] Creating venv: {venv_path}"
rm -rf {venv_path}
{python_exe} -m venv {venv_path}

. {venv_path}/bin/activate

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_DEFAULT_TIMEOUT="${{PIP_DEFAULT_TIMEOUT:-60}}"
export PIP_RETRIES="${{PIP_RETRIES:-3}}"
export WHEELHOUSE="{wheelhouse_path}"
export PIP_STEP_TIMEOUT="${{PIP_STEP_TIMEOUT:-300}}"
export CONSTRAINTS_FILE="/tmp/sweagent-constraints.txt"
export REPO_ROOT="{repo_path_in_container}"
export PYTHONPATH="{repo_path_in_container}:${{PYTHONPATH:-}}"

echo "[setup] Writing dependency constraints: $CONSTRAINTS_FILE"
python - <<'PY'
from pathlib import Path

constraints = {constraints_text!r}
path = Path("/tmp/sweagent-constraints.txt")
path.write_text(constraints + "\\n", encoding="utf-8")

print("[setup] constraints file:", path)
print(path.read_text())
PY

echo "[setup] Adding repo root to venv site-packages via .pth"
python - <<'PY'
import sysconfig
from pathlib import Path

repo_root = "{repo_path_in_container}"
site_packages = Path(sysconfig.get_paths()["purelib"])
site_packages.mkdir(parents=True, exist_ok=True)

pth = site_packages / "sweagent_repo_root.pth"
pth.write_text(repo_root + "\\n", encoding="utf-8")

print("[setup] wrote", pth, "->", repo_root)
PY

echo "[setup] Wheelhouse: $WHEELHOUSE"
if [ -d "$WHEELHOUSE" ]; then
  echo "[setup] Wheelhouse exists"
else
  echo "[setup][warning] Wheelhouse not found: $WHEELHOUSE"
fi

pip_online_install() {{
  echo "[setup] Fallback to online pip install: $*"
  if [ -s "$CONSTRAINTS_FILE" ]; then
    timeout "$PIP_STEP_TIMEOUT" python -m pip install --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" -c "$CONSTRAINTS_FILE" "$@"
  else
    timeout "$PIP_STEP_TIMEOUT" python -m pip install --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" "$@"
  fi
}}

pip_install_local_then_online() {{
  echo "[setup] Try local wheelhouse first: $*"

  if [ -d "$WHEELHOUSE" ]; then
    if [ -s "$CONSTRAINTS_FILE" ]; then
      if python -m pip install --no-index --find-links="$WHEELHOUSE" -c "$CONSTRAINTS_FILE" "$@"; then
        echo "[setup] local wheelhouse install succeeded: $*"
        return 0
      fi
    else
      if python -m pip install --no-index --find-links="$WHEELHOUSE" "$@"; then
        echo "[setup] local wheelhouse install succeeded: $*"
        return 0
      fi
    fi
    echo "[setup][warning] local wheelhouse install failed: $*"
  else
    echo "[setup][warning] skip local wheelhouse because directory is missing: $WHEELHOUSE"
  fi

  pip_online_install "$@"
}}

pip_install_req_local_then_online() {{
  req_file="$1"
  echo "[setup] Try local wheelhouse for requirements: $req_file"

  if [ -d "$WHEELHOUSE" ]; then
    if [ -s "$CONSTRAINTS_FILE" ]; then
      if python -m pip install --no-index --find-links="$WHEELHOUSE" -c "$CONSTRAINTS_FILE" -r "$req_file"; then
        echo "[setup] requirements installed from local wheelhouse: $req_file"
        return 0
      fi
    else
      if python -m pip install --no-index --find-links="$WHEELHOUSE" -r "$req_file"; then
        echo "[setup] requirements installed from local wheelhouse: $req_file"
        return 0
      fi
    fi
    echo "[setup][warning] local requirements install failed: $req_file"
  else
    echo "[setup][warning] skip local requirements install because wheelhouse is missing: $WHEELHOUSE"
  fi

  echo "[setup] Fallback to online requirements install: $req_file"
  if [ -s "$CONSTRAINTS_FILE" ]; then
    timeout "$PIP_STEP_TIMEOUT" python -m pip install --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" -c "$CONSTRAINTS_FILE" -r "$req_file"
  else
    timeout "$PIP_STEP_TIMEOUT" python -m pip install --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" -r "$req_file"
  fi
}}

pip_editable_local_then_online() {{
  echo "[setup] Try editable install using local wheelhouse only"
  if [ -d "$WHEELHOUSE" ]; then
    if [ -s "$CONSTRAINTS_FILE" ]; then
      if python -m pip install -e . --no-index --find-links="$WHEELHOUSE" -c "$CONSTRAINTS_FILE"; then
        echo "[setup] editable install succeeded using local wheelhouse"
        return 0
      fi
    else
      if python -m pip install -e . --no-index --find-links="$WHEELHOUSE"; then
        echo "[setup] editable install succeeded using local wheelhouse"
        return 0
      fi
    fi
    echo "[setup][warning] editable install using local wheelhouse failed"
  else
    echo "[setup][warning] skip local editable install because wheelhouse is missing: $WHEELHOUSE"
  fi

  echo "[setup] Fallback to editable install with online access"
  if [ -s "$CONSTRAINTS_FILE" ]; then
    timeout "$PIP_STEP_TIMEOUT" python -m pip install --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" -c "$CONSTRAINTS_FILE" -e .
  else
    timeout "$PIP_STEP_TIMEOUT" python -m pip install --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" -e .
  fi
}}

echo "[setup] Installing base tools"
pip_install_local_then_online {base_packages_cmd} || echo "[setup][warning] base tools install failed"

echo "[setup] Installing common test packages"
pip_install_local_then_online {test_packages_cmd} || echo "[setup][warning] common test package install failed"

echo "[setup] Installing common build packages"
pip_install_local_then_online {build_packages_cmd} || echo "[setup][warning] common build package install failed"

echo "[setup] Installing common numeric packages"
pip_install_local_then_online {numeric_packages_cmd} || echo "[setup][warning] common numeric package install failed"

if [ -f requirements.txt ]; then
  echo "[setup] Installing requirements.txt with constraints"
  pip_install_req_local_then_online requirements.txt || echo "[setup][warning] requirements.txt install failed"
fi

if [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ]; then
  echo "[setup] First try: editable install"
  if pip_editable_local_then_online; then
    echo "[setup] editable install succeeded"
  else
    echo "[setup][warning] editable install failed"
    echo "[setup] Fallback: prepare conservative build environment for --no-build-isolation"

    pip_install_local_then_online setuptools wheel packaging || echo "[setup][warning] conservative build tools install failed"

    if [ -n "{build_requires_cmd}" ]; then
      echo "[setup] Installing pyproject build-system.requires into current venv"
      pip_install_local_then_online {build_requires_cmd} || echo "[setup][warning] build-system.requires install failed"
    else
      echo "[setup] No pyproject build-system.requires found or parsed"
    fi

    echo "[setup] Retry: editable install with --no-build-isolation"
    if [ -s "$CONSTRAINTS_FILE" ]; then
      if timeout "$PIP_STEP_TIMEOUT" python -m pip install --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" -c "$CONSTRAINTS_FILE" -e . --no-build-isolation; then
        echo "[setup] editable install succeeded with --no-build-isolation"
      else
        echo "[setup][warning] editable install without build isolation failed"
        echo "[setup][warning] retry legacy setup.py develop if setup.py exists"

        if [ -f setup.py ]; then
          timeout "$PIP_STEP_TIMEOUT" python setup.py develop || echo "[setup][warning] setup.py develop also failed"
        else
          echo "[setup][warning] no setup.py, skip legacy develop fallback"
        fi
      fi
    else
      if timeout "$PIP_STEP_TIMEOUT" python -m pip install --timeout "$PIP_DEFAULT_TIMEOUT" --retries "$PIP_RETRIES" -e . --no-build-isolation; then
        echo "[setup] editable install succeeded with --no-build-isolation"
      else
        echo "[setup][warning] editable install without build isolation failed"
        echo "[setup][warning] retry legacy setup.py develop if setup.py exists"

        if [ -f setup.py ]; then
          timeout "$PIP_STEP_TIMEOUT" python setup.py develop || echo "[setup][warning] setup.py develop also failed"
        else
          echo "[setup][warning] no setup.py, skip legacy develop fallback"
        fi
      fi
    fi
  fi
fi

echo "[setup] Environment ready"
python --version
python -m pip --version

echo "[setup] Dependency sanity check"
python - <<'PY'
import os
import sys

print("[setup] cwd:", os.getcwd())
print("[setup] REPO_ROOT:", os.environ.get("REPO_ROOT"))
print("[setup] repo_root_in_sys_path:", os.environ.get("REPO_ROOT") in sys.path)
print("[setup] sys.path[:5]:", sys.path[:5])

for name in ["setuptools", "pytest", "hypothesis", "numpy", "scipy", "cython"]:
    try:
        mod = __import__(name)
        version = getattr(mod, "__version__", "<no __version__>")
        print(f"[setup] {{name}} {{version}}")
    except Exception as e:
        print(f"[setup][warning] cannot import {{name}}: {{e}}")
PY
"""

    result = docker_env.execute(
        {"command": setup_script},
        cwd=repo_path_in_container,
        timeout=900,
    )

    if result.get("returncode") != 0:
        raise RuntimeError(
            "Docker 容器初始化失败\n"
            f"output:\n{result.get('output', '')[-4000:]}\n"
            f"exception_info:\n{result.get('exception_info', '')}"
        )

    logger.info(
        "Docker 初始化完成 | python=%s venv=%s wheelhouse=%s constraints=%s build_requires=%s output_tail=%s",
        python_exe,
        venv_path,
        wheelhouse_path,
        constraints,
        build_requires,
        result.get("output", "")[-1200:],
    )

    return python_exe, venv_path

def prepare_agent_environment(
    instance: dict,
    repo_path: str,
    use_docker: bool,
    docker_image: str,
    docker_repo_path: str,
    docker_timeout: int,
    container_timeout: str,
):
    """
    创建 agent 使用的 environment。

    use_docker=False:
      返回 LocalEnvironment

    use_docker=True:
      返回 ActivatedDockerEnvironment

    返回：
      (env, env_info)
    """
    if not use_docker:
        from minisweagent.environments.local import LocalEnvironment

        return LocalEnvironment(cwd=repo_path), {
            "environment": "local",
            "repo_cwd": repo_path,
        }

    raw_env = create_docker_environment_for_instance(
        repo_path=repo_path,
        docker_image=docker_image,
        docker_repo_path=docker_repo_path,
        docker_timeout=docker_timeout,
        container_timeout=container_timeout,
    )

    python_version = choose_python_version(instance, repo_path)
    build_requires = read_pyproject_build_requires(repo_path)

    python_exe, venv_path = initialize_container_repo_environment(
        docker_env=raw_env,
        repo_path_in_container=docker_repo_path,
        python_version=python_version,
        build_requires=build_requires,
    )

    env = ActivatedDockerEnvironment(
        raw_env,
        venv_path=venv_path,
        repo_root=docker_repo_path,
    )

    return env, {
        "environment": "docker",
        "docker_image": docker_image,
        "docker_repo_path": docker_repo_path,
        "repo_root": docker_repo_path,
        "python_version": python_version,
        "python_exe": python_exe,
        "venv_path": venv_path,
        "build_requires": build_requires,
    }