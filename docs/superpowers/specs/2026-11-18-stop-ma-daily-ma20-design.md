# `stop.ma` 支持日线 MA20，并把基准线落到表单上（R3-13 / D357）

**关联**：R3-13（第三轮专家实测：做不了「跌破日线 MA20」）、R3-14（试算输入基，另案）、
R3-25/R3-26（T7 同批 P2 清理，不影响本步）、D73（后复权折回现价口径只有一处）、
D283（网页不出现命令行）、D346（注记渠道中性）。

## 背景

`stop.ma`（跌破均线）当前只有两个基准：`weekly_ma20`（默认）与 `daily_ma60`。
提交 `daily_ma20` 会被 `_coerce` 拒掉（「basis 只能是 weekly_ma20/daily_ma60」），
而「跌破日线 MA20」是波段用户最常用的一条止损线。同时，这个模板没有任何数值参数，
表单旁的「这条线在现价哪一侧」那一格由 `_line_notes` 按 `unit == "元"` 的参数生成，
所以选了均线基准也看不到均线值、看不到距离；试算只回一句「没有到触发条件」。

## 现状与约束

1. `core/rules.py:156` 的 `stop.ma` 用 `_choice("basis", …, ("weekly_ma20", "daily_ma60"),
   "weekly_ma20")`；取值表就是这一处，仓库里存的是裸串。
2. `services/rules/templates.py` 的 `_MA_LINES` 把 basis 映射到 `Snapshot` 字段与屏幕名；
   未知取值触发 `KeyError`，引擎把它折成「这条规则用不了 'x'」的「判不了」一句。
3. `services/rules/snapshot.py` 只读一条日线均线：`_MA_COLUMN = "ma60"`，
   `daily_ma60=_fold_line(daily.table, _MA_COLUMN, valuation.price)`。日线指标组 `("ma",)`
   本来就产出 ma5/ma10/ma20/ma60/ma120/ma250，MA20 不需要多读一趟库。
4. 均线在后复权序列上算，阈值在现价口径里比：折回因子只有 `services/_fold.py` 一处
   （`fold` / `fold_to_price_basis` / `weekly_line`）。再写一个折回公式，除权日就会出现
   一侧折回、另一侧没折——D73 说的就是这件事。
5. 表单旁的相对位置说明（`_line_notes`）只换算 `unit == "元"` 的参数，MA 基准没有数字框，
   那一格一直是空的。试算 POST 走同一个 `_form_page`，所以面板在「试算」之后也还在。
6. 网页全部字符串不含 `sams `（D283）；分析层的复权拒绝文案已经是渠道中性的
   （`CoverageMissing` / `NotAdjustable` 都不带命令）。

## 方案

### 1. 基准取值只有一个出处：`MA_BASES`

`core/rules.py` 新增：

```python
#: `stop.ma` 的 basis → (屏幕上的名字, 读哪一档周期, 哪条均线)。
#: 表单的取值表、判定的取列、表单旁「这条线在哪」都读它，三处不各写一份。
MA_BASES: dict[str, tuple[str, str, int]] = {
    "weekly_ma20": ("周 MA20", "week", 20),
    "daily_ma20": ("日 MA20", "day", 20),
    "daily_ma60": ("日 MA60", "day", 60),
}
```

* `stop.ma` 的 `_choice` 改为 `tuple(MA_BASES)`，默认仍是 `weekly_ma20`
  （`choices[0]` 与默认值保持同一条，表单那格「不填」的语义不变）。
* `services/rules/templates.py` 删掉 `_MA_LINES`，`stop_ma` 改为
  `field = str(ask.params["basis"])`、`label = MA_BASES[field][0]`：字段名与 basis 同名，
  未知取值仍然抛 `KeyError`，引擎那句「判不了」原样生效。
* 库里已有的 `weekly_ma20` / `daily_ma60` 行一条都不用迁移：键与含义都没变。

### 2. 日线 MA20 进 `Snapshot`

```python
@dataclass(frozen=True)
class Snapshot:
    ...
    weekly_ma20: float | None = None
    daily_ma20: float | None = None
    daily_ma60: float | None = None
```

`read` 里：

```python
daily_ma20=line_at(daily.table, "ma20", valuation.price),
daily_ma60=line_at(daily.table, "ma60", valuation.price),
```

`line_at(table, column, price)` 放进 `services/_fold.py`：

```python
def line_at(table: pl.DataFrame, column: str, price: float | None) -> float | None:
    """一条后复权均线折到 price 这一侧：因子取自它自己那条序列的当日两个收盘。"""
    return fold_to_price_basis(last(table, column), fold(price, last(table, "close_hfq")))
```

`weekly_line` 改为 `line_at(table, "ma20", last(table, "close"))`，`snapshot._fold_line`
删除：折回公式仍然只有一处。`stop_ma` 的判定本身不改——它只多认一个字段。

### 3. 表单旁给出基准线的值与距离（试算也看得到）

`web/pages/rules.py` 的 `_price_panel` 在现价那一支里，若 `template.kind == "stop.ma"`：

* 取**生效的**基准：刚提交的 `param_basis` > 编辑页库里那份的提示 > 模板默认
  （编辑页的下拉按约定不预选，提示行已经写着「当前：daily_ma60」，这里必须读同一份，
  否则选择框旁会并排出现「当前：日 MA60」与「周 MA20 线…」两句互相打架的话）。
* 用与图表/判停同一条取数路径 `ctx.analysis.series(...)`：`weekly_ma20` 走
  `period="week"`，日线基准走 `period="day"`；`indicators=("ma",)`、
  `windows={"ma": (窗口,)}`（只算要的那一条）。起止日与个股页技术面卡同量级
  （`today - 400d ~ today`），预热由覆盖确认决定。
* 折回用 `line_at(read.table, f"ma{window}", view.price)`：`view.price` 是这一屏的现价
  （行情板或最后收盘），与其它「线在现价哪一侧」的说明用的是同一个价。
* 写进 `panel.lines["basis"]`，模板现有的 `panel.lines.get(spec.name)` 就把它印在
  基准选择框旁边，不需要改模板。

措辞（与 `_line_notes` 同形：说位置，不说结论）：

| 情形 | 那句话 |
|---|---|
| 算得出 | `日 MA20（后复权均线折回现价）12.34 元：这条线在现价下方 2.10%` |
| 差不到 0.005% | `… ：这条线就是现价` |
| 周线且最后一根没走完 | 句尾加 `（最后一根周 K 还没走完，这个读数会跟着它变）` |
| 日线不够窗口 | `日 MA20：日线不足 20 根，这条线还没有数` |
| 覆盖未确认（`CoverageMissing`） | `日 MA20：公司行为覆盖未确认，均线暂时没有数` |
| 其它 `NotAdjustable` | `日 MA20：复权序列被拒绝，均线暂时没有数` |

后三种都不给数字、不印命令、不说「已排队」（这一屏的排队那句话属于现价那一格）。

## 接口与不变式

1. basis 的取值、屏幕名、读哪档周期、均线窗口只有一个出处 `core.rules.MA_BASES`。
2. 折回公式只有一个：`services/_fold.line_at`；判停、估值、表单旁说明都走它。
3. 旧规则的存储形态不变；未知 basis 仍落「判不了」而不会静默当成默认那条线。
4. 表单旁的说明读的是 `AnalysisService.series` 那条过闸序列，不拿裸价算价格类字段；
   它说的是位置，不是「会不会触发」（那是试算的结论）。
5. 试算仍是纯只读：这条说明是渲染出来的，GET 与 POST 都不写库。
6. 网页字符串不含 `sams `。

## 测试（先红后绿）

`tests/core/test_rules_contract.py`：

* `validate_params("stop.ma", {"basis": "daily_ma20"})` 通过；默认仍是 `weekly_ma20`；
  `{"basis": "hourly"}` 的报错名单里包含 `daily_ma20`。

`tests/services/test_rules_templates_stops.py`：

* `basis=daily_ma20` 在现价跌破时命中，`why` 里出现 `日 MA20`；
* `daily_ma20=None` 时 `why_not` 点名「日 MA20」，不是周线。

`tests/services/test_rules_snapshot.py`：

* 与现有 `daily_ma60` 用例同一份已知日线：`snapshot.daily_ma20` 等于手算 MA20 折回值。

`tests/web/test_rules_form_price.py`：

* 60 根已知日线 + 全历史覆盖 + 现价：`/rules/preview` 交 `basis=daily_ma20`，
  页面出现算出来的 MA20 值与「这条线在现价下方/上方 x%」；全文无 `sams `。
* 没有覆盖确认：同一屏出现「公司行为覆盖未确认，均线暂时没有数」，且没有编出来的数字。
* 新建页默认基准显示周 MA20；编辑页存着 `daily_ma60` 时显示日 MA60（读库里那份）。

`tests/web/test_rules_web.py`：

* 新建页的 `basis` 下拉含 `daily_ma20`；提交 `daily_ma20` 后库里存的是它。

## 验证

* 串行：目标四组（core 契约 / rules templates / rules snapshot / rules_web + form_price）、
  ruff、全量 pytest（按目录分段，理由见 D356 回归记录里的 polars spill 死锁）。
* 生产库副本（`tmp/d357` 复制 `data/sams.duckdb` + `.wal`，`sams serve --port 8766`）：
  `/rules/new?kind=stop.ma&target_type=symbol&target_ref=A/600519` 的基准框旁有周 MA20
  的值与距离；POST `/rules/preview` 交 `param_basis=daily_ma20`，数值与
  `/api/symbol/A/600519/bars` 的 `main.ma.ma20` 末值按同一因子折回后一致；整页无 `sams `。
  `/rules/new?kind=stop.ma&target_ref=HK/00700` 200，读不出的那几支给中性说明。
* 生产重启后 `/rules` 200、`/rules/new?kind=stop.ma` 200、`/healthz` 200。

## 范围外

* R3-14：试算如实标注输入基（最新收盘 vs 盘中报价）与「保存/启用分离」，另案。
* R3-25 数字格式化、R3-26 乱码存量：T7 的其余两项，另案。
* 自定义均线窗口（MA5/MA30）进 `stop.ma`：本步只补 R3-13 点名的日线 MA20。
* 表单旁说明的前端联动（改下拉即时刷新）：本步是服务端渲染，点试算后刷新。

## 回归结果

（实现与验证完成后填写。）
