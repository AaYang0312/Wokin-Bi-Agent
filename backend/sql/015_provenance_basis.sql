-- 口径与来源版本进入请求指纹（原路线图 Task 5.4d）。
-- 必须在 009_query_provenance.sql 之后执行。
--
-- 指纹必须包含“这次到底用了哪条来源、哪个口径、按什么时间归属、当时生效的是哪版
-- 质量规则”。缺了它们，同一条查询在换来源之后会命中旧结果，把口径变化读成经营变化。

ALTER TABLE bi.query_provenance
  ADD COLUMN IF NOT EXISTS source_registry_version text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS basis_signature        text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS quality_rule           text;

COMMENT ON COLUMN bi.query_provenance.source_registry_version IS
  '来源注册表（bi_agent/sources.py）版本号；登记新来源或改通道口径时递增，旧结果不得复用。';
COMMENT ON COLUMN bi.query_provenance.basis_signature IS
  '实际用到的口径签名，形如 指标|口径|时间归属；只含标签，不含 ERP 主键与接口方法名。';
COMMENT ON COLUMN bi.query_provenance.quality_rule IS
  '当时生效的对账口径版本（bi_agent.data_quality.QUALITY_RULE）；规则升级后能力可能已被回收。';
