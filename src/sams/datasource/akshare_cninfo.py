"""A 股公司行为的第三个源：巨潮「个股-历史分红」→ 标准列。纯解析，网络调用留在适配器里。

为什么再加一个源（#383 / D318）：同花顺对北交所标的**不写 A 股除权日**（它自己的解析器就是
`if "A股股权登记日" in columns ... else ...`，北交所走 else 分支），于是 920289 那次
10 转 12.5397 在两个源里都读不出日期，覆盖确认也写不下去。交易所自己的披露平台写了那一天
（实测 2024-07-17），所以这条链上多一个源，不是多一个备份。

三条口径：

1. **单位是「每 10 股」**，与东财同源同口径：`派息比例 280.2423` 对应「10派280.2423元(含税)」
   （600519 实测）⇒ 每股 28.02423。数值列与说明文本互查，对不上就整批失败（D34）。
2. **一行里送、转、派是三件事**，拆成三行——`corp_actions` 的主键含 `kind`（D34）。
3. **除权日是序列上的锚**：这张表同时给 股权登记日 / 除权日 / 派息日 / 股份到账日，
   只有除权日能乘因子。取错一天，那天的涨跌就少乘一次而曲线看着仍然连续。

**它不能替掉同花顺**：实测 002450、600636（同花顺当初被引进来的那两只，东财空答复、
同花顺有 8~28 条）在巨潮同样是无记录。三个源是三家上游，谁都不是全集（D36）。

配股不在这个接口里（与东财、同花顺一样，见 spec §18 U16），`price` 列恒空。

D324 起它不止公司行为：公司概况（含上市日期）与公告也走巨潮自己的端点——
`stock_profile_cninfo`（`p_sysapi1133`）与信息披露查询（`hisAnnouncement/query`）。
两条都是**与东财不同的域名**（D322 的第一档），而不是给东财接口换一层 akshare 包装。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime

import polars as pl

from sams.core.codes import canonical_code
from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.actions import empty_actions, normalize_actions
from sams.datasource.base import classify_error
from sams.datasource.f10 import empty_announcements, empty_profile
from sams.datasource.sources import source

logger = logging.getLogger("sams.datasource")

SOURCE = "akshare_cninfo"

_ANNOUNCED = "实施方案公告日期"
_EX_DATE = "除权日"
_CASH = "派息比例"
_BONUS = "送股比例"
_TRANSFER = "转增比例"
_SCHEME = "实施方案分红说明"
_REPORT = "报告时间"
#: 判定「这张表还是不是巨潮的历史分红表」的列（D316 的三态里，只有整列消失才算改版）。
#: 三个比例列都算必需：整列没了以后 `派息比例` 里装着什么都有可能，而这一族的列是
#: akshare 按 `F0xxN` 重命名出来的，上游换字段名时会静默缺列。
_REQUIRED = (_ANNOUNCED, _EX_DATE, _CASH, _BONUS, _TRANSFER, _SCHEME)

#: 源口径「每 10 股」→ 标准列口径「每股」，与东财同一个常量（D34）
_PER_SHARE = 10.0
#: 文本与数值的自洽比对容差：源把金额格式化到分，四舍五入会造成半分以内的偏差
_TOLERANCE = 0.005
_CASH_IN_DESC = re.compile(r"派\s*([0-9]+(?:\.[0-9]+)?)\s*元")
_BONUS_IN_DESC = re.compile(r"送\s*([0-9]+(?:\.[0-9]+)?)")
_TRANSFER_IN_DESC = re.compile(r"转(?:增)?\s*([0-9]+(?:\.[0-9]+)?)")
#: 「不分配不转增」是一句完整答复，不是一个折不出数的事件
_NOTHING = re.compile(r"不(分配|派发|转增|送转)")

_SCHEMA = {
    "ex_date": pl.Date,
    "kind": pl.Utf8,
    "cash_per_share": pl.Float64,
    "ratio": pl.Float64,
    "price": pl.Float64,
    "note": pl.Utf8,
}


def required_columns(frame: pl.DataFrame) -> None:
    """这张表还是不是巨潮的历史分红表？不是才算改版（D316）。"""
    absent = [c for c in _REQUIRED if c not in frame.columns]
    if absent:
        raise FieldDrift(SOURCE, f"历史分红响应缺少必需列 {absent}，实际列 {list(frame.columns)}")


def _number(value: object) -> float | None:
    """NaN/None/空串都算「源没给这个数」，不是 0。"""
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN != NaN


def _text(value: object) -> str | None:
    """空串与全空白按「源没给」处理；非空值原样交回（简介里的空格有语义）。"""
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _per_share(per_ten: float) -> float:
    """每 10 股 → 每股，取 10 位小数（除以 10 在二进制下不闭合）。"""
    return round(per_ten / _PER_SHARE, 10)


def _desc_amount(text: str, pattern: re.Pattern[str]) -> float | None:
    match = pattern.search(text)
    return None if match is None else float(match.group(1))


def _to_date(value: object) -> date | None:
    """源给的日期实测有四种形态：`date`、`datetime`、`'2024-07-17'`、pandas 的 `NaT`。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10] if value is not None else ""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def expand_cninfo(raw: pl.DataFrame) -> pl.DataFrame:
    """巨潮历史分红明细 → 标准公司行为列（一事件一行，单位已换成每股）。

    输出还没有 market/code/src，那是 `normalize_actions` 的事：这里只做「一行方案 →
    若干条事实」的展开与单位换算，两层各自可测。
    """
    if raw.is_empty():
        return pl.DataFrame(schema=_SCHEMA)
    required_columns(raw)

    rows: list[dict] = []
    #: 一句我们读不出的答复（有事实、定不了位）：不是「没有」（D316）
    undated: list[str] = []
    nothing = 0
    for row in raw.iter_rows(named=True):
        scheme = (row.get(_SCHEME) or "").strip()
        cash = _number(row.get(_CASH))
        bonus = _number(row.get(_BONUS))
        transfer = _number(row.get(_TRANSFER))
        if not (cash or bonus or transfer) and _NOTHING.search(scheme):
            # 「不分配不转增」是一句完整答复，不是一个折不出数的事件，也不该拖住整只票
            nothing += 1
            continue
        ex_date = _to_date(row.get(_EX_DATE))
        if ex_date is None:
            undated.append(f"{row.get(_ANNOUNCED)} {scheme}")
            continue
        note = "；".join(
            part for part in (f"{_REPORT} {row[_REPORT]}" if row.get(_REPORT) else None, scheme)
            if part
        )
        if not (cash or bonus or transfer):
            # 有除权日、三个数值全空：这一天确实动了东西，只是折不出数 ⇒ 记 unknown（D39）。
            # 丢掉它等于替这只票签一句「那些日子没发生过除权」，读取层见到 unknown 才拒绝出数
            rows.append(
                {
                    "ex_date": ex_date,
                    "kind": "unknown",
                    "cash_per_share": None,
                    "ratio": None,
                    "price": None,
                    "note": note or scheme,
                }
            )
            continue
        if cash:
            if "每股" in scheme:
                raise FieldDrift(
                    SOURCE,
                    f"除权日 {ex_date} 的说明写的是每股口径（{scheme!r}），与列名给的「每10股」"
                    "矛盾：单位换算的反查依据本身变了，继续算会把因子差十倍",
                )
            described = _desc_amount(scheme, _CASH_IN_DESC)
            if described is None:
                raise FieldDrift(
                    SOURCE,
                    f"除权日 {ex_date} 的派息 {_CASH}={cash} 无法与说明核对（{scheme!r}）："
                    "每10股→每股的换算失去交叉验证，不敢乘进因子",
                )
            if abs(described - cash) > _TOLERANCE:
                raise FieldDrift(
                    SOURCE,
                    f"除权日 {ex_date} 的派息数值 {cash} 与说明里的 {described} 不一致，"
                    "按源侧口径漂移处理",
                )
            rows.append(
                {
                    "ex_date": ex_date,
                    "kind": "dividend",
                    "cash_per_share": _per_share(cash),
                    "ratio": None,
                    "price": None,
                    "note": note,
                }
            )
        for value, kind, pattern in (
            (bonus, "bonus", _BONUS_IN_DESC),
            (transfer, "transfer", _TRANSFER_IN_DESC),
        ):
            if not value:
                continue
            described = _desc_amount(scheme, pattern)
            if described is not None and abs(described - value) > _TOLERANCE:
                raise FieldDrift(
                    SOURCE,
                    f"除权日 {ex_date} 的{kind}数值 {value} 与说明里的 {described} 不一致，"
                    "按源侧口径漂移处理",
                )
            rows.append(
                {
                    "ex_date": ex_date,
                    "kind": kind,
                    "cash_per_share": None,
                    "ratio": 1.0 + _per_share(value),
                    "price": None,
                    "note": note,
                }
            )
    if nothing:
        logger.info("巨潮有 %d 行写明不分配不转增，按完整答复跳过", nothing)
    if undated:
        logger.warning(
            "巨潮有 %d 行历史分红没有除权日，无法定位到序列上的某一天：%s",
            len(undated),
            "；".join(undated[:3]),
        )
    if undated and not rows:
        # 一行都定不了位：这既不是「没分过红」（表里明明有记录），也不能算源故障（D279）
        raise EmptyResponse(
            SOURCE,
            f"{raw.height} 行历史分红里有 {len(undated)} 行读不出除权日："
            f"{'；'.join(undated[:3])}",
        )
    out = pl.DataFrame(rows, schema=_SCHEMA)
    # 同一方案在两个公告阶段各出现一次时只能算一次：D41 的相加针对的是不同的事实
    out = out.unique(subset=["ex_date", "kind", "cash_per_share", "ratio"], keep="first")
    return out.sort(["ex_date", "kind"])


def cninfo_says_no_records(symbol: str, *, post: Callable[[str], dict] | None = None) -> bool:
    """直接问一次巨潮：这只票的历史分红表是不是「查无记录」（只在崩了之后走这一趟）。

    akshare 的 `stock_dividend_cninfo` 在 `records: []` 时对空帧做重命名，随后在
    `temp_df["实施方案公告日期"]` 上抛 `KeyError: 实施方案公告日期`——**与东财那个 `TypeError`
    是同一个坑**（#360 记过一次）。实测 920162 / 001246 / 301666 三只是
    HTTP 200、`resultcode: 200`、`resultmsg: success`、`records: []`。
    多花一个请求换这个区分，只在失败路径上付。

    问不出结论（连不上、非 200、JSON 不是那个形状）一律返回 False：分不清就照故障处理，
    静默当「没有记录」会把一只分过红的票抹平。
    """
    probe = post or _cninfo_json
    try:
        body = probe(symbol)
    except Exception:  # noqa: BLE001 — 问不出结论就退回「这不是查无记录」，让原异常说话
        return False
    return body.get("records") == [] and str(body.get("resultcode")) == "200"


def _cninfo_json(symbol: str) -> dict:
    """原始那一页的 JSON 信封（`records` / `resultcode`），绕开 akshare 的解析。

    请求头里的 `Accept-Enckey` 是 akshare 自带的 `cninfo.js` 算出来的，这里照抄它的做法：
    我们只要读出「空表」这一种答复，不重造它的列名映射。
    """
    import py_mini_racer
    import requests
    from akshare.datasets import get_ths_js

    with open(get_ths_js("cninfo.js"), encoding="utf-8") as script:
        js = py_mini_racer.MiniRacer()
        js.eval(script.read())
    response = requests.post(
        "https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1139",
        params={"scode": symbol},
        headers={
            "Accept": "*/*",
            "Accept-Enckey": js.call("getResCode1"),
            "Host": "webapi.cninfo.com.cn",
            "Origin": "http://webapi.cninfo.com.cn",
            "Referer": "http://webapi.cninfo.com.cn/",
            "User-Agent": "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=(5, 15),
    )
    return response.json()


def akshare_cninfo_plans(
    symbol: str, *, is_no_records: Callable[[str], bool] = cninfo_says_no_records
) -> pl.DataFrame:
    """真实网络调用：某只 A 股的历史分红**全历史**（巨潮 `p_sysapi1139`）。

    接口不带日期参数，一次给全史 ⇒ 适配器标 `corp_actions_full_history`（D36/D40）。
    """
    import akshare as ak

    try:
        frame = ak.stock_dividend_cninfo(symbol=symbol)
    except KeyError as exc:
        if not is_no_records(symbol):
            raise
        # 「这张表里没有这只票的记录」：先当一句答复交回空帧，让降级链继续问下一个源，
        # 写不写覆盖由服务层按「有没有源真的答上来」决定（D36/D43）
        logger.info("巨潮历史分红对 %s 回「无记录」，换下一个源（%s）", symbol, exc)
        return pl.DataFrame()
    if frame is None or len(frame) == 0:
        return pl.DataFrame()
    return pl.from_pandas(frame)


#: 公司概况的必需列：三个各自承担一件事（代码、名字、上市日），整列消失才算改版。
#: 期数不等于必需：源对「查无此票」回的是 0 行而不是缺列（实测 `stock_profile_cninfo`）。
_PROFILE_REQUIRED = ("A股代码", "A股简称", "上市日期")

_PROFILE_TEXT = {
    "name": "A股简称",
    "full_name": "公司名称",
    "name_en": "英文名称",
    "industry": "所属行业",
    "exchange": "所属市场",
    "chairman": "法人代表",
    "legal_person": "法人代表",
    "phone": "联系电话",
    "email": "电子邮箱",
    "website": "官方网站",
    "address": "办公地址",
    "reg_address": "注册地址",
}


def akshare_cninfo_profile(symbol: str) -> pl.DataFrame:
    """真实网络调用：巨潮个股概况（`p_sysapi1133`），一票一行，含上市日期。"""
    import akshare as ak

    frame = ak.stock_profile_cninfo(symbol=symbol)
    if frame is None or len(frame) == 0:
        return pl.DataFrame()
    return pl.from_pandas(frame)


def parse_cninfo_profile(
    raw: pl.DataFrame, *, market: str, code: str, source: str
) -> pl.DataFrame:
    """巨潮概况 → `PROFILE_COLUMNS`。0 行是「这个源没有这只票的概况」，不是改版。

    `所属市场` 在巨潮是「上交所 / 深交所主板 / 北交所」这种混合口径，所以放进 `exchange`，
    `board` 留空：把「深交所主板」拆成交易所+板块需要一张我们没测过的对照表，猜错的那天
    屏幕上会出现一个看着正常的错板块。
    """
    if raw.is_empty():
        return empty_profile()
    absent = [column for column in _PROFILE_REQUIRED if column not in raw.columns]
    if absent:
        raise FieldDrift(SOURCE, f"公司概况响应缺少必需列 {absent}，实际列 {list(raw.columns)}")
    row = raw.row(0, named=True)
    values = {name: _text(row.get(column)) for name, column in _PROFILE_TEXT.items()}
    values.update(
        {
            "market": market,
            "code": canonical_code(market, code),
            "industry_csrc": None,
            "board": None,
            "listed_on": _to_date(row.get("上市日期")),
            "founded_on": _to_date(row.get("成立日期")),
            "isin": None,
            "lot_size": None,
            "president": None,
            "secretary": None,
            "employees": None,
            "auditor": None,
            "fiscal_year_end": None,
            "hk_connect": None,
            "stock_type": None,
            "summary": _text(row.get("机构简介")) or _text(row.get("主营业务")),
            "source": source,
        }
    )
    return pl.DataFrame([values], schema=empty_profile().schema)


#: 公告的必需列：代码用来挡串包，标题与链接是落库的两样，日期定窗口。
_ANN_REQUIRED = ("代码", "公告标题", "公告时间", "公告链接")
_ANN_ID = re.compile(r"announcementId=(\d+)")


def akshare_cninfo_announcements(symbol: str, start: date, end: date) -> pl.DataFrame:
    """真实网络调用：巨潮信息披露查询（`hisAnnouncement/query`，`market="沪深京"`）。

    实测 920289 的 30 天窗口：34 行 / 4.8 秒（两页）；空窗口是**列齐全的 0 行帧**，
    与「源答了没有公告」同形。这条端点是 `szse` 列，但覆盖沪深京三所（`szse_stock.json`
    里北交所也在），所以不能拿交易所号段猜 `column`。
    """
    import akshare as ak

    frame = ak.stock_zh_a_disclosure_report_cninfo(
        symbol=symbol,
        market="沪深京",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    if frame is None:
        return pl.DataFrame({column: [] for column in _ANN_REQUIRED})
    return pl.from_pandas(frame)


def parse_cninfo_announcements(
    raw: pl.DataFrame,
    *,
    market: str,
    code: str,
    source: str,
    start: date,
    end: date,
) -> pl.DataFrame:
    """巨潮公告 → `ANNOUNCEMENT_COLUMNS`；窗口内 0 条是 0 行答复。

    `art_code` 从详情链接里的 `announcementId` 取：巨潮那一页没有单独的编号列，
    而链接自带编号是它的公开契约。取不出来的行不猜（跳过并记日志），
    否则一堆空编号会在主键上互相覆盖。
    """
    absent = [column for column in _ANN_REQUIRED if column not in raw.columns]
    if absent:
        raise FieldDrift(SOURCE, f"公告响应缺少必需列 {absent}，实际列 {list(raw.columns)}")
    canonical = canonical_code(market, code)
    rows: list[dict] = []
    skipped = 0
    for item in raw.iter_rows(named=True):
        echoed = _text(item.get("代码"))
        if echoed and canonical_code(market, echoed) != canonical:
            raise FieldDrift(
                SOURCE, f"公告响应代码 {echoed} 与请求代码 {code} 不一致（串包或缓存错位）"
            )
        link = _text(item.get("公告链接"))
        title = _text(item.get("公告标题"))
        match = _ANN_ID.search(link) if link else None
        published = _to_date(item.get("公告时间"))
        if match is None or not title or published is None:
            skipped += 1
            continue
        if not start <= published <= end:
            continue
        rows.append(
            {
                "market": market,
                "code": canonical,
                "art_code": match.group(1),
                "published_on": published,
                "title": title,
                "category": None,
                "url": link,
                "source": source,
            }
        )
    if skipped:
        logger.warning("巨潮公告有 %d 行读不出编号、标题或日期，按读不出跳过", skipped)
    if not rows:
        return empty_announcements()
    return (
        pl.DataFrame(rows, schema=empty_announcements().schema)
        .unique(subset=["art_code"], keep="last")
        .sort("published_on", descending=True)
    )


class AkshareCninfoAdapter:
    name = SOURCE
    markets = frozenset({"A"})
    #: 历史分红是全史；概况/公告/上市日期是另外三个端点（D324），没有日线
    corp_actions_full_history = True

    def __init__(
        self,
        plans_fetch: Callable[[str], pl.DataFrame] = akshare_cninfo_plans,
        profile_fetch: Callable[[str], pl.DataFrame] = akshare_cninfo_profile,
        announcements_fetch: Callable[[str, date, date], pl.DataFrame] = (
            akshare_cninfo_announcements
        ),
    ) -> None:
        self._plans_fetch = plans_fetch
        self._profile_fetch = profile_fetch
        self._announcements_fetch = announcements_fetch

    def corp_actions(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """全史交回去，**不按窗口切**（D40）：切掉的行是源已经给到的事实，而服务层照样会写
        「全历史，确认至今天」——库与覆盖标记说反话时守卫就放行裸价。
        """
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        symbol = canonical_code(market, code)
        try:
            raw = _as_polars(self._plans_fetch(symbol))
            if raw.is_empty():
                return empty_actions()
            expanded = expand_cninfo(raw)
            if expanded.is_empty():
                return empty_actions()
            actions = normalize_actions(expanded, market=market, code=code, source=self.name)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001 — 边界处统一归类，见 classify_error
            raise classify_error(self.name, exc) from exc
        return actions

    def company_profile(
        self, market: str, code: str, start: date, end: date
    ) -> pl.DataFrame:
        """公司概况一票一行（D324）。窗口对它没有意义，只为与降级链同形。"""
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        try:
            raw = _as_polars(self._profile_fetch(canonical_code(market, code)))
            return parse_cninfo_profile(raw, market=market, code=code, source=self.name)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def announcements(
        self, market: str, code: str, start: date, end: date
    ) -> pl.DataFrame:
        """窗口内公告（D324）。0 条是 0 行答复；编号取不出的行跳过而不是猜。"""
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        try:
            raw = _as_polars(self._announcements_fetch(canonical_code(market, code), start, end))
            return parse_cninfo_announcements(
                raw, market=market, code=code, source=self.name, start=start, end=end
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def listed_date(self, market: str, code: str) -> date | None:
        """上市首日：同一份概况里那一格（D324）。

        `None` 是「巨潮没有这只票的概况」，一句答复；抛出去才是「答不上来」。
        缺 `上市日期` 这一列也返回 None——源把这一列拿掉时，我们确实不知道哪天，
        替它写一个日子或写「没上市」都是假话。
        """
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        try:
            raw = _as_polars(self._profile_fetch(canonical_code(market, code)))
            if raw.is_empty() or "上市日期" not in raw.columns:
                return None
            return _to_date(raw["上市日期"][0])
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


def _as_polars(frame: object) -> pl.DataFrame:
    return frame if isinstance(frame, pl.DataFrame) else pl.DataFrame(frame)  # type: ignore[arg-type]


source(
    "akshare_cninfo",
    note="巨潮 A 股：历史分红、公司概况、公告、上市日期（无日线）",
)(AkshareCninfoAdapter)
