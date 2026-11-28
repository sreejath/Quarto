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
            board_title = "Team Board"

            def __init__(self, *a, **k):
                pass

            def boards(self, project=None):
                return [{"id": 42, "name": self.board_title, "type": "scrum"}]

            def board_filter_jql(self, board_id):
                return 'project = "PROJ" AND component in ("Cart", "Checkout")'

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

    def test_board_and_project_stable_folders_and_board_filter_jql(self):
        import jira2ado.jira_client as jc
        # 1. Project mode: toggling --include-done keeps the exact same run folder
        p_open, cfg_open, _ = self._prepare("--mode", "project", "--from", "PROJ", "--to", "Sandbox")
        p_done, cfg_done, _ = self._prepare("--mode", "project", "--from", "PROJ", "--to", "Sandbox", "--include-done")
        self.assertEqual(p_open, p_done)
        self.assertEqual(p_open, os.path.join("runs", "proj", "entire-project", "to-sandbox", "config.yaml"))
        self.assertFalse(cfg_open["run"]["include_done"])
        self.assertTrue(cfg_done["run"]["include_done"])

        # 2. Board mode: renaming the board keeps the exact same run folder (keyed by board_id) and uses board filter JQL
        p_b1, cfg_b1, _ = self._prepare("--mode", "board", "--from", "PROJ", "--to", "Sandbox", "--board", "42")
        jc.JiraClient.board_title = "Team Board Renamed"
        p_b2, cfg_b2, _ = self._prepare("--mode", "board", "--from", "PROJ", "--to", "Sandbox", "--board", "42", "--include-done")
        self.assertEqual(p_b1, p_b2)
        self.assertEqual(p_b1, os.path.join("runs", "proj", "board-42", "to-sandbox", "config.yaml"))
        self.assertEqual(
            cfg_b1["jira"]["jql"],
            '(project = "PROJ" AND component in ("Cart", "Checkout")) AND statusCategory != Done ORDER BY key ASC',
        )
        self.assertEqual(
            cfg_b2["jira"]["jql"],
            '(project = "PROJ" AND component in ("Cart", "Checkout")) ORDER BY key ASC',
        )


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
        entries = [("PROJ-4200", "Fix  the Banner"), ("PROJ-3", "Other")]
        items = [self.item(1, "Anything", "NAV; proj-4200"),
                 self.item(2, "Anything", "", **{"Custom.JiraKey": "PROJ-4200"}),
                 self.item(3, "[PROJ-4200] copied by hand"),
                 self.item(4, "fix the banner"),
                 self.item(5, "PROJ-42000 is a different ticket"),
                 self.item(6, "Follow-up to PROJ-3 work")]
        found = classify(entries, items, "Custom.JiraKey")
        self.assertEqual([(m["id"], m["kind"]) for m in found["PROJ-4200"]],
                         [(1, SAME_KEY), (2, SAME_KEY), (3, KEY_IN_TITLE), (4, SAME_TITLE)])
        self.assertEqual([(m["id"], m["kind"]) for m in found["PROJ-3"]], [(6, KEY_IN_TITLE)])
        self.assertNotIn(4, [m["id"] for m in classify(entries, items, titles=False).get("PROJ-4200", [])])

    def test_descriptions_and_groups(self):
        from jira2ado.duplicates import classify, describe, same_summary_groups
        m = classify([("PROJ-9", "Release prep")], [self.item(18500, "Release Prep", "PROJ-8")])["PROJ-9"][0]
        self.assertEqual(describe(m), "same title as #18500 (Committed, from PROJ-8)")
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
        plans[1]["subtask"] = False
        plans.append({**plans[1], "key": "DEMO-3", "subtask": False})  # a second non-subtask ticket with the same summary
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

    def test_multi_mode_pages_and_saved_runs_recovery_api(self):
        # Verify Mode Hub (/), Mode 1 (/sprints), Mode 2 (/board), Mode 3 (/project), and Mode 4 (/jql Not Implemented)
        for route, expected in (
            ("/", b'data-page-mode="hub"'),
            ("/sprints", b'data-page-mode="sprints"'),
            ("/board", b'data-page-mode="board"'),
            ("/project", b'data-page-mode="project"'),
            ("/jql", b'data-page-mode="jql"'),
        ):
            status, html_bytes = self.call(route, key=False)
            self.assertEqual(status, 200, f"Route {route} failed")
            self.assertIn(expected, html_bytes)

        # Create a failed run in self.run_dir and verify GET /api/runs and POST /api/runs/recover
        os.makedirs(self.run_dir, exist_ok=True)
        cfg_file = os.path.join(self.run_dir, "config.yaml")
        pv_file = os.path.join(self.run_dir, "preview.json")
        res_file = os.path.join(self.run_dir, "import-result.json")
        with open(cfg_file, "w") as fh:
            fh.write("ado: {org_url: 'https://dev.azure.com/o', project: Shop}\n"
                     "jira: {base_url: 'https://x.atlassian.net', jql: 'project = DEMO'}\n"
                     "run: {mode: board, from: DEMO, to: Shop, board_id: '12', board_name: 'Core Board'}\n")
        with open(pv_file, "w") as fh:
            json.dump({"project": "Shop", "issues": 2, "tickets": [
                {"key": "DEMO-1", "revisions": 2, "ado_state": "New"},
                {"key": "DEMO-2", "revisions": 2, "ado_state": "New"},
            ]}, fh)
        with open(res_file, "w") as fh:
            json.dump({"created": 1, "failed": ["DEMO-2"], "remaining": 1}, fh)
        rel = os.path.relpath(cfg_file, self.server.KIT)

        status, runs_data = self.call("/api/runs")
        self.assertEqual(status, 200)
        found_run = next((r for r in runs_data["runs"] if r["config"] == rel), None)
        self.assertIsNotNone(found_run)
        self.assertEqual(found_run["mode"], "board")
        self.assertEqual(found_run["state"], "failed")
        self.assertEqual(found_run["failed"], 1)

        status, rec_data = self.call("/api/runs/recover", {"config": rel})
        self.assertEqual(status, 200)
        self.assertEqual(rec_data["recovered"], "loaded")
        self.assertEqual(rec_data["run"]["preview"]["project"], "Shop")

        # Medium #8: /api/run/progress omits preview.json so polling on 10,000-ticket projects stays tiny
        status, prog_data = self.call(f"/api/run/progress?config={rel}")
        self.assertEqual(status, 200)
        self.assertNotIn("preview", prog_data)
        self.assertEqual(prog_data["result"]["failed"], ["DEMO-2"])

        # Smaller #3: /api/import rejects only_keys starting with '--' or non-Jira key strings
        self.call("/api/connect", {"jira_email": "me@x.com", "jira_token": "good", "ado_token": "good"})
        status, bad_key_resp = self.call("/api/import", {
            "config": rel,
            "confirm": "Shop",
            "only_keys": ["--no-links"],
        })
        self.assertEqual(status, 400)
        self.assertIn("Invalid Jira ticket key", bad_key_resp["error"])

        # Medium #7: run_step only reports "paused" when exit code is 2 (never overriding exit code 0 or 1)
        job = self.server.Job("import", ["Plan", "Import"])
        job.paused = True
        saved_cmd = self.server.CMD
        try:
            self.server.CMD = [sys.executable, "-c", "import sys; sys.exit(0)"]
            code0, _ = self.server.run_step(job, 0, ["plan"])
            self.assertEqual(code0, 0)
            self.assertEqual(job.steps[0]["status"], "done")

            self.server.CMD = [sys.executable, "-c", "import sys; sys.exit(1)"]
            code1, _ = self.server.run_step(job, 1, ["import"])
            self.assertEqual(code1, 1)
            self.assertEqual(job.steps[1]["status"], "failed")
        finally:
            self.server.CMD = saved_cmd


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
        # Cross-type bulk override (e.g. applying PBI 'New' or 'Done' across Task and Test Case tickets) must adapt to valid_states
        meta_valid = dict(sample.META, valid_states_by_type={
            "Task": ["To Do", "In Progress", "Done", "Removed"],
            "Test Case": ["Design", "Ready", "Closed"],
        })
        cfg["mapping"]["types"]["Test"] = "Test Case"
        cfg["mapping"]["ticket_states"] = {"DEMO-2": "New", "DEMO-TEST1": "New", "DEMO-TEST2": "Done"}
        p_valid = Planner(cfg, meta_valid, USERS)
        self.assertEqual(p_valid.plan(sample.subtask())["revisions"][0]["fields"]["System.State"], "To Do")
        tc1 = copy.deepcopy(sample.subtask())
        tc1["key"] = "DEMO-TEST1"
        tc1["fields"]["issuetype"] = {"name": "Test"}
        tc2 = copy.deepcopy(tc1)
        tc2["key"] = "DEMO-TEST2"
        self.assertEqual(p_valid.plan(tc1)["revisions"][0]["fields"]["System.State"], "Design")
        self.assertEqual(p_valid.plan(tc2)["revisions"][0]["fields"]["System.State"], "Closed")

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
        import importlib.util
        from unittest.mock import patch
        from jira2ado import config
        build_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging", "build.py")
        spec = importlib.util.spec_from_file_location("jira2ado_pkg_build", build_py)
        pkg_build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pkg_build)

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
        import hashlib
        import re
        import subprocess
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tracked = subprocess.check_output(["git", "-C", repo_root, "ls-files"], text=True).splitlines()
        # SHA-256 prefixes of forbidden lowercase tokens so the test itself never stores the literal terms
        forbidden_hashes = {
            "7d873d138de7ddb5", "6568421ed8b0c51a", "40de80a7f7386b54",
            "2b65e2f45f23ac94", "81cabe6d5e7a09c3", "7e93bf8ba9708c55",
            "831e6bd92db10f33", "0f334a448c32623c", "8d462078db7248de",
        }
        for rel in tracked:
            full = os.path.join(repo_root, rel)
            if not os.path.isfile(full):
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    text = fh.read().lower()
            except UnicodeDecodeError:
                continue
            tokens = set(re.findall(r"[a-z0-9_&\-]+", text))
            for tok in tokens:
                digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()[:16]
                self.assertNotIn(digest, forbidden_hashes, f"Forbidden token found in {rel}")

    def test_subtask_duplicates_checked_by_jira_key_only_never_by_title(self):
        from jira2ado.cli import _duplicate_report
        from jira2ado.importer import check_duplicates
        from jira2ado.util import Journal, read_json
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        cfg = cfg_for(folder)
        cfg["paths"]["state_dir"] = tempfile.mkdtemp()
        planner = Planner(cfg, sample.META, USERS)

        # Create two subtasks across different stories that have the exact same summary "Add retry token"
        sub1 = sample.subtask()  # key DEMO-2, summary "Add retry token", issuetype "Sub-task"
        sub2 = copy.deepcopy(sub1)
        sub2["key"] = "DEMO-3"
        plans = [planner.plan(sub1), planner.plan(sub2)]
        self.assertTrue(plans[0]["subtask"])
        self.assertTrue(plans[1]["subtask"])

        ado = sample.FakeAdo()
        # Existing ADO Task from a different Jira sub-task (DEMO-99) that has the exact same title "Add retry token"
        ado.create_item("Task", [
            {"op": "add", "path": "/fields/System.Title", "value": "Add retry token"},
            {"op": "add", "path": "/fields/System.State", "value": "To Do"},
            {"op": "add", "path": "/fields/System.Tags", "value": "DEMO-99"},
        ])
        journal = Journal(os.path.join(cfg["paths"]["state_dir"], "import-state.jsonl"), readonly=True)
        there, possible = check_duplicates(ado, cfg, plans, journal)
        # Must NOT match DEMO-2 or DEMO-3 against DEMO-99 by title!
        self.assertEqual(there, {})
        self.assertEqual(possible, {})

        # Also check _duplicate_report: DEMO-2 and DEMO-3 must NOT be grouped in same_summary_in_run
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            _duplicate_report(cfg, plans, journal, ado)
        dup_json = read_json(os.path.join(cfg["paths"]["state_dir"], "duplicates.json"))
        self.assertEqual(dup_json["same_summary_in_run"], [])

        # Now add an ADO item that actually carries DEMO-2's Jira key -> must be matched as SAME_KEY
        ado.create_item("Task", [
            {"op": "add", "path": "/fields/System.Title", "value": "Add retry token"},
            {"op": "add", "path": "/fields/System.State", "value": "Done"},
            {"op": "add", "path": "/fields/System.Tags", "value": "DEMO-2"},
        ])
        there2, possible2 = check_duplicates(ado, cfg, plans, journal)
        self.assertIn("DEMO-2", there2)
        self.assertNotIn("DEMO-3", there2)
        self.assertEqual(possible2, {})

    def test_mode2_board_and_mode3_project_prepare_and_unsprinted_tickets(self):
        from jira2ado.exporter import search_run_keys
        from jira2ado.jira_client import JiraClient
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        cfg = cfg_for(folder)
        cfg["run"] = {"mode": "board", "from": "DEMO", "to": "Shop", "board_id": "42", "board_name": "Team Board",
                      "sprints": [{"id": 7, "number": 7, "name": "Sprint 2&A", "state": "active",
                                   "start": "2024-01-01", "end": "2024-01-15"}]}
        cfg["jira"]["board_id"] = "42"
        cfg["jira"]["board_filter_jql"] = "statusCategory != Done"
        cfg["mapping"]["only_sprints"] = None

        # 1. Test JiraClient.board_keys against the real documented Agile response shape (using 'issues' + 'total')
        class AgileResp:
            def __init__(self, body):
                self.status_code = 200
                self.headers = {"Content-Type": "application/json"}
                self.text = json.dumps(body)
                self._b = body
            def json(self):
                return self._b

        class AgileSession:
            headers = {}
            def request(self, method, url, **kw):
                params = kw.get("params") or {}
                if url.endswith("/rest/agile/1.0/board/42/configuration"):
                    return AgileResp({"id": 42, "filter": {"id": "990"}})
                if url.endswith("/rest/api/3/filter/990"):
                    return AgileResp({"id": "990", "jql": 'project = "DEMO" AND component = "Core" ORDER BY Rank ASC'})
                if url.endswith("/rest/agile/1.0/board/42/issue"):
                    start = params.get("startAt", 0)
                    if start == 0:
                        return AgileResp({
                            "startAt": 0, "maxResults": 2, "total": 3,
                            "issues": [{"key": "DEMO-1"}, {"key": "DEMO-2"}],
                        })
                    return AgileResp({
                        "startAt": 2, "maxResults": 2, "total": 3,
                        "issues": [{"key": "DEMO-UNSPRINTED"}],
                    })
                if url.endswith("/rest/api/3/search/jql"):
                    return AgileResp({
                        "issues": [
                            {"key": "DEMO-1", "fields": {"updated": "2024-01-20T09:00:00.000-0500"}},
                            {"key": "DEMO-2", "fields": {"updated": "2024-01-06T09:00:00.000-0500"}},
                            {"key": "DEMO-UNSPRINTED", "fields": {"updated": "2024-01-10T09:00:00.000-0500"}},
                        ],
                        "isLast": True,
                    })
                raise AssertionError(f"Unexpected URL {url}")

        real_jc = JiraClient("https://demo.atlassian.net", "a@b.com", "tok", session=AgileSession())
        self.assertEqual(real_jc.board_filter_jql(42), 'project = "DEMO" AND component = "Core"')
        self.assertEqual(list(real_jc.board_keys(42)), ["DEMO-1", "DEMO-2", "DEMO-UNSPRINTED"])
        cfg["jira"]["jql"] = '(project = "DEMO" AND component = "Core") AND statusCategory != Done ORDER BY key ASC'
        self.assertEqual(search_run_keys(real_jc, cfg), ["DEMO-1", "DEMO-2", "DEMO-UNSPRINTED"])

        # Unsprinted backlog ticket on the board must be placed at the base/root iteration while sprinted ticket gets its sprint iteration
        unsprinted = sample.story()
        unsprinted["key"] = "DEMO-UNSPRINTED"
        unsprinted["fields"]["customfield_10020"] = None
        unsprinted["changelog"] = []
        planner = Planner(cfg, sample.META, USERS)
        p_un = planner.plan(unsprinted)
        p_sp = planner.plan(sample.story())
        self.assertEqual(p_un["revisions"][0]["fields"]["System.IterationPath"], "Shop")
        sp_iters = [r["fields"]["System.IterationPath"] for r in p_sp["revisions"] if "System.IterationPath" in r["fields"]]
        self.assertEqual(sp_iters[-1], "Shop\\Sprint 2-A")

    def test_batched_import_pause_and_resume(self):
        from jira2ado.importer import pause_path
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        cfg = cfg_for(folder)
        cfg["paths"]["state_dir"] = tempfile.mkdtemp()
        q = lambda *a, **k: None
        ado = sample.FakeAdo()

        # Batch 1 with limit=1 must migrate DEMO-1 only and report remaining=1
        b1 = import_items(ado, cfg, USERS, limit=1, log=q)
        self.assertEqual(b1["created"], 1)
        self.assertEqual(b1["remaining"], 1)
        self.assertEqual(len(ado.items), 1)

        # Write a .pause sentinel before Batch 2 -> must pause cleanly with paused=True and remaining=1
        with open(pause_path(cfg), "w", encoding="utf-8") as fh:
            fh.write("pause\n")
        b_paused = import_items(ado, cfg, USERS, limit=1, log=q)
        self.assertTrue(b_paused["paused"])
        self.assertEqual(b_paused["created"], 0)
        self.assertEqual(b_paused["complete"], 1)
        self.assertEqual(b_paused["remaining"], 1)
        self.assertFalse(os.path.exists(pause_path(cfg)))

        # Resume Batch 2 with limit=1 -> must skip already-completed DEMO-1 and migrate DEMO-2!
        b2 = import_items(ado, cfg, USERS, limit=1, log=q)
        self.assertFalse(b2["paused"])
        self.assertEqual(b2["created"], 1)
        self.assertEqual(b2["complete"], 1)
        self.assertEqual(b2["remaining"], 0)
        self.assertEqual(len(ado.items), 2)

    def test_round3_export_reexports_only_tickets_whose_updated_timestamp_changed(self):
        from jira2ado.exporter import export
        from jira2ado.util import read_json, write_json
        folder = tempfile.mkdtemp()
        state_dir = tempfile.mkdtemp()
        cfg = cfg_for(folder)
        cfg["paths"]["state_dir"] = state_dir
        cfg["jira"]["jql"] = 'project = "DEMO"'
        # Create a stale preview.json in state_dir; export() must remove it so paused exports aren't mistaken for valid previews
        stale_preview = os.path.join(state_dir, "preview.json")
        write_json(stale_preview, {"issues": 99})

        story_v1 = sample.story()
        for a in story_v1["fields"].get("attachment") or []:
            a.pop("local_path", None)
            a["content"] = f"https://demo.atlassian.net/rest/api/3/attachment/content/{a['id']}"
        sub_v1 = sample.subtask()
        fetched_keys = []

        class DeltaJira:
            def __init__(self):
                self.issues_map = {"DEMO-1": copy.deepcopy(story_v1), "DEMO-2": copy.deepcopy(sub_v1)}

            def fields(self):
                return [
                    {"id": "customfield_10020", "name": "Sprint"},
                    {"id": "customfield_10016", "name": "Story point estimate"},
                    {"id": "customfield_10000", "name": "Development"},
                ]

            def statuses(self):
                return [
                    {"name": "To Do", "statusCategory": {"key": "new"}},
                    {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                    {"name": "In Review", "statusCategory": {"key": "indeterminate"}},
                    {"name": "Done", "statusCategory": {"key": "done"}},
                ]

            def search_key_timestamps(self, jql):
                for k, iss in self.issues_map.items():
                    yield k, iss["fields"]["updated"]

            def issue(self, key):
                fetched_keys.append(key)
                return copy.deepcopy(self.issues_map[key])

            def changelog(self, key):
                return self.issues_map[key].get("changelog", [])

            def comments(self, key):
                return self.issues_map[key].get("comments", [])

            def worklogs(self, key):
                return []

            def remote_links(self, key):
                return []

            def download(self, url, dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(b"png-bytes")

        dj = DeltaJira()
        q = lambda *a, **k: None
        done1, skipped1 = export(dj, cfg, force=False, log=q)
        self.assertEqual((done1, skipped1), (2, 0))
        self.assertEqual(sorted(fetched_keys), ["DEMO-1", "DEMO-2"])
        self.assertFalse(os.path.exists(stale_preview))

        # Second export when nothing changed: 0 tickets fetched!
        fetched_keys.clear()
        done2, skipped2 = export(dj, cfg, force=False, log=q)
        self.assertEqual((done2, skipped2), (0, 2))
        self.assertEqual(fetched_keys, [])

        # Modify DEMO-1's status, comments, and updated timestamp in Jira -> only DEMO-1 is re-exported!
        dj.issues_map["DEMO-1"]["fields"]["status"] = {"name": "In Review"}
        dj.issues_map["DEMO-1"]["fields"]["updated"] = "2024-02-01T10:00:00.000-0500"
        dj.issues_map["DEMO-1"]["comments"].append({
            "id": "c99", "author": sample.U1, "created": "2024-02-01T10:00:00.000-0500",
            "renderedBody": "<p>New comment after first export</p>",
        })
        fetched_keys.clear()
        done3, skipped3 = export(dj, cfg, force=False, log=q)
        self.assertEqual((done3, skipped3), (1, 1))
        self.assertEqual(fetched_keys, ["DEMO-1"])
        on_disk = read_json(os.path.join(folder, "issues", "DEMO-1.json"))
        self.assertEqual(on_disk["fields"]["status"]["name"], "In Review")
        self.assertEqual(len(on_disk["comments"]), 3)

    def test_round3_open_backlog_ticket_with_only_closed_sprint_stays_on_backlog(self):
        # Serious #3: An "In Progress" ticket whose only sprint is the closed Sprint 27 must go to
        # the root/base iteration ("Shop"), both with history ON and with history OFF.
        open_issue = sample.story()
        open_issue["fields"]["status"] = {"name": "In Progress"}
        open_issue["fields"]["resolution"] = None
        open_issue["fields"]["resolutiondate"] = None
        open_issue["fields"]["customfield_10020"] = [
            {"id": 27, "name": "Sprint 27", "state": "closed",
             "startDate": "2024-01-01T00:00:00.000Z", "endDate": "2024-01-14T00:00:00.000Z"}
        ]
        open_issue["changelog"] = [
            {"created": "2024-01-03T09:00:00.000-0500", "author": sample.U1, "items": [
                sample.item("status", "To Do", "In Progress"),
                sample.item("Sprint", "", "Sprint 27", "", "27"),
            ]}
        ]

        # 1. History OFF
        cfg_no_hist = cfg_for("x")
        cfg_no_hist["mapping"]["history"] = False
        p_no_hist = Planner(cfg_no_hist, sample.META, USERS).plan(open_issue)
        self.assertEqual(p_no_hist["revisions"][0]["fields"]["System.IterationPath"], "Shop")

        # 2. History ON
        cfg_hist = cfg_for("x")
        cfg_hist["mapping"]["history"] = True
        planner_hist = Planner(cfg_hist, sample.META, USERS)
        p_hist = planner_hist.plan(open_issue)
        final_fields = {}
        for r in p_hist["revisions"]:
            final_fields.update(r["fields"])
        self.assertEqual(final_fields["System.IterationPath"], "Shop")
        self.assertFalse(any("did not add up" in w for w in planner_hist.warnings))

    def test_round3_same_name_sprints_with_different_ids_are_not_merged(self):
        # Serious #4: Two "Sprint 1"s on different boards, a year apart (ID 10 in 2024, ID 20 in 2025)
        # must create two distinct iterations with their own dates and keep their tickets separate.
        from jira2ado.util import write_json
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        iss1 = sample.story()
        iss1["key"] = "DEMO-1"
        iss1["changelog"] = []
        iss1["fields"]["customfield_10020"] = [{
            "id": 10, "name": "Sprint 1", "state": "closed",
            "startDate": "2024-01-01T00:00:00.000Z", "endDate": "2024-01-14T00:00:00.000Z",
        }]
        iss2 = sample.subtask()
        iss2["key"] = "DEMO-2"
        iss2["fields"]["status"] = {"name": "Done"}
        iss2["fields"]["customfield_10020"] = [{
            "id": 20, "name": "Sprint 1", "state": "closed",
            "startDate": "2025-01-01T00:00:00.000Z", "endDate": "2025-01-14T00:00:00.000Z",
        }]
        write_json(os.path.join(folder, "issues", "DEMO-1.json"), iss1)
        write_json(os.path.join(folder, "issues", "DEMO-2.json"), iss2)

        cfg = cfg_for(folder)
        cfg["mapping"]["history"] = False
        ado = sample.FakeAdo()
        stats = import_items(ado, cfg, USERS, log=lambda *a, **k: None)
        self.assertEqual(stats["created"], 2)
        self.assertIn(("Sprint 1 (10)", "2024-01-01T00:00:00.000Z", "2024-01-14T00:00:00.000Z"), ado.iterations)
        self.assertIn(("Sprint 1 (20)", "2025-01-01T00:00:00.000Z", "2025-01-14T00:00:00.000Z"), ado.iterations)
        self.assertEqual(ado.items[101]["fields"]["System.IterationPath"], "Shop\\Sprint 1 (10)")
        self.assertEqual(ado.items[102]["fields"]["System.IterationPath"], "Shop\\Sprint 1 (20)")

    def test_round3_batched_import_reuses_duplicates_and_verifies_only_current_batch(self):
        # Serious #5: Across batches, duplicate search runs once (not repeated per batch) and
        # batch-scoped verify checks only the keys in the current batch.
        from jira2ado.util import write_json
        folder = tempfile.mkdtemp()
        write_json(os.path.join(folder, "meta.json"), sample.META)
        keys = [f"DEMO-{i}" for i in range(1, 7)]
        write_json(os.path.join(folder, "keys.json"), keys)
        for k in keys:
            iss = sample.subtask()
            iss["key"] = k
            iss["fields"]["parent"] = None
            write_json(os.path.join(folder, "issues", f"{k}.json"), iss)

        cfg = cfg_for(folder)
        cfg["paths"]["state_dir"] = tempfile.mkdtemp()
        q = lambda *a, **k: None
        ado = sample.FakeAdo()
        get_item_calls = []
        orig_get = ado.get_item
        def tracked_get(item_id):
            get_item_calls.append(item_id)
            return orig_get(item_id)
        ado.get_item = tracked_get

        report = os.path.join(cfg["paths"]["state_dir"], "verify.csv")
        # Run 3 batches of 2 tickets (6 tickets total)
        for _ in range(3):
            st = import_items(ado, cfg, USERS, limit=2, log=q)
            self.assertEqual(len(st["batch_keys"]), 2)
            verify(ado, cfg, USERS, report, log=q, only=st["batch_keys"])

        # Duplicate search was only called ONCE on Batch 1 (6 tickets total, not 6 + 4 + 2 = 12)
        self.assertEqual(len(ado.key_searches), 1)
        self.assertEqual(len(ado.key_searches[0][0]), 6)
        # Verify called get_item only 2 times per batch = 6 times total (not 2 + 4 + 6 = 12)
        self.assertEqual(len(get_item_calls), 6)

    def test_round3_stable_run_folders_and_tick_order_independence(self):
        # Medium #6: Run folder paths stay stable when include_done or board name changes,
        # and sprint folder names are independent of sprint tick order.
        from jira2ado.util import sprint_folder_slug
        s1 = {"id": 101, "name": "Sprint 27"}
        s2 = {"id": 102, "name": "Sprint 28"}
        self.assertEqual(sprint_folder_slug([s1, s2]), sprint_folder_slug([s2, s1]))

    def test_round3_sync_rollback_preserves_post_sync_edits_and_matches_attachment_ids(self):
        # Open items:
        # 1) Two attachments with the same filename have distinct IDs and are both attached.
        # 2) Comments reformatted by ADO (e.g. <br/> instead of <br>) are not duplicated on re-sync.
        # 3) Rolling back a sync does not undo edits made in ADO after the sync or write read-only System.* fields.
        from jira2ado.importer import rollback_run
        from jira2ado.util import write_json
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        os.makedirs(os.path.join(folder, "attachments", "DEMO-1"), exist_ok=True)
        with open(os.path.join(folder, "attachments", "DEMO-1", "901_shot.png"), "wb") as fh:
            fh.write(b"png-bytes-2")
        iss = sample.story()
        iss["fields"]["attachment"].append({
            "id": "901", "filename": "shot.png", "created": "2024-01-03T11:00:00.000-0500",
            "author": sample.U1, "local_path": "attachments/DEMO-1/901_shot.png",
        })
        write_json(os.path.join(folder, "issues", "DEMO-1.json"), iss)

        cfg = cfg_for(folder)
        cfg["mapping"]["history"] = False
        q = lambda *a, **k: None
        ado = sample.FakeAdo()
        import_items(ado, cfg, USERS, only={"DEMO-1"}, log=q)
        # Both attachments named shot.png (IDs 900 and 901) were uploaded
        self.assertEqual(len(ado.uploads), 2)

        # Simulate ADO reformatting comment HTML tags and user changing Title & State in ADO before sync
        ado.items[101]["comments"] = [(d, c.replace("<p>", "<div>").replace("</p>", "</div>")) for d, c in ado.items[101]["comments"]]
        ado.items[101]["fields"]["System.State"] = "New"
        ado.items[101]["fields"]["System.Title"] = "Pre-Sync Title"

        sync_cfg = copy.deepcopy(cfg)
        sync_cfg["paths"]["state_dir"] = tempfile.mkdtemp()
        sync_cfg["mapping"]["ticket_actions"] = {"DEMO-1": "sync"}
        comment_count_before = len(ado.items[101]["comments"])
        import_items(ado, sync_cfg, USERS, only={"DEMO-1"}, log=q)
        # Neither attachment was re-uploaded and existing comments were not duplicated (only the 1 delta-sync note added)
        self.assertEqual(len(ado.uploads), 2)
        self.assertEqual(len(ado.items[101]["comments"]), comment_count_before + 1)
        self.assertEqual(ado.items[101]["fields"]["System.State"], "Done")
        self.assertEqual(ado.items[101]["fields"]["System.Title"], "Checkout fails on retry")

        # Now user edits System.Title in ADO AFTER the sync!
        ado.items[101]["fields"]["System.Title"] = "Post-Sync Manual Edit in ADO"
        rb = rollback_run(ado, sync_cfg, log=q)
        self.assertEqual(len(rb["restored"]), 1)
        # System.State (not edited after sync) is reverted to "New", while System.Title (edited after sync) is preserved!
        self.assertEqual(ado.items[101]["fields"]["System.State"], "New")
        self.assertEqual(ado.items[101]["fields"]["System.Title"], "Post-Sync Manual Edit in ADO")

    def test_sql_server_xx5z_rounding_bump_and_changeddate_auto_retry(self):
        # Group A fix: .xx5Z timestamps rounded to .xx7Z by SQL Server DATETIME never collide,
        # and a server-side ChangedDate rejection auto-recovers by reading live System.ChangedDate + 20ms.
        from jira2ado.ado_client import AdoError
        from jira2ado.util import bump, iso, parse_dt, write_json

        last = parse_dt("2023-08-24T21:02:44.325Z")
        nxt = bump(parse_dt("2023-08-24T21:02:44.100Z"), last)
        # SQL Server rounds .325Z up to .327Z; 10ms step lands at .335Z (> .327Z)
        self.assertEqual(iso(nxt), "2023-08-24T21:02:44.335Z")
        self.assertGreater(iso(nxt), "2023-08-24T21:02:44.327Z")

        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        iss = sample.story()
        iss["changelog"][0]["created"] = "2024-01-03T09:00:00.325-0500"
        iss["attachments_extra"] = []
        iss["fields"]["attachment"][0]["created"] = "2024-01-03T09:00:00.100-0500"
        write_json(os.path.join(folder, "issues", "DEMO-1.json"), iss)

        cfg = cfg_for(folder)
        ado = sample.FakeAdo()
        orig_update = ado.update_item
        injected = [False]

        def picky_update(item_id, ops):
            if not injected[0]:
                injected[0] = True
                # Simulate SQL Server having rounded System.ChangedDate 50ms ahead on the server
                ado.items[item_id]["fields"]["System.ChangedDate"] = "2024-01-03T14:00:00.900Z"
                ado.items[item_id]["last"] = "2024-01-03T14:00:00.900Z"
                raise AdoError("TF400073: Invalid field value System.ChangedDate", 400)
            return orig_update(item_id, ops)

        ado.update_item = picky_update
        stats = import_items(ado, cfg, USERS, only={"DEMO-1"}, log=lambda *a, **k: None)
        self.assertEqual(stats["failed"], [])
        self.assertEqual(stats["created"], 1)
        self.assertTrue(injected[0])

    def test_oversized_60mb_attachment_explicitly_skipped_and_verify_passes(self):
        # Group C fix: > 60 MB files are explicitly marked skipped in warnings, noted on the ADO item,
        # and recorded in verify.csv attachments_skipped without failing verification.
        import csv
        from unittest.mock import patch

        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        # 900_shot.png has 9 bytes ("png-bytes"); patch MAX_ATTACHMENT to 4 bytes to simulate > 60 MB
        cfg = cfg_for(folder)
        q = lambda *a, **k: None
        ado = sample.FakeAdo()
        report = os.path.join(folder, "verify.csv")

        with patch("jira2ado.importer.MAX_ATTACHMENT", 4), patch("jira2ado.verify.MAX_ATTACHMENT", 4):
            stats = import_items(ado, cfg, USERS, log=q)
            import_links(ado, cfg, log=q)
            total, bad = verify(ado, cfg, USERS, report, log=q)

        self.assertEqual(len(ado.uploads), 0)
        self.assertTrue(any("skipped (> 60 MB ADO limit): shot.png" in w for w in stats["warnings"]))
        self.assertTrue(any("Skipped attachment" in c for _, c in ado.items[101]["comments"]))
        self.assertEqual((total, bad), (2, 0))
        with open(report, newline="", encoding="utf-8") as fh:
            rows = {r["jira_key"]: r for r in csv.DictReader(fh)}
        self.assertEqual(rows["DEMO-1"]["attachments_expected"], "0")
        self.assertEqual(rows["DEMO-1"]["attachments_found"], "0")
        self.assertEqual(rows["DEMO-1"]["attachments_skipped"], "1")
        self.assertEqual(rows["DEMO-1"]["problems"], "")

    def test_noop_intermediate_revisions_coalesced_and_in_progress_run_preserved(self):
        # Group B + Planner speedup: back-to-back Jira sub-statuses mapping to the same ADO state
        # are coalesced on fresh runs, while an already-started run without coalesced_noops preserves indices.
        from jira2ado.importer import load_meta
        from jira2ado.util import write_json

        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        iss = sample.subtask()
        iss["key"] = "DEMO-99"
        iss["fields"]["issuetype"] = {"name": "Story"}
        iss["fields"]["status"] = {"name": "Done"}
        iss["changelog"] = [
            {"created": "2024-01-06T10:00:00.000-0500", "author": sample.U1,
             "items": [sample.item("status", "To Do", "In Progress")]},
            {"created": "2024-01-06T11:00:00.000-0500", "author": sample.U1,
             "items": [sample.item("status", "In Progress", "In Review")]},  # also maps to Committed -> pure no-op!
            {"created": "2024-01-06T12:00:00.000-0500", "author": sample.U1,
             "items": [sample.item("status", "In Review", "Done")]},
        ]
        write_json(os.path.join(folder, "issues", "DEMO-99.json"), iss)

        cfg = cfg_for(folder)
        cfg["mapping"]["history_notes"] = False
        sdir = tempfile.mkdtemp()
        cfg["paths"]["state_dir"] = sdir

        fresh_revs = Planner(cfg, load_meta(cfg), USERS).plan(iss)["revisions"]
        # Create (New) -> Committed -> Done = 3 revisions (In Review duplicate Committed skipped)
        self.assertEqual(len(fresh_revs), 3)

        # Simulate an in-progress run created before coalesced_noops
        with open(os.path.join(sdir, "import-state.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"t": "rev", "k": "DEMO-99", "id": 500, "n": 2, "d": "2024-01-06T15:00:00.000Z"}\n')
        write_json(os.path.join(sdir, "preview.json"), {"issues": 1, "tickets": []})
        legacy_revs = Planner(cfg, load_meta(cfg), USERS).plan(iss)["revisions"]
        self.assertEqual(len(legacy_revs), 4)

    def test_parallel_import_workers_and_bulk_batch_verify_and_export(self):
        import threading
        from jira2ado.exporter import export
        from jira2ado.util import write_json

        # 1. Bulk export via search_issues_batch
        exp_dir = tempfile.mkdtemp()
        cfg = cfg_for(exp_dir)
        cfg["jira"]["jql"] = 'project = "DEMO"'
        cfg["mapping"]["import_workers"] = 4

        class BulkFakeJira:
            def __init__(self):
                self.batch_calls = 0
                self.single_calls = 0
            def fields(self):
                return [{"id": "customfield_10020", "name": "Sprint"}, {"id": "customfield_10016", "name": "Story point estimate"}]
            def statuses(self):
                return [{"name": "In Review", "statusCategory": {"key": "indeterminate"}}]
            def search_key_timestamps(self, jql):
                return [(f"DEMO-{i}", "2024-01-06T09:00:00.000-0500") for i in range(1, 9)]
            def search_issues_batch(self, keys):
                self.batch_calls += 1
                out = {}
                for k in keys:
                    st = sample.subtask()
                    st["key"] = k
                    st["fields"]["parent"] = None
                    st["changelog"] = {"total": 0, "histories": []}
                    st["fields"]["comment"] = {"total": 0, "comments": []}
                    st["fields"]["worklog"] = {"total": 0, "worklogs": []}
                    out[k] = st
                return out
            def issue(self, key):
                self.single_calls += 1
                raise AssertionError("Should use search_issues_batch instead of single issue GET")
            def remote_links(self, key):
                return []
            def dev_links(self, issue_id):
                return []

        fj = BulkFakeJira()
        done_cnt, skip_cnt = export(fj, cfg, log=lambda *a, **k: None)
        self.assertEqual((done_cnt, skip_cnt), (8, 0))
        self.assertEqual(fj.batch_calls, 1)
        self.assertEqual(fj.single_calls, 0)

        # 2. Parallel 4-worker import + bulk get_items_batch verify
        class ThreadSafeFakeAdo(sample.FakeAdo):
            def __init__(self):
                super().__init__()
                self._lk = threading.Lock()
                self.batch_get_calls = 0
            def create_item(self, wit, ops):
                with self._lk:
                    return super().create_item(wit, ops)
            def update_item(self, item_id, ops):
                with self._lk:
                    return super().update_item(item_id, ops)
            def get_items_batch(self, item_ids):
                self.batch_get_calls += 1
                return {iid: self.get_item(iid) for iid in item_ids}

        ado = ThreadSafeFakeAdo()
        stats = import_items(ado, cfg, USERS, log=lambda *a, **k: None)
        self.assertEqual(stats["created"], 8)
        self.assertEqual(stats["failed"], [])
        report = os.path.join(exp_dir, "verify.csv")
        total, bad = verify(ado, cfg, USERS, report, log=lambda *a, **k: None)
        self.assertEqual((total, bad), (8, 0))
        self.assertEqual(ado.batch_get_calls, 1)

    def test_round4_partial_only_import_preserves_sprint_collision_disambiguation(self):
        # Finding #2 & #8: Importing only={"DEMO-1"} when DEMO-2 has a colliding "Sprint 1" (id 20)
        # still creates the suffixed iteration "Sprint 1 (10)" and surfaces subscription warnings.
        from jira2ado.util import write_json
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        iss1 = sample.story()
        iss1["key"] = "DEMO-1"
        iss1["changelog"] = []
        iss1["fields"]["customfield_10020"] = [{
            "id": 10, "name": "Sprint 1", "state": "closed",
            "startDate": "2024-01-01T00:00:00.000Z", "endDate": "2024-01-14T00:00:00.000Z",
        }]
        iss2 = sample.subtask()
        iss2["key"] = "DEMO-2"
        iss2["fields"]["status"] = {"name": "Done"}
        iss2["fields"]["customfield_10020"] = [{
            "id": 20, "name": "Sprint 1", "state": "closed",
            "startDate": "2025-01-01T00:00:00.000Z", "endDate": "2025-01-14T00:00:00.000Z",
        }]
        write_json(os.path.join(folder, "issues", "DEMO-1.json"), iss1)
        write_json(os.path.join(folder, "issues", "DEMO-2.json"), iss2)

        cfg = cfg_for(folder)
        cfg["mapping"]["history"] = False
        ado = sample.FakeAdo()
        ado.subscribe_team_iteration = lambda path, team=None: False
        logs = []
        stats = import_items(ado, cfg, USERS, only={"DEMO-1"}, log=logs.append)
        self.assertEqual(stats["created"], 1)
        self.assertIn(("Sprint 1 (10)", "2024-01-01T00:00:00.000Z", "2024-01-14T00:00:00.000Z"), ado.iterations)
        self.assertEqual(ado.items[101]["fields"]["System.IterationPath"], "Shop\\Sprint 1 (10)")
        self.assertTrue(any("could not subscribe team sprint board" in m for m in logs))

    def test_round4_delta_export_retries_missing_attachment_on_unchanged_ticket(self):
        # Finding #4: If an attachment download fails on Export 1, Export 2 retries that ticket even
        # when its Jira updated timestamp has not changed.
        from jira2ado.exporter import export
        from jira2ado.util import read_json
        folder = tempfile.mkdtemp()
        cfg = cfg_for(folder)
        cfg["jira"]["jql"] = 'project = "DEMO"'
        story_v1 = sample.story()
        story_v1["fields"]["attachment"] = [{
            "id": "900", "filename": "shot.png", "created": "2024-01-03T10:00:00.000-0500",
            "author": sample.U1, "content": "https://demo.atlassian.net/rest/api/3/attachment/content/900",
        }]

        class FlakyAttJira:
            def __init__(self):
                self.fail_download = True
            def fields(self):
                return [{"id": "customfield_10020", "name": "Sprint"}, {"id": "customfield_10016", "name": "Story point estimate"}]
            def statuses(self):
                return [{"name": "Done", "statusCategory": {"key": "done"}}]
            def search_key_timestamps(self, jql):
                yield "DEMO-1", story_v1["fields"]["updated"]
            def issue(self, key):
                return copy.deepcopy(story_v1)
            def changelog(self, key):
                return []
            def comments(self, key):
                return []
            def worklogs(self, key):
                return []
            def remote_links(self, key):
                return []
            def download(self, url, dest):
                if self.fail_download:
                    raise RuntimeError("transient network error")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(b"png-bytes")

        fj = FlakyAttJira()
        q = lambda *a, **k: None
        done1, skip1 = export(fj, cfg, log=q)
        self.assertEqual((done1, skip1), (1, 0))
        self.assertNotIn("local_path", read_json(os.path.join(folder, "issues", "DEMO-1.json"))["fields"]["attachment"][0])

        # Now network recovers; second delta export (without --force) must retry DEMO-1 and save local_path!
        fj.fail_download = False
        done2, skip2 = export(fj, cfg, log=q)
        self.assertEqual((done2, skip2), (1, 0))
        self.assertIn("local_path", read_json(os.path.join(folder, "issues", "DEMO-1.json"))["fields"]["attachment"][0])

        # Third export skips cleanly now that the attachment is on disk
        done3, skip3 = export(fj, cfg, log=q)
        self.assertEqual((done3, skip3), (0, 1))

    def test_round4_journal_persists_planning_mode_without_preview_json_and_sync_rollback_keeps_manual_links(self):
        # Finding #9 & #1: Legacy journal without preview.json still preserves uncoalesced revisions,
        # and sync rollback removes only migration-added relations while preserving post-sync manual links.
        from jira2ado.importer import load_meta, rollback_run
        from jira2ado.util import write_json
        folder = tempfile.mkdtemp()
        sample.write_export(folder)
        iss = sample.subtask()
        iss["key"] = "DEMO-99"
        iss["fields"]["issuetype"] = {"name": "Story"}
        iss["fields"]["status"] = {"name": "Done"}
        iss["changelog"] = [
            {"created": "2024-01-06T10:00:00.000-0500", "author": sample.U1,
             "items": [sample.item("status", "To Do", "In Progress")]},
            {"created": "2024-01-06T11:00:00.000-0500", "author": sample.U1,
             "items": [sample.item("status", "In Progress", "In Review")]},
            {"created": "2024-01-06T12:00:00.000-0500", "author": sample.U1,
             "items": [sample.item("status", "In Review", "Done")]},
        ]
        write_json(os.path.join(folder, "issues", "DEMO-99.json"), iss)
        sdir = tempfile.mkdtemp()
        cfg = cfg_for(folder)
        cfg["paths"]["state_dir"] = sdir
        cfg["mapping"]["history_notes"] = False
        # Write a legacy journal WITHOUT preview.json at all!
        with open(os.path.join(sdir, "import-state.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"t": "rev", "k": "DEMO-99", "id": 500, "n": 2, "d": "2024-01-06T15:00:00.000Z"}\n')
        self.assertEqual(len(Planner(cfg, load_meta(cfg), USERS).plan(iss)["revisions"]), 4)

        # Finding #1: Delta sync adds DEMO-1's attachment; human adds an external hyperlink in ADO after sync;
        # rollback removes DEMO-1's synced attachment but keeps the human's manual hyperlink!
        q = lambda *a, **k: None
        ado = sample.FakeAdo()
        pre_id = ado.create_item("Product Backlog Item", [
            {"op": "add", "path": "/fields/System.Title", "value": "Existing Item"},
            {"op": "add", "path": "/fields/System.State", "value": "New"},
            {"op": "add", "path": "/fields/System.Tags", "value": "DEMO-1"},
        ])["id"]
        sync_cfg = cfg_for(folder)
        sync_cfg["paths"]["state_dir"] = tempfile.mkdtemp()
        sync_cfg["mapping"]["ticket_actions"] = {"DEMO-1": "sync"}
        import_items(ado, sync_cfg, USERS, only={"DEMO-1"}, log=q)
        # Human adds an unrelated link in ADO after the sync
        manual_url = "https://external.example/manual-spec"
        ado.items[pre_id]["relations"].append({"rel": "Hyperlink", "url": manual_url})
        rb = rollback_run(ado, sync_cfg, log=q)
        self.assertEqual(len(rb["restored"]), 1)
        remaining_urls = [r.get("url") for r in ado.items[pre_id]["relations"]]
        self.assertIn(manual_url, remaining_urls)
        self.assertFalse(any("shot.png" in (u or "") for u in remaining_urls))

    def test_round4_jira_download_rejects_foreign_host_and_surfaces_non_404_errors(self):
        # Finding #5 & #6: JiraClient.download refuses non-Jira hosts, and worklogs/remote_links
        # return [] on 404 (disabled feature) while surfacing 500/401 server errors.
        from jira2ado.jira_client import JiraClient

        class MockResp:
            def __init__(self, code, payload="{}"):
                self.status_code = code
                self.text = payload
                self.headers = {}
            def json(self):
                return json.loads(self.text)

        class MockSession:
            def __init__(self, code):
                self.code = code
                self.auth = None
                self.headers = {}
            def request(self, method, url, **kw):
                return MockResp(self.code, "error")

        jc_404 = JiraClient("https://demo.atlassian.net", "a@b.com", "tok", session=MockSession(404))
        self.assertEqual(jc_404.worklogs("DEMO-1"), [])
        self.assertEqual(jc_404.remote_links("DEMO-1"), [])

        jc_401 = JiraClient("https://demo.atlassian.net", "a@b.com", "tok", session=MockSession(401))
        with self.assertRaises(RuntimeError):
            jc_401.worklogs("DEMO-1")
        with self.assertRaises(RuntimeError):
            jc_401.remote_links("DEMO-1")
        with self.assertRaises(RuntimeError) as ctx:
            jc_401.download("https://evil.example.com/attachment/content/1", os.path.join(tempfile.mkdtemp(), "x"))
        self.assertIn("untrusted attachment URL", str(ctx.exception))


class BoardsToAreasAndTeamsTests(unittest.TestCase):
    """Per-board Area Paths, nested Board\\Sprint Iterations, and auto-created ADO Teams."""

    def _make_issue(self, key, summary="Task", status="In Progress", sprints=None, changelog=None):
        return {
            "key": key,
            "fields": {
                "summary": summary,
                "issuetype": {"name": "Story"},
                "status": {"name": status},
                "priority": {"name": "Medium"},
                "creator": sample.U1, "reporter": sample.U1, "assignee": sample.U1,
                "labels": [], "components": [], "fixVersions": [],
                "customfield_10020": sprints or [],
                "created": "2024-02-01T10:00:00.000-0500",
                "updated": "2024-02-05T10:00:00.000-0500",
                "description": None, "attachment": [], "issuelinks": [],
            },
            "renderedFields": {},
            "changelog": changelog or [],
            "comments": [],
        }

    def _make_fake_ado(self, existing_areas=None, existing_iters=None, existing_teams=None, team_error=None):
        class TeamFakeAdo(sample.FakeAdo):
            def __init__(self):
                super().__init__()
                self._existing_areas = list(existing_areas or [])
                self._existing_iters = list(existing_iters or [])
                self._existing_teams = list(existing_teams or [])
                self.area_paths_calls = 0
                self.iter_paths_calls = 0
                self.created_teams = []
                self.configured_areas = []
                self.configured_backlogs = []
                self.created_iters_full = []

            def area_paths(self):
                self.area_paths_calls += 1
                return list(self._existing_areas)

            def iteration_paths(self):
                self.iter_paths_calls += 1
                return list(self._existing_iters)

            def create_iteration(self, name, start=None, finish=None, parent=None):
                full = f"{parent}\\{name}" if parent else name
                self.created_iters_full.append((full, start, finish))
                return super().create_iteration(name, start=start, finish=finish, parent=parent)

            def teams(self):
                return [{"id": f"tid-{i}", "name": n} for i, n in enumerate(self._existing_teams)]

            def create_team(self, name, description=""):
                if team_error is not None:
                    raise team_error
                self.created_teams.append((name, description))
                self._existing_teams.append(name)
                return {"id": f"new-{name}", "name": name}

            def configure_team_area(self, team, area_path, include_children=True, replace=False):
                self.configured_areas.append((team, area_path, include_children, replace))
                return True

            def configure_team_backlog_iteration(self, team, rel_iter_path):
                self.configured_backlogs.append((team, rel_iter_path))
                return True

        return TeamFakeAdo()

    def test_multi_board_scrum_kanban_orphan_tiebreak_and_team_auto_creation(self):
        from jira2ado.exporter import export
        from jira2ado.util import read_json

        # 3 boards: broad All-Project board (4 tickets), Scrum Alpha (2 tickets), Kanban Ops (1 ticket)
        s_alpha_1 = {"id": 101, "name": "Sprint 1", "state": "active",
                     "startDate": "2024-02-01T00:00:00.000Z", "endDate": "2024-02-14T00:00:00.000Z", "originBoardId": 10}
        s_alpha_closed = {"id": 100, "name": "Sprint 0", "state": "closed",
                          "startDate": "2024-01-15T00:00:00.000Z", "endDate": "2024-01-31T00:00:00.000Z", "originBoardId": 10}
        issues_map = {
            "APP-1": self._make_issue("APP-1", "Active Scrum story", "In Progress", sprints=[s_alpha_1]),
            "APP-2": self._make_issue("APP-2", "Open ticket from closed sprint (backlog rule)", "To Do", sprints=[s_alpha_closed]),
            "APP-3": self._make_issue("APP-3", "Kanban card", "In Progress", sprints=[]),
            "APP-4": self._make_issue("APP-4", "Orphaned ticket", "To Do", sprints=[]),
        }

        class FakeJira:
            def fields(self):
                return [{"id": "customfield_10020", "name": "Sprint"}]
            def statuses(self):
                return [
                    {"name": "To Do", "statusCategory": {"key": "new"}},
                    {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                    {"name": "Done", "statusCategory": {"key": "done"}},
                ]
            def search_key_timestamps(self, jql):
                return [(k, "2024-02-05T10:00:00.000-0500") for k in sorted(issues_map)]
            def search_keys(self, jql):
                if "ALL_BOARD" in jql:
                    return ["APP-1", "APP-2", "APP-3", "APP-99"]  # broad board; APP-99 outside export
                if "SCRUM_ALPHA" in jql:
                    return ["APP-1", "APP-2"]
                if "KANBAN_OPS" in jql:
                    return ["APP-3"]
                return []
            def boards(self, project=None):
                return [
                    {"id": 1, "name": "All Project Board", "type": "kanban"},
                    {"id": 10, "name": "Scrum Alpha", "type": "scrum"},
                    {"id": 20, "name": "Kanban Ops", "type": "kanban"},
                ]
            def board_filter_jql(self, board_id):
                return {1: "labels = ALL_BOARD", 10: "labels = SCRUM_ALPHA", 20: "labels = KANBAN_OPS"}[board_id]
            def board_sprints(self, board_id, board_name=None):
                if board_id == 10:
                    return [s_alpha_1, s_alpha_closed]
                return []
            def issue(self, key):
                return copy.deepcopy(issues_map[key])

        folder = tempfile.mkdtemp()
        cfg = cfg_for(folder)
        cfg["jira"]["jql"] = 'project = "APP" ORDER BY key ASC'
        cfg["run"] = {"mode": "project", "from": "APP", "to": "Shop"}
        cfg["mapping"]["boards_to_areas"] = True
        cfg["mapping"]["history"] = False
        cfg["ado"]["create_board_teams"] = True
        q = lambda *a, **k: None

        export(FakeJira(), cfg, log=q)
        meta = read_json(os.path.join(folder, "meta.json"))
        # Tie-breaker: APP-3 matched both All Project Board (3 exported matches) & Kanban Ops (1 match) -> Kanban Ops wins
        self.assertEqual(meta["ticket_boards"]["APP-3"], "Kanban Ops")
        self.assertEqual(meta["ticket_boards"]["APP-2"], "Scrum Alpha")
        self.assertNotIn("APP-4", meta["ticket_boards"])
        self.assertEqual(meta["sprint_boards"]["101"], "Scrum Alpha")

        ado = self._make_fake_ado()
        stats = import_items(ado, cfg, USERS, log=q)
        self.assertEqual((stats["created"], stats["failed"]), (4, []))

        by_key = {}
        for it in ado.items.values():
            tags = it["fields"].get("System.Tags", "")
            for k in issues_map:
                if k in tags:
                    by_key[k] = it["fields"]
        # Sprinted ticket -> Board Area + nested Board\Sprint Iteration
        self.assertEqual(by_key["APP-1"]["System.AreaPath"], "Shop\\Scrum Alpha")
        self.assertEqual(by_key["APP-1"]["System.IterationPath"], "Shop\\Scrum Alpha\\Sprint 1")
        # Open ticket whose only sprint is closed -> Backlog Rule puts it at Board Area & Board Iteration
        self.assertEqual(by_key["APP-2"]["System.AreaPath"], "Shop\\Scrum Alpha")
        self.assertEqual(by_key["APP-2"]["System.IterationPath"], "Shop\\Scrum Alpha")
        # Kanban ticket -> Board Area & Board Iteration
        self.assertEqual(by_key["APP-3"]["System.AreaPath"], "Shop\\Kanban Ops")
        self.assertEqual(by_key["APP-3"]["System.IterationPath"], "Shop\\Kanban Ops")
        # Orphaned ticket -> Project root for both
        self.assertEqual(by_key["APP-4"]["System.AreaPath"], "Shop")
        self.assertEqual(by_key["APP-4"]["System.IterationPath"], "Shop")

        # Verify Areas, nested Iterations, and Teams were created & configured
        self.assertEqual(ado.area_paths_calls, 1)
        self.assertEqual(ado.iter_paths_calls, 1)
        self.assertEqual(sorted(ado.areas), ["Kanban Ops", "Scrum Alpha"])
        created_iter_paths = [p for p, _, _ in ado.created_iters_full]
        self.assertIn("Kanban Ops", created_iter_paths)
        self.assertIn("Scrum Alpha", created_iter_paths)
        self.assertIn("Scrum Alpha\\Sprint 1", created_iter_paths)
        self.assertEqual(sorted(t for t, _ in ado.created_teams), ["Kanban Ops", "Scrum Alpha"])
        self.assertIn(("Scrum Alpha", "Shop\\Scrum Alpha", True, True), ado.configured_areas)
        self.assertIn(("Kanban Ops", "Shop\\Kanban Ops", True, True), ado.configured_areas)
        self.assertIn(("Scrum Alpha", "Scrum Alpha"), ado.configured_backlogs)
        self.assertIn(("Kanban Ops", "Kanban Ops"), ado.configured_backlogs)
        # Sprint 1 subscribed specifically to the Scrum Alpha team!
        self.assertIn(("Scrum Alpha\\Sprint 1", "Scrum Alpha"), ado.subscribed_iterations)

        # Verify passes with 0 problems (including System.AreaPath check)
        total, bad = verify(ado, cfg, USERS, os.path.join(folder, "verify.csv"), log=q)
        self.assertEqual((total, bad), (4, 0))

        # Re-running import is 100% idempotent (no duplicate teams, areas, or iterations)
        ado.created_teams.clear()
        ado.created_iters_full.clear()
        ado.areas.clear()
        again = import_items(ado, cfg, USERS, log=q)
        self.assertEqual((again["created"], again["complete"]), (0, 4))
        self.assertEqual(ado.created_teams, [])
        self.assertEqual(ado.created_iters_full, [])
        self.assertEqual(ado.areas, [])

    def test_same_name_sprints_across_boards_vs_same_board_collision(self):
        # Board A has TWO "Sprint 1"s (ids 101 & 102) -> disambiguated on Board A.
        # Board B has ONE "Sprint 1" (id 201) -> stays clean "Board B\\Sprint 1" without (201)!
        meta = {
            **sample.META,
            "boards": [
                {"id": 10, "name": "Board A", "type": "scrum", "area": "Board A"},
                {"id": 20, "name": "Board B", "type": "scrum", "area": "Board B"},
            ],
            "sprint_boards": {"101": "Board A", "102": "Board A", "201": "Board B"},
            "ticket_boards": {},
            "sprints": [
                {"id": 101, "name": "Sprint 1", "state": "closed"},
                {"id": 102, "name": "Sprint 1", "state": "active"},
                {"id": 201, "name": "Sprint 1", "state": "active"},
            ],
        }
        i_a1 = self._make_issue("X-1", "A1", "Done", sprints=[{"id": 101, "name": "Sprint 1", "state": "closed"}])
        i_a2 = self._make_issue("X-2", "A2", "In Progress", sprints=[{"id": 102, "name": "Sprint 1", "state": "active"}])
        i_b1 = self._make_issue("X-3", "B1", "In Progress", sprints=[{"id": 201, "name": "Sprint 1", "state": "active"}])
        cfg = cfg_for("x")
        cfg["mapping"]["boards_to_areas"] = True
        cfg["mapping"]["history"] = False

        planner = Planner(cfg, meta, USERS, issues=[i_a1, i_a2, i_b1])
        p_a1 = planner.plan(i_a1)["revisions"][0]["fields"]
        p_a2 = planner.plan(i_a2)["revisions"][0]["fields"]
        p_b1 = planner.plan(i_b1)["revisions"][0]["fields"]

        self.assertEqual(p_a1["System.AreaPath"], "Shop\\Board A")
        self.assertEqual(p_a1["System.IterationPath"], "Shop\\Board A\\Sprint 1 (101)")
        self.assertEqual(p_a2["System.AreaPath"], "Shop\\Board A")
        self.assertEqual(p_a2["System.IterationPath"], "Shop\\Board A\\Sprint 1 (102)")
        self.assertEqual(p_b1["System.AreaPath"], "Shop\\Board B")
        self.assertEqual(p_b1["System.IterationPath"], "Shop\\Board B\\Sprint 1")

        # History replay: ticket moved from Board A's Sprint 1 (id 101) to Board B's Sprint 1 (id 201)
        i_moved = self._make_issue(
            "X-4", "Moved across boards", "In Progress",
            sprints=[
                {"id": 101, "name": "Sprint 1", "state": "closed"},
                {"id": 201, "name": "Sprint 1", "state": "active"},
            ],
            changelog=[
                {"created": "2024-02-02T10:00:00.000-0500", "author": sample.U1,
                 "items": [sample.item("Sprint", "", "Sprint 1", "", "101")]},
                {"created": "2024-02-04T10:00:00.000-0500", "author": sample.U1,
                 "items": [sample.item("Sprint", "Sprint 1", "Sprint 1, Sprint 1", "101", "101, 201")]},
            ],
        )
        cfg_hist = cfg_for("x")
        cfg_hist["mapping"]["boards_to_areas"] = True
        cfg_hist["mapping"]["history"] = True
        pl_hist = Planner(cfg_hist, meta, USERS, issues=[i_a1, i_a2, i_b1, i_moved])
        revs_m = pl_hist.plan(i_moved)["revisions"]
        self.assertEqual(revs_m[0]["fields"]["System.AreaPath"], "Shop\\Board B")
        self.assertEqual(revs_m[0]["fields"]["System.IterationPath"], "Shop\\Board B")
        self.assertEqual(revs_m[1]["fields"]["System.IterationPath"], "Shop\\Board A\\Sprint 1 (101)")
        self.assertEqual(revs_m[2]["fields"]["System.IterationPath"], "Shop\\Board B\\Sprint 1")
        self.assertFalse(any("did not add up" in w for w in pl_hist.warnings))

    def test_duplicate_board_names_and_zero_board_fallback(self):
        from jira2ado.exporter import export
        from jira2ado.util import read_json

        # 1. Duplicate sanitized board names ("Platform/Core" and "Platform&Core") get (id) suffix
        iss = self._make_issue("DUP-1", "Story", "In Progress")

        class DupBoardJira:
            def fields(self):
                return [{"id": "customfield_10020", "name": "Sprint"}]
            def statuses(self):
                return [{"name": "In Progress", "statusCategory": {"key": "indeterminate"}}]
            def search_key_timestamps(self, jql):
                return [("DUP-1", "2024-02-05T10:00:00.000-0500")]
            def boards(self, project=None):
                return [
                    {"id": 11, "name": "Platform/Core", "type": "kanban"},
                    {"id": 22, "name": "Platform&Core", "type": "kanban"},
                ]
            def board_keys(self, board_id):
                return ["DUP-1"] if board_id == 22 else []
            def issue(self, key):
                return copy.deepcopy(iss)

        folder = tempfile.mkdtemp()
        cfg = cfg_for(folder)
        cfg["jira"]["jql"] = 'project = "DUP"'
        cfg["mapping"]["boards_to_areas"] = True
        export(DupBoardJira(), cfg, log=lambda *a, **k: None)
        meta = read_json(os.path.join(folder, "meta.json"))
        self.assertEqual([b["area"] for b in meta["boards"]], ["Platform-Core (11)", "Platform-Core (22)"])
        self.assertEqual(meta["ticket_boards"]["DUP-1"], "Platform-Core (22)")

        # 2. Zero-board project falls back cleanly to <ADO Project>
        class ZeroBoardJira(DupBoardJira):
            def boards(self, project=None):
                return []

        folder0 = tempfile.mkdtemp()
        cfg0 = cfg_for(folder0)
        cfg0["jira"]["jql"] = 'project = "DUP"'
        cfg0["mapping"]["boards_to_areas"] = True
        export(ZeroBoardJira(), cfg0, log=lambda *a, **k: None)
        meta0 = read_json(os.path.join(folder0, "meta.json"))
        self.assertEqual(meta0["boards"], [])
        plan0 = Planner(cfg0, meta0, USERS).plan(iss)["revisions"][0]["fields"]
        self.assertEqual(plan0["System.AreaPath"], "Shop")
        self.assertEqual(plan0["System.IterationPath"], "Shop")

    def test_explicit_area_and_iteration_overrides_win_over_boards(self):
        meta = {
            **sample.META,
            "boards": [{"id": 10, "name": "Board A", "type": "scrum", "area": "Board A"}],
            "sprint_boards": {"101": "Board A"},
            "ticket_boards": {"OV-1": "Board A"},
        }
        iss = self._make_issue("OV-1", "Override test", "In Progress",
                               sprints=[{"id": 101, "name": "Sprint 1", "state": "active"}])
        cfg = cfg_for("x")
        cfg["mapping"]["boards_to_areas"] = True
        cfg["mapping"]["history"] = False
        cfg["ado"]["area_path"] = "Shop\\ExplicitArea"
        cfg["mapping"]["iteration_override"] = "Release 9"

        f = Planner(cfg, meta, USERS).plan(iss)["revisions"][0]["fields"]
        self.assertEqual(f["System.AreaPath"], "Shop\\ExplicitArea")
        self.assertEqual(f["System.IterationPath"], "Shop\\Release 9")

    def test_team_403_permission_error_warns_and_continues_importing_areas_and_items(self):
        from jira2ado.ado_client import AdoError
        from jira2ado.util import write_json

        folder = tempfile.mkdtemp()
        meta = {
            **sample.META,
            "boards": [
                {"id": 10, "name": "Team A", "type": "scrum", "area": "Team A"},
                {"id": 20, "name": "Team B", "type": "kanban", "area": "Team B"},
            ],
            "sprint_boards": {"101": "Team A"},
            "ticket_boards": {"P-1": "Team A", "P-2": "Team B"},
        }
        write_json(os.path.join(folder, "meta.json"), meta)
        write_json(os.path.join(folder, "keys.json"), ["P-1", "P-2"])
        write_json(os.path.join(folder, "issues", "P-1.json"),
                   self._make_issue("P-1", "Story A", "In Progress", sprints=[{"id": 101, "name": "Sprint 1", "state": "active"}]))
        write_json(os.path.join(folder, "issues", "P-2.json"),
                   self._make_issue("P-2", "Kanban B", "To Do", sprints=[]))

        cfg = cfg_for(folder)
        cfg["mapping"]["boards_to_areas"] = True
        cfg["mapping"]["history"] = False
        cfg["ado"]["create_board_teams"] = True

        ado = self._make_fake_ado(team_error=AdoError("HTTP 403 missing Project & Team scope", 403))
        stats = import_items(ado, cfg, USERS, log=lambda *a, **k: None)
        # Work items, Areas, and nested Iterations STILL succeed!
        self.assertEqual((stats["created"], stats["failed"]), (2, []))
        self.assertEqual(sorted(ado.areas), ["Team A", "Team B"])
        self.assertIn("Team A\\Sprint 1", [p for p, _, _ in ado.created_iters_full])
        self.assertTrue(any("Project and Team" in w for w in stats["warnings"]))

    def test_existing_team_and_existing_nodes_not_recreated_and_ado_client_team_apis(self):
        from jira2ado.ado_client import AdoClient
        from jira2ado.util import write_json

        # 1. Importer skips creating already-existing areas/iterations/teams and uses replace=False for pre-existing team
        folder = tempfile.mkdtemp()
        meta = {
            **sample.META,
            "boards": [{"id": 10, "name": "Existing Team", "type": "kanban", "area": "Existing Team"}],
            "sprint_boards": {},
            "ticket_boards": {"EX-1": "Existing Team"},
        }
        write_json(os.path.join(folder, "meta.json"), meta)
        write_json(os.path.join(folder, "keys.json"), ["EX-1"])
        write_json(os.path.join(folder, "issues", "EX-1.json"), self._make_issue("EX-1", "Card", "To Do"))
        cfg = cfg_for(folder)
        cfg["mapping"]["boards_to_areas"] = True
        cfg["mapping"]["history"] = False
        ado = self._make_fake_ado(
            existing_areas=["Existing Team"],
            existing_iters=["Existing Team"],
            existing_teams=["Existing Team"],
        )
        stats = import_items(ado, cfg, USERS, log=lambda *a, **k: None)
        self.assertEqual((stats["created"], stats["failed"]), (1, []))
        self.assertIsNone(ado.areas)  # area already existed, create_area never called
        self.assertEqual(ado.created_iters_full, [])  # iteration already existed
        self.assertEqual(ado.created_teams, [])  # team already existed
        # Pre-existing team configured with replace=False so existing team areas are preserved
        self.assertEqual(ado.configured_areas, [("Existing Team", "Shop\\Existing Team", True, False)])
        self.assertEqual(ado.configured_backlogs, [("Existing Team", "Existing Team")])

        # 2. AdoClient REST methods for create_team, configure_team_area, configure_team_backlog_iteration, 409 subscribe
        calls = []

        class MockResp:
            def __init__(self, code, payload):
                self.status_code = code
                self.headers = {"Content-Type": "application/json"}
                self._p = payload
                self.text = json.dumps(payload)
            def json(self):
                return self._p

        class MockSession:
            headers = {}
            def request(self, method, url, **kw):
                calls.append((method, url, kw.get("json")))
                if method == "POST" and url.endswith("/teams"):
                    return MockResp(201, {"id": "t-1", "name": kw["json"]["name"]})
                if method == "GET" and url.endswith("/teamfieldvalues"):
                    return MockResp(200, {"defaultValue": "Shop\\Old", "values": [{"value": "Shop\\Old", "includeChildren": True}]})
                if method == "PATCH" and url.endswith("/teamfieldvalues"):
                    return MockResp(200, kw["json"])
                if method == "GET" and "/classificationnodes/Iterations/" in url:
                    return MockResp(200, {"identifier": "guid-iter-1", "name": "Board A"})
                if method == "PATCH" and url.endswith("/teamsettings"):
                    return MockResp(200, kw["json"])
                if method == "POST" and url.endswith("/teamsettings/iterations"):
                    return MockResp(409, {"message": "already exists"})
                return MockResp(200, {})

        client = AdoClient("https://dev.azure.com/org", "Shop", "tok", session=MockSession())
        created = client.create_team("Board A", description="From Jira")
        self.assertEqual(created["name"], "Board A")
        # replace=True sets only the board area
        client.configure_team_area("Board A", "Shop\\Board A", replace=True)
        self.assertEqual(calls[-1][2], {
            "defaultValue": "Shop\\Board A",
            "values": [{"value": "Shop\\Board A", "includeChildren": True}],
        })
        # replace=False merges with pre-existing team areas
        client.configure_team_area("Board A", "Shop\\Board A", replace=False)
        self.assertEqual(calls[-1][2]["defaultValue"], "Shop\\Old")
        self.assertEqual(len(calls[-1][2]["values"]), 2)
        # Backlog iteration sets GUID from classification node
        self.assertTrue(client.configure_team_backlog_iteration("Board A", "Board A"))
        self.assertEqual(calls[-1][2], {"backlogIteration": "guid-iter-1"})
        # 409 on already-subscribed sprint iteration returns True cleanly
        self.assertTrue(client.subscribe_team_iteration("Board A\\Sprint 1", team="Board A"))

    def test_prepare_project_mode_defaults_and_cli_flags(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        import yaml
        from jira2ado import ado_client, cli, jira_client

        tmp = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        os.chdir(tmp)
        old_env = {k: os.environ.get(k) for k in ("JIRA_EMAIL", "JIRA_API_TOKEN", "ADO_PAT", "JIRA_BASE_URL", "ADO_ORG_URL")}
        orig_jira, orig_ado = jira_client.JiraClient, ado_client.AdoClient

        class StubJira:
            def __init__(self, *a, **k):
                pass
            def project_sprints(self, *a, **k):
                return []

        class StubAdo:
            def __init__(self, *a, **k):
                pass
            def project_name(self):
                return "Shop"

        try:
            os.environ.update(JIRA_EMAIL="a@b.com", JIRA_API_TOKEN="t", ADO_PAT="p",
                              JIRA_BASE_URL="https://demo.atlassian.net", ADO_ORG_URL="https://dev.azure.com/org")
            jira_client.JiraClient, ado_client.AdoClient = StubJira, StubAdo

            def run_prep(*extra):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    cli.main(["prepare", "--from", "APP", "--to", "Shop", *extra])
                cfg_path = out.getvalue().strip()
                with open(cfg_path, encoding="utf-8") as fh:
                    return yaml.safe_load(fh)

            # Project mode defaults both boards_to_areas and create_board_teams to True
            c_proj = run_prep("--mode", "project")
            self.assertTrue(c_proj["mapping"]["boards_to_areas"])
            self.assertTrue(c_proj["ado"]["create_board_teams"])
            self.assertTrue(c_proj["run"]["boards_to_areas"])
            self.assertTrue(c_proj["run"]["create_board_teams"])

            # --no-boards-to-areas and --no-create-board-teams disable them
            c_no_b = run_prep("--mode", "project", "--no-boards-to-areas", "--no-create-board-teams")
            self.assertFalse(c_no_b["mapping"]["boards_to_areas"])
            self.assertFalse(c_no_b["ado"]["create_board_teams"])
            self.assertFalse(c_no_b["run"]["boards_to_areas"])
            self.assertFalse(c_no_b["run"]["create_board_teams"])
        finally:
            os.chdir(old_cwd)
            jira_client.JiraClient, ado_client.AdoClient = orig_jira, orig_ado
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class ZephyrSquadAndSecretsTests(unittest.TestCase):
    """Zephyr Squad Cloud steps, Tested By links, Azure Test Plans/Suites, backfill sync, and git-ignored .env."""

    def test_git_ignores_secret_files_and_dotenv_auto_connect_never_leaks_tokens(self):
        import subprocess
        from jira2ado.ui import server

        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for secret_name in (".env", ".secrets.env", "secrets.env", "secrets.yaml"):
            proc = subprocess.run(
                ["git", "check-ignore", "-q", secret_name],
                cwd=repo_dir, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, f"{secret_name} MUST be git-ignored!")

        # Write a temporary .env file with all 3 services' keys and verify auto_connect_from_env + /api/state
        tmp = tempfile.mkdtemp()
        env_path = os.path.join(tmp, ".env")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write(
                'JIRA_BASE_URL="https://demo.atlassian.net"\n'
                'JIRA_EMAIL="qa@example.com"\n'
                'JIRA_API_TOKEN="SECRET_JIRA_TOKEN_XYZ"\n'
                'ADO_ORG_URL="https://dev.azure.com/demo"\n'
                'ADO_PAT="SECRET_ADO_PAT_XYZ"\n'
                'ZEPHYR_ACCESS_KEY="SECRET_Z_ACCESS_XYZ"\n'
                'ZEPHYR_SECRET_KEY="SECRET_Z_SECRET_XYZ"\n'
            )
        parsed = config.read_dotenv(env_path)
        self.assertEqual(parsed["JIRA_API_TOKEN"], "SECRET_JIRA_TOKEN_XYZ")
        self.assertEqual(parsed["ADO_PAT"], "SECRET_ADO_PAT_XYZ")
        self.assertEqual(parsed["ZEPHYR_ACCESS_KEY"], "SECRET_Z_ACCESS_XYZ")
        self.assertEqual(parsed["ZEPHYR_SECRET_KEY"], "SECRET_Z_SECRET_XYZ")

        orig_jc, orig_ac, orig_zc = server.JiraClient, server.AdoClient, server.ZephyrSquadClient
        orig_tokens, orig_names = dict(server.S.tokens), dict(server.S.names)
        orig_from_env = server.S.connected_from_env
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmp)

            class StubJ:
                def __init__(self, *a, **k):
                    pass
                def myself_details(self):
                    return {"accountId": "acc-qa-1", "displayName": "QA Admin"}

            class StubA:
                def __init__(self, *a, **k):
                    pass
                def whoami(self):
                    return "QA ADO"

            class StubZ:
                def __init__(self, ak, sk, acc_id, base_url=None):
                    assert ak == "SECRET_Z_ACCESS_XYZ"
                    assert sk == "SECRET_Z_SECRET_XYZ"
                    assert acc_id == "acc-qa-1"
                def check_access(self):
                    return True

            server.JiraClient, server.AdoClient, server.ZephyrSquadClient = StubJ, StubA, StubZ
            res = server.auto_connect_from_env(env_path)
            self.assertEqual(res["jira"], "QA Admin")
            self.assertEqual(res["ado"], "QA ADO")
            self.assertEqual(res["zephyr"], "Zephyr Squad Cloud")
            self.assertTrue(res["connected_from_env"])
            self.assertEqual(res["problems"], {})

            state_json = json.dumps(server.api("GET", "/api/state", {}, {}))
            for raw_secret in ("SECRET_JIRA_TOKEN_XYZ", "SECRET_ADO_PAT_XYZ", "SECRET_Z_ACCESS_XYZ", "SECRET_Z_SECRET_XYZ"):
                self.assertNotIn(raw_secret, state_json)
            self.assertIn('"connected_from_env": true', state_json)
        finally:
            os.chdir(orig_cwd)
            server.JiraClient, server.AdoClient, server.ZephyrSquadClient = orig_jc, orig_ac, orig_zc
            server.S.tokens.clear()
            server.S.tokens.update(orig_tokens)
            server.S.names.clear()
            server.S.names.update(orig_names)
            server.S.connected_from_env = orig_from_env

    def test_zephyr_jwt_qsh_signing_and_https_enforcement(self):
        import base64
        import hashlib
        import hmac
        from jira2ado.zephyr_client import ZephyrError, ZephyrSquadClient, build_jwt

        tok = build_jwt(
            "my-access-key", "my-secret-key", "acc-123", "GET",
            "/public/rest/api/1.0/teststep/1001",
            params={"projectId": "200", "a": "hello world"},
            ttl=180, now=1700000000,
        )
        parts = tok.split(".")
        self.assertEqual(len(parts), 3)
        pad = lambda s: s + "=" * (-len(s) % 4)
        hdr = json.loads(base64.urlsafe_b64decode(pad(parts[0])))
        pay = json.loads(base64.urlsafe_b64decode(pad(parts[1])))
        self.assertEqual(hdr, {"alg": "HS256", "typ": "JWT"})
        self.assertEqual(pay["iss"], "my-access-key")
        self.assertEqual(pay["sub"], "acc-123")
        self.assertEqual(pay["iat"], 1700000000)
        self.assertEqual(pay["exp"], 1700000180)
        want_canon = "GET&/public/rest/api/1.0/teststep/1001&a=hello%20world&projectId=200"
        self.assertEqual(pay["qsh"], hashlib.sha256(want_canon.encode()).hexdigest())
        want_sig = base64.urlsafe_b64encode(
            hmac.new(b"my-secret-key", f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(parts[2], want_sig)

        with self.assertRaises(ZephyrError):
            ZephyrSquadClient("ak", "sk", "acc", base_url="http://insecure.example/connect")

    def test_steps_to_ado_xml_action_validate_data_escaping_and_attachments(self):
        import xml.etree.ElementTree as ET
        from jira2ado.zephyr import step_attachment_key, step_count_from_ado_xml, steps_to_ado_xml

        self.assertIsNone(steps_to_ado_xml([]))
        steps = [
            {
                "id": "s2", "order": 2,
                "step": "Click <Submit> & verify \"OK\"", "data": "", "result": "",
                "attachments": [],
            },
            {
                "id": "s1", "order": 1,
                "step": "Enter credentials", "data": "user=a@b.com", "result": "Dashboard loads",
                "attachments": [{"id": "att9", "filename": "creds.png"}],
            },
        ]
        att_urls = {step_attachment_key("s1", "att9"): "https://ado.example/_apis/wit/attachments/77"}
        xml_str = steps_to_ado_xml(steps, att_urls=att_urls)
        self.assertEqual(step_count_from_ado_xml(xml_str), 2)
        root = ET.fromstring(xml_str)
        self.assertEqual(root.attrib["id"], "0")
        self.assertEqual(root.attrib["last"], "3")
        step_nodes = list(root.findall("step"))
        self.assertEqual(step_nodes[0].attrib, {"id": "2", "type": "ValidateStep"})
        self.assertEqual(step_nodes[1].attrib, {"id": "3", "type": "ActionStep"})
        p0 = [el.text or "" for el in step_nodes[0].findall("parameterizedString")]
        self.assertIn("Enter credentials", p0[0])
        self.assertIn("Test Data:", p0[0])
        self.assertIn("user=a@b.com", p0[0])
        self.assertIn("https://ado.example/_apis/wit/attachments/77", p0[0])
        self.assertIn("Dashboard loads", p0[1])
        p1 = [el.text or "" for el in step_nodes[1].findall("parameterizedString")]
        self.assertIn("&lt;Submit&gt; &amp; verify", p1[0])
        self.assertEqual(p1[1], "")

    def test_full_zephyr_pipeline_steps_attachments_tested_by_links_test_plans_and_verify(self):
        from jira2ado.exporter import export
        from jira2ado.importer import import_test_plans

        def mk(key, itype, summary, links=None, rlinks=None, iid="1001"):
            return {
                "id": iid, "key": key,
                "fields": {
                    "summary": summary,
                    "project": {"id": "10500", "key": "QA"},
                    "issuetype": {"name": itype},
                    "status": {"name": "Done"},
                    "priority": {"name": "High"},
                    "creator": sample.U1, "reporter": sample.U1, "assignee": sample.U1,
                    "labels": [], "components": [], "fixVersions": [],
                    "created": "2024-03-01T10:00:00.000-0500",
                    "updated": "2024-03-02T10:00:00.000-0500",
                    "description": None, "attachment": [],
                    "issuelinks": links or [],
                },
                "renderedFields": {},
                "changelog": [], "comments": [],
                "remotelinks": rlinks or [],
            }

        issues_src = {
            "QA-1": mk(
                "QA-1", "Test", "Verify Checkout", iid="5001",
                links=[
                    {"type": {"name": "Relates"}, "outwardIssue": {"key": "QA-3"}},   # Test -> Story => TestedBy-Reverse
                    {"type": {"name": "Relates"}, "outwardIssue": {"key": "QA-2"}},   # Test -> Test => Related
                    {"type": {"name": "Relates"}, "outwardIssue": {"key": "QA-99"}},  # Test -> pre-existing ADO Story!
                ],
                rlinks=[
                    {"object": {"url": "https://demo.atlassian.net/plugins/servlet/ac/com.thed.zephyr.je/general", "title": "Zephyr"}},
                    {"object": {"url": "https://docs.example/spec", "title": "Spec"}},
                ],
            ),
            "QA-2": mk("QA-2", "Test", "Verify Refund", iid="5002"),
            "QA-3": mk("QA-3", "Story", "Checkout Flow", iid="5003",
                       links=[{"type": {"name": "Relates"}, "outwardIssue": {"key": "QA-2"}}]),  # Story -> Test => TestedBy-Forward
        }

        class FakeJira:
            def fields(self):
                return [{"id": "customfield_10020", "name": "Sprint"}]
            def statuses(self):
                return [{"name": "Done", "statusCategory": {"key": "done"}}]
            def project_info(self, key):
                return {"id": "10500", "key": key, "name": "QA"}
            def project_versions(self, key):
                return [{"id": "201", "name": "Release 1.0", "startDate": "2024-03-01", "releaseDate": "2024-03-31", "description": "R1"}]
            def search_key_timestamps(self, jql):
                return [(k, "2024-03-02T10:00:00.000-0500") for k in sorted(issues_src)]
            def issue(self, key):
                return copy.deepcopy(issues_src[key])

        class FakeZephyr:
            def test_steps(self, issue_id, project_id):
                if str(issue_id) == "5001":
                    return [
                        {"id": "st1", "order": 1, "step": "Open cart", "data": "item=SKU1", "result": "Cart visible",
                         "html_step": "", "html_data": "", "html_result": "", "attachments": []},
                        {"id": "st2", "order": 2, "step": "Click Pay", "data": "", "result": "",
                         "html_step": "", "html_data": "", "html_result": "", "attachments": []},
                    ]
                if str(issue_id) == "5002":
                    return [{"id": "st9", "order": 1, "step": "Trigger refund", "data": "", "result": "Refunded",
                             "html_step": "", "html_data": "", "html_result": "", "attachments": []}]
                return []
            def step_attachments(self, step_id, issue_id, project_id):
                if str(step_id) == "st1":
                    return [{"id": "a55", "filename": "cart.png", "size": 9}]
                return []
            def download_attachment(self, att_id, step_id, issue_id, project_id, dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(b"png-bytes")
            def cycles(self, project_id, version_id="-1"):
                if str(version_id) == "201":
                    return [{"id": "cyc1", "name": "Regression Cycle", "description": "Full reg", "startDate": "", "endDate": ""}]
                return []
            def folders(self, cycle_id, project_id, version_id="-1"):
                if str(cycle_id) == "cyc1":
                    return [{"id": "fld1", "name": "Checkout Folder", "description": ""}]
                return []
            def folder_execution_keys(self, folder_id, cycle_id, project_id, version_id="-1"):
                return ["QA-1"] if str(folder_id) == "fld1" else []
            def cycle_execution_keys(self, cycle_id, project_id, version_id="-1"):
                # QA-1 is in fld1, QA-2 is directly in cyc1
                return ["QA-1", "QA-2"]

        class PlanFakeAdo(sample.FakeAdo):
            available_types = ["Product Backlog Item", "Bug", "Task", "Epic", "Feature", "Test Case"]
            type_states = {
                "Product Backlog Item": ["New", "Approved", "Committed", "Done", "Removed"],
                "Bug": ["New", "Approved", "Committed", "Done", "Removed"],
                "Task": ["To Do", "In Progress", "Done", "Removed"],
                "Test Case": ["Design", "Ready", "Closed"],
            }
            def __init__(self):
                super().__init__()
                self.plans = []
                self.suites = {}
                self.suite_assignments = []
                self._next_plan = 500
                self._next_suite = 600
            def list_test_plans(self):
                return list(self.plans)
            def create_test_plan(self, name, **kw):
                self._next_plan += 1
                self._next_suite += 1
                p = {"id": self._next_plan, "name": name, "rootSuite": {"id": self._next_suite, "name": name}, **kw}
                self.plans.append(p)
                self.suites[p["id"]] = [{"id": p["rootSuite"]["id"], "name": name, "parentSuite": None}]
                return p
            def list_test_suites(self, plan_id):
                return list(self.suites.get(plan_id, []))
            def create_static_suite(self, plan_id, parent_suite_id, name):
                self._next_suite += 1
                s = {"id": self._next_suite, "name": name, "parentSuite": {"id": parent_suite_id}}
                self.suites.setdefault(plan_id, []).append(s)
                return s
            def add_test_cases_to_suite(self, plan_id, suite_id, work_item_ids):
                self.suite_assignments.append((plan_id, suite_id, list(work_item_ids)))
                return len(work_item_ids)

        folder = tempfile.mkdtemp()
        cfg = cfg_for(folder)
        cfg["jira"]["jql"] = 'project = "QA"'
        cfg["run"] = {"mode": "project", "from": "QA", "to": "Shop"}
        cfg["mapping"]["types"]["Test"] = "Test Case"
        cfg["mapping"]["history"] = False
        cfg["zephyr"]["enabled"] = True
        cfg["zephyr"]["include_test_plans"] = True
        q = lambda *a, **k: None

        export(FakeJira(), cfg, log=q, zephyr=FakeZephyr())

        ado = PlanFakeAdo()
        # Pre-existing ADO Story QA-99 migrated in an earlier run
        pre_story_id = ado.create_item("Product Backlog Item", [
            {"op": "add", "path": "/fields/System.Title", "value": "Earlier Story"},
            {"op": "add", "path": "/fields/System.State", "value": "New"},
            {"op": "add", "path": "/fields/System.Tags", "value": "QA-99"},
        ])["id"]

        stats = import_items(ado, cfg, USERS, log=q)
        self.assertEqual((stats["created"], stats["failed"]), (3, []))
        lstats = import_links(ado, cfg, log=q)
        self.assertEqual(lstats["added"], 4)

        by_key = {}
        for iid, it in ado.items.items():
            tags = it["fields"].get("System.Tags", "")
            for k in ("QA-1", "QA-2", "QA-3", "QA-99"):
                if k in tags:
                    by_key[k] = (iid, it)

        qa1_id, qa1 = by_key["QA-1"]
        qa2_id, qa2 = by_key["QA-2"]
        qa3_id, qa3 = by_key["QA-3"]
        # Test Steps XML + uploaded step attachment URL embedded!
        self.assertIn("<steps id=\"0\" last=\"3\">", qa1["fields"]["Microsoft.VSTS.TCM.Steps"])
        self.assertIn("https://ado.example/_apis/wit/attachments/1", qa1["fields"]["Microsoft.VSTS.TCM.Steps"])
        # Internal Zephyr servlet remote link filtered out, real Spec link kept
        hyperlinks = [r["url"] for r in qa1["relations"] if r["rel"] == "Hyperlink"]
        self.assertIn("https://docs.example/spec", hyperlinks)
        self.assertFalse(any("com.thed.zephyr.je" in u for u in hyperlinks))
        # TestedBy link promotion + cross-run QA-99 resolution!
        qa1_rels = {(r["rel"], r["url"]) for r in qa1["relations"]}
        self.assertIn(("Microsoft.VSTS.Common.TestedBy-Reverse", ado.item_url(qa3_id)), qa1_rels)
        self.assertIn(("Microsoft.VSTS.Common.TestedBy-Reverse", ado.item_url(pre_story_id)), qa1_rels)
        self.assertIn(("System.LinkTypes.Related", ado.item_url(qa2_id)), qa1_rels)
        qa3_rels = {(r["rel"], r["url"]) for r in qa3["relations"]}
        self.assertIn(("Microsoft.VSTS.Common.TestedBy-Forward", ado.item_url(qa2_id)), qa3_rels)

        # Azure Test Plans & Suites creation (Release 1.0 -> Regression Cycle -> Checkout Folder)
        pstats = import_test_plans(ado, cfg, log=q)
        self.assertEqual((pstats["plans_created"], pstats["suites_created"], pstats["cases_added"]), (1, 2, 2))
        self.assertEqual(ado.plans[0]["name"], "Release 1.0")
        suite_names = {s["id"]: s["name"] for s in ado.suites[ado.plans[0]["id"]]}
        assigned_by_suite = {suite_names[sid]: ids for _, sid, ids in ado.suite_assignments}
        # QA-1 in Checkout Folder only (deduplicated from Regression Cycle); QA-2 directly in Regression Cycle
        self.assertEqual(assigned_by_suite["Checkout Folder"], [qa1_id])
        self.assertEqual(assigned_by_suite["Regression Cycle"], [qa2_id])

        # Re-running import_test_plans is 100% idempotent
        pstats2 = import_test_plans(ado, cfg, log=q)
        self.assertEqual((pstats2["plans_created"], pstats2["suites_created"], pstats2["cases_added"]), (0, 0, 0))

        # Verify passes with 0 problems (counting steps AND TestedBy links!)
        total, bad = verify(ado, cfg, USERS, os.path.join(folder, "verify.csv"), log=q)
        self.assertEqual((total, bad), (3, 0))

    def test_test_plans_403_warns_cleanly_and_sync_zephyr_backfills_idempotently(self):
        from jira2ado.ado_client import AdoError
        from jira2ado.importer import import_test_plans, sync_zephyr_steps
        from jira2ado.util import write_json

        folder = tempfile.mkdtemp()
        write_json(os.path.join(folder, "meta.json"), sample.META)
        write_json(os.path.join(folder, "keys.json"), ["QA-10", "QA-11"])
        write_json(os.path.join(folder, "issues", "QA-10.json"), {
            "id": "9010", "key": "QA-10",
            "fields": {
                "summary": "Login Test", "issuetype": {"name": "Test"}, "status": {"name": "Done"},
                "priority": {"name": "High"}, "creator": sample.U1, "reporter": sample.U1, "assignee": sample.U1,
                "labels": [], "components": [], "fixVersions": [],
                "created": "2024-03-01T10:00:00.000-0500", "updated": "2024-03-02T10:00:00.000-0500",
                "description": None, "attachment": [],
                "issuelinks": [{"type": {"name": "Relates"}, "outwardIssue": {"key": "QA-11"}}],
            },
            "renderedFields": {}, "changelog": [], "comments": [],
            "zephyr_steps": [{"id": "1", "order": 1, "step": "Sign in", "data": "", "result": "Welcome page", "attachments": []}],
        })
        write_json(os.path.join(folder, "issues", "QA-11.json"), {
            "id": "9011", "key": "QA-11",
            "fields": {
                "summary": "Login Story", "issuetype": {"name": "Story"}, "status": {"name": "Done"},
                "priority": {"name": "High"}, "creator": sample.U1, "reporter": sample.U1, "assignee": sample.U1,
                "labels": [], "components": [], "fixVersions": [],
                "created": "2024-03-01T10:00:00.000-0500", "updated": "2024-03-02T10:00:00.000-0500",
                "description": None, "attachment": [], "issuelinks": [],
            },
            "renderedFields": {}, "changelog": [], "comments": [],
        })
        write_json(os.path.join(folder, "zephyr-cycles.json"), {
            "project_key": "QA", "project_id": "1",
            "versions": [{"id": "-1", "name": "Unscheduled", "cycles": [
                {"id": "c1", "name": "Smoke", "direct_keys": ["QA-10"], "folders": []}
            ]}],
        })

        cfg = cfg_for(folder)
        cfg["mapping"]["types"]["Test"] = "Test Case"
        cfg["zephyr"]["enabled"] = True
        cfg["zephyr"]["include_test_plans"] = True
        q = lambda *a, **k: None

        class NoTestPlanPermAdo(sample.FakeAdo):
            def list_test_plans(self):
                raise AdoError("HTTP 403 missing Test Management scope", 403)
            def create_test_plan(self, *a, **k):
                raise AdoError("HTTP 403", 403)
            def create_static_suite(self, *a, **k):
                raise AdoError("HTTP 403", 403)

        ado = NoTestPlanPermAdo()
        # Simulate already-migrated Test Case (without steps!) and already-migrated Story
        tc_id = ado.create_item("Test Case", [
            {"op": "add", "path": "/fields/System.Title", "value": "Login Test"},
            {"op": "add", "path": "/fields/System.State", "value": "Ready"},
            {"op": "add", "path": "/fields/System.Tags", "value": "QA-10"},
        ])["id"]
        st_id = ado.create_item("Product Backlog Item", [
            {"op": "add", "path": "/fields/System.Title", "value": "Login Story"},
            {"op": "add", "path": "/fields/System.State", "value": "Done"},
            {"op": "add", "path": "/fields/System.Tags", "value": "QA-11"},
        ])["id"]

        res = sync_zephyr_steps(ado, cfg, USERS, log=q)
        self.assertEqual(res["updated"], 1)
        self.assertEqual(res["links_added"], 1)
        self.assertIn("Sign in", ado.items[tc_id]["fields"]["Microsoft.VSTS.TCM.Steps"])
        self.assertIn(
            ("Microsoft.VSTS.Common.TestedBy-Reverse", ado.item_url(st_id)),
            {(r["rel"], r["url"]) for r in ado.items[tc_id]["relations"]},
        )
        # 403 on Test Plans warned clearly without raising or losing steps/links
        self.assertTrue(any("Test Management (Read & write)" in w for w in res["warnings"]))
        # Second backfill run is a no-op on steps & links
        res2 = sync_zephyr_steps(ado, cfg, USERS, log=q)
        self.assertEqual((res2["updated"], res2["unchanged"], res2["links_added"]), (0, 1, 0))

    def test_adf_table_macro_fallback_and_ado_test_plan_rest_client(self):
        from jira2ado.ado_client import AdoClient
        from jira2ado.planner import _description_html

        iss = {
            "fields": {
                "description": {
                    "type": "doc",
                    "content": [{
                        "type": "table",
                        "content": [{
                            "type": "tableRow",
                            "content": [{"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Precond A"}]}]}],
                        }],
                    }],
                },
                "attachment": [],
            },
            "renderedFields": {"description": "<p>Intro</p><!-- ADF macro (type = 'table') -->"},
        }
        html = _description_html(iss)
        self.assertIn("<table", html)
        self.assertIn("Precond A", html)

        calls = []

        class MockResp:
            def __init__(self, code, payload):
                self.status_code = code
                self.headers = {"Content-Type": "application/json"}
                self._p = payload
                self.text = json.dumps(payload)
            def json(self):
                return self._p

        class MockSession:
            headers = {}
            def request(self, method, url, **kw):
                calls.append((method, url, kw.get("json")))
                if method == "GET" and url.endswith("/_apis/testplan/plans"):
                    return MockResp(200, {"value": []})
                if method == "POST" and url.endswith("/_apis/testplan/plans"):
                    return MockResp(200, {"id": 10, "name": kw["json"]["name"], "rootSuite": {"id": 11}})
                if method == "GET" and url.endswith("/_apis/testplan/Plans/10/suites"):
                    return MockResp(200, {"value": [{"id": 11, "name": "R1"}]})
                if method == "POST" and url.endswith("/_apis/testplan/Plans/10/suites"):
                    return MockResp(200, {"id": 12, "name": kw["json"]["name"], "parentSuite": kw["json"]["parentSuite"]})
                if method == "POST" and "/testcases/" in url:
                    return MockResp(409, {"message": "already exists"})
                return MockResp(200, {})

        c = AdoClient("https://dev.azure.com/org", "Shop", "tok", session=MockSession())
        self.assertEqual(c.list_test_plans(), [])
        p = c.create_test_plan("R1", area_path="Shop", iteration="Shop")
        self.assertEqual((p["id"], p["rootSuite"]["id"]), (10, 11))
        self.assertEqual(len(c.list_test_suites(10)), 1)
        s = c.create_static_suite(10, 11, "Cycle 1")
        self.assertEqual(s["id"], 12)
        self.assertEqual(calls[-1][2]["suiteType"], "staticTestSuite")
        self.assertEqual(c.add_test_cases_to_suite(10, 12, [101, 102]), 2)


if __name__ == "__main__":
    unittest.main()
