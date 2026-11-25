import React from 'react';

const hypotheses = [['conservative','保守'],['balanced','均衡'],['growth','较高风险']];
const criteria = {declared_attitude:'顾问明确描述客户的风险意愿',risk_return_tradeoff:'客户说明了收益与风险之间的取舍',
  observed_risk_choice:'客户主动选择承担或减少风险',no_direction:'未给出风险方向'};
const strengths = {explicit:'明确声明',medium:'中等信号',weak:'弱信号',strong:'强信号'};

export function scopeName(scope) {
  if (!scope) return '客户整体';
  return [scope.account_id,scope.goal_id,scope.applicability==='current' && '本轮分析'].filter(Boolean).join(' · ') || '客户整体';
}

function evidenceDescription(row) {
  if (row.source) return row.source;
  const p=row.parameters || {};
  if (Number.isFinite(p.current_stock_weight) && Number.isFinite(p.target_stock_weight))
    return (p.parameter_source==='manual'?'手动试算':'默认试算') + '，股票占比 ' +
      (100*p.current_stock_weight).toFixed(1) + '% → ' + (100*p.target_stock_weight).toFixed(1) + '%';
  return row.rule;
}

export function RiskScore({scores, visibleEvents}) {
  if (!scores?.length) return null;
  return <section className="risk-score" aria-label="客户风险意愿分">
    <h4>客户风险意愿分</h4>
    {scores.map(card=><div key={JSON.stringify(card.scope)}>
      <p><strong>{scopeName(card.scope)} · {card.score == null
        ? (card.status==='conflict'?'证据有分歧，等待核对':'证据不足，暂不出分')
        : (card.evidence.some(row=>row.legacy_text)?'旧证据试算 ':'') + card.score.toFixed(1) + ' / 100'}</strong>　{card.evidence.length} 组计分依据</p>
      {card.score!=null && <><meter aria-label="风险意愿指数" min="0" max="100" value={card.score}/>
        <p>{hypotheses.map(([k,title])=>title+'支持 '+(100*card.support[k]).toFixed(1)+'%').join('　')}</p></>}
      {card.evidence.some(row=>row.legacy_text) && <p>含旧版文本分类，建议核对原话后再采用这个分数。</p>}
      <details><summary>查看判据和逐条复算</summary>
        <p>先核实说的是哪位客户、哪段时间，以及是事实、假设还是否定。明确风险意愿或明确收益与风险取舍，可成为强依据。行为暗示较弱。资金需求、投资目标、单纯查询保留在客户资料中，单独出现时计分贡献为零。</p>
        <p>初始支持（保守、均衡、较高风险）为 {hypotheses.map(([k])=>(100*card.prior[k]).toFixed(1)+'%').join(' / ')}。每一步分别乘以对应系数，再除以三项之和。风险意愿分 = 50 × 均衡支持 + 100 × 较高风险支持（支持取 0 至 1）。</p>
        <p>同一范围和维度每组只取最强证据。等强且方向相反时该组待核实。较新的明确声明替代旧声明。默认试算最多计入最近 {card.default_group_limit ?? '若干'} 组。以下用当前有效证据按时间复算，系数来自原型规则 {card.rule_version}。</p>
        <ol>{card.evidence.map(row=><li key={row.event_id + ':' + row.revision}>
          <strong>{evidenceDescription(row)}</strong> · {strengths[row.strength] || row.strength}<br/>
          {criteria[row.criterion] || (row.legacy_text?'旧版语义分类，缺少判据字段':'试算目标与当时股票敞口比较')}
          {row.interpretation && <span> · 模型解读「{row.interpretation}」</span>}<br/>
          三项系数 {row.multipliers.join(' / ')} → 累计 {row.running_score.toFixed(1)} 分
          <span>。仅去掉这一组系数，其余计分依据固定时为 {row.score_without_this_factor?.toFixed(1) ?? '暂无分数'}。</span>
          <small> · 来源 {visibleEvents?.has(row.event_id)
            ? <a href={'#learning-event-'+encodeURIComponent(row.event_id)}>查看原始记录</a> : row.event_id}（修订 {row.revision}）</small>
        </li>)}</ol>
      </details>
    </div>)}
    <p>分数越高，已有证据越支持承担波动。支持度为规则权重归一化的结果。刻度和系数尚待用独立客户测评校准，客户承担损失的财务能力仍需单独核实。</p>
  </section>;
}
