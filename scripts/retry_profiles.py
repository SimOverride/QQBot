"""查看或显式恢复暂停的认知批次，不输出聊天正文和密钥。"""

import argparse
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


def retry(path, *, all_suspended=False, key=None):
    """保留批次范围，清零预算并使在途请求的编号失效。"""
    with closing(sqlite3.connect(path)) as db, db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='profile_retry'"
        ).fetchone():
            raise ValueError("请先用更新后的代码启动机器人，再查询重试状态。")
        if not all_suspended and key is None:
            rows = db.execute(
                "SELECT attempts,COUNT(*) FROM profile_retry GROUP BY attempts"
            ).fetchall()
            return {
                "suspended": sum(n for a, n in rows if a >= 3),
                "waiting": sum(n for a, n in rows if 0 < a < 3),
            }
        where, params = (
            ("attempts>=3", []) if all_suspended else ("bot=? AND grp=? AND usr=?", list(key))
        )
        count = db.execute(
            "UPDATE profile_retry SET attempts=0,retry_at=0,attempt_id=?,error_type='' WHERE "
            + where,
            [uuid.uuid4().hex, *params],
        ).rowcount
        return {"resumed": count}


def main():
    parser = argparse.ArgumentParser(description="查看认知重试状态；显式恢复后下轮后台检查重试")
    parser.add_argument("--all", action="store_true", help="恢复全部已暂停批次")
    parser.add_argument("--bot", type=int, help="机器人 QQ")
    parser.add_argument("--group", type=int, help="群号；私聊使用负的本人 QQ")
    parser.add_argument("--user", type=int, help="用户 QQ")
    args = parser.parse_args()
    values = (args.bot, args.group, args.user)
    if any(v is not None for v in values) and not all(v is not None for v in values):
        parser.error("定向恢复必须同时指定 --bot、--group 和 --user")
    if args.all and any(v is not None for v in values):
        parser.error("--all 与定向恢复不能同时使用")
    path = Path(__file__).resolve().parents[1] / "data/memory.sqlite3"
    if not path.is_file():
        parser.error("聊天数据库不存在")
    try:
        result = retry(path, all_suspended=args.all, key=values if args.bot is not None else None)
    except (ValueError, sqlite3.Error) as error:
        parser.exit(1, str(error) + "\n")
    if "resumed" in result:
        print(f"已恢复 {result['resumed']} 个认知批次，下轮后台检查按预算重试。")
    else:
        print(f"暂停 {result['suspended']} 个批次，等待重试 {result['waiting']} 个批次。")


if __name__ == "__main__":
    main()
