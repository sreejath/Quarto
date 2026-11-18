# Jira2ADO — Complete User & Admin Help Guide

This guide covers everything required to connect, validate, review, and migrate tickets from **Atlassian Jira Cloud** to **Azure DevOps Services** with 100% data fidelity — directly from your browser.

---

## 1. Launching the Browser App (No Command Line Needed)

Jira2ADO runs as a **100% local web application** bound exclusively to `127.0.0.1` on your machine. Your API tokens are held in memory only for your active session and are **never saved to disk**.

- **Standalone Click-to-Run App**: Download **`Jira2ADO-macOS.dmg`** (drag `Jira2ADO.app` to Applications) or **`Jira2ADO-Windows.exe`**. Runs without Python installed and saves run folders in `~/Documents/Jira2ADO`. *(First launch on macOS: if Gatekeeper warns about an unidentified developer, Right-click `Jira2ADO.app` → **Open**.)*
- **macOS (from source)**: Double-click **`Start jira2ado.command`** in Finder (or run `bash ui.sh`).
- **Windows (from source)**: Double-click **`Start-Jira2ADO.bat`** in File Explorer.
- **Python Package (Any OS)**:
  ```bash
  pip install -e .
  jira2ado-ui
  ```
- **Docker**:
  ```bash
  docker build -t jira2ado .
  docker run --rm -p 127.0.0.1:8765:8765 -v "$(pwd)/runs:/app/runs" jira2ado
  ```

Your default browser will automatically open the Jira2ADO web interface. You can also click the **`? Help & Setup Guide`** button in the top-right corner of the app at any time.

---

## 2. Credentials & Minimum Permissions

### 2.1 Atlassian Jira Cloud (100% Read-Only)
1. **Atlassian URL**: Your Jira Cloud instance URL (`https://your-company.atlassian.net` — HTTPS required).
2. **Jira Email**: The email address of your Atlassian account.
3. **Jira API Token**: Generate one at [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens).
4. **Permissions**:
   - Jira2ADO is **strictly read-only on Jira** and never modifies your Jira issues.
   - **Browse Projects** & **View Development Tools** on the source Jira projects (to read issues, sprints, boards, attachments, comments, worklogs, and Git PR/branch/commit links).

### 2.2 Azure DevOps Services
1. **Azure DevOps Organization URL**: Your organization URL (`https://dev.azure.com/your-org` — HTTPS required).
2. **Personal Access Token (PAT)**: Click **User Settings (top-right gear icon in ADO) → Personal access tokens → + New Token**.
3. **Minimum PAT Scopes**:
   - **Work Items**: `Read & write` (required)
   - **Project and Team**: `Read & write` (optional — needed only when auto-creating Sprint Iterations, Area Paths, and subscribing Team Boards)
   - **Graph / User Profile**: `Read` (optional — enables automatic verification of ADO user identities for assignee/author resolution)
4. **Bypass Rules Permission (Required for Work Item Creation & History Replay)**:
   - Jira2ADO uses `bypassRules=true` on work item writes so that migrated items preserve their original Jira `System.CreatedDate`, `System.ChangedDate`, `Microsoft.VSTS.Common.ClosedDate`, `System.CreatedBy`, and target workflow states (including setting completed/in-progress states directly on creation when full history replay is turned off).
   - Ensure your ADO user account has **"Bypass rules on work item updates" = Allow**:
     1. Open **Project Settings → Permissions → Users** (or your security group).
     2. Set **Bypass rules on work item updates** to **Allow**.

---

## 3. Step-by-Step Browser Workflow

### Step 1: Connect to Jira & Azure DevOps
Enter your **Atlassian Jira URL**, **Jira Email**, **Jira API Token**, **Azure DevOps Org URL**, and **ADO PAT**, then click **Connect**.
- The app verifies both tokens immediately and displays your signed-in display names for Jira and Azure DevOps.
- Paste each token **once** (the app automatically detects accidental double-pastes).

### Step 2: Choose Source Scope & Destination Project
1. **Jira Project**: Select your source Jira project from the searchable dropdown.
2. **Jira Board**: Select a specific Scrum/Kanban board (or leave on **All boards in project**).
3. **Sprint(s)**: Check **one or more sprints** from the list (active sprints are listed first, followed by upcoming and closed sprints with date ranges and sprint goals).
4. **Destination ADO Project**: Select the target Azure DevOps project. Jira2ADO discovers all projects in your ADO organization automatically.
5. **Options**:
   - **Include Done tickets**: Include Jira issues whose status is already `Done`.
   - **Include parent Epics/Stories outside sprint**: Automatically fetches parent Epics/Features/Stories that live outside the selected sprint so no Sub-task or Story loses its hierarchy link.
   - **Replay full revision history**: Replays every historical field change, comment, attachment, and worklog in chronological order (`true` by default).
   - **Iteration & Area Path Overrides**: Optionally place all migrated tickets into a specific ADO Iteration or Area Path.

### Step 3: Validate & Review Tickets
Click **Validate**. Without writing anything to Azure DevOps, Jira2ADO:
1. Exports all matching tickets, attachments, comments, changelog history, worklogs, remote links, and Git development links from Jira.
2. Discovers valid Work Item Types and Workflow States in the destination ADO project (auto-adapting across **Scrum**, **Agile**, **Basic**, **CMMI**, and **Inherited** processes).
3. Scans the destination ADO project for existing work items to detect **duplicates**.
4. Displays the interactive **Ticket Review & Validation Table** with:
   - **Issue Key** (clickable link to Jira + button to open the **Ticket Inspector Drawer**)
   - **Issue Type** (`Story → Product Backlog Item / User Story`, `Bug → Bug`, `Sub-task → Task`, etc.)
   - **Summary** (with attachment, comment, worklog, and link badges)
   - **Assignee** (showing Jira Assignee → resolved ADO Identity)
   - **Status (Interactive Dropdown)**: Automatically pre-selected to the **closest matching ADO state** (`Exact Match`, `Configured`, `Fuzzy/Semantic Match`, or `Category Fallback`). You can change the status for any individual ticket or click **"Apply to all [Status]"** to update every ticket with that Jira status at once.
   - **Outcome & Duplicate Controls**: Shows whether the ticket will be created, is already migrated, or is a **Possible Duplicate**.

---

## 4. Duplicate Detection & Per-Ticket Resolution

During **Validate**, Jira2ADO checks every ticket against the destination ADO project using three layers of detection:
1. **Exact Jira Key Match (`already in ADO`)**: An item in ADO already has this Jira key in `Custom.JiraKey` or `System.Tags`.
2. **Jira Key in Title (`possible duplicate`)**: An item in ADO mentions `[KEY-123]` in `System.Title`.
3. **Same Summary (`possible duplicate` / `same summary in run`)**: An item in ADO (or another ticket in the same sprint) has the same normalized title.

For any duplicate or existing ticket, you can choose its action **per-ticket** in the **Outcome** column or in bulk using the toolbar:
- **Skip (`skip`)**: Do not create a new work item in ADO.
- **Create Duplicate (`duplicate`)**: Create a new work item in ADO anyway, tag it `Possible duplicate`, and add a `Related` link to the existing ADO work item so teams can review or merge them.
- **Sync Delta (`sync` — exact key matches only)**: Safely update an existing ADO work item that already carries the exact Jira key with the latest Jira status, fields, new comments, worklogs, and attachments without overwriting unrelated title-matched work items.

---

## 5. What Gets Migrated ("EVERYTHING" Checklist)

| Jira Artifact | Azure DevOps Destination | Details |
| :--- | :--- | :--- |
| **Summary, Description & Acceptance Criteria** | `System.Title`, `System.Description`, `Microsoft.VSTS.Common.AcceptanceCriteria` | Full Atlassian Document Format (ADF) and Jira Wiki Markup converted to clean HTML |
| **Inline Images & File Attachments** | Uploaded to ADO Attachments + `AttachedFile` relation | Inline `<img>` and file links in descriptions/comments are rewritten to point to the uploaded ADO attachment URLs |
| **Comments & `@mentions`** | `System.History` revisions | Original author, timestamp, and HTML body preserved; opaque `[~accountid:...]` mentions resolved to human display names |
| **Full Changelog / Revision History** | Chronological Work Item Revisions | Replays status transitions, assignee changes, sprint moves, description edits, and unmapped field changes in time order |
| **Worklogs & Time Tracking** | `OriginalEstimate`, `CompletedWork`, `RemainingWork` + History Notes | Converts seconds to hours and logs each worklog entry with author, timestamp, duration, and comment |
| **Parent / Child & Epic Hierarchy** | `System.LinkTypes.Hierarchy-Reverse` | Sub-task → Story/Bug and Story → Epic/Feature links preserved (with optional out-of-sprint parent auto-fetch) |
| **Issue Links & Cross-Ticket Links** | `Dependency-Forward/Reverse`, `Related`, `Duplicate` + HTML Link Rewriting | `Blocks`, `Relates`, `Clones`, and `Duplicates` links mapped to native ADO link types; inline Jira browse URLs rewritten to ADO work item links |
| **Confluence & Remote Web Links** | `Hyperlink` relations | Preserves linked Confluence pages, specs, and external URLs |
| **Git PRs, Branches & Commits** | `Hyperlink` relations | Extracts GitHub, Bitbucket, and GitLab PR/branch/commit URLs from Jira's development panel |
| **Sprints & Iterations** | `System.IterationPath` + Team Sprint Subscription | Creates missing sprint iterations with start/end dates and subscribes the target ADO team board |
| **Board Rank & Custom Fields** | `Microsoft.VSTS.Common.StackRank` + `Custom.*` or Fallback Note | Preserves backlog ordering; any custom field not present on the target ADO template is safely recorded in the initial migration note so zero data is lost |

---

## 6. Post-Migration Verification, Idempotent Retry & Confirmed Rollback

- **Automatic Verification (`verify.csv`)**: Immediately after migration, Jira2ADO reads back every migrated work item from Azure DevOps and verifies its title, state, iteration path, assignee, attachment count, comment count, and links.
- **Idempotent Retry / Self-Healing Resume**: If a migration is stopped or encounters a network error, click **Retry / Resume Remaining**. Jira2ADO reconciles the local journal with live Azure DevOps revisions and resumes without creating duplicates.
- **Confirmed Rollback**: If you did a test run into a sandbox project and want to undo it, click **Undo / Rollback Run** and confirm the project name to move all work items created by that run to the Azure DevOps Recycle Bin.

---

## 7. Troubleshooting Common Messages

- **"Azure DevOps did not accept the access token (HTTP 203 / 401)"**:
  - Your PAT may be expired, scoped to a different organization, or pasted twice. Create a PAT scoped to your target organization with `Work Items (Read & write)` (and `Project and Team (Read & write)` if auto-creating sprint iterations).
- **"VS403474: You do not have permission to bypass rules"**:
  - Grant **"Bypass rules on work item updates = Allow"** to your user in Azure DevOps Project Settings → Permissions → Users so Jira2ADO can write historical `CreatedDate`, `ChangedDate`, `ClosedDate`, and workflow states.
- **"★ Highlighted Statuses"**:
  - Statuses marked with **`★ Recommended`** do not have an identically named state in the destination ADO project and are mapped to the closest matching state automatically. You can override any status using the dropdown in the Validation table, or add custom states in Azure DevOps (Organization Settings → Process) and click **Validate again**.
