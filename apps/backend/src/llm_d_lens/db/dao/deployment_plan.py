"""DAO for immutable plans and one-time, transactional Run creation."""

from sqlalchemy import select

from llm_d_lens.db.dao.base import BaseDao
from llm_d_lens.db.models.deployment_batch import DeploymentRunRow
from llm_d_lens.db.models.deployment_job import DeploymentCaseRow
from llm_d_lens.db.models.deployment_plan import DeploymentPlanRow
from llm_d_lens.deploy.contracts import DeploymentPreparedPlan, DeploymentRun


class DeploymentPlanDao(BaseDao):
    def create(self, plan: DeploymentPreparedPlan) -> DeploymentPreparedPlan:
        with self._transaction() as session:
            row = DeploymentPlanRow.from_dto(plan)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, plan_id: str) -> DeploymentPreparedPlan | None:
        with self._read_only() as session:
            row = session.get(DeploymentPlanRow, plan_id)
            return row.to_dto() if row else None

    def consume(self, plan_id: str, digest: str, run: DeploymentRun) -> str:
        with self._transaction() as session:
            row = session.scalars(
                select(DeploymentPlanRow).where(DeploymentPlanRow.id == plan_id).with_for_update()
            ).one_or_none()
            if row is None or row.digest != digest:
                raise ValueError("Deployment plan is missing or its digest changed")
            if row.run_id:
                return row.run_id
            batch = DeploymentRunRow.from_dto(
                run, cluster_id=row.cluster_id, owner_user_id=row.owner_user_id, owner_group_id=row.owner_group_id
            )
            batch.jobs = [DeploymentCaseRow.from_dto(case, batch_id=run.id) for case in run.cases]
            session.add(batch)
            row.status, row.run_id = "consumed", run.id
            session.flush()
            return run.id
