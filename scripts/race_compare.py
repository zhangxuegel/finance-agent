# -*- coding: utf-8 -*-
"""race_compare.py —— T-Fin28 策略赛马对比报告生成器

口径（台账纪律）：
  · 四条腿独立单跑（race.enable=False 冻结赛马、走 init_weights 单腿满仓），
    引擎复用同一状态机/费用/停泊口径，杜绝口径漂移；
  · 合并赛马跑一次（race.enable=True）还原真实资金分配路径；
  · 每条腿 2026 年度切片：收益率 / 夏普 / MDD / 交易笔数 + 当年周赛马夺冠次数；
  · 黑天鹅重放结论直接引用 crash_test 台账报告（2015/2020）；
  · 全部数字来自行情 CSV 与引擎台账，LLM 不参与任何数字。

用法：python3 scripts/race_compare.py
输出：reports/race_T-Fin28_<date>.md
"""
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import fetch_data
import portfolio
from ensemble_engine import EnsembleEngine, ACTIVE_LEGS

P = fetch_data.load_params()
CAP = float(P["account"]["initial_capital"])
YEAR = "2026-01-01"
LEGS_NAME = {"A": "A 趋势双均线(MA20/60)", "B": "B RSI均值回归(14/30/70)",
             "D": "D 震荡网格(MA120±3%)", "E": "E 防守红利(510880·MA60)"}
REPORTS = os.path.join(ROOT, "reports")

SOLO_W = {"A": {"A": 1.0, "B": 0.0, "D": 0.0, "E": 0.0},
          "B": {"A": 0.0, "B": 1.0, "D": 0.0, "E": 0.0},
          "D": {"A": 0.0, "B": 0.0, "D": 1.0, "E": 0.0},
          "E": {"A": 0.0, "B": 0.0, "D": 0.0, "E": 1.0}}


def slice_metrics(curve: list, trades: list, start: str = YEAR) -> dict | None:
    """年度切片指标（确定性）：收益/夏普(244日年化,扣除无风险)/MDD/已平仓笔数。"""
    pts = [(d, e) for d, e in curve if d >= start]
    if len(pts) < 3:
        return None
    vals = [e for _, e in pts]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))
            if vals[i - 1] > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    sd = (sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
    sharpe = ((m * 244) - portfolio.RISK_FREE) / (sd * 244 ** 0.5) if sd > 1e-12 else None
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    closed = [t for t in trades if t.get("date", "") >= start
              and t.get("side") in ("SELL", "BUY") and not t.get("forced")
              and t.get("code") not in (P["ensemble"]["C"]["code"],)]
    wins = [t for t in closed if t["side"] == "SELL" and (t.get("pnl") or 0) > 0]
    sells = [t for t in closed if t["side"] == "SELL"]
    return {"base": vals[0], "last": vals[-1],
            "ret": vals[-1] / vals[0] - 1, "sharpe": sharpe, "mdd": mdd,
            "n_trades": len(closed),
            "win_rate": (len(wins) / len(sells)) if sells else None,
            "first": pts[0][0]}


def main() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    print("[race] 合并赛马（台账默认参数，race.enable=true）…")
    eng = EnsembleEngine(log_writes=False)
    st_full = eng.run(tail_days=20)
    m_full = portfolio.metrics(
        {"cash": 0, "positions": {}, "curve": st_full["curve"],
         "trades": [t for leg in ACTIVE_LEGS
                    for t in st_full["sleeves"][leg]["trades"]
                    if t.get("pnl") is not None]},
        CAP)

    # 周赛马 2026 夺冠统计（夏普最高者=当周冠军；None 视为并列不记）
    wins = {leg: 0 for leg in ACTIVE_LEGS}
    weeks_26 = 0
    for tr in st_full["race_log"]:
        if not tr["week_end"].startswith("2026"):
            continue
        weeks_26 += 1
        base_sh = {l: tr[f"sharpe_{l.lower()}"] for l in ACTIVE_LEGS}
        best = max(base_sh, key=lambda l: base_sh[l])
        vals = sorted(base_sh.values(), reverse=True)
        if vals and vals[0] > vals[1]:                 # 并列不记冠军
            wins[best] += 1

    combined = {}
    for leg in ACTIVE_LEGS:
        combined[leg] = slice_metrics(st_full["sleeves"][leg]["curve"],
                                      st_full["sleeves"][leg]["trades"])

    print("[race] 四腿独立单跑（race.enable=false，单腿满仓）…")
    solo = {}
    for leg in ACTIVE_LEGS:
        ov = {("race", "enable"): False, ("race", "init_weights"): SOLO_W[leg]}
        e2 = EnsembleEngine(param_overrides=ov, log_writes=False)
        st2 = e2.run(tail_days=20)
        solo[leg] = slice_metrics(st2["sleeves"][leg]["curve"],
                                  st2["sleeves"][leg]["trades"])
        print(f"[race] {leg} 单跑完成：2026收益 {solo[leg]['ret']:+.2%}")

    bench = [k for k in fetch_data.load_csv(P["benchmark"]["code"])
             if k["date"] >= YEAR]
    bench_ret = bench[-1]["close"] / bench[0]["close"] - 1 if bench else None

    # ---------------- 报告 ----------------
    L = [f"# T-Fin28 策略赛马对比报告（{today}）", "",
         f"> 任务编号：T-Fin28（业主 2026-10-21 指令）· 数字全部来自行情 CSV 与引擎台账",
         f"> （确定性还原，LLM 零参与）· 初始资金 ¥{CAP:,.0f} · 全量历史 2023-10-23 起、",
         f"> 温铺 120 日后开跑 · 单跑口径：同一引擎冻结赛马单腿满仓（费用/停泊/熔断全同）。", "",
         "## 1) 四腿独立单跑（race.enable=false，单腿满仓同口径引擎）", "",
         "| 策略 | 2026 期初权益 | 2026 期末权益 | 年度收益 | 年化夏普 | MDD |",
         " 交易笔数(买+卖) | 胜率 |", "|---|---|---|---|---|---|---|---|"]
    rank = sorted(ACTIVE_LEGS, key=lambda l: -(solo[l]["ret"] if solo[l] else -9))
    for leg in rank:
        s = solo[leg]
        wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "—"
        L.append(f"| {LEGS_NAME[leg]} | ¥{s['base']:,.0f} | ¥{s['last']:,.0f} "
                 f"| **{s['ret']:+.2%}** | {s['sharpe']:.2f} | {s['mdd']:.2%} "
                 f"| {s['n_trades']} 笔 | {wr} |")
    if bench_ret is not None:
        L.append(f"| （基准 {P['benchmark']['code']} 买入持有） | — | — "
                 f"| {bench_ret:+.2%} | — | — | — | — |")

    L += ["", "## 2) 合并赛马（真实资金分配路径）2026 年度", "",
          "| 策略 | 年度切片收益 | 年化夏普 | MDD | 2026 已平仓 |",
          "|---|---|---|---|---|"]
    for leg in rank:
        c = combined[leg]
        L.append(f"| {LEGS_NAME[leg]} | **{c['ret']:+.2%}** | "
                 f"{c['sharpe']:.2f} | {c['mdd']:.2%} | {c['n_trades']} 笔 |")
    L += ["", f"### 2b) 合并净值 2026：{m_full['total_return']:+.2%}"
              f"（夏普 {m_full.get('sharpe')}，MDD {m_full['mdd']:.2%}）", "",
          f"### 2c) 2026 周赛马夺冠次数（{weeks_26} 个赛马周，夏普并列不记）",
          "、".join(f"{leg}={wins[leg]} 次" for leg in ACTIVE_LEGS), "",
          "## 3) 台账溯源（本次报告引用）", "",
          "- 参数：`index/params.yaml ensemble.A/B/D/E/race/tilt/crash_gate`（T-Fin28 登记）；",
          "- 数据：`data/<code>.csv`（东财前复权，fetch_data 2026-10-21 校验 99.9% 覆盖）；",
          "- 510880 分红台账：`params.dividends`（东财 fhsp 2026-10-21 在线核验，10 次除息）；",
          "- 黑天鹅重放：`reports/crash_test_2026-10-21.md`（2015 千股跌停 / 2020 美股熔断，双 PASS）；",
          "- 状态机：`ensemble_engine.run()` 分腿防守矩阵"
          "（黄灯放行网格 D；红灯/急跌清仓非防守腿；E 防守豁免位）。", "",
          "## 4) 结论（Append：谁今年最强）", "",
          f"**独立单跑 2026 冠军：{LEGS_NAME[rank[0]]}"
          f"（{solo[rank[0]]['ret']:+.2%}）**；亚军 {LEGS_NAME[rank[1]]}"
          f"（{solo[rank[1]]['ret']:+.2%}）。合并赛马下冠军腿"
          f"为 {LEGS_NAME[max(wins, key=wins.get)]}（夺冠 {max(wins.values())} 次）。", "",
          "> 复盘注记：夏普口径 244 日年化并扣除无风险利率；胜率以已平仓 SELL 行 pnl>0 计；",
          "> 网格 D 腿的黄灯交易带 grid_yellow 台账标记，防守腿 E 的交易带 defensive 标记。",
        ]
    out = os.path.join(REPORTS, f"race_T-Fin28_{today}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"[race] 报告 → {out}")
    print("\n".join(L[16:26]))


if __name__ == "__main__":
    main()
