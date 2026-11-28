import React, {useEffect, useState, useRef} from 'react';
import {scopeName} from './RiskScore';
import {createPortal} from 'react-dom';
import {WorkingRisk} from './WorkingRisk';
import {describeEvidence, EventImpact, OperationScore} from './PreferenceEvidence';
import {requestJson} from './connectionRequest';
import {PreferenceAudit} from './PreferenceAudit';
import {OperationLink, OperationNumber} from './OperationReference';
import {englishText, englishJson} from './displayLanguage';

const labels={waiting_for_module:'Operation saved. Waiting for the learning module.',connected:'Learning module connected',processing_pending:'Operations are pending. The profile will refresh after processing.',module_error:'Learning is temporarily unavailable. Your operations are saved.',module_busy:'Learning is still processing. Refresh or retry shortly.'};
const deliveryLabels = {
  applied: 'Processed',
  superseded: 'Superseded by a revision or withdrawal',
  needs_review: 'Needs review',
  pending: 'Pending',
};
const tabs = [['overview','Client overview'],['records','Operation history'],['evidence','Scoring evidence']];
const groups = [
  ['explicit_constraints','Explicit constraints'],
  ['team_declared','Explicit advisor statements'],
  ['tentative_preferences','Tentative preferences'],
  ['client_cues','Client needs and open questions'],
  ['recent_attention','Recent topics'],
  ['unknowns','Unknowns'],
];
const firstPage = {page:1};

function downloadJson(filename,data) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
  const link=document.createElement('a');
  link.href=url;
  link.download=filename;
  link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}

function EvidenceList({items, openEvent, sourceNumbers}) {
  return <ul className="learning-evidence-list">{items.map((item,i)=>{
    const source = item?.source_event_id ?? item?.sources?.at(-1)?.event_id;
    return <li key={item?.signal_id ?? i}>
      {describeEvidence(item)}
      {item?.field && !item?.probabilities && item?.evidence_text && <span> · “{englishText(item.evidence_text)}”</span>}
      {(item?.scope || source) && <small>
        {item.scope && scopeName(item.scope)}
        {item.scope && source && ' · '}
        {source && <OperationLink eventId={source} number={sourceNumbers?.[source]} onOpenEvent={openEvent}/>}
      </small>}
    </li>;
  })}</ul>;
}

export default function CustomerLearning({client,clientName,group,refresh,disabled,scoreHost,onClear}) {
  const [view,setView]=useState(null), [error,setError]=useState(''), [note,setNote]=useState('');
  const [busy,setBusy]=useState(false), [version,setVersion]=useState(0);
  const [navigation,setNavigation]=useState(firstPage);
  const [lookup,setLookup]=useState('');
  const [lookupError,setLookupError]=useState('');
  const [tab,setTab]=useState('overview');
  const tabButtons=useRef({});
  const recordTarget=useRef(null);
  const historyHeading=useRef(null);
  const noteAttempt=useRef(null);
  const lastScore=useRef(null);
  const generation=useRef(0);
  const clearDialog=useRef(null);
  const clearTarget=useRef(null);
  const cancelClear=useRef(null);
  const [clearNotice,setClearNotice]=useState('');
  const base=`/api/portfolio/learning/${encodeURIComponent(client)}`;
  useEffect(()=>{
    if(tab!=='records' || view?.navigation!==navigation || !recordTarget.current) return;
    const eventId=recordTarget.current.eventId ?? view.located_event_id;
    const target=eventId && document.getElementById('learning-event-'+encodeURIComponent(eventId));
    recordTarget.current=null;
    (target ?? historyHeading.current)?.focus();
  },[tab,view,navigation]);
  useEffect(()=>{setView(null);},[base,group,refresh,version]);
  useEffect(()=>{
    const controller=new AbortController();
    let timer, transientRetries=0;
    const query = new URLSearchParams();
    if (navigation.operationNumber) query.set('operation_number',navigation.operationNumber);
    else if (navigation.eventId) query.set('event_id',navigation.eventId);
    else if (navigation.page!==1) query.set('page',navigation.page);
    const params = query.toString();
    const endpoint = params ? `${base}?${params}` : base;
    async function read(attempt) {
      try {
        const next = await requestJson(endpoint,
          {signal:controller.signal,headers:{"X-Portfolio-Analysis":group}});
        if (controller.signal.aborted) return;
        if (next.generation!=null && next.generation!==generation.current) {
          generation.current=next.generation;
          lastScore.current=null;
        }
        if (next.context?.working_risk_index && next.status==='connected')
          lastScore.current={score:next.context.working_risk_index.effective};
        else if(next.last_confirmed_display) lastScore.current=next.last_confirmed_display;
        setError('');
        setView({...next,navigation});
        const pending=next.pending_records || (next.events || []).filter(e=>e.delivery_status==='pending');
        const waits=pending
          .flatMap(e=>e.update?.issues || []).filter(i=>i.retry_at &&
            ['model_busy','model_rate_limited'].includes(i.code)).map(i=>Date.parse(i.retry_at)).filter(Number.isFinite);
        if (waits.length && Math.max(...waits)-Date.now()<=86401000 && transientRetries<2) {
          transientRetries++;
          timer=setTimeout(async()=>{
            try {await requestJson(base+'/retry',{method:'POST',signal:controller.signal});}
            catch { /* The last stored result is still available for the next read. */ }
            if(!controller.signal.aborted) read(attempt+1);
          },Math.max(1500,Math.max(...waits)-Date.now()+500));
          return;
        }
        const processing = next.status === 'module_busy' || (next.status === 'processing_pending' &&
          pending.some(r=>!r.update));
        if (processing && attempt < 16) timer = setTimeout(async()=>{
          try {
            if(next.status==='processing_pending' && pending.some(r=>!r.update)
               && !pending.some(r=>r.update?.issues?.some(i=>i.retry_at)))
              await requestJson(base+'/retry',{method:'POST',signal:controller.signal});
          } catch { /* Read the durable delivery status again after a transient failure. */ }
          if(!controller.signal.aborted) read(attempt+1);
        },2200);
      } catch(e) {
        if (!controller.signal.aborted) {setView(null);setError(e.message);}
      }
    }
    read(0);
    return ()=>{clearTimeout(timer);controller.abort();};
  },[base,group,refresh,version,navigation]);
  async function action(path,body) {
    setBusy(true);setError('');
    try {
      const data=await requestJson(base+path,{method:'POST',body:JSON.stringify(body||{}),timeoutMs:15000});
      if(data.status==='error'||data.learning?.status==='storage_error') throw new Error('The record could not be saved. Please try again.');
      if(path==='/notes'){
        setNote('');
        noteAttempt.current=null;
        recordTarget.current=null;
        setNavigation(firstPage);
        setTab('records');
        requestAnimationFrame(()=>historyHeading.current?.focus());
      }
      setVersion(v=>v+1);
      return true;
    } catch(e) {setError(e.message);return false;} finally {setBusy(false);}
  }
  async function exportRecords() {
    setBusy(true);setError('');
    try {
      const data=await requestJson(base+'/export');
      downloadJson(`portfolio-${client}-operations.json`,data);
    } catch(e) {setError(e.message);} finally {setBusy(false);}
  }
  function askToClear() {
    clearTarget.current=generation.current;
    setError('');
    setClearNotice('');
    clearDialog.current.showModal();
    cancelClear.current.focus();
  }
  async function clearRecords() {
    setBusy(true);
    setError('');
    try {
      const result=await requestJson(base+'/clear',{method:'POST',
        body:JSON.stringify({confirm:true,generation:clearTarget.current}),
        timeoutMessage:'The clear request was not confirmed. Check the current records before trying again.'});
      generation.current=result.generation;
      lastScore.current=null;
      noteAttempt.current=null;
      recordTarget.current=null;
      setNote('');
      setLookup('');
      setLookupError('');
      setView(null);
      setNavigation(firstPage);
      clearDialog.current.close();
      onClear();
      setClearNotice('All learning records and score settings for this client were cleared.');
      setVersion(v=>v+1);
    } catch(e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const rows=view?.events||[];
  const latest=new Map();
  for(const row of rows)if(row.kind==='event')latest.set(row.event_id,row);
  const retracts=new Map(rows.filter(r=>r.kind==='retract').map(r=>[r.event_id,r]));
  const providerIssue = (view?.pending_records || [...latest.values()]
    .filter(row => row.delivery_status === 'pending' && !retracts.has(row.event_id)))
    .flatMap(row => row.update?.issues || [])
    .find(issue => issue.retry_at && ['model_rate_limited', 'model_busy'].includes(issue.code));
  const context=view?.context;
  const sourceNumbers=view?.source_operation_numbers;
  const pagination=view?.pagination;
  const paging=Boolean(view && view.navigation!==navigation);
  const findingSource=Boolean(navigation.eventId || navigation.operationNumber);
  const locating=findingSource && (!view || paging) && !error;
  const missingSource=findingSource && !paging && view?.located_event===false;
  const pendingCount=view?.pending_count;
  const reviewCount=view?.review_count || 0;
  const populatedGroups=context ? groups.filter(([key])=>context[key]?.length) : [];
  function openEvent(event,eventId) {
    event.preventDefault();
    recordTarget.current={eventId};
    if(paging || !latest.has(eventId)) setNavigation({eventId});
    setTab('records');
  }
  function findOperation(event) {
    event.preventDefault();
    const digits=/^(?:UNI-)?([0-9]{1,19})$/i.exec(lookup.trim())?.[1].replace(/^0+/,'');
    if(!digits) {
      setLookupError('Enter an operation ID, such as UNI-000123 or 123.');
      return;
    }
    const operationNumber='UNI-'+digits.padStart(6,'0');
    setLookup(operationNumber);
    setLookupError('');
    setError('');
    recordTarget.current={};
    setNavigation({operationNumber});
  }
  function navigateTabs(event,index) {
    const last=tabs.length-1;
    const previous=(index+last)%tabs.length;
    const next=(index+1)%tabs.length;
    const destination={ArrowLeft:previous,ArrowRight:next,Home:0,End:last}[event.key];
    if(destination===undefined) return;
    event.preventDefault();
    const [key]=tabs[destination];
    setTab(key);
    tabButtons.current[key]?.focus();
  }
  function saveNote(event) {
    event.preventDefault();
    if(!note.trim() || busy || disabled) return;
    const key=JSON.stringify([note,group]);
    if(noteAttempt.current?.key!==key)noteAttempt.current={key,id:crypto.randomUUID()};
    action('/notes',{message:note,client_id:client,
      observation:{event_id:noteAttempt.current.id,evidence_group_id:group,text_source:'manual'}});
  }
  function changePage(nextPage) {
    recordTarget.current=null;
    setNavigation({page:nextPage});
    historyHeading.current?.scrollIntoView({block:'start'});
  }
  return <section className="customer-learning" aria-label="Client preference learning">
    {scoreHost && createPortal(<WorkingRisk key={generation.current} generation={generation.current} context={context} lastConfirmed={lastScore.current} status={view?.status} onSave={action} disabled={disabled||busy}
      error={error} onRefresh={()=>setVersion(v=>v+1)}
      latest={pagination?view.latest_event:[...latest.values()].filter(e=>!retracts.has(e.event_id)).at(-1)}/>, scoreHost)}
    <header className="learning-header">
      <div>
        <h3>Client preference learning</h3>
        <p>Selected client <strong>{clientName||client}</strong></p>
      </div>
      <div className="learning-status" role="status">
        <span className={'learning-badge '+(view?.status==='connected'?'is-ready':'')}>
          {view ? labels[view.status]||view.status : error?'Could not load client records. Please retry.':'Loading client records…'}
        </span>
        <span>Pending <b>{pendingCount??'—'}</b>{reviewCount>0 && <> · Needs review <b>{reviewCount}</b></>}</span>
      </div>
    </header>
    {providerIssue && <p className="learning-notice" role="status">
      {providerIssue.code === 'model_rate_limited' ? 'Gemini reached its request limit.' : 'Gemini is temporarily busy.'}
      Your records are saved. You can retry after {new Date(providerIssue.retry_at).toLocaleTimeString('en-US')}. This page will also retry when the service is available.
    </p>}
    {clearNotice&&<p className="learning-notice" role="status">{clearNotice}</p>}
    {error&&<p className="learning-notice is-warning" role="alert">{error}</p>}
    <div className="learning-tabs" role="tablist" aria-label="Preference learning sections">
      {tabs.map(([key,title],i)=><button type="button" key={key} ref={button=>{tabButtons.current[key]=button;}}
        role="tab" id={'learning-tab-'+key} aria-controls={'learning-panel-'+key}
        aria-selected={tab===key} tabIndex={tab===key?0:-1} onClick={()=>setTab(key)} onKeyDown={e=>navigateTabs(e,i)}>
        {title}{key==='records' && view?.total_events>0 && <small>{view.total_events}</small>}
      </button>)}
    </div>
    <section className="learning-tab-body learning-overview" role="tabpanel" id="learning-panel-overview"
      aria-labelledby="learning-tab-overview" hidden={tab!=='overview'}>
      <div>
        <h4>Client context for future analyses</h4>
        {context?.conflicts?.length>0 && <p className="learning-notice is-warning" role="status">Items to review: {context.conflicts.length}. Open Scoring evidence to check the sources.</p>}
        {context ? <>
          <p>This information helps interpret the analysis for this client. Calculations and questions record ideas explored. Confirm any decision or execution separately.</p>
          <div className="learning-groups">
            {populatedGroups.map(([key,title])=><details key={key}
              open={['explicit_constraints','team_declared','tentative_preferences'].includes(key) || undefined}>
              <summary>{title} <small>{context[key].length}</small></summary>
              <EvidenceList items={context[key]} openEvent={openEvent} sourceNumbers={sourceNumbers}/>
            </details>)}
          </div>
          {!populatedGroups.some(([key])=>key!=='unknowns') && <p>No client cues yet. Run a calculation or add a client note.</p>}
        </> : <p>Client context will appear here after the learning module processes the records.</p>}
      </div>
      <form className="learning-note" onSubmit={saveNote}>
        <label htmlFor="learning-note-text">Add a client note</label>
        <p id="learning-note-hint">Record a client statement, a temporary need, an assumption, or a correction. After saving, open Operation history to see its interpretation and score impact.</p>
        <textarea id="learning-note-text" aria-describedby="learning-note-hint" value={note} maxLength={3000} disabled={busy||disabled}
          onChange={e=>setNote(e.target.value)} placeholder="Example: The client needs these funds next year and wants less volatility."/>
        <button className="learning-primary" type="submit" disabled={busy||disabled||!note.trim()}>Save client note</button>
      </form>
    </section>
    <section className="learning-tab-body" role="tabpanel" id="learning-panel-records"
      aria-labelledby="learning-tab-records" hidden={tab!=='records'}>
      <div className="learning-record-head">
        <div>
          <h4 tabIndex={-1} ref={historyHeading}>Recent operations and learning results</h4>
          <p>Each record shows the before-and-after score under the current rules.</p>
        </div>
        <div className="learning-actions">
          <button disabled={busy||disabled} onClick={()=>action('/retry')}>Retry pending records</button>
          <button disabled={busy} onClick={exportRecords}>Export client records</button>
        </div>
      </div>
      <form className="learning-record-search" onSubmit={findOperation}>
        <label htmlFor="learning-operation-lookup">Find by operation ID</label>
        <input id="learning-operation-lookup" type="text" value={lookup} placeholder="UNI-000123" autoComplete="off"
          aria-invalid={Boolean(lookupError)} aria-describedby={lookupError ? 'learning-operation-error' : undefined}
          onChange={e=>{setLookup(e.target.value);setLookupError('');}}/>
        <button type="submit" disabled={!lookup.trim() || locating}>Find</button>
        {lookupError && <p id="learning-operation-error" role="alert">{lookupError}</p>}
      </form>
      <dialog ref={clearDialog} className="connection-dialog" aria-labelledby="learning-clear-title"
        aria-describedby="learning-clear-details" onCancel={e=>{if(busy)e.preventDefault();}}>
        <div className="connection-content">
          <h3 id="learning-clear-title">Clear all learning records for this client?</h3>
          <p id="learning-clear-details"><strong>{clientName||client} ({client})</strong>. This clears calculations, questions, notes, pending records, learned preferences, and manual score settings. The household score resets to 50. This action is permanent.</p>
          {error&&<p role="alert">{error}</p>}
          <div className="connection-footer">
            <button type="button" ref={cancelClear} disabled={busy} onClick={()=>clearDialog.current.close()}>Cancel</button>
            <button type="button" className="learning-danger" disabled={busy} onClick={clearRecords}>{busy?'Clearing…':'Confirm clear'}</button>
          </div>
        </div>
      </dialog>
      {locating && <p role="status">Finding the original record…</p>}
      {missingSource && <p className="learning-notice" role="status">{navigation.operationNumber
        ? `No record for this client matches ${navigation.operationNumber}. Check the selected client and operation ID.`
        : 'This source could not be found in the selected client’s records.'}</p>}
      {!paging && view && !latest.size && <p>No operations yet. Run a calculation or add a client note.</p>}
      {!locating && [...latest.values()].reverse().map(row=><article key={row.event_id} id={'learning-event-'+encodeURIComponent(row.event_id)}
        className="learning-event" tabIndex={-1}>
        <header>
          <strong>{englishText(row.event.user_text)||({allocation_drift:'Allocation drift calculation',risk:'Risk and return calculation',projection:'Goal projection',holdings:'Holdings lookup',quote:'External quote lookup',history:'Price history lookup',client_statement:'Client note',advisor_request:'Agent question'}[row.event.task]||row.event.task)}</strong>
          <div className="learning-event-meta">
            <OperationNumber number={row.operation_number}/>
            <time dateTime={row.event.occurred_at}>{new Date(row.event.occurred_at).toLocaleString('en-US')}</time>
            <span className={'learning-badge '+(row.delivery_status==='applied'?'is-ready':'')}>{deliveryLabels[row.delivery_status] || deliveryLabels.pending}</span>
            {retracts.has(row.event_id) && <span className="learning-badge">{retracts.get(row.event_id).delivery_status==='applied'?'Withdrawn':'Withdrawal pending'}</span>}
          </div>
        </header>
        <OperationScore row={row}/>
        {retracts.has(row.event_id) && <OperationScore row={retracts.get(row.event_id)}/>}
        <EventImpact row={row} withdrawn={retracts.has(row.event_id)}
          currentIndex={context?.scoped_risk_index?.scoring_event_ids?.includes(row.event_id)
            ? context.scoped_risk_index : context?.working_risk_index}/>
        <details><summary>View inputs and learning details</summary><pre>{englishJson({operation_number:row.operation_number,
          input:row.event,learning_result:row.update})}</pre></details>
        <div className="learning-actions learning-event-actions">
          {row.event.outcome==='success' && ['advisor_note','direct','agent'].includes(row.event.entry_point)
            && row.delivery_status!=='pending' && !retracts.has(row.event_id) &&
            <button disabled={busy||disabled} onClick={()=>action('/reinterpret/'+encodeURIComponent(row.event_id),{revision:row.revision})}>Reinterpret this record</button>}
          <button disabled={busy||disabled||retracts.has(row.event_id)} onClick={()=>action(`/retract/${encodeURIComponent(row.event_id)}`)}>Withdraw this record</button>
        </div>
      </article>)}
      {pagination && pagination.pages>1 && <nav className="learning-pagination" aria-label="Operation history pagination">
        <button type="button" disabled={paging||pagination.page===1} onClick={()=>changePage(pagination.page-1)}>Previous</button>
        <span role="status">{paging?'Loading records…':`Page ${pagination.page} / ${pagination.pages} · ${view.total_events} operations`}</span>
        <button type="button" disabled={paging||pagination.page===pagination.pages} onClick={()=>changePage(pagination.page+1)}>Next</button>
      </nav>}
      <div className="learning-record-footer">
        <button className="learning-danger" disabled={busy||disabled||!view} onClick={askToClear}>Clear all records</button>
      </div>
    </section>
    <section className="learning-tab-body" role="tabpanel" id="learning-panel-evidence"
      aria-labelledby="learning-tab-evidence" hidden={tab!=='evidence'}>
      <PreferenceAudit context={context} sources={view?.conflict_sources} sourceNumbers={sourceNumbers} onOpenEvent={openEvent}
        onDownload={()=>downloadJson(`portfolio-${client}-evidence.json`,{context, conflict_sources:view?.conflict_sources,
          operation_numbers:sourceNumbers})}/>
    </section>
  </section>;
}
