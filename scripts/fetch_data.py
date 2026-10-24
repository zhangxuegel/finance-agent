# -*- coding: utf-8 -*-
"""fetch_data.py —— 每日行情抓取模块（Task 1 交付）

数据来源（免 Token 公开行情接口，按顺序回退）：
  1) 东方财富 push2his K 线接口（A股/ETF/QDII/指数 通用）
  2) 腾讯行情 web.ifzq.gtimg.cn K 线接口（备用）

铁律：本脚本落盘的 CSV 是全项目唯一认可的行情数据来源，
其他模块只允许 read_csv，不允许自行联网或造数字。

用法：
  python3 scripts/fetch_data.py            # 抓取 watchlist 全部标的，默认近 3 年
  python3 scripts/fetch_data.py 510300     # 只抓某标的
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DATA_DIR = os.path.join(ROOT, "data")
INDEX_DIR = os.path.join(ROOT, "index")

HEADERS = {"User-Agent": "Mozilla/5.0 (Android) AppleWebKit/537.36 (KHTML, Gecko)"}
DEFAULT_YEARS = 3
NATURAL_PER_YEAR = 365


# ---------------------------------------------------------------------------
# 网络请求（带重试，Termux 移动网络下偶发连接重置）
def _get(url: str, params: dict = None, retries: int = 3) -> requests.Response:
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0 + 1.5 * i)
    raise last_err


# ---------------------------------------------------------------------------
# 东方财富 K 线（主源，前复权日线）
def _klines_east(secid: str, natural_days: int) -> list[dict]:
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid, "klt": "101", "fqt": "1",
        "lmt": str(natural_days + 15), "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    try:
        data = (_get(url, params).json() or {}).get("data") or {}
        klines = data.get("klines") or []
    except Exception:  # noqa: BLE001
        klines = []

    out, prev_close = [], None
    for line in klines:
        f = line.split(",")
        close = float(f[2])
        out.append({
            "date": f[0], "open": float(f[1]), "close": close,
            "high": float(f[3]), "low": float(f[4]),
            "volume": int(float(f[5])), "amount": float(f[6]),
        })
        prev_close = close
    return out


# ---------------------------------------------------------------------------
# 腾讯 K 线（备用源）
def _tencent_symbol(secid: str) -> str | None:
    try:
        market, code = secid.split(".", 1)
    except ValueError:
        return None
    if market in ("0", "1") and len(code) == 6:
        return ("sh" if market == "1" else "sz") + code
    return None


def _klines_tencent(secid: str, natural_days: int) -> list[dict]:
    symbol = _tencent_symbol(secid)
    if not symbol:
        return []
    url = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
    try:
        r = _get(url, {"param": f"{symbol},day,,,{natural_days + 20}"})
        node = ((r.json() or {}).get("data") or {}).get(symbol) or {}
        rows = node.get("day") or node.get("qfqday") or []
    except Exception:  # noqa: BLE001
        return []

    return [{
        "date": f[0], "open": float(f[1]), "close": float(f[2]),
        "high": float(f[3]), "low": float(f[4]),
        "volume": int(float(f[5])), "amount": 0.0,
    } for f in rows if len(f) >= 6]


# ---------------------------------------------------------------------------
# 对外接口
def fetch_klines(secid: str, natural_days: int = DEFAULT_YEARS * NATURAL_PER_YEAR,
                 start_date: str | None = None) -> list[dict]:
    """抓日线并按日期正序返回。两个数据源都失败则抛 RuntimeError。

    start_date（T-Fin33 登记 series.from）：引擎信号/撮合窗口要求台账数据
    与孕育期逐根一致——给定时用该交易日精确截断（含该日），不再按 自然日 回退。"""
    try:
        data = _klines_east(secid, natural_days)
    except Exception:  # noqa: BLE001 —— 东财限流/断连 → 降级腾讯
        data = []
    if len(data) < 30:
        data = _klines_tencent(secid, natural_days)
    cutoff = start_date or (datetime.now() - timedelta(days=natural_days)
                            ).strftime("%Y-%m-%d")
    data = [k for k in data if k["date"] >= cutoff]
    if len(data) < 30:
        raise RuntimeError(f"行情抓取失败或数据过少: {secid}（{len(data)} 条）")
    data.sort(key=lambda k: k["date"])
    return data


def load_watchlist() -> list[dict]:
    with open(os.path.join(INDEX_DIR, "watchlist.yaml"), encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    assert isinstance(wl, list) and wl, "watchlist.yaml 必须是列表"
    for item in wl:
        for key in ("code", "name", "secid", "kind"):
            assert key in item, f"watchlist 缺少字段 {key}: {item}"
    return wl


def validate(klines: list[dict], code: str, natural_days: int) -> dict:
    """校验：日历覆盖 ≥ 95%、相邻交易日缺口 ≤ 12 个自然日、字段无空值。"""
    dates = [k["date"] for k in klines]
    for k in klines:
        for field in ("open", "close", "high", "low", "volume"):
            assert isinstance(k[field], (int, float)), f"{code} {k['date']} {field} 为空"

    first, last = datetime.strptime(dates[0], "%Y-%m-%d"), datetime.strptime(dates[-1], "%Y-%m-%d")
    cover_days = (last - first).days
    cover = cover_days / natural_days

    gaps = [(dates[i + 1], dates[i]) for i in range(len(dates) - 1)
            if (datetime.strptime(dates[i + 1], "%Y-%m-%d")
                - datetime.strptime(dates[i], "%Y-%m-%d")).days > 12]
    # 非交易表：周末不算缺口，直接用缺口列表人工核对（节假日 > 12 天即报警）
    assert cover >= 0.95, f"{code} 覆盖率不足: {cover:.0%}（{first:%F} ~ {last:%F}）"
    assert not gaps, f"{code} 存在 >12 自然日的缺口: {gaps}"
    return {"rows": len(klines), "first": dates[0], "last": dates[-1],
            "coverage": f"{cover:.1%}", "max_gap_checked": 12}


def save_csv(klines: list[dict], code: str, data_dir: str | None = None) -> str:
    path = os.path.join(data_dir or DATA_DIR, f"{code}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "close", "high", "low",
                                          "volume", "amount"])
        w.writeheader()
        w.writerows(klines)
    return path


def load_csv(code: str, data_dir: str | None = None) -> list[dict]:
    """读取台账 CSV；其他模块统一从这里读数据。
    data_dir 可指定独立目录（如 data/crash 黑天鹅长历史，避免污染正式台账）。"""
    path = os.path.join(data_dir or DATA_DIR, f"{code}.csv")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for field in ("open", "close", "high", "low", "amount"):
            r[field] = float(r[field])
        r["volume"] = int(r["volume"])
    return rows


def load_raw_csv(code: str, params: dict | None = None,
                 data_dir: str | None = None) -> list[dict]:
    """T-Fin33（业主拍板 a）：台账成交价的不复权 raw 口径单一事实源。

    raw(d) = qfq(d) + Σ{已登记分红、除息日>d 的每股派现}（params.dividends；
    与 risk_control.audit_fill_time_lock._recode 同一公式）。除息事件须及时
    登记——对"未来补登"具有不变性（补登不减旧）。全链信号与撮合统一在
    raw 序列上进行，与审计口径完全一致。"""
    rows = load_csv(code, data_dir)
    div = (params or load_params()).get("dividends") or {}
    evs = sorted(str(e.get("ex_date")) for e in div.get(code, []))
    if not evs:
        return rows
    out = []
    for k in rows:
        rebase = sum(float(str(div[code][j].get("per_share", 0)))
                     for j in range(len(evs)) if evs[j] > k["date"])
        if rebase:
            k = {**k, "open": k["open"] + rebase,
                 "close": k["close"] + rebase,
                 "high": k["high"] + rebase, "low": k["low"] + rebase}
        out.append(k)
    return out


def fetch_and_save(secid: str, code: str, natural_days: int,
                   data_dir: str | None = None) -> str:
    """抓取并落盘到指定目录（crash_test 用：长历史黑天鹅数据）。"""
    kl = fetch_klines(secid, natural_days)
    info = validate(kl, code, natural_days)
    path = save_csv(kl, code, data_dir)
    print(f"[fetch] {code}: {info['rows']} 行 {info['first']}~{info['last']} → {path}")
    return path


def load_params() -> dict:
    with open(os.path.join(INDEX_DIR, "params.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
def main() -> int:
    codes = sys.argv[1:] or []
    wl = load_watchlist()
    wanted = wl if not codes else [w for w in wl if w["code"] in codes]
    if not wanted:
        print("标的代码不在 watchlist 中:", codes)
        return 2
    series_from = (load_params().get("series") or {}).get("from")   # T-Fin33
    natural_days = DEFAULT_YEARS * NATURAL_PER_YEAR
    if series_from:
        natural_days = max(natural_days,
                           (datetime.now()
                            - datetime.strptime(series_from, "%Y-%m-%d")
                            ).days + 5)
    summary = []
    for w in wanted:
        kl = fetch_klines(w["secid"], natural_days, start_date=series_from)
        info = validate(kl, w["code"], natural_days)
        path = save_csv(kl, w["code"])
        summary.append({**w, **info, "path": path})
        print(f"[fetch] {w['code']} {w['name']}: {info['rows']} 行 "
              f"{info['first']} ~ {info['last']} 覆盖 {info['coverage']} → {path}")
    # 台账运行记录（可追溯）
    log_path = os.path.join(DATA_DIR, "fetch_log.json")
    history = []
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            history = json.load(f)
    history.append({"at": datetime.now().isoformat(timespec="seconds"),
                    "items": summary})
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(history[-30:], f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
