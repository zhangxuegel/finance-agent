# -*- coding: utf-8 -*-
"""dashboard.py —— 全息量化看板（T-Fin06 模块 1）

技术选型说明（诚实登记）：任务书建议 Streamlit；Termux/aarch64 环境下
Streamlit 依赖链（pyarrow 等）无法在时限内源码编译完成，故采用 **Flask +
纯 SVG/CSS 手绘图表**：零 numpy/pandas 依赖、一眼可审计、同为本地 Web 看板。

启动：  python3 scripts/dashboard.py [--port 8501]
访问：  http://127.0.0.1:8501   （Termux 内浏览器）

视图：
  · 顶部指标卡：净值 / 今日盈亏% / 大盘灯 / 最大回撤 / 夏普 / 卡玛
  · 持仓饼图 CSS conic-gradient：A腿(趋势) / B腿(震荡) / C腿(货币防守)
  · 净值曲线 SVG：影子净值 vs 沪深300（data/shadow_live.db eod 表）
  · 交易流水表：最近 20 笔撮合（倒序）
数据纪律：一切数字读台账（sqlite + data/live/*.csv + ensemble_state.json），
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


def _legs_of(st: dict) -> tuple:
    """台账赛马腿自适应（T-Fin28：D/E 腿并发）；旧双色台账自动回退 A/B。"""
    return tuple(l for l in ("A", "B", "D", "E") if l in st.get("sleeves", {}))
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

DB = os.path.join(ROOT, "data", "shadow_live.db")
STATE = os.path.join(ROOT, "data", "shadow_state.json")
ENS_STATE = os.path.join(ROOT, "data", "ensemble_state.json")
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


def collect() -> dict:
    with open(STATE, encoding="utf-8") as f:
        st = json.load(f)
    with open(ENS_STATE, encoding="utf-8") as f:
        ens = json.load(f)
    eod = _db_rows("SELECT * FROM eod ORDER BY trade_date")
    fills = _db_rows("SELECT ts, trade_date, leg, code, side, shares, price, "
                     "fee, reason FROM fills ORDER BY id DESC LIMIT 20")
    money_code = ens["ensemble"]["C"]["code"] \
        if "ensemble" in ens else "511990"
    mpx = _close_of(money_code) or st.get("money", {}).get("last_px") or 0.0
    nav = eod[-1]["nav"] if eod else 0.0
    day_ret = eod[-1]["day_ret"] if eod else 0.0
    alphas = [r["alpha"] for r in eod]
    navs = [r["nav"] for r in eod]
    sharpe, calmar, mdd = _series(navs)
    # 大盘灯（引擎台账 gate_state 的 light，收盘口径）
    gs = ens.get("gate_state") or {}
    light = LIGHT_LABEL.get(gs.get("light", "green"), LIGHT_LABEL["green"])
    # 腿权益占比（最新台账收盘；C = MASTER 货币 + 母现金）
    vals = {}
    for leg in _legs_of(st):
        port = st["sleeves"][leg]
        v = port["cash"] + sum(pp["shares"] * (_close_of(c) or pp["cost"])
                               for c, pp in port["positions"].items())
        v += st["money"].get(leg, 0) * mpx
        vals[leg] = v
    vals["C"] = st["money"].get("MASTER", 0) * mpx + st["master_cash"]
    return {"nav": nav, "day_ret": day_ret, "light": light,
            "light_state": gs.get("light", "green"),
            "defense": bool(gs.get("defense")),
            "mdd": mdd, "sharpe": sharpe, "calmar": calmar,
            "alpha_sum": sum(alphas), "vals": vals,
            "curve": [(r["trade_date"], r["nav"], r["bench_index_close"])
                      for r in eod],
            "ens_curve": [[d, float(v)] for d, v in
                          (ens.get("curve") or [])[-120:]],
            "fills": fills,
            "panic": os.path.exists(os.path.join(ROOT, "PANIC_LOCK.flag")),
            "ts": datetime.now().strftime("%F %T"), "n_eod": len(eod)}


def svg_curve(curve, ens_curve=None, w=880, h=260) -> str:
    """影子净值 vs 沪深300（+ 正式台账回放基准，虚线）SVG，全部读台账。"""
    series_ready = len(curve) >= 2 or (ens_curve and len(ens_curve) >= 2)
    if not series_ready:
        return ("<svg width=100% height=140><text x=20 y=70 "
                "fill='#889'>净值曲线待积累（连续追踪第 1 天）</text></svg>")
    xs = [c[0] for c in curve]
    base_nav, base_bx = curve[0][1], curve[0][2]
    def norm(vals, v0):
        return [v / v0 if v0 else 0 for v in vals]
    ns = norm([c[1] for c in curve], base_nav)
    bs = norm([c[2] for c in curve], base_bx or base_nav)
    es = []
    if ens_curve and len(ens_curve) >= 2:
        es = norm([v for _, v in ens_curve], ens_curve[0][1])
        # 对齐尾部（影子台账短于台账回放 → 补前导空位，直接截尾对齐展示）
        if len(es) < len(ns):
            es = [None] * (len(ns) - len(es)) + es
        elif len(es) > len(ns):
            es = es[-len(ns):]
            ns = ns + [None] * (len(es) - len(ns))
            bs = bs + [None] * (len(es) - len(bs))
    all_v = [v for v in (ns + bs + es) if v is not None]
    lo = min(all_v) * 0.998
    hi = max(all_v) * 1.002
    pad, steps = 36, max(1, max(len(ns), len(es)) - 1)
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
    ens_path = (f'<polyline fill="none" stroke="#8a97a8" stroke-width="1.6" '
                f'stroke-dasharray="5 5" points="{path(es)}"/>' if es else "")
    legend3 = ('<span style="color:#8a97a8">■</span> 正式台账（离线回放基准，虚线）　'
               if es else "")
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="xMidYMid meet">'
            f'{grid}{labels}{ens_path}'
            f'<polyline fill="none" stroke="#e0a848" stroke-width="2.2" '
            f'points="{path(bs)}"/>'
            f'<polyline fill="none" stroke="#4aa3df" stroke-width="2.6" '
            f'points="{path(ns)}"/></svg>'
            f'<div style="font-size:11px;color:#7d8fa3">{legend3}'
            '<span style="color:#4aa3df">■</span> 影子净值　'
            '<span style="color:#e0a848">■</span> 沪深300（均归一化到起点=1）</div>')


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
<h2>持仓分布（含各腿名下货币份额；台账最新收盘口径）</h2>
<div class="grid" style="grid-template-columns:auto 1fr">
 <div><div class="pie" style="background:$grad"></div></div>
 <div class="card legend">$legend</div>
</div>
<h2>净值曲线 · 影子 vs 沪深300（$n_eod 个交易日台账）</h2>
<div class="card">$curve_svg
<div style="font-size:11px;color:#7d8fa3;margin-top:6px">
 累计超额（Alpha 合计）$alpha_sum</div></div>
<h2>最近撮合明细（20 笔，倒序）</h2>
<div class="card"><table><tr><th>时间</th><th>腿</th><th>标的</th><th>方向</th>
<th>数量</th><th>价格</th><th>手续费</th><th>依据</th></tr>
$rows</table></div>
<p style="color:#61738a;font-size:11px;margin-top:18px">
台账源：data/shadow_live.db · data/shadow_state.json · data/live/*.csv ·
data/ensemble_state.json。全部数字可溯源；本看板只读，不写台账，不下单。</p>
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
            f"<tr><td>{f['ts'][5:16]}</td><td>{f['leg']}</td>"
            f"<td>{f['code']}</td>"
            f"<td class=\"{'pos' if f['side'] == 'BUY' else 'neg'}\">{f['side']}</td>"
            f"<td>{f['shares']:,}</td><td>{f['price']}</td>"
            f"<td>¥{f['fee']:.2f}</td><td>{f['reason']}</td></tr>" for f in d["fills"])
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
        curve_svg=svg_curve(d["curve"], d.get("ens_curve")), n_eod=d["n_eod"],
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
