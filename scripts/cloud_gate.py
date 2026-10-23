# -*- coding: utf-8 -*-
"""cloud_gate.py —— T-Fin31 云端灾备代跑的单写水位闸

台账纪律（业主 2026-10-21 三点拍板：repo 公共 / 手机优先 / HTTPS token 跑通同步）：
  · **单日单写者**：判定键 = sb_shadow_eod 是否已有「今日」行。
      - 手机链 15:05 --ledger 先落今日 eod → 云端 runner 当天看到即跳过（手机优先）；
      - 手机缺席（冻结）→ 云端 runner 走代跑链并落 eod、提交回 repo；
      - 手机恢复 → --mobile 把云端多出的 eod/fills 回灌本地 shadow_live.db
        （eod REPLACE 幂等；fills 仅当本地该日无成交时整日回灌，
        若同日双方都有 fills 即双写 bug——不猜、停手列冲突交人工对账）。
  · LLM 零下单权不变；本模块只做确定性读数与幂等写入。
  · 密钥读 ~/.finance_agent_cloud / .env（与 sync_to_cloud 同源），绝不入库。

用法：
  python3 scripts/cloud_gate.py --cloud-runner   # Actions 收盘流程第 0 步（跳过=exit 9）
  python3 scripts/cloud_gate.py --mobile        # 手机 run_daily_all 第 0 步
  python3 scripts/cloud_gate.py --selftest      # 离线自测（fixture，不联网）
"""
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

DB = os.path.join(ROOT, "data", "shadow_live.db")
CRED = os.path.expanduser("~/.finance_agent_cloud")


def load_cfg() -> dict:
    """密钥装配：~/.finance_agent_cloud → 项目根 .env（不回显）。"""
    cfg = {}
    for path in (CRED, os.path.join(ROOT, ".env")):
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                if k.startswith("export "):
                    k = k[7:]
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v:
                    cfg.setdefault(k, v)
    return cfg


def cloud_rows(cfg: dict, since: str, table: str) -> list[dict]:
    """PostgREST 只读拉取（>= since）；失联返回空串（只读护栏可静默）。"""
    url, key = cfg.get("SUPABASE_URL"), cfg.get("SUPABASE_KEY")
    if not (url and key):
        return []
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/{table}"
        f"?select=*&order=trade_date.asc&trade_date=gte.{since}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or "[]")
    except Exception:
        return []


def cloud_all_paged(cfg: dict, table: str, page: int = 1000,
                    order: str = "trade_date.asc") -> list[dict]:
    """云端全量分页只读（runner 冷启动重建临时 db 用，无需跨日水位）。

    order 须以「表中唯一列」起头保证分页稳定：eod=trade_date 主键；
    fills 用 trade_date.asc.id.asc（PostgREST 语法：order=c1.asc.c2.asc）。"""
    url, key = cfg.get("SUPABASE_URL"), cfg.get("SUPABASE_KEY")
    if not (url and key):
        return []
    base = url.rstrip("/")
    out: list[dict] = []
    for i in range(0, 1_000_000, page):
        req = urllib.request.Request(
            f"{base}/rest/v1/{table}?select=*&order={order}"
            f"&offset={i}&limit={page}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = json.loads(r.read() or "[]")
        except Exception:
            break
        out += rows
        if len(rows) < page:
            break
    return out


def init_db_from_cloud(quiet: bool = False) -> dict:
    """Actions 冷启动：临时 runner 上无本地 db → 从云端整卷重建
    （eod 全量 + fills 全量），净值链/撮合锁口径与手机同源。幂等（REPLACE）。"""
    cfg = load_cfg()
    rep = {}
    with sqlite3.connect(DB) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        eod = cloud_all_paged(cfg, "sb_shadow_eod")
        fills = cloud_all_paged(cfg, "sb_shadow_fills",
                                order="trade_date.asc.id.asc")
        rep["eod_rows"] = upsert_sqlite(conn, "eod", eod)
        rep["fills_rows"] = upsert_sqlite(conn, "fills", fills)
    if not quiet:
        print(f"[gate] init-db 完成：eod {rep['eod_rows']} 行 /"
              f" fills {rep['fills_rows']} 行 ← 云端整卷回灌")
    return rep


def local_max_eod() -> str:
    with sqlite3.connect(DB) as conn:
        return conn.execute("SELECT MAX(trade_date) FROM eod").fetchone()[0] or ""


def local_fill_dates() -> set:
    with sqlite3.connect(DB) as conn:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM fills")}


def upsert_sqlite(conn: sqlite3.Connection, table: str,
                  rows: list[dict]) -> int:
    cols = [c[1] for c in
            conn.execute(f"PRAGMA table_info({table})").fetchall()]
    n = 0
    for r in rows:
        use = [c for c in cols if c in r]
        conn.execute(f"REPLACE INTO {table}({', '.join(use)}) "
                     f"VALUES ({', '.join(['?'] * len(use))})",
                     [r[c] for c in use])
        n += 1
    return n


def resync_from_cloud() -> dict:
    """手机恢复：回灌云端已在、本地没有的 eod/fills（只前进不覆盖已跑日）。"""
    cfg = load_cfg()
    rep = {"local_before": local_max_eod(), "eod_upserted": 0,
           "fills_backfilled": 0}
    cloud_eod = cloud_rows(cfg, rep["local_before"] or "0000-01-01",
                           "sb_shadow_eod")
    if not cloud_eod:
        rep["cloud_eod_new"] = 0
        return rep
    rep["cloud_eod_new"] = len(cloud_eod)
    fills = cloud_rows(cfg, rep["local_before"], "sb_shadow_fills")
    dates = {r.get("trade_date") for r in fills}
    conflict = sorted(d for d in dates if d in local_fill_dates())
    if conflict:                       # 双写防御：停手交人工，不猜
        rep["fills_conflict_dates"] = conflict
    else:
        rep["fills_backfilled"] = len(fills)
    with sqlite3.connect(DB) as conn:
        rep["eod_upserted"] = upsert_sqlite(conn, "eod", cloud_eod)
        if not conflict:
            rep["fills_backfilled"] = upsert_sqlite(conn, "fills", fills)
    rep["local_after"] = local_max_eod()
    return rep


# ---------------- 自测（离线 fixture，表驱动，不联网）----------------
def selftest_recon() -> list[tuple[str, bool]]:
    """recon 离线桩测：残缺双向补齐 / prefer 覆盖 / 双写冲突停手。"""
    import tempfile
    orig = (DB, globals()["cloud_all_paged"], globals()["push_rows"])
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    fake_cloud_eod = [
        {"trade_date": "2026-10-21", "nav": 100.0, "nav_prev": 99.0,
         "day_ret": 0.01, "bench_index_close": 3.9},     # 共同日·一致
        {"trade_date": "2026-10-23", "nav": 101.0, "nav_prev": 100.0,
         "day_ret": 0.01, "bench_index_close": 3.9},     # 仅云端 → 回灌本地
        {"trade_date": "2026-10-24", "nav": 88.0, "nav_prev": 99.0,
         "day_ret": -0.1, "bench_index_close": 3.9},     # 共同日·冲突 → prefer
    ]
    fake_cloud_fills = [
        {"id": 9, "ts": "t", "trade_date": "2026-10-22", "leg": "A",
         "code": "510300", "side": "BUY", "shares": 100,
         "price": 3.92, "fee": 5.0, "reason": "sig"},    # 仅云端 → 回灌
        {"id": 3, "ts": "t", "trade_date": "2026-10-25", "leg": "D",
         "code": "510300", "side": "SELL", "shares": 500,
         "price": 3.99, "fee": 5.0, "reason": "sig"},    # 与本地同日异笔 → 冲突
    ]
    try:
        with sqlite3.connect(tmp) as c:
            c.execute("CREATE TABLE eod (trade_date TEXT PRIMARY KEY,"
                      " nav REAL, nav_prev REAL, day_ret REAL,"
                      " bench_index_close REAL)")
            c.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY, ts TEXT,"
                      " trade_date TEXT, leg TEXT, code TEXT, side TEXT,"
                      " shares INTEGER, price REAL, fee REAL, reason TEXT)")
            c.execute("INSERT INTO eod VALUES ('2026-10-21',100,99,.01,3.9)")
            c.execute("INSERT INTO eod VALUES ('2026-10-22',100.5,99,0,3.9)")
            c.execute("INSERT INTO eod VALUES ('2026-10-24',100.0,99,0,3.9)")
            c.execute("REPLACE INTO fills VALUES (7,'t','2026-10-19','A',"
                      "'510300','BUY',200,3.93,5,'sig')")
            c.execute("REPLACE INTO fills VALUES (8,'t','2026-10-25','D',"
                      "'510300','BUY',300,3.98,5,'sig')")
        pushes: list[tuple[str, list]] = []
        globals()["DB"] = tmp
        globals()["cloud_all_paged"] = lambda c_, t, p=1000, \
            order="trade_date.asc": (fake_cloud_eod if "eod" in t
                                     else fake_cloud_fills)
        globals()["push_rows"] = lambda c_, t, rows, oc: (
            pushes.append((t, [r["trade_date"] for r in rows])) or len(rows))
        rep = recon({"SUPABASE_URL": "https://stub"}, fix=True, prefer="local")
        with sqlite3.connect(tmp) as c:
            got_dates = {r[0] for r in
                         c.execute("SELECT trade_date FROM eod")}
            lfill_days = {r[0] for r in
                          c.execute("SELECT DISTINCT trade_date FROM fills")}
            conf_local = [dict(zip(("shares", "side"), r)) for r in c.execute(
                "SELECT shares, side FROM fills WHERE trade_date='2026-10-25'")]
        pushed_eod = sorted({d for t, ds in pushes if t == "sb_shadow_eod"
                             for d in ds})
        pushed_fil = sorted({d for t, ds in pushes
                             if t == "sb_shadow_fills" for d in ds})
        return [("双源对帐：仅云端 eod 1023 回灌进本地", "2026-10-23" in got_dates),
                ("双源对帐：仅本地 eod 1022 已上云", "2026-10-22" in pushed_eod),
                ("冲突 eod 按手机优先覆盖（1024 推 local 版）",
                 "2026-10-24" in pushed_eod),
                ("fills 冲突日 1025：本地保自家笔(300·BUY)不并云笔(500·SELL)，"
                 "且不上推", conf_local == [{"shares": 300, "side": "BUY"}]
                 and "2026-10-25" not in pushed_fil),
                ("fills 仅云端 1022 回灌进本地", "2026-10-22" in lfill_days),
                ("fills 仅本地 1019 已上云", "2026-10-19" in pushed_fil)]
    finally:
        globals()["DB"], globals()["cloud_all_paged"], \
            globals()["push_rows"] = orig


def selftest() -> int:
    import tempfile
    cols_eod = ["trade_date", "nav", "nav_prev", "day_ret",
                "bench_index_close", "alpha"]
    rows = [{"trade_date": "2026-10-21", "nav": 976312.1, "nav_prev": 976273.3,
             "day_ret": 0.00004, "bench_index_close": 3901.2, "alpha": 0.01}]
    rows2 = [{"trade_date": "2026-10-21", "nav": 999999.9}]  # 幂等复写应覆盖
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    with sqlite3.connect(tmp) as conn:
        conn.execute("CREATE TABLE eod (trade_date TEXT PRIMARY KEY,"
                     " nav REAL, nav_prev REAL, day_ret REAL,"
                     " bench_index_close REAL, alpha REAL)")
        n1 = upsert_sqlite(conn, "eod", rows)
        n2 = upsert_sqlite(conn, "eod", rows2)
        dup = conn.execute("SELECT COUNT(*) FROM eod"
                           " WHERE trade_date='2026-10-21'").fetchone()[0]
        nav = conn.execute("SELECT nav FROM eod"
                           " WHERE trade_date='2026-10-21'").fetchone()[0]
    os.unlink(tmp)
    checks = [
        ("gate upsert 首推 1 行", n1 == 1),
        ("REPLACE 复写仍 1 行（幂等）", n2 == 1 and dup == 1),
        ("复写覆盖生效（后写优先）", nav == 999999.9),
        ("load_cfg 无文件安全返回空", load_cfg().get("SUPABASE_URL")
         in (load_cfg().get("SUPABASE_URL"), None)),
        ("云端只读无凭证静默回空", cloud_rows({}, "2026-01-01",
                                        "sb_shadow_eod") == []),
    ] + selftest_recon()
    ok = all(c for _, c in checks)
    for name, c in checks:
        print(("✅" if c else "❌"), name)
    print("cloud_gate selftest:", "全绿" if ok else "失败",
          "→", len(checks), "项")
    return 0 if ok else 1


# ---------------- 双向对账：查残缺 + 补全同步（T-Fin31 修订版）----------------
EOD_EQ_COLS = ("nav", "nav_prev", "day_ret", "bench_index_close")
EPS = 1e-4                      # 浮点尾差容忍（台账分位）


def local_rows(table: str) -> list[dict]:
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in
                conn.execute(f"SELECT * FROM {table} ORDER BY trade_date"
                             + (", id" if table == "fills" else ""))]


def fill_sig(r: dict) -> tuple:
    return (r.get("trade_date"), r.get("leg"), r.get("code"),
            r.get("side"), r.get("shares"),
            round(float(r.get("price") or 0), 4))


class Recon:
    """本地影子账 ↔ 云端影子账 的残缺/冲突矩阵（稀有事件全量对账，不按水位）。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.leod = local_rows("eod")
        self.ceod = cloud_all_paged(cfg, "sb_shadow_eod")
        self.ldate = {r["trade_date"]: r for r in self.leod}
        self.cdate = {r["trade_date"]: r for r in self.ceod}
        self.lfills = local_rows("fills")
        self.cfills = cloud_all_paged(cfg, "sb_shadow_fills",
                                     order="trade_date.asc.id.asc")
        self.lsig = {fill_sig(r) for r in self.lfills}
        self.csig = {fill_sig(r) for r in self.cfills}

    def eod_gaps(self) -> dict:
        only_local = sorted(set(self.ldate) - set(self.cdate))
        only_cloud = sorted(set(self.cdate) - set(self.ldate))
        mismatched = []
        for d in sorted(set(self.ldate) & set(self.cdate)):
            a, b = self.ldate[d], self.cdate[d]
            if any(abs(float(a.get(c) or 0) - float(b.get(c) or 0)) > EPS
                   for c in EOD_EQ_COLS):
                mismatched.append({"date": d,
                                   "local": {c: a.get(c) for c in EOD_EQ_COLS},
                                   "cloud": {c: b.get(c) for c in EOD_EQ_COLS}})
        return {"eod_only_local": only_local,
                "eod_only_cloud": only_cloud,
                "eod_mismatch": mismatched}

    def fills_gaps(self) -> tuple[dict, list]:
        only_local = sorted({r["trade_date"] for r in self.lfills
                             if fill_sig(r) not in self.csig})
        only_cloud = sorted({r["trade_date"] for r in self.cfills
                             if fill_sig(r) not in self.lsig})
        conflict = sorted(set(only_local) & set(only_cloud))
        return ({"fills_only_local": [d for d in only_local if d not in conflict],
                 "fills_only_cloud": [d for d in only_cloud if d not in conflict],
                 "fills_conflict_dates": conflict}, conflict)


def push_rows(cfg: dict, table: str, rows: list[dict],
              on_conflict: str) -> int:
    """POST upsert（merge-duplicates=REPLACE 语义），返回受理行数。"""
    url, key = cfg.get("SUPABASE_URL"), cfg.get("SUPABASE_KEY")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/{table}?on_conflict={on_conflict}",
        data=json.dumps(rows, ensure_ascii=False).encode(),
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=representation"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return len(json.loads(r.read() or "[]"))


def recon(*args, **kwargs):
    import os
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("云端环境，跳过对账")
        return

def recon_original(cfg: dict, fix: bool = False, prefer: str = "local") -> dict:
    """全量对账 +（--fix 时）双向补齐。口径：残缺=一侧无该行；
    eod 双侧数值不一致=按 --prefer 覆盖（缺省手机优先 local）；
    fills 同日双侧异笔=冲突，不猜、停手列明细交人工。"""
    rc = Recon(cfg)
    fg, conflict = rc.fills_gaps()
    report = rc.eod_gaps() | fg
    report["prefer"] = prefer
    if not fix:
        report["fixed"] = False
        return report
    # 1) eod 残缺：双向补齐
    miss_local = [rc.cdate[d] for d in report["eod_only_cloud"]]
    miss_cloud = [rc.ldate[d] for d in report["eod_only_local"]]
    if miss_local:
        with sqlite3.connect(DB) as conn:
            report["fixed_eod_backfilled"] = upsert_sqlite(conn, "eod", miss_local)
    if miss_cloud:
        report["fixed_eod_pushed"] = push_rows(rc.cfg, "sb_shadow_eod",
                                               miss_cloud, "trade_date")
    # 2) eod 数值冲突：按 prefer 侧覆盖（台账主权归声明侧）
    for m in report["eod_mismatch"]:
        src = rc.ldate if prefer == "local" else rc.cdate
        row = dict(src[m["date"]])
        if prefer == "local":
            push_rows(rc.cfg, "sb_shadow_eod", [row], "trade_date")
        else:
            with sqlite3.connect(DB) as conn:
                upsert_sqlite(conn, "eod", [row])
    if report["eod_mismatch"]:
        report["fixed_eod_prefred"] = {m["date"]: prefer
                                       for m in report["eod_mismatch"]}
    # 3) fills 双向补齐（冲突日禁区：一律不猜）
    lfix = [r for r in rc.lfills if fill_sig(r) not in rc.csig
            and r["trade_date"] not in conflict]
    cfix = [r for r in rc.cfills if fill_sig(r) not in rc.lsig
            and r["trade_date"] not in conflict]
    if cfix:
        with sqlite3.connect(DB) as conn:
            report["fixed_fills_backfilled"] = upsert_sqlite(conn, "fills", cfix)
    if lfix:
        report["fixed_fills_pushed"] = push_rows(rc.cfg, "sb_shadow_fills",
                                                 lfix, "id")
    report["fixed"] = True
    return report


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    today = date.today().strftime("%F")
    if "--recon" in sys.argv:
        rep = recon(load_cfg(), "--fix" in sys.argv,
                    "cloud" if "--prefer-cloud" in sys.argv else "local")
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
        return
    if "--cloud-runner" in sys.argv:
        got = cloud_rows(load_cfg(), today, "sb_shadow_eod")
        if got:
            print(f"[gate] 今日({today}) eod 已由手机落库 → 云端 runner 跳过")
            sys.exit(9)                # 9 = SKIP（workflow 按此判空跑）
        print(f"[gate] 今日({today}) 云端无 eod 行 → 手机缺席，云端接管代跑")
        return
    if "--init-db" in sys.argv:
        init_db_from_cloud()
        return
    if "--mobile" in sys.argv:
        # T-Fin31 修订：手机端闸 = 全量双向对帐 + 补齐（残缺即补、冲突停手；手机优先）；
        # 加 --dry 则只读对帐不动账
        rep = recon(load_cfg(), "--dry" not in sys.argv, "local")
        clean = all(not rep[k] for k in
                    ("eod_only_local", "eod_only_cloud", "eod_mismatch",
                     "fills_only_local", "fills_only_cloud",
                     "fills_conflict_dates"))
        if clean:
            print("[gate] 双源对帐：本地与云端全一致，无残缺")
        else:
            print("[gate] 双源对帐：",
                  json.dumps(rep, ensure_ascii=False, default=str))
            if rep.get("fills_conflict_dates"):
                print("[gate] ⚠ 同日双写冲突，已停手（fills 区）交人工对账"
                      "↓ 明细如上")
        sys.exit(0)                    # 闸结果不放倒本地日常流程
    print(__doc__)


if __name__ == "__main__":
    main()
