#!/usr/bin/env python3
"""历史用量总账的回归测试：Codex、Claude Code、opencode 三个来源的解析、去重和索引。

    ./build.sh && python3 tests/test_usage.py

样本全部写在临时目录，通过 CODEX_HOME / CLAUDE_CONFIG_DIR / XDG_DATA_HOME /
CODEX_COST_DATA_DIR 指过去，不碰你真实的日志和索引。
"""
from __future__ import annotations
import json, os, shutil, sqlite3, subprocess, tempfile, unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "codex-cost")


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def jl(path, rows, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def codex_event(ts, inp, cached, out, reas=0):
    return {"timestamp": iso(ts), "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": {"limit_id": "codex"},
                        "info": {"last_token_usage": {
                            "input_tokens": inp, "cached_input_tokens": cached,
                            "output_tokens": out, "reasoning_output_tokens": reas}}}}


def claude_line(ts, mid, inp, cw, cr, out, model="claude-opus-5"):
    return {"timestamp": iso(ts), "type": "assistant", "sessionId": "s",
            "message": {"id": mid, "model": model, "role": "assistant",
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cw,
                                  "cache_read_input_tokens": cr, "output_tokens": out}}}


@unittest.skipUnless(os.path.exists(APP), "先跑 ./build.sh")
class UsageLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="codex-cost-usage-")
        j = lambda *p: os.path.join(self.tmp, *p)
        self.env = dict(os.environ, CODEX_HOME=j("codex"), CLAUDE_CONFIG_DIR=j("claude"),
                        XDG_DATA_HOME=j("xdg"), CODEX_COST_DATA_DIR=j("data"))
        self.now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        self.old = self.now - timedelta(days=10)

        # Codex：今天 3 次请求 + 10 天前 1 次；一条空 info（不是请求）；归档目录里复制了 2 条
        self.rollout = j("codex", "sessions", "2026", "09", "15", "rollout-a.jsonl")
        self.cx = [(self.now, 10_000, 8_000, 100, 50), (self.now + timedelta(minutes=1), 12_000, 9_000, 200, 0),
                   (self.now + timedelta(minutes=2), 5_000, 0, 30, 10), (self.old, 7_000, 1_000, 40, 0)]
        rows = [{"timestamp": iso(self.old), "type": "turn_context", "payload": {"model": "gpt-5.6-sol"}}]
        rows += [codex_event(*e) for e in self.cx]
        rows.append({"timestamp": iso(self.now), "type": "event_msg",
                     "payload": {"type": "token_count", "info": None, "rate_limits": {"limit_id": "codex"}}})
        jl(self.rollout, rows)
        jl(j("codex", "archived_sessions", "rollout-a-copy.jsonl"),
           [rows[0]] + [codex_event(*e) for e in self.cx[:2]])

        # Claude Code：m1 在文件内写两遍、又被复制到另一个文件；m2 输出递增；m3 是 10 天前；
        # 另有一条 <synthetic>（不计）
        self.claude_b = j("claude", "projects", "p2", "b.jsonl")
        jl(j("claude", "projects", "p1", "a.jsonl"), [
            claude_line(self.now, "m1", 5, 100, 1_000, 20),
            claude_line(self.now, "m1", 5, 100, 1_000, 20),
            claude_line(self.now, "m2", 3, 0, 2_000, 10),
            claude_line(self.now, "m2", 3, 0, 2_000, 40),
            claude_line(self.now, "syn", 0, 0, 0, 0, model="<synthetic>"),
        ])
        jl(self.claude_b, [
            claude_line(self.now, "m1", 5, 100, 1_000, 20),
            claude_line(self.old, "m3", 7, 50, 500, 15),
        ])

        # opencode：两条有用量的助手消息、一条用户消息、一条用量为 0 的助手消息
        db = j("xdg", "opencode", "opencode.db")
        os.makedirs(os.path.dirname(db))
        con = sqlite3.connect(db)
        con.execute("create table message (id text, session_id text, time_created integer, "
                    "time_updated integer, data text)")
        ms = lambda d: int(d.timestamp() * 1000)
        for mid, role, tok, cost, when in (
                ("o1", "assistant", {"input": 900, "output": 60, "reasoning": 5, "cache": {"read": 4000, "write": 0}}, 0.12, self.now),
                ("o2", "assistant", {"input": 100, "output": 10, "reasoning": 0, "cache": {"read": 0, "write": 300}}, 0.03, self.now),
                ("o3", "user", {}, 0, self.now),
                ("o4", "assistant", {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}, 0, self.now)):
            con.execute("insert into message values (?, 's', ?, ?, ?)",
                        (mid, ms(when), ms(when), json.dumps({"role": role, "tokens": tok, "cost": cost,
                                                              "modelID": "glm-5.3-flash", "time": {"created": ms(when)}})))
        con.commit(); con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dump(self, period="all"):
        p = subprocess.run([APP, "--usage-json", period], env=self.env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        return {t["tool"]: t for t in d["tools"]}, d

    def test_codex_dedupes_archive_copies_and_ignores_empty_info(self):
        tools, _ = self.dump()
        c = tools["codex"]
        self.assertEqual(c["requests"], 4)
        self.assertEqual(c["input"], sum(max(0, i - k) for _, i, k, _, _ in self.cx))
        self.assertEqual(c["cacheRead"], sum(k for _, _, k, _, _ in self.cx))
        self.assertEqual(c["output"], sum(o + r for _, _, _, o, r in self.cx))
        self.assertGreater(c["quotaPct"], 0)

    def test_claude_dedupes_by_message_id_keeping_largest_output(self):
        tools, _ = self.dump()
        c = tools["claudeCode"]
        self.assertEqual(c["requests"], 3)                 # m1、m2、m3；<synthetic> 不计
        self.assertEqual(c["output"], 20 + 40 + 15)        # m2 取递增后的 40
        self.assertEqual(c["cacheRead"], 1_000 + 2_000 + 500)
        self.assertEqual(c["cacheWrite"], 100 + 0 + 50)
        self.assertEqual(c["quotaPct"], 0)                 # Claude Code 没有额度系数

    def test_opencode_reads_assistant_messages_with_cost(self):
        tools, _ = self.dump()
        o = tools["opencode"]
        self.assertEqual(o["requests"], 2)
        self.assertEqual(o["output"], 60 + 5 + 10)
        self.assertEqual(o["cacheWrite"], 300)
        self.assertAlmostEqual(o["cost"], 0.15, places=6)

    def test_week_excludes_older_records(self):
        tools, d = self.dump("week")
        self.assertEqual(tools["codex"]["requests"], 3)
        self.assertEqual(tools["claudeCode"]["requests"], 2)
        self.assertEqual(len(d["daily"]), 7)
        self.assertEqual(d["first_day"], self.old.strftime("%Y-%m-%d"))

    def test_index_keeps_history_after_source_file_is_deleted(self):
        before, _ = self.dump()
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "data", "usage-index.json")))
        os.remove(self.claude_b)                           # 模拟 Claude Code 清理旧会话
        after, _ = self.dump()
        self.assertEqual(after["claudeCode"], before["claudeCode"])

    def test_appended_events_are_picked_up(self):
        self.dump()
        jl(self.rollout, [codex_event(self.now + timedelta(minutes=5), 3_000, 0, 20)], mode="a")
        tools, _ = self.dump()
        self.assertEqual(tools["codex"]["requests"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
