from sqlalchemy.dialects import postgresql
import pytest

from app.agent2.case_followup_metrics import load_case_followup_metrics


class _Rows:
    def all(self): return []


class _Session:
    def __init__(self): self.statements = []
    async def scalars(self, statement):
        self.statements.append(statement)
        return _Rows()


@pytest.mark.asyncio
async def test_followup_metrics_are_empty_safe_and_every_query_is_tenant_fenced():
    session = _Session()

    result = await load_case_followup_metrics(
        session, tenant_id="tenant-a", user_id="user-1"
    )

    assert all(value == 0 for value in result["metrics"].values())
    assert "duplicate_followups_prevented" in result["evidence_unavailable"]
    for statement in session.statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        assert "tenant_id" in str(compiled)
        assert "tenant-a" in compiled.params.values()
