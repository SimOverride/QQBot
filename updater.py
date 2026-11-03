"""本机更新监护：机器人退出后才替换代码与依赖。"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from migration import project_lock

ROOT = Path(__file__).resolve().parent


class UpdateBlocked(RuntimeError):
    """可直接向所有者说明的固定预检原因。"""


def run(root, command, timeout=120):
    """不输出子进程原文，避免远端地址、凭据及配置进入聊天或日志。"""
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
    )
    if result.returncode:
        raise RuntimeError("命令执行失败")
    return result.stdout.strip()


def git(root, *args):
    return run(root, ["git", *args])


def check(root):
    """只接受干净的普通分支及其已配置上游，不猜测远端或分支。"""
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise UpdateBlocked("项目不是仓库根目录")
    try:
        branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    except RuntimeError:
        raise UpdateBlocked("当前没有可更新的本地分支，请先切换到部署分支") from None
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise UpdateBlocked("存在未提交修改或未跟踪文件")
    try:
        remote = git(root, "config", "--get", f"branch.{branch}.remote")
        git(root, "rev-parse", "--verify", "@{upstream}")
    except RuntimeError:
        raise UpdateBlocked("当前分支缺少可解析的上游，请检查远端跟踪配置") from None
    if remote == "." or remote.startswith("-"):
        raise UpdateBlocked("上游必须是已配置远端")
    return remote


def admin(root, action):
    run(root, ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(root / "scripts" / "update_admin.ps1"),
               "-ProjectDirectory", str(root), "-Action", action], timeout=60)


def status(root, stage, state, **details):
    path = root / "data" / "update-status.json"
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    report = {"stage": stage, "status": state, **details}
    temporary.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def last_result(root):
    path = root / "data/update-status.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"status": "unknown"}
    except FileNotFoundError:
        return {"status": "none"}
    except (ValueError, OSError):
        return {"status": "unknown"}


def summary(root, before, target):
    span = f"{before}..{target}"
    titles = git(root, "log", "-10", "--format=%h %s", span).splitlines()
    return {
        "branch": git(root, "symbolic-ref", "--short", "HEAD"),
        "before": before, "target": target,
        "commit_count": int(git(root, "rev-list", "--count", span)),
        "commits": [line[:200] for line in titles],
    }


def fetch(root, remote):
    try:
        git(root, "fetch", "--", remote)
    except subprocess.TimeoutExpired:
        git(root, "fetch", "--", remote)


def preview(root):
    remote = check(root)
    fetch(root, remote)
    target = git(root, "rev-parse", "@{upstream}")
    git(root, "merge-base", "--is-ancestor", "HEAD", target)
    return summary(root, git(root, "rev-parse", "HEAD"), target)


def update(root):
    details = {"code_applied": False}
    stage = "检查仓库与迁移互斥"
    try:
        with project_lock(root, name=".migration.lock"), project_lock(root, runtime=True):
            previous = last_result(root)
            remote = check(root)
            original = (git(root, "symbolic-ref", "HEAD"), git(root, "rev-parse", "HEAD"))
            stage = "获取远端"
            status(root, stage, "running", **details)
            fetch(root, remote)
            stage = "检查快进"
            target = git(root, "rev-parse", "@{upstream}")
            git(root, "merge-base", "--is-ancestor", "HEAD", target)
            # 获取远端期间可能有人编辑文件，发布前再次检查。
            if check(root) != remote or original != (
                git(root, "symbolic-ref", "HEAD"), git(root, "rev-parse", "HEAD")
            ):
                raise RuntimeError("更新检查期间仓库已变化")
            details.update(summary(root, original[1], target))
            if target == original[1] and previous.get("status") in ("updated", "current", "none"):
                status(root, "已是最新版本", "current", **details)
                return True
            stage = "停止后台"
            admin(root, "stop")
            stage = "快进更新"
            git(root, "merge", "--ff-only", target)
            details["code_applied"] = True
            stage = "同步依赖"
            status(root, stage, "running", **details)
            run(root, [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                       "--no-input", "-r", "requirements.txt"], timeout=600)
            stage = "检查依赖"
            run(root, [sys.executable, "-m", "pip", "check"])
        # 后台启动会获取迁移锁；须在释放锁后启动。
        stage = "启动后台"
        admin(root, "start")
        status(root, "等待机器人启动", "updated", **details)
        return True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        status(root, stage, "failed", **details)
        print(f"更新失败：{stage}。请检查 data/update-status.json，修复后重新启动。", flush=True)
        return False


def supervise(root=ROOT):
    """单次授权只消费一次；普通退出、崩溃及 Ctrl+C 不触发自动更新。"""
    with project_lock(root, name=".supervisor.lock"):
        token = secrets.token_hex(24)
        request = root / "data" / f"update-{token}.request"
        env = {**os.environ, "QQBOT_UPDATE_TOKEN": token}
        process = subprocess.Popen([sys.executable, str(root / "bot.py")], cwd=root, env=env)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            # 控制台 Ctrl+C 同时到达子进程，等待其关闭，绝不执行更新。
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait()
            request.unlink(missing_ok=True)
            return 130
        requested = request.exists() and request.read_text(encoding="ascii") == token
        request.unlink(missing_ok=True)
        if not requested:
            return code
        return 75 if update(root) else 1


if __name__ == "__main__":
    raise SystemExit(supervise())
