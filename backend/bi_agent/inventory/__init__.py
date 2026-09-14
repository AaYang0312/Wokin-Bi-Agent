"""库存预警域（InventoryWatchGraph）：实物总量与店铺渠道可售的两级预警。

分层与 `commerce/`、`listing_audit/` 同一套理由：`rules.py` 是纯算术与词表（不碰
数据库、不碰门禁），`repository.py` 是唯一读库的地方（只读获准 reporting 视图），
`graph.py` 是 spec §6 的固定节点链与全部降级门禁，`tool.py` 是模型可见面的唯一出口，
`models.py` 是输入契约与图内数据结构。

本包**不在这里重导出符号**：`runtime.models` 要引用 `inventory.rules` 的词表，而包级
重导出会把这条引用变成循环导入。请从具体模块导入，例如
`from bi_agent.inventory.tool import inspect_inventory`。

两条本域特有的红线：

1. **两个口径永不相加**。实物按 (池, 仓库, SKU, 批次, 单位) 去重后各算一次；渠道可售
   单独一格一行，共享库存绝不按店重复累计（spec §5.5）。
2. **来源门禁默认关闭**。交付代码里没有任何已核验的库存来源，所以真实部署只能报
   `unsupported`（见 `rules.py` 模块说明与 `tests/test_inventory.py`）。
"""
