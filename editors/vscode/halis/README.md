# Halis — VS Code Extension

Stage 14 release of the Halis toolchain ships an official VS Code
extension that wires the `hls-lsp` language server, the `hlfmt`
formatter, and the `hllint` linter into VS Code.

## Features

- **Language Server** — full LSP via `tools/hls-lsp.py`:
  - hover (inferred types)
  - go-to-definition (cross-file, follows imports)
  - find references
  - rename refactoring (across all open documents)
  - document symbols (outline view)
  - diagnostics (type + effects errors as you type)
  - completion (keywords + builtins + identifiers)
- **Formatter** — `Format File` command runs `hlfmt -w` on the active
  document. Enable `halis.formatOnSave` to run on every save.
- **Linter** — `Lint File` command runs `hllint --strict` and shows
  the output in a `Halis Lint` channel.
- **Syntax highlighting** — TextMate grammar covering keywords,
  built-in types, effects, strings, numbers, comments, function names.

## Installation

The extension is a single-folder plugin (no npm build step needed).
To install locally for development:

```bash
cd editors/vscode/halis
# Install the vscode-languageclient npm dependency for LSP:
npm install vscode-languageclient
# Then in VS Code: Run "Extensions: Install from Location..."
# and pick the editors/vscode/halis folder.
```

Alternatively, run the extension in the Extension Development Host:

1. Open the `editors/vscode/halis` folder in VS Code.
2. Press `F5` (or `Run > Start Debugging`).
3. A new VS Code window opens with the Halis extension loaded.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `halis.languageServerPath` | `""` | Path to `hls-lsp.py`. Empty = auto-discover. |
| `halis.formatOnSave` | `false` | Run `hlfmt -w` on save. |
| `halis.lintOnSave` | `true` | Run `hllint` on save. |
| `halis.pythonPath` | `"python3"` | Python interpreter for the toolchain. |

## Auto-discovery

When `halis.languageServerPath` is empty, the extension looks for:

1. `<workspace>/tools/hls-lsp.py`
2. `hls-lsp` on `PATH` (assumes the repo is installed system-wide)

The same auto-discovery applies to `hlfmt.py` and `hllint.py`.
