"""`sams db` —— 建库、迁移状态与保留策略。"""

from __future__ import annotations

import argparse

from sams.cli.common import group
from sams.services.annotation_prune import ORPHAN_DAYS, PURGED_DAYS
from sams.services.prune import KEEP_HEARTBEATS, KEEP_QUOTE_SESSIONS, KEEP_TASK_RUN_DAYS


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} 不是整数") from None
    if value < 1:
        raise argparse.ArgumentTypeError("要大于 0")
    return value


def add_to(subparsers) -> None:
    actions = group(subparsers, "db", "数据库维护")
    for name, handler, help_text in (
        ("migrate", migrate, "应用尚未执行的迁移"),
        ("status", status, "显示已应用 / 待应用的迁移"),
    ):
        parser = actions.add_parser(name, help=help_text)
        parser.set_defaults(handler=handler)

    parser = actions.add_parser(
        "prune",
        help="按保留策略清理报价快照、任务台账与调度心跳（sams serve 每日 UTC 03:30 也执行）",
    )
    parser.add_argument(
        "--quotes-sessions",
        type=_positive,
        default=KEEP_QUOTE_SESSIONS,
        metavar="N",
        help=f"每个市场保留最近 N 个有快照的交易日（默认 {KEEP_QUOTE_SESSIONS}）",
    )
    parser.add_argument(
        "--task-runs-days",
        type=_positive,
        default=KEEP_TASK_RUN_DAYS,
        metavar="N",
        help=f"task_runs 保留最近 N 天（默认 {KEEP_TASK_RUN_DAYS}）",
    )
    parser.add_argument(
        "--heartbeats-keep",
        type=_positive,
        default=KEEP_HEARTBEATS,
        metavar="N",
        help=f"调度心跳只留最近 N 条（默认 {KEEP_HEARTBEATS}）",
    )
    parser.add_argument(
        "--annotations-purged-days",
        type=_positive,
        default=PURGED_DAYS,
        metavar="N",
        help=f"软删满 N 天的批注硬清除（默认 {PURGED_DAYS}）",
    )
    parser.add_argument(
        "--attachments-orphan-days",
        type=_positive,
        default=ORPHAN_DAYS,
        metavar="N",
        help=f"没有对应批注行的附件文件，超过 N 天未变动的回收（默认 {ORPHAN_DAYS}）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将删行数，不动库")
    parser.set_defaults(handler=prune)


def migrate(container, args) -> int:
    newly = container.migrate()
    if newly:
        print(f"已应用迁移: {', '.join(newly)}")
    else:
        print("数据库已是最新，无待应用迁移")
    print(f"库: {container.db.path}  表: {len(container.db.table_names())} 张")
    return 0


def status(container, args) -> int:
    done = sorted(container.applied_migrations())
    todo = container.pending_migrations()
    print(f"已应用 {len(done)} 个: {', '.join(done) or '（无）'}")
    print(f"待应用 {len(todo)} 个: {', '.join(todo) or '（无）'}")
    print(f"数据库路径: {container.db.path}")
    return 0


def prune(container, args) -> int:
    report = container.prune.run(
        quotes_sessions=args.quotes_sessions,
        task_runs_days=args.task_runs_days,
        heartbeats_keep=args.heartbeats_keep,
        annotations_purged_days=args.annotations_purged_days,
        attachments_orphan_days=args.attachments_orphan_days,
        dry_run=args.dry_run,
    )
    verb = "将删除" if report.dry_run else "删除"
    for step in report.steps:
        print(f"{step.name}: {verb} {step.rows} {step.unit}")
    if report.files or report.file_bytes:
        print(f"文件: {verb} {report.files} 个（{report.file_bytes} 字节）")
    print(f"{'预计' if report.dry_run else '合计'}: {report.total} 行")
    return 0
