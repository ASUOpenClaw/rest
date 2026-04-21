from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDMixin


class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Null for OAuth-only users; set when the user registers with email+password.
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Set when user was invited by email but hasn't authenticated yet.
    # Cleared and merged on first OAuth login matching this email.
    invite_email: Mapped[str | None] = mapped_column(
        String(256), nullable=True, index=True
    )

    oauth_accounts: Mapped[list["OAuthAccount"]] = relationship(  # noqa: F821
        "OAuthAccount", back_populates="user", cascade="all, delete-orphan"
    )
    workspace_members: Mapped[list["WorkspaceMember"]] = relationship(  # noqa: F821
        "WorkspaceMember", back_populates="user", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list["ApiKey"]] = relationship(  # noqa: F821
        "ApiKey", back_populates="user", cascade="all, delete-orphan"
    )
