# -*- coding: utf-8 -*-
"""aggregate_performance.py —— 连续追踪与报表聚合（T-Fin06 模块 3）

每日 15:15（run_live_day.sh 尾段）运行：
1. 连续追踪统计：影子净值 N 日累计/年化、连涨连跌、Alpha 合计、
   对照正式台账回放基准的偏离（真实执行差异）。
2. 策略胜率热力图：过去 N 天（≤10）× A/B 腿 × 大盘灯色（黄/绿）的
   局部胜率（腿级日收益>0 计胜），灯色取自引擎台账（traffic light 收盘口径）。
3. 报告尾部输出 PANIC_LOCK.flag 人工干预开关状态提示。

数据全部读台账（shadow_live.db eod / shadow_state 腿曲线 /
ensemble_state 灯色），只读；产出 reports/aggregate_<date>.md + 推送摘要。
"""
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import live_data_feed as feed


def _legs_of(st: dict) -> tuple:
    """台账赛马腿自适应（T-Fin28：D/E 腿并发）；旧双色台账自动回退 A/B。"""
    return tuple(l for l in ("A", "B", "D", "E") if l in st.get("sleeves", {}))

DB = os.path.join(ROOT, "data", "shadow_live.db")
STATE = os.path.join(ROOT, "data", "shadow_state.json")
REPORTS = os.path.join(ROOT, "reports")
HEAT_DAYS = 10          # 热力图窗口（任务书：10 天）


def _rows(sql, args=()):
    import sqlite3
    with sqlite3.connect(DB) as conn:
        cur = conn.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _light_by_date(eng) -> dict:
    """"日期→灯色"映射（引擎 light_at 收盘口径，确定性）。"""
    out = {}
    for i, date in enumerate(eng.dates):
        try:
            out[date] = eng.light_at(i)["state"]
        except Exception:  # noqa: BLE001
            out[date] = "green"
    return out


def _leg_day_returns(st: dict) -> dict:
    """腿级日收益 dict[leg][date] = ret（来自 ledger 的腿级权益曲线）。"""
    out = {}
    for leg in _legs_of(st):
        curve = st["sleeves"][leg].get("curve", [])
        rets = {}
        for i in range(1, len(curve)):
            d0, v0 = curve[i - 1]
            d1, v1 = curve[i]
            rets[d1] = v1 / v0 - 1 if v0 else 0.0
        out[leg] = rets
    return out


def winrate_grid(leg_rets: dict, lights: dict, n_days: int) -> list[dict]:
    """纯函数：腿级日收益 × 灯色 → A/B ×（黄/绿）局部胜率网格（可单测）。
    窗口语义：先取最近 n_days 个台账日，再按当日灯色分组（红灯不含在内）。"""
    out = []
    for leg in ("A", "B", "D", "E"):   # T-Fin28 四腿；leg_rets 缺席的腿自动跳过
        all_days = sorted(leg_rets.get(leg, {}).items())
        window = all_days[-n_days:]
        for light in ("yellow", "green"):
            rets = [(d, r) for d, r in window if lights.get(d) == light]
            if not rets:
                out.append({"leg": leg, "light": light, "days": 0,
                            "winrate": None, "sum_ret": None})
                continue
            wins = sum(1 for _, r in rets if r > 0)
            out.append({"leg": leg, "light": light, "days": len(rets),
                        "winrate": wins / len(rets),
                        "sum_ret": sum(r for _, r in rets)})
    return out


def heatmap(n_days: int = HEAT_DAYS) -> list[dict]:
    """过去 N 天 A/B ×（黄/绿）局部胜率。"""
    import ensemble_engine as ee
    eng = ee.EnsembleEngine()
    lights = _light_by_date(eng)
    with open(STATE, encoding="utf-8") as f:
        st = json.load(f)
    leg_rets = _leg_day_returns(st)
    return winrate_grid(leg_rets, lights, n_days)


def render_report() -> str:
    eod = _rows("SELECT * FROM eod ORDER BY trade_date")
    day = datetime.now().strftime("%F")
    lines = [f"# 影子实盘连续追踪聚合 · {day}", "",
             f"> 生成 {datetime.now():%F %T} · 台账源：data/shadow_live.db "
             f"(eod/fills/events) + data/shadow_state.json（腿级权益曲线）", ""]
    panic_lines = _panic_footer()
    if len(eod) >= 2:
        navs = [r["nav"] for r in eod]
        cum = navs[-1] / navs[0] - 1
        streak, sign = 0, 0
        for r in eod[1:]:
            s = 1 if r["day_ret"] > 0 else -1 if r["day_ret"] < 0 else sign
            if s == sign or sign == 0:
                streak += 1
                sign = s or sign
            else:
                streak, sign = 1, s
        lines += ["## 连续追踪统计", "",
                  f"- 影子台账日数：**{len(eod)}**（{eod[0]['trade_date']} → "
                  f"{eod[-1]['trade_date']}）",
                  f"- 累计净值变化：**{cum:+.2%}**；当日净值 ¥{navs[-1]:,.0f}",
                  f"- 当前连{'涨' if sign > 0 else '跌/平'}：**{streak} 日**",
                  f"- Alpha 合计（vs 沪深300）："
                  f"**{sum(r['alpha'] or 0 for r in eod):+.2f}pp**；"
                  f"最佳单日 {max(r['day_ret'] for r in eod):+.2%} / "
                  f"最差 {min(r['day_ret'] for r in eod):+.2%}",
                  "", "## 策略胜率热力图（局部：日收益>0 计胜，最近"
                  f"{HEAT_DAYS} 天窗口）", "",
                  "| 策略 | 灯色 | 天数 | 局部胜率 | 窗口累计 |",
                  "|---|---|---|---|---|"]
        for h in heatmap():
            wr = f"{h['winrate']:.0%}" if h["winrate"] is not None else "--"
            sr = f"{h['sum_ret']:+.2%}" if h["sum_ret"] is not None else "--"
            lines.append(f"| {h['leg']} 腿 | 🟡黄/🟢绿的{h['light']} | "
                         f"{h['days']} | {wr} | {sr} |")
        lines += ["- 读法：黄/绿灯=有持仓环境下的局部胜率；红灯日双腿"
                  "被防守门清仓，不计入窗口（铁律优先）。"]
    else:
        lines += ["## 连续追踪统计", "",
                  f"影子 eod 台账仅 {len(eod)} 天——连续追踪热力图在满 2 天后自动出图。"]
    lines += panic_lines
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"aggregate_{day}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _panic_footer() -> list[str]:
    import kill_switch
    if kill_switch.flag_active():
        head = kill_switch.flag_reason().splitlines() or [""]
        return ["", "---", "",
                f"🛑 **人工干预锁定中（PANIC_LOCK.flag）**——{head[0]}",
                "撮合与每日调度已全局拦截；解锁："
                "`python3 scripts/kill_switch.py --release`"]
    return ["", "---", "",
            "🟢 人工干预开关：正常（无 PANIC_LOCK.flag；"
            "紧急制动：`python3 scripts/kill_switch.py --panic`）"]


if __name__ == "__main__":
    try:
        path = render_report()
        print(f"[aggregate] → {path}")
        try:
            import push_notification as push
            st = json.load(open(STATE, encoding="utf-8"))
            import sqlite3
            with sqlite3.connect(DB) as c:
                last = c.execute("SELECT nav, day_ret FROM eod "
                                 "ORDER BY trade_date DESC LIMIT 1").fetchone()
            txt = (f"[连续追踪] 影子净值 ¥{last[0]:,.0f} "
                   f"({last[1] * 100:+.2f}%) · {path.split('/')[-1]}"
                   + (" · 🛑 PANIC 锁定中" if "🛑" in " ".join(_panic_footer())
                      else ""))
            ok, info = push.push_to_params(txt)
        except Exception as e:  # noqa: BLE001
            feed.log_error("aggregate_push", str(e))
    except Exception as e:  # noqa: BLE001 —— cron 不倒
        feed.log_error("aggregate_fatal", f"{type(e).__name__}: {e}")
        print("[aggregate] 异常已登记 logs/live_error.log")
        sys.exit(0)
