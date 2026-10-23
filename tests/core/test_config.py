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

[general.quote_stale_s]
"""
    # quote_stale_s 属于 [general]：上面那段只验证 [server] 的读取
    read = write(tmp_path, text.replace("[general.quote_stale_s]\n", ""))
    server = Settings.load(read).server
    assert server.port == 9000 and server.session_ttl_days == 3
    assert server.login_max_failures == 4 and server.display_tz == "Asia/Hong_Kong"
    assert server.tls_certfile == "/tmp/cert.pem" and server.tls_keyfile == "/tmp/key.pem"


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
    ],
)
def test_server_numbers_and_timezone_are_validated(tmp_path, line, keyword):
    text = BASE + f"\n[server]\n{line}\n"
    with pytest.raises(ValidationError, match=keyword):
        Settings.load(write(tmp_path, text))
