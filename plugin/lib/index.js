// @atta/dsh-atta — DSH headless modeling plugin (host side).
//
// The plugin is a thin shell.  Geometry truth lives in the Atta engine:
//
//   python -m atta --json tool <name> --args-file <tmp.json>
//
// Node's job: pick a usable interpreter, spawn the CLI, parse the `data` field,
// attach preview PNGs, and surface a compact error envelope ({data, error})
// instead of a stack trace.
//
// Tools (10, all `atta_*` so they can never collide with another plugin's
// namespace — e.g. the `blender_*` tools of dsh-blender-plugin, which must be
// installable in the same DSH profile):
//   atta_doctor    engine / Blender / catalog self-check
//   atta_catalog   recipes, feature schema, constraints, workflow
//   atta_spec      Spec v3 validate / propose / compile / schema
//   atta_model     recipe | Spec v3 | feature graph build, refine via patches
//   atta_variant   parameter sampling / solve / fit, ranked candidates
//   atta_describe  measured facts: bbox, volume, topology, holes, wall, rays
//   atta_verify    pass | degraded | fail with a receipt
//   atta_preview   judgement/presentation PNGs + facts + image analysis
//   atta_deliver   verify gate, then GLB/OBJ/STL + manifest
//   atta_run       one call, several steps
//
// Units: public params/spec are mm; the Blender scene is metres.
//
// ───────────────────────────────────────────────────────────────────────────
// THREE RULES THIS FILE EXISTS TO ENFORCE
// ───────────────────────────────────────────────────────────────────────────
//
// 1. NEVER THROW AT IMPORT TIME.  A module that throws while the host imports
//    it makes every tool vanish, and the host only reports "failed to import"
//    with the real cause swallowed.  That symptom and its cause are in
//    different layers, so it is close to unfixable for a user who has never
//    seen this repo.  Everything that can fail to resolve (host defineTool,
//    schemastery, the Python interpreter, the repo root) resolves through a
//    function that RECORDS WHAT IT TRIED and returns a result instead.  On
//    failure we DEGRADE: register one `atta_plugin_status` tool that explains
//    the problem and the fix, and keep the plugin loaded.
//
// 2. EVERY TOOL RETURN GOES THROUGH ONE losslessSanitize EXIT GATE.  The host
//    validates tool output with a walker stricter than JSON.stringify: a
//    single `undefined` value, NaN, Infinity, -0, Date, Map, cyclic reference
//    or class instance anywhere in the payload rejects the WHOLE result with
//    "tool returned invalid output" — the work was done, the receipt is
//    refused.  So sanitize once, at the boundary, and report what changed.
//
// 3. TOOL NAMES AND GLOBAL STATE ARE NAMESPACED.  `atta_*` only, and no
//    globalThis keys, so a second plugin in the same profile cannot collide
//    with us or be corrupted by us.
//
// Location independence: the package may be `link:`ed into a profile, installed
// from a registry, or run from the checkout.  Nothing below assumes a path.
// The Python interpreter is discovered (ATTA_PYTHON → venv → python3/python/
// py -3) and *verified by importing atta*, and failures name the exact knob.

import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { existsSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url)) // .../plugin/lib
const NL = String.fromCharCode(10)
const VERSION = '1.2.0'
const PLUGIN_NAME = '@atta/dsh-atta'
const DIAGNOSTIC_TOOL = 'atta_plugin_status'

// Host-side deadlines for the Python CLI.  These are static (defineTool only
// accepts constants); each one is the engine's own budget plus margin, so the
// engine's readable error always fires before the host's deadline.
const TOOL_TIMEOUT_MS = {
  atta_doctor: 120000,
  atta_catalog: 120000,
  atta_spec: 120000,
  atta_model: 600000,
  atta_variant: 1200000,
  atta_describe: 300000,
  atta_verify: 600000,
  atta_preview: 3600000,
  atta_deliver: 600000,
  atta_run: 1800000,
}
const DIAGNOSTIC_TIMEOUT_MS = 10000

// Modeling is stateful (model -> verify -> preview read the same workspace),
// so only genuinely read-only verbs are advertised as concurrency-safe.
const CONCURRENT = {
  atta_doctor: true,
  atta_catalog: true,
  atta_spec: true,
  atta_model: false,
  atta_variant: false,
  atta_describe: true,
  atta_verify: false,
  atta_preview: false,
  atta_deliver: false,
  atta_run: false,
}

const TOOL_NAMES = Object.keys(TOOL_TIMEOUT_MS)

/** First line of an error, truncated: a diagnostic must fit on one line. */
function errLine(value) {
  const raw = String((value && (value.code || value.message)) || value || 'unknown error')
  return raw.split(NL)[0].slice(0, 200)
}

/**
 * The useful line out of a failed Python probe.
 *
 * `traceback.format_exc()` starts with "Traceback (most recent call last):",
 * and that first line is exactly what errLine would keep — so the receipt said
 * "python3 (PATH): Traceback (most recent call last):" and told the user
 * nothing.  The informative part is the exception line: last non-empty line,
 * preferring one that looks like `SomeError: message`.
 * @param {string} stderr captured stderr
 * @returns {string} one line, empty when stderr carried nothing
 */
export function pythonErrorLine(stderr) {
  const lines = String(stderr || '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  if (!lines.length) return ''
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    if (/^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception|Exit)\b.*:/.test(lines[index])) {
      return lines[index].slice(0, 200)
    }
  }
  // No recognisable exception: the last line still beats the traceback header.
  return lines[lines.length - 1].slice(0, 200)
}

/** Object (non-array, non-null) check for values that crossed a process boundary. */
function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

// ────────────────────────────────────────────────────────────── lossless exit gate
//
// Mirrors the host's own walker (@deepseek-ai/dsh-util-values walkJsonValue).
// Kept local on purpose: the plugin must behave identically whether or not the
// host packages are resolvable from here, and this file must not depend on the
// very resolution that may be failing.  Silent coercion would hide bugs, so
// every change is recorded in `fixes` and surfaced in the receipt.
export function losslessSanitize(root) {
  const fixes = []
  const seen = new Set()
  const walk = (value, at) => {
    if (value === null) return null
    const type = typeof value
    if (type === 'string' || type === 'boolean') return value
    if (type === 'number') {
      if (!Number.isFinite(value)) {
        fixes.push(at + '=' + String(value) + '->null')
        return null
      }
      if (Object.is(value, -0)) {
        fixes.push(at + '=-0->0')
        return 0
      }
      return value
    }
    if (type === 'undefined') {
      fixes.push(at + '=undefined->dropped')
      return undefined
    }
    if (type === 'bigint') {
      const asNumber = Number(value)
      const safe = Number.isSafeInteger(asNumber)
      fixes.push(at + '=bigint->' + (safe ? 'number' : 'string'))
      return safe ? asNumber : String(value)
    }
    if (type === 'function' || type === 'symbol') {
      fixes.push(at + '=' + type + '->dropped')
      return undefined
    }
    if (seen.has(value)) {
      fixes.push(at + '=cycle->[Circular]')
      return '[Circular]'
    }
    seen.add(value)
    let out
    if (Array.isArray(value)) {
      out = []
      for (let index = 0; index < value.length; index += 1) {
        const item = walk(value[index], at + '[' + String(index) + ']')
        out.push(item === undefined ? null : item)
      }
    } else if (value instanceof Date) {
      const ms = value.getTime()
      fixes.push(at + '=Date->ISO')
      out = Number.isFinite(ms) ? value.toISOString() : null
    } else if (value instanceof Map) {
      fixes.push(at + '=Map->array')
      out = Array.from(value.entries()).map((entry, index) =>
        walk(entry, at + '<map' + String(index) + '>'),
      )
    } else if (value instanceof Set) {
      fixes.push(at + '=Set->array')
      out = Array.from(value.values()).map((item, index) =>
        walk(item, at + '<set' + String(index) + '>'),
      )
    } else if (ArrayBuffer.isView(value) || value instanceof ArrayBuffer) {
      const bytes = value.byteLength
      fixes.push(at + '=binary(' + String(bytes) + 'B)->{bytes}')
      out = { bytes: bytes, why: 'binary-not-json' }
    } else {
      // Plain records and class instances both project onto own enumerable
      // keys; that is exactly what JSON.stringify would have kept.
      out = {}
      for (const key of Object.keys(value)) {
        const item = walk(value[key], at + '.' + key)
        if (item !== undefined) out[key] = item
      }
    }
    seen.delete(value) // shared (non-cyclic) DAG nodes may appear twice
    return out
  }
  const value = walk(root, 'value')
  return { value: value === undefined ? null : value, fixes: fixes }
}

/** Back-compat alias: earlier revisions exported this name. */
export const sanitize = losslessSanitize

// ────────────────────────────────────────────────────── host API (defineTool)
//
// Resolution order, and why:
//   ① ESM bare `import()` — the path the host's own loader takes, so it wins
//      in profile layouts where a createRequire is intercepted or misses.
//   ② createRequire — covers CJS-only host builds and older layouts.
//   ③ filesystem fallbacks — the global npm install and the profile dir.
// Every attempt is recorded; failure yields {ok:false} and NEVER throws.
const DSH_TOOLS_ID = '@deepseek-ai/dsh-tools'
const SCHEMASTERY_IDS = ['@deepseek-ai/schemastery', 'schemastery']

function pickDefineTool(module) {
  if (!module) return undefined
  const direct = module.defineTool
  if (typeof direct === 'function') return direct
  const viaDefault = module.default && module.default.defineTool
  if (typeof viaDefault === 'function') return viaDefault
  return undefined
}

function pickSchemastery(module) {
  if (!module) return undefined
  const candidate = module.default ? module.default : module
  if (candidate && typeof candidate.object === 'function') return candidate
  return undefined
}

/**
 * Filesystem roots that may hold a DSH-provided package, most specific first.
 *
 * This matters more than it looks.  A profile install is usually a symlink
 * (`link:../Atta/plugin`), and Node resolves a symlinked entry to its realpath
 * before it walks `node_modules` — so the plugin ends up resolving from the
 * checkout, not from the profile, and the bare specifier that the host itself
 * resolves fine fails here.  The host does provide these packages; we just have
 * to look where DSH actually keeps them.
 * @returns {string[]} absolute `node_modules` directories to probe
 */
export function nodeModulesRoots(options = {}) {
  const env = options.env || process.env
  const home = options.home || os.homedir()
  const execPath = options.execPath || process.execPath
  const execDir = path.dirname(execPath)
  const anchor = options.anchor || path.join(execDir, '..', 'lib')
  const roots = []
  const push = (dir) => {
    const resolved = path.normalize(dir)
    if (resolved && !roots.includes(resolved)) roots.push(resolved)
  }
  if (env.DSH_HOME) push(path.join(String(env.DSH_HOME), 'profiles', 'node_modules'))
  push(path.join(home, '.dsh', 'profiles', 'node_modules'))
  // Global npm install: <prefix>/bin/node -> <prefix>/lib/node_modules/...
  push(path.join(anchor, 'node_modules'))
  push(path.join(anchor, 'node_modules', '@deepseek-ai', 'dsh', 'node_modules'))
  // Local npx/global layouts where the package sits one level up.
  push(path.join(execDir, '..', 'lib', 'node_modules', '@deepseek-ai', 'dsh', 'node_modules'))
  return roots
}

/**
 * Entry files for one installed package directory, read from its manifest.
 *
 * Guessing `lib/index.js` is not good enough: packages in the same DSH tree
 * disagree (dsh-tools ships lib/index.js, schemastery ships lib/index.mjs).
 * Read `exports["."]` then `main`/`module`, and only fall back to conventional
 * names when the manifest cannot be read.
 * @param {string} dir package root
 * @returns {string[]} absolute entry-file candidates
 */
export function packageEntryCandidates(dir, options = {}) {
  const exists = options.exists || existsSync
  const readFile = options.readFile || ((file) => readFileSync(file, 'utf8'))
  const entries = []
  const push = (rel) => {
    if (!rel) return
    const abs = path.resolve(dir, String(rel))
    if (!entries.includes(abs)) entries.push(abs)
  }
  try {
    const manifest = JSON.parse(readFile(path.join(dir, 'package.json')))
    const exported = manifest && manifest.exports
    const dot = exported && typeof exported === 'object' ? exported['.'] : exported
    if (typeof dot === 'string') {
      push(dot)
    } else if (dot && typeof dot === 'object') {
      // Prefer the runtime entry points, in ESM-then-CJS order.
      for (const key of ['import', 'node', 'default', 'require']) {
        const value = dot[key]
        if (typeof value === 'string') push(value)
        else if (value && typeof value === 'object') {
          for (const nested of ['import', 'default', 'require']) {
            if (typeof value[nested] === 'string') push(value[nested])
          }
        }
      }
    }
    push(manifest && manifest.module)
    push(manifest && manifest.main)
  } catch (_error) {
    /* unreadable manifest: fall through to conventional names */
  }
  for (const conventional of ['lib/index.mjs', 'lib/index.js', 'lib/index.cjs', 'index.js']) {
    push(conventional)
  }
  return entries.filter((file) => exists(file) || options.includeMissing === true)
}

/** Filesystem candidates for `@deepseek-ai/dsh-tools`, most specific first. */
export function defineToolPathCandidates(options = {}) {
  return nodeModulesRoots(options).flatMap((root) =>
    packageEntryCandidates(path.join(root, DSH_TOOLS_ID), options),
  )
}

/**
 * Resolve one package by trying, in order: ESM bare import, createRequire, then
 * the filesystem ladder.  Every attempt is recorded; the result is a value, so
 * callers can degrade instead of crashing the host's import.
 * @returns {Promise<{ok: true, value: any, via: string, tried: string[]} |
 *                    {ok: false, tried: string[]}>}
 */
export async function resolvePackage(ids, pick, options = {}) {
  const tried = []
  const exists = options.exists || existsSync
  const candidates = options.candidates
  // ① ESM bare import: the exact path the host's own loader takes.
  for (const id of ids) {
    try {
      const found = pick(await import(/* webpackIgnore: true */ id))
      if (found) return { ok: true, value: found, via: 'esm:' + id, tried: tried }
      tried.push('esm:' + id + ' (unexpected export shape)')
    } catch (error) {
      tried.push('esm:' + id + ': ' + errLine(error))
    }
  }
  // ② createRequire: covers CJS-only host builds and older resolution layouts.
  try {
    const require = createRequire(import.meta.url)
    for (const id of ids) {
      try {
        const found = pick(require(id))
        if (found) return { ok: true, value: found, via: 'cjs:' + id, tried: tried }
        tried.push('cjs:' + id + ' (unexpected export shape)')
      } catch (error) {
        tried.push('cjs:' + id + ': ' + errLine(error))
      }
    }
  } catch (error) {
    tried.push('createRequire: ' + errLine(error))
  }
  // ③ filesystem ladder (see nodeModulesRoots for why this is not redundant).
  for (const candidate of candidates || []) {
    if (options.candidates === undefined && !exists(candidate)) {
      tried.push(candidate + ' (absent)')
      continue
    }
    try {
      const found = pick(await import(pathToFileURL(candidate).href))
      if (found) return { ok: true, value: found, via: 'fs:' + candidate, tried: tried }
      tried.push(candidate + ' (unexpected export shape)')
    } catch (error) {
      tried.push(candidate + ': ' + errLine(error))
    }
  }
  return { ok: false, tried: tried }
}

/**
 * Resolve the host's `defineTool`.  Returns a result object — never throws.
 * @returns {{ok: true, defineTool: Function, via: string, tried: string[]} |
 *           {ok: false, error: string, tried: string[]}}
 */
export async function resolveDefineTool(options = {}) {
  const result = await resolvePackage([options.specifier || DSH_TOOLS_ID], pickDefineTool, {
    ...options,
    candidates: options.candidates || defineToolPathCandidates(options),
  })
  if (result.ok) {
    return { ok: true, defineTool: result.value, via: result.via, tried: result.tried }
  }
  return {
    ok: false,
    tried: result.tried,
    error:
      'cannot resolve ' + DSH_TOOLS_ID + ' from ' + HERE + '. The host provides this package; ' +
      'if DSH lives somewhere unusual, upgrade DSH or start DSH from its own installation.',
  }
}

/**
 * Optional schemastery for the Config schema.  Failure just means no schema:
 * the host then passes config through unvalidated, which runtimeView tolerates.
 * Both published names are tried because DSH has shipped the scoped and the
 * bare form at different times.
 */
async function resolveSchemastery(options = {}) {
  const candidates = (options.nodeModulesRoots || nodeModulesRoots(options)).flatMap((root) =>
    SCHEMASTERY_IDS.flatMap((id) => packageEntryCandidates(path.join(root, id), options)),
  )
  const result = await resolvePackage(SCHEMASTERY_IDS, pickSchemastery, {
    ...options,
    candidates: options.candidates || candidates,
  })
  if (result.ok) {
    schemasteryResolve.via = result.via
    schemasteryResolve.tried = result.tried
    return result.value
  }
  schemasteryResolve.tried = result.tried
  return undefined
}

const schemasteryResolve = { via: null, tried: [] }

// ────────────────────────────────────────────────────────── root + python
//
// Two independent facts are discovered once:
//   ROOT — the checkout that contains the `atta` package, when there is one.
//          A registry/pip install needs no ROOT at all, so its absence is not
//          an error by itself.
//   PY   — an interpreter that can actually `import atta`.  Verified, not
//          assumed: `python3` on PATH may be a different environment than the
//          one where atta's dependencies (numpy, trimesh) live.

const PYTHON_PROBE = 'import atta, sys; sys.stdout.write(atta.__version__)'
const PYTHON_PROBE_TIMEOUT_MS = 20000

/**
 * Locate the Atta checkout, if the plugin is running from one.
 * Walks up from plugin/lib; a monorepo layout puts atta/ two levels up, while a
 * profile symlink can point anywhere, so both are covered.
 * @returns {{root: string|null, via: string, tried: string[]}}
 */
export function resolveRoot(options = {}) {
  const env = options.env || process.env
  const exists = options.exists || existsSync
  const start = options.here || HERE
  const tried = []
  const isCheckout = (dir) =>
    exists(path.join(dir, 'atta', '__init__.py')) || exists(path.join(dir, 'pyproject.toml'))
  if (env.ATTA_ROOT) {
    const declared = path.resolve(String(env.ATTA_ROOT))
    tried.push('ATTA_ROOT=' + declared)
    if (isCheckout(declared)) return { root: declared, via: 'ATTA_ROOT', tried: tried }
  }
  let dir = start
  for (let level = 0; level < 6; level += 1) {
    tried.push(dir)
    if (exists(path.join(dir, 'atta', '__init__.py'))) return { root: dir, via: 'walk-up', tried: tried }
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  const cwd = options.cwd || process.cwd()
  tried.push('cwd=' + cwd)
  if (exists(path.join(cwd, 'atta', '__init__.py'))) return { root: cwd, via: 'cwd', tried: tried }
  return { root: null, via: 'none', tried: tried }
}

/** Split an ATTA_PYTHON value ("py -3", "/venv/bin/python") into cmd + args. */
function splitInterpreter(value) {
  const raw = String(value || '').trim()
  if (!raw) return null
  if (raw.includes(' ')) {
    const parts = raw.split(/\s+/)
    return { cmd: parts[0], args: parts.slice(1), label: raw }
  }
  return { cmd: raw, args: [], label: raw }
}

/**
 * Candidate interpreters, most intentional first.  A project venv outranks the
 * PATH default because that is where `pip install -e .` normally lands Atta's
 * numeric dependencies.
 * @returns {Array<{cmd: string, args: string[], label: string, source: string}>}
 */
export function pythonCandidates(options = {}) {
  const env = options.env || process.env
  const root = options.root || null
  const isWin = (options.platform || process.platform) === 'win32'
  const candidates = []
  const push = (entry, source) => {
    if (!entry) return
    if (candidates.some((item) => item.label === entry.label)) return
    candidates.push({ cmd: entry.cmd, args: entry.args, label: entry.label, source: source })
  }
  push(splitInterpreter(env.ATTA_PYTHON), 'ATTA_PYTHON')
  if (root) {
    const venvs = ['.venv', 'venv', 'env']
    const relative = isWin ? ['Scripts', 'python.exe'] : ['bin', 'python']
    for (const venv of venvs) {
      const candidate = path.join(root, venv, ...relative)
      push({ cmd: candidate, args: [], label: candidate }, venv)
    }
  }
  push({ cmd: isWin ? 'python.exe' : 'python3', args: [], label: isWin ? 'python.exe' : 'python3' }, 'PATH')
  push({ cmd: 'python', args: [], label: 'python' }, 'PATH')
  if (isWin) push({ cmd: 'py', args: ['-3'], label: 'py -3' }, 'py launcher')
  return candidates
}

/** Environment for the child: make `atta` importable from a checkout. */
export function buildPythonEnv(options = {}) {
  const base = options.env || process.env
  const root = options.root || null
  const workspace = options.workspace || null
  const env = { ...base, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
  if (root) {
    env.ATTA_ROOT = root
    const previous = base.PYTHONPATH
    env.PYTHONPATH = previous ? root + path.delimiter + previous : root
  }
  if (workspace) env.ATTA_HOME = workspace
  return env
}

/**
 * Run one probe command; resolves with captured output, never throws.
 * @returns {Promise<{ok: boolean, code: number|null, stdout: string, stderr: string, error?: string}>}
 */
function probeInterpreter(candidate, env, options = {}) {
  const spawnFn = options.spawnFn || spawn
  const timeoutMs = options.timeoutMs || PYTHON_PROBE_TIMEOUT_MS
  return new Promise((resolve) => {
    let child
    try {
      child = spawnFn(candidate.cmd, [...candidate.args, '-c', PYTHON_PROBE], {
        env: env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (error) {
      resolve({ ok: false, code: null, stdout: '', stderr: '', error: errLine(error) })
      return
    }
    let stdout = ''
    let stderr = ''
    let settled = false
    const finish = (value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(value)
    }
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch (_error) { /* already gone */ }
      finish({ ok: false, code: null, stdout: stdout, stderr: stderr, error: 'probe timed out' })
    }, timeoutMs)
    child.stdout.on('data', (chunk) => { stdout += String(chunk) })
    child.stderr.on('data', (chunk) => { stderr += String(chunk) })
    child.on('error', (error) => {
      finish({ ok: false, code: null, stdout: stdout, stderr: stderr, error: errLine(error) })
    })
    child.on('close', (code) => {
      finish({ ok: code === 0, code: code, stdout: stdout, stderr: stderr })
    })
  })
}

/**
 * Discover an interpreter that can import atta.  Never throws: a total failure
 * returns {ok:false, error} whose text names the knobs and the fix.
 * @returns {Promise<{ok: true, python: {cmd,args,label}, version: string|null, candidates, tried}
 *                  | {ok: false, error: string, tried: string[], candidates}>}
 */
export async function resolvePython(options = {}) {
  const root = options.root === undefined ? resolveRoot(options) : options.root
  const rootDir = root && root.root !== undefined ? root.root : root
  const workspace = options.workspace || null
  const baseEnv = options.env || process.env
  // An explicitly pinned interpreter is AUTHORITATIVE: if the user (or a
  // profile) named one, silently falling back to a different environment would
  // run atta with the wrong dependency set — the exact class of bug that looks
  // like a geometry error. So a pin disables discovery and fails loudly.
  const pinned = String(options.override || '').trim() ||
    String(baseEnv.ATTA_PYTHON || '').trim()
  const candidates = options.candidates || (pinned
    ? pythonCandidates({ ...options, root: rootDir, env: { ...baseEnv, ATTA_PYTHON: pinned } })
        .filter((candidate) => candidate.source === 'ATTA_PYTHON')
    : pythonCandidates({ ...options, root: rootDir, env: baseEnv }))
  const env = buildPythonEnv({ ...options, root: rootDir, workspace: workspace })
  const spawnFn = options.spawnFn || spawn
  const tried = []
  for (const candidate of candidates) {
    const result = await probeInterpreter(candidate, env, { ...options, spawnFn: spawnFn })
    if (result.ok) {
      return {
        ok: true,
        python: { cmd: candidate.cmd, args: candidate.args, label: candidate.label },
        version: String(result.stdout || '').trim() || null,
        via: candidate.source,
        tried: tried,
        candidates: candidates,
      }
    }
    tried.push(candidate.label + ' (' + candidate.source + '): ' + (result.error || pythonErrorLine(result.stderr) || ('exit ' + String(result.code))))
  }
  if (pinned) {
    return {
      ok: false,
      tried: tried,
      candidates: candidates,
      error:
        'the interpreter pinned by ' + (options.override ? 'the plugin `python` config' : 'ATTA_PYTHON') +
        ' cannot `import atta`: ' + tried.join(' / ') + '. ' +
        'Nothing else was tried on purpose — a silent fallback would run Atta with the wrong ' +
        'dependencies. Fix the pin, or unset it to let the plugin auto-detect ' +
        '(venv -> python3 -> python' + (process.platform === 'win32' ? ' -> py -3' : '') + ').',
    }
  }
  const hint = rootDir
    ? 'Run `' + (candidates[0] ? candidates[0].label : 'python3') + ' -m pip install -e .` in "' +
      rootDir + '", or point ATTA_PYTHON at an interpreter that can `import atta`.'
    : 'Install the engine (`pip install atta`) or point ATTA_ROOT at an Atta checkout, then retry; ' +
      'ATTA_PYTHON overrides the interpreter.'
  return {
    ok: false,
    tried: tried,
    candidates: candidates,
    error: 'no Python interpreter can `import atta`. Tried: ' + tried.join(' / ') + '. ' + hint,
  }
}

/** Cached interpreter resolution: probing costs one process spawn. */
const pythonCache = { key: null, result: null }
function cachedPython(rootDir, workspace, override) {
  const key = String(rootDir) + '|' + String(workspace) + '|' +
    String(override || process.env.ATTA_PYTHON || '')
  if (pythonCache.key === key && pythonCache.result) return Promise.resolve(pythonCache.result)
  const pending = resolvePython({ root: rootDir, workspace: workspace, override: override }).then((result) => {
    // Only cache success; a user who installs atta and retries should recover
    // without restarting DSH.
    if (result.ok && pythonCache.key === key) pythonCache.result = result
    if (result.ok) pythonCache.key = key
    return result
  })
  return pending
}

// ────────────────────────────────────────────────────────────── CLI plumbing

function parseJson(text) {
  const raw = String(text || '').trim()
  if (!raw) return null
  try { return JSON.parse(raw) } catch (_error) { /* fall through to slicing */ }
  const first = raw.indexOf('{')
  const last = raw.lastIndexOf('}')
  if (first >= 0 && last > first) {
    try { return JSON.parse(raw.slice(first, last + 1)) } catch (_error) { /* not JSON */ }
  }
  return null
}

function normalizeError(value) {
  if (isRecord(value)) {
    const out = {
      code: String(value.code || 'error'),
      message: String(value.message || 'unknown error'),
      path: value.path === undefined ? null : value.path,
      fix: value.fix === undefined ? null : value.fix,
    }
    if (value.details !== undefined) out.details = value.details
    return out
  }
  return { code: 'error', message: String(value || 'unknown error'), path: null, fix: null }
}

function normalizeEnvelope(name, envelope, fallback) {
  const data = isRecord(envelope) && envelope.data !== undefined ? envelope.data : null
  const failed = (isRecord(envelope) && envelope.ok === false) || Boolean(fallback && fallback.ok === false)
  let error = null
  if (failed) {
    if (isRecord(envelope) && envelope.error !== undefined) error = normalizeError(envelope.error)
    else if (fallback) error = normalizeError({ code: fallback.code || 'error', message: fallback.error })
    else error = normalizeError(null)
  }
  return {
    ok: !failed,
    tool: (isRecord(envelope) && envelope.tool) || name,
    data: data,
    error: error,
    ms: isRecord(envelope) && typeof envelope.ms === 'number' ? envelope.ms : null,
  }
}

function runProcess(python, args, options) {
  const timeoutMs = options.timeoutMs || 120000
  const spawnFn = options.spawnFn || spawn
  return new Promise((resolve) => {
    let child
    try {
      child = spawnFn(python.cmd, [...python.args, ...args], {
        cwd: options.cwd,
        env: options.env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (error) {
      resolve({ ok: false, code: 'spawn', error: errLine(error), stdout: '', stderr: '' })
      return
    }
    let stdout = ''
    let stderr = ''
    let settled = false
    const finish = (value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(value)
    }
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch (_error) { /* already gone */ }
      finish({ ok: false, code: 'timeout', error: 'python -m atta timed out after ' + timeoutMs + 'ms', stdout: stdout, stderr: stderr })
    }, timeoutMs)
    child.stdout.on('data', (chunk) => { stdout += String(chunk) })
    child.stderr.on('data', (chunk) => { stderr += String(chunk) })
    child.on('error', (error) => {
      finish({ ok: false, code: 'spawn', error: errLine(error), stdout: stdout, stderr: stderr })
    })
    child.on('close', (code) => {
      finish({ ok: code === 0, code: code, stdout: stdout, stderr: stderr })
    })
  })
}

/** Per-plugin runtime view: resolved root, workspace, interpreter, config. */
function runtimeView(config) {
  const cfg = isRecord(config) ? config : {}
  const rootResolution = resolveRoot({})
  const rootDir = rootResolution.root
  // The workspace is the user's project (where _atta/ and exports land), NOT
  // the engine checkout: a link:-installed plugin must never write into itself.
  const workspace = path.resolve(
    String(cfg.workspace || process.env.ATTA_HOME || process.cwd()),
  )
  const scale = Number(cfg.timeoutScale)
  return {
    root: rootDir,
    rootVia: rootResolution.via,
    rootTried: rootResolution.tried,
    workspace: workspace,
    // Config wins over the environment so a profile can pin the interpreter
    // without exporting anything; ATTA_PYTHON remains the fallback.
    python: String(cfg.python || process.env.ATTA_PYTHON || '').trim() || null,
    timeoutScale: Number.isFinite(scale) && scale > 0 ? scale : 1,
  }
}

/**
 * Call one Atta tool through the CLI.
 * @returns {Promise<object>} the normalized {ok, tool, data, error, ms} envelope
 */
export async function callTool(name, args, config) {
  const view = runtimeView(config)
  const timeoutMs = Math.round((TOOL_TIMEOUT_MS[name] || 120000) * view.timeoutScale)
  const pythonResult = await cachedPython(view.root, view.workspace, view.python)
  if (!pythonResult.ok) {
    return {
      ok: false,
      tool: name,
      data: null,
      error: normalizeError({
        code: 'python_missing',
        message: pythonResult.error,
        path: 'ATTA_PYTHON',
        fix: view.root
          ? 'export ATTA_PYTHON=/path/to/python && python -m pip install -e "' + view.root + '"'
          : 'pip install atta, or export ATTA_ROOT=/path/to/atta-checkout',
      }),
      ms: null,
    }
  }
  const argsFile = path.join(
    os.tmpdir(),
    'atta-tool-' + String(process.pid) + '-' + String(Date.now()) + '-' +
      Math.random().toString(16).slice(2) + '.json',
  )
  try {
    writeFileSync(argsFile, JSON.stringify(args || {}), 'utf8')
    const result = await runProcess(
      pythonResult.python,
      ['-m', 'atta', '--json', 'tool', name, '--args-file', argsFile],
      {
        timeoutMs: timeoutMs,
        cwd: view.workspace,
        env: buildPythonEnv({ root: view.root, workspace: view.workspace }),
      },
    )
    const parsed = parseJson(result.stdout)
    if (!isRecord(parsed)) {
      const detail = String(result.stdout || result.stderr || '').slice(0, 1200)
      const failure = result.ok
        ? { code: 'bad_json', error: 'CLI stdout is not a JSON envelope' }
        : { code: result.code || 'cli_failed', error: result.error || 'CLI failed' }
      return {
        ok: false,
        tool: name,
        data: null,
        error: normalizeError({
          code: failure.code,
          message: failure.error + ': ' + detail,
          path: 'stdout',
          fix: 'Check that `' + pythonResult.python.label + ' -m atta --json tool ' + name +
            ' --args-file <json>` works in "' + view.workspace + '"',
        }),
        ms: null,
      }
    }
    return normalizeEnvelope(name, parsed, result)
  } finally {
    try { unlinkSync(argsFile) } catch (_error) { /* best effort */ }
  }
}

// ──────────────────────────────────────────────────────────────── images

function collectImages(value, out = []) {
  if (Array.isArray(value)) {
    for (const item of value) collectImages(item, out)
    return out
  }
  if (!isRecord(value)) return out
  for (const [key, item] of Object.entries(value)) {
    if (key === 'images' && Array.isArray(item)) {
      for (const record of item) if (isRecord(record)) out.push(record)
    } else {
      collectImages(item, out)
    }
  }
  return out
}

function resolvePath(candidate, workspace) {
  const raw = String(candidate == null ? '' : candidate)
  if (!raw) return raw
  return path.isAbsolute(raw) ? raw : path.resolve(workspace, raw)
}

async function attachPng(ctx, filePath, name) {
  try {
    const attachments = ctx && typeof ctx.get === 'function' ? ctx.get('attachments') : null
    if (!attachments || typeof attachments.saveImage !== 'function') {
      return { why: 'host has no attachments service' }
    }
    const mediaTypes = (attachments.imageLimits && attachments.imageLimits.mediaTypes) || []
    if (mediaTypes.length && !mediaTypes.includes('image/png')) {
      return { why: 'host imageLimits.mediaTypes excludes image/png' }
    }
    if (!existsSync(filePath)) return { why: 'image missing: ' + filePath }
    const ref = await attachments.saveImage({
      data: readFileSync(filePath),
      mediaType: 'image/png',
      name: String(name || path.basename(filePath)),
    })
    return {
      ref: {
        attachmentId: ref.attachmentId,
        mediaType: ref.mediaType,
        bytes: ref.bytes,
        width: ref.width,
        height: ref.height,
        ...(ref.name === undefined ? {} : { name: ref.name }),
      },
    }
  } catch (error) {
    return { why: 'saveImage failed: ' + errLine(error) }
  }
}

async function attachImages(ctx, records, workspace, max = 3) {
  const refs = []
  const failed = []
  const seen = new Set()
  for (const record of records || []) {
    if (refs.length + failed.length >= max) break
    const raw = record && (record.abs_path || record.path || record.file)
    if (!raw) continue
    const resolved = resolvePath(raw, workspace)
    if (seen.has(resolved)) continue
    seen.add(resolved)
    if (path.extname(resolved).toLowerCase() !== '.png') continue
    const label = record.view ? 'atta-' + String(record.view) + '.png' : 'atta-preview.png'
    const outcome = await attachPng(ctx, resolved, label)
    if (outcome.ref) refs.push(outcome.ref)
    else failed.push(resolved + ' (' + String(outcome.why || 'attach failed') + ')')
  }
  return { refs: refs, failed: failed }
}

// ─────────────────────────────────────────────────────────────── rendering

/** The one place a tool result becomes a content-block payload. */
export function resultEnvelope(result, human, images) {
  const body = { text: JSON.stringify(result.value) + (human ? NL + human : '') }
  if (images && images.length) body.images = images
  return body
}

function renderResult(_args, value) {
  const blocks = [{ type: 'text', text: String((value && value.text) || '') }]
  if (value && value.image) blocks.push({ type: 'image', attachment: value.image })
  if (value && Array.isArray(value.images)) {
    for (const image of value.images) blocks.push({ type: 'image', attachment: image })
  }
  return blocks
}

function humanSummary(name, result, attached) {
  if (!result.ok) {
    const error = result.error || {}
    return [
      'FAILED ' + name + ': ' + String(error.message || 'unknown error'),
      error.fix ? 'fix: ' + String(error.fix) : '',
      error.path ? 'at: ' + String(error.path) : '',
      attached && attached.failed.length ? 'images: ' + attached.failed.join(' | ') : '',
    ].filter(Boolean).join(NL)
  }
  const data = isRecord(result.data) ? result.data : {}
  switch (name) {
    case 'atta_doctor': {
      const status = data.status || data
      return 'doctor ok; ' + JSON.stringify(status).slice(0, 900)
    }
    case 'atta_catalog': {
      const recipes = data.recipes || (data.catalog && data.catalog.recipes)
      const count = Array.isArray(recipes)
        ? recipes.length
        : (isRecord(recipes) ? Object.keys(recipes).length : null)
      return count === null ? 'catalog ok' : 'catalog ok; recipes=' + String(count)
    }
    case 'atta_spec': {
      if (data.ok === false) {
        const errors = (Array.isArray(data.errors) ? data.errors : [])
          .map((item) => String(item.path || '?') + ' ' + String(item.message || ''))
          .slice(0, 6)
        return ['spec rejected', errors.join(' | ')].filter(Boolean).join(NL)
      }
      return 'spec ok' + (data.spec_hash ? '; spec_hash=' + String(data.spec_hash).slice(0, 12) : '')
    }
    case 'atta_model':
      return [
        'model ok; model_id=' + String(data.model_id || '?'),
        Array.isArray(data.parts) ? 'parts=' + String(data.parts.length) : '',
        'next: atta_describe -> atta_verify',
      ].filter(Boolean).join(NL)
    case 'atta_variant': {
      const candidates = data.candidates || data.variants || []
      return 'variant ok; candidates=' + (Array.isArray(candidates) ? String(candidates.length) : '?')
    }
    case 'atta_describe':
      return 'facts: ' + JSON.stringify(data.facts || data).slice(0, 900)
    case 'atta_verify': {
      const issues = data.issues || data.checks || []
      return 'verdict=' + String(data.verdict || '?') +
        ' score=' + String(data.score == null ? '?' : data.score) +
        (Array.isArray(issues) ? ' issues=' + String(issues.length) : '')
    }
    case 'atta_preview':
      return 'preview ok; attached=' + String(attached ? attached.refs.length : 0) +
        (attached && attached.failed.length ? '; failed=' + String(attached.failed.length) : '')
    case 'atta_deliver':
      return 'deliver status=' + String(data.status || '?') +
        (Array.isArray(data.files) ? ' files=' + String(data.files.length) : '')
    case 'atta_run': {
      const results = Array.isArray(data.results) ? data.results : []
      const lines = ['run ok=' + String(data.ok) + ' n=' +
        String(data.n_run == null ? results.length : data.n_run)]
      for (const item of results.slice(0, 12)) {
        lines.push('- ' + String(item.name || item.tool || item.op || '?') + ': ' + (item.ok ? 'ok' : 'FAILED'))
      }
      return lines.join(NL)
    }
    default:
      return ''
  }
}

// ──────────────────────────────────────────────────────────── tool specs
//
// Declared as data, one entry per verb, so the surface is auditable in one
// screen and the Python engine stays the single source of truth for behaviour.
// Schemas are the model contract: they mirror atta/agent/tools.py.

const JSON_OBJECT = { type: 'object', additionalProperties: true }

function toolSpecs() {
  return [
    {
      name: 'atta_doctor',
      description:
        '[Atta v' + VERSION + '] Engine self-check: version, Blender availability, recipe/spec/feature counts, current model. Run this before modeling; fix the environment first.',
      parameters: {
        deep: { type: 'boolean', description: 'true returns a deeper probe (may be slower)' },
      },
    },
    {
      name: 'atta_catalog',
      description:
        '[Atta v' + VERSION + '] Capability catalog: recipe ids and parameter schemas, feature/constraint schemas, recommended workflow. Call it instead of guessing field names.',
      parameters: {
        verbose: { type: 'boolean', description: 'true returns the full feature/parameter documentation' },
      },
    },
    {
      name: 'atta_spec',
      description:
        '[Atta v' + VERSION + '] Spec v3 entry: validate (no geometry), propose (deterministic design defaults + assumption_ledger + unresolved), compile (resolved params/features), schema (field documentation). model.kind may be recipe, graph, or assembly.',
      parameters: {
        op: { type: 'string', enum: ['validate', 'propose', 'compile', 'schema'], default: 'validate', description: 'validate | propose | compile | schema' },
        spec: { ...JSON_OBJECT, description: 'Atta Spec v3 JSON' },
        assume: { type: 'boolean', description: 'validate with design defaults filled in' },
      },
    },
    {
      name: 'atta_model',
      description:
        '[Atta v' + VERSION + '] Build: recipe + params, Spec v3, a feature graph, or a multi-part assembly (model.kind=assembly with parts+mates); patches refine an existing build. Returns model_id + facts. Describe, then verify; never declare done from volume alone.',
      parameters: {
        recipe: { type: 'string', description: 'recipe id, e.g. product.mug' },
        spec: { ...JSON_OBJECT, description: 'Atta Spec v3 JSON' },
        params: { ...JSON_OBJECT, description: 'recipe parameter overrides (mm / deg)' },
        patches: { ...JSON_OBJECT, description: 'refine patch applied to the current spec/params' },
        preset: { type: 'string', description: 'recipe preset (default nominal)' },
        quality: { type: 'string', enum: ['draft', 'review', 'deliver'], default: 'draft', description: 'draft/review/deliver resolution profile' },
        name: { type: 'string', description: 'model name' },
        material: { type: 'string', description: 'material name, e.g. ceramic' },
        finish: { ...JSON_OBJECT, description: '{"bevel_mm":0.8,"smooth_deg":35}' },
      },
    },
    {
      name: 'atta_variant',
      description:
        '[Atta v' + VERSION + '] Parameter sampling or solving: without targets it samples and ranks candidates; with targets/solve=true it builds real draft meshes, solves constrained targets, and returns a feasible solution plus sensitivity, closest-achievable diagnostics and an optional Pareto front. base may be a recipe model or an assembly model (part params are addressed as part.param).',
      parameters: {
        recipe: { type: 'string', description: 'recipe id; use base for fit/solve' },
        base: { type: 'string', description: 'reference model id (recipe or assembly)' },
        count: { type: 'integer', default: 6, description: 'initial sample count' },
        ranges: { ...JSON_OBJECT, description: '{"height":[80,120]} or {"height":{"min":80,"max":120}}; omitted in solve mode uses full parameter bounds' },
        strategy: { type: 'string', default: 'lhs', description: 'lhs|auto|compass|nelder-mead|differential-evolution|local|pareto' },
        seed: { type: 'integer', default: 42, description: 'random seed' },
        objective: { ...JSON_OBJECT, description: '{"volume_mm3":"min"} or [{metric,direction,weight}]' },
        targets: { ...JSON_OBJECT, description: '{"bbox_z":100,"volume_mm3":[250000,320000],"min_wall_mm":{"min":2.5}}' },
        constraints: { ...JSON_OBJECT, description: 'same keys as atta_verify constraints; converted to solver targets' },
        max_evaluations: { type: 'integer', description: 'solver evaluation budget' },
        tolerance: { type: 'number', default: 0.000001, description: 'normalized feasibility tolerance' },
        initial: { ...JSON_OBJECT, description: 'starting parameter values' },
        pareto: { type: 'boolean', description: 'return a Pareto front' },
        free: { type: 'array', items: { type: 'string' }, description: 'solve only these parameters' },
        fixed: { type: 'array', items: { type: 'string' }, description: 'freeze these parameters' },
        solve: { type: 'boolean', description: 'force solve mode' },
        quality: { type: 'string', enum: ['draft', 'review', 'deliver'], default: 'draft', description: 'candidate evaluation quality' },
      },
    },
    {
      name: 'atta_describe',
      description:
        '[Atta v' + VERSION + '] Measured structure + text perception: bbox/volume/topology/wall/curvature and probes. perceive=true returns low-power ASCII silhouettes, sections and shape traits without rendering; trace=true returns construction steps; compare_to=<model_id> returns silhouette IoU and +/- diff. Read these before looking at images.',
      parameters: {
        model_id: { type: 'string', description: 'model id; current model when omitted' },
        probe: { description: 'probe name, array, or {"op":"raycast","axis":"z","res":[32,32]}', oneOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }, JSON_OBJECT] },
        wall: { type: 'boolean', description: 'run wall-thickness ray sampling (approx)' },
        curvature: { type: 'boolean', description: 'report curvature statistics' },
        perceive: { type: 'boolean', description: 'return the text/ASCII perception pack (no render)' },
        views: { type: 'array', items: { type: 'string' }, description: 'front/top/right/left/back/iso (default front,top,right)' },
        res: { type: 'integer', description: 'ASCII resolution 8..64 (default 24)' },
        sections: { description: 'Z fraction or array of fractions (default 0.5)', oneOf: [{ type: 'number' }, { type: 'array', items: { type: 'number' } }] },
        compare_to: { type: 'string', description: 'reference model id for a silhouette diff' },
        trace: { type: 'boolean', description: 'return recipe/graph construction steps with per-step volume' },
      },
    },
    {
      name: 'atta_verify',
      description:
        '[Atta v' + VERSION + '] Strict gate: topology + bbox/volume windows + clearance/interference + wall/dihedral/curvature; for assemblies also per-part solids, mate residuals, DOF and broadphase collisions. Verdict is pass | degraded | fail. pass only means verdict==pass.',
      parameters: {
        model_id: { type: 'string', description: 'model id; current model when omitted' },
        level: { type: 'string', enum: ['basic', 'standard', 'strict'], default: 'standard', description: 'gate level' },
        constraints: { ...JSON_OBJECT, description: 'bbox/volume_mm3/min_wall_mm/genus/clearance/interference/envelope/render' },
        reference: { type: 'string', description: 'reference model id for deviation/clearance' },
      },
    },
    {
      name: 'atta_preview',
      description:
        '[Atta v' + VERSION + '] Judgement images and visual analysis: multi-view PNG plus facts and a render receipt. mode=beauty keeps the original look; studio/catalog/dark/clay are studio-lit presentation renders with per-part materials; diagnostic renders normal/depth/id/curvature/deviation. Beauty images carry exposure/Zone/CIELAB/contrast/chroma/edge/key-fill statistics. Images and analysis inform judgement; they are not the geometry verdict.',
      parameters: {
        model_id: { type: 'string', description: 'model id; current model when omitted' },
        views: { type: 'array', items: { type: 'string' }, description: 'iso/front/top/left/right/back/hero/hero_l/hero_r' },
        res: { type: 'integer', description: 'longest-edge pixels' },
        samples: { type: 'integer', description: 'sample count' },
        quality: { type: 'string', enum: ['draft', 'review', 'deliver'], description: 'resolution profile' },
        mode: { type: 'string', enum: ['beauty', 'studio', 'catalog', 'dark', 'clay', 'diagnostic'], description: 'beauty/studio/catalog/dark/clay/diagnostic' },
        lighting: { type: 'string', enum: ['studio3', 'studio', 'catalog', 'dark', 'clay', 'none'], description: 'lighting preset' },
        background: { type: 'string', enum: ['transparent', 'dark', 'white', 'gray', 'gradient'], description: 'background' },
        ground: { type: 'boolean', description: 'ground plane and contact shadow' },
        lens_mm: { type: 'number', description: 'camera focal length in mm' },
        exposure: { type: 'number', description: 'exposure compensation in stops' },
        look: { type: 'string', description: "AgX look, e.g. 'AgX - Medium High Contrast'" },
        ortho: { type: 'boolean', description: 'orthographic camera' },
        distance_factor: { type: 'number', description: 'camera distance multiplier (<1 closer)' },
        materials: { oneOf: [{ type: 'string' }, JSON_OBJECT], description: 'global material or {part_id: material, "*": material}' },
        passes: { type: 'array', items: { type: 'string' }, description: 'beauty/normal/depth/id/curvature/deviation' },
        reference: { type: 'string', description: 'reference model id for the deviation pass' },
        deviation_range_mm: { type: 'number', description: 'deviation colour scale in mm (default p95*2)' },
        focus: { type: 'string', description: 'part id to frame (macro/detail view)' },
        focus_padding: { type: 'number', description: 'focus framing padding ratio (default 0.3)' },
        aspect: { oneOf: [{ type: 'string', enum: ['auto', 'square'] }, { type: 'number' }], description: 'frame aspect; auto follows the projected bbox' },
        outdir: { type: 'string', description: 'output directory (default _atta/out)' },
      },
    },
    {
      name: 'atta_deliver',
      description:
        '[Atta v' + VERSION + '] Delivery: verify first; fail blocks, degraded needs allow_degraded=true. Exports GLB/OBJ/STL plus a manifest with sha256 and the verify receipt. Assembly models export a named-part GLB plus per-part OBJ/STL and an assembly receipt.',
      parameters: {
        model_id: { type: 'string', description: 'model id; current model when omitted' },
        formats: { type: 'array', items: { type: 'string' }, description: 'glb/obj/stl/blend/step; default glb,obj' },
        outdir: { type: 'string', description: 'output directory (default _atta/out)' },
        allow_degraded: { type: 'boolean', description: 'permit delivery on degraded' },
        name: { type: 'string', description: 'export filename stem' },
      },
    },
    {
      name: 'atta_run',
      description:
        '[Atta v' + VERSION + '] One call, several steps (model -> describe -> verify -> preview -> deliver). Later steps can use {"$prev":"model_id"} or {"$ref":"model_id"} in args. Nested atta_run is not supported. Stops on first error by default.',
      parameters: {
        steps: { type: 'array', items: { ...JSON_OBJECT }, description: '[{"tool":"atta_model","args":{...}},{"tool":"atta_verify","args":{"model_id":{"$prev":"model_id"}}}]' },
        stop_on_error: { type: 'boolean', default: true, description: 'stop on first error (default true)' },
      },
    },
  ]
}

// ──────────────────────────────────────────────────────── bundled skill
//
// Optional capability: a missing or malformed SKILL.md must never affect the
// tool surface, so every read is cached and returns null on failure.

const SKILL_DIR = path.resolve(HERE, '..', 'skills', 'atta')
const SKILL_PROVIDER = 'atta'
const SKILL_RANK = 600
let skillCache

export function parseFrontmatter(raw) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(String(raw || ''))
  if (!match) return { meta: {}, body: String(raw || '') }
  const meta = {}
  let key = ''
  for (const line of match[1].split(/\r?\n/)) {
    const pair = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line)
    if (pair) {
      key = pair[1]
      let value = pair[2].trim()
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1)
      }
      meta[key] = value
      continue
    }
    // YAML folded/continuation lines belong to the previous key.
    if (key && /^\s+\S/.test(line)) meta[key] = (meta[key] ? meta[key] + ' ' : '') + line.trim()
  }
  return { meta: meta, body: String(raw || '').slice(match[0].length) }
}

export function readSkill(options = {}) {
  if (skillCache !== undefined) return skillCache
  try {
    const dir = options.dir || SKILL_DIR
    const file = path.join(dir, 'SKILL.md')
    const { meta, body } = parseFrontmatter(readFileSync(file, 'utf8'))
    const name = String(meta.name || '').trim()
    const description = String(meta.description || '').trim()
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name) || !description) {
      skillCache = null
      return null
    }
    skillCache = { name: name, description: description, content: body, file: file, dir: dir }
    return skillCache
  } catch (_error) {
    skillCache = null
    return null
  }
}

export function createSkillProvider(options = {}) {
  const skill = readSkill(options)
  if (!skill) return null
  const resourceBase = { kind: 'directory', path: skill.dir.split(path.sep).join('/') }
  const candidate = {
    name: skill.name,
    description: skill.description,
    invocation: { modelInvocable: true, userInvocable: true },
    source: 'bundled',
    provider: SKILL_PROVIDER,
    rank: SKILL_RANK,
    resourceBase: resourceBase,
    path: skill.file,
    locator: skill.file,
  }
  // list()/get() MUST be async: the host's registry calls .then() on them when
  // the caller passes an abort signal, and a synchronous return is silently
  // skipped as "promise.then is not a function".
  return {
    name: SKILL_PROVIDER,
    list: async () => [candidate],
    get: async () => ({ ...candidate, content: skill.content }),
  }
}

function warn(ctx, message) {
  try {
    ctx.logger?.warn?.('[atta] ' + message)
  } catch (_error) { /* logging must never break loading */ }
}

function info(ctx, message) {
  try {
    ctx.logger?.info?.('[atta] ' + message)
  } catch (_error) { /* ignore */ }
}

function registerSkill(ctx) {
  let provider
  try {
    provider = createSkillProvider()
  } catch (error) {
    warn(ctx, 'bundled skill not registered: ' + errLine(error))
    return
  }
  if (!provider) {
    warn(ctx, 'bundled skill not registered: skills/atta/SKILL.md missing or invalid frontmatter (tools unaffected)')
    return
  }
  const mount = (scope) => {
    try {
      scope.skills.registerProvider(() => provider)
    } catch (error) {
      warn(ctx, 'skill registration failed: ' + errLine(error) + ' (tools unaffected)')
    }
  }
  try {
    if (typeof ctx.inject === 'function') ctx.inject(['skills'], mount)
    else if (ctx.skills && typeof ctx.skills.registerProvider === 'function') mount(ctx)
  } catch (error) {
    warn(ctx, 'skill mount failed: ' + errLine(error) + ' (tools unaffected)')
  }
}

// ────────────────────────────────────────────────────────────────── apply

// Import-time resolution, deliberately wrapped so it CANNOT throw.
//
// This is the single most important lesson from the reference plugin's
// changelog: an `await resolve()` at module scope is fine, but only inside a
// try/catch that turns failure into a degraded value.  The old revision of this
// file did `const defineTool = await resolveDefineTool()` where the resolver
// threw — so a resolvable-looking environment could make the whole plugin
// vanish, with the real error swallowed by the host's one-line import message.
//
// Top-level await is used (rather than deferring to apply) because cordis calls
// apply synchronously: an async ladder inside apply would either register
// nothing or register after the fiber judgement.
let HOST_API
try {
  HOST_API = await resolveDefineTool()
} catch (error) {
  HOST_API = {
    ok: false,
    tried: ['unexpected resolver failure'],
    error: 'defineTool resolution crashed unexpectedly: ' + errLine(error),
  }
}
// schemastery is optional: it only shapes the Config schema, so a failure must
// downgrade the schema, never the plugin.
let schema
try {
  schema = await resolveSchemastery()
} catch (_error) {
  schema = undefined
}

/**
 * Plugin config.  When schemastery is unavailable this is `undefined` and the
 * host passes the raw config through; `runtimeView` reads it defensively either
 * way (a host may also skip applying schema defaults, so never assume them).
 */
export const Config = schema
  ? schema.object({
      python: schema.string().default(''),
      workspace: schema.string().default(''),
      timeoutScale: schema.number().default(1),
    })
  : undefined

/** Build one host-ready tool definition from a spec + a live host defineTool. */
export function buildTool(defineTool, spec, ctx, config) {
  return defineTool({
    name: spec.name,
    description: spec.description,
    parameters: spec.parameters,
    output: { schema: { type: 'object', additionalProperties: true }, render: renderResult },
    isConcurrencySafe: () => CONCURRENT[spec.name] !== false,
    timeoutMs: TOOL_TIMEOUT_MS[spec.name],
    async execute(args) {
      const view = runtimeView(config)
      let result
      try {
        result = await callTool(spec.name, args, config)
      } catch (error) {
        // A thrown tool kills the call AND looks like a plugin bug.  Every
        // failure becomes a value instead.
        result = {
          ok: false,
          tool: spec.name,
          data: null,
          error: normalizeError({ code: 'plugin_error', message: errLine(error), path: null, fix: 'run atta_doctor; if it also fails, check ATTA_PYTHON' }),
          ms: null,
        }
      }
      let attached = { refs: [], failed: [] }
      try {
        const records = collectImages(result.data)
        if (records.length) attached = await attachImages(ctx, records, view.workspace, 3)
      } catch (error) {
        attached = { refs: [], failed: ['image attach failed: ' + errLine(error)] }
      }
      // Exit gate: sanitize once, then note any coercion in the receipt.
      const sanitized = losslessSanitize(result)
      if (sanitized.fixes.length) {
        const note = '[lossless] ' + String(sanitized.fixes.length) +
          ' non-lossless value(s) coerced at the tool boundary: ' +
          sanitized.fixes.slice(0, 6).join(' | ') + (sanitized.fixes.length > 6 ? ' ...' : '')
        warn(ctx, note)
        if (isRecord(sanitized.value)) sanitized.value.__losslessNote = note
      }
      return resultEnvelope(sanitized, humanSummary(spec.name, sanitized.value, attached), attached.refs)
    },
  })
}

/** The one tool registered when something essential could not be resolved. */
function buildDiagnostic(lines, name) {
  const text = lines.join(NL)
  return {
    name: name || DIAGNOSTIC_TOOL,
    description:
      '[Atta v' + VERSION + '] Diagnostic only: the Atta plugin loaded in DEGRADED mode and the ' +
      'atta_* modeling tools are NOT registered. Calling it reports why and how to fix it.',
    parameters: { note: { type: 'string', description: 'ignored; any value just returns the diagnostic' } },
    output: { schema: { type: 'object', additionalProperties: true }, render: renderResult },
    isConcurrencySafe: () => true,
    timeoutMs: DIAGNOSTIC_TIMEOUT_MS,
    async execute() {
      return { text: text }
    },
  }
}

export { PLUGIN_NAME as name }
export const inject = ['tools']

/**
 * Plugin entry.  Cordis calls apply(ctx, config) after a successful import;
 * nothing here may throw, because a throw during apply disposes the fiber and
 * the tools disappear with only a one-line host error.
 *
 * @param ctx host context (needs ctx.tools.register and ctx.effect)
 * @param config plugin config (optional; validated schema when schemastery resolved)
 * @param deps test seam: { defineTool } overrides resolution
 */
export function apply(ctx, config, deps) {
  const diagnostic = (lines) => {
    try {
      const definition = buildDiagnostic(lines)
      ctx.effect(
        () => ctx.tools.register(definition),
        PLUGIN_NAME + ': degraded diagnostic',
      )
      warn(ctx, lines[0])
    } catch (error) {
      // Absolute last resort: even registering the diagnostic failed.  Say so
      // in the log rather than throwing out of apply.
      warn(ctx, 'degraded diagnostic could not be registered: ' + errLine(error))
    }
  }

  if (!ctx || typeof ctx.tools !== 'object' || ctx.tools === null) {
    warn(ctx, 'no ctx.tools service; tools not registered')
    return
  }
  if (typeof ctx.effect !== 'function') {
    diagnostic([
      'Atta plugin: DEGRADED. ctx.effect is missing, so tool registration cannot be ' +
        'scoped to this plugin fiber (a recent cordis/DSH is required).',
      'Fix: upgrade DSH. Tools were NOT registered on purpose: registering them ' +
        'without an effect would leak them past plugin disposal.',
    ])
    return
  }

  // ── duplicate-install guard ────────────────────────────────────────────
  // The documented failure mode for this plugin family is being configured
  // twice (bundle layer + a runtime injector).  Two fibers then register the
  // same names and the host rejects the second registration inside apply,
  // which looks exactly like "the plugin vanished".  Detect it and explain.
  let already = null
  try {
    if (typeof ctx.tools.get === 'function') {
      for (const name of TOOL_NAMES) {
        if (ctx.tools.get(name) !== undefined) {
          already = name
          break
        }
      }
    }
  } catch (_error) {
    /* a host without .get() cannot be checked; proceed */
  }
  if (already) {
    diagnostic([
      'Atta plugin: ALREADY LOADED, so this second copy registered nothing.',
      'First conflicting tool: ' + already,
      'Cause: @atta/dsh-atta is listed in the profile twice (for example both in ' +
        'dsh.profile.bundles and in a runtime injector registry).',
      'Fix: keep exactly one installation path. `dsh plugin remove @atta/dsh-atta` then ' +
        '`dsh plugin add <path>` re-registers it once. Remove any injector entry that also ' +
        'points at this package.',
    ])
    return
  }

  // ── host API ───────────────────────────────────────────────────────────
  // HOST_API was resolved once at import time with top-level await (see the
  // bottom of this file): a `catch` there, not a throw, so importing this
  // module cannot fail the host loader.  `deps` is the test seam.
  const host = deps && deps.defineTool
    ? { ok: true, defineTool: deps.defineTool, via: 'injected', tried: [] }
    : HOST_API
  if (!host.ok) {
    diagnostic([
      'Atta plugin: DEGRADED. The atta_* modeling tools are NOT registered.',
      'Cause: ' + String(host.error),
      'Tried: ' + (host.tried && host.tried.length ? host.tried.join(' / ') : '(nothing)'),
      'This is a readable failure on purpose: a plugin that throws at import time ' +
        'disappears from the tool list and the host only reports "failed to import".',
      'Fix: run DSH from its own installation (its node_modules must provide ' +
        DSH_TOOLS_ID + '), then restart DSH.',
    ])
    // Skills are independent of the tool surface: keep them available.
    registerSkill(ctx)
    return
  }

  // ── tool surface ───────────────────────────────────────────────────────
  const specs = toolSpecs()
  let registered = 0
  for (const spec of specs) {
    try {
      const definition = buildTool(host.defineTool, spec, ctx, config)
      // ctx.effect ties the registration to this plugin fiber: cordis disposes
      // effects in reverse order, so the last tool registered is unregistered
      // first and the surface never half-exists.
      ctx.effect(
        () => ctx.tools.register(definition),
        PLUGIN_NAME + ': tool ' + spec.name,
      )
      registered += 1
    } catch (error) {
      warn(ctx, 'tool ' + spec.name + ' not registered: ' + errLine(error))
    }
  }
  if (registered === 0) {
    diagnostic([
      'Atta plugin: DEGRADED. defineTool resolved via ' + String(host.via) +
        ', but no tool could be registered (see the [atta] lines in the DSH log).',
      'Fix: this is normally a host/plugin API version mismatch; upgrade DSH, then restart.',
    ])
    registerSkill(ctx)
    return
  }

  registerSkill(ctx)
  const view = runtimeView(config)
  info(
    ctx,
    'v' + VERSION + ' loaded; tools=' + String(registered) + '/' + String(specs.length) +
      '; host=' + String(host.via) + '; root=' + String(view.root || '(pip-installed)') +
      '; workspace=' + view.workspace,
  )
}

// ───────────────────────────────────────────────────────────── test seams
//
// tests/smoke.mjs imports these directly: the degrade path, the lossless gate
// and interpreter discovery are verified WITHOUT a running DSH host.

export const __internals = {
  VERSION: VERSION,
  PLUGIN_NAME: PLUGIN_NAME,
  DIAGNOSTIC_TOOL: DIAGNOSTIC_TOOL,
  HERE: HERE,
  TOOL_NAMES: TOOL_NAMES,
  TOOL_TIMEOUT_MS: TOOL_TIMEOUT_MS,
  CONCURRENT: CONCURRENT,
  SKILL_DIR: SKILL_DIR,
  losslessSanitize: losslessSanitize,
  sanitize: sanitize,
  resolveDefineTool: resolveDefineTool,
  resolvePackage: resolvePackage,
  packageEntryCandidates: packageEntryCandidates,
  nodeModulesRoots: nodeModulesRoots,
  defineToolPathCandidates: defineToolPathCandidates,
  resolveSchemastery: resolveSchemastery,
  resolveRoot: resolveRoot,
  resolvePython: resolvePython,
  pythonCandidates: pythonCandidates,
  buildPythonEnv: buildPythonEnv,
  parseJson: parseJson,
  pythonErrorLine: pythonErrorLine,
  normalizeError: normalizeError,
  normalizeEnvelope: normalizeEnvelope,
  callTool: callTool,
  collectImages: collectImages,
  resultEnvelope: resultEnvelope,
  toolSpecs: toolSpecs,
  buildTool: buildTool,
  buildDiagnostic: buildDiagnostic,
  readSkill: readSkill,
  createSkillProvider: createSkillProvider,
  parseFrontmatter: parseFrontmatter,
  runtimeView: runtimeView,
  HOST_API: HOST_API,
  schemaResolve: { have: Boolean(schema), via: schemasteryResolve.via, tried: schemasteryResolve.tried },
  resetSkillCache: () => { skillCache = undefined },
  resetPythonCache: () => { pythonCache.key = null; pythonCache.result = null },
}
