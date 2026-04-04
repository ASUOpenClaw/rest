import uuid

from httpx import AsyncClient

from src.models import Workspace


async def test_create_folder(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/folders",
        headers=auth_headers,
        json={"name": "Documents"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Documents"
    assert data["parent_id"] is None
    assert "id" in data


async def test_list_folders(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    await client.post(
        f"/v1/workspaces/{test_workspace.id}/folders",
        headers=auth_headers,
        json={"name": "Folder A"},
    )
    await client.post(
        f"/v1/workspaces/{test_workspace.id}/folders",
        headers=auth_headers,
        json={"name": "Folder B"},
    )

    resp = await client.get(
        f"/v1/workspaces/{test_workspace.id}/folders", headers=auth_headers
    )
    assert resp.status_code == 200
    names = [f["name"] for f in resp.json()["items"]]
    assert "Folder A" in names
    assert "Folder B" in names


async def test_get_folder_tree(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.get(
        f"/v1/workspaces/{test_workspace.id}/folders/tree", headers=auth_headers
    )
    assert resp.status_code == 200
    assert "tree" in resp.json()


async def test_create_nested_folder(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    parent_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/folders",
        headers=auth_headers,
        json={"name": "Parent"},
    )
    parent_id = parent_resp.json()["id"]

    child_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/folders",
        headers=auth_headers,
        json={"name": "Child", "parent_id": parent_id},
    )
    assert child_resp.status_code == 201
    assert child_resp.json()["parent_id"] == parent_id


async def test_rename_folder(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    create_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/folders",
        headers=auth_headers,
        json={"name": "Old Name"},
    )
    folder_id = create_resp.json()["id"]

    patch_resp = await client.patch(
        f"/v1/workspaces/{test_workspace.id}/folders/{folder_id}",
        headers=auth_headers,
        json={"name": "New Name"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "New Name"


async def test_delete_folder(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    create_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/folders",
        headers=auth_headers,
        json={"name": "To Delete"},
    )
    folder_id = create_resp.json()["id"]

    del_resp = await client.delete(
        f"/v1/workspaces/{test_workspace.id}/folders/{folder_id}",
        headers=auth_headers,
    )
    assert del_resp.status_code == 204


async def test_delete_folder_not_found(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.delete(
        f"/v1/workspaces/{test_workspace.id}/folders/{uuid.uuid4()}",
        headers=auth_headers,
    )
    assert resp.status_code == 404
