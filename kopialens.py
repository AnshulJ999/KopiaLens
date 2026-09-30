"""Single entry point for the compiled build: one binary plays every KopiaLens role.

    kopialens.exe                               tray (starts the server)
    kopialens.exe --server                      server
    kopialens.exe --reset-password              set a new sign-in password (local reset)
    kopialens.exe -m app.index.worker <repo>    index worker
    kopialens.exe -m app.backblaze.worker       Backblaze worker

The ``-m`` form mirrors how the app spawns workers from source, so the spawn sites and the
command-line checks that find running workers work unchanged in both modes.
"""
import sys


def run() -> int:
    argv = sys.argv[1:]
    if argv[:2] == ["-m", "app.index.worker"]:
        from app.index import worker
        return worker.main(argv[2:])
    if argv[:2] == ["-m", "app.backblaze.worker"]:
        from app.backblaze import worker
        return worker.main(argv[2:])
    if argv[:1] == ["--reset-password"]:
        from app.core import resetpw
        return resetpw.main()
    if argv[:1] == ["--server"]:
        import main
        main.run()
        return 0
    if argv:
        print(f"kopialens: unknown arguments: {' '.join(argv)}", file=sys.stderr)
        return 2
    import kopialens_tray
    kopialens_tray.main()
    return 0


if __name__ == "__main__":
    sys.exit(run())
