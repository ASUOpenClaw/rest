import enum
import uuid

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDMixin


class IndexingStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class FileSecurityMode(str, enum.Enum):
    # Access is determined solely by workspace role (default)
    role = "role"
    # Access is determined by explicit per-user entries in file_permissions.
    # owner and admin always have full access regardless.
    per_user = "per_user"


class File(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "files"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("folders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    security_mode: Mapped[FileSecurityMode] = mapped_column(
        Enum(FileSecurityMode, name="file_security_mode"),
        nullable=False,
        default=FileSecurityMode.role,
        index=True,
    )
    indexing_status: Mapped[IndexingStatus] = mapped_column(
        Enum(IndexingStatus, name="indexing_status"),
        nullable=False,
        default=IndexingStatus.pending,
        index=True,
    )
    indexing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_indexed_at: Mapped[str | None] = mapped_column(nullable=True)
    file_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    workspace: Mapped["Workspace"] = relationship(
        "Workspace", back_populates="files"
    )  # noqa: F821
    folder: Mapped["Folder | None"] = relationship(
        "Folder", back_populates="files"
    )  # noqa: F821
    uploader: Mapped["User"] = relationship(
        "User", foreign_keys=[uploaded_by]
    )  # noqa: F821
    permissions: Mapped[list["FilePermission"]] = relationship(  # noqa: F821
        "FilePermission", back_populates="file", cascade="all, delete-orphan"
    )
