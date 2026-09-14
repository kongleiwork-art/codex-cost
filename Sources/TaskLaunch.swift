import Foundation

/// 用锁定模型开一次新的 `codex exec` 会话。不 resume，避免中途换模型打穿缓存。
enum TaskLaunch {

    struct Outcome {
        var ok: Bool
        var model: String
        var reason: String
        var message: String
        var usedLuna: Bool
    }

    /// 在后台线程调用。先路由，再 exec。
    static func run(task: String, workdir: String, allowLuna: Bool = true) -> Outcome {
        let decision = Router.route(task: task, workdir: workdir, allowLuna: allowLuna)
        let model = decision.model

        let codex = Router.findCodex()
        guard !codex.isEmpty else {
            return Outcome(ok: false, model: model, reason: decision.reason,
                           message: L.codexMissing, usedLuna: decision.usedLuna)
        }

        let dir = workdir.isEmpty ? NSHomeDirectory() : workdir
        if !FileManager.default.fileExists(atPath: dir) {
            return Outcome(ok: false, model: model, reason: decision.reason,
                           message: L.workdirMissing, usedLuna: decision.usedLuna)
        }

        // 记住上次目录，方便下次展开面板
        UserDefaults.standard.set(dir, forKey: "taskWorkdir")

        var args = [
            "exec",
            "--skip-git-repo-check",
            "-C", dir,
            "-m", model,
            "-c", "sandbox_mode=\"workspace-write\"",
            "-c", "approval_policy=\"never\"",
            "-c", "projects.\"\(dir)\".trust_level=\"trusted\"",
            task,
        ]

        let p = Process()
        p.executableURL = URL(fileURLWithPath: codex)
        p.arguments = args
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = FileHandle.nullDevice
        do {
            try p.run()
            p.waitUntilExit()
        } catch {
            return Outcome(ok: false, model: model, reason: decision.reason,
                           message: error.localizedDescription, usedLuna: decision.usedLuna)
        }

        let errText = String(data: err.fileHandleForReading.readDataToEndOfFile(),
                             encoding: .utf8) ?? ""
        if p.terminationStatus == 0 {
            return Outcome(ok: true, model: model, reason: decision.reason,
                           message: L.taskStarted(Router.short(model)),
                           usedLuna: decision.usedLuna)
        }
        let tail = String(errText.suffix(280)).trimmingCharacters(in: .whitespacesAndNewlines)
        let msg = tail.isEmpty ? L.taskFailed : "\(L.taskFailed): \(tail)"
        return Outcome(ok: false, model: model, reason: decision.reason,
                       message: msg, usedLuna: decision.usedLuna)
    }

    static func savedWorkdir() -> String {
        if let d = UserDefaults.standard.string(forKey: "taskWorkdir"), !d.isEmpty {
            return d
        }
        return NSHomeDirectory()
    }
}
