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


class Installer(unittest.TestCase):
    """安装脚本只许动自己那条钩子，别人的原样保留，卸载要能还原。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-cost-install-")
        self.home = self.tmp.name
        self.path = os.path.join(self.home, "hooks.json")

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, *args):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "cli", "install_model_hint.py"), *args],
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, CODEX_HOME=self.home))
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def ours(self, data):
        return [h for g in data.get("hooks", {}).get("UserPromptSubmit", [])
                for h in g["hooks"] if "codex_model_hint.py" in h["command"]]

    def test_dry_run_touches_nothing(self):
        out = self.install("--dry-run")
        self.assertFalse(os.path.exists(self.path), "--dry-run 写文件了")
        self.assertIn("codex_model_hint.py", out)

    def test_install_is_idempotent(self):
        self.install()
        self.install()
        self.assertEqual(len(self.ours(self.read())), 1, "重复安装叠加了")

    def test_keeps_other_hooks_and_restores(self):
        other = {"hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "/usr/bin/true"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "/bin/echo hi"}]}]}}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(other, f)
        self.install()
        after = self.read()
        cmds = [h["command"] for g in after["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertIn("/usr/bin/true", cmds, "把别人的钩子弄丢了")
        self.assertEqual(len(self.ours(after)), 1)
        self.install("--uninstall")
        self.assertEqual(self.read(), other, "卸载后没还原成原样")

    def test_backup_before_write(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"hooks": {}}, f)
        self.install()
        self.assertTrue([n for n in os.listdir(self.home) if n.startswith("hooks.json.bak-")],
                        "覆盖前没备份")

    def test_block_flag(self):
        self.install("--block")
        self.assertIn("--block", self.ours(self.read())[0]["command"])

    def test_missing_script_cannot_block_messages(self):
        """脚本被挪走时，钩子命令必须仍以 0 退出 —— 退出码 2 会被当成拦截"""
        self.install()
        cmd = self.ours(self.read())[0]["command"].replace("codex_model_hint.py",
                                                           "codex_model_hint.py.moved")
        p = subprocess.run(cmd, shell=True, input="{}", text=True, capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, "脚本不在时钩子会拦住消息")

    def test_broken_hooks_json_is_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "cli", "install_model_hint.py")],
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, CODEX_HOME=self.home))
        self.assertNotEqual(p.returncode, 0, "坏文件也照写")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ 这不是 json", "覆盖了坏文件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
