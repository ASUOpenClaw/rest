from httpx import AsyncClient

from src.models import User


async def test_me_requires_auth(client: AsyncClient):
    resp = await client.get("/v1/auth/me")
    assert resp.status_code == 401


async def test_me_returns_current_user(
    client: AsyncClient, auth_headers: dict, test_user: User
):
    resp = await client.get("/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == test_user.email
    assert data["display_name"] == test_user.display_name


async def test_create_api_key(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/v1/auth/api-keys",
        headers=auth_headers,
        json={"name": "my-key", "scopes": ["files:read"]},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my-key"
    assert data["scopes"] == ["files:read"]
    assert data["key"].startswith("lab_sk_")


async def test_list_api_keys(client: AsyncClient, auth_headers: dict):
    # Create two keys
    for name in ("key-a", "key-b"):
        await client.post(
            "/v1/auth/api-keys",
            headers=auth_headers,
            json={"name": name},
        )

    resp = await client.get("/v1/auth/api-keys", headers=auth_headers)
    assert resp.status_code == 200
    names = [k["name"] for k in resp.json()]
    assert "key-a" in names
    assert "key-b" in names


async def test_revoke_api_key(client: AsyncClient, auth_headers: dict):
    # Create
    create_resp = await client.post(
        "/v1/auth/api-keys",
        headers=auth_headers,
        json={"name": "temp-key"},
    )
    assert create_resp.status_code == 201
    key_id = create_resp.json()["id"]

    # Revoke
    del_resp = await client.delete(f"/v1/auth/api-keys/{key_id}", headers=auth_headers)
    assert del_resp.status_code == 204

    # Verify it no longer appears in list
    list_resp = await client.get("/v1/auth/api-keys", headers=auth_headers)
    ids = [k["id"] for k in list_resp.json()]
    assert key_id not in ids


async def test_authenticate_with_api_key(client: AsyncClient, auth_headers: dict):
    # Create an API key
    create_resp = await client.post(
        "/v1/auth/api-keys",
        headers=auth_headers,
        json={"name": "api-auth-key"},
    )
    raw_key = create_resp.json()["key"]

    # Use it to authenticate
    resp = await client.get("/v1/auth/me", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
