# SAMS · 多市场股票分析、监控与模拟交易系统

覆盖 A 股 / 港股 / 美股的分析、监控与**模拟**交易链路：

```
数据采集 → 指标与估值 → 投资观点(Thesis) → 派生监控规则 → 多渠道告警
                                                  ↓
                                        批注留痕 + 复盘闭环
```

不接券商实盘；真实持仓以手动录入 / CSV 导入形成镜像账本。

**当前进度：M1 地基已完成并自测通过**（配置、事件总线、分市场交易日历、DuckDB 存储与仓库层、A 股主备源与港美股日线采集、CLI 运维入口）。
M2 指标与估值、M3 规则与告警尚未开始。设计文档见
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
uv run sams sync actions --market A --code 601088

uv run sams calendar refresh --market HK --year 2026
uv run sams calendar check   --market US --date 2026-03-09   # 是否交易日 + 时段（本地 / UTC）
uv run sams health                                             # 各表行数、日历覆盖、各源最近错误
```

除 `db` 外的命令都会顺手补齐迁移，新装环境直接 `user add` 不会报缺表。
`sync` 可重复执行：主键 + `INSERT OR REPLACE` 保证同一区间重跑后行数不变。
两个子命令走两条独立的能力链（D38）：`daily` 只走有日线的源，`actions` 只走能给除权记录的源，
链上被跳过的源会打印出来——「这只票没分过红」与「这条链没人能给」是两件事。

## 结构

分层单体，依赖单向向下，`bootstrap.py` 是唯一的组装根。

| 目录 | 职责 |
|---|---|
| `src/sams/core/` | 领域类型与端口（日历、源健康、事件总线、配置、异常）。不 import 任何下层。 |
| `src/sams/storage/` | DuckDB 包装、迁移、仓库层。用户数据隔离在这里兜底。 |
| `src/sams/datasource/` | 「外部接口 → 标准列集」规范化，按市场路由与降级、限速。 |
| `src/sams/services/` | 用例编排（注册、同步、日历、健康）。 |
| `src/sams/cli/` | argparse 分发，逻辑都在可测函数里。 |
| `src/sams/bootstrap.py` | 把上面四层装配成 `Container`。 |

`tests/test_layering.py` 用 AST 扫描 import 机器校验这些边界，越界依赖会直接让测试失败。

## 测试

```bash
uv run pytest -q          # 离线，不碰网络
uv run pytest -m live     # 需要真实网络的用例（akshare/yfinance/交易所日历）
uv run ruff check .
```

测试通过构造注入替换数据源（`tests/support.py` 的 `StubAdapter`），真实网络用例统一打
`@pytest.mark.live` 并由默认 `addopts` 排除。
