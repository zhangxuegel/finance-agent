# README_DEPLOY.md —— 云端看板保姆级部署（T-Fin07）

> 目标：手机没电/无网/弱网时，用任意浏览器打开云端看板查看影子实盘净值。
> 链路：**本地确定性引擎算账 → sync_all.sh 增量推送 → Supabase 云库
> → Streamlit Cloud 看板只读**。本地与云端完全解耦，云端全程只读，
> 不碰撮合、不碰券商、不碰真实资金（AGENTS.md 铁律不变）。

预估用时 15 分钟，全部免费额度可承载。

---

## 第 0 步 · 前置认知

| 角色 | 位置 | 说明 |
|---|---|---|
| 算账引擎 | 你的手机/电脑（本地） | 唯一有权写交易的进程，永不接触云端 API 写库之外的任何东西 |
| 同步器 | `scripts/sync_to_cloud.py` | 只推本地新增的 eod/fills 行（水位断点续传） |
| 云数据库 | Supabase（免费 2 项目） | 两张表：`sb_shadow_eod`、`sb_shadow_fills` |
| 云看板 | Streamlit Cloud | 跑 `scripts/dashboard_cloud.py`，只读 |

---

## 第 1 步 · 开通 Supabase 并建表

1. 注册 https://supabase.com → **New project**（随便起名，如 `qsim-ledger`）。
2. 左侧 **SQL Editor** → New query → 粘贴执行下面整段：

```sql
-- 影子净值/Alpha 日线（主键已在 local sqlite 唯一）
CREATE TABLE IF NOT EXISTS sb_shadow_eod (
  trade_date        text PRIMARY KEY,
  nav               numeric,
  nav_prev          numeric,
  day_ret           numeric,
  bench_index_close numeric,
  bench_index_pct   numeric,
  alpha             numeric,
  defense           integer,
  fills_n           integer,
  lock_remaining    integer,
  notes             text
);

-- 撮合流水（id 与本地自增同名，on_conflict=id 幂等）
CREATE TABLE IF NOT EXISTS sb_shadow_fills (
  id             bigint PRIMARY KEY,
  ts             text,
  trade_date     text,
  leg            text,
  code           text,
  side           text,
  shares         bigint,
  price          numeric,
  fee            numeric,
  reason         text,
  limit_blocked  integer
);
```

3. 左下 **Project Settings → API** 抄两个值：
   - `Project URL` → 这就是 **SUPABASE_URL**
   - `anon public` key → 这就是 **SUPABASE_KEY**
   （想保险就用 service key——都保留 RLS 默认策略，本项目只 upsert 两张表。）

---

## 第 2 步 · 本地跑通验证（Termux）

```bash
export SUPABASE_URL="https://你的项目ID.supabase.co"
export SUPABASE_KEY="你的key"
python3 scripts/sync_to_cloud.py --test     # 应打印"云端连通 ✅"
python3 scripts/sync_to_cloud.py --push     # 首推（把已有 eod 行推上去）
python3 scripts/dashboard_cloud.py --fetch-test   # 读回验证
```

Supabase 面板 Table Manager 里应能看到 `sb_shadow_eod` 有数据行。

---

## 第 3 步 · 部署 Streamlit Cloud

**方式 0（最快，T-Fin09）：全自动接管**

```bash
python3 scripts/cloud_provision.py   # 提示你给 1 枚 Personal Access Token（唯一人工步）
bash sync_all.sh                     # 数据上去
python3 scripts/test_cloud_connection.py
```

脚本自动：建 org → 建 project `finance-agent`（IP 地理定位选最近区）→ 轮询 ACTIVE
→ 执行 `scripts/cloud_schema.sql` 建表 → 抓 URL/anon key 注入 `.env` +
`data/CLOUD_KEYS.txt`（备忘录，供下面 Secrets 使用）。以下“方式 A”的人工步骤
你已无需再做——只需跳到第 3 步填 secrets（从 data/CLOUD_KEYS.txt 复制）。

**方式 A（推荐）：GitHub 拉取法**

1. 在手机/任意设备把整个 `finance-agent` 目录推/上传到 GitHub（新建 repo，
   名字随意，Public/Private 均可）。
2. https://share.streamlit.io → 用 GitHub 登录 → **Create app**：
   - Repository：选你的 repo
   - Main file path：`scripts/dashboard_cloud.py`
   - Deploy。
3. 部署后 **Manage app → Settings → Secrets**，粘贴：

```toml
[supabase]
url = "https://你的项目ID.supabase.co"
key = "你的key"
```

4. 刷新页面 → 看板上线。把 `share.streamlit.io/xxx` 的链接发给自己随时看。

**方式 B：本地 zip/拖拽**

Streamlit Cloud 要求代码在 GitHub——若不便 git，先把目录上传到任意
GitHub 新仓库（网页端 Add file → Upload files 拖拽即可，Android 用
[github 网页上传](https://github.com/new) 不需要 git 命令）。

---

## 第 4 步 · 日常一键同步（T-Fin07 模块 4）

本地每日收盘流程跑完后（run_daily_all.sh → 15:05 落库 → …）：

```bash
bash sync_all.sh          # 增量推送（水位断点续传，重复运行自动跳过）
```

想全自动（可选，加进 crontab）：

```cron
40 15 * * 1-5  bash ~/finance-agent/sync_all.sh >> ~/finance-agent/data/cron.log 2>&1
```

---

## 常见问题

| 现象 | 处置 |
|---|---|
| `未配置云端 API` | SUPABASE_URL/KEY 没设置；或 Streamlit secrets 没填 |
| `云端写入失败 HTTP 401/403` | key 抄错——回 Project Settings→API 重抄 anon key 最保险 |
| 看板 404 "no tables" | 第 1 步 SQL 没执行成功；回 Supabase SQL Editor 重跑 |
| 推了但看板没更新 | eod 只在 15:05 落库后产生；看板右上角 Rerun/刷新 |
| 断网/云端故障 | 本地照常运转（点火键都不用动）；恢复后再 --push 补传 |

## 安全红线（重申）

- 云端看板**只读**两张台账表——买不了股票、动不了本地撮合；
- 铁律不变：模拟资金 100 万、LLM 零下单权、数字可溯源；
- `secrets.toml` 含 key，**不要提交 git**（.gitignore 已配置）。
