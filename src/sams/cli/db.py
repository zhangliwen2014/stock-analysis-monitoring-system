"""`sams db` —— 建库与迁移状态。"""

from __future__ import annotations

from sams.cli.common import group


def add_to(subparsers) -> None:
    actions = group(subparsers, "db", "数据库维护")
    for name, handler, help_text in (
        ("migrate", migrate, "应用尚未执行的迁移"),
        ("status", status, "显示已应用 / 待应用的迁移"),
    ):
        parser = actions.add_parser(name, help=help_text)
        parser.set_defaults(handler=handler)


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
