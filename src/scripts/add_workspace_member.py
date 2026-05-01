#!/usr/bin/env python
"""
Add a user to a workspace by email and workspace name.

Usage (from rest/ directory):
    uv run python scripts/add_workspace_member.py --email user@example.com --workspace "my-workspace"
    uv run python scripts/add_workspace_member.py --email user@example.com --workspace "my-workspace" --role admin
"""

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole


async def add_member(email: str, workspace_name: str, role: WorkspaceRole) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        user_result = await db.execute(select(User).where(User.email == email))
        user = user_result.scalar_one_or_none()
        if user is None:
            print(f"Error: no user found with email '{email}'.", file=sys.stderr)
            await engine.dispose()
            sys.exit(1)

        ws_result = await db.execute(
            select(Workspace).where(Workspace.name == workspace_name)
        )
        workspaces = ws_result.scalars().all()
        if not workspaces:
            print(f"Error: no workspace named '{workspace_name}'.", file=sys.stderr)
            await engine.dispose()
            sys.exit(1)
        if len(workspaces) > 1:
            print(
                f"Error: multiple workspaces named '{workspace_name}':",
                file=sys.stderr,
            )
            for ws in workspaces:
                print(f"  {ws.id}  {ws.name}", file=sys.stderr)
            print("Use --workspace-id to target a specific workspace.", file=sys.stderr)
            await engine.dispose()
            sys.exit(1)

        workspace = workspaces[0]

        existing = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == user.id,
            )
        )
        member = existing.scalar_one_or_none()

        if member is not None:
            old_role = member.role
            member.role = role
            await db.commit()
            print(
                f"Updated existing member {user.email} in '{workspace.name}': "
                f"{old_role} → {role}"
            )
        else:
            if role == WorkspaceRole.owner:
                print(
                    "Error: cannot assign 'owner' — a workspace already has exactly one owner.",
                    file=sys.stderr,
                )
                await engine.dispose()
                sys.exit(1)

            new_member = WorkspaceMember(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                user_id=user.id,
                role=role,
            )
            db.add(new_member)
            try:
                await db.commit()
            except IntegrityError as e:
                await db.rollback()
                print(f"Error: database constraint violated: {e.orig}", file=sys.stderr)
                await engine.dispose()
                sys.exit(1)

            print(
                f"Added {user.email} (id={user.id}) to workspace '{workspace.name}' "
                f"(id={workspace.id}) as {role}"
            )

    await engine.dispose()


def main() -> None:
    roles = [r.value for r in WorkspaceRole if r != WorkspaceRole.owner]

    parser = argparse.ArgumentParser(description="Add a user to a workspace")
    parser.add_argument("--email", required=True, help="User email address")
    parser.add_argument(
        "--workspace", required=True, metavar="NAME", help="Workspace name (exact)"
    )
    parser.add_argument(
        "--role",
        default="member",
        choices=roles,
        help=f"Role to assign ({', '.join(roles)}); default: member",
    )
    args = parser.parse_args()

    asyncio.run(
        add_member(
            email=args.email.strip().lower(),
            workspace_name=args.workspace.strip(),
            role=WorkspaceRole(args.role),
        )
    )


if __name__ == "__main__":
    main()
