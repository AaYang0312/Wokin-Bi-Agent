export type ChatSummary = {
  id: string
  title: string
  created_at: string
  updated_at: string
}

/**
 * 授权展示实体：引用由后端稳定派生，展示名只在授权投影里出现。
 * display_name 为 null 表示名称未取得——前端显示占位，不猜名字。
 */
export type DisplayEntity = {
  ref: string
  kind: 'shop' | 'product' | 'sku'
  display_name?: string | null
  sku_label?: string | null
  name_source: 'archive' | 'trade_snapshot' | 'shop_profile' | 'unresolved'
  /**
   * 店铺属于哪个平台的短码（后端校验后才会到这里）。
   * 模型载荷不含平台，所以跨平台判断只能在展示层做。
   */
  platform?: string | null
}

/**
 * 一条已落库 Artifact 的展示载荷。
 *
 * 除了投影本身的字段，展示层还会补上它自己的 `artifact_id` 与 `artifact_type`
 * （见后端 `business_query.tool.artifact_event_payload`）：`chart_spec` 只带
 * `dataset_ref`，渲染侧要按引用找回被引用的那一份数据集。这两个字段不在这里写成
 * 必填类型：载荷是外部输入，组件按 `typeof` 逐项验，不拿类型声明当信任凭据。
 */
export type Artifact = Record<string, unknown>

/**
 * 图表下钻意图：只携带“该重新问什么”，不携带任何业务数字。
 *
 * 为什么不给组件一份现成的店铺列表：平台柱上的一个点击如果直接回看上一轮结果，
 * 就等于拿客户端缓存当授权结论。下钻必须变成一次新的 `compare_performance`，
 * 由服务端按本次范围重新展开获准店铺；窗口与口径在这里原样带上，是为了不让
 * “点一下就把七天变成三十天”这种静默改口径发生。
 */
export type DrilldownIntent = {
  /** 已登记平台码（后端 `sources.platform_codes` 才会到这里）。 */
  platform: string
  groupBy: 'shop'
  /** [start, end) 北京时间，与本轮图表所引用数据集一致。 */
  start: string
  end: string
  /** 图上那一列的指标名（与后端已登记指标同名，不自带中文同义词）。 */
  metric: string
  /** `指标|口径|时间归属` 形状的口径签名，原样来自 chart_spec。 */
  metricBasis: string
  /** 交给外层的提问文本：口径写在明面上，不依赖上一轮聊天理解。 */
  question: string
}

/**
 * 一个 (店铺, 指标) 的口径凭证。后端在 results 与 Artifact 里都带这份证据：
 * 同名指标可能来自不同通道，缺了口径就容易被拿去汇总或排名。
 * 只出现不透明 shop_ref，不出现 ERP 主键与接口方法名。
 */
export type BasisEntry = {
  shop_ref?: string
  metric: string
  basis: string
  time_basis: string
  metric_version?: string
}

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  artifacts: Artifact[]
  status: 'complete' | 'error'
  created_at: string
}

export type ChatEvent =
  | { event: 'status'; data: { stage: 'thinking' | 'querying' | 'answering' } }
  | { event: 'artifact'; data: Artifact }
  | { event: 'message'; data: ChatMessage }
  | { event: 'error'; data: { code: string; message: string } }
  | { event: 'done'; data: { status: 'complete' | 'error' } }
