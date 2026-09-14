#!/usr/bin/env python3
"""codex_route — 本地规则 +（可选）Luna 判定任务难度，选出锁定模型。

与 Sources/Router.swift 保持同一套分级与关键词。会话中途不切模型：
这里只负责「开新任务时选一次」。

    python3 cli/codex_route.py --task "fix the flaky test" --workdir . --json
    python3 cli/codex_route.py --task "..." --no-luna

零依赖，标准库。
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, tempfile

LUNA = "gpt-5.6-luna"
SOL = "gpt-5.6-sol"
ASTRA = "gpt-6-astra"

LUNA_KW = [
    "typo", "rename", "what is", "what's", "explain", "how do i",
    "简单", "改个字", "错别字", "解释一下", "什么是", "咋写",
]
SOL_KW = [
    "fix", "bug", "test", "implement", "add", "update", "refactor",
    "修复", "实现", "加个", "补测试", "改一下",
]
ASTRA_KW = [
    "architecture", "redesign", "migrate", "multi-file", "across the",
    "rewrite", "design system", "performance", "race condition",
    "架构", "重构整个", "迁移", "跨模块", "重写", "并发", "性能优化",
]


def short(model: str) -> str:
    return model.replace("gpt-", "", 1)


def run(cmd, timeout=3):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def inspect(workdir: str | None) -> dict:
    s = {"changed_files": 0, "diff_bytes": 0, "touches_tests": False,
         "multi_file_arch": False}
    if not workdir or not os.path.isdir(workdir):
        return s
    status = run(["git", "-C", workdir, "status", "--porcelain"])
    lines = [ln for ln in status.splitlines() if ln.strip()]
    s["changed_files"] = len(lines)
    paths = [ln[3:] if len(ln) > 3 else ln for ln in lines]
    low = [p.lower() for p in paths]
    s["touches_tests"] = any(
        ("test" in p) or ("/tests/" in p) or p.endswith("_test.py")
        or p.endswith("tests.swift") or ("spec." in p)
        for p in low
    )
    code_ext = (".swift", ".ts", ".tsx", ".py", ".go", ".rs")
    s["multi_file_arch"] = sum(1 for p in paths if p.endswith(code_ext)) >= 5
    diff = run(["git", "-C", workdir, "diff", "--stat", "HEAD"])
    s["diff_bytes"] = len(diff.encode("utf-8", errors="replace"))
    m = re.search(r"(\d+)\s+files?\s+changed", diff)
    if m and int(m.group(1)) > s["changed_files"]:
        s["changed_files"] = int(m.group(1))
    return s


def local_rules(task: str, signals: dict | None = None) -> dict:
    signals = signals or {
        "changed_files": 0, "diff_bytes": 0,
        "touches_tests": False, "multi_file_arch": False,
    }
    t = task.lower()
    score = 0.0
    hits = []  # type: list
    confidence = 0.35

    def bump(d, why):
        nonlocal score, confidence
        score += d
        hits.append(why)
        confidence = min(1.0, confidence + 0.12)

    if any(k in t for k in LUNA_KW):
        bump(-1.2, "simple-kw")
    if any(k in t for k in SOL_KW):
        bump(0.5, "impl-kw")
    if any(k in t for k in ASTRA_KW):
        bump(1.6, "hard-kw")

    cf = signals["changed_files"]
    db = signals["diff_bytes"]
    # 有工作区强信号时，短文案不再往 luna 拉，避免冲掉 many-files
    if len(t) < 40 and score <= 0 and cf == 0 and not signals["multi_file_arch"]:
        bump(-0.4, "short-task")
    if len(t) > 400:
        bump(0.5, "long-brief")

    if cf >= 8 or signals["multi_file_arch"]:
        bump(1.6, "many-files")
    elif cf >= 3:
        bump(0.5, "few-files")
    elif cf == 1 and db < 800:
        bump(-1.0, "tiny-diff")

    if db >= 40_000:
        bump(1.0, "huge-diff")
    elif db >= 8_000:
        bump(0.4, "med-diff")

    if signals["touches_tests"] and score >= 0:
        bump(0.2, "tests")

    # 只有 short-task / 完全没信号 → 留给 Luna，别假装很有把握
    if not hits or hits == ["short-task"]:
        confidence = 0.25
        hits = ["no-signal"]
    elif any(h.endswith("-kw") for h in hits) or "many-files" in hits or "tiny-diff" in hits:
        confidence = max(confidence, 0.75)
    elif abs(score) >= 1.2:
        confidence = max(confidence, 0.82)
    elif abs(score) >= 0.5:
        confidence = max(confidence, 0.72)

    if score <= -0.8:
        model = LUNA
    elif score >= 1.2:
        model = ASTRA
    else:
        model = SOL

    return {
        "model": model,
        "confidence": min(1.0, confidence),
        "reason": f"local rules → {short(model)} ({', '.join(hits)})",
        "used_luna": False,
        "signals": signals,
        "score": score,
        "hits": hits,
    }


def parse_tier(raw: str) -> str | None:
    low = (raw or "").lower()
    for key, model in (("astra", ASTRA), ("sol", SOL), ("luna", LUNA)):
        if re.search(rf"\b{key}\b", low):
            return model
    if "astra" in low:
        return ASTRA
    if "luna" in low:
        return LUNA
    if "sol" in low:
        return SOL
    return None


def judge_prompt(task: str) -> str:
    return (
        "Classify this coding task difficulty as exactly one word: luna, sol, or astra.\n"
        "luna = trivial lookup/typo/explain; sol = normal implementation; "
        "astra = hard architecture or large multi-file work.\n"
        "Reply with only that one word.\n\n"
        f"Task:\n{task[:2000]}"
    )


def find_codex() -> str:
    env = os.environ.get("CODEX_BIN") or ""
    if env:
        return env
    w = shutil.which("codex")
    if w:
        return w
    home = os.path.expanduser("~")
    for p in (f"{home}/.local/bin/codex", "/usr/local/bin/codex",
              "/opt/homebrew/bin/codex"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def luna_judge(task: str) -> str | None:
    codex = find_codex()
    if not codex:
        return None
    tmp = tempfile.mkdtemp(prefix="codex-cost-judge-")
    try:
        cmd = [
            codex, "exec", "--skip-git-repo-check",
            "-C", tmp, "-m", LUNA,
            "-c", 'sandbox_mode="read-only"',
            "-c", 'approval_policy="never"',
            judge_prompt(task),
        ]
        out = run(cmd, timeout=90)
        return out or None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def route(task: str, workdir: str | None = None, allow_luna: bool = True,
          luna_fn=None) -> dict:
    task = (task or "").strip()
    signals = inspect(workdir)
    local = local_rules(task, signals)
    if local["confidence"] >= 0.7 or not allow_luna:
        return local
    fn = luna_fn or luna_judge
    raw = fn(task)
    if not raw:
        return {
            "model": SOL,
            "confidence": 0.45,
            "reason": "Luna judge failed; fell back to sol",
            "used_luna": True,
            "signals": signals,
        }
    picked = parse_tier(raw) or SOL
    return {
        "model": picked,
        "confidence": 0.75,
        "reason": f"Luna judged → {short(picked)}",
        "used_luna": True,
        "signals": signals,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Route a task to luna/sol/astra")
    ap.add_argument("--task", required=True)
    ap.add_argument("--workdir")
    ap.add_argument("--no-luna", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    d = route(args.task, args.workdir, allow_luna=not args.no_luna)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{d['model']}  conf={d['confidence']:.2f}  {d['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
