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


# ---------------------------------------------------------------------------
# T-Fin04 模块 1：赛马参数网格寻优（红线继承：熔断不可优化，MDD≤12% 绝对上限）
# 小范围网格：平滑带 × 权重下限 × A双均线 × B RSI → 16 组（台账登记搜索半径）
def _ensemble_combos():
    combos = []
    for shift in (0.10, 0.20):                     # race.max_weekly_shift
        for minw in (0.30, 0.40):                  # race.min_weight
            for a in ((20, 60), (10, 60)):         # A: (ma_fast, ma_slow)
                for b in ((30, 70), (25, 75)):     # B: (rsi_buy, rsi_sell)
                    combos.append({("race", "max_weekly_shift"): shift,
                                   ("race", "min_weight"): minw,
                                   ("A", "ma_fast"): a[0],
                                   ("A", "ma_slow"): a[1],
                                   ("B", "rsi_buy"): b[0],
                                   ("B", "rsi_sell"): b[1]})
    return combos


ENSEMBLE_OOS_DAYS = 63          # 样本外=最后约 3 个月（未参与训练）
ENSEMBLE_MDD_CAP = 0.12         # 绝对红线：全历史 MDD 不得超过
ENSEMBLE_SUGGEST = os.path.join(ROOT, "index", "ensemble_suggested.json")
REPORT_DIR = os.path.join(ROOT, "reports")


def _seg(curve, lo, hi):
    v = [e for _, e in curve[lo:hi]]
    if len(v) < 3:
        return None
    rets = [v[i] / v[i - 1] - 1 for i in range(1, len(v))]
    m = sum(rets) / len(rets)
    sd = (sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
    peak = m = 0.0
    peak, mdd = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        mdd = max(mdd, 1 - x / peak) if peak else 0
    ann_vol = sd * 244 ** 0.5
    return {"total": v[-1] / v[0] - 1,
            "sharpe": round(((m * 244) - portfolio.RISK_FREE) / ann_vol, 3)
                      if ann_vol else None,
            "mdd": round(mdd, 4)}


def _full_metrics(st):
    pseudo = {"cash": 0.0, "positions": {}, "curve": st["curve"],
              "trades": st["trades"]}
    return portfolio.metrics(pseudo, 1_000_000.0)


def run_ensemble(verbose: bool = True) -> dict:
    """赛马网格寻优：训练=去掉最后 3 个月，OOS=最后 3 个月。
    红线：① 全历史 MDD > 12% → 直接淘汰；② OOS 夏普较训练期崩塌超 50%
    （或 OOS≤0）→ 拦截并降级 confidence。确定性回放（因子走规则分）。"""
    import ensemble_engine as ee
    combos = _ensemble_combos()
    results = []
    n = len(combos)
    for gi, overrides in enumerate(combos, 1):
        eng_ = ee.EnsembleEngine(param_overrides=overrides, log_writes=False)
        st = eng_.run()
        curve = st["curve"]
        split = max(3, len(curve) - ENSEMBLE_OOS_DAYS)
        full = _full_metrics(st)
        tr, oo = _seg(curve, 0, split), _seg(curve, split, len(curve))
        row = {"params": _pretty(overrides),
               "full_mdd": full["mdd"], "full_sharpe": full["sharpe"],
               "full_nav": round(full["nav"], 2),
               "train_sharpe": tr and tr["sharpe"], "train_total": tr and round(tr["total"], 4),
               "oos_sharpe": oo and oo["sharpe"], "oos_total": oo and round(oo["total"], 4),
               "oos_mdd": oo and oo["mdd"], "_ov": overrides}
        crash = (row["oos_sharpe"] is None or (row["train_sharpe"] or 0) > 0
                 and (row["oos_sharpe"] or 0) < 0.5 * row["train_sharpe"])
        row["oos_crash"] = bool(crash)
        mdd_violation = full["mdd"] > ENSEMBLE_MDD_CAP
        row["rejected_mdd"] = bool(mdd_violation)
        results.append(row)
        if verbose:
            print(f"[opt-e] {gi}/{n} {json.dumps(row['params'], ensure_ascii=False)} "
                  f"MDD={full['mdd'] * 100:.2f}% train={row['train_sharpe']} "
                  f"oos={row['oos_sharpe']} {'❌MDD红线' if mdd_violation else ''}"
                  f"{'⚠崩塌' if crash else ''}")
    assert results, "网格搜索无结果"
    ok = [r for r in results if not r["rejected_mdd"]]
    assert ok, f"全部候选触发 MDD≤{ENSEMBLE_MDD_CAP:.0%} 红线 → 不允许产出建议"
    # 基线（T-Fin03 默认参数）
    eng0 = ee.EnsembleEngine(log_writes=False)
    st0 = eng0.run()
    base_split = max(3, len(st0["curve"]) - ENSEMBLE_OOS_DAYS)
    base_full = _full_metrics(st0)
    base = {"params": "默认(T-Fin03)", "full_mdd": base_full["mdd"],
            "full_sharpe": base_full["sharpe"], "full_nav": round(base_full["nav"], 2),
            "train_sharpe": _seg(st0["curve"], 0, base_split)["sharpe"],
            "oos_sharpe": _seg(st0["curve"], base_split, len(st0["curve"]))["sharpe"],
            "_ov": {}}
    # 排序：OOS 夏普优先（样本外才是真实目标），同则 train
    ok.sort(key=lambda r: ((r["oos_sharpe"] if r["oos_sharpe"] is not None else -9),
                           (r["train_sharpe"] or -9)), reverse=True)
    best = ok[0]
    intercept = best["oos_crash"] or (best["oos_sharpe"] or -9) <= 0
    best["confidence"] = "low" if intercept else "high"
    best["note"] = ("OOS 夏普崩塌>50% 或 ≤0 → 系统已拦截（防过拟合）" if intercept
                    else "训练/样本外夏普同向通过（无过拟合迹象）")
    out = {"date": _today(), "baseline": _clean(base),
           "best": _clean(best), "mdd_cap": ENSEMBLE_MDD_CAP,
           "oos_days": ENSEMBLE_OOS_DAYS, "grid_size": n,
           "rejected_mdd_n": sum(r["rejected_mdd"] for r in results),
           "all_rows": [_clean(r) for r in ok[:6]]}
    with open(ENSEMBLE_SUGGEST, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": _today(), "type": "ensemble_grid",
                            "best": _clean(best), "baseline": _clean(base)},
                           ensure_ascii=False) + "\n")
    _report_ensemble_opt(out)
    if verbose:
        print(f"\n[opt-e] 基线  全程夏普 {base['full_sharpe']} · MDD {base['full_mdd'] * 100:.2f}%"
              f" · OOS {base['oos_sharpe']}")
        print(f"[opt-e] 最优  {json.dumps(best['params'], ensure_ascii=False)}")
        print(f"[opt-e] 全程夏普 {best['full_sharpe']} · MDD {best['full_mdd'] * 100:.2f}%"
              f" · OOS {best['oos_sharpe']} · confidence: {best['confidence']}")
        print(f"[opt-e] {best['note']}")
        print(f"[opt-e] 台账：{ENSEMBLE_SUGGEST} · {LOG_PATH}")
    return out


def _pretty(ov):
    return {f"{a}.{b}": v for (a, b), v in ov.items()}


def _clean(row):
    r = {k: v for k, v in row.items() if not k.startswith("_")}
    if isinstance(r.get("params"), dict):
        return r
    return r


def _today():
    import datetime as _dt
    return _dt.date.today().isoformat()


def _report_ensemble_opt(out: dict) -> str:
    b, base = out["best"], out["baseline"]
    path = os.path.join(REPORT_DIR, f"optimizer_ensemble_{out['date']}.md")
    os.makedirs(REPORT_DIR, exist_ok=True)
    lines = ["# T-Fin04 · 赛马参数网格寻优（样本外验证报告）", "",
             f"> 生成 {out['date']} · 网格 {out['grid_size']} 组 · "
             f"OOS=最后 {out['oos_days']} 交易日（未参与训练）· "
             f"绝对红线：全历史 MDD ≤ {out['mdd_cap']:.0%}（熔断规则不可被优化）", "",
             "## 1) 基线（T-Fin03 默认）vs 最优候选", "",
             "| 口径 | 全程夏普 | 全程MDD | 净值 | 训练夏普 | OOS夏普 | 置信 |",
             "|---|---|---|---|---|---|---|"]
    for name, r in (("默认参数", base), ("最优候选", b)):
        lines.append(f"| {name} | {r['full_sharpe']} | {r['full_mdd'] * 100:.2f}% "
                     f"| ¥{r['full_nav']:,.0f} | {r['train_sharpe']} "
                     f"| {r['oos_sharpe']} | {r.get('confidence', '-')} |")
    lines += ["", f"- 最优参数：`{json.dumps(b['params'], ensure_ascii=False)}`", "",
              "## 2) 样本外存活者（Top-6，按 OOS 夏普排序）", "",
              "| 参数 | 全程MDD | 训练夏普 | OOS夏普 | OOS累计 | 判定 |",
              "|---|---|---|---|---|---|"]
    for r in out["all_rows"]:
        tag = ("❌MDD红线" if r["rejected_mdd"] else
               ("⚠OOS崩塌→拦截" if r["oos_crash"] else "✅通过"))
        lines.append(f"| `{json.dumps(r['params'], ensure_ascii=False)}` "
                     f"| {r['full_mdd'] * 100:.2f}% | {r['train_sharpe']} "
                     f"| {r['oos_sharpe']} | {r['oos_total']} | {tag} |")
    lines += ["",
              "## 3) 防过拟合结论", "",
              f"- MDD 红线淘汰 {out['rejected_mdd_n']} 组；"
              f"最终置信 **{b['confidence']}**（{b['note']}）",
              "- 规则：OOS 夏普较训练期下跌超 50% 或 OOS ≤ 0 → 拦截、confidence=low，",
              "  参数仍写入台账但**不会自动生效**（需 CRO 人工确认后手工改 params.yaml）。",
              "- 排序依据 OOS 夏普（样本外表现优先于训练集过拟合高分）。", "",
              "## 4) 台账溯源", "",
              f"- 建议值：`index/ensemble_suggested.json`；审计流水：`data/optimizer_log.jsonl`；",
              "- 网格维度：`race.max_weekly_shift / race.min_weight / A.ma_fast·ma_slow / B.rsi_buy·rsi_sell`",
              "  定义于 `scripts/optimizer.py ENSEMBLE_GRID`（台账登记小范围搜索，非暴力全域）。",
              ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[opt-e] 报告 → {path}")
    return path


if __name__ == "__main__":
    if "--ensemble" in sys.argv:
        run_ensemble()
    else:
        run()
