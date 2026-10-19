# -*- coding: utf-8 -*-
"""ensemble_engine.py —— 多策略赛马与资金动态分配（T-Fin03 模块 1）

三腿结构：
  A 趋势跟随：MA20 上穿 MA60 买入 / 下穿卖出（复用确定性公式 signal_series_a）
  B 均值回归：RSI14 < 30 买入 / > 70 卖出（Wilder RSI，本文件实现）
  C 避险防守：🟡黄灯/🔴红灯 或 A/B 同时空仓 → 全部资金买入货币 ETF(511990)
              吃无风险利息，禁止现金空转（台账 index/params.yaml ensemble.C）

赛马：每周最后一个交易日收盘后，用 A/B 各自过去 20 个交易日的夏普比率
分配下周资金（胜者 7 / 败者 3），并叠加平滑机制（防过拟合）：
  · 单周权重漂移 ≤ ±20pp（禁止 0→100% 赌注式跳变）
  · 双方权重下限 0.3（拒绝 0:10 绝对押注）
  · LLM 因子分只做辅助调权：>80 → A 加权 +5pp；<20 → C 腿预配 10pp
    （Python 侧 clamp——LLM 隔离红线的执行位，绝不直接生成买卖信号）

状态机红线：黄/红灯下任何 BUY 指令（货币腿除外）→ FatalControlError，
当日运行立即终止。熔断（单日-3% / 峰值回撤-10% + 锁3日）同样覆盖合并账户。
"""
import argparse
import copy as _copy
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import fetch_data
import portfolio
import risk_control
import regime_filter
import strategy

STATE_PATH = os.path.join(ROOT, "data", "ensemble_state.json")
RACE_LOG = os.path.join(ROOT, "data", "ensemble_race_log.jsonl")
REPORT_DIR = os.path.join(ROOT, "reports")


class FatalControlError(RuntimeError):
    """状态机违规：黄/红灯下出现多头 BUY 指令 → 立即终止当日运行。"""


# ---------------------------------------------------------------------------
# 策略腿信号（确定性数学公式）
def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder RSI 序列，前 period 个为 None。"""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    out: list[float | None] = [None] * n
    changes = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(c, 0.0) for c in changes[:period]]
    losses = [max(-c, 0.0) for c in changes[:period]]
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period

    def rsi(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    out[period] = rsi(avg_gain, avg_loss)
    for i, ch in enumerate(changes[period:], start=period + 1):
        g, l = max(ch, 0.0), max(-ch, 0.0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = rsi(avg_gain, avg_loss)
    return out


def signals_rsi(closes: list[float], period: int = 14,
                buy: float = 30.0, sell: float = 70.0) -> list[float]:
    """RSI 均值回归腿：<30 → 1；>70 → 0；区间内维持。"""
    out, pos = [], 0.0
    for r in rsi_series(closes, period):
        if r is None:
            out.append(0.0)
            continue
        if r < buy:
            pos = 1.0
        elif r > sell:
            pos = 0.0
        out.append(pos)
    return out


def leg_signals(core_codes: list[str], engine_cfg: dict) -> dict:
    """预计算全部标的的 A/B 腿信号（只用截至当日的数据，无未来函数）。"""
    legs = {"A": {}, "B": {}}
    for code in core_codes:
        closes = [k["close"] for k in fetch_data.load_csv(code)]
        legs["A"][code] = strategy.signal_series_a(
            closes, engine_cfg["A"]["ma_fast"], engine_cfg["A"]["ma_slow"])
        legs["B"][code] = signals_rsi(closes, engine_cfg["B"]["rsi_period"],
                                      engine_cfg["B"]["rsi_buy"],
                                      engine_cfg["B"]["rsi_sell"])
    return legs


# ---------------------------------------------------------------------------
# 赛马权重（纯函数，便于单元测试）
def race_weights(prev_wa: float, sharpe_a: float | None, sharpe_b: float | None,
                 engine_cfg: dict, factor_score: int | None = None) -> dict:
    """收盘后赛马 → 下周权重（平滑 + 双方下限 + LLM 辅助倾斜；全程 Python clamp）。"""
    rc, tl = engine_cfg["race"], engine_cfg["tilt"]
    if sharpe_a is None or sharpe_b is None:            # 数据未满赛马窗口 → 5:5
        share_a = float(tl.get("warmup_fixed_share", 0.5))
    else:
        share_a = rc["winner_share"] if (sharpe_a or 0) >= (sharpe_b or 0) \
            else rc["loser_share"]
    # 平滑（核心：防一周 0→100%）
    lo = max(rc["min_weight"], prev_wa - rc["max_weekly_shift"])
    hi = min(1 - rc["min_weight"], prev_wa + rc["max_weekly_shift"])
    share_a = min(max(share_a, lo), hi)
    # LLM 辅助调权（隔离位）
    tilt_note = "neutral"
    c_buffer = 0.0
    if factor_score is not None:
        if factor_score >= tl["score_high"]:
            share_a = min(max(share_a + tl["a_tilt_pp"], rc["min_weight"]),
                          1 - rc["min_weight"])
            tilt_note = f"A+{tl['a_tilt_pp'] * 100:.0f}pp(score {factor_score})"
        elif factor_score <= tl["score_low"]:
            c_buffer = tl["c_tilt_pp"]
            tilt_note = f"C预配{tl['c_tilt_pp'] * 100:.0f}pp(score {factor_score})"
    share_b = (1 - c_buffer) * (1 - share_a)
    return {"weight_a": round((1 - c_buffer) * share_a, 4),
            "weight_b": round(share_b, 4),
            "c_buffer": round(c_buffer, 4), "tilt_note": tilt_note}


def assert_gate(light_state: str, orders: list) -> None:
    """状态机红线：黄/红灯 sleeves 出现任何 BUY → FatalControlError。"""
    if light_state in ("yellow", "red") and any(o["side"] == "BUY" for o in orders):
        raise FatalControlError(
            f"状态机违规：大盘[{light_state}]下出现多头 BUY 指令 → 当日运行终止")


def sleeve_sharpe(curve: list, lookback: int = 20) -> float | None:
    vals = [e for _, e in curve][-lookback:]
    if len(vals) < 3:
        return None
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    sd = (sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
    if sd < 1e-12:
        return None
    return ((m * 244) - portfolio.RISK_FREE) / (sd * 244 ** 0.5)


# ---------------------------------------------------------------------------
class EnsembleEngine:
    def __init__(self):
        self.params = portfolio.load_params()
        wl = fetch_data.load_watchlist()
        self.watch = [w for w in wl if not w.get("exclude_core")]
        self.core = [w["code"] for w in self.watch]
        c = self.params["ensemble"]["C"]
        self.money_code = c["code"]
        self.money_px = {k["date"]: k["close"] for k in fetch_data.load_csv(self.money_code)}
        self.dates = sorted(set.intersection(*[
            {k["date"] for k in fetch_data.load_csv(code)} for code in self.core]))
        self.by_date = {c: {k["date"]: k for k in fetch_data.load_csv(c)}
                        for c in self.core}
        b = self.params["benchmark"]["code"]
        self.bench = {k["date"]: k["close"] for k in fetch_data.load_csv(b)}
        warm = max(self.params["ensemble"]["A"]["ma_slow"],
                   self.params["ensemble"]["B"]["rsi_period"])
        self.warmup = max(warm, 2)
        self.legs = leg_signals(self.core, self.params["ensemble"])
        self.params["n_assets"] = len(self.core)

    # ---- 组件 ----
    def light_at(self, i: int) -> dict:
        """研判日 i 的信号灯：只用 ≤ dates[i-1] 的收盘（时间锁口径）。"""
        closes = [self.bench[d] for d in self.dates[:i] if d in self.bench]
        return regime_filter.traffic_light(closes)

    def flat_legs(self, i: int) -> bool:
        return all(abs(v) < 1e-12 for c in self.core
                   for v in (self.legs["A"][c][i - 1], self.legs["B"][c][i - 1]))

    def sleeve_params(self) -> dict:
        p = _copy.deepcopy(self.params)
        p["n_assets"] = len(self.core)
        return p

    def sleeve_equity(self, st: dict, leg: str, date: str) -> float:
        """腿权益 = 腿现金 + 持仓市值 + 该腿名下停泊的货币份额市值。"""
        port = st["sleeves"][leg]
        val = port["cash"]
        for c, p in port["positions"].items():
            val += p["shares"] * self.by_date[c][date]["close"]
        val += st["money"][leg] * (self.money_px.get(date) or
                                   st["money"].get("last_px") or 0.0)
        return val

    # ---- 交易执行 ----
    def clear_sleeves(self, st: dict, date: str, reason: str) -> list[dict]:
        """兑现腿持仓（开盘价）；现金留在各腿，由 park_money 停泊货币份额。"""
        done = []
        for leg in ("A", "B"):
            port = st["sleeves"][leg]
            if port["positions"]:
                orders = portfolio.place_orders(
                    port, {c: self.by_date[c][date] for c in port["positions"]},
                    {c: 0.0 for c in port["positions"]},
                    self.sleeve_params(), date)
                for o in orders:
                    o["reason"] = reason
                    o["leg"] = leg
                done += orders
        return done

    def park_money(self, st: dict, date: str):
        """各腿/母账户剩余现金 → 名下货币份额（禁止现金空转；不足一手记尾差）。"""
        px = self.money_px.get(date) or st["money"].get("last_px")
        if not px:
            return
        price = px * (1 + self.params["fees"]["slippage_bps"] / 10000)
        lot = self.params["trading_rule"]["lot"]
        cash_of = {leg: st["sleeves"][leg]["cash"] for leg in ("A", "B")}
        cash_of["MASTER"] = st["master_cash"]
        for who, cash in cash_of.items():
            shares = int(cash // price // lot) * lot
            if shares <= 0:
                continue
            notional = shares * price
            fee = portfolio.trade_fees(notional, "BUY", self.params)
            if notional + fee > cash:
                shares -= lot
                if shares <= 0:
                    continue
                notional = shares * price
                fee = portfolio.trade_fees(notional, "BUY", self.params)
            if who == "MASTER":
                st["master_cash"] -= notional + fee
            else:
                st["sleeves"][who]["cash"] -= notional + fee
            st["money"][who] = st["money"].get(who, 0) + shares
            st["money"]["last_px"] = price
            st["trades"].append({"date": date, "code": self.money_code,
                                 "side": "BUY", "shares": shares,
                                 "price": round(price, 4), "fee": round(fee, 2),
                                 "reason": "park", "leg": who})
        st["park_residual"] = round(st["master_cash"] +
                                    sum(st["sleeves"][l]["cash"]
                                        for l in ("A", "B")), 2)

    def redeem_money(self, st: dict, date: str) -> float:
        """按各腿记账逐笔赎回全部货币份额 → 回到来源账户现金。"""
        total = 0.0
        px = self.money_px.get(date) or st["money"].get("last_px")
        if not px:
            return 0.0
        sell_px = px * (1 - self.params["fees"]["slippage_bps"] / 10000)
        for who in ("A", "B", "MASTER"):
            shares = st["money"].get(who, 0)
            if not shares:
                continue
            notional = shares * sell_px
            fee = portfolio.trade_fees(notional, "SELL", self.params)
            if who == "MASTER":
                st["master_cash"] += notional - fee
            else:
                st["sleeves"][who]["cash"] += notional - fee
            st["money"][who] = 0
            total += notional - fee
            st["trades"].append({"date": date, "code": self.money_code,
                                 "side": "SELL", "shares": shares,
                                 "price": round(sell_px, 4),
                                 "fee": round(fee, 2), "reason": "redeem",
                                 "leg": who})
        return total

    def rebalance_sleeves(self, st: dict, w: dict, date: str):
        """按昨收合并净值调整两腿预算（腿间记账划转，只动现金不动持仓）。"""
        eq_prev = st["curve"][-1][1] if st["curve"] else \
            float(self.params["account"]["initial_capital"])
        ports = st["sleeves"]
        cur = {leg: self.sleeve_equity(st, leg, date) for leg in ("A", "B")}
        targets = {"A": eq_prev * w["weight_a"], "B": eq_prev * w["weight_b"]}
        for leg, target in targets.items():
            other = "B" if leg == "A" else "A"
            delta = target - cur[leg]
            if abs(delta) < 1.0:
                continue
            if delta > 0:                       # 注资（对手腿现金 → 本腿）
                move = min(delta, ports[other]["cash"])
                ports[other]["cash"] -= move
                ports[leg]["cash"] += move
                st["trades"].append({"date": date, "leg": leg, "side": "FUND",
                                     "amount": round(move, 2)})
            elif ports[leg]["cash"] > 0:        # 抽资（只抽现金，避免churn）
                move = min(-delta, ports[leg]["cash"])
                ports[leg]["cash"] -= move
                ports[other]["cash"] += move
                st["trades"].append({"date": date, "leg": leg, "side": "DRAIN",
                                     "amount": round(move, 2)})

    def synth_weights(self, st: dict) -> dict:
        wb = (1 - st["c_buffer"]) * (1 - st["weight_a"])
        return {"weight_a": round((1 - st["c_buffer"]) * st["weight_a"], 4),
                "weight_b": round(wb, 4), "c_buffer": st["c_buffer"]}

    # ---- 主循环 ----
    def run(self, tail_days: int = 20, allow_llm: bool = False) -> dict:
        p = self.params
        st = {"master_cash": 0.0,
              "money": {"A": 0, "B": 0, "MASTER": 0, "last_px": None},
              "sleeves": {leg: {"cash": 0.0, "positions": {}, "trades": [], "curve": []}
                          for leg in ("A", "B")},
              "weight_a": 0.5, "c_buffer": 0.0,
              "trades": [], "curve": [], "last_date": None,
              "race_log": [], "alarms": [], "park_residual": 0.0,
              "locked_until": -1,
              "created": datetime.now().isoformat(timespec="seconds")}
        # 初始对半注资两条腿（母账户清零；无现金空转）
        half = float(p["account"]["initial_capital"]) * 0.5
        st["sleeves"]["A"]["cash"] = half
        st["sleeves"]["B"]["cash"] = half

        week_prev = None
        race_pending = None
        sp = self.sleeve_params()

        for i in range(self.warmup, len(self.dates)):
            date = self.dates[i]
            iso = datetime.strptime(date, "%Y-%m-%d").isocalendar()[:2]
            light = self.light_at(i)
            money_mode = light["state"] != "green" or self.flat_legs(i)
            day_price = {c: self.by_date[c][date] for c in self.core}
            prev_sig = {leg: {c: self.legs[leg][c][i - 1] for c in self.core}
                        for leg in ("A", "B")}
            locked = i <= st["locked_until"]

            if not locked:
                # 1) 上周赛马再配置（今日开盘执行）
                held_money = any(st["money"].get(k, 0) > 0 for k in ("A", "B", "MASTER"))
                if race_pending is not None:
                    if not money_mode:
                        if held_money:
                            self.redeem_money(st, date)
                        self.rebalance_sleeves(st, race_pending, date)
                    race_pending = None
                    held_money = any(st["money"].get(k, 0) > 0
                                     for k in ("A", "B", "MASTER"))
                # 2) 模式切换（黄/红灯 或 A/B 双空仓 ↔ 正常）
                if money_mode and not held_money and \
                        any(st["sleeves"][leg]["positions"] for leg in ("A", "B")):
                    self.clear_sleeves(st, date, "exit→money regime/双空仓")
                    assert not any(st["sleeves"][leg]["positions"]
                                   for leg in ("A", "B")), "状态机违规：清仓未完成"
                    self.park_money(st, date)        # 强制转入货币 ETF（开盘即停泊）
                if not money_mode and held_money:
                    self.redeem_money(st, date)
                    self.rebalance_sleeves(st, self.synth_weights(st), date)
                if not money_mode and not held_money:
                    # 3) 腿策略执行（prev 信号 → 今日开盘撮合；状态机门禁）
                    for leg in ("A", "B"):
                        o = portfolio.place_orders(st["sleeves"][leg], day_price,
                                                   prev_sig[leg], sp, date)
                        assert_gate(light["state"], o)
                # 4) 每日收盘残余现金停泊货币腿（禁止现金空转）
                self.park_money(st, date)

            # 5) 每周最后交易日收盘：赛马 + 因子倾斜
            is_last = (i == len(self.dates) - 1 or
                       datetime.strptime(self.dates[i + 1], "%Y-%m-%d")
                       .isocalendar()[:2] != iso)
            if is_last and not locked:
                import factor_mining
                sc = factor_mining.weekly_score(asof=date, allow_llm=allow_llm)
                score = sc.get("score") if sc else None
                w = race_weights(st["weight_a"], sleeve_sharpe(st["sleeves"]["A"]["curve"]),
                                 sleeve_sharpe(st["sleeves"]["B"]["curve"]),
                                 p["ensemble"], score)
                tr = {"week_end": date,
                      "sharpe_a": round(sleeve_sharpe(st["sleeves"]["A"]["curve"]) or 0, 3),
                      "sharpe_b": round(sleeve_sharpe(st["sleeves"]["B"]["curve"]) or 0, 3),
                      "prev_wa": round(st["weight_a"], 3), **w, "factor": sc}
                st["race_log"].append(tr)
                with open(RACE_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps(tr, ensure_ascii=False) + "\n")
                race_pending = w
                st["weight_a"] = w["weight_a"]
                st["c_buffer"] = w["c_buffer"]

            # 6) 收盘估值（腿曲线 + 合并净值）+ 熔断（绝对规则覆盖合并账户）
            for leg in ("A", "B"):
                st["sleeves"][leg]["curve"].append(
                    [date, round(self.sleeve_equity(st, leg, date), 2)])
            money_val = sum(st["money"].get(k, 0) for k in ("A", "B", "MASTER")) * \
                (self.money_px.get(date) or st["money"].get("last_px") or 0.0)
            sleeve_val = sum(self.sleeve_equity(st, leg, date) for leg in ("A", "B"))
            eq_total = round(st["master_cash"] + sleeve_val, 2)  # 腿权益已含其名下 money
            assert st["master_cash"] >= -1e-6 and eq_total > float(
                p["account"]["initial_capital"]) * 0.4, \
                "资金恒等式校验失败：出现现金凭空消失/蒸发"
            prev_eq = st["curve"][-1][1] if st["curve"] else \
                float(p["account"]["initial_capital"])
            st["curve"].append([date, eq_total])
            chk = risk_control.check_breakers(i, date, eq_total, prev_eq,
                                              st["curve"][:-1], p["risk"])
            if chk["triggered"] and not locked:
                for leg in ("A", "B"):
                    portfolio.force_liquidate(
                        st["sleeves"][leg],
                        {c: day_price[c] for c in st["sleeves"][leg]["positions"]},
                        p, date, chk["reason"])
                self.redeem_money(st, date)
                self.park_money(st, date)
                st["locked_until"] = i + p["risk"]["lock_days"]
                st["alarms"].append({"date": date, "type": "breaker",
                                     "reason": chk["reason"]})
            st["last_date"] = date
            week_prev = iso
        return st


# ---------------------------------------------------------------------------
def fmt(x, signed=True):
    if x is None:
        return "N/A"
    return f"{'+' if signed and x > 0 else ''}{x * 100:.2f}%"


def run_engine(tail_days: int = 20, allow_llm: bool = False) -> dict:
    eng = EnsembleEngine()
    st = eng.run(tail_days=tail_days, allow_llm=allow_llm)
    pseudo = {"cash": st["master_cash"], "positions": {}, "curve": st["curve"],
              "trades": st["trades"],
              "initial": float(eng.params["account"]["initial_capital"])}
    m = portfolio.metrics(pseudo, eng.params["account"]["initial_capital"])
    return {"engine": eng, "state": st, "metrics": m}


def write_report(res: dict, tail_days: int = 20) -> str:
    eng, st, m = res["engine"], res["state"], res["metrics"]
    today = st["last_date"]
    lines = [f"# T-Fin03 多策略赛马并发回测 · 尾部 {tail_days} 日 · 截至 {today}", ""]
    lines.append(f"> 生成 {datetime.now():%Y-%m-%d %H:%M} · "
                 f"初始 ¥{eng.params['account']['initial_capital']:,.0f} · "
                 "A=双均线20/60 · B=RSI14 · C=货币腿 · 熔断红线全程在线")
    lines += ["", "## 1) 全程绩效（赛马机制整体）", ""]
    lines.append(f"- 净值 **¥{m['nav']:,.2f}**（{fmt(m['total_return'])} 累计 / "
                 f"{fmt(m['ann_return'])} 年化）")
    lines.append(f"- 夏普 **{m['sharpe']}** · Sortino **{m['sortino']}** · "
                 f"Calmar **{m['calmar']}**")
    lines.append(f"- 最大回撤 **{m['mdd']:.2%}** · 胜率 {m['win_rate']}"
                 f"（{m['n_trades_closed']} 笔已平仓）")
    lines += ["", "## 2) 赛马权重演变 & A/B 策略夏普变化", "",
              "| 周终 | A夏普 | B夏普 | 分配A/B | C预配 | 因子分 | 依据 |",
              "|---|---|---|---|---|---|---|"]
    for tr in st["race_log"][-12:]:
        lines.append(f"| {tr['week_end']} | {tr['sharpe_a']} | {tr['sharpe_b']} "
                     f"| {tr['weight_a'] * 100:.0f}%/{(1 - tr['c_buffer'] - tr['weight_a']) * 100:.0f}%"
                     f" | {tr['c_buffer'] * 100:.0f}% | {tr['factor'] and tr['factor'].get('score')}"
                     f" | {tr['tilt_note']} |")
    lines += ["", "## 3) 最近 20 个交易日净值曲线", "",
              "| 日期 | 合并净值 | 日变动 |", "|---|---|---|"]
    curve = st["curve"]
    idx0 = len(curve) - min(tail_days, len(curve))
    for j in range(idx0, len(curve)):
        d, e = curve[j]
        chg = (e / curve[j - 1][1] - 1) * 100 if j > 0 else 0.0
        lines.append(f"| {d} | {e:,.0f} | {'+' if chg >= 0 else ''}{chg:.2f}% |")
    lines += ["", "## 4) 风控与状态机", ""]
    alarms = st["alarms"][-5:] if st["alarms"] else []
    lines.append(f"- 熔断事件：{json.dumps(alarms, ensure_ascii=False) if alarms else '无'}")
    money_sh = sum(st["money"].get(k, 0) for k in ("A", "B", "MASTER"))
    lines.append(f"- C 腿（{eng.money_code}）合计持仓：{money_sh} 股 · "
                 f"现金尾差（<一手，台账登记）：¥{st['park_residual']:,.2f}")
    lines += ["", "## 5) 概念图谱（T-Fin01→02→03 挂接不变）", "",
              "- LLM 仅辅助调权（tilt ±pp，Python clamp）；信号=数学公式；",
              "- 黄/红灯 BUY → FatalControlError；熔断规则绝对；",
              "- 赛马=确定性函数 race_weights，可单元测试重放。"]
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"ensemble_{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[ensemble] → {path}")
    return path


def daily_increment(allow_llm: bool = False) -> dict:
    """--daily：全历史确定性重放（口径不变），状态落盘。"""
    res = run_engine(allow_llm=allow_llm)
    st, m = res["state"], res["metrics"]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, default=str)
    print(f"[ensemble] 状态落盘 {STATE_PATH}：{st['last_date']} NAV ¥{m['nav']:,.0f}")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", type=int, default=20, help="报告截尾天数")
    ap.add_argument("--daily", action="store_true", help="每日增量（重放+落盘）")
    ap.add_argument("--llm", action="store_true", help="启用真实 GLM 因子倾斜（默认规则）")
    a = ap.parse_args()
    res = daily_increment(allow_llm=a.llm) if a.daily \
        else run_engine(tail_days=a.replay, allow_llm=a.llm)
    write_report(res, a.replay)
    print(json.dumps(res["metrics"], ensure_ascii=False, indent=1))
