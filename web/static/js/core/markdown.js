/* ── Markdown renderer ──────────────────────────────────────────────── */
/* Lightweight, dependency-free markdown-to-HTML for artifact display.  */
/* Handles: headings, bold, italic, inline code, code blocks, unordered  */
/* and ordered lists, blockquotes, horizontal rules, and paragraphs.    */
export function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function renderMarkdown(md) {
  if (!md) return '';
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let i = 0;
  let inCode = false;
  let codeLang = '';
  let codeBuf = [];
  let listType = null; // 'ul' or 'ol'
  let listBuf = [];
  let paraBuf = [];

  function inline(text) {
    let s = escapeHtml(text);
    // inline code
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    // bold
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    // italic
    s = s.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
    s = s.replace(/(?<!_)_([^_]+)_(?!_)/g, '<em>$1</em>');
    // links [text](url) — url must be http/https/mailto
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return s;
  }

  function flushPara() {
    if (paraBuf.length) {
      out.push('<p>' + paraBuf.map(inline).join('<br>') + '</p>');
      paraBuf = [];
    }
  }
  function flushList() {
    if (listBuf.length) {
      out.push(`<${listType}>` + listBuf.join('') + `</${listType}>`);
      listBuf = [];
      listType = null;
    }
  }
  function flushAll() { flushPara(); flushList(); }

  while (i < lines.length) {
    const line = lines[i];

    // Code block fence
    if (line.match(/^```/)) {
      if (inCode) {
        out.push('<pre><code>' + escapeHtml(codeBuf.join('\n')) + '</code></pre>');
        codeBuf = [];
        inCode = false;
        codeLang = '';
      } else {
        flushAll();
        inCode = true;
        codeLang = line.replace(/^```/, '').trim();
      }
      i++;
      continue;
    }
    if (inCode) { codeBuf.push(line); i++; continue; }

    // Horizontal rule
    if (line.match(/^---+\s*$/) || line.match(/^\*\*\*+\s*$/)) {
      flushAll();
      out.push('<hr>');
      i++;
      continue;
    }

    // Headings
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushAll();
      const level = h[1].length;
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }

    // Blockquote
    if (line.match(/^>\s?/)) {
      flushAll();
      const quoteLines = [];
      while (i < lines.length && lines[i].match(/^>\s?/)) {
        quoteLines.push(lines[i].replace(/^>\s?/, ''));
        i++;
      }
      out.push('<blockquote>' + quoteLines.map(inline).join('<br>') + '</blockquote>');
      continue;
    }

    // Unordered list
    if (line.match(/^[-*+]\s+/)) {
      flushPara();
      if (listType !== 'ul') { flushList(); listType = 'ul'; }
      const itemText = line.replace(/^[-*+]\s+/, '');
      // Handle nested indentation
      const nested = itemText.match(/^(\s+)(.*)$/);
      if (nested && listBuf.length) {
        listBuf[listBuf.length - 1] += '<br>' + inline(nested[2]);
      } else {
        listBuf.push('<li>' + inline(itemText) + '</li>');
      }
      i++;
      continue;
    }

    // Ordered list
    if (line.match(/^\d+\.\s+/)) {
      flushPara();
      if (listType !== 'ol') { flushList(); listType = 'ol'; }
      const itemText = line.replace(/^\d+\.\s+/, '');
      listBuf.push('<li>' + inline(itemText) + '</li>');
      i++;
      continue;
    }

    // Non-list line ends a list
    if (listType) flushList();

    // Blank line ends a paragraph
    if (line.trim() === '') {
      flushPara();
      i++;
      continue;
    }

    // Accumulate paragraph
    paraBuf.push(line);
    i++;
  }

  // Flush remaining
  if (inCode) out.push('<pre><code>' + escapeHtml(codeBuf.join('\n')) + '</code></pre>');
  flushAll();

  return out.join('\n');
}

export function mdToElement(md) {
  const div = document.createElement('div');
  div.className = 'markdown-body';
  div.innerHTML = renderMarkdown(md);
  return div;
}
