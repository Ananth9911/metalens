/* MetaLens unified frontend.
   Requires the backend (AI features call the LLM). Graph features are
   deterministic. All three tabs operate on ONE shared catalog served by the API. */

const API = '';
const LAYER_COLORS = { raw:'#6b7785', staging:'#5aa9e6', mart:'#8b7bd8', report:'#4fbf9a' };
const LAYER_ORDER = ['raw','staging','mart','report'];

let CATALOG = {}, DOWNSTREAM = {}, UPSTREAM = {};
let state = { layer:'all', search:'', selected:null, mode:'overview' };

const SAMPLE_QS = [
  "where does revenue come from?",
  "what breaks if I change staging.stg_orders?",
  "which datasets contain PII?",
  "what feeds the executive summary?",
];
const SAMPLE_SQL = `CREATE TABLE mart.fct_refunds AS
SELECT
  o.order_id,
  o.user_id,
  o.amount        AS refund_amount,
  u.country
FROM staging.stg_orders o
JOIN mart.dim_users u ON o.user_id = u.user_id
WHERE o.status = 'refunded';`;

/* ---------- boot ---------- */
async function boot() {
  await refreshCatalog();
  try {
    const h = await (await fetch(`${API}/api/health`)).json();
    setPills(h);
  } catch(e){ document.getElementById('pills').innerHTML='<span class="pill warn">api down</span>'; }
  renderLayerFilter(); renderCatalog(); drawOverview();
  renderSamples();
  document.getElementById('sql-input').value = SAMPLE_SQL;
}
function setPills(h){
  document.getElementById('pills').innerHTML =
    `<span class="pill ${h.llm_configured?'ok':'warn'}">${h.llm_configured?'llm ok':'llm off'}</span>`+
    `<span class="pill">embed·${h.embedding_backend}</span>`+
    `<span class="pill">${h.stats.datasets} datasets</span>`;
}

async function refreshCatalog(){
  const g = await (await fetch(`${API}/api/graph`)).json();
  const list = await (await fetch(`${API}/api/datasets`)).json();
  CATALOG={}; DOWNSTREAM={}; UPSTREAM={};
  list.forEach(d=>{ CATALOG[d.id]=d; DOWNSTREAM[d.id]=DOWNSTREAM[d.id]||[]; UPSTREAM[d.id]=UPSTREAM[d.id]||[]; });
  g.edges.forEach(e=>{ (DOWNSTREAM[e.source]=DOWNSTREAM[e.source]||[]).push(e.target);
                       (UPSTREAM[e.target]=UPSTREAM[e.target]||[]).push(e.source); });
}

/* client-side traversals mirror the backend (used for graph drawing) */
function traverse(start, adj){ const seen=new Set(), st=[start];
  while(st.length){ const n=st.pop(); (adj[n]||[]).forEach(x=>{ if(!seen.has(x)){seen.add(x);st.push(x);} }); }
  seen.delete(start); return [...seen]; }
const upstreamOf=id=>traverse(id,UPSTREAM), downstreamOf=id=>traverse(id,DOWNSTREAM);
function buildOrder(){ const indeg={}; Object.keys(CATALOG).forEach(id=>indeg[id]=0);
  Object.keys(CATALOG).forEach(id=>(DOWNSTREAM[id]||[]).forEach(t=>indeg[t]++));
  const q=Object.keys(indeg).filter(n=>indeg[n]===0).sort(), order=[];
  while(q.length){ const n=q.shift(); order.push(n);
    (DOWNSTREAM[n]||[]).slice().sort().forEach(t=>{ if(--indeg[t]===0) q.push(t); }); }
  return order; }
function impactOf(id){ const aff=downstreamOf(id), byL={};
  aff.forEach(a=>{const L=CATALOG[a].layer;(byL[L]=byL[L]||[]).push(a);});
  return {total:aff.length, affected:aff, byLayer:byL}; }
function columnLineage(dsId,colName){ const edges=[],seen=new Set(),ds=CATALOG[dsId];
  const col=ds&&ds.columns.find(c=>c.name===colName); if(!col) return edges;
  const st=[[`${dsId}.${colName}`,col]];
  while(st.length){ const [ref,cur]=st.pop();
    (cur.derived_from||[]).forEach(p=>{ edges.push([p,ref]);
      if(seen.has(p))return; seen.add(p);
      const dot=p.lastIndexOf('.'),pid=p.slice(0,dot),pcol=p.slice(dot+1),pds=CATALOG[pid];
      const pc=pds&&pds.columns.find(c=>c.name===pcol);
      if(pc&&pc.derived_from&&pc.derived_from.length) st.push([p,pc]); }); }
  return edges; }

/* ---------- tabs ---------- */
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('view-'+t.dataset.tab).classList.add('active');
  if(t.dataset.tab==='graph'){ if(state.mode==='overview') drawOverview();
    else if(state.selected) drawLineage(state.selected); }
});
function gotoTab(name){ document.querySelector(`.tab[data-tab="${name}"]`).click(); }

/* ================= GRAPH TAB ================= */
function renderLayerFilter(){
  const el=document.getElementById('layer-filter'), layers=['all',...LAYER_ORDER];
  el.innerHTML=layers.map(L=>`<span class="chip ${state.layer===L?'active':''}" data-layer="${L}">${L}</span>`).join('');
  el.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{ state.layer=c.dataset.layer; renderLayerFilter(); renderCatalog(); });
}
function renderCatalog(){
  const list=document.getElementById('dataset-list');
  let items=Object.values(CATALOG);
  if(state.layer!=='all') items=items.filter(d=>d.layer===state.layer);
  if(state.search){ const q=state.search.toLowerCase();
    items=items.filter(d=>(`${d.id} ${d.name} ${d.owner} ${(d.tags||[]).join(' ')}`).toLowerCase().includes(q)); }
  items.sort((a,b)=>LAYER_ORDER.indexOf(a.layer)-LAYER_ORDER.indexOf(b.layer)||a.id.localeCompare(b.id));
  if(!items.length){ list.innerHTML='<div class="none" style="padding:16px">No datasets match.</div>'; return; }
  list.innerHTML=items.map(d=>{
    const ai=(d.tags||[]).includes('ai-extracted')?'ai-tag':'';
    return `<div class="ds-item ${state.selected===d.id?'selected':''} ${ai}" data-id="${d.id}">
      <div class="ds-id"><span class="ds-dot" style="background:${LAYER_COLORS[d.layer]}"></span>${d.id}</div>
      <div class="ds-meta">${d.layer} · ${d.owner} · ${d.columns.length} cols</div></div>`;
  }).join('');
  list.querySelectorAll('.ds-item').forEach(el=>el.onclick=()=>select(el.dataset.id));
}
document.getElementById('search').addEventListener('input',e=>{ state.search=e.target.value; renderCatalog(); });

function select(id){ if(!CATALOG[id])return; state.selected=id; state.mode='lineage';
  gotoTab('graph'); renderCatalog(); drawLineage(id); renderInspector(id); }

/* D3 drawing */
function gc(){ const g=document.getElementById('graph'); return {w:g.clientWidth||900,h:g.clientHeight||500}; }
function baseSvg(){ const {w,h}=gc(); d3.select('#graph').selectAll('*').remove();
  const svg=d3.select('#graph').append('svg').attr('viewBox',`0 0 ${w} ${h}`);
  const grid=svg.append('g');
  for(let x=0;x<w;x+=32) grid.append('line').attr('class','grid-line').attr('x1',x).attr('y1',0).attr('x2',x).attr('y2',h);
  for(let y=0;y<h;y+=32) grid.append('line').attr('class','grid-line').attr('x1',0).attr('y1',y).attr('x2',w).attr('y2',y);
  const root=svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.4,2.5]).on('zoom',ev=>root.attr('transform',ev.transform)));
  const defs=svg.append('defs');
  [['arr','#24303d'],['arr-up','#1b8f96'],['arr-down','#f0a54a']].forEach(([id,col])=>{
    defs.append('marker').attr('id',id).attr('viewBox','0 -5 10 10').attr('refX',9).attr('refY',0)
      .attr('markerWidth',6).attr('markerHeight',6).attr('orient','auto')
      .append('path').attr('d','M0,-5L10,0L0,5').attr('fill',col); });
  return {svg,root,w,h}; }
function layout(nodes){ const order=buildOrder(),rank={}; order.forEach((id,i)=>rank[id]=i);
  const cols={raw:0,staging:1,mart:2,report:3},buckets={0:[],1:[],2:[],3:[]};
  nodes.forEach(n=>buckets[cols[n.layer]].push(n));
  Object.values(buckets).forEach(b=>b.sort((a,b2)=>rank[a.id]-rank[b2.id]));
  const {w,h}=gc(),colGap=Math.max(190,(w-160)/4),pos={};
  Object.entries(buckets).forEach(([c,b])=>{ const x=70+(+c)*colGap,gap=Math.min(74,(h-70)/Math.max(b.length,1)),sy=(h-(b.length-1)*gap)/2;
    b.forEach((n,i)=>pos[n.id]={x,y:sy+i*gap,layer:n.layer,role:n.role}); });
  return pos; }
function renderNodes(root,pos){ const NW=150,NH=40;
  const edges=[]; Object.keys(pos).forEach(s=>(DOWNSTREAM[s]||[]).forEach(t=>{ if(pos[t]){
    const cls = (state.mode==='lineage')?edgeClass(s,t):''; edges.push({source:s,target:t,cls}); } }));
  root.selectAll('.edge').data(edges).enter().append('path').attr('class',d=>`edge ${d.cls||''}`)
    .attr('marker-end',d=>`url(#${d.cls==='up'?'arr-up':d.cls==='down'?'arr-down':'arr'})`)
    .attr('d',d=>{ const s=pos[d.source],t=pos[d.target],sx=s.x+NW/2,tx=t.x-NW/2,mx=(sx+tx)/2;
      return `M${sx},${s.y} C${mx},${s.y} ${mx},${t.y} ${tx},${t.y}`; });
  const g=root.selectAll('.node-box').data(Object.entries(pos)).enter().append('g')
    .attr('class',([id,p])=>`node-box ${p.role||''}`)
    .attr('transform',([id,p])=>`translate(${p.x-NW/2},${p.y-NH/2})`)
    .on('click',(ev,[id])=>select(id));
  g.append('rect').attr('width',NW).attr('height',NH);
  g.append('rect').attr('width',4).attr('height',NH).attr('fill',([id,p])=>LAYER_COLORS[p.layer]);
  g.append('text').attr('class','node-label').attr('x',12).attr('y',17).text(([id])=>id.split('.').pop().slice(0,20));
  g.append('text').attr('class','node-sub').attr('x',12).attr('y',30).text(([id,p])=>p.layer); }
let _up=new Set(),_down=new Set(),_focus=null;
function edgeClass(s,t){ if((_up.has(s)||s===_focus)&&(_up.has(t)||t===_focus))return 'up';
  if((_down.has(s)||s===_focus)&&(_down.has(t)||t===_focus))return 'down'; return ''; }
function drawOverview(){ state.mode='overview'; state.selected=null; renderCatalog();
  document.getElementById('canvas-title').innerHTML='Full pipeline';
  const {root}=baseSvg();
  const nodes=Object.values(CATALOG).map(d=>({id:d.id,layer:d.layer,role:''}));
  renderNodes(root,layout(nodes)); setLegend(false);
  document.getElementById('canvas-foot').textContent=`${nodes.length} datasets · left→right = raw→report · scroll to zoom, drag to pan`; }
function drawLineage(id){ const up=upstreamOf(id),down=downstreamOf(id);
  _up=new Set(up);_down=new Set(down);_focus=id;
  const ids=new Set([...up,...down,id]);
  document.getElementById('canvas-title').innerHTML=`Lineage · <span class="focus-id">${id}</span>`;
  const {root}=baseSvg();
  const nodes=[...ids].map(n=>({id:n,layer:CATALOG[n].layer,role:n===id?'focus':(up.includes(n)?'upstream':'downstream')}));
  renderNodes(root,layout(nodes)); setLegend(true);
  document.getElementById('canvas-foot').innerHTML=`<b style="color:var(--signal)">${up.length}</b> upstream · <b style="color:var(--impact)">${down.length}</b> downstream · cyan feeds this · amber fed by this`; }
function setLegend(lin){ const el=document.getElementById('legend');
  el.innerHTML = lin
    ? `<span class="lg"><i style="background:var(--signal-deep)"></i>upstream</span><span class="lg"><i style="background:var(--focus)"></i>focus</span><span class="lg"><i style="background:var(--impact)"></i>downstream</span>`
    : LAYER_ORDER.map(L=>`<span class="lg"><i style="background:${LAYER_COLORS[L]}"></i>${L}</span>`).join(''); }
function showBuildOrder(){ document.getElementById('canvas-title').innerHTML='Build order · topological sort';
  const order=buildOrder(),g=document.getElementById('graph');
  g.innerHTML='<div class="buildorder">'+order.map((id,i)=>`<div class="bo-row"><span class="bo-n">${String(i+1).padStart(2,'0')}</span><span class="bo-layer" style="background:${LAYER_COLORS[CATALOG[id].layer]}"></span>${id}</div>`).join('')+'</div>';
  document.getElementById('legend').innerHTML='';
  document.getElementById('canvas-foot').textContent="Kahn's algorithm, O(V+E). Every dataset appears after all its sources."; }
document.getElementById('btn-overview').onclick=drawOverview;
document.getElementById('btn-buildorder').onclick=showBuildOrder;

/* inspector */
function renderInspector(id){ const d=CATALOG[id];
  document.getElementById('inspector-empty').hidden=true;
  const body=document.getElementById('inspector-body'); body.hidden=false;
  const imp=impactOf(id);
  const impLayers=Object.entries(imp.byLayer).map(([L,a])=>`<span class="il"><b>${a.length}</b> ${L}</span>`).join('')||'<span class="none">no downstream datasets</span>';
  const cols=d.columns.map(c=>{ const lin=columnLineage(id,c.name); let lh='';
    if(lin.length){ const back={}; lin.forEach(([f,t])=>back[t]=f); const chain=[]; let cur=`${id}.${c.name}`;
      while(back[cur]){chain.unshift(cur);cur=back[cur];} chain.unshift(cur);
      lh=`<div class="clineage">${chain.map((s,i)=>`${i?'<span class="arrow"> → </span>':''}${s}`).join('')}</div>`; }
    return `<div class="col-row"><span class="cn">${c.name}</span><span class="ct">${c.type}</span>${c.description?`<span class="cd">${c.description}</span>`:''}${lh}</div>`;
  }).join('');
  const srcs=(UPSTREAM[id]||[]).length?`<div class="link-list">${UPSTREAM[id].map(lnk).join('')}</div>`:'<span class="none">— none (root / raw source)</span>';
  const tgts=(DOWNSTREAM[id]||[]).length?`<div class="link-list">${DOWNSTREAM[id].map(lnk).join('')}</div>`:'<span class="none">— none (leaf / final report)</span>';
  body.innerHTML=`<div class="insp-head">
      <span class="insp-layer" style="background:${LAYER_COLORS[d.layer]}">${d.layer}</span>
      <div class="insp-id">${d.id}</div><div class="insp-name">${d.name}</div>
      <div class="insp-desc">${d.description||''}</div><div class="insp-owner">owner · ${d.owner}</div>
      <div class="insp-tags">${(d.tags||[]).map(t=>`<span>${t}</span>`).join('')}</div></div>
    <div class="insp-section"><h3>Impact if changed</h3>
      <div class="impact-meter"><div class="impact-big ${imp.total===0?'zero':''}">${imp.total}</div>
      <div class="impact-cap">downstream dataset${imp.total===1?'':'s'} affected</div>
      <div class="impact-layers">${impLayers}</div></div></div>
    <div class="insp-section"><h3>Columns <span class="count">${d.columns.length}</span></h3>${cols}</div>
    <div class="insp-section"><h3>Sources <span class="count">${(UPSTREAM[id]||[]).length}</span></h3>${srcs}</div>
    <div class="insp-section"><h3>Feeds <span class="count">${(DOWNSTREAM[id]||[]).length}</span></h3>${tgts}</div>
    <button class="ask-btn" id="ask-about">✦ Ask the AI about ${d.id}</button>`;
  body.querySelectorAll('[data-goto]').forEach(a=>a.onclick=()=>select(a.dataset.goto));
  document.getElementById('ask-about').onclick=()=>{ gotoTab('ask');
    document.getElementById('ask-input').value=`Tell me about ${id} — where does its data come from and what depends on it?`; ask(); };
}
function lnk(id){ const d=CATALOG[id]; return `<a data-goto="${id}"><span class="ld" style="background:${LAYER_COLORS[d.layer]}"></span>${id}</a>`; }

let rt; window.addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(()=>{
  if(!document.getElementById('view-graph').classList.contains('active'))return;
  if(state.mode==='overview')drawOverview(); else if(state.selected)drawLineage(state.selected); },200);});

/* ================= ASK TAB (RAG) ================= */
function renderSamples(){ document.getElementById('samples').innerHTML=SAMPLE_QS.map(q=>`<button class="sample">${q}</button>`).join('');
  document.querySelectorAll('#samples .sample').forEach(b=>b.onclick=()=>{ document.getElementById('ask-input').value=b.textContent; ask(); }); }
function citeDatasets(text){ return text.replace(/\[([a-z0-9_]+\.[a-z0-9_]+)\]/gi,'<span class="dsref" data-ds="$1">$1</span>'); }
async function ask(){ const input=document.getElementById('ask-input'),q=input.value.trim(); if(!q)return; input.value='';
  const log=document.getElementById('ask-log'),hint=log.querySelector('.hint-card'); if(hint)hint.remove();
  const block=document.createElement('div'); block.className='qa';
  block.innerHTML=`<div class="q-line"><span class="q-caret">&gt;</span><span class="q-text">${q}</span></div><div class="a-body loading">retrieving catalog context and asking the model…</div>`;
  log.appendChild(block); log.scrollTop=log.scrollHeight; const a=block.querySelector('.a-body');
  try{ const r=await fetch(`${API}/api/ask`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});
    if(!r.ok){const e=await r.json();throw new Error(e.detail||'request failed');}
    const d=await r.json(); a.classList.remove('loading'); a.innerHTML=citeDatasets(d.answer.replace(/\n/g,'<br>'));
    a.querySelectorAll('.dsref').forEach(s=>s.onclick=()=>select(s.dataset.ds));
    if(d.retrieved&&d.retrieved.length){ const rr=document.createElement('div'); rr.className='retrieved';
      rr.innerHTML=`<span class="rl">retrieved</span>`+d.retrieved.map(x=>`<span class="rchip" data-ds="${x.id}">${x.id} <b>${x.score}</b></span>`).join('');
      block.appendChild(rr); rr.querySelectorAll('.rchip').forEach(s=>s.onclick=()=>select(s.dataset.ds)); }
  }catch(err){ a.classList.remove('loading'); a.classList.add('err'); a.textContent='⚠ '+err.message+'  (is GROQ_API_KEY set? see README)'; }
  log.scrollTop=log.scrollHeight; }
document.getElementById('ask-send').onclick=ask;
document.getElementById('ask-input').addEventListener('keydown',e=>{if(e.key==='Enter')ask();});

/* ================= EXTRACT TAB ================= */
async function runExtract(){ const sql=document.getElementById('sql-input').value.trim(),add=document.getElementById('sql-add').checked,out=document.getElementById('extract-out');
  if(!sql){out.innerHTML='<div class="a-body err">Paste some SQL first.</div>';return;}
  out.innerHTML='<div class="a-body loading">reading the SQL with the model…</div>';
  try{ const r=await fetch(`${API}/api/extract-lineage`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sql,add_to_catalog:add})});
    if(!r.ok){const e=await r.json();throw new Error(e.detail||'request failed');}
    const d=await r.json(); let html='';
    if(d.added_to_catalog) html+=`<div class="added-badge">✓ added ${d.target} to catalog & graph — open the Graph tab</div>`;
    html+=`<div class="result-target">target: <b>${d.target}</b></div><div class="result-sub">${d.columns.length} columns · inferred by the model</div>`;
    html+=d.columns.map(c=>{ const from=(c.derived_from&&c.derived_from.length)
      ?`<div class="lc-from">${c.derived_from.map((f,i)=>`${i?'<span class="arrow"> + </span>':''}${f}`).join('')} <span class="arrow">→</span> ${c.name}</div>`
      :`<div class="lc-none">— no source columns (constant / generated)</div>`;
      return `<div class="lin-col"><div class="lc-name">${c.name}</div>${from}</div>`; }).join('');
    if(d.rejected_refs&&d.rejected_refs.length) html+=`<div class="rejected-box"><div class="rb-title">rejected — not in known catalog (possible hallucination)</div>`+d.rejected_refs.map(x=>`<div class="rb-item">${x}</div>`).join('')+`</div>`;
    out.innerHTML=html;
    if(add){ await refreshCatalog(); renderCatalog(); if(document.getElementById('view-graph').classList.contains('active')) drawOverview();
      const h=await(await fetch(`${API}/api/health`)).json(); setPills(h); }
  }catch(err){ out.innerHTML=`<div class="a-body err">⚠ ${err.message}<br><br>Is GROQ_API_KEY set? See the README.</div>`; }
}
document.getElementById('sql-run').onclick=runExtract;

boot();
