"""配置加载：TOML 文件 + 环境变量覆盖。秘密只从环境变量进，不落文件。"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sams.core.errors import ValidationError

DEFAULT_CONFIG_PATH = Path("config/settings.toml")
EXAMPLE_CONFIG_PATH = Path("config/settings.example.toml")


@dataclass(frozen=True)
class MarketCfg:
    market: str
    tz: str
    #: 标的币种（spec D19）：采集入库时写进 symbols.currency，展示按需换算
    currency: str
    post_close_min: int
    sources: tuple[str, ...]
    #: 盘前/盘后时段 (名称, 开始 HH:MM, 结束 HH:MM)，市场本地时间；默认不参与轮询
    extended: tuple[tuple[str, str, str], ...] = ()

    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError(f"市场 {self.market} 时区非法: {self.tz}") from exc


#: 大盘条默认问的那九只（D145）：内置一份，配置没写时也能渲染出一条 bar。
DEFAULT_INDEX_CODES: dict[str, tuple[str, ...]] = {
    "A": ("sh000001", "sz399001", "sz399006"),
    "HK": ("hkHSI", "hkHSTECH", "hkHSCEI"),
    "US": ("usSPX", "usIXIC", "usDJI"),
}

#: 展示名进配置：各源的指数名参差（「上证指数」/「上证综指」/「sh000001」），
#: 界面念什么必须由这一份说了算，否则同一条 bar 换个源就改名字。
DEFAULT_INDEX_LABELS: dict[str, str] = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "hkHSI": "恒生指数",
    "hkHSTECH": "恒生科技指数",
    "hkHSCEI": "H股指数",
    "usSPX": "标普500",
    "usIXIC": "纳斯达克",
    "usDJI": "道琼斯",
}


@dataclass(frozen=True)
class IndicesCfg:
    """大盘条问「画哪几只、叫什么」，答案全部在这里（D145）。"""

    codes_by_market: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: {k: tuple(v) for k, v in DEFAULT_INDEX_CODES.items()}
    )
    labels: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_INDEX_LABELS))

    def for_market(self, market: str) -> tuple[str, ...]:
        return tuple(self.codes_by_market.get(market, ()))

    def label(self, index_code: str) -> str:
        return self.labels.get(index_code, index_code)


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class ServerCfg:
    """`sams serve` 的监听与登录策略（spec §7/§8）。

    默认只绑回环地址：这个产品先服务「本机一个人」。`0.0.0.0` 与「允许局域网」
    必须同时成立——单独一个 host 就开门，等于让一次改端口顺手把库暴露给整层楼。
    """

    host: str = "127.0.0.1"
    port: int = 8765
    allow_lan: bool = False
    session_ttl_days: int = 7
    idle_timeout_min: int = 720
    login_max_failures: int = 5
    login_window_min: int = 5
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    display_tz: str = "Asia/Shanghai"
    #: 推送深链的根地址；空 = 不附链接（D126）。只接受 http/https，末尾斜杠在加载时去掉
    public_base_url: str = ""

    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    def validate(self) -> None:
        # 安全闸必须 fail-closed：bool("false") 是 True，写错引号就会静默放开 LAN，
        # 所以这里拒绝一切非布尔值而不是帮用户「纠正」
        if not isinstance(self.allow_lan, bool):
            raise ValidationError("server.allow_lan 必须是布尔值")
        if not (1 <= self.port <= 65535):
            raise ValidationError(f"server.port 非法: {self.port}")
        if self.session_ttl_days <= 0:
            raise ValidationError("server.session_ttl_days 必须为正数")
        if self.idle_timeout_min <= 0:
            raise ValidationError("server.idle_timeout_min 必须为正数")
        if self.login_max_failures <= 0:
            raise ValidationError("server.login_max_failures 必须为正数")
        if self.login_window_min <= 0:
            raise ValidationError("server.login_window_min 必须为正数")
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise ValidationError("server.tls_certfile 与 tls_keyfile 必须成对给出")
        if not self.is_loopback() and not self.allow_lan:
            raise ValidationError(
                f"server.host={self.host!r} 不是回环地址：要监听局域网请同时设置 allow_lan = true"
            )
        try:
            ZoneInfo(self.display_tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError(f"server.display_tz 非法: {self.display_tz}") from exc
        if self.public_base_url and not self.public_base_url.startswith(
            ("http://", "https://")
        ):
            raise ValidationError(
                "server.public_base_url 只接受 http:// 或 https://："
                "一个猜出来的地址在手机上点不开，不如不放"
            )


@dataclass(frozen=True)
class AnnotationsCfg:
    """批注与附件的上限（spec §9）。所有上限都是**拒绝线**，不是截断线：
    悄悄截一半的正文比报错更难发现。"""

    attachments_dir: str = ""
    max_attachments: int = 5
    max_attachment_mb: int = 5
    max_user_total_mb: int = 512
    max_body_chars: int = 20000
    break_glass_minutes: int = 15
    break_glass_max_per_hour: int = 5

    def validate(self) -> None:
        for name, value in (
            ("max_attachments", self.max_attachments),
            ("max_attachment_mb", self.max_attachment_mb),
            ("max_user_total_mb", self.max_user_total_mb),
            ("max_body_chars", self.max_body_chars),
            ("break_glass_minutes", self.break_glass_minutes),
            ("break_glass_max_per_hour", self.break_glass_max_per_hour),
        ):
            if value <= 0:
                raise ValidationError(f"annotations.{name} 必须为正数")


@dataclass(frozen=True)
class Settings:
    db_path: str
    secret_key: str
    report_currency: str
    rate_limit_per_s: float
    polling_interval_s: int
    cooldown_min: int
    markets: dict[str, MarketCfg]
    #: 调度器的心跳/到期检查间隔（D97）。它**不是**盘中轮询间隔：
    #: `polling_interval_s` 预留给 M4/U7 的 10 秒快照，那条链目前还没有消费者；
    #: 盘后任务按分钟级检查就够了，把一个盘中参数借给它，会让改盘中频率顺手改掉盘后
    scheduler_tick_s: int = 60
    #: 报价超过这么久没更新 ⇒ `stale`，价格类规则不评估（spec D134）
    quote_stale_s: int = 60
    server: ServerCfg = field(default_factory=ServerCfg)
    annotations: AnnotationsCfg = field(default_factory=AnnotationsCfg)
    #: 大盘条的指数与展示名（D145）：默认那九只，配置写了整组覆盖
    indices: IndicesCfg = field(default_factory=IndicesCfg)

    def market_cfg(self, market: str) -> MarketCfg:
        try:
            return self.markets[market]
        except KeyError:
            known = ", ".join(sorted(self.markets))
            raise ValidationError(f"未知市场 {market!r}，已配置: {known}") from None

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        # 显式传参 > SAMS_CONFIG > 默认路径：命令行上指定的文件必须赢过环境变量，
        # 否则「我用 --config 指了另一个库」会静默操作环境变量里那个库
        cfg_path = Path(path or os.environ.get("SAMS_CONFIG") or DEFAULT_CONFIG_PATH)
        if not cfg_path.exists():
            raise ValidationError(
                f"配置文件不存在: {cfg_path}；请先 cp {EXAMPLE_CONFIG_PATH} {cfg_path}"
            )
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))

        storage = raw.get("storage", {})
        general = raw.get("general", {})
        db_path = os.environ.get("SAMS_DB_PATH") or storage.get("db_path", "data/sams.duckdb")
        secret_key = os.environ.get("SAMS_SECRET_KEY") or general.get("secret_key", "")
        if not secret_key:
            raise ValidationError("缺少 secret_key：请设置环境变量 SAMS_SECRET_KEY")

        markets: dict[str, MarketCfg] = {}
        for market, cfg in (raw.get("markets") or {}).items():
            extended = tuple(
                (name, str(rng["open"]), str(rng["close"]))
                for name, rng in (cfg.get("extended") or {}).items()
            )
            markets[market] = MarketCfg(
                market=market,
                tz=cfg["tz"],
                currency=str(cfg.get("currency") or "").upper(),
                post_close_min=int(cfg.get("post_close_min", 30)),
                sources=tuple(cfg.get("sources") or ()),
                extended=extended,
            )
        if not markets:
            raise ValidationError("配置缺少 [markets.*] 段")

        server_raw = raw.get("server") or {}
        server = ServerCfg(
            host=str(server_raw.get("host", "127.0.0.1")),
            port=int(server_raw.get("port", 8765)),
            allow_lan=server_raw.get("allow_lan", False),
            session_ttl_days=int(server_raw.get("session_ttl_days", 7)),
            idle_timeout_min=int(server_raw.get("idle_timeout_min", 720)),
            login_max_failures=int(server_raw.get("login_max_failures", 5)),
            login_window_min=int(server_raw.get("login_window_min", 5)),
            tls_certfile=(
                str(server_raw["tls_certfile"]) if server_raw.get("tls_certfile") else None
            ),
            tls_keyfile=str(server_raw["tls_keyfile"]) if server_raw.get("tls_keyfile") else None,
            display_tz=str(server_raw.get("display_tz", "Asia/Shanghai")),
            public_base_url=str(server_raw.get("public_base_url", "")).rstrip("/"),
        )

        annotations_raw = raw.get("annotations") or {}
        annotations = AnnotationsCfg(
            attachments_dir=str(annotations_raw.get("attachments_dir", "")),
            max_attachments=int(annotations_raw.get("max_attachments", 5)),
            max_attachment_mb=int(annotations_raw.get("max_attachment_mb", 5)),
            max_user_total_mb=int(annotations_raw.get("max_user_total_mb", 512)),
            max_body_chars=int(annotations_raw.get("max_body_chars", 20000)),
            break_glass_minutes=int(annotations_raw.get("break_glass_minutes", 15)),
            break_glass_max_per_hour=int(
                annotations_raw.get("break_glass_max_per_hour", 5)
            ),
        )

        idx_raw = raw.get("indices") or {}
        codes_by_market: dict[str, tuple[str, ...]] = {}
        # 只给已配置的市场铺默认：一个没配的市场凭空有三只指数，job 会登记、日线会入库，
        # 而没有任何一屏能解释它为什么在那里
        for market_name in markets:
            given = (idx_raw.get(market_name) or {}).get("codes")
            if given is not None and not isinstance(given, (list, tuple)):
                raise ValidationError(f"[indices.{market_name}] codes 必须是代码数组")
            codes_by_market[market_name] = (
                tuple(given) if given else tuple(DEFAULT_INDEX_CODES.get(market_name, ()))
            )
        labels = dict(DEFAULT_INDEX_LABELS)
        labels.update({str(k): str(v) for k, v in (idx_raw.get("labels") or {}).items()})
        indices = IndicesCfg(codes_by_market=codes_by_market, labels=labels)

        settings = cls(
            db_path=str(db_path),
            secret_key=str(secret_key),
            report_currency=general.get("report_currency", "CNY"),
            rate_limit_per_s=float(general.get("rate_limit_per_s", 5)),
            polling_interval_s=int(general.get("polling_interval_s", 10)),
            cooldown_min=int(general.get("cooldown_min", 30)),
            markets=markets,
            scheduler_tick_s=int(general.get("scheduler_tick_s", 60)),
            quote_stale_s=int(general.get("quote_stale_s", 60)),
            server=server,
            annotations=annotations,
            indices=indices,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.rate_limit_per_s <= 0:
            raise ValidationError("rate_limit_per_s 必须为正数")
        if self.polling_interval_s <= 0:
            raise ValidationError("polling_interval_s 必须为正数")
        if self.cooldown_min < 0:
            raise ValidationError("cooldown_min 不能为负")
        if self.scheduler_tick_s <= 0:
            raise ValidationError("scheduler_tick_s 必须为正数")
        if self.quote_stale_s <= 0:
            raise ValidationError("quote_stale_s 必须为正数")
        self.server.validate()
        self.annotations.validate()
        if not self.report_currency:
            raise ValidationError("report_currency 不能为空")
        for market, cfg in self.markets.items():
            cfg.zone()  # 尽早失败，别等常驻循环才炸
            if not cfg.currency:
                raise ValidationError(f"{market}.currency 不能为空")
            if cfg.post_close_min < 0:
                raise ValidationError(f"{market}.post_close_min 不能为负")
            if not cfg.sources:
                raise ValidationError(f"{market}.sources 为空，无法采集")
