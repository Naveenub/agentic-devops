"""Legal documents served by the product (/terms, /privacy, /dpa, /refunds) and the gate that keeps them honest.

The texts are Markdown templates in product/legal_docs/. `{{TOKEN}}` placeholders are filled from
product/legal_docs/entity.json (override the path with PRODUCT_LEGAL_FILE). The documents are only published, and public
signup only opens, when every field is filled in AND COUNSEL_REVIEWED is true, so an unfinished draft is never shown to
customers and nobody accepts a text that still has blanks.

  python -m product.legal check   list what is still missing (exit 1 if anything is)
  python -m product.legal fill    write the entity values into product/LICENSE and CLA.md (their {{TOKENS}})"""
import html
import json
import os
import re
import sys
from pathlib import Path

DOCS = Path(__file__).parent / "legal_docs"
ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.1"  # bump when the text changes; stored with each signup as the version that was accepted
FIELDS = ("LEGAL_NAME", "ENTITY_TYPE", "LEGAL_ADDRESS", "LEGAL_EMAIL", "SALES_CONTACT", "GOVERNING_LAW", "VENUE",
          "LIABILITY_CAP", "REFUND_WINDOW_DAYS", "HOSTING_PROVIDER", "EFFECTIVE_DATE")
TITLES = {"terms": "Terms of Service", "privacy": "Privacy Policy", "dpa": "Data Processing Addendum",
          "refunds": "Refunds and Cancellation"}
TOKEN = re.compile(r"\{\{([A-Z_]+)\}\}")
FILES_WITH_TOKENS = (ROOT / "product" / "LICENSE", ROOT / "CLA.md")


def entity_path() -> Path:
    return Path(os.getenv("PRODUCT_LEGAL_FILE") or DOCS / "entity.json")


def load_entity() -> dict:
    try:
        data = json.loads(entity_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _value(data: dict, key: str) -> str:
    v = data.get(key)
    return "" if v is None or isinstance(v, bool) else " ".join(str(v).split())


def missing(data: dict | None = None) -> list:
    """Field names that are not filled in, plus COUNSEL_REVIEWED while it is not true."""
    data = load_entity() if data is None else data
    out = [k for k in FIELDS if not _value(data, k)]
    if data.get("COUNSEL_REVIEWED") is not True:
        out.append("COUNSEL_REVIEWED")
    return out


def configured() -> bool:
    return not missing()


def fill(text: str, data: dict) -> str:
    return TOKEN.sub(lambda m: _value(data, m.group(1)) or m.group(0), text)


def source(slug: str) -> str | None:
    if slug not in TITLES:
        return None
    return (DOCS / f"{slug}.md").read_text(encoding="utf-8")


_LINK = re.compile(r"\[([^\]]+)\]\((/[a-z]+|https://[^)\s]+|mailto:[^)\s]+)\)")


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)


def render(slug: str) -> str | None:
    """HTML body for one document, or None while the legal setup is incomplete (callers answer 503)."""
    data = load_entity()
    raw = source(slug)
    if raw is None or missing(data):
        return None
    out, para, items = [], [], []

    def flush():
        nonlocal para, items
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
        para, items = [], []

    for line in fill(raw, data).splitlines():
        s = line.strip()
        if not s:
            flush()
        elif s.startswith("# "):
            flush()
            out.append(f"<h1>{_inline(s[2:])}</h1>")
        elif s.startswith("## "):
            flush()
            out.append(f"<h2>{_inline(s[3:])}</h2>")
        elif s.startswith("- "):
            if para:
                flush()
            items.append(s[2:])
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return "\n".join(out)


def unresolved_tokens() -> dict:
    """{file: [tokens]} for the licence files that still contain {{TOKENS}}."""
    found = {}
    for f in FILES_WITH_TOKENS:
        toks = sorted(set(TOKEN.findall(f.read_text(encoding="utf-8")))) if f.exists() else []
        if toks:
            found[str(f.relative_to(ROOT))] = toks
    return found


def main(argv: list) -> int:
    cmd = argv[0] if argv else "check"
    data = load_entity()
    if cmd == "fill":
        for f in FILES_WITH_TOKENS:
            f.write_text(fill(f.read_text(encoding="utf-8"), data), encoding="utf-8")
        cmd = "check"
    if cmd != "check":
        print("usage: python -m product.legal [check|fill]")
        return 2
    gaps, left = missing(data), unresolved_tokens()
    for k in gaps:
        print(f"missing: {k}" + (" (set to true once counsel has approved the texts)" if k == "COUNSEL_REVIEWED" else ""))
    for f, toks in left.items():
        print(f"unresolved in {f}: {', '.join(toks)}  (run: python -m product.legal fill)")
    print("legal: OK" if not (gaps or left) else "legal: NOT READY")
    return 1 if (gaps or left) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
