"""Rich text handling: Atlassian Document Format to HTML, @mention resolution, and attachment/cross-ticket URL rewriting."""
import html
import re

ATT_PATTERNS = [
    re.compile(r"jira-attachment:(\d+)"),
    re.compile(r"/attachment/(?:content|thumbnail)/(\d+)"),
    re.compile(r"/secure/(?:attachment|thumbnail)/(\d+)/"),
]


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def plain_to_html(text):
    if not text:
        return ""
    return "<div>" + esc(text).replace("\r\n", "\n").replace("\n", "<br>") + "</div>"


def _marks(text, marks):
    for m in marks or []:
        t, a = m.get("type"), m.get("attrs") or {}
        if t == "strong":
            text = f"<b>{text}</b>"
        elif t == "em":
            text = f"<i>{text}</i>"
        elif t == "code":
            text = f"<code>{text}</code>"
        elif t == "strike":
            text = f"<s>{text}</s>"
        elif t == "underline":
            text = f"<u>{text}</u>"
        elif t == "link":
            text = f'<a href="{esc(a.get("href"))}">{text}</a>'
        elif t == "subsup":
            tag = "sub" if a.get("type") == "sub" else "sup"
            text = f"<{tag}>{text}</{tag}>"
        elif t == "textColor":
            text = f'<span style="color:{esc(a.get("color"))}">{text}</span>'
    return text


def adf_to_html(node, att_by_name=None, user_names=None):
    """Convert an ADF node to HTML. att_by_name maps file name -> Jira attachment id.
    user_names maps Jira accountId -> display name for @mention resolution."""
    if not node:
        return ""
    if isinstance(node, str):
        return plain_to_html(node)
    att_by_name = att_by_name or {}
    user_names = user_names or {}
    t = node.get("type")
    a = node.get("attrs") or {}
    kids = "".join(adf_to_html(c, att_by_name, user_names) for c in node.get("content") or [])
    if t == "doc":
        return kids
    if t == "text":
        return _marks(esc(node.get("text")), node.get("marks"))
    if t == "paragraph":
        return f"<p>{kids}</p>"
    if t == "hardBreak":
        return "<br>"
    if t == "heading":
        lvl = min(max(int(a.get("level", 1)), 1), 6)
        return f"<h{lvl}>{kids}</h{lvl}>"
    if t in ("bulletList", "taskList", "decisionList"):
        return f"<ul>{kids}</ul>"
    if t == "orderedList":
        return f"<ol>{kids}</ol>"
    if t in ("listItem", "decisionItem"):
        return f"<li>{kids}</li>"
    if t == "taskItem":
        box = "[x]" if a.get("state") == "DONE" else "[ ]"
        return f"<li>{box} {kids}</li>"
    if t == "codeBlock":
        return f"<pre><code>{kids}</code></pre>"
    if t == "blockquote":
        return f"<blockquote>{kids}</blockquote>"
    if t == "panel":
        return f'<div style="border-left:4px solid #999;padding:4px 8px"><b>{esc(a.get("panelType", "note")).title()}</b>{kids}</div>'
    if t in ("expand", "nestedExpand"):
        return f"<div><b>{esc(a.get('title'))}</b>{kids}</div>"
    if t == "rule":
        return "<hr>"
    if t == "table":
        return f'<table border="1" style="border-collapse:collapse">{kids}</table>'
    if t == "tableRow":
        return f"<tr>{kids}</tr>"
    if t in ("tableHeader", "tableCell"):
        tag = "th" if t == "tableHeader" else "td"
        span = "".join(f' {k}="{int(a[k])}"' for k in ("colspan", "rowspan") if a.get(k, 1) != 1)
        return f"<{tag}{span}>{kids}</{tag}>"
    if t == "mention":
        uid = str(a.get("id") or "")
        text = a.get("text")
        if not text and uid in user_names and user_names[uid]:
            text = f"@{user_names[uid]}"
        elif text and uid in user_names and user_names[uid] and uid in text:
            text = f"@{user_names[uid]}"
        return f"<b>{esc(text or '@' + (user_names.get(uid) or uid or 'user'))}</b>"
    if t == "emoji":
        return esc(a.get("text") or a.get("shortName"))
    if t in ("inlineCard", "blockCard", "embedCard"):
        url = a.get("url") or ""
        return f'<a href="{esc(url)}">{esc(url)}</a>'
    if t == "date":
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(int(a["timestamp"]) / 1000, timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return ""
    if t == "status":
        return f"<b>[{esc(a.get('text'))}]</b>"
    if t in ("mediaSingle", "mediaGroup"):
        return f"<div>{kids}</div>"
    if t in ("media", "mediaInline"):
        name = a.get("alt") or ""
        if a.get("type") == "external" and a.get("url"):
            return f'<img src="{esc(a["url"])}">'
        if name in att_by_name:
            ref = f"jira-attachment:{att_by_name[name]}"
            if re.search(r"\.(png|jpe?g|gif|bmp|webp|svg)$", name, re.I):
                return f'<img src="{ref}" alt="{esc(name)}">'
            return f'<a href="{ref}">{esc(name)}</a>'
        return f"<i>[attachment: {esc(name or 'see attachments')}]</i>"
    return kids


def rewrite_html(html_text, att_urls, jira_base="", key_to_ado=None, ado_edit_base=""):
    """Point attachment references at the uploaded ADO copies, make site-relative Jira links absolute,
    and optionally annotate/resolve cross-ticket Jira links to ADO work items."""
    if not html_text:
        return html_text

    def repl(m):
        attr, url = m.group(1), html.unescape(m.group(2))
        for p in ATT_PATTERNS:
            hit = p.search(url)
            if hit:
                new = att_urls.get(hit.group(1))
                if new:
                    return f'{attr}="{esc(new)}"'
                break
        if url.startswith("/") and not url.startswith("//") and jira_base:
            return f'{attr}="{esc(jira_base + url)}"'
        return m.group(0)

    out = re.sub(r'\b(src|href)="([^"]*)"', repl, html_text)
    if key_to_ado and ado_edit_base:
        def key_repl(m):
            full, key = m.group(0), m.group(1).upper()
            ado_id = key_to_ado.get(key)
            if ado_id and f"#{ado_id}" not in full:
                return f'{full} <a href="{esc(ado_edit_base + str(ado_id))}">(#{ado_id})</a>'
            return full
        out = re.sub(r'<a\s+href="[^"]*/browse/([A-Za-z][A-Za-z0-9]+-\d+)"[^>]*>.*?</a>', key_repl, out)
    return out
