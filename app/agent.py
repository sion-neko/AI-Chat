"""所長と所員によるチーム調査。

1ターンの流れ:
  0. 所長が依頼を受け付ける。何の話か特定できないほど曖昧なら、調べずに聞き返してターンを終える
  1. 選ばれたソースごとに所員（LangGraphのツールループ）を並列で走らせる
     所員に渡すのはユーザーの質問だけ。他の所員の結果や過去の回答は見せない
  2. 所長が所員の報告を読む。足りなければ所員に再調査を頼める（FOLLOWUP_ROUNDS 回まで）
     再調査では、その所員の前回の報告と所長の依頼を渡す
  3. 所長が報告をまとめて回答する
  4. 所長が、次の調査でも役に立つ前提（社内の呼び方や話のありか等）を事務所のメモに書き足す

所長と所員のプロンプトには、事務所のメモ（全体と、調査が入っているフォルダの分）を入れる。

画像を読めるモデル（app/models.json）のときは:
  - 質問に添えられた画像を、受付の前に1回だけ文字に起こし、質問に添えて所長と所員に渡す
  - 所員に view_image を持たせ、検索結果の中の画像を必要なときだけ読ませる（文字に起こして返す）

進み具合は emit に渡したイベントで通知する。
LLMとのやりとりは Trace に記録し、返り値の trace に入れる（あとから画面で確かめられるように）。
"""

import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date
from concurrent.futures import ThreadPoolExecutor

import base64

import openai
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolInvocationError

from app import characters, links, models
from app.sources import Source
from app.sources import scope as search_scope
from app.sources.http import ApiError

Emit = Callable[[dict], None]


class Trace:
    """1ターン分のLLMとのやりとり。所員は並列に動くが、list.append はスレッドをまたいでも壊れない。"""

    def __init__(self):
        self.calls: list[dict] = []

    def record(self, who: str, messages: list, tools: list, response: AIMessage, seconds: float):
        usage = response.usage_metadata or {}
        self.calls.append(
            {
                "who": who,
                "seconds": round(seconds, 2),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "tools": [t["function"]["name"] if isinstance(t, dict) else t.name for t in tools],
                "messages": [_message_view(m) for m in messages],
                "response": _message_view(response),
            }
        )


def _message_view(msg) -> dict:
    view = {"role": msg.type, "content": msg.text}
    # 画像は記録に入れない（大きいので）。何枚あったかだけ残す
    if isinstance(msg.content, list):
        images = sum(1 for b in msg.content if isinstance(b, dict) and b.get("type") == "image_url")
        if images:
            view["content"] += f"\n[画像{images}枚]"
    if getattr(msg, "tool_calls", None):
        view["tool_calls"] = [{"name": c["name"], "args": c["args"]} for c in msg.tool_calls]
    return view


class Cancelled(BaseException):
    """調査が中止された。emit が投げる。

    所員の失敗（except Exception）として握りつぶされず、ターン全体を止めるよう BaseException にしている。
    """

# 所員1人あたりのグラフのステップ上限（LLM呼び出しとツール実行の合計）。長引く調査を打ち切るため
MEMBER_RECURSION_LIMIT = 12
# 所長が再調査を頼める回数（1ターンあたり）。頼んだ報告を受けたら必ず回答する
FOLLOWUP_ROUNDS = 1
# 所員1人が1回の調査で view_image で読める画像の枚数
VIEW_IMAGE_LIMIT = 3

IMAGE_PROMPT = """この画像に写っているものを、検索や調査に使えるよう文字に起こしてください。
- 画面やエラーのスクリーンショットなら、表示されている文字（エラーメッセージ・画面名・ホスト名・日時など）をそのまま書き写す
- グラフなら、何のグラフで、いつ・どう変化しているか
- 図や写真なら、何が写っているか
- 推測はしない。読み取れない部分は「読み取れない」と書く"""

MEMBER_VISION_NOTE = """
- 検索結果の画像（スクリーンショットやグラフ）が話の核心にありそうなときは、view_image で中身を読める。読むのは本当に必要な画像だけにする"""

MEMBER_PROMPT = """あなたは「おばけ探偵事務所」の所員「{name}」です。{persona}
担当は「{source}」です。依頼人の質問について、担当ソースだけを調べて所長に報告します。
今日は {today} です。

- 必ず検索ツールで根拠を探す
- ヒットしなければ検索語を言い換えて複数回検索する（同義語・略語・英語表記など）
- 期間が示されていれば日付で絞り込む（「先月」「最近」なども今日を基準に YYYY-MM-DD に直す）
- 報告は見つかった事実だけを時系列の箇条書きにし、各項目に根拠のURLを付ける
- 見つからなければ「見つからなかった」と報告する。推測で補わない
- 口調はキャラクターに合わせてよいが、事実の正確さを最優先する"""

DIRECTOR_PROMPT = """あなたは「おばけ探偵事務所」の所長「{name}」です。{persona}
所員の調査報告をもとに、依頼人の質問に答えます。今日は {today} です。

- 報告に書かれた事実だけで答える。推測で補わない
- 複数の報告を突き合わせ、経緯を時系列でまとめる
- 回答には根拠のURLを必ず付ける。URLは所員の報告に書かれたものを一字一句そのまま使い、自分で作らない
- どの所員も見つけられなかった場合は、そう伝える
- 報告では答えきれないとき（結末が分からない、報告どうしが食い違う、検索語や期間の見落としがありそう等）は、
  request_reinvestigation で所員に再調査を頼める。頼むのは本当に必要なときだけにし、何をどう調べ直すかを具体的に書く"""

# 調べる場所を選ばずに頼まれたとき（相談）。所員は動かさず、所長がそのまま答える
CHAT_PROMPT = """あなたは「おばけ探偵事務所」の所長「{name}」です。{persona}
今回は所員に調べさせず、依頼人の頼みにあなたが直接応えます。今日は {today} です。

- 貼られた文章の要約・言い換え・翻訳・下書き・整理・相談などに、そのまま応える
- 社内の出来事や経緯など、調べないと分からない事実は推測で答えない。
  「調べる場所を選んで依頼すれば、所員が調べます」と伝える
- 回答はMarkdownで書いてよい"""

# 続報を調べるとき（「その後どうなった？」）に足す
MEMBER_SINCE_NOTE = """

## 続報の調査
この件は {since} に一度調べています。{since} 以降の新しい動きだけを探してください。
- 検索ツールの since は {since} より前にできない（指定しても {since} からになる）
- 報告するのは {since} 以降に起きたことだけ。それより前の経緯は書かない
- 新しい動きがなければ「{since} 以降の動きは見つからなかった」と報告する"""

DIRECTOR_SINCE_NOTE = """
- 今回は続報の確認。前回 {since} の回答から何が変わったか（決まったこと・進んだこと・新しく出た問題）だけを答える。
  前回の回答の繰り返しはしない。新しい動きがなければ、そう短く伝える"""

INTAKE_PROMPT = """あなたは「おばけ探偵事務所」の所長「{name}」です。{persona}
依頼人の質問を受け付け、所員に調べさせる前に、聞き返す必要があるかを判断します。今日は {today} です。
所員はチャットやIssueなどを検索して調べます。

- 何の話か特定できず、検索語すら決められないときだけ、ask_user で依頼人に1つだけ聞き返す
  （例:「あの件どうなった？」だけで、これまでのやりとりからも何の件か分からない）
- 少しでも手がかり（固有名詞・システム名・時期など）があれば聞き返さない。所員が調べて絞り込める
- 聞き返すときは、答えやすいよう候補や例を添える
- 聞き返さないときは、何も呼ばずに「調査します」とだけ答える"""

REMEMBER_PROMPT = """あなたは「おばけ探偵事務所」の所長「{name}」です。
調査を終えたところです。次の調査でも役に立つ「前提」があれば、remember で事務所のメモに書き足します。今日は {today} です。

書き足すもの:
- 社内での呼び方と、正式な名前・システム名・リポジトリ名などの対応（例:「監視サーバー」は mon01）
- ある話題が、どのソース・リポジトリ・チャンネルにあるか
- 誰が何の担当か
- 依頼人が示した、回答の書き方の好み
書き足さないもの:
- 今回の質問への答えそのもの（調査の記録に残るので不要）
- その時だけの状況（「今は障害対応中」など）、推測、メモにすでにあること

- 1件は1行で短く書く。書き足すのは多くても{limit}件
- {scopes}
- 書き足すことがなければ、何も呼ばずに「なし」とだけ答える"""

# 所長に渡す過去のやりとりのターン数（最初のやりとりは別に渡す）。ローカルLLMの文脈を使い切らないように
HISTORY_TURNS = 10

# 1回の調査でメモに書き足す件数の上限
REMEMBER_LIMIT = 3

TITLE_PROMPT = """依頼人の質問と、それへの回答を読み、この調査にあとで一覧から探しやすい短い題名を付けてください。
- {length}文字以内。何の件かが分かる名詞句にする（例: 監視サーバーのディスク増設、VPN証明書の更新手順）
- 題名だけを1行で書く。かぎかっこや句点は付けない"""
TITLE_LENGTH = 24

# LLMを同時に呼ぶ数の上限。所員は並列に動くので、GPU1台のローカルLLMサーバーが詰まらないよう絞る
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY") or 2)
# 1回の呼び出しを待つ上限（秒）。サーバーが固まっても調査が終わらなくならないように
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT") or 300)

_llm = ChatOpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["LLM_API_KEY"],
    model=os.environ["LLM_MODEL"],
    timeout=LLM_TIMEOUT,
    max_retries=1,
)
_llm_slots = threading.BoundedSemaphore(LLM_CONCURRENCY)


@contextmanager
def _llm_slot():
    """LLMを呼ぶ間だけ枠を1つ使う。LLMの呼び出しはすべてこの中で行う。"""
    with _llm_slots:
        yield


def explain_error(e: Exception) -> str:
    """調査が失敗したときに画面に出す文。LLMのよくある失敗は、何を確かめればよいかを添える。"""
    detail = f"{type(e).__name__}: {e}"
    if isinstance(e, openai.APITimeoutError):
        hint = f"LLMが{LLM_TIMEOUT:g}秒のうちに答えませんでした。LLMのサーバーが混んでいないか確かめ、必要なら .env の LLM_TIMEOUT を延ばしてください"
    elif isinstance(e, openai.APIConnectionError):
        hint = f"LLMにつながりません（{os.environ['LLM_BASE_URL']}）。LLMのサーバーが動いているか、.env の LLM_BASE_URL を確かめてください"
    elif isinstance(e, openai.AuthenticationError):
        hint = "LLMに断られました。.env の LLM_API_KEY を確かめてください"
    elif isinstance(e, openai.NotFoundError):
        hint = f"LLMにモデル {os.environ['LLM_MODEL']} が見つかりません。.env の LLM_MODEL を確かめてください"
    elif isinstance(e, openai.BadRequestError) and any(w in str(e).lower() for w in ("context", "too long", "maximum", "token")):
        hint = "会話や貼った文章が長すぎて、LLMが一度に読める量を超えました。新しい調査で、必要な部分だけを貼って聞き直してください"
    else:
        return detail
    return f"{hint}（{detail}）"


def describe_image(data_url: str, purpose: str, trace: Trace, who: str) -> str:
    """画像を読めるモデルに画像を見せ、写っている内容を文字で返す。purpose は何の画像かの説明。"""
    content = [{"type": "text", "text": f"{IMAGE_PROMPT}\n\n{purpose}"}, {"type": "image_url", "image_url": {"url": data_url}}]
    messages = [HumanMessage(content=content)]
    started = time.monotonic()
    with _llm_slot():
        answer = _llm.invoke(messages)
    trace.record(who, messages, [], answer, time.monotonic() - started)
    return answer.text.strip()


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _view_image_tool(source: Source, trace: Trace, who: str) -> BaseTool:
    """所員が検索結果の画像を読むツール。画像はソースの認証で取ってきて、文字に起こして返す。

    ツールの結果に画像をそのまま入れられないOpenAI互換サーバーも多いので、別の呼び出しで文字にする。
    """
    count = 0

    def view_image(url: str) -> str:
        nonlocal count
        if count >= VIEW_IMAGE_LIMIT:
            raise ApiError(f"画像を読めるのは1回の調査で{VIEW_IMAGE_LIMIT}枚までです。読んだ内容で報告してください")
        count += 1
        data, mime = source.fetch_image(url)
        purpose = f"「{source.label}」の検索結果に含まれていた画像です（{url}）。"
        return describe_image(_data_url(data, mime), purpose, trace, f"{who}（画像）")

    return StructuredTool.from_function(
        view_image,
        name="view_image",
        description=(
            f"{source.label}の検索結果に含まれる画像（スクリーンショット・グラフ等）を見て、写っている内容を文字で返す。"
            f"url は検索結果に書かれた画像のURL（相対URLも可）。1回の調査で{VIEW_IMAGE_LIMIT}枚まで"
        ),
    )


def _tool_loop(system_prompt: str, tools: list[BaseTool], trace: Trace, who: str):
    llm = _llm.bind_tools(tools)

    def call_llm(state: MessagesState):
        messages = [SystemMessage(system_prompt), *state["messages"]]
        started = time.monotonic()
        with _llm_slot():
            response = llm.invoke(messages)
        trace.record(who, messages, tools, response, time.monotonic() - started)
        return {"messages": [response]}

    graph = StateGraph(MessagesState)
    graph.add_node("llm", call_llm)
    # 引数の誤りとAPIのエラーはLLMに返して、検索し直させる。それ以外は所員の調査失敗にする
    graph.add_node("tools", ToolNode(tools, handle_tool_errors=(ToolInvocationError, ApiError)))
    graph.add_edge(START, "llm")
    graph.add_conditional_edges("llm", tools_condition)
    graph.add_edge("tools", "llm")
    return graph.compile()


def run_member(
    source: Source,
    questions: list[str],
    emit: Emit,
    trace: Trace,
    scope: list[str] | None = None,
    followup: dict | None = None,
    memo: str = "",
    since: str | None = None,
) -> dict:
    """所員1人に調査させ、報告を返す。失敗しても例外は投げず、error に入れて返す。

    scope は画面で選んだ検索範囲（リポジトリやチャンネル）。None なら絞らない。
    followup は所長からの再調査の依頼 {"instruction": 依頼, "previous": 前回の報告}。
    memo はプロンプトに足す事務所のメモ（memo_section で作る）。
    since は続報を調べるときの前回の調査日（YYYY-MM-DD）。検索はその日より前を探さない。
    """
    character = characters.member(source.id, source.label)
    report = {"source": source.id, "label": source.label, "agent": character.name, "content": "", "steps": [], "error": None}
    if scope:
        report["scope"] = scope
    if followup:
        report["request"] = followup["instruction"]
    emit({"type": "member_start", "source": source.id, "agent": character.name})

    who = f"{character.name}（{source.label}）"
    tools = list(source.tools)
    prompt = MEMBER_PROMPT.format(name=character.name, persona=character.persona, source=source.label, today=date.today())
    if source.fetch_image and models.vision():
        tools.append(_view_image_tool(source, trace, who))
        prompt += MEMBER_VISION_NOTE
    prompt += memo
    *earlier, current = questions
    request = f"今回の質問: {current}"
    if earlier:
        request = "これまでの質問:\n" + "\n".join(f"- {q}" for q in earlier) + f"\n\n{request}"
    if scope:
        request += f"\n\n検索範囲（{source.scope_label}）は次に絞られています: {', '.join(scope)}"
    if since:
        request += MEMBER_SINCE_NOTE.format(since=since)
    if followup:
        request += f"\n\n## あなたの前回の報告\n{followup['previous']}\n\n## 所長からの再調査の依頼\n{followup['instruction']}"

    # このスレッドで動く検索ツールに、範囲を渡す
    search_scope.set(scope)
    search_scope.set_since(since)
    started = time.monotonic()
    called_at = {}  # ツール呼び出しID → 呼んだ時刻（結果が返るまでの時間を測る）
    try:
        graph = _tool_loop(prompt, tools, trace, who)
        stream = graph.stream(
            {"messages": [HumanMessage(request)]},
            {"recursion_limit": MEMBER_RECURSION_LIMIT},
            stream_mode="updates",
        )
        for update in stream:
            for msg in (m for node in update.values() for m in node["messages"]):
                if isinstance(msg, AIMessage) and msg.tool_calls:
                    for call in msg.tool_calls:
                        report["steps"].append({"id": call["id"], "tool": call["name"], "args": call["args"], "result": None})
                        called_at[call["id"]] = time.monotonic()
                        emit({"type": "tool", "source": source.id, "tool": call["name"], "args": call["args"]})
                elif isinstance(msg, ToolMessage):
                    for step in report["steps"]:
                        if step["id"] == msg.tool_call_id:
                            step["result"] = msg.text
                            step["seconds"] = round(time.monotonic() - called_at[step["id"]], 2)
                elif isinstance(msg, AIMessage):
                    report["content"] = msg.text
    except Exception as e:
        report["error"] = explain_error(e)

    report["seconds"] = round(time.monotonic() - started, 2)
    emit({"type": "member_done", "report": report})
    return report


def _report_text(report: dict) -> str:
    return f"（調査失敗: {report['error']}）" if report["error"] else report["content"] or "（報告なし）"


def _followup_tool(sources: list[Source]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "request_reinvestigation",
            "description": "所員に担当ソースの再調査を頼む。複数の所員に同時に頼める。結果は所員の報告として返る",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": [s.id for s in sources], "description": "再調査させる所員の担当ソースID"},
                    "instruction": {"type": "string", "description": "何を調べ直してほしいか（検索語・期間・確かめたい点など）"},
                },
                "required": ["source", "instruction"],
            },
        },
    }


def _stream(llm, messages: list, emit: Emit, trace: Trace, who: str, tools: list) -> AIMessage:
    """回答を受け取りながら、届いた文字を delta イベントで流す。ツール呼び出しも含めて全体をまとめて返す。"""
    started = time.monotonic()
    answer = None
    with _llm_slot():
        for chunk in llm.stream(messages):
            answer = chunk if answer is None else answer + chunk
            if chunk.text:
                emit({"type": "delta", "text": chunk.text})
    trace.record(who, messages, tools, answer, time.monotonic() - started)
    return answer


def run_director(
    director: characters.Character,
    history: list[dict],
    questions: list[str],
    reports: list[dict],
    sources: list[Source],
    emit: Emit,
    trace: Trace,
    memo: str = "",
    since: str | None = None,
) -> dict:
    """所長が報告を読み、必要なら再調査を頼んでから回答する。

    history は過去のターン（質問と所長の回答）。返り値の followups は再調査で増えた報告。
    since は続報を調べるときの前回の調査日。
    """
    prompt = DIRECTOR_PROMPT.format(name=director.name, persona=director.persona, today=date.today())
    if since:
        prompt += DIRECTOR_SINCE_NOTE.format(since=since)
    prompt += memo
    messages = [SystemMessage(prompt), *_history_messages(history)]

    sections = [f"### {r['agent']}（{r['label']}、ソースID: {r['source']}）\n{_report_text(r)}" for r in reports]
    messages.append(HumanMessage(f"{questions[-1]}\n\n## 所員の報告\n\n" + "\n\n".join(sections)))

    by_id = {s.id: s for s in sources}
    latest = {r["source"]: r for r in reports}
    followups = []
    for round_ in range(FOLLOWUP_ROUNDS + 1):
        # 最後の回はツールを渡さず、回答させる
        tools = [_followup_tool(sources)] if round_ < FOLLOWUP_ROUNDS else []
        llm = _llm.bind_tools(tools) if tools else _llm
        answer = _stream(llm, messages, emit, trace, f"{director.name}所長", tools)
        if not answer.tool_calls:
            break
        messages.append(answer)
        calls = [c for c in answer.tool_calls if c["args"].get("source") in by_id]

        def follow(call: dict) -> dict:
            source = by_id[call["args"]["source"]]
            emit({"type": "followup", "source": source.id, "agent": latest[source.id]["agent"], "instruction": call["args"]["instruction"]})
            previous = _report_text(latest[source.id])
            request = {"instruction": call["args"]["instruction"], "previous": previous}
            return run_member(source, questions, emit, trace, latest[source.id].get("scope"), request, memo, since)

        with ThreadPoolExecutor(max_workers=len(calls) or 1) as pool:
            results = dict(zip((c["id"] for c in calls), pool.map(follow, calls)))
        for call in answer.tool_calls:
            report = results.get(call["id"])
            if report:
                followups.append(report)
                latest[report["source"]] = report
            text = _report_text(report) if report else f"使えないソースです: {call['args'].get('source')}"
            messages.append(ToolMessage(text, tool_call_id=call["id"]))
        emit({"type": "director_start", "agent": director.name})

    # 設定値ではなく、LLMが実際に返したモデル名
    model = answer.response_metadata.get("model_name") or os.environ["LLM_MODEL"]
    return {"content": answer.text, "model": model, "followups": followups}


def check_llm() -> dict:
    """LLMに短い質問を1回送って、つながるか確かめる。結果は sources.check と同じ形。"""
    try:
        with _llm_slot():
            answer = _llm.invoke([HumanMessage("接続の確認です。「OK」とだけ返してください。")])
    except Exception as e:
        return {"state": "error", "message": explain_error(e)}
    model = answer.response_metadata.get("model_name") or os.environ["LLM_MODEL"]
    return {"state": "ok", "message": f"{model} が応答しました"}


ASK_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": "調べ始める前に、依頼人に1つだけ聞き返す",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "依頼人への質問（候補や例を添える）"}},
            "required": ["question"],
        },
    },
}


def user_text(turn: dict) -> str:
    """依頼人のターンの文章。画像が添えられていれば、文字に起こした内容も付ける。"""
    if turn.get("image_text"):
        return f"{turn['content']}\n\n（添えられた画像の内容）\n{turn['image_text']}"
    return turn["content"]


def _history_messages(history: list[dict]) -> list:
    """所長に渡す過去のやりとり。長い会話は、最初のやりとり（何の話か）と直近の HISTORY_TURNS ターンだけにする。"""
    if len(history) > HISTORY_TURNS + 2:
        history = history[:2] + history[-HISTORY_TURNS:]
    return [HumanMessage(user_text(t)) if t["role"] == "user" else AIMessage(t["content"]) for t in history]


def intake(director: characters.Character, history: list[dict], question: str, trace: Trace, memo: str = "") -> dict | None:
    """聞き返すべきなら {"question": 依頼人への質問, "model": 応答したモデル} を返す。調べてよければ None。

    受付は補助なので、LLMの呼び出しに失敗しても調査は止めない（None を返す）。
    メモに用語の対応などがあれば、それで分かる依頼は聞き返さない。
    """
    prompt = INTAKE_PROMPT.format(name=director.name, persona=director.persona, today=date.today()) + memo
    messages = [SystemMessage(prompt), *_history_messages(history), HumanMessage(question)]
    started = time.monotonic()
    try:
        with _llm_slot():
            answer = _llm.bind_tools([ASK_USER_TOOL]).invoke(messages)
    except Exception:
        return None
    trace.record(f"{director.name}所長（受付）", messages, [ASK_USER_TOOL], answer, time.monotonic() - started)
    for call in answer.tool_calls:
        if call["name"] == "ask_user" and str(call["args"].get("question", "")).strip():
            model = answer.response_metadata.get("model_name") or os.environ.get("LLM_MODEL", "")
            return {"question": call["args"]["question"].strip(), "model": model}
    return None


def memo_section(memos: dict | None) -> str:
    """プロンプトの最後に足す、事務所のメモ。memos は {"global": 全体のメモ, "folder": {"name", "memo"} か None}。"""
    if not memos:
        return ""
    parts = []
    if memos.get("global"):
        parts.append(f"### 全体\n{memos['global']}")
    if folder := memos.get("folder"):
        parts.append(f"### 案件「{folder['name']}」\nこの調査は案件「{folder['name']}」のものです。\n{folder['memo']}".rstrip())
    if not parts:
        return ""
    return (
        "\n\n## 事務所のメモ\n"
        "過去の調査で分かった前提（社内の呼び方、話がどこにあるか等）や、依頼人の好みです。"
        "検索語の言い換え、どこを調べるかの判断、回答の書き方に使ってください。"
        "ただしメモは回答の根拠にはせず、事実は必ず検索結果で確かめてください。\n\n" + "\n\n".join(parts)
    )


def _remember_tool(has_folder: bool) -> dict:
    scopes = ["folder", "global"] if has_folder else ["global"]
    return {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "事務所のメモに1行書き足す",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": scopes, "description": "folder: この案件のメモ / global: すべての調査で使うメモ"},
                    "note": {"type": "string", "description": "書き足す1行"},
                },
                "required": ["scope", "note"],
            },
        },
    }


def remember(
    director: characters.Character, questions: list[str], answer: str, memos: dict | None, trace: Trace, memo: str
) -> list[dict]:
    """調査を終えて、メモに書き足すことを決める。[{"scope": "folder" か "global", "note"}] を返す。

    書き足すかどうかを依頼人に確かめはしない（書いたものは画面に出し、メモの画面で直せる）。
    メモは補助なので、LLMの呼び出しに失敗しても何も書かずに終える。
    """
    has_folder = bool(memos and memos.get("folder"))
    scopes = (
        f"案件「{memos['folder']['name']}」だけの前提は folder、どの調査にも効くこと（依頼人の好みなど）は global に書く"
        if has_folder
        else "この調査はフォルダに入っていないので、どの調査にも効くことだけを global に書く"
    )
    prompt = REMEMBER_PROMPT.format(name=director.name, today=date.today(), limit=REMEMBER_LIMIT, scopes=scopes) + memo
    asked = "\n".join(f"- {q}" for q in questions)
    messages = [SystemMessage(prompt), HumanMessage(f"## 依頼人の質問\n{asked}\n\n## あなたの回答\n{answer}")]
    tool = _remember_tool(has_folder)
    started = time.monotonic()
    try:
        with _llm_slot():
            response = _llm.bind_tools([tool]).invoke(messages)
    except Exception:
        return []
    trace.record(f"{director.name}所長（メモ）", messages, [tool], response, time.monotonic() - started)
    allowed = tool["function"]["parameters"]["properties"]["scope"]["enum"]
    notes = []
    for call in response.tool_calls:
        note = " ".join(str(call["args"].get("note", "")).split())
        if call["name"] == "remember" and call["args"].get("scope") in allowed and note:
            notes.append({"scope": call["args"]["scope"], "note": note})
    return notes[:REMEMBER_LIMIT]


def make_title(questions: list[str], answer: str) -> str | None:
    """調査の題名を付ける。最初の質問がそのまま題名になると、「あの件どうなった？」のような一覧で見分けにくいものが並ぶため。

    題名は補助なので、LLMの呼び出しに失敗したり、使えない題名が返ったりしたら None。
    """
    asked = "\n".join(f"- {q}" for q in questions)
    messages = [SystemMessage(TITLE_PROMPT.format(length=TITLE_LENGTH)), HumanMessage(f"## 質問\n{asked}\n\n## 回答\n{answer[:2000]}")]
    try:
        with _llm_slot():
            response = _llm.invoke(messages)
    except Exception:
        return None
    lines = [line for line in response.text.splitlines() if line.strip()]
    title = lines[0].strip().strip("「」『』\"'#*。 ") if lines else ""
    return title[: TITLE_LENGTH * 2] or None


def consult(
    director: characters.Character, history: list[dict], asked: str, emit: Emit, trace: Trace, memo: str, extra: dict, started: float
) -> dict:
    """調べる場所を選ばずに頼まれたとき。所員は動かさず、所長がそのまま答える（聞き返しもメモへの書き足しもしない）。"""
    emit({"type": "director_start", "agent": director.name})
    prompt = CHAT_PROMPT.format(name=director.name, persona=director.persona, today=date.today()) + memo
    messages = [SystemMessage(prompt), *_history_messages(history), HumanMessage(asked)]
    answer = _stream(_llm, messages, emit, trace, f"{director.name}所長（相談）", [])
    return {
        "role": "assistant",
        "kind": "chat",
        "agent": director.name,
        "date": date.today().isoformat(),
        "content": answer.text,
        "model": answer.response_metadata.get("model_name") or os.environ["LLM_MODEL"],
        **extra,
        "seconds": round(time.monotonic() - started, 2),
        "llm_calls": len(trace.calls),
        "trace": trace.calls,
    }


def member_questions(history: list[dict], question: str) -> list[str]:
    """所員に渡す、これまでの質問。所長が聞き返した後の答えには、その確認を添える。"""
    questions = []
    for i, t in enumerate([*history, {"role": "user", "content": question}]):
        if t["role"] != "user":
            continue
        previous = history[i - 1] if 0 < i <= len(history) else None
        if previous and previous.get("kind") == "clarify":
            questions.append(f"{user_text(t)}（所長の確認「{previous['content']}」への答え）")
        else:
            questions.append(user_text(t))
    return questions


def read_images(images: list[str], director: characters.Character, emit: Emit, trace: Trace) -> str:
    """質問に添えられた画像（data URL）を文字に起こす。画像が複数なら番号を付けてつなげる。"""
    emit({"type": "images_start", "agent": director.name, "count": len(images)})
    texts = [describe_image(image, "依頼人が質問に添えた画像です。", trace, f"{director.name}所長（画像）") for image in images]
    if len(texts) == 1:
        return texts[0]
    return "\n\n".join(f"画像{i}:\n{text}" for i, text in enumerate(texts, 1))


def run_turn(
    history: list[dict],
    question: str,
    sources: list[Source],
    emit: Emit,
    scopes: dict[str, list[str]] | None = None,
    clarify: bool = True,
    memos: dict | None = None,
    images: list[str] | None = None,
    since: str | None = None,
) -> dict:
    """1ターン分の調査と回答。返り値は保存・表示用の assistant ターン。

    scopes はソースIDごとの検索範囲。書かれていないソースは絞らない。
    memos は事務所のメモ（memo_section の形）。所長と所員のプロンプトに入る。
    images は質問に添えられた画像（data URL）。最初に文字に起こし、返り値の image_text に入れる
    （保存する側が依頼人のターンに移す。次のターンからは文字のほうを使う）。
    clarify が False なら聞き返さない（依頼人が「聞き返さずに調べる」を選んだとき）。
    直前に聞き返したばかりのときも、続けては聞き返さない。
    since（YYYY-MM-DD）があれば続報の調査（「その後どうなった？」）。その日より後の動きだけを調べ、聞き返さない。
    聞き返したときは kind: "clarify" のターンを返す（reports などは持たない）。
    sources が空なら相談（consult）。調べずに所長が答え、kind: "chat" のターンを返す。

    trace（LLMとのやりとり）は大きいので、保存する側が会話とは分けて保存する。
    """
    started = time.monotonic()
    trace = Trace()
    director = characters.director()
    memo = memo_section(memos)
    image_text = read_images(images, director, emit, trace) if images else ""
    asked = user_text({"content": question, "image_text": image_text})
    extra = {"image_text": image_text} if image_text else {}

    if sources and clarify and not since and not (history and history[-1].get("kind") == "clarify"):
        emit({"type": "intake_start", "agent": director.name})
        if ask := intake(director, history, asked, trace, memo):
            return {
                "role": "assistant",
                "kind": "clarify",
                "agent": director.name,
                "date": date.today().isoformat(),
                "content": ask["question"],
                "model": ask["model"],
                **extra,
                "seconds": round(time.monotonic() - started, 2),
                "llm_calls": len(trace.calls),
                "trace": trace.calls,
            }

    if not sources:
        return consult(director, history, asked, emit, trace, memo, extra, started)

    questions = member_questions(history, asked)
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        reports = list(pool.map(lambda s: run_member(s, questions, emit, trace, (scopes or {}).get(s.id), memo=memo, since=since), sources))

    emit({"type": "director_start", "agent": director.name})
    answer = run_director(director, history, questions, reports, sources, emit, trace, memo, since)
    reports += answer.pop("followups")

    emit({"type": "memo_start", "agent": director.name})
    notes = remember(director, questions, answer["content"], memos, trace, memo)
    return {
        "role": "assistant",
        "agent": director.name,
        # 調べた日。次に「その後どうなった？」と聞かれたら、この日より後を調べる
        "date": date.today().isoformat(),
        **answer,
        "reports": reports,
        "links": links.verify(answer["content"], reports),
        # メモに書き足す内容。書き足すのは保存する側（メモの上限を見て、書けたかどうかに置き換える）
        "memo_notes": notes,
        **extra,
        "seconds": round(time.monotonic() - started, 2),
        "llm_calls": len(trace.calls),
        "trace": trace.calls,
    }
