import React from 'react';
import {OperationLink} from './OperationReference';
import {englishText} from './displayLanguage';

const hypotheses = [['conservative','Conservative'],['balanced','Balanced'],['growth','Higher risk']];
const criteria = {declared_attitude:'The advisor explicitly described the client’s willingness to take risk',risk_return_tradeoff:'The client stated a trade-off between returns and risk',
  observed_risk_choice:'The client actively chose to take or reduce risk',directed_inquiry:'The advisor explored a specific risk direction',
  stock_target_inquiry:'The advisor’s stated target matches the trial',no_direction:'No risk direction was stated'};
const strengths = {explicit:'Explicit statement',medium:'Moderate signal',weak:'Weak signal',strong:'Strong signal'};
const exploratory = new Set(['exploration_differs_from_declaration','opposing_equal_evidence']);
const support = value => Number.isFinite(value) ? (100*value).toFixed(1)+'%' : '—';

export function scopeName(scope) {
  if (!scope) return 'Household';
  return [scope.account_id,scope.goal_id,scope.applicability==='current' && 'Current analysis round'].filter(Boolean).join(' · ') || 'Household';
}

function evidenceDescription(row) {
  if (row.source) return englishText(row.source);
  const p=row.parameters || {};
  if (Number.isFinite(p.current_stock_weight) && Number.isFinite(p.target_stock_weight))
    return (p.parameter_source==='manual'?'Manual trial':'Default trial') + ', stock weight ' +
      (100*p.current_stock_weight).toFixed(1) + '% → ' + (100*p.target_stock_weight).toFixed(1) + '%';
  return 'See the recorded operation';
}

export function RiskScore({scores, title, sourceNumbers, onOpenEvent}) {
  if (!scores?.length) return null;
  return <section className="audit-score" aria-label={title}>
    <h4>{title}</h4>
    {scores.map(card=>{
      const evidence = card.evidence ?? [];
      const hasScore = Number.isFinite(card.score);
      const softConflict = card.conflicts?.length>0 && card.conflicts.every(kind=>exploratory.has(kind));
      const legacy = evidence.some(row=>row.legacy_text);
      return <div className="audit-score-scope" key={JSON.stringify(card.scope)}>
        <div className="audit-score-meta"><span>{scopeName(card.scope)}</span><span>{evidence.length} evidence items</span></div>
        <p className={card.status==='conflict' ? 'audit-review-label' : undefined}>
          {hasScore ? <><strong>{card.score.toFixed(1)} / 100</strong> · {legacy ? 'Prototype score with older classifications' : 'Prototype score'}</>
            : card.status==='conflict' ? (softConflict ? 'Exploratory cues need review. Prototype score withheld.' : 'Conflicting evidence. Prototype score withheld.')
            : 'A prototype score will appear when evidence is available.'}
        </p>
        {(evidence.length>0 || hasScore) && <details>
          <summary>View criteria and calculation</summary>
          {hasScore && <dl className="audit-support">{hypotheses.map(([key,name])=><div key={key}>
            <dt>{name} support</dt><dd>{support(card.support?.[key])}</dd>
          </div>)}</dl>}
          {legacy && <p>Some records used an older text classification. Review the original wording.</p>}
          {evidence.length>0 && <ol className="audit-ledger">{evidence.map((row,i)=><li key={row.signal_id ?? i}>
            <strong>{evidenceDescription(row)}</strong>
            <p>{[strengths[row.strength], criteria[row.criterion] ||
              (row.legacy_text?'Older text classification':'Compared with stock exposure at the time')].filter(Boolean).join(' · ')}</p>
            {row.interpretation && <p>Interpretation: “{englishText(row.interpretation)}”</p>}
            <p>Weights (conservative / balanced / higher risk): {row.multipliers?.map(v=>Number.isFinite(v) ? Number(v.toFixed(4)) : '—').join(' / ')}
              {Number.isFinite(row.running_score) && <> · Score through this item: {row.running_score.toFixed(1)}</>}
            </p>
            {Number.isFinite(row.score_without_this_factor) && <p>Without this weight, the score would be {row.score_without_this_factor.toFixed(1)}, holding the other evidence fixed.</p>}
            {row.event_id && <small><OperationLink eventId={row.event_id} number={sourceNumbers?.[row.event_id]}
              onOpenEvent={onOpenEvent}/>{row.revision!=null && <> · Revision {row.revision}</>}</small>}
          </li>)}</ol>}
          <details className="audit-method"><summary>View calculation rules</summary>
            <p>Each group contributes its strongest evidence for a given scope and preference. Equally strong opposing signals need review. A newer explicit statement replaces the older statement in the same scope. {card.default_group_limit!=null && <>Only the most recent {card.default_group_limit} groups of default trials can contribute.</>}</p>
            <p>Explicit statements and risk-return trade-offs are usually stronger evidence. Cash needs, investment goals, and simple lookups contribute zero on their own.</p>
            <p>Initial support (conservative / balanced / higher risk): {hypotheses.map(([key])=>support(card.prior?.[key])).join(' / ')}.
              At each step, multiply by the matching weights, then divide by the sum of all three values. Prototype score = 50 × balanced support + 100 × higher-risk support (on a 0-to-1 support scale).</p>
            {card.rule_version && <small>Rule version {card.rule_version}</small>}
          </details>
        </details>}
      </div>;
    })}
  </section>;
}
