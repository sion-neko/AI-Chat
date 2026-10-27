"""所長と所員によるチーム調査。

1ターンの流れ:
  1. 選ばれたソースごとに所員（LangGraphのツールループ）を並列で走らせる
     所員に渡すのはユーザーの質問だけ。他の所員の結果や過去の回答は見せない
  2. 所長が所員の報告を読む。足りなければ所員に再調査を頼める（FOLLOWUP_ROUNDS 回まで）
     再調査では、その所員の前回の報告と所長の依頼を渡す
  3. 所長が報告をまとめて回答する

進み具合は emit に渡したイベントで通知する。
LLMとのやりとりは Trace に記録し、返り値の trace に入れる（あとから画面で確かめられるように）。
"""

import os
import time
from collections.abc import Callable
from datetime import date
from concurrent.futures import ThreadPoolExecutor

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolInvocationError

from app import characters, links
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

_llm = ChatOpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["LLM_API_KEY"],
    model=os.environ["LLM_MODEL"],
)


def _tool_loop(system_prompt: str, tools: list[BaseTool], trace: Trace, who: str):
    llm = _llm.bind_tools(tools)

    def call_llm(state: MessagesState):
        messages = [SystemMessage(system_prompt), *state["messages"]]
        started = time.monotonic()
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
) -> dict:
    """所員1人に調査させ、報告を返す。失敗しても例外は投げず、error に入れて返す。

    scope は画面で選んだ検索範囲（リポジトリやチャンネル）。None なら絞らない。
    followup は所長からの再調査の依頼 {"instruction": 依頼, "previous": 前回の報告}。
    """
    character = characters.member(source.id, source.label)
    report = {"source": source.id, "label": source.label, "agent": character.name, "content": "", "steps": [], "error": None}
    if scope:
        report["scope"] = scope
    if followup:
        report["request"] = followup["instruction"]
    emit({"type": "member_start", "source": source.id, "agent": character.name})

    prompt = MEMBER_PROMPT.format(name=character.name, persona=character.persona, source=source.label, today=date.today())
    *earlier, current = questions
    request = f"今回の質問: {current}"
    if earlier:
        request = "これまでの質問:\n" + "\n".join(f"- {q}" for q in earlier) + f"\n\n{request}"
    if scope:
        request += f"\n\n検索範囲（{source.scope_label}）は次に絞られています: {', '.join(scope)}"
    if followup:
        request += f"\n\n## あなたの前回の報告\n{followup['previous']}\n\n## 所長からの再調査の依頼\n{followup['instruction']}"

    # このスレッドで動く検索ツールに、範囲を渡す
    search_scope.set(scope)
    started = time.monotonic()
    called_at = {}  # ツール呼び出しID → 呼んだ時刻（結果が返るまでの時間を測る）
    try:
        graph = _tool_loop(prompt, source.tools, trace, f"{character.name}（{source.label}）")
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
        report["error"] = f"{type(e).__name__}: {e}"

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
) -> dict:
    """所長が報告を読み、必要なら再調査を頼んでから回答する。

    history は過去のターン（質問と所長の回答）。返り値の followups は再調査で増えた報告。
    """
    prompt = DIRECTOR_PROMPT.format(name=director.name, persona=director.persona, today=date.today())
    messages = [SystemMessage(prompt)]
    for turn in history:
        messages.append(HumanMessage(turn["content"]) if turn["role"] == "user" else AIMessage(turn["content"]))

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
            return run_member(source, questions, emit, trace, latest[source.id].get("scope"), {"instruction": call["args"]["instruction"], "previous": previous})

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
        answer = _llm.invoke([HumanMessage("接続の確認です。「OK」とだけ返してください。")])
    except Exception as e:
        return {"state": "error", "message": f"{type(e).__name__}: {e}"}
    model = answer.response_metadata.get("model_name") or os.environ["LLM_MODEL"]
    return {"state": "ok", "message": f"{model} が応答しました"}


def run_turn(
    history: list[dict],
    question: str,
    sources: list[Source],
    emit: Emit,
    scopes: dict[str, list[str]] | None = None,
) -> dict:
    """1ターン分の調査と回答。返り値は保存・表示用の assistant ターン。

    scopes はソースIDごとの検索範囲。書かれていないソースは絞らない。

    trace（LLMとのやりとり）は大きいので、保存する側が会話とは分けて保存する。
    """
    started = time.monotonic()
    trace = Trace()
    questions = [t["content"] for t in history if t["role"] == "user"] + [question]
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        reports = list(pool.map(lambda s: run_member(s, questions, emit, trace, (scopes or {}).get(s.id)), sources))

    director = characters.director()
    emit({"type": "director_start", "agent": director.name})
    answer = run_director(director, history, questions, reports, sources, emit, trace)
    reports += answer.pop("followups")
    return {
        "role": "assistant",
        "agent": director.name,
        **answer,
        "reports": reports,
        "links": links.verify(answer["content"], reports),
        "seconds": round(time.monotonic() - started, 2),
        "llm_calls": len(trace.calls),
        "trace": trace.calls,
    }
