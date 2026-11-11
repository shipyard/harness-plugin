"""Fake Shipyard API and payload builders shared by the tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


def env_payload(**attrs):
    base = {
        "url": "https://app-pr-1.example.shipyard.host",
        "bypass_token": "bypass-123",
        "ready": True,
        "stopped": False,
        "retired": False,
        "processing": False,
        "projects": [],
    }
    base.update(attrs)
    return {"data": [{"id": "env-uuid", "attributes": base}]}


def detail_payload(additional_urls=None):
    return {
        "data": {
            "id": "env-uuid",
            "attributes": {"additional_urls": additional_urls or {}},
        }
    }


class FakeShipyard:
    """Local HTTP server that replays queued responses.

    GET /api/v1/environment?... (list) pops list_responses. Like the real API,
    the list response has no additional_urls; GET /api/v1/environment/<id>
    (detail) pops detail_responses, defaulting to no additional URLs.
    """

    def __init__(self):
        self.list_responses = []  # list of (status, body)
        self.detail_responses = []
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.requests.append(("GET", self.path, headers))
                if urlparse(self.path).path == "/api/v1/environment":
                    status, body = fake.list_responses.pop(0)
                elif fake.detail_responses:
                    status, body = fake.detail_responses.pop(0)
                else:
                    status, body = 200, detail_payload()
                self._reply(status, body)

            def do_POST(self):
                fake.requests.append(("POST", self.path, dict(self.headers)))
                self._reply(200, {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def list_requests(self):
        return [
            r
            for r in self.requests
            if r[0] == "GET" and urlparse(r[1]).path == "/api/v1/environment"
        ]

    def close(self):
        self.server.shutdown()
