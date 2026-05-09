import uuid

from httpx import AsyncClient

from src.models import Workspace

_FAKE_FILE = b"fake file content"
_FAKE_FILENAME = "document.pdf"
_FAKE_MIME = "application/pdf"


def _file_payload():
    return {"file": (_FAKE_FILENAME, _FAKE_FILE, _FAKE_MIME)}


async def test_upload_file(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/files",
        headers=auth_headers,
        files=_file_payload(),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["original_name"] == _FAKE_FILENAME
    assert data["mime_type"] == _FAKE_MIME
    assert "id" in data


async def test_upload_file_with_description(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/files",
        headers=auth_headers,
        files=_file_payload(),
        data={"description": "My important doc"},
    )
    assert resp.status_code == 201
    assert resp.json()["description"] == "My important doc"


async def test_list_files(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    await client.post(
        f"/v1/workspaces/{test_workspace.id}/files",
        headers=auth_headers,
        files=_file_payload(),
    )

    resp = await client.get(
        f"/v1/workspaces/{test_workspace.id}/files", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["items"]) >= 1


async def test_get_file(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    upload_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/files",
        headers=auth_headers,
        files=_file_payload(),
    )
    file_id = upload_resp.json()["id"]

    resp = await client.get(
        f"/v1/workspaces/{test_workspace.id}/files/{file_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == file_id


async def test_get_file_not_found(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    resp = await client.get(
        f"/v1/workspaces/{test_workspace.id}/files/{uuid.uuid4()}",
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_update_file_description(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    upload_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/files",
        headers=auth_headers,
        files=_file_payload(),
    )
    file_id = upload_resp.json()["id"]

    patch_resp = await client.patch(
        f"/v1/workspaces/{test_workspace.id}/files/{file_id}",
        headers=auth_headers,
        json={"description": "Updated description"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["description"] == "Updated description"


async def test_download_file(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    upload_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/files",
        headers=auth_headers,
        files=_file_payload(),
    )
    file_id = upload_resp.json()["id"]

    resp = await client.get(
        f"/v1/workspaces/{test_workspace.id}/files/{file_id}/download",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")


async def test_delete_file(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    upload_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/files",
        headers=auth_headers,
        files=_file_payload(),
    )
    file_id = upload_resp.json()["id"]

    del_resp = await client.delete(
        f"/v1/workspaces/{test_workspace.id}/files/{file_id}",
        headers=auth_headers,
    )
    assert del_resp.status_code == 204

    get_resp = await client.get(
        f"/v1/workspaces/{test_workspace.id}/files/{file_id}",
        headers=auth_headers,
    )
    assert get_resp.status_code == 404


async def test_upload_file_into_folder(
    client: AsyncClient, auth_headers: dict, test_workspace: Workspace
):
    folder_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/folders",
        headers=auth_headers,
        json={"name": "Uploads"},
    )
    folder_id = folder_resp.json()["id"]

    upload_resp = await client.post(
        f"/v1/workspaces/{test_workspace.id}/files",
        headers=auth_headers,
        files=_file_payload(),
        data={"folder_id": folder_id},
    )
    assert upload_resp.status_code == 201
    assert upload_resp.json()["folder"]["id"] == folder_id
