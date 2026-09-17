#!/usr/bin/env python3
"""路线图 2.5：缓存重读分析 —— 什么时候重读、连续使用时为什么也重读。不花额度。

    python3 research/cache_misses.py

「重读」：上一次请求的上下文 ≥ 3 万 token，这一次却有一半以上报成新增输入。
「命中」：这一次报成新增输入的不到 20%。

**重读不额外花额度**（路线图 2.3 实测：39 次请求含 2 次重读，实测 17%，全按缓存价
预测 16.4%，把重读按新增输入价预测 24%）。所以这里报的是重读的**频率和 token 量**，
不是额度占比。最后一列是「如果按新增输入计价会多花多少」—— 那是已被证伪的算法，
留着只为说明早期版本 11% 的结论是怎么来的。

只看普通会话（不含实验沙箱和子 agent），只读本机 ~/.codex，只输出聚合数字。
"""
from __future__ import annotations
import collections, glob, json, os, time
from codex_logs import FREE, SANDBOX_MARKERS, codex_home, load_coefficients, parse_ts

MIN_CONTEXT = 30_000
SKIP_FIELDS = {"turn_id", "root_turn_id", "timezone"}
IDLE_BUCKETS = ((60, "<1 分钟"), (300, "1–5 分钟"), (600, "5–10 分钟"),
                (1800, "10–30 分钟"), (3600, "30–60 分钟"), (float("inf"), "> 1 小时"))


def bucket(gap):
    return next(name for limit, name in IDLE_BUCKETS if gap < limit)


def scan(coef, since):
    """逐个普通会话按时间顺序走一遍，统计命中 / 失效，并给 5 分钟内的失效找原因"""
    total = 0.0          # 普通会话总额度（按 v5 计价：重读算缓存）
    reread_tok = 0        # 被重读的 token 量
    input_tok = 0         # 所有请求的 input token 量，用作分母
    idle = collections.OrderedDict((n, {"hit": 0, "miss": 0, "tok": 0, "if_fresh": 0.0})
                                   for _, n in IDLE_BUCKETS)
    causes, cause_tok, fields = collections.Counter(), collections.Counter(), collections.Counter()
    for path in glob.glob(os.path.join(codex_home(), "sessions", "**", "rollout-*.jsonl"), recursive=True):
        kind, model, tc, prev, between = "normal", None, {}, None, []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload") or {}
                t = p.get("type") or d.get("type") or ""
                if t == "session_meta":
                    cwd = p.get("cwd") or ""
                    if any(m in cwd for m in SANDBOX_MARKERS):
                        kind = "sandbox"
                    elif p.get("parent_thread_id") or p.get("agent_nickname"):
                        kind = "subagent"
                    continue
                if t == "turn_context":
                    model = p.get("model") or model
                    tc = {k: json.dumps(v, sort_keys=True) for k, v in p.items() if k not in SKIP_FIELDS}
                    continue
                if t != "token_count":
                    between.append(t)
                    continue
                u = (p.get("info") or {}).get("last_token_usage") or {}
                ts = parse_ts(d.get("timestamp"))
                if kind != "normal" or not u or ts is None:
                    continue
                inp, cached = u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0
                out = (u.get("output_tokens") or 0) + (u.get("reasoning_output_tokens") or 0)
                fresh = max(0, inp - cached)
                c = None if model in FREE else coef.get(model)
                reread = bool(inp and prev and prev[1] >= MIN_CONTEXT and fresh >= 0.5 * inp)
                if ts >= since and c:
                    # v5 计价：重读按缓存算，与 Budget.swift / codex_budget.py 同一条规则
                    billed_fresh, billed_cached = (0, cached + fresh) if reread else (fresh, cached)
                    total += billed_fresh / c[0] + billed_cached / c[1] + out / c[2] + c[3]
                    input_tok += inp
                if ts >= since and c and inp and prev and prev[1] >= MIN_CONTEXT:
                    gap = ts - prev[0]
                    b = idle[bucket(gap)]
                    if reread:
                        b["miss"] += 1
                        b["tok"] += fresh
                        b["if_fresh"] += fresh / c[0] - fresh / c[1]
                        reread_tok += fresh
                        if gap < 300:
                            changed = sorted(k for k in set(tc) | set(prev[2]) if tc.get(k) != prev[2].get(k))
                            if any("compact" in e for e in between):
                                why = "上下文压缩"
                            elif inp < 0.7 * prev[1]:
                                why = "上下文缩小 ≥30%"
                            elif changed:
                                why = "会话设置变了"
                                fields.update(changed)
                            else:
                                why = "日志里看不出原因"
                            causes[why] += 1
                            cause_tok[why] += fresh
                    elif fresh <= 0.2 * inp:
                        b["hit"] += 1
                prev, between = (ts, inp, tc), []
    return total, input_tok, reread_tok, idle, causes, cause_tok, fields


def main():
    coef = load_coefficients()
    now = time.time()
    for label, since in (("近 30 天", now - 30 * 86400), ("全部历史", 0.0)):
        total, input_tok, reread_tok, idle, causes, cause_tok, fields = scan(coef, since)
        misses = sum(b["miss"] for b in idle.values())
        extra = sum(b["if_fresh"] for b in idle.values())
        print(f"\n【{label}】缓存重读 {misses} 次，重读 {reread_tok / 1e6:.1f}M token"
              f"（占全部 input 的 {reread_tok / input_tok:.1%}）；额外额度 0")
        print(f"   （若按已证伪的「重读=新增输入」计价，会多算 {extra / total:.1%}）")
        print(f"   {'距上一次请求':<10}{'命中':>7}{'重读':>6}{'重读率':>8}{'重读 token':>12}")
        for name, b in idle.items():
            n = b["hit"] + b["miss"]
            print(f"   {name:<10}{b['hit']:>7}{b['miss']:>6}{(b['miss'] / n if n else 0):>8.0%}"
                  f"{b['tok'] / 1e6:>11.1f}M")
        print("   5 分钟内的重读，按原因：")
        for why, k in causes.most_common():
            print(f"     {why:<16}{k:>5} 次  {cause_tok[why] / 1e6:>6.1f}M token")
        if fields:
            print("     变了的设置字段：" + "、".join(f"{k} {v}" for k, v in fields.most_common(6)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
