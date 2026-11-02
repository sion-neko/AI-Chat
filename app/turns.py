"""質問を受け付けて調査を走らせ、結果を会話に保存する。

調査は画面とは切り離してスレッドで最後まで走らせ、進み具合は app/jobs.py で画面に配る。
保存は調査が成功したときだけ。中止・失敗したら何も保存せず、新しい会話なら会話ごと消す。
保存したあとに、会話に未読の印を付け、所長にメモへの書き足しと（最初の回答なら）題名付けをさせる。
"""

import threading
import traceback
from dataclasses import dataclass, field

from app import agent, db, jobs
from app.errors import InputError, NotFound
from app.sources import Source

# 新しい会話の題名にする、最初の質問の長さ（あとで所長が題名を付け直す）
QUESTION_TITLE_LENGTH = 40
# メモの上限。メモはそのままプロンプトに入るので、ローカルLLMの文脈を使い切らない長さに抑える
MEMO_LENGTH = 2000


@dataclass
class Question:
    """画面から届いた質問（検証済み）。"""

    content: str
    sources: list[Source]
    # ソースIDごとの検索範囲（リポジトリやチャンネル）
    scopes: dict[str, list[str]] = field(default_factory=dict)
    # False なら所長は聞き返さずに調べる
    clarify: bool = True
    # 添えられた画像 [(MIMEタイプ, バイト列)]
    images: list[tuple[str, bytes]] = field(default_factory=list)


def ask_new(question: Question, folder_id: str | None) -> jobs.Job:
    """新しい会話を作って、最初の質問を調べ始める。"""
    conv_id = db.create_conversation(question.content[:QUESTION_TITLE_LENGTH], folder_id)
    return _start(conv_id, [], question, is_new=True)


def ask(conv_id: str, question: Question, from_turn: int | None = None, followup: bool = False) -> jobs.Job:
    """会話の続きを調べ始める。

    from_turn（質問のターンの位置）があれば、そこから後を消して質問し直す（成功したときだけ置き換える）。
    やり直しで画像を送り直さなかったときは、元の質問の画像を使う。
    followup なら続報（「その後どうなった？」）。直前の回答を調べた日より後の動きだけを調べる。
    """
    history = db.load_turns(conv_id)
    if history is None:
        raise NotFound
    image_ids = None
    if from_turn is not None:
        if not (isinstance(from_turn, int) and 0 <= from_turn < len(history) and history[from_turn]["role"] == "user"):
            raise InputError("やり直す質問の位置が正しくありません")
        if not question.images and history[from_turn].get("images"):
            image_ids = history[from_turn]["images"]
            question.images = [db.load_image(i) for i in image_ids]
        history = history[:from_turn]
    since = None
    if followup:
        if not question.sources:
            raise InputError("続報を調べるには、調べる場所を選んでください")
        since = followup_since(history, db.get_conversation(conv_id))
    return _start(conv_id, history, question, is_new=False, replace_from=from_turn, image_ids=image_ids, since=since)


def cancel_conversation(conv_id: str):
    """会話の調査を中止する（会話を消すとき。保存先がなくなるので）。"""
    for job in jobs.running():
        if job.conversation_id == conv_id:
            job.cancel.set()


def followup_since(history: list[dict], conversation: dict) -> str:
    """「その後どうなった？」で調べ始める日。直前の回答を調べた日（日付を持つ前の回答は、会話の更新日）。"""
    last = history[-1] if history else None
    if not last or last["role"] != "assistant" or last.get("kind") in ("clarify", "chat"):
        raise InputError("続報を調べられるのは、所長が回答した後だけです")
    return last.get("date") or conversation["updated_at"][:10]


def memos_for(conv_id: str) -> dict:
    """調査のプロンプトに入れるメモ（agent.memo_section の形）。会話がフォルダに入っていれば、そのフォルダのメモも。"""
    folder_id = (db.get_conversation(conv_id) or {}).get("folder_id")
    folder = db.get_folder(folder_id) if folder_id else None
    return {"global": db.get_setting("memo", ""), "folder": {"name": folder["name"], "memo": folder["memo"]} if folder else None}


def add_to_memo(conv_id: str, notes: list[dict]) -> list[dict]:
    """所長が決めた内容をメモに書き足す。上限を超えるものは書かない。

    返り値は [{"scope", "note", "folder"（フォルダ名。全体なら None）, "saved"}]。回答の下に出す。
    """
    folder = db.get_folder((db.get_conversation(conv_id) or {}).get("folder_id") or "")
    result = []
    for n in notes:
        target = folder if n["scope"] == "folder" else None
        if n["scope"] == "folder" and not folder:
            continue
        current = target["memo"] if target else db.get_setting("memo", "")
        memo = f"{current}\n- {n['note']}".strip()
        saved = len(memo) <= MEMO_LENGTH
        if saved and target:
            db.update_folder(target["id"], memo=memo)
            target["memo"] = memo
        elif saved:
            db.set_setting("memo", memo)
        result.append({"scope": n["scope"], "note": n["note"], "folder": target["name"] if target else None, "saved": saved})
    return result


def retitle(conv_id: str):
    """最初の回答が出たら、所長に調査の題名を付けさせる（題名が最初の質問のままのときだけ。名前を変えていたら触らない）。"""
    conversation = db.get_conversation(conv_id)
    turns = db.load_turns(conv_id) or []
    answers = [t for t in turns if t["role"] == "assistant" and t.get("kind") != "clarify"]
    if not conversation or len(answers) != 1 or conversation["title"] != turns[0]["content"][:QUESTION_TITLE_LENGTH]:
        return
    questions = agent.member_questions(turns[:-2], turns[-2]["content"])
    if title := agent.make_title(questions, answers[0]["content"]):
        db.update_conversation(conv_id, title=title)
        jobs.broadcast({"type": "title", "conversation_id": conv_id, "title": title})


def _start(
    conv_id: str,
    history: list[dict],
    question: Question,
    is_new: bool,
    replace_from: int | None = None,
    image_ids: list[str] | None = None,
    since: str | None = None,
) -> jobs.Job:
    """調査をスレッドで始める。同じ会話で調査中なら InputError。

    image_ids は、やり直しで元の質問の画像を使うときの、保存済みの画像のID（同じ画像を保存し直さない）。
    since は続報を調べるときの前回の調査日（followup_since）。
    """
    info = {
        "title": (db.get_conversation(conv_id) or {}).get("title", ""),
        "question": question.content,
        # 質問のターンの位置。やり直しなら、そこから後が置き換わる
        "index": len(history),
        "replace": replace_from is not None,
        "followup": since is not None,
        # 調べる場所を選ばずに所長に相談している
        "consult": not question.sources,
        "image_count": len(question.images),
    }
    try:
        job = jobs.start(conv_id, info)
    except jobs.Busy:
        raise InputError("この調査はまだ調べています。終わるか中止してから質問してください")

    def emit(event: dict):
        # 中止されたら、次にイベントを出そうとしたところで調査を止める
        if job.cancel.is_set():
            raise agent.Cancelled
        jobs.publish(job, event)

    def work():
        try:
            urls = [agent.data_url(data, mime) for mime, data in question.images]
            turn = agent.run_turn(
                history, question.content, question.sources, emit, question.scopes, question.clarify, memos_for(conv_id), urls, since
            )
            # 最後のLLMの呼び出しの間に中止された（会話が消された）なら保存しない
            if job.cancel.is_set():
                raise agent.Cancelled
            asked = _save(conv_id, question, turn, replace_from, image_ids, since)
            jobs.publish(job, {"type": "done", "conversation_id": conv_id, "question": asked, "turn": turn})
            retitle(conv_id)
        except agent.Cancelled:
            if is_new:
                db.delete_conversation(conv_id)
            jobs.publish(job, {"type": "cancelled", "conversation_id": conv_id, "deleted": is_new})
        except Exception as e:
            traceback.print_exc()
            if is_new:
                db.delete_conversation(conv_id)
            jobs.publish(job, {"type": "error", "conversation_id": conv_id, "deleted": is_new, "message": agent.explain_error(e)})

    threading.Thread(target=work, daemon=True).start()
    return job


def _save(conv_id: str, question: Question, turn: dict, replace_from: int | None, image_ids: list[str] | None, since: str | None) -> dict:
    """質問と回答のターンを会話に保存する。turn（所長のターン）は保存する形に書き換える。保存した質問のターンを返す。"""
    turn["trace_id"] = db.save_trace(conv_id, turn.pop("trace"))
    if "memo_notes" in turn:
        turn["memo_added"] = add_to_memo(conv_id, turn.pop("memo_notes"))
    asked = {"role": "user", "content": question.content}
    if since:
        asked["since"] = since
    if question.images:
        asked["images"] = image_ids or [db.save_image(conv_id, mime, data) for mime, data in question.images]
        # 画像を文字に起こした内容は、次のターンから所長と所員に渡すので、質問の側に持たせる
        asked["image_text"] = turn.pop("image_text", "")
    if replace_from is None:
        db.append_turns(conv_id, [asked, turn])
    else:
        db.replace_turns(conv_id, replace_from, [asked, turn])
    # 画面で読むまで、一覧に印を出す（見ている画面が読んだら外す）
    db.update_conversation(conv_id, unread=True)
    return asked
