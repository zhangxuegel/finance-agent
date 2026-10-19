# -*- coding: utf-8 -*-
"""live_monitor.py —— 实时监控面板与异动报警（T-Fin05 模块 3）

盘中熔断监控（cron：交易日 09:30-15:00 每 5 分钟）：
  沪深300 涨跌幅 ≤ live.monitor.bench_drop（−2%）→ **立即**紧急 webhook 报警
  （不必等到收盘——收盘前的熔断窗口提前叫醒 CRO）。报警带滞回（re-arm）：
  数字回到 ≥ −1.0pp 之前不重复轰炸。

预案推送（cron：08:45）：读取 data/shadow_plan.json → 生成
  reports/shadow_plan_<date>.md 并推送"今日操作预案"。
收盘复盘推送（cron：15:10）委托 shadow_broker --review（15:05 已落库）。

铁律：任何异常 try-except 吞掉并写 logs/live_error.log，绝不放倒 cron。
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
import portfolio

ALERT_FILE = os.path.join(ROOT, "data", "live_alert_state.json")
REPORTS = os.path.join(ROOT, "reports")


def _err(kind, detail):
    feed.log_error(kind, detail)


def _push(text) -> tuple:
    try:
        import push_notification as push
        return push.push_to_params(text)
    except Exception as e:  # noqa: BLE001
        _err("monitor_push", str(e))
        return False, f"{e}"


def stage_monitor(replay: bool = False) -> dict:
    """盘中 5 分钟级指数监控：≤ −2% 立即报警（滞回防轰炸）。"""
    now = feed.sh_now()
    if not replay:
        t = now.strftime("%H:%M")
        if now.weekday() >= 5 or not ("09:30" <= t < "15:00"):
            return {"skipped": "非盘中时段"}
    idx = feed.index_realtime()
    if not idx:
        _err("monitor_index", "指数行情不可用（本轮回退，下轮再试）")
        return {"skipped": "no index quote"}
    pct = idx["pct"]
    p = portfolio.load_params()
    drop = float(p.get("live", {}).get("monitor", {}).get("bench_drop", -0.02))
    rearm = float(p.get("live", {}).get("monitor", {}).get("rearm_pct", -1.0))
    state = {"ts": now.strftime("%F %T"), "pct": pct, "alerted": False}
    try:
        with open(ALERT_FILE, encoding="utf-8") as f:
            prev = json.load(f)
        state["alerted"] = bool(prev.get("alerted")) \
            and not (pct >= rearm)
    except Exception:  # noqa: BLE001
        pass
    fired = False
    if pct <= drop and not state["alerted"]:
        fired = True
        state["alerted"] = True
        msg = (f"🔴 紧急：沪深300 盘中 {pct:+.2f}% （阈值 {drop:+.1%}）"
               f"@ {now:%T}。急跌熔断门预计下一开盘生效——请查看系统！")
        _push("[CRO盘中报警]" + msg)
        import shadow_broker
        shadow_broker.db_event("intraday_alert",
                               f"沪深300 {pct:+.2f}% ≤ {drop:+.1%}")
    elif pct >= rearm and state["alerted"]:
        state["alerted"] = False      # 回到滞回区间 → 重新武装
        _push(f"[CRO盘中-解除] 沪深300 已回升 {pct:+.2f}%，报警滞回重置")
    with open(ALERT_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    print(f"[monitor] {now:%T} 沪深300 {pct:+.2f}% "
          f"（阈值 {drop:+.1%}）本拍报警={fired} 滞回挂起={state['alerted']}")
    return {"pct": pct, "alert_fired": fired}


def stage_plan_push(replay: bool = False) -> str:
    """08:45 今日操作预案（读 shadow_plan.json——纯台账计划，无实时依赖）。"""
    from shadow_broker import PLAN, stage_plan
    trade_date = feed.sh_now().strftime("%F")
    try:
        plan = json.load(open(PLAN, encoding="utf-8"))
        if plan.get("trade_date") != trade_date:
            plan = stage_plan(replay, verbose=False)
    except Exception:  # noqa: BLE001
        plan = stage_plan(replay, verbose=False)
    lines = [f"# 影子实盘 · 今日操作预案 · {trade_date}", "",
             f"> 生成 {datetime.now():%F %T} · 台账源：ensemble_state + "
             f"`EnsembleEngine.export_plan()`（只用 ≤ 台账最后一日数据）", "",
             f"- 大盘灯：**{plan['light']}** ｜ 引擎防守判定：**防守**"
             f"（{plan['defense_reason']}）" if plan["defense"] else
             f"- 大盘灯：**{plan['light']}** ｜ 引擎判定：**正常出闸**",
             f"- 数据源模式：{plan['mode']['mode']}"
             + (f"（{plan['mode'].get('reason', '')}）"
                if plan["mode"].get("reason") else ""),
             f"- 赛马待执行：{'是 → 开盘赎回+按权重再分配' if plan['race_pending'] else '否'}",
             f"- 建议信号（上一交易日收盘生成）：A 腿 {plan['prev_sig']['A']}, "
             f"B 腿 {plan['prev_sig']['B']}",
             "- 执行纪律：09:30 真实开盘价撮合；**跌停标的当日禁买**；"
             "降级模式下放弃开新交易；两条熔断绝对规则继承。", "",
             "---", "", "### 台账溯源（本次预案引用）", "",
             "- 防守判定：`ensemble.race/tilt/crash_gate.*` + `risk.*`；",
             "- 信号：`scripts/ensemble_engine.leg_signals`（≤ 前一交易日收盘）；",
             "- 成交台账：`data/shadow_live.db`（真实时价；落库时点 15:05）。"]
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"shadow_plan_{trade_date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    head = (f"08:45 预案：灯={plan['light']} "
            f"{'防守' if plan['defense'] else '正常出闸'} "
            f"赛马待执行={plan['race_pending']} "
            f"数据源={plan['mode']['mode']}")
    _push("[影子预案] " + head)
    print(f"[plan-push] → {path}")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor", action="store_true", help="盘中指数监控（5 分钟）")
    ap.add_argument("--plan-push", action="store_true", help="08:45 预案推送")
    ap.add_argument("--replay", action="store_true", help="回放时钟（验收）")
    a = ap.parse_args()
    try:
        if a.plan_push:
            stage_plan_push(replay=a.replay)
        elif a.monitor:
            stage_monitor(replay=a.replay)
        else:
            print("用法：--monitor（盘中） / --plan-push（08:45）")
    except Exception as e:  # noqa: BLE001 —— cron 不倒
        _err("monitor_fatal", f"{type(e).__name__}: {e}")
        print(f"[monitor] 异常已登记 logs/live_error.log")
