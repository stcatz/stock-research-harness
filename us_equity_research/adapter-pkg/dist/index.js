import { spawn } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineTool } from '@deepseek-ai/dsh-tools';
export const name = 'us-equity-research-tools';
export const inject = [
    'tools'
];
const SCHEMA_VERSION = '0.1';
const MARKET = 'US';
const DEFAULT_TOP_N = 5;
const DEFAULT_MAX_CHARS = 12000;
const MIN_MAX_CHARS = 500;
const MAX_MAX_CHARS = 20000;
const DEFAULT_TIMEOUT_MS = 120000;
const MAX_TIMEOUT_MS = 600000;
const MAX_CLI_OUTPUT_BYTES = 262144;
const MAX_RENDER_CHARS = 22000;
const MAX_FOCUS_ITEMS = 20;
const MAX_PREVIEW_ITEMS = 5;
const TERMINATION_GRACE_MS = 750;
const DOMAIN_CLI_ERRORS = new Set([
    'ContractError',
    'JSONDecodeError',
    'KeyError',
    'ValueError'
]);
const CHILD_ENV_ALLOWLIST = [
    'HOME',
    'LANG',
    'LC_ALL',
    'LC_CTYPE',
    'PATH',
    'SYSTEMROOT',
    'TMPDIR',
    'TEMP',
    'TMP'
];
const WORKFLOWS = new Set([
    'daily_report',
    'theme_research',
    'stock_research'
]);
const SNAPSHOT_SELECTORS = new Set([
    'demo',
    'latest',
    'id'
]);
const ARTIFACT_SECTIONS = new Set([
    'summary',
    'report',
    'manifest',
    'packet'
]);
const SAFE_IDENTIFIER = /^(?!\.)(?!.*\.\.)[A-Za-z0-9._-]{1,128}$/;
const US_SYMBOL = /^[A-Z][A-Z0-9.-]{0,14}$/;
class BridgeAbortError extends Error {
    constructor(){
        super('US research CLI request was aborted');
        this.name = 'AbortError';
    }
}
class BridgeTimeoutError extends Error {
    constructor(){
        super('US research CLI request timed out');
        this.name = 'BridgeTimeoutError';
    }
}
class BridgeOutputLimitError extends Error {
    constructor(){
        super('US research CLI exceeded its bounded output limit');
        this.name = 'BridgeOutputLimitError';
    }
}
export function resolveProjectRoot(fromUrl = import.meta.url) {
    const currentDir = dirname(fileURLToPath(fromUrl));
    const configured = process.env.US_EQUITY_RESEARCH_ROOT;
    return configured ? resolve(configured) : resolve(currentDir, '..', '..');
}
export function resolveWorkspace(projectRoot) {
    const configured = process.env.STOCK_RESEARCH_WORKSPACE;
    return configured ? resolve(configured) : dirname(projectRoot);
}
export function resolvePythonBin(projectRoot) {
    const configured = process.env.US_EQUITY_RESEARCH_PYTHON_BIN;
    if (configured) {
        return configured;
    }
    return join(projectRoot, '.venv', 'bin', 'python');
}
export function assertSupportedPlatform(platform = process.platform) {
    if (platform !== 'darwin' && platform !== 'linux') {
        throw new Error('us_equity_research adapter supports only macOS and Linux');
    }
}
function ensurePlainObject(value, label) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new Error(`${label} must be an object`);
    }
    return value;
}
function rejectUnknownFields(value, allowed, label) {
    const allowedSet = new Set(allowed);
    const unknown = Object.keys(value).filter((key)=>!allowedSet.has(key)).sort();
    if (unknown.length) {
        throw new Error(`${label} contains unsupported fields: ${unknown.join(', ')}`);
    }
}
function requireInputString(value, field, maxLength = 256) {
    if (typeof value !== 'string' || value.length === 0) {
        throw new Error(`${field} must be a non-empty string`);
    }
    if (value.length > maxLength) {
        throw new Error(`${field} is too long`);
    }
    return value;
}
function optionalInputString(value, field, maxLength = 256) {
    if (value == null) {
        return undefined;
    }
    const raw = requireInputString(value, field, maxLength).trim();
    if (!raw) {
        throw new Error(`${field} must be a non-empty string`);
    }
    return raw;
}
function getInteger(value, field) {
    if (value == null) {
        return undefined;
    }
    if (typeof value !== 'number' || !Number.isSafeInteger(value)) {
        throw new Error(`${field} must be an integer`);
    }
    return value;
}
function getBoolean(value, field) {
    if (value == null) {
        return undefined;
    }
    if (typeof value !== 'boolean') {
        throw new Error(`${field} must be a boolean`);
    }
    return value;
}
function validateIdentifier(value, field) {
    const raw = requireInputString(value, field, 128);
    if (!SAFE_IDENTIFIER.test(raw)) {
        throw new Error(`${field} contains unsupported characters`);
    }
    return raw;
}
function validateSymbol(value) {
    const symbol = requireInputString(value, 'symbol', 15);
    if (!US_SYMBOL.test(symbol)) {
        throw new Error('symbol must match [A-Z][A-Z0-9.-]{0,14}');
    }
    return symbol;
}
function validateDecisionAt(value) {
    const raw = requireInputString(value, 'decision_at', 64);
    if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(raw) || Number.isNaN(Date.parse(raw))) {
        throw new Error('decision_at must be an ISO-8601 datetime with a timezone offset');
    }
    return raw;
}
function normalizeTopN(value) {
    const topN = getInteger(value, 'top_n') ?? DEFAULT_TOP_N;
    if (topN < 1 || topN > 20) {
        throw new Error('top_n must be an integer between 1 and 20');
    }
    return topN;
}
function normalizeMaxChars(value) {
    const maxChars = getInteger(value, 'max_chars') ?? DEFAULT_MAX_CHARS;
    if (maxChars < MIN_MAX_CHARS || maxChars > MAX_MAX_CHARS) {
        throw new Error(`max_chars must be an integer between ${MIN_MAX_CHARS} and ${MAX_MAX_CHARS}`);
    }
    return maxChars;
}
function normalizeRunArgs(args) {
    const raw = ensurePlainObject(args, 'us_research_run arguments');
    rejectUnknownFields(raw, [
        'workflow',
        'decision_at',
        'snapshot',
        'subject',
        'symbol',
        'top_n'
    ], 'us_research_run arguments');
    const workflow = requireInputString(raw.workflow, 'workflow');
    if (!WORKFLOWS.has(workflow)) {
        throw new Error('workflow must be one of daily_report, theme_research, stock_research');
    }
    const decisionAt = validateDecisionAt(raw.decision_at);
    const snapshotRaw = ensurePlainObject(raw.snapshot, 'snapshot');
    rejectUnknownFields(snapshotRaw, [
        'selector',
        'snapshot_id'
    ], 'snapshot');
    const selector = requireInputString(snapshotRaw.selector, 'snapshot.selector');
    if (!SNAPSHOT_SELECTORS.has(selector)) {
        throw new Error('snapshot.selector must be one of demo, latest, id');
    }
    let snapshotId;
    if (selector === 'id') {
        if (snapshotRaw.snapshot_id == null) {
            throw new Error('snapshot.snapshot_id is required when snapshot.selector=id');
        }
        snapshotId = validateIdentifier(snapshotRaw.snapshot_id, 'snapshot.snapshot_id');
    } else if (Object.hasOwn(snapshotRaw, 'snapshot_id')) {
        throw new Error('snapshot.snapshot_id is only allowed when snapshot.selector=id');
    }
    const hasSubject = Object.hasOwn(raw, 'subject');
    const hasSymbol = Object.hasOwn(raw, 'symbol');
    let subject;
    let symbol;
    if (workflow === 'daily_report') {
        if (hasSubject || hasSymbol) {
            throw new Error('daily_report does not accept subject or symbol');
        }
    } else if (workflow === 'theme_research') {
        if (!hasSubject) {
            throw new Error('subject is required for theme_research');
        }
        if (hasSymbol) {
            throw new Error('theme_research does not accept symbol');
        }
        subject = optionalInputString(raw.subject, 'subject', 256);
    } else {
        if (!hasSymbol) {
            throw new Error('symbol is required for stock_research');
        }
        if (hasSubject) {
            throw new Error('stock_research does not accept subject');
        }
        symbol = validateSymbol(raw.symbol);
    }
    return {
        workflow,
        decision_at: decisionAt,
        snapshot: {
            selector,
            ...snapshotId ? {
                snapshot_id: snapshotId
            } : {}
        },
        ...subject ? {
            subject
        } : {},
        ...symbol ? {
            symbol
        } : {},
        top_n: normalizeTopN(raw.top_n)
    };
}
function normalizeArtifactReadArgs(args) {
    const raw = ensurePlainObject(args, 'us_artifact_read arguments');
    rejectUnknownFields(raw, [
        'artifact_id',
        'section',
        'max_chars'
    ], 'us_artifact_read arguments');
    const artifactId = validateIdentifier(raw.artifact_id, 'artifact_id');
    const sectionRaw = raw.section == null ? 'summary' : requireInputString(raw.section, 'section');
    if (!ARTIFACT_SECTIONS.has(sectionRaw)) {
        throw new Error('section must be summary, report, manifest, or packet');
    }
    return {
        artifact_id: artifactId,
        section: sectionRaw,
        max_chars: normalizeMaxChars(raw.max_chars)
    };
}
function redactSensitiveText(value, compact = false) {
    const redacted = value.replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [redacted]').replace(/\b(api[_-]?key|access[_-]?token|authorization|password|secret|token)\b\s*[:=]\s*[^\s,;]+/gi, '$1=[redacted]').replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[redacted-secret]').replace(/(?:\/Users|\/home|\/private|\/tmp|\/var|\/opt|\/Volumes)\/[^\s"'`<>()]+/g, '[redacted-path]').replace(/(^|[\s("'`])\/(?:[A-Za-z0-9._-]+\/)+[A-Za-z0-9._-]+/gm, '$1[redacted-path]').replace(/[A-Za-z]:\\[^\s"'`<>()]+/g, '[redacted-path]').replace(/\\\\[^\s\\]+\\[^\s"'`<>()]+/g, '[redacted-path]');
    return compact ? redacted.replace(/\s+/g, ' ').trim() : redacted;
}
function boundedVisibleText(value, field, maxLength) {
    if (typeof value !== 'string') {
        throw new Error(`${field} must be a string`);
    }
    return redactSensitiveText(value, true).slice(0, maxLength);
}
function boundedOptionalText(value, field, maxLength) {
    if (value == null) {
        return undefined;
    }
    const result = boundedVisibleText(value, field, maxLength);
    return result || undefined;
}
function sanitizeStringArray(value, field, maxItems = MAX_PREVIEW_ITEMS) {
    if (value == null) {
        return [];
    }
    if (!Array.isArray(value)) {
        throw new Error(`${field} must be an array`);
    }
    return value.slice(0, maxItems).flatMap((item, index)=>{
        if (typeof item !== 'string') {
            return [];
        }
        const sanitized = boundedVisibleText(item, `${field}[${index}]`, 500);
        return sanitized ? [
            sanitized
        ] : [];
    });
}
function assertHandshake(payload, label) {
    if (payload.market !== MARKET) {
        throw new Error(`${label} market handshake failed; expected US`);
    }
    if (payload.schema_version !== SCHEMA_VERSION) {
        throw new Error(`${label} schema handshake failed; expected 0.1`);
    }
}
function sanitizeCliError(raw) {
    assertHandshake(raw, 'CLI error result');
    return {
        schema_version: SCHEMA_VERSION,
        market: MARKET,
        error: boundedVisibleText(raw.error, 'error', 80) || 'UnknownError',
        message: boundedVisibleText(raw.message, 'message', 500) || 'unknown CLI failure'
    };
}
function sanitizeCounts(value) {
    const raw = ensurePlainObject(value, 'counts');
    const readCount = (field)=>{
        const count = getInteger(raw[field], `counts.${field}`);
        if (count == null || count < 0) {
            throw new Error(`counts.${field} must be a non-negative integer`);
        }
        return count;
    };
    return {
        observe: readCount('observe'),
        continue_research: readCount('continue_research'),
        exclude: readCount('exclude')
    };
}
function sanitizeFocus(value) {
    if (!Array.isArray(value)) {
        throw new Error('focus must be an array');
    }
    return value.slice(0, MAX_FOCUS_ITEMS).flatMap((item, index)=>{
        if (!item || typeof item !== 'object' || Array.isArray(item)) {
            return [];
        }
        const raw = item;
        const symbol = boundedOptionalText(raw.symbol, `focus[${index}].symbol`, 15);
        const name = boundedOptionalText(raw.name, `focus[${index}].name`, 120);
        if (!symbol || !name || !US_SYMBOL.test(symbol)) {
            return [];
        }
        const focusItem = {
            symbol,
            name
        };
        const theme = boundedOptionalText(raw.theme, `focus[${index}].theme`, 160);
        if (theme !== undefined) {
            focusItem.theme = theme;
        }
        const decision = boundedOptionalText(raw.decision, `focus[${index}].decision`, 40);
        if (decision !== undefined) {
            focusItem.decision = decision;
        }
        const reason = boundedOptionalText(raw.reason, `focus[${index}].reason`, 500);
        if (reason !== undefined) {
            focusItem.reason = reason;
        }
        return [
            focusItem
        ];
    });
}
function sanitizeSections(value) {
    if (!Array.isArray(value)) {
        throw new Error('available_sections must be an array');
    }
    return value.flatMap((item)=>typeof item === 'string' && ARTIFACT_SECTIONS.has(item) ? [
            item
        ] : []);
}
function sanitizeRelativePath(value) {
    if (value == null) {
        return undefined;
    }
    if (typeof value !== 'string' || !value || value.length > 512 || value.includes('\\')) {
        return undefined;
    }
    if (value.startsWith('/') || /^[A-Za-z]:/.test(value) || value.includes('://')) {
        return undefined;
    }
    const parts = value.split('/');
    if (parts.some((part)=>!part || part === '.' || part === '..' || !/^[A-Za-z0-9._-]+$/.test(part))) {
        return undefined;
    }
    return value;
}
function maybeParseJson(raw) {
    try {
        return JSON.parse(raw);
    } catch  {
        return undefined;
    }
}
function childEnvironment() {
    const env = {
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1'
    };
    for (const key of CHILD_ENV_ALLOWLIST){
        const value = process.env[key];
        if (value != null) {
            env[key] = value;
        }
    }
    return env;
}
function terminateProcessTree(child, signal) {
    if (child.pid) {
        try {
            process.kill(-child.pid, signal);
            return;
        } catch (error) {
            if (error.code !== 'ESRCH') {
                try {
                    child.kill(signal);
                } catch  {}
            }
            return;
        }
    }
}
async function spawnBounded(pythonBin, argv, request, projectRoot, options) {
    assertSupportedPlatform(options.platform ?? process.platform);
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > MAX_TIMEOUT_MS) {
        throw new Error(`timeoutMs must be an integer between 1 and ${MAX_TIMEOUT_MS}`);
    }
    if (options.signal?.aborted) {
        throw new BridgeAbortError();
    }
    const processTreeTerminator = options.processTreeTerminator ?? terminateProcessTree;
    const child = spawn(pythonBin, argv, {
        cwd: projectRoot,
        env: childEnvironment(),
        stdio: [
            'pipe',
            'pipe',
            'pipe'
        ],
        detached: true,
        windowsHide: true
    });
    if (!child.stdout || !child.stderr || !child.stdin) {
        processTreeTerminator(child, 'SIGKILL');
        throw new Error('US research CLI process did not expose standard streams');
    }
    let stdout = '';
    let stderr = '';
    let outputBytes = 0;
    let terminationReason;
    let forceTimer;
    let childClosed = false;
    const clearForceTimer = ()=>{
        if (forceTimer) {
            clearTimeout(forceTimer);
            forceTimer = undefined;
        }
    };
    const requestTermination = (reason)=>{
        if (terminationReason || childClosed) {
            return;
        }
        terminationReason = reason;
        processTreeTerminator(child, 'SIGTERM');
        forceTimer = setTimeout(()=>{
            forceTimer = undefined;
            if (!childClosed) {
                processTreeTerminator(child, 'SIGKILL');
            }
        }, TERMINATION_GRACE_MS);
        forceTimer.unref();
    };
    const append = (target, chunk)=>{
        const text = typeof chunk === 'string' ? chunk : chunk.toString('utf8');
        outputBytes += Buffer.byteLength(text);
        if (outputBytes > MAX_CLI_OUTPUT_BYTES) {
            requestTermination('output');
            return;
        }
        if (target === 'stdout') stdout += text;
        else stderr += text;
    };
    child.stdout.on('data', (chunk)=>append('stdout', chunk));
    child.stderr.on('data', (chunk)=>append('stderr', chunk));
    child.stdin.on('error', ()=>{});
    const closed = new Promise((resolvePromise, rejectPromise)=>{
        child.once('error', rejectPromise);
        child.once('close', (exitCode)=>{
            childClosed = true;
            clearForceTimer();
            resolvePromise(exitCode);
        });
    });
    const onAbort = ()=>requestTermination('abort');
    options.signal?.addEventListener('abort', onAbort, {
        once: true
    });
    if (options.signal?.aborted) {
        onAbort();
    }
    const timeout = setTimeout(()=>requestTermination('timeout'), timeoutMs);
    timeout.unref();
    child.stdin.end(`${JSON.stringify(request)}\n`);
    let exitCode;
    try {
        exitCode = await closed;
    } catch (error) {
        throw new Error(`US research CLI could not start: ${redactSensitiveText(String(error.message), true)}`);
    } finally{
        childClosed = true;
        clearTimeout(timeout);
        clearForceTimer();
        options.signal?.removeEventListener('abort', onAbort);
    }
    if (terminationReason === 'abort') throw new BridgeAbortError();
    if (terminationReason === 'timeout') throw new BridgeTimeoutError();
    if (terminationReason === 'output') throw new BridgeOutputLimitError();
    return {
        stdout,
        stderr,
        exitCode
    };
}
export async function callResearchCli(command, request, options = {}) {
    const projectRoot = options.projectRoot ?? resolveProjectRoot();
    const workspace = options.workspace ?? resolveWorkspace(projectRoot);
    const pythonBin = options.pythonBin ?? resolvePythonBin(projectRoot);
    const argv = [
        '-m',
        'us_equity_research.cli',
        '--workspace',
        workspace,
        command,
        '--request-json',
        '-'
    ];
    const { stdout, stderr, exitCode } = await spawnBounded(pythonBin, argv, request, projectRoot, options);
    const stdoutValue = maybeParseJson(stdout);
    const stderrValue = maybeParseJson(stderr);
    if (exitCode !== 0) {
        if (stderrValue && typeof stderrValue === 'object' && !Array.isArray(stderrValue)) {
            const cliError = sanitizeCliError(stderrValue);
            if (DOMAIN_CLI_ERRORS.has(cliError.error)) {
                return cliError;
            }
            throw new Error(`us_equity_research CLI infrastructure failure: ${cliError.error}`);
        }
        throw new Error(`us_equity_research CLI failed for ${command} with non-canonical error output (exit ${String(exitCode)})`);
    }
    if (stdoutValue && typeof stdoutValue === 'object' && !Array.isArray(stdoutValue)) {
        return stdoutValue;
    }
    throw new Error(`us_equity_research CLI returned invalid JSON for ${command}`);
}
export function sanitizeResearchRunResult(raw) {
    const payload = ensurePlainObject(raw, 'run result');
    if (payload.error != null) {
        return sanitizeCliError(payload);
    }
    assertHandshake(payload, 'CLI run result');
    const workflow = boundedVisibleText(payload.workflow, 'workflow', 32);
    if (!WORKFLOWS.has(workflow)) {
        throw new Error('CLI run result returned an unsupported workflow');
    }
    const result = {
        schema_version: SCHEMA_VERSION,
        market: MARKET,
        run_id: validateIdentifier(payload.run_id, 'run_id'),
        artifact_id: validateIdentifier(payload.artifact_id, 'artifact_id'),
        status: boundedVisibleText(payload.status, 'status', 40),
        writer_mode: boundedVisibleText(payload.writer_mode, 'writer_mode', 40),
        data_mode: boundedVisibleText(payload.data_mode, 'data_mode', 40),
        pit_quality: boundedVisibleText(payload.pit_quality, 'pit_quality', 64),
        workflow,
        decision_at: validateDecisionAt(payload.decision_at),
        snapshot_id: validateIdentifier(payload.snapshot_id, 'snapshot_id'),
        analysis_hash: boundedVisibleText(payload.analysis_hash, 'analysis_hash', 128),
        counts: sanitizeCounts(payload.counts),
        focus: sanitizeFocus(payload.focus),
        warnings: sanitizeStringArray(payload.warnings, 'warnings'),
        gaps: sanitizeStringArray(payload.gaps, 'gaps'),
        available_sections: sanitizeSections(payload.available_sections),
        manifest_hash: boundedVisibleText(payload.manifest_hash, 'manifest_hash', 128)
    };
    const reused = getBoolean(payload.reused, 'reused');
    if (reused !== undefined) {
        result.reused = reused;
    }
    return result;
}
export function sanitizeArtifactReadResult(raw, maxChars = DEFAULT_MAX_CHARS) {
    const payload = ensurePlainObject(raw, 'artifact result');
    if (payload.error != null) {
        return sanitizeCliError(payload);
    }
    assertHandshake(payload, 'CLI artifact result');
    const section = boundedVisibleText(payload.section, 'section', 16);
    if (!ARTIFACT_SECTIONS.has(section)) {
        throw new Error('CLI artifact result returned an unsupported section');
    }
    const normalizedMaxChars = normalizeMaxChars(maxChars);
    if (typeof payload.content !== 'string') {
        throw new Error('content must be a string');
    }
    const sanitizedContent = redactSensitiveText(payload.content);
    const marker = '\n…[truncated]';
    const wasCapped = sanitizedContent.length > normalizedMaxChars;
    const content = wasCapped ? `${sanitizedContent.slice(0, Math.max(0, normalizedMaxChars - marker.length))}${marker}` : sanitizedContent;
    const result = {
        schema_version: SCHEMA_VERSION,
        market: MARKET,
        artifact_id: validateIdentifier(payload.artifact_id, 'artifact_id'),
        section,
        content_type: boundedVisibleText(payload.content_type, 'content_type', 80),
        content,
        truncated: Boolean(getBoolean(payload.truncated, 'truncated') || wasCapped)
    };
    const relativePath = sanitizeRelativePath(payload.relative_path);
    if (relativePath !== undefined) {
        result.relative_path = relativePath;
    }
    return result;
}
function boundRendered(value) {
    if (value.length <= MAX_RENDER_CHARS) {
        return value;
    }
    const marker = '\n…[render truncated]';
    return `${value.slice(0, MAX_RENDER_CHARS - marker.length)}${marker}`;
}
function renderResearchRun(value) {
    if ('error' in value) {
        return boundRendered(`market: ${value.market}\nerror: ${value.error}\nmessage: ${value.message}`);
    }
    const lines = [
        `market: ${value.market}`,
        `workflow: ${value.workflow}`,
        `run_id: ${value.run_id}`,
        `artifact_id: ${value.artifact_id}`,
        `status: ${value.status}`,
        `decision_at: ${value.decision_at}`,
        `snapshot_id: ${value.snapshot_id}`,
        `data_mode: ${value.data_mode}`,
        `pit_quality: ${value.pit_quality}`,
        `counts: observe=${value.counts.observe}, continue_research=${value.counts.continue_research}, exclude=${value.counts.exclude}`
    ];
    if (value.focus.length) {
        lines.push('', 'focus:');
        for (const item of value.focus){
            lines.push(`- ${item.name}(${item.symbol}) ${item.decision ?? ''}`.trim());
        }
    }
    if (value.available_sections.length) {
        lines.push('', `sections: ${value.available_sections.join(', ')}`);
    }
    if (value.warnings.length) {
        lines.push('', 'warnings:', ...value.warnings.map((warning)=>`- ${warning}`));
    }
    if (value.gaps.length) {
        lines.push('', 'gaps:', ...value.gaps.map((gap)=>`- ${gap}`));
    }
    return boundRendered(lines.join('\n'));
}
function renderArtifactRead(value) {
    if ('error' in value) {
        return boundRendered(`market: ${value.market}\nerror: ${value.error}\nmessage: ${value.message}`);
    }
    return boundRendered([
        `market: ${value.market}`,
        `artifact_id: ${value.artifact_id}`,
        `section: ${value.section}`,
        `content_type: ${value.content_type}`,
        `truncated: ${String(value.truncated)}`,
        value.relative_path ? `relative_path: ${value.relative_path}` : '',
        '',
        value.content
    ].filter(Boolean).join('\n').trim());
}
export async function runResearchWorkflow(args, options = {}) {
    const normalized = normalizeRunArgs(args);
    const request = {
        schema_version: SCHEMA_VERSION,
        market: MARKET,
        workflow: normalized.workflow,
        decision_at: normalized.decision_at,
        snapshot: {
            selector: normalized.snapshot.selector,
            ...normalized.snapshot.snapshot_id ? {
                snapshot_id: normalized.snapshot.snapshot_id
            } : {}
        },
        top_n: normalized.top_n ?? DEFAULT_TOP_N
    };
    if (normalized.subject) request.subject = normalized.subject;
    if (normalized.symbol) request.symbol = normalized.symbol;
    const raw = await callResearchCli('run', request, options);
    return sanitizeResearchRunResult(raw);
}
export async function readArtifact(args, options = {}) {
    const normalized = normalizeArtifactReadArgs(args);
    const request = {
        artifact_id: normalized.artifact_id,
        section: normalized.section,
        max_chars: normalized.max_chars
    };
    const raw = await callResearchCli('artifact-read', request, options);
    return sanitizeArtifactReadResult(raw, normalized.max_chars);
}
const researchTool = defineTool({
    name: 'us_research_run',
    description: 'Run a real, research-only US equity workflow from a normalized snapshot. Use snapshot.selector=latest for the newest real snapshot or selector=id for a specific real snapshot. The run result is only a bounded summary: when the user requests the full research report, call us_artifact_read with the returned artifact_id and section=report, and never present the run summary as the full report.',
    parameters: {
        workflow: {
            type: 'string',
            required: true,
            enum: [
                'daily_report',
                'theme_research',
                'stock_research'
            ],
            description: 'Research workflow to execute.'
        },
        decision_at: {
            type: 'string',
            required: true,
            description: 'Timezone-aware ISO-8601 research cutoff, for example 2026-08-16T08:30:00-04:00.'
        },
        snapshot: {
            type: 'object',
            required: true,
            additionalProperties: false,
            properties: {
                selector: {
                    type: 'string',
                    required: true,
                    enum: [
                        'latest',
                        'id'
                    ],
                    description: 'Real snapshot selector. Use latest for the newest normalized snapshot or id for a specific normalized snapshot. Demo fixtures are intentionally unavailable to model-facing research.'
                },
                snapshot_id: {
                    type: 'string',
                    description: 'Canonical real snapshot ID; required only when selector=id.'
                }
            },
            description: 'Normalized real-data snapshot source. Use latest for routine current research and id for reproducible research against a known snapshot.'
        },
        subject: {
            type: 'string',
            description: 'Theme subject, required only for theme_research.'
        },
        symbol: {
            type: 'string',
            description: 'Uppercase US ticker, required only for stock_research.'
        },
        top_n: {
            type: 'integer',
            description: 'Candidate limit from 1 through 20.'
        }
    },
    output: {
        schema: {
            type: 'object',
            additionalProperties: true
        },
        render: (_args, value)=>[
                {
                    type: 'text',
                    text: renderResearchRun(value)
                }
            ]
    },
    async execute (args, exec) {
        return runResearchWorkflow(args, {
            signal: exec.signal
        });
    }
});
const artifactTool = defineTool({
    name: 'us_artifact_read',
    description: 'Read a bounded canonical US research artifact section by opaque artifact ID. When the user asks for a full or complete research report, section=report is required; summary is only a bounded preview and must never be presented as the full report.',
    parameters: {
        artifact_id: {
            type: 'string',
            required: true,
            description: 'Opaque artifact ID returned by us_research_run.'
        },
        section: {
            type: 'string',
            enum: [
                'summary',
                'report',
                'manifest',
                'packet'
            ],
            description: 'Predefined artifact section to read. Use report for the full Markdown research report; summary is only a compact machine-readable preview.'
        },
        max_chars: {
            type: 'integer',
            description: `Maximum visible content length, ${MIN_MAX_CHARS}-${MAX_MAX_CHARS}.`
        }
    },
    output: {
        schema: {
            type: 'object',
            additionalProperties: true
        },
        render: (_args, value)=>[
                {
                    type: 'text',
                    text: renderArtifactRead(value)
                }
            ]
    },
    async execute (args, exec) {
        return readArtifact(args, {
            signal: exec.signal
        });
    }
});
export function apply(ctx) {
    ctx.tools.register(researchTool);
    ctx.tools.register(artifactTool);
}
