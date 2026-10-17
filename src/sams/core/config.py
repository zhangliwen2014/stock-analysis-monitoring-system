"""配置加载：TOML 文件 + 环境变量覆盖。秘密只从环境变量进，不落文件。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Settings:
    db_path: str
    secret_key: str
    report_currency: str
    rate_limit_per_s: float
    polling_interval_s: int
    cooldown_min: int
    markets: dict[str, MarketCfg]

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

        settings = cls(
            db_path=str(db_path),
            secret_key=str(secret_key),
            report_currency=general.get("report_currency", "CNY"),
            rate_limit_per_s=float(general.get("rate_limit_per_s", 5)),
            polling_interval_s=int(general.get("polling_interval_s", 10)),
            cooldown_min=int(general.get("cooldown_min", 30)),
            markets=markets,
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
