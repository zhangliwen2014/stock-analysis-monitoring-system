"""投递端口：形状要在 core，实现要在 monitor，两边各钉一枚。

`AlertSender` 是 `services` 能看到的唯一发送出口，所以它的参数表必须与
`NotifyService.dispatch` 一模一样——差一个参数名，接上去的那一行就会在 M3-D 挂机跑到
某条规则时才炸，而那时红的是网络对面的事。

生产里唯一接上这个端口的是 `bootstrap._NotifyAlertSender`：它那一头返回的是
`DispatchResult`，端口要的是 id 串。折法（取 `.alert_id`）与「不吞异常」都钉在这里——
参数表对齐而本体没人调过，等于只验了信封没验信纸。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from sams.bootstrap import _NotifyAlertSender
from sams.core.notify import AlertMessage, AlertSender
from sams.monitor.router import DispatchResult

NOW = datetime(2026, 10, 20, 7, 30, tzinfo=UTC)


def message() -> AlertMessage:
    return AlertMessage(market="A", code="601088", severity="strong", triggered_at=NOW)


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


class _FakeNotify:
    """`NotifyService` 的替身：只记录收到什么、返回一个哨兵结果。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids=(),
        rule_id=None,
        account_id=None,
    ) -> DispatchResult:
        self.calls.append(
            {
                "message": message,
                "user_id": user_id,
                "channel_ids": channel_ids,
                "rule_id": rule_id,
                "account_id": account_id,
            }
        )
        #: 那条 `DispatchResult` 里除了 id 还有每通道的 `DeliveryReport`：那些留在
        #: `monitor` 那一头由 `sams alert show` 去查，端口这一侧只许看到 id
        return DispatchResult("alert-42", ())


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


def test_the_production_adapter_folds_the_result_into_its_id() -> None:
    """唯一的生产接线：解出 `.alert_id` 原样透传，参数一字不改地递过去。"""
    fake = _FakeNotify()
    adapter = _NotifyAlertSender(fake)
    got = adapter.dispatch(
        message(),
        user_id="u-rule",
        channel_ids=["c-1", "c-2"],
        rule_id="r-1",
        account_id="acc-1",
    )
    assert got == "alert-42"
    assert isinstance(got, str)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["user_id"] == "u-rule"
    assert list(call["channel_ids"]) == ["c-1", "c-2"]
    assert call["rule_id"] == "r-1" and call["account_id"] == "acc-1"
    assert call["message"] is not None


def test_the_production_adapter_answers_the_port() -> None:
    """`isinstance` 走 `runtime_checkable`：认的是「有 `dispatch` 这个方法」，
    而参数表那一头由上面那枚逐字钉住——两头合起来才是「接得上」。"""
    assert isinstance(_NotifyAlertSender(_FakeNotify()), AlertSender)


def test_the_production_adapter_does_not_swallow_a_failure() -> None:
    """不吞异常：投递炸了就让它炸上去。

    折成空串或记一条日志的话，屏幕上那条「已推送」与库里那一行就对不上了，
    而 M3-D 的调度会以为这一条发成功、继续按冷却压下一条。
    """

    class _Boom:
        def dispatch(self, *args, **kwargs) -> DispatchResult:
            raise RuntimeError("通道全断了")

    with pytest.raises(RuntimeError, match="通道全断了"):
        _NotifyAlertSender(_Boom()).dispatch(message(), user_id="u-rule")
