import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sample
from jira2ado import config
from jira2ado.adf import adf_to_html, rewrite_html
from jira2ado.importer import import_items, import_links
from jira2ado.planner import Planner
from jira2ado.verify import verify

USERS = {"ids": {"acc-1": "ana@contoso.com"}, "names": {"acc-1": "Ana Lee", "acc-2": "Raj Rao"}}


def cfg_for(folder):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["jira"]["base_url"] = "https://demo.atlassian.net"
    cfg["ado"].update(org_url="https://ado.example", project="Shop")
    cfg["paths"]["export_dir"] = folder
    return cfg


class AdfTests(unittest.TestCase):
    def test_marks_lists_tables_and_escaping(self):
        doc = {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "a <b> & c", "marks": [{"type": "strong"}, {"type": "link", "attrs": {"href": "http://x"}}]}]},
            {"type": "bulletList", "content": [{"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]}]},
            {"type": "table", "content": [{"type": "tableRow", "content": [
                {"type": "tableHeader", "attrs": {"colspan": 2}, "content": []}]}]},
            {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"type": "file", "alt": "shot.png"}}]},
        ]}
        html = adf_to_html(doc, {"shot.png": "900"})
        self.assertIn('<a href="http://x"><b>a &lt;b&gt; &amp; c</b></a>', html)
        self.assertIn("<ul><li><p>one</p></li></ul>", html)
        self.assertIn('<th colspan="2">', html)
        self.assertIn('src="jira-attachment:900"', html)

    def test_rewrite(self):
        html = '<img src="/rest/api/3/attachment/content/900"><a href="/browse/X-1">x</a><a href="https://o.com">o</a>'
        out = rewrite_html(html, {"900": "https://ado/att/1"}, "https://demo.atlassian.net")
        self.assertIn('src="https://ado/att/1"', out)
        self.assertIn('href="https://demo.atlassian.net/browse/X-1"', out)
        self.assertIn('href="https://o.com"', out)


class WikiTests(unittest.TestCase):
    def test_common_markup(self):
        from jira2ado.wiki import wiki_to_html
        src = ("h2. Steps\n{color:#bf2600}*Dev only*{color} and _note_ on 2026-04-22\n"
               "* one\n** nested [link|https://x.com/a?b=1&c=2]\n# first\n\n"
               "||Col A||Col B||\n|a [x|https://y.com]|b|\n!shot.png|thumbnail! !missing.png!\n"
               "{code:sql}\nselect * from t where a < 1\n{code}\n{quote}\nquoted\n{quote}\n----\nline one\\\\ two")
        html = wiki_to_html(src, {"shot.png": "900"})
        self.assertIn("<h2>Steps</h2>", html)
        self.assertIn('<span style="color:#bf2600"><b>Dev only</b></span>', html)
        self.assertIn("<i>note</i> on 2026-04-22", html)
        self.assertIn('<ul><li>one<ul><li>nested <a href="https://x.com/a?b=1&amp;c=2">link</a></li></ul></li></ul><ol><li>first</li></ol>', html)
        self.assertIn("<tr><th>Col A</th><th>Col B</th></tr>", html)
        self.assertIn('<td>a <a href="https://y.com">x</a></td><td>b</td>', html)
        self.assertIn('<img src="jira-attachment:900" alt="shot.png">', html)
        self.assertIn("<i>[image: missing.png]</i>", html)
        self.assertIn("<pre><code>select * from t where a &lt; 1</code></pre>", html)
        self.assertIn("<blockquote><p>quoted</p></blockquote>", html)
        self.assertIn("<hr>", html)
        self.assertIn("<p>line one<br> two</p>", html)

    def test_plain_text_is_safe(self):
        from jira2ado.wiki import wiki_to_html
        self.assertEqual(wiki_to_html("a <script> & b - c"), "<p>a &lt;script&gt; &amp; b - c</p>")
        self.assertEqual(wiki_to_html(""), "")


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.p = Planner(cfg_for("x"), sample.META, USERS)
        self.plan = self.p.plan(sample.story())
        self.revs = self.plan["revisions"]

    def test_initial_values_are_rebuilt_from_history(self):
        f = self.revs[0]["fields"]
        self.assertEqual(self.plan["type"], "Product Backlog Item")
        self.assertEqual(f["System.Title"], "Checkout fails")
        self.assertEqual(f["System.State"], "New")
        self.assertEqual(f["System.AssignedTo"], "")
        self.assertEqual(f["Microsoft.VSTS.Scheduling.Effort"], 3.0)
        self.assertEqual(f["System.IterationPath"], "Shop")
        self.assertEqual(f["System.Description"], "<p>first draft</p>")
        self.assertEqual(f["Microsoft.VSTS.Common.ValueArea"], "Business")
        self.assertEqual(f["System.Tags"], "checkout; component:Search; version:1.2; DEMO-1")
        self.assertEqual(self.revs[0]["date"], "2024-01-02T14:00:00.000Z")

    def test_replay_order_and_final_state(self):
        dates = [r["date"] for r in self.revs]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(self.revs), 7)  # create, 3 changes, 2 comments, 1 attachment group
        final = {}
        for r in self.revs:
            final.update(r["fields"])
        self.assertEqual(final["System.Title"], "Checkout fails on retry")
        self.assertEqual(final["System.State"], "Done")
        self.assertEqual(final["Microsoft.VSTS.Common.ClosedDate"], "2024-01-05T14:00:00.000Z")
        self.assertEqual(final["Microsoft.VSTS.Scheduling.Effort"], 5.0)
        self.assertEqual(final["System.IterationPath"], "Shop\\Sprint 2-A")
        self.assertEqual(final["System.Tags"], "checkout; component:Cart; version:1.2; DEMO-1")
        self.assertIn("Final <b>text</b>", final["System.Description"])
        self.assertFalse(any("did not add up" in w for w in self.p.warnings))

    def test_unmapped_fields_become_history_notes_and_rank_is_ignored(self):
        notes = [r["comment"] for r in self.revs if r["comment"] and "Jira history" in r["comment"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("Team: Alpha → Beta", notes[0])
        self.assertNotIn("Rank", notes[0])

    def test_unmapped_assignee_is_reported(self):
        self.assertTrue(any("Raj Rao is not in the user map" in w for w in self.p.warnings))

    def test_incomplete_history_is_reconciled(self):
        issue = sample.story()
        issue["fields"]["status"] = {"name": "In Review"}  # log says Done, Jira says In Review
        plan = self.p.plan(issue)
        self.assertEqual(plan["revisions"][-1]["fields"]["System.State"], "Committed")
        self.assertIn("Migration note", plan["revisions"][-1]["comment"])

    def test_subtask_maps_to_task_states(self):
        plan = self.p.plan(sample.subtask())
        self.assertEqual(plan["type"], "Task")
        self.assertEqual(plan["revisions"][0]["fields"]["System.State"], "In Progress")
        self.assertNotIn("Microsoft.VSTS.Scheduling.Effort", plan["revisions"][0]["fields"])
        self.assertNotIn("Microsoft.VSTS.Common.ValueArea", plan["revisions"][0]["fields"])

    def test_type_specific_default_wins(self):
        cfg = cfg_for("x")
        cfg["mapping"]["create_defaults"]["Product Backlog Item"] = {"Microsoft.VSTS.Common.ValueArea": "Architectural"}
        plan = Planner(cfg, sample.META, USERS).plan(sample.story())
        self.assertEqual(plan["revisions"][0]["fields"]["Microsoft.VSTS.Common.ValueArea"], "Architectural")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        sample.write_export(self.dir)
        self.cfg = cfg_for(self.dir)
        self.q = lambda *a, **k: None

    def test_full_import_links_and_verify(self):
        ado = sample.FakeAdo()
        stats = import_items(ado, self.cfg, USERS, log=self.q)
        self.assertEqual((stats["created"], stats["failed"]), (2, []))
        story = ado.items[101]
        self.assertEqual(story["rev"], 7)
        self.assertEqual(story["fields"]["System.CreatedBy"], "ana@contoso.com")
        self.assertEqual(story["fields"]["System.CreatedDate"], "2024-01-02T14:00:00.000Z")
        self.assertEqual(len(story["comments"]), 3)
        self.assertTrue(any("Jira author: Raj Rao" in c for _, c in story["comments"]))  # unmapped author is named
        self.assertIn("https://ado.example/_apis/wit/attachments/1", story["fields"]["System.Description"])
        rels = [r["rel"] for r in story["relations"]]
        self.assertEqual(sorted(rels), ["AttachedFile", "Hyperlink"])
        self.assertIn(("Sprint 2-A", None, None), ado.iterations)
        self.assertIn(("Sprint 1", "2024-01-01T00:00:00.000Z", "2024-01-14T00:00:00.000Z"), ado.iterations)

        links = import_links(ado, self.cfg, log=self.q)
        self.assertEqual(links["added"], 2)
        self.assertEqual(links["skipped_outside_scope"], ["DEMO-1 -> OTHER-9"])
        self.assertIn({"rel": "System.LinkTypes.Dependency-Forward", "url": ado.item_url(102)}, story["relations"])
        self.assertIn({"rel": "System.LinkTypes.Hierarchy-Reverse", "url": ado.item_url(101)}, ado.items[102]["relations"])

        report = os.path.join(self.dir, "report.csv")
        total, bad = verify(ado, self.cfg, USERS, report, log=self.q)
        self.assertEqual((total, bad), (2, 0))

        # Running everything again must change nothing.
        before = {i: it["rev"] for i, it in ado.items.items()}
        again = import_items(ado, self.cfg, USERS, log=self.q)
        import_links(ado, self.cfg, log=self.q)
        self.assertEqual((again["created"], again["revisions"], again["complete"]), (0, 0, 2))
        self.assertEqual(before, {i: it["rev"] for i, it in ado.items.items()})
        self.assertEqual(len(ado.uploads), 1)

    def test_resume_after_failure_has_no_duplicates(self):
        ado = sample.FakeAdo(fail_after=3)
        stats = import_items(ado, self.cfg, USERS, log=self.q)
        self.assertEqual(stats["failed"], ["DEMO-1", "DEMO-2"])
        self.assertEqual(ado.items[101]["rev"], 3)
        ado.fail_after = None
        stats = import_items(ado, self.cfg, USERS, log=self.q)
        self.assertEqual((stats["created"], stats["resumed"]), (1, 1))
        self.assertEqual(ado.items[101]["rev"], 7)
        self.assertEqual(len(ado.items), 2)
        self.assertEqual(len(ado.items[101]["comments"]), 3)
        _, bad = verify(ado, self.cfg, USERS, os.path.join(self.dir, "r.csv"), log=self.q)
        self.assertEqual(bad, 2)  # links not run yet, verify must say so for both items
        import_links(ado, self.cfg, log=self.q)
        _, bad = verify(ado, self.cfg, USERS, os.path.join(self.dir, "r.csv"), log=self.q)
        self.assertEqual(bad, 0)

    def test_same_timestamp_revisions_get_increasing_dates(self):
        ado = sample.FakeAdo()  # FakeAdo asserts ChangedDate strictly increases
        import_items(ado, self.cfg, USERS, log=self.q)
        dates = [o["value"] for ops in ado.items[101]["log"] for o in ops if o["path"] == "/fields/System.ChangedDate"]
        self.assertEqual(len(set(dates)), len(dates))


class AdoClientTests(unittest.TestCase):
    def test_rejected_token_gives_a_clear_message(self):
        from jira2ado.ado_client import AdoClient, AdoError

        class Resp:
            status_code, headers, text = 203, {"Content-Type": "text/html; charset=utf-8"}, "\r\n\r\n<!DOCTYPE html>"

        class Session:
            headers = {}
            def request(self, *a, **k):
                return Resp()

        client = AdoClient("https://dev.azure.com/org", "Proj", "bad-token", session=Session())
        with self.assertRaises(AdoError) as ctx:
            client.states("Product Backlog Item")
        self.assertIn("did not accept the access token", str(ctx.exception))
        self.assertIn("pasted once", str(ctx.exception))


class AdoRefusalTests(unittest.TestCase):
    def _client(self, status, ctype, body):
        from jira2ado.ado_client import AdoClient

        class Resp:
            status_code, headers, text = status, {"Content-Type": ctype}, body
            def json(self):
                import json
                return json.loads(body)

        class Session:
            headers = {}
            def request(self, *a, **k):
                return Resp()

        return AdoClient("https://dev.azure.com/contoso", "Proj", "tok", session=Session())

    def test_401_names_the_token(self):
        from jira2ado.ado_client import AdoError
        c = self._client(401, "application/json", '{"message": "TF400813: not authorized"}')
        with self.assertRaises(AdoError) as ctx:
            c.states("Bug")
        self.assertIn("rejected the access token", str(ctx.exception))
        self.assertIn("TF400813", str(ctx.exception))
        self.assertIn("contoso organization", str(ctx.exception))

    def test_403_names_a_missing_permission_not_the_token(self):
        from jira2ado.ado_client import AdoError
        c = self._client(403, "application/json", '{"message": "VS403474: no permission to bypass rules"}')
        with self.assertRaises(AdoError) as ctx:
            c.states("Bug")
        self.assertIn("VS403474", str(ctx.exception))
        self.assertIn("token was accepted", str(ctx.exception))
        self.assertNotIn("pasted once", str(ctx.exception))


class ProjectNameTests(unittest.TestCase):
    def test_name_comes_from_the_root_iteration_and_a_missing_project_is_named(self):
        from jira2ado.ado_client import AdoClient, AdoError
        calls = []

        class Resp:
            def __init__(self, status, body):
                self.status_code, self.headers, self.text, self._b = status, {"Content-Type": "application/json"}, "", body
            def json(self):
                return self._b

        class Session:
            headers = {}
            def request(self, method, url, **k):
                calls.append(url)
                if "Nope" in url:
                    return Resp(404, {"message": "TF200016: The following project does not exist: Nope"})
                return Resp(200, {"name": "Sandbox", "structureType": "iteration"})

        c = AdoClient("https://dev.azure.com/contoso", "sandbox", "t", session=Session())
        self.assertEqual(c.project_name(), "Sandbox")
        self.assertIn("/_apis/wit/classificationnodes/Iterations", calls[0])
        with self.assertRaises(AdoError) as ctx:
            AdoClient("https://dev.azure.com/contoso", "Nope", "t", session=Session()).project_name()
        self.assertIn("project 'Nope' was not found in the contoso organization", str(ctx.exception))


class AccessCommandTests(unittest.TestCase):
    def _run(self, jira_ok, ado_ok):
        import io
        from contextlib import redirect_stdout
        from jira2ado import cli
        from jira2ado.ado_client import AdoError

        class J:
            def myself(self):
                if not jira_ok:
                    raise RuntimeError("Jira GET /rest/api/3/myself failed: 401")
                return "Test User"

            def search_keys(self, jql):
                yield from ["DEMO-1", "DEMO-2"]

        class A:
            def check_access(self):
                if not ado_ok:
                    raise AdoError("Azure DevOps did not accept the access token")
                return "Test User"

        saved = cli._jira, cli._ado
        cli._jira, cli._ado = (lambda cfg: J()), (lambda cfg: A())
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                cli.cmd_access({"ado": {"project": "Sandbox"}, "jira": {"jql": "project = DEMO"}}, None)
        finally:
            cli._jira, cli._ado = saved
        return buf.getvalue()

    def test_both_tokens_work(self):
        out = self._run(True, True)
        self.assertIn("Jira: signed in as Test User", out)
        self.assertIn("2 ticket(s) match the query: DEMO-1, DEMO-2", out)
        self.assertIn("reached project 'Sandbox', signed in as Test User", out)

    def test_a_bad_token_stops_before_export(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(True, False)
        self.assertIn("Nothing was exported or written", str(ctx.exception.code))
        self.assertIn("did not accept the access token", str(ctx.exception.code))


class ScopeTests(unittest.TestCase):
    """Current-values mode and custom field mapping options."""

    def setUp(self):
        self.cfg = cfg_for("x")
        m = self.cfg["mapping"]
        m["history"] = False
        m["custom_fields"] = [
            {"ado": "Custom.BusinessAnalysts", "jira": ["Business Analyst"], "kind": "user"},
            {"ado": "Custom.Developers", "jira": ["Developer(s)", "Developer"], "kind": "users"},
            {"ado": "Custom.ChangeRequest", "jira": ["Change Request #"], "kind": "text"},
            {"ado": "Custom.ReleaseNotes", "jira": ["Release Notes"], "kind": "html"},
        ]
        m["resolution_states"] = {"default": {"Won't Do": "Removed"}}
        m["type_tags"] = {"Story": "Jira Story"}
        m["iteration_parent"] = "Team A"
        m["skip_future_sprints"] = True
        m["jira_key_field"] = "Custom.JiraKey"
        m["tags"]["jira_key"] = False
        self.meta = dict(sample.META, field_names={
            "customfield_20001": "Business Analyst", "customfield_20002": "Developer(s)", "customfield_20003": "Developer",
            "customfield_20004": "Change Request #", "customfield_20005": "Release Notes"})

    def story(self, **extra):
        issue = sample.story()
        issue["fields"].update({"customfield_20001": sample.U1, "customfield_20002": [sample.U1, sample.U2],
                                "customfield_20004": "CHG-77", "customfield_20005": "*bold* note",
                                "resolution": {"name": "Done"}, "resolutiondate": "2024-01-05T09:00:00.000-0500"})
        issue["renderedFields"]["customfield_20005"] = "<p><b>bold</b> note</p>"
        issue["fields"]["customfield_10020"][0]["state"] = "closed"
        issue["fields"]["customfield_10020"][1].update(state="closed", startDate="2024-01-15T00:00:00.000Z",
                                                       endDate="2024-01-28T00:00:00.000Z")
        issue["fields"].update(extra)
        return issue

    def test_current_values_then_comments_and_attachments(self):
        p = Planner(self.cfg, self.meta, USERS)
        revs = p.plan(self.story())["revisions"]
        self.assertEqual(len(revs), 4)  # create, 2 comments, 1 attachment group
        f = revs[0]["fields"]
        self.assertEqual(f["System.Title"], "Checkout fails on retry")
        self.assertEqual(f["System.State"], "Done")
        self.assertEqual(f["Microsoft.VSTS.Common.ClosedDate"], "2024-01-05T14:00:00.000Z")
        self.assertEqual(f["Microsoft.VSTS.Scheduling.Effort"], 5.0)
        self.assertEqual(f["System.IterationPath"], "Shop\\Team A\\Sprint 2-A")
        self.assertEqual(f["System.Tags"], "Jira Story; checkout; component:Cart; version:1.2")
        self.assertEqual(f["Custom.JiraKey"], "DEMO-1")
        self.assertEqual(f["Custom.BusinessAnalysts"], "ana@contoso.com")
        self.assertEqual(f["Custom.Developers"], "ana@contoso.com")
        self.assertEqual(f["Custom.ChangeRequest"], "CHG-77")
        self.assertEqual(f["Custom.ReleaseNotes"], "<p><b>bold</b> note</p>")
        self.assertIn("Developers also: Raj Rao", revs[0]["comment"])
        self.assertEqual([bool(r["comment"]) for r in revs[1:]], [False, True, True])  # in time order
        self.assertEqual(revs[1]["attachments"][0]["id"], "900")

    def test_resolution_maps_to_removed(self):
        revs = Planner(self.cfg, self.meta, USERS).plan(self.story(resolution={"name": "Won't Do"}))["revisions"]
        self.assertEqual(revs[0]["fields"]["System.State"], "Removed")
        self.assertNotIn("Microsoft.VSTS.Common.ClosedDate", revs[0]["fields"])

    def test_future_sprint_stays_on_backlog(self):
        issue = self.story()
        issue["fields"]["customfield_10020"][1]["state"] = "future"
        revs = Planner(self.cfg, self.meta, USERS).plan(issue)["revisions"]
        self.assertEqual(revs[0]["fields"]["System.IterationPath"], "Shop\\Team A")

    def test_current_sprint_is_the_active_one_not_the_last_listed(self):
        issue = self.story()
        issue["fields"]["customfield_10020"] = [
            {"id": 3, "name": "Sprint 3", "state": "active", "startDate": "2024-02-01T00:00:00.000Z"},
            {"id": 1, "name": "Sprint 1", "state": "closed", "endDate": "2024-01-14T00:00:00.000Z"}]
        revs = Planner(self.cfg, self.meta, USERS).plan(issue)["revisions"]
        self.assertEqual(revs[0]["fields"]["System.IterationPath"], "Shop\\Team A\\Sprint 3")
        issue["fields"]["customfield_10020"] = [
            {"id": 2, "name": "Sprint 2", "state": "closed", "endDate": "2024-01-28T00:00:00.000Z"},
            {"id": 1, "name": "Sprint 1", "state": "closed", "endDate": "2024-01-14T00:00:00.000Z"}]
        revs = Planner(self.cfg, self.meta, USERS).plan(issue)["revisions"]
        self.assertEqual(revs[0]["fields"]["System.IterationPath"], "Shop\\Team A\\Sprint 2")

    def test_first_points_field_wins(self):
        meta = dict(self.meta, points_fields=["customfield_10016", "customfield_20009"])
        revs = Planner(self.cfg, meta, USERS).plan(self.story(customfield_20009=8))["revisions"]
        self.assertEqual(revs[0]["fields"]["Microsoft.VSTS.Scheduling.Effort"], 5.0)

    def test_import_checks_fields_per_type(self):
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        from jira2ado.util import write_json
        write_json(os.path.join(folder, "meta.json"), self.meta)
        write_json(os.path.join(folder, "issues", "DEMO-1.json"), dict(self.story(), comments=sample.story()["comments"]))
        self.cfg["paths"]["export_dir"] = folder
        self.cfg["ado"].update(area_path="Shop\\Migration Test", create_area=True)
        self.cfg["mapping"]["link_mode"] = "parent"
        ado = sample.FakeAdo()
        pbi = {"Microsoft.VSTS.Common.ValueArea", "Microsoft.VSTS.Common.Priority", "Microsoft.VSTS.Scheduling.Effort",
               "Custom.BusinessAnalysts", "Custom.Developers", "Custom.ChangeRequest", "Custom.ReleaseNotes",
               "Microsoft.VSTS.Common.ClosedDate"}
        ado.type_field_map = {"Product Backlog Item": pbi, "Task": {"Microsoft.VSTS.Common.Priority"}}
        q = lambda *a, **k: None
        stats = import_items(ado, self.cfg, USERS, log=q)
        self.assertEqual(stats["failed"], [])
        self.assertEqual(ado.areas, ["Migration Test"])
        self.assertEqual(ado.iteration_parents, ["Team A"])  # current-values mode: only the sprint in use
        story, task = ado.items[101], ado.items[102]
        self.assertNotIn("Custom.JiraKey", story["fields"])            # not on this type ...
        self.assertTrue(story["fields"]["System.Tags"].endswith("DEMO-1"))  # ... so it became a tag
        self.assertEqual(story["fields"]["Custom.ChangeRequest"], "CHG-77")
        self.assertEqual(story["fields"]["System.AreaPath"], "Shop\\Migration Test")
        self.assertNotIn("Microsoft.VSTS.Common.ValueArea", task["fields"])
        self.assertFalse(any("Custom.JiraKey" in w for w in stats["warnings"]))  # the key fallback is silent
        links = import_links(ado, self.cfg, log=q)
        self.assertEqual(links["added"], 1)  # parent only; the Blocks link is not migrated in this mode
        self.assertIn({"rel": "System.LinkTypes.Hierarchy-Reverse", "url": ado.item_url(101)}, task["relations"])

    def _export(self):
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        from jira2ado.util import write_json
        write_json(os.path.join(folder, "meta.json"), self.meta)
        write_json(os.path.join(folder, "issues", "DEMO-1.json"), dict(self.story(), comments=sample.story()["comments"]))
        self.cfg["paths"]["export_dir"] = folder
        return folder

    def test_fields_the_target_lacks_go_into_a_note(self):
        self._export()
        self.cfg["mapping"]["note_jira_status"] = True
        self.cfg["mapping"]["custom_fields"][2]["label"] = "Change Request #"
        ado = sample.FakeAdo()
        ado.type_field_map = {"Product Backlog Item": {"Microsoft.VSTS.Common.ValueArea", "Microsoft.VSTS.Common.Priority",
                                                       "Microsoft.VSTS.Scheduling.Effort"}, "Task": set()}
        stats = import_items(ado, self.cfg, USERS, log=lambda *a, **k: None)
        self.assertEqual(stats["failed"], [])
        story = ado.items[101]
        note = story["comments"][0][1]
        self.assertIn("Jira status: Done (Done)", note)
        self.assertIn("<b>Change Request #:</b> CHG-77", note)
        self.assertIn("<b>ReleaseNotes:</b> <p><b>bold</b> note</p>", note)
        self.assertIn("<b>BusinessAnalysts:</b> ana@contoso.com", note)
        self.assertNotIn("Custom.ChangeRequest", story["fields"])
        self.assertIn("DEMO-1", story["fields"]["System.Tags"])

    def test_unknown_state_stops_before_any_write(self):
        self._export()
        ado = sample.FakeAdo()
        ado.type_states = {"Product Backlog Item": ["New", "Committed"], "Task": ["To Do", "Done"]}
        with self.assertRaises(SystemExit) as ctx:
            import_items(ado, self.cfg, USERS, log=lambda *a, **k: None)
        self.assertIn("Product Backlog Item: 'Done'", str(ctx.exception))
        self.assertEqual(ado.items, {})
        self.assertEqual(ado.uploads, [])

    def test_users_file_rewrites_domains(self):
        from jira2ado.exporter import write_users_file
        import csv
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        path = os.path.join(folder, "users.csv")
        write_users_file(folder, path, {"example.com": "contoso.com"})
        with open(path, newline="") as fh:
            rows = {r["display_name"]: r["ado_identity"] for r in csv.DictReader(fh)}
        self.assertEqual(rows["Ana Lee"], "ana@contoso.com")
        self.assertEqual(rows["Raj Rao"], "")

    def test_users_file_keeps_a_blank_left_on_purpose(self):
        from jira2ado.exporter import write_users_file
        import csv
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        path = os.path.join(folder, "users.csv")
        with open(path, "w") as fh:
            fh.write("account_id,display_name,jira_email,active,ado_identity\nacc-1,Ana Lee,ana@example.com,True,\n")
        total, blank = write_users_file(folder, path, {"example.com": "contoso.com"})
        with open(path, newline="") as fh:
            rows = {r["display_name"]: r["ado_identity"] for r in csv.DictReader(fh)}
        self.assertEqual(rows["Ana Lee"], "")  # not re-guessed
        self.assertEqual((total, blank), (2, 2))

    def test_users_file_keeps_people_from_earlier_runs(self):
        from jira2ado.exporter import write_users_file
        import csv
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        path = os.path.join(folder, "users.csv")
        with open(path, "w") as fh:
            fh.write("account_id,display_name,jira_email,active,ado_identity\n"
                     "acc-old,Old Sprint Person,old@example.com,True,old@contoso.com\n")
        total, blank = write_users_file(folder, path, {"example.com": "contoso.com"})
        with open(path, newline="") as fh:
            rows = {r["display_name"]: r["ado_identity"] for r in csv.DictReader(fh)}
        self.assertEqual(rows["Old Sprint Person"], "old@contoso.com")   # not in this export, still kept
        self.assertEqual(total, 2)                                       # counts are for this run's people


SPRINTS = [
    {"id": 101, "name": "Platform&Core Sprint 27", "state": "closed", "startDate": "2026-10-14T00:00:00.000Z",
     "endDate": "2026-10-28T00:00:00.000Z"},
    {"id": 102, "name": "Platform&Core Sprint 28", "state": "active", "startDate": "2026-10-28T00:00:00.000Z",
     "endDate": "2026-11-11T00:00:00.000Z"},
    {"id": 201, "name": "Checkout Sprint 28", "state": "active", "startDate": "2026-10-28T00:00:00.000Z"},
]


class SprintLookupTests(unittest.TestCase):
    def test_names_match_the_way_ado_spells_them(self):
        from jira2ado.cli import resolve_sprint
        self.assertEqual(resolve_sprint("Platform-Core Sprint 28", SPRINTS, "PROJ")["id"], 102)
        self.assertEqual(resolve_sprint("platform&core sprint 28", SPRINTS, "PROJ")["id"], 102)
        self.assertEqual(resolve_sprint("101", SPRINTS, "PROJ")["id"], 101)
        self.assertEqual(resolve_sprint("Core Sprint 27", SPRINTS, "PROJ")["id"], 101)  # one partial match is fine

    def test_unclear_or_unknown_names_stop_with_choices(self):
        from jira2ado.cli import resolve_sprint
        with self.assertRaises(SystemExit) as ctx:
            resolve_sprint("Sprint 28", SPRINTS, "PROJ")
        self.assertIn("more than one sprint", str(ctx.exception))
        self.assertIn("number 102", str(ctx.exception))
        with self.assertRaises(SystemExit) as ctx:
            resolve_sprint("Platform-Core Sprint 99", SPRINTS, "PROJ")
        self.assertIn("No sprint called 'Platform-Core Sprint 99'", str(ctx.exception))
        self.assertIn("Platform&Core Sprint 28 (number 102, active)", str(ctx.exception))


class PrepareTests(unittest.TestCase):
    """run.sh --from/--to/--sprint writes a config through `prepare`."""

    def setUp(self):
        import shutil
        self.home = os.getcwd()
        self.dir = tempfile.mkdtemp()
        kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        os.makedirs(os.path.join(self.dir, "configs"))
        for name in ("enterprise-development.yaml", "targets.yaml"):
            shutil.copy(os.path.join(kit, "configs", name), os.path.join(self.dir, "configs", name))
        os.chdir(self.dir)
        os.environ.update(
            JIRA_BASE_URL="https://demo.atlassian.net",
            JIRA_EMAIL="me@example.com",
            JIRA_API_TOKEN="j",
            ADO_ORG_URL="https://dev.azure.com/contoso",
            ADO_PAT="a",
        )
        import jira2ado.ado_client as ac
        import jira2ado.jira_client as jc
        self.saved = ac.AdoClient, jc.JiraClient

        class FakeJira:
            def __init__(self, *a, **k):
                pass

            def project_sprints(self, project, board_id=None):
                return SPRINTS

        class FakeAdoClient:
            def __init__(self, org, project, pat, *a, **k):
                self.project = project

            def project_name(self):
                return {"sandbox": "Sandbox", "production": "Production"}.get(
                    self.project.lower(), self.project)

        ac.AdoClient, jc.JiraClient = FakeAdoClient, FakeJira

    def tearDown(self):
        import jira2ado.ado_client as ac
        import jira2ado.jira_client as jc
        ac.AdoClient, jc.JiraClient = self.saved
        os.chdir(self.home)

    def _prepare(self, *args):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from jira2ado import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli.main(["prepare", *args])
        path = out.getvalue().strip()
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)  # run.sh reads only the path from stdout
        return path, config.load(path), err.getvalue()

    def test_sandbox_run(self):
        path, cfg, notes = self._prepare("--from", "proj", "--to", "sandbox",
                                         "--sprint", "Platform-Core Sprint 28")
        self.assertEqual(path, os.path.join("runs", "proj", "platform-core-sprint-28", "to-sandbox", "config.yaml"))
        self.assertEqual(cfg["jira"]["jql"], 'project = "PROJ" AND sprint in (102) AND statusCategory != Done ORDER BY key ASC')
        self.assertEqual(cfg["ado"]["project"], "Sandbox")  # spelled as ADO spells it
        self.assertIsNone(cfg["ado"]["area_path"])
        self.assertFalse(cfg["ado"]["confirm_with_project_name"])
        self.assertEqual(cfg["paths"]["export_dir"], os.path.join("runs", "proj", "platform-core-sprint-28", "export"))
        self.assertEqual(cfg["paths"]["users_file"], "users.csv")
        self.assertEqual(cfg["mapping"]["only_sprints"], ["Platform&Core Sprint 28"])
        self.assertFalse(cfg["mapping"]["skip_future_sprints"])
        self.assertEqual(cfg["mapping"]["points_field"]["default"], "Microsoft.VSTS.Scheduling.Effort")
        self.assertIn("Platform&Core Sprint 28 (number 102, active, 2026-10-28 to 2026-11-11)", notes)

    def test_two_sprints_an_iteration_and_done_tickets(self):
        path, cfg, _ = self._prepare("--from", "PROJ", "--to", "Production", "--sprint", "101",
                                     "--sprint", "Platform-Core Sprint 28", "--iteration", "Platform-Core Sprint 28", "--include-done")
        self.assertIn("platform-core-sprint-27+platform-core-sprint-28", path)
        self.assertEqual(cfg["jira"]["jql"], 'project = "PROJ" AND sprint in (101, 102) ORDER BY key ASC')
        self.assertTrue(cfg["ado"]["confirm_with_project_name"])
        self.assertEqual(cfg["mapping"]["iteration_override"], "Platform-Core Sprint 28")

    def test_a_project_with_no_settings_uses_the_project_root(self):
        _, cfg, notes = self._prepare("--from", "PROJ", "--to", "Other Project", "--sprint", "102")
        self.assertIsNone(cfg["ado"]["area_path"])
        self.assertIsNone(cfg["mapping"]["iteration_parent"])
        self.assertTrue(cfg["ado"]["confirm_with_project_name"])
        self.assertIn("has no section for 'Other Project'", notes)

    def test_show_prints_values_for_run_sh(self):
        import io
        from contextlib import redirect_stdout
        from jira2ado import cli
        path, _, _ = self._prepare("--from", "PROJ", "--to", "Sandbox", "--sprint", "102")
        for key, want in (("ado.project", "Sandbox"), ("ado.area_path", ""),
                          ("ado.confirm_with_project_name", "false")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["-c", path, "show", key])
            self.assertEqual(buf.getvalue().strip(), want)


class PlacementTests(unittest.TestCase):
    """Where items land when run.sh is given sprints and, optionally, one iteration."""

    def setUp(self):
        self.cfg = cfg_for("x")
        self.cfg["mapping"]["history"] = False
        self.issue = sample.story()
        self.issue["fields"]["customfield_10020"] = [
            {"id": 102, "name": "Platform&Core Sprint 28", "state": "active", "startDate": "2026-10-28T00:00:00.000Z",
             "endDate": "2026-11-11T00:00:00.000Z"},
            {"id": 101, "name": "Platform&Core Sprint 27", "state": "closed", "startDate": "2026-10-14T00:00:00.000Z",
             "endDate": "2026-10-28T00:00:00.000Z"}]

    def path(self):
        return Planner(self.cfg, sample.META, USERS).plan(self.issue)["revisions"][0]["fields"]["System.IterationPath"]

    def test_the_requested_sprint_wins_over_the_current_one(self):
        self.assertEqual(self.path(), "Shop\\Platform-Core Sprint 28")
        self.cfg["mapping"]["only_sprints"] = ["Platform&Core Sprint 27"]
        self.assertEqual(self.path(), "Shop\\Platform-Core Sprint 27")

    def test_one_iteration_for_everything(self):
        m = self.cfg["mapping"]
        m["iteration_override"] = "Migration Test"
        self.assertEqual(self.path(), "Shop\\Migration Test")
        m["iteration_parent"] = "Team A"
        self.assertEqual(self.path(), "Shop\\Team A\\Migration Test")       # under the team's folder
        m["iteration_override"] = "Shop\\Elsewhere/Sprint 9"
        self.assertEqual(self.path(), "Shop\\Elsewhere\\Sprint 9")         # a full path is used as given
        m["iteration_override"] = "Shop"
        self.assertEqual(self.path(), "Shop")

    def test_import_creates_only_the_chosen_iteration_with_its_sprint_dates(self):
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        from jira2ado.util import write_json
        write_json(os.path.join(folder, "issues", "DEMO-1.json"), self.issue)
        self.cfg["paths"]["export_dir"] = folder
        self.cfg["mapping"]["iteration_override"] = "Platform-Core Sprint 28"
        ado = sample.FakeAdo()
        stats = import_items(ado, self.cfg, USERS, log=lambda *a, **k: None)
        self.assertEqual(stats["failed"], [])
        self.assertEqual(ado.iterations, [("Platform-Core Sprint 28", "2026-10-28T00:00:00.000Z", "2026-11-11T00:00:00.000Z")])
        self.assertEqual({it["fields"]["System.IterationPath"] for it in ado.items.values()}, {"Shop\\Platform-Core Sprint 28"})


class TargetSafetyTests(unittest.TestCase):
    """Each target project keeps its own import log, and items already there are not created twice."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        sample.write_export(self.dir)
        self.cfg = cfg_for(self.dir)
        self.q = lambda *a, **k: None

    def test_logs_are_kept_per_target(self):
        self.cfg["paths"]["state_dir"] = os.path.join(self.dir, "to-shop")
        import_items(sample.FakeAdo(), self.cfg, USERS, log=self.q)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "to-shop", "import-state.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "import-state.jsonl")))
        other = copy.deepcopy(self.cfg)
        other["ado"]["project"] = "Other"
        other["paths"]["state_dir"] = os.path.join(self.dir, "to-other")
        ado2 = sample.FakeAdo()
        stats = import_items(ado2, other, USERS, log=self.q)
        self.assertEqual(stats["created"], 2)  # the first target's log does not stop this one
        self.assertEqual(len(ado2.items), 2)

    def test_items_already_in_the_target_are_skipped(self):
        ado = sample.FakeAdo()
        import_items(ado, self.cfg, USERS, log=self.q)        # first run, from one folder
        self.assertEqual(len(ado.items), 2)
        fresh = copy.deepcopy(self.cfg)
        fresh["paths"]["state_dir"] = tempfile.mkdtemp()   # second run, new folder, no log
        before = {i: it["rev"] for i, it in ado.items.items()}
        stats = import_items(ado, fresh, USERS, log=self.q)
        self.assertEqual(stats["created"], 0)
        self.assertEqual(sorted(stats["existing"]), ["DEMO-1", "DEMO-2"])
        self.assertEqual(before, {i: it["rev"] for i, it in ado.items.items()})
        self.assertEqual(import_links(ado, fresh, log=self.q)["added"], 2)  # found items still get their links
        _, bad = verify(ado, fresh, USERS, os.path.join(self.dir, "v.csv"), log=self.q)
        self.assertEqual(bad, 0)
        with open(os.path.join(self.dir, "v.csv")) as fh:
            self.assertEqual(fh.read().count("already there before this run"), 2)

    def test_differences_on_items_found_by_key_are_notes(self):
        ado = sample.FakeAdo()
        ado.create_item("Product Backlog Item", [{"op": "add", "path": "/fields/System.Title", "value": "Old copy"},
                                                 {"op": "add", "path": "/fields/System.Tags", "value": "DEMO-1"}])
        import_items(ado, self.cfg, USERS, log=self.q)
        _, bad = verify(ado, self.cfg, USERS, os.path.join(self.dir, "v.csv"), log=self.q)
        with open(os.path.join(self.dir, "v.csv")) as fh:
            report = fh.read()
        self.assertIn("already there before this run: title differs", report)  # DEMO-1: a note, not a failure
        self.assertEqual(bad, 1)
        self.assertIn("links missing", report)       # DEMO-2: its parent link is added by the links step
        import_links(ado, self.cfg, log=self.q)
        self.assertEqual(verify(ado, self.cfg, USERS, os.path.join(self.dir, "v.csv"), log=self.q)[1], 0)

    def test_the_key_field_is_searched_when_the_target_has_it(self):
        ado = sample.FakeAdo()
        ado.type_field_map = {"Product Backlog Item": {"Custom.JiraKey"}, "Task": set()}
        self.cfg["mapping"]["jira_key_field"] = "Custom.JiraKey"
        import_items(ado, self.cfg, USERS, log=self.q)
        self.assertEqual(ado.key_searches[0][1], "Custom.JiraKey")


class ExportScopeTests(unittest.TestCase):
    def test_only_the_latest_query_is_loaded(self):
        from jira2ado.exporter import load_issues
        from jira2ado.util import write_json
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        self.assertEqual([i["key"] for i in load_issues(folder)], ["DEMO-1", "DEMO-2"])
        write_json(os.path.join(folder, "keys.json"), ["DEMO-2", "DEMO-9"])  # DEMO-1 left the sprint; DEMO-9 failed
        self.assertEqual([i["key"] for i in load_issues(folder)], ["DEMO-2"])



class DuplicateMatchTests(unittest.TestCase):
    def item(self, i, title, tags="", **extra):
        return {"id": i, "fields": {"System.Title": title, "System.Tags": tags, "System.State": "Committed", **extra}}

    def test_kinds_of_match(self):
        from jira2ado.duplicates import KEY_IN_TITLE, SAME_KEY, SAME_TITLE, classify
        entries = [("PROJ-3637", "Fix  the Banner"), ("PROJ-3", "Other")]
        items = [self.item(1, "Anything", "NAV; proj-3637"),
                 self.item(2, "Anything", "", **{"Custom.JiraKey": "PROJ-3637"}),
                 self.item(3, "[PROJ-3637] copied by hand"),
                 self.item(4, "fix the banner"),
                 self.item(5, "PROJ-36370 is a different ticket"),
                 self.item(6, "Follow-up to PROJ-3 work")]
        found = classify(entries, items, "Custom.JiraKey")
        self.assertEqual([(m["id"], m["kind"]) for m in found["PROJ-3637"]],
                         [(1, SAME_KEY), (2, SAME_KEY), (3, KEY_IN_TITLE), (4, SAME_TITLE)])
        self.assertEqual([(m["id"], m["kind"]) for m in found["PROJ-3"]], [(6, KEY_IN_TITLE)])
        self.assertNotIn(4, [m["id"] for m in classify(entries, items, titles=False).get("PROJ-3637", [])])

    def test_descriptions_and_groups(self):
        from jira2ado.duplicates import classify, describe, same_summary_groups
        m = classify([("PROJ-9", "Release prep")], [self.item(17990, "Release Prep", "PROJ-8")])["PROJ-9"][0]
        self.assertEqual(describe(m), "same title as #17990 (Committed, from PROJ-8)")
        self.assertEqual(same_summary_groups([("A-1", "Release prep"), ("A-2", "release  PREP "), ("A-3", "Other")]),
                         [["A-1", "A-2"]])


class FindDuplicatesQueryTests(unittest.TestCase):
    def test_queries_sent_to_azure_devops(self):
        import json as _json
        from jira2ado.ado_client import AdoClient
        sent = []

        class Resp:
            def __init__(self, body):
                self.status_code, self.headers, self.text, self._b = 200, {"Content-Type": "application/json"}, "", body
            def json(self):
                return self._b

        class Session:
            headers = {}
            def request(self, method, url, **k):
                if url.endswith("/wiql"):
                    sent.append(k["json"]["query"])
                    return Resp({"workItems": [{"id": 7}]})
                return Resp({"value": [{"id": 7, "fields": {"System.Title": "It's  here", "System.Tags": "",
                                                            "System.State": "New"}}, None]})

        c = AdoClient("https://dev.azure.com/org", "Proj", "t", session=Session())
        found = c.find_duplicates([("PROJ-1", "It's  here")], "Custom.JiraKey")
        q = sent[0]
        self.assertIn("[System.TeamProject] = @project", q)
        self.assertIn("[System.Tags] CONTAINS 'PROJ-1'", q)
        self.assertIn("[System.Title] CONTAINS 'PROJ-1'", q)
        self.assertIn("[Custom.JiraKey] = 'PROJ-1'", q)
        self.assertIn("[System.Title] = 'It''s  here'", q)   # quotes doubled
        self.assertIn("[System.Title] = 'It''s here'", q)    # and the version with single spaces
        self.assertEqual(found["PROJ-1"][0]["id"], 7)
        sent.clear()
        c.find_duplicates([(f"PROJ-{i}", "x" * 250) for i in range(300)])
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(q) < 32000 for q in sent))


class PossibleDuplicateImportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        sample.write_export(self.dir)
        self.cfg = cfg_for(self.dir)
        self.q = lambda *a, **k: None
        self.ado = sample.FakeAdo()
        # Someone already copied DEMO-1 into Azure DevOps by hand, with no Jira key on it.
        self.ado.create_item("Product Backlog Item", [{"op": "add", "path": "/fields/System.Title",
                                                       "value": "checkout fails on  retry"}])
        self.manual = max(self.ado.items)

    def test_left_out_by_default_then_created_and_tagged_when_asked(self):
        stats = import_items(self.ado, self.cfg, USERS, log=self.q)
        self.assertEqual(stats["held"], ["DEMO-1"])
        self.assertEqual(stats["created"], 1)                       # DEMO-2 only
        titles = [it["fields"].get("System.Title") for it in self.ado.items.values()]
        self.assertEqual(titles.count("Checkout fails on retry"), 0)
        _, bad = verify(self.ado, self.cfg, USERS, os.path.join(self.dir, "v.csv"), log=self.q)
        with open(os.path.join(self.dir, "v.csv")) as fh:
            self.assertIn("left out, possible duplicate: same title as #", fh.read())
        self.assertEqual(bad, 0)

        stats = import_items(self.ado, self.cfg, USERS, log=self.q, possible_duplicates="create")
        self.assertEqual(stats["created"], 1)                       # now DEMO-1
        new = [it for it in self.ado.items.values() if it["fields"].get("System.Title") == "Checkout fails on retry"][0]
        self.assertIn("Possible duplicate", new["fields"]["System.Tags"])
        self.assertIn({"rel": "System.LinkTypes.Related", "url": self.ado.item_url(self.manual),
                       "attributes": {"comment": "Possible duplicate: same title"}}, new["relations"])

    def test_a_key_in_a_title_is_a_possible_duplicate(self):
        self.ado.items[self.manual]["fields"]["System.Title"] = "DEMO-2 - copied"
        stats = import_items(self.ado, self.cfg, USERS, log=self.q)
        self.assertEqual(stats["held"], ["DEMO-2"])
        self.assertEqual(stats["possible"]["DEMO-2"][0]["kind"], "Jira key in the title")

    def test_titles_can_be_left_out_of_the_check(self):
        self.cfg["mapping"]["duplicate_check_titles"] = False
        stats = import_items(self.ado, self.cfg, USERS, log=self.q)
        self.assertEqual(stats["held"], [])
        self.assertEqual(stats["created"], 2)

    def test_preview_report_and_count(self):
        import io
        from contextlib import redirect_stdout
        from jira2ado import cli
        from jira2ado.planner import Planner
        from jira2ado.util import Journal
        from jira2ado.importer import journal_path
        planner = Planner(self.cfg, sample.META, USERS)
        from jira2ado.exporter import load_issues
        plans = [planner.plan(i) for i in load_issues(self.dir)]
        plans.append({**plans[1], "key": "DEMO-3"})               # a second ticket with DEMO-2's summary
        buf = io.StringIO()
        with redirect_stdout(buf):
            found, possible = cli._duplicate_report(self.cfg, plans, Journal(journal_path(self.cfg), readonly=True), self.ado)
            cli.cmd_duplicates(self.cfg, None)
        out = buf.getvalue()
        self.assertEqual(list(possible), ["DEMO-1"])
        self.assertIn("1 possible duplicate(s):", out)
        self.assertIn("DEMO-2, DEMO-3: Add retry test", out)
        self.assertEqual(out.strip().splitlines()[-1], "1")         # what run.sh reads
        with open(os.path.join(self.dir, "duplicates.csv")) as fh:
            rows = fh.read().splitlines()
        self.assertTrue(rows[1].startswith("possible,DEMO-1,Checkout fails on retry,same title,"))
        self.assertTrue(any(r.startswith("same summary in run,DEMO-3") for r in rows))



class IterationCreateTests(unittest.TestCase):
    def test_existing_iterations_are_not_reported_as_created(self):
        from jira2ado.ado_client import AdoClient
        posts = []

        class Resp:
            def __init__(self, status):
                self.status_code, self.headers, self.text = status, {"Content-Type": "application/json"}, ""
            def json(self):
                return {}

        class Session:
            headers = {}
            def __init__(self, exists):
                self.exists = exists
            def get(self, url, **k):
                return Resp(200 if self.exists else 404)
            def request(self, method, url, **k):
                posts.append(url)
                return Resp(200)

        self.assertFalse(AdoClient("https://dev.azure.com/o", "P", "t", session=Session(True)).create_iteration("Sprint 1"))
        self.assertEqual(posts, [])
        self.assertTrue(AdoClient("https://dev.azure.com/o", "P", "t", session=Session(False)).create_iteration("Sprint 1"))
        self.assertEqual(len(posts), 1)



class BrowserViewTests(unittest.TestCase):
    """The local server behind the browser view: who may call it, and what it refuses."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        from jira2ado.ui import server
        cls.server = server
        cls.saved = (server.JiraClient, server.AdoClient, server.SETTINGS)
        server.SETTINGS = os.path.join(tempfile.mkdtemp(), "ui-settings.json")

        class J:
            def __init__(self, base, email, token):
                self.token = token
            def myself(self):
                if self.token != "good":
                    raise RuntimeError("Jira GET /rest/api/3/myself failed: 401 Unauthorized")
                return "Test User"

        class A:
            def __init__(self, org, project, pat, *a, **k):
                self.pat = pat
            def whoami(self):
                from jira2ado.ado_client import AdoError
                if self.pat != "good":
                    raise AdoError("Azure DevOps did not accept the access token (it sent back its sign-in page, HTTP 203).", 203)
                return "Test User"

        server.JiraClient, server.AdoClient = J, A
        server.S.tokens.clear()
        server.S.names.clear()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        server.S.server = cls.httpd
        server.S.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{server.S.port}"
        cls.run_dir = os.path.join(server.KIT, "runs", "test-browser-view", "to-shop")

    @classmethod
    def tearDownClass(cls):
        import shutil
        cls.httpd.shutdown()
        cls.server.JiraClient, cls.server.AdoClient, cls.server.SETTINGS = cls.saved
        cls.server.S.tokens.clear()
        cls.server.S.names.clear()
        shutil.rmtree(os.path.join(cls.server.KIT, "runs", "test-browser-view"), ignore_errors=True)
        try:
            os.rmdir(os.path.join(cls.server.KIT, "runs"))
        except OSError:
            pass

    def call(self, path, body=None, key=True, host=None):
        import io
        import json as _json
        raw_body = b"" if body is None else _json.dumps(body).encode()
        method = "GET" if body is None else "POST"
        hdrs = [f"{method} {path} HTTP/1.1", f"Host: {host or f'127.0.0.1:{self.server.S.port}'}"]
        if key:
            hdrs.append(f"X-Jira2ado-Key: {self.server.S.key}")
        if body is not None:
            hdrs.append("Content-Type: application/json")
            hdrs.append(f"Content-Length: {len(raw_body)}")
        raw_req = ("\r\n".join(hdrs) + "\r\n\r\n").encode() + raw_body

        class _Conn:
            def __init__(self):
                self.rfile = io.BytesIO(raw_req)
                self.out = bytearray()
            def makefile(self, mode="rb", *a, **k):
                return self.rfile if "r" in mode else io.BytesIO()
            def sendall(self, data):
                self.out.extend(data)

        conn = _Conn()
        self.server.Handler(conn, ("127.0.0.1", 55000), self.httpd)
        head, _, payload = bytes(conn.out).partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split()[1])
        resp_headers = {k.strip().lower(): v.strip() for line in lines[1:] if ":" in line for k, v in [line.split(":", 1)]}
        if resp_headers.get("content-type", "").startswith("application/json"):
            return status, _json.loads(payload or b"{}")
        return status, payload

    def test_page_loads_but_the_api_needs_the_key(self):
        status, page = self.call("/", key=False)
        self.assertEqual(status, 200)
        self.assertIn(b"jira2ado", page)
        self.assertEqual(self.call("/api/state", key=False)[0], 403)
        self.assertEqual(self.call("/api/state")[0], 200)

    def test_other_hosts_are_refused(self):
        self.assertEqual(self.call("/api/state", host="evil.example:80")[0], 403)
        self.assertEqual(self.call("/", host="evil.example:80")[0], 403)

    def test_connect_reports_each_service(self):
        status, d = self.call("/api/connect", {"jira_email": "me@x.com", "jira_token": "good", "ado_token": "bad"})
        self.assertEqual(status, 200)
        self.assertEqual(d["jira"], "Test User")
        self.assertIsNone(d["ado"])
        self.assertIn("did not accept the access token", d["problems"]["ado"])
        status, d = self.call("/api/connect", {"jira_email": "me@x.com", "jira_token": "good", "ado_token": "good"})
        self.assertEqual((d["jira"], d["ado"], d["problems"]), ("Test User", "Test User", {}))
        self.assertNotIn("good", json.dumps(self.call("/api/state")[1]))  # tokens never come back
        with open(self.server.SETTINGS) as fh:
            self.assertNotIn("good", fh.read())                          # and are never saved

    def test_files_outside_the_runs_folder_are_refused(self):
        self.assertEqual(self.call("/api/open", {"path": "../../etc/passwd"})[0], 403)
        self.assertEqual(self.call("/api/run?config=configs/targets.yaml")[0], 403)

    def test_the_import_needs_the_right_confirmation(self):
        self.call("/api/connect", {"jira_email": "me@x.com", "jira_token": "good", "ado_token": "good"})
        os.makedirs(self.run_dir, exist_ok=True)
        cfg = os.path.join(self.run_dir, "config.yaml")
        with open(cfg, "w") as fh:
            fh.write("ado: {org_url: 'https://dev.azure.com/o', project: Shop, confirm_with_project_name: true}\n"
                     "jira: {base_url: 'https://x.atlassian.net', jql: 'project = X'}\n")
        rel = os.path.relpath(cfg, self.server.KIT)
        status, d = self.call("/api/import", {"config": rel, "confirm": "yes"})
        self.assertEqual(status, 400)
        self.assertIn("Type Shop exactly", d["error"])
        status, d = self.call("/api/import", {"config": rel, "confirm": "Shop", "possible_duplicates": "maybe"})
        self.assertEqual(status, 400)

    def test_override_persists_per_ticket_states_and_actions(self):
        self.call("/api/connect", {"jira_email": "me@x.com", "jira_token": "good", "ado_token": "good"})
        os.makedirs(self.run_dir, exist_ok=True)
        cfg = os.path.join(self.run_dir, "config.yaml")
        pv = os.path.join(self.run_dir, "preview.json")
        with open(cfg, "w") as fh:
            fh.write("ado: {org_url: 'https://dev.azure.com/o', project: Shop}\n"
                     "jira: {base_url: 'https://x.atlassian.net', jql: 'project = X'}\n")
        with open(pv, "w") as fh:
            json.dump({"tickets": [{"key": "DEMO-1", "jira_status": "QA", "ado_state": "Committed", "action": "create"}]}, fh)
        rel = os.path.relpath(cfg, self.server.KIT)
        status, d = self.call("/api/override", {
            "config": rel,
            "ticket_states": {"DEMO-1": "Done"},
            "ticket_actions": {"DEMO-1": "skip"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(d["run"]["preview"]["tickets"][0]["ado_state"], "Done")
        self.assertEqual(d["run"]["preview"]["tickets"][0]["action"], "skip")

    def test_s3_s6_m6_m8_d2_server_hardening(self):
        # S3: /api/ado/create-state is removed (404)
        self.assertEqual(self.call("/api/ado/create-state", {"project": "Shop", "wit": "Task", "state": "QA"})[0], 404)
        # D2: Double-pasted tokens are caught immediately
        status, d = self.call("/api/connect", {
            "jira_url": "https://x.atlassian.net",
            "ado_url": "https://dev.azure.com/o",
            "jira_email": "me@x.com",
            "jira_token": "secret123456secret123456",
            "ado_token": "adotok123456adotok123456",
        })
        self.assertEqual(status, 200)
        self.assertIn("pasted twice", d["problems"]["jira"])
        self.assertIn("pasted twice", d["problems"]["ado"])
        # Reconnect with valid tokens
        self.call("/api/connect", {"jira_email": "me@x.com", "jira_token": "good", "ado_token": "good"})
        os.makedirs(self.run_dir, exist_ok=True)
        cfg = os.path.join(self.run_dir, "config.yaml")
        with open(cfg, "w") as fh:
            fh.write("ado: {org_url: 'https://dev.azure.com/o', project: Shop, confirm_with_project_name: true}\n"
                     "jira: {base_url: 'https://x.atlassian.net', jql: 'project = X'}\n")
        rel = os.path.relpath(cfg, self.server.KIT)
        # M8: Unconfirmed import does NOT save overrides to config.yaml
        status, _ = self.call("/api/import", {
            "config": rel,
            "confirm": "wrong",
            "ticket_states": {"DEMO-1": "HackedState"},
        })
        self.assertEqual(status, 400)
        with open(cfg) as fh:
            self.assertNotIn("HackedState", fh.read())
        # M8: Invalid ticket action is rejected
        status, d = self.call("/api/override", {"config": rel, "ticket_actions": {"DEMO-1": "drop_database"}})
        self.assertEqual(status, 400)
        # S6: Rollback requires server-side confirmation
        status, d = self.call("/api/rollback", {"config": rel})
        self.assertEqual(status, 400)
        self.assertIn("Type Shop exactly to confirm rollback", d["error"])
        # M6: Session key in query string is rejected when X-Jira2ado-Key header is missing
        self.assertEqual(self.call(f"/api/download?path={rel}&key={self.server.S.key}", key=False)[0], 403)


class ClosestAdoStateTests(unittest.TestCase):
    def test_exact_synonym_and_category_matching(self):
        from jira2ado.planner import closest_ado_state
        # 1. Exact case-insensitive match when ADO has an identically named state
        state, ok, reason = closest_ado_state("Ready for QA", "Product Backlog Item", ["New", "Ready for QA", "Done"])
        self.assertEqual((state, ok, reason), ("Ready for QA", True, "exact"))
        # 2. Semantic/synonym match into standard Scrum states
        state, ok, reason = closest_ado_state("In Progress", "Product Backlog Item", ["New", "Approved", "Committed", "Done"])
        self.assertEqual(state, "Committed")
        # 3. Semantic/synonym match into Agile states
        state, ok, reason = closest_ado_state("In Development", "User Story", ["New", "Active", "Resolved", "Closed"])
        self.assertEqual(state, "Active")
        # 4. Category fallback when status name is completely custom
        state, ok, reason = closest_ado_state("Awaiting Sign-Off", "Product Backlog Item", ["New", "Committed", "Done"],
                                              meta={"statuses": {"Awaiting Sign-Off": "indeterminate"}})
        self.assertEqual(state, "Committed")

    def test_per_ticket_status_override_wins_in_planner(self):
        cfg = cfg_for("x")
        cfg["mapping"]["history"] = False
        cfg["mapping"]["ticket_states"] = {"DEMO-1": "Approved"}
        plan = Planner(cfg, sample.META, USERS).plan(sample.story())
        self.assertEqual(plan["revisions"][0]["fields"]["System.State"], "Approved")


class MultiProcessTypeTests(unittest.TestCase):
    def test_adapts_work_item_type_to_agile_basic_and_cmmi(self):
        from jira2ado.planner import type_for
        cfg = cfg_for("x")
        story_issue = sample.story()
        bug_issue = copy.deepcopy(story_issue)
        bug_issue["fields"]["issuetype"]["name"] = "Bug"
        # Scrum (default)
        self.assertEqual(type_for(story_issue, cfg, ["Product Backlog Item", "Bug", "Task"]), "Product Backlog Item")
        # Agile
        self.assertEqual(type_for(story_issue, cfg, ["User Story", "Bug", "Task"]), "User Story")
        # Basic
        self.assertEqual(type_for(story_issue, cfg, ["Issue", "Task", "Epic"]), "Issue")
        self.assertEqual(type_for(bug_issue, cfg, ["Issue", "Task", "Epic"]), "Issue")
        # CMMI
        self.assertEqual(type_for(story_issue, cfg, ["Requirement", "Bug", "Task"]), "Requirement")


class MentionAndCrossRefTests(unittest.TestCase):
    def test_account_id_mentions_resolved_to_display_names(self):
        from jira2ado.wiki import wiki_to_html
        html = wiki_to_html("cc [~accountid:acc-1] and [~accountid:acc-2]", user_names=USERS["names"])
        self.assertIn("@Ana Lee", html)
        self.assertIn("@Raj Rao", html)
        self.assertNotIn("accountid:acc-1", html)

    def test_cross_ticket_references_linked_to_ado_items(self):
        html = '<p>Depends on <a href="https://demo.atlassian.net/browse/DEMO-2">DEMO-2</a></p>'
        out = rewrite_html(
            html, {}, "https://demo.atlassian.net",
            key_to_ado={"DEMO-2": 102},
            ado_edit_base="https://ado.example/Shop/_workitems/edit/",
        )
        self.assertIn("https://ado.example/Shop/_workitems/edit/102", out)
        self.assertIn("(#102)", out)


class WorklogsAndLinksTests(unittest.TestCase):
    def test_worklogs_remote_links_and_dev_links_planned(self):
        cfg = cfg_for("x")
        issue = sample.story()
        issue["fields"]["timeoriginalestimate"] = 7200  # 2 hours
        issue["fields"]["timespent"] = 5400             # 1.5 hours
        issue["fields"]["timeestimate"] = 1800          # 0.5 hours
        issue["worklogs"] = [{
            "author": sample.U1,
            "created": "2024-01-04T12:00:00.000-0500",
            "timeSpent": "1h 30m",
            "timeSpentSeconds": 5400,
            "comment": "Investigated retry logic",
        }]
        issue["remote_links"] = [{
            "object": {"url": "https://confluence.example.com/pages/123", "title": "Retry Spec"},
        }]
        issue["dev_links"] = [{
            "kind": "pullrequest", "name": "PR #42: Fix retry", "url": "https://github.com/org/repo/pull/42", "status": "MERGED",
        }]
        plan = Planner(cfg, sample.META, USERS).plan(issue)
        final_fields = {}
        comments = []
        for r in plan["revisions"]:
            final_fields.update(r["fields"])
            if r.get("comment"):
                comments.append(r["comment"])
        self.assertEqual(final_fields.get("Microsoft.VSTS.Scheduling.OriginalEstimate"), 2.0)
        self.assertEqual(final_fields.get("Microsoft.VSTS.Scheduling.CompletedWork"), 1.5)
        self.assertEqual(final_fields.get("Microsoft.VSTS.Scheduling.RemainingWork"), 0.5)
        self.assertTrue(any("Jira worklog:" in c and "1h 30m" in c for c in comments))
        ext_urls = {r["url"] for r in plan["revisions"][0].get("relations", [])}
        self.assertIn("https://confluence.example.com/pages/123", ext_urls)
        self.assertIn("https://github.com/org/repo/pull/42", ext_urls)


class DeltaSyncAndRollbackTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        sample.write_export(self.dir)
        self.cfg = cfg_for(self.dir)
        self.q = lambda *a, **k: None

    def test_delta_sync_updates_existing_item_and_rollback_deletes_created_items(self):
        from jira2ado.importer import rollback_run
        ado = sample.FakeAdo()
        stats = import_items(ado, self.cfg, USERS, log=self.q)
        self.assertEqual(stats["created"], 2)
        # Modify DEMO-1 in ADO, then run with ticket_actions={"DEMO-1": "sync"} in a fresh state_dir
        ado.items[101]["fields"]["System.State"] = "New"
        sync_cfg = copy.deepcopy(self.cfg)
        sync_cfg["paths"]["state_dir"] = tempfile.mkdtemp()
        sync_cfg["mapping"]["ticket_actions"] = {"DEMO-1": "sync", "DEMO-2": "skip"}
        sync_stats = import_items(ado, sync_cfg, USERS, log=self.q)
        self.assertEqual(sync_stats["synced"], ["DEMO-1"])
        self.assertEqual(ado.items[101]["fields"]["System.State"], "Done")

        # Now test rollback of the initial run
        rb = rollback_run(ado, self.cfg, log=self.q)
        self.assertEqual(len(rb["deleted"]), 2)
        self.assertEqual(len(ado.items), 0)

    def test_sync_delta_never_overwrites_title_only_match_and_preserves_assignee(self):
        ado = sample.FakeAdo()
        # Unrelated work item that only shares the title (no DEMO-1 tag or JiraKey)
        ado.create_item("Product Backlog Item", [
            {"op": "add", "path": "/fields/System.Title", "value": "Checkout fails on retry"},
            {"op": "add", "path": "/fields/System.State", "value": "New"},
            {"op": "add", "path": "/fields/System.AssignedTo", "value": "existing.owner@contoso.com"},
        ])
        unrelated_id = max(ado.items)
        sync_cfg = copy.deepcopy(self.cfg)
        sync_cfg["mapping"]["ticket_actions"] = {"DEMO-1": "sync", "DEMO-2": "skip"}
        stats = import_items(ado, sync_cfg, USERS, log=self.q)
        # Must NOT overwrite the title-only match; it should hold DEMO-1 as a possible duplicate
        self.assertEqual(stats["synced"], [])
        self.assertIn("DEMO-1", stats["held"])
        self.assertEqual(ado.items[unrelated_id]["fields"]["System.State"], "New")
        self.assertEqual(ado.items[unrelated_id]["fields"]["System.AssignedTo"], "existing.owner@contoso.com")


class CodeReviewRegressionTests(unittest.TestCase):
    """Regression tests for S1-S7, M1-M9, D1-D4, and Smaller #1-#5."""

    def test_s4_preview_and_import_agree_on_agile_and_custom_states(self):
        # When check --online saves available_wits and valid_states_by_type into meta.json,
        # Planner(cfg, meta, users) used by import_items must pick the exact same WIT and State.
        meta = dict(
            sample.META,
            available_wits=["User Story", "Bug", "Task", "Epic", "Feature"],
            valid_states_by_type={
                "User Story": ["New", "Approved", "Active", "Resolved", "Closed"],
                "Task": ["New", "Active", "Closed"],
            },
            state_categories_by_type={
                "User Story": {"New": "Proposed", "Approved": "Proposed", "Active": "InProgress", "Resolved": "Resolved", "Closed": "Completed"},
                "Task": {"New": "Proposed", "Active": "InProgress", "Closed": "Completed"},
            },
        )
        cfg = cfg_for("x")
        cfg["mapping"]["history"] = False
        issue = sample.story()
        issue["fields"]["status"] = {"name": "Approved"}
        plan = Planner(cfg, meta, USERS).plan(issue)
        self.assertEqual(plan["type"], "User Story")
        # Exact 1:1 state "Approved" in User Story valid_states must win over "New"
        self.assertEqual(plan["revisions"][0]["fields"]["System.State"], "Approved")

    def test_s5_and_smaller1_idempotent_create_and_self_healing_resume(self):
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        cfg = cfg_for(folder)
        q = lambda *a, **k: None
        # Fail after 3 writes on DEMO-1 (which has 7 revisions and 3 comments)
        ado = sample.FakeAdo(fail_after=3)
        stats1 = import_items(ado, cfg, USERS, log=q)
        self.assertEqual(stats1["failed"], ["DEMO-1", "DEMO-2"])
        self.assertEqual(ado.items[101]["rev"], 3)
        # Simulate another update or relation added to #101 in ADO while failed (raising rev to 4 and advancing ChangedDate)
        ado.fail_after = None
        ado.update_item(101, [
            {"op": "add", "path": "/fields/System.ChangedDate", "value": "2024-06-01T00:00:00.000Z"},
            {"op": "add", "path": "/relations/-", "value": {"rel": "Hyperlink", "url": "https://example.com"}},
        ])
        self.assertEqual(ado.items[101]["rev"], 4)
        # Retrying import_items must NOT skip revision #3 due to live rev=4; all 3 comments must be written
        stats2 = import_items(ado, cfg, USERS, log=q)
        self.assertEqual(stats2["failed"], [])
        self.assertEqual(len(ado.items), 2)
        self.assertEqual(len(ado.items[101]["comments"]), 3)
        import_links(ado, cfg, log=q)
        _, bad = verify(ado, cfg, USERS, os.path.join(folder, "r.csv"), log=q)
        self.assertEqual(bad, 0)

    def test_m1_retried_attachment_upload_rewinds_file_stream(self):
        import io
        from unittest.mock import patch
        from jira2ado.ado_client import AdoClient

        sent_Payloads = []

        class Resp:
            def __init__(self, status, body=None):
                self.status_code = status
                self.headers = {"Content-Type": "application/json"}
                self.text = json.dumps(body or {})
                self._b = body or {}
            def json(self):
                return self._b

        class Session:
            headers = {}
            def __init__(self):
                self.calls = 0
            def request(self, method, url, **kw):
                self.calls += 1
                data = kw.get("data")
                raw = data.read() if hasattr(data, "read") else (data or b"")
                sent_Payloads.append(raw)
                if self.calls == 1:
                    return Resp(503, {"message": "Service Unavailable"})
                return Resp(201, {"url": "https://ado.example/_apis/wit/attachments/1"})

        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(b"x" * 5000)
        tmp.close()
        try:
            with patch("jira2ado.ado_client.backoff_sleep", return_value=None):
                c = AdoClient("https://dev.azure.com/org", "Proj", "tok", session=Session())
                url = c.upload_attachment(tmp.name, "test.bin")
            self.assertEqual(url, "https://ado.example/_apis/wit/attachments/1")
            self.assertEqual(len(sent_Payloads), 2)
            self.assertEqual(len(sent_Payloads[0]), 5000)
            self.assertEqual(len(sent_Payloads[1]), 5000)
        finally:
            os.unlink(tmp.name)

    def test_m2_and_m5_https_enforced_and_jira_is_readonly(self):
        from jira2ado.ado_client import AdoClient, AdoError
        from jira2ado.jira_client import JiraClient
        with self.assertRaises(RuntimeError):
            JiraClient("http://insecure.example.com", "a@b.com", "tok")
        with self.assertRaises(AdoError):
            AdoClient("http://insecure.example.com/org", "Proj", "tok")
        self.assertFalse(hasattr(JiraClient, "stamp_migrated"))

    def test_m4_identities_checked_against_ado_and_ambiguous_names_excluded(self):
        import csv
        from jira2ado.exporter import write_users_file
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        path = os.path.join(folder, "users.csv")
        # Provide an online ado_discovered directory that does NOT contain ana@contoso.com
        ado_discovered = {"by_email": {"someone.else@contoso.com": "someone.else@contoso.com"}, "by_name": {}}
        write_users_file(folder, path, {"example.com": "contoso.com"}, ado_discovered=ado_discovered)
        with open(path, newline="") as fh:
            rows = {r["display_name"]: r["ado_identity"] for r in csv.DictReader(fh)}
        # Unverified domain-rewritten guess must NOT be trusted when ado_discovered directory is provided
        self.assertEqual(rows["Ana Lee"], "")

    def test_smaller3_per_ticket_state_override_updates_closed_date(self):
        cfg = cfg_for("x")
        cfg["mapping"]["history"] = False
        # Story is "Done" in Jira, override to "Committed" -> ClosedDate must be cleared
        cfg["mapping"]["ticket_states"] = {"DEMO-1": "Committed", "DEMO-2": "Done"}
        p = Planner(cfg, sample.META, USERS)
        story_plan = p.plan(sample.story())
        self.assertEqual(story_plan["revisions"][0]["fields"]["System.State"], "Committed")
        self.assertNotIn("Microsoft.VSTS.Common.ClosedDate", story_plan["revisions"][0]["fields"])
        # Subtask is "In Review" in Jira, override to "Done" -> ClosedDate must be populated
        sub_plan = p.plan(sample.subtask())
        self.assertEqual(sub_plan["revisions"][0]["fields"]["System.State"], "Done")
        self.assertTrue(sub_plan["revisions"][0]["fields"].get("Microsoft.VSTS.Common.ClosedDate"))

    def test_smaller5_http_date_retry_after_and_truncated_journal(self):
        from unittest.mock import patch
        from jira2ado.util import Journal, backoff_sleep
        with patch("jira2ado.util.time.sleep") as mock_sleep:
            backoff_sleep(0, "Wed, 21 Oct 2015 07:28:00 GMT")
            self.assertTrue(mock_sleep.called)
        jfile = os.path.join(tempfile.mkdtemp(), "state.jsonl")
        with open(jfile, "w") as fh:
            fh.write('{"t":"rev","k":"DEMO-1","id":101,"n":2}\n{"t":"rev","k":"BROKEN')
        j = Journal(jfile, readonly=True)
        self.assertEqual(j.items["DEMO-1"]["revs"], 2)

    def test_d4_verify_checks_iteration_and_assignee(self):
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        cfg = cfg_for(folder)
        q = lambda *a, **k: None
        ado = sample.FakeAdo()
        import_items(ado, cfg, USERS, log=q)
        import_links(ado, cfg, log=q)
        # Tamper with IterationPath on item 101
        ado.items[101]["fields"]["System.IterationPath"] = "Shop\\WrongIteration"
        report = os.path.join(folder, "verify.csv")
        _, bad = verify(ado, cfg, USERS, report, log=q)
        self.assertEqual(bad, 1)
        with open(report) as fh:
            self.assertIn("iteration is Shop\\WrongIteration, expected Shop\\Sprint 2-A", fh.read())

    def test_round2_sync_delta_comments_attachments_tags_and_rollback(self):
        from jira2ado.importer import rollback_run
        from jira2ado.util import read_json, write_json
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        cfg = cfg_for(folder)
        cfg["mapping"]["history"] = False
        cfg["mapping"]["note_jira_status"] = True
        q = lambda *a, **k: None
        ado = sample.FakeAdo()

        # Initial import of DEMO-1 (2 comments, 1 attachment)
        stats0 = import_items(ado, cfg, USERS, only={"DEMO-1"}, log=q)
        self.assertEqual(stats0["created"], 1)
        self.assertEqual(len(ado.uploads), 1)
        # User adds a custom tag in Azure DevOps and changes State to New
        ado.items[101]["fields"]["System.Tags"] = str(ado.items[101]["fields"].get("System.Tags") or "") + "; ADO-Custom-Tag"
        ado.items[101]["fields"]["System.State"] = "New"

        # Add a 3rd comment to DEMO-1 in the Jira export and run 1st Sync from a brand-new state_dir
        issue_file = os.path.join(folder, "issues", "DEMO-1.json")
        iss = read_json(issue_file)
        iss["comments"].append({
            "id": "c3", "author": sample.U1,
            "created": "2024-01-05T10:00:00.000-0500",
            "body": "Third comment added before first sync",
        })
        write_json(issue_file, iss)

        sync_cfg1 = copy.deepcopy(cfg)
        sync_cfg1["paths"]["state_dir"] = tempfile.mkdtemp()
        sync_cfg1["mapping"]["ticket_actions"] = {"DEMO-1": "sync"}
        s1 = import_items(ado, sync_cfg1, USERS, only={"DEMO-1"}, log=q)
        self.assertEqual(s1["synced"], ["DEMO-1"])
        # Attachment must NOT be re-uploaded or duplicated even in a new state_dir!
        self.assertEqual(len(ado.uploads), 1)
        att_rels = [r for r in ado.items[101]["relations"] if r.get("rel") == "AttachedFile"]
        self.assertEqual(len(att_rels), 1)
        # ADO-Custom-Tag must be preserved alongside Jira tags
        self.assertIn("ADO-Custom-Tag", ado.items[101]["fields"]["System.Tags"])
        self.assertIn("checkout", ado.items[101]["fields"]["System.Tags"])
        # The 3rd comment must be added even with note_jira_status=True
        all_comments_1 = "\n".join(ado.comments(101))
        self.assertIn("Third comment added before first sync", all_comments_1)

        # Add a 4th comment in Jira and run a 2nd Sync from another new state_dir
        iss["comments"].append({
            "id": "c4", "author": sample.U2,
            "created": "2024-01-06T11:00:00.000-0500",
            "body": "Fourth comment added before second sync",
        })
        write_json(issue_file, iss)
        sync_cfg2 = copy.deepcopy(cfg)
        sync_cfg2["paths"]["state_dir"] = tempfile.mkdtemp()
        sync_cfg2["mapping"]["ticket_actions"] = {"DEMO-1": "sync"}
        s2 = import_items(ado, sync_cfg2, USERS, only={"DEMO-1"}, log=q)
        self.assertEqual(s2["synced"], ["DEMO-1"])
        self.assertEqual(len(ado.uploads), 1)
        all_comments_2 = "\n".join(ado.comments(101))
        self.assertIn("Fourth comment added before second sync", all_comments_2)
        # Ensure 3rd comment was not duplicated by the 2nd sync
        self.assertEqual(all_comments_2.count("Third comment added before first sync"), 1)

        # Rollback of sync_cfg1 must restore DEMO-1's State to "New" (its state before sync_cfg1) without deleting #101
        rb = rollback_run(ado, sync_cfg1, log=q)
        self.assertEqual(len(rb["deleted"]), 0)
        self.assertEqual(len(rb["restored"]), 1)
        self.assertIn(101, ado.items)
        self.assertEqual(ado.items[101]["fields"]["System.State"], "New")

    def test_round2_config_state_map_beats_same_name_state(self):
        from jira2ado.planner import closest_ado_state
        cfg = cfg_for("x")
        cfg["mapping"]["states"]["default"]["In Progress"] = "Development"
        valid = ["To Do", "In Progress", "Development", "Done"]
        state, ok, reason = closest_ado_state("In Progress", "Product Backlog Item", valid, cfg, sample.META)
        self.assertEqual((state, ok, reason), ("Development", True, "configured"))

    def test_round2_empty_ado_user_discovery_does_not_guess_emails(self):
        import csv
        from jira2ado.exporter import write_users_file
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        path = os.path.join(folder, "users.csv")
        write_users_file(folder, path, {"example.com": "contoso.com"}, ado_discovered={"by_email": {}, "by_name": {}})
        with open(path, newline="") as fh:
            rows = {r["display_name"]: r["ado_identity"] for r in csv.DictReader(fh)}
        self.assertEqual(rows["Ana Lee"], "")

    def test_round2_per_destination_ado_meta_isolation_and_multi_sprint_slug(self):
        from jira2ado.importer import load_meta
        from jira2ado.util import sprint_folder_slug, write_json
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        dest_a = tempfile.mkdtemp()
        dest_b = tempfile.mkdtemp()
        write_json(os.path.join(dest_a, "ado-meta.json"), {"available_wits": ["Product Backlog Item", "Bug", "Task"]})
        write_json(os.path.join(dest_b, "ado-meta.json"), {"available_wits": ["User Story", "Bug", "Task"]})
        cfg_a = cfg_for(folder)
        cfg_a["paths"]["state_dir"] = dest_a
        cfg_b = cfg_for(folder)
        cfg_b["paths"]["state_dir"] = dest_b
        self.assertEqual(load_meta(cfg_a)["available_wits"][0], "Product Backlog Item")
        self.assertEqual(load_meta(cfg_b)["available_wits"][0], "User Story")

        # 25 sprints must produce a short, deterministic folder name (< 80 chars)
        many_sprints = [{"id": i, "name": f"Platform and Core Engineering Sprint {i}"} for i in range(1, 26)]
        folder_name = sprint_folder_slug(many_sprints)
        self.assertLessEqual(len(folder_name), 80)
        self.assertIn("25-sprints", folder_name)

    def test_round2_bundled_config_fallback_when_frozen(self):
        from jira2ado import config
        empty_cwd = tempfile.mkdtemp()
        saved_cwd = os.getcwd()
        try:
            os.chdir(empty_cwd)
            resolved_base = config.resolve_config_path(os.path.join("configs", "enterprise-development.yaml"))
            resolved_targets = config.resolve_config_path(os.path.join("configs", "targets.yaml"))
            self.assertTrue(os.path.exists(resolved_base))
            self.assertTrue(os.path.exists(resolved_targets))
            loaded = config.load(os.path.join("configs", "enterprise-development.yaml"))
            self.assertEqual(loaded["ado"]["project"], "Production")
        finally:
            os.chdir(saved_cwd)

    def test_packaging_workspace_and_clean_config_staging(self):
        from unittest.mock import patch
        from jira2ado import config
        from packaging import build as pkg_build

        # 1. frozen_workspace_dir must resolve to JIRA2ADO_HOME (or ~/Documents/Jira2ADO), never read-only bundle dirs
        custom_home = os.path.join(tempfile.mkdtemp(), "MyWorkspace")
        with patch.dict(os.environ, {"JIRA2ADO_HOME": custom_home}):
            ws = config.frozen_workspace_dir()
        self.assertEqual(ws, custom_home)
        self.assertTrue(os.path.isdir(ws))

        # 2. stage_clean_configs must include only public templates and never leak configs/local/
        staged = pkg_build.stage_clean_configs()
        self.assertEqual(sorted(os.listdir(staged)), ["enterprise-development.yaml", "targets.yaml"])
        self.assertFalse(os.path.exists(os.path.join(staged, "local")))

    def test_round2_no_company_strings_in_tracked_files(self):
        import subprocess
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tracked = subprocess.check_output(["git", "-C", repo_root, "ls-files"], text=True).splitlines()
        forbidden = (
            "petvalu", "petretailbrands", "Fin Corp", "Fin&Corp", "Fin-Corp",
            "DEA-", "PVE-", "17990", "3637", "customfield_10330", "customfield_10455",
            "PO/BA Review", "Pre-Prod Promotion",
        )
        for rel in tracked:
            if rel == "tests/test_all.py":
                continue
            full = os.path.join(repo_root, rel)
            if not os.path.isfile(full):
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
            except UnicodeDecodeError:
                continue
            for term in forbidden:
                self.assertNotIn(term.lower(), text.lower(), f"Forbidden term '{term}' found in {rel}")


if __name__ == "__main__":
    unittest.main()
