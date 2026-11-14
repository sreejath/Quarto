"""A small fake Jira export used by the tests."""
import os

from jira2ado.util import write_json

U1 = {"accountId": "acc-1", "displayName": "Ana Lee", "emailAddress": "ana@example.com"}
U2 = {"accountId": "acc-2", "displayName": "Raj Rao"}

META = {
    "base_url": "https://demo.atlassian.net",
    "sprint_fields": ["customfield_10020"],
    "points_fields": ["customfield_10016"],
    "statuses": {"To Do": "new", "In Progress": "indeterminate", "In Review": "indeterminate", "Done": "done"},
}


def item(field, frm, to, fid=None, tid=None):
    return {"field": field, "from": fid, "fromString": frm, "to": tid, "toString": to}


def story():
    return {
        "key": "DEMO-1",
        "fields": {
            "summary": "Checkout fails on retry",
            "issuetype": {"name": "Story"},
            "status": {"name": "Done"},
            "priority": {"name": "High"},
            "creator": U1, "reporter": U1, "assignee": U2,
            "labels": ["checkout"],
            "components": [{"name": "Cart"}],
            "fixVersions": [{"name": "1.2"}],
            "customfield_10016": 5,
            "customfield_10020": [{"name": "Sprint 1", "startDate": "2024-01-01T00:00:00.000Z",
                                   "endDate": "2024-01-14T00:00:00.000Z"},
                                  {"name": "Sprint 2/A"}],
            "created": "2024-01-02T09:00:00.000-0500",
            "updated": "2024-01-20T09:00:00.000-0500",
            "description": {"type": "doc", "content": []},
            "attachment": [{"id": "900", "filename": "shot.png", "created": "2024-01-03T10:00:00.000-0500",
                            "author": U1, "local_path": "attachments/DEMO-1/900_shot.png"}],
            "issuelinks": [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "DEMO-2"}},
                           {"type": {"name": "Relates"}, "outwardIssue": {"key": "OTHER-9"}}],
        },
        "renderedFields": {"description": '<p>Final <b>text</b> <img src="/rest/api/3/attachment/content/900"></p>'},
        "changelog": [
            {"created": "2024-01-03T09:00:00.000-0500", "author": U1, "items": [
                item("status", "To Do", "In Progress"),
                item("assignee", None, "Raj Rao", None, "acc-2"),
                item("Sprint", "", "Sprint 1")]},
            {"created": "2024-01-04T09:00:00.000-0500", "author": U2, "items": [
                item("description", "first draft", "second draft"),
                item("Component", None, "Cart"),
                item("Component", "Search", None),
                item("Rank", "", "Ranked higher"),
                item("Team", "Alpha", "Beta")]},
            {"created": "2024-01-05T09:00:00.000-0500", "author": U2, "items": [
                item("Story point estimate", "3", "5"),
                item("summary", "Checkout fails", "Checkout fails on retry"),
                item("Sprint", "Sprint 1", "Sprint 1, Sprint 2/A"),
                item("status", "In Progress", "Done")]},
        ],
        "comments": [
            {"created": "2024-01-03T12:00:00.000-0500", "author": U2, "renderedBody": "<p>Looking at it</p>"},
            {"created": "2024-01-05T09:00:00.000-0500", "author": U1,
             "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                 {"type": "text", "text": "Fixed & shipped", "marks": [{"type": "strong"}]}]}]}},
        ],
    }


def subtask():
    return {
        "key": "DEMO-2",
        "fields": {
            "summary": "Add retry test", "issuetype": {"name": "Sub-task"}, "status": {"name": "In Review"},
            "priority": {"name": "Low"}, "creator": U2, "assignee": None, "labels": [], "components": [],
            "fixVersions": [], "created": "2024-01-06T09:00:00.000-0500", "updated": "2024-01-06T09:00:00.000-0500",
            "description": None, "attachment": [], "parent": {"key": "DEMO-1"}, "issuelinks": [],
        },
        "renderedFields": {}, "changelog": [], "comments": [],
    }


def write_export(folder):
    write_json(os.path.join(folder, "meta.json"), META)
    for issue in (story(), subtask()):
        write_json(os.path.join(folder, "issues", issue["key"] + ".json"), issue)
    os.makedirs(os.path.join(folder, "attachments", "DEMO-1"), exist_ok=True)
    with open(os.path.join(folder, "attachments", "DEMO-1", "900_shot.png"), "wb") as fh:
        fh.write(b"png-bytes")


class FakeAdo:
    """Stands in for Azure DevOps and applies the same date rule the real service does."""

    def __init__(self, fail_after=None):
        self.items, self.next_id, self.uploads, self.iterations = {}, 100, [], []
        self.fail_after, self.calls = fail_after, 0

    def _apply(self, it, ops):
        from jira2ado.ado_client import AdoError
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise AdoError("simulated outage", 503)
        changed = [o["value"] for o in ops if o["path"] == "/fields/System.ChangedDate"]
        if changed:
            assert it["last"] is None or changed[0] > it["last"], "ChangedDate must increase"
            it["last"] = changed[0]
        for o in ops:
            if o["path"].startswith("/fields/"):
                ref = o["path"][8:]
                if ref == "System.History":
                    it["comments"].append((it["last"], o["value"]))
                else:
                    it["fields"][ref] = o["value"]
            else:
                it["relations"].append(o["value"])
        it["rev"] += 1
        it["log"].append(ops)

    def create_item(self, wit, ops):
        self.next_id += 1
        it = {"id": self.next_id, "type": wit, "fields": {}, "relations": [], "comments": [], "rev": 0,
              "last": None, "log": []}
        self._apply(it, ops)
        self.items[it["id"]] = it
        return {"id": it["id"]}

    def update_item(self, item_id, ops):
        self._apply(self.items[item_id], ops)
        return {"id": item_id}

    def get_item(self, item_id):
        it = self.items[item_id]
        return {"id": item_id, "rev": it["rev"], "fields": it["fields"], "relations": it["relations"]}

    def comment_count(self, item_id):
        return len(self.items[item_id]["comments"])

    def upload_attachment(self, path, filename):
        self.uploads.append(filename)
        return f"https://ado.example/_apis/wit/attachments/{len(self.uploads)}?fileName={filename}"

    def item_url(self, item_id):
        return f"https://ado.example/_apis/wit/workItems/{item_id}"

    type_field_map = None   # {type: set of field refs}; None means "no check"
    areas = None

    def create_iteration(self, name, start=None, finish=None, parent=None):
        self.iterations.append((name, start, finish))
        self.iteration_parents = getattr(self, "iteration_parents", []) + [parent]
        return True

    def subscribe_team_iteration(self, path, team=None):
        self.subscribed_iterations = getattr(self, "subscribed_iterations", []) + [(path, team)]
        return True

    def delete_item(self, item_id, destroy=False):
        if item_id in self.items:
            del self.items[item_id]
            return True
        return False

    def create_area(self, path):
        self.areas = (self.areas or []) + [path]

    type_states = None      # {type: [states]}; None means "no check"
    available_types = None  # list of WIT names; None means default Scrum types

    def work_item_types(self):
        return ["Product Backlog Item", "Bug", "Task", "Epic", "Feature"] if self.available_types is None else self.available_types

    def states(self, wit):
        return ["New", "Approved", "Committed", "Done", "Removed", "To Do", "In Progress"] if self.type_states is None \
            else self.type_states.get(wit, [])

    def type_fields(self, wit):
        return None if self.type_field_map is None else self.type_field_map.get(wit, set())

    def find_duplicates(self, entries, key_field=None, titles=True):
        from jira2ado.duplicates import classify
        self.key_searches = getattr(self, "key_searches", []) + [(sorted(k for k, _ in entries), key_field)]
        items = [{"id": i, "fields": it["fields"]} for i, it in sorted(self.items.items())]
        return classify(entries, items, key_field, titles)
