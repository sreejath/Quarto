import os
import sys


def _ensure_stdio(log_dir=None):
    """GUI/windowed bundles on Windows and macOS set sys.stdout/sys.stderr to None."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    target = os.devnull
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        target = os.path.join(log_dir, "quarto.log")
    fh = open(target, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def _entry():
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        _ensure_stdio()
        from quarto.cli import main as cli_main
        raise SystemExit(cli_main(sys.argv[2:]))
    from quarto import config
    _ensure_stdio(config.frozen_workspace_dir() if getattr(sys, "frozen", False) else None)
    from quarto.ui.server import main as ui_main
    ui_main()


if __name__ == "__main__":
    _entry()
else:
    from quarto import config
    _ensure_stdio(config.frozen_workspace_dir() if getattr(sys, "frozen", False) else None)
    from quarto.ui.server import main as ui_main
    ui_main()
