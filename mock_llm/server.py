"""OpenAI互換 /v1/chat/completions のフェイク実装（標準ライブラリのみ）。

挙動:
- 直前がユーザー発話でツールが渡されていれば、query だけで呼べるツールを全部、質問文そのままを query にして呼ぶ
- 直前がツール結果なら、その結果を要約したことにして最終回答を返す
- ツールなしで呼ばれたら、受け取った内容をそのまま返す
"""

import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer


def _completion(model: str, message: dict, finish_reason: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def respond(req: dict) -> dict:
    model = req.get("model", "mock")
    last = req["messages"][-1]
    tools = [t for t in req.get("tools") or [] if set(t["function"]["parameters"].get("required", [])) <= {"query"}]

    if last["role"] == "user" and tools:
        args = json.dumps({"query": last["content"]}, ensure_ascii=False)
        calls = [
            {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": t["function"]["name"], "arguments": args}}
            for t in tools
        ]
        return _completion(model, {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls")

    if last["role"] == "tool":
        content = f"[mock] 検索結果をもとに回答します:\n{last['content']}"
    else:
        # ツールなしで呼ばれた（所長のまとめ）。受け取った内容をそのまま返す
        content = f"[mock] まとめます:\n{last['content']}"
    return _completion(model, {"role": "assistant", "content": content}, "stop")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps(respond(req), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
