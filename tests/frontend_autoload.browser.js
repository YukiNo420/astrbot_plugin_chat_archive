// Run after frontend_timeline.browser.js supplies synthetic archive APIs.
async(page)=>{
 const checks=[];
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 let mode='normal',failOlder=false,requests=[],inFlight=0,maxInFlight=0;
 await page.route('**/api/history?**',async route=>{
  const params=new URL(route.request().url()).searchParams;
  const pageNumber=Number(params.get('page')||1),sessionId=params.get('session_id');
  requests.push({page:pageNumber,cursor:params.get('cursor'),keyword:params.get('keyword')});
  inFlight++;maxInFlight=Math.max(maxInFlight,inFlight);
  try{
   if(pageNumber>1)await new Promise(resolve=>setTimeout(resolve,350));
   if(pageNumber>1&&failOlder)return route.fulfill({status:503,json:{success:false}});
   const batch=mode==='short'?3:50;
   const end=2000-(pageNumber-1)*batch;
   const empty=mode==='empty'&&pageNumber>1;
   const data=empty?[]:Array.from({length:batch},(_,i)=>({id:end-i,msg_id:String(end-i),session_id:sessionId,user_id:'demo',sender_name:'测试用户',timestamp:1790900000+(end-i)*20,message:'用于验证连续加载与阅读位置。'}));
   return route.fulfill({json:{success:true,data,has_more:mode==='empty'||pageNumber<(mode==='short'?4:3),next_cursor:empty?0:end-batch+1}});
  }finally{inFlight--;}
 });
 async function open(width=390){
  await page.waitForFunction(()=>!isHistoryLoading);
  requests=[];maxInFlight=0;
  await page.setViewportSize({width,height:844});
  await page.goto('http://127.0.0.1:8090/?session_id=timeline-demo');
  await page.waitForSelector('.msg-bubble');
  await page.waitForTimeout(250);
 }
 async function anchor(){
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  return page.evaluate(()=>{
   const bounds=viewport.getBoundingClientRect();
   const el=[...viewport.querySelectorAll('.msg-bubble')].find(el=>el.getBoundingClientRect().top>=bounds.top);
   return {id:el.dataset.msgId,top:el.getBoundingClientRect().top};
  });
 }
 async function sameAnchor(before){
  return page.evaluate(before=>{
   const el=viewport.querySelector(`[data-msg-id="${before.id}"]`);
   return !!el&&Math.abs(el.getBoundingClientRect().top-before.top)<=2;
  },before);
 }
 for(const width of [1440,390]){
  await open(width);
  await page.evaluate(()=>{viewport.scrollTop=350;});
  const before=await anchor();
  await page.waitForFunction(()=>virtualMessages.length===100&&!isHistoryLoading);
  await page.waitForTimeout(180);
  checks.push({name:`prefetch before reaching top at ${width}px`,pass:requests.length===2&&requests[1].page===2});
  checks.push({name:`prepend keeps reading position at ${width}px`,pass:await sameAnchor(before)});
  checks.push({name:`no manual button needed at ${width}px`,pass:await page.locator('#loadMoreBtn').isHidden()});
 }
 await open();
 await page.evaluate(()=>{viewport.scrollTop=350;});
 await page.waitForFunction(()=>isHistoryLoading);
 checks.push({name:'loading feedback',pass:await page.locator('.load-more-status').textContent()==='正在加载更早记录…'});
 await page.evaluate(()=>{viewport.scrollTop=0;for(let i=0;i<20;i++)viewport.dispatchEvent(new Event('scroll'));});
 const topAnchor=await anchor();
 await page.waitForFunction(()=>virtualMessages.length===100&&!isHistoryLoading);
 await page.waitForTimeout(180);
 checks.push({name:'rapid scrolling cannot duplicate a request',pass:requests.length===2&&maxInFlight===1,requests:requests.slice(),maxInFlight});
 checks.push({name:'reaching the top during a request retains anchor',pass:await sameAnchor(topAnchor)});
 await page.evaluate(()=>{viewport.scrollTop=0;});
 await page.waitForFunction(()=>virtualMessages.length===150&&!historyHasMore&&!isHistoryLoading);
 const finalCount=requests.length;
 await page.evaluate(()=>{viewport.scrollTop=0;});
 await page.waitForTimeout(500);
 checks.push({name:'exhausted history stops loading',pass:requests.length===finalCount&&await page.locator('.load-more-status').textContent()==='已到达这段记录的开头'});
 failOlder=true;
 await open();
 await page.evaluate(()=>{viewport.scrollTop=0;});
 await page.waitForFunction(()=>historyLoadFailed&&!isHistoryLoading);
 await page.evaluate(()=>{for(let i=0;i<20;i++)viewport.dispatchEvent(new Event('scroll'));});
 await page.waitForTimeout(550);
 checks.push({name:'failure pauses automatic retries',pass:requests.length===2&&await page.locator('#loadMoreBtn').isVisible()&&(await page.locator('#loadMoreBtn').textContent()).trim()==='重试加载'});
 failOlder=false;
 await page.locator('#loadMoreBtn').click();
 await page.waitForFunction(()=>virtualMessages.length===100&&!isHistoryLoading);
 checks.push({name:'retry uses the same page and cursor',pass:requests[1].page===requests[2].page&&requests[1].cursor===requests[2].cursor});
 checks.push({name:'successful retry restores automatic loading',pass:await page.locator('#loadMoreBtn').isHidden()});
 mode='short';
 await open();
 await page.waitForFunction(()=>virtualMessages.length===12&&!historyHasMore&&!isHistoryLoading);
 await page.waitForTimeout(200);
 checks.push({name:'short pages fill without requiring a scroll event',pass:requests.length===4&&maxInFlight===1,requests:requests.slice(),maxInFlight});
 checks.push({name:'short pages keep latest message visible',...await page.evaluate(()=>({pass:viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight<=2,bottom:viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight,top:viewport.scrollTop}))});
 mode='empty';
 await open();
 await page.evaluate(()=>{viewport.scrollTop=0;});
 await page.waitForFunction(()=>!historyHasMore&&!isHistoryLoading);
 await page.waitForTimeout(500);
 checks.push({name:'empty response stops even with incorrect has_more',pass:requests.length===2});
 mode='normal';
 await open();
 requests=[];
 await page.evaluate(async()=>{activeSearchKeyword='测试';await fetchHistory();});
 await page.waitForTimeout(500);
 checks.push({name:'search does not automatically move its first result',pass:requests.length===1&&await page.evaluate(()=>viewport.scrollTop<=2)});
 checks.push({name:'search still has explicit pagination',pass:await page.locator('#loadMoreBtn').isVisible()});
 await page.locator('#loadMoreBtn').click();
 await page.waitForFunction(()=>virtualMessages.length===100&&!isHistoryLoading);
 checks.push({name:'search pagination works',pass:requests.length===2&&requests[1].keyword==='测试'});
 await open();
 await page.evaluate(()=>{viewport.scrollTop=0;});
 await page.waitForFunction(()=>isHistoryLoading);
 await page.evaluate(()=>showSettings());
 await page.waitForTimeout(600);
 checks.push({name:'leaving the conversation cancels automatic loading',pass:requests.length===2&&await page.evaluate(()=>!!document.querySelector('.settings-page')&&!timeline)});
 checks.push({name:'no script errors',pass:errors.length===0,errors});
 await page.evaluate(checks=>window.__autoHistoryChecks=checks,checks);
 if(checks.some(c=>!c.pass))throw new Error(JSON.stringify(checks.filter(c=>!c.pass)));
 return {passed:checks.length};
}
