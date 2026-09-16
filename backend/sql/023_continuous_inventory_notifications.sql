-- 持续库存监控的存储契约（计划 2026-09-14-continuous-inventory-notifications.md
-- Task 2）。必须在 009（query_runs/artifacts 与 inventory_alerts 类型）、019
-- （reporting 库存视图）、021（角色探测写法）之后执行。
--
-- 六条写死的规矩：
--
--  1) 幂等。角色用 duplicate_object 探测，表用 IF NOT EXISTS，函数与视图
--     CREATE OR REPLACE / DROP IF EXISTS 后重建；本机 `*_test` 库要求重放两次
--     通过。固定 service chat/message 用 ON CONFLICT DO NOTHING 播种。
--  2) bi_monitor 是 NOLOGIN 最小权限身份：只有获准 reporting 库存视图与目录
--     展示面（v_shops / v_catalog_version，二者不含库存数量、渠道快照或扫描
--     证据）的 SELECT、监控策略 SELECT、commit/deliver 两函数 EXECUTE。告警、
--     事件、outbox、通知、runtime、聊天与来源底表没有任何直接读写。
--  3) bi_app 只读 owner 过滤的通知/告警 API 投影，只可执行 read/ack 两函数；
--     owner 复核在函数内部重做，底表权限从未给出。
--  4) 通知载荷安全由不可变递归 validator 承担：payload 出现
--     shop_id/pool_id/warehouse_id/erp_sku_id/subject_id/dsn/evidence/scan_evidence
--     任一键（任意深度）即拒绝；outbox 与应用内通知的 CHECK 都走它。
--  5) commit_inventory_monitor_scan 是唯一的写事务面：先校验来源载荷与闭集
--     decision 形状（含 observed 布尔），再在 run 级与 per-key advisory xact
--     锁内创建 service run → 不可变来源 artifact → 迁移告警状态 → 写事件 →
--     只为 triggered/retriggered/resolved 写 outbox。任一步失败整批回滚。
--  6) 函数失败只报固定错误码（monitor_* / inventory_* 前缀），不回显参数、
--     DSN 或数据库错误原文；每个 SECURITY DEFINER 函数固定 search_path 并
--     REVOKE PUBLIC EXECUTE。

-- ---------------------------------------------------------------------------
-- 1) 监控身份：NOLOGIN，不发给任何人当登录身份；应用侧另有独立监控 DSN。
-- ---------------------------------------------------------------------------
DO $m023$
BEGIN
  CREATE ROLE bi_monitor NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$m023$;

-- ---------------------------------------------------------------------------
-- 2) 固定 service chat/message：monitor 的 query run 挂在这条服务会话下，
--    永远不创建或写入用户聊天。
-- ---------------------------------------------------------------------------
INSERT INTO bi.app_chats (id, subject_id, title, title_source)
VALUES ('00000000-0000-4000-8000-000000000023', 'inventory-monitor-service',
        '库存监控服务运行', 'auto')
ON CONFLICT (id) DO NOTHING;
INSERT INTO bi.app_messages (id, chat_id, role, content, status)
VALUES ('00000000-0000-4000-8000-000000000024',
        '00000000-0000-4000-8000-000000000023', 'user',
        'inventory-monitor/scheduled-run', 'complete')
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3) 通知载荷安全：不可变递归 validator，CHECK 与函数共用同一份判定。
--    拒绝真实主键/主体/凭据键在任意深度出现；标量与 null 恒安全。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION bi.inventory_notification_payload_safe(payload jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog
AS $m023$
DECLARE
  forbidden constant text[] := ARRAY['shop_id', 'pool_id', 'warehouse_id',
    'erp_sku_id', 'subject_id', 'dsn', 'evidence', 'scan_evidence'];
  child jsonb;
BEGIN
  IF payload IS NULL THEN
    RETURN true;
  END IF;
  CASE jsonb_typeof(payload)
    WHEN 'object' THEN
      IF payload ?| forbidden THEN
        RETURN false;
      END IF;
      FOR child IN SELECT each.value FROM jsonb_each(payload) AS each LOOP
        IF NOT bi.inventory_notification_payload_safe(child) THEN
          RETURN false;
        END IF;
      END LOOP;
      RETURN true;
    WHEN 'array' THEN
      FOR child IN SELECT element.value
                     FROM jsonb_array_elements(payload) AS element LOOP
        IF NOT bi.inventory_notification_payload_safe(child) THEN
          RETURN false;
        END IF;
      END LOOP;
      RETURN true;
    ELSE
      RETURN true;
  END CASE;
END
$m023$;

-- ---------------------------------------------------------------------------
-- 4) 监控表：策略、告警、事件、outbox、应用内通知。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.inventory_monitor_policies (
  policy_ref text PRIMARY KEY,
  threshold_policy_ref text NOT NULL,
  owner_subject_id text NOT NULL,
  shop_refs text[] NOT NULL,
  inventory_pool_refs text[] NOT NULL,
  levels text[] NOT NULL,
  cooldown_seconds integer NOT NULL CHECK (cooldown_seconds BETWEEN 3600 AND 604800),
  enabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (levels <@ ARRAY['physical_total','shop_sellable']::text[]),
  CHECK (cardinality(levels) > 0)
);

CREATE TABLE IF NOT EXISTS bi.inventory_alert_instances (
  alert_ref text PRIMARY KEY,
  dedupe_key text NOT NULL,
  generation integer NOT NULL CHECK (generation >= 1),
  policy_ref text NOT NULL REFERENCES bi.inventory_monitor_policies(policy_ref),
  rule_code text NOT NULL,
  level text NOT NULL CHECK (level IN ('physical_total','shop_sellable')),
  sku_ref text NOT NULL,
  scope_ref text NOT NULL,
  status text NOT NULL CHECK (status IN ('open','acknowledged','resolved','suppressed')),
  source_artifact_id uuid NOT NULL REFERENCES bi.query_artifacts(id),
  opened_at timestamptz NOT NULL,
  last_observed_at timestamptz NOT NULL,
  last_notified_at timestamptz,
  resolved_at timestamptz,
  acknowledged_by text,
  acknowledged_at timestamptz,
  UNIQUE (dedupe_key, generation)
);

-- 同一格最多一条活跃告警：并发首发由唯一部分索引兜底（advisory lock 之外
-- 的最后一道闸）。
CREATE UNIQUE INDEX IF NOT EXISTS inventory_one_active_alert_idx
  ON bi.inventory_alert_instances(dedupe_key)
  WHERE status IN ('open','acknowledged');

CREATE TABLE IF NOT EXISTS bi.inventory_alert_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alert_ref text NOT NULL REFERENCES bi.inventory_alert_instances(alert_ref),
  event_kind text NOT NULL CHECK (event_kind IN ('triggered','retriggered','updated','resolved')),
  previous_status text,
  next_status text NOT NULL,
  source_artifact_id uuid NOT NULL REFERENCES bi.query_artifacts(id),
  idempotency_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bi.notification_outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  topic text NOT NULL CHECK (topic = 'inventory_alert'),
  owner_subject_id text NOT NULL,
  alert_ref text NOT NULL REFERENCES bi.inventory_alert_instances(alert_ref),
  event_id bigint NOT NULL REFERENCES bi.inventory_alert_events(id),
  idempotency_key text NOT NULL UNIQUE,
  payload jsonb NOT NULL CHECK (bi.inventory_notification_payload_safe(payload)),
  available_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 20)
);

CREATE TABLE IF NOT EXISTS bi.in_app_notifications (
  notification_ref text PRIMARY KEY,
  owner_subject_id text NOT NULL,
  alert_ref text NOT NULL REFERENCES bi.inventory_alert_instances(alert_ref),
  event_kind text NOT NULL,
  payload jsonb NOT NULL CHECK (bi.inventory_notification_payload_safe(payload)),
  idempotency_key text NOT NULL UNIQUE,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE bi.inventory_alert_instances IS
  '库存告警的唯一去重状态：dedupe_key 只由策略版本/层级/SKU ref/作用域 ref/规则码'
  '派生，generation 从 1 起只增不减；同键同时最多一条 open/acknowledged。';
COMMENT ON TABLE bi.notification_outbox IS
  '首版唯一通知渠道的 outbox：at-least-once 投递、稳定幂等键；payload 经不可变'
  '递归 validator 拒绝真实主键/主体/凭据键。';
COMMENT ON COLUMN bi.inventory_alert_events.idempotency_key IS
  'sha256(dedupe_key:generation:event_kind:source_fingerprint)：同一次扫描重放'
  '不会产生第二条事件或通知。';

-- ---------------------------------------------------------------------------
-- 5) 提交函数：monitor 唯一的写事务面（run → artifact → 告警/事件/outbox）。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION bi.commit_inventory_monitor_scan(
  policy_ref         text,
  source_payload     jsonb,
  source_fingerprint text,
  data_as_of         timestamptz,
  decisions          jsonb,
  observed_at        timestamptz
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, bi
AS $m023$
DECLARE
  forbidden constant text[] := ARRAY['shop_id', 'pool_id', 'warehouse_id',
    'erp_sku_id', 'subject_id', 'dsn', 'evidence', 'scan_evidence'];
  decision_keys constant text[] := ARRAY['dedupe_key', 'event_kind', 'generation',
    'level', 'next_status', 'notify', 'observed', 'previous_status', 'quantity',
    'rule_code', 'scope_ref', 'sku_ref', 'threshold', 'unit'];
  statuses constant text[] := ARRAY['open', 'acknowledged', 'resolved', 'suppressed'];
  kinds constant text[] := ARRAY['triggered', 'retriggered', 'updated', 'resolved'];
  policy_row bi.inventory_monitor_policies%ROWTYPE;
  service_subject text;
  run_id uuid;
  artifact_id uuid;
  attempt_no integer;
  decision jsonb;
  decision_keys_present text[];
  d_key text;
  d_generation integer;
  d_previous text;
  d_next text;
  d_kind text;
  d_notify boolean;
  d_observed boolean;
  d_rule text;
  d_level text;
  d_sku text;
  d_scope text;
  d_quantity text;
  d_threshold text;
  d_unit text;
  seen_keys text[] := ARRAY[]::text[];
  active_row bi.inventory_alert_instances%ROWTYPE;
  history_generation integer;
  target_alert text;
  event_key text;
  transitions jsonb := '[]'::jsonb;
BEGIN
  -- 形状与安全先于任何写入：指纹/载荷/决策全部定型后才碰表。
  IF policy_ref IS NULL OR source_payload IS NULL OR decisions IS NULL
     OR source_fingerprint IS NULL OR observed_at IS NULL OR data_as_of IS NULL
     OR source_fingerprint !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'monitor_source_payload_unsafe';
  END IF;
  IF NOT bi.inventory_notification_payload_safe(source_payload)
     OR NOT bi.inventory_notification_payload_safe(decisions) THEN
    RAISE EXCEPTION 'monitor_source_payload_unsafe';
  END IF;
  IF jsonb_typeof(decisions) <> 'array' THEN
    RAISE EXCEPTION 'monitor_invalid_decision';
  END IF;
  IF (SELECT count(*) FROM jsonb_array_elements(decisions)) > 500 THEN
    RAISE EXCEPTION 'monitor_decision_budget_exceeded';
  END IF;

  SELECT * INTO policy_row
    FROM bi.inventory_monitor_policies AS p
   WHERE p.policy_ref = commit_inventory_monitor_scan.policy_ref;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'monitor_policy_not_found';
  END IF;
  IF NOT policy_row.enabled THEN
    RAISE EXCEPTION 'monitor_policy_disabled';
  END IF;

  SELECT c.subject_id INTO service_subject
    FROM bi.app_chats c
   WHERE c.id = '00000000-0000-4000-8000-000000000023'
     AND c.subject_id = 'inventory-monitor-service';
  IF service_subject IS NULL OR NOT EXISTS (
       SELECT 1 FROM bi.app_messages
        WHERE id = '00000000-0000-4000-8000-000000000024'
          AND chat_id = '00000000-0000-4000-8000-000000000023'
          AND role = 'user') THEN
    RAISE EXCEPTION 'monitor_service_chat_missing';
  END IF;

  -- 闭集 decision 形状：键集精确相等、枚举/布尔/形状逐项核、同键只出现一次、
  -- observed=false 只属于 updated（triggered/retriggered/resolved 都代表本轮
  -- 真看到了那一格）。
  FOR decision IN SELECT * FROM jsonb_array_elements(decisions) LOOP
    IF jsonb_typeof(decision) <> 'object' THEN
      RAISE EXCEPTION 'monitor_invalid_decision';
    END IF;
    SELECT array_agg(k ORDER BY k) INTO decision_keys_present
      FROM jsonb_object_keys(decision) AS k;
    IF decision_keys_present <> decision_keys THEN
      RAISE EXCEPTION 'monitor_invalid_decision';
    END IF;
    IF decision ?| forbidden THEN
      RAISE EXCEPTION 'monitor_invalid_decision';
    END IF;
    d_key := decision->>'dedupe_key';
    d_generation := (decision->>'generation')::integer;
    d_previous := decision->>'previous_status';
    d_next := decision->>'next_status';
    d_kind := decision->>'event_kind';
    d_rule := decision->>'rule_code';
    d_level := decision->>'level';
    d_sku := decision->>'sku_ref';
    d_scope := decision->>'scope_ref';
    d_quantity := decision->>'quantity';
    d_threshold := decision->>'threshold';
    d_unit := decision->>'unit';
    IF d_key IS NULL OR d_key !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(decision->'generation') <> 'number'
       OR d_generation IS NULL OR d_generation < 1
       OR d_previous IS NOT NULL AND NOT d_previous = ANY(statuses)
       OR d_next IS NULL OR NOT d_next = ANY(statuses)
       OR NOT d_kind = ANY(kinds)
       OR jsonb_typeof(decision->'notify') <> 'boolean'
       OR jsonb_typeof(decision->'observed') <> 'boolean'
       OR d_rule !~ '^[a-z][a-z0-9_-]{0,31}$'
       OR NOT d_level = ANY(ARRAY['physical_total', 'shop_sellable'])
       OR d_sku !~ '^ent-[0-9a-z]{8}$'
       OR d_scope IS NULL OR btrim(d_scope) = ''
       OR (d_quantity IS NOT NULL
           AND d_quantity !~ '^-?(0|[1-9][0-9]*)(\.[0-9]+)?$')
       OR (d_threshold IS NOT NULL
           AND d_threshold !~ '^-?(0|[1-9][0-9]*)(\.[0-9]+)?$')
       OR (d_unit IS NOT NULL
           AND d_unit <> ALL(ARRAY['piece', 'box', 'set', 'kit'])) THEN
      RAISE EXCEPTION 'monitor_invalid_decision';
    END IF;
    d_notify := (decision->>'notify')::boolean;
    d_observed := (decision->>'observed')::boolean;
    IF d_key = ANY(seen_keys)
       OR (d_kind = 'triggered'
           AND (d_previous IS NOT NULL OR d_next <> 'open' OR NOT d_observed))
       OR (d_kind = 'retriggered'
           AND (d_previous IS NULL OR d_next <> d_previous OR NOT d_observed))
       OR (d_kind = 'updated'
           AND (d_previous IS NULL OR d_notify
                OR NOT (d_next = d_previous
                        OR (d_next = 'suppressed' AND d_previous = ANY(statuses)))))
       OR (d_kind = 'resolved'
           AND (d_previous NOT IN ('open', 'acknowledged')
                OR d_next <> 'resolved' OR NOT d_observed)) THEN
      RAISE EXCEPTION 'monitor_invalid_decision';
    END IF;
    seen_keys := seen_keys || d_key;
  END LOOP;

  -- run 级 advisory xact lock：attempt_no 只在锁内递增，两个 policy/进程不会
  -- 撞 query_runs 的 (user_message_id, domain, attempt_no) 唯一约束。
  PERFORM pg_advisory_xact_lock(
    hashtextextended('bi.commit_inventory_monitor_scan/run', 0));
  SELECT COALESCE(max(r.attempt_no), 0) + 1 INTO attempt_no
    FROM bi.query_runs AS r
   WHERE r.user_message_id = '00000000-0000-4000-8000-000000000024'
     AND r.domain = 'inventory_watch';

  BEGIN
    run_id := gen_random_uuid();
    INSERT INTO bi.query_runs (
        id, chat_id, user_message_id, subject_id, tool_call_id, domain,
        attempt_no, normalized_request, state, root_request_id,
        request_fingerprint, recovery_count, status, current_node, revision,
        started_at, updated_at, completed_at)
    VALUES (run_id,
            '00000000-0000-4000-8000-000000000023',
            '00000000-0000-4000-8000-000000000024',
            service_subject,
            'monitor-' || commit_inventory_monitor_scan.policy_ref,
            'inventory_watch', attempt_no,
            jsonb_build_object('policy_ref',
                commit_inventory_monitor_scan.policy_ref,
                'levels', to_jsonb(policy_row.levels)),
            '{}', run_id, source_fingerprint, 0, 'succeeded', 'finalize', 1,
            observed_at, observed_at, observed_at)
    RETURNING id INTO run_id;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'monitor_run_write_failed';
  END;

  BEGIN
    artifact_id := gen_random_uuid();
    INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload, data_as_of)
    VALUES (artifact_id, run_id, 'inventory_alerts', source_payload, data_as_of)
    RETURNING id INTO artifact_id;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'monitor_artifact_write_failed';
  END;

  -- 逐格迁移：per-key advisory xact lock 固定全局顺序（按 dedupe_key 排序）
  -- 消除死锁；事件幂等键让崩溃重放只算一次。
  FOR decision IN
    SELECT e.value FROM jsonb_array_elements(decisions) e(value)
   ORDER BY e.value->>'dedupe_key'
  LOOP
    d_key := decision->>'dedupe_key';
    d_generation := (decision->>'generation')::integer;
    d_previous := decision->>'previous_status';
    d_next := decision->>'next_status';
    d_kind := decision->>'event_kind';
    d_rule := decision->>'rule_code';
    d_level := decision->>'level';
    d_sku := decision->>'sku_ref';
    d_scope := decision->>'scope_ref';
    d_quantity := decision->>'quantity';
    d_threshold := decision->>'threshold';
    d_unit := decision->>'unit';
    d_notify := (decision->>'notify')::boolean;
    d_observed := (decision->>'observed')::boolean;
    event_key := encode(sha256(convert_to(d_key || ':' || d_generation || ':'
                                          || d_kind || ':' || source_fingerprint,
                                          'UTF8')), 'hex');
    BEGIN
      PERFORM pg_advisory_xact_lock(hashtextextended(d_key, 0));
    EXCEPTION WHEN query_canceled THEN
      RAISE EXCEPTION 'monitor_lock_unavailable';
    END;
    BEGIN
      SELECT * INTO active_row
        FROM bi.inventory_alert_instances
       WHERE dedupe_key = d_key
         AND status IN ('open', 'acknowledged')
       ORDER BY alert_ref
       FOR UPDATE;
      SELECT COALESCE(max(h.generation), 0) INTO history_generation
        FROM bi.inventory_alert_instances AS h
       WHERE h.dedupe_key = d_key;
      IF EXISTS (SELECT 1 FROM bi.inventory_alert_events
                  WHERE idempotency_key = event_key) THEN
        -- 崩溃重放的同一批决策：事件与首发都已落库，幂等跳过。
        CONTINUE;
      END IF;
      IF active_row.alert_ref IS NOT NULL THEN
        IF active_row.status <> d_previous OR d_generation <> active_row.generation
           OR d_next NOT IN (active_row.status, 'resolved', 'suppressed') THEN
          RAISE EXCEPTION 'monitor_invalid_transition';
        END IF;
        target_alert := active_row.alert_ref;
        BEGIN
          UPDATE bi.inventory_alert_instances AS upd SET
              status = d_next,
              last_observed_at = CASE WHEN d_observed THEN observed_at
                                      ELSE upd.last_observed_at END,
              last_notified_at = CASE WHEN d_notify THEN observed_at
                                      ELSE upd.last_notified_at END,
              resolved_at = CASE WHEN d_next = 'resolved' THEN observed_at
                                 ELSE upd.resolved_at END
            WHERE upd.alert_ref = target_alert;
        EXCEPTION WHEN OTHERS THEN
          RAISE EXCEPTION 'monitor_alert_write_failed';
        END;
      ELSE
        IF d_kind <> 'triggered' OR d_previous IS NOT NULL OR d_next <> 'open'
           OR d_generation <> history_generation + 1 THEN
          RAISE EXCEPTION 'monitor_invalid_transition';
        END IF;
        target_alert := 'alert-' || replace(gen_random_uuid()::text, '-', '');
        BEGIN
          INSERT INTO bi.inventory_alert_instances (
              alert_ref, dedupe_key, generation, policy_ref, rule_code, level,
              sku_ref, scope_ref, status, source_artifact_id, opened_at,
              last_observed_at, last_notified_at)
          VALUES (target_alert, d_key, d_generation,
                  commit_inventory_monitor_scan.policy_ref, d_rule, d_level,
                  d_sku, d_scope, 'open', artifact_id, observed_at,
                  observed_at, CASE WHEN d_notify THEN observed_at END);
        EXCEPTION WHEN OTHERS THEN
          RAISE EXCEPTION 'monitor_alert_write_failed';
        END;
      END IF;
      BEGIN
        INSERT INTO bi.inventory_alert_events (
            alert_ref, event_kind, previous_status, next_status,
            source_artifact_id, idempotency_key, created_at)
        VALUES (target_alert, d_kind, d_previous, d_next, artifact_id, event_key,
                observed_at)
        ON CONFLICT (idempotency_key) DO NOTHING;
      EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'monitor_event_write_failed';
      END;
      IF d_notify AND d_kind = ANY(ARRAY['triggered', 'retriggered', 'resolved']) THEN
        BEGIN
          INSERT INTO bi.notification_outbox (
              topic, owner_subject_id, alert_ref, event_id, idempotency_key,
              payload, available_at)
          VALUES ('inventory_alert', policy_row.owner_subject_id, target_alert,
                  (SELECT id FROM bi.inventory_alert_events
                    WHERE idempotency_key = event_key),
                  event_key,
                  jsonb_build_object(
                      'alert_ref', target_alert, 'event_kind', d_kind,
                      'status', d_next, 'level', d_level, 'sku_ref', d_sku,
                      'scope_ref', d_scope, 'rule_code', d_rule,
                      'quantity', to_jsonb(d_quantity),
                      'threshold', to_jsonb(d_threshold),
                      'unit', to_jsonb(d_unit),
                      'data_as_of', to_jsonb(data_as_of)),
                  observed_at)
          ON CONFLICT (idempotency_key) DO NOTHING;
        EXCEPTION WHEN OTHERS THEN
          RAISE EXCEPTION 'monitor_outbox_write_failed';
        END;
      END IF;
      transitions := transitions || jsonb_build_object(
          'alert_ref', target_alert, 'dedupe_key', d_key,
          'previous_status', to_jsonb(d_previous), 'next_status', d_next,
          'event_kind', d_kind);
    EXCEPTION WHEN lock_not_available THEN
      RAISE EXCEPTION 'monitor_lock_unavailable';
    END;
  END LOOP;

  RETURN jsonb_build_object('run_id', run_id, 'artifact_id', artifact_id,
                            'attempt_no', attempt_no,
                            'transitions', transitions);
END
$m023$;

-- ---------------------------------------------------------------------------
-- 6) 投递与用户动作：at-least-once outbox 投递、幂等已读、owner 复核的 ack。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION bi.deliver_inventory_outbox(
  delivery_at  timestamptz,
  batch_limit  integer
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, bi
AS $m023$
DECLARE
  batch integer := 100;
  outbox_row record;
  selected integer := 0;
  delivered integer := 0;
  retried integer := 0;
  dead_lettered integer := 0;
BEGIN
  IF batch_limit IS NOT NULL AND batch_limit >= 1 THEN
    batch := LEAST(batch_limit, 100);
  END IF;
  FOR outbox_row IN
    SELECT id, owner_subject_id, alert_ref, event_id, idempotency_key,
           payload, attempts
      FROM bi.notification_outbox
     WHERE delivered_at IS NULL
       AND available_at <= deliver_inventory_outbox.delivery_at
       AND attempts < 20
     ORDER BY id
     FOR UPDATE SKIP LOCKED
     LIMIT batch
  LOOP
    selected := selected + 1;
    BEGIN
      INSERT INTO bi.in_app_notifications (
          notification_ref, owner_subject_id, alert_ref, event_kind, payload,
          idempotency_key)
      VALUES ('ntf-' || replace(gen_random_uuid()::text, '-', ''),
              outbox_row.owner_subject_id, outbox_row.alert_ref,
              (SELECT event_kind FROM bi.inventory_alert_events
                WHERE id = outbox_row.event_id),
              outbox_row.payload, outbox_row.idempotency_key)
      ON CONFLICT (idempotency_key) DO NOTHING;
      UPDATE bi.notification_outbox
         SET delivered_at = delivery_at, attempts = attempts + 1
       WHERE id = outbox_row.id;
      delivered := delivered + 1;
    EXCEPTION WHEN OTHERS THEN
      -- 单行失败只记重试与指数退避；同一事务里其余行照常处理。
      UPDATE bi.notification_outbox
         SET attempts = attempts + 1,
             available_at = delivery_at + make_interval(secs =>
                 LEAST((30 * power(2, LEAST(outbox_row.attempts, 7)))::integer,
                       3600))
       WHERE id = outbox_row.id;
      IF outbox_row.attempts + 1 >= 20 THEN
        dead_lettered := dead_lettered + 1;
      ELSE
        retried := retried + 1;
      END IF;
    END;
  END LOOP;
  RETURN jsonb_build_object('selected', selected, 'delivered', delivered,
                            'retried', retried, 'dead_lettered', dead_lettered);
END
$m023$;

CREATE OR REPLACE FUNCTION bi.mark_inventory_notification_read(
  notification_ref text,
  actor_subject_id text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, bi
AS $m023$
DECLARE
  target bi.in_app_notifications%ROWTYPE;
  read_time timestamptz;
BEGIN
  SELECT * INTO target
    FROM bi.in_app_notifications AS n
   WHERE n.notification_ref = mark_inventory_notification_read.notification_ref
   FOR UPDATE;
  IF target.notification_ref IS NULL
     OR target.owner_subject_id
        <> mark_inventory_notification_read.actor_subject_id
     OR NOT bi.inventory_notification_payload_safe(target.payload) THEN
    RAISE EXCEPTION 'inventory_notification_not_found';
  END IF;
  UPDATE bi.in_app_notifications AS n
     SET read_at = COALESCE(n.read_at, now())
   WHERE n.notification_ref = target.notification_ref
  RETURNING read_at INTO read_time;
  RETURN jsonb_build_object('notification_ref', target.notification_ref,
                            'read_at', to_jsonb(read_time));
END
$m023$;

CREATE OR REPLACE FUNCTION bi.acknowledge_inventory_alert(
  alert_ref text,
  actor_subject_id text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, bi
AS $m023$
DECLARE
  owner text;
  current_status text;
  ack_time timestamptz;
BEGIN
  SELECT p.owner_subject_id, a.status, a.acknowledged_at
    INTO owner, current_status, ack_time
  FROM bi.inventory_alert_instances a
  JOIN bi.inventory_monitor_policies p ON p.policy_ref = a.policy_ref
  WHERE a.alert_ref = acknowledge_inventory_alert.alert_ref
  FOR UPDATE OF a;
  -- 跨 owner 与不存在返回同一个固定码：不泄漏任何告警的存在性。
  IF owner IS NULL OR owner <> acknowledge_inventory_alert.actor_subject_id THEN
    RAISE EXCEPTION 'inventory_alert_not_found';
  END IF;
  IF current_status = 'acknowledged' THEN
    RETURN jsonb_build_object('alert_ref',
        acknowledge_inventory_alert.alert_ref, 'status', current_status,
        'acknowledged_at', to_jsonb(ack_time));
  END IF;
  IF current_status <> 'open' THEN
    RAISE EXCEPTION 'inventory_alert_not_acknowledgeable';
  END IF;
  UPDATE bi.inventory_alert_instances AS upd
     SET status = 'acknowledged',
         acknowledged_by = acknowledge_inventory_alert.actor_subject_id,
         acknowledged_at = now()
   WHERE upd.alert_ref = acknowledge_inventory_alert.alert_ref
  RETURNING acknowledged_at INTO ack_time;
  RETURN jsonb_build_object('alert_ref',
      acknowledge_inventory_alert.alert_ref, 'status', 'acknowledged',
      'acknowledged_at', to_jsonb(ack_time));
END
$m023$;

-- ---------------------------------------------------------------------------
-- 7) owner 过滤的通知/告警 API 投影：bi_app 唯一的新读取面。owner 精确过滤
--    由应用查询带 owner_subject_id 完成（与 v_approved_query_examples 同一
--    机制），read/ack 两函数在库内再复核一次 owner。
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS reporting.v_inventory_notifications;
CREATE VIEW reporting.v_inventory_notifications AS
SELECT n.notification_ref, n.owner_subject_id, n.alert_ref, n.event_kind,
       a.status, a.level, a.sku_ref, a.scope_ref,
       (n.payload->>'rule_code') AS reason_code,
       (n.payload->>'quantity') AS quantity,
       (n.payload->>'threshold') AS threshold,
       (n.payload->>'unit') AS unit,
       (n.payload->>'data_as_of')::timestamptz AS data_as_of,
       n.read_at, n.created_at
FROM bi.in_app_notifications n
JOIN bi.inventory_alert_instances a ON a.alert_ref = n.alert_ref;

-- ---------------------------------------------------------------------------
-- 8) 权限：新表零公开授权；bi_monitor / bi_app 各拿精确的一小块。
-- ---------------------------------------------------------------------------
REVOKE ALL ON bi.inventory_monitor_policies, bi.inventory_alert_instances,
  bi.inventory_alert_events, bi.notification_outbox, bi.in_app_notifications
  FROM PUBLIC;
REVOKE ALL ON bi.inventory_monitor_policies, bi.inventory_alert_instances,
  bi.inventory_alert_events, bi.notification_outbox, bi.in_app_notifications
  FROM bi_app;
REVOKE ALL ON bi.inventory_monitor_policies, bi.inventory_alert_instances,
  bi.inventory_alert_events, bi.notification_outbox, bi.in_app_notifications
  FROM bi_monitor;

REVOKE ALL ON FUNCTION bi.inventory_notification_payload_safe(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION bi.commit_inventory_monitor_scan(
    text, jsonb, text, timestamptz, jsonb, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION bi.deliver_inventory_outbox(timestamptz, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION bi.mark_inventory_notification_read(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION bi.acknowledge_inventory_alert(text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION bi.commit_inventory_monitor_scan(
    text, jsonb, text, timestamptz, jsonb, timestamptz) TO bi_monitor;
GRANT EXECUTE ON FUNCTION bi.deliver_inventory_outbox(timestamptz, integer)
  TO bi_monitor;
GRANT EXECUTE ON FUNCTION bi.mark_inventory_notification_read(text, text)
  TO bi_app;
GRANT EXECUTE ON FUNCTION bi.acknowledge_inventory_alert(text, text) TO bi_app;

-- bi_monitor 的完整读取面：获准 reporting 库存视图 + 目录展示面（店铺显示名
-- 目录与目录版本号，不含库存数量、渠道快照或扫描证据）+ 监控策略。
GRANT USAGE ON SCHEMA bi, reporting TO bi_monitor;
GRANT SELECT ON bi.inventory_monitor_policies TO bi_monitor;
GRANT SELECT ON reporting.v_inventory_pools, reporting.v_inventory_pool_shops,
  reporting.v_physical_stock_snapshots, reporting.v_physical_stock_items,
  reporting.v_channel_stock_snapshots, reporting.v_channel_stock_items,
  reporting.v_inventory_threshold_policies,
  reporting.v_shops, reporting.v_catalog_version TO bi_monitor;

REVOKE ALL ON reporting.v_inventory_notifications FROM PUBLIC;
GRANT SELECT ON reporting.v_inventory_notifications TO bi_app;
