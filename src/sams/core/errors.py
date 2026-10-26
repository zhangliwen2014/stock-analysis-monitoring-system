"""SAMS 异常类型。分层原则：数据源异常与领域校验异常不混用。"""

import re

#: DuckDB 冲突锁那句话里唯一还是「谁在拿库」的证据：PID。
#: 解析放在异常里而不是调用方：库打不开的那一刻，除了这条消息没有别的来源。
_HOLDER_PID = re.compile(r"\(PID (\d+)\)")


class SamsError(Exception):
    """所有本项目异常的基类。"""


class ValidationError(SamsError):
    """入参校验失败（用户名、密码、阈值、币种等）。"""


class StorageError(SamsError):
    """仓库层错误：迁移失败、约束冲突等。"""


class NotFound(SamsError):
    """要的东西不存在，或者存在但不该让当前用户知道。

    两者用同一个异常：对屏幕外的探测者，「别人的私有批注」与「没有这条批注」
    必须长得一样，否则一条 404 与一条 403 就把「谁写过什么」泄露了一半。
    """


class Conflict(SamsError):
    """当前状态不允许这个动作：已处理的告警再处置一次、编辑时版本已被别人改过。

    Web 层把它映射成 409，CLI 映射成退出码 1。单独一个类型而不是 `ValidationError`：
    参数本身没敲错，错的是这一刻的库——重试同样的表单不会成功，要先刷新。
    """


class TooManyRequests(SamsError):
    """滑动窗口内的次数用尽（spec §6.5:342）：break-glass 每小时超限走这一条。

    与 `Conflict` 分开是因为处置不同：Conflict 是「这条现在不能批」（墓碑、状态不符），
    要换动作；这一条是「过一会儿还能批」，消息里必须带等待秒数，Web 层映射成 429。
    """


class DatabaseLocked(StorageError):
    """库文件被另一个进程以读写方式打开（DuckDB 单写者）。

    单独一个类型而不是「又一种 `StorageError`」：处置完全不同。配置错误去改文件，
    这个去等常驻调度器退出；而「锁住」本身不是故障——那个进程可能正在干活。
    DuckDB 1.5.6 的实测是另一个进程**连只读连接也打不开**，所以这里没有
    「绕开锁只读」的中间道路：读不到就是读不到，只能改读库旁那一拍（`beatfile`）。
    """

    def __init__(self, path: str, detail: str = "") -> None:
        self.path = path
        self.detail = detail
        match = _HOLDER_PID.search(detail)
        self.holder_pid = int(match.group(1)) if match else None
        message = f"数据库 {path} 正被另一个进程以读写方式打开"
        if self.holder_pid is not None:
            message += f"（PID {self.holder_pid}）"
        message += "；DuckDB 单写者，换只读连接也打不开"
        super().__init__(message)


class DatasourceError(SamsError):
    """采集层基类。"""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"[{source}] {message}")
        self.source = source


class RateLimited(DatasourceError):
    """被限流（HTTP 429 或接口返回频率提示）。可重试。"""


class TransientError(DatasourceError):
    """网络抖动、超时等瞬时故障——值得退避重试，但不是被限流。

    路由器据此区分「退避后重试」与「换源/放弃」：对结构漂移做重试毫无意义。
    """


class EmptyResponse(DatasourceError):
    """接口正常响应但无数据。用于区分「停牌/无数据」与「真的失败」。"""


class FieldDrift(DatasourceError):
    """接口返回结构缺字段——免费数据源改版，必须显式失败而非静默填 NaN。"""


class CalendarSourceError(SamsError):
    """交易日历来源不可用。"""


class NotAdjustable(SamsError):
    """区间涨跌无法复权 ⇒ 不出数。

    裸价跨除权日算出来的涨跌是**一个看起来完全正常的错数**（601088 的 −3.81% vs
    交易所公布的 +1.26%），所以这一层的拒绝不是保守而是必要：算不出必须说算不出，
    而且原因要一起抛出去——「没采集过」和「确实没分过红」对使用者是两个结论。
    """


class SchedulerBusy(SamsError):
    """同一份库上已经有一个心跳还热的调度器（D100）。

    单独一个类型而不是 `ValidationError`：这不是「参数敲错了」（退出 2 会让人去查命令），
    而是「这件事已经有一个循环在做」——该做的动作是把那一个停掉，不是改命令行。
    """
