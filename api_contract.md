# REST API Contract — Lab AI Platform

**Base URL:** `https://api.lab-platform.ru/v1`
**Формат:** JSON
**Авторизация:** Bearer JWT (кроме auth endpoints)
**Версионирование:** через URL prefix (`/v1`)

---

## 1. Аутентификация и авторизация

### 1.1 Yandex OAuth — инициация

```
GET /auth/yandex
```

Редирект на Yandex OAuth consent screen. После подтверждения — callback на `/auth/yandex/callback`.

**Query params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| redirect_uri | string | no | Куда вернуть после auth (default: web UI) |
| state | string | no | CSRF-token, возвращается в callback |

**Response:** `302 Redirect` → `https://oauth.yandex.ru/authorize?...`

---

### 1.2 Yandex OAuth — callback

```
GET /auth/yandex/callback
```

Обрабатывает callback от Yandex, создаёт/обновляет пользователя, выдаёт JWT.

**Query params:**

| Param | Type | Description |
|-------|------|-------------|
| code | string | Authorization code от Yandex |
| state | string | CSRF-token |

**Response:** `200 OK`

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "email": "user@yandex.ru",
    "display_name": "Иван Петров",
    "avatar_url": "https://avatars.yandex.net/...",
    "created_at": "2026-04-01T10:00:00Z"
  }
}
```

**Errors:** `400` invalid code, `403` auth denied

---

### 1.3 Обновление токена

```
POST /auth/refresh
```

**Body:**

```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

**Response:** `200 OK`

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_in": 3600
}
```

**Errors:** `401` invalid/expired refresh token

---

### 1.4 Получение текущего пользователя

```
GET /auth/me
```

**Headers:** `Authorization: Bearer <access_token>`

**Response:** `200 OK`

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "user@yandex.ru",
  "display_name": "Иван Петров",
  "avatar_url": "https://avatars.yandex.net/...",
  "created_at": "2026-04-01T10:00:00Z",
  "workspaces": [
    {
      "id": "ws-001",
      "name": "Лаборатория молекулярной биологии",
      "role": "admin"
    },
    {
      "id": "ws-002",
      "name": "Отдел биохимии",
      "role": "member"
    }
  ]
}
```

---

### 1.5 Выход

```
POST /auth/logout
```

Инвалидирует refresh token.

**Response:** `204 No Content`

---

### 1.6 Создание API-ключа (для интеграций)

```
POST /auth/api-keys
```

**Body:**

```json
{
  "name": "LIMS Integration",
  "workspace_id": "ws-001",
  "scopes": ["files:read", "rag:search"],
  "expires_in_days": 90
}
```

**Response:** `201 Created`

```json
{
  "id": "ak-001",
  "key": "lab_sk_live_abc123...",
  "name": "LIMS Integration",
  "workspace_id": "ws-001",
  "scopes": ["files:read", "rag:search"],
  "created_at": "2026-04-01T10:00:00Z",
  "expires_at": "2026-07-01T10:00:00Z"
}
```

> `key` показывается только в этом ответе. При потере — создать новый.

---

### 1.7 Список API-ключей

```
GET /auth/api-keys
```

**Query:** `workspace_id` (optional)

**Response:** `200 OK` — массив (без поля `key`)

---

### 1.8 Отзыв API-ключа

```
DELETE /auth/api-keys/{key_id}
```

**Response:** `204 No Content`

---

## 2. Workspace-ы

### 2.1 Создание workspace

```
POST /workspaces
```

**Body:**

```json
{
  "name": "Лаборатория молекулярной биологии",
  "description": "Workspace для группы молекулярной биологии НИИ",
  "system_prompt": "Ты — ассистент-биолог. Помогаешь с анализом лабораторных данных, протоколами экспериментов и интерпретацией результатов. Отвечай на русском языке. При ссылке на документы указывай название файла и страницу.",
  "config": {
    "preferred_model": "Qwen3-14B",
    "temperature": 0.7,
    "max_tokens": 4096,
    "allowed_tools": ["search_documents", "get_file_content", "list_files", "transcribe_audio", "analyze_table"]
  }
}
```

**Response:** `201 Created`

```json
{
  "id": "ws-001",
  "name": "Лаборатория молекулярной биологии",
  "description": "Workspace для группы молекулярной биологии НИИ",
  "system_prompt": "Ты — ассистент-биолог...",
  "config": { ... },
  "created_by": "550e8400-e29b-41d4-a716-446655440000",
  "created_at": "2026-04-01T10:00:00Z",
  "stats": {
    "members_count": 1,
    "files_count": 0,
    "indexed_chunks": 0
  }
}
```

Создатель автоматически становится admin.

---

### 2.2 Список workspace-ов пользователя

```
GET /workspaces
```

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| page | int | 1 | Номер страницы |
| per_page | int | 20 | Элементов на страницу (max 100) |
| search | string | — | Поиск по имени |

**Response:** `200 OK`

```json
{
  "items": [
    {
      "id": "ws-001",
      "name": "Лаборатория молекулярной биологии",
      "description": "...",
      "role": "admin",
      "stats": {
        "members_count": 5,
        "files_count": 47,
        "indexed_chunks": 2340
      },
      "created_at": "2026-04-01T10:00:00Z",
      "updated_at": "2026-04-01T12:30:00Z"
    }
  ],
  "total": 3,
  "page": 1,
  "per_page": 20
}
```

---

### 2.3 Получение workspace

```
GET /workspaces/{workspace_id}
```

**Response:** `200 OK` — полный объект workspace (как в 2.1, плюс stats)

**Errors:** `403` не member, `404` не найден

---

### 2.4 Обновление workspace

```
PATCH /workspaces/{workspace_id}
```

**Requires:** role `admin`

**Body (partial update):**

```json
{
  "name": "Лаб. мол. биологии (обновлено)",
  "system_prompt": "Ты — ассистент-биолог. Новые инструкции...",
  "config": {
    "preferred_model": "Qwen3.5-9B",
    "temperature": 0.5
  }
}
```

**Response:** `200 OK` — обновлённый workspace

При изменении `system_prompt` — автоматически обновляет SOUL.md в OpenClaw-контейнере.

---

### 2.5 Удаление workspace

```
DELETE /workspaces/{workspace_id}
```

**Requires:** role `admin`

Удаляет workspace, все файлы в MinIO, коллекцию в Qdrant, останавливает OpenClaw-контейнер.

**Response:** `204 No Content`

---

### 2.6 Управление участниками

#### Список участников

```
GET /workspaces/{workspace_id}/members
```

**Response:** `200 OK`

```json
{
  "items": [
    {
      "user_id": "550e8400-...",
      "email": "user@yandex.ru",
      "display_name": "Иван Петров",
      "role": "admin",
      "joined_at": "2026-04-01T10:00:00Z"
    }
  ],
  "total": 5
}
```

#### Добавление участника

```
POST /workspaces/{workspace_id}/members
```

**Requires:** role `admin`

**Body:**

```json
{
  "email": "colleague@yandex.ru",
  "role": "member"
}
```

**Response:** `201 Created`

```json
{
  "user_id": "660e8400-...",
  "email": "colleague@yandex.ru",
  "display_name": "Мария Сидорова",
  "role": "member",
  "joined_at": "2026-04-01T14:00:00Z"
}
```

Если пользователь ещё не зарегистрирован — создаётся invite. При первом входе через с email (или OAuth) — автоматически получает доступ.

#### Изменение роли

```
PATCH /workspaces/{workspace_id}/members/{user_id}
```

**Requires:** role `admin`

**Body:**

```json
{
  "role": "viewer"
}
```

**Response:** `200 OK`

#### Удаление участника

```
DELETE /workspaces/{workspace_id}/members/{user_id}
```

**Requires:** role `admin`. Нельзя удалить последнего admin.

**Response:** `204 No Content`

---

### 2.7 Инвайт-ссылки

#### Создание инвайт-ссылки

```
POST /workspaces/{workspace_id}/invites
```

**Requires:** role `admin`

**Body:**

```json
{
  "role": "member",
  "max_uses": 10,
  "expires_in_hours": 72
}
```

**Response:** `201 Created`

```json
{
  "id": "inv-001",
  "url": "https://lab-platform.ru/invite/abc123xyz",
  "role": "member",
  "max_uses": 10,
  "used_count": 0,
  "expires_at": "2026-04-04T10:00:00Z"
}
```

#### Принятие инвайта

```
POST /workspaces/join
```

**Body:**

```json
{
  "invite_code": "abc123xyz"
}
```

**Response:** `200 OK` — workspace object с ролью

---

## 3. Файлы, папки и документы

### Схема данных (PostgreSQL)

```
folders
├── id (UUID, PK)
├── workspace_id (FK → workspaces)
├── parent_id (UUID, FK → folders, nullable) — null = корень workspace
├── name (varchar)
├── created_by (FK → users)
├── created_at, updated_at
├── UNIQUE(workspace_id, parent_id, name)  — нет дубликатов имён в одном parent

files
├── id (UUID, PK)
├── workspace_id (FK → workspaces)
├── folder_id (UUID, FK → folders, nullable) — null = корень workspace
├── original_name (varchar)
├── mime_type (varchar)
├── size_bytes (bigint)
├── description (text, nullable)
├── s3_key (varchar)
├── uploaded_by (FK → users)
├── indexing_status (enum: pending/processing/completed/failed)
├── indexed_chunks (int)
├── metadata (jsonb)
├── created_at, updated_at
```

`folder` в ответах API всегда возвращается как объект или `null`:

```json
"folder": {
  "id": "fold-003",
  "name": "Утверждённые",
  "path": "Протоколы / Утверждённые"
}
// или
"folder": null  // файл в корне workspace
```

`path` — вычисляемое поле, собирается из цепочки parent → root.

### 3.1 Загрузка файла

```
POST /workspaces/{workspace_id}/files
```

**Requires:** role `admin` или `member`

**Content-Type:** `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| file | binary | yes | Файл (max 500 MB) |
| folder_id | uuid | no | ID папки (null = корень workspace) |
| description | string | no | Описание файла |
| auto_index | bool | no | Индексировать в RAG (default: true) |

**Response:** `201 Created`

```json
{
  "id": "file-001",
  "original_name": "protocol_western_blot_v3.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 2457600,
  "folder": {
    "id": "fold-003",
    "name": "Утверждённые",
    "path": "Протоколы / Утверждённые"
  },
  "description": "Протокол Western Blot, версия 3",
  "s3_key": "ws-001/files/file-001.pdf",
  "uploaded_by": {
    "id": "550e8400-...",
    "display_name": "Иван Петров"
  },
  "indexing_status": "pending",
  "indexed_chunks": 0,
  "created_at": "2026-04-01T15:00:00Z"
}
```

`indexing_status`: `pending` → `processing` → `completed` / `failed`

---

### 3.2 Список файлов

```
GET /workspaces/{workspace_id}/files
```

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| page | int | 1 | Страница |
| per_page | int | 20 | На страницу (max 100) |
| folder_id | uuid | — | Фильтр по папке (null = корень workspace) |
| recursive | bool | false | Включить файлы из вложенных папок |
| mime_type | string | — | Фильтр по MIME (e.g. "application/pdf") |
| search | string | — | Поиск по имени файла |
| indexing_status | string | — | Фильтр: pending/processing/completed/failed |
| sort_by | string | created_at | Сортировка: created_at, name, size_bytes |
| sort_order | string | desc | asc или desc |

**Response:** `200 OK`

```json
{
  "items": [
    {
      "id": "file-001",
      "original_name": "protocol_western_blot_v3.pdf",
      "mime_type": "application/pdf",
      "size_bytes": 2457600,
      "folder": {
        "id": "fold-003",
        "name": "Утверждённые",
        "path": "Протоколы / Утверждённые"
      },
      "description": "...",
      "uploaded_by": { "id": "...", "display_name": "Иван Петров" },
      "indexing_status": "completed",
      "indexed_chunks": 34,
      "created_at": "2026-04-01T15:00:00Z"
    }
  ],
  "total": 47,
  "page": 1,
  "per_page": 20
}
```

---

### 3.3 Метаданные файла

```
GET /workspaces/{workspace_id}/files/{file_id}
```

**Response:** `200 OK` — полный объект файла

---

### 3.4 Обновление метаданных

```
PATCH /workspaces/{workspace_id}/files/{file_id}
```

**Requires:** role `admin` или uploader

**Body:**

```json
{
  "description": "Обновлённое описание",
  "folder_id": "fold-003"
}
```

`folder_id`: UUID папки или `null` для перемещения в корень workspace.


**Response:** `200 OK`

---

### 3.5 Получение presigned URL для скачивания

```
GET /workspaces/{workspace_id}/files/{file_id}/download
```

**Response:** `200 OK`

```json
{
  "url": "https://minio.lab-platform.ru/lab-files/ws-001/files/file-001.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=...&X-Amz-Expires=3600&X-Amz-Signature=...",
  "expires_in": 3600,
  "filename": "protocol_western_blot_v3.pdf",
  "content_type": "application/pdf"
}
```

URL действителен `expires_in` секунд. Не требует авторизации при обращении.

---

### 3.6 Получение presigned URL для загрузки (для больших файлов). (Это наметки на будущее, пока что скип)

```
POST /workspaces/{workspace_id}/files/upload-url
```

Для клиентов, которые хотят загружать файлы напрямую в MinIO (минуя API-сервер).

**Body:**

```json
{
  "filename": "large_video.mp4",
  "content_type": "video/mp4",
  "size_bytes": 524288000,
  "folder_id": "fold-005"
}
```

**Response:** `200 OK`

```json
{
  "file_id": "file-002",
  "upload_url": "https://minio.lab-platform.ru/lab-files/ws-001/files/file-002.mp4?X-Amz-Algorithm=...&X-Amz-Expires=3600&...",
  "expires_in": 3600,
  "method": "PUT",
  "headers": {
    "Content-Type": "video/mp4"
  },
  "confirm_url": "/v1/workspaces/ws-001/files/file-002/confirm-upload"
}
```

После загрузки клиент вызывает `confirm_url`.

---

### 3.7 Подтверждение загрузки через presigned URL

```
POST /workspaces/{workspace_id}/files/{file_id}/confirm-upload
```

Проверяет что файл появился в MinIO, запускает индексацию.

**Response:** `200 OK` — объект файла с `indexing_status: "pending"`

---

### 3.8 Переиндексация файла

```
POST /workspaces/{workspace_id}/files/{file_id}/reindex
```

**Requires:** role `admin`

Удаляет старые чанки из Qdrant и запускает повторную индексацию.

**Response:** `202 Accepted`

```json
{
  "file_id": "file-001",
  "indexing_status": "pending",
  "message": "Reindexing started"
}
```

---

### 3.9 Удаление файла

```
DELETE /workspaces/{workspace_id}/files/{file_id}
```

**Requires:** role `admin` или uploader

Удаляет файл из MinIO + все чанки из Qdrant.

**Response:** `204 No Content`

---

### 3.10 Папки (вложенная структура)

Папки — отдельные сущности с `parent_id` для вложенности. Файлы ссылаются на папку через `folder_id` (nullable — корень workspace).

#### Создание папки

```
POST /workspaces/{workspace_id}/folders
```

**Requires:** role `admin` или `member`

**Body:**

```json
{
  "name": "Утверждённые",
  "parent_id": "fold-001"
}
```

`parent_id`: UUID родительской папки или `null` для корневой папки.

**Response:** `201 Created`

```json
{
  "id": "fold-003",
  "name": "Утверждённые",
  "parent_id": "fold-001",
  "path": "Протоколы / Утверждённые",
  "workspace_id": "ws-001",
  "files_count": 0,
  "children_count": 0,
  "created_by": {
    "id": "550e8400-...",
    "display_name": "Иван Петров"
  },
  "created_at": "2026-04-01T15:00:00Z"
}
```

**Errors:** `409` папка с таким именем уже существует в parent, `404` parent_id не найден

---

#### Список папок (содержимое директории)

```
GET /workspaces/{workspace_id}/folders
```

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| parent_id | uuid | null | ID родительской папки (null = корневые) |
| recursive | bool | false | Вернуть всё дерево плоским списком |
| include_files_count | bool | true | Считать файлы в каждой папке |

**Response:** `200 OK`

```json
{
  "parent": null,
  "items": [
    {
      "id": "fold-001",
      "name": "Протоколы",
      "parent_id": null,
      "path": "Протоколы",
      "files_count": 7,
      "children_count": 2,
      "created_at": "2026-04-01T10:00:00Z"
    },
    {
      "id": "fold-002",
      "name": "Датасеты",
      "parent_id": null,
      "path": "Датасеты",
      "files_count": 8,
      "children_count": 0,
      "created_at": "2026-04-01T10:05:00Z"
    }
  ],
  "total": 2
}
```

С `parent_id=fold-001`:

```json
{
  "parent": {
    "id": "fold-001",
    "name": "Протоколы",
    "path": "Протоколы"
  },
  "items": [
    {
      "id": "fold-003",
      "name": "Утверждённые",
      "parent_id": "fold-001",
      "path": "Протоколы / Утверждённые",
      "files_count": 5,
      "children_count": 0,
      "created_at": "2026-04-01T15:00:00Z"
    },
    {
      "id": "fold-004",
      "name": "Черновики",
      "parent_id": "fold-001",
      "path": "Протоколы / Черновики",
      "files_count": 3,
      "children_count": 1,
      "created_at": "2026-04-01T15:05:00Z"
    }
  ],
  "total": 2
}
```

---

#### Получение дерева папок (полное)

```
GET /workspaces/{workspace_id}/folders/tree
```

Возвращает полное дерево вложенных папок одним запросом.

**Response:** `200 OK`

```json
{
  "tree": [
    {
      "id": "fold-001",
      "name": "Протоколы",
      "files_count": 7,
      "children": [
        {
          "id": "fold-003",
          "name": "Утверждённые",
          "files_count": 5,
          "children": []
        },
        {
          "id": "fold-004",
          "name": "Черновики",
          "files_count": 3,
          "children": [
            {
              "id": "fold-006",
              "name": "2026-Q1",
              "files_count": 2,
              "children": []
            }
          ]
        }
      ]
    },
    {
      "id": "fold-002",
      "name": "Датасеты",
      "files_count": 8,
      "children": []
    },
    {
      "id": "fold-005",
      "name": "Видео",
      "files_count": 3,
      "children": []
    }
  ],
  "total_folders": 6,
  "total_files": 28
}
```

---

#### Получение папки

```
GET /workspaces/{workspace_id}/folders/{folder_id}
```

**Response:** `200 OK`

```json
{
  "id": "fold-003",
  "name": "Утверждённые",
  "parent_id": "fold-001",
  "path": "Протоколы / Утверждённые",
  "workspace_id": "ws-001",
  "files_count": 5,
  "children_count": 0,
  "breadcrumbs": [
    { "id": "fold-001", "name": "Протоколы" },
    { "id": "fold-003", "name": "Утверждённые" }
  ],
  "created_by": { "id": "550e8400-...", "display_name": "Иван Петров" },
  "created_at": "2026-04-01T15:00:00Z"
}
```

---

#### Переименование / перемещение папки

```
PATCH /workspaces/{workspace_id}/folders/{folder_id}
```

**Requires:** role `admin`

**Body:**

```json
{
  "name": "Approved Protocols",
  "parent_id": "fold-002"
}
```

`parent_id`: новый родитель (null = корень). Нельзя переместить папку внутрь самой себя или своих потомков.

**Response:** `200 OK` — обновлённый объект папки (с пересчитанным `path`)

**Errors:** `409` циклическая зависимость, `409` имя дублируется в новом parent

---

#### Удаление папки

```
DELETE /workspaces/{workspace_id}/folders/{folder_id}
```

**Requires:** role `admin`

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| mode | string | fail | `fail` — ошибка если папка не пустая; `move_to_parent` — файлы и подпапки перемещаются в parent; `cascade` — удалить всё рекурсивно |

**Response:** `204 No Content`

**Errors:** `409` папка не пустая (при `mode=fail`)


---

## 4. RAG — поиск по документам

### 4.1 Семантический поиск

```
POST /workspaces/{workspace_id}/rag/search
```

Основной endpoint, вызываемый OpenClaw через MCP-сервер.

**Body:**

```json
{
  "query": "Какие контроли использовать для Western blot при анализе белков массой 50kDa?",
  "top_k": 5,
  "min_score": 0.5,
  "filters": {
    "folder_id": "fold-001",
    "include_subfolders": true,
    "mime_types": ["application/pdf"],
    "file_ids": null
  }
}
```

**Response:** `200 OK`

```json
{
  "results": [
    {
      "chunk_id": "chunk-001-017",
      "text": "Для Western blot анализа белков массой 40-60 kDa рекомендуется использовать следующие контроли: положительный контроль — лизат клеток HeLa...",
      "score": 0.89,
      "file": {
        "id": "file-001",
        "name": "protocol_western_blot_v3.pdf",
        "folder": {
          "id": "fold-003",
          "name": "Утверждённые",
          "path": "Протоколы / Утверждённые"
        }
      },
      "metadata": {
        "page": 7,
        "section": "Methods — Controls",
        "chunk_index": 17
      }
    },
    {
      "chunk_id": "chunk-003-042",
      "text": "...",
      "score": 0.76,
      "file": { ... },
      "metadata": { ... }
    }
  ],
  "total_found": 5,
  "query_embedding_ms": 12,
  "search_ms": 8
}
```

---

### 4.2 Статус индексации workspace

```
GET /workspaces/{workspace_id}/rag/status
```

**Response:** `200 OK`

```json
{
  "workspace_id": "ws-001",
  "total_files": 47,
  "indexed_files": 45,
  "pending_files": 1,
  "failed_files": 1,
  "total_chunks": 2340,
  "collection_name": "ws_ws-001",
  "embedding_model": "BAAI/bge-m3",
  "last_indexed_at": "2026-04-01T16:30:00Z"
}
```

---

### 4.3 Список неиндексированных/проблемных файлов

```
GET /workspaces/{workspace_id}/rag/issues
```

**Response:** `200 OK`

```json
{
  "items": [
    {
      "file_id": "file-015",
      "filename": "scan_bad_quality.pdf",
      "status": "failed",
      "error": "OCR failed: image quality too low (DPI < 100)",
      "failed_at": "2026-04-01T15:45:00Z"
    }
  ]
}
```

---

## 5. Транскрипция

### 5.1 Транскрибировать файл

```
POST /workspaces/{workspace_id}/transcribe
```

**Body:**

```json
{
  "file_id": "file-010",
  "language": "ru",
  "include_timestamps": true
}
```

`file_id` — ссылка на аудио/видео файл уже загруженный в workspace.

**Response:** `202 Accepted` (асинхронная задача)

```json
{
  "task_id": "task-001",
  "status": "processing",
  "file_id": "file-010",
  "estimated_duration_sec": 120
}
```

---

### 5.2 Статус транскрипции

```
GET /workspaces/{workspace_id}/transcribe/{task_id}
```

**Response:** `200 OK`

```json
{
  "task_id": "task-001",
  "status": "completed",
  "file_id": "file-010",
  "result": {
    "text": "Итак, результаты эксперимента показали, что концентрация белка...",
    "language": "ru",
    "duration_sec": 342.5,
    "segments": [
      { "start": 0.0, "end": 3.2, "text": "Итак, результаты эксперимента показали," },
      { "start": 3.2, "end": 6.8, "text": "что концентрация белка..." }
    ]
  },
  "processing_time_sec": 45,
  "completed_at": "2026-04-01T15:02:00Z"
}
```

`status`: `pending` → `processing` → `completed` / `failed`

---

## 6. Чат (OpenAI-Compatible API)
 
Вся цепочка запросов говорит на OpenAI-протоколе:
 
```
Client (openai SDK / curl / Open WebUI / Continue / ...)
  → Rust Proxy (auth, workspace routing, rate limiting)
    → OpenClaw Gateway (agent loop, RAG, tool execution)
      → LiteLLM Router (complexity-based model routing)
        → vLLM / Ollama (inference)
```
 
Никаких кастомных форматов. Rust proxy не трансформирует тела запросов — он добавляет auth layer, определяет workspace и проксирует байты.
 
**Base URL:** `https://api.lab-platform.ru/v1/openai/{workspace_id}`
 
---
 
### Роль Rust Proxy
 
Rust proxy — единственная точка входа для всех клиентов.
 
Proxy не парсит `messages`, не модифицирует `tools`, не трогает streaming chunks. Чистый L7 reverse proxy с auth.
 
---
 
### 6.1 Chat Completions
 
```
POST /openai/{workspace_id}/chat/completions
```
 
Полностью совместим с OpenAI Chat Completions API.
 
**Headers:**
 
```
Authorization: Bearer <jwt_or_api_key>
Content-Type: application/json
```
 
**Body:**
 
```json
{
  "model": "auto",
  "messages": [
    {
      "role": "user",
      "content": "Какие контроли нужны для Western blot при анализе белков 50kDa?"
    }
  ],
  "stream": true,
  "temperature": 0.7,
  "max_tokens": 4096
}
```
 
**Поддерживаемые параметры (стандартные OpenAI):**
 
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| model | string | "auto" | `"auto"` (smart routing), `"simple"`, `"medium"`, `"complex"` |
| messages | array | required | Массив сообщений |
| stream | bool | false | SSE-стриминг |
| temperature | float | из workspace config | 0.0 - 2.0 |
| max_tokens | int | из workspace config | Максимум токенов ответа |
| top_p | float | 1.0 | Nucleus sampling |
| stop | string/array | null | Stop-последовательности |
| tools | array | null | Дополнительные tool definitions |
| tool_choice | string/object | "auto" | `"auto"`, `"none"`, `"required"` |
 
**Расширения платформы (через `extra_body` / дополнительные поля):**
 
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| conversation_id | string | null | Привязать к существующему разговору |
| save_history | bool | true | Сохранять в историю |
| disable_rag | bool | false | Отключить автоматический RAG |
| stream_tool_calls | bool | false | Показывать промежуточные tool calls в стриме |
 
Стандартные клиенты (openai SDK, curl) просто игнорируют неизвестные поля в ответе. Расширения передаются через `extra_body`:
 
```python
response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "..."}],
    extra_body={
        "conversation_id": "conv-001",
        "stream_tool_calls": True
    }
)
```
 
**Обработка контекста (на стороне OpenClaw, прозрачно для клиента):**
 
- System prompt из SOUL.md workspace подставляется автоматически. Если клиент передаёт свой `system` message — он мержится (SOUL.md первый, клиентский дополняет).
- RAG-поиск выполняется автоматически по последнему `user` сообщению. Результаты инжектируются в контекст до отправки в LLM.
- Если `conversation_id` указан — предыдущие сообщения подгружаются из OpenClaw session.
- Tool calls (search_documents, transcribe и т.д.) выполняются внутренне через MCP-сервер → REST API. Клиент видит только финальный ответ.
 
---
 
**Response (non-streaming):** `200 OK`
 
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1743523200,
  "model": "Qwen2.5-72B-Instruct-AWQ",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Для Western blot анализа белков массой 50 kDa рекомендуются следующие контроли:\n\n1. **Положительный контроль** — лизат клеток HeLa..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 1247,
    "completion_tokens": 384,
    "total_tokens": 1631
  },
  "system_fingerprint": "ws-001/auto/medium",
  "x_lab_metadata": {
    "workspace_id": "ws-001",
    "conversation_id": "conv-001",
    "message_id": "msg-042",
    "routing_tier": "medium",
    "rag_sources": [
      { "file_id": "file-001", "filename": "protocol_wb.pdf", "page": 7, "score": 0.89 }
    ]
  }
}
```
 
`x_lab_metadata` — расширение платформы. Стандартные клиенты игнорируют, кастомный UI использует для citations и отображения routing info. Кастомный UI может получить RAG-источники из `response.x_lab_metadata.rag_sources` или `response.model_extra["x_lab_metadata"]` в openai SDK.
 
---
 
**Response (streaming):** `200 OK`, `Content-Type: text/event-stream`
 
Стандартный OpenAI SSE формат. `x_lab_metadata` в последнем чанке:
 
```
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1743523200,"model":"Qwen2.5-72B-Instruct-AWQ","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
 
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1743523200,"model":"Qwen2.5-72B-Instruct-AWQ","choices":[{"index":0,"delta":{"content":"Для Western blot"},"finish_reason":null}]}
 
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1743523200,"model":"Qwen2.5-72B-Instruct-AWQ","choices":[{"index":0,"delta":{"content":" анализа белков"},"finish_reason":null}]}
 
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1743523200,"model":"Qwen2.5-72B-Instruct-AWQ","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1247,"completion_tokens":384,"total_tokens":1631},"x_lab_metadata":{"workspace_id":"ws-001","conversation_id":"conv-001","routing_tier":"medium","rag_sources":[{"file_id":"file-001","filename":"protocol_wb.pdf","page":7}]}}
 
data: [DONE]
```
 
---
 
**Streaming с tool calls** (`stream_tool_calls: true`):
 
Для кастомного UI с отображением "идёт поиск по документам...":
 
```
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_001","type":"function","function":{"name":"search_documents","arguments":""}}]},"finish_reason":null}]}
 
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"query\":\"Western blot controls 50kDa\"}"}}]},"finish_reason":null}]}
 
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}
 
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","x_lab_tool_result":{"call_id":"call_001","name":"search_documents","result_preview":"Found 5 documents: protocol_wb.pdf p.7, controls_guide.pdf p.12..."}}
 
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Для Western blot"},"finish_reason":null}]}
 
...
 
data: [DONE]
```
 
По умолчанию (`stream_tool_calls: false`) — tool calls выполняются server-side, клиент видит только финальный текст.
 
---
 
### 6.2 Список моделей
 
```
GET /openai/{workspace_id}/models
```
 
**Response:** `200 OK`
 
```json
{
  "object": "list",
  "data": [
    {
      "id": "auto",
      "object": "model",
      "created": 1743523200,
      "owned_by": "lab-platform",
      "description": "Smart routing — автоматический выбор модели по сложности"
    },
    {
      "id": "simple",
      "object": "model",
      "created": 1743523200,
      "owned_by": "lab-platform",
      "description": "Qwen3-30B-A3B MoE — быстрые ответы (~80-100 tok/s)"
    },
    {
      "id": "medium",
      "object": "model",
      "created": 1743523200,
      "owned_by": "lab-platform",
      "description": "Qwen 2.5 72B — анализ, RAG (~20-30 tok/s)"
    },
    {
      "id": "complex",
      "object": "model",
      "created": 1743523200,
      "owned_by": "lab-platform",
      "description": "Qwen 3 235B — reasoning, planning (~10-15 tok/s)"
    }
  ]
}
```
 
---
 
### 6.3 Embeddings
 
```
POST /openai/{workspace_id}/embeddings
```
 
**Body:**
 
```json
{
  "model": "bge-m3",
  "input": "Western blot protocol controls"
}
```
 
**Response:** `200 OK`
 
```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "index": 0,
      "embedding": [0.0123, -0.0456, ...]
    }
  ],
  "model": "BAAI/bge-m3",
  "usage": { "prompt_tokens": 5, "total_tokens": 5 }
}
```
 
---
 
### 6.4 История разговоров
 
Эти endpoints не входят в OpenAI spec, но нужны для UI (sidebar с историей чатов). Живут в нативном namespace `/workspaces/`.
 
#### Список разговоров
 
```
GET /workspaces/{workspace_id}/conversations
```
 
**Query:** `page`, `per_page`, `sort_by` (updated_at)
 
**Response:** `200 OK`
 
```json
{
  "items": [
    {
      "id": "conv-001",
      "title": "Western blot контроли",
      "messages_count": 8,
      "created_at": "2026-04-01T15:00:00Z",
      "updated_at": "2026-04-01T15:12:00Z"
    }
  ],
  "total": 15
}
```
 
#### Получение сообщений
 
```
GET /workspaces/{workspace_id}/conversations/{conversation_id}
```
 
**Query:** `page`, `per_page`
 
**Response:** `200 OK`
 
```json
{
  "id": "conv-001",
  "title": "Western blot контроли",
  "messages": [
    {
      "id": "msg-041",
      "role": "user",
      "content": "Какие контроли нужны для Western blot?",
      "timestamp": "2026-04-01T15:00:00Z"
    },
    {
      "id": "msg-042",
      "role": "assistant",
      "content": "Для Western blot рекомендуются следующие контроли...",
      "model": "Qwen2.5-72B-Instruct-AWQ",
      "routing_tier": "medium",
      "rag_sources": [
        { "file_id": "file-001", "filename": "protocol_wb.pdf", "page": 7, "score": 0.89 }
      ],
      "timestamp": "2026-04-01T15:00:05Z"
    }
  ],
  "total_messages": 8
}
```
 
#### Удаление разговора
 
```
DELETE /workspaces/{workspace_id}/conversations/{conversation_id}
```
 
**Response:** `204 No Content`
 
---
 
### 6.5 Примеры подключения клиентов
 
#### Python (openai SDK)
 
```python
from openai import OpenAI
 
client = OpenAI(
    base_url="https://api.lab-platform.ru/v1/openai/ws-001",
    api_key="lab_sk_live_abc123..."
)
 
# Простой запрос
response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Где найти протокол ELISA?"}]
)
print(response.choices[0].message.content)
 
# Streaming
for chunk in client.chat.completions.create(
    model="medium",
    messages=[{"role": "user", "content": "Объясни Western blot"}],
    stream=True
):
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
 
# С привязкой к разговору и tool call стримингом
response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Спланируй эксперимент"}],
    stream=True,
    extra_body={
        "conversation_id": "conv-001",
        "stream_tool_calls": True
    }
)
```
 
#### curl
 
```bash
curl -X POST https://api.lab-platform.ru/v1/openai/ws-001/chat/completions \
  -H "Authorization: Bearer lab_sk_live_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Какие контроли для WB?"}]
  }'
```
 
#### Open WebUI
 
```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    environment:
      OPENAI_API_BASE_URL: https://api.lab-platform.ru/v1/openai/ws-001
      OPENAI_API_KEY: lab_sk_live_abc123...
```
 
#### Continue (VS Code)
 
```json
{
  "models": [{
    "provider": "openai",
    "title": "Lab AI (Biology)",
    "model": "auto",
    "apiBase": "https://api.lab-platform.ru/v1/openai/ws-001",
    "apiKey": "lab_sk_live_abc123..."
  }]
}
```
 
#### TypeScript
 
```typescript
import OpenAI from 'openai';
 
const client = new OpenAI({
  baseURL: 'https://api.lab-platform.ru/v1/openai/ws-001',
  apiKey: 'lab_sk_live_abc123...',
});
 
const completion = await client.chat.completions.create({
  model: 'auto',
  messages: [{ role: 'user', content: 'Объясни метод ПЦР' }],
});
```
 
---
 
## 7. Администрирование (system-wide)
 
### 7.1 Статистика платформы
 
```
GET /admin/stats
```
 
**Requires:** system admin role
 
**Response:** `200 OK`
 
```json
{
  "users_total": 150,
  "users_active_24h": 42,
  "workspaces_total": 23,
  "workspaces_active": 18,
  "files_total": 1240,
  "total_storage_bytes": 15728640000,
  "rag_chunks_total": 58200,
  "gpu_servers": [
    {
      "name": "gpu-fast",
      "model": "Qwen3-30B-A3B",
      "status": "healthy",
      "requests_24h": 3420,
      "avg_latency_ms": 245
    },
    {
      "name": "gpu-main",
      "model": "Qwen2.5-72B-AWQ",
      "status": "healthy",
      "requests_24h": 1580,
      "avg_latency_ms": 890
    }
  ],
  "openclaw_gateways": [
    { "workspace_id": "ws-001", "status": "running", "uptime_hours": 72 },
    { "workspace_id": "ws-002", "status": "hibernated" }
  ]
}
```
 
---
 
### 7.2 Управление OpenClaw Gateway-ами
 
#### Список gateway-ов
 
```
GET /admin/gateways
```
 
**Response:** `200 OK`
 
```json
{
  "items": [
    {
      "workspace_id": "ws-001",
      "container_id": "openclaw-ws-001",
      "status": "running",
      "memory_mb": 1840,
      "uptime_hours": 72,
      "active_sessions": 3
    }
  ]
}
```
 
#### Перезапуск gateway
 
```
POST /admin/gateways/{workspace_id}/restart
```
 
**Response:** `202 Accepted`
 
#### Hibernate gateway
 
```
POST /admin/gateways/{workspace_id}/hibernate
```
 
**Response:** `202 Accepted`
 
#### Wake gateway
 
```
POST /admin/gateways/{workspace_id}/wake
```
 
**Response:** `202 Accepted`
 
---
 
### 7.3 Статус LLM Router
 
```
GET /admin/llm/status
```
 
**Response:** `200 OK`
 
```json
{
  "router": "litellm",
  "routing_strategy": "complexity_router",
  "models": [
    {
      "name": "simple",
      "backend": "Qwen3-30B-A3B",
      "gpu_server": "gpu-fast:8000",
      "status": "healthy",
      "current_queue": 2,
      "requests_1h": 145,
      "avg_tokens_per_sec": 92
    },
    {
      "name": "medium",
      "backend": "Qwen2.5-72B-AWQ",
      "gpu_server": "gpu-main:8000",
      "status": "healthy",
      "current_queue": 0,
      "requests_1h": 67,
      "avg_tokens_per_sec": 24
    }
  ],
  "routing_stats_1h": {
    "total_requests": 212,
    "simple_pct": 58,
    "medium_pct": 32,
    "complex_pct": 10
  }
}
```
 
---
 
## 8. Webhooks (для внешних интеграций)
 
### 8.1 Регистрация webhook
 
```
POST /workspaces/{workspace_id}/webhooks
```
 
**Requires:** role `admin`
 
**Body:**
 
```json
{
  "url": "https://lims.lab.ru/api/webhook",
  "events": ["file.uploaded", "file.indexed", "transcription.completed"],
  "secret": "whsec_abc123..."
}
```
 
**Response:** `201 Created`
 
---
 
### 8.2 Payload webhooks
 
Все webhooks подписываются HMAC-SHA256 с `secret` в заголовке `X-Webhook-Signature`.
 
**Event: file.uploaded**
 
```json
{
  "event": "file.uploaded",
  "workspace_id": "ws-001",
  "data": {
    "file_id": "file-001",
    "filename": "protocol.pdf",
    "uploaded_by": "550e8400-...",
    "timestamp": "2026-04-01T15:00:00Z"
  }
}
```
 
**Event: file.indexed**
 
```json
{
  "event": "file.indexed",
  "workspace_id": "ws-001",
  "data": {
    "file_id": "file-001",
    "chunks_count": 34,
    "status": "completed",
    "timestamp": "2026-04-01T15:05:00Z"
  }
}
```
 
**Event: transcription.completed**
 
```json
{
  "event": "transcription.completed",
  "workspace_id": "ws-001",
  "data": {
    "task_id": "task-001",
    "file_id": "file-010",
    "text_length": 4520,
    "duration_sec": 342.5,
    "timestamp": "2026-04-01T15:02:00Z"
  }
}
```
 
---
 
## 9. Общие соглашения
 
### Ошибки
 
Все ошибки возвращаются в формате:
 
```json
{
  "error": {
    "code": "workspace_not_found",
    "message": "Workspace ws-999 not found",
    "details": null
  }
}
```
 
| HTTP Code | Значение |
|-----------|----------|
| 400 | Bad Request — невалидные параметры |
| 401 | Unauthorized — отсутствует или невалидный токен |
| 403 | Forbidden — нет прав на операцию |
| 404 | Not Found — ресурс не найден |
| 409 | Conflict — дубликат (e.g. email уже в workspace) |
| 413 | Payload Too Large — файл превышает лимит |
| 422 | Unprocessable Entity — валидация провалена |
| 429 | Too Many Requests — rate limit |
| 500 | Internal Server Error |
| 502 | Bad Gateway — OpenClaw или GPU недоступен |
| 503 | Service Unavailable — workspace gateway hibernated, waking up |
 
### Rate Limiting
 
| Scope | Лимит |
|-------|-------|
| Per user | 100 req/min |
| Per workspace | 300 req/min |
| File upload | 10 files/min per user |
| RAG search | 60 req/min per user |
| Chat messages | 30 msg/min per user |
| Transcription | 5 tasks/hour per workspace |
 
Заголовки: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`
 
### Пагинация
 
Все list-эндпоинты поддерживают offset-based пагинацию:
 
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| page | int | 1 | Номер страницы (начинается с 1) |
| per_page | int | 20 | Элементов на страницу (max 100) |
 
Response wrapper:
 
```json
{
  "items": [...],
  "total": 150,
  "page": 1,
  "per_page": 20
}
```
 
### Фильтрация по дате
 
Эндпоинты, возвращающие списки с `created_at`, поддерживают:
 
| Param | Type | Description |
|-------|------|-------------|
| created_after | ISO datetime | Только после этой даты |
| created_before | ISO datetime | Только до этой даты |
