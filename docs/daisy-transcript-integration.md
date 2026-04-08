# DAISY (PHP) Transcript Integration Guide (End-to-End)

This document explains how DAISY (your PHP CRM) can receive **real-time call transcripts** (customer + agent) from the **Genesys AudioHook Transcription Connector** and display them inside the agent UI.

It is intended to be self-contained so you can implement without needing additional clarification.

---

## 1) What you are integrating

### Goal
When a Genesys call starts, the transcription connector produces streaming transcripts for:
- **Channel 0**: customer audio
- **Channel 1**: agent audio

DAISY should show both streams in the agent UI (two panes), updating live with partial results and then final results.

### High-level flow
1. Genesys connects to the connector (AudioHook WebSocket).
2. Connector negotiates media and receives audio frames.
3. Connector runs STT and produces transcript text for channel 0 / channel 1.
4. For each transcript update, the connector sends an **HTTP POST** to DAISY.
5. DAISY stores/broadcasts those transcript updates to the agent UI.

Important: The `/debug` web page in this repo is **only for debugging**. Production DAISY integration is via HTTP POST updates.

---

## 2) Connector → DAISY: HTTP endpoint contract

### Endpoint
DAISY must expose an HTTP endpoint that accepts JSON:
- **Method:** `POST`
- **Path (default):** `/live_transcription/update`
- **Content-Type:** `application/json`

The connector constructs the URL as:
`{DAISY_BASE_URL}{DAISY_UPDATE_PATH}`

Where:
- `DAISY_BASE_URL` example: `https://daisy.example.com`
- `DAISY_UPDATE_PATH` default: `/live_transcription/update`

Connector reference:
- `config.py` (`DAISY_BASE_URL`, `DAISY_UPDATE_PATH`)
- `daisy_client.py` (`post_update`)

### Authentication (recommended)
If the connector is configured with `DAISY_API_KEY`, requests include:
- Header: `Authorization: Bearer <DAISY_API_KEY>`

If `DAISY_API_KEY` is empty/unset, the connector sends no Authorization header.

DAISY should validate this token server-side.

### Expected response
DAISY should return:
- `2xx` for success (connector treats any 2xx as success)
- Non-2xx will be retried briefly (see **Retry behavior** below)

### Retry behavior (connector)
The connector retries when DAISY returns non-2xx or times out:
- Timeout: `DAISY_TIMEOUT_SECS` (default `2.0s`)
- Retries: `DAISY_MAX_RETRIES` (default `2`)
- Backoff base: `DAISY_RETRY_BASE_DELAY_SECS` (default `0.25s`)

This means DAISY should respond quickly. If DAISY needs heavier processing, it should enqueue work and return `202 Accepted` fast.

Connector reference:
- `config.py` (`DAISY_TIMEOUT_SECS`, `DAISY_MAX_RETRIES`, `DAISY_RETRY_BASE_DELAY_SECS`)
- `daisy_client.py` (`post_update`)

---

## 3) Message types and payloads

DAISY will receive JSON payloads with this common envelope:

```json
{
  "conversation_id": "string",
  "type": "TRANSCRIPT",
  "data": { }
}
```

### 3.1 `TRANSCRIPT`

Sent whenever STT produces a transcript update on a channel.

#### Fields
- `conversation_id` (string, required)
  - Primary key for grouping updates for one call/conversation.
  - If Genesys provides a `conversationId`, the connector uses that.
  - Otherwise, the connector uses its AudioHook session id as fallback.
- `type` = `"TRANSCRIPT"`
- `data` object:
  - `speaker` (string, required): usually `"customer"` or `"agent"`
  - `text` (string, required): transcript content (may be empty in rare cases)
  - `timestamp` (string, required): ISO-8601 UTC string, example: `"2026-04-08T04:46:12.123Z"`
  - `is_final` (boolean, optional): only included if connector setting `DAISY_INCLUDE_IS_FINAL=true`

Connector reference:
- Payload generation: `daisy_client.py` (`send_transcript`)
- Where it is called: `audio_hook_server.py` (inside `process_transcription_responses`)

#### Examples

Partial transcript (no `is_final` unless enabled):
```json
{
  "conversation_id": "b2a0b0d1-1111-2222-3333-444444444444",
  "type": "TRANSCRIPT",
  "data": {
    "speaker": "customer",
    "text": "hello mic testing",
    "timestamp": "2026-04-08T04:46:03.000Z"
  }
}
```

Final transcript (when `DAISY_INCLUDE_IS_FINAL=true`):
```json
{
  "conversation_id": "b2a0b0d1-1111-2222-3333-444444444444",
  "type": "TRANSCRIPT",
  "data": {
    "speaker": "agent",
    "text": "yes, how may I help you today",
    "timestamp": "2026-04-08T04:46:05.000Z",
    "is_final": true
  }
}
```

### 3.2 Other types (ignore for now)
The connector can also send other update types (e.g. suggestions). For this transcript-only integration, DAISY can safely:
- accept any `type`
- ignore unknown `type`
- focus only on `type === "TRANSCRIPT"`

---

## 4) Speaker/channel mapping rules

DAISY should rely on the `speaker` field in the payload.

In the connector:
- By default (2 channels):
  - channel 0 → `"customer"`
  - channel 1 → `"agent"`
- Genesys can override speakers via `customConfig.channelSpeakers` in the AudioHook `open` message.

Connector reference:
- Default mapping: `audio_hook_server.py` (`handle_open`, after media negotiation)
- Where speaker is chosen: `audio_hook_server.py` (`speaker = self.channel_speakers.get(channel, ...)`)

---

## 5) Ordering, duplicates, and partial vs final behavior (important for UI)

### Ordering
Updates usually arrive in chronological order, but DAISY should be robust to small reordering.

### Partial vs final
Many STT providers emit partial (interim) updates before emitting a final result.

Recommended display logic:
- If `is_final` is **not present**, treat every incoming transcript as a new line (simple).
- If `is_final` **is present**:
  - if `is_final=false`, update the most recent “live line” for that speaker (replace text in-place)
  - if `is_final=true`, append/commit a final line and clear the “live line”

If DAISY doesn’t want to implement partial updates:
- Ask us to set `DAISY_INCLUDE_IS_FINAL=false` and treat each update as an append-only line.

### Duplicates
The connector may retry POSTs briefly when DAISY returns non-2xx/timeouts, which can result in duplicate payloads if DAISY processed the request but responded late.

Recommended dedupe strategy (choose one):
1. **Fast + simple**: accept duplicates; UI still works but may show repeats.
2. **Better**: dedupe by `(conversation_id, speaker, timestamp, text)` for a short time window.

---

## 6) DAISY backend implementation (PHP)

### 6.1 Minimal “plain PHP” receiver example

Create an endpoint file (example) `public/live_transcription/update.php` and route `/live_transcription/update` to it.

This example:
- validates `Authorization: Bearer ...` (if you configure a token)
- parses JSON
- handles `type=TRANSCRIPT`
- responds quickly with `204 No Content`

```php
<?php
// public/live_transcription/update.php

declare(strict_types=1);

// 1) Auth (recommended)
// Set this in your DAISY environment (or config):
//   DAISY_INGEST_BEARER="some-long-random-token"
$expectedBearer = getenv('DAISY_INGEST_BEARER') ?: '';
if ($expectedBearer !== '') {
  $auth = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
  if (!preg_match('/^Bearer\\s+(.+)$/i', $auth, $m) || !hash_equals($expectedBearer, trim($m[1]))) {
    http_response_code(401);
    header('Content-Type: text/plain');
    echo "Unauthorized\n";
    exit;
  }
}

// 2) Parse JSON body
$raw = file_get_contents('php://input');
$payload = json_decode($raw ?? '', true);
if (!is_array($payload)) {
  http_response_code(400);
  header('Content-Type: text/plain');
  echo "Invalid JSON\n";
  exit;
}

$conversationId = (string)($payload['conversation_id'] ?? '');
$type = (string)($payload['type'] ?? '');
$data = $payload['data'] ?? null;

if ($conversationId === '' || $type === '' || !is_array($data)) {
  http_response_code(400);
  header('Content-Type: text/plain');
  echo "Missing required fields\n";
  exit;
}

// 3) Handle transcript updates
if ($type === 'TRANSCRIPT') {
  $speaker = (string)($data['speaker'] ?? '');
  $text = (string)($data['text'] ?? '');
  $timestamp = (string)($data['timestamp'] ?? '');
  $isFinal = array_key_exists('is_final', $data) ? (bool)$data['is_final'] : null;

  // TODO: Persist or publish to agent UI
  // - Store in DB keyed by $conversationId
  // - Push to UI via WebSocket/SSE
  // - Or keep in memory and have UI poll
  //
  // Example storage record:
  // {
  //   conversation_id, speaker, text, timestamp, is_final
  // }

  // Always respond fast
  http_response_code(204);
  exit;
}

// 4) Ignore other message types for now
http_response_code(204);
exit;
```

### 6.2 Where to put the data (recommended)

At minimum DAISY needs a way to fetch transcript lines for a conversation in the agent UI.

Recommended data model (table or document):
- `conversation_id` (string) – indexed
- `speaker` (string) – `"customer"` / `"agent"`
- `timestamp` (datetime / string)
- `text` (text)
- `is_final` (boolean nullable)

Delivery to UI options:
1. **WebSocket**: push updates to the browser immediately (best UX).
2. **SSE**: simpler than WebSocket for server→browser streaming.
3. **Polling**: simplest; UI polls `/transcripts?conversation_id=...` every 500–1000ms.

---

## 7) DAISY frontend UI rendering (recommended behavior)

Minimum UI:
- Two transcript panels:
  - Customer (speaker=`customer`)
  - Agent (speaker=`agent`)
- Each panel shows a list of transcript lines with timestamps.

If using partial updates (when `is_final` exists):
- Maintain one “live” line per speaker for `is_final=false`
- Replace it in place
- When `is_final=true`, commit it as a final line and clear live line

---

## 8) Connector configuration for your environment

These environment variables are set on the connector side (EC2/service):
- `DAISY_BASE_URL` (required to enable DAISY sending)
  - Example: `https://daisy.example.com`
- `DAISY_UPDATE_PATH` (optional)
  - Default: `/live_transcription/update`
- `DAISY_API_KEY` (optional but recommended)
  - If set, sent as `Authorization: Bearer ...`
- `DAISY_INCLUDE_IS_FINAL`
  - `true` to include `is_final` in transcript payloads
  - `false` to omit it

Connector reference:
- `config.py` (DAISY settings)

---

## 9) Testing checklist (end-to-end)

### 9.1 Test the DAISY endpoint alone (no Genesys needed)

Use `curl` to send a sample transcript:
```bash
curl -i \
  -X POST "https://daisy.example.com/live_transcription/update" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <DAISY_API_KEY>" \
  --data-binary '{
    "conversation_id": "test-conv-123",
    "type": "TRANSCRIPT",
    "data": {
      "speaker": "customer",
      "text": "test transcript",
      "timestamp": "2026-04-08T00:00:00.000Z",
      "is_final": true
    }
  }'
```

Expected:
- `204` or any `2xx`

### 9.2 Test with connector running (real flow)
1. Ensure connector service has DAISY env vars configured.
2. Start a Genesys test call that triggers AudioHook.
3. Verify DAISY receives requests (server logs).
4. Verify DAISY UI shows the two transcript streams.

---

## 10) Troubleshooting

### DAISY receives nothing
- Confirm connector has `DAISY_BASE_URL` set (empty disables sending).
- Confirm DAISY endpoint is reachable from connector network (firewall/SG).
- Confirm DAISY returns `2xx` quickly.

### DAISY gets requests but UI shows nothing
- Ensure DAISY stores/publishes by `conversation_id` and the agent UI is viewing the same `conversation_id`.
- If DAISY doesn’t have the Genesys conversation id at UI time, implement a mapping:
  - Genesys `conversationId` should be passed/known to DAISY agent UI context, or
  - DAISY should provide a correlation id and the connector should be configured to use it (future enhancement).

### Duplicates
- Implement dedupe as described in section 5 (because of retries).

---

## 11) Questions DAISY dev must answer (integration decisions)

1. How will DAISY agent UI know the `conversation_id` to subscribe to?
   - If DAISY already uses Genesys `conversationId`, you’re done.
2. Push vs poll:
   - WebSocket/SSE (recommended) or polling endpoint?
3. Partial transcript UX:
   - Use `DAISY_INCLUDE_IS_FINAL=true` and update-in-place, or append-only.
