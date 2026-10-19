# -*- coding: utf-8 -*-
"""sync_to_cloud.py —— 本地台账 → 云端数据库 增量同步（T-Fin07 模块 1）

数据链路（解耦设计）：本地算账（shadow_live.db / data/live/*.csv）
→ 增量推流（REST API）→ 云端看板（Streamlit，读云端库）。
**本地链路零依赖云端**：任何云端故障都不影响本地撮合与台账
（验收 1：本地与云端完全隔离）。

配置（环境变量，也可写在 .streamlit/secrets.toml）：
  SUPABASE_URL     例如 https://xxxx.supabase.co
  SUPABASE_KEY     anon/service key（README_DEPLOY.md 有取用说明）
  SUPABASE_TABLES  可选，批量改名前缀（默认 sb_）

云端表（README_DEPLOY.md 提供建表 SQL）：
  sb_shadow_eod   (trade_date 主键, nav, nav_prev, day_ret, bench_index_close,
                   bench_index_pct, alpha, defense, fills_n, lock_remaining, notes)
  sb_shadow_fills (id 主键, ts, trade_date, leg, code, side, shares, price,
                   fee, reason, limit_blocked)

增量纪律（只推新增）：
  · data/cloud_sync_state.json 记录水位（max_eod_date / max_fill_id）；
  · eod 推 trade_date > 水位的行，fills 推 id > 水位的行；
  · 云端以 upsert（on_conflict 主键，merge-duplicates）写入：重放不重不丢；
  · 成功后才推进水位（幂等：重复运行推第二批 = 空集）。
"""
import argparse
import json
import os
import sys
import sqlite3
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

DB = os.path.join(ROOT, "data", "shadow_live.db")
SYNC_STATE = os.path.join(ROOT, "data", "cloud_sync_state.json")


def _read_state() -> dict:
    try:
        with open(SYNC_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"max_eod_date": "", "max_fill_id": 0}


def _write_state(st: dict):
    os.makedirs(os.path.dirname(SYNC_STATE), exist_ok=True)
    with open(SYNC_STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def read_local(state: dict, table_prefix: str = "sb_") -> dict:
    """读本地 SQLite：eod 行（date>水位）+ fills 行（id>水位）。纯本地。"""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    eod = [dict(r) for r in conn.execute(
        f"SELECT * FROM eod WHERE trade_date > ? ORDER BY trade_date",
        (state["max_eod_date"],))]
    fills = [dict(r) for r in conn.execute(
        f"SELECT * FROM fills WHERE id > ? ORDER BY id",
        (state["max_fill_id"],))]
    conn.close()
    return {f"{table_prefix}shadow_eod": eod,
            f"{table_prefix}shadow_fills": fills}


def _client():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise RuntimeError(
            "未配置云端 API：请设置 SUPABASE_URL / SUPABASE_KEY "
            "（README_DEPLOY.md 第 1 步）")
    import requests
    sess = requests.Session()
    sess.headers.update({"apikey": key, "Authorization": f"Bearer {key}",
                         "Content-Type": "application/json",
                         "User-Agent": "finance-agent-sync/1.0"})
    return sess, url


def push_cloud(payload: dict, url: str, sess, on_conflict_cols: dict) -> dict:
    """逐表 upsert（Supabase REST：Prefer merge-duplicates）。"""
    import requests
    pushed = {}
    for table, rows in payload.items():
        if not rows:
            pushed[table] = 0
            continue
        resp = sess.post(
            f"{url}/rest/v1/{table}",
            params={"on_conflict": on_conflict_cols[table]},
            data=json.dumps(rows, ensure_ascii=False, default=str),
            headers={"Prefer": "resolution=merge-duplicates,"
                               "return=representation,count=*"}, timeout=(5, 30))
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(f"云端写入失败 {table}: HTTP {resp.status_code} "
                               f"{resp.text[:200]}")
        pushed[table] = len(json.loads(resp.text or "[]")) or len(rows)
    return pushed


def advance_watermark(payload: dict, state: dict) -> dict:
    for row in payload.get("sb_shadow_eod", []):
        state["max_eod_date"] = max(state["max_eod_date"], row["trade_date"])
    for row in payload.get("sb_shadow_fills", []):
        state["max_fill_id"] = max(state["max_fill_id"], int(row["id"]))
    return state


def sync_once(verbose: bool = True) -> dict:
    """一次增量同步：读本地新增 → 推云端 → 推进水位。云端无关本地（隔离）。"""
    state = _read_state()
    payload = read_local(state)
    n_eod = len(payload["sb_shadow_eod"])
    n_fills = len(payload["sb_shadow_fills"])
    if n_eod == 0 and n_fills == 0:
        if verbose:
            print("[sync] 本地无新增（水位 max_fills_id="
                  f"{state['max_fill_id']} max_eod_date='{state['max_eod_date']}'）")
        return {"eod": 0, "fills": 0}
    sess, url = _client()
    pushed = push_cloud(payload, url, sess,
                        on_conflict_cols={"sb_shadow_eod": "trade_date",
                                          "sb_shadow_fills": "id"})
    _write_state(advance_watermark(payload, state))
    if verbose:
        print(f"[sync] 云端 upsert：eod +{pushed.get('sb_shadow_eod', n_eod)} 行 / "
              f"fills +{pushed.get('sb_shadow_fills', n_fills)} 行 → {url}")
    _event_log(n_eod, n_fills, pushed)
    return {"eod": pushed.get("sb_shadow_eod", n_eod),
            "fills": pushed.get("sb_shadow_fills", n_fills)}


def _event_log(n_eod: int, n_fills: int, pushed: dict):
    """同步事件进本地 db（可溯源；失败只进日志不炸 cron）。"""
    try:
        feed = __import__("live_data_feed")
        feed.log_error("cloud_sync", f"eod+{n_eod} fills+{n_fills} → {pushed}")
    except Exception as e:  # noqa: BLE001
        print(f"[sync] log_error 通道异常 {e}")


def ping_cloud() -> bool:
    """连通性自检：读取 1 行 counts。"""
    sess, url = _client()
    for table in ("sb_shadow_eod", "sb_shadow_fills"):
        import requests
        r = sess.get(f"{url}/rest/v1/{table}",
                     params={"select": "*", "limit": "1"}, timeout=(5, 15))
        if r.status_code not in (200,):
            raise RuntimeError(f"{table} 探活失败 HTTP {r.status_code}")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="增量推送一次")
    ap.add_argument("--test", action="store_true",
                    help="自检：本地读数 + （已配置则）云端探活")
    a = ap.parse_args()
    try:
        if a.test:
            st = _read_state()
            payload = read_local(st)
            print(f"[sync] 本地读数 ok：待推 eod={len(payload['sb_shadow_eod'])} "
                  f"fills={len(payload['sb_shadow_fills'])}")
            if os.environ.get("SUPABASE_URL"):
                print("[sync] 云端探活…")
                ping_cloud()
                print("[sync] 云端连通 ✅")
            else:
                print("[sync] 未配置 SUPABASE_URL（本地不受影响）；"
                      "云端配置见 README_DEPLOY.md 第 1 步")
            sys.exit(0)
        if a.push:
            sync_once()
            sys.exit(0)
        print("用法：--push（增量推送）/ --test（自检）")
    except Exception as e:  # noqa: BLE001 —— 云端故障绝不放倒本地
        try:
            __import__("live_data_feed").log_error("cloud_sync_fatal", str(e))
        except Exception:
            pass
        print(f"[sync] 失败（本地不受影响）：{e}")
        sys.exit(0)
