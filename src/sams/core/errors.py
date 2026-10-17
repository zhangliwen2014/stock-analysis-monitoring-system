"""SAMS 异常类型。分层原则：数据源异常与领域校验异常不混用。"""


class SamsError(Exception):
    """所有本项目异常的基类。"""


class ValidationError(SamsError):
    """入参校验失败（用户名、密码、阈值、币种等）。"""


class StorageError(SamsError):
    """仓库层错误：迁移失败、约束冲突等。"""


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
