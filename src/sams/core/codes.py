"""股票代码的域规则：一只票只有一个入库形态。

采集端、仓库端、自选清单都要归一，否则 `700` / `00700` / `HK00700` 会为同一天写出
三个主键，而自选清单里的那一行只认其中一种写法——读不到数据还不报错，是最难查的一类
「数据没进来」。归一规则只放这一处：它属于域，不属于任何一层实现。
"""

from __future__ import annotations

from sams.core.errors import ValidationError


def canonical_market(market: str) -> str:
    """市场名归一：大小写与空格不算语义。

    这不是排版问题。港美股的键存的是大写，`"hk"` 查出来是空，症状换成
    「这只票从没分过红」；而 D37 那道「不可能跳变」的闸要按市场取涨跌幅上限，
    拼写走不进那个分支时它不报错，只是安静地不再工作——区间涨跌照样出一个数。
    两处判定现在都调本函数（`daily_price_limit` 与仓库层的键归一，D46/D50），
    这里宁可拒绝一个空市场名，也不让每种小写各长出一条路。
    """
    cleaned = (market or "").strip().upper()
    if not cleaned:
        raise ValidationError("market 不能为空：A / HK / US")
    return cleaned


def canonical_code(market: str, code: str) -> str:
    market = canonical_market(market)
    cleaned = (code or "").strip().upper()
    if not cleaned:
        raise ValidationError("code 不能为空")
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


def fits_market(market: str, code: str) -> bool:
    """这个代码是不是这个市场的形态：A 6 位数字、HK 5 位数字；US 不猜（D210）。

    自选里折起的旧行一直留在采集清单里（D169），A 市场的清单里可能混进一只港股形态的
    代码；它不属于这个市场，报价与入账都该在发请求之前挡下——一只坏代码不该让同批
    的其余 49 只一起失联。
    """
    market = canonical_market(market)
    if market == "A":
        return len(code) == 6 and code.isdigit()
    if market == "HK":
        return len(code) == 5 and code.isdigit()
    return True


def currency_override(market: str, code: str) -> str | None:
    """市场级默认币种的两个真实例外；返回 None 表示用市场默认。

    `MarketCfg.currency` 只能回答「这个市场默认什么币」，而报价币种是标的属性：
    A 股 B 股以外币报价、港股人民币双柜台以 CNY 报价。把 CNY 写进 900901 或把 HKD
    写进 80700，会顺着 `symbols.currency` 一路污染到展示与换算，且再也看不出错。
    """
    market = canonical_market(market)
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
    market = canonical_market(market)
    if market == "A":
        return 100
    if market == "US":
        return 1
    return None


#: 创业板与科创板：±20%（新股前五日不设限，那是另一回事，由调用方按「跳过开头几根」处理）
_WIDE_LIMIT_PREFIXES = ("30", "68")


def exchange_folds_dividend_into_daily_change(market: str) -> bool:
    """该交易所公布的「当日涨跌幅」，是否已经把现金分红折进前收盘价。

    这不是排版问题，也不是两个数的小差异：A 股交易所在除权日给出**除权参考价**，
    当天公布的涨跌幅就是按它算的，所以 D34 的因子法在 A 股与板上那个数同一算法
    （601088 除权日 `43.38/42.84−1 = +1.2605%` 是交易所公布值）。港美股不复权现金分红：
    前收盘价照旧，除净那天板上就是跌那几点——公布的涨跌幅等于**裸价**那一行。
    同一句话在一个市场是真、在另两个市场是假，而它决定的是「这两个数该跟谁核对」：
    打印时按它分叉（D53），不要再写成一个无条件成立的声明。

    拆股/送转是另一回事：三个市场的交易所都按股数调整前收盘价，所以那条路两种口径一致。
    """
    return canonical_market(market) == "A"


def daily_price_limit(market: str, code: str) -> float | None:
    """该标的单日最大**正常**涨跌幅，判不出返回 None。

    这个数只用于一件事（D37）：裸价序列里出现超过它的跳变，就一定不是交易出来的——
    A 股没有任何合法情形能让主板一天跌 15%，除了除权。所以它是「一定有没记录的除权」
    的证据，而不是给使用者看的行情字段。

    取的是该板块**最宽**的口径：ST 是 ±5%，但按 5% 判会把 ST 股的正常跌停误报成除权，
    而按 10% 判只会漏判（漏判由 D36 的覆盖标记兜住）。港美股没有涨跌幅限制，返回 None
    等于承认「这条证据在这里不存在」。
    """
    if canonical_market(market) != "A" or not code.isdigit():
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


def looks_like_index_code(market: str, code: str) -> bool:
    """源形态的指数码（sh000001/hkHSI/usSPX）。

    000001 是平安银行也是上证指数：区分靠这层市场前缀，轮询与入库据此分叉，
    否则个股表和指数表会互相冒名。HK/US 后缀 2~6 位大写字母（HSCEI 5 位、HSTECH 6 位）。
    """
    c = code.strip()
    tail = c[2:]
    if not tail:
        return False
    if market == "A":
        return c[:2].lower() in ("sh", "sz", "bj") and tail.isdigit() and len(tail) == 6
    if market == "HK":
        return c[:2].lower() == "hk" and tail.isalpha() and tail.isupper() and len(tail) <= 6
    if market == "US":
        return c[:2].lower() == "us" and tail.isalpha() and tail.isupper() and len(tail) <= 6
    return False
