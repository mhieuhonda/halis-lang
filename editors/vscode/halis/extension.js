/**
 * Halis VS Code extension — Stage 14 release, debugger added in Stage
 * 121 (v0.140.0-alpha).
 *
 * Provides:
 *   - language server (tools/hls-lsp.py) over stdio
 *   - on-save formatter (tools/hlfmt.py -w)
 *   - on-save linter (tools/hllint.py)
 *   - debugger (tools/hls-dap.py): the Debug Adapter Protocol over
 *     stdio, launched through a descriptor factory — no extra npm
 *     dependency, the API is built into VS Code
 *   - syntax highlighting via TextMate grammar
 *
 * The extension auto-discovers the toolchain relative to:
 *   1. the matching setting (languageServerPath / debugAdapterPath)
 *      if set
 *   2. <workspace>/tools/<fallback>
 *   3. the fallback name on PATH
 */
const vscode = require('vscode');
const path = require('path');
const cp = require('child_process');
const fs = require('fs');

let client = null;

function findTool(workspaceFolders, settingName, fallback) {
    const cfg = vscode.workspace.getConfiguration('halis');
    const explicit = cfg.get(settingName);
    if (explicit && fs.existsSync(explicit)) return explicit;
    if (workspaceFolders && workspaceFolders.length > 0) {
        const candidate = path.join(workspaceFolders[0].uri.fsPath, 'tools', fallback);
        if (fs.existsSync(candidate)) return candidate;
    }
    return fallback;  // rely on PATH
}

function startServer(context) {
    const cfg = vscode.workspace.getConfiguration('halis');
    const python = cfg.get('pythonPath') || 'python3';
    const serverPath = findTool(
        vscode.workspace.workspaceFolders,
        'languageServerPath',
        'hls-lsp.py'
    );

    const serverOptions = {
        run: { command: python, args: [serverPath] },
        debug: { command: python, args: [serverPath] }
    };

    const clientOptions = {
        documentSelector: [{ scheme: 'file', language: 'halis' }],
        synchronize: {
            fileEvents: vscode.workspace.createFileSystemWatcher('**/*.hls')
        }
    };

    // Use the bundled vscode-languageclient if available, otherwise fall
    // back to a minimal stdio transport. Many VS Code installs ship
    // vscode-languageclient by default; we lazy-require it.
    try {
        const { LanguageClient, TransportKind } = require('vscode-languageclient');
        client = new LanguageClient(
            'halisLanguageServer',
            'Halis Language Server',
            serverOptions,
            clientOptions
        );
        client.start();
        context.subscriptions.push({ dispose: () => client && client.stop() });
    } catch (e) {
        vscode.window.showWarningMessage(
            'Halis: vscode-languageclient not available — language-server features disabled. ' +
            'Install the "Halis" extension dependencies or run `npm install vscode-languageclient`.'
        );
    }
}

function activate(context) {
    startServer(context);
    registerDebugger(context);

    // Format File command — runs `hlfmt -w` on the active document.
    const formatCmd = vscode.commands.registerCommand('halis.formatFile', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor || editor.document.languageId !== 'halis') return;
        const cfg = vscode.workspace.getConfiguration('halis');
        const python = cfg.get('pythonPath') || 'python3';
        const fmtPath = findTool(
            vscode.workspace.workspaceFolders,
            null,
            'hlfmt.py'
        );
        const filePath = editor.document.uri.fsPath;
        return new Promise((resolve, reject) => {
            const proc = cp.spawn(python, [fmtPath, '-w', filePath]);
            proc.on('exit', (code) => {
                if (code === 0) {
                    vscode.window.showInformationMessage('Halis: formatted ' + path.basename(filePath));
                    resolve();
                } else {
                    vscode.window.showErrorMessage('Halis: hlfmt failed (exit ' + code + ')');
                    reject(new Error('hlfmt exit ' + code));
                }
            });
        });
    });
    context.subscriptions.push(formatCmd);

    // Lint File command — runs `hllint --strict` and prints output to a channel.
    const lintCmd = vscode.commands.registerCommand('halis.lintFile', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor || editor.document.languageId !== 'halis') return;
        const cfg = vscode.workspace.getConfiguration('halis');
        const python = cfg.get('pythonPath') || 'python3';
        const lintPath = findTool(
            vscode.workspace.workspaceFolders,
            null,
            'hllint.py'
        );
        const filePath = editor.document.uri.fsPath;
        const channel = vscode.window.createOutputChannel('Halis Lint');
        channel.clear();
        channel.show(true);
        const proc = cp.spawn(python, [lintPath, '--strict', filePath]);
        let out = '';
        proc.stdout.on('data', (d) => out += d.toString());
        proc.stderr.on('data', (d) => out += d.toString());
        proc.on('exit', (code) => {
            channel.append(out);
            if (code === 0) {
                channel.appendLine('\n[lint clean]');
            }
        });
    });
    context.subscriptions.push(lintCmd);

    // Restart server command.
    const restartCmd = vscode.commands.registerCommand('halis.restartServer', async () => {
        if (client) {
            await client.stop();
            await client.start();
            vscode.window.showInformationMessage('Halis: language server restarted.');
        }
    });
    context.subscriptions.push(restartCmd);

    // Debug Program command — launch the active file (or pick one).
    const debugCmd = vscode.commands.registerCommand('halis.startDebugging', async () => {
        const editor = vscode.window.activeTextEditor;
        let program = null;
        if (editor && editor.document.languageId === 'halis') {
            program = editor.document.uri.fsPath;
        } else if (vscode.workspace.workspaceFolders) {
            const picks = await vscode.workspace.findFiles(
                '**/*.hls', '**/.hls-pkg-deps/**', 50);
            if (picks.length === 0) {
                vscode.window.showWarningMessage(
                    'Halis: no .hls files found in the workspace.');
                return;
            }
            const chosen = await vscode.window.showQuickPick(
                picks.map((p) => ({
                    label: path.basename(p.fsPath),
                    description: path.dirname(p.fsPath),
                    uri: p
                })),
                { placeHolder: 'Select a Halis program to debug' }
            );
            if (!chosen) return;
            program = chosen.uri.fsPath;
        }
        if (!program) return;
        vscode.debug.startDebugging(undefined, {
            type: 'halis',
            request: 'launch',
            name: 'Halis: ' + path.basename(program),
            program: program
        });
    });
    context.subscriptions.push(debugCmd);

    // The Halis debugger: the adapter runs as a Python child process
    // speaking DAP over stdio (the same shape as the language server).
    // A descriptor factory is all VS Code needs — DebugAdapterExecutable
    // is built-in, so the extension ships with zero npm dependencies.
    function registerDebugger(context) {
        const factory = {
            createDebugAdapterDescriptor(session) {
                const cfg = vscode.workspace.getConfiguration('halis');
                const python = cfg.get('pythonPath') || 'python3';
                const dapPath = findTool(
                    vscode.workspace.workspaceFolders,
                    'debugAdapterPath',
                    'hls-dap.py'
                );
                return new vscode.DebugAdapterExecutable(python, [dapPath]);
            }
        };
        context.subscriptions.push(
            vscode.debug.registerDebugAdapterDescriptorFactory('halis', factory)
        );
    }

    // Format-on-save.
    vscode.workspace.onWillSaveTextDocument((event) => {
        const cfg = vscode.workspace.getConfiguration('halis');
        if (!cfg.get('formatOnSave')) return;
        if (event.document.languageId !== 'halis') return;
        const fmtPath = findTool(
            vscode.workspace.workspaceFolders,
            null,
            'hlfmt.py'
        );
        const python = cfg.get('pythonPath') || 'python3';
        const filePath = event.document.uri.fsPath;
        // Run synchronously (formatted source must be on disk before save).
        try {
            cp.execFileSync(python, [fmtPath, '-w', filePath], { stdio: 'ignore' });
        } catch (e) {
            vscode.window.showErrorMessage('Halis: format-on-save failed');
        }
    });
}

function deactivate() {
    if (client) {
        return client.stop();
    }
    return undefined;
}

module.exports = { activate, deactivate };
