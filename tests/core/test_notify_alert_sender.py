"""投递端口：形状要在 core，实现要在 monitor，两边各钉一枚。

`AlertSender` 是 `services` 能看到的唯一发送出口，所以它的参数表必须与
`NotifyService.dispatch` 一模一样——差一个参数名，接上去的那一行就会在 M3-D 挂机跑到
某条规则时才炸，而那时红的是网络对面的事。
"""

from __future__ import annotations

import inspect

from sams.core.notify import AlertMessage, AlertSender


class _Stub:
    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids=(),
        rule_id=None,
        account_id=None,
    ) -> str:
        return "alert-1"


def test_a_stub_with_the_right_shape_satisfies_the_port() -> None:
    assert isinstance(_Stub(), AlertSender)


def test_the_real_router_still_answers_the_port() -> None:
    """参数表逐字对齐（返回类型**应当**不同：那边是 `DispatchResult`，这里是 id 串）。"""
    from sams.monitor.router import NotifyService

    # 两边都开了 PEP 695 的 `from __future__ import annotations`（注解是字符串），
    # 所以这里 `eval_str=True` 求值后再比：端口那侧的返回注解必须是真正的 `str`。
    port = inspect.signature(AlertSender.dispatch, eval_str=True)
    real = inspect.signature(NotifyService.dispatch, eval_str=True)
    assert list(port.parameters) == list(real.parameters)
    assert port.return_annotation is str
