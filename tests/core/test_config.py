from __future__ import annotations

from pathlib import Path

import pytest

from sams.core.config import Settings
from sams.core.errors import ValidationError

BASE = """
[storage]
db_path = "data/x.duckdb"

[general]
secret_key = "from-file"
rate_limit_per_s = 3

[markets.A]
tz = "Asia/Shanghai"
currency = "cny"
post_close_min = 20
sources = ["akshare"]
"""


def write(tmp_path: Path, text: str = BASE) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_from_file(tmp_path):
    settings = Settings.load(write(tmp_path))
    assert settings.secret_key == "from-file"
    assert settings.rate_limit_per_s == 3
    assert settings.market_cfg("A").currency == "CNY"  # 币种统一大写，入库才不会有 cny/CNY 两行


def test_env_overrides_secret_and_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMS_SECRET_KEY", "from-env")
    monkeypatch.setenv("SAMS_DB_PATH", "/tmp/other.duckdb")
    settings = Settings.load(write(tmp_path))
    assert settings.secret_key == "from-env"
    assert settings.db_path == "/tmp/other.duckdb"


def test_missing_secret_key_fails(tmp_path):
    path = write(tmp_path, BASE.replace('secret_key = "from-file"', ""))
    with pytest.raises(ValidationError, match="secret_key"):
        Settings.load(path)


def test_missing_file_tells_you_to_copy_the_example(tmp_path):
    with pytest.raises(ValidationError, match="settings.example.toml"):
        Settings.load(tmp_path / "nope.toml")


def test_unknown_market_lists_known_ones(tmp_path):
    settings = Settings.load(write(tmp_path))
    with pytest.raises(ValidationError, match="A"):
        settings.market_cfg("JP")


@pytest.mark.parametrize(
    "old, new, keyword",
    [
        ("rate_limit_per_s = 3", "rate_limit_per_s = 0", "rate_limit_per_s"),
        ("rate_limit_per_s = 3", "rate_limit_per_s = 3\npolling_interval_s = 0", "polling"),
        ("rate_limit_per_s = 3", "rate_limit_per_s = 3\ncooldown_min = -1", "cooldown"),
    ],
)
def test_general_numbers_must_be_sane(tmp_path, old, new, keyword):
    with pytest.raises(ValidationError, match=keyword):
        Settings.load(write(tmp_path, BASE.replace(old, new)))


def test_empty_sources_and_currency_and_tz_are_rejected(tmp_path):
    cases = [
        ('sources = ["akshare"]', "sources = []"),
        ('currency = "cny"', 'currency = ""'),
        ('tz = "Asia/Shanghai"', 'tz = "Mars/Olympus"'),
    ]
    for old, new in cases:
        text = BASE.replace(old, new)
        with pytest.raises(ValidationError):
            Settings.load(write(tmp_path, text))


def test_extended_hours_are_read_as_triplets(tmp_path):
    text = BASE + """
[markets.A.extended.pre]
open = "08:00"
close = "09:30"
"""
    settings = Settings.load(write(tmp_path, text))
    assert settings.market_cfg("A").extended == (("pre", "08:00", "09:30"),)


def test_scheduler_tick_defaults_to_60(tmp_path):
    assert Settings.load(write(tmp_path)).scheduler_tick_s == 60


def test_scheduler_tick_is_read_and_validated(tmp_path):
    read = write(tmp_path, BASE.replace("[general]", "[general]\nscheduler_tick_s = 15"))
    assert Settings.load(read).scheduler_tick_s == 15

    with pytest.raises(ValidationError, match="scheduler_tick_s"):
        Settings.load(write(tmp_path, BASE.replace("[general]", "[general]\nscheduler_tick_s = 0")))


def test_server_defaults_are_loopback_only(tmp_path):
    settings = Settings.load(write(tmp_path))
    assert (settings.server.host, settings.server.port) == ("127.0.0.1", 8765)
    assert settings.server.allow_lan is False
    assert settings.server.session_ttl_days == 7
    assert settings.server.idle_timeout_min == 720
    assert settings.server.display_tz == "Asia/Shanghai"
    assert settings.quote_stale_s == 60


def test_server_section_is_read(tmp_path):
    text = BASE + """
[server]
host = "127.0.0.1"
port = 9000
allow_lan = false
session_ttl_days = 3
idle_timeout_min = 60
login_max_failures = 4
login_window_min = 2
display_tz = "Asia/Hong_Kong"
tls_certfile = "/tmp/cert.pem"
tls_keyfile = "/tmp/key.pem"
"""
    server = Settings.load(write(tmp_path, text)).server
    assert server.port == 9000 and server.session_ttl_days == 3
    assert server.login_max_failures == 4 and server.display_tz == "Asia/Hong_Kong"
    assert server.tls_certfile == "/tmp/cert.pem" and server.tls_keyfile == "/tmp/key.pem"
    assert server.idle_timeout_min == 60
    assert server.login_window_min == 2


def test_quote_stale_seconds_is_read(tmp_path):
    text = BASE.replace("[general]", "[general]\nquote_stale_s = 30")
    assert Settings.load(write(tmp_path, text)).quote_stale_s == 30
    with pytest.raises(ValidationError, match="quote_stale_s"):
        Settings.load(write(tmp_path, BASE.replace("[general]", "[general]\nquote_stale_s = 0")))


def test_a_non_loopback_host_requires_allow_lan(tmp_path):
    text = BASE + '\n[server]\nhost = "0.0.0.0"\n'
    with pytest.raises(ValidationError, match="allow_lan"):
        Settings.load(write(tmp_path, text))
    allowed = write(tmp_path, text + "allow_lan = true\n")
    assert Settings.load(allowed).server.allow_lan is True


def test_tls_pair_must_be_complete(tmp_path):
    half = BASE + '\n[server]\ntls_certfile = "/tmp/cert.pem"\n'
    with pytest.raises(ValidationError, match="tls"):
        Settings.load(write(tmp_path, half))


def test_tls_keyfile_alone_is_also_incomplete(tmp_path):
    half = BASE + '\n[server]\ntls_keyfile = "/tmp/key.pem"\n'
    with pytest.raises(ValidationError, match="tls"):
        Settings.load(write(tmp_path, half))


@pytest.mark.parametrize(
    "line, keyword",
    [
        ("port = 0", "port"),
        ("port = 70000", "port"),
        ("session_ttl_days = 0", "session_ttl_days"),
        ("idle_timeout_min = 0", "idle_timeout_min"),
        ("login_max_failures = 0", "login_max_failures"),
        ("login_window_min = 0", "login_window_min"),
        ('display_tz = "Mars/Olympus"', "display_tz"),
        ('allow_lan = "false"', "allow_lan"),
    ],
)
def test_server_numbers_and_timezone_are_validated(tmp_path, line, keyword):
    text = BASE + f"\n[server]\n{line}\n"
    with pytest.raises(ValidationError, match=keyword):
        Settings.load(write(tmp_path, text))


def test_annotations_defaults():
    from sams.core.config import AnnotationsCfg

    cfg = AnnotationsCfg()
    assert cfg.attachments_dir == ""
    assert cfg.max_attachments == 5
    assert cfg.max_attachment_mb == 5
    assert cfg.max_user_total_mb == 512
    assert cfg.max_body_chars == 20000
    assert cfg.break_glass_minutes == 15
    assert cfg.break_glass_max_per_hour == 5


def test_annotations_section_is_read(tmp_path):
    text = BASE + """
[annotations]
attachments_dir = "/srv/sams/attachments"
max_attachments = 3
max_body_chars = 5000
"""
    settings = Settings.load(write(tmp_path, text))
    assert settings.annotations.attachments_dir == "/srv/sams/attachments"
    assert settings.annotations.max_attachments == 3
    assert settings.annotations.max_body_chars == 5000


@pytest.mark.parametrize(
    "line",
    (
        "max_attachments = 0",
        "max_attachment_mb = 0",
        "max_user_total_mb = 0",
        "max_body_chars = 0",
        "break_glass_minutes = 0",
        "break_glass_max_per_hour = 0",
    ),
)
def test_annotation_limits_must_be_positive(tmp_path, line):
    text = BASE + "\n[annotations]\n" + line + "\n"
    with pytest.raises(ValidationError):
        Settings.load(write(tmp_path, text))


def test_public_base_url_is_trimmed_and_optional(tmp_path):
    settings = Settings.load(write(tmp_path))
    assert settings.server.public_base_url == ""

    text = BASE + '\n[server]\npublic_base_url = "https://sams.example.com/"\n'
    settings = Settings.load(write(tmp_path, text))
    assert settings.server.public_base_url == "https://sams.example.com"


def test_public_base_url_must_be_http_or_https(tmp_path):
    text = BASE + '\n[server]\npublic_base_url = "ftp://example.com"\n'
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings.load(write(tmp_path, text))


THREE_MARKETS = BASE + """
[markets.HK]
tz = "Asia/Hong_Kong"
currency = "HKD"
sources = ["yfinance"]

[markets.US]
tz = "America/New_York"
currency = "USD"
sources = ["yfinance"]
"""


def test_indices_defaults(tmp_path):
    """不写 `[indices]` 也要有内置那九只：大盘条不该因为没人配过就整条不渲染（D145）。"""
    s = Settings.load(write(tmp_path, THREE_MARKETS))
    assert s.indices.for_market("A") == ("sh000001", "sz399001", "sz399006")
    assert s.indices.for_market("HK") == ("hkHSI", "hkHSTECH", "hkHSCEI")
    assert s.indices.for_market("US") == ("usSPX", "usIXIC", "usDJI")
    assert s.indices.label("sh000001") == "上证指数"


def test_indices_override(tmp_path):
    """写了的市场整组覆盖：默认那九只不会和自定义的混成一段没法解释的清单。"""
    text = THREE_MARKETS + """
[indices.A]
codes = ["sh000001"]

[indices.labels]
sh000001 = "沪综指"
"""
    s = Settings.load(write(tmp_path, text))
    assert s.indices.for_market("A") == ("sh000001",)
    assert s.indices.label("sh000001") == "沪综指"
    assert s.indices.label("unknown") == "unknown"


def test_indices_codes_must_be_an_array(tmp_path):
    """`codes = "sh000001"` 会被 tuple() 拆成一串单字符「指数」：静默入库，bar 上一条都不认识。"""
    text = THREE_MARKETS + """
[indices.A]
codes = "sh000001"
"""
    with pytest.raises(ValidationError, match=r"\[indices\.A\] codes"):
        Settings.load(write(tmp_path, text))
