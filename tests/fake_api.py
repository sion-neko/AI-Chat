"""テスト用の偽APIサーバー。パスごとに決めたJSONを返し、受けたリクエストを記録する。"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeApi:
    def __init__(self, routes: dict):
        """routes: {パス: 返すJSON}。値が (status, JSON) のタプルならそのステータスで返す。"""
        self.routes = routes
        self.requests: list[dict] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                api.requests.append({"path": url.path, "params": dict(urllib.parse.parse_qsl(url.query)), "headers": dict(self.headers)})
                status, body = 404, {"message": "Not Found"}
                if url.path in api.routes:
                    route = api.routes[url.path]
                    status, body = route if isinstance(route, tuple) else (200, route)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

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
