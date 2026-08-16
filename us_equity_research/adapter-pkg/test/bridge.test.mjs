import test from 'node:test'
import assert from 'node:assert/strict'
import { chmod, mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  apply,
  assertSupportedPlatform,
  callResearchCli,
  readArtifact,
  runResearchWorkflow,
  sanitizeArtifactReadResult,
  sanitizeResearchRunResult,
} from '../dist/index.js'

const testDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = join(testDir, '..')

async function makeTempProject() {
  const baseRoot = await mkdtemp(join(tmpdir(), 'us-equity-adapter-'))
  const workspace = join(baseRoot, 'workspace')
  const projectRoot = join(workspace, 'us_equity_research')
  await mkdir(join(projectRoot, '.venv', 'bin'), { recursive: true })
  return { baseRoot, workspace, projectRoot }
}

async function writeFakePython(projectRoot, { delayMs = 0, grandchildPidFile } = {}) {
  const pythonPath = join(projectRoot, '.venv', 'bin', 'python')
  const childSetup = grandchildPidFile
    ? `
const { spawn } = await import('node:child_process');
const { writeFileSync } = await import('node:fs');
const grandchild = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {
  stdio: 'ignore'
});
writeFileSync(${JSON.stringify(grandchildPidFile)}, String(grandchild.pid));
`
    : ''
  const script = `#!/usr/bin/env node
const chunks = [];
process.stdin.on('data', (chunk) => chunks.push(chunk));
process.stdin.on('end', async () => {
  const payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
  const argv = process.argv.slice(2);
  const workspaceIndex = argv.indexOf('--workspace');
  const workspace = workspaceIndex >= 0 ? argv[workspaceIndex + 1] : null;
  const command = workspaceIndex >= 0 ? argv[workspaceIndex + 2] : null;
  ${childSetup}
  const finish = (stream, value, code = 0) => {
    const write = () => {
      stream.write(JSON.stringify(value));
      if (code !== 0) process.exitCode = code;
    };
    if (${Number(delayMs)} > 0) setTimeout(write, ${Number(delayMs)});
    else write();
  };

  if (command === 'artifact-read') {
    if (payload.artifact_id === 'missing-artifact') {
      finish(process.stderr, {
        schema_version: '0.1', market: 'US', error: 'KeyError',
        message: 'not found under /Users/demo/private and C:\\\\Users\\\\demo\\\\secret; token=super-secret-value'
      }, 2);
      return;
    }
    if (payload.artifact_id === 'corrupt-artifact') {
      finish(process.stderr, {
        schema_version: '0.1', market: 'US', error: 'RuntimeError',
        message: 'immutable artifact mismatch at /home/demo/artifacts'
      }, 2);
      return;
    }
    if (payload.artifact_id === 'raw-error') {
      process.stderr.write('traceback at /Users/demo/private.py with sk-abcdefghijklmnopqrstuvwxyz');
      process.exitCode = 2;
      return;
    }
    finish(process.stdout, {
      schema_version: '0.1', market: 'US', artifact_id: payload.artifact_id,
      section: payload.section ?? 'summary',
      content_type: payload.section === 'report' ? 'text/markdown' : 'application/json',
      content: '# FIXTURE report\\n/private/demo/secret token=top-secret ' + 'x'.repeat(24000),
      truncated: false,
      relative_path: payload.artifact_id === 'unsafe-path'
        ? '../private/report.md'
        : 'artifacts/us/runs/us-run-demo/report.md',
      raw_provider_payload: 'must be dropped'
    });
    return;
  }

  finish(process.stdout, {
    argv, workspace, stdin: payload,
    inherited_secret: process.env.DEEPSEEK_API_KEY ?? null,
    inherited_openai: process.env.OPENAI_API_KEY ?? null,
    schema_version: '0.1', market: 'US',
    run_id: 'us-run-demo', artifact_id: 'us-artifact-demo',
    status: payload.subject === 'partial-case' ? 'partial' : 'completed',
    writer_mode: 'engine', data_mode: 'fixture',
    pit_quality: payload.subject === 'partial-case' ? 'UNKNOWN' : 'FIXTURE',
    workflow: payload.workflow, decision_at: payload.decision_at,
    snapshot_id: payload.snapshot?.snapshot_id ?? 'us-demo-snapshot-001',
    analysis_hash: 'analysis-demo', manifest_hash: 'manifest-demo',
    counts: { observe: 1, continue_research: 1, exclude: 1 },
    focus: [{
      symbol: payload.symbol ?? 'DEMOA', name: 'Synthetic Alpha',
      theme: payload.subject ?? 'Synthetic Theme', decision: 'observe',
      reason: 'fixture only', private_path: '/Users/demo/private'
    }],
    warnings: ['fixture only'], gaps: payload.subject === 'partial-case' ? ['UNKNOWN'] : [],
    available_sections: ['summary', 'report', 'manifest', 'packet'],
    raw_provider_payload: 'must be dropped'
  });
});
`
  await writeFile(pythonPath, script, 'utf8')
  await chmod(pythonPath, 0o755)
  return pythonPath
}

function canonicalRun(overrides = {}) {
  return {
    schema_version: '0.1',
    market: 'US',
    run_id: 'us-run-demo',
    artifact_id: 'us-artifact-demo',
    status: 'completed',
    writer_mode: 'engine',
    data_mode: 'fixture',
    pit_quality: 'FIXTURE',
    workflow: 'daily_report',
    decision_at: '2026-08-16T08:30:00-04:00',
    snapshot_id: 'us-demo-snapshot-001',
    analysis_hash: 'analysis-demo',
    manifest_hash: 'manifest-demo',
    counts: { observe: 1, continue_research: 1, exclude: 1 },
    focus: [{ symbol: 'DEMOA', name: 'Synthetic Alpha', decision: 'observe' }],
    warnings: [],
    gaps: [],
    available_sections: ['summary', 'report', 'manifest', 'packet'],
    ...overrides,
  }
}

async function waitFor(check, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await check()) return
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
  throw new Error('condition was not met before timeout')
}

function processExists(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    if (error?.code === 'ESRCH') return false
    throw error
  }
}

test('bundle declares only the official Cordis and dsh-tools dependencies', async () => {
  const manifest = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'))
  assert.deepEqual(manifest.peerDependencies, {
    '@deepseek-ai/cordis': '^4.0.1',
    '@deepseek-ai/dsh-tools': '^0.1.0-rc.6',
  })
  assert.equal(manifest.devDependencies['@deepseek-ai/dsh-tools'], '0.1.0-rc.6')
  assert.equal(manifest.dependencies?.['@deepseek-ai/dsh'], undefined)
  assert.equal(manifest.devDependencies?.['@deepseek-ai/dsh'], undefined)
  assert.deepEqual(manifest.os, ['darwin', 'linux'])
})

test('platform guard rejects non-Unix runtimes before spawning Python', async () => {
  assert.doesNotThrow(() => assertSupportedPlatform('darwin'))
  assert.doesNotThrow(() => assertSupportedPlatform('linux'))
  assert.throws(() => assertSupportedPlatform('win32'), /supports only macOS and Linux/)

  await assert.rejects(callResearchCli('run', {
    schema_version: '0.1', market: 'US', workflow: 'daily_report',
    decision_at: '2026-08-16T08:30:00-04:00', snapshot: { selector: 'demo' }, top_n: 5,
  }, {
    projectRoot: '/path-that-must-never-be-spawned',
    platform: 'win32',
  }), /supports only macOS and Linux/)
})

test('source statically defines exactly two model-facing tools', async () => {
  const source = await readFile(join(packageRoot, 'src', 'index.ts'), 'utf8')
  assert.equal((source.match(/defineTool\s*\(\s*\{/g) ?? []).length, 2)
  assert.equal(source.includes("from '@deepseek-ai/dsh-tools'"), true)
  assert.equal(source.includes("from '@deepseek-ai/dsh'"), false)
  assert.doesNotMatch(source, /\b(?:fetch|axios|sqlite|broker|order)\s*\(/i)
})

test('apply registers exactly the two US tool names', () => {
  const registered = []
  apply({ tools: { register(tool) { registered.push(tool) } } })
  assert.deepEqual(registered.map((tool) => tool.name).sort(), [
    'us_artifact_read',
    'us_research_run',
  ])
})

test('callResearchCli uses explicit argv, one JSON stdin object, and a secret-free env', async () => {
  const { workspace, projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)
  const savedDeepSeek = process.env.DEEPSEEK_API_KEY
  const savedOpenAI = process.env.OPENAI_API_KEY
  process.env.DEEPSEEK_API_KEY = 'must-not-reach-child'
  process.env.OPENAI_API_KEY = 'must-not-reach-child'
  try {
    const result = await callResearchCli('run', {
      schema_version: '0.1', market: 'US', workflow: 'daily_report',
      decision_at: '2026-08-16T08:30:00-04:00', snapshot: { selector: 'demo' }, top_n: 2,
    }, { projectRoot, workspace })
    assert.deepEqual(result.argv.slice(0, 6), [
      '-m', 'us_equity_research.cli', '--workspace', workspace, 'run', '--request-json',
    ])
    assert.equal(result.argv[6], '-')
    assert.equal(result.workspace, workspace)
    assert.equal(result.stdin.market, 'US')
    assert.deepEqual(result.stdin.snapshot, { selector: 'demo' })
    assert.equal(result.inherited_secret, null)
    assert.equal(result.inherited_openai, null)
  } finally {
    if (savedDeepSeek == null) delete process.env.DEEPSEEK_API_KEY
    else process.env.DEEPSEEK_API_KEY = savedDeepSeek
    if (savedOpenAI == null) delete process.env.OPENAI_API_KEY
    else process.env.OPENAI_API_KEY = savedOpenAI
  }
})

test('all three workflows map to the canonical public request shapes', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)
  const daily = await runResearchWorkflow({
    workflow: 'daily_report', decision_at: '2026-08-16T08:30:00-04:00',
    snapshot: { selector: 'demo' }, top_n: 3,
  }, { projectRoot })
  assert.equal(daily.market, 'US')
  assert.equal(daily.workflow, 'daily_report')

  const theme = await runResearchWorkflow({
    workflow: 'theme_research', decision_at: '2026-08-16T08:30:00-04:00',
    snapshot: { selector: 'latest' }, subject: 'Synthetic Theme', top_n: 4,
  }, { projectRoot })
  assert.equal(theme.workflow, 'theme_research')
  assert.equal(theme.focus[0].theme, 'Synthetic Theme')

  const stock = await runResearchWorkflow({
    workflow: 'stock_research', decision_at: '2026-08-16T08:30:00-04:00',
    snapshot: { selector: 'id', snapshot_id: 'us-snapshot-20260816' }, symbol: 'BRK.B', top_n: 1,
  }, { projectRoot })
  assert.equal(stock.workflow, 'stock_research')
  assert.equal(stock.snapshot_id, 'us-snapshot-20260816')
  assert.equal(stock.focus[0].symbol, 'BRK.B')
})

test('US ticker policy accepts exchange symbols and rejects unsafe spellings', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)
  for (const symbol of ['AAPL', 'BRK.B', 'RDS-A']) {
    const result = await runResearchWorkflow({
      workflow: 'stock_research', decision_at: '2026-08-16T08:30:00-04:00',
      snapshot: { selector: 'demo' }, symbol,
    }, { projectRoot })
    assert.equal(result.focus[0].symbol, symbol)
  }
  for (const symbol of ['aapl', ' AAPL', 'AAPL ', 'AAPL/US', '.AAPL', 'A'.repeat(16), '']) {
    await assert.rejects(runResearchWorkflow({
      workflow: 'stock_research', decision_at: '2026-08-16T08:30:00-04:00',
      snapshot: { selector: 'demo' }, symbol,
    }, { projectRoot }), /symbol/)
  }
})

test('workflow matrix, unknown fields, and snapshot_id contract are strict', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)
  const common = { decision_at: '2026-08-16T08:30:00-04:00', snapshot: { selector: 'demo' } }
  await assert.rejects(runResearchWorkflow({ workflow: 'daily_report', ...common, subject: 'x' }, { projectRoot }), /does not accept/)
  await assert.rejects(runResearchWorkflow({ workflow: 'theme_research', ...common }, { projectRoot }), /subject is required/)
  await assert.rejects(runResearchWorkflow({ workflow: 'theme_research', ...common, subject: 'x', symbol: 'AAPL' }, { projectRoot }), /does not accept symbol/)
  await assert.rejects(runResearchWorkflow({ workflow: 'stock_research', ...common }, { projectRoot }), /symbol is required/)
  await assert.rejects(runResearchWorkflow({ workflow: 'daily_report', ...common, output_path: '/tmp/leak' }, { projectRoot }), /unsupported fields/)
  await assert.rejects(runResearchWorkflow({
    workflow: 'daily_report', decision_at: common.decision_at,
    snapshot: { selector: 'id', id: 'legacy-alias' },
  }, { projectRoot }), /unsupported fields: id/)
  await assert.rejects(runResearchWorkflow({
    workflow: 'daily_report', decision_at: common.decision_at,
    snapshot: { selector: 'id' },
  }, { projectRoot }), /snapshot\.snapshot_id is required/)
  await assert.rejects(runResearchWorkflow({
    workflow: 'daily_report', decision_at: common.decision_at,
    snapshot: { selector: 'demo', snapshot_id: 'not-allowed' },
  }, { projectRoot }), /only allowed/)
})

test('run and artifact outputs enforce the US/schema handshake', () => {
  assert.throws(() => sanitizeResearchRunResult(canonicalRun({ market: 'CN' })), /market handshake failed/)
  assert.throws(() => sanitizeResearchRunResult(canonicalRun({ schema_version: '9' })), /schema handshake failed/)
  assert.throws(() => sanitizeArtifactReadResult({
    schema_version: '0.1', market: 'CN', artifact_id: 'us-artifact-demo',
    section: 'summary', content_type: 'text/plain', content: 'x', truncated: false,
  }), /market handshake failed/)
})

test('sanitizers whitelist, redact paths and secrets, and bound model-visible output', () => {
  const run = sanitizeResearchRunResult(canonicalRun({
    status: 'partial', pit_quality: 'UNKNOWN',
    warnings: [
      'see /Users/alice/private/file.json',
      'see /home/alice/private/file.json',
      'see C:\\Users\\alice\\private.txt',
      'Authorization: Bearer abcdefghijklmnop',
      'api_key=super-secret-value',
      ...Array.from({ length: 20 }, (_, index) => `extra-${index}`),
    ],
    gaps: ['UNKNOWN'],
    raw_provider_payload: 'drop-me',
  }))
  assert.equal(run.status, 'partial')
  assert.equal(run.pit_quality, 'UNKNOWN')
  assert.equal(run.raw_provider_payload, undefined)
  assert.ok(run.warnings.length <= 5)
  assert.doesNotMatch(JSON.stringify(run), /Users|home\/alice|super-secret|abcdefghijklmnop/)

  const artifact = sanitizeArtifactReadResult({
    schema_version: '0.1', market: 'US', artifact_id: 'us-artifact-demo',
    section: 'report', content_type: 'text/markdown',
    content: '# Report\n/private/alice/secret\n/etc/private/config\ntoken=top-secret\n' + 'x'.repeat(30000),
    truncated: false, relative_path: '../private/report.md', raw_provider_payload: 'drop-me',
  }, 1000)
  assert.equal(artifact.relative_path, undefined)
  assert.equal(artifact.raw_provider_payload, undefined)
  assert.equal(artifact.truncated, true)
  assert.ok(artifact.content.length <= 1000)
  assert.ok(artifact.content.endsWith('…[truncated]'))
  assert.match(artifact.content, /^# Report\n/)
  assert.doesNotMatch(artifact.content, /private\/alice|etc\/private|top-secret/)
})

test('structured run output preserves all 20 public focus items', () => {
  const focus = Array.from({ length: 20 }, (_, index) => ({
    symbol: `DEMO${String.fromCharCode(65 + index)}`,
    name: `Synthetic Company ${index + 1}`,
    theme: 'Synthetic Theme',
    decision: index % 2 === 0 ? 'observe' : 'continue_research',
    reason: `bounded fixture reason ${index + 1}`,
  }))
  const result = sanitizeResearchRunResult(canonicalRun({ focus }))
  assert.equal('error' in result, false)
  assert.equal(result.focus.length, 20)
  assert.deepEqual(result.focus.map((item) => item.symbol), focus.map((item) => item.symbol))
})

test('artifact bridge keeps domain errors structured and rejects infrastructure errors', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)
  const missing = await readArtifact({ artifact_id: 'missing-artifact', section: 'summary', max_chars: 1000 }, { projectRoot })
  assert.equal(missing.error, 'KeyError')
  assert.doesNotMatch(missing.message, /Users|C:\\|super-secret/)
  assert.match(missing.message, /\[redacted-path\]/)

  await assert.rejects(readArtifact({ artifact_id: 'corrupt-artifact' }, { projectRoot }), /infrastructure failure: RuntimeError/)
  await assert.rejects(readArtifact({ artifact_id: 'raw-error' }, { projectRoot }), /non-canonical error output/)
})

test('artifact reads retain only safe relative paths and bounded content', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)
  const safe = await readArtifact({ artifact_id: 'us-artifact-demo', section: 'report', max_chars: 700 }, { projectRoot })
  assert.equal(safe.relative_path, 'artifacts/us/runs/us-run-demo/report.md')
  assert.ok(safe.content.length <= 700)
  assert.equal(safe.truncated, true)

  const unsafe = await readArtifact({ artifact_id: 'unsafe-path', section: 'report', max_chars: 700 }, { projectRoot })
  assert.equal(unsafe.relative_path, undefined)
})

test('registered tool forwards exec.signal and cancellation leaves no grandchild', { skip: process.platform === 'win32' }, async () => {
  const { projectRoot, baseRoot } = await makeTempProject()
  const pidFile = join(baseRoot, 'grandchild.pid')
  await writeFakePython(projectRoot, { delayMs: 5000, grandchildPidFile: pidFile })
  const registered = []
  apply({ tools: { register(tool) { registered.push(tool) } } })
  const tool = registered.find((candidate) => candidate.name === 'us_research_run')
  const controller = new AbortController()
  const savedRoot = process.env.US_EQUITY_RESEARCH_ROOT
  process.env.US_EQUITY_RESEARCH_ROOT = projectRoot
  let grandchildPid
  try {
    const pending = tool.execute({
      workflow: 'daily_report', decision_at: '2026-08-16T08:30:00-04:00',
      snapshot: { selector: 'demo' },
    }, { signal: controller.signal })
    await waitFor(async () => {
      try {
        grandchildPid = Number(await readFile(pidFile, 'utf8'))
        return Number.isInteger(grandchildPid)
      } catch {
        return false
      }
    })
    controller.abort()
    await assert.rejects(pending, (error) => error?.name === 'AbortError')
    await waitFor(() => !processExists(grandchildPid))
  } finally {
    if (savedRoot == null) delete process.env.US_EQUITY_RESEARCH_ROOT
    else process.env.US_EQUITY_RESEARCH_ROOT = savedRoot
    if (grandchildPid && processExists(grandchildPid)) process.kill(grandchildPid, 'SIGKILL')
  }
})

test('bridge timeout terminates the child and reports an infrastructure timeout', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot, { delayMs: 5000 })
  await assert.rejects(callResearchCli('run', {
    schema_version: '0.1', market: 'US', workflow: 'daily_report',
    decision_at: '2026-08-16T08:30:00-04:00', snapshot: { selector: 'demo' }, top_n: 5,
  }, { projectRoot, timeoutMs: 50 }), /timed out/)
})

test('graceful SIGTERM close cancels the force timer without a delayed SIGKILL', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot, { delayMs: 5000 })
  const terminationSignals = []
  await assert.rejects(callResearchCli('run', {
    schema_version: '0.1', market: 'US', workflow: 'daily_report',
    decision_at: '2026-08-16T08:30:00-04:00', snapshot: { selector: 'demo' }, top_n: 5,
  }, {
    projectRoot,
    timeoutMs: 50,
    processTreeTerminator(child, signal) {
      terminationSignals.push(signal)
      child.kill(signal)
    },
  }), /timed out/)

  await new Promise((resolve) => setTimeout(resolve, 900))
  assert.deepEqual(terminationSignals, ['SIGTERM'])
})

test('partial and UNKNOWN with gaps are successful domain results', () => {
  const result = sanitizeResearchRunResult(canonicalRun({
    status: 'partial', pit_quality: 'UNKNOWN', warnings: ['partial data'], gaps: ['UNKNOWN'],
  }))
  assert.equal('error' in result, false)
  assert.equal(result.status, 'partial')
  assert.deepEqual(result.gaps, ['UNKNOWN'])
})
