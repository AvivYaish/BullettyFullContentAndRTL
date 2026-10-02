"""Update bulletty library, pull full content for new articles and format RTL text."""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from filelock import FileLock, Timeout

sys.dont_write_bytecode = True
import hebfix

FIELDS = re.compile(r'''(?m)^([ \t]*(title|description|author|text)[ \t]*=[ \t]*)'''
                    r"""(?:\"{3}.*?\"{3}|'{3}.*?'{3}|\"(?:\\.|[^\"\\\r\n])*\"|'[^'\r\n]*')""", re.S)
GUARD = "[hebfix]: #\n\n"


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / "content"
        temporary.write_bytes(content)
        temporary.replace(path)


def parts(text):
    prefix, body = hebfix.document(text)
    if not prefix:
        raise ValueError("Missing Bulletty article header")
    return prefix, tomllib.loads(prefix.lstrip("\ufeff").split("\n", 1)[1].rsplit("+++", 1)[0]), body


def metadata(prefix, fields, rtl):
    def replace(match):
        value = fields.get(match[2])
        return (match[1] + json.dumps(hebfix.display(value, "R" if rtl else "L"), ensure_ascii=False)
                if isinstance(value, str) and hebfix.HEBREW.search(value) else match[0])
    return FIELDS.sub(replace, prefix)


def download(url, timeout):
    result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("fulltext.py")), url, "--timeout", str(timeout)], capture_output=True, text=True, encoding="utf-8", timeout=timeout, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return result.stdout


def update(library):
    configured = subprocess.run(["bulletty", "--no-hooks", "dirs", "library"], capture_output=True, text=True, encoding="utf-8", check=True, stdin=subprocess.DEVNULL, timeout=10)
    if Path(configured.stdout.strip()).resolve() != library:
        raise ValueError("Library differs from the configured Bulletty library")
    try:
        return subprocess.run(["bulletty", "--no-hooks", "update"], stdin=subprocess.DEVNULL, timeout=120).returncode
    except subprocess.TimeoutExpired:
        print("Feed update timed out after 120 seconds", file=sys.stderr, flush=True)
        return 1


def commit(files):
    previous = {path: path.read_bytes() if path.exists() else None for path in files}
    try:
        for path, content in files.items():
            if content != previous[path]:
                atomic(path, content)
    except BaseException:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            elif not path.exists() or path.read_bytes() != content:
                atomic(path, content)
        raise


def sync(args):
    state_path, categories = args.library / ".sync.json", args.library / "categories"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    width = args.width
    if width == "auto":
        appearance = args.library / ".appearance.toml"
        settings = tomllib.loads(appearance.read_text(encoding="utf-8")) if appearance.exists() else {}
        size = hebfix.terminal_columns()
        width = max(1, min(size - 9, (size - 8) * settings.get("reader_width", 60) // 100))
    failed, changed = (0 if args.reflow else int(bool(update(args.library)))), 0
    def pending(article):
        name = article.relative_to(categories).as_posix()
        record = state.get(name, {})
        return bool(record.get("layout")) if args.reflow else args.retry or record.get("download") is None or "layout" not in record
    queued = sorted(path for path in categories.rglob("*") if path.is_file() and path.suffix.lower() in (".md", ".markdown") and pending(path))
    for number, article in enumerate(queued, 1):
        name = article.relative_to(categories).as_posix()
        print(f"[{number}/{len(queued)}] {name}", flush=True)
        previous = state.get(name, {})
        record, files, error = previous.copy(), {}, None
        try:
            current = article.read_bytes().decode("utf-8")
            _, fields, original = parts(current)
            body = original
            rewrite = args.reflow or not record.get("layout")
            if not args.reflow and (args.retry or record.get("download") is None):
                url = fields.get("url", "")
                record["download"] = True
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    try:
                        body = download(url, args.timeout)
                        rewrite = True
                    except Exception as failure:
                        record["download"], error = "failed", ("Download timed out" if isinstance(failure, subprocess.TimeoutExpired) else str(failure))
            if rewrite:
                body = hebfix.fix(body, width, rtl=args.rtl, measure=hebfix.visible_width, reflow=args.reflow)
                if not args.reflow and "\u00a0" in body:
                    body = GUARD + body
            current = article.read_bytes().decode("utf-8")
            prefix, latest, latest_body = parts(current)
            if latest.get("url") != fields.get("url"):
                raise ValueError("Article URL changed during processing")
            if rewrite and latest_body != original:
                raise ValueError("Article body changed during processing")
            if not args.reflow and not previous.get("layout"):
                prefix = metadata(prefix, latest, args.rtl)
            rendered = prefix + (body if rewrite else latest_body)
            if rewrite:
                record["layout"] = [width, args.rtl]
            backup = args.backup / "original" / name if args.backup else None
            if backup and not args.reflow and not backup.exists():
                files[backup] = current.encode("utf-8")
            state[name] = record
            files.update({article: rendered.encode("utf-8"), state_path: json.dumps(state, ensure_ascii=False).encode("utf-8")})
            commit(files)
        except Exception as failure:
            state[name], error = previous, str(failure)
        else:
            changed += current != rendered
        if error:
            failed += 1
            print(f"Failed: {name}: {error}", file=sys.stderr, flush=True)
    print(f"Finished: {len(queued)} checked, {changed} changed, {failed} failed.", flush=True)
    return int(bool(failed))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("--backup", type=Path, help="Save original article copies outside categories")
    hebfix.options(parser)
    parser.add_argument("--retry", action="store_true", help="Redownload completed or failed articles")
    parser.add_argument("--reflow", action="store_true", help="Only rewrap already formatted articles")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    args.library = args.library.expanduser().resolve()
    args.backup = args.backup.expanduser().resolve() if args.backup else None
    if not (args.library / "categories").is_dir() or not 0 < args.timeout < float("inf"):
        parser.error("Library must contain categories and --timeout must be positive")
    if args.backup and (args.backup.is_relative_to(args.library / "categories") or args.library.is_relative_to(args.backup)):
        parser.error("Backup must be outside categories and must not contain the library")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        with FileLock(args.library / ".sync.log.lock", timeout=0):
            return sync(args)
    except Timeout:
        print("Already running.", flush=True)
        return 0
    except Exception as error:
        print(f"Failed: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
