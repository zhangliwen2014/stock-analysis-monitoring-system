# 摘掉 symbol_master_search：一枚没人用、却能把库打坏的索引（D230）

## 1. 事故

2026-10-30 启动 serve 时，周更代码表的 `INSERT OR REPLACE INTO symbol_master` 在 COMMIT
时报：

```
Invalid Input Error: Failed to delete all rows from index.
Only deleted 0 out of 1855 rows
```

连接随即被 DuckDB 标为 FATAL，进程内所有后续查询不可用。数据本身完好（回读行数与内容
一致），`DROP INDEX` + `CREATE INDEX` 重建后恢复正常。

这是 DuckDB 2.0.0 的上游缺陷 [duckdb/duckdb#23645](https://github.com/duckdb/duckdb/issues/23645)：
显式非唯一 ART 索引可以持久化到坏状态，且跨 checkpoint / 重启存活。也就是说，**重建
只是把下一次故障推迟**，周更每跑一次就再掷一次骰子。

## 2. 那枚索引从未被用上

0021 建的是：

```sql
CREATE INDEX symbol_master_search ON symbol_master (market, py_initials, code);
```

而 `SymbolListRepo.search` 的查询：

- 没有 `market = ?` 这样的等值条件——检索是跨市场的；
- `WHERE` 是一个横跨 `code / py_initials / py_full / name` 的九路 `OR`；
- 结果按五档 `CASE` 排序，不是按索引列序。

6000 行合成数据上 `EXPLAIN ANALYZE` 的结果：有没有这枚索引都是 `Sequential Scan`，
6,000 行扫描耗时 0µs。表本身只有一万四千行（A+HK+US），全表扫本来就是几微秒的事。

## 3. D230：迁移 0031 直接摘掉它

`DROP INDEX IF EXISTS symbol_master_search;`

- 对老库：迁移时摘掉，行原样保留（已验证 14,304 行不变）。
- 对新库：0021 建、0031 摘，最终形态一致；迁移序列保留历史，不改旧文件。
- 不做「坏了就重建」的自愈：自愈要接住一个 FATAL 的连接（此时进程已经不可信），
  还要判断是索引故障还是别的问题；而收益是零——这枚索引对任何查询都没有贡献。
- `PRIMARY KEY (market, code)` 的隐式索引仍在：本次事故与它无关，也没有理由为了
  一个没有替代方案的索引去动主键。

## 4. 验收

- `tests/storage/test_migration_0031.py`：新库没有这枚索引；只应用 0001–0030 的老库
  在补上 0031 后索引消失、行不丢。
- `tests/storage` + 代码表检索/服务/页面的用例全绿（744 passed）。
- 真实库：迁移后 `duckdb_indexes()` 里不再有 `symbol_master_search`，`symbol_master`
  仍是 14,304 行。
