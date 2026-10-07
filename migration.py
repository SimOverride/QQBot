"""机器人私有数据迁移：账号校验、快照导出和离线合并。"""

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

from dotenv import dotenv_values
from dotenv.parser import parse_stream

ROOT = Path(__file__).resolve().parent
MEMORY = "data/memory.sqlite3"
IDENTITY = "data/identity.json"
JOURNAL = "data/migration-pending.json"
TABLES = {
    "messages",
    "turns",
    "facts",
    "profile_cursor",
    "disclosure_reviews",
    "share_attempts",
    "directed_sends",
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(root, name):
    """所有归档和恢复路径都限制在指定目录内，拒绝链接和特殊路径。"""
    p = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or p.is_absolute()
        or any(x in ("", ".", "..") for x in name.split("/"))
        or any(x.endswith((".", " ")) for x in p.parts)
        or any(
            x.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *[f"COM{i}" for i in range(10)],
                *[f"LPT{i}" for i in range(10)],
            }
            for x in p.parts
        )
    ):
        raise ValueError("迁移包包含不安全路径")
    target = root.joinpath(*p.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("迁移路径越界")
    for parent in (target, *target.parents):
        if parent == root.parent:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("迁移不支持符号链接或目录联接")
    return target


def allowed(name):
    return name in (".env", "group_chat.json", MEMORY, IDENTITY) or name.startswith(
        ("emotes/", "personas/", "styles/")
    )


def emote_dir(root):
    """自定义图库可以迁移，但必须位于项目内且不能覆盖代码或其他数据目录。"""
    value = dotenv_values(root / ".env").get("EMOTES_DIR") or "emotes"
    value = value.replace("\\", "/")
    if value.split("/")[0] in {
        "data",
        "personas",
        "styles",
        "plugins",
        "tests",
        "docs",
        ".git",
        "backups",
    }:
        raise ValueError("EMOTES_DIR 与项目保留目录冲突")
    return safe_path(root, value)


def snapshot(source, target):
    """使用备份接口纳入已提交的 WAL 数据，不直接复制活动数据库文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("数据库完整性检查失败")


def account(root, expected=None):
    """账号取自数据库及迁移绑定；新部署必须由操作者明确指定目标 QQ。"""
    found = set()
    if (root / IDENTITY).exists():
        found.add(int(read_json(root / IDENTITY)["bot_qq"]))
    if (root / MEMORY).exists():
        with closing(
            sqlite3.connect((root / MEMORY).resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            for table in TABLES - {"disclosure_reviews"}:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    found.update(r[0] for r in db.execute(f'SELECT DISTINCT bot FROM "{table}"'))
    if expected is not None:
        found.add(expected)
    if len(found) != 1 or any(type(x) is not int or x <= 0 for x in found):
        raise ValueError("无法确认唯一机器人 QQ，或账号不一致；新部署请使用 --bot-qq 指定目标账号")
    return found.pop()


@contextmanager
def project_lock(root, runtime=False, name=".runtime.lock"):
    """运行进程与导入共用排他锁，防止导入时机器人启动并写入旧数据库。"""
    (root / "data").mkdir(parents=True, exist_ok=True)
    with (root / "data" / name).open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("机器人或导入工具正在运行，请先停止后重试") from None
        try:
            if runtime and (root / JOURNAL).exists():
                raise ValueError("检测到未完成导入，请先执行 migration.py recover")
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def check_legacy_process(root):
    """Windows 上同时识别升级前未持有运行锁的机器人进程。"""
    if os.name != "nt":
        return
    script = (
        "$p=Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\"; "
        "@($p | Select-Object ExecutablePath,CommandLine) | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=True
    )
    rows = json.loads(result.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        command = (row.get("CommandLine") or "").lower().replace("/", "\\")
        executable = (row.get("ExecutablePath") or "").lower()
        if "bot.py" in command and (
            str(root).lower() in command or str(root).lower() in executable
        ):
            raise ValueError("检测到当前项目机器人正在运行，请先停止再导入")


def export_data(root, output, bot_qq=None):
    """迁移操作互斥，机器人正常运行时仍允许快照导出。"""
    with project_lock(root, name=".migration.lock"):
        return _export_data(root, output, bot_qq)


def _export_data(root, output, bot_qq=None):
    """只导出迁移需要的数据，包内使用统一图库路径并带 SHA-256 清单。"""
    if (root / JOURNAL).exists():
        raise ValueError("存在未完成导入，须先恢复")
    with tempfile.TemporaryDirectory(prefix="qqbot-export-") as temporary:
        staging = Path(temporary)
        files = {}
        for name in (".env", "group_chat.json", MEMORY):
            source = root / name
            if source.is_file():
                files[name] = source
        for prefix, directory in (
            ("emotes", emote_dir(root)),
            ("personas", root / "personas"),
            ("styles", root / "styles"),
        ):
            if directory.exists():
                for source in directory.rglob("*"):
                    safe_path(root, source.relative_to(root).as_posix())
                    if source.is_file() and not source.name.endswith(("-wal", "-shm", "-journal")):
                        files[prefix + "/" + source.relative_to(directory).as_posix()] = source
        for name, source in files.items():
            safe_path(root, source.relative_to(root).as_posix())
            target = safe_path(staging, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith(".sqlite3"):
                snapshot(source, target)
            else:
                # 文件在读取期间变化则拒绝该快照，避免静默导出半写入内容。
                before = source.stat()
                shutil.copy2(source, target)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("导出期间文件发生变化，请重试")
        if (root / IDENTITY).exists():
            (staging / "data").mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / IDENTITY, staging / IDENTITY)
        qq = account(staging, account(root, bot_qq))
        write_json(staging / IDENTITY, {"bot_qq": qq})
        files[IDENTITY] = staging / IDENTITY
        manifest = {
            "format": 1,
            "bot_qq": qq,
            "created": datetime.now(timezone.utc).isoformat(),
            "files": {
                n: {"sha256": digest(staging / n), "size": (staging / n).stat().st_size}
                for n in files
            },
        }
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        # 排他创建，避免覆盖旧备份。
        with output.open("xb") as stream:
            try:
                with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
                    archive.writestr(
                        "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                    )
                    for name in sorted(files):
                        archive.write(staging / name, name)
            except Exception:
                stream.close()
                output.unlink(missing_ok=True)
                raise
        return manifest


def unpack(archive_path, target):
    """先完整校验清单和路径，再写入隔离临时目录，不执行包内代码或 SQL。"""
    with ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(n.casefold() for n in names)) or len(names) > 50000:
            raise ValueError("迁移包有重复路径或文件过多")
        if sum(i.file_size for i in infos) > 20 * 1024**3:
            raise ValueError("迁移包解压大小超过 20 GiB")
        for info in infos:
            safe_path(target, info.filename)
            if info.filename.endswith(("-wal", "-shm", "-journal")):
                raise ValueError("迁移包不得包含活动数据库旁路文件")
            if info.filename != "manifest.json" and not allowed(info.filename):
                raise ValueError("迁移包包含范围外文件")
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("迁移包包含目录项或符号链接")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != 1 or type(manifest.get("bot_qq")) is not int:
            raise ValueError("不支持的迁移包版本或账号")
        if set(manifest["files"]) != set(names) - {"manifest.json"}:
            raise ValueError("迁移包清单不完整")
        for name, metadata in manifest["files"].items():
            path = safe_path(target, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, path.open("xb") as dest:
                shutil.copyfileobj(source, dest)
            if path.stat().st_size != metadata["size"] or digest(path) != metadata["sha256"]:
                raise ValueError("迁移包文件校验失败")
        if IDENTITY not in manifest["files"]:
            raise ValueError("迁移包缺少账号绑定")
        account(target, manifest["bot_qq"])
        return manifest


def read_tables(path, expected):
    """导入仅接受当前已知表结构；不运行来源数据库里的触发器。"""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("数据库损坏")
        actual = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} - {
            "sqlite_sequence"
        }
        if (
            actual != expected
            or db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()
        ):
            raise ValueError("数据库结构不受支持，请在相同代码版本之间迁移")
        return {
            table: [dict(r) for r in db.execute(f'SELECT * FROM "{table}"')] for table in expected
        }


def insert(db, table, row):
    keys = list(row)
    db.execute(
        f'INSERT INTO "{table}" ('
        + ",".join('"' + k + '"' for k in keys)
        + ") VALUES ("
        + ",".join("?" for _ in keys)
        + ")",
        [row[k] for k in keys],
    )


def merge_memory(local, incoming, output):
    """重建消息主键及全部引用；已处理消息排在待处理消息前，保留游标语义。"""
    from plugins.assistant.config import Config
    from plugins.assistant.longterm import LongTermMemory

    sources = [
        read_tables(p, TABLES) if p.exists() else {t: [] for t in TABLES} for p in (local, incoming)
    ]
    archive = LongTermMemory(output, Config())
    db = archive.db
    try:
        # 字段必须与本版本完全相同，避免悄悄丢弃新版本增加的数据。
        for source in sources:
            for table, rows in source.items():
                columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                if any(set(r) != columns for r in rows):
                    raise ValueError("数据库字段不兼容")
        merged, old_keys, processed = {}, [{}, {}], set()
        for index, source in enumerate(sources):
            cursors = {
                (r["bot"], r["grp"], r["usr"]): r["last_id"] for r in source["profile_cursor"]
            }
            for row in source["messages"]:
                key = (row["bot"], row["grp"], row["message_id"])
                old_keys[index][row["id"]] = key
                merged.setdefault(key, row)
                if row["id"] <= cursors.get((row["bot"], row["grp"], row["usr"]), 0):
                    processed.add(key)
        new_ids, cursor_rows = {}, {}
        with db:
            ordered = sorted(merged, key=lambda k: (k not in processed, merged[k]["created"], k))
            for ident, key in enumerate(ordered, 1):
                row = dict(merged[key], id=ident)
                insert(db, "messages", row)
                new_ids[key] = ident
                if key in processed:
                    cursor_rows[(row["bot"], row["grp"], row["usr"])] = ident
            for key, last in cursor_rows.items():
                insert(
                    db, "profile_cursor", dict(zip(("bot", "grp", "usr", "last_id"), (*key, last)))
                )
            maps = [{old: new_ids[key] for old, key in keys.items()} for keys in old_keys]
            # 认知选择更新时间较新的记录；相同时间保留本地。审核冲突从严拒绝公开。
            for table, keys in [
                ("turns", ("bot", "grp", "message_id")),
                ("facts", ("bot", "grp", "usr", "field")),
                ("directed_sends", ("bot", "usr", "message_id")),
                ("disclosure_reviews", ("source_id",)),
                ("share_attempts", ("source_id",)),
            ]:
                records = {}
                for index, source in enumerate(sources):
                    for original in source[table]:
                        row = dict(original)
                        if "source_id" in row and row["source_id"] is not None:
                            row["source_id"] = maps[index].get(row["source_id"])
                            if row["source_id"] is None and table != "facts":
                                continue
                        row.pop("id", None)
                        key = tuple(row[k] for k in keys)
                        previous = records.get(key)
                        if previous is None or (
                            table == "facts" and row["updated"] > previous["updated"]
                        ):
                            records[key] = row
                        elif table == "disclosure_reviews":
                            previous["safe"] = min(previous["safe"], row["safe"])
                for row in sorted(records.values(), key=lambda r: r.get("created", 0)):
                    insert(db, table, row)
    finally:
        archive.close()


def merge_emotes(local, incoming, output):
    """表情内容以哈希去重，描述取较新版本，收藏时间取较晚值以保留限额。"""
    schemas = {
        "emote_memory": "digest TEXT PRIMARY KEY, description TEXT NOT NULL, updated REAL NOT NULL",
        "emote_imports": "digest TEXT PRIMARY KEY, source TEXT NOT NULL, created REAL NOT NULL",
    }
    with closing(sqlite3.connect(output)) as dst, dst:
        for table, schema in schemas.items():
            dst.execute(f'CREATE TABLE "{table}" ({schema})')
        records = {t: {} for t in schemas}
        for path in (local, incoming):
            if not path.exists():
                continue
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                present = {
                    r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            if not present <= schemas.keys():
                raise ValueError("未知表情数据库结构")
            for table, rows in read_tables(path, present).items():
                for row in rows:
                    columns = {r[1] for r in dst.execute(f'PRAGMA table_info("{table}")')}
                    if set(row) != columns:
                        raise ValueError("表情数据库字段不兼容")
                    previous = records[table].get(row["digest"])
                    field = "updated" if table == "emote_memory" else "created"
                    if previous is None or row[field] > previous[field]:
                        records[table][row["digest"]] = row
        for table, rows in records.items():
            for row in rows.values():
                insert(dst, table, row)


def fill_missing(local, incoming):
    """配置冲突以目标为准，递归补齐来源独有配置。"""
    result = dict(incoming)
    for key, value in local.items():
        result[key] = (
            fill_missing(value, result[key])
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else value
        )
    return result


def stage_merge(root, incoming, staged, qq, fresh):
    """在隔离目录完成合并，返回最终项目路径到暂存文件的映射。"""
    plan = {}
    for path in incoming.rglob("*"):
        if path.is_file() and not path.name.endswith(("-wal", "-shm", "-journal")):
            name = path.relative_to(incoming).as_posix()
            dest = safe_path(staged, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    # 已有 .env 按键补齐；保留目标机器连接参数和既有密钥。
    env = staged / ".env"
    if (root / ".env").exists() and env.exists():
        text = (root / ".env").read_text(encoding="utf-8-sig")
        keys = {b.key for b in parse_stream(io.StringIO(text)) if b.key}
        additions = [
            b.original.string
            for b in parse_stream(io.StringIO(env.read_text(encoding="utf-8-sig")))
            if b.key and b.key not in keys
        ]
        env.write_text(text.rstrip() + "\n" + "".join(additions), encoding="utf-8")
    # 包内图库统一放 emotes，新部署不沿用来源机器的目录位置。
    from dotenv import set_key

    if env.exists():
        set_key(str(env), "EMOTES_DIR", emote_dir(root).relative_to(root).as_posix())
    library = emote_dir(root).relative_to(root).as_posix()
    renamed = {}
    for path in list(staged.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(staged).as_posix()
        target_name = library + name[len("emotes") :] if name.startswith("emotes/") else name
        target = safe_path(root, target_name)
        if name in (MEMORY, "emotes/.memory.sqlite3", IDENTITY, "group_chat.json", ".env"):
            plan[target_name] = path
            continue
        if target.exists() and target.read_bytes() != path.read_bytes() and not fresh:
            if name.startswith(("personas/", "styles/", "emotes/")):
                alternate = target.with_name(
                    target.stem + "_导入_" + digest(path)[:12] + target.suffix
                )
                target_name = alternate.relative_to(root).as_posix()
                renamed[name] = alternate.stem
        plan[target_name] = path
    group = staged / "group_chat.json"
    if group.exists():
        config = read_json(group)
        for settings in config.get("groups", {}).values():
            for field, directory in (("persona", "personas"), ("style", "styles")):
                selection = settings.get(field)
                if selection and directory + "/" + selection["name"] + ".txt" in renamed:
                    selection["name"] = renamed[directory + "/" + selection["name"] + ".txt"]
        if (root / "group_chat.json").exists() and not fresh:
            config = fill_missing(read_json(root / "group_chat.json"), config)
        write_json(group, config)
        from plugins.assistant.groupchat import GroupSettings

        GroupSettings(group).read()
    for name, function, local_name in (
        (MEMORY, merge_memory, MEMORY),
        ("emotes/.memory.sqlite3", merge_emotes, library + "/.memory.sqlite3"),
    ):
        if (incoming / name).exists():
            out = staged / name
            out.unlink(missing_ok=True)
            function(root / local_name, incoming / name, out)
            plan[local_name] = out
    write_json(staged / IDENTITY, {"bot_qq": qq})
    plan[IDENTITY] = staged / IDENTITY
    return plan


def recover(root):
    """按持久化日志撤销未完成发布；已恢复部分也可重复执行。"""
    journal = read_json(root / JOURNAL)
    rollback = safe_path(root, journal["rollback"])
    for name in sorted(
        journal["paths"], key=lambda n: not n.endswith(("-wal", "-shm", "-journal"))
    ):
        target = safe_path(root, name)
        previous = safe_path(rollback, name)
        if previous.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(previous, target)
        else:
            target.unlink(missing_ok=True)
    (root / JOURNAL).unlink()


def import_data(root, archive_path, bot_qq=None):
    with project_lock(root), project_lock(root, name=".migration.lock"):
        check_legacy_process(root)
        if (root / JOURNAL).exists():
            raise ValueError("请先执行 recover 恢复未完成导入")
        with tempfile.TemporaryDirectory(prefix="qqbot-import-") as temporary:
            work = Path(temporary)
            incoming, staged = work / "incoming", work / "staged"
            incoming.mkdir()
            staged.mkdir()
            manifest = unpack(archive_path, incoming)
            fresh = not (root / IDENTITY).exists() and not (root / MEMORY).exists()
            qq = account(root, bot_qq)
            if qq != manifest["bot_qq"]:
                raise ValueError("机器人 QQ 不同，拒绝导入")
            plan = stage_merge(root, incoming, staged, qq, fresh)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = root / "backups" / ("before-import-" + stamp + ".zip")
            _export_data(root, backup, qq)
            # 保存来源包及精确回滚副本；即使进程被强制终止也能恢复。
            shutil.copy2(archive_path, root / "backups" / ("import-source-" + stamp + ".zip"))
            rollback = root / "backups" / ("rollback-" + stamp)
            paths = list(plan)
            for name in list(plan):
                if name.endswith(".sqlite3"):
                    paths.extend(name + suffix for suffix in ("-wal", "-shm", "-journal"))
            for name in paths:
                target = safe_path(root, name)
                if target.exists():
                    previous = safe_path(rollback, name)
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, previous)
            write_json(
                root / JOURNAL, {"rollback": rollback.relative_to(root).as_posix(), "paths": paths}
            )
            try:
                for name in paths:
                    target = safe_path(root, name)
                    if name in plan:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(plan[name], target)
                    else:
                        target.unlink(missing_ok=True)
                (root / JOURNAL).unlink()
            except BaseException:
                recover(root)
                raise
            return backup


def main():
    parser = argparse.ArgumentParser(description="QQBot 全量私有数据迁移（不加密 ZIP）")
    parser.add_argument("--root", type=Path, default=ROOT, help="项目目录")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="导出当前账号全部迁移数据")
    export.add_argument("--output", type=Path)
    export.add_argument("--bot-qq", type=int)
    imp = sub.add_parser("import", help="同账号离线合并")
    imp.add_argument("archive", type=Path)
    imp.add_argument("--bot-qq", type=int, help="目标机器人 QQ；新部署必须指定")
    sub.add_parser("recover", help="撤销未完成导入")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "export":
            output = args.output or root / "backups" / (
                "qqbot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".zip"
            )
            result = export_data(root, output, args.bot_qq)
            print(
                f"导出完成：{output.resolve()}，机器人 QQ：{result['bot_qq']}，"
                f"文件数：{len(result['files'])}"
            )
        elif args.command == "import":
            backup = import_data(root, args.archive.resolve(), args.bot_qq)
            print(
                f"合并完成；原数据备份：{backup}。"
                "请核对 NapCat 登录账号与 OneBot Token 后启动机器人。"
            )
        else:
            with project_lock(root), project_lock(root, name=".migration.lock"):
                check_legacy_process(root)
                recover(root)
            print("已恢复导入前数据")
    except (ValueError, OSError, sqlite3.Error, KeyError) as error:
        # 不输出配置内容、密钥或数据库记录。
        detail = str(error) if type(error) is ValueError else "请检查路径、文件格式及权限"
        parser.exit(
            1,
            f"操作失败：{type(error).__name__}；{detail}\n",
        )


if __name__ == "__main__":
    main()
