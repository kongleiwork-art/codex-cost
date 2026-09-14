import AppKit

/// 用选定的模型在「终端」里开一个新的交互式 Codex 会话。
///
/// 不在后台跑 `codex exec`，原因有三：
/// - 看不到进度和结果，任务跑多久面板就停在「路由中」多久；
/// - 输出接 Pipe 却等进程退出才读，超过 64KB 管道写满就互相等死；
/// - 后台跑只能关掉审批（approval never + 写权限 + 强制信任目录），刘海里误按一次
///   回车，就是一个不经确认、能写整个目录的 agent。
/// 交互式会话沿用你自己的 Codex 审批与沙箱设置，过程和结果都在终端里看得到。
/// 模型只在启动时用 -m 定一次；会话中途不切，保住缓存。
enum TaskLaunch {

    enum Failure: Error {
        case codexMissing, workdirMissing, workdirIsHome, openFailed(String)

        var message: String {
            switch self {
            case .codexMissing:          return L.codexMissing
            case .workdirMissing:        return L.workdirMissing
            case .workdirIsHome:         return L.workdirIsHome
            case .openFailed(let why):   return "\(L.taskFailed): \(why)"
            }
        }
    }

    /// POSIX shell 单引号转义：' → '\''
    static func shq(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// 终端里要执行的脚本。纯函数，引号转义可以单独验证。
    /// `--` 之后才是任务文本，免得以 - 开头的任务被当成 codex 的选项。
    static func script(codex: String, model: String, workdir: String, task: String) -> String {
        """
        #!/bin/zsh
        rm -f "$0"
        cd \(shq(workdir)) || exit 1
        exec \(shq(codex)) -m \(shq(model)) -- \(shq(task))
        """
    }

    /// 成功返回 nil，失败返回原因。
    @MainActor
    static func open(model: String, workdir rawDir: String, task: String) -> Failure? {
        guard let codex = Router.findCodex() else { return .codexMissing }
        let dir = (rawDir.trimmingCharacters(in: .whitespaces) as NSString).expandingTildeInPath
        var isDir: ObjCBool = false
        guard !dir.isEmpty, FileManager.default.fileExists(atPath: dir, isDirectory: &isDir),
              isDir.boolValue else { return .workdirMissing }
        // Codex 会把工作目录当项目根：整个主目录的读写范围太大
        let home = URL(fileURLWithPath: NSHomeDirectory()).standardizedFileURL.path
        guard URL(fileURLWithPath: dir).standardizedFileURL.path != home else {
            return .workdirIsHome
        }
        UserDefaults.standard.set(dir, forKey: "taskWorkdir")

        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-task-\(UUID().uuidString).command")
        do {
            try script(codex: codex, model: model, workdir: dir, task: task)
                .write(to: file, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700],
                                                  ofItemAtPath: file.path)
        } catch {
            return .openFailed(error.localizedDescription)
        }
        // .command 文件交给「终端」打开即执行，不需要自动化（AppleScript）权限
        return NSWorkspace.shared.open(file) ? nil : .openFailed("Terminal")
    }

    /// 上次用过的工作目录；没有就留空，让用户自己填 —— 不默认主目录。
    static func savedWorkdir() -> String {
        UserDefaults.standard.string(forKey: "taskWorkdir") ?? ""
    }
}
