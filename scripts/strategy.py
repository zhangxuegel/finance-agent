# -*- coding: utf-8 -*-
"""strategy.py —— 绝对理性的策略引擎（Task 2 交付）

两条确定性策略，只算信号（1 满仓 / 0 空仓），绝不涉及资金计算：

  策略 A（趋势追踪）：双均线。MA_fast 上穿 MA_slow → 1；下穿 → 0（持有中间态）。
  策略 B（均值回归）：布林带(20, 2σ)。收盘 < 下轨 → 1；收盘 ≥ 中轨 → 0。

铁律：本文件只承认数学公式，任何 LLM 不得改写、绕过或直接下发信号。
信号由信号日收盘价生成，撮合规则（次日开盘价、滑点、手续费）在 portfolio。
"""
import os
import sys

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)


def load_params() -> dict:
    with open(os.path.join(ROOT, "index", "params.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def sma_series(values: list[float], window: int) -> list[float | None]:
    """滚动均值序列，前 window-1 个为 None。"""
    out: list[float | None] = []
    for i in range(len(values)):
        out.append(sma(values[max(0, i - window + 1): i + 1], window)
                   if i + 1 >= window else None)
    return out


def bollinger(values: list[float], window: int, n_std: float):
    """滚动布林带，返回 (mid, upper, lower) 序列（None = 未成熟）。"""
    mids = sma_series(values, window)
    uppers, lowers = [], []
    for i, m in enumerate(mids):
        if m is None:
            uppers.append(None)
            lowers.append(None)
            continue
        w = values[i - window + 1: i + 1]
        var = sum((x - m) ** 2 for x in w) / window   # 总体方差（同通达信口径）
        sd = var ** 0.5
        uppers.append(m + n_std * sd)
        lowers.append(m - n_std * sd)
    return mids, uppers, lowers


# ---------------------------------------------------------------------------
def signal_series_a(closes: list[float], ma_fast: int, ma_slow: int) -> list[float]:
    """策略 A：双均线金叉/死叉。信号在收盘后确定，用收盘口径（不需要 shift，
    shift 的次日在撮合层执行：下一交易日开盘价成交）。"""
    sig: list[float] = []
    pos = 0.0
    fast = sma_series(closes, ma_fast)
    slow = sma_series(closes, ma_slow)
    for i in range(len(closes)):
        if fast[i] is None or slow[i] is None:
            sig.append(0.0)
            continue
        if fast[i] > slow[i]:
            pos = 1.0     # 金叉满仓
        elif fast[i] < slow[i]:
            pos = 0.0     # 死叉清仓
        sig.append(pos)
    return sig


def signal_series_b(closes: list[float], window: int, n_std: float) -> list[float]:
    """策略 B：布林带。收盘 < 下轨买入；收盘 ≥ 中轨卖出。"""
    mids, _, lowers = bollinger(closes, window, n_std)
    sig: list[float] = []
    pos = 0.0
    for i in range(len(closes)):
        if mids[i] is None or lowers[i] is None:
            sig.append(0.0)
            continue
        if closes[i] < lowers[i]:
            pos = 1.0     # 触下轨买入
        elif closes[i] >= mids[i]:
            pos = 0.0     # 触中轨卖出
        sig.append(pos)
    return sig


# ---------------------------------------------------------------------------
def signals_for(code: str, klines: list[dict], params: dict | None = None) -> list[dict]:
    """对外主接口：单标的日信号。

    返回：[{"date", "signal"}]，与 klines 逐日对齐（0/1）。
    signal[i] 由第 i 日收盘生成 → portfolio 在第 i+1 日开盘撮合。
    """
    params = params or load_params()["strategy"]
    engine = params["engine"]
    closes = [k["close"] for k in klines]

    if engine == "A":
        raw = signal_series_a(closes, params["A"]["ma_fast"], params["A"]["ma_slow"])
    elif engine == "B":
        raw = signal_series_b(closes, params["B"]["window"], params["B"]["n_std"])
    else:
        raise ValueError(f"未知策略引擎: {engine}（只支持 A/B）")

    return [{"date": k["date"], "signal": s} for k, s in zip(klines, raw)]


def latest_signal(code: str, klines: list[dict], params: dict | None = None) -> dict:
    """最新一根线的信号（daily_report 使用）。"""
    sigs = signals_for(code, klines, params)
    return sigs[-1]


if __name__ == "__main__":
    sys.path.insert(0, BASE)
    import fetch_data

    for w in fetch_data.load_watchlist():
        kl = fetch_data.load_csv(w["code"])
        p = load_params()["strategy"]
        sigs = signals_for(w["code"], kl, p)
        turns = sum(1 for i in range(1, len(sigs)) if sigs[i]["signal"] != sigs[i - 1]["signal"])
        print(f"[{p['engine']}] {w['code']} {w['name']}: "
              f"末3信号={[s['signal'] for s in sigs[-3:]]} "
              f"信号翻转次数={turns}/{len(sigs)}")
