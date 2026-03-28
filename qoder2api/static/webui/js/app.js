const OVERVIEW_API = '/webui/api/overview';
const LOGS_API = '/webui/api/logs';
const ACCOUNTS_API = '/webui/api/accounts';
const MODELS_API = '/v1/models';
const SETTINGS_API = '/webui/api/settings';
const COMPLETIONS_API = '/v1/chat/completions';
const AUTH_START_API = '/auth/browser/start';
const REGISTRATION_API = '/webui/api/registration';

const PAGE_META = {
    dashboard: {
        title: '仪表盘',
        subtitle: '多账号轮询概览',
    },
    accounts: {
        title: '账号池',
        subtitle: '登录、启停、检测与多账号轮询管理',
    },
    models: {
        title: '模型与设置',
        subtitle: '模型暴露、代理配置与账号池并发限制',
    },
    playground: {
        title: '调试面板',
        subtitle: '直接验证 OpenAI 兼容输出与账号池行为',
    },
    logs: {
        title: '日志追踪',
        subtitle: '查看服务日志与网页登录过程输出',
    },
};

const state = {
    activePage: 'dashboard',
    sessionId: null,
    sessionDone: true,
    sessionPollTimer: null,
    authUrl: '',
    registrationSessionId: null,
    registrationSessionDone: true,
    registrationSessionPollTimer: null,
    registrationAuthUrl: '',
    registration: null,
    popup: null,
    models: [],
    accounts: [],
    runtimeSettings: null,
    overview: null,
    activeAccountId: null,
    busyAccountId: null,
    logsAutoScroll: true,
};

function $(id) {
    return document.getElementById(id);
}

function setText(id, value) {
    const element = $(id);
    if (element) {
        element.textContent = value;
    }
}

function setDataState(id, stateValue) {
    const element = $(id);
    if (!element) {
        return;
    }
    if (stateValue) {
        element.dataset.state = stateValue;
    } else {
        delete element.dataset.state;
    }
}

function resolveRuntimeStreamSetting(settings, key) {
    if (typeof settings?.[key] === 'boolean') {
        return settings[key];
    }
    return settings?.default_stream !== false;
}

function setToggleSwitchState(id, enabled) {
    const element = $(id);
    if (!element) {
        return;
    }
    const isEnabled = Boolean(enabled);
    element.dataset.enabled = isEnabled ? '1' : '0';
    element.setAttribute('aria-checked', isEnabled ? 'true' : 'false');
    const stateLabel = element.querySelector('.switch-toggle-state');
    if (stateLabel) {
        stateLabel.textContent = isEnabled ? '已开启' : '已关闭';
    }
}

function getToggleSwitchState(id) {
    return $(id)?.dataset.enabled === '1';
}

function bindToggleSwitch(id) {
    const element = $(id);
    if (!element) {
        return;
    }
    const toggle = () => setToggleSwitchState(id, !getToggleSwitchState(id));
    element.addEventListener('click', toggle);
    element.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') {
            return;
        }
        event.preventDefault();
        toggle();
    });
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escapeHtmlAttribute(value) {
    return escapeHtml(value).replace(/\n/g, '&#10;');
}

function showToast(message, type = 'info') {
    const container = $('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'slideIn 0.28s ease reverse';
        setTimeout(() => toast.remove(), 280);
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

function updatePageMeta(page) {
    const meta = PAGE_META[page] || PAGE_META.dashboard;
    setText('page-title', meta.title);
    setText('page-subtitle', meta.subtitle);
}

function syncAuthUrlInput() {
    const currentAuthUrl = state.registrationAuthUrl || state.authUrl || '';
    const authUrlInput = $('auth-url');
    const openAuthUrlButton = $('open-auth-url-btn');
    const copyAuthUrlButton = $('copy-auth-url-btn');
    if (!authUrlInput || !openAuthUrlButton || !copyAuthUrlButton) {
        return;
    }
    authUrlInput.value = currentAuthUrl;
    openAuthUrlButton.disabled = !currentAuthUrl;
    copyAuthUrlButton.disabled = !currentAuthUrl;
}

function clearAuthUrlInput() {
    state.authUrl = '';
    syncAuthUrlInput();
}

function setBadgeTone(id, text, tone) {
    const element = $(id);
    if (!element) {
        return;
    }
    element.className = `table-badge ${tone}`;
    element.textContent = text;
}

function resolveRegistrationStatusMeta(status) {
    switch (String(status || '').toLowerCase()) {
        case 'success':
            return { text: '完成', tone: 'tone-success' };
        case 'ready':
            return { text: '入池中', tone: 'tone-success' };
        case 'starting':
        case 'starting_browser_login':
        case 'registering':
        case 'oauth_signing_in':
            return { text: '进行中', tone: 'tone-warning' };
        case 'cancelled':
            return { text: '已取消', tone: 'tone-muted' };
        case 'error':
            return { text: '失败', tone: 'tone-danger' };
        default:
            return { text: '空闲', tone: 'tone-muted' };
    }
}

function updateRegistrationActionState() {
    const startRegistrationButton = $('start-registration-btn');
    const cancelRegistrationButton = $('cancel-registration-btn');
    const startLoginButton = $('start-login-btn');
    if (!startRegistrationButton || !cancelRegistrationButton || !startLoginButton) {
        return;
    }
    const registrationBusy = !state.registrationSessionDone;
    const browserLoginBusy = !state.sessionDone;
    const available = Boolean(state.registration?.available);
    startRegistrationButton.disabled = !available || registrationBusy || browserLoginBusy;
    cancelRegistrationButton.disabled = !registrationBusy;
    startLoginButton.disabled = registrationBusy || browserLoginBusy;
}

function switchPage(page) {
    state.activePage = page;

    document.querySelectorAll('.nav-item').forEach((item) => {
        item.classList.toggle('active', item.dataset.page === page);
    });

    document.querySelectorAll('.section').forEach((section) => {
        section.classList.toggle('active', section.id === `${page}-section`);
    });

    updatePageMeta(page);
    if (page !== 'accounts' && state.sessionDone && state.registrationSessionDone) {
        clearAuthUrlInput();
    }
}

function renderLoginState(session, auth = {}) {
    const previousAuthUrl = state.authUrl;
    state.sessionId = session?.session_id || null;
    state.sessionDone = !(session && !session.done);
    state.authUrl = session && !session.done ? (session.auth_url || '') : '';

    if (
        state.popup &&
        !state.popup.closed &&
        state.authUrl &&
        previousAuthUrl &&
        previousAuthUrl !== state.authUrl
    ) {
        try {
            state.popup.location.replace(state.authUrl);
        } catch (error) {
            console.warn('failed to refresh login popup url', error);
        }
    }

    syncAuthUrlInput();
    updateRegistrationActionState();

    if (session) {
        $('login-terminal-output').textContent = session.terminal_output || '等待网页登录状态更新...';
    } else if (auth.logged_in_hint) {
        $('login-terminal-output').textContent = '未检测到活跃登录会话；当前本地存在登录态。';
    } else {
        $('login-terminal-output').textContent = '尚未启动登录会话。';
    }

    autoScrollLogsIfNeeded();
}

function renderRegistration(payload = {}) {
    const registration = payload || {};
    const session = registration.session || null;
    const statusMeta = resolveRegistrationStatusMeta(session?.status);
    const available = Boolean(registration.available);
    const availabilityText = available ? '已就绪' : '未就绪';
    const availabilityBadgeText = available ? '注册 Worker 已就绪' : '注册 Worker 不可用';

    state.registration = registration;
    state.registrationSessionId = session?.session_id || null;
    state.registrationSessionDone = !(session && !session.done);
    state.registrationAuthUrl = session && !session.done ? (session.auth_url || '') : '';

    const registrationTerminalOutput = $('registration-terminal-output');
    if (!registrationTerminalOutput) {
        syncAuthUrlInput();
        updateRegistrationActionState();
        return;
    }

    setText('registration-availability-text', availabilityText);
    setText('registration-session-status', statusMeta.text);
    setText('registration-current-email', session?.current_email || '—');
    setText(
        'registration-session-counts',
        session ? `${Number(session.success_count || 0)} / ${Number(session.requested_count || 0)}` : '0 / 0',
    );
    setText(
        'registration-session-meta',
        session?.message || registration.message || '尚未启动自动注册流程。',
    );
    registrationTerminalOutput.textContent = session?.terminal_output || registration.message || '尚未启动自动注册流程。';
    scrollTerminalToBottom('registration-terminal-output');
    setBadgeTone('registration-availability-badge', availabilityBadgeText, available ? 'tone-success' : 'tone-warning');
    setBadgeTone('registration-session-badge', statusMeta.text, statusMeta.tone);

    syncAuthUrlInput();
    updateRegistrationActionState();
}

function updateTopbarSummary() {
    const accountsPayload = state.overview?.accounts || { total_accounts: 0, enabled_accounts: 0 };
    const health = state.overview?.health || {};
    const total = Number(accountsPayload.total_accounts || 0);
    const enabled = Number(accountsPayload.enabled_accounts || 0);
    const limit = state.runtimeSettings?.account_pool_max_concurrency ?? health.account_pool_max_concurrency ?? '--';
    const active = health.active_requests ?? 0;
    const summary = `${enabled}/${total} 启用 · ${active} 活跃 · 上限 ${limit}`;
    const summaryElement = $('topbar-summary');
    setText('topbar-summary', summary);
    if (summaryElement?.parentElement) {
        if (enabled > 0) {
            summaryElement.parentElement.dataset.state = 'ready';
        } else {
            summaryElement.parentElement.dataset.state = 'warning';
        }
    }
    setDataState('topbar-summary', enabled > 0 ? 'ready' : 'warning');
}

function resolvePlanTone(planText) {
    const text = String(planText || '').toLowerCase();
    if (!text || text === '未检测') {
        return 'tone-muted';
    }
    if (text.includes('ultra') || text.includes('pro+')) {
        return 'tone-success';
    }
    if (text.includes('trial') || text.includes('pro')) {
        return 'tone-success';
    }
    if (text.includes('free')) {
        return 'tone-neutral';
    }
    return 'tone-muted';
}

function resolveQuotaState(account) {
    const creditUsageError = String(account?.credit_usage_error || '').trim();
    if (creditUsageError) {
        return { text: '额度查询失败', tone: 'tone-warning' };
    }
    if (account?.is_quota_exceeded === true) {
        return { text: '额度受限', tone: 'tone-warning' };
    }
    if (account?.is_quota_exceeded === false) {
        return { text: '额度正常', tone: 'tone-success' };
    }
    return { text: '额度未知', tone: 'tone-muted' };
}

function iconSvg(name) {
    const icons = {
        detect: '<svg viewBox="0 0 24 24" fill="none"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6Z"/><circle cx="12" cy="12" r="3"/></svg>',
        test: '<svg viewBox="0 0 24 24" fill="none"><path d="M4 5h16v14H4z"/><path d="m8 10 3 2-3 2"/><path d="M13 16h3"/></svg>',
        rename: '<svg viewBox="0 0 24 24" fill="none"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 1 1 3 3L7 19l-4 1 1-4 12.5-12.5Z"/></svg>',
        toggle_on: '<svg viewBox="0 0 24 24" fill="none"><rect x="2" y="7" width="20" height="10" rx="5"/><circle cx="16" cy="12" r="3"/></svg>',
        toggle_off: '<svg viewBox="0 0 24 24" fill="none"><rect x="2" y="7" width="20" height="10" rx="5"/><circle cx="8" cy="12" r="3"/></svg>',
        delete: '<svg viewBox="0 0 24 24" fill="none"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>',
    };
    return icons[name] || icons.detect;
}

function buildActionButton({ action, accountId, label, icon, disabled = false, variant = 'secondary', extraAttrs = '', title = '' }) {
    const normalizedTitle = String(title || '').trim();
    const titleAttr = normalizedTitle ? `title="${escapeHtmlAttribute(normalizedTitle)}"` : '';
    const buttonHtml = `<button type="button" class="btn btn-${variant} btn-sm btn-with-icon" data-action="${action}" data-account-id="${accountId}" ${titleAttr} ${extraAttrs} ${disabled ? 'disabled' : ''}><span class="btn-icon" aria-hidden="true">${iconSvg(icon)}</span><span>${label}</span></button>`;
    if (!normalizedTitle) {
        return buttonHtml;
    }
    return `<span class="action-tooltip-wrapper" ${titleAttr}>${buttonHtml}</span>`;
}

function loadingIconSvg() {
    return '<svg viewBox="0 0 24 24" fill="none"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>';
}

function actionLoadingLabel(action) {
    const labels = {
        detect: '刷新中',
        test: '测试中',
        rename: '保存中',
        toggle: '更新中',
        delete: '删除中',
    };
    return labels[action] || '处理中';
}

function syncAccountRowStates() {
    document.querySelectorAll('#accounts-table tbody tr[data-account-id]').forEach((row) => {
        row.classList.toggle('is-active', row.dataset.accountId === state.activeAccountId);
        row.classList.toggle('is-busy', row.dataset.accountId === state.busyAccountId);
    });
}

function setActionButtonLoading(button, label) {
    const originalHtml = button.innerHTML;
    const wasDisabled = button.disabled;
    button.disabled = true;
    button.classList.add('is-loading');
    button.innerHTML = `<span class="btn-icon" aria-hidden="true">${loadingIconSvg()}</span><span>${label}</span>`;
    return () => {
        button.innerHTML = originalHtml;
        button.disabled = wasDisabled;
        button.classList.remove('is-loading');
    };
}

function scrollTerminalToBottom(id) {
    const element = $(id);
    const container = element?.closest('.log-container');
    if (!container) {
        return;
    }
    container.scrollTop = container.scrollHeight;
}

function autoScrollLogsIfNeeded() {
    if (!state.logsAutoScroll) {
        return;
    }
    scrollTerminalToBottom('logs-output');
    scrollTerminalToBottom('login-terminal-output');
}

function updateLogsAutoscrollUi() {
    const button = $('logs-autoscroll-btn');
    const label = $('logs-autoscroll-label');
    if (!button || !label) {
        return;
    }
    button.dataset.mode = state.logsAutoScroll ? 'active' : 'paused';
    label.textContent = state.logsAutoScroll ? '暂停自动滚动' : '恢复自动滚动';
}

function toggleLogsAutoscroll() {
    state.logsAutoScroll = !state.logsAutoScroll;
    updateLogsAutoscrollUi();
    if (state.logsAutoScroll) {
        autoScrollLogsIfNeeded();
        showToast('日志区已恢复自动滚动', 'success');
    } else {
        showToast('日志区已暂停自动滚动', 'info');
    }
}

function renderOverview(data) {
    const health = data.health || {};
    const auth = data.auth || {};
    const session = data.session || null;
    const accountsPayload = data.accounts || { accounts: [], total_accounts: 0, enabled_accounts: 0 };
    const totalAccounts = Number(accountsPayload.total_accounts || 0);
    const enabledAccounts = Number(accountsPayload.enabled_accounts || 0);
    const activeRequests = Number(health.active_requests || 0);
    const concurrencyLimit = health.account_pool_max_concurrency ?? '--';
    const proxyConfigured = Boolean(state.runtimeSettings?.proxy_url || health.proxy_configured);
    const llmapiConfigured = Boolean(state.runtimeSettings?.llmapi_api_key || health.llmapi_api_key_configured);
    const quotaSummary = accountsPayload.quota_summary || {};
    const quotaTotal = toNumericQuota(quotaSummary.total);
    const quotaUsed = toNumericQuota(quotaSummary.used);
    const quotaRemaining = toNumericQuota(quotaSummary.remaining);
    const quotaUnit = String(quotaSummary.unit || '').trim();
    const quotaAccounts = Number(quotaSummary.accounts_with_quota || 0);

    state.overview = data;
    renderRegistration(data.registration || state.registration || {});

    setText('health-status', String(health.status || '--').toUpperCase());
    setText('health-version', health.version || '--');
    setText('models-count', String(health.models || 0));

    setText('dashboard-enabled-count', String(enabledAccounts));
    setText('dashboard-accounts-total', String(totalAccounts));
    setText('dashboard-active-requests', String(activeRequests));
    setText('dashboard-concurrency-limit', String(concurrencyLimit));
    setText('dashboard-quota-total', formatQuotaMetric(quotaTotal, quotaUnit));
    setText('dashboard-quota-remaining', formatQuotaMetric(quotaRemaining, quotaUnit));
    setText(
        'dashboard-quota-total-hint',
        quotaAccounts > 0
            ? `覆盖 ${quotaAccounts} 个账号${quotaUnit ? ` · ${quotaUnit}` : ''}`
            : '暂无额度数据'
    );
    setText(
        'dashboard-quota-remaining-hint',
        quotaAccounts > 0
            ? `已使用 ${formatQuotaMetric(quotaUsed, quotaUnit)}`
            : '等待状态刷新'
    );
    setText('dashboard-routing-mode', 'Round Robin');
    setText('dashboard-proxy-status', proxyConfigured ? '已配置' : '未配置');
    setText('dashboard-llmapi-status', llmapiConfigured ? '已配置' : '未配置');
    setText('dashboard-concurrency-text', `${activeRequests} / ${concurrencyLimit}`);
    setText('dashboard-concurrency-pill', `并发 ${concurrencyLimit}`);
    setText('dashboard-proxy-pill', proxyConfigured ? '代理已配置' : '代理未配置');
    setText('dashboard-monitor-status', auth.logged_in_hint ? 'POOL READY' : 'WAITING AUTH');
    setText('dashboard-monitor-accounts', `${enabledAccounts} / ${totalAccounts}`);
    setText('dashboard-monitor-active', String(activeRequests));
    setText('dashboard-monitor-models', String(Number(health.models || 0)));
    setText('dashboard-monitor-llmapi', llmapiConfigured ? '已配置' : '未配置');
    setText('dashboard-monitor-concurrency', String(concurrencyLimit));
    setText('dashboard-monitor-proxy', proxyConfigured ? '已配置' : '未配置');
    setDataState('dashboard-concurrency-pill', 'ready');
    setDataState('dashboard-proxy-pill', proxyConfigured ? 'ready' : 'warning');

    state.accounts = accountsPayload.accounts || [];
    updateTopbarSummary();
    renderLoginState(session, auth);
    renderAccountsTable(state.accounts);
}

function toPositiveInt(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        return 0;
    }
    return Math.trunc(parsed);
}

function formatAccountTimestamp(value) {
    const text = String(value || '').trim();
    if (!text) {
        return '';
    }
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) {
        return text;
    }
    return date.toLocaleString('zh-CN', { hour12: false });
}

function truncateText(value, maxLength = 72) {
    const text = String(value || '').trim();
    if (!text || text.length <= maxLength) {
        return text;
    }
    return `${text.slice(0, Math.max(maxLength - 1, 1))}…`;
}

function isAccountCoolingDown(account) {
    return Boolean(account?.cooldown_active);
}

function isAccountAutoDisabled(account) {
    return !account?.enabled && toPositiveInt(account?.consecutive_request_failures) >= 5;
}

function buildAccountFailureTitle(account) {
    const lines = [];
    const failures = toPositiveInt(account?.consecutive_request_failures);
    if (failures > 0) {
        lines.push(`连续失败 ${failures} 次`);
    }
    const cooldownUntil = formatAccountTimestamp(account?.cooldown_until);
    if (isAccountCoolingDown(account) && cooldownUntil) {
        lines.push(`冷却至 ${cooldownUntil}`);
    }
    const failedAt = formatAccountTimestamp(account?.last_request_failed_at);
    if (failedAt) {
        lines.push(`最近失败：${failedAt}`);
    }
    const error = String(account?.last_request_error || '').trim();
    if (error) {
        lines.push(`原因：${error}`);
    }
    return lines.join('\n');
}

function buildAccountStatusMeta(account) {
    const failures = toPositiveInt(account?.consecutive_request_failures);
    const cooldownUntil = formatAccountTimestamp(account?.cooldown_until);
    const failureTitle = buildAccountFailureTitle(account);

    if (isAccountAutoDisabled(account)) {
        return {
            text: '自动禁用',
            tone: 'tone-danger',
            detail: cooldownUntil ? `冷却至 ${cooldownUntil}` : (failures > 0 ? `连续失败 ${failures} 次` : ''),
            title: failureTitle || '账号已因连续失败自动禁用',
        };
    }
    if (isAccountCoolingDown(account)) {
        return {
            text: '冷却',
            tone: 'tone-warning',
            detail: cooldownUntil ? `冷却至 ${cooldownUntil}` : (failures > 0 ? `连续失败 ${failures} 次` : ''),
            title: failureTitle || '账号当前处于冷却中',
        };
    }
    if (account?.enabled) {
        return {
            text: '启用',
            tone: 'tone-success',
            detail: '',
            title: failureTitle || '账号当前可参与调度',
        };
    }
    return {
        text: '禁用',
        tone: 'tone-muted',
        detail: '',
        title: failureTitle || '账号当前已禁用',
    };
}

function buildAccountStatusText(account) {
    return buildAccountStatusMeta(account).text;
}

function normalizePlanLabel(planText) {
    const raw = String(planText || '').trim();
    const text = raw.toLowerCase();
    if (!text) {
        return '未检测';
    }
    if (text.includes('pro+')) {
        return 'Pro+';
    }
    if (text.includes('ultra')) {
        return 'Ultra';
    }
    if (text.includes('trial') || text.includes('professional_trial')) {
        return 'Pro';
    }
    if (text.includes('free')) {
        return 'Free';
    }
    if (text.includes('pro')) {
        return 'Pro';
    }
    return raw;
}

function isSyntheticAccountLabel(label) {
    const text = String(label || '').trim();
    return /^qoder\s*账号-[0-9a-f]+$/i.test(text);
}

function buildAccountIdentity(account) {
    const email = String(account.email || '').trim();
    const name = String(account.name || '').trim();
    const label = String(account.label || '').trim();
    const primary = email || name || (isSyntheticAccountLabel(label) ? '未登录账号' : label || '未命名账号');
    let secondary = '';
    if (email && name && name !== email) {
        secondary = name;
    } else if (!email && label && !isSyntheticAccountLabel(label) && label !== primary) {
        secondary = label;
    }
    return { primary, secondary };
}

function toNumericQuota(value) {
    if (typeof value === 'number' && Number.isFinite(value)) {
        return value;
    }
    if (typeof value === 'string') {
        const normalized = value.trim().replace(/,/g, '');
        if (!normalized) {
            return null;
        }
        const parsed = Number(normalized);
        if (Number.isFinite(parsed)) {
            return parsed;
        }
    }
    return null;
}

function formatQuotaAmount(value) {
    if (!Number.isFinite(value)) {
        return '--';
    }
    if (Math.abs(value) >= 1000 || Number.isInteger(value)) {
        return value.toLocaleString();
    }
    if (Math.abs(value) >= 100) {
        return value.toFixed(1).replace(/\.0$/, '');
    }
    return value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

function formatQuotaMetric(value, unit = '') {
    if (!Number.isFinite(value)) {
        return '--';
    }
    const suffix = unit ? ` ${unit}` : '';
    return `${formatQuotaAmount(value)}${suffix}`;
}

function resolveQuotaProgress(account) {
    const total = toNumericQuota(account.quota_total);
    if (!(total > 0)) {
        return null;
    }
    let used = toNumericQuota(account.quota_used);
    const remaining = toNumericQuota(account.quota_remaining);
    if (used === null && remaining !== null) {
        used = Math.max(total - remaining, 0);
    }
    if (used === null) {
        return null;
    }
    const clampedUsed = Math.min(Math.max(used, 0), total);
    const percent = Math.min(100, Math.max(0, (clampedUsed / total) * 100));
    const unit = String(account.quota_unit || '').trim();
    const suffix = unit ? ` ${unit}` : '';
    return {
        used: clampedUsed,
        total,
        percent,
        label: `已用 ${formatQuotaAmount(clampedUsed)}/${formatQuotaAmount(total)}${suffix}`,
    };
}

function buildAccountCapabilityText(account) {
    const planText = normalizePlanLabel(account.plan_tier_name || account.user_tag || account.user_type || '');
    const helperParts = [];
    const failures = toPositiveInt(account?.consecutive_request_failures);
    const cooldownUntil = formatAccountTimestamp(account?.cooldown_until);
    const failedAt = formatAccountTimestamp(account?.last_request_failed_at);
    const error = String(account?.last_request_error || '').trim();

    if (isAccountAutoDisabled(account)) {
        helperParts.push('已自动禁用');
    }
    if (isAccountCoolingDown(account) && cooldownUntil) {
        helperParts.push(`冷却至 ${cooldownUntil}`);
    }
    if (failures > 0) {
        helperParts.push(`连续失败 ${failures} 次`);
    }
    if (failedAt) {
        helperParts.push(`最近失败 ${failedAt}`);
    }
    if (error) {
        helperParts.push(`原因：${truncateText(error, 40)}`);
    }

    return {
        planText,
        quota: resolveQuotaState(account),
        quotaProgress: resolveQuotaProgress(account),
        helperText: helperParts.join(' · '),
        helperTitle: buildAccountFailureTitle(account),
    };
}

function renderAccountsTable(accounts) {
    const tbody = $('accounts-table').querySelector('tbody');
    tbody.innerHTML = '';

    if (!accounts.length) {
        tbody.innerHTML = '<tr><td colspan="4">暂无账号</td></tr>';
        return;
    }

    for (const account of accounts) {
        const tr = document.createElement('tr');
        tr.dataset.accountId = account.id;
        const identity = buildAccountIdentity(account);
        const identitySecondary = identity.secondary
            ? `<div class="table-secondary-text">${escapeHtml(identity.secondary)}</div>`
            : '';
        const actions = [];
        const capability = buildAccountCapabilityText(account);
        const statusMeta = buildAccountStatusMeta(account);
        const planTone = resolvePlanTone(capability.planText);
        const quotaProgress = capability.quotaProgress
            ? `
                <div class="quota-progress-row" aria-label="额度使用进度">
                    <div class="quota-progress-track">
                        <div class="quota-progress-fill" style="width: ${capability.quotaProgress.percent.toFixed(2)}%;"></div>
                    </div>
                    <span class="quota-progress-label">${escapeHtml(capability.quotaProgress.label)}</span>
                </div>
            `
            : '';
        const statusDetail = statusMeta.detail
            ? `<div class="table-secondary-text" title="${escapeHtmlAttribute(statusMeta.title || statusMeta.detail)}">${escapeHtml(statusMeta.detail)}</div>`
            : '';
        const capabilityHelper = capability.helperText
            ? `<div class="table-secondary-text" title="${escapeHtmlAttribute(capability.helperTitle || capability.helperText)}">${escapeHtml(capability.helperText)}</div>`
            : '';
        const testButtonTitle = (() => {
            if (!account.logged_in_hint) {
                return '账号当前没有有效登录态，无法测试';
            }
            if (isAccountCoolingDown(account)) {
                return `${statusMeta.title}，但仍可手动测试该账号`; 
            }
            if (isAccountAutoDisabled(account)) {
                return `${statusMeta.title}，可手动重新启用后再测试`;
            }
            if (!account.enabled) {
                return '账号已禁用，请先启用';
            }
            return '发送一条测试请求验证账号可用性';
        })();

        actions.push(buildActionButton({
            action: 'detect',
            accountId: account.id,
            label: '刷新状态',
            icon: 'detect',
            disabled: !account.logged_in_hint,
            title: account.logged_in_hint
                ? `实时刷新该账号的套餐、额度与登录状态${statusMeta.title ? `\n\n当前状态：${statusMeta.title}` : ''}`
                : '账号当前没有有效登录态，无法刷新状态',
        }));
        actions.push(buildActionButton({
            action: 'test',
            accountId: account.id,
            label: '测试',
            icon: 'test',
            disabled: !account.enabled || !account.logged_in_hint,
            title: testButtonTitle,
        }));

        if (account.managed) {
            actions.push(buildActionButton({
                action: 'rename',
                accountId: account.id,
                label: '改名',
                icon: 'rename',
                title: '修改该账号在账号池中的显示名称',
            }));
            actions.push(buildActionButton({
                action: 'toggle',
                accountId: account.id,
                label: account.enabled ? '停用' : '启用',
                icon: account.enabled ? 'toggle_on' : 'toggle_off',
                extraAttrs: `data-enabled="${account.enabled ? '1' : '0'}"`,
                title: account.enabled
                    ? '手动停用该账号'
                    : (isAccountAutoDisabled(account)
                        ? `${statusMeta.title}，可手动重新启用`
                        : '手动启用该账号'),
            }));
            actions.push(buildActionButton({
                action: 'delete',
                accountId: account.id,
                label: '删除',
                icon: 'delete',
                variant: 'danger',
                title: '从账号池中删除该账号',
            }));
        }

        tr.innerHTML = `
            <td>
                <div class="table-primary-text">${escapeHtml(identity.primary)}</div>
                ${identitySecondary}
            </td>
            <td class="account-status-cell">
                <span class="table-badge ${statusMeta.tone}" title="${escapeHtmlAttribute(statusMeta.title)}">${escapeHtml(statusMeta.text)}</span>
                ${statusDetail}
            </td>
            <td class="account-capability-cell">
                <div class="table-badge-row">
                    <span class="table-badge ${planTone}">${escapeHtml(capability.planText)}</span>
                    <span class="table-badge ${capability.quota.tone}">${escapeHtml(capability.quota.text)}</span>
                </div>
                ${quotaProgress}
                ${capabilityHelper}
            </td>
            <td class="account-actions-cell">
                <div class="account-actions-group">${actions.join('')}</div>
            </td>
        `;
        tbody.appendChild(tr);
    }

    syncAccountRowStates();
}

function renderModels(models) {
    state.models = models;
    const tbody = $('models-table').querySelector('tbody');
    tbody.innerHTML = '';

    if (!models.length) {
        tbody.innerHTML = '<tr><td colspan="5">暂无模型</td></tr>';
        return;
    }

    for (const model of models) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><span class="mono-inline">${escapeHtml(model.id)}</span></td>
            <td>
                <div class="table-primary-text">${escapeHtml(model.display_name || model.id)}</div>
            </td>
            <td>${Number(model.context_window || 0).toLocaleString()}</td>
            <td>${Number(model.max_output_tokens || 0).toLocaleString()}</td>
            <td>${escapeHtml(String(model.price_factor ?? '--'))}</td>
        `;
        tbody.appendChild(tr);
    }

    renderModelOptions(models);
}

function renderRuntimeSettings(payload) {
    const settings = payload?.settings || payload || {};
    const registration = payload?.registration || state.registration || {};
    const limit = payload?.limiter?.limit ?? settings.account_pool_max_concurrency ?? '--';
    const active = payload?.limiter?.active ?? 0;
    const proxyConfigured = Boolean(settings.proxy_url);
    const llmapiConfigured = Boolean(settings.llmapi_api_key);
    const defaultStreamOpenAI = resolveRuntimeStreamSetting(settings, 'default_stream_openai');
    const defaultStreamAnthropic = resolveRuntimeStreamSetting(settings, 'default_stream_anthropic');
    const debugLoggingEnabled = Boolean(settings.debug_logging_enabled);

    state.runtimeSettings = settings;
    $('settings-proxy-url').value = settings.proxy_url || '';
    $('settings-llmapi-api-key').value = settings.llmapi_api_key || '';
    $('settings-account-pool-max-concurrency').value = String(settings.account_pool_max_concurrency ?? 4);
    setToggleSwitchState('settings-default-stream-openai', defaultStreamOpenAI);
    setToggleSwitchState('settings-default-stream-anthropic', defaultStreamAnthropic);
    setToggleSwitchState('settings-debug-logging-enabled', debugLoggingEnabled);
    setText('settings-runtime-path', payload?.path || '--');
    setText(
        'settings-runtime-status',
        `当前生效并发 ${limit}，运行中 ${active}。OpenAI 默认${defaultStreamOpenAI ? '流式' : '非流式'}，Anthropic 默认${defaultStreamAnthropic ? '流式' : '非流式'}，调试日志已${debugLoggingEnabled ? '开启' : '关闭'}，代理${proxyConfigured ? '已配置' : '未配置'}，API Key${llmapiConfigured ? '已配置' : '未配置'}。`
    );
    renderRegistration(registration);

    setText('dashboard-concurrency-limit', String(limit));
    setText('dashboard-concurrency-text', `${active} / ${limit}`);
    setText('dashboard-concurrency-pill', `并发 ${limit}`);
    setText('dashboard-proxy-pill', proxyConfigured ? '代理已配置' : '代理未配置');
    setText('dashboard-proxy-status', proxyConfigured ? '已配置' : '未配置');
    setText('dashboard-llmapi-status', llmapiConfigured ? '已配置' : '未配置');
    setText('dashboard-monitor-concurrency', String(limit));
    setText('dashboard-monitor-proxy', proxyConfigured ? '已配置' : '未配置');
    setText('dashboard-monitor-llmapi', llmapiConfigured ? '已配置' : '未配置');
    setDataState('dashboard-concurrency-pill', 'ready');
    setDataState('dashboard-proxy-pill', proxyConfigured ? 'ready' : 'warning');
    updateTopbarSummary();
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
    setText(
        'logs-meta',
        data.exists ? `${data.path} · 最近 ${data.lines} 行` : `日志文件不存在：${data.path}`
    );
    $('logs-output').textContent = data.content || '暂无日志';
    autoScrollLogsIfNeeded();
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

async function loadRuntimeSettings() {
    const data = await apiRequest(SETTINGS_API);
    renderRuntimeSettings(data);
    return data;
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

function stopRegistrationPolling() {
    if (state.registrationSessionPollTimer) {
        clearInterval(state.registrationSessionPollTimer);
        state.registrationSessionPollTimer = null;
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
                await Promise.all([loadOverview(), loadAccounts(), loadModels(), loadRuntimeSettings()]);
                showToast(session.message || '登录流程已结束', session.status === 'success' ? 'success' : 'info');
            }
        } catch (error) {
            stopSessionPolling();
            showToast(error.message, 'error');
        }
    }, 1200);
}

function startRegistrationPolling() {
    stopRegistrationPolling();
    if (!state.registrationSessionId || state.registrationSessionDone) {
        return;
    }

    state.registrationSessionPollTimer = setInterval(async () => {
        try {
            const session = await apiRequest(`${REGISTRATION_API}/sessions/${state.registrationSessionId}`);
            renderRegistration({ ...(state.registration || {}), session });
            if (session.done) {
                stopRegistrationPolling();
                await Promise.all([loadOverview(), loadAccounts(), loadModels(), loadRuntimeSettings()]);
                showToast(session.message || '自动注册流程已结束', session.status === 'success' ? 'success' : 'info');
            }
        } catch (error) {
            stopRegistrationPolling();
            showToast(error.message, 'error');
        }
    }, 1500);
}

async function startBrowserLogin() {
    const startLoginButton = $('start-login-btn');
    if (startLoginButton) {
        startLoginButton.disabled = true;
    }
    clearAuthUrlInput();
    try {
        const data = await apiRequest(AUTH_START_API, {
            method: 'POST',
            body: '{}',
        });
        renderOverview(data);
        await loadAccounts();
        state.popup = null;

        const session = data.session || null;
        if (session && !session.done) {
            startSessionPolling();
        }

        showToast(session?.auth_url ? '已生成登录链接，请手动打开。' : (session?.message || '已启动登录流程'), 'success');
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        if (startLoginButton) {
            startLoginButton.disabled = false;
        }
    }
}

async function copyAuthUrl() {
    const authUrlInput = $('auth-url');
    if (!authUrlInput) {
        return;
    }
    const currentAuthUrl = authUrlInput.value.trim();
    if (!currentAuthUrl) {
        return;
    }
    try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            await navigator.clipboard.writeText(currentAuthUrl);
        } else {
            authUrlInput.removeAttribute('readonly');
            authUrlInput.focus();
            authUrlInput.select();
            authUrlInput.setSelectionRange(0, authUrlInput.value.length);
            const success = document.execCommand('copy');
            authUrlInput.setAttribute('readonly', 'readonly');
            if (!success) {
                throw new Error('当前浏览器不支持剪贴板复制');
            }
        }
        showToast('登录链接已复制到剪贴板', 'success');
    } catch (error) {
        showToast(`复制失败: ${error.message}`, 'error');
    }
}

function openAuthUrl() {
    const authUrlInput = $('auth-url');
    if (!authUrlInput) {
        return;
    }
    const currentAuthUrl = authUrlInput.value.trim();
    if (!currentAuthUrl) {
        return;
    }
    state.popup = window.open(currentAuthUrl, 'qoder-browser-login');
}

async function startRegistration() {
    const startRegistrationButton = $('start-registration-btn');
    if (startRegistrationButton) {
        startRegistrationButton.disabled = true;
    }
    try {
        const data = await apiRequest(`${REGISTRATION_API}/start`, {
            method: 'POST',
            body: '{}',
        });
        renderRegistration(data);
        if (data.session && !data.session.done) {
            startRegistrationPolling();
        }
        showToast(data.message || '自动注册流程已启动', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        updateRegistrationActionState();
    }
}

async function cancelRegistration() {
    if (!state.registrationSessionId) {
        return;
    }
    const cancelRegistrationButton = $('cancel-registration-btn');
    if (cancelRegistrationButton) {
        cancelRegistrationButton.disabled = true;
    }
    try {
        const data = await apiRequest(`${REGISTRATION_API}/sessions/${state.registrationSessionId}/cancel`, {
            method: 'POST',
            body: '{}',
        });
        renderRegistration({ ...(state.registration || {}), session: data });
        stopRegistrationPolling();
        showToast(data.message || '自动注册流程已取消', 'info');
        await Promise.all([loadOverview(), loadAccounts(), loadRuntimeSettings()]);
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        updateRegistrationActionState();
    }
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

async function saveRuntimeSettings(event) {
    event.preventDefault();

    const proxyUrl = $('settings-proxy-url').value.trim();
    const llmapiApiKey = $('settings-llmapi-api-key').value.trim();
    const concurrencyValue = $('settings-account-pool-max-concurrency').value.trim();
    const parsedConcurrency = Number(concurrencyValue || '4');
    const defaultStreamOpenAI = getToggleSwitchState('settings-default-stream-openai');
    const defaultStreamAnthropic = getToggleSwitchState('settings-default-stream-anthropic');
    const debugLoggingEnabled = getToggleSwitchState('settings-debug-logging-enabled');

    if (!Number.isInteger(parsedConcurrency) || parsedConcurrency < 1 || parsedConcurrency > 512) {
        showToast('账号池总限制并发数必须是 1 到 512 的整数', 'warning');
        return;
    }

    $('settings-save-btn').disabled = true;
    try {
        const result = await apiRequest(SETTINGS_API, {
            method: 'PUT',
            body: JSON.stringify({
                proxy_url: proxyUrl,
                llmapi_api_key: llmapiApiKey,
                account_pool_max_concurrency: parsedConcurrency,
                default_stream_openai: defaultStreamOpenAI,
                default_stream_anthropic: defaultStreamAnthropic,
                debug_logging_enabled: debugLoggingEnabled,
            }),
        });
        renderRuntimeSettings(result);
        showToast(result.message || '运行设置已保存', 'success');
        await loadOverview();
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        $('settings-save-btn').disabled = false;
    }
}

async function refreshAccountStatus(accountId, { silent = false } = {}) {
    const result = await apiRequest(`${ACCOUNTS_API}/${accountId}/status`, {
        method: 'POST',
        body: '{}',
    });
    if (!silent) {
        showToast(result.message || '账号状态已刷新', 'success');
    }
    await Promise.all([loadOverview(), loadAccounts()]);
    return result;
}

async function refreshAllAccountStatuses() {
    const button = $('accounts-refresh-status-btn');
    const restoreButton = setActionButtonLoading(button, '刷新中');
    try {
        const result = await apiRequest(`${ACCOUNTS_API}/refresh-statuses`, {
            method: 'POST',
            body: '{}',
        });
        showToast(result.message || '账号状态已批量刷新', result.success === false ? 'warning' : 'success');
        await Promise.all([loadOverview(), loadAccounts()]);
    } finally {
        restoreButton();
    }
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
        stream: false,
    };
    if (maxTokensValue) {
        payload.max_tokens = Number(maxTokensValue);
    }

    $('playground-submit-btn').disabled = true;
    $('playground-response').textContent = '请求中...';
    setText('playground-usage', '等待模型返回');

    try {
        const data = await apiRequest(COMPLETIONS_API, {
            method: 'POST',
            body: JSON.stringify(payload),
        });

        const content = data?.choices?.[0]?.message?.content || '';
        const usage = data?.usage || {};
        $('playground-response').textContent = content || '(空响应)';
        setText(
            'playground-usage',
            `model=${data.model || model} · prompt_tokens=${usage.prompt_tokens ?? '--'} · completion_tokens=${usage.completion_tokens ?? '--'} · total_tokens=${usage.total_tokens ?? '--'}`
        );
        showToast('请求完成', 'success');
    } catch (error) {
        $('playground-response').textContent = error.message;
        setText('playground-usage', '请求失败');
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
    setText('playground-usage', '尚未发起请求');
}

async function handleAccountsTableClick(event) {
    const row = event.target.closest('tbody tr[data-account-id]');
    if (row) {
        state.activeAccountId = row.dataset.accountId;
        syncAccountRowStates();
    }

    const button = event.target.closest('button[data-action]');
    if (!button) {
        return;
    }

    const accountId = button.dataset.accountId;
    const action = button.dataset.action;
    const restoreButton = setActionButtonLoading(button, actionLoadingLabel(action));
    state.activeAccountId = accountId;
    state.busyAccountId = accountId;
    syncAccountRowStates();

    try {
        if (action === 'toggle') {
            await toggleAccount(accountId, button.dataset.enabled === '1');
        } else if (action === 'detect') {
            await refreshAccountStatus(accountId);
        } else if (action === 'test') {
            await testAccount(accountId);
        } else if (action === 'delete') {
            await deleteAccount(accountId);
        } else if (action === 'rename') {
            await renameAccount(accountId);
        }
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        restoreButton();
        state.busyAccountId = null;
        syncAccountRowStates();
    }
}

function bindEvents() {
    document.querySelectorAll('.nav-item').forEach((item) => {
        item.addEventListener('click', async (event) => {
            event.preventDefault();
            const page = item.dataset.page;
            switchPage(page);
            if (page === 'models') {
                await Promise.all([loadModels(), loadRuntimeSettings()]);
            }
            if (page === 'logs') {
                await loadLogs();
            }
            if (page === 'accounts') {
                await Promise.all([loadOverview(), loadAccounts(), loadRuntimeSettings()]);
            }
        });
    });

    $('dashboard-refresh-btn').addEventListener('click', () => loadOverview().catch((error) => showToast(error.message, 'error')));
    if ($('start-registration-btn')) {
        $('start-registration-btn').addEventListener('click', startRegistration);
    }
    if ($('cancel-registration-btn')) {
        $('cancel-registration-btn').addEventListener('click', cancelRegistration);
    }
    $('accounts-refresh-status-btn').addEventListener('click', () => refreshAllAccountStatuses().catch((error) => showToast(error.message, 'error')));
    $('accounts-refresh-btn').addEventListener('click', () => Promise.all([loadOverview(), loadAccounts()]).catch((error) => showToast(error.message, 'error')));
    $('models-refresh-btn').addEventListener('click', () => Promise.all([loadModels(), loadRuntimeSettings()]).catch((error) => showToast(error.message, 'error')));
    $('settings-refresh-btn').addEventListener('click', () => loadRuntimeSettings().catch((error) => showToast(error.message, 'error')));
    $('logs-refresh-btn').addEventListener('click', () => loadLogs().catch((error) => showToast(error.message, 'error')));
    $('logs-autoscroll-btn').addEventListener('click', toggleLogsAutoscroll);
    $('logs-lines-select').addEventListener('change', () => loadLogs().catch((error) => showToast(error.message, 'error')));
    bindToggleSwitch('settings-default-stream-openai');
    bindToggleSwitch('settings-default-stream-anthropic');
    bindToggleSwitch('settings-debug-logging-enabled');
    if ($('start-login-btn')) {
        $('start-login-btn').addEventListener('click', startBrowserLogin);
    }
    if ($('open-auth-url-btn')) {
        $('open-auth-url-btn').addEventListener('click', openAuthUrl);
    }
    if ($('copy-auth-url-btn')) {
        $('copy-auth-url-btn').addEventListener('click', copyAuthUrl);
    }
    $('playground-form').addEventListener('submit', submitPlayground);
    $('runtime-settings-form').addEventListener('submit', saveRuntimeSettings);
    $('playground-clear-btn').addEventListener('click', clearPlayground);
    $('accounts-table').addEventListener('click', handleAccountsTableClick);
}

async function init() {
    updatePageMeta(state.activePage);
    updateLogsAutoscrollUi();
    clearAuthUrlInput();
    bindEvents();
    try {
        await Promise.all([loadOverview(), loadAccounts(), loadModels(), loadRuntimeSettings()]);
        if (state.sessionId && !state.sessionDone) {
            startSessionPolling();
        }
        if (state.registrationSessionId && !state.registrationSessionDone) {
            startRegistrationPolling();
        }
    } catch (error) {
        showToast(error.message, 'error');
    }
}

init();
