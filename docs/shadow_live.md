# T-Fin05 影子实盘引擎（Shadow Live）——运行手册与降级策略

> 定位：把 T-Fin01→04 打磨出的**确定性信号引擎**接到**真实行情只读接口**
> 上，按真实时间轴逐日"影子交易"：信号真实产生、真实时价模拟撮合、台账真实落库，
> **绝不连接券商、绝不打款**（铁律 1：100 万模拟资金）。

## 1. 架构与每日时间轴（北京时间）

```text
08:45  live_monitor.py --plan-push   预案：读 ensemble.export_plan()（只用 ≤ 前一交易日
                                    收盘数据），推送"今日预案"+ reports/shadow_plan_<date>.md
09:25  live_data_feed.py --heartbeat 盘前心跳：5 只标的行情新鲜度 ≤180s
09:30  shadow_broker.py --match      开盘撮合：真实时价 × portfolio.place_orders
09:30-15:00 每5min live_monitor.py --monitor  沪深300 ≤ -2% → 立即紧急 webhook（不等收盘）
14:55  live_data_feed.py --heartbeat 尾盘前心跳
15:05  shadow_broker.py --ledger     真实 EOD 落库：data/live/*.csv + Alpha + 熔断继承
15:10  shadow_broker.py --review     成交复盘推送 + reports/shadow_review_<date>.md
```

一键验收（回放时钟，真实行情快照、不占用真实资金）：

```bash
bash scripts/run_live_day.sh --replay
```

正式部署（Termux cron，分段调度）：

```cron
45 8  * * 1-5  python3 $HOME/finance-agent/scripts/live_monitor.py --plan-push
25 9  * * 1-5  python3 $HOME/finance-agent/scripts/live_data_feed.py --heartbeat
30 9  * * 1-5  python3 $HOME/finance-agent/scripts/shadow_broker.py --match
*/5 9-14 * * 1-5 python3 $HOME/finance-agent/scripts/live_monitor.py --monitor
55 14 * * 1-5  python3 $HOME/finance-agent/scripts/live_data_feed.py --heartbeat
5  15 * * 1-5  python3 $HOME/finance-agent/scripts/shadow_broker.py --ledger
10 15 * * 1-5  python3 $HOME/finance-agent/scripts/shadow_broker.py --review
```

> cron 时间语义按**北京时间**；设备时区不同（如 UTC+2）时，`live_data_feed.sh_now()`
> 统一换算 Asia/Shanghai（zoneinfo 缺失时回退 UTC+8 定差，已登记）。

## 2. 数据源与新鲜度（模块 1）

| 源 | 接口 | 用途 | 特点 |
|---|---|---|---|
| 主源 | `qt.gtimg.cn/q=sh510300,...`（腾讯） | 实时行情（价/昨收/今开/时戳） | 无验证码；字段位 `_parse_tencent` 落注释防漂移 |
| 备源 | `hq.sinajs.cn/list=...`（新浪，带 Referer） | 主源失败逐标的补缺 | 无验证码；格式解析 `_parse_sina` |
| 指数 | `qt.gtimg.cn/q=s_sh000300` | 盘中监控与 Alpha 基准 | 失败回退 510300 ETF 代理（`source=etf_proxy` 标注） |
| EOD | 东财 kline（fetch_data，双源兜底腾讯） | 15:05 收盘台账收口 | 与 T-Fin01 同一台账口径 |

**心跳红线**：行情自带时戳与当前时刻延迟 > `live.max_quote_delay_sec`（180s，
台账 `index/params.yaml`）→ 报警 + 写 `data/live_mode.json` = **degraded**。
跨日自动复位，待当日心跳重新确认。

## 3. 异常降级策略（分级）

| 级别 | 触发 | 行为 | 台账痕迹 |
|---|---|---|---|
| L0 逐标的补缺 | 个别标的缺失/解析失败 | 新浪备源补；仍缺则该标的开盘单**不产生买入**（信号置 0） | `logs/live_error.log[quote_*.]` |
| L1 数据陈旧心跳失败 | 时戳延迟 >180s 或全源失败 | **degraded 模式**：当日放弃开新交易，**保持现有臂位**（不向疑似错误的数据清仓） | `data/live_mode.json` + db.events |
| L2 撮合期数据不可用 | match 时快照为空/降级未解除 | 本日 match 跳过、只停泊尾差 | db.events(degrade_match) |
| L3 指数行情不可用（监控） | s_sh000300 与代理源都失败 | 本轮报警静默跳过（下轮再试；监控本身永不放倒 cron） | `logs/live_error.log[monitor_index]` |
| L4 程序级异常 | 任何 stage 抛异常 | try-except 捕获 → `logs/live_error.log` + db.events(stage_fatal)，**退出码恒 0**，cron 不倒 | stage_fatal 事件 |

**实盘时间锁**（红线 3）：
- 撮合只发生在交易日 `[09:30, 15:00)`；09:20 / 15:01 / 周六的请求一律拒绝并留痕；
- **计划隔离**：08:45 预案**不触碰任何实时接口**（纯台账计算），从机制上杜绝
  "用当日数据倒推信号"；
- 盘中快照的 `close` 字段一律按"临时=开盘价"记账，真实收盘仅允许在
  15:00 后的 `--ledger`（replay 验收模式需显式 `--replay` 且全程留痕）。

**跌停不买入**（红线 4）：
开盘价 ≤ 昨收×0.90（+0.002 容差，`feed.is_limit_down_open`）→ 该标的当日
买入信号直接置 0，宁缺毋滥，**不伪造流动性**；卖出按开盘价正常撮合
（跌停日卖出本就只能在跌停价排队，按真实可成交价格记录）。

## 4. 台账与溯源（铁律 2/5）

- **撮合数学零改动**：影子撮合直接复用 `portfolio.place_orders` / 引擎
  `clear_sleeves / park_money / redeem / rebalance` —— 手续费、滑点、整手、
  目标权重与正式台账完全同源；
- **影子状态**：`data/shadow_state.json`（首日从 `ensemble_state.json` 克隆持仓，
  此后独立演化于真实时价——影子净值与台账净值的差 = 真实执行差异）；
- **SQLite 台账** `data/shadow_live.db`：
  - `fills(ts, trade_date, leg, code, side, shares, price, fee, reason, limit_blocked)`
  - `eod(trade_date PK, nav, nav_prev, day_ret, bench_index_close, bench_index_pct, alpha, defense, fills_n, lock_remaining, notes)`
  - `events(ts, kind, detail)`（心跳/降级/报警/熔断/跌停拦截全留痕）
- **live CSV** `data/live/*.csv` = 正式台账 CSV 全量 + 逐日真实 EOD 增量；
  沪深300 指数台账 `data/live/000300.csv`（东财 secid 1.000300）；
- **熔断继承（铁律 3）**：15:05 用 `risk_control.check_breakers` 校验影子净值曲线，
  单日 > −3% 或峰值回撤 > −10% → 按真实收盘强平 + 计数锁 3 交易日
  （`locked_remaining`，fragile 式递减），**优化器不可移除**；
- **LLM 零下单权（铁律 4）**：全链路无任何 LLM 参与撮合；因子倾斜只影响
  赛马权重（Python clamp），且计划信号 100% 来自 `strategy.py` 确定性数学。

## 5. 自测与验收命令

```bash
python3 scripts/shadow_broker.py --test      # 15 项：时间锁/跌停/降级/落库/解析器/撮合合成
python3 scripts/validate_sim.py              # V1-V9（V9=影子门禁 + live.* 台账）
bash scripts/run_live_day.sh --replay        # 一日全流程回放（真实行情快照）
```

## 6. 人工干预物理开关 + 看板 + 聚合（T-Fin06 新增）

```bash
python3 scripts/kill_switch.py --panic "原因"   # 一键强平 A/B（台账真实收盘价+滑点费）
                                              # + 写根目录 PANIC_LOCK.flag → 全局停牌
python3 scripts/kill_switch.py --release       # 解锁，恢复自动运转
python3 scripts/dashboard.py --port 8501       # 本地看板 http://127.0.0.1:8501
```

- **开关纪律**：flag 存在期间，`run_live_day.sh` 与 `shadow_broker --match`
  首行即拦截（"人工干预锁定中，跳过今日交易"）；数据库只增不改不删
  （panic 只追加 fills/events 行，eod 净值曲线零触碰）。
- **看板**（Flask+SVG，Streamlit 依赖链在 Termux/arm64 无法编译——已登记）：
  指标卡（净值/今日盈亏/信号灯/MDD/夏普/卡玛）+ A/B/C 腿持仓饼图 +
  影子 vs 沪深300 净值曲线（附正式台账回放基准虚线）+ 最近 20 笔流水；只读。
- **聚合**：`aggregate_performance.py` 每日 15:15 产出
  `reports/aggregate_<date>.md`——10 日胜率热力图（A/B × 黄/绿灯，红灯剔除）
  + 连涨连跌 + Alpha 合计 + PANIC 开关状态尾注。

## 7. 已知边界（诚实登记）

1. 回放验收模式下，心跳新鲜度以"行情自带会话时戳"为准（非墙钟）——
   快照与 09:30 实盘进程取的是**同一数据集**，仅时钟为演播时钟；
2. 影子首日从 ensemble 台账克隆持仓（成本价沿用），影子与正式台账的
   分叉从当日起算，报告中已注明 `seeded_from`；
3. EOD 个别标的"行情未出"时以最近一日收盘估值并留 `eod_gap` 痕迹；
4. `tzdata` 新增为环境依赖（已安装）；缺失时代码回退 UTC+8 定差。
