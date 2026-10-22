'use strict';
// Application-owned collection browser. Uses the site's normal page loading and pagination.
const {chromium} = require(process.env.XHS_PW_MODULE || 'playwright');
const readline = require('node:readline');
const path = require('node:path');
const emit = value => process.stdout.write(JSON.stringify(value)+'\n');
let context, stopped=false;
const input=readline.createInterface({input:process.stdin});
input.on('line',line=>{try{if(JSON.parse(line).action==='cancel'){stopped=true;context?.close().catch(()=>{});}}catch{}});
const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));

function normalizeNote(raw,userId){
  const card=raw.note_card||raw.noteCard||raw;
  const id=raw.note_id||raw.noteId||raw.id||card.note_id||card.noteId;
  const token=raw.xsec_token||raw.xsecToken||card.xsec_token||card.xsecToken;
  const user=card.user||raw.user||{};
  const owner=user.user_id||user.userId||user.id;
  if(owner && owner!==userId)return null;
  if(!/^[a-f0-9]{24}$/i.test(id||'') || !token)return null;
  return {id,url:`https://www.xiaohongshu.com/user/profile/${userId}/${id}?xsec_token=${encodeURIComponent(token)}&xsec_source=pc_user`,title:card.display_title||card.displayTitle||card.title||'未命名作品',author:user.nickname||user.nick_name||user.nickName||'',kind:(card.type||raw.type)==='video'?'视频':'图文'};
}

async function run(config){
  const userId=new URL(config.url).pathname.match(/^\/user\/profile\/([a-f0-9]{24})\/?$/i)?.[1];
  if(!userId)throw new Error('请输入完整的作者主页链接。');
  const notes=new Map();let endConfirmed=false;let blocked=false;let author='';let lastAdded=Date.now();let lastEmit=0;
  function add(rawItems){
    let added=[];
    for(const raw of rawItems||[]){const note=normalizeNote(raw,userId);if(note&&!notes.has(note.id)){notes.set(note.id,note);added.push(note);if(note.author)author=note.author;}}
    if(added.length){lastAdded=Date.now();emit({type:'notes',notes:added,author,count:notes.size});}
  }
  context=await chromium.launchPersistentContext(path.join(__dirname,'browser-profile'),{channel:'msedge',headless:false,chromiumSandbox:true,viewport:null,args:['--start-maximized'],timeout:60000});
  const page=context.pages()[0]||await context.newPage();
  for(const extra of context.pages().slice(1))await extra.close();
  page.on('response',async response=>{
    if(!response.url().includes('/user_posted'))return;
    try{
      const requestUrl=new URL(response.url());
      if(requestUrl.searchParams.get('user_id')!==userId)return;
      const body=await response.json();
      if(body.success===false || (body.code && body.code!==0)){blocked=true;return;}
      const data=body.data||{};add(data.notes||[]);
      if(data.has_more===false || data.hasMore===false)endConfirmed=true;
    }catch{}
  });
  emit({type:'status',status:'scanning',message:'正在打开作者主页…'});
  await page.goto(config.url,{waitUntil:'domcontentloaded',timeout:60000});
  const started=Date.now();let loginStarted=0;let stable=0;let previous=0;let previousHeight=0;
  while(!stopped && Date.now()-started<30*60*1000){
    if(page.isClosed())throw new Error('采集窗口已关闭，请重新获取作品集。');
    const view=await page.evaluate(userId=>{
      const all=[];
      // The bundled upstream userscript reads the same hydrated published-notes collection.
      const ref=window.__INITIAL_STATE__?.user?.notes;
      const value=ref?._rawValue||ref?.value||ref;
      const first=Array.isArray(value?.[0])?value[0]:Array.isArray(value)?value:[];
      for(const note of first)if(note?.noteCard||note?.note_card)all.push(note);
      for(const link of document.querySelectorAll('a[href]')){
        const url=new URL(link.href,location.href);
        const match=url.pathname.match(/^\/user\/profile\/([^/]+)\/([a-f0-9]{24})\/?$/i);
        if(match&&match[1]===userId)all.push({id:match[2],xsecToken:url.searchParams.get('xsec_token'),displayTitle:link.textContent.trim(),user:{userId}});
      }
      const text=document.body.innerText;
      const visible=selector=>[...document.querySelectorAll(selector)].some(el=>el.getBoundingClientRect().width&&el.getBoundingClientRect().height);
      return {notes:all,author:document.querySelector('.user-name')?.textContent?.trim()||document.title.replace(/\s*[-–]\s*小红书.*$/,''),login:visible('#login-btn')||/登录后推荐更懂你的笔记/.test(text),captcha:/安全验证|请完成验证|拖动滑块|访问频繁|网络异常/.test(text),end:/暂时没有更多|没有更多了|已经到底了|你还没有发布任何笔记|该用户还没有发布笔记/.test(text),height:document.documentElement.scrollHeight,bottom:window.scrollY+window.innerHeight>=document.documentElement.scrollHeight-30};
    },userId);
    if(view.author&&!/小红书|你的生活/.test(view.author))author=view.author;
    add(view.notes);
    if(view.captcha || blocked){
      emit({type:'status',status:'attention',author,message:'页面出现验证或访问限制，请在采集窗口处理后继续；系统不会绕过验证。'});
      blocked=false;await pause(3500);continue;
    }
    if(view.login && notes.size===0){
      loginStarted ||= Date.now();
      if(Date.now()-loginStarted>15*60*1000)throw new Error('等待登录超时，请重新获取作品集。');
      if(Date.now()-lastEmit>8000){emit({type:'status',status:'login',author,message:'请在弹出的 Edge 采集窗口扫码登录，登录后会自动继续。'});lastEmit=Date.now();}
      await pause(1800);continue;
    }
    if(endConfirmed || (view.end && view.bottom)){
      emit({type:'complete',complete:true,author,count:notes.size,message:`已加载全部可见作品，共 ${notes.size} 篇`});return;
    }
    if(view.bottom && notes.size===previous && view.height===previousHeight)stable++;else stable=0;
    previous=notes.size;previousHeight=view.height;
    if(stable>=18 && Date.now()-lastAdded>35000){
      emit({type:'complete',complete:false,author,count:notes.size,message:`已获取 ${notes.size} 篇，但页面未确认全部加载完成。可下载当前结果，或重新扫描。`});return;
    }
    if(Date.now()-lastEmit>4500){emit({type:'status',status:'scanning',author,count:notes.size,message:`已发现 ${notes.size} 篇作品，正在加载更多…`});lastEmit=Date.now();}
    await page.mouse.wheel(0,Math.max(800,Math.round((await page.evaluate(()=>innerHeight))*.85)));
    await pause(1600);
    if(notes.size>=20000){emit({type:'complete',complete:false,author,count:notes.size,message:'已达到本次 20000 篇采集上限，当前结果已保留。'});return;}
  }
  emit({type:'complete',complete:false,author,count:notes.size,message:stopped?'已停止采集，已获取的结果已保留。':'采集时间较长，已保留当前结果，请检查页面后重新扫描。'});
}

input.once('line',async line=>{
  try{await run(JSON.parse(line));}catch(error){if(!stopped)emit({type:'error',message:error.message.includes('Executable')?'未找到可用的 Edge 浏览器。':error.message.slice(0,350)});}
  finally{await context?.close().catch(()=>{});input.close();process.exit(0);}
});

module.exports={normalizeNote};
