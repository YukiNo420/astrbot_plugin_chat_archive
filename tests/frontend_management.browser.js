async (page) => {
    await page.unrouteAll({behavior:'wait'});
    await page.setViewportSize({width:390,height:844});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const avatar = '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" rx="8" fill="#6366f1"/></svg>';
    await page.route('**/api/**', async route => {
        const url = new URL(route.request().url());
        const sid = url.searchParams.get('session_id') || 'timeline-demo';
        if (url.pathname === '/api/proxy/image') return route.fulfill({contentType:'image/svg+xml',body:avatar});
        let result = {success:true,data:{total_messages:2000,today_messages:50,top_users:[],hourly_distribution:Array(24).fill(0)}};
        if (url.pathname === '/api/auth/status') result = {success:true,configured:true,authenticated:true};
        if (url.pathname === '/api/sessions') result = {success:true,data:[
            {session_id:'timeline-demo',name:'产品讨论',message_type:'GroupMessage',platform_name:'qq',last_msg:'消息流重构与交互验证',last_time:1790900000},
            {session_id:'timeline-media',name:'设计与图片',message_type:'GroupMessage',platform_name:'discord',last_msg:'查看最新效果',last_time:1790900000},
        ]};
        if (url.pathname.includes('members')) result={success:true,data:{members:['小雪','林间','归档助手'].map((sender_name,i)=>({user_id:`demo-${i}`,sender_name,count:42-i*7,platform_name:'qq'})),total:3,total_exact:true,has_more:false}};
        if (url.pathname === '/api/history') {
            if (sid === 'timeline-slow') await new Promise(resolve => setTimeout(resolve, 600));
            const pageNumber = Number(url.searchParams.get('page') || 1);
            const end = 2000 - (pageNumber-1)*50;
            const data = Array.from({length:50}, (_,i) => {
                const id = end - 49 + i;
                return {id, msg_id:String(id), session_id:sid,user_id:`demo-${Math.floor(i/3)%3}`,
                    sender_name:['小雪','林间','归档助手'][Math.floor(i/3)%3], timestamp:1790900000+id*40,
                    message: id % 7 === 0 ? '这条记录包含较长的说明。\n' + '滚动时保留正在阅读的位置，图片加载完成以后也能继续阅读。'.repeat(4) : ['今天继续看消息列表的交互细节。','连续发言保持在一起，读起来更连贯。','收到，可以直接向上翻阅之前的记录。'][i%3],
                };
            }).reverse();
            result = {success:true, data, has_more:pageNumber < 4,next_cursor:end-49};
        }
        return route.fulfill({json:result});
    });
    const previews = [];
    const exports = [];
    const deletes = [];
    let delayNext = false;
    await page.route('**/api/manage/**', async route => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith('/preview')) {
            const body = route.request().postDataJSON();
            const number = previews.push(body);
            const count = body.start_ts > 2000000000 ? 0 : body.start_ts || body.end_ts ? 1201 : 2505;
            if (delayNext) { delayNext=false; await new Promise(resolve=>setTimeout(resolve,1000)); }
            return route.fulfill({json:{preview_token:count ? `synthetic-preview-${number}` : null,session_id:body.session_id,count:count,matched_count:count,expires_in:300}});
        }
        if (path.endsWith('/export')) {
            exports.push(route.request().postDataJSON());
            return route.fulfill({json:{format:'chat-archive-message-backup-v1',messages:Array.from({length:1201},(_,i)=>({id:i+1,message:'Synthetic export'})),attachments_included:false}});
        }
        if (path.endsWith('/delete')) { deletes.push(route.request().postDataJSON()); return route.fulfill({json:{count:route.request().postDataJSON().confirm_count,recoverable:true}}); }
        return route.fulfill({json:path.endsWith('/storage') ? {active:{count:2505},trash:{count:0}} : {operations:[]}});
    });
    await page.goto('http://127.0.0.1:8090/?session_id=timeline-demo');
    await page.waitForSelector('.msg-bubble');

    await page.locator('#btn-sidebar').click();
    await page.locator('#settings-btn').click();
    await page.locator('#settings-management').click();
    await page.waitForFunction(()=>document.querySelector('#manage-status')?.textContent==='匹配 2,505 条');
    if (await page.locator('#manage-preview').count()) throw new Error('Manual preview button remains');
    if (await page.locator('#manage-before').count()) throw new Error('Old cutoff field remains');
    const start = page.locator('#manage-start');
    const end = page.locator('#manage-end');
    const initial = previews.length;
    await start.fill('2026-10-01T10:00');
    if (await page.locator('#manage-export').isEnabled()) throw new Error('Stale export enabled while range is pending');
    await end.fill('2026-10-02T10:00');
    await page.waitForFunction(()=>document.querySelector('#manage-status').textContent==='匹配 1,201 条');
    if (previews.length!==initial+1) throw new Error('Input was not debounced');
    const dates = await page.evaluate(()=>[new Date(document.querySelector('#manage-start').value).getTime()/1000,new Date(document.querySelector('#manage-end').value).getTime()/1000]);
    if (previews.at(-1).start_ts!==dates[0] || previews.at(-1).end_ts!==dates[1]) throw new Error('Wrong date bounds');
    const beforeInvalid=previews.length;
    await start.fill('2026-10-03T10:00');
    await page.waitForTimeout(550);
    if (previews.length!==beforeInvalid || await page.locator('#manage-confirm').isEnabled()) throw new Error('Invalid range was accepted');
    if (await page.locator('#manage-status').textContent()!=='开始时间不能晚于结束时间') throw new Error('Missing invalid range feedback');
    delayNext=true;
    await start.fill('2026-10-01T09:00');
    await page.waitForTimeout(550);
    await start.fill('2026-10-01T08:00');
    await page.waitForFunction(()=>document.querySelector('#manage-export').disabled===false);
    await page.waitForTimeout(700);
    const lastToken=`synthetic-preview-${previews.length}`;
    const downloadPromise=page.waitForEvent('download');
    await page.locator('#manage-export').click();
    const download=await downloadPromise;
    await download.saveAs('output/playwright/management-download.json');
    if (exports.at(-1).preview_token!==lastToken) throw new Error('Stale preview won race');
    await page.waitForFunction(()=>document.querySelector('#manage-export').disabled===false);
    await start.fill('2040-01-01T00:00');
    await end.fill('');
    await page.waitForFunction(()=>document.querySelector('#manage-status').textContent==='没有匹配消息');
    if (await page.locator('#manage-export').isEnabled()) throw new Error('Empty range export enabled');
    await start.fill('');
    await page.waitForFunction(()=>document.querySelector('#manage-status').textContent==='匹配 2,505 条');
    if (previews.at(-1).start_ts!==0 || previews.at(-1).end_ts!==0) throw new Error('Cleared range not unlimited');
    await page.locator('#manage-session-select').selectOption('timeline-media');
    await page.waitForFunction(()=>document.querySelector('#manage-export').disabled===false);
    if (previews.at(-1).session_id!=='timeline-media') throw new Error('Wrong conversation after switch');
    await page.evaluate(() => { window.confirmationText = ''; window.confirm = text => { window.confirmationText=text; return false; }; });
    await page.locator('#manage-confirm').click();
    if (deletes.length) throw new Error('Cancelled confirmation sent a delete request');
    const confirmation = await page.evaluate(()=>window.confirmationText);
    if (!confirmation.includes('2,505') || !confirmation.includes('设计与图片') || !confirmation.includes('不限开始 至 不限结束')) throw new Error('Confirmation does not show the complete scope');
    await page.evaluate(() => { window.confirm = () => true; });
    await page.locator('#manage-confirm').click();
    await page.waitForFunction(()=>document.querySelector('#manage-export').disabled===false);
    if (deletes.length!==1 || deletes[0].confirm_session_id!=='timeline-media' || deletes[0].confirm_count!==2505) throw new Error('Deletion scope changed');
    await page.locator('#manage-mode').selectOption('permanent');
    await page.evaluate(() => { window.prompt = text => { window.confirmationText = text; return null; }; });
    await page.locator('#manage-confirm').click();
    if (deletes.length!==1) throw new Error('Cancelled permanent deletion sent a request');
    await page.evaluate(() => { window.prompt = () => 'wrong'; });
    await page.locator('#manage-confirm').click();
    if (deletes.length!==1) throw new Error('Wrong confirmation text was accepted');
    await page.evaluate(() => { window.prompt = text => { window.confirmationText = text; return '永久删除'; }; });
    await page.locator('#manage-confirm').click();
    await page.waitForFunction(()=>document.querySelector('#manage-export').disabled===false);
    if (deletes.length!==2 || deletes[1].confirm_permanent!=='永久删除' || deletes[1].confirm_count!==2505) throw new Error('Full permanent deletion confirmation failed');
    if (!(await page.evaluate(()=>window.confirmationText)).includes('2,505')) throw new Error('Permanent confirmation omitted total count');
    for (const width of [320,390,1440]) {
        await page.setViewportSize({width,height:844});
        await page.waitForTimeout(250);
        if (await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw new Error(`Overflow at ${width}`);
        if (width===390) await page.screenshot({path:'output/playwright/management-mobile.png'});
    }
    const beforeLeave=previews.length;
    await start.fill('2026-10-01T08:00');
    await page.locator('#settings-back').click();
    await page.waitForTimeout(600);
    if (previews.length!==beforeLeave) throw new Error('Detached page sent automatic preview');
    if (errors.length) throw new Error(errors.join('\n'));
    await page.evaluate(() => { window.managementChecksPassed = true; });
    return {automaticPreview:true,debounced:true,staleResultIgnored:true,inclusiveBoundsSent:true,emptyAndInvalidRanges:true,unlimitedEmptyBounds:true,conversationSwitch:true,download:true,fullRangeDelete:true,cancelPreventsDeletion:true,permanentConfirmation:true,responsive:true,noDetachedRequests:true};
}
