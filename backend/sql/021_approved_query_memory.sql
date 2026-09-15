-- approved 查询学习记忆的存储契约（计划 2026-09-14-approved-query-memory.md Task 1）。
-- 必须在 009_query_provenance.sql 之后执行（样例的 source_run_id 引用 bi.query_runs，
-- 审核复核还要读 provenance 做血缘核对）。
--
-- 四条写死的规矩：
--
--  1) 幂等。角色与表用 pg_roles 探测 / IF NOT EXISTS，视图 DROP IF EXISTS 后重建，
--     本机 `*_test` 库要求重放两次通过；表有状态（审批记录是长期记忆），所以表
--     不做 DROP + 重建——幂等靠形状稳定，不靠清库。
--  2) bi_app 无底表权限。草稿与事件只能经 bi_approver（独立审核 DSN，最小权限）
--     写入；聊天检索只读 reporting 投影视图里的 status='approved' 行。底表对
--     bi_app 的 SELECT/INSERT 拒绝与「投影只暴露 approved 行」由 tests.test_db
--     的 ApprovedQueryMemoryMigrationTests 逐一钉住。
--  3) 数据库侧也拒绝一次性业务值。normalized_request 不许出现 start/end/date/
--     shop_id/subject_id/target_price/threshold/budget/sql_text/rows/result/prompt
--     键——与应用侧 bi_agent.query_memory.models.FORBIDDEN_VALUE_KEYS 同一词表，
--     由 tests.test_db 逐键用真实插入探测，两边单边改都会在测试现场变红。
--  4) 事件只追加。approved_query_events 没有 UPDATE/DELETE 授权：审批历史不可改写，
--     撤销与替换是样例表上的状态迁移（UPDATE status/revision），不是改历史。
--     因此 bi_approver 对事件表只有 SELECT + INSERT（计划架构行的「不可变事件」，
--     也是最小权限：用不到的授权不给）。

-- ---------------------------------------------------------------------------
-- 1) 审核身份：NOLOGIN，不发给任何人当登录身份；应用侧另有独立审核 DSN 的会话
--    成员关系（001 的角色探测写法）。
-- ---------------------------------------------------------------------------
DO $mig021$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bi_approver') THEN
    CREATE ROLE bi_approver NOLOGIN;
  END IF;
END
$mig021$;

-- ---------------------------------------------------------------------------
-- 2) 样例表：人工审核的唯一长期记忆。schema_version 沿用 018 的「迁移号入列」
--    记录方式；生命周期与 revision 的合法性由应用侧状态机（Task 2）负责。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.approved_query_examples (
  example_ref          text PRIMARY KEY,
  source_run_id        uuid NOT NULL REFERENCES bi.query_runs(id),
  owner_subject_id     text NOT NULL,
  domain               text NOT NULL,
  intent_signature     text NOT NULL,
  question_template    text NOT NULL,
  slots                jsonb NOT NULL,
  normalized_request   jsonb NOT NULL,
  expected_tool        text NOT NULL,
  version_requirements jsonb NOT NULL,
  authorization_refs   text[] NOT NULL,
  status               text NOT NULL DEFAULT 'draft',
  approval_revision    integer NOT NULL DEFAULT 0,
  created_by           text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  schema_version       text NOT NULL DEFAULT '021',
  CONSTRAINT approved_query_status CHECK (
    status IN ('draft', 'approved', 'superseded', 'revoked')),
  CONSTRAINT approved_query_revision CHECK (approval_revision >= 0),
  -- 授权域为空 = 谁都不授权 = 永远检索不到，与静默失效无异，所以直接拒收。
  CONSTRAINT approved_query_refs CHECK (cardinality(authorization_refs) > 0),
  CONSTRAINT approved_query_no_bound_values CHECK (
    NOT (normalized_request ?| ARRAY['start', 'end', 'date', 'shop_id',
      'subject_id', 'target_price', 'threshold', 'budget', 'sql_text',
      'rows', 'result', 'prompt'])),
  CONSTRAINT approved_query_schema_version CHECK (schema_version ~ '^[0-9]{3}$')
);

-- ---------------------------------------------------------------------------
-- 3) 审批事件：actor、时间、理由、revision 与 replacement 全部留痕；只追加。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.approved_query_events (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  example_ref      text NOT NULL REFERENCES bi.approved_query_examples(example_ref),
  revision         integer NOT NULL,
  actor_subject_id text NOT NULL,
  event_kind       text NOT NULL,
  reason           text NOT NULL,
  replacement_ref  text REFERENCES bi.approved_query_examples(example_ref),
  created_at       timestamptz NOT NULL DEFAULT now(),
  schema_version   text NOT NULL DEFAULT '021',
  UNIQUE (example_ref, revision),
  CONSTRAINT approved_query_event_kind CHECK (
    event_kind IN ('drafted', 'approved', 'superseded', 'revoked')),
  CONSTRAINT approved_query_event_reason CHECK (btrim(reason) <> ''),
  CONSTRAINT approved_query_event_schema_version CHECK (schema_version ~ '^[0-9]{3}$')
);

CREATE INDEX IF NOT EXISTS approved_query_lookup_idx
  ON bi.approved_query_examples (status, domain, intent_signature);

COMMENT ON TABLE bi.approved_query_examples IS
  '人工审核后的查询样例：只有 status=approved 且版本精确匹配的行可被检索；'
  '一次性业务值只能是槽位，数据库侧与模型侧各挡一次绑定业务值。';
COMMENT ON TABLE bi.approved_query_events IS
  '审批事件，只追加：操作者、理由、revision 与替换目标全部留痕，任何身份都没有'
  '改写或删除既有事件的授权。';

-- ---------------------------------------------------------------------------
-- 4) 权限：bi_app 无底表权限（沿用 019 的逐表收权写法）；bi_approver 最小权限——
--    样例表读写、事件只追加、来源运行与血缘只读、事件序号序列可用。
-- ---------------------------------------------------------------------------
REVOKE ALL ON bi.approved_query_examples, bi.approved_query_events FROM PUBLIC;
REVOKE ALL ON bi.approved_query_examples, bi.approved_query_events FROM bi_app;
GRANT SELECT, INSERT, UPDATE ON bi.approved_query_examples TO bi_approver;
GRANT SELECT, INSERT ON bi.approved_query_events TO bi_approver;
GRANT SELECT ON bi.query_runs, bi.query_provenance TO bi_approver;
GRANT USAGE, SELECT ON SEQUENCE bi.approved_query_events_id_seq TO bi_approver;
GRANT USAGE ON SCHEMA bi TO bi_approver;

-- ---------------------------------------------------------------------------
-- 5) 检索投影：只有 approved 行进 reporting；来源运行、状态与创建者不出投影。
--    视图属主是执行迁移的管理员，bi_app 只需 SELECT（与既有 reporting 视图同机制）。
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS reporting.v_approved_query_examples;
CREATE VIEW reporting.v_approved_query_examples AS
SELECT example_ref, owner_subject_id, domain, intent_signature, question_template,
       slots, normalized_request, expected_tool, version_requirements,
       authorization_refs, approval_revision
FROM bi.approved_query_examples
WHERE status = 'approved';

REVOKE ALL ON reporting.v_approved_query_examples FROM PUBLIC;
GRANT SELECT ON reporting.v_approved_query_examples TO bi_app;
