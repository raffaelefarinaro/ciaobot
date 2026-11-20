#!/usr/bin/env python3
"""A deliberately tiny, isolated CiaobotMac fixture.

It binds an ephemeral loopback port, prints the origin, and serves only a
startup-status response and a throwaway page. It is not a Ciaobot server and
must never be pointed at workspace data. Stop it with Ctrl-C.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys

PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>CiaobotMac isolated fixture</title></head>
<body style="font: 16px -apple-system, system-ui, sans-serif; padding: 3rem">
<h1>CiaobotMac isolated fixture</h1>
<p>This page is served from a temporary loopback port by
<code>scripts/fixture-server.py</code>. It is not your Ciaobot server.</p>
<p id="status">Navigating to the loaded fixture...</p>
<script>
  setTimeout(() => location.replace('/loaded'), 500);
</script>
</body></html>
"""

LOADED = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-loaded</title></head>
<body style="font: 16px -apple-system, system-ui, sans-serif; padding: 3rem">
<h1>CiaobotMac fixture loaded</h1>
<p>The native window, startup probe, and WebKit same-origin navigation work.</p>
<p><a href="/appearance">Open the scrollbar appearance check</a></p>
</body></html>
"""

# The menu-bar feed is served here only so the tray can be inspected by hand
# against throwaway rows. These are not workspace chats and the script never
# reads a Ciaobot workspace.
CHATS = b"""{"chats":[
  {"chat_id":"fixture-chat-1","title":"Fixture chat one","workspace":"fixture","last_activity_at":"2026-10-29T12:00:00Z","unread":true,"needs_input":false},
  {"chat_id":"fixture-chat-2","title":"Fixture chat two","workspace":"fixture","last_activity_at":"2026-10-29T11:00:00Z","unread":false,"needs_input":true},
  {"chat_id":"fixture-chat-3","title":"Quiet fixture chat","workspace":"other","last_activity_at":"2026-10-29T10:00:00Z","unread":false,"needs_input":false}
],"attention_count":2}
"""

# Manual-only appearance check. The page mirrors Ciaobot's own contract: dark
# by default, light when <html> has theme-light. It is long enough to show the
# native scroller track in both the page and a nested pane; toggle the link to
# see the client's theme-aware scrollbar styling switch. No Ciaobot data here.
APPEARANCE = b"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>fixture-appearance</title>
<style>
  :root { --bg: #1a1a2e; background: var(--bg); color: #e8e8f0; }
  :root.theme-light { --bg: #f4f4fa; color: #1a1a2e; }
  body { font: 16px -apple-system, system-ui, sans-serif; margin: 0; padding: 2rem; }
  .pane { height: 180px; overflow-y: auto; border: 1px solid currentColor; padding: 1rem; }
  .tall { height: 1600px; }
</style></head>
<body>
<h1>Scrollbar appearance fixture</h1>
<p><a href="#" id="toggle">Toggle theme-light</a>. The outer page and the pane below are both scrollable.</p>
<div class="pane"><div class="tall">nested scroller</div></div>
<div class="tall">page scroller</div>
<script>
  document.getElementById('toggle').addEventListener('click', function (event) {
    event.preventDefault();
    document.documentElement.classList.toggle('theme-light');
  });
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/startup-status":
            body = b'{"startup_id":"manual-fixture","phases":[],"overall_ready":true}'
            content_type = "application/json"
            status = 200
        elif path == "/api/menubar-chats":
            body = CHATS
            content_type = "application/json"
            status = 200
        elif path == "/":
            body = PAGE
            content_type = "text/html; charset=utf-8"
            status = 200
        elif path == "/loaded":
            body = LOADED
            content_type = "text/html; charset=utf-8"
            status = 200
        elif path.startswith("/chat/"):
            chat_id = path[len("/chat/"):]
            body = (
                "<!doctype html><html><head><meta charset=\"utf-8\">"
                f"<title>fixture-chat {chat_id}</title></head>"
                "<body style=\"font: 16px -apple-system, system-ui, sans-serif; padding: 3rem\">"
                f"<h1>Fixture chat {chat_id}</h1>"
                "<p>This is an isolated chat path, not workspace data.</p>"
                "<p><a href=\"/loaded\">Back to the fixture</a></p></body></html>"
            ).encode("utf-8")
            content_type = "text/html; charset=utf-8"
            status = 200
        elif path == "/appearance":
            body = APPEARANCE
            content_type = "text/html; charset=utf-8"
            status = 200
        elif path == "/favicon.ico":
            body = b""
            content_type = "text/plain"
            status = 204
        else:
            body = b"not found"
            content_type = "text/plain"
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    print(f"Isolated CiaobotMac fixture: http://127.0.0.1:{port}", flush=True)
    print("Not a Ciaobot server. Press Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
