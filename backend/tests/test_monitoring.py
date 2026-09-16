"""持续库存监控 Task 1 契约：监控设置、严格模型与按层级来源门禁。

计划 2026-09-14-continuous-inventory-notifications.md Task 1：

- ``MonitorSettings`` 默认关闭；关闭时不要求任何秘密、也不把秘密装进配置；
  开启时三件套（独立 DSN、服务主体、策略清单）缺一不可，且监控 DSN 不得等于
  app / writer(同步) / approver 任一既有身份。
- 来源门禁按层级判定（所有者 2026-09-17 Option A 决定）：已核验层照常可跑，
  缺已核验登记的层级是一等 ``data_missing``——不是零、不替位；只有请求层级
  **全部**未核验才 ``monitor_source_unverified`` 整条策略 disabled 退出。
- ``InventorySourceRegistration`` 新增必填 ``scan_complete_supported`` 与
  ``production_reconciled_at``：登记凭什么算核验的口径变严，生产注册表仍为空。
- 模型是 bounded/frozen 的状态机输入面：ref 排序、去重、非空；行必带本层级
  作用域引用；诊断码只收闭集；数量文本与 runtime 载荷契约同一形状。

Task 1 交付契约、配置与 gate；Task 2 交付单事务 repository；本文件同时承载
计划 Task 3 的确定性状态机（``AlertStateMachineTests``）与 gold 转移矩阵
（``AlertTransitionGoldTests``，tests/fixtures/inventory_monitor_transitions.json）。
runner / CLI 属后续 Task。
"""

import hashlib
import json
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

import psycopg

# 监控侧登记用的"生产对账时点"固定值：只存在于测试夹具的合成场景里。
RECONCILED_AT = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)


def valid_monitor_env() -> dict[str, str]:
    """监控 loader 的环境基线：四个监控变量 + 三个既有身份的 DSN。

    ``BI_APP_DSN`` 等既有身份放在这里，是为了钉"监控 DSN 不得与它们相同"：
    同一个部署环境里这些变量并存，混用任何一个是身份边界事故。
    """
    return {
        "BI_APP_DSN": "postgresql://bi_app:pw@localhost/bi_agent",
        "BI_WRITER_DSN": "postgresql://bi_sync:pw@localhost/bi_agent",
        "BI_APPROVER_DSN": "postgresql://bi_approver:pw@localhost/bi_agent",
        "INVENTORY_MONITOR_ENABLED": "false",
        "BI_MONITOR_DSN": "postgresql://bi_monitor:pw@localhost/bi_agent",
        "MONITOR_SERVICE_SUBJECT": "inventory-monitor-service",
        "MONITOR_POLICY_REFS": "inventory-monitor/1, inventory-monitor/1.1",
    }


def physical_and_channel_policy():
    """请求两层的策略：Option A 的主形态（实物已核验、渠道缺登记）。"""
    from bi_agent.monitoring.models import MonitorPolicy

    return MonitorPolicy(
        policy_ref="inventory-monitor/1",
        threshold_policy_ref="inventory-thresholds/1",
        owner_subject_id="owner-subject",
        shop_refs=("shop-a",),
        inventory_pool_refs=("pool-a",),
        levels=("physical_total", "shop_sellable"),
        cooldown_seconds=3600,
        enabled=True,
    )


def physical_only_policy():
    """只请求实物层的策略：行为必须与两层策略在实物层一致。"""
    from bi_agent.monitoring.models import MonitorPolicy

    return MonitorPolicy(
        policy_ref="inventory-monitor/2",
        threshold_policy_ref="inventory-thresholds/1",
        owner_subject_id="owner-subject",
        shop_refs=(),
        inventory_pool_refs=("pool-a",),
        levels=("physical_total",),
        cooldown_seconds=3600,
        enabled=True,
    )


def physical_only_registrations():
    """只有实物层带完整监控凭据的登记表：渠道层是一等 data_missing。

    ``scan_complete_supported=True`` + 非空 ``production_reconciled_at`` 是这一层
    通过监控门禁的合成场景；它不构成任何真实来源就绪声明（来源验收仍 FAIL）。
    """
    from bi_agent.inventory.rules import InventorySourceRegistration

    return {
        "physical_total": InventorySourceRegistration(
            level="physical_total", channel="erp", evidence="probe-1",
            max_age_seconds=86400, scan_complete_supported=True,
            production_reconciled_at=RECONCILED_AT),
    }


def channel_only_registrations():
    """只有渠道层带完整监控凭据的登记表：对称用例的输入。"""
    from bi_agent.inventory.rules import InventorySourceRegistration

    return {
        "shop_sellable": InventorySourceRegistration(
            level="shop_sellable", channel="official_export", evidence="probe-1",
            max_age_seconds=86400, scan_complete_supported=True,
            production_reconciled_at=RECONCILED_AT),
    }


class MonitorContractTests(unittest.TestCase):
    """计划 Task 1 Step 1 的三个焦点用例：逐字钉方向。"""

    def test_monitor_settings_require_separate_identity_when_enabled(self):
        from bi_agent.config import load_monitor_settings

        env = valid_monitor_env() | {
            "INVENTORY_MONITOR_ENABLED": "true",
            "BI_MONITOR_DSN": valid_monitor_env()["BI_APP_DSN"],
        }
        with self.assertRaisesRegex(ValueError, "MONITOR_DSN_MUST_BE_SEPARATE"):
            load_monitor_settings(env)

    def test_policy_with_no_verified_level_exits_disabled(self):
        from bi_agent.monitoring.source_gate import assert_monitor_sources_verified

        with self.assertRaisesRegex(ValueError, "monitor_source_unverified"):
            assert_monitor_sources_verified(physical_and_channel_policy(),
                                            registrations={})

    def test_verified_physical_runs_while_channel_is_data_missing(self):
        from bi_agent.monitoring.source_gate import assert_monitor_sources_verified

        runnable = assert_monitor_sources_verified(
            physical_and_channel_policy(),
            registrations=physical_only_registrations())
        self.assertEqual(runnable, ("physical_total",))
        # 这个返回值就是 runner 交给 graph 的 levels（Task 4 Step 3）：未核验的渠道层
        # 不被询问一次，所以不会以 graph 占位行的形式回来，也不会撑 expected_items。
        self.assertNotIn("shop_sellable", runnable)
        self.assertEqual(
            assert_monitor_sources_verified(physical_only_policy(),
                                            registrations=physical_only_registrations()),
            ("physical_total",))   # 只请求一层的策略行为不变


class MonitorSettingsGateTests(unittest.TestCase):
    """MonitorSettings：默认关、关不装秘密、开要三件套与独立身份。"""

    def test_gate_defaults_off_and_disabled_needs_no_secret(self):
        """缺席与显式 false 都是关；关着的监控不把 DSN 装进配置（功能关就是全关）。"""
        from bi_agent.config import MonitorSettings, load_monitor_settings

        self.assertFalse(load_monitor_settings({}).enabled)
        self.assertFalse(load_monitor_settings({"INVENTORY_MONITOR_ENABLED": "false"}).enabled)
        # 环境里哪怕放着 DSN/主体/策略清单，关着的门禁也不装载它们：
        # 与 approved 查询记忆的 approver 设置同一口径。
        settings = load_monitor_settings(valid_monitor_env())
        self.assertFalse(settings.enabled)
        self.assertIsNone(settings.monitor_dsn)
        self.assertEqual(settings.service_subject_id, "")
        self.assertEqual(settings.policy_refs, ())
        self.assertEqual((settings.max_policies, settings.run_deadline_seconds),
                         (100, 30))
        self.assertIs(MonitorSettings.model_fields["enabled"].default, False)
        for bad in ("TRUE", "True", "1", "0", "yes", "on", "enabled", ";"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "INVENTORY_MONITOR_ENABLED"):
                    load_monitor_settings({"INVENTORY_MONITOR_ENABLED": bad})

    def test_enabled_requires_all_three_values(self):
        """开启时独立 DSN、服务主体、策略清单缺一不可；报错不回显任何 DSN。"""
        from bi_agent.config import load_monitor_settings

        enabled = valid_monitor_env() | {"INVENTORY_MONITOR_ENABLED": "true"}
        base_dsn = valid_monitor_env()["BI_MONITOR_DSN"]
        with self.assertRaises(ValueError) as caught:
            load_monitor_settings({**enabled, "BI_MONITOR_DSN": ""})
        self.assertEqual(str(caught.exception),
                         "INVENTORY_MONITOR_REQUIRES_BI_MONITOR_DSN")
        with self.assertRaises(ValueError) as caught:
            load_monitor_settings({**enabled, "MONITOR_SERVICE_SUBJECT": " "})
        self.assertEqual(str(caught.exception),
                         "INVENTORY_MONITOR_REQUIRES_MONITOR_SERVICE_SUBJECT")
        with self.assertRaises(ValueError) as caught:
            load_monitor_settings({**enabled, "MONITOR_POLICY_REFS": ""})
        self.assertEqual(str(caught.exception),
                         "INVENTORY_MONITOR_REQUIRES_MONITOR_POLICY_REFS")
        # 报错路径不得回显秘密：DSN 原文不出现在任何 ValueError 文本里。
        self.assertNotIn(base_dsn, str(caught.exception))

    def test_enabled_settings_load_secrets_without_echoing_them(self):
        """合法开启：三件套进配置；SecretStr 的 repr/str 不落 DSN。"""
        from bi_agent.config import load_monitor_settings

        settings = load_monitor_settings(
            {**valid_monitor_env(), "INVENTORY_MONITOR_ENABLED": "true"})
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.monitor_dsn.get_secret_value(),
                         valid_monitor_env()["BI_MONITOR_DSN"])
        self.assertEqual(settings.service_subject_id, "inventory-monitor-service")
        self.assertEqual(settings.policy_refs,
                         ("inventory-monitor/1", "inventory-monitor/1.1"))
        self.assertNotIn(valid_monitor_env()["BI_MONITOR_DSN"], repr(settings))
        self.assertNotIn(valid_monitor_env()["BI_MONITOR_DSN"], str(settings))

    def test_enabled_monitor_dsn_must_differ_from_every_other_identity(self):
        """监控 DSN 不得等于 app / writer(同步) / approver 任一既有身份。"""
        from bi_agent.config import load_monitor_settings

        for other in ("BI_APP_DSN", "BI_WRITER_DSN", "BI_APPROVER_DSN"):
            with self.subTest(other=other):
                env = {**valid_monitor_env(), "INVENTORY_MONITOR_ENABLED": "true",
                       "BI_MONITOR_DSN": valid_monitor_env()[other]}
                with self.assertRaisesRegex(ValueError, "MONITOR_DSN_MUST_BE_SEPARATE"):
                    load_monitor_settings(env)

    def test_policy_refs_are_parsed_unique_and_capped_at_100(self):
        """策略清单逗号切分、去空白、拒绝重复、最多 100 个。"""
        from bi_agent.config import load_monitor_settings

        enabled = {**valid_monitor_env(), "INVENTORY_MONITOR_ENABLED": "true",
                   "MONITOR_POLICY_REFS": " inventory-monitor/1 ,  ,inventory-monitor/2 "}
        self.assertEqual(load_monitor_settings(enabled).policy_refs,
                         ("inventory-monitor/1", "inventory-monitor/2"))
        duplicate = {**enabled,
                     "MONITOR_POLICY_REFS": "inventory-monitor/1,inventory-monitor/1"}
        with self.assertRaisesRegex(ValueError, "MONITOR_POLICY_REFS_MUST_BE_UNIQUE"):
            load_monitor_settings(duplicate)
        too_many = {**enabled, "MONITOR_POLICY_REFS":
                    ",".join(f"inventory-monitor/{i}" for i in range(101))}
        with self.assertRaisesRegex(ValueError, "MONITOR_POLICY_REFS_TOO_MANY"):
            load_monitor_settings(too_many)
        exactly_100 = {**enabled, "MONITOR_POLICY_REFS":
                       ",".join(f"inventory-monitor/{i}" for i in range(100))}
        self.assertEqual(len(load_monitor_settings(exactly_100).policy_refs), 100)

    def test_loader_reads_only_the_four_monitor_variables(self):
        """loader 只读四个监控变量：同环境多塞无关变量不改变结果。"""
        from bi_agent.config import load_monitor_settings

        quiet = {**valid_monitor_env(), "INVENTORY_MONITOR_ENABLED": "true"}
        # 部署环境常见的无关变量：loader 一个都不读，设置逐字相同。
        noisy = {**quiet, "SEMANTIC_CATALOG_ENABLED": "true",
                 "APP_ALLOWED_SUBJECTS": "a", "APP_ENV": "production",
                 "LLM_PROVIDER": "qwen", "KUAI_MAI_APP_KEY": "x"}
        self.assertEqual(load_monitor_settings(quiet),
                         load_monitor_settings(noisy))


class MonitorPolicyModelTests(unittest.TestCase):
    """MonitorPolicy：ref 排序、去重、非空；层级驱动各自的作用域要求。"""

    def test_scope_refs_must_be_sorted_unique_and_nonempty(self):
        """未排序 / 重复 / 空白 ref 都当场拒绝：策略是进版本化存储的输入面。"""
        from pydantic import ValidationError

        from bi_agent.monitoring.models import MonitorPolicy

        base = dict(policy_ref="inventory-monitor/1",
                    threshold_policy_ref="inventory-thresholds/1",
                    owner_subject_id="owner-subject",
                    levels=("physical_total",), cooldown_seconds=3600, enabled=True)
        with self.assertRaises(ValidationError):   # 未排序
            MonitorPolicy(**{**base, "shop_refs": ("shop-b", "shop-a"),
                             "inventory_pool_refs": ("pool-a",)})
        with self.assertRaises(ValidationError):   # 重复
            MonitorPolicy(**{**base, "shop_refs": ("shop-a", "shop-a"),
                             "inventory_pool_refs": ("pool-a",)})
        with self.assertRaises(ValidationError):   # 空白 ref
            MonitorPolicy(**{**base, "shop_refs": ("shop-a",),
                             "inventory_pool_refs": ("pool-a", " ")})
        # 排序去重非空的正例回到两个 helper 已经验过的构造上。
        self.assertEqual(physical_and_channel_policy().inventory_pool_refs, ("pool-a",))
        self.assertEqual(physical_and_channel_policy().shop_refs, ("shop-a",))

    def test_levels_drive_per_level_scope_requirements(self):
        """physical_total 必须带池 ref，shop_sellable 必须带店 ref；levels 非空有序。"""
        from pydantic import ValidationError

        from bi_agent.monitoring.models import MonitorPolicy

        base = dict(policy_ref="inventory-monitor/1",
                    threshold_policy_ref="inventory-thresholds/1",
                    owner_subject_id="owner-subject",
                    inventory_pool_refs=("pool-a",), shop_refs=("shop-a",),
                    cooldown_seconds=3600, enabled=True)
        with self.assertRaises(ValidationError):
            MonitorPolicy(**{**base, "levels": ("physical_total",),
                             "inventory_pool_refs": ()})
        with self.assertRaises(ValidationError):
            MonitorPolicy(**{**base, "levels": ("shop_sellable",), "shop_refs": ()})
        with self.assertRaises(ValidationError):
            MonitorPolicy(**{**base, "levels": ()})
        with self.assertRaises(ValidationError):
            # 未排序的 levels 同样拒绝：levels 是策略存储的一部分，不是运行时集合。
            MonitorPolicy(**{**base, "levels": ("shop_sellable", "physical_total")})
        # 渠道层策略允许 shop_refs 为空池策略；实物层策略允许 pool-only。
        self.assertEqual(physical_only_policy().shop_refs, ())

    def test_policy_ref_cooldown_and_error_input_hiding(self):
        """policy_ref 形状、冷却区间与 hide_input_in_errors 的不回显。"""
        from pydantic import ValidationError

        from bi_agent.monitoring.models import MonitorPolicy

        base = dict(threshold_policy_ref="inventory-thresholds/1",
                    owner_subject_id="owner-subject",
                    shop_refs=("shop-a",), inventory_pool_refs=("pool-a",),
                    levels=("physical_total",), cooldown_seconds=3600, enabled=True)
        for bad_ref in ("inventory-monitor/", "other/1", "inventory-monitor/-x"):
            with self.subTest(bad_ref=bad_ref):
                with self.assertRaises(ValidationError):
                    MonitorPolicy(policy_ref=bad_ref, **base)
        for bad_cooldown in (3599, 604801):
            with self.subTest(bad_cooldown=bad_cooldown):
                with self.assertRaises(ValidationError):
                    MonitorPolicy(policy_ref="inventory-monitor/1",
                                  **{**base, "cooldown_seconds": bad_cooldown})
        with self.assertRaises(ValidationError) as caught:
            # 非法输入里带 owner subject：错误文本不得把原始输入回显出来。
            MonitorPolicy(policy_ref="bad ref", owner_subject_id="subject-secret",
                          **{k: v for k, v in base.items() if k != "owner_subject_id"})
        self.assertNotIn("subject-secret", str(caught.exception))


class MonitorRowAndScanModelTests(unittest.TestCase):
    """MonitorAlertRow / MonitorScan：行必带作用域引用，诊断码闭集。"""

    def row(self, **overrides):
        from bi_agent.monitoring.models import MonitorAlertRow

        values = dict(level="physical_total", status="low", sku_ref="ent-sku-1",
                      scope_ref="pool-a|wh-a", quantity="3", threshold="2",
                      unit="piece")
        values.update(overrides)
        return MonitorAlertRow(**values)

    def test_alert_row_requires_nonempty_scope_ref(self):
        """缺作用域引用的 graph 行根本拼不出这个模型（Task 4 Step 4 的第二道闸）。"""
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self.row(scope_ref="")
        with self.assertRaises(ValidationError):
            self.row(scope_ref=" ")
        row = self.row()
        self.assertEqual((row.level, row.status, row.sku_ref, row.scope_ref),
                         ("physical_total", "low", "ent-sku-1", "pool-a|wh-a"))

    def test_alert_row_quantity_threshold_follow_runtime_decimal_shape(self):
        """数量/阈值文本与 runtime 载荷契约同一形状；空串不是 null。"""
        from pydantic import ValidationError

        self.assertIsNone(self.row(quantity=None).quantity)
        self.assertIsNone(self.row(threshold=None).threshold)
        self.assertEqual(self.row(quantity="0").quantity, "0")
        self.assertEqual(self.row(quantity="100.0000").quantity, "100.0000")
        for bad in ("1e3", "1,000", " 3", "3 ", "+3", ".5"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.row(quantity=bad)

    def test_alert_row_status_and_unit_stay_in_runtime_vocabularies(self):
        """status 与 unit 收 runtime 载荷同一份闭集词表，不收自由文本。"""
        from pydantic import ValidationError

        for status in ("low", "normal", "unconfigured", "unknown", "stale",
                       "data_anomaly", "unsupported"):
            with self.subTest(status=status):
                self.row(status=status)
        with self.assertRaises(ValidationError):
            self.row(status="fine")
        for unit in ("piece", "box", "set", "kit"):
            with self.subTest(unit=unit):
                self.row(unit=unit)
        with self.assertRaises(ValidationError):
            self.row(unit="pallet")

    def test_scan_complete_levels_are_sorted_unique_audit_levels(self):
        """complete_levels 只能是两个口径里的有序去重子集，空集合法（截断轮）。"""
        from pydantic import ValidationError

        from bi_agent.monitoring.models import MonitorScan

        def scan(**overrides):
            values = dict(policy_ref="inventory-monitor/1",
                          source_artifact_payload={"data": []},
                          source_fingerprint="a" * 64,
                          data_as_of=RECONCILED_AT, fresh=True,
                          complete_levels=("physical_total",),
                          rows=(), diagnostics=())
            values.update(overrides)
            return MonitorScan(**values)

        scan()
        scan(complete_levels=())
        scan(complete_levels=("physical_total", "shop_sellable"))
        for bad in (("shop_sellable", "physical_total"),
                    ("physical_total", "physical_total"),
                    ("both_levels",)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    scan(complete_levels=bad)

    def test_scan_diagnostics_are_a_closed_vocabulary(self):
        """诊断码只收 graph 既有固定限制码 + monitor_ 前缀固定码，不自创。"""
        from pydantic import ValidationError

        from bi_agent.monitoring.models import MonitorScan

        def scan(diagnostics):
            return MonitorScan(policy_ref="inventory-monitor/1",
                               source_artifact_payload={"data": []},
                               source_fingerprint="a" * 64,
                               data_as_of=RECONCILED_AT, fresh=True,
                               complete_levels=("physical_total",), rows=(),
                               diagnostics=diagnostics)

        allowed = ("inventory_snapshot_missing", "inventory_channel_snapshot_missing",
                   "inventory_scan_incomplete", "inventory_audit_incomplete",
                   "inventory_snapshot_stale", "inventory_display_truncated",
                   "monitor_source_unverified", "monitor_level_unverified",
                   "monitor_unverified_level_row", "monitor_row_scope_missing")
        for code in allowed:
            with self.subTest(code=code):
                scan((code,))
        for bad in ("inventory_made_up_code", "monitor_made_up",
                    "inventory_snapshot_missing "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    scan((bad,))

    def test_scan_source_fingerprint_is_a_sha256_text(self):
        from pydantic import ValidationError

        from bi_agent.monitoring.models import MonitorScan

        with self.assertRaises(ValidationError):
            MonitorScan(policy_ref="inventory-monitor/1",
                        source_artifact_payload={"data": []},
                        source_fingerprint="XYZ", data_as_of=RECONCILED_AT,
                        fresh=True, complete_levels=(), rows=(), diagnostics=())

    def test_transition_and_run_request_ref_shapes(self):
        """MonitorRunRequest / AlertTransition 的 ref 形状与枚举当场拒绝坏值。"""
        from pydantic import ValidationError

        from bi_agent.monitoring.models import (AlertTransition, MonitorRunRequest)

        MonitorRunRequest(policy_ref="inventory-monitor/1")
        with self.assertRaises(ValidationError):
            MonitorRunRequest(policy_ref="nope")
        with self.assertRaises(ValidationError):
            MonitorRunRequest(policy_ref="inventory-monitor/1", as_of="now")
        AlertTransition(alert_ref="alert-abc123", previous_status=None,
                        next_status="open", event_kind="triggered",
                        dedupe_key="b" * 64, source_artifact_ref="art-1")
        for bad_alert_ref in ("alert-", "Alert-1", "alert-x_y"):
            with self.subTest(bad_alert_ref=bad_alert_ref):
                with self.assertRaises(ValidationError):
                    AlertTransition(alert_ref=bad_alert_ref, previous_status=None,
                                    next_status="open", event_kind="triggered",
                                    dedupe_key="b" * 64, source_artifact_ref="art-1")
        with self.assertRaises(ValidationError):
            AlertTransition(alert_ref="alert-abc123", previous_status="open",
                            next_status="closed", event_kind="triggered",
                            dedupe_key="b" * 64, source_artifact_ref="art-1")
        with self.assertRaises(ValidationError):
            AlertTransition(alert_ref="alert-abc123", previous_status=None,
                            next_status="open", event_kind="escalated",
                            dedupe_key="b" * 64, source_artifact_ref="art-1")


class MonitorSourceGateTests(unittest.TestCase):
    """verified_monitor_levels：核验判定逐维独立，顺序跟随 policy.levels。"""

    def gate(self):
        from bi_agent.monitoring.source_gate import verified_monitor_levels

        return verified_monitor_levels

    def registration(self, *, level="physical_total", scan_complete=True,
                     reconciled=RECONCILED_AT, max_age_seconds=86400):
        from bi_agent.inventory.rules import InventorySourceRegistration

        return InventorySourceRegistration(
            level=level, channel="erp" if level == "physical_total"
            else "official_export", evidence="probe-1",
            max_age_seconds=max_age_seconds, scan_complete_supported=scan_complete,
            production_reconciled_at=reconciled)

    def test_each_monitor_evidence_dimension_is_required_per_level(self):
        """scan_complete / production_reconciled / 登记缺席任一缺失只排除该层。

        freshness（``max_age_seconds <= 0``）在登记处就被注册表拒了
        （``inventory_source_freshness_policy_required``），所以这里不需要再造
        一条非法登记：gate 的 ``max_age_seconds > 0`` 只是第二道防御。
        """
        both = physical_and_channel_policy()
        cases = (
            {"scan_complete": False},
            {"reconciled": None},
            None,   # 登记表里根本没有这一层
        )
        for kwargs in cases:
            with self.subTest(case=str(kwargs)):
                registrations = ({"physical_total": self.registration(**kwargs)}
                                 if kwargs is not None else {})
                self.assertEqual(self.gate()(both, registrations), ())
                self.assertEqual(
                    self.gate()(physical_only_policy(), registrations), ())
        # 渠道层自己的凭据只回答渠道层：实物层不因渠道缺凭据而一起失效。
        registrations = {
            "physical_total": self.registration(),
            "shop_sellable": self.registration(level="shop_sellable",
                                               scan_complete=False)}
        self.assertEqual(self.gate()(both, registrations), ("physical_total",))
        registrations = {
            "physical_total": self.registration(),
            "shop_sellable": self.registration(level="shop_sellable",
                                               reconciled=None)}
        self.assertEqual(self.gate()(both, registrations), ("physical_total",))
        registrations = {"shop_sellable": self.registration(level="shop_sellable")}
        self.assertEqual(self.gate()(both, registrations), ("shop_sellable",))

    def test_verified_levels_keep_policy_levels_order_and_dedup_registry_noise(self):
        """返回集合只含 policy.levels 里已核验的子集，顺序照 policy.levels。"""
        from bi_agent.monitoring.models import MonitorPolicy
        from bi_agent.inventory.rules import InventorySourceRegistration

        policy = MonitorPolicy(
            policy_ref="inventory-monitor/1",
            threshold_policy_ref="inventory-thresholds/1",
            owner_subject_id="owner-subject",
            shop_refs=("shop-a",), inventory_pool_refs=("pool-a",),
            levels=("physical_total", "shop_sellable"),
            cooldown_seconds=3600, enabled=True)
        registrations = {
            "shop_sellable": InventorySourceRegistration(
                level="shop_sellable", channel="official_export", evidence="probe-1",
                max_age_seconds=86400, scan_complete_supported=True,
                production_reconciled_at=RECONCILED_AT),
            "physical_total": InventorySourceRegistration(
                level="physical_total", channel="erp", evidence="probe-1",
                max_age_seconds=86400, scan_complete_supported=True,
                production_reconciled_at=RECONCILED_AT),
        }
        self.assertEqual(self.gate()(policy, registrations),
                         ("physical_total", "shop_sellable"))
        # 登记表里有策略没请求的层级：不进返回集合。
        extra = dict(registrations)
        self.assertEqual(self.gate()(physical_only_policy(), extra),
                         ("physical_total",))

    def test_assert_returns_verified_or_raises_only_when_empty(self):
        """assert_* 在有已核验层时原样返回；只在全空时 monitor_source_unverified。"""
        from bi_agent.monitoring.source_gate import assert_monitor_sources_verified

        both = physical_and_channel_policy()
        self.assertEqual(
            assert_monitor_sources_verified(both,
                                            registrations=channel_only_registrations()),
            ("shop_sellable",))
        with self.assertRaisesRegex(ValueError, "monitor_source_unverified"):
            assert_monitor_sources_verified(
                physical_only_policy(),
                registrations=channel_only_registrations())


# --- 计划 Task 2：MonitorRepository 的单事务持久化 -----------------------------

from .fakeconn import MONITOR_STEP_CODES, MonitorConn

MONITOR_NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
SNAPSHOT_AT = MONITOR_NOW - timedelta(minutes=5)
SNAPSHOT_ISO = SNAPSHOT_AT.isoformat()
SOURCE_FINGERPRINT = hashlib.sha256(b"monitor-source").hexdigest()


def _sku_ref(tag: str) -> str:
    from bi_agent.catalog import ref_for_key
    from bi_agent.catalog.models import EntityKind

    return ref_for_key(EntityKind.SKU.value, tag)


def _shop_ref(tag: str) -> str:
    from bi_agent.catalog import ref_for_key

    return ref_for_key("shop", tag)


SKU_REF = _sku_ref("SKU1")
POOL_REF = "pl-0123456789ab"
WAREHOUSE_REF = "wh-0123456789ab"
SCOPE_REF = f"{POOL_REF}|{WAREHOUSE_REF}"
DEDUPE_KEY_A = hashlib.sha256(b"cell-a").hexdigest()
DEDUPE_KEY_B = hashlib.sha256(b"cell-b").hexdigest()
POLICY_REF = "inventory-monitor/1"


def alerts_payload(**overrides) -> dict:
    """一份对 inventory_alerts 载荷契约合法的最小来源载荷（合成引用）。"""
    from bi_agent.runtime.models import validate_artifact_payload

    row = {"level": "physical_total", "sku_ref": SKU_REF, "pool_ref": POOL_REF,
           "warehouse_ref": WAREHOUSE_REF, "quantity": "10", "threshold": "10",
           "unit": "piece", "inventory_status": "low",
           "snapshot_at": SNAPSHOT_ISO, "batch_count": 1}
    payload = {
        "status": "partial",
        "inventory": {
            "expected_items": 1, "evaluated_items": 1, "scanned_items": 1,
            "truncated": False, "all_safe": False, "counts": {"low": 1},
            "levels": ["physical_total"], "threshold_source": "this_turn",
            "pools": [{"pool_ref": POOL_REF, "connection_kind": "shared",
                       "fresh": True, "scan_complete": True,
                       "snapshot_at": SNAPSHOT_ISO}],
            "freshness_policy_seconds": 86400,
            "rule_version": "inventory-rules/2026-09-14.1"},
        "data": [row],
        "filters": {"as_of": "latest", "levels": ["physical_total"],
                    "products": "selected",
                    "thresholds": [{"level": "low_replenish", "sku_ref": SKU_REF,
                                    "quantity": "10", "unit": "piece"}]},
        "limitations": [],
    }
    payload.update(overrides)
    # 基线本身必须合法：改坏形状的用例要在自己现场被证明是载荷不合法，而不是
    # 基线本来就过不了校验。
    validate_artifact_payload(payload, "inventory_alerts")
    return payload


def make_scan(**overrides):
    from bi_agent.monitoring.models import MonitorAlertRow, MonitorScan

    row = MonitorAlertRow(level="physical_total", status="low", sku_ref=SKU_REF,
                          scope_ref=SCOPE_REF, quantity="10", threshold="10",
                          unit="piece")
    values = dict(policy_ref=POLICY_REF, source_artifact_payload=alerts_payload(),
                  source_fingerprint=SOURCE_FINGERPRINT, data_as_of=MONITOR_NOW,
                  fresh=True, complete_levels=("physical_total",), rows=(row,),
                  diagnostics=())
    values.update(overrides)
    return MonitorScan(**values)


SCAN = make_scan()


def make_decision(**overrides):
    from bi_agent.monitoring.models import AlertDecision

    values = dict(dedupe_key=DEDUPE_KEY_A, generation=1, previous_status=None,
                  next_status="open", event_kind="triggered", notify=True,
                  observed=True, rule_code="low_replenish",
                  level="physical_total", sku_ref=SKU_REF, scope_ref=SCOPE_REF)
    values.update(overrides)
    return AlertDecision(**values)


def monitor_policy_dict(*, enabled: bool = True) -> dict:
    return {"policy_ref": POLICY_REF,
            "threshold_policy_ref": "inventory-thresholds/1",
            "owner_subject_id": "owner-subject", "shop_refs": ["shop-a"],
            "inventory_pool_refs": [POOL_REF],
            "levels": ["physical_total", "shop_sellable"],
            "cooldown_seconds": 3600, "enabled": enabled}


def stored_alert(**overrides) -> dict:
    values = dict(alert_ref="alert-open0001", dedupe_key=DEDUPE_KEY_A, generation=1,
                  policy_ref=POLICY_REF, rule_code="low_replenish",
                  level="physical_total", sku_ref=SKU_REF, scope_ref=SCOPE_REF,
                  status="open", opened_at=SNAPSHOT_AT,
                  last_observed_at=SNAPSHOT_AT, last_notified_at=SNAPSHOT_AT)
    values.update(overrides)
    return values


def repository_with_failure(step: str | None):
    from bi_agent.monitoring.repository import MonitorRepository

    conn = MonitorConn(policies={POLICY_REF: monitor_policy_dict()}, fail_at=step)
    return MonitorRepository(conn), conn


class MonitorRepositoryTests(unittest.TestCase):
    """计划 Task 2 Step 1：单事务提交、稳定错误映射与失败零残留。

    用真库形状的替身（fakeconn.MonitorConn）注入每个写步骤的失败；真库上的
    原子性、权限矩阵与并发由 tests.test_db 的 InventoryMonitorMigrationTests
    另行证明（023 只在本机 *_test 库执行）。
    """

    def test_outbox_failure_rolls_back_alert_and_source_artifact(self):
        repository, conn = repository_with_failure("insert_outbox")
        with self.assertRaisesRegex(RuntimeError, "outbox_write_failed"):
            repository.commit_scan(SCAN, (make_decision(),), now=MONITOR_NOW)
        self.assertEqual(conn.counts(), {"runs": 0, "artifacts": 0, "alerts": 0,
                                         "events": 0, "outbox": 0})
        self.assertEqual(conn.writes, [], "失败的事务不得升任何写入")

    def test_rollback_covers_every_injected_failure_step(self):
        for step, code in MONITOR_STEP_CODES.items():
            with self.subTest(step=step):
                repository, conn = repository_with_failure(step)
                with self.assertRaisesRegex(RuntimeError, code):
                    repository.commit_scan(SCAN, (make_decision(),), now=MONITOR_NOW)
                self.assertEqual(conn.counts(), {"runs": 0, "artifacts": 0,
                                                 "alerts": 0, "events": 0,
                                                 "outbox": 0})
                self.assertEqual(conn.writes, [])

    def test_commit_scan_calls_only_the_fixed_function_in_one_transaction(self):
        repository, conn = repository_with_failure(None)
        transitions = repository.commit_scan(SCAN, (make_decision(),), now=MONITOR_NOW)
        self.assertEqual(conn.sql_log,
                         ["SELECT bi.commit_inventory_monitor_scan("
                          "%s, %s, %s, %s, %s, %s)"])
        self.assertEqual(conn.counts(), {"runs": 1, "artifacts": 1, "alerts": 1,
                                         "events": 1, "outbox": 1})
        self.assertEqual([kind for _, kind, _ in conn.writes],
                         ["run", "artifact", "alert", "event", "outbox"])
        self.assertTrue(all(depth == 1 for depth, _, _ in conn.writes))
        self.assertEqual(len(transitions), 1)
        transition = transitions[0]
        self.assertEqual((transition.previous_status, transition.next_status,
                          transition.event_kind, transition.dedupe_key),
                         (None, "open", "triggered", DEDUPE_KEY_A))
        self.assertEqual(transition.source_artifact_ref,
                         str(conn.artifacts[0]["artifact_id"]))

    def test_commit_scan_enriches_decisions_with_their_own_row_fields(self):
        repository, conn = repository_with_failure(None)
        repository.commit_scan(SCAN, (make_decision(),), now=MONITOR_NOW)
        self.assertEqual(conn.outbox[0]["payload"]["quantity"], "10")
        self.assertEqual(conn.outbox[0]["payload"]["threshold"], "10")
        self.assertEqual(conn.outbox[0]["payload"]["unit"], "piece")

    def test_commit_scan_rejects_unsafe_source_payload_before_any_sql(self):
        poisoned = alerts_payload()
        poisoned["inventory"]["pools"][0]["evidence"] = "scan-evidence-text"
        repository, conn = repository_with_failure(None)
        with self.assertRaisesRegex(ValueError, "monitor_source_payload_unsafe"):
            repository.commit_scan(make_scan(source_artifact_payload=poisoned),
                                   (make_decision(),), now=MONITOR_NOW)
        self.assertEqual(conn.sql_log, [], "未过验证的输入不得碰到数据库")

    def test_commit_scan_rejects_artifact_type_outside_the_domain(self):
        from unittest.mock import patch

        repository, conn = repository_with_failure(None)
        with patch("bi_agent.monitoring.repository.allows_artifact_type",
                   return_value=False):
            with self.assertRaisesRegex(ValueError,
                                        "monitor_artifact_type_not_allowed"):
                repository.commit_scan(SCAN, (make_decision(),), now=MONITOR_NOW)
        self.assertEqual(conn.sql_log, [])

    def test_commit_scan_revalidates_decisions_before_the_database(self):
        repository, conn = repository_with_failure(None)
        malformed = make_decision().model_construct(
            **{**make_decision().model_dump(), "event_kind": "escalated"})
        with self.assertRaisesRegex(ValueError, "monitor_invalid_decision"):
            repository.commit_scan(SCAN, (malformed,), now=MONITOR_NOW)
        with self.assertRaisesRegex(ValueError, "monitor_invalid_decision"):
            # 同一格两条决策：冲突，不是重复强调（与 023 的 SQL 侧同一拒绝）。
            repository.commit_scan(SCAN, (make_decision(), make_decision()),
                                   now=MONITOR_NOW)
        bad_key = make_decision().model_construct(
            **{**make_decision().model_dump(), "dedupe_key": "XYZ"})
        with self.assertRaisesRegex(ValueError, "monitor_invalid_decision"):
            repository.commit_scan(SCAN, (bad_key,), now=MONITOR_NOW)
        self.assertEqual(conn.sql_log, [])

    def test_commit_scan_enforces_the_decision_budget_before_the_database(self):
        decisions = tuple(make_decision(dedupe_key=hashlib.sha256(
            str(index).encode()).hexdigest()) for index in range(501))
        repository, conn = repository_with_failure(None)
        with self.assertRaisesRegex(ValueError, "monitor_decision_budget_exceeded"):
            repository.commit_scan(SCAN, decisions, now=MONITOR_NOW)
        self.assertEqual(conn.sql_log, [])

    def test_rowless_update_decision_writes_event_without_outbox(self):
        """observed=false 的 updated：保留观测、只写审计事件，零 outbox。"""
        repository, conn = repository_with_failure(None)
        conn.alerts = [stored_alert()]
        decision = make_decision(previous_status="open", next_status="open",
                                 event_kind="updated", notify=False, observed=False)
        transitions = repository.commit_scan(SCAN, (decision,), now=MONITOR_NOW)
        self.assertEqual([(t.previous_status, t.next_status, t.event_kind)
                          for t in transitions], [("open", "open", "updated")])
        self.assertEqual(conn.counts(), {"runs": 1, "artifacts": 1, "alerts": 1,
                                         "events": 1, "outbox": 0})

    def test_load_policy_maps_rows_through_the_monitor_model(self):
        repository, conn = repository_with_failure(None)
        policy = repository.load_policy(POLICY_REF)
        self.assertEqual((policy.policy_ref, policy.owner_subject_id, policy.enabled),
                         (POLICY_REF, "owner-subject", True))
        self.assertEqual(policy.levels, ("physical_total", "shop_sellable"))
        with self.assertRaisesRegex(ValueError, "monitor_policy_not_found"):
            repository.load_policy("inventory-monitor/404")

    def test_load_policy_returns_disabled_policies_without_failing(self):
        conn = MonitorConn(policies={POLICY_REF: monitor_policy_dict(enabled=False)})
        from bi_agent.monitoring.repository import MonitorRepository

        policy = MonitorRepository(conn).load_policy(POLICY_REF)
        self.assertFalse(policy.enabled)

    def test_load_active_alerts_exposes_only_open_or_acknowledged(self):
        from bi_agent.monitoring.repository import MonitorRepository

        conn = MonitorConn(policies={POLICY_REF: monitor_policy_dict()}, alerts=[
            stored_alert(),
            stored_alert(alert_ref="alert-ack00001", status="acknowledged"),
            stored_alert(alert_ref="alert-done001", status="resolved",
                         resolved_at=SNAPSHOT_AT)])
        alerts = MonitorRepository(conn).load_active_alerts(POLICY_REF)
        self.assertEqual([alert.alert_ref for alert in alerts],
                         ["alert-ack00001", "alert-open0001"])
        self.assertEqual([alert.generation for alert in alerts], [1, 1])
        # Task 3 的状态机需要真实身份才能为保留/退场产出带身份的决策：读取面
        # 按 023 表列把 level/sku_ref/scope_ref 一并带回（2026-09-17 批准的
        # StoredAlert 身份桥，见 models.StoredAlert 的文档字符串）。
        self.assertEqual({(alert.level, alert.sku_ref, alert.scope_ref)
                          for alert in alerts},
                         {("physical_total", SKU_REF, SCOPE_REF)})

    def test_load_alert_history_returns_full_lifecycle_with_identity(self):
        """P1 回归：Task 4 交给状态机的读取面必须是全生命周期历史。

        SQL 侧新首发要求 generation = max(全部历史)+1：resolved/suppressed 终态
        行必须带着真实身份一起回来，否则恢复后再次下降会算出 generation 1，
        整个提交以 monitor_invalid_transition 失败。旧名
        ``load_active_alerts`` 保留原语义（只 open/acknowledged），文档写明。"""
        from bi_agent.monitoring.repository import MonitorRepository

        conn = MonitorConn(policies={POLICY_REF: monitor_policy_dict()}, alerts=[
            stored_alert(),
            stored_alert(alert_ref="alert-ack00001", status="acknowledged"),
            stored_alert(alert_ref="alert-done001", status="resolved",
                         resolved_at=SNAPSHOT_AT),
            stored_alert(alert_ref="alert-gone001", status="suppressed")])
        repository = MonitorRepository(conn)
        history = repository.load_alert_history(POLICY_REF)
        self.assertEqual([alert.alert_ref for alert in history],
                         ["alert-ack00001", "alert-done001", "alert-gone001",
                          "alert-open0001"])
        self.assertEqual([alert.status for alert in history],
                         ["acknowledged", "resolved", "suppressed", "open"])
        self.assertEqual([alert.generation for alert in history], [1, 1, 1, 1])
        # 全历史行都带真实身份桥三列，一行不少。
        self.assertEqual({(alert.level, alert.sku_ref, alert.scope_ref)
                          for alert in history},
                         {("physical_total", SKU_REF, SCOPE_REF)})
        self.assertEqual([alert.status for alert in
                          repository.load_active_alerts(POLICY_REF)],
                         ["acknowledged", "open"])

    def test_database_read_failures_map_to_stable_non_secret_errors(self):
        from bi_agent.monitoring.repository import MonitorRepository

        class BrokenConn(MonitorConn):
            def execute(self, sql, params=None):
                raise psycopg.errors.OperationalError("DSN or network detail")

        repository = MonitorRepository(BrokenConn())
        with self.assertRaisesRegex(RuntimeError, "monitor_policy_read_failed"):
            repository.load_policy(POLICY_REF)
        with self.assertRaisesRegex(RuntimeError, "monitor_alerts_read_failed"):
            repository.load_active_alerts(POLICY_REF)
        repository, _ = repository_with_failure(None)
        conn = repository.conn
        conn.fail_at = None
        conn.execute = lambda sql, params=None: (_ for _ in ()).throw(
            psycopg.errors.OperationalError("secret detail"))
        with self.assertRaises(RuntimeError) as caught:
            repository.commit_scan(SCAN, (make_decision(),), now=MONITOR_NOW)
        self.assertEqual(str(caught.exception), "monitor_commit_failed")
        self.assertNotIn("secret detail", str(caught.exception))


# --- 计划 Task 3：确定性状态机、稳定去重键与事件幂等键 -------------------------

STATE_NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
STATE_SNAPSHOT = STATE_NOW - timedelta(minutes=5)
SHOP1_REF = _shop_ref("shop-1")
SHOP2_REF = _shop_ref("shop-2")
SKU2_REF = _sku_ref("SKU2")
CHANNEL_SCOPE = SHOP1_REF


def canonical_key_oracle(policy_version: str, level: str, sku_ref: str, scope_ref: str,
                         rule_code: str) -> str:
    """计划 Task 3 Step 3 公式的独立实现：钉哈希不借用被测函数本身。"""
    payload = [policy_version, level, sku_ref, scope_ref, rule_code]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def cell_key(*, level: str, sku_ref: str = SKU_REF, scope_ref: str | None = None,
             policy_ref: str = POLICY_REF) -> str:
    from bi_agent.inventory.rules import THRESHOLD_LEVEL_BY_LEVEL

    if scope_ref is None:
        scope_ref = SCOPE_REF if level == "physical_total" else CHANNEL_SCOPE
    return canonical_key_oracle(policy_ref, level, sku_ref, scope_ref,
                                THRESHOLD_LEVEL_BY_LEVEL[level])


def state_policy(*, enabled: bool = True,
                 levels: tuple[str, ...] = ("physical_total", "shop_sellable"),
                 cooldown_seconds: int = 3600):
    from bi_agent.monitoring.models import MonitorPolicy

    return MonitorPolicy(
        policy_ref=POLICY_REF, threshold_policy_ref="inventory-thresholds/1",
        owner_subject_id="owner-subject",
        shop_refs=("shop-a",) if "shop_sellable" in levels else (),
        inventory_pool_refs=(POOL_REF,) if "physical_total" in levels else (),
        levels=levels, cooldown_seconds=cooldown_seconds, enabled=enabled)


def state_row(level: str = "physical_total", status: str = "low",
              sku_ref: str = SKU_REF, scope_ref: str | None = None,
              quantity: str | None = "1", threshold: str | None = "2",
              unit: str = "piece"):
    from bi_agent.monitoring.models import MonitorAlertRow

    if scope_ref is None:
        scope_ref = SCOPE_REF if level == "physical_total" else CHANNEL_SCOPE
    return MonitorAlertRow(level=level, status=status, sku_ref=sku_ref,
                           scope_ref=scope_ref, quantity=quantity,
                           threshold=threshold, unit=unit)


def state_scan(*, fresh: bool = True,
               complete_levels: tuple[str, ...] = ("physical_total",),
               rows: tuple = (), diagnostics: tuple = ()):  # noqa: ANN401
    from bi_agent.monitoring.models import MonitorScan

    return MonitorScan(policy_ref=POLICY_REF, source_artifact_payload={"data": []},
                       source_fingerprint="0" * 64, data_as_of=STATE_SNAPSHOT,
                       fresh=fresh, complete_levels=complete_levels,
                       rows=tuple(rows), diagnostics=tuple(diagnostics))


def stored_state_alert(*, alert_ref: str = "alert-open0001", status: str = "open",
                       generation: int = 1, level: str = "physical_total",
                       sku_ref: str = SKU_REF, scope_ref: str | None = None,
                       key: str | None = None,
                       last_observed_at: datetime = STATE_SNAPSHOT,
                       last_notified_at: datetime | None = STATE_SNAPSHOT):
    from bi_agent.inventory.rules import THRESHOLD_LEVEL_BY_LEVEL
    from bi_agent.monitoring.models import StoredAlert

    if scope_ref is None:
        scope_ref = SCOPE_REF if level == "physical_total" else CHANNEL_SCOPE
    if key is None:
        key = canonical_key_oracle(POLICY_REF, level, sku_ref, scope_ref,
                                   THRESHOLD_LEVEL_BY_LEVEL.get(level, "low_replenish"))
    return StoredAlert(alert_ref=alert_ref, dedupe_key=key, generation=generation,
                       status=status, level=level, sku_ref=sku_ref,
                       scope_ref=scope_ref, last_observed_at=last_observed_at,
                       last_notified_at=last_notified_at)


def decide(scan, *, policy=None, active: tuple = (),
           verified_levels: tuple[str, ...] = ("physical_total", "shop_sellable"),
           now: datetime = STATE_NOW):
    """计划测试里 ``decide(...)`` 速记：默认两层已核验、无历史告警、固定 now。"""
    from bi_agent.monitoring.state_machine import decide_alert_transitions

    return decide_alert_transitions(
        scan, policy=policy or state_policy(), active=tuple(active),
        verified_levels=verified_levels, now=now)


def outbox_decisions(decisions) -> list:
    """outbox 形状：023 只为 notify=true 的 triggered/retriggered/resolved 写投递。"""
    return [d for d in decisions if d.notify
            and d.event_kind in ("triggered", "retriggered", "resolved")]


class AlertStateMachineTests(unittest.TestCase):
    """计划 Task 3 Step 1 的焦点用例（逐字钉方向）+ 身份/闭集/幂等回归。"""

    def test_threshold_equal_triggers_once_then_only_updates(self):
        first = decide(state_scan(rows=[state_row(quantity="10", threshold="10")]))
        self.assertEqual([d.event_kind for d in first], ["triggered"])
        active = tuple(stored_state_alert(status=d.next_status,
                                          generation=d.generation,
                                          key=d.dedupe_key) for d in first)
        second = decide(state_scan(rows=[state_row(quantity="10", threshold="10")]),
                        active=active)
        self.assertEqual([d.event_kind for d in second], ["updated"])
        self.assertFalse(second[0].notify)
        self.assertTrue(second[0].observed)

    def test_only_fresh_complete_recovery_resolves(self):
        for candidate in (state_scan(fresh=False,
                                     rows=[state_row(quantity="11", status="normal")]),
                          state_scan(complete_levels=(),
                                     rows=[state_row(quantity="11", status="normal")])):
            self.assertEqual(decide(candidate, active=(stored_state_alert(),))[0].next_status,
                             "open")
        resolved = decide(state_scan(rows=[state_row(quantity="11", status="normal")]),
                          active=(stored_state_alert(),))
        self.assertEqual(resolved[0].next_status, "resolved")
        self.assertTrue(resolved[0].notify)
        self.assertEqual(outbox_decisions(resolved), list(resolved))

    def test_acknowledged_is_not_resolved_and_new_drop_creates_generation_two(self):
        self.assertEqual(decide(state_scan(rows=[state_row()]),
                                active=(stored_state_alert(status="acknowledged"),))[0].next_status,
                         "acknowledged")
        reopened = decide(state_scan(rows=[state_row()]),
                          active=(stored_state_alert(status="resolved"),))
        self.assertEqual(reopened[0].generation, 2)
        self.assertEqual(reopened[0].event_kind, "triggered")
        self.assertIsNone(reopened[0].previous_status)
        self.assertEqual(outbox_decisions(reopened), list(reopened))

    def test_verified_physical_still_alerts_while_channel_level_is_unverified(self):
        decisions = decide(state_scan(rows=[state_row()]),
                           verified_levels=("physical_total",))
        self.assertEqual([(d.level, d.event_kind, d.next_status) for d in decisions],
                         [("physical_total", "triggered", "open")])
        self.assertFalse(any(d.level == "shop_sellable" for d in decisions))

    def test_unverified_channel_level_emits_no_decisions_and_no_inferred_rows(self):
        # 渠道层未核验又没有历史告警：不产生任何渠道决策。状态机只看 ref 不看数量，
        # “实物 100 不得出现在渠道行里”由四件事钉住：runner 根本没把渠道层发给
        # graph、graph 对被请求而未登记的层级逐行清空数量并置 unsupported、
        # Task 4 runner 按 verified_levels 的第二道行过滤、以及本用例的 ref 断言。
        scan = state_scan(rows=[state_row()])
        decisions = decide(scan, verified_levels=("physical_total",))
        self.assertEqual({d.level for d in decisions}, {"physical_total"})
        self.assertEqual(len(decisions), len(scan.rows))

    def test_withdrawn_channel_source_suppresses_but_never_resolves(self):
        # 渠道层已有 active 告警而本轮不再核验该层 ⇒ 来源/能力退场，只能 suppressed。
        channel_open = stored_state_alert(alert_ref="alert-chan0001",
                                          level="shop_sellable",
                                          scope_ref=SHOP1_REF)
        channel_ack = stored_state_alert(alert_ref="alert-ack00001",
                                         status="acknowledged",
                                         level="shop_sellable",
                                         scope_ref=SHOP2_REF)
        decisions = decide(state_scan(rows=[state_row()]),
                           active=(channel_open, channel_ack),
                           verified_levels=("physical_total",))
        channel = [d for d in decisions if d.level == "shop_sellable"]
        self.assertEqual([(d.next_status, d.event_kind, d.notify) for d in channel],
                         [("suppressed", "updated", False),
                          ("suppressed", "updated", False)])
        self.assertTrue(all(not d.observed for d in channel))
        self.assertFalse(any(d.next_status == "resolved" for d in decisions))
        # 退场抑制保留真实身份：身份逐字来自 StoredAlert，不是占位符。
        self.assertEqual({(d.sku_ref, d.scope_ref) for d in channel},
                         {(channel_open.sku_ref, channel_open.scope_ref),
                          (channel_ack.sku_ref, channel_ack.scope_ref)})

    def test_missing_scan_from_verified_channel_keeps_status_and_cannot_resolve(self):
        # 渠道登记仍在，但本轮没有它的快照 ⇒ 该层不算完整，不解除也不写 outbox。
        channel_open = stored_state_alert(alert_ref="alert-chan0001",
                                          level="shop_sellable",
                                          scope_ref=SHOP1_REF)
        decisions = decide(state_scan(rows=[state_row()]), active=(channel_open,))
        channel = [d for d in decisions if d.level == "shop_sellable"]
        self.assertEqual([(d.next_status, d.event_kind, d.notify) for d in channel],
                         [("open", "updated", False)])
        self.assertFalse(channel[0].observed)  # 没看到就不是刚看到，不推进 last_observed_at
        # 缺扫描的那一层自己零 outbox（实物层的正常首发不受牵连）。
        self.assertEqual([d for d in outbox_decisions(decisions)
                          if d.level == "shop_sellable"], [])
        self.assertEqual((channel[0].sku_ref, channel[0].scope_ref),
                         (channel_open.sku_ref, channel_open.scope_ref))

    def test_withdrawn_retained_and_disabled_alerts_keep_true_identity(self):
        # 2026-09-17 批准的 StoredAlert 身份桥回归：三条保留路径的决策身份都必须
        # 逐字来自告警本身（不同 sku/scope 的告警不得互相串号，也不得出现占位符）。
        other = stored_state_alert(alert_ref="alert-open0002", sku_ref=SKU2_REF,
                                   scope_ref="pl-0123456789ab|wh-ffffffffffff")
        retained = decide(state_scan(complete_levels=(), rows=[state_row()]),
                          active=(other,))[0]
        self.assertEqual((retained.level, retained.sku_ref, retained.scope_ref,
                          retained.generation, retained.previous_status),
                         ("physical_total", SKU2_REF,
                          "pl-0123456789ab|wh-ffffffffffff", 1, "open"))
        suppressed = decide(state_scan(rows=[state_row()]),
                            policy=state_policy(enabled=False), active=(other,))[0]
        self.assertEqual((suppressed.level, suppressed.sku_ref, suppressed.scope_ref,
                          suppressed.next_status, suppressed.notify,
                          suppressed.observed),
                         ("physical_total", SKU2_REF,
                          "pl-0123456789ab|wh-ffffffffffff", "suppressed", False,
                          False))

    def test_repository_history_feeds_generation_two_after_resolved(self):
        """P1 回归：repository 全历史 → resolved 代上的再次下降必须发 generation 2。

        SQL 侧 ``commit_inventory_monitor_scan`` 以全部历史算
        ``history_generation + 1``；若读取面只给 open/acknowledged，这里会算出
        generation 1，整个提交以 monitor_invalid_transition 失败。
        """
        from bi_agent.monitoring.repository import MonitorRepository

        conn = MonitorConn(policies={POLICY_REF: monitor_policy_dict()}, alerts=[
            # 种子历史必须与 state_row() 的格子同键，否则另开一格。
            stored_alert(alert_ref="alert-done0001", status="resolved",
                         dedupe_key=cell_key(level="physical_total"),
                         resolved_at=SNAPSHOT_AT)])
        history = MonitorRepository(conn).load_alert_history(POLICY_REF)
        decisions = decide(state_scan(rows=[state_row()]), active=history)
        self.assertEqual([(decision.generation, decision.event_kind,
                           decision.previous_status) for decision in decisions],
                         [(2, "triggered", None)])
        self.assertEqual(outbox_decisions(decisions), list(decisions))

    def test_repository_history_feeds_generation_two_after_suppressed(self):
        from bi_agent.monitoring.repository import MonitorRepository

        conn = MonitorConn(policies={POLICY_REF: monitor_policy_dict()}, alerts=[
            stored_alert(alert_ref="alert-gone0001", status="suppressed",
                         dedupe_key=cell_key(level="physical_total"))])
        history = MonitorRepository(conn).load_alert_history(POLICY_REF)
        decisions = decide(state_scan(rows=[state_row()]), active=history)
        self.assertEqual([(decision.generation, decision.event_kind)
                          for decision in decisions], [(2, "triggered")])

    def test_generation_two_commit_passes_sql_side_generation_check(self):
        """fakeconn 复刻的 SQL 侧校验：resolved gen1 历史 + generation 2 首发
        → 事件/首发/outbox 各一，绝无 monitor_invalid_transition。"""
        from bi_agent.monitoring.repository import MonitorRepository

        conn = MonitorConn(policies={POLICY_REF: monitor_policy_dict()}, alerts=[
            stored_alert(alert_ref="alert-done0001", status="resolved",
                         dedupe_key=cell_key(level="physical_total"),
                         resolved_at=SNAPSHOT_AT)])
        repository = MonitorRepository(conn)
        history = repository.load_alert_history(POLICY_REF)
        # make_scan() 是载荷契约合法的低库存扫描，格子与种子历史同键。
        scan = make_scan()
        transitions = repository.commit_scan(scan, decide(scan, active=history),
                                             now=MONITOR_NOW)
        self.assertEqual([(transition.event_kind, transition.next_status)
                          for transition in transitions],
                         [("triggered", "open")])
        self.assertEqual(conn.counts(), {"runs": 1, "artifacts": 1, "alerts": 2,
                                         "events": 1, "outbox": 1})

    def test_policy_disabled_ignores_terminal_history_rows(self):
        """策略停用只抑制 open/acknowledged：resolved/suppressed 终态行绝不拿决策。"""
        history = (stored_state_alert(status="resolved"),
                   stored_state_alert(alert_ref="alert-gone0001",
                                      status="suppressed", sku_ref=SKU2_REF,
                                      scope_ref="pl-0123456789ab|wh-ffffffffffff"),
                   stored_state_alert(alert_ref="alert-open0002",
                                      scope_ref="pl-0123456789ab|wh-111111111111"),
                   stored_state_alert(alert_ref="alert-ack00002",
                                      status="acknowledged",
                                      scope_ref="pl-0123456789ab|wh-222222222222"))
        decisions = decide(state_scan(rows=[state_row()]),
                           policy=state_policy(enabled=False), active=history)
        self.assertEqual(sorted((decision.previous_status, decision.next_status)
                                for decision in decisions),
                         [("acknowledged", "suppressed"), ("open", "suppressed")])
        self.assertEqual({decision.event_kind for decision in decisions}, {"updated"})
        self.assertTrue(all(not decision.notify and not decision.observed
                            for decision in decisions))

    def test_incomplete_round_ignores_terminal_history_rows(self):
        """缺页轮的保留只落在活跃告警上；resolved 终态行不拿保留决策。"""
        history = (stored_state_alert(status="resolved"),
                   stored_state_alert(alert_ref="alert-open0002",
                                      scope_ref="pl-0123456789ab|wh-111111111111"))
        decisions = decide(state_scan(complete_levels=(), rows=[state_row()]),
                           active=history)
        self.assertEqual([(decision.previous_status, decision.next_status)
                          for decision in decisions], [("open", "open")])
        self.assertFalse(decisions[0].observed)

    def test_terminal_history_never_receives_decisions(self):
        """fresh+complete 轮里终端历史只参与代际：resolved 格再次下降发新
        triggered；suppressed 格（无行、无 active）零决策，也绝不被抑制/保留。"""
        history = (stored_state_alert(status="resolved"),
                   stored_state_alert(alert_ref="alert-gone0001",
                                      status="suppressed", sku_ref=SKU2_REF,
                                      scope_ref="pl-0123456789ab|wh-ffffffffffff"))
        decisions = decide(state_scan(rows=[state_row()]), active=history)
        self.assertEqual([(decision.generation, decision.event_kind,
                           decision.previous_status) for decision in decisions],
                         [(2, "triggered", None)])
        self.assertNotIn("suppressed", {decision.next_status
                                        for decision in decisions})
        self.assertEqual({decision.sku_ref for decision in decisions}, {SKU_REF})

    def test_cooldown_retriggers_exactly_at_boundary(self):
        inside = stored_state_alert(
            last_notified_at=STATE_NOW - timedelta(seconds=3599))
        decision = decide(state_scan(rows=[state_row()]), active=(inside,))[0]
        self.assertEqual((decision.event_kind, decision.notify, decision.observed),
                         ("updated", False, True))
        at_boundary = stored_state_alert(
            last_notified_at=STATE_NOW - timedelta(seconds=3600))
        decision = decide(state_scan(rows=[state_row()]), active=(at_boundary,))[0]
        self.assertEqual((decision.event_kind, decision.notify, decision.observed),
                         ("retriggered", True, True))
        self.assertEqual(decision.generation, 1)

    def test_never_notified_active_retriggers(self):
        never = stored_state_alert(last_notified_at=None)
        decision = decide(state_scan(rows=[state_row()]), active=(never,))[0]
        self.assertEqual((decision.event_kind, decision.notify), ("retriggered", True))

    def test_duplicate_scan_rows_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "monitor_duplicate_row"):
            decide(state_scan(rows=[state_row(), state_row()]))

    def test_duplicate_active_alerts_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "monitor_duplicate_active_alert"):
            decide(state_scan(rows=[state_row()]),
                   active=(stored_state_alert(alert_ref="alert-a0000001"),
                           stored_state_alert(alert_ref="alert-a0000002")))

    def test_ambiguous_history_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "monitor_ambiguous_history"):
            decide(state_scan(rows=[state_row()]),
                   active=(stored_state_alert(alert_ref="alert-d0000001",
                                              status="resolved"),
                           stored_state_alert(alert_ref="alert-d0000002",
                                              status="resolved")))

    def test_naive_now_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "monitor_now_naive"):
            decide(state_scan(rows=[state_row()]), now=STATE_NOW.replace(tzinfo=None))

    def test_scan_policy_ref_mismatch_fail_closed(self):
        scan = state_scan(rows=[state_row()])
        mismatched = scan.model_copy(update={"policy_ref": "inventory-monitor/2"})
        with self.assertRaisesRegex(ValueError, "monitor_policy_ref_mismatch"):
            decide(mismatched)

    def test_dedupe_key_matches_canonical_json_oracle_and_is_stable(self):
        from bi_agent.monitoring.state_machine import dedupe_key

        kwargs = dict(policy_version=POLICY_REF, level="physical_total",
                      sku_ref=SKU_REF, scope_ref=SCOPE_REF, rule_code="low_replenish")
        value = dedupe_key(**kwargs)
        self.assertEqual(value, canonical_key_oracle(**kwargs))
        self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertEqual(value, dedupe_key(**kwargs))

    def test_dedupe_key_changes_only_with_its_five_members(self):
        from bi_agent.monitoring.state_machine import dedupe_key

        base = dict(policy_version=POLICY_REF, level="physical_total",
                    sku_ref=SKU_REF, scope_ref=SCOPE_REF, rule_code="low_replenish")
        baseline = dedupe_key(**base)
        for changed in (dict(base, policy_version="inventory-monitor/1.1"),
                        dict(base, sku_ref=SKU2_REF),
                        dict(base, scope_ref="pl-0123456789cd|wh-0123456789ab"),
                        dict(base, rule_code="low_quota"),
                        # 作用域形状与层级绑定，层级的换键只能连同自己的作用域：
                        # 键仍不同（level 成员在 canonical JSON 里）。
                        dict(base, level="shop_sellable", scope_ref=CHANNEL_SCOPE,
                             rule_code="low_quota")):
            with self.subTest(changed=str(changed)):
                self.assertNotEqual(dedupe_key(**changed), baseline)

    def test_dedupe_key_rejects_inputs_outside_closed_shapes(self):
        from bi_agent.monitoring.state_machine import dedupe_key

        base = dict(policy_version=POLICY_REF, level="physical_total",
                    sku_ref=SKU_REF, scope_ref=SCOPE_REF, rule_code="low_replenish")
        for bad in (dict(base, policy_version="inventory-monitor/"),
                    dict(base, policy_version="other/1"),
                    dict(base, policy_version="inventory-monitor/-x"),
                    dict(base, level="store_stock"),
                    dict(base, sku_ref="SKU1"),
                    dict(base, sku_ref="ent-XYZ"),
                    dict(base, scope_ref=""),
                    dict(base, scope_ref=" "),
                    dict(base, scope_ref="pool-a|wh-a"),
                    dict(base, scope_ref="pl-0123456789ab"),
                    dict(base, rule_code="Low_Replenish"),
                    dict(base, rule_code="")):
            with self.subTest(bad=str(bad)):
                with self.assertRaisesRegex(ValueError, "monitor_dedupe_"):
                    dedupe_key(**bad)
        # 渠道作用域 = 店铺 ref：同键函数按层级收自己的作用域形状。
        dedupe_key(**dict(base, level="shop_sellable", scope_ref=CHANNEL_SCOPE,
                          rule_code="low_quota"))

    def test_event_idempotency_key_matches_sql_formula_and_is_stable(self):
        from bi_agent.monitoring.state_machine import event_idempotency_key

        key = cell_key(level="physical_total")
        expected = hashlib.sha256(
            f"{key}:1:triggered:{SOURCE_FINGERPRINT}".encode()).hexdigest()
        self.assertEqual(event_idempotency_key(key, 1, "triggered",
                                               SOURCE_FINGERPRINT), expected)
        self.assertEqual(event_idempotency_key(key, 1, "triggered",
                                               SOURCE_FINGERPRINT),
                         event_idempotency_key(key, 1, "triggered",
                                               SOURCE_FINGERPRINT))
        self.assertNotEqual(event_idempotency_key(key, 1, "resolved",
                                                  SOURCE_FINGERPRINT), expected)
        self.assertNotEqual(event_idempotency_key(key, 2, "triggered",
                                                  SOURCE_FINGERPRINT), expected)
        for bad in (("XYZ", 1, "triggered", SOURCE_FINGERPRINT),
                    (key, 0, "triggered", SOURCE_FINGERPRINT),
                    (key, 1, "escalated", SOURCE_FINGERPRINT),
                    (key, 1, "triggered", "zz")):
            with self.subTest(bad=str(bad)):
                with self.assertRaisesRegex(ValueError, "monitor_event_"):
                    event_idempotency_key(*bad)

    def test_unverifiable_or_negative_low_row_never_triggers(self):
        # "low" 状态但 Decimal 复核做不了（0.5 件不符整数精度）或数量为负：
        # fail closed——不首发，有 active 告警时只保留且 observed=false。
        for quantity in ("0.5", "-1"):
            with self.subTest(quantity=quantity):
                scan = state_scan(rows=[state_row(quantity=quantity)])
                self.assertEqual(decide(scan), ())
                retained = decide(scan, active=(stored_state_alert(),))[0]
                self.assertEqual((retained.event_kind, retained.observed,
                                  retained.notify), ("updated", False, False))

    def test_decisions_are_independent_of_input_order(self):
        rows = (state_row(),
                state_row(level="shop_sellable", quantity="1", threshold="5"),
                state_row(sku_ref=SKU2_REF, quantity="0", threshold="4"))
        active = (stored_state_alert(status="acknowledged"),
                  stored_state_alert(alert_ref="alert-chan0001",
                                     level="shop_sellable"))
        baseline = decide(state_scan(rows=rows), active=active)
        flipped = decide(state_scan(rows=tuple(reversed(rows))),
                         active=tuple(reversed(active)))
        self.assertEqual(baseline, flipped)
        # 层内按 (sku_ref, scope_ref) 排序：ent-1b9814cb 先于 ent-efd0f37a。
        self.assertEqual([d.event_kind for d in baseline],
                         ["triggered", "updated", "updated"])

    def test_stored_alert_carries_strict_identity(self):
        from pydantic import ValidationError

        alert = stored_state_alert()
        self.assertEqual((alert.level, alert.sku_ref, alert.scope_ref),
                         ("physical_total", SKU_REF, SCOPE_REF))
        for bad in (dict(level="store_stock"), dict(sku_ref=""),
                    dict(scope_ref=" "), dict(status="closed")):
            with self.subTest(bad=str(bad)):
                with self.assertRaises(ValidationError):
                    stored_state_alert(**bad)


class AlertTransitionGoldTests(unittest.TestCase):
    """Task 3 Step 5 的 gold 转移矩阵：fixture 逐字段钉死，反序输入不变。

    每条 case 固定 previous/scan/verified_levels/now/expected decisions/outbox
    count；缺数据/保留/退场用例的 outbox 计数全为 0。dedupe key 是字面钉值，
    改动 canonical 形状当场转红。
    """

    @staticmethod
    def _cases():
        path = pathlib.Path(__file__).parent / "fixtures" / \
            "inventory_monitor_transitions.json"
        return json.loads(path.read_text(encoding="utf-8"))["cases"]

    @staticmethod
    def _decide(case, *, scan=None, active=None):
        from bi_agent.monitoring.models import (MonitorAlertRow, MonitorPolicy,
                                                MonitorScan, StoredAlert)
        from bi_agent.monitoring.state_machine import decide_alert_transitions

        policy = MonitorPolicy.model_validate(case["policy"])
        if scan is None:
            scan_data = case["scan"]
            scan = MonitorScan(
                policy_ref=policy.policy_ref, source_artifact_payload={"data": []},
                source_fingerprint="0" * 64, data_as_of=STATE_SNAPSHOT,
                fresh=scan_data["fresh"],
                complete_levels=tuple(scan_data["complete_levels"]),
                rows=tuple(MonitorAlertRow.model_validate(row)
                           for row in scan_data["rows"]),
                diagnostics=tuple(scan_data["diagnostics"]))
        if active is None:
            active = tuple(StoredAlert.model_validate(alert)
                           for alert in case["active"])
        return decide_alert_transitions(
            scan, policy=policy, active=active,
            verified_levels=tuple(case["verified_levels"]),
            now=datetime.fromisoformat(case["now"]))

    def test_every_gold_case_matches_field_by_field(self):
        for case in self._cases():
            with self.subTest(case=case["name"]):
                if case["expect_error"] is not None:
                    with self.assertRaisesRegex(ValueError, case["expect_error"]):
                        self._decide(case)
                    continue
                decisions = self._decide(case)
                self.assertEqual([d.model_dump(mode="json") for d in decisions],
                                 case["expected_decisions"])
                self.assertEqual(len(outbox_decisions(decisions)),
                                 case["expected_outbox_count"])
                # 缺失/保留/退场永不产生用户事件：updated 决策一律 notify=False。
                self.assertTrue(all(not d.notify for d in decisions
                                    if d.event_kind == "updated"))

    def test_gold_outcomes_survive_reversed_input_order(self):
        from bi_agent.monitoring.models import StoredAlert

        for case in self._cases():
            if case["expect_error"] is not None:
                continue
            with self.subTest(case=case["name"]):
                baseline = self._decide(case)
                flipped_active = tuple(reversed(
                    [StoredAlert.model_validate(alert) for alert in case["active"]]))
                flipped = self._decide(case, scan=self._flipped_scan_rows(case),
                                       active=flipped_active)
                self.assertEqual(baseline, flipped)
                self.assertEqual([d.model_dump(mode="json") for d in flipped],
                                 case["expected_decisions"])

    def _flipped_scan_rows(self, case):
        from bi_agent.monitoring.models import MonitorAlertRow, MonitorScan

        scan_data = case["scan"]
        scan = MonitorScan(
            policy_ref=case["policy"]["policy_ref"],
            source_artifact_payload={"data": []}, source_fingerprint="0" * 64,
            data_as_of=STATE_SNAPSHOT, fresh=scan_data["fresh"],
            complete_levels=tuple(scan_data["complete_levels"]),
            rows=tuple(reversed([MonitorAlertRow.model_validate(row)
                                 for row in scan_data["rows"]])),
            diagnostics=tuple(scan_data["diagnostics"]))
        return scan


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
