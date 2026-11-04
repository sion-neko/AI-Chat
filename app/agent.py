"""所長と所員によるチーム調査。

1ターンの流れ:
  0. 所長が依頼を受け付ける。何の話か特定できないほど曖昧なら、調べずに聞き返してターンを終える
  1. 選ばれたソースごとに所員（LangGraphのツールループ）を並列で走らせる
     所員に渡すのはユーザーの質問だけ。他の所員の結果や過去の回答は見せない
  2. 所長が所員の報告を読む。足りなければ所員に再調査を頼める（FOLLOWUP_ROUNDS 回まで）
     再調査では、その所員の前回の報告と所長の依頼を渡す
  3. 所長が報告をまとめて回答する
  4. 所長が、次の調査でも役に立つ前提（社内の呼び方や話のありか等）を事務所のメモに書き足す

調べる場所（ソース）が選ばれていなければ、所員は動かさず所長がそのまま答える（相談）。

所長と所員のプロンプト（app/prompts.py）には、事務所のメモ（全体と、調査が入っているフォルダの分）を入れる。

画像を読めるモデル（app/models.json）のときは:
  - 質問に添えられた画像を、受付の前に1回だけ文字に起こし、質問に添えて所長と所員に渡す
  - 所員に view_image を持たせ、検索結果の中の画像を必要なときだけ読ませる（文字に起こして返す）

進み具合は emit に渡したイベントで通知する。
LLMとのやりとりは Trace に記録し、返り値の trace に入れる（あとから画面で確かめられるように）。
"""

import base64
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import openai
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolInvocationError

from app import characters, links, models
from app.prompts import (
    CHAT_PROMPT,
    DIRECTOR_PROMPT,
    DIRECTOR_SINCE_NOTE,
    IMAGE_PROMPT,
    INTAKE_PROMPT,
    MEMBER_PROMPT,
    MEMBER_SINCE_NOTE,
    MEMBER_VISION_NOTE,
    REMEMBER_PROMPT,
    TITLE_PROMPT,
)
from app.sources import Source
from app.sources import scope as search_scope
from app.sources.http import ApiError

Emit = Callable[[dict], None]

# 所員1人あたりのグラフのステップ上限（LLM呼び出しとツール実行の合計）。長引く調査を打ち切るため
MEMBER_RECURSION_LIMIT = 12
# 所長が再調査を頼める回数（1ターンあたり）。頼んだ報告を受けたら必ず回答する
FOLLOWUP_ROUNDS = 1
# 所員1人が1回の調査で view_image で読める画像の枚数
VIEW_IMAGE_LIMIT = 3
# 所長に渡す過去のやりとりのターン数（最初のやりとりは別に渡す）。ローカルLLMの文脈を使い切らないように
HISTORY_TURNS = 10
# 1回の調査でメモに書き足す件数の上限
REMEMBER_LIMIT = 3
# 所長が付ける調査の題名の長さ（プロンプトで頼む長さ。返ってきた題名はこの2倍で切る）
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
# LLMの呼び出しはすべて、この枠を1つ使って行う（_call と _stream）
_llm_slots = threading.BoundedSemaphore(LLM_CONCURRENCY)


class Cancelled(BaseException):
    """調査が中止された。emit が投げる。

    所員の失敗（except Exception）として握りつぶされず、ターン全体を止めるよう BaseException にしている。
    """


# ---- LLMの呼び出しと記録 ----


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


def _call(messages: list, trace: Trace | None, who: str, tools: list = ()) -> AIMessage:
    """LLMを1回呼ぶ。tools があれば渡す。trace があれば記録する。"""
    llm = _llm.bind_tools(tools) if tools else _llm
    started = time.monotonic()
    with _llm_slots:
        response = llm.invoke(messages)
    if trace:
        trace.record(who, messages, list(tools), response, time.monotonic() - started)
    return response


def _stream(messages: list, emit: Emit, trace: Trace, who: str, tools: list = ()) -> AIMessage:
    """_call と同じだが、届いた文字を delta イベントで流す。ツール呼び出しも含めて全体をまとめて返す。"""
    llm = _llm.bind_tools(tools) if tools else _llm
    started = time.monotonic()
    answer = None
    with _llm_slots:
        for chunk in llm.stream(messages):
            answer = chunk if answer is None else answer + chunk
            if chunk.text:
                emit({"type": "delta", "text": chunk.text})
    trace.record(who, messages, list(tools), answer, time.monotonic() - started)
    return answer


def _model_name(response: AIMessage) -> str:
    """応答したモデルの名前。設定値ではなく、LLMが実際に返したもの（返さないサーバーなら設定値）。"""
    return response.response_metadata.get("model_name") or os.environ.get("LLM_MODEL", "")


def _function_tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """LLMに渡すツールの定義（OpenAIの function の形）。所長のツールは実行せず、呼び出しの引数だけを読む。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def explain_error(e: Exception) -> str:
    """調査が失敗したときに画面に出す文。LLMのよくある失敗は、何を確かめればよいかを添える。"""
    detail = f"{type(e).__name__}: {e}"
    if isinstance(e, openai.APITimeoutError):
        hint = f"LLMが{LLM_TIMEOUT:g}秒のうちに答えませんでした。LLMのサーバーが混んでいないか確かめ、必要なら .env の LLM_TIMEOUT を延ばしてください"
    elif isinstance(e, openai.APIConnectionError):
        hint = f"LLMにつながりません（{os.environ.get('LLM_BASE_URL', '')}）。LLMのサーバーが動いているか、.env の LLM_BASE_URL を確かめてください"
    elif isinstance(e, openai.AuthenticationError):
        hint = "LLMに断られました。.env の LLM_API_KEY を確かめてください"
    elif isinstance(e, openai.NotFoundError):
        hint = f"LLMにモデル {os.environ.get('LLM_MODEL', '')} が見つかりません。.env の LLM_MODEL を確かめてください"
    elif isinstance(e, openai.BadRequestError) and any(w in str(e).lower() for w in ("context", "too long", "maximum", "token")):
        hint = "会話や貼った文章が長すぎて、LLMが一度に読める量を超えました。新しい調査で、必要な部分だけを貼って聞き直してください"
    else:
        return detail
    return f"{hint}（{detail}）"


def check_llm() -> dict:
    """LLMに短い質問を1回送って、つながるか確かめる。結果は sources.check と同じ形。"""
    try:
        answer = _call([HumanMessage("接続の確認です。「OK」とだけ返してください。")], None, "")
    except Exception as e:
        return {"state": "error", "message": explain_error(e)}
    return {"state": "ok", "message": f"{_model_name(answer)} が応答しました"}


# ---- 依頼人の質問と、これまでのやりとり ----


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


# ---- 画像 ----


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def describe_image(url: str, purpose: str, trace: Trace, who: str) -> str:
    """画像を読めるモデルに画像（data URL）を見せ、写っている内容を文字で返す。purpose は何の画像かの説明。"""
    content = [{"type": "text", "text": f"{IMAGE_PROMPT}\n\n{purpose}"}, {"type": "image_url", "image_url": {"url": url}}]
    return _call([HumanMessage(content=content)], trace, who).text.strip()


def read_images(images: list[str], director: characters.Character, emit: Emit, trace: Trace) -> str:
    """質問に添えられた画像（data URL）を文字に起こす。画像が複数なら番号を付けてつなげる。"""
    emit({"type": "images_start", "agent": director.name, "count": len(images)})
    texts = [describe_image(image, "依頼人が質問に添えた画像です。", trace, f"{director.name}所長（画像）") for image in images]
    if len(texts) == 1:
        return texts[0]
    return "\n\n".join(f"画像{i}:\n{text}" for i, text in enumerate(texts, 1))


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
        return describe_image(data_url(data, mime), purpose, trace, f"{who}（画像）")

    return StructuredTool.from_function(
        view_image,
        name="view_image",
        description=(
            f"{source.label}の検索結果に含まれる画像（スクリーンショット・グラフ等）を見て、写っている内容を文字で返す。"
            f"url は検索結果に書かれた画像のURL（相対URLも可）。1回の調査で{VIEW_IMAGE_LIMIT}枚まで"
        ),
    )


# ---- 所員 ----


def _tool_loop(system_prompt: str, tools: list[BaseTool], trace: Trace, who: str):
    """所員の調査の流れ（LangGraph）。LLMがツールを呼ばなくなる（報告を書く）まで、検索と読み直しを繰り返す。"""

    def call_llm(state: MessagesState):
        return {"messages": [_call([SystemMessage(system_prompt), *state["messages"]], trace, who, tools)]}

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


# ---- 所長 ----

ASK_USER_TOOL = _function_tool(
    "ask_user",
    "調べ始める前に、依頼人に1つだけ聞き返す",
    {"question": {"type": "string", "description": "依頼人への質問（候補や例を添える）"}},
    ["question"],
)


def intake(director: characters.Character, history: list[dict], question: str, trace: Trace, memo: str = "") -> dict | None:
    """聞き返すべきなら {"question": 依頼人への質問, "model": 応答したモデル} を返す。調べてよければ None。

    受付は補助なので、LLMの呼び出しに失敗しても調査は止めない（None を返す）。
    メモに用語の対応などがあれば、それで分かる依頼は聞き返さない。
    """
    prompt = INTAKE_PROMPT.format(name=director.name, persona=director.persona, today=date.today()) + memo
    messages = [SystemMessage(prompt), *_history_messages(history), HumanMessage(question)]
    try:
        answer = _call(messages, trace, f"{director.name}所長（受付）", [ASK_USER_TOOL])
    except Exception:
        return None
    for call in answer.tool_calls:
        if call["name"] == "ask_user" and str(call["args"].get("question", "")).strip():
            return {"question": call["args"]["question"].strip(), "model": _model_name(answer)}
    return None


def _followup_tool(sources: list[Source]) -> dict:
    return _function_tool(
        "request_reinvestigation",
        "所員に担当ソースの再調査を頼む。複数の所員に同時に頼める。結果は所員の報告として返る",
        {
            "source": {"type": "string", "enum": [s.id for s in sources], "description": "再調査させる所員の担当ソースID"},
            "instruction": {"type": "string", "description": "何を調べ直してほしいか（検索語・期間・確かめたい点など）"},
        },
        ["source", "instruction"],
    )


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
        answer = _stream(messages, emit, trace, f"{director.name}所長", tools)
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
        # 再調査の報告を読んで回答を書き直すので、画面には書きかけの回答を捨てさせる
        emit({"type": "director_start", "agent": director.name})

    return {"content": answer.text, "model": _model_name(answer), "followups": followups}


def consult(director: characters.Character, history: list[dict], asked: str, emit: Emit, trace: Trace, memo: str) -> dict:
    """調べる場所を選ばずに頼まれたとき。所員は動かさず、所長がそのまま答える（聞き返しもメモへの書き足しもしない）。"""
    emit({"type": "director_start", "agent": director.name})
    prompt = CHAT_PROMPT.format(name=director.name, persona=director.persona, today=date.today()) + memo
    messages = [SystemMessage(prompt), *_history_messages(history), HumanMessage(asked)]
    answer = _stream(messages, emit, trace, f"{director.name}所長（相談）")
    return {"content": answer.text, "model": _model_name(answer)}


# ---- 調査のあと（メモと題名） ----


def _remember_tool(has_folder: bool) -> dict:
    return _function_tool(
        "remember",
        "事務所のメモに1行書き足す",
        {
            "scope": {
                "type": "string",
                "enum": ["folder", "global"] if has_folder else ["global"],
                "description": "folder: この案件のメモ / global: すべての調査で使うメモ",
            },
            "note": {"type": "string", "description": "書き足す1行"},
        },
        ["scope", "note"],
    )


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
    try:
        response = _call(messages, trace, f"{director.name}所長（メモ）", [tool])
    except Exception:
        return []
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
    ターンを保存したあとに呼ぶので、記録（Trace）には残さない。
    """
    asked = "\n".join(f"- {q}" for q in questions)
    messages = [SystemMessage(TITLE_PROMPT.format(length=TITLE_LENGTH)), HumanMessage(f"## 質問\n{asked}\n\n## 回答\n{answer[:2000]}")]
    try:
        response = _call(messages, None, "")
    except Exception:
        return None
    lines = [line for line in response.text.splitlines() if line.strip()]
    title = lines[0].strip().strip("「」『』\"'#*。 ") if lines else ""
    return title[: TITLE_LENGTH * 2] or None


# ---- 1ターン ----


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

    def turn(**fields) -> dict:
        """どの終わり方（聞き返し・相談・回答）でも付ける項目をそろえる。"""
        return {
            "role": "assistant",
            "agent": director.name,
            # 調べた日。次に「その後どうなった？」と聞かれたら、この日より後を調べる
            "date": date.today().isoformat(),
            **fields,
            **({"image_text": image_text} if image_text else {}),
            "seconds": round(time.monotonic() - started, 2),
            "llm_calls": len(trace.calls),
            "trace": trace.calls,
        }

    if sources and clarify and not since and not (history and history[-1].get("kind") == "clarify"):
        emit({"type": "intake_start", "agent": director.name})
        if ask := intake(director, history, asked, trace, memo):
            return turn(kind="clarify", content=ask["question"], model=ask["model"])

    if not sources:
        return turn(kind="chat", **consult(director, history, asked, emit, trace, memo))

    questions = member_questions(history, asked)
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        reports = list(pool.map(lambda s: run_member(s, questions, emit, trace, (scopes or {}).get(s.id), memo=memo, since=since), sources))

    emit({"type": "director_start", "agent": director.name})
    answer = run_director(director, history, questions, reports, sources, emit, trace, memo, since)
    reports += answer.pop("followups")

    emit({"type": "memo_start", "agent": director.name})
    notes = remember(director, questions, answer["content"], memos, trace, memo)
    return turn(
        **answer,
        reports=reports,
        links=links.verify(answer["content"], reports),
        # メモに書き足す内容。書き足すのは保存する側（メモの上限を見て、書けたかどうかに置き換える）
        memo_notes=notes,
    )
