// Run after frontend_timeline.browser.js with candidate assets and synthetic APIs.
async (page) => {
    const checks = [];
    const errors = [];
    const requests = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
        window.__managementDialogs = [];
        window.confirm = message => { window.__managementDialogs.push({type:'confirm',message}); return true; };
        window.prompt = message => { window.__managementDialogs.push({type:'prompt',message}); return null; };
    });
    await page.route('**/api/manage/**', async route => {
        const url = new URL(route.request().url());
        const body = route.request().method() === 'POST' ? route.request().postDataJSON() : null;
        requests.push({path:url.pathname, body, session:url.searchParams.get('session_id')});
        if (url.pathname.endsWith('/storage')) return route.fulfill({json:{active:{count:2000},trash:{count:3}}});
        if (url.pathname.endsWith('/trash')) return route.fulfill({json:{operations:[{operation_id:'synthetic-trash',count:3,created_at:1790900000}]}});
        if (url.pathname.endsWith('/preview')) {
            if (body.session_id === 'timeline-media') await new Promise(resolve => setTimeout(resolve, 250));
            return route.fulfill({json:{session_id:body.session_id,preview_token:'synthetic-preview',count:10,matched_count:42}});
        }
        return route.fulfill({json:{success:true,messages:[]}});
    });
    async function check(name, predicate) {
        checks.push({name,pass:await predicate()});
    }
    await page.setViewportSize({width:1440,height:960});
    await page.goto('http://127.0.0.1:8090/?session_id=timeline-demo');
    await page.waitForSelector('.msg-bubble');
    await check('removed top shortcuts and bottom footer',()=>page.evaluate(()=>!document.querySelector('#home-btn,#manage-btn,.timeline-footer,#refreshTimelineBtn,#timelineStatus')));
    await check('desktop title, search, details order',()=>page.evaluate(()=>{
        const boxes=['#activeSessionId','.search-area','#btn-analysis'].map(s=>document.querySelector(s).getBoundingClientRect());
        return boxes[0].right<=boxes[1].left&&boxes[1].right<=boxes[2].left&&Math.abs(boxes[1].top-boxes[2].top)<15;
    }));
    await page.screenshot({path:'output/playwright/layout-settings/desktop.png'});
    for (const width of [360,390,768]) {
        await page.setViewportSize({width,height:844});
        await check(`mobile two-row header at ${width}px`,()=>page.evaluate(()=>{
            const [sessions,title,details,search]=['#btn-sidebar','#activeSessionId','#btn-analysis','.search-area'].map(s=>document.querySelector(s).getBoundingClientRect());
            return sessions.right<=title.left&&title.right<=details.left&&Math.abs(sessions.top-details.top)<2&&search.top>=Math.max(sessions.bottom,title.bottom,details.bottom)&&search.width>innerWidth-32&&document.documentElement.scrollWidth<=innerWidth;
        }));
    }
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'output/playwright/layout-settings/mobile.png'});
    await page.locator('#btn-sidebar').click();
    await page.locator('#settings-btn').click();
    await page.locator('#settings-management').click();
    await page.waitForSelector('#manage-trash button');
    await check('settings second page selects current conversation',()=>page.evaluate(()=>document.getElementById('manage-session-select').value==='timeline-demo'&&new URLSearchParams(location.search).get('section')==='messages'&&document.getElementById('activeSessionId').textContent==='消息管理'));
    await page.locator('#manage-preview').click();
    await page.waitForFunction(()=>!document.getElementById('manage-confirm').disabled);
    await check('preview only selected conversation',async()=>requests.filter(r=>r.path.endsWith('/preview')).at(-1).body.session_id==='timeline-demo');
    await page.locator('#manage-session-select').selectOption('timeline-media');
    await check('switching conversation invalidates preview',()=>page.locator('#manage-confirm').isDisabled());
    await page.locator('#manage-preview').click();
    await check('scope locked during preview',()=>page.locator('#manage-session-select').isDisabled());
    await page.waitForFunction(()=>!document.getElementById('manage-confirm').disabled);
    await check('preview tracks changed conversation',async()=>requests.filter(r=>r.path.endsWith('/preview')).at(-1).body.session_id==='timeline-media');
    await page.screenshot({path:'output/playwright/layout-settings/management-mobile.png'});
    await check('management fits mobile viewport',()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth&&document.getElementById('messageList').scrollWidth<=document.getElementById('messageList').clientWidth));
    await page.setViewportSize({width:1440,height:960});
    await page.screenshot({path:'output/playwright/layout-settings/management-desktop.png'});
    const download = page.waitForEvent('download');
    await page.locator('#manage-export').click();
    await download;
    await page.waitForFunction(()=>!document.getElementById('manage-confirm').disabled);
    await check('export uses current preview',async()=>requests.some(r=>r.path.endsWith('/export')&&r.body.preview_token==='synthetic-preview'));
    await page.locator('#manage-confirm').click();
    await page.waitForFunction(()=>document.getElementById('manage-status').textContent==='已移入回收站。'&&!document.getElementById('manage-session-select').disabled);
    await check('mock deletion retains settings and correct scope',async()=>await page.locator('#management-page').isVisible()&&requests.filter(r=>r.path.endsWith('/delete')).at(-1).body.confirm_session_id==='timeline-media');
    await page.locator('#manage-trash button').click();
    await page.waitForFunction(()=>document.getElementById('manage-status').textContent==='已恢复。'&&!document.getElementById('manage-session-select').disabled);
    await check('mock restore uses selected conversation',async()=>requests.filter(r=>r.path.endsWith('/restore')).at(-1).body.session_id==='timeline-media');
    await page.locator('#manage-mode').selectOption('permanent');
    await page.locator('#manage-preview').click();
    await page.waitForFunction(()=>!document.getElementById('manage-confirm').disabled);
    const before = requests.filter(r=>r.path.endsWith('/delete')).length;
    await page.locator('#manage-confirm').click();
    await check('permanent deletion requires typed confirmation',async()=>requests.filter(r=>r.path.endsWith('/delete')).length===before && await page.evaluate(()=>window.__managementDialogs.some(dialog=>dialog.type==='prompt')));
    await page.locator('#manage-before').fill('2026-09-01T12:00');
    await check('changed cutoff invalidates preview',()=>page.locator('#manage-confirm').isDisabled());
    await page.locator('#settings-back').click();
    await check('back returns to settings root',()=>page.evaluate(()=>!!document.getElementById('show-message-media')&&!document.getElementById('management-page')&&!new URLSearchParams(location.search).has('section')));
    await page.goBack();
    await page.waitForSelector('#management-page');
    await check('browser back restores selected management conversation',()=>page.evaluate(()=>document.getElementById('manage-session-select').value==='timeline-media'));
    await page.reload();
    await page.waitForSelector('#management-page');
    await check('reload retains management section and conversation',()=>page.evaluate(()=>document.getElementById('manage-session-select').value==='timeline-media'));
    await page.locator('#manage-session-select').selectOption('');
    await check('no conversation disables management actions',()=>page.evaluate(()=>['manage-preview','manage-confirm','manage-export'].every(id=>document.getElementById(id).disabled)));
    await page.reload();
    await page.waitForSelector('#management-page');
    await check('reload preserves empty conversation choice',()=>page.evaluate(()=>document.getElementById('manage-session-select').value===''));
    await page.locator('#settings-back').click();
    await page.locator('#settings-back').click();
    await page.waitForSelector('.msg-bubble');
    await check('return to conversation clears settings URL',()=>page.evaluate(()=>!new URLSearchParams(location.search).has('view')&&!new URLSearchParams(location.search).has('section')));
    await check('message footer has no management shortcut',()=>page.evaluate(()=>!document.querySelector('.message-manage')));
    await page.locator('#settings-btn').click();
    await page.locator('#settings-management').click();
    await page.locator('#manage-session-select').selectOption('timeline-media');
    await page.locator('#manage-preview').click();
    await page.locator('#settings-back').click();
    await page.waitForTimeout(400);
    await check('late management response cannot overwrite settings root',()=>page.evaluate(()=>!!document.getElementById('show-message-media')&&!document.getElementById('manage-status')));
    checks.push({name:'no script errors',pass:errors.length===0,errors});
    await page.evaluate(checks=>window.__navigationChecks=checks,checks);
    if(checks.some(c=>!c.pass)) throw new Error(JSON.stringify(checks.filter(c=>!c.pass)));
    return {checks:checks.length,passed:checks.filter(c=>c.pass).length};
}
