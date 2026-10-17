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
    # A 股/美股的交易所后缀是数据源要的形态，不是本系统的键
    return cleaned.removesuffix(".SH").removesuffix(".SS").removesuffix(".SZ")


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
