import sys


def _entry():
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from jira2ado.cli import main as cli_main
        raise SystemExit(cli_main(sys.argv[2:]))
    from jira2ado.ui.server import main as ui_main
    ui_main()


if __name__ == "__main__":
    _entry()
else:
    from jira2ado.ui.server import main as ui_main
    ui_main()
