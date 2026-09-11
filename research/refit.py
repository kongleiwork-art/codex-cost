#!/usr/bin/env python3
"""refit — 用 results.jsonl 重新核对 codex-cost 的成本系数。

quota_probe.py 负责**采集**，这个脚本负责**核对**：把已有的实验数据重算一遍，
回答「仓库里现在这套系数，跟自己的数据对得上吗」。

跑出来的结论（截至 421 条记录）：

  1. 对不上，而且是单边高估。sol 上当前系数的 RMS 是 1.48，重拟合能到 0.36；
     astra 是 1.60 对 0.55。主因是「每请求固定成本」偏高 —— 即使把 cached
     费率钉死在 677,444，最佳的 request 系数也只有 0.048，不是 0.0667。

  2. cached 费率在这批数据里**不可辨识**。固定它做剖面，从 18 万到「完全免费」
     整条区间的 RMS 都在 0.36~0.45，而 1% 量化噪声的下限就有 0.29。
     原因是 cached 与请求数共线（r=+0.949）—— 每次请求都拖着一份上下文，
     「缓存贵」和「每请求贵」在这批数据里是同一个信号。

  3. 更麻烦的是两个区间互相矛盾。已提交的实验全在「每次约 1.6 万缓存」，
     而 README 引用的真实长会话是「每次约 15 万」。用前者拟合的系数预测后者，
     高估约 47 个百分点 —— 说明「成本线性于 cached」这个形式本身就可疑。

  破法见 quota_probe.py 里的 req/* 和 ctx/* 两组实验：前者让 cached 总量相等
  而请求数差 4 倍，直接分离出每请求成本；后者固定请求数扫上下文规模，测线性。

用法：
    python3 refit.py              # 全部核对
    python3 refit.py --model gpt-6-astra

零依赖，标准库，Python 3.8+。
"""
from __future__ import annotations
import argparse, collections, datetime as dt, itertools, json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results.jsonl")
WIN_5H = "300"

# 仓库当前在用的系数（Sources/Budget.swift / cli/codex_budget.py）
SHIPPED = {
    "gpt-5.6-sol":   (41_398, 677_444, 15_450, 0.0667),
    "gpt-5.5":       (48_137, 787_521, 17_965, 0.0574),
    "gpt-5.6-terra": (46_000, 752_560, 17_167, 0.0600),
    "gpt-6-astra":   (15_415, 252_190,  2_495, 0.3514),
    "gpt-5.6-luna":  None,
}
# used_percent 只有 1% 分辨率。均匀量化误差的标准差是 1/sqrt(12)，
# 任何 RMS 低于这个数的「拟合优度差异」都是在拟合噪声，不必当真。
NOISE_FLOOR = 1 / math.sqrt(12)


# ── 读数据 ───────────────────────────────────────────────────────────────
def load(path=RESULTS):
    if not os.path.exists(path):
        sys.exit(f"没有 {path}，先跑 quota_probe.py run")
    rows = []
    for line in open(path, encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def when(r):
    return dt.datetime.fromisoformat(
        (r.get("quota_ts") or r["started"]).replace("Z", "+00:00"))


def pct5h(r):
    q = r.get("quota") or {}
    w = q.get(WIN_5H) or q.get(int(WIN_5H)) or {}
    return w.get("used_percent")


def increments(rows):
    """还原每次请求的真实增量。

    这是整个脚本的地基，也是之前算错的地方：scan_rollout() 把一个 rollout 里
    所有 token_count 的 last_token_usage 求和，而续会话组每轮追加到同一个文件，
    所以第 N 条记录装的是前 N 次的**累计**。直接按 trial 求和/取中位数会把
    缓存量放大好几倍。这里按 session 分组，用相邻累计值之差还原增量。
    """
    ok = [r for r in rows
          if r.get("ok") and r.get("tokens") and pct5h(r) is not None
          and r.get("phase", "measure") == "measure"]
    by_sess = collections.defaultdict(list)
    for r in ok:
        by_sess[r.get("session_id") or r.get("rollout")].append(r)

    out = []
    for rs in by_sess.values():
        rs.sort(key=lambda r: (r.get("events") or 0, r.get("trial") or 0))
        prev = (0, 0, 0)
        for r in rs:
            t = r["tokens"]
            cum = (t["input_tokens"] - t["cached_input_tokens"],
                   t["cached_input_tokens"],
                   t["output_tokens"] + t["reasoning_output_tokens"])
            d = tuple(max(0, cum[k] - prev[k]) for k in range(3)) if len(rs) > 1 else cum
            prev = cum
            out.append(dict(t=when(r), cell=r["cell"], model=r["model"],
                            f=d[0], c=d[1], o=d[2], p=pct5h(r)))
    out.sort(key=lambda x: x["t"])
    return out


def runs_for(inc, model, min_len=6):
    """同一模型、连续、5h 窗口未回绕的请求序列。

    期间只要混进别的模型就把这一段作废 —— rate_limits 是全局读数，
    别的模型在同一窗口里的消耗会直接算到这一段的 Δ 上。
    """
    out, cur = [], None
    for x in inc:
        if x["model"] != model:
            if cur:
                out.append(cur)
            cur = None
            continue
        if cur is None:
            cur = []
        if cur and ((x["t"] - cur[-1]["t"]).total_seconds() > 3600 or x["p"] < cur[-1]["p"]):
            out.append(cur)
            cur = []
        cur.append(x)
    if cur:
        out.append(cur)
    return [r for r in out if len(r) >= min_len]


def design(inc, model, every=3, warm=6):
    """回归矩阵：沿每段的累计轨迹取点，(fresh, cached, output, 请求数) → Δ%。

    用累计点而不是单次增量，是因为单次的 Δ 几乎总是 0 或 1，全是量化噪声；
    累计之后 Δ 能覆盖 1~34%，斜率才定得住。
    """
    A, y = [], []
    for run in runs_for(inc, model):
        cf = cc = co = 0
        p0 = run[0]["p"]
        for k, x in enumerate(run, 1):
            cf += x["f"]; cc += x["c"]; co += x["o"]
            if k >= warm and k % every == 0:
                A.append([cf, cc, co, k])
                y.append(x["p"] - p0)
    return A, y


# ── 拟合 ─────────────────────────────────────────────────────────────────
def lstsq(A, y, cols):
    """正规方程 + 高斯消元，只对 cols 里的列求解，其余固定为 0。"""
    n = len(cols)
    if n == 0:
        return [0.0] * 4
    m = len(A)
    M = [[sum(A[i][a] * A[i][b] for i in range(m)) for b in cols] for a in cols]
    v = [sum(A[i][a] * y[i] for i in range(m)) for a in cols]
    for a in range(n):
        piv = max(range(a, n), key=lambda r: abs(M[r][a]))
        M[a], M[piv] = M[piv], M[a]
        v[a], v[piv] = v[piv], v[a]
        if abs(M[a][a]) < 1e-20:
            return None
        for r in range(n):
            if r == a:
                continue
            k = M[r][a] / M[a][a]
            for cidx in range(a, n):
                M[r][cidx] -= k * M[a][cidx]
            v[r] -= k * v[a]
    x = [0.0] * 4
    for i, cidx in enumerate(cols):
        x[cidx] = v[i] / M[i][i]
    return x


def rms(A, y, x):
    return math.sqrt(sum((sum(A[i][j] * x[j] for j in range(4)) - y[i]) ** 2
                         for i in range(len(y))) / len(y))


def nnls(A, y, allowed):
    """非负最小二乘。只有 4 个参数，直接枚举 16 种活跃集求精确解。

    非负约束不是装饰：不加的话自由拟合会给出「fresh 为负」这种解 ——
    在共线的数据上它拟合得更好，但物理上不可能。
    """
    best = None
    for k in range(len(allowed), -1, -1):
        for cols in itertools.combinations(allowed, k):
            x = lstsq(A, y, list(cols))
            if x is None or any(v < -1e-12 for v in x):
                continue
            r = rms(A, y, x)
            if best is None or r < best[1]:
                best = (x, r)
    return best


def fmt_rate(v):
    return "免费" if v <= 1e-12 else f"{1 / v:,.0f}"


# ── 各节报告 ──────────────────────────────────────────────────────────────
def section_bursts(inc, rows):
    print("\n【1】按 burst 汇总：观测 Δ vs 当前系数")
    print("    burst = 连续的一段实验（间隔 >1 小时就断开）。混合模型的段跳过。")
    bursts, cur = [], []
    for x in inc:
        if cur and (x["t"] - cur[-1]["t"]).total_seconds() > 3600:
            bursts.append(cur); cur = []
        cur.append(x)
    if cur:
        bursts.append(cur)
    print(f"\n    {'burst':<6}{'模型':<12}{'请求':>5}{'fresh':>11}{'cached':>12}"
          f"{'output':>9}{'观测Δ':>7}{'预测':>8}{'误差':>8}")
    print("    " + "-" * 82)
    for i, b in enumerate(bursts):
        models = {x["model"] for x in b}
        obs = max(x["p"] for x in b) - min(x["p"] for x in b)
        f = sum(x["f"] for x in b); c = sum(x["c"] for x in b)
        o = sum(x["o"] for x in b); n = len(b)
        if len(models) > 1:
            print(f"    {i:<6}{'混合':<12}{n:>5}{f:>11,}{c:>12,}{o:>9,}{obs:>7.0f}"
                  f"{'—':>8}{'—':>8}")
            continue
        m = models.pop()
        sh = SHIPPED.get(m)
        if not sh:
            print(f"    {i:<6}{m.replace('gpt-',''):<12}{n:>5}{f:>11,}{c:>12,}"
                  f"{o:>9,}{obs:>7.0f}{0.0:>8.2f}{-obs:>+8.2f}   （标为免费）")
            continue
        pr = f / sh[0] + c / sh[1] + o / sh[2] + sh[3] * n
        print(f"    {i:<6}{m.replace('gpt-',''):<12}{n:>5}{f:>11,}{c:>12,}{o:>9,}"
              f"{obs:>7.0f}{pr:>8.2f}{pr - obs:>+8.2f}")


def section_forms(A, y, model):
    print(f"\n【2】{model}：不同模型形式的拟合（{len(A)} 个回归点，"
          f"Δ 覆盖 {min(y):.0f}~{max(y):.0f}%）")
    print(f"\n    {'形式':<20}{'fresh/1%':>12}{'cached/1%':>13}{'output/1%':>12}"
          f"{'每请求%':>10}{'RMS':>8}{'留一CV':>9}")
    print("    " + "-" * 84)
    forms = (("全放开（非负）", [0, 1, 2, 3]),
             ("强制 cached 免费", [0, 2, 3]),
             ("强制无每请求成本", [0, 1, 2]),
             ("只有 fresh+output", [0, 2]))
    for name, allowed in forms:
        got = nnls(A, y, allowed)
        if not got:
            print(f"    {name:<20} 奇异，无解")
            continue
        x, r = got
        loo = []
        for k in range(len(A)):
            Ak = [A[i] for i in range(len(A)) if i != k]
            yk = [y[i] for i in range(len(y)) if i != k]
            gk = nnls(Ak, yk, allowed)
            if gk:
                loo.append((sum(A[k][j] * gk[0][j] for j in range(4)) - y[k]) ** 2)
        cv = math.sqrt(sum(loo) / len(loo)) if loo else float("nan")
        print(f"    {name:<20}{fmt_rate(x[0]):>12}{fmt_rate(x[1]):>13}"
              f"{fmt_rate(x[2]):>12}{x[3]:>+10.4f}{r:>8.3f}{cv:>9.3f}")
    sh = SHIPPED.get(model)
    if sh:
        xs = [1 / sh[0], 1 / sh[1], 1 / sh[2], sh[3]]
        print(f"    {'仓库当前系数':<20}{sh[0]:>12,}{sh[1]:>13,}{sh[2]:>12,}"
              f"{sh[3]:>+10.4f}{rms(A, y, xs):>8.3f}{'—':>9}")
    print(f"\n    1% 量化噪声对应的 RMS 下限 ≈ {NOISE_FLOOR:.3f}"
          " —— 低于它的差异没有意义。")
    diagnose(A, y, forms)


def diagnose(A, y, forms):
    """别把重拟合当成答案 —— 先说清楚这批数据到底能定下哪几项。

    两个检查：
      去列检验 —— 把某一项强制为 0，RMS 几乎不变，就说明数据根本不需要
                  这一项，它的系数是拟合出来的，不是测出来的。
      形式简并 —— 几种模型形式的 RMS 差不多，说明数据分不开它们，
                  此时"最佳拟合"只是一族等价解里的一个点。
    """
    full = nnls(A, y, [0, 1, 2, 3])
    if not full:
        return
    x, r0 = full
    names = ("fresh", "cached", "output", "每请求")
    print("\n    去列检验（把某一项强制为 0，看 RMS 退化多少）：")
    for j, name in enumerate(names):
        got = nnls(A, y, [k for k in range(4) if k != j])
        if not got:
            continue
        dr = got[1] - r0
        # 这一项在所有观测点里最多能解释多少成本 —— 给退化量一个量纲参照
        share = 0.0
        for i in range(len(A)):
            tot = sum(A[i][k] * x[k] for k in range(4))
            if tot > 0:
                share = max(share, A[i][j] * x[j] / tot)
        if dr > NOISE_FLOOR / 4:
            verdict = "测得动"
        elif share > 0.20:
            # 扛着一大半成本、去掉却毫发无损 —— 共线的典型特征：
            # 这一项的份额可以整个让给别的项，拟合一样好。
            verdict = "与其它项共线 —— 去掉由别项顶上，系数只是一族解里的一个"
        else:
            verdict = "数据不需要这一项 —— 系数不可信"
        print(f"      去掉 {name:<7} RMS {got[1]:.3f} ({dr:+.3f})  "
              f"最多解释 {share*100:>4.0f}% 的成本   {verdict}")

    notes = []
    rs = [g for g in (nnls(A, y, a) for _, a in forms) if g]
    if len(rs) >= 2:
        spread = max(g[1] for g in rs) - min(g[1] for g in rs)
        if spread < NOISE_FLOOR / 2:
            notes.append(f"几种形式的 RMS 只差 {spread:.3f}（< 噪声的一半）"
                         "—— 这批数据分不开它们，别拿最佳拟合当结论")
    if len(A) < 15:
        notes.append(f"只有 {len(A)} 个回归点，样本太少")
    for n in notes:
        print(f"      ⚠ {n}")


def section_profile(A, y, model):
    print(f"\n【3】{model}：固定 cached 费率，其余三项重新非负拟合")
    print("    如果剖面是平的，说明这批数据定不了 cached 费率。")
    print(f"\n    {'cached tok/1%':>16}{'fresh/1%':>12}{'output/1%':>12}"
          f"{'每请求%':>10}{'RMS':>8}")
    print("    " + "-" * 60)
    sh = SHIPPED.get(model)
    rates = [120_000, 180_000, 250_000, 350_000, 500_000, 700_000, 1_000_000, None]
    if sh:
        rates = sorted(set(rates[:-1]) | {sh[1]}) + [None]
    for rate in rates:
        cr = 0.0 if rate is None else 1 / rate
        yy = [y[i] - A[i][1] * cr for i in range(len(A))]
        got = nnls(A, yy, [0, 2, 3])
        if not got:
            continue
        x = list(got[0]); x[1] = cr
        tag = "  ← 仓库当前" if sh and rate == sh[1] else ""
        print(f"    {('免费' if rate is None else f'{rate:,}'):>16}"
              f"{fmt_rate(x[0]):>12}{fmt_rate(x[2]):>12}{x[3]:>+10.4f}"
              f"{rms(A, y, x):>8.3f}{tag}")


def section_regime(inc, A, y):
    """已提交实验 vs README 引用的真实长会话 —— 两个区间对不上。"""
    print("\n【4】区间冲突：实验区间 vs 真实长会话")
    sol = [x for x in inc if x["model"] == "gpt-5.6-sol"]
    per_req = sum(x["c"] for x in sol) / max(1, len(sol))
    print(f"\n    已提交的 sol 实验：{len(sol)} 次请求，平均每次 {per_req:,.0f} 缓存 token")

    # README 的三个数：当前系数预测 80.7%、缓存按免费算 47.9%、实测 82%。
    # 两者之差就是缓存项，据此反推那条会话的缓存总量。
    pred_now, pred_free, observed, reqs = 80.7, 47.9, 82.0, 148
    cached_tot = (pred_now - pred_free) * SHIPPED["gpt-5.6-sol"][1]
    print(f"    README 引用的真实会话：{reqs} 次请求，反推缓存约 "
          f"{cached_tot/1e6:.1f}M token，每次约 {cached_tot/reqs:,.0f}")
    print(f"    → 两者相差 {cached_tot/reqs/per_req:.0f} 倍，实验根本没覆盖到那个区间。")

    got = nnls(A, y, [0, 1, 2, 3])
    if not got:
        return
    x = got[0]
    # 非缓存部分按当前系数算出来是 pred_free 减掉每请求项，再按新费率折算
    fo_now = pred_free - SHIPPED["gpt-5.6-sol"][3] * reqs
    fo_new = fo_now * (SHIPPED["gpt-5.6-sol"][0] * x[0])   # 粗略按 fresh 费率缩放
    pred_refit = cached_tot * x[1] + fo_new + x[3] * reqs
    print(f"\n    {'':<22}{'预测':>8}{'实测':>8}")
    print(f"    {'当前系数':<22}{pred_now:>8.1f}{observed:>8.1f}")
    print(f"    {'按实验数据重拟合':<22}{pred_refit:>8.1f}{observed:>8.1f}"
          f"   高估 {pred_refit - observed:.0f} 个百分点")
    print("\n    同一个线性形式没法同时拟合两个区间 —— 这是形式错了，不是噪声。")
    print("    （那条真实会话的原始 rollout 不在仓库里，以上由 README 的三个数反推。）")


def section_gaps(rows):
    print("\n【5】数据缺口")
    cells = collections.Counter(r["cell"] for r in rows if r.get("ok"))
    for cell, note in (("cache/bigctx", "README 用它定下 677,444，但一条结果都没有"),
                       ("req/many", "分离每请求成本，待跑"),
                       ("req/few", "分离每请求成本，待跑"),
                       ("ctx/200k", "测缓存线性，待跑")):
        n = cells.get(cell, 0)
        print(f"    {cell:<16}{n:>4} 条   {note}")
    zero = [r["tokens"].get("cache_write_input_tokens", 0)
            for r in rows if r.get("ok") and r.get("tokens")]
    print(f"\n    cache_write_input_tokens：{len(zero)} 条里 "
          f"{sum(1 for v in zero if v)} 条非零 —— 排除它是缺失项的可能。")


def main():
    ap = argparse.ArgumentParser(prog="refit",
        description="用 results.jsonl 重新核对成本系数")
    ap.add_argument("--model", default="gpt-5.6-sol", help="重点分析哪个模型")
    ap.add_argument("--results", default=RESULTS)
    args = ap.parse_args()

    rows = load(args.results)
    inc = increments(rows)
    print(f"记录 {len(rows)} 条，可用请求 {len(inc)} 次，"
          f"{min(x['t'] for x in inc):%m-%d} ~ {max(x['t'] for x in inc):%m-%d}")

    section_bursts(inc, rows)
    for model in dict.fromkeys([args.model] + list(SHIPPED)):
        if SHIPPED.get(model) is None and model != args.model:
            continue
        A, y = design(inc, model)
        if len(A) < 8:
            print(f"\n【2】{model}：可用回归点仅 {len(A)} 个，不足以拟合，跳过")
            continue
        section_forms(A, y, model)
        if model == args.model:
            section_profile(A, y, model)
            section_regime(inc, A, y)
    section_gaps(rows)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
