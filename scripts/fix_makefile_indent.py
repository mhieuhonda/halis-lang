#!/usr/bin/env python3
"""Convert leading 8-space indent (used in Makefile command lines) to TAB.
Make REQUIRES tabs (not spaces) for command-line indentation. The
Makefile in this repo was authored with 8-space indentation in some
editors that silently converted tabs to spaces; `make` rejects it.
This script restores a TAB on every line whose leading whitespace
is exactly 8 spaces AND the line starts with a command character
(anything except a target name + colon). Continuation lines
(starting with whitespace then content) also use TAB."""
import sys

def fix_makefile(path):
    with open(path, "r") as f:
        lines = f.read().splitlines(keepends=False)
    out = []
    in_recipe = False
    for line in lines:
        # Detect a target line: "name: deps" at column 0.
        stripped = line.lstrip()
        if not line.startswith(" ") and not line.startswith("\t"):
            # Column 0 line.
            in_recipe = False
            # A target line has a colon (but not := which is variable assignment).
            if ":" in line and not line.startswith("\t") and ":=" not in line.split(":")[0] + ":":
                # Could be a target. Mark to convert following indented lines.
                in_recipe = True
            out.append(line)
            continue
        # Indented line: if we're in a recipe, replace leading 8 spaces with TAB.
        if in_recipe:
            # Replace leading runs of 8 spaces with TABs.
            new_line = line
            # Count leading spaces.
            n_leading = 0
            while n_leading < len(new_line) and new_line[n_leading] == " ":
                n_leading += 1
            # Convert every 8 spaces to one TAB (Makefile convention).
            n_tabs = n_leading // 8
            n_remainder = n_leading % 8
            new_line = ("\t" * n_tabs) + (" " * n_remainder) + new_line[n_leading:]
            out.append(new_line)
        else:
            out.append(line)
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")

if __name__ == "__main__":
    fix_makefile(sys.argv[1] if len(sys.argv) > 1 else "Makefile")
