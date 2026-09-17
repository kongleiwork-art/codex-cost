#!/usr/bin/env python3
"""路线图 2.1：真实会话验证集 —— 拿你平时真实使用的片段核对成本模型，不花额度。

    python3 research/validate_real.py            # 打印逐段对比和汇总
    python3 research/validate_real.py --json out.json

片段怎么挑见 codex_logs.clean_segments。对齐方式和 refit 一致：读数滞后一次请求，
所以首尾读数之差对应「第一个请求到倒数第二个请求」的用量。
只输出聚合数字，不含提示词和路径。

**这个口径系统性偏低，别拿它下结论**：跨度 ≥8% 的片段基本都是长时间连续使用，
而那种时段常常同时开着好几个会话，别的会话花的额度也算进了这一段的读数。
判断模型准不准用 intervals.py（逐区间、区分独占和并行；独占时段比值 0.99）。
"""
from __future__ import annotations
import argparse, json, statistics, time
from codex_logs import WIN_5H, WIN_WEEK, clean_segments, cost, load, load_coefficients


def summarize(seg, coef):
    p0, p1 = seg[0].reading[WIN_5H][0], seg[-1].reading[WIN_5H][0]
    billed = seg[:-1]
    pred = sum(cost(r, coef) for r in billed)
    models = sorted({r.model.replace("gpt-", "") for r in billed})
    w0 = seg[0].reading.get(WIN_WEEK)
    w1 = seg[-1].reading.get(WIN_WEEK)
    return {
        "day": time.strftime("%Y-%m-%d %H:%M", time.localtime(seg[0].ts)),
        "hours": round((seg[-1].ts - seg[0].ts) / 3600, 2),
        "requests": len(billed),
        "sessions": len({r.session for r in billed}),
        "subagent_requests": sum(1 for r in billed if r.kind == "subagent"),
        "models": models,
        "fresh": sum(r.fresh for r in billed),
        "cached": sum(r.cached for r in billed),
        "output": sum(r.output for r in billed),
        "avg_context": round(sum(r.fresh + r.cached for r in billed) / max(1, len(billed))),
        "observed": p1 - p0,
        "predicted": round(pred, 2),
        "error": round(pred - (p1 - p0), 2),
        "weekly_delta": (w1[0] - w0[0]) if (w0 and w1 and w0[1] and w1[1]
                                            and abs(w0[1] - w1[1]) <= 300) else None,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", help="把逐段结果写到这个文件")
    ap.add_argument("--min-delta", type=float, default=8)
    args = ap.parse_args(argv)

    coef = load_coefficients()
    sessions, requests = load()
    kinds = {k: sum(1 for s in sessions if s.kind == k) for k in ("normal", "subagent", "sandbox")}
    segs = [summarize(s, coef) for s in clean_segments(requests, coef, min_delta=args.min_delta)]
    print(f"会话 {len(sessions)} 个（普通 {kinds['normal']}、子 agent {kinds['subagent']}、"
          f"实验沙箱 {kinds['sandbox']}），请求 {len(requests):,} 次")
    print(f"可核对的真实片段 {len(segs)} 段（读数跨度 ≥ {args.min_delta:g}%、没撞上限、窗口没滚动、不混实验）\n")
    if not segs:
        return 0

    print(f"{'开始':<17}{'时长h':>6}{'请求':>5}{'会话':>5}{'平均上下文':>11}{'实测':>6}{'预测':>7}{'误差':>7}  模型")
    for s in sorted(segs, key=lambda x: x["day"]):
        print(f"{s['day']:<17}{s['hours']:>6.1f}{s['requests']:>5}{s['sessions']:>5}{s['avg_context']:>11,}"
              f"{s['observed']:>6.0f}{s['predicted']:>7.1f}{s['error']:>+7.1f}  {'/'.join(s['models'])}")

    errs = [s["error"] for s in segs]
    rel = [s["error"] / s["observed"] for s in segs]
    print(f"\n汇总：平均偏差 {statistics.mean(errs):+.2f} 个百分点，平均绝对误差 {statistics.mean(map(abs, errs)):.2f}，"
          f"相对误差中位数 {statistics.median(rel):+.0%}")
    for lo, hi, label in ((0, 60_000, "上下文 < 6 万"), (60_000, 120_000, "6 万 ~ 12 万"), (120_000, 10**9, "≥ 12 万")):
        b = [s for s in segs if lo <= s["avg_context"] < hi]
        if b:
            e = [s["error"] for s in b]
            print(f"  {label:<12} {len(b):>3} 段  平均偏差 {statistics.mean(e):+.2f}  "
                  f"相对 {statistics.median([s['error'] / s['observed'] for s in b]):+.0%}")
    print("  注意：并行会话花的额度也记在这些片段的读数里 —— 判断模型准不准见 intervals.py")
    ratios = [s["observed"] / s["weekly_delta"] for s in segs if s["weekly_delta"]]
    if ratios:
        print(f"\n同一片段里 5 小时读数跨度 / 周读数跨度：中位数 {statistics.median(ratios):.1f}（{len(ratios)} 段）")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(segs, f, ensure_ascii=False, indent=2)
        print(f"逐段结果写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
