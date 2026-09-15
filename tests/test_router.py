#!/usr/bin/env python3
"""路由规则回归：本地规则、Luna 判定与回退。

    python3 tests/test_router.py

不会真的调用 codex：Luna 判定一律用假的 runner / luna_fn。
"""
from __future__ import annotations
import json, os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "cli"))
import codex_route as R  # noqa: E402


class LocalRules(unittest.TestCase):
    def test_simple_explain_goes_luna(self):
        d = R.local_rules("explain what this function does")
        self.assertEqual(d["model"], R.LUNA)
        self.assertGreaterEqual(d["confidence"], 0.7)
        self.assertIn("simple-kw", d["hits"])

    def test_typo_zh_goes_luna(self):
        self.assertEqual(R.local_rules("改个字：把错别字修一下")["model"], R.LUNA)

    def test_implement_goes_sol(self):
        d = R.local_rules("implement a retry helper and add unit tests")
        self.assertEqual(d["model"], R.SOL)
        self.assertGreaterEqual(d["confidence"], 0.7)

    def test_architecture_goes_astra(self):
        d = R.local_rules("redesign the architecture and migrate across the modules")
        self.assertEqual(d["model"], R.ASTRA)
        self.assertIn("hard-kw", d["hits"])

    def test_vague_task_low_confidence(self):
        d = R.local_rules("do the thing")
        self.assertLess(d["confidence"], 0.7)
        self.assertEqual(d["hits"], ["no-signal"])

    def test_rename_everywhere_is_not_luna(self):
        # 「rename」偏简单，但「everywhere」说明要改遍全仓
        d = R.local_rules("rename getUser to fetchUser everywhere")
        self.assertEqual(d["model"], R.SOL)
        self.assertIn("scope", d["hits"])

    def test_across_services_is_hard(self):
        d = R.local_rules("implement OAuth token refresh across services")
        self.assertEqual(d["model"], R.ASTRA)

    def test_no_substring_false_positives(self):
        # address / padding 里有 add，prefix 里有 fix，latest 里有 test
        d = R.local_rules("check the latest prefix in the address padding")
        self.assertEqual(d["hits"], ["no-signal"])

    def test_inflections_still_match(self):
        self.assertIn("impl-kw", R.local_rules("fixes the failing tests")["hits"])
        self.assertIn("hard-kw", R.local_rules("the service deadlocks under load")["hits"])

    def test_only_task_text_is_used(self):
        # 工作区状态（脏文件数、diff 体量）与这次任务的难度无关，规则里不再有它
        import inspect
        self.assertEqual(list(inspect.signature(R.local_rules).parameters), ["task"])


class LunaFallback(unittest.TestCase):
    def test_parse_tier_first_word(self):
        self.assertEqual(R.parse_tier("sol\n"), R.SOL)
        self.assertEqual(R.parse_tier("I pick astra for this."), R.ASTRA)
        self.assertEqual(R.parse_tier("Answer: luna"), R.LUNA)
        self.assertIsNone(R.parse_tier("not sure"))
        self.assertIsNone(R.parse_tier("solution"))

    def test_route_skips_luna_when_confident(self):
        def boom(_):
            raise AssertionError("should not call luna")
        d = R.route("explain this typo", allow_luna=True, luna_fn=boom)
        self.assertEqual(d["model"], R.LUNA)
        self.assertFalse(d["used_luna"])

    def test_route_calls_luna_when_ambiguous(self):
        d = R.route("handle the request carefully", allow_luna=True,
                    luna_fn=lambda _: "astra")
        self.assertEqual(d["model"], R.ASTRA)
        self.assertTrue(d["used_luna"])

    def test_route_luna_failure_falls_back_sol(self):
        for answer in (None, "no idea"):
            d = R.route("handle the request carefully", allow_luna=True,
                        luna_fn=lambda _: answer)
            self.assertEqual(d["model"], R.SOL)
            self.assertTrue(d["used_luna"])
            self.assertIn("failed", d["reason"])

    def test_no_luna_flag_keeps_local(self):
        d = R.route("handle the request carefully", allow_luna=False)
        self.assertFalse(d["used_luna"])
        self.assertLess(d["confidence"], 0.7)


class LunaJudgeCommand(unittest.TestCase):
    def test_reads_last_message_file_not_stdout(self):
        seen = {}

        def fake(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            with open(cmd[cmd.index("-o") + 1], "w") as f:
                f.write("sol\n")

            class P:
                returncode = 0
            return P()

        raw = R.luna_judge("do the thing", codex="/bin/true", runner=fake)
        self.assertEqual(R.parse_tier(raw), R.SOL)
        cmd = seen["cmd"]
        self.assertIn("--ephemeral", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], R.LUNA)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        # 输出不接管道：接了又不读，超过 64KB 就会死锁
        self.assertEqual(seen["kw"]["stdout"], subprocess.DEVNULL)
        self.assertEqual(seen["kw"]["stderr"], subprocess.DEVNULL)

    def test_prompt_echo_would_mislead(self):
        # 提示词里三个档位词都有：解析回显的提示词会得到假结果，所以只能读 -o 文件
        p = R.judge_prompt("x").lower()
        for w in ("luna", "sol", "astra"):
            self.assertIn(w, p)

    def test_nonzero_exit_returns_none(self):
        def fake(cmd, **kw):
            class P:
                returncode = 1
            return P()
        self.assertIsNone(R.luna_judge("x", codex="/bin/true", runner=fake))


if __name__ == "__main__":
    unittest.main(verbosity=2)
