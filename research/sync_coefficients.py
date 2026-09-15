#!/usr/bin/env python3
"""把 research/coefficients.json 同步到 app 和 README —— 系数只在 JSON 里改。

    python3 research/sync_coefficients.py          # 写入
    python3 research/sync_coefficients.py --check  # 只检查，不一致就退出 1（测试和 CI 用）

生成 Sources/Coefficients.swift；更新两份 README 系数表里对应模型那一行的数字，
单元格末尾的脚注记号（\\*）保留，免费模型那一行不动。
refit.py 和 cli/codex_budget.py 运行时直接读 JSON，不需要同步。
"""
from __future__ import annotations
import argparse, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JSON_PATH = os.path.join(HERE, "coefficients.json")
SWIFT_PATH = os.path.join(ROOT, "Sources", "Coefficients.swift")
READMES = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "README.zh-CN.md")]


def load():
    with open(JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def swift_source(data) -> str:
    models = data["models"]
    width = max(len(m) for m in models) + 3
    rows = []
    for m, c in models.items():
        key = f'"{m}":'.ljust(width)
        if c.get("free"):
            rows.append(f"        {key}Coef(fresh: nil, cached: nil, output: nil, request: 0.0),")
        else:
            rows.append(f"        {key}Coef(fresh: {c['fresh']:_}, cached: {c['cached']:_}, "
                        f"output: {c['output']:_}, request: {float(c['request'])!r}),")
    alts = ", ".join(f'"{m}"' for m, c in models.items() if c.get("counterfactual", True))
    return (
        "// 由 research/sync_coefficients.py 从 research/coefficients.json 生成，不要手改。\n"
        "// 改系数：编辑 JSON，再运行 python3 research/sync_coefficients.py。\n\n"
        "extension Budget {\n"
        f"    /// 成本系数 {data['version']}（{data['updated']}），来龙去脉见 Budget.swift 顶部注释\n"
        "    static let coef: [String: Coef] = [\n"
        + "\n".join(rows) + "\n"
        "    ]\n"
        "    /// 没有系数的模型按它估算\n"
        f"    static let fallback = coef[\"{data['fallback']}\"]!\n"
        "    /// 出现在「换成单一模型」对照和换模型建议里的模型\n"
        f"    static let counterfactualModels: [String] = [{alts}]\n"
        "}\n"
    )


ROW = re.compile(r"^\| `(?P<model>[^`]+)` \|(?P<cells>.*)\|[ \t]*$", re.M)


def readme_source(text, data) -> str:
    models = data["models"]

    def fix(m):
        c = models.get(m.group("model"))
        cells = m.group("cells").split("|")
        if not c or c.get("free") or len(cells) != 4:
            return m.group(0)
        values = [f"{c['fresh']:,} tok", f"{c['cached']:,} tok", f"{c['output']:,} tok",
                  f"{float(c['request']):.4f}%"]
        out = []
        for old, new in zip(cells, values):
            star = "\\*" if old.strip().endswith("\\*") else ""
            out.append(f" {new}{star} ")
        return f"| `{m.group('model')}` |" + "|".join(out) + "|"

    return ROW.sub(fix, text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    data = load()
    targets = {SWIFT_PATH: swift_source(data)}
    for path in READMES:
        with open(path, encoding="utf-8") as f:
            targets[path] = readme_source(f.read(), data)
    stale = []
    for path, content in targets.items():
        current = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if current != content:
            stale.append(os.path.relpath(path, ROOT))
            if not args.check:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
    if args.check:
        if stale:
            print("与 research/coefficients.json 不一致：" + "、".join(stale)
                  + "。运行 python3 research/sync_coefficients.py 同步。")
            return 1
        print("系数一致")
        return 0
    print("已同步：" + ("、".join(stale) if stale else "无变化"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
