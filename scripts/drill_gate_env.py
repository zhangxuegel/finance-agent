# -*- coding: utf-8 -*-
"""drill_gate_env.py —— T-Fin37 沙演：GA runner 凭证注入通道 + 闸0/闸2 行为矩阵

背景（run 40341024519 三连败根因）：load_cfg 旧版只读凭证文件不读 OS 环境变量，
GA runner 恒空 dict → 闸0 cloud_rows 静默回空被误判"手机缺席"假接管、
闸2 --mobile 判凭证缺失 exit 3。

本演练**完全模拟 GA 场景**：HOME 覆写（无 ~/.finance_agent_cloud）、
项目根无 .env、凭证只经 os.environ 注入（等同 GITHUB_ENV 语义），
目标端为 drill_fake_supabase.py 起的 PostgREST 子集模拟器（零真实触碰）。

用例矩阵（期望）：
  1 无凭证 + --cloud-runner      → exit 3「拒绝接管」（旧版=0 假接管，已根除）
  2 env凭证 + 云端有今日 eod     → --cloud-runner → exit 9（手机先手 SKIP）
  3 env凭证 + 云端无今日 eod     → --cloud-runner → exit 0（允许接管代跑）
  4 env凭证 + 云端=本地全集      → --mobile --dry → exit 0（对账统一放行）
  5 空凭证 + --mobile            → exit 3（凭证缺失护栏仍完好）
零账本写入：对账一律 --dry（recon fix=False 只读）；落库用例由 --selftest 覆盖。

用法：python3 scripts/drill_gate_env.py   （自起自停，无需外部端口）
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)

PORT = 8993
URL = f"http://127.0.0.1:{PORT}"
TODAY = __import__("datetime").date.today().strftime("%F")

FAKE_SUPABASE = os.path.join(BASE, "drill_fake_supabase.py")


def local_eod_rows() -> list[dict]:
    conn = sqlite3.connect(os.path.join(ROOT, "data", "shadow_live.db"))
    cols = [c[1] for c in conn.execute("PRAGMA table_info(eod)")]
    return [dict(zip(cols, r)) for r in
            conn.execute("SELECT * FROM eod ORDER BY trade_date ASC")]


def wait_alive(timeout: float = 15.0) -> None:
    import urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{URL}/rest/v1/sb_shadow_eod?select=*",
                                   timeout=2).read()
            return
        except urllib.error.HTTPError:
            return              # 有响应即活（500=state 未播种，后续 go() 会补）
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("fake supabase 起不来")


def run_gate(argv: list[str], env_extra: dict) -> tuple[int, str]:
    env = dict(os.environ)
    env.pop("SUPABASE_URL", None)
    env.pop("SUPABASE_KEY", None)
    env.update(env_extra)
    p = subprocess.run(
        [sys.executable, os.path.join(BASE, "cloud_gate.py")] + argv,
        capture_output=True, text=True, env=env, timeout=120, cwd=ROOT)
    out = (p.stdout + p.stderr).strip()
    return p.returncode, out


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="drill_gate_env_")
    state = os.path.join(tmp, "cloud_state.json")
    fake_home = os.path.join(tmp, "home")       # 无 ~/.finance_agent_cloud
    os.makedirs(fake_home)
    results: list[tuple[str, bool, str]] = []
    server = None
    try:
        rows = local_eod_rows()
        today_row = dict(rows[-1], trade_date=TODAY, notes='{"drill": 1}')
        # 先播种再起服（模拟器 _load 要求 state 文件存在）
        json.dump({"sb_shadow_eod": rows, "sb_shadow_fills": []},
                  open(state, "w", encoding="utf-8"))
        server = subprocess.Popen(
            [sys.executable, FAKE_SUPABASE, state, str(PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_alive()
        env_creds = {"SUPABASE_URL": URL, "SUPABASE_KEY": "drill-key",
                     "HOME": fake_home}
        env_clean = {"HOME": fake_home}

        def go(name, argv, seeded, env_use, expect, needle):
            json.dump({"sb_shadow_eod": seeded, "sb_shadow_fills": []},
                      open(state, "w", encoding="utf-8"))
            code, out = run_gate(argv, env_use)
            ok = (code == expect and needle in out)
            results.append((name, ok, f"exit={code}（期望 {expect}）· {needle!r}"
                                       f"{'在' if needle in out else '不在'}输出"))
            return code, out

        # 1 无凭证 + --cloud-runner → 拒绝接管（响亮失败，非静默假接管）
        go("1 无凭证闸0拒绝接管", ["--cloud-runner"], rows,
           env_clean, 3, "拒绝接管")
        # 2 env 凭证 + 云端已有今日 eod → 手机先手 SKIP
        go("2 云端有今日eod→SKIP", ["--cloud-runner"], rows + [today_row],
           env_creds, 9, "已由手机落库")
        # 3 env 凭证 + 云端无今日 eod → 允许接管（exit 0）
        go("3 云端无今日eod→接管放行", ["--cloud-runner"], rows,
           env_creds, 0, "云端无 eod 行")
        # 4 env 凭证 + 云端=本地全集 → --mobile --dry 对账统一放行
        go("4 env凭证mobile闸统一", ["--mobile", "--dry"], rows,
           env_creds, 0, "统一")
        # 5 空凭证 + --mobile → 凭证缺失护栏（exit 3）仍完好
        go("5 mobile空凭证护栏", ["--mobile"], [],
           env_clean, 3, "云端凭证缺失")
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 58)
    bad = 0
    for name, ok, note in results:
        print(f"{'✅' if ok else '❌'} {name}：{note}")
        bad += 0 if ok else 1
    print(f"drill_gate_env: {'全绿' if not bad else f'{bad} 败'}"
          f" → {len(results) - bad}/{len(results)} 项")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
