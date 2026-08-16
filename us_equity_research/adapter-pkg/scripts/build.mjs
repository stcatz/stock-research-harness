import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { stripTypeScriptTypes } from 'node:module'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = join(scriptDir, '..')
const sourcePath = join(packageRoot, 'src', 'index.ts')
const outputPath = join(packageRoot, 'dist', 'index.js')

const source = await readFile(sourcePath, 'utf8')
const transformed = stripTypeScriptTypes(source, {
  mode: 'transform',
})

await mkdir(dirname(outputPath), { recursive: true })
await writeFile(outputPath, transformed, 'utf8')
