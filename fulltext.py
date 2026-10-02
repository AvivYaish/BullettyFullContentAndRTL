"""Extract an article URL as Markdown on stdout."""

import argparse
import sys
from urllib.request import Request, urlopen

from trafilatura import extract


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Article URL")
    parser.add_argument("--timeout", type=float, default=30, help="Download timeout in seconds")
    args = parser.parse_args(argv)
    if not 0 < args.timeout < float("inf"):
        parser.error("--timeout must be positive")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="")
    try:
        if not args.url.startswith(("https://", "http://")):
            raise ValueError("URL must start with http:// or https://")
        with urlopen(Request(args.url, headers={"User-Agent": "Mozilla/5.0"}), timeout=args.timeout) as response:
            body = extract(response.read(), url=response.geturl(), output_format="markdown", include_comments=False, include_links=True, favor_recall=True)
        if not (body or "").strip():
            raise ValueError("No article text extracted")
        sys.stdout.write(body.rstrip().replace("\r\n", "\n").replace("\r", "\n") + "\n")
    except Exception as error:
        print(f"Failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
