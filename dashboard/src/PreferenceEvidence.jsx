import React from 'react';
import {scopeName} from './RiskScore';
import {englishText, englishJson} from './displayLanguage';

const names = {risk_tilt:'Risk preference',adjustment_style:'Adjustment style',conservative:'Conservative',balanced:'Balanced',
  growth:'Growth-oriented',gradual:'Phase changes in',flexible:'Open to one larger adjustment',liquidity:'Near-term cash need',
  income_goal:'Income goal',return_priority:'Return objective',loss_reaction:'Reaction to a loss',capital_preservation:'Principal preservation objective',leverage:'Use of leverage',risk_change:'Change in risk attitude',
  adjustment_pace:'Adjustment pace',other:'Client note',risk:'Risk and return',holdings:'Holdings',projection:'Goal projection',
  quote:'Quote',history:'Price history',constraint:'Explicit constraint',budget:'Budget',restricted_funds:'Restricted funds',
  max_loss_limit:'Loss limit',maximum_loss_limit:'Maximum loss limit',adopted_target_allocation:'Accepted target allocation'};
const label = value => typeof value === 'string' && Object.hasOwn(names,value) ? names[value] :
  (typeof value === 'string' ? englishText(value) : englishJson(value ?? 'Needs verification'));
export {label as evidenceLabel};
const pct = value => (value * 100).toFixed(1) + '%';
const scoreFormat = new Intl.NumberFormat('en-US', {minimumFractionDigits:1,maximumFractionDigits:2});
const scoreParts = {anchor:'Long-term baseline',trial_points:'Allocation trial',behavior_points:'Directional actions',current_points:'Recent attitude'};
const delta = value => (value>0?'+':'')+scoreFormat.format(Math.abs(value)<1e-9?0:value);

export function OperationScore({row}) {
  const effects=row.score_effects || [];
  if (!effects.length) {
    const message=row.delivery_status==='pending'?'Waiting for the learning module':
      row.delivery_status==='superseded'?'This revision did not contribute':'No score impact to recalculate';
    return <p className="operation-score">{row.kind==='retract'?'Withdrawal impact':'Score impact'} · {message}</p>;
  }
  const important=effects.filter(effect=>effect.revision>2 || effect.changes.some(change=>
    Math.abs(change.effective[1]-change.effective[0])>=1e-9 || Object.keys(change.components).length));
  return <div className="operation-score">
    {(important.length?important:[effects.at(-1)]).map(effect=>{
      const action=effect.kind==='retract'?'Withdrawal':effect.revision>2?'Reinterpreted':effect.revision===1?'First processing':'This operation';
      return <div key={effect.revision}>
        {effect.changes.map(change=>{
          const [before,after]=change.effective;
          const [systemBefore,systemAfter]=change.reference;
          const movement=after-before;
          return <div className="operation-score-change" key={JSON.stringify(change.scope)}>
            <p>{action} · {scopeName(change.scope)}{change.scope?' applied score':' applied score'} <strong>
              {scoreFormat.format(before)} → {scoreFormat.format(after)} ({Math.abs(movement)<1e-9?'0 points':
                Math.abs(movement)<.005?(movement>0?'up less than 0.01 points':'down less than 0.01 points'):delta(movement)+' points'})</strong></p>
            {(before!==systemBefore || after!==systemAfter) && <p>System score {scoreFormat.format(systemBefore)}
              {' → '+scoreFormat.format(systemAfter)+' ('+delta(systemAfter-systemBefore)+' points)'}</p>}
            {Object.keys(change.components).length>0 && <p>{Object.entries(change.components)
              .map(([key,value])=>scoreParts[key]+' '+delta(value)+' points').join(' · ')}</p>}
          </div>;
        })}
      </div>;
    })}
    {row.delivery_status==='pending' && <p>This update is still pending.</p>}
  </div>;
}

export function describeEvidence(item) {
  if (typeof item === 'string') return label(item);
  if (!item) return 'Needs verification';
  if (item.probabilities) {
    const entries = Object.entries(item.probabilities).filter(([,v])=>Number.isFinite(v)).sort((a,b)=>b[1]-a[1]);
    if (!entries.length) return label(item.field);
    const leading = entries.filter(([,v])=>Math.abs(v-entries[0][1])<1e-9);
    const direction = leading.length===1 ? label(entries[0][0]) + ' support ' + pct(entries[0][1]) : 'No single direction is supported';
    const source = item.sources?.at(-1);
    const basis = englishText(source?.quote) || (Number.isFinite(source?.basis?.target_stock_weight) ?
      'Stock target tested: ' + pct(source.basis.target_stock_weight) : '');
    return label(item.field) + ' · ' + direction + (basis ? ', based on “' + basis + '”' : '');
  }
  if (item.kind === 'calculation_topic') {
    const basis = item.basis || {};
    const detail = Number.isFinite(basis.annual_volatility) ? ', annualized portfolio volatility ' + pct(basis.annual_volatility) :
      (basis.symbol ? ' · ' + basis.symbol : '');
    const fields = {target_amount:'Goal amount',years:'Horizon (years)',annual_contribution:'Annual contribution',annual_withdrawal:'Annual withdrawal'};
    const manual = Object.entries(basis.manual_parameters || {}).filter(([k])=>Object.hasOwn(fields,k))
      .map(([k,v])=>fields[k] + ' ' + v).join(', ');
    return 'Reviewed ' + label(item.value) + detail + (manual ? ', manual inputs: ' + manual : '');
  }
  if (['client_cue','constraint','behavioral_hint'].includes(item.kind)) return label(item.value) + ' · ' + englishText(item.evidence_text);
  if (Number.isFinite(item.target_stock_weight)) return 'Tested ' + pct(item.target_stock_weight) + ' stock weight';
  if (['hypothetical','negated','uncertain'].includes(item.kind))
    return ({hypothetical:'Hypothetical scenario',negated:'Denied statement',uncertain:'Unconfirmed'}[item.kind]) + ' · ' + englishText(item.evidence_text);
  if (item.field) return label(item.field) + ' · ' + label(item.value);
  return englishText(item.evidence_text) || label(item.value ?? item.summary ?? item.reason ?? 'See the source record');
}

export function EventImpact({row,withdrawn,currentIndex}) {
  if (withdrawn || !['applied','needs_review'].includes(row.delivery_status)) return null;
  const extraction = row.update?.extraction;
  if (!extraction) return null;
  const signals = extraction.signals || [];
  if (!signals.length) return <p>{row.delivery_status==='needs_review'?'This wording needs review. No usable cue was found yet.':'Processed. No client cue or scoring evidence was found.'}</p>;
  const applied = new Set(row.update.applied || []);
  const counted=currentIndex?.scoring_event_ids?.includes(row.event_id);
  return <><ul>{signals.map(s=>{
    let explanation;
    if (s.kind==='allocation_exploration' && currentIndex?.policy>=2)
      explanation='Allocation trial · '+label(s.value)+' direction. '+(counted?'Included in the current score, subject to the daily cap.':'No separate contribution to the current score.');
    else if (!applied.has(s.signal_id)) explanation = 'No separate weight in the original classification';
    else if (s.kind === 'allocation_exploration')
      explanation='Allocation trial · '+label(s.value)+' direction. In the same round, a manual trial contributes at most 2 points and a default trial at most 0.25 points.';
    else if (s.kind === 'behavioral_hint')
      explanation = 'Tentative direction · ' + label(s.dimension) + ' / ' + label(s.value) + (s.strength==='medium'?' (moderate evidence)':' (weak evidence)');
    else if (s.kind === 'explicit_statement' || s.kind === 'preference_change') explanation = 'Advisor statement · ' + label(s.value);
    else explanation = describeEvidence(s);
    if (s.kind==='client_cue' && s.reason) explanation += '. Interpretation: ' + englishText(s.reason);
    if (s.kind==='uninterpreted_excerpt') explanation='This part still needs interpretation. Original text: '+englishText(s.evidence_text);
    if (s.kind==='unverified_target') explanation='No single directional change in stock exposure was verified. This trial earned no directional points. Original text: '+englishText(s.evidence_text);
    return <li key={s.signal_id}>{explanation}</li>;
  })}</ul>{counted && <p>This operation contributes to the current score for {scopeName(currentIndex.scope)}.</p>}{row.update.risk_score_changes?.length ? <details><summary>View the earlier evidence support</summary>{row.update.risk_score_changes.map(change=><p key={JSON.stringify(change.scope)}>
      Historical evidence score including notes ({scopeName(change.scope)}) {change.before?.toFixed(1) ?? 'No score'} → {change.after?.toFixed(1) ?? 'No score'}
    </p>)}</details> : !counted && !signals.some(s=>s.dimension==='risk_tilt') && <p>Direct contribution to the risk willingness score: 0.</p>}</>;
}

export function RiskInterpretation({result,snapshot,onFollowUp}) {
  const context=snapshot?.status==='connected' ? snapshot.context : null;
  const risk=context?.team_declared?.find(row=>row.field==='risk_tilt');
  const tentative=context?.tentative_preferences?.find(row=>row.field==='risk_tilt');
  const cues=context?.client_cues || [];
  const liquidity=cues.findLast(row=>row.value==='liquidity');
  const income=cues.findLast(row=>row.value==='income_goal');
  const returns=cues.findLast(row=>row.value==='return_priority');
  const lossReaction=cues.findLast(row=>row.value==='loss_reaction');
  const protect=cues.findLast(row=>row.value==='capital_preservation');
  const riskChange=cues.findLast(row=>row.value==='risk_change' || (row.kind==='behavioral_hint' && row.dimension==='risk_tilt'));
  const leverage=cues.findLast(row=>row.value==='leverage');
  const pace=context?.team_declared?.find(row=>row.field==='adjustment_style') ||
    context?.tentative_preferences?.find(row=>row.field==='adjustment_style') ||
    cues.findLast(row=>row.value==='adjustment_pace' || (row.kind==='behavioral_hint' && row.dimension==='adjustment_style'));
  const constraints=context?.explicit_constraints || [];
  const nextTask = liquidity || income ? 'projection' : 'allocation';
  const emphasis = {conservative:'Start with volatility and the client’s capacity for loss, then review expected return.',growth:'Confirm whether the client can accept the volatility that comes with seeking higher returns.',balanced:'Discuss expected return and volatility together to understand the client’s trade-off.'};
  const comparison = result.scenario_comparison;
  const general = context?.risk_preference_scores?.find(s=>!s.scope);
  const working = context?.scoped_risk_index || context?.working_risk_index;
  return <section aria-label="Risk interpretation using client context"><h4>How to use this risk calculation</h4>
    <p>Annualized portfolio volatility is {pct(result.annual_volatility)} based on the current holdings and draft assumptions. It describes how much portfolio returns vary.</p>
    {comparison?.status==='success' && <div className="risk-comparison">
      <h4>Allocation comparison</h4>
      <p>{comparison.source==='manual_scenario'?'Requested allocation':'Saved household draft'} · Stocks {(100*comparison.weights.stock).toFixed(1)}%, bonds {(100*comparison.weights.bond).toFixed(1)}%. Both columns use the same stock and bond assumptions.</p>
      <div className="portfolio-table" tabIndex={0} role="region" aria-label="Risk and return comparison results"><table><thead><tr><th>Annualized metric</th><th>Current holdings</th><th>Trial portfolio</th><th>Change (pp)</th></tr></thead><tbody>
        {[['Volatility','annual_volatility','volatility_delta_pp'],['Expected return','annual_expected_return','return_delta_pp']].map(([title,key,delta])=>
          <tr key={key}><th>{title}</th><td>{(result[key]*100).toFixed(2)}%</td><td>{(comparison[key]*100).toFixed(2)}%</td>
            <td>{comparison[delta]>0?'+':''}{comparison[delta].toFixed(2)}</td></tr>)}
      </tbody></table></div>
      {!working && general?.score!=null && <p>Household risk willingness evidence score {general.score.toFixed(1)} / 100{general.evidence.some(e=>e.legacy_text)?' (includes older classifications that need review)':''}. {general.evidence.some(e=>e.legacy_text) ? 'Review the original statements in Scoring evidence before deciding what to discuss.' : general.support.conservative>Math.max(general.support.balanced,general.support.growth)
        ? 'Start with the difference in volatility, then confirm what change in expected return the client can accept.'
        : general.support.growth>Math.max(general.support.balanced,general.support.conservative)
          ? 'Start with the difference in expected return, then confirm whether the client is willing to take on the related volatility.'
          : 'Discuss both changes and ask the client which matters most.'}</p>}
      {(working?.status ?? general?.status)==='conflict' && <p>The client’s risk statements conflict. Verify the original wording before discussing the trade-offs.</p>}
    </div>}
    {!context ? <p>Client context was unavailable. Confirm how the results apply to this client.</p> : <>
      {risk && (working ? working.anchor_source===risk.evidence_text && working.status!=='conflict'
        : general?.status!=='conflict' && !general?.evidence.some(e=>e.legacy_text)) && <p>The recorded risk preference is {label(risk.value)} (source: “{englishText(risk.evidence_text)}”).{!working && emphasis[risk.value]}</p>}
      {!risk && tentative && <p>The evidence is tentative. {describeEvidence(tentative)}. Confirm what level of volatility the client can accept.</p>}
      {!risk && !tentative && (!working || working.status==='insufficient') && <p>The client’s risk preference is still unknown. Ask how comfortable they are with changes in portfolio value.</p>}
      {context.conflicts?.some(c=>c.dimension!=='risk_tilt' || !working) && <p>The evidence conflicts. Check the original wording.</p>}
      {liquidity && <p>The client mentioned “{englishText(liquidity.evidence_text)}”. Confirm the amount and timing, then test the withdrawals in Goal projection.</p>}
      {income && <p>The client mentioned “{englishText(income.evidence_text)}”. Confirm the cash income needed. Expected annual returns do not measure withdrawable cash income.</p>}
      {returns && <p>Return objective: “{englishText(returns.evidence_text)}”. Ask how much of a portfolio decline the client would accept in pursuit of higher returns.</p>}
      {lossReaction && <p>Loss reaction: “{englishText(lossReaction.evidence_text)}”. Confirm the actual loss, when the funds are needed, and the drawdown the client can accept.</p>}
      {protect && <p>The client mentioned “{englishText(protect.evidence_text)}”. Confirm the acceptable loss amount and time horizon.</p>}
      {riskChange && <p>Recent cue: “{englishText(riskChange.evidence_text)}”. Confirm whether it reflects a temporary response or a change in long-term risk preference.</p>}
      {leverage && <p>Leverage cue: “{englishText(leverage.evidence_text)}”. This estimate uses stock and bond weights. Verify leveraged exposure separately.</p>}
      {pace && <p>When comparing adjustments, check the pace the client can accept. Source: “{englishText(pace.evidence_text) || describeEvidence(pace)}”.</p>}
      {constraints.map(item=><p key={item.signal_id}>Recorded constraint: “{englishText(item.evidence_text)}”. Check it separately from the stock and bond volatility estimate.</p>)}
    </>}
    <button type="button" onClick={()=>onFollowUp(nextTask)}>{nextTask==='projection'?'Model goals and withdrawals':'Review this client’s allocation drift'}</button>
  </section>;
}
