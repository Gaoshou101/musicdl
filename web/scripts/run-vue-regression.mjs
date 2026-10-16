import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

/** Run the real Vue component tests instead of emulating a framework runtime. */
export function runRegression(...files) {
  const result = spawnSync(process.execPath, [
    fileURLToPath(new URL('../node_modules/vitest/vitest.mjs', import.meta.url)),
    'run', ...files,
  ], {
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    stdio: 'inherit',
  })
  if (result.error) throw result.error
  if (result.signal) throw new Error(`Vue regression process stopped: ${result.signal}`)
  process.exitCode = result.status ?? 1
}
