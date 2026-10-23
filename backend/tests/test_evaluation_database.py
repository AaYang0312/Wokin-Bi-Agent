"""T1（计划 2026-10-24）：评测沙箱连接的硬性边界。

两类用例：
1. 纯函数 ``parse_evaluation_dsn``：非本机地址、错误数据库名（含既有
   ``bi_agent_test`` 与生产名 ``bi_agent``）在任何连接建立**之前**就被拒绝，
   拒绝信息不含 DSN 或密码；URL 与 key/value 两种写法都走 psycopg 的解析。
2. 活库用例（配置 ``BI_EVALUATION_ADMIN_DSN`` 才跑）：真实连接后校验数据库名、
   主机与沙箱标记；标记缺失的库不允许进入种子路径。
"""

from __future__ import annotations

import os
import unittest

from tests.evaluation.database import (
    EVALUATION_DB_NAME,
    EvaluationTargetError,
    parse_evaluation_dsn,
)


class ParseEvaluationDsnTests(unittest.TestCase):
    """拒绝必须发生在任何网络连接之前：解析是纯函数。"""

    def test_url_dsn_local_eval_db_is_allowed(self):
        params = parse_evaluation_dsn(
            "postgresql://postgres:secret@127.0.0.1:55439/bi_agent_eval_test")
        self.assertEqual(params["dbname"], EVALUATION_DB_NAME)
        self.assertEqual(params["host"], "127.0.0.1")
        self.assertEqual(params["port"], 55439)

    def test_url_dsn_localhost_variant_allowed(self):
        params = parse_evaluation_dsn(
            "postgresql://postgres:secret@localhost:55439/bi_agent_eval_test")
        self.assertEqual(params["host"], "localhost")

    def test_remote_host_rejected(self):
        with self.assertRaises(EvaluationTargetError) as caught:
            parse_evaluation_dsn(
                "postgresql://postgres:secret@10.1.2.3:5432/bi_agent_eval_test")
        self.assertNotIn("secret", str(caught.exception))

    def test_remote_host_in_keyvalue_form_rejected(self):
        with self.assertRaises(EvaluationTargetError):
            parse_evaluation_dsn(
                "host=db.internal.example port=5432 "
                f"dbname={EVALUATION_DB_NAME} user=postgres password=secret")

    def test_existing_shared_test_db_rejected(self):
        # 既有 bi_agent_test 承载过真实导入与同步数据，库名带 _test 也不够。
        with self.assertRaises(EvaluationTargetError):
            parse_evaluation_dsn(
                "postgresql://postgres:secret@127.0.0.1:54329/bi_agent_test")

    def test_production_db_name_rejected(self):
        with self.assertRaises(EvaluationTargetError):
            parse_evaluation_dsn(
                "postgresql://postgres:secret@127.0.0.1:54329/bi_agent")

    def test_lookalike_eval_name_rejected(self):
        with self.assertRaises(EvaluationTargetError):
            parse_evaluation_dsn(
                "postgresql://postgres:secret@127.0.0.1:55439/bi_agent_eval")

    def test_keyvalue_form_allowed_and_parsed(self):
        params = parse_evaluation_dsn(
            f"host=127.0.0.1 port=55439 dbname={EVALUATION_DB_NAME} "
            "user=postgres password=secret")
        self.assertEqual(params["dbname"], EVALUATION_DB_NAME)
        self.assertEqual(params["host"], "127.0.0.1")

    def test_keyvalue_form_wrong_db_rejected(self):
        with self.assertRaises(EvaluationTargetError):
            parse_evaluation_dsn(
                "host=127.0.0.1 port=55439 dbname=bi_agent_test "
                "user=postgres password=secret")

    def test_percent_encoded_password_survives_real_parser(self):
        # psycopg 负责解码，不是手写 string 匹配——密码里带保留字符也要原样通过。
        params = parse_evaluation_dsn(
            "postgresql://postgres:p%40ss%2Fword@127.0.0.1:55439/"
            f"{EVALUATION_DB_NAME}")
        self.assertEqual(params["password"], "p@ss/word")

    def test_unix_socket_without_host_rejected(self):
        # 不写 host 时 psycopg 默认连本机 socket，但目标库校验仍必须成立；
        # 若没有 dbname（psycopg 默认取用户名），一律拒绝。
        with self.assertRaises(EvaluationTargetError):
            parse_evaluation_dsn("user=postgres password=secret")


@unittest.skipUnless(os.getenv("BI_EVALUATION_ADMIN_DSN"), "未配置评测沙箱 DSN")
class ConnectEvaluationDbTests(unittest.TestCase):
    """活库路径：先验身份，再放行种子。"""

    def test_connects_and_confirms_identity(self):
        from tests.evaluation.database import connect_evaluation_db

        conn = connect_evaluation_db(os.environ["BI_EVALUATION_ADMIN_DSN"])
        self.addCleanup(conn.close)
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            self.assertEqual(cursor.fetchone()[0], EVALUATION_DB_NAME)

    def test_sandbox_marker_present(self):
        from tests.evaluation.database import connect_evaluation_db, sandbox_marker

        conn = connect_evaluation_db(os.environ["BI_EVALUATION_ADMIN_DSN"])
        self.addCleanup(conn.close)
        marker = sandbox_marker(conn)
        self.assertEqual(marker["label"], "bi-agent-evaluation-sandbox")

    def test_wrong_database_on_same_instance_rejected(self):
        from tests.evaluation.database import connect_evaluation_db

        admin = os.environ["BI_EVALUATION_ADMIN_DSN"]
        wrong = admin.replace(f"/{EVALUATION_DB_NAME}", "/postgres")
        with self.assertRaises(EvaluationTargetError) as caught:
            connect_evaluation_db(wrong)
        self.assertNotIn("postgres:", str(caught.exception))

    def test_initialize_refuses_on_already_initialized_instance(self):
        # 同名已有资源不自动重建：标记在，初始化就必须拒绝。
        from tests.evaluation.database import (
            initialize_evaluation_db, parse_evaluation_dsn)

        params = parse_evaluation_dsn(os.environ["BI_EVALUATION_ADMIN_DSN"])
        with self.assertRaises(EvaluationTargetError):
            initialize_evaluation_db(params)


if __name__ == "__main__":
    unittest.main()
