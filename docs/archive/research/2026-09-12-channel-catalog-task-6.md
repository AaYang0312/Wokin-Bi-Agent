# Task 6 交付记录：跨渠道商品 / SKU 映射与名称解析

日期：2026-09-12，北京时间。分支 `feat/channel-catalog`。范围：运营工作流计划
[Task 6](../plans/2026-09-11-data-and-query-closure.md)（P0，接 Task 2）。

## 交付

| 位置 | 内容 |
| --- | --- |
| `backend/sql/016_channel_catalog.sql` | `bi.channel_items`：身份 = `(namespace, shop_id, listing_id, platform_sku_id, valid_from)`，带 `status(either approved/ambiguous/unresolved)`、`source`、`evidence`、`mapping_version` 与有效期；CHECK 强制「证据非空」「approved 必须有 ERP 商品」「`listing_id` 为空只允许 `trade_line`」；`reporting.v_channel_items` 给 bi_app/bi_reader 读 |
| `catalog/channel_mapping.py` | `record_identifier_mapping`（缺证据 / 缺身份直接拒绝）、`record_trade_line_mapping`、`channel_items_for`（按授权店铺 + 有效期时点读取） |
| `catalog/resolver.py` | `resolve_product(ref|text)` → `resolved/ambiguous/unresolved` + 候选卡片；`expand_channel_items` 从映射表取渠道落点 |

编号说明：计划原为 Task 6 预留 010，但主线已按时间顺序应用到 015，保留 010 会让
「编号 = 应用顺序」失效，故顺延 016。

## 三条红线怎么落地的

1. **合并只认显式标识映射。** 文本解析只产候选，永远不产 `approved`；
   `test_platform_name_alone_never_marks_a_mapping_approved` 钉住。
2. **跨账号同号不归并。** `namespace` 进主键与查询条件，
   `test_same_platform_number_in_another_account_is_not_merged` 钉住。
3. **上架全集不从历史成交推导。** `expand_channel_items` 只读映射表，
   无成交的已映射 listing 照样返回（`test_mapped_listing_without_any_trade_is_kept`）。

另外两条实现约束值得记下来：

- 候选携带 `shop_ids`（授权范围内出现过该商品的店铺集合），不是"先取一个店"。
  上一版这里塞了 `shops[0]`，越权检查看着通过其实是空转；现在同一用例既断言
  越权店不出现，也断言放开授权范围后它**会**出现，才证明过滤真的在起作用。
- 名称与规格的取用规则不在解析层重写：档案名 > 成交快照 > 未取得仍由
  `pick_display_name` / `pick_sku_label` 单点决定；SQL 只负责"任一命中"的匹配。
  （`strpos` 而非 `LIKE`：`LIKE` 的通配字面量会被 psycopg 当成占位符前缀。）

## 未做与原因

- **`bi.skus` SKU 主档未建**：Task 2 就留了这条（"未完成：SKU 主档"），它需要
  已核验来源填充。没有来源时凭空建一张没人写的表，只会让"已接入"看起来比实际更早。
  规格文本目前继续取自成交行快照。
- **渠道上架链接的真实来源未接入**：`listing_id` 只能由 `manual_map` / `channel_api`
  / `import` 写入，本轮没有任何 `channel_api` 生产者。因此**上架复核（Task 9）仍受
  "缺来源"门禁约束**，不能因为映射表存在就宣称全店复核可用。
- 尚未接入 `analyze_product_performance`（Task 7）的调用链：解析器现在是独立可测单元。

## 验证

```sh
cd backend
uv run --no-sync --env-file ../.env.test python -m unittest discover -s tests -t .   # 486 项 OK
uv run --no-sync --env-file ../.env.test python -m tests.acceptance --offline        # 20/20
```

新增 `tests/test_channel_mapping.py` 13 项（真实测试库、外层事务回滚、只写合成店铺）。

**红灯质量要打个折扣记清楚**：测试先写、实现后写，但实现是紧接着一次性写完的，
所以中途那几次转红（列名 `title` 写错、psycopg 占位符、GROUP BY 形状）证明的是
**夹具与 SQL 写错了**，不是"功能缺失被断言抓住"。真正算数的行为反证只有两条：
越权用例的反向断言（放开范围就该出现）与 `expand_channel_items` 的无成交 listing 保留。
下一-个任务如果要拿这份用例当质量证据，只能拿这两条。
