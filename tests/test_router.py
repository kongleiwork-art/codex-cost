#!/usr/bin/env python3
"""路由规则回归：本地规则关键词、工作区信号、Luna 回退与解析。"""
from __future__ import annotations
import os, sys, tempfile, unittest

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
        d = R.local_rules("改个字：把错别字修一下")
        self.assertEqual(d["model"], R.LUNA)

    def test_implement_goes_sol(self):
        d = R.local_rules("implement a retry helper and add unit tests")
        self.assertEqual(d["model"], R.SOL)
        self.assertGreaterEqual(d["confidence"], 0.7)

    def test_architecture_goes_astra(self):
        d = R.local_rules("redesign the architecture and migrate across the modules")
        self.assertEqual(d["model"], R.ASTRA)
        self.assertGreaterEqual(d["confidence"], 0.7)
        self.assertIn("hard-kw", d["hits"])

    def test_vague_task_low_confidence(self):
        d = R.local_rules("do the thing")
        self.assertLess(d["confidence"], 0.7)
        self.assertIn("no-signal", d["hits"])

    def test_many_files_bumps_astra(self):
        sig = {"changed_files": 12, "diff_bytes": 5000,
               "touches_tests": False, "multi_file_arch": True}
        d = R.local_rules("please help with this", sig)
        self.assertEqual(d["model"], R.ASTRA)
        self.assertIn("many-files", d["hits"])

    def test_tiny_diff_prefers_luna(self):
        sig = {"changed_files": 1, "diff_bytes": 200,
               "touches_tests": False, "multi_file_arch": False}
        d = R.local_rules("small tweak", sig)
        self.assertEqual(d["model"], R.LUNA)
        self.assertIn("tiny-diff", d["hits"])

    def test_huge_diff_with_impl_kw_stays_hard(self):
        sig = {"changed_files": 4, "diff_bytes": 50_000,
               "touches_tests": True, "multi_file_arch": False}
        d = R.local_rules("fix the flaky integration bug", sig)
        # impl-kw + huge-diff + few-files + tests → astra territory
        self.assertIn(d["model"], (R.SOL, R.ASTRA))
        self.assertGreaterEqual(d["confidence"], 0.7)


class LunaFallback(unittest.TestCase):
    def test_parse_tier_first_word(self):
        self.assertEqual(R.parse_tier("sol\n"), R.SOL)
        self.assertEqual(R.parse_tier("I pick astra for this."), R.ASTRA)
        self.assertEqual(R.parse_tier("luna"), R.LUNA)

    def test_route_skips_luna_when_confident(self):
        called = {"n": 0}

        def boom(_):
            called["n"] += 1
            raise AssertionError("should not call luna")

        d = R.route("explain this typo", allow_luna=True, luna_fn=boom)
        self.assertEqual(d["model"], R.LUNA)
        self.assertFalse(d["used_luna"])
        self.assertEqual(called["n"], 0)

    def test_route_calls_luna_when_ambiguous(self):
        d = R.route("handle the request carefully", allow_luna=True,
                    luna_fn=lambda _: "astra")
        self.assertEqual(d["model"], R.ASTRA)
        self.assertTrue(d["used_luna"])

    def test_route_luna_failure_falls_back_sol(self):
        d = R.route("handle the request carefully", allow_luna=True,
                    luna_fn=lambda _: None)
        self.assertEqual(d["model"], R.SOL)
        self.assertTrue(d["used_luna"])

    def test_no_luna_flag_keeps_local(self):
        d = R.route("handle the request carefully", allow_luna=False)
        self.assertFalse(d["used_luna"])
        self.assertLess(d["confidence"], 0.7)


class WorkspaceInspect(unittest.TestCase):
    def test_inspect_git_repo(self):
        with tempfile.TemporaryDirectory() as td:
            subprocess_ok(td)
            open(os.path.join(td, "a.py"), "w").write("print(1)\n")
            open(os.path.join(td, "test_a.py"), "w").write("def test_x():\n  assert 1\n")
            run(["git", "-C", td, "add", "-A"])
            # unstaged edit after first commit so status --porcelain sees changes
            run(["git", "-C", td, "commit", "-m", "init"])
            open(os.path.join(td, "a.py"), "w").write("print(2)\n")
            open(os.path.join(td, "b.py"), "w").write("x=1\n")
            sig = R.inspect(td)
            self.assertGreaterEqual(sig["changed_files"], 1)
            # either modified tracked or untracked; touches_tests if test file dirty
            # test_a.py is committed clean — touch it
            open(os.path.join(td, "test_a.py"), "a").write("#\n")
            sig2 = R.inspect(td)
            self.assertTrue(sig2["touches_tests"])


def run(cmd):
    import subprocess
    subprocess.run(cmd, check=True, capture_output=True)


def subprocess_ok(td):
    run(["git", "-C", td, "init"])
    run(["git", "-C", td, "config", "user.email", "t@t.com"])
    run(["git", "-C", td, "config", "user.name", "t"])


if __name__ == "__main__":
    unittest.main()
