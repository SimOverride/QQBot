"""Conservative publication screening with persistent at-most-once dispatch."""

import asyncio
import json
import re
import time

from nonebot.log import logger


# Deterministic rejection supplements, but does not replace, semantic screening.
def restricted(text):
    return bool(
        re.search(
            r"保密|秘密|别.{0,6}(说|告诉|分享|发)|不要.{0,6}(说|告诉|分享|发)|不许|仅限私聊|"
            r"密码|口令|密钥|验证码|身份证|银行卡|住址|手机号|病历|诊断|私钥|"
            r"confidential|secret|password|api.?key|token|https?://|[\w.+-]+@[\w.-]+|\d{7,}",
            text,
            re.I,
        )
    )


class Sharing:
    def __init__(self, service, contacts):
        self.service, self.contacts = service, contacts
        self.archive = service.archive
        self.started = time.time()
        self.lock = asyncio.Lock()

    async def review(self, bot, source_id):
        async with self.lock:
            db = self.archive.db
            row = db.execute("SELECT * FROM messages WHERE id=?", (source_id,)).fetchone()
            if row is None or row["bot"] != int(bot.self_id) or row["usr"] == row["bot"]:
                return
            if db.execute(
                "SELECT 1 FROM disclosure_reviews WHERE source_id=?", (source_id,)
            ).fetchone():
                return
            key = (row["bot"], row["grp"], row["usr"])
            if key[1] <= 0 and key[1] != -key[2]:
                return
            text = row["content"]
            previous = db.execute(
                "SELECT content FROM messages WHERE bot=? AND grp=? AND usr=? AND id<=? "
                "ORDER BY id DESC LIMIT 20",
                (*key, source_id),
            ).fetchall()
            context = "\n".join(r[0] for r in reversed(previous))[-12000:]
            if restricted(context):
                # Later confidentiality requests also retract previously reusable knowledge.
                with db:
                    db.execute(
                        "UPDATE disclosure_reviews SET safe=0 WHERE source_id IN "
                        "(SELECT id FROM messages WHERE bot=? AND grp=? AND usr=?)",
                        key,
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO disclosure_reviews SELECT id,0 FROM messages "
                        "WHERE bot=? AND grp=? AND usr=? AND id<=?",
                        (*key, source_id),
                    )
            safe, target = False, None
            groups = []
            if (
                2 <= len(text) <= 500
                and not text.lstrip().startswith("/")
                and bool(json.loads(row["types"]))
                and all(t == "text" for t in json.loads(row["types"]))
                and not restricted(context)
            ):
                if key[1] < 0:
                    _, groups = await self.contacts.private_access(bot, key[2])
                async with self.service.limits.slot():
                    async with asyncio.timeout(self.service.config.llm_timeout_seconds):
                        decision = await self.service.llm.screen_disclosure(text, context, groups)
                        safe = decision.get("safe") is True
                        target = decision.get("group_id")
                        # Independent review cannot choose recipients or rewrite the excerpt.
                        if safe:
                            check = await self.service.llm.screen_disclosure(text, context, [])
                            safe = check.get("safe") is True
            async with self.service.memory.locked(key):
                if not self.archive.source_exists(key, source_id):
                    return
                with db:
                    db.execute(
                        "INSERT OR IGNORE INTO disclosure_reviews VALUES(?,?)",
                        (source_id, int(safe)),
                    )
                logger.info(
                    "share_review source={} safe={} target_selected={} common_groups={}",
                    source_id,
                    safe,
                    target in groups,
                    len(groups),
                )
                if (
                    db.execute(
                        "SELECT 1 FROM directed_sends WHERE bot=? AND usr=? AND message_id=?",
                        (row["bot"], row["usr"], row["message_id"]),
                    ).fetchone()
                    is not None
                    or not safe
                    or type(target) is not int
                    or target not in groups
                    or key[1] >= 0
                    or row["created"] < self.started
                    or not self.service.config.proactive_share_enabled
                ):
                    return
                # Revalidate membership immediately before publication.
                _, current_groups = await self.contacts.private_access(bot, key[2])
                if target not in current_groups:
                    logger.info("share_skip source={} reason=membership_changed", source_id)
                    return
                now = time.time()
                prior = db.execute(
                    "SELECT 1 FROM share_attempts WHERE bot=? AND "
                    "((grp=? AND created>?) OR (usr=? AND created>?))",
                    (key[0], target, now - 3600, key[2], now - 86400),
                ).fetchone()
                if prior:
                    logger.info("share_skip source={} reason=rate_limit", source_id)
                    return
                # Reserve before I/O; ambiguous send failures are never retried automatically.
                with db:
                    result = db.execute(
                        "INSERT OR IGNORE INTO share_attempts VALUES(?,?,?,?,?,?)",
                        (source_id, key[0], target, key[2], now, "attempted"),
                    )
                if result.rowcount != 1:
                    return
                body = f"分享一段与 QQ {key[2]} 的普通聊天：\n{text}"
                try:
                    result = await asyncio.wait_for(
                        bot.send_group_forward_msg(
                            group_id=target,
                            messages=[
                                {
                                    "type": "node",
                                    "data": {
                                        "name": self.contacts.for_scope(key[0], target).get(
                                            key[2], f"QQ {key[2]}"
                                        ),
                                        "uin": str(key[2]),
                                        "content": [{"type": "text", "data": {"text": text}}],
                                    },
                                }
                            ],
                        ),
                        30,
                    )
                    with db:
                        db.execute(
                            "UPDATE share_attempts SET status='sent' WHERE source_id=?",
                            (source_id,),
                        )
                    logger.info("share_sent source={} group={}", source_id, target)
                    if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                        self.archive.collect(
                            (key[0], target, key[0]),
                            result["message_id"],
                            body,
                            ["text"],
                            now,
                            key[2],
                        )
                except Exception as error:
                    logger.warning("share_send_failure={}", type(error).__name__)

    async def tick(self, bot):
        rows = self.archive.db.execute(
            "SELECT m.id FROM messages m LEFT JOIN disclosure_reviews r ON r.source_id=m.id "
            "WHERE m.bot=? AND m.usr<>m.bot AND r.source_id IS NULL ORDER BY m.id DESC LIMIT 2",
            (int(bot.self_id),),
        ).fetchall()
        for row in rows:
            try:
                await self.review(bot, row[0])
            except Exception as error:
                logger.warning("disclosure_review_failure={}", type(error).__name__)
