import AppKit
import SwiftUI

/// 离屏渲染面板到 PNG。
///
/// 用途有二：一是屏幕锁着/不在电脑旁时也能出图；二是让 README 的截图
/// 可复现 —— 任何人跑一条命令就能重新生成，不用手动摆窗口再截屏。
///
/// 注意：NSVisualEffectView 的背景模糊离屏渲染不出来（它需要真实的窗口后方内容），
/// 所以这里换成静态的深色渐变底 —— 视觉上接近，且不会渲染成透明。
enum Renderer {

    /// 社交预览封面 1280×640。GitHub 的 Settings → Social preview 用这个尺寸，
    /// 链接被贴到 X / Slack / 微信时展开显示的就是它。
    /// 注意：这张图只能在网页端手工上传，GitHub 没有开放对应的 API。
    @MainActor
    static func social(to path: String, panelPNG: String) {
        let W: CGFloat = 1280, H: CGFloat = 640
        let panel = NSImage(contentsOfFile: panelPNG)

        let card = ZStack {
            LinearGradient(colors: [Color(red: 0.07, green: 0.08, blue: 0.10),
                                    Color(red: 0.02, green: 0.02, blue: 0.03)],
                           startPoint: .topLeading, endPoint: .bottomTrailing)
            // 右上角一抹冷光，避免整块死黑
            RadialGradient(colors: [Color(red: 0.30, green: 0.68, blue: 1.0).opacity(0.20),
                                    .clear],
                           center: .init(x: 0.82, y: 0.10), startRadius: 10, endRadius: 520)

            HStack(alignment: .center, spacing: 56) {
                VStack(alignment: .leading, spacing: 0) {
                    Text("codex-cost")
                        .font(.system(size: 62, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                    Text("Codex quota in your Mac's notch — and what your\ntokens would have cost on another model.")
                        .font(.system(size: 23, weight: .regular))
                        .foregroundStyle(.white.opacity(0.62))
                        .lineSpacing(6)
                        .padding(.top, 18)
                    VStack(alignment: .leading, spacing: 11) {
                        bullet("655 controlled trials behind the cost model",
                               Color(red: 0.30, green: 0.68, blue: 1.00))
                        bullet("Native Swift · no dependencies",
                               Color(red: 0.72, green: 0.44, blue: 1.00))
                        bullet("Nothing leaves your Mac",
                               Color(red: 0.36, green: 0.85, blue: 0.52))
                    }
                    .padding(.top, 34)
                }
                .frame(width: 600, alignment: .leading)

                if let panel {
                    Image(nsImage: panel)
                        .resizable().aspectRatio(contentMode: .fit)
                        .frame(height: 500)
                        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                        .shadow(color: .black.opacity(0.6), radius: 30, y: 14)
                }
            }
            .padding(.horizontal, 64)
        }
        .frame(width: W, height: H)
        .environment(\.colorScheme, .dark)

        write(card, to: path, scale: 1)
    }

    @ViewBuilder
    private static func bullet(_ t: String, _ c: Color) -> some View {
        HStack(spacing: 11) {
            Circle().fill(c).frame(width: 9, height: 9)
            Text(t).font(.system(size: 19)).foregroundStyle(.white.opacity(0.82))
        }
    }

    @MainActor
    private static func write<V: View>(_ v: V, to path: String, scale: CGFloat) {
        let r = ImageRenderer(content: v)
        r.scale = scale
        r.isOpaque = true
        guard let cg = r.cgImage,
              let png = NSBitmapImageRep(cgImage: cg)
                        .representation(using: .png, properties: [:]) else {
            FileHandle.standardError.write("渲染失败\n".data(using: .utf8)!); exit(1)
        }
        try? png.write(to: URL(fileURLWithPath: path))
        print("已写出 \(path)  \(cg.width)x\(cg.height)")
    }

    @MainActor
    static func run(to path: String, scale: CGFloat = 2) {
        let snap = Budget.compute()

        let size = CGSize(width: Expanded.width, height: Expanded.fittingHeight(snap))
        let content = ZStack {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(LinearGradient(colors: [Color(white: 0.13), Color(white: 0.03)],
                                     startPoint: .top, endPoint: .bottom))
            Expanded(snap: snap, error: nil, lastRefresh: Date()) {}
        }
        .frame(width: size.width, height: size.height)
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(LinearGradient(
                    colors: [.white.opacity(0.30), .white.opacity(0.08)],
                    startPoint: .top, endPoint: .bottom), lineWidth: 0.7)
        }
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .environment(\.colorScheme, .dark)

        // 用 SwiftUI 的 ImageRenderer 而不是 NSHostingView + cacheDisplay：
        // 后者要先建位图再改 size，AppKit 只在原尺寸区域绘制，结果面板缩在角落。
        // ImageRenderer 原生支持 scale，出图就是 Retina 分辨率。
        let renderer = ImageRenderer(content: content)
        renderer.scale = scale
        renderer.isOpaque = false
        guard let cg = renderer.cgImage else {
            FileHandle.standardError.write("渲染失败\n".data(using: .utf8)!)
            exit(1)
        }
        let rep = NSBitmapImageRep(cgImage: cg)
        guard let png = rep.representation(using: .png, properties: [:]) else {
            FileHandle.standardError.write("PNG 编码失败\n".data(using: .utf8)!)
            exit(1)
        }
        do {
            try png.write(to: URL(fileURLWithPath: path))
            print("已写出 \(path)  \(cg.width)x\(cg.height)")
        } catch {
            FileHandle.standardError.write("写文件失败：\(error)\n".data(using: .utf8)!)
            exit(1)
        }
        exit(0)
    }

    /// 「历史用量」页离屏出图。数据来自环境变量指向的目录（CODEX_HOME、CLAUDE_CONFIG_DIR、
    /// XDG_DATA_HOME、CODEX_COST_DATA_DIR）；docs/render.sh 指向 tests/usage_fixtures.py
    /// 生成的演示数据，不读你自己的日志。
    @MainActor
    static func history(to path: String, period: Usage.Period, scale: CGFloat = 2) {
        let summary = Usage.summarize(Usage.loadRecords(), period: period)
        let page = VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 4) {
                ForEach(PanelTab.allCases, id: \.self) { t in
                    Pill(text: t.label, selected: t == .history) {}
                }
                Spacer()
            }
            .padding(.bottom, 12)
            HistoryView(summary: summary, period: period)
        }
        .padding(.horizontal, 17).padding(.top, 15).padding(.bottom, 11)
        .frame(width: Expanded.width)
        let height = ceil(NSHostingController(rootView: page)
            .sizeThatFits(in: CGSize(width: Expanded.width, height: 4000)).height)
        let content = ZStack(alignment: .top) {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(LinearGradient(colors: [Color(white: 0.13), Color(white: 0.03)],
                                     startPoint: .top, endPoint: .bottom))
            page
        }
        .frame(width: Expanded.width, height: height)
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(LinearGradient(colors: [.white.opacity(0.30), .white.opacity(0.08)],
                                             startPoint: .top, endPoint: .bottom), lineWidth: 0.7)
        }
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .environment(\.colorScheme, .dark)

        let r = ImageRenderer(content: content)
        r.scale = scale
        r.isOpaque = false
        guard let cg = r.cgImage,
              let png = NSBitmapImageRep(cgImage: cg).representation(using: .png, properties: [:]) else {
            FileHandle.standardError.write("渲染失败\n".data(using: .utf8)!)
            exit(1)
        }
        do {
            try png.write(to: URL(fileURLWithPath: path))
            print("已写出 \(path)  \(cg.width)x\(cg.height)")
        } catch {
            FileHandle.standardError.write("写文件失败：\(error)\n".data(using: .utf8)!)
            exit(1)
        }
        exit(0)
    }
}
