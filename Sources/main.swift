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
    private var timer: Timer?

    init() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    func refresh() {
        Task.detached(priority: .utility) {
            let r = Budget.compute()          // 纯 Swift，无外部依赖
            await MainActor.run {
                if r.requests == 0 && r.quota.isEmpty {
                    self.error = "没找到 Codex 会话记录"
                } else {
                    self.snap = r; self.error = nil
                    Alerts.shared.check(r)
                }
                self.lastRefresh = Date()
                self.onUpdate?()
            }
        }
    }
}

// MARK: - 视图

enum Palette {
    static let read  = Color(red: 0.30, green: 0.68, blue: 1.00)
    static let write = Color(red: 0.72, green: 0.44, blue: 1.00)
    static let floor = Color(red: 1.00, green: 0.74, blue: 0.25)
    static let other = Color(red: 1.00, green: 0.42, blue: 0.40)
    static func quota(_ v: Double) -> Color {
        v >= 85 ? Color(red: 1, green: 0.38, blue: 0.36)
        : v >= 60 ? Color(red: 1, green: 0.74, blue: 0.30)
                  : Color(red: 0.36, green: 0.85, blue: 0.52)
    }
}

func fmtTokens(_ n: Int) -> String {
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

// MARK: 折叠态（与刘海融为一体）

struct Collapsed: View {
    let snap: Snapshot?
    let notchWidth: CGFloat
    var body: some View {
        let used = snap?.fiveHour?.usedPercent ?? 0
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
    let onRefresh: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let error {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 11)).foregroundStyle(.orange)
                    .padding(.vertical, 26).frame(maxWidth: .infinity)
            } else if let s = snap {
                header(s)
                composition(s)
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
        .padding(.horizontal, 17)
        .padding(.top, 15)
        .padding(.bottom, 11)
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
        let cf = s.costFresh, co = s.costOutput, cr = s.costRequest
        let gap = max(0, s.gap)
        let showGap = gap > max(4, (s.fiveHour?.usedPercent ?? 0) * 0.2)
        var parts: [(Double, Color)] {
            var p: [(Double, Color)] = [(cf, Palette.read), (co, Palette.write), (cr, Palette.floor)]
            if showGap { p.append((gap, Palette.other)) }
            return p
        }
        StackedBar(parts: parts).padding(.top, 13)
        HStack(spacing: 13) {
            legend(L.freshIn, cf, Palette.read)
            legend(L.modelOut, co, Palette.write)
            legend(L.reqFloor, cr, Palette.floor)
            Spacer(minLength: 0)
        }
        .padding(.top, 9)
        if s.cached > 0 {
            Text(L.cachedFree(fmtTokens(s.cached)))
                .font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.34))
                .padding(.top, 6)
        }
        let sum = max(cf + co + cr, 0.0001)
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
    @ViewBuilder func quota(_ s: Snapshot) -> some View {
        SectionLabel(t: L.quotaSection)
        VStack(spacing: 9) {
            ForEach([(L.fiveHour, s.fiveHour), (L.weekly, s.weekly)], id: \.0) { name, w in
                if let w {
                    HStack(spacing: 11) {
                        Text(name).font(.system(size: 11, weight: .medium))
                            .foregroundStyle(.white.opacity(0.9))
                            .frame(width: 44, alignment: .leading)
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
        let rows = Budget.coef.keys.map { ($0, s.counterfactual($0)) }.sorted { $0.1 < $1.1 }
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
    @State private var hovering = false

    var body: some View {
        Group {
            if expanded {
                Expanded(snap: store.snap, error: store.error,
                         lastRefresh: store.lastRefresh) { store.refresh() }
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
        .scaleEffect(hovering && !expanded ? 1.03 : 1.0, anchor: .top)
        .animation(.spring(response: 0.3, dampingFraction: 0.72), value: hovering)
        .onHover { hovering = $0 }
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
        shape.fill(.clear)
            .background { Backdrop() }
            .background {
                LinearGradient(colors: [Color(white: 0.13).opacity(expanded ? 0.90 : 0.94),
                                        Color(white: 0.03).opacity(expanded ? 0.95 : 0.97)],
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
            .shadow(color: .black.opacity(expanded ? 0.55 : 0.30),
                    radius: expanded ? 28 : 10, y: expanded ? 12 : 4)
    }
}

// MARK: - 窗口

/// 无边框窗口，悬在刘海正下方。不抢焦点、跟随所有 Space。
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
        hasShadow = true
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
        let n = Notch(screen: window?.screen ?? NSScreen.main ?? NSScreen.screens[0])
        return NSSize(width: 372, height: 470)
    }

    var statusBar: StatusBarController?

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
            self.place(expanded ? self.expandedSize : self.collapsedSize, animated: true)
        })
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
        // --dump 必须在 NSApplication 启动前处理：一旦 app.run() 起来，
        // 这是个常驻 GUI 进程，不会退出（之前误以为是卡死）。
        // --social <路径>：生成 1280×640 社交预览封面
        if let i = CommandLine.arguments.firstIndex(of: "--social"),
           i + 1 < CommandLine.arguments.count {
            let app = NSApplication.shared
            app.setActivationPolicy(.prohibited)
            Renderer.social(to: CommandLine.arguments[i + 1],
                            panelPNG: "docs/panel-en.png")
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
            exit(0)
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)   // 不占 Dock，不抢焦点
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
