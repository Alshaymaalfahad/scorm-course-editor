const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
let state = null;
let currentPkg = null;
let currentPage = null;
let currentData = null;
let currentMedia = [];
let activeTab = 'content';
let dirty = false;

function toast(msg, error=false){ const el=$('#toast'); el.textContent=msg; el.className='toast show'+(error?' error':''); clearTimeout(toast.t); toast.t=setTimeout(()=>el.className='toast',2600); }
function loading(show,text='جاري المعالجة...'){ $('#loading').classList.toggle('hidden',!show); $('#loadingText').textContent=text; }
function fmtBytes(n){ if(!n)return '0 B'; const u=['B','KB','MB','GB']; let i=0,v=n; while(v>=1024&&i<u.length-1){v/=1024;i++} return `${v.toFixed(v<10&&i?1:0)} ${u[i]}`; }
function esc(s=''){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function setDirty(v=true){ dirty=v; $('#dirtyFlag').classList.toggle('hidden',!dirty); }
async function api(url,opt={}){ const r=await fetch(url,opt); if(!r.ok){ let e; try{e=(await r.json()).error}catch{e=await r.text()} throw new Error(e||`HTTP ${r.status}`);} const ct=r.headers.get('content-type')||''; return ct.includes('json')?r.json():r; }

const fileInput=$('#fileInput');
$('#chooseBtn').onclick=()=>fileInput.click();
fileInput.onchange=()=>fileInput.files[0]&&upload(fileInput.files[0]);
const drop=$('#dropZone');
['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('drag')}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('drag')}));
drop.addEventListener('drop',ev=>{const f=ev.dataTransfer.files[0];if(f)upload(f)});

async function upload(file){
  if(!file.name.toLowerCase().endsWith('.zip')) return toast('يرجى اختيار ملف ZIP',true);
  loading(true,'جاري تجهيز رفع المقرر...');
  try{
    // Upload in chunks so large courses can pass through common reverse-proxy limits.
    const start=await api('/api/upload/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:file.name,size:file.size})});
    const chunkSize=start.chunkSize||50*1024*1024;
    let offset=0,part=0,totalParts=Math.ceil(file.size/chunkSize);
    while(offset<file.size){
      const end=Math.min(offset+chunkSize,file.size); const chunk=file.slice(offset,end); part++;
      loading(true,`جاري رفع المقرر... ${part}/${totalParts} (${Math.round(end/file.size*100)}%)`);
      await api(`/api/upload/chunk?id=${encodeURIComponent(start.uploadId)}&offset=${offset}`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:chunk});
      offset=end;
    }
    loading(true,'اكتمل الرفع، جاري تحليل حزم SCORM والوحدات...');
    state=await api('/api/upload/finish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uploadId:start.uploadId})});
    $('#welcome').classList.add('hidden'); $('#workspace').classList.remove('hidden');
    $('#validateBtn').disabled=false; $('#exportBtn').disabled=false;
    renderState(); toast(`تم العثور على ${state.packageCount} وحدات و ${state.pageCount} شاشات`);
  }catch(e){toast(e.message,true)}finally{loading(false)}
}

function renderState(){
  $('#fileName').textContent=state.originalName; $('#pkgCount').textContent=state.packageCount; $('#pageCount').textContent=state.pageCount;
  renderNav();
}
function renderNav(filter=''){
  const q=filter.trim().toLowerCase();
  $('#nav').innerHTML=state.packages.map(pkg=>{
    const pages=pkg.pages.filter(p=>!q||`${p.title} ${p.name} ${p.type} ${p.template}`.toLowerCase().includes(q));
    if(q&&!pages.length&&!`${pkg.courseName} ${pkg.unitTitle}`.toLowerCase().includes(q))return '';
    return `<div class="package"><div class="package-title"><span>${esc(pkg.unitTitle||pkg.courseName||pkg.name)}</span><small>${pages.length}</small></div>${pages.map((p,i)=>`<div class="page-link ${currentPkg?.id===pkg.id&&currentPage===p.name?'active':''}" data-pkg="${esc(pkg.id)}" data-page="${esc(p.name)}"><div class="page-num">${String(p.index+1).padStart(2,'0')}</div><div class="page-text"><span>${esc(p.title||p.name)}</span><small>${esc(p.template||p.type)}</small></div></div>`).join('')}</div>`;
  }).join('');
  $$('.page-link').forEach(el=>el.onclick=()=>openPage(el.dataset.pkg,el.dataset.page));
}
$('#searchInput').oninput=e=>renderNav(e.target.value);

async function openPage(pkgId,pageName){
  if(dirty && !confirm('لديك تغييرات غير محفوظة. هل تريد الانتقال بدون حفظها؟')) return;
  loading(true,'جاري فتح الشاشة...');
  try{
    currentPkg=state.packages.find(p=>p.id===pkgId); currentPage=pageName;
    const resp=await api(`/api/page?package=${encodeURIComponent(pkgId)}&page=${encodeURIComponent(pageName)}`);
    currentData=resp.data; currentMedia=resp.media||[]; setDirty(false);
    $('#emptyEditor').classList.add('hidden'); $('#editor').classList.remove('hidden');
    $('#pageTitle').value=resp.meta.title||'';
    $('#pageHeading').textContent=resp.meta.title||pageName;
    $('#pageBadge').textContent=resp.meta.type||'screen';
    $('#pageMeta').textContent=`${pageName} · ${resp.meta.template||'—'}`;
    $('#previewType').textContent=resp.meta.template||resp.meta.type||'';
    $('#courseName').value=currentPkg.courseName||''; $('#unitTitle').value=currentPkg.unitTitle||''; $('#courseCode').value=currentPkg.courseCode||''; $('#scormVersion').value=currentPkg.scormVersion||''; $('#pdfName').value=currentPkg.pdf||'';
    renderContent(); renderMedia(); renderRaw(); renderPreview(); renderNav($('#searchInput').value);
  }catch(e){toast(e.message,true)}finally{loading(false)}
}

function rootInfo(){
  if(!currentData||typeof currentData!=='object')return {key:'',body:{}};
  const key=Object.keys(currentData)[0]||''; const body=currentData[key]||{}; return {key,body};
}
function renderContent(){
  const {key,body}=rootInfo(); const host=$('#contentEditor');
  if(['quiz','activity'].includes(key) || Array.isArray(body.questions)){
    host.innerHTML=`<div class="grid2"><div class="field"><label>نوع النشاط</label><input value="${esc(body.type||key)}" disabled></div>${key==='quiz'?`<div class="field"><label>نسبة النجاح %</label><input id="passPct" type="number" min="0" max="100" value="${esc(body.passPercentage||'80')}"></div>`:''}</div><div class="panel-title"><div><h3>الأسئلة</h3><p>${(body.questions||[]).length} سؤال</p></div><button id="addQuestion" class="button secondary">+ إضافة سؤال</button></div><div id="questions"></div>`;
    renderQuestions(); $('#addQuestion').onclick=()=>{collectQuestions(); body.questions=body.questions||[]; body.questions.push({type:'choice',sound:'',header:'سؤال جديد',choice1:'الخيار الأول',choice2:'الخيار الثاني',answer:'1',random:''}); renderQuestions(); setDirty(); renderPreview();};
  }else if(Object.prototype.hasOwnProperty.call(body,'screenText')){
    host.innerHTML=`<div class="field"><label>نص الشاشة (HTML)</label><textarea id="screenText" class="screen-text">${esc(body.screenText||'')}</textarea></div>${Object.prototype.hasOwnProperty.call(body,'sound')?`<div class="field"><label>ملف الصوت</label><input id="soundName" value="${esc(body.sound||'')}"></div>`:''}`;
    $('#screenText').addEventListener('input',()=>{body.screenText=$('#screenText').value;setDirty();renderPreview()});
    if($('#soundName'))$('#soundName').addEventListener('input',()=>{body.sound=$('#soundName').value;setDirty()});
  }else{
    host.innerHTML=`<div class="card"><strong>قالب غير معروف في النسخة المبسطة</strong><p class="muted">يمكن تعديل هذه الشاشة بالكامل من تبويب JSON دون فقد أي حقل.</p><p>Root key: <code>${esc(key)}</code></p></div>`;
  }
  $('#pageTitle').oninput=()=>setDirty();
  if($('#passPct'))$('#passPct').oninput=()=>{body.passPercentage=$('#passPct').value;setDirty();renderPreview()};
}

function renderQuestions(){
  const {body}=rootInfo(); const qs=body.questions||[]; const host=$('#questions');
  host.innerHTML=qs.map((q,i)=>questionCard(q,i)).join('');
  host.querySelectorAll('input,textarea,select').forEach(el=>el.addEventListener('input',()=>{setDirty();collectQuestions();renderPreview()}));
  host.querySelectorAll('[data-role=type]').forEach(el=>el.addEventListener('change',()=>{collectQuestions();renderQuestions();setDirty();renderPreview()}));
  host.querySelectorAll('[data-act]').forEach(btn=>btn.onclick=()=>questionAction(btn.dataset.act,+btn.dataset.i));
}
function questionCard(q,i){
  let details=''; const t=q.type||'choice';
  if(t==='matching'){
    const a=q.column1||[],b=q.column2||[]; const n=Math.max(a.length,b.length);
    details=`<div class="field"><label>أزواج المطابقة</label>${Array.from({length:n},(_,j)=>`<div class="matching-row"><textarea data-role="m1" data-j="${j}" rows="2">${esc(a[j]||'')}</textarea><textarea data-role="m2" data-j="${j}" rows="2">${esc(b[j]||'')}</textarea></div>`).join('')}</div>`;
  }else if(t==='sorting'){
    details=`<div class="field"><label>الترتيب الصحيح</label>${(q.items||[]).map((v,j)=>`<div class="choice-row"><div class="question-index">${j+1}</div><input data-role="sort" data-j="${j}" value="${esc(v)}"></div>`).join('')}</div>`;
  }else{
    const choices=Object.keys(q).filter(k=>/^choice\d+$/.test(k)).sort((a,b)=>+a.slice(6)-+b.slice(6));
    const ans=String(q.answer||'').split(',').map(x=>x.trim()); const multi=t==='multipleChoice';
    details=`<div class="field"><label>الخيارات والإجابة الصحيحة</label>${choices.map((k,j)=>`<div class="choice-row"><input data-role="ans" data-j="${j+1}" type="${multi?'checkbox':'radio'}" name="ans-${i}" ${ans.includes(String(j+1))?'checked':''}><input data-role="choice" data-j="${j+1}" value="${esc(q[k]||'')}"></div>`).join('')}</div>`;
  }
  return `<div class="card question" data-i="${i}"><div class="card-head"><div class="qtitle"><div class="question-index">${i+1}</div><select class="question-type" data-role="type"><option value="choice" ${t==='choice'?'selected':''}>اختيار واحد / صح-خطأ</option><option value="multipleChoice" ${t==='multipleChoice'?'selected':''}>اختيار متعدد</option><option value="matching" ${t==='matching'?'selected':''}>مطابقة</option><option value="sorting" ${t==='sorting'?'selected':''}>ترتيب</option></select></div><div class="card-actions"><button class="icon-btn" data-act="up" data-i="${i}" title="أعلى">↑</button><button class="icon-btn" data-act="down" data-i="${i}" title="أسفل">↓</button><button class="icon-btn danger" data-act="delete" data-i="${i}" title="حذف">×</button></div></div><div class="field"><label>السؤال</label><textarea data-role="header" rows="3">${esc(q.header||'')}</textarea></div>${details}</div>`;
}
function collectQuestions(){
  const {body}=rootInfo(); if(!Array.isArray(body.questions))return;
  $$('.question').forEach(card=>{
    const i=+card.dataset.i; const q=body.questions[i]; if(!q)return;
    const newType=card.querySelector('[data-role=type]').value; q.type=newType; q.header=card.querySelector('[data-role=header]').value;
    // Remove old type-specific fields only when type has changed.
    if(newType==='matching'){
      Object.keys(q).filter(k=>/^choice\d+$/.test(k)).forEach(k=>delete q[k]); delete q.items; delete q.answer;
      q.column1=[...card.querySelectorAll('[data-role=m1]')].map(x=>x.value); q.column2=[...card.querySelectorAll('[data-role=m2]')].map(x=>x.value);
      if(!q.column1.length){q.column1=['العنصر الأول','العنصر الثاني'];q.column2=['المطابقة الأولى','المطابقة الثانية'];}
    } else if(newType==='sorting'){
      Object.keys(q).filter(k=>/^choice\d+$/.test(k)).forEach(k=>delete q[k]); delete q.column1; delete q.column2; delete q.answer;
      q.items=[...card.querySelectorAll('[data-role=sort]')].map(x=>x.value); if(!q.items.length)q.items=['الخطوة الأولى','الخطوة الثانية'];
    } else {
      delete q.column1; delete q.column2; delete q.items;
      const choices=[...card.querySelectorAll('[data-role=choice]')];
      // If type was changed from matching/sorting, card has no choices yet; seed two.
      if(!choices.length){q.choice1='الخيار الأول';q.choice2='الخيار الثاني';q.answer='1';}
      else{
        Object.keys(q).filter(k=>/^choice\d+$/.test(k)).forEach(k=>delete q[k]);
        choices.forEach(x=>q['choice'+x.dataset.j]=x.value);
        const selected=[...card.querySelectorAll('[data-role=ans]:checked')].map(x=>x.dataset.j); q.answer=selected.join(',')||'1';
      }
    }
  });
}
function questionAction(act,i){
  collectQuestions(); const {body}=rootInfo(); const q=body.questions;
  if(act==='delete'){if(confirm('حذف هذا السؤال؟'))q.splice(i,1)}
  if(act==='up'&&i>0)[q[i-1],q[i]]=[q[i],q[i-1]];
  if(act==='down'&&i<q.length-1)[q[i+1],q[i]]=[q[i],q[i+1]];
  renderQuestions();setDirty();renderPreview();
}

function renderRaw(){ $('#rawJson').value=JSON.stringify(currentData,null,2); }
$('#rawJson').addEventListener('input',()=>setDirty());

function renderMedia(){
  const host=$('#mediaList');
  if(!currentMedia.length){host.innerHTML='<div class="card muted">لم يتم اكتشاف وسائط مرتبطة مباشرة بهذه الشاشة. يمكن أن تكون الوسائط معرفة بواسطة القالب نفسه.</div>';return;}
  host.innerHTML=currentMedia.map((m,i)=>`<div class="media-item"><div class="media-top"><div><div class="media-name">${esc(m.name)}</div><div class="media-path">${esc(m.path)}</div></div><span class="badge">${esc(m.extension.replace('.','').toUpperCase())} · ${fmtBytes(m.size)}</span></div><div id="player-${i}"></div><div class="media-actions"><button class="button secondary" data-preview="${i}">معاينة</button><label class="button secondary">استبدال<input type="file" hidden data-replace="${i}"></label></div></div>`).join('');
  $$('[data-preview]').forEach(b=>b.onclick=()=>previewMedia(+b.dataset.preview));
  $$('[data-replace]').forEach(inp=>inp.onchange=()=>inp.files[0]&&replaceMedia(+inp.dataset.replace,inp.files[0]));
}
function previewMedia(i){
  const m=currentMedia[i]; const url=`/api/media?package=${encodeURIComponent(currentPkg.id)}&path=${encodeURIComponent(m.path)}&v=${Date.now()}`; const host=$(`#player-${i}`); const ext=m.extension;
  if(['.mp4','.webm','.mov'].includes(ext))host.innerHTML=`<video class="media-player" controls src="${url}"></video>`;
  else if(['.mp3','.wav','.ogg','.m4a'].includes(ext))host.innerHTML=`<audio class="media-player" controls src="${url}"></audio>`;
  else if(['.png','.jpg','.jpeg','.gif','.svg'].includes(ext))host.innerHTML=`<img class="media-player" src="${url}">`;
  else host.innerHTML=`<a class="button secondary" href="${url}" target="_blank">فتح الملف</a>`;
}
async function replaceMedia(i,file){
  const m=currentMedia[i]; loading(true,'جاري استبدال ملف الوسائط...');
  try{await api(`/api/replace-media?package=${encodeURIComponent(currentPkg.id)}&path=${encodeURIComponent(m.path)}`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});m.size=file.size;renderMedia();toast('تم استبدال الوسائط وسيتم تضمينها عند التصدير');}
  catch(e){toast(e.message,true)}finally{loading(false)}
}

function collectContent(){
  if(activeTab==='raw'){
    try{currentData=JSON.parse($('#rawJson').value)}catch(e){throw new Error('JSON غير صالح: '+e.message)}
    return;
  }
  const {body}=rootInfo();
  if(Array.isArray(body.questions))collectQuestions();
  if($('#screenText'))body.screenText=$('#screenText').value;
  if($('#soundName'))body.sound=$('#soundName').value;
  if($('#passPct'))body.passPercentage=$('#passPct').value;
}

async function savePage(){
  if(!currentPkg||!currentPage)return;
  try{collectContent()}catch(e){return toast(e.message,true)}
  loading(true,'جاري حفظ التعديلات...');
  try{
    const r=await api('/api/page',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({package:currentPkg.id,page:currentPage,title:$('#pageTitle').value,data:currentData})});
    const idx=state.packages.findIndex(p=>p.id===currentPkg.id); state.packages[idx]=r.package; currentPkg=state.packages[idx];
    $('#pageHeading').textContent=$('#pageTitle').value||currentPage; setDirty(false); renderRaw(); renderNav($('#searchInput').value); toast('تم حفظ الشاشة');
  }catch(e){toast(e.message,true)}finally{loading(false)}
}
$('#saveBtn').onclick=savePage;

$('#savePackageBtn').onclick=async()=>{
  loading(true,'جاري حفظ بيانات الوحدة...');
  try{
    const r=await api('/api/package',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({package:currentPkg.id,courseName:$('#courseName').value,unitTitle:$('#unitTitle').value,pdf:$('#pdfName').value})});
    const idx=state.packages.findIndex(p=>p.id===currentPkg.id);state.packages[idx]=r.package;currentPkg=state.packages[idx];renderNav($('#searchInput').value);toast('تم حفظ بيانات الوحدة');
  }catch(e){toast(e.message,true)}finally{loading(false)}
};

$$('.tab').forEach(btn=>btn.onclick=()=>switchTab(btn.dataset.tab));
function switchTab(tab){
  if(activeTab==='raw'&&tab!=='raw'){
    try{currentData=JSON.parse($('#rawJson').value);renderContent();renderPreview()}catch(e){return toast('صحح JSON قبل مغادرة التبويب',true)}
  }else if(tab==='raw'&&activeTab!=='raw'){
    try{collectContent();renderRaw()}catch(e){return toast(e.message,true)}
  }
  activeTab=tab; $$('.tab').forEach(x=>x.classList.toggle('active',x.dataset.tab===tab)); $$('.tab-panel').forEach(x=>x.classList.remove('active')); $(`#tab-${tab}`).classList.add('active');
}

function renderPreview(){
  if(!currentData)return; const {key,body}=rootInfo(); const host=$('#preview');
  if(Object.prototype.hasOwnProperty.call(body,'screenText')){
    host.innerHTML=`<div class="screen-preview">${body.screenText||'<span class="muted">لا يوجد نص</span>'}</div>`;
  }else if(Array.isArray(body.questions)){
    host.innerHTML=body.questions.map((q,i)=>{
      let items=[]; if(q.type==='matching')items=(q.column1||[]).map((x,j)=>`${x} ↔ ${(q.column2||[])[j]||''}`); else if(q.type==='sorting')items=q.items||[]; else items=Object.keys(q).filter(k=>/^choice\d+$/.test(k)).sort((a,b)=>+a.slice(6)-+b.slice(6)).map(k=>q[k]);
      return `<div class="preview-question"><strong>${i+1}. ${q.header||''}</strong><ul>${items.map(x=>`<li>${x}</li>`).join('')}</ul>${q.answer?`<div class="preview-answer">الإجابة: ${esc(q.answer)}</div>`:''}</div>`;
    }).join('');
  }else host.innerHTML=`<div class="preview-empty">المعاينة البصرية غير متاحة لقالب ${esc(key)}. استخدم JSON للتعديل.</div>`;
}

$('#validateBtn').onclick=async()=>{
  loading(true,'جاري فحص بنية المقرر...');
  try{const v=await api('/api/validate');renderValidation(v);$('#validationDialog').showModal()}catch(e){toast(e.message,true)}finally{loading(false)}
};
$('#closeValidation').onclick=()=>$('#validationDialog').close();
function renderValidation(v){
  let h=`<div class="validation-summary"><span class="pill ${v.errors?'err':'ok'}">${v.errors} أخطاء</span><span class="pill ${v.warnings?'warn':'ok'}">${v.warnings} تحذيرات</span>${v.ok?'<span class="pill ok">جاهز للتصدير</span>':''}</div>`;
  h+=v.packages.map(p=>`<div class="validation-package"><strong>${esc(p.name)}</strong>${p.issues.length?p.issues.map(i=>`<div class="issue ${i.level}">${i.level==='error'?'●':'▲'} ${esc(i.message)}</div>`).join(''):'<div class="issue" style="color:var(--ok)">✓ لا توجد مشكلات</div>'}</div>`).join(''); $('#validationBody').innerHTML=h;
}

$('#exportBtn').onclick=async()=>{
  if(dirty){if(!confirm('هناك تغييرات غير محفوظة في الشاشة الحالية. هل تريد حفظها قبل التصدير؟'))return;await savePage();if(dirty)return;}
  loading(true,'جاري فحص المقرر قبل التصدير...');
  try{
    const v=await api('/api/validate');
    if(!v.ok){renderValidation(v);$('#validationDialog').showModal();throw new Error('يوجد خطأ يجب إصلاحه قبل التصدير');}
    // Let the browser stream the potentially very large ZIP directly to disk
    // instead of holding hundreds of megabytes in JavaScript memory.
    const a=document.createElement('a');a.href='/api/export';a.download='';a.style.display='none';document.body.appendChild(a);a.click();a.remove();
    toast('تم بدء إنشاء وتنزيل حزمة SCORM المعدلة');
  }catch(e){toast(e.message,true)}finally{loading(false)}
};


window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue=''}});
