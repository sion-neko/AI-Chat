"""所長と所員によるチーム調査。

1ターンの流れ:
  1. 選ばれたソースごとに所員（LangGraphのツールループ）を並列で走らせる
     所員に渡すのはユーザーの質問だけ。他の所員の結果や過去の回答は見せない
  2. 所長が所員の報告をまとめて回答する（ツールは持たない）

進み具合は emit に渡したイベントで通知する。
"""

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolInvocationError

from app import characters
from app.sources import Source
from app.sources.http import ApiError

Emit = Callable[[dict], None]

# 所員1人あたりのグラフのステップ上限（LLM呼び出しとツール実行の合計）。長引く調査を打ち切るため
MEMBER_RECURSION_LIMIT = 12

MEMBER_PROMPT = """あなたは「おばけ探偵事務所」の所員「{name}」です。{persona}
担当は「{source}」です。依頼人の質問について、担当ソースだけを調べて所長に報告します。

- 必ず検索ツールで根拠を探す
- ヒットしなければ検索語を言い換えて複数回検索する（同義語・略語・英語表記など）
- 期間が示されていれば日付で絞り込む
- 報告は見つかった事実だけを時系列の箇条書きにし、各項目に根拠のURLを付ける
- 見つからなければ「見つからなかった」と報告する。推測で補わない
- 口調はキャラクターに合わせてよいが、事実の正確さを最優先する"""

DIRECTOR_PROMPT = """あなたは「おばけ探偵事務所」の所長「{name}」です。{persona}
所員の調査報告をもとに、依頼人の質問に答えます。

- 報告に書かれた事実だけで答える。推測で補わない
- 複数の報告を突き合わせ、経緯を時系列でまとめる
- 回答には根拠のURLを必ず付ける
- どの所員も見つけられなかった場合は、そう伝える"""

_llm = ChatOpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["LLM_API_KEY"],
    model=os.environ["LLM_MODEL"],
)


def _tool_loop(system_prompt: str, tools: list[BaseTool]):
    llm = _llm.bind_tools(tools)

    def call_llm(state: MessagesState):
        return {"messages": [llm.invoke([SystemMessage(system_prompt), *state["messages"]])]}

    graph = StateGraph(MessagesState)
    graph.add_node("llm", call_llm)
    # 引数の誤りとAPIのエラーはLLMに返して、検索し直させる。それ以外は所員の調査失敗にする
    graph.add_node("tools", ToolNode(tools, handle_tool_errors=(ToolInvocationError, ApiError)))
    graph.add_edge(START, "llm")
    graph.add_conditional_edges("llm", tools_condition)
    graph.add_edge("tools", "llm")
    return graph.compile()


def run_member(source: Source, questions: list[str], emit: Emit) -> dict:
    """所員1人に調査させ、報告を返す。失敗しても例外は投げず、error に入れて返す。"""
    character = characters.member(source.id, source.label)
    report = {"source": source.id, "label": source.label, "agent": character.name, "content": "", "steps": [], "error": None}
    emit({"type": "member_start", "source": source.id, "agent": character.name})

    prompt = MEMBER_PROMPT.format(name=character.name, persona=character.persona, source=source.label)
    *earlier, current = questions
    request = f"今回の質問: {current}"
    if earlier:
        request = "これまでの質問:\n" + "\n".join(f"- {q}" for q in earlier) + f"\n\n{request}"

    try:
        graph = _tool_loop(prompt, source.tools)
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
                        emit({"type": "tool", "source": source.id, "tool": call["name"], "args": call["args"]})
                elif isinstance(msg, ToolMessage):
                    for step in report["steps"]:
                        if step["id"] == msg.tool_call_id:
                            step["result"] = msg.text
                elif isinstance(msg, AIMessage):
                    report["content"] = msg.text
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"

    emit({"type": "member_done", "report": report})
    return report


def run_director(history: list[dict], question: str, reports: list[dict]) -> dict:
    """所長が報告をまとめる。history は過去のターン（質問と所長の回答）。"""
    prompt = DIRECTOR_PROMPT.format(name=characters.DIRECTOR.name, persona=characters.DIRECTOR.persona)
    messages = [SystemMessage(prompt)]
    for turn in history:
        messages.append(HumanMessage(turn["content"]) if turn["role"] == "user" else AIMessage(turn["content"]))

    sections = []
    for r in reports:
        body = f"（調査失敗: {r['error']}）" if r["error"] else r["content"] or "（報告なし）"
        sections.append(f"### {r['agent']}（{r['label']}）\n{body}")
    messages.append(HumanMessage(f"{question}\n\n## 所員の報告\n\n" + "\n\n".join(sections)))

    answer = _llm.invoke(messages)
    # 設定値ではなく、LLMが実際に返したモデル名
    return {"content": answer.text, "model": answer.response_metadata.get("model_name") or os.environ["LLM_MODEL"]}


def run_turn(history: list[dict], question: str, sources: list[Source], emit: Emit) -> dict:
    """1ターン分の調査と回答。返り値は保存・表示用の assistant ターン。"""
    questions = [t["content"] for t in history if t["role"] == "user"] + [question]
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        reports = list(pool.map(lambda s: run_member(s, questions, emit), sources))

    emit({"type": "director_start", "agent": characters.DIRECTOR.name})
    answer = run_director(history, question, reports)
    return {"role": "assistant", "agent": characters.DIRECTOR.name, **answer, "reports": reports}
