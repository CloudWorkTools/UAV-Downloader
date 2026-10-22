(() => {
  const STAGES = ['vad', 'asr', 'correction', 'translation'];
  const STAGE_LABELS = {vad:'語音活動偵測（VAD）', asr:'語音辨識（ASR）', correction:'原文後矯正', translation:'字幕翻譯'};
  const KIND_STAGES = {vad:['vad'], asr:['asr'], text:['correction','translation']};
  const PROVIDERS = ['localai','openai-compatible','ollama','lmstudio','openai','anthropic','gemini'];
  const probeIdentity = profile => JSON.stringify([profile?.provider,profile?.model,profile?.base_url,profile?.parameters]);
  const safeJson = text => {
    try { const value = JSON.parse(text); if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error(); return value; }
    catch { throw new Error('模型參數須為 JSON 物件'); }
  };
  const createProfileId = () => `profile-${globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2,12)}`}`;

  window.createSubtitleSettings = ({request, render=()=>{}, confirmExternal=message => window.confirm(message)}={}) => {
    const api = request || (async (method, path, body) => {
      const response = await fetch(path, {method, headers:body ? {'Content-Type':'application/json'} : undefined,
        body:body ? JSON.stringify(body) : undefined});
      const data = await response.json();
      const isProbe = path === '/api/subtitles/profiles/test' && body && body.action === 'test';
      if (!response.ok || (data.success === false && !isProbe)) throw new Error(data.error || data.message || '字幕操作失敗');
      return data;
    });
    const state = {profiles:[], videos:[], listErrors:[], reasoningControl:{}, localVad:{available:false,reason:'尚未載入部署能力'},
      selectedStages:[], profileIds:{}, targetLanguages:['zh-TW'], outputLanguages:['ja','zh-TW'], allowReasoning:false};
    let paint = render;
    const update = () => paint(state);
    async function load() {
      const [profileData, videoData] = await Promise.all([
        api('GET','/api/subtitles/profiles'), api('GET','/api/subtitles/videos'),
      ]);
      state.profiles = profileData.profiles || [];
      state.videos = videoData.videos || [];
      state.listErrors = videoData.errors || [];
      state.localVad = videoData.capabilities?.local_vad || state.localVad;
      for (const stage of STAGES) {
        const existing = state.profiles.find(profile => profile.capabilities?.includes(stage));
        if (!state.profileIds[stage] && existing) state.profileIds[stage] = existing.profile_id;
      }
      update();
      return state;
    }
    async function saveProfile(profile, apiKey='') {
      const previous=state.profiles.find(item=>item.profile_id===profile.profile_id);
      const value = {profile_id:profile.profile_id,display_name:profile.display_name,kind:profile.kind,
        provider:profile.provider,capabilities:profile.capabilities,base_url:profile.base_url,
        model:profile.model,parameters:profile.parameters};
      if (apiKey) value.api_key = apiKey;
      const data = await api('PUT','/api/subtitles/profiles',{profiles:[value]});
      const saved = data.profiles?.[0] || value;
      if(previous&&probeIdentity(previous)!==probeIdentity(saved))delete state.reasoningControl[saved.profile_id];
      state.profiles = [...state.profiles.filter(item => item.profile_id !== saved.profile_id),saved];
      update();
      return saved;
    }
    async function testProfile(profileId, stage, {allowReasoning=false}={}) {
      const profile = state.profiles.find(item => item.profile_id === profileId);
      if (!profile) throw new Error('請先選擇字幕模型設定檔');
      const localAiContrast=profile.provider==='localai'&&['correction','translation'].includes(stage);
      const contrastText=localAiContrast
        ? '會送出兩筆小型合成文字請求：一筆停用思考，另一筆短暫啟用思考作對照；這只用來驗證設定，正常字幕校正／翻譯仍要求停用思考。'
        : '';
      if (!confirmExternal(`即將向 ${profile.base_url || '尚未設定的模型服務'} 傳送小型${STAGE_LABELS[stage] || '字幕'}相容性測試。${contrastText}這會呼叫外部服務並可能產生費用。要繼續嗎？`)) return false;
      const body={action:'test', profile_id:profileId, stage, confirm_external_call:true};
      if(profile.provider!=='localai') body.allow_reasoning=!!allowReasoning;
      const report = await api('POST','/api/subtitles/profiles/test',body);
      (state.reasoningControl[profileId] ||= {})[stage] = report.reasoning_control || 'unknown';
      if(profile.provider==='localai'&&report.reasoning_verification){
        const current=state.profiles.find(item=>item.profile_id===profileId);
        if(current) current.reasoning_verification={...(current.reasoning_verification||{}),[stage]:report.reasoning_verification};
      }
      update();
      return report;
    }
    async function discoverModels(profileId) {
      const profile = state.profiles.find(item => item.profile_id === profileId);
      if (!profile) throw new Error('請先選擇字幕模型設定檔');
      if (!confirmExternal(`即將向 ${profile.base_url || '尚未設定的模型服務'} 查詢模型清單。這會呼叫外部服務。要繼續嗎？`)) return false;
      return api('POST','/api/subtitles/profiles/test',{
        action:'discover_models', profile_id:profileId, confirm_external_call:true,
      });
    }
    function downloadRequest(options) {
      const selectedStages = options?.selectedStages ?? readSelectedStages();
      const profileIds = options?.profileIds ?? readProfileIds(selectedStages);
      const targetLanguages = options?.targetLanguages ?? readChecked('subtitle-target-language');
      const outputLanguages = options?.outputLanguages ?? readChecked('subtitle-output-language');
      const allowReasoning = options?.allowReasoning ?? document.getElementById('subtitle-allow-reasoning')?.checked ?? false;
      if (!selectedStages.length) return null;
      if (selectedStages.some(stage => !profileIds[stage] && !profileIds.text)) throw new Error('請為每個已選字幕階段選擇設定檔');
      if (selectedStages.includes('translation') && !targetLanguages.length) throw new Error('請至少選擇一種字幕翻譯語言');
      if (!allowReasoning) for (const stage of ['correction','translation']) if (selectedStages.includes(stage)) {
        const id=profileIds[stage] || profileIds.text;
        const profile=state.profiles.find(item=>item.profile_id===id);
        const known=profile && (['ollama','lmstudio'].includes(profile.provider) ||
          (profile.provider==='localai' ? profile.reasoning_verification?.[stage]?.status==='supported' :
            state.reasoningControl[id]?.[stage]==='supported'));
        if(!known) throw new Error('尚未確認此設定檔能停用額外思考；請明確允許思考或選用已確認的服務。');
      }
      state.selectedStages=[...selectedStages];state.profileIds={...profileIds};state.targetLanguages=[...targetLanguages];state.outputLanguages=[...outputLanguages];state.allowReasoning=!!allowReasoning;
      return {selected_stages:selectedStages, profile_ids:profileIds,
        target_languages:targetLanguages, output_languages:outputLanguages, allow_reasoning:!!allowReasoning};
    }
    function readSelectedStages() {
      return STAGES.filter(stage => document.getElementById(`subtitle-stage-enabled-${stage}`)?.checked);
    }
    function readProfileIds(stages=readSelectedStages()) {
      const ids = {};
      for (const stage of stages) {
        const value = document.getElementById(`subtitle-stage-profile-${stage}`)?.value;
        if (value) ids[stage] = value;
      }
      return ids;
    }
    function readChecked(name) { return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(item => item.value); }
    function bind(root=document) {
      const byId = id => root.getElementById(id);
      const el = (tag, text, className='') => {const item=root.createElement(tag); item.textContent=text; if(className)item.className=className; return item;};
      const status = (message, isError=false) => {const target=byId('subtitle-settings-status'); if(target){target.textContent=message; target.setAttribute('role',isError?'alert':'status');}};
      const profileList = byId('subtitle-profile-list');
      const stageSelect = stage => byId(`subtitle-stage-profile-${stage}`);
      function renderProfiles() {
        if (!profileList) return;
        profileList.replaceChildren();
        if (!state.profiles.length) profileList.append(el('p','尚無字幕模型設定檔，請先建立一組。','text-sm text-gray-400'));
        for (const profile of state.profiles) {
          const row=el('div','', 'flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[#3e3e4e] p-3');
          const summary=el('span',`${profile.display_name} · ${profile.provider} · ${profile.model}`,'min-w-0 break-words text-sm');
          const verification=profile.provider==='localai'&&profile.kind==='text'
            ? Object.entries(profile.reasoning_verification||{}).map(([stage,result])=>{
              const label=result.status==='supported'?'已驗證停用思考':result.status==='unsupported'?'未通過停用思考驗證':'尚未驗證';
              const when=result.verified_at?` · ${result.verified_at}`:'';
              const stale=result.stale_reason?` · ${result.stale_reason}`:'';
              return `${STAGE_LABELS[stage]||stage}：${label}（範圍：此模型／連線／參數的開關對照）${when}${stale}`;
            }).join('；') : '';
          const verificationNote=verification?el('span',verification,'basis-full text-xs text-gray-400'):null;
          const edit=el('button','編輯','h-10 rounded border border-[#3e3e4e] px-3 text-sm');
          edit.type='button'; edit.addEventListener('click',()=>fillProfile(profile));
          row.append(summary,edit); if(verificationNote)row.append(verificationNote); profileList.append(row);
        }
        for (const stage of STAGES) {
          const select=stageSelect(stage); if(!select) continue;
          const previous=state.profileIds[stage] || select.value;
          select.replaceChildren();
          const empty=el('option','選擇設定檔'); empty.value=''; select.append(empty);
          state.profiles.filter(profile=>profile.capabilities?.includes(stage)).forEach(profile=>{
            const option=el('option',`${profile.display_name} · ${profile.model}`); option.value=profile.profile_id; select.append(option);
          });
          if ([...select.options].some(option=>option.value===previous)) select.value=previous;
          else if (select.options.length>1) select.value=select.options[1].value;
          state.profileIds[stage]=select.value;
        }
        const localVad=byId('subtitle-local-vad-status');
        if(localVad) localVad.textContent=state.localVad.available?'本機 VAD 可用。':`${state.localVad.reason || '本機 VAD 不可用。'} 可改選已設定的外部 VAD 服務；此頁不會下載本機執行環境。`;
      }
      function fillProfile(profile) {
        for(const key of ['profile-id','profile-name','profile-kind','profile-provider','profile-url','profile-model']) {
          const node=byId(`subtitle-${key}`); if(node) node.value=profile[{ 'profile-id':'profile_id','profile-name':'display_name','profile-kind':'kind','profile-provider':'provider','profile-url':'base_url','profile-model':'model'}[key]] || '';
        }
        const parameters=byId('subtitle-profile-parameters'); if(parameters) parameters.value=JSON.stringify(profile.parameters || {},null,2);
        for(const stage of STAGES){const check=byId(`subtitle-profile-cap-${stage}`); if(check)check.checked=!!profile.capabilities?.includes(stage);}
        const secret=byId('subtitle-profile-key'); if(secret)secret.value='';
        updateCapabilityControls();
      }
      const save=byId('subtitle-profile-save');
      if(save) save.addEventListener('click',async()=>{
        save.disabled=true;
        try {
          const profile={profile_id:byId('subtitle-profile-id').value.trim() || createProfileId(),
            display_name:byId('subtitle-profile-name').value.trim(),kind:byId('subtitle-profile-kind').value,
            provider:byId('subtitle-profile-provider').value,base_url:byId('subtitle-profile-url').value.trim(),
            model:byId('subtitle-profile-model').value.trim(),parameters:safeJson(byId('subtitle-profile-parameters').value),
            capabilities:STAGES.filter(stage=>byId(`subtitle-profile-cap-${stage}`).checked)};
          const saved=await saveProfile(profile,byId('subtitle-profile-key').value);
          byId('subtitle-profile-id').value=saved.profile_id; byId('subtitle-profile-key').value='';
          status('設定檔已保存；空白金鑰會沿用相同服務位址已保存的金鑰。');
        } catch(error){status(error.message,true);} finally{save.disabled=false;}
      });
      const newButton=byId('subtitle-profile-new');
      if(newButton)newButton.addEventListener('click',()=>{
        for(const id of ['subtitle-profile-id','subtitle-profile-name','subtitle-profile-url','subtitle-profile-model','subtitle-profile-key'])byId(id).value='';
        byId('subtitle-profile-kind').value='text';byId('subtitle-profile-provider').value='localai';byId('subtitle-profile-parameters').value='{}';
        for(const stage of STAGES)byId(`subtitle-profile-cap-${stage}`).checked=stage==='correction'||stage==='translation';
        updateCapabilityControls();
        byId('subtitle-profile-name').focus();
      });
      function updateCapabilityControls(){
        const kind=byId('subtitle-profile-kind')?.value;
        for(const stage of STAGES){const check=byId(`subtitle-profile-cap-${stage}`);if(!check)continue;const allowed=kind==='vad'?stage==='vad':kind==='asr'?stage==='asr':stage==='correction'||stage==='translation';check.disabled=!allowed;if(!allowed)check.checked=false;}
        const provider=byId('subtitle-profile-provider');if(provider){for(const option of provider.options){const supported=kind==='vad'?option.value==='localai':kind==='asr'?['localai','openai-compatible'].includes(option.value):true;option.disabled=!supported;}if([...provider.options].find(option=>option.value===provider.value)?.disabled)provider.value='localai';}
        const help=byId('subtitle-profile-protocol-help');if(help){const service=provider?.value||'localai';if(kind==='vad')help.textContent='LocalAI VAD：POST /v1/vad，以 16 kHz 單聲道 PCM 樣本呼叫；回應 segments.start/end 必須是秒。門檻、靜音長度與補邊在服務端模型設定；此設定檔參數須為空物件。';else if(kind==='asr')help.textContent='ASR：OpenAI 音訊轉錄 multipart POST /audio/transcriptions；服務須接受 response_format=verbose_json 與預設 timestamp_granularities[]=segment。可在模型參數 JSON 貼上 {"timestamp_granularities":["segment","word"]} 要求原生字詞時間戳；服務仍必須回傳有效時間戳 segments。可填 prompt 與 temperature（0–2）。';else{const endpoints={localai:'/chat/completions',openai:'/chat/completions','openai-compatible':'/chat/completions',ollama:'/api/chat',lmstudio:'/api/v1/chat',anthropic:'/messages',gemini:'/models/{model}:generateContent'};const serviceLabels={localai:'LocalAI 相容',openai:'OpenAI', 'openai-compatible':'OpenAI 相容',ollama:'Ollama 原生',lmstudio:'LM Studio 原生',anthropic:'Anthropic',gemini:'Gemini'};const params=service==='ollama'?'temperature、top_p、top_k、repeat_penalty、num_predict':service==='lmstudio'?'temperature、top_p、top_k、min_p、repeat_penalty、max_output_tokens、context_length':'temperature、max_output_tokens';help.textContent=`文字服務：${serviceLabels[service]||service} 使用 ${endpoints[service]||'服務端 API'}；停用思考控制依服務回報或測試結果，未知時會阻擋矯正／翻譯，除非明確允許額外思考。可填參數：${params}；另外可設定 batch_size 控制影匣每批字幕段數（預設 20，範圍 1–20），與 max_output_tokens 額度各自獨立，只影響影匣本地分批，不會傳給模型 API。溫度範圍 0–1（ASR 例外為 0–2），其他上限由服務契約驗證；服務端模板仍可能覆寫模型實際行為。`;}}
      }
      byId('subtitle-profile-kind')?.addEventListener('change',updateCapabilityControls);
      byId('subtitle-profile-provider')?.addEventListener('change',updateCapabilityControls);
      updateCapabilityControls();
      const discover=byId('subtitle-discover-models');
      if(discover)discover.addEventListener('click',async()=>{
        discover.disabled=true;
        try{const profileId=byId('subtitle-profile-id').value;const result=await discoverModels(profileId);
          const options=byId('subtitle-discovered-models');if(options){options.replaceChildren();for(const model of result.models||[]){const option=el('option',model);option.value=model;options.append(option);}}
          status(result===false?'未呼叫外部服務。':result.available?`已載入 ${result.models.length} 個模型；仍可手動輸入。`:result.message || '模型清單無法載入，仍可手動輸入。');
        }catch(error){status(error.message,true);}finally{discover.disabled=false;}
      });
      const test=byId('subtitle-test-profile');
      if(test)test.addEventListener('click',async()=>{
        test.disabled=true;
        try{const profileId=byId('subtitle-profile-id').value;const stage=byId('subtitle-test-stage').value;
          const report=await testProfile(profileId,stage,{allowReasoning:byId('subtitle-test-allow-reasoning').checked});
          status(report===false?'未呼叫外部服務。':formatProbe(report),report!==false&&report.success===false);
        }catch(error){status(error.message,true);}finally{test.disabled=false;}
      });
      for(const stage of STAGES){const select=stageSelect(stage);if(select)select.addEventListener('change',()=>{state.profileIds[stage]=select.value;});const enabled=byId(`subtitle-stage-enabled-${stage}`);if(enabled)enabled.addEventListener('change',()=>{state.selectedStages=readSelectedStages();});}
      for(const input of root.querySelectorAll('input[name="subtitle-target-language"], input[name="subtitle-output-language"]'))input.addEventListener('change',()=>{state.targetLanguages=readChecked('subtitle-target-language');state.outputLanguages=readChecked('subtitle-output-language');});
      const reload=byId('subtitle-reload-settings');if(reload)reload.addEventListener('click',async()=>{try{await load();status('設定與影片清單已更新。');}catch(error){status(error.message,true);}});
      renderProfiles();
      return {renderProfiles};
    }
    function renderDom(root=document) {
      const dom=bind(root);
      paint=dom.renderProfiles;
      update();
      return dom;
    }
    return {state,load,saveProfile,testProfile,discoverModels,downloadRequest,renderDom};
  };
  function formatProbe(report){
    const format=report.format_pass===true?'格式符合':report.format_pass===false?'格式不符合':'格式未確認';
    const reasoning={supported:'可停用額外思考',unsupported:'不支援停用額外思考',unknown:'額外思考控制未知',not_applicable:'不適用'}[report.reasoning_control] || '額外思考控制未知';
    const quality=report.quality==='unverified'?'品質未驗收':'品質狀態未知';
    return `${report.message?`${report.message}；`:''}${format}；${reasoning}；${quality}。`;
  }
  window.createSubtitleProfileId = createProfileId;
})();
