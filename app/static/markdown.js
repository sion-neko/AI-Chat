// LLMの回答でよく使われるMarkdownだけを表示する小さなレンダラ。
// DOMを組み立てて返す。innerHTMLは使わない（回答本文をHTMLとして解釈させない）
//
// ブロック: 見出し(#) / 段落 / 箇条書き(-*+ と 1.、インデントで入れ子) / 引用(>) / コード(```) / 表(|) / 区切り線(---)
// インライン: `code` / **太字** / *斜体* / ~~取り消し~~ / [文字](URL) / 裸のURL

// 公開するのは renderMarkdown だけ（app.js の名前とぶつからないように閉じ込める）
const renderMarkdown = (() => {
  function renderMarkdown(text) {
    const frag = document.createDocumentFragment();
    for (const block of parseBlocks(text.replace(/\r\n?/g, "\n").split("\n"))) frag.append(block);
    return frag;
  }

  const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const TABLE_SEPARATOR = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

  function parseBlocks(lines) {
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      if (!line.trim()) {
        i++;
        continue;
      }

      const fence = line.match(/^\s*(```+|~~~+)\s*(\S*)/);
      if (fence) {
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
        i++; // 閉じフェンス（なければ末尾まで）
        const pre = mdEl("pre");
        const code = mdEl("code", body.join("\n"));
        if (fence[2]) code.dataset.lang = fence[2];
        pre.append(code);
        blocks.push(pre);
        continue;
      }

      const heading = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (heading) {
        blocks.push(wrapInline(`h${heading[1].length}`, heading[2]));
        i++;
        continue;
      }

      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        blocks.push(mdEl("hr"));
        i++;
        continue;
      }

      if (/^\s*>/.test(line)) {
        const body = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ""));
        const quote = mdEl("blockquote");
        quote.append(...parseBlocks(body));
        blocks.push(quote);
        continue;
      }

      if (line.includes("|") && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1])) {
        const rows = [line];
        i += 2;
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(lines[i++]);
        blocks.push(table(rows));
        continue;
      }

      if (LIST_ITEM.test(line)) {
        const items = [];
        while (i < lines.length && (LIST_ITEM.test(lines[i]) || (/^\s+\S/.test(lines[i]) && items.length))) {
          items.push(lines[i++]);
        }
        blocks.push(list(items));
        continue;
      }

      const para = [];
      while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) para.push(lines[i++].trim());
      if (!para.length) para.push(lines[i++].trim()); // どのブロックにもならない行（念のため）
      const p = mdEl("p");
      para.forEach((l, n) => {
        if (n) p.append(mdEl("br"));
        p.append(inline(l));
      });
      blocks.push(p);
    }
    return blocks;
  }

  function startsBlock(lines, i) {
    const line = lines[i];
    return (
      /^\s*(```|~~~|#{1,6}\s|>)/.test(line) ||
      LIST_ITEM.test(line) ||
      /^\s*([-*_])(\s*\1){2,}\s*$/.test(line) ||
      (line.includes("|") && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1]))
    );
  }

  // 箇条書き。最初の項目より深くインデントされた行は、直前の項目の中身（入れ子）として扱う
  function list(lines) {
    const first = lines[0].match(LIST_ITEM);
    const base = first[1].length;
    const node = mdEl(/\d/.test(first[2]) ? "ol" : "ul");
    if (node.tagName === "OL" && parseInt(first[2]) !== 1) node.start = parseInt(first[2]);

    let item = null;
    let children = [];
    const flush = () => {
      if (item && children.length) item.append(...parseBlocks(children.map((l) => l.slice(Math.min(indentOf(l), base + 2)))));
      children = [];
    };
    for (const line of lines) {
      const m = line.match(LIST_ITEM);
      if (m && m[1].length <= base) {
        flush();
        item = wrapInline("li", m[3]);
        node.append(item);
      } else {
        children.push(line);
      }
    }
    flush();
    return node;
  }

  function indentOf(line) {
    return line.match(/^\s*/)[0].length;
  }

  function table(rows) {
    const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    const node = mdEl("table");
    const head = mdEl("thead");
    const headRow = mdEl("tr");
    for (const c of cells(rows[0])) headRow.append(wrapInline("th", c));
    head.append(headRow);
    const body = mdEl("tbody");
    for (const row of rows.slice(1)) {
      const tr = mdEl("tr");
      for (const c of cells(row)) tr.append(wrapInline("td", c));
      body.append(tr);
    }
    node.append(head, body);
    const wrap = mdEl("div");
    wrap.className = "table-wrap";
    wrap.append(node);
    return wrap;
  }

  // インライン要素。先に見つかったものから順に処理し、残りを再帰で処理する
  const INLINE = [
    { re: /`([^`]+)`/, build: (m) => mdEl("code", m[1]) },
    { re: /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/, build: (m) => link(m[2], inline(m[1])) },
    // 裸のURLの切れ目は、出典を照合する app/links.py の URL と同じにする（表示と照合でURLがずれないように）
    { re: /https?:\/\/[^\s"'<>()[\]{}）」、。]+/, build: (m) => link(trimUrl(m[0]), trimUrl(m[0])), rest: (m) => m[0].slice(trimUrl(m[0]).length) },
    { re: /\*\*(.+?)\*\*|__(.+?)__/, build: (m) => wrapInline("strong", m[1] ?? m[2]) },
    { re: /~~(.+?)~~/, build: (m) => wrapInline("del", m[1]) },
    { re: /(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)/, build: (m) => wrapInline("em", m[1] ?? m[2]) },
  ];

  function inline(text) {
    const frag = document.createDocumentFragment();
    let rest = text;
    while (rest) {
      let best = null;
      for (const rule of INLINE) {
        const m = rest.match(rule.re);
        if (m && (!best || m.index < best.m.index)) best = { rule, m };
      }
      if (!best) {
        frag.append(rest);
        break;
      }
      const { rule, m } = best;
      frag.append(rest.slice(0, m.index), rule.build(m));
      rest = (rule.rest ? rule.rest(m) : "") + rest.slice(m.index + m[0].length);
    }
    return frag;
  }

  // 文末の句読点はURLに含めない（app/links.py の TRAILING と同じ）
  function trimUrl(url) {
    return url.replace(/[.,;:!?]+$/, "");
  }

  function link(href, content) {
    const a = mdEl("a");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.append(content);
    return a;
  }

  function wrapInline(tag, text) {
    const node = mdEl(tag);
    node.append(inline(text));
    return node;
  }

  function mdEl(tag, text) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  return renderMarkdown;
})();
