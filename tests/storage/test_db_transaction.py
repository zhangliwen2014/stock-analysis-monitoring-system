"""`Database.transaction()`：仓库层的多表写必须有一个原子出口。"""

from __future__ import annotations

import pytest

from sams.core.errors import StorageError
from sams.storage.db import Database


@pytest.fixture
def widget(db: Database) -> Database:
    db.execute("CREATE TABLE widget (id INTEGER PRIMARY KEY)")
    return db


def test_a_committed_block_leaves_every_row(widget: Database) -> None:
    with widget.transaction():
        widget.execute("INSERT INTO widget VALUES (1)")
        widget.execute("INSERT INTO widget VALUES (2)")
    assert widget.row_count("widget") == 2


def test_an_exception_rolls_back_the_whole_block(widget: Database) -> None:
    """回滚必须是整段：留下第 1 行就等于留下一笔已经扣了现金却没建的仓。"""
    with pytest.raises(ValueError), widget.transaction():
        widget.execute("INSERT INTO widget VALUES (1)")
        raise ValueError("后半段炸了")
    assert widget.row_count("widget") == 0


def test_nested_blocks_join_the_outer_one(widget: Database) -> None:
    """内层不另开事务，也不自己提交。

    `record_fill` 包住一整笔成交，顺手动现金的 `adjust_cash` 自己也包一层。如果嵌套
    报错，这两个方法就只能有一个真的开事务，而「谁负责原子性」从代码掉进口头约定；
    DuckDB 对嵌套 BEGIN 的原始报错读起来又像驱动 bug。所以这里明确支持重入。
    """
    with widget.transaction():
        with widget.transaction():
            widget.execute("INSERT INTO widget VALUES (1)")
        assert widget.row_count("widget") == 1  # 外层还没提交，但同连接可见
    assert widget.row_count("widget") == 1


def test_an_inner_exception_rolls_back_the_outer_block(widget: Database) -> None:
    with pytest.raises(ValueError), widget.transaction():
        widget.execute("INSERT INTO widget VALUES (1)")
        with widget.transaction():
            raise ValueError("内层炸了")
    assert widget.row_count("widget") == 0


def test_the_transaction_flag_reflects_the_outermost_block(widget: Database) -> None:
    assert widget.in_transaction is False
    with widget.transaction():
        assert widget.in_transaction is True
        with widget.transaction():
            assert widget.in_transaction is True
        assert widget.in_transaction is True
    assert widget.in_transaction is False


def test_a_storage_error_inside_the_block_rolls_back_too(widget: Database) -> None:
    """主键冲突是仓库层最常见的失败，它必须和异常一样整段撤销。

    这里点名 `StorageError` 而不是 `Exception`：后者会把「根本没有 transaction()」
    也算成通过，一条本该红的用例就从此不说任何话了。
    """
    with pytest.raises(StorageError), widget.transaction():
        widget.execute("INSERT INTO widget VALUES (1)")
        widget.execute("INSERT INTO widget VALUES (1)")
    assert widget.row_count("widget") == 0
