// Run with playwright-cli run-code --filename=tests/frontend_timeline.browser.js.
// The archive must be open with its candidate static assets routed locally.
async (page) => {
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
    await page.goto('http://127.0.0.1:8090/?session_id=timeline-demo');
    await page.waitForSelector('.msg-bubble');
    await page.evaluate(() => { window.__timelineChecks = []; });
    await page.waitForTimeout(350);
    async function check(name, fn) {
        const result = await page.evaluate(fn);
        await page.evaluate(({name,result}) => window.__timelineChecks.push({name,...result}), {name,result});
    }
    await check('initial latest and bounded rows', () => ({pass:viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight <= 2 && document.querySelectorAll('[data-vkey]').length < 50, bottom:viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight}));
    await check('details default to a drawer and reading uses available width', () => ({pass:getComputedStyle(document.getElementById('analysisPanel')).position === 'fixed' && document.querySelector('.main-container').getBoundingClientRect().width > innerWidth-310}));
    await page.evaluate(() => { timeline.scrollToIndex(30); });
    await page.waitForTimeout(200);
    await check('visible message DOM survives small scrolling', async () => {
        const top=viewport.getBoundingClientRect().top;
        const node=[...viewport.querySelectorAll('.msg-bubble')].find(el=>el.getBoundingClientRect().top>top+150);
        viewport.scrollTop+=40;
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
        return {pass:!!node?.isConnected && node===viewport.querySelector(`[data-msg-id="${node.dataset.msgId}"]`)};
    });
    await check('prepend preserves the visible message position', async () => {
        const top=viewport.getBoundingClientRect().top;
        const node=[...viewport.querySelectorAll('.msg-bubble')].find(el=>el.getBoundingClientRect().top>=top);
        const before=node.getBoundingClientRect().top;
        currentPage++;
        await fetchHistory(true);
        await new Promise(resolve=>setTimeout(resolve,250));
        return {pass:node.isConnected && Math.abs(node.getBoundingClientRect().top-before)<=2, drift:node.getBoundingClientRect().top-before};
    });
    await check('refresh keeps the visible rows mounted while the request is pending', async () => {
        const node=viewport.querySelector('.msg-bubble');
        const promise=fetchHistory();
        const retained=node.isConnected && !viewport.querySelector('.skeleton-group');
        await promise;
        return {pass:retained};
    });
    await page.evaluate(() => {
        historyHasMore=false;
        const messages=Array.from({length:2000},(_,i)=>({id:i+1,msg_id:String(i+1),session_id:activeSessionId,user_id:`author-${Math.floor(i/4)%4}`,sender_name:'列表测试',timestamp:1790900000+i*10,message:i%13===0?'很长的记录。'.repeat(100):`第 ${i+1} 条记录。滚动时保持消息连续。`}));
        setVirtualMessages(messages);
        timeline.scrollToIndex(1000);
    });
    await page.waitForTimeout(300);
    await check('large scroll jumps keep the viewport filled and DOM bounded', async () => {
        const failures=[];
        for(const index of [300,1600,800,1900,600,1200,200,1750]) {
            timeline.scrollToIndex(index);
            await new Promise(resolve=>setTimeout(resolve,90));
            const rect=viewport.getBoundingClientRect();
            const rows=[...document.querySelectorAll('.timeline-window > *')];
            const intersects=rows.some(el=>{const r=el.getBoundingClientRect();return r.bottom>rect.top&&r.top<rect.bottom});
            if(!intersects || rows.length>100) failures.push(index);
        }
        return {pass:!failures.length,failures,rendered:document.querySelectorAll('[data-vkey]').length,cached:timeline.nodes.size};
    });
    await page.evaluate(()=>scrollListToBottom(viewport));
    await page.waitForTimeout(250);
    await check('jump to latest resolves estimated heights',()=>({pass:viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight<=2,bottom:viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight}));
    await page.setViewportSize({width:390,height:844});
    await page.waitForTimeout(350);
    await check('mobile layout does not overflow',()=>({pass:document.documentElement.scrollWidth<=innerWidth && viewport.clientWidth<=innerWidth}));
    await page.evaluate(()=>timeline.scrollToIndex(1750));
    await page.waitForTimeout(200);
    await check('media growth above the viewport keeps the reading anchor', async()=>{
        const bounds=viewport.getBoundingClientRect();
        const visible=[...viewport.querySelectorAll('.msg-bubble')].find(el=>el.getBoundingClientRect().top>=bounds.top);
        const above=[...document.querySelectorAll('.timeline-window > .message-group')].filter(el=>el.getBoundingClientRect().bottom<bounds.top).at(-1);
        const before=visible.getBoundingClientRect().top;
        const media=document.createElement('div');media.style.height='450px';above.querySelector('.msg-text').append(media);
        await new Promise(resolve=>setTimeout(resolve,220));
        return {pass:visible.isConnected && Math.abs(visible.getBoundingClientRect().top-before)<=2,drift:visible.getBoundingClientRect().top-before};
    });
    await page.evaluate(()=>scrollListToBottom(viewport));
    await page.waitForTimeout(250);
    await check('media growth at the end remains pinned',async()=>{
        const media=document.createElement('div');media.style.height='360px';
        document.querySelector('.timeline-window > .message-group:last-child .msg-text').append(media);
        await new Promise(resolve=>setTimeout(resolve,220));
        return {pass:viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight<=2,bottom:viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight};
    });
    await page.evaluate(async()=>{activeSearchKeyword='滚动';await fetchHistory();});
    await page.waitForTimeout(200);
    await check('search begins at the first result',()=>({pass:viewport.scrollTop<=2,top:viewport.scrollTop}));
    await page.evaluate(()=>{activeSessionId='timeline-slow';fetchHistory();showSettings();});
    await page.waitForTimeout(750);
    await check('late history responses cannot overwrite settings',()=>({pass:!!document.querySelector('.settings-page')&&!document.getElementById('virtualRows')&&timeline===null}));
    await page.evaluate(()=>{activeSessionId='timeline-demo';selectSession('timeline-demo','产品讨论','GroupMessage');});
    await page.waitForTimeout(300);
    await page.screenshot({path:'output/playwright/timeline/after-mobile.png'});
    await page.setViewportSize({width:1440,height:960});
    await page.waitForTimeout(300);
    await page.screenshot({path:'output/playwright/timeline/after-desktop.png'});
    await page.evaluate(errors=>window.__timelineChecks.push({name:'browser errors',pass:errors.length===0,errors}),errors);
    const failures = await page.evaluate(() => window.__timelineChecks.filter(check => !check.pass).map(check => check.name));
    if (failures.length) throw new Error('Timeline regressions: ' + failures.join(', '));
}
