// 由 research/sync_coefficients.py 从 research/coefficients.json 生成，不要手改。
// 改系数：编辑 JSON，再运行 python3 research/sync_coefficients.py。

extension Budget {
    /// 成本系数 v4（2026-09-15），来龙去脉见 Budget.swift 顶部注释
    static let coef: [String: Coef] = [
        "gpt-5.6-sol":  Coef(fresh: 42_500, cached: 492_537, output: 13_572, request: 0.0328),
        "gpt-5.5":      Coef(fresh: 49_419, cached: 572_717, output: 15_782, request: 0.0282),
        "gpt-5.6-terra":Coef(fresh: 47_223, cached: 547_263, output: 15_080, request: 0.0295),
        "gpt-5.6-luna": Coef(fresh: nil, cached: nil, output: nil, request: 0.0),
        "gpt-6-astra":  Coef(fresh: 35_094, cached: 250_428, output: 2_318, request: 0.5632),
    ]
    /// 没有系数的模型按它估算
    static let fallback = coef["gpt-5.6-sol"]!
    /// 出现在「换成单一模型」对照和换模型建议里的模型
    static let counterfactualModels: [String] = ["gpt-5.6-sol", "gpt-5.5", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"]
}
