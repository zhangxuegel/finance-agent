# -*- coding: utf-8 -*-
"""dashboard_cloud.py —— 云端原生 Streamlit 看板（T-Fin12 手机竖屏重构）

**云端环境专用**：数据源不读本地 SQLite——一切从云端 Supabase 拉取（只读）。
本脚本被 Streamlit Cloud 运行（requirements.txt 见仓库根目录）；
本地仅作开发/自检（streamlit lazy-import，导入无害）。

配置来源（三重兜底，同 README_DEPLOY.md）：
  1) Streamlit Cloud: .streamlit/secrets.toml 的 [supabase] url/key
  2) 环境变量 SUPABASE_URL / SUPABASE_KEY
  3) 环境变量 SUPABASE_TABLES（可选表前缀，默认 sb_）

T-Fin12 竖屏三视图（顶层三卡：
  · 当前净值  · 今日盈亏（含昨日差）  · 红绿灯状态）：
  · 红绿灯判定（纯函数，来自云端 eod 行 defense/lock_remaining/day_ret）：
      锁仓剩余>0 → 🔴 熔断锁仓；防守=1 → 🟡 防守日；
      当日 ≤-2% → 🟠 急跌预警；其余 → 🟢 正常。
  · 净值折线图：影子 vs 沪深300（归一化=1，独立起始锚）
  · 最近交易流水表：按 id 倒序 40 笔（涨停拒买单独标旗）

铁律映射：只读（云端拉取→展示，绝不写库）；一切数字来自 synced 台账行；
无任何 LLM/人工订单注水——净值/流水全部由本地确定性引擎产出后增量推送。
"""
import os
import sys
import math

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
if BASE not in sys.path:
    sys.path.insert(0, BASE)

try:  # 云端运行必需；本地（无 streamlit）导入无害
    import streamlit as st
except ImportError:  # noqa: BLE001 —— 本地纯函数自检路径
    st = None

try:
    import pandas as pd
except ImportError:  # noqa: BLE001
    pd = None

# 竖屏首行初始净值兜底（云端行 nav_prev 缺失且只有 1 行时用）
INITIAL_NAV = 1_000_000.0

_LIGHT_CSS = """
<style>
/* —— T-Fin12 手机竖屏紧凑化 —— */
.block-container {padding: 1.0rem 1.0rem 2.5rem;}
[data-testid="stMetric"] {background: rgba(128,128,128,0.08);
  border-radius: 10px; padding: 10px 12px;}
[data-testid="stMetricLabel"] {font-size: 0.78rem;}
[data-testid="stMetricValue"] {font-size: 1.30rem; font-weight: 700;}
div[data-testid="stMarkdownContainer"] .fa-badge {
  font-size: 1.05rem; font-weight: 700; line-height: 1.5;
  border-radius: 10px; padding: 10px 12px; width: 100%;
  display: inline-block; text-align: center;}
.fa-badge.fa-red   {background: rgba(220,60,60,0.18);
  border: 1px solid rgba(220,60,60,0.55);}
.fa-badge.fa-amber {background: rgba(230,160,30,0.18);
  border: 1px solid rgba(230,160,30,0.55);}
.fa-badge.fa-green {background: rgba(60,180,90,0.16);
  border: 1px solid rgba(60,180,90,0.55);}
</style>
"""


def _pick(section, names: list[str]) -> str:
    """纯函数：从 dict/映射里按候选名取第一个非空字符串（大小写兼容）。
    兼容 st.secrets["supabase"] 为 Mapping 或 dict-get 可用的情况。"""
    for n in names:
        try:
            v = section.get(n, "")
        except AttributeError:
            v = section[n] if n in section else ""
        if v:
            return str(v)
    return ""


_URL_NAMES = ["url", "supabase_url", "SUPABASE_URL"]
_KEY_NAMES = ["key", "supabase_key", "SUPABASE_KEY"]


def config_from_env() -> tuple[str, str, str]:
    """(url, key, prefix)。读取优先级（T-Fin14 兼容矩阵）：
      1) st.secrets["supabase"] 下 url/supabase_url/SUPABASE_URL（+key 同理）
      2) st.secrets 顶层同上候选名（有人把配置平铺写在表外）
      3) os.environ SUPABASE_URL / SUPABASE_KEY
    secrets.toml → 环境变量 双兜底。"""
    url = key = ""
    if st is not None:
        for src in ("supabase", None):
            try:
                sec = st.secrets[src] if src else dict(st.secrets)
            except Exception:  # noqa: BLE001 —— secrets 缺该表/整表缺
                continue
            url = _pick(sec, _URL_NAMES)
            key = _pick(sec, _KEY_NAMES)
            if url or key:
                break
    url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
    key = key or os.environ.get("SUPABASE_KEY", "")
    prefix = os.environ.get("SUPABASE_TABLES", "sb_")
    return url, key, prefix


def _session(url: str, key: str):
    import requests
    s = requests.Session()
    s.headers.update({"apikey": key, "Authorization": f"Bearer {key}",
                      "User-Agent": "finance-agent-cloud-dashboard/1.2"})
    return s, url


def fetch_rows(table: str, url: str, key: str, limit: int = 2000,
               order: str = "trade_date.asc") -> list[dict]:
    """云端只读拉取：order 用 supabase 语法（如 id.desc）。"""
    s, base = _session(url, key)
    import requests
    r = s.get(f"{base}/rest/v1/{table}",
              params={"select": "*", "order": order, "limit": str(limit)},
              timeout=(5, 20))
    if r.status_code != 200:
        raise RuntimeError(f"云端读取 {table} 失败 HTTP {r.status_code}")
    rows = r.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"云端 {table} 返回异常类型（非 JSON 数组）")
    return rows


def compute_metrics(eod: list[dict]) -> dict:
    """纯函数（本地可单测）：eod 行 → 指标卡数字（与云端行序无关，内部按
    trade_date 排序）。V11b 兼容：行可缺 nav_prev/alpha（or 0 兜底）。"""
    rows = sorted(eod, key=lambda r: r.get("trade_date") or "")
    navs = [float(r["nav"]) for r in rows]
    if not navs:
        return {"nav": 0.0, "nav_prev": 0.0, "pnl": 0.0, "day_ret": 0.0,
                "cum": 0.0, "mdd": 0.0, "sharpe": 0.0, "alpha_sum": 0.0,
                "days": 0}
    last = rows[-1]
    nav = navs[-1]
    nav_prev = next((float(x) for x in
                     [last.get("nav_prev")] if x not in (None, "")), None)
    if nav_prev is None:
        nav_prev = navs[-2] if len(navs) > 1 else INITIAL_NAV
    pnl = nav - nav_prev
    day_ret = pnl / nav_prev if nav_prev > 0 else 0.0
    cum = navs[-1] / navs[0] - 1 if navs[0] else 0.0
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))
            if navs[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((x - mean) ** 2 for x in rets) / len(rets) if rets else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0.0
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak) if peak else mdd
    return {"nav": nav, "nav_prev": nav_prev, "pnl": pnl,
            "day_ret": day_ret, "cum": cum, "mdd": mdd, "sharpe": sharpe,
            "alpha_sum": sum(float(r.get("alpha") or 0) for r in rows),
            "days": len(rows)}


def light_state(last: dict) -> dict:
    """纯函数：最新 eod 行 → 红绿灯。（判据全部来自云端台账行字段）

    优先级：熔断锁仓 🔴 > 防守日 🟡 > 当日急跌(≤-2%) 🟠 > 正常 🟢
    """
    lock = int(float(last.get("lock_remaining") or 0))
    defense = bool(int(float(last.get("defense") or 0)))
    try:
        day_ret = float(last.get("day_ret") or 0)
    except (TypeError, ValueError):
        day_ret = 0.0
    if lock > 0:
        return {"key": "red", "cls": "fa-red", "emoji": "🔴",
                "label": f"熔断锁仓 · 剩余 {lock} 日"}
    if defense:
        return {"key": "amber", "cls": "fa-amber", "emoji": "🟡",
                "label": "防守日 · 强制清仓/防御 regime"}
    if day_ret <= -0.02:
        return {"key": "amber", "cls": "fa-amber", "emoji": "🟠",
                "label": f"急跌预警 · 当日 {day_ret:+.2%}"}
    return {"key": "green", "cls": "fa-green", "emoji": "🟢",
            "label": "绿灯 · 正常运转"}


def normalize(curve: list[dict]) -> pd.DataFrame | list[dict]:
    """影子 / 沪深300 归一化到起点=1（独立起始锚，差值=执行差异）。"""
    if not curve:
        return pd.DataFrame() if pd else []
    rows_sorted = sorted(curve, key=lambda r: r.get("trade_date") or "")
    b0 = float(rows_sorted[0]["nav"])
    x0 = float(rows_sorted[0]["bench_index_close"] or rows_sorted[0]["nav"])
    rows = [{"date": r["trade_date"],
             "影子净值": float(r["nav"]) / b0 if b0 else 0,
             "沪深300": float(r["bench_index_close"] or 0) / x0 if x0 else 0}
            for r in rows_sorted]
    return pd.DataFrame(rows) if pd else rows


def fills_frame(fills: list[dict], n: int = 40) -> pd.DataFrame | list[dict]:
    """纯函数：云端 fills 行 → 手机可读流水表（按 id 倒序最近 n 笔）。"""
    rows = sorted(fills, key=lambda r: r.get("id") or 0)[-n:][::-1]
    out = []
    for r in rows:
        side = r.get("side") or ""
        flag = " 🚫涨停拒买" if int(float(r.get("limit_blocked") or 0)) else ""
        shares = int(float(r.get("shares") or 0))
        out.append({
            "时间": (r.get("ts") or "")[-8:],
            "日期": r.get("trade_date") or "",
            "腿": r.get("leg") or "",
            "代码": r.get("code") or "",
            "方向": side,
            "股数": f"{shares:,}" if shares else "",
            "成交价(¥)": f"{float(r.get('price') or 0):.3f}",
            "手续费(¥)": f"{float(r.get('fee') or 0):.2f}",
            "事由": (r.get("reason") or "")[:28] + flag,
        })
    return pd.DataFrame(out) if pd else out


# ---------------- Streamlit 呈现（lazy，本地不导入 UI） ----------------

def _badge(light: dict):
    st.markdown(f'<div class="fa-badge {light["cls"]}">'
                f'{light["emoji"]} {light["label"]}'
                f"</div>", unsafe_allow_html=True)


def _refresh_button():
    if st.button("🔄 刷新云端数据", type="secondary"):
        st.cache_data.clear()
        st.rerun()


def run_app():
    """Streamlit 页面主体（云端入口，竖屏优先）。"""
    st.set_page_config(page_title="影子实盘云端看板",
                       page_icon="🛰️", layout="centered")
    st.markdown(_LIGHT_CSS, unsafe_allow_html=True)
    url, key, prefix = config_from_env()
    if not url or not key:
        st.error("未配置云端数据库（secrets.toml 或环境变量 SUPABASE_URL/"
                 "SUPABASE_KEY）——见主仓库 README_DEPLOY.md 第 2 步。")
        st.stop()
    try:
        eod = _cached_fetch(url, key, f"{prefix}shadow_eod",
                            "trade_date.asc", 2000)
        fills = _cached_fetch(url, key, f"{prefix}shadow_fills",
                              "id.desc", 60)
    except Exception as e:  # noqa: BLE001 —— 云端故障给用户可读指引
        st.error(f"云端读取失败：{e}")
        _refresh_button()
        st.stop()
    if not eod:
        st.info("云端台账为空：本地跑一次 `python3 scripts/sync_to_cloud.py` "
                "后刷新。看板只读，不落任何数字。")
        st.stop()
    last = max(eod, key=lambda r: r.get("trade_date") or "")
    light = light_state(last)
    m = compute_metrics(eod)

    # —— 顶层三卡：净值 / 今日盈亏 / 红绿灯（任务书硬要求）——
    c1, c2, c3 = st.columns(3)
    c1.metric("🛰️ 当前净值", f"¥{m['nav']:,.0f}",
              delta=f"{m['cum']:+.2%} 累计" if m["days"] > 1 else None)
    c2.metric("今日盈亏", f"{m['pnl']:+,.0f} ¥",
              delta=f"{m['day_ret']:+.2%}" if m["days"] > 1 else None)
    with c3:
        st.markdown('<div style="font-size:0.78rem;margin-bottom:2px;">'
                    f"红绿灯（{last.get('trade_date', '')}）</div>",
                    unsafe_allow_html=True)
        _badge(light)
    st.caption(f"台账 {m['days']} 个交易日（影子实盘·确定性引擎；"
               "看板只读，绝不写库、绝不下单）")
    _refresh_button()

    # —— 次级指标（竖屏 2 列）——
    r1, r2 = st.columns(2), st.columns(2)
    r1[0].metric("累计净值变化", f"{m['cum']:+.2%}")
    r2[0].metric("最大回撤", f"{m['mdd']:.2%}")
    r1[1].metric("年化夏普", f"{m['sharpe']:.3f}")
    r2[1].metric("Alpha合计 vs 沪深300", f"{m['alpha_sum']:+.2f}pp")
    if (last.get("notes") or "").strip():
        st.info(f"最新备注：{last['notes']}")

    st.divider()
    st.subheader("净值曲线 · 影子 vs 沪深300（起点=1）")
    if pd is not None:
        st.line_chart(normalize(eod), height=330, use_container_width=True)
    else:
        st.table(normalize(eod))

    st.divider()
    st.subheader(f"最近交易流水（最多 {len(fills)} 笔 · 按 id 倒序）")
    if pd is not None:
        try:
            st.dataframe(fills_frame(fills), hide_index=True,
                         use_container_width=True)
        except TypeError:  # 老版 streamlit 无 hide_index → 退回表格
            st.table(fills_frame(fills))
    else:
        st.json(fills[:40])

    st.caption("数据链路：本地确定性引擎 → sync_to_cloud 增量 push → "
               "云端 Supabase → 本看板（只读镜像，一切数字可溯源到 "
               "scripts/fetch_data.py 拉取的行情 CSV）。（AGENTS.md 铁律）")


# @st.cache_data 需在 streamlit 可用时才定义（本地 st=None，模块级装饰会炸）
def _cached_fetch(url: str, key: str, table: str, order: str,
                  limit: int) -> list[dict]:
    """90s 时间窗缓存（云端 key 只作缓存签名不落盘）；本地无 st 直连。"""
    if st is None:
        return fetch_rows(table, url, key, limit=limit, order=order)

    @st.cache_data(ttl=90, show_spinner="拉取云端台账…")
    def _inner(url_: str, key_: str, table_: str, order_: str,
               limit_: int) -> list[dict]:
        return fetch_rows(table_, url_, key_, limit=limit_, order=order_)
    return _inner(url, key, table, order, limit)


def fetch_test():
    """本地自检：配置→拉数→打印指标（不需要 streamlit）。"""
    url, key, prefix = config_from_env()
    if not url or not key:
        print("[cloud] 未配置 SUPABASE_URL/KEY（本地不受影响）；"
              "参见 README_DEPLOY.md")
        return 0
    eod = fetch_rows(f"{prefix}shadow_eod", url, key)
    fills = fetch_rows(f"{prefix}shadow_fills", url, key,
                       order="id.desc", limit=60)
    m = compute_metrics(eod)
    light = light_state(max(eod, key=lambda r: r.get("trade_date") or ""))
    print(f"[cloud] eod={len(eod)} rows, fills={len(fills)} rows；"
          f"nav=¥{m['nav']:,.0f} pnl={m['pnl']:+,.0f} "
          f"cum={m['cum']:+.2%} mdd={m['mdd']:.2%}"
          f" 灯={light['emoji']}{light['label']}")
    return 0


def selftest() -> int:
    """本地纯函数自检（不需要 streamlit/pandas/网络）。"""
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("✅" if cond else "❌"), name, extra)
        ok = ok and cond

    # secrets 兼容矩阵（_pick：小写/大写/平铺）
    chk("C1 secrets 小写键名", _pick(
        {"url": "https://x.supabase.co/", "key": "k1"},
        _URL_NAMES) == "https://x.supabase.co/")
    chk("C2 secrets 大写键名", _pick(
        {"url": "", "SUPABASE_URL": "https://y.supabase.co"},
        _URL_NAMES) == "https://y.supabase.co")
    chk("C3 候选序（url 优先于 SUPABASE_URL）", _pick(
        {"url": "https://a", "SUPABASE_URL": "https://b",
         "key": "k"}, _URL_NAMES) == "https://a")
    chk("C4 全空返回空串", _pick({"url": "", "key": ""},
                                  _URL_NAMES) == "")
    chk("C5 缺键不炸", _pick({}, _KEY_NAMES) == "")

    # 红绿灯：熔断/防守/急跌/正常 四态 + 优先级
    chk("L1 熔断最高优先",
        light_state({"lock_remaining": "2", "defense": "1",
                     "day_ret": "-0.01"})["key"] == "red")
    chk("L2 防守次之",
        light_state({"lock_remaining": "0", "defense": "1",
                     "day_ret": "-0.01"})["key"] == "amber")
    chk("L3 急跌预警 -2%",
        light_state({"lock_remaining": "0", "defense": "0",
                     "day_ret": "-0.021"})["key"] == "amber")
    chk("L4 缓跌不预警",
        light_state({"lock_remaining": "0", "defense": "0",
                     "day_ret": "-0.015"})["key"] == "green")
    chk("L5 空字段兜底绿灯", light_state({})["key"] == "green")

    # 指标纯函数（V11b 同族，兼容缺字段行）
    m = compute_metrics([
        {"trade_date": "2026-10-18", "nav": 100, "alpha": 0.001,
         "bench_index_close": 4000},
        {"trade_date": "2026-10-19", "nav": 102, "alpha": 0.001,
         "bench_index_close": 4030},
        {"trade_date": "2026-10-20", "nav": 101, "alpha": -0.0002,
         "nav_prev": "102", "bench_index_close": 3990}])
    chk("M1 cum/mdd/days", abs(m["cum"] - 0.01) < 1e-9 and m["mdd"] > 0
        and m["days"] == 3, f"cum={m['cum']:.3f}")
    chk("M2 今日盈亏=nav-nav_prev", abs(m["pnl"] - (-1)) < 1e-9
        and abs(m["day_ret"] - (-1 / 102)) < 1e-9, f"pnl={m['pnl']}")
    chk("M3 缺nav_prev单行→首日兜底100万",
        abs(compute_metrics(
            [{"trade_date": "d1", "nav": 998000}])["nav_prev"]
            - INITIAL_NAV) < 1e-9)
    chk("M4 乱序输入也正确排序", compute_metrics([
        {"trade_date": "d2", "nav": 102, "bench_index_close": 4030},
        {"trade_date": "d1", "nav": 100, "bench_index_close": 4000},
        {"trade_date": "d3", "nav": 101, "nav_prev": 102,
         "bench_index_close": 3990}])["pnl"] == -1)

    # 归一化曲线 / 流水表
    rows = normalize([{"trade_date": "d", "nav": 100,
                       "bench_index_close": 4000},
                      {"trade_date": "d2", "nav": 101,
                       "bench_index_close": 4080}])
    recs = records(rows)
    chk("N1 归一化曲线", abs(recs[-1]["影子净值"] - 1.01) < 1e-9
        and abs(recs[-1]["沪深300"] - 1.02) < 1e-9)

    ff = records(fills_frame([
        {"id": 1, "ts": "2026-10-20 09:30:01", "trade_date": "2026-10-20",
         "leg": "MOM", "code": "510300", "side": "BUY", "shares": 100,
         "price": 3.905, "fee": 13.44, "reason": "影子:信号", "limit_blocked": 0},
        {"id": 2, "ts": "2026-10-20 09:30:02", "trade_date": "2026-10-20",
         "leg": "MR", "code": "159915", "side": "SELL", "shares": 200,
         "price": 2.010, "fee": 8.2, "reason": "影子:信号", "limit_blocked": 1}]))
    chk("F1 流水倒序(id大在前)", ff[0]["代码"] == "159915")
    chk("F2 涨停拒买标旗", "涨停拒买" in ff[0]["事由"]
        and "涨停拒买" not in ff[1]["事由"])
    chk("F3 手续费/价格式", ff[1]["成交价(¥)"] == "3.905"
        and ff[1]["手续费(¥)"] == "13.44")
    print("selftest", "→ 全绿" if ok else "→ 有失败")
    return 0 if ok else 1


def records(df):
    if pd is not None and hasattr(df, "to_dict"):
        return df.to_dict("records")
    return df


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-test", action="store_true",
                    help="云端连通+读数自检（无需 streamlit）")
    ap.add_argument("--selftest", action="store_true",
                    help="本地纯函数自检（无网络/无 streamlit）")
    a = ap.parse_args()
    if a.fetch_test:
        sys.exit(fetch_test())
    if a.selftest:
        sys.exit(selftest())
    if st is None:
        print("本机未安装 streamlit（云端才运行 UI）。")
        print("本地自检：python3 scripts/dashboard_cloud.py --selftest"
              " / --fetch-test")
        sys.exit(0)
    run_app()
