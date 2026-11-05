"""agent-compute-credit 卡2：schema v15 模型层契约。"""

from yuxi.storage.postgres import manager as schema_manager
from yuxi.storage.postgres.models_business import AgentRun, CreditReportFailed


def test_agent_run_has_credits_li_column():
    col = AgentRun.__table__.c.credits_li
    assert col.nullable is True


def test_credit_report_failed_table():
    assert CreditReportFailed.__tablename__ == "credit_report_failed"
    names = {c.name for c in CreditReportFailed.__table__.columns}
    assert {"id", "run_id", "payload", "error", "created_at"} <= names


def test_schema_version_and_statements():
    """v15 升级契约：版本号与升级语句存在（升级方法本体由 require_current_schema 集成路径覆盖）。"""
    assert schema_manager.BUSINESS_SCHEMA_VERSION == 15
    # 现场核对（Task 2.0）：升级方法是 pg_manager 单例的实例方法，分发处经实例调用。
    assert hasattr(schema_manager.pg_manager, "upgrade_business_schema_v14_to_v15")
    joined = " ".join(schema_manager.CREDIT_REPORT_SCHEMA_STATEMENTS)
    assert "credits_li" in joined and "credit_report_failed" in joined
