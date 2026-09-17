#!/usr/bin/env python3
"""路线图 2.6：逐区间对账 —— 两个额度读数之间的所有请求，对上读数实际走的量。不花额度。

    python3 research/intervals.py

和 validate_real.py 的片段口径互补，用来回答「模型在真实使用里到底准不准」：

* 片段要求读数跨度 ≥ 8%，挑出来的都是长时间连续使用的时段 —— 而那种时段最容易
  同时开着好几个会话。并行时读数走的量没法归给某一个会话，片段口径会把别人花的
  算到这一段头上，看起来就是「模型算低了」。
* 区间口径用上每一对相邻读数（一万多个），并把「独占」和「并行」分开报：
  独占 = 区间前后 2 分钟内只有这一个会话在活动。

只读本机 ~/.codex，只输出聚合数字。
"""
from __future__ import annotations
import argparse, bisect, collections, time
from codex_logs import FREE, WIN_5H, codex_home, cost, load, load_coefficients

WINDOW = 120          # 判定「独占」时往前后各看这么久
CAP = 95              # 读数到这个百分比以上就可能被截断，不要


def intervals(requests, coef, exclusive_window=WINDOW):
    """把请求切成「相邻两个读数之间」的区间。

    返回 (ts, model, observed, predicted, n_requests, exclusive)。
    跳过：混了实验沙箱的窗口、混模型的区间、跨窗口的相邻读数、读数回落或撞上限。
    对齐方式和 refit / validate_real 一致：读数滞后一次请求，所以区间 [a, b) 的
    用量对应 a 到 b 之前的那些请求。
    """
    rs = sorted(requests, key=lambda r: r.ts)
    dirty = {r.reading[WIN_5H][1] for r in rs
             if r.kind == "sandbox" and r.reading and r.reading.get(WIN_5H)}
    rs = [r for r in rs if r.kind != "sandbox"]
    ts = [r.ts for r in rs]
    idx = [i for i, r in enumerate(rs) if r.reading and r.reading.get(WIN_5H)]

    def alone(t0, t1):
        lo = bisect.bisect_left(ts, t0 - exclusive_window)
        hi = bisect.bisect_right(ts, t1 + exclusive_window)
        return len({rs[i].session for i in range(lo, hi)}) == 1

    out = []
    for ia, ib in zip(idx, idx[1:]):
        a, b = rs[ia], rs[ib]
        (p0, reset0), (p1, reset1) = a.reading[WIN_5H], b.reading[WIN_5H]
        if reset0 != reset1 or reset0 in dirty or p1 >= CAP or p1 < p0:
            continue
        mid = rs[ia:ib]
        models = {r.model for r in mid}
        if len(models) != 1:
            continue
        model = models.pop()
        if model in FREE or model not in coef:
            continue
        out.append((a.ts, model, p1 - p0, sum(cost(r, coef) for r in mid),
                    len(mid), alone(a.ts, b.ts)))
    return out


def table(title, rows, key):
    g = collections.defaultdict(lambda: [0, 0, 0.0, 0.0])
    for r in rows:
        t = g[key(r)]
        t[0] += 1; t[1] += r[4]; t[2] += r[2]; t[3] += r[3]
    print(f"\n{title}")
    print(f"  {'':<16}{'区间':>7}{'请求':>7}{'实测':>9}{'预测':>9}{'比值':>7}")
    for k in sorted(g):
        n, q, o, p = g[k]
        print(f"  {k:<16}{n:>7}{q:>7}{o:>9.0f}{p:>9.0f}{(o / p if p else 0):>7.3f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window", type=int, default=WINDOW,
                    help="判定独占时前后各看多少秒（默认 %(default)s）")
    args = ap.parse_args(argv)

    coef = load_coefficients()
    _, requests = load()
    rows = intervals(requests, coef, args.window)
    solo = [r for r in rows if r[5]]
    para = [r for r in rows if not r[5]]
    print(f"日志目录 {codex_home()}")
    print(f"可对账区间 {len(rows):,} 个（独占 {len(solo):,}、有并行会话 {len(para):,}）")
    for name, sel in (("独占", solo), ("并行", para)):
        o, p = sum(r[2] for r in sel), sum(r[3] for r in sel)
        print(f"  {name}：实测 {o:,.0f}%  预测 {p:,.0f}%  比值 {(o / p if p else 0):.3f}")
    print("\n并行时读数走的量没法归给某一个会话 —— 那一列的偏差是归属问题，不是系数问题。")

    month = lambda r: time.strftime("%Y-%m", time.localtime(r[0]))
    table("独占时段，按月：", solo, month)
    table("独占时段，按模型：", solo, lambda r: r[1].replace("gpt-", ""))
    table("有并行会话，按月：", para, month)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
