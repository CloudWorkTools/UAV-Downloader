(() => {
  window.createSubtitleTasks = ({settings, request, root=document}={}) => {
    const api = request || (async (method,path,body) => {
      const response=await fetch(path,{method,headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});
      const data=await response.json(); if(!response.ok||data.success===false)throw new Error(data.error||'字幕操作失敗'); return data;
    });
    const state={videos:[],videoErrors:[],videoPartial:false,videoNotice:null,tasks:[],publications:[],results:[],selected:new Set(),activeVideo:null,preview:null};
    let submitting=false,resultsRequest=0,refreshInFlight=null;
    let pendingTaskRender=false,pendingResultsRender=false;
    const byId=id=>root.getElementById(id), el=(tag,text='',cls='')=>{const node=root.createElement(tag);node.textContent=text;if(cls)node.className=cls;return node;};
    function addSnapshotDetails(container,entries,label='檢視設定快照'){
      const details=el('details','','text-xs text-gray-400');const summary=el('summary',label,'cursor-pointer');details.append(summary);
      const pre=el('pre','','mt-2 whitespace-pre-wrap break-all font-mono');const lines=[];
      for(const [stage,snapshot] of entries){const profile=snapshot?.profile||{};lines.push(`${stage} · 快照 ${snapshot?.snapshot_id||'未提供'}`,`顯示名稱：${profile.display_name||'未命名'}`,`服務：${profile.provider||'未知'}`,`模型：${profile.model||'未知'}`,`Base URL：${profile.base_url||'未提供'}`,`參數：${JSON.stringify(profile.parameters||{})}`,`階段能力：${(profile.capabilities||[]).join('、')||'未提供'}`);}
      pre.textContent=lines.join('\n');details.append(pre);details.addEventListener('toggle',()=>{if(details.open)return;queueMicrotask(()=>{if(container.closest('#subtitle-task-list')&&pendingTaskRender)renderTasks();if(container.closest('#subtitle-results')&&pendingResultsRender)renderResults();});});container.append(details);
    }
    const status=(message,error=false)=>{const node=byId('subtitle-error');if(node){node.textContent=message;node.setAttribute('role',error?'alert':'status');}const dialogNode=byId('subtitle-redo-dialog')?.open?byId('subtitle-redo-error'):byId('subtitle-confirm-dialog')?.open?byId('subtitle-confirm-error'):null;if(dialogNode){dialogNode.textContent=message;dialogNode.setAttribute('role',error?'alert':'status');}};
    const requestBody=()=>settings.downloadRequest();
    const busyActions=new Set();
    const stageLabels={vad:'語音區間',asr:'語音辨識',correction:'字幕校正',translation:'字幕翻譯'};
    const statusLabels={queued:'排隊中',running:'處理中',retry_wait:'等待重試',cancelling:'取消中',waiting_for_resume:'等待手動續跑',completed:'已完成',failed:'失敗',cancelled:'已取消'};
    const languageLabels={ja:'日文',en:'英文','zh-TW':'繁體中文'};
    const sourceLabels={history:'下載歷史',configured:'影片目錄',smb:'NAS'};
    const publicationLabels={published:'已發布',failed:'儲存失敗',publishing:'發布中',not_requested:'未要求發布'};
    const label=(map,value,fallback='')=>map[value]||fallback||value||'—';
    function readableDate(value){const numeric=typeof value==='number'?value:Number(value);if(!Number.isFinite(numeric)||numeric<=0)return '';try{return new Intl.DateTimeFormat('zh-TW',{dateStyle:'medium',timeStyle:'short'}).format(new Date(numeric*1000));}catch{return '';}}
    function restoreFailedDialogActionFocus(dialogId,button,wasFocused){
      const dialog=byId(dialogId);if(!wasFocused||!button||!dialog?.open)return;
      const active=root.activeElement;
      if(active===root.body||active===dialog||active===button)button.focus();
    }
    function liveTaskTitle(task){const source=state.videos.find(video=>video.video_id===task.video_id);const profile=task.profile_snapshots?.[task.current_stage]?.profile;const stage=task.current_stage?label(stageLabels,task.current_stage):(['completed','failed','cancelled'].includes(task.status)?'':'等待階段');return `${source?.title||source?.name||task.video_id} · ${label(statusLabels,task.status,'未知狀態')}${stage?` · ${stage}`:''}${task.current_language?` / ${label(languageLabels,task.current_language)}`:''}${profile?` · ${profile.display_name?`${profile.display_name} · `:''}${profile.provider||'未知服務'} / ${profile.model||'未知模型'}`:''}`;}
    function updateFocusedTaskFields(){const box=byId('subtitle-task-list');if(!box)return;for(const task of state.tasks){const row=[...box.querySelectorAll('article[data-task-id]')].find(item=>item.dataset.taskId===task.task_id);if(!row)continue;const title=row.querySelector('[data-task-live-title]'),progress=row.querySelector('[data-task-live-progress]'),error=row.querySelector('[data-task-live-error]');if(title)title.textContent=liveTaskTitle(task);if(progress)progress.textContent=task.progress!=null?`進度 ${task.progress}%`:'排隊等待';if(error){error.textContent=task.error?`錯誤：${task.error}`:'';error.hidden=!task.error;}for(const line of row.querySelectorAll('[data-task-publication-language]')){const entry=task.publish_status?.[line.dataset.taskPublicationLanguage];if(entry)line.textContent=`輸出 ${label(languageLabels,line.dataset.taskPublicationLanguage)}：${label(publicationLabels,entry.status)}${entry.error?` · ${entry.error}`:''}`;}}}
    function renderVideos(){
      const box=byId('subtitle-videos'); if(!box||box.contains(root.activeElement))return;
      box.replaceChildren();
      if(!state.videos.length){box.append(el('p','目前找不到可處理的影片。影片會從下載歷史與已設定的影片目錄載入。','text-sm text-gray-400'));return;}
      for(const video of state.videos){
        const row=el('label','', 'flex items-start gap-3 rounded-lg border border-[#3e3e4e] p-3');
        const check=el('input');check.type='checkbox';check.value=video.video_id;check.dataset.testid=`subtitle-video-${video.video_id}`;check.checked=state.selected.has(video.video_id);check.className='mt-1 h-5 w-5 accent-brand-600';
        check.addEventListener('change',()=>{check.checked?state.selected.add(video.video_id):state.selected.delete(video.video_id);});
        const text=el('span',`${video.title || video.name || '未命名影片'} · ${label(sourceLabels,video.source,'影片')}`,'min-w-0 break-words text-sm');row.append(check,text);box.append(row);
      }
    }
    function renderVideoErrors(){const box=byId('subtitle-video-errors');if(!box)return;box.replaceChildren();if(state.videoNotice)box.append(el('p',state.videoNotice));for(const error of state.videoErrors){box.append(el('p',`${label(sourceLabels,error.source,'影片來源')}：${error.error||'目前無法讀取'}`));}}
    function renderTasks(preserveFocus=false){
      const box=byId('subtitle-task-list');if(!box)return;
      if(byId('subtitle-redo-dialog')?.open||box.querySelector('details[open]')){pendingTaskRender=true;return;}
      if(!preserveFocus&&box.contains(root.activeElement)){pendingTaskRender=true;updateFocusedTaskFields();return;}
      const focusedTestId=preserveFocus&&box.contains(root.activeElement)?root.activeElement.dataset?.testid:null;
      pendingTaskRender=false;
      box.replaceChildren();
      if(!state.tasks.length){box.append(el('p','尚無字幕任務。選取影片後可提交新的字幕工作。','text-sm text-gray-400'));return;}
      for(const [index,task] of state.tasks.entries()){
        const row=el('article','', 'space-y-2 rounded-lg border border-[#3e3e4e] p-3');row.dataset.testid=`subtitle-task-${task.task_id}`;row.dataset.taskId=task.task_id;
        const source=state.videos.find(video=>video.video_id===task.video_id);
        const model=task.profile_snapshots?.[task.current_stage]?.profile;
        const title=el('h3',liveTaskTitle(task),'break-all text-sm font-semibold');title.dataset.taskLiveTitle='true';
        const progress=el('p',task.progress!=null?`進度 ${task.progress}%`:'排隊等待','text-xs text-gray-400');progress.dataset.taskLiveProgress='true';const errorLine=el('p',task.error?`錯誤：${task.error}`:'','break-words text-xs text-rose-300');errorLine.dataset.taskLiveError='true';errorLine.hidden=!task.error;row.append(title,progress,errorLine);
        const snapshots=Object.entries(task.profile_snapshots||{});
        if(snapshots.length){row.append(el('p',`已保存階段設定：${snapshots.map(([stage,snapshot])=>`${label(stageLabels,stage)} · ${snapshot.profile?.provider||'未知服務'} / ${snapshot.profile?.model||'未知模型'}`).join('；')}`,'break-words text-xs text-gray-400'));addSnapshotDetails(row,snapshots);}
        const actions=el('div','','flex flex-wrap gap-2');
        const publications=Object.entries(task.publish_status||{});
        if(publications.length)for(const [language,entry] of publications){const line=el('p',`輸出 ${label(languageLabels,language)}：${label(publicationLabels,entry.status)}${entry.error?` · ${entry.error}`:''}`,'break-words text-xs text-gray-400');line.dataset.taskPublicationLanguage=language;row.append(line);if(entry.status==='failed')addPublishRetry(task,language,actions);}
        const addAction=(action,label,handler)=>{const button=el('button',label,'min-h-10 rounded border border-[#3e3e4e] px-3 text-xs');button.type='button';button.dataset.testid=`subtitle-action-${action}-${task.task_id}`;button.addEventListener('click',async()=>{const key=`${task.task_id}:${action}`;if(busyActions.has(key))return;busyActions.add(key);button.disabled=true;try{await handler();}finally{busyActions.delete(key);if(button.isConnected)button.disabled=false;}});actions.append(button);};
        if(['waiting_for_resume','failed'].includes(task.status))addAction('resume','繼續',()=>act(task,'resume'));
        if(['queued','running','retry_wait','cancelling'].includes(task.status))addAction('cancel','取消',()=>act(task,'cancel'));
        if(['queued','waiting_for_resume'].includes(task.status)){
          addAction('up','往前',()=>act(task,'reorder',{position:Math.max(0,index-1)}));
          addAction('down','往後',()=>act(task,'reorder',{position:index+1}));
        }
        addAction('redo','重做階段',()=>openRedo(task));
        if(['queued','waiting_for_resume'].includes(task.status)&&!task.ever_started&&!task.inputs?.length)addAction('settings','套用新設定',()=>applySettings(task));
        addAction('results','查看字幕結果',()=>loadResults(task.video_id));
        row.append(actions);box.append(row);
      }
      if(focusedTestId){const target=[...box.querySelectorAll('[data-testid]')].find(node=>node.dataset.testid===focusedTestId);target?.focus();}
    }
    async function act(task,action,extra={}){try{await api('POST',`/api/subtitles/tasks/${encodeURIComponent(task.task_id)}/action`,{action,...extra});await refresh();status('字幕任務已更新。');}catch(error){status(error.message,true);}}
    function addPublishRetry(task,language,actions){const button=el('button',`只重試 ${language} 儲存`,'min-h-10 rounded border border-[#3e3e4e] px-3 text-xs');button.type='button';button.dataset.testid=`subtitle-action-retry-publish-${task.task_id}-${language}`;button.addEventListener('click',()=>runOnce(`${task.task_id}:publish:${language}`,button,async()=>{try{await api('POST',`/api/subtitles/tasks/${encodeURIComponent(task.task_id)}/action`,{action:'retry_publish',language});await refresh();status(`已重試 ${language} 字幕儲存。`);}catch(error){status(error.message,true);}}));actions.append(button);}
    async function runOnce(key,button,handler){if(busyActions.has(key))return;busyActions.add(key);button.disabled=true;try{await handler();}finally{busyActions.delete(key);if(button.isConnected)button.disabled=false;}}
    let redoTask=null,redoResults=[],redoTriggerTestId=null;
    async function openRedo(task){
      redoTriggerTestId=root.activeElement?.dataset?.testid||null;redoTask=task;const dialog=byId('subtitle-redo-dialog'),stage=byId('subtitle-redo-stage'),profile=byId('subtitle-redo-profile'),input=byId('subtitle-redo-input'),warning=byId('subtitle-redo-warning');const errorNode=byId('subtitle-redo-error');if(errorNode)errorNode.textContent='';
      stage.replaceChildren(...['vad','asr','correction','translation'].map(value=>{const option=el('option',label(stageLabels,value));option.value=value;return option;}));
      try{const data=await api('GET',`/api/subtitles/videos/${encodeURIComponent(task.video_id)}/results`);redoResults=data.results||[];}catch(error){redoResults=[];status(error.message,true);}
      let populatedStage=null;
      function updateRedoChoices(){
        const selected=stage.value;
        if(populatedStage!==selected){
          const previousProfile=profile.value;profile.replaceChildren();input.replaceChildren();const placeholder=el('option','請明確選擇上游版本');placeholder.value='';input.append(placeholder);
          const capability=selected==='vad'?'vad':selected==='asr'?'asr':selected;
          for(const item of settings.state.profiles.filter(candidate=>candidate.capabilities?.includes(capability))){const option=el('option',`${item.display_name} · ${item.model}`);option.value=item.profile_id;profile.append(option);}
          if([...profile.options].some(option=>option.value===previousProfile))profile.value=previousProfile;
          const upstream=selected==='asr'?redoResults.filter(item=>item.stage==='vad'):selected==='translation'?redoResults.filter(item=>['asr','correction'].includes(item.stage)):selected==='correction'?redoResults.filter(item=>item.stage==='asr'):[];
          const available=upstream.filter(item=>item.content_available);
          for(const result of available){const option=el('option',`${label(stageLabels,result.stage)} · ${label(languageLabels,result.language,'原文')} · ${readableDate(result.created_at)}${result.stale?' · 過期':''}`);option.value=result.result_id;option.dataset.stale=String(!!result.stale);input.append(option);}
          populatedStage=selected;
        }
        const upstream=selected==='asr'?redoResults.filter(item=>item.stage==='vad'):selected==='translation'?redoResults.filter(item=>['asr','correction'].includes(item.stage)):selected==='correction'?redoResults.filter(item=>item.stage==='asr'):[];
        const available=upstream.filter(item=>item.content_available);
        const needs=['asr','correction','translation'].includes(selected);input.disabled=!needs;
        warning.textContent=needs?(available.length?'請明確選擇有效且可讀取的上游版本；過期版本會標示。下游階段不會自動呼叫。':'找不到可讀取的上游字幕版本，請先完成前置階段。'):'此階段使用原始影片，不需要字幕上游版本。既有下游結果可能標為過期，不會自動重跑。';
        byId('subtitle-redo-language').disabled=selected!=='translation';
        const confirm=byId('subtitle-redo-confirm');if(confirm)confirm.disabled=!profile.value||(needs&&!input.value);
        const summary=byId('subtitle-redo-summary'),chosen=redoResults.find(item=>item.result_id===input.value),chosenProfile=settings.state.profiles.find(item=>item.profile_id===profile.value);
        if(summary)summary.textContent=`將只重做 ${label(stageLabels,selected)} (${chosenProfile?.provider||'服務'} / ${chosenProfile?.model||'模型'})${chosen?`，使用 ${label(stageLabels,chosen.stage)} 版本 ${chosen.result_id}${chosen.stale?'（已過期）':''}`:''}；既有下游版本可能標為過期，不會自動重跑。`;
      }
      stage.onchange=updateRedoChoices;profile.onchange=updateRedoChoices;input.onchange=updateRedoChoices;updateRedoChoices();dialog?.showModal();
    }
    async function redo(){
      if(!redoTask)return;const stage=byId('subtitle-redo-stage').value,profile_id=byId('subtitle-redo-profile').value,input_result_id=byId('subtitle-redo-input').value;
      if(!profile_id){status('請選擇此階段的設定檔。',true);return;}
      if(['asr','correction','translation'].includes(stage)&&!input_result_id){status('請明確選擇上游字幕版本。',true);return;}
      const key=`redo:${redoTask.task_id}:${stage}`;if(busyActions.has(key))return;busyActions.add(key);status('');const button=byId('subtitle-redo-confirm'),wasFocused=root.activeElement===button;let failed=false;if(button)button.disabled=true;
      try{const body={action:'redo',stage,profile_id,input_result_id:input_result_id||null,allow_reasoning:!!byId('subtitle-allow-reasoning')?.checked,output_languages:[...root.querySelectorAll('input[name="subtitle-output-language"]:checked')].map(item=>item.value)};if(stage==='translation')body.language=byId('subtitle-redo-language').value.trim();await api('POST',`/api/subtitles/tasks/${encodeURIComponent(redoTask.task_id)}/action`,body);byId('subtitle-redo-dialog')?.close();redoTask=null;await refresh();status('階段重做任務已加入佇列。');}catch(error){failed=true;status(error.message,true);}finally{busyActions.delete(key);if(button)button.disabled=false;if(failed)restoreFailedDialogActionFocus('subtitle-redo-dialog',button,wasFocused);}
    }
    async function applySettings(task){const profile_ids={};for(const stage of task.selected_stages||[]){const id=byId(`subtitle-stage-profile-${stage}`)?.value;if(id)profile_ids[stage]=id;}if((task.selected_stages||[]).some(stage=>!profile_ids[stage])){status('請為每個待套用階段選擇設定檔。',true);return;}await act(task,'apply_new_settings',{profile_ids});}
    async function loadResults(videoId){if(state.activeVideo!==videoId){state.preview=null;const preview=byId('subtitle-preview'),download=byId('subtitle-download');if(preview)preview.textContent='';if(download){download.hidden=true;if(download.dataset.objectUrl)URL.revokeObjectURL(download.dataset.objectUrl);delete download.dataset.objectUrl;}}state.activeVideo=videoId;const requestId=++resultsRequest;try{const data=await api('GET',`/api/subtitles/videos/${encodeURIComponent(videoId)}/results`);if(requestId!==resultsRequest||state.activeVideo!==videoId)return;state.results=data.results||[];renderResults();}catch(error){if(requestId===resultsRequest)status(error.message,true);}}
    function renderResults(){
      const box=byId('subtitle-results');if(!box)return;
      if(byId('subtitle-redo-dialog')?.open||box.contains(root.activeElement)||box.querySelector('details[open]')){pendingResultsRender=true;return;}
      pendingResultsRender=false;box.replaceChildren();
      if(!state.activeVideo||!state.results.length){box.append(el('p','尚無保存的字幕版本。','text-sm text-gray-400'));return;}
      const stagePosition=new Map();
      const resultVideoId=state.activeVideo;
      for(const result of state.results){const key=`${result.stage}:${result.language||''}`,position=stagePosition.get(key)||0;stagePosition.set(key,position+1);const row=el('article','', 'space-y-2 rounded-lg border border-[#3e3e4e] p-3');row.dataset.testid=`subtitle-result-${result.result_id}`;
        const source=state.videos.find(video=>video.video_id===resultVideoId);const sourceName=source?.title||source?.name||resultVideoId;
        row.append(el('h3',`${position===0?'最新版本 · ':position===1?'上一版本 · ':''}${sourceName} · ${label(stageLabels,result.stage)} · ${label(languageLabels,result.language)} · ${readableDate(result.created_at)}${result.stale?' · 上游版本已更新':''}${result.adopted?' · 已採用':''}${result.content_available?'':' · 內容已清理'}`,'break-words text-sm font-semibold'));
        const profile=result.profile_snapshot?.profile;row.append(el('p',`不可變模型快照：${profile?.display_name?`${profile.display_name} · `:''}${profile?.provider||'未知服務'} / ${profile?.model||'未知模型'}；上游版本：${result.input_result_ids?.join('、')||'影片來源'}`,'break-words text-xs text-gray-400'));addSnapshotDetails(row,[[result.stage,result.profile_snapshot]]);
        const actions=el('div','','flex flex-wrap gap-2');const button=(label,run)=>{const b=el('button',label,'min-h-10 rounded border border-[#3e3e4e] px-3 text-xs');b.type='button';b.addEventListener('click',()=>runOnce(`${resultVideoId}:${result.result_id}:${label}`,b,run));actions.append(b);};
        button('查看版本',async()=>{try{const data=await api('GET',`/api/subtitles/videos/${encodeURIComponent(resultVideoId)}/results/${encodeURIComponent(result.result_id)}`);if(state.activeVideo!==resultVideoId)return;const content=await api('GET',`/api/subtitles/videos/${encodeURIComponent(resultVideoId)}/results/${encodeURIComponent(result.result_id)}/content`);if(state.activeVideo!==resultVideoId)return;const current=typeof content.content==='string'?content.content:JSON.stringify(content.content,null,2);let display=current;if(['correction','translation'].includes(result.stage)&&data.inputs?.length){const parents=await Promise.all(data.inputs.map(parent=>api('GET',`/api/subtitles/videos/${encodeURIComponent(resultVideoId)}/results/${encodeURIComponent(parent.result_id)}/content`)));if(state.activeVideo!==resultVideoId)return;display=`上游版本\n${parents.map(parent=>typeof parent.content==='string'?parent.content:JSON.stringify(parent.content,null,2)).join('\n---\n')}\n\n目前版本\n${current}`;}state.preview=data;const preview=byId('subtitle-preview');if(preview)preview.textContent=display;const download=byId('subtitle-download');if(download.dataset.objectUrl)URL.revokeObjectURL(download.dataset.objectUrl);download.dataset.objectUrl=URL.createObjectURL(new Blob([current],{type:content.content_type}));download.href=download.dataset.objectUrl;download.download=content.filename;download.textContent=result.stage==='vad'?'下載 VAD 區間 JSON':'下載字幕 SRT';download.hidden=false;}catch(error){if(state.activeVideo===resultVideoId)status(error.message,true);}});
        if(result.stage!=='vad')button('發布字幕',async()=>{const defaultLanguage=result.language||(result.stage==='asr'||result.stage==='correction'?'ja':'zh-TW');const language=root.defaultView.prompt('輸出語言代碼（例如 zh-TW）',defaultLanguage);if(!language)return;const replace=root.defaultView.confirm('若存在同名字幕，是否明確替換？');try{const value=await api('POST',`/api/subtitles/videos/${encodeURIComponent(resultVideoId)}/results/${encodeURIComponent(result.result_id)}/action`,{action:'publish',output_language:language,replace_existing:replace});if(value.publish_status==='failed')status(`字幕發布失敗：${value.error||'儲存原因未提供'}`,true);else status(`字幕已發布${value.path?`：${value.path}`:''}。`);await refresh();}catch(error){status(error.message,true);}});
        if(position===1)button('恢復上一版本',async()=>{const output=result.stage==='vad'?'':root.defaultView.prompt('輸出語言代碼（留空只恢復字幕資料、不發布）',result.language||'');if(output===null)return;const replace=!!output&&root.defaultView.confirm('若同名字幕已存在，是否明確替換？');try{const value=await api('POST',`/api/subtitles/videos/${encodeURIComponent(resultVideoId)}/results/${encodeURIComponent(result.result_id)}/action`,{action:'restore',stage:result.stage,language:result.language,output_language:output||null,replace_existing:replace});const restored=value.restore_status==='restored';if(value.publish_status==='failed')status(`${restored?'結果已恢復；':''}字幕發布失敗：${value.error||'儲存原因未提供'}`,true);else if(value.publish_status==='published')status(`結果已恢復；字幕已發布${value.path?`：${value.path}`:''}。`);else status(restored?'結果已恢復；未要求發布字幕。':'上一個字幕版本已恢復。');await refresh();}catch(error){status(error.message,true);}});
        row.append(actions);box.append(row);
      }
    }
    function renderPublications(){
      const box=byId('subtitle-publications');if(!box||box.contains(root.activeElement))return;box.replaceChildren();
      if(!state.publications.length){box.append(el('p','尚無字幕發布紀錄。','text-sm text-gray-400'));return;}
      for(const publication of state.publications){
        const row=el('div','', 'flex flex-wrap items-center justify-between gap-2 rounded border border-[#3e3e4e] p-3');
        const source=state.videos.find(video=>video.video_id===publication.video_id);const title=source?.title||source?.name||publication.video_id;
        row.append(el('span',`${title} · ${label(languageLabels,publication.language)} · ${label(publicationLabels,publication.publish_status)}${publication.created_at?` · ${readableDate(publication.created_at)}`:''}${publication.error?` · ${publication.error}`:''}`,'break-words text-xs'));
        if(publication.publish_status==='failed'){
          const retry=el('button','只重試字幕儲存','min-h-10 rounded border border-[#3e3e4e] px-3 text-xs');retry.type='button';
          retry.dataset.testid=`subtitle-publication-retry-${publication.publication_id}`;retry.addEventListener('click',()=>runOnce(`publication:${publication.publication_id}`,retry,async()=>{try{const value=await api('POST',`/api/subtitles/videos/${encodeURIComponent(publication.video_id)}/results/${encodeURIComponent(publication.publication_id)}/action`,{action:'retry_publish'});await refresh();if(value.publish_status==='failed')status(`字幕儲存仍失敗：${value.error||'原因未提供'}`,true);else status('字幕儲存重試成功。');}catch(error){status(error.message,true);}}));row.append(retry);
        }
        box.append(row);
      }
    }
    async function submitSelected(){
      let spec;try{spec=requestBody();if(!spec)throw new Error('請至少選擇一個字幕階段。');}catch(error){status(error.message,true);return;}
      const videoIds=[...state.selected];if(!videoIds.length){status('請先選擇至少一部影片。',true);return;}
      const videoNames=videoIds.map(id=>{const video=state.videos.find(item=>item.video_id===id);return video?.title||video?.name||id;});
      const stageModels=spec.selected_stages.map(stage=>{const id=spec.profile_ids[stage]||spec.profile_ids.text;const profile=settings.state.profiles.find(item=>item.profile_id===id);return `${label(stageLabels,stage)}：${profile?.display_name||'設定檔'} (${profile?.provider||'服務未知'} / ${profile?.model||'模型未知'})`;});
      const summary=byId('subtitle-submit-summary');if(summary)summary.textContent=`影片：${videoNames.join('、')}。階段與模型：${stageModels.join('；')}。辨識來源語言：日文；翻譯目標語言：${spec.target_languages.join('、')||'未選擇'}；輸出字幕語言：${spec.output_languages.join('、')||'僅保存內部結果'}；額外思考${spec.allow_reasoning?'已明確允許':'停用或未知時由服務規則保護'}。各階段會使用目前設定檔快照。`;
      const errorNode=byId('subtitle-confirm-error');if(errorNode)errorNode.textContent='';byId('subtitle-confirm-dialog')?.showModal();
    }
    async function confirmSubmit(){
      if(submitting)return;
      let spec;try{spec=requestBody();if(!spec)throw new Error('請至少選擇一個字幕階段。');}catch(error){status(error.message,true);return;}
      const items=[...state.selected].map(video_id=>({video_id,...spec}));
      submitting=true;status('');const button=byId('subtitle-confirm-submit'),wasFocused=root.activeElement===button;let failed=false;if(button)button.disabled=true;
      try{const data=await api('POST','/api/subtitles/tasks',{items});const accepted=data.items||[];const failed=accepted.filter(item=>!item.success);state.selected=new Set(failed.map(item=>item.video_id).filter(Boolean));if(failed.length)status(`已提交 ${accepted.length-failed.length} 項；${failed.length} 項失敗：${failed.map(item=>item.error).join('；')}`,true);else status(`已提交 ${accepted.length} 項字幕任務。`);byId('subtitle-confirm-dialog')?.close();await refresh();}
      catch(error){failed=true;status(error.message,true);}
      finally{submitting=false;if(button)button.disabled=false;if(failed)restoreFailedDialogActionFocus('subtitle-confirm-dialog',button,wasFocused);}
    }
    async function refresh({scanVideos=false}={}){
      if(refreshInFlight){await refreshInFlight;if(scanVideos)return refresh({scanVideos:true});return state;}
      refreshInFlight=(async()=>{try{const [videos,tasks]=await Promise.all([scanVideos?api('GET','/api/subtitles/videos'):Promise.resolve(null),api('GET','/api/subtitles/tasks')]);if(videos){state.videos=videos.videos||[];state.videoErrors=videos.errors||[];state.videoPartial=videos.partial===true;state.videoNotice=videos.notice||null;settings.state.localVad=videos.capabilities?.local_vad||settings.state.localVad;renderVideos();renderVideoErrors();}state.tasks=tasks.tasks||[];state.publications=tasks.publications||[];renderTasks();renderPublications();const selector=byId('subtitle-result-video');if(scanVideos&&selector&&!selector.contains(root.activeElement)){const previous=selector.value;selector.replaceChildren();const known=new Set();for(const video of state.videos){const option=el('option',video.title||video.name||video.video_id);option.value=video.video_id;selector.append(option);known.add(video.video_id);}for(const task of state.tasks){if(known.has(task.video_id))continue;const option=el('option',`${task.video_id} · 來源可能離線`);option.value=task.video_id;selector.append(option);known.add(task.video_id);}if([...selector.options].some(option=>option.value===previous))selector.value=previous;else if(selector.options.length)selector.value=selector.options[0].value;}if(state.activeVideo)await loadResults(state.activeVideo);return state;}catch(error){status(error.message,true);return state;}})();
      try{return await refreshInFlight;}finally{refreshInFlight=null;}
    }
    function bind(){
      byId('subtitle-submit')?.addEventListener('click',submitSelected);
      byId('subtitle-confirm-submit')?.addEventListener('click',event=>{event.preventDefault();confirmSubmit();});
      byId('subtitle-confirm-cancel')?.addEventListener('click',()=>byId('subtitle-confirm-dialog')?.close());
      byId('subtitle-refresh')?.addEventListener('click',()=>refresh({scanVideos:true}));
      byId('subtitle-load-results')?.addEventListener('click',()=>{const id=byId('subtitle-result-video')?.value;if(id)loadResults(id);});
      byId('subtitle-redo-cancel')?.addEventListener('click',()=>byId('subtitle-redo-dialog')?.close());
      byId('subtitle-redo-confirm')?.addEventListener('click',event=>{event.preventDefault();redo();});
      const redoDialog=byId('subtitle-redo-dialog');redoDialog?.addEventListener('close',()=>{setTimeout(()=>{renderTasks(true);renderResults();if(redoTriggerTestId){const trigger=[...byId('subtitle-task-list')?.querySelectorAll('[data-testid]')||[]].find(node=>node.dataset.testid===redoTriggerTestId);trigger?.focus();}redoTriggerTestId=null;},0);});
      for(const id of ['subtitle-task-list','subtitle-results'])byId(id)?.addEventListener('focusout',()=>{queueMicrotask(()=>{if(id==='subtitle-task-list'&&pendingTaskRender)renderTasks();if(id==='subtitle-results'&&pendingResultsRender)renderResults();});});
      root.defaultView?.setInterval(()=>{const panel=byId('tab-subtitles');if(!root.hidden&&panel&&!panel.classList.contains('hidden'))refresh();},3000);
      return {refresh,renderVideos,renderTasks,loadResults};
    }
    return {state,refresh,submitSelected,confirmSubmit,loadResults,bind};
  };
})();
