# REST API — Implementation Plan

## Stack

| Layer | Choice |
|---|---|
| Framework | FastAPI + Pydantic v2 |
| DB | PostgreSQL via SQLAlchemy async + asyncpg |
| Cache / sessions | Redis |
| Full-text search | Meilisearch |
| Object storage | S3-compatible (Garage) via boto3 |
| Messaging | NATS JetStream (indexing, transcription, conversation dump) |
| RAG service | HTTP (synchronous, httpx) |
| Auth | Yandex / GitHub OAuth2 + JWT (python-jose) + API keys |

---

## Project layout

```
src/
  api/
    auth.py
    workspaces.py
    files.py
    folders.py
    rag.py
    transcribe.py
    conversations.py
    openai_proxy.py
  models/
  schemas/
  services/
    auth.py
    workspace.py
    file.py
    file_permission.py
    folder.py
    rag_client.py        # HTTP client to RAG service
    transcribe_client.py
    conversation.py
    s3.py
    nats.py
  core/
    config.py            # pydantic-settings
    db.py                # async engine + session factory
    redis.py
    security.py          # JWT, API key hashing
    deps.py              # FastAPI dependency injection
  subscribers/
    conversation.py      # NATS subscriber: writes conversation history to DB
    indexing.py          # NATS subscriber: updates file indexing_status from results
    transcription.py     # NATS subscriber: updates task status from results
  main.py
```

---

## Role hierarchy

`owner > admin > member > guest`

| Action | owner | admin | member | guest |
|---|---|---|---|---|
| Read workspace, files, folders | ✓ | ✓ | ✓ | ✓ (role) |
| Upload files, create folders | ✓ | ✓ | ✓ | — |
| Edit own file metadata | ✓ | ✓ | ✓ | — |
| Delete own files | ✓ | ✓ | ✓ | — |
| Manage members / invites | ✓ | ✓ | — | — |
| Delete any file, any folder | ✓ | ✓ | — | — |
| Update / delete workspace | ✓ | ✓ | — | — |
| Reindex files | ✓ | ✓ | — | — |
| Transfer ownership | ✓ | — | — | — |
| View all conversations (admin endpoint) | ✓ | ✓ | — | — |

One `owner` per workspace (enforced by partial unique index). Ownership is transferable.
Workspace creator is automatically set as `owner`.
Default invite role is `guest` when no role is specified.

---

## File permission system

Every file has a `security_mode` column (default: `role`).

### `role` mode
Access determined by the user's workspace role. `guest` gets read-only access to all files.

### `per_user` mode
Explicit permission entries live in `file_permissions(file_id, user_id, permission)`.

| Permission | Can do |
|---|---|
| `none` | File is invisible; excluded from listings and RAG |
| `read` | View metadata, download, included in RAG |
| `write` | read + edit metadata, replace/re-upload file, delete |

**Rules:**
- `owner` and `admin` always have implicit full write access, regardless of `file_permissions` entries.
- For all other roles, if no `file_permissions` row exists → default is `none` (invisible).
- Switching a file from `role` → `per_user` does NOT auto-create permission rows; admin must explicitly grant access.

### RAG impact
When the RAG service receives a search request it gets `workspace_id` + `user_id`. It calls back to this API (or queries the DB directly — TBD with RAG team) to resolve which `file_ids` are visible to this user, then filters chunks accordingly. Alternatively, this REST API pre-computes the visible `file_ids` list and passes it in the search request.

---

## Auth flow

### OAuth
- `GET /auth/{provider}` → redirect to provider consent screen
- `GET /auth/{provider}/callback` → exchange code → fetch user info → upsert `users` row → upsert `oauth_accounts` row → issue JWT pair
- Multiple providers per user: `oauth_accounts(provider, provider_user_id)` with unique constraint.
- Email invite merge: on first OAuth login, if `users.invite_email` matches → merge stub user, clear `invite_email`.

### JWT
- `access_token`: 1 h, HS256, `sub=user_id`
- `refresh_token`: 30 d, stored as hash in Redis `rt:<user_id>:<jti>`
- Logout / revoke: delete Redis key

### API keys
- Random 32-byte key, prefix (first 8 chars) stored plaintext for lookup, secret stored as bcrypt hash.
- Carry `scopes` list (e.g. `files:read`, `rag:search`).
- Auth check: workspace role AND scope.

### FastAPI dependency chain
```
get_current_user
  ├── JWT path: decode → validate exp/iat → load User
  └── API key path: prefix lookup → bcrypt verify → load User + scopes

require_workspace_member(min_role)
  └── loads WorkspaceMember, checks role ≥ min_role

require_file_access(min_permission)
  └── checks security_mode:
      role     → maps workspace role to permission level
      per_user → loads FilePermission row (owner/admin bypass)

require_scope(scope)   # API key requests only
```

---

## File upload flow

```
POST /workspaces/{ws}/files (multipart)
  → stream upload to Garage S3 (key: {ws_id}/files/{file_id}.{ext})
  → insert File row (security_mode=role, indexing_status=pending)
  → if auto_index: publish indexing.jobs to NATS
  → return 201
```

Deletion: delete S3 object → publish `indexing.jobs` with `type=delete` → delete DB row.

---

## RAG proxy (HTTP)

`POST /workspaces/{ws}/rag/search` is a synchronous HTTP call to the RAG service.

### ACL context passed to RAG

Instead of enumerating visible file IDs (expensive at scale), the REST API sends a compact exclusion list:

```json
{
  "workspace_id": "ws-001",
  "user_id": "user-001",
  "role": "member",
  "excluded_file_ids": ["file-005", "file-012"],
  "query": "...",
  ...
}
```

`excluded_file_ids` is computed with one focused query — O(restrictions), not O(files):

```sql
SELECT fp.file_id
FROM file_permissions fp
JOIN files f ON f.id = fp.file_id
WHERE fp.user_id = :user_id
  AND fp.permission = 'none'
  AND f.workspace_id = :workspace_id
  AND f.security_mode = 'per_user'
```

RAG rules:
- `owner` / `admin` → `excluded_file_ids` is always empty, full access
- everyone else → search all workspace chunks except `excluded_file_ids`

Payload stays tiny regardless of workspace size. RAG only needs one exclusion filter.

REST API awaits the response and returns it directly to the client. Timeout: 10 s → 503 on timeout.

Status endpoints (`/rag/status`, `/rag/issues`) query the `files` table locally — no RAG service call.

---

## Conversation history

The Rust proxy publishes `ConversationMessage` to NATS subject `conversation.{workspace_id}` for every request and response.

This service runs a background NATS subscriber at startup (`subscribers/conversation.py`):

```
On direction=Request:
  - Upsert Conversation row (id from body, workspace_id, user_id)
  - Auto-generate title from first user message content (truncate to 60 chars)
  - Insert ConversationMessage(role=user, content=last messages[-1].content, raw=body)

On direction=Response:
  - Insert ConversationMessage(role=assistant, content=choices[0].message.content,
      model, finish_reason, prompt_tokens, completion_tokens, total_tokens, raw=body)
  - Update Conversation.message_count, last_message_at
```

### Conversation indexing into RAG
After a conversation accumulates N messages (configurable, default 20) or on explicit trigger, publish an `indexing.jobs` message with `type=index` for a virtual "conversation file". The transcript is assembled and stored as a temporary S3 object, then indexed. `conversations.rag_indexed_at` tracks when this last happened.

### Endpoints
- `GET /workspaces/{ws}/conversations` — user's own conversations (paginated)
- `GET /workspaces/{ws}/conversations/{id}` — conversation metadata
- `GET /workspaces/{ws}/conversations/{id}/messages` — paginated message history
- `PATCH /workspaces/{ws}/conversations/{id}` — edit title
- `DELETE /workspaces/{ws}/conversations/{id}` — delete conversation + messages
- `GET /workspaces/{ws}/admin/conversations` — **admin/owner only**: all conversations in workspace

---

## Transcription flow

```
POST /workspaces/{ws}/transcribe
  → insert TranscriptionTask(status=pending)
  → publish transcription.jobs to NATS
  → return 202 {task_id}

subscribers/transcription.py (background):
  ← consume transcription.results
  → update TranscriptionTask(status, result, processing_time_sec, completed_at, error)
```

---

## OpenAI proxy

`POST /openai/{ws}/chat/completions` — thin reverse proxy to OpenClaw gateway:
1. Authenticate + resolve workspace + check role ≥ guest.
2. Add `X-Workspace-Id` and `X-User-Id` headers.
3. Stream response bytes through without buffering or parsing.

---

## Implementation steps

1. **Core** — `config.py`, `db.py` (async session dep), `redis.py`, `security.py` (JWT + bcrypt + API key helpers)
2. **Auth** — OAuth client (Yandex first, GitHub second), token issuance/refresh/logout, `/auth/me`, API key CRUD
3. **Workspace** — CRUD, member management (role transfer, owner transfer), invite links
4. **Folder** — CRUD, path via recursive CTE, move/rename with cycle detection
5. **File** — multipart upload → S3 → DB, presigned download, metadata PATCH, delete, reindex trigger
6. **File permissions** — `security_mode` toggle, per-user grant/revoke endpoints, `require_file_access` dep
7. **NATS** — `NatsClient` singleton with graceful no-op on connection failure, background subscribers for indexing results, transcription results, conversation dump
8. **RAG HTTP client** — httpx client, `/rag/search` proxy, status/issues from DB
9. **Transcription** — submit job, poll task status
10. **Conversations** — NATS subscriber writes history, REST endpoints for reading, title edit
11. **OpenAI proxy** — httpx streaming reverse proxy
12. **Meilisearch sync** — index workspace names + file names for search params
13. **Error handling & middleware** — global exception handler, request-id header, CORS
