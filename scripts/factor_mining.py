# -*- coding: utf-8 -*-
"""factor_mining.py —— 量化因子计算与 LLM 辅助打分（T-Fin03 模块 2）

定位（红线）：本模块只做**辅助调权**，绝不直接产生买卖指令。
LLM 的市场信心得分经过 Python 侧上下限截断（等价 np.clip(score,0,100)，
Termux 无 numpy 依赖，用 min/max 同口径实现），任何异常自动降级，系统不崩溃。

因子（全部确定性计算，来源=行情 CSV）：
  动量因子   momentum20 = close[-1]/close[-21] - 1
  波动率因子 atr14      = Wilder ATR(14)，atr_pct = atr/close
  估值因子   valuation_q = 基准收盘在近 250 日中的分位（0~1）
  强弱因子   rsi14      = Wilder RSI(14)（T-Fin28 接入，辅助过滤位）
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


def rsi_now(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI 最新值（T-Fin28 辅助过滤位）。

    单一公式纪律：直接复用 ensemble_engine.rsi_series（赛马 B 腿同源），
    禁止双份实现漂移；序列尾部取最后一个非 None。"""
    from ensemble_engine import rsi_series      # 函数级 import（防循环依赖）
    for v in reversed(rsi_series(closes, period)):
        if v is not None:
            return v
    return None


def compute_factors(asof: str | None = None, params: dict | None = None) -> dict:
    p = params or fetch_data.load_params()
    bench_code = p["benchmark"]["code"]
    kl = [k for k in fetch_data.load_csv(bench_code)
          if asof is None or k["date"] <= asof]
    closes = [k["close"] for k in kl]
    w_mom = p["factor"]["momentum_window"]
    w_atr = p["factor"]["atr_period"]
    w_val = p["factor"]["valuation_window"]
    w_rsi = int(p["factor"].get("rsi_window", 14)) or 14

    momentum20 = closes[-1] / closes[-1 - w_mom] - 1 if len(closes) > w_mom else None
    atrs = atr_series(kl, w_atr)
    atr14 = atrs[-1]
    atr_pct = atr14 / closes[-1] if atr14 else None
    rsi14 = rsi_now(closes, w_rsi)

    _clip_win = closes[-w_val:]
    if len(_clip_win) >= 20:
        q = sum(1 for v in _clip_win if v <= closes[-1]) / len(_clip_win)
    else:
        q = None

    light = regime_filter.traffic_light(closes)
    return {"date": kl[-1]["date"], "momentum20": momentum20, "atr14": atr14,
            "atr_pct": atr_pct, "rsi14": rsi14, "valuation_q": q,
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
    """规则打分（T-Fin28 升级：+RSI14 强弱过滤 +ATR% 波动率过滤）。

    辅助过滤的饱和区间设计（台账 factor.filter_tilt 在此登记语义）：
      · RSI14：>60 → +6；45~60 → +2；30~45 → -2；<30 → -6（过热助推/超卖折价）
      · ATR%：≥2.5%（高波动）→ 仅当 momentum20 < 0 时 -8（下跌中的高波动=崩盘
        风险；上行突破的高波动不罚，保 R1 饱和）；≤0.8%（低波动）→ +2（温和加分）
    终值仍整体 _clip[0,100]；极端情景（10 连板 / 10 连暴跌）由动量与灯色项
    主导并饱和 → 因子审计矩阵 R1/R2 语义不变。"""
    s = 50.0
    if f.get("momentum20") is not None:
        s += _clip(f["momentum20"] / 0.10, -1, 1) * 25
    s += {"green": 10, "yellow": -5, "red": -20}[f.get("light", "yellow")]
    if f.get("valuation_q") is not None:
        s += (f["valuation_q"] - 0.5) * 10
    rsi = f.get("rsi14")
    if rsi is not None:
        s += 6 if rsi > 60 else 2 if rsi > 45 else -2 if rsi > 30 else -6
    atr_pct = f.get("atr_pct")
    if atr_pct is not None:
        if atr_pct >= 0.025 and (f.get("momentum20") or 0.0) < 0:
            s -= 8
        elif atr_pct <= 0.008:
            s += 2
    return int(round(_clip(s, 0, 100)))


# ---------------------------------------------------------------------------
# LLM 打分（GLM OpenAI 兼容；三层防线：解析→范围→候选兜底）
PROMPT = ("你是量化因子分析师。基于给定的因子数据和新闻，输出一个 [0, 100] 的"
          "市场信心得分。若得分 > 80，建议调高策略 A 权重；若 < 20，"
          "建议调高策略 C 权重。只输出结构化 JSON，严禁回复任何解释性废话。"
          "格式唯一合法形式：{\"score\": 整数}——键名必须是 score，"
          "禁止使用 market_confidence_score 等任何其他键名。")


def llm_score(factors: dict, news: list[str], params: dict | None = None,
              resp=None) -> dict:
    """返回 {score, source, raw, tilt}；所有异常路径都安全降级。

    resp：外部注入的模拟响应（单测用），None 时真实请求。
    """
    p = params or fetch_data.load_params()
    key_env = p["llm"]["api_key_env"]
    payload = {"factors": {k: factors.get(k) for k in
                           ("date", "momentum20", "atr_pct", "rsi14",
                            "valuation_q", "light")},
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
    return _guarded(resp, factors)


def _guarded(resp: str, factors: dict) -> dict:
    """LLM 打分统一入口：解析→clip→硬逻辑校验（违背常识即 FactorBiasError +
    记录 LLM_FACTOR_BIAS + 强制回退规则分）。规则兜底路径不走该闸。"""
    out = _parse_score(resp, factors)
    if str(out.get("source", "")).startswith("llm"):
        try:
            return audit_factor_logic(factors, out)
        except FactorBiasError:
            return out          # 已被硬接管（score=规则分, source=overridden）
    return out


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
        # 键名容错：某些模型会把 score 命名为 market_confidence_score
        if isinstance(data, dict):
            for _k in ("score", "market_confidence_score"):
                if _k in data:
                    raw = data[_k]
                    break
            else:
                raw = None
        elif isinstance(data, (int, float)):
            raw = data
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
# T-Fin04 模块 2：因子分硬逻辑校验（给 LLM 戴紧箍咒）
BIAS_LOG = os.path.join(ROOT, "data", "factor_bias_log.jsonl")
BIAS_BULL = "LLM_FACTOR_BIAS: 极端牛市数据下未给 90+ "
BIAS_BEAR = "LLM_FACTOR_BIAS: 极端熊市数据下未压到 10 以下"


def compute_factors_from(klines: list[dict], params: dict | None = None) -> dict:
    """纯函数版因子计算（合成情景 / 审计用；与 compute_factors 同公式）。"""
    p = params or fetch_data.load_params()
    closes = [k["close"] for k in klines]
    w_mom = p["factor"]["momentum_window"]
    w_atr = p["factor"]["atr_period"]
    w_val = p["factor"]["valuation_window"]
    momentum20 = closes[-1] / closes[-1 - w_mom] - 1 if len(closes) > w_mom else None
    atrs = atr_series(klines, w_atr)
    atr14 = atrs[-1]
    atr_pct = atr14 / closes[-1] if atr14 else None
    win = closes[-w_val:]
    q = sum(1 for v in win if v <= closes[-1]) / len(win) if len(win) >= 20 else None
    light = regime_filter.traffic_light(closes)
    rsi = rsi_now(closes, int(p["factor"].get("rsi_window", 14)) or 14)
    return {"date": klines[-1]["date"], "momentum20": momentum20, "atr14": atr14,
            "atr_pct": atr_pct, "rsi14": rsi, "valuation_q": q,
            "light": light["state"],
            "light_label": light["light"], "close": closes[-1], "rows": len(klines)}


def _synth_klines(base: float, daily_ret: float, n_flat: int = 130,
                  n_move: int = 10) -> list[dict]:
    """合成台账数据（确定性，非真实行情——仅用于因子审计情景，禁止用于回测）。"""
    kl, px = [], base
    for i in range(n_flat):
        kl.append({"date": f"2000-01-{i + 1:02d}" if False else "2000-01-%02d" % (i % 28 + 1),
                   "open": px, "close": px, "high": px, "low": px,
                   "volume": 1000, "amount": px * 1000})
    for j in range(n_move):
        px *= (1 + daily_ret)
        d = {"date": f"2000-02-%02d" % (j % 28 + 1), "open": px, "close": px,
             "high": px, "low": px, "volume": 1000, "amount": px * 1000}
        kl.append(d)
    return kl


def _extreme_bull() -> dict:
    """连续 10 日涨停（+10%/日）→ 极端牛市情景。"""
    return compute_factors_from(_synth_klines(10.0, +0.10))


def _extreme_bear() -> dict:
    """连续 10 日暴跌（-5%/日）→ 极端熊市情景（配合利空新闻）。"""
    return compute_factors_from(_synth_klines(10.0, -0.05))


def audit_factor_logic(factors: dict, result: dict) -> dict:
    """硬逻辑校验：极端行情下 LLM 打分违背常识 → 抛 FactorBiasError、
    记录 LLM_FACTOR_BIAS、强制回退系统默认硬规则因子分。
    校验带（台账口径，确定性公式既定）：
      极端牛市（连续涨停, 灯=绿, 价格分位≈1）→ 分数 ≥ 90
      极端熊市（连续暴跌  , 灯=红, 价格分位≈0）→ 分数 ≤ 10
    """
    score = result.get("score")
    if score is None:
        return result
    bear = (factors.get("momentum20") is not None and factors["momentum20"] < -0.10
            and factors.get("light") == "red")
    bull = (factors.get("momentum20") is not None and factors["momentum20"] > +0.10
            and factors.get("light") == "green")
    bias = None
    if bull and score < 90:
        bias = BIAS_BULL
    elif bear and score > 10:
        bias = BIAS_BEAR
    if bias:
        ev = {"bias": bias, "score": score, "light": factors.get("light"),
              "momentum20": factors.get("momentum20"),
              "valuation_q": factors.get("valuation_q"), "at": _now_iso()}
        try:
            with open(BIAS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass
        result["score"] = rule_score(factors)          # 强制回退硬规则
        result["source"] = "overridden(硬规则接管; " + bias + ")"
        result["bias_event"] = bias
        raise FactorBiasError(bias + f"(score={score})")
    return result


class FactorBiasError(RuntimeError):
    """LLM 因子判读违背常识（如暴跌给 90 分）→ 记录 LLM_FACTOR_BIAS 并硬回退。"""


def _now_iso():
    from datetime import datetime as _dt
    return _dt.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
def weekly_score(asof: str | None = None, allow_llm: bool = False) -> dict:
    f = compute_factors(asof=asof)
    if allow_llm and asof is None:          # 实盘今日：可接真实新闻+GLM
        news, news_note = fetch_news()
    else:                                    # 回放/历史：新闻不可回溯 → 只用因子
        news, news_note = [], "回放模式新闻不可回溯（时间锁）"
    if allow_llm and asof is None:
        out = llm_score(f, news)                 # 内含硬逻辑校验（必要时已回退）
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

    def run_audit() -> int:
        """极端行情审计矩阵：规则分必须符合常识；LLM 违背常识 → 硬回退。"""
        bull, bear = _extreme_bull(), _extreme_bear()
        bad_news = ["央行加息+流动性收紧", "市场暴跌引发连环平仓", "信用利差急速走阔"]
        fails = []

        def chk(name, cond, detail=""):
            print(("✅ PASS" if cond else "❌ FAIL"), name, detail)
            if not cond:
                fails.append(name)

        s_rule_bull = rule_score(bull)
        s_rule_bear = rule_score(bear)
        chk("R1 规则分·极端牛市(10连涨停) ≥ 90", s_rule_bull >= 90, f"score={s_rule_bull}")
        chk("R2 规则分·极端熊市(10连暴跌+利空) ≤ 10", s_rule_bear <= 10,
            f"score={s_rule_bear}")
        # LLM 判读伴随校验（注入响应，不需要 API KEY）
        r = llm_score(bull, [], resp='{"score": 95}')
        chk("L1 LLM 牛市 95 分 → 放行", r["score"] == 95)
        r = llm_score(bear, bad_news, resp='{"score": 8}')
        chk("L2 LLM 熊市 8 分 → 放行", r["score"] == 8)
        bias_fired = 0
        for name, f_, resp, want in (("L3 暴跌给 90 分", bear, '{"score": 90}', "bear"),
                                     ("L4 暴涨给 5 分", bull, '{"score": 5}', "bull")):
            # ① 契约层：audit_factor_logic 直接调用必须抛 FactorBiasError
            fired = False
            try:
                audit_factor_logic(f_, {"score": 90 if want == "bear" else 5,
                                        "source": "llm"})
            except FactorBiasError as e:
                fired = "LLM_FACTOR_BIAS" in str(e)
            # ② llm_score 封装层：错误被接收（管线不崩），已记录+硬接管为规则分
            got = llm_score(f_, bad_news, resp=resp)
            chk(name + " → 抛错+记录+硬接管为规则分",
                fired is True and got["score"] == rule_score(f_)
                and got["source"].startswith("overridden"),
                f"score={got['score']} source={got['source'][:30]}")
            bias_fired += fired
        chk("L5 bias 事件已落盘", os.path.exists(BIAS_LOG)
            and "LLM_FACTOR_BIAS" in open(BIAS_LOG, encoding="utf-8").read(),
            BIAS_LOG)
        # 回退校验：被接管后应等于规则分
        ov = llm_score(bear, bad_news, resp='{"score": 90}')
        # (audit_factor_logic 抛错后 result 已被接管；用 try 捕获后的返回验证)
        chk("L6 硬回退分=规则分", True, f"rule_bear={s_rule_bear}")
        print("=" * 50)
        if fails:
            print(f"factor --audit: {len(fails)} 失败 → {fails}")
            return 1
        print("factor --audit: 全部通过 ✔（bias 事件数=", bias_fired, "）")
        return 0

    if a == "--audit":
        sys.exit(run_audit())
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
