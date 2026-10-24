# -*- coding: utf-8 -*-
"""fake_supabase.py —— 沙盒演练专用 PostgREST 子集模拟器（仅演练，零真实触碰）

支持 cloud_gate.py 依赖的最小语义：
  GET  /rest/v1/<table>?select=*&order=c1.asc.c2.asc&trade_date=gte.X&offset&limit
  POST /rest/v1/<table>?on_conflict=trade_date...   # merge-duplicates REPLACE 语义
状态存方 <state.json>（演练前由驱动脚本按场景播种）。
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

STATE_PATH = sys.argv[1] if len(sys.argv) > 1 else "cloud_state.json"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8991
PK = {"sb_shadow_eod": "trade_date", "sb_shadow_fills": "id"}


def _load():
    return json.load(open(STATE_PATH, encoding="utf-8"))


def _save(state):
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def _num(v):
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return v


def _canon(row):
    return {k: _num(v) for k, v in row.items()}


def _sorted(rows, order: str):
    toks = [t for t in order.split(".") if t]
    keys = []
    for i in range(0, len(toks) - 1, 2):
        col, direction = toks[i], toks[i + 1]
        keys.append((lambda r, c=col, d=direction: (
            (_num(r.get(c)) if isinstance(_num(r.get(c)), (int, float)) else str(r.get(c))) * (-1 if d == "desc" else 1),
        )))
    for k in reversed(keys):
        rows = sorted(rows, key=k)
    return rows


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):      # 静默
        return

    def do_GET(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if parts[0] != "rest" or len(parts) < 3:
            self._code(404, [])
            return
        table = parts[2]
        q = parse_qs(u.query)
        rows = [dict(r) for r in _load().get(table, [])]
        if "trade_date" in q:
            cond = q["trade_date"][0]          # 形如 gte.2026-10-21
            _, op, val = cond.split(".") if cond.count(".") >= 2 else (None, "", "")
            pre, _, v = cond.partition(".")
            val = v
            op = pre if pre != "trade_date" else "eq"
            rows = [r for r in rows
                    if (r["trade_date"] >= val if op == "gte" else
                        r["trade_date"] == val if op == "eq" else True)]
        rows = _sorted(rows, (q.get("order", ["trade_date.asc"])[0]))
        off = int(q.get("offset", ["0"])[0])
        lim = int(q["limit"][0]) if "limit" in q else len(rows)
        self._code(200, rows[off:off + lim])

    def do_POST(self):
        u = urlparse(self.path)
        table = u.path.strip("/").split("/")[2]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))
                                         or b"0") or b"[]")
        state = _load()
        bucket = state.setdefault(table, [])
        pk = PK.get(table, "trade_date")
        for row in body:
            row = _canon(row)
            idx = next((i for i, r in enumerate(bucket)
                        if r.get(pk) == row.get(pk)), None)
            if idx is None:
                bucket.append(row)
            else:
                bucket[idx] = row            # merge-duplicates ⇒ REPLACE 语义
        _save(state)
        self._code(200, body)

    def _code(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
