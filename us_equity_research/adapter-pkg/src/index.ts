import { spawn } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'us-equity-research-tools'
export const inject = ['tools']

export type ResearchWorkflow = 'daily_report' | 'theme_research' | 'stock_research'
type SnapshotSelector = 'demo' | 'latest' | 'id'
type ArtifactSection = 'summary' | 'report' | 'manifest' | 'packet'
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
    snapshot_id?: string
  }
  subject?: string
  symbol?: string
  top_n?: number
}

export type ArtifactReadArgs = {
  artifact_id: string
  section?: ArtifactSection
  max_chars?: number
}

type CliBridgeOptions = {
  projectRoot?: string
  workspace?: string
  pythonBin?: string
  signal?: AbortSignal
  timeoutMs?: number
  platform?: NodeJS.Platform
  processTreeTerminator?: ProcessTreeTerminator
}

type ProcessTreeTerminator = (
  child: ReturnType<typeof spawn>,
  signal: NodeJS.Signals,
) => void

type RunSuccessResult = {
  schema_version: '0.1'
  market: 'US'
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
  counts: {
    observe: number
    continue_research: number
    exclude: number
  }
  focus: Array<{
    symbol: string
    name: string
    theme?: string
    decision?: string
    reason?: string
  }>
  warnings: string[]
  gaps: string[]
  available_sections: ArtifactSection[]
  manifest_hash: string
  reused?: boolean
}

type ArtifactReadSuccessResult = {
  schema_version: '0.1'
  market: 'US'
  artifact_id: string
  section: ArtifactSection
  content_type: string
  content: string
  truncated: boolean
  relative_path?: string
}

type CliErrorResult = {
  schema_version: '0.1'
  market: 'US'
  error: string
  message: string
}

export type ResearchRunResult = RunSuccessResult | CliErrorResult
export type ArtifactReadResult = ArtifactReadSuccessResult | CliErrorResult

const SCHEMA_VERSION = '0.1' as const
const MARKET = 'US' as const
const DEFAULT_TOP_N = 5
const DEFAULT_MAX_CHARS = 12000
const MIN_MAX_CHARS = 500
const MAX_MAX_CHARS = 20000
const DEFAULT_TIMEOUT_MS = 120000
const MAX_TIMEOUT_MS = 600000
const MAX_CLI_OUTPUT_BYTES = 262144
const MAX_RENDER_CHARS = 22000
const MAX_FOCUS_ITEMS = 20
const MAX_PREVIEW_ITEMS = 5
const TERMINATION_GRACE_MS = 750
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
const WORKFLOWS = new Set<ResearchWorkflow>(['daily_report', 'theme_research', 'stock_research'])
const SNAPSHOT_SELECTORS = new Set<SnapshotSelector>(['demo', 'latest', 'id'])
const ARTIFACT_SECTIONS = new Set<ArtifactSection>(['summary', 'report', 'manifest', 'packet'])
const SAFE_IDENTIFIER = /^(?!\.)(?!.*\.\.)[A-Za-z0-9._-]{1,128}$/
const US_SYMBOL = /^[A-Z][A-Z0-9.-]{0,14}$/

class BridgeAbortError extends Error {
  constructor() {
    super('US research CLI request was aborted')
    this.name = 'AbortError'
  }
}

class BridgeTimeoutError extends Error {
  constructor() {
    super('US research CLI request timed out')
    this.name = 'BridgeTimeoutError'
  }
}

class BridgeOutputLimitError extends Error {
  constructor() {
    super('US research CLI exceeded its bounded output limit')
    this.name = 'BridgeOutputLimitError'
  }
}

export function resolveProjectRoot(fromUrl: string = import.meta.url): string {
  const currentDir = dirname(fileURLToPath(fromUrl))
  const configured = process.env.US_EQUITY_RESEARCH_ROOT
  return configured ? resolve(configured) : resolve(currentDir, '..', '..')
}

export function resolveWorkspace(projectRoot: string): string {
  const configured = process.env.STOCK_RESEARCH_WORKSPACE
  return configured ? resolve(configured) : dirname(projectRoot)
}

export function resolvePythonBin(projectRoot: string): string {
  const configured = process.env.US_EQUITY_RESEARCH_PYTHON_BIN
  if (configured) {
    return configured
  }
  return join(projectRoot, '.venv', 'bin', 'python')
}

export function assertSupportedPlatform(platform: NodeJS.Platform = process.platform): void {
  if (platform !== 'darwin' && platform !== 'linux') {
    throw new Error('us_equity_research adapter supports only macOS and Linux')
  }
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

function requireInputString(value: unknown, field: string, maxLength = 256): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`${field} must be a non-empty string`)
  }
  if (value.length > maxLength) {
    throw new Error(`${field} is too long`)
  }
  return value
}

function optionalInputString(value: unknown, field: string, maxLength = 256): string | undefined {
  if (value == null) {
    return undefined
  }
  const raw = requireInputString(value, field, maxLength).trim()
  if (!raw) {
    throw new Error(`${field} must be a non-empty string`)
  }
  return raw
}

function getInteger(value: unknown, field: string): number | undefined {
  if (value == null) {
    return undefined
  }
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) {
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

function validateIdentifier(value: unknown, field: string): string {
  const raw = requireInputString(value, field, 128)
  if (!SAFE_IDENTIFIER.test(raw)) {
    throw new Error(`${field} contains unsupported characters`)
  }
  return raw
}

function validateSymbol(value: unknown): string {
  const symbol = requireInputString(value, 'symbol', 15)
  if (!US_SYMBOL.test(symbol)) {
    throw new Error('symbol must match [A-Z][A-Z0-9.-]{0,14}')
  }
  return symbol
}

function validateDecisionAt(value: unknown): string {
  const raw = requireInputString(value, 'decision_at', 64)
  if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(raw) || Number.isNaN(Date.parse(raw))) {
    throw new Error('decision_at must be an ISO-8601 datetime with a timezone offset')
  }
  return raw
}

function normalizeTopN(value: unknown): number {
  const topN = getInteger(value, 'top_n') ?? DEFAULT_TOP_N
  if (topN < 1 || topN > 20) {
    throw new Error('top_n must be an integer between 1 and 20')
  }
  return topN
}

function normalizeMaxChars(value: unknown): number {
  const maxChars = getInteger(value, 'max_chars') ?? DEFAULT_MAX_CHARS
  if (maxChars < MIN_MAX_CHARS || maxChars > MAX_MAX_CHARS) {
    throw new Error(`max_chars must be an integer between ${MIN_MAX_CHARS} and ${MAX_MAX_CHARS}`)
  }
  return maxChars
}

function normalizeRunArgs(args: unknown): ResearchRunArgs {
  const raw = ensurePlainObject(args, 'us_research_run arguments')
  rejectUnknownFields(
    raw,
    ['workflow', 'decision_at', 'snapshot', 'subject', 'symbol', 'top_n'],
    'us_research_run arguments',
  )

  const workflow = requireInputString(raw.workflow, 'workflow') as ResearchWorkflow
  if (!WORKFLOWS.has(workflow)) {
    throw new Error('workflow must be one of daily_report, theme_research, stock_research')
  }
  const decisionAt = validateDecisionAt(raw.decision_at)
  const snapshotRaw = ensurePlainObject(raw.snapshot, 'snapshot')
  rejectUnknownFields(snapshotRaw, ['selector', 'snapshot_id'], 'snapshot')
  const selector = requireInputString(snapshotRaw.selector, 'snapshot.selector') as SnapshotSelector
  if (!SNAPSHOT_SELECTORS.has(selector)) {
    throw new Error('snapshot.selector must be one of demo, latest, id')
  }

  let snapshotId: string | undefined
  if (selector === 'id') {
    if (snapshotRaw.snapshot_id == null) {
      throw new Error('snapshot.snapshot_id is required when snapshot.selector=id')
    }
    snapshotId = validateIdentifier(snapshotRaw.snapshot_id, 'snapshot.snapshot_id')
  } else if (Object.hasOwn(snapshotRaw, 'snapshot_id')) {
    throw new Error('snapshot.snapshot_id is only allowed when snapshot.selector=id')
  }

  const hasSubject = Object.hasOwn(raw, 'subject')
  const hasSymbol = Object.hasOwn(raw, 'symbol')
  let subject: string | undefined
  let symbol: string | undefined
  if (workflow === 'daily_report') {
    if (hasSubject || hasSymbol) {
      throw new Error('daily_report does not accept subject or symbol')
    }
  } else if (workflow === 'theme_research') {
    if (!hasSubject) {
      throw new Error('subject is required for theme_research')
    }
    if (hasSymbol) {
      throw new Error('theme_research does not accept symbol')
    }
    subject = optionalInputString(raw.subject, 'subject', 256)
  } else {
    if (!hasSymbol) {
      throw new Error('symbol is required for stock_research')
    }
    if (hasSubject) {
      throw new Error('stock_research does not accept subject')
    }
    symbol = validateSymbol(raw.symbol)
  }

  return {
    workflow,
    decision_at: decisionAt,
    snapshot: {
      selector,
      ...(snapshotId ? { snapshot_id: snapshotId } : {}),
    },
    ...(subject ? { subject } : {}),
    ...(symbol ? { symbol } : {}),
    top_n: normalizeTopN(raw.top_n),
  }
}

function normalizeArtifactReadArgs(args: unknown): Required<ArtifactReadArgs> {
  const raw = ensurePlainObject(args, 'us_artifact_read arguments')
  rejectUnknownFields(raw, ['artifact_id', 'section', 'max_chars'], 'us_artifact_read arguments')
  const artifactId = validateIdentifier(raw.artifact_id, 'artifact_id')
  const sectionRaw = raw.section == null ? 'summary' : requireInputString(raw.section, 'section')
  if (!ARTIFACT_SECTIONS.has(sectionRaw as ArtifactSection)) {
    throw new Error('section must be summary, report, manifest, or packet')
  }
  return {
    artifact_id: artifactId,
    section: sectionRaw as ArtifactSection,
    max_chars: normalizeMaxChars(raw.max_chars),
  }
}

function redactSensitiveText(value: string, compact = false): string {
  const redacted = value
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [redacted]')
    .replace(
      /\b(api[_-]?key|access[_-]?token|authorization|password|secret|token)\b\s*[:=]\s*[^\s,;]+/gi,
      '$1=[redacted]',
    )
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[redacted-secret]')
    .replace(/(?:\/Users|\/home|\/private|\/tmp|\/var|\/opt|\/Volumes)\/[^\s"'`<>()]+/g, '[redacted-path]')
    .replace(/(^|[\s("'`])\/(?:[A-Za-z0-9._-]+\/)+[A-Za-z0-9._-]+/gm, '$1[redacted-path]')
    .replace(/[A-Za-z]:\\[^\s"'`<>()]+/g, '[redacted-path]')
    .replace(/\\\\[^\s\\]+\\[^\s"'`<>()]+/g, '[redacted-path]')
  return compact ? redacted.replace(/\s+/g, ' ').trim() : redacted
}

function boundedVisibleText(value: unknown, field: string, maxLength: number): string {
  if (typeof value !== 'string') {
    throw new Error(`${field} must be a string`)
  }
  return redactSensitiveText(value, true).slice(0, maxLength)
}

function boundedOptionalText(value: unknown, field: string, maxLength: number): string | undefined {
  if (value == null) {
    return undefined
  }
  const result = boundedVisibleText(value, field, maxLength)
  return result || undefined
}

function sanitizeStringArray(
  value: unknown,
  field: string,
  maxItems = MAX_PREVIEW_ITEMS,
): string[] {
  if (value == null) {
    return []
  }
  if (!Array.isArray(value)) {
    throw new Error(`${field} must be an array`)
  }
  return value.slice(0, maxItems).flatMap((item, index) => {
    if (typeof item !== 'string') {
      return []
    }
    const sanitized = boundedVisibleText(item, `${field}[${index}]`, 500)
    return sanitized ? [sanitized] : []
  })
}

function assertHandshake(payload: JsonObject, label: string): void {
  if (payload.market !== MARKET) {
    throw new Error(`${label} market handshake failed; expected US`)
  }
  if (payload.schema_version !== SCHEMA_VERSION) {
    throw new Error(`${label} schema handshake failed; expected 0.1`)
  }
}

function sanitizeCliError(raw: JsonObject): CliErrorResult {
  assertHandshake(raw, 'CLI error result')
  return {
    schema_version: SCHEMA_VERSION,
    market: MARKET,
    error: boundedVisibleText(raw.error, 'error', 80) || 'UnknownError',
    message: boundedVisibleText(raw.message, 'message', 500) || 'unknown CLI failure',
  }
}

function sanitizeCounts(value: unknown): RunSuccessResult['counts'] {
  const raw = ensurePlainObject(value, 'counts')
  const readCount = (field: string): number => {
    const count = getInteger(raw[field], `counts.${field}`)
    if (count == null || count < 0) {
      throw new Error(`counts.${field} must be a non-negative integer`)
    }
    return count
  }
  return {
    observe: readCount('observe'),
    continue_research: readCount('continue_research'),
    exclude: readCount('exclude'),
  }
}

function sanitizeFocus(value: unknown): RunSuccessResult['focus'] {
  if (!Array.isArray(value)) {
    throw new Error('focus must be an array')
  }
  return value.slice(0, MAX_FOCUS_ITEMS).flatMap((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      return []
    }
    const raw = item as JsonObject
    const symbol = boundedOptionalText(raw.symbol, `focus[${index}].symbol`, 15)
    const name = boundedOptionalText(raw.name, `focus[${index}].name`, 120)
    if (!symbol || !name || !US_SYMBOL.test(symbol)) {
      return []
    }
    return [{
      symbol,
      name,
      theme: boundedOptionalText(raw.theme, `focus[${index}].theme`, 160),
      decision: boundedOptionalText(raw.decision, `focus[${index}].decision`, 40),
      reason: boundedOptionalText(raw.reason, `focus[${index}].reason`, 500),
    }]
  })
}

function sanitizeSections(value: unknown): ArtifactSection[] {
  if (!Array.isArray(value)) {
    throw new Error('available_sections must be an array')
  }
  return value.flatMap((item) => (
    typeof item === 'string' && ARTIFACT_SECTIONS.has(item as ArtifactSection)
      ? [item as ArtifactSection]
      : []
  ))
}

function sanitizeRelativePath(value: unknown): string | undefined {
  if (value == null) {
    return undefined
  }
  if (typeof value !== 'string' || !value || value.length > 512 || value.includes('\\')) {
    return undefined
  }
  if (value.startsWith('/') || /^[A-Za-z]:/.test(value) || value.includes('://')) {
    return undefined
  }
  const parts = value.split('/')
  if (parts.some((part) => !part || part === '.' || part === '..' || !/^[A-Za-z0-9._-]+$/.test(part))) {
    return undefined
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

function childEnvironment(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
  }
  for (const key of CHILD_ENV_ALLOWLIST) {
    const value = process.env[key]
    if (value != null) {
      env[key] = value
    }
  }
  return env
}

function terminateProcessTree(child: ReturnType<typeof spawn>, signal: NodeJS.Signals): void {
  if (child.pid) {
    try {
      process.kill(-child.pid, signal)
      return
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ESRCH') {
        try {
          child.kill(signal)
        } catch {
          // The child may have exited between the group and direct kill attempts.
        }
      }
      return
    }
  }
}

type CollectedProcess = {
  stdout: string
  stderr: string
  exitCode: number | null
}

async function spawnBounded(
  pythonBin: string,
  argv: string[],
  request: JsonObject,
  projectRoot: string,
  options: CliBridgeOptions,
): Promise<CollectedProcess> {
  assertSupportedPlatform(options.platform ?? process.platform)
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > MAX_TIMEOUT_MS) {
    throw new Error(`timeoutMs must be an integer between 1 and ${MAX_TIMEOUT_MS}`)
  }
  if (options.signal?.aborted) {
    throw new BridgeAbortError()
  }

  const processTreeTerminator = options.processTreeTerminator ?? terminateProcessTree
  const child = spawn(pythonBin, argv, {
    cwd: projectRoot,
    env: childEnvironment(),
    stdio: ['pipe', 'pipe', 'pipe'],
    detached: true,
    windowsHide: true,
  })
  if (!child.stdout || !child.stderr || !child.stdin) {
    processTreeTerminator(child, 'SIGKILL')
    throw new Error('US research CLI process did not expose standard streams')
  }

  let stdout = ''
  let stderr = ''
  let outputBytes = 0
  let terminationReason: 'abort' | 'timeout' | 'output' | undefined
  let forceTimer: NodeJS.Timeout | undefined
  let childClosed = false

  const clearForceTimer = (): void => {
    if (forceTimer) {
      clearTimeout(forceTimer)
      forceTimer = undefined
    }
  }

  const requestTermination = (reason: typeof terminationReason): void => {
    if (terminationReason || childClosed) {
      return
    }
    terminationReason = reason
    processTreeTerminator(child, 'SIGTERM')
    forceTimer = setTimeout(() => {
      forceTimer = undefined
      if (!childClosed) {
        processTreeTerminator(child, 'SIGKILL')
      }
    }, TERMINATION_GRACE_MS)
    forceTimer.unref()
  }

  const append = (target: 'stdout' | 'stderr', chunk: Buffer | string): void => {
    const text = typeof chunk === 'string' ? chunk : chunk.toString('utf8')
    outputBytes += Buffer.byteLength(text)
    if (outputBytes > MAX_CLI_OUTPUT_BYTES) {
      requestTermination('output')
      return
    }
    if (target === 'stdout') stdout += text
    else stderr += text
  }
  child.stdout.on('data', (chunk: Buffer | string) => append('stdout', chunk))
  child.stderr.on('data', (chunk: Buffer | string) => append('stderr', chunk))
  child.stdin.on('error', () => {
    // Close/error handling below reports the canonical process outcome.
  })

  const closed = new Promise<number | null>((resolvePromise, rejectPromise) => {
    child.once('error', rejectPromise)
    child.once('close', (exitCode) => {
      childClosed = true
      clearForceTimer()
      resolvePromise(exitCode)
    })
  })
  const onAbort = (): void => requestTermination('abort')
  options.signal?.addEventListener('abort', onAbort, { once: true })
  if (options.signal?.aborted) {
    onAbort()
  }
  const timeout = setTimeout(() => requestTermination('timeout'), timeoutMs)
  timeout.unref()
  child.stdin.end(`${JSON.stringify(request)}\n`)

  let exitCode: number | null
  try {
    exitCode = await closed
  } catch (error) {
    throw new Error(`US research CLI could not start: ${redactSensitiveText(String((error as Error).message), true)}`)
  } finally {
    childClosed = true
    clearTimeout(timeout)
    clearForceTimer()
    options.signal?.removeEventListener('abort', onAbort)
  }

  if (terminationReason === 'abort') throw new BridgeAbortError()
  if (terminationReason === 'timeout') throw new BridgeTimeoutError()
  if (terminationReason === 'output') throw new BridgeOutputLimitError()
  return { stdout, stderr, exitCode }
}

export async function callResearchCli(
  command: 'run' | 'artifact-read',
  request: JsonObject,
  options: CliBridgeOptions = {},
): Promise<unknown> {
  const projectRoot = options.projectRoot ?? resolveProjectRoot()
  const workspace = options.workspace ?? resolveWorkspace(projectRoot)
  const pythonBin = options.pythonBin ?? resolvePythonBin(projectRoot)
  const argv = [
    '-m',
    'us_equity_research.cli',
    '--workspace',
    workspace,
    command,
    '--request-json',
    '-',
  ]
  const { stdout, stderr, exitCode } = await spawnBounded(
    pythonBin,
    argv,
    request,
    projectRoot,
    options,
  )
  const stdoutValue = maybeParseJson(stdout)
  const stderrValue = maybeParseJson(stderr)

  if (exitCode !== 0) {
    if (stderrValue && typeof stderrValue === 'object' && !Array.isArray(stderrValue)) {
      const cliError = sanitizeCliError(stderrValue as JsonObject)
      if (DOMAIN_CLI_ERRORS.has(cliError.error)) {
        return cliError
      }
      throw new Error(`us_equity_research CLI infrastructure failure: ${cliError.error}`)
    }
    throw new Error(
      `us_equity_research CLI failed for ${command} with non-canonical error output (exit ${String(exitCode)})`,
    )
  }
  if (stdoutValue && typeof stdoutValue === 'object' && !Array.isArray(stdoutValue)) {
    return stdoutValue
  }
  throw new Error(`us_equity_research CLI returned invalid JSON for ${command}`)
}

export function sanitizeResearchRunResult(raw: unknown): ResearchRunResult {
  const payload = ensurePlainObject(raw, 'run result')
  if (payload.error != null) {
    return sanitizeCliError(payload)
  }
  assertHandshake(payload, 'CLI run result')
  const workflow = boundedVisibleText(payload.workflow, 'workflow', 32) as ResearchWorkflow
  if (!WORKFLOWS.has(workflow)) {
    throw new Error('CLI run result returned an unsupported workflow')
  }

  return {
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
    manifest_hash: boundedVisibleText(payload.manifest_hash, 'manifest_hash', 128),
    reused: getBoolean(payload.reused, 'reused'),
  }
}

export function sanitizeArtifactReadResult(
  raw: unknown,
  maxChars: number = DEFAULT_MAX_CHARS,
): ArtifactReadResult {
  const payload = ensurePlainObject(raw, 'artifact result')
  if (payload.error != null) {
    return sanitizeCliError(payload)
  }
  assertHandshake(payload, 'CLI artifact result')
  const section = boundedVisibleText(payload.section, 'section', 16) as ArtifactSection
  if (!ARTIFACT_SECTIONS.has(section)) {
    throw new Error('CLI artifact result returned an unsupported section')
  }
  const normalizedMaxChars = normalizeMaxChars(maxChars)
  if (typeof payload.content !== 'string') {
    throw new Error('content must be a string')
  }
  const sanitizedContent = redactSensitiveText(payload.content)
  const marker = '\n…[truncated]'
  const wasCapped = sanitizedContent.length > normalizedMaxChars
  const content = wasCapped
    ? `${sanitizedContent.slice(0, Math.max(0, normalizedMaxChars - marker.length))}${marker}`
    : sanitizedContent
  return {
    schema_version: SCHEMA_VERSION,
    market: MARKET,
    artifact_id: validateIdentifier(payload.artifact_id, 'artifact_id'),
    section,
    content_type: boundedVisibleText(payload.content_type, 'content_type', 80),
    content,
    truncated: Boolean(getBoolean(payload.truncated, 'truncated') || wasCapped),
    relative_path: sanitizeRelativePath(payload.relative_path),
  }
}

function boundRendered(value: string): string {
  if (value.length <= MAX_RENDER_CHARS) {
    return value
  }
  const marker = '\n…[render truncated]'
  return `${value.slice(0, MAX_RENDER_CHARS - marker.length)}${marker}`
}

function renderResearchRun(value: ResearchRunResult): string {
  if ('error' in value) {
    return boundRendered(`market: ${value.market}\nerror: ${value.error}\nmessage: ${value.message}`)
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
    `counts: observe=${value.counts.observe}, continue_research=${value.counts.continue_research}, exclude=${value.counts.exclude}`,
  ]
  if (value.focus.length) {
    lines.push('', 'focus:')
    for (const item of value.focus) {
      lines.push(`- ${item.name}(${item.symbol}) ${item.decision ?? ''}`.trim())
    }
  }
  if (value.available_sections.length) {
    lines.push('', `sections: ${value.available_sections.join(', ')}`)
  }
  if (value.warnings.length) {
    lines.push('', 'warnings:', ...value.warnings.map((warning) => `- ${warning}`))
  }
  if (value.gaps.length) {
    lines.push('', 'gaps:', ...value.gaps.map((gap) => `- ${gap}`))
  }
  return boundRendered(lines.join('\n'))
}

function renderArtifactRead(value: ArtifactReadResult): string {
  if ('error' in value) {
    return boundRendered(`market: ${value.market}\nerror: ${value.error}\nmessage: ${value.message}`)
  }
  return boundRendered([
    `market: ${value.market}`,
    `artifact_id: ${value.artifact_id}`,
    `section: ${value.section}`,
    `content_type: ${value.content_type}`,
    `truncated: ${String(value.truncated)}`,
    value.relative_path ? `relative_path: ${value.relative_path}` : '',
    '',
    value.content,
  ].filter(Boolean).join('\n').trim())
}

export async function runResearchWorkflow(
  args: unknown,
  options: CliBridgeOptions = {},
): Promise<ResearchRunResult> {
  const normalized = normalizeRunArgs(args)
  const request: JsonObject = {
    schema_version: SCHEMA_VERSION,
    market: MARKET,
    workflow: normalized.workflow,
    decision_at: normalized.decision_at,
    snapshot: {
      selector: normalized.snapshot.selector,
      ...(normalized.snapshot.snapshot_id
        ? { snapshot_id: normalized.snapshot.snapshot_id }
        : {}),
    },
    top_n: normalized.top_n ?? DEFAULT_TOP_N,
  }
  if (normalized.subject) request.subject = normalized.subject
  if (normalized.symbol) request.symbol = normalized.symbol
  const raw = await callResearchCli('run', request, options)
  return sanitizeResearchRunResult(raw)
}

export async function readArtifact(
  args: unknown,
  options: CliBridgeOptions = {},
): Promise<ArtifactReadResult> {
  const normalized = normalizeArtifactReadArgs(args)
  const request: JsonObject = {
    artifact_id: normalized.artifact_id,
    section: normalized.section,
    max_chars: normalized.max_chars,
  }
  const raw = await callResearchCli('artifact-read', request, options)
  return sanitizeArtifactReadResult(raw, normalized.max_chars)
}

const researchTool = defineTool({
  name: 'us_research_run',
  description: 'Run the canonical, research-only US equity workflow through the Python CLI.',
  parameters: {
    workflow: {
      type: 'string',
      required: true,
      enum: ['daily_report', 'theme_research', 'stock_research'],
      description: 'Research workflow to execute.',
    },
    decision_at: {
      type: 'string',
      required: true,
      description: 'Timezone-aware ISO-8601 research cutoff, for example 2026-08-16T08:30:00-04:00.',
    },
    snapshot: {
      type: 'object',
      required: true,
      additionalProperties: false,
      properties: {
        selector: {
          type: 'string',
          required: true,
          enum: ['demo', 'latest', 'id'],
          description: 'Offline snapshot selector.',
        },
        snapshot_id: {
          type: 'string',
          description: 'Canonical snapshot ID; required only when selector=id.',
        },
      },
      description: 'Versioned offline snapshot source.',
    },
    subject: {
      type: 'string',
      description: 'Theme subject, required only for theme_research.',
    },
    symbol: {
      type: 'string',
      description: 'Uppercase US ticker, required only for stock_research.',
    },
    top_n: {
      type: 'integer',
      description: 'Candidate limit from 1 through 20.',
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
})

const artifactTool = defineTool({
  name: 'us_artifact_read',
  description: 'Read a bounded canonical US research artifact section by opaque artifact ID.',
  parameters: {
    artifact_id: {
      type: 'string',
      required: true,
      description: 'Opaque artifact ID returned by us_research_run.',
    },
    section: {
      type: 'string',
      enum: ['summary', 'report', 'manifest', 'packet'],
      description: 'Predefined artifact section to read.',
    },
    max_chars: {
      type: 'integer',
      description: `Maximum visible content length, ${MIN_MAX_CHARS}-${MAX_MAX_CHARS}.`,
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
})

export function apply(ctx: Context): void {
  ctx.tools.register(researchTool)
  ctx.tools.register(artifactTool)
}
