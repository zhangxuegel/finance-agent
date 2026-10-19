# -*- coding: utf-8 -*-
"""portfolio.py —— 账户资金管理模块（Task 3 交付）

撮合逻辑（铁律，全部以 index/params.yaml 台账为准）：
  · 信号日收盘生成指令 → 次日开盘价撮合（trading_rule.settle=next_open）
  · 滑点 treat slippage_bps：买入按 开盘×(1+slip)，卖出按 开盘×(1-slip)
  · 手续费：佣金 max(成交额×commission_bps, commission_min) 双边计，
            印花税 stamp_duty_bps 仅卖出（ETF=0，个股=100）
  · 整手 lot=100 股，整数股买入手数
  · 目标权重：单标的买入预算 = 当日总净值 × 1/n_assets

净值/绝对账：每日收盘记录 data/portfolio.json；持仓市值 = close × shares 精确相等。
绩效指标：净值、累计收益、年化、最大回撤 MDD、胜率、夏普比率。
"""
import json
import os
import math
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "portfolio.json")
TRADING_DAYS = 244
RISK_FREE = 0.015


# ---------------------------------------------------------------------------
# 台账读取
def load_params() -> dict:
    import yaml
    with open(os.path.join(ROOT, "index", "params.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict | None:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(port: dict):
    if not os.path.exists(os.path.join(ROOT, "data")):
        os.makedirs(os.path.join(ROOT, "data"))
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(port, f, ensure_ascii=False, indent=2)


def reset_state(params: dict):
    """把台账的初始资金重置为空白账户。"""
    port = {
        "cash": float(params["account"]["initial_capital"]),
        "positions": {},       # code -> {shares, cost, entry}
        "trades": [],
        "curve": [],           # [[date, equity], ...]
        "last_date": None,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    save_state(port)
    return port


def reset_risk():
    from risk_control import save
    save({"locked": False, "lock_until_day": 0, "alarm_days": []})


# ---------------------------------------------------------------------------
# 费用与估值（精确口径）。滑点统一体现在成交价上（买入 open*(1+slip)，
# 卖出 open*(1-slip)），费用只计佣金与印花税，二者绝不混算。
def buy_cost(notional: float, p: dict) -> float:
    return notional * (1 + p["slippage_bps"] / 10000)
def trade_fees(notional: float, side: str, p: dict) -> float:
    p = flatten(p)
    fee = max(notional * p["commission_bps"] / 10000, p["commission_min"])
    if side == "SELL":
        fee += notional * p["stamp_duty_bps"] / 10000
    return fee


def equity(port: dict, prices: dict) -> float:
    return round(port["cash"] + sum(p["shares"] * prices[c]
                 for c, p in port["positions"].items()), 2)


def flatten(params: dict) -> dict:
    """台账归一化：把嵌套的 fees/trading_rule 拍平，供撮合函数统一取参。"""
    if "commission_bps" in params:      # 已是平铺形式
        return params
    f = dict(params["fees"])
    f.update(params["trading_rule"])
    f.update(params["risk"])
    f["slippage_bps"] = params["fees"]["slippage_bps"]     # 归到费用口径
    return f


# ---------------------------------------------------------------------------
# 撮合执行
def place_orders(port: dict, day_price: dict, prev_sig: dict, params: dict,
                 date: str) -> list[dict]:
    """day_price: {code: kline-of-today}; prev_sig: {code: 0/1 昨日收盘信号}
    依据昨日信号→今日开盘撮合；实现整手与目标权重。锁仓职责在外层(risk)。"""
    p = flatten(params)
    n_assets = params["n_assets"]
    orders = []
    # 卖先买后，减少现金压力
    for code in [c for c, s in prev_sig.items() if s == 0 and c in port["positions"]]:
        pos = port["positions"][code]
        open_px = day_price[code]["open"] * (1 - p["slippage_bps"] / 10000)
        notional = pos["shares"] * open_px
        fee = trade_fees(notional, "SELL", p)
        port["cash"] += notional - fee
        pnl = notional - pos["shares"] * pos["cost"] - (
            max(notional * p["commission_bps"] / 10000, p["commission_min"]))
        port["trades"].append({
            "date": date, "code": code, "side": "SELL",
            "shares": pos["shares"], "price": round(open_px, 4),
            "fee": round(fee, 2), "pnl": round(pnl, 2),
        })
        del port["positions"][code]
        orders.append({"date": date, "code": code, "side": "SELL",
                       "shares": pos["shares"], "price": round(open_px, 4)})

    for code, s in prev_sig.items():
        if s != 1 or code in port["positions"] or code not in day_price:
            continue
        eq = equity(port, {c: day_price[c]["close"] for c in port["positions"]} |
                    {code: day_price[code]["open"]})
        budget = min(eq / n_assets, port["cash"])
        open_px = day_price[code]["open"] * (1 + p["slippage_bps"] / 10000)
        shares = int(budget // (open_px * p["lot"])) * p["lot"]
        if shares <= 0:
            continue
        fee = trade_fees(shares * open_px, "BUY", p)
        if shares * open_px + fee > port["cash"] + 1e-6:
            shares -= p["lot"]
            if shares <= 0:
                continue
            fee = trade_fees(shares * open_px, "BUY", p)
        if fee + shares * open_px > port["cash"]:
            continue
        port["cash"] -= shares * open_px + fee
        port["positions"][code] = {"shares": shares, "cost": round(open_px, 4)}
        port["trades"].append({
            "date": date, "code": code, "side": "BUY",
            "shares": shares, "price": round(open_px, 4),
            "fee": round(fee, 2), "pnl": None,
        })
        orders.append({"date": date, "code": code, "side": "BUY",
                       "shares": shares, "price": round(open_px, 4)})
    return orders


def force_liquidate(port: dict, day_price: dict, params: dict, date: str,
                    reason: str) -> list[dict]:
    """熔断强平：按当日收盘价（保守：不利侧下滑点）清算全部持仓。"""
    cleared = []
    p = flatten(params)
    slip = p["slippage_bps"] / 10000
    for code in list(port["positions"]):
        pos = port["positions"][code]
        px = day_price[code]["close"] * (1 - slip)
        notional = pos["shares"] * px
        fee = trade_fees(notional, "SELL", p)
        pnl = notional - pos["shares"] * pos["cost"] - (
            max(notional * p["commission_bps"] / 10000, p["commission_min"]))
        port["cash"] += notional - fee
        port["trades"].append({"date": date, "code": code, "side": "SELL",
                               "shares": pos["shares"], "price": round(px, 4),
                               "fee": round(fee, 2), "pnl": round(pnl, 2),
                               "forced": True})
        del port["positions"][code]
        cleared.append({"code": code, "price": round(px, 4),
                        "shares": pos["shares"], "reason": reason})
    return cleared


# ---------------------------------------------------------------------------
# 绩效指标
def metrics(port: dict, initial: float) -> dict:
    curve = port["curve"]
    if len(curve) < 3:
        return {}
    values = [e for _, e in curve]
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    total = values[-1] / initial - 1
    ann = (1 + total) ** (TRADING_DAYS / (len(values) - 1)) - 1
    mean_d = sum(rets) / len(rets)
    vol_d = (sum((r - mean_d) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 if len(rets) > 1 else 0.0
    ann_vol = vol_d * (TRADING_DAYS ** 0.5)
    sharpe = ((mean_d * TRADING_DAYS) - RISK_FREE) / ann_vol if ann_vol else None

    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)

    sells = [t for t in port["trades"] if t["side"] == "SELL"]
    wins = [t for t in sells if (t.get("pnl") or 0) > 0]

    # ---- T-Fin02 新增：下行波动（Sortino）与卡玛（Calmar）比率 ----
    downside = [r * r for r in rets if r < 0]          # 半方差口径
    dd_dev_d = (sum(downside) / len(rets)) ** 0.5 if rets else 0.0
    dd_dev_ann = dd_dev_d * TRADING_DAYS ** 0.5
    sortino = ((ann - RISK_FREE) / dd_dev_ann) if dd_dev_ann > 1e-12 else None
    calmar = (ann / mdd) if (mdd and mdd > 1e-12) else None

    return {
        "nav": round(values[-1], 2),
        "total_return": total,
        "ann_return": ann,
        "ann_vol": ann_vol,
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "mdd": mdd,
        "sortino": round(sortino, 3) if sortino is not None else None,
        "calmar": round(calmar, 3) if calmar is not None else None,
        "win_rate": round(len(wins) / len(sells), 4) if sells else None,
        "n_trades_closed": len(sells),
        "days_recorded": len(values),
    }
