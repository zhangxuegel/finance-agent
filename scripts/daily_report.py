# -*- coding: utf-8 -*-
"""daily_report.py —— 每日运行入口 + 报告生成（Task 4 交付）

职责：
  1) 用台账 CSV 数据逐日推进模拟账户（信号→次日开盘撮合→收盘估值→风控熔断）
  2) 生成 Markdown 每日复盘报告到 reports/（含绩效看板 + LLM 分析）

模式：
  python3 scripts/daily_report.py                # 增量：从 data/portfolio.json 推进 1 个交易日
  python3 scripts/daily_report.py --replay N     # 全量重放近 N 个交易日（验收演示，每日出报告）
  python3 scripts/daily_report.py --backtest     # 用台账 CSV 全历史回测（准备工作）
  python3 scripts/daily_report.py --reset        # 清空模拟账户与风控状态

铁律：信号由 strategy.py 生成；LLM 只做复盘分析（策略分析师/风控官），不下单。
"""
import argparse
import json
import os
import sys
import math
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import fetch_data
import portfolio
import risk_control
import screener
import strategy
import regime_filter

REPORT_DIR = os.path.join(ROOT, "reports")


def _build_screener_cfg(params: dict) -> dict:
    """从 params.yaml 的 screener 块构建 screener 运行配置。"""
    sc = params.get("screener", {})
    return {
        "select_count": sc.get("select_count", 3),
        "volume_ratio_threshold": sc.get("volume_ratio_threshold", 2.5),
        "roc_threshold": sc.get("roc_threshold", 0.15),
        "bull_volume_ratio_threshold": sc.get("bull_volume_ratio_threshold", 0.65),
        "lookback_days": sc.get("lookback_days", 20),
        "roc_days": sc.get("roc_days", 10),
        "ma_fast": sc.get("ma_fast", 5),
        "ma_mid": sc.get("ma_mid", 10),
        "ma_slow": sc.get("ma_slow", 20),
    }


# ---------------------------------------------------------------------------
# 引擎：逐日推进（回测与实盘一模一样，保证口径一致）
def engine_run(start_date: str | None = None, report_from: str | None = None,
               buzzer=None) -> dict:
    """从状态（或新账户）推进到台账数据最后一日。

    report_from: 只为 >= 该日期的交易日生成报告（None=只生成最后一日）。
    返回 {"port", "risk_st", "params", "orders_by_day", "reported_days"}
    """
    params = portfolio.load_params()
    watch_all = fetch_data.load_watchlist()
    watch = [w for w in watch_all if not w.get("exclude_core")]  # 511990 仅归 ensemble 的 C 腿
    codes = [w["code"] for w in watch]
    params["n_assets"] = len(codes)

    # T-Fin33（业主拍板 a）：全链（行情/信号/估值）统一 raw 口径
    # （= qfq + Σ{除息日>d}分红，与 audit_fill_time_lock 同一公式），
    # 重放与真账从此同源可互检。
    kline_by = {c: fetch_data.load_raw_csv(c, params) for c in codes}
    dates = sorted(set.intersection(*[{k["date"] for k in kl} for kl in kline_by.values()]))
    by_date = {c: {k["date"]: k for k in kl} for c, kl in kline_by.items()}

    sig_by = {c: {s["date"]: s["signal"] for s in
                  strategy.signals_for(c, kline_by[c], params["strategy"])} for c in codes}

    eng = params["strategy"]["engine"]
    if eng == "A":
        warmup = max(params["strategy"]["A"]["ma_fast"],
                     params["strategy"]["A"]["ma_slow"])
    else:
        warmup = params["strategy"]["B"]["window"]

    port = portfolio.load_state()
    if port is None or not port.get("curve"):
        port = portfolio.reset_state(params)
        # T-Fin33（业主拍板 a）：台账孕育首日=dates[warmup]（自然 MA 成熟点），
        # open 起自然金叉建仓——git e6f93c9 账本首日 2023-11-17 即该点（起跑=锚点），
        # 重放与真账同源可互检；params.anchor.date 作 audit 豁免与说明位保留。
        i0 = warmup
    else:
        i0 = dates.index(port["last_date"]) + 1 if port["last_date"] in dates else warmup

    risk = risk_control.RiskController()
    orders_by_day: dict[str, list] = {}
    reported: list[str] = []

    for i in range(i0, len(dates)):
        date = dates[i]
        prev_date = dates[i - 1]
        prev_sig = {c: sig_by[c].get(prev_date, 0.0) for c in codes}
        # ---- 选股过滤：基于前一日收盘数据动态选股，只拦截买入信号 ----
        # 盘前语义：用截至 prev_date 的行情运行 screiner，选出 date 日可买股票
        if params.get("screener", {}).get("enable", False):
            codes_with_meta = [w for w in watch_all if not w.get("exclude_core")]
            selected = screener.run_for_date(codes_with_meta, _build_screener_cfg(params), prev_date)
            today_watchlist = {s["code"] for s in selected}
        else:
            today_watchlist = {w["code"] for w in watch if not w.get("exclude_core")}
        filtered_sig = {}
        for c in codes:
            sig = prev_sig[c]
            if sig > 0 and c not in today_watchlist:
                # 买入信号但该股不在当日选股结果中：拦截开仓
                filtered_sig[c] = 0.0
            else:
                # 卖出信号（sig == 0）或该股在 watchlist 中：放行（确保止损/止盈平仓）
                filtered_sig[c] = sig
        prev_sig = filtered_sig
        day_price = {c: by_date[c][date] for c in codes if date in by_date[c]}

        orders = []
        if risk.locked(i):
            pass  # 锁仓期：禁止买卖
        else:
            orders = portfolio.place_orders(port, day_price, prev_sig, params, date)
        orders_by_day[date] = orders

        closes_now = {c: by_date[c][date]["close"] for c in codes}
        eq = portfolio.equity(port, closes_now)
        prev_eq = port["curve"][-1][1] if port["curve"] else params["account"]["initial_capital"]
        port["curve"].append([date, round(eq, 2)])

        chk = risk_control.check_breakers(i, date, eq, prev_eq, port["curve"][:-1],
                                          params["risk"])
        forced = []
        if chk["triggered"] and not risk.locked(i):
            forced = portfolio.force_liquidate(port, day_price, params, date,
                                               chk["reason"])
            risk.trigger_lock(i, params["risk"]["lock_days"], chk["reason"], date)
        risk.note_day(i)
        port["last_date"] = date
        portfolio.save_state(port)

        if report_from and date >= report_from:
            reported.append(date)
            write_report(date, port, orders, forced, params, risk, watch)

        chkdict = {"date": date, "orders": len(orders), "equity": round(eq, 2),
                   "forced": forced, "locked": risk.locked(i)}
        if buzzer:
            buzzer(chkdict)

    return {"port": port, "params": params, "watch": watch,
            "orders_by_day": orders_by_day, "reported": reported,
            "risk_st": risk.st}


# ---------------------------------------------------------------------------
# 绩效 & 评分
def benchmark_return(dates_used: list[str]) -> float | None:
    try:
        kl = fetch_data.load_csv(fetch_data.load_params()["benchmark"]["code"])
        within = [k["close"] for k in kl if k["date"] in set(dates_used)]
        if len(within) < 2:
            return None
        return within[-1] / within[0] - 1
    except Exception:  # noqa: BLE001
        return None


def stars_1_to5(m: dict, bench: float | None) -> int:
    """规则评分（LLM 缺席时的兜底）：夏普、回撤、跑赢基准三维度。"""
    if not m:
        return 0
    score = 0
    sh = m.get("sharpe") or 0
    score += 2 if sh > 1.0 else 1 if sh > 0.3 else 0
    mdd = m.get("mdd") or 1
    score += 2 if mdd < 0.10 else 1 if mdd < 0.15 else 0
    if bench is not None:
        r = m.get("total_return") or 0
        score += 1 if r > bench else 0
    return score


def regime_snapshot(params: dict, date: str) -> dict:
    """大盘信号灯快照（确定性规则，见 regime_filter.py）。"""
    px = {k["date"]: k["close"] for k in
          fetch_data.load_csv(params["benchmark"]["code"])}
    closes = [v for d, v in px.items() if d <= date]
    return regime_filter.traffic_light(closes) | {"asof": date}


def exposure_snapshot(port: dict, params: dict, date: str,
                      daily_ret: float | None) -> dict:
    """敞口快照：资金使用率/集中度/β/熔断余量（数字全部系统算，注入 LLM 防幻觉）。"""
    curve = [(d, e) for d, e in port["curve"] if d <= date]
    nav = curve[-1][1] if curve else params["account"]["initial_capital"]
    peak = max((e for _, e in curve), default=nav)
    dd_now = (peak - nav) / peak if peak > 0 else 0.0

    pos_vals = {}
    for code, pos in port["positions"].items():
        rows = [k for k in fetch_data.load_csv(code) if k["date"] == date]
        px = rows[-1]["close"] if rows else pos["cost"]
        pos_vals[code] = round(pos["shares"] * px, 2)
    usage = 1 - port["cash"] / nav if nav else 0.0
    conc = (max(pos_vals.values()) / nav) if pos_vals and nav else 0.0

    # 组合每日收益 vs 基准每日收益 → β（cov/var 估算）
    px = {k["date"]: k["close"] for k in
          fetch_data.load_csv(params["benchmark"]["code"])}
    series = [(d, e) for d, e in curve if d in px]
    if len(series) > 3:
        r_e = [series[i][1] / series[i - 1][1] - 1 for i in range(1, len(series))]
        r_b = [px[series[i][0]] / px[series[i - 1][0]] - 1 for i in range(1, len(series))]
        me, mb = sum(r_e) / len(r_e), sum(r_b) / len(r_b)
        var_b = sum((x - mb) ** 2 for x in r_b) / (len(r_b) - 1)
        cov = sum((a - me) * (b - mb) for a, b in zip(r_e, r_b)) / (len(r_b) - 1)
        beta = round(cov / var_b, 3) if var_b > 0 else None
    else:
        beta = None

    cut_dd = params["risk"]["max_drawdown_cutoff"]
    cut_daily = params["risk"]["daily_loss_cutoff"]
    return {"date": date, "nav": round(nav, 2),
            "usage": usage, "cash": round(port["cash"], 2),
            "concentration": conc, "top_code": (max(pos_vals, key=pos_vals.get)
                                                if pos_vals else None),
            "beta": beta,
            "dd_now": round(dd_now, 4),
            "headroom_dd": round(max(0.0, cut_dd - dd_now), 4),
            "headroom_daily": round(cut_daily - max(0.0, -(daily_ret or 0)), 4),
            "n_positions": len(port["positions"])}


def exposure_md(exp: dict, light: dict) -> str:
    """敞口快照 → 强制注入 LLM 的数字段（LLM 只引用，不重构）。"""
    return (f"【敞口快照·系统注入】date={exp['date']}; NAV=¥{exp['nav']:,.2f}; "
            f"资金使用率={exp['usage']*100:.1f}%; 现金=¥{exp['cash']:,.2f}; "
            f"持仓数={exp['n_positions']}; "
            f"最大单一集中度={exp['top_code']} {exp['concentration']*100:.1f}%; "
            f"组合对大盘β≈{exp['beta']}; 大盘信号灯={light['light']}; "
            f"当前回撤={exp['dd_now']*100:.2f}%; "
            f"熔断余量：回撤线剩 {exp['headroom_dd']*100:.2f}pp、"
            f"单日线剩 {max(0.0, exp['headroom_daily'])*100:.2f}pp")


# ---------------------------------------------------------------------------
# LLM 分析（T-Fin02：升级为"首席风控官（CRO）"视角，强制回答风险敞口）
ANALYST_PROMPT = (
    "你是本模拟组合的首席风控官（CRO）。请强制回答以下 4 个问题，逐条编号：\n"
    "1. 今日风险敞口清单：资金使用率（现金占比的补）、最大单一标的集中度、"
    "组合与大盘的联动性（结合大盘信号灯绿/黄/红）、"
    "距离两条熔断线（单日-3%、峰值回撤-10%）的余量；\n"
    "2. 今日策略执行是否与预期一致，给出依据；\n"
    "3. 若今日发生回撤，用一句话总结教训；\n"
    "4. 按 1-5 星给出策略健康度，并说明扣分原因。\n"
    "口径纪律：所有数字只能引用系统注入的敞口快照，禁止编造。")


def llm_review(context_md: str, params: dict) -> tuple[str | None, str]:
    """返回 (analysis_md, mode)；mode ∈ {llm, rule}。"""
    import requests
    key = os.environ.get(params["llm"]["api_key_env"])
    if not key:
        return None, "rule"
    url = params["llm"]["base_url"].rstrip("/") + "/chat/completions"
    try:
        import requests
        r = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                          json={"model": params["llm"]["model"],
                                "messages": [
                                    {"role": "system", "content": ANALYST_PROMPT},
                                    {"role": "user", "content": context_md}]},
                          timeout=30)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        return text, "llm"
    except Exception as e:  # noqa: BLE001
        return None, f"rule (LLM 调用失败: {type(e).__name__})"


# ---------------------------------------------------------------------------
# 报告
def fmt_pct(x, signed: bool = True) -> str:
    if x is None:
        return "N/A"
    return f"{'+' if signed and x > 0 else ''}{x * 100:.2f}%"


def write_report(date: str, port: dict, orders: list, forced: list,
                 params: dict, risk: risk_control.RiskController, watch: dict):
    m = portfolio.metrics(port, params["account"]["initial_capital"])
    names = {w["code"]: w["name"] for w in watch}
    curve_dates = [d for d, _ in port["curve"]]
    bench = benchmark_return(curve_dates) if curve_dates else None
    today_eq = [e for d, e in port["curve"] if d == date]
    prev = [e for d, e in port["curve"] if d == curve_dates[curve_dates.index(date) - 1]] \
        if curve_dates and curve_dates.index(date) > 0 else [None]
    daily_ret = ((today_eq[0] / prev[0] - 1) if today_eq and prev and prev[0] else None)

    # ---- T-Fin02：大盘信号灯 + 敞口快照（全部系统计算，非 LLM 生成）----
    light = regime_snapshot(params, date)
    exp = exposure_snapshot(port, params, date, daily_ret)

    lines = [f"# T-Fin01·02 每日复盘 · {date}", ""]
    lines.append(f"> 生成时间 {datetime.now():%Y-%m-%d %H:%M:%S} · "
                 f"模拟账户（初始资金 ¥{params['account']['initial_capital']:,.0f}）"
                 f" · 铁律：LLM 只做分析，不下单")
    lines += ["", "## 0) 大盘信号灯（regime_filter · 确定性规则）", "",
              f"- **{light['light']}**：{light['rule']}",
              f"- 研究对象：{params['benchmark']['name']} · 判定公式：close×MA60×MA120 排列法"]
    if light.get("note"):
        lines.append(f"- note：{light['note']}")
    lines += ["", "## 1) 当日模拟交易指令", ""]
    if not orders and not forced:
        lines.append("_无买卖指令（信号未变化，或锁仓期中）。_")
    else:
        lines += ["| 日期 | 代码 | 名称 | 方向 | 数量(股) | 成交价 | 手续费 |", "|---|---|---|---|---|---|---|"]
        for o in orders + [
                {**f, "name": ""} for f in forced]:
            lines.append(f"| {o['date']} | {o['code']} | {names.get(o['code'], o.get('name', ''))} "
                         f"| {o['side']} | {o['shares']} | {o['price']} "
                         f"| {o.get('fee', '—')} |")
    if forced:
        lines += ["", f"**⚠ 熔断强平**：{'; '.join(f['reason'] + ' → ' + f['code'] for f in forced)}"]

    # 持仓
    lines += ["", "## 2) 当前持仓", "",
              "| 代码 | 名称 | 持股 | 成本价 | 现价 | 市值(收盘) |", "|---|---|---|---|---|---|"]
    kl = fetch_data.load_csv  # local alias
    total_pos = 0.0
    for code, pos in port["positions"].items():
        closes = fetch_data.load_csv(code)
        px = [k for k in closes if k["date"] == date]
        px = px[-1]["close"] if px else pos["cost"]
        val = round(pos["shares"] * px, 2)
        total_pos += val
        lines.append(f"| {code} | {names.get(code)} | {pos['shares']} "
                     f"| {pos['cost']:.4f} | {px:.4f} | {val:,.2f} |")
    lines.append(f"| **现金** |  |  |  |  | **{port['cash']:,.2f}** |")
    assert abs(total_pos + port["cash"] - (today_eq[0] if today_eq else
              port["cash"])) < 0.01, "持仓市值 + 现金 ≠ 净值（估值恒等式被破坏）"

    days_n = None
    lines += ["", "## 3) 绩效看板", ""]
    sharpe = m.get("sharpe")
    lines.append(f"- 当前净值：**¥{m['nav']:,.2f}**（{fmt_pct(m['total_return'])} 累计," 
                 f"{fmt_pct(m.get('ann_return'))} 年化）")
    lines.append(f"- 最大回撤：**{fmt_pct(m['mdd'], False)}**"
                 f"（风控线 {params['risk']['max_drawdown_cutoff']:.0%}，"
                 f"{'✅ 达标' if m['mdd'] < params['risk']['max_drawdown_cutoff'] else '❌ 超线'}）")
    lines.append(f"- 胜率：**{fmt_pct(m['win_rate'], False)}**"
                 f"（{m['n_trades_closed']} 笔已平仓交易；目标 >45%）")
    lines.append(f"- 夏普比率：**{sharpe}**（风险利率 {portfolio.RISK_FREE}；"
                 f"Sortino **{m.get('sortino')}**，Calmar **{m.get('calmar')}**）")
    if daily_ret is not None:
        lines.append(f"- 今日净值变动：**{fmt_pct(daily_ret)}**"
                     f"（单日熔断线 -{params['risk']['daily_loss_cutoff']:.0%}）")
    lines.append(f"- 基准（{params['benchmark']['name']}）同窗累计：**{fmt_pct(bench)}**")

    locked = risk.st.get("locked", False)
    lines += ["", "## 4) 风控状态", ""]
    if locked:
        lines.append(f"- 🔒 **锁仓中**（熔断日 {risk.st.get('lock_from')}，"
                     f"原因：{risk.st.get('lock_reason')}）")
    else:
        lines.append("- ✅ 未锁仓；两条熔断规则在线（单日 -3% / 峰值回撤 -10%）")
    if risk.st.get("alarm_days"):
        lines.append(f"- 历史熔断 {len(risk.st['alarm_days'])} 次")

    # LLM 分析（T-Fin02：首席风控官视角 + 敞口快照强制注入）
    snap = exposure_md(exp, light)
    ctx = (f"日期 {date}；净值 ¥{m['nav']:,.2f}（今日 {fmt_pct(daily_ret)}）；"
           f"累计 {fmt_pct(m['total_return'])}；最大回撤 {fmt_pct(m['mdd'], False)}；"
           f"胜率 {fmt_pct(m['win_rate'], False)}；夏普 {sharpe}；"
           f"Sortino {m.get('sortino')}；Calmar {m.get('calmar')}；"
           f"持仓 {len(port['positions'])} 只，市值 ¥{total_pos:,.0f}；"
           f"今日成交 {len(orders)} 笔，强平 {len(forced)} 笔；{snap}")
    text, mode = llm_review(ctx, params)
    starrule = stars_1_to5(m, bench)
    lines += ["", "## 5) 首席风控官（CRO）分析", ""]
    if text:
        lines.append(f"> {text}")
    else:
        mode = f"rule（LLM 未接入：{'未设置 ' + params['llm']['api_key_env']}）"
        # CRO 四问兜底：逐条强制回答（数字同样来自系统快照，防幻觉口径一致）
        verdict = ("符合预期：无强平、无锁仓，今日亏损/收益未触熔断线"
                   if not forced and exp["headroom_daily"] > 0 and exp["headroom_dd"] > 0
                   else "异常：已触发熔断强平，进入锁仓流程")
        lesson = (f"回撤 {fmt_pct(m['mdd'], False)}：教训是入场前必须等排列走顺，"
                  "事后止损永远比事前过滤贵。"
                  if exp["dd_now"] > 0.05 else "无回撤发生，无教训项。")
        deduction = []
        if (m.get("sharpe") or 0) <= 1.0:
            deduction.append("夏普≤1")
        if (m.get("mdd") or 0) >= 0.10:
            deduction.append("回撤触线")
        if (m.get("win_rate") is not None and m["win_rate"] < 0.45
                and m["n_trades_closed"] > 5):
            deduction.append("胜率<45%")
        star = ("无扣分" if not deduction else "扣分：" + "、".join(deduction))
        rule_txt = ("【CRO 规则点评｜LLM 缺席兜底】\n"
                    f"> 1) 风险敞口：资金使用率 {exp['usage']*100:.1f}%"
                    f"（现金 ¥{exp['cash']:,.0f}）；"
                    f"最大集中度 {exp['top_code']} {exp['concentration']*100:.1f}%；"
                    f"组合对大盘 β≈{exp['beta']}（大盘灯：{light['state']}）；"
                    f"熔断余量：回撤线剩 {exp['headroom_dd']*100:.2f}pp、"
                    f"单日线剩 {max(0.0, exp['headroom_daily'])*100:.2f}pp\n"
                    f"> 2) 执行判定：{verdict}\n"
                    f"> 3) 教训：{lesson}\n"
                    f"> 4) 健康度：{'★' * starrule or '☆'}{'☆' * (5 - starrule)}"
                    f"（{star}）")
        lines.append(f"> {rule_txt}")
    lines.append(f"> ——分析来源：{mode}；prompt=CRO 四问（敞口必答）；"
                 f"敞口快照已注入={snap[:40]}…")

    # 数据溯源
    lines += ["", "## 6) 数据溯源", "",
              f"- 行情源：东方财富 push2his（主）/ 腾讯行情（备）· 免Token",
              f"- 数据文件：data/{{510300,159915,513500,518880}}.csv"
              f"· 台账=index/params.yaml · 引擎={params['strategy']['engine']}",
              "- 风控台账：data/risk_state.json · 账户台账：data/portfolio.json",
              "- T-Fin02 挂接：regime_filter（信号灯）· portfolio.metrics"
              "（Sortino/Calmar）· risk_control（时间锁/地狱模式审计）",
              "- 声明：本报告为模拟盘学习用途，不构成投资建议。"]

    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"{date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[report] {path}")
    return path


# ---------------------------------------------------------------------------
# CLI
def summary_text(res: dict):
    m = portfolio.metrics(res["port"], res["params"]["account"]["initial_capital"])
    print(json.dumps(m, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", type=int, metavar="N", dest="replay",
                    help="全量重放，只为最近 N 日生成报告")
    ap.add_argument("--backtest", action="store_true", help="全历史回测只出汇总")
    ap.add_argument("--reset", action="store_true", help="清空账户与风控状态")
    ap.add_argument("--advance", action="store_true", help="增量推进 1 个交易日（默认）")
    a = ap.parse_args()

    if a.reset:
        params = portfolio.load_params()
        portfolio.reset_state(params)
        portfolio.reset_risk()
        print("[reset] 账户已重置为初始资金。")
        sys.exit(0)

    if a.replay or a.backtest:
        params = portfolio.load_params()
        portfolio.reset_state(params)
        portfolio.reset_risk()
        dates = sorted({k["date"] for k in fetch_data.load_csv(
            fetch_data.load_watchlist()[0]["code"])})
        report_from = dates[-a.replay] if a.replay else None
        res = engine_run(report_from=report_from)
        summary_text(res)
        if not a.replay:
            print("（--backtest 完整重放，仅最近 1 日报告）")
    else:
        state = portfolio.load_state()
        assert state and state.get("last_date"), \
            "无历史状态，请先运行 --replay 20 或 --backtest 初始化。"
        dates = sorted({k["date"] for k in fetch_data.load_csv(
            fetch_data.load_watchlist()[0]["code"])})
        res = engine_run(start_date=None, report_from=dates[-1])
        summary_text(res)
