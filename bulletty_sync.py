"""
Update bulletty library, pull full content for new articles and format RTL text.
Run using: `python bulletty_sync.py ; bulletty --no-hooks`.
Install prerequisites using: `python -m pip install pyicu-wheels==2.15.2 wcwidth==0.9.1 markdown-it-py==4.2.0 mdit-py-plugins==0.6.1 trafilatura==2.3.0 "filelock>=3.16,<4"`.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from unicodedata import bidirectional

from filelock import FileLock, Timeout

sys.dont_write_bytecode = True
import hebfix

FIELDS = re.compile(r'''(?m)^([ \t]*(title|description|author|text)[ \t]*=[ \t]*)'''
                    r"""(?:\"{3}.*?\"{3}|'{3}.*?'{3}|\"(?:\\.|[^\"\\\r\n])*\"|'[^'\r\n]*')|^[ \t]*\[.*""", re.S)
GUARD = "[hebfix]: #\n\n"


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / "content"
        temporary.write_bytes(content)
        temporary.replace(path)


def parts(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not (header := re.match(r"\A\ufeff?\+{3}\n(.*?)\n\+{3}(?:\n|$)", text, re.S)):
        raise ValueError("Missing Bulletty article header")
    return header[0], tomllib.loads(header[1]), text[header.end():]


def metadata(prefix, fields):
    def replace(match):
        if not isinstance(value := fields.get(match[2]), str):
            return match[0]
        right = next((bidirectional(char) in ("R", "AL") for char in value if char.isalpha()), False)
        return match[1] + json.dumps(hebfix.display(value, right, protect_urls=True), ensure_ascii=False)
    return FIELDS.sub(replace, prefix)


def download(url, timeout):
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "trafilatura.cli", "-u", url, "--markdown", "--links", "--no-comments", "--recall"], capture_output=True, text=True, encoding="utf-8", timeout=timeout, stdin=subprocess.DEVNULL)
    if result.returncode or not result.stdout.strip():
        raise ValueError(result.stderr.strip() or "No article text extracted")
    return result.stdout


def configured_library():
    return Path(subprocess.run(["bulletty", "--no-hooks", "dirs", "library"], capture_output=True, text=True, encoding="utf-8", check=True, stdin=subprocess.DEVNULL, timeout=10).stdout.strip()).resolve()


def update(library):
    if configured_library() != library:
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
    state_path, categories, width = args.library / ".sync.json", args.library / "categories", args.width
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if width == "auto":
        appearance = args.library / ".appearance.toml"
        settings = tomllib.loads(appearance.read_text(encoding="utf-8")) if appearance.exists() else {}
        size = shutil.get_terminal_size().columns
        width = max(1, min(size - 9, (size - 8) * settings.get("reader_width", 60) // 100))
    failed, changed = (0 if args.reflow else int(bool(update(args.library)))), 0
    names = (path.relative_to(categories).as_posix() for path in sorted(categories.rglob("*")) if path.is_file() and path.suffix.lower() in (".md", ".markdown"))
    records = ((name, state.get(name, {})) for name in names)
    queued = [(name, record) for name, record in records if (bool(record.get("layout")) if args.reflow else args.retry or record.get("download") is None or "layout" not in record)]
    for number, (name, previous) in enumerate(queued, 1):
        print(f"[{number}/{len(queued)}] {name}", flush=True)
        article, source = categories / name, args.library / ".hebfix" / "source" / name
        record, files, error = previous.copy(), {}, None
        try:
            _, fields, original = parts(article.read_bytes().decode("utf-8"))
            body, layout = original, previous.get("layout", [])
            formatted = bool(layout or re.match(r"\A\[hebfix_*\]: #\n", original))
            rewrite = args.reflow or not formatted
            if args.reflow:
                if not source.exists():
                    raise ValueError("No cached source for reflow. Run --retry once to download it.")
                body = source.read_text(encoding="utf-8")
            elif args.retry or record.get("download") is None:
                url = fields.get("url", "")
                record["download"] = True
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    try:
                        body, rewrite = download(url, args.timeout), True
                    except Exception as failure:
                        record["download"], error = "failed", ("Download timed out" if isinstance(failure, subprocess.TimeoutExpired) else str(failure))
            if rewrite:
                files[source], record["layout"] = body.encode("utf-8"), [width]
                body = hebfix.fix(body, width)
                if "\u00a0" in body:
                    label = "hebfix"
                    while re.search(r"\[\s*" + label + r"\s*\]", body, re.I):
                        label += "_"
                    body = f"[{label}]: #\n\n" + body
            current = article.read_bytes().decode("utf-8")
            prefix, latest, latest_body = parts(current)
            if latest.get("url") != fields.get("url"):
                raise ValueError("Article URL changed during processing")
            if rewrite and latest_body != original:
                raise ValueError("Article body changed during processing")
            if rewrite and not args.reflow and (not formatted or len(layout) == 3 and layout[-1] is False):
                prefix = metadata(prefix, latest)
            rendered = prefix + body if rewrite else current
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
    parser.add_argument("library", nargs="?", type=Path, help="default: Bulletty's configured library")
    parser.add_argument("--backup", type=Path, help="original copies (default: LIBRARY/Backups)")
    parser.add_argument("--width", default="auto", type=lambda value: value if value == "auto" else int(value) if value.isdecimal() else 80, help="columns, auto for reader width, or 0 for no wrapping")
    parser.add_argument("--retry", action="store_true", help="Redownload completed or failed articles")
    parser.add_argument("--reflow", action="store_true", help="Reformat cached logical sources without downloading")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        args.library = (args.library or configured_library()).expanduser().resolve()
    except Exception as error:
        parser.error(str(error))
    args.backup = (args.backup or args.library / "Backups").expanduser().resolve()
    if not (args.library / "categories").is_dir() or not 0 < args.timeout < float("inf"):
        parser.error("Library must contain categories and --timeout must be positive")
    if args.backup.is_relative_to(args.library / "categories") or args.library.is_relative_to(args.backup):
        parser.error("Backup must be outside categories and must not contain the library")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        with FileLock(args.library / ".sync.log.lock", timeout=0):
            return sync(args)
    except Timeout:
        print("Already running.", flush=True)
        return 1
    except Exception as error:
        print(f"Failed: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
