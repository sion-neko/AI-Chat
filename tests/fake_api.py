"""テスト用の偽APIサーバー。パスごとに決めた応答を返し、受けたリクエストを記録する。"""

import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Raw:
    """JSON以外の応答（画像やリダイレクト）。"""

    body: bytes = b""
    content_type: str = "application/octet-stream"
    status: int = 200
    headers: dict = field(default_factory=dict)


class FakeApi:
    def __init__(self, routes: dict):
        """routes: {パス: 返すJSON}。値が (status, JSON) のタプルならそのステータスで、Raw ならそのまま返す。"""
        self.routes = routes
        self.requests: list[dict] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                api.requests.append({"path": url.path, "params": dict(urllib.parse.parse_qsl(url.query)), "headers": {k.lower(): v for k, v in self.headers.items()}})
                route = api.routes.get(url.path, (404, {"message": "Not Found"}))
                if not isinstance(route, Raw):
                    status, body = route if isinstance(route, tuple) else (200, route)
                    route = Raw(json.dumps(body).encode(), "application/json", status)
                self.send_response(route.status)
                self.send_header("Content-Type", route.content_type)
                self.send_header("Content-Length", str(len(route.body)))
                for key, value in route.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(route.body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
