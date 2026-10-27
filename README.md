# おばけ探偵事務所

Google Chat・Gmail・Zulip・GitLab を横断して、「あの話ってどうなったっけ？」を調べてくれるAIチャット。
LLMはローカルLLM（OpenAI互換エンドポイント）を使い、データは手元のPCから外に出さない。

仕様・制約は [docs/SPEC.md](docs/SPEC.md)。

## 構成

開発予定は [docs/ROADMAP.md](docs/ROADMAP.md)。

- `app/agent.py` … 所長と所員のチーム調査。所員はLangGraphのツールループで並列に動き、所長は報告が足りなければ再調査を頼む
- `app/characters.py` … 所長・所員の名前と性格の既定値。画面（ヘッダーの「キャラクター」）で変えた内容はDBに保存して優先する
- `app/sources/` … 検索ソースの定義（ダミー2つ、GitHub、GitLab、Zulip、MCPサーバー）
- `app/web.py` + `app/static/` … ローカルWeb UI（標準ライブラリのみ）
- `app/db.py` … 会話の保存（SQLite、`data/chat.db`）
- `mock_llm/server.py` … OpenAI互換のフェイクLLM。ローカルLLMなしでループを検証する用
- `tests/` … ソースのテスト。偽のAPIサーバーを相手にするので実データには触れない

## 動かし方（WSL）

```sh
cp .env.example .env          # デフォルトはモックLLM向け
docker compose up -d --build  # → http://localhost:8080
```

`app/` はボリュームでマウントしているので、HTML/CSS/JSの変更はリロードで反映される。
Pythonを変更したら `docker compose restart agent`。

## 検索ソースを設定する

`.env` にトークンを書いたソースだけが画面に出る（書き方は `.env.example`）。トークンは自分のアカウントのものを使う。

| ソース | 必要な設定 | 調べるもの |
|---|---|---|
| GitHub | `GITHUB_TOKEN`（`GITHUB_SCOPE` で範囲を絞れる） | Issue・Pull Request とコメント |
| GitLab | `GITLAB_URL` `GITLAB_TOKEN`（`GITLAB_GROUP` で範囲を絞れる） | Issue・Merge Request とコメント（検索はタイトルと本文だけ） |
| Zulip | `ZULIP_URL` `ZULIP_EMAIL` `ZULIP_API_KEY` | チャンネルのメッセージとDM、トピック単位の流れ |

画面の「接続確認」で、LLMと各ソースに実際につながるかを確かめられる。

GitHub・GitLab・Zulip は、入力欄のソースの横の「▾」から、調べるリポジトリ・プロジェクト・チャンネルを質問ごとに選べる（選んだ内容はブラウザに残る）。選ぶと `.env` の `GITHUB_SCOPE` や `GITLAB_GROUP` より優先する。何も選ばなければ `.env` の設定どおりに調べる。

### MCPサーバーを足す

`data/mcp.json` に書いたMCPサーバーが、1つずつ所員の担当ソースになる（書き方は Claude Desktop などと同じ `mcpServers` の形）。

```json
{
  "mcpServers": {
    "wiki": { "label": "Wiki", "url": "http://host.docker.internal:9000/mcp", "headers": { "Authorization": "Bearer xxx" } },
    "notes": { "label": "メモ", "command": "python", "args": ["/work/data/notes_server.py"] }
  }
}
```

- `url` はHTTP（Streamable HTTP。`"type": "sse"` ならSSE）でつなぐ。自分のPCで動かしているサーバーには `host.docker.internal` でつながる
- `command` はコンテナの中でサーバーを起動する。コンテナに入っていないコマンド（`npx` 等）は使えないので、HTTPでつなぐほうが手軽
- 起動したときにつなぐので、書き換えたらアプリを再起動する

Zulipの全文検索で日本語をうまく拾うには、Zulip側で PGroonga が有効になっている必要がある（Zulipの管理者の設定）。

## テスト

```sh
docker compose run --rm -v ./tests:/work/tests agent python -m unittest
```

## 配布する

```sh
python scripts/make_dist.py --env dist.env   # → dist/ai-chat-YYYYMMDD.zip
```

`dist.env` には配布先で使うLLMの設定（`.env.example` の LLM の部分）だけを書く。zipに `.env` として同梱される。
`dist.env` と `dist/` はコミットしない（.gitignore 済み）。個人のトークンが書かれていると zip は作られない。

受け取った人は、zipを展開して `.env` に自分のトークン（検索ソースの設定）を書き足し、`docker compose up -d --build` する。

## LLMを切り替える

`.env` の `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` を書き換えて `docker compose up -d`（例は `.env.example`）。
エンドポイントは OpenAI互換（`/v1/chat/completions` + `tools`）であることを前提にしている。

> ローカルLLM構成での実データ確認は、Claude Code を通さず本人が行う（SPEC の制約）。
> `data/chat.db` にも実データの検索結果が入るので、Claude Code に読ませない。
