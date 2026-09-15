import AppKit
import SwiftUI
import Combine

// MARK: - 数据

typealias Snapshot = Budget.Result

@MainActor
final class Store: ObservableObject {
    @Published var snap: Snapshot?
    @Published var error: String?
    @Published var lastRefresh = Date()
    /// 菜单栏模式下用来刷新状态栏标题
    var onUpdate: (() -> Void)?

    // 历史用量
    // --history：启动即停在「历史用量」页，截图和录 demo 用（同 --expanded，免得要程序化点击）
    @Published var tab: PanelTab = CommandLine.arguments.contains("--history") ? .history : .quota
    @Published var period: Usage.Period = .week
    @Published var usage: Usage.Summary?
    @Published var usageLoading = false
    private var usageRecords: [Usage.Record]?
    private var usageTimer: Timer?

    private var timer: Timer?

    init() {
        refresh()
        if tab == .history { refreshUsage() }
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        // 历史总账只在打开过「历史用量」之后才定时更新，平时不扫日志
        usageTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            Task { @MainActor in
                if self?.usageRecords != nil { self?.refreshUsage() }
            }
        }
    }

    func refresh() {
        Task.detached(priority: .utility) {
            let r = Budget.compute()          // 纯 Swift，无外部依赖
            await MainActor.run {
                if r.requests == 0 && r.quota.isEmpty {
                    self.error = L.noLogs
                } else {
                    self.snap = r; self.error = nil
                    Alerts.shared.check(r)
                }
                self.lastRefresh = Date()
                self.onUpdate?()
            }
        }
    }

    func select(tab t: PanelTab) {
        tab = t
        if t == .history && usageRecords == nil { refreshUsage() }
        onUpdate?()
    }

    func select(period p: Usage.Period) {
        period = p
        if let recs = usageRecords { usage = Usage.summarize(recs, period: p) }
        onUpdate?()
    }

    /// 更新索引、重新汇总。首次要扫全部日志，放后台。
    func refreshUsage() {
        guard !usageLoading else { return }
        usageLoading = true
        onUpdate?()
        Task.detached(priority: .utility) {
            let recs = Usage.loadRecords()
            await MainActor.run {
                self.usageRecords = recs
                self.usage = Usage.summarize(recs, period: self.period)
                self.usageLoading = false
                self.onUpdate?()
            }
        }
    }
}

// MARK: - 视图

enum Palette {
    static let read  = Color(red: 0.30, green: 0.68, blue: 1.00)
    static let write = Color(red: 0.72, green: 0.44, blue: 1.00)
    static let cached = Color(red: 0.36, green: 0.80, blue: 0.72)   // 青：缓存输入
    static let floor = Color(red: 1.00, green: 0.74, blue: 0.25)
    static let other = Color(red: 1.00, green: 0.42, blue: 0.40)
    static func quota(_ v: Double) -> Color {
        v >= 85 ? Color(red: 1, green: 0.38, blue: 0.36)
        : v >= 60 ? Color(red: 1, green: 0.74, blue: 0.30)
                  : Color(red: 0.36, green: 0.85, blue: 0.52)
    }
}

func fmtTokens(_ n: Int) -> String {
    if n >= 1_000_000_000 { return String(format: "%.2fB", Double(n) / 1_000_000_000) }
    if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
    if n >= 1_000 { return String(format: "%.0fK", Double(n) / 1_000) }
    return "\(n)"
}
func fmtLeft(_ ts: Double?) -> String {
    guard let ts else { return "" }
    let m = Int((ts - Date().timeIntervalSince1970) / 60)
    if m <= 0 { return L.resetDone }
    if m >= 2880 { return L.isZH ? "\(m / 1440) 天" : "\(m / 1440)d" }
    if m >= 60 { return "\(m / 60)h\(String(format: "%02d", m % 60))m" }
    return "\(m)m"
}
/// 空闲时长：72 分钟 → 「1 小时 12 分钟」/「1h12m」
func fmtIdle(_ minutes: Double) -> String {
    let m = Int(minutes)
    if m >= 60 {
        return L.isZH ? "\(m / 60) 小时 \(m % 60) 分钟" : "\(m / 60)h\(String(format: "%02d", m % 60))m"
    }
    return L.isZH ? "\(m) 分钟" : "\(m)m"
}
func shortModel(_ m: String?) -> String {
    (m ?? "-").replacingOccurrences(of: "gpt-", with: "")
}

struct Meter: View {
    let value: Double
    var tint: Color = .white
    var height: CGFloat = 6
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(.white.opacity(0.10))
                Capsule().fill(tint)
                    .frame(width: max(4, g.size.width * min(1, max(0, value) / 100)))
            }
        }
        .frame(height: height)
    }
}

struct StackedBar: View {
    let parts: [(Double, Color)]
    var height: CGFloat = 7
    var body: some View {
        GeometryReader { g in
            let total = max(parts.reduce(0) { $0 + $1.0 }, 0.0001)
            HStack(spacing: 2) {
                ForEach(Array(parts.enumerated()), id: \.offset) { _, p in
                    Capsule().fill(p.1)
                        .frame(width: max(0, g.size.width * (p.0 / total) - 2))
                }
            }
        }
        .frame(height: height)
    }
}

struct Dot: View {
    let c: Color
    var d: CGFloat = 6
    var body: some View { Circle().fill(c).frame(width: d, height: d) }
}

struct SectionLabel: View {
    let t: String
    var body: some View {
        Text(t).font(.system(size: 11))
            .foregroundStyle(.white.opacity(0.40))
    }
}

// MARK: 历史用量

enum PanelTab: CaseIterable {
    case quota, history
    var label: String { self == .quota ? L.tabQuota : L.tabHistory }
}

extension Usage.Period {
    var label: String {
        switch self {
        case .today: return L.periodToday
        case .week:  return L.periodWeek
        case .month: return L.periodMonth
        case .all:   return L.periodAll
        }
    }
}

/// 小胶囊按钮：标签页和时段选择共用
struct Pill: View {
    let text: String
    let selected: Bool
    let action: () -> Void
    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(.white.opacity(selected ? 0.95 : 0.45))
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(.white.opacity(selected ? 0.14 : 0)))
            .contentShape(Capsule())
            .onTapGesture(perform: action)
    }
}

/// 标签页切换。Expanded 本身不观察 store，切标签要一个观察 store 的视图才会重绘。
struct PanelSwitch<Quota: View>: View {
    @ObservedObject var store: Store
    @ViewBuilder let quota: () -> Quota
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 4) {
                ForEach(PanelTab.allCases, id: \.self) { t in
                    Pill(text: t.label, selected: store.tab == t) { store.select(tab: t) }
                }
                Spacer()
            }
            .padding(.bottom, 12)
            if store.tab == .history {
                HistoryView(store: store)
            } else {
                quota()
            }
        }
    }
}

struct HistoryView: View {
    @ObservedObject var store: Store

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 4) {
                ForEach(Usage.Period.allCases, id: \.self) { p in
                    Pill(text: p.label, selected: store.period == p) { store.select(period: p) }
                }
                Spacer()
                if store.usageLoading { ProgressView().controlSize(.mini) }
            }
            if let s = store.usage {
                content(s)
            } else {
                Text(L.usageIndexing)
                    .font(.system(size: 11)).foregroundStyle(.white.opacity(0.5))
                    .padding(.vertical, 26)
            }
            Text(L.usageNoLocal)
                .font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.32))
                .padding(.top, 12)
        }
    }

    @ViewBuilder func content(_ s: Usage.Summary) -> some View {
        let total = s.tools.reduce(0) { $0 + $1.total }
        DailyBars(values: s.daily.map(\.tokens))
            .frame(height: 40)
            .padding(.top, 12)
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Text(L.usageTotal).font(.system(size: 11)).foregroundStyle(.white.opacity(0.45))
            Spacer()
            Text(fmtTokens(total))
                .font(.system(size: 22, weight: .semibold, design: .rounded))
                .monospacedDigit().foregroundStyle(.white)
            Text("tokens").font(.system(size: 10)).foregroundStyle(.white.opacity(0.4))
        }
        .padding(.top, 10)
        if s.tools.isEmpty {
            Text(L.usageEmpty)
                .font(.system(size: 11)).foregroundStyle(.white.opacity(0.5))
                .padding(.top, 8)
        } else {
            sep()
            VStack(spacing: 10) {
                ForEach(s.tools) { t in
                    toolRow(t, share: total > 0 ? Double(t.total) / Double(total) : 0)
                }
            }
            sep()
            SectionLabel(t: L.usageByModel)
            VStack(spacing: 7) {
                ForEach(s.models) { m in
                    HStack(spacing: 6) {
                        Text(m.model == "?" ? L.usageUnknownModel : shortModel(m.model))
                            .font(.system(size: 11)).foregroundStyle(.white.opacity(0.8)).lineLimit(1)
                        Text(m.tool.label)
                            .font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.35))
                        Spacer()
                        Text(fmtTokens(m.tokens))
                            .font(.system(size: 11, weight: .medium)).monospacedDigit()
                            .foregroundStyle(.white.opacity(0.9))
                    }
                }
            }
            .padding(.top, 9)
            if s.models.contains(where: { $0.model == "?" }) {
                Text(L.usageUnknownNote)
                    .font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.32))
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 6)
            }
        }
        if let first = s.firstDay {
            Text(L.usageSince(first))
                .font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.32))
                .padding(.top, 10)
        }
    }

    @ViewBuilder func toolRow(_ t: Usage.ToolTotal, share: Double) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(t.tool.label)
                    .font(.system(size: 12, weight: .medium)).foregroundStyle(.white.opacity(0.92))
                Spacer()
                Text(extra(t)).font(.system(size: 10)).foregroundStyle(.white.opacity(0.5))
                Text(fmtTokens(t.total))
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .monospacedDigit().foregroundStyle(.white)
                    .frame(minWidth: 52, alignment: .trailing)
            }
            Meter(value: share * 100, tint: Palette.read, height: 4)
            Text(L.usageBreakdown(fmtTokens(t.input), fmtTokens(t.cacheRead),
                                  fmtTokens(t.cacheWrite), fmtTokens(t.output), t.requests))
                .font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.38)).lineLimit(1)
        }
    }

    /// 工具行右侧的补充：Codex 折合额度，opencode 自带费用，Claude Code 没有额度系数
    func extra(_ t: Usage.ToolTotal) -> String {
        switch t.tool {
        case .codex:
            return t.quotaPct >= 100 ? L.usageQuotaWindows(t.quotaPct / 100) : L.usageQuotaPct(t.quotaPct)
        case .opencode:
            return t.cost > 0 ? String(format: "$%.2f", t.cost) : ""
        case .claudeCode:
            return ""
        }
    }

    @ViewBuilder func sep() -> some View {
        Rectangle().fill(.white.opacity(0.08)).frame(height: 1).padding(.vertical, 13)
    }
}

/// 每日用量柱状图，最右边一根是今天
struct DailyBars: View {
    let values: [Int]
    var body: some View {
        GeometryReader { g in
            let peak = max(values.max() ?? 0, 1)
            HStack(alignment: .bottom, spacing: 2) {
                ForEach(Array(values.enumerated()), id: \.offset) { i, v in
                    RoundedRectangle(cornerRadius: 1.5)
                        .fill(i == values.count - 1 ? Palette.read : .white.opacity(v > 0 ? 0.35 : 0.08))
                        .frame(height: max(2, g.size.height * CGFloat(v) / CGFloat(peak)))
                }
            }
            .frame(maxHeight: .infinity, alignment: .bottom)
        }
    }
}

// MARK: 折叠态（与刘海融为一体）

struct Collapsed: View {
    let snap: Snapshot?
    let notchWidth: CGFloat
    var body: some View {
        // 看更满的那个窗口：周额度打满时 5 小时还剩多少都没用
        let used = snap?.binding?.usedPercent ?? 0
        HStack(spacing: 0) {
            HStack(spacing: 5) {
                Dot(c: Palette.quota(used), d: 7)
                Text(shortModel(snap?.currentModel))
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.white.opacity(0.92))
                    .lineLimit(1)
            }
            .padding(.leading, 11)
            Spacer(minLength: notchWidth)
            HStack(spacing: 7) {
                Text(String(format: "%.1f%%", used))
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(.white.opacity(0.92))
                Ring(value: used)
            }
            .padding(.trailing, 10)
        }
    }
}

/// 折叠态右侧的小圆环
struct Ring: View {
    let value: Double
    var body: some View {
        ZStack {
            Circle().stroke(.white.opacity(0.16), lineWidth: 3)
            Circle()
                .trim(from: 0, to: min(1, max(0.02, value / 100)))
                .stroke(Palette.quota(value), style: StrokeStyle(lineWidth: 3, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .frame(width: 15, height: 15)
    }
}

// MARK: 展开态

struct Expanded: View {
    let snap: Snapshot?
    let error: String?
    let lastRefresh: Date
    var store: Store? = nil
    let onRefresh: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // 有 store 才有标签页；离屏渲染（README 截图）没有 store，保持原样
            if let store {
                PanelSwitch(store: store) { quotaPage() }
            } else {
                quotaPage()
            }
        }
        .padding(.horizontal, 17)
        .padding(.top, 15)
        .padding(.bottom, 11)
    }

    /// 「当前额度」页，也就是原来的整个面板
    @ViewBuilder func quotaPage() -> some View {
            if let error {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 11)).foregroundStyle(.orange)
                    .padding(.vertical, 26).frame(maxWidth: .infinity)
            } else if let s = snap {
                header(s)
                composition(s)
                cacheHint(s)
                sep()
                quota(s)
                if !s.byModel.isEmpty { sep(); models(s) }
                sep()
                alternatives(s)
                footer()
            } else {
                HStack { Spacer(); ProgressView().controlSize(.small); Spacer() }
                    .padding(.vertical, 30)
            }
    }

    @ViewBuilder func sep() -> some View {
        Rectangle().fill(.white.opacity(0.08))
            .frame(height: 1).padding(.vertical, 13)
    }

    // 顶部：模型胶囊 + 大数字 + 说明
    @ViewBuilder func header(_ s: Snapshot) -> some View {
        HStack(alignment: .top) {
            HStack(spacing: 4) {
                Text(shortModel(s.currentModel))
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.95))
                Image(systemName: "chevron.right")
                    .font(.system(size: 8, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.4))
            }
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(Capsule().fill(.white.opacity(0.11)))
            Spacer()
            VStack(alignment: .trailing, spacing: 1) {
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text(String(format: "%.1f", s.spent))
                        .font(.system(size: 27, weight: .semibold, design: .rounded))
                        .monospacedDigit().foregroundStyle(.white)
                    Text("%").font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.white.opacity(0.45))
                }
                Text(L.totalUsage)
                    .font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.38))
            }
        }
    }

    // 成本构成
    @ViewBuilder func composition(_ s: Snapshot) -> some View {
        let cf = s.costFresh, cc = s.costCached, co = s.costOutput, cr = s.costRequest
        let gap = max(0, s.gap)
        let showGap = gap > max(4, (s.fiveHour?.usedPercent ?? 0) * 0.2)
        var parts: [(Double, Color)] {
            var p: [(Double, Color)] = [(cf, Palette.read), (cc, Palette.cached),
                                        (co, Palette.write), (cr, Palette.floor)]
            if showGap { p.append((gap, Palette.other)) }
            return p
        }
        StackedBar(parts: parts).padding(.top, 13)
        // 四项一行放不下（372pt 宽，名字会被截成"新增…"），排成 2×2
        Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 6) {
            GridRow {
                legend(L.freshIn, cf, Palette.read)
                legend(L.cachedIn, cc, Palette.cached)
            }
            GridRow {
                legend(L.modelOut, co, Palette.write)
                legend(L.reqFloor, cr, Palette.floor)
            }
        }
        .padding(.top, 9)
        ForEach(Array(s.otherPools.filter { $0.requests > 0 }.enumerated()), id: \.offset) { _, p in
            Text(L.otherPoolNote(p.requests, shortModel(p.label)))
                .font(.system(size: 9.5)).foregroundStyle(Palette.other.opacity(0.9))
                .padding(.top, 5)
        }
        if s.cached > 0 {
            Text(L.cachedNote(fmtTokens(s.cached), String(Budget.cacheDiscount)))
                .font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.34))
                .padding(.top, 6)
        }
        let sum = max(cf + cc + co + cr, 0.0001)
        if cr / sum > 0.35 && s.requests > 10 {
            Text(L.choppy(Int(cr / sum * 100)))
                .font(.system(size: 9.5)).foregroundStyle(Palette.floor.opacity(0.9))
                .padding(.top, 4)
        }
        if showGap {
            HStack(spacing: 5) {
                Dot(c: Palette.other, d: 6)
                Text(L.estGap(s.gap)).font(.system(size: 10, weight: .medium))
                    .foregroundStyle(Palette.other.opacity(0.95))
                Text(L.trustServer).font(.system(size: 9))
                    .foregroundStyle(.white.opacity(0.32))
            }
            .padding(.top, 6)
        }
    }

    /// 空闲太久：接着这段会话，缓存可能已经失效，整段上下文要按新增输入重读
    @ViewBuilder func cacheHint(_ s: Snapshot) -> some View {
        if let lr = s.lastRequest, let hint = lr.cacheHint {
            let likely = hint == .likely
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 5) {
                    Image(systemName: "clock.arrow.circlepath").font(.system(size: 10))
                    Text(L.lastRequestIdle(fmtIdle(lr.idleMinutes), fmtTokens(lr.context)))
                        .font(.system(size: 10, weight: .medium))
                }
                .foregroundStyle(likely ? Palette.floor.opacity(0.95) : .white.opacity(0.62))
                Text(L.cacheResume(likely: likely, miss: lr.resumeMissPct, hit: lr.resumeHitPct))
                    .font(.system(size: 9.5))
                    .foregroundStyle(.white.opacity(0.45))
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.top, 10)
        }
    }

    @ViewBuilder func legend(_ n: String, _ v: Double, _ c: Color) -> some View {
        HStack(spacing: 4) {
            Dot(c: c, d: 6)
            Text(n).font(.system(size: 10)).foregroundStyle(.white.opacity(0.55))
            Text(String(format: "%.1f", v))
                .font(.system(size: 10.5, weight: .medium, design: .rounded))
                .monospacedDigit().foregroundStyle(.white.opacity(0.9))
        }
    }

    // 额度：标签在左、加粗
    @ViewBuilder func quotaRow(_ name: String, _ w: Budget.Window) -> some View {
        HStack(spacing: 11) {
            Text(name).font(.system(size: 11, weight: .medium))
                .foregroundStyle(.white.opacity(0.9))
                .lineLimit(1).fixedSize()
                .frame(minWidth: 44, alignment: .leading)
            Meter(value: w.usedPercent, tint: Palette.quota(w.usedPercent))
            Text("\(Int(w.usedPercent))%")
                .font(.system(size: 11, weight: .semibold, design: .rounded))
                .monospacedDigit().foregroundStyle(.white)
                .frame(width: 34, alignment: .trailing)
            Text(fmtLeft(w.resetsAt))
                .font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.34))
                .frame(width: 42, alignment: .trailing)
        }
    }

    @ViewBuilder func quota(_ s: Snapshot) -> some View {
        SectionLabel(t: L.quotaSection)
        VStack(spacing: 9) {
            if let w = s.fiveHour { quotaRow(L.fiveHour, w) }
            if let w = s.weekly { quotaRow(L.weekly, w) }
            // 主额度之外的独立额度池（例如主周额度打满后切去的 reserve）
            ForEach(Array(s.otherPools.enumerated()), id: \.offset) { _, p in
                quotaRow(L.poolWeekly(shortModel(p.label)), p.window)
            }
        }
        .padding(.top, 10)
    }

    @ViewBuilder func models(_ s: Snapshot) -> some View {
        SectionLabel(t: L.modelsSection)
        let mx = max(s.byModel.values.map(\.pct).max() ?? 1, 0.0001)
        VStack(spacing: 8) {
            ForEach(s.byModel.sorted { $0.value.pct > $1.value.pct }, id: \.key) { k, v in
                let cur = k == s.currentModel
                HStack(spacing: 10) {
                    Text(shortModel(k))
                        .font(.system(size: 11, weight: cur ? .semibold : .regular))
                        .foregroundStyle(.white.opacity(cur ? 0.95 : 0.62))
                        .frame(width: 62, alignment: .leading)
                    Meter(value: v.pct / mx * 100,
                          tint: cur ? Palette.read : .white.opacity(0.30), height: 5)
                    Text(L.times(v.requests)).font(.system(size: 9.5))
                        .foregroundStyle(.white.opacity(0.34))
                        .frame(width: 34, alignment: .trailing)
                    Text(String(format: "%.1f%%", v.pct))
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .monospacedDigit().foregroundStyle(.white.opacity(0.92))
                        .frame(width: 42, alignment: .trailing)
                }
            }
        }
        .padding(.top, 10)
    }

    @ViewBuilder func alternatives(_ s: Snapshot) -> some View {
        SectionLabel(t: L.altSection)
        let rows = Budget.counterfactualModels.map { ($0, s.counterfactual($0)) }.sorted { $0.1 < $1.1 }
        let mx = max(rows.map(\.1).max() ?? 1, 0.0001)
        VStack(spacing: 8) {
            ForEach(rows, id: \.0) { k, v in
                let cur = k == s.currentModel
                HStack(spacing: 10) {
                    Text(shortModel(k))
                        .font(.system(size: 11, weight: cur ? .semibold : .regular))
                        .foregroundStyle(.white.opacity(cur ? 0.95 : 0.6))
                        .frame(width: 62, alignment: .leading)
                    Meter(value: v / mx * 100,
                          tint: cur ? Palette.read : .white.opacity(0.26), height: 5)
                    Text(String(format: "%.1f%%", v))
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(.white.opacity(cur ? 0.95 : 0.62))
                        .frame(width: 52, alignment: .trailing)
                }
            }
        }
        .padding(.top, 10)
    }

    @ViewBuilder func footer() -> some View {
        HStack {
            Button(action: onRefresh) {
                HStack(spacing: 5) {
                    Image(systemName: "arrow.clockwise").font(.system(size: 10))
                    Text(L.refresh).font(.system(size: 10.5))
                }
                .foregroundStyle(.white.opacity(0.55))
            }
            .buttonStyle(.plain)
            Spacer()
            HStack(spacing: 5) {
                Dot(c: Color(red: 0.36, green: 0.85, blue: 0.52), d: 6)
                Text(freshness).font(.system(size: 10)).foregroundStyle(.white.opacity(0.42))
            }
        }
        .padding(.top, 15)
    }

    var freshness: String {
        let m = Int(Date().timeIntervalSince(lastRefresh) / 60)
        return m < 1 ? L.justUpdated : L.updatedAgo(m)
    }
}

// MARK: 根视图

struct Root: View {
    @ObservedObject var store: Store
    let notchWidth: CGFloat
    let onResize: (Bool) -> Void
    @State private var expanded = CommandLine.arguments.contains("--expanded")

    var body: some View {
        Group {
            if expanded {
                Expanded(snap: store.snap, error: store.error,
                         lastRefresh: store.lastRefresh,
                         store: store) { store.refresh() }
            } else {
                Collapsed(snap: store.snap, notchWidth: notchWidth)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background {
            // 折叠态贴着刘海，只圆下面两角；展开态是独立浮层，四角全圆
            // 展开态上沿紧贴刘海下沿，所以顶部两角只给很小的圆角 ——
            // 全圆角会在接缝处露出缺口，看着像两块独立的东西。
            let shape = AnyInsettableShape(expanded
                ? AnyInsettableShape(UnevenRoundedRectangle(
                    topLeadingRadius: 6, bottomLeadingRadius: 22,
                    bottomTrailingRadius: 22, topTrailingRadius: 6, style: .continuous))
                : AnyInsettableShape(UnevenRoundedRectangle(
                    bottomLeadingRadius: 13, bottomTrailingRadius: 13, style: .continuous)))
            GlassPanel(shape: shape, expanded: expanded)
        }
        .onTapGesture {
            expanded.toggle(); onResize(expanded)
            if expanded { store.refresh() }
        }
        .contextMenu {
            Button(L.refresh) { store.refresh() }
            Divider()
            Button(L.quit) { NSApp.terminate(nil) }
        }
    }
}

/// 类型擦除的 InsettableShape，好让折叠/展开用不同形状走同一条渲染路径
struct AnyInsettableShape: InsettableShape {
    private let _path: (CGRect) -> Path
    private let _inset: (CGFloat) -> AnyInsettableShape
    init<S: InsettableShape>(_ s: S) {
        _path = { s.path(in: $0) }
        _inset = { AnyInsettableShape(s.inset(by: $0)) }
    }
    func path(in rect: CGRect) -> Path { _path(rect) }
    func inset(by amount: CGFloat) -> AnyInsettableShape { _inset(amount) }
}

/// 真正的背景模糊：SwiftUI 的 .ultraThinMaterial 在无边框窗口里只是叠色。
struct Backdrop: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material = .underPageBackground
        v.blendingMode = .behindWindow
        v.state = .active
        // 钉死深色：否则材质跟随系统外观，背后是浅色窗口时整块发白，白字读不出来
        v.appearance = NSAppearance(named: .darkAqua)
        return v
    }
    func updateNSView(_ v: NSVisualEffectView, context: Context) {}
}

struct GlassPanel: View {
    let shape: AnyInsettableShape
    var expanded: Bool
    var body: some View {
        if expanded {
            shape.fill(.clear)
                .background { Backdrop() }
                .background {
                    LinearGradient(colors: [Color(white: 0.13).opacity(0.90),
                                            Color(white: 0.03).opacity(0.95)],
                                   startPoint: .top, endPoint: .bottom)
                }
                .overlay {
                    LinearGradient(stops: [
                        .init(color: .white.opacity(0.055), location: 0.0),
                        .init(color: .white.opacity(0.012), location: 0.4),
                        .init(color: .clear, location: 0.7)],
                        startPoint: .topLeading, endPoint: .bottomTrailing)
                }
                .overlay {
                    shape.strokeBorder(
                        LinearGradient(colors: [.white.opacity(0.30), .white.opacity(0.09),
                                                .white.opacity(0.05)],
                                       startPoint: .top, endPoint: .bottom), lineWidth: 0.7)
                }
                .clipShape(shape)
        } else {
            // 折叠态要和刘海融成一块。刘海是纯黑、没有描边也没有高光，
            // 毛玻璃、渐变或描边任何一样都会让它看起来是贴在刘海旁边的一条灰块。
            shape.fill(Color.black)
        }
        // 不加投影：SwiftUI 的 .shadow 只能画在窗口范围内，超出的部分被裁掉，
        // 边缘留下一圈矩形硬边；窗口自带的阴影再沿这圈半透明像素描出一个矩形框。
    }
}

// MARK: - 窗口

/// 无边框窗口，悬在刘海正下方。从不成为 key：看一眼额度，不该抢走你正在打字的那个应用的焦点。
final class NotchWindow: NSWindow {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    /// macOS 默认不允许窗口盖住菜单栏区域，会把 frame 往下推（实测 44px）。
    /// 刘海挂件必须贴在屏幕最顶端，所以原样返回。
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
        frameRect
    }

    init(contentRect: NSRect) {
        super.init(contentRect: contentRect, styleMask: [.borderless],
                   backing: .buffered, defer: false)
        isOpaque = false
        backgroundColor = .clear
        // 窗口是透明的异形面板，系统阴影会沿窗口矩形描边，见 GlassPanel
        hasShadow = false
        level = .statusBar
        collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        isMovable = false
        ignoresMouseEvents = false
        appearance = NSAppearance(named: .darkAqua)
    }
}

/// 刘海几何。没有刘海的机器退化成「屏幕顶端居中」。
struct Notch {
    let screen: NSScreen
    var height: CGFloat { max(screen.safeAreaInsets.top, 24) }
    var centerX: CGFloat {
        guard #available(macOS 12.0, *),
              let l = screen.auxiliaryTopLeftArea, let r = screen.auxiliaryTopRightArea,
              r.minX > l.maxX else { return screen.frame.midX }
        return (l.maxX + r.minX) / 2
    }
    var topY: CGFloat { screen.frame.maxY - height }
    /// 刘海物理宽度。没有刘海时给 0。
    var width: CGFloat {
        guard #available(macOS 12.0, *),
              let l = screen.auxiliaryTopLeftArea, let r = screen.auxiliaryTopRightArea,
              r.minX > l.maxX else { return 0 }
        return r.minX - l.maxX
    }
}

extension Expanded {
    static let width: CGFloat = 372
    /// 展开态需要的真实高度：按固定宽度把面板排一遍版量出来。
    ///
    /// 不能写死或按行数估：独立额度池、池子说明、估算偏差提示都是可有可无的行，
    /// 少算一行，内容就会把顶部的模型和总数挤出窗口。
    @MainActor
    static func fittingHeight(_ snap: Snapshot?, error: String? = nil,
                              store: Store? = nil) -> CGFloat {
        // 传 store 才会按真实的任务输入区排版（多了工作目录一行、状态行）；
        // 不传就是离屏渲染用的占位版，两者高度不同
        let host = NSHostingController(rootView:
            Expanded(snap: snap, error: error, lastRefresh: Date(), store: store) {}
                .frame(width: width))
        let h = host.sizeThatFits(in: CGSize(width: width, height: 4000)).height
        // 量出来接近上限说明有纵向可伸缩的内容，量不准 —— 退回旧的固定高度
        return h > 3900 ? 470 : ceil(h)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NotchWindow!
    var store: Store!
    var collapsedSize: NSSize {
        let n = Notch(screen: window?.screen ?? NSScreen.main ?? NSScreen.screens[0])
        // 比刘海两侧各宽 62pt，看起来就是刘海变宽了；高度与刘海齐平
        return NSSize(width: (n.width > 0 ? n.width : 180) + 124, height: n.height)
    }
    var expandedSize: NSSize {
        NSSize(width: Expanded.width,
               height: Expanded.fittingHeight(store?.snap, error: store?.error, store: store))
    }

    var statusBar: StatusBarController?
    var expandedNow = CommandLine.arguments.contains("--expanded")

    func applicationDidFinishLaunching(_ note: Notification) {
        store = Store()
        Alerts.shared.prepare()

        // 没有刘海就退回菜单栏：Mac mini / Studio / 外接显示器都属于这种，
        // 没有这个分支它们上面等于不可用。
        // --menubar 可以在有刘海的机器上强制用菜单栏（有人就是偏好这个）。
        let n0 = Notch(screen: NSScreen.main ?? NSScreen.screens[0])
        let forceMenuBar = CommandLine.arguments.contains("--menubar")
        guard n0.width > 0, !forceMenuBar else {
            let sb = StatusBarController(store: store)
            statusBar = sb
            store.onUpdate = { [weak sb] in sb?.render() }
            return
        }


        let screen = NSScreen.main ?? NSScreen.screens[0]
        window = NotchWindow(contentRect: .zero)
        let root = Root(store: store,
                        notchWidth: n0.width > 0 ? n0.width : 180,
                        onResize: { [weak self] expanded in
            guard let self else { return }
            self.expandedNow = expanded
            self.place(expanded ? self.expandedSize : self.collapsedSize, animated: true)
        })
        // 展开着的时候出现或消失一个独立额度池，窗口高度要跟着变
        store.onUpdate = { [weak self] in
            guard let self, self.expandedNow,
                  self.window.frame.height != self.expandedSize.height else { return }
            self.place(self.expandedSize, animated: true)
        }
        let host = NSHostingView(rootView: root)
        host.autoresizingMask = [.width, .height]
        window.contentView = host
        // --expanded：启动即展开。截图和录 demo 用 —— 程序化点击要辅助功能权限，
        // 这样就不需要了。
        place(CommandLine.arguments.contains("--expanded") ? expandedSize : collapsedSize,
              animated: false)
        window.orderFrontRegardless()
        _ = screen
    }

    /// 把窗口摆到刘海正下方、水平居中于刘海。
    func place(_ size: NSSize, animated: Bool) {
        let screen = window.screen ?? NSScreen.main ?? NSScreen.screens[0]
        let n = Notch(screen: screen)
        // 两种状态都贴着屏幕顶端：折叠态与刘海齐平（两侧露出的部分看起来就是
        // 刘海变宽了）；展开态上沿正好接在刘海下沿，看起来是从刘海里长出来的，
        // 而不是底下悬着一块。之前留了 8pt 间隙，视觉上就断开了。
        let isExpanded = size.height > n.height + 40
        let top = isExpanded ? screen.frame.maxY - n.height : screen.frame.maxY
        let frame = NSRect(x: (n.centerX - size.width / 2).rounded(),
                           y: (top - size.height).rounded(),
                           width: size.width, height: size.height)
        if animated {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.22
                ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
                window.animator().setFrame(frame, display: true)
            }
        } else {
            window.setFrame(frame, display: true)
        }
    }
}

@main
enum Launcher {
    @MainActor static func main() {
        // --usage-json [today|week|month|all]：更新索引后打印历史总账，测试和排查用
        if let i = CommandLine.arguments.firstIndex(of: "--usage-json") {
            let args = CommandLine.arguments
            let period = (i + 1 < args.count ? Usage.Period(rawValue: args[i + 1]) : nil) ?? .all
            let s = Usage.summarize(Usage.loadRecords(), period: period)
            let obj: [String: Any] = [
                "period": period.rawValue, "records": s.records,
                "first_day": s.firstDay ?? NSNull(),
                "tools": s.tools.map {
                    ["tool": $0.tool.rawValue, "requests": $0.requests, "input": $0.input,
                     "cacheRead": $0.cacheRead, "cacheWrite": $0.cacheWrite, "output": $0.output,
                     "cost": $0.cost, "quotaPct": $0.quotaPct] as [String: Any]
                },
                "models": s.models.map {
                    ["tool": $0.tool.rawValue, "model": $0.model, "tokens": $0.tokens] as [String: Any]
                },
                "daily": s.daily.map { ["day": $0.day, "tokens": $0.tokens] as [String: Any] },
            ]
            let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .prettyPrinted])
            print(String(data: data, encoding: .utf8)!)
            exit(0)
        }
        // --dump 必须在 NSApplication 启动前处理：一旦 app.run() 起来，
        // 这是个常驻 GUI 进程，不会退出（之前误以为是卡死）。
        // --social <路径>：生成 1280×640 社交预览封面
        if let i = CommandLine.arguments.firstIndex(of: "--social"),
           i + 1 < CommandLine.arguments.count {
            let app = NSApplication.shared
            app.setActivationPolicy(.prohibited)
            // --social <输出> [面板 PNG]：面板图默认取 docs/panel-en.png，
            // docs/render.sh 会传刚渲染好的那张，免得拼进旧图
            let args = CommandLine.arguments
            let panel = i + 2 < args.count && !args[i + 2].hasPrefix("--")
                ? args[i + 2] : "docs/panel-en.png"
            Renderer.social(to: args[i + 1], panelPNG: panel)
            exit(0)
        }
        // --render <路径>：离屏出图，不需要屏幕亮着
        if let i = CommandLine.arguments.firstIndex(of: "--render"),
           i + 1 < CommandLine.arguments.count {
            let app = NSApplication.shared
            app.setActivationPolicy(.prohibited)
            Renderer.run(to: CommandLine.arguments[i + 1])
        }
        if CommandLine.arguments.contains("--dump") {
            let r = Budget.compute()
            // --dump --json：机器可读，回归测试拿它和 CLI 的 --json 对账
            if CommandLine.arguments.contains("--json") {
                func win(_ w: Budget.Window) -> [String: Any] {
                    ["used_percent": w.usedPercent, "resets_at": w.resetsAt ?? NSNull(),
                     "stale": w.stale]
                }
                var quota: [String: Any] = [:]
                for (k, w) in r.quota { quota[String(Int(k))] = win(w) }
                var models: [String: Any] = [:]
                for (k, v) in r.byModel {
                    models[k] = ["requests": v.requests, "pct": v.pct] as [String: Any]
                }
                let obj: [String: Any] = [
                    "requests": r.requests, "fresh": r.fresh, "cached": r.cached,
                    "output": r.output, "spent": r.spent,
                    "current_model": r.currentModel ?? NSNull(),
                    "by_model": models, "quota": quota,
                    "pools": r.otherPools.map {
                        ["label": $0.label, "requests": $0.requests,
                         "window": win($0.window)] as [String: Any]
                    },
                    "binding": r.binding.map { $0.usedPercent as Any } ?? NSNull(),
                    "last_request": r.lastRequest.map { lr -> Any in
                        ["model": lr.model, "context": lr.context,
                         "idle_minutes": lr.idleMinutes,
                         "resume_miss_pct": lr.resumeMissPct, "resume_hit_pct": lr.resumeHitPct,
                         "cache_hint": lr.cacheHint.map { $0.rawValue as Any } ?? NSNull()]
                            as [String: Any]
                    } ?? NSNull(),
                ]
                let data = try! JSONSerialization.data(withJSONObject: obj,
                                                       options: [.sortedKeys, .prettyPrinted])
                print(String(data: data, encoding: .utf8)!)
                exit(0)
            }
            print("requests   \(r.requests)")
            print("fresh      \(r.fresh)")
            print("cached     \(r.cached)")
            print("output     \(r.output)")
            print("spent      " + String(format: "%.4f", r.spent))
            print("  fresh    " + String(format: "%.4f", r.costFresh))
            print("  output   " + String(format: "%.4f", r.costOutput))
            print("  request  " + String(format: "%.4f", r.costRequest))
            print("current    \(r.currentModel ?? "-")")
            for (k, v) in r.byModel.sorted(by: { $0.value.pct > $1.value.pct }) {
                print("  \(k)  \(v.requests) 次  " + String(format: "%.4f", v.pct))
            }
            for (w, q) in r.quota.sorted(by: { $0.key < $1.key }) {
                print(String(format: "quota %.0f  %.0f%%", w, q.usedPercent)
                      + (q.stale ? " (已重置)" : ""))
            }
            for p in r.otherPools {
                print("pool  \(p.label)  " + String(format: "%.0f%%", p.window.usedPercent)
                      + "  \(p.requests) 次请求")
            }
            if let b = r.binding { print(String(format: "binding %.0f%%", b.usedPercent)) }
            if let lr = r.lastRequest {
                print("last   \(lr.model)  context \(lr.context)  idle \(Int(lr.idleMinutes))m  "
                      + String(format: "resume %.2f%% / %.2f%%  ", lr.resumeMissPct, lr.resumeHitPct)
                      + "hint \(lr.cacheHint?.rawValue ?? "-")")
            }
            exit(0)
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)   // 不占 Dock，不抢焦点
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
