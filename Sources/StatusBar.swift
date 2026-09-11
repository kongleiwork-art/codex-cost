import AppKit
import SwiftUI

/// 菜单栏模式。Mac mini、Mac Studio、任何外接显示器都没有刘海 ——
/// 没有这个回退，这些机器上应用等于不可用。
@MainActor
final class StatusBarController {
    private let item: NSStatusItem
    private let popover = NSPopover()
    private var store: Store

    init(store: Store) {
        self.store = store
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        popover.behavior = .transient
        popover.contentSize = NSSize(width: 372, height: 470)
        popover.contentViewController = NSHostingController(
            rootView: PanelWrapper(store: store))
        if let b = item.button {
            b.target = self
            b.action = #selector(toggle)
            b.imagePosition = .imageLeading
        }
        render()
    }

    func render() {
        guard let b = item.button else { return }
        let used = store.snap?.binding?.usedPercent ?? 0   // 看更满的那个窗口
        let cfg = NSImage.SymbolConfiguration(pointSize: 12, weight: .semibold)
        b.image = NSImage(systemSymbolName: symbol(for: used),
                          accessibilityDescription: nil)?
                  .withSymbolConfiguration(cfg)
        b.title = " " + String(format: "%.0f%%", used)
        b.contentTintColor = used >= 85 ? .systemRed : (used >= 60 ? .systemOrange : nil)
    }

    private func symbol(for v: Double) -> String {
        switch v {
        case ..<25:  return "gauge.low"
        case ..<75:  return "gauge.medium"
        default:     return "gauge.high"
        }
    }

    @objc private func toggle() {
        if popover.isShown { popover.performClose(nil); return }
        store.refresh()
        guard let b = item.button else { return }
        popover.show(relativeTo: b.bounds, of: b, preferredEdge: .minY)
    }
}

/// 菜单栏弹出层里复用同一套展开视图，外面套个深色玻璃底。
private struct PanelWrapper: View {
    @ObservedObject var store: Store
    var body: some View {
        Expanded(snap: store.snap, error: store.error,
                 lastRefresh: store.lastRefresh) { store.refresh() }
            .frame(width: 372)
            .background {
                ZStack {
                    Backdrop()
                    Color.black.opacity(0.55)
                }
            }
            .environment(\.colorScheme, .dark)
    }
}
