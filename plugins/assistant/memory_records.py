"""从明确人设与真实记忆变化维护机器人档案和每日记录。"""

from datetime import datetime, timedelta, timezone

from .vault import digest, identifier


def seed_profile(vault, profiles):
    if profiles is None:
        return
    baseline = profiles.effective()
    body = baseline.get("persona", "")
    interests = baseline.get("interests", "")
    if interests:
        body += "\n\n自身兴趣：" + interests
    body = body[:12000].strip()
    old = vault.field(vault.bot, "自我档案", "self")
    if not body or (old and (old.get("fixed") or not old.get("profile_baseline"))):
        return
    fingerprint = digest(baseline)
    if old and old.get("profile_baseline") == fingerprint:
        return
    vault.put(
        {
            "id": identifier(vault.bot, "自我档案", "self"),
            "subject": vault.bot,
            "kind": "self",
            "field": "自我档案",
            "value": body,
            "profile_baseline": fingerprint,
            "sources": [],
        },
        old["revision"] if old else None,
    )


def journal(vault, row, note_ids):
    """仅记录实际写入变化；不将整理行为当作新证据或强化。"""
    if not note_ids:
        return
    day = datetime.fromtimestamp(row["created"], timezone(timedelta(hours=8))).date().isoformat()
    field = "记忆变化：" + day
    old = vault.field(vault.bot, field, "diary")
    if old and old.get("fixed"):
        return
    entries = dict((old or {}).get("entries", {}))
    for ident in note_ids:
        note = vault.get(ident)
        if note and note["value"] and vault.valid(note):
            entries[ident] = {
                "field": note.get("field", note["kind"]),
                "subject": note["subject"],
                "value": note["value"][:300],
                "sources": note["sources"],
            }
    entries = dict(list(entries.items())[-20:])
    text = "当日实际记忆变化（整理记录，不代表新增经历或情绪）：\n" + "\n".join(
        f"- [[{ident}|{entry['subject']} · {entry['field']}]]：{entry['value']}"
        for ident, entry in entries.items()
    )
    if old and old["value"] == text:
        return
    sources = {digest(ref): ref for entry in entries.values() for ref in entry["sources"]}
    vault.put(
        {
            "id": identifier(vault.bot, field, "diary"),
            "subject": vault.bot,
            "kind": "diary",
            "field": field,
            "value": text[:12000],
            "sources": list(sources.values()),
            "entries": entries,
        },
        old["revision"] if old else None,
    )
