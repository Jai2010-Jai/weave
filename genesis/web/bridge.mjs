// Genesis <-> weave website engine bridge.
//
// Runs weave's real website generator (src/portfolio/designInspirationResearcher.js and
// bespokeHtmlGenerator.js) from an unmodified weave checkout, in a child process started by
// genesis/web/builder.py. Reads one JSON request on stdin, writes one JSON result on stdout.
//
// Guard rails (weave's own fallbacks would otherwise hide failures or switch providers):
// - `dotenv` resolves to a no-op stub, so weave's .env is never read.
// - Only the Gemini API-key path may run: the agy CLI and Google OAuth / Cloud Code fallbacks are blocked.
// - Every Gemini call is recorded. If the page itself was not written by the model (weave would return a
//   canned template or the unchanged page), the result is an error, never a success.
// - Nothing is deployed: weave's orchestrator and Netlify deployer are not imported.

import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import * as nodeModule from 'node:module';

const STUB_DOTENV = 'data:text/javascript,' + encodeURIComponent(
  'export function config() { return { parsed: {} }; } export default { config };');

function blockDotenv() {
  if (typeof nodeModule.registerHooks === 'function') {
    nodeModule.registerHooks({
      resolve(specifier, context, next) {
        if (specifier === 'dotenv') return { url: STUB_DOTENV, shortCircuit: true };
        return next(specifier, context);
      },
    });
    return;
  }
  const hooks = `export async function resolve(s, c, n) {
    if (s === 'dotenv') return { url: ${JSON.stringify(STUB_DOTENV)}, shortCircuit: true };
    return n(s, c);
  }`;
  nodeModule.register('data:text/javascript,' + encodeURIComponent(hooks));
}

// weave logs progress with console.log; keep stdout for the JSON result only.
const log = (...a) => process.stderr.write(a.map(String).join(' ') + '\n');
console.log = log;
console.info = log;
console.warn = log;

function sanitize(text) {
  return String(text || '')
    .replace(/AIza[0-9A-Za-z_-]{20,}/g, '[redacted]')
    .replace(/(key=|x-goog-api-key["':\s]*)[^\s&"']+/gi, '$1[redacted]')
    .replace(/Bearer\s+[A-Za-z0-9._-]+/g, 'Bearer [redacted]')
    .slice(0, 600);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (c) => { data += c; });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

// Node's built-in fetch gives up when response headers take more than 300 s; Gemini can take longer than
// that to write a whole page (seen in practice: "fetch failed" at exactly 301 s). Gemini API requests go through
// node:https instead, which has no such limit. Genesis's own timeout (GENESIS_WEBSITE_TIMEOUT_SECONDS) still
// kills the whole bridge. Every other request keeps the normal fetch.
const AI_HOSTS = new Set(['generativelanguage.googleapis.com', 'api.z.ai', 'open.bigmodel.cn']);
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === 'string' ? input : input.url);
  if (!AI_HOSTS.has(url.hostname)) return nativeFetch(input, init);
  const https = await import('node:https');
  return new Promise((resolve, reject) => {
    const req = https.request(url, { method: init.method || 'GET', headers: init.headers || {} }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const body = Buffer.concat(chunks).toString('utf8');
        resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, status: res.statusCode,
          text: async () => body, json: async () => JSON.parse(body) });
      });
      res.on('error', reject);
    });
    req.on('error', reject);
    if (init.signal) init.signal.addEventListener('abort', () => req.destroy(new Error('aborted')));
    if (init.body) req.write(init.body);
    req.end();
  });
};

// The exact shape weave's researcher reads. With a schema Gemini must return valid JSON (JSON mode alone still
// produced invalid JSON in real runs). weave's long `stitchPrompt` is left out: it's only used for Google Stitch.
const STR = { type: 'string' };
const RESEARCH_SCHEMA = {
  type: 'object',
  properties: {
    topicAnalysis: {
      type: 'object',
      properties: {
        domain: STR, brandName: STR, tagline: STR, headline: STR, targetAudience: STR,
        benchmarkSites: { type: 'array', items: STR },
        keyFeatures: { type: 'array', items: { type: 'object', properties: { title: STR, description: STR, metric: STR },
          required: ['title', 'description', 'metric'] } },
      },
      required: ['domain', 'brandName', 'tagline', 'headline', 'keyFeatures'],
    },
    inspirations: {
      type: 'array', minItems: 3, maxItems: 3,
      items: {
        type: 'object',
        properties: {
          option: { type: 'integer' }, id: STR, name: STR, sourceInspiration: STR, aestheticDescription: STR,
          fontHeading: STR, fontBody: STR, googleFontsUrl: STR, stitchHeadlineFont: STR, stitchBodyFont: STR,
          colors: { type: 'object', properties: { bg: STR, bgCard: STR, accent: STR, accentSecondary: STR, textPrimary: STR,
            textMuted: STR, border: STR }, required: ['bg', 'bgCard', 'accent', 'accentSecondary', 'textPrimary', 'textMuted', 'border'] },
          components: { type: 'array', items: STR },
        },
        required: ['option', 'id', 'name', 'sourceInspiration', 'aestheticDescription', 'fontHeading', 'fontBody',
          'googleFontsUrl', 'colors', 'components'],
      },
    },
  },
  required: ['topicAnalysis', 'inspirations'],
};

// Speed. Gemini 3 models think before answering by default, and that was most of a build's time (research:
// 379 s for 1.7k output tokens). Low thinking keeps page quality at a fraction of the wait.
const THINKING = process.env.GENESIS_WEBSITE_THINKING || 'low';
const noThinking = new Set(); // models that rejected a thinking setting: sent without one from then on
function thinkingFor(model) {
  if (THINKING === 'default' || noThinking.has(model)) return null;
  if (/^gemini-2\.5-flash/.test(model)) return { thinkingBudget: 0 };
  if (/^gemini-2\.5/.test(model)) return { thinkingBudget: 128 };
  return { thinkingLevel: THINKING };
}
// Unbounded pages reached 86 KB (27k output tokens, 8 minutes of generation) without looking better.
const LENGTH_BUDGET = '\n\nLENGTH BUDGET: one focused page of about 400-700 lines of HTML (roughly 25-40 KB). Prefer fewer, ' +
  'richer sections over long repeated ones. No duplicated blocks, no placeholder filler.' +
  '\n\nSAFETY: never add password, payment card, CVV or one-time-code inputs, and no login or sign-in forms; ' +
  'use a contact or enquiry form (name, email, phone, message) when a form is needed.';
const BUDGETED = /^(?:html-\d|redesign|pages)$/;
// How long a model may stay silent before the same request also goes to the next model. Healthy calls take
// 10-30 s for research and plans and 60-140 s for a page; overloaded ones were seen taking 5-6 minutes.
const HEDGE_MS = { research: 45_000, 'pages-plan': 25_000 };
function hedgeAfter(p) {
  const override = Number(process.env.GENESIS_HEDGE_MS || 0);
  if (override) return override;
  return HEDGE_MS[p] ?? (BUDGETED.test(p) ? 150_000 : 60_000);
}

// Model health shared by all builds, including parallel ones: a model that was overloaded in the last few
// minutes is tried last, so each build doesn't lose a minute rediscovering it.
const HEALTH_FILE = process.env.GEMINI_HEALTH_FILE || '';
const HEALTH_TTL_MS = 10 * 60_000;
function readHealth() {
  try {
    return JSON.parse(fs.readFileSync(HEALTH_FILE, 'utf8')) || {};
  } catch {
    return {};
  }
}
function markHealth(model, busy) {
  if (!HEALTH_FILE) return;
  try {
    const h = readHealth();
    if (!busy && !h[model]) return;
    h[model] = busy ? Date.now() : 0;
    const tmp = `${HEALTH_FILE}.${process.pid}`;
    fs.writeFileSync(tmp, JSON.stringify(h));
    fs.renameSync(tmp, HEALTH_FILE);
  } catch {
    // best effort: health is only an ordering hint
  }
}
function recentlyBusy(model) {
  return HEALTH_FILE !== '' && Date.now() - (readHealth()[model] || 0) < HEALTH_TTL_MS;
}

const calls = [];
const busyModels = new Set();
const TRANSIENT = /\((?:429|500|502|503|504)\)|UNAVAILABLE|RESOURCE_EXHAUSTED|overloaded|high demand|fetch failed|ECONNRESET|ETIMEDOUT|socket hang up/i;
let phase = 'setup';
const aiText = {}; // phase -> last raw model text

// GENESIS_AI_PROVIDER=glm: the engine's Gemini calls go to GLM (Zhipu / Z.ai) instead, in the OpenAI chat format,
// answering in the shape weave reads ({ text, raw.usageMetadata }). GLM's JSON mode takes no schema, so the schema is
// spelled out in the system prompt. Busy answers (code 1305 "overloaded" and friends) report as (503) so the retry and
// next-model logic above treats them like Gemini's.
async function glmGenerate(model, apiKey, prompt, system, config = {}) {
  const schema = config?.responseJsonSchema;
  const sys = (system || '') + (schema
    ? `\n\nReply with one JSON object only (no markdown fences) that matches this JSON schema:\n${JSON.stringify(schema)}` : '');
  const body = {
    model,
    messages: [...(sys ? [{ role: 'system', content: sys }] : []), { role: 'user', content: prompt }],
    max_tokens: Math.min(config?.maxOutputTokens || 32768, 32768),
    thinking: { type: 'disabled' },
  };
  if (config?.temperature != null) body.temperature = config.temperature;
  if (schema || config?.responseMimeType === 'application/json') body.response_format = { type: 'json_object' };
  const base = (process.env.GLM_BASE_URL || 'https://api.z.ai/api/paas/v4').replace(/\/$/, '');
  // The free glm-4.7-flash answers "overloaded" about half the time and fine a moment later: ask again first.
  let res;
  let raw;
  for (const pause of [500, 1000, 2000, 3000, 5000, null]) {
    res = await fetch(`${base}/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(body),
    });
    raw = await res.text();
    if (res.ok) break;
    let code = '';
    try { code = String(JSON.parse(raw)?.error?.code ?? ''); } catch { /* not JSON */ }
    const busy = ['1302', '1303', '1305', '1312'].includes(code) || res.status >= 500;
    if (!busy || pause === null) throw new Error(`GLM API error (${busy ? 503 : res.status}): ${code} ${raw.slice(0, 300)}`);
    await new Promise((r) => setTimeout(r, pause));
  }
  const data = JSON.parse(raw);
  const usage = data.usage || {};
  return { text: data.choices?.[0]?.message?.content || '',
    raw: { usageMetadata: { promptTokenCount: usage.prompt_tokens, candidatesTokenCount: usage.completion_tokens } } };
}

async function loadEngine(engineDir) {
  const src = path.join(engineDir, 'src');
  for (const f of ['gemini-client.js', 'portfolio/designInspirationResearcher.js', 'portfolio/bespokeHtmlGenerator.js']) {
    if (!fs.existsSync(path.join(src, f))) throw new Error(`website engine file missing: src/${f}`);
  }
  blockDotenv();
  const url = (f) => pathToFileURL(path.join(src, f)).href;
  const { geminiClient } = await import(url('gemini-client.js'));
  const research = await import(url('portfolio/designInspirationResearcher.js'));
  const bespoke = await import(url('portfolio/bespokeHtmlGenerator.js'));

  geminiClient.generateViaAgy = async () => { throw new Error('agy CLI is disabled by Genesis'); };
  geminiClient.resolveCloudCodeProject = async () => { throw new Error('Cloud Code OAuth is disabled by Genesis'); };
  // glm-* models go to GLM, every other model to Gemini, so GLM can answer first with Gemini as the fallback.
  const gemini = geminiClient.generateContent.bind(geminiClient);
  const original = (prompt, system, config) => (String(geminiClient.model).startsWith('glm')
    ? glmGenerate(geminiClient.model, process.env.GLM_API_KEY, prompt, system, config)
    : gemini(prompt, system, config));
  geminiClient.generateContent = async (prompt, system, config) => {
    if (!geminiClient.apiKey && !process.env.GLM_API_KEY) {
      const err = new Error('GEMINI_API_KEY is not set for the website engine');
      calls.push({ phase, ok: false, error: err.message });
      throw err;
    }
    if (phase === 'research' && !config) {
      // weave parses this reply as JSON but doesn't ask for JSON; without JSON mode Gemini sometimes returns
      // invalid JSON and weave silently switches to canned directions.
      // A normal reply is ~1,500 tokens; one ran away to 32,000 (2.5 minutes). The cap cuts that short.
      config = { responseMimeType: 'application/json', responseJsonSchema: RESEARCH_SCHEMA, maxOutputTokens: 8192 };
    }
    // Gemini regularly answers 503 "high demand" (also 429/500), and an overloaded model can also just stall for
    // minutes. Each model gets a retry; a failure moves on to the next model in GEMINI_FALLBACK_MODELS, and a
    // model that hasn't answered within hedgeAfter(phase) gets company: the same request also goes to the next
    // model and the first good answer wins. Different model families have separate capacity.
    const primary = geminiClient.model;
    const all = [...new Set([primary, ...String(process.env.GEMINI_FALLBACK_MODELS || '').split(',').map((m) => m.trim()).filter(Boolean)])];
    // Models that were overloaded earlier in this build, or recently in any build, go last.
    const slow = (m) => busyModels.has(m) || recentlyBusy(m);
    const models = [...all.filter((m) => !slow(m)), ...all.filter(slow)];
    const ph = phase;
    const sys = BUDGETED.test(ph) ? (system || '') + LENGTH_BUDGET : system;
    const pause = Number(process.env.GENESIS_RESEARCH_RETRY_MS ?? 5000);
    const race = { settled: false };

    async function ask(model) {
      let lastError;
      // glmGenerate already retries busy answers itself, so a GLM failure moves straight on to the next model.
      const tries = model.startsWith('glm') ? 1 : 2;
      for (let attempt = 1; attempt <= tries; attempt++) {
        const thinking = thinkingFor(model);
        const cfg = thinking ? { ...(config || {}), thinkingConfig: thinking } : config;
        const started = Date.now();
        // A call that loses the race still costs tokens: it is recorded, but under its own phase name so it
        // never counts as this phase's answer.
        const record = (entry) => calls.push({ phase: race.settled ? `${ph}:late` : ph, model, ms: Date.now() - started, ...entry });
        try {
          geminiClient.model = model; // read synchronously when the request starts, so parallel calls are safe
          const pending = original(prompt, sys, cfg);
          geminiClient.model = primary;
          const res = await pending;
          const usage = res?.raw?.usageMetadata || {};
          record({ ok: true, input_tokens: usage.promptTokenCount ?? null, output_tokens: usage.candidatesTokenCount ?? null,
            thinking_tokens: usage.thoughtsTokenCount ?? null });
          markHealth(model, false);
          return res;
        } catch (e) {
          geminiClient.model = primary;
          record({ ok: false, error: sanitize(e.message) });
          lastError = e;
          if (thinking && /\(400\)/.test(String(e?.message)) && /think/i.test(String(e?.message))) {
            noThinking.add(model); // this model takes no thinking setting: ask again without one
            attempt--;
            continue;
          }
          if (!TRANSIENT.test(String(e?.message))) {
            e.fatal = true; // a bad request fails the same way on every model
            throw e;
          }
          if (attempt === tries) {
            busyModels.add(model);
            markHealth(model, true);
          }
          log(`AI busy on ${model} (${ph}); ${attempt < tries ? 'retrying' : 'trying the next model'}`);
          if (attempt < tries) await new Promise((r) => setTimeout(r, pause * attempt));
        }
      }
      throw lastError;
    }

    const res = await new Promise((resolve, reject) => {
      let next = 0;
      let running = 0;
      let lastError;
      let timer;
      const finish = (fn, value) => {
        if (race.settled) return;
        race.settled = true;
        clearTimeout(timer);
        fn(value);
      };
      const launch = () => {
        clearTimeout(timer);
        if (race.settled) return;
        if (next >= models.length) {
          if (!running) finish(reject, lastError);
          return;
        }
        const model = models[next++];
        running++;
        if (next < models.length) {
          timer = setTimeout(() => {
            log(`${model} is slow on ${ph}; also asking ${models[next]}`);
            launch();
          }, hedgeAfter(ph));
        }
        ask(model).then((r) => {
          running--;
          finish(resolve, r);
        }, (e) => {
          running--;
          lastError = e;
          if (e?.fatal) next = models.length; // no point asking more models; let a call already running finish
          launch();
        });
      };
      launch();
    });
    aiText[ph] = res?.text || '';
    return res;
  };
  return { research, bespoke, gemini: geminiClient };
}

function lastCall(p) {
  const list = calls.filter((c) => c.phase === p);
  return list[list.length - 1] || null;
}

function failure(p, fallbackMessage) {
  const c = lastCall(p);
  return c && !c.ok ? `${fallbackMessage}: ${c.error}` : fallbackMessage;
}

// True when `html` came out of the model's reply (weave only strips code fences), not a canned template.
function fromModel(p, html) {
  const text = aiText[p] || '';
  const c = lastCall(p);
  if (!c || !c.ok || !text || !html) return false;
  const probe = html.slice(0, 300).trim();
  return probe.length > 0 && text.includes(probe);
}

async function generate(req, engine) {
  const out = req.out_dir;
  // weave silently swaps in canned directions (with the first words of the prompt as the "brand name") when the
  // research call fails or its reply isn't valid JSON. Both happen in practice; retry, then fail honestly rather
  // than build a site called "Can U Build".
  let research;
  let researchFromModel = false;
  for (let attempt = 1; attempt <= 3 && !researchFromModel; attempt++) {
    if (attempt > 1) await new Promise((r) => setTimeout(r, Number(process.env.GENESIS_RESEARCH_RETRY_MS ?? 5000) * attempt));
    phase = 'research';
    aiText.research = '';
    research = await engine.research.researchDesignInspirations(req.prompt);
    const firstName = String(research?.inspirations?.[0]?.name || '');
    researchFromModel = Boolean(lastCall('research')?.ok && firstName && aiText.research.includes(firstName));
  }
  if (!researchFromModel) {
    return { ok: false, error: failure('research', 'The AI could not design the website (busy or invalid reply)') };
  }
  const topic = research.topicAnalysis || {};
  const inspirations = (research.inspirations || []).slice(0, 3);
  const count = Math.max(1, Math.min(3, Number(req.variants) || 1));
  const pages = [];
  for (let i = 0; i < count && i < inspirations.length; i++) {
    phase = `html-${i + 1}`;
    const html = await engine.bespoke.generateBespokeHtml({ prompt: req.prompt, topicAnalysis: topic, inspiration: inspirations[i] });
    if (!fromModel(phase, html)) {
      if (i === 0) return { ok: false, error: failure(phase, 'The AI did not produce the website page') };
      break; // extra design options are optional
    }
    const file = i === 0 ? 'index.html' : `option${i + 1}.html`;
    // Written whole (temp file + rename): Genesis publishes index.html the moment it appears, while extra pages
    // are still being written.
    fs.writeFileSync(path.join(out, `.${file}.tmp`), html, 'utf8');
    fs.renameSync(path.join(out, `.${file}.tmp`), path.join(out, file));
    pages.push({ file, option: i + 1, name: String(inspirations[i]?.name || '').slice(0, 120),
      inspiration: String(inspirations[i]?.sourceInspiration || '').slice(0, 200) });
  }
  const extra = Math.max(0, Math.min(MAX_EXTRA_PAGES, Number(req.extra_pages) || 0));
  if (extra > 0) {
    // Extra pages are optional: a failure here still leaves a working one-page site.
    try {
      pages.push(...await addPages(req, engine, topic, out, extra));
    } catch (e) {
      log(`extra pages skipped: ${sanitize(e?.message || e)}`);
    }
  }
  return {
    ok: true,
    brand_name: String(topic.brandName || '').slice(0, 60),
    headline: String(topic.headline || '').slice(0, 200),
    research: researchFromModel ? 'ai' : 'fallback',
    brand_from_research: researchFromModel,
    pages,
  };
}

// ---------- multi-page sites (Genesis addition; weave itself writes one page) ----------
const MAX_EXTRA_PAGES = 4;
const RESERVED_PAGES = new Set(['index', 'option2', 'option3']);
const PLAN_SCHEMA = {
  type: 'object',
  properties: { pages: { type: 'array', maxItems: MAX_EXTRA_PAGES, items: { type: 'object',
    properties: { slug: STR, title: STR, purpose: STR }, required: ['slug', 'title', 'purpose'] } } },
  required: ['pages'],
};

function stripFences(text) {
  const t = String(text || '').trim();
  const m = t.match(/```(?:html)?\s*([\s\S]*?)```/i);
  return (m ? m[1] : t).trim();
}

function pageSlug(raw) {
  const slug = String(raw || '').toLowerCase().replace(/\.html?$/, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 20);
  return /^[a-z][a-z0-9-]{1,19}$/.test(slug) && !RESERVED_PAGES.has(slug) ? slug.replace(/-+$/, '') : '';
}

const firstNav = (html) => (html.match(/<nav\b[\s\S]*?<\/nav>/i) || [null])[0];

// Gemini often answers 503/429 under load; extra pages retry a few times before giving up.
async function withRetry(fn) {
  for (let attempt = 1; ; attempt++) {
    try {
      return await fn();
    } catch (e) {
      if (attempt >= 3 || !/\((?:429|500|503)\)|fetch failed|ECONNRESET/i.test(String(e?.message))) throw e;
      await new Promise((r) => setTimeout(r, Number(process.env.GENESIS_RESEARCH_RETRY_MS ?? 5000) * attempt));
    }
  }
}

// Milliseconds left before optional work (extra pages) must stop; Infinity without a deadline.
function timeLeft() {
  const deadline = Number(process.env.WEBSITE_DEADLINE_MS || 0);
  return deadline ? deadline - Date.now() : Infinity;
}

async function addPages(req, engine, topic, out, extra) {
  if (timeLeft() < 60_000) {
    log('no time left for extra pages; shipping the home page');
    return [];
  }
  const index = fs.readFileSync(path.join(out, 'index.html'), 'utf8');
  phase = 'pages-plan';
  const plan = await withRetry(() => engine.gemini.generateContent(
    `Website request: ${req.prompt}\nBrand: ${topic.brandName || ''}\n\nThe home page (index.html) already exists. ` +
    `Choose up to ${extra} more pages this website should have (for example about, menu, services, pricing, contact). ` +
    'Use short lowercase slugs. Do not include the home page.',
    'You plan the page structure of small business and portfolio websites. Reply with JSON only.',
    { responseMimeType: 'application/json', responseJsonSchema: PLAN_SCHEMA }));
  const seen = new Set();
  const wanted = [];
  for (const p of (JSON.parse(plan.text || '{}').pages || [])) {
    const slug = pageSlug(p.slug);
    if (slug && !seen.has(slug) && wanted.length < extra) {
      seen.add(slug);
      wanted.push({ file: `${slug}.html`, title: String(p.title || slug).slice(0, 60), purpose: String(p.purpose || '').slice(0, 300) });
    }
  }
  if (!wanted.length) return [];
  const navList = [{ file: 'index.html', title: 'Home' }, ...wanted].map((p) => `${p.title} -> ${p.file}`).join('\n');
  const reference = index.slice(0, 14000);
  // Pages are written in parallel: each is a few minutes of Gemini output, so one after another took far too long.
  phase = 'pages';
  let closed = false;  // set when out of time: late pages are dropped, never half-linked
  const finished = [];
  const all = Promise.all(wanted.map(async (p) => {
    let res;
    try {
      res = await withRetry(() => engine.gemini.generateContent(
      `Write the "${p.title}" page (${p.file}) of this website. Purpose: ${p.purpose}\n` +
      `Original request: ${req.prompt}\n\nThe site's pages (link to them with these exact relative file names):\n${navList}\n\n` +
      'Reuse the home page\'s exact <head> setup (fonts, Tailwind config, colors), header, <nav> and footer, ' +
      'with the nav linking every page above. Fill the body with real, specific content for this page. ' +
      'Forms must use action="#". No iframes. Return one complete HTML document only.\n\n' +
      `HOME PAGE (index.html), for style reference:\n${reference}`,
      'You are a senior web designer extending an existing website with a matching page. Reply with HTML only.'));
    } catch (e) {
      log(`page ${p.file} skipped: ${sanitize(e?.message || e)}`);
      return null;
    }
    const html = stripFences(res?.text);
    if (closed || !/<html[\s>]/i.test(html) || !/<\/html>/i.test(html)) return;
    fs.writeFileSync(path.join(out, p.file), html, 'utf8');
    finished.push({ file: p.file, option: 1, name: p.title, inspiration: '' });
  }));
  let timer;
  await Promise.race([all, new Promise((r) => { timer = setTimeout(r, Math.max(0, timeLeft())); })]);
  clearTimeout(timer);
  closed = true;
  if (finished.length < wanted.length) log(`out of time for extra pages; shipping ${finished.length} of ${wanted.length}`);
  // Keep the planned order; the nav lists only pages that exist.
  const done = wanted.map((p) => finished.find((f) => f.file === p.file)).filter(Boolean);
  if (!done.length) return [];
  // Pages that missed the deadline are still linked from the ones that made it: point those links home.
  const missing = wanted.filter((p) => !done.some((d) => d.file === p.file)).map((p) => p.file);
  for (const d of done) {
    const f = path.join(out, d.file);
    let html = fs.readFileSync(f, 'utf8');
    for (const m of missing) html = html.split(`href="${m}"`).join('href="index.html"');
    fs.writeFileSync(f, html, 'utf8');
  }
  // Give the home page the same navigation as the new pages.
  const nav = firstNav(fs.readFileSync(path.join(out, done[0].file), 'utf8'));
  let home = index;
  if (nav && firstNav(home)) {
    home = home.replace(firstNav(home), () => nav);
  } else {
    const links = [{ file: 'index.html', name: 'Home' }, ...done].map((p) => `<a href="${p.file}" style="margin:0 .6rem">${p.name.replace(/[<>&"]/g, '')}</a>`).join('');
    home = home.replace(/<\/body>/i, () => `<nav style="text-align:center;padding:1.5rem 0">${links}</nav>\n</body>`);
  }
  fs.writeFileSync(path.join(out, 'index.html'), home, 'utf8');
  return done;
}

async function modify(req, engine) {
  const current = fs.readFileSync(path.join(req.base_dir, 'index.html'), 'utf8');
  phase = 'redesign';
  const html = await engine.bespoke.redesignBespokeHtml({ currentHtml: current, feedbackText: req.feedback,
    brandName: req.brand_name || 'Website' });
  if (!fromModel(phase, html) || html === current) {
    return { ok: false, error: failure(phase, 'The AI did not apply the change') };
  }
  fs.writeFileSync(path.join(req.out_dir, 'index.html'), html, 'utf8');
  // A change rewrites the home page; the site's other pages carry over unchanged.
  const kept = [];
  for (const f of fs.readdirSync(req.base_dir)) {
    if (/^[a-z][a-z0-9-]{1,19}\.html$/.test(f) && !RESERVED_PAGES.has(f.slice(0, -5))) {
      fs.copyFileSync(path.join(req.base_dir, f), path.join(req.out_dir, f));
      kept.push({ file: f, option: 1, name: '', inspiration: '' });
    }
  }
  return { ok: true, brand_name: req.brand_name || '', pages: [{ file: 'index.html', option: 1, name: '', inspiration: '' }, ...kept],
    truncated_input: current.length > 8000 };
}

async function main() {
  let result;
  try {
    const req = JSON.parse(await readStdin());
    if (!req.engine_dir || !req.out_dir) throw new Error('engine_dir and out_dir are required');
    const engine = await loadEngine(req.engine_dir);
    if (req.op === 'probe') result = { ok: true };
    else if (req.op === 'generate') result = await generate(req, engine);
    else if (req.op === 'modify') result = await modify(req, engine);
    else throw new Error(`unknown op: ${req.op}`);
  } catch (e) {
    result = { ok: false, error: sanitize(e?.message || e) };
  }
  result.calls = calls;
  // Exit explicitly: Gemini requests abandoned after the page deadline must not keep the build open.
  process.stdout.write(JSON.stringify(result) + '\n', () => process.exit(0));
}

main();
