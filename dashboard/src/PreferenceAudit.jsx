import React from 'react';
import {RiskScore, scopeName} from './RiskScore';
import {evidenceLabel} from './PreferenceEvidence';
import {OperationLink} from './OperationReference';
import {englishText} from './displayLanguage';

const issues = {
  exploration_differs_from_declaration: ['Exploration differs from a recorded statement',
    'Confirm whether the advisor was comparing scenarios or the client changed preferences. Questions and calculations are exploratory evidence.'],
  opposing_equal_evidence: ['This round contains opposing cues',
    'Check whether both directions were being compared. If the client made a choice, record the decision and its scope.'],
  declaration_denied: ['A later statement contradicts an earlier one',
    'Confirm which statement applies now and to which accounts or goals.'],
  conflicting_statements: ['One input contains conflicting statements',
    'Confirm which funds, goals, or time periods the statements refer to.'],
  applicable_scopes_disagree: ['Statements differ across applicable scopes',
    'Verify which statement applies to the household, account, or goal.'],
  review_multiple_constraints: ['Multiple records for one type of constraint',
    'Compare amounts, time periods, and scope to confirm which constraints apply together.'],
};
const sourceKinds = {
  explicit_statement:'Explicit statement', preference_change:'Preference change', denial:'Denial of an earlier statement',
  allocation_exploration:'Allocation trial', behavioral_hint:'Directional cue', reasoned_choice:'Reasoned choice', constraint:'Explicit constraint',
};
const entryPoints = {advisor_note:'Client notes', agent:'Agent question', direct:'Portfolio calculation'};
const dateFormat = new Intl.DateTimeFormat('en-US',{dateStyle:'medium'});
const percent = new Intl.NumberFormat('en-US',{style:'percent',maximumFractionDigits:2});

function ConflictSource({source,number,onOpenEvent}) {
  const date = Date.parse(source.occurred_at);
  const channel = entryPoints[source.entry_point] ?? 'Operation record';
  const kind = sourceKinds[source.kind] ?? channel;
  const target = source.basis?.target_stock_weight;
  const current = source.basis?.current_stock_weight;
  const quote = source.evidence_text || source.user_text;
  return <li className="audit-source">
    <div className="audit-source-meta"><strong>{kind}</strong>
      <span>{[kind!==channel ? channel : '',scopeName(source.scope),Number.isFinite(date) ? dateFormat.format(date) : ''].filter(Boolean).join(' · ')}</span>
    </div>
    {quote ? <blockquote>“{englishText(quote)}”</blockquote> : Number.isFinite(target) &&
      <p>Stock weight tested: {Number.isFinite(current) ? percent.format(current)+' → ' : ''}{percent.format(target)}</p>}
    <OperationLink eventId={source.event_id} number={number} onOpenEvent={onOpenEvent}/>
  </li>;
}

export function PreferenceAudit({context,sources,sourceNumbers,onOpenEvent,onDownload}) {
  if (!context) return <p className="audit-empty">Review items and scoring evidence will appear when the learning data is ready.</p>;
  const conflicts = context.conflicts ?? [];
  const separated = context.risk_source_scores;
  const operations = separated?.operations ?? context.risk_preference_scores;
  const notes = separated?.notes?.filter(card=>card.evidence?.length || card.status==='conflict');

  return <div className="preference-audit">
    <section className="audit-review" aria-label="Items to review">
      <div className="audit-heading"><h4>Items to review</h4><span className="audit-count">{conflicts.length} items</span></div>
      {!conflicts.length && <p>No conflicting evidence to review.</p>}
      {conflicts.map((conflict,i)=>{
        const [title,message] = Object.hasOwn(issues,conflict.kind) ? issues[conflict.kind] :
          ['Evidence needs review','Check the related records and their scope.'];
        const refs = [...new Set(conflict.evidence_ids ?? [])];
        const index = conflict.kind==='applicable_scopes_disagree' ? sources?.events : sources?.signals;
        const found = refs.map(ref=>[ref,index?.[ref]]).filter(([,source])=>source);
        return <details className="audit-issue" key={i} open={conflicts.length===1 || undefined}>
          <summary><strong>{title}</strong><span>{evidenceLabel(conflict.field ?? conflict.dimension)} · {scopeName(conflict.scope)}</span></summary>
          <div className="audit-issue-body">
            <p>{message}</p>
            {found.length>0 && <ul className="audit-sources">{found.map(([ref,source])=><ConflictSource key={ref} source={source}
              number={sourceNumbers?.[source.event_id]} onOpenEvent={onOpenEvent}/>)}</ul>}
            {found.length<refs.length && <p className="audit-muted">Sources unavailable: {refs.length-found.length}. Check the scoring data download for details.</p>}
          </div>
        </details>;
      })}
    </section>
    <section className="audit-scores" aria-label="Raw evidence support">
      <h4>Raw evidence support</h4>
      <p>Review the direction and weight of the evidence here. Client risk willingness above shows the score used in analysis.</p>
      {!operations?.length && !notes?.length && <p>No evidence to recalculate yet.</p>}
      <div className="audit-score-grid">
        <RiskScore title={separated?.operations ? 'Routine operations':'Client risk willingness evidence'} scores={operations} sourceNumbers={sourceNumbers} onOpenEvent={onOpenEvent}/>
        <RiskScore title="Client notes" scores={notes} sourceNumbers={sourceNumbers} onOpenEvent={onOpenEvent}/>
      </div>
    </section>
    <footer className="audit-footer">
      <p>These support values are rule-based and have yet to be calibrated against independent assessments. Verify the client’s financial capacity for loss separately.</p>
      <button type="button" onClick={onDownload}>Download scoring data (JSON)</button>
    </footer>
  </div>;
}
