import { spawn } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineTool } from '@deepseek-ai/dsh-tools';
export const name = 'cn-a-share-research-tools';
export const inject = [
    'tools'
];
const DEFAULT_TOP_N = 5;
const DEFAULT_MAX_CHARS = 12000;
const MIN_MAX_CHARS = 500;
const MAX_MAX_CHARS = 20000;
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
export function resolveProjectRoot(fromUrl = import.meta.url) {
    const currentDir = dirname(fileURLToPath(fromUrl));
    return process.env.A_SHARE_RESEARCH_ROOT ? resolve(process.env.A_SHARE_RESEARCH_ROOT) : resolve(currentDir, '..', '..');
}
export function resolveWorkspace(projectRoot) {
    return process.env.STOCK_RESEARCH_WORKSPACE ? resolve(process.env.STOCK_RESEARCH_WORKSPACE) : dirname(projectRoot);
}
export function resolvePythonBin(projectRoot) {
    if (process.env.A_SHARE_RESEARCH_PYTHON_BIN) {
        return process.env.A_SHARE_RESEARCH_PYTHON_BIN;
    }
    if (process.platform === 'win32') {
        return join(projectRoot, '.venv', 'Scripts', 'python.exe');
    }
    return join(projectRoot, '.venv', 'bin', 'python');
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
function getString(value, field) {
    if (typeof value === 'string') {
        return value.trim() || undefined;
    }
    if (value == null) {
        return undefined;
    }
    throw new Error(`${field} must be a non-empty string`);
}
function getInteger(value, field) {
    if (value == null) {
        return undefined;
    }
    if (typeof value !== 'number' || !Number.isInteger(value)) {
        throw new Error(`${field} must be an integer`);
    }
    return value;
}
function requireResultString(value, field) {
    const result = getString(value, field);
    if (result === undefined) throw new Error(`${field} is required in the CLI result`);
    return result;
}
function requireResultInteger(value, field) {
    const result = getInteger(value, field);
    if (result === undefined) throw new Error(`${field} is required in the CLI result`);
    return result;
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
function getStringArray(value) {
    if (value == null) {
        return undefined;
    }
    if (!Array.isArray(value)) {
        throw new Error('expected an array of strings');
    }
    return value.filter((item)=>typeof item === 'string' && item.trim().length > 0);
}
function validateIdentifier(value, field) {
    if (!/^[A-Za-z0-9._-]{1,128}$/.test(value)) {
        throw new Error(`${field} contains unsupported characters`);
    }
    return value;
}
function validateDecisionAt(value) {
    if (!/(Z|[+-]\d{2}:\d{2})$/i.test(value)) {
        throw new Error('decision_at must be an ISO-8601 datetime with a timezone offset');
    }
    const parsed = Date.parse(value);
    if (Number.isNaN(parsed)) {
        throw new Error('decision_at must be an ISO-8601 datetime with a timezone offset');
    }
    return value;
}
function clampTopN(value) {
    const topN = getInteger(value, 'top_n') ?? DEFAULT_TOP_N;
    if (topN < 1 || topN > 20) {
        throw new Error('top_n must be an integer between 1 and 20');
    }
    return topN;
}
function clampMaxChars(value) {
    const maxChars = getInteger(value, 'max_chars') ?? DEFAULT_MAX_CHARS;
    if (maxChars < MIN_MAX_CHARS || maxChars > MAX_MAX_CHARS) {
        throw new Error(`max_chars must be an integer between ${MIN_MAX_CHARS} and ${MAX_MAX_CHARS}`);
    }
    return maxChars;
}
function normalizeRunArgs(args) {
    const raw = ensurePlainObject(args, 'cn_research_run arguments');
    rejectUnknownFields(raw, [
        'workflow',
        'decision_at',
        'snapshot',
        'subject',
        'symbol',
        'top_n'
    ], 'cn_research_run arguments');
    const workflow = getString(raw.workflow, 'workflow');
    if (workflow !== 'daily_report' && workflow !== 'stock_research' && workflow !== 'theme_research') {
        throw new Error('workflow must be one of daily_report, stock_research, theme_research');
    }
    const decisionAt = validateDecisionAt(getString(raw.decision_at, 'decision_at') || '');
    const snapshotRaw = ensurePlainObject(raw.snapshot, 'snapshot');
    rejectUnknownFields(snapshotRaw, [
        'selector',
        'id'
    ], 'snapshot');
    const selector = getString(snapshotRaw.selector, 'snapshot.selector');
    if (selector !== 'demo' && selector !== 'latest' && selector !== 'id') {
        throw new Error('snapshot.selector must be one of demo, latest, id');
    }
    const snapshotId = getString(snapshotRaw.id, 'snapshot.id');
    if (selector === 'id' && !snapshotId) {
        throw new Error('snapshot.id is required when snapshot.selector=id');
    }
    if (selector !== 'id' && snapshotId) {
        throw new Error('snapshot.id is only allowed when snapshot.selector=id');
    }
    if (snapshotId) {
        validateIdentifier(snapshotId, 'snapshot.id');
    }
    const subject = getString(raw.subject, 'subject');
    const symbol = getString(raw.symbol, 'symbol');
    if (symbol && !/^\d{6}$/.test(symbol)) {
        throw new Error('symbol must be a 6-digit A-share code when provided');
    }
    if (workflow === 'daily_report' && (subject || symbol)) {
        throw new Error('daily_report does not accept subject or symbol');
    }
    if (workflow === 'theme_research') {
        if (!subject) {
            throw new Error('subject is required for theme_research');
        }
        if (symbol) {
            throw new Error('theme_research does not accept symbol');
        }
    }
    if (workflow === 'stock_research') {
        if (!symbol) {
            throw new Error('symbol is required for stock_research');
        }
        if (subject) {
            throw new Error('stock_research does not accept subject');
        }
    }
    return {
        workflow,
        decision_at: decisionAt,
        snapshot: {
            selector,
            ...snapshotId ? {
                id: snapshotId
            } : {}
        },
        ...subject ? {
            subject
        } : {},
        ...symbol ? {
            symbol
        } : {},
        top_n: clampTopN(raw.top_n)
    };
}
function normalizeArtifactReadArgs(args) {
    const raw = ensurePlainObject(args, 'cn_artifact_read arguments');
    rejectUnknownFields(raw, [
        'artifact_id',
        'section',
        'max_chars',
        'cursor'
    ], 'cn_artifact_read arguments');
    const artifactId = validateIdentifier(getString(raw.artifact_id, 'artifact_id') || '', 'artifact_id');
    const section = getString(raw.section, 'section') ?? 'summary';
    if (section !== 'summary' && section !== 'report' && section !== 'manifest' && section !== 'packet' && section !== 'facts') {
        throw new Error('section must be summary, report, manifest, packet, or facts');
    }
    const cursor = getInteger(raw.cursor, 'cursor') ?? 0;
    if (cursor < 0 || cursor > 10000000) {
        throw new Error('cursor must be an integer between 0 and 10000000');
    }
    return {
        artifact_id: artifactId,
        section,
        max_chars: clampMaxChars(raw.max_chars),
        cursor
    };
}
function normalizeOutcomeHistoryArgs(args) {
    const raw = ensurePlainObject(args, 'cn_outcome_history arguments');
    rejectUnknownFields(raw, [
        'evaluation_at',
        'limit'
    ], 'cn_outcome_history arguments');
    const evaluationAt = validateDecisionAt(raw.evaluation_at);
    const limit = getInteger(raw.limit, 'limit') ?? 10;
    if (limit < 1 || limit > 20) {
        throw new Error('limit must be an integer between 1 and 20');
    }
    return {
        evaluation_at: evaluationAt,
        limit
    };
}
function normalizeResearchHistoryArgs(args) {
    const raw = ensurePlainObject(args, 'cn_research_history arguments');
    rejectUnknownFields(raw, [
        'evaluation_at',
        'limit',
        'candidate_ids'
    ], 'cn_research_history arguments');
    const evaluationAt = validateDecisionAt(raw.evaluation_at);
    const limit = getInteger(raw.limit, 'limit') ?? 100;
    if (limit < 1 || limit > 500) {
        throw new Error('limit must be an integer between 1 and 500');
    }
    if (raw.candidate_ids != null && !Array.isArray(raw.candidate_ids)) {
        throw new Error('candidate_ids must be an array');
    }
    const candidateIds = raw.candidate_ids?.map((value, index)=>{
        const candidateId = getString(value, `candidate_ids[${String(index)}]`);
        if (!candidateId) throw new Error(`candidate_ids[${String(index)}] must not be empty`);
        return candidateId;
    });
    if (candidateIds !== undefined && (candidateIds.length > 50 || new Set(candidateIds).size !== candidateIds.length)) {
        throw new Error('candidate_ids must contain at most 50 unique strings');
    }
    for (const candidateId of candidateIds ?? []){
        if (candidateId.length > 256 || /[\u0000-\u001f\u007f]/.test(candidateId)) {
            throw new Error('candidate_ids contains an invalid candidate ID');
        }
    }
    return {
        evaluation_at: evaluationAt,
        limit,
        candidate_ids: candidateIds
    };
}
function sanitizeCliError(raw) {
    const schemaVersion = getString(raw.schema_version, 'schema_version') || '0.1';
    const error = getString(raw.error, 'error') || 'UnknownError';
    const message = sanitizeErrorMessage(getString(raw.message, 'message') || 'unknown CLI failure');
    return {
        schema_version: schemaVersion,
        market: 'CN',
        error,
        message
    };
}
function sanitizeErrorMessage(value) {
    return value.replace(/(?:\/Users|\/home|\/private|\/tmp|\/var|\/opt|\/Volumes)\/[^\s"'`]+/g, '[redacted-path]').replace(/[A-Za-z]:\\[^\s"'`]+/g, '[redacted-path]').replace(/\s+/g, ' ').slice(0, 500);
}
function sanitizeFocus(value) {
    if (!Array.isArray(value)) {
        return undefined;
    }
    return value.flatMap((item)=>{
        if (!item || typeof item !== 'object' || Array.isArray(item)) {
            return [];
        }
        const record = item;
        const symbol = getString(record.symbol, 'focus.symbol');
        const name = getString(record.name, 'focus.name');
        if (!symbol || !name) {
            return [];
        }
        return [
            omitUndefinedProperties({
                symbol,
                name,
                theme: getString(record.theme, 'focus.theme'),
                decision: getString(record.decision, 'focus.decision'),
                reason: getString(record.reason, 'focus.reason'),
                attention_score: getInteger(record.attention_score, 'focus.attention_score'),
                attention_bucket: getString(record.attention_bucket, 'focus.attention_bucket'),
                opportunity_view: getString(record.opportunity_view, 'focus.opportunity_view'),
                new_information: getString(record.new_information, 'focus.new_information'),
                economic_impact: getString(record.economic_impact, 'focus.economic_impact'),
                expectation_gap: getString(record.expectation_gap, 'focus.expectation_gap'),
                market_pricing: getString(record.market_pricing, 'focus.market_pricing')
            })
        ];
    });
}
function sanitizeCounts(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        return undefined;
    }
    const counts = value;
    const sanitized = omitUndefinedProperties({
        observe: getInteger(counts.observe, 'counts.observe'),
        continue_research: getInteger(counts.continue_research, 'counts.continue_research'),
        exclude: getInteger(counts.exclude, 'counts.exclude')
    });
    return Object.keys(sanitized).length ? sanitized : undefined;
}
function omitUndefinedProperties(value) {
    return Object.fromEntries(Object.entries(value).filter(([, entryValue])=>entryValue !== undefined));
}
function sanitizeRelativePath(value) {
    const relativePath = getString(value, 'relative_path');
    if (!relativePath) {
        return undefined;
    }
    if (relativePath.startsWith('/') || relativePath.split('/').some((part)=>part === '..' || part === '.')) {
        return undefined;
    }
    return relativePath;
}
function requireSafeArtifactContent(value, maxChars) {
    if (typeof value !== 'string') {
        throw new Error('artifact content must be a string');
    }
    if (value.length > maxChars) {
        throw new Error('CLI artifact content exceeded the requested max_chars contract');
    }
    const containsSecret = /\bBearer\s+[A-Za-z0-9._~+/=-]+/i.test(value) || /\b(api[_-]?key|access[_-]?token|authorization|password|secret|token)\b\s*[:=]\s*[^\s,;]+/i.test(value) || /\bsk-[A-Za-z0-9_-]{8,}\b/.test(value);
    const containsAbsolutePath = /(?:^|[\s("'`])\/(?:Users|home|private|tmp|var|opt|Volumes|etc)\/[^\s"'`<>()]+/m.test(value) || /[A-Za-z]:\\[^\s"'`<>()]+/.test(value);
    if (containsSecret || containsAbsolutePath) {
        throw new Error('canonical artifact content violated the model-visible safety boundary');
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
async function collectProcessOutput(child) {
    let stdout = '';
    let stderr = '';
    if (!child.stdout || !child.stderr) {
        throw new Error('CLI process did not expose stdout/stderr pipes');
    }
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk)=>{
        stdout += chunk;
    });
    child.stderr.on('data', (chunk)=>{
        stderr += chunk;
    });
    const exitCode = await new Promise((resolvePromise, rejectPromise)=>{
        child.once('error', rejectPromise);
        child.once('close', resolvePromise);
    });
    return {
        stdout,
        stderr,
        exitCode
    };
}
function childEnvironment() {
    const env = {
        PYTHONUNBUFFERED: '1'
    };
    for (const key of CHILD_ENV_ALLOWLIST){
        const value = process.env[key];
        if (value != null) {
            env[key] = value;
        }
    }
    return env;
}
export async function callResearchCli(command, request, options = {}) {
    const projectRoot = options.projectRoot ?? resolveProjectRoot();
    const workspace = options.workspace ?? resolveWorkspace(projectRoot);
    const pythonBin = options.pythonBin ?? resolvePythonBin(projectRoot);
    const env = childEnvironment();
    const child = spawn(pythonBin, [
        '-m',
        'a_share_research.cli',
        '--workspace',
        workspace,
        command,
        '--request-json',
        '-'
    ], {
        cwd: projectRoot,
        env,
        stdio: [
            'pipe',
            'pipe',
            'pipe'
        ],
        signal: options.signal
    });
    child.stdin.end(`${JSON.stringify(request)}\n`);
    const { stdout, stderr, exitCode } = await collectProcessOutput(child);
    const stdoutValue = maybeParseJson(stdout);
    const stderrValue = maybeParseJson(stderr);
    if (exitCode !== 0) {
        if (stderrValue && typeof stderrValue === 'object' && !Array.isArray(stderrValue)) {
            const cliError = sanitizeCliError(stderrValue);
            if (DOMAIN_CLI_ERRORS.has(cliError.error)) {
                return cliError;
            }
            throw new Error(`a_share_research CLI infrastructure failure: ${cliError.error}`);
        }
        throw new Error(`a_share_research CLI failed for ${command} with non-canonical error output (exit ${String(exitCode)})`);
    }
    if (stdoutValue !== undefined) {
        return stdoutValue;
    }
    throw new Error(`a_share_research CLI returned invalid JSON for ${command}`);
}
function sanitizeBoundedJson(value, depth = 0) {
    if (depth > 6 || value == null || typeof value === 'boolean') return value;
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value === 'string') return sanitizeErrorMessage(value).slice(0, 2000);
    if (Array.isArray(value)) return value.slice(0, 200).map((item)=>sanitizeBoundedJson(item, depth + 1));
    if (typeof value === 'object') {
        const entries = Object.entries(value).filter(([key])=>!/(?:secret|token|password|credential|api[_-]?key)/i.test(key)).slice(0, 100).map(([key, item])=>[
                key,
                sanitizeBoundedJson(item, depth + 1)
            ]);
        return Object.fromEntries(entries);
    }
    return undefined;
}
export function sanitizeOutcomeHistoryResult(raw) {
    const payload = ensurePlainObject(raw, 'outcome history result');
    if (payload.error != null) return sanitizeCliError(payload);
    if (payload.market !== 'CN' || payload.schema_version !== '0.1') {
        throw new Error('CLI outcome history handshake failed; expected CN schema 0.1');
    }
    return sanitizeBoundedJson(payload);
}
export function sanitizeResearchHistoryResult(raw) {
    const payload = ensurePlainObject(raw, 'research history result');
    if (payload.error != null) return sanitizeCliError(payload);
    if (payload.market !== 'CN' || payload.schema_version !== '0.1') {
        throw new Error('CLI research history handshake failed; expected CN schema 0.1');
    }
    if (!Array.isArray(payload.candidates)) {
        throw new Error('CLI research history candidates must be an array');
    }
    const candidates = payload.candidates.slice(0, 50).map((rawCandidate, index)=>{
        const candidate = ensurePlainObject(rawCandidate, `research history candidates[${String(index)}]`);
        const latestDecision = requireResultString(candidate.latest_decision, 'latest_decision');
        if (latestDecision !== 'exclude' && latestDecision !== 'continue_research' && latestDecision !== 'observe') {
            throw new Error('CLI research history returned an unsupported latest_decision');
        }
        const agingAction = requireResultString(candidate.aging_action, 'aging_action');
        if (![
            'active',
            'deprioritize',
            'close_review',
            'closed'
        ].includes(agingAction)) {
            throw new Error('CLI research history returned an unsupported aging_action');
        }
        return {
            candidate_id: requireResultString(candidate.candidate_id, 'candidate_id'),
            symbol: requireResultString(candidate.symbol, 'symbol'),
            latest_decision: latestDecision,
            run_count: requireResultInteger(candidate.run_count, 'run_count'),
            state_or_gap_transition_count: requireResultInteger(candidate.state_or_gap_transition_count, 'state_or_gap_transition_count'),
            consecutive_continue_research: requireResultInteger(candidate.consecutive_continue_research, 'consecutive_continue_research'),
            last_progress_at: requireResultString(candidate.last_progress_at, 'last_progress_at'),
            stale_days: requireResultInteger(candidate.stale_days, 'stale_days'),
            aging_action: agingAction
        };
    });
    return omitUndefinedProperties({
        schema_version: '0.1',
        market: 'CN',
        evaluation_at: requireResultString(payload.evaluation_at, 'evaluation_at'),
        candidate_filter_count: payload.candidate_filter_count === null ? null : requireResultInteger(payload.candidate_filter_count, 'candidate_filter_count'),
        candidate_count: requireResultInteger(payload.candidate_count, 'candidate_count'),
        returned_count: requireResultInteger(payload.returned_count, 'returned_count'),
        aging_counts: sanitizeBoundedJson(payload.aging_counts),
        candidates,
        policy: sanitizeBoundedJson(payload.policy)
    });
}
export function sanitizeResearchRunResult(raw) {
    const payload = ensurePlainObject(raw, 'run result');
    if (payload.error != null) {
        return sanitizeCliError(payload);
    }
    if (payload.market !== 'CN') {
        throw new Error('CLI run result market handshake failed; expected CN');
    }
    if (payload.schema_version !== '0.1') {
        throw new Error('CLI run result schema handshake failed; expected 0.1');
    }
    const workflow = getString(payload.workflow, 'workflow');
    if (workflow !== 'daily_report' && workflow !== 'stock_research' && workflow !== 'theme_research') {
        throw new Error('CLI run result returned an unsupported workflow');
    }
    const schemaVersion = getString(payload.schema_version, 'schema_version') || '0.1';
    const runId = getString(payload.run_id, 'run_id') || '';
    const artifactId = getString(payload.artifact_id, 'artifact_id') || '';
    const status = getString(payload.status, 'status') || '';
    const writerMode = getString(payload.writer_mode, 'writer_mode') || '';
    const methodId = getString(payload.method_id, 'method_id');
    if (!methodId) {
        throw new Error('CLI run result returned no method_id');
    }
    const dataMode = getString(payload.data_mode, 'data_mode') || '';
    const pitQuality = getString(payload.pit_quality, 'pit_quality') || '';
    const decisionAt = getString(payload.decision_at, 'decision_at') || '';
    const snapshotId = getString(payload.snapshot_id, 'snapshot_id') || '';
    const analysisHash = getString(payload.analysis_hash, 'analysis_hash') || '';
    return omitUndefinedProperties({
        schema_version: schemaVersion,
        market: 'CN',
        run_id: runId,
        artifact_id: artifactId,
        status,
        writer_mode: writerMode,
        method_id: methodId,
        data_mode: dataMode,
        pit_quality: pitQuality,
        workflow,
        decision_at: decisionAt,
        snapshot_id: snapshotId,
        analysis_hash: analysisHash,
        counts: sanitizeCounts(payload.counts),
        focus: sanitizeFocus(payload.focus),
        warnings: getStringArray(payload.warnings),
        gaps: getStringArray(payload.gaps),
        available_sections: getStringArray(payload.available_sections),
        manifest_hash: getString(payload.manifest_hash, 'manifest_hash'),
        reused: getBoolean(payload.reused, 'reused')
    });
}
export function sanitizeArtifactReadResult(raw, maxChars = DEFAULT_MAX_CHARS) {
    const payload = ensurePlainObject(raw, 'artifact result');
    if (payload.error != null) {
        return sanitizeCliError(payload);
    }
    if (payload.market !== 'CN') {
        throw new Error('CLI artifact result market handshake failed; expected CN');
    }
    if (payload.schema_version !== '0.1') {
        throw new Error('CLI artifact result schema handshake failed; expected 0.1');
    }
    const section = getString(payload.section, 'section');
    if (section !== 'summary' && section !== 'report' && section !== 'manifest' && section !== 'packet' && section !== 'facts') {
        throw new Error('CLI artifact result returned an unsupported section');
    }
    const content = requireSafeArtifactContent(payload.content, maxChars);
    return omitUndefinedProperties({
        schema_version: getString(payload.schema_version, 'schema_version') || '0.1',
        market: 'CN',
        artifact_id: getString(payload.artifact_id, 'artifact_id') || '',
        section,
        content_type: getString(payload.content_type, 'content_type') || 'text/plain',
        content,
        truncated: Boolean(getBoolean(payload.truncated, 'truncated')),
        cursor: getInteger(payload.cursor, 'cursor'),
        next_cursor: payload.next_cursor === null ? null : getInteger(payload.next_cursor, 'next_cursor'),
        total_chars: getInteger(payload.total_chars, 'total_chars'),
        content_sha256: getString(payload.content_sha256, 'content_sha256'),
        relative_path: sanitizeRelativePath(payload.relative_path)
    });
}
function renderResearchRun(value) {
    if ('error' in value) {
        return `market: ${value.market}\nerror: ${value.error}\nmessage: ${value.message}`;
    }
    const lines = [
        `market: ${value.market}`,
        `workflow: ${value.workflow}`,
        `run_id: ${value.run_id}`,
        `artifact_id: ${value.artifact_id}`,
        `method_id: ${value.method_id ?? 'UNKNOWN'}`,
        `status: ${value.status}`,
        `decision_at: ${value.decision_at}`,
        `snapshot_id: ${value.snapshot_id}`,
        `data_mode: ${value.data_mode}`,
        `pit_quality: ${value.pit_quality}`
    ];
    if (value.counts) {
        lines.push(`counts: observe=${value.counts.observe ?? 0}, continue_research=${value.counts.continue_research ?? 0}, exclude=${value.counts.exclude ?? 0}`);
    }
    if (value.focus?.length) {
        lines.push('', 'focus:');
        for (const item of value.focus.slice(0, 5)){
            lines.push(`- ${item.name}(${item.symbol}) ${item.decision ?? ''}`.trim());
            lines.push(`  attention=${String(item.attention_score ?? 'UNKNOWN')} ` + `bucket=${item.attention_bucket ?? 'UNKNOWN'} view=${item.opportunity_view ?? 'UNKNOWN'} ` + `new=${item.new_information ?? 'UNKNOWN'} impact=${item.economic_impact ?? 'UNKNOWN'} ` + `gap=${item.expectation_gap ?? 'UNKNOWN'} pricing=${item.market_pricing ?? 'UNKNOWN'}`);
        }
    }
    if (value.available_sections?.length) {
        lines.push('', `sections: ${value.available_sections.join(', ')}`);
    }
    if (value.warnings?.length) {
        lines.push('', 'warnings:');
        lines.push(...value.warnings.slice(0, 5).map((warning)=>`- ${warning}`));
    }
    if (value.gaps?.length) {
        lines.push('', 'gaps:');
        lines.push(...value.gaps.slice(0, 5).map((gap)=>`- ${gap}`));
    }
    return lines.join('\n');
}
function renderArtifactRead(value) {
    if ('error' in value) {
        return `market: ${value.market}\nerror: ${value.error}\nmessage: ${value.message}`;
    }
    return [
        `artifact_id: ${value.artifact_id}`,
        `section: ${value.section}`,
        `content_type: ${value.content_type}`,
        `truncated: ${String(value.truncated)}`,
        value.next_cursor != null ? `next_cursor: ${String(value.next_cursor)}` : '',
        value.total_chars != null ? `total_chars: ${String(value.total_chars)}` : '',
        value.content_sha256 ? `content_sha256: ${value.content_sha256}` : '',
        value.relative_path ? `relative_path: ${value.relative_path}` : '',
        '',
        value.content
    ].filter(Boolean).join('\n').trim();
}
export async function runResearchWorkflow(args, options = {}) {
    const normalized = normalizeRunArgs(args);
    const request = {
        schema_version: '0.1',
        market: 'CN',
        workflow: normalized.workflow,
        decision_at: normalized.decision_at,
        snapshot: {
            selector: normalized.snapshot.selector,
            ...normalized.snapshot.id ? {
                snapshot_id: normalized.snapshot.id
            } : {}
        },
        top_n: normalized.top_n ?? DEFAULT_TOP_N
    };
    if (normalized.subject) {
        request.subject = normalized.subject;
    }
    if (normalized.symbol) {
        request.symbol = normalized.symbol;
    }
    const raw = await callResearchCli('run', request, options);
    return sanitizeResearchRunResult(raw);
}
export async function readArtifact(args, options = {}) {
    const normalized = normalizeArtifactReadArgs(args);
    const request = {
        artifact_id: normalized.artifact_id,
        section: normalized.section,
        max_chars: normalized.max_chars,
        cursor: normalized.cursor
    };
    const raw = await callResearchCli('artifact-read', request, options);
    return sanitizeArtifactReadResult(raw, normalized.max_chars);
}
export async function readOutcomeHistory(args, options = {}) {
    const normalized = normalizeOutcomeHistoryArgs(args);
    const raw = await callResearchCli('outcome-history', {
        schema_version: '0.1',
        market: 'CN',
        evaluation_at: normalized.evaluation_at,
        limit: normalized.limit
    }, options);
    return sanitizeOutcomeHistoryResult(raw);
}
export async function readResearchHistory(args, options = {}) {
    const normalized = normalizeResearchHistoryArgs(args);
    const request = {
        schema_version: '0.1',
        market: 'CN',
        evaluation_at: normalized.evaluation_at,
        limit: normalized.limit
    };
    if (normalized.candidate_ids !== undefined) {
        request.candidate_ids = normalized.candidate_ids;
    }
    const raw = await callResearchCli('research-history', request, options);
    return sanitizeResearchHistoryResult(raw);
}
function registerResearchTool(ctx) {
    return ctx.tools.register(defineTool({
        name: 'cn_research_run',
        description: 'Run a real CN A-share research workflow from a normalized snapshot. Use snapshot.selector=latest for the newest real snapshot or selector=id for a specific real snapshot. The run result is only a bounded summary: when the user requests the full research report, call cn_artifact_read with the returned artifact_id and section=report, and never present the run summary as the full report.',
        parameters: {
            workflow: {
                type: 'string',
                required: true,
                enum: [
                    'daily_report',
                    'stock_research',
                    'theme_research'
                ],
                description: 'Research workflow to execute.'
            },
            decision_at: {
                type: 'string',
                required: true,
                description: 'Timezone-aware ISO-8601 research cutoff, for example 2026-08-16T08:30:00+08:00.'
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
                    id: {
                        type: 'string',
                        description: 'Canonical real snapshot id; required only when selector=id.'
                    }
                },
                description: 'Normalized real-data snapshot source. Use latest for routine current research and id for reproducible research against a known snapshot.'
            },
            subject: {
                type: 'string',
                description: 'Theme or subject. Required for theme_research.'
            },
            symbol: {
                type: 'string',
                description: '6-digit A-share code. Required for stock_research.'
            },
            top_n: {
                type: 'integer',
                description: 'Candidate limit, 1-20.'
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
    }));
}
function registerArtifactTool(ctx) {
    return ctx.tools.register(defineTool({
        name: 'cn_artifact_read',
        description: 'Read one lossless page of a canonical CN research artifact section by artifact_id. When the user asks for a full or complete research report, section=report is required and every non-null next_cursor must be followed while content_sha256 remains stable; summary is only a bounded preview and must never be presented as the full report.',
        parameters: {
            artifact_id: {
                type: 'string',
                required: true,
                description: 'Canonical artifact id returned by cn_research_run.'
            },
            section: {
                type: 'string',
                enum: [
                    'summary',
                    'report',
                    'manifest',
                    'packet',
                    'facts'
                ],
                description: 'Artifact section to read. Use report for the full Markdown research report, facts for the thesis-blind fact packet, and summary only for a compact preview.'
            },
            max_chars: {
                type: 'integer',
                description: `Maximum content length to request from the CLI, ${MIN_MAX_CHARS}-${MAX_MAX_CHARS}.`
            },
            cursor: {
                type: 'integer',
                description: 'Zero-based character cursor returned as next_cursor by the prior page.'
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
    }));
}
function registerOutcomeHistoryTool(ctx) {
    return ctx.tools.register(defineTool({
        name: 'cn_outcome_history',
        description: 'Read immutable T+5/T+20 research-state scorecards available by evaluation_at. This is calibration memory, never a trading signal.',
        parameters: {
            evaluation_at: {
                type: 'string',
                required: true,
                description: 'Timezone-aware availability cutoff for historical outcome memory.'
            },
            limit: {
                type: 'integer',
                description: 'Maximum prior runs, 1-20.'
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
                        text: JSON.stringify(value, null, 2).slice(0, 22000)
                    }
                ]
        },
        async execute (args, exec) {
            return readOutcomeHistory(args, {
                signal: exec.signal
            });
        }
    }));
}
function registerResearchHistoryTool(ctx) {
    return ctx.tools.register(defineTool({
        name: 'cn_research_history',
        description: 'Read candidate aging, consecutive continue_research counts and days since the last state-or-gap change. Use it to allocate research attention; it never rewrites canonical decisions.',
        parameters: {
            evaluation_at: {
                type: 'string',
                required: true,
                description: 'Timezone-aware cutoff for research-history state.'
            },
            limit: {
                type: 'integer',
                description: 'Maximum candidate histories, 1-500.'
            },
            candidate_ids: {
                type: 'array',
                items: {
                    type: 'string'
                },
                description: 'Optional exact canonical candidate IDs, at most 50.'
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
                        text: JSON.stringify(value, null, 2).slice(0, 30000)
                    }
                ]
        },
        async execute (args, exec) {
            return readResearchHistory(args, {
                signal: exec.signal
            });
        }
    }));
}
export function apply(ctx) {
    registerResearchTool(ctx);
    registerArtifactTool(ctx);
    registerOutcomeHistoryTool(ctx);
    registerResearchHistoryTool(ctx);
}
