import React, {useEffect, useRef, useState} from 'react';

const show = score => Number.isFinite(score) ? score.toFixed(1) : '—';

export function WorkingRisk({context, status, disabled, onSave, latest, error, onRefresh}) {
  const [editing,setEditing]=useState(false);
  const [draft,setDraft]=useState(50);
  const [basis,setBasis]=useState(null);
  const index=context?.working_risk_index;
  const seen=useRef(null);
  const [movement,setMovement]=useState(null);
  useEffect(()=>{
    if(!index)return;
    if(seen.current && index.automatic!==seen.current.value)
      setMovement({before:seen.current.value,after:index.automatic});
    seen.current={value:index.automatic};
  },[index]);
  if (!index) return <section className="working-risk" aria-label="客户激进程度"><div><h3>客户激进程度</h3>
    <p>{error || (['module_busy','processing_pending'].includes(status)?'正在处理客户操作，评分待更新。':
      status?'当前无法取得已核实的评分。':'正在读取客户评分…')}</p></div></section>;
  const adjusted=index.calibration.chosen!=null;
  const effective=index.effective;
  function edit() {
    setDraft(Math.round(effective ?? 50));
    setBasis({revision:index.calibration.revision,profile_version:context.profile_version});
    setEditing(true);
  }
  async function save(score) {
    const saved=await onSave('/risk-calibration',{score,...basis});
    setEditing(false);
    if(!saved) onRefresh();
  }
  return <section className="working-risk" aria-label="客户激进程度">
    <div><h3>客户激进程度</h3><p><strong>{show(effective)}</strong> / 100　{adjusted?'顾问校准后持续学习':'当前采用分'}</p>
      <meter aria-label="当前采用分" min="0" max="100" value={effective??50} hidden={effective==null}/>
      <p>0 偏向减少波动 · 50 居中 · 100 偏向争取上行</p></div>
    <div><p>日常操作自动分 {show(index.automatic)} · {index.groups} 组有效依据
      {adjusted && ' · 顾问上次调至 '+index.calibration.chosen+' 分'}</p>
      {index.note_score!=null && <p>{index.note_legacy?'旧客户说明仅供复核，暂未参与采用分。':
        '补充说明分 '+show(index.note_score)+'，占校准前采用分的 15%。'}</p>}
      {context.scoped_risk_index && <p>当前账户或本轮另有适用分 {show(context.scoped_risk_index.effective)}。</p>}
      {index.status==='conflict' ? <p>风险证据有分歧，{adjusted?'暂用顾问设定的分数。':'暂用双向比较。'}</p> : index.legacy_review ?
        <p>自动分含待核对的旧判断，分析暂展示双向情景。</p> :
        effective==null ? <p>暂无方向证据。首次有方向的试算或提问将从中性起点更新。</p> :
        <p>下次持仓、配置或风险分析将{effective<45?'加算降低股票敞口的情景':effective>55?'加算提高股票敞口的情景':'加算提高和降低股票敞口两个情景'}。</p>}
      {latest && <p>最近操作 · {latest.event.user_text || latest.event.task} · {latest.delivery_status==='pending'?'待处理':
        latest.delivery_status==='needs_review'?'部分内容待核对':'已处理'}</p>}
      {movement && <p>最近观察到的自动分变化 {show(movement.before)} → {show(movement.after)}</p>}
      {error && <p>{error}</p>}
      {!editing ? <button type="button" disabled={disabled} onClick={edit}>调整评分</button> :
        <div className="risk-calibration"><label>调整后的客户整体分 <output>{draft}</output>
          <input aria-label="调整后的客户整体分" type="range" min="0" max="100" value={draft}
            onChange={e=>setDraft(Number(e.target.value))}/></label>
          <p>日常操作占主导。保存后，采用分会跟随校准前评分的变化，在 0–100 之间更新。</p>
          <button type="button" disabled={disabled} onClick={()=>save(draft)}>保存评分</button>
          {adjusted&&<button type="button" disabled={disabled} onClick={()=>save(null)}>取消手动校准</button>}
          <button type="button" disabled={disabled} onClick={()=>setEditing(false)}>取消</button>
        </div>}
    </div></section>;
}

const rowNames={current:'当前持仓',baseline:'本次对照配置',less_equity:'加算 · 减少股票',more_equity:'加算 · 增加股票'};
const usd=n=>n.toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2});

export function ScenarioCheck({analysis}) {
  if(!analysis) return null;
  const origin=analysis.advisor_adjusted?'校准后采用分':'采用分';
  const reach=analysis.scope?'当前适用范围':'客户整体';
  return <section className="scenario-check" aria-label="根据评分加算的情景"><h3>根据评分加算的情景</h3>
    <p>运行时使用的{reach}{origin} {show(analysis.score_used)} · {analysis.focus==='downside'?'优先检查降低敞口能少承担多少损失':
      analysis.focus==='upside'?'优先检查增加敞口的上行空间及额外损失':'同时检查增加和降低敞口的取舍'}。</p>
    <p>以{analysis.target_source==='request_override'?'本次指定目标':'客户已存草稿'}为对照，加算幅度为 {analysis.scenario_step_pp} 个百分点股票，上下限为 0% 和 100%。</p>
    <div className="portfolio-table"><table><thead><tr><th>组合</th><th>股票占比</th><th>股票跌 20%</th><th>股票涨 20%</th></tr></thead>
      <tbody>{analysis.rows.map(row=><tr key={row.label}><th>{rowNames[row.label]}</th><td>{(row.stock_weight*100).toFixed(1)}%</td>
        <td>{usd(row.stock_fall_usd)}</td><td>{usd(row.stock_rise_usd)}</td></tr>)}</tbody></table></div>
    {analysis.rows.length===2 && <p>本次目标已到所选方向的边界，无法继续增减股票敞口。</p>}
    {analysis.rows.slice(2).map(row=><p key={row.label}>{row.label==='less_equity'?'降低':'提高'}股票敞口后，
      相对本次对照配置，在下跌情景中{row.label==='less_equity'?'少损失':'多损失'} {usd(Math.abs(row.downside_change_usd))}，
      在上涨情景中{row.label==='less_equity'?'少获得':'多获得'} {usd(Math.abs(row.upside_change_usd))}。</p>)}
    <p>债券价格保持不变，金额为演示持仓在该情景下的变化。当前目标仍由本次输入或客户草稿决定。</p>
  </section>;
}
