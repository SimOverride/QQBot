"""插件配置及群数据的原子存储与迁移校验。"""

import json
import math
import re
import uuid
from pathlib import Path

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


class Store:
    def __init__(self, root):
        self.path = Path(root) / FILE

    def read(self):
        if not self.path.exists():
            return {"version": 1, "bots": {}}
        return validate(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, value):
        validate(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_name(f".plugins-{uuid.uuid4().hex}.tmp")
        try:
            pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            pending.replace(self.path)
        finally:
            pending.unlink(missing_ok=True)
