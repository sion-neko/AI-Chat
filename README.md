# おばけ探偵事務所

Google Chat・Gmail・Zulip・GitLab を横断して、「あの話ってどうなったっけ？」を調べてくれるAIチャット。
LLMはローカルLLM（OpenAI互換エンドポイント）を使い、データは手元のPCから外に出さない。

仕様・制約は [docs/SPEC.md](docs/SPEC.md)。

## 構成

開発予定は [docs/ROADMAP.md](docs/ROADMAP.md)。

- `app/agent.py` … 所長と所員のチーム調査。所員はLangGraphのツールループで並列に動く
- `app/characters.py` … 所長・所員の名前と性格
- `app/sources/` … 検索ソースの定義。今はダミー2つ
- `app/web.py` + `app/static/` … ローカルWeb UI（標準ライブラリのみ）
- `app/db.py` … 会話の保存（SQLite、`data/chat.db`）
- `mock_llm/server.py` … OpenAI互換のフェイクLLM。ローカルLLMなしでループを検証する用

## 動かし方（WSL）

```sh
cp .env.example .env          # デフォルトはモックLLM向け
docker compose up -d --build  # → http://localhost:8080
```

`app/` はボリュームでマウントしているので、HTML/CSS/JSの変更はリロードで反映される。
Pythonを変更したら `docker compose restart agent`。

## LLMを切り替える

`.env` の `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` を書き換えて `docker compose up -d`（例は `.env.example`）。
エンドポイントは OpenAI互換（`/v1/chat/completions` + `tools`）であることを前提にしている。

> ローカルLLM構成での実データ確認は、Claude Code を通さず本人が行う（SPEC の制約）。
> `data/chat.db` にも実データの検索結果が入るので、Claude Code に読ませない。
