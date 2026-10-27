// 表示中の会話ID。null は「新しい会話」（最初の送信でサーバー側に作られる）
let currentId = null;
// 会話の一覧（今の検索とアーカイブの表示に合うもの）
let conversations = [];
// アーカイブした会話を見ているか
let showingArchive = false;
// 開いている会話。検索で一覧から外れても、名前やタグを出せるように持っておく
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

const log = document.getElementById("log");
const list = document.getElementById("conversations");
const search = document.getElementById("search");
const titleEl = document.getElementById("conversation-title");
const archiveButton = document.getElementById("archive");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const sourceBox = document.getElementById("sources");
const characterDialog = document.getElementById("characters");
const statusDialog = document.getElementById("status");
const traceDialog = document.getElementById("trace");

// 最初の画面に出す質問の例
const EXAMPLES = [
  "監視サーバーのディスク増設ってどうなった？",
  "VPN証明書の更新手順ってどこかにまとまってる？",
  "先月あった障害対応の経緯を教えて",
];

document.getElementById("new-chat").addEventListener("click", () => startNew(viewFolderId));

// 打つたびにサーバーで探す（中身まで探すので、打ち終わるのを少し待つ）
let searchTimer = null;
search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(refreshList, 250);
});
document.getElementById("toggle-archive").addEventListener("click", (e) => {
  showingArchive = !showingArchive;
  e.currentTarget.textContent = showingArchive ? "通常の一覧に戻る" : "アーカイブを見る";
  document.querySelector(".sidebar").classList.toggle("archive-mode", showingArchive);
  refreshList();
});
archiveButton.addEventListener("click", async () => {
  const c = currentConversation();
  await api("PATCH", `/api/conversations/${c.id}`, { archived: !c.archived });
  c.archived = !c.archived;
  renderHead();
  await refreshList();
});
titleEl.addEventListener("click", renameConversation);
sourceBox.addEventListener("change", () => {
  selection.unchecked = [...sourceBox.querySelectorAll("input:not(:checked)")].map((box) => box.value);
  saveSelection();
});

document.getElementById("open-characters").addEventListener("click", async () => {
  renderCharacters(await api("GET", "/api/characters"));
  characterDialog.showModal();
});
document.getElementById("close-characters").addEventListener("click", () => characterDialog.close());
// 名前が変わったらソースの表示（所員の名前）も変わる
characterDialog.addEventListener("close", loadConfig);

document.getElementById("open-status").addEventListener("click", async () => {
  const [status, mcp] = await Promise.all([api("GET", "/api/status"), api("GET", "/api/mcp")]);
  renderStatus(status);
  renderMcp(mcp);
  closeMcpForm();
  statusDialog.showModal();
});
document.getElementById("close-status").addEventListener("click", () => statusDialog.close());
document.getElementById("close-trace").addEventListener("click", () => traceDialog.close());
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

input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  // 日本語変換の確定Enterでは送信しない
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    // 調査中のEnterで中止にならないよう、送れるときだけ送る
    if (!running) form.requestSubmit();
  }
});

// 調査中のターン。調査中は送信ボタンが中止ボタンになる
let running = null; // { turnId, cancelling }

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (running) return cancelTurn();
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
async function ask(content, fromTurn, skipClarify = false, images = [], shownImages = images) {
  if (document.getElementById("empty")) {
    log.replaceChildren();
    renderHead({ title: content });
  }
  if (fromTurn !== null) {
    // やり直す質問と、それより後のやりとりを画面から外す（失敗したら開き直して元に戻す）
    for (const node of [...log.querySelectorAll(".turn")].filter((n) => Number(n.dataset.index) >= fromTurn)) node.remove();
  }
  const index = fromTurn ?? turnCount;
  const turn = append(turnView(content, index, { images: shownImages }));

  const progress = progressView();
  turn.append(progress.node);
  running = { turnId: null, cancelling: false };
  setSendButton();
  try {
    const path = currentId ? `/api/conversations/${currentId}/messages` : "/api/conversations";
    const body = { content, sources: selectedSources(), scopes: selection.scopes };
    if (!currentId && viewFolderId) body.folder_id = viewFolderId;
    if (fromTurn !== null) body.from_turn = fromTurn;
    if (skipClarify) body.skip_clarify = true;
    if (images.length) body.images = images;
    let result = null;
    await streamTurn(path, body, (event) => {
      if (event.type === "error") throw new Error(event.message);
      if (event.type === "start") running.turnId = event.turn_id;
      else if (event.type === "done" || event.type === "cancelled") result = event;
      else progress.update(event);
    });
    if (!result) throw new Error("応答が途中で切れました");
    if (result.type === "cancelled") {
      // 何も保存されていないので、質問ごと消して入力欄に戻す
      turn.remove();
      restoreQuestion(content, images);
      if (fromTurn !== null) await openConversation(currentId);
      else if (!log.children.length) showEmpty();
      return;
    }
    progress.node.replaceWith(answerView(result.turn, index));
    if (result.question.image_text) turn.querySelector(".question-block").append(imageTextView(result.question.image_text));
    turnCount = index + 2;
    setPlaceholder(result.turn);
    if (!currentId) {
      currentId = result.conversation_id;
      await Promise.all([refreshList(), refreshFolders()]);
      renderHead();
    }
  } catch (err) {
    progress.node.replaceWith(el("p", "error-text", `調査できませんでした: ${err.message}`));
    restoreQuestion(content, images);
  } finally {
    running = null;
    setSendButton();
    input.focus();
  }
}

async function cancelTurn() {
  if (!running.turnId || running.cancelling) return;
  running.cancelling = true;
  setSendButton();
  await api("POST", `/api/turns/${running.turnId}/cancel`);
}

function setSendButton() {
  send.textContent = !running ? "調査を依頼" : running.cancelling ? "中止しています…" : "中止";
  send.classList.toggle("stop", Boolean(running));
  send.disabled = Boolean(running?.cancelling);
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

// ---- 質問に添える画像 ----

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
      remove.type = "button";
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
  addImages(attachFile.files);
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
  addImages(e.dataTransfer.files);
});

// ---- 設定とソース ----

// 使えるソースの一覧（入力欄を描き直すときに使う）
let sourceList = [];

async function loadConfig() {
  const c = await api("GET", "/api/config");
  document.getElementById("model").textContent = c.model;
  document.getElementById("model").title = c.vision ? "設定中のモデル（画像を読める）" : "設定中のモデル";
  visionEnabled = c.vision;
  attachButton.hidden = !c.vision;
  directorName = c.director;
  sourceList = c.sources;
  sourceLabels = new Map(c.sources.map((s) => [s.id, s.label]));
  renderSources(c.sources);
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
}

// 範囲を選べるソースのチップの横に出すボタン。今の選択を短く出す
function scopeButton(source) {
  const button = el("button", "scope-button");
  button.type = "button";
  const names = selection.scopes[source.id] ?? [];
  button.textContent = names.length ? (names.length === 1 ? names[0] : `${names[0]} ほか${names.length - 1}件`) : `すべての${source.scope_label}`;
  button.title = `${source.label}で調べる${source.scope_label}を選ぶ`;
  button.classList.toggle("narrowed", names.length > 0);
  button.addEventListener("click", () => openScope(source, button));
  return button;
}

// ---- 検索範囲の選択 ----

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
document.getElementById("close-scope").addEventListener("click", () => scopeDialog.close());
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

function selectedSources() {
  return [...sourceBox.querySelectorAll("input:checked")].map((box) => box.value);
}

// ---- 接続確認 ----

// results（確認の結果）がなければ、設定の有無だけ出す
// ---- MCPサーバー ----

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
      edit.type = "button";
      edit.addEventListener("click", () => openMcpForm(s));
      const remove = el("button", "quiet danger", "外す");
      remove.type = "button";
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

// ---- キャラクター ----

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
  reset.type = "button";
  const save = el("button", "primary", "保存");

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

// ---- 会話の一覧 ----

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

async function openConversation(id) {
  const data = await api("GET", `/api/conversations/${id}`);
  const folderBefore = activeFolderId();
  currentId = id;
  openedConversation = conversations.find((c) => c.id === id) ?? null;
  if (activeFolderId() !== folderBefore) applySelection();
  renderHead();
  const nodes = [];
  data.turns.forEach((t, i) => {
    if (t.role === "user") {
      const images = (t.images ?? []).map((id) => `/api/images/${id}`);
      nodes.push(turnView(t.content, i, { images, imageText: t.image_text }));
    }
    else nodes.at(-1).append(answerView(t, i - 1));
  });
  turnCount = data.turns.length;
  log.replaceChildren(...nodes);
  log.scrollTop = log.scrollHeight;
  setPlaceholder(data.turns.at(-1));
  highlightCurrent();
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
  title.type = "button";
  title.title = c.title;
  title.append(el("span", "conv-name", c.title));
  title.addEventListener("click", () => openConversation(c.id));
  const del = el("button", "conv-delete", "×");
  del.type = "button";
  del.title = "削除";
  del.addEventListener("click", () => deleteConversation(c.id));
  item.append(title, del);
  return item;
}

// ---- フォルダ ----

const folderList = document.getElementById("folder-list");
const folderDialog = document.getElementById("folder-dialog");
const crumb = document.getElementById("folder-crumb");
const moveSelect = document.getElementById("move-folder");
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
    open.type = "button";
    open.append(el("span", "folder-name", name));
    if (count !== undefined) open.append(el("span", "folder-count", String(count)));
    open.addEventListener("click", () => startNew(id));
    row.append(open);
    if (id) {
      const settings = el("button", "folder-settings", "設定");
      settings.type = "button";
      settings.title = `「${name}」の名前・メモ・削除`;
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

document.getElementById("close-folder").addEventListener("click", () => folderDialog.close());
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

// 開いている調査を別のフォルダへ移す
moveSelect.addEventListener("change", async () => {
  const c = currentConversation();
  const folderId = moveSelect.value || null;
  await api("PATCH", `/api/conversations/${c.id}`, { folder_id: folderId });
  c.folder_id = folderId;
  await Promise.all([refreshFolders(), refreshList()]);
  applySelection();
  renderHead();
});
crumb.addEventListener("click", () => startNew(activeFolderId()));

// ---- メモ（全体と、今のフォルダ） ----

const MEMO_LENGTH = 2000; // サーバーの上限（web.MEMO_LENGTH）と同じ
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
document.getElementById("close-memo").addEventListener("click", () => memoDialog.close());
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

// ---- 開いている会話の見出し（フォルダ・名前・アーカイブ） ----

// 新しい会話を送った直後は、まだ一覧にないので draft（仮の名前）を出す
function renderHead(draft) {
  const c = draft ? null : currentConversation();
  titleEl.textContent = c?.title ?? draft?.title ?? "新しい調査";
  titleEl.classList.toggle("editable", Boolean(c));
  titleEl.title = c ? "クリックして名前を変える" : "";
  archiveButton.hidden = !c;
  archiveButton.textContent = c?.archived ? "アーカイブから戻す" : "アーカイブ";
  const folder = activeFolder();
  crumb.hidden = !folder;
  crumb.textContent = folder ? `${folder.name} ›` : "";
  crumb.title = folder ? `フォルダ「${folder.name}」で新しい調査を始める` : "";
  document.getElementById("move").hidden = !c;
  if (c) {
    const none = Object.assign(el("option", "", "なし"), { value: "" });
    moveSelect.replaceChildren(none, ...folders.map((f) => Object.assign(el("option", "", f.name), { value: f.id })));
    moveSelect.value = c.folder_id ?? "";
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

function highlightCurrent() {
  for (const item of list.querySelectorAll(".conv")) item.classList.toggle("active", item.dataset.id === currentId);
}

// ---- 調査と回答 ----

// 1往復分の入れ物。質問を出し、回答はあとから足す
// index は質問のターンの位置。やり直すときにサーバーへ渡す
// images は質問に添えた画像のURL、imageText はそれを所長が文字に起こしたもの
function turnView(question, index, { images = [], imageText = "" } = {}) {
  const node = el("section", "turn");
  node.dataset.index = index;
  node.images = images;
  const block = el("div", "question-block");
  const bubble = el("div", "question", question);
  const edit = el("button", "question-edit", "編集");
  edit.type = "button";
  edit.title = "質問を直して調べ直す";
  edit.addEventListener("click", () => editQuestion(node, question, index));
  const row = el("div", "question-row");
  row.append(edit, bubble);
  block.append(row);
  if (images.length) {
    const gallery = el("div", "question-images");
    for (const src of images) {
      const link = Object.assign(el("a"), { href: src, target: "_blank", rel: "noopener" });
      link.append(Object.assign(el("img"), { src, alt: "添えた画像" }));
      gallery.append(link);
    }
    block.append(gallery);
  }
  if (imageText) block.append(imageTextView(imageText));
  node.append(block);
  return node;
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
  if (running || !confirmRedo(index)) return;
  ask(content, index, skipClarify, [], turnImages(index));
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
  if (running) return;
  const row = node.querySelector(".question-row");
  const editor = el("div", "question-editor");
  const field = el("textarea");
  field.value = question;
  field.rows = Math.min(8, question.split("\n").length + 1);
  const cancel = el("button", "quiet", "やめる");
  cancel.type = "button";
  const submit = el("button", "primary", "調べ直す");
  submit.type = "button";
  const actions = el("div", "dialog-actions");
  actions.append(cancel, submit);
  editor.append(field, actions);
  row.replaceWith(editor);
  field.focus();
  cancel.addEventListener("click", () => editor.replaceWith(row));
  submit.addEventListener("click", () => {
    const content = field.value.trim();
    if (!content) return;
    if (running || !confirmRedo(index)) return;
    ask(content, index, false, [], node.images);
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
  const skip = el("button", "", "聞き返さずに調べる");
  skip.type = "button";
  skip.title = "今の質問のまま、所員に調べさせます";
  skip.addEventListener("click", (e) => redo(e.currentTarget.closest(".turn").querySelector(".question").textContent, index, true));
  const actions = el("div", "answer-actions");
  actions.append(skip);
  body.append(markdownView(content), note, actions);
  return node;
}

function answerView(turn, index) {
  if (turn.kind === "clarify") return clarifyView(turn, index);
  const { agent, content, model, reports, links, seconds, llm_calls, trace_id, memo_added } = turn;
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
  const actions = el("div", "answer-actions");
  actions.append(copyButton(content));
  const regenerate = el("button", "", "作り直す");
  regenerate.type = "button";
  regenerate.title = "同じ質問でもう一度調べる";
  regenerate.addEventListener("click", (e) => redo(e.currentTarget.closest(".turn").querySelector(".question").textContent, index));
  actions.append(regenerate);
  if (trace_id) {
    const button = el("button", "", `LLMとのやりとり（${llm_calls}回）`);
    button.type = "button";
    button.addEventListener("click", () => openTrace(trace_id));
    actions.append(button);
  }
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
  open.type = "button";
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
  for (const link of links) {
    const item = el("li", link.found ? "" : "unverified");
    const a = el("a", "", link.title || link.url);
    a.href = link.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    const meta = link.found ? [link.source, link.date].filter(Boolean).join(" · ") : "検索結果にありません（LLMが作ったURLかもしれません）";
    item.append(a, el("span", "citation-meta", meta));
    if (link.title) item.append(el("span", "citation-url", link.url));
    list.append(item);
  }
  box.append(summary, list);
  return box;
}

// 調査中の表示。所員ごとの状態と、届いた分の回答を出す
function progressView() {
  const { node, body, meta } = answerFrame(directorName);
  meta.textContent = "所員が調べています…";
  const answer = el("div", "markdown streaming");
  const record = el("details", "record");
  record.open = true;
  record.hidden = true; // 所員が動き出してから出す
  const members = membersView([]);
  record.append(el("summary", "", "調査の様子"), members);
  body.append(answer, record);

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
      meta.textContent = "報告をまとめています…";
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
  return { node, update };
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

// ---- LLMとのやりとり ----

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

function copyButton(text) {
  const button = el("button", "", "コピー");
  button.type = "button";
  button.title = "回答をMarkdownのままコピー";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "コピーしました";
    } catch {
      button.textContent = "コピーできませんでした";
    }
    setTimeout(() => (button.textContent = "コピー"), 1500);
  });
  return button;
}

function markdownView(text) {
  const node = el("div", "markdown");
  node.append(renderMarkdown(text));
  return node;
}

// ---- 通信と小物 ----

// NDJSON（1行1イベント）のレスポンスを読みながら onEvent を呼ぶ。onEvent が投げた例外はそのまま伝わる
async function streamTurn(path, body, onEvent) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json();
    throw new Error(data.error || res.statusText);
  }
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

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function append(node) {
  log.append(node);
  scrollToBottom();
  return node;
}

function scrollToBottom() {
  log.scrollTop = log.scrollHeight;
}

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
    example.type = "button";
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
  node.append(logo, el("h2", "", heading), el("p", "", note), examples);
  log.replaceChildren(node);
}

function autosize() {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
}

// ---- 起動 ----
// すべての宣言がすんでから動かすため、ファイルの最後に置く
// ソースの一覧と、フォルダの外での選択がそろってから入力欄を描く
Promise.all([loadConfig(), refreshFolders()]).then(() => applySelection());
refreshList();
showEmpty();
