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
const GEMINI_HOST = 'generativelanguage.googleapis.com';
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === 'string' ? input : input.url);
  if (url.hostname !== GEMINI_HOST) return nativeFetch(input, init);
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

const calls = [];
const busyModels = new Set();
const TRANSIENT = /\((?:429|500|502|503|504)\)|UNAVAILABLE|RESOURCE_EXHAUSTED|overloaded|high demand|fetch failed|ECONNRESET|ETIMEDOUT|socket hang up/i;
let phase = 'setup';
const aiText = {}; // phase -> last raw model text

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
  const original = geminiClient.generateContent.bind(geminiClient);
  geminiClient.generateContent = async (prompt, system, config) => {
    if (!geminiClient.apiKey) {
      const err = new Error('GEMINI_API_KEY is not set for the website engine');
      calls.push({ phase, ok: false, error: err.message });
      throw err;
    }
    if (phase === 'research' && !config) {
      // weave parses this reply as JSON but doesn't ask for JSON; without JSON mode Gemini sometimes returns
      // invalid JSON and weave silently switches to canned directions.
      config = { responseMimeType: 'application/json', responseJsonSchema: RESEARCH_SCHEMA };
    }
    // Gemini regularly answers 503 "high demand" (also 429/500). Each model gets a retry, then the next model in
    // GEMINI_FALLBACK_MODELS takes over: different model families have separate capacity.
    const primary = geminiClient.model;
    const all = [...new Set([primary, ...String(process.env.GEMINI_FALLBACK_MODELS || '').split(',').map((m) => m.trim()).filter(Boolean)])];
    // Models that were overloaded earlier in this build go last, so later pages don't wait on them again.
    const models = [...all.filter((m) => !busyModels.has(m)), ...all.filter((m) => busyModels.has(m))];
    const pause = Number(process.env.GENESIS_RESEARCH_RETRY_MS ?? 5000);
    let lastError;
    try {
      for (const model of models) {
        for (let attempt = 1; attempt <= 2; attempt++) {
          geminiClient.model = model;
          const started = Date.now();
          try {
            const res = await original(prompt, system, config);
            const usage = res?.raw?.usageMetadata || {};
            calls.push({ phase, ok: true, model, ms: Date.now() - started,
              input_tokens: usage.promptTokenCount ?? null, output_tokens: usage.candidatesTokenCount ?? null });
            aiText[phase] = res?.text || '';
            return res;
          } catch (e) {
            calls.push({ phase, ok: false, model, ms: Date.now() - started, error: sanitize(e.message) });
            lastError = e;
            if (!TRANSIENT.test(String(e?.message))) throw e;
            if (attempt === 2) busyModels.add(model);
            log(`Gemini busy on ${model} (${phase}); ${attempt < 2 ? 'retrying' : 'trying the next model'}`);
            if (attempt < 2) await new Promise((r) => setTimeout(r, pause * attempt));
          }
        }
      }
      throw lastError;
    } finally {
      geminiClient.model = primary;
    }
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
    fs.writeFileSync(path.join(out, file), html, 'utf8');
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

async function addPages(req, engine, topic, out, extra) {
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
  const done = [];
  for (const p of wanted) {
    phase = `page-${p.file}`;
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
      continue;
    }
    const html = stripFences(res?.text);
    if (!/<html[\s>]/i.test(html) || !/<\/html>/i.test(html)) continue;
    fs.writeFileSync(path.join(out, p.file), html, 'utf8');
    done.push({ file: p.file, option: 1, name: p.title, inspiration: '' });
  }
  if (!done.length) return [];
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
  process.stdout.write(JSON.stringify(result) + '\n');
}

main();
