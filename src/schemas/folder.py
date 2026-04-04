import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class BreadcrumbItem(BaseModel):
    id: uuid.UUID
    name: str


class CreatedByRef(BaseModel):
    id: uuid.UUID
    display_name: str


class FolderOut(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    path: str
    workspace_id: uuid.UUID
    files_count: int
    children_count: int
    created_by: CreatedByRef | None
    created_at: datetime


class FolderDetailOut(FolderOut):
    breadcrumbs: list[BreadcrumbItem]


class FolderListItem(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    path: str
    files_count: int
    children_count: int
    created_at: datetime


class FolderParentRef(BaseModel):
    id: uuid.UUID
    name: str
    path: str


class FolderListOut(BaseModel):
    parent: FolderParentRef | None
    items: list[FolderListItem]
    total: int


class FolderTreeNode(BaseModel):
    id: uuid.UUID
    name: str
    files_count: int
    children: list["FolderTreeNode"] = []


FolderTreeNode.model_rebuild()


class FolderTreeOut(BaseModel):
    tree: list[FolderTreeNode]
    total_folders: int
    total_files: int


class FolderCreateRequest(BaseModel):
    name: str
    parent_id: uuid.UUID | None = None


class FolderPatchRequest(BaseModel):
    name: str | None = None
    parent_id: uuid.UUID | None = (
        None  # use sentinel to distinguish "not set" vs explicit null
    )
    move_to_root: bool = False  # set true to explicitly move to root (parent_id=null)
