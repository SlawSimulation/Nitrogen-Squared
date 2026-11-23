// GPL-3.0-or-later. Interactions against the real frontend with synthetic backend state.
import {test,expect} from '@playwright/test';
for(const size of [{width:760,height:500},{width:960,height:620},{width:1100,height:700},{width:1440,height:900}]){
  test(`navigation and settings fit ${size.width}×${size.height}`,async({page})=>{
    await page.setViewportSize(size);const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto('/');await expect(page.getByRole('button',{name:'Choose game disc',exact:true})).toBeVisible();
    for(const name of ['Home',"What's New",'Mods','Soundtrack','Settings']){
      await page.getByRole('button',{name,exact:true}).click();
      await expect(page.getByRole('button',{name,exact:true})).toHaveAttribute('aria-current','page');
      await expect.poll(()=>page.locator('#view').evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBeTruthy();
    }
    for(const name of ['Display','Audio','Controls','Storage','About']){
      await page.getByRole('tab',{name,exact:true}).check();
      await expect(page.locator('[role="tabpanel"]')).toHaveAttribute('id','panel-'+name);
      expect(await page.locator('#view').evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBeTruthy();
    }
    expect(errors).toEqual([]);
  });
}
test('tasks survive page changes and preferences commit through the bridge',async({page})=>{
  await page.goto('/');await page.getByRole('button',{name:'Choose game disc',exact:true}).click();
  await page.getByRole('button',{name:'Prepare & play',exact:true}).click();
  await expect(page.locator('#task-pill')).toContainText('42%');
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  await expect(page.getByRole('combobox',{name:'Window mode',exact:true})).toBeDisabled();
  await expect(page.locator('#task-pill')).toContainText('42%');
  await page.locator('#task-pill').click();await page.getByRole('button',{name:'Stop',exact:true}).click();
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByRole('combobox',{name:'Window mode',exact:true}).selectOption('2');
  await expect.poll(()=>page.evaluate(()=>window.__N2_MOCK__.state.options.video_mode)).toBe(2);
  await page.getByRole('tab',{name:'Audio',exact:true}).check();
  await page.getByRole('slider',{name:'Master volume',exact:true}).focus();
  await page.keyboard.press('Home');
  for(let i=0;i<26;i++)await page.keyboard.press('ArrowRight');
  await expect.poll(()=>page.evaluate(()=>window.__N2_MOCK__.state.options.master_volume)).toBe(26);
});
test('remote release text is literal and reduced-motion navigation remains scrollable',async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});await page.goto('/');
  await page.evaluate(()=>{const {state,emit}=window.__N2_MOCK__;state.releases[0].body='<img src=x onerror="window.bad=true">';state.revision++;emit('state',structuredClone(state));});
  await page.getByRole('button',{name:"What's New",exact:true}).click();
  await expect(page.locator('.notes-body')).toHaveText('<img src=x onerror="window.bad=true">');
  expect(await page.evaluate(()=>window.bad)).toBeUndefined();
  await page.getByRole('button',{name:'Settings',exact:true}).click();await page.getByRole('tab',{name:'Controls',exact:true}).check();
  expect(await page.locator('#view').evaluate(el=>getComputedStyle(el).overflowY)).toBe('auto');
});
test('build details and elapsed time stay visible and unknown progress is indeterminate',async({page})=>{
  await page.setViewportSize({width:760,height:500});await page.goto('/');
  await expect(page.getByRole('button',{name:'Choose game disc',exact:true})).toBeVisible();
  await page.evaluate(()=>{const {state,emit}=window.__N2_MOCK__;Object.assign(state,{busy:true,phase:'setup',title:'Configuring game',detail:'Checking graphics and audio dependencies…',percent:null,elapsed:65,workers:'12 of 16 logical CPUs'});state.revision++;emit('state',structuredClone(state));});
  await expect(page.locator('#task-msg')).toHaveText('Configuring game');
  await expect(page.locator('#task-detail')).toHaveText('Checking graphics and audio dependencies…');
  await expect(page.locator('#task-bar')).not.toHaveAttribute('value');
  await expect(page.locator('#task-pct')).toHaveText('');
  await expect(page.locator('#task-meta')).toContainText('12 of 16 logical CPUs');
  await expect(page.locator('#task-meta')).toContainText('1:05 elapsed');
  await expect(page.locator('#task-meta')).not.toContainText('1:05 elapsed',{timeout:4000});
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  await page.locator('#task-pill').click();
  await expect(page.locator('#task-detail')).toHaveText('Checking graphics and audio dependencies…');
  expect(await page.locator('#view').evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBeTruthy();
  await page.getByRole('button',{name:'Stop',exact:true}).click();
  await expect(page.locator('#task')).toBeHidden();
});
test('mods page lists package details and drives install, enable and remove through the bridge',async({page})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setViewportSize({width:760,height:500});await page.goto('/');
  await page.getByRole('button',{name:'Mods',exact:true}).click();
  const ready=page.locator('.mod[data-id="example.ready"]'),missing=page.locator('.mod[data-id="example.missing"]');
  await expect(ready.locator('h2')).toHaveText('Ready Example');
  await expect(ready).toContainText('1.0.0');await expect(ready).toContainText('Synthetic author');
  await expect(ready).toContainText('GPL-3.0-or-later');await expect(ready).toContainText('nitrogen.mod-probe');
  await expect(ready.locator('.mod-status')).toHaveText('Compatible');
  await expect(missing.locator('.mod-status')).toHaveText('Missing plugins');
  await expect(missing.getByRole('switch',{name:'Enable Missing Plugin Example'})).toBeDisabled();
  await ready.getByRole('switch',{name:'Enable Ready Example'}).check();
  await expect.poll(()=>page.evaluate(()=>window.__N2_MOCK__.mods.packages[0].enabled)).toBe(true);
  await ready.getByRole('switch',{name:'Enable Ready Example'}).uncheck();
  await expect.poll(()=>page.evaluate(()=>window.__N2_MOCK__.mods.packages[0].enabled)).toBe(false);
  await page.getByRole('button',{name:'Install package…',exact:true}).click();
  await expect(page.locator('.mod[data-id="example.installed"]')).toBeVisible();
  await missing.getByRole('button',{name:'Remove'}).click();
  await page.getByRole('dialog').getByRole('button',{name:'Remove',exact:true}).click();
  await expect(missing).toHaveCount(0);
  expect(await page.evaluate(()=>window.__N2_MOCK__.mods.packages.map(p=>p.id))).toEqual(['example.ready','example.installed']);
  expect(await page.locator('#view').evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBeTruthy();
  expect(errors).toEqual([]);
});
test('mods are locked while the game is busy and an empty store explains how to install',async({page})=>{
  await page.goto('/');
  await page.evaluate(()=>{const {state,emit,mods}=window.__N2_MOCK__;mods.packages.length=0;Object.assign(state,{busy:true,phase:'running',title:'Game running'});state.revision++;emit('state',structuredClone(state));});
  await page.getByRole('button',{name:'Mods',exact:true}).click();
  await expect(page.getByRole('heading',{name:'No mods installed'})).toBeVisible();
  await expect(page.getByRole('button',{name:'Install package…',exact:true})).toBeDisabled();
});
test('soundtrack page previews, replaces, reorders, restores and exports through the bridge',async({page})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setViewportSize({width:760,height:500});await page.goto('/');
  await page.getByRole('button',{name:'Soundtrack',exact:true}).click();
  const rows=page.locator('.st-row');await expect(rows).toHaveCount(10);
  await expect(rows.nth(0)).toContainText('Original track 02');await expect(rows.nth(9)).toContainText('Original track 11');
  await expect(rows.nth(0).locator('.st-state')).toHaveText('Original');
  await expect(page.getByRole('button',{name:'Export package',exact:true})).toBeDisabled();
  await expect(page.getByRole('button',{name:'Move track 02 up'})).toBeDisabled();
  await expect(page.getByRole('button',{name:'Move track 11 down'})).toBeDisabled();
  await page.getByRole('button',{name:'Preview track 02'}).click();
  await expect.poll(()=>page.evaluate(()=>window.__N2_MOCK__.soundtrack.previews)).toEqual([2]);
  await page.getByRole('button',{name:'Preview playlist',exact:true}).click();
  await expect.poll(()=>page.evaluate(()=>window.__N2_MOCK__.soundtrack.previews)).toEqual([2,null]);
  await rows.nth(1).getByRole('button',{name:'Replace…'}).click();
  await expect(rows.nth(1)).toContainText('synthetic-tone.wav');await expect(rows.nth(1).locator('.st-state')).toHaveText('Replacement');
  await expect(rows.nth(1)).toContainText('0:40');
  await page.getByRole('button',{name:'Move track 03 up'}).click();
  await expect(rows.nth(0)).toContainText('synthetic-tone.wav');
  await expect(rows.nth(1)).toContainText('Original track 02');await expect(rows.nth(1).locator('.st-state')).toHaveText('Moved');
  await rows.nth(1).getByRole('button',{name:'Restore'}).click();
  await expect(rows.nth(1).locator('.st-state')).toHaveText('Original');
  await page.getByRole('textbox',{name:'Package id'}).fill('tester.tones');
  await page.getByRole('button',{name:'Export and install',exact:true}).click();
  await expect.poll(()=>page.evaluate(()=>window.__N2_MOCK__.soundtrack.exports)).toEqual(['tester.tones-1.0.0.psxmod']);
  await page.getByRole('button',{name:'Mods',exact:true}).click();
  await expect(page.locator('.mod[data-id="tester.tones"]')).toContainText('nitrogen.soundtrack');
  expect(await page.locator('#view').evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBeTruthy();
  expect(errors).toEqual([]);
});
test('soundtrack editing is locked while busy but preview remains available',async({page})=>{
  await page.goto('/');
  await page.evaluate(()=>{const {state,emit}=window.__N2_MOCK__;Object.assign(state,{busy:true,phase:'running',title:'Game running'});state.revision++;emit('state',structuredClone(state));});
  await page.getByRole('button',{name:'Soundtrack',exact:true}).click();
  await expect(page.locator('.st-row')).toHaveCount(10);
  await expect(page.locator('.st-row').first().getByRole('button',{name:'Replace…'})).toBeDisabled();
  await expect(page.getByRole('button',{name:'Restore all',exact:true})).toBeDisabled();
  await expect(page.getByRole('button',{name:'Preview track 02'})).toBeEnabled();
});
