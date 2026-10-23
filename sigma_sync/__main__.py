from .importer import run

if __name__ == "__main__":
    try:
        run()
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
