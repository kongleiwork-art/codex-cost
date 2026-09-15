#!/usr/bin/env python3
"""codex_route — 开新任务时选一次模型：本地规则（0 token），规则没有依据时才问 Luna。

只看任务描述，不看工作区状态：十几个没提交的文件，说明不了下一个任务难不难。
没有接进 app。按你自己的 Codex 历史回测，按难度选模型省不下额度
（research/backtest_routing.py，docs/ROADMAP.md 门槛 G1）。

    python3 cli/codex_route.py --task "fix the flaky test" --json
    python3 cli/codex_route.py --task "..." --no-luna      # 只跑本地规则

零依赖，标准库。
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, tempfile

LUNA = "gpt-5.6-luna"
SOL = "gpt-5.6-sol"
ASTRA = "gpt-6-astra"

LUNA_KW = ["typo", "rename", "what is", "what's", "explain", "how do i",
           "简单", "改个字", "错别字", "解释一下", "什么是", "咋写"]
SOL_KW = ["fix", "bug", "test", "implement", "add", "update", "refactor",
          "修复", "实现", "加个", "补测试", "改一下"]
ASTRA_KW = ["architecture", "redesign", "migrate", "migration", "multi-file",
            "rewrite", "design system", "performance", "race condition", "deadlock",
            "架构", "重构整个", "迁移", "跨模块", "重写", "并发", "性能优化", "死锁"]
# 改动范围：同一个动作，改一处和改遍全仓不是一个难度
SCOPE_KW = ["everywhere", "across", "all files", "every file",
            "entire codebase", "whole codebase",
            "全部", "所有文件", "整个项目", "全局"]

# 随 ChatGPT 桌面版安装的 codex 不在 PATH 里
CODEX_CANDIDATES = [
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    "/usr/local/bin/codex", "/opt/homebrew/bin/codex",
]


def short(model: str) -> str:
    return model.replace("gpt-", "", 1)


def matches(text: str, kw: str) -> bool:
    """英文按整词匹配（容许 s / es / d / ed / ing 词尾），中文按子串。

    纯子串会误伤：「add」命中 address / padding，「fix」命中 prefix，「test」命中 latest。
    """
    if not kw.isascii():
        return kw in text
    pattern = r"(?<![a-z0-9])" + re.escape(kw) + r"(?:s|es|d|ed|ing)?(?![a-z0-9])"
    return re.search(pattern, text) is not None


def local_rules(task: str) -> dict:
    t = task.lower()
    score = 0.0          # <0 偏 luna，>0 偏 astra；0 附近 = sol
    hits = []  # type: list
    confidence = 0.35

    def bump(d, why):
        nonlocal score, confidence
        score += d
        hits.append(why)
        confidence = min(1.0, confidence + 0.12)

    def hit(kws):
        return any(matches(t, k) for k in kws)

    if hit(LUNA_KW):
        bump(-1.2, "simple-kw")
    if hit(SOL_KW):
        bump(0.5, "impl-kw")
    if hit(ASTRA_KW):
        bump(1.6, "hard-kw")
    if hit(SCOPE_KW):
        bump(1.0, "scope")

    if len(t) < 40 and score <= 0:
        bump(-0.4, "short-task")
    if len(t) > 400:
        bump(0.5, "long-brief")

    # 只有 short-task / 完全没信号 → 留给 Luna，别假装很有把握
    if not hits or hits == ["short-task"]:
        confidence = 0.25
        hits = ["no-signal"]
    elif any(h.endswith("-kw") for h in hits) or "scope" in hits:
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
        "hits": hits,
        "score": score,
    }


def parse_tier(raw: str | None) -> str | None:
    """取回复里第一个出现的档位词。

    回复来自 -o 写出的最后一条消息，而不是 stdout —— stdout 里可能回显提示词，
    而提示词里 luna / sol / astra 三个词都有。
    """
    m = re.search(r"(?<![a-z0-9])(luna|sol|astra)(?![a-z0-9])", (raw or "").lower())
    return {"luna": LUNA, "sol": SOL, "astra": ASTRA}[m.group(1)] if m else None


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
    if env and os.access(env, os.X_OK):
        return env
    for p in CODEX_CANDIDATES + [os.path.expanduser("~/.local/bin/codex")]:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("codex") or ""


def luna_judge(task: str, codex: str | None = None, runner=subprocess.run) -> str | None:
    """让 Luna 用一个词回答难度。只读 -o 写出的最后一条回复，stdout / stderr 全部丢弃。

    --ephemeral 不落会话记录，免得这次判定被 codex-cost 算进你的用量。
    """
    codex = codex or find_codex()
    if not codex:
        return None
    tmp = tempfile.mkdtemp(prefix="codex-cost-judge-")
    answer = os.path.join(tmp, "answer.txt")
    try:
        cmd = [codex, "exec", "--skip-git-repo-check", "--ephemeral",
               "-C", tmp, "-m", LUNA, "-s", "read-only",
               "-c", 'approval_policy="never"',
               "-o", answer, judge_prompt(task)]
        try:
            p = runner(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if getattr(p, "returncode", 1) != 0 or not os.path.exists(answer):
            return None
        with open(answer, encoding="utf-8", errors="replace") as f:
            return f.read() or None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def route(task: str, allow_luna: bool = True, luna_fn=None) -> dict:
    task = (task or "").strip()
    local = local_rules(task)
    if local["confidence"] >= 0.7 or not allow_luna:
        return local
    picked = parse_tier((luna_fn or luna_judge)(task))
    if not picked:
        return {"model": SOL, "confidence": 0.45,
                "reason": "Luna judge failed; fell back to sol",
                "used_luna": True, "hits": local["hits"]}
    return {"model": picked, "confidence": 0.75,
            "reason": f"Luna judged → {short(picked)}",
            "used_luna": True, "hits": local["hits"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Route a task to luna/sol/astra")
    ap.add_argument("--task", required=True)
    ap.add_argument("--no-luna", action="store_true", help="只跑本地规则")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    d = route(args.task, allow_luna=not args.no_luna)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{d['model']}  conf={d['confidence']:.2f}  {d['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
