-- 0031：摘掉 symbol_master_search（D230）
--
-- DuckDB 2.0 的显式非唯一 ART 索引能持久化到坏状态（duckdb/duckdb#23645）：
-- 周更的 INSERT OR REPLACE 在 COMMIT 时报 "Failed to delete all rows from index"，
-- 整个连接被标成 FATAL。那枚索引对检索从未有用：查询没有 market 等值条件、OR 跨九列，
-- 6000 行的 EXPLAIN ANALYZE 有没有它都是 Sequential Scan。留着只有故障面，没有收益。
DROP INDEX IF EXISTS symbol_master_search;
