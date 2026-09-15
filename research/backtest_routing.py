#!/usr/bin/env python3
"""路线图门槛 G1：省额度回测 —— 如果每段会话开头都按本地规则选了模型，能省多少额度。不花额度。

    python3 research/backtest_routing.py

做法：对每段普通会话（不含实验沙箱和子 agent），用 cli/codex_route.py 的本地规则
按首条任务定档；规则没把握（置信度 < 0.7）的，真实路由会去问 Luna，这里按「不换模型」计。
同样的 token 分别按实际模型和建议模型折算额度，差值就是可省或多花的额度。

这是上限估计：换成更弱的模型可能要多几轮才做完，换成更强的也可能少几轮 —— 回测里
token 数不变。所以降到 Luna 的部分单独列出，它的「省」最不可靠。
只输出聚合数字，不含提示词和路径。
"""
from __future__ import annotations
import os, sys, time
from codex_logs import cost, load, load_coefficients, weekly_ratio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cli"))
from codex_route import local_rules, short  # noqa: E402

GATE_WEEKLY_PCT = 10.0


def backtest(sessions, coef, since=0.0):
    agg = {"sessions": 0, "low_conf": 0, "no_coef": 0, "no_coef_tokens": 0, "tokens": 0,
           "actual": 0.0, "routed": 0.0, "saved": 0.0, "saved_to_luna": 0.0, "extra": 0.0,
           "down": 0, "up": 0, "same": 0, "first": None, "last": None}
    for s in sessions:
        if s.kind != "normal" or not s.first_prompt:
            continue
        reqs = [r for r in s.requests if r.ts >= since]
        if not reqs:
            continue
        toks = sum(r.total for r in reqs)
        actual = [cost(r, coef) for r in reqs]
        if any(a is None for a in actual):
            agg["no_coef"] += 1
            agg["no_coef_tokens"] += toks
            continue
        agg["sessions"] += 1
        agg["tokens"] += toks
        agg["first"] = min(agg["first"] or reqs[0].ts, reqs[0].ts)
        agg["last"] = max(agg["last"] or reqs[-1].ts, reqs[-1].ts)
        a = sum(actual)
        rule = local_rules(s.first_prompt)
        if rule["confidence"] < 0.7:
            agg["low_conf"] += 1
            routed = a
        else:
            routed = sum(cost(r, coef, rule["model"]) for r in reqs)
        agg["actual"] += a
        agg["routed"] += routed
        diff = a - routed
        if diff > 1e-9:
            agg["down"] += 1
            agg["saved"] += diff
            if rule["model"] == "gpt-5.6-luna":
                agg["saved_to_luna"] += diff
        elif diff < -1e-9:
            agg["up"] += 1
            agg["extra"] += -diff
        else:
            agg["same"] += 1
    return agg


def report(label, agg, ratio, weeks):
    print(f"\n【{label}】{agg['sessions']} 段会话、{agg['tokens'] / 1e6:,.0f}M token，约 {weeks:.1f} 周")
    if agg["no_coef"]:
        print(f"  另有 {agg['no_coef']} 段含没有系数的模型（reserve、未知模型等），"
              f"{agg['no_coef_tokens'] / 1e6:,.0f}M token，不计入")
    print(f"  规则有把握 {agg['sessions'] - agg['low_conf']} 段；没把握 {agg['low_conf']} 段（按不换模型计）")
    print(f"  建议换便宜的 {agg['down']} 段、换更贵的 {agg['up']} 段、不变 {agg['same']} 段")
    to_w = (lambda x: x / ratio / weeks) if ratio else (lambda x: float("nan"))
    print(f"  实际花掉：5 小时额度 {agg['actual']:,.0f}%  ≈ 每周 {to_w(agg['actual']):.0f}% 周额度")
    print(f"  换便宜的能省：{agg['saved']:,.0f}%（其中降到 Luna {agg['saved_to_luna']:,.0f}%）"
          f"  ≈ 每周 {to_w(agg['saved']):.1f}% 周额度")
    print(f"  换更贵的多花：{agg['extra']:,.0f}%  ≈ 每周 {to_w(agg['extra']):.1f}% 周额度")
    net = agg["saved"] - agg["extra"]
    no_luna = agg["saved"] - agg["saved_to_luna"] - agg["extra"]
    print(f"  净省：每周 {to_w(net):.1f}% 周额度；不算降到 Luna 的：每周 {to_w(no_luna):.1f}%")
    return to_w(net), to_w(no_luna)


def main():
    coef = load_coefficients()
    sessions, requests = load()
    ratio = weekly_ratio(requests)
    print(f"换算：周额度 1% ≈ 5 小时额度 {ratio:.1f}%（按两个读数同涨的幅度估计）" if ratio
          else "读数不足，算不出周额度换算")
    now = time.time()
    results = {}
    for label, since in (("全部历史", 0.0), ("近 30 天", now - 30 * 86400)):
        agg = backtest(sessions, coef, since)
        if not agg["sessions"]:
            print(f"\n【{label}】没有可回测的会话")
            continue
        span = (agg["last"] - agg["first"]) / 604800 if since == 0 else 30 / 7
        results[label] = report(label, agg, ratio, max(span, 1 / 7))
    if "近 30 天" in results:
        net, no_luna = results["近 30 天"]
        print(f"\n门槛 G1（按近 30 天）：可省 < 周额度 {GATE_WEEKLY_PCT:g}% 就停在 cx 给建议")
        verdict = "过" if net >= GATE_WEEKLY_PCT else "不过"
        print(f"  净省每周 {net:.1f}% → {verdict}；不算 Luna 每周 {no_luna:.1f}%")
        print("  另一半门槛（规则准确率 ≥ 70%）需要人工标注样本，这个脚本不判断")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
