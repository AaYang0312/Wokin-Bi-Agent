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

export type Artifact = Record<string, unknown>

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
