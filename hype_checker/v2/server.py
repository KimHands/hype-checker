"""Webhook HTTP endpoint. Answers fast: verify, queue, 200. All work happens in the worker (design-v2 §3)."""

import json
import re
import sys
import threading
import time
from socket import SHUT_RDWR
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

from hype_checker.v2.webhook import media_link_shape, matched_secret, parse_shares, signature_shaped, unknown_senders, verify_subscription

PATH = "/webhook"
MAX_BODY = 1_000_000
READ_TIMEOUT_S = 10   # per socket operation (headers)
REQUEST_DEADLINE_S = 10   # whole body, however slowly it drips
MAX_CONNECTIONS = 8
CONNECTION_DEADLINE_S = 15   # whole connection, headers included: slow headers cannot hold a slot longer


class BodyTooSlow(Exception):
    pass


class BoundedServer(ThreadingHTTPServer):
    """Caps concurrent connections so unauthenticated slow clients cannot exhaust threads or descriptors."""

    daemon_threads = True

    def __init__(self, *args, max_connections=MAX_CONNECTIONS, deadline_s=CONNECTION_DEADLINE_S, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._deadline_s = deadline_s

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        timer = threading.Timer(self._deadline_s, _hang_up, (request,))
        try:
            timer.start()
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            self._slots.release()


def _hang_up(request):
    try:
        request.shutdown(SHUT_RDWR)
    except OSError:
        pass


def read_body(rfile, connection, length, deadline_s=REQUEST_DEADLINE_S):
    deadline, chunks, received = time.monotonic() + deadline_s, [], 0
    while received < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BodyTooSlow()
        connection.settimeout(remaining)
        try:
            chunk = rfile.read1(min(65536, length - received))
        except TimeoutError:
            raise BodyTooSlow() from None
        if not chunk:
            raise BodyTooSlow()
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def make_handler(config, inbox, clock=time.time, path=PATH):
    logged_senders = set()   # pending diagnosis: each sender id once per process, not on every retry
    class Handler(BaseHTTPRequestHandler):
        timeout = READ_TIMEOUT_S
        server_version = "hype-check"
        sys_version = ""

        def _reply(self, status, body=b""):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            challenge = verify_subscription(dict(parse_qsl(url.query)), config) if url.path == path else None
            self._reply(200, challenge.encode()) if challenge else self._reply(403)

        def do_POST(self):
            length = self.headers.get("Content-Length", "")
            length = int(length) if re.fullmatch(r"\d{1,7}", length) else 0
            if urlparse(self.path).path != path or not 0 < length <= MAX_BODY:
                return self._reply(400)
            if not signature_shaped(self.headers.get("X-Hub-Signature-256")):
                return self._reply(403)
            try:
                body = read_body(self.rfile, self.connection, length)
            except BodyTooSlow:
                self.close_connection = True
                return
            secret = matched_secret(body, self.headers.get("X-Hub-Signature-256"), config)
            if secret is None:
                return self._reply(403)
            print(f"signature ok · secret #{secret}", file=sys.stderr)
            try:
                payload = json.loads(body)
            except ValueError:
                return self._reply(400)
            for sender in set(unknown_senders(payload, config)) - logged_senders:
                logged_senders.add(sender)
                print(f"pending: first DM sender {sender} — put it in IG_ALLOWED_SENDERS", file=sys.stderr)
            for share in parse_shares(payload, config, clock()):
                if config.video == "on" and share.has_media_url and not share.media_url:
                    print(f"media link refused: {media_link_shape(payload)}", file=sys.stderr)
                inbox.enqueue(share)
            self._reply(200, b"EVENT_RECEIVED")

        def log_message(self, *args):
            pass

    return Handler
