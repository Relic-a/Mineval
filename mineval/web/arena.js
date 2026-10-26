'use strict';
const main = document.querySelector('#main');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const base = location.pathname.replace(/\/?$/, '/');
let state = null, tab = '', benchmark = '', configs = [], anonymous = false, grouping = 'configuration', timer = null, routeVersion = 0;
const date = value => new Date(value).toLocaleString([], {dateStyle:'medium', timeStyle:'short'});
const identity = r => `${r.config.harness} / ${r.config.model}`;
const alias = r => {const keys=[...new Set(state.generations.map(identity))];const i=keys.indexOf(identity(r));return `Model ${i<26?String.fromCharCode(65+i):i+1}`;};
const label = r => `${anonymous?alias(r):r.config.model} · ${r.config.effort}`;
function notify(message) { const n = document.querySelector('#notice'); n.textContent = message; n.style.display = 'block'; clearTimeout(timer); timer = setTimeout(() => n.style.display='none', 7000); }
async function api(path, data) {
  const response = await fetch(base + 'api/' + path, data === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Arena-Request':'1'},body:JSON.stringify(data)});
  const result = await response.json(); if (!response.ok) throw Error(result.error || 'Request failed'); return result;
}
function go(suffix='results') { location.hash = `${benchmark}/${suffix}`; }
function bindForm(id, callback) { const form = document.querySelector(id); if (!form) return; form.onsubmit = async e => {e.preventDefault(); const button=form.querySelector('[type=submit]'); button.disabled=true; try {await callback(new FormData(form));} catch(e){notify(e.message);} finally{button.disabled=false;} }; }
function action(id, callback) {const el=document.querySelector(id); if(el)el.onclick=async()=>{el.disabled=true;try{await callback();}catch(e){notify(e.message);}finally{el.disabled=false;}};}
async function refresh() {state=await api('benchmarks/'+benchmark);}
function heading(title, description, actions='') { return `<div class="heading"><div><h1>${esc(title)}</h1><p class="muted">${esc(description)}</p></div><div class="actions">${actions}</div></div>`; }
async function route() {
  const version = ++routeVersion;
  const parts = location.hash.slice(1).split('/'); benchmark=parts[0]||''; tab=parts[1]||'results';
  try {
    if (!benchmark) {await listing();return;}
    if (benchmark==='new') {createPage();return;}
    if (benchmark==='coverage') {await coveragePage();return;}
    const next=await api('benchmarks/'+benchmark); if(version!==routeVersion)return; state=next; render();
  } catch(e) {main.innerHTML=heading('Unable to open this page', e.message, '<a href="#">Return to benchmarks</a>');}
  finally {if(version===routeVersion)focusDestination();}
}
function focusDestination(afterJudgment=false) {
 const target=main.querySelector(afterJudgment?'h2':'h1')||main;
 target.setAttribute('tabindex','-1');target.focus({preventScroll:true});
 document.title=`${state&&benchmark===state.benchmark.id?state.benchmark.title+' · '+tab:target.textContent} · Mineval Web Arena`;
}
async function listing() {
  const rows=await api('benchmarks');
  main.innerHTML=heading('Web Arena', 'One brief. Independent websites. Your preference decides.', '<a class="button-link" href="#new">Create benchmark</a>')+
    '<p class="steps">Create a benchmark → generate websites → explore and rank → share</p>'+
    (rows.length ? `<div class="benchmarks">${rows.slice().reverse().map(r=>`<article class="benchmark"><div><h2><a href="#${r.id}/results">${esc(r.title)}</a></h2><p>${esc(r.prompt.slice(0,160))}${r.prompt.length>160?'…':''}</p></div><small>Created ${esc(date(r.created_at))}<br>${r.private?'Private project':'General brief'}</small><a href="#${r.id}/generate">Add models</a></article>`).join('')}</div>` : '<div class="empty"><h2>Give the models the same starting point.</h2><p>Save a design brief, add references or a starter, and generate your first websites. Return to the same benchmark whenever a new model arrives.</p><a href="#new">Create your first benchmark</a></div>');
}
function createPage() {
  main.innerHTML=heading('Create a benchmark', 'A persistent design task for today’s models and the ones that come next.')+`<form id="create" class="form"><label for="title">Benchmark title</label><input id="title" name="title" required maxlength="160" placeholder="e.g. A thoughtful reading app"><label for="prompt">Design brief<small>Describe the audience, content, interactions, and what a successful website should do.</small></label><textarea id="prompt" name="prompt" required maxlength="64000" rows="7" placeholder="Build a website that…"></textarea><label for="references">References <small>Optional context or reference links. Upload reference images below for reproducible access.</small></label><textarea id="references" name="references" rows="2" maxlength="16000"></textarea><label for="assets">Assets and reference images <small>Up to 20 files, 20 MiB total. Files become part of every model’s input.</small></label><input id="assets" type="file" multiple><label for="starter">Starter project <small>Optional .tar.gz with project files at the archive root. Exclude .git, node_modules, credentials, links, and build caches.</small></label><input id="starter" type="file" accept=".gz,.tar,.tgz"><label><input name="private" type="checkbox" checked> Contains private project content</label><p class="muted">Inputs are frozen when you create the benchmark. Start a new benchmark when the brief changes so comparisons stay meaningful.</p><div class="actions"><button type="submit">Create and choose models</button><a href="#">Cancel</a></div></form>`;
  bindForm('#create', async f => {
    const files=[...document.querySelector('#assets').files]; if(files.length>20||files.reduce((s,x)=>s+x.size,0)>20*1024*1024)throw Error('Assets exceed 20 files or 20 MiB');
    const starter=document.querySelector('#starter').files[0]; if(starter&&starter.size>32*1024*1024)throw Error('Starter exceeds 32 MiB');
    const encode = file => new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
    const data={title:f.get('title'),prompt:f.get('prompt'),references:f.get('references'),private:f.has('private'),assets:await Promise.all(files.map(async file=>({name:file.name,data:await encode(file)})))};
    if(starter)data.starter=await encode(starter);
    const r=await api('benchmarks',data);benchmark=r.id;configs=[];go('generate');
  });
}
function shell() {
  const task=state.benchmark, ready=state.generations.filter(r=>r.status==='ready').length;
  return heading(task.title, `${ready} websites · ${state.judgments.length} saved judgments · created ${date(task.created_at)}`, '<a href="#">All benchmarks</a>')+
    `<details class="task"><summary>Read the design brief${task.private?' · private project':''}</summary><pre>${esc(task.prompt)}</pre>${task.references?`<p>${esc(task.references)}</p>`:''}<p class="muted">${Object.keys(task.inputs).length} frozen input files. Every generation receives these exact inputs.</p></details>`+
    `<div class="tabs" role="navigation" aria-label="Benchmark">${[['results','Websites'],['generate','Generate'],['rank','Rank'],['leaderboard','Leaderboard'],['share','Share']].map(([id,name])=>`<a href="#${benchmark}/${id}" ${tab===id?'aria-current="page"':''}>${name}${id==='results'?`<span class="counter">${ready}</span>`:''}</a>`).join('')}</div>`;
}
function render() {
  main.innerHTML=shell()+({results:resultsPage,generate:generatePage,rank:rankPage,leaderboard:leaderboardPage,share:sharePage}[tab]||resultsPage)();
  bindCommon();
  if(tab==='generate') bindGeneration();
  if(tab==='rank') bindRanking();
  if(tab==='leaderboard') bindLeaderboard();
  if(tab==='share') bindShare();
}
function website(r, candidate='') {
  return `<article class="website" data-run="${r.id}"><div class="website-head"><div><h3>${candidate?`Website ${esc(candidate)} · `:''}${esc(label(r))}</h3><small>${esc(date(r.created_at))}</small></div>${r.status==='ready'?`<button class="secondary expand" data-id="${r.id}">Expand</button>`:`<span class="status ${esc(r.status)}">${esc(r.status)}</span>`}</div><div class="preview" id="preview-${r.id}">${r.status==='ready'?`<button class="secondary open-preview" data-id="${r.id}">Open interactive website</button><small>Runs in an isolated offline runtime</small>`:r.status==='error'?`<p class="error">${esc(r.error)}</p><small>Create a new run to retry.</small>`:`<p>${r.status==='queued'?'Waiting for the generation worker.':'Generating and building this website…'}</p>`}</div><div class="website-foot">${esc(r.config.harness)} · ${esc(r.id.slice(0,8))} <span>· ${r.status==='ready'?'Saved independently':'Refresh for updates'}</span></div></article>`;
}
function anonymity() {return `<label><input id="anonymous" type="checkbox" ${anonymous?'checked':''}> Hide model identities</label>`;}
function resultsPage() {
  return `<div class="toolbar"><p class="muted">Explore the websites before judging. Expand for a full working canvas.</p><div class="actions">${anonymity()}<button id="refresh" class="secondary">Refresh</button></div></div>`+
  (state.generations.length?`<div class="gallery">${state.generations.slice().reverse().map(r=>website(r)).join('')}</div>`:`<div class="empty"><h2>The brief is ready. Bring in the models.</h2><p>Generate several websites now, or add models one at a time. Every run stays available for future comparison.</p><a href="#${benchmark}/generate">Choose models and reasoning efforts</a></div>`);
}
function generatePage() {
  return `<h2>Choose independent generations</h2><p class="muted">Add any model and effort combination. Repeating a configuration creates a new dated result; previous results remain in the leaderboard.</p><form id="add-config"><div class="generation-builder"><div><label for="model">Model identifier</label><input id="model" name="model" required placeholder="Model available to your Codex account" maxlength="120"></div><div><label for="effort">Reasoning effort</label><select id="effort" name="effort">${['minimal','low','medium','high','xhigh'].map(x=>`<option ${x==='medium'?'selected':''}>${x}</option>`).join('')}</select></div><div><label for="harness">Harness</label><select id="harness" name="harness"><option value="codex">Codex</option><option value="fixture">Fixture · no model call</option></select></div><button type="submit">Add to batch</button></div></form><ul id="configs" class="config-list"></ul><div class="actions"><button id="generate" ${configs.length?'':'disabled'}>Generate ${configs.length||''} websites</button><span class="muted">Runs one at a time in rootless Docker</span></div><div class="notice spacer">Codex uses the preloaded image and optional model credential file configured when Mineval started. Unsupported effort levels or missing runtimes produce an inspectable failed run.</div><h2>Generation history</h2>`+ (state.generations.length?`<div class="scroll"><table><thead><tr><th>Model / effort</th><th>Created</th><th>State</th><th>Action</th></tr></thead><tbody>${state.generations.slice().reverse().map(r=>`<tr><td>${esc(r.config.model)}<small>${esc(r.config.effort)} · ${esc(r.config.harness)}</small></td><td>${esc(date(r.created_at))}</td><td><span class="status ${esc(r.status)}">${esc(r.status)}</span>${r.error?`<small class="error">${esc(r.error)}</small>`:''}</td><td><button class="secondary rerun" data-id="${r.id}">Add rerun</button></td></tr>`).join('')}</tbody></table></div>`:'<p class="muted">Your first generation will appear here.</p>')+'<p><button id="refresh" class="secondary">Refresh generation status</button></p>';
}
function drawConfigs() {
  document.querySelector('#configs').innerHTML=configs.map((c,i)=>`<li><span>${esc(c.model)} · ${esc(c.effort)} <small> / ${esc(c.harness)}</small></span><button class="secondary remove-config" data-index="${i}" aria-label="Remove ${esc(c.model)}">Remove</button></li>`).join('');
  const b=document.querySelector('#generate');b.disabled=!configs.length;b.textContent=`Generate ${configs.length||''} website${configs.length===1?'':'s'}`;
  document.querySelectorAll('.remove-config').forEach(b=>b.onclick=()=>{configs.splice(Number(b.dataset.index),1);drawConfigs();});
}
function bindGeneration() {
  drawConfigs();
  bindForm('#add-config', async f=>{if(configs.length>=24)throw Error('A batch supports up to 24 generations');configs.push({model:f.get('model').trim(),effort:f.get('effort'),harness:f.get('harness')});drawConfigs();});
  action('#generate', async()=>{const n=configs.length;await api(`benchmarks/${benchmark}/generate`,{configs});configs=[];await refresh();render();notify(`${n} independent generations queued`);});
  document.querySelectorAll('.rerun').forEach(b=>b.onclick=()=>{configs.push({...state.generations.find(r=>r.id===b.dataset.id).config});drawConfigs();document.querySelector('#generate').scrollIntoView({behavior:'smooth',block:'center'});});
}
function rankPage() {
  const pair=state.next_pair;
  if(!pair)return `<div class="empty"><h2>${state.leaderboard.length<2?'Two websites make a comparison.':'You’re up to date.'}</h2><p>${state.leaderboard.length<2?'Generate at least two websites to start ranking.':'The suggested comparisons are complete. Add a new generation to continue the same ranking history. You can still inspect every saved judgment below.'}</p><a href="#${benchmark}/${state.leaderboard.length<2?'generate':'leaderboard'}">${state.leaderboard.length<2?'Generate websites':'View the leaderboard'}</a></div>`+judgmentHistory();
  return `<div class="toolbar"><div><h2 class="rank-heading">Which website do you prefer?</h2><p class="muted">New and uncertain results are compared first. Ties are welcome. Each judgment is saved once.</p></div>${anonymity()}</div><div class="gallery">${[pair.left,pair.right].map((id,i)=>website(state.generations.find(r=>r.id===id),i===0?'A':'B')).join('')}</div><form id="judgment"><label for="vote-note">Why? <small>Optional private note, preserved with this judgment.</small></label><input id="vote-note" name="note" type="text" maxlength="2000"><div class="votes"><button type="submit" name="preference" value="left">Prefer A</button><button type="submit" class="secondary" name="preference" value="tie">Tie</button><button type="submit" name="preference" value="right">Prefer B</button></div></form><p class="muted">Overall preference is the evaluation. There is no automated design score.</p>`+judgmentHistory();
}
function judgmentHistory() {
  return `<details class="spacer"><summary class="disclosure">Saved judgments (${state.judgments.length})</summary><ul class="history">${state.judgments.slice().reverse().map(v=>{const a=state.generations.find(r=>r.id===v.left), b=state.generations.find(r=>r.id===v.right);return `<li>${esc(label(a))} ${v.preference==='tie'?'tied with':v.preference==='left'?'preferred to':'ranked below'} ${esc(label(b))}<small> · ${esc(date(v.created_at))} · ${esc(v.left.slice(0,8))} / ${esc(v.right.slice(0,8))}</small>${v.note?`<p>${esc(v.note)}</p>`:''}</li>`;}).join('')}</ul></details>`;
}
function bindRanking() {
  const form=document.querySelector('#judgment');if(!form)return;
  form.onsubmit=async e=>{e.preventDefault();const pref=e.submitter.value;const buttons=[...form.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);try{await api(`benchmarks/${benchmark}/judge`,{...state.next_pair,preference:pref,note:new FormData(form).get('note')});await refresh();render();focusDestination(true);notify('Judgment saved');}catch(e){notify(e.message);buttons.forEach(b=>b.disabled=false);}};
}
function groupedRows() {
  if(grouping==='runs')return state.leaderboard;
  const groups=new Map();
  for(const r of state.leaderboard){const key=`${r.config.harness} / ${r.config.model}${grouping==='configuration'?' / '+r.config.effort:''}`;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(r);}
  return [...groups].map(([name,members])=>({name:anonymous?alias(members[0])+(grouping==='configuration'?' / '+members[0].config.effort:''):name,members,score:members.reduce((s,r)=>s+r.score,0)/members.length,uncertainty:members.reduce((s,r)=>s+r.uncertainty,0)/members.length,judgments:members.reduce((s,r)=>s+r.judgments,0)})).sort((a,b)=>b.score-a.score);
}
function leaderboardPage() {
  const rows=groupedRows();
  return `<div class="toolbar"><p class="muted">A provisional ordering from your saved preferences.</p><div class="actions">${anonymity()}<label>Show <select id="group"><option value="configuration" ${grouping==='configuration'?'selected':''}>Model + effort</option><option value="model" ${grouping==='model'?'selected':''}>Model · all efforts</option><option value="runs" ${grouping==='runs'?'selected':''}>Every dated run</option></select></label></div></div><div class="notice">Scores summarize human judgments with regularization. Uncertainty is approximate; close positions and unjudged results are unresolved. Grouped scores average <strong>all</strong> dated runs, with no best-run selection. A group’s evidence count includes judgments between its own runs.</div>`+
  (rows.length?`<div class="scroll"><table><thead><tr><th>Position</th><th>Website / model</th><th>Preference estimate</th><th>Evidence</th><th>Saved results</th></tr></thead><tbody>${rows.map((r,i)=>`<tr><td class="rank">${r.judgments?rows.findIndex(x=>Math.abs(x.score-r.score)<1e-6)+1:'—'}</td><td><strong>${esc(r.name||label(r))}</strong><small>${r.members?`${r.members.length} dated runs`:esc(date(r.created_at))}</small></td><td class="score">${r.score.toFixed(2)} ± ${r.uncertainty.toFixed(2)}<small>${r.judgments?'Provisional':'Unplaced · no judgments'}</small></td><td>${r.judgments} judgments</td><td>${r.members?`<details><summary class="disclosure">Inspect all runs</summary>${r.members.map(m=>`<p>${esc(label(m))}<small>${esc(date(m.created_at))} · ${esc(m.id.slice(0,8))}</small><button class="secondary expand" data-id="${m.id}">Open website</button></p>`).join('')}</details>`:`<button class="secondary expand" data-id="${r.id}">Open website</button>`}</td></tr>`).join('')}</tbody></table></div><p class="spacer"><a href="#${benchmark}/rank">Continue comparisons</a> · <a href="#${benchmark}/share">Create a share image</a></p>`:'<div class="empty"><h2>No finished websites yet.</h2><a href="#'+benchmark+'/generate">Generate websites</a></div>')+judgmentHistory();
}
function bindLeaderboard(){document.querySelector('#group').onchange=e=>{grouping=e.target.value;render();};}
function sharePage(){
 const ready=state.generations.filter(r=>r.status==='ready');
 if(!ready.length)return '<div class="empty"><h2>Generate a website before sharing.</h2><p>PNG images and animated GIFs include real website previews and your preference rankings.</p></div>';
 return `<h2>Make the comparison worth sharing.</h2><p class="muted">Create a composed PNG or an animated GIF from live website captures. Exports stay local until you choose to share the file.</p><form id="share" class="share-options"><fieldset><legend>Websites to include · choose up to four</legend>${ready.map(r=>`<div class="selection"><input id="share-${r.id}" type="checkbox" name="generations" value="${r.id}" ${ready.indexOf(r)<4?'checked':''}><label for="share-${r.id}">${esc(label(r))}<small>${esc(date(r.created_at))} · ${esc(r.id.slice(0,8))}</small></label><button type="button" class="secondary expand" data-id="${r.id}">Inspect</button></div>`).join('')}</fieldset><div class="toolbar"><label>Format <select name="format"><option value="png">PNG image</option><option value="gif">Animated GIF · 3 live frames</option></select></label></div><label><input type="checkbox" name="anonymous" checked> Anonymize model labels in the exported image</label><label><input type="checkbox" name="include_title" ${state.benchmark.private?'':'checked'}> Include benchmark title</label><label><input type="checkbox" name="include_prompt"> Include design brief excerpt (up to three lines)</label><div class="notice spacer"><strong>Privacy review</strong><p>The website captures can reveal private project content, names, branding, model clues, or data. Anonymous labels only replace Mineval’s labels. Inspect the selected websites and the downloaded export before publishing.</p><label><input type="checkbox" name="reviewed_content" required> I reviewed the selected websites for private or sensitive content.</label><label><input type="checkbox" name="reviewed_identity" required> I reviewed the title, brief, model labels, and possible identity clues.</label></div><div class="actions spacer"><button type="submit">Create local export</button><span class="muted">Captures may take a few seconds.</span></div></form><div id="share-result" class="share-result" aria-live="polite"></div>`;
}
function bindShare(){
 document.querySelector('#share').addEventListener('change',e=>{if(!['reviewed_content','reviewed_identity'].includes(e.target.name))for(const name of ['reviewed_content','reviewed_identity'])document.querySelector(`[name="${name}"]`).checked=false;});
 bindForm('#share',async f=>{const keys=f.getAll('generations');if(keys.length<1||keys.length>4)throw Error('Select 1–4 websites');const data={generations:keys,format:f.get('format')};for(const k of ['anonymous','include_title','include_prompt','reviewed_content','reviewed_identity'])data[k]=f.has(k);notify('Capturing live websites and composing your export…');const r=await api(`benchmarks/${benchmark}/share`,data);const url=base+'download/'+r.id;document.querySelector('#share-result').innerHTML=`<h2>Review your export</h2><p>Check the final image for private content before posting it anywhere.</p><a href="${url}" download="mineval-web-arena.${r.format}">Download ${r.format.toUpperCase()}</a><img src="${url}" alt="Web Arena share image with selected websites and provisional rankings">`;});}
function frame(url,title){const f=document.createElement('iframe');f.src=url;f.title=title;f.sandbox='allow-scripts allow-same-origin allow-forms';f.referrerPolicy='no-referrer';f.allow='fullscreen';return f;}
function bindCommon(){
 action('#refresh',async()=>{await refresh();render();notify('Generation status updated');});
 const anon=document.querySelector('#anonymous');if(anon)anon.onchange=()=>{anonymous=anon.checked;render();};
 document.querySelectorAll('.open-preview').forEach(b=>b.onclick=async()=>{b.disabled=true;b.textContent='Starting isolated preview…';try{const r=await api('preview/'+b.dataset.id,{});const host=document.querySelector('#preview-'+b.dataset.id);host.replaceChildren(frame(r.url,label(state.generations.find(x=>x.id===b.dataset.id))));}catch(e){notify(e.message);b.disabled=false;b.textContent='Retry opening website';}});
 document.querySelectorAll('.expand').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await api('preview/'+b.dataset.id,{});const name=label(state.generations.find(x=>x.id===b.dataset.id));document.querySelector('#expanded-title').textContent=name;document.querySelector('#expanded-content').replaceChildren(frame(r.url,name));document.querySelector('#expanded').showModal();}catch(e){notify(e.message);}finally{b.disabled=false;}});
}
async function coveragePage(){
 const tasks=await api('benchmarks');
 main.innerHTML=heading('Across benchmarks','Compare the same model identities on an explicit common set of tasks.')+`<p>Every dated generation contributes. Benchmarks receive equal weight. Identities missing any selected benchmark are excluded; preference estimates remain provisional and are not comparable win rates.</p><form id="coverage"><fieldset><legend>Benchmark coverage</legend>${tasks.map(t=>`<label><input type="checkbox" name="benchmarks" value="${t.id}"> ${esc(t.title)}</label>`).join('')||'<p>Create a benchmark first.</p>'}</fieldset><div class="toolbar"><label>Group by <select name="group"><option value="configuration">Model + reasoning effort</option><option value="model">Model · all efforts</option></select></label></div><button type="submit">Compare common coverage</button></form><div id="coverage-result" class="spacer"></div>`;
 bindForm('#coverage',async f=>{const r=await api('aggregate',{benchmarks:f.getAll('benchmarks'),group:f.get('group')});document.querySelector('#coverage-result').innerHTML=`<h2>${r.coverage} selected benchmark${r.coverage===1?'':'s'}</h2><p>${esc(r.policy)}</p>`+(r.rows.length?`<div class="scroll"><table><thead><tr><th>Common identity</th><th>Balanced observed preference</th><th>Judged task coverage</th><th>Evidence</th></tr></thead><tbody>${r.rows.map(x=>`<tr><td>${esc(x.identity)}</td><td>${x.score===null?'Insufficient coverage':(100*x.score).toFixed(1)+'%'}</td><td>${x.evaluated_benchmarks} / ${r.coverage}<small>${x.judged_runs} / ${x.total_runs} dated runs judged</small></td><td>${x.judgments} judgments</td></tr>`).join('')}</tbody></table></div>`:'<p class="notice">No common model identities on this selection. Add matching models to each benchmark to build comparable coverage.</p>');});
}
document.querySelector('#close-expanded').onclick=()=>document.querySelector('#expanded').close();
document.querySelector('#expanded').addEventListener('close',()=>document.querySelector('#expanded-content').replaceChildren());
window.addEventListener('hashchange',route);route();
// Refresh changed run states without replacing websites the user is exploring.
setInterval(async()=>{
 if(!state || !state.generations.some(r=>['queued','running'].includes(r.status)) || ['new','coverage',''].includes(benchmark))return;
 const key=benchmark, version=routeVersion;
 try{
  const next=await api('benchmarks/'+key);if(version!==routeVersion)return;
  const changed=next.generations.some(r=>state.generations.find(x=>x.id===r.id)?.status!==r.status);
  if(!changed)return;
  const previous=state;state=next;
  if(tab==='generate'&&!document.activeElement?.closest('#add-config'))render();
  else if(tab==='results'){
   for(const r of next.generations){const old=previous.generations.find(x=>x.id===r.id);if(old&&old.status!==r.status){const el=document.querySelector(`[data-run="${r.id}"]`);if(el){const temp=document.createElement('template');temp.innerHTML=website(r);el.replaceWith(temp.content.firstElementChild);}}}
   bindCommon();
  }
  if(next.generations.some(r=>r.status==='ready'&&previous.generations.find(x=>x.id===r.id)?.status!=='ready'))notify('A new website is ready to explore.');
 }catch(e){/* A manual refresh retains a clear recovery path after disconnection. */}
},3000);
