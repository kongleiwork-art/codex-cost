import Foundation

/// 数据层：直接在 Swift 里解析 Codex 会话日志并套用成本模型。
///
/// 原先是 Process 调 `python3 codex_budget.py --json`，但干净的 macOS 上
/// /usr/bin/python3 只是个占位符，首次执行会要求安装 Xcode 命令行工具 ——
/// 别人下载 .app 后大概率跑不起来。移植到 Swift 后零外部依赖。
enum Budget {

    // MARK: 成本模型
    //
    // 每个模型四个系数：新增输入、缓存输入、输出侧（output+reasoning）、每次请求固定成本。
    // 不能用「单一乘数 × 基准公式」——astra 的缓存约是 sol 的 2 倍、输出约 6 倍，
    // 每请求底价则高一个数量级，用一个标量描述会在不同调用构成下明显漂移。
    //
    // 系数来自受控实验（详见仓库 research/）：
    //   sol   —— 全部 sol 测量联合非负拟合（research/refit.py），140 个回归点 RMS 0.84
    //   astra —— 纯 astra 回归；缓存费率由 astra/bigctx（约 12 万上下文）定下，约 25 万
    //   terra —— 只测到整体乘数（±0.11），按比例缩放
    //   5.5   —— 老模型，不参加对照；系数只用来折算历史用量（真实使用里它被低估 13%~33%）
    //   luna  —— 30 次调用零消耗
    // 缓存输入约比 fresh 便宜 12 倍。
    //
    // 计费规则：缓存失效后重读的老内容，服务端仍按缓存价计费，日志却把它记成
    // 新增输入（缓存计数归零）。判据是「上一次上下文 ≥3 万、这次一半以上按新增算」。
    // 不改判的话，带失效的长会话会被高估好几倍。
    //
    // 系数经历了五版：
    //   ① 「缓存免费」：分析 bug，续会话记累计 token 却被当增量求和，缓存虚增约 7 倍
    //   ② 677,444：拿 cache/bigctx 单组做残差，其余系数沿用缓存按零时拟合的旧值，重复计费
    //   ③ 481 条联合拟合：fresh 66,457、每请求 0.0819%。旧实验每次请求都顺带几千到两万
    //      fresh，两者同涨同落拆不开，大半 fresh 成本被记到了「每请求」上
    //   ④ 补上 req/* 和 ctx/* 后重拟合，按「读数滞后一次」对齐；每请求降到 0.0328%
    //   ⑤ 现在这版：发现上面那条计费规则。改判前，带缓存失效的实验组反解出缓存约 55 万
    //      tok/1%、不带失效的只有 37 万，联合拟合被迫折中（RMS 1.02）；改判后五组收敛到
    //      35~39 万，RMS 0.39（1% 量化噪声下限 0.29），每请求实测为 0。
    //      对照组两次（09-09、09-17）每次都是 0.211%，计费口径没变。
    //
    // 仍未对上：193 段真实使用片段仍比预测贵，相对误差中位数 −15%（近两月 −7%~−9%）。
    // 用真实数据直接回归会给出更贵的缓存（约 30 万 tok/1%），但误差大得多（RMS 6.8 对 0.39），
    // 且只基于一个账号，所以没有采用。缺口的候选解释是桌面版的后台请求。
    struct Coef {
        let fresh: Double?      // 每 1% 额度能买多少 fresh 输入 token；nil = 不计费
        let cached: Double?     // 每 1% 能买多少缓存输入 token
        let output: Double?
        let request: Double     // 每次请求的固定成本（%）
    }

    // 系数表（coef、fallback、counterfactualModels）在 Sources/Coefficients.swift，
    // 由 research/coefficients.json 生成。
    /// 缓存比 fresh 便宜几倍（界面文案用，由系数算出，不写死）
    static var cacheDiscount: Int {
        Int(((fallback.cached ?? 0) / (fallback.fresh ?? 1)).rounded())
    }
    static let windowMinutes5h = 300.0
    static let windowMinutesWeek = 10080.0

    static func cost(fresh: Int, cached: Int = 0, output: Int, requests: Int,
                     model: String?) -> Double {
        let c = coef[model ?? ""] ?? fallback
        guard let f = c.fresh, let o = c.output, let cc = c.cached else { return 0 }
        return Double(fresh) / f + Double(cached) / cc
             + Double(output) / o + c.request * Double(requests)
    }

    // MARK: 结果

    struct Window { var usedPercent: Double; var resetsAt: Double?; var stale: Bool }
    struct ModelUse { var requests: Int; var fresh: Int; var cached: Int; var output: Int
                      var pct: Double { Budget.cost(fresh: fresh, cached: cached,
                                                    output: output, requests: requests,
                                                    model: model) }
                      var model: String }
    /// 主额度之外的独立额度池 —— 周窗口的重置时间与主池不同。
    /// 例如主周额度打满后 Codex 切去的 gpt-reserve。
    struct Pool { var label: String; var window: Window; var requests: Int }
    /// 最近一次请求。用来提示「空闲太久，缓存可能已经失效」——
    /// 失效后接着用，整段上下文要按新增输入重读（sol 上约贵 12 倍）。
    struct LastRequest {
        var ts: Date
        var model: String
        var context: Int        // 这次请求的输入 token（含缓存），也就是接着用时要重读的上下文
        var idleMinutes: Double { Date().timeIntervalSince(ts) / 60 }
        /// 接着这段会话，每轮大约要花多少（%）。
        ///
        /// 花的是「每轮重新发送的上下文」，跟缓存有没有过期无关：缓存失效后重读的老
        /// 内容，服务端仍按缓存价计费（见顶部计费规则）。实测 recheck/bigctx 里含 2 次
        /// 失效的 39 次请求共 17%，与全部按缓存计的 16.4% 吻合；按新增输入计要 24%。
        var resumePct: Double {
            Budget.cost(fresh: 0, cached: context, output: 0, requests: 1, model: model)
        }
        /// 上下文够大、每轮开销值得一提时才显示。
        /// cli/codex_budget.py 的 last_request_info 用同一套规则。
        var worthShowing: Bool { context >= 30_000 && resumePct >= 0.2 }
    }
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
        var otherPools: [Pool] = []
        var lastRequest: LastRequest?

        /// 真正卡住你的那个窗口：5 小时与周额度里用得更满的那个。
        /// 周额度打满时，5 小时还剩多少都没用 —— 折叠态、菜单栏和提醒都该看这个。
        var binding: Window? {
            [fiveHour, weekly].compactMap { $0 }.max { $0.usedPercent < $1.usedPercent }
        }

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
        var costCached: Double { byModel.values.reduce(0) {
            guard let c = (Budget.coef[$1.model] ?? Budget.fallback).cached else { return $0 }
            return $0 + Double($1.cached) / c } }
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
            Budget.cost(fresh: fresh, cached: cached, output: output,
                        requests: requests, model: model)
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
    /// 要读哪些 rollout：一周内修改过的，最多 limit 个。
    ///
    /// 不能只挑「5 小时窗口内修改过」的：额度读数要从最新的一条记录里取，而那条
    /// 记录可能早于窗口（比如你几小时没用 Codex）；独立额度池按周窗口认，也要看得到
    /// 一周内的读数。一周以上没动过的文件对这两件事都没用 —— 之前不看时间只取最近
    /// 40 个，实测每次刷新有 391 MB 白读在这种文件上。全都太旧时留最新的一个兜底。
    private static func sessionFiles(limit: Int = 40) -> [URL] {
        // 与 Codex 自己一致：设了 CODEX_HOME 就用它，否则 ~/.codex。
        // 从 Finder / open 启动的 app 看不到 shell 里的变量，这主要给命令行和测试用。
        let home = ProcessInfo.processInfo.environment["CODEX_HOME"]
            .flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex")
        let root = home.appendingPathComponent("sessions")
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
        let horizon = Date().addingTimeInterval(-windowMinutesWeek * 60)
        let sorted = out.sorted { $0.1 > $1.1 }
        let fresh = sorted.filter { $0.1 >= horizon }.prefix(limit).map(\.0)
        return fresh.isEmpty ? Array(sorted.prefix(1).map(\.0)) : Array(fresh)
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

    /// 一条额度读数：来自某个 token_count 事件的 rate_limits
    fileprivate struct Reading { let ts: Date; let model: String; let windows: [Double: Window] }
    /// 一次真实请求的用量，附带它自己那条 rate_limits 属于哪个额度池
    /// 判定缓存失效：上一次请求的上下文至少这么大，这次却有一半以上按新增输入计
    static let missContext = 30_000

    fileprivate struct UsageEvent {
        let ts: Date; let model: String
        let fresh: Int; let cached: Int; let output: Int
        let has5h: Bool; let weeklyReset: Double?
    }

    /// 一个文件解析到哪儿了。折叠态每 30 秒扫一次，整份重读的代价太大：
    /// 实测一次 588 MB / 1.2 秒 CPU，开 10 小时累计 23 分钟 CPU。这里记住读到的
    /// 偏移量，之后每次只解析新增的字节；稳态下每次只有末尾几 KB。
    private struct FileState {
        var offset = 0            // 已经解析到这个字节位置（下一行的起点）
        var started = false       // 是否已经定过起点
        var model: String? = nil  // 解析到 offset 时的当前模型
        var lastContext = 0       // 解析到 offset 时上一次请求的上下文
        var events: [UsageEvent] = []
        var readings: [Reading] = []
        var latest: LastRequest? = nil
    }
    private static var fileStates: [String: FileState] = [:]
    /// 首次解析一个文件时最多往回读这么多 —— 200 MB 的会话日志也只读尾部
    private static let firstReadCap = 24 << 20

    /// 从 off 往后找到下一行的起点，保证不会从半行开始解析
    private static func lineStart(_ raw: UnsafeRawBufferPointer, _ off: Int) -> Int {
        if off <= 0 { return 0 }
        var i = off
        while i < raw.count, raw[i] != nl { i += 1 }
        return min(i + 1, raw.count)
    }

    /// 这一段开头那条记录的时间（连着几行都解析不出来就放弃）
    private static func firstTimestamp(_ raw: UnsafeRawBufferPointer, from: Int) -> Date? {
        var start = from, lines = 0
        while start < raw.count, lines < 200 {
            var end = start
            while end < raw.count, raw[end] != nl { end += 1 }
            defer { start = end + 1; lines += 1 }
            let len = end - start
            guard len > 40 else { continue }
            let line = Data(bytes: raw.baseAddress!.advanced(by: start), count: len)
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let ts = parseDate(obj["timestamp"] as? String ?? "") else { continue }
            return ts
        }
        return nil
    }

    /// 首次解析的起点：从尾部往回翻倍试探，直到这一段的开头早于窗口起点
    private static func firstOffset(_ raw: UnsafeRawBufferPointer, since: Date) -> Int {
        var back = 1 << 20
        while back < min(firstReadCap, raw.count) {
            let start = lineStart(raw, raw.count - back)
            if let ts = firstTimestamp(raw, from: start), ts < since { return start }
            back <<= 2
        }
        return lineStart(raw, max(0, raw.count - firstReadCap))
    }

    /// 从中间开始解析时，当前模型名可能写在起点之前 —— 往回找最近一条 turn_context。
    /// 找不到的话这些请求的模型是「?」，没有系数，成本会算成 0。
    private static func modelBefore(_ raw: UnsafeRawBufferPointer, _ offset: Int) -> String? {
        var end = offset
        while end > 0 {
            let start = max(0, end - (1 << 20))
            var found: String? = nil
            var i = start
            while i < end {
                var j = i
                while j < end, raw[j] != nl { j += 1 }
                let len = j - i
                if len > 40 {
                    let slice = UnsafeRawBufferPointer(rebasing: raw[i..<j])
                    if contains(slice, markCtx),
                       let obj = try? JSONSerialization.jsonObject(
                           with: Data(bytes: slice.baseAddress!, count: len)) as? [String: Any],
                       let payload = obj["payload"] as? [String: Any],
                       let m = payload["model"] as? String { found = m }
                }
                i = j + 1
            }
            if let found { return found }
            if start == 0 { break }
            end = start
        }
        return nil
    }

    private static func scan(_ url: URL, since: Date,
                            events: inout [UsageEvent],
                            readings: inout [Reading],
                            latest: inout LastRequest?) -> Bool {
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe) else { return false }
        let path = url.path
        var st = fileStates[path] ?? FileState()
        // 文件被截断或整个换掉了（归档、改名复用），之前的偏移量作废
        if st.offset > data.count { st = FileState() }
        if st.offset < data.count {
            data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
                if !st.started {
                    st.started = true
                    st.offset = firstOffset(raw, since: since)
                    if st.offset > 0 { st.model = modelBefore(raw, st.offset) }
                }
                parse(raw, to: data.count, since: since, into: &st)
            }
        }
        // 滑出窗口的用量事件丢掉，别无限攒；读数按周窗口留，至少留最新的一条
        st.events.removeAll { $0.ts < since }
        let weekAgo = Date().addingTimeInterval(-windowMinutesWeek * 60)
        if let newest = st.readings.max(by: { $0.ts < $1.ts }) {
            st.readings.removeAll { $0.ts < weekAgo }
            if st.readings.isEmpty { st.readings = [newest] }
        }
        fileStates[path] = st
        events.append(contentsOf: st.events)
        readings.append(contentsOf: st.readings)
        if let l = st.latest, latest == nil || l.ts > latest!.ts { latest = l }
        return !st.events.isEmpty
    }

    /// 解析 [st.offset, to) 这一段，把结果攒进 st。最后一行可能还没写完，留到下次。
    private static func parse(_ raw: UnsafeRawBufferPointer, to: Int,
                             since: Date, into st: inout FileState) {
        var model = st.model
        var lastContext = st.lastContext
        var start = st.offset
        defer { st.offset = start; st.model = model; st.lastContext = lastContext }
        do {
            while start < to {
                var end = start
                while end < to, raw[end] != nl { end += 1 }
                if end >= to { break }          // 半行：Codex 还在写，下次再读
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

                var got: [Double: Window] = [:]
                // 只认 Codex 自己的额度读数。同一类事件里也会出现别的限额：
                // base_model_inference（limit_name 为 gpt-reserve，周窗口恒为 0%、重置时间总在
                // 事件 7 天后）、premium（没有窗口）。它们不是独立额度池 —— 主池 5 小时读数
                // 照样跟着这些请求涨 —— 当成池子会把请求排除在估算之外。读数忽略，用量照算。
                if let rl = payload["rate_limits"] as? [String: Any],
                   (rl["limit_id"] as? String).map({ $0 == "codex" }) ?? true {
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
                }
                if let ts, !got.isEmpty {
                    st.readings.append(Reading(ts: ts, model: model ?? "?", windows: got))
                }

                // info 为空的 token_count 不是一次真实请求 —— 会话启动、额度
                // 刷新都会写这么一条。照计的话每条白加一次「每请求固定成本」。
                let info = payload["info"] as? [String: Any] ?? [:]
                guard let ts, let u = info["last_token_usage"] as? [String: Any],
                      !u.isEmpty else { continue }
                let inp = u["input_tokens"] as? Int ?? 0
                let cch = u["cached_input_tokens"] as? Int ?? 0
                let outp = (u["output_tokens"] as? Int ?? 0)
                         + (u["reasoning_output_tokens"] as? Int ?? 0)
                // 最近一次请求不受 5 小时窗口限制：空闲超过 5 小时回来，也要能提示缓存失效
                if st.latest == nil || ts > st.latest!.ts {
                    st.latest = LastRequest(ts: ts, model: model ?? "?", context: inp)
                }
                let prevContext = lastContext
                lastContext = inp
                guard ts >= since else { continue }
                // 缓存失效后重读的老内容，服务端仍按缓存价计费，日志却把它记成新增输入
                // （缓存计数清零）。不改判的话，带失效的长会话会被高估好几倍：各实验组
                // 反解的缓存费率原本从 37 万到 57 万各说各话，改判后收敛到 35~39 万。
                var fresh = max(0, inp - cch), cached = cch
                if prevContext >= missContext, inp > 0, fresh >= inp / 2 {
                    cached += fresh
                    fresh = 0
                }
                st.events.append(UsageEvent(ts: ts, model: model ?? "?",
                                            fresh: fresh, cached: cached, output: outp,
                                            has5h: got[windowMinutes5h] != nil,
                                            weeklyReset: got[windowMinutesWeek]?.resetsAt))
            }
        }
    }

    /// 两个周窗口是不是同一个桶：按重置时间认，相差一小时以内算同一个
    private static func sameBucket(_ a: Double?, _ b: Double?) -> Bool {
        guard let a, let b else { return false }
        return abs(a - b) < 3600
    }

    /// 当前 5 小时滚动窗口内的用量。
    ///
    /// 是滚动窗口而非到点清零的固定窗口 —— 历史数据里出现过 43 分钟内从 84%
    /// 掉到 0%，固定窗口做不到，滚动窗口在一批集中用量整体过期时可以。
    ///
    /// 额度读数要按「桶」分，不能只取时间上最新的一条：主周额度打满后，Codex 会
    /// 切到 gpt-reserve 这类模型，它的 rate_limits 同样是 limit_id=codex，报的却是
    /// 另一个周窗口（重置时间不同，也没有 5 小时窗口）。只取最新一条的话，备用池
    /// 的 36% 会盖掉主池的 100%，5 小时那行也随之消失 —— 恰好在最该准的时候显示错。
    static func compute() -> Result {
        let since = Date().addingTimeInterval(-windowMinutes5h * 60)
        var events: [UsageEvent] = []
        var readings: [Reading] = []
        var sessions = 0
        var latest: LastRequest? = nil
        for url in sessionFiles() {
            if scan(url, since: since, events: &events, readings: &readings, latest: &latest) {
                sessions += 1
            }
        }
        readings.sort { $0.ts < $1.ts }

        // ① 主池：最近一条带 5 小时窗口的读数，与它一起报出的周窗口就是主周额度。
        //    整段时期都不报 5 小时窗口时（7~8 月就是这样），退回用最新的周读数认主池。
        let main5 = readings.last(where: { $0.windows[windowMinutes5h] != nil })
        var mainReset = main5?.windows[windowMinutesWeek]?.resetsAt
        if mainReset == nil {
            mainReset = readings.last(where: { $0.windows[windowMinutesWeek] != nil })?
                .windows[windowMinutesWeek]?.resetsAt
        }
        var quota: [Double: Window] = [:]
        if let w = main5?.windows[windowMinutes5h] { quota[windowMinutes5h] = w }
        if let wk = readings.last(where: {
            sameBucket($0.windows[windowMinutesWeek]?.resetsAt, mainReset)
        })?.windows[windowMinutesWeek] {
            quota[windowMinutesWeek] = wk
        }

        // ② 其它池：周窗口的重置时间对不上主池、且还没重置的
        let now = Date().timeIntervalSince1970
        var poolWin: [Int: Window] = [:]
        var poolModels: [Int: Set<String>] = [:]
        for rd in readings {
            guard rd.windows[windowMinutes5h] == nil,
                  let wk = rd.windows[windowMinutesWeek], let ra = wk.resetsAt,
                  !sameBucket(ra, mainReset), ra > now else { continue }
            let k = Int(ra / 3600)
            poolWin[k] = wk
            poolModels[k, default: []].insert(rd.model)
        }

        // ③ 用量归属：走独立额度池的请求单独计数，不算进主池的 5 小时估算
        var agg: [String: ModelUse] = [:]
        var totals: (f: Int, c: Int, o: Int, n: Int) = (0, 0, 0, 0)
        var newest: (Date, String)? = nil
        var span: (Date, Date)? = nil
        var poolReq: [Int: Int] = [:]
        for e in events {
            if newest == nil || e.ts > newest!.0 { newest = (e.ts, e.model) }
            if !e.has5h, let ra = e.weeklyReset, !sameBucket(ra, mainReset),
               poolWin[Int(ra / 3600)] != nil {
                poolReq[Int(ra / 3600), default: 0] += 1
                continue
            }
            var m = agg[e.model] ?? ModelUse(requests: 0, fresh: 0, cached: 0,
                                             output: 0, model: e.model)
            m.requests += 1; m.fresh += e.fresh; m.cached += e.cached; m.output += e.output
            agg[e.model] = m
            totals.f += e.fresh; totals.c += e.cached; totals.o += e.output; totals.n += 1
            span = (min(span?.0 ?? e.ts, e.ts), max(span?.1 ?? e.ts, e.ts))
        }

        var r = Result(windowStart: since, currentModel: newest?.1, sessions: sessions)
        r.fresh = totals.f; r.cached = totals.c; r.output = totals.o; r.requests = totals.n
        r.byModel = agg
        r.quota = quota
        r.quotaReadAt = readings.last?.ts
        r.firstEvent = span?.0
        r.lastEvent = span?.1
        r.otherPools = poolWin.keys.sorted().map { k in
            Pool(label: (poolModels[k] ?? []).sorted().joined(separator: "/"),
                 window: poolWin[k]!, requests: poolReq[k] ?? 0)
        }
        r.lastRequest = latest
        return r
    }
}
