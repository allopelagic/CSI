"""Build the prescribed archive without caches, credentials or environments."""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("server.py", "client.py", "server_network.py", "game.py", "protocol.py",
           "question_bank.py", "questions.json", "README.md", "PROTOCOL.md")


def build(output, documentation=None, *, code_only=False, root=ROOT):
    if code_only == (documentation is not None):
        raise ValueError("Choose exactly one of --documentation or --code-only.")
    root, output = Path(root), Path(output)
    files = [root / filename for filename in SOURCES]
    files += sorted((root / "tests").glob("*.py"))
    files += sorted((root / "tools").glob("*.py"))
    files += sorted((root / "docs").glob("*.md"))
    for source in files:
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Missing or symlinked source: {source.name}")
    pdf = None
    if documentation is not None:
        pdf = Path(documentation)
        with pdf.open("rb") as source:
            header = source.read(5)
            source.seek(max(0, pdf.stat().st_size - 1024))
            trailer = source.read()
        if header != b"%PDF-" or b"%%EOF" not in trailer:
            raise ValueError("Documentation must be an existing PDF with PDF header and EOF marker.")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves an existing archive rather than silently replacing it.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in files:
            archive.write(source, "Codes/" + source.relative_to(root).as_posix())
        if pdf is not None:
            archive.write(pdf, "Documentation/Development_Documentation.pdf")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--documentation", type=Path, help="final team-reviewed PDF")
    mode.add_argument("--code-only", action="store_true", help="incomplete code-only development archive")
    args = parser.parse_args(argv)
    try:
        output = build(args.output, args.documentation, code_only=args.code_only)
    except (OSError, ValueError) as exc:
        print(f"Packaging failed: {exc}", file=sys.stderr)
        return 1
    print(f"Created {output}")
    if args.code_only:
        print("CODE_ONLY: no final PDF; this is not a complete assessed submission.")
    else:
        print("PDF included; the team must verify formatting, authorship and official-image evidence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
