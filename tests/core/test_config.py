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
