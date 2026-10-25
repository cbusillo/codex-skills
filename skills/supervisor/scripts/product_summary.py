# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import re
from pathlib import Path


def product_section(text: str) -> str | None:
    lines: list[str] = []
    section_level = 0
    fence = ""
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = ""
        elif marker:
            fence = marker[1]
        else:
            heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading:
                level = len(heading[1])
                if section_level and level <= section_level:
                    break
                if not section_level and re.fullmatch(r"What .+ does", heading[2]):
                    section_level = level
        if section_level:
            lines.append(line)
    section = "\n".join(lines).strip()
    return section or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print only a repository's What <product> does section from AGENTS.md.")
    parser.add_argument("repository", type=Path, help="Local repository checkout")
    args = parser.parse_args(argv)
    if not args.repository.is_dir():
        parser.exit(1, f"Repository directory does not exist: {args.repository}\n")
    agents = args.repository / "AGENTS.md"
    try:
        text = agents.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"No product section: {agents} does not exist.")
        return 0
    except (OSError, UnicodeError) as error:
        parser.exit(1, f"Cannot read {agents}: {error}\n")
    section = product_section(text)
    print(section or f"No What <product> does section in {agents}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
