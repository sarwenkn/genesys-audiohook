import asyncio
import json
import uuid
import logging
import http
import os
from datetime import datetime
import hmac
import hashlib
import base64
from urllib.parse import urlsplit, parse_qs

from config import (
    GENESYS_LISTEN_HOST,
    GENESYS_LISTEN_PORT,
    GENESYS_PATH,
    DEBUG,
    GENESYS_API_KEY,
    GENESYS_ORG_ID,
    DEBUG_UI_TOKEN,
)
from audio_hook_server import AudioHookServer
from utils import format_json
from debug_hub import DebugHub

# ---------------------------
# Simple Logging Setup
# ---------------------------
if DEBUG == 'true':
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("GenesysGoogleBridge")

# Updated import: get the protocol from websockets.server (websockets 15.0)
from websockets.server import WebSocketServerProtocol

debug_hub = DebugHub()

class CustomWebSocketServerProtocol(WebSocketServerProtocol):
    async def handshake(self, *args, **kwargs):
        try:
            return await super().handshake(*args, **kwargs)
        except Exception as exc:
            logger.error(f"Handshake failed: {exc}", exc_info=True)
            raise

async def validate_request(connection, request):
    """
    This function is called by websockets.serve() to validate the HTTP request
    before upgrading to a WebSocket.
    Signature verification is disabled; only the API key is required.
    """
    raw_path = request.path
    split = urlsplit(raw_path)
    path_only = split.path
    qs = parse_qs(split.query or "")

    # Health endpoint support (plain HTTP)
    if path_only == "/health":
        return connection.respond(http.HTTPStatus.OK, "OK\n")

    # Optional debug UI (protected by token)
    if path_only in ("/debug", "/debug/ws"):
        if not DEBUG_UI_TOKEN:
            return connection.respond(http.HTTPStatus.NOT_FOUND, "Not found\n")
        token = (qs.get("token") or [""])[0]
        if token != DEBUG_UI_TOKEN:
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        if path_only == "/debug":
            html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AudioHook Debug</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 16px; }}
    .row {{ display: flex; gap: 12px; flex-wrap: wrap; }}
    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 12px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; }}
    .muted {{ color: #666; }}
  </style>
</head>
<body>
  <h2>AudioHook Debug</h2>
  <div class="row">
    <div class="card">
      <div><b>Status</b>: <span id="status" class="muted">connecting...</span></div>
      <div><b>Filter session</b>: <input id="sessionFilter" placeholder="optional session id" /></div>
    </div>
    <div class="card">
      <div><b>Tip</b>: keep this page open during a Genesys test call.</div>
      <div class="muted">This endpoint is protected by a token. Disable when done.</div>
    </div>
  </div>
  <h3>Transcripts</h3>
  <pre id="transcripts"></pre>
  <h3>Events</h3>
  <pre id="events"></pre>

  <script>
    const statusEl = document.getElementById('status');
    const eventsEl = document.getElementById('events');
    const transcriptsEl = document.getElementById('transcripts');
    const sessionFilterEl = document.getElementById('sessionFilter');

    const wsProto = (location.protocol === 'https:') ? 'wss' : 'ws';
    const token = new URLSearchParams(location.search).get('token') || '';
    const wsUrl = `${{wsProto}}://${{location.host}}/debug/ws?token=${{encodeURIComponent(token)}}`;
    const ws = new WebSocket(wsUrl);

    function append(el, line) {{
      el.textContent += line + "\\n";
      el.scrollTop = el.scrollHeight;
    }}

    ws.onopen = () => {{ statusEl.textContent = 'connected'; }};
    ws.onclose = () => {{ statusEl.textContent = 'closed'; }};
    ws.onerror = () => {{ statusEl.textContent = 'error'; }};

    ws.onmessage = (ev) => {{
      let msg;
      try {{ msg = JSON.parse(ev.data); }} catch {{ return; }}
      const sessionFilter = (sessionFilterEl.value || '').trim();
      const sid = msg?.payload?.session_id || msg?.payload?.id || '';
      if (sessionFilter && sid && sid !== sessionFilter) return;

      const ts = new Date((msg.ts || Date.now()/1000) * 1000).toISOString();
      if (msg.type === 'transcript') {{
        const p = msg.payload || {{}};
        append(transcriptsEl, `[${{ts}}] ${{p.speaker || p.channel || ''}}${{p.is_final ? ' (final)' : ''}}: ${{p.text || ''}}`);
      }}
      append(eventsEl, `[${{ts}}] ${{msg.type}} ${{JSON.stringify(msg.payload || {{}})}}`);
    }};
  </script>
</body>
</html>"""
            return connection.respond(http.HTTPStatus.OK, html)
        # /debug/ws: allow WebSocket upgrade to proceed
        return None
    
    path_str = path_only
    raw_headers = dict(request.headers)

    logger.info(f"\n{'='*50}\n[HTTP] Starting WebSocket upgrade validation")
    logger.info(f"[HTTP] Target path: {GENESYS_PATH}")
    logger.info(f"[HTTP] Remote address: {raw_headers.get('host', 'unknown')}")

    logger.info("[HTTP] Full headers received:")
    for name, value in raw_headers.items():
        if name.lower() in ['x-api-key', 'authorization']:
            logger.info(f"[HTTP]   {name}: {'*' * 8}")
        else:
            logger.info(f"[HTTP]   {name}: {value}")

    normalized_path = path_str.rstrip('/')
    normalized_target = GENESYS_PATH.rstrip('/')
    if normalized_path != normalized_target:
        logger.error("[HTTP] Path mismatch:")
        logger.error(f"[HTTP]   Expected: {GENESYS_PATH}")
        logger.error(f"[HTTP]   Normalized received: {normalized_path}")
        logger.error(f"[HTTP]   Normalized expected: {normalized_target}")
        return connection.respond(http.HTTPStatus.NOT_FOUND, "Invalid path\n")

    required_headers = [
        'audiohook-organization-id',
        'audiohook-correlation-id',
        'audiohook-session-id',
        'x-api-key',
        'upgrade',
        'sec-websocket-version',
        'sec-websocket-key'
    ]

    header_keys = {k.lower(): v for k, v in raw_headers.items()}
    logger.info("[HTTP] Normalized headers for validation:")
    for k, v in header_keys.items():
        if k in ['x-api-key', 'authorization']:
            logger.info(f"[HTTP]   {k}: {'*' * 8}")
        else:
            logger.info(f"[HTTP]   {k}: {v}")

    if header_keys.get('x-api-key') != GENESYS_API_KEY:
        logger.error("Invalid X-API-KEY header value.")
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "Invalid API key\n")

    if header_keys.get('audiohook-organization-id') != GENESYS_ORG_ID:
        logger.error("Invalid Audiohook-Organization-Id header value.")
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "Invalid Audiohook-Organization-Id\n")

    missing_headers = []
    found_headers = []
    for h in required_headers:
        if h.lower() not in header_keys:
            missing_headers.append(h)
        else:
            found_headers.append(h)

    if missing_headers:
        error_msg = f"Missing required headers: {', '.join(missing_headers)}"
        logger.error(f"[HTTP] Connection rejected - {error_msg}")
        logger.error("[HTTP] Found headers: " + ", ".join(found_headers))
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, error_msg)

    upgrade_header = header_keys.get('upgrade', '').lower()
    logger.info(f"[HTTP] Checking upgrade header: {upgrade_header}")
    if upgrade_header != 'websocket':
        error_msg = f"Invalid upgrade header: {upgrade_header}"
        logger.error(f"[HTTP] {error_msg}")
        return connection.respond(http.HTTPStatus.BAD_REQUEST, "WebSocket upgrade required\n")

    ws_version = header_keys.get('sec-websocket-version', '')
    logger.info(f"[HTTP] Checking WebSocket version: {ws_version}")
    if ws_version != '13':
        error_msg = f"Invalid WebSocket version: {ws_version}"
        logger.error(f"[HTTP] {error_msg}")
        return connection.respond(http.HTTPStatus.BAD_REQUEST, "WebSocket version 13 required\n")

    ws_key = header_keys.get('sec-websocket-key')
    if not ws_key:
        logger.error("[HTTP] Missing WebSocket key")
        return connection.respond(http.HTTPStatus.BAD_REQUEST, "WebSocket key required\n")
    logger.info("[HTTP] Found valid WebSocket key")

    ws_protocol = header_keys.get('sec-websocket-protocol', '')
    if ws_protocol:
        logger.info(f"[HTTP] WebSocket protocol requested: {ws_protocol}")
        if 'audiohook' not in ws_protocol.lower():
            logger.warning("[HTTP] Client didn't request 'audiohook' protocol")

    connection_header = header_keys.get('connection', '').lower()
    logger.info(f"[HTTP] Connection header: {connection_header}")
    if 'upgrade' not in connection_header:
        logger.warning("[HTTP] Connection header doesn't contain 'upgrade'")

    logger.info("[HTTP] All validation checks passed successfully")
    logger.info(f"[HTTP] Proceeding with WebSocket upgrade")
    logger.info("="*50)
    return None

async def handle_genesys_connection(websocket):
    connection_id = str(uuid.uuid4())[:8]
    logger.info(f"\n{'='*50}\n[WS-{connection_id}] New WebSocket connection handler started")

    session = None

    try:
        ws_path = urlsplit(getattr(websocket, "path", "") or "").path
        if ws_path == "/debug/ws":
            await debug_hub.register(websocket)
            try:
                await websocket.wait_closed()
            finally:
                await debug_hub.unregister(websocket)
            return

        logger.info(f"Received WebSocket connection from {websocket.remote_address}")
        logger.info(f"[WS-{connection_id}] Remote address: {websocket.remote_address}")
        logger.info(f"[WS-{connection_id}] Connection state: {websocket.state}")

        ws_attributes = ['path', 'remote_address', 'local_address', 'state', 'open', 'subprotocol']
        logger.info(f"[WS-{connection_id}] WebSocket object attributes:")
        for attr in ws_attributes:
            value = getattr(websocket, attr, "Not available")
            logger.info(f"[WS-{connection_id}]   {attr}: {value}")

        logger.info(f"[WS-{connection_id}] WebSocket connection established; handshake was validated beforehand.")

        session = AudioHookServer(websocket, debug_hub=debug_hub)
        logger.info(f"[WS-{connection_id}] Session created with ID: {session.session_id}")

        logger.info(f"[WS-{connection_id}] Starting main message loop")
        while session.running:
            try:
                logger.debug(f"[WS-{connection_id}] Waiting for next message...")
                msg = await websocket.recv()
                if isinstance(msg, bytes):
                    logger.debug(f"[WS-{connection_id}] Received binary frame: {len(msg)} bytes")
                    await session.handle_audio_frame(msg)
                else:
                    try:
                        data = json.loads(msg)
                        logger.debug(f"[WS-{connection_id}] Received JSON message:\n{format_json(data)}")
                        await session.handle_message(data)
                    except json.JSONDecodeError as e:
                        logger.error(f"[WS-{connection_id}] Error parsing JSON: {e}")
                        await session.disconnect_session("error", f"JSON parse error: {e}")
                    except Exception as ex:
                        logger.error(f"[WS-{connection_id}] Error processing message: {ex}")
                        await session.disconnect_session("error", f"Message processing error: {ex}")
            except Exception as ex:
                from websockets.exceptions import ConnectionClosed
                if isinstance(ex, ConnectionClosed):
                    logger.info(f"[WS-{connection_id}] Connection closed: code={ex.code}, reason={ex.reason}")
                else:
                    logger.error(f"[WS-{connection_id}] Unexpected error: {ex}", exc_info=True)
                break

        logger.info(f"[WS-{connection_id}] Session loop ended, cleaning up")
        if session and hasattr(session, 'openai_client') and session.openai_client:
            await session.openai_client.close()
        logger.info(f"[WS-{connection_id}] Session cleanup complete")
    except Exception as e:
        logger.error(f"[WS-{connection_id}] Fatal connection error: {e}", exc_info=True)
        if session is None:
            session = AudioHookServer(websocket)
        await session.disconnect_session(reason="error", info=f"Internal error: {str(e)}")
    finally:
        logger.info(f"[WS-{connection_id}] Connection handler finished\n{'='*50}")

async def main():
    startup_msg = f"""
{'='*80}
Genesys-OpenAIBridge Server
Starting up at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Host: {GENESYS_LISTEN_HOST}
Port: {GENESYS_LISTEN_PORT}
Path: {GENESYS_PATH}
{'='*80}
"""
    logger.info(startup_msg)

    websockets_logger = logging.getLogger('websockets')
    if DEBUG != 'true':
        websockets_logger.setLevel(logging.INFO)

    # Monkey-patch the default protocol in the server.
    import websockets.server
    websockets.server.WebSocketServerProtocol = CustomWebSocketServerProtocol

    try:
        async with websockets.serve(
            handle_genesys_connection,
            GENESYS_LISTEN_HOST,
            int(GENESYS_LISTEN_PORT),
            process_request=validate_request,
            max_size=64000,
            ping_interval=None,
            ping_timeout=None
        ):
            logger.info(
                f"Server is listening for Genesys AudioHook connections on "
                f"ws://{GENESYS_LISTEN_HOST}:{GENESYS_LISTEN_PORT}{GENESYS_PATH}"
            )
            try:
                await asyncio.Future()  # run forever
            except asyncio.CancelledError:
                logger.info("Server shutdown initiated")
    except Exception as e:
        logger.error(f"Failed to start server: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down via KeyboardInterrupt.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
    finally:
        logger.info("Server shutdown complete.")
