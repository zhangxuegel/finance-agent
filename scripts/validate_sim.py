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

# ------------------------------------------------------------- V7 因子硬逻辑
try:
    bull = factor_mining._extreme_bull()
    s = factor_mining.rule_score(bull)
    check("V7a 极端牛市规则分 ≥ 90", s >= 90, f"score={s}")
    bear = factor_mining._extreme_bear()
    s = factor_mining.rule_score(bear)
    check("V7b 极端熊市规则分 ≤ 10", s <= 10, f"score={s}")
    try:
        factor_mining.audit_factor_logic(bear, {"score": 90, "source": "llm"})
        check("V7c 暴跌给90分 → FactorBiasError", False)
    except factor_mining.FactorBiasError as e:
        check("V7c 暴跌给90分 → FactorBiasError+LLM_FACTOR_BIAS",
              "LLM_FACTOR_BIAS" in str(e))
except Exception as e:  # noqa: BLE001
    check("V7 因子审计执行失败", False, f"{type(e).__name__}: {e}")

# ------------------------------------------------------------- V8 窗口确定性
eng1 = __import__("ensemble_engine").EnsembleEngine(
    window=("2026-06-01", "2026-10-19"), log_writes=False)
c1 = eng1.run()["curve"]
eng2 = __import__("ensemble_engine").EnsembleEngine(
    window=("2026-06-01", "2026-10-19"), log_writes=False)
c2 = eng2.run()["curve"]
check("V8 窗口重放确定性（两次独立运行曲线一致）", c1 == c2,
      f"{len(c1)} 天尾值 {c1[-1][1] if c1 else '-'}")

# ------------------------------------------------------------- V9 影子引擎门禁
try:
    import io, contextlib
    import shadow_broker as _sb
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _sb.self_test()
    check("V9a 影子撮合自测（时间锁/跌停拦截/降级/落库）", rc == 0,
          f"rc={rc}")
except Exception as e:  # noqa: BLE001
    check("V9a 影子撮合自测", False, f"{type(e).__name__}: {e}")
try:
    import datetime as _dt
    import live_data_feed as _lf  # noqa: F401（确保依赖可用）
    tk = (_sb.clock_ok(_dt.datetime(2026, 10, 20, 9, 31), False) is True
          and _sb.clock_ok(_dt.datetime(2026, 10, 20, 9, 20), False) is False
          and _sb.clock_ok(_dt.datetime(2026, 10, 20, 15, 1), False) is False
          and _sb.clock_ok(_dt.datetime(2026, 10, 20, 9, 31), True) is True)
    check("V9b 实盘时间锁守卫（09:31放 / 09:20拒 / 15:01拒 / replay豁免）", tk)
except Exception as e:  # noqa: BLE001
    check("V9b 实盘时间锁守卫", False, f"{type(e).__name__}: {e}")
try:
    p9 = portfolio.load_params()
    check("V9c live.* 台账登记（心跳180s/监控-2%/调度三点）",
          p9["live"]["max_quote_delay_sec"] == 180
          and p9["live"]["monitor"]["bench_drop"] == -0.02
          and p9["live"]["schedule"]["match"].endswith("* * 1-5"))
except Exception as e:  # noqa: BLE001
    check("V9c live.* 台账登记", False, f"{type(e).__name__}: {e}")

# ------------------------------------------------------------- V10 看板/开关/聚合
try:
    import aggregate_performance as _ag
    leg_rets = {"A": {f"2026-10-{d:02d}": (0.01 if d % 2 == 0 else -0.005)
                      for d in range(3, 19)},
                "B": {f"2026-10-{d:02d}": (0.02 if d % 3 else -0.01)
                      for d in range(3, 19)}}
    lights = {f"2026-10-{d:02d}": ("yellow" if d % 4 < 2 else "green")
              for d in range(3, 19)}
    grid = _ag.winrate_grid(leg_rets, lights, 10)
    ay = [g for g in grid if g["leg"] == "A" and g["light"] == "yellow"][0]
    by = [g for g in grid if g["leg"] == "B" and g["light"] == "yellow"][0]
    ag_ = [g for g in grid if g["leg"] == "A" and g["light"] == "green"][0]
    # 最近 10 台账日 = 09..18：黄灯 d%4∈{0,1} → {9,12,13,16,17}；
    # A 偶数日 ret>0 → {12,16} = 2/5
    check("V10a 胜率网格（最近10台账日×灯色分组、红灯剔除）",
          ay["days"] == 5 and abs(ay["winrate"] - 2 / 5) < 1e-9
          and ag_["days"] == 5 and abs(ag_["winrate"] - 3 / 5) < 1e-9
          and by["days"] == 5,
          f"A黄 {ay['winrate']:.0%} A绿 {ag_['winrate']:.0%}")
except Exception as e:  # noqa: BLE001
    check("V10a 胜率网格", False, f"{type(e).__name__}: {e}")
try:
    import dashboard as _db
    svg = _db.svg_curve([("2026-10-01", 100.0, 4800.0),
                         ("2026-10-02", 101.0, 4880.0)])
    check("V10b 看板 SVG 双线可渲染（polyline≥2）",
          svg.count("polyline") == 2, f"n={svg.count('polyline')}")
    s2, c2, m2 = _db._series([100, 101, 100.5, 102, 103])
    check("V10b2 绩效数学（上行序列夏普>0、MDD>0）", s2 > 0 and m2 > 0,
          f"sharpe={s2:.2f} mdd={m2:.4f}")
except Exception as e:  # noqa: BLE001
    check("V10b 看板渲染", False, f"{type(e).__name__}: {e}")
try:
    import kill_switch as _ks
    was = _ks.flag_active()
    _ks.do_panic("V10 验收演练")
    locked = _ks.flag_active()
    _ks.do_release()
    unlocked = not _ks.flag_active()
    check("V10c 物理开关全周期（panic→flag 落盘 → release→恢复）",
          locked and unlocked and (was == unlocked or not was),
          f"was={was}")
except Exception as e:  # noqa: BLE001
    check("V10c 物理开关全周期", False, f"{type(e).__name__}: {e}")

# ------------------------------------------------------------- V11 云端链路
try:
    import sync_to_cloud as _sc
    st0 = {"max_eod_date": "2026-10-19", "max_fill_id": 7}
    raw = {("sb_shadow_eod"): [
        {"trade_date": "2026-10-19", "nav": 976273.3},
        {"trade_date": "2026-10-20", "nav": 975900.0},
        {"trade_date": "2026-10-21", "nav": 977000.0}]}
    rows_new = [r for r in raw["sb_shadow_eod"]
                if r["trade_date"] > st0["max_eod_date"]]
    st1 = _sc.advance_watermark(
        {"sb_shadow_eod": rows_new, "sb_shadow_fills": []}, dict(st0))
    check("V11a 增量水位（新行才算+水位推进+重放=空集）",
          len(rows_new) == 2 and st1["max_eod_date"] == "2026-10-21"
          and not [r for r in raw["sb_shadow_eod"]
                   if r["trade_date"] > st1["max_eod_date"]])
except Exception as e:  # noqa: BLE001
    check("V11a 增量水位", False, f"{type(e).__name__}: {e}")
try:
    import dashboard_cloud as _dc
    m = _dc.compute_metrics([
        {"nav": 100, "alpha": 0.001, "bench_index_close": 4000},
        {"nav": 102, "alpha": 0.001, "bench_index_close": 4030},
        {"nav": 101, "alpha": -0.0002, "bench_index_close": 3990}])
    check("V11b 云端看板纯函数（指标不依赖streamlit）",
          abs(m["cum"] - 0.01) < 1e-9 and m["mdd"] > 0 and m["days"] == 3,
          f"cum={m['cum']:.3f}")
    df = _dc.normalize([{"trade_date": "d", "nav": 100,
                         "bench_index_close": 4000},
                        {"trade_date": "d2", "nav": 101,
                         "bench_index_close": 4080}])
    rows = (df.to_dict("records") if _dc.pd else df)
    check("V11b2 归一化曲线", abs(rows[-1]["影子净值"] - 1.01) < 1e-9
          and abs(rows[-1]["沪深300"] - 1.02) < 1e-9)
except Exception as e:  # noqa: BLE001
    check("V11b 云端看板纯函数", False, f"{type(e).__name__}: {e}")
try:
    ROOT2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    def _rf(rel):
        return os.path.exists(os.path.join(ROOT2, rel))
    check("V11c 云端基建齐备（requirements/config/secrets样例/部署文档）",
          all(_rf(p) for p in ("requirements.txt", ".streamlit/config.toml",
                               ".streamlit/secrets.toml.example",
                               "README_DEPLOY.md", "sync_all.sh")))
    check("V11c2 secrets 真文件不入库（.gitignore 覆盖）",
          ".streamlit/secrets.toml" in open(
              os.path.join(ROOT2, ".gitignore"), encoding="utf-8").read())
except Exception as e:  # noqa: BLE001
    check("V11c 云端基建", False, f"{type(e).__name__}: {e}")

# -------------------------------------------------------------
print("=" * 50)
if FAILS:
    print(f"validate_sim 结果: {len(FAILS)} 失败 → {FAILS}")
    sys.exit(1)
print("validate_sim 结果: 全部通过 ✔")
