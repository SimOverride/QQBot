"""Platform-verified contacts; names are display data, never authority."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing


class Contacts:
    def __init__(self, config):
        self.config = config
        self.names = {}
        self.refreshed = {}
        self.directory_path = None
        self.directory = {}
        self.directory_refreshed = {}
        self.directory_locks = {}

    def save_directory(self):
        if self.directory_path is None:
            return
        self.directory_path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.directory_path.with_suffix(".pending")
        pending.write_text(json.dumps(self.directory, ensure_ascii=False), encoding="utf-8")
        pending.replace(self.directory_path)

    def observe_nickname(self, bot_id, user_id, name):
        if not name:
            return
        users = self.directory.setdefault(str(bot_id), {}).setdefault("users", {})
        if users.get(str(user_id), {}).get("name") == str(name)[:80]:
            return False
        users[str(user_id)] = {"name": str(name)[:80], "updated": time.time()}
        return True

    def recorded_people(self, bot_id):
        """只读取历史身份，用于补齐已退群或非好友的显示资料。"""
        if self.directory_path is None:
            return set()
        database = self.directory_path.parent / "memory.sqlite3"
        if not database.exists():
            return set()
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return {
                int(row[0])
                for row in db.execute(
                    "SELECT usr FROM messages WHERE bot=? UNION SELECT usr FROM facts WHERE bot=?",
                    (bot_id, bot_id),
                )
                if int(row[0]) > 0 and int(row[0]) != bot_id
            }

    async def sync_directory(self, bot, force=False):
        """先发布群名，再同步成员；失败保留缓存并在下次轮询重试。"""
        bot_id = int(bot.self_id)
        lock = self.directory_locks.setdefault(bot_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            if not force and time.monotonic() - self.directory_refreshed.get(bot_id, -300) < 300:
                return
            data = self.directory.setdefault(str(bot_id), {})
            complete = True
            seen = set()
            try:
                login = await asyncio.wait_for(bot.get_login_info(), 8)
                if int(login.get("user_id", 0)) == bot_id:
                    self.observe_nickname(bot_id, bot_id, login.get("nickname"))
                    self.save_directory()
            except Exception:
                complete = False
            try:
                groups = await asyncio.wait_for(bot.get_group_list(no_cache=True), 8)
            except Exception:
                groups = []
                complete = False
            # 群名不等待逐群成员请求完成，避免大群或接口超时拖住整个目录。
            for item in groups:
                group = int(item["group_id"])
                entry = data.setdefault("groups", {}).setdefault(str(group), {})
                if item.get("group_name"):
                    entry["name"] = str(item["group_name"])[:100]
            self.save_directory()
            try:
                friends = await asyncio.wait_for(bot.get_friend_list(), 8)
                for friend in friends:
                    user = int(friend["user_id"])
                    if friend.get("nickname"):
                        self.observe_nickname(bot_id, user, friend["nickname"])
                        seen.add(user)
            except Exception:
                complete = False
            self.save_directory()
            for item in groups:
                group = int(item["group_id"])
                entry = data["groups"][str(group)]
                try:
                    members = await asyncio.wait_for(
                        bot.get_group_member_list(group_id=group, no_cache=True), 8
                    )
                    entry["members"] = [int(m["user_id"]) for m in members]
                    for member in members:
                        user = int(member["user_id"])
                        if member.get("nickname") and user not in seen:
                            self.observe_nickname(bot_id, user, member["nickname"])
                            seen.add(user)
                except Exception:
                    complete = False
                self.save_directory()
            for user in sorted(self.recorded_people(bot_id) - seen):
                try:
                    info = await asyncio.wait_for(
                        bot.get_stranger_info(user_id=user, no_cache=True), 8
                    )
                    if int(info.get("user_id", 0)) == user and info.get("nickname"):
                        self.observe_nickname(bot_id, user, info["nickname"])
                    else:
                        complete = False
                except Exception:
                    complete = False
                self.save_directory()
            if complete:
                self.directory_refreshed[bot_id] = time.monotonic()
            else:
                self.directory_refreshed.pop(bot_id, None)

    def observe(self, bot_id, group_id, user_id, sender):
        if self.observe_nickname(bot_id, user_id, getattr(sender, "nickname", None)):
            try:
                self.save_directory()
            except OSError:
                # 显示名缓存写入失败不能中断消息归档。
                pass
        name = getattr(sender, "card", None) or getattr(sender, "nickname", None)
        if name:
            self.names[(bot_id, group_id, user_id)] = str(name)[:80]

    def for_scope(self, bot_id, group_id):
        return {
            uid: name
            for (bid, gid, uid), name in self.names.items()
            if bid == bot_id and gid == group_id
        }

    async def private_access(self, bot, user_id):
        """Revalidate on every message; API failures never grant membership."""
        friend = False
        groups = []
        try:
            friends = await asyncio.wait_for(bot.get_friend_list(), 8)
            for item in friends:
                if int(item["user_id"]) == user_id:
                    friend = True
                    self.names[(int(bot.self_id), -user_id, user_id)] = str(
                        item.get("nickname", "")
                    )[:80]
        except Exception:
            pass
        try:
            joined = await asyncio.wait_for(bot.get_group_list(), 8)
            group_ids = sorted(
                {int(item["group_id"]) for item in joined if int(item["group_id"]) > 0}
            )
        except Exception:
            group_ids = []
        for group in group_ids:
            try:
                member = await asyncio.wait_for(
                    bot.get_group_member_info(group_id=group, user_id=user_id, no_cache=True), 8
                )
                if (
                    int(member.get("user_id", 0)) != user_id
                    or int(member.get("group_id", 0)) != group
                ):
                    continue
                groups.append(group)
                name = str(member.get("card") or member.get("nickname") or "")[:80]
                if name:
                    self.names[(int(bot.self_id), group, user_id)] = name
            except Exception:
                pass
        return friend or bool(groups), groups

    async def refresh_group(self, bot, group_id):
        key = (int(bot.self_id), group_id)
        if time.monotonic() - self.refreshed.get(key, -300) < 300:
            return
        self.refreshed[key] = time.monotonic()
        try:
            members = await asyncio.wait_for(bot.get_group_member_list(group_id=group_id), 8)
            for item in members:
                name = str(item.get("card") or item.get("nickname") or "")[:80]
                if name:
                    self.names[(*key, int(item["user_id"]))] = name
        except Exception:
            pass
