# -*- coding: utf-8 -*-
"""repair_shadow_backfill.py —— 影子链缺日 EOD 补账维护工具（T-Fin32）

背景：手机侧/云端 workflow 同缺交易日时，shadow_live.db 的 eod 行缺失，
水位断点续传会永远跳过这些日。本工具按**真实公开日线**（fetch_data 接口，
绝不伪造数字）逐日补估值落库。

前提与安全闸（违反即停手不改账）：
  A. 目标区间内 shadow_live.db.fills 必须为 0 行——缺日内若本应有成交，
     必须走 stage_plan/stage_match 全流程（需要当日 09:30 真实快照），
     不能事后用日线开收价冒充快照价 → 停手交人工。
  B. 影子腿（A/B sleeves）必须空仓——空仓 + 红灯防守 ⇒ 缺日撮合本应
     零订单，eod 补估值与"当日实跑"口径一致；有持仓则停手交人工。
  C. 计划基准日（前一收盘日）的信号灯用 regime_filter.traffic_light 在
     截断台账上确定性重算，defense 元数据按灯色回填 `_door`，不猜。
  D. 每日 ledger 用 shadow_broker.stage_ledger(replay=True) 原函数执行
     （时钟 override 到该交易日 15:05），K 线缺行由 stage_ledger 内部
     `eod_gap` 显式报错并用最近一日收盘标注——不静默。

用法：
  python3 scripts/repair_shadow_backfill.py --from 2026-10-22 --to 2026-10-23 [-n]
  python3 scripts/repair_shadow_backfill.py --status   # 只读体检

铁律继承 AGENTS.md：模拟盘、数字全部来自真实行情接口台账、LLM 无下单权。
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

import live_data_feed as feed
import shadow_broker


def _fills_in_range(d_from: str, d_to: str) -> list:
    conn = sqlite3.connect(shadow_broker.DB)
    rows = conn.execute(
        "SELECT trade_date, leg, code, side, shares FROM fills "
        "WHERE trade_date>=? AND trade_date<=?", (d_from, d_to)).fetchall()
    conn.close()
    return rows


def _sleeve_positions(st: dict) -> dict:
    return {leg: port["positions"]
            for leg, port in st.get("sleeves", {}).items()
            if port.get("positions")}


def _plan_light(basis_date: str) -> dict:
    """计划基准日（basis_date 当天收盘）的确定性信号灯。"""
    import fetch_data
    import regime_filter
    kl = [k for k in fetch_data.load_csv("510300")
          if k["date"] <= basis_date]
    return regime_filter.traffic_light([k["close"] for k in kl])


def _fake_clock(d_from: str, d_to: str):
    """把 sh_now 定点回放为区间内每个交易日的 15:05（北京时间）。"""
    import fetch_data
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001 —— 与 live_data_feed 同口径降级
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=8))
    # 用台账首个标的闭弦推断交易日序列（fetch 已含全部目标日）
    kdates = sorted({k["date"] for k in
                     fetch_data.load_csv("510300")
                     if d_from <= k["date"] <= d_to})
    missing = []
    kset = set(kdates)
    d = datetime.strptime(d_from, "%Y-%m-%d").date()
    end = datetime.strptime(d_to, "%Y-%m-%d").date()
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in kset:
            missing.append(d.isoformat())
        d = d.fromordinal(d.toordinal() + 1)
    return kdates, missing, tz


def status() -> int:
    import fetch_data
    st = json.load(open(shadow_broker.STATE, encoding="utf-8"))
    conn = sqlite3.connect(shadow_broker.DB)
    tail = conn.execute("SELECT trade_date, nav, day_ret, defense, fills_n "
                        "FROM eod ORDER BY trade_date DESC LIMIT 3").fetchall()
    conn.close()
    print("shadow eod tail (date/nav/day_ret/defense/fills_n):")
    for r in tail:
        print(" ", r)
    print("curve tail:", st.get("curve", [])[-3:])
    print("sleeve positions:", _sleeve_positions(st) or "空")
    last = tail[0][0] if tail else "?"
    csv_date = max({k["date"] for k in fetch_data.load_csv("510300")})
    if last != csv_date:
        print(f"[gap] 影子 eod 尾日 {last} < 台账尾日 {csv_date} → 需补账")
        return 1
    print("[ok] 影子 eod 与台账尾日一致")
    return 0


def backfill(d_from: str, d_to: str, dry: bool) -> int:
    import fetch_data
    kdates, missing, tz = _fake_clock(d_from, d_to)
    if missing:
        print(f"[stop] 台账 CSV 缺柜台交易日 {missing} → 先跑 fetch_data.py")
        return 2
    if not kdates:
        print(f"[stop] 区间 {d_from}…{d_to} 无交易日")
        return 2
    fills = _fills_in_range(kdates[0], kdates[-1])
    if fills:
        print(f"[stop] 安全闸 A：影子库缺日区间已有 {len(fills)} 笔 fills，"
              f"须走 plan/match 全流程，本工具只处理零成交缺口")
        for r in fills:
            print("   ", r)
        return 3
    st = json.load(open(shadow_broker.STATE, encoding="utf-8"))
    if _sleeve_positions(st):
        print("[stop] 安全闸 B：影子腿有持仓，缺日撮合口径须人工核 → 停手")
        return 3

    import portfolio as portfolio  # noqa: F401 —— 台账参数由 stage_ledger 内部读取
    orig = feed.sh_now
    try:
        for d in kdates:
            alldates = sorted({k["date"] for k in
                               fetch_data.load_csv("510300")})
            basis = [k for k in alldates if k < d]
            basis = basis[-1] if basis else None
            light = _plan_light(basis) if basis else {"state": "yellow"}
            defense = light["state"] in ("red", "yellow")
            if not dry:
                st = json.load(open(shadow_broker.STATE, encoding="utf-8"))
                st["_door"] = {"defense": defense, "door":
                               "defense" if defense else "off"}
                with open(shadow_broker.STATE, "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False, default=str)

                def fake_now(_d=d, _tz=tz):
                    dt = datetime.fromisoformat(f"{_d}T15:05:00")
                    return dt.replace(tzinfo=_tz)

                feed.sh_now = fake_now
                res = shadow_broker.stage_ledger(replay=True, verbose=True)
                feed.sh_now = orig
                if res.get("trade_date") != d:
                    print(f"[stop] {d} 落库日漂移 → {res.get('trade_date')}")
                    return 4
                if res["fills_n"]:
                    print(f"[stop] {d} 出现意外成交 {res['fills_n']} 笔 → 停手")
                    return 5
                cur = json.load(open(shadow_broker.STATE,
                                     encoding="utf-8"))["curve"]
                days = [r[0] for r in cur[-4:]]
                print(f"[ok] {d} nav=¥{res['nav']:,.0f} "
                      f"day_ret={res['day_ret'] * 100:+.2f}% "
                      f"bench={res['bench_pct'] * 100:+.2f}% "
                      f"defense={defense} fills=0")
            else:
                print(f"[dry] {d} 计划灯={light['state']} "
                      f"defense={defense} （不落库）")
    finally:
        feed.sh_now = orig
    if not dry:
        with sqlite3.connect(shadow_broker.DB) as conn:
            rows = conn.execute(
                "SELECT trade_date, nav, day_ret FROM eod "
                "WHERE trade_date>=? ORDER BY trade_date", (d_from,
                                                            )).fetchall()
        print("已落库：", rows)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="d_from", required=False)
    ap.add_argument("--to", dest="d_to", required=False)
    ap.add_argument("-n", "--dry", action="store_true", help="只读预演")
    ap.add_argument("--status", action="store_true", help="只读体检")
    a = ap.parse_args()
    if a.status:
        sys.exit(status())
    if not a.d_from or not a.d_to:
        ap.print_help()
        sys.exit(2)
    sys.exit(backfill(a.d_from, a.d_to, a.dry))


if __name__ == "__main__":
    main()
