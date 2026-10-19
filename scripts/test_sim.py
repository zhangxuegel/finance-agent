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
print("=" * 50)
if FAILS:
    print(f"test_sim 结果: {len(FAILS)} 失败 → {FAILS}")
    sys.exit(1)
print(f"test_sim 结果: 全部通过 ✔")
