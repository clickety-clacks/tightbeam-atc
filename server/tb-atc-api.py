#!/usr/bin/env python3
"""tb-atc-api — the control plane for the Air Traffic Control view.

A deliberately small, dependency-free HTTP service holding three things in
memory: the page's current selection, a queue of commands for the page, and a
set of tags. Anything on the network may read or write it; there is no
authentication, by design, because it controls a display and nothing else.

The page polls /api/pull, applies any commands it has not seen, and reports
what it currently has selected. External callers use the verbs below.

    GET    /api/state                    everything at once
    GET    /api/selection                [{id, type, title}]
    POST   /api/select                   {add:[id], remove:[id], clear:bool}
    POST   /api/focus                    {id, mode:"single"|"neighborhood"|"clear"}
    POST   /api/fit                      {on:bool}
    GET    /api/tags                     [{tagId, target, text, source, at}]
    POST   /api/tags                     {tags:[{target, text, source}]}
    DELETE /api/tags                     clear all
    DELETE /api/tags/<tagId>             clear one
    GET    /api/pull?since=<seq>         page only: commands + tags

Node ids are the ones the feed publishes: work items as `wi_...`, agents as
their session suffix `s_...`. `type` is "item" or "agent".

It also serves the page itself, so one process is the whole installation: no
nginx, no second port, and the API sits at /api next to the page that uses it.

Config:
  TB_ATC_HOST  bind address                     (default 127.0.0.1)
  TB_ATC_PORT  port                             (default 8787)
  TB_ATC_WEB   directory holding index.html etc (default ./web next to this file)
"""
import json, os, posixpath, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

HOST = os.environ.get("TB_ATC_HOST", "127.0.0.1")
PORT = int(os.environ.get("TB_ATC_PORT", "8787"))
WEB = Path(os.environ.get("TB_ATC_WEB", Path(__file__).resolve().parent.parent / "web"))

lock = threading.Lock()
state = {
    "selection": [],        # last reported by the page
    "selectionAt": 0,
    "commands": [],         # [{seq, kind, ...}] — consumed by seq, trimmed
    "seq": 0,
    "tags": {},             # tagId -> tag
    "tagSeq": 0,
    "tagsRev": 0,
}


def push(kind, **payload):
    state["seq"] += 1
    cmd = {"seq": state["seq"], "kind": kind, **payload}
    state["commands"].append(cmd)
    del state["commands"][:-200]        # a display, not a durable log
    return cmd


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------- plumbing ----------
    def log_message(self, *a):
        pass                            # quiet; journald carries the unit's own lines

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self._send({})

    # ---------- reads ----------
    def _serve_file(self, path):
        rel = posixpath.normpath(path.lstrip("/")) or "index.html"
        if rel in (".", "/"): rel = "index.html"
        target = (WEB / rel).resolve()
        if not str(target).startswith(str(WEB.resolve())) or target.is_dir():
            target = WEB / "index.html"
        if not target.exists():
            return self._send({"error": "not found"}, 404)
        ctype = {".html":"text/html", ".js":"text/javascript", ".json":"application/json",
                 ".css":"text/css", ".png":"image/png", ".svg":"image/svg+xml"}.get(
                     target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # the feed and the page itself must never be cached; this is a live view
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._serve_file(u.path)
        with lock:
            if u.path == "/api/state":
                return self._send({
                    "selection": state["selection"],
                    "selectionAt": state["selectionAt"],
                    "tags": list(state["tags"].values()),
                    "seq": state["seq"],
                })
            if u.path == "/api/selection":
                return self._send(state["selection"])
            if u.path == "/api/tags":
                return self._send(list(state["tags"].values()))
            if u.path == "/api/pull":
                since = int((parse_qs(u.query).get("since") or ["0"])[0])
                return self._send({
                    "commands": [c for c in state["commands"] if c["seq"] > since],
                    "seq": state["seq"],
                    "tags": list(state["tags"].values()),
                    "tagsRev": state["tagsRev"],
                })
        self._send({"error": "not found"}, 404)

    # ---------- writes ----------
    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        with lock:
            if u.path == "/api/select":
                add = [str(x) for x in (b.get("add") or [])]
                rm = [str(x) for x in (b.get("remove") or [])]
                clear = bool(b.get("clear"))
                if not (add or rm or clear):
                    return self._send({"error": "nothing to do"}, 400)
                return self._send(push("select", add=add, remove=rm, clear=clear))

            if u.path == "/api/focus":
                mode = b.get("mode", "single")
                if mode not in ("single", "neighborhood", "clear"):
                    return self._send({"error": "mode must be single, neighborhood or clear"}, 400)
                if mode != "clear" and not b.get("id"):
                    return self._send({"error": "id required"}, 400)
                return self._send(push("focus", id=b.get("id"), mode=mode))

            if u.path == "/api/fit":
                return self._send(push("fit", on=bool(b.get("on", True))))

            if u.path == "/api/selection":       # the page reporting in
                state["selection"] = b.get("selection") or []
                state["selectionAt"] = int(time.time() * 1000)
                return self._send({"ok": True, "count": len(state["selection"])})

            if u.path == "/api/tags":
                made = []
                for t in (b.get("tags") or []):
                    target, text = t.get("target"), (t.get("text") or "").strip()
                    if not target or not text:
                        continue
                    state["tagSeq"] += 1
                    tag = {
                        "tagId": f"t{state['tagSeq']}",
                        "target": str(target),
                        "text": text[:80],
                        "source": "user" if t.get("source") == "user" else "agent",
                        "at": int(time.time() * 1000),
                    }
                    state["tags"][tag["tagId"]] = tag
                    made.append(tag)
                if made:
                    state["tagsRev"] += 1
                return self._send({"created": made})

            if u.path == "/api/tags/clear":
                state["tags"].clear(); state["tagsRev"] += 1
                return self._send({"ok": True})
        self._send({"error": "not found"}, 404)

    def do_DELETE(self):
        u = urlparse(self.path)
        with lock:
            if u.path == "/api/tags":
                state["tags"].clear(); state["tagsRev"] += 1
                return self._send({"ok": True})
            if u.path.startswith("/api/tags/"):
                tid = u.path.rsplit("/", 1)[-1]
                gone = state["tags"].pop(tid, None)
                if gone:
                    state["tagsRev"] += 1
                return self._send({"deleted": bool(gone)}, 200 if gone else 404)
        self._send({"error": "not found"}, 404)


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"tb-atc on http://{HOST}:{PORT}/  (web root: {WEB})", flush=True)
    srv.serve_forever()
