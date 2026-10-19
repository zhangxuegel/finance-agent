# -*- coding: utf-8 -*-
"""factor_mining.py —— 量化因子计算与 LLM 辅助打分（T-Fin03 模块 2）

定位（红线）：本模块只做**辅助调权**，绝不直接产生买卖指令。
LLM 的市场信心得分经过 Python 侧上下限截断（等价 np.clip(score,0,100)，
Termux 无 numpy 依赖，用 min/max 同口径实现），任何异常自动降级，系统不崩溃。

因子（全部确定性计算，来源=行情 CSV）：
  动量因子   momentum20 = close[-1]/close[-21] - 1
  波动率因子 atr14      = Wilder ATR(14)，atr_pct = atr/close
  估值因子   valuation_q = 基准收盘在近 250 日中的分位（0~1）
  ---- 估值因子替代口径声明（台账 factor.valuation_window）----
  ETF 无市盈率台账，为禁用幻觉 PE，采用"价格分位"替代估值热度的确定性代理。

LLM 打分契约：
  · Prompt（任务书原文）："你是量化因子分析师。基于给定的因子数据和新闻，
    输出一个 [0, 100] 的市场信心得分。若得分 > 80，建议调高策略 A 权重；
    若 < 20，建议调高策略 C 权重。只输出结构化 JSON，严禁回复任何解释性废话。"
  · 期望输出 JSON：{"score": 0~100}
  · 格式错误/无法解析/缺 score → 降级 50 分（hard-coded 降级规则）
  · 数值超范围 → clip 到 [0,100]（不丢弃信号）
  · API 失败/无 KEY → 确定性规则得分兜底（source: rule）
"""
import json
import math
import os
import re
import sys
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import fetch_data
import regime_filter

FACTOR_SNAPSHOT = os.path.join(ROOT, "data", "factors.json")
FACTOR_LOG = os.path.join(ROOT, "data", "factor_log.jsonl")


# ---------------------------------------------------------------------------
# 因子计算（Python 侧，确定性）
def atr_series(klines: list[dict], period: int = 14) -> list[float | None]:
    """Wilder ATR 序列；不足 period+1 根为 None。"""
    n = len(klines)
    if n < period + 1:
        return [None] * n
    trs = []
    for i in range(1, n):
        h, l = klines[i]["high"], klines[i]["low"]
        pc = klines[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    out: list[float | None] = [None] * n
    atr = sum(trs[:period]) / period
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i - 1]) / period
        out[i] = atr
    return out


def compute_factors(asof: str | None = None, params: dict | None = None) -> dict:
    p = params or fetch_data.load_params()
    bench_code = p["benchmark"]["code"]
    kl = [k for k in fetch_data.load_csv(bench_code)
          if asof is None or k["date"] <= asof]
    closes = [k["close"] for k in kl]
    w_mom = p["factor"]["momentum_window"]
    w_atr = p["factor"]["atr_period"]
    w_val = p["factor"]["valuation_window"]

    momentum20 = closes[-1] / closes[-1 - w_mom] - 1 if len(closes) > w_mom else None
    atrs = atr_series(kl, w_atr)
    atr14 = atrs[-1]
    atr_pct = atr14 / closes[-1] if atr14 else None

    _clip_win = closes[-w_val:]
    if len(_clip_win) >= 20:
        q = sum(1 for v in _clip_win if v <= closes[-1]) / len(_clip_win)
    else:
        q = None

    light = regime_filter.traffic_light(closes)
    return {"date": kl[-1]["date"], "momentum20": momentum20, "atr14": atr14,
            "atr_pct": atr_pct, "valuation_q": q,
            "light": light["state"], "light_label": light["light"],
            "close": closes[-1], "rows": len(kl)}


# ---------------------------------------------------------------------------
# 新闻（新浪财经快讯，免 Token；抓取失败回退空列表，不崩溃）
def fetch_news(limit: int = 12) -> tuple[list[str], str]:
    import requests
    try:
        r = requests.get(
            "https://zhibo.sina.com.cn/api/zhibo/feed",
            params={"page": 1, "page_size": limit, "zhibo_id": 152, "tag_id": 0},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        lst = ((r.json() or {}).get("result", {}).get("data", {})
               .get("feed", {}) or {}).get("list", []) or []
        titles = [str(it.get("rich_text", "")).strip()[:60]
                  for it in lst[:limit] if it.get("rich_text")]
        return titles, "sina-zhibo(实时)"
    except Exception as e:  # noqa: BLE001
        return [], f"无新闻源（{type(e).__name__}），LLM 仅依据因子"


# 确定性规则兜底（LLM 缺席时）；等效 np.clip 纪律同样适用
def _clip(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)          # ≈ np.clip（无 numpy 依赖实现）


def rule_score(f: dict) -> int:
    s = 50.0
    if f.get("momentum20") is not None:
        s += _clip(f["momentum20"] / 0.10, -1, 1) * 25
    s += {"green": 10, "yellow": -5, "red": -20}[f.get("light", "yellow")]
    if f.get("valuation_q") is not None:
        s += (f["valuation_q"] - 0.5) * 10
    return int(round(_clip(s, 0, 100)))


# ---------------------------------------------------------------------------
# LLM 打分（GLM OpenAI 兼容；三层防线：解析→范围→候选兜底）
PROMPT = ("你是量化因子分析师。基于给定的因子数据和新闻，输出一个 [0, 100] 的"
          "市场信心得分。若得分 > 80，建议调高策略 A 权重；若 < 20，"
          "建议调高策略 C 权重。只输出结构化 JSON，严禁回复任何解释性废话。")


def llm_score(factors: dict, news: list[str], params: dict | None = None,
              resp=None) -> dict:
    """返回 {score, source, raw, tilt}；所有异常路径都安全降级。

    resp：外部注入的模拟响应（单测用），None 时真实请求。
    """
    p = params or fetch_data.load_params()
    key_env = p["llm"]["api_key_env"]
    payload = {"factors": {k: factors.get(k) for k in
                           ("date", "momentum20", "atr_pct", "valuation_q",
                            "light")},
               "news": [t for t in news[:p["factor"]["news_max"]]]}
    if resp is None:
        key = os.environ.get(key_env)
        if not key:
            return {"score": rule_score(factors), "source": "rule(LLM未启用兜底)",
                    "raw": None, "tilt": _tilt_of(None)}
        import requests as _rq
        url = p["llm"]["base_url"].rstrip("/") + "/chat/completions"
        try:
            r = _rq.post(url, headers={"Authorization": f"Bearer {key}"},
                         json={"model": p["llm"]["model"],
                               "messages": [
                                   {"role": "system", "content": PROMPT},
                                   {"role": "user",
                                    "content": json.dumps(payload, ensure_ascii=False)}]},
                         timeout=30)
            r.raise_for_status()
            resp = r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            return {"score": rule_score(factors),
                    "source": f"rule(API失败兜底: {type(e).__name__})",
                    "raw": None, "tilt": _tilt_of(None)}
    return _parse_score(resp, factors)


def _tilt_of(score: int | None) -> str:
    if score is None:
        return "none"
    if score > 80:
        return "A"
    if score < 20:
        return "C"
    return "none"


def _parse_score(resp: str, factors: dict) -> dict:
    """解析 LLM 输出：JSON 失败/缺 score → 50；数值出现 → clip（红线 1）。"""
    raw = None
    try:
        txt = (resp or "").strip()
        txt = re.sub(r"^```(json)?|```$", "", txt, flags=re.M).strip()
        data = json.loads(txt)
        raw = data["score"] if isinstance(data, dict) and "score" in data \
            else (data if isinstance(data, (int, float)) else None)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) \
                or math.isnan(raw):
            raise ValueError("score 非数值")
        score = int(round(_clip(float(raw), 0, 100)))      # 硬编码校验②：范围截断
        return {"score": score, "source": "llm clipped到[0,100]" if
                float(raw) != score else "llm", "raw": raw,
                "tilt": _tilt_of(score)}
    except Exception:  # noqa: BLE001 —— 格式错误：硬编码降级 50（红线兜底）
        return {"score": 50, "source": "degraded(格式错误→50)", "raw": raw,
                "tilt": "none"}


# ---------------------------------------------------------------------------
def weekly_score(asof: str | None = None, allow_llm: bool = False) -> dict:
    """供 ensemble_engine 调用的周度打分入口（确定性优先）。"""
    f = compute_factors(asof=asof)
    if allow_llm and asof is None:          # 实盘今日：可接真实新闻+GLM
        news, news_note = fetch_news()
    else:                                    # 回放/历史：新闻不可回溯 → 只用因子
        news, news_note = [], "回放模式新闻不可回溯（时间锁）"
    if allow_llm and asof is None:
        out = llm_score(f, news)
    else:
        out = {"score": rule_score(f),
               "source": f"rule(回放确定性; {news_note})",
               "raw": None, "tilt": _tilt_of(None)}
    snap = {**f, **out, "news_count": len(news), "generated_at":
            str(date.today())}
    os.makedirs(os.path.dirname(FACTOR_SNAPSHOT), exist_ok=True)
    with open(FACTOR_SNAPSHOT, "w", encoding="utf-8") as fx:
        json.dump(snap, fx, ensure_ascii=False, indent=1, default=str)
    with open(FACTOR_LOG, "a", encoding="utf-8") as fx:
        fx.write(json.dumps({k: snap.get(k) for k in
                             ("date", "score", "source", "light",
                              "momentum20", "valuation_q")} ,
                            ensure_ascii=False) + "\n")
    out["factors"] = {k: f.get(k) for k in
                      ("date", "momentum20", "atr14", "atr_pct",
                       "valuation_q", "light")}
    out["news_count"] = len(news)
    return out


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else ""
    if a == "--test":
        # 硬编码校验矩阵：格式错误→50；超范围→clip；正常→原样
        f = {"momentum20": 0.05, "light": "green", "valuation_q": 0.7}
        cases = [('{"score": 88.9}', 89), ('{"score": 250}', 100),
                 ('{"score": -30}', 0), ('"废话 not json"', 50),
                 ('{"no_key": true}', 50), ('[{"score": 42}]', 50),
                 ('42', 42)]
        for payload, want in cases:
            got = llm_score(f, [], resp=payload)["score"]
            print(("✅" if got == want else "❌"), payload[:24], "→", got,
                  f"(expect {want})")
        assert all(llm_score(f, [], resp=p)["score"] == want
                   for p, want in cases), "硬编码校验失败"
        print("[factor --test] 降级矩阵全部通过")
        sys.exit(0)
    out = weekly_score(allow_llm="--llm" in sys.argv)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
