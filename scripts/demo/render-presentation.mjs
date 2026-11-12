// Render a frozen local presentation snapshot. No HTTP, model, or server calls.
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import {
  copyFile, lstat, mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile,
} from 'node:fs/promises';
import { basename, dirname, extname, join, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const DIRECTORY = resolve('apps/web/public/presentation');
const ARCHIVE = resolve('apps/web/public/presentation-offline.zip');
const PLAYWRIGHT = process.env.PLAYWRIGHT_MODULE
  || 'playwright';
const CHROME = '/usr/bin/google-chrome';
const FILES = [
  'index.html', 'demo.html', 'speaker-script.html', 'slide-content.html',
  'demo-runbook.html', 'acceptance.html', 'acceptance-summary.json', 'demo-manifest.json',
];
const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.svg']);
const SLIDE_COUNT = 5;

function digest(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function assertPublicJson(value) {
  if (!value || typeof value !== 'object') return;
  for (const [key, child] of Object.entries(value)) {
    if (/(?:password|passwd|secret|authorization|credentials?)(?:_|$)/i.test(key)
        || /(?:^|_)(?:api_token|enrollment_token|access_token)(?:_|$)/i.test(key)) {
      throw new Error('A public JSON file contains a credential-shaped field');
    }
    if (typeof child === 'string' && /^https?:\/\//i.test(child)) {
      const url = new URL(child);
      if (url.username || url.password) throw new Error('A public URL embeds credentials');
    }
    assertPublicJson(child);
  }
}

async function regularFile(path) {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink()) {
    throw new Error('Offline package inputs must be regular, non-symlink files');
  }
}

async function assetFiles(directory, prefix = 'assets') {
  const info = await lstat(directory);
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new Error('Presentation assets must be a regular directory');
  }
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.name.startsWith('.') || entry.isSymbolicLink()) {
      throw new Error('Hidden or symlink assets are not permitted in the offline archive');
    }
    const relative = join(prefix, entry.name);
    if (entry.isDirectory()) {
      result.push(...await assetFiles(join(directory, entry.name), relative));
    } else if (entry.isFile() && IMAGE_EXTENSIONS.has(extname(entry.name).toLowerCase())) {
      result.push(relative);
    } else {
      throw new Error('Only image assets are permitted in the offline archive');
    }
  }
  return result.sort();
}

async function freezeInputs(packageDirectory) {
  const inputs = [...FILES, ...await assetFiles(join(DIRECTORY, 'assets'))];
  const hashes = new Map();
  for (const relative of inputs) {
    const source = join(DIRECTORY, relative);
    await regularFile(source);
    const data = await readFile(source);
    if (extname(relative) === '.json') assertPublicJson(JSON.parse(data));
    hashes.set(relative, digest(data));
    const destination = join(packageDirectory, relative);
    await mkdir(dirname(destination), { recursive: true });
    await writeFile(destination, data);
  }
  return hashes;
}

async function verifyInputsStillStable(hashes) {
  for (const [relative, expected] of hashes) {
    if (digest(await readFile(join(DIRECTORY, relative))) !== expected) {
      throw new Error('Presentation inputs changed during rendering; retry after HTML is stable');
    }
  }
}

async function pdfInfo(path) {
  const { stdout } = await exec('/usr/bin/pdfinfo', [path], { maxBuffer: 1024 * 1024 });
  const pages = Number(stdout.match(/^Pages:\s+(\d+)/m)?.[1]);
  const size = stdout.match(/^Page size:\s+([\d.]+) x ([\d.]+) pts/m);
  if (!Number.isInteger(pages) || pages < 1 || !size) {
    throw new Error('Generated PDF metadata could not be validated');
  }
  return { pages, width_pt: Number(size[1]), height_pt: Number(size[2]) };
}

async function layoutReport(page, media) {
  await page.emulateMedia({ media });
  const reports = [];
  for (let index = 0; index < SLIDE_COUNT; index += 1) {
    if (media === 'screen') {
      await page.evaluate(active => {
        document.querySelectorAll('.slide').forEach((slide, number) => {
          slide.classList.toggle('active', number === active);
        });
      }, index);
    }
    await page.evaluate(() => new Promise(requestAnimationFrame));
    reports.push(await page.evaluate(number => {
      const slide = document.querySelectorAll('.slide')[number];
      const box = slide.getBoundingClientRect();
      const overflow = [];
      if (slide.scrollWidth > slide.clientWidth + 1 || slide.scrollHeight > slide.clientHeight + 1) {
        overflow.push('slide-scroll-size');
      }
      for (const element of slide.querySelectorAll('*')) {
        const rect = element.getBoundingClientRect();
        if (!rect.width || !rect.height || getComputedStyle(element).visibility === 'hidden') continue;
        if (rect.left < box.left - 1 || rect.right > box.right + 1
            || rect.top < box.top - 1 || rect.bottom > box.bottom + 1) {
          overflow.push(element.tagName.toLowerCase() + '.' + element.className);
        }
      }
      const footer = slide.querySelector('.caption');
      if (footer) {
        const footerTop = footer.getBoundingClientRect().top;
        for (const child of slide.children) {
          if (child !== footer && child.getBoundingClientRect().bottom > footerTop + 1) {
            overflow.push('content-overlaps-caption:' + child.className);
          }
        }
      }
      return {
        slide: number + 1,
        media: matchMedia('print').matches ? 'print' : 'screen',
        width_px: Math.round(box.width * 100) / 100,
        height_px: Math.round(box.height * 100) / 100,
        overflow: [...new Set(overflow)],
      };
    }, index));
  }
  return reports;
}

async function renderDeck(context, packageDirectory) {
  const page = await context.newPage();
  try {
    await page.goto(pathToFileURL(join(packageDirectory, 'index.html')).href, { waitUntil: 'load' });
    await page.evaluate(() => document.fonts.ready);
    const slideCount = await page.locator('.deck > .slide').count();
    if (slideCount !== SLIDE_COUNT) throw new Error('Presentation must contain exactly five slides');
    const screen = await layoutReport(page, 'screen');
    const print = await layoutReport(page, 'print');
    const overflow = [...screen, ...print].filter(slide => slide.overflow.length);
    if (overflow.length) {
      console.error(JSON.stringify({ phase: 'layout-failed', slides: overflow }));
      throw new Error('Slide content overflows or overlaps its footer');
    }
    const destination = join(packageDirectory, 'slides.pdf');
    await page.pdf({
      path: destination,
      preferCSSPageSize: true,
      printBackground: true,
      displayHeaderFooter: false,
      tagged: true,
    });
    const info = await pdfInfo(destination);
    if (info.pages !== SLIDE_COUNT || Math.abs(info.width_pt - 960) > 1
        || Math.abs(info.height_pt - 540) > 1) {
      throw new Error('Slide PDF must have five landscape pages of approximately 960 × 540 pt');
    }
    return { pdf: 'slides.pdf', ...info, screen, print };
  } finally {
    await page.close();
  }
}

async function renderDocument(context, packageDirectory, source, destination) {
  const page = await context.newPage();
  try {
    await page.goto(pathToFileURL(join(packageDirectory, source)).href, { waitUntil: 'load' });
    await page.evaluate(() => document.fonts.ready);
    await page.emulateMedia({ media: 'print' });
    await page.pdf({
      path: join(packageDirectory, destination),
      preferCSSPageSize: true,
      format: 'A4',
      printBackground: true,
      displayHeaderFooter: false,
      tagged: true,
    });
    return { pdf: destination, ...await pdfInfo(join(packageDirectory, destination)) };
  } finally {
    await page.close();
  }
}

export async function main() {
  // Stage on the same filesystem as the final ZIP so its rename is atomic.
  // Only the explicit packageDirectory is archived, never its .storage parent.
  const temporaryParent = resolve('.storage/presentation');
  await mkdir(temporaryParent, { recursive: true });
  const temporary = await mkdtemp(join(temporaryParent, 'render-'));
  const packageDirectory = join(temporary, 'presentation');
  await mkdir(packageDirectory);
  let browser;
  try {
    const hashes = await freezeInputs(packageDirectory);
    const moduleSpecifier = PLAYWRIGHT.startsWith('/') || PLAYWRIGHT.startsWith('.')
      ? pathToFileURL(resolve(PLAYWRIGHT)).href : PLAYWRIGHT;
    const { chromium } = await import(moduleSpecifier);
    browser = await chromium.launch({
      executablePath: CHROME,
      headless: true,
      args: [
        '--disable-background-networking', '--disable-component-update',
        '--disable-domain-reliability', '--disable-sync', '--no-first-run',
        '--no-default-browser-check', '--host-resolver-rules=MAP * ~NOTFOUND',
        '--disable-features=MediaRouter,OptimizationHints,Translate',
      ],
    });
    const context = await browser.newContext({
      viewport: { width: 1280, height: 720 },
      offline: true,
      serviceWorkers: 'block',
    });
    const blocked = [];
    await context.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.protocol === 'file:') {
        const path = resolve(fileURLToPath(url));
        if (path.startsWith(packageDirectory + sep)) return route.continue();
      }
      if (url.protocol === 'data:' || url.protocol === 'about:') return route.continue();
      blocked.push({ protocol: url.protocol, resource_type: request.resourceType() });
      return route.abort();
    });
    const documents = [
      await renderDeck(context, packageDirectory),
      await renderDocument(context, packageDirectory, 'speaker-script.html', 'speaker-script.pdf'),
      await renderDocument(context, packageDirectory, 'demo-runbook.html', 'demo-runbook.pdf'),
    ];
    await context.close();
    await browser.close();
    browser = undefined;
    if (blocked.length) {
      console.error(JSON.stringify({ phase: 'external-resource-blocked', requests: blocked }));
      throw new Error('Presentation attempted to load a non-local resource');
    }
    await verifyInputsStillStable(hashes);
    const archive = join(temporary, 'presentation-offline.zip');
    await exec('/usr/bin/zip', ['-q', '-r', archive, 'presentation'], {
      cwd: temporary,
      maxBuffer: 1024 * 1024,
    });
    await exec('/usr/bin/zip', ['-T', archive], { cwd: temporary, maxBuffer: 1024 * 1024 });
    // Generated artifacts alone replace prior generated versions. Never include
    // .storage, .env, credential files, source repositories, or an old ZIP.
    for (const document of documents) {
      await copyFile(join(packageDirectory, document.pdf), join(DIRECTORY, document.pdf));
    }
    await rename(archive, ARCHIVE);
    console.log(JSON.stringify({
      phase: 'complete',
      local_files_only: true,
      viewport: { width: 1280, height: 720 },
      blocked_external_requests: blocked.length,
      documents,
      archive: basename(ARCHIVE),
      archive_file_count: hashes.size + documents.length,
    }));
    return { documents, archive: ARCHIVE };
  } finally {
    if (browser) await browser.close();
    // Exact freshly-created, task-owned temporary directory, not user data.
    await rm(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(JSON.stringify({ error: 'Offline presentation render failed', type: error.name }));
    process.exitCode = 1;
  });
}
