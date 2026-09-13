#!/usr/bin/env python3
"""quota_probe — 受控实验：测出 Codex 订阅额度到底按什么计费。

为什么需要它：观测性数据里 cached / output / 请求数 三者高度共线
（实测 r = 0.83~0.95），所以无论收集多久的真实开发数据，都分不开
"是缓存便宜" 还是 "按请求计费"。只有固定其余条件、单独改一个变量，
才能把它们拆开。

用法：
    python3 quota_probe.py plan                    # 只打印实验矩阵，不消耗任何额度
    python3 quota_probe.py run --yes --budget 15   # 真正执行，最多烧掉 15% 的 5h 额度
    python3 quota_probe.py analyze                 # 分析已收集的结果

结果追加写入 results.jsonl，可随时中断续跑。零依赖，标准库，Python 3.8+。
"""
from __future__ import annotations
import argparse, glob, json, os, subprocess, sys, time
from datetime import datetime, timezone

__version__ = "0.1.0"
HOME = os.path.expanduser("~")
SESS_GLOB = os.path.join(HOME, ".codex/sessions/**/rollout-*.jsonl")
CODEX_CANDIDATES = [
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    "/usr/local/bin/codex", "/opt/homebrew/bin/codex",
    os.path.join(HOME, ".local/bin/codex"),
]
HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results.jsonl")
WIN_5H, WIN_WEEK = 300, 10080

def find_codex():
    for p in CODEX_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    from shutil import which
    return which("codex")

# ── 实验矩阵 ─────────────────────────────────────────────────────────────
# 每一组内部只变一个维度，其余全部固定 —— 这是能解开共线性的唯一方式。
BASE_MODEL  = "gpt-5.6-sol"
BASE_EFFORT = "medium"

TINY   = ("Reply with exactly the word OK. Do not use any tools. "
          "Do not explain. Do not read any files.")
LONG   = ("Write exactly 600 words explaining why merge sort is stable and "
          "quicksort is not. Plain prose, no lists, no tools, no file reads.")
# effort 组必须用真正触发推理的任务。
# 试过两版都失败：「回一个 OK」不触发推理；经典斑马谜题被模型背了下来
# （output 仅 5 token，reasoning 0）。必须是不可能记住的搜索型问题。
# 实测本题在 effort=high 下产生 823 个 reasoning token。
REASON = ("Without using any tools, work this out by hand and show no working — "
          "give only the final answer.\n"
          "Consider every 5-digit number whose digits are strictly increasing "
          "(each digit larger than the one before) and whose digit sum is exactly 27. "
          "How many such numbers are there, and what is the largest one? "
          "Answer in the form: count=N largest=M")

# 让输出侧主导成本：prompt 本身很短（fresh 低），但要求长篇输出 + 推理。
VERBOSE = ("Explain, in about 900 words of flowing prose with no lists and no "
           "headings, why merge sort is stable while quicksort is not, what "
           "stability actually costs in memory and time, and when a practitioner "
           "should care. Work through the reasoning carefully before writing.")

def padded(kb):
    """把同一个任务塞进不同大小的上下文里 —— 只变 input tokens。"""
    filler = ("The quick brown fox jumps over the lazy dog. " * 24 + "\n") * kb
    return (f"<reference-material>\n{filler}</reference-material>\n"
            "Ignore the reference material entirely. Reply with exactly the "
            "word OK. Do not use any tools.")

# padded() 的换算，实测自 results.jsonl：TINY 空跑是 20,008 个 input token
# （系统提示 + 工具定义），padded(kb) 每个 kb 单位再加 241 个。
# input/1k→20,253、input/20k→24,832、input/80k→39,292，三点都落在这条直线上。
BASE_CTX_TOKENS = 20_008
TOK_PER_UNIT    = 241
# 一个 padded 单位约 1.06KB，prompt 是作为命令行参数传给 codex 的。
# 400 单位约 430KB。macOS 的 ARG_MAX 是 1MB，单轮其实塞得下 —— 早前的
# cache/bigctx 就是一轮塞完跑的，60 条结果都在（当初只是没同步进仓库）。
# 分几轮喂仍然更稳：离上限远一点，也方便把预热轮单独标记出来。
MAX_UNITS_PER_TURN = 200

def ctx_seed(target_tokens):
    """返回把上下文撑到约 target_tokens 所需的预热 prompt 列表。

    分多轮喂不是必须的（1MB 的 ARG_MAX 放得下 430KB），但更稳。预热轮记 phase=warmup，
    分析时不计入测量段，这样"撑上下文烧掉的 fresh"不会污染组间对比。
    """
    units = max(0, round((target_tokens - BASE_CTX_TOKENS) / TOK_PER_UNIT))
    out = []
    while units > 0:
        n = min(units, MAX_UNITS_PER_TURN)
        out.append(padded(n))
        units -= n
    return out

def matrix():
    C = []
    # 对照组：隔天/隔周续跑时先重测它。若换算率和上次不一致，
    # 说明官方改了计费口径，之前的数据不能和新数据混用。
    C.append(dict(cell="control", prompt=TINY, model=BASE_MODEL, effort=BASE_EFFORT,
                  resume=False, trials=20,
                  why="对照组，用来验证计费口径没变"))
    # A —— 缓存：同一个 prompt，冷启动 vs 复用会话吃缓存
    C.append(dict(cell="cache/cold", prompt=TINY, model=BASE_MODEL, effort=BASE_EFFORT,
                  resume=False, trials=12,
                  why="冷启动，cached_input≈0"))
    C.append(dict(cell="cache/warm", prompt=TINY, model=BASE_MODEL, effort=BASE_EFFORT,
                  resume=True, trials=12,
                  why="首次建会话，其后全部续同一个会话吃缓存"))
    # B —— reasoning effort：其余全部相同
    # 注意：effort/* 用的就是 BASE_MODEL（gpt-5.6-sol），
    # 与下面 astra/* 构成两个模型的同构网格，可直接横向对比。
    for eff in ("low", "medium", "high", "xhigh", "max"):
        C.append(dict(cell=f"effort/{eff}", prompt=REASON, model=BASE_MODEL, effort=eff,
                      resume=False, trials=8,
                      why="只变 reasoning effort（用真正需要推理的任务）"))
    # C —— 输出长度：input 基本不变，output 差一个量级
    C.append(dict(cell="output/short", prompt=TINY, model=BASE_MODEL, effort=BASE_EFFORT,
                  resume=False, trials=8, why="output≈1 token"))
    C.append(dict(cell="output/long", prompt=LONG, model=BASE_MODEL, effort=BASE_EFFORT,
                  resume=False, trials=8, why="output≈800 tokens"))
    # D —— 输入长度：output 固定，input 阶梯变化
    for kb in (1, 20, 80):
        C.append(dict(cell=f"input/{kb}k", prompt=padded(kb), model=BASE_MODEL,
                      effort=BASE_EFFORT, resume=False, trials=6,
                      why=f"约 {kb}KB 填充，output 固定"))
    # I —— 缓存到底收不收费。
    # 真实使用中发现反例：148 次请求、每轮扛约 15 万上下文的会话实测 82%，
    # 模型（缓存按零计价）只算 48%，缺口约对应 29 万缓存 token/1%。
    # （注意 cache/warm 那组曾被读成"上下文 26 万"，那是累计求和的假象 ——
    #  它每轮真实上下文只有约 2 万，根本没进大上下文区间。）
    # 这组专测大上下文持续场景：分几轮把上下文撑到约 15 万，
    # 之后每轮只发一个字，fresh 几乎不涨，缓存量线性累积。
    C.append(dict(cell="cache/bigctx", prompt=TINY, warmup=ctx_seed(150_000),
                  model=BASE_MODEL, effort=BASE_EFFORT, resume=True, trials=60,
                  why="大上下文持续场景，定缓存费率"))
    # J —— 把「每请求固定成本」和「缓存成本」拆开。
    #
    # 在不含 cache/bigctx 的 421 条数据里，cached 与请求数 r=+0.949，缓存费率
    # 确实定不下来（剖面从 18 万到「免费」RMS 都在 0.36~0.45）。补上 bigctx 那
    # 60 条后缓存被强识别（去掉这一项 RMS +2.6，最优约 50 万），但 fresh 与
    # 「每请求」之间仍有此消彼长的余地 —— 这组正是用来把它们拆开的。
    #
    # 破法是让两组的 cached 总量相等、请求数差 4 倍：
    #   req/many  64 次 × 5 万上下文  ≈ 320 万 cached
    #   req/few   16 次 × 20 万上下文 ≈ 320 万 cached
    # 若每请求真有 0.0667% 的固定成本，两组 Δ 应差约 3.2%；若没有，应当相等。
    # 预热轮不计入测量，两组撑上下文的 fresh（5 万 vs 18 万）因此不参与对比。
    C.append(dict(cell="req/many", prompt=TINY, warmup=ctx_seed(50_000),
                  model=BASE_MODEL, effort=BASE_EFFORT, resume=True, trials=64,
                  why="多请求·小上下文；与 req/few 的 cached 总量相同"))
    C.append(dict(cell="req/few", prompt=TINY, warmup=ctx_seed(200_000),
                  model=BASE_MODEL, effort=BASE_EFFORT, resume=True, trials=16,
                  why="少请求·大上下文；与 req/many 的 cached 总量相同"))
    # K —— 缓存费率到底是不是线性的。
    #
    # 只用 421 条（缺 bigctx）拟合时，预测那条真实会话（148 次、2220 万 cached、
    # 实测 82%）会给出 128%，看起来像「成本线性于 cached」不成立。补上 bigctx
    # 后联合拟合给出 79.5%~81.9%，矛盾消失 —— 当时是数据缺了大上下文区间。
    # 这组仍值得跑：固定请求数、只扫上下文规模，直接检验线性，而不是靠一次对账。
    # 这组固定请求数、只扫上下文规模：线性的话 Δ 应与上下文大小成正比。
    for t in (20, 60, 120, 200):
        C.append(dict(cell=f"ctx/{t}k", prompt=TINY, warmup=ctx_seed(t * 1_000),
                      model=BASE_MODEL, effort=BASE_EFFORT, resume=True, trials=20,
                      why=f"固定 20 次请求，上下文约 {t} 千 token"))
    # H —— 纯 astra + 强制大量输出：把输出侧系数单独测准。
    # 之前那个 461 tok/1%（"贵 34 倍"）是在两个已打满窗口上做残差得来的，
    # 误差叠加且是下界。这一组让输出占成本的 ~80%，且 55% 就停不打满。
    C.append(dict(cell="astra/verbose", prompt=VERBOSE, model="gpt-6-astra",
                  effort="high", resume=False, trials=25,
                  why="纯 astra、输出主导、不打满 —— 定输出侧系数"))
    # G —— 纯 astra 的干净测量：不混其它模型（不必假设别人的系数），
    # 且在 60% 就停（不打满 100%，避免删失把乘数压低）。
    # 之前六个窗口用残差法得到 3.9~11.5×，全部接近上限、全是下界。
    C.append(dict(cell="astra/clean", prompt=TINY, model="gpt-6-astra",
                  effort=BASE_EFFORT, resume=False, trials=45,
                  why="纯 astra、不打满，用来定乘数"))
    # F —— astra（gpt-6 代）× effort 档位。
    # 前沿模型上 effort 的代价差异最大，Router 的核心决策就在这里。
    for eff in ("low", "medium", "high", "xhigh", "max"):
        C.append(dict(cell=f"astra/{eff}", prompt=REASON, model="gpt-6-astra",
                      effort=eff, resume=False, trials=30,
                      why="astra 各 effort 档位"))
    # E —— 模型：其余全部相同
    for m in ("gpt-5.6-sol", "gpt-5.5", "gpt-5.6-luna", "gpt-6-astra", "gpt-5.6-terra"):
        C.append(dict(cell=f"model/{m}", prompt=TINY, model=m, effort=BASE_EFFORT,
                      resume=False, trials=8, why="只变模型"))
    return C

# ── 读取额度与用量 ────────────────────────────────────────────────────────
def newest_rollouts(since_ts=0.0):
    out = []
    for f in glob.glob(SESS_GLOB, recursive=True):
        try:
            if os.path.getmtime(f) > since_ts:
                out.append(f)
        except OSError:
            pass
    return out

def scan_rollout(path):
    """把一个 rollout 里的 token 用量与最后一次额度读数取出来。"""
    tok = dict(input_tokens=0, cached_input_tokens=0, cache_write_input_tokens=0,
               output_tokens=0, reasoning_output_tokens=0)
    quota = {}
    ctx = None
    n = 0
    quota_ts = ""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in fh:
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("payload") or {}
        if (p.get("type") or d.get("type")) != "token_count":
            continue
        n += 1
        last = (p.get("info") or {}).get("last_token_usage") or {}
        for k in tok:
            tok[k] += last.get(k, 0) or 0
        ctx = (p.get("info") or {}).get("model_context_window") or ctx
        rl = p.get("rate_limits") or {}
        ts = d.get("timestamp") or ""
        for slot in ("primary", "secondary"):
            sl = rl.get(slot) or {}
            if sl.get("window_minutes") and sl.get("used_percent") is not None:
                if ts >= quota_ts:
                    quota[sl["window_minutes"]] = {"used_percent": sl["used_percent"],
                                                   "resets_at": sl.get("resets_at")}
        if rl.get("primary") and ts > quota_ts:
            quota_ts = ts
    fh.close()
    return {"tokens": tok, "quota": quota, "context_window": ctx,
            "events": n, "quota_ts": quota_ts}

def first_event_ts(path):
    """rollout 里第一条事件的时间戳 —— 用来判断是新开的会话还是续上了旧会话"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    return json.loads(line).get("timestamp") or ""
                except Exception:
                    continue
    except OSError:
        pass
    return ""

def current_quota():
    """取最新的一次额度读数。

    按文件 mtime 取第一个是错的：mtime 最新的文件里可能是旧读数。
    要在最近改动过的若干文件里，挑事件时间戳最大的那次读数。
    """
    files = sorted(glob.glob(SESS_GLOB, recursive=True),
                   key=lambda f: os.path.getmtime(f), reverse=True)[:12]
    best, best_ts = {}, ""
    for f in files:
        r = scan_rollout(f)
        if r and r["quota"] and r.get("quota_ts", "") > best_ts:
            best, best_ts = r["quota"], r["quota_ts"]
    # 日志只在 Codex 真正发请求时更新。若某个窗口的 resets_at 已经过去，
    # 说明窗口已经滚动，这条读数是陈旧的 —— 不修正的话会误判成额度用满。
    now = time.time()
    for w, v in best.items():
        ra = v.get("resets_at")
        if ra and ra < now:
            v["used_percent"] = 0.0
            v["stale"] = True
    return best

def fmt_quota(q):
    b = []
    for w, label in ((WIN_5H, "5h"), (WIN_WEEK, "weekly")):
        if w in q:
            tag = " (窗口已重置)" if q[w].get("stale") else ""
            b.append(f"{label} {q[w]['used_percent']:.0f}%{tag}")
    return "  ".join(b) or "(读不到)"

# ── 执行 ─────────────────────────────────────────────────────────────────
SESSION_RE = __import__("re").compile(
    r"rollout-\d{4}-\d{2}-\d{2}T[\d-]+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
def session_id_of(rollout_name):
    m = SESSION_RE.search(rollout_name or "")
    return m.group(1) if m else None

def run_trial(codex, cell, workdir, timeout=300, resume_id=None):
    """跑一次 codex exec，返回它新写出来的那个 rollout 的解析结果。"""
    t0 = time.time() - 1
    cmd = [codex, "exec",
           "--skip-git-repo-check",
           "-C", workdir,
           "-m", cell["model"],
           "-c", f'model_reasoning_effort="{cell["effort"]}"',
           "-c", 'sandbox_mode="read-only"',
           "-c", 'approval_policy="never"',
           "-c", f'projects."{workdir}".trust_level="trusted"']
    if cell.get("resume") and resume_id:
        # 只续本组自己开出来的会话。组内第一次调用不带 resume，开一个新会话。
        # 以前这里退回 `resume --last`，会接到 sandbox 里上一次用过的会话上：
        # 09-13 的 req/many 就接到了 09-10 cache/bigctx 那个 128K 的会话，
        # 上下文从设计的 5 万变成 16 万，整组作废。
        cmd += ["resume", resume_id]
    cmd.append(cell["prompt"])
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True,
                              text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        ok = proc.returncode == 0
        err = "" if ok else (proc.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        ok, err = False, "timeout"
    except OSError as e:
        ok, err = False, str(e)
    time.sleep(1.5)                       # 等 rollout 落盘
    best, best_m = None, 0.0
    for f in newest_rollouts(t0):
        m = os.path.getmtime(f)
        if m > best_m:
            best, best_m = f, m
    parsed = scan_rollout(best) if best else None
    name = os.path.basename(best) if best else None
    return {"started": started, "ok": ok, "error": err,
            "rollout": name, "session_id": session_id_of(name),
            "session_started": first_event_ts(best) if best else "",
            "elapsed_s": round(time.time() - t0, 1),
            **(parsed or {})}

def cmd_plan(args):
    cells = matrix()
    q = current_quota()
    print(f"当前额度: {fmt_quota(q)}\n")
    print(f"{'实验组':<20} {'模型':<14} {'effort':<8} {'次数':>4}  说明")
    print("-" * 82)
    total = 0
    for c in cells:
        warm = len(c.get("warmup") or [])
        total += c["trials"] + warm
        n = f"{c['trials']}+{warm}" if warm else str(c["trials"])
        print(f"{c['cell']:<20} {c['model']:<14} {c['effort']:<8} {n:>6}  {c['why']}")
    print("-" * 82)
    print(f"合计 {total} 次调用，{len(cells)} 个实验组（次数列 a+b 表示 a 次测量 + b 轮预热）\n")
    print("注意：req/* 和 ctx/* 是大上下文组，很贵 —— 按当前系数两组各约 10~20%，"
          "若缓存实际更贵还会更高。\n建议用 --cell 一组一组跑，并用 --budget 卡住。\n")
    print("成对对比（每一对只差一个变量，这是能解开共线性的原因）：")
    print("  cache/cold  vs cache/warm    → 缓存 token 是否真的更便宜")
    print("  effort/low..xhigh            → reasoning effort 的额度权重")
    print("  output/short vs output/long  → output token 的权重")
    print("  input/1k vs 20k vs 80k       → input token 与上下文长度的权重")
    print("  model/*                      → 各模型换算率")
    print("\n这会消耗真实订阅额度。执行：")
    print("  python3 quota_probe.py run --yes --budget 15")
    return 0

def load_done():
    done = {}
    if os.path.exists(RESULTS):
        for line in open(RESULTS, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("ok"):
                continue          # 失败不计入，否则重跑会跳过整组
            if r.get("phase", "measure") != "measure":
                continue          # 预热轮不算进度（老记录没有 phase，按测量轮算）
            done[r["cell"]] = done.get(r["cell"], 0) + 1
    return done

def cmd_run(args):
    codex = find_codex()
    if not codex:
        sys.exit("找不到 codex 可执行文件")
    q0 = current_quota()
    if WIN_5H not in q0:
        sys.exit("读不到当前额度 —— 先在 Codex 里随便跑一次，让它写出一条 rate_limits")
    start_pct = q0[WIN_5H]["used_percent"]
    if start_pct >= 95:
        ra = q0[WIN_5H].get("resets_at")
        when = ""
        if ra:
            from datetime import datetime, timezone as _tz
            r = datetime.fromtimestamp(ra, _tz.utc).astimezone()
            mins = (r - datetime.now(r.tzinfo)).total_seconds() / 60
            when = f"，{mins:.0f} 分钟后（{r:%m-%d %H:%M}）重置" if mins > 0 else ""
        sys.exit(f"5h 额度已用 {start_pct:.0f}%{when}。等重置后再跑，"
                 f"否则实验会在半途撞上限，数据被删失。")
    print(f"codex: {codex}")
    print(f"起始额度: {fmt_quota(q0)}   预算上限: +{args.budget}%\n")

    workdir = os.path.join(HERE, "sandbox")
    os.makedirs(workdir, exist_ok=True)
    done = load_done()
    cells = [c for c in matrix() if not args.cell or c["cell"] == args.cell]
    if getattr(args, "trials", None):
        for c in cells:
            c["trials"] = args.trials

    with open(RESULTS, "a", encoding="utf-8") as out:
        for c in cells:
            need = c["trials"] - done.get(c["cell"], 0)
            if need <= 0:
                print(f"[跳过] {c['cell']} 已完成 {done[c['cell']]} 次")
                continue
            warm = c.get("warmup") or []
            print(f"[{c['cell']}] 还需 {need} 次"
                  + (f"（另有 {len(warm)} 轮预热，不计入）" if warm else "") + " …")
            resume_id = None
            consecutive_fail = 0
            for i in range(len(warm) + need):
                # 预热轮：分几次把上下文撑到目标大小（离 ARG_MAX 远一点，也便于单独标记）。
                # 之后每轮只发极短 prompt，fresh 几乎不涨、缓存主导成本。
                warming = i < len(warm)
                phase = "warmup" if warming else "measure"
                trial = dict(c, prompt=warm[i]) if warming else c
                cur = current_quota()
                used = cur.get(WIN_5H, {}).get("used_percent", start_pct) - start_pct
                if used >= args.budget:
                    print(f"\n已达预算上限（+{used:.0f}%），停止。续跑直接重新执行本命令。")
                    return 0
                r = run_trial(codex, trial, workdir, timeout=args.timeout,
                              resume_id=resume_id if c.get("resume") else None)
                # 护栏：本组第一次调用必须是新会话。接到旧会话说明上下文不是设计的大小，
                # 这一轮不写入结果，整组停下 —— 最多浪费一次调用，而不是一整组。
                if (c.get("resume") and not resume_id and r.get("ok")
                        and r.get("session_started", "")[:19] < r["started"][:19]):
                    print(f"\n本组第一次调用接到了旧会话 {r.get('rollout')}"
                          f"（开始于 {r.get('session_started')}），停止，本轮不写入。")
                    return 1
                if c.get("resume") and not resume_id and r.get("session_id"):
                    resume_id = r["session_id"]      # 之后每次都续这一个会话
                if warming and not r["ok"]:
                    print(f"\n预热轮失败，这组的上下文没建起来，跳过整组：\n"
                          f"  {r.get('error','')[:300]}")
                    break
                rec = {"cell": c["cell"], "model": c["model"], "effort": c["effort"],
                       "resume": bool(c.get("resume")), "phase": phase,
                       "trial": 0 if warming
                                else done.get(c["cell"], 0) + i - len(warm) + 1,
                       **r}
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                if (c["cell"].startswith("effort/") and r["ok"] and not warming
                        and i == len(warm)
                        and not (r.get("tokens") or {}).get("reasoning_output_tokens")):
                    print("\n这个 prompt 没有触发推理（reasoning_output_tokens=0），"
                          "effort 组测不出任何东西。换更难的任务再跑，别浪费额度。")
                    return 1
                consecutive_fail = 0 if r["ok"] else consecutive_fail + 1
                if consecutive_fail >= 3:
                    print(f"\n连续 3 次失败，停止。最后的错误：\n  {r.get('error','')[:300]}")
                    return 1
                t = r.get("tokens") or {}
                fresh = t.get("input_tokens", 0) - t.get("cached_input_tokens", 0)
                print(f"   {('预热' if warming else '#' + str(rec['trial'])):<4}"
                      f"{'ok ' if r['ok'] else 'ERR'} "
                      f"fresh_in {fresh:>7,}  cached {t.get('cached_input_tokens',0):>8,}  "
                      f"out {t.get('output_tokens',0):>6,}  "
                      f"5h {fmt_quota(r.get('quota') or {})}  {r['elapsed_s']}s"
                      + (f"  {r['error'][:60]}" if not r["ok"] else ""))
    print("\n全部完成。运行 `python3 quota_probe.py analyze` 看结果。")
    return 0

def cmd_analyze(args):
    if not os.path.exists(RESULTS):
        sys.exit("还没有结果，先跑 run")
    rows = []
    for line in open(RESULTS, encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    ok = [r for r in rows if r.get("ok") and r.get("tokens")]
    print(f"试验 {len(rows)} 次，成功 {len(ok)} 次\n")
    import collections, statistics
    g = collections.defaultdict(list)
    for r in ok:
        g[r["cell"]].append(r)
    print(f"{'实验组':<20} {'次数':>4} {'fresh_in':>9} {'cached':>9} {'output':>8} {'reason':>8} {'Δ5h%':>7}")
    print("-" * 78)
    for cell in sorted(g):
        rs = g[cell]
        med = lambda k: statistics.median([r["tokens"].get(k, 0) for r in rs])
        pcts = [r["quota"].get(str(WIN_5H), r["quota"].get(WIN_5H, {})).get("used_percent")
                for r in rs if r.get("quota")]
        pcts = [p for p in pcts if p is not None]
        d = (max(pcts) - min(pcts)) if len(pcts) >= 2 else 0
        print(f"{cell:<20} {len(rs):>4} {med('input_tokens')-med('cached_input_tokens'):>9,.0f} "
              f"{med('cached_input_tokens'):>9,.0f} {med('output_tokens'):>8,.0f} "
              f"{med('reasoning_output_tokens'):>8,.0f} {d:>7.0f}")
    print("\n成对对比 —— 每一对只差一个变量：")
    for a, b, what in (("cache/cold", "cache/warm", "缓存"),
                       ("output/short", "output/long", "输出长度"),
                       ("effort/low", "effort/xhigh", "reasoning")):
        if a in g and b in g:
            ta = statistics.median([sum(r["tokens"].values()) for r in g[a]])
            tb = statistics.median([sum(r["tokens"].values()) for r in g[b]])
            print(f"  {what:<8} {a} 总 token {ta:>9,.0f}   {b} {tb:>9,.0f}   比值 {tb/max(1,ta):.2f}x")
    for cell, rs in sorted(g.items()):
        if cell.startswith("effort/") and not sum(
                r["tokens"].get("reasoning_output_tokens", 0) for r in rs):
            print(f"  ⚠ {cell}: reasoning_output_tokens 全为 0 —— 任务没触发推理，这组无效")
    print("\n注意：单次试验的额度变化低于 1% 分辨率，所以每组必须累计到 Δ≥5% 才有意义。")
    print("样本不足时不要下结论。")
    return 0

def main():
    ap = argparse.ArgumentParser(prog="quota_probe",
        description="受控实验：测 Codex 订阅额度按什么计费")
    ap.add_argument("--version", action="version", version=f"quota_probe {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan", help="打印实验矩阵，不消耗额度").set_defaults(fn=cmd_plan)
    p = sub.add_parser("run", help="执行实验（消耗真实额度）")
    p.add_argument("--yes", action="store_true", help="确认要消耗额度")
    p.add_argument("--budget", type=float, default=10.0, help="最多消耗的 5h 额度百分比")
    p.add_argument("--cell", help="只跑某一个实验组")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--trials", type=int,
                   help="覆盖每组次数；配合 --budget 让预算来决定实际跑多少")
    p.set_defaults(fn=cmd_run)
    sub.add_parser("analyze", help="分析已有结果").set_defaults(fn=cmd_analyze)
    args = ap.parse_args()
    if args.cmd == "run" and not args.yes:
        sys.exit("run 会消耗真实订阅额度。确认请加 --yes，并用 --budget 设上限。\n"
                 "想先看计划：python3 quota_probe.py plan")
    return args.fn(args)

if __name__ == "__main__":
    sys.exit(main())
