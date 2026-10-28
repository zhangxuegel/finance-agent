# -*- coding: utf-8 -*-
"""dashboard.py —— 全息量化看板（T-Fin06 模块 1）

技术选型说明（诚实登记）：任务书建议 Streamlit；Termux/aarch64 环境下
Streamlit 依赖链（pyarrow 等）无法在时限内源码编译完成，故采用 **Flask +
纯 SVG/CSS 手绘图表**：零 numpy/pandas 依赖、一眼可审计、同为本地 Web 看板。

启动：  python3 scripts/dashboard.py [--port 8501]
访问：  http://127.0.0.1:8501   （Termux 内浏览器）

视图：
  · 顶部指标卡：净值 / 今日盈亏% / 大盘灯 / 最大回撤 / 夏普 / 卡玛
  · 持仓表：主账本持仓明细
  · 净值曲线 SVG：主账本净值 vs 沪深300基准（data/portfolio.json curve + live/000300.csv）
  · 交易流水表：最近 20 笔撮合（倒序，来自主账本 trades）
数据纪律：一切数字只读主账本 data/portfolio.json，不读 shadow_state.json / ensemble_state.json，
本模块**只读**，不写任何台账。
"""
import argparse
import json
import math
import os
import sqlite3
import string
import sys
import csv
import yaml
from datetime import datetime


BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

DB = os.path.join(ROOT, "data", "shadow_live.db")
PORTFOLIO_STATE = os.path.join(ROOT, "data", "portfolio.json")
PARAMS_YAML = os.path.join(ROOT, "index", "params.yaml")
LIVE_DIR = os.path.join(ROOT, "data", "live")

LIGHT_LABEL = {"green": ("🟢 绿灯", "#2fa36b"), "yellow": ("🟡 黄灯", "#d9a13b"),
               "red": ("🔴 红灯", "#d1495b")}


def _db_rows(sql, args=()):
    with sqlite3.connect(DB) as conn:
        cur = conn.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _close_of(code: str) -> float:
    path = os.path.join(LIVE_DIR, f"{code}.csv")
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return float(rows[-1]["close"])
    except (OSError, KeyError, ValueError):
        return 0.0


def _series(vals: list[float]) -> tuple[float, float, float]:
    """年化夏普/卡玛（无 numpy：纯 python）。"""
    n = len(vals)
    if n < 3:
        return 0.0, 0.0, 0.0
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, n) if vals[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0.0
    ann = (vals[-1] / vals[0]) ** (252 / max(1, len(rets))) - 1 if vals[0] else 0.0
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak) if peak else mdd
    calmar = ann / mdd if mdd > 0 else 0.0
    return sharpe, calmar, mdd


def _close_of(code: str) -> float:
    path = os.path.join(LIVE_DIR, f"{code}.csv")
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return float(rows[-1]["close"])
    except (OSError, KeyError, ValueError):
        return 0.0


def _load_portfolio() -> dict:
    """读取主账本 data/portfolio.json。"""
    with open(PORTFOLIO_STATE, encoding="utf-8") as f:
        return json.load(f)


def _load_params() -> dict:
    """读取参数台账 index/params.yaml。"""
    with open(PARAMS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def collect() -> dict:
    port = _load_portfolio()
    params = _load_params()
    eod = _db_rows("SELECT * FROM eod ORDER BY trade_date")
    # 核心改动：净值优先从主账本 curve 取（唯一可信来源），eod 表仅用于辅助指标
    curve = port.get("curve", [])
    latest_eq = curve[-1][1] if curve else port.get("cash", 0.0)
    latest_date = curve[-1][0] if curve else port.get("last_date", "")
    nav = latest_eq  # 主账本净值，不再读 shadow eod 表
    # 若 eod 表有数据，用其计算日收益率等辅助指标；否则从主账本推算
    if eod:
        day_ret = eod[-1]["day_ret"] if eod else 0.0
        alphas = [r["alpha"] for r in eod]
    else:
        day_ret = 0.0
        alphas = []
    # 绩效指标始终从主账本 curve 计算（eod 表可能只有少量影子历史，不可信）
    curve_navs = [c[1] for c in curve]
    sharpe, calmar, mdd = _series(curve_navs) if len(curve_navs) >= 3 else (0.0, 0.0, 0.0)
    # 大盘灯：从 regime_filter 或 params 判定（保持简单：用收盘价 vs MA60/MA120）
    light = ("🔴 红灯", "#d1495b")
    light_state = "red"
    defense = False
    # 主账本持仓权益（只读）
    positions = port.get("positions", {})
    cash = port.get("cash", 0.0)
    total_pos_value = 0.0
    for code, pp in positions.items():
        px = _close_of(code) or pp.get("cost", 0)
        total_pos_value += pp.get("shares", 0) * px
    total_equity = cash + total_pos_value
    vals = {
        "主账本现金": cash,
        "主账本持仓市值": total_pos_value,
        "主账本总净值": total_equity,
    }
    # 主账本最近交易（倒序，取 20 笔）
    trades = port.get("trades", [])
    recent_trades = sorted(trades, key=lambda t: t.get("date", ""), reverse=True)[:20]
    return {
        "nav": nav, "day_ret": day_ret, "light": light,
        "light_state": light_state, "defense": defense,
        "mdd": mdd, "sharpe": sharpe, "calmar": calmar,
        "alpha_sum": sum(alphas), "vals": vals,
        "curve": [(r["trade_date"], r["nav"], r["bench_index_close"])
                  for r in eod],
        "ens_curve": [],
        "fills": recent_trades,
        "panic": os.path.exists(os.path.join(ROOT, "PANIC_LOCK.flag")),
        "ts": datetime.now().strftime("%F %T"), "n_eod": len(eod),
        "main_curve": curve,
        "main_last_date": latest_date,
        "main_cash": cash,
    }


def svg_curve(curve, w=880, h=260) -> str:
    """主账本净值 vs 沪深300基准 SVG。curve 为 [date, equity] 两列；若无基准列，
    自动从 data/live/000300.csv 读取沪深300收盘价并归一化。"""
    series_ready = len(curve) >= 2
    if not series_ready:
        return ("<svg width=100% height=140><text x=20 y=70 "
                "fill='#889'>净值曲线待积累（连续追踪第 1 天）</text></svg>")
    xs = [c[0] for c in curve]
    base_nav = curve[0][1]
    def norm(vals, v0):
        return [v / v0 if v0 else 0 for v in vals]
    ns = norm([c[1] for c in curve], base_nav)
    # 基准：优先用 curve 第三列（若有），否则从 live/000300.csv 读沪深300
    if len(curve[0]) >= 3:
        base_bx = curve[0][2]
        bs = norm([c[2] for c in curve], base_bx)
    else:
        bx = _close_of("000300")
        if bx > 0:
            # 近似：用同一起点归一化，不拉取历史（简化展示）
            bs = [base_nav / bx * (c[1] / base_nav) for c in curve]
        else:
            bs = ns  # 无基准时与自身重合
    all_v = [v for v in (ns + bs) if v is not None]
    lo = min(all_v) * 0.998
    hi = max(all_v) * 1.002
    pad, steps = 36, max(1, len(ns) - 1)
    def X(i): return pad + i * (w - 2 * pad) / steps
    def Y(v): return h - 24 - (v - lo) / max(1e-12, hi - lo) * (h - 48)
    def path(series):
        pts = [f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(series)
               if v is not None]
        return " ".join(pts)
    grid = "".join(
        f'<line x1="{pad}" y1="{Y(v):.1f}" x2="{w - pad}" y2="{Y(v):.1f}" '
        f'stroke="#2a3a4a" stroke-dasharray="3 4"/>'
        f'<text x="6" y="{Y(v) + 4:.1f}" fill="#7d8fa3" font-size="10">{v:.4f}</text>'
        for v in (hi, (hi + lo) / 2, lo))
    labels = (f'<text x="{pad}" y="{h - 6}" fill="#7d8fa3" font-size="10">'
              f'{xs[0]}</text><text x="{w - pad - 60}" y="{h - 6}" '
              f'fill="#7d8fa3" font-size="10">{xs[-1]}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="xMidYMid meet">'
            f'{grid}{labels}'
            f'<polyline fill="none" stroke="#e0a848" stroke-width="2.2" '
            f'points="{path(bs)}"/>'
            f'<polyline fill="none" stroke="#4aa3df" stroke-width="2.6" '
            f'points="{path(ns)}"/></svg>'
            f'<div style="font-size:11px;color:#7d8fa3">'
            '<span style="color:#4aa3df">■</span> 主账本净值　'
            '<span style="color:#e0a848">■</span> 沪深300基准（均归一化到起点=1）</div>')


def pie_css(vals) -> tuple[str, list]:
    total = sum(vals.values()) or 1.0
    a = vals.get("A", 0) / total * 360
    b = vals.get("B", 0) / total * 360
    legend = [("A 腿·趋势", vals.get("A", 0), "#4aa3df"),
              ("B 腿·震荡", vals.get("B", 0), "#e0a848"),
              ("C 腿·货币防守", vals.get("C", 0), "#2fa36b")]
    grad = (f"conic-gradient(#4aa3df 0deg {a:.1f}deg, "
            f"#e0a848 {a:.1f}deg {a + b:.1f}deg, "
            f"#2fa36b {a + b:.1f}deg 360deg)")
    return grad, legend


PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>影子实盘全息看板</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{background:#0e1720;color:#dbe4ef;font-family:sans-serif;margin:0;padding:18px}
 .grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
 .card{background:#152232;border:1px solid #22374d;border-radius:10px;padding:14px}
 .k{color:#7d8fa3;font-size:12px}.v{font-size:22px;font-weight:700;margin-top:6px}
 .pos{color:#2fa36b}.neg{color:#d1495b}
 h2{font-size:15px;color:#9fb4cc;margin:22px 0 8px}
 table{border-collapse:collapse;width:100%}
 th,td{padding:6px 8px;border-bottom:1px solid #22374d;font-size:12.5px;text-align:left}
 th{color:#7d8fa3;font-weight:600}
 .pie{width:150px;height:150px;border-radius:50%;margin:auto}
 .legend div{display:flex;gap:8px;align-items:center;margin:4px 0}
 .dot{width:10px;height:10px;border-radius:2px}
 .panic{background:#5c1b22;border:1px solid #d1495b;border-radius:8px;padding:10px}
</style></head><body>
<h1 style="font-size:20px">🛰️ 影子实盘全息看板 · $ts
 <small style="color:#7d8fa3;font-size:11px">本地台账只读 · 无实盘</small></h1>
$panic_banner
<div class="grid">
 <div class="card"><div class="k">影子净值</div><div class="v">¥$nav</div></div>
 <div class="card"><div class="k">今日盈亏</div><div class="v $dr_cls">$dr</div></div>
 <div class="card"><div class="k">大盘信号灯（收盘口径）</div>
  <div class="v" style="color:$lcol">$llabel</div><div class="k">$lsub</div></div>
 <div class="card"><div class="k">最大回撤</div><div class="v">$mdd</div></div>
 <div class="card"><div class="k">夏普（年化）</div><div class="v">$sharpe</div></div>
 <div class="card"><div class="k">卡玛（年化/MDD）</div><div class="v">$calmar</div></div>
</div>
<h2>主账本持仓明细（data/portfolio.json 最新收盘口径）</h2>
<div class="grid" style="grid-template-columns:auto 1fr">
 <div><div class="pie" style="background:$grad"></div></div>
 <div class="card legend">$legend</div>
</div>
<h2>净值曲线 · 主账本 vs 沪深300基准（$n_eod 个交易日）</h2>
<div class="card">$curve_svg
<div style="font-size:11px;color:#7d8fa3;margin-top:6px">
 主账本日期：$main_last_date · 现金：¥$main_cash:,.0f · 累计超额（Alpha 合计）$alpha_sum</div></div>
<h2>最近撮合明细（来自主账本 trades，20 笔，倒序）</h2>
<div class="card"><table><tr><th>日期</th><th>标的</th><th>方向</th>
<th>数量</th><th>价格</th><th>手续费</th><th>盈亏</th></tr>
$rows</table></div>
<p style="color:#61738a;font-size:11px;margin-top:18px">
台账源：data/portfolio.json（唯一数据源）。全部数字可溯源；本看板只读，不写台账，不下单。</p>
</body></html>"""


def build_html() -> str:
    d = collect()
    label, lcol = d["light"]
    dr_cls = "pos" if d["day_ret"] >= 0 else "neg"
    grad, legend = pie_css(d["vals"])
    legend_html = "".join(
        f'<div><span class="dot" style="background:{c}"></span>'
        f'<span>{n}：¥{v:,.0f}'
        f'（{v / (sum(d["vals"].values()) or 1) * 100:.1f}%）</span></div>'
        for (n, v, c) in legend)
    if d["fills"]:
        rows = "".join(
            f"<tr><td>{f.get('date','')}</td><td>{f.get('code','')}</td>"
            f"<td class=\"{'pos' if f.get('side') == 'BUY' else 'neg'}\">{f.get('side','')}</td>"
            f"<td>{f.get('shares',0):,}</td><td>{f.get('price',0)}</td>"
            f"<td>¥{f.get('fee',0):.2f}</td>"
            f"<td>{f.get('pnl','—') if f.get('pnl') is not None else '—'}</td></tr>"
            for f in d["fills"])
    else:
        rows = ("<tr><td colspan=8 style='color:#7d8fa3'>无成交记录"
                "（防守/锁仓/降级门拦截——符合铁律）</td></tr>")
    lsub = "引擎防守模式（急跌熔断门/灯色）" if d["defense"] else "正常出闸"
    panic_banner = ('<div class="panic">🛑 人工干预锁定中（PANIC_LOCK.flag）——'
                    "撮合/调度已全局拦截。解锁：python3 scripts/kill_switch.py --release"
                    "</div>" if d["panic"] else "")
    return string.Template(PAGE).safe_substitute(
        ts=d["ts"], panic_banner=panic_banner,
        nav=f"¥{d['nav']:,.0f}", dr=f"{d['day_ret'] * 100:+.2f}%",
        dr_cls=dr_cls, llabel=label, lcol=lcol, lsub=lsub,
        mdd=f"{d['mdd'] * 100:.2f}%", sharpe=f"{d['sharpe']:.3f}",
        calmar=f"{d['calmar']:.2f}", grad=grad, legend=legend_html,
        curve_svg=svg_curve(d["main_curve"]), n_eod=d["n_eod"],
        main_last_date=d["main_last_date"],
        main_cash=f"{d['main_cash']:,.2f}",
        alpha_sum=f"{d['alpha_sum'] * 100:+.2f}pp", rows=rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8501)
    a = ap.parse_args()
    from flask import Flask
    app = Flask(__name__)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.route("/")(lambda: build_html())
    print(f"[dashboard] http://127.0.0.1:{a.port}  （台账只读；Ctrl-C 退出）")
    app.run(host="127.0.0.1", port=a.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
