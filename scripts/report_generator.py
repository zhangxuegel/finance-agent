# -*- coding: utf-8 -*-
"""report_generator.py —— 每日晨报与周末深度复盘（T-Fin03 模块 3 的闭环延伸）

每日晨报（--daily）：当前红绿灯 / 可用资金 / 持仓分布 / 昨日盈亏。
周末深度复盘（--weekly）：本周 A/B/C 三腿绩效对比 / 赛马权重演变 /
CRO 风控总结；报告尾部附**台账溯源**（本次权重调整引用 params.yaml 的哪些条款）。

数字纪律：全部来自系统台账（data/ensemble_state.json、index/params.yaml、
行情 CSV），LLM 生成物仅允许出现在文字定性区，且必须与 CRO 硬注入一致。
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import fetch_data
import regime_filter

STATE = os.path.join(ROOT, "data", "ensemble_state.json")
REPORTS = os.path.join(ROOT, "reports")


def _legs_of(st: dict) -> tuple:
    """台账赛马腿自适应（T-Fin28：D/E 腿并发）；旧双色台账自动回退 A/B。"""
    return tuple(l for l in ("A", "B", "D", "E") if l in (st or {}).get("sleeves", {}))


def _light(asof: str | None = None) -> dict:
    kl = fetch_data.load_csv("510300")
    if asof:
        kl = [k for k in kl if k["date"] <= asof]
    return regime_filter.traffic_light([k["close"] for k in kl])


def _load_state():
    if not os.path.exists(STATE):
        return None
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def _curve_tail(st, n=2):
    curve = [(d, e) for d, e in st["curve"]]
    return curve[-n:]


# ---------------------------------------------------------------------------
def daily(asof: str | None = None) -> str:
    import portfolio
    st = _load_state()
    assert st, "缺少 data/ensemble_state.json —— 先运行 ensemble_engine --daily"
    params = portfolio.load_params()
    light = _light(asof)
    curve = [(d, e) for d, e in st["curve"] if asof is None or d <= asof]
    date, eq = curve[-1]
    yesterday = curve[-2][1] if len(curve) > 1 else eq
    pnl = eq / yesterday - 1 if yesterday else 0.0
    eng = __import__("ensemble_engine").EnsembleEngine()
    money_px = eng.money_px.get(date) or (st["money"].get("last_px") or 0.0)
    money_val = sum(st["money"].get(k, 0)
                     for k in _legs_of(st) + ("MASTER",)) * money_px
    pos_val = 0.0
    rows = ["| 腿 | 持仓 | 市值占比 |", "|---|---|---|"]
    names = {w["code"]: w["name"] for w in eng.watch}
    for leg in _legs_of(st):
        port = st["sleeves"][leg]
        leg_val = port["cash"]
        if port["positions"]:
            for cp, pp in sorted(port["positions"].items()):
                close = eng.by_date[cp][date]["close"]
                val = pp["shares"] * close
                pos_val += val
                leg_val += val
                rows.append(
                    f"| {leg}·{cp} {names.get(cp, '')} | {pp['shares']:,} 股 | "
                    f"{val / eq * 100:.1f}% |")
        mv = st["money"][leg] * money_px
        money_val -= 0  # money_val 已在汇总，此处仅展示
        if mv > 0:
            rows.append(f"| {leg}·C货(511990) | {st['money'][leg]:,} 股 | "
                        f"{mv / eq * 100:.1f}% |")
        rows.append(f"| {leg}·现金 | —— | {port['cash'] / eq * 100:.1f}% |")
    if st["money"]["MASTER"]:
        rows.append(f"| 母·C货(511990) | {st['money']['MASTER']:,} 股 | "
                    f"{st['money']['MASTER'] * money_px / eq * 100:.1f}% |")
    idle = st["master_cash"] + sum(st["sleeves"][l]["cash"] for l in _legs_of(st))
    sa = st["race_log"][-1] if st["race_log"] else {}
    lines = [f"# T-Fin04 每日晨报 · {date}", "",
             f"> 生成 {datetime.now():%F %H:%M} · 数字源：system台账（LLM 未经手）", "",
             "## 1) 今日速览", "",
             f"- 大盘灯：**{light['light']}**（{light['state']}；"
             f"策略与仓位限制：绿=进攻 黄=减仓 红=全面防守）",
             f"- 合并净值：**¥{eq:,.2f}**（昨日 ¥{yesterday:,.2f}，"
             f"{'+' if pnl >= 0 else ''}{pnl * 100:.2f}%）",
             f"- 可用资金（现金类）：**¥{idle:,.2f}**（{idle / eq * 100:.1f}%；"
             f"其余在场含货币腿 ¥{money_val:,.0f}）",
             f"- 当前赛马权重：A {sa.get('weight_a', 0) * 100:.0f}% / "
             f"B {sa.get('weight_b', 0) * 100:.0f}%"
             f"（{sa.get('tilt_note', 'neutral')}）", ""]
    lines += ["## 2) 持仓分布", ""] + rows + ["", "---", "",
              "### 台账溯源（本报告使用的台账条款）", "",
              "- 灯色：`regime_filter.traffic_light` ← `index/params.yaml benchmark.*`；",
              "- 权重：`index/params.yaml ensemble.race.* 与 ensemble.tilt.*`；",
              "- 熔断与锁定期：`index/params.yaml risk.*`（单日-3%/峰值回撤-10%+锁3日）；",
              "- 行情：`data/*.csv` ← `scripts/fetch_data.py`（东财/腾讯公开接口落盘）；",
              f"- 账户状态：`data/ensemble_state.json`（last_date={st['last_date']}）。"]
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"morning_{date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[morning] → {path}")
    return path


def weekly(asof: str | None = None) -> str:
    import portfolio
    st = _load_state()
    assert st, "缺少 data/ensemble_state.json —— 先运行 ensemble_engine --daily"
    params = portfolio.load_params()
    curve = st["curve"]
    asof = asof or st["last_date"]
    import datetime as _dt
    wk = _dt.datetime.strptime(asof, "%Y-%m-%d").isocalendar()
    lo = f"{wk[0]}-W{wk[1]:02d}"
    # 本周起点 = 上一个周终之后
    prev_race = [r for r in st["race_log"] if r["week_end"] < asof]
    week_start_race = prev_race[-1]["week_end"] if prev_race else curve[0][0]
    idx = {d: i for i, (d, _) in enumerate(curve)}
    i0, i1 = idx.get(week_start_race, 0), idx[asof]
    # 三腿绩效对比（曲线段收益）
    leg_rows, comp = ["| 腿 | 本周收益 | 期末权益 |", "|---|---|---|"], {}
    eng = __import__("ensemble_engine").EnsembleEngine(log_writes=False)
    eq_sign = 0.0
    for leg in _legs_of(st):
        lc = st["sleeves"][leg]["curve"]
        lidx = {d: e for d, e in lc}
        seg = [(d, e) for d, e in lc if week_start_race <= d <= asof and e]
        ret = seg[-1][1] / seg[0][1] - 1 if len(seg) > 1 and seg[0][1] else 0.0
        comp[leg] = ret
        leg_rows.append(f"| {leg} | {ret * 100:+.2f}% | ¥{seg[-1][1] if seg else 0:,.0f} |")
    money_px = eng.money_px.get(asof) or (st["money"].get("last_px") or 0.0)
    mv0 = sum(st["money"].get(k, 0) for k in ("A", "B", "MASTER")) * money_px
    comp["C"] = None  # 货币腿无独立曲线：以持仓期间收益近似→用 511990 区间涨幅
    mk = fetch_data.load_csv(st and eng.money_code)
    mseg = [k for k in mk if week_start_race <= k["date"] <= asof]
    c_ret = (mseg[-1]["close"] / mseg[0]["close"] - 1) if len(mseg) > 1 else 0.0
    leg_rows.append(f"| C(货币腿) | {c_ret * 100:+.2f}%（511990 区间）| ¥{mv0:,.0f} |")
    eq_ret = curve[i1][1] / curve[i0][1] - 1 if i1 > i0 else 0.0
    m_all = portfolio.metrics({"cash": st["master_cash"], "positions": {},
                               "curve": curve, "trades": st["trades"]},
                              params["account"]["initial_capital"])
    light = _light(asof)
    races = [r for r in st["race_log"] if week_start_race <= r["week_end"] <= asof]
    race_tbl = ["| 周终 | A夏普 | B夏普 | A/B分配 | C预配 | 因子分 | 依据 |",
                "|---|---|---|---|---|---|---|"]
    for r in races:
        race_tbl.append(
            f"| {r['week_end']} | {r['sharpe_a']} | {r['sharpe_b']} "
            f"| {r['weight_a'] * 100:.0f}%/{r['weight_b'] * 100:.0f}% "
            f"| {r['c_buffer'] * 100:.0f}% | {(r['factor'] or {}).get('score')} "
            f"| {r.get('tilt_note') or (r.get('factor') or {}).get('source') or '-'} |")
    alarms = [a for a in st["alarms"] if week_start_race <= (a.get("date", "")) <= asof]
    mdd_now = m_all["mdd"]
    dist_mdd = max(0.0, params["risk"]["max_drawdown_cutoff"] * 100 - mdd_now * 100)
    lines = [f"# T-Fin04 周末深度复盘 · {lo}", "",
             f"> 生成 {datetime.now():%F %H:%M} · 区间 {week_start_race} → {asof}", "",
             "## 1) 本周绩效（合并 + 三腿对比）", "",
             f"- 合并净值：¥{curve[i1][1]:,.2f}（本周 {eq_ret * 100:+.2f}%）；"
             f"全程夏普 {m_all['sharpe']} · Sortino {m_all['sortino']} · "
             f"Calmar {m_all['calmar']} · MDD {mdd_now:.2%}",
             "- 灯色更迭不改变策略信号（铁律）；赛马只调权重不越熔断。", ""]
    lines += leg_rows + ["", "## 2) 赛马权重演变", ""] + race_tbl + [
        "", "## 3) CRO 本周风控总结", "",
        f"- 熔断触发：{len(alarms)} 次"
        + (f"（{json.dumps(alarms, ensure_ascii=False)}）" if alarms else "（安稳）"),
        f"- 回撤余量：距 -{params['risk']['max_drawdown_cutoff'] * 100:.0f}% 峰值熔断还有 "
        f"{dist_mdd:.2f} 个百分点（当前 MDD {mdd_now * 100:.2f}%）；"
        f"单日护栏 -{params['risk']['daily_loss_cutoff'] * 100:.0f}%。",
        "- 本周 Moves 全部为确定性驱动（赛马=Sharpe 排名、C 腿=灯色门）。",
        "- LLM 只出现在因子倾斜位（tilt ±pp，Python clamp），无任何买卖权。", "",
        "---", "", "### 台账溯源（本次复盘引用的 params.yaml 条款）", "",
        f"- 赛马：`ensemble.race.winner_share={params['ensemble']['race']['winner_share']}`、"
        f"`loser_share={params['ensemble']['race']['loser_share']}`、"
        f"`max_weekly_shift={params['ensemble']['race']['max_weekly_shift']}`（±20pp 平滑）、"
        f"`min_weight={params['ensemble']['race']['min_weight']}`（下限）、"
        f"`lookback={params['ensemble']['race']['lookback']}`；",
        f"- 因子倾斜：`ensemble.tilt.score_high={params['ensemble']['tilt']['score_high']}`、"
        f"`a_tilt_pp={params['ensemble']['tilt']['a_tilt_pp']}`、"
        f"`score_low={params['ensemble']['tilt']['score_low']}`、"
        f"`c_tilt_pp={params['ensemble']['tilt']['c_tilt_pp']}`；",
        f"- C 腿标的：`ensemble.C.code={params['ensemble']['C']['code']}` "
        f"（watchlist: secid {params['ensemble']['C']['secid']}，exclude_core=true）；",
        f"- 熔断（不可优化红线）：`risk.daily_loss_cutoff={params['risk']['daily_loss_cutoff']}`、"
        f"`risk.max_drawdown_cutoff={params['risk']['max_drawdown_cutoff']}`、"
        f"`risk.lock_days={params['risk']['lock_days']}`；",
        f"- 手续费/滑点：`fees.*`（佣金 max({params['fees']['commission_bps']}bp, "
        f"¥{params['fees']['commission_min']})、滑点 {params['fees']['slippage_bps']}bp）。"]
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"weekly_{lo}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[weekly] → {path}")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true", help="生成每日晨报")
    ap.add_argument("--weekly", action="store_true", help="生成周末深度复盘")
    ap.add_argument("--asof", default=None, help="截止日期（默认最新台账日）")
    a = ap.parse_args()
    if a.weekly:
        weekly(a.asof)
    elif a.daily:
        daily(a.asof)
    else:
        print("用法：--daily（晨报） / --weekly（周末深度复盘）")
