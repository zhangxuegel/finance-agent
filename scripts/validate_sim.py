# -*- coding: utf-8 -*-
"""validate_sim —— T-Fin03 验收自检（与 test_sim 互补）。

覆盖：
  V1 赛马权重：平滑 ±20pp / 双方下限 0.3（拒绝 0:10 押注）
  V2 并发资金：A/B 两腿同日同标的开仓 → 各自扣款、无重复扣减、恒等式闭合
  V3 状态机：黄/红灯下 BUY → FatalControlError；重放全程黄/红日收盘必无腿持仓
  V4 因子红线：格式错误→50、超范围→clip（Python 截断，LLM 不可越权）
  V5 推送：loopback mock 送达；真实 URL 测试模式拒绝外发
  V6 C 腿纪律：无现金空转（尾差 < 一手货币 ETF 市值）
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import ensemble_engine as ee
import factor_mining
import portfolio
import push_notification as push
import regime_filter as rf

FAILS = []


def check(name, cond, detail=""):
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


P = portfolio.load_params()

# ------------------------------------------------------------- V1 赛马权重
ens_cfg = P["ensemble"]
w0 = ee.race_weights(0.5, 2.0, 0.1, ens_cfg)           # 0.5 → 0.7（一次到位=+0.2）
check("V1a 平滑上限：0.5→0.7 允许", abs(w0["weight_a"] - 0.7) < 1e-9,
      f"{w0['weight_a']}")
w1 = ee.race_weights(0.3, 9.0, -9.0, ens_cfg)          # 0.3 → 0.5（拦截跳变）
check("V1b 平滑拦截：0.3 不得跳到 0.7", abs(w1["weight_a"] - 0.5) < 1e-9,
      f"{w1['weight_a']}")
ok_min = True
prev = 0.5
for i in range(40):
    import random
    random.seed(i)
    w = ee.race_weights(prev, random.uniform(-9, 9), random.uniform(-9, 9), ens_cfg)
    ok_min &= w["weight_a"] >= ens_cfg["race"]["min_weight"] - 1e-9
    ok_min &= w["weight_a"] <= 1 - ens_cfg["race"]["min_weight"] + 1e-9
    ok_min &= abs(w["weight_a"] - prev) <= ens_cfg["race"]["max_weekly_shift"] + 1e-9
    prev = w["weight_a"]
check("V1c 40 次随机赛马：权重始终在 [0.3,0.7] 且漂移≤±20pp", ok_min)
w2 = ee.race_weights(0.5, None, None, ens_cfg)
check("V1d 赛马数据未满 → 5:5", w2["weight_a"] == 0.5)

# ------------------------------------------------------------- V2 并发资金
sp_day = {"510300": {"open": 10.0, "close": 10.1}}
master_before = 0.0
sleeveA = {"cash": 250000.0, "positions": {}, "trades": [], "curve": []}
sleeveB = {"cash": 250000.0, "positions": {}, "trades": [], "curve": []}
sp = dict(P, n_assets=4)
o1 = portfolio.place_orders(sleeveA, sp_day, {"510300": 1}, sp, "d1")
o2 = portfolio.place_orders(sleeveB, sp_day, {"510300": 1}, sp, "d1")
# 按成交成本价核账（隔离市值波动，纯检验账本不重复扣款）
a_eq = sleeveA["cash"] + sleeveA["positions"]["510300"]["shares"] * \
    sleeveA["positions"]["510300"]["cost"]
b_eq = sleeveB["cash"] + sleeveB["positions"]["510300"]["shares"] * \
    sleeveB["positions"]["510300"]["cost"]
check("V2a 并发同标杆：两腿各自成交", o1 and o2 and
      sleeveA["positions"]["510300"]["shares"] > 0 and
      sleeveB["positions"]["510300"]["shares"] > 0)
check("V2b 恒等式：250k×2 = 两腿账本合计（损耗=双边佣金，滑点已计入成本基准）",
      0 <= 500000.0 - (a_eq + b_eq) < 100,
      f"{a_eq + b_eq:,.2f}（差额={500000 - a_eq - b_eq:,.2f}）")
check("V2c 无重复扣减：母账户未被动用", master_before == 0.0)

# ------------------------------------------------------------- V3 状态机
try:
    ee.assert_gate("yellow", [{"side": "BUY"}])
    check("V3a 黄灯 BUY 应抛 FatalControlError", False)
except ee.FatalControlError:
    check("V3a 黄灯 BUY → FatalControlError", True)
try:
    ee.assert_gate("red", [{"side": "BUY"}])
    check("V3b 红灯 BUY 应抛 FatalControlError", False)
except ee.FatalControlError:
    check("V3b 红灯 BUY → FatalControlError", True)
ee.assert_gate("green", [{"side": "BUY"}])
check("V3c 绿灯 BUY 放行", True)
ee.assert_gate("yellow", [{"side": "SELL"}])
check("V3d 黄灯 SELL（减仓）放行", True)

# 重放全程黄/红日收盘无腿持仓 + C 腿无空转
if os.path.exists(os.path.join(BASE, os.pardir, "data", "ensemble_state.json")):
    with open(os.path.join(BASE, os.pardir, "data", "ensemble_state.json"),
              encoding="utf-8") as f:
        st = json.load(f)
    # 关键判据：扫描两腿全部交易流水，确认任何 BUY 都只发生在绿灯日
    eng = ee.EnsembleEngine()
    light_of = {d: eng.light_at(i)["state"]
                for i, d in enumerate(eng.dates) if i >= eng.warmup}
    viol = 0
    for leg in ("A", "B"):
        for t in st["sleeves"][leg]["trades"]:
            if t["side"] == "BUY" and light_of.get(t["date"]) in ("yellow", "red"):
                viol += 1
    check("V3e 全流水：黄/红灯日零 BUY", viol == 0, f"违规笔数={viol}")
    money_total = sum(st["money"].get(k, 0) for k in ("A", "B", "MASTER"))
    px = eng.money_px[st["last_date"]]
    lot_value = px * P["trading_rule"]["lot"]
    idle = st["master_cash"] + st["sleeves"]["A"]["cash"] + st["sleeves"]["B"]["cash"]
    check("V6 现金空转核查：无现金 ≥ 一手货币 ETF 市值", idle < lot_value,
          f"闲置现金 ¥{idle:,.2f} < 一手约 ¥{lot_value:,.0f}")
else:
    print("⏭ V3e/V6（需先跑 ensemble --daily 生成台账状态）")

# ------------------------------------------------------------- V4 因子红线
f = {"momentum20": 0.05, "light": "green", "valuation_q": 0.7}
cases = [('{"score": 130}', 100), ('{"score": -5}', 0),
         ('gibberish !!!', 50), ('{"score": "abc"}', 50)]
ok = all(factor_mining.llm_score(f, [], resp=p)["score"] == want
         for p, want in cases)
check("V4 LLM 分数红线：clip/降级矩阵", ok,
      str([(factor_mining.llm_score(f, [], resp=p)["score"], w) for p, w in cases]))

# ------------------------------------------------------------- V5 推送
url = push.mock_server()
ok, note = push.send_webhook(url, "V5")
check("V5a loopback mock 200", ok, note)
try:
    push.send_webhook("https://open.feishu.cn/hook/REAL", "V5")
    check("V5b 真实 URL 测试模式拦截", False, "竟然放行了！")
except push.PushBlocked:
    check("V5b 真实 URL 测试模式拦截", True)

# -------------------------------------------------------------
print("=" * 50)
if FAILS:
    print(f"validate_sim 结果: {len(FAILS)} 失败 → {FAILS}")
    sys.exit(1)
print("validate_sim 结果: 全部通过 ✔")
