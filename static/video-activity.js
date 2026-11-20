/* One local reservation per video. The API owns the cross-tab/process lock. */
(() => {
  "use strict";
  const active = new Map();
  const detections = new Map();
  const names = {"render-preview":"Gerando prévia", "render-final":"Exportando vídeo final",
    "timeline-preview":"Gerando prévia da montagem", "timeline-final":"Exportando montagem",
    "detect-faces":"Procurando rostos", "transcribe-local":"Transcrevendo vídeo",
    analyze:"Analisando vídeo", "prepare-after-live":"Preparando gravação",
    "download-vod":"Preparando vídeo importado", "publish-youtube":"Publicando no YouTube", edit:"Salvando alteração", check:"Verificando atividade"};
  const changed = job => window.dispatchEvent(new CustomEvent("video-activity", {detail:{id:job.id}}));
  const busy = id => active.has(id);
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

  function reserve(id, kind) {
    if (!id || busy(id)) return null;
    const job = {id, kind, taskId:null, uncertain:false, message:names[kind] || "Processando vídeo"};
    active.set(id, job);
    changed(job);
    return job;
  }
  function release(job) {
    if (job && !job.uncertain && active.get(job.id) === job) {
      active.delete(job.id);
      changed(job);
    }
  }
  async function read(url) {
    const response = await fetch(url, {credentials:"same-origin",cache:"no-store", signal:AbortSignal.timeout(15000)});
    if (!response.ok) throw Object.assign(new Error("Falha ao consultar a tarefa."), {status:response.status});
    return response.json();
  }
  function pendingKind(id, value) {
    const key = "autoclips.pending-submit."+id;
    try {
      if (value === undefined) return sessionStorage.getItem(key);
      if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key,value);
    } catch (_) { /* Manual recovery still works when session storage is disabled. */ }
    return null;
  }
  function unknownSubmission(job) {
    job.unknownSubmission = true;
    job.uncertain = true;
    pendingKind(job.id, job.kind);
    job.message = "O pedido pode ter chegado ao servidor. Aguarde e atualize a atividade antes de tentar novamente.";
    changed(job);
  }
  function uncertain(job) {
    job.uncertain = true;
    job.message = "Não foi possível confirmar a atividade do vídeo. Atualize para verificar.";
    changed(job);
  }
  async function track(job, taskId, onProgress) {
    if (!taskId) {
      if (job.submitted) unknownSubmission(job);
      throw new Error("O servidor não retornou o identificador da tarefa. Atualize a atividade.");
    }
    job.taskId = taskId;
    let failures = 0;
    while (true) {
      let task;
      try {
        task = await read('/api/tasks/'+encodeURIComponent(taskId));
        failures = 0;
      } catch (error) {
        if (error.status === 404 || ++failures >= 3) {
          uncertain(job);
          throw new Error(job.message);
        }
        job.message = "Reconectando ao processamento…";
        changed(job);
        await delay(1250);
        continue;
      }
      if (["done","error","interrupted","failed"].includes(task.status)) {
        if (job.kind === "detect-faces" && task.status === "done") detections.set(job.id,task.result);
        await checkNext(job);
        if (task.status !== "done") throw new Error(task.error || "A tarefa foi interrompida.");
        return task.result;
      }
      const percent = Number(task.progress);
      const label = task.message || (task.status === "queued" ? "Aguardando na fila" : names[job.kind] || "Processando vídeo");
      job.message = label + (task.progress != null && Number.isFinite(percent) ? ' · '+Math.round(percent)+'%' : '');
      changed(job);
      if (onProgress) onProgress(job.message);
      await delay(document.hidden ? 5000 : 1250);
    }
  }
  const settled = (job, result, error) => window.dispatchEvent(new CustomEvent("video-task-restored",
    {detail:{id:job.id,kind:job.kind,result,error}}));

  function resume(job, task) {
    job.kind = task.kind;
    job.message = task.message || names[job.kind] || "Processando vídeo";
    changed(job);
    track(job,task.task_id).then(
      result => { release(job); settled(job,result,""); },
      error => { release(job); settled(job,null,error.message); }
    );
  }
  async function checkNext(job) {
    // Another queued publication can still occupy this video after the first
    // reaches a terminal state. Keep controls locked across the handover.
    try {
      const {activity} = await read('/api/videos/'+encodeURIComponent(job.id)+'/activity');
      if (active.get(job.id) !== job || !activity) return;
      if (activity.task_id === job.taskId) { uncertain(job); return; }
      const next = {id:job.id, kind:activity.kind, uncertain:false};
      active.set(job.id,next);
      resume(next,activity);
    } catch (_) { uncertain(job); }
  }
  async function refresh(id, manual = false) {
    const previous = active.get(id);
    if (!id || (previous && !previous.uncertain)) return;
    const pending = pendingKind(id) || (previous?.unknownSubmission && previous.kind);
    if (pending && !manual) {
      if (!previous) unknownSubmission(reserve(id,pending));
      return;
    }
    if (previous) active.delete(id);
    const job = reserve(id,"check");
    if (previous?.taskId) {
      job.kind = previous.kind;
      job.taskId = previous.taskId;
    }
    try {
      const result = await read('/api/videos/'+encodeURIComponent(id)+'/activity');
      if (!result.activity && pending && !previous?.checkedIdle) {
        job.kind = pending;
        job.unknownSubmission = true;
        job.checkedIdle = true;
        job.uncertain = true;
        job.message = "Nenhuma tarefa ativa foi encontrada. A resposta do pedido anterior se perdeu. Confira o resultado antes de liberar novas ações.";
        changed(job);
        return;
      }
      if (pending) pendingKind(id,null);
      if (!result.activity) {
        // The active registry only lists unfinished tasks. Check the known task
        // for its actual outcome if it finished while this page was offline.
        let task = null;
        if (job.taskId) {
          try { task = await read('/api/tasks/'+encodeURIComponent(job.taskId)); }
          catch (error) { if (error.status !== 404) throw error; }
        }
        if (job.kind === "detect-faces" && task?.status === "done") detections.set(id,task.result);
        release(job);
        if (previous) {
          const message = job.taskId && task?.status !== "done" ? task?.error || "A tarefa não está mais ativa. Confira os arquivos disponíveis." : "";
          settled(job, task?.result, message);
        }
        return;
      }
      resume(job,result.activity);
    } catch (_) { if (pending) { job.kind = pending; unknownSubmission(job); } else uncertain(job); }
  }
  function failure(job, error) {
    // The storage preflight returns 507 before a task can be created.
    if (!job || job.taskId || error?.status === 507) return;
    if (error?.status === 409) {
      // The request was refused. Find the operation that holds the server lock.
      uncertain(job);
      void refresh(job.id);
    } else if (job.submitted && (!error?.status || error.status >= 500)) {
      // An immediate idle read could race the POST. Only a user-initiated
      // status check clears this uncertainty, including after a page reload.
      unknownSubmission(job);
    }
  }
  function paint(id) {
    const box = document.getElementById("video-activity");
    if (!box) return;
    const job = active.get(id);
    box.hidden = !job;
    box.querySelector("[data-message]").textContent = job?.message || "";
    const retry = box.querySelector("button");
    retry.hidden = !job?.uncertain;
    retry.textContent = job?.checkedIdle ? "Conferi o resultado. Liberar ações" : "Atualizar atividade";
    retry.onclick = () => refresh(id,true);
  }
  window.VideoActivity = {reserve, release, track, refresh, failure, busy, paint, detection:id => detections.get(id)};
})();
