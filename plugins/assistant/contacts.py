"""Platform-verified contacts; names are display data, never authority."""

import asyncio
import time


class Contacts:
    def __init__(self, config):
        self.config = config
        self.names = {}
        self.refreshed = {}

    def observe(self, bot_id, group_id, user_id, sender):
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
