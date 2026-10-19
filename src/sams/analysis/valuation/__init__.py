"""估值合成（M2-D，spec §8.3）：把「现在的这个数」放回它自己的时间序列，再合成区间与赔率。

分层与 M2-C 一样：这一层可以推导，但不碰数据库。输入是 services 从 `valuation_daily` /
`financials` / 指标序列里读出来的行，所以每一个数都能被逐日复算。

包内三块各管一件事，谁也不替谁下结论：

* `percentile` —— 「现在这个 PR / PE / PB 处在过去 N 年的第几档」，PIT 分位；
* `styles` —— `value` / `trend` 两套模板的权重与参数（可复制改，内置那两份只读）；
* `scoring` / `compose` —— 分项打分（每项带 evidence）与 fair/entry/stop/target/赔率的合成。
"""
