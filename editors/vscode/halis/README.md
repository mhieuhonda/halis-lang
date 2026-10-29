# Halis — VS Code extension

The official VS Code extension for the Halis toolchain. It wires the
`hls-lsp` language server, the `hlfmt` formatter, the `hllint`
linter and the `hls-dap` debugger into the editor.

## Features

- **Language server** — full LSP via `tools/hls-lsp.py`:
  - hover with inferred types
  - go-to-definition (cross-file, follows imports)
  - find references, rename across open documents
  - document symbols (outline)
  - diagnostics — type and effects errors as you type
  - completion for keywords, builtins and identifiers
- **Formatter** — the `Format File` command runs `hlfmt -w` on the
  active document; enable `halis.formatOnSave` to format on every
  save.
- **Linter** — the `Lint File` command runs `hllint --strict` and
  shows the output in a dedicated channel; `halis.lintOnSave`
  repeats it on save.
- **Debugger** — debug a Halis program through the debug adapter
  (`tools/hls-dap.py`, Debug Adapter Protocol over stdio):
  - breakpoints verified against the real AST (a line without a
    statement snaps to the nearest one and says so)
  - step over / into / out, stop on entry, pause
  - variables view: locals with declared types, struct fields, enum
    payloads, list elements
  - a debug console evaluating real expressions in the paused frame
    (arithmetic, field chains, user functions, builtins)
  - panics stop the program at the panic site with the real message
    and stack (the `panics` exception filter, on by default)
  - spawned tasks appear as threads with their own stacks

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
file open. The adapter is launched automatically with the configured
Python (`halis.pythonPath`); point `halis.debugAdapterPath` at a
specific `hls-dap.py` to override auto-discovery.

## Installation

The extension is a single-folder plugin (no npm build step needed).
To install locally for development:

```bash
cd editors/vscode/halis
npm install vscode-languageclient   # the one LSP dependency
# Then in VS Code: "Extensions: Install from Location..."
# and pick the editors/vscode/halis folder.
```

To run it in the Extension Development Host:

1. Open the `editors/vscode/halis` folder in VS Code.
2. Press `F5` (or *Run > Start Debugging*).
3. A new window opens with the Halis extension loaded.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `halis.languageServerPath` | `""` | Path to `hls-lsp.py`. Empty = auto-discover. |
| `halis.debugAdapterPath` | `""` | Path to `hls-dap.py`. Empty = auto-discover. |
| `halis.formatOnSave` | `false` | Run `hlfmt -w` on save. |
| `halis.lintOnSave` | `true` | Run `hllint` on save. |
| `halis.pythonPath` | `"python3"` | Interpreter for the toolchain (LSP, formatter, linter, debug adapter). |

## Auto-discovery

When a path setting is empty, the extension looks for:

1. `<workspace>/tools/<tool>.py` (`hls-lsp.py`, `hlfmt.py`,
   `hllint.py`, `hls-dap.py`)
2. the tool name on `PATH` (for system-wide installs)
