'use strict';
// Only the respondent's bearer capability is read from their link. No server
// credentials, external libraries, analytics, or persistent browser answer cache.
const token = new URL(location.href).searchParams.get('token');
const supportEmail = document.querySelector('meta[name="survey-support-email"]')?.content || '';
const $ = id => document.getElementById(id);
let state, step = 0, q1, candidateEdited = false, draft = '', dirty = false, timer, pending = null, busy = false, conflict = false;
let inFlight = null, finishing = false, finishLocked = false;
const question = id => state.questions.find(q => q.id === id);
const value = id => state.answers[id]?.value;
const accepted = id => ['answered','skipped'].includes(state.answers[id]?.status);
function requestId() {
  if(typeof crypto.randomUUID==='function') return crypto.randomUUID();
  const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);
  bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  return Array.from(bytes,byte=>byte.toString(16).padStart(2,'0')).join('');
}
function message(text) { $('save-state').textContent = text; }
function savedMessage() {
  message(state.state!=='completed' && !dirty && step===2 && accepted('q4') ? 'Comment saved. Tap Finish to complete.' : '');
}
function finishLock(on) {
  finishLocked=on;
  clearTimeout(timer);
  const text=$('comment'); if(text) text.readOnly=on;
  $('finish').textContent=on?'Finishing…':'Finish survey';
  $('finish').disabled=on;
}
function focusRecovery() {
  const action=!$('retry').hidden?$('retry'):!$('reload').hidden?$('reload'):$('error');
  action.tabIndex=0; action.focus(); $('error').scrollIntoView({block:'nearest'});
}
function clearError() {
  $('error').hidden=true;$('retry').hidden=true;$('reload').hidden=true;conflict=false;
  for(const el of $('question').querySelectorAll('[aria-invalid]')) {
    el.removeAttribute('aria-invalid');
    el.setAttribute('aria-describedby','save-state');
  }
}
function error(text, kind='unavailable') {
  $('error').textContent = text; $('error').hidden = false;
  $('retry').hidden = kind !== 'unavailable'; $('reload').hidden = kind !== 'conflict';
  conflict = kind === 'conflict'; message(kind==='unavailable'?'Save not confirmed':'Check your answers');
  for (const el of $('question').querySelectorAll('button,input,textarea')) {
    el.setAttribute('aria-describedby','error'); el.setAttribute('aria-invalid','true');
  }
}
async function request(path, options={}) {
  const response = await fetch(path, {signal:AbortSignal.timeout(15000),...options, cache:'no-store'});
  let data; try { data = await response.json(); } catch { throw {message:'We couldn’t confirm the save. Please retry.',error:'unavailable'}; }
  if (!response.ok) throw data;
  return data;
}
function lock(on) {
  busy = on;
  for (const el of $('question').querySelectorAll('button,input')) el.disabled = on || Boolean(pending) || conflict || finishLocked;
  for(const el of document.querySelectorAll('nav button')) el.disabled=finishLocked||(!on&&Boolean(pending))||conflict;
  $('retry').disabled=on; $('reload').disabled=on;
  // Text typed during an in-flight save stays a separate unsaved draft.
}
async function load(initial=false) {
  try {
    const next = await request('/survey/state?token='+encodeURIComponent(token || ''));
    state = next; q1 = value('q1') ?? state.q1_candidate; candidateEdited = false; draft = value('q4') ?? '';
    dirty = false; pending = null; finishLock(false); clearError();
    document.querySelector('.progress').hidden=false; $('save-state').hidden=false;
    if (!accepted('q1') || !accepted('q2')) step=0;
    else if (!accepted('q3')) step=1; else step=2;
    render(!initial); savedMessage();
  } catch(e) {
    $('question').replaceChildren(make('h1',e.error==='link'?'This survey link is no longer available.':'Couldn’t load your survey',{id:'heading',tabindex:'-1'}));
    for(const id of ['back','next','finish']) $(id).hidden=true;
    document.querySelector('.progress').hidden=true; $('save-state').hidden=true; clearError();
    if(e.error==='link') $('question').append(supportGuidance());
    else { error('We couldn’t load your survey. Try again.'); $('retry').textContent='Try again'; }

  }
}
function make(tag, text, attrs={}) {
  const el = document.createElement(tag); if(text!==null) el.textContent=text;
  for(const [key,val] of Object.entries(attrs)) el.setAttribute(key,val); return el;
}
function supportGuidance() {
  const line=make('p',null);line.append(document.createTextNode('Try reopening the original email. If it still doesn’t work, contact '));
  line.append(supportEmail?make('a','Send It support',{href:'mailto:'+supportEmail}):document.createTextNode('Send It support'));
  line.append(document.createTextNode('.'));return line;
}
function render(focus=true) {
  const root=$('question'); root.replaceChildren();
  for(const id of ['back','next','finish']) {$(id).hidden=true;$(id).disabled=false;}
  if(state.read_only || state.state==='completed') {
    root.append(make('p','✓',{class:'thanks','aria-hidden':'true'}),make('h1','Thanks for sharing.',{id:'heading',tabindex:'-1'}),make('p','Your feedback helps make the next visit better.'));
    $('progress-label').textContent='Complete'; $('progress').value=4; clearError(); message('');
    if(focus) $('heading').focus(); return;
  }
  $('progress-label').textContent=step===0?'Questions 1–2 of 4':`Question ${step+2} of 4`;
  $('progress').value=Math.min(3.5,state.questions.filter(q=>state.applicability[q.id] && accepted(q.id)).length);
  $('progress').max=state.questions.filter(q=>state.applicability[q.id]).length;
  if(step===0) {
    root.append(make('div',null,{id:'candidate',class:'candidate'})); ratingSummary(accepted('q1'));
    const field=make('fieldset',null); field.append(make('legend',question('q1').wording,{class:'sr-only'}));
    const row=make('div',null,{class:'ratings'});
    for(let n=1;n<=5;n++) {
      const label=make('label',null,{class:'rating'}), input=make('input',null,{type:'radio',name:'q1',value:n,'aria-label':`${n} out of 5${n===1?' — Awful':n===5?' — Exceptional':''}`});
      input.checked=n===q1; label.append(input,document.createTextNode(String(n))); row.append(label);
      input.addEventListener('change',()=>{q1=n; candidateEdited=true; ratingSummary(accepted('q1'));
        if(accepted('q1')&&accepted('q2')&&!pending&&!conflict) save({kind:'answer',expected_revision:state.revision,answers:{q1:{status:'answered',value:n}}});
        else message('');});
    }
    field.append(row); const ends=make('div',null,{class:'endpoints'}); ends.append(make('span','1 · Awful'),make('span','5 · Exceptional')); field.append(ends);root.append(field);
    root.append(make('h1',question('q2').wording,{id:'heading',tabindex:'-1'}));
    choices('q2', async selected => {
      if(!q1) return error('Choose your overall rating first.','validation');
      await save({kind:'confirm_q1_q2',expected_revision:state.revision,q2:selected,edited_q1:q1},()=>{step=1;render();});
    });
    if(accepted('q1')&&accepted('q2')) $('next').hidden=false;
  } else if(step===1) {
    root.append(make('h1',question('q3').wording,{id:'heading',tabindex:'-1'}));
    choices('q3',selected=>save({kind:'answer',expected_revision:state.revision,answers:{q3:{status:'answered',value:selected}}},()=>{step=2;render();}));
    $('back').hidden=false; $('next').hidden=!accepted('q3');
  } else {
    root.append(make('h1',question('q4').wording,{id:'heading',tabindex:'-1'}),make('label','Optional',{for:'comment'}));
    const text=make('textarea',null,{id:'comment',maxlength:question('q4').max_length,placeholder:'Tell us in your own words…','aria-describedby':'save-state'}); text.value=draft; text.readOnly=finishLocked; root.append(text);
    text.addEventListener('input',()=>{if(finishLocked){text.value=draft;return;}draft=text.value;dirty=true;message('');clearTimeout(timer);timer=setTimeout(()=>{if(!pending&&!conflict&&!finishLocked) flushText();},700);});
    $('back').hidden=false; $('finish').hidden=false;
  }
  if(focus) $('heading').focus();
  savedMessage();
}
function ratingSummary(saved) {
  const summary=$('candidate'); summary.replaceChildren();
  if(!q1) return summary.append(make('strong','Choose your rating.'));
  const label=saved?'Your saved rating':candidateEdited?'Your rating':'Your rating from the email';
  summary.append(make('strong',`${label}: ${q1}/5`),make('span','You can change it.'));
}
function choices(id, action) {
  const field=make('fieldset',null,{'aria-labelledby':'heading'});
  const group=make('div',null,{class:`choices choices-${id}`});
  for(const [option,label] of question(id).options) {
    const button=make('button',label,{type:'button','aria-label':label,'aria-pressed':String(value(id)===option)});
    button.addEventListener('click',()=>{if(!pending&&!busy&&!conflict&&!finishLocked) {
      for(const choice of group.children) {choice.removeAttribute('data-pending');choice.setAttribute('aria-pressed','false');}
      button.setAttribute('data-pending','true');button.setAttribute('aria-pressed','true');action(option);
    }});group.append(button);
  } field.append(group);$('question').append(field);
}
async function save(command, after=()=>{}) {
  if(busy || pending) return false;
  pending={body:{request_id:requestId(),command},after}; return runPending();
}
async function runPending() {
  const promise=attempt();inFlight=promise;
  try{return await promise;}finally{if(inFlight===promise)inFlight=null;}
}
async function attempt() {
  if(busy || !pending) return false;
  const operation=pending; clearError(); lock(true);message('Saving…');
  try {
    const receipt=await request('/survey/answer',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify(operation.body)});
    state.revision=receipt.revision;state.state=receipt.state;
    const cmd=operation.body.command;
    if(cmd.kind==='confirm_q1_q2') {state.answers.q1={status:'answered',value:cmd.edited_q1};state.answers.q2={status:'answered',value:cmd.q2};}
    if(cmd.kind==='answer') Object.assign(state.answers,cmd.answers);
    pending=null; operation.after();
    if(cmd.kind==='complete') clearTimeout(timer);
    savedMessage(); return true;
  } catch(e) {
    const kind=['validation','conflict','link','version'].includes(e.error)?e.error:'unavailable';
    if(kind==='validation')pending=null;
    const copy=kind==='conflict' ? 'Your survey changed in another tab. Copy any unsaved comment before choosing Load saved answers: it will replace your comment with the saved version.' :
      kind==='link' ? 'This survey link is no longer available. Reopen the original email or contact Send It support.' :
      kind==='validation' ? 'Check your answers before continuing.' :
      kind==='version' ? 'This survey is not available here.' :
      `We couldn’t confirm your ${operation.body.command.kind==='complete'?'completion': 'save'}. ${operation.body.command.kind==='complete'?'Try again to check completion.':(finishLocked?'Your comment':'Your answer')+' is still here. Try again.'}`;
    error(copy,kind);return false;
  }
  finally {lock(false);if(finishLocked&&!$('error').hidden)focusRecovery();}
}
async function flushText() {
  clearTimeout(timer);
  if(busy || pending || conflict) return false;
  if(!dirty) return true;
  const text=draft;
  const ok=await save({kind:'answer',expected_revision:state.revision,answers:{q4:{status:'answered',value:text}}},()=>{dirty=draft!==text;});
  // Serialize text revisions; never discard keystrokes typed during the request.
  if(ok && dirty) return flushText(); return ok;
}
async function navigate(next) {
  if(inFlight) await inFlight;
  if(pending||conflict||finishLocked) return;
  if(dirty && !await flushText()) return;
  step=next;clearError();render();
}
$('back').onclick=()=>navigate(step-1);
$('next').onclick=()=>navigate(step+1);
$('finish').onclick=async()=>{
  if(finishing||finishLocked)return;finishing=true;finishLock(true);
  try {
  if(inFlight) await inFlight;
  if(pending||conflict) return;
  if(!await flushText()) return;
  if(!accepted('q4')) {
    if(!await save({kind:'answer',expected_revision:state.revision,answers:{q4:{status:'skipped'}}})) return;
  }
  await save({kind:'complete',expected_revision:state.revision},()=>{state.read_only=true;render();});
  } finally {finishing=false;if(!pending && state.state!=='completed'){finishLock(false);lock(false);}}
};
$('retry').onclick=async()=>{
  if(pending){
    const recoveringFinish=finishLocked;
    const ok=await runPending();
    if(ok&&dirty) await flushText();
    if(ok && recoveringFinish && !pending && state.state!=='completed') {
      finishLock(false); lock(false); savedMessage(); $('finish').focus();
    }
  }else await load();
};
$('reload').onclick=()=>{if(!dirty||confirm('Load saved answers? Your unsaved comment will be replaced.')){clearTimeout(timer);load();}};
window.addEventListener('beforeunload',event=>{if(dirty||pending||busy){event.preventDefault();event.returnValue='';}});
load(true); // GET only. No startup/focus/visibility/timer mutation without typing.
