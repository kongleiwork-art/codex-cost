#!/usr/bin/env python3
"""路线图 2.5：缓存失效分析 —— 什么时候失效、占多少额度、连续使用时为什么也失效。不花额度。

    python3 research/cache_misses.py

「缓存失效」：上一次请求的上下文 ≥ 3 万 token，这一次却有一半以上按新增输入计费。
「命中」：这一次按新增输入计费的不到 20%。只看普通会话（不含实验沙箱和子 agent），
只读本机 ~/.codex，只输出聚合数字。
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
    total = 0.0
    idle = collections.OrderedDict((n, {"hit": 0, "miss": 0, "cost": 0.0}) for _, n in IDLE_BUCKETS)
    causes, cause_cost, fields = collections.Counter(), collections.defaultdict(float), collections.Counter()
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
                if ts >= since and c:
                    total += fresh / c[0] + cached / c[1] + out / c[2] + c[3]
                if ts >= since and c and inp and prev and prev[1] >= MIN_CONTEXT:
                    gap = ts - prev[0]
                    b = idle[bucket(gap)]
                    if fresh >= 0.5 * inp:
                        b["miss"] += 1
                        b["cost"] += fresh / c[0]
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
                            cause_cost[why] += fresh / c[0]
                    elif fresh <= 0.2 * inp:
                        b["hit"] += 1
                prev, between = (ts, inp, tc), []
    return total, idle, causes, cause_cost, fields


def main():
    coef = load_coefficients()
    now = time.time()
    for label, since in (("近 30 天", now - 30 * 86400), ("全部历史", 0.0)):
        total, idle, causes, cause_cost, fields = scan(coef, since)
        misses = sum(b["miss"] for b in idle.values())
        print(f"\n【{label}】缓存失效 {misses} 次，占普通会话总额度 "
              f"{sum(b['cost'] for b in idle.values()) / total:.1%}")
        print(f"   {'距上一次请求':<10}{'命中':>7}{'失效':>6}{'失效率':>8}{'占总额度':>9}")
        for name, b in idle.items():
            n = b["hit"] + b["miss"]
            print(f"   {name:<10}{b['hit']:>7}{b['miss']:>6}{(b['miss'] / n if n else 0):>8.0%}"
                  f"{b['cost'] / total:>9.1%}")
        print("   5 分钟内的失效，按原因：")
        for why, k in causes.most_common():
            print(f"     {why:<16}{k:>5} 次  占总额度 {cause_cost[why] / total:.1%}")
        if fields:
            print("     变了的设置字段：" + "、".join(f"{k} {v}" for k, v in fields.most_common(6)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
