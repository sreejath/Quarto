"""Pure Zephyr Squad -> Azure DevOps transformation helpers (Microsoft.VSTS.TCM.Steps XML & test classification)."""
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_esc

from .adf import esc
from .wiki import wiki_to_html

_INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_TEST_LINK_NAMES = {
    "relates", "relates to", "tests", "test", "tested by", "is tested by", "verifies", "verified by",
}


def _clean_xml_text(s):
    return _INVALID_XML_CHARS.sub("", str(s or ""))


def step_attachment_key(step_id, att_id):
    """Unique journal/att_urls key for a Zephyr step-level attachment."""
    return f"zstep:{step_id}:{att_id}"


def is_test_case_issue(issue, cfg=None):
    """Return True if a Jira issue represents a Zephyr Test Case (by Jira type or mapped ADO type)."""
    f = (issue or {}).get("fields") or {}
    jtype = ((f.get("issuetype") or {}).get("name") or "").strip()
    if not jtype:
        return False
    zcfg = ((cfg or {}).get("zephyr") or {})
    allowed = {t.strip().lower() for t in (zcfg.get("test_issue_types") or ["Test", "Test Case"]) if t}
    if jtype.lower() in allowed:
        return True
    m = ((cfg or {}).get("mapping") or {})
    mapped = (m.get("ticket_types") or {}).get((issue or {}).get("key")) or (m.get("types") or {}).get(jtype)
    return (mapped or "").strip().lower() == "test case"


def is_test_link_type(jira_link_type_name):
    """Return True if a Jira issue link type should be promoted to ADO TestedBy when connecting a Test Case."""
    name = (jira_link_type_name or "").strip().lower()
    return (not name) or (name in _TEST_LINK_NAMES)


def _cell_html(html_val, raw_val):
    if html_val and str(html_val).strip():
        return _clean_xml_text(html_val).strip()
    raw = _clean_xml_text(raw_val).strip()
    if not raw:
        return ""
    return wiki_to_html(raw) or f"<p>{esc(raw)}</p>"


def steps_to_ado_xml(steps, att_urls=None):
    """Convert normalized Zephyr test steps into Azure DevOps Microsoft.VSTS.TCM.Steps XML.

    ADO requires:
    - Root <steps id="0" last="{N}"> where sequential <step id="2"> .. <step id="{N}"> start at 2.
    - Each <step> has type="ValidateStep" when Expected Result is non-empty, else "ActionStep".
    - Exactly two <parameterizedString isformatted="true"> children (Action+Data, Expected Result),
      containing XML-escaped HTML, followed by <description/>.
    """
    if not steps:
        return None
    att_urls = att_urls or {}
    ordered = sorted(steps, key=lambda s: (int(s.get("order", 0)), str(s.get("id", ""))))
    parts = []
    step_id = 2
    for st in ordered:
        action_html = _cell_html(st.get("html_step"), st.get("step"))
        data_html = _cell_html(st.get("html_data"), st.get("data"))
        result_html = _cell_html(st.get("html_result"), st.get("result"))

        action_blocks = []
        if action_html:
            action_blocks.append(action_html)
        if data_html:
            action_blocks.append(f"<p><b>Test Data:</b></p>{data_html}")

        for att in st.get("attachments") or []:
            akey = step_attachment_key(st.get("id"), att.get("id"))
            fname = _clean_xml_text(att.get("filename") or "attachment")
            url = att_urls.get(akey)
            if url:
                action_blocks.append(f'<p><i>Attachment:</i> <a href="{esc(url)}">{esc(fname)}</a></p>')
            else:
                action_blocks.append(f"<p><i>Attachment:</i> {esc(fname)}</p>")

        action_body = f"<div>{''.join(action_blocks)}</div>" if action_blocks else ""
        result_body = f"<div>{result_html}</div>" if result_html else ""
        step_type = "ValidateStep" if result_body else "ActionStep"
        parts.append(
            f'<step id="{step_id}" type="{step_type}">'
            f'<parameterizedString isformatted="true">{xml_esc(action_body)}</parameterizedString>'
            f'<parameterizedString isformatted="true">{xml_esc(result_body)}</parameterizedString>'
            f'<description/>'
            f'</step>'
        )
        step_id += 1
    last_id = step_id - 1
    return f'<steps id="0" last="{last_id}">{"".join(parts)}</steps>'


def step_count_from_ado_xml(xml_str):
    """Return the number of <step> nodes inside an ADO Microsoft.VSTS.TCM.Steps XML string."""
    if not xml_str or not str(xml_str).strip():
        return 0
    try:
        root = ET.fromstring(str(xml_str))
    except ET.ParseError:
        return 0
    return sum(1 for el in root.iter() if el.tag.lower().endswith("step"))
