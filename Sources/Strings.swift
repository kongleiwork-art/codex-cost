import Foundation

/// 中英文案。按系统语言自动切换，不用 .lproj 资源包 —— 单文件应用，
/// 保持零构建配置。
enum L {
    static let isZH: Bool = {
        // --lang zh|en 可强制覆盖，出 README 素材时两版都要
        if let i = CommandLine.arguments.firstIndex(of: "--lang"),
           i + 1 < CommandLine.arguments.count {
            return CommandLine.arguments[i + 1].hasPrefix("zh")
        }
        let pref = Locale.preferredLanguages.first ?? "en"
        return pref.hasPrefix("zh")
    }()
    private static func s(_ zh: String, _ en: String) -> String { isZH ? zh : en }

    // 顶部
    static var currentModel   : String { s("当前模型", "Model") }
    static var totalUsage     : String { s("总体使用量", "of the 5-hour window") }
    static func windowCaption(_ n: Int) -> String {
        s("近 5 小时 · Codex 发出 \(n) 次请求",
          "Last 5 hours · \(n) requests from Codex")
    }
    // 成本构成
    static var freshIn        : String { s("新增输入", "New input") }
    static var modelOut       : String { s("模型输出", "Model output") }
    static var reqFloor       : String { s("请求开销", "Per-request") }
    static var cachedIn : String { s("缓存输入", "Cached input") }
    static func cachedNote(_ t: String, _ x: String) -> String {
        s("其中 \(t) 命中缓存，按 fresh 的 1/\(x) 计价",
          "\(t) of it came from cache, billed at ~1/\(x) of fresh")
    }
    static func choppy(_ pct: Int) -> String {
        s("请求太零碎，光固定开销就占了 \(pct)%",
          "Too many small requests — \(pct)% is just the per-request floor")
    }
    static func estGap(_ v: Double) -> String {
        s(String(format: "估算偏差 %+.0f%%", v), String(format: "Estimate off by %+.0f%%", v))
    }
    static var trustServer    : String { s("以下面的实际额度为准", "Trust the figures below") }
    // 分区
    static var quotaSection   : String { s("额度使用", "Quota") }
    static var modelsSection  : String { s("各模型消耗", "By model") }
    static var altSection     : String { s("换成单一模型的话", "If it had all run on one model") }
    static var fiveHour       : String { s("5 小时", "5 hours") }
    static var weekly         : String { s("每周", "Weekly") }
    /// 独立额度池（周窗口重置时间与主池不同）的行标签，例如「reserve 周额度」
    static func poolWeekly(_ m: String) -> String { s("\(m) 周额度", "\(m) weekly") }
    static func otherPoolNote(_ n: Int, _ m: String) -> String {
        s("另有 \(n) 次请求走 \(m) 的独立额度，没算进上面的 5 小时",
          "\(n) more requests drew on \(m)'s separate quota, not counted above")
    }
    static func times(_ n: Int) -> String { s("\(n) 次", "\(n)×") }
    static var current        : String { s("当前", "now") }
    // 底栏 / 菜单
    static var settings       : String { s("设置", "Settings") }
    static var justUpdated    : String { s("刚刚更新", "Just updated") }
    static func updatedAgo(_ m: Int) -> String {
        s("\(m) 分钟前更新", "Updated \(m)m ago")
    }
    static var refresh        : String { s("刷新", "Refresh") }
    static var quit           : String { s("退出", "Quit") }
    static var noLogs         : String { s("没找到 Codex 会话记录", "No Codex session logs found") }
    static var resetIn        : String { s("后重置", "left") }
    static var resetDone      : String { s("已重置", "reset") }
    // 提醒
    static func alertTitle(_ p: Int, weekly: Bool = false) -> String {
        weekly ? s("周额度已用 \(p)%", "\(p)% of your weekly quota is gone")
               : s("5 小时额度已用 \(p)%", "\(p)% of your 5-hour quota is gone")
    }
    static func alertBody(_ mins: String, _ model: String) -> String {
        s("按当前速率约 \(mins) 后撞上限。当前模型 \(model)。",
          "About \(mins) left at this burn rate. Currently on \(model).")
    }
    static func switchHint(_ from: String, _ to: String, _ save: Double) -> String {
        s("换成 \(to) 可省约 \(Int(save))%", "Switching to \(to) would save about \(Int(save))%")
    }

    // 缓存失效提示
    static func lastRequestIdle(_ t: String, _ ctx: String) -> String {
        s("上次请求在 \(t)前 · 上下文 \(ctx) tok", "Last request \(t) ago · \(ctx) tokens of context")
    }
    static func cacheResume(likely: Bool, miss: Double, hit: Double) -> String {
        let m = String(format: "%.1f", miss), h = String(format: "%.1f", hit)
        return likely
            ? s("缓存大概率已失效，接着这段会话约花 \(m)%（缓存还在只要 \(h)%）",
                "Cache has most likely expired: resuming costs about \(m)% (\(h)% if still warm)")
            : s("缓存可能已失效，接着这段会话约花 \(m)%（缓存还在只要 \(h)%）",
                "Cache may have expired: resuming costs about \(m)% (\(h)% if still warm)")
    }

    // 历史用量
    static var tabQuota: String { s("当前额度", "Quota") }
    static var tabHistory: String { s("历史用量", "History") }
    static var periodToday: String { s("今天", "Today") }
    static var periodWeek: String { s("7 天", "7 days") }
    static var periodMonth: String { s("30 天", "30 days") }
    static var periodAll: String { s("全部", "All") }
    static var usageTotal: String { s("合计", "Total") }
    static var usageByModel: String { s("按模型", "By model") }
    static var usageUnknownModel: String { s("未知模型", "Unknown model") }
    static var usageUnknownNote: String {
        s("「未知模型」来自日志里没记录模型名的会话，多是早期或走自定义 provider 的会话",
          "“Unknown model” covers sessions whose logs never recorded a model name — mostly early or custom-provider sessions")
    }
    static var usageEmpty: String { s("这段时间没有用量记录", "No usage in this period") }
    static var usageIndexing: String {
        s("正在建立索引，首次要扫描全部日志…", "Indexing logs — the first run scans everything…")
    }
    static var usageNoLocal: String {
        s("Cursor、Gemini、网页和桌面聊天在本地没有 token 记录，未计入",
          "Cursor, Gemini, and web/desktop chats keep no local token counts — not included")
    }
    static func usageSince(_ day: String) -> String { s("最早记录 \(day)", "Earliest record \(day)") }
    static func usageBreakdown(_ i: String, _ r: String, _ w: String, _ o: String, _ n: Int) -> String {
        s("新增 \(i) · 缓存读 \(r) · 缓存写 \(w) · 输出 \(o) · \(n) 次",
          "new \(i) · cache read \(r) · cache write \(w) · out \(o) · \(n) req")
    }
    static func usageQuotaPct(_ p: Double) -> String {
        s("折合 5 小时额度 \(Int(p.rounded()))%", "≈ \(Int(p.rounded()))% of a 5h window")
    }
    static func usageQuotaWindows(_ x: Double) -> String {
        s(String(format: "折合 %.1f 个 5 小时额度", x), String(format: "≈ %.1f× a 5h window", x))
    }
}
