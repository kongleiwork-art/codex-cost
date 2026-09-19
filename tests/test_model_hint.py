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

    def test_counts_turns_marked_by_user_message(self):
        """桌面版用 role=user 的 message 分界，不是 task_started。

        只认 task_started 的话，几轮会被并成一轮，「每轮几次请求」估少好几倍，
        差值算不到阈值就永远静默 —— 真机上就是这么哑掉的。
        """
        import importlib
        sys.path.insert(0, os.path.join(ROOT, "cli"))
        hint = importlib.import_module("codex_model_hint")
        now = time.time()
        s = fixtures.Session(self.tmp.name, now - 3600, "gpt-6-astra")
        for t in range(4):                      # 4 轮，每轮 3 次请求
            s.add(now - 3000 + t * 300, {"type": "message",
                                         "payload": {"type": "message", "role": "user",
                                                     "content": [{"type": "input_text", "text": "x"}]}})
            for i in range(3):
                s.tokens(now - 3000 + t * 300 + i,
                         {"input_tokens": 120_000, "cached_input_tokens": 118_000,
                          "output_tokens": 100, "reasoning_output_tokens": 50},
                         {"primary": fixtures.window(300, 20, now + 7200)})
        s.close()
        context, per_turn = hint.session_shape(s.path)
        self.assertEqual(per_turn, 3, "没按 role=user 的 message 分界")
        self.assertEqual(context, 120_000)

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
    """安装脚本只许在 config.toml 末尾加自己那一段，别的一律不动，卸载要能还原。

    为什么是 config.toml：Codex 也认 hooks.json，但只在「从 Claude Code 迁移」和
    「插件自带清单」两处读它 —— 往 ~/.codex/hooks.json 写，Codex 连读都不读。
    """

    OTHER = ('model = "gpt-5.6-sol"\n\n'
             '[mcp_servers."x"]\ncommand = "/bin/true"\n')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-cost-install-")
        self.home = self.tmp.name
        self.path = os.path.join(self.home, "config.toml")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.OTHER)

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
            return f.read()

    def command_line(self):
        """我们那条 command=，不是配置里别人的"""
        for line in self.read().splitlines():
            if line.startswith("command = ") and "codex_model_hint.py" in line:
                return line
        return ""

    def hook_command(self):
        line = self.command_line()
        return line[len("command = "):].strip().strip('"').replace('\\"', '"')

    def test_dry_run_touches_nothing(self):
        out = self.install("--dry-run")
        self.assertEqual(self.read(), self.OTHER, "--dry-run 改文件了")
        self.assertIn("codex_model_hint.py", out)

    def test_writes_hook_section(self):
        self.install()
        text = self.read()
        self.assertIn("[[hooks.UserPromptSubmit]]", text)
        self.assertIn("codex_model_hint.py", text)

    def test_keeps_the_rest_of_the_config(self):
        self.install()
        text = self.read()
        for keep in ('model = "gpt-5.6-sol"', '[mcp_servers."x"]', 'command = "/bin/true"'):
            self.assertIn(keep, text, f"把 {keep} 弄丢了")

    def test_install_is_idempotent(self):
        self.install()
        once = self.read()
        self.install()
        self.assertEqual(self.read(), once, "重复安装叠加了")
        self.assertEqual(once.count("[[hooks.UserPromptSubmit]]"), 1)

    def test_uninstall_restores_byte_for_byte(self):
        self.install()
        self.install("--uninstall")
        self.assertEqual(self.read(), self.OTHER, "卸载后没还原成原样")

    def test_backup_before_write(self):
        self.install()
        self.assertTrue([n for n in os.listdir(self.home) if n.startswith("config.toml.bak-")],
                        "覆盖前没备份")

    def test_block_flag(self):
        self.install("--block")
        self.assertIn("--block", self.command_line())

    def test_missing_script_cannot_block_messages(self):
        """脚本被挪走时，钩子命令必须仍以 0 退出 —— 退出码 2 会被当成拦截"""
        self.install()
        cmd = self.hook_command().replace("codex_model_hint.py", "codex_model_hint.py.moved")
        p = subprocess.run(cmd, shell=True, input="{}", text=True, capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, "脚本不在时钩子会拦住消息")

    def test_command_survives_toml_round_trip(self):
        """命令里全是引号，写进 TOML 再读出来必须还是同一条能跑的命令"""
        self.install()
        p = subprocess.run(self.hook_command(), shell=True, input="不是 json", text=True,
                           capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
