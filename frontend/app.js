const state = {
  projects: [],
  currentProject: null,
  currentTask: null,
  pollTimer: null,
};

const $ = (id) => document.getElementById(id);
const moduleLabels = {
  research_brief: '研究素材整理',
  outline: '论文大纲',
  evidence_map: '证据地图',
  section_draft: '分章节草稿',
  citation_check: '引用核验',
  export: '编辑与导出',
};

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function showToast(message, isError = false) {
  const toast = document.createElement('div');
  toast.textContent = message;
  toast.style.cssText = `position:fixed;right:26px;bottom:24px;z-index:20;padding:12px 16px;border-radius:10px;color:#fff;background:${isError ? '#d85858' : '#263a59'};font-size:12px;box-shadow:0 10px 30px rgba(0,0,0,.16)`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2600);
}

function formatStatus(status) {
  return ({ draft: '草稿', processing: '处理中', ready: '已就绪', error: '有错误', queued: '排队中', running: '执行中', completed: '已完成' })[status] || status || '—';
}

function openProjectModal() { $('projectModal').classList.remove('hidden'); $('newProjectTitle').focus(); }
function closeProjectModal() { $('projectModal').classList.add('hidden'); }

async function loadProjects() {
  try {
    const data = await request('/api/projects');
    state.projects = data.projects || [];
    renderProjectList();
    if (!state.currentProject && state.projects.length) await selectProject(state.projects[0].id);
    $('serviceStatus').textContent = '工作台服务在线';
    document.querySelector('.status-dot').style.background = '#21a179';
  } catch (error) {
    $('serviceStatus').textContent = '服务未连接';
    document.querySelector('.status-dot').style.background = '#d85858';
    showToast(error.message, true);
  }
}

function renderProjectList() {
  const container = $('projectList');
  if (!state.projects.length) {
    container.innerHTML = '<div class="empty-sidebar">还没有项目</div>';
    return;
  }
  container.innerHTML = state.projects.map(project => `
    <div class="project-item ${state.currentProject?.id === project.id ? 'active' : ''}" data-project-id="${project.id}">
      <div class="project-item-title">${escapeHtml(project.title)}</div>
      <div class="project-item-meta">${formatStatus(project.status)} · ${project.updated_at || '刚刚'}</div>
    </div>`).join('');
  container.querySelectorAll('[data-project-id]').forEach(item => item.addEventListener('click', () => selectProject(item.dataset.projectId)));
}

async function createProject() {
  const title = $('newProjectTitle').value.trim() || '未命名论文项目';
  try {
    const data = await request('/api/projects', { method: 'POST', body: JSON.stringify({ title }) });
    closeProjectModal();
    $('newProjectTitle').value = '';
    state.projects.unshift(data.project);
    await selectProject(data.project.id);
    renderProjectList();
    showToast('项目创建成功');
  } catch (error) { showToast(error.message, true); }
}

async function selectProject(projectId) {
  try {
    const data = await request(`/api/projects/${projectId}`);
    state.currentProject = data.project;
    state.currentTask = data.tasks?.[0] || null;
    fillProjectForm();
    renderProjectList();
    renderWorkspace();
    if (state.currentTask) renderTask(state.currentTask);
  } catch (error) { showToast(error.message, true); }
}

function fillProjectForm() {
  const project = state.currentProject;
  if (!project) return;
  $('pageTitle').textContent = project.title;
  $('pageSubtitle').textContent = '围绕当前研究项目组织证据、生成章节草稿并完成论文交付。';
  $('researchQuestion').value = project.research_question || '';
  $('methodNotes').value = project.method_notes || '';
  $('experimentNotes').value = project.experiment_notes || '';
  $('targetVenue').value = project.target_venue || '';
  $('documentPath').value = project.document_path || '';
  $('literatureFolder').value = project.literature_folder || '';
  $('formatRule').value = project.format_rule || '';
  $('projectStatus').textContent = formatStatus(project.status);
  $('summaryProjectStatus').textContent = formatStatus(project.status);
}

function renderWorkspace() {
  $('emptyState').classList.add('hidden');
  $('workspace').classList.remove('hidden');
}

async function saveProject() {
  const project = state.currentProject;
  if (!project) return;
  try {
    const data = await request(`/api/projects/${project.id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        research_question: $('researchQuestion').value.trim(),
        method_notes: $('methodNotes').value.trim(),
        experiment_notes: $('experimentNotes').value.trim(),
        target_venue: $('targetVenue').value.trim(),
        document_path: $('documentPath').value.trim(),
        literature_folder: $('literatureFolder').value.trim(),
        format_rule: $('formatRule').value.trim() || '通用学术论文规范',
      }),
    });
    state.currentProject = data.project;
    $('saveHint').textContent = '研究素材已保存，可生成大纲和章节草稿';
    showToast('项目资料已保存');
  } catch (error) { showToast(error.message, true); }
}

async function startTask() {
  if (!state.currentProject) return showToast('请先创建项目', true);
  await saveProject();
  try {
    const data = await request(`/api/projects/${state.currentProject.id}/tasks`, {
      method: 'POST',
      body: JSON.stringify({ instruction: $('taskInstruction').value.trim() }),
    });
    state.currentTask = data.task;
    renderTask(state.currentTask);
    startPolling(state.currentTask.id);
    showToast('审阅任务已启动');
  } catch (error) { showToast(error.message, true); }
}

function startPolling(taskId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(() => refreshTask(taskId), 1200);
  refreshTask(taskId);
}

async function refreshTask(taskId) {
  try {
    const data = await request(`/api/tasks/${taskId}`);
    if (!data.success) throw new Error(data.error || '任务不存在');
    state.currentTask = data.task;
    renderTask(state.currentTask);
    if (['completed', 'error'].includes(state.currentTask.status)) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      await loadProjects();
    }
  } catch (error) {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
    showToast(error.message, true);
  }
}

function renderTask(task) {
  if (!task) return;
  const progress = Number(task.progress || 0);
  $('taskIdLabel').textContent = task.id || '暂无任务';
  $('progressValue').textContent = `${progress}%`;
  $('progressBar').style.width = `${progress}%`;
  $('taskMessage').textContent = task.message || formatStatus(task.status);
  $('summaryActiveModule').textContent = moduleLabels[task.active_module] || task.active_module || '—';
  $('taskOutput').textContent = task.output || '任务完成后，这里会显示大纲、证据地图和章节草稿。';
  $('moduleList').innerHTML = (task.modules || []).map((module, index) => `
    <div class="module-item ${module.status || 'waiting'}">
      <div class="module-index">${String(index + 1).padStart(2, '0')}</div>
      <div><div class="module-label">${escapeHtml(module.label)}</div><div class="module-summary">${escapeHtml(module.summary || '等待执行')}</div></div>
      <div class="module-status">${formatStatus(module.status)}</div>
    </div>`).join('');
  $('activityList').innerHTML = (task.events || []).slice().reverse().map(event => `
    <div class="activity-item"><div class="activity-time">${escapeHtml(event.time || '')}</div><div class="activity-message">${escapeHtml(event.message || '')}</div></div>`).join('') || '<div class="activity-empty">暂无执行记录</div>';
  $('historyList').innerHTML = '<div class="history-item"><div class="history-item-title">当前任务</div><div class="history-item-meta">' + formatStatus(task.status) + ' · ' + (task.created_at || '') + '</div></div>';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
}

async function copyOutput() {
  const text = $('taskOutput').textContent || '';
  await navigator.clipboard?.writeText(text);
  showToast('结果已复制');
}

async function checkHealth() {
  try { await request('/health'); } catch (error) { showToast('后端服务未启动，请运行 python api.py', true); }
}

$('newProjectButton').addEventListener('click', openProjectModal);
$('emptyNewProjectButton').addEventListener('click', openProjectModal);
$('closeModalButton').addEventListener('click', closeProjectModal);
$('cancelModalButton').addEventListener('click', closeProjectModal);
$('createProjectButton').addEventListener('click', createProject);
$('saveProjectButton').addEventListener('click', saveProject);
$('startTaskButton').addEventListener('click', startTask);
$('refreshButton').addEventListener('click', loadProjects);
$('copyOutputButton').addEventListener('click', copyOutput);
$('projectModal').addEventListener('click', event => { if (event.target.id === 'projectModal') closeProjectModal(); });

checkHealth();
loadProjects();
