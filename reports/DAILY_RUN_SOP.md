# T-Fin01 模拟盘 — 每日运行指南 (SOP)

**系统状态（已锁定，不再修改）：**
- 策略引擎：A（双均线趋势追踪，MA 8/30）
- 大盘过滤：关闭（benchmark_filter: false）
- 选股模块：封存（screener.enable: false，仅作盘前参考）
- 风控：单日亏损 >3% 强制清仓锁 3 天；峰值回撤 >10% 强制清仓锁 3 天
- 交割规则：信号日收盘生成 → 次日开盘价撮合（附滑点与手续费）

---

## 一、盘前（交易日 09:00 前）

### 1. 查看昨日信号与持仓

```bash
# 进入工作目录
cd /data/data/com.termux/files/home/finance-agent

# 查看当前账户状态
python3 -c "
import json
p = json.load(open('data/portfolio.json'))
print('last_date:', p.get('last_date'))
print('cash: ¥{:,.2f}'.format(p.get('cash', 0)))
print('positions:', p.get('positions'))
"

# 查看风控状态
cat data/risk_state.json
```

**判断标准：**
- `locked: false` → 可正常交易
- `locked: true` → **今日禁止一切买卖**，等待锁仓期结束（3 个交易日）
- `positions: {}` → 空仓，可建仓
- `positions` 不为空 → 持有仓位，监控是否触发卖出信号

### 2. （可选）运行选股模块查看盘前参考

选股模块已封存，不挂钩交易引擎。如需作为盘前参考，可手动运行：

```bash
cd /data/data/com.termux/files/home/finance-agent/scripts
python3 screener.py
```

**注意**：输出仅供参考，**不自动写入交易逻辑**。策略信号仍由 `strategy.py` 的确定性公式生成。

---

## 二、盘中（交易日 09:30–15:00）

**无需任何操作。**

本系统为 T+1 模拟盘，信号在**收盘后**生成，次日开盘撮合。盘中不干预、不下单、不修改参数。

---

## 三、盘后（交易日 15:30 后）

### 1. 更新行情数据

```bash
cd /data/data/com.termux/files/home/finance-agent
python3 scripts/fetch_data.py
```

### 2. 推进账户并生成复盘报告

```bash
cd /data/data/com.termux/files/home/finance-agent/scripts
python3 daily_report.py
```

**该命令会：**
- 从 `data/portfolio.json` 的 `last_date` 推进 1 个交易日
- 根据昨日收盘信号，于今日开盘价撮合买卖
- 收盘后估值、检查两条熔断规则
- 生成 `reports/<日期>.md` 每日复盘报告

### 3. 查看今日报告

```bash
ls reports/ | tail -5
cat reports/$(date +%F).md
```

**报告关注点：**
- 第 1 部分：今日模拟交易指令（买入/卖出/无操作）
- 第 3 部分：绩效看板（净值、回撤、胜率、夏普）
- 第 4 部分：风控状态（是否锁仓、熔断余量）
- 第 5 部分：首席风控官（CRO）分析

---

## 四、熔断处理流程

### 熔断触发条件（绝对规则，不可优化掉）

| 规则 | 触发条件 | 后果 |
|------|---------|------|
| R1 单日熔断 | 今日净值亏损 > 3% | 强制清仓 + 锁仓 3 个交易日 |
| R2 回撤熔断 | 净值自峰值回撤 > 10% | 强制清仓 + 锁仓 3 个交易日 |

### 熔断后处理步骤

1. **不要手动干预账本**  
   `daily_report.py` 会自动执行 `force_liquidate` 强平所有持仓，并写入 `risk_state.json` 的 `locked: true`。

2. **查看锁仓结束日期**  
   ```bash
   cat data/risk_state.json
   ```
   关注 `lock_from` 和 `lock_until_day`。锁仓期内禁止一切买卖。

3. **等待自动解锁**  
   `risk_control.note_day()` 会在每日收盘后检查，当交易日索引超过 `lock_until_day` 时自动将 `locked` 设为 `false`。无需人工操作。

4. **解锁后恢复正常**  
   锁仓期结束后，下一个交易日可继续按策略信号交易。

---

## 五、异常情况处理

| 情况 | 处理方式 |
|------|---------|
| `daily_report.py` 报错 "无历史状态" | 先运行 `python3 scripts/daily_report.py --reset` 重置，再运行 `--backtest` 初始化 |
| 行情数据未更新（fetch_data 失败） | 检查网络连接，稍后重新运行 `fetch_data.py` |
| LLM 分析降级为 rule 兜底 | 正常现象，表示 `GLM_API_KEY` 未设置，不影响交易逻辑 |
| 需要重新跑全历史回测 | `python3 scripts/daily_report.py --reset && python3 scripts/daily_report.py --backtest` |

---

## 六、禁止事项（铁律）

1. **绝对禁止接入实盘、券商账户或任何真实交易 API。** 本系统仅为模拟盘，初始资金 100 万元。
2. **LLM 只当"策略分析师"和"风控官"，绝不下单。** 买卖信号一律由 `scripts/strategy.py` 的确定性数学公式生成。
3. **任何脚本不得私改账本。** 交易指令由 `scripts/portfolio.py` 统一记账，其他模块不得修改 `data/portfolio.json`。
4. **熔断规则是绝对规则，optimizer 不得优化掉它。**
5. **所有行情、手续费、滑点参数必须能在 `index/params.yaml` 与 `index/watchlist.yaml` 中找到登记项。** 禁止凭空生成行情数字。

---

## 七、每日命令速查表

| 时机 | 命令 | 目的 |
|------|------|------|
| 盘前 09:00 前 | `cat data/risk_state.json` | 检查是否锁仓 |
| 盘前 09:00 前 | `python3 scripts/screener.py` | （可选）盘前选股参考 |
| 盘后 15:30 后 | `python3 scripts/fetch_data.py` | 更新行情 CSV |
| 盘后 15:30 后 | `python3 scripts/daily_report.py` | 推进账户 + 生成复盘报告 |
| 需要重置时 | `python3 scripts/daily_report.py --reset` | 清空账户与风控状态 |
| 需要回测时 | `python3 scripts/daily_report.py --backtest` | 全历史回测 |

---

> **生成时间**：2026-10-28  
> **系统版本**：T-Fin01 模拟盘 · 策略 A (MA 8/30) · 已毕业锁定  
> **声明**：本报告为模拟盘学习用途，不构成投资建议。
