"""商品运营域（CommercePerformanceGraph）：现有表项的经营指标与商品运营图。

分层的唯一理由是"谁能碰数字"：`metrics.py` 是纯算术（不碰数据库、不碰门禁），
`repository.py` 是唯一读库的地方（只读获准 reporting 视图），`graph.py` 是 spec §6
的固定节点链与全部降级门禁，`tool.py` 是模型可见面的唯一出口，`models.py` 是输入
契约与服务端上下文。

本包**不在这里重导出符号**：`runtime.models` 要引用 `commerce.metrics` 的词表，
而 `commerce.graph` 要引用 `business_query.graph` 的收尾助手；包级重导出会把
`import bi_agent.agent` 变成一条循环导入。请从具体模块导入，例如
`from bi_agent.commerce.tool import analyze_product_performance`。

口径与验收来源：`docs/superpowers/plans/2026-09-11-data-and-query-closure.md`
Task 7、`docs/superpowers/specs/2026-09-11-operator-workflows-design.md` §3–§6。
"""
