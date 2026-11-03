// @atta/dsh-atta — DSH headless modeling plugin (host side).
//
// The plugin is a thin shell.  Geometry truth lives in the Atta v1.1 engine:
//
//   python3 -m atta --json tool <name> --args-file <tmp.json>
//
// Node's job: spawn the CLI, parse the `data` field, attach preview PNGs, and
// surface a compact error envelope ({data, error}) instead of a stack trace.
//
// Tools (10):
//   atta_doctor    engine / Blender / catalog self-check
//   atta_catalog   recipes, feature schema, constraints, workflow
//   atta_spec      Spec v3 validate / propose / compile / schema
//   atta_model     recipe | Spec v3 | feature graph build, or refine via patches
//   atta_variant   parameter sampling / fit, ranked candidates
//   atta_describe  measured facts: bbox, volume, topology, holes, wall, rays
//   atta_verify    pass | degraded | fail with a receipt
//   atta_preview   judgement/presentation PNGs + facts + image analysis
//   atta_deliver   verify gate, then GLB/OBJ/STL + manifest
//   atta_run       one call, several steps
//
// Units: public params/spec are mm; the Blender scene is metres.

import { spawn } from 'node:child_process'
import { existsSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url)) // .../plugin/lib
const ROOT = process.env.ATTA_ROOT || path.resolve(HERE, '..', '..')
const PY = process.env.ATTA_PYTHON || 'python3'
const VERSION = '1.1.0'
const PLUGIN_NAME = '@atta/dsh-atta'

/**
 * Resolve the host's defineTool from the DSH profile.  Bare import first, then
 * the usual profile/global layouts.  If none is found we throw instead of
 * pretending the tools were registered.
 */
async function resolveDefineTool() {
  const tried = []
  try {
    const mod = await import('@deepseek-ai/dsh-tools')
    if (typeof mod.defineTool === 'function') return mod.defineTool
    tried.push('bare(no defineTool)')
  } catch (err) {
    tried.push('bare:' + String((err && err.code) || (err && err.message) || err))
  }
  const home = os.homedir()
  const execDir = path.dirname(process.execPath)
  const candidates = [
    path.join(home, '.dsh', 'profiles', 'node_modules', '@deepseek-ai', 'dsh-tools', 'lib', 'index.js'),
    path.join(execDir, '..', 'lib', 'node_modules', '@deepseek-ai', 'dsh', 'node_modules', '@deepseek-ai', 'dsh-tools', 'lib', 'index.js'),
    path.join(execDir, '..', 'lib', 'node_modules', '@deepseek-ai', 'dsh-tools', 'lib', 'index.js'),
  ]
  for (const candidate of candidates) {
    try {
      const mod = await import(pathToFileURL(candidate).href)
      if (typeof mod.defineTool === 'function') return mod.defineTool
      tried.push(candidate + '(no defineTool)')
    } catch (err) {
      tried.push(candidate + ':' + String((err && err.code) || (err && err.message) || err))
    }
  }
  throw new Error('@atta/dsh-atta: cannot resolve host @deepseek-ai/dsh-tools. Tried: ' + tried.join(' / '))
}

const defineTool = await resolveDefineTool()

const NL = String.fromCharCode(10)

// Host-side deadlines for the Python CLI.  The long ones mirror the engine's
// own defaults; the fast ones only guard against a wedged process.
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

const CONCURRENT = {
  atta_doctor: true,
  atta_catalog: true,
  atta_spec: true,
  atta_model: false,
  atta_variant: false,
  atta_describe: true,
  atta_verify: true,
  atta_preview: true,
  atta_deliver: false,
  atta_run: false,
}

// ---------------------------------------------------------------- utilities

function sanitize(value) {
  try {
    return JSON.parse(JSON.stringify(value, (_key, item) => {
      if (typeof item === 'number' && !Number.isFinite(item)) return null
      return item
    }))
  } catch (err) {
    return { ok: false, error: 'result is not JSON-safe: ' + String((err && err.message) || err) }
  }
}

function envWithRoot() {
  const previous = process.env.PYTHONPATH
  return {
    ...process.env,
    ATTA_ROOT: ROOT,
    PYTHONPATH: previous ? ROOT + path.delimiter + previous : ROOT,
    PYTHONUNBUFFERED: '1',
  }
}

function runPython(args, timeoutMs) {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(PY, ['-m', 'atta', ...args], {
        cwd: ROOT,
        env: envWithRoot(),
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (err) {
      resolve({ ok: false, code: 'spawn', error: String((err && err.message) || err), stdout: '', stderr: '' })
      return
    }
    let out = ''
    let err = ''
    let settled = false
    const finish = (value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(value)
    }
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch (_err) { /* ignore */ }
      finish({ ok: false, code: 'timeout', error: 'python -m atta timed out after ' + timeoutMs + 'ms', stdout: out, stderr: err })
    }, timeoutMs)
    child.stdout.on('data', (chunk) => { out += String(chunk) })
    child.stderr.on('data', (chunk) => { err += String(chunk) })
    child.on('error', (spawnErr) => {
      finish({ ok: false, code: 'spawn', error: String((spawnErr && spawnErr.message) || spawnErr), stdout: out, stderr: err })
    })
    child.on('close', (code) => {
      finish({ ok: code === 0, code, stdout: out, stderr: err })
    })
  })
}

function parseJson(text) {
  const raw = String(text || '').trim()
  if (!raw) return null
  try { return JSON.parse(raw) } catch (_err) { /* fall through */ }
  const first = raw.indexOf('{')
  const last = raw.lastIndexOf('}')
  if (first >= 0 && last > first) {
    try { return JSON.parse(raw.slice(first, last + 1)) } catch (_err) { /* ignore */ }
  }
  return null
}

function normalizeError(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
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
  const data = envelope && envelope.data !== undefined ? envelope.data : null
  const ok = envelope && envelope.ok === false ? false : !(fallback && fallback.ok === false)
  const error = ok ? null : normalizeError(
    envelope && envelope.error !== undefined
      ? envelope.error
      : (fallback ? { code: fallback.code || 'error', message: fallback.error } : null),
  )
  return {
    ok,
    tool: (envelope && envelope.tool) || name,
    data,
    error,
    ms: envelope && typeof envelope.ms === 'number' ? envelope.ms : null,
  }
}

// ---------------------------------------------------------------- CLI call

async function callTool(name, args) {
  const timeoutMs = TOOL_TIMEOUT_MS[name] || 120000
  const argsFile = path.join(
    os.tmpdir(),
    'atta-tool-' + process.pid + '-' + Date.now() + '-' + Math.random().toString(16).slice(2) + '.json',
  )
  try {
    writeFileSync(argsFile, JSON.stringify(args || {}), 'utf8')
    const result = await runPython(
      ['--json', 'tool', name, '--args-file', argsFile],
      timeoutMs,
    )
    const parsed = parseJson(result.stdout)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      const fallback = result.ok
        ? { code: 'bad_json', error: 'CLI stdout is not JSON' }
        : { code: result.code || 'cli_failed', error: result.error || 'CLI failed' }
      return {
        ok: false,
        tool: name,
        data: null,
        error: normalizeError({
          code: fallback.code,
          message: fallback.error + ': ' + String(result.stdout || result.stderr || '').slice(0, 1200),
          path: 'stdout',
          fix: '确认 `python3 -m atta --json tool <name> --args-file <json>` 可用',
        }),
        ms: null,
      }
    }
    return normalizeEnvelope(name, parsed, result)
  } finally {
    try { unlinkSync(argsFile) } catch (_err) { /* ignore */ }
  }
}

// ---------------------------------------------------------------- images

function collectImages(value, out = []) {
  if (Array.isArray(value)) {
    for (const item of value) collectImages(item, out)
    return out
  }
  if (!value || typeof value !== 'object') return out
  for (const [key, item] of Object.entries(value)) {
    if (key === 'images' && Array.isArray(item)) {
      for (const record of item) {
        if (record && typeof record === 'object') out.push(record)
      }
    } else {
      collectImages(item, out)
    }
  }
  return out
}

function resolvePath(candidate) {
  const raw = String(candidate == null ? '' : candidate)
  if (!raw) return raw
  return path.isAbsolute(raw) ? raw : path.resolve(ROOT, raw)
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
    const resolved = resolvePath(filePath)
    if (!existsSync(resolved)) return { why: 'image missing: ' + resolved }
    const data = readFileSync(resolved)
    const ref = await attachments.saveImage({
      data,
      mediaType: 'image/png',
      name: String(name || path.basename(resolved)),
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
  } catch (err) {
    return { why: 'saveImage failed: ' + String((err && err.message) || err) }
  }
}

async function attachImages(ctx, records, max = 3) {
  const refs = []
  const failed = []
  const seen = new Set()
  for (const record of (records || [])) {
    if (refs.length + failed.length >= max) break
    const raw = record && (record.abs_path || record.path || record.file)
    if (!raw) continue
    const resolved = resolvePath(raw)
    if (seen.has(resolved)) continue
    seen.add(resolved)
    if (path.extname(resolved).toLowerCase() !== '.png') continue
    const outcome = await attachPng(ctx, resolved, record.view ? 'atta-' + record.view + '.png' : 'atta-preview.png')
    if (outcome.ref) refs.push(outcome.ref)
    else failed.push(resolved + ' (' + String(outcome.why || 'attach failed') + ')')
  }
  return { refs, failed }
}

// ---------------------------------------------------------------- rendering

function resultEnvelope(result, human, images) {
  const text = JSON.stringify(sanitize(result)) + (human ? NL + human : '')
  return images && images.length ? { text, images } : { text }
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

  const data = result.data || {}
  switch (name) {
    case 'atta_doctor': {
      const status = data.status || data
      return 'doctor ok; ' + JSON.stringify(status).slice(0, 900)
    }
    case 'atta_catalog': {
      const recipes = data.recipes || (data.catalog && data.catalog.recipes)
      const count = Array.isArray(recipes) ? recipes.length : (recipes && typeof recipes === 'object' ? Object.keys(recipes).length : null)
      return count === null ? 'catalog ok' : 'catalog ok; recipes=' + count
    }
    case 'atta_spec': {
      if (data.ok === false) {
        const errors = (data.errors || []).map((item) => String(item.path || '?') + ' ' + String(item.message || '')).slice(0, 6)
        return ['spec rejected', errors.join(' | ')].filter(Boolean).join(NL)
      }
      return 'spec ok' + (data.spec_hash ? '; spec_hash=' + String(data.spec_hash).slice(0, 12) : '')
    }
    case 'atta_model':
      return [
        'model ok; model_id=' + String(data.model_id || '?'),
        Array.isArray(data.parts) ? 'parts=' + data.parts.length : '',
        'next: atta_describe -> atta_verify',
      ].filter(Boolean).join(NL)
    case 'atta_variant': {
      const candidates = data.candidates || data.variants || []
      return 'variant ok; candidates=' + (Array.isArray(candidates) ? candidates.length : '?')
    }
    case 'atta_describe':
      return 'facts: ' + JSON.stringify(data.facts || data).slice(0, 900)
    case 'atta_verify': {
      const issues = data.issues || data.checks || []
      return 'verdict=' + String(data.verdict || '?') + ' score=' + String(data.score == null ? '?' : data.score)
        + (Array.isArray(issues) ? ' issues=' + issues.length : '')
    }
    case 'atta_preview':
      return 'preview ok; attached=' + (attached ? attached.refs.length : 0)
        + (attached && attached.failed.length ? '; failed=' + attached.failed.length : '')
    case 'atta_deliver':
      return 'deliver status=' + String(data.status || '?')
        + (Array.isArray(data.files) ? ' files=' + data.files.length : '')
    case 'atta_run': {
      const results = data.results || []
      const lines = ['run ok=' + String(data.ok) + ' n=' + String(data.n_run == null ? results.length : data.n_run)]
      for (const item of results.slice(0, 12)) {
        lines.push('- ' + String(item.name || item.tool || item.op || '?') + ': ' + (item.ok ? 'ok' : 'FAILED'))
      }
      return lines.join(NL)
    }
    default:
      return ''
  }
}

// ---------------------------------------------------------------- tools

function makeTools(ctx) {
  const tools = []

  const add = (name, description, parameters) => {
    tools.push(defineTool({
      name,
      description,
      parameters,
      output: { schema: { type: 'json' }, render: renderResult },
      isConcurrencySafe: () => CONCURRENT[name] !== false,
      timeoutMs: TOOL_TIMEOUT_MS[name],
      async execute(args) {
        const result = await callTool(name, args)
        const imageRecords = collectImages(result.data)
        const attached = imageRecords.length
          ? await attachImages(ctx, imageRecords, 3)
          : { refs: [], failed: [] }
        return resultEnvelope(result, humanSummary(name, result, attached), attached.refs)
      },
    }))
  }

  add('atta_doctor',
    '[Atta v' + VERSION + '] Engine self-check: version, Blender availability, recipe/spec/feature counts, current model. Run this before modeling; fix the environment first.', {
      deep: { type: 'boolean', description: 'true returns a deeper probe (may be slower)' },
    })

  add('atta_catalog',
    '[Atta v' + VERSION + '] Capability catalog: recipe ids and parameter schemas, feature/constraint schemas, recommended workflow. Call it instead of guessing field names.', {
      verbose: { type: 'boolean', description: 'true returns the full feature/parameter documentation' },
    })

  add('atta_spec',
    '[Atta v' + VERSION + '] Spec v3 entry: validate (no geometry), propose (deterministic design defaults + assumption_ledger + unresolved), compile (resolved params/features), schema (field documentation). model.kind may be recipe, graph, or assembly.', {
      op: { type: 'string', enum: ['validate', 'propose', 'compile', 'schema'], default: 'validate', description: 'validate | propose | compile | schema' },
      spec: { type: 'object', additionalProperties: true, description: 'Atta Spec v3 JSON' },
      assume: { type: 'boolean', description: 'validate with design defaults filled in' },
    })

  add('atta_model',
    '[Atta v' + VERSION + '] Build: recipe + params, Spec v3, a feature graph, or a multi-part assembly (model.kind=assembly with parts+mates); patches refine an existing build. Returns model_id + facts. Describe, then verify; never declare done from volume alone.', {
      recipe: { type: 'string', description: 'recipe id, e.g. product.mug' },
      spec: { type: 'object', additionalProperties: true, description: 'Atta Spec v3 JSON' },
      params: { type: 'object', additionalProperties: true, description: 'recipe parameter overrides (mm / deg)' },
      patches: { type: 'object', additionalProperties: true, description: 'refine patch applied to the current spec/params' },
      preset: { type: 'string', description: 'recipe preset (default nominal)' },
      quality: { type: 'string', enum: ['draft', 'review', 'deliver'], default: 'draft', description: 'draft/review/deliver resolution profile' },
      name: { type: 'string', description: 'model name' },
      material: { type: 'string', description: 'material name, e.g. ceramic' },
      finish: { type: 'object', additionalProperties: true, description: '{"bevel_mm":0.8,"smooth_deg":35}' },
    })

  add('atta_variant',
    '[Atta v' + VERSION + '] Parameter sampling or solving: without targets it samples and ranks candidates; with targets/solve=true it builds real draft meshes, solves constrained targets, and returns a feasible solution plus sensitivity, closest-achievable diagnostics and an optional Pareto front. base may be a recipe model or an assembly model (part params are addressed as part.param).', {
      recipe: { type: 'string', description: 'recipe id; use base for fit/solve' },
      base: { type: 'string', description: 'reference model id (recipe or assembly)' },
      count: { type: 'integer', default: 6, description: 'initial sample count' },
      ranges: { type: 'object', additionalProperties: true, description: '{"height":[80,120]} or {"height":{"min":80,"max":120}}; omitted in solve mode uses full parameter bounds' },
      strategy: { type: 'string', default: 'lhs', description: 'lhs|auto|compass|nelder-mead|differential-evolution|local|pareto' },
      seed: { type: 'integer', default: 42, description: 'random seed' },
      objective: { type: 'object', additionalProperties: true, description: '{"volume_mm3":"min"} or [{metric,direction,weight}]' },
      targets: { type: 'object', additionalProperties: true, description: '{"bbox_z":100,"volume_mm3":[250000,320000],"min_wall_mm":{"min":2.5}}' },
      constraints: { type: 'object', additionalProperties: true, description: 'same keys as atta_verify constraints; converted to solver targets' },
      max_evaluations: { type: 'integer', description: 'solver evaluation budget' },
      tolerance: { type: 'number', default: 0.000001, description: 'normalized feasibility tolerance' },
      initial: { type: 'object', additionalProperties: true, description: 'starting parameter values' },
      pareto: { type: 'boolean', description: 'return a Pareto front' },
      free: { type: 'array', items: { type: 'string' }, description: 'solve only these parameters' },
      fixed: { type: 'array', items: { type: 'string' }, description: 'freeze these parameters' },
      solve: { type: 'boolean', description: 'force solve mode' },
      quality: { type: 'string', enum: ['draft', 'review', 'deliver'], default: 'draft', description: 'candidate evaluation quality' },
    })

  add('atta_describe',
    '[Atta v' + VERSION + '] Measured structure + text perception: bbox/volume/topology/wall/curvature and probes. perceive=true returns low-power ASCII silhouettes, sections and shape traits without rendering; trace=true returns construction steps; compare_to=<model_id> returns silhouette IoU and +/- diff. Read these before looking at images.', {
      model_id: { type: 'string', description: 'model id; current model when omitted' },
      probe: { description: 'probe name, array, or {"op":"raycast","axis":"z","res":[32,32]}', oneOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }, { type: 'object', additionalProperties: true }] },
      wall: { type: 'boolean', description: 'run wall-thickness ray sampling (approx)' },
      curvature: { type: 'boolean', description: 'report curvature statistics' },
      perceive: { type: 'boolean', description: 'return the text/ASCII perception pack (no render)' },
      views: { type: 'array', items: { type: 'string' }, description: 'front/top/right/left/back/iso (default front,top,right)' },
      res: { type: 'integer', description: 'ASCII resolution 8..64 (default 24)' },
      sections: { description: 'Z fraction or array of fractions (default 0.5)', oneOf: [{ type: 'number' }, { type: 'array', items: { type: 'number' } }] },
      compare_to: { type: 'string', description: 'reference model id for a silhouette diff' },
      trace: { type: 'boolean', description: 'return recipe/graph construction steps with per-step volume' },
    })

  add('atta_verify',
    '[Atta v' + VERSION + '] Strict gate: topology + bbox/volume windows + clearance/interference + wall/dihedral/curvature; for assemblies also per-part solids, mate residuals, DOF and broadphase collisions. Verdict is pass | degraded | fail. pass only means verdict==pass.', {
      model_id: { type: 'string', description: 'model id; current model when omitted' },
      level: { type: 'string', enum: ['basic', 'standard', 'strict'], default: 'standard', description: 'gate level' },
      constraints: { type: 'object', additionalProperties: true, description: 'bbox/volume_mm3/min_wall_mm/genus/clearance/interference/envelope/render' },
      reference: { type: 'string', description: 'reference model id for deviation/clearance' },
    })

  add('atta_preview',
    '[Atta v' + VERSION + '] Judgement images and visual analysis: multi-view PNG plus facts and a render receipt. mode=beauty keeps the original look; studio/catalog/dark/clay are studio-lit presentation renders with per-part materials; diagnostic renders normal/depth/id/curvature/deviation. Beauty images carry exposure/Zone/CIELAB/contrast/chroma/edge/key-fill statistics. Images and analysis inform judgement; they are not the geometry verdict.', {
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
      materials: { oneOf: [{ type: 'string' }, { type: 'object', additionalProperties: true }], description: 'global material or {part_id: material, "*": material}' },
      passes: { type: 'array', items: { type: 'string' }, description: 'beauty/normal/depth/id/curvature/deviation' },
      reference: { type: 'string', description: 'reference model id for the deviation pass' },
      deviation_range_mm: { type: 'number', description: 'deviation colour scale in mm (default p95*2)' },
      focus: { type: 'string', description: 'part id to frame (macro/detail view)' },
      focus_padding: { type: 'number', description: 'focus framing padding ratio (default 0.3)' },
      aspect: { oneOf: [{ type: 'string', enum: ['auto', 'square'] }, { type: 'number' }], description: 'frame aspect; auto follows the projected bbox' },
      outdir: { type: 'string', description: 'output directory (default _atta/out)' },
    })

  add('atta_deliver',
    '[Atta v' + VERSION + '] Delivery: verify first; fail blocks, degraded needs allow_degraded=true. Exports GLB/OBJ/STL plus a manifest with sha256 and the verify receipt. Assembly models export a named-part GLB plus per-part OBJ/STL and an assembly receipt.', {
      model_id: { type: 'string', description: 'model id; current model when omitted' },
      formats: { type: 'array', items: { type: 'string' }, description: 'glb/obj/stl/blend/step; default glb,obj' },
      outdir: { type: 'string', description: 'output directory (default _atta/out)' },
      allow_degraded: { type: 'boolean', description: 'permit delivery on degraded' },
      name: { type: 'string', description: 'export filename stem' },
    })

  add('atta_run',
    '[Atta v' + VERSION + '] One call, several steps (model -> describe -> verify -> preview -> deliver). Later steps can use {"$prev":"model_id"} or {"$ref":"model_id"} in args. Nested atta_run is not supported. Stops on first error by default.', {
      steps: { type: 'array', items: { type: 'object', additionalProperties: true }, description: '[{"tool":"atta_model","args":{...}},{"tool":"atta_verify","args":{"model_id":{"$prev":"model_id"}}}]' },
      stop_on_error: { type: 'boolean', default: true, description: 'stop on first error (default true)' },
    })

  return tools
}

// ---------------------------------------------------------------- bundled skill

const SKILL_DIR = path.resolve(HERE, '..', 'skills', 'atta')
const SKILL_PROVIDER = 'atta'
const SKILL_RANK = 600
let skillCache

function parseFrontmatter(raw) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(raw)
  if (!match) return { meta: {}, body: raw }
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
    if (key && /^\s+\S/.test(line)) meta[key] = (meta[key] ? meta[key] + ' ' : '') + line.trim()
  }
  return { meta, body: raw.slice(match[0].length) }
}

function readSkill() {
  if (skillCache !== undefined) return skillCache
  try {
    const file = path.join(SKILL_DIR, 'SKILL.md')
    const raw = readFileSync(file, 'utf8')
    const { meta, body } = parseFrontmatter(raw)
    const name = String(meta.name || '').trim()
    const description = String(meta.description || '').trim()
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name) || !description) {
      skillCache = null
      return null
    }
    skillCache = { name, description, content: body, file }
    return skillCache
  } catch (_err) {
    skillCache = null
    return null
  }
}

function createSkillProvider() {
  const skill = readSkill()
  if (!skill) return null
  const resourceBase = { kind: 'directory', path: SKILL_DIR.split(path.sep).join('/') }
  const candidate = {
    name: skill.name,
    description: skill.description,
    invocation: { modelInvocable: true, userInvocable: true },
    source: 'bundled',
    provider: SKILL_PROVIDER,
    rank: SKILL_RANK,
    resourceBase,
    path: skill.file,
    locator: skill.file,
  }
  return {
    name: SKILL_PROVIDER,
    list: async () => [candidate],
    get: async () => ({ ...candidate, content: skill.content }),
  }
}

function registerSkill(ctx) {
  const provider = createSkillProvider()
  if (!provider) {
    try {
      ctx.logger?.warn?.('[atta] bundled skill not registered: skills/atta/SKILL.md missing or invalid frontmatter')
    } catch (_err) { /* ignore */ }
    return
  }
  const mount = (scope) => {
    try {
      scope.skills.registerProvider(() => provider)
    } catch (err) {
      try { ctx.logger?.warn?.('[atta] skill registration failed: ' + String((err && err.message) || err)) } catch (_err) { /* ignore */ }
    }
  }
  try {
    if (typeof ctx.inject === 'function') ctx.inject(['skills'], mount)
    else if (ctx.skills && typeof ctx.skills.registerProvider === 'function') mount(ctx)
  } catch (err) {
    try { ctx.logger?.warn?.('[atta] skill mount failed: ' + String((err && err.message) || err)) } catch (_err) { /* ignore */ }
  }
}

// ---------------------------------------------------------------- entry

export const name = PLUGIN_NAME
export const inject = ['tools']

export function apply(ctx, _config) {
  const tools = makeTools(ctx)
  for (const tool of tools) {
    ctx.effect(() => ctx.tools.register(tool), PLUGIN_NAME + ': tool ' + tool.name)
  }
  registerSkill(ctx)
  try {
    ctx.logger?.info?.('[atta] v' + VERSION + ' loaded; root=' + ROOT + '; tools=' + tools.length)
  } catch (_err) { /* ignore */ }
}

export const __internals = {
  VERSION,
  ROOT,
  PY,
  TOOL_TIMEOUT_MS,
  sanitize,
  parseJson,
  normalizeError,
  normalizeEnvelope,
  callTool,
  collectImages,
  makeTools,
  readSkill,
  createSkillProvider,
}
