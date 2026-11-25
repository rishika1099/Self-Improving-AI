import {test,expect} from '@playwright/test';

test.beforeEach(async({page})=>{
  await page.goto('/');
  await page.getByRole('button',{name:'Portfolio Analysis',exact:true}).click();
});

test('ordinary actions update the visible score and change the supplemental calculation',async({page})=>{
  test.skip(!process.env.PORTFOLIO_BROWSER_FACTORY,'Runs against a separate fresh learning database');
  const score=page.getByRole('region',{name:'客户激进程度'});
  const plan=page.getByRole('region',{name:'根据评分加算的情景'});
  await expect(score).toContainText('暂无方向证据');
  await page.getByLabel('Stock target (%)').fill('60');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(score).toContainText('43.4');
  await page.getByLabel('Analysis',{exact:true}).selectOption('holdings');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(plan).toContainText('加算 · 减少股票');
  await expect(plan).toContainText('$1,761');
  await score.getByRole('button',{name:'调整评分'}).click();
  const slider=score.getByRole('slider');
  await slider.press('End');
  for(let i=0;i<25;i++) await slider.press('ArrowLeft');
  await score.getByRole('button',{name:'保存评分'}).click();
  await expect(score).toContainText('顾问上次调至 75');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(plan).toContainText('加算 · 增加股票');
  await expect(plan).toContainText('$2,935');
  await page.reload();
  await page.getByRole('button',{name:'Portfolio Analysis',exact:true}).click();
  await expect(score).toContainText('顾问上次调至 75');
  await score.getByRole('button',{name:'调整评分'}).click();
  await score.getByRole('button',{name:'取消手动校准'}).click();
  await expect(score).toContainText('43.4');
  await expect(score).not.toContainText('顾问上次调至');
});

test('captures defaults and manual changes and exports all revisions',async({page})=>{
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.locator('.portfolio-results')).toContainText('$146,750.00');
  await expect(page.getByRole('region',{name:'客户偏好学习'})).toContainText('操作已记录，等待学习模块');
  await page.getByLabel('Stock target (%)').fill('60');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.locator('.portfolio-results')).toContainText('$146,750.00');
  const dl=page.waitForEvent('download');
  await page.getByRole('button',{name:'导出当前客户记录'}).click();
  expect((await dl).suggestedFilename()).toBe('portfolio-CLI-001-operations.json');
  await page.screenshot({path:'../artifacts/screenshots/learning-desktop.png',fullPage:true});
});

test('note stays with selected client and retraction is honestly pending',async({page})=>{
  const note=`UI-test-${Date.now()} 该客户比较保守`;
  await page.getByLabel('补充客户说明').fill(note);
  await page.getByRole('button',{name:'保存客户说明'}).click();
  const card=page.locator('.learning-event').filter({hasText:note});
  await expect(card).toBeVisible();
  await card.getByRole('button',{name:'撤回这条记录'}).click();
  await expect(card).toContainText('撤回待处理');
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

test('mobile customer records fit screen',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await expect(page.getByLabel('补充客户说明')).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({path:'../artifacts/screenshots/learning-mobile.png',fullPage:true});
});

test('a failed refresh clears the old customer context',async({page})=>{
  const base='**/api/portfolio/learning/CLI-002';
  let fail=false;
  await page.route(base,route=>route.fulfill(fail?{status:503,json:{detail:'Test storage failure'}}:
    {json:{client_id:'CLI-002',status:'connected',pending_count:0,events:[],
      context:{summary:'Old context must disappear',team_declared:[],unknowns:[]}}}));
  await page.route('**/api/portfolio/learning/CLI-002/retry',route=>{fail=true;return route.fulfill({json:{status:'module_busy'}});});
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  await expect(page.locator('.customer-learning')).toContainText('Old context must disappear');
  await page.getByRole('button',{name:'重试待处理记录'}).click();
  await expect(page.locator('.customer-learning')).toContainText('客户记录读取失败');
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
  await expect(page.locator('.customer-learning')).toContainText('Structured context test');
  await expect(page.locator('.customer-learning')).toContainText('constructor');
  await expect(page.locator('.customer-learning')).toContainText('unknown');
});

test('a rate-limited note stays visible while waiting to retry',async({page})=>{
  const retryAt='2030-01-01T12:35:00Z';
  const userText='这个客户整体比较保守。';
  await page.route('**/api/portfolio/learning/CLI-002',route=>route.fulfill({json:{
    client_id:'CLI-002',status:'processing_pending',pending_count:1,
    events:[{kind:'event',event_id:'rate-limited-note',delivery_status:'pending',
      event:{user_text:userText,occurred_at:'2030-01-01T12:34:00Z'},
      update:{issues:[{code:'model_rate_limited',retry_at:retryAt}]}}],
  }}));
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  const learning=page.getByRole('region',{name:'客户偏好学习'});
  const retryTime=await page.evaluate(value=>new Date(value).toLocaleTimeString(),retryAt);
  await expect(learning.getByText('Gemini 请求已达限额。',{exact:false})).toContainText(retryTime);
  await expect(learning.locator('.learning-event')).toContainText(userText);
  await expect(learning.getByRole('button',{name:'重试待处理记录'})).toBeEnabled();
});


test('risk results explain the calculation using the saved customer need', async({page})=>{
  await page.route('**/api/portfolio/execute',route=>route.fulfill({json:{
    status:'success',mode:'explicit_tool_execution',result:{annual_expected_return:.05,annual_volatility:.14546},
    learning_context_used:{status:'connected',context:{client_cues:[{value:'liquidity',evidence_text:'下个月有学费支出'}]}}
  }}));
  await page.getByLabel('Analysis',{exact:true}).selectOption('risk');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  const panel=page.getByRole('region',{name:'结合客户信息解读风险'});
  await expect(panel).toContainText('14.5%');
  await expect(panel).toContainText('下个月有学费支出');
  await expect(panel).toContainText('试算相应提款');
});


test('risk willingness discloses scored evidence and an independent scenario',async({page})=>{
  const context={summary:'test profile',risk_preference_scores:[{scope:null,score:10.526,status:'scored',
    prior:{conservative:1/3,balanced:1/3,growth:1/3},support:{conservative:8/9.5,balanced:1/9.5,growth:.5/9.5},
    evidence:[{event_id:'test-source',revision:2,source:'客户愿意少赚来减少波动',strength:'explicit',
      criterion:'risk_return_tradeoff',multipliers:[8,1,.5],running_score:10.526}]}]};
  await page.route('**/api/portfolio/learning/CLI-002',route=>route.fulfill({json:{
    client_id:'CLI-002',status:'connected',pending_count:0,events:[],context}}));
  await page.getByLabel('Client',{exact:true}).selectOption('CLI-002');
  const score=page.getByRole('region',{name:'客户风险意愿分'});
  await expect(score).toContainText('10.5 / 100');
  await score.getByText('查看判据和逐条复算').click();
  await expect(score).toContainText('8 / 1 / 0.5');
  await page.getByLabel('Analysis',{exact:true}).selectOption('risk');
  await page.getByLabel('Stock target (%)').fill('60');
  await page.getByRole('button',{name:'Run analysis',exact:true}).click();
  await expect(page.locator('.risk-comparison')).toContainText('本次指定情景');
  await expect(page.locator('.risk-comparison')).toContainText('试算组合');
});
