const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const file = 'uav-web/frontend/subtitle-settings.js';
assert.ok(fs.existsSync(file), 'subtitle settings controller should be present');
const context = vm.createContext({window: {}});
vm.runInContext(fs.readFileSync(file, 'utf8'), context);

const calls = [];
const profiles = [
  {profile_id:'vad-one', display_name:'VAD', kind:'vad', provider:'localai', capabilities:['vad'], base_url:'http://model.test/v1', model:'vad-model', parameters:{}},
  {profile_id:'asr-one', display_name:'ASR', kind:'asr', provider:'localai', capabilities:['asr'], base_url:'http://model.test/v1', model:'asr-model', parameters:{}},
  {profile_id:'text-one', display_name:'Text', kind:'text', provider:'localai', capabilities:['correction','translation'], base_url:'http://model.test/v1', model:'text-model', parameters:{}},
];
const request = async (method, path, body) => {
  calls.push({method, path, body});
  if (path === '/api/subtitles/profiles' && method === 'GET') return {profiles};
  if (path === '/api/subtitles/profiles' && method === 'PUT') return {profiles:body.profiles.map(profile=>({...profile,credential_configured:!!profile.api_key,reasoning_verification:{correction:{status:'unknown',verified_at:null,scope:'localai false/true contrast',stale_reason:'設定或憑證已變更，請重新驗證'}}}))};
  if (path === '/api/subtitles/videos') return {videos:[], errors:[], capabilities:{local_vad:{available:false,reason:'部署未提供獨立 VAD'}}};
  if (path === '/api/subtitles/profiles/test' && body.action==='test' && !body.allow_reasoning) return {success:false,test_status:'not_run',format_pass:null,reasoning_control:'unknown',quality:'unverified',message:'未確認推理控制'};
  return {success:true, profiles};
};

function fakeElement(isSelect=false) {
  const listeners={};
  return {textContent:'',children:[],options:[],value:'',append(...items){this.children.push(...items);if(isSelect)this.options.push(...items);},
    replaceChildren(...items){this.children=[...items];if(isSelect)this.options=[...items];},addEventListener(name,callback){listeners[name]=callback;},dispatch(name){listeners[name]?.();},setAttribute(){}};
}

async function run() {
  let confirmations = 0;
  const confirmationMessages=[];
  let approved = false;
  const subtitles = context.window.createSubtitleSettings({request, render:()=>{}, confirmExternal:message=>{confirmations++;confirmationMessages.push(message);return approved;}});
  await subtitles.load();
  assert.equal(calls.length, 2, 'loading UI data must not probe a model service');
  assert.equal(subtitles.state.localVad.available, false);
  assert.match(subtitles.state.localVad.reason, /獨立 VAD/);
  subtitles.state.profiles.find(profile=>profile.profile_id==='text-one').reasoning_verification={
    correction:{status:'supported',verified_at:'2026-10-21T00:00:00Z',scope:'localai false/true contrast',stale_reason:null},
  };
  const profileList=fakeElement();
  const kind=fakeElement(); kind.value='text';
  const provider=fakeElement(true); provider.value='localai'; provider.options=[{value:'localai'}];
  const protocolHelp=fakeElement();
  const elements={'subtitle-profile-list':profileList,'subtitle-profile-kind':kind,
    'subtitle-profile-provider':provider,'subtitle-profile-protocol-help':protocolHelp};
  for(const stage of ['vad','asr','correction','translation'])elements[`subtitle-stage-profile-${stage}`]=fakeElement(true);
  const root={getElementById:id=>elements[id]||null,createElement:()=>fakeElement(),querySelectorAll:()=>[]};
  subtitles.renderDom(root);
  kind.value='asr'; kind.dispatch('change');
  const wordTimestampExample=protocolHelp.textContent.match(/貼上 (\{"timestamp_granularities":\["segment","word"\]\})/);
  assert.ok(wordTimestampExample,
    'ASR profile help must expose a pasteable JSON example for optional word timestamps');
  assert.deepEqual(JSON.parse(wordTimestampExample[1]),{timestamp_granularities:['segment','word']});
  assert.match(protocolHelp.textContent,/response_format=verbose_json.*有效時間戳 segments/,
    'ASR profile help must retain the verbose JSON and timestamped-segments contract');
  kind.value='text'; kind.dispatch('change');
  assert.match(protocolHelp.textContent,/batch_size.*每批字幕段數.*預設 ?20.*1–20.*max_output_tokens.*獨立.*不會傳給模型 API/,
    'text profile help must explain that local batch size is independent from the model token limit and is not sent to the provider');
  const outputContractExample=protocolHelp.textContent.match(/LocalAI 可選參數 JSON：(\{"output_contract":"ordered_segment_list_v1"\})/);
  assert.ok(outputContractExample,'LocalAI text profile help must expose the opt-in output contract example');
  assert.deepEqual(JSON.parse(outputContractExample[1]),{output_contract:'ordered_segment_list_v1'});
  assert.match(protocolHelp.textContent,/影匣本地設定.*映射為 LocalAI v4\.11 llama\.cpp grammar.*並非直接傳送的 OpenAI 欄位/,
    'output contract help must explain that the profile option maps to a LocalAI grammar extension');
  assert.match(protocolHelp.textContent,/停用思考驗證不會驗證 grammar 支援.*小批文字階段資格測試.*不保證字幕語意/,
    'output contract help must separate reasoning verification from grammar qualification');
  provider.value='openai-compatible'; provider.dispatch('change');
  assert.doesNotMatch(protocolHelp.textContent,/output_contract/,
    'other text providers must not be shown the LocalAI-only output contract');
  provider.value='localai'; provider.dispatch('change');
  const visibleText=JSON.stringify(profileList.children);
  assert.match(visibleText,/範圍：此模型／連線／參數的開關對照/,
    'LocalAI verification UI must show the exact profile-scoped evidence range');
  subtitles.state.profiles.find(profile=>profile.profile_id==='text-one').reasoning_verification={
    correction:{status:'unknown',verified_at:null,scope:'localai false/true contrast',stale_reason:'尚未驗證'},
  };
  const edited={...profiles[0],display_name:'Edited',credential_configured:true};
  await subtitles.saveProfile(edited);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1).body)),{profiles:[{...profiles[0],display_name:'Edited'}]});
  assert.equal(subtitles.state.profiles.length,profiles.length,'upsert must retain other profiles in local state');
  assert.ok(subtitles.state.profiles.some(profile=>profile.profile_id==='asr-one'));
  const beforeExternal=calls.length;

  assert.equal(await subtitles.testProfile('text-one', 'correction'), false);
  assert.equal(await subtitles.discoverModels('text-one'), false);
  assert.equal(confirmations, 2, 'each external request must be confirmed');
  assert.equal(calls.length, beforeExternal, 'cancelled confirmation must make no external request');
  assert.match(confirmationMessages[0],/兩筆小型合成文字請求.*短暫啟用思考/,
    'LocalAI confirmation must explain the temporary true-mode contrast probe');

  approved = true;
  const blockedReport=await subtitles.testProfile('text-one','correction');
  assert.equal(blockedReport.reasoning_control,'unknown','HTTP 200 diagnostic must remain visible to the caller');
  await subtitles.testProfile('text-one', 'correction', {allowReasoning:true});
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))), {
    method:'POST', path:'/api/subtitles/profiles/test',
    body:{action:'test', profile_id:'text-one', stage:'correction', confirm_external_call:true},
  });
  subtitles.state.reasoningControl['text-one']={correction:'supported'};
  assert.throws(()=>subtitles.downloadRequest({selectedStages:['correction'],profileIds:{correction:'text-one'},targetLanguages:['zh-TW'],outputLanguages:['ja'],allowReasoning:false}),/尚未確認.*停用額外思考/,
    'transient test state must not authorize normal LocalAI tasks');
  subtitles.state.profiles.find(profile=>profile.profile_id==='text-one').reasoning_verification={
    correction:{status:'supported',verified_at:'2026-10-21T00:00:00Z',scope:'localai false/true contrast',stale_reason:null},
  };
  assert.ok(subtitles.downloadRequest({selectedStages:['correction'],profileIds:{correction:'text-one'},targetLanguages:['zh-TW'],outputLanguages:['ja'],allowReasoning:false}),
    'saved profile-specific verification should authorize the ordinary non-reasoning request');
  await subtitles.saveProfile({...profiles[2],model:'changed-model'});
  assert.equal(subtitles.state.reasoningControl['text-one'],undefined,'changing a tested profile runtime invalidates cached capability state');
  assert.equal(subtitles.state.profiles.find(profile=>profile.profile_id==='text-one').reasoning_verification.correction.status,'unknown',
    'server save response should replace stale verification with unknown');
  assert.match(context.window.createSubtitleProfileId(),/^profile-[a-z0-9-]+$/,'new-profile IDs must work outside secure contexts without randomUUID');

  const pipeline = {
    selectedStages:['vad','asr','correction'], profileIds:{vad:'vad-one',asr:'asr-one',correction:'text-one'},
    targetLanguages:[], outputLanguages:['ja'], allowReasoning:false,
  };
  assert.throws(()=>subtitles.downloadRequest(pipeline), /尚未確認.*停用額外思考/);
  const requestBody = subtitles.downloadRequest({...pipeline,allowReasoning:true});
  assert.deepEqual(JSON.parse(JSON.stringify(requestBody)), {
    selected_stages:['vad','asr','correction'],
    profile_ids:{vad:'vad-one',asr:'asr-one',correction:'text-one'},
    target_languages:[], output_languages:['ja'], allow_reasoning:true,
  });
  console.log('Subtitle profile loading, explicit probes and download snapshot: PASS');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
