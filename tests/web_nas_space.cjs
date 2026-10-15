const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {value:'', disabled:false, _html:'', _text:'',
      get innerHTML() {return this._html;}, set innerHTML(value) {this._html=value;this._text='';},
      get textContent() {return this._text;}, set textContent(value) {this._text=value;this._html='';},
      classList:{toggle(name,on) {on ? classes.add(name) : classes.delete(name);},contains:name=>classes.has(name)}});
  }
  return elements.get(id);
}
const context=vm.createContext({window:{},document:{getElementById:element}});
vm.runInContext(fs.readFileSync('uav-web/frontend/nas-settings.js','utf8'),context);
const nas=context.window.NasSettings;
const quota={enabled:true,success:true,space:{total_bytes:1e12,free_bytes:4e11,available_bytes:3e11},checked_at:1700000000};
async function run() {
  nas.renderSpace(quota,'nas-space-queue');
  let html=element('nas-space-queue-values').innerHTML;
  assert.match(html,/總容量.*1\.00 TB/);
  assert.match(html,/剩餘空間.*400\.00 GB/);
  assert.match(html,/已使用 600\.00 GB（60\.0%）/);
  assert.match(html,/此帳號可用空間：300\.00 GB/);
  assert.match(html,/<progress[^>]*value="60\.0"/);
  nas.renderSpace({enabled:true,space:{total_bytes:1e12,free_bytes:0,available_bytes:0}},'nas-space-queue');
  assert.match(element('nas-space-queue-values').innerHTML,/剩餘空間.*0 B/);
  nas.renderSpace({enabled:true,space:{total_bytes:0,free_bytes:0,available_bytes:0}},'nas-space-queue');
  assert.equal(element('nas-space-queue-values').innerHTML,'');
  assert(element('nas-space-queue').classList.contains('hidden'));
  nas.renderSpace({enabled:true,space_error:'<img src=x onerror=alert(1)>'},'nas-space-queue');
  assert.equal(element('nas-space-queue-values').innerHTML,'');
  assert.equal(element('nas-space-queue-values').textContent,'');
  assert(element('nas-space-queue').classList.contains('hidden'));
  nas.renderSpace({enabled:false,space:null},'nas-space-queue');
  assert(element('nas-space-queue').classList.contains('hidden'));
  let count=0,release;
  context.fetch=(path,options)=>{
    assert.equal(path,'/api/download/action');
    assert.deepEqual(JSON.parse(options.body),{action:'nas_space'});
    count++;
    return new Promise(resolve=>{release=resolve;});
  };
  const first=nas.refreshSpace();
  await nas.refreshSpace();
  assert.equal(count,1,'Overlapping capacity refreshes must not create another SMB connection');
  release({ok:true,json:async()=>quota});
  await first;
  assert.match(element('nas-space-queue-values').innerHTML,/1\.00 TB/);
  assert.match(element('nas-space-settings-values').innerHTML,/1\.00 TB/);
  nas.load({enabled:true,mode:'smb',host:'nas.local',share:'video',username:'user'});
  const tested={...quota,space:{total_bytes:2e12,free_bytes:1e12,available_bytes:1e12}};
  context.fetch=async(path,options)=>{
    assert.equal(JSON.parse(options.body).test_nas,true);
    return {ok:true,json:async()=>tested};
  };
  await nas.testConnection();
  assert.match(element('nas-space-settings-values').innerHTML,/2\.00 TB/);
  assert.equal(element('nas-space-settings-label').textContent,'本次連線測試容量');
  context.fetch=async()=>({ok:true,json:async()=>quota});
  await nas.refreshSpace();
  assert.match(element('nas-space-queue-values').innerHTML,/1\.00 TB/);
  assert.match(element('nas-space-settings-values').innerHTML,/2\.00 TB/,'Refreshing saved NAS must not replace an unsaved connection test');
  context.fetch=async()=>({ok:false,json:async()=>({error:'容量連線失敗'})});
  await nas.refreshSpace();
  assert.equal(element('nas-space-queue-values').innerHTML,'');
  assert.equal(element('nas-space-queue-values').textContent,'');
  assert(element('nas-space-queue').classList.contains('hidden'));
  context.fetch=async()=>({ok:true,json:async()=>({success:true,enabled:true,space:null})});
  await nas.testConnection();
  assert(element('nas-space-settings').classList.contains('hidden'),'Unsupported capacity must stay hidden after test cleanup');
  nas.updateMode();
  assert(element('nas-space-settings').classList.contains('hidden'),'Selecting SMB must not reveal unavailable capacity');
  nas.renderSpace(tested,'nas-space-settings');
  context.fetch=async()=>({ok:false,json:async()=>({error:'連線失敗'})});
  await nas.testConnection();
  assert(element('nas-space-settings').classList.contains('hidden'),'Failed connection test must clear previous capacity');
  assert.equal(element('nas-space-settings-values').innerHTML,'');
  console.log('NAS total/free/used capacity, quotas, hidden unsupported values, refresh isolation and duplicate requests: PASS');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
