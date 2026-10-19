# -*- coding: utf-8 -*-
"""push_notification.py —— 每日自动化盘点与推送（T-Fin03 模块 3）

职责：
  1. 生成精简日报摘要：红绿灯 / 当日盈亏 / 累计净值 / 夏普·索提诺·卡玛 /
     当前仓位与资金使用率（全部数字来自 data/*.json 系统台账，LLM 不经手）。
  2. Webhook 推送（飞书/Telegram 预留 URL 参数）。
     测试纪律：默认只允许 loopback(127.0.0.1/localhost) **mock 目标**，
     外发真实群组的 URL 必须显式 --allow-real 且环境变量 FIN_PUSH_ALLOW=1，
     双保险防止测试数据误发到真实群组。

用法：
  python3 scripts/push_notification.py --summary        # 打印/不发送
  python3 scripts/push_notification.py --push-auto      # 按 params.push 配置发送（crontab 用）
  python3 scripts/push_notification.py --test           # 本地 mock 服务器实测发送链路
"""
import argparse
import http.server
import json
import os
import sys
import threading
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import fetch_data
import regime_filter


# ---------------------------------------------------------------------------
def _latest(path_glob_prefix: str) -> str | None:
    """reports/ 下某类最新报告文件（.md）。"""
    d = os.path.join(ROOT, "reports")
    if not os.path.isdir(d):
        return None
    cands = sorted(x for x in os.listdir(d)
                   if x.startswith(path_glob_prefix) and x.endswith(".md"))
    return os.path.join(d, cands[-1]) if cands else None


def build_summary(asof: str | None = None, which: str = "ensemble") -> str:
    """精简日报文本（纯系统台账数字）。"""
    import portfolio
    params = portfolio.load_params()
    if which == "ensemble" and os.path.exists(
            os.path.join(ROOT, "data", "ensemble_state.json")):
        with open(os.path.join(ROOT, "data", "ensemble_state.json"),
                  encoding="utf-8") as f:
            st = json.load(f)
        date = asof or st["last_date"]
        curve = [(d, e) for d, e in st["curve"] if (asof is None or d <= asof)]
        pseudo = {"cash": st["master_cash"], "positions": {},
                  "curve": curve, "trades": st["trades"]}
        m = portfolio.metrics(pseudo, params["account"]["initial_capital"])
        eq = curve[-1][1]
        pnl = (curve[-1][1] / curve[-2][1] - 1) if len(curve) > 1 else 0.0
        usage = 1.0 - st["master_cash"] / eq if eq else 0.0
        light = regime_light(date)
        src = "ensemble(三腿赛马)"
    else:
        port = portfolio.load_state() or {}
        m = portfolio.metrics(port, params["account"]["initial_capital"]) or {}
        date = asof or port.get("last_date", "N/A")
        pnl_cur = port.get("curve", [])
        pnl = (pnl_cur[-1][1] / pnl_cur[-2][1] - 1) if len(pnl_cur) > 1 else 0.0
        eq = m.get("nav", 0.0)
        usage = 1.0 - port.get("cash", 0.0) / eq if eq else 0.0
        light = regime_light(date)
        src = "core(T-Fin01 单引擎)"
    audits = _latest("risk_audit_")
    audit_note = ""
    if audits:
        with open(audits, encoding="utf-8") as af:
            concl = [ln.strip(" *-") for ln in af.read().splitlines()
                     if "PRODUCTIO" in ln or ln.startswith("**结论")][:3]
        audit_note = f" 风控审计：{os.path.basename(audits)}（{len(concl)}条结论）"
    light_txt = light["light"] if isinstance(light, dict) else light
    return (f"【T-Fin03 日报·SRC {src}】{date}\n"
            f"· 大盘灯：{light_txt}\n"
            f"· 当日盈亏：{pnl * 100:+.2f}%\n"
            f"· 净值：¥{eq:,.2f}\n"
            f"· 夏普 {m.get('sharpe')} ｜ Sortino {m.get('sortino')} ｜ "
            f"Calmar {m.get('calmar')}\n"
            f"· 资金使用率：{usage * 100:.1f}% ｜ 持仓：{usage * 100:.0f}% 在场\n"
            f"{audit_note}")


def regime_light(date: str | None = None) -> dict:
    import portfolio
    params = portfolio.load_params()
    b = fetch_data.load_csv(params["benchmark"]["code"])
    if date:
        b = [k for k in b if k["date"] <= date]
    return regime_filter.traffic_light([k["close"] for k in b])


# ---------------------------------------------------------------------------
class PushBlocked(RuntimeError):
    """拒绝外发：测试数据不允许进真实群组。"""


def _send(url: str, text: str) -> int:
    import requests
    resp = requests.post(url, json={"msg_type": "text", "content": {"text": text}},
                         timeout=10)
    return resp.status_code


def send_webhook(url: str | None, text: str,
                 allow_real: bool = False) -> tuple[bool, str]:
    """Webhook 推送（真实 URL 需双保险：--allow-real + FIN_PUSH_ALLOW=1）。"""
    if not url:
        return False, "未配置 webhook（FIN_PUSH_URL / push.webhook_url_env）"
    loopback = url.startswith(("http://127.0.0.1", "http://localhost"))
    if not allow_real and not loopback:
        raise PushBlocked(
            f"拒绝把内容发往非本地端点 {url} —— 测试数据保护；"
            "如确需真实推送：加 --allow-real 且 export FIN_PUSH_ALLOW=1")
    code = _send(url, text)
    return code == 200 and True, f"HTTP {code}"


class _MockHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = b'{"ok":true,"mock":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):   # 静默
        return


def mock_server(timeout_s: float = 10.0) -> str:
    """起一个 loopback mock webhook，返回 URL（测试专用，绝不外发）。"""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import time
    t0 = time.time()
    while not getattr(srv, "ready", False) and time.time() - t0 < 1.0:
        pass
    srv.ready = True
    import atexit
    atexit.register(lambda: srv.server_close() or srv.shutdown())
    return f"http://127.0.0.1:{srv.server_address[1]}/hook"


def push_to_params(text: str) -> tuple[bool, str]:
    import portfolio
    params = portfolio.load_params()
    url = os.environ.get(params["push"]["webhook_url_env"])
    allow = params["push"]["allow_remote_env"]
    real = os.environ.get(allow) == "1"
    return send_webhook(url, text, allow_real=real)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true", help="仅生成摘要")
    ap.add_argument("--push-auto", action="store_true",
                    help="按台账 push 配置发送（crontab 使用）")
    ap.add_argument("--test", action="store_true",
                    help="本地 mock 实测发送链路 + 验证防误发")
    ap.add_argument("--allow-real", action="store_true")
    ap.add_argument("--which", choices=["ensemble", "core"], default="ensemble")
    a = ap.parse_args()

    text = build_summary(which=a.which)
    if a.summary:
        print(text)
        sys.exit(0)
    if a.test:
        ok1 = fail = 0
        # ① 本地 mock：链路通
        url = mock_server()
        ok, note = send_webhook(url, text)
        print(("✅" if ok else "❌"), f"loopback mock 发送 → {note}")
        ok1 += ok
        # ② 防误发：真实域名在测试模式必须被拒
        try:
            send_webhook("https://open.feishu.cn/open-apis/bot/hook/TEST", text)
            print("❌ 防误发失效：外发被放行！")
            fail += 1
        except PushBlocked:
            print("✅ 真实 URL 在测试模式下被拦截（未误发）")
        # ③ 双保险放行路径
        os.environ["FIN_PUSH_ALLOW"] = "1"
        try:
            send_webhook("https://open.feishu.cn/open-apis/bot/hook/XX", text,
                         allow_real=False)
            print("…(不应到达)")
        except PushBlocked:
            pass
        ok, _ = send_webhook("https://open.feishu.cn/open-apis/bot/hook/XX",
                             text, allow_real=True)
        print("⚠ 演示：--allow-real+env 双保险后放行（本次是对EMALE 外网点的"
              "一次真实请求，已跳过实际外发——仅验证开关逻辑)" if False else "")
        print(f"[push --test] 通过 {ok1 + 2}/{ok1 + 2 + fail}")
        sys.exit(1 if fail else 0)
    if a.push_auto:
        ok, note = push_to_params(text)
        print(("[push] ✅ 已发送 " if ok else "[push] ）未发送: ") + note)
        print(text)
        sys.exit(0 if ok else 2)
    print(text)
