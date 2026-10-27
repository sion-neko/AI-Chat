// 表示中の会話ID。null は「新しい会話」（最初の送信でサーバー側に作られる）
let currentId = null;
// 会話の一覧（検索で絞り込むために持っておく）
let conversations = [];
// 所長の名前（回答の見出しに使う）と、ソースIDから画面の名前への対応
let directorName = "所長";
let sourceLabels = new Map();

const log = document.getElementById("log");
const list = document.getElementById("conversations");
const search = document.getElementById("search");
const titleEl = document.getElementById("conversation-title");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const sourceBox = document.getElementById("sources");
const characterDialog = document.getElementById("characters");
const statusDialog = document.getElementById("status");
const traceDialog = document.getElementById("trace");

// 外したソースはブラウザに覚えておく（新しく使えるようになったソースは選んだ状態で出す）
const UNCHECKED_KEY = "unchecked-sources";
// ソースごとに選んだ検索範囲 {ソースID: [名前]}。これもブラウザに覚えておく。ないソースは絞らない
const SCOPES_KEY = "source-scopes";
let selectedScopes = loadJson(SCOPES_KEY, {});

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
  input.focus();
});

search.addEventListener("input", renderList);
sourceBox.addEventListener("change", saveSourceSelection);

document.getElementById("open-characters").addEventListener("click", async () => {
  renderCharacters(await api("GET", "/api/characters"));
  characterDialog.showModal();
});
document.getElementById("close-characters").addEventListener("click", () => characterDialog.close());
// 名前が変わったらソースの表示（所員の名前）も変わる
characterDialog.addEventListener("close", loadConfig);

document.getElementById("open-status").addEventListener("click", async () => {
  renderStatus(await api("GET", "/api/status"));
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

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (running) return cancelTurn();
  const content = input.value.trim();
  if (!content) return;

  if (document.getElementById("empty")) {
    log.replaceChildren();
    titleEl.textContent = content;
  }
  const turn = append(turnView(content));
  input.value = "";
  autosize();

  const progress = progressView();
  turn.append(progress.node);
  running = { turnId: null, cancelling: false };
  setSendButton();
  try {
    const path = currentId ? `/api/conversations/${currentId}/messages` : "/api/conversations";
    let result = null;
    await streamTurn(path, { content, sources: selectedSources(), scopes: selectedScopes }, (event) => {
      if (event.type === "error") throw new Error(event.message);
      if (event.type === "start") running.turnId = event.turn_id;
      else if (event.type === "done" || event.type === "cancelled") result = event;
      else progress.update(event);
    });
    if (!result) throw new Error("応答が途中で切れました");
    if (result.type === "cancelled") {
      // 何も保存されていないので、質問ごと消して入力欄に戻す
      turn.remove();
      restoreQuestion(content);
      if (!log.children.length) showEmpty();
      return;
    }
    progress.node.replaceWith(answerView(result.turn));
    if (!currentId) {
      currentId = result.conversation_id;
      await refreshList();
    }
  } catch (err) {
    progress.node.replaceWith(el("p", "error-text", `調査できませんでした: ${err.message}`));
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
  send.textContent = !running ? "調査を依頼" : running.cancelling ? "中止しています…" : "中止";
  send.classList.toggle("stop", Boolean(running));
  send.disabled = Boolean(running?.cancelling);
}

// 聞き直せるよう、質問を入力欄に戻す（入力中の文章があれば上書きしない）
function restoreQuestion(content) {
  if (input.value.trim()) return;
  input.value = content;
  autosize();
}

// ---- 設定とソース ----

async function loadConfig() {
  const c = await api("GET", "/api/config");
  document.getElementById("model").textContent = c.model;
  directorName = c.director;
  sourceLabels = new Map(c.sources.map((s) => [s.id, s.label]));
  renderSources(c.sources);
}

function renderSources(sources) {
  const unchecked = new Set(loadJson(UNCHECKED_KEY, []));
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
  const names = selectedScopes[source.id] ?? [];
  button.textContent = names.length ? (names.length === 1 ? names[0] : `${names[0]} ほか${names.length - 1}件`) : `すべての${source.scope_label}`;
  button.title = `${source.label}で調べる${source.scope_label}を選ぶ`;
  button.classList.toggle("narrowed", names.length > 0);
  button.addEventListener("click", () => openScope(source, button));
  return button;
}

function loadJson(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}

// 保存できない環境では、開き直すたびに既定（すべて選ぶ・絞らない）に戻るだけ
function saveJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {}
}

function saveSourceSelection() {
  saveJson(UNCHECKED_KEY, [...sourceBox.querySelectorAll("input:not(:checked)")].map((box) => box.value));
}

// ---- 検索範囲の選択 ----

const scopeDialog = document.getElementById("scope");
const scopeList = document.getElementById("scope-list");
const scopeFilter = document.getElementById("scope-filter");
// 開いている範囲の選択 { source, button, choices, checked: Set }
let scopeEditing = null;

async function openScope(source, button) {
  scopeEditing = { source, button, choices: [], checked: new Set(selectedScopes[source.id] ?? []) };
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
  if (checked.size) selectedScopes[source.id] = [...checked];
  else delete selectedScopes[source.id];
  saveJson(SCOPES_KEY, selectedScopes);
  button.replaceWith(scopeButton(source));
  scopeDialog.close();
});

function selectedSources() {
  return [...sourceBox.querySelectorAll("input:checked")].map((box) => box.value);
}

// ---- 接続確認 ----

// results（確認の結果）がなければ、設定の有無だけ出す
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
    const unset = { state: "unset", message: `${s.requires} を .env に書くと使えます` };
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

async function openConversation(id) {
  const data = await api("GET", `/api/conversations/${id}`);
  currentId = id;
  titleEl.textContent = conversations.find((c) => c.id === id)?.title ?? "";
  const nodes = [];
  for (const t of data.turns) {
    if (t.role === "user") nodes.push(turnView(t.content));
    else nodes.at(-1).append(answerView(t));
  }
  log.replaceChildren(...nodes);
  log.scrollTop = log.scrollHeight;
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
  conversations = await api("GET", "/api/conversations");
  renderList();
}

// 更新日ごとにまとめて出す。検索語があればタイトルで絞る
function renderList() {
  const query = search.value.trim().toLowerCase();
  const shown = conversations.filter((c) => !query || c.title.toLowerCase().includes(query));
  const nodes = [];
  let group = null;
  for (const c of shown) {
    const g = dateGroup(c.updated_at);
    if (g !== group) nodes.push(el("div", "conv-group", (group = g)));
    nodes.push(conversationItem(c));
  }
  if (!shown.length) nodes.push(el("div", "conv-empty", query ? "見つかりませんでした" : "まだ調査はありません"));
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
}

function highlightCurrent() {
  for (const item of list.querySelectorAll(".conv")) item.classList.toggle("active", item.dataset.id === currentId);
}

// ---- 調査と回答 ----

// 1往復分の入れ物。質問を出し、回答はあとから足す
function turnView(question) {
  const node = el("section", "turn");
  node.append(el("div", "question", question));
  return node;
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

function answerView({ agent, content, model, reports, links, seconds, llm_calls, trace_id }) {
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
  if (trace_id) {
    const button = el("button", "", `LLMとのやりとり（${llm_calls}回）`);
    button.type = "button";
    button.addEventListener("click", () => openTrace(trace_id));
    actions.append(button);
  }
  // 照合の結果がない（照合を入れる前の）回答には何も出さない
  if (links) actions.append(markLinks(answer, links));
  const record = el("details", "record");
  record.append(el("summary", "", "調査の記録を見る"), membersView(reports.map(memberView)));
  body.append(answer, actions, record);
  return node;
}

// 検索結果になかったリンクに印を付け、根拠の件数を返す
function markLinks(answer, links) {
  const key = (url) => url.replace(/[.,;:!?]+$/, "").replace(/\/+$/, "");
  const missing = new Set(links.filter((l) => !l.found).map((l) => key(l.url)));
  for (const a of answer.querySelectorAll("a")) {
    if (!missing.has(key(a.getAttribute("href")))) continue;
    a.classList.add("unverified");
    a.title = "所員の検索結果にないリンクです。LLMが作ったURLかもしれません";
  }
  if (!links.length) return el("span", "pill unset", "根拠のリンクなし");
  if (!missing.size) return el("span", "pill done", `根拠のリンク${links.length}件はすべて検索結果にあります`);
  const pill = el("span", "pill failed", `根拠のリンク${links.length}件のうち${missing.size}件は検索結果にありません`);
  pill.title = "印の付いたリンクは、所員の検索結果に出てこなかったURLです";
  return pill;
}

// 調査中の表示。所員ごとの状態と、届いた分の回答を出す
function progressView() {
  const { node, body, meta } = answerFrame(directorName);
  meta.textContent = "所員が調べています…";
  const answer = el("div", "markdown streaming");
  const record = el("details", "record");
  record.open = true;
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
    if (event.type === "member_start") {
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
  titleEl.textContent = "新しい調査";
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
  node.append(
    logo,
    el("h2", "", "あの話、どうなったっけ？"),
    el("p", "", "所員が選んだ場所を手分けして調べ、所長が経緯を根拠のリンクつきでまとめます。"),
    examples,
  );
  log.replaceChildren(node);
}

function autosize() {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
}
