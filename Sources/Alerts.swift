import Foundation
import UserNotifications

/// 阈值提醒。这是整个工具里唯一能改变行为的功能 —— 其余都是事后看。
///
/// 每个额度窗口内每档只提醒一次（按 resets_at 区分窗口），
/// 避免每 30 秒刷新一次就弹一条。
@MainActor
final class Alerts {
    static let shared = Alerts()
    private var fired: [String: Set<Int>] = [:]     // 窗口标识 -> 已触发的档位
    private var authorized = false
    private var asked = false

    private let thresholds = [80, 95]

    /// UNUserNotificationCenter 在非 .app 包环境里会直接抛
    /// NSInternalInconsistencyException（bundleProxyForCurrentProcess is nil），
    /// 不是返回错误 —— 所以必须在调用前判断，不能靠 try/catch。
    private static let canNotify: Bool = {
        Bundle.main.bundleIdentifier != nil
            && Bundle.main.bundleURL.pathExtension == "app"
    }()

    func prepare() {
        guard Self.canNotify, !asked else { return }
        asked = true
        UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound]) { ok, _ in
                Task { @MainActor in self.authorized = ok }
            }
    }

    /// 返回 true 表示这次刷新触发了提醒（UI 可以据此做视觉强调）。
    @discardableResult
    func check(_ r: Budget.Result) -> Bool {
        guard let w = r.binding, !w.stale else { return false }   // 周额度打满也要提醒
        // 用 resets_at 标识窗口：窗口一滚动，已触发记录自动作废
        let key = String(format: "%.0f", w.resetsAt ?? 0)
        var done = fired[key] ?? []
        var triggered = false
        for t in thresholds where w.usedPercent >= Double(t) && !done.contains(t) {
            done.insert(t)
            triggered = true
            notify(threshold: t, r: r)
        }
        fired[key] = done
        if fired.count > 8 { fired.removeValue(forKey: fired.keys.first!) }
        return triggered
    }

    private func notify(threshold: Int, r: Budget.Result) {
        let model = shortModel(r.currentModel)
        var body: String
        if let mins = r.minutesToWall, mins.isFinite, mins > 0 {
            let m = Int(mins)
            let human = m >= 60 ? "\(m / 60)h\(String(format: "%02d", m % 60))m" : "\(m)m"
            body = L.alertBody(human, model)
        } else {
            body = L.alertBody("—", model)
        }
        // 有更便宜的模型能明显省，就一并给出建议 —— 把研究结论变成可执行的话
        if let cur = r.currentModel, let curCost = Budget.coef[cur],
           curCost.fresh != nil {
            let alt = Budget.coef.keys
                .map { ($0, r.counterfactual($0)) }
                .filter { $0.1 < r.spent * 0.7 && $0.1 > 0 }
                .min { $0.1 < $1.1 }
            if let alt, alt.0 != cur {
                body += "\n" + L.switchHint(shortModel(cur), shortModel(alt.0),
                                            r.spent - alt.1)
            }
        }
        guard Self.canNotify, authorized else { return }
        let c = UNMutableNotificationContent()
        c.title = L.alertTitle(threshold,
                               weekly: (r.weekly?.usedPercent ?? -1) > (r.fiveHour?.usedPercent ?? -1))
        c.body = body
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "quota-\(threshold)-\(Date().timeIntervalSince1970)",
                                  content: c, trigger: nil))
    }
}
