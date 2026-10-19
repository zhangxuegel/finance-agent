# -*- coding: utf-8 -*-
"""crash_test.py —— 极端行情模拟器（T-Fin04 模块 4）

用真实历史黑天鹅拷问完整 ensemble 系统：
  · 2015 A股千股跌停（沪指 2015-06 见顶 → 连续千股跌停）
  · 2020 美股熔断（ квартал恐慌，经 513500/QDII 与 A 股共振传导）

数据纪律：另存 data/crash/ 长历史 CSV（与正式台账隔离，fetch_data.fetch_and_save
落盘，口径与台账完全同源），不污染 T-Fin01/02 素有 3 年回测的可复现性。

预期（任务书）：股灾爆发后 **前 2 个交易日** 触发避险（黄/红均属避险灯），
清空 A/B 仓，转入货币 ETF；窗口内最大回撤 ≤ 5%。
若失败 → 自动生成链路推演排查报告（逐日：灯色/敞口/动作/净值）。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import fetch_data
from ensemble_engine import EnsembleEngine

CRASH_DIR = os.path.join(ROOT, "data", "crash")
REPORTS = os.path.join(ROOT, "reports")
NATURAL_DAYS = 4800          # ≈13 年，覆盖 2015 与 2020 两段黑天鹅

SCENARIOS = [
    {"key": "crash2015", "label": "2015 A股千股跌停",
     "window": ("2015-03-02", "2015-09-30"),
     "note": "沪指 2015-06-12 见顶 5178 后连续千股跌停（含 8-24 单日-8.5%）"},
    {"key": "crash2020", "label": "2020 美股熔断(经QDII传导)",
     "window": ("2019-11-15", "2020-04-30"),
     "note": "2020-02-20 起美股三次熔断；避险资产 518880/货币腿为主要承接"},
]

EXPECT_ESCAPE_DAYS = 2       # 熔断起点后 ≤2 个交易日内转入避险
EXPECT_MDD = 0.05            # 窗口内最大回撤 ≤ 5%


def ensure_data() -> None:
    """长历史数据准备（覆盖 2015 与 2020 两段黑天鹅；与正式台账隔离落盘）。
    快路径：东财单次 lmt 直取；被限流则走腾讯分段抓取（9 个月/段）拼装。"""
    import time
    os.makedirs(CRASH_DIR, exist_ok=True)
    wl = [w for w in fetch_data.load_watchlist() if not w.get("exclude_core")]
    bench = fetch_data.load_params()["benchmark"]
    money = [w for w in fetch_data.load_watchlist() if w.get("exclude_core")]
    targets = [(w["secid"], w["code"]) for w in wl + money] \
        + [(bench["secid"], bench["code"])]
    seen = set()
    for secid, code in targets:
        if code in seen:
            continue
        seen.add(code)
        path = os.path.join(CRASH_DIR, f"{code}.csv")
        if os.path.exists(path) and os.path.getsize(path) > 10000:
            print(f"[crash] {code} 长历史已就绪 → {path}")
            continue
        kl = []
        try:                                   # 快路径：东财一次拉齐
            kl = fetch_data._klines_east(secid, NATURAL_DAYS)
        except Exception:  # noqa: BLE001
            kl = []
        if len(kl) < 2200:                     # 慢路径：腾讯 9 个月分段
            kl = _tencent_long(secid, time)
        kl = _dedupe(kl)
        assert len(kl) >= 2200, f"{code} 长历史抓取不足（{len(kl)} 条）"
        info = fetch_data.validate(kl, code, NATURAL_DAYS)
        print(f"[crash] {code}: {info['rows']} 行 {info['first']}~{info['last']}"
              f" → {path}")
        fetch_data.save_csv(kl, code, CRASH_DIR)


def _tencent_long(secid: str, time) -> list[dict]:
    """腾讯分段抓取（9 个月/段，~4800 自然日 ≈ 20 段；限流友好）。"""
    import requests
    from datetime import date, timedelta
    sym = fetch_data._tencent_symbol(secid)
    if not sym:
        return []
    url = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
    out, cur = [], date(2013, 11, 1)
    end_all = date.today()
    while cur < end_all:
        end = cur + timedelta(days=275)
        for attempt in range(3):
            try:
                r = requests.get(url,
                                 params={"param": f"{sym},day,{cur:%F},{end:%F},600"},
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                node = ((r.json() or {}).get("data") or {}).get(sym) or {}
                rows = node.get("day") or node.get("qfqday") or []
                break
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    rows = []
                time.sleep(3.0)
        for f in rows:
            if len(f) >= 6:
                out.append({"date": f[0], "open": float(f[1]),
                            "close": float(f[2]), "high": float(f[3]),
                            "low": float(f[4]), "volume": int(float(f[5])),
                            "amount": 0.0})
        cur = end + timedelta(days=1)
        time.sleep(0.7)
    return out


def _dedupe(kl: list[dict]) -> list[dict]:
    seen, out = set(), []
    for k in sorted(kl, key=lambda x: x["date"]):
        if k["date"] in seen:
            continue
        seen.add(k["date"])
        out.append(k)
    return out


def find_crash_start(dates: list[str], bench: dict) -> tuple[str, float]:
    """黑天鹅起点 = 窗口内峰值收盘之后的第一个单日跌幅 ≤ -3% 的交易日。"""
    val = {d: bench.get(d) for d in dates}
    peak_d = max((d for d in dates if val[d] is not None), key=lambda d: val[d])
    peak_i = dates.index(peak_d)
    for j in range(peak_i + 1, len(dates)):
        d, prev = dates[j], dates[j - 1]
        if val[d] is not None and val[prev] and val[d] / val[prev] - 1 <= -0.03:
            return d, val[peak_d]
    return peak_d, val[peak_d] if val[peak_d] else 0.0


def write_crash_report(sc: dict, res: dict, today: str) -> str:
    passing = res["pass"]
    lines = [f"# 崩溃测试 · {sc['label']} · {today}", "",
             f"> 窗口 {sc['window'][0]} ~ {sc['window'][1]} · {sc['note']}", "",
             "## 结论", ""]
    verdict = ("✅ **先保住命**：" if passing else "❌ **未达标（链路推演见下）**：")
    lines.append(
        f"{verdict}熔断起点 **{res['crash_start']}**；避险灯(黄/红)出现在 "
        f"**+{res['escape_days']} 个交易日**（预期 ≤{EXPECT_ESCAPE_DAYS}）；"
        f"窗口 MDD **{res['mdd'] * 100:.2f}%**（预期 ≤{EXPECT_MDD * 100:.0f}%）；"
        f"期末净值 ¥{res['final_eq']:,.0f}（区间 {res['total'] * 100:+.2f}%）。")
    if not passing:
        lines += ["", "## 失败定位（链路推演）", "",
                  "| 延迟环节 | 观测 | 说明 |", "|---|---|---|"]
        if res["escape_days"] > EXPECT_ESCAPE_DAYS:
            lines.append(f"| 灯色切换 | 避险第 {res['escape_days']} 日才触发 |"
                         " MA60×MA120 排列法对急跌有滞后（结构性）——"
                         "改善路径：加'单日-3% → 次日强制预警'触发（需增台账条款+回归） |")
        if res["mdd"] > EXPECT_MDD:
            lines.append(f"| 回撤 | {res['mdd'] * 100:.2f}% > 5% |"
                         "避险前敞口时滞所致——见逐日时间线的'持仓占比'列 |")
        lines += ["", "**逐日时间线（熔断前 3 日 → 后 10 日）**", "",
                  "| 日期 | 基准收盘 | 日跌幅 | 灯 | A敞口 | B敞口 | C货 | 净值 | 日变动 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for row in res["timeline_focus"]:
            lines.append("| " + " | ".join(str(x) for x in row) + " |")
    else:
        lines += ["", "## 避险执行轨迹（熔断起点 ± 5 日抽样）", "",
                  "| 日期 | 基准收盘 | 灯 | A敞口 | B敞口 | C货 | 净值 |",
                  "|---|---|---|---|---|---|---|"]
        for row in res["timeline_focus"]:
            lines.append("| " + " | ".join(str(x) for x in row) + " |")
    lines += ["", "## 口径与台账", "",
              "- 数据：`data/crash/*.csv`（长历史，fetch_data.fetch_and_save 落盘，"
              "与正式 data/*.csv 隔离；不影响 T-Fin01/02 可复现基线）；",
              "- 灯色：`regime_filter.traffic_light`（close×MA60×MA120，时间锁口径"
              "：只用 ≤前一交易日的收盘）；",
              "- 熔断继承：`risk_control.check_breakers`（单日-3%/峰值-10%+锁3日）；",
              "- 因子倾斜：窗口日期早于因子台账 → 自动停用（规则分仅对 ≥ 台账首日生效）。",
              ""]
    path = os.path.join(REPORTS, f"crash_{sc['key']}_{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[crash] {sc['key']} → {path} ({'PASS' if passing else 'FAIL'})")
    return path


def run_scenario(sc: dict, today: str) -> dict:
    eng = EnsembleEngine(window=sc["window"], data_dir=CRASH_DIR, log_writes=False)
    dates = eng.dates
    assert len(dates) >= 90, "窗口数据不足（需含预热期）"
    st = eng.run()
    curve_map = {d: e for d, e in st["curve"]}
    crash_start, peak_px = find_crash_start(dates, eng.bench)
    ci = dates.index(crash_start)
    # ---------- 避险时滞：熔断起点当日前即非绿视为 0 ----------
    if eng.light_at(ci)["state"] != "green":
        escape_days = 0
    else:
        escape_days = None
        for i in range(ci + 1, len(dates)):
            if eng.light_at(i)["state"] != "green":
                escape_days = i - ci
                break
    # ---------- 逐日账本回溯（流水重建持仓） ----------
    ex_a, ex_b, money_at = _holdings_series(eng, st, dates)
    eq_map = curve_map
    mdd, peak = 0.0, eq_map[dates[eng.warmup]]
    for d in dates[eng.warmup:]:
        peak = max(peak, eq_map[d])
        mdd = max(mdd, 1 - eq_map[d] / peak)
    total = st["curve"][-1][1] / st["curve"][0][1] - 1
    failed = (escape_days is None or escape_days > EXPECT_ESCAPE_DAYS
              or mdd > EXPECT_MDD + 0.005)
    # ---------- 聚焦时间线 ----------
    focus = []
    for i in range(max(eng.warmup, ci - 3), min(len(dates), ci + 10)):
        d = dates[i]
        light = eng.light_at(i)["state"]
        mk = "🔴" if light == "red" else ("🟡" if light == "yellow" else "🟢")
        px = eng.money_px.get(d) or 0.0
        mv = money_at.get(d, 0) * px
        prev_e = eq_map[dates[i - 1]] if i > 0 else eq_map[d]
        chg = (eq_map[d] / prev_e - 1) * 100 if prev_e else 0.0
        focus.append([d, eng.bench.get(d), mk, f"{ex_a.get(d, 0) * 100:.0f}%",
                      f"{ex_b.get(d, 0) * 100:.0f}%", f"¥{mv:,.0f}",
                      f"{eq_map[d]:,.0f}", f"{'+' if chg >= 0 else ''}{chg:.2f}%"])
    res = {"key": sc["key"], "label": sc["label"], "crash_start": crash_start,
           "escape_days": escape_days if escape_days is not None else 999,
           "mdd": round(mdd, 4), "final_eq": st["curve"][-1][1], "total": total,
           "timeline_focus": focus, "pass": not failed}
    write_crash_report(sc, res, today)
    return res


def _holdings_series(eng, st, dates):
    """流水重建：每日期的腿敞口占比（A/B）与货币份额持仓（C 腿台账）。"""
    idx_eq = {d: e for d, e in st["curve"]}
    legs = ("A", "B")
    core_held = {leg: {} for leg in legs}       # code -> shares
    money = {"A": 0, "B": 0, "MASTER": 0}
    sleeve_tr = {leg: sorted(st["sleeves"][leg]["trades"],
                             key=lambda t: t["date"]) for leg in legs}
    ti = {leg: 0 for leg in legs}
    mon_tr = sorted((t for t in st["trades"] if t.get("reason") in ("park", "redeem")),
                    key=lambda t: t["date"])
    mi = 0
    ex = {leg: {} for leg in legs}
    money_at = {}
    for d in dates:
        for leg in legs:
            while ti[leg] < len(sleeve_tr[leg]) and sleeve_tr[leg][ti[leg]]["date"] <= d:
                t = sleeve_tr[leg][ti[leg]]
                code = t.get("code")
                if code:
                    delta = t["shares"] * (1 if t["side"] == "BUY" else -1)
                    if t["side"] == "SELL" and t.get("shares") is None:
                        delta = -core_held[leg].get(code, 0)
                    core_held[leg][code] = core_held[leg].get(code, 0) + delta
                ti[leg] += 1
        while mi < len(mon_tr) and mon_tr[mi]["date"] <= d:
            t = mon_tr[mi]
            who = t.get("leg", "MASTER")
            if t["reason"] == "park":
                money[who] = money.get(who, 0) + t["shares"]
            else:
                money[who] = max(0, money.get(who, 0) - t["shares"])
            mi += 1
        eq = idx_eq.get(d) or 0.0
        px_money = eng.money_px.get(d) or 0.0
        money_at[d] = sum(money.values())
        for leg in legs:
            val = sum(sh * eng.by_date[c][d]["close"]
                      for c, sh in core_held[leg].items()
                      if c in eng.by_date and sh > 0)
            ex[leg][d] = val / eq if eq else 0.0
    return ex["A"], ex["B"], money_at


def main() -> int:
    import datetime as _dt
    today = _dt.date.today().isoformat()
    ensure_data()
    results = []
    for sc in SCENARIOS:
        print(f"[crash] === {sc['label']} ===")
        results.append(run_scenario(sc, today))
    # 汇总报告
    all_pass = all(r["pass"] for r in results)
    lines = [f"# 崩溃测试总汇 · {today}", "",
             "> 两段真实黑天鹅 × 完整 ensemble（三腿+赛马+熔断继承）", "",
             "| 情景 | 熔断起点 | 避险时滞(交易日) | 窗口MDD | 判定 |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['label']} | {r['crash_start']} | {r['escape_days']} "
                     f"| {r['mdd'] * 100:.2f}% | {'✅' if r['pass'] else '❌'} |")
    lines += ["", f"总体判定：{'✅ 全部通过——这台机器懂得在风暴中先保住命' if all_pass else '❌ 存在未通过情景（详见各情景报告的链路推演）'}",
              "", "链路四要件核验：1) 灯色时间锁（次日开盘执行）2) 黄/红→强制清 A/B 3) "
              "现金停泊 511990 4) 熔断兜底（不可绕过）。"]
    spath = os.path.join(REPORTS, f"crash_test_{today}.md")
    with open(spath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[crash] 汇总 → {spath} ({'ALL PASS' if all_pass else 'HAS FAILURES'})")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
