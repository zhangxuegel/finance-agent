# -*- coding: utf-8 -*-
"""cloud_gate.py —— 云端↔本地 影子账对账闸（T-Fin35 业务规则重构版）

台账纪律（业主 2026-10-24 对账规则拍板，替代 10-21 三点的"手机优先"覆盖口径）：
  1) **阻断条件**：仅当「信息不全」（缺失必需数据/基础状态）才停跑——
     数据完整性检查由 preflight.py 负责，与对账彻底分离；
  2) **恢复与前置**：信息满足后允许继续跑，但每次流水线启动的第一件事
     必须是强制对账——无论上一轮是否对过；
  3) **执行隔离**：对账状态统一（残缺清零 + 双侧指纹相等）之前，
     绝不允许执行记账逻辑（daily_report 等）；
  4) **统一规则**：云端与本地用同一份 cloud_gate 代码与同一标准
     （残缺=一侧无该行；冲突=同键异值；统一校验=fingerprint 指纹）。
  5) **谁缺谁停**：断网/凭证缺失的一侧停止记账，另一侧不受影响照常跑。
  6) **防脏数据**：不可调和冲突（fills 同日双写异笔 / eod 同键数值打架）
     抛异常阻断记账，明细落盘；人工经 --resolve-eod 显式裁决后放行。

用法：
  python3 scripts/cloud_gate.py --gate [--wait-minutes 15] [--dry]
      # 强制对账闸（本地流水线第 1 步；有界等待重试，绝不轻言退出）
      # exit 0=对账统一（允许记账）; 2=冲突阻断; 3=对账未完成超时/凭证缺失
  python3 scripts/cloud_gate.py --resolve-eod 2026-10-22 --prefer local|cloud
      # eod 数值冲突的人工裁决工具：被裁决覆盖的一侧以裁决侧为准
  python3 scripts/cloud_gate.py --cloud-runner   # Actions 收盘流程第 0 步（跳过=exit 9）
  python3 scripts/cloud_gate.py --mobile [--dry] # 快速对帐（单轮，严格分类）
  python3 scripts/cloud_gate.py --init-db        # Actions 冷启动整卷回灌
  python3 scripts/cloud_gate.py --selftest      # 离线自测（fixture，不联网）
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
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
                ("冲突 eod 1024 → 阻断不上推（本地保自家值，不再静默覆盖）",
                 "2026-10-24" not in pushed_eod),
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
    rows_a = [{"trade_date": "2026-10-21", "nav": 976312.1, "nav_prev": 976273.3,
               "day_ret": 0.00004, "bench_index_close": 3901.2},
              {"trade_date": "2026-10-20", "nav": 976273.3, "nav_prev": 976200.0,
               "day_ret": -0.00001, "bench_index_close": 3899.9}]
    rows_b = list(reversed(rows_a))
    fp_ok = (fingerprint(rows_a, FPRINT_COLS["eod"])
             == fingerprint(rows_b, FPRINT_COLS["eod"]))
    fp_num = (fingerprint([{"trade_date": "d", "shares": 300}], ("shares",)) ==
              fingerprint([{"trade_date": "d", "shares": 300.0}], ("shares",)))
    checks = [
        ("gate upsert 首推 1 行", n1 == 1),
        ("REPLACE 复写仍 1 行（幂等）", n2 == 1 and dup == 1),
        ("复写覆盖生效（后写优先）", nav == 999999.9),
        ("load_cfg 无文件安全返回空", load_cfg().get("SUPABASE_URL")
         in (load_cfg().get("SUPABASE_URL"), None)),
        ("云端只读无凭证静默回空", cloud_rows({}, "2026-01-01",
                                        "sb_shadow_eod") == []),
        ("统一指纹：行序无关", fp_ok),
        ("统一指纹：int/float 规范一致", fp_num),
    ] + selftest_recon()
    ok = all(c for _, c in checks)
    for name, c in checks:
        print(("✅" if c else "❌"), name)
    print("cloud_gate selftest:", "全绿" if ok else "失败",
          "→", len(checks), "项")
    return 0 if ok else 1


# ---------------- 双向对账：查残缺 + 补全同步（T-Fin35 统一标准版）----------------
EOD_EQ_COLS = ("nav", "nav_prev", "day_ret", "bench_index_close")
EPS = 1e-4                      # 浮点尾差容忍（台账分位）

# 统一校验指纹的投影列（规则4：双侧同一标准；只比共同键值，列序无关）
FPRINT_COLS = {
    "eod": ("trade_date",) + EOD_EQ_COLS,
    "fills": ("trade_date", "id", "leg", "code", "side",
              "shares", "price", "fee", "reason"),
}


def _canon_v(v):
    """指纹/比较的数值规范化：int/float 统一发到 6 位小数（台账分位以下噪声不计）。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v) + 0.0, 6)
    return str(v)


def fingerprint(rows: list, cols: tuple) -> str:
    """统一校验指纹 = sha256(canonical_json(投影行))；列序/行序无关。
    云端与本地各跑同一份代码同一投影——指纹相等 ⇔ 内容一致（规则4）。"""
    proj = []
    for r in rows:
        if r.get("trade_date") is None:
            continue
        proj.append({c: _canon_v(r.get(c)) for c in cols})
    proj.sort(key=lambda d: tuple(json.dumps(v, sort_keys=True, ensure_ascii=False)
                                  for v in d.values()))
    blob = json.dumps(proj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def table_fingerprint(table: str, cfg: dict) -> tuple[str, str]:
    """返回 (local_fp, cloud_fp)；投影列见 FPRINT_COLS。"""
    cols = FPRINT_COLS["eod" if table == "eod" else "fills"]
    ctable = f"sb_shadow_{table}"
    lf = fingerprint(local_rows(table), cols)
    cf = fingerprint(cloud_all_paged(cfg, ctable, order=(
        "trade_date.asc.id.asc" if table == "fills" else "trade_date.asc")), cols)
    return lf, cf


def cloud_alive(cfg: dict) -> None:
    """云端可达性探针：不可达抛异常（与「静默回空」区分，防误判为对账一致）。"""
    if not cloud_rows(cfg, "1990-01-01", "sb_shadow_eod"):
        raise ConnectionError("云端只读探针失败（网络/凭证）")


REPO = "zhangxuegel/finance-agent"      # 公共仓库 Actions 状态探测（无凭证）


def cloud_busy() -> bool:
    """云端 workflow 正在跑 → 视为对账未完成（等其收口，不与其抢写）。"""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/workflows/"
            "daily_close.yml/runs?per_page=1")
        with urllib.request.urlopen(req, timeout=15) as r:
            run = json.loads(r.read() or "{}").get("workflow_runs", [{}])[0]
        return (run.get("status") in ("in_progress", "queued", "waiting",
                                      "pending"))
    except Exception:  # noqa: BLE001 —— 探测失败不阻断对账（靠对账本身收敛判定）
        return False


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
    """T-Fin31 缺陷修复（T-Fin32）：24c5e45 欲在云端环境跳过对账，但把
    本地路径一并改成 return None → --mobile 闸 TypeError 崩、对账闸形同虚设。
    修正：仅 GITHUB_ACTIONS 环境跳过；本地一律派发 recon_original。"""
    import os
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("云端环境，跳过对账")
        return None
    return recon_original(*args, **kwargs)

def recon_original(cfg: dict, fix: bool = False, prefer: str = "local") -> dict:
    """全量对账 +（--fix 时）双向补齐。口径（T-Fin35 统一标准）：
    残缺 = 一侧无该行 → 双向幂等补齐（eod REPLACE / fills 按签名去重）；
    冲突 = eod 同键数值不一致 或 fills 同日双写异笔 → 一律不猜、列明细停手，
    交人工 --resolve-eod 裁决（规则4/6；prefer 仅供裁决工具使用）。"""
    rc = Recon(cfg)
    fg, conflict = rc.fills_gaps()
    report = rc.eod_gaps() | fg
    report["prefer"] = prefer
    if not fix:
        report["fixed"] = False
        return report
    # 1) eod 残缺：双向补齐（REPLACE 幂等）
    miss_local = [rc.cdate[d] for d in report["eod_only_cloud"]]
    miss_cloud = [rc.ldate[d] for d in report["eod_only_local"]]
    if miss_local:
        with sqlite3.connect(DB) as conn:
            report["fixed_eod_backfilled"] = upsert_sqlite(conn, "eod", miss_local)
    if miss_cloud:
        report["fixed_eod_pushed"] = push_rows(rc.cfg, "sb_shadow_eod",
                                               miss_cloud, "trade_date")
    # 2) eod 数值冲突：不再按 prefer 静默覆盖（T-Fin35 阻断口径），留待人工裁决
    if report["eod_mismatch"]:
        report["eod_mismatch_blocked"] = [m["date"] for m in report["eod_mismatch"]]
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


def resolve_eod(day: str, prefer: str) -> int:
    """--resolve-eod：eod 数值冲突的人工裁决（规则2显式逃生门）。
    prefer=local → 以本地为准覆写云端；prefer=cloud → 以云端为准覆写本地。
    只动该一行的对侧，绝不触碰其他日期；裁决后复检并打印指纹。"""
    cfg = load_cfg()
    cloud_alive(cfg)
    rc = Recon(cfg)
    if day not in rc.ldate or day not in rc.cdate:
        print(f"[resolve] {day} 并非双侧共同行（本地={day in rc.ldate}，"
              f"云端={day in rc.cdate}）→ 若仅一侧缺失属残缺，--gate 已可幂等补齐")
        return 1
    row = dict(rc.ldate[day] if prefer == "local" else rc.cdate[day])
    if prefer == "local":
        n = push_rows(cfg, "sb_shadow_eod", [row], "trade_date")
        print(f"[resolve] {day} → 已以本地值覆写云端行（push {n}）")
    else:
        with sqlite3.connect(DB) as conn:
            n = upsert_sqlite(conn, "eod", [row])
        print(f"[resolve] {day} → 已以云端值覆写本地行（{n}）")
    # 裁决终检：复对 + 指纹
    rep = recon_original(cfg, fix=False, prefer=prefer)
    lf, cf = table_fingerprint("eod", cfg)
    code, msg = classify_report(rep, (lf, cf))
    print(f"[resolve] 裁决后复检：exit {code}（{msg}）")
    return code


def _alert(text: str, severity: str = "ERROR") -> None:
    """闸阻断/超时的严重告警推送（T-Fin35 补充拍板 #1/#2）。
    顺着 push_notification 的 webhook 双保险惯例（未配置→静默跳过）；
    告警通道故障绝不影响闸本身的判定与退出码。"""
    try:
        from push_notification import push_to_params
        ok, note = push_to_params(f"【{severity} | 对账闸 · 本地】{text}")
        print(f"[alert] {severity} 告警{'已发 ✅' if ok else '未外发'}：{note}")
    except Exception as e:  # noqa: BLE001
        print(f"[alert] ⚠ 告警通道异常（不影响闸判定）：{e}")


ERROR_BANNER = ("="*58)


def _error_conflict(detail: str) -> None:
    """§规则2 补充拍板：同键异值/双写冲突的 ERROR 级日志——必须讲清
    (a) 记账已被阻断 (b) 无人值守的下轮会持续卡死 (c) 人工恢复命令。"""
    print(f"[ERROR] {ERROR_BANNER}")
    print("[ERROR] ⛔ 不可调和冲突（同键异值/同日双写）→ 记账已被阻断")
    print("[ERROR] ⚠ 此状态无法自动恢复：无人值守时后续每轮都会在这里卡死")
    print("[ERROR] 人工恢复唯一路径（裁决后放行）：")
    print("[ERROR]   python3 scripts/cloud_gate.py --resolve-eod YYYY-MM-DD \\")
    print("[ERROR]        --prefer local   # 以本地为准覆写云端（反之 --prefer cloud）")
    print("[ERROR] 冲突明细 ↓")
    print(detail)
    print(f"[ERROR] {ERROR_BANNER}")


def classify_report(rep: dict, fp_pair: tuple | None = None) -> tuple[int, str]:
    """对账结果分类（唯一判定口径，--gate 与 --mobile 共用）：
      2 = 不可调和冲突（fills 同日双写异笔 / eod 同键数值打架）→ 阻断记账
      3 = 对账未完成（仍有残缺未收敛）→ 停手等待/下轮重试
      0 = 对账统一（残缺清零；指纹给出则要求双侧相等）→ 允许记账"""
    if rep.get("fills_conflict_dates") or rep.get("eod_mismatch"):
        return 2, "不可调和冲突 → 阻断记账（--resolve-eod 显式裁决后放行）"
    if any(rep.get(k) for k in ("eod_only_local", "eod_only_cloud",
                                "fills_only_local", "fills_only_cloud")):
        return 3, "对账未完成（仍有残缺未收敛）"
    if fp_pair is not None and fp_pair[0] != fp_pair[1]:
        return 2, "指纹不一致（残缺清零但内容有差）→ 阻断记账"
    return 0, "对账统一"


def gate_run(wait_minutes: float = 15.0, dry: bool = False) -> int:
    """强制对账闸（T-Fin35）：信息已全（preflight 已过）→ 每次启动必跑。
    「信息全但对账未完成」（云端运行中/网络抖动/残缺未收敛）= 继续对，
    有界等待（--wait-minutes，缺省 15 分钟，60s 步进）而非直接退出。
    exit 0=统一(允许记账)；2=冲突阻断；3=超时/凭证缺失（本侧停跑，他侧不受影响）。"""
    cfg = load_cfg()
    if not (cfg.get("SUPABASE_URL") and cfg.get("SUPABASE_KEY")):
        note = "本侧对账前提缺失（云端凭证不可用）→ 本侧停跑；另一侧（云端/手机）不受影响"
        print(f"[ERROR] {ERROR_BANNER}")
        print(f"[ERROR] ⛔ {note}")
        print(f"[ERROR] {ERROR_BANNER}")
        _alert(note, severity="ERROR")
        return 3
    deadline = time.time() + wait_minutes * 60
    attempt = 0
    while True:
        attempt += 1
        busy = cloud_busy()
        try:
            cloud_alive(cfg)                    # 区分「真一致」与「静默空读」
        except Exception as e:  # noqa: BLE001
            print(f"[gate] 对账通信异常（第{attempt}轮）：{e}")
            busy = True                          # 无法通信 → 视为未完成
        if busy:
            print(f"[gate] 云端运行中/不可达 → 对账暂不定论（第{attempt}轮）")
        else:
            if not dry:
                recon_original(cfg, fix=True, prefer="local")  # 幂等补齐（无判定）
            rep = recon_original(cfg, fix=False, prefer="local")  # 全新复检定判
            fp_e = table_fingerprint("eod", cfg)
            fp_f = table_fingerprint("fills", cfg)
            code, msg = classify_report(rep, fp_e)
            if code != 0:
                code, msg = classify_report(rep, fp_f)
            if code == 2:                        # 冲突：ERROR 日志+告警+阻断
                _error_conflict(json.dumps(rep, ensure_ascii=False,
                                           indent=1, default=str))
                _alert("对账闸阻断：同键异值/同日双写冲突无人值守无法自动恢复，"
                       "须人工 --resolve-eod 裁决；明细见手机报错尾部",
                       severity="ERROR")
                return 2
            if code == 0:
                mode = "--dry 只读" if dry else "幂等补齐后"
                print(f"[gate] 对账统一（{mode}）：eod 指纹 {fp_e[0]}＝{fp_e[1]}，"
                      f"fills 指纹 {fp_f[0]}＝{fp_f[1]} → 允许记账")
                return 0
        if time.time() >= deadline:
            note = (f"对账未完成超时（等待 {wait_minutes:g} 分钟，第{attempt}轮）"
                    "→ 本侧停跑（他侧不受影响）；下轮 cron 将重试")
            print(f"[ERROR] {ERROR_BANNER}")
            print(f"[ERROR] ⛔ {note}")
            print("[ERROR] 若持续再现，先查云端 workflow 日志与手机网络；"
                  "恢复方式：直接重跑 run_daily_all.sh")
            print(f"[ERROR] {ERROR_BANNER}")
            _alert(note, severity="ERROR")
            return 3
        print(f"[gate] 待 60s 后重试（截止还剩 "
              f"{max(0, deadline - time.time()):.0f}s）")
        time.sleep(60)


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    today = date.today().strftime("%F")
    if "--gate" in sys.argv:
        wait = 15.0
        argv = sys.argv
        if "--wait-minutes" in argv:
            i = argv.index("--wait-minutes")
            if i + 1 >= len(argv):
                print("[gate] --wait-minutes 需要数值参数")
                sys.exit(1)
            try:
                wait = float(argv[i + 1])
            except ValueError:
                print("[gate] --wait-minutes 需要数值参数")
                sys.exit(1)
        sys.exit(gate_run(wait_minutes=wait, dry="--dry" in argv))
    if "--resolve-eod" in sys.argv:
        i = sys.argv.index("--resolve-eod")
        day = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        prefer = ("cloud" if "--prefer-cloud" in sys.argv else "local")
        if "--prefer-local" in sys.argv:
            prefer = "local"
        if not day:
            print("[resolve] 用法：--resolve-eod YYYY-MM-DD --prefer local|cloud")
            sys.exit(1)
        sys.exit(resolve_eod(day, prefer))
    if "--recon" in sys.argv:
        rep = recon(load_cfg(), "--fix" in sys.argv,
                    "cloud" if "--prefer-cloud" in sys.argv else "local")
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
        return
    if "--cloud-runner" in sys.argv:
        today_arg = date.today()
        if today_arg.weekday() >= 5:
            print("[gate] 非交易日(周末) → 云端 runner 无操作（防幻影 eod 行）")
            sys.exit(9)
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
        # T-Fin31 修订：手机端快速闸 = 全量双向对帐 + 补齐（单轮，无长等待，
        # 长等待有界重试语义归 --gate）。T-Fin35 严格分类：
        # exit 0=统一(允许记账) / 2=冲突阻断 / 3=对账未完成(残缺未收敛/不可达)。
        cfg = load_cfg()
        if not (cfg.get("SUPABASE_URL") and cfg.get("SUPABASE_KEY")):
            print("[error] 云端凭证缺失 → 本侧停跑（--mobile）；另一侧不受影响")
            _alert("对账闸凭证缺失（--mobile）→ 本侧停跑；他侧不受影响",
                   severity="ERROR")
            sys.exit(3)
        rep = recon(cfg, "--dry" not in sys.argv, "local")
        if rep is None:
            print("[mobile] 对账闸被跳过（云端环境），放行本地主链")
            sys.exit(0)
        try:
            cloud_alive(cfg)
            fp_e = table_fingerprint("eod", cfg)
            fp_f = table_fingerprint("fills", cfg)
        except Exception as e:  # noqa: BLE001
            print(f"[error] 云端不可达 → 本侧停跑；另一侧不受影响：{e}")
            _alert(f"对账闸连不上云端（--mobile）→ 本侧停跑；他侧不受影响：{e}",
                   severity="ERROR")
            sys.exit(3)
        code, msg = classify_report(rep, fp_e)
        if code != 0:
            code, msg = classify_report(rep, fp_f)
        print("[mobile] 双源对帐（严格分类）：", msg)
        if code == 2:
            _error_conflict(json.dumps(rep, ensure_ascii=False, indent=1,
                                       default=str))
            _alert("对账闸阻断：同键异值/同日双写冲突仍需人工 --resolve-eod "
                   "裁决；明细见手机报错尾部", severity="ERROR")
        elif code == 3:
            note = "对账未完成（残缺未收敛/不可达）→ 本侧停跑；他侧不受影响"
            _alert(note, severity="ERROR")
            print(json.dumps(rep, ensure_ascii=False, default=str))
        elif code == 0:
            print(f"[mobile] eod 指纹 {fp_e[0]}＝{fp_e[1]}，"
                  f"fills 指纹 {fp_f[0]}＝{fp_f[1]}")
        sys.exit(code)
    print(__doc__)


if __name__ == "__main__":
    main()
