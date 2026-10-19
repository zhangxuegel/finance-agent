# -*- coding: utf-8 -*-
"""kill_switch.py —— 紧急人工干预物理开关（T-Fin06 模块 2）

场景：战争、交易所宕机、API 数据污染等绝对不可预知的极端灾难。

用法：
  python3 scripts/kill_switch.py --panic     # 一键：A/B 全部强平（台账真实价）+ 写 PANIC_LOCK.flag
  python3 scripts/kill_switch.py --release   # 解锁：删除 flag，恢复自动运转
  python3 scripts/kill_switch.py --status    # 查看开关状态

铁律红线映射：
- 强平价格一律取台账 CSV 最后一日收盘（fetch_data 口径），加卖出滑点与手续费，
  与熔断强平同一套数学（portfolio.trade_fees），台账事件写入 shadow_live.db；
- PANIC_LOCK.flag 存在期间：撮合引擎 09:30 开盘环节直接跳过（人工锁定优先于一切信号）；
- 数据库只增不改不删：panic 只追加 fills 行与 events 行，eod/历史曲线零触碰。
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import live_data_feed as feed

FLAG = os.path.join(ROOT, "PANIC_LOCK.flag")
DB = os.path.join(ROOT, "data", "shadow_live.db")
STATE = os.path.join(ROOT, "data", "shadow_state.json")


def flag_active() -> bool:
    return os.path.exists(FLAG)


def flag_reason() -> str:
    try:
        with open(FLAG, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _live_px(code: str) -> float | None:
    """台账真实价：data/live/<code>.csv 最后一日收盘；缺 → 正式台账 CSV。"""
    import csv
    for path in (os.path.join(ROOT, "data", "live", f"{code}.csv"),
                 os.path.join(ROOT, "data", f"{code}.csv")):
        try:
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if rows:
                return float(rows[-1]["close"])
        except (OSError, KeyError, ValueError):
            continue
    return None


def _db_row(sql: str, args=()):
    import sqlite3
    conn = sqlite3.connect(DB)
    row = conn.execute(sql, args).fetchone()
    conn.close()
    return row


def _db_exec(sql: str, args=()):
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _event(kind: str, detail: str):
    try:
        _db_exec("INSERT INTO events(ts, kind, detail) VALUES (?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), kind, detail))
    except Exception as e:  # noqa: BLE001
        feed.log_error("kill_db", f"{kind}: {e}")


def do_panic(reason: str = "") -> dict:
    now = datetime.now()
    closed, skipped = [], []
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
        try:
            import portfolio
            from ensemble_engine import EnsembleEngine
            eng = EnsembleEngine()
            p = eng.params
            slip = p["fees"]["slippage_bps"] / 10000
            trade_date = now.strftime("%F")
            for leg in ("A", "B"):
                port = st["sleeves"][leg]
                for code in list(port["positions"]):
                    pos = port["positions"][code]
                    px = _live_px(code)
                    if not px:
                        skipped.append({"code": code, "reason": "无台账价"})
                        continue
                    sell_px = px * (1 - slip)
                    notional = pos["shares"] * sell_px
                    fee = portfolio.trade_fees(notional, "SELL", p)
                    port["cash"] += notional - fee
                    row = {"date": trade_date, "code": code, "side": "SELL",
                           "shares": pos["shares"], "price": round(sell_px, 4),
                           "fee": round(fee, 2), "forced": True}
                    port["trades"].append(row)
                    st["trades"].append(row)
                    closed.append(row)
                    del port["positions"][code]
            # 残余现金→货币腿全额收编（与 park_money 同账：尾差停泊不可免）
            if closed:
                try:
                    mq = _live_px(eng.money_code)
                    if mq:
                        eng.money_px = {trade_date: mq}
                        eng.park_money(st, trade_date)
                except Exception as e:  # noqa: BLE001
                    feed.log_error("kill_park", str(e))
            st["alarms"].append({"date": trade_date, "type": "kill_switch",
                                 "reason": f"人工 PANIC{(':' + reason) if reason else ''}"})
            with open(STATE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, default=str)
        except Exception as e:  # noqa: BLE001
            feed.log_error("kill_liquidate", f"{type(e).__name__}: {e}")
    # 无论台账是否可读：flag 必定落盘（物理开关的最后一道保险）
    with open(FLAG, "w", encoding="utf-8") as f:
        f.write(f"{now:%F %T} 人工紧急清仓 PANIC{(':' + reason) if reason else ''}\n"
                f"解锁命令：python3 scripts/kill_switch.py --release\n")
    n = len(closed)
    _event("kill_switch",
           f"PANIC 清仓 {n} 笔（{[r['code'] for r in closed] or '无持仓'}），"
           f"系统停牌锁定" + (f"；未平仓跳过={skipped}" if skipped else ""))
    codes = [r["code"] for r in closed]
    print(f"[kill_switch] PANIC：强平 {n} 笔 {codes or '（无持仓）'} → {FLAG}")
    print("[kill_switch] 系统停牌：撮合与每日调度已全局拦截。"
          "解锁：python3 scripts/kill_switch.py --release")
    return {"closed": n, "skipped": skipped}


def do_release() -> bool:
    if flag_active():
        os.remove(FLAG)
        _event("kill_switch", "--release 人工解锁，恢复自动运转")
        print("[kill_switch] 已解锁：PANIC_LOCK.flag 删除，系统恢复自动运转")
        return True
    print("[kill_switch] 无锁定（PANIC_LOCK.flag 不存在），无需解锁")
    return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--panic", nargs="?", const="", metavar="REASON")
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    if a.release:
        do_release()
        return 0
    if a.status:
        print(f"[kill_switch] 锁定={'是' if flag_active() else '否'}"
              + (f"（{flag_reason().splitlines()[0]}）" if flag_active() else ""))
        return 0
    if a.panic is not None:
        _event("kill_switch", f"--panic 触发 reason={a.panic or '未填写'}")
        do_panic(a.panic)
        return 0
    print("用法：--panic[原因] / --release / --status")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 —— 开关自身也绝不崩溃
        feed.log_error("kill_fatal", f"{type(e).__name__}: {e}")
        print(f"[kill_switch] 异常已登记 logs/live_error.log（flag 兜底逻辑仍生效）")
        sys.exit(0)
