# Halis — VS Code Extension

The official VS Code extension for the Halis toolchain: it wires the
`hls-lsp` language server, the `hlfmt` formatter, the `hllint` linter,
and — since Stage 121 (v0.140.0-alpha) — the `hls-dap` debugger into
VS Code.

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
- **Debugger** (Stage 121) — debug a Halis program through the
  Stage-0 interpreter via `tools/hls-dap.py` (Debug Adapter Protocol):
  - breakpoints, verified against the real AST (a line without a
    statement snaps to the nearest one and says so)
  - step over / into / out, stop on entry, pause
  - the variables view: locals with declared types, struct fields,
    enum payloads, list elements
  - a debug console that evaluates real expressions in the paused
    frame (arithmetic, field chains, user fns, builtins)
  - panics stop the program dead at the panic site with the real
    message and stack (the `panics` exception filter, on by default)
  - spawned tasks appear as threads with their own stacks
  - `Halis: Debug Program` command launches the active file (or picks
    one from the workspace)

## Debugging

Add a launch configuration to `.vscode/launch.json`:

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "type": "halis",
            "request": "launch",
            "name": "Debug Halis program",
            "program": "${workspaceFolder}/main.hls",
            "args": [],
            "stopOnEntry": false
        }
    ]
}
```

Or run `Halis: Debug Program` from the command palette with a `.hls`
file open. Launch snippets are offered by the editor (Insert Snippet
in a launch.json). The adapter is launched automatically with the
configured Python (`halis.pythonPath`); point `halis.debugAdapterPath`
at a specific `hls-dap.py` to override auto-discovery.

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
| `halis.debugAdapterPath` | `""` | Path to `hls-dap.py`. Empty = auto-discover. |
| `halis.formatOnSave` | `false` | Run `hlfmt -w` on save. |
| `halis.lintOnSave` | `true` | Run `hllint` on save. |
| `halis.pythonPath` | `"python3"` | Python interpreter for the toolchain (LSP, formatter, linter, debug adapter). |

## Auto-discovery

When a path setting is empty, the extension looks for:

1. `<workspace>/tools/<tool>.py` (`hls-lsp.py`, `hlfmt.py`,
   `hllint.py`, `hls-dap.py`)
2. the tool name on `PATH` (assumes the repo is installed system-wide)
