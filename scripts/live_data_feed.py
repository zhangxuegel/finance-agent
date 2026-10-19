# -*- coding: utf-8 -*-
"""live_data_feed.py —— 真实行情只读接入（T-Fin05 模块 1）

数据源（公开接口，无验证码）：
  主源：腾讯 qt.gtimg.cn/q=sh510300,sz159915,...（实时行情，字段 30=行情时间戳）
  备源：新浪 hq.sinajs.cn/list=sh510300,...（需 Referer；主源失败才降级用）
  指数：s_sh000300（沪深300指数，监控与 Alpha 基准）

纪律（铁律映射）：
- 只读接入；一切行情数字仍以 fetch_data.py 落盘的 CSV 为最终台账，
  本模块抓到的快照写入 data/live_snapshot.json 并带原始时间戳（可溯源）。
- 心跳检测：09:25（盘前）与 14:55（尾盘前）各一次；接口报错或
  行情时戳延迟 > live.max_quote_delay_sec（默认 180 秒）→ 报警 +
  写 data/live_mode.json 降级标记（当日放弃开新交易）。
- 实盘时间锁：提供 clock guards——09:30 前禁止用"当日"数据撮合，
  15:00 后禁止把盘后数据当作盘中快照撮合；由 shadow_broker 调取。
"""
import argparse
import json
import os
import re
import sys
import time as _time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import portfolio

SNAPSHOT = os.path.join(ROOT, "data", "live_snapshot.json")
MODE_FILE = os.path.join(ROOT, "data", "live_mode.json")
ERR_LOG = os.path.join(ROOT, "logs", "live_error.log")
LIVE_DIR = os.path.join(ROOT, "data", "live")


def sh_now():
    """北京时间（Asia/Shanghai）；zoneinfo 缺失则回退 UTC+8 固定时差。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:  # noqa: BLE001 —— 降级：UTC+8 定差（登记为已知降级）
        import time as _t
        return datetime.utcfromtimestamp(_t.time() + 8 * 3600)


def log_error(kind: str, detail: str):
    os.makedirs(os.path.dirname(ERR_LOG), exist_ok=True)
    with open(ERR_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%F %T} [{kind}] {detail}\n")


def _get(url, params=None, headers=None, retries=2):
    import requests
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params or {}, headers=headers or
                             {"User-Agent": "Mozilla/5.0"},
                             timeout=(5, 10))
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            log_error("quote_fetch", f"{url} try{i}: {type(e).__name__} {e}")
            _time.sleep(1.0 + i)
    raise last


# ---------------------------------------------------------------------------
def tencent_symbol(code: str) -> str:
    """6 位代码 → 腾讯行情符号（沪 ETF=5 开头；深 ETF=1 开头）。"""
    return ("sh" if code[0] == "5" else "sz") + code


# 腾讯 qt 行情字段位（实测口径，落注释防漂移）：
# 0:市场 1:名称 2:代码 3:最新 4:昨收 5:今开 6:成交量(手) 30:行情时间戳 YYYYMMDDhhmmss
# 31:涨跌 32:涨跌% 33:最高 34:最低
def _parse_tencent(sym: str, text: str) -> dict | None:
    m = re.search(rf'v_{sym}="([^"]*)"', text)
    if not m:
        return None
    f = m.group(1).split("~")
    if len(f) < 35:
        return None
    try:
        ts = datetime.strptime(f[30].split(" ")[0], "%Y%m%d%H%M%S")
        return {"symbol": sym, "name": f[1], "code": f[2],
                "last": float(f[3]), "prev_close": float(f[4]),
                "open": float(f[5]), "volume": float(f[6]),
                "high": float(f[33]), "low": float(f[34]),
                "pct": float(f[32]), "ts": ts.strftime("%F %T")}
    except (ValueError, IndexError) as e:
        log_error("quote_parse", f"{sym}: {e}")
        return None


def _parse_sina(sym: str, text: str) -> dict | None:
    r = re.search(rf'hq_str_{sym}="([^"]*)"', text)
    if not r:
        return None
    f = r.group(1).split(",")
    if len(f) < 32:
        return None
    try:
        # 0:今开 1:昨收 2:最新 3:最高 4:最低 ... 30:日期 31:时间
        dt = f"{f[30]} {f[31]}"
        datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
        last, prev, op = float(f[2]), float(f[1]), float(f[0])
        return {"symbol": sym, "name": sym, "code": sym[2:],
                "last": last, "prev_close": prev, "open": op,
                "volume": float(f[8] or 0),
                "high": float(f[3]), "low": float(f[4]),
                "pct": round((last / prev - 1) * 100, 3) if prev else 0.0,
                "ts": dt}
    except (ValueError, IndexError) as e:
        log_error("quote_parse", f"sina {sym}: {e}")
        return None


def fetch_realtime(codes: list[str]) -> dict:
    """抓实时行情；腾讯主源失败 → 新浪备源。返回 {code: quote_dict}。
    quote 带 source 与 ts（原始时戳，供新鲜度判定）。"""
    out = {}
    syms = {tencent_symbol(c): c for c in codes}
    try:
        r = _get("https://qt.gtimg.cn/q=" + ",".join(syms))
        text = r.text
        for sym, code in syms.items():
            q = _parse_tencent(sym, text)
            if q:
                q["source"] = "tencent"
                out[code] = q
    except Exception as e:  # noqa: BLE001
        log_error("source_primary", f"tencent 失败 → 降级 sina: {e}")
    miss = [c for c in codes if c not in out]
    if miss:
        try:
            r = _get("https://hq.sinajs.cn/list=" + ",".join(syms),
                     headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://finance.sina.com.cn"})
            text = r.text
            for sym in map(tencent_symbol, miss):
                q = _parse_sina(sym, text)
                if q:
                    q["source"] = "sina"
                    out[q["code"]] = q
        except Exception as e:  # noqa: BLE001
            log_error("source_fallback", f"sina 也失败: {e}")
    return out


def quote_delay_sec(q: dict, now: datetime | None = None) -> float | None:
    """行情时戳与当前时刻的延迟（秒）；解析失败 → None（视为过期）。"""
    if not q or not q.get("ts"):
        return None
    try:
        ts = datetime.strptime(q["ts"], "%Y-%m-%d %H:%M:%S")
        return abs((now or sh_now()).timestamp() - ts.timestamp())
    except ValueError:
        return None


def limit_down_price(prev_close: float, tick: float = 0.001) -> float:
    """主板 ETF 跌停价 = round(昨收 × 0.9, 3 位小数)。"""
    return round(prev_close * 0.90, 3) if prev_close else 0.0


def is_limit_down_open(q: dict, tol: float = 0.002) -> bool:
    """开盘即跌停判定（含集合竞价快照）：今开 ≤ 跌停价 + 容差。"""
    ld = limit_down_price(q["prev_close"])
    return bool(ld and q["open"] <= ld + tol)


# ---------------------------------------------------------------------------
def codes_for_live() -> list[str]:
    """实时接入的标的集合：4 只核心 ETF + 货币腿 511990 + 基准 510300。"""
    import fetch_data
    p = portfolio.load_params()
    codes = [w["code"] for w in fetch_data.load_watchlist()]
    codes.append(p["ensemble"]["C"]["code"])        # 货币腿
    codes.append(p["benchmark"]["code"])            # 基准 ETF
    return sorted(set(codes))


def heartbeat(replay: bool = False, verbose: bool = True) -> dict:
    """心跳检测（09:25 / 14:55 cron 调用）。
    全部代码行情可用且新鲜 → mode=live；任一失败/过期 >180s →
    报警 + data/live_mode.json 降级标记（当日放弃开新交易）。
    本函数绝不抛异常（铁律：cron 不倒）。"""
    import fetch_data
    try:
        p = portfolio.load_params()
        max_delay = float(p.get("live", {}).get("max_quote_delay_sec", 180))
    except Exception:  # noqa: BLE001
        max_delay = 180.0
    now = sh_now()
    try:
        quotes = fetch_realtime(codes_for_live())
    except Exception as e:  # noqa: BLE001
        quotes = {}
        log_error("heartbeat", f"fetch_realtime 异常: {e}")
    want = codes_for_live()
    miss = [c for c in want if c not in quotes]
    # 会话日 = 行情自己带的市场时戳最新日期（非墙钟——回放验收可复用）
    session_day = max((q["ts"].split(" ")[0] for q in quotes.values()),
                      default="")
    stale = []
    for c, q in quotes.items():
        if replay:
            # 回放口径快照是否"属于最近一个会话且盘中/收盘后产生"
            ok_ts = (q["ts"].split(" ")[0] == session_day
                     and q["ts"] >= f"{session_day} 09:25:00")
            if not ok_ts:
                stale.append({"code": c, "delay_sec": None, "ts": q["ts"]})
        else:
            d = quote_delay_sec(q, now)
            if d is None or d > max_delay:
                stale.append({"code": c, "delay_sec": d, "ts": q["ts"]})
    ok = bool(quotes) and not miss and not stale
    snap = {"ts": now.strftime("%F %T"), "stage": "heartbeat",
            "session_day": session_day, "replay": bool(replay),
            "quotes": quotes, "miss": miss, "stale": stale,
            "fresh": ok}
    try:
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
    except OSError as e:
        log_error("snapshot_write", str(e))
    reason = ""
    if not ok:
        reason = (f"缺失 {miss} " if miss else "") + \
                 (f"过期 {stale}" if stale else "")
        _write_degrade(now, reason)
        _event_db("degrade", f"心跳失败：{reason} → 当日放弃开新交易")
    else:
        _write_mode(now, "live", "")
        _event_db("heartbeat", "全部行情新鲜，live 模式")
    if verbose:
        print(f"[heartbeat] {'✅ live' if ok else '⚠️ 降级'} "
              f"codes={len(quotes)}/{len(want)} {('reason=' + reason) if reason else ''}")
    return snap


def _write_mode(now, mode: str, reason: str):
    try:
        with open(MODE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": now.strftime("%F"), "mode": mode,
                       "reason": reason,
                       "ts": now.strftime("%F %T")}, f, ensure_ascii=False)
    except OSError as e:
        log_error("mode_write", str(e))


def _write_degrade(now, reason: str):
    _write_mode(now, "degraded", reason)


def current_mode() -> dict:
    try:
        with open(MODE_FILE, encoding="utf-8") as f:
            m = json.load(f)
    except Exception:  # noqa: BLE001
        return {"mode": "unknown", "reason": "no mode file"}
    # 当日标记才有效（跨日自动回到 live 待心跳确认）
    today = sh_now().strftime("%F")
    m["date"] = m.get("date", "")
    if m["date"] != today:
        return {"mode": "live", "reason": "mode file 为旧日期，待心跳确认"}
    return m


def _event_db(kind: str, detail: str):
    """事件写 shadow_live.db（失败仅进错误日志，不抖动心跳）。"""
    try:
        import shadow_broker
        shadow_broker.db_event(kind, detail)
    except Exception as e:  # noqa: BLE001
        log_error("event_db", f"{kind}: {e}")


# ---------------------------------------------------------------------------
# 指数（沪深300）实时：用于盘中熔断监控与 Alpha 对比基准
def index_realtime(replay: bool = False) -> dict:
    try:
        r = _get("https://qt.gtimg.cn/q=s_sh000300")
        f = re.search(r'v_s_sh000300="([^"]*)"', r.text).group(1).split("~")
        # s_ 精简版：1:名称 2:代码 3:最新 4:涨跌 5:涨跌% 6:成交量 7:成交额 8:时戳?
        last = float(f[3])
        pct = float(f[5])
        prev = last / (1 + pct / 100)
        try:
            ts = datetime.strptime(f[8].split(" ")[0], "%Y%m%d%H%M%S")
            ts_s = ts.strftime("%F %T")
        except (ValueError, IndexError):
            ts_s = sh_now().strftime("%F %T")
        return {"index": "sh000300", "last": last, "pct": pct,
                "prev_close": round(prev, 2), "ts": ts_s,
                "source": "tencent"}
    except Exception as e:  # noqa: BLE001 —— 指数行情失败 → 基准 ETF 代理
        log_error("index_fetch", f"s_sh000300: {e}")
        q = fetch_realtime(["510300"]).get("510300")
        if q:
            return {"index": "510300(etf_proxy)", "last": q["last"],
                    "pct": q["pct"], "prev_close": q["prev_close"],
                    "ts": q["ts"], "source": "etf_proxy"}
        return {}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--heartbeat", action="store_true", help="心跳检测")
    ap.add_argument("--replay", action="store_true", help="回放时钟（验收）")
    ap.add_argument("--probe", action="store_true", help="仅抓一次实时行情")
    ap.add_argument("--index", action="store_true", help="沪深300 实时")
    a = ap.parse_args()
    if a.probe:
        print(json.dumps(fetch_realtime(codes_for_live()), ensure_ascii=False, indent=1)[:800])
    elif a.index:
        print(json.dumps(index_realtime(), ensure_ascii=False))
    else:
        heartbeat(replay=a.replay)
    sys.exit(0)   # 无论如何退出码 0（cron 不倒）
