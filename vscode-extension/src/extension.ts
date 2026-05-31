import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

function getPort(): number {
    // Try reading from config file first, then VS Code setting
    const cfgFile = path.join(
        process.env.HOME || process.env.USERPROFILE || '',
        '.config', 'devmem', 'config.json'
    );
    try {
        const raw = fs.readFileSync(cfgFile, 'utf-8');
        const cfg = JSON.parse(raw);
        if (cfg.daemon_port) { return cfg.daemon_port; }
    } catch { /* ignore */ }

    const vsCfg = vscode.workspace.getConfiguration('devmem');
    return vsCfg.get<number>('port', 27182);
}

function isEnabled(): boolean {
    return vscode.workspace.getConfiguration('devmem').get<boolean>('enabled', true);
}

// ---------------------------------------------------------------------------
// HTTP fire-and-forget
// ---------------------------------------------------------------------------

async function sendEvent(type: string, data: Record<string, unknown>): Promise<void> {
    if (!isEnabled()) { return; }
    const port = getPort();
    const body = JSON.stringify({ type, ts: new Date().toISOString(), ...data });
    try {
        await fetch(`http://127.0.0.1:${port}/event`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body,
            signal: AbortSignal.timeout(500),
        });
    } catch {
        // Daemon may not be running — silently ignore
    }
}

// ---------------------------------------------------------------------------
// Active time tracking
// ---------------------------------------------------------------------------

let lastActivityAt: number = Date.now();
let activeTimeInterval: ReturnType<typeof setInterval> | undefined;

function recordActivity(): void {
    lastActivityAt = Date.now();
}

// ---------------------------------------------------------------------------
// Extension activation
// ---------------------------------------------------------------------------

export function activate(context: vscode.ExtensionContext): void {
    // Report workspace open for each workspace folder
    const folders = vscode.workspace.workspaceFolders;
    if (folders) {
        for (const folder of folders) {
            sendEvent('workspace_open', { workspace: folder.uri.fsPath });
        }
    }

    // Workspace folders changed
    context.subscriptions.push(
        vscode.workspace.onDidChangeWorkspaceFolders(e => {
            for (const added of e.added) {
                sendEvent('workspace_open', { workspace: added.uri.fsPath });
            }
        })
    );

    // File save
    context.subscriptions.push(
        vscode.workspace.onDidSaveTextDocument(doc => {
            recordActivity();
            const workspaceFolder = vscode.workspace.getWorkspaceFolder(doc.uri);
            sendEvent('file_save', {
                file: doc.uri.fsPath,
                language: doc.languageId,
                workspace: workspaceFolder?.uri.fsPath ?? '',
            });
        })
    );

    // Track activity for active_time reporting
    context.subscriptions.push(
        vscode.window.onDidChangeActiveTextEditor(recordActivity),
        vscode.workspace.onDidChangeTextDocument(recordActivity),
    );

    // File lifecycle events
    context.subscriptions.push(
        vscode.workspace.onDidCreateFiles(e => {
            recordActivity();
            for (const uri of e.files) {
                const workspaceFolder = vscode.workspace.getWorkspaceFolder(uri);
                sendEvent('file_create', {
                    file: uri.fsPath,
                    filename: uri.fsPath.split('/').pop() ?? '',
                    workspace: workspaceFolder?.uri.fsPath ?? '',
                });
            }
        }),
        vscode.workspace.onDidDeleteFiles(e => {
            recordActivity();
            for (const uri of e.files) {
                const workspaceFolder = vscode.workspace.getWorkspaceFolder(uri);
                sendEvent('file_delete', {
                    file: uri.fsPath,
                    filename: uri.fsPath.split('/').pop() ?? '',
                    workspace: workspaceFolder?.uri.fsPath ?? '',
                });
            }
        }),
        vscode.workspace.onDidRenameFiles(e => {
            recordActivity();
            for (const { oldUri, newUri } of e.files) {
                const workspaceFolder = vscode.workspace.getWorkspaceFolder(newUri);
                sendEvent('file_rename', {
                    old_file: oldUri.fsPath,
                    new_file: newUri.fsPath,
                    old_filename: oldUri.fsPath.split('/').pop() ?? '',
                    new_filename: newUri.fsPath.split('/').pop() ?? '',
                    workspace: workspaceFolder?.uri.fsPath ?? '',
                });
            }
        }),
    );

    // Debug session events
    context.subscriptions.push(
        vscode.debug.onDidStartDebugSession(session => {
            const workspace = session.workspaceFolder?.uri.fsPath ?? '';
            sendEvent('debug_session_start', {
                name: session.name,
                debug_type: session.type,
                workspace,
            });
        }),
        vscode.debug.onDidTerminateDebugSession(session => {
            const workspace = session.workspaceFolder?.uri.fsPath ?? '';
            sendEvent('debug_session_end', {
                name: session.name,
                debug_type: session.type,
                workspace,
            });
        }),
    );

    // Test task events (tasks in the Test group)
    context.subscriptions.push(
        vscode.tasks.onDidStartTask(e => {
            if (e.execution.task.group === vscode.TaskGroup.Test) {
                const scope = e.execution.task.scope;
                const workspace = (scope && typeof scope !== 'number')
                    ? (scope as vscode.WorkspaceFolder).uri.fsPath : '';
                sendEvent('test_run_start', {
                    name: e.execution.task.name,
                    workspace,
                });
            }
        }),
        vscode.tasks.onDidEndTaskProcess(e => {
            if (e.execution.task.group === vscode.TaskGroup.Test) {
                const scope = e.execution.task.scope;
                const workspace = (scope && typeof scope !== 'number')
                    ? (scope as vscode.WorkspaceFolder).uri.fsPath : '';
                sendEvent('test_run_finish', {
                    name: e.execution.task.name,
                    exit_code: e.exitCode ?? 0,
                    workspace,
                });
            }
        }),
    );

    // Report active time every 5 minutes
    activeTimeInterval = setInterval(() => {
        const secondsSinceActivity = (Date.now() - lastActivityAt) / 1000;
        // Only report if user was active in the last 5 minutes
        if (secondsSinceActivity < 300) {
            const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
            sendEvent('active_time', {
                workspace,
                seconds_active: 300 - secondsSinceActivity,
            });
        }
    }, 5 * 60 * 1000);
}

// ---------------------------------------------------------------------------
// Extension deactivation
// ---------------------------------------------------------------------------

export function deactivate(): void {
    if (activeTimeInterval) {
        clearInterval(activeTimeInterval);
    }
    const folders = vscode.workspace.workspaceFolders;
    if (folders) {
        for (const folder of folders) {
            // Fire-and-forget — deactivation is synchronous, best effort
            const port = getPort();
            const body = JSON.stringify({
                type: 'workspace_close',
                ts: new Date().toISOString(),
                workspace: folder.uri.fsPath,
            });
            try {
                // Synchronous best-effort using Node.js http module
                const http = require('http');
                const req = http.request({
                    hostname: '127.0.0.1', port, path: '/event', method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    timeout: 200,
                });
                req.write(body);
                req.end();
            } catch { /* ignore */ }
        }
    }
}
