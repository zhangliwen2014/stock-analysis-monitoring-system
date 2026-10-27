# SAMS · 多市场股票分析、监控与模拟交易系统

覆盖 A 股 / 港股 / 美股的分析、监控与**模拟**交易链路：

```
数据采集 → 指标与估值 → 投资观点(Thesis) → 派生监控规则 → 多渠道告警
                                                  ↓
                                        批注留痕 + 复盘闭环
```

不接券商实盘；真实持仓以手动录入 / CSV 导入形成镜像账本。

**当前进度：M1 地基完成并自测通过**（配置、事件总线、分市场交易日历、DuckDB 存储与仓库层、A 股主备源与港美股日线采集、CLI 运维入口）；
**M2-A 除权链路完成**（公司行为采集与覆盖确认、D34 因子口径、区间收益跨除权日的拒绝出数规则）。
M2 剩余（指标、估值、Thesis）与 M3 规则告警尚未开始。设计文档见
[`docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md`](docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md)。

## 安装

需要 Python 3.13 与 [uv](https://docs.astral.sh/uv/)。所有命令都走 `uv run`，不要用系统 Python。

```bash
uv sync                                    # 安装依赖（含 dev 组）
cp config/settings.example.toml config/settings.toml
export SAMS_SECRET_KEY="$(uv run python -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

`config/settings.toml` 与 `data/` 已在 `.gitignore` 中；`secret_key` 只从环境变量读，不写进配置文件。
配置里的 `sources` 数组就是降级顺序，改顺序即改路由优先级。

## 上手

```bash
uv run sams db migrate                     # 建表（幂等，重跑不重复应用）
uv run sams user add --username admin --admin   # 创建第一个管理员（口令交互式询问）
uv run sams login --username admin              # 校验口令并签发会话 token

uv run sams sync daily --market A  --code 600519 --start 2024-01-01 --end 2024-02-01
uv run sams sync daily --market US --code AAPL   --start 2024-01-01 --end 2024-02-01
uv run sams sync daily --market HK --code 00700  --start 2024-01-01 --end 2024-02-01

# 除权除息/送转配：区间收益要靠它才不被跨除权日的裸价涨跌骗到
uv run sams sync actions --market A  --code 601088
uv run sams sync actions --market HK --code 00700    # 港股的公司行为来自东财，不是 yfinance

uv run sams calendar refresh --market HK --year 2026
uv run sams calendar check   --market US --date 2026-03-09   # 是否交易日 + 时段（本地 / UTC）
uv run sams health                                             # 各表行数、日历覆盖、各源最近错误

# 区间涨跌：裸价与复权并列，过不了守卫就拒绝出数。退出码 0 是请求的那段、1 是拒绝、
# 3 是数给了但锚点落在请求之外且缺口里有开市日（或日历判不了的日子）——脚本据此决定敢不敢信
uv run sams analysis return --market A --code 601088 --start 2024-07-05 --end 2024-07-08
uv run sams analysis return --market A --code 601999 --start 2024-07-05 --end 2024-07-08 \
  --listed-on 2024-06-28 --json   # 新股首段的无限制波动要显式豁免（打印会声明这个数少了哪条形证据）；
                                   # --json 是机器出口，里面的 exit 与进程退出码同值
```

除 `db` 外的命令都会顺手补齐迁移，新装环境直接 `user add` 不会报缺表。
`sync` 可重复执行：主键 + `INSERT OR REPLACE` 保证同一区间重跑后行数不变。
两个子命令走两条独立的能力链（D38）：`daily` 只走有日线的源，`actions` 只走能给除权记录的源，
链上被跳过的源会打印出来——「这只票没分过红」与「这条链没人能给」是两件事。
`sams health` 的备源判据也按能力数：只有一个「仅公司行为」源的市场，日线仍然算单源。

复权口径（D34–D57）：库里只存**裸价**，`_hfq` 在读取层现算，因子来自 `corp_actions`。
出数只有一个入口——`analysis.adjust.adjusted_series()`：过闸之后才把 `open/high/low/close`
各乘上因子，所以下游（区间收益、指标、估值）拿到的是同一条判定过的序列。
会拒绝出数的情形都在 `analysis/guard.py` 里说清原因——
覆盖没确认过（不知道是没分红还是没查）、K 线上出现没有对应记录的除权日、
A 股裸价跳变超过涨跌停上限且对不上这天的记录（多半是漏记一次除权）、
遇到 `kind=unknown`（实物分派、权证这类折不出每股现金或股数乘数的行为）、
序列里有缺失或非有限的收盘价。
同日同类的多行公司行为按 D41 相加合成一行（主键放不下第二行，丢一行等于第二笔从没发生）。
两个源对同一天同一类型给出**对不上**的数时，主键谁也不许盖住谁：整批不写、覆盖确认也不写，
`sync actions` 直接失败（同日两件事真要支持得换主键，见 spec §18 U21）。
`sams analysis return` 打出的那行「口径」是承诺不是装饰，它**必须由产出那个数的算式决定**（D48），
还得**与那个市场的规则一致**（D53）：D34 的因子法把每股现金按**除权参考价**折成股数，也就是按参考价
再投资——只有 A 股的交易所会替现金分红复权前收盘价，所以「这与交易所公布的当日涨跌幅同一算法」
这句只在 A 股打印（除权日当天复权 = 板上那个数）；港美股板上那个数就是裸价那一行，屏幕不能说它是错数。
「现金到手不再投资」是另一个数（差半分位），这里不给（U22）。两个数在两位小数下长得一样时会自动多打两位；
覆盖确认的截止日期、逐条除权明细（前收 → 参考价、每股现金、因子）与含税/送转那两条丑话也一并打印（D54）。
要机器读就加 `--json`：payload 里带一个与退出码同值的 `exit` 字段——筛选循环处处是管道，`$?` 在那里必然丢失（D57）。
全历史源不受请求窗口约束（D40）：`sync actions --start/--end` 只决定「从哪问起」，不决定「存几行」。
拒绝是可恢复的：补上源、补上记录，数字就出来；错数看不出来。

## 启动服务（M4-A）

```bash
uv run sams db migrate
uv run sams user add --username alice --admin   # 首个管理员，按提示设密码（至少 6 位）
uv run sams serve                               # http://127.0.0.1:8765
```

- 网页上没有自助注册：第二个用户起由 admin 在 `/admin/users` 创建。
- 密钥用环境变量提供：`export SAMS_SECRET_KEY=...`（它同时是通道凭据的加密材料，
  换掉它旧通道就解不开了——见 `config/settings.example.toml` 头部的说明）。
- `serve` 单进程带上三件事：盘中实时监控、每日调度、Web 界面；`/healthz` 看进程状态，
  `/admin/health`（admin）看心跳、数据源与手动清理。
- 监听局域网：`[server] host = "0.0.0.0"` + `allow_lan = true`，要加密就配
  `tls_certfile/tls_keyfile`。

## 备份与附件（M4-B）

数据仍然是一个 DuckDB 文件，但批注的附件不在库里：它们落在 `attachments_root`——
`config.toml` 里 `[annotations] attachments_dir` 给的路径，没给就是库文件同级的
`attachments/` 子目录。**备份 = 把库文件与那个目录一起打包**：批注正文在库里、图片在
库外，只备库等于删图——行还在、缩略图全指到空文件上（面板会把这种行标成「文件缺失」，
但备份里没有第二次机会）。

| 键（`[annotations]`） | 默认 | 意思 |
|---|---|---|
| `attachments_dir` | 库文件旁的 `attachments/` | 附件根目录 |
| `max_attachments` | 5 | 每条批注最多几个附件 |
| `max_attachment_mb` | 5 | 单个附件上限（MB） |
| `max_user_total_mb` | 512 | 每用户附件总量上限（MB） |

`sams db prune` 顺带清理孤儿文件与软删过期的附件。恢复时把库与附件目录恢复到
**同一时刻**（先停服务再拷两边）：错开的那一侧要么库指着不存在的文件，要么文件在
库里没有行。

## 结构

分层单体，依赖单向向下，`bootstrap.py` 是唯一的组装根。

| 目录 | 职责 |
|---|---|
| `src/sams/core/` | 领域类型与端口（日历、源健康、事件总线、配置、异常）。不 import 任何下层。 |
| `src/sams/storage/` | DuckDB 包装、迁移、仓库层。用户数据隔离在这里兜底。**主键的形态也在这一层落定**（D50 + D51 + D52）：`market` 走 `canonical_market`、`code` 走 `canonical_code`，写侧归一、读侧用**同一个函数**处理入参——各仓库自己 `strip().upper()` 看着一样，实际会让 K 线存 `700`、除权记录存 `00700`。批量写入那两个仓库（`bars_daily`/`symbols`）共用 `repos/_keys.normalize_keys`，一条规则从此只有一份实现。键的正确性不能取决于「调用方走没走那条归一的路」。 |
| `src/sams/analysis/` | 现算层：复权因子、区间收益、（后续的）指标与估值。只依赖 core——它拿到的三份事实（K 线、公司行为、覆盖确认）全是参数，「能顺手去看一眼库」与「由调用方给」只差一个习惯，而前者会让守卫变成装饰。「谁调我」不影响我能看见谁，所以 `services` 调它而不是反过来。所有「不可信就不给数」的判定都在这层，并带上原因。 |
| `src/sams/datasource/` | 「外部接口 → 标准列集」规范化，按市场路由与降级、限速。 |
| `src/sams/services/` | 用例编排（注册、同步、日历、健康、复权读数）。`services/analysis.py` 是守卫唯一的读用例：市场名与代码在入口归一（`a` 与 `A`、`sh601088` 与 `601088` 在库里是同一个键；这是第二重，第一重在仓库层，D50），取三份事实（K 线、`until=today` 的公司行为、覆盖确认）交给 `analysis`，判定一条都不在这里写——人类出口是 `sams analysis return`。 |
| `src/sams/cli/` | argparse 分发，逻辑都在可测函数里。 |
| `src/sams/bootstrap.py` | 把上面几层装配成 `Container`。 |

`tests/test_layering.py` 用 AST 扫描 import 机器校验这些边界，越界依赖会直接让测试失败。

## 测试

```bash
uv run pytest -q          # 离线，不碰网络
uv run pytest -m live     # 需要真实网络的用例（akshare/yfinance/交易所日历）
uv run ruff check .
```

测试通过构造注入替换数据源（`tests/support.py` 的 `StubAdapter`），真实网络用例统一打
`@pytest.mark.live` 并由默认 `addopts` 排除。
