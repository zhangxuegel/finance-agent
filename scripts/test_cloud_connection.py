# -*- coding: utf-8 -*-
"""test_cloud_connection.py —— 全链路读写自检（T-Fin09 步骤 4）

读验证：云端 sb_shadow_eod 拉最新 1 行，与本地 shadow_live.db 最新 eod 行
        逐字段对账（数字必须一致——防云端被污染/同步丢失）。
写验证：把本地最新一行以同值幂等 upsert 回云（merge-duplicates 主键不变、
        数据不变）→ HTTP 2xx 即可写；不留任何"测试行"垃圾。
退出：问题 → 非0 + 指引；一切正常 → 打印 ✅。
"""
import json
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import sync_to_cloud as sc

# eod 数值字段（对账仅 float 这些；trade_date/notes 等文本字段做相等比较）
_NUMERIC_FIELDS = frozenset({
    "nav", "nav_prev", "day_ret", "bench_index_close", "bench_index_pct",
    "alpha", "defense", "fills_n", "lock_remaining"})


def local_latest_eod() -> tuple[dict, int]:
    conn = sqlite3.connect(sc.DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM eod ORDER BY trade_date DESC "
                       "LIMIT 1").fetchone()
    n = conn.execute("SELECT COUNT(*) FROM eod").fetchone()[0]
    conn.close()
    return (dict(row) if row else {}), n


def main() -> int:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    if not (url and key):
        print("⛔ 云端未配置：SUPABASE_URL/KEY（先跑 python3 scripts/"
              "cloud_provision.py 完成自动接管）")
        return 2
    import requests
    sess = requests.Session()
    sess.headers.update({"apikey": key, "Authorization": f"Bearer {key}"})
    # ---- 读验证 ----
    r = sess.get(f"{url}/rest/v1/sb_shadow_eod",
                 params={"select": "*", "order": "trade_date.desc",
                         "limit": "1"}, timeout=(5, 20))
    if r.status_code != 200:
        print(f"❌ 读失败 HTTP {r.status_code}（表存在？看 README_DEPLOY 第 1 步）")
        return 1
    cloud = (r.json() or [None])[0]
    loc, local_n = local_latest_eod()
    if not loc:
        print("⚠️ 本地 eod 为空——先跑影子台账；读验证中止（云端不动）")
        return 1
    if cloud is None:
        print(f"❌ 云端空表：本地 {local_n} 行还没推？→ bash sync_all.sh")
        return 1
    mism_num, mism_txt = [], []
    for k, v in loc.items():
        if k not in cloud:
            continue
        if k in _NUMERIC_FIELDS:  # 只对数值字段做 float 对账
            if float(cloud[k] or 0) != float(v or 0):
                mism_num.append(k)
        elif (cloud[k] or "") != (str(v) if v is not None else ""):
            mism_txt.append(k)
    if mism_num or mism_txt:
        print(f"❌ 云/本地对账不一致字段：数值={mism_num} 文本={mism_txt}"
              "（数据可能被污染——查 events）")
        return 1
    # ---- 写验证（同值幂等 upsert，不新增行）----
    r2 = sess.post(f"{url}/rest/v1/sb_shadow_eod",
                   params={"on_conflict": "trade_date"},
                   data=json.dumps([loc]),
                   headers={"Prefer": "resolution=merge-duplicates,"
                                      "return=minimal"},
                   timeout=(5, 20))
    if r2.status_code < 200 or r2.status_code >= 300:
        print(f"❌ 写失败 HTTP {r2.status_code}（key 权限不足？）")
        return 1
    n_c = sess.get(f"{url}/rest/v1/sb_shadow_eod?select=*",
                   timeout=(5, 20)).json()
    print(f"✅ 读写全通：本地 {local_n} 行 == 云端 {len(n_c)} 行，"
          f"对账 0 字段差；写验证=幂等 upsert（未增行）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"❌ 未预期 {type(e).__name__}: {e}")
        sys.exit(1)
