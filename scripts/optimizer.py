# -*- coding: utf-8 -*-
"""optimizer.py —— 策略进化引擎（Task 5，每周运行）

网格搜索策略参数（只搜 strategy.*，风险熔断参数绝不参与优化），并做
**样本外验证（OOS）**防过拟合：
  · 训练集 = 前段历史（前 ~75% 交易日）
  · 验证集 = 最后 ~25%（约 0.75 年，全新数据）
  · 拦截规则：训练集夏普最高、但验证集夏普≤0 或 < 30%×训练夏普的组合
    视为过拟合嫌疑 → 拒绝采用，台账标记 confidence: low
  · 通过拦截的最优组合 → 写入 index/params_suggested.json（confidence: high）
  · 每次运行追加台账 index/optimizer_log.jsonl，全程可追溯

铁律：risk.* 不在本文件任何搜索空间里；熔断规则按 real-time 逻辑在线生效。
"""
import itertools
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import fetch_data
import portfolio
import risk_control
import strategy

SUGGEST_PATH = os.path.join(ROOT, "index", "params_suggested.json")
LOG_PATH = os.path.join(ROOT, "index", "optimizer_log.jsonl")

GRID = {
    "A": {"ma_fast": [5, 10, 15, 20],
          "ma_slow": [20, 30, 60, 120]},
    "B": {"window": [20, 30, 40],
          "n_std": [1.5, 2.0, 2.5]},
}
FORBIDDEN = ["risk"]          # 红线：这些台账段永不入搜索空间


def warmup_index(params: dict) -> int:
    """策略最慢指标需要的历史根数（信号从该索引后才有意义）。"""
    eng = params["strategy"]["engine"]
    if eng == "A":
        return max(params["strategy"]["A"]["ma_fast"],
                   params["strategy"]["A"]["ma_slow"])
    return params["strategy"]["B"]["window"]


# ---------------------------------------------------------------------------
def grid_combos(eng: str):
    g = GRID[eng]
    keys = list(g)
    for values in itertools.product(*(g[k] for k in keys)):
        cfg = dict(zip(keys, values))
        if eng == "A" and cfg["ma_fast"] >= cfg["ma_slow"]:
            continue
        yield cfg


def prepare():
    params = portfolio.load_params()
    watch = [w for w in fetch_data.load_watchlist() if not w.get("exclude_core")]
    codes = [w["code"] for w in watch]
    params["n_assets"] = len(codes)
    kline_by = {c: fetch_data.load_csv(c) for c in codes}
    dates = sorted(set.intersection(*[{k["date"] for k in kl} for kl in kline_by.values()]))
    by_date = {c: {k["date"]: k for k in kl} for c, kl in kline_by.items()}
    return params, codes, dates, by_date


def replay_with(params: dict, codes: list, dates: list, by_date: dict,
                overrides: dict, start_idx: int) -> dict:
    """与 daily_report.engine_run 同口径的内存重放（无落盘、无报告）。"""
    klc = {c: fetch_data.load_csv(c) for c in codes}
    sig_by = {c: {s["date"]: s["signal"] for s in
                  strategy.signals_for(c, klc[c], {**params["strategy"], **overrides})}
              for c in codes}

    port = {"cash": float(params["account"]["initial_capital"]), "positions": {},
            "trades": [], "curve": [], "last_date": None}
    locked_day = -1
    for i in range(start_idx, len(dates)):
        date = dates[i]
        prev_sig = {c: sig_by[c].get(dates[i - 1], 0.0) for c in codes}
        day_price = {c: by_date[c][date] for c in codes}
        if i > locked_day:                     # 锁仓期外才允许下单
            portfolio.place_orders(port, day_price, prev_sig, params, date)
        eq = portfolio.equity(port, {c: day_price[c]["close"] for c in codes})
        prev_eq = port["curve"][-1][1] if port["curve"] else port["cash"]
        port["curve"].append([date, round(eq, 2)])
        chk = risk_control.check_breakers(i, date, eq, prev_eq,
                                          port["curve"][:-1], params["risk"])
        if chk["triggered"] and i > locked_day:
            portfolio.force_liquidate(port, day_price, params, date, chk["reason"])
            locked_day = i + params["risk"]["lock_days"]
    return port


def oos_metrics(port: dict, initial: float, split: int) -> tuple[dict, dict | None]:
    """把曲线按索引切成 train/oos 两段，分别计算夏普等指标。"""
    curve = port["curve"]
    segs = {}
    for name, idx in (("train", range(0, min(split, len(curve)))),
                      ("oos", range(split, len(curve)))):
        v = [curve[i][1] for i in idx]
        if len(v) < 3:
            segs[name] = None
            continue
        rets = [v[j] / v[j - 1] - 1 for j in range(1, len(v))]
        mean_d = sum(rets) / len(rets)
        vol_d = (sum((r - mean_d) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        ann_vol = vol_d * 244 ** 0.5
        peak = mdd = v[0]
        mdd = 0.0
        for x in v:
            peak = max(peak, x)
            mdd = max(mdd, (peak - x) / peak) if peak else 0.0
        segs[name] = {"total": v[-1] / v[0] - 1,
                      "sharpe": round(((mean_d * 244) - portfolio.RISK_FREE) / ann_vol, 3)
                                if ann_vol else None, "mdd": round(mdd, 4)}
    return segs["train"], segs["oos"]


# ---------------------------------------------------------------------------
def run():
    params, codes, dates, by_date = prepare()
    eng = params["strategy"]["engine"]
    warmup = (max(params["strategy"]["A"].values()) if eng == "A"
              else max(params["strategy"]["B"]["window"], 2))
    split = int(len(dates) * 0.75)              # 75% 训练，25% 样本外
    print(f"[opt] 交易日 {len(dates)}，训练阈值索引 {split}（前 75%），OOS 余约 {len(dates) - split} 日")
    assert warmup < split, "预热期长于训练集，请增加历史数据"

    rows = []
    for overrides in grid_combos(eng):
        port = replay_with(params, codes, dates, by_date, overrides, warmup)
        mtr, moos = oos_metrics(port, params["account"]["initial_capital"], split)
        if not mtr or mtr["sharpe"] is None:
            continue
        rows.append({"engine": eng, "params": overrides,
                     "train_sharpe": mtr["sharpe"], "train_total": round(mtr["total"], 4),
                     "train_mdd": mtr["mdd"],
                     "oos_sharpe": (moos or {}).get("sharpe"),
                     "oos_total": round(moos["total"], 4) if moos else None,
                     "oos_mdd": (moos or {}).get("mdd")})
    assert rows, "网格搜索无有效结果"

    rows.sort(key=lambda r: r["train_sharpe"], reverse=True)
    best = rows[0]
    # 防过拟合拦截
    intercept = (best["oos_sharpe"] is None or best["oos_sharpe"] <= 0
                 or best["oos_sharpe"] < 0.3 * best["train_sharpe"])
    best["confidence"] = "low" if intercept else "high"
    best["note"] = ("OOS 夏普崩塌，系统已拦截（防过拟合）" if intercept
                    else "训练/样本外夏普同向通过")

    # 输出台账
    os.makedirs(os.path.dirname(SUGGEST_PATH), exist_ok=True)
    with open(SUGGEST_PATH, "w", encoding="utf-8") as f:
        json.dump(best, f, ensure_ascii=False, indent=2)
    entry = {"date": fetch_data.__name__ and __import__("datetime").date.today().isoformat(),
             "engine": eng, "split": split,
             "best": {k: best[k] for k in
                      ("params", "train_sharpe", "oos_sharpe", "confidence", "note")},
             "top5": rows[:5]}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print("\n[opt] 训练集夏普 Top-5（OOS 为后 25% 全新数据）")
    print(f"{'参数':<30}{'train夏普':>10}{'OOS夏普':>9}{'OOS累计':>10}{'置信':>8}")
    for r in rows[:5]:
        print(f"{json.dumps(r['params'], ensure_ascii=False):<30}"
              f"{r['train_sharpe']:>10}{str(r['oos_sharpe']):>9}"
              f"{(str(round(r['oos_total'],4)) if r['oos_total'] is not None else '-'):>10}"
              f"{('拦截!' if r is best and intercept else '-'):>8}")
    print(f"\n[opt] 最优组: {best['params']}")
    print(f"[opt] 防过拟合: {best['note']} → confidence: {best['confidence']}")
    print(f"[opt] 台账: {SUGGEST_PATH} · {LOG_PATH}")
    return best


if __name__ == "__main__":
    run()
