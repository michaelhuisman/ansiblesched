from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.models.run import RunStatus


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("must be a relative path without '..'")
    return value


RelPath = Annotated[str, AfterValidator(_relative_path)]
Name = Annotated[str, Field(min_length=1, max_length=200)]
NonEmpty = Annotated[str, Field(min_length=1)]
# Extra vars zijn vrije JSON voor Ansible.
ExtraVars = dict[str, Any]


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- credentials -----------------------------------------------------------


class CredentialIn(BaseModel):
    name: Name
    type: Literal["ssh_key", "vault_password", "git_token"]
    openbao_path: NonEmpty
    openbao_key: NonEmpty


class CredentialOut(CredentialIn, OrmModel):
    id: int
    created_at: datetime


# --- projects --------------------------------------------------------------


class ProjectIn(BaseModel):
    name: Name
    git_url: NonEmpty
    branch: NonEmpty = "main"
    credential_id: int | None = None


class ProjectOut(ProjectIn, OrmModel):
    id: int
    created_at: datetime
    updated_at: datetime


# --- inventories -----------------------------------------------------------


class InventoryIn(BaseModel):
    name: Name
    project_id: int | None = None
    source_type: Literal["project_file", "inline"]
    path: RelPath | None = None
    content: str | None = None

    @model_validator(mode="after")
    def _check_source(self) -> Self:
        if self.source_type == "project_file":
            if self.path is None or self.project_id is None:
                raise ValueError("project_file requires path and project_id")
            if self.content is not None:
                raise ValueError("project_file does not take content")
        else:
            if self.content is None:
                raise ValueError("inline requires content")
            if self.path is not None:
                raise ValueError("inline does not take path")
        return self


class InventoryOut(InventoryIn, OrmModel):
    id: int
    created_at: datetime
    updated_at: datetime


# --- templates -------------------------------------------------------------


class TemplateIn(BaseModel):
    name: Name
    project_id: int
    playbook_path: RelPath
    inventory_id: int
    extra_vars: ExtraVars = Field(default_factory=dict)
    limit: str | None = None
    tags: str | None = None
    skip_tags: str | None = None
    verbosity: int = Field(default=0, ge=0, le=5)
    machine_credential_id: int
    vault_credential_id: int | None = None
    timeout_s: int | None = Field(default=None, gt=0)


class TemplateOut(TemplateIn, OrmModel):
    id: int
    created_at: datetime
    updated_at: datetime


# --- runs ------------------------------------------------------------------


class LaunchIn(BaseModel):
    extra_vars: ExtraVars = Field(default_factory=dict)
    limit: str | None = None


class RunOut(OrmModel):
    id: int
    template_id: int
    triggered_by: str
    status: RunStatus
    extra_vars: ExtraVars
    limit: str | None
    commit_sha: str | None
    worker_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    rc: int | None
    status_reason: str | None
    stats: dict[str, Any] | None


class RunEventOut(OrmModel):
    seq: int
    event: str
    host: str | None
    task: str | None
    created_at: datetime
    stdout: str | None
    data: dict[str, Any]


class RunEventsPage(BaseModel):
    items: list[RunEventOut]
    next_after_seq: int | None
