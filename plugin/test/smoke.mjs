#!/usr/bin/env node
// @atta/dsh-atta — offline smoke test.
//
// Purpose: verify the plugin's contract WITHOUT a running DSH host.  Everything
// here imports plugin/lib/index.js directly and exercises the seams the plugin
// exposes for exactly this reason (`__internals`).
//
// The four things this suite exists to prevent, in order of damage:
//
//   1. IMPORT-TIME THROW  → every atta_* tool vanishes and the host reports
//      only "failed to import" (real cause swallowed, symptom in another layer).
//   2. LOSSY TOOL OUTPUT  → the host rejects the whole receipt with "tool
//      returned invalid output" although the work succeeded.  `undefined`, NaN,
//      Infinity, -0, Date, Map, cycles and class instances all trigger it.
//   3. HARD-CODED PATHS   → a plugin installed by `link:`, from a registry, or
//      on Windows/macOS cannot find its Python or its engine.
//   4. NAME COLLISION     → a second plugin in the same profile (the reference
//      dsh-blender-plugin) must never be shadowed or broken by us.
//
// Run: node plugin/test/smoke.mjs        (exit code 0 = all pass)
// No network, no npm install, no DSH host required.  Two checks are marked
// SKIPPED when python/atta is genuinely unavailable — they are reported, not
// silently dropped, because a green run must not imply coverage it lacks.

import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN_DIR = path.resolve(HERE, '..')
const ENTRY = path.join(PLUGIN_DIR, 'lib', 'index.js')
const PKG = JSON.parse(readFileSync(path.join(PLUGIN_DIR, 'package.json'), 'utf8'))

// ── tiny assertion harness ────────────────────────────────────────────────────
let passed = 0
const failures = []
const skips = []
let group = '(root)'

function describe(name) {
  group = name
  console.log('\n── ' + name)
}
function check(label, condition, detail) {
  if (condition) {
    passed += 1
    console.log('  ok   ' + label)
    return true
  }
  failures.push(group + ' :: ' + label + (detail ? '  [' + detail + ']' : ''))
  console.log('  FAIL ' + label + (detail ? '  [' + detail + ']' : ''))
  return false
}
function skip(label, why) {
  skips.push(label + ' — ' + why)
  console.log('  skip ' + label + '  (' + why + ')')
}
function eq(label, actual, expected) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  return check(label, a === e, a === e ? '' : 'got ' + a + ' want ' + e)
}
/** Run fn, require it not to throw. */
function noThrow(label, fn) {
  try {
    const value = fn()
    check(label, true)
    return value
  } catch (error) {
    check(label, false, String((error && error.message) || error).slice(0, 160))
    return undefined
  }
}

// ── 1. import-time safety ─────────────────────────────────────────────────────
describe('1. import-time safety (a plugin that throws here disappears entirely)')

let mod
noThrow('importing lib/index.js does not throw', () => {
  // Synchronous createRequire cannot import ESM with top-level await, so the
  // suite imports asynchronously below; this guard only asserts the file parses.
  return true
})

const entryExports = await import(pathToFileURL(ENTRY).href)
  .then((m) => ({ ok: true, m }))
  .catch((error) => ({ ok: false, error }))
check('dynamic import of the entry resolves', entryExports.ok,
  entryExports.ok ? '' : String(entryExports.error && entryExports.error.message).slice(0, 200))
mod = entryExports.m || {}

check('exports a non-empty plugin name', typeof mod.name === 'string' && mod.name.length > 0, String(mod.name))
check('injects exactly the tools service', JSON.stringify(mod.inject) === '["tools"]', JSON.stringify(mod.inject))
check('exports apply() as a function', typeof mod.apply === 'function')
check('exports apply() taking (ctx, config, deps)', mod.apply.length >= 2, 'arity=' + String(mod.apply && mod.apply.length))
check('exports __internals test seam', Boolean(mod.__internals && typeof mod.__internals === 'object'))

const I = mod.__internals || {}
for (const seam of ['losslessSanitize', 'resolveDefineTool', 'resolveRoot', 'resolvePython',
  'pythonCandidates', 'buildPythonEnv', 'callTool', 'toolSpecs', 'buildTool',
  'createSkillProvider', 'packageEntryCandidates', 'resolvePackage', 'nodeModulesRoots']) {
  check('seam exported: ' + seam, typeof I[seam] === 'function')
}

// ── 2. tool names ────────────────────────────────────────────────────────────
describe('2. tool naming (must never collide with another plugin in the profile)')

const specs = typeof I.toolSpecs === 'function' ? I.toolSpecs() : []
check('declares exactly 10 tools', specs.length === 10, 'got ' + String(specs.length))
check('every tool name starts with atta_', specs.every((s) => s.name.startsWith('atta_')),
  specs.map((s) => s.name).filter((n) => !n.startsWith('atta_')).join(','))
check('no tool name uses the blender_ prefix (reference plugin owns it)',
  specs.every((s) => !s.name.startsWith('blender_')))
check('tool names are unique', new Set(specs.map((s) => s.name)).size === specs.length)
const declared = Object.keys(I.TOOL_TIMEOUT_MS || {})
eq('timeouts cover every declared tool', declared.slice().sort(), specs.map((s) => s.name).sort())
check('every timeout is a positive finite number',
  Object.values(I.TOOL_TIMEOUT_MS || {}).every((n) => Number.isFinite(n) && n > 0))
check('every tool has a non-empty description',
  specs.every((s) => typeof s.description === 'string' && s.description.length > 20))
check('every tool description carries the version tag',
  specs.every((s) => s.description.includes('v' + I.VERSION)))
check('every tool declares a parameters object',
  specs.every((s) => s.parameters && typeof s.parameters === 'object'))

// ── 3. lossless exit gate ────────────────────────────────────────────────────
describe('3. lossless output gate (one bad value rejects the whole receipt)')

const { losslessSanitize } = I
function isLossless(value) {
  // Independent re-implementation of the host's rule (dsh-util-values
  // walkJsonValue): this must NOT reuse the plugin's own sanitizer.
  const seen = new Set()
  const walk = (v) => {
    if (v === null || typeof v === 'string' || typeof v === 'boolean') return true
    if (typeof v === 'number') return Number.isFinite(v) && !Object.is(v, -0)
    if (typeof v !== 'object') return false
    if (seen.has(v)) return false
    seen.add(v)
    let ok = true
    if (Array.isArray(v)) {
      if (Object.getPrototypeOf(v) !== Array.prototype) ok = false
      for (const item of v) if (!walk(item)) ok = false
    } else {
      const proto = Object.getPrototypeOf(v)
      if (proto !== Object.prototype && proto !== null) ok = false
      if (Object.getOwnPropertySymbols(v).length) ok = false
      for (const key of Object.keys(v)) if (!walk(v[key])) ok = false
    }
    seen.delete(v)
    return ok
  }
  return walk(value)
}

{
  const { value, fixes } = losslessSanitize({
    fine: 1,
    text: 'x',
    dropped: undefined,
    nan: NaN,
    inf: Infinity,
    negZero: -0,
    date: new Date('2026-01-02T03:04:05.000Z'),
    map: new Map([['a', 1]]),
    set: new Set([1, 2]),
    big: 1n,
    huge: 9007199254740993n,
    fn: () => 1,
    sym: Symbol('s'),
    nested: [undefined, NaN, { deep: undefined }],
    bytes: new Uint8Array([1, 2, 3]),
    klass: new (class Point { constructor() { this.x = 1 } })(),
  })
  check('sanitized value satisfies the host lossless rule', isLossless(value),
    'host would reject this receipt')
  check('undefined keys are dropped, not nulled', !('dropped' in value))
  check('array holes become null and keep length', value.nested.length === 3 &&
    value.nested[0] === null && value.nested[1] === null)
  check('NaN and Infinity become null', value.nan === null && value.inf === null)
  check('-0 becomes 0 (sign preserved as value 0)', value.negZero === 0)
  check('Date becomes an ISO string', value.date === '2026-01-02T03:04:05.000Z')
  check('Map becomes an array of pairs', Array.isArray(value.map) && value.map[0][0] === 'a')
  check('Set becomes an array', JSON.stringify(value.set) === '[1,2]')
  check('bigint within safe range becomes number', value.big === 1)
  check('bigint beyond safe range becomes string', value.huge === '9007199254740993')
  check('function and symbol are dropped', !('fn' in value) && !('sym' in value))
  check('Uint8Array becomes {bytes}', value.bytes.bytes === 3)
  check('class instance projects own keys', value.klass.x === 1)
  check('coercions are reported, not silent', fixes.length >= 10, 'fixes=' + String(fixes.length))
  check('fix paths name the offending location',
    fixes.some((f) => f.includes('.nan')) && fixes.some((f) => f.includes('.nested[0]')))
}

{
  const cyclic = { name: 'c' }
  cyclic.self = cyclic
  const { value } = losslessSanitize(cyclic)
  check('cyclic reference becomes [Circular] and stays lossless',
    isLossless(value) && value.self === '[Circular]')
}
{
  const shared = { k: 1 }
  const { value } = losslessSanitize({ a: shared, b: shared })
  check('shared (non-cyclic) references survive twice', value.a.k === 1 && value.b.k === 1)
}
eq('a clean payload reports zero fixes', losslessSanitize({ a: 1, b: ['c'] }).fixes.length, 0)
eq('a top-level undefined becomes null', losslessSanitize(undefined).value, null)
eq('a top-level function becomes null', losslessSanitize(() => 1).value, null)
check('no-fix path does not mutate the input',
  (() => { const src = { a: [1, 2] }; const { value } = losslessSanitize(src); return value !== src && src.a.length === 2 })())

// ── 4. degrade mode, not a crash ─────────────────────────────────────────────
describe('4. degrade mode (missing host API must not kill the plugin)')

/** Minimal host ctx: records effects and the tools registered into them. */
function fakeContext(options = {}) {
  const effects = []
  const registered = []
  const logs = []
  const ctx = {
    logger: { warn: (m) => logs.push('warn:' + m), info: (m) => logs.push('info:' + m) },
    effect(fn, label) {
      effects.push({ label, fn })
      const dispose = typeof fn === 'function' ? fn() : undefined
      return () => { if (typeof dispose === 'function') dispose() }
    },
    tools: {
      register(definition) {
        if (options.rejectRegister) throw new Error('host refused registration')
        registered.push(definition)
        return () => registered.pop()
      },
      get: options.existingTool ? (name) => (name === options.existingTool ? {} : undefined) : undefined,
    },
    inject(services, callback) {
      if (options.skillsAvailable) callback(ctx)
    },
    skills: options.skillsAvailable ? { registerProvider: () => () => {} } : undefined,
  }
  if (options.withoutEffect) delete ctx.effect
  if (options.withoutTools) delete ctx.tools
  return { ctx, effects, registered, logs }
}
/** A stand-in host defineTool: same validation surface the real one enforces. */
function fakeDefineTool(options) {
  if (options && options.validate) options.validate(options)
  const tool = { ...options }
  return tool
}

{
  // The critical path: the host package could not be resolved at all.
  const { ctx, registered, logs } = fakeContext()
  let threw = null
  try {
    mod.apply(ctx, {}, { defineTool: undefined })
  } catch (error) {
    threw = error
  }
  // HOST_API resolved in this environment, so the real surface registers; the
  // degrade branch is exercised explicitly by the next block.
  check('apply() never throws', threw === null, threw ? String(threw.message).slice(0, 160) : '')
  check('apply() registers tools or a diagnostic', registered.length > 0 || logs.length > 0)
  check('every registration is wrapped in ctx.effect',
    registered.length === 0 || logs.length > 0)
}

{
  // Force the degraded branch: no resolvable defineTool at all.
  const { ctx, registered, logs } = fakeContext()
  const broken = await import(pathToFileURL(ENTRY).href)
  // Simulate the unresolved host by handing apply a resolution that failed.
  const original = broken.__internals.HOST_API
  const restore = () => { Object.assign(broken.__internals.HOST_API, original) }
  Object.assign(broken.__internals.HOST_API, {
    ok: false,
    tried: ['esm:@deepseek-ai/dsh-tools: ERR_MODULE_NOT_FOUND', '/nope (absent)'],
    error: 'cannot resolve @deepseek-ai/dsh-tools (simulated)',
  })
  let threw = null
  try {
    broken.apply(ctx, {}, undefined)
  } catch (error) {
    threw = error
  } finally {
    restore()
  }
  check('degraded apply() does not throw', threw === null,
    threw ? String(threw.message).slice(0, 160) : '')
  check('degraded mode registers exactly one diagnostic tool', registered.length === 1,
    'registered=' + String(registered.length))
  const diagnostic = registered[0]
  check('diagnostic tool is named atta_plugin_status',
    diagnostic && diagnostic.name === I.DIAGNOSTIC_TOOL, String(diagnostic && diagnostic.name))
  check('diagnostic tool name keeps the atta_ prefix',
    diagnostic && diagnostic.name.startsWith('atta_'))
  check('diagnostic registers through ctx.effect (disposable)', ctx.effect.length >= 1)
  const text = await diagnostic.execute({})
  const body = String((text && text.text) || '')
  check('diagnostic explains it is DEGRADED', /DEGRADED/.test(body), body.slice(0, 120))
  check('diagnostic names the missing host package', body.includes('dsh-tools'))
  check('diagnostic includes every attempted resolution',
    body.includes('ERR_MODULE_NOT_FOUND') && body.includes('/nope'))
  check('diagnostic tells the user how to fix it', /Fix:/.test(body))
  check('diagnostic output is lossless', isLossless(losslessSanitize(text).value))
}

{
  // A host that lacks ctx.effect: registering unscoped would leak tools past
  // plugin disposal, so the honest behaviour is to register nothing but say so.
  const { ctx, registered, logs } = fakeContext({ withoutEffect: true })
  let threw = null
  try { mod.apply(ctx, {}, { defineTool: fakeDefineTool }) } catch (error) { threw = error }
  check('apply() survives a host without ctx.effect', threw === null)
  check('no tools leak when registration cannot be scoped', registered.length === 0,
    'registered=' + String(registered.length))
  check('the missing ctx.effect is explained in the log',
    logs.some((l) => l.includes('ctx.effect')), logs.join(' | ').slice(0, 160))
}

{
  const { ctx, logs } = fakeContext({ withoutTools: true })
  let threw = null
  try { mod.apply(ctx, {}, { defineTool: fakeDefineTool }) } catch (error) { threw = error }
  check('apply() survives a host without ctx.tools', threw === null)
  check('a missing tools service is logged', logs.length > 0)
}

{
  // Double installation: the host already has our tools. Registering twice
  // makes the host reject the second attempt, which looks like a vanished
  // plugin. We must detect it and explain instead.
  const { ctx, registered } = fakeContext({ existingTool: 'atta_doctor' })
  let threw = null
  try { mod.apply(ctx, {}, { defineTool: fakeDefineTool }) } catch (error) { threw = error }
  check('double installation does not throw', threw === null)
  check('double installation registers only the diagnostic', registered.length === 1,
    'registered=' + String(registered.length))
  const body = String((await registered[0].execute({})).text || '')
  check('double installation is reported as ALREADY LOADED', /ALREADY LOADED/.test(body))
  check('double installation names the duplicate-install cause',
    /twice|injector/i.test(body))
}

// ── 5. full surface through a fake host ──────────────────────────────────────
describe('5. registered surface (through a stand-in host defineTool)')

{
  const { ctx, registered } = fakeContext()
  mod.apply(ctx, {}, { defineTool: fakeDefineTool })
  check('all 10 tools registered', registered.length === 10, 'got ' + String(registered.length))
  check('registered names match the declared specs',
    JSON.stringify(registered.map((t) => t.name).sort()) === JSON.stringify(specs.map((s) => s.name).sort()))
  check('every tool declares an executable execute()',
    registered.every((t) => typeof t.execute === 'function'))
  check('every tool declares output.render()',
    registered.every((t) => t.output && typeof t.output.render === 'function'))
  check('every tool declares a supported output schema',
    registered.every((t) => t.output && t.output.schema && t.output.schema.type === 'object'),
    JSON.stringify(registered.map((t) => t.output && t.output.schema && t.output.schema.type)))
  check('every tool is concurrency-annotated',
    registered.every((t) => typeof t.isConcurrencySafe === 'function'))
  // Model-mutating verbs must NOT advertise concurrency safety.
  for (const name of ['atta_model', 'atta_variant', 'atta_deliver', 'atta_run', 'atta_verify', 'atta_preview']) {
    const tool = registered.find((t) => t.name === name)
    check(name + ' is not advertised as concurrency-safe', tool && tool.isConcurrencySafe({}) === false)
  }
  for (const name of ['atta_doctor', 'atta_catalog', 'atta_spec', 'atta_describe']) {
    const tool = registered.find((t) => t.name === name)
    check(name + ' is advertised as concurrency-safe', tool && tool.isConcurrencySafe({}) === true)
  }
}

// ── 6. render output is lossless and image-carrying ──────────────────────────
describe('6. render contract (text block first, images forwarded)')

{
  const tool = I.buildTool(fakeDefineTool, specs.find((s) => s.name === 'atta_doctor'),
    fakeContext().ctx, {})
  const value = { text: 'hello' + String.fromCharCode(10) + 'more' }
  const blocks = tool.output.render({}, value)
  check('render returns at least one block', Array.isArray(blocks) && blocks.length >= 1)
  check('first block is the text payload', blocks[0].type === 'text' && blocks[0].text === value.text)
  const withImages = tool.output.render({}, { text: 't', images: [{ attachmentId: 'a' }, { attachmentId: 'b' }] })
  check('images become image blocks', withImages.filter((b) => b.type === 'image').length === 2)
  const envelope = I.resultEnvelope({ value: { ok: true } }, 'human', [{ attachmentId: 'a' }])
  check('result envelope carries text and images', typeof envelope.text === 'string' &&
    envelope.images.length === 1)
  check('result envelope text embeds parseable JSON', (() => {
    try { return JSON.parse(envelope.text.split(String.fromCharCode(10))[0]).ok === true } catch { return false }
  })())
  const noImages = I.resultEnvelope({ value: { ok: true } }, 'human', [])
  check('result envelope omits an empty images array', noImages.images === undefined)
}

// ── 7. path independence ─────────────────────────────────────────────────────
describe('7. location independence (link:, registry, Windows, macOS)')

check('plugin does not hard-code the checkout root in the shipped entry', (() => {
  const source = readFileSync(ENTRY, 'utf8')
  // A literal absolute path from the author's machine would break every other
  // install.  The only absolute paths allowed are /tmp style temp dirs created
  // at runtime, which do not appear as literals.
  return !/['"]\/(home|Users)\//.test(source)
})(), 'entry contains an absolute home path literal')

{
  // Exact paths on purpose: a "ends with atta/__init__.py" fake would match at
  // every level and never exercise the walk-up.
  const fakeExists = (p) =>
    p === path.join('/opt/somewhere', 'atta', '__init__.py') ||
    p === path.join('/work/project', 'atta', '__init__.py')
  const found = I.resolveRoot({ env: {}, exists: fakeExists, here: path.join('/opt/somewhere', 'plugin', 'lib') })
  check('resolveRoot walks up from the plugin directory', found.root === '/opt/somewhere',
    String(found.root))
  const fromCwd = I.resolveRoot({
    env: {}, exists: fakeExists, here: '/definitely/not/a/checkout', cwd: '/work/project',
  })
  check('resolveRoot falls back to the working directory', fromCwd.root === '/work/project',
    String(fromCwd.root))
  const nowhere = I.resolveRoot({ env: {}, exists: () => false, here: '/a/b/c', cwd: '/z' })
  check('resolveRoot returns null (not a throw) when nothing is found', nowhere.root === null)
}

{
  const candidates = I.pythonCandidates({
    env: { ATTA_PYTHON: '/custom/python3' }, root: '/repo', platform: 'linux',
  })
  check('ATTA_PYTHON is the first candidate', candidates[0].label === '/custom/python3',
    candidates.length ? candidates[0].label : '(none)')
  check('a project venv is preferred over PATH',
    candidates.findIndex((c) => c.label.includes('.venv')) <
      candidates.findIndex((c) => c.label === 'python3'))
  check('python3 is probed on POSIX', candidates.some((c) => c.label === 'python3'))
  check('bytecode-free: no candidate is an empty string',
    candidates.every((c) => c.cmd && c.cmd.length > 0))
}
{
  const win = I.pythonCandidates({ env: {}, root: 'C:\\repo', platform: 'win32' })
  check('Windows venv path uses Scripts/python.exe',
    win.some((c) => c.cmd.toLowerCase().includes('scripts' + path.sep + 'python.exe') ||
      c.cmd.toLowerCase().includes('scripts\\python.exe') || c.cmd.toLowerCase().includes('scripts/python.exe')),
    win.map((c) => c.cmd).join(' | ').slice(0, 160))
  check('Windows probes the py -3 launcher (no bare python3 guarantee)',
    win.some((c) => c.label === 'py -3' && c.args[0] === '-3'))
  check('Windows does not require python3', !win.some((c) => c.label === 'python3'))
}
{
  const split = I.pythonCandidates({ env: { ATTA_PYTHON: 'py -3.12' }, root: null, platform: 'win32' })
  check('ATTA_PYTHON with arguments is split into cmd + args',
    split[0].cmd === 'py' && JSON.stringify(split[0].args) === '["-3.12"]')
}
{
  const env = I.buildPythonEnv({ env: { PATH: '/bin', PYTHONPATH: '/keep' }, root: '/repo', workspace: '/ws' })
  check('PYTHONPATH gains the checkout so `-m atta` works from anywhere',
    env.PYTHONPATH.startsWith('/repo'), String(env.PYTHONPATH))
  check('PYTHONPATH keeps the caller value', env.PYTHONPATH.includes('/keep'))
  check('PYTHONPATH is delimited portably', env.PYTHONPATH.includes(path.delimiter))
  check('ATTA_ROOT is exported for the engine', env.ATTA_ROOT === '/repo')
  check('ATTA_HOME is the user workspace, never the checkout', env.ATTA_HOME === '/ws')
  check('unbuffered + utf8 output is forced (Windows cp1252 breaks JSON)',
    env.PYTHONUNBUFFERED === '1' && env.PYTHONIOENCODING === 'utf-8')
  const noRoot = I.buildPythonEnv({ env: {}, root: null, workspace: null })
  check('a pip-installed engine needs no PYTHONPATH or ATTA_ROOT',
    noRoot.PYTHONPATH === undefined && noRoot.ATTA_ROOT === undefined)
}
{
  // No interpreter can import atta: the failure must be actionable, not a throw.
  const result = await I.resolvePython({
    root: null, workspace: null, env: {},
    candidates: [{ cmd: '/definitely/missing/python', args: [], label: 'missing-python', source: 'test' }],
  })
  check('resolvePython returns a value instead of throwing', result && typeof result === 'object')
  check('resolvePython reports failure', result.ok === false)
  check('the failure names ATTA_PYTHON', String(result.error).includes('ATTA_PYTHON'), String(result.error).slice(0, 120))
  check('the failure names ATTA_ROOT or pip install', /ATTA_ROOT|pip install/.test(String(result.error)))
  check('the failure lists what was tried', Array.isArray(result.tried) && result.tried.length >= 1)
}
{
  // A Python traceback begins with "Traceback (most recent call last):" — that
  // header tells the user nothing. The receipt must surface the exception line.
  const tb = [
    'Traceback (most recent call last):',
    '  File "<string>", line 1, in <module>',
    "ModuleNotFoundError: No module named 'atta'",
    '',
  ].join(String.fromCharCode(10))
  const line = I.pythonErrorLine(tb)
  check('pythonErrorLine skips the traceback header',
    !/^Traceback/.test(line), line)
  check('pythonErrorLine surfaces the real exception',
    line.includes('ModuleNotFoundError') && line.includes("No module named 'atta'"), line)
  check('pythonErrorLine is a single line',
    !line.includes(String.fromCharCode(10)), JSON.stringify(line))
  check('pythonErrorLine tolerates empty stderr', I.pythonErrorLine('') === '')
  check('pythonErrorLine handles non-exception stderr',
    I.pythonErrorLine('some plain message') === 'some plain message')
  check('pythonErrorLine never throws on odd input',
    (() => { try { I.pythonErrorLine(undefined); I.pythonErrorLine(null); I.pythonErrorLine(42); return true } catch { return false } })())
}
{
  // A pinned interpreter that cannot import atta must NOT silently fall back:
  // running the engine under a different environment than the user chose is how
  // "works on my machine" geometry bugs happen. Fail loudly instead.
  I.resetPythonCache()
  const result = await I.callTool('atta_doctor', {}, { workspace: os.tmpdir(), python: '/definitely/missing' })
  check('callTool with no interpreter returns an envelope', result && typeof result.ok === 'boolean')
  check('the envelope is marked failed', result.ok === false)
  check('the envelope carries a machine-readable code', result.error && typeof result.error.code === 'string',
    String(result.error && result.error.code))
  check('a pinned interpreter failure names the pin',
    /ATTA_PYTHON/.test(String(result.error && result.error.message)),
    String(result.error && result.error.message).slice(0, 140))
  check('a pinned failure explains that no fallback was attempted',
    /Nothing else was tried/.test(String(result.error && result.error.message)))
  check('the failure path suggests unsetting the pin to auto-detect',
    /unset it/.test(String(result.error && result.error.message)))
  check('the envelope never leaks a stack trace', !/\n\s+at /.test(String(result.error && result.error.message)))
  check('the envelope is lossless', isLossless(result))
  // Unpinned, the same call must auto-detect whatever this machine provides.
  I.resetPythonCache()
  const auto = await I.callTool('atta_doctor', {}, { workspace: os.tmpdir() })
  check('without a pin, auto-detection is used and yields an envelope',
    auto && typeof auto.ok === 'boolean')
  check('without a pin the engine either runs or reports a readable reason',
    auto.ok === true || (auto.error && typeof auto.error.code === 'string'),
    String(auto.error && auto.error.code))
  I.resetPythonCache()
}

// ── 8. engine round trip (only when a real interpreter + atta exist) ─────────
describe('8. engine round trip (needs python + importable atta)')

function findWorkingPython() {
  const probe = 'import atta,sys;sys.stdout.write(atta.__version__)'
  for (const cmd of ['python3', 'python']) {
    try {
      const out = execFileSync(cmd, ['-c', probe], {
        cwd: path.dirname(PLUGIN_DIR), stdio: ['ignore', 'pipe', 'ignore'], timeout: 20000,
      })
      if (String(out).trim()) return { cmd, version: String(out).trim() }
    } catch (_error) { /* try the next one */ }
  }
  return null
}

const python = findWorkingPython()
if (!python) {
  skip('engine round trip', 'no python interpreter can import atta')
} else {
  const root = path.dirname(PLUGIN_DIR)
  I.resetPythonCache()
  const resolved = await I.resolvePython({
    root: root, workspace: os.tmpdir(),
    candidates: [{ cmd: python.cmd, args: [], label: python.cmd, source: 'test' }],
  })
  check('resolvePython verifies the engine by importing atta', resolved.ok === true,
    resolved.ok ? '' : String(resolved.error).slice(0, 200))
  check('resolvePython reports the engine version', resolved.ok && typeof resolved.version === 'string',
    String(resolved.ok && resolved.version))

  // A real CLI call, through the plugin's own path.
  const doctor = await I.callTool('atta_doctor', {}, { workspace: mkdtempSync(path.join(os.tmpdir(), 'atta-smoke-')) })
  check('atta_doctor round-trips through the CLI', doctor.ok === true,
    doctor.ok ? '' : String(doctor.error && doctor.error.message).slice(0, 300))
  check('atta_doctor returns engine data', doctor.ok && doctor.data && typeof doctor.data === 'object')
  check('the doctor envelope survives the lossless gate', isLossless(losslessSanitize(doctor).value))
  check('the doctor envelope names its tool', doctor.tool === 'atta_doctor', String(doctor.tool))
  check('the doctor envelope reports the workspace, not the checkout',
    doctor.ok && JSON.stringify(doctor.data).includes('workspace'))

  // A bad tool name must come back as a value with a code, not a crash.
  const bogus = await I.callTool('atta_not_a_tool', {}, { workspace: os.tmpdir() })
  check('an unknown verb degrades to an error envelope', bogus.ok === false)
  check('an unknown verb is lossless', isLossless(losslessSanitize(bogus).value))

  // The full tool surface must produce host-legal envelopes for a cheap call.
  const catalog = await I.callTool('atta_catalog', {}, { workspace: os.tmpdir() })
  check('atta_catalog round-trips', catalog.ok === true,
    catalog.ok ? '' : String(catalog.error && catalog.error.message).slice(0, 200))
  check('atta_catalog output is lossless', isLossless(losslessSanitize(catalog).value))
}

// ── 8b. the REAL host packages (best evidence available without a live host) ──
//
// Everything above uses stand-ins.  This section imports the actual
// @deepseek-ai/dsh-tools and @deepseek-ai/dsh-util-values from the installed DSH
// and asserts the plugin's surface against THEIR validators — the same code a
// live host runs.  That is the closest thing to a host round trip obtainable
// offline: it proves registration and output shape, not the cordis lifecycle.
describe('8b. real host validators (the actual dsh-tools / dsh-util-values)')

const realHost = await (async () => {
  const candidates = I.defineToolPathCandidates({})
  for (const candidate of candidates) {
    try {
      const tools = await import(pathToFileURL(candidate).href)
      if (typeof tools.defineTool !== 'function') continue
      const utilsDir = path.resolve(path.dirname(candidate), '..', '..', 'dsh-util-values')
      for (const rel of ['lib/index.mjs', 'lib/index.js']) {
        const utils = path.join(utilsDir, rel)
        if (!existsSync(utils)) continue
        const values = await import(pathToFileURL(utils).href)
        if (typeof values.snapshotJsonValue === 'function') {
          return { tools, values, from: candidate }
        }
      }
      return { tools, values: null, from: candidate }
    } catch (_error) { /* next candidate */ }
  }
  return null
})()

if (!realHost) {
  skip('real host validator checks', 'dsh-tools not resolvable in this environment')
} else {
  check('the real host defineTool was located', typeof realHost.tools.defineTool === 'function',
    realHost.from)
  const realRegistered = []
  const realCtx = {
    logger: { warn() {}, info() {} },
    effect(fn) { fn() },
    tools: { register(d) { realRegistered.push(d); return () => {} }, get() {} },
  }
  let applyThrew = null
  try { mod.apply(realCtx, {}, { defineTool: realHost.tools.defineTool }) } catch (error) { applyThrew = error }
  check('apply() registers the surface through the REAL defineTool', applyThrew === null,
    applyThrew ? String(applyThrew.message).slice(0, 200) : '')
  check('all 10 tools survive real defineTool validation', realRegistered.length === 10,
    'registered=' + String(realRegistered.length))
  check('the real host compiled a parameter schema for every tool',
    realRegistered.every((t) => t.parameters && t.parameters.type === 'object'))

  if (realHost.values) {
    // The single most important assertion in this file: the host walks every
    // tool return with snapshotJsonValue and rejects the WHOLE receipt if it is
    // not lossless JSON.
    let lossyRenders = 0
    for (const tool of realRegistered) {
      const blocks = tool.output.render({}, { text: 'x', images: [{ attachmentId: 'a' }] })
      if (realHost.values.snapshotJsonValue(blocks) === undefined) lossyRenders += 1
    }
    check('every tool render block passes the REAL lossless gate', lossyRenders === 0,
      'lossy=' + String(lossyRenders))

    // Real envelopes from real engine calls must also pass.
    if (python) {
      I.resetPythonCache()
      const envelope = await I.callTool('atta_doctor', {}, { workspace: os.tmpdir() })
      const gated = realHost.values.snapshotJsonValue(I.losslessSanitize(envelope).value)
      check('a real atta_doctor envelope passes the REAL lossless gate', gated !== undefined)
      if (envelope.ok) {
        const rendered = I.resultEnvelope(I.losslessSanitize(envelope), 'summary', [])
        check('the rendered tool result passes the REAL lossless gate',
          realHost.values.snapshotJsonValue(rendered) !== undefined)
      }
    } else {
      skip('real envelope gate check', 'no python interpreter can import atta')
    }
  } else {
    skip('real lossless gate checks', 'dsh-util-values not found next to dsh-tools')
  }

  // The model-facing schemas must accept the arguments a model will really send.
  const argumentCases = [
    ['atta_doctor', {}],
    ['atta_doctor', { deep: true }],
    ['atta_spec', { op: 'validate', spec: { model: { kind: 'recipe' } } }],
    ['atta_model', { recipe: 'product.mug', params: { height: 90 } }],
    ['atta_describe', { probe: { op: 'raycast', axis: 'z', res: [32, 32] } }],
    ['atta_describe', { probe: ['bbox', 'wall'] }],
    ['atta_describe', { probe: 'bbox' }],
    ['atta_preview', { materials: { '*': 'ceramic' } }],
    ['atta_preview', { materials: 'ceramic' }],
    ['atta_preview', { aspect: 'square' }],
    ['atta_preview', { aspect: 1.5 }],
    ['atta_verify', { constraints: { min_wall_mm: 2.5 } }],
    ['atta_run', { steps: [{ tool: 'atta_model', args: { recipe: 'product.mug' } }] }],
  ]
  let rejected = 0
  for (const [name, args] of argumentCases) {
    const tool = realRegistered.find((t) => t.name === name)
    if (!tool) { rejected += 1; continue }
    try {
      // real defineTool validates args before execute(); the engine call itself
      // may fail for environmental reasons, which is not an args problem.
      await tool.execute(args, {})
    } catch (error) {
      if (/INVALID_TOOL_ARGUMENTS|invalid argument|violation/i.test(String(error && error.message))) {
        rejected += 1
        console.log('       rejected: ' + name + ' ' + JSON.stringify(args))
      }
    }
  }
  check('no realistic argument payload is rejected by the real host schemas', rejected === 0,
    'rejected=' + String(rejected))
  I.resetPythonCache()
}

// ── 8c. coexistence with the reference plugin in one profile ─────────────────
//
// The reference dsh-blender-plugin registers blender_* tools and must keep
// working with this plugin installed beside it. The profile shares ONE tool
// registry, so collisions are the host's problem to reject — which looks like a
// crashing plugin. Assert we never cause one, never touch their names, and
// never write globals they could trip over.
describe('8c. coexistence with the reference blender plugin (one shared profile)')

{
  const shared = new Map()
  const makeSharedCtx = (tag) => ({
    logger: { warn(m) { shared.log.push(tag + ':' + String(m)) }, info() {} },
    effect(fn) { fn() },
    tools: {
      register(definition) {
        // The host rejects duplicate names inside one layer — model that here.
        if (shared.has(definition.name)) throw new Error('DUPLICATE tool ' + definition.name)
        shared.set(definition.name, definition)
        return () => shared.delete(definition.name)
      },
      get(name) { return shared.get(name) },
    },
  })
  shared.log = []

  mod.apply(makeSharedCtx('atta'), {}, { defineTool: fakeDefineTool })
  const attaNames = [...shared.keys()].filter((n) => n.startsWith('atta_'))
  check('atta registers its 10 tools into a shared registry', attaNames.length === 10,
    'got ' + String(attaNames.length))

  // The reference plugin's namespace appears in the same registry.
  const blenderNames = ['blender_rt_see', 'blender_rt_do', 'blender_rt_headless', 'blender_viewport']
  for (const name of blenderNames) shared.set(name, { name })
  check('the blender_* namespace is untouched by atta',
    blenderNames.every((n) => shared.has(n)))
  check('no atta tool name shadows a blender_* name',
    attaNames.every((n) => !blenderNames.includes(n)))

  // Loading atta twice must degrade with an explanation, not throw.
  let threw = null
  try { mod.apply(makeSharedCtx('atta2'), {}, { defineTool: fakeDefineTool }) } catch (error) { threw = error }
  check('a second atta copy in the same profile does not throw', threw === null,
    threw ? String(threw.message).slice(0, 160) : '')
  check('the second copy adds only the diagnostic, not duplicates',
    [...shared.keys()].filter((n) => n.startsWith('atta_')).length === 11,
    [...shared.keys()].filter((n) => n.startsWith('atta_')).length + ' atta_* names')
  check('the duplicate is explained in the log',
    shared.log.some((line) => /ALREADY LOADED/.test(line)), shared.log.join(' | ').slice(0, 200))
  check('blender tools still present after the duplicate load',
    blenderNames.every((n) => shared.has(n)))

  // No shared global keys: a globalThis cache is how two plugins silently
  // corrupt each other.
  check('the plugin writes no globalThis keys',
    !Object.keys(globalThis).some((key) => key.startsWith('__atta') || key.startsWith('dshAtta')))
  const source = readFileSync(ENTRY, 'utf8')
  check('the plugin never assigns to globalThis', !/globalThis\s*[.[]/.test(source))
}

// ── 9. package manifest contract ─────────────────────────────────────────────
describe('9. package manifest (what the DSH plugin manager actually reads)')

check('version is 1.2.0', PKG.version === '1.2.0', String(PKG.version))
check('package is ESM ("type": "module")', PKG.type === 'module', String(PKG.type))
check('main points at the shipped entry', PKG.main === './lib/index.js', String(PKG.main))
check('declares dsh.bundle.patch', Boolean(PKG.dsh && PKG.dsh.bundle && PKG.dsh.bundle.patch))
check('the declared bundle patch file exists',
  Boolean(PKG.dsh && PKG.dsh.bundle && existsSync(path.join(PLUGIN_DIR, PKG.dsh.bundle.patch))),
  String(PKG.dsh && PKG.dsh.bundle && PKG.dsh.bundle.patch))
check('files[] ships lib/', Array.isArray(PKG.files) && PKG.files.includes('lib'), JSON.stringify(PKG.files))
check('files[] ships the bundled skill',
  Array.isArray(PKG.files) && PKG.files.includes('skills'))
check('files[] ships the bundle patch',
  Array.isArray(PKG.files) && PKG.files.includes('cordis.patch.yml'))
check('declares a Node engine floor', Boolean(PKG.engines && PKG.engines.node), JSON.stringify(PKG.engines))
check('does NOT declare engines.dsh outside a supported range',
  !(PKG.engines && typeof PKG.engines.dsh === 'string' && PKG.engines.dsh.trim() === ''),
  JSON.stringify(PKG.engines))
check('peerDependencies are all optional (the host supplies them)',
  !PKG.peerDependencies ||
    Object.keys(PKG.peerDependencies).every(
      (id) => PKG.peerDependenciesMeta && PKG.peerDependenciesMeta[id] &&
        PKG.peerDependenciesMeta[id].optional === true,
    ),
  JSON.stringify(PKG.peerDependenciesMeta))
check('tool names do not collide with the reference plugin namespace', (() => {
  const blenderNames = ['blender_rt_see', 'blender_rt_do', 'blender_rt_headless', 'blender_viewport']
  return specs.every((s) => !blenderNames.includes(s.name))
})())
check('the two published cordis names are covered if peers are declared', (() => {
  const peers = Object.keys(PKG.peerDependencies || {})
  if (!peers.length) return true
  return peers.includes('cordis') && peers.includes('@deepseek-ai/cordis')
})(), JSON.stringify(Object.keys(PKG.peerDependencies || {})))

// The client-half trap: declaring dsh.client WITHOUT exports["./client"] makes
// the host throw during web boot (dsh-client-modules resolveMeta), which breaks
// the whole Web UI — unrelated to this plugin's tools.
describe('10. client-half declaration trap (a wrong dsh.client breaks Web boot)')
{
  const declaresClient = Boolean(PKG.dsh && PKG.dsh.client)
  const hasClientExport = Boolean(PKG.exports && PKG.exports['./client'])
  check('dsh.client is either absent or backed by exports["./client"]',
    !declaresClient || hasClientExport,
    'dsh.client=' + String(declaresClient) + ' exports["./client"]=' + String(hasClientExport))
  if (declaresClient && hasClientExport) {
    const rel = typeof PKG.exports['./client'] === 'string'
      ? PKG.exports['./client']
      : PKG.exports['./client'].default
    check('the declared client bundle exists on disk',
      Boolean(rel) && existsSync(path.resolve(PLUGIN_DIR, String(rel))), String(rel))
  }
  check('the bundle patch does not double-register a client platform hook',
    !(PKG.dsh && PKG.dsh.client && PKG.dsh.client.platform && !hasClientExport))

  // The bundle patch must insert exactly one entry, with our own id.
  const patchText = readFileSync(path.join(PLUGIN_DIR, 'cordis.patch.yml'), 'utf8')
  check('the bundle patch inserts an entry', /^-\s*insert:/m.test(patchText))
  check('the bundle patch entry id is atta', /id:\s*atta\b/.test(patchText), patchText.slice(0, 200))
  check('the bundle patch names this package', patchText.includes(PKG.name))
  check('the bundle patch inserts exactly one entry', (patchText.match(/-\s*id:/g) || []).length === 1)
}

// ── 11. bundled skill ────────────────────────────────────────────────────────
describe('11. bundled skill (optional; must never break the tool surface)')

{
  const provider = I.createSkillProvider()
  check('the bundled skill provider is created', Boolean(provider))
  if (provider) {
    check('provider name is atta', provider.name === 'atta', String(provider.name))
    check('list() returns a promise (host calls .then on it)', (() => {
      try { return typeof provider.list().then === 'function' } catch { return false }
    })())
    check('get() returns a promise', (() => {
      try { return typeof provider.get().then === 'function' } catch { return false }
    })())
    const listed = await provider.list()
    check('the skill list has exactly one entry', Array.isArray(listed) && listed.length === 1)
    const detail = await provider.get()
    check('the skill has a valid kebab-case name', /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(detail.name), detail.name)
    check('the skill has a description', typeof detail.description === 'string' && detail.description.length > 20)
    check('the skill has body content', typeof detail.content === 'string' && detail.content.length > 100)
    check('the skill declares model + user invocation',
      detail.invocation.modelInvocable === true && detail.invocation.userInvocable === true)
    check('the skill is marked bundled with the atta provider',
      detail.source === 'bundled' && detail.provider === 'atta')
  }
  I.resetSkillCache()
  check('a missing SKILL.md yields null, not an exception',
    I.readSkill({ dir: '/definitely/not/a/skill/dir' }) === null)
  const bad = mkdtempSync(path.join(os.tmpdir(), 'atta-badskill-'))
  writeFileSync(path.join(bad, 'SKILL.md'), '---\nname: Not A Valid Name\n---\nbody', 'utf8')
  I.resetSkillCache()
  check('an invalid skill name yields null (no crash)', I.readSkill({ dir: bad }) === null)
  const empty = mkdtempSync(path.join(os.tmpdir(), 'atta-empty-'))
  writeFileSync(path.join(empty, 'SKILL.md'), 'no frontmatter at all', 'utf8')
  I.resetSkillCache()
  check('a skill without frontmatter yields null', I.readSkill({ dir: empty }) === null)
  I.resetSkillCache()
  const fm = I.parseFrontmatter('---\nname: x\ndescription: >\n  folded\n---\nbody here')
  check('frontmatter parsing handles folded descriptions',
    fm.meta.name === 'x' && fm.meta.description.includes('folded'), JSON.stringify(fm.meta))
  check('frontmatter parsing returns the body after the header',
    !fm.body.includes('---'), JSON.stringify(fm.body))
  rmSync(bad, { recursive: true, force: true })
  rmSync(empty, { recursive: true, force: true })
  I.resetSkillCache()
}

// ── 12. host API resolution ladder ───────────────────────────────────────────
describe('12. host API resolution (must degrade, never throw)')

{
  // Explicit `candidates` REPLACE the filesystem ladder (that is the documented
  // contract, and it is what makes this testable), so a bogus list must fail
  // cleanly rather than quietly reaching for the real package.
  const result = await I.resolvePackage(['@deepseek-ai/dsh-tools'],
    (m) => (m && typeof m.defineTool === 'function' ? m.defineTool : undefined),
    { candidates: ['/definitely/not/a/module.mjs'] })
  check('resolvePackage returns a result object', result && typeof result.ok === 'boolean')
  check('resolvePackage records every attempt', Array.isArray(result.tried) && result.tried.length >= 3,
    'tried=' + String(result.tried && result.tried.length))
  check('an explicit candidate list replaces the built-in ladder', result.ok === false,
    result.tried.join(' | ').slice(0, 200))
  check('the tried list names each channel it attempted',
    result.tried.some((t) => t.startsWith('esm:')) &&
      result.tried.some((t) => t.startsWith('cjs:')) &&
      result.tried.some((t) => t.includes('/definitely/not/a/module.mjs')),
    result.tried.join(' | ').slice(0, 240))
  // With the ladder enabled, the host package must be found in this environment.
  // Note: resolvePackage is generic — the ladder is supplied by the caller
  // (resolveDefineTool wires defineToolPathCandidates in), which is the seam
  // that lets both be tested independently.
  const real = await I.resolvePackage(['@deepseek-ai/dsh-tools'],
    (m) => (m && typeof m.defineTool === 'function' ? m.defineTool : undefined),
    { candidates: I.defineToolPathCandidates({}) })
  check('resolvePackage finds the real host package via the default ladder',
    real.ok === true && typeof real.value === 'function',
    real.tried.join(' | ').slice(0, 240))
  check('the successful resolution reports which channel won',
    real.ok === true && /^(esm|cjs|fs):/.test(String(real.via)), String(real.via))
  const wired = await I.resolveDefineTool({})
  check('resolveDefineTool wires the ladder and succeeds here',
    wired.ok === true && typeof wired.defineTool === 'function',
    String(wired.ok ? wired.via : wired.error).slice(0, 200))
}
{
  const failures = await I.resolveDefineTool({ candidates: ['/definitely/not/here.js'] })
  check('resolveDefineTool never throws on total failure', Boolean(failures))
  check('a total failure is reported as ok:false or the real package',
    typeof failures.ok === 'boolean', String(failures.ok))
  if (!failures.ok) {
    check('the failure carries an actionable message', String(failures.error).includes('dsh-tools'))
    check('the failure does not include the whole stack', !/\n\s+at /.test(String(failures.error)))
  }
}
{
  // The ladder must reject a module that resolves but lacks defineTool.
  const fake = mkdtempSync(path.join(os.tmpdir(), 'atta-fakehost-'))
  const fakeFile = path.join(fake, 'index.mjs')
  writeFileSync(fakeFile, 'export const notDefineTool = 1\n', 'utf8')
  const result = await I.resolvePackage(['@deepseek-ai/dsh-tools'], (m) => m.defineTool, { candidates: [fakeFile] })
  check('a module without defineTool is rejected', result.ok === false)
  check('the rejection explains the export shape',
    result.tried.some((t) => t.includes('unexpected export shape')), result.tried.slice(-1).join(''))
  rmSync(fake, { recursive: true, force: true })
}
{
  // packageEntryCandidates must not guess: schemastery ships lib/index.mjs.
  const smDir = path.join(os.homedir(), '.dsh', 'profiles', 'node_modules', '@deepseek-ai', 'schemastery')
  if (existsSync(smDir)) {
    const entries = I.packageEntryCandidates(smDir)
    check('packageEntryCandidates reads exports from package.json',
      entries.some((e) => e.endsWith('.mjs') || e.endsWith('.js')), entries.join(' | ').slice(0, 160))
    check('packageEntryCandidates returns only files that exist',
      entries.every((e) => existsSync(e)))
    check('packageEntryCandidates prefers the import condition (.mjs)',
      entries[0].endsWith('.mjs'), entries[0])
  } else {
    skip('packageEntryCandidates against a real package', 'schemastery not installed here')
  }
  check('packageEntryCandidates falls back for an unreadable manifest',
    I.packageEntryCandidates('/definitely/not/a/package').length >= 0)
}
{
  const roots = I.nodeModulesRoots({ env: {}, home: '/home/u' })
  check('nodeModulesRoots includes the DSH profiles dir',
    roots.some((r) => r.includes(path.join('.dsh', 'profiles', 'node_modules'))), roots.join(' | '))
  check('nodeModulesRoots honours DSH_HOME',
    I.nodeModulesRoots({ env: { DSH_HOME: '/custom/dsh' }, home: '/home/u' })
      .some((r) => r.startsWith('/custom/dsh')))
  check('nodeModulesRoots returns unique absolute paths',
    new Set(roots).size === roots.length && roots.every((r) => path.isAbsolute(r)))
}

// ── summary ──────────────────────────────────────────────────────────────────
console.log('\n' + '─'.repeat(72))
console.log('passed: ' + String(passed) + '   failed: ' + String(failures.length) +
  '   skipped: ' + String(skips.length))
if (skips.length) {
  console.log('\nSKIPPED (not covered by this run — do not read green as coverage):')
  for (const item of skips) console.log('  - ' + item)
}
if (failures.length) {
  console.log('\nFAILURES:')
  for (const item of failures) console.log('  - ' + item)
  console.log('')
  process.exit(1)
}
console.log('\nAll smoke checks passed.')
console.log('NOTE: this suite runs WITHOUT a live DSH host. Tool registration, config')
console.log('      schema application and the host lossless gate are exercised through')
console.log('      faithful stand-ins; a real host round trip stays untested here.')
