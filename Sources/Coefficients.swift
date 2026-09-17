// 由 research/sync_coefficients.py 从 research/coefficients.json 生成，不要手改。
// 改系数：编辑 JSON，再运行 python3 research/sync_coefficients.py。

extension Budget {
    /// 成本系数 v5（2026-09-17），来龙去脉见 Budget.swift 顶部注释
    static let coef: [String: Coef] = [
        "gpt-5.6-sol":  Coef(fresh: 35_646, cached: 364_295, output: 13_859, request: 0.0),
        "gpt-5.5":      Coef(fresh: 35_646, cached: 364_295, output: 13_859, request: 0.0),
        "gpt-5.6-terra":Coef(fresh: 35_646, cached: 364_295, output: 13_859, request: 0.0),
        "gpt-5.6-luna": Coef(fresh: nil, cached: nil, output: nil, request: 0.0),
        "gpt-6-astra":  Coef(fresh: 14_174, cached: 129_305, output: 2_364, request: 0.2239),
    ]
    /// 没有系数的模型按它估算
    static let fallback = coef["gpt-5.6-sol"]!
    /// 出现在「换成单一模型」对照和换模型建议里的模型
    static let counterfactualModels: [String] = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"]
}
