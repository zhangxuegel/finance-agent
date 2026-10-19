-- cloud_schema.sql —— 影子实盘云端台账表（T-Fin09 自动建表输入）
-- 由 cloud_provision.py 通过 Management API 在目标库执行（幂等可重放）。

CREATE TABLE IF NOT EXISTS sb_shadow_eod (
  trade_date        text PRIMARY KEY,
  nav               numeric,
  nav_prev          numeric,
  day_ret           numeric,
  bench_index_close numeric,
  bench_index_pct   numeric,
  alpha             numeric,
  defense           integer,
  fills_n           integer,
  lock_remaining    integer,
  notes             text
);

CREATE TABLE IF NOT EXISTS sb_shadow_fills (
  id             bigint PRIMARY KEY,
  ts             text,
  trade_date     text,
  leg            text,
  code           text,
  side           text,
  shares         bigint,
  price          numeric,
  fee            numeric,
  reason         text,
  limit_blocked  integer
);

-- 收尾自检：两表存在即可（cloud_provision 会 POST 这段后逐表 count）
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'sb_shadow_eod') THEN
    RAISE EXCEPTION 'sb_shadow_eod 建表失败';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'sb_shadow_fills') THEN
    RAISE EXCEPTION 'sb_shadow_fills 建表失败';
  END IF;
END $$;
