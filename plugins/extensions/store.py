"""插件配置及群数据的原子存储与迁移校验。"""

import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path

import migration

FILE = "data/plugins.json"


def positive(value):
    return isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0


def validate(value, account=None):
    if not isinstance(value, dict) or set(value) != {"version", "bots"}:
        raise ValueError("插件配置结构无效")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("插件配置版本不支持")
    if not isinstance(value["bots"], dict):
        raise ValueError("插件账号配置无效")
    for bot, plugins in value["bots"].items():
        if not positive(bot) or (account is not None and int(bot) != account):
            raise ValueError("插件配置含不同机器人账号")
        if not isinstance(plugins, dict):
            raise ValueError("插件列表无效")
        for name, entry in plugins.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
                raise ValueError("插件标识无效")
            if not isinstance(entry, dict) or set(entry) != {"enabled", "scope", "groups", "data"}:
                raise ValueError("插件设置无效")
            scope = entry["scope"]
            if name.startswith("gen_") and (
                not re.fullmatch(r"gen_[a-f0-9]{16}", name)
                or not isinstance(scope, list)
                or len(scope) != 1
            ):
                raise ValueError("生成插件必须绑定唯一原群")
            if scope is not None and (
                not isinstance(scope, list) or not scope or any(not positive(g) for g in scope)
            ):
                raise ValueError("插件群范围无效")
            if type(entry["enabled"]) is not bool or not isinstance(entry["groups"], dict):
                raise ValueError("插件开关无效")
            if not isinstance(entry["data"], dict):
                raise ValueError("插件数据无效")
            for group, enabled in entry["groups"].items():
                if not positive(group) or type(enabled) is not bool:
                    raise ValueError("插件群开关无效")
                if scope is not None and group not in scope:
                    raise ValueError("插件开关超出群范围")
            for group, data in entry["data"].items():
                if not positive(group) or (scope is not None and group not in scope):
                    raise ValueError("插件数据超出群范围")
                if not isinstance(data, dict):
                    raise ValueError("插件群数据无效")
                if name == "schedule":
                    validate_schedule(data)
                if name.startswith("gen_"):
                    meta = data.get("_runtime")
                    if (
                        not isinstance(meta, dict)
                        or not isinstance(data.get("state"), dict)
                        or not re.fullmatch(r"[a-f0-9]{64}", str(meta.get("version", "")))
                        or any(
                            not isinstance(meta.get(k), str) or not 1 <= len(meta[k]) <= limit
                            for k, limit in (("title", 40), ("description", 400))
                        )
                    ):
                        raise ValueError("生成插件版本或数据无效")
    return value


def validate_schedule(data):
    if set(data) != {"jobs"} or not isinstance(data["jobs"], dict):
        raise ValueError("定时任务数据无效")
    for job_id, job in data["jobs"].items():
        if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not isinstance(job, dict):
            raise ValueError("定时任务标识无效")
        if set(job) != {"kind", "at", "text", "next_at", "status", "last_status", "creator"}:
            raise ValueError("定时任务字段无效")
        if job["kind"] not in ("once", "daily") or not isinstance(job["at"], str):
            raise ValueError("定时任务类型无效")
        if not isinstance(job["text"], str) or not 1 <= len(job["text"].strip()) <= 1000:
            raise ValueError("定时消息长度无效")
        if type(job["next_at"]) not in (int, float) or not math.isfinite(job["next_at"]):
            raise ValueError("定时任务时间无效")
        if not 0 < job["next_at"] < 253402185600:
            raise ValueError("定时任务时间超出范围")
        if job["status"] not in ("active", "completed", "cancelled", "expired"):
            raise ValueError("定时任务状态无效")
        if job["last_status"] not in ("pending", "attempting", "sent", "unknown", "skipped"):
            raise ValueError("定时任务发送状态无效")
        if type(job["creator"]) is not int or job["creator"] <= 0:
            raise ValueError("定时任务创建者无效")
        from datetime import datetime

        try:
            datetime.strptime(job["at"], "%H:%M" if job["kind"] == "daily" else "%Y-%m-%d %H:%M")
        except ValueError:
            raise ValueError("定时任务时间格式无效") from None


def preserve_progress(merged, incoming):
    """配置以目标为准，执行进度不能因合并旧副本而倒退。"""
    for bot, entries in incoming["bots"].items():
        source = entries.get("schedule", {}).get("data", {})
        target = merged["bots"].get(bot, {}).get("schedule", {}).get("data", {})
        for group, data in source.items():
            for job_id, job in data["jobs"].items():
                current = target.get(group, {}).get("jobs", {}).get(job_id)
                if current is None:
                    continue
                if current["status"] == "active" and job["status"] != "active":
                    current["status"] = job["status"]
                if job["next_at"] > current["next_at"]:
                    current["next_at"] = job["next_at"]
                    current["last_status"] = job["last_status"]
                elif job["next_at"] == current["next_at"] and current["last_status"] == "pending":
                    current["last_status"] = job["last_status"]
    return merged


class Snapshot(dict):
    """将读取版本绑定到该快照，避免后台与机器人互相覆盖。"""

    def __init__(self, value, fingerprint):
        super().__init__(value)
        self.fingerprint = fingerprint


class Store:
    def __init__(self, root):
        self.path = Path(root) / FILE

    def fingerprint(self):
        return hashlib.sha256(self.path.read_bytes() if self.path.exists() else b"").hexdigest()

    def read(self):
        raw = self.path.read_bytes() if self.path.exists() else b""
        value = validate(json.loads(raw)) if raw else {"version": 1, "bots": {}}
        return Snapshot(value, hashlib.sha256(raw).hexdigest())

    def switch_time(self, bot, name, group=None):
        path = self.path.with_name("plugin-controls.json")
        try:
            controls = json.loads(path.read_text(encoding="utf-8"))
            return max(controls.get(f"{bot}:{name}", 0), controls.get(f"{bot}:{name}:{group}", 0))
        except (OSError, ValueError):
            return 0

    def write(self, value):
        validate(value)
        with migration.project_lock(self.path.parents[1], name=".plugins.lock"):
            if isinstance(value, Snapshot) and value.fingerprint != self.fingerprint():
                raise ValueError("插件数据已变化，请重新读取后重试")
            previous = self.read()
            controls_path = self.path.with_name("plugin-controls.json")
            controls = (
                json.loads(controls_path.read_text(encoding="utf-8"))
                if controls_path.exists()
                else {}
            )
            for bot, entries in value["bots"].items():
                for name, entry in entries.items():
                    old = previous["bots"].get(bot, {}).get(name, {})
                    if old.get("enabled") != entry["enabled"]:
                        controls[f"{bot}:{name}"] = time.time()
                    for group in set(old.get("groups", {})) | set(entry["groups"]):
                        if old.get("groups", {}).get(group) != entry["groups"].get(group):
                            controls[f"{bot}:{name}:{group}"] = time.time()
            installed = {
                f"{bot}:{name}" for bot, entries in value["bots"].items() for name in entries
            }
            controls = {
                k: v for k, v in controls.items() if ":".join(k.split(":")[:2]) in installed
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_name(f".plugins-{uuid.uuid4().hex}.tmp")
            try:
                pending.write_text(json.dumps(controls), encoding="utf-8")
                pending.replace(controls_path)
                pending.write_text(
                    json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                pending.replace(self.path)
                if isinstance(value, Snapshot):
                    value.fingerprint = self.fingerprint()
            finally:
                pending.unlink(missing_ok=True)
