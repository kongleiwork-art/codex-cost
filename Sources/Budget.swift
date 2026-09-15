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
    // 系数经历了四版：
    //   ① 「缓存免费」：分析 bug，续会话记累计 token 却被当增量求和，缓存虚增约 7 倍
    //   ② 677,444：拿 cache/bigctx 单组做残差，其余系数沿用缓存按零时拟合的旧值，重复计费
    //   ③ 481 条联合拟合：fresh 66,457、每请求 0.0819%。旧实验每次请求都顺带几千到两万
    //      fresh，两者同涨同落拆不开，大半 fresh 成本被记到了「每请求」上
    //   ④ 现在这版：补上 req/*（缓存总量相同、请求数差 4 倍）和 ctx/*（请求数固定、只扫
    //      上下文规模）后重拟合，按「读数滞后一次」对齐。req 成对比较直接解出每请求约 0，
    //      ctx 四组落在一条直线上 —— 缓存成本与上下文大小成正比。
    //
    // 仍未对上：真实 148 次长会话预测 88%，实测 82%（③ 是 81.9%）。分段验证 MAE 1.13
    // （③ 0.99），但平均偏差 +0.31（③ +0.61）、最大误差 −2.1（③ +3.9）。
    // astra 只有 4 段数据：缓存费率逐段删除仍在 25~30 万，但 fresh 与每请求此消彼长
    // （fresh 从 2.5 万到 8 万拟合误差几乎一样），留一交叉验证 1.67。
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
        /// 缓存还在时，接着用一次的代价（%）
        var resumeHitPct: Double {
            Budget.cost(fresh: 0, cached: context, output: 0, requests: 1, model: model)
        }
        /// 缓存已失效时的代价：整段上下文按新增输入重读
        var resumeMissPct: Double {
            Budget.cost(fresh: context, cached: 0, output: 0, requests: 1, model: model)
        }
        enum Hint: String { case maybe, likely }
        /// 空闲越久越容易失效：实测 10–30 分钟约四分之一，超过 1 小时约九成（见 README）。
        /// 上下文小、重读也不贵，或者空闲超过半天（多半已经换了事做），都不提示。
        /// cli/codex_budget.py 的 last_request_info 用同一套规则。
        var cacheHint: Hint? {
            guard context >= 30_000, resumeMissPct >= 0.5 else { return nil }
            let m = idleMinutes
            if m >= 60 && m <= 12 * 60 { return .likely }
            if m >= 10 && m < 60 { return .maybe }
            return nil
        }
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
    /// 不能只挑「窗口内修改过」的：额度读数要从最新的一条记录里取，而那条
    /// 记录可能早于窗口（比如你几小时没用 Codex）。窗口过滤只作用于用量累加。
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

    /// 一条额度读数：来自某个 token_count 事件的 rate_limits
    fileprivate struct Reading { let ts: Date; let model: String; let windows: [Double: Window] }
    /// 一次真实请求的用量，附带它自己那条 rate_limits 属于哪个额度池
    fileprivate struct UsageEvent {
        let ts: Date; let model: String
        let fresh: Int; let cached: Int; let output: Int
        let has5h: Bool; let weeklyReset: Double?
    }

    private static func scan(_ url: URL, since: Date,
                            events: inout [UsageEvent],
                            readings: inout [Reading],
                            latest: inout LastRequest?) -> Bool {
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
                    readings.append(Reading(ts: ts, model: model ?? "?", windows: got))
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
                if latest == nil || ts > latest!.ts {
                    latest = LastRequest(ts: ts, model: model ?? "?", context: inp)
                }
                guard ts >= since else { continue }
                events.append(UsageEvent(ts: ts, model: model ?? "?",
                                         fresh: max(0, inp - cch), cached: cch, output: outp,
                                         has5h: got[windowMinutes5h] != nil,
                                         weeklyReset: got[windowMinutesWeek]?.resetsAt))
                touched = true
            }
        }
        return touched
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
