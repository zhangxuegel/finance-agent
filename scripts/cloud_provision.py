# -*- coding: utf-8 -*-
"""cloud_provision.py —— Supabase 云端基建全自动接管（T-Fin09）

四步全自动（唯一不可能自动化的一步在最前面）：

  第 0 步 [唯一人工] 成 Personal Access Token：
      https://supabase.com/dashboard/account/keys
      → Generate New Token → 复制，然后三选一交付：
        ① export SUPABASE_ACCESS_TOKEN=...（推荐）
        ② echo "token" > ~/.supabase_token
        ③ python3 scripts/cloud_provision.py --token "..."
      命令把这步做好之后，本脚本不再需要任何人工。

  第 1 步 建组织（无 org 时自动建 finance-agent-org）
  第 2 步 建 project `finance-agent`，区域自动遴选（IP 地理定位 →
          最近 Supabase 区；Tokyo/Singapore 上海用户可达）；
          轮询状态至 ACTIVE（默认最长 25 分钟，-timeout 可调）
  第 3 步 执行 scripts/cloud_schema.sql 建表（幂等）+ 逐表 count 自检
  第 4 步 抓取 URL/anon key：写 .env（根目录，git 忽略脱敏）+
          data/CLOUD_KEYS.txt（备忘录）+ ~/.finance_agent_cloud（sync_all.sh 自动 source）

幂等：重复运行可续跑（org 已在→复用；项目同名已 ACTIVE→复用；
表已存在→ IF NOT EXISTS 跳过；台账文件重写无害）。

执行态纪律：只写云端两表 + 本地配置文件；绝不触碰撮合台账与队列。
"""
import argparse
import json
import math
import os
import secrets as _pysecrets
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

API = "https://api.supabase.com"
PROJECT_NAME = "finance-agent"
ORG_NAME = "finance-agent-org"
ENV_PATH = os.path.join(ROOT, ".env")
KEYS_TXT = os.path.join(ROOT, "data", "CLOUD_KEYS.txt")
HOME_CFG = os.path.join(os.path.expanduser("~"), ".finance_agent_cloud")
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".supabase_token")
SCHEMA = os.path.join(BASE, "cloud_schema.sql")

# Supabase 区位（code → 地理坐标），选最近区须先知道用户在哪
REGIONS = [
    ("ap-northeast-1", "Tokyo", 35.68, 139.69),
    ("ap-southeast-1", "Singapore", 1.35, 103.82),
    ("ap-southeast-2", "Sydney", -33.87, 151.21),
    ("ap-south-1", "Mumbai", 19.08, 72.88),
    ("eu-central-1", "Frankfurt", 50.11, 8.68),
    ("eu-west-1", "Ireland", 53.35, -6.26),
    ("us-east-1", "Virginia", 38.90, -77.04),
    ("sa-east-1", "São Paulo", -23.55, -46.63),
]


def log(msg: str):
    print(f"[provision] {datetime.now():%H:%M:%S} {msg}", flush=True)


def get_token(cli_token: str = "") -> str:
    tok = (cli_token or os.environ.get("SUPABASE_ACCESS_TOKEN", "")
           or _read_file(TOKEN_FILE)).strip()
    if not tok:
        print(
            "⛔ 暂停：需要人工授权一次（任务书预留的那个动作）。\n"
            "   唯一步骤：浏览器登录 https://supabase.com/dashboard/account/keys\n"
            "   → Personal Access Tokens → Generate new token → 复制。\n"
            "   然后三选一交付（之后全自动，无需再人工）：\n"
            "     export SUPABASE_ACCESS_TOKEN=eyJ...\n"
            "     echo \"eyJ...\" > ~/.supabase_token\n"
            "     python3 scripts/cloud_provision.py --token \"eyJ...\"")
        sys.exit(2)
    return tok


def _read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def client(token: str):
    import requests
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}",
                      "User-Agent": "finance-agent-provision/1.0"})
    return s


def api(s, method: str, path: str, **kw):
    import requests
    r = s.request(method, API + path, timeout=(5, 30), **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} → HTTP {r.status_code} "
                           f"{r.text[:300]}")
    return r.json() if r.text else {}


def nearest_region() -> str:
    """IP 地理定位 → 距离最近 Supabase 区。失败兜底 Singapore。"""
    lat, lon = None, None
    try:
        import requests
        ip = requests.get("https://ipinfo.io/json", timeout=(5, 10)).json()
        loc = (ip.get("loc") or "").split(",")
        if len(loc) == 2:
            lat, lon = float(loc[0]), float(loc[1])
        log(f"设备位置 {ip.get('country', '?')} ({lat}, {lon})")
    except Exception as e:  # noqa: BLE001
        log(f"IP 定位失败（{e}）→ 兜底 Singapore")
    if lat is None:
        return "ap-southeast-1"
    best = min(REGIONS, key=lambda r: math.dist((lat, lon), (r[2], r[3])))
    log(f"最近 Supabase 区 → {best[1]} ({best[0]})")
    return best[0]


def ensure_org(s) -> str:
    orgs = api(s, "GET", "/v1/organizations")
    for o in orgs:
        if o.get("name") == ORG_NAME:
            log(f"复用已有 org: {o['id']}")
            return o["id"]
    org = api(s, "POST", "/v1/organizations",
              json={"name": ORG_NAME})
    log(f"已创建 org: {org['id']}")
    return org["id"]


def ensure_project(s, org_id: str, region: str, db_pass: str,
                   wait_sec: int) -> str:
    projects = api(s, "GET", "/v1/projects")
    for pr in projects:
        if pr.get("name") == PROJECT_NAME and pr.get("organization_id") == org_id:
            ref = pr["id"]
            st = pr.get("status")
            log(f"发现已有 project {ref}（status={st}）→ 复用")
            if st == "ACTIVE_HEALTHY":
                return ref
            break
    else:
        log(f"创建 project '{PROJECT_NAME}' @ {region} …（云端开库，数分钟）")
        pr = api(s, "POST", "/v1/projects",
                 json={"name": PROJECT_NAME, "organization_id": org_id,
                       "region": region, "db_pass": db_pass,
                       "postgres_version": "15"})
        ref = pr["id"]
        log(f"project {ref} 已受理，开始轮询…")
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        pr = api(s, "GET", f"/v1/projects/{ref}")
        st = pr.get("status")
        log(f"状态 = {st}")
        if st == "ACTIVE_HEALTHY":
            return ref
        if st == "PAUSED":
            log("项目处于暂停 → 尝试 RESTORE")
            api(s, "POST", f"/v1/projects/{ref}/restore")
        time.sleep(20)
    raise RuntimeError(f"初始化超时（>{wait_sec}s）——重跑本脚本可续轮询")


def exec_sql(s, ref: str, sql: str) -> list:
    """Management API SQL 直通；4xx 时提示手工路径。"""
    try:
        res = api(s, "POST", f"/v1/projects/{ref}/database/query",
                  json={"query": sql})
        return res if isinstance(res, list) else []
    except RuntimeError as e:
        raise RuntimeError(
            f"建表 SQL 执行失败：{e}\n"
            f"兜底手路径：Supabase Dashboard → SQL Editor → 粘贴 "
            f"scripts/cloud_schema.sql → Run")


def ensure_tables(s, ref: str):
    sql = _read_file(SCHEMA)
    if not sql:
        raise RuntimeError(f"建表脚本缺失：{SCHEMA}")
    log("执行 cloud_schema.sql（幂等 IF NOT EXISTS）…")
    exec_sql(s, ref, sql)
    counts = []
    for t in ("sb_shadow_eod", "sb_shadow_fills"):
        res = exec_sql(s, ref,
                       f"SELECT COUNT(*)::int AS n FROM {t}")
        n = res[0]["n"] if res and isinstance(res, list) else "?"
        counts.append(f"{t}={n}")
    log(f"建表自检通过（两表在库）：{', '.join(counts)}")


def fetch_keys(s, ref: str) -> dict:
    """Management API 取 API keys（anon 优先）。"""
    try:
        keys = api(s, "GET", f"/v1/projects/{ref}/api-keys")
    except RuntimeError:
        keys = []
    if keys and isinstance(keys, list):
        for k in keys:
            if k.get("name") == "anon":
                return {"ref": ref, "anon": k.get("api_key", "")}
        ka = keys[-1] if keys else {}
        return {"ref": ref, "anon": ka.get("api_key", "")}
    return {"ref": ref, "anon": ""}


def write_config(ref: str, url: str, anon: str, region: str):
    """三处落配置：.env / data/CLOUD_KEYS.txt / ~/.finance_agent_cloud。"""
    stamp = f"{datetime.now():%F %T}"
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(f"# 自动生成 {stamp}（T-Fin09 cloud_provision；勿提交 git）\n"
                f"SUPABASE_URL={url}\n"
                f"SUPABASE_KEY={anon}\n")
    os.makedirs(os.path.dirname(KEYS_TXT), exist_ok=True)
    with open(KEYS_TXT, "w", encoding="utf-8") as f:
        f.write(f"# T-Fin09 云端账条备忘录 · {stamp}\n"
                f"# Streamlit Cloud 部署时（share.streamlit.io → Settings → Secrets）"
                f"复制以下两行填入 [supabase]：\n\n"
                f"Project URL: {url}\n"
                f"anon key:    {anon or '（复制失败→Dashboard→Settings→API 手取）'}\n\n"
                f"region={region}  project_ref={ref}\n"
                f"提醒：本文件含密钥，勿提交 git（.gitignore 已配置）。\n")
    with open(HOME_CFG, "w", encoding="utf-8") as f:
        f.write(f"export SUPABASE_URL={url}\nexport SUPABASE_KEY={anon}\n")
    log(f"配置注入 → {ENV_PATH} / {KEYS_TXT} / {HOME_CFG}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="")
    ap.add_argument("--timeout", type=int, default=1500,
                    help="项目就绪轮询上限秒数（默认 1500s）")
    a = ap.parse_args(argv)
    tok = get_token(a.token)
    s = client(tok)
    org = ensure_org(s)
    region = nearest_region()
    db_pass = _pysecrets.token_urlsafe(24)   # 仅本次创建用（无需回头记录）
    ref = ensure_project(s, org, region, db_pass, a.timeout)
    ensure_tables(s, ref)
    url = f"https://{ref}.supabase.co"
    keys = fetch_keys(s, ref)
    write_config(ref, url, keys["anon"], region)
    log("✅ 全自动链路完成 → 直接 bash sync_all.sh 推数据；"
        "CLOUD_KEYS.txt 供 Streamlit Cloud 部署使用。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        log(f"❌ {e}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        log(f"❌ 未预期 {type(e).__name__}: {e}")
        sys.exit(1)
