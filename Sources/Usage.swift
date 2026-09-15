import Foundation
import SQLite3

/// 本机 AI 编程工具的历史 token 总账：Codex、Claude Code、opencode。
///
/// 只有这三个在本地留了逐条用量。Cursor 本地的 tokenCount 全是 0，Gemini CLI、
/// ChatGPT / Claude 的聊天在本地没有 token 记录 —— 面板上会注明，免得以为漏算。
///
/// Codex 和 Claude Code 的会话文件只追加不改，Codex 光日志就有几个 GB，所以按文件
/// 建索引：修改时间和大小都没变就复用上次的解析结果。索引里保留已经被删掉的
/// 文件的记录 —— Claude Code 默认 30 天清理旧会话，原始记录删了，这里的历史还在。
enum Usage {

    enum Tool: String, Codable, CaseIterable {
        case codex, claudeCode, opencode
        var label: String {
            switch self {
            case .codex:      return "Codex"
            case .claudeCode: return "Claude Code"
            case .opencode:   return "opencode"
            }
        }
    }

    /// 一次请求（Codex）或一条回复（Claude Code、opencode）的用量
    struct Record: Codable {
        var key: String          // 去重键，见各解析函数
        var day: String          // 本地日期 yyyy-MM-dd
        var tool: Tool
        var model: String
        var input: Int           // 新增输入，不含缓存
        var cacheRead: Int
        var cacheWrite: Int
        var output: Int          // 含推理
        var cost: Double?        // 美元，只有 opencode 自带

        var total: Int { input + cacheRead + cacheWrite + output }

        // 索引里有几万条，键名用单字母
        enum CodingKeys: String, CodingKey {
            case key = "k", day = "d", tool = "t", model = "m", input = "i",
                 cacheRead = "r", cacheWrite = "w", output = "o", cost = "c"
        }
    }

    // MARK: - 路径（环境变量可覆盖，测试靠它指向样本目录）

    private static var env: [String: String] { ProcessInfo.processInfo.environment }
    private static func dir(_ key: String, default fallback: URL) -> URL {
        guard let v = env[key], !v.isEmpty else { return fallback }
        return URL(fileURLWithPath: (v as NSString).expandingTildeInPath)
    }
    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    static var codexHome: URL { dir("CODEX_HOME", default: home.appendingPathComponent(".codex")) }
    static var claudeHome: URL { dir("CLAUDE_CONFIG_DIR", default: home.appendingPathComponent(".claude")) }
    static var opencodeDB: URL {
        dir("XDG_DATA_HOME", default: home.appendingPathComponent(".local/share"))
            .appendingPathComponent("opencode/opencode.db")
    }
    static var indexURL: URL {
        dir("CODEX_COST_DATA_DIR", default: home.appendingPathComponent("Library/Application Support/codex-cost"))
            .appendingPathComponent("usage-index.json")
    }

    // MARK: - 解析

    final class DayFormatter {
        private let frac: ISO8601DateFormatter = {
            let f = ISO8601DateFormatter()
            f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            return f
        }()
        private let plain = ISO8601DateFormatter()
        private let out: DateFormatter = {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = .current
            f.dateFormat = "yyyy-MM-dd"
            return f
        }()
        func day(_ ts: String) -> String? {
            (frac.date(from: ts) ?? plain.date(from: ts)).map { out.string(from: $0) }
        }
        func day(_ d: Date) -> String { out.string(from: d) }
    }

    private static func int(_ v: Any?) -> Int { (v as? NSNumber)?.intValue ?? 0 }

    /// 按字节切行，只对含 marker 的行做 JSON 解析（同 Budget：避开 Swift 字符串的字素簇开销）
    private static func eachLine(_ url: URL, markers: [String], _ body: ([String: Any]) -> Void) {
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe) else { return }
        let marks = markers.map { Array($0.utf8) }
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.baseAddress else { return }
            let n = raw.count
            var start = 0
            while start < n {
                let end: Int
                if let p = memchr(base + start, 0x0A, n - start) {
                    end = base.distance(to: UnsafeRawPointer(p))
                } else {
                    end = n
                }
                defer { start = end + 1 }
                let len = end - start
                guard len > 20 else { continue }
                let line = base + start
                guard marks.contains(where: { m in
                    m.withUnsafeBytes { memmem(line, len, $0.baseAddress, m.count) != nil }
                }) else { continue }
                guard let obj = try? JSONSerialization.jsonObject(with: Data(bytes: line, count: len))
                        as? [String: Any] else { continue }
                body(obj)
            }
        }
    }

    /// Codex：每个带用量的 token_count 是一次请求。
    /// 去重键是时间戳 + 各项 token：分叉 / 归档的会话会把同一批事件原样复制一份。
    ///
    /// 模型取自前面最近的 turn_context。有的会话里 turn_context 出现在第一批请求之后，
    /// 那些请求用文件里第一个出现的模型回填；整份都没有 turn_context 的（多是早期会话、
    /// 自定义 provider）只能记为未知。
    static func parseCodex(_ url: URL, days: DayFormatter) -> [Record] {
        var model = "?"
        var firstModel: String?
        var out: [Record] = []
        eachLine(url, markers: ["\"turn_context\"", "\"token_count\""]) { obj in
            let p = obj["payload"] as? [String: Any] ?? [:]
            let type = (p["type"] as? String) ?? (obj["type"] as? String) ?? ""
            if type == "turn_context" {
                model = (p["model"] as? String) ?? model
                if firstModel == nil, model != "?" { firstModel = model }
                return
            }
            guard type == "token_count",
                  let ts = obj["timestamp"] as? String,
                  let u = (p["info"] as? [String: Any])?["last_token_usage"] as? [String: Any],
                  !u.isEmpty, let day = days.day(ts) else { return }
            let inp = int(u["input_tokens"]), cached = int(u["cached_input_tokens"])
            let o = int(u["output_tokens"]), r = int(u["reasoning_output_tokens"])
            out.append(Record(key: "cx|\(ts)|\(inp)|\(cached)|\(o)|\(r)", day: day, tool: .codex,
                              model: model, input: max(0, inp - cached), cacheRead: cached,
                              cacheWrite: 0, output: o + r, cost: nil))
        }
        if let firstModel {
            for i in out.indices where out[i].model == "?" { out[i].model = firstModel }
        }
        return out
    }

    /// Claude Code：同一条回复会按内容块写好几遍（用量相同，或输出递增），恢复 / 分叉的
    /// 会话还会把它复制到别的文件。按 message.id 去重，文件内留最后一遍。
    static func parseClaude(_ url: URL, days: DayFormatter) -> [Record] {
        var byID: [String: Record] = [:]
        var order: [String] = []
        eachLine(url, markers: ["\"usage\""]) { obj in
            guard let m = obj["message"] as? [String: Any],
                  let u = m["usage"] as? [String: Any],
                  let id = m["id"] as? String,
                  let ts = obj["timestamp"] as? String,
                  let day = days.day(ts) else { return }
            let model = m["model"] as? String ?? "?"
            guard model != "<synthetic>" else { return }
            if byID[id] == nil { order.append(id) }
            byID[id] = Record(key: "cc|\(id)", day: day, tool: .claudeCode, model: model,
                              input: int(u["input_tokens"]),
                              cacheRead: int(u["cache_read_input_tokens"]),
                              cacheWrite: int(u["cache_creation_input_tokens"]),
                              output: int(u["output_tokens"]), cost: nil)
        }
        return order.compactMap { byID[$0] }
    }

    /// opencode：SQLite 里每条助手消息自带 token 和费用。只读打开，不影响正在运行的 opencode。
    static func readOpencode(_ url: URL, days: DayFormatter) -> [Record] {
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        var db: OpaquePointer?
        guard sqlite3_open_v2(url.path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return []
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 2000)
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "select id, data from message", -1, &stmt, nil) == SQLITE_OK else {
            return []
        }
        defer { sqlite3_finalize(stmt) }
        var out: [Record] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            guard let idC = sqlite3_column_text(stmt, 0),
                  let dataC = sqlite3_column_blob(stmt, 1) else { continue }
            let data = Data(bytes: dataC, count: Int(sqlite3_column_bytes(stmt, 1)))
            guard let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  j["role"] as? String == "assistant",
                  let t = j["tokens"] as? [String: Any],
                  let created = ((j["time"] as? [String: Any])?["created"] as? NSNumber)?.doubleValue,
                  created > 0 else { continue }
            let cache = t["cache"] as? [String: Any] ?? [:]
            let rec = Record(key: "oc|\(String(cString: idC))",
                             day: days.day(Date(timeIntervalSince1970: created / 1000)),
                             tool: .opencode, model: j["modelID"] as? String ?? "?",
                             input: int(t["input"]), cacheRead: int(cache["read"]),
                             cacheWrite: int(cache["write"]),
                             output: int(t["output"]) + int(t["reasoning"]),
                             cost: (j["cost"] as? NSNumber)?.doubleValue)
            if rec.total > 0 || (rec.cost ?? 0) > 0 { out.append(rec) }
        }
        return out
    }

    // MARK: - 索引

    struct FileEntry: Codable {
        var mtime: Double
        var size: Int
        var records: [Record]
        var gone: Bool?          // 原文件已被删除，记录照样保留
    }
    struct Index: Codable {
        var version: Int
        var files: [String: FileEntry]
    }
    static let indexVersion = 2          // 2：Codex 未知模型回填，旧索引要重新解析

    private static func jsonlFiles(under root: URL, prefix: String) -> [URL] {
        guard let en = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil,
                                                      options: [.skipsHiddenFiles]) else { return [] }
        var out: [URL] = []
        for case let url as URL in en
        where url.pathExtension == "jsonl" && url.lastPathComponent.hasPrefix(prefix) {
            out.append(url)
        }
        return out
    }

    /// 更新索引，返回去重后的全部记录（含已删除文件的）。首次要扫全部日志，在后台线程调用。
    static func loadRecords() -> [Record] {
        let fm = FileManager.default
        var index = (try? JSONDecoder().decode(Index.self, from: Data(contentsOf: indexURL)))
            .flatMap { $0.version == indexVersion ? $0 : nil }
            ?? Index(version: indexVersion, files: [:])
        let days = DayFormatter()
        var seen = Set<String>()
        var changed = false

        func scan(_ files: [URL], _ parse: (URL) -> [Record]) {
            for url in files {
                let path = url.path
                seen.insert(path)
                guard let attrs = try? fm.attributesOfItem(atPath: path) else { continue }
                let mtime = (attrs[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
                let size = (attrs[.size] as? NSNumber)?.intValue ?? 0
                if let e = index.files[path], e.mtime == mtime, e.size == size, e.gone != true { continue }
                index.files[path] = FileEntry(mtime: mtime, size: size, records: parse(url), gone: nil)
                changed = true
            }
        }
        let codex = codexHome
        scan(jsonlFiles(under: codex.appendingPathComponent("sessions"), prefix: "rollout-")
             + jsonlFiles(under: codex.appendingPathComponent("archived_sessions"), prefix: "")) {
            parseCodex($0, days: days)
        }
        scan(jsonlFiles(under: claudeHome.appendingPathComponent("projects"), prefix: "")) {
            parseClaude($0, days: days)
        }
        for (path, e) in index.files where !seen.contains(path) && e.gone != true {
            index.files[path]?.gone = true
            changed = true
        }
        if changed, let data = try? JSONEncoder().encode(index) {
            try? fm.createDirectory(at: indexURL.deletingLastPathComponent(),
                                    withIntermediateDirectories: true)
            try? data.write(to: indexURL, options: .atomic)
        }
        return dedupe(index.files.values.flatMap(\.records) + readOpencode(opencodeDB, days: days))
    }

    /// 全局去重：同一个键留输出最多的那条
    static func dedupe(_ records: [Record]) -> [Record] {
        var best: [String: Record] = [:]
        best.reserveCapacity(records.count)
        for r in records {
            if let b = best[r.key], b.output >= r.output { continue }
            best[r.key] = r
        }
        return Array(best.values)
    }

    // MARK: - 汇总

    enum Period: String, CaseIterable {
        case today, week, month, all
        var days: Int? {
            switch self {
            case .today: return 1
            case .week:  return 7
            case .month: return 30
            case .all:   return nil
            }
        }
    }

    struct ToolTotal: Identifiable {
        let tool: Tool
        var input = 0, cacheRead = 0, cacheWrite = 0, output = 0, requests = 0
        var cost = 0.0
        var quotaPct = 0.0       // 只有 Codex：按实测系数折合的 5 小时额度百分比
        var id: Tool { tool }
        var total: Int { input + cacheRead + cacheWrite + output }
    }
    struct ModelTotal: Identifiable {
        let tool: Tool
        let model: String
        var tokens: Int
        var id: String { tool.rawValue + "|" + model }
    }
    struct DayTotal: Identifiable {
        let day: String
        var tokens: Int
        var id: String { day }
    }
    struct Summary {
        let period: Period
        var tools: [ToolTotal]
        var models: [ModelTotal]
        var daily: [DayTotal]
        var firstDay: String?
        var records: Int
    }

    /// Codex 一次请求折合多少 5 小时额度（与面板上「当前额度」用同一套系数）
    static func codexPct(_ r: Record) -> Double {
        let c = Budget.coef[r.model] ?? Budget.fallback
        guard let f = c.fresh, let ca = c.cached, let o = c.output else { return 0 }
        return Double(r.input) / f + Double(r.cacheRead) / ca + Double(r.output) / o + c.request
    }

    static func summarize(_ records: [Record], period: Period, now: Date = Date()) -> Summary {
        let fmt = DayFormatter()
        let cal = Calendar.current
        let cutoff = period.days.map { fmt.day(cal.date(byAdding: .day, value: -($0 - 1), to: now) ?? now) }
        var tools: [Tool: ToolTotal] = [:]
        var models: [String: ModelTotal] = [:]
        var perDay: [String: Int] = [:]
        var first: String?
        var count = 0
        for r in records {
            if first == nil || r.day < first! { first = r.day }
            if let cutoff, r.day < cutoff { continue }
            count += 1
            var t = tools[r.tool] ?? ToolTotal(tool: r.tool)
            t.input += r.input; t.cacheRead += r.cacheRead; t.cacheWrite += r.cacheWrite
            t.output += r.output; t.requests += 1; t.cost += r.cost ?? 0
            if r.tool == .codex { t.quotaPct += codexPct(r) }
            tools[r.tool] = t
            models[r.tool.rawValue + "|" + r.model, default: ModelTotal(tool: r.tool, model: r.model, tokens: 0)]
                .tokens += r.total
            perDay[r.day, default: 0] += r.total
        }
        // 柱状图：「今天」「7 天」看最近 7 天，其余看最近 30 天
        let span = max(7, period.days ?? 30)
        let daily = (0..<span).reversed().map { i -> DayTotal in
            let d = fmt.day(cal.date(byAdding: .day, value: -i, to: now) ?? now)
            return DayTotal(day: d, tokens: perDay[d] ?? 0)
        }
        return Summary(period: period,
                       tools: Tool.allCases.compactMap { tools[$0] },
                       models: Array(models.values.sorted { $0.tokens > $1.tokens }.prefix(6)),
                       daily: daily, firstDay: first, records: count)
    }
}
