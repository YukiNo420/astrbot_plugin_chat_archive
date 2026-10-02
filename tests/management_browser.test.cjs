const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const output = process.env.ARCHIVE_TEST_OUTPUT_DIR || require('node:os').tmpdir();
const server = spawn(process.env.ARCHIVE_TEST_PYTHON || 'python', [root + '/tests/browser_fixture.py'], { stdio: ['ignore', 'ignore', 'pipe'] });
let stderr = '';
server.stderr.on('data', data => { stderr += data; });
const base = 'http://127.0.0.1:18993';
const checks = [];
let browser;

async function run() {
    await fs.mkdir(output, { recursive: true });
    let ready = false;
    for (let i = 0; i < 100; i++) {
        try { if ((await fetch(base + '/')).ok) { ready = true; break; } } catch {}
        if (server.exitCode !== null) throw Error(stderr);
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert(ready, 'Synthetic server did not start');
    browser = await chromium.launch({ headless: true, executablePath: process.env.ARCHIVE_TEST_CHROME || (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : undefined) });
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, timezoneId: 'UTC', acceptDownloads: true });
    await context.route('**/*', route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    assert.equal((await context.request.post(base + '/api/manage/preview', { data: { session_id: 'qq:group:42' } })).status(), 401);
    await page.goto(base + '/?session_id=qq%3Agroup%3A42');
    await page.locator('#api-key-input').fill('synthetic-browser-only');
    await page.locator('#login-btn').click();
    await page.locator('.msg-bubble').filter({ hasText: 'UI message one' }).waitFor();
    const state = async () => (await context.request.get(base + '/api/test/state')).json();
    const initial = await state();
    assert.equal(initial.rows.length, 3);
    assert.equal(initial.retention_disabled_by_default, true);
    assert.equal((await context.request.post(base + '/api/manage/preview', { data: { session_id: 'qq:group:42' }, headers: { Origin: 'https://evil.example' } })).status(), 403);
    checks.push('login works; anonymous and foreign-origin management denied');

    await page.locator('#settings-btn').click();
    await page.locator('#settings-management').click();
    const previewCount = async count => page.waitForFunction(value => document.querySelector('#manage-status').textContent.includes('匹配 ' + value.toLocaleString() + ' 条'), count);
    await previewCount(2);
    assert.equal(await page.locator('#manage-preview').count(), 0);
    assert.equal(await page.locator('#manage-before').count(), 0);
    await page.locator('#manage-end').fill('1970-01-01T00:02');
    assert.equal(await page.locator('#manage-confirm').isDisabled(), true);
    await previewCount(1);
    page.once('dialog', dialog => dialog.dismiss());
    await page.locator('#manage-confirm').click();
    assert.equal((await state()).rows.length, 3);
    checks.push('settings management automatically previews the range; cancel preserves rows');

    const downloadPromise = page.waitForEvent('download');
    await page.locator('#manage-export').click();
    const download = await downloadPromise;
    const backupPath = path.join(output, 'synthetic-message-backup.json');
    await download.saveAs(backupPath);
    const backup = JSON.parse(await fs.readFile(backupPath, 'utf8'));
    assert.equal(backup.messages.length, 1);
    assert.match(backup.messages[0].message, /UI message one/);
    assert.equal(backup.attachments_included, false);
    await page.waitForFunction(() => !document.querySelector('#manage-confirm').disabled);
    checks.push('JSON download matches the selected range and excludes attachments');

    let deletes = 0;
    page.on('request', request => { if (request.url().endsWith('/api/manage/delete')) deletes++; });
    await page.route('**/api/manage/delete', async route => { await new Promise(resolve => setTimeout(resolve, 150)); await route.continue(); });
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#manage-confirm').click();
    await page.locator('#manage-confirm').dispatchEvent('click');
    await page.getByRole('button', { name: /恢复 1 条/ }).waitFor();
    const trashed = await state();
    assert.equal(trashed.rows.length, 2);
    assert.equal(trashed.trash_count, 1);
    assert.equal(trashed.cache_bytes, initial.cache_bytes);
    assert.equal(deletes, 1);
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: /恢复 1 条/ }).click();
    await previewCount(1);
    assert.equal((await state()).rows.length, 3);
    checks.push('delete commits once; restore preserves statistics and shared attachments');

    await page.locator('#manage-end').fill('');
    await previewCount(2);
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#manage-confirm').click();
    await page.getByRole('button', { name: /恢复 2 条/ }).waitFor();
    assert.deepEqual((await state()).rows.map(row => row.session_id), ['tg:group:42']);
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: /恢复 2 条/ }).click();
    await previewCount(2);
    assert.equal((await state()).rows.length, 3);
    checks.push('whole-session deletion and restoration leave the other platform untouched');

    await page.locator('#manage-end').fill('1970-01-01T00:02');
    await previewCount(1);
    await page.locator('#manage-mode').selectOption('permanent');
    page.once('dialog', dialog => dialog.accept('wrong'));
    await page.locator('#manage-confirm').click();
    assert.equal((await state()).rows.length, 3);
    page.once('dialog', dialog => dialog.accept('永久删除'));
    await page.locator('#manage-confirm').click();
    await page.waitForFunction(() => document.querySelector('#manage-status').textContent.includes('没有匹配消息'));
    const final = await state();
    assert.equal(final.rows.length, 2);
    assert.equal(final.trash_count, 0);
    assert.equal(final.cache_bytes, initial.cache_bytes);
    assert.equal(final.stats.find(row => row.session_id === 'qq:group:42').message_count, 1);
    checks.push('permanent deletion requires the exact confirmation and preserves attachments');

    for (const width of [320, 390, 1440]) {
        await page.setViewportSize({ width, height: 844 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
    await page.screenshot({ path: path.join(output, 'management-browser.png'), fullPage: true });
    await page.locator('#settings-back').click();
    await page.locator('#settings-back').click();
    await page.locator('.msg-bubble').filter({ hasText: 'UI message two' }).waitFor();
    await page.locator('.session-item[data-session-id="tg:group:42"]').click();
    await page.locator('.msg-bubble').filter({ hasText: 'UI message other' }).waitFor();
    assert.deepEqual(errors, []);
    checks.push('mobile layouts do not overflow; returning to the conversation and switching sessions work');
    await fs.writeFile(path.join(output, 'management-browser-results.json'), JSON.stringify({ synthetic_only: true, checks, page_errors: errors }, null, 2));
    console.log(JSON.stringify({ passed: checks.length, checks }, null, 2));
}

run().catch(error => { console.error(error.stack); console.error(stderr); process.exitCode = 1; }).finally(async () => {
    if (browser) await browser.close();
    if (server.exitCode === null) { const stopped = once(server, 'exit'); server.kill('SIGTERM'); await stopped; }
});
