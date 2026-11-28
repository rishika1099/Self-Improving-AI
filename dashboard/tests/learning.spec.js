import {test,expect} from '@playwright/test';

test.beforeEach(async({page})=>{
  await page.goto('/');
  await page.getByRole('button',{name:'Portfolio Analysis',exact:true}).click();
});

test('ordinary actions update the visible score and change the supplemental calculation',async({page})=>{
  test.skip(!process.env.PORTFOLIO_BROWSER_FACTORY,'Runs against a separate fresh learning database');
  const score=page.getByRole('region',{name:'Client risk willingness'});
  const plan=page.getByRole('region',{name:'Allocation stress test'});
  await expect(score).toContainText('Starting at neutral');
  await page.getByLabel('Stock target (%)').fill('60');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(score).toContainText('48.1');
  await expect(score.locator('.risk-component')).toHaveCount(4);
  await score.getByText('View score drivers',{exact:true}).click();
  await expect(score.getByText('Net impact today',{exact:true})).toBeVisible();
  await score.getByText('View score drivers',{exact:true}).click();
  await page.getByLabel('Stock target (%)').fill('100');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(score).toContainText('52.0 / 100');
  await page.getByLabel('Stock target (%)').fill('60');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(score).toContainText('48.1 / 100');
  await expect(score).toContainText('1 evidence groups');
  await page.getByLabel('Analysis',{exact:true}).selectOption('holdings');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(plan).toContainText('Extra scenario · Lower stocks');
  await expect(plan).toContainText('$1,467.50');
  await plan.getByRole('spinbutton',{name:/Stock shock/}).fill('10');
  await expect(plan.getByRole('row',{name:/Lower stocks/})).toContainText('Loss reduction: $733.75');
  await score.getByRole('button',{name:'Adjust score'}).click();
  const slider=score.getByRole('slider');
  await slider.press('End');
  for(let i=0;i<25;i++) await slider.press('ArrowLeft');
  await score.getByRole('button',{name:'Save score'}).click();
  await expect(score).toContainText('Advisor set 75');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(plan).toContainText('Extra scenario · Higher stocks');
  await expect(plan).toContainText('$2,935');
  await page.reload();
  await page.getByRole('button',{name:'Portfolio Analysis',exact:true}).click();
  await expect(score).toContainText('Advisor set 75');
  await score.getByRole('button',{name:'Adjust score'}).click();
  await score.getByRole('button',{name:'Remove manual adjustment'}).click();
  await expect(score).toContainText('48.1');
  await expect(score).not.toContainText('Advisor set');
  await score.getByRole('button',{name:'Adjust score'}).click();
  await score.getByRole('slider').press('Home');
  await score.getByRole('slider').press('ArrowRight');
  const lock=score.getByRole('switch',{name:'Lock applied score'});
  await expect(lock).not.toBeChecked();
  await lock.press('Space');
  await expect(lock).toBeChecked();
  await score.getByRole('button',{name:'Save score'}).click();
  await expect(score.locator('.risk-value')).toContainText('1.0 / 100');
  await expect(score.getByText('Locked score',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Start a new analysis round'}).click();
  await page.getByLabel('Analysis',{exact:true}).selectOption('allocation');
  await page.getByLabel('Stock target (%)').fill('95');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(score).toContainText('2 evidence groups');
  await expect(score.locator('.risk-value')).toContainText('1.0 / 100');
  await expect(score.getByText('Locked score',{exact:true})).toBeVisible();
  await score.getByRole('button',{name:'Adjust score'}).click();
  await expect(lock).toBeChecked();
  await lock.click();
  await expect(lock).not.toBeChecked();
  await score.getByRole('button',{name:'Save score'}).click();
  await expect(score).toContainText('Learning continues from this starting point');
  await page.getByRole('button',{name:'Start a new analysis round'}).click();
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(score).toContainText('3 evidence groups');
  await expect(score).not.toContainText('1.0 / 100');
});

test('portfolio stress testing compares each scenario against a loss limit and recalculates locally',async({page})=>{
  const rows=[['current',.7986371379897785,23440],['baseline',.41,12033.5],['less_equity',.35,10272.5],['more_equity',.47,13794.5]];
  let runs=0;
  await page.route('**/api/portfolio/execute',route=>{
    runs++;
    return route.fulfill({json:{
      run_id:'example-'+runs,status:'success',mode:'explicit_tool_execution',
      result:{personalized_analysis:{score_used:54,score_status:'scored',focus:'compare',portfolio_value:146750,
        stock_shock_pct:20,bond_shock_pct:0,scenario_step_pp:6,target_source:'request_override',
        rows:rows.map(([label,stock_weight,loss])=>({label,stock_weight,stock_fall_usd:-loss,stock_rise_usd:loss}))}},
      learning_context_used:{status:'connected',context:{explicit_constraints:[
        {value:'max_loss_limit',evidence_text:'Confirm that the maximum loss is eleven thousand dollars'},
        {value:'max_loss_limit',scope:{account_id:'OTHER'},evidence_text:'A constraint for another account'},
      ]}},
    }});
  });
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  const panel=page.getByRole('region',{name:'Allocation stress test'});
  const target=panel.getByRole('row',{name:/Comparison target/});
  const less=panel.getByRole('row',{name:/Lower stocks/});
  const shock=panel.getByRole('spinbutton',{name:/Stock shock/});
  const ceiling=panel.getByRole('spinbutton',{name:/Loss limit/});
  await expect(ceiling).toHaveValue('');
  await panel.getByText('View recorded loss constraints (1)').click();
  await expect(panel.getByText(/maximum loss is eleven thousand dollars/)).toBeVisible();
  await expect(panel).not.toContainText('A constraint for another account');
  await ceiling.fill('11000');
  await expect(target).toContainText('Over by $1,033.50');
  await expect(less).toContainText('Within limit · $727.50');
  await expect(less).toContainText('Less upside: $1,761.00');
  await shock.fill('10');
  await expect(target).toContainText('-$6,016.75');
  await expect(target).toContainText('Within limit · $4,983.25');
  await ceiling.fill('6016.75');
  await expect(target).toContainText('At the limit');
  await ceiling.fill('6016.751');
  await expect(ceiling).toHaveAttribute('aria-invalid','true');
  await expect(target).not.toContainText('At the limit');
  await shock.fill('101');
  await expect(target).toHaveCount(0);
  await shock.fill('0');
  await expect(target).toContainText('$0.00');
  expect(runs).toBe(1);
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(shock).toHaveValue('20');
  await expect(ceiling).toHaveValue('');
});

test('pending score survives reload and switching clients clears the old draft',async({page})=>{
  const card=page.getByRole('region',{name:'Client risk willingness'});
  await page.route('**/api/portfolio/learning/CLI-002',route=>route.fulfill({json:{status:'module_busy',
    client_id:'CLI-002',context:null,events:[],last_confirmed_display:{score:64.2}}}));
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  await expect(card).toContainText('64.2 / 100 Last confirmed score');
  await expect(card.getByRole('button',{name:'Adjust score'})).toHaveCount(0);
  await page.reload();
  await page.getByRole('button',{name:'Portfolio Analysis',exact:true}).click();
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  await expect(card).toContainText('64.2 / 100 Last confirmed score');
  await page.getByLabel('Workflow').selectOption('agent');
  await page.getByLabel('Your question').fill('Only the current client needs tuition money.');
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-003');
  await expect(card).not.toContainText('64.2');
  await expect(page.getByLabel('Your question')).not.toHaveValue(/tuition/);
});

test('clear all records requires a second click and survives reload',async({page})=>{
  test.skip(!process.env.PORTFOLIO_BROWSER_FACTORY,'Runs against a separate learning database');
  const client=page.getByLabel('Client',{exact:true});
  await client.selectOption('CLI-004');
  const score=page.getByRole('region',{name:'Client risk willingness'});
  await page.getByLabel('Stock target (%)').fill('10');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(score).toContainText('1 evidence groups');
  await score.getByRole('button',{name:'Adjust score'}).click();
  await score.getByRole('slider').press('End');
  await score.getByRole('button',{name:'Save score'}).click();
  await expect(score).toContainText('100.0 / 100');
  await page.getByRole('tab',{name:/Operation history/}).click();
  const trigger=page.getByRole('button',{name:'Clear all records',exact:true});
  await trigger.click();
  const dialog=page.getByRole('dialog',{name:'Clear all learning records for this client?'});
  await expect(dialog).toContainText('Okafor Family (CLI-004)');
  await expect(dialog.getByRole('button',{name:'Cancel',exact:true})).toBeFocused();
  await dialog.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(score).toContainText('100.0 / 100');
  await trigger.click();
  await dialog.getByRole('button',{name:'Confirm clear',exact:true}).click();
  await expect(dialog).not.toBeVisible();
  await expect(score).toContainText('50.0 / 100');
  await expect(page.locator('.learning-event')).toHaveCount(0);
  await expect(page.locator('.portfolio-results')).toHaveCount(0);
  await page.reload();
  await page.getByRole('button',{name:'Portfolio Analysis',exact:true}).click();
  await client.selectOption('CLI-004');
  await expect(score).toContainText('50.0 / 100');
  await expect(page.locator('.learning-event')).toHaveCount(0);
  await score.getByRole('button',{name:'Adjust score'}).click();
  await score.getByRole('slider').press('End');
  await score.getByRole('button',{name:'Save score'}).click();
  await expect(score).toContainText('100.0 / 100');
});

test('operation history pages show score movements and keep withdrawals with their records',async({page})=>{
  test.skip(!process.env.PORTFOLIO_BROWSER_FACTORY,'Uses an isolated learning database');
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  for(const [event,target] of [['first',10],['same',10]]){
    const result=await page.request.post('/api/portfolio/execute',{data:{task:'allocation',client_id:'CLI-002',
      target_stock_weight:target/100,observation:{event_id:event,evidence_group_id:'same-round',
        parameter_sources:{target_stock_weight:'manual'}}}});
    expect(result.ok()).toBeTruthy();
  }
  for(let i=0;i<9;i++){
    const result=await page.request.post('/api/portfolio/execute',{data:{task:'holdings',client_id:'CLI-002',
      observation:{event_id:`position-${i}`}}});
    expect(result.ok()).toBeTruthy();
  }
  const retraction=await page.request.post('/api/portfolio/learning/CLI-002/retract/first');
  expect(retraction.ok()).toBeTruthy();
  await page.reload();
  await page.getByRole('button',{name:'Portfolio Analysis',exact:true}).click();
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  await page.getByRole('tab',{name:/Operation history/}).click();
  const pages=page.getByRole('navigation',{name:'Operation history pagination'});
  await expect(pages).toContainText('Page 1 / 2 · 11 operations');
  await expect(page.locator('.learning-event')).toHaveCount(10);
  await expect(page.locator('#learning-event-same .operation-score')).toContainText('0 points');
  await pages.getByRole('button',{name:'Next'}).click();
  await expect(pages).toContainText('Page 2 / 2');
  await expect(page.locator('.learning-event')).toHaveCount(1);
  const older=page.locator('#learning-event-first');
  await expect(older).toContainText('Withdrawn');
  const number=await older.locator('.learning-operation-number').textContent();
  expect(number).toMatch(/^UNI-[0-9]{6,}$/);
  await expect(older).toContainText('50.0 → 48.85 (-1.15 points)');
  await expect(older).toContainText('Withdrawal · Household applied score');
  await expect(older).toContainText('48.85 → 48.85 (0 points)');
  await pages.getByRole('button',{name:'Previous'}).click();
  await expect(pages).toContainText('Page 1 / 2');
  const lookup=page.getByLabel('Find by operation ID');
  await lookup.fill('UNI-oops');
  await lookup.press('Enter');
  await expect(lookup).toHaveAttribute('aria-invalid','true');
  await lookup.fill(number.replace(/^UNI-0*/,''));
  await lookup.press('Enter');
  await expect(older).toBeFocused();
  await expect(lookup).toHaveValue(number);
  await expect(pages).toContainText('Page 2 / 2');
});

test('captures defaults and manual changes and exports all revisions',async({page})=>{
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.locator('.portfolio-results')).toContainText('$146,750.00');
  await expect(page.getByRole('region',{name:'Client preference learning'})).toContainText('Operation saved. Waiting for the learning module.');
  await page.getByLabel('Stock target (%)').fill('60');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.locator('.portfolio-results')).toContainText('$146,750.00');
  const dl=page.waitForEvent('download');
  await page.getByRole('tab',{name:/Operation history/}).click();
  await page.getByRole('button',{name:'Export client records'}).click();
  expect((await dl).suggestedFilename()).toBe('portfolio-CLI-001-operations.json');
  await page.screenshot({path:'../artifacts/screenshots/learning-desktop.png',fullPage:true});
});

test('note stays with selected client and retraction is honestly pending',async({page})=>{
  const note=`UI-test-${Date.now()} The client is conservative.`;
  await page.getByLabel('Add a client note').fill(note);
  await page.getByRole('button',{name:'Save client note'}).click();
  const card=page.locator('.learning-event').filter({hasText:note});
  await expect(card).toBeVisible();
  await card.getByRole('button',{name:'Withdraw this record'}).click();
  await expect(card).toContainText('Withdrawal pending');
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  await expect(page.locator('.customer-learning')).not.toContainText(note);
  await page.getByLabel('Workflow').selectOption('agent');
  await expect(page.getByLabel('Client',{exact:true})).toHaveValue('CLI-002');
});

test('agent request includes client and origin without calling a live model',async({page})=>{
  let payload;
  await page.route('**/api/portfolio/agent',async route=>{
    payload=route.request().postDataJSON();
    await route.fulfill({json:{status:'success',response:'Transport test only',trace:[]}});
  });
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-003');
  await page.getByLabel('Workflow').selectOption('agent');
  await page.getByLabel('Your question').fill('Test-only request');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.locator('.portfolio-results')).toContainText('Transport test only');
  expect(payload.client_id).toBe('CLI-003');
  expect(payload.observation.text_source).toBe('manual');
});

test('Portfolio presents Markdown in English and keeps original-language evidence out of the screen',async({page})=>{
  const original='客户只想保住本金';
  let answer='## Allocation summary\n\n**Current holdings**\n\n- Review the stock allocation.';
  await page.route('**/api/portfolio/agent',route=>route.fulfill({json:{
    status:'success',mode:'adk_gemini',response:answer,trace:[{kind:'source',quote:original}],
  }}));
  await page.getByLabel('Workflow').selectOption('agent');
  await page.getByLabel('Your question').fill(original);
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  const result=page.locator('.portfolio-results');
  await expect(result.getByRole('heading',{name:'Allocation summary'})).toBeVisible();
  await expect(result.getByRole('listitem')).toHaveText('Review the stock allocation.');
  await result.getByText('Execution evidence',{exact:true}).click();
  await expect(result.locator('pre')).toContainText('Original-language text retained in the source record.');
  await expect(result).not.toContainText(original);
  answer=original;
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(result).toContainText('The model did not return an English answer');
  await expect(result).not.toContainText(original);
});

test('partial interpretation exposes the usable cue and actual next analysis scope',async({page})=>{
  const baseIndex={status:'scored',automatic:72,effective:72,groups:1,calibration:{chosen:null}};
  const event={kind:'event',event_id:'partial-a',delivery_status:'needs_review',
    event:{user_text:'Compound question',outcome:'success',entry_point:'agent',occurred_at:'2026-11-25T10:00:00Z'},
    update:{applied:['one'],extraction:{signals:[
      {signal_id:'one',kind:'client_cue',value:'liquidity',evidence_text:'The funds are needed next month.'},
      {signal_id:'two',kind:'uninterpreted_excerpt',evidence_text:'Verify the second account.'}]}}};
  await page.route('**/api/portfolio/learning/CLI-002',route=>route.fulfill({json:{
    client_id:'CLI-002',status:'connected',events:[event],context:{
      working_risk_index:baseIndex,scoped_risk_index:{...baseIndex,effective:20}}}}));
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  const card=page.getByRole('region',{name:'Client risk willingness'});
  await expect(card).toContainText('Test a lower stock allocation');
  await page.getByRole('tab',{name:/Operation history/}).click();
  const record=page.locator('.learning-event').filter({hasText:'Compound question'});
  await expect(record).toContainText('Near-term cash need');
  await expect(record).toContainText('This part still needs interpretation. Original text: Verify the second account.');
});

test('mobile customer records fit screen',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await expect(page.getByLabel('Add a client note')).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({path:'../artifacts/screenshots/learning-mobile.png',fullPage:true});
});

test('a failed refresh clears the old customer context',async({page})=>{
  const base='**/api/portfolio/learning/CLI-002';
  let fail=false;
  await page.route(base,route=>route.fulfill(fail?{status:503,json:{detail:'Test storage failure'}}:
    {json:{client_id:'CLI-002',status:'connected',pending_count:0,events:[],
      context:{summary:'Internal raw summary',team_declared:[{field:'risk_tilt',value:'conservative',
        evidence_text:'Old context must disappear'}],unknowns:[]}}}));
  await page.route('**/api/portfolio/learning/CLI-002/retry',route=>{fail=true;return route.fulfill({json:{status:'module_busy'}});});
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  await expect(page.locator('.customer-learning')).toContainText('Old context must disappear');
  await page.getByRole('tab',{name:/Operation history/}).click();
  await page.getByRole('button',{name:'Retry pending records'}).click();
  await expect(page.locator('.customer-learning')).toContainText('Could not load client records');
  await expect(page.locator('.customer-learning')).not.toContainText('Old context must disappear');
});

test('network retry reuses event id and preset market symbol stays default',async({page})=>{
  const payloads=[];
  await page.route('**/api/portfolio/execute',async route=>{
    payloads.push(route.request().postDataJSON());
    if(payloads.length===1)return route.abort('failed');
    return route.fulfill({json:{status:'success',mode:'explicit_tool_execution',trace:[],result:{source:'transport-test'}}});
  });
  await page.getByLabel('Analysis',{exact:true}).selectOption('quote');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('Could not reach');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.locator('.portfolio-results')).toContainText('transport-test');
  expect(payloads[0].observation.event_id).toBe(payloads[1].observation.event_id);
  expect(payloads[1].observation.parameter_sources.symbol).toBe('default');
  await page.getByLabel('Symbol',{exact:true}).fill('MSFT');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.locator('.portfolio-results')).toContainText('transport-test');
  expect(payloads[2].observation.parameter_sources.symbol).toBe('manual');
  expect(payloads[2].observation.event_id).not.toBe(payloads[1].observation.event_id);
});

test('structured context values cannot crash the learning panel',async({page})=>{
  await page.route('**/api/portfolio/learning/CLI-002',route=>route.fulfill({json:{client_id:'CLI-002',
    status:'connected',pending_count:0,events:[],context:{summary:'Structured context test',
    team_declared:[{field:'risk_tilt',value:{status:'unknown'}}],unknowns:['constructor']}}}));
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  await expect(page.locator('.customer-learning')).toContainText('constructor');
  await expect(page.locator('.customer-learning')).toContainText('unknown');
});

test('audit explains soft conflicts, keeps raw payload in a download, and opens older evidence',async({page})=>{
  const quote='Preserving principal is the priority for these funds.';
  const eventId='older-note';
  const event={kind:'event',event_id:eventId,delivery_status:'applied',operation_number:'UNI-000034',
    event:{user_text:quote,occurred_at:'2026-10-12T10:00:00Z',entry_point:'advisor_note'}};
  const context={summary:'Internal raw summary',
    conflicts:[{kind:'exploration_differs_from_declaration',dimension:'risk_tilt',scope:null,evidence_ids:['note-ref','trial-ref']}],
    risk_source_scores:{operations:[],notes:[{scope:null,score:null,status:'conflict',evidence:[],
      conflicts:['exploration_differs_from_declaration']}]}};
  await page.route(/\/api\/portfolio\/learning\/CLI-002(?:\?.*)?$/,route=>{
    const target=new URL(route.request().url()).searchParams.get('event_id');
    return route.fulfill({json:{status:'connected',context,events:target?[event]:[],total_events:12,
      source_operation_numbers:{[eventId]:'UNI-000034',trial:'UNI-000093'},
      ...(target ? {located_event:true} : {}),pagination:{page:target?2:1,pages:2}, conflict_sources:{signals:{
        'note-ref':{event_id:eventId,evidence_text:quote,kind:'explicit_statement',entry_point:'advisor_note'},
        'trial-ref':{event_id:'trial',kind:'allocation_exploration',entry_point:'direct',basis:{target_stock_weight:.95,current_stock_weight:.8}},
      }},
    }});
  });
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  await page.getByRole('tab',{name:'Scoring evidence'}).click();
  const audit=page.getByRole('tabpanel',{name:'Scoring evidence'});
  await expect(audit).toContainText('Exploration differs from a recorded statement');
  await expect(audit).toContainText('Stock weight tested: 80% → 95%');
  await expect(audit).toContainText('Exploratory cues need review. Prototype score withheld.');
  await expect(audit).not.toContainText('Internal raw summary');
  await expect(audit.locator('pre')).toHaveCount(0);
  const download=page.waitForEvent('download');
  await audit.getByRole('button',{name:'Download scoring data (JSON)'}).click();
  expect((await download).suggestedFilename()).toBe('portfolio-CLI-002-evidence.json');
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await audit.locator('.audit-source').filter({hasText:quote}).getByRole('link',{name:/View original record.*UNI-000034/}).click();
  await expect(page.locator('#learning-event-older-note')).toBeFocused();
  await expect(page.locator('#learning-event-older-note .learning-operation-number')).toHaveText('UNI-000034');
  await expect(page.getByRole('navigation',{name:'Operation history pagination'})).toContainText('Page 2 / 2');
});

test('a rate-limited note stays visible while waiting to retry',async({page})=>{
  const retryAt='2030-01-01T12:35:00Z';
  const userText='The client is generally conservative.';
  await page.route('**/api/portfolio/learning/CLI-002',route=>route.fulfill({json:{
    client_id:'CLI-002',status:'processing_pending',pending_count:1,
    events:[{kind:'event',event_id:'rate-limited-note',delivery_status:'pending',
      event:{user_text:userText,occurred_at:'2030-01-01T12:34:00Z'},
      update:{issues:[{code:'model_rate_limited',retry_at:retryAt}]}}],
  }}));
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  const learning=page.getByRole('region',{name:'Client preference learning'});
  const retryTime=await page.evaluate(value=>new Date(value).toLocaleTimeString(),retryAt);
  await expect(learning.getByText('Gemini reached its request limit.',{exact:false})).toContainText(retryTime);
  await learning.getByRole('tab',{name:/Operation history/}).click();
  await expect(learning.locator('.learning-event')).toContainText(userText);
  await expect(learning.getByRole('button',{name:'Retry pending records'})).toBeEnabled();
});


test('risk results explain the calculation using the saved customer need', async({page})=>{
  await page.route('**/api/portfolio/execute',route=>route.fulfill({json:{
    status:'success',mode:'explicit_tool_execution',result:{annual_expected_return:.05,annual_volatility:.14546},
    learning_context_used:{status:'connected',context:{client_cues:[{value:'liquidity',evidence_text:'Tuition is due next month.'}]}}
  }}));
  await page.getByLabel('Analysis',{exact:true}).selectOption('risk');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  const panel=page.getByRole('region',{name:'Risk interpretation using client context'});
  await expect(panel).toContainText('14.5%');
  await expect(panel).toContainText('Tuition is due next month.');
  await expect(panel).toContainText('test the withdrawals');
});


test('risk willingness discloses scored evidence and an independent scenario',async({page})=>{
  const context={summary:'test profile',risk_preference_scores:[{scope:null,score:10.526,status:'scored',
    prior:{conservative:1/3,balanced:1/3,growth:1/3},support:{conservative:8/9.5,balanced:1/9.5,growth:.5/9.5},
    evidence:[{event_id:'test-source',revision:2,source:'The client accepts lower returns for less volatility.',strength:'explicit',
      criterion:'risk_return_tradeoff',multipliers:[8,1,.5],running_score:10.526}]}]};
  await page.route('**/api/portfolio/learning/CLI-002',route=>route.fulfill({json:{
    client_id:'CLI-002',status:'connected',pending_count:0,events:[{kind:'event',event_id:'test-source',
      delivery_status:'applied',event:{user_text:'The client accepts lower returns for less volatility.',occurred_at:'2026-11-25T10:00:00Z'}}],context}}));
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  await page.getByRole('tab',{name:'Client overview'}).press('End');
  const score=page.getByRole('region',{name:'Client risk willingness evidence'});
  await expect(score).toContainText('10.5 / 100');
  await score.getByText('View criteria and calculation').click();
  await expect(score).toContainText('8 / 1 / 0.5');
  await score.getByRole('link',{name:'View original record'}).click();
  await expect(page.locator('#learning-event-test-source')).toBeFocused();
  await expect(page.getByRole('tab',{name:/Operation history/})).toHaveAttribute('aria-selected','true');
  await page.getByLabel('Analysis',{exact:true}).selectOption('risk');
  await page.getByLabel('Stock target (%)').fill('60');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.locator('.risk-comparison')).toContainText('Requested allocation');
  await expect(page.locator('.risk-comparison')).toContainText('Trial portfolio');
});
