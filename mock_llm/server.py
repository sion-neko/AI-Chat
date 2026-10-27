"""OpenAI互換 /v1/chat/completions のフェイク実装（標準ライブラリのみ）。

挙動:
- 直前がユーザー発話でツールが渡されていれば、query だけで呼べるツールを全部、質問文そのままを query にして呼ぶ
- 所長に再調査のツールが渡され、質問に「再調査」が含まれていれば、全所員に再調査を頼む
- 所長の受付（ask_user が渡される）では、質問に「あの件」が含まれていれば聞き返し、そうでなければ調べる
- 所長のメモ（remember が渡される）では、質問に「覚えて」が含まれていればメモに書き足し、そうでなければ何もしない
- 画像を見せられたら、決まった読み取り結果を返す
- 直前がツール結果なら、その結果を要約したことにして最終回答を返す
- ツールなしで呼ばれたら、受け取った内容をそのまま返す
- stream: true なら、同じ内容を少しずつSSEで返す（画面で逐次表示を確かめられるよう、間を置く）
- MOCK_DELAY（秒）を指定すると、毎回その分だけ待ってから返す（ローカルLLMの遅さを真似る）
"""

import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _completion(model: str, message: dict, finish_reason: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _call(name: str, args: dict) -> dict:
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def _text(content) -> str:
    """メッセージの内容を文字列にする（画像つきのときは、文字の部分だけをつなげる）。"""
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
    return content or ""


def respond(req: dict) -> dict:
    model = req.get("model", "mock")
    last = {**req["messages"][-1], "content": _text(req["messages"][-1].get("content"))}

    # 画像を見せられたら、読み取ったことにして返す
    raw = req["messages"][-1].get("content")
    if isinstance(raw, list) and any(part.get("type") == "image_url" for part in raw):
        content = "[mock] 画像の内容: エラー画面。「Disk quota exceeded」と表示されている（mon01、2026-10-07 03:12）"
        return _completion(model, {"role": "assistant", "content": content}, "stop")

    all_tools = {t["function"]["name"]: t["function"] for t in req.get("tools") or []}
    tools = [name for name, f in all_tools.items() if set(f["parameters"].get("required", [])) <= {"query"}]

    if "ask_user" in all_tools:
        if "あの件" in last["content"]:
            calls = [_call("ask_user", {"question": "[mock] どの件のことですか？（例: 監視サーバーのディスク増設、VPN証明書の更新）"})]
            return _completion(model, {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls")
        return _completion(model, {"role": "assistant", "content": "調査します"}, "stop")

    if "remember" in all_tools:
        if "覚えて" in last["content"]:
            scope = all_tools["remember"]["parameters"]["properties"]["scope"]["enum"][0]
            calls = [_call("remember", {"scope": scope, "note": "[mock] 「監視サーバー」は mon01 のこと"})]
            return _completion(model, {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls")
        return _completion(model, {"role": "assistant", "content": "なし"}, "stop")

    if last["role"] == "user" and "request_reinvestigation" in all_tools and "再調査" in last["content"]:
        sources = all_tools["request_reinvestigation"]["parameters"]["properties"]["source"]["enum"]
        calls = [_call("request_reinvestigation", {"source": s, "instruction": "[mock] 別の言い方でもう一度調べて"}) for s in sources]
        return _completion(model, {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls")

    if last["role"] == "user" and tools:
        calls = [_call(name, {"query": last["content"]}) for name in tools]
        return _completion(model, {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls")

    if last["role"] == "tool":
        content = f"[mock] 検索結果をもとに回答します:\n{last['content']}"
    else:
        # ツールなしで呼ばれた（所長のまとめ）。受け取った内容をそのまま返す
        content = f"[mock] まとめます:\n{last['content']}"
    return _completion(model, {"role": "assistant", "content": content}, "stop")


def chunks(completion: dict):
    """1回分の応答を、ストリーミングのチャンク（chat.completion.chunk）に分ける。"""
    choice = completion["choices"][0]
    message = choice["message"]
    base = {k: completion[k] for k in ("id", "created", "model")} | {"object": "chat.completion.chunk"}

    def chunk(delta: dict, finish_reason=None) -> dict:
        return base | {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

    yield chunk({"role": "assistant", "content": ""})
    for i, call in enumerate(message.get("tool_calls") or []):
        yield chunk({"tool_calls": [{"index": i, **call}]})
    content = message.get("content") or ""
    for start in range(0, len(content), 12):
        yield chunk({"content": content[start : start + 12]})
    yield chunk({}, choice["finish_reason"])


DELAY = float(os.environ.get("MOCK_DELAY", "0"))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        time.sleep(DELAY)
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for c in chunks(respond(req)):
                self.wfile.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.03)
            self.wfile.write(b"data: [DONE]\n\n")
            return
        body = json.dumps(respond(req), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
