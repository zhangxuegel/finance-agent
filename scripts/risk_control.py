# -*- coding: utf-8 -*-
"""risk_control.py —— 冷酷无情的风控校验模块（Task 3 交付）

两条绝对熔断规则（台账 index/params.yaml risk.*，不得被优化器/LLM修改）：
  R1 单日亏损 >  daily_loss_cutoff(3%)     → 强制清仓 + 锁仓 3 个交易日
  R2 净值自峰值回撤 > max_drawdown_cutoff(10%) → 强制清仓 + 锁仓 3 个交易日

锁仓期内禁止一切买卖（强平动作本身除外）。状态持久化于 data/risk_state.json。
"""
import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "risk_state.json")


def load():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"locked": False, "lock_until_day": 0, "alarm_days": []}


def save(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def peak_equity(curve_slice: list) -> float:
    return max(eq for _, eq in curve_slice) if curve_slice else 0.0


def check_breakers(day_index: int, date: str, equity: float,
                   prev_equity: float, curve: list, params: dict) -> dict:
    """收盘后校验两条熔断；触发则返回 shield=1，由 portfolio 执行强平与锁仓。"""
    r1 = None
    r2 = None
    if prev_equity > 0 and (equity / prev_equity - 1) < -params["daily_loss_cutoff"]:
        r1 = f"单日亏损 {equity / prev_equity - 1:.2%} > {params['daily_loss_cutoff']:.0%} 阈值"
    peak = peak_equity([(d, e) for d, e in curve] + [(date, equity)])
    if peak > 0 and (peak - equity) / peak > params["max_drawdown_cutoff"]:
        r2 = f"自峰值回撤 {(peak - equity) / peak:.2%} > {params['max_drawdown_cutoff']:.0%} 阈值"
    tri = r1 or r2
    return {"triggered": bool(tri), "reason": tri,
            "rule": ("R1_daily_loss" if r1 else "R2_max_drawdown") if tri else None}


class RiskController:
    """锁仓状态机 + 熔断事件的账本。"""

    def __init__(self):
        self.st = load()

    def locked(self, day_index: int) -> bool:
        return self.st.get("locked", False) and day_index <= self.st.get("lock_until_day", -1)

    def trigger_lock(self, day_index: int, lock_days: int, reason: str, date: str):
        self.st.update({
            "locked": True, "lock_until_day": day_index + lock_days,
            "lock_reason": reason, "lock_from": date,
        })
        self.st.setdefault("alarm_days", []).append({
            "date": date, "day_index": day_index, "reason": reason,
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        save(self.st)

    def note_day(self, day_index: int):
        """每日收盘登记；锁仓期满自动解锁。"""
        if self.st.get("locked") and day_index > self.st.get("lock_until_day", -1):
            self.st["locked"] = False
            save(self.st)


# ===========================================================================
# T-Fin02 新增：防未来函数时间锁校验 + 高滑点地狱模式回测
# ===========================================================================

def audit_no_lookahead(params: dict | None = None,
                       probes: int | None = None) -> dict:
    """【信号口径时间锁】防未来函数校验（截断重算法）。

    原理：任何 t 日的信号只允许依赖 ≤ t 的数据。对若干探针日 t：
        sig_full(t)（全历史前向计算的信号）
      vs sig_cut(t)（把数据截断到 t 再重算的信号）
    两者必须一致；任一探针不一致 → 策略/撮合存在未来函数 → FAIL。
    返回 {"ok", "probes", "results": [{code, probes, mismatch:[t,...]}]}
    """
    import fetch_data
    import strategy

    p = params or fetch_data.load_params()
    n_probes = probes or p.get("stress", {}).get("lookahead_probes", 40)
    if p["strategy"]["engine"] == "A":
        warm = max(p["strategy"]["A"]["ma_fast"], p["strategy"]["A"]["ma_slow"])
    else:
        warm = p["strategy"]["B"]["window"]

    results = []
    for w in fetch_data.load_watchlist():
        kl = fetch_data.load_csv(w["code"])
        dates = [k["date"] for k in kl]
        full = strategy.signals_for(w["code"], kl, p["strategy"])
        anchor = min(warm, max(0, len(dates) - 1))
        idxs = sorted({anchor + int((k + 1) * (len(dates) - 1 - anchor) / n_probes)
                       for k in range(n_probes)})
        mismatch = []
        for t in idxs:
            cut = strategy.signals_for(w["code"], kl[:t + 1], p["strategy"])
            if abs(cut[-1]["signal"] - full[t]["signal"]) > 1e-12:
                mismatch.append(dates[t])
        results.append({"code": w["code"], "n_probes": len(idxs),
                        "mismatch": mismatch})
    return {"ok": all(not r["mismatch"] for r in results),
            "probes": n_probes, "results": results}


def audit_fill_time_lock(params: dict | None = None,
                         port_state: dict | None = None) -> dict:
    """【撮合口径时间锁】逐笔核对成交账本（T-Fin27 口径自洽版）：

      0. 复权口径：台账成交价 = 不复权 raw 口径（git e6f93c9 可证）。当前 CSV 为
         东财前复权（fqt=1），故审计先把 CSV 反推回 raw：
         raw(d) = qfq(d) + Σ{已登记分红、除息日>d 的每股派现}（分红台账 params.dividends；
         该反推对"未来新登记分红"具有不变性——补登不减旧，除息须及时登记）。
      1. BUY 价必须 = 成交日 raw_open×(1+slip)；SELL = raw_open×(1-slip)
         （熔断强平例外：raw_close×(1-slip)，账本带 forced 标记）
      2. 普通成交必须有信号依据：成交日**前一交易日**（raw 口径信号）须与方向一致
         （BUY ⇔ prev_sig=1，SELL ⇔ prev_sig=0）——"信号日收盘生成、次日开盘撮合"。
         例外：params.anchor.date 的首日等权锚定建仓（T-Fin01 设计，登记在台账）。
    """
    import fetch_data
    import strategy
    import portfolio

    p = params or fetch_data.load_params()
    slip = p["fees"]["slippage_bps"] / 10000
    # 账本价格按 4 位小数入账（round(px,4)，最大舍入误差 5e-5）→ 审计容差对齐该口径
    TOL = 5.1e-5
    div = p.get("dividends") or {}
    anchor_date = (p.get("anchor") or {}).get("date")
    port = port_state or portfolio.load_state()
    assert port and port.get("trades"), "账本为空，无法审计撮合时间锁"

    cache: dict[str, dict[str, dict]] = {}
    sigs: dict[str, dict[str, float]] = {}
    prev_day_map: dict[str, dict[str, str]] = {}

    def _recode(code: str):
        """把前复权 CSV 重构为不复权 raw 口径。"""
        kl = fetch_data.load_csv(code)
        evs = sorted(str(e.get("ex_date")) for e in div.get(code, []))
        out = []
        for k in kl:
            rebase = sum(float(str(div[code][j].get("per_share", 0)))
                         for j in range(len(evs)) if evs[j] > k["date"])
            out.append({**k, "open": k["open"] + rebase, "close": k["close"] + rebase,
                        "high": k["high"] + rebase, "low": k["low"] + rebase})
        return out

    def ensure(code: str):
        if code not in cache:
            kl = _recode(code)
            cache[code] = {k["date"]: k for k in kl}
            sig = strategy.signals_for(code, kl, p["strategy"])
            sigs[code] = {s["date"]: s["signal"] for s in sig}
            dates_in = [s["date"] for s in sig]
            prev_day_map[code] = {dates_in[i]: dates_in[i - 1] for i in range(1, len(dates_in))}

    bad = []
    exempt_anchor: set[tuple[str, str]] = set()
    for t in port["trades"]:
        ensure(t["code"])
        row = cache[t["code"]].get(t["date"])
        if row is None:
            bad.append({**t, "why": "成交日无行情行"})
            continue
        if t["side"] == "BUY":
            expect = row["open"] * (1 + slip)
            prev_sig = sigs[t["code"]].get(prev_day_map[t["code"]].get(t["date"], ""))
            if anchor_date and t["date"] == str(anchor_date):
                # 台账登记的开局锚定建仓（T-Fin01 设计）：豁免前一日信号校验
                exempt_anchor.add((str(t["code"]), str(t["date"])))
            elif prev_sig is None or abs(prev_sig - 1.0) > 1e-12:
                bad.append({**t, "why": "BUY 缺少前一交易日的满仓信号（时间锁违规）"})
            if abs(t["price"] - expect) > TOL:
                bad.append({**t, "why": f"买价≠当日开×(1+slip)（{t['price']} vs {expect:.6f}）"},)
        else:
            ref = row["close"] if t.get("forced") else row["open"]
            expect = ref * (1 - slip)
            if abs(t["price"] - expect) > TOL:
                bad.append({**t, "why": f"卖价≠{'收' if t.get('forced') else '开'}×(1-slip)"})
            if not t.get("forced"):
                prev_sig = sigs[t["code"]].get(prev_day_map[t["code"]].get(t["date"], ""))
                if prev_sig is None or abs(prev_sig) > 1e-12:
                    bad.append({**t, "why": "SELL 缺少前一交易日的空仓信号（时间锁违规）"})
    return {"ok": not bad, "checked": len(port["trades"]), "violations": bad, "anchor_exempt": len(exempt_anchor)}


def stress_high_slippage(params: dict | None = None,
                         multiplier: float | None = None) -> dict:
    """【高滑点地狱模式】滑点×N 倍全历史重放，检验流动性冲击下的生存能力。

    复用 optimizer 的内存重放（同一撮合/熔断口径）。
    生存判据（登记口径）：地狱 NAV 跌幅相对基准 ≤ 25%，且 MDD < 20%。
    """
    import copy as _copy
    import optimizer
    import portfolio

    p0, codes, dates, by_date = optimizer.prepare()
    mult = multiplier or p0.get("stress", {}).get("slippage_multiplier", 10)
    start = optimizer.warmup_index(p0)
    base = optimizer.replay_with(p0, codes, dates, by_date, {}, start)

    p_hell = _copy.deepcopy(p0)
    p_hell["fees"]["slippage_bps"] = p0["fees"]["slippage_bps"] * mult
    hell = optimizer.replay_with(p_hell, codes, dates, by_date, {}, start)

    init = p0["account"]["initial_capital"]
    mb = portfolio.metrics(base, init)
    mh = portfolio.metrics(hell, init)
    survived = (mh["nav"] and mb["nav"]
                and (mh["nav"] / mb["nav"] - 1) >= -0.25
                and (mh["mdd"] or 1) < 0.20)
    return {"multiplier": mult,
            "base": {k: mb[k] for k in ("nav", "total_return", "mdd", "sharpe",
                                        "sortino", "calmar", "n_trades_closed")},
            "hell": {k: mh[k] for k in ("nav", "total_return", "mdd", "sharpe",
                                        "sortino", "calmar", "n_trades_closed")},
            "survived": bool(survived),
            "note": ("地狱模式存活：策略有真实安全边际（价格冲击下仍守住本金线附近）"
                     if survived else
                     "地狱模式未存活：策略对滑点冲击过敏感，建议流动性更好的标的池")}


# ---------------------------------------------------------------------------
# CLI：python3 scripts/risk_control.py --audit  → 生成 reports/risk_audit_<date>.md
def _audit_main():
    import argparse
    from datetime import date as _date
    ap = argparse.ArgumentParser(description="T-Fin02 风控综合审计")
    ap.add_argument("--audit", action="store_true", help="时钟锁/地狱模式审计并出报告")
    a = ap.parse_args()
    if not a.audit:
        print(__doc__[:600])
        return 0

    print("[audit] 1) 防未来函数时间锁校验（信号口径，截断重算法）...")
    la = audit_no_lookahead()
    print(f"        探针×{la['probes']} 结果：{'PASS 无未来函数' if la['ok'] else 'FAIL ' + repr([r['mismatch'] for r in la['results'] if r['mismatch']])}")

    print("[audit] 2) 撮合口径时间锁（逐笔账本核对）...")
    fl = audit_fill_time_lock()
    print(f"        核对 {fl['checked']} 笔成交：{'PASS 全部合规' if fl['ok'] else 'FAIL ' + repr(fl['violations'][:3])}")

    print("[audit] 3) 高滑点地狱模式回测（slippage ×N）...")
    st = stress_high_slippage()
    print(f"        ×{st['multiplier']}：基准 NAV ¥{st['base']['nav']:,.0f} → 地狱 NAV "
          f"¥{st['hell']['nav']:,.0f}（{'存活' if st['survived'] else '未存活'}）")

    lines = ["# T-Fin02 风控综合审计报告",
             f"> 生成时间 {datetime.now():%Y-%m-%d %H:%M:%S} · "
             f"台账口径：index/params.yaml", ""]
    lines += ["## 1) 防未来函数时间锁校验（信号口径）", ""]
    lines.append(f"- 方法：截断重算法，探针日 ×{la['probes']}（台账 stress.lookahead_probes）")
    for r in la["results"]:
        if not r["mismatch"]:
            lines.append(f"- {r['code']}：{r['n_probes']} 探针，✔ 全部一致（无未来函数）")
        else:
            lines.append(f"- {r['code']}：❌ 不一致日 {r['mismatch'][:5]}")
    total_mism = [r["code"] for r in la["results"] if r["mismatch"]]
    lines += ["", f"**结论：{'✔ PASS 全库存无未来函数' if not total_mism else '❌ FAIL ' + str(total_mism)}**", ""]
    lines += ["## 2) 撮合口径时间锁（账本逐笔核对）", ""]
    lines.append(f"- 核对成交 {fl['checked']} 笔：买价=开×(1+slip)、卖价=开×(1-slip)、"
                 f"信号日=成交前一交易日、强平=收×(1-slip)（forced 标记豁免信号检查）")
    lines.append(f"**结论：{'✔ PASS 账本与时间锁完全一致' if fl['ok'] else '❌ FAIL 违规 ' + str(len(fl['violations'])) + ' 笔'}**")
    lines.append("")
    lines += ["## 3) 高滑点地狱模式回测", ""]
    lines.append(f"- 滑点倍数 ×{st['multiplier']}（台账 stress.slippage_multiplier）")
    lines.append(f"- 基准回测：NAV ¥{st['base']['nav']:,.0f}，MDD {st['base']['mdd']:.2%}，"
                 f"夏普 {st['base']['sharpe']}，Sortino {st['base']['sortino']}，Calmar {st['base']['calmar']}")
    lines.append(f"- 地狱回测：NAV ¥{st['hell']['nav']:,.0f}，MDD {st['hell']['mdd']:.2%}，"
                 f"夏普 {st['hell']['sharpe']}，Sortino {st['hell']['sortino']}，Calmar {st['hell']['calmar']}")
    lines.append(f"**结论：{'✔ 存活：流动性冲击下策略仍有边际' if st['survived'] else '⚠ 未存活：对滑点冲击过敏感'}**")
    lines.append("")
    lines += ["## 4) 概念图谱挂接（T-Fin01 ↔ T-Fin02）", "",
              "| 概念 | 挂接点 |", "|---|---|",
              "| 防未来函数时间锁 | risk_control.audit_no_lookahead + audit_fill_time_lock |",
              "| 高滑点地狱模式 | params.stress.slippage_multiplier → risk_control.stress_high_slippage |",
              "| 大盘信号灯 | regime_filter.traffic_light → daily_report 报告 0 区 |",
              "| Sortino / Calmar | portfolio.metrics → daily_report 绩效看板 |",
              "| 首席风控官 Prompt | daily_report.ANALYST_PROMPT + 敞口快照强制注入 |",
              "", "- 声明：本报告为模拟盘学习用途，不构成投资建议。"]

    path = os.path.join(ROOT, "reports", f"risk_audit_{_date.today().isoformat()}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[audit] → {path}")
    return 0 if (la["ok"] and fl["ok"]) else 1


if __name__ == "__main__":
    _audit_main()
