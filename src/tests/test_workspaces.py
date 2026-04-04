import uuid

from httpx import AsyncClient

from src.models import User, Workspace


async def test_create_workspace(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/v1/workspaces",
        headers=auth_headers,
        json={"name": "My Workspace", "description": "A test workspace"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Workspace"
    assert data["description"] == "A test workspace"
    assert "id" in data


async def test_list_workspaces(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.get("/v1/workspaces", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    ids = [ws["id"] for ws in data["items"]]
    assert str(test_workspace.id) in ids


async def test_get_workspace(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.get(f"/v1/workspaces/{test_workspace.id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(test_workspace.id)
    assert data["name"] == test_workspace.name


async def test_get_workspace_not_member(client: AsyncClient, auth_headers: dict):
    resp = await client.get(f"/v1/workspaces/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code in (403, 404)


async def test_update_workspace(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.patch(
        f"/v1/workspaces/{test_workspace.id}",
        headers=auth_headers,
        json={"name": "Renamed Workspace"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Workspace"


async def test_delete_workspace(
    client: AsyncClient, auth_headers: dict, test_user: User
):
    # Create a fresh workspace to delete
    create_resp = await client.post(
        "/v1/workspaces",
        headers=auth_headers,
        json={"name": "To Delete"},
    )
    ws_id = create_resp.json()["id"]

    del_resp = await client.delete(f"/v1/workspaces/{ws_id}", headers=auth_headers)
    assert del_resp.status_code == 204

    # Deleted workspace returns 403 (not a member anymore)
    get_resp = await client.get(f"/v1/workspaces/{ws_id}", headers=auth_headers)
    assert get_resp.status_code == 403


async def test_list_members(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace, test_user: User
):
    resp = await client.get(
        f"/v1/workspaces/{test_workspace.id}/members", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    user_ids = [m["user_id"] for m in data["items"]]
    assert str(test_user.id) in user_ids


async def test_add_member(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/members",
        headers=auth_headers,
        json={"email": "newmember@example.com", "role": "member"},
    )
    assert resp.status_code == 201
    assert resp.json()["email"] == "newmember@example.com"
    assert resp.json()["role"] == "member"


async def test_create_invite_and_join(
    client: AsyncClient,
    auth_headers: dict,
    test_workspace: Workspace,
    test_user: User,
    db_session,
):
    from src.core.security import create_access_token
    from src.models import User

    # Create invite as owner
    invite_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/invites",
        headers=auth_headers,
        json={"role": "member"},
    )
    assert invite_resp.status_code == 201
    invite_code = invite_resp.json()["code"]

    # Create a second user and join
    second_user = User(email="joiner@example.com", display_name="Joiner")
    db_session.add(second_user)
    await db_session.flush()
    await db_session.refresh(second_user)
    token, _ = create_access_token(second_user.id)
    second_headers = {"Authorization": f"Bearer {token}"}

    join_resp = await client.post(
        "/v1/workspaces/join",
        headers=second_headers,
        json={"invite_code": invite_code},
    )
    assert join_resp.status_code == 200

    # Confirm second user is now a member
    members_resp = await client.get(
        f"/v1/workspaces/{test_workspace.id}/members", headers=auth_headers
    )
    user_ids = [m["user_id"] for m in members_resp.json()["items"]]
    assert str(second_user.id) in user_ids
