// Run after the synthetic archive fixture; all avatar responses are synthetic.
async (page) => {
    const checks = [], errors = [];
    let version = 0, memberResponses = 0, historyRequests = 0;
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {if (new URL(request.url()).pathname === '/api/history') historyRequests++;});
    const members = () => (version === 3 ? [] : version === 2 ? [2, 0, 3] : version === 1 ? [2, 1, 0] : [0, 1, 2])
        .map(i => ({user_id: 'demo-'+i, sender_name: '测试成员 '+i, count: 100+version*10-i,
            platform_name: 'discord', avatar_url: 'https://avatars.example.test/avatar-'+i+'.svg'}));
    await page.route('**/api/proxy/image?**', route => route.fulfill({contentType: 'image/svg+xml',
        headers: {'cache-control': 'public, max-age=3600'}, body: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="#6366f1"/></svg>'}));
    await page.route('**/api/stats?**', async route => {
        const individual = new URL(route.request().url()).searchParams.get('user_id');
        await new Promise(resolve => setTimeout(resolve, 40));
        return route.fulfill({json: {success: true, data: {total_messages: 2000+version,
            today_messages: 50, time_distribution: Array(12).fill(10),
            ...(individual ? {message_types: [{name:'文字',value:10}]} : {top_users: members()})}}});
    });
    await page.route('**/api/members?**', async route => {
        await new Promise(resolve => setTimeout(resolve, 120));
        const users = members(); memberResponses++;
        return route.fulfill({json: {success:true, data:{members:users,total:users.length,has_more:false,total_exact:true}}});
    });
    await page.setViewportSize({width:390,height:844});
    await page.reload();
    await page.waitForSelector('.rank-avatar', {state:'attached'});
    await page.locator('#btn-analysis').tap();
    await page.waitForTimeout(650);
    await page.waitForFunction(() => [...document.querySelectorAll('.rank-avatar')].every(img => img.complete && img.naturalWidth));
    await page.evaluate(() => {
        window.__rankAvatars = new Map([...document.querySelectorAll('.rank-item')].map(row => [row.dataset.userId, row.querySelector('img')]));
        window.__sidebarAvatars = [...document.querySelectorAll('.member-mini-avatar')];
        window.__avatarMutations = [];
        window.__avatarObserver = new MutationObserver(records => __avatarMutations.push(...records));
        __avatarObserver.observe(document.getElementById('analysisContent'), {subtree:true,childList:true,attributes:true,attributeFilter:['src']});
    });
    await page.keyboard.press('Escape');
    version = 1;
    await page.locator('#btn-analysis').tap();
    await page.waitForTimeout(650);
    checks.push(await page.evaluate(() => ({name:'refresh and ranking changes preserve decoded avatars',
        pass:[...__rankAvatars].every(([id,img]) => img.isConnected && img.complete && img.naturalWidth
            && document.querySelector('.rank-item[data-user-id="'+id+'"]').querySelector('img')===img)
            && !__avatarMutations.some(record => record.type==='attributes' && [...__rankAvatars.values()].includes(record.target))})));
    checks.push(await page.evaluate(() => ({name:'statistics, order and counts still update',
        pass:document.querySelector('.rank-item').dataset.userId==='demo-2'
            && document.querySelector('.rank-count').textContent.includes('108')
            && document.querySelector('[data-od-id="session-stat-total"] .value').textContent==='2,001'})));
    checks.push(await page.evaluate(() => ({name:'sidebar avatars survive member refresh', pass:__sidebarAvatars.every(img=>img.isConnected)})));
    for (let i=0;i<2;i++) {
        await page.keyboard.press('Escape');
        await page.locator('#btn-analysis').tap();
        await page.waitForTimeout(600);
    }
    checks.push(await page.evaluate(() => ({name:'repeated opens preserve the same avatars',pass:[...__rankAvatars.values()].every(img=>img.isConnected)})));
    historyRequests=0;
    await page.locator('.rank-item[data-user-id="demo-2"]').focus();
    await page.keyboard.press('Enter');
    await page.waitForTimeout(400);
    checks.push({name:'reused rank row has one keyboard activation',pass:historyRequests===1 && await page.evaluate(()=>activeUserId==='demo-2')});
    await page.locator('#btn-sidebar').tap();
    await page.waitForTimeout(250);
    historyRequests=0;
    await page.locator('.sub-menu-item[data-user-id="demo-2"]').focus();
    await page.keyboard.press('Enter');
    await page.waitForTimeout(400);
    checks.push({name:'reused sidebar member toggles the filter once',pass:historyRequests===1 && await page.evaluate(()=>activeUserId==='')});
    version=2;
    await page.locator('#btn-analysis').tap();
    await page.waitForTimeout(600);
    checks.push(await page.evaluate(() => ({name:'added and removed members reconcile correctly',
        pass:document.querySelectorAll('.rank-item').length===3 && !document.querySelector('.rank-item[data-user-id="demo-1"]')
            && !!document.querySelector('.rank-item[data-user-id="demo-3"]')})));
    version=3;
    await page.keyboard.press('Escape');
    await page.locator('#btn-analysis').tap();
    await page.waitForTimeout(600);
    checks.push({name:'empty response removes old members',pass:await page.locator('.rank-item').count()===0});
    await page.evaluate(() => __avatarObserver.disconnect());
    checks.push({name:'no browser errors',pass:errors.length===0,errors});
    const failed=checks.filter(check=>!check.pass);
    if(failed.length)throw new Error(JSON.stringify({failed,memberResponses,historyRequests}));
    return {passed:checks.length,checks};
}
