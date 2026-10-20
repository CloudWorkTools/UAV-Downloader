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
  if (path === '/api/subtitles/profiles' && method === 'PUT') return {profiles:body.profiles.map(profile=>({...profile,credential_configured:!!profile.api_key}))};
  if (path === '/api/subtitles/videos') return {videos:[], errors:[], capabilities:{local_vad:{available:false,reason:'部署未提供獨立 VAD'}}};
  if (path === '/api/subtitles/profiles/test' && body.action==='test' && !body.allow_reasoning) return {success:false,test_status:'not_run',format_pass:null,reasoning_control:'unknown',quality:'unverified',message:'未確認推理控制'};
  return {success:true, profiles};
};

async function run() {
  let confirmations = 0;
  let approved = false;
  const subtitles = context.window.createSubtitleSettings({request, render:()=>{}, confirmExternal:()=>{confirmations++; return approved;}});
  await subtitles.load();
  assert.equal(calls.length, 2, 'loading UI data must not probe a model service');
  assert.equal(subtitles.state.localVad.available, false);
  assert.match(subtitles.state.localVad.reason, /獨立 VAD/);
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

  approved = true;
  const blockedReport=await subtitles.testProfile('text-one','correction');
  assert.equal(blockedReport.reasoning_control,'unknown','HTTP 200 diagnostic must remain visible to the caller');
  await subtitles.testProfile('text-one', 'correction', {allowReasoning:true});
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))), {
    method:'POST', path:'/api/subtitles/profiles/test',
    body:{action:'test', profile_id:'text-one', stage:'correction', confirm_external_call:true, allow_reasoning:true},
  });
  subtitles.state.reasoningControl['text-one']={correction:'supported'};
  await subtitles.saveProfile({...profiles[2],model:'changed-model'});
  assert.equal(subtitles.state.reasoningControl['text-one'],undefined,'changing a tested profile runtime invalidates cached capability state');
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
