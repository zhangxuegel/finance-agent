# -*- coding: utf-8 -*-
"""dashboard_cloud.py —— 云端原生 Streamlit 看板（T-Fin07 模块 2）

**云端环境专用**：数据源不读本地 SQLite——一切从云端库拉取。
本脚本被 Streamlit Cloud 运行（requirements.txt 见仓库根目录）；
本地仅作开发/自检（streamlit lazy-import，导入无害）。

配置来源（三重兜底，同 README_DEPLOY.md）：
  1) Streamlit Cloud: .streamlit/secrets.toml 的 [supabase] url/key
  2) 环境变量 SUPABASE_URL / SUPABASE_KEY
  3) 环境变量 SUPABASE_TABLES（可选前缀，默认 sb_）

UI（任务书三视图）：
  · 指标卡：最新净值 / 累计净值变化 / 最大回撤 / 年化夏普 / Alpha 合计
  · 净值折线图：影子 vs 沪深300（归一化）——st.line_chart
  · 最近交易流水表：最近 20 笔（st.dataframe）

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


def config_from_env() -> tuple[str, str, str]:
    """(url, key, prefix)。secrets.toml → 环境变量 双兜底。"""
    url = key = ""
    if st is not None:
        try:
            url = st.secrets["supabase"]["url"].rstrip("/")
            key = st.secrets["supabase"]["key"]
        except Exception:  # noqa: BLE001 —— secrets 未配置 → 走环境变量
            pass
    url = url or os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = key or os.environ.get("SUPABASE_KEY", "")
    prefix = os.environ.get("SUPABASE_TABLES", "sb_")
    return url, key, prefix


def _session(url: str, key: str):
    import requests
    s = requests.Session()
    s.headers.update({"apikey": key, "Authorization": f"Bearer {key}",
                      "User-Agent": "finance-agent-cloud-dashboard/1.0"})
    return s, url


def fetch_rows(table: str, url: str, key: str, limit: int = 1000) -> list[dict]:
    s, base = _session(url, key)
    import requests
    r = s.get(f"{base}/rest/v1/{table}",
              params={"select": "*", "order": "trade_date.asc",
                      "limit": str(limit)},
              timeout=(5, 20))
    if r.status_code != 200:
        raise RuntimeError(f"云端读取 {table} 失败 HTTP {r.status_code}")
    return r.json()


def compute_metrics(eod: list[dict]) -> dict:
    """纯函数（本地可单测）：eod 行 → 指标卡数字。"""
    navs = [float(r["nav"]) for r in eod]
    if not navs:
        return {"nav": 0.0, "cum": 0.0, "mdd": 0.0, "sharpe": 0.0,
                "alpha_sum": 0.0, "days": 0}
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
    return {"nav": navs[-1], "cum": cum, "mdd": mdd, "sharpe": sharpe,
            "alpha_sum": sum(float(r.get("alpha") or 0) for r in eod),
            "days": len(navs)}


def normalize(curve: list[dict]) -> pd.DataFrame | list[dict]:
    """影子 / 沪深300 归一化到起点=1（独立起始锚，差值=执行差异）。"""
    if not curve:
        return pd.DataFrame() if pd else []
    b0 = float(curve[0]["nav"])
    x0 = float(curve[0]["bench_index_close"] or curve[0]["nav"])
    rows = [{"date": r["trade_date"],
             "影子净值": float(r["nav"]) / b0 if b0 else 0,
             "沪深300": float(r["bench_index_close"] or 0) / x0 if x0 else 0}
            for r in curve]
    return pd.DataFrame(rows) if pd else rows


def run_app():
    """Streamlit 页面主体（云端入口）。"""
    st.set_page_config(page_title="影子实盘云端看板",
                       page_icon="🛰️", layout="wide")
    url, key, prefix = config_from_env()
    if not url or not key:
        st.error("未配置云端数据库（secrets.toml 或环境变量 SUPABASE_URL/"
                 "SUPABASE_KEY）——见主仓库 README_DEPLOY.md 第 2 步。")
        st.stop()
    try:
        eod = fetch_rows(f"{prefix}shadow_eod", url, key)
        fills = fetch_rows(f"{prefix}shadow_fills", url, key)
    except Exception as e:  # noqa: BLE001 —— 云端故障给用户可读指引
        st.error(f"云端读取失败：{e}")
        st.stop()
    m = compute_metrics(eod)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("🔥 今日净值", f"¥{m['nav']:,.0f}")
    c2.metric("累计净值变化", f"{m['cum']:+.2%}")
    c3.metric("最大回撤", f"{m['mdd']:.2%}")
    c4.metric("年化夏普", f"{m['sharpe']:.3f}")
    c5.metric("Alpha 合计(vs 沪深300)", f"{m['alpha_sum']:+.2f}pp")
    st.caption(f"台账 {m['days']} 个交易日（影子实盘·确定性引擎，无实盘）")
    st.subheader("净值曲线 · 影子 vs 沪深300")
    if pd is not None:
        st.line_chart(normalize(eod), height=300)
    else:
        st.table(normalize(eod))
    st.subheader("最近交易流水（20 笔）")
    if pd is not None:
        st.dataframe(pd.DataFrame(fills[-20:]).iloc[::-1], use_container_width=True)
    else:
        st.json(fills[-20:])
    st.caption("数据链路：本地确定性引擎 → sync_to_cloud 增量 push → 云端库 → 本看板"
               "；看板只读，绝不写库、绝不下单（AGENTS.md 铁律）。")


def fetch_test():
    """本地自检：配置→拉数→打印指标（不需要 streamlit）。"""
    url, key, prefix = config_from_env()
    if not url or not key:
        print("[cloud] 未配置 SUPABASE_URL/KEY（本地不受影响）；"
              "参见 README_DEPLOY.md")
        return 0
    eod = fetch_rows(f"{prefix}shadow_eod", url, key)
    fills = fetch_rows(f"{prefix}shadow_fills", url, key)
    m = compute_metrics(eod)
    print(f"[cloud] eod={len(eod)} rows, fills={len(fills)} rows；"
          f"nav=¥{m['nav']:,.0f} cum={m['cum']:+.2%} mdd={m['mdd']:.2%}")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-test", action="store_true",
                    help="云端连通+读数自检（无需 streamlit）")
    a = ap.parse_args()
    if a.fetch_test:
        sys.exit(fetch_test())
    if st is None:
        print("本机未安装 streamlit（云端才运行 UI）。")
        print("本地自检：python3 scripts/dashboard_cloud.py --fetch-test")
        sys.exit(0)
    run_app()
