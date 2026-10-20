-- 0015：通知层能落地的四处形状（spec §11 / §4.1 / §4.3）
--
-- 1) `channels` 缺「这条通道叫什么」和「最后一次为什么失败」。名字不是装饰：一个用户
--    接了两个企微群时，`sams channel list` 里两行 `wecom` 是不可区分的，而规则上的
--    `channel_ids` 存的正是 id——选错一个就把告警发进了另一个群。`last_error` +
--    `last_error_at` 是「健康页说它不健康」的唯一依据：只有 `last_ok_at` 的话，
--    「没发过」与「每次都失败」在屏幕上长得一样。
--
-- 2) `users` 的免打扰三列是一个**整体**：只有 start 没有 end 意思是「从今天起永远不打扰」，
--    这不是任何人的本意。所以校验放在仓库层（三者同存同空），不放在 DDL：DuckDB 的
--    CHECK 报错到 CLI 就只剩一句约束名，而这一条约束的话要说清「为什么必须一起」。
--
-- 3) `notify_log.success BOOLEAN` 换成 `outcome VARCHAR`：§11 的四态（sent/failed/
--    suppressed/no_channel）里后两种既不是成功也不是失败，「被压制」与「发失败」混在
--    一个 false 里，健康页就只能报「N 次没成」而说不出「其中 M 次是你自己让开的」。
--    DuckDB 不许 `ADD COLUMN ... NOT NULL`（实测），新列只能先可空；读侧一律
--    `coalesce(outcome, 'failed')`，这样即使有人手工插了一行没有结果的记录，报告里它
--    也算失败——宁缺勿滥的方向是「不冒充成功」。
--
-- 4) `alerts.severity` 与 5) `alerts.rule_id` 的 NOT NULL 一起去掉，走重建而不是 ALTER。
--    级别属于那一条告警，不属于规则：规则今天把 mid 改成 strong，历史里那条仍然是当时的
--    mid，否则「上次为什么没打电话」这个问题没有答案。
--    `0001` 写下 rule_id NOT NULL 时假设「每条告警都由一条规则产生」，而通知层第一批消费者
--    里就有不是规则来的：手工投递、M3-D 的系统告警。这一列从此是「可空的外来引用」，
--    与 account_id 同形。
--
--    为什么不是 `ALTER COLUMN ... DROP NOT NULL`：那句要重建表，而 `idx_alerts_throttle`
--    挂在它上面，DuckDB 先拒绝（Dependency Error）；把「拆索引 → 改列 → 建回同名索引」
--    塞进同一个事务，COMMIT 时 DuckDB 直接 abort（实测 libc++abi: Pure virtual function
--    called）。0003 的重建形状没这个问题：表整个换掉，旧索引随表消失，新索引是新对象。
CREATE TABLE alerts_new (
    id           VARCHAR PRIMARY KEY,
    user_id      VARCHAR NOT NULL,
    rule_id      VARCHAR,
    account_id   VARCHAR,
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    stage        VARCHAR NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload_json VARCHAR NOT NULL,
    status       VARCHAR NOT NULL DEFAULT 'new',
    handled_at   TIMESTAMPTZ,
    handle_note  VARCHAR,
    dismiss_reason VARCHAR,
    severity     VARCHAR
);

INSERT OR REPLACE INTO alerts_new
    (id, user_id, rule_id, account_id, market, code, stage, triggered_at, payload_json,
     status, handled_at, handle_note, dismiss_reason)
SELECT id, user_id, rule_id, account_id, market, code, stage, triggered_at, payload_json,
       status, handled_at, handle_note, dismiss_reason
FROM alerts;

DROP TABLE alerts;
ALTER TABLE alerts_new RENAME TO alerts;
CREATE INDEX IF NOT EXISTS idx_alerts_throttle ON alerts (rule_id, market, code, triggered_at);

ALTER TABLE channels ADD COLUMN IF NOT EXISTS name VARCHAR;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS last_error VARCHAR;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;

ALTER TABLE users ADD COLUMN IF NOT EXISTS dnd_start TIME;
ALTER TABLE users ADD COLUMN IF NOT EXISTS dnd_end TIME;
ALTER TABLE users ADD COLUMN IF NOT EXISTS dnd_tz VARCHAR;

-- `outcome` **故意**停在可空：老库里已有的行没有「四态」可言（那一列当时叫 `success`，
-- 一个 boolean 拆不出「被压制」还是「没通道」），读侧一律 `coalesce(outcome, 'failed')`。
-- 因此 `success` 直接 DROP，不参与回填：回填的依据只能是「新列还没有值」，
-- 而不是一个已经被删掉、语义又对不上四态的旧列。
ALTER TABLE notify_log DROP COLUMN IF EXISTS success;
ALTER TABLE notify_log ADD COLUMN IF NOT EXISTS outcome VARCHAR;
UPDATE notify_log SET outcome = 'failed' WHERE outcome IS NULL;

-- 老行（`0001` 时代写进来的）没有名字：按 kind + id 尾巴给一个可解释的默认名，
-- 而不是留 NULL——NULL 名字会让 `channel list` 出现一行空白，运维认不出是哪条。
UPDATE channels
SET name = kind || '-' || substr(id, 1, 6)
WHERE name IS NULL;
