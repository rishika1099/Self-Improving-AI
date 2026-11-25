import React, {useEffect, useState, useRef} from 'react';
import {RiskScore} from './RiskScore';
import {createPortal} from 'react-dom';
import {WorkingRisk} from './WorkingRisk';
import {describeEvidence, EventImpact} from './PreferenceEvidence';
import {requestJson} from './connectionRequest';

const labels={waiting_for_module:'操作已记录，等待学习模块',connected:'学习模块已连接',processing_pending:'有操作等待处理，暂不使用旧画像',module_error:'学习模块暂不可用，操作仍保留',module_busy:'学习模块仍在处理，请稍后刷新或重试'};
const deliveryLabels = {
  applied: '模块已处理',
  superseded: '已由新修订或撤回替代',
  needs_review: '需人工确认',
  pending: '等待处理',
};
export default function CustomerLearning({client,clientName,group,refresh,disabled,scoreHost}) {
  const [view,setView]=useState(null), [error,setError]=useState(''), [note,setNote]=useState('');
  const [busy,setBusy]=useState(false), [version,setVersion]=useState(0);
  const noteAttempt=useRef(null);
  const base=`/api/portfolio/learning/${encodeURIComponent(client)}`;
  useEffect(()=>{
    const controller=new AbortController();
    setView(null);
    let timer, transientRetries=0;
    async function read(attempt) {
      try {
        const next = await requestJson(base,{signal:controller.signal,headers:{"X-Portfolio-Analysis":group}});
        if (controller.signal.aborted) return;
        setView(next);
        const waits=(next.events || []).filter(e=>e.delivery_status==='pending')
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
          next.events?.some(r=>r.delivery_status==='pending' && !r.update));
        if (processing && attempt < 16) timer = setTimeout(async()=>{
          try {
            if(next.status==='processing_pending' && next.events?.some(r=>r.delivery_status==='pending' && !r.update)
               && !next.events?.some(r=>r.delivery_status==='pending' && r.update?.issues?.some(i=>i.retry_at)))
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
  },[base,group,refresh,version]);
  async function action(path,body) {
    setBusy(true);setError('');
    try {
      const data=await requestJson(base+path,{method:'POST',body:JSON.stringify(body||{}),timeoutMs:15000});
      if(data.status==='error'||data.learning?.status==='storage_error') throw new Error('记录保存失败，请重试。');
      if(path==='/notes'){setNote('');noteAttempt.current=null;}
      setVersion(v=>v+1);
      return true;
    } catch(e) {setError(e.message);return false;} finally {setBusy(false);}
  }
  async function exportRecords() {
    setBusy(true);setError('');
    try {
      const data=await requestJson(base+'/export');
      const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
      const a=document.createElement('a');a.href=url;a.download=`portfolio-${client}-operations.json`;a.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    } catch(e) {setError(e.message);} finally {setBusy(false);}
  }
  const rows=view?.events||[];
  const latest=new Map();
  for(const row of rows)if(row.kind==='event')latest.set(row.event_id,row);
  const retracts=new Map(rows.filter(r=>r.kind==='retract').map(r=>[r.event_id,r]));
  const providerIssue = [...latest.values()]
    .filter(row => row.delivery_status === 'pending' && !retracts.has(row.event_id))
    .flatMap(row => row.update?.issues || [])
    .find(issue => issue.retry_at && ['model_rate_limited', 'model_busy'].includes(issue.code));
  const context=view?.context;
  return <section className="customer-learning" aria-label="客户偏好学习">
    {scoreHost && createPortal(<WorkingRisk context={context} status={view?.status} onSave={action} disabled={disabled||busy}
      error={error} onRefresh={()=>setVersion(v=>v+1)}
      latest={[...latest.values()].filter(e=>!retracts.has(e.event_id)).at(-1)}/>, scoreHost)}
    <h3>客户偏好学习</h3>
    <p role="status">{view ? labels[view.status]||view.status : error?'客户记录读取失败，请重试':'正在读取客户记录…'}</p>
    {providerIssue && <p role="status">
      {providerIssue.code === 'model_rate_limited' ? 'Gemini 请求已达限额。' : 'Gemini 暂时繁忙。'}
      记录已保存，可在 {new Date(providerIssue.retry_at).toLocaleTimeString()} 后重试。页面会在恢复窗口内尝试重新处理。
    </p>}
    <p>当前客户：{clientName||client} · 待处理记录：{view?.pending_count??'—'}{view?.review_count ? ` · 需确认记录：${view.review_count}` : ''}。记录操作不代表客户已接受或执行方案。</p>
    <RiskScore scores={context?.risk_source_scores?.operations || context?.risk_preference_scores} visibleEvents={latest}/>
    {context?.risk_source_scores?.notes?.some(c=>c.evidence.length>0 || c.status==='conflict') &&
      <details><summary>查看补充说明的独立评分依据</summary>
        <RiskScore scores={context.risk_source_scores.notes} visibleEvents={latest} title="补充说明分"/></details>}
    <label>补充客户说明<textarea aria-label="补充客户说明" value={note} maxLength={3000} disabled={busy||disabled} onChange={e=>setNote(e.target.value)} placeholder="例如：该客户整体比较保守。也可以记录假设或纠正，含义由学习模块判断。"/></label>
    <div className="learning-actions">
      <button disabled={busy||disabled||!note.trim()} onClick={()=>{const key=JSON.stringify([note,group]);if(noteAttempt.current?.key!==key)noteAttempt.current={key,id:crypto.randomUUID()};action('/notes',{message:note,client_id:client,observation:{event_id:noteAttempt.current.id,evidence_group_id:group,text_source:'manual'}});}}>保存客户说明</button>
      <button disabled={busy||disabled} onClick={()=>action('/retry')}>重试待处理记录</button>
      <button disabled={busy} onClick={exportRecords}>导出当前客户记录</button>
    </div>
    {error&&<p role="alert">{error}</p>}
    {context ? <><h4>供后续分析使用的客户信息</h4><p>{context.summary||'学习模块已返回客户信息。'}</p><p>暂定支持程度不等于已校准的风险评分。</p>{[['team_declared','团队明确说明'],['tentative_preferences','暂定倾向'],['client_cues','客户需求与待核实线索'],['explicit_constraints','明确约束'],['recent_attention','近期关注'],['unknowns','尚不明确'],['usage_notes','本次使用说明']].map(([key,title])=><div key={key}><strong>{title}</strong>{Array.isArray(context[key])&&context[key].length?<ul>{context[key].map((x,i)=><li key={i}>{describeEvidence(x)}{x?.source_event_id?`（依据 ${x.source_event_id}）`:''}</li>)}</ul>:<p>暂无返回记录</p>}</div>)}<details><summary>查看完整客户信息和来源</summary><pre>{JSON.stringify(context,null,2)}</pre></details></>:
      <p>暂时没有可供后续分析使用的客户信息。模块接入并处理记录后，这里会显示返回的信息。</p>}
    <h4>最近操作与学习结果</h4>
    {view&&!latest.size&&<p>还没有操作。可以先运行一次计算，或补充一条客户说明。</p>}
    {[...latest.values()].reverse().map(row=><article key={row.event_id} id={'learning-event-'+encodeURIComponent(row.event_id)} className="learning-event">
      <strong>{row.event.user_text||({allocation_drift:'配置偏离试算',risk:'风险指标计算',projection:'目标预测',holdings:'持仓查询',quote:'外部报价查询',history:'历史价格查询'}[row.event.task]||row.event.task)}</strong>
      <p>{new Date(row.event.occurred_at).toLocaleString()} · {deliveryLabels[row.delivery_status] || deliveryLabels.pending}{retracts.has(row.event_id)?` · ${retracts.get(row.event_id).delivery_status==='applied'?'已撤回':'撤回待处理'}`:''}</p>
      <EventImpact row={row} withdrawn={retracts.has(row.event_id)}/>
      <details><summary>查看输入来源和变化依据</summary><pre>{JSON.stringify({input:row.event,learning_result:row.update},null,2)}</pre></details>
      {row.event.outcome==='success' && ['advisor_note','direct','agent'].includes(row.event.entry_point)
        && row.delivery_status!=='pending' && !retracts.has(row.event_id) &&
        <button disabled={busy||disabled} onClick={()=>action('/reinterpret/'+encodeURIComponent(row.event_id),{revision:row.revision})}>重新解读这条记录</button>}
      <button disabled={busy||disabled||retracts.has(row.event_id)} onClick={()=>action(`/retract/${encodeURIComponent(row.event_id)}`)}>撤回这条记录</button>
    </article>)}
    {view?.total_events>100&&<p>这里只显示最近 100 次操作及其撤回状态，导出包含完整修订历史。</p>}
  </section>;
}
