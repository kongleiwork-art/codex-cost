import Foundation

/// 开新任务时选一次模型：本地规则（0 token），规则没有依据时才问 Luna。会话中途不切。
///
/// 分级：`gpt-5.6-luna`（简单）→ `gpt-5.6-sol`（默认）→ `gpt-6-astra`（高难度）。
/// 按 research/ 的实测，astra 每次请求的底价约是 sol 的 17 倍，要有明确的高难度
/// 信号才派给它。
///
/// 只看任务描述，不看工作区状态。早先版本把 `git status` 的脏文件数、diff 体量算进
/// 难度，但那说明的是之前没提交的改动，与这次要做的事无关：12 个脏文件的仓库里
/// 「fix typo in README」会被派给 astra。
///
/// cli/codex_route.py 是同一套规则的 Python 版，tests/test_router.py 核对两边一致。
enum Router {

    static let luna  = "gpt-5.6-luna"
    static let sol   = "gpt-5.6-sol"
    static let astra = "gpt-6-astra"

    struct Decision: Equatable {
        var model: String
        var confidence: Double          // 0…1；≥0.7 视为本地规则已够用
        var reason: String
        var usedLuna: Bool
        var hits: [String]
    }

    // MARK: - 入口

    static func route(task: String, allowLuna: Bool = true,
                      lunaJudge: ((String) -> String?)? = nil) -> Decision {
        let trimmed = task.trimmingCharacters(in: .whitespacesAndNewlines)
        let local = localRules(task: trimmed)
        if local.confidence >= 0.7 || !allowLuna {
            return local
        }
        let judge = lunaJudge ?? defaultLunaJudge
        guard let picked = judge(trimmed).flatMap(parseTier) else {
            return Decision(model: sol, confidence: 0.45,
                            reason: L.isZH ? "Luna 判定失败，回退 sol"
                                           : "Luna judge failed; fell back to sol",
                            usedLuna: true, hits: local.hits)
        }
        return Decision(model: picked, confidence: 0.75,
                        reason: L.isZH ? "Luna 判定 → \(short(picked))"
                                       : "Luna judged → \(short(picked))",
                        usedLuna: true, hits: local.hits)
    }

    // MARK: - 本地规则

    static let lunaKW = ["typo", "rename", "what is", "what's", "explain", "how do i",
                         "简单", "改个字", "错别字", "解释一下", "什么是", "咋写"]
    static let solKW = ["fix", "bug", "test", "implement", "add", "update", "refactor",
                        "修复", "实现", "加个", "补测试", "改一下"]
    static let astraKW = ["architecture", "redesign", "migrate", "migration", "multi-file",
                          "rewrite", "design system", "performance", "race condition",
                          "deadlock",
                          "架构", "重构整个", "迁移", "跨模块", "重写", "并发", "性能优化", "死锁"]
    /// 改动范围：同一个动作，改一处和改遍全仓不是一个难度
    static let scopeKW = ["everywhere", "across", "all files", "every file",
                          "entire codebase", "whole codebase",
                          "全部", "所有文件", "整个项目", "全局"]

    /// 英文按整词匹配（容许 s / es / d / ed / ing 词尾），中文按子串。
    /// 纯子串会误伤：「add」命中 address / padding，「fix」命中 prefix，「test」命中 latest。
    static func matches(_ text: String, _ kw: String) -> Bool {
        guard kw.unicodeScalars.allSatisfy({ $0.isASCII }) else { return text.contains(kw) }
        let pattern = "(?<![a-z0-9])" + NSRegularExpression.escapedPattern(for: kw)
            + "(?:s|es|d|ed|ing)?(?![a-z0-9])"
        return text.range(of: pattern, options: .regularExpression) != nil
    }

    static func localRules(task: String) -> Decision {
        let t = task.lowercased()
        var score = 0.0          // <0 偏 luna，>0 偏 astra；0 附近 = sol
        var hits: [String] = []
        var confidence = 0.35

        func bump(_ d: Double, _ why: String) {
            score += d
            hits.append(why)
            confidence = min(1.0, confidence + 0.12)
        }
        func hit(_ kws: [String]) -> Bool { kws.contains { matches(t, $0) } }

        if hit(lunaKW)  { bump(-1.2, "simple-kw") }
        if hit(solKW)   { bump(0.5, "impl-kw") }
        if hit(astraKW) { bump(1.6, "hard-kw") }
        if hit(scopeKW) { bump(1.0, "scope") }

        if t.count < 40 && score <= 0 { bump(-0.4, "short-task") }
        if t.count > 400 { bump(0.5, "long-brief") }

        // 只有 short-task / 完全没信号 → 留给 Luna，别假装很有把握
        if hits.isEmpty || hits == ["short-task"] {
            confidence = 0.25
            hits = ["no-signal"]
        } else if hits.contains(where: { $0.hasSuffix("-kw") }) || hits.contains("scope") {
            confidence = max(confidence, 0.75)
        } else if abs(score) >= 1.2 {
            confidence = max(confidence, 0.82)
        } else if abs(score) >= 0.5 {
            confidence = max(confidence, 0.72)
        }

        let model: String
        if score <= -0.8 { model = luna }
        else if score >= 1.2 { model = astra }
        else { model = sol }

        let reason = L.isZH
            ? "本地规则 → \(short(model))（\(hits.joined(separator: ", "))）"
            : "local rules → \(short(model)) (\(hits.joined(separator: ", ")))"
        return Decision(model: model, confidence: min(1, confidence),
                        reason: reason, usedLuna: false, hits: hits)
    }

    // MARK: - Luna

    /// 取回复里第一个出现的档位词。回复来自 `-o` 写出的最后一条消息，而不是 stdout ——
    /// stdout 里可能回显提示词，而提示词里 luna / sol / astra 三个词都有。
    static func parseTier(_ raw: String) -> String? {
        let low = raw.lowercased()
        var best: (String.Index, String)?
        for (key, model) in [("luna", luna), ("sol", sol), ("astra", astra)] {
            if let r = low.range(of: "(?<![a-z0-9])\(key)(?![a-z0-9])", options: .regularExpression),
               best == nil || r.lowerBound < best!.0 {
                best = (r.lowerBound, model)
            }
        }
        return best?.1
    }

    static func judgePrompt(task: String) -> String {
        """
        Classify this coding task difficulty as exactly one word: luna, sol, or astra.
        luna = trivial lookup/typo/explain; sol = normal implementation; astra = hard architecture or large multi-file work.
        Reply with only that one word.

        Task:
        \(task.prefix(2000))
        """
    }

    private static func defaultLunaJudge(_ task: String) -> String? {
        guard let codex = findCodex() else { return nil }
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-cost-judge-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmp) }
        let answer = tmp.appendingPathComponent("answer.txt")
        // -o 只写最后一条回复；stdout / stderr 都丢给 /dev/null —— 接 Pipe 却不读的话，
        // 输出一过 64KB 管道写满，子进程和这里会互相等死。
        // --ephemeral 不落会话记录，免得这次判定被 codex-cost 算进你的用量。
        let ok = runQuiet([codex, "exec", "--skip-git-repo-check", "--ephemeral",
                           "-C", tmp.path, "-m", luna, "-s", "read-only",
                           "-c", "approval_policy=\"never\"",
                           "-o", answer.path, judgePrompt(task: task)], timeout: 90)
        guard ok, let text = try? String(contentsOf: answer, encoding: .utf8),
              !text.isEmpty else { return nil }
        return text
    }

    /// 随 ChatGPT 桌面版安装的 codex 不在 PATH 里；从 Finder 启动的 app 也拿不到
    /// shell 的 PATH。所以先查固定位置，最后才按 PATH 找。
    static let codexCandidates = [
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        "/usr/local/bin/codex", "/opt/homebrew/bin/codex",
    ]

    static func findCodex() -> String? {
        let fm = FileManager.default
        if let env = ProcessInfo.processInfo.environment["CODEX_BIN"], !env.isEmpty,
           fm.isExecutableFile(atPath: env) {
            return env
        }
        for p in codexCandidates + ["\(NSHomeDirectory())/.local/bin/codex"]
        where fm.isExecutableFile(atPath: p) {
            return p
        }
        for dir in (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":") {
            let p = "\(dir)/codex"
            if fm.isExecutableFile(atPath: p) { return p }
        }
        return nil
    }

    static func short(_ model: String) -> String {
        model.replacingOccurrences(of: "gpt-", with: "")
    }

    /// 跑子进程，最多等 timeout 秒；输出全部丢弃，只看是否正常退出。
    @discardableResult
    static func runQuiet(_ args: [String], timeout: TimeInterval) -> Bool {
        guard let bin = args.first else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: bin)
        p.arguments = Array(args.dropFirst())
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        let deadline = Date().addingTimeInterval(timeout)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if p.isRunning { p.terminate(); return false }
        return p.terminationStatus == 0
    }
}
