# -*- coding: utf-8 -*-
"""screener.py —— 选股过滤模块（盘前运行）

职责：从候选股票池中筛选满足三条条件的标的，覆盖写入 index/watchlist.yaml。
所有阈值均来自 index/params.yaml screener.*，代码中禁止写死数字。

筛选条件：
  1. 量能极值：当日成交额 > 过去20日均额 × volume_ratio_threshold
  2. 强趋势：   收盘价 > MA_fast > MA_mid > MA_slow，且过去10日涨幅(ROC) > roc_threshold
  3. 强承接：  过去20日阳线成交量总和 / 20日总成交量 > bull_volume_ratio_threshold

输出：前 select_count 只股票，写入 watchlist.yaml（覆盖模式）
"""
import os
import sys
import yaml
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import fetch_data


def load_params() -> dict:
    with open(os.path.join(ROOT, "index", "params.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def screen_one(code: str, klines: list[dict], cfg: dict) -> dict | None:
    """对单标的做三条过滤。

    通过时返回包含评分数值的 dict；不通过时返回 None。
    返回字段：code, vol_mult, roc, bull_ratio, detail
    """
    lookback = cfg["lookback_days"]
    if len(klines) < lookback + 1:
        return None

    recent = klines[-lookback:]              # 最近20日（含“今日”）
    today = recent[-1]
    prev_amounts = [k["amount"] for k in recent[:-1]]  # 过去20日（不含今日）

    # ---- 条件1：量能极值 ----
    if len(prev_amounts) < lookback - 1:
        return None
    avg_amount = sum(prev_amounts) / len(prev_amounts)
    if avg_amount <= 0:
        return None
    vol_mult = today["amount"] / avg_amount
    if vol_mult <= cfg["volume_ratio_threshold"]:
        return None

    # ---- 条件2：强趋势（均线周期从 cfg 读取，不硬编码） ----
    closes = [k["close"] for k in recent]
    ma_fast = sma(closes, cfg["ma_fast"])
    ma_mid = sma(closes, cfg["ma_mid"])
    ma_slow = sma(closes, cfg["ma_slow"])
    if ma_fast is None or ma_mid is None or ma_slow is None:
        return None
    if not (today["close"] > ma_fast > ma_mid > ma_slow):
        return None

    close_roc_ago = closes[-(cfg["roc_days"] + 1)] if len(closes) > cfg["roc_days"] else None
    if close_roc_ago is None or close_roc_ago <= 0:
        return None
    roc = today["close"] / close_roc_ago - 1
    if roc <= cfg["roc_threshold"]:
        return None

    # ---- 条件3：强承接（控盘） ----
    bull_vol = 0
    total_vol = 0
    for k in recent:
        total_vol += k["volume"]
        if k["close"] > k["open"]:
            bull_vol += k["volume"]
    if total_vol <= 0:
        return None
    bull_ratio = bull_vol / total_vol
    if bull_ratio <= cfg["bull_volume_ratio_threshold"]:
        return None

    return {
        "code": code,
        "vol_mult": vol_mult,
        "roc": roc,
        "bull_ratio": bull_ratio,
        "detail": f"量能{vol_mult:.2f}x 涨幅{roc:.2%} 承接{bull_ratio:.2%}",
    }


def run_for_date(codes_with_meta: list[dict], cfg: dict, asof_date: str,
                 data_dir: str | None = None) -> list[dict]:
    """基于截至 asof_date 的历史行情截断，筛选出当日可选股票。

    用于 daily_report.py 每日回测/推进：基于 T-1 收盘数据选出 T 日可买股票，
    严格避免未来函数。返回列表已按 vol_mult 倒序，截取前 select_count。
    """
    import fetch_data

    candidate_map = {c["code"]: c for c in codes_with_meta}
    passed = []
    for c in codes_with_meta:
        try:
            kl = fetch_data.load_csv(c["code"], data_dir=data_dir)
        except FileNotFoundError:
            continue
        # 截断到 asof_date（含该日），模拟“截至昨收”的信息集
        kl_cut = [k for k in kl if k["date"] <= asof_date]
        if len(kl_cut) < cfg["lookback_days"] + 1:
            continue
        result = screen_one(c["code"], kl_cut, cfg)
        if result is not None:
            passed.append({**c, **result})

    passed.sort(key=lambda x: x["vol_mult"], reverse=True)
    return passed[: cfg["select_count"]]


def write_watchlist(selected: list[dict], candidate_map: dict):
    """覆盖写入 index/watchlist.yaml。保留原有字段并追加选股标记。"""
    watchlist_path = os.path.join(ROOT, "index", "watchlist.yaml")
    new_wl = []
    for s in selected:
        base = candidate_map.get(s["code"], {})
        new_wl.append({
            "code": s["code"],
            "name": base.get("name", s.get("name", "")),
            "secid": base.get("secid", s.get("secid", "")),
            "kind": base.get("kind", s.get("kind", "etf")),
            "screener_selected": True,
            "selected_at": datetime.now().strftime("%Y-%m-%d"),
        })
    with open(watchlist_path, "w", encoding="utf-8") as f:
        yaml.dump(new_wl, f, allow_unicode=True, sort_keys=False)


def main() -> int:
    params = load_params()
    sc = params.get("screener", {})
    if not sc.get("enable", False):
        print("[screener] 选股模块未启用，跳过")
        return 0

    cfg = {
        "select_count": sc["select_count"],
        "volume_ratio_threshold": sc["volume_ratio_threshold"],
        "roc_threshold": sc["roc_threshold"],
        "bull_volume_ratio_threshold": sc["bull_volume_ratio_threshold"],
        "lookback_days": sc["lookback_days"],
        "roc_days": sc["roc_days"],
        "ma_fast": sc["ma_fast"],
        "ma_mid": sc["ma_mid"],
        "ma_slow": sc["ma_slow"],
    }

    candidates = sc.get("candidate_pool", [])
    if not candidates:
        print("[screener] candidate_pool 为空，无标的可筛选")
        return 1

    candidate_map = {c["code"]: c for c in candidates}
    passed = []
    for c in candidates:
        try:
            kl = fetch_data.load_csv(c["code"])
        except FileNotFoundError:
            print(f"[screener] {c['code']} CSV 不存在，跳过")
            continue
        result = screen_one(c["code"], kl, cfg)
        if result is not None:
            passed.append({**c, **result})
        else:
            print(f"[screener] {c['code']} 未通过筛选")

    # 按 vol_mult 数值倒序排列，取前 select_count（不再从字符串 split）
    passed.sort(key=lambda x: x["vol_mult"], reverse=True)
    selected = passed[: cfg["select_count"]]

    if selected:
        write_watchlist(selected, candidate_map)
        codes = [s["code"] for s in selected]
        print(f"[screener] 选出 {len(selected)} 只：{codes}")
    else:
        # 无标的通过时，保留现有 watchlist 不动（避免清空导致无法交易）
        print("[screener] 无标的通过筛选，watchlist 未做更改")

    return 0


if __name__ == "__main__":
    sys.exit(main())
