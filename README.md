# Quarto — High-Fidelity Jira Cloud to Azure DevOps Migration App

![Quarto Banner](assets/banner.svg)

**Quarto** is a browser-driven migration application and CLI toolkit that migrates **Jira Cloud** projects, boards, sprints, and **Zephyr Squad** test assets into **Azure DevOps Services** — preserving full revision history, comments, inline images, attachments, worklogs, parent/child hierarchies, Confluence/web links, Git PR/branch/commit links, per-board Area Paths/Teams/Sprints, and Azure Test Plans.

It runs 100% locally on your machine (`127.0.0.1`) on **macOS, Windows, and Linux**, is **100% read-only on Jira & Zephyr**, and supports a git-ignored `.env` file so credentials stay off GitHub and out of run configs.

---

## Key Capabilities

1. **Flexible Migration Modes & Crash-Safe Recovery**
   - **Sprint Migration**, **Entire Board Migration** (Scrum & Kanban, including unsprinted/backlog items), and **Whole Project Migration**.
   - **Batching, Pause/Resume & Recovery**: Run large migrations in checkpoints, pause cleanly after in-flight tickets finish, and resume interrupted runs from disk without creating duplicates.
   - **Live Progress & ETA**: Real-time revision-weighted progress bar showing in-flight parallel tickets and estimated time remaining.
2. **Automatic Per-Board Organization & ADO Teams**
   - Mirrors every Jira board into its own Azure DevOps **Area Path** (`<Project>\<Board>`) and nested **Sprint Iterations** (`<Project>\<Board>\<Sprint>`), placing unsprinted/Kanban tickets on their board and orphaned tickets at the project root.
   - Optionally **auto-creates an Azure DevOps Team per Jira board** with its default Area Path, Backlog Iteration, and sprint subscriptions configured automatically.
3. **Zephyr Squad Cloud Test Cases, Links & Azure Test Plans**
   - Exports **Zephyr Squad** test steps (Action, Test Data, Expected Result) and step-level attachments into Azure DevOps `Test Case` work items (`Microsoft.VSTS.TCM.Steps`).
   - Automatically promotes links between Test Cases and Stories/PBIs/Bugs to native Azure DevOps **`Tests` / `Tested By`** relations (including cross-run resolution for previously migrated tickets).
   - Migrates Jira Releases/Versions → **Azure Test Plans**, Zephyr Cycles → **Static Test Suites**, and Zephyr Folders → **nested Child Suites**.
   - Includes **`quarto sync-zephyr`** (and a UI button) to backfill steps, `Tested By` links, and Test Plans onto already-migrated Test Cases without creating duplicates.
4. **Interactive Validate, Status Recommendations & Duplicate Guard**
   - Press **Validate** to preview every ticket, inspect rendered HTML descriptions & Zephyr test steps, and edit user identity mappings (`users.csv`) before writing anything.
   - **Dynamic Status Discovery**: Matches live ADO states automatically (highlighting recommended closest states with `★ Recommended`) with per-ticket and bulk overrides.
   - **3-Way Duplicate Detection**: Flags exact Jira key matches, key-in-title matches, and matching summaries; choose **Skip**, **Create Duplicate**, or **Sync Delta** (exact key matches only).
5. **Complete Data Fidelity & Verification**
   - **Descriptions & Comments**: Full ADF (including tables) and Jira Wiki Markup converted to HTML with resolved `@mentions` and rewritten inline/cross-ticket links.
   - **Attachments, Full Revision History, Worklogs, Links & Rollback**: Uploads attachments (explicitly skipping and logging files over ADO's 60 MB limit), replays chronological history (`bypassRules`), verifies every migrated item in `verify.csv`, and supports one-click confirmed rollback.
6. **Local `.env` Credential Auto-Loading**
   - Copy `.env.example` to `.env` (strictly git-ignored) to auto-load your Jira, Azure DevOps, and Zephyr Squad credentials into both the CLI and Browser UI at startup.

---

## Quick Start (Run in Browser)

### Option 1: One-Click Local Launchers (No Command Line Needed)
1. Download or clone this repository.
2. *(Optional)* Copy `.env.example` to `.env` (git-ignored) and fill in your Jira, ADO, and optional Zephyr Squad keys.
3. Launch for your operating system:
   - **macOS**: Double-click **`Start Quarto.command`** in Finder (or run `bash ui.sh`).
   - **Windows**: Double-click **`Start-Quarto.bat`** in File Explorer.
4. Your default browser will open `http://127.0.0.1:<port>` automatically.

### Option 2: Install via `pip`
```bash
pip install -e .
quarto-ui
```

### Option 3: Run via Docker
```bash
docker build -t quarto .
docker run --rm -p 127.0.0.1:8765:8765 -v "$(pwd)/runs:/app/runs" quarto
```

### Option 4: Standalone Click-to-Run Desktop Apps (macOS `.dmg` & Windows `.exe`)
No Python installation or terminal required:
- **Download from GitHub Releases**: Every tagged release (`v*`) automatically publishes **`Quarto-macOS.dmg`** and **`Quarto-Windows.exe`** via [`.github/workflows/release.yml`](.github/workflows/release.yml).
- **Build locally**:
  ```bash
  pip install -r requirements.txt pyinstaller
  python packaging/build.py   # produces dist/Quarto-macOS.dmg on macOS or dist/Quarto-Windows.exe on Windows
  ```
- Migration runs, logs, and user CSVs are saved cleanly in `~/Documents/Quarto`.

---

## Step-by-Step Usage Guide

1. **Connect**: Auto-connect from your local git-ignored `.env` file, or enter your **Jira**, **Azure DevOps**, and optional **Zephyr Squad** credentials and click **Connect**.
2. **Choose Mode & Scope**:
   - Pick **Sprint Migration**, **Entire Board Migration**, or **Whole Project Migration**.
   - Choose your source Jira Project/Board/Sprints, destination ADO Project, and options (per-board Areas/Teams, Zephyr steps & Test Plans, Done tickets, history mode).
3. **Validate & Review**:
   - Click **Validate**. Review the ticket table, open the **Ticket Inspector Drawer** (including Zephyr Test Steps), adjust any **Status** dropdowns, and resolve any highlighted duplicates.
4. **Migrate & Verify**:
   - Click **Migrate**, confirm the destination project, and monitor live progress as work items, history, attachments, links, Teams, and Azure Test Plans are created and verified.

For detailed admin setup, PAT scopes (`Work Items`, `Project and Team`, `Test Management`), rule-bypass permissions, and troubleshooting, see [**docs/HELP.md**](docs/HELP.md) or click **`? Help & Setup Guide`** inside the web app.

---

## Command-Line Interface (Optional for CI/Automation)

Every action in the browser app is also available via the `quarto` CLI:

```bash
# Full interactive CLI workflow
bash run.sh --from PROJ --to "Destination Project" --sprint "Sprint 28"

# Individual CLI subcommands
python -m quarto prepare --mode project --from PROJ --to "Destination Project" --include-done --include-zephyr
python -m quarto -c runs/.../config.yaml access
python -m quarto -c runs/.../config.yaml export
python -m quarto -c runs/.../config.yaml users
python -m quarto -c runs/.../config.yaml check --online
python -m quarto -c runs/.../config.yaml plan
python -m quarto -c runs/.../config.yaml import --possible-duplicates skip
python -m quarto -c runs/.../config.yaml verify --report verify.csv
python -m quarto -c runs/.../config.yaml sync-zephyr
python -m quarto -c runs/.../config.yaml rollback
```

---

## Running Tests

```bash
python tests/test_all.py -v
```

---

## License

MIT License — see [LICENSE](LICENSE).
