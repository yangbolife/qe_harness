"""eval_harness · Git 版本采集（N3 并入增强 · 代码版本钉）

为什么需要：版本钉此前只有 harness / model / dataset 三钉，缺**代码**这一钉。
于是出现「同一套核、同一个数据集、同一模型，两次分数不同」时无法回答
「这次跑的是哪份代码」。Braintrust 把 git metadata 随实验自动记录，我们补齐。

设计约束（评测流程优先级高于版本采集）：
- 任何失败都静默降级为 ``{"available": False, "reason": ...}``，绝不中断评测。
- 只记 ``dirty`` 布尔，不采集 diff 正文（体积不可控，且可能含密钥）。
- 结果按 cwd 进程内缓存：一次评测里多处取用不重复起子进程。
- 支持 CI 覆盖：``EVAL_GIT_COMMIT`` / ``EVAL_GIT_BRANCH`` 环境变量优先。
  关闭采集：``EVAL_HARNESS_GIT=0``。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

# 与 Braintrust 对齐的字段集（去掉 diff 正文）
FIELDS = (
    "commit", "branch", "tag", "dirty",
    "author_name", "author_email", "commit_message", "commit_time",
)

_CACHE: dict[str, dict] = {}
_TIMEOUT = 5.0


def _git(args: list[str], cwd: str) -> Optional[str]:
    """执行 git 子命令；任何异常/非零退出返回 None（调用方决定降级）。"""
    exe = shutil.which("git")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, *args], cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def collect_git_info(cwd: Optional[str] = None, use_cache: bool = True) -> dict:
    """采集当前工作区的 git 版本信息。

    返回恒为 dict，其中 ``available`` 表示是否真正拿到仓库信息。
    """
    cwd = os.path.abspath(cwd or os.getcwd())
    if os.environ.get("EVAL_HARNESS_GIT", "1") == "0":
        return {"available": False, "reason": "disabled_by_env"}
    if use_cache and cwd in _CACHE:
        return dict(_CACHE[cwd])

    if _git(["rev-parse", "--is-inside-work-tree"], cwd) != "true":
        info = {"available": False, "reason": "not_a_repo"}
        _CACHE[cwd] = info
        return dict(info)

    info: dict = {"available": True}
    info["commit"] = _git(["rev-parse", "HEAD"], cwd) or ""
    info["branch"] = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or ""
    info["tag"] = _git(["describe", "--tags", "--always"], cwd) or ""
    # --untracked-files=no：只看已跟踪文件的改动，避免大仓库上 untracked 扫描拖慢评测
    info["dirty"] = bool(_git(["status", "--porcelain", "--untracked-files=no"], cwd))
    info["author_name"] = _git(["log", "-1", "--pretty=%an"], cwd) or ""
    info["author_email"] = _git(["log", "-1", "--pretty=%ae"], cwd) or ""
    info["commit_message"] = (_git(["log", "-1", "--pretty=%s"], cwd) or "")[:500]
    info["commit_time"] = _git(["log", "-1", "--pretty=%cI"], cwd) or ""

    # CI 覆盖：容器里常是 detached HEAD / 无 .git，由 CI 注入权威值
    for env_key, field in (("EVAL_GIT_COMMIT", "commit"), ("EVAL_GIT_BRANCH", "branch")):
        v = os.environ.get(env_key)
        if v:
            info[field] = v

    _CACHE[cwd] = info
    return dict(info)


def short_commit(info: Optional[dict]) -> str:
    """短 commit（7 位），无则空串——用于报告与 UI 展示。"""
    commit = (info or {}).get("commit") or ""
    return commit[:7]


def describe(info: Optional[dict]) -> str:
    """一行摘要，如 ``main@a1b2c3d*``；* 表示工作区有改动。"""
    info = info or {}
    if not info.get("available"):
        return "no-git"
    branch = info.get("branch") or "detached"
    return f"{branch}@{short_commit(info) or '-'}{'*' if info.get('dirty') else ''}"


def reset_cache() -> None:
    """清缓存（测试用）。"""
    _CACHE.clear()
