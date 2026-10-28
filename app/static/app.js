// おばけ探偵事務所の画面（標準のJavaScriptだけ。組み立ての手順なし）
//
// 節の並び:
//   状態とよく使う要素 / 質問して調べる / 進み具合のストリーム / 回答が届いたときの知らせ / 質問に添えるファイル
//   設定と調べる場所 / 調べる範囲を選ぶ / 調査の一覧 / フォルダ / 開いている調査の見出しと操作 / 調査と回答の表示
//   最初の画面 / メモ / ソースと接続 / キャラクター / LLMとのやりとり / 使い方 / キーボード / 通信と小物 / 起動
//
// 調査はサーバーで走り、進み具合は /api/events の1本のストリームで届く（app/jobs.py）。
// 画面は「調査中のターン」（jobs）を持ち、開いている会話のものだけを描く。

// ---- 状態とよく使う要素 ----

// 表示中の会話ID。null は「新しい会話」（最初の送信でサーバー側に作られる）
let currentId = null;
// 会話の一覧（今の検索とアーカイブの表示に合うもの）
let conversations = [];
// アーカイブした会話を見ているか
let showingArchive = false;
// 開いている会話。検索で一覧から外れても、名前やフォルダを出せるように持っておく
let openedConversation = null;
// 開いている会話に保存されているターンの数（次の質問の位置）
let turnCount = 0;
// フォルダの一覧と、サイドバーで選んでいるフォルダ（null は「すべての調査」）
let folders = [];
let viewFolderId = null;
// 入力欄の選択 {unchecked: [外したソースID], scopes: {ソースID: [名前]}}。今のフォルダ（なければ全体）に保存する
let selection = { unchecked: [], scopes: {} };
// 所長の名前（回答の見出しに使う）と、ソースIDから画面の名前への対応
let directorName = "所長";
let sourceLabels = new Map();

// よく使う要素
const log = document.getElementById("log");
const list = document.getElementById("conversations");
const search = document.getElementById("search");
const titleEl = document.getElementById("conversation-title");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const sourceBox = document.getElementById("sources");

// ---- 質問して調べる ----

input.addEventListener("input", autosize);

input.addEventListener("keydown", (e) => {
  // 日本語変換の確定Enterでは送信しない
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    // 調査中のEnterで中止にならないよう、送れるときだけ送る
    if (!busy()) form.requestSubmit();
    else if (input.value.trim()) explainBusy();
  }
});

// 調査中のターン。turn_id → { turnId, conversationId, info, events, view, turnNode, cancelling, mine }
// info はサーバーの "job" イベントの中身（質問・質問の位置・やり直しか・続報か）
// view と turnNode は、その会話を開いている間だけの進み具合の表示。mine はこの画面から送った質問と画像
// 調査はサーバーで最後まで走るので、別の会話を開いても、画面をリロードしても続く
const jobs = new Map();

// 質問を送っている途中（サーバーが受け付けるまで）。二重に送らないようにする
let submitting = false;

// 開いている会話で調査中か、質問を送っている途中。やり直しや続報はできない
function busy() {
  return Boolean(currentJob()) || submitting;
}

// 調査中に送ろうとしたときに、なぜ送れないかを出す（入力した文章はそのまま残る）
function explainBusy() {
  showComposerStatus("まだ調べています。終わるか中止してから送れます");
}

function jobFor(conversationId) {
  return conversationId ? [...jobs.values()].find((j) => j.conversationId === conversationId) : undefined;
}

// 開いている会話で調査中のもの。あれば送信ボタンが中止ボタンになる
function currentJob() {
  return jobFor(currentId);
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (currentJob()) return cancelTurn();
  if (submitting) return;
  const images = attachments;
  // 画像だけを添えて送ったときは、画像について調べてもらう
  const content = input.value.trim() || (images.length ? "この画像について、関係する話を調べて" : "");
  if (!content) return;
  input.value = "";
  autosize();
  attachments = [];
  renderAttachments();
  ask(content, null, false, images);
});

// 質問して調査させる。fromTurn（質問のターンの位置）があれば、そこから後をやり直す
// skipClarify なら所長は聞き返さずに調べる
// images は新しく添える画像（data URL）。shownImages は質問に並べて見せる画像（やり直しでは元の質問の画像）
// followup なら続報の調査（直前の回答を調べた日より後の動きだけを調べる）
async function ask(content, fromTurn, skipClarify = false, images = [], shownImages = images, followup = false) {
  askNotificationPermission();
  if (document.getElementById("empty")) {
    log.replaceChildren();
    renderHead({ title: content });
  }
  if (fromTurn !== null) hideTurnsFrom(fromTurn);
  const index = fromTurn ?? turnCount;
  // サーバーが受け付けるまでの仮の表示。受け付けたら調査の表示としてそのまま使う
  const turnNode = append(turnView(content, index, { images: shownImages, followup }));
  const consult = !selectedSources().length;
  const view = progressView(undefined, consult);
  turnNode.append(view.node);
  submitting = true;
  setSendButton();
  const asked = currentId;
  let job = null;
  try {
    const path = asked ? `/api/conversations/${asked}/messages` : "/api/conversations";
    const body = { content, sources: selectedSources(), scopes: selection.scopes };
    if (!asked && viewFolderId) body.folder_id = viewFolderId;
    if (fromTurn !== null) body.from_turn = fromTurn;
    if (skipClarify) body.skip_clarify = true;
    if (images.length) body.images = images;
    if (followup) body.followup = true;
    const { turn_id, conversation_id } = await api("POST", path, body);
    if (finished.has(turn_id)) {
      // 受け付けの返事より先に、調査が終わったと届いた（とても速いLLM）。保存された結果を開く
      if (currentId === asked) await openConversation(conversation_id);
      else await refreshList();
      return;
    }
    job = ensureJob(turn_id, conversation_id, { question: content, index, replace: fromTurn !== null, followup, consult });
    job.mine = { content, images, shownImages };
    // 受け付けを待つ間に別の会話へ移っていたら、仮の表示は捨てる（戻ってきたときに描き直す）
    if (currentId === asked && turnNode.isConnected) {
      if (!asked) {
        currentId = conversation_id;
        renderHead({ title: content });
      }
      job.turnNode = turnNode;
      job.view = view;
      for (const event of job.events) view.update(event);
    }
    if (!asked) await Promise.all([refreshList(), refreshFolders()]);
    renderHead();
  } catch (err) {
    if (currentId === asked) restoreQuestion(content, images);
    if (fromTurn !== null && currentId === asked) {
      // やり直しで外したやりとりを元に戻す
      await openConversation(asked);
      showComposerStatus(`調査を始められませんでした: ${err.message}`);
    } else view.node.replaceWith(el("p", "error-text turn-error", `調査を始められませんでした: ${err.message}`));
  } finally {
    submitting = false;
    // 受け付けを待つ間に別の会話へ移って戻ってきていたら、ここで描く
    if (job) showJob(job);
    setSendButton();
    input.focus();
  }
}

// やり直す質問と、それより後のやりとりを画面から外す（失敗・中止したら開き直して元に戻す）
function hideTurnsFrom(index) {
  for (const node of [...log.querySelectorAll(".turn")].filter((n) => Number(n.dataset.index) >= index)) node.remove();
}

// 調査中の一覧に入れる。進み具合のストリームと質問の応答のどちらが先に届いても、同じものを指す
function ensureJob(turnId, conversationId, info) {
  let job = jobs.get(turnId);
  if (!job) {
    job = { turnId, conversationId, info, events: [], view: null, turnNode: null, cancelling: false, mine: null, seen: true };
    jobs.set(turnId, job);
  }
  return job;
}

// 開いている会話の調査中のターンを、溜まった進み具合から描く
function showJob(job) {
  if (job.conversationId !== currentId || job.turnNode?.isConnected) return;
  // この画面から送った質問は、受け付けを待つ間の仮の表示をそのまま使う（ask が付ける）
  if (submitting) return;
  const { question, index, replace, followup, image_count } = job.info;
  if (replace) hideTurnsFrom(index);
  log.querySelector(".turn-error")?.remove();
  job.turnNode = append(turnView(question, index, { images: job.mine?.shownImages ?? [], followup, imageCount: job.mine ? 0 : image_count }));
  job.view = progressView(job.info.started_at, job.info.consult);
  job.turnNode.append(job.view.node);
  for (const event of job.events) job.view.update(event);
  setSendButton();
}

async function cancelTurn() {
  const job = currentJob();
  if (!job || job.cancelling) return;
  job.cancelling = true;
  setSendButton();
  await api("POST", `/api/turns/${job.turnId}/cancel`);
}

function setSendButton() {
  const job = currentJob();
  const consult = !selectedSources().length;
  document.getElementById("consult-hint").hidden = !consult;
  const idle = consult ? "所長に聞く" : "調査を依頼";
  send.textContent = job ? (job.cancelling ? "中止しています…" : "中止") : submitting ? "受け付けています…" : idle;
  send.classList.toggle("stop", Boolean(job));
  send.disabled = Boolean(job?.cancelling) || (submitting && !job);
}

// 聞き直せるよう、質問と添えた画像を入力欄に戻す（入力中の文章や画像があれば上書きしない）
function restoreQuestion(content, images = []) {
  if (!attachments.length && images.length) {
    attachments = images;
    renderAttachments();
  }
  if (input.value.trim()) return;
  input.value = content;
  autosize();
}

// ---- 進み具合のストリーム ----

async function connectEvents() {
  for (let wait = 1000; ; wait = Math.min(wait * 2, 15000)) {
    try {
      // つなぎ直すと調査中のものが始めから流れてくるので、溜めた分は捨てて、流れてきたものだけを残す
      for (const job of jobs.values()) job.seen = false;
      await readLines("/api/events", (event) => {
        if (event.type === "ready") {
          wait = 1000;
          return lostJobs();
        }
        if (event.type === "title") return onTitle(event);
        if (event.turn_id) onJobEvent(event);
      });
    } catch {
      // つながらなかった・切れた。少し待ってつなぎ直す
    }
    await new Promise((resolve) => setTimeout(resolve, wait));
  }
}

// 所長が調査に題名を付けた
function onTitle({ conversation_id, title }) {
  for (const c of [...conversations, openedConversation]) if (c?.id === conversation_id) c.title = title;
  renderList();
  if (conversation_id === currentId && !titleEl.querySelector("input")) renderHead();
}

function onJobEvent(event) {
  if (event.type === "job") {
    const job = ensureJob(event.turn_id, event.conversation_id, event);
    job.info = event;
    job.events = [];
    job.seen = true;
    // つなぎ直したときは、描いてあった進み具合を描き直す
    if (job.view) {
      job.view.reset();
    }
    showJob(job);
    renderList();
    updateTitle();
    // 別の画面（タブ）で始めた調査なら、一覧に出す
    if (!conversations.some((c) => c.id === event.conversation_id)) refreshListSoon();
    return;
  }
  const job = jobs.get(event.turn_id);
  if (!job) {
    // 受け付けの返事より先に終わりが届いた
    if (["done", "cancelled", "error"].includes(event.type)) finished.add(event.turn_id);
    return;
  }
  if (event.type === "done" || event.type === "cancelled" || event.type === "error") return finishJob(job, event);
  job.events.push(event);
  job.view?.update(event);
}

// つながっていない間に終わった調査。結果はDBにあるので、一覧と開いている会話を読み直す
function lostJobs() {
  const lost = [...jobs.values()].filter((j) => !j.seen);
  if (!lost.length) return;
  for (const job of lost) jobs.delete(job.turnId);
  refreshList();
  refreshFolders();
  updateTitle();
  setSendButton();
  // 終わっていれば開き直すと回答が出る。最初の調査の途中でサーバーが止まった会話は、もうない
  const current = lost.find((j) => j.conversationId === currentId);
  if (current) {
    // 回答が保存されていなければ（中断された）、質問を入力欄に戻して聞き直せるようにする
    const restore = () => current.mine && restoreQuestion(current.mine.content, current.mine.images);
    openConversation(currentId)
      .then(() => !current.info.replace && turnCount <= current.info.index && restore())
      .catch(() => {
        startNew(viewFolderId);
        restore();
      });
  }
  toast("つなぎ直しました", "アプリが止まっていた間の調査は、終わっていなければ中断されています。", null);
}

// 終わった調査の turn_id（受け付けの返事より先に終わりが届いたときのため）
const finished = new Set();

function finishJob(job, event) {
  jobs.delete(job.turnId);
  finished.add(job.turnId);
  // 読んだ印を付け終えてから一覧を読み直す（先に読むと、届いたばかりの未読の印が残る）
  let read = null;
  const shown = job.conversationId === currentId && job.turnNode?.isConnected;
  const title = job.info.title || conversations.find((c) => c.id === job.conversationId)?.title || job.info.question;
  if (event.type === "done") {
    if (shown) {
      const { index } = job.info;
      job.view.node.replaceWith(answerView(event.turn, index));
      const block = job.turnNode.querySelector(".question-block");
      if (event.question.image_text) block.append(imageTextView(event.question.image_text));
      if (event.question.since) job.turnNode.querySelector(".followup-since").textContent = followupLabel(event.question.since);
      // 画像はサーバーに保存された分に差し替える（リロードしなくても開ける）
      job.turnNode.images = (event.question.images ?? []).map((id) => `/api/images/${id}`);
      showFollowupOnLast();
      turnCount = index + 2;
      setPlaceholder(event.turn);
    }
    if (shown && !document.hidden) read = markRead(job.conversationId);
    else arrived(job.conversationId, title, event.turn);
  } else if (event.type === "cancelled") {
    if (shown) {
      job.turnNode.remove();
      if (job.mine) restoreQuestion(job.mine.content, job.mine.images);
    }
    if (job.conversationId === currentId) {
      if (event.deleted) {
        currentId = null;
        showEmpty();
      } else if (job.info.replace) openConversation(currentId);
    }
  } else {
    if (shown) {
      job.view.node.replaceWith(el("p", "error-text turn-error", `調査できませんでした: ${event.message}`));
      if (job.mine) restoreQuestion(job.mine.content, job.mine.images);
    }
    if (job.conversationId === currentId && event.deleted) {
      // 会話はなくなったので、次に送る質問は新しい調査として送る（エラーの表示は残す）
      currentId = null;
      openedConversation = null;
      turnCount = 0;
      renderHead({ title: job.info.question });
    }
    if (!shown || document.hidden) notify(`調査できませんでした: ${title}`, event.message, event.deleted ? null : job.conversationId);
  }
  setSendButton();
  updateTitle();
  Promise.resolve(read).then(() => Promise.all([refreshList(), refreshFolders()]));
}

// ---- 回答が届いたときの知らせ ----

// この画面を開いてから届いて、まだ見ていない回答の会話
const arrivals = new Set();

function arrived(conversationId, title, turn) {
  arrivals.add(conversationId);
  const heading = turn.kind === "clarify" ? `${turn.agent}所長から確認があります` : `${turn.agent}所長から回答が届きました`;
  notify(heading, title, conversationId);
}

// 画面の外（デスクトップ通知）と中（右下の知らせ）で知らせる。conversationId があれば、押すとその会話を開く
function notify(heading, body, conversationId) {
  if (document.hidden && "Notification" in window && Notification.permission === "granted") {
    const n = new Notification(heading, { body, icon: "/static/ghost.svg", tag: conversationId ?? undefined });
    n.addEventListener("click", () => {
      window.focus();
      if (conversationId) openConversation(conversationId);
      n.close();
    });
  }
  toast(heading, body, conversationId);
}

const toasts = document.getElementById("toasts");

function toast(heading, body, conversationId) {
  const node = el("div", "toast");
  const text = el("div", "toast-text");
  text.append(el("b", "", heading), el("span", "", body));
  node.append(text);
  if (conversationId) {
    const open = el("button", "primary", "開く");
    open.addEventListener("click", () => {
      node.remove();
      openConversation(conversationId);
    });
    node.append(open);
  }
  const close = el("button", "close", "×");
  close.title = "閉じる";
  close.addEventListener("click", () => node.remove());
  node.append(close);
  // 同じ会話の知らせは新しいものだけにする
  if (conversationId) {
    node.dataset.conversation = conversationId;
    toasts.querySelector(`[data-conversation="${conversationId}"]`)?.remove();
  }
  toasts.append(node);
  setTimeout(() => node.remove(), 15000);
}

// 最初に質問するときに、デスクトップ通知を出してよいか聞く（押したときでないと聞けない）
function askNotificationPermission() {
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
}

async function markRead(conversationId) {
  arrivals.delete(conversationId);
  for (const t of toasts.querySelectorAll(`[data-conversation="${conversationId}"]`)) t.remove();
  updateTitle();
  const c = conversations.find((c) => c.id === conversationId);
  if (c) c.unread = false;
  renderList();
  await api("PATCH", `/api/conversations/${conversationId}`, { unread: false });
}

// タブの題名に、調査中の数とまだ見ていない回答の数を出す（別のタブで作業していても気づけるように）
function updateTitle() {
  const parts = [];
  if (arrivals.size) parts.push(`(${arrivals.size})`);
  if (jobs.size) parts.push(`調査中${jobs.size}件`);
  document.title = [...parts, "おばけ探偵事務所"].join(" ");
}

// 裏に回していたタブに戻ってきたら、開いている会話の回答は読んだことにする
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && currentId && (arrivals.has(currentId) || currentConversation()?.unread)) markRead(currentId);
});

// ---- 質問に添えるファイル ----

const IMAGE_COUNT = 4; // サーバーの上限（web.IMAGE_COUNT）と同じ
const IMAGE_SIDE = 1600; // これより大きい画像は縮めて送る（LLMの負荷を減らす）
const IMAGE_DATA_LENGTH = 6_500_000; // data URL の長さの目安（5MB の画像）
const attachBox = document.getElementById("attachments");
const attachButton = document.getElementById("attach");
const attachFile = document.getElementById("attach-file");
const composerStatus = document.getElementById("composer-status");
// 添えた画像（data URL）
let attachments = [];
// 今のモデルが画像を読めるか（設定から）
let visionEnabled = false;

function showComposerStatus(text) {
  composerStatus.textContent = text;
  setTimeout(() => composerStatus.textContent === text && (composerStatus.textContent = ""), 4000);
}

// 添えられたファイル。画像は画像として、それ以外はテキストとして入力欄に貼る
function addFiles(files) {
  addImages(files);
  addTextFiles([...files].filter((f) => !f.type.startsWith("image/")));
}

// ログや設定ファイルは、中身をコードブロックにして入力欄の最後に貼る（そのままLLMに送られる）
const TEXT_FILE_SIZE = 200_000;
const TEXT_FILE_TYPES = "text/*,.log,.md,.csv,.tsv,.json,.yaml,.yml,.conf,.cfg,.ini,.xml,.sh,.py,.sql,.toml,.tf,.txt";

async function addTextFiles(files) {
  for (const file of files) {
    if (file.size > TEXT_FILE_SIZE) {
      showComposerStatus(`${file.name} は大きすぎます（${Math.round(TEXT_FILE_SIZE / 1000)}KBまで）。必要な部分だけを貼ってください`);
      continue;
    }
    const text = await file.text();
    if (text.includes("\0")) {
      showComposerStatus(`${file.name} は文字のファイルではないので添えられません`);
      continue;
    }
    // 中に ``` があっても閉じないよう、4つで囲む
    const block = `\`\`\`\`${file.name}\n${text.replace(/\n+$/, "")}\n\`\`\`\``;
    input.value = input.value.trim() ? `${input.value.replace(/\s+$/, "")}\n\n${block}` : `${block}\n\n`;
    autosize();
  }
  input.focus();
}

async function addImages(files) {
  const images = [...files].filter((f) => f.type.startsWith("image/"));
  if (!images.length) return;
  if (!visionEnabled) return showComposerStatus("今のモデルは画像を読めません");
  for (const file of images) {
    if (attachments.length >= IMAGE_COUNT) {
      showComposerStatus(`画像は${IMAGE_COUNT}枚までです`);
      break;
    }
    try {
      attachments.push(await shrinkImage(file));
    } catch {
      showComposerStatus("画像を読み込めませんでした");
    }
  }
  renderAttachments();
}

// 長い辺を IMAGE_SIDE までに縮め、data URL にする。スクリーンショットの文字が潰れないよう、PNGはPNGのまま
async function shrinkImage(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, IMAGE_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = Object.assign(document.createElement("canvas"), {
    width: Math.round(bitmap.width * scale),
    height: Math.round(bitmap.height * scale),
  });
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const type = ["image/png", "image/jpeg", "image/webp"].includes(file.type) ? file.type : "image/png";
  const url = canvas.toDataURL(type, 0.9);
  return url.length <= IMAGE_DATA_LENGTH ? url : canvas.toDataURL("image/jpeg", 0.85);
}

function renderAttachments() {
  attachBox.hidden = !attachments.length;
  attachBox.replaceChildren(
    ...attachments.map((url, i) => {
      const item = el("div", "attachment");
      const img = Object.assign(el("img"), { src: url, alt: `添えた画像${i + 1}` });
      const remove = el("button", "attachment-remove", "×");
      remove.title = "外す";
      remove.addEventListener("click", () => {
        attachments.splice(i, 1);
        renderAttachments();
      });
      item.append(img, remove);
      return item;
    }),
  );
}

attachButton.addEventListener("click", () => attachFile.click());

attachFile.addEventListener("change", () => {
  addFiles(attachFile.files);
  attachFile.value = "";
});

input.addEventListener("paste", (e) => {
  const files = [...e.clipboardData.files].filter((f) => f.type.startsWith("image/"));
  if (!files.length) return;
  e.preventDefault();
  addImages(files);
});

form.addEventListener("dragover", (e) => {
  if (![...e.dataTransfer.types].includes("Files")) return;
  e.preventDefault();
  form.classList.add("dragging");
});

form.addEventListener("dragleave", () => form.classList.remove("dragging"));

form.addEventListener("drop", (e) => {
  form.classList.remove("dragging");
  if (!e.dataTransfer.files.length) return;
  e.preventDefault();
  addFiles(e.dataTransfer.files);
});

// ---- 設定と調べる場所 ----

// 使えるソースの一覧（入力欄を描き直すときに使う）
let sourceList = [];

async function loadConfig() {
  const c = await api("GET", "/api/config");
  appConfig = c;
  renderDestination(c);
  renderSetup();
  visionEnabled = c.vision;
  attachFile.accept = (c.vision ? "image/png,image/jpeg,image/webp,image/gif," : "") + TEXT_FILE_TYPES;
  directorName = c.director;
  sourceList = c.sources;
  sourceLabels = new Map(c.sources.map((s) => [s.id, s.label]));
  renderSources(c.sources);
}

// 設定（/api/config）。はじめにの案内に使う
let appConfig = null;

// 最初の画面の「はじめに」。モックのLLMやサンプルのソースで動いているときに、何を設定すれば本番で使えるかを出す
function renderSetup() {
  const box = document.getElementById("setup");
  if (!box || !appConfig) return;
  const llmReady = appConfig.model !== "mock";
  const sourcesReady = !appConfig.demo;
  box.hidden = llmReady && sourcesReady;
  const step = (done, title, body) => {
    const item = el("li", done ? "done" : "");
    item.append(icon(done ? "check" : "circle"));
    const text = el("div");
    text.append(el("b", "", title), el("span", "", body));
    item.append(text);
    return item;
  };
  const list = el("ol", "setup-steps");
  list.append(
    step(llmReady, "LLMを決める", llmReady ? `${appConfig.model} を使っています` : "今はお試しのモックLLMです。.env の LLM_BASE_URL・LLM_MODEL に、使うLLMを書いて再起動します"),
    step(sourcesReady, "調べる場所をつなぐ", sourcesReady ? "つないだ場所を調べます" : "今はサンプルの架空のデータを調べます。GitHub・GitLab・Zulip のトークンを .env に書くか、MCPサーバーを画面から足します"),
  );
  const open = iconButton("plug", "ソースと接続を開く", "primary");
  open.addEventListener("click", () => document.getElementById("open-status").click());
  box.replaceChildren(el("div", "setup-head", "はじめに：お試しで動いています"), list, open);
}

// LLMの送り先。秘密の情報を入れてよいか判断できるよう、手元のネットワークの中か外かを出す
const DESTINATIONS = {
  local: ["ローカル", "手元のネットワークの中"],
  external: ["外部", "インターネット上"],
  unknown: ["不明", "送り先のアドレスが分からない場所"],
};

let destinationNote = "";

function renderDestination({ model, vision, llm_url, destination: d }) {
  const [label, where] = DESTINATIONS[d.kind];
  const badge = document.getElementById("model");
  badge.replaceChildren(el("span", `destination ${d.kind}`, label), el("span", "model-name", model));
  const addresses = d.addresses.length ? `（${d.addresses.join(", ")}）` : "";
  badge.title =
    `質問と検索結果は、${where}のLLMに送ります。\n送り先: ${llm_url}${addresses}\nモデル: ${model}${vision ? "（画像を読める）" : ""}` +
    (d.kind === "local" ? "" : "\n秘密の情報を入れる前に、送ってよい相手か確かめてください。");
  destinationNote =
    d.kind === "local"
      ? `質問と検索結果は、${where}のLLM（${d.host}）にだけ送ります。`
      : `質問と検索結果を、${where}のLLM（${d.host}）に送ります。秘密の情報を入れる前に確かめてください。`;
  const note = document.getElementById("destination-note");
  if (note) {
    note.textContent = destinationNote;
    note.className = `destination-note ${d.kind}`;
  }
}

// 今のフォルダ。開いている調査があればそのフォルダ、なければサイドバーで選んでいるフォルダ
function activeFolderId() {
  return currentId ? (currentConversation()?.folder_id ?? null) : viewFolderId;
}

function activeFolder() {
  return folders.find((f) => f.id === activeFolderId()) ?? null;
}

// 今のフォルダ（なければ全体）で覚えている選択を入力欄に出す
async function applySelection() {
  const saved = activeFolder()?.selection ?? (await api("GET", "/api/settings")).selection;
  selection = { unchecked: saved.unchecked ?? [], scopes: saved.scopes ?? {} };
  renderSources(sourceList);
}

// 入力欄の選択を、今のフォルダ（なければ全体）に保存する
async function saveSelection() {
  const folder = activeFolder();
  if (folder) {
    folder.selection = structuredClone(selection);
    await api("PATCH", `/api/folders/${folder.id}`, { selection });
  } else {
    await api("PATCH", "/api/settings", { selection });
  }
}

function renderSources(sources) {
  const unchecked = new Set(selection.unchecked);
  sourceBox.replaceChildren(
    ...sources.map((s) => {
      const item = el("div", "source-item");
      const label = el("label", "source");
      label.title = `${s.agent}が調べます`;
      const box = el("input");
      box.type = "checkbox";
      box.value = s.id;
      box.checked = !unchecked.has(s.id);
      const chip = el("span", "", s.label);
      chip.append(el("small", "", s.agent));
      label.append(box, chip);
      item.append(label);
      if (s.scope_label) item.append(scopeButton(s));
      return item;
    }),
  );
  setSendButton();
}

// 範囲を選べるソースのチップの横に出すボタン。今の選択を短く出す
function scopeButton(source) {
  const button = el("button", "scope-button");
  const names = selection.scopes[source.id] ?? [];
  button.textContent = names.length ? (names.length === 1 ? names[0] : `${names[0]} ほか${names.length - 1}件`) : `すべての${source.scope_label}`;
  button.title = `${source.label}で調べる${source.scope_label}を選ぶ`;
  button.classList.toggle("narrowed", names.length > 0);
  button.addEventListener("click", () => openScope(source, button));
  return button;
}

function selectedSources() {
  return [...sourceBox.querySelectorAll("input:checked")].map((box) => box.value);
}

sourceBox.addEventListener("change", () => {
  selection.unchecked = [...sourceBox.querySelectorAll("input:not(:checked)")].map((box) => box.value);
  saveSelection();
  setSendButton();
});

// ---- 調べる範囲を選ぶ ----

const scopeDialog = document.getElementById("scope");
const scopeList = document.getElementById("scope-list");
const scopeFilter = document.getElementById("scope-filter");
// 開いている範囲の選択 { source, button, choices, checked: Set }
let scopeEditing = null;

async function openScope(source, button) {
  scopeEditing = { source, button, choices: [], checked: new Set(selection.scopes[source.id] ?? []) };
  document.getElementById("scope-title").textContent = `${source.label}で調べる${source.scope_label}`;
  document.getElementById("scope-note").textContent = `選んだ${source.scope_label}だけを${source.agent}が調べます。何も選ばなければ、すべてを調べます。`;
  scopeFilter.value = "";
  scopeList.replaceChildren(el("div", "conv-empty", "読み込んでいます…"));
  scopeDialog.showModal();
  try {
    const choices = await api("GET", `/api/sources/${source.id}/scopes`);
    // 以前選んだのに一覧から消えたものも、外せるように出しておく
    scopeEditing.choices = [...new Set([...scopeEditing.checked, ...choices])];
    renderScopeList();
  } catch (err) {
    scopeList.replaceChildren(el("div", "error-text", `一覧を取れませんでした: ${err.message}`));
  }
}

function renderScopeList() {
  const query = scopeFilter.value.trim().toLowerCase();
  const shown = scopeEditing.choices.filter((name) => name.toLowerCase().includes(query));
  scopeList.replaceChildren(
    ...shown.map((name) => {
      const label = el("label", "scope-choice");
      const box = el("input");
      box.type = "checkbox";
      box.checked = scopeEditing.checked.has(name);
      box.addEventListener("change", () => (box.checked ? scopeEditing.checked.add(name) : scopeEditing.checked.delete(name)));
      label.append(box, name);
      return label;
    }),
  );
  if (!shown.length) scopeList.append(el("div", "conv-empty", "見つかりませんでした"));
}

scopeFilter.addEventListener("input", renderScopeList);


document.getElementById("scope-clear").addEventListener("click", () => {
  scopeEditing.checked.clear();
  renderScopeList();
});

document.getElementById("scope-done").addEventListener("click", () => {
  const { source, button, checked } = scopeEditing;
  if (checked.size) selection.scopes[source.id] = [...checked];
  else delete selection.scopes[source.id];
  saveSelection();
  button.replaceWith(scopeButton(source));
  scopeDialog.close();
});

// ---- 調査の一覧 ----

document.getElementById("new-chat").addEventListener("click", () => startNew(viewFolderId));

// 打つたびにサーバーで探す（中身まで探すので、打ち終わるのを少し待つ）
let searchTimer = null;

search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(refreshList, 250);
});

document.getElementById("toggle-archive").addEventListener("click", (e) => {
  showingArchive = !showingArchive;
  setLabel(e.currentTarget, showingArchive ? "一覧に戻る" : "アーカイブ");
  e.currentTarget.classList.toggle("active", showingArchive);
  document.querySelector(".sidebar").classList.toggle("archive-mode", showingArchive);
  refreshList();
});

function currentConversation() {
  return conversations.find((c) => c.id === currentId) ?? openedConversation;
}

// 新しい調査を始める。folderId のフォルダに入る（null ならフォルダの外）
function startNew(folderId) {
  currentId = null;
  viewFolderId = folderId;
  showEmpty();
  highlightCurrent();
  renderFolders();
  applySelection();
  refreshList();
  input.focus();
}

// 最後に開こうとした会話（読み込みの途中で別の会話を開いたら、前の結果は捨てる）
let opening = null;

async function openConversation(id) {
  opening = id;
  const data = await api("GET", `/api/conversations/${id}`);
  if (opening !== id) return;
  const folderBefore = activeFolderId();
  currentId = id;
  openedConversation = conversations.find((c) => c.id === id) ?? null;
  if (activeFolderId() !== folderBefore) applySelection();
  renderHead();
  const nodes = [];
  data.turns.forEach((t, i) => {
    if (t.role === "user") {
      const images = (t.images ?? []).map((id) => `/api/images/${id}`);
      nodes.push(turnView(t.content, i, { images, imageText: t.image_text, since: t.since }));
    }
    else nodes.at(-1).append(answerView(t, i - 1));
  });
  turnCount = data.turns.length;
  log.replaceChildren(...nodes);
  const job = jobFor(id);
  if (job) showJob(job);
  showFollowupOnLast();
  log.scrollTop = log.scrollHeight;
  // 検索して開いたときは、見つかった言葉に印を付けて最初の場所を見せる
  markSearchWords();
  setPlaceholder(data.turns.at(-1));
  highlightCurrent();
  setSendButton();
  if (!document.hidden && (openedConversation?.unread || arrivals.has(id))) markRead(id);
}

function markSearchWords() {
  const words = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return;
  const walker = document.createTreeWalker(log, NodeFilter.SHOW_TEXT);
  const hits = [];
  for (let node; (node = walker.nextNode()); ) {
    // たたんだ調査の記録の中までは印を付けない（開かないと見えないので）
    if (node.parentElement.closest("details:not([open]), button, mark")) continue;
    if (words.some((w) => node.data.toLowerCase().includes(w))) hits.push(node);
  }
  for (const node of hits) {
    const text = node.data;
    const lower = text.toLowerCase();
    const parts = [];
    let at = 0;
    while (at < text.length) {
      // いちばん手前に出てくる言葉から印を付ける
      const next = words.map((w) => [lower.indexOf(w, at), w]).filter(([i]) => i >= 0).sort((a, b) => a[0] - b[0])[0];
      if (!next) break;
      const [i, w] = next;
      parts.push(text.slice(at, i), el("mark", "search-hit", text.slice(i, i + w.length)));
      at = i + w.length;
    }
    parts.push(text.slice(at));
    node.replaceWith(...parts);
  }
  log.querySelector("mark.search-hit")?.scrollIntoView({ block: "center" });
}

async function deleteConversation(id) {
  if (!confirm("この調査を削除しますか？")) return;
  await api("DELETE", `/api/conversations/${id}`);
  if (id === currentId) {
    currentId = null;
    showEmpty();
  }
  await refreshList();
}

async function refreshList() {
  const params = new URLSearchParams({ q: search.value.trim() });
  if (showingArchive) params.set("archived", "1");
  if (viewFolderId) params.set("folder", viewFolderId);
  conversations = await api("GET", `/api/conversations?${params}`);
  if (currentId) openedConversation = conversations.find((c) => c.id === currentId) ?? openedConversation;
  renderList();
}

// 更新日ごとにまとめて出す
function renderList() {
  const nodes = [];
  let group = null;
  for (const c of conversations) {
    const g = dateGroup(c.updated_at);
    if (g !== group) nodes.push(el("div", "conv-group", (group = g)));
    nodes.push(conversationItem(c));
  }
  if (!conversations.length) {
    const text = search.value.trim()
      ? "見つかりませんでした"
      : showingArchive
        ? "アーカイブした調査はありません"
        : viewFolderId
          ? "このフォルダにはまだ調査がありません"
          : "まだ調査はありません";
    nodes.push(el("div", "conv-empty", text));
  }
  list.replaceChildren(...nodes);
  highlightCurrent();
}

function dateGroup(iso) {
  const day = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((day(new Date()) - day(new Date(iso))) / 86400000);
  if (days <= 0) return "今日";
  if (days === 1) return "昨日";
  if (days < 7) return "この1週間";
  if (days < 30) return "この1か月";
  return "それより前";
}

function conversationItem(c) {
  const item = el("div", "conv");
  item.dataset.id = c.id;
  const title = el("button", "conv-title");
  title.title = c.title;
  title.append(el("span", "conv-name", c.title));
  // 調査中なら動く印、まだ見ていない回答があれば点を出す
  if (jobFor(c.id)) {
    item.classList.add("running");
    title.append(el("span", "conv-state running", "調査中"));
  } else if (c.unread && c.id !== currentId) {
    item.classList.add("unread");
    title.append(Object.assign(el("span", "conv-state unread", ""), { title: "まだ見ていない回答があります" }));
  }
  title.addEventListener("click", () => openConversation(c.id));
  const del = el("button", "conv-delete", "×");
  del.title = "削除";
  del.addEventListener("click", () => deleteConversation(c.id));
  item.append(title, del);
  return item;
}

function highlightCurrent() {
  for (const item of list.querySelectorAll(".conv")) item.classList.toggle("active", item.dataset.id === currentId);
}

let listTimer = null;

function refreshListSoon() {
  clearTimeout(listTimer);
  listTimer = setTimeout(refreshList, 200);
}

// ---- フォルダ ----

const folderList = document.getElementById("folder-list");
const folderDialog = document.getElementById("folder-dialog");
// 設定を開いているフォルダ
let editingFolder = null;

async function refreshFolders() {
  folders = await api("GET", "/api/folders");
  renderFolders();
}

function renderFolders() {
  const item = (id, name, count) => {
    const row = el("div", "folder-item");
    row.classList.toggle("active", id === viewFolderId);
    const open = el("button", "folder-open");
    open.append(icon(id ? "folder" : "inbox"), el("span", "folder-name", name));
    if (count !== undefined) open.append(el("span", "folder-count", String(count)));
    open.addEventListener("click", () => startNew(id));
    row.append(open);
    if (id) {
      const settings = el("button", "folder-settings");
      settings.append(icon("more"));
      settings.title = `「${name}」の名前・削除`;
      settings.setAttribute("aria-label", settings.title);
      settings.addEventListener("click", () => openFolderDialog(id));
      row.append(settings);
    }
    return row;
  };
  folderList.replaceChildren(item(null, "すべての調査"), ...folders.map((f) => item(f.id, f.name, f.count)));
}

document.getElementById("new-folder").addEventListener("click", (e) => {
  if (folderList.querySelector(".folder-input")) return;
  const field = el("input", "folder-input");
  field.placeholder = "フォルダの名前（案件名など）";
  field.maxLength = 40;
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const name = field.value.trim();
    field.remove();
    if (!save || !name) return;
    const folder = await api("POST", "/api/folders", { name });
    await refreshFolders();
    startNew(folder.id);
  };
  field.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.isComposing) finish(true);
    if (ev.key === "Escape") finish(false);
  });
  field.addEventListener("blur", () => finish(true));
  folderList.append(field);
  field.focus();
});

function openFolderDialog(id) {
  editingFolder = folders.find((f) => f.id === id);
  document.getElementById("folder-name").value = editingFolder.name;
  document.getElementById("folder-status").textContent = "";
  folderDialog.showModal();
}


document.getElementById("folder-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("PATCH", `/api/folders/${editingFolder.id}`, { name: document.getElementById("folder-name").value });
  } catch (err) {
    document.getElementById("folder-status").textContent = err.message;
    return;
  }
  folderDialog.close();
  await refreshFolders();
  renderHead();
});

document.getElementById("delete-folder").addEventListener("click", async () => {
  if (!confirm(`フォルダ「${editingFolder.name}」を削除しますか？中の調査は消えず、フォルダの外に出ます。`)) return;
  await api("DELETE", `/api/folders/${editingFolder.id}`);
  folderDialog.close();
  const wasViewing = viewFolderId === editingFolder.id;
  await refreshFolders();
  if (wasViewing) startNew(null);
  else {
    await refreshList();
    renderHead();
  }
});

// ---- 開いている調査の見出しと操作 ----

const archiveButton = document.getElementById("archive");
const crumb = document.getElementById("folder-crumb");

// 新しい会話を送った直後は、まだ一覧にないので draft（仮の名前）を出す
function renderHead(draft) {
  const c = draft ? null : currentConversation();
  titleEl.textContent = c?.title ?? draft?.title ?? "新しい調査";
  titleEl.classList.toggle("editable", Boolean(c));
  titleEl.title = c ? "クリックして名前を変える" : "";
  menuButton.hidden = !c;
  if (!c) closeMenu();
  setLabel(archiveButton, c?.archived ? "アーカイブから戻す" : "アーカイブ");
  const folder = activeFolder();
  crumb.hidden = !folder;
  crumb.replaceChildren(...(folder ? [icon("folder"), folder.name] : []));
  crumb.title = folder ? `フォルダ「${folder.name}」で新しい調査を始める` : "";
  if (c) {
    // フォルダへ移すメニュー。今のフォルダには印を付ける
    const item = (id, name, iconName) => {
      const button = el("button", (c.folder_id ?? null) === id ? "current" : "");
      button.setAttribute("role", "menuitemradio");
      button.setAttribute("aria-checked", String((c.folder_id ?? null) === id));
      button.append(icon((c.folder_id ?? null) === id ? "check" : iconName), name);
      button.addEventListener("click", () => moveTo(id));
      return button;
    };
    moveSelect.replaceChildren(item(null, "フォルダの外", "inbox"), ...folders.map((f) => item(f.id, f.name, "folder")));
  }
}

function renameConversation() {
  const c = currentConversation();
  if (!c || titleEl.querySelector("input")) return;
  const field = el("input", "title-input");
  field.value = c.title;
  field.maxLength = 80;
  const done = async (save) => {
    const title = field.value.trim();
    if (save && title && title !== c.title) {
      await api("PATCH", `/api/conversations/${c.id}`, { title });
      c.title = title;
      renderList();
    }
    renderHead();
  };
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) field.blur();
    if (e.key === "Escape") {
      field.value = c.title;
      field.blur();
    }
  });
  field.addEventListener("blur", () => done(true));
  titleEl.replaceChildren(field);
  field.focus();
  field.select();
}

titleEl.addEventListener("click", renameConversation);

crumb.addEventListener("click", () => startNew(activeFolderId()));

const menuButton = document.getElementById("conv-menu-button");
const menu = document.getElementById("conv-menu");

function closeMenu() {
  menu.hidden = true;
  menuButton.setAttribute("aria-expanded", "false");
}

menuButton.addEventListener("click", (e) => {
  e.stopPropagation();
  menu.hidden = !menu.hidden;
  menuButton.setAttribute("aria-expanded", String(!menu.hidden));
  if (!menu.hidden) menu.querySelector("button")?.focus();
});

document.addEventListener("click", (e) => !menu.hidden && !menu.contains(e.target) && closeMenu());

document.addEventListener("keydown", (e) => e.key === "Escape" && !menu.hidden && (closeMenu(), menuButton.focus()));

document.getElementById("rename").addEventListener("click", () => {
  closeMenu();
  renameConversation();
});

document.getElementById("delete-conversation").addEventListener("click", () => {
  closeMenu();
  deleteConversation(currentId);
});

archiveButton.addEventListener("click", async () => {
  const c = currentConversation();
  closeMenu();
  await api("PATCH", `/api/conversations/${c.id}`, { archived: !c.archived });
  c.archived = !c.archived;
  renderHead();
  await refreshList();
});

const moveSelect = document.getElementById("move-folder");

// 開いている調査を別のフォルダへ移す
async function moveTo(folderId) {
  const c = currentConversation();
  closeMenu();
  if ((c.folder_id ?? null) === folderId) return;
  await api("PATCH", `/api/conversations/${c.id}`, { folder_id: folderId });
  c.folder_id = folderId;
  await Promise.all([refreshFolders(), refreshList()]);
  applySelection();
  renderHead();
}

// 開いている調査を、質問・回答・出典のMarkdownにしてファイルに保存する
document.getElementById("export").addEventListener("click", async () => {
  closeMenu();
  const c = currentConversation();
  if (!c) return;
  const { turns } = await api("GET", `/api/conversations/${c.id}`);
  const blob = new Blob([conversationMarkdown(c.title, turns)], { type: "text/markdown" });
  const link = Object.assign(el("a"), { href: URL.createObjectURL(blob), download: `${fileName(c.title)}.md` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});

function conversationMarkdown(title, turns) {
  const lines = [`# ${title}`, ""];
  for (const t of turns) {
    if (t.role === "user") {
      lines.push(`## 質問${t.since ? `（${t.since} 以降の続報）` : ""}`, "", t.content, "");
      continue;
    }
    lines.push(`## ${t.agent}所長の${t.kind === "clarify" ? "確認" : "回答"}${t.date ? `（${t.date}）` : ""}`, "", t.content, "");
    const found = (t.links ?? []).filter((l) => l.found);
    if (found.length) {
      lines.push("### 出典", "");
      found.forEach((l, i) => lines.push(`${i + 1}. [${l.title || l.url}](${l.url})${[l.source, l.date].filter(Boolean).map((x) => ` · ${x}`).join("")}`));
      lines.push("");
    }
  }
  return lines.join("\n");
}

function fileName(title) {
  return title.replace(/[\\/:*?"<>|\s]+/g, "_").slice(0, 60) || "調査";
}

// ---- 調査と回答の表示 ----

// 1往復分の入れ物。質問を出し、回答はあとから足す
// index は質問のターンの位置。やり直すときにサーバーへ渡す
// images は質問に添えた画像のURL、imageText はそれを所長が文字に起こしたもの
// imageCount は、画像はあるがまだ見られない（別の画面から送った調査の途中）ときの枚数
function turnView(question, index, { images = [], imageText = "", followup = false, since = "", imageCount = 0 } = {}) {
  const node = el("section", "turn");
  node.dataset.index = index;
  // 質問の元の文章（作り直すときに使う。画面の吹き出しは貼ったファイルを整えて出すので、文字が違う）
  node.question = question;
  node.images = images;
  node.followup = followup || Boolean(since);
  const block = el("div", "question-block");
  const bubble = el("div", "question");
  bubble.append(...questionNodes(question));
  // 貼ったログなどで長い質問は、たたんでおく
  let more = null;
  if (question.length > 600 || question.split("\n").length > 12) {
    bubble.classList.add("clamped");
    more = el("button", "question-more quiet", "全文を見る");
    more.addEventListener("click", () => {
      const open = bubble.classList.toggle("clamped");
      more.textContent = open ? "全文を見る" : "たたむ";
    });
  }
  const edit = el("button", "question-edit", "編集");
  edit.title = "質問を直して調べ直す";
  edit.addEventListener("click", () => editQuestion(node, question, index));
  const row = el("div", "question-row");
  row.append(edit, bubble);
  block.append(row);
  if (more) block.append(more);
  if (node.followup) block.append(el("span", "pill followup followup-since", since ? followupLabel(since) : "続報"));
  if (images.length) {
    const gallery = el("div", "question-images");
    for (const src of images) {
      const link = Object.assign(el("a"), { href: src, target: "_blank", rel: "noopener" });
      link.append(Object.assign(el("img"), { src, alt: "添えた画像" }));
      gallery.append(link);
    }
    block.append(gallery);
  }
  if (imageCount) block.append(el("span", "pill unset", `画像${imageCount}枚（調査が終わると見られます）`));
  if (imageText) block.append(imageTextView(imageText));
  node.append(block);
  return node;
}

// 質問の文章。貼ったファイル（```` で囲んだ部分）だけは等幅で出す。ほかは書いたとおりに出す
function questionNodes(text) {
  const nodes = [];
  let at = 0;
  for (const m of text.matchAll(/^(`{3,})([^\n]*)\n([\s\S]*?)\n\1$/gm)) {
    nodes.push(text.slice(at, m.index).replace(/\n+$/, ""));
    const pre = el("pre", "question-code");
    if (m[2].trim()) pre.append(el("span", "question-code-name", m[2].trim()));
    pre.append(m[3]);
    nodes.push(pre);
    at = m.index + m[0].length;
  }
  nodes.push(text.slice(at).replace(/^\n+/, ""));
  return nodes.filter((n) => n !== "");
}

function followupLabel(since) {
  return `続報（${since} 以降）`;
}

// 「その後どうなった？」は最後の回答にだけ出す（途中の回答の続報は、会話の流れと合わなくなる）
function showFollowupOnLast() {
  // 最後が所長の聞き返しなら、どれも出さない（先に答えてもらう）
  const last = [...log.querySelectorAll(".turn")].at(-1);
  for (const b of log.querySelectorAll(".followup-ask")) b.hidden = b.closest(".turn") !== last;
}

function askFollowup() {
  if (busy()) return explainBusy();
  ask("その後どうなった？", null, false, [], [], true);
}

// 添えた画像から所長が読み取った内容。読み違いに気づけるよう、たたんで出しておく
function imageTextView(text) {
  const details = el("details", "image-text");
  details.append(el("summary", "", "画像から読み取った内容"), el("div", "image-text-body", text));
  return details;
}

// より後のやりとりが消えるときは確かめる
function confirmRedo(index) {
  if (index + 2 >= turnCount) return true;
  return confirm("この質問より後のやりとりは消えます。調べ直しますか？");
}

// 画像はサーバーが元の質問のものを使い直すので、送らずに見せるだけ
function redo(content, index, skipClarify = false) {
  if (busy()) return explainBusy();
  if (!confirmRedo(index)) return;
  const node = log.querySelector(`.turn[data-index="${index}"]`);
  ask(content, index, skipClarify, [], turnImages(index), Boolean(node?.followup));
}

function turnImages(index) {
  return log.querySelector(`.turn[data-index="${index}"]`)?.images ?? [];
}

// 所長が聞き返しているときは、答えを書く欄だと分かるようにする（元の文言は最初に1度だけ覚える）
function setPlaceholder(lastTurn) {
  input.dataset.placeholder ??= input.placeholder;
  input.placeholder = lastTurn?.kind === "clarify" ? "所長の質問に答える（Enterで送信）" : input.dataset.placeholder;
}

function editQuestion(node, question, index) {
  if (busy()) return explainBusy();
  const row = node.querySelector(".question-row");
  const editor = el("div", "question-editor");
  const field = el("textarea");
  field.value = question;
  field.rows = Math.min(8, question.split("\n").length + 1);
  const cancel = el("button", "quiet", "やめる");
  const submit = el("button", "primary", "調べ直す");
  const actions = el("div", "dialog-actions");
  actions.append(cancel, submit);
  editor.append(field, actions);
  row.replaceWith(editor);
  field.focus();
  cancel.addEventListener("click", () => editor.replaceWith(row));
  submit.addEventListener("click", () => {
    const content = field.value.trim();
    if (!content) return;
    if (busy() || !confirmRedo(index)) return;
    ask(content, index, false, [], node.images, node.followup);
  });
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit.click();
    }
    if (e.key === "Escape") cancel.click();
  });
}

// 所長の回答の枠（アイコン・名前・補足）。body に中身を足す
function answerFrame(agent) {
  const node = el("div", "answer");
  const avatar = el("img", "avatar");
  avatar.src = "/static/ghost.svg";
  avatar.alt = "";
  const body = el("div", "answer-body");
  const head = el("div", "answer-head");
  const meta = el("span", "answer-meta");
  head.append(el("span", "answer-name", `${agent}所長`), meta);
  body.append(head);
  node.append(avatar, body);
  return { node, body, meta };
}

// 調べる前に所長が聞き返したターン
function clarifyView({ agent, content }, index) {
  const { node, body, meta } = answerFrame(agent);
  meta.replaceChildren(el("span", "pill followup", "確認"));
  const note = el("div", "clarify-note", "答えを下の入力欄に書いてください。");
  const skip = iconButton("search", "聞き返さずに調べる");
  skip.title = "今の質問のまま、所員に調べさせます";
  skip.addEventListener("click", (e) => redo(e.currentTarget.closest(".turn").question, index, true));
  const actions = el("div", "answer-actions");
  actions.append(skip);
  body.append(markdownView(content), note, actions);
  return node;
}

// 調べる場所を選ばずに所長に相談したターン。出典や調査の記録はない
function chatView(turn, index) {
  const { node, body, meta } = answerFrame(turn.agent);
  meta.replaceChildren(el("span", "pill unset", "相談（調べていません）"), " ", [turn.model, turn.seconds !== undefined && formatSeconds(turn.seconds)].filter(Boolean).join(" · "));
  body.append(markdownView(turn.content), answerActions(turn, index, "同じ頼みでもう一度答えてもらう（今選んでいる調べる場所で）"));
  return node;
}

// 回答の下の操作（コピー・作り直す・その後どうなった？・LLMとのやりとり）。続報は調べた回答にだけ出す
function answerActions(turn, index, regenerateTitle, followup = false) {
  const actions = el("div", "answer-actions");
  const regenerate = iconButton("redo", "作り直す");
  regenerate.title = regenerateTitle;
  regenerate.addEventListener("click", (e) => redo(e.currentTarget.closest(".turn").question, index));
  actions.append(copyButton(turn.content), regenerate);
  if (followup) {
    const ask = iconButton("clock", "その後どうなった？", "followup-ask");
    ask.title = turn.date ? `${turn.date} 以降の動きだけを調べる` : "この回答の日以降の動きだけを調べる";
    ask.addEventListener("click", askFollowup);
    actions.append(ask);
  }
  if (turn.trace_id) {
    const trace = iconButton("list", `LLMとのやりとり（${turn.llm_calls}回）`, "subtle");
    trace.addEventListener("click", () => openTrace(turn.trace_id));
    actions.append(trace);
  }
  return actions;
}

function answerView(turn, index) {
  if (turn.kind === "clarify") return clarifyView(turn, index);
  if (turn.kind === "chat") return chatView(turn, index);
  const { agent, content, model, reports, links, seconds, memo_added } = turn;
  const { node, body, meta } = answerFrame(agent);
  const searches = reports.reduce((n, r) => n + r.steps.length, 0);
  const members = new Set(reports.map((r) => r.source)).size;
  const followups = reports.filter((r) => r.request).length;
  meta.textContent = [
    model,
    seconds !== undefined && formatSeconds(seconds),
    `所員${members}人`,
    `検索${searches}回`,
    followups && `再調査${followups}件`,
  ]
    .filter(Boolean)
    .join(" · ");

  const answer = markdownView(content);
  const actions = answerActions(turn, index, "同じ質問でもう一度調べる", true);
  const record = el("details", "record");
  record.append(el("summary", "", "調査の記録を見る"), membersView(reports.map(memberView)));
  body.append(answer);
  // 照合の結果がない（照合を入れる前の）回答には出典を出さない
  if (links) body.append(citations(answer, links));
  if (memo_added?.length) body.append(memoAddedView(memo_added));
  body.append(actions, record);
  return node;
}

// 調査のあとに所長がメモに書き足した内容。直したいときにすぐ開けるようにする
function memoAddedView(added) {
  const box = el("div", "memo-added");
  const head = el("div", "memo-added-head", "メモに書き足しました");
  const open = el("button", "quiet", "メモを開く");
  open.addEventListener("click", openMemo);
  head.append(open);
  const list = el("ul");
  for (const a of added) {
    const where = a.folder ? `「${a.folder}」` : "全体";
    const item = el("li", a.saved ? "" : "not-saved");
    item.append(el("span", "memo-where", where), a.note);
    if (!a.saved) item.append(el("span", "memo-full", "メモがいっぱいで書き足せませんでした"));
    list.append(item);
  }
  box.append(head, list);
  return box;
}

// 回答の中のリンクに出典の番号を振り、出典の一覧を返す。検索結果になかったリンクには印を付ける
function citations(answer, links) {
  if (!links.length) {
    const none = el("div", "citations");
    none.append(el("span", "pill unset", "根拠のリンクがありません"));
    return none;
  }
  const key = (url) => url.replace(/[.,;:!?]+$/, "").replace(/\/+$/, "");
  const numbers = new Map(links.map((l, i) => [key(l.url), i + 1]));
  for (const a of answer.querySelectorAll("a")) {
    const n = numbers.get(key(a.getAttribute("href")));
    if (!n) continue;
    const link = links[n - 1];
    // URLがそのまま書かれていたら番号に置き換え、文字のリンクなら番号を後ろに添える
    let mark = a;
    if (a.textContent === a.getAttribute("href")) a.textContent = `[${n}]`;
    else a.after((mark = Object.assign(el("a", "", `[${n}]`), { href: a.href, target: "_blank", rel: "noopener noreferrer" })));
    mark.classList.add("cite");
    mark.title = link.found
      ? [link.title, link.source].filter(Boolean).join(" · ") || link.url
      : "所員の検索結果にないリンクです。LLMが作ったURLかもしれません";
    if (!link.found) mark.classList.add("unverified");
  }

  const missing = links.filter((l) => !l.found).length;
  const box = el("details", "citations");
  box.open = true;
  const summary = el("summary", "", `出典 ${links.length}件`);
  if (missing) summary.append(el("span", "pill failed", `${missing}件は検索結果にありません`));
  const list = el("ol", "citation-list");
  links.forEach((link, i) => {
    const item = el("li", link.found ? "" : "unverified");
    const a = el("a", "citation-card");
    a.href = link.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.title = link.url;
    let host = link.url;
    try {
      host = new URL(link.url).host;
    } catch {}
    const meta = link.found ? [link.source, link.date, host].filter(Boolean).join(" · ") : `検索結果にありません（LLMが作ったURLかもしれません） · ${host}`;
    a.append(el("span", "citation-number", String(i + 1)), el("span", "citation-title", link.title || link.url), el("span", "citation-meta", meta));
    item.append(a);
    list.append(item);
  });
  box.append(summary, list);
  return box;
}

// 調査中の表示。所員ごとの状態と、届いた分の回答を出す
// startedAt は調査を始めた時刻（秒）。かかっている時間を出す（ローカルLLMは時間がかかるので、止まっていないと分かるように）
// consult なら調べずに所長が答えている（相談）
function progressView(startedAt = Date.now() / 1000, consult = false) {
  const { node, body, meta: head } = answerFrame(directorName);
  const meta = el("span");
  const elapsed = el("span", "elapsed");
  head.append(meta, elapsed);
  const tick = () => {
    elapsed.textContent = formatElapsed(Date.now() / 1000 - startedAt);
  };
  tick();
  // 画面から外れたら（回答に置き換わった・別の会話を開いた）止める。作ってすぐ画面に足すので、最初の1秒の間には足されている
  const timer = setInterval(() => (node.isConnected ? tick() : clearInterval(timer)), 1000);
  meta.textContent = consult ? "考えています…" : "所員が調べています…";
  const answer = el("div", "markdown streaming");
  const record = el("details", "record");
  record.open = true;
  record.hidden = true; // 所員が動き出してから出す
  const members = membersView([]);
  record.append(el("summary", "", "調査の様子"), members);
  // 待っている間にできることを知らせる（調査は裏で続き、終わったら知らせる）
  const hint = el("p", "progress-hint", "終わったらお知らせします。待つ間に、ほかの調査を開いたり新しく頼んだりできます。");
  body.append(answer, record, hint);

  // ソースごとの今の行。再調査が始まると、その所員の行は再調査の行に切り替わる
  const rows = new Map();
  const rowFor = (source) => rows.get(source);

  // 届いた分の回答。描き直しは画面の更新1回につき1度にまとめる
  let answerText = "";
  let drawing = false;
  const drawAnswer = () => {
    if (drawing) return;
    drawing = true;
    requestAnimationFrame(() => {
      drawing = false;
      answer.replaceChildren(renderMarkdown(answerText));
      scrollToBottom();
    });
  };

  const update = (event) => {
    if (event.type === "images_start") {
      meta.textContent = `添えられた画像（${event.count}枚）を読んでいます…`;
    } else if (event.type === "intake_start") {
      meta.textContent = "依頼の内容を確かめています…";
    } else if (event.type === "member_start") {
      record.hidden = false;
      meta.textContent = "所員が調べています…";
      if (!rows.has(event.source)) {
        const row = memberRow(event.agent, sourceLabels.get(event.source) ?? event.source);
        rows.set(event.source, row);
        members.append(row.node);
      }
      rowFor(event.source).set("busy", "調査中", "");
    } else if (event.type === "tool") {
      const query = event.args.query ?? JSON.stringify(event.args);
      rowFor(event.source).set("busy", "調査中", `「${query}」で検索`);
    } else if (event.type === "member_done") {
      const r = event.report;
      const row = rowFor(r.source);
      row.set(r.error ? "failed" : "done", r.error ? "失敗" : "報告済み", r.error ? "" : memberNote(r));
    } else if (event.type === "director_start") {
      directorName = event.agent;
      meta.textContent = consult ? "考えています…" : "報告をまとめています…";
      // 再調査を頼む前に書きかけた文章は捨てる
      answerText = "";
      answer.replaceChildren();
    } else if (event.type === "memo_start") {
      meta.textContent = "メモに書き足すことを考えています…";
    } else if (event.type === "delta") {
      answerText += event.text;
      drawAnswer();
    } else if (event.type === "followup") {
      meta.textContent = "再調査を頼みました";
      const row = memberRow(event.agent, sourceLabels.get(event.source) ?? event.source, event.instruction);
      rows.set(event.source, row);
      members.append(row.node);
      row.set("followup", "再調査を依頼", "");
    }
    scrollToBottom();
  };
  // つなぎ直したときに、始めから流れてくる進み具合で描き直す
  const reset = () => {
    meta.textContent = consult ? "考えています…" : "所員が調べています…";
    answerText = "";
    answer.replaceChildren();
    members.replaceChildren();
    rows.clear();
    record.hidden = true;
  };
  return { node, update, reset };
}

function membersView(rows) {
  const node = el("div", "members");
  node.append(...rows);
  return node;
}

// 調査中の所員の1行。set で状態を変える
function memberRow(agent, label, request, scope) {
  const node = el("div", "member");
  const pill = el("span", "pill");
  const note = el("span", "member-note");
  const head = el("div", "member-head");
  head.append(el("span", "member-name", agent), el("span", "member-source", label), pill, note);
  node.append(head);
  if (request) node.append(el("div", "member-request", `所長からの依頼: ${request}`));
  if (scope) node.append(el("div", "member-request", `調べた範囲: ${scope.join(", ")}`));
  const set = (state, label, text) => {
    pill.className = `pill ${state}`;
    pill.textContent = label;
    note.textContent = text;
  };
  return { node, set };
}

// 終わった調査の所員の1行。検索の過程と報告はたたんでおく
function memberView(report) {
  const row = memberRow(report.agent, report.label, report.request, report.scope);
  if (report.error) {
    row.set("failed", "失敗", "");
    row.node.append(el("div", "member-report error-text", report.error));
    return row.node;
  }
  row.set(report.request ? "followup" : "done", report.request ? "再調査" : "報告", memberNote(report));
  const details = el("details");
  details.append(el("summary", "", "検索の過程と報告"));
  const steps = el("div", "steps");
  for (const s of report.steps) steps.append(stepView(s));
  const body = markdownView(report.content || "（報告なし）");
  body.classList.add("member-report");
  details.append(steps, body);
  row.node.append(details);
  return row.node;
}

function memberNote(report) {
  return [`検索${report.steps.length}回`, report.seconds !== undefined && formatSeconds(report.seconds)].filter(Boolean).join(" · ");
}

function formatElapsed(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? `${s}秒` : `${Math.floor(s / 60)}分${String(s % 60).padStart(2, "0")}秒`;
}

function formatSeconds(seconds) {
  return seconds < 60 ? `${seconds.toFixed(1)}秒` : `${Math.floor(seconds / 60)}分${Math.round(seconds % 60)}秒`;
}

function stepView(step) {
  const node = el("div", "step");
  const call = el("div", "step-call");
  const args = Object.entries(step.args).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`);
  call.append(el("b", "", step.tool), ` ${args.join(" / ")}`);
  if (step.seconds !== undefined) call.append(` （${formatSeconds(step.seconds)}）`);
  node.append(call, el("pre", "", step.result || "（結果なし）"));
  return node;
}

function markdownView(text) {
  const node = el("div", "markdown");
  node.append(renderMarkdown(text));
  // コードブロック（コマンドや設定）は、そのままコピーできるようにする
  for (const pre of node.querySelectorAll("pre")) {
    const button = copyButton(pre.textContent);
    button.classList.add("code-copy");
    button.title = "このコードをコピー";
    pre.append(button);
  }
  return node;
}

// ---- 最初の画面 ----

// 最初の画面に出す質問の例
const EXAMPLES = [
  "監視サーバーのディスク増設ってどうなった？",
  "VPN証明書の更新手順ってどこかにまとまってる？",
  "先月あった障害対応の経緯を教えて",
];

function showEmpty() {
  openedConversation = null;
  turnCount = 0;
  setPlaceholder(null);
  renderHead();
  const node = el("div", "empty");
  node.id = "empty";
  const logo = el("img", "empty-logo");
  logo.src = "/static/ghost.svg";
  logo.alt = "";
  const examples = el("div", "examples");
  for (const text of EXAMPLES) {
    const example = el("button", "example", text);
    example.addEventListener("click", () => {
      input.value = text;
      autosize();
      input.focus();
    });
    examples.append(example);
  }
  const folder = activeFolder();
  const heading = folder ? `フォルダ「${folder.name}」で調べる` : "あの話、どうなったっけ？";
  const note = folder
    ? "このフォルダの調査は、フォルダで選んだ調べる場所を引き継ぎます。"
    : "所員が選んだ場所を手分けして調べ、所長が経緯を根拠のリンクつきでまとめます。";
  const destination = el("p", "destination-note", destinationNote);
  destination.id = "destination-note";
  const setup = el("section", "setup");
  setup.id = "setup";
  setup.hidden = true;
  node.append(logo, el("h2", "", heading), el("p", "", note), examples, setup, destination);
  log.replaceChildren(node);
  renderSetup();
}

// ---- メモ ----

const MEMO_LENGTH = 2000; // サーバーの上限（turns.MEMO_LENGTH）と同じ
const memoDialog = document.getElementById("memo-dialog");
const globalMemo = document.getElementById("global-memo");
const folderMemo = document.getElementById("folder-memo");

function countMemo(field, counter) {
  const n = field.value.trim().length;
  counter.textContent = `${n} / ${MEMO_LENGTH}文字`;
  counter.classList.toggle("over", n > MEMO_LENGTH);
}

async function openMemo() {
  const [settings] = await Promise.all([api("GET", "/api/settings"), refreshFolders()]);
  const folder = activeFolder();
  globalMemo.value = settings.memo;
  document.getElementById("folder-memo-field").hidden = !folder;
  if (folder) {
    document.getElementById("folder-memo-name").textContent = `「${folder.name}」`;
    folderMemo.value = folder.memo;
  }
  document.getElementById("memo-status").textContent = "";
  countMemo(globalMemo, document.getElementById("global-memo-count"));
  countMemo(folderMemo, document.getElementById("folder-memo-count"));
  memoDialog.showModal();
}

document.getElementById("open-memo").addEventListener("click", openMemo);


globalMemo.addEventListener("input", () => countMemo(globalMemo, document.getElementById("global-memo-count")));

folderMemo.addEventListener("input", () => countMemo(folderMemo, document.getElementById("folder-memo-count")));

document.getElementById("memo-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const folder = activeFolder();
  try {
    await api("PATCH", "/api/settings", { memo: globalMemo.value });
    if (folder) await api("PATCH", `/api/folders/${folder.id}`, { memo: folderMemo.value });
  } catch (err) {
    document.getElementById("memo-status").textContent = err.message;
    return;
  }
  await refreshFolders();
  memoDialog.close();
});

// ---- ソースと接続（接続確認とMCPサーバー） ----

const statusDialog = document.getElementById("status");

document.getElementById("open-status").addEventListener("click", async () => {
  const [status, mcp] = await Promise.all([api("GET", "/api/status"), api("GET", "/api/mcp")]);
  renderStatus(status);
  renderMcp(mcp);
  closeMcpForm();
  statusDialog.showModal();
});


document.getElementById("run-check").addEventListener("click", async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  button.textContent = "確認中…";
  try {
    renderStatus(await api("GET", "/api/status"), await api("POST", "/api/check"));
  } finally {
    button.disabled = false;
    button.textContent = "確認する";
  }
});

function renderStatus(status, results) {
  const STATES = { ok: ["done", "OK"], error: ["failed", "エラー"], unset: ["unset", "未設定"] };
  const row = (name, result) => {
    const tr = el("tr");
    const state = el("td");
    if (result.state) state.append(el("span", `pill ${STATES[result.state][0]}`, STATES[result.state][1]));
    tr.append(el("th", "", name), state, el("td", "status-message", result.message));
    return tr;
  };
  const rows = [row("LLM", results?.llm ?? { message: `${status.llm.model}（${status.llm.base_url}）` })];
  for (const s of status.sources) {
    // MCPサーバーがつながらないのは「未設定」ではなくエラー（下の一覧から直す）
    const unset = s.mcp ? { state: "error", message: s.requires } : { state: "unset", message: `${s.requires} を .env に書くと使えます` };
    rows.push(row(s.label, results?.sources[s.id] ?? (s.configured ? { message: "設定済み（まだ確認していません）" } : unset)));
  }
  document.getElementById("status-table").replaceChildren(...rows);
}

// results（確認の結果）がなければ、設定の有無だけ出す
const mcpForm = document.getElementById("mcp-form");
// 編集しているサーバーのキー（null なら足す）
let mcpEditing = null;

function renderMcp({ path, servers }) {
  document.getElementById("mcp-path").textContent = path;
  const list = document.getElementById("mcp-list");
  if (!servers.length) {
    list.replaceChildren(el("div", "conv-empty", "まだありません。「＋ 足す」から足せます。"));
    return;
  }
  list.replaceChildren(
    ...servers.map((s) => {
      const item = el("div", "mcp-item");
      const head = el("div", "mcp-head");
      head.append(
        el("b", "", s.label),
        el("span", s.connected ? "pill done" : "pill failed", s.connected ? "つながっています" : "つながりません"),
        el("span", "mcp-where", s.url ?? [s.command, ...(s.args ?? [])].join(" ")),
      );
      const edit = el("button", "quiet", "編集");
      edit.addEventListener("click", () => openMcpForm(s));
      const remove = el("button", "quiet danger", "外す");
      remove.addEventListener("click", async () => {
        if (!confirm(`MCPサーバー「${s.label}」を外しますか？`)) return;
        renderMcp(await api("DELETE", `/api/mcp/${s.key}`));
        await refreshSources();
      });
      head.append(edit, remove);
      const detail = s.connected
        ? el("div", "mcp-detail", `使えるツール: ${s.tools.join(", ") || "なし"}`)
        : el("div", "mcp-detail error-text", s.error);
      item.append(head, detail);
      return item;
    }),
  );
}

// 「名前: 値」「名前=値」の行を読み書きする
function parseLines(text, separator) {
  const result = {};
  for (const line of text.split("\n")) {
    const at = line.indexOf(separator);
    if (at > 0) result[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return result;
}

function formatLines(dict = {}, separator) {
  return Object.entries(dict)
    .map(([k, v]) => `${k}${separator}${v}`)
    .join("\n");
}

function setMcpKind(kind) {
  for (const radio of mcpForm.querySelectorAll('[name="mcp-kind"]')) radio.checked = radio.value === kind;
  document.getElementById("mcp-url-fields").hidden = kind !== "url";
  document.getElementById("mcp-command-fields").hidden = kind !== "command";
}

function openMcpForm(server = null) {
  mcpEditing = server?.key ?? null;
  document.getElementById("mcp-form-title").textContent = server ? `「${server.label}」を編集` : "MCPサーバーを足す";
  document.getElementById("mcp-label").value = server?.label ?? "";
  document.getElementById("mcp-url").value = server?.url ?? "";
  document.getElementById("mcp-type").value = server?.type ?? "";
  document.getElementById("mcp-headers").value = formatLines(server?.headers, ": ");
  document.getElementById("mcp-command").value = server?.command ?? "";
  document.getElementById("mcp-args").value = (server?.args ?? []).join("\n");
  document.getElementById("mcp-env").value = formatLines(server?.env, "=");
  document.getElementById("mcp-status").textContent = "";
  setMcpKind(server?.command ? "command" : "url");
  mcpForm.hidden = false;
  document.getElementById("mcp-label").focus();
}

function closeMcpForm() {
  mcpForm.hidden = true;
}

// ソースが増減したので、入力欄のチップと接続確認の表を描き直す
async function refreshSources() {
  await loadConfig();
  renderStatus(await api("GET", "/api/status"));
}

document.getElementById("add-mcp").addEventListener("click", () => openMcpForm());

document.getElementById("mcp-cancel").addEventListener("click", closeMcpForm);

for (const radio of mcpForm.querySelectorAll('[name="mcp-kind"]')) radio.addEventListener("change", () => setMcpKind(radio.value));

mcpForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const kind = mcpForm.querySelector('[name="mcp-kind"]:checked').value;
  const conf = { label: document.getElementById("mcp-label").value };
  if (kind === "url") {
    conf.url = document.getElementById("mcp-url").value;
    conf.type = document.getElementById("mcp-type").value || undefined;
    conf.headers = parseLines(document.getElementById("mcp-headers").value, ":");
  } else {
    conf.command = document.getElementById("mcp-command").value;
    conf.args = document.getElementById("mcp-args").value.split("\n").map((a) => a.trim()).filter(Boolean);
    conf.env = parseLines(document.getElementById("mcp-env").value, "=");
  }
  const save = document.getElementById("mcp-save");
  const status = document.getElementById("mcp-status");
  save.disabled = true;
  status.textContent = "つないでいます…";
  try {
    const result = mcpEditing ? await api("PUT", `/api/mcp/${mcpEditing}`, conf) : await api("POST", "/api/mcp", conf);
    renderMcp(result);
    closeMcpForm();
    await refreshSources();
  } catch (err) {
    status.textContent = err.message;
  } finally {
    save.disabled = false;
    if (status.textContent === "つないでいます…") status.textContent = "";
  }
});

// ---- キャラクター ----

const characterDialog = document.getElementById("characters");

document.getElementById("open-characters").addEventListener("click", async () => {
  renderCharacters(await api("GET", "/api/characters"));
  characterDialog.showModal();
});


// 名前が変わったらソースの表示（所員の名前）も変わる
characterDialog.addEventListener("close", loadConfig);

function renderCharacters(characters) {
  document.getElementById("character-list").replaceChildren(...characters.map(characterForm));
}

function characterForm(c) {
  const form = el("form", "character");
  const name = el("input");
  name.id = `character-name-${c.key}`;
  name.value = c.name;
  name.maxLength = 20;
  name.required = true;
  const persona = el("textarea");
  persona.id = `character-persona-${c.key}`;
  persona.value = c.persona;
  persona.maxLength = 300;
  persona.rows = 2;
  const status = el("span", "character-status");
  const reset = el("button", "quiet", "既定に戻す");
  const save = el("button", "primary", "保存");
  save.type = "submit";

  const run = async (request) => {
    try {
      renderCharacters(await request());
    } catch (err) {
      status.textContent = err.message;
    }
  };
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    run(() => api("PUT", `/api/characters/${c.key}`, { name: name.value, persona: persona.value }));
  });
  reset.addEventListener("click", () => run(() => api("DELETE", `/api/characters/${c.key}`)));

  const actions = el("div", "dialog-actions");
  actions.append(status, reset, save);
  form.append(el("div", "character-role", c.role), labeled("名前", name), labeled("性格", persona), actions);
  return form;
}

function labeled(text, control) {
  const label = el("label", "field");
  label.append(el("span", "", text), control);
  return label;
}

// ---- LLMとのやりとり ----

const traceDialog = document.getElementById("trace");


const ROLE_LABELS = { system: "指示（システム）", human: "依頼", ai: "LLM", tool: "ツールの結果" };

async function openTrace(traceId) {
  const calls = await api("GET", `/api/traces/${traceId}`);
  const total = calls.reduce((n, c) => n + c.seconds, 0);
  const tokens = calls.reduce((n, c) => n + (c.input_tokens ?? 0) + (c.output_tokens ?? 0), 0);
  document.getElementById("trace-summary").textContent =
    `LLMを${calls.length}回呼び、応答待ちは合計${formatSeconds(total)}でした（所員は並列に動くので、実際にかかった時間より長くなります）。` +
    (tokens ? ` トークンは合計${tokens.toLocaleString()}です。` : "");
  document.getElementById("trace-list").replaceChildren(...calls.map(callView));
  traceDialog.showModal();
}

function callView(call, index) {
  const node = el("details", "call");
  const summary = el("summary");
  const tokens = call.input_tokens != null ? `入力${call.input_tokens} / 出力${call.output_tokens}トークン` : "";
  const result = call.response.tool_calls ? `ツールを呼んだ: ${call.response.tool_calls.map((c) => c.name).join(", ")}` : "文章で答えた";
  summary.append(
    el("span", "call-index", `${index + 1}`),
    el("b", "", call.who),
    el("span", "call-meta", [formatSeconds(call.seconds), tokens, result].filter(Boolean).join(" · ")),
  );
  node.append(summary);
  if (call.tools.length) node.append(el("div", "call-tools", `渡したツール: ${call.tools.join(", ")}`));
  for (const m of call.messages) node.append(messageView(m));
  const response = messageView(call.response);
  response.classList.add("response");
  node.append(response);
  return node;
}

function messageView(m) {
  const node = el("div", `call-message ${m.role}`);
  node.append(el("div", "call-role", ROLE_LABELS[m.role] ?? m.role));
  let text = m.content;
  if (m.tool_calls) text += (text ? "\n" : "") + m.tool_calls.map((c) => `→ ${c.name}(${JSON.stringify(c.args)})`).join("\n");
  node.append(el("pre", "", text || "（空）"));
  return node;
}

// ---- 使い方 ----

const helpDialog = document.getElementById("help");

document.getElementById("open-help").addEventListener("click", () => helpDialog.showModal());


// ---- キーボード ----

document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  // 文字を打っている最中でなければ、? で使い方を開く
  const typing = e.target.closest("input, textarea, select, [contenteditable]");
  if (e.key === "?" && !typing && !mod && !document.querySelector("dialog[open]")) {
    e.preventDefault();
    helpDialog.showModal();
    return;
  }
  if (mod && !e.shiftKey && e.key.toLowerCase() === "k") {
    e.preventDefault();
    search.focus();
    search.select();
  } else if (mod && e.shiftKey && e.key.toLowerCase() === "o") {
    e.preventDefault();
    startNew(viewFolderId);
  }
});

// ---- 通信と小物 ----

// ボタンの文字だけを変える（アイコンは残す）
function setLabel(button, text) {
  button.replaceChildren(...button.querySelectorAll("svg"), text);
}

// アイコンつきのボタン
function iconButton(name, text, className = "") {
  const button = el("button", className);
  button.append(icon(name), text);
  return button;
}

function copyButton(text) {
  const button = iconButton("copy", "コピー");
  button.title = "回答をMarkdownのままコピー";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      setLabel(button, "コピーしました");
    } catch {
      setLabel(button, "コピーできませんでした");
    }
    setTimeout(() => setLabel(button, "コピー"), 1500);
  });
  return button;
}

// NDJSON（1行1イベント）のレスポンスを読みながら onEvent を呼ぶ。切れたら返る
async function readLines(path, onEvent) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(res.statusText);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) if (line) onEvent(JSON.parse(line));
  }
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// 要素を作る。ボタンは既定で type="button"（フォームの中でも送信しない。送信させるボタンだけ type を変える）
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (tag === "button") node.type = "button";
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// 自分で足したもの（質問など）は、読んでいた位置にかかわらず見せる
function append(node) {
  log.append(node);
  followBottom = true;
  scrollToBottom();
  return node;
}

// 一番下を見ているときだけ、届いた分に合わせて下へ送る（上の方を読んでいるときは動かさない）
function scrollToBottom() {
  if (followBottom) log.scrollTop = log.scrollHeight;
}

let followBottom = true;

log.addEventListener("scroll", () => {
  followBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
});

function autosize() {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
}

// ダイアログの × はどれも、そのダイアログを閉じる
for (const button of document.querySelectorAll("dialog [data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}

// ---- 起動 ----

// すべての宣言がすんでから動かすため、ファイルの最後に置く
// ソースの一覧と、フォルダの外での選択がそろってから入力欄を描く
Promise.all([loadConfig(), refreshFolders()]).then(() => applySelection());

refreshList();

showEmpty();

connectEvents();
