import {test,expect} from '@playwright/test';

test.beforeEach(async({page})=>{
  await page.goto('/');
  await page.getByRole('button',{name:'Portfolio Analysis',exact:true}).click();
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
