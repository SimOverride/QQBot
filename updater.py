"""本机更新监护：机器人退出后才替换代码与依赖。"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from migration import project_lock

ROOT = Path(__file__).resolve().parent


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
        raise RuntimeError("项目不是仓库根目录")
    git(root, "symbolic-ref", "--quiet", "HEAD")
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("存在未提交修改或未跟踪文件")
    branch = git(root, "symbolic-ref", "--short", "HEAD")
    remote = git(root, "config", "--get", f"branch.{branch}.remote")
    if remote == "." or remote.startswith("-"):
        raise RuntimeError("上游必须是已配置远端")
    git(root, "rev-parse", "--verify", "@{upstream}")
    return remote


def admin(root, action):
    run(root, ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(root / "scripts" / "update_admin.ps1"),
               "-ProjectDirectory", str(root), "-Action", action], timeout=60)


def status(root, stage, state):
    path = root / "data" / "update-status.json"
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"stage": stage, "status": state}, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(path)


def update(root):
    stage = "检查仓库与迁移互斥"
    try:
        with project_lock(root, name=".migration.lock"), project_lock(root, runtime=True):
            remote = check(root)
            original = (git(root, "symbolic-ref", "HEAD"), git(root, "rev-parse", "HEAD"))
            stage = "获取远端"
            status(root, stage, "running")
            git(root, "fetch", "--", remote)
            stage = "检查快进"
            target = git(root, "rev-parse", "@{upstream}")
            git(root, "merge-base", "--is-ancestor", "HEAD", target)
            # 获取远端期间可能有人编辑文件，发布前再次检查。
            if check(root) != remote or original != (
                git(root, "symbolic-ref", "HEAD"), git(root, "rev-parse", "HEAD")
            ):
                raise RuntimeError("更新检查期间仓库已变化")
            stage = "停止后台"
            admin(root, "stop")
            stage = "快进更新"
            git(root, "merge", "--ff-only", target)
            stage = "同步依赖"
            status(root, stage, "running")
            run(root, [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                       "--no-input", "-r", "requirements.txt"], timeout=600)
            stage = "检查依赖"
            run(root, [sys.executable, "-m", "pip", "check"])
        # 后台启动会获取迁移锁；须在释放锁后启动。
        stage = "启动后台"
        admin(root, "start")
        status(root, "等待机器人启动", "updated")
        return True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        status(root, stage, "failed")
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
