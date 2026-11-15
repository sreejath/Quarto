"""Decide whether an Azure DevOps item may already hold a Jira ticket.

Three kinds of match, strongest first:
  same Jira key           the item has the key as a tag, or in the Jira Key field. Certain.
  Jira key in the title   the item's title contains the key, for example "PROJ-101: Fix banner".
  same title              the item's title equals the Jira summary, ignoring case and extra spaces.
The first kind means the ticket is already there. The other two are possible duplicates.
"""
import re

SAME_KEY = "same Jira key"
KEY_IN_TITLE = "Jira key in the title"
SAME_TITLE = "same title"

_KEY_LIKE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def norm_title(text):
    """Titles compare equal when they differ only in case or spacing."""
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def keys_on_item(item, key_field=None):
    """Upper-cased tags of an ADO item, plus the value of its Jira Key field."""
    f = item.get("fields", {})
    keys = {t.strip().upper() for t in (f.get("System.Tags") or "").split(";") if t.strip()}
    if key_field and f.get(key_field):
        keys.add(str(f[key_field]).strip().upper())
    return keys


def key_in_text(key, text):
    """PROJ-101 counts in "PROJ-101: Fix" or "[PROJ-101] Fix", not in "PROJ-1010"."""
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![0-9])", text or "", re.I))


def classify(entries, items, key_field=None, titles=True):
    """entries: [(jira key, jira summary)]. items: ADO work items with fields.
    Returns {jira key: [match]}, where a match is a dict with kind, id, title, state, type,
    iteration and jira_keys (the Jira keys the ADO item itself carries)."""
    found = {}
    for it in items:
        f = it.get("fields", {})
        keys = keys_on_item(it, key_field)
        title = f.get("System.Title") or ""
        for key, summary in entries:
            if key.upper() in keys:
                kind = SAME_KEY
            elif key_in_text(key, title):
                kind = KEY_IN_TITLE
            elif titles and summary and norm_title(title) == norm_title(summary):
                kind = SAME_TITLE
            else:
                continue
            found.setdefault(key, []).append({
                "kind": kind, "id": it["id"], "title": title, "state": f.get("System.State"),
                "type": f.get("System.WorkItemType"), "iteration": f.get("System.IterationPath"),
                "jira_keys": sorted(k for k in keys if _KEY_LIKE.match(k))})
    for matches in found.values():
        matches.sort(key=lambda m: ([SAME_KEY, KEY_IN_TITLE, SAME_TITLE].index(m["kind"]), m["id"]))
    return found


def describe(match):
    """One line for the preview and logs: 'same title as #100 (Committed, from PROJ-102)'."""
    what = {SAME_KEY: "already there as", KEY_IN_TITLE: "Jira key in the title of", SAME_TITLE: "same title as"}
    extra = [match.get("state") or ""] + [f"from {k}" for k in match.get("jira_keys") or []]
    extra = ", ".join(x for x in extra if x)
    return f"{what[match['kind']]} #{match['id']}" + (f" ({extra})" if extra else "")


def same_summary_groups(entries):
    """Tickets in one run whose summaries match each other: [[key, key, ...], ...]."""
    groups = {}
    for key, summary in entries:
        if summary and summary.strip():
            groups.setdefault(norm_title(summary), []).append(key)
    return [keys for keys in groups.values() if len(keys) > 1]
