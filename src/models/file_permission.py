import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin


class FilePermissionLevel(str, enum.Enum):
    none = "none"  # user cannot see the file at all (excluded from RAG too)
    read = "read"  # view metadata, download, included in RAG
    write = "write"  # read + edit metadata, re-upload/replace, delete


class FilePermission(Base, UUIDMixin):
    __tablename__ = "file_permissions"
    __table_args__ = (
        UniqueConstraint("file_id", "user_id", name="uq_file_permission_user"),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    permission: Mapped[FilePermissionLevel] = mapped_column(
        Enum(FilePermissionLevel, name="file_permission_level"), nullable=False
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    file: Mapped["File"] = relationship(
        "File", back_populates="permissions"
    )  # noqa: F821
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])  # noqa: F821
    grantor: Mapped["User | None"] = relationship(
        "User", foreign_keys=[granted_by]
    )  # noqa: F821
