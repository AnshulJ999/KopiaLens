"""Single entry point for the compiled server build: one binary plays every server-side role.

    kopialens-server.exe --server                      server
    kopialens-server.exe --reset-password              set a new sign-in password (local reset)
    kopialens-server.exe -m app.index.worker <repo>    index worker
    kopialens-server.exe -m app.backblaze.worker       Backblaze worker
    kopialens-server.exe -m app.core.sampledata ...   sample data build

The desktop app (shell/) starts it with ``--server``. The ``-m`` form mirrors how the app spawns
workers from source, so the spawn sites and the command-line checks that find running workers work
unchanged in both modes.
"""
import sys

USAGE = "usage: kopialens-server --server | --reset-password (the desktop app starts the server)"


def run() -> int:
    argv = sys.argv[1:]
    if argv[:2] == ["-m", "app.index.worker"]:
        from app.index import worker
        return worker.main(argv[2:])
    if argv[:2] == ["-m", "app.backblaze.worker"]:
        from app.backblaze import worker
        return worker.main(argv[2:])
    if argv[:2] == ["-m", "app.core.sampledata"]:
        from app.core import sampledata
        return sampledata.main(argv[2:])
    if argv[:1] == ["--reset-password"]:
        from app.core import resetpw
        return resetpw.main()
    if argv[:1] == ["--server"]:
        import main
        main.run()
        return 0
    print(f"kopialens: unknown arguments: {' '.join(argv)}\n{USAGE}" if argv else USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(run())
