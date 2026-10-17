"""股票代码的域规则：一只票只有一个入库形态。

采集端、仓库端、自选清单都要归一，否则 `700` / `00700` / `HK00700` 会为同一天写出
三个主键，而自选清单里的那一行只认其中一种写法——读不到数据还不报错，是最难查的一类
「数据没进来」。归一规则只放这一处：它属于域，不属于任何一层实现。
"""

from __future__ import annotations


def canonical_code(market: str, code: str) -> str:
    cleaned = (code or "").strip().upper()
    if not cleaned:
        raise ValueError("code 不能为空")
    if market == "HK":
        # HK00700 / HK.00700 / 00700.HK / 700 是同一只票，落库必须只有 00700 一种写法
        digits = cleaned.removeprefix("HK").removesuffix(".HK").removeprefix(".").strip()
        return f"{int(digits):05d}" if digits.isdigit() else cleaned
    # A 股：交易所前后缀是数据源要的形态，不是本系统的键。
    # `sh600519` 不剥掉的话会一路传到只认 6 位数字的东财接口——那里的症状不是报错，
    # 是「这只票从没分过红」（空响应），比多一个主键更难查。
    cleaned = (
        cleaned.removesuffix(".SH")
        .removesuffix(".SS")
        .removesuffix(".SZ")
        .removesuffix(".BJ")
    )
    for prefix in ("SH", "SZ", "BJ"):
        if cleaned.startswith(prefix) and cleaned[len(prefix) :].isdigit():
            return cleaned[len(prefix) :]
    return cleaned


def currency_override(market: str, code: str) -> str | None:
    """市场级默认币种的两个真实例外；返回 None 表示用市场默认。

    `MarketCfg.currency` 只能回答「这个市场默认什么币」，而报价币种是标的属性：
    A 股 B 股以外币报价、港股人民币双柜台以 CNY 报价。把 CNY 写进 900901 或把 HKD
    写进 80700，会顺着 `symbols.currency` 一路污染到展示与换算，且再也看不出错。
    """
    if market == "A":
        if code.startswith("900"):
            return "USD"  # 沪市 B 股
        if code.startswith("200"):
            return "HKD"  # 深市 B 股
    if market == "HK" and code.isdigit() and 80000 <= int(code) <= 89999:
        # 人民币双柜台占用整个 8xxxx 段（80700 腾讯、83690 小米、89988 阿里……），
        # 用 startswith("80") 会漏掉 8 万以后的那些：同一公司的两条曲线被当成两种币，
        # 永久显示一个不存在的「折价」。08xxx 是港股通外的创业板，仍是 HKD。
        return "CNY"
    return None


def lot_size_of(market: str) -> int | None:
    """每手股数：A 股与美股是规定，港股逐票由交易所定，这里给不出就是给不出。

    存 1 冒充「知道」比留 NULL 危险——M2 的下单手数换算会算错一个数量级。
    """
    if market == "A":
        return 100
    if market == "US":
        return 1
    return None


#: 创业板与科创板：±20%（新股前五日不设限，那是另一回事，由调用方按「跳过开头几根」处理）
_WIDE_LIMIT_PREFIXES = ("30", "68")


def daily_price_limit(market: str, code: str) -> float | None:
    """该标的单日最大**正常**涨跌幅，判不出返回 None。

    这个数只用于一件事（D37）：裸价序列里出现超过它的跳变，就一定不是交易出来的——
    A 股没有任何合法情形能让主板一天跌 15%，除了除权。所以它是「一定有没记录的除权」
    的证据，而不是给使用者看的行情字段。

    取的是该板块**最宽**的口径：ST 是 ±5%，但按 5% 判会把 ST 股的正常跌停误报成除权，
    而按 10% 判只会漏判（漏判由 D36 的覆盖标记兜住）。港美股没有涨跌幅限制，返回 None
    等于承认「这条证据在这里不存在」。
    """
    if market != "A" or not code.isdigit():
        return None
    if code.startswith(_WIDE_LIMIT_PREFIXES):
        return 0.20
    # 北交所：43 / 83 / 87 / 88 / 92 开头，±30%
    if code.startswith(("43", "83", "87", "88", "92")):
        return 0.30
    if code.startswith(("60", "00")):
        return 0.10
    # B 股（900/200）与其它老代码：不猜——判错一次的代价是把正常行情说成除权
    return None
