import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createHash, X509Certificate } from 'node:crypto';

const {chromium} = await import(process.env.PLAYWRIGHT_MODULE);
assert(process.env.REVIEWER_TEST_PASSWORD, 'Local test login password must be supplied privately');
const root = process.cwd();
const directory = '.storage/reviewer-internal-access';
const credentials = JSON.parse(await readFile(`${directory}/credentials.json`, 'utf8'));
const certificate = new X509Certificate(await readFile(`${directory}/tls.crt`));
const pin = createHash('sha256').update(certificate.publicKey.export({type:'spki',format:'der'})).digest('base64');
const base = 'https://192.0.2.122:15174/confluence';
const browser = await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,
  args:['--ignore-certificate-errors-spki-list=' + pin]});
const context = await browser.newContext({httpCredentials:credentials,viewport:{width:1440,height:960}});
const page = await context.newPage();
const requests = [];
const loginResponses = [];
const failedResponses = [];
page.on('requestfailed',request => failedResponses.push({path:new URL(request.url()).pathname,error:request.failure()?.errorText}));
page.on('response', response => {
  const url = new URL(response.url());
  if(response.status()>=400) failedResponses.push({path:url.pathname,status:response.status(),method:response.request().method()});
  if (/login|authenticate|\/auth\//i.test(url.pathname)) {
    loginResponses.push({path:url.pathname,status:response.status(),method:response.request().method()});
  }
});
page.on('request', request => {
  if (request.url().includes('review-draft')) requests.push(new URL(request.url()).origin);
});
let status = JSON.parse(await readFile('.storage/presentation/reviewer-relay-status.json','utf8'));
try {
  await page.goto(base + '/pages/editpage.action?pageId=5636125');
  await page.getByRole('textbox',{name:'Username',exact:true}).fill(process.env.REVIEWER_TEST_LOGIN || 'admin');
  await page.getByRole('textbox',{name:'Password',exact:true}).fill(process.env.REVIEWER_TEST_PASSWORD);
  await page.getByRole('button',{name:'Log in',exact:true}).click();
  await page.getByRole('button',{name:'Проверить',exact:true}).waitFor({timeout:30000});
  await page.waitForFunction(() => window.tinymce?.activeEditor?.getBody());
  const fixture = await page.evaluate(() => ({
    pageId:String(AJS.Meta.get('page-id')),html:tinymce.activeEditor.getContent(),
    tables:tinymce.activeEditor.getBody().querySelectorAll('table').length,
    macros:tinymce.activeEditor.getBody().querySelectorAll('[data-macro-name]').length,
  }));
  assert.equal(fixture.pageId,'5636125');
  assert(fixture.html.includes('systmctl'));
  assert.equal(new URL(page.url()).origin,'https://192.0.2.122:15174');
  // Browser fetch uses the same pinned TLS certificate and authenticated origin.
  // Playwright's separate Node request client does not inherit Chrome's SPKI pin.
  const publishedVersion = () => page.evaluate(async () => {
    const response = await fetch('/confluence/rest/api/content/5636125?expand=version',{credentials:'same-origin'});
    if (response.status !== 200) throw new Error('Published-version read failed: '+response.status);
    return (await response.json()).version.number;
  });
  const publishedBefore = await publishedVersion();
  status.remote_editor_verified = true;
  status.remote_ui_certificate_spki_pinned = true;
  status.remote_ui_arbitrary_certificate_bypass = false;
  console.log(JSON.stringify({phase:'remote_editor',verified:true,page_id:fixture.pageId,published_version:publishedBefore}));
  if(process.argv.includes('--diagnose')) {
    await page.waitForTimeout(12000);
    console.log(JSON.stringify({phase:'editor_diagnostics',failed_responses:failedResponses}));
  }
  if (process.argv.includes('--review')) {
    const closeFlags = page.locator('#aui-flag-container').getByRole('button',{name:'Close',exact:true});
    while(await closeFlags.count()) await closeFlags.first().click();
    await page.getByRole('button',{name:'Проверить',exact:true}).click({trial:true,timeout:30000});
    const started = Date.now();
    const responsePromise = page.waitForResponse(response => response.url().includes('review-draft') && response.request().method() === 'POST',{timeout:240000});
    responsePromise.catch(()=>{});
    await page.getByRole('button',{name:'Проверить',exact:true}).click();
    console.log(JSON.stringify({phase:'remote_review',started:true}));
    const response = await responsePromise;
    assert.equal(response.status(),200);
    status.remote_review_request_ms = Date.now() - started;
    await page.getByRole('button',{name:'Linux diagnostics · фрагмент 3',exact:true}).first().waitFor({timeout:10000});
    await page.screenshot({path:root+'/apps/web/public/presentation/assets/reviewer-remote-result.png'});
    await page.getByRole('button',{name:'Linux diagnostics · фрагмент 3',exact:true}).first().click();
    assert.equal(await page.getByRole('textbox',{name:'Замена',exact:true}).inputValue(),'systemctl');
    await page.getByRole('button',{name:'Применить',exact:true}).click();
    const after = await page.evaluate(() => tinymce.activeEditor.getContent());
    const iframe = await page.evaluate(() => tinymce.activeEditor.id+'_ifr');
    await page.frameLocator('#'+iframe).locator('body').press('Control+z');
    const restored = await page.evaluate(() => ({html:tinymce.activeEditor.getContent(),
      tables:tinymce.activeEditor.getBody().querySelectorAll('table').length,
      macros:tinymce.activeEditor.getBody().querySelectorAll('[data-macro-name]').length}));
    assert.equal(after,fixture.html.replace('systmctl','systemctl'));
    assert.equal(restored.html,fixture.html);
    assert.equal(restored.tables,fixture.tables);
    assert.equal(restored.macros,fixture.macros);
    const publishedAfter = await publishedVersion();
    assert.equal(publishedAfter,publishedBefore);
    assert(requests.every(origin => origin === 'https://192.0.2.122:15174'));
    status.remote_review_verified = true;
    status.remote_apply_minimal_verified = true;
    status.remote_undo_full_html_verified = true;
    status.published_version_unchanged = true;
    status.saved_result = 'assets/reviewer-remote-result.png';
    console.log(JSON.stringify({phase:'remote_review',verified:true,request_ms:status.remote_review_request_ms,
      minimal_apply:true,full_html_undo:true,published_version_unchanged:true}));
  }
} catch (error) {
  await page.screenshot({path:root+'/.playwright-mcp/reviewer-relay-diagnostic.png'});
  console.error(JSON.stringify({phase:'remote_ui_failed',url:page.url().split('?')[0],title:await page.title(),error_type:error.name,
    error_summary:String(error.message).split('\n').slice(0,3).join('\n').replace(/https?:\/\/[^\s)]+/g,'[url]').slice(0,350),
    visible_buttons:await page.getByRole('button').allTextContents(),login_responses:loginResponses,failed_responses:failedResponses}));
  process.exitCode = 1;
} finally {
  status.checked_at = new Date().toISOString();
  status.remote_ui_checked_at = status.checked_at;
  await writeFile('.storage/presentation/reviewer-relay-status.json',JSON.stringify(status,null,2)+'\n',{mode:0o600});
  await browser.close();
}
