# -*- coding: utf-8 -*-
"""preflight.py —— T-Fin35 行前数据完整性检查（只读，绝不写任何文件）

业主 2026-10-24 业务规则（对账闸重构）：
  1) 阻断条件：仅当「信息不全」（缺失必需数据文件/基础状态）时停跑；
  2) 信息满足后允许继续，但第一件事必须是强制对账（cloud_gate --gate）；
  3) 对账统一之前不允许执行记账逻辑（daily_report 等）；
  4) 云端与本地使用完全相同的校验与对账标准。

本模块只负责规则 1 的「数据完整性」判定——与对账彻底分离：
  - 任何一项缺失/损坏 → 打印精确缺失清单 → exit 1（停跑，不产生半途状态）
  - 全部满足 → exit 0（放行进入对账闸）

检查项（读侧需要，不含"数据新鲜度"——那是记账推进段的职责）：
  A. index/params.yaml        可解析且含 account/fees/strategy/series/anchor
  B. index/watchlist.yaml     非空列表，每项含 code/name/secid/kind
  C. data/<code>.csv          每个登记标的：存在、≥260行、表头字段齐、数值可解析
  D. data/portfolio.json      合法 JSON：last_date/cash/curve/trades 结构完整
  E. data/risk_state.json     合法 JSON 含 locked
  F. data/shadow_state.json   合法 JSON
  G. data/shadow_live.db      存在且含表 eod/fills（可靠对账前提）

用法：
  python3 scripts/preflight.py            # 检查并打印清单（默认即 --check）
  python3 scripts/preflight.py --check
"""
import csv
import json
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

DATA = os.path.join(ROOT, "data")
INDEX = os.path.join(ROOT, "index")

CSV_COLS = ("date", "open", "close", "high", "low", "volume", "amount")
PARAM_KEYS = ("account", "fees", "strategy", "series", "anchor")


def _check_params(miss: list) -> None:
    path = os.path.join(INDEX, "params.yaml")
    if not os.path.exists(path):
        miss.append("index/params.yaml 不存在")
        return
    try:
        import yaml
        p = yaml.safe_load(open(path, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        miss.append(f"index/params.yaml 解析失败: {e}")
        return
    for k in PARAM_KEYS:
        if k not in (p or {}):
            miss.append(f"index/params.yaml 缺少顶层节: {k}")
    if not (p or {}).get("series", {}).get("from"):
        miss.append("index/params.yaml 缺少 series.from（T-Fin33 绝对项）")


def _check_watchlist(miss: list) -> list:
    path = os.path.join(INDEX, "watchlist.yaml")
    if not os.path.exists(path):
        miss.append("index/watchlist.yaml 不存在")
        return []
    try:
        import yaml
        wl = yaml.safe_load(open(path, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        miss.append(f"index/watchlist.yaml 解析失败: {e}")
        return []
    if not isinstance(wl, list) or not wl:
        miss.append("index/watchlist.yaml 必须是非空列表")
        return []
    for item in wl:
        for key in ("code", "name", "secid", "kind"):
            if key not in item:
                miss.append(f"watchlist 条目缺字段 {key}: {item.get('code')}")
    return wl


def _check_csvs(wl: list, miss: list) -> None:
    for w in wl:
        code = w.get("code", "?")
        path = os.path.join(DATA, f"{code}.csv")
        if not os.path.exists(path):
            miss.append(f"data/{code}.csv 不存在（{w.get('name')}）")
            continue
        try:
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if len(rows) < 260:
                miss.append(f"data/{code}.csv 行数不足（{len(rows)} < 260）")
                continue
            head = rows[0]
            if any(k not in head for k in CSV_COLS):
                miss.append(f"data/{code}.csv 表头缺字段"
                            f"{[k for k in CSV_COLS if k not in head]}")
                continue
            float(head["open"])             # 首行数值可解析性
            int(head["volume"])
            float(rows[-1]["close"])
        except Exception as e:  # noqa: BLE001
            miss.append(f"data/{code}.csv 内容损坏: {e}")


def _check_json(path: str, schema: str, miss: list) -> None:
    name = os.path.relpath(path, ROOT)
    if not os.path.exists(path):
        miss.append(f"{name} 不存在")
        return
    try:
        p = json.load(open(path, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        miss.append(f"{name} 非法 JSON: {e}")
        return
    if schema == "portfolio":
        for k in ("last_date", "cash", "curve", "trades"):
            if k not in p:
                miss.append(f"{name} 缺少键: {k}")
        if not isinstance(p.get("curve"), list) or not p.get("curve"):
            miss.append(f"{name} curve 为空（账本无历史）")
        if not isinstance(p.get("trades"), list):
            miss.append(f"{name} trades 非列表")
    elif schema == "risk":
        if "locked" not in p:
            miss.append(f"{name} 缺少键: locked")


def _check_shadow_db(miss: list) -> None:
    path = os.path.join(DATA, "shadow_live.db")
    if not os.path.exists(path):
        miss.append("data/shadow_live.db 不存在（对账无本地侧载体）")
        return
    try:
        with sqlite3.connect(path) as conn:
            have = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("eod", "fills"):
            if t not in have:
                miss.append(f"data/shadow_live.db 缺少表: {t}")
    except sqlite3.Error as e:
        miss.append(f"data/shadow_live.db 无法打开: {e}")


def check() -> tuple[int, list, int]:
    miss: list = []
    _check_params(miss)
    wl = _check_watchlist(miss)
    _check_csvs(wl, miss)
    _check_json(os.path.join(DATA, "portfolio.json"), "portfolio", miss)
    _check_json(os.path.join(DATA, "risk_state.json"), "risk", miss)
    _check_json(os.path.join(DATA, "shadow_state.json"), "shadow", miss)
    _check_shadow_db(miss)
    return (1 if miss else 0), miss, len(wl)


def main() -> int:
    rc, miss, n_wl = check()
    if rc == 0:
        print(f"[preflight] 信息齐全（watchlist {n_wl} 标的、CSV 行数表头、"
              "账本/风控/影子状态/DB 齐备）→ 放行对账闸")
        return 0
    print("[preflight] ⚠ 信息不全 → 停跑（缺信息不记帐），缺失清单：")
    for m in miss:
        print("  ✗", m)
    print("[preflight] 修复以上清单后重跑；本轮未产生任何写入。")
    return 1


if __name__ == "__main__":
    if "--check" in sys.argv or len(sys.argv) == 1:
        sys.exit(main())
    print(__doc__)
