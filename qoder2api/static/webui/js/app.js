const OVERVIEW_API = '/webui/api/overview';
const LOGS_API = '/webui/api/logs';
const ACCOUNTS_API = '/webui/api/accounts';
const MODELS_API = '/v1/models';
const COMPLETIONS_API = '/v1/chat/completions';
const AUTH_STATUS_API = '/auth/status';
const AUTH_START_API = '/auth/browser/start';

const state = {
    activePage: 'dashboard',
    sessionId: null,
    sessionDone: true,
    sessionPollTimer: null,
    authUrl: '',
    popup: null,
    models: [],
    accounts: [],
};

function $(id) {
    return document.getElementById(id);
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function showToast(message, type = 'info') {
    const container = $('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'slideIn 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

async function apiRequest(url, options = {}) {
    const headers = {
        'Content-Type': 'application/json',
        ...(options.headers || {}),
    };

    const response = await fetch(url, {
        ...options,
        headers,
        credentials: 'same-origin',
    });

    const raw = await response.text();
    let data = {};
    if (raw) {
        try {
            data = JSON.parse(raw);
        } catch {
            data = { raw };
        }
    }

    if (!response.ok) {
        const message = data?.error?.message || data?.detail || data?.raw || `请求失败 (${response.status})`;
        throw new Error(message);
    }

    return data;
}

function setStatusBadge(element, status, text) {
    if (!element) return;
    element.textContent = text;
    element.classList.remove('running', 'stopped', 'starting');

    if (['success', 'ok', 'logged_in', 'ready'].includes(status)) {
        element.classList.add('running');
        return;
    }

    if (['waiting_browser', 'needs_input', 'starting', 'pending_login'].includes(status)) {
        element.classList.add('starting');
        return;
    }

    element.classList.add('stopped');
}

function switchPage(page) {
    state.activePage = page;

    document.querySelectorAll('.nav-item').forEach((item) => {
        item.classList.toggle('active', item.dataset.page === page);
    });

    document.querySelectorAll('.section').forEach((section) => {
        section.classList.toggle('active', section.id === `${page}-section`);
    });

    const activeNav = document.querySelector(`.nav-item[data-page="${page}"] span:last-child`);
    $('page-title').textContent = activeNav ? activeNav.textContent : 'qoder2api';
}

function updateSessionControls(session) {
    const active = Boolean(session && !session.done);
    const canInput = Boolean(active && session.input_enabled);
    $('cancel-login-btn').disabled = !active;
    $('login-up-btn').disabled = !canInput;
    $('login-down-btn').disabled = !canInput;
    $('login-enter-btn').disabled = !canInput;
    $('login-escape-btn').disabled = !canInput;
}

function renderLoginState(session, auth = {}) {
    state.sessionId = session?.session_id || null;
    state.sessionDone = !(session && !session.done);
    state.authUrl = session?.auth_url || '';

    $('auth-url').value = state.authUrl;
    $('open-auth-url-btn').disabled = !state.authUrl;
    $('copy-auth-url-btn').disabled = !state.authUrl;

    if (session) {
        $('login-terminal-output').textContent = session.terminal_output || '等待网页登录状态更新...';
    } else if (auth.logged_in_hint) {
        $('login-terminal-output').textContent = '未检测到活跃登录会话；当前本地存在登录态。';
    } else {
        $('login-terminal-output').textContent = '尚未启动登录会话。';
    }

    updateSessionControls(session);
}

function renderOverview(data) {
    const health = data.health || {};
    const auth = data.auth || {};
    const session = data.session || null;
    const accountsPayload = data.accounts || { accounts: [], total_accounts: 0, enabled_accounts: 0 };

    setStatusBadge(
        $('auth-status-badge'),
        auth.logged_in_hint ? 'logged_in' : 'missing',
        auth.logged_in_hint ? '已发现本地登录态' : '尚未登录'
    );
    $('auth-status-text').textContent = auth.logged_in_hint
        ? `账号池 ${accountsPayload.total_accounts || 0} 个，启用 ${accountsPayload.enabled_accounts || 0} 个。`
        : '当前未发现可用登录态，请去“账号池”页发起网页登录。';

    const sessionLabel = session ? session.status : 'idle';
    setStatusBadge($('session-status-badge'), sessionLabel, session ? session.status : '空闲');
    $('session-status-text').textContent = session ? session.message : '当前没有活跃登录会话';

    $('health-status').textContent = health.status || '--';
    $('health-version').textContent = health.version || '--';
    $('health-qoder-home').textContent = health.qoder_home || '--';
    $('health-workspace').textContent = health.workspace || '--';
    $('health-infer-endpoint').textContent = health.infer_endpoint || '--';
    $('health-openapi-endpoint').textContent = health.openapi_endpoint || '--';
    $('health-session-id').textContent = session?.session_id || '--';
    $('models-count').textContent = String(health.models || 0);
    $('topbar-summary').textContent = `${health.version || 'unknown'} · 账号 ${accountsPayload.enabled_accounts || 0}/${accountsPayload.total_accounts || 0}`;

    state.accounts = accountsPayload.accounts || [];
    renderLoginState(session, auth);
    renderAccountsTable(state.accounts);
}

function formatAccountType(account) {
    if (!account.managed) {
        return '外部 .qoder';
    }
    return 'Qoder Browser';
}

function buildAccountStatusText(account) {
    const flags = [];
    flags.push(account.enabled ? '启用' : '停用');
    flags.push(account.logged_in_hint ? '已登录' : '未登录');
    if (account.status) {
        flags.push(account.status);
    }
    return flags.join(' / ');
}

function renderAccountsTable(accounts) {
    const tbody = $('accounts-table').querySelector('tbody');
    tbody.innerHTML = '';

    if (!accounts.length) {
        tbody.innerHTML = '<tr><td colspan="5">暂无账号</td></tr>';
        return;
    }

    for (const account of accounts) {
        const tr = document.createElement('tr');
        const machineIdText = account.machine_id ? `<div class="table-secondary-text mono-inline">machine_id: ${escapeHtml(account.machine_id)}</div>` : '';
        const authDirText = `<div class="table-secondary-text mono-inline">${escapeHtml(account.qoder_home)}</div>`;
        const actions = [];

        actions.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="test" data-account-id="${account.id}" ${(!account.enabled || !account.logged_in_hint) ? 'disabled' : ''}>测试</button>`);

        if (account.managed) {
            actions.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="rename" data-account-id="${account.id}">改名</button>`);
            actions.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="toggle" data-account-id="${account.id}" data-enabled="${account.enabled ? '1' : '0'}">${account.enabled ? '停用' : '启用'}</button>`);
            actions.push(`<button type="button" class="btn btn-danger btn-sm" data-action="delete" data-account-id="${account.id}">删除</button>`);
        } else {
            actions.push('<span class="table-secondary-text">外部账号</span>');
        }

        tr.innerHTML = `
            <td>
                <div class="table-primary-text">${escapeHtml(account.label)}</div>
                ${machineIdText}
            </td>
            <td>${escapeHtml(formatAccountType(account))}</td>
            <td>
                <div class="table-primary-text">${escapeHtml(buildAccountStatusText(account))}</div>
                ${authDirText}
            </td>
            <td>
                <div class="table-primary-text">模型 ${escapeHtml(String(account.model_count ?? 0))} 个</div>
                <div class="table-secondary-text">创建：${escapeHtml(account.created_at || '--')}</div>
            </td>
            <td class="account-actions-cell">
                <div class="account-actions-group">${actions.join('')}</div>
            </td>
        `;
        tbody.appendChild(tr);
    }
}

function renderModels(models) {
    state.models = models;
    const tbody = $('models-table').querySelector('tbody');
    tbody.innerHTML = '';

    if (!models.length) {
        tbody.innerHTML = '<tr><td colspan="6">暂无模型</td></tr>';
        return;
    }

    for (const model of models) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><span class="mono-inline">${escapeHtml(model.id)}</span></td>
            <td>${escapeHtml(model.display_name || model.id)}</td>
            <td>${Number(model.context_window || 0).toLocaleString()}</td>
            <td>${Number(model.max_output_tokens || 0).toLocaleString()}</td>
            <td>${escapeHtml(String(model.price_factor ?? '--'))}</td>
            <td>${model.is_reasoning ? '是' : '否'}</td>
        `;
        tbody.appendChild(tr);
    }

    renderModelOptions(models);
}

function renderModelOptions(models) {
    const select = $('playground-model');
    const currentValue = select.value;
    select.innerHTML = '';

    for (const model of models) {
        const option = document.createElement('option');
        option.value = model.id;
        option.textContent = `${model.display_name || model.id} (${model.id})`;
        select.appendChild(option);
    }

    select.value = currentValue && models.some((item) => item.id === currentValue)
        ? currentValue
        : (models[0]?.id || 'auto');
}

function renderLogs(data) {
    $('logs-meta').textContent = data.exists
        ? `${data.path} · 最近 ${data.lines} 行`
        : `日志文件不存在：${data.path}`;
    $('logs-output').textContent = data.content || '暂无日志';
}

async function loadOverview() {
    const data = await apiRequest(OVERVIEW_API);
    renderOverview(data);
    return data;
}

async function loadAccounts() {
    const data = await apiRequest(ACCOUNTS_API);
    state.accounts = data.accounts || [];
    renderAccountsTable(state.accounts);
    return data;
}

async function loadModels() {
    const data = await apiRequest(MODELS_API);
    renderModels(data.data || []);
}

async function loadLogs() {
    const lines = $('logs-lines-select').value;
    const data = await apiRequest(`${LOGS_API}?lines=${encodeURIComponent(lines)}`);
    renderLogs(data);
}

function stopSessionPolling() {
    if (state.sessionPollTimer) {
        clearInterval(state.sessionPollTimer);
        state.sessionPollTimer = null;
    }
}

function startSessionPolling() {
    stopSessionPolling();
    if (!state.sessionId || state.sessionDone) {
        return;
    }

    state.sessionPollTimer = setInterval(async () => {
        try {
            const session = await apiRequest(`/auth/browser/sessions/${state.sessionId}`);
            renderLoginState(session);
            if (session.done) {
                stopSessionPolling();
                await Promise.all([loadOverview(), loadAccounts(), loadModels()]);
                showToast(session.message || '登录流程已结束', session.status === 'success' ? 'success' : 'info');
            }
        } catch (error) {
            stopSessionPolling();
            showToast(error.message, 'error');
        }
    }, 1200);
}

async function startBrowserLogin() {
    $('start-login-btn').disabled = true;
    try {
        const label = $('new-account-label').value.trim();
        const data = await apiRequest(AUTH_START_API, {
            method: 'POST',
            body: JSON.stringify({ label }),
        });
        renderOverview(data);
        await loadAccounts();

        const session = data.session || null;
        if (session?.auth_url) {
            state.popup = window.open(
                session.auth_url,
                'qoder-browser-login',
                'width=780,height=840,toolbar=no,menubar=no'
            );
        }

        if (session && !session.done) {
            startSessionPolling();
        }

        showToast(session?.message || '已启动登录流程', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        $('start-login-btn').disabled = false;
    }
}

async function refreshAuthStatus() {
    try {
        const data = await apiRequest(AUTH_STATUS_API);
        renderLoginState(data.session, data.auth);
        await Promise.all([loadOverview(), loadAccounts()]);
        if (data.session && !data.session.done) {
            startSessionPolling();
        }
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function sendLoginKey(key) {
    if (!state.sessionId) {
        showToast('当前没有活跃登录会话', 'warning');
        return;
    }

    try {
        const session = await apiRequest(`/auth/browser/sessions/${state.sessionId}/input`, {
            method: 'POST',
            body: JSON.stringify({ key }),
        });
        renderLoginState(session);
        if (!session.done) {
            startSessionPolling();
        }
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function cancelLoginSession() {
    if (!state.sessionId) {
        return;
    }

    try {
        const session = await apiRequest(`/auth/browser/sessions/${state.sessionId}/cancel`, {
            method: 'POST',
            body: '{}',
        });
        renderLoginState(session);
        stopSessionPolling();
        await Promise.all([loadOverview(), loadAccounts()]);
        showToast(session.message || '会话已取消', 'info');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function copyAuthUrl() {
    if (!state.authUrl) {
        return;
    }
    try {
        await navigator.clipboard.writeText(state.authUrl);
        showToast('登录链接已复制到剪贴板', 'success');
    } catch (error) {
        showToast(`复制失败: ${error.message}`, 'error');
    }
}

function openAuthUrl() {
    if (!state.authUrl) {
        return;
    }
    state.popup = window.open(state.authUrl, 'qoder-browser-login');
}

async function toggleAccount(accountId, currentEnabled) {
    const nextEnabled = !currentEnabled;
    const result = await apiRequest(`${ACCOUNTS_API}/${accountId}/enabled`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: nextEnabled }),
    });
    showToast(result.message || '账号状态已更新', 'success');
    await Promise.all([loadOverview(), loadAccounts()]);
}

async function testAccount(accountId) {
    const result = await apiRequest(`${ACCOUNTS_API}/${accountId}/test`, {
        method: 'POST',
        body: '{}',
    });
    const preview = result.content_preview ? `：${result.content_preview}` : '';
    showToast(result.message ? `${result.message}${preview}` : '账号测试成功', 'success');
}

async function deleteAccount(accountId) {
    if (!window.confirm('确定要删除这个账号吗？')) {
        return;
    }
    const result = await apiRequest(`${ACCOUNTS_API}/${accountId}`, {
        method: 'DELETE',
    });
    showToast(result.message || '账号已删除', 'success');
    await Promise.all([loadOverview(), loadAccounts()]);
}

async function renameAccount(accountId) {
    const account = state.accounts.find((item) => item.id === accountId);
    if (!account) {
        showToast('找不到账号', 'error');
        return;
    }
    const label = window.prompt('请输入新的账号标签', account.label || '');
    if (label === null) {
        return;
    }
    const trimmed = label.trim();
    if (!trimmed) {
        showToast('账号标签不能为空', 'warning');
        return;
    }
    const result = await apiRequest(`${ACCOUNTS_API}/${accountId}/label`, {
        method: 'PATCH',
        body: JSON.stringify({ label: trimmed }),
    });
    showToast(result.message || '账号标签已更新', 'success');
    await loadAccounts();
}

async function submitPlayground(event) {
    event.preventDefault();

    const userPrompt = $('playground-user').value.trim();
    const systemPrompt = $('playground-system').value.trim();
    const model = $('playground-model').value || 'auto';
    const maxTokensValue = $('playground-max-tokens').value.trim();

    if (!userPrompt) {
        showToast('请输入 User Prompt', 'warning');
        return;
    }

    const messages = [];
    if (systemPrompt) {
        messages.push({ role: 'system', content: systemPrompt });
    }
    messages.push({ role: 'user', content: userPrompt });

    const payload = {
        model,
        messages,
    };
    if (maxTokensValue) {
        payload.max_tokens = Number(maxTokensValue);
    }

    $('playground-submit-btn').disabled = true;
    $('playground-response').textContent = '请求中...';
    $('playground-usage').textContent = '等待模型返回';

    try {
        const data = await apiRequest(COMPLETIONS_API, {
            method: 'POST',
            body: JSON.stringify(payload),
        });

        const content = data?.choices?.[0]?.message?.content || '';
        const usage = data?.usage || {};
        $('playground-response').textContent = content || '(空响应)';
        $('playground-usage').textContent = `model=${data.model || model} · prompt_tokens=${usage.prompt_tokens ?? '--'} · completion_tokens=${usage.completion_tokens ?? '--'} · total_tokens=${usage.total_tokens ?? '--'}`;
        showToast('请求完成', 'success');
    } catch (error) {
        $('playground-response').textContent = error.message;
        $('playground-usage').textContent = '请求失败';
        showToast(error.message, 'error');
    } finally {
        $('playground-submit-btn').disabled = false;
    }
}

function clearPlayground() {
    $('playground-system').value = '';
    $('playground-user').value = '';
    $('playground-max-tokens').value = '';
    $('playground-response').textContent = '等待请求...';
    $('playground-usage').textContent = '尚未发起请求';
}

async function handleAccountsTableClick(event) {
    const button = event.target.closest('button[data-action]');
    if (!button) {
        return;
    }

    const accountId = button.dataset.accountId;
    const action = button.dataset.action;
    try {
        if (action === 'toggle') {
            await toggleAccount(accountId, button.dataset.enabled === '1');
        } else if (action === 'test') {
            await testAccount(accountId);
        } else if (action === 'delete') {
            await deleteAccount(accountId);
        } else if (action === 'rename') {
            await renameAccount(accountId);
        }
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function bindEvents() {
    document.querySelectorAll('.nav-item').forEach((item) => {
        item.addEventListener('click', async (event) => {
            event.preventDefault();
            const page = item.dataset.page;
            switchPage(page);
            if (page === 'models' && !state.models.length) {
                await loadModels();
            }
            if (page === 'logs') {
                await loadLogs();
            }
            if (page === 'accounts') {
                await loadAccounts();
            }
        });
    });

    $('dashboard-refresh-btn').addEventListener('click', () => loadOverview().catch((error) => showToast(error.message, 'error')));
    $('accounts-refresh-btn').addEventListener('click', () => loadAccounts().catch((error) => showToast(error.message, 'error')));
    $('models-refresh-btn').addEventListener('click', () => loadModels().catch((error) => showToast(error.message, 'error')));
    $('logs-refresh-btn').addEventListener('click', () => loadLogs().catch((error) => showToast(error.message, 'error')));
    $('refresh-login-btn').addEventListener('click', refreshAuthStatus);
    $('start-login-btn').addEventListener('click', startBrowserLogin);
    $('cancel-login-btn').addEventListener('click', cancelLoginSession);
    $('login-up-btn').addEventListener('click', () => sendLoginKey('up'));
    $('login-down-btn').addEventListener('click', () => sendLoginKey('down'));
    $('login-enter-btn').addEventListener('click', () => sendLoginKey('enter'));
    $('login-escape-btn').addEventListener('click', () => sendLoginKey('escape'));
    $('open-auth-url-btn').addEventListener('click', openAuthUrl);
    $('copy-auth-url-btn').addEventListener('click', copyAuthUrl);
    $('playground-form').addEventListener('submit', submitPlayground);
    $('playground-clear-btn').addEventListener('click', clearPlayground);
    $('accounts-table').addEventListener('click', handleAccountsTableClick);
}

async function init() {
    bindEvents();
    try {
        await Promise.all([loadOverview(), loadAccounts(), loadModels()]);
        if (state.sessionId && !state.sessionDone) {
            startSessionPolling();
        }
    } catch (error) {
        showToast(error.message, 'error');
    }
}

init();
