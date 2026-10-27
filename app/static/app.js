// 表示中の会話ID。null は「新しい会話」（最初の送信でサーバー側に作られる）
let currentId = null;

const log = document.getElementById("log");
const list = document.getElementById("conversations");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const sourceBox = document.getElementById("sources");

const characterDialog = document.getElementById("characters");

// 外したソースはブラウザに覚えておく（新しく使えるようになったソースは選んだ状態で出す）
const UNCHECKED_KEY = "unchecked-sources";

// 最初の画面に出す質問の例
const EXAMPLES = [
  "監視サーバーのディスク増設ってどうなった？",
  "VPN証明書の更新手順ってどこかにまとまってる？",
  "先月あった障害対応の経緯を教えて",
];

loadConfig();
refreshList();
showEmpty();

document.getElementById("new-chat").addEventListener("click", () => {
  currentId = null;
  showEmpty();
  highlightCurrent();
  closeSidebar();
  input.focus();
});

// 狭い画面では会話の一覧を重ねて開く
const sidebar = document.getElementById("sidebar");
document.getElementById("toggle-sidebar").addEventListener("click", (e) => {
  e.stopPropagation();
  sidebar.classList.toggle("open");
});
document.addEventListener("click", (e) => {
  if (!sidebar.contains(e.target)) closeSidebar();
});
function closeSidebar() {
  sidebar.classList.remove("open");
}

sourceBox.addEventListener("change", saveSourceSelection);

document.getElementById("open-characters").addEventListener("click", async () => {
  renderCharacters(await api("GET", "/api/characters"));
  characterDialog.showModal();
});
document.getElementById("close-characters").addEventListener("click", () => characterDialog.close());
// 名前が変わったらソースの表示（「GitHub（名前）」）も変わる
characterDialog.addEventListener("close", loadConfig);

const statusDialog = document.getElementById("status");
document.getElementById("open-status").addEventListener("click", async () => {
  renderStatus(await api("GET", "/api/status"));
  statusDialog.showModal();
});
document.getElementById("close-status").addEventListener("click", () => statusDialog.close());
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

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (running) return cancelTurn();
  const content = input.value.trim();
  if (!content) return;

  document.getElementById("empty")?.remove();
  const question = append(el("div", "msg user", content));
  input.value = "";
  autosize();

  const progress = progressView();
  const pending = append(progress.node);
  running = { turnId: null, cancelling: false };
  setSendButton();
  try {
    const path = currentId ? `/api/conversations/${currentId}/messages` : "/api/conversations";
    let result = null;
    await streamTurn(path, { content, sources: selectedSources() }, (event) => {
      if (event.type === "error") throw new Error(event.message);
      if (event.type === "start") running.turnId = event.turn_id;
      else if (event.type === "done" || event.type === "cancelled") result = event;
      else progress.update(event);
    });
    if (!result) throw new Error("応答が途中で切れました");
    if (result.type === "cancelled") {
      // 何も保存されていないので、質問ごと消して入力欄に戻す
      question.remove();
      pending.remove();
      restoreQuestion(content);
      if (!log.children.length) showEmpty();
      return;
    }
    pending.replaceWith(assistantMessage(result.turn));
    if (!currentId) {
      currentId = result.conversation_id;
      await refreshList();
    }
  } catch (err) {
    pending.replaceWith(el("div", "msg error", `エラー: ${err.message}`));
    restoreQuestion(content);
  } finally {
    running = null;
    setSendButton();
    input.focus();
  }
});

async function cancelTurn() {
  if (!running.turnId || running.cancelling) return;
  running.cancelling = true;
  setSendButton();
  await api("POST", `/api/turns/${running.turnId}/cancel`);
}

function setSendButton() {
  send.textContent = !running ? "送信" : running.cancelling ? "中止中…" : "中止";
  send.classList.toggle("stop", Boolean(running));
  send.disabled = Boolean(running?.cancelling);
}

// 聞き直せるよう、質問を入力欄に戻す（入力中の文章があれば上書きしない）
function restoreQuestion(content) {
  if (input.value.trim()) return;
  input.value = content;
  autosize();
}

async function loadConfig() {
  const c = await api("GET", "/api/config");
  document.getElementById("model").textContent = c.model;
  renderSources(c.sources);
}

function renderSources(sources) {
  const unchecked = loadUnchecked();
  for (const label of sourceBox.querySelectorAll("label")) label.remove();
  for (const s of sources) {
    const label = el("label", "source");
    const box = el("input");
    box.type = "checkbox";
    box.value = s.id;
    box.checked = !unchecked.has(s.id);
    label.append(box, `${s.label}（${s.agent}）`);
    sourceBox.append(label);
  }
}

function loadUnchecked() {
  try {
    return new Set(JSON.parse(localStorage.getItem(UNCHECKED_KEY)) || []);
  } catch {
    return new Set();
  }
}

function saveSourceSelection() {
  const unchecked = [...sourceBox.querySelectorAll("input:not(:checked)")].map((box) => box.value);
  try {
    localStorage.setItem(UNCHECKED_KEY, JSON.stringify(unchecked));
  } catch {
    // 保存できない環境（プライベートモード等）では毎回すべて選んだ状態になるだけ
  }
}

function selectedSources() {
  return [...sourceBox.querySelectorAll("input:checked")].map((box) => box.value);
}

// 接続確認の表。results（確認の結果）がなければ、設定の有無だけ出す
function renderStatus(status, results) {
  const STATE_LABELS = { ok: "OK", error: "エラー", unset: "未設定" };
  const row = (name, result) => {
    const tr = el("tr");
    tr.append(
      el("th", "", name),
      el("td", `state ${result.state || ""}`, STATE_LABELS[result.state] || ""),
      el("td", "status-message", result.message),
    );
    return tr;
  };
  const rows = [row("LLM", results?.llm ?? { message: `${status.llm.model}（${status.llm.base_url}）` })];
  for (const s of status.sources) {
    const unset = { state: "unset", message: `${s.requires} を .env に書くと使えます` };
    rows.push(row(s.label, results?.sources[s.id] ?? (s.configured ? { message: "設定済み（未確認）" } : unset)));
  }
  document.getElementById("status-table").replaceChildren(...rows);
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
  closeSidebar();
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
  let director = "";
  // 所長の回答（届いた分）。描き直しは画面の更新1回につき1度にまとめる
  const answer = el("div", "markdown streaming");
  let answerText = "";
  let drawing = false;
  const drawAnswer = () => {
    if (drawing) return;
    drawing = true;
    requestAnimationFrame(() => {
      drawing = false;
      answer.replaceChildren(renderMarkdown(answerText));
      if (!answer.isConnected && answerText) node.append(answer);
      log.scrollTop = log.scrollHeight;
    });
  };
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
      director = event.agent;
      row("director", `🕵️ ${director}所長: 報告をまとめています…`);
      node.append(rows.get("director")); // 再調査の後は最後の行に移す
      // 再調査を頼む前に書きかけた文章は捨てる
      answerText = "";
      answer.remove();
    } else if (event.type === "delta") {
      answerText += event.text;
      drawAnswer();
    } else if (event.type === "followup") {
      row(`followup:${event.source}`, `🕵️ ${director}所長 → ${event.agent}: 再調査を依頼「${event.instruction}」`);
    }
  };
  return { node, update };
}

function assistantMessage({ agent, content, model, reports }) {
  const box = el("div", "msg assistant");
  box.append(el("div", "meta", `${agent}所長 · ${model}`));
  for (const r of reports) box.append(reportView(r));
  box.append(markdownView(content), copyButton(content));
  return box;
}

function copyButton(text) {
  const button = el("button", "copy", "コピー");
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

function reportView(report) {
  const details = el("details", "report");
  const status = report.error ? "調査失敗" : `検索 ${report.steps.length}回`;
  const kind = report.request ? "再調査の報告" : "報告";
  details.append(el("summary", "", `👻 ${report.agent}（${report.label}）の${kind} · ${status}`));
  if (report.request) details.append(el("div", "report-request", `所長からの依頼: ${report.request}`));
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
  const node = el("div", "empty");
  node.id = "empty";
  const logo = el("img", "empty-logo");
  logo.src = "/static/ghost.svg";
  logo.alt = "";
  const examples = el("div", "examples");
  for (const text of EXAMPLES) {
    const chip = el("button", "example", text);
    chip.type = "button";
    chip.addEventListener("click", () => {
      input.value = text;
      autosize();
      input.focus();
    });
    examples.append(chip);
  }
  node.append(logo, el("p", "", "「あの話ってどうなったっけ？」を聞いてください。所員がソースを手分けして調べます。"), examples);
  log.replaceChildren(node);
}

function autosize() {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
}
