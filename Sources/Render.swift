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
    @MainActor
    static func run(to path: String, scale: CGFloat = 2) {
        let snap = Budget.compute()

        let size = CGSize(width: 372, height: 470)
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
}
