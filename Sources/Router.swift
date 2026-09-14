import Foundation

/// 本地规则 +（可选）Luna 判定：只在开新任务时选一次模型，会话中途不切。
///
/// 分级：`gpt-5.6-luna`（简单）→ `gpt-5.6-sol`（默认）→ `gpt-6-astra`（高难度）。
/// 本地规则 0 token；仅当置信度低时才用 Luna 看任务描述（失败回退 sol）。
enum Router {

    static let luna  = "gpt-5.6-luna"
    static let sol   = "gpt-5.6-sol"
    static let astra = "gpt-6-astra"

    struct WorkspaceSignals: Equatable {
        var changedFiles: Int = 0
        var diffBytes: Int = 0
        var touchesTests: Bool = false
        var multiFileArch: Bool = false
    }

    struct Decision: Equatable {
        var model: String
        var confidence: Double          // 0…1；≥0.7 视为本地规则已够用
        var reason: String
        var usedLuna: Bool
        var signals: WorkspaceSignals
    }

    // MARK: - 入口

    /// `allowLuna` 为 true 且本地置信度低时，才壳出 `codex exec -m luna` 做短判定。
    static func route(task: String, workdir: String?,
                      allowLuna: Bool = true,
                      lunaJudge: ((String) -> String?)? = nil) -> Decision {
        let trimmed = task.trimmingCharacters(in: .whitespacesAndNewlines)
        let signals = inspect(workdir: workdir)
        let local = localRules(task: trimmed, signals: signals)
        if local.confidence >= 0.7 || !allowLuna {
            return local
        }
        let judge = lunaJudge ?? defaultLunaJudge
        guard let raw = judge(trimmed) else {
            return Decision(model: sol, confidence: 0.45,
                            reason: L.isZH
                                ? "Luna 判定失败，回退 sol"
                                : "Luna judge failed; fell back to sol",
                            usedLuna: true, signals: signals)
        }
        let picked = parseTier(raw) ?? sol
        return Decision(model: picked, confidence: 0.75,
                        reason: L.isZH
                            ? "Luna 判定 → \(short(picked))"
                            : "Luna judged → \(short(picked))",
                        usedLuna: true, signals: signals)
    }

    // MARK: - 本地规则

    static func localRules(task: String, signals: WorkspaceSignals) -> Decision {
        let t = task.lowercased()
        var score = 0.0          // <0 偏 luna，>0 偏 astra；0 附近 = sol
        var hits: [String] = []
        var confidence = 0.35

        func bump(_ d: Double, _ why: String) {
            score += d
            hits.append(why)
            confidence = min(1.0, confidence + 0.12)
        }

        let lunaKW = ["typo", "rename", "what is", "what's", "explain", "how do i",
                      "简单", "改个字", "错别字", "解释一下", "什么是", "咋写"]
        let solKW  = ["fix", "bug", "test", "implement", "add", "update", "refactor",
                      "修复", "实现", "加个", "补测试", "改一下"]
        let astraKW = ["architecture", "redesign", "migrate", "multi-file", "across the",
                       "rewrite", "design system", "performance", "race condition",
                       "架构", "重构整个", "迁移", "跨模块", "重写", "并发", "性能优化"]

        if lunaKW.contains(where: { t.contains($0) }) { bump(-1.2, "simple-kw") }
        if solKW.contains(where: { t.contains($0) })  { bump(0.5, "impl-kw") }
        if astraKW.contains(where: { t.contains($0) }) { bump(1.6, "hard-kw") }

        // 有工作区强信号时，短文案不再往 luna 拉，避免冲掉 many-files
        if t.count < 40 && score <= 0 && signals.changedFiles == 0 && !signals.multiFileArch {
            bump(-0.4, "short-task")
        }
        if t.count > 400 { bump(0.5, "long-brief") }

        if signals.changedFiles >= 8 || signals.multiFileArch {
            bump(1.6, "many-files")
        } else if signals.changedFiles >= 3 {
            bump(0.5, "few-files")
        } else if signals.changedFiles == 1 && signals.diffBytes < 800 {
            bump(-1.0, "tiny-diff")
        }

        if signals.diffBytes >= 40_000 { bump(1.0, "huge-diff") }
        else if signals.diffBytes >= 8_000 { bump(0.4, "med-diff") }

        if signals.touchesTests && score >= 0 { bump(0.2, "tests") }

        // 只有 short-task / 完全没信号 → 留给 Luna，别假装很有把握
        if hits.isEmpty || hits == ["short-task"] {
            confidence = 0.25
            hits = ["no-signal"]
        } else if hits.contains(where: { $0.hasSuffix("-kw") })
                    || hits.contains("many-files") || hits.contains("tiny-diff") {
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
                        reason: reason, usedLuna: false, signals: signals)
    }

    // MARK: - 工作区探测

    static func inspect(workdir: String?) -> WorkspaceSignals {
        var s = WorkspaceSignals()
        guard let dir = workdir, !dir.isEmpty,
              FileManager.default.fileExists(atPath: dir) else { return s }

        let status = run(["git", "-C", dir, "status", "--porcelain"], timeout: 3)
        let lines = status.split(separator: "\n").map(String.init).filter { !$0.isEmpty }
        s.changedFiles = lines.count
        s.touchesTests = lines.contains {
            let p = $0.dropFirst(3).lowercased()
            return p.contains("test") || p.contains("/tests/") || p.hasSuffix("_test.py")
                || p.hasSuffix("tests.swift") || p.contains("spec.")
        }
        s.multiFileArch = lines.filter {
            let p = $0.dropFirst(3)
            return p.hasSuffix(".swift") || p.hasSuffix(".ts") || p.hasSuffix(".tsx")
                || p.hasSuffix(".py") || p.hasSuffix(".go") || p.hasSuffix(".rs")
        }.count >= 5

        let diff = run(["git", "-C", dir, "diff", "--stat", "HEAD"], timeout: 3)
        s.diffBytes = diff.utf8.count
        if let m = diff.range(of: #"(\d+)\s+files?\s+changed"#, options: .regularExpression) {
            let num = diff[m].split(whereSeparator: { !$0.isNumber }).first
            if let n = num.flatMap({ Int($0) }), n > s.changedFiles { s.changedFiles = n }
        }
        return s
    }

    // MARK: - Luna

    static func parseTier(_ raw: String) -> String? {
        let low = raw.lowercased()
        for (key, model) in [("astra", astra), ("sol", sol), ("luna", luna)] {
            if low.range(of: "\\b\(key)\\b", options: .regularExpression) != nil {
                return model
            }
        }
        if low.contains("astra") { return astra }
        if low.contains("luna") { return luna }
        if low.contains("sol") { return sol }
        return nil
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
        let codex = findCodex()
        guard !codex.isEmpty else { return nil }
        let prompt = judgePrompt(task: task)
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-cost-judge-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmp) }

        let args = [codex, "exec",
                    "--skip-git-repo-check",
                    "-C", tmp.path,
                    "-m", luna,
                    "-c", "sandbox_mode=\"read-only\"",
                    "-c", "approval_policy=\"never\"",
                    prompt]
        let out = run(args, timeout: 90)
        return out.isEmpty ? nil : out
    }

    static func findCodex() -> String {
        if let env = ProcessInfo.processInfo.environment["CODEX_BIN"], !env.isEmpty {
            return env
        }
        let which = run(["/usr/bin/which", "codex"], timeout: 2)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if !which.isEmpty { return which }
        let home = NSHomeDirectory()
        for p in ["\(home)/.local/bin/codex", "/usr/local/bin/codex", "/opt/homebrew/bin/codex"] {
            if FileManager.default.isExecutableFile(atPath: p) { return p }
        }
        return ""
    }

    static func short(_ model: String) -> String {
        model.replacingOccurrences(of: "gpt-", with: "")
    }

    // MARK: - Process helper

    @discardableResult
    static func run(_ args: [String], timeout: TimeInterval) -> String {
        guard let bin = args.first else { return "" }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: bin)
        p.arguments = Array(args.dropFirst())
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { return "" }

        let group = DispatchGroup()
        group.enter()
        DispatchQueue.global().async {
            p.waitUntilExit()
            group.leave()
        }
        _ = group.wait(timeout: .now() + timeout)
        if p.isRunning { p.terminate() }

        let data = out.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? ""
    }
}
