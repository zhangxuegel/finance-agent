# -*- coding: utf-8 -*-
"""shadow_broker.py —— 影子交易撮合引擎（T-Fin05 模块 2）

角色：把 ensemble_engine 的"信号日收盘生成、次日开盘撮合"决策搬到真实时间轴上：
- 台账源头：`EnsembleEngine.export_plan()`（只用 ≤ 上一个台账交易日收盘数据）
- 撮合价格：ativos 真实行情快照（live_data_feed 实时接口，09:30 盘口）
- 台账库：data/shadow_live.db（sqlite3：fills / eod / events 三表）
- 熔断继承：risk_control.check_breakers 绝对规则（单日-3% / 峰值-10%+锁3日）
- 跌停红线：开盘即跌停的标的当日**禁止买入**（不伪造流动性；卖出仍按开盘价）

影子状态：data/shadow_state.json（与 ensemble_state.json 一模一样的结构，
独立演化于真实成交价——首日从 ensemble 台账克隆持仓，此后逐日随真实价格分叉）。

时间锁：撮合只允许在北京时间交易日 [09:30, 15:00) 内或 --replay 验收模式下执行；
盘中绝不引用当日收盘（快照里 close 字段全部按"临时=开盘价"记账，EOD 阶段才以
真实收盘重估净值）。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import ensemble_engine as ee
import fetch_data
import live_data_feed as feed
import portfolio
import risk_control

DB = os.path.join(ROOT, "data", "shadow_live.db")
STATE = os.path.join(ROOT, "data", "shadow_state.json")
ENS_STATE = os.path.join(ROOT, "data", "ensemble_state.json")
PLAN = os.path.join(ROOT, "data", "shadow_plan.json")
REPORTS = os.path.join(ROOT, "reports")
LIVE_DIR = os.path.join(ROOT, "data", "live")
IDX_CSV = os.path.join(LIVE_DIR, "000300.csv")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, trade_date TEXT, leg TEXT, code TEXT, side TEXT,
  shares INTEGER, price REAL, fee REAL, reason TEXT,
  limit_blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS eod (
  trade_date TEXT PRIMARY KEY, nav REAL, nav_prev REAL, day_ret REAL,
  bench_index_close REAL, bench_index_pct REAL,
  alpha REAL, defense INTEGER, fills_n INTEGER, lock_remaining INTEGER,
  notes TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, detail TEXT);
"""


# ---------------------------------------------------------------------------
def db():
    if DB != ":memory:":
        os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    return conn


def db_event(kind: str, detail: str):
    try:
        conn = db()
        conn.execute("INSERT INTO events(ts, kind, detail) VALUES (?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"), kind,
                      detail))
        conn.commit()
        conn.close()
    except Exception as e:  # noqa: BLE001 —— 事件库失败只进错误日志
        feed.log_error("db_event", f"{kind}: {e}")


def sh_date() -> str:
    return feed.sh_now().strftime("%F")


def clock_ok(now, replay: bool) -> bool:
    """实盘时间锁：撮合只允许交易日 09:30-15:00（replay 验收模式豁免）。"""
    if replay:
        return True
    t = now.strftime("%H:%M")
    if now.weekday() >= 5:
        return False
    return "09:30" <= t < "15:00"


def load_shadow_state(p: dict) -> dict | None:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def bootstrap_from_ensemble(p: dict, eng) -> dict:
    """首日播种：从 ensemble_state 克隆持仓/现金/权重（注明 source）。
    之后影子台账独立演化于真实成交价。"""
    with open(ENS_STATE, encoding="utf-8") as f:
        ens = json.load(f)
    st = {"master_cash": ens.get("master_cash", 0.0),
          "money": dict(ens.get("money", {"A": 0, "B": 0, "MASTER": 0,
                                          "last_px": None})),
          "sleeves": {leg: {"cash": ens["sleeves"][leg]["cash"],
                            "positions": json.loads(json.dumps(
                                ens["sleeves"][leg]["positions"])),
                            "trades": [], "curve": []} for leg in ("A", "B")},
          "weight_a": ens.get("weight_a", 0.5),
          "c_buffer": ens.get("c_buffer", 0.0),
          "trades": [], "curve": [], "alarms": [],
          "locked_remaining": 0, "race_log": [],
          "seeded_from": {"date": ens.get("last_date"),
                          "nav": ens.get("curve", [])[-1][1]
                          if ens.get("curve") else None}}
    eq = st["curve"][-1][1] if st["curve"] else None
    if eq is None or eq <= 0:
        eq = compute_equity(st, {}, p, eng)
        st["curve"] = [[ens.get("last_date", sh_date()), round(eq, 2)]]
    return st


def compute_equity(st: dict, px_close: dict, p: dict, eng=None) -> float:
    """合并净值 = master_cash + Σ(腿现金持仓) + 货币份额×现价。"""
    leg_eq = 0.0
    for leg in ("A", "B"):
        port = st["sleeves"][leg]
        val = port["cash"]
        for c, pp in port["positions"].items():
            px = px_close.get(c) or (eng.by_date[c][px_close["_date"]]["close"]
                                     if eng and px_close.get("_date")
                                     and c in eng.by_date
                                     and px_close["_date"] in eng.by_date[c]
                                     else pp["cost"])
            val += pp["shares"] * px
        leg_eq += val
    money = sum(st["money"].get(k, 0) for k in ("A", "B", "MASTER"))
    mpx = (px_close.get(eng.money_code) if eng else None) or \
        st["money"].get("last_px") or 0.0
    return round(st["master_cash"] + leg_eq + money * mpx, 2)


# ---------------------------------------------------------------------------
# 撮合三阶段
# ---------------------------------------------------------------------------
def stage_plan(replay: bool = False, verbose: bool = True) -> dict:
    """08:45 喊单（预案）：只基于台账 ≤ 上一交易日收盘 → 绝不触碰时段快照的
    当日盘中数据（比时间锁更强的"计划隔离"）。"""
    eng = ee.EnsembleEngine()
    plan = eng.export_plan()
    ens_state = json.load(open(ENS_STATE, encoding="utf-8"))
    last_race = [r for r in ens_state.get("race_log", [])
                 if r["week_end"] == plan["prev_date"]]
    plan["race_pending"] = bool(last_race)
    if last_race:
        plan["target_weights"] = {"weight_a": last_race[-1]["weight_a"],
                                  "weight_b": last_race[-1]["weight_b"],
                                  "c_buffer": last_race[-1]["c_buffer"]}
    plan["trade_date"] = sh_date()
    plan["mode"] = feed.current_mode()
    plan["engine_gate"] = ens_state.get("gate_state", {})
    with open(PLAN, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1, default=str)
    if verbose:
        print(f"[plan] {plan['trade_date']} 防守={plan['defense']} "
              f"({plan['defense_reason']}) 灯={plan['light']} "
              f"赛马待执行={plan['race_pending']} 模式={plan['mode']['mode']}")
    return plan


def _inject_live_prices(eng, snap: dict, trade_date: str) -> dict:
    """把当日真实开盘价注入引擎 actuator：
    by_date/code[trade_date] 与 money_px[trade_date]（影子撮合专用，
    **不落盘到 data/*.csv**——收盘台账由 ledger 阶段经 kline 收口）。"""
    quotes = snap["quotes"]
    p = eng.params
    for code in eng.core:
        q = quotes.get(code)
        if not q:
            continue
        bar_open = float(q["open"])
        eng.by_date[code][trade_date] = {
            "date": trade_date, "open": bar_open, "close": bar_open,
            "high": float(q["high"]), "low": float(q["low"]),
            "volume": float(q["volume"] or 0), "amount": 0.0}
    mq = quotes.get(eng.money_code)
    if mq:
        eng.money_px[trade_date] = float(mq["open"])
    return eng


def stage_match(replay: bool = False, verbose: bool = True) -> dict:
    """09:30 开盘撮合：真实时价 × 引擎方法口 accounting（铁律：次日开盘价）。"""
    now = feed.sh_now()
    trade_date = sh_date()
    if not clock_ok(now, replay):
        feed.log_error("time_lock", f"撮合被时间锁拒绝 clock={now}")
        return {"blocked_by_time_lock": True}
    snap = feed.heartbeat(replay=replay, verbose=False)
    mode = feed.current_mode()
    p = portfolio.load_params()
    st = load_shadow_state(p)
    eng = ee.EnsembleEngine()
    if st is None:
        st = bootstrap_from_ensemble(p, eng)
    plan = json.load(open(PLAN, encoding="utf-8")) \
        if os.path.exists(PLAN) else stage_plan(replay, verbose=False)
    if plan.get("trade_date") != trade_date:
        plan = stage_plan(replay, verbose=False)
    st["_door"] = {"defense": bool(plan.get("defense"))}   # 门在下方确定后回填
    _inject_live_prices(eng, snap, trade_date)

    conn = db()
    fills = []

    def plan_door():
        # 跨日锁仓递减：每处理一个新交易日，锁计数减 1
        if st.get("locked_remaining", 0) > 0:
            return "lock"
        if mode.get("mode") == "degraded":
            return "degrade"      # 数据异常：当日放弃开新交易（保持现状）
        if plan["defense"]:
            return "defense"
        return "normal"

    done = plan_door()
    if done == "lock":
        pass   # 无任何动作（锁仓日保持空仓/货币评分不变）
    elif done == "degrade":
        db_event("degrade_match",
                 "降级模式：放弃开新交易，保持现有臂位（不强制清仓）")
    elif done == "defense":
        if any(st["sleeves"][l]["positions"] for l in ("A", "B")):
            for o in eng.clear_sleeves(st, trade_date, "影子:defense regime"):
                fills.append(_fill_row(o, trade_date))
        eng.park_money(st, trade_date)
    else:
        # 正常出闸：赛马待执行 → 赎回货币+按权重换预算；随后腿策略
        if plan.get("race_pending"):
            held = sum(st["money"].get(k, 0) for k in ("A", "B", "MASTER")) > 0
            if held:
                eng.redeem_money(st, trade_date)
            w = plan["target_weights"]
            eng.rebalance_sleeves(st, w, trade_date)
            for o in [t for t in st["trades"]
                      if t["date"] == trade_date
                      and t.get("side") in ("FUND", "DRAIN")]:
                fills.append(_fill_row(o, trade_date))
        held_now = sum(st["money"].get(k, 0) for k in ("A", "B", "MASTER")) > 0
        if held_now:
            eng.redeem_money(st, trade_date)
        # 腿信号（引擎给的 prev_sig）★跌停不买入★
        sig = json.loads(json.dumps(plan["prev_sig"]))
        blocked = {}
        for code in eng.core:
            q = snap["quotes"].get(code)
            if q and feed.is_limit_down_open(q):
                blocked[code] = (float(q["open"]),
                                 feed.limit_down_price(float(q["prev_close"])))
                for leg in ("A", "B"):
                    if sig[leg].get(code) == 1:
                        sig[leg][code] = 0.0
        if blocked:
            db_event("limit_down_block",
                     f"跌停不买入：{sorted(blocked)} (开盘=跌停价)")
        for leg in ("A", "B"):
            day_price = {c: eng.by_date[c][trade_date] for c in eng.core
                         if trade_date in eng.by_date[c]}
            orders = portfolio.place_orders(st["sleeves"][leg], day_price,
                                            sig[leg], eng.sleeve_params(),
                                            trade_date)
            for o in orders:
                o["leg"] = leg
                o["reason"] = "shadow-place_orders"
                fills.append(_fill_row(o, trade_date))
        eng.park_money(st, trade_date)
    # 尾差停泊台账（每日必有，含防守日）
    eng.park_money(st, trade_date)

    for f in fills:
        conn.execute("INSERT INTO fills(ts, trade_date, leg, code, side, "
                     "shares, price, fee, reason, limit_blocked) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"),
                      trade_date, f["leg"], f.get("code"),
                      f["side"], f.get("shares"), f.get("price"),
                      f.get("fee"), f.get("reason"),
                      1 if f.get("code") in blocked else 0))
    conn.commit()
    conn.close()
    st["_door"]["door"] = done
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, default=str)
    res = {"trade_date": trade_date, "door": done, "fills": len(fills),
           "blocked": blocked if done == "normal" else {}}
    if verbose:
        print(f"[match] 门={done} fills={len(fills)} blocked={list(res['blocked'])}")
    _push(replay, "shadow_0930",
          f"09:30 开盘撮合完成：门={done}，成交 {len(fills)} 笔"
          + (f"；跌停拦截 {list(res['blocked'])}" if res["blocked"] else ""))
    st.pop("_tmp", None)
    return res


def o_marker(t):
    return t.get("side") in ("FUND", "DRAIN")


def _fill_row(o, trade_date):
    return {"trade_date": trade_date, "leg": o.get("leg", ""),
            "code": o.get("code"), "side": o["side"],
            "shares": o.get("shares") or 0,
            "price": o.get("price") or 0.0, "fee": o.get("fee") or 0.0,
            "reason": o.get("reason", "")}


def _push(replay, subject, text):
    """推送通道（mock 闭环在验收 replay 中是正式出口，双保险防误发）。"""
    try:
        import push_notification as push
        ok, info = push.push_to_params(f"[{subject}] {text}")
        feed.log_error("push", f"{subject} → ok={ok} {info}") if not ok else None
    except Exception as e:  # noqa: BLE001
        feed.log_error("push", f"{subject}: {e}")


# ---------------------------------------------------------------------------
def _kbd_line(section: str) -> str:
    return f"- 台账：{section}"


def stage_ledger(replay: bool = False, verbose: bool = True) -> dict:
    """15:05 收盘落库：真实 EOHLCV（腾讯日线）→ data/live/*.csv + EOD 行。
    并执行熔断继承（收盘后校验，触发则次日锁仓开始倒计时）。"""
    now = feed.sh_now()
    if not replay and now.strftime("%H:%M") < "15:00":
        feed.log_error("time_lock", f"ledger 在 15:00 前被拒绝 clock={now}")
        return {"blocked_by_time_lock": True}
    trade_date = sh_date()
    p = portfolio.load_params()
    st = load_shadow_state(p)
    if st is None:
        stage_match(replay, verbose=False)
        st = load_shadow_state(p)
    eng = ee.EnsembleEngine()
    bench_idx = "000300"
    # ① 真实日线末根（4 核心 ETF + 货币腿 + 指数）→ live CSV 增量落盘
    kmaps = {}
    for code in [c for c in [w["code"] for w in fetch_data.load_watchlist()]
                 + [p["ensemble"]["C"]["code"], p["benchmark"]["code"]]]:
        kmaps[code] = _ensure_live_csv(code, trade_date)
    idx_row = _ensure_index_csv(trade_date)
    # ② 用真实收盘重估影子净值（替换 09:30 的"临时=开盘价"估值）
    closes = {c: kmaps[c][-1]["close"] for c in kmaps}
    equity = compute_equity(st, {**closes, "_date": trade_date}, p, eng)
    curve_replace(st, trade_date, equity)
    prev_eq = st["curve"][-2][1] if len(st["curve"]) > 1 else equity
    # ③ 指数与 Alpha
    idx_close, idx_prev = idx_row["close"], idx_row["prev_close"]
    bench_pct = (idx_close / idx_prev - 1) if idx_prev else 0.0
    day_ret = equity / prev_eq - 1 if prev_eq else 0.0
    alpha = day_ret - bench_pct
    # ④ 熔断继承（绝对规则）
    conn = db()
    fills_n = conn.execute("SELECT COUNT(*) FROM fills WHERE trade_date=?",
                           (trade_date,)).fetchone()[0]
    chk = risk_control.check_breakers(len(st["curve"]), trade_date, equity,
                                      prev_eq, st["curve"][:-1], p["risk"])
    if chk["triggered"] and st.get("locked_remaining", 0) == 0:
        # 熔断：按真实收盘强平 + 次日起锁 3 交易日（计数式，交易日递减）
        _shadow_force_liquidate(st, kmaps, trade_date, chk["reason"], eng)
        st["locked_remaining"] = int(p["risk"]["lock_days"])
        st["alarms"].append({"date": trade_date,
                             "type": "breaker", "reason": chk["reason"]})
        db_event("breaker", chk["reason"])
    elif st.get("locked_remaining", 0) > 0:
        st["locked_remaining"] = st["locked_remaining"] - 1  # 锁满自然递减
    fills_n = conn.execute("SELECT COUNT(*) FROM fills WHERE trade_date=?",
                           (trade_date,)).fetchone()[0]   # 含熔断强平笔
    conn.execute("REPLACE INTO eod(trade_date, nav, nav_prev, day_ret, "
                 "bench_index_close, bench_index_pct, alpha, defense, "
                 "fills_n, lock_remaining, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (trade_date, equity, prev_eq, day_ret, idx_close, bench_pct,
                  alpha, 1 if plan_defense(st) else 0, fills_n,
                  st.get("locked_remaining", 0),
                  json.dumps({"fills": fills_n}, ensure_ascii=False)))
    conn.commit()
    conn.close()
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, default=str)
    res = {"trade_date": trade_date, "nav": equity, "day_ret": day_ret,
           "bench_pct": bench_pct, "alpha": alpha, "fills_n": fills_n,
           "breaker": chk["triggered"]}
    if verbose:
        print(f"[ledger] EOD {trade_date}: NAV ¥{equity:,.0f} "
              f"({day_ret * 100:+.2f}%) vs 沪深300 {bench_pct * 100:+.2f}% "
              f"→ Alpha {alpha * 100:+.2f}pp fills={fills_n} "
              f"breaker={chk['triggered']}")
    return res


def plan_defense(st: dict) -> bool:
    """当日是否防守（match 阶段写入 st["_door"]）"""
    return (st.get("_door") or {}).get("defense", False)


def curve_replace(st: dict, date: str, eq: float):
    rows = [r for r in st["curve"] if r[0] != date] + [[date, round(eq, 2)]]
    rows.sort()
    st["curve"] = rows


def _shadow_force_liquidate(st, kmaps, trade_date, reason, eng):
    p = eng.params
    slip = p["fees"]["slippage_bps"] / 10000
    for leg in ("A", "B"):
        port = st["sleeves"][leg]
        for code in list(port["positions"]):
            pos = port["positions"][code]
            px = kmaps[code][-1]["close"] * (1 - slip)
            notional = pos["shares"] * px
            fee = portfolio.trade_fees(notional, "SELL", p)
            port["cash"] += notional - fee
            port["trades"].append({"date": trade_date, "code": code,
                                   "side": "SELL", "shares": pos["shares"],
                                   "price": round(px, 4),
                                   "fee": round(fee, 2), "forced": True})
            del port["positions"][code]
            _db_insert_fill({"leg": leg, "code": code, "side": "SELL",
                             "shares": pos["shares"],
                             "price": round(px, 4), "fee": round(fee, 2),
                             "reason": f"熔断强平:{reason}"}, trade_date)


# ==== 实盘台账 CSV（data/live/*.csv = 正式台账 CSV + 逐日真实 EOD 增量）====
def _upsert_daily_csv(code: str, secid: str, path: str,
                      trade_date: str) -> list[dict]:
    """确保 CSV 含 trade_date 的真实日线；缺 → 从 kline 接口抓最近 5 根补。
    首次建库则对齐正式台账全量（口径同源，注明）。返回更新后的全部 rows。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import csv as _csv
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rows = [dict(r) for r in _csv.DictReader(f)]
        for r in rows:
            for k in ("open", "close", "high", "low", "volume", "amount"):
                r[k] = float(r[k] or 0)
    if not rows and code.startswith("5") or (not rows and len(code) == 6):
        # 首次建库：正式台账全量复制（口径同源台账，随后逐日真实增量）
        try:
            rows = list(fetch_data.load_csv(code))
        except Exception as e:  # noqa: BLE001
            feed.log_error("live_seed", f"{code}: {e}")
    if not rows or rows[-1]["date"] < trade_date:
        # fetch_klines 要求 ≥30 条（交易日口径）→ 最小 60 自然日窗口
        fresh = fetch_data.fetch_klines(secid, natural_days=60)
        for k in fresh:
            if any(r["date"] == k["date"] for r in rows):
                continue
            rows.append(k)
    rows = [r for r in sorted(rows, key=lambda x: x["date"])
            if r["date"] < trade_date or r["date"] == trade_date]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["date", "open", "close",
                                           "high", "low", "volume", "amount"])
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{r[k]:g}" if isinstance(r[k], float) else r[k])
                        for k in w.fieldnames})
    if rows and rows[-1]["date"] != trade_date:
        feed.log_error("eod_gap", f"{code}: {trade_date} 行情未出，"
                        f"EOD 以最近一日 {rows[-1]['date']} 收盘估值")
    return rows


def _ensure_live_csv(code: str, trade_date: str) -> list[dict]:
    import fetch_data as _fd
    # secid：东财口径 沪=1.x / 深=0.x；watchlist 已登记的直接复用（台账溯源）
    secid = next((w["secid"] for w in _fd.load_watchlist()
                  if w["code"] == code), None)
    secid = secid or (("1." if code.startswith("5") else "0.") + code)
    return _upsert_daily_csv(code, secid,
                             os.path.join(LIVE_DIR, f"{code}.csv"),
                             trade_date)


def _ensure_index_csv(trade_date: str) -> dict:
    """沪深300 指数日线台账 data/live/000300.csv（东财 secid 1.000300）。"""
    if not os.path.exists(IDX_CSV):
        rows = fetch_data.fetch_klines("1.000300",
                                       natural_days=fetch_data.DEFAULT_YEARS
                                       * fetch_data.NATURAL_PER_YEAR)
        _write_rows_csv(IDX_CSV, rows)
    rows = _upsert_daily_csv("000300", "1.000300", IDX_CSV, trade_date)
    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else last
    return {"code": "000300", "close": last["close"],
            "prev_close": prev["close"], "date": last["date"]}


def _write_rows_csv(path: str, rows: list[dict]):
    import csv as _csv
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["date", "open", "close", "high",
                                           "low", "volume", "amount"])
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{r[k]:g}" if isinstance(r.get(k), float)
                            else r.get(k, 0)) for k in w.fieldnames})


def _db_insert_fill(row: dict, trade_date: str):
    try:
        conn = db()
        conn.execute("INSERT INTO fills(ts, trade_date, leg, code, side, "
                     "shares, price, fee, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"),
                      trade_date, row.get("leg"), row.get("code"),
                      row["side"], row.get("shares"), row.get("price"),
                      row.get("fee"), row.get("reason")))
        conn.commit()
        conn.close()
    except Exception as e:  # noqa: BLE001
        feed.log_error("db_fill", f"{row}: {e}")


# ==== 15:10 复盘推送 ====
def stage_review(replay: bool = False, verbose: bool = True) -> str:
    trade_date = sh_date()
    conn = db()
    fills = _db_rows(conn, "SELECT side, code, shares, price, fee, reason, "
                     "leg FROM fills WHERE trade_date=?", (trade_date,))
    eod = _db_rows(conn, "SELECT * FROM eod WHERE trade_date=?", (trade_date,))
    conn.close()
    e = eod[0] if eod else {}
    lines = [f"# 影子实盘复盘 · {trade_date}", "",
             f"> 生成 {datetime.now():%F %T}（北京时间）· 数字全部来自"
             f" data/shadow_live.db 与 data/live/*.csv 台账", "",
             f"- 净值：¥{e.get('nav', 0):,.2f}（{(e.get('day_ret') or 0) * 100:+.2f}%）",
             f"- 沪深300：{(e.get('bench_index_pct') or 0) * 100:+.2f}%，"
             f"**影子 Alpha {(e.get('alpha') or 0) * 100:+.2f}pp**",
             f"- 当日成交 {e.get('fills_n', 0)} 笔 · 防守日={bool(e.get('defense'))}"
             f" · 锁仓剩 {e.get('lock_remaining', 0)} 日"]
    if fills:
        lines += ["", "| 腿 | 操作 | 标的 | 数量 | 价格 | 费 | 依据 |",
                  "|---|---|---|---|---|---|---|"]
        for f in fills:
            lines.append(f"| {f['leg']} | {f['side']} | {f['code']} | "
                         f"{f['shares']:,} | {f['price']} | ¥{f['fee']:.2f} | "
                         f"{f['reason']} |")
    if not fills:
        lines += ["", "（当日无成交——防守/锁仓/降级门拦截，符合铁律）"]
    lines += ["", "### 异动与事件", "",
              "- 事件台账：`data/shadow_live.db.events`；错误日志："
              "`logs/live_error.log`（抓取/解析异常也在此留痕）", "",
              _kbd_line("data/shadow_live.db（fills/eod/events）+ "
                        "data/shadow_state.json + data/live/*.csv")]
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"shadow_review_{trade_date}.md")
    with open(path, "w", encoding="utf-8") as fl:
        fl.write("\n".join(lines) + "\n")
    text = (f"15:10 影子复盘 NAV ¥{e.get('nav', 0):,.0f} "
            f"({(e.get('day_ret') or 0) * 100:+.2f}%) vs 沪深300 "
            f"{(e.get('bench_index_pct') or 0) * 100:+.2f}% "
            f"→ Alpha {(e.get('alpha') or 0) * 100:+.2f}pp；"
            f"成交 {e.get('fills_n', 0)} 笔")
    if replay:
        push_text = "[影子复盘-回放验收] " + text
    else:
        push_text = "[影子复盘] " + text
    ok, info = _try_push(push_text)
    if verbose:
        print(f"[review] → {path} push(ok={ok}, {info})")
    return path


def _db_rows(conn, q, args=(), as_dict=True):
    cur = conn.execute(q, args)
    if not as_dict:
        return cur.fetchall()
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _try_push(text) -> tuple[bool, str]:
    try:
        import push_notification as push
        return push.push_to_params(text)
    except Exception as e:  # noqa: BLE001
        feed.log_error("review_push", str(e))
        return False, f"push 异常 {e}"


# ---------------------------------------------------------------------------
# 无网络自测（铁律演练：跌停不买/时间锁/降级门/台账库读写）
def self_test() -> int:
    fails = []

    def chk(name, cond, detail=""):
        print(("✅ PASS " if cond else "❌ FAIL "), name, detail)
        if not cond:
            fails.append(name)

    # 1) 跌停不买入判定
    q_dn = {"prev_close": 1.000, "open": 0.900}
    q_ok = {"prev_close": 1.000, "open": 0.905}
    chk("S1 开盘即跌停 → 拦截", feed.is_limit_down_open(q_dn))
    chk("S2 未至跌停 → 放行", not feed.is_limit_down_open(q_ok))
    # 2) 时间锁
    import datetime as _dt
    tue_0920 = _dt.datetime(2026, 10, 20, 9, 20)
    tue_0931 = _dt.datetime(2026, 10, 20, 9, 31)
    tue_1501 = _dt.datetime(2026, 10, 20, 15, 1)
    sat_1000 = _dt.datetime(2026, 10, 24, 10, 0)
    chk("S3 09:20 撮合被拒", not clock_ok(tue_0920, False))
    chk("S4 09:31 撮合放行", clock_ok(tue_0931, False))
    chk("S5 15:01 撮合被拒", not clock_ok(tue_1501, False))
    chk("S6 周六撮合被拒", not clock_ok(sat_1000, False))
    chk("S7 replay 豁免时间锁", clock_ok(sat_1000, True))
    # 3) 台账库读写（临时文件库）
    import tempfile
    global DB
    tmp_db = os.path.join(tempfile.gettempdir(), "shadow_selftest.db")
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    old_db = DB
    DB = tmp_db
    try:
        conn = db()
        _db_insert_fill({"leg": "A", "code": "510300", "side": "BUY",
                         "shares": 100, "price": 4.2, "fee": 1.2,
                         "reason": "test"}, "2026-10-20")
        rows = _db_rows(conn, "SELECT * FROM fills")
        chk("S8 fills 落库可读回", len(rows) == 1 and
            rows[0]["code"] == "510300")
        conn.close()
    except Exception as e:  # noqa: BLE001
        chk("S8 fills 落库可读回", False, str(e))
    finally:
        DB = old_db
    # 4) 腾讯行情解析器（canned payload，无网络、字段位精确构造）
    f = [""] * 40
    f[1], f[2], f[3], f[4], f[5], f[6] = ("沪深300ETF华泰柏瑞", "510300",
                                          "4.240", "4.236", "4.224",
                                          "12105648")
    f[30] = "20261019161439"
    f[31], f[32], f[33], f[34] = "0.004", "0.09", "4.267", "4.200"
    q = feed._parse_tencent("sh510300", 'v_sh510300="' + "~".join(f) + '";')
    chk("S9 行情解析器字段", bool(q and q["last"] == 4.240 and
                                  q["open"] == 4.224 and
                                  q["ts"].startswith("2026-10-19")))
    chk("S10 畸形 payload → None", feed._parse_tencent("sh510300", 'v_sh510300="1~ab"') is None)
    # 5) 降级门/防守语义
    stx = {}
    stx["_door"] = {"defense": True}
    chk("S11 _door 防守语义", plan_defense(stx) is True)
    stx["_door"] = {"defense": False}
    chk("S12 _door 非防守语义", plan_defense(stx) is False)
    # 6) 撮合引擎合成用例（真实 place_orders 数学，无网络）：
    #    昨日信号全 1 + 今日开盘快照 → 应产生 BUY 成交；
    #    其一开盘即跌停 → 该标的被拦（信号置 0，无 BUY）。
    try:
        import portfolio as _pf
        from ensemble_engine import EnsembleEngine
        eng = EnsembleEngine()
        px = 10.0
        snap_quotes = {c: {"open": px, "prev_close": px, "high": px,
                           "low": px, "volume": 1000,
                           "ts": "2026-10-20 09:30:00"} for c in eng.core}
        snap_quotes[eng.core[1]]["open"] = 9.00      # 昨收10 → 跌停 9.00
        sig2 = {c: 1.0 for c in eng.core}
        blocked2 = [c for c in eng.core
                    if feed.is_limit_down_open(snap_quotes[c])]
        for c in blocked2:
            sig2[c] = 0.0
        td = "2026-10-20"
        day_price = {c: {"date": td, "open": snap_quotes[c]["open"],
                         "close": snap_quotes[c]["open"],
                         "high": snap_quotes[c]["high"],
                         "low": snap_quotes[c]["low"]}
                     for c in eng.core}
        st2 = {"master_cash": 200000.0,
               "money": {"A": 0, "B": 0, "MASTER": 0, "last_px": None},
               "sleeves": {"A": {"cash": 100000.0, "positions": {},
                                 "trades": [], "curve": []},
                           "B": {"cash": 100000.0, "positions": {},
                                 "trades": [], "curve": []}},
               "weight_a": 0.45, "c_buffer": 0.1, "trades": [],
               "curve": [], "alarms": [], "locked_remaining": 0}
        got = []
        for leg in ("A", "B"):
            orders = _pf.place_orders(st2["sleeves"][leg], day_price,
                                      sig2, eng.sleeve_params(), td)
            got += [o for o in orders if o["side"] == "BUY"]
        chk("S13 正常标的产生 BUY", len(got) >= 2, f"n={len(got)}")
        chk("S14 跌停标的被拦（无其 BUY）",
            all(o["code"] != eng.core[1] for o in got)
            and blocked2 == [eng.core[1]])
        chk("S15 成交价带滑点上浮",
            all(o["price"] >= snap_quotes[o["code"]]["open"] for o in got))
    except Exception as e:  # noqa: BLE001
        chk("S13-15 撮合合成用例", False, str(e))
    print("=" * 46)
    if fails:
        print(f"shadow_broker --test：{len(fails)} 失败 → {fails}")
        return 1
    print("shadow_broker --test：全部通过 ✔")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="08:45 喊单预案")
    ap.add_argument("--match", action="store_true", help="09:30 开盘撮合")
    ap.add_argument("--ledger", action="store_true", help="15:05 收盘落库")
    ap.add_argument("--review", action="store_true", help="15:10 复盘推送")
    ap.add_argument("--replay", action="store_true", help="回放时钟（验收）")
    ap.add_argument("--test", action="store_true", help="无网络自测")
    a = ap.parse_args()
    if a.test:
        sys.exit(self_test())
    _stage = (stage_plan if a.plan else stage_match if a.match else
              stage_ledger if a.ledger else stage_review if a.review
              else None)
    if _stage:
        try:
            _stage(replay=a.replay)
        except Exception as e:  # noqa: BLE001 —— 任何异常都不放倒 cron
            feed.log_error("stage_fatal",
                           f"{_stage.__name__}: {type(e).__name__} {e}")
            db_event("stage_fatal",
                     f"{_stage.__name__}: {type(e).__name__} {e}")
            print(f"[shadow] {getattr(_stage, '__name__', '?')} 异常已登记 "
                  f"logs/live_error.log（cron 不倒）")
            sys.exit(0)
    else:
        print("用法：--plan/--match/--ledger/--review [--replay] / --test")


