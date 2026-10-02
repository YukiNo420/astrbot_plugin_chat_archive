// Run with playwright-cli against the candidate frontend. Images are synthetic.
async (page) => {
    await page.route('**/api/history?**', async route => {
        const url = new URL(route.request().url());
        const sid = url.searchParams.get('session_id');
        const older = Number(url.searchParams.get('page')) > 1;
        const data = Array.from({ length: 52 }, (_, i) => ({
            id: older ? i + 1001 : i + 1, msg_id: String(older ? i + 1001 : i + 1), session_id: sid,
            user_id: sid.includes('group') ? 'diag-author' : `diag-${i % 2}`,
            sender_name: 'Scroll diagnostic', timestamp: 1790900000 + (older ? i - 52 : i) * 60,
            message: i < 40 || i >= 50 || (sid.includes('fail') && i % 2 === 1) || older ? `Diagnostic message ${i + 1}` :
                `[CQ:image,url=/static/scroll-check-${sid}-${i}.svg${sid.includes('fail') || sid.includes('known') ? ',width=320,height=640' : ''}]`,
        })).reverse();
        await route.fulfill({ json: { success: true, data, has_more: !older, next_cursor: 1 } });
    });
    await page.route('**/static/scroll-check-*.svg', async route => {
        await new Promise(resolve => setTimeout(resolve, 1200));
        if (route.request().url().includes('fail')) {
            await route.fulfill({ status: 404, body: 'Diagnostic missing image' });
        } else {
            await route.fulfill({ contentType: 'image/svg+xml', body:
                '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="640"><rect width="320" height="640" fill="#64748b"/></svg>' });
        }
    });
    await page.route('**/api/proxy/image?**', route => route.fulfill({ contentType: 'image/svg+xml', body:
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="#64748b"/></svg>' }));
    await page.reload();
    await page.evaluate(() => { window.__scrollChecks = []; });
    for (const name of ['bottom', 'known-bottom', 'read', 'group-read', 'search', 'fail-bottom', 'fail-read', 'mobile-bottom', 'mobile-read']) {
        await page.setViewportSize(name.includes('mobile') ? { width: 390, height: 844 } : { width: 1440, height: 960 });
        await page.evaluate(async name => {
            showAuth(false);
            document.body.classList.remove('global-view');
            activeSessionId = `check-${name}`;
            activeSearchKeyword = name === 'search' ? 'Diagnostic' : '';
            await fetchHistory();
        }, name);
        await page.waitForTimeout(250);
        if (name.includes('read')) {
            await page.evaluate(() => {
                const list = document.getElementById('messageList');
                const target = list.querySelector('.msg-bubble[data-msg-id="44"]');
                list.scrollTop += target.getBoundingClientRect().top - list.getBoundingClientRect().top - 100;
            });
        } else if (name === 'search') {
            await page.evaluate(() => scrollListToBottom(document.getElementById('messageList')));
        }
        await page.waitForTimeout(120);
        await page.evaluate(() => {
            const list = document.getElementById('messageList');
            const top = list.getBoundingClientRect().top;
            const target = [...list.querySelectorAll('.msg-bubble')].find(el => el.getBoundingClientRect().top >= top && el.getBoundingClientRect().top < list.getBoundingClientRect().bottom);
            window.__beforeScrollCheck = { id: target?.dataset.msgId, top: target?.getBoundingClientRect().top - top,
                bottom: list.scrollHeight - list.scrollTop - list.clientHeight, scrollTop: list.scrollTop };
        });
        await page.waitForTimeout(2600);
        await page.evaluate(name => {
            const list = document.getElementById('messageList');
            const before = window.__beforeScrollCheck;
            const target = list.querySelector(`.msg-bubble[data-msg-id="${before.id}"]`);
            const after = { top: target?.getBoundingClientRect().top - list.getBoundingClientRect().top,
                bottom: list.scrollHeight - list.scrollTop - list.clientHeight, scrollTop: list.scrollTop };
            const pass = name.endsWith('bottom') ? after.bottom <= 2 : Math.abs(after.top - before.top) <= 2;
            window.__scrollChecks.push({ name, pass, before, after, imagesLoaded: list.querySelectorAll('.msg-image.loaded').length });
        }, name);
    }
    const failures = await page.evaluate(() => window.__scrollChecks.filter(check => !check.pass).map(check => check.name));
    if (failures.length) throw new Error('Timeline regressions: ' + failures.join(', '));
}
