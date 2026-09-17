#!/usr/bin/env python3
"""额度解析的回归测试。

app（`codex-cost --dump --json`）和 CLI（`codex_budget.py --json`）各跑一遍
tests/fixtures.py 生成的固定样本：既核对每个场景的期望值，也核对两边互相一致
—— 两份实现各写一遍解析逻辑，最容易在这里悄悄分叉。

    ./build.sh && python3 tests/test_quota.py

零依赖，标准库。样本写在临时目录，不碰你的 ~/.codex。
"""
from __future__ import annotations
import glob, json, os, subprocess, sys, tempfile, time, unittest

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
        "context": (d.get("last_request") or {}).get("context"),
        "resume_pct": (d.get("last_request") or {}).get("resume_pct"),
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
            for key in ("context",):
                if key in exp:
                    self.assertEqual(got[key], exp[key], f"{name} · {side} · {key}")
        if self.app[name]["resume_pct"] is not None or self.cli[name]["resume_pct"] is not None:
            self.assertAlmostEqual(self.app[name]["resume_pct"], self.cli[name]["resume_pct"], places=6,
                                   msg=f"{name} · app 与 CLI 的每轮开销不一致")
        self.assertEqual(self.app[name]["binding"], exp["binding"], f"{name} · app · binding")
        self.assertAlmostEqual(self.app[name]["spent"], self.cli[name]["spent"], places=6,
                               msg=f"{name} · app 与 CLI 的估算不一致")
    test.__doc__ = (fixtures.SCENARIOS[name].__doc__ or "").strip().splitlines()[0]
    return test


for _name in fixtures.SCENARIOS:
    setattr(Quota, f"test_{_name}", scenario_test(_name))


class Incremental(unittest.TestCase):
    """增量解析：app 常驻时每次刷新只解析新增的字节（Budget.FileState）。

    这条路一旦漏算，面板会悄悄少报用量而没人发现 —— 所以这里跑一次真的增量：
    先让 app 在同一个进程里扫一遍，往日志追加几次请求，再让它扫第二遍，
    结果必须和「整份重读」（新起一个进程）完全一致。
    """

    def setUp(self):
        if not os.path.exists(APP):
            raise RuntimeError("找不到 ./codex-cost，先跑 ./build.sh")
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-cost-incremental-")
        self.now = time.time()
        fixtures.build(self.tmp.name, now=self.now)
        self.home = os.path.join(self.tmp.name, "normal")

    def tearDown(self):
        self.tmp.cleanup()

    def newest_rollout(self):
        files = glob.glob(os.path.join(self.home, "sessions", "**", "rollout-*.jsonl"),
                          recursive=True)
        return max(files, key=os.path.getmtime)

    def append_requests(self, n=4):
        """按 fixtures 的形状往最近的会话里追加几次请求，额度读数跟着涨"""
        path = self.newest_rollout()
        lines = []
        for i in range(n):
            t = self.now - 60 + i * 10
            lim = {"primary": fixtures.window(300, 13 + i, self.now + 2.2 * fixtures.H),
                   "secondary": fixtures.window(10080, 42, self.now + 3.4 * 24 * fixtures.H)}
            lines.append(json.dumps({
                "timestamp": fixtures.iso(t),
                "type": "event_msg",
                "payload": {"type": "token_count",
                            "info": {"last_token_usage": fixtures.usage(i, cached=30_000)},
                            "rate_limits": {"limit_id": "codex", **lim}}}))
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def test_append_between_scans(self):
        env = dict(os.environ, CODEX_HOME=self.home)
        p = subprocess.Popen([APP, "--dump", "--json", "--recompute", "2"], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn("recomputed", p.stderr.readline(), "app 没报告第一次扫描完成")
            self.append_requests()
            p.stdin.write("\n")
            p.stdin.flush()
            out, err = p.communicate(timeout=120)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, err)
        incremental = view(json.loads(out), "spent")
        full = view(run([APP, "--dump", "--json"], self.home), "spent")
        self.assertEqual(incremental["requests"], full["requests"], "增量扫描漏算了请求")
        self.assertEqual(incremental["models"], full["models"], "增量扫描的模型归属不一致")
        self.assertEqual(incremental["five_hour"], full["five_hour"], "增量扫描没读到最新的额度读数")
        self.assertAlmostEqual(incremental["spent"], full["spent"], places=6,
                               msg="增量扫描与整份重读的估算不一致")
        self.assertEqual(incremental["context"], full["context"], "增量扫描的最近一次上下文不一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
