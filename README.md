# おばけ探偵事務所

Google Chat・Gmail・Zulip・GitLab を横断して、「あの話ってどうなったっけ？」を調べてくれるAIチャット。
LLMはローカルLLM（OpenAI互換エンドポイント）を使い、データは手元のPCから外に出さない。

仕様・制約は [docs/SPEC.md](docs/SPEC.md)。

## 構成

開発予定は [docs/ROADMAP.md](docs/ROADMAP.md)。

- `app/jobs.py` … 調査中のターンと進み具合。調査は画面と切り離して最後まで走り、画面には1本のストリームで配る
- `app/agent.py` … 所長と所員のチーム調査。所員はLangGraphのツールループで並列に動き、所長は報告が足りなければ再調査を頼む。調査のあとに所長がメモに書き足す
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

ヘッダーの「ソース」の「接続確認」で、LLMと各ソースに実際につながるかを確かめられる。

GitHub・GitLab・Zulip は、入力欄のソースの横の「▾」から、調べるリポジトリ・プロジェクト・チャンネルを選べる。選ぶと `.env` の `GITHUB_SCOPE` や `GITLAB_GROUP` より優先する。何も選ばなければ `.env` の設定どおりに調べる。

## フォルダとメモ

- 調査は案件ごとにフォルダにまとめられる（サイドバーの「フォルダ」）。入力欄で選んだソースと範囲はフォルダごとに覚えるので、案件Xのフォルダでは案件Xのチャンネルやリポジトリを調べる、といった使い分けができる
- ヘッダーの「メモ」に、社内での呼び方・どの話がどこにあるか・回答の好みなどを書いておくと、所長と所員が調べるときに読む。全体のメモはすべての調査で、フォルダのメモはそのフォルダの調査だけで読む
- 調査のあとに、所長が次の調査でも役立つ前提を自分でメモに書き足す（確認はしない。何を書いたかは回答の下に出るので、違っていればメモを直す）
- メモはそのままプロンプトに入るので、それぞれ2000文字まで

## 調べている間に別のことをする

調査は画面とは切り離してサーバーで最後まで走るので、待たずに別の調査を開いたり、新しい調査を頼んだりできる（同じ調査の中では1件ずつ）。
画面をリロードしたり閉じたりしても調査は続き、開き直すと進み具合の続きが出る。

回答が届くと:

- サイドバーのその調査に点が付く（開くと消える）。調査中の調査には「調査中」と出る
- 別の調査を見ていたら、右上に知らせが出る（「開く」でその調査へ）
- ブラウザのタブを裏に回していたら、デスクトップ通知が出る（最初に質問したときに、通知してよいかブラウザが聞く）
- タブの題名に、まだ見ていない回答の数と調査中の数が出る

## 続報を調べる

回答の下の「その後どうなった？」で、その件の続報だけを調べる（最後の回答にだけ出る）。
前回の回答を調べた日以降に絞って検索し、所長は前回から何が変わったかだけを答える。
GitHub・GitLab・Zulip は検索の日付で確実に絞る。MCPサーバーは、そう指示するだけ（ツールに日付の指定があるかはサーバーしだい）。

### MCPサーバーを足す

ヘッダーの「ソース」から、MCPサーバーを足したり直したり外したりできる。保存するとその場でつなぎ、再起動はいらない。
足したMCPサーバーは、1つずつ所員の担当ソースになる。

設定は `data/mcp.json` に書かれる（書き方は Claude Desktop などと同じ `mcpServers` の形）。手で書いてもよい（そのときはアプリを再起動する）。

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

所員は並列に動くが、LLMを同時に呼ぶのは `LLM_CONCURRENCY`（既定 2）件まで。ローカルLLMのサーバーが詰まるならこれを 1 にする。
1回の呼び出しは `LLM_TIMEOUT` 秒（既定 300）で打ち切る。

### 画像を読めるモデル

画像を読めるかどうかは、`LLM_MODEL` から `app/models.json` の対応表で自動で決まる（設定で切り替えるものではない）。
表にないモデルは画像を読めないものとして扱うので、使うモデルが画像を読めるなら表に1行足す（上から順に照らし、最初に合った行を使う。`*` は任意の文字列）。

画像を読めるモデルのときは:

- 入力欄に画像を添えられる（貼り付け・ドラッグ・「画像を添える」。4枚・1枚5MBまで。長い辺が1600pxより大きい画像は縮めて送る）。所長が調べる前に1回だけ読んで文字に起こし、所員にはその文字を渡す
- 所員が検索結果の中の画像（Zulip・GitHub・GitLab に貼られた画像）を、必要なときだけ自分のトークンで取ってきて読む（1回の調査で3枚まで）。GitLab の画像は API（GitLab 17.4 以降）で取る

> ローカルLLM構成での実データ確認は、Claude Code を通さず本人が行う（SPEC の制約）。
> `data/chat.db` にも実データの検索結果が入るので、Claude Code に読ませない。
