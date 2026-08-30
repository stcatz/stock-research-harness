import { spawn } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cn-a-share-research-tools'
export const inject = ['tools']

export type ResearchWorkflow = 'daily_report' | 'stock_research' | 'theme_research'
type SnapshotSelector = 'demo' | 'latest' | 'id'
type ArtifactSection = 'summary' | 'report' | 'manifest' | 'packet' | 'facts'
type JsonObject = Record<string, unknown>

export type Context = {
  tools: {
    register: (tool: unknown) => unknown
  }
}

export type ResearchRunArgs = {
  workflow: ResearchWorkflow
  decision_at: string
  snapshot: {
    selector: SnapshotSelector
    id?: string
  }
  subject?: string
  symbol?: string
  top_n?: number
}

export type ArtifactReadArgs = {
  artifact_id: string
  section?: ArtifactSection
  max_chars?: number
  cursor?: number
}

export type OutcomeHistoryArgs = {
  evaluation_at: string
  limit?: number
}

type CliBridgeOptions = {
  projectRoot?: string
  workspace?: string
  pythonBin?: string
  signal?: AbortSignal
}

type RunSuccessResult = {
  schema_version: string
  market: 'CN'
  run_id: string
  artifact_id: string
  status: string
  writer_mode: string
  data_mode: string
  pit_quality: string
  workflow: ResearchWorkflow
  decision_at: string
  snapshot_id: string
  analysis_hash: string
  counts?: {
    observe?: number
    continue_research?: number
    exclude?: number
  }
  focus?: Array<{
    symbol: string
    name: string
    theme?: string
    decision?: string
    reason?: string
  }>
  warnings?: string[]
  gaps?: string[]
  available_sections?: string[]
  manifest_hash?: string
  reused?: boolean
}

type ArtifactReadSuccessResult = {
  schema_version: string
  market: 'CN'
  artifact_id: string
  section: ArtifactSection
  content_type: string
  content: string
  truncated: boolean
  cursor?: number
  next_cursor?: number | null
  total_chars?: number
  content_sha256?: string
  relative_path?: string
}

type CliErrorResult = {
  schema_version: string
  market: 'CN'
  error: string
  message: string
}

export type ResearchRunResult = RunSuccessResult | CliErrorResult
export type ArtifactReadResult = ArtifactReadSuccessResult | CliErrorResult
export type OutcomeHistoryResult = JsonObject | CliErrorResult

const DEFAULT_TOP_N = 5
const DEFAULT_MAX_CHARS = 12000
const MIN_MAX_CHARS = 500
const MAX_MAX_CHARS = 20000
const DOMAIN_CLI_ERRORS = new Set(['ContractError', 'JSONDecodeError', 'KeyError', 'ValueError'])
const CHILD_ENV_ALLOWLIST = [
  'HOME',
  'LANG',
  'LC_ALL',
  'LC_CTYPE',
  'PATH',
  'SYSTEMROOT',
  'TMPDIR',
  'TEMP',
  'TMP',
] as const

export function resolveProjectRoot(fromUrl: string = import.meta.url): string {
  const currentDir = dirname(fileURLToPath(fromUrl))
  return process.env.A_SHARE_RESEARCH_ROOT
    ? resolve(process.env.A_SHARE_RESEARCH_ROOT)
    : resolve(currentDir, '..', '..')
}

export function resolveWorkspace(projectRoot: string): string {
  return process.env.STOCK_RESEARCH_WORKSPACE
    ? resolve(process.env.STOCK_RESEARCH_WORKSPACE)
    : dirname(projectRoot)
}

export function resolvePythonBin(projectRoot: string): string {
  if (process.env.A_SHARE_RESEARCH_PYTHON_BIN) {
    return process.env.A_SHARE_RESEARCH_PYTHON_BIN
  }
  if (process.platform === 'win32') {
    return join(projectRoot, '.venv', 'Scripts', 'python.exe')
  }
  return join(projectRoot, '.venv', 'bin', 'python')
}

function ensurePlainObject(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be an object`)
  }
  return value as JsonObject
}

function rejectUnknownFields(value: JsonObject, allowed: readonly string[], label: string): void {
  const allowedSet = new Set(allowed)
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key)).sort()
  if (unknown.length) {
    throw new Error(`${label} contains unsupported fields: ${unknown.join(', ')}`)
  }
}

function getString(value: unknown, field: string): string | undefined {
  if (typeof value === 'string') {
    return value.trim() || undefined
  }
  if (value == null) {
    return undefined
  }
  throw new Error(`${field} must be a non-empty string`)
}

function getInteger(value: unknown, field: string): number | undefined {
  if (value == null) {
    return undefined
  }
  if (typeof value !== 'number' || !Number.isInteger(value)) {
    throw new Error(`${field} must be an integer`)
  }
  return value
}

function getBoolean(value: unknown, field: string): boolean | undefined {
  if (value == null) {
    return undefined
  }
  if (typeof value !== 'boolean') {
    throw new Error(`${field} must be a boolean`)
  }
  return value
}

function getStringArray(value: unknown): string[] | undefined {
  if (value == null) {
    return undefined
  }
  if (!Array.isArray(value)) {
    throw new Error('expected an array of strings')
  }
  return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
}

function validateIdentifier(value: string, field: string): string {
  if (!/^[A-Za-z0-9._-]{1,128}$/.test(value)) {
    throw new Error(`${field} contains unsupported characters`)
  }
  return value
}

function validateDecisionAt(value: string): string {
  if (!/(Z|[+-]\d{2}:\d{2})$/i.test(value)) {
    throw new Error('decision_at must be an ISO-8601 datetime with a timezone offset')
  }
  const parsed = Date.parse(value)
  if (Number.isNaN(parsed)) {
    throw new Error('decision_at must be an ISO-8601 datetime with a timezone offset')
  }
  return value
}

function clampTopN(value: unknown): number {
  const topN = getInteger(value, 'top_n') ?? DEFAULT_TOP_N
  if (topN < 1 || topN > 20) {
    throw new Error('top_n must be an integer between 1 and 20')
  }
  return topN
}

function clampMaxChars(value: unknown): number {
  const maxChars = getInteger(value, 'max_chars') ?? DEFAULT_MAX_CHARS
  if (maxChars < MIN_MAX_CHARS || maxChars > MAX_MAX_CHARS) {
    throw new Error(`max_chars must be an integer between ${MIN_MAX_CHARS} and ${MAX_MAX_CHARS}`)
  }
  return maxChars
}

function normalizeRunArgs(args: unknown): ResearchRunArgs {
  const raw = ensurePlainObject(args, 'cn_research_run arguments')
  rejectUnknownFields(
    raw,
    ['workflow', 'decision_at', 'snapshot', 'subject', 'symbol', 'top_n'],
    'cn_research_run arguments',
  )
  const workflow = getString(raw.workflow, 'workflow')
  if (workflow !== 'daily_report' && workflow !== 'stock_research' && workflow !== 'theme_research') {
    throw new Error('workflow must be one of daily_report, stock_research, theme_research')
  }

  const decisionAt = validateDecisionAt(getString(raw.decision_at, 'decision_at') || '')
  const snapshotRaw = ensurePlainObject(raw.snapshot, 'snapshot')
  rejectUnknownFields(snapshotRaw, ['selector', 'id'], 'snapshot')
  const selector = getString(snapshotRaw.selector, 'snapshot.selector')
  if (selector !== 'demo' && selector !== 'latest' && selector !== 'id') {
    throw new Error('snapshot.selector must be one of demo, latest, id')
  }
  const snapshotId = getString(snapshotRaw.id, 'snapshot.id')
  if (selector === 'id' && !snapshotId) {
    throw new Error('snapshot.id is required when snapshot.selector=id')
  }
  if (selector !== 'id' && snapshotId) {
    throw new Error('snapshot.id is only allowed when snapshot.selector=id')
  }
  if (snapshotId) {
    validateIdentifier(snapshotId, 'snapshot.id')
  }

  const subject = getString(raw.subject, 'subject')
  const symbol = getString(raw.symbol, 'symbol')
  if (symbol && !/^\d{6}$/.test(symbol)) {
    throw new Error('symbol must be a 6-digit A-share code when provided')
  }
  if (workflow === 'daily_report' && (subject || symbol)) {
    throw new Error('daily_report does not accept subject or symbol')
  }
  if (workflow === 'theme_research') {
    if (!subject) {
      throw new Error('subject is required for theme_research')
    }
    if (symbol) {
      throw new Error('theme_research does not accept symbol')
    }
  }
  if (workflow === 'stock_research') {
    if (!symbol) {
      throw new Error('symbol is required for stock_research')
    }
    if (subject) {
      throw new Error('stock_research does not accept subject')
    }
  }

  return {
    workflow,
    decision_at: decisionAt,
    snapshot: {
      selector,
      ...(snapshotId ? { id: snapshotId } : {}),
    },
    ...(subject ? { subject } : {}),
    ...(symbol ? { symbol } : {}),
    top_n: clampTopN(raw.top_n),
  }
}

function normalizeArtifactReadArgs(args: unknown): Required<ArtifactReadArgs> {
  const raw = ensurePlainObject(args, 'cn_artifact_read arguments')
  rejectUnknownFields(
    raw,
    ['artifact_id', 'section', 'max_chars', 'cursor'],
    'cn_artifact_read arguments',
  )
  const artifactId = validateIdentifier(getString(raw.artifact_id, 'artifact_id') || '', 'artifact_id')
  const section = getString(raw.section, 'section') ?? 'summary'
  if (section !== 'summary' && section !== 'report' && section !== 'manifest' && section !== 'packet' && section !== 'facts') {
    throw new Error('section must be summary, report, manifest, packet, or facts')
  }
  const cursor = getInteger(raw.cursor, 'cursor') ?? 0
  if (cursor < 0 || cursor > 10000000) {
    throw new Error('cursor must be an integer between 0 and 10000000')
  }
  return {
    artifact_id: artifactId,
    section,
    max_chars: clampMaxChars(raw.max_chars),
    cursor,
  }
}

function normalizeOutcomeHistoryArgs(args: unknown): Required<OutcomeHistoryArgs> {
  const raw = ensurePlainObject(args, 'cn_outcome_history arguments')
  rejectUnknownFields(raw, ['evaluation_at', 'limit'], 'cn_outcome_history arguments')
  const evaluationAt = validateDecisionAt(raw.evaluation_at)
  const limit = getInteger(raw.limit, 'limit') ?? 10
  if (limit < 1 || limit > 20) {
    throw new Error('limit must be an integer between 1 and 20')
  }
  return { evaluation_at: evaluationAt, limit }
}

function sanitizeCliError(raw: JsonObject): CliErrorResult {
  const schemaVersion = getString(raw.schema_version, 'schema_version') || '0.1'
  const error = getString(raw.error, 'error') || 'UnknownError'
  const message = sanitizeErrorMessage(getString(raw.message, 'message') || 'unknown CLI failure')
  return {
    schema_version: schemaVersion,
    market: 'CN',
    error,
    message,
  }
}

function sanitizeErrorMessage(value: string): string {
  return value
    .replace(/(?:\/Users|\/home|\/private|\/tmp|\/var|\/opt|\/Volumes)\/[^\s"'`]+/g, '[redacted-path]')
    .replace(/[A-Za-z]:\\[^\s"'`]+/g, '[redacted-path]')
    .replace(/\s+/g, ' ')
    .slice(0, 500)
}

function sanitizeFocus(value: unknown): RunSuccessResult['focus'] {
  if (!Array.isArray(value)) {
    return undefined
  }
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      return []
    }
    const record = item as JsonObject
    const symbol = getString(record.symbol, 'focus.symbol')
    const name = getString(record.name, 'focus.name')
    if (!symbol || !name) {
      return []
    }
    return [omitUndefinedProperties({
      symbol,
      name,
      theme: getString(record.theme, 'focus.theme'),
      decision: getString(record.decision, 'focus.decision'),
      reason: getString(record.reason, 'focus.reason'),
    })]
  })
}

function sanitizeCounts(value: unknown): RunSuccessResult['counts'] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return undefined
  }
  const counts = value as JsonObject
  const sanitized = omitUndefinedProperties({
    observe: getInteger(counts.observe, 'counts.observe'),
    continue_research: getInteger(counts.continue_research, 'counts.continue_research'),
    exclude: getInteger(counts.exclude, 'counts.exclude'),
  })
  return Object.keys(sanitized).length ? sanitized : undefined
}

function omitUndefinedProperties<T extends Record<string, unknown>>(value: T): T {
  return Object.fromEntries(
    Object.entries(value).filter(([, entryValue]) => entryValue !== undefined),
  ) as T
}

function sanitizeRelativePath(value: unknown): string | undefined {
  const relativePath = getString(value, 'relative_path')
  if (!relativePath) {
    return undefined
  }
  if (relativePath.startsWith('/') || relativePath.split('/').some((part) => part === '..' || part === '.')) {
    return undefined
  }
  return relativePath
}

function requireSafeArtifactContent(value: unknown, maxChars: number): string {
  if (typeof value !== 'string') {
    throw new Error('artifact content must be a string')
  }
  if (value.length > maxChars) {
    throw new Error('CLI artifact content exceeded the requested max_chars contract')
  }
  const containsSecret = (
    /\bBearer\s+[A-Za-z0-9._~+/=-]+/i.test(value)
    || /\b(api[_-]?key|access[_-]?token|authorization|password|secret|token)\b\s*[:=]\s*[^\s,;]+/i.test(value)
    || /\bsk-[A-Za-z0-9_-]{8,}\b/.test(value)
  )
  const containsAbsolutePath = (
    /(?:^|[\s("'`])\/(?:Users|home|private|tmp|var|opt|Volumes|etc)\/[^\s"'`<>()]+/m.test(value)
    || /[A-Za-z]:\\[^\s"'`<>()]+/.test(value)
  )
  if (containsSecret || containsAbsolutePath) {
    throw new Error('canonical artifact content violated the model-visible safety boundary')
  }
  return value
}

function maybeParseJson(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return undefined
  }
}

async function collectProcessOutput(child: ReturnType<typeof spawn>): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
  let stdout = ''
  let stderr = ''

  if (!child.stdout || !child.stderr) {
    throw new Error('CLI process did not expose stdout/stderr pipes')
  }

  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', (chunk: string) => {
    stdout += chunk
  })
  child.stderr.on('data', (chunk: string) => {
    stderr += chunk
  })

  const exitCode = await new Promise<number | null>((resolvePromise, rejectPromise) => {
    child.once('error', rejectPromise)
    child.once('close', resolvePromise)
  })

  return { stdout, stderr, exitCode }
}

function childEnvironment(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { PYTHONUNBUFFERED: '1' }
  for (const key of CHILD_ENV_ALLOWLIST) {
    const value = process.env[key]
    if (value != null) {
      env[key] = value
    }
  }
  return env
}

export async function callResearchCli(
  command: 'run' | 'artifact-read' | 'outcome-history',
  request: JsonObject,
  options: CliBridgeOptions = {},
): Promise<unknown> {
  const projectRoot = options.projectRoot ?? resolveProjectRoot()
  const workspace = options.workspace ?? resolveWorkspace(projectRoot)
  const pythonBin = options.pythonBin ?? resolvePythonBin(projectRoot)
  const env = childEnvironment()

  const child = spawn(
    pythonBin,
    ['-m', 'a_share_research.cli', '--workspace', workspace, command, '--request-json', '-'],
    {
      cwd: projectRoot,
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
      signal: options.signal,
    },
  )

  child.stdin.end(`${JSON.stringify(request)}\n`)
  const { stdout, stderr, exitCode } = await collectProcessOutput(child)

  const stdoutValue = maybeParseJson(stdout)
  const stderrValue = maybeParseJson(stderr)

  if (exitCode !== 0) {
    if (stderrValue && typeof stderrValue === 'object' && !Array.isArray(stderrValue)) {
      const cliError = sanitizeCliError(stderrValue as JsonObject)
      if (DOMAIN_CLI_ERRORS.has(cliError.error)) {
        return cliError
      }
      throw new Error(`a_share_research CLI infrastructure failure: ${cliError.error}`)
    }
    throw new Error(
      `a_share_research CLI failed for ${command} with non-canonical error output (exit ${String(exitCode)})`,
    )
  }

  if (stdoutValue !== undefined) {
    return stdoutValue
  }
  throw new Error(`a_share_research CLI returned invalid JSON for ${command}`)
}

function sanitizeBoundedJson(value: unknown, depth = 0): unknown {
  if (depth > 6 || value == null || typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string') return sanitizeErrorMessage(value).slice(0, 2000)
  if (Array.isArray(value)) return value.slice(0, 200).map((item) => sanitizeBoundedJson(item, depth + 1))
  if (typeof value === 'object') {
    const entries = Object.entries(value as JsonObject)
      .filter(([key]) => !/(?:secret|token|password|credential|api[_-]?key)/i.test(key))
      .slice(0, 100)
      .map(([key, item]) => [key, sanitizeBoundedJson(item, depth + 1)])
    return Object.fromEntries(entries)
  }
  return undefined
}

export function sanitizeOutcomeHistoryResult(raw: unknown): OutcomeHistoryResult {
  const payload = ensurePlainObject(raw, 'outcome history result')
  if (payload.error != null) return sanitizeCliError(payload)
  if (payload.market !== 'CN' || payload.schema_version !== '0.1') {
    throw new Error('CLI outcome history handshake failed; expected CN schema 0.1')
  }
  return sanitizeBoundedJson(payload) as JsonObject
}

export function sanitizeResearchRunResult(raw: unknown): ResearchRunResult {
  const payload = ensurePlainObject(raw, 'run result')
  if (payload.error != null) {
    return sanitizeCliError(payload)
  }

  if (payload.market !== 'CN') {
    throw new Error('CLI run result market handshake failed; expected CN')
  }
  if (payload.schema_version !== '0.1') {
    throw new Error('CLI run result schema handshake failed; expected 0.1')
  }

  const workflow = getString(payload.workflow, 'workflow')
  if (workflow !== 'daily_report' && workflow !== 'stock_research' && workflow !== 'theme_research') {
    throw new Error('CLI run result returned an unsupported workflow')
  }

  const schemaVersion = getString(payload.schema_version, 'schema_version') || '0.1'
  const runId = getString(payload.run_id, 'run_id') || ''
  const artifactId = getString(payload.artifact_id, 'artifact_id') || ''
  const status = getString(payload.status, 'status') || ''
  const writerMode = getString(payload.writer_mode, 'writer_mode') || ''
  const dataMode = getString(payload.data_mode, 'data_mode') || ''
  const pitQuality = getString(payload.pit_quality, 'pit_quality') || ''
  const decisionAt = getString(payload.decision_at, 'decision_at') || ''
  const snapshotId = getString(payload.snapshot_id, 'snapshot_id') || ''
  const analysisHash = getString(payload.analysis_hash, 'analysis_hash') || ''

  return omitUndefinedProperties({
    schema_version: schemaVersion,
    market: 'CN',
    run_id: runId,
    artifact_id: artifactId,
    status,
    writer_mode: writerMode,
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
    reused: getBoolean(payload.reused, 'reused'),
  })
}

export function sanitizeArtifactReadResult(raw: unknown, maxChars: number = DEFAULT_MAX_CHARS): ArtifactReadResult {
  const payload = ensurePlainObject(raw, 'artifact result')
  if (payload.error != null) {
    return sanitizeCliError(payload)
  }


  if (payload.market !== 'CN') {
    throw new Error('CLI artifact result market handshake failed; expected CN')
  }
  if (payload.schema_version !== '0.1') {
    throw new Error('CLI artifact result schema handshake failed; expected 0.1')
  }

  const section = getString(payload.section, 'section')
  if (section !== 'summary' && section !== 'report' && section !== 'manifest' && section !== 'packet' && section !== 'facts') {
    throw new Error('CLI artifact result returned an unsupported section')
  }

  const content = requireSafeArtifactContent(payload.content, maxChars)

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
    relative_path: sanitizeRelativePath(payload.relative_path),
  })
}

function renderResearchRun(value: ResearchRunResult): string {
  if ('error' in value) {
    return `market: ${value.market}\nerror: ${value.error}\nmessage: ${value.message}`
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
  ]
  if (value.counts) {
    lines.push(
      `counts: observe=${value.counts.observe ?? 0}, continue_research=${value.counts.continue_research ?? 0}, exclude=${value.counts.exclude ?? 0}`,
    )
  }
  if (value.focus?.length) {
    lines.push('', 'focus:')
    for (const item of value.focus.slice(0, 5)) {
      lines.push(`- ${item.name}(${item.symbol}) ${item.decision ?? ''}`.trim())
    }
  }
  if (value.available_sections?.length) {
    lines.push('', `sections: ${value.available_sections.join(', ')}`)
  }
  if (value.warnings?.length) {
    lines.push('', 'warnings:')
    lines.push(...value.warnings.slice(0, 5).map((warning) => `- ${warning}`))
  }
  if (value.gaps?.length) {
    lines.push('', 'gaps:')
    lines.push(...value.gaps.slice(0, 5).map((gap) => `- ${gap}`))
  }
  return lines.join('\n')
}

function renderArtifactRead(value: ArtifactReadResult): string {
  if ('error' in value) {
    return `market: ${value.market}\nerror: ${value.error}\nmessage: ${value.message}`
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
    value.content,
  ].filter(Boolean).join('\n').trim()
}

export async function runResearchWorkflow(args: unknown, options: CliBridgeOptions = {}): Promise<ResearchRunResult> {
  const normalized = normalizeRunArgs(args)
  const request: JsonObject = {
    schema_version: '0.1',
    market: 'CN',
    workflow: normalized.workflow,
    decision_at: normalized.decision_at,
    snapshot: {
      selector: normalized.snapshot.selector,
      ...(normalized.snapshot.id ? { snapshot_id: normalized.snapshot.id } : {}),
    },
    top_n: normalized.top_n ?? DEFAULT_TOP_N,
  }
  if (normalized.subject) {
    request.subject = normalized.subject
  }
  if (normalized.symbol) {
    request.symbol = normalized.symbol
  }
  const raw = await callResearchCli('run', request, options)
  return sanitizeResearchRunResult(raw)
}

export async function readArtifact(args: unknown, options: CliBridgeOptions = {}): Promise<ArtifactReadResult> {
  const normalized = normalizeArtifactReadArgs(args)
  const request: JsonObject = {
    artifact_id: normalized.artifact_id,
    section: normalized.section,
    max_chars: normalized.max_chars,
    cursor: normalized.cursor,
  }
  const raw = await callResearchCli('artifact-read', request, options)
  return sanitizeArtifactReadResult(raw, normalized.max_chars)
}

export async function readOutcomeHistory(
  args: unknown,
  options: CliBridgeOptions = {},
): Promise<OutcomeHistoryResult> {
  const normalized = normalizeOutcomeHistoryArgs(args)
  const raw = await callResearchCli('outcome-history', {
    schema_version: '0.1',
    market: 'CN',
    evaluation_at: normalized.evaluation_at,
    limit: normalized.limit,
  }, options)
  return sanitizeOutcomeHistoryResult(raw)
}

function registerResearchTool(ctx: Context) {
  return ctx.tools.register(defineTool({
    name: 'cn_research_run',
    description: 'Run a real CN A-share research workflow from a normalized snapshot. Use snapshot.selector=latest for the newest real snapshot or selector=id for a specific real snapshot. The run result is only a bounded summary: when the user requests the full research report, call cn_artifact_read with the returned artifact_id and section=report, and never present the run summary as the full report.',
    parameters: {
      workflow: {
        type: 'string',
        required: true,
        enum: ['daily_report', 'stock_research', 'theme_research'],
        description: 'Research workflow to execute.',
      },
      decision_at: {
        type: 'string',
        required: true,
        description: 'Timezone-aware ISO-8601 research cutoff, for example 2026-08-16T08:30:00+08:00.',
      },
      snapshot: {
        type: 'object',
        required: true,
        additionalProperties: false,
        properties: {
          selector: {
            type: 'string',
            required: true,
            enum: ['latest', 'id'],
            description: 'Real snapshot selector. Use latest for the newest normalized snapshot or id for a specific normalized snapshot. Demo fixtures are intentionally unavailable to model-facing research.',
          },
          id: {
            type: 'string',
            description: 'Canonical real snapshot id; required only when selector=id.',
          },
        },
        description: 'Normalized real-data snapshot source. Use latest for routine current research and id for reproducible research against a known snapshot.',
      },
      subject: {
        type: 'string',
        description: 'Theme or subject. Required for theme_research.',
      },
      symbol: {
        type: 'string',
        description: '6-digit A-share code. Required for stock_research.',
      },
      top_n: {
        type: 'integer',
        description: 'Candidate limit, 1-20.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
      },
      render: (_args, value) => [{ type: 'text', text: renderResearchRun(value as ResearchRunResult) }],
    },
    async execute(args, exec) {
      return runResearchWorkflow(args, { signal: exec.signal })
    },
  }))
}

function registerArtifactTool(ctx: Context) {
  return ctx.tools.register(defineTool({
    name: 'cn_artifact_read',
    description: 'Read one lossless page of a canonical CN research artifact section by artifact_id. When the user asks for a full or complete research report, section=report is required and every non-null next_cursor must be followed while content_sha256 remains stable; summary is only a bounded preview and must never be presented as the full report.',
    parameters: {
      artifact_id: {
        type: 'string',
        required: true,
        description: 'Canonical artifact id returned by cn_research_run.',
      },
      section: {
        type: 'string',
        enum: ['summary', 'report', 'manifest', 'packet', 'facts'],
        description: 'Artifact section to read. Use report for the full Markdown research report, facts for the thesis-blind fact packet, and summary only for a compact preview.',
      },
      max_chars: {
        type: 'integer',
        description: `Maximum content length to request from the CLI, ${MIN_MAX_CHARS}-${MAX_MAX_CHARS}.`,
      },
      cursor: {
        type: 'integer',
        description: 'Zero-based character cursor returned as next_cursor by the prior page.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
      },
      render: (_args, value) => [{ type: 'text', text: renderArtifactRead(value as ArtifactReadResult) }],
    },
    async execute(args, exec) {
      return readArtifact(args, { signal: exec.signal })
    },
  }))
}

function registerOutcomeHistoryTool(ctx: Context) {
  return ctx.tools.register(defineTool({
    name: 'cn_outcome_history',
    description: 'Read immutable T+5/T+20 research-state scorecards available by evaluation_at. This is calibration memory, never a trading signal.',
    parameters: {
      evaluation_at: {
        type: 'string',
        required: true,
        description: 'Timezone-aware availability cutoff for historical outcome memory.',
      },
      limit: {
        type: 'integer',
        description: 'Maximum prior runs, 1-20.',
      },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [{
        type: 'text',
        text: JSON.stringify(value as OutcomeHistoryResult, null, 2).slice(0, 22000),
      }],
    },
    async execute(args, exec) {
      return readOutcomeHistory(args, { signal: exec.signal })
    },
  }))
}

export function apply(ctx: Context) {
  registerResearchTool(ctx)
  registerArtifactTool(ctx)
  registerOutcomeHistoryTool(ctx)
}
