# -*- coding: utf-8 -*-
"""ensemble_engine.py —— 多策略赛马与资金动态分配（T-Fin03 模块 1 / T-Fin28 升级）

四腿结构（T-Fin28 业主指令 2026-10-21）：
  A 趋势跟随：MA20 上穿 MA60 买入 / 下穿卖出（复用确定性公式 signal_series_a）
  B 均值回归：RSI14 < 30 买入 / > 70 卖出（Wilder RSI，本文件实现）
  D 震荡网格：MA120 基线起步，跌破 基线×(1-3%) 建仓并以该收盘为格点；
              站上 格点×(1+3%) 清仓重布网（单格级联，0/1 信号，无未来函数）
  E 防守红利：510880 红利ETF 单标的；close ≥ 自身 MA60 持有，否则空仓。
              [T-Fin28 状态机豁免位] 黄/红灯不强清，按自身公式交易（台账登记）。
  C 避险防守：🟡黄灯/🔴红灯 或 A/B/D 核心池同空仓 → 核心腿资金全部买入
              货币 ETF(511990) 吃无风险利息，禁止现金空转（台账 index/params.yaml ensemble.C）

赛马（T-Fin28 维持每周五收盘，4 腿并入）：按 A/B/D/E 各自过去 20 个交易日的
夏普比率排名分配下周资金（台账 race.shares t1..t4），并叠加平滑机制：
  · 单周单腿权重漂移 ≤ ±max_weekly_shift（禁 0→100% 赌注式跳变）
  · 单腿权重下限 race.min_weight（防守腿 E 另有 def_floor 更高地板）
  · LLM 因子分只做辅助调权：>80 → A 加权 5pp；<20 → C 腿预配 10pp
    （Python 侧 clamp——LLM 隔离红线的执行位，绝不直接生成买卖信号）

状态机红线（T-Fin28 修订）：黄/红灯下任何 BUY 指令（核心腿 A/B/D，货币腿除外）
→ FatalControlError，当日运行立即终止；E 防守腿按自身公式豁免（trade 打
defensive 标记，validate V3 放行防御腿专项检查）。熔断（单日-3% / 峰值回撤
-10% + 锁3日）绝对覆盖合并账户与全部四腿。
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


def signals_grid(closes: list[float], anchor_window: int = 120,
                 step_pct: float = 0.03) -> list[float]:
    """震荡网格腿（D，T-Fin28）：MA(anchor_window) 基线起步，±step% 单格级联。

    确定性推进（只用 ≤i 的收盘，无未来函数）：
      · 空仓：收盘 ≤ 基线×(1-step) → 建仓，格点 = 该收盘（首次咬合正式布网）
      · 持仓：收盘 ≥ 格点×(1+step) → 清仓，格点 = 该收盘（随后若无网格信号，
        基线回归 MA 慢速漂移等待下一次咬合）
    信号 0/1；信号由收盘生成，撮合在下一交易日开盘（统一口径）。"""
    out, pos, grid = [], 0.0, None
    for i, c in enumerate(closes):
        if i + 1 < anchor_window:
            out.append(0.0)
            continue
        anchor = sum(closes[i + 1 - anchor_window:i + 1]) / anchor_window
        base = grid if (pos and grid) else anchor * (1 - step_pct)
        if pos:
            if c >= grid * (1 + step_pct):
                pos, grid = 0.0, c
        elif c <= base:
            pos, grid = 1.0, c
        out.append(pos)
    return out


def signals_defensive(closes: list[float], ma_window: int = 60) -> list[float]:
    """防守红利腿（E，T-Fin28）：自身标的 close ≥ MA(ma_window) → 1，否则 0。

    这是唯一允许穿越黄/红灯的腿（台账 ensemble.E.defensive=True）：
    防守语义 = 红灯风险下保持红利现金流敞口，而非全面退出现金。"""
    out, pos = [], 0.0
    for i, c in enumerate(closes):
        if i + 1 < ma_window:
            out.append(0.0)
            continue
        ma = sum(closes[i + 1 - ma_window:i + 1]) / ma_window
        if c >= ma:
            pos = 1.0
        else:
            pos = 0.0
        out.append(pos)
    return out


ACTIVE_LEGS = ("A", "B", "D", "E")     # T-Fin28：四条赛马腿
CORE_LEGS = ("A", "B", "D")            # 核心池腿（黄/红灯/急跌强制清仓位）
DEF_LEG = "E"                          # 防守豁免腿（红灯不清仓）


def leg_signals(core_codes: list[str], engine_cfg: dict,
                data_dir: str | None = None, div_code: str | None = None
                ) -> dict:
    """预计算全部标的的 A/B/D/E 腿信号，按日期索引（窗口模式安全，无未来函数：
    每个日期的信号只用该日期及之前的收盘价）。"""
    legs = {"A": {}, "B": {}, "D": {}, "E": {}}
    for code in core_codes:
        kl = fetch_data.load_csv(code, data_dir)
        dates = [k["date"] for k in kl]
        closes = [k["close"] for k in kl]
        legs["A"][code] = dict(zip(dates, strategy.signal_series_a(
            closes, engine_cfg["A"]["ma_fast"], engine_cfg["A"]["ma_slow"])))
        legs["B"][code] = dict(zip(dates, signals_rsi(
            closes, engine_cfg["B"]["rsi_period"],
            engine_cfg["B"]["rsi_buy"], engine_cfg["B"]["rsi_sell"])))
        legs["D"][code] = dict(zip(dates, signals_grid(
            closes, engine_cfg["D"]["anchor_window"],
            engine_cfg["D"]["step_pct"])))
    if div_code:                                     # E 腿：防守红利单标的
        kl = fetch_data.load_csv(div_code, data_dir)
        dates = [k["date"] for k in kl]
        closes = [k["close"] for k in kl]
        legs["E"][div_code] = dict(zip(dates, signals_defensive(
            closes, engine_cfg["E"].get("ma_window", 60))))
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


def race_weights4(prev_w: dict, sharpes: dict, cfg: dict,
                  factor_score: int | None = None) -> dict:
    """赛马（T-Fin28，4 腿 A/B/D/E 每周收盘）：
    夏普排名 → 台账 race.shares 目标位 → 平滑 clamp → LLM 倾斜 → 精确归一。

    纯函数；weight 总和恒等于 1 - c_buffer（LLM 只动倾斜位）。"""
    rc, tl = cfg["race"], cfg["tilt"]
    legs = list(ACTIVE_LEGS)
    shares = rc.get("shares") or {"t1": 0.35, "t2": 0.30, "t3": 0.20, "t4": 0.15}
    init_w = rc.get("init_weights") or {"A": 0.30, "B": 0.25, "D": 0.25, "E": 0.20}
    min_w = float(rc.get("min_weight", 0.10))
    def_floor = float(rc.get("def_floor", 0.15))
    floor = {leg: (def_floor if leg == DEF_LEG else min_w) for leg in legs}
    shift = float(rc.get("max_weekly_shift", 0.15))

    notes: list[str] = []
    if any(sharpes.get(l) is None for l in legs):      # 热身不足 → 台账 init_weights
        target = {leg: float(init_w.get(leg, 1 / len(legs))) for leg in legs}
        notes.append(f"warmup→固定init {init_w}")
    else:
        rank = sorted(legs, key=lambda l: (-(sharpes[l] or 0.0), legs.index(l)))
        order = {l: i for i, l in enumerate(rank)}
        target = {leg: float(shares[f"t{order[leg] + 1}"]) for leg in legs}
        notes.append(f"rank {rank}→shares {shares}")

    # 平滑：单周单腿漂移 ≤ ±shift
    target = {leg: min(max(target[leg], prev_w.get(leg, target[leg]) - shift),
                       prev_w.get(leg, target[leg]) + shift) for leg in legs}
    # 板块地板/帽 clamp+归一（逐腿地板下限；帽 = 1 - 其余腿地板之和）
    w = {leg: min(max(target[leg], floor[leg]),
                  1 - sum(floor[o] for o in legs if o != leg)) for leg in legs}
    room_total = 1.0 - sum(w.values())
    if abs(room_total) > 1e-12:                        # 按剩余空间比例分摊
        rooms = {leg: max(w[leg] - floor[leg] if room_total < 0 else
                          1 - sum(floor[o] for o in legs if o != leg) - w[leg],
                          0.0) for leg in legs}
        s = sum(rooms.values()) or 1.0
        w = {leg: w[leg] + room_total * rooms[leg] / s for leg in legs}
    # LLM 辅助倾斜（隔离位：Python clamp；score≤20 → C 腿预配 buffer）
    tilt_note = "neutral"
    c_buffer = 0.0
    if factor_score is not None:
        if factor_score >= tl["score_high"]:
            add = float(tl["a_tilt_pp"])
            w["A"] = min(w["A"] + add, 1 - sum(floor[l] for l in legs if l != "A"))
            give = {leg: max(w[leg] - floor[leg], 0.0)
                    for leg in legs if leg != "A"}
            gs = sum(give.values()) or 1.0
            for leg in give:                            # 从其他腿等比例让出
                w[leg] -= add * give[leg] / gs
            tilt_note = f"A+{add * 100:.0f}pp(score {factor_score})"
        elif factor_score <= tl["score_low"]:
            c_buffer = float(tl["c_tilt_pp"])
            tilt_note = f"C预配{c_buffer * 100:.0f}pp(score {factor_score})"
    # 精确归一（残差注入最大腿，保证 Σw == 1 - c_buffer 恰由浮点舍入补齐）
    w = {leg: round((1 - c_buffer) * w[leg], 6) for leg in legs}
    big = max(w, key=w.get)
    w[big] = round(w[big] + (1 - c_buffer) - sum(w.values()), 6)
    return {"weights": w, "c_buffer": round(c_buffer, 4),
            "tilt_note": tilt_note, "note": "; ".join(notes)}


class EnsembleEngine:
    def __init__(self, param_overrides: dict | None = None,
                 window: tuple[str, str] | None = None,
                 data_dir: str | None = None, log_writes: bool = True):
        """param_overrides：仅覆盖 ensemble.* 叶子参数（optimizer 网格用）。
        window=(start,end)：窗口重放（crash_test 用；信号与灯色仍用窗口内
        日期之前的全部历史，保证无未来函数）。data_dir：独立数据目录
        （如 data/crash 长历史黑天鹅，不污染正式台账）。log_writes=False
        时不写 data/ensemble_race_log.jsonl（批量寻优/崩溃测试防污染台账）。"""
        self.params = portfolio.load_params()
        if param_overrides:
            for k, v in param_overrides.items():
                node = self.params["ensemble"]
                sub = k[0]
                if len(k) == 2 and isinstance(node.get(sub), dict):
                    node[sub][k[1]] = v
                else:
                    raise ValueError(f"非法覆盖键：{k}（只允许 ensemble.<亚层>.<叶子>）")
        self.window, self.data_dir, self.log_writes = window, data_dir, log_writes
        wl = fetch_data.load_watchlist()
        self.watch = [w for w in wl if not w.get("exclude_core")
                      and not w.get("defensive")]   # T-Fin28：defensive 仅 E 腿用
        self.core = [w["code"] for w in self.watch]
        _defs = [w for w in wl if w.get("defensive")]
        self.div_entry = _defs[0] if _defs else None
        self.div_code = self.div_entry["code"] if self.div_entry else None
        c = self.params["ensemble"]["C"]
        self.money_code = c["code"]
        self.money_px = {k["date"]: k["close"]
                         for k in fetch_data.load_csv(self.money_code, data_dir)}
        self.dates = sorted(set.intersection(*[
            {k["date"] for k in fetch_data.load_csv(code, data_dir)}
            for code in self.core]))
        if window:
            self.dates = [d for d in self.dates if window[0] <= d <= window[1]]
        self.by_date = {c: {k["date"]: k for k in fetch_data.load_csv(c, data_dir)}
                        for c in self.core}
        if self.div_code:                # E 腿行情映射（防守红利单标的）
            _dkl = fetch_data.load_csv(self.div_code, data_dir)
            self.by_date_div = {self.div_code: {k["date"]: k for k in _dkl}}
            self.div_close = {k["date"]: k["close"] for k in _dkl}
        else:
            self.by_date_div, self.div_close = {}, {}
        b = self.params["benchmark"]["code"]
        self.bench = {k["date"]: k["close"] for k in fetch_data.load_csv(b, data_dir)}
        # 因子分可用下界：正式台账基准首个有效日（窗口外日期不启用因子倾斜）
        self.fac_min = fetch_data.load_csv(b)[0]["date"]
        if window:
            # 窗口模式：信号已在更长历史上热身，起跑只需赛马回看窗口
            self.warmup = max(self.params["ensemble"]["race"]["lookback"], 5)
        else:
            warm = max(self.params["ensemble"]["A"]["ma_slow"],
                       self.params["ensemble"]["B"]["rsi_period"],
                       self.params["ensemble"].get("D", {}).get("anchor_window", 120),
                       self.params["ensemble"].get("E", {}).get("ma_window", 60))
            self.warmup = max(warm, 2)
        self.legs = leg_signals(self.core, self.params["ensemble"], data_dir,
                                div_code=self.div_code)
        self.closes_by_date = {c: {k["date"]: k["close"]
                                   for k in fetch_data.load_csv(c, data_dir)}
                               for c in self.core}
        self.params["n_assets"] = len(self.core)

    # ---- 组件 ----
    def light_at(self, i: int) -> dict:
        """研判日 i 的信号灯：只用 ≤ dates[i-1] 的收盘（时间锁口径）。
        窗口模式下使用基准 CSV 全历史前缀（窗口起点之前的历史仍生效）。"""
        if self.window:
            cut = self.dates[i - 1] if i > 0 else None
            closes = [v for d, v in self.bench.items() if cut is None or d <= cut]
        else:
            closes = [self.bench[d] for d in self.dates[:i] if d in self.bench]
        return regime_filter.traffic_light(closes)

    def flat_legs(self, i: int) -> bool:
        prev = self.dates[i - 1]
        return all(abs(self.legs[leg].get(c, {}).get(prev, 0.0)) < 1e-12
                   for c in self.core for leg in CORE_LEGS)

    def sleeve_params(self) -> dict:
        p = _copy.deepcopy(self.params)
        p["n_assets"] = len(self.core)
        return p

    def sleeve_equity(self, st: dict, leg: str, date: str) -> float:
        """腿权益 = 腿现金 + 持仓市值 + 该腿名下停泊的货币份额市值。"""
        port = st["sleeves"][leg]
        val = port["cash"]
        px_map = self.by_date_div if leg == DEF_LEG else self.by_date
        for c, p in port["positions"].items():
            src = px_map if c in px_map else self.by_date
            val += p["shares"] * src[c][date]["close"]
        val += st["money"][leg] * (self.money_px.get(date) or
                                   st["money"].get("last_px") or 0.0)
        return val

    # ---- 交易执行 ----
    def clear_sleeves(self, st: dict, date: str, reason: str,
                      legs: tuple = CORE_LEGS) -> list[dict]:
        """兑现腿持仓（开盘价）；现金留在各腿，由 park_money 停泊货币份额。
        默认只清核心腿 CORE_LEGS（T-Fin28：防守腿 E 豁免，由其自身公式管理）。"""
        done = []
        for leg in legs:
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
        cash_of = {leg: st["sleeves"][leg]["cash"] for leg in ACTIVE_LEGS}
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
                                        for l in ACTIVE_LEGS), 2)

    def redeem_money(self, st: dict, date: str,
                     legs: tuple | None = None) -> float:
        """按各腿记账逐笔赎回全部货币份额 → 回到来源账户现金。
        legs=None 时赎全部（T-Fin28：可指定 ("D",) 等局部赎回，黄灯解锁网格腿）。"""
        total = 0.0
        px = self.money_px.get(date) or st["money"].get("last_px")
        if not px:
            return 0.0
        sell_px = px * (1 - self.params["fees"]["slippage_bps"] / 10000)
        who_list = legs or (ACTIVE_LEGS + ("MASTER",))
        for who in who_list:
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
        """按昨收合并净值调整各腿预算（T-Fin28 四腿：腿间现金划转，不动持仓）。
        w: {"weights": {leg: frac}, "c_buffer": float}（race_weights4 输出；
        旧 2 腿格式 {"weight_a":...,"weight_b":...} 由 all_weights 兼容归一）；
        c_buffer 的停泊实现统一交给 park_money（禁止现金空转兜底）。"""
        eq_prev = st["curve"][-1][1] if st["curve"] else \
            float(self.params["account"]["initial_capital"])
        ports = st["sleeves"]
        full = self.all_weights(w)
        cur = {leg: self.sleeve_equity(st, leg, date) for leg in ACTIVE_LEGS}
        deltas = {leg: eq_prev * full[leg] - cur[leg] for leg in ACTIVE_LEGS}
        for leg in ACTIVE_LEGS:
            delta = deltas[leg]
            if abs(delta) < 1.0:
                continue
            if delta > 0:                        # 注资：先池后腿（母池=周一赎回资金的
                                                 # 统一回流位，先扫净再从富裕腿弹性借，
                                                 # 消除腿间环形追讨导致的饿死腿）
                need = delta
                for src in ["MASTER"] + list(ACTIVE_LEGS):
                    if src == leg or need <= 0:
                        continue
                    pool = st["master_cash"] if src == "MASTER" \
                        else ports[src]["cash"]
                    if pool <= 0:
                        continue
                    move = min(need, pool)
                    if src == "MASTER":
                        st["master_cash"] -= move
                    else:
                        ports[src]["cash"] -= move
                    ports[leg]["cash"] += move
                    need -= move
                    st["trades"].append({"date": date, "leg": leg, "side": "FUND",
                                         "amount": round(move, 2)})
            elif ports[leg]["cash"] > 0:         # 抽资（只抽现金，避免 churn）
                move = min(-delta, ports[leg]["cash"])
                ports[leg]["cash"] -= move
                # 抽资余数统一回母池（后续腿的注资按"先池后腿"自动回收，
                # 禁止现金空转；master 是唯一回流位，杜绝腿间环形追讨）
                st["master_cash"] += move
                st["trades"].append({"date": date, "leg": leg, "side": "DRAIN",
                                     "amount": round(move, 2)})

    def all_weights(self, w: dict) -> dict:
        """race 输出 → 全腿权重 dict（旧 2 腿格式兼容：D/E 视为 0）。"""
        if "weights" in w:
            return dict(w["weights"])
        wa, wb = float(w.get("weight_a", 0.5)), float(w.get("weight_b", 0.5))
        return {"A": wa, "B": wb, "D": 0.0, "E": 0.0}

    def synth_weights(self, st: dict) -> dict:
        """正常模式权重输出（T-Fin28 四腿，等比缩放 c_buffer）。"""
        k = 1 - st["c_buffer"]
        return {"weights": {leg: round(k * st["weights"][leg], 4)
                            for leg in ACTIVE_LEGS},
                "c_buffer": st["c_buffer"]}

    # ---- 急跌门共享判定（run() 与 export_plan() 同式，防漂移）----
    def gate_extras(self, i: int) -> dict:
        """急跌门判定所需的 ＜全部 ≤ dates[i-1] 收盘的确定量＞（无未来函数）。"""
        cg = self.params["ensemble"].get("crash_gate", {})
        cg_drop = float(cg.get("drop_threshold", 0.03))
        prev_rets = []
        for j in range(max(1, i - 5), i):
            d0, d1 = self.bench.get(self.dates[j - 1]), self.bench.get(self.dates[j])
            if d0 and d1:
                prev_rets.append(d1 / d0 - 1)
        newest = prev_rets[-1] if prev_rets else 0.0
        worst5 = min(prev_rets[-5:], default=0.0)
        w_close = [self.bench[d] for d in self.dates[max(1, i - 60):i]
                   if d in self.bench]
        hi60 = max(w_close, default=0.0)
        latest = self.bench.get(self.dates[i - 1], 0.0) if i > 0 else 0.0
        ratio = float(cg.get("reentry_ratio", 0.99))
        recovered = bool(hi60 and latest >= ratio * hi60)
        light = self.light_at(i) if i > 0 else None
        return {"cg": cg, "cg_on": bool(cg.get("enable")),
                "cg_drop": cg_drop, "newest": newest, "worst5": worst5,
                "hi60": hi60, "latest": latest, "recovered": recovered,
                "light_state": light["state"] if light else "green",
                "calm_exit": (light is not None
                              and light["state"] == "green"
                              and worst5 > -cg_drop and recovered)}

    # ---- 主循环 ----
    def run(self, tail_days: int = 20, allow_llm: bool = False) -> dict:
        p = self.params
        rc = p["ensemble"]["race"]
        init_w = rc.get("init_weights") or {"A": 0.30, "B": 0.25, "D": 0.25, "E": 0.20}
        st = {"master_cash": 0.0,
              "money": {**{k: 0 for k in ACTIVE_LEGS}, "MASTER": 0, "last_px": None},
              "sleeves": {leg: {"cash": 0.0, "positions": {}, "trades": [], "curve": []}
                          for leg in ACTIVE_LEGS},
              "weights": dict(init_w), "weight_a": float(init_w["A"]), "c_buffer": 0.0,
              "trades": [], "curve": [], "last_date": None,
              "race_log": [], "alarms": [], "park_residual": 0.0,
              "locked_until": -1,
              "created": datetime.now().isoformat(timespec="seconds")}
        # 初始按台账 init_weights 注资四腿（母账户清零；无现金空转）
        cap = float(p["account"]["initial_capital"])
        for leg in ACTIVE_LEGS:
            st["sleeves"][leg]["cash"] = round(cap * float(init_w[leg]), 2)

        week_prev = None
        race_pending = None
        sp = self.sleeve_params()
        cg = self.params["ensemble"].get("crash_gate", {})
        cg_on = bool(cg.get("enable"))
        cg_drop = float(cg.get("drop_threshold", 0.03))
        cg_reentry_days = int(cg.get("reentry_days", 10))
        cg_reentry_damp = float(cg.get("reentry_damp", 0.5))
        cg_reentry_ratio = float(cg.get("reentry_ratio", 0.97))
        crash_until = -1          # 急跌熔断门：保持全面防守直到解除条件满足
        was_defense = False       # 上一日 gate_mode（防守→正常切换检测）
        _prev_defense = False

        for i in range(self.warmup, len(self.dates)):
            date = self.dates[i]
            iso = datetime.strptime(date, "%Y-%m-%d").isocalendar()[:2]
            light = self.light_at(i)
            money_mode = light["state"] != "green" or self.flat_legs(i)
            # 急跌熔断门（台账 ensemble.crash_gate），出闸三条件（全确定量、仅用 t-1 收盘）：
            #   ① 灯回绿（close×MA60×MA120）  ② 近5日无 ≤-drop 急跌（反绿闪）
            #   ③ 高点回归：最新收盘 ≥ 60日高点×reentry_ratio（防 V 反弹悬空绿灯）
            gx = self.gate_extras(i)
            newest, worst5 = gx["newest"], gx["worst5"]
            cg_drop = gx["cg_drop"]
            if gx["cg_on"] and newest <= -cg_drop:
                crash_until = i + max(1, int(gx["cg"].get("hold_days", 3)))
            calm_exit = gx["calm_exit"]
            # 出闸三条件常设生效：其一不满足 → 即便计时器已过仍全面防守
            crashed = gx["cg_on"] and not calm_exit
            gate_mode = money_mode or crashed
            # T-Fin28 分腿防守矩阵（台账登记）：
            #   A/B（进攻腿）：黄/红灯即防守清仓（T-Fin03 原语义不变）
            #   D（震荡网格）：黄灯=纠缠震荡期=网格用武之地 → 放行；
            #                  红灯/急跌门 → 清仓+禁买（业主铁律：红灯非防守腿=0）
            #   E（防守红利）：黄/红灯都按自身公式豁免（defensive 位）
            ab_gate = light["state"] != "green" or crashed     # A/B 门
            d_gate = crashed or light["state"] == "red"        # D 腿们（红/急跌防守）
            was_defense = _prev_defense   # 上一交易日的 gate_mode（切换检测）
            _prev_defense = gate_mode
            day_price = {c: self.by_date[c][date] for c in self.core}
            prev_date = self.dates[i - 1]
            prev_sig = {leg: {c: self.legs[leg].get(c, {}).get(prev_date, 0.0)
                              for c in self.core}
                        for leg in CORE_LEGS}
            prev_sig_e = ({self.div_code: self.legs["E"].get(self.div_code, {})
                           .get(prev_date, 0.0)} if self.div_code else {})
            locked = i <= st["locked_until"]

            if not locked:
                # 1) 上周赛马再配置（今日开盘执行；四腿）
                held_money = any(st["money"].get(k, 0) > 0
                                 for k in ACTIVE_LEGS + ("MASTER",))
                if race_pending is not None:
                    if not gate_mode:
                        if held_money:
                            self.redeem_money(st, date)
                        self.rebalance_sleeves(st, race_pending, date)
                    race_pending = None
                    held_money = any(st["money"].get(k, 0) > 0
                                     for k in ACTIVE_LEGS + ("MASTER",))
                # 2) 模式切换与分腿清仓（T-Fin28 分腿防守矩阵）
                #    黄/红/急跌：A/B 清仓（T-Fin03 语义）；红灯/急跌才清 D；
                #    E 防守腿豁免（由其自身公式管理，不在此清）
                if gate_mode and any(st["sleeves"][leg]["positions"]
                                     for leg in ("A", "B")):
                    self.clear_sleeves(st, date, "exit→defense regime/急跌/双空仓",
                                       ("A", "B"))
                    assert not any(st["sleeves"][leg]["positions"]
                                   for leg in ("A", "B")), "状态机违规：清仓未完成"
                if d_gate and st["sleeves"]["D"]["positions"]:
                    self.clear_sleeves(st, date, "D→red/急跌 防守清仓", ("D",))
                if gate_mode and (st["sleeves"]["A"]["positions"]
                                  or st["sleeves"]["B"]["positions"]
                                  or st["sleeves"]["D"]["positions"]):
                    self.park_money(st, date)        # 强制转入货币 ETF（开盘即停泊）
                if not gate_mode and held_money:
                    self.redeem_money(st, date)
                    self.rebalance_sleeves(st, self.synth_weights(st), date)
                if not gate_mode and not held_money:
                    # 防守→正常切换点：衰减进场窗口启动
                    if was_defense:
                        st["reentry_until"] = i + cg_reentry_days
                    # 3) 核心池腿执行（prev 信号 → 今日开盘撮合；状态机门禁）
                    # 出闸后回稳期(≤reentry_days)：买入预算按 damp 衰减——
                    # 出闸三条件（灯绿/近5日无急跌/高点回归）已在门内生效
                    damp_on = cg_on and i <= st.get("reentry_until", -1)
                    sp_d = sp
                    sig_eff = prev_sig
                    if damp_on and cg_reentry_damp < 1:
                        sp_d = _copy.deepcopy(sp)
                        sp_d["n_assets"] = len(self.core) / cg_reentry_damp
                        # 逐资产高点回归：未回归自身 60 日高点×ratio 的标的
                        # 本日信号置零（不接飞刀，防 QDII 独立崩盘咬仓）
                        sig_eff = _copy.deepcopy(prev_sig)
                        w_dates = self.dates[max(1, i - 60):i]
                        for leg in CORE_LEGS:
                            for c in self.core:
                                closes = self.closes_by_date[c]
                                w60 = [closes[d] for d in w_dates if d in closes]
                                hi = max(w60, default=0.0)
                                latest = closes.get(prev_date, 0.0)
                                if hi and latest < cg_reentry_ratio * hi:
                                    sig_eff[leg][c] = 0.0
                    for leg in ("A", "B"):           # 核心池 A/B：门禁 assert_gate
                        o = portfolio.place_orders(st["sleeves"][leg], day_price,
                                                   sig_eff[leg], sp_d, date)
                        assert_gate(light["state"], o)
                # 3b) 黄灯日 D 腿的可交易现金解锁：赎回其名下停泊份额（仅 D）
                if (not locked and not d_gate and gate_mode
                        and st["money"].get("D", 0) > 0):
                    self.redeem_money(st, date, legs=("D",))
                # 3c) D 震荡网格腿独立执行窗口（T-Fin28：绿灯正常；黄灯放行=震荡期；
                #     红灯/急跌= d_gate 已在清仓段处理，此处不执行）
                if not d_gate and not locked:
                    n0 = len(st["sleeves"]["D"]["trades"])
                    o_d = portfolio.place_orders(st["sleeves"]["D"], day_price,
                                                 prev_sig["D"], sp, date)
                    for t in st["sleeves"]["D"]["trades"][n0:]:
                        t["leg"] = "D"
                        if light["state"] == "yellow":
                            t["grid_yellow"] = True   # V3 放行标记（台账 D.yellow_ok）
                    if light["state"] == "red":       # 不应发生（d_gate 已挡）；防御性断言
                        assert_gate("red", o_d)
                # 3b) E 防守红利腿独立执行（T-Fin28 豁免位：黄/红灯不清仓、
                #     不进 assert_gate；trade 打 defensive 标记供 V3 放行检查）
                if self.div_code:
                    div_px = self.by_date_div.get(self.div_code, {}).get(date)
                    if div_px is not None:
                        sp_e = _copy.deepcopy(sp)
                        sp_e["n_assets"] = 1          # 单标的腿，预算=全腿权益
                        n0 = len(st["sleeves"]["E"]["trades"])
                        o_e = portfolio.place_orders(
                            st["sleeves"]["E"], {self.div_code: div_px},
                            prev_sig_e, sp_e, date)
                        # 打标到台账行本身（place_orders 返回值与台账行是两份 dict）
                        for t in st["sleeves"]["E"]["trades"][n0:]:
                            t["leg"] = "E"
                            t["defensive"] = True
                # 4) 每日收盘残余现金停泊货币腿（禁止现金空转；四腿+母账户）
                self.park_money(st, date)

            # 5) 每周最后交易日收盘：赛马（4 腿 A/B/D/E）+ 因子倾斜
            is_last = (i == len(self.dates) - 1 or
                       datetime.strptime(self.dates[i + 1], "%Y-%m-%d")
                       .isocalendar()[:2] != iso)
            if is_last and not locked and p["ensemble"]["race"].get("enable", True):
                sc = None
                if date >= self.fac_min:      # 窗口外历史日无因子台账 → 纯赛马
                    import factor_mining
                    sc = factor_mining.weekly_score(asof=date, allow_llm=allow_llm)
                score = sc.get("score") if sc else None
                sharpes = {leg: sleeve_sharpe(st["sleeves"][leg]["curve"])
                           for leg in ACTIVE_LEGS}
                w = race_weights4(st["weights"], sharpes, p["ensemble"], score)
                tr = {"week_end": date,
                      "sharpe_a": round(sharpes["A"] or 0, 3),
                      "sharpe_b": round(sharpes["B"] or 0, 3),
                      "sharpe_d": round(sharpes["D"] or 0, 3),
                      "sharpe_e": round(sharpes["E"] or 0, 3),
                      "weights": w["weights"],
                      "prev_wa": round(st["weights"]["A"], 3),
                      "weight_a": w["weights"]["A"],
                      "weight_b": w["weights"]["B"],
                      "c_buffer": w["c_buffer"], "race_note": w["note"],
                      "factor": sc}
                st["race_log"].append(tr)
                if self.log_writes:
                    with open(RACE_LOG, "a", encoding="utf-8") as f:
                        f.write(json.dumps(tr, ensure_ascii=False) + "\n")
                race_pending = w
                st["weights"] = w["weights"]
                st["weight_a"] = w["weights"]["A"]
                st["c_buffer"] = w["c_buffer"]

            # 6) 收盘估值（腿曲线 + 合并净值）+ 熔断（绝对规则覆盖合并账户）
            for leg in ACTIVE_LEGS:
                st["sleeves"][leg]["curve"].append(
                    [date, round(self.sleeve_equity(st, leg, date), 2)])
            money_val = sum(st["money"].get(k, 0)
                            for k in ACTIVE_LEGS + ("MASTER",)) * \
                (self.money_px.get(date) or st["money"].get("last_px") or 0.0)
            sleeve_val = sum(self.sleeve_equity(st, leg, date)
                             for leg in ACTIVE_LEGS)
            master_val = st["master_cash"] + st["money"]["MASTER"] * \
                (self.money_px.get(date) or st["money"].get("last_px") or 0.0)
            eq_total = round(master_val + sleeve_val, 2)
            # 资金恒等式：master_cash 非负恒定；净值地板仅赛马（正式）模式校验
            # （T-Fin28 race_compare 独立单跑时其余腿 0 权益，地板不适用）
            race_on = p["ensemble"]["race"].get("enable", True)
            assert st["master_cash"] >= -1e-6 and \
                (eq_total > float(p["account"]["initial_capital"]) * 0.4
                 or not race_on), \
                f"资金恒等式校验失败@{date}：出现现金凭空消失/蒸发 (eq_total={eq_total})"
            prev_eq = st["curve"][-1][1] if st["curve"] else \
                float(p["account"]["initial_capital"])
            st["curve"].append([date, eq_total])
            chk = risk_control.check_breakers(i, date, eq_total, prev_eq,
                                              st["curve"][:-1], p["risk"])
            if chk["triggered"] and not locked:
                for leg in ACTIVE_LEGS:
                    px_src = self.by_date_div if leg == DEF_LEG else self.by_date
                    portfolio.force_liquidate(
                        st["sleeves"][leg],
                        {c: px_src[c][date] for c in st["sleeves"][leg]["positions"]},
                        p, date, chk["reason"])
                self.redeem_money(st, date)
                self.park_money(st, date)
                st["locked_until"] = i + p["risk"]["lock_days"]
                st["alarms"].append({"date": date, "type": "breaker",
                                     "reason": chk["reason"]})
            # T-Fin05：给影子引擎的当日防守状态（收盘口径）——
            # "signal-only-after-close"：该状态供次日开盘 plan 使用
            st["gate_state"] = {"date": date, "defense": bool(gate_mode),
                                "light": light["state"],
                                "crash_gate": gx["cg_on"]}
            st["last_date"] = date
            week_prev = iso
        return st

    # ---- T-Fin05 影子引擎接口 ----
    def export_plan(self) -> dict:
        """导出"下一交易日开盘"的决策输入（铁律：只用 ≤ 台账最后一日的
        数据，绝不引用当日盘中信息；成交与否由影子撮合按真实开盘价执行）。"""
        i = len(self.dates)                     # "下一交易日"的虚拟索引
        prev = self.dates[-1]
        light = self.light_at(i)
        gx = self.gate_extras(i)
        money_mode = light["state"] != "green" or self.flat_legs(i)
        crashed = gx["cg_on"] and not gx["calm_exit"]
        prev_sig = {leg: {c: self.legs[leg].get(c, {}).get(prev, 0.0)
                          for c in self.core}
                    for leg in CORE_LEGS}
        sig_e = self.legs["E"].get(self.div_code, {}).get(prev, 0.0) \
            if self.div_code else 0.0
        reason = ("灯色防守" if light["state"] != "green" else
                  "双空仓" if self.flat_legs(i) else
                  "急跌熔断门(出闸三条件未满)" if crashed else "正常")
        return {"prev_date": prev, "light": light["state"],
                "gate_extras": {k: round(gx[k], 6) if isinstance(gx[k], float)
                                else gx[k] for k in ("newest", "worst5",
                                                     "hi60", "latest",
                                                     "recovered")},
                "defense": bool(money_mode or crashed),
                "defense_reason": "灯色防守" if money_mode and not self.flat_legs(i)
                and light["state"] != "green" else
                ("双空仓" if self.flat_legs(i)
                 else "急跌熔断门(出闸三条件未满足)" if crashed else reason),
                "prev_sig": prev_sig,
                "prev_sig_e": sig_e,
                "def_leg": {"code": self.div_code, "signal": sig_e,
                            "defensive": True,
                            "note": "T-Fin28 防守豁免位：黄/红灯不清仓"},
                "core": list(self.core) + ([self.div_code] if self.div_code else [])}


# ---------------------------------------------------------------------------
def fmt(x, signed=True):
    if x is None:
        return "N/A"
    return f"{'+' if signed and x > 0 else ''}{x * 100:.2f}%"


def run_engine(tail_days: int = 20, allow_llm: bool = False) -> dict:
    eng = EnsembleEngine()
    st = eng.run(tail_days=tail_days, allow_llm=allow_llm)
    # T-Fin28 绩效口径：胜率/已平仓以各腿真实交易流水（含 pnl）合并计算，
    # 停泊/赎回（reason=park/redeem，无 pnl）不再混入分母（原有口径缺陷）。
    merged = [t for leg in ACTIVE_LEGS
              for t in st["sleeves"][leg]["trades"]] + \
             [t for t in st["trades"] if t.get("pnl") is not None]
    pseudo = {"cash": st["master_cash"], "positions": {}, "curve": st["curve"],
              "trades": merged,
              "initial": float(eng.params["account"]["initial_capital"])}
    m = portfolio.metrics(pseudo, eng.params["account"]["initial_capital"])
    return {"engine": eng, "state": st, "metrics": m}


def write_report(res: dict, tail_days: int = 20) -> str:
    eng, st, m = res["engine"], res["state"], res["metrics"]
    today = st["last_date"]
    lines = [f"# T-Fin03 多策略赛马并发回测 · 尾部 {tail_days} 日 · 截至 {today}", ""]
    lines.append(f"> 生成 {datetime.now():%Y-%m-%d %H:%M} · "
                 f"初始 ¥{eng.params['account']['initial_capital']:,.0f} · "
                 "A=双均线20/60 · B=RSI14 · D=网格120/3%（黄灯放行） · "
                 "E=红利510880（防守豁免） · C=货币腿 · 熔断红线全程在线")
    lines += ["", "## 1) 全程绩效（赛马机制整体）", ""]
    lines.append(f"- 净值 **¥{m['nav']:,.2f}**（{fmt(m['total_return'])} 累计 / "
                 f"{fmt(m['ann_return'])} 年化）")
    lines.append(f"- 夏普 **{m['sharpe']}** · Sortino **{m['sortino']}** · "
                 f"Calmar **{m['calmar']}**")
    lines.append(f"- 最大回撤 **{m['mdd']:.2%}** · 胜率 {m['win_rate']}"
                 f"（{m['n_trades_closed']} 笔已平仓）")
    lines += ["", "## 2) 赛马权重演变 & 四腿策略夏普变化（T-Fin28）", "",
              "| 周终 | A夏普 | B夏普 | D夏普 | E夏普 | A/B/D/E 分配 | C预配 | 因子分 | 依据 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for tr in st["race_log"][-12:]:
        w = tr.get("weights") or {"A": tr.get("weight_a", 0),
                                  "B": tr.get("weight_b", 0), "D": 0, "E": 0}
        pct = "/".join(f"{w[l] * 100:.0f}%" for l in ("A", "B", "D", "E"))
        lines.append(f"| {tr['week_end']} | {tr['sharpe_a']} | {tr['sharpe_b']} "
                     f"| {tr.get('sharpe_d', '-')} | {tr.get('sharpe_e', '-')} "
                     f"| {pct} | {tr['c_buffer'] * 100:.0f}% "
                     f"| {tr['factor'] and tr['factor'].get('score')} "
                     f"| {tr.get('race_note', tr.get('tilt_note', ''))} |")
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
    money_sh = sum(st["money"].get(k, 0) for k in ACTIVE_LEGS + ("MASTER",))
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
