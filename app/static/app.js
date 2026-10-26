// 表示中の会話ID。null は「新しい会話」（最初の送信でサーバー側に作られる）
let currentId = null;

const log = document.getElementById("log");
const list = document.getElementById("conversations");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const sourceBox = document.getElementById("sources");

const characterDialog = document.getElementById("characters");

loadConfig();
refreshList();
showEmpty();

document.getElementById("new-chat").addEventListener("click", () => {
  currentId = null;
  showEmpty();
  highlightCurrent();
  input.focus();
});

document.getElementById("open-characters").addEventListener("click", async () => {
  renderCharacters(await api("GET", "/api/characters"));
  characterDialog.showModal();
});
document.getElementById("close-characters").addEventListener("click", () => characterDialog.close());
// 名前が変わったらソースの表示（「GitHub（名前）」）も変わる
characterDialog.addEventListener("close", loadConfig);

input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  // 日本語変換の確定Enterでは送信しない
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    form.requestSubmit();
  }
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const content = input.value.trim();
  if (!content || send.disabled) return;

  document.getElementById("empty")?.remove();
  append(el("div", "msg user", content));
  input.value = "";
  autosize();

  const progress = progressView();
  const pending = append(progress.node);
  send.disabled = true;
  try {
    const path = currentId ? `/api/conversations/${currentId}/messages` : "/api/conversations";
    let result = null;
    await streamTurn(path, { content, sources: selectedSources() }, (event) => {
      if (event.type === "error") throw new Error(event.message);
      if (event.type === "done") result = event;
      else progress.update(event);
    });
    if (!result) throw new Error("応答が途中で切れました");
    pending.replaceWith(assistantMessage(result.turn));
    if (!currentId) {
      currentId = result.conversation_id;
      await refreshList();
    }
  } catch (err) {
    pending.replaceWith(el("div", "msg error", `エラー: ${err.message}`));
  } finally {
    send.disabled = false;
    input.focus();
  }
});

async function loadConfig() {
  const c = await api("GET", "/api/config");
  document.getElementById("model").textContent = c.model;
  renderSources(c.sources);
}

// 選択状態は描き直しても保つ（初回はすべて選択）
function renderSources(sources) {
  const checked = sourceBox.querySelector("input") ? new Set(selectedSources()) : null;
  for (const label of sourceBox.querySelectorAll("label")) label.remove();
  for (const s of sources) {
    const label = el("label", "source");
    const box = el("input");
    box.type = "checkbox";
    box.value = s.id;
    box.checked = checked ? checked.has(s.id) : true;
    label.append(box, `${s.label}（${s.agent}）`);
    sourceBox.append(label);
  }
}

function selectedSources() {
  return [...sourceBox.querySelectorAll("input:checked")].map((box) => box.value);
}

function renderCharacters(list) {
  document.getElementById("character-list").replaceChildren(...list.map(characterForm));
}

function characterForm(c) {
  const form = el("form", "character");
  const name = el("input");
  name.value = c.name;
  name.maxLength = 20;
  name.required = true;
  const persona = el("textarea");
  persona.value = c.persona;
  persona.maxLength = 300;
  persona.rows = 3;
  const status = el("span", "character-status");
  const reset = el("button", "", "既定に戻す");
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

  const actions = el("div", "character-actions");
  actions.append(status, reset, save);
  form.append(el("div", "character-role", c.role), labeled("名前", name), labeled("性格", persona), actions);
  return form;
}

function labeled(text, control) {
  const label = el("label", "field");
  label.append(el("span", "", text), control);
  return label;
}

async function openConversation(id) {
  const data = await api("GET", `/api/conversations/${id}`);
  currentId = id;
  log.replaceChildren(
    ...data.turns.map((t) => (t.role === "user" ? el("div", "msg user", t.content) : assistantMessage(t))),
  );
  log.scrollTop = log.scrollHeight;
  highlightCurrent();
}

async function deleteConversation(id) {
  if (!confirm("この会話を削除しますか？")) return;
  await api("DELETE", `/api/conversations/${id}`);
  if (id === currentId) {
    currentId = null;
    showEmpty();
  }
  await refreshList();
}

async function refreshList() {
  const conversations = await api("GET", "/api/conversations");
  list.replaceChildren(
    ...conversations.map((c) => {
      const item = el("div", "conv");
      item.dataset.id = c.id;
      const title = el("button", "conv-title", c.title);
      title.type = "button";
      title.title = c.title;
      title.addEventListener("click", () => openConversation(c.id));
      const del = el("button", "conv-delete", "×");
      del.type = "button";
      del.title = "削除";
      del.addEventListener("click", () => deleteConversation(c.id));
      item.append(title, del);
      return item;
    }),
  );
  highlightCurrent();
}

function highlightCurrent() {
  for (const item of list.children) item.classList.toggle("active", item.dataset.id === currentId);
}

// 調査中の表示。所員ごとに1行、最後に所長の1行
function progressView() {
  const node = el("div", "msg progress");
  const rows = new Map();
  const agents = new Map();
  const row = (key, text) => {
    if (!rows.has(key)) rows.set(key, node.appendChild(el("div")));
    rows.get(key).textContent = text;
    log.scrollTop = log.scrollHeight;
  };
  const update = (event) => {
    if (event.type === "member_start") {
      agents.set(event.source, event.agent);
      row(event.source, `👻 ${event.agent}: 調査中…`);
    } else if (event.type === "tool") {
      const query = event.args.query ?? JSON.stringify(event.args);
      row(event.source, `👻 ${agents.get(event.source)}: 「${query}」で検索中…`);
    } else if (event.type === "member_done") {
      const r = event.report;
      row(r.source, `👻 ${r.agent}: ${r.error ? "調査失敗" : "報告済み"}`);
    } else if (event.type === "director_start") {
      row("director", `🕵️ ${event.agent}所長: 報告をまとめています…`);
    }
  };
  return { node, update };
}

function assistantMessage({ agent, content, model, reports }) {
  const box = el("div", "msg assistant");
  box.append(el("div", "meta", `${agent}所長 · ${model}`));
  for (const r of reports) box.append(reportView(r));
  box.append(markdownView(content));
  return box;
}

function reportView(report) {
  const details = el("details", "report");
  const status = report.error ? "調査失敗" : `検索 ${report.steps.length}回`;
  details.append(el("summary", "", `👻 ${report.agent}（${report.label}）の報告 · ${status}`));
  for (const s of report.steps) {
    details.append(el("pre", "", `${s.tool}(${JSON.stringify(s.args)})\n→ ${s.result || "(結果なし)"}`));
  }
  details.append(report.error ? el("div", "report-body", `エラー: ${report.error}`) : markdownView(report.content || "（報告なし）"));
  return details;
}

function markdownView(text) {
  const node = el("div", "markdown");
  node.append(renderMarkdown(text));
  return node;
}

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
  log.scrollTop = log.scrollHeight;
  return node;
}

function showEmpty() {
  const node = el("div", "empty", "「あの話ってどうなったっけ？」を聞いてください");
  node.id = "empty";
  log.replaceChildren(node);
}

function autosize() {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
}
