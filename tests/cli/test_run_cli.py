"""`sams run start|once|jobs`：钉的是「给定一份读数，屏幕上该有什么、以什么码退出」。

替身只回答三个问题——注册了什么、`tick` 收到哪些参数、循环推了几轮。「该不该跑这一轮」
「连败几个周期才告警」那些判断在 `tests/services/test_scheduler_service.py` 里钉过；
这一层去重演它们，同一句话就会有两份各自失效的主人。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from sams.cli import build_parser
from sams.core.errors import SchedulerBusy, ValidationError
from sams.services.scheduler.due import DAILY, FX, WEEKLY
from sams.services.scheduler.runner import SKIPPED, RunResult
from sams.services.scheduler.service import TickReport
from sams.services.scheduler.types import JobOutcome, JobSpec

NOW = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
DAY = date(2024, 10, 25)


def result(**over) -> RunResult:
    """一行执行结果，默认是「跑成了，并且有一句可说的话」。

    用例要「没有尾巴」的那几格自己写 `note=""`：屏幕上那些尾巴判的就是空串，
    默认值替它们说话会把「没话说」这一支钉不住。
    """
    fields = {
        "job": "bars",
        "market": "A",
        "period_key": "2024-10-25",
        "status": "ok",
        "rows_affected": 3,
        "note": "入库 3 根",
    }
    fields.update(over)
    return RunResult(**fields)


class FakeScheduler:
    """只回答三个问题：注册了什么、tick 收到了哪些参数、循环推了几轮。"""

    def __init__(self, *, report: TickReport | None = None, reports=(), error=None):
        # 四个而不是三个：名字列的宽度要靠一个 10 个字符的名字才钉得住（`financials`），
        # 而注册顺序的语义（数据 → 周检 → 规则 → 汇率 → 日历）由 period 那三样代表
        self.jobs = (
            JobSpec("bars", DAILY, "A", lambda context: JobOutcome()),
            JobSpec("financials", DAILY, "A", lambda context: JobOutcome()),
            JobSpec("recheck", WEEKLY, "A", lambda context: JobOutcome()),
            JobSpec("fx", FX, None, lambda context: JobOutcome()),
        )
        self.report = report if report is not None else TickReport(now=NOW)
        self.reports = tuple(reports)
        self.error = error
        self.calls: list[dict] = []

    def tick(self, *, as_of=None, only_job=None, only_market=None) -> TickReport:
        self.calls.append(
            {"as_of": as_of, "only_job": only_job, "only_market": only_market}
        )
        if self.error is not None:
            raise self.error
        return self.report

    def run_forever(self, *, on_tick=None, max_ticks=None) -> int:
        if self.error is not None:
            raise self.error
        for report in self.reports:
            on_tick(report)
        return len(self.reports)


def invoke(scheduler, argv: list[str]) -> int:
    """走一遍真的 argparse：命令名、参数、`--help` 那一套都由 `build_parser` 负责。

    容器是手搭的：装配归 `tests/test_bootstrap.py`，这里只给这两个 handler 读的那两样。
    `scheduler_tick_s` 特意取一个不是默认值的数：屏幕上那一格读的是设置，
    写死成 60 的用例分不出「读了设置」和「抄了默认值」。
    """
    args = build_parser().parse_args(argv)
    container = SimpleNamespace(
        scheduler=scheduler,
        settings=SimpleNamespace(scheduler_tick_s=45),
    )
    return args.handler(container, args)


def test_once_prints_each_result_and_counts_failures(capsys):
    """三种结论各占一行，其中失败让退出码变 1：`once` 是排障入口，
    敲它的人要知道「这一轮有没有需要人看的东西」。"""
    scheduler = FakeScheduler(
        report=TickReport(
            now=NOW,
            results=(
                result(),
                result(
                    job="actions",
                    market="HK",
                    status="failed",
                    note="",
                    error="RateLimited: 429",
                ),
                result(
                    job="fx",
                    market=None,
                    status=SKIPPED,
                    note="",
                    skip_reason="10 分钟内重试过",
                ),
            ),
            waiting=("calendar@A",),
        )
    )
    assert invoke(scheduler, ["run", "once"]) == 1
    out = capsys.readouterr().out
    lines = out.splitlines()
    # 三行都钉整行：note / skip_reason / error 三格各自在一行上有值、在另两行上是空串，
    # 「条件改成无条件」多出来的是一个 ` · ` 空尾巴，只查子串的断言拦不住
    assert "ok     bars@A  周期 2024-10-25 · 入库 3 根" in lines
    assert "failed actions@HK  周期 2024-10-25 · RateLimited: 429" in lines
    assert "skipped fx@全局  周期 2024-10-25 · 10 分钟内重试过" in lines
    assert "等待  calendar@A  还没到点" in out


def test_once_exits_zero_when_nothing_failed(capsys):
    """全成功 + 等待中也还是 0：「还没到点」不是故障，把两者合成一个码，
    cron 就会每天为一个正常的下午报警。

    三个旗标都不给时也钉：`None` 传下去才是「按各市场当地日期、全部 job」，替一个空旗标
    填上「今天」会让三个市场共用同一个日期——屏幕上那一轮说的就不是「今晚」了。
    """
    scheduler = FakeScheduler(
        report=TickReport(now=NOW, results=(result(),), waiting=("fx@全局",))
    )
    assert invoke(scheduler, ["run", "once"]) == 0
    assert "等待  fx@全局  还没到点" in capsys.readouterr().out
    assert scheduler.calls == [{"as_of": None, "only_job": None, "only_market": None}]


def test_once_normalizes_market_and_parses_as_of():
    """钉的是「进调度器之前已经归一」：`Scheduler._selected` 按 `spec.market == only_market`
    过滤，而注册表里的键是大写，所以原样传下去的 `hk` 会选出零个 job，屏幕上写着「跑了
    一轮」而什么都没做。替身只记录自己收到了什么，真实的那道过滤在 `service.py`。"""
    scheduler = FakeScheduler()
    argv = ["run", "once", "--market", "hk", "--as-of", "2024-10-25", "--job", "bars"]
    assert invoke(scheduler, argv) == 0
    assert scheduler.calls == [
        {"as_of": DAY, "only_job": "bars", "only_market": "HK"}
    ]


def test_run_once_help_names_every_job_it_accepts(capsys):
    """`symbol_master` 早就在注册表里，帮助里没有它等于告诉运维「这个 job 不存在」——
    港股主档同步正是 D210 上线后的第一步。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "once", "-h"])
    assert "symbol_master" in capsys.readouterr().out


def test_a_malformed_date_is_a_usage_error(capsys):
    """2 与 1 的分界（D77）：`--as-of 2024-13-40` 是敲错了，改参数就能重跑；
    一段栈会让运维先去查那个抛错的文件。"""
    scheduler = FakeScheduler()
    assert invoke(scheduler, ["run", "once", "--as-of", "2024-13-40"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err
    assert scheduler.calls == [], "日期都没看懂，就不该碰调度器"


def test_an_unknown_job_is_a_usage_error(capsys):
    """job 名敲错同样是 2：`_selected` 抛的是 `ValidationError`，
    而「这一轮没选出 job」与「这一轮跑成了」在屏幕上必须分得开。"""
    scheduler = FakeScheduler(error=ValidationError("没有匹配的 job（name=ghost）"))
    assert invoke(scheduler, ["run", "once", "--job", "ghost"]) == 2
    assert "没有匹配的 job" in capsys.readouterr().err


def test_once_prints_an_error_line_and_exits_1(capsys):
    """到期计算算不出来的那些 job 不进 `results`，进 `errors`：只打结果的那一屏
    会把「三个市场的日历读不到」说成「这一轮无事可做」。"""
    scheduler = FakeScheduler(
        report=TickReport(
            now=NOW,
            results=(),
            waiting=("calendar@A",),
            errors=("rules@A: 日历里没有 2024-10-25",),
        )
    )
    assert invoke(scheduler, ["run", "once"]) == 1, "只有错误、没有失败也要非零"
    out = capsys.readouterr().out
    assert "错误  rules@A: 日历里没有 2024-10-25" in out
    assert "等待  calendar@A  还没到点" in out


def test_start_reports_the_job_count_and_suppresses_skipped_only_ticks(capsys):
    """启动那一句先报「几个 job、多久一轮」：敲的人要知道自己启动的是不是刚改完的配置。
    一轮里全是「这个周期已经跑过」时整轮不出声——几十秒一轮刷屏会把真正的失败冲走。"""
    skipped = TickReport(
        now=NOW,
        results=(result(status=SKIPPED, note="", skip_reason="成功过的周期不重跑"),),
    )
    alive = TickReport(now=NOW, results=(result(),))
    scheduler = FakeScheduler(reports=(skipped, alive))
    assert invoke(scheduler, ["run", "start"]) == 0
    out = capsys.readouterr().out
    assert "4 个 job，每 45 秒一轮" in out
    assert "已停止（2 个 tick）" in out
    assert "成功过的周期不重跑" not in out
    # 整行连缩进一起钉：少掉那一格缩进，这一行就和「出声的那一轮」的头一行分不出层级
    assert "  ok     bars@A  周期 2024-10-25 · 入库 3 根" in out.splitlines()
    assert out.count("bars@A") == 1
    # 静音连头一行一起静音：只挡结果行，一轮全 skipped 时仍然留一行光秃秃的时刻，
    # 那一行读起来像「这一轮做了点什么」
    assert out.count("2024-10-25 23:00:00 UTC") == 1


def test_a_tick_with_an_error_speaks_even_when_every_job_was_skipped(capsys):
    """「全 skipped」与「全 skipped 且这一轮有算不出来的东西」是两屏：
    前一种该安静，后一种必须出声，否则静音规则会顺手吃掉真正的故障。"""
    noisy = TickReport(
        now=NOW,
        results=(result(status=SKIPPED, note="", skip_reason="成功过的周期不重跑"),),
        errors=("bars@A: 读不到账本",),
    )
    scheduler = FakeScheduler(reports=(noisy,))
    assert invoke(scheduler, ["run", "start"]) == 0
    lines = capsys.readouterr().out.splitlines()
    # 连那一格缩进一起钉：错误行是这一轮头一行的下属，顶格打就看不出它属于哪一轮
    assert "  错误  bars@A: 读不到账本" in lines
    assert "[2024-10-25 23:00:00 UTC]" in lines, "出声的那一轮要带上它是哪一轮"
    assert "成功过的周期不重跑" not in "\n".join(lines), "出声不等于把 skipped 也倒出来"


def test_start_reports_a_second_daemon_as_a_message(capsys):
    """认领心跳失败（已经有一个循环在跑）是一句话，不是一段栈；
    也不是 2——命令行没错，是那个进程已经存在（D100）。"""
    scheduler = FakeScheduler(
        error=SchedulerBusy("已经有一个调度器在运行（心跳 30 秒前）")
    )
    assert invoke(scheduler, ["run", "start"]) == 1
    captured = capsys.readouterr()
    assert "已经有一个调度器" in captured.err
    assert "SchedulerBusy" not in captured.err
    assert captured.out.startswith("调度器启动")


def test_jobs_lists_the_registration_order(capsys):
    """「为什么今晚没有港股规则」只有两个答案来源：注册顺序，以及每条各自的市场与周期。

    钉整行而不是列的位置：名字那一格少留一格时四行**仍然彼此对齐**（一起左移），
    只查「齐不齐」拦不住改宽度。而 11 与 4 这两格的出处是「注册表里最长的名字
    （`financials`）之后还留两格」，与健康页 `[调度]` 那一节同一条线——所以这里要红的是
    那两格的具体间距，市场那一格同时给两种长度（`A` 与「全局」）。
    """
    assert invoke(FakeScheduler(), ["run", "jobs"]) == 0
    out = capsys.readouterr().out
    assert "注册 4 个 job（执行顺序即下列顺序）：" in out
    rows = [ln for ln in out.splitlines() if ln.startswith("  ")]
    assert rows == [
        "  bars        A    daily",
        "  financials  A    daily",
        "  recheck     A    weekly",
        "  fx          全局   fx",
    ]
