# -*- coding: utf-8 -*-
"""regime_filter.py —— 大盘环境感知模块（T-Fin02 交付）

确定性红绿灯判定公式（LLM 只能引用结论，不能改变判定）：

  输入：benchmark（沪深300ETF）截至研判日的收盘序列
  · 🟢 绿灯：close > MA60 且 MA60 > MA120（多头排列，趋势环境友好）
  · 🔴 红灯：close < MA60 且 MA60 < MA120（空头排列，趋势环境恶化）
  · 🟡 黄灯：其他（纠缠期，方向不明）
  · 历史不足时：黄灯 + note

灯色仅用于报告呈现与"首席风控官"说的话术上下文；当前版本不改变
strategy.py 的信号公式（铁律：信号只出自数学公式，模块挂接不越权）。
"""
from functools import lru_cache


def sma_now(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def traffic_light(closes: list[float], ma_fast: int = 60,
                  ma_slow: int = 120) -> dict:
    close = closes[-1] if closes else None
    ma_f = sma_now(closes, ma_fast)
    ma_s = sma_now(closes, ma_slow)
    if close is None or ma_f is None or ma_s is None:
        return {"state": "yellow", "light": "🟡 黄灯",
                "close": close, "ma60": ma_f, "ma120": ma_s,
                "rule": "MU（不足）", "note": "历史数据不足 min(ma120) 根，默认黄灯"}

    if close > ma_f and ma_f > ma_s:
        state, light = "green", "🟢 绿灯"
        rule = f"close {close:.3f} > MA60 {ma_f:.3f} 且 MA60 > MA120 → 多头排列"
    elif close < ma_f and ma_f < ma_s:
        state, light = "red", "🔴 红灯"
        rule = f"close {close:.3f} < MA60 {ma_f:.3f} 且 MA60 < MA120 → 空头排列"
    else:
        state, light = "yellow", "🟡 黄灯"
        rule = (f"close {close:.3f} / MA60 {ma_f:.3f} / MA120 {ma_s:.3f} "
                f"相互纠缠 → 方向不明")
    return {"state": state, "light": light, "close": close,
            "ma60": ma_f, "ma120": ma_s, "rule": rule, "note": ""}


def lights_for_kline(klines: list[dict]) -> dict:
    """便捷入口：单标的 K 线（正序）→ 最新一根研判信号灯。"""
    return traffic_light([k["close"] for k in klines])
