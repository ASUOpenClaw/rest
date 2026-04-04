# NATS Contract — Internal Services

All messages use JSON. JetStream is used for durable delivery.
RAG search is **HTTP** (synchronous), not NATS — see RAG HTTP contract below.

---

## Subjects overview

| Subject | Type | Publisher | Consumer |
|---|---|---|---|
| `indexing.jobs` | JetStream | REST API | Indexing service |
| `indexing.results` | JetStream | Indexing service | REST API (subscriber) |
| `transcription.jobs` | JetStream | REST API | Transcription service |
| `transcription.results` | JetStream | Transcription service | REST API (subscriber) |
| `conversation.{workspace_id}` | JetStream | Rust proxy | REST API (subscriber) |

---

## JetStream streams

```
INDEXING      subjects: indexing.*        retention: limits  ack_policy: explicit
TRANSCRIPTION subjects: transcription.*   retention: limits  ack_policy: explicit
CONVERSATIONS subjects: conversation.*    retention: limits  ack_policy: explicit
```

---

## 1. Indexing

### `indexing.jobs` — index, reindex, or delete file chunks

Published by REST API on file upload (`auto_index=true`), `/reindex`, or file deletion.

```json
{
  "job_id": "uuid",
  "type": "index",
  "workspace_id": "ws-001",
  "file_id": "file-001",
  "s3_key": "ws-001/files/file-001.pdf",
  "mime_type": "application/pdf",
  "original_name": "protocol_western_blot_v3.pdf"
}
```

`type`: `"index"` | `"reindex"` | `"delete"`

For `"delete"`: only `workspace_id` and `file_id` are required. Indexer drops all chunks for that file.

For conversation transcripts (`type=index` on a virtual file):
```json
{
  "job_id": "uuid",
  "type": "index",
  "workspace_id": "ws-001",
  "file_id": "conversation:{conversation_id}",
  "s3_key": "ws-001/conversations/{conversation_id}.txt",
  "mime_type": "text/plain",
  "original_name": "conversation_{title}.txt",
  "metadata": {
    "source": "conversation",
    "conversation_id": "conv-001",
    "user_id": "user-001"
  }
}
```

---

### `indexing.results` — indexing outcome

Published by Indexing service on job completion or failure.

```json
{
  "job_id": "uuid",
  "file_id": "file-001",
  "workspace_id": "ws-001",
  "status": "completed",
  "indexed_chunks": 34,
  "error": null,
  "completed_at": "2026-04-01T16:30:00Z"
}
```

`status`: `"completed"` | `"failed"`

REST API subscriber updates `files.indexing_status`, `indexed_chunks`, `indexing_error`, `last_indexed_at`.
For conversation virtual files, updates `conversations.rag_indexed_at`.

---

## 2. Transcription

### `transcription.jobs`

Published by REST API after inserting a `TranscriptionTask` row.

```json
{
  "task_id": "task-001",
  "workspace_id": "ws-001",
  "file_id": "file-010",
  "s3_key": "ws-001/files/file-010.mp4",
  "mime_type": "video/mp4",
  "language": "ru",
  "include_timestamps": true
}
```

---

### `transcription.results`

Published by Transcription service on completion or failure.

```json
{
  "task_id": "task-001",
  "status": "completed",
  "result": {
    "text": "Итак, результаты эксперимента показали...",
    "language": "ru",
    "duration_sec": 342.5,
    "segments": [
      { "start": 0.0, "end": 3.2, "text": "Итак, результаты эксперимента показали," },
      { "start": 3.2, "end": 6.8, "text": "что концентрация белка..." }
    ]
  },
  "processing_time_sec": 45,
  "error": null,
  "completed_at": "2026-04-01T15:02:00Z"
}
```

REST API subscriber updates `transcription_tasks` row.

---

## 3. Conversation history dump

### `conversation.{workspace_id}` — published by Rust proxy

One message per proxied request, one per response. REST API subscriber writes to `conversations` and `conversation_messages`.

**Direction = Request** (new user message):

```json
{
  "message_id": "uuid",
  "workspace_id": "ws-001",
  "user_id": "user-001",
  "direction": "request",
  "body": {
    "model": "auto",
    "messages": [
      { "role": "system", "content": "..." },
      { "role": "user", "content": "Какие контроли для Western blot?" }
    ],
    "stream": true,
    "conversation_id": "conv-001",
    "save_history": true
  },
  "timestamp": "2026-04-01T15:00:00Z"
}
```

Subscriber actions:
- Upsert `conversations` row (`id=conversation_id`, workspace_id, user_id)
- If `title IS NULL`: set title = first 60 chars of last `user` message content
- Insert `conversation_messages(role=user, content=body.messages[-1 with role=user].content, raw=body)`

**Direction = Response** (assistant reply):

```json
{
  "message_id": "uuid",
  "workspace_id": "ws-001",
  "user_id": "user-001",
  "direction": "response",
  "body": {
    "id": "chatcmpl-abc123",
    "model": "Qwen2.5-72B-Instruct-AWQ",
    "choices": [
      {
        "index": 0,
        "message": { "role": "assistant", "content": "Для Western blot..." },
        "finish_reason": "stop"
      }
    ],
    "usage": {
      "prompt_tokens": 1247,
      "completion_tokens": 384,
      "total_tokens": 1631
    },
    "x_lab_metadata": {
      "conversation_id": "conv-001"
    }
  },
  "timestamp": "2026-04-01T15:00:05Z"
}
```

Subscriber actions:
- Insert `conversation_messages(role=assistant, content, model, finish_reason, prompt_tokens, completion_tokens, total_tokens, raw=body)`
- Update `conversations.message_count += 1`, `last_message_at`
- If `message_count >= RAG_INDEX_THRESHOLD`: publish `indexing.jobs` for conversation transcript

---

---

## RAG HTTP contract

**Endpoint (RAG service):** `POST /search`

**Request** (sent by REST API):

```json
{
  "workspace_id": "ws-001",
  "user_id": "user-001",
  "role": "member",
  "excluded_file_ids": ["file-005", "file-012"],
  "query": "Какие контроли использовать для Western blot?",
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

`excluded_file_ids` — files with `security_mode=per_user` where this user has `permission=none`.
For `owner`/`admin` this list is always `[]`.
RAG service excludes these file IDs from chunk search regardless of other filters.

**Response:**

```json
{
  "results": [
    {
      "chunk_id": "chunk-001-017",
      "text": "Для Western blot анализа белков массой 40-60 kDa...",
      "score": 0.89,
      "file_id": "file-001",
      "metadata": {
        "page": 7,
        "section": "Methods — Controls",
        "chunk_index": 17
      }
    }
  ],
  "total_found": 5,
  "query_embedding_ms": 12,
  "search_ms": 8,
  "error": null
}
```

Timeout: 10 s. REST API returns `503` to client on timeout or RAG unavailability.

---

## Error handling

- Indexing / transcription services: NACK with delay on transient errors, max retries then ACK + publish failed result.
- Conversation subscriber: on parse error, log and ACK (never block the stream on bad messages).
- RAG HTTP: REST API returns `503` to client on timeout (10 s) or RAG service unavailable.
