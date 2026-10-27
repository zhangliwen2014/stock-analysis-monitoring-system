-- 0022：自选的「移除」改成折起（D169）。一列时间戳，只写不读回 Python
ALTER TABLE watchlists ADD COLUMN removed_at TIMESTAMPTZ;
