import React, {useEffect, useRef, useState} from 'react';
import {englishText} from './displayLanguage';

const show = score => Number.isFinite(score) ? score.toFixed(1) : '—';
const signed = score => (score>0?'+':'')+show(score);
const intensity = {mild:'mild',moderate:'explicit',strong:'strong'};

function ScoreScale({score}) {
  return <div className="risk-scale">
    {Number.isFinite(score) && <meter aria-label="Applied score" min="0" max="100" value={score}/>}
    <div className="risk-scale-track" aria-hidden="true">
      {Number.isFinite(score) && <span className="risk-scale-marker" style={{left:`${score}%`}}/>}
    </div>
    <div className="risk-scale-labels"><span><b>0</b> Lower risk</span><span><b>50</b> Neutral</span><span><b>100</b> Higher risk</span></div>
  </div>;
}

function ScoreBreakdown({index}) {
  const detail=index.breakdown;
  const components=detail?[
    ['Long-term baseline', detail.anchor, show(detail.anchor)],
    ['Allocation trials', detail.trial_points, signed(detail.trial_points)],
    ['Directional actions', detail.behavior_points, signed(detail.behavior_points)],
    ['Recent attitude', detail.current_points, signed(detail.current_points)],
  ]:[];
  return <div className="risk-breakdown">
    <div className="risk-breakdown-heading"><h4>Score breakdown</h4>
      <div className="risk-total">System score <strong>{show(index.reference)}</strong></div>
    </div>
    {detail?<dl className="risk-components" aria-label="Four components of the system score">{components.map(([name,score,value],i)=><div className="risk-component" key={name}>
      <dt>{name}</dt><dd className={i===0?'risk-base':score>0?'risk-increase':score<0?'risk-decrease':'risk-neutral'}>{value}</dd>
    </div>)}</dl>:<p className="risk-muted">The score breakdown is temporarily unavailable.</p>}
    <div className="risk-breakdown-foot">
      <p>Score from routine operations <b>{show(index.automatic)}</b><span aria-hidden="true"> · </span>{index.groups} evidence groups</p>
      {detail&&<details className="risk-evidence"><summary>View score drivers</summary>
        <dl>
          <div><dt>Long-term baseline</dt><dd>{index.anchor_source?
            <>“{englishText(index.anchor_source)}”{index.anchor_intensity&&` · ${intensity[index.anchor_intensity]} statement`}</>:'Starts at a neutral baseline of 50.'}</dd></div>
          <div><dt>Recent attitude</dt><dd>{detail.current_source?
            <>“{englishText(detail.current_source)}” · Applies for {detail.current_days} calendar days</>:'No recent attitude currently applies.'}</dd></div>
          <div><dt>Routine operations score</dt><dd>Recomputed from routine calculations and chat.</dd></div>
          {detail.work_timezone&&<div><dt>Net impact today</dt><dd>Allocation trials: {signed(detail.today_trial)} points (daily cap ±{detail.daily_trial_limit}). Directional actions: {signed(detail.today_behavior)} points (daily cap ±{detail.daily_behavior_limit}).</dd></div>}
        </dl>
        {index.mixed_trials&&<p>This round contains opposing exploratory signals. The group was treated as neutral.</p>}
        {index.note_legacy&&<p>You can reinterpret older notes from their original records.</p>}
      </details>}
    </div>
  </div>;
}

export function WorkingRisk({context, lastConfirmed, status, disabled, onSave, latest, error, onRefresh, generation}) {
  const [editing,setEditing]=useState(false);
  const [draft,setDraft]=useState(50);
  const [basis,setBasis]=useState(null);
  const [draftLocked,setDraftLocked]=useState(false);
  const slider=useRef(null);
  const index=context?.working_risk_index;
  const seen=useRef(null);
  const [movement,setMovement]=useState(null);
  useEffect(()=>{
    if(!index)return;
    if(seen.current && index.effective!==seen.current.value)
      setMovement({before:seen.current.value,after:index.effective});
    seen.current={value:index.effective};
  },[index]);
  useEffect(()=>{if(editing)slider.current?.focus();},[editing]);
  if (!index) return <section className="working-risk risk-unavailable" aria-label="Client risk willingness"><h3>Client risk willingness</h3>
    {Number.isFinite(lastConfirmed?.score) && <p><strong>{show(lastConfirmed.score)}</strong> / 100  Last confirmed score · Updating</p>}
    <p>{error || (['module_busy','processing_pending'].includes(status)?'Client operations are being processed. The score will update when they are ready.':
      status?'A verified score is currently unavailable.':'Loading client score…')}</p></section>;
  const adjusted=index.calibration.chosen!=null;
  const effective=index.effective;
  const applied=context.scoped_risk_index || index;
  const displayedScore=Number(show(applied.effective));
  const fixed=adjusted && index.calibration.mode==='fixed';
  function edit() {
    setDraft(Math.round(effective ?? 50));
    setDraftLocked(fixed);
    setBasis({revision:index.calibration.revision,profile_version:context.profile_version,generation});
    setEditing(true);
  }
  async function save(score) {
    const saved=await onSave('/risk-calibration',{score,mode:draftLocked?'fixed':'adaptive',...basis});
    setEditing(false);
    if(!saved) onRefresh();
  }
  const analysisFocus=applied.status==='conflict' && applied.calibration.chosen==null?'Compare both directions while conflicting statements are reviewed':
    applied.legacy_review?'Compare both directions while older evidence is reviewed':
    displayedScore<45?'Test a lower stock allocation':
    displayedScore>55?'Test a higher stock allocation':'Test both higher and lower stock allocations';
  const change=movement?Number((movement.after-movement.before).toFixed(1)):0;
  const latestOperation=englishText(latest?.event.user_text || latest?.event.task,'Original-language input');
  return <section className="working-risk" aria-label="Client risk willingness">
    <header className="risk-header"><h3>Client risk willingness</h3>
      <button className="risk-button" type="button" disabled={disabled} aria-expanded={editing} aria-controls="risk-calibration-editor"
        onClick={editing?()=>setEditing(false):edit}>{editing?'Close score editor':'Adjust score'}</button>
    </header>
    <div className="risk-summary">
      <div className="risk-hero">
        <div className="risk-hero-top"><span>Applied score</span>
          <span className={`risk-mode${fixed?' risk-mode-fixed':''}`}>{fixed?'Locked score':adjusted?'Manual starting point':'Automatic learning'}</span>
        </div>
        <div className="risk-value"><strong>{show(effective)}</strong><span> / 100</span>
          {change!==0&&<small className={change>0?'risk-increase':'risk-decrease'}>Since last update {signed(change)}</small>}
        </div>
        <ScoreScale score={effective}/>
        {adjusted&&<p className="risk-hero-note">Advisor set {index.calibration.chosen}{fixed?'':' · Learning continues from this starting point'}</p>}
        {index.status==='insufficient'&&!adjusted&&<p className="risk-hero-note">Starting at neutral until directional evidence is available.</p>}
      </div>
      <ScoreBreakdown index={index}/>
    </div>
    {editing&&<form id="risk-calibration-editor" className="risk-calibration" onSubmit={event=>{event.preventDefault();if(!disabled)save(draft);}}>
      <div className="risk-editor-grid">
        <div className="risk-slider-field">
          <div className="risk-slider-heading"><label htmlFor="risk-draft-score">Adjusted household score</label><output htmlFor="risk-draft-score">{draft}</output></div>
          <input id="risk-draft-score" ref={slider} type="range" min="0" max="100" value={draft} disabled={disabled}
            style={{'--risk-draft':`${draft}%`}} onChange={e=>setDraft(Number(e.target.value))}/>
          <div className="risk-slider-labels" aria-hidden="true"><span>0</span><span>50</span><span>100</span></div>
        </div>
        <div className="risk-lock">
          <div className="risk-lock-heading"><b id="risk-lock-label"><span className="risk-lock-icon" aria-hidden="true"/>Lock applied score</b>
            <button className="risk-switch" type="button" role="switch" aria-labelledby="risk-lock-label"
              aria-describedby="risk-lock-help" aria-checked={draftLocked} disabled={disabled}
              onClick={()=>setDraftLocked(value=>!value)}><span aria-hidden="true" className="risk-switch-thumb"/></button>
          </div>
          <p id="risk-lock-help">{draftLocked?'On · The applied household score stays fixed. The system score continues to update.':
            'Off · The applied score starts at your manual value and adjusts with new evidence.'}</p>
        </div>
      </div>
      <div className="risk-editor-actions">
        {adjusted&&<button type="button" className="risk-button risk-reset-button" disabled={disabled} onClick={()=>save(null)}>Remove manual adjustment</button>}
        <button type="button" className="risk-button" disabled={disabled} onClick={()=>setEditing(false)}>Cancel</button>
        <button type="submit" className="risk-button risk-save-button" disabled={disabled}>Save score</button>
      </div>
    </form>}
    <div className="risk-footer">
      <p><b>Next analysis</b><span>{analysisFocus}{context.scoped_risk_index&&` · Score for this scope ${show(applied.effective)}`}</span></p>
      {latest && <p className="risk-latest"><b>Latest operation</b><span title={latestOperation}>{latestOperation}</span>
        <small>{latest.delivery_status==='pending'?'Pending':latest.delivery_status==='needs_review'?'Needs review':'Processed'}</small></p>}
    </div>
    {error&&<p role="alert" className="risk-action-error">{error}</p>}
  </section>;
}
