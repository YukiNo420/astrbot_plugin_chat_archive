// Run after frontend_timeline.browser.js, which supplies synthetic archive APIs.
async(page)=>{
 await page.evaluate(async()=>{activeSearchKeyword='';await fetchHistory();window.__controlChecks=[];});
 await page.waitForTimeout(200);
 const box=await page.locator('#messageList').boundingBox();
 await page.mouse.move(box.x+box.width/2,box.y+box.height/2);
 await page.mouse.wheel(0,-30000);
 await page.waitForFunction(()=>virtualMessages.length>50);
 await page.evaluate(()=>window.__controlChecks.push({name:'wheel up loads older history',pass:virtualMessages.length===100}));
 await page.locator('#btn-analysis').click();
 await page.waitForTimeout(220);
 await page.evaluate(()=>window.__controlChecks.push({name:'desktop details drawer opens',pass:document.getElementById('analysisPanel').classList.contains('open')&&document.querySelector('.main-container').inert}));
 await page.keyboard.press('Escape');
 await page.evaluate(()=>window.__controlChecks.push({name:'Escape restores focus',pass:document.activeElement===document.getElementById('btn-analysis')&&!document.querySelector('.main-container').inert}));
 await page.setViewportSize({width:360,height:800});
 await page.locator('#btn-sidebar').click();
 await page.waitForTimeout(200);
 await page.evaluate(()=>window.__controlChecks.push({name:'mobile session drawer opens',pass:document.getElementById('sessionSidebar').classList.contains('open')&&document.querySelector('.main-container').inert}));
 await page.keyboard.press('Escape');
 await page.evaluate(()=>window.__controlChecks.push({name:'mobile drawer closes',pass:!document.getElementById('sessionSidebar').classList.contains('open')&&!document.querySelector('.main-container').inert}));
 const failHistory=route=>route.fulfill({status:503,json:{success:false}});
 await page.route('**/api/history?**',failHistory);
 await page.evaluate(async()=>{const node=viewport.querySelector('.msg-bubble');await fetchHistory();window.__controlChecks.push({name:'failed refresh retains messages with feedback',pass:node.isConnected&&[...document.querySelectorAll('.app-toast')].some(toast=>toast.textContent.includes('刷新失败'))});});
 await page.unroute('**/api/history?**',failHistory);
 await page.evaluate(()=>{
  historyHasMore=false;
  setVirtualMessages(Array.from({length:2000},(_,i)=>({id:i,msg_id:String(i),session_id:'raw-scroll',user_id:'demo',sender_name:'滚动测试',timestamp:1790900000+i*30,message:i%9===0?'长消息。'.repeat(90):'滚动与浏览记录。'})));
 });
 await page.waitForTimeout(150);
 await page.evaluate(async()=>{
  const failures=[];
  for(const fraction of [.3,.9,.2,.7,.4,.8]){
   viewport.scrollTop=(viewport.scrollHeight-viewport.clientHeight)*fraction;
   await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
   const bounds=viewport.getBoundingClientRect();
   const rows=[...document.querySelectorAll('.timeline-window > *')];
   if(!rows.some(el=>{const r=el.getBoundingClientRect();return r.bottom>bounds.top&&r.top<bounds.bottom;}))failures.push(fraction);
  }
  window.__controlChecks.push({name:'scrollbar jumps render visible records',pass:!failures.length,failures});
 });
 await page.setViewportSize({width:1440,height:960});
 const failures=await page.evaluate(()=>window.__controlChecks.filter(c=>!c.pass).map(c=>c.name));
 if(failures.length)throw new Error('Control regressions: '+failures.join(', '));
}
