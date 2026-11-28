import React, {useState} from 'react';
import {scopeName} from './RiskScore';
import {englishText} from './displayLanguage';

const names = {
  current: 'Current holdings',
  baseline: 'Comparison target',
  less_equity: 'Extra scenario · Lower stocks',
  more_equity: 'Extra scenario · Higher stocks',
};
const focusText = {
  downside: 'This run tests how much downside a lower stock allocation would avoid',
  upside: 'This run tests the upside and added downside of a higher stock allocation',
  compare: 'This run compares higher and lower stock allocations',
};
const usd = new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'});
const money = cents => usd.format(cents / 100);
const percentage = new Intl.NumberFormat('en-US', {maximumFractionDigits:2});
const percent = value => percentage.format(value) + '%';

function BudgetStatus({loss, limit}) {
  if (limit === null) return null;
  const margin = limit - loss;
  return <span className={'scenario-budget ' + (margin < 0 ? 'is-over' : 'is-within')}>
    {margin < 0 ? 'Over by ' + money(-margin) : margin === 0 ? 'At the limit' : 'Within limit · ' + money(margin)}
  </span>;
}

export function ScenarioCheck({analysis, snapshot}) {
  const [shockInput, setShock] = useState(null);
  const [limitInput, setLimit] = useState('');
  const [invalidLimit, setInvalidLimit] = useState(false);
  if (!analysis) return null;

  const originalShock = analysis.stock_shock_pct;
  const canChangeShock = Number.isFinite(analysis.portfolio_value) && analysis.portfolio_value > 0;
  const shock = shockInput ?? String(originalShock);
  const shockNumber = Number(shock);
  const validShock = shock !== '' && Number.isFinite(shockNumber) && shockNumber >= 0 && shockNumber <= 100;
  const limitNumber = Number(limitInput);
  const validLimit = !invalidLimit && limitNumber >= 0 && Number.isSafeInteger(Math.round(limitNumber * 100));
  const limit = validLimit && limitInput !== '' ? Math.round(limitNumber * 100) : null;
  const lossOf = row => shockNumber === originalShock || !canChangeShock
    ? Math.round(-row.stock_fall_usd * 100)
    : Math.round(analysis.portfolio_value * row.stock_weight * shockNumber);
  const baseline = analysis.rows.find(row => row.label === 'baseline');
  const fallback = analysis.legacy_review || (analysis.score_status === 'conflict' && !analysis.advisor_adjusted);
  const constraints = snapshot?.status === 'connected'
    ? (snapshot.context?.explicit_constraints ?? []).filter(row => row.value === 'max_loss_limit' && row.evidence_text
      && (!row.scope?.account_id || row.scope.account_id === analysis.account_id))
    : [];

  return <section className="scenario-check" aria-labelledby="scenario-heading">
    <header className="scenario-heading">
      <div>
        <h3 id="scenario-heading">Allocation stress test</h3>
        <p>Compare current holdings, the target, and score-driven alternatives under the same stock shock.</p>
      </div>
      {Number.isFinite(analysis.score_used) && <span className="scenario-score">
        {scopeName(analysis.scope)} · {analysis.advisor_adjusted ? 'Adjusted score' : 'Applied score'} <b>{analysis.score_used.toFixed(1)}</b>
      </span>}
    </header>
    <p className="scenario-explanation">
      <strong>The score sets the direction and size of the extra scenarios.</strong>{' '}{fallback ? 'The evidence needs review. Both directions are tested by default.' : focusText[analysis.focus] + '.'}{' '}
      The comparison starts from {analysis.target_source === 'request_override' ? 'the target you entered' : 'the saved draft'} and tests a stock allocation up to {analysis.scenario_step_pp} percentage points away.
    </p>
    <div className="scenario-controls">
      <label>
        <span>Stock shock <small>%</small></span>
        <input type="number" min="0" max="100" step="any" disabled={!canChangeShock}
          aria-invalid={!validShock} aria-describedby="scenario-shock-hint" value={shock} onChange={e=>setShock(e.target.value)}/>
        <small id="scenario-shock-hint">{!validShock ? 'Enter a value from 0 to 100.' :
          canChangeShock ? 'The same shock is applied up and down.' : 'This result uses the original shock.'}</small>
      </label>
      <label>
        <span>Loss limit <small>USD · optional</small></span>
        <input type="number" min="0" step="0.01" placeholder="Enter an acceptable portfolio loss"
          aria-invalid={!validLimit} aria-describedby="scenario-limit-hint" value={limitInput}
          onChange={e=>{setLimit(e.target.value);setInvalidLimit(e.target.validity.badInput || e.target.validity.stepMismatch);}}/>
        <small id="scenario-limit-hint">{validLimit ? 'See how far each downside scenario is above or below the limit.' : 'Enter a nonnegative dollar amount with at most two decimal places.'}</small>
      </label>
    </div>
    {constraints.length > 0 && <details className="scenario-constraints">
      <summary>View recorded loss constraints ({constraints.length})</summary>
      <p>Confirm the amount, scope, and measurement before entering a loss limit above.</p>
      <ul>{constraints.map((row,i)=><li key={row.signal_id ?? i}>{scopeName(row.scope)} · “{englishText(row.evidence_text)}”</li>)}</ul>
    </details>}
    {validShock && <div className="portfolio-table scenario-table" tabIndex={0} role="region" aria-label="Stress test results by allocation">
      <table role="table">
        <thead role="rowgroup"><tr role="row"><th scope="col">Allocation</th><th scope="col">Stock weight</th>
          <th scope="col">Stocks down {percent(shockNumber)}</th><th scope="col">Stocks up {percent(shockNumber)}</th></tr></thead>
        <tbody role="rowgroup">{analysis.rows.map(row=>{
          const loss = lossOf(row);
          const difference = loss - lossOf(baseline);
          const compare = row.label !== 'baseline';
          return <tr key={row.label} role="row" className={row.label === 'baseline' ? 'scenario-baseline' : undefined}>
            <th role="rowheader" scope="row">{names[row.label]}</th>
            <td role="cell" className="scenario-weight"><span className="scenario-mobile-label" aria-hidden="true">Stock weight </span>{percent(row.stock_weight * 100)}</td>
            <td role="cell" className="scenario-loss"><span className="scenario-mobile-label" aria-hidden="true">Stocks down {percent(shockNumber)}</span><b>{money(loss ? -loss : 0)}</b>
              {compare && <small>{difference === 0 ? 'Same as target' : (difference < 0 ? 'Loss reduction: ' : 'Additional loss: ') + money(Math.abs(difference))}</small>}
              <BudgetStatus loss={loss} limit={limit}/>
            </td>
            <td role="cell" className="scenario-gain"><span className="scenario-mobile-label" aria-hidden="true">Stocks up {percent(shockNumber)}</span><b>{money(loss)}</b>
              {compare && <small>{difference === 0 ? 'Same as target' : (difference < 0 ? 'Less upside: ' : 'More upside: ') + money(Math.abs(difference))}</small>}
            </td>
          </tr>;
        })}</tbody>
      </table>
    </div>}
    {analysis.rows.length === 2 && <p>The comparison target is already at the stock-weight boundary in the selected direction (0% or 100%).</p>}
    <p className="scenario-footnote">Bond prices are held flat. Results show the one-time change in the synthetic portfolio under this stock shock.</p>
  </section>;
}
