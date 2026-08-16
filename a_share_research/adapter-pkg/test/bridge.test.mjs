import test from 'node:test'
import assert from 'node:assert/strict'
import { chmod, mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  apply,
  callResearchCli,
  readArtifact,
  runResearchWorkflow,
  sanitizeArtifactReadResult,
  sanitizeResearchRunResult,
} from '../dist/index.js'

function jsonRoundTrip(value) {
  return JSON.parse(JSON.stringify(value))
}

async function makeTempProject() {
  const baseRoot = await mkdtemp(join(tmpdir(), 'a-share-research-'))
  const workspace = join(baseRoot, 'workspace')
  const projectRoot = join(workspace, 'a_share_research')
  await mkdir(join(projectRoot, '.venv', 'bin'), { recursive: true })
  await mkdir(workspace, { recursive: true })
  return { baseRoot, workspace, projectRoot }
}

async function writeFakePython(projectRoot, { delayMs = 0 } = {}) {
  const pythonPath = join(projectRoot, '.venv', 'bin', 'python')
  const script = `#!/usr/bin/env node
const chunks = [];
process.stdin.on('data', (chunk) => chunks.push(chunk));
process.stdin.on('end', () => {
  const payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
  const argv = process.argv.slice(2);
  const workspaceIndex = argv.indexOf('--workspace');
  const workspace = workspaceIndex >= 0 ? argv[workspaceIndex + 1] : null;
  const command = workspaceIndex >= 0 ? argv[workspaceIndex + 2] : null;
  const delayMs = ${Number(delayMs)};
  const finish = (stream, value, code = 0) => {
    const write = () => {
      stream.write(JSON.stringify(value));
      if (code !== 0) process.exitCode = code;
    };
    if (delayMs > 0) {
      setTimeout(write, delayMs);
      return;
    }
    write();
  };

  if (command === 'artifact-read') {
    if (payload.artifact_id === 'missing-artifact') {
      finish(process.stderr, {
        schema_version: '0.1',
        market: 'CN',
        error: 'KeyError',
        message: 'artifact not found: missing-artifact at /Users/example/ai/stock/artifacts'
      }, 2);
      return;
    }
    if (payload.artifact_id === 'raw-error') {
      process.stderr.write('traceback at /Users/example/ai/stock/private.py');
      process.exitCode = 2;
      return;
    }
    if (payload.artifact_id === 'corrupt-artifact') {
      finish(process.stderr, {
        schema_version: '0.1',
        market: 'CN',
        error: 'RuntimeError',
        message: 'immutable artifact hash mismatch'
      }, 2);
      return;
    }
    finish(process.stdout, {
      schema_version: '0.1',
      market: 'CN',
      artifact_id: payload.artifact_id,
      section: payload.section ?? 'summary',
      content_type: payload.section === 'report' ? 'text/markdown' : 'application/json',
      content: payload.section === 'report'
        ? '# Report\\n' + 'x'.repeat(14000)
        : JSON.stringify({
            schema_version: '0.1',
            market: 'CN',
            artifact_id: payload.artifact_id,
            section: payload.section ?? 'summary',
            counts: { observe: 1, continue_research: 1, exclude: 0 }
          }, null, 2),
      truncated: false,
      relative_path: 'artifacts/runs/cn-2026-08-16-deadbeef/summary.json'
    });
    return;
  }

  finish(process.stdout, {
    argv,
    workspace,
    stdin: payload,
    inherited_secret: process.env.DEEPSEEK_API_KEY ?? null,
    schema_version: '0.1',
    market: 'CN',
    run_id: 'cn-2026-08-16-deadbeef12',
    artifact_id: 'cn-artifact-deadbeef1234',
    status: 'completed',
    writer_mode: 'engine',
    data_mode: 'fixture',
    pit_quality: 'FIXTURE',
    workflow: payload.workflow,
    decision_at: payload.decision_at,
    snapshot_id: payload.snapshot?.snapshot_id ?? 'demo-snapshot-001',
    analysis_hash: 'hash-demo',
    counts: { observe: 1, continue_research: 1, exclude: 0 },
    focus: [
      { symbol: '000000', name: '合成测试公司', theme: '合成测试主题', decision: 'continue_research', reason: 'fixture reason' }
    ],
    warnings: ['fixture only'],
    gaps: ['manual review required'],
    available_sections: ['summary', 'report', 'manifest', 'packet'],
    manifest_hash: 'manifest-hash-demo'
  });
});
`
  await writeFile(pythonPath, script, 'utf8')
  await chmod(pythonPath, 0o755)
  return pythonPath
}

test('apply registers both official tool names', () => {
  const registered = []
  apply({
    tools: {
      register(tool) {
        registered.push(tool)
      },
    },
  })

  assert.deepEqual(
    registered.map((tool) => tool.name).sort(),
    ['cn_artifact_read', 'cn_research_run'],
  )
})

test('callResearchCli sends canonical argv and JSON stdin', async () => {
  const { workspace, projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)

  const previousSecret = process.env.DEEPSEEK_API_KEY
  process.env.DEEPSEEK_API_KEY = 'must-not-reach-python'
  let result
  try {
    result = await callResearchCli(
      'run',
      {
        schema_version: '0.1',
        market: 'CN',
        workflow: 'daily_report',
        decision_at: '2026-08-16T08:30:00+08:00',
        snapshot: { selector: 'demo' },
        top_n: 2,
      },
      { projectRoot, workspace },
    )
  } finally {
    if (previousSecret == null) delete process.env.DEEPSEEK_API_KEY
    else process.env.DEEPSEEK_API_KEY = previousSecret
  }

  assert.equal(result.argv[0], '-m')
  assert.equal(result.argv[1], 'a_share_research.cli')
  assert.equal(result.argv[2], '--workspace')
  assert.equal(result.argv[3], workspace)
  assert.equal(result.argv[4], 'run')
  assert.equal(result.stdin.market, 'CN')
  assert.equal(result.stdin.snapshot.selector, 'demo')
  assert.equal(result.workspace, workspace)
  assert.equal(result.inherited_secret, null)
})

test('callResearchCli honors abort signals', async () => {
  const { workspace, projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot, { delayMs: 2000 })
  const controller = new AbortController()

  const pending = callResearchCli(
    'run',
    {
      schema_version: '0.1',
      market: 'CN',
      workflow: 'daily_report',
      decision_at: '2026-08-16T08:30:00+08:00',
      snapshot: { selector: 'demo' },
      top_n: 2,
    },
    {
      projectRoot,
      workspace,
      signal: controller.signal,
    },
  )

  setTimeout(() => controller.abort(), 50)

  await assert.rejects(pending, /AbortError|aborted/)
})

test('runResearchWorkflow maps the new canonical request contract', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)

  const result = await runResearchWorkflow(
    {
      workflow: 'stock_research',
      decision_at: '2026-08-16T08:30:00+08:00',
      snapshot: { selector: 'id', id: 'snapshot-2026-08-15' },
      symbol: '000000',
      top_n: 4,
    },
    { projectRoot },
  )

  assert.equal(result.market, 'CN')
  assert.ok(!('error' in result))
  assert.equal(result.workflow, 'stock_research')
  assert.equal(result.artifact_id, 'cn-artifact-deadbeef1234')
  assert.equal(result.snapshot_id, 'snapshot-2026-08-15')
  assert.equal(result.focus[0].symbol, '000000')
  assert.deepEqual(result.available_sections, ['summary', 'report', 'manifest', 'packet'])

  const daily = await runResearchWorkflow(
    {
      workflow: 'daily_report',
      decision_at: '2026-08-16T08:30:00+08:00',
      snapshot: { selector: 'demo', id: '' },
      subject: '',
      symbol: '',
    },
    { projectRoot },
  )
  assert.ok(!('error' in daily))
  assert.equal(daily.workflow, 'daily_report')
})

test('runResearchWorkflow rejects fields the canonical CLI contract does not allow', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)

  await assert.rejects(
    runResearchWorkflow(
      {
        workflow: 'daily_report',
        decision_at: '2026-08-16T08:30:00+08:00',
        snapshot: { selector: 'demo' },
        subject: 'not allowed',
      },
      { projectRoot },
    ),
    /daily_report does not accept subject or symbol/,
  )

  await assert.rejects(
    runResearchWorkflow(
      {
        workflow: 'theme_research',
        decision_at: '2026-08-16T08:30:00+08:00',
        snapshot: { selector: 'latest', id: 'not-allowed' },
        subject: 'AI产业链',
      },
      { projectRoot },
    ),
    /snapshot.id is only allowed when snapshot.selector=id/,
  )

  await assert.rejects(
    runResearchWorkflow(
      {
        workflow: 'stock_research',
        decision_at: '2026-08-16T08:30:00+08:00',
        snapshot: { selector: 'demo' },
        symbol: '000000',
        subject: 'not allowed',
      },
      { projectRoot },
    ),
    /stock_research does not accept subject/,
  )

  await assert.rejects(
    runResearchWorkflow(
      {
        workflow: 'daily_report',
        decision_at: '2026-08-16T08:30:00+08:00',
        snapshot: { selector: 'demo' },
        output_path: '/tmp/not-allowed',
      },
      { projectRoot },
    ),
    /unsupported fields: output_path/,
  )
})

test('readArtifact uses artifact_id and preserves canonical CLI errors', async () => {
  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot)

  const report = await readArtifact(
    { artifact_id: 'cn-artifact-deadbeef1234', section: 'report', max_chars: 1000 },
    { projectRoot },
  )
  assert.equal(report.market, 'CN')
  assert.ok(!('error' in report))
  assert.equal(report.section, 'report')
  assert.equal(report.truncated, true)
  assert.ok(report.content.includes('…[truncated]'))

  const missing = await readArtifact(
    { artifact_id: 'missing-artifact', section: 'summary', max_chars: 1000 },
    { projectRoot },
  )
  assert.deepEqual(missing, {
    schema_version: '0.1',
    market: 'CN',
    error: 'KeyError',
    message: 'artifact not found: missing-artifact at [redacted-path]',
  })

  await assert.rejects(
    readArtifact(
      { artifact_id: 'corrupt-artifact', section: 'summary', max_chars: 1000 },
      { projectRoot },
    ),
    /infrastructure failure: RuntimeError/,
  )

  await assert.rejects(
    readArtifact(
      { artifact_id: 'raw-error', section: 'summary', max_chars: 1000 },
      { projectRoot },
    ),
    /non-canonical error output/,
  )
})

test('registered tool execute forwards exec.signal to the CLI', async () => {
  const registered = []
  apply({
    tools: {
      register(tool) {
        registered.push(tool)
      },
    },
  })

  const { projectRoot } = await makeTempProject()
  await writeFakePython(projectRoot, { delayMs: 2000 })
  const controller = new AbortController()
  const previousRoot = process.env.A_SHARE_RESEARCH_ROOT
  process.env.A_SHARE_RESEARCH_ROOT = projectRoot

  const researchTool = registered.find((tool) => tool.name === 'cn_research_run')
  assert.ok(researchTool)

  try {
    const pending = researchTool.execute(
      {
        workflow: 'daily_report',
        decision_at: '2026-08-16T08:30:00+08:00',
        snapshot: { selector: 'demo' },
      },
      { signal: controller.signal },
    )

    setTimeout(() => controller.abort(), 50)

    await assert.rejects(pending, /AbortError|aborted/)
  } finally {
    if (previousRoot == null) {
      delete process.env.A_SHARE_RESEARCH_ROOT
    } else {
      process.env.A_SHARE_RESEARCH_ROOT = previousRoot
    }
  }
})

test('sanitize helpers whitelist the new canonical CLI payloads', () => {
  const run = sanitizeResearchRunResult({
    schema_version: '0.1',
    market: 'CN',
    run_id: 'cn-run-demo',
    artifact_id: 'cn-artifact-demo',
    status: 'completed',
    writer_mode: 'engine',
    data_mode: 'fixture',
    pit_quality: 'FIXTURE',
    workflow: 'daily_report',
    decision_at: '2026-08-16T08:30:00+08:00',
    snapshot_id: 'demo-snapshot-001',
    analysis_hash: 'hash-demo',
    counts: { observe: 1, continue_research: 2, exclude: 3 },
    focus: [{ symbol: '000000', name: '合成测试公司', theme: '合成测试主题', decision: 'observe', reason: 'demo' }],
    warnings: ['fixture only'],
    gaps: ['manual review'],
    available_sections: ['summary'],
    secret_raw_blob: 'drop me',
  })
  assert.ok(!('error' in run))
  assert.equal(run.artifact_id, 'cn-artifact-demo')
  assert.equal(run.secret_raw_blob, undefined)

  const artifact = sanitizeArtifactReadResult({
    schema_version: '0.1',
    market: 'CN',
    artifact_id: 'cn-artifact-demo',
    section: 'summary',
    content_type: 'application/json',
    content: 'x'.repeat(13000),
    truncated: false,
    relative_path: '../secret.txt',
  }, 1000)
  assert.ok(!('error' in artifact))
  assert.equal(artifact.truncated, true)
  assert.equal(artifact.relative_path, undefined)
  assert.ok(artifact.content.length <= 1000)
  assert.ok(artifact.content.endsWith('…[truncated]'))

  assert.throws(
    () => sanitizeResearchRunResult({ schema_version: '0.1', market: 'US' }),
    /market handshake failed/,
  )
})

test('sanitize helpers omit absent optional fields and remain lossless JSON', () => {
  const run = sanitizeResearchRunResult({
    schema_version: '0.1',
    market: 'CN',
    run_id: 'cn-run-demo',
    artifact_id: 'cn-artifact-demo',
    status: 'completed',
    writer_mode: 'engine',
    data_mode: 'fixture',
    pit_quality: 'FIXTURE',
    workflow: 'daily_report',
    decision_at: '2026-08-16T08:30:00+08:00',
    snapshot_id: 'demo-snapshot-001',
    analysis_hash: 'hash-demo',
    counts: { observe: 2 },
    focus: [{ symbol: '000001', name: '最小样例', decision: 'observe' }],
  })
  assert.ok(!('error' in run))
  assert.deepEqual(run, jsonRoundTrip(run))
  assert.deepEqual(run.counts, { observe: 2 })
  assert.deepEqual(run.focus, [{ symbol: '000001', name: '最小样例', decision: 'observe' }])
  assert.equal('continue_research' in run.counts, false)
  assert.equal('exclude' in run.counts, false)
  assert.equal('theme' in run.focus[0], false)
  assert.equal('reason' in run.focus[0], false)
  assert.equal('reused' in run, false)
  assert.equal('warnings' in run, false)

  const artifact = sanitizeArtifactReadResult({
    schema_version: '0.1',
    market: 'CN',
    artifact_id: 'cn-artifact-demo',
    section: 'summary',
    content_type: 'application/json',
    content: '{"ok":true}',
    truncated: false,
  }, 1000)
  assert.ok(!('error' in artifact))
  assert.deepEqual(artifact, jsonRoundTrip(artifact))
  assert.equal('relative_path' in artifact, false)
})
