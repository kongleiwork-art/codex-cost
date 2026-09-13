#!/usr/bin/env python3
"""refit — 用 results.jsonl 重新核对 codex-cost 的成本系数。

quota_probe.py 负责**采集**，这个脚本负责**核对**：把已有的实验数据重算一遍，
回答「仓库里现在这套系数，跟自己的数据对得上吗」。

跑出来的结论（481 条记录，含 cache/bigctx）：

  1. 旧系数对不上，而且是单边高估：sol 上 RMS 1.458，联合重拟合 0.494（1% 量化
     噪声下限 0.289）。原因是缓存项是后加的、其余系数没重拟合 —— 「每请求成本」
     和缓存共线，旧的 0.0667 里本来就吸收了一部分缓存成本，再加一项等于重复计费。

  2. 缓存费率在完整数据里**测得动**：去掉这一项 RMS 从 0.494 升到 3.107，剖面最优
     在 50 万附近（677,444 时已升到 0.911）。只看不含 bigctx 的 421 条会得出
     「不可辨识」—— 那批实验全在每次约 1.6 万缓存的小区间里。

  3. 同理，只用 421 条拟合去预测真实长会话会高估到 128%，像是线性形式不成立；补上
     bigctx 后是 79.5%，实测 82%。矛盾来自数据缺区间，不是形式错了。

  4. astra 的缓存费率在现有数据里确实不可辨识（剖面 0.547~0.548 完全平），需要一组
     astra 的大上下文实验。

  仍未拆干净的是 sol 的 fresh 与「每请求」—— 剖面上两者此消彼长。quota_probe.py 里的
  req/* 让缓存总量相等而请求数差 4 倍，正是为此设计；ctx/* 固定请求数扫上下文规模，
  直接检验缓存是否线性。

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
    "gpt-5.6-sol":   (66_457, 508_494, 15_196, 0.0819),
    "gpt-5.5":       (77_276, 591_272, 17_670, 0.0704),
    "gpt-5.6-terra": (73_841, 564_993, 16_884, 0.0737),
    "gpt-6-astra":   (27_567, 252_190,  2_566, 0.4814),
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
    # 预热轮也要参与求差：它和测量轮写在同一个会话文件里，累计值里有它。
    # 只拿测量轮分组的话，第一条测量的「增量」会吞下整段预热的 token，而那部分
    # 额度在起点读数之前就花掉了 —— req/few 这种 20 万的预热会凭空多出约 18 万
    # fresh，把 fresh 系数压低。所以先全部求差，再只输出测量轮。
    ok = [r for r in rows
          if r.get("ok") and r.get("tokens")
          and (pct5h(r) is not None or r.get("phase", "measure") != "measure")]
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
            if r.get("phase", "measure") != "measure":
                continue
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
    print("    burst = 连续的一段实验（间隔 >1 小时或额度读数回落就断开）。混合模型的段跳过。")
    bursts, cur = [], []
    for x in inc:
        # 额度读数回落 = 窗口重置。跨重置的一段，max−min 会漏掉重置后的消耗，
        # 而请求数照算 —— 汇总表会凭空显示成「高估」。astra/clean 就踩过。
        if cur and ((x["t"] - cur[-1]["t"]).total_seconds() > 3600
                    or x["p"] < cur[-1]["p"]):
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
    """已提交实验 vs 真实长会话 —— 检验两个区间能否用同一套系数描述。"""
    print("\n【4】区间检验：实验区间 vs 真实长会话")
    sol = [x for x in inc if x["model"] == "gpt-5.6-sol"]
    per_req = sum(x["c"] for x in sol) / max(1, len(sol))
    print(f"\n    已提交的 sol 实验：{len(sol)} 次请求，平均每次 {per_req:,.0f} 缓存 token")

    # 这条会话的原始 rollout 不在仓库里，但总量是直接从本机日志统计的
    # （148 次请求，5h 读数 0% → 82%，全程 sol，未跨重置）。之前是用 README 里
    # 两个预测值之差反推缓存总量，系数一换就推错 —— 曾推成 16.7M，真实是 22.3M。
    fresh_tot, cached_tot, out_tot, reqs, observed = 1_329_467, 22_266_112, 90_817, 148, 82.0
    sh = SHIPPED["gpt-5.6-sol"]
    pred_now = fresh_tot / sh[0] + cached_tot / sh[1] + out_tot / sh[2] + sh[3] * reqs
    print(f"    真实长会话：{reqs} 次请求，缓存 {cached_tot/1e6:.1f}M token，"
          f"每次约 {cached_tot/reqs:,.0f}")
    print(f"    → 两者相差 {cached_tot/reqs/per_req:.0f} 倍，（这是全部 sol 实验的平均；有没有覆盖到，要看是否跑了大上下文组。）")

    got = nnls(A, y, [0, 1, 2, 3])
    if not got:
        return
    x = got[0]
    # x 依次是 fresh / cached / output / 每请求 的「每单位百分比」
    pred_refit = fresh_tot * x[0] + cached_tot * x[1] + out_tot * x[2] + reqs * x[3]
    print(f"\n    {'':<22}{'预测':>8}{'实测':>8}")
    print(f"    {'当前系数':<22}{pred_now:>8.1f}{observed:>8.1f}")
    print(f"    {'按实验数据重拟合':<22}{pred_refit:>8.1f}{observed:>8.1f}"
          f"   偏差 {pred_refit - observed:+.0f} 个百分点")
    # 结论要跟着数字走，不能写死
    if abs(pred_refit - observed) > max(5.0, observed * 0.1):
        print("\n    重拟合的系数对不上这条会话 —— 要么实验缺了这个区间，要么线性形式不成立。")
    else:
        print("\n    重拟合的系数能对上这条会话 —— 线性形式在这个区间内站得住。")
    print("    （那条真实会话的原始 rollout 不在仓库里，总量取自本机日志统计。）")


def section_pairs(inc, model="gpt-5.6-sol"):
    """req/many 与 req/few 成对比较，不经联合拟合直接解「每请求成本」。

    两组的缓存总量设计成相等、请求数差 4 倍，fresh 和输出都很小。于是
        Δ实测(many) − Δ实测(few) ≈ R × (请求数之差) + 残余 token 部分之差
    残余部分按现行 F/C/O 扣掉，剩下的就是 R。

    读数滞后一次（第 k 次的费用要到第 k+1 次的读数里才出现，见联合拟合里
    「滞后」对齐 RMS 最低），所以第 1..n 次的读数差对应第 1..n−1 次的 token。
    """
    print("\n【5】req/many vs req/few：直接解每请求成本")
    F, C, O, R = SHIPPED[model]
    got = {}
    for cell in ("req/many", "req/few"):
        xs = sorted((x for x in inc if x["cell"] == cell), key=lambda x: x["t"])
        if len(xs) < 4:
            print(f"    {cell:<10} 可用 {len(xs)} 次，不够")
            continue
        if any(b["p"] < a["p"] for a, b in zip(xs, xs[1:])):
            print(f"    {cell:<10} 读数中途回落（窗口滚动），这组不能直接用")
            continue
        seg = xs[:-1]
        f, c, o = (sum(x[k] for x in seg) for k in "fco")
        n, dp = len(seg), xs[-1]["p"] - xs[0]["p"]
        got[cell] = (f, c, o, n, dp)
        pred = f / F + c / C + o / O + R * n
        print(f"    {cell:<10} {n:>3} 次  fresh {f:>8,}  cached {c:>10,}  out {o:>5,}"
              f"   实测 Δ{dp:>3}%   按现行系数 {pred:5.1f}%")
    if len(got) < 2:
        print("    两组都跑完才能解")
        return
    (f1, c1, o1, n1, d1), (f2, c2, o2, n2, d2) = got["req/many"], got["req/few"]
    tok = (f1 - f2) / F + (c1 - c2) / C + (o1 - o2) / O
    r_hat = (d1 - d2 - tok) / (n1 - n2)
    band = 2 / abs(n1 - n2)          # 两个读数差各有 ±1 的截断误差
    print(f"    两组 cached 相差 {abs(c1 - c2) / max(c1, c2):.0%}；扣掉 token 部分 {tok:+.2f}% 后")
    print(f"    每请求成本 ≈ {r_hat:.3f}%（量化误差 ±{band:.3f}），现行 {R}")


def section_gaps(rows):
    print("\n【6】数据覆盖")
    cells = collections.Counter(r["cell"] for r in rows
                                if r.get("ok") and r.get("phase", "measure") == "measure")
    for cell, note in (("cache/bigctx", "定缓存费率"),
                       ("req/many", "分离每请求成本"),
                       ("req/few", "分离每请求成本"),
                       ("ctx/200k", "测缓存是否线性")):
        n = cells.get(cell, 0)
        print(f"    {cell:<16}{n:>4} 条   {note}{'' if n else '，待跑'}")
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
    section_pairs(inc)
    section_gaps(rows)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
