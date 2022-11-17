import json
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlalchemy import TEXT, Column
from sqlmodel import Field, Relationship

from zenml.enums import ExecutionStatus
from zenml.models import PipelineRunResponseModel
from zenml.zen_stores.schemas.base_schemas import NamedSchema
from zenml.zen_stores.schemas.pipeline_schemas import PipelineSchema
from zenml.zen_stores.schemas.project_schemas import ProjectSchema
from zenml.zen_stores.schemas.schema_utils import build_foreign_key_field
from zenml.zen_stores.schemas.stack_schemas import StackSchema
from zenml.zen_stores.schemas.user_schemas import UserSchema

if TYPE_CHECKING:
    from zenml.models import PipelineRunUpdateModel


class PipelineRunSchema(NamedSchema, table=True):
    """SQL Model for pipeline runs."""

    __tablename__ = "pipeline_run"

    pipeline_configuration: str = Field(sa_column=Column(TEXT, nullable=False))
    num_steps: Optional[int]

    zenml_version: str
    git_sha: Optional[str] = Field(nullable=True)
    mlmd_id: Optional[int] = Field(default=None, nullable=True)
    stack_id: Optional[UUID] = build_foreign_key_field(
        source=__tablename__,
        target=StackSchema.__tablename__,
        source_column="stack_id",
        target_column="id",
        ondelete="SET NULL",
        nullable=True,
    )
    stack: "StackSchema" = Relationship(back_populates="runs")

    pipeline_id: Optional[UUID] = build_foreign_key_field(
        source=__tablename__,
        target=PipelineSchema.__tablename__,
        source_column="pipeline_id",
        target_column="id",
        ondelete="SET NULL",
        nullable=True,
    )
    pipeline: "PipelineSchema" = Relationship(back_populates="runs")

    user_id: Optional[UUID] = build_foreign_key_field(
        source=__tablename__,
        target=UserSchema.__tablename__,
        source_column="user_id",
        target_column="id",
        ondelete="SET NULL",
        nullable=True,
    )
    user: "UserSchema" = Relationship(back_populates="runs")

    project_id: UUID = build_foreign_key_field(
        source=__tablename__,
        target=ProjectSchema.__tablename__,
        source_column="project_id",
        target_column="id",
        ondelete="CASCADE",
        nullable=False,
    )
    project: "ProjectSchema" = Relationship(back_populates="runs")

    orchestrator_run_id: Optional[str] = Field(nullable=True)

    status: ExecutionStatus

    def to_model(
        self, _block_recursion: bool = False
    ) -> PipelineRunResponseModel:
        """Convert a `PipelineRunSchema` to a `PipelineRunResponseModel`.

        Returns:
            The created `PipelineRunResponseModel`.
        """
        if _block_recursion:
            return PipelineRunResponseModel(
                id=self.id,
                name=self.name,
                stack=self.stack.to_model() if self.stack else None,
                project=self.project.to_model(),
                user=self.user.to_model(),
                orchestrator_run_id=self.orchestrator_run_id,
                status=self.status,
                pipeline_configuration=json.loads(self.pipeline_configuration),
                num_steps=self.num_steps,
                git_sha=self.git_sha,
                zenml_version=self.zenml_version,
                mlmd_id=self.mlmd_id,
                created=self.created,
                updated=self.updated,
            )
        else:
            return PipelineRunResponseModel(
                id=self.id,
                name=self.name,
                stack=self.stack.to_model() if self.stack else None,
                project=self.project.to_model(),
                user=self.user.to_model(),
                orchestrator_run_id=self.orchestrator_run_id,
                status=self.status,
                pipeline=(
                    self.pipeline.to_model(not _block_recursion)
                    if self.pipeline
                    else None
                ),
                pipeline_configuration=json.loads(self.pipeline_configuration),
                num_steps=self.num_steps,
                git_sha=self.git_sha,
                zenml_version=self.zenml_version,
                mlmd_id=self.mlmd_id,
                created=self.created,
                updated=self.updated,
            )

    def update(self, run_update: "PipelineRunUpdateModel"):
        """ """
        if run_update.mlmd_id:
            self.mlmd_id = run_update.mlmd_id

        if run_update.status:
            self.status = run_update.status

        self.updated = datetime.now()
