import Foundation

/// 数据层：直接在 Swift 里解析 Codex 会话日志并套用成本模型。
///
/// 原先是 Process 调 `python3 codex_budget.py --json`，但干净的 macOS 上
/// /usr/bin/python3 只是个占位符，首次执行会要求安装 Xcode 命令行工具 ——
/// 别人下载 .app 后大概率跑不起来。移植到 Swift 后零外部依赖。
enum Budget {

    // MARK: 成本模型
    //
    // 每个模型三个系数：新增输入、输出侧（output+reasoning）、每次请求固定成本。
    // 不能用「单一乘数 × 基准公式」——astra 三个成分相对 sol 分别是
    // 2.7× / 6× / 5.3×，用一个标量描述会在不同调用构成下飘到 3× ~ 11×。
    //
    // 系数来自受控实验（详见仓库 research/）：
    //   sol   —— 22 组实验，按次归一化回归 R² 0.987
    //   astra —— 纯 astra、未打满窗口的累积回归；输出侧用强制长输出的一组单独定
    //   5.5 / terra —— 只测到整体乘数（±0.11），按比例缩放
    //   luna  —— 30 次调用零消耗
    struct Coef {
        let fresh: Double?      // 每 1% 额度能买多少 fresh 输入 token；nil = 不计费
        let output: Double?
        let request: Double     // 每次请求的固定成本（%）
    }

    static let coef: [String: Coef] = [
        "gpt-5.6-sol":   Coef(fresh: 41_398, output: 15_450, request: 0.0667),
        "gpt-5.5":       Coef(fresh: 48_137, output: 17_965, request: 0.0574),
        "gpt-5.6-terra": Coef(fresh: 46_000, output: 17_167, request: 0.0600),
        "gpt-5.6-luna":  Coef(fresh: nil,    output: nil,    request: 0.0),
        "gpt-6-astra":   Coef(fresh: 15_415, output:  2_495, request: 0.3514),
    ]
    static let fallback = Coef(fresh: 41_398, output: 15_450, request: 0.0667)
    static let windowMinutes5h = 300.0
    static let windowMinutesWeek = 10080.0

    static func cost(fresh: Int, output: Int, requests: Int, model: String?) -> Double {
        let c = coef[model ?? ""] ?? fallback
        guard let f = c.fresh, let o = c.output else { return 0 }
        return Double(fresh) / f + Double(output) / o + c.request * Double(requests)
    }

    // MARK: 结果

    struct Window { var usedPercent: Double; var resetsAt: Double?; var stale: Bool }
    struct ModelUse { var requests: Int; var fresh: Int; var output: Int
                      var pct: Double { Budget.cost(fresh: fresh, output: output,
                                                    requests: requests, model: model) }
                      var model: String }
    struct Result {
        var windowStart: Date
        var currentModel: String?
        var sessions: Int
        var fresh = 0, cached = 0, output = 0, requests = 0
        var byModel: [String: ModelUse] = [:]
        var quota: [Double: Window] = [:]
        var quotaReadAt: Date?
        var firstEvent: Date?
        var lastEvent: Date?

        /// 每分钟烧掉多少额度。用窗口内首末事件的跨度算，
        /// 空闲时段不计入 —— 否则挂机一晚上速率会被稀释成 0。
        var burnPerMinute: Double? {
            guard let a = firstEvent, let b = lastEvent else { return nil }
            let mins = b.timeIntervalSince(a) / 60
            guard mins > 3, spent > 0 else { return nil }
            return spent / mins
        }
        /// 按当前速率还能撑多少分钟。nil 表示速率不可估或已到上限。
        var minutesToWall: Double? {
            guard let rate = burnPerMinute, rate > 0,
                  let used = fiveHour?.usedPercent else { return nil }
            return max(0, 100 - used) / rate
        }

        var spent: Double { byModel.values.reduce(0) { $0 + $1.pct } }
        var costFresh: Double { byModel.values.reduce(0) {
            guard let f = (Budget.coef[$1.model] ?? Budget.fallback).fresh else { return $0 }
            return $0 + Double($1.fresh) / f } }
        var costOutput: Double { byModel.values.reduce(0) {
            guard let o = (Budget.coef[$1.model] ?? Budget.fallback).output else { return $0 }
            return $0 + Double($1.output) / o } }
        var costRequest: Double { byModel.values.reduce(0) {
            $0 + (Budget.coef[$1.model] ?? Budget.fallback).request * Double($1.requests) } }
        var fiveHour: Window? { quota[Budget.windowMinutes5h] }
        var weekly: Window? { quota[Budget.windowMinutesWeek] }
        var gap: Double { (fiveHour?.usedPercent ?? 0) - spent }
        /// 同样这些 token，全用某个模型的话
        func counterfactual(_ model: String) -> Double {
            Budget.cost(fresh: fresh, output: output, requests: requests, model: model)
        }
    }

    // MARK: 解析

    // Codex 的时间戳带毫秒（2026-09-10T08:26:52.512Z）。
    // 只用 .withInternetDateTime 会解析失败返回 nil —— 那会让所有记录被静默丢弃。
    private static let isoFrac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let isoPlain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]; return f
    }()
    private static func parseDate(_ s: String) -> Date? {
        isoFrac.date(from: s) ?? isoPlain.date(from: s)
    }

    /// 取最近修改的若干个会话文件。
    ///
    /// 不能只挑「窗口内修改过」的：额度读数要从最新的一条记录里取，而那条
    /// 记录可能早于窗口（比如你几小时没用 Codex）。窗口过滤只作用于用量累加。
    private static func sessionFiles(limit: Int = 40) -> [URL] {
        let root = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".codex/sessions")
        guard let en = FileManager.default.enumerator(
            at: root, includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]) else { return [] }
        var out: [(URL, Date)] = []
        for case let url as URL in en {
            guard url.lastPathComponent.hasPrefix("rollout-"),
                  url.pathExtension == "jsonl",
                  let m = try? url.resourceValues(forKeys: [.contentModificationDateKey])
                                 .contentModificationDate else { continue }
            out.append((url, m))
        }
        return out.sorted { $0.1 > $1.1 }.prefix(limit).map(\.0)
    }

    /// 读取一个 rollout 文件里落在窗口内的用量，并回传见到的最新额度读数。
    // 按字节切行，不要用 String.split(separator:"\n")：
    // Swift 的 Character 要做字素簇解析，日志里大量中文时在多 MB 字符串上
    // 慢到几分钟级别。这里全程走 Data，只对候选行做 JSON 解析。
    private static let nl = UInt8(0x0A)
    private static let markTok = Array("\"token_count\"".utf8)
    private static let markCtx = Array("\"turn_context\"".utf8)

    private static func contains(_ hay: UnsafeRawBufferPointer, _ needle: [UInt8]) -> Bool {
        guard hay.count >= needle.count else { return false }
        let first = needle[0]
        var i = 0
        let end = hay.count - needle.count
        while i <= end {
            if hay[i] == first {
                var j = 1
                while j < needle.count, hay[i + j] == needle[j] { j += 1 }
                if j == needle.count { return true }
            }
            i += 1
        }
        return false
    }

    private static func scan(_ url: URL, since: Date,
                            into agg: inout [String: ModelUse],
                            totals: inout (f: Int, c: Int, o: Int, n: Int),
                            newest: inout (Date, String)?,
                            span: inout (Date, Date)?,
                            quota: inout (Date, [Double: Window])?) -> Bool {
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe) else { return false }
        var model: String? = nil
        var touched = false

        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            var start = 0
            while start < raw.count {
                var end = start
                while end < raw.count, raw[end] != nl { end += 1 }
                defer { start = end + 1 }
                let len = end - start
                if len < 40 { continue }
                let slice = UnsafeRawBufferPointer(rebasing: raw[start..<end])
                let isTok = contains(slice, markTok)
                let isCtx = isTok ? false : contains(slice, markCtx)
                guard isTok || isCtx else { continue }
                let lineData = Data(bytes: slice.baseAddress!, count: len)
                guard let obj = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any]
                      else { continue }
                let payload = obj["payload"] as? [String: Any] ?? [:]
                let type = (payload["type"] as? String) ?? (obj["type"] as? String) ?? ""
                if type == "turn_context" {
                    model = (payload["model"] as? String) ?? model
                    continue
                }
                guard type == "token_count" else { continue }
                let ts = parseDate(obj["timestamp"] as? String ?? "")

                if let rl = payload["rate_limits"] as? [String: Any], let ts {
                    var got: [Double: Window] = [:]
                    for slot in ["primary", "secondary"] {
                        guard let w = rl[slot] as? [String: Any],
                              let mins = w["window_minutes"] as? Double,
                              let used = w["used_percent"] as? Double else { continue }
                        let resets = w["resets_at"] as? Double
                        // 日志只在发请求时更新；resets_at 已过期说明窗口早已滚动，
                        // 不修正会把已重置的窗口读成用满。
                        let stale = (resets ?? .infinity) < Date().timeIntervalSince1970
                        got[mins] = Window(usedPercent: stale ? 0 : used,
                                           resetsAt: resets, stale: stale)
                    }
                    if !got.isEmpty, quota == nil || ts > quota!.0 { quota = (ts, got) }
                }

                guard let ts, ts >= since else { continue }
                let info = payload["info"] as? [String: Any] ?? [:]
                let u = info["last_token_usage"] as? [String: Any] ?? [:]
                let inp = u["input_tokens"] as? Int ?? 0
                let cch = u["cached_input_tokens"] as? Int ?? 0
                let outp = (u["output_tokens"] as? Int ?? 0)
                         + (u["reasoning_output_tokens"] as? Int ?? 0)
                let fresh = max(0, inp - cch)
                let key = model ?? "?"
                var e = agg[key] ?? ModelUse(requests: 0, fresh: 0, output: 0, model: key)
                e.requests += 1; e.fresh += fresh; e.output += outp
                agg[key] = e
                totals.f += fresh; totals.c += cch; totals.o += outp; totals.n += 1
                if newest == nil || ts > newest!.0 { newest = (ts, key) }
                span = (min(span?.0 ?? ts, ts), max(span?.1 ?? ts, ts))
                touched = true
            }
        }
        return touched
    }

    /// 当前 5 小时滚动窗口内的用量。
    ///
    /// 是滚动窗口而非到点清零的固定窗口 —— 历史数据里出现过 43 分钟内从 84%
    /// 掉到 0%，固定窗口做不到，滚动窗口在一批集中用量整体过期时可以。
    static func compute() -> Result {
        let since = Date().addingTimeInterval(-windowMinutes5h * 60)
        var agg: [String: ModelUse] = [:]
        var totals: (f: Int, c: Int, o: Int, n: Int) = (0, 0, 0, 0)
        var newest: (Date, String)? = nil
        var span: (Date, Date)? = nil
        var quota: (Date, [Double: Window])? = nil
        var sessions = 0
        for url in sessionFiles() {
            if scan(url, since: since, into: &agg, totals: &totals,
                    newest: &newest, span: &span, quota: &quota) { sessions += 1 }
        }
        var r = Result(windowStart: since, currentModel: newest?.1, sessions: sessions)
        r.fresh = totals.f; r.cached = totals.c; r.output = totals.o; r.requests = totals.n
        r.byModel = agg
        r.quota = quota?.1 ?? [:]
        r.quotaReadAt = quota?.0
        r.firstEvent = span?.0
        r.lastEvent = span?.1
        return r
    }
}
