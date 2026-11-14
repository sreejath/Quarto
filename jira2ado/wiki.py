"""Jira wiki markup to HTML.

Jira's change log stores old descriptions as wiki markup, not as ADF.
This covers the markup people actually use: headings, emphasis, colour,
lists, tables, links, images, @mentions, code and quote blocks.
"""
import re

from .adf import esc

INLINE = [
    (re.compile(r"\{\{(.+?)\}\}"), r"<code>\1</code>"),
    (re.compile(r"\{color:([#\w]+)\}(.*?)\{color\}"), r'<span style="color:\1">\2</span>'),
    (re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])"), r"<b>\1</b>"),
    (re.compile(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])"), r"<i>\1</i>"),
    (re.compile(r"(?<![\w+])\+(?=\S)(.+?)(?<=\S)\+(?![\w+])"), r"<u>\1</u>"),
    (re.compile(r"(?<![\w\-])-(?=[^\s\-])(.+?)(?<=[^\s\-])-(?![\w\-])"), r"<s>\1</s>"),
]
IMAGE = re.compile(r"!([^!\s|][^!|]*?)(?:\|[^!]*)?!")
LINK = re.compile(r"\[([^\[\]|]+)\|([^\[\]]+)\]")
BARE = re.compile(r"\[((?:https?|mailto):[^\[\]\s]+)\]")
MENTION = re.compile(r"\[~(?:accountid:)?([^\]]+)\]")
LIST = re.compile(r"^\s*([*#-]+)\s+(.*)$")
HEAD = re.compile(r"^\s*h([1-6])\.\s+(.*)$")
BLOCK = re.compile(r"^\s*\{(quote|panel)(?::[^}]*)?\}\s*$")
PRE = re.compile(r"\{(code|noformat)(?::[^}]*)?\}")


def _inline(text, att_by_name, user_names=None):
    text = esc(text)
    user_names = user_names or {}

    def image(m):
        name = m.group(1).strip()
        if name in att_by_name:
            return f'<img src="jira-attachment:{att_by_name[name]}" alt="{name}">'
        if name.startswith(("http://", "https://")):
            return f'<img src="{name}">'
        return f"<i>[image: {name}]</i>"

    def mention(m):
        uid = m.group(1).strip()
        resolved = user_names.get(uid) or uid
        return f"<b>@{esc(resolved)}</b>"

    text = IMAGE.sub(image, text)
    text = MENTION.sub(mention, text)
    text = LINK.sub(lambda m: f'<a href="{m.group(2).strip()}">{m.group(1).strip()}</a>', text)
    text = BARE.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', text)
    for pattern, repl in INLINE:
        text = pattern.sub(repl, text)
    text = re.sub(r"\{color(?::[^}]*)?\}", "", text)  # unclosed colour tags
    return text.replace("\\\\", "<br>")


def _cells(line):
    """Split a table row on | while leaving [text|url] links intact."""
    cells, cur, depth, i = [], "", 0, 0
    while i < len(line):
        ch = line[i]
        if ch in "[{":
            depth += 1
        elif ch in "]}" and depth:
            depth -= 1
        if ch == "|" and depth == 0:
            cells.append(cur)
            cur = ""
            while i + 1 < len(line) and line[i + 1] == "|":
                i += 1
        else:
            cur += ch
        i += 1
    cells.append(cur)
    return [c.strip() for c in cells[1:-1]] if line.strip().endswith("|") else [c.strip() for c in cells[1:]]


def wiki_to_html(text, att_by_name=None, user_names=None):
    if not text:
        return ""
    att = att_by_name or {}
    users = user_names or {}
    out, para, lists, table = [], [], [], []
    lines = text.replace("\r\n", "\n").split("\n")

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def close_lists(depth=0):
        while len(lists) > depth:
            out.append("</li></%s>" % lists.pop())

    def flush_table():
        if table:
            out.append('<table border="1" style="border-collapse:collapse">' + "".join(table) + "</table>")
            table.clear()

    def flush_all():
        flush_para(); close_lists(); flush_table()

    i = 0
    while i < len(lines):
        line = lines[i]
        pre = PRE.search(line)
        if pre:
            flush_all()
            tag = pre.group(1)
            rest = line[pre.end():]
            body = []
            close = re.search(r"\{%s\}" % tag, rest)
            if close:
                body.append(rest[:close.start()])
            else:
                if rest.strip():
                    body.append(rest)
                i += 1
                while i < len(lines) and not re.search(r"\{%s\}" % tag, lines[i]):
                    body.append(lines[i]); i += 1
                if i < len(lines):
                    body.append(lines[i][: re.search(r"\{%s\}" % tag, lines[i]).start()])
            out.append("<pre><code>" + esc("\n".join(body).strip("\n")) + "</code></pre>")
            i += 1
            continue
        block = BLOCK.match(line)
        if block:
            flush_all()
            tag = "blockquote" if block.group(1) == "quote" else "div"
            opened = sum(1 for x in out if x.startswith(f"<{tag}")) - sum(1 for x in out if x == f"</{tag}>")
            out.append(f"</{tag}>" if opened > 0 else (f"<{tag}>" if tag == "blockquote" else '<div style="border:1px solid #ccc;padding:4px 8px">'))
            i += 1
            continue
        stripped = line.strip()
        if stripped.startswith("|"):
            flush_para(); close_lists()
            head = stripped.startswith("||")
            cells = _cells(stripped)
            t = "th" if head else "td"
            table.append("<tr>" + "".join(f"<{t}>{_inline(c, att, users)}</{t}>" for c in cells) + "</tr>")
            i += 1
            continue
        flush_table()
        m = LIST.match(line)
        if m and not (m.group(1) == "-" * len(m.group(1)) and len(m.group(1)) > 1):
            flush_para()
            depth = len(m.group(1))
            kind = "ol" if m.group(1)[-1] == "#" else "ul"
            if depth > len(lists):
                while depth > len(lists):
                    out.append(f"<{kind}><li>")
                    lists.append(kind)
            else:
                close_lists(depth)
                if lists and lists[-1] != kind:
                    out.append("</li></%s><%s><li>" % (lists.pop(), kind)); lists.append(kind)
                else:
                    out.append("</li><li>")
            out.append(_inline(m.group(2), att, users))
            i += 1
            continue
        close_lists()
        h = HEAD.match(line)
        if h:
            flush_para()
            out.append(f"<h{h.group(1)}>{_inline(h.group(2), att, users)}</h{h.group(1)}>")
        elif re.match(r"^\s*bq\.\s+", line):
            flush_para()
            out.append("<blockquote>" + _inline(re.sub(r"^\s*bq\.\s+", "", line), att, users) + "</blockquote>")
        elif re.match(r"^\s*-{4,}\s*$", line):
            flush_para()
            out.append("<hr>")
        elif not stripped:
            flush_para()
        else:
            para.append(_inline(line, att, users))
        i += 1
    flush_all()
    return "".join(out)
