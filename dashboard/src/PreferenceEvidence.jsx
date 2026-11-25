import React from 'react';

const names = {risk_tilt:'风险倾向',adjustment_style:'调整方式',conservative:'保守',balanced:'均衡',
  growth:'偏向较高风险',gradual:'分阶段调整',flexible:'可接受一次较大调整',liquidity:'近期用钱需求',
  income_goal:'收入目标',capital_preservation:'本金保障目标',leverage:'杠杆使用习惯',risk_change:'风险态度变化',
  adjustment_pace:'调整节奏',other:'客户说明',risk:'风险与收益',holdings:'持仓构成',projection:'目标预测',
  quote:'报价',history:'历史价格',constraint:'明确约束',budget:'预算',restricted_funds:'受限资金',
  max_loss_limit:'损失限制',maximum_loss_limit:'最大损失限制',adopted_target_allocation:'已接受的目标配置'};
const label = value => typeof value === 'string' && Object.hasOwn(names,value) ? names[value] :
  (typeof value === 'string' ? value : JSON.stringify(value ?? '待核实'));
const pct = value => (value * 100).toFixed(1) + '%';

export function describeEvidence(item) {
  if (typeof item === 'string') return label(item);
  if (!item) return '待核实';
  if (item.probabilities) {
    const entries = Object.entries(item.probabilities).filter(([,v])=>Number.isFinite(v)).sort((a,b)=>b[1]-a[1]);
    if (!entries.length) return label(item.field);
    const leading = entries.filter(([,v])=>Math.abs(v-entries[0][1])<1e-9);
    const direction = leading.length===1 ? label(entries[0][0]) + ' 支持度 ' + pct(entries[0][1]) : '证据尚无单一方向';
    const source = item.sources?.at(-1);
    const basis = source?.quote || (Number.isFinite(source?.basis?.target_stock_weight) ?
      '试算股票目标 ' + pct(source.basis.target_stock_weight) : '');
    return label(item.field) + ' · ' + direction + (basis ? '，依据「' + basis + '」' : '');
  }
  if (item.kind === 'calculation_topic') {
    const basis = item.basis || {};
    const detail = Number.isFinite(basis.annual_volatility) ? '，本次组合年化波动率 ' + pct(basis.annual_volatility) :
      (basis.symbol ? ' · ' + basis.symbol : '');
    const fields = {target_amount:'目标金额',years:'期限（年）',annual_contribution:'年度投入',annual_withdrawal:'年度提款'};
    const manual = Object.entries(basis.manual_parameters || {}).filter(([k])=>Object.hasOwn(fields,k))
      .map(([k,v])=>fields[k] + ' ' + v).join('、');
    return '关注' + label(item.value) + detail + (manual ? '，本次手动试算 ' + manual : '');
  }
  if (['client_cue','constraint','behavioral_hint'].includes(item.kind)) return label(item.value) + ' · ' + item.evidence_text;
  if (Number.isFinite(item.target_stock_weight)) return '试算过股票 ' + pct(item.target_stock_weight) + ' 的情景';
  if (['hypothetical','negated','uncertain'].includes(item.kind))
    return ({hypothetical:'假设场景',negated:'否定表述',uncertain:'尚待确认'}[item.kind]) + ' · ' + item.evidence_text;
  if (item.field) return label(item.field) + ' · ' + label(item.value);
  return item.evidence_text || label(item.value ?? item.summary ?? item.reason ?? '详情见来源记录');
}

export function EventImpact({row,withdrawn}) {
  if (withdrawn || row.delivery_status !== 'applied') return null;
  const extraction = row.update?.extraction;
  if (!extraction) return null;
  const signals = extraction.signals || [];
  if (!signals.length) return <p>本条已处理，未提取出可用的客户线索。</p>;
  const applied = new Set(row.update.applied || []);
  return <ul>{signals.map(s=>{
    let explanation;
    if (!applied.has(s.signal_id)) explanation = '本条在本次更新中未单独计权';
    else if (s.kind === 'behavioral_hint' || s.kind === 'allocation_exploration')
      explanation = '暂定方向 · ' + label(s.dimension) + ' / ' + label(s.value) + (s.strength==='medium'?'（中等证据）':'（弱证据）');
    else if (s.kind === 'explicit_statement' || s.kind === 'preference_change') explanation = '团队说明 · ' + label(s.value);
    else explanation = describeEvidence(s);
    return <li key={s.signal_id}>{explanation}</li>;
  })}</ul>;
}

export function RiskInterpretation({result,snapshot,onFollowUp}) {
  const context=snapshot?.status==='connected' ? snapshot.context : null;
  const risk=context?.team_declared?.find(row=>row.field==='risk_tilt');
  const tentative=context?.tentative_preferences?.find(row=>row.field==='risk_tilt');
  const cues=context?.client_cues || [];
  const liquidity=cues.findLast(row=>row.value==='liquidity');
  const income=cues.findLast(row=>row.value==='income_goal');
  const protect=cues.findLast(row=>row.value==='capital_preservation');
  const riskChange=cues.findLast(row=>row.value==='risk_change' || (row.kind==='behavioral_hint' && row.dimension==='risk_tilt'));
  const leverage=cues.findLast(row=>row.value==='leverage');
  const pace=context?.team_declared?.find(row=>row.field==='adjustment_style') ||
    context?.tentative_preferences?.find(row=>row.field==='adjustment_style') ||
    cues.findLast(row=>row.value==='adjustment_pace' || (row.kind==='behavioral_hint' && row.dimension==='adjustment_style'));
  const constraints=context?.explicit_constraints || [];
  const nextTask = liquidity || income ? 'projection' : 'allocation';
  const emphasis = {conservative:'先看波动与本金承受能力，再看预期收益。',growth:'核对为争取收益而承担的波动是否在客户可接受范围内。',balanced:'把预期收益与波动放在一起，核对客户愿意承担的取舍。'};
  return <section aria-label="结合客户信息解读风险"><h4>这次风险计算怎么用</h4>
    <p>按当前持仓和草稿假设，组合年化波动率为 {pct(result.annual_volatility)}。这个指标描述组合收益的波动程度。</p>
    {!context ? <p>本次未取得可用的客户资料，适配性仍待核实。</p> : <>
      {risk && <p>已记录的风险倾向为{label(risk.value)}（依据「{risk.evidence_text}」）。{emphasis[risk.value]}</p>}
      {!risk && tentative && <p>目前只有暂定线索。{describeEvidence(tentative)}。请先与客户核实可接受的波动。</p>}
      {!risk && !tentative && <p>客户风险倾向尚待了解，可询问其对组合价值起伏的接受程度。</p>}
      {context.conflicts?.length>0 && <p>资料中有分歧，请在作出适配判断前核实相关原话。</p>}
      {liquidity && <p>客户提到「{liquidity.evidence_text}」。先确认用款时间和金额，再在 Goal projection 中试算相应提款。</p>}
      {income && <p>客户提到「{income.evidence_text}」。可核实需要的现金收入。年预期收益并非可提取的现金收入。</p>}
      {protect && <p>客户提到「{protect.evidence_text}」。还需要确认可接受的损失金额和期限。</p>}
      {riskChange && <p>最近提到「{riskChange.evidence_text}」。可核实这是短期行为还是长期风险倾向的变化。</p>}
      {leverage && <p>杠杆使用线索「{leverage.evidence_text}」。这次估算基于股票和债券权重，杠杆暴露需另行核实。</p>}
      {pace && <p>若继续试算调整方案，可一并核对调整节奏。依据「{pace.evidence_text || describeEvidence(pace)}」。</p>}
      {constraints.map(item=><p key={item.signal_id}>已记录约束「{item.evidence_text}」。这次股票和债券波动估算未检验该约束是否满足。</p>)}
    </>}
    <button type="button" onClick={()=>onFollowUp(nextTask)}>{nextTask==='projection'?'试算目标与提款':'查看同一客户的配置偏离'}</button>
  </section>;
}
