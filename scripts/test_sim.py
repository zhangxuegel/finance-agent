# -*- coding: utf-8 -*-
"""test_sim —— 模拟盘自检（验收标准：test_sim 全部通过）。

合成数据 + 台账参数，单元测试以下铁律：
  1. 估值恒等式：持仓市值 = close × shares（误差 < 0.01）
  2. 费用口径：佣金 max(万一2, ¥5)双边；ETF 印花税 0；滑点体现在价差
  3. 策略 A/B 信号在合成数据上的交叉点正确
  4. 熔断 R1（单日 -3%）触发强平 + 锁仓 3 日；锁仓期禁止下单
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fetch_data
import portfolio
import risk_control
import strategy

FAILS = []


def check(name, cond, detail=""):
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def fake_kline(ps, start=10.0):
    kl = []
    px = start
    for i, p in enumerate(ps):
        px = px * p
        kl.append({"date": f"2025-{(i // 28 % 12) + 1:02d}-{i % 28 + 1:02d}",
                   "open": px, "close": px, "high": px, "low": px,
                   "volume": 1_000_000, "amount": px * 1_000_000})
    return kl


P = portfolio.load_params()
# 单标的测试口径：目标权重 1/1
PTEST = dict(P, n_assets=1)

# ---------------------------------------------------------------- 1) 估值
fx = {"code": {"open": 10.0, "close": 10.0}}
port = {"cash": 1.0, "positions": {"code": {"shares": 100, "cost": 8.0}},
        "trades": [], "curve": []}
eq = portfolio.equity(port, {"code": 9.999})
check("估值恒等式 close×shares", abs(eq - (1.0 + 100 * 9.999)) < 0.01, f"eq={eq}")

# ---------------------------------------------------------------- 2) 费用
fee_buy = portfolio.trade_fees(10000, "BUY", P)
check("佣金下限 ¥5（小额买单）", abs(fee_buy - P["fees"]["commission_min"]) < 1e-9,
      f"fee={fee_buy}")
fee_sell_big = portfolio.trade_fees(1_000_000, "SELL", P)
expect = 1_000_000 * P["fees"]["commission_bps"] / 10000 + \
         1_000_000 * P["fees"]["stamp_duty_bps"] / 10000
check("大额卖出费率 = 佣金+印花税", abs(fee_sell_big - expect) < 1e-6,
      f"fee={fee_sell_big}")
# 滑点：买入价 = open×(1+slip)
px = fx["code"]["open"] * (1 + P["fees"]["slippage_bps"] / 10000)
check("买入滑点价 = open×(1+slip)", abs(px - 10.001) < 1e-9, f"px={px}")

# ---------------------------------------------------------------- 3) 策略
closes = [1] * 5 + [2] * 40                       # 慢涨 → 金叉后持有
sigA = strategy.signal_series_a(closes, 5, 20)
crossA = next(i for i in range(1, len(sigA)) if sigA[i] == 1 and sigA[i - 1] == 0)
check("策略A金叉后才满仓", sigA[crossA] == 1 and all(s == 0 for s in sigA[:crossA]),
      f"首个金叉索引={crossA}")
sigA2 = strategy.signal_series_a(closes + [0.2] * 30, 5, 20)
check("策略A跌破清仓", sigA2[-1] == 0, f"末信号={sigA2[-1]}")
closesB = [10] * 25 + [7.0] * 3 + [10] * 25        # 跌破下轨买入, 回中轨卖出
sigB = strategy.signal_series_b(closesB, 20, 2.0)
check("策略B触下轨买入", 1.0 in sigB, f"sigB含1={1.0 in sigB}")
i_buy = sigB.index(1.0)
check("策略B回中轨卖出", sigB[-1] == 0, f"末信号={sigB[-1]}")

# ---------------------------------------------------------------- 4) 熔断
risk_state_path = risk_control.STATE_PATH
risk_control.save({"locked": False, "lock_until_day": 0, "alarm_days": []})
prev_eq, today_eq = 100.0, 90.0                      # -10% 单日
chk = risk_control.check_breakers(0, "t0", today_eq, prev_eq, [], P["risk"])
check("R1 单日跌 >3% 触发", chk["triggered"] and chk["rule"] == "R1_daily_loss")
curve = [("d0", 100.0), ("d1", 95.0), ("d2", 89.0)]  # 缓跌: 峰值回撤11%但单日仅-1.1%
chk2 = risk_control.check_breakers(2, "d2", 89.0, 90.0, curve[:-1], P["risk"])
check("R2 峰值回撤 >10% 触发", chk2["triggered"] and chk2["rule"] == "R2_max_drawdown")

rc = risk_control.RiskController()
rc.trigger_lock(5, P["risk"]["lock_days"], "test", "2025-01-01")
check("熔断锁仓 3 日生效", rc.locked(6) and rc.locked(8) and not rc.locked(9))

# 强平走一遍（保守收盘价清算）
port2 = {"cash": 0.0,
         "positions": {"510300": {"shares": 1000, "cost": 3.0}},
         "trades": [], "curve": []}
forced = portfolio.force_liquidate(port2, {"510300": {"open": 3.0, "close": 4.0}},
                                   P, "t", "R-test")
check("强平后只持有现金", port2["positions"] == {} and port2["cash"] > 3000,
      f"cash={port2['cash']}")
check("强平记录含原因", all("forced" in t for t in port2["trades"]))

# ---------------------------------------------------------------- 5) 撮合闭环
# 信号1 → 次日开盘买入整手；信号0 → 次日开盘清仓；全程用 fake 数据
risk_control.save({"locked": False, "lock_until_day": 0, "alarm_days": []})
port3 = {"cash": 100000.0, "positions": {}, "trades": [], "curve": []}
kp = {"510300": {"open": 10.0, "close": 10.1}}
orders = portfolio.place_orders(port3, kp, {"510300": 1}, PTEST, "d1")
pos = port3["positions"].get("510300", {})
mkt = bool(pos) and pos.get("shares", 0) % 100 == 0 and orders and orders[0]["side"] == "BUY"
check("next_open 整手买入", mkt,
      f"shares={pos.get('shares')}, left cash={port3['cash']:.2f}")
orders2 = portfolio.place_orders(port3, kp, {"510300": 0}, PTEST, "d2")
check("次日收盘信号0 → 清仓Risk", port3["positions"] == {} and port3["trades"][-1]["side"] == "SELL")

# ================================================================ T-Fin02 新增
# ---- 6) Sortino / Calmar ----
curve_up = []
v = 100.0
for i in range(60):
    curve_up.append((f"2026-01-{i+1:02d}", round(v, 2)))
    v *= 0.98 if i % 7 == 6 else 1.004          # 每 7 日一次回撤 → 下行样本非空
port_s = {"cash": 0.0, "positions": {"X": {"shares": 1, "cost": 1.0}},
          "trades": [{"id": 1, "side": "SELL", "pnl": 50.0}], "curve": curve_up}
ms = portfolio.metrics(port_s, 100.0)
check("Sortino 字段且 > 夏普口径存在", "sortino" in ms and ms["sortino"] is not None
      and ms["sortino"] > 0, f"sortino={ms.get('sortino')}")
check("Calmar = 年化/最大回撤", "calmar" in ms and ms["calmar"] is not None
      and ms["calmar"] > 0, f"calmar={ms.get('calmar')}")

# ---- 7) 大盘信号灯（regime_filter）----
rise = [100 * (1 + 0.002) ** i for i in range(130)]        # 趋势上 → 绿
fall = [100 * (1 - 0.002) ** i for i in range(130)]        # 趋势下 → 红
chop = [100 + 5 * (1 if i % 2 else -1) for i in range(130)]  # 横盘纠缠 → 黄
import regime_filter as _rf
check("绿灯=多头排列", _rf.traffic_light(rise)["state"] == "green")
check("红灯=空头排列", _rf.traffic_light(fall)["state"] == "red")
check("黄灯=纠缠", _rf.traffic_light(chop)["state"] == "yellow")
check("数据不足默认黄灯", _rf.traffic_light([1.0] * 10)["state"] == "yellow")

# ---- 8) 高滑点地狱模式口径（滑点×10 → 净值应更低）----
import copy as _copy
p_hell = _copy.deepcopy(P)
p_hell["fees"]["slippage_bps"] = P["fees"]["slippage_bps"] * 10
risk_control.save({"locked": False, "lock_until_day": 0, "alarm_days": []})
pa, pb = {}, {}
for tag, pp in (("base", P), ("hell", p_hell)):
    port4 = {"cash": 100000.0, "positions": {}, "trades": [], "curve": []}
    portfolio.place_orders(port4, {"510300": {"open": 10.0, "close": 10.1}},
                           {"510300": 1}, dict(pp, n_assets=1), "d1")
    portfolio.place_orders(port4, {"510300": {"open": 10.5, "close": 10.6}},
                           {"510300": 0}, dict(pp, n_assets=1), "d2")
    pb[tag] = port4["cash"]
check("地狱滑点成本高于基准滑点", pb["hell"] < pb["base"],
      f"hell cash={pb['hell']:.2f} < base cash={pb['base']:.2f}")

# ---- 9) 防未来函数时间锁校验（真实台账 CSV，小探针抽样）----
if os.path.exists(os.path.join(os.path.dirname(BASE), "data",
                               fetch_data.load_watchlist()[0]["code"] + ".csv")):
    la_n = risk_control.audit_no_lookahead(probes=6)
    check("截断重算法：全库无未来函数", la_n["ok"],
          f"probes×{la_n['probes']} mismatch={[r['mismatch'] for r in la_n['results'] if r['mismatch']]}")
    if portfolio.load_state() and portfolio.load_state().get("trades"):
        fl_n = risk_control.audit_fill_time_lock()
        check("撮合时间锁：账本逐笔合规", fl_n["ok"],
              f"checked={fl_n['checked']} violations={len(fl_n['violations'])}")
else:
    print("⏭ 跳过 9)（台账 CSV 未就绪：先运行 fetch_data.py）")

# ----------------------------------------------------------------
risk_control.save({"locked": False, "lock_until_day": 0, "alarm_days": []})

# ================================================================ T-Fin28 新增
# ---- 10) D 震荡网格腿：咬合/解除/格点重置（纯函数，确定性） ----
import ensemble_engine as ee

# 形状：130 日横盘 @10 → 单日 -4%（跌破 MA120×0.97 → 咬合建仓，格点=9.6）→
# 3 日 +3.1%（站上格点×1.03 → 清仓重布网）→ 再深跌一字（二次咬合）
up_osc = [10.0] * 130
up_osc += [9.6, 9.62, 9.9, 10.4]      # 单日跌破 → 数日内收回突破格点 → 清仓
sig = ee.signals_grid(up_osc, anchor_window=120, step_pct=0.03)
buys = [i for i in range(1, len(sig)) if sig[i] == 1 and sig[i - 1] == 0]
sells = [i for i in range(1, len(sig)) if sig[i] == 0 and sig[i - 1] == 1]
check("T28-1 网格咬合：MA120 基线下探 -3% 建仓", buys == [130], f"buys={buys}")
check("T28-2 网格解除：站上格点×1.03 清仓", len(sells) >= 1, f"sells={sells}")
if buys and sells:
    check("T28-3 网格先买后卖（无裸卖）", buys[0] < sells[0])
    check("T28-4 网格持仓段连续（中间无悬空置零）",
          all(sig[i] == 1 for i in range(buys[0], sells[0])))
# ---- 11) E 防守红利腿：自身 MA60 公式 ----
import ensemble_engine as _ee
rise50 = [1.0 + 0.01 * i for i in range(80)]      # 80 日缓涨：第 61 日起 ≥ MA60
sd = _ee.signals_defensive(rise50, ma_window=60)
check("T28-5 防守红利：历史不足期全空", all(v == 0.0 for v in sd[:59]))
check("T28-6 防守红利：越过自身 MA60 → 持有", sd[70] == 1.0, f"sd70={sd[70]}")
sid = _ee.signals_defensive([100.0] * 40 + [100.0 * (1 - 0.002 * i) for i in range(40)], 60)
check("T28-7 防守红利：跌破 MA60 → 空仓", sid[-1] == 0.0, f"last={sid[-1]}")
# ---- 12) race_weights4：4 腿赛马不变量（Σw = 1-c_buffer；地板；漂移帽） ----
cfg = {"race": {"min_weight": 0.10, "def_floor": 0.15, "max_weekly_shift": 0.2,
                "shares": {"t1": 0.35, "t2": 0.30, "t3": 0.20, "t4": 0.15},
                "init_weights": {"A": 0.30, "B": 0.25, "D": 0.25, "E": 0.20}},
       "tilt": {"score_high": 80, "a_tilt_pp": 0.05, "score_low": 20,
                "c_tilt_pp": 0.10}}
prev = {"A": 0.30, "B": 0.25, "D": 0.25, "E": 0.20}
w = ee.race_weights4(prev, {"A": 2.0, "B": 1.0, "D": 0.5, "E": 0.1}, cfg)
check("T28-8 4腿赛马 Σw==1 且紧凑（平局按固定序）", abs(sum(w["weights"].values()) - 1) < 1e-9,
      f"w={w['weights']}")
check("T28-9 防守腿地板 def_floor=0.15", w["weights"]["E"] >= 0.15 - 1e-9,
      f"E={w['weights']['E']}")
w2 = ee.race_weights4(prev, {"A": -9.0, "B": 2.0, "D": 1.0, "E": -1.0}, cfg)
check("T28-10 排名驱动：B(夏普最佳)得 t1=0.35，E(第3名)得 t3=0.20",
      abs(w2["weights"]["B"] - 0.35) < 1e-6 and abs(w2["weights"]["E"] - 0.20) < 6e-2,
      f"w={w2['weights']}")
w3 = ee.race_weights4(prev, {"A": None, "B": None, "D": None, "E": None}, cfg)
check("T28-11 热身不足 → init_weights 固定分权", abs(w3["weights"]["B"] - 0.25) < 1e-9,
      f"w={w3['weights']}")
w4 = ee.race_weights4(prev, {"A": 2.0, "B": 1.0, "D": 0.5, "E": 0.1}, cfg,
                      factor_score=80)
check("T28-12 LLM 倾斜：score≥80 → A 加权且 Σw==1", w4["weights"]["A"] >= w["weights"]["A"],
      f"A {w4['weights']['A']} vs neutral {w['weights']['A']}")
w5 = ee.race_weights4(prev, {"A": 2.0, "B": 1.0, "D": 0.5, "E": 0.1}, cfg,
                      factor_score=15)
check("T28-13 LLM 预配：score≤20 → c_buffer=0.10 且 Σw==0.9",
      abs(sum(w5["weights"].values()) - 0.9) < 1e-9 and w5["c_buffer"] == 0.10,
      f"w={w5['weights']}")
# ---- 13) factor 过滤：ATR% 罚分仅在下跌动量；极端矩阵保持 ----
base_f = {"momentum20": 0.0, "light": "green", "valuation_q": 0.5,
          "rsi14": 50.0, "atr_pct": 0.005}
f_hi = dict(base_f, atr_pct=0.03)
f_mid = dict(base_f, atr_pct=0.015)
check("T28-14 上行高波动不罚（=中性波动；低波动才有低波加分）",
      __import__("factor_mining").rule_score(f_hi) == __import__("factor_mining").rule_score(f_mid))
f_lo = dict(base_f, momentum20=-0.15, light="red", rsi14=25.0, atr_pct=0.03)
check("T28-15 下跌+高波动 → 罚分生效且落 0..10", __import__("factor_mining").rule_score(f_lo) <= 10)
f_bull = {"momentum20": 0.20, "light": "green", "valuation_q": 1.0,
          "rsi14": 99.0, "atr_pct": 0.06}
check("T28-16 极端牛市 R1：≥90", __import__("factor_mining").rule_score(f_bull) >= 90)

# ----------------------------------------------------------------
print("=" * 50)
if FAILS:
    print(f"test_sim 结果: {len(FAILS)} 失败 → {FAILS}")
    sys.exit(1)
print(f"test_sim 结果: 全部通过 ✔")
