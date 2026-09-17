#!/usr/bin/env python3
"""系数只存一处：app（生成的 Coefficients.swift）、README 表格、refit、CLI 都要和
research/coefficients.json 一致。

    python3 tests/test_coefficients.py
"""
from __future__ import annotations
import importlib.util, json, os, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(ROOT, "research", "coefficients.json")


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Coefficients(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(JSON_PATH, encoding="utf-8") as f:
            cls.data = json.load(f)
        cls.expected = {m: None if c.get("free") else (c["fresh"], c["cached"], c["output"], c["request"])
                        for m, c in cls.data["models"].items()}

    def test_generated_swift_and_readme_tables_are_in_sync(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "research", "sync_coefficients.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_refit_reads_json(self):
        refit = module(os.path.join(ROOT, "research", "refit.py"), "refit")
        self.assertEqual(refit.SHIPPED, self.expected)

    def test_cli_reads_json(self):
        cli = module(os.path.join(ROOT, "cli", "codex_budget.py"), "codex_budget")
        for m, exp in self.expected.items():
            self.assertEqual(cli.COEF[m], (None, None, None, 0.0) if exp is None else exp, m)
        self.assertEqual(cli.DEFAULT_COEF, self.expected[self.data["fallback"]])
        self.assertEqual(cli.COUNTERFACTUAL,
                         [m for m, c in self.data["models"].items() if c.get("counterfactual", True)])

    def test_json_is_complete(self):
        self.assertIn(self.data["fallback"], self.data["models"])
        for m, c in self.data["models"].items():
            if not c.get("free"):
                for k in ("fresh", "cached", "output"):
                    self.assertGreater(c[k], 0, f"{m}.{k}")
                # 每请求固定成本实测为 0（req/many 与 req/few 专门拆过），只要求非负
                self.assertGreaterEqual(c["request"], 0, f"{m}.request")


if __name__ == "__main__":
    unittest.main(verbosity=2)
