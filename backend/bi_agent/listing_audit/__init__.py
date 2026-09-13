"""上架复核域（ListingPriceAuditGraph）：用户指定目标价 vs 渠道在售标价。

分层与 `commerce/` 同一套理由：`rules.py` 是纯算术与词表（不碰数据库、不碰门禁），
`repository.py` 是唯一读库的地方（只读获准 reporting 视图），`graph.py` 是 spec §6
的固定节点链与全部降级门禁，`tool.py` 是模型可见面的唯一出口，`models.py` 是输入
契约与图内数据结构。

本包**不在这里重导出符号**：`runtime.models` 要引用 `listing_audit.rules` 的词表，
而包级重导出会把这条引用变成循环导入。请从具体模块导入，例如
`from bi_agent.listing_audit.tool import audit_listing_prices`。

两条本域特有的红线：

1. **来源门禁默认关闭**。交付代码里没有任何已核验的渠道在售价来源，所以真实部署
   只能报 `unsupported`（见 `rules.py` 模块说明与 `tests/test_listing_audit.py`）。
2. **目标价只来自本轮**。`expected_prices` 是必填输入，图上没有任何一条读取
   历史标准的路径。
"""
