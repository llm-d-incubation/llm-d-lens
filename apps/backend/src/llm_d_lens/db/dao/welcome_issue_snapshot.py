"""DAO for the single-row Welcome issue snapshot."""

from __future__ import annotations

from sqlalchemy.exc import OperationalError, ProgrammingError

from llm_d_lens.dashboard.contracts import WelcomeIssueSnapshot
from llm_d_lens.db.dao.base import BaseDao
from llm_d_lens.db.models.welcome_issue_snapshot import WelcomeIssueSnapshotRow

_CURRENT_ID = "current"


class WelcomeIssueSnapshotDao(BaseDao):
    def get_current(self) -> WelcomeIssueSnapshot | None:
        try:
            with self._read_only() as session:
                row = session.get(WelcomeIssueSnapshotRow, _CURRENT_ID)
                return row.to_dto() if row else None
        except (OperationalError, ProgrammingError) as exc:
            if _is_missing_table_error(exc):
                return None
            raise

    def put(self, snapshot: WelcomeIssueSnapshot) -> WelcomeIssueSnapshot:
        with self._transaction() as session:
            row = session.get(WelcomeIssueSnapshotRow, _CURRENT_ID)
            if row is None:
                row = WelcomeIssueSnapshotRow.from_dto(snapshot, id=_CURRENT_ID)
                session.add(row)
            else:
                row.sync_from_dto(snapshot, id=_CURRENT_ID)
            session.flush()
            return row.to_dto()


def _is_missing_table_error(exc: OperationalError | ProgrammingError) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return "does not exist" in message or "no such table" in message
