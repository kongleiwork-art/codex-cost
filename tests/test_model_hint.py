#!/usr/bin/env python3
"""贵模型提醒钩子（cli/codex_model_hint.py）的测试。

钩子跑在你每一条消息前面，所以这里盯三件事：该说话时说话、该闭嘴时闭嘴、
以及**任何情况下都不许挡住消息**（坏输入、文件不存在、日志格式变了）。

    python3 tests/test_model_hint.py

零依赖，标准库。样本写在临时目录，不碰你的 ~/.codex。
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fixtures  # noqa: E402

HOOK = os.path.join(ROOT, "cli", "codex_model_hint.py")


def run(payload, *args, env=None):
    p = subprocess.run([sys.executable, HOOK, *args], input=payload, text=True,
                       capture_output=True, timeout=60,
                       env=dict(os.environ, **(env or {})))
    return p


def transcript(tmp, model, context, requests_per_turn=5, turns=3):
    """写一份最小的 rollout：每轮 requests_per_turn 次请求，上下文固定"""
    now = time.time()
    s = fixtures.Session(tmp, now - 3600, model)
    for t in range(turns):
        s.add(now - 3000 + t * 300, {"type": "task_started", "payload": {"type": "task_started"}})
        for i in range(requests_per_turn):
            s.tokens(now - 3000 + t * 300 + i,
                     {"input_tokens": context, "cached_input_tokens": context - 2_000,
                      "output_tokens": 200, "reasoning_output_tokens": 100},
                     {"primary": fixtures.window(300, 20, now + 7200)})
    s.close()
    return s.path


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-cost-hint-")

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, model, path, prompt="改一下这个函数"):
        return json.dumps({"cwd": self.tmp.name, "hook_event_name": "UserPromptSubmit",
                           "model": model, "permission_mode": "default", "prompt": prompt,
                           "session_id": "s", "transcript_path": path, "turn_id": "t"})

    def test_expensive_model_speaks(self):
        """在 astra 上、上下文够大：提示成本差"""
        path = transcript(self.tmp.name, "gpt-6-astra", 180_000)
        p = run(self.payload("gpt-6-astra", path))
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertIn("systemMessage", out)
        self.assertIn("astra", out["systemMessage"])
        self.assertIn("5.6-sol", out["systemMessage"])
        self.assertNotIn("decision", out, "默认不该拦消息")

    def test_reference_model_silent(self):
        """已经在参照模型上：一个字都不说"""
        path = transcript(self.tmp.name, "gpt-5.6-sol", 180_000)
        p = run(self.payload("gpt-5.6-sol", path))
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))

    def test_small_context_silent(self):
        """上下文小、差距不到阈值：不打扰"""
        path = transcript(self.tmp.name, "gpt-6-astra", 8_000, requests_per_turn=1)
        p = run(self.payload("gpt-6-astra", path))
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))

    def test_threshold_is_configurable(self):
        """阈值调高到 999 之后，同样的会话也该闭嘴"""
        path = transcript(self.tmp.name, "gpt-6-astra", 180_000)
        p = run(self.payload("gpt-6-astra", path), env={"CODEX_COST_HINT_MIN": "999"})
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))

    def test_block_mode(self):
        """--block：按协议只能用 decision=block，并带上给人看的 reason"""
        path = transcript(self.tmp.name, "gpt-6-astra", 180_000)
        out = json.loads(run(self.payload("gpt-6-astra", path), "--block").stdout)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"])

    def test_output_keys_match_protocol(self):
        """输出只许用 Codex 0.155 认的键，多一个都会被判成非法 JSON 输出"""
        allowed = {"continue", "decision", "reason", "stopReason",
                   "suppressOutput", "systemMessage", "hookSpecificOutput"}
        path = transcript(self.tmp.name, "gpt-6-astra", 180_000)
        for args in ((), ("--block",)):
            out = json.loads(run(self.payload("gpt-6-astra", path), *args).stdout)
            self.assertTrue(set(out) <= allowed, f"多了不认识的键：{set(out) - allowed}")

    def test_never_blocks_on_bad_input(self):
        """坏输入不许挡消息：退出码必须是 0，且不许输出 decision"""
        path = transcript(self.tmp.name, "gpt-6-astra", 180_000)
        cases = ["", "not json", "{}", '{"model":"gpt-6-astra"}',
                 '{"model":"gpt-6-astra","transcript_path":"/no/such/file"}',
                 '{"model":"未知模型","transcript_path":"%s"}' % path,
                 '{"model":null,"transcript_path":null}']
        for raw in cases:
            p = run(raw)
            self.assertEqual(p.returncode, 0, f"输入 {raw[:40]!r} 让钩子非零退出了")
            self.assertNotIn("decision", p.stdout, f"输入 {raw[:40]!r} 拦了消息")

    def test_garbled_transcript(self):
        """日志里混进坏行（Codex 正在写、格式变了）也要照常工作"""
        path = transcript(self.tmp.name, "gpt-6-astra", 180_000)
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"timestamp":"坏的\n{不是 json\n')
        p = run(self.payload("gpt-6-astra", path))
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
