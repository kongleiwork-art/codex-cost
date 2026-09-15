#!/usr/bin/env python3
"""额度解析的回归测试。

app（`codex-cost --dump --json`）和 CLI（`codex_budget.py --json`）各跑一遍
tests/fixtures.py 生成的固定样本：既核对每个场景的期望值，也核对两边互相一致
—— 两份实现各写一遍解析逻辑，最容易在这里悄悄分叉。

    ./build.sh && python3 tests/test_quota.py

零依赖，标准库。样本写在临时目录，不碰你的 ~/.codex。
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fixtures  # noqa: E402

APP = os.path.join(ROOT, "codex-cost")
CLI = os.path.join(ROOT, "cli", "codex_budget.py")


def run(cmd, home):
    env = dict(os.environ, CODEX_HOME=home)
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} 退出码 {p.returncode}\n{p.stderr}")
    return json.loads(p.stdout)


def used(w):
    return None if w is None else w["used_percent"]


def view(d, spent_key):
    """把两边的 JSON 收成同一个形状"""
    q = d.get("quota") or {}
    return {
        "requests": d["requests"],
        "five_hour": used(q.get("300")),
        "five_hour_stale": bool((q.get("300") or {}).get("stale")),
        "weekly": used(q.get("10080")),
        "pools": [{"label": p["label"], "used": used(p["window"]), "requests": p["requests"]}
                  for p in d.get("pools") or []],
        "current_model": d.get("current_model"),
        "models": {m: v["requests"] for m, v in (d.get("by_model") or {}).items()},
        "spent": d.get(spent_key, 0.0),
        "binding": d.get("binding"),
        "cache_hint": (d.get("last_request") or {}).get("cache_hint"),
        "context": (d.get("last_request") or {}).get("context"),
        "resume_miss": (d.get("last_request") or {}).get("resume_miss_pct"),
    }


class Quota(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(APP):
            raise RuntimeError("找不到 ./codex-cost，先跑 ./build.sh")
        cls.tmp = tempfile.TemporaryDirectory(prefix="codex-cost-fixtures-")
        cls.expect = fixtures.build(cls.tmp.name)
        cls.app, cls.cli = {}, {}
        for name in cls.expect:
            home = os.path.join(cls.tmp.name, name)
            cls.app[name] = view(run([APP, "--dump", "--json"], home), "spent")
            cls.cli[name] = view(run([sys.executable, CLI, "--json"], home), "spent_pct")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


def scenario_test(name):
    def test(self):
        exp = self.expect[name]
        for side, got in (("app", self.app[name]), ("cli", self.cli[name])):
            for key in ("requests", "five_hour", "weekly", "pools", "current_model", "models"):
                self.assertEqual(got[key], exp[key], f"{name} · {side} · {key}")
            if exp.get("five_hour_stale"):
                self.assertTrue(got["five_hour_stale"], f"{name} · {side} · 5 小时窗口应标记为已重置")
            for key in ("cache_hint", "context"):
                if key in exp:
                    self.assertEqual(got[key], exp[key], f"{name} · {side} · {key}")
        if self.app[name]["resume_miss"] is not None or self.cli[name]["resume_miss"] is not None:
            self.assertAlmostEqual(self.app[name]["resume_miss"], self.cli[name]["resume_miss"], places=6,
                                   msg=f"{name} · app 与 CLI 的重读代价不一致")
        self.assertEqual(self.app[name]["binding"], exp["binding"], f"{name} · app · binding")
        self.assertAlmostEqual(self.app[name]["spent"], self.cli[name]["spent"], places=6,
                               msg=f"{name} · app 与 CLI 的估算不一致")
    test.__doc__ = (fixtures.SCENARIOS[name].__doc__ or "").strip().splitlines()[0]
    return test


for _name in fixtures.SCENARIOS:
    setattr(Quota, f"test_{_name}", scenario_test(_name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
