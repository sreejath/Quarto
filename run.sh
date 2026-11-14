#!/bin/bash
# Migrate Jira tickets to Azure DevOps: check tokens, export, preview, then (after you confirm) import and verify.
# Run from the jira2ado folder. Type  bash run.sh --help  for the options.
set -e
cd "$(dirname "$0")"

usage() {
  cat <<'USAGE'
Usage, from the jira2ado folder:
  bash run.sh --from DEA --to "Digital Project - ADO - Sandbox" --sprint "Fin-Corp Sprint 28"

  --from KEY         Jira project to export from, for example DEA.
  --to NAME          Azure DevOps project to import into.
  --sprint NAME      Jira sprint to export: its name or its number. Repeat it for
                     more than one sprint. "&" and "-" count as the same, so
                     "Fin-Corp Sprint 28" finds "Fin&Corp Sprint 28".
  --iteration PATH   Optional. Put every ticket in this Azure DevOps iteration.
                     Without it, each ticket goes to the iteration named after its
                     sprint. The import creates the iteration if it is missing.
  --include-done     Optional. Also export tickets whose status is Done.

Put any value that has spaces or "&" in quotes.

Older form, still works:
  bash run.sh sprint28/config.yaml
USAGE
}

C=""; FROM=""; TO=""; ITERATION=""; INCLUDE_DONE=""; SPRINTS=()
if [ $# -eq 0 ]; then
  usage; exit 1
elif [ $# -eq 1 ] && [ -f "$1" ]; then
  C="$1"
else
  while [ $# -gt 0 ]; do
    case "$1" in
      --from|--to|--sprint|--iteration)
        if [ $# -lt 2 ] || [ -z "$2" ] || [ "${2:0:2}" = "--" ]; then
          echo "$1 needs a value."; echo; usage; exit 1
        fi
        case "$1" in
          --from) FROM="$2" ;;
          --to) TO="$2" ;;
          --sprint) SPRINTS+=("$2") ;;
          --iteration) ITERATION="$2" ;;
        esac
        shift 2 ;;
      --include-done) INCLUDE_DONE=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Not an option I know: $1"; echo; usage; exit 1 ;;
    esac
  done
  if [ -z "$FROM" ] || [ -z "$TO" ] || [ ${#SPRINTS[@]} -eq 0 ]; then
    echo "Give --from, --to and at least one --sprint."; echo; usage; exit 1
  fi
fi

# Hidden prompt that says what it received, refuses an empty answer and catches a double paste.
ask_secret() {  # ask_secret VARNAME "Prompt: "
  local value half
  while true; do
    read -r -s -p "$2" value
    echo
    if [ -z "$value" ]; then
      echo "  Nothing was received. Paste the token once, then press Return."
      continue
    fi
    half=$(( ${#value} / 2 ))
    if [ $(( ${#value} % 2 )) -eq 0 ] && [ "${value:0:$half}" = "${value:$half}" ]; then
      echo "  That looks like the token pasted twice. Paste it once, then press Return."
      continue
    fi
    echo "  Received ${#value} characters."
    break
  done
  printf -v "$1" '%s' "$value"
}

echo "== Setting up Python (slow the first time only)"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo
echo "== Credentials (the tokens are not shown on screen and not saved)"
echo "   Paste each token once and press Return. Nothing appears while you paste."
read -r -p "Jira email: " JIRA_EMAIL
ask_secret JIRA_API_TOKEN "Jira API token: "
ask_secret ADO_PAT "Azure DevOps token: "
export JIRA_EMAIL JIRA_API_TOKEN ADO_PAT

if [ -z "$C" ]; then
  echo
  echo "== Setting up the run"
  PREP=(--from "$FROM" --to "$TO")
  for S in "${SPRINTS[@]}"; do PREP+=(--sprint "$S"); done
  if [ -n "$ITERATION" ]; then PREP+=(--iteration "$ITERATION"); fi
  if [ -n "$INCLUDE_DONE" ]; then PREP+=(--include-done); fi
  C=$(python -m jira2ado prepare "${PREP[@]}")
fi

echo
echo "== Checking the tokens and the query"
python -m jira2ado -c "$C" access
PROJECT=$(python -m jira2ado -c "$C" show ado.project)
TARGET=$(python -m jira2ado -c "$C" show ado.area_path)
[ -n "$TARGET" ] || TARGET="$PROJECT"
echo
echo "Jira query: $(python -m jira2ado -c "$C" show jira.jql)"
echo "Target:     $TARGET"
echo "Settings:   $C"

echo
echo "== Step 1 of 4: export from Jira"
python -m jira2ado -c "$C" export --force
python -m jira2ado -c "$C" users

echo
echo "== Step 2 of 4: preview. Nothing is sent to Azure DevOps"
python -m jira2ado -c "$C" check --online
python -m jira2ado -c "$C" plan

# Throw away anything typed while the steps above ran, so a stray Return is not taken as the answer.
while read -r -t 1 -n 10000 DISCARD 2>/dev/null; do :; done
DUPLICATES=()
POSSIBLE=$(python -m jira2ado -c "$C" duplicates)
if [ "$POSSIBLE" != "0" ]; then
  echo
  echo "$POSSIBLE ticket(s) may already be in $PROJECT (see 'possible duplicate' in the preview above)."
  echo "  Press Return to leave them out of this import. A later run checks them again."
  echo "  Type create to import them anyway. Each one is tagged 'Possible duplicate' and"
  echo "  gets a Related link to the item it matched, so you can find and merge them."
  read -r -p "Your choice: " CHOICE
  if [ "$CHOICE" = "create" ]; then
    DUPLICATES=(--possible-duplicates create)
    echo "  They will be created and tagged."
  else
    echo "  They will be left out."
  fi
fi
echo
if [ "$(python -m jira2ado -c "$C" show ado.confirm_with_project_name)" = "true" ]; then
  WANT="$PROJECT"
  read -r -p "To create these work items in $PROJECT, type the project name exactly: " ANSWER
else
  WANT="yes"
  read -r -p "Create these work items in $PROJECT now? Type yes to continue: " ANSWER
fi
if [ "$ANSWER" != "$WANT" ]; then
  echo "Stopped. Nothing was written to Azure DevOps."
  exit 0
fi

echo
echo "== Step 3 of 4: import"
python -m jira2ado -c "$C" import "${DUPLICATES[@]}"

echo
echo "== Step 4 of 4: verify"
REPORT="$(dirname "$C")/verify.csv"
python -m jira2ado -c "$C" verify --report "$REPORT" || true
echo
echo "Finished. The report is in $REPORT"
