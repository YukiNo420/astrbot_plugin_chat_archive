let showMessageMedia = true;
try {
    localStorage.removeItem('astr_chat_key');
    showMessageMedia = localStorage.getItem('astr_chat_show_media') !== 'false';
} catch (_) { /* Preferences remain usable when browser storage is unavailable. */ }
let currentPage = 1;
const limit = 50;
let nextCursor = 0;
let activeSessionId = '';
let activeUserId = '';
let isHistoryLoading = false;
const SESSION_DRAWER_MAX_WIDTH = 900;
const ANALYSIS_DRAWER_MAX_WIDTH = Infinity;
let historyAbortController = null;
let authReturnFocus = null;
let historyRequestSeq = 0;
let dashboardRequestSeq = 0;
let statsRequestSeq = 0;
let sessionsRequestSeq = 0;
let appInitRequestSeq = 0;
let activeHistoryViewKey = '';
let activeSearchKeyword = '';
let highlightedMessageId = '';
let highlightedMessageTimer = null;
const avatarPreloadCache = new Map();
const avatarResolvedCache = new Map();
const formattedMsgCache = new Map();
const fullMessageFormattedCache = new Map();
const fullMessageRequests = new Map();
const CLIENT_CACHE_MAX = 1000;

function setCappedMap(map, key, value, max = CLIENT_CACHE_MAX) {
    if (map.has(key)) map.delete(key);
    map.set(key, value);
    while (map.size > max) map.delete(map.keys().next().value);
    return value;
}

function copyTextWithLegacyFallback(text) {
    const previousFocus = document.activeElement;
    const input = document.createElement('textarea');
    input.value = text;
    input.setAttribute('readonly', '');
    input.style.cssText = 'position:fixed; left:-9999px; top:0; opacity:0;';
    document.body.appendChild(input);
    input.select();
    input.setSelectionRange(0, input.value.length);
    let copied = false;
    try { copied = document.execCommand('copy'); } catch (_) { }
    input.remove();
    if (previousFocus instanceof HTMLElement) previousFocus.focus({ preventScroll: true });
    return copied;
}

function showClipboardToast(message, failed = false) {
    document.querySelectorAll('.app-toast').forEach(toast => toast.remove());
    const toast = document.createElement('div');
    toast.className = `app-toast${failed ? ' is-error' : ''}`;
    toast.setAttribute('role', failed ? 'alert' : 'status');
    toast.setAttribute('aria-live', failed ? 'assertive' : 'polite');
    toast.tabIndex = 0;
    toast.innerText = message;
    document.body.appendChild(toast);

    let remaining = failed ? 5000 : 3000;
    let startedAt = 0;
    let dismissTimer = null;
    const dismiss = () => {
        toast.classList.add('is-leaving');
        setTimeout(() => toast.remove(), 200);
    };
    const resumeDismiss = () => {
        if (toast.matches(':hover') || toast.contains(document.activeElement)) return;
        if (dismissTimer) clearTimeout(dismissTimer);
        startedAt = performance.now();
        dismissTimer = setTimeout(dismiss, remaining);
    };
    const pauseDismiss = () => {
        if (!dismissTimer) return;
        clearTimeout(dismissTimer);
        dismissTimer = null;
        remaining = Math.max(500, remaining - (performance.now() - startedAt));
    };
    toast.addEventListener('pointerenter', pauseDismiss);
    toast.addEventListener('pointerleave', resumeDismiss);
    toast.addEventListener('focusin', pauseDismiss);
    toast.addEventListener('focusout', resumeDismiss);
    resumeDismiss();
}

window.copyToClipboard = async (text) => {
    if (!text) return;
    let copied = false;
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(text);
            copied = true;
        }
    } catch (err) {
        console.warn('Clipboard API failed, trying legacy fallback', err);
    }
    if (!copied) copied = copyTextWithLegacyFallback(text);
    if (copied) showClipboardToast('已复制');
    else showClipboardToast('复制失败，请手动复制', true);
};

function makeKeyboardActivatable(element, label = '') {
    if (!element || element.dataset.keyboardActivatable === 'true') return element;
    element.dataset.keyboardActivatable = 'true';
    if (label) element.setAttribute('aria-label', label);
    const isNativeControl = element.matches('button, a[href], input, select, textarea, summary');
    if (!isNativeControl) {
        element.setAttribute('role', 'button');
        element.tabIndex = 0;
        element.onkeydown = event => {
            if (event.isComposing || (event.key !== 'Enter' && event.key !== ' ')) return;
            event.preventDefault();
            element.click();
        };
    }
    return element;
}

window.userMap = {};
window.globalTopUsers = [];
const memberPageSize = 10;
const memberInitialPageMax = 30;
const memberAutoFillMaxRequests = 6;
let sidebarMemberUsers = [];
let rankMemberUsers = [];
let memberOffset = 0;
let memberTotal = 0;
let memberHasMore = false;
let memberTotalExact = false;
let rankOffset = 0;
let rankTotal = 0;
let rankHasMore = false;
let rankTotalExact = false;
let memberSearchKeyword = '';
let memberSearchTimer = null;
let memberRequestSeq = 0;
let rankRequestSeq = 0;
let memberAutoFillPending = false;
let memberAutoFillCount = 0;
let filterStart = 0;
let filterEnd = 0;
let activeMsgType = '';
const sessionsById = new Map();

// Modern SVG Icons for chat platforms
const QQ_SVG = `<svg role="img" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><title>QQ</title><path d="M21.395 15.035a40 40 0 0 0-.803-2.264l-1.079-2.695c.001-.032.014-.562.014-.836C19.526 4.632 17.351 0 12 0S4.474 4.632 4.474 9.241c0 .274.013.804.014.836l-1.08 2.695a39 39 0 0 0-.802 2.264c-1.021 3.283-.69 4.643-.438 4.673.54.065 2.103-2.472 2.103-2.472 0 1.469.756 3.387 2.394 4.771-.612.188-1.363.479-1.845.835-.434.32-.379.646-.301.778.343.578 5.883.369 7.482.189 1.6.18 7.14.389 7.483-.189.078-.132.132-.458-.301-.778-.483-.356-1.233-.646-1.846-.836 1.637-1.384 2.393-3.302 2.393-4.771 0 0 1.563 2.537 2.103 2.472.251-.03.581-1.39-.438-4.673"/></svg>`;
const TG_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-1-.65-.35-1 .22-1.6 1.5-1.55 2.76-2.93 2.76-2.95 0-.03-.01-.16-.09-.23a.3.3 0 0 0-.23-.04c-.1.02-1.74 1.1-4.93 3.25-.47.32-.9.48-1.28.47-.42-.01-1.22-.24-1.82-.43-.73-.24-1.3-.37-1.25-.79.03-.22.3-.44.82-.67 3.2-1.39 5.34-2.3 6.42-2.73 3.05-1.22 3.68-1.43 4.1-.14.09.28.1.58.07.89z"/></svg>`;
const DISCORD_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994.021-.041.001-.09-.041-.106a13.094 13.094 0 0 1-1.873-.894.077.077 0 0 1-.008-.128c.126-.093.252-.19.372-.287a.075.075 0 0 1 .077-.011c3.92 1.793 8.18 1.793 12.061 0a.073.073 0 0 1 .078.009c.12.099.246.195.373.289a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.894.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.156-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.156 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.156-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.156 2.418z"/></svg>`;
const WECHAT_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8.283 2.167c-4.114 0-7.442 2.872-7.442 6.417 0 2.054 1.127 3.883 2.88 5.093l-.744 2.222 2.533-1.282c.84.254 1.745.385 2.773.385.556 0 1.103-.04 1.636-.118a5.955 5.955 0 0 1-.223-1.579c0-3.327 3.018-6.027 6.742-6.027.26 0 .524.015.782.042C16.31 4.218 12.639 2.167 8.283 2.167zm12.35 6.643c-3.435 0-6.223 2.47-6.223 5.518 0 3.047 2.788 5.518 6.223 5.518.736 0 1.442-.11 2.096-.316l1.97 1.037-.58-1.895c1.373-1.01 2.247-2.5 2.247-4.16 0-3.136-2.88-5.702-6.733-5.702z"/></svg>`;
const KOOK_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-2 13h-2.5l-3-3.75V16H9V8h2.5v3.25L14.5 8H17l-3.5 4.5 3.5 3.5z"/></svg>`;
const TEAMSPEAK_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>`;
const FEISHU_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2c5.52 0 10 4.48 10 10s-4.48 10-10 10S2 17.52 2 12 6.48 2 12 2zm1 5.5l-5.5 5.5H11v3.5l5.5-5.5H13V7.5z"/></svg>`;
const DINGTALK_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M2 12C2 6.48 6.48 2 12 2s10 4.48 10 10-4.48 10-10 10S2 17.52 2 12zm13.84-2.83l-3.32-.83-.83-3.32a.5.5 0 0 0-.96 0l-.83 3.32-3.32.83a.5.5 0 0 0 0 .96l3.32.83.83 3.32a.5.5 0 0 0 .96 0l.83-3.32 3.32-.83a.5.5 0 0 0 0-.96z"/></svg>`;
const FALLBACK_PLATFORM_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm8.9-6.26c-.37-.88-1.16-1.5-2.1-1.67L17 11V9c0-1.1-.9-2-2-2h-3V5c0-.55-.45-1-1-1s-1 .45-1 1v2H7V6c0-.55-.45-1-1-1s-1 .45-1 1v3.5c0 .3.13.58.35.78L7.8 12.3c.13.12.3.2.49.2H11v3c0 .55.45 1 1 1h2l.72 2.16c.1.3.3.54.58.67.28.13.6.14.89.04 1.76-.62 3.19-1.92 3.96-3.58.12-.26.1-.56-.05-.8z"/></svg>`;

const UI_ICON_PATHS = {
    search: '<circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.4-3.4"></path>',
    dashboard: '<rect x="3" y="3" width="7" height="7" rx="1"></rect><rect x="14" y="3" width="7" height="7" rx="1"></rect><rect x="3" y="14" width="7" height="7" rx="1"></rect><rect x="14" y="14" width="7" height="7" rx="1"></rect>',
    folder: '<path d="M3 6.5h6l2 2h10v9.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path><path d="M3 8.5V6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v.5"></path>',
    link: '<path d="M10 13a5 5 0 0 0 7.1.1l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1"></path><path d="M14 11a5 5 0 0 0-7.1-.1l-2 2A5 5 0 0 0 12 20l1.1-1.1"></path>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"></rect><circle cx="8.5" cy="9" r="1.5"></circle><path d="m21 15-4.5-4.5L7 20"></path>',
    video: '<rect x="3" y="5" width="14" height="14" rx="2"></rect><path d="m17 10 4-2v8l-4-2z"></path>',
    audio: '<path d="M9 18V5l11-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="17" cy="16" r="3"></circle>',
    file: '<path d="M6 2h8l4 4v16H6z"></path><path d="M14 2v5h5"></path>',
    reply: '<path d="m9 17-5-5 5-5"></path><path d="M20 18c0-4.4-3.6-8-8-8H4"></path>',
    database: '<ellipse cx="12" cy="5" rx="8" ry="3"></ellipse><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5"></path><path d="M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"></path>',
    trend: '<path d="M3 18 9 12l4 4 8-10"></path><path d="M15 6h6v6"></path>',
    distribution: '<path d="M4 19V9"></path><path d="M10 19V5"></path><path d="M16 19v-7"></path><path d="M22 19H2"></path>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M22 21v-2a4 4 0 0 0-3-3.9"></path><path d="M16 3.1a4 4 0 0 1 0 7.8"></path>',
    arrowUp: '<path d="m6 10 6-6 6 6"></path><path d="M12 4v16"></path>',
};

function uiIcon(name, className = 'ui-icon') {
    const path = UI_ICON_PATHS[name] || UI_ICON_PATHS.file;
    return `<svg class="${escapeAttr(className)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${path}</svg>`;
}

const PLATFORM_BADGE_META = {
    'qq': { name: 'QQ', class: 'badge-plat-qq', svg: QQ_SVG },
    'telegram': { name: 'Telegram', class: 'badge-plat-telegram', svg: TG_SVG },
    'discord': { name: 'Discord', class: 'badge-plat-discord', svg: DISCORD_SVG },
    'wechat': { name: '微信', class: 'badge-plat-wechat', svg: WECHAT_SVG },
    'wecom': { name: '企业微信', class: 'badge-plat-wecom', svg: WECHAT_SVG },
    'kook': { name: 'KOOK', class: 'badge-plat-kook', svg: KOOK_SVG },
    'teamspeak': { name: 'TeamSpeak', class: 'badge-plat-teamspeak', svg: TEAMSPEAK_SVG },
    'feishu': { name: '飞书', class: 'badge-plat-feishu', svg: FEISHU_SVG },
    'dingtalk': { name: '钉钉', class: 'badge-plat-dingtalk', svg: DINGTALK_SVG }
};

function getPlatformBadgeHtml(platformName) {
    if (!platformName) return '';
    const plat = platformName.toLowerCase();
    const meta = PLATFORM_BADGE_META[plat];
    if (!meta) {
        return `
            <div class="session-platform-badge" title="其他平台: ${escapeAttr(platformName)}">
                ${FALLBACK_PLATFORM_SVG}
            </div>
        `;
    }
    return `
        <div class="session-platform-badge ${meta.class}" title="${meta.name}">
            ${meta.svg}
        </div>
    `;
}


function isFriendSessionType(type = activeMsgType) {
    return safeText(type).toLowerCase().includes('friend');
}

function getInitialMemberLimit() {
    const height = window.innerHeight || document.documentElement.clientHeight || 900;
    const estimated = Math.ceil((height - 180) / 74);
    return Math.max(memberPageSize, Math.min(memberInitialPageMax, estimated));
}


async function fetchAPI(endpoint, method = 'GET', body = null, { signal = null } = {}) {
    const headers = {
        'Content-Type': 'application/json'
    };
    const options = { method, headers };
    if (body) options.body = JSON.stringify(body);
    if (signal) options.signal = signal;

    try {
        const response = await fetch(endpoint, options);
        if (response.status === 401) {
            showAuth(true);
            throw new Error('Unauthorized');
        }
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return await response.json();
    } catch (err) {
        if (err.name === 'AbortError') throw err;
        if (err.message === 'Unauthorized') {
            console.error('API Key invalid or expired');
        } else {
            console.error('Fetch error:', err);
            showClipboardToast('网络请求失败，请检查连接或稍后重试', true);
        }
        throw err;
    }
}

function getFocusableElements(root) {
    if (!root) return [];
    return Array.from(root.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(element => (
        !element.hidden
        && !element.closest('[hidden], [inert], [aria-hidden="true"]')
    ));
}

function trapFocusWithin(event, root) {
    if (event.key !== 'Tab' || !root) return;
    const focusable = getFocusableElements(root);
    if (!focusable.length) {
        event.preventDefault();
        root.focus?.({ preventScroll: true });
        return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) {
        event.preventDefault();
        last.focus({ preventScroll: true });
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus({ preventScroll: true });
    }
}

function isValidFocusReturnTarget(element) {
    const style = element instanceof HTMLElement ? window.getComputedStyle(element) : null;
    return Boolean(
        element instanceof HTMLElement
        && element !== document.body
        && element.isConnected
        && !element.hidden
        && !element.matches(':disabled')
        && !element.closest('[hidden], [inert], [aria-hidden="true"]')
        && style?.display !== 'none'
        && style?.visibility !== 'hidden'
        && element.getClientRects().length > 0
    );
}

function focusFirstAvailable(targets) {
    for (const target of targets) {
        if (!isValidFocusReturnTarget(target)) continue;
        target.focus({ preventScroll: true });
        if (document.activeElement === target) return true;
    }
    return false;
}

function showAuth(show) {
    const overlay = document.getElementById('auth-overlay');
    if (!overlay) return;
    const appRoots = [
        document.getElementById('mobile-overlay'),
        document.getElementById('sessionSidebar'),
        document.querySelector('.main-container'),
        document.getElementById('analysisPanel'),
    ].filter(Boolean);

    if (show) {
        if (!overlay.contains(document.activeElement) && isValidFocusReturnTarget(document.activeElement)) {
            authReturnFocus = document.activeElement;
        }
        document.body.classList.add('auth-blocked');
        appRoots.forEach(root => {
            root.setAttribute('aria-hidden', 'true');
            root.setAttribute('inert', '');
        });
        overlay.classList.remove('hidden');
        overlay.removeAttribute('inert');
        overlay.setAttribute('aria-hidden', 'false');
        requestAnimationFrame(() => document.getElementById('api-key-input')?.focus({ preventScroll: true }));
    } else {
        document.body.classList.remove('auth-blocked');
        syncPanelAccessibility();
        focusFirstAvailable([
            authReturnFocus,
            document.getElementById('searchInput'),
            document.getElementById('btn-sidebar'),
            document.getElementById('btn-analysis'),
        ]);
        overlay.classList.add('hidden');
        overlay.setAttribute('aria-hidden', 'true');
        overlay.setAttribute('inert', '');
        authReturnFocus = null;
    }
}

async function hasAuthSession() {
    try {
        const res = await fetch('/api/auth/status', {
            method: 'GET',
            cache: 'no-store',
            credentials: 'same-origin',
        });
        if (!res.ok) return false;
        const data = await res.json();
        return Boolean(data.configured && data.authenticated);
    } catch (e) {
        console.warn('Auth status probe failed', e);
        return false;
    }
}

async function verifyLogin() {
    const input = document.getElementById('api-key-input');
    const key = input.value;
    const loginBtn = document.getElementById('login-btn');
    const buttonLabel = loginBtn?.querySelector('.button-label');
    const error = document.getElementById('auth-error');
    if (loginBtn?.disabled) return;
    if (!key.trim()) {
        error.textContent = '请输入 API 密钥';
        error.style.display = 'block';
        input.setAttribute('aria-invalid', 'true');
        input.focus({ preventScroll: true });
        return;
    }
    error.style.display = 'none';
    input.removeAttribute('aria-invalid');
    if (loginBtn) {
        loginBtn.disabled = true;
        loginBtn.setAttribute('aria-busy', 'true');
    }
    input.readOnly = true;
    if (buttonLabel) buttonLabel.textContent = '正在验证…';
    try {
        const res = await fetch('/api/auth/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: key })
        });
        const data = await res.json();
        if (data.success) {
            input.value = '';
            showAuth(false);
            initApp({ authenticated: true });
        } else {
            error.textContent = data.message || data.detail || '密钥校验失败';
            error.style.display = 'block';
            input.setAttribute('aria-invalid', 'true');
            input.focus({ preventScroll: true });
        }
    } catch (e) {
        console.error(e);
        error.textContent = '登录请求失败，请检查连接后重试';
        error.style.display = 'block';
        input.setAttribute('aria-invalid', 'true');
    } finally {
        if (loginBtn) {
            loginBtn.disabled = false;
            loginBtn.removeAttribute('aria-busy');
        }
        input.readOnly = false;
        if (buttonLabel) buttonLabel.textContent = '验证并进入';
    }
}

async function logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (e) {
        console.warn('Logout request failed', e);
    }
    location.reload();
}

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false
});

function formatTime(ts) {
    return dateFormatter.format(new Date(ts * 1000));
}

function getDateStr(ts) {
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

function escapeAttr(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function safeText(value, fallback = '') {
    if (value === null || value === undefined) return fallback;
    return String(value);
}

function safeCount(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
}

function hashText(value) {
    const text = safeText(value);
    let hash = 2166136261;
    for (let i = 0; i < text.length; i++) {
        hash ^= text.charCodeAt(i);
        hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(36);
}

function escapeCssValue(value) {
    const text = safeText(value);
    if (window.CSS && typeof window.CSS.escape === 'function') {
        return window.CSS.escape(text);
    }
    return text.replace(/[^a-zA-Z0-9_-]/g, ch => {
        const code = ch.codePointAt(0);
        return code === undefined ? '' : `\\${code.toString(16)} `;
    });
}

function makePrivateToken(prefix, index) {
    return `\uE000${prefix}_${index}\uE001`;
}

function escapeHtmlText(value) {
    const div = document.createElement('div');
    div.textContent = safeText(value);
    return div.innerHTML;
}

function decodeHtmlEntities(value) {
    const textarea = document.createElement('textarea');
    textarea.innerHTML = safeText(value);
    return textarea.value;
}

function isSafeNavigationUrl(url) {
    const normalized = safeText(url).trim();
    if (!normalized || /[\u0000-\u001F\u007F\s]/.test(normalized)) return false;
    if (/["'<>]/.test(normalized)) return false;
    return /^(https?:\/\/|\/static\/)/i.test(normalized);
}

function isSafeResourceUrl(url) {
    const normalized = safeText(url).trim();
    if (!normalized || /[\u0000-\u001F\u007F\s]/.test(normalized)) return false;
    if (/["'<>]/.test(normalized)) return false;
    return normalized.startsWith('/static/')
        || normalized.startsWith('/api/proxy/image?url=');
}

function getMediaResourceUrl(urlText) {
    const url = decodeHtmlEntities(urlText).trim();
    if (isSafeResourceUrl(url)) return url;
    if (isSafeNavigationUrl(url) && /^https?:\/\//i.test(url)) {
        return `/api/proxy/image?url=${encodeURIComponent(url)}`;
    }
    return '';
}

function isSafeMarkdownUrl(url) {
    return isSafeNavigationUrl(url);
}

function getMarkdownUrl(urlText) {
    const url = decodeHtmlEntities(urlText).trim();
    return isSafeMarkdownUrl(url) ? url : '';
}

function getMarkdownMediaUrl(urlText) {
    return getMediaResourceUrl(urlText);
}

function readQuotedStringAt(value, start) {
    const quote = value[start];
    if (quote !== '"' && quote !== "'") return null;

    let raw = '';
    for (let i = start + 1; i < value.length; i++) {
        const ch = value[i];
        if (ch === '\\' && i + 1 < value.length) {
            raw += ch + value[i + 1];
            i += 1;
            continue;
        }
        if (ch === quote) {
            return { raw, end: i + 1 };
        }
        raw += ch;
    }
    return null;
}

function decodeSerializedStringEscapes(value) {
    return safeText(value)
        .replace(/\\U([0-9a-fA-F]{8})/g, (match, hex) => {
            const codePoint = Number.parseInt(hex, 16);
            return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : match;
        })
        .replace(/\\u([0-9a-fA-F]{4})/g, (match, hex) => String.fromCharCode(Number.parseInt(hex, 16)))
        .replace(/\\x([0-9a-fA-F]{2})/g, (match, hex) => String.fromCharCode(Number.parseInt(hex, 16)))
        .replace(/\\r\\n/g, '\n')
        .replace(/\\n/g, '\n')
        .replace(/\\r/g, '\n')
        .replace(/\\t/g, '\t')
        .replace(/\\"/g, '"')
        .replace(/\\'/g, "'")
        .replace(/\\\\/g, '\\');
}

function getQuotedFieldValue(value, fieldName, startAt = 0) {
    const escapedField = fieldName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = new RegExp(`(['"])${escapedField}\\1\\s*:`, 'g');
    pattern.lastIndex = startAt;

    let match;
    while ((match = pattern.exec(value)) !== null) {
        let cursor = pattern.lastIndex;
        while (cursor < value.length && /\s/.test(value[cursor])) cursor += 1;
        const quoted = readQuotedStringAt(value, cursor);
        if (quoted) return quoted.raw;
    }
    return null;
}

function extractSerializedTextComponent(value) {
    const text = safeText(value).trim();
    if (!text || !text.startsWith('{') || !/(['"])type\1\s*:\s*(['"])text\2/.test(text)) return null;

    const rawText = getQuotedFieldValue(text, 'text');
    return rawText === null ? null : decodeSerializedStringEscapes(rawText);
}

function parseSerializedTextComponentAt(value, start) {
    if (value[start] !== '{') return null;

    const typeMatch = value.slice(start).match(/^\{\s*(['"])type\1\s*:\s*(['"])text\2\s*,/);
    if (!typeMatch) return null;

    let cursor = start + typeMatch[0].length;
    while (cursor < value.length && /\s/.test(value[cursor])) cursor += 1;

    const fieldMatch = value.slice(cursor).match(/^(['"])text\1\s*:/);
    if (!fieldMatch) return null;
    cursor += fieldMatch[0].length;
    while (cursor < value.length && /\s/.test(value[cursor])) cursor += 1;

    const quoted = readQuotedStringAt(value, cursor);
    if (!quoted) return null;
    cursor = quoted.end;
    while (cursor < value.length && /\s/.test(value[cursor])) cursor += 1;
    if (value[cursor] !== '}') return null;

    return {
        text: decodeSerializedStringEscapes(quoted.raw),
        end: cursor + 1
    };
}

function replaceEmbeddedSerializedTextComponents(value) {
    const text = safeText(value);
    let output = '';
    let cursor = 0;

    while (cursor < text.length) {
        const start = text.indexOf('{', cursor);
        if (start === -1) {
            output += text.slice(cursor);
            break;
        }

        const parsed = parseSerializedTextComponentAt(text, start);
        if (!parsed) {
            output += text.slice(cursor, start + 1);
            cursor = start + 1;
            continue;
        }

        let replacement = parsed.text;
        if (replacement.includes('\n') && parsed.end < text.length && text[parsed.end] !== '\n') {
            replacement += '\n';
        }
        output += text.slice(cursor, start) + replacement;
        cursor = parsed.end;
    }

    return output;
}

function normalizeArchiveMessageText(value) {
    if (value && typeof value === 'object') {
        if (String(value.type || '').toLowerCase() === 'text') {
            if (typeof value.text === 'string') return value.text;
            if (value.data && typeof value.data.text === 'string') return value.data.text;
        }
    }

    let text = safeText(value);
    const serializedText = extractSerializedTextComponent(text);
    if (serializedText !== null) return serializedText;

    text = replaceEmbeddedSerializedTextComponents(text);

    if (text.startsWith("<Event,") || text.includes("'raw_message':") || text.includes('"raw_message":')) {
        const rawMessage = getQuotedFieldValue(text, 'raw_message');
        if (rawMessage !== null) return decodeSerializedStringEscapes(rawMessage);
    }
    return text;
}

function renderMessageMarkdown(escapedText) {
    const codeTokens = [];
    const stashCode = html => {
        const token = makePrivateToken('MD_CODE', codeTokens.length);
        codeTokens.push(html);
        return token;
    };
    const restoreCode = html => html.replace(/\uE000MD_CODE_(\d+)\uE001/g, (match, index) => codeTokens[Number(index)] ?? match);
    const renderInline = value => {
        let html = safeText(value);
        html = html.replace(/`([^`\n]+)`/g, (match, code) => stashCode(`<code class="msg-md-code">${code}</code>`));
        html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
        html = html.replace(/(^|[^\*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
        html = html.replace(/!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (match, alt, urlText) => {
            if (!showMessageMedia) return '<span class="msg-tag msg-tag-muted" data-media-hidden="image">[图片已隐藏]</span>';
            const url = getMarkdownMediaUrl(urlText);
            if (!url) return match;
            const safeUrl = escapeAttr(url);
            const safeAlt = escapeAttr(decodeHtmlEntities(alt));
            return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer"><img src="${safeUrl}" class="msg-image msg-md-image" alt="${safeAlt || '图片'}" loading="lazy" onload="this.classList.add('loaded')" onerror="this.parentElement.outerHTML='<span class=\\'msg-tag msg-tag-muted\\'>[图片无法加载]</span>'" /></a>`;
        });
        html = html.replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (match, label, urlText) => {
            const url = getMarkdownUrl(urlText);
            if (!url) return label;
            return `<a class="msg-md-link" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
        });
        return html;
    };
    const renderInlineLines = value => renderInline(value).replace(/\n/g, '<br>');
    const lines = safeText(escapedText).replace(/\r\n?/g, '\n').split('\n');
    const output = [];

    const isBlank = line => line.trim() === '';
    const isFence = line => /^```[A-Za-z0-9_-]*\s*$/.test(line.trim());
    const isHr = line => /^(\s*)(-{3,}|\*{3,}|_{3,})\s*$/.test(line);
    const isHeading = line => /^(#{1,6})\s+(.+)$/.test(line);
    const isQuote = line => /^\s*&gt;\s?/.test(line);
    const isUnordered = line => /^\s*[-+*]\s+(.+)$/.test(line);
    const isOrdered = line => /^\s*\d+\.\s+(.+)$/.test(line);
    const isBlockStart = line => isFence(line) || isHr(line) || isHeading(line) || isQuote(line) || isUnordered(line) || isOrdered(line);

    for (let i = 0; i < lines.length;) {
        const line = lines[i];
        if (isBlank(line)) {
            i += 1;
            continue;
        }

        const fenceMatch = line.trim().match(/^```([A-Za-z0-9_-]*)\s*$/);
        if (fenceMatch) {
            const codeLines = [];
            i += 1;
            while (i < lines.length && !/^```\s*$/.test(lines[i].trim())) {
                codeLines.push(lines[i]);
                i += 1;
            }
            if (i < lines.length) i += 1;
            const lang = fenceMatch[1] ? ` data-lang="${escapeAttr(fenceMatch[1])}"` : '';
            output.push(stashCode(`<pre class="msg-md-codeblock"${lang}><code>${codeLines.join('\n')}</code></pre>`));
            continue;
        }

        const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
        if (headingMatch) {
            const level = headingMatch[1].length;
            output.push(`<div class="msg-md-heading msg-md-heading-${level}">${renderInline(headingMatch[2].trim())}</div>`);
            i += 1;
            continue;
        }

        if (isHr(line)) {
            output.push('<hr class="msg-md-hr">');
            i += 1;
            continue;
        }

        if (isQuote(line)) {
            const quoteLines = [];
            while (i < lines.length && (isQuote(lines[i]) || isBlank(lines[i]))) {
                quoteLines.push(isBlank(lines[i]) ? '' : lines[i].replace(/^\s*&gt;\s?/, ''));
                i += 1;
            }
            output.push(`<blockquote class="msg-md-quote">${renderInlineLines(quoteLines.join('\n'))}</blockquote>`);
            continue;
        }

        const unorderedMatch = line.match(/^\s*[-+*]\s+(.+)$/);
        const orderedMatch = line.match(/^\s*\d+\.\s+(.+)$/);
        if (unorderedMatch || orderedMatch) {
            const ordered = Boolean(orderedMatch);
            const tag = ordered ? 'ol' : 'ul';
            const items = [];
            while (i < lines.length) {
                const itemMatch = ordered
                    ? lines[i].match(/^\s*\d+\.\s+(.+)$/)
                    : lines[i].match(/^\s*[-+*]\s+(.+)$/);
                if (!itemMatch) break;
                items.push(`<li>${renderInlineLines(itemMatch[1].trim())}</li>`);
                i += 1;
            }
            output.push(`<${tag} class="msg-md-list">${items.join('')}</${tag}>`);
            continue;
        }

        const paragraph = [];
        while (i < lines.length && !isBlank(lines[i]) && !isBlockStart(lines[i])) {
            paragraph.push(lines[i]);
            i += 1;
        }
        output.push(`<p class="msg-md-p">${renderInlineLines(paragraph.join('\n').trim())}</p>`);
    }

    return {
        html: output.join(''),
        restore: restoreCode
    };
}

function getImageDisplayStyle(width, height) {
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0 || width > 100000 || height > 100000) {
        return '';
    }

    const ratio = width / height;
    let maxWidth = 520;
    let maxHeight = 420;

    if (ratio >= 2.4) {
        maxWidth = 560;
        maxHeight = 240;
    } else if (ratio >= 1.25) {
        maxWidth = 540;
        maxHeight = 380;
    } else if (ratio <= 0.45) {
        maxWidth = 300;
        maxHeight = 520;
    } else if (ratio <= 0.8) {
        maxWidth = 360;
        maxHeight = 500;
    } else {
        maxWidth = 440;
        maxHeight = 440;
    }

    let scale = Math.min(maxWidth / width, maxHeight / height, 1);
    const longEdge = Math.max(width, height);
    if (longEdge < 180) {
        scale = Math.min(maxWidth / width, maxHeight / height, 180 / longEdge, 2.5);
    }

    const displayWidth = Math.max(48, Math.round(width * scale));
    return ` style="width:${displayWidth}px;max-width:100%;aspect-ratio:${width}/${height};"`;
}

function formatSessionPreview(text) {
    if (!text) return '';
    const rawText = cleanInlineCqMediaCodes(normalizeArchiveMessageText(text));
    const previewText = isShareJsonPayload(rawText.trim())
        ? formatSharePreview(rawText.trim())
        : replaceCqJsonCodes(rawText, data => formatSharePreview(data));
    return previewText
        .replace(/\[CQ:image(?:,[^\]]*)?\]/g, '[图片]')
        .replace(/\[CQ:video(?:,[^\]]*)?\]/g, '[视频]')
        .replace(/\[CQ:record(?:,[^\]]*)?\]/g, '[语音]')
        .replace(/\[CQ:file(?:,[^\]]*)?\]/g, '[文件]')
        .replace(/\[CQ:face,[^\]]*\]/g, '[表情]')
        .replace(/\[CQ:at,qq=all[^\]]*\]/g, '@全体成员')
        .replace(/\[CQ:at,qq=([^\],]+)[^\]]*\]/g, (match, qq) => {
            const name = window.userMap && window.userMap[qq] ? window.userMap[qq] : qq;
            return `@${name}`;
        })
        .replace(/\[CQ:reply,[^\]]*\]/g, '[回复]')
        .replace(/\s+/g, ' ')
        .trim();
}

function getDesiredSessionId() {
    return new URLSearchParams(window.location.search).get('session_id') || '';
}

function updateSessionUrl(sessionId, replace = false) {
    if (!sessionId) return;
    const url = new URL(window.location.href);
    url.searchParams.delete('view');
    url.searchParams.delete('section');
    url.searchParams.set('session_id', sessionId);
    const state = { session_id: sessionId };
    if (replace) window.history.replaceState(state, '', url);
    else window.history.pushState(state, '', url);
}

function updateDashboardUrl(replace = false) {
    const url = new URL(window.location.href);
    url.searchParams.delete('view');
    url.searchParams.delete('section');
    url.searchParams.delete('session_id');
    const state = { view: 'dashboard' };
    if (replace) window.history.replaceState(state, '', url);
    else window.history.pushState(state, '', url);
}

function formatCompactNumber(value) {
    const n = safeCount(value);
    if (n >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
    if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
    return n.toLocaleString();
}

function renderDashboardTrendSvg(points = []) {
    const values = points.map(p => safeCount(p.count));
    const maxValue = Math.max(...values, 1);
    const width = 640;
    const height = 180;
    const padX = 28;
    const padY = 24;
    const usableW = width - padX * 2;
    const usableH = height - padY * 2;
    const coords = values.map((value, idx) => {
        const x = padX + (points.length <= 1 ? 0 : (idx / (points.length - 1)) * usableW);
        const y = padY + usableH - (value / maxValue) * usableH;
        return { x, y, value, date: points[idx]?.date || '' };
    });
    const polyline = coords.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
    const areaPoints = coords.length
        ? `${coords[0].x.toFixed(1)},${(height - padY).toFixed(1)} ${polyline} ${coords[coords.length - 1].x.toFixed(1)},${(height - padY).toFixed(1)}`
        : '';
    const ticks = [0, 0.5, 1].map(t => {
        const y = padY + usableH - usableH * t;
        return `<g><line x1="${padX}" y1="${y}" x2="${width - padX}" y2="${y}" class="dashboard-grid"/><text x="${padX}" y="${y - 4}" class="dashboard-axis">${Math.round(maxValue * t).toLocaleString()}</text></g>`;
    }).join('');
    const circles = coords.map(p => `<circle class="dashboard-dot" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="4.5" data-date="${escapeAttr(p.date)}" data-value="${p.value}" tabindex="0" role="img" aria-label="${escapeAttr(p.date)}，${p.value.toLocaleString()} 条消息"><title>${escapeAttr(p.date)}：${p.value.toLocaleString()} 条消息</title></circle>`).join('');
    const labels = coords.filter((_p, idx) => idx === 0 || idx === coords.length - 1 || idx % Math.ceil(Math.max(coords.length, 1) / 4) === 0)
        .map(p => `<text x="${p.x.toFixed(1)}" y="${height - 4}" text-anchor="middle" class="dashboard-axis">${escapeAttr(p.date.slice(5))}</text>`).join('');
    return `<svg class="dashboard-trend-svg" viewBox="0 0 ${width} ${height}" role="group" aria-label="消息活跃趋势">
        ${ticks}
        ${areaPoints ? `<polygon points="${areaPoints}" class="dashboard-area"></polygon>` : ''}
        ${polyline ? `<polyline points="${polyline}" class="dashboard-line"></polyline>` : ''}
        ${circles}
        ${labels}
    </svg>`;
}

function renderDashboardTrendContent(points = []) {
    if (!Array.isArray(points) || points.length === 0) {
        return '<div class="dashboard-muted dashboard-trend-empty">所选时段暂无趋势数据。</div>';
    }
    return renderDashboardTrendSvg(points);
}

function renderTypeDistribution(items = []) {
    const total = items.reduce((sum, item) => sum + safeCount(item.count), 0) || 1;
    if (!items.length) return '<div class="dashboard-muted">暂无类型统计</div>';

    const typeMapping = {
        'text': 0,
        'image': 1,
        'other': 2
    };

    return items.map((item, idx) => {
        const count = safeCount(item.count);
        const pct = Math.round((count / total) * 1000) / 10;
        const colorIdx = typeMapping[item.type] !== undefined ? typeMapping[item.type] : (idx % 5);
        return `<div class="dashboard-type-row">
            <div class="dashboard-type-meta"><span>${escapeAttr(item.name || item.type)}</span><span>${count.toLocaleString()} · ${pct}%</span></div>
            <div class="dashboard-type-bar"><span class="dashboard-type-fill dashboard-type-${colorIdx}" style="width:${Math.max(pct, 2)}%"></span></div>
        </div>`;
    }).join('');
}

function dashboardOpenSession(sessionId, name = '', messageType = '') {
    const sid = safeText(sessionId);
    if (!sid) return;
    const meta = sessionsById.get(sid) || {};
    selectSession(sid, name || meta.name || meta.session_name || sid, messageType || meta.message_type || '');
}

function attachTrendTooltipHandlers(panel) {
    const dots = panel.querySelectorAll('.dashboard-dot');
    let tooltip = document.getElementById('dashboardTrendTooltip');
    if (!tooltip) {
        tooltip = document.createElement('div');
        tooltip.id = 'dashboardTrendTooltip';
        tooltip.className = 'dashboard-trend-tooltip';
        tooltip.setAttribute('role', 'status');
        panel.appendChild(tooltip);
    }

    dots.forEach(dot => {
        const show = () => {
            const date = dot.getAttribute('data-date');
            const val = parseInt(dot.getAttribute('data-value'), 10) || 0;
            const dateLabel = document.createElement('div');
            dateLabel.className = 'dashboard-tooltip-date';
            dateLabel.textContent = date;
            const valueLabel = document.createElement('div');
            valueLabel.className = 'dashboard-tooltip-value';
            valueLabel.textContent = `${val.toLocaleString()} 条消息`;
            tooltip.replaceChildren(dateLabel, valueLabel);

            tooltip.style.display = 'block';
            tooltip.style.opacity = '1';

            const panelRect = panel.getBoundingClientRect();
            const dotRect = dot.getBoundingClientRect();

            const desiredLeft = dotRect.left - panelRect.left + dotRect.width / 2;
            const tooltipHalf = Math.max(58, tooltip.offsetWidth / 2);
            const left = Math.min(
                panelRect.width - tooltipHalf - 8,
                Math.max(tooltipHalf + 8, desiredLeft)
            );
            const top = dotRect.top - panelRect.top - 6;

            tooltip.style.left = `${left}px`;
            tooltip.style.top = `${top}px`;
        };

        const hide = () => {
            tooltip.style.opacity = '0';
            tooltip.style.display = 'none';
        };

        dot.addEventListener('mouseenter', show);
        dot.addEventListener('click', show);
        dot.addEventListener('mouseleave', hide);
        dot.addEventListener('focus', show);
        dot.addEventListener('blur', hide);
    });
}

function attachDashboardHandlers(root) {
    root.querySelectorAll('[data-dashboard-range]').forEach(btn => {
        btn.addEventListener('click', () => fetchDashboard(btn.dataset.dashboardRange || '30d'));
    });
    root.querySelectorAll('[data-dashboard-session]').forEach(el => {
        el.addEventListener('click', () => dashboardOpenSession(el.dataset.dashboardSession, el.dataset.dashboardName, el.dataset.dashboardType));
    });

    const trendPanel = root.querySelector('.dashboard-panel-wide');
    if (trendPanel) {
        attachTrendTooltipHandlers(trendPanel);
    }
}

function getPerfBarPercent(ms, total) {
    if (!total || total <= 0) return 0;
    return Math.min(100, Math.max(0, (ms / total) * 100));
}

function renderPerformanceChart(perf = {}, summary = {}) {
    const isHit = !!perf.cache_hit;
    const dbSize = typeof perf.db_size_mb === 'number' ? perf.db_size_mb : 0;
    const totalTime = typeof perf.total_db_time_ms === 'number' ? perf.total_db_time_ms : 0;

    const timeSummary = typeof perf.time_summary_ms === 'number' ? perf.time_summary_ms : 0;
    const timeType = typeof perf.time_type_ms === 'number' ? perf.time_type_ms : 0;
    const timeTrend = typeof perf.time_trend_ms === 'number' ? perf.time_trend_ms : 0;
    const timeGroups = typeof perf.time_groups_ms === 'number' ? perf.time_groups_ms : 0;

    const pctSummary = getPerfBarPercent(timeSummary, totalTime);
    const pctType = getPerfBarPercent(timeType, totalTime);
    const pctTrend = getPerfBarPercent(timeTrend, totalTime);
    const pctGroups = getPerfBarPercent(timeGroups, totalTime);

    // Calculate throughput: messages per millisecond (total_messages / total_time)
    const totalMsgs = typeof summary.total_messages === 'number' ? summary.total_messages : 0;
    const speed = totalTime > 0 ? Math.round(totalMsgs / totalTime) : 0;
    const throughputHtml = `<span class="perf-kpi-value">${speed.toLocaleString()} <span class="perf-kpi-unit">条/ms</span></span>`;

    return `
        <div class="perf-container animate-fade">
            <!-- KPI Cards Row -->
            <div class="perf-kpi-row">
                <div class="perf-kpi-card">
                    <span class="perf-kpi-label">数据库大小</span>
                    <span class="perf-kpi-value">${dbSize.toFixed(2)} <span class="perf-kpi-unit">MB</span></span>
                </div>
                <div class="perf-kpi-card">
                    <span class="perf-kpi-label">SQL 查询总耗时</span>
                    <span class="perf-kpi-value ${isHit ? 'cache-hit-text' : ''}">${totalTime.toFixed(2)} <span class="perf-kpi-unit">ms</span></span>
                </div>
                <div class="perf-kpi-card">
                    <span class="perf-kpi-label">单次数据检索吞吐率</span>
                    ${throughputHtml}
                </div>
            </div>

            <!-- SQL Subquery Performance Bar Grid -->
            <div class="perf-bars-grid">
                <div class="perf-bar-row">
                    <div class="perf-bar-meta">
                        <span class="perf-bar-name">${uiIcon('dashboard', 'perf-bar-icon')} 全局概览统计</span>
                        <span class="perf-bar-time">${timeSummary.toFixed(2)} ms</span>
                    </div>
                    <div class="perf-bar-track">
                        <div class="perf-bar-fill fill-summary" style="width: ${pctSummary}%"></div>
                    </div>
                </div>

                <div class="perf-bar-row">
                    <div class="perf-bar-meta">
                        <span class="perf-bar-name">${uiIcon('distribution', 'perf-bar-icon')} 消息类型分布</span>
                        <span class="perf-bar-time">${timeType.toFixed(2)} ms</span>
                    </div>
                    <div class="perf-bar-track">
                        <div class="perf-bar-fill fill-type" style="width: ${pctType}%"></div>
                    </div>
                </div>

                <div class="perf-bar-row">
                    <div class="perf-bar-meta">
                        <span class="perf-bar-name">${uiIcon('trend', 'perf-bar-icon')} 活跃度趋势分析</span>
                        <span class="perf-bar-time">${timeTrend.toFixed(2)} ms</span>
                    </div>
                    <div class="perf-bar-track">
                        <div class="perf-bar-fill fill-trend" style="width: ${pctTrend}%"></div>
                    </div>
                </div>

                <div class="perf-bar-row">
                    <div class="perf-bar-meta">
                        <span class="perf-bar-name">${uiIcon('users', 'perf-bar-icon')} 活跃群聊排行</span>
                        <span class="perf-bar-time">${timeGroups.toFixed(2)} ms</span>
                    </div>
                    <div class="perf-bar-track">
                        <div class="perf-bar-fill fill-groups" style="width: ${pctGroups}%"></div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function renderDashboard(data) {
    const list = document.getElementById('messageList');
    if (!list) return;
    list.setAttribute('aria-busy', 'false');
    deactivateVirtualHistoryView(list);
    const loadMore = document.getElementById('loadMoreWrap');
    if (loadMore) loadMore.style.display = 'none';

    const summary = data?.summary || {};
    const trend = data?.activity_trend || [];
    const topGroups = data?.top_groups || [];
    const dist = data?.message_type_distribution || [];
    const perf = data?.performance || {};
    const currentRange = data?.range || '30d';

    const rangeButtons = [['1d', '24小时'], ['7d', '7天'], ['30d', '30天']]
        .map(([key, label]) => `<button type="button" class="dashboard-range-btn ${currentRange === key ? 'active' : ''}" data-dashboard-range="${key}" data-od-id="dashboard-range-${key}" aria-pressed="${currentRange === key}">${label}</button>`).join('');

    const topGroupHtml = topGroups.length ? topGroups.map((g, idx) => `
        <button class="dashboard-list-item dashboard-clickable" type="button" data-dashboard-session="${escapeAttr(g.session_id)}" data-dashboard-name="${escapeAttr(g.name)}" data-dashboard-type="${escapeAttr(g.message_type)}" data-od-id="dashboard-group-${idx + 1}">
            <span class="dashboard-rank">${String(idx + 1).padStart(2, '0')}</span>
            <span class="dashboard-item-main"><strong>${escapeAttr(g.name)}</strong><small>${escapeAttr(g.last_msg || '暂无消息预览')}</small></span>
            <span class="dashboard-item-count">${formatCompactNumber(g.message_count)}</span>
        </button>
    `).join('') : '<div class="dashboard-muted">暂无群聊排行</div>';

    list.innerHTML = `
        <section class="dashboard-view animate-fade" data-od-id="dashboard-overview" aria-labelledby="dashboard-heading">
            <div class="dashboard-hero" data-od-id="dashboard-summary">
                <div>
                    <p class="dashboard-kicker">归档工作台</p>
                    <h2 id="dashboard-heading">归档总览</h2>
                    <p>查看消息规模、活跃趋势、群聊排行与查询性能。</p>
                </div>
                <div class="dashboard-cache-note">${data?.cached ? '已使用缓存' : '实时生成'} · 有效 ${data?.cache_ttl || 30} 秒</div>
            </div>
            <div class="dashboard-summary-grid">
                <div class="dashboard-card" data-od-id="dashboard-card-total-messages"><span>总消息数</span><strong>${formatCompactNumber(summary.total_messages)}</strong></div>
                <div class="dashboard-card" data-od-id="dashboard-card-today"><span>今日消息</span><strong>${formatCompactNumber(summary.today_messages)}</strong></div>
                <div class="dashboard-card" data-od-id="dashboard-card-sessions"><span>总会话数</span><strong>${formatCompactNumber(summary.total_sessions)}</strong></div>
                <div class="dashboard-card" data-od-id="dashboard-card-images"><span>总图片数</span><strong>${formatCompactNumber(summary.total_images)}</strong></div>
                <div class="dashboard-card" data-od-id="dashboard-card-videos"><span>总视频数</span><strong>${formatCompactNumber(summary.total_videos)}</strong></div>
            </div>
            <div class="dashboard-grid-layout">
                <div class="dashboard-panel dashboard-panel-wide" data-od-id="dashboard-trend">
                    <div class="dashboard-panel-header"><h3>活跃度趋势</h3><div class="dashboard-range-group" id="dashboardRangeGroup">${rangeButtons}</div></div>
                    <div id="dashboardTrendWrapper" class="dashboard-trend-wrapper">${renderDashboardTrendContent(trend)}</div>
                </div>
                <div class="dashboard-column">
                    <div class="dashboard-panel" data-od-id="dashboard-message-types">
                        <div class="dashboard-panel-header"><h3>消息类型分布</h3></div>
                        ${renderTypeDistribution(dist)}
                    </div>
                    <div class="dashboard-panel" data-od-id="dashboard-performance">
                        <div class="dashboard-panel-header">
                            <h3>${uiIcon('database', 'dashboard-heading-icon')} 缓存与 SQL 查询性能</h3>
                            ${perf.cache_hit ? '<span class="perf-cache-badge cache-hit">缓存命中</span>' : '<span class="perf-cache-badge cache-miss">数据库查询</span>'}
                        </div>
                        ${renderPerformanceChart(perf, summary)}
                    </div>
                </div>
                <div class="dashboard-panel" data-od-id="dashboard-top-groups">
                    <div class="dashboard-panel-header"><h3>群活跃排行</h3></div>
                    <div class="dashboard-list">${topGroupHtml}</div>
                </div>
            </div>
        </section>`;
    attachDashboardHandlers(list);
}

function showDashboardSkeleton() {
    const list = document.getElementById('messageList');
    if (!list) return;
    list.setAttribute('aria-busy', 'true');
    list.innerHTML = `
        <section class="dashboard-view dashboard-loading" aria-hidden="true">
            <div class="dashboard-hero dashboard-loading-hero skeleton"></div>
            <div class="dashboard-summary-grid">
                ${Array.from({ length: 5 }, () => '<div class="dashboard-card dashboard-loading-card"><span class="skeleton"></span><strong class="skeleton"></strong></div>').join('')}
            </div>
            <div class="dashboard-panel dashboard-loading-panel">
                <div class="skeleton"></div>
                <div class="skeleton"></div>
                <div class="skeleton"></div>
            </div>
        </section>`;
}

function isDashboardViewActive() {
    return !activeSessionId && !getActiveSearchKeyword() && !document.body.classList.contains('settings-view');
}

async function fetchDashboard(range = '30d') {
    const requestSeq = ++dashboardRequestSeq;
    const trendWrapper = document.getElementById('dashboardTrendWrapper');
    const rangeGroup = document.getElementById('dashboardRangeGroup');
    const isAlreadyVisible = Boolean(trendWrapper && rangeGroup);

    if (isAlreadyVisible) {
        trendWrapper.style.opacity = '0.5';
    } else {
        showDashboardSkeleton();
    }

    try {
        const data = await fetchAPI(`/api/dashboard?range=${encodeURIComponent(range)}`);
        if (requestSeq !== dashboardRequestSeq || !isDashboardViewActive()) return;
        if (!data.success) throw new Error('Dashboard request failed');

        if (isAlreadyVisible) {
            const trend = data.data?.activity_trend || [];
            const currentRange = data.data?.range || range;

            // 1. Update range buttons
            const rangeButtons = [['1d', '24小时'], ['7d', '7天'], ['30d', '30天']]
                .map(([key, label]) => `<button type="button" class="dashboard-range-btn ${currentRange === key ? 'active' : ''}" data-dashboard-range="${key}" data-od-id="dashboard-range-${key}" aria-pressed="${currentRange === key}">${label}</button>`).join('');
            rangeGroup.innerHTML = rangeButtons;

            // 2. Update trend Svg
            trendWrapper.innerHTML = renderDashboardTrendContent(trend);
            trendWrapper.style.opacity = '1';

            // 3. Re-attach click events to the new range buttons
            rangeGroup.querySelectorAll('[data-dashboard-range]').forEach(btn => {
                btn.addEventListener('click', () => fetchDashboard(btn.dataset.dashboardRange || '30d'));
            });

            // 4. Re-attach trend tooltip handlers
            const trendPanel = document.querySelector('.dashboard-panel-wide');
            if (trendPanel) {
                attachTrendTooltipHandlers(trendPanel);
            }
        } else {
            renderDashboard(data.data);
        }
    } catch (e) {
        if (requestSeq !== dashboardRequestSeq || !isDashboardViewActive()) return;
        console.error(e);
        const list = document.getElementById('messageList');
        if (list && !document.getElementById('dashboardTrendWrapper')) {
            list.setAttribute('aria-busy', 'false');
            const error = document.createElement('div');
            error.className = 'empty-state';
            error.setAttribute('role', 'alert');
            error.innerHTML = '<h2>归档总览加载失败</h2><p>当前无法获取统计数据。请稍后重试；如果问题持续，再查看服务日志。</p>';
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'primary-btn';
            retry.textContent = '重新加载';
            retry.addEventListener('click', () => fetchDashboard(range));
            error.appendChild(retry);
            list.replaceChildren(error);
        }
    } finally {
        if (requestSeq === dashboardRequestSeq && trendWrapper?.isConnected) {
            trendWrapper.style.opacity = '1';
        }
    }
}


function updateActiveSessionHeader() {
    const header = document.getElementById('activeSessionId');
    const searchInput = document.getElementById('searchInput');
    if (!header) return;
    if (document.body.classList.contains('settings-view')) {
        header.textContent = document.getElementById('settings-title')?.textContent || '设置';
        return;
    }
    if (!activeSessionId) {
        const keyword = getActiveSearchKeyword();
        if (keyword) {
            header.innerHTML = `<div class="active-session-title">${uiIcon('search', 'active-session-icon')}<span>全局搜索：“${escapeAttr(keyword)}”</span></div><div class="active-session-details"><span class="active-session-chip active-session-back">返回归档总览</span></div>`;
            const back = header.querySelector('.active-session-chip');
            makeKeyboardActivatable(back);
            back.addEventListener('click', () => showDashboard());
        } else {
            header.innerHTML = `<div class="active-session-title">${uiIcon('dashboard', 'active-session-icon')}<span>归档总览</span></div>`;
        }
        if (searchInput) searchInput.placeholder = '全局搜索消息…';
    } else {
        const meta = sessionsById.get(activeSessionId) || {};
        let title = meta.name || meta.session_name || activeSessionId;

        // Clean title if it contains any legacy prefix first
        title = title.replace(/^👤\s*私聊:\s*/, '').replace(/^私聊:\s*/, '');
        title = title.replace(/^💬\s*群聊:\s*/, '').replace(/^群聊:\s*/, '');
        title = title.replace(/^📢\s*频道:\s*/, '').replace(/^频道:\s*/, '');

        // Add the correct prefix for the header at the top
        const mt = (meta.message_type || '').toLowerCase();
        const sPlat = getSessionPlatform(meta);
        const isServerPlatform = ['discord', 'kook', 'teamspeak'].includes(sPlat);
        const isGroupLike = mt.includes('channel') || mt.includes('group');

        if (isServerPlatform && isGroupLike) {
            if (title.includes(' / #')) {
                title = title.replace(' / #', ' > #');
            } else if (title.includes(' / ')) {
                title = title.replace(' / ', ' > ');
            }
            title = '服务器：' + title;
        } else if (mt.includes('friend')) {
            title = '私聊：' + title;
        } else if (mt.includes('channel')) {
            if (title.includes(' / #')) {
                title = title.replace(' / #', ' > #');
            } else if (title.includes(' / ')) {
                title = title.replace(' / ', ' > ');
            }
            title = '频道：' + title;
        } else if (mt.includes('group')) {
            title = '群聊：' + title;
        }

        header.innerText = title;
        if (searchInput) searchInput.placeholder = '搜索当前会话…';
    }
}

function showSettings({ skipUrl = false, section = '', manageSessionId = '', messageId = 0 } = {}) {
    const url = new URL(window.location.href);
    if (skipUrl) section = url.searchParams.get('section') || '';
    const managementPage = section === 'messages' && document.getElementById('settings-management-template');
    abortHistoryRequest();
    cancelMemberSearch();
    dashboardRequestSeq += 1;
    statsRequestSeq += 1;
    memberRequestSeq += 1;
    rankRequestSeq += 1;
    document.body.classList.add('settings-view', 'global-view');
    closeAllPanels();
    activeSessionId = getDesiredSessionId();
    const list = document.getElementById('messageList');
    deactivateVirtualHistoryView(list, { resetScroll: true });
    list.replaceChildren(document.getElementById(managementPage ? 'settings-management-template' : 'settings-template').content.cloneNode(true));
    document.querySelectorAll('.session-item').forEach(item => {
        item.classList.remove('active');
        item.removeAttribute('aria-current');
    });
    document.querySelectorAll('.sidebar-sub-menu').forEach(item => item.remove());
    document.getElementById('settings-btn').setAttribute('aria-current', 'page');
    document.getElementById('scrollToBottomBtn').style.display = 'none';
    updateActiveSessionHeader();
    if (!skipUrl && (url.searchParams.get('view') !== 'settings' || (url.searchParams.get('section') || '') !== (managementPage ? 'messages' : ''))) {
        url.searchParams.set('view', 'settings');
        if (managementPage) url.searchParams.set('section', 'messages');
        else url.searchParams.delete('section');
        window.history.pushState({ view: 'settings' }, '', url);
    }
    if (managementPage) {
        setupMessageManagement({
            sessionId: manageSessionId || (skipUrl ? window.history.state?.managementSessionId ?? activeSessionId : activeSessionId),
            messageId: skipUrl ? window.history.state?.managementMessageId || 0 : messageId,
        });
        const back = document.getElementById('settings-back');
        back.textContent = '返回设置';
        back.onclick = () => showSettings();
    } else {
        const toggle = document.getElementById('show-message-media');
        toggle.checked = showMessageMedia;
        toggle.addEventListener('change', () => {
            showMessageMedia = toggle.checked;
            formattedMsgCache.clear();
            fullMessageFormattedCache.clear();
            const status = document.getElementById('settings-save-status');
            try {
                localStorage.setItem('astr_chat_show_media', String(showMessageMedia));
                status.textContent = '已保存';
            } catch (_) {
                status.textContent = '已应用，但当前浏览器无法保存此设置。';
            }
        });
        const back = document.getElementById('settings-back');
        back.textContent = activeSessionId ? '返回会话' : '返回总览';
        back.onclick = () => {
            if (activeSessionId) {
                const meta = sessionsById.get(activeSessionId);
                selectSession(activeSessionId, meta?.name || activeSessionId, meta?.message_type || '');
            } else {
                showDashboard();
            }
        };
        document.getElementById('settings-management')?.addEventListener('click', () => showSettings({ section: 'messages' }));
    }
    document.getElementById('settings-title').focus({ preventScroll: true });
}

function showDashboard(options = {}) {
    const wasDashboardActive = isDashboardViewActive();
    document.body.classList.remove('settings-view');
    document.getElementById('settings-btn')?.removeAttribute('aria-current');
    closeAllPanels();
    abortHistoryRequest();
    cancelMemberSearch();
    statsRequestSeq += 1;
    const searchInput = document.getElementById('searchInput');
    if (searchInput) searchInput.value = '';
    activeSearchKeyword = '';
    syncSearchCancelControl();
    activeSessionId = '';
    activeUserId = '';
    activeMsgType = '';
    document.body.classList.add('global-view');
    syncPanelAccessibility();
    currentPage = 1;
    nextCursor = 0;
    activeHistoryViewKey = '';
    memberRequestSeq += 1;
    rankRequestSeq += 1;
    memberTotalExact = false;
    rankTotalExact = false;
    window.userMap = {};
    window.globalTopUsers = [];
    updateAnalysisPanel(null);

    const list = document.getElementById('messageList');
    if (list) {
        deactivateVirtualHistoryView(list, { resetScroll: true });
    }
    document.querySelectorAll('.session-item').forEach(el => {
        const isDashboard = el.classList.contains('dashboard-nav');
        el.classList.toggle('active', isDashboard);
        if (isDashboard) el.setAttribute('aria-current', 'page');
        else el.removeAttribute('aria-current');
    });
    document.querySelectorAll('.sidebar-sub-menu').forEach(el => el.remove());
    const loadMore = document.getElementById('loadMoreWrap');
    if (loadMore) loadMore.style.display = 'none';
    if (scrollBtn) scrollBtn.style.display = 'none';
    updateActiveSessionHeader();
    if (!options.skipUrl && (!wasDashboardActive || options.replaceUrl === true)) {
        updateDashboardUrl(options.replaceUrl === true);
    }
    fetchDashboard(options.range || '30d');
}

function findCqJsonEnd(text, dataStart) {
    let braceDepth = 0;
    let inString = false;
    let escaped = false;
    let started = false;

    for (let i = dataStart; i < text.length; i++) {
        const ch = text[i];

        if (!started) {
            if (/\s/.test(ch)) continue;
            if (ch !== '{') return -1;
            started = true;
        }

        if (inString) {
            if (escaped) {
                escaped = false;
            } else if (ch === '\\') {
                escaped = true;
            } else if (ch === '"') {
                inString = false;
            }
            continue;
        }

        if (ch === '"') {
            inString = true;
        } else if (ch === '{') {
            braceDepth++;
        } else if (ch === '}') {
            braceDepth--;
            if (braceDepth === 0) {
                let closeIndex = i + 1;
                while (closeIndex < text.length && /\s/.test(text[closeIndex])) closeIndex++;
                return text[closeIndex] === ']' ? closeIndex : -1;
            }
        }
    }

    return -1;
}

function replaceCqJsonCodes(text, replacer) {
    const prefix = '[CQ:json,data=';
    let output = '';
    let cursor = 0;

    while (cursor < text.length) {
        const start = text.indexOf(prefix, cursor);
        if (start === -1) {
            output += text.slice(cursor);
            break;
        }

        const dataStart = start + prefix.length;
        let end = findCqJsonEnd(text, dataStart);
        let data = end === -1 ? '' : text.slice(dataStart, end);

        if (end === -1 || !parseCqJsonData(data)) {
            for (let probe = text.indexOf(']', dataStart); probe !== -1; probe = text.indexOf(']', probe + 1)) {
                const candidate = text.slice(dataStart, probe);
                if (parseCqJsonData(candidate)) {
                    end = probe;
                    data = candidate;
                    break;
                }
            }
        }

        if (end === -1) {
            output += text.slice(cursor);
            break;
        }

        output += text.slice(cursor, start);
        output += replacer(data);
        cursor = end + 1;
    }

    return output;
}

function decodeCqJsonData(data) {
    if (!data) return '';
    let decoded = String(data);
    const entities = {
        '&quot;': '"',
        '&#34;': '"',
        '&#39;': "'",
        '&apos;': "'",
        '&#44;': ',',
        '&#91;': '[',
        '&#93;': ']',
        '&lt;': '<',
        '&gt;': '>',
        '&amp;': '&'
    };

    for (let i = 0; i < 3; i++) {
        const next = decoded.replace(/&(quot|apos|lt|gt|amp);|&#(34|39|44|91|93);/g, match => entities[match] || match);
        if (next === decoded) break;
        decoded = next;
    }
    return decoded;
}

function parseCqJsonData(data) {
    try {
        return JSON.parse(decodeCqJsonData(data));
    } catch (err) {
        return null;
    }
}

function decodeCqParamValue(value, htmlEscaped = false) {
    if (!value) return '';
    let decoded = String(value);
    // Media markers have already passed through escapeHtmlText. Remove exactly
    // that layer before CQ decoding, preserving literal entities in the URL.
    if (htmlEscaped) {
        decoded = decoded.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
    }
    return decoded
        .replace(/&amp;/g, '&')
        .replace(/&#44;/g, ',')
        .replace(/&#91;/g, '[')
        .replace(/&#93;/g, ']');
}

function getCqParamValue(inner, key) {
    const pattern = new RegExp(`(?:^|,)${key}=([\\s\\S]*?)(?=,[A-Za-z_][\\w.-]*=|$)`, 'i');
    const match = safeText(inner).match(pattern);
    return match ? match[1] : '';
}

function isInlineCqMediaSource(value) {
    const text = decodeCqParamValue(value).trim();
    return /^(?:base64:\/\/|data:[^,\s]+;base64,)/i.test(text);
}

function inlineCqMediaPlaceholder(type, inner) {
    const normalized = safeText(type).toLowerCase();
    if (normalized === 'image') return '[CQ:image]';
    if (normalized === 'video') return '[CQ:video]';
    if (normalized === 'record') return '[语音]';
    if (normalized === 'file') {
        const name = decodeCqParamValue(getCqParamValue(inner, 'name')).trim();
        return name ? `[文件: ${name}]` : '[文件]';
    }
    return '';
}

function cleanInlineCqMediaCodes(value) {
    const text = safeText(value);
    if (!/(?:base64:\/\/|;base64(?:,|&#44;))/i.test(text)) return text;
    return text.replace(/\[CQ:(image|video|record|file),([^\]]*)\]/gi, (match, type, inner) => {
        const source = getCqParamValue(inner, 'url') || getCqParamValue(inner, 'file');
        return isInlineCqMediaSource(source) ? inlineCqMediaPlaceholder(type, inner) || match : match;
    });
}

function getJsonFieldFromText(text, field) {
    if (!text) return '';
    const reg = new RegExp(`"${field}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"`);
    const match = String(text).match(reg);
    if (!match) return '';
    return match[1]
        .replace(/\\"/g, '"')
        .replace(/\\\\\//g, '/')
        .replace(/\\\\n/g, '\n')
        .trim();
}

function firstText(...values) {
    for (const value of values) {
        if (typeof value === 'string' && value.trim()) return value.trim();
        if (typeof value === 'number' && Number.isFinite(value)) return String(value);
    }
    return '';
}

function eachShareMeta(payload, callback) {
    if (!payload || typeof payload !== 'object' || !payload.meta || typeof payload.meta !== 'object') return;
    Object.values(payload.meta).forEach(item => {
        if (item && typeof item === 'object') callback(item);
    });
}

function detectSharePlatformFromUrl(url) {
    if (!url) return '';
    const value = String(url).toLowerCase();
    const patterns = [
        [/bilibili\.com|b23\.tv|bili2233\.cn/, '哔哩哔哩'],
        [/music\.163\.com|y\.music\.163\.com/, '网易云音乐'],
        [/y\.qq\.com|c6\.y\.qq\.com|i\.y\.qq\.com/, 'QQ音乐'],
        [/douyin\.com|iesdouyin\.com/, '抖音'],
        [/kuaishou\.com|gifshow\.com/, '快手'],
        [/xiaohongshu\.com|xhslink\.com/, '小红书'],
        [/weibo\.com|weibo\.cn/, '微博'],
        [/zhihu\.com/, '知乎'],
        [/github\.com/, 'GitHub'],
        [/mp\.weixin\.qq\.com/, '微信文章'],
        [/acfun\.cn/, 'AcFun']
    ];
    const match = patterns.find(([reg]) => reg.test(value));
    return match ? match[1] : '';
}

function getCqJsonShareInfo(data) {
    const payload = parseCqJsonData(data);
    const decodedData = decodeCqJsonData(data);
    if (!payload) {
        const tag = getJsonFieldFromText(decodedData, 'tag');
        const source = getJsonFieldFromText(decodedData, 'source');
        const sourceName = getJsonFieldFromText(decodedData, 'source_name');
        const metaTitle = getJsonFieldFromText(decodedData, 'title');
        const metaDesc = getJsonFieldFromText(decodedData, 'desc');
        const prompt = getJsonFieldFromText(decodedData, 'prompt');
        const urls = ['qqdocurl', 'jumpUrl', 'url', 'source_url'].map(key => getJsonFieldFromText(decodedData, key));
        const platform = firstText(tag, source, sourceName, urls.map(detectSharePlatformFromUrl).find(Boolean), metaTitle, prompt.match(/^\[([^\]]+)\]/)?.[1], '链接分享');
        const title = (metaTitle && metaTitle !== platform ? metaTitle : metaDesc) || prompt.replace(/^\[[^\]]+\]\s*/, '');
        return { platform, title };
    }

    let platform = '';
    let title = '';
    let metaTitle = '';
    let metaDesc = '';
    const urls = [];

    eachShareMeta(payload, item => {
        platform = platform || firstText(item.tag, item.source, item.source_name);
        metaTitle = metaTitle || firstText(item.title);
        metaDesc = metaDesc || firstText(item.desc);
        ['qqdocurl', 'jumpUrl', 'url', 'source_url', 'preview'].forEach(key => {
            if (item[key]) urls.push(item[key]);
        });
    });

    const promptPlatform = firstText(payload.prompt).match(/^\[([^\]]+)\]/)?.[1] || '';
    platform = platform || urls.map(detectSharePlatformFromUrl).find(Boolean) || metaTitle || promptPlatform || firstText(payload.app);
    title = (metaTitle && metaTitle !== platform ? metaTitle : metaDesc) || firstText(payload.prompt).replace(/^\[[^\]]+\]\s*/, '') || firstText(payload.desc);

    return {
        platform: platform || '链接分享',
        title
    };
}

function formatSharePreview(data) {
    const info = getCqJsonShareInfo(data);
    return info.title ? `[${info.platform}] ${info.title}` : `[${info.platform}]`;
}

function isShareJsonPayload(text) {
    const payload = parseCqJsonData(text);
    return Boolean(payload && typeof payload === 'object' && payload.meta && payload.app);
}

function cancelMemberSearch() {
    clearTimeout(memberSearchTimer);
    memberSearchTimer = null;
}

function debounceMemberSearch(value) {
    const requestSessionId = activeSessionId;
    memberSearchKeyword = safeText(value).trim();
    cancelMemberSearch();
    memberSearchTimer = setTimeout(() => {
        memberSearchTimer = null;
        if (activeSessionId !== requestSessionId) return;
        fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: 0, append: false });
    }, 180);
}

function toggleCategory(header, content) {
    const willCollapse = !header.classList.contains('collapsed');
    header.classList.toggle('collapsed', willCollapse);
    header.setAttribute('aria-expanded', String(!willCollapse));
    content.toggleAttribute('inert', willCollapse);
    content.setAttribute('aria-hidden', String(willCollapse));

    if (willCollapse) {
        if (content.contains(document.activeElement)) header.focus({ preventScroll: true });
        content.style.maxHeight = `${content.scrollHeight}px`;
        content.offsetHeight;
        content.classList.add('hidden');
    } else {
        content.style.maxHeight = '0px';
        content.classList.remove('hidden');
        content.offsetHeight;
        content.style.maxHeight = `${content.scrollHeight}px`;
        const clearHeight = () => {
            if (!content.classList.contains('hidden')) content.style.maxHeight = '';
            content.removeEventListener('transitionend', clearHeight);
        };
        content.addEventListener('transitionend', clearHeight);
    }
}

window.toggleForwardCard = (headerEl) => {
    const container = headerEl.closest('.msg-forward-container');
    const content = container.querySelector('.msg-forward-content');
    if (!container || !content) return;

    const isCollapsed = content.classList.contains('collapsed');
    if (isCollapsed) {
        content.classList.remove('collapsed');
        content.removeAttribute('inert');
        content.setAttribute('aria-hidden', 'false');
        container.classList.add('expanded');
        headerEl.setAttribute('aria-expanded', 'true');
        const btn = container.querySelector('.msg-forward-toggle-btn');
        if (btn) btn.textContent = '收起 ';
    } else {
        if (content.contains(document.activeElement)) headerEl.focus({ preventScroll: true });
        content.classList.add('collapsed');
        content.setAttribute('inert', '');
        content.setAttribute('aria-hidden', 'true');
        container.classList.remove('expanded');
        headerEl.setAttribute('aria-expanded', 'false');
        const btn = container.querySelector('.msg-forward-toggle-btn');
        if (btn) btn.textContent = '展开 ';
    }
};

function renderMergedForwardCard(forwardId, rest, depth = 0) {
    const lines = safeText(rest).replace(/\r\n?/g, '\n').split('\n');
    const items = [];
    let currentItem = null;
    let afterText = '';

    lines.forEach(line => {
        const lineMatch = line.match(/^\d+\.\s+([^\n:]+):\s*([\s\S]*)$/);
        if (lineMatch) {
            if (currentItem) {
                items.push(currentItem);
            }
            currentItem = {
                sender: lineMatch[1].trim(),
                content: lineMatch[2].trim()
            };
        } else if (currentItem) {
            currentItem.content += '\n' + line.replace(/^    /, '');
        } else if (line.trim()) {
            afterText += line + '\n';
        }
    });
    if (currentItem) {
        items.push(currentItem);
    }

    let html = '';
    if (items.length > 0) {
        const idDisplay = forwardId ? ` (ID: ${escapeAttr(forwardId)})` : '';
        const itemsHtml = items.map(item => `
            <div class="msg-forward-item">
                <span class="msg-forward-sender">${escapeAttr(item.sender)}</span>
                <span class="msg-forward-text">${formatMsg(item.content, depth + 1)}</span>
            </div>
        `).join('');

        html = `
            <div class="msg-forward-container">
                <div class="msg-forward-header" data-forward-toggle="true" aria-expanded="false">
                    <div class="msg-forward-title-row">
                        <span class="msg-forward-icon">${uiIcon('folder')}</span>
                        <span class="msg-forward-title">合并转发消息</span>
                        <span class="msg-forward-count">(共 ${items.length} 条消息${idDisplay})</span>
                    </div>
                    <span class="msg-forward-toggle-btn">展开 </span>
                </div>
                <div class="msg-forward-content collapsed" aria-hidden="true" inert>
                    ${itemsHtml}
                </div>
            </div>
        `;
    } else {
        const idDisplay = forwardId ? ` (未展开, ID: ${escapeAttr(forwardId)})` : ' (未展开)';
        html = `
            <div class="msg-forward-container unexpanded">
                <div class="msg-forward-header is-static">
                    <div class="msg-forward-title-row">
                        <span class="msg-forward-icon">${uiIcon('folder')}</span>
                        <span class="msg-forward-title">合并转发消息</span>
                        <span class="msg-forward-count">${idDisplay}</span>
                    </div>
                </div>
            </div>
        `;
    }

    return { html, afterText };
}

function replaceMergedForwardCodes(text, replacer) {
    const value = safeText(text);
    // New archives have balanced boundaries; retain the legacy text reader below.
    if (value.includes('[合并转发结束]')) {
        const tokens = /\[合并转发(?:,id=([^\]]*))?\]|\[合并转发结束\]/g;
        let level = 0, start = 0, contentStart = 0, forwardId = '', result = '', cursor = 0;
        for (const token of value.matchAll(tokens)) {
            if (token[0] === '[合并转发结束]') {
                if (level && --level === 0) {
                    result += value.slice(cursor, start) + replacer(forwardId, value.slice(contentStart, token.index).replace(/^\r?\n/, '').replace(/\r?\n\s*$/, ''));
                    cursor = token.index + token[0].length;
                }
            } else {
                if (level++ === 0) {
                    start = token.index;
                    contentStart = token.index + token[0].length;
                    forwardId = decodeCqParamValue(token[1] || '');
                }
            }
        }
        return result + value.slice(cursor);
    }
    const index = value.indexOf('[合并转发');
    if (index === -1) return value;

    const beforeText = value.substring(0, index);
    const forwardChunk = value.substring(index);
    const match = forwardChunk.match(/\[合并转发(?:,id=([^\]]*))?\](?:\r?\n)?([\s\S]*)/i);
    if (!match) return value;

    const replacement = replacer(match[1] || '', match[2] || '');
    return beforeText + replacement;
}

function formatMsg(text, depth = 0) {
    text = cleanInlineCqMediaCodes(normalizeArchiveMessageText(text));
    if (!text) return "";
    if (depth > 8) return escapeHtmlText(text);

    if (text.startsWith("<Event,") || (typeof text === 'string' && text.includes("'raw_message':"))) {
        const match = text.match(/['"]raw_message['"]\s*:\s*['"](.*?)['"]/);
        if (match && match[1]) {
            text = match[1];
        } else {
            return `<span class="msg-unparsed">[无法解析的消息内容]</span>`;
        }
    }

    // QQ's advanced Markdown command tags are display labels in the archive.
    // Restore them only after Markdown/CQ rendering so decoded text stays inert.
    const commandLabels = [];
    text = safeText(text).replace(/<qqbot-cmd-input\b[^>]*\/>/gi, tag => {
        const show = tag.match(/\s+show\s*=\s*(["'])(.*?)\1/i);
        if (!show) return tag;
        let label = show[2];
        try { label = decodeURIComponent(label); } catch (_) { /* Preserve malformed encoding. */ }
        const index = commandLabels.length;
        commandLabels.push(escapeHtmlText(label));
        return makePrivateToken('QQCOMMAND', index);
    });

    const forwardCards = [];
    const makeForwardPlaceholder = (forwardId, rest) => {
        const index = forwardCards.length;
        const rendered = renderMergedForwardCard(forwardId, rest, depth);
        forwardCards.push(rendered.html);
        return makePrivateToken('FORWARD', index) + rendered.afterText;
    };

    const textWithForwardPlaceholders = replaceMergedForwardCodes(text, makeForwardPlaceholder);

    const shareCards = [];
    const makeSharePlaceholder = data => {
        const info = getCqJsonShareInfo(data);
        const safePlatform = escapeAttr(info.platform);
        const safeTitle = escapeAttr(info.title);
        const titleHtml = safeTitle ? `<span class="msg-share-title">${safeTitle}</span>` : '';
        const index = shareCards.length;
        shareCards.push(`<span class="msg-share-card"><span class="msg-share-platform">${uiIcon('link', 'msg-share-icon')} ${safePlatform}</span>${titleHtml}</span>`);
        return `__CQ_JSON_SHARE_${index}__`;
    };

    const textWithSharePlaceholders = isShareJsonPayload(String(textWithForwardPlaceholders).trim())
        ? makeSharePlaceholder(String(textWithForwardPlaceholders).trim())
        : replaceCqJsonCodes(textWithForwardPlaceholders, makeSharePlaceholder);

    let escaped = escapeHtmlText(textWithSharePlaceholders);
    const markdown = renderMessageMarkdown(escaped);
    escaped = markdown.html;

    const isSafeUrl = url => isSafeResourceUrl(url);

    // Never let archive-controlled media URLs make the browser contact a
    // remote/LAN host directly. The authenticated backend enforces domain,
    // DNS-address and content-type policy.
    function proxyUrl(url) {
        // CQ parameters have already been decoded exactly once.
        if (isSafeResourceUrl(url)) return url;
        if (isSafeNavigationUrl(url) && /^https?:\/\//i.test(url)) return `/api/proxy/image?url=${encodeURIComponent(url)}`;
        return "";
    }

    // CQ Code Handling
    shareCards.forEach((html, index) => {
        escaped = escaped.replace(`__CQ_JSON_SHARE_${index}__`, html);
    });

    // Images
    escaped = escaped.replace(/\[CQ:image,([^\]]+)\]/g, (match, inner) => {
        if (!showMessageMedia) return '<span class="msg-tag msg-tag-muted" data-media-hidden="image">[图片已隐藏]</span>';
        const urlMatch = inner.match(/url=([^,\]]+)/);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1], true);
            url = proxyUrl(url);
            if (!isSafeUrl(url)) return `<span class="msg-tag">${uiIcon('image', 'msg-tag-icon')} [图片]</span>`;
            const safeUrl = escapeAttr(url);
            const widthMatch = inner.match(/(?:^|,)width=(\d+)(?:,|$)/);
            const heightMatch = inner.match(/(?:^|,)height=(\d+)(?:,|$)/);
            const width = widthMatch ? parseInt(widthMatch[1], 10) : 0;
            const height = heightMatch ? parseInt(heightMatch[1], 10) : 0;
            const sizeStyle = getImageDisplayStyle(width, height);
            return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer"><img src="${safeUrl}" class="msg-image" alt="图片" loading="lazy"${sizeStyle} onload="this.classList.add('loaded')" onerror="this.parentElement.outerHTML='<span class=\\'msg-tag msg-tag-muted\\'>[图片无法加载]</span>'" /></a>`;
        }
        return `<span class="msg-tag">${uiIcon('image', 'msg-tag-icon')} [图片]</span>`;
    });
    escaped = escaped.replace(/\[CQ:image\]/g, `<span class="msg-tag">${uiIcon('image', 'msg-tag-icon')} [图片]</span>`);

    // QQ Faces
    escaped = escaped.replace(/\[CQ:face,id=(\d+)[^\]]*\]/g, (match, id) => {
        const faceUrl = proxyUrl(`https://gxh.vip.qq.com/sys/hycdn/sng/face/s/${id}.png`);
        return `<img src="${escapeAttr(faceUrl)}" class="msg-face" alt="表情" loading="lazy" onload="this.style.background='none'" onerror="this.style.display='none'" />`;
    });
    escaped = escaped.replace(/\[CQ:face,[^\]]*\]/g, `<span class="msg-tag">${uiIcon('image', 'msg-tag-icon')} 表情</span>`);

    // Video Handling
    escaped = escaped.replace(/\[CQ:video,([^\]]+)\]/g, (match, inner) => {
        if (!showMessageMedia) return '<span class="msg-tag msg-tag-muted" data-media-hidden="video">[视频已隐藏]</span>';
        const urlMatch = inner.match(/url=([^,\]]+)/);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1], true);
            url = proxyUrl(url);
            if (!isSafeUrl(url)) return `<span class="msg-tag">${uiIcon('video', 'msg-tag-icon')} [视频]</span>`;
            const safeUrl = escapeAttr(url);
            return `<video src="${safeUrl}" controls class="msg-video" preload="metadata" aria-label="视频消息" onerror="this.outerHTML='<span class=\\'msg-tag msg-tag-muted\\'>[视频无法加载]</span>'"></video>`;
        }
        return `<span class="msg-tag">${uiIcon('video', 'msg-tag-icon')} [视频]</span>`;
    });
    escaped = escaped.replace(/\[CQ:video\]/g, `<span class="msg-tag">${uiIcon('video', 'msg-tag-icon')} [视频]</span>`);

    // Voice/Record Handling
    escaped = escaped.replace(/\[CQ:record,([^\]]+)\]/g, (match, inner) => {
        const urlMatch = inner.match(/url=([^,\]]+)/);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1], true);
            url = proxyUrl(url);
            if (!isSafeUrl(url)) return `<span class="msg-tag">${uiIcon('audio', 'msg-tag-icon')} [语音]</span>`;
            const safeUrl = escapeAttr(url);
            return `<div class="msg-audio-wrap"><span class="msg-media-icon" aria-hidden="true">${uiIcon('audio')}</span><audio src="${safeUrl}" controls preload="metadata" class="msg-audio" aria-label="语音消息" onerror="this.parentElement.outerHTML='<span class=\\'msg-tag msg-tag-muted\\'>[语音无法加载]</span>'"></audio></div>`;
        }
        return `<span class="msg-tag">${uiIcon('audio', 'msg-tag-icon')} [语音]</span>`;
    });
    escaped = escaped.replace(/\[CQ:record\]/g, `<span class="msg-tag">${uiIcon('audio', 'msg-tag-icon')} [语音]</span>`);

    // File Handling
    escaped = escaped.replace(/\[CQ:file,([^\]]+)\]/g, (match, inner) => {
        const nameMatch = inner.match(/name=([^,\]]+)/);
        const urlMatch = inner.match(/url=([^,\]]+)/);
        const fileName = nameMatch && nameMatch[1] ? decodeCqParamValue(nameMatch[1], true) : '文件';
        const safeName = escapeAttr(fileName);
        if (urlMatch && urlMatch[1]) {
            const url = decodeCqParamValue(urlMatch[1], true);
            if (isSafeResourceUrl(url) || isSafeNavigationUrl(url)) {
                return `<a class="msg-tag" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${uiIcon('file', 'msg-tag-icon')} ${safeName}</a>`;
            }
        }
        return `<span class="msg-tag">${uiIcon('file', 'msg-tag-icon')} ${safeName}</span>`;
    });
    escaped = escaped.replace(/\[CQ:file\]/g, `<span class="msg-tag">${uiIcon('file', 'msg-tag-icon')} [文件]</span>`);

    const tags = ["动画表情", "文件", "红包"];
    tags.forEach(tag => {
        const regex = new RegExp(`\\[${tag}\\]`, 'g');
        escaped = escaped.replace(regex, `<span class="msg-tag">${uiIcon('file', 'msg-tag-icon')} [${tag}]</span>`);
    });
    // Fallback plain text tags for voice/video without CQ codes
    escaped = escaped.replace(/\[语音\]/g, `<span class="msg-tag">${uiIcon('audio', 'msg-tag-icon')} [语音]</span>`);
    escaped = escaped.replace(/\[视频\]/g, `<span class="msg-tag">${uiIcon('video', 'msg-tag-icon')} [视频]</span>`);

    escaped = escaped.replace(/\[CQ:at,qq=all[^\]]*\]/g, '<span class="msg-tag msg-tag-danger">@全体成员</span>');
    escaped = escaped.replace(/\[CQ:at,qq=(\d+)[^\]]*\]/g, (match, qq) => {
        let name = window.userMap && window.userMap[qq] ? window.userMap[qq] : qq;
        const safeName = escapeAttr(name);
        return `<span class="msg-tag msg-tag-person">
            <img src="${escapeAttr(getAvatarUrl(qq))}" alt="" onerror="this.src=getAvatarUrl('fallback')" class="msg-inline-avatar" />
            @${safeName}
        </span>`;
    });
    escaped = escaped.replace(/\[CQ:reply,[^\]]*\]/g, `<span class="msg-tag msg-tag-subtle">${uiIcon('reply', 'msg-tag-icon')} 回复</span>`);

    // Recall Links
    escaped = escaped.replace(/(?:🛡️ )?\[撤回了一条消息 \(ID: ([^\]]+)\)\]/g, (match, id) => {
        const safeId = escapeAttr(id);
        return `[撤回了一条消息 (ID: <span class="recall-link" data-msg-id="${safeId}" role="button" tabindex="0">${safeId}</span>)]`;
    });

    forwardCards.forEach((html, index) => {
        escaped = escaped.split(makePrivateToken('FORWARD', index)).join(html);
    });

    let rendered = markdown.restore(escaped);
    commandLabels.forEach((label, index) => {
        rendered = rendered.split(makePrivateToken('QQCOMMAND', index)).join(label);
    });
    return rendered;
}

function settleLoadedImages(container) {
    container.querySelectorAll('img.msg-image').forEach(img => {
        if (img.complete && img.naturalWidth > 0) {
            img.classList.add('loaded');
        }
    });
}

function enhanceRenderedMessageContent(container) {
    container.querySelectorAll('.recall-link').forEach(el => {
        makeKeyboardActivatable(el, '定位被撤回的消息');
    });
    container.querySelectorAll('[data-forward-toggle="true"]').forEach(el => {
        makeKeyboardActivatable(el, '展开或收起合并转发消息');
        el.addEventListener('click', () => window.toggleForwardCard(el));
    });
    settleLoadedImages(container);
}

function centerAndHighlightMessage(list, element) {
    if (!list || !element) return false;
    const listRect = list.getBoundingClientRect();
    const elementRect = element.getBoundingClientRect();
    const targetTop = list.scrollTop
        + elementRect.top
        - listRect.top
        - Math.max(0, (list.clientHeight - elementRect.height) / 2);
    highlightedMessageId = safeText(element.dataset.msgId);
    clearTimeout(highlightedMessageTimer);
    list.scrollTo({ top: Math.max(0, targetTop), behavior: 'smooth' });
    element.classList.add('highlight-flash');
    highlightedMessageTimer = setTimeout(() => {
        highlightedMessageId = '';
        document.querySelectorAll('.msg-bubble.highlight-flash').forEach(el => el.classList.remove('highlight-flash'));
    }, 2000);
    return true;
}

function virtualRowContainsMessageId(row, targetId) {
    const messages = row?.type === 'group' ? row.messages : row?.msg ? [row.msg] : [];
    return messages.some(msg => {
        const platformMessageId = safeText(msg.msg_id || msg.message_id);
        return platformMessageId ? platformMessageId === targetId : safeText(msg.id) === targetId;
    });
}

function renderVirtualMessageTarget(msgId) {
    const list = document.getElementById('messageList');
    const targetId = safeText(msgId);
    const rowIndex = virtualRows.findIndex(row => virtualRowContainsMessageId(row, targetId));
    if (!list || rowIndex < 0) return false;

    timeline?.scrollToIndex(rowIndex);

    const safeMsgId = escapeCssValue(targetId);
    const target = list.querySelector(`.msg-bubble[data-msg-id="${safeMsgId}"]`);
    if (target) {
        centerAndHighlightMessage(list, target);
    } else {
        requestAnimationFrame(() => {
            const retryTarget = list.querySelector(`.msg-bubble[data-msg-id="${safeMsgId}"]`);
            if (retryTarget) centerAndHighlightMessage(list, retryTarget);
            else showMessageNotLoadedToast(targetId);
        });
    }
    return true;
}

function showMessageNotLoadedToast(msgId) {
    console.warn(`Message ${msgId} is not loaded.`);
    showClipboardToast('该消息不在当前加载范围内');
}

window.scrollToMsg = (msgId) => {
    const list = document.getElementById('messageList');
    const safeMsgId = escapeCssValue(msgId);
    const rendered = list?.querySelector(`.msg-bubble[data-msg-id="${safeMsgId}"]`);
    if (rendered && centerAndHighlightMessage(list, rendered)) return;
    if (renderVirtualMessageTarget(msgId)) return;
    showMessageNotLoadedToast(msgId);
};

document.addEventListener('click', (event) => {
    const recall = event.target.closest('.recall-link[data-msg-id]');
    if (recall) {
        window.scrollToMsg(recall.getAttribute('data-msg-id') || '');
        return;
    }

    const copy = event.target.closest('.msg-id[data-copy-id]');
    if (copy) {
        window.copyToClipboard(copy.getAttribute('data-copy-id') || '');
    }
});

function isSafeAvatarUrl(url) {
    return isSafeResourceUrl(url);
}

function isQqLikePlatform(platformName = '') {
    const platform = safeText(platformName).toLowerCase();
    return !platform || platform.includes('qq') || platform.includes('onebot') || platform === 'aiocqhttp';
}

function getAvatarUrl(userId, avatarUrl = '', platformName = '') {
    const directUrl = safeText(avatarUrl);
    const proxiedDirectUrl = getMediaResourceUrl(directUrl);
    if (proxiedDirectUrl && isSafeAvatarUrl(proxiedDirectUrl)) {
        return proxiedDirectUrl;
    }
    if (isQqLikePlatform(platformName) && /^\d+$/.test(userId)) {
        return getMediaResourceUrl(`https://q1.qlogo.cn/g?b=qq&nk=${userId}&s=100`);
    }
    return `data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%2364748b'%3E%3Cpath d='M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z'/%3E%3C/svg%3E`;
}

function preloadAvatar(userId, avatarUrl = '', platformName = '') {
    const key = `${safeText(platformName)}:${safeText(userId)}:${safeText(avatarUrl)}`;
    if (avatarPreloadCache.has(key)) return avatarPreloadCache.get(key);

    const url = getAvatarUrl(userId, avatarUrl, platformName);
    const promise = new Promise(resolve => {
        const img = new Image();
        let settled = false;
        const done = (src) => {
            if (settled) return;
            settled = true;
            setCappedMap(avatarResolvedCache, key, src);
            resolve(src);
        };
        img.onload = () => {
            if (img.decode) img.decode().then(() => done(url)).catch(() => done(url));
            else done(url);
        };
        img.onerror = () => done(getAvatarUrl('fallback'));
        img.src = url;
        setTimeout(() => done(url), 800);
    });
    return setCappedMap(avatarPreloadCache, key, promise);
}

async function preloadUserAvatars(users, limit = 12) {
    const pending = users.slice(0, limit).map(u => preloadAvatar(u.user_id, u.avatar_url, u.platform_name));
    await Promise.allSettled(pending);
}

function showSkeleton(containerId, count = 5) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.setAttribute('aria-busy', 'true');
    container.querySelectorAll('.message-group, .skeleton-group, .date-divider, .empty-state, .loading-label, .slow-request-note').forEach(el => el.remove());
    const loadingLabel = document.createElement('p');
    loadingLabel.className = 'loading-label';
    loadingLabel.setAttribute('role', 'status');
    loadingLabel.textContent = getActiveSearchKeyword() ? '正在搜索归档…' : '正在加载记录…';
    container.appendChild(loadingLabel);
    for (let i = 0; i < count; i++) {
        const sk = document.createElement('div');
        sk.className = 'skeleton-group animate-fade';
        sk.style.animationDelay = `${i * 0.05}s`;
        sk.innerHTML = `
            <div class="avatar-col">
                <div class="skeleton skeleton-avatar"></div>
            </div>
            <div class="content-col skeleton-content">
                <div class="skeleton skeleton-name"></div>
                <div class="skeleton skeleton-message"></div>
            </div>
        `;
        container.appendChild(sk);
    }
}

const SEARCH_CODE_PREVIEW_MAX_CHARS = 1200;

function getActiveSearchKeyword() {
    return activeSearchKeyword;
}

function syncSearchCancelControl() {
    const input = document.getElementById('searchInput');
    const cancel = document.getElementById('searchCancelBtn');
    if (!input || !cancel) return;
    const hasSearch = Boolean(input.value.trim() || activeSearchKeyword);
    const canCancelRequest = isHistoryLoading && Boolean(activeSearchKeyword);
    cancel.hidden = !hasSearch;
    cancel.setAttribute('aria-label', canCancelRequest ? '取消当前搜索' : '清除搜索');
    cancel.title = canCancelRequest ? '取消当前搜索' : '清除搜索';
}

function abortHistoryRequest({ invalidate = true } = {}) {
    if (historyAbortController) historyAbortController.abort();
    historyAbortController = null;
    if (invalidate) historyRequestSeq += 1;
    isHistoryLoading = false;
    document.getElementById('messageList')?.setAttribute('aria-busy', 'false');
    const loadMore = document.getElementById('loadMoreBtn');
    if (loadMore) loadMore.disabled = false;
    syncSearchCancelControl();
}

function cancelSearch() {
    const input = document.getElementById('searchInput');
    const hadSearch = Boolean(input?.value.trim() || activeSearchKeyword);
    const hadCommittedSearch = Boolean(activeSearchKeyword);
    if (input) input.value = '';
    if (!hadCommittedSearch) {
        syncSearchCancelControl();
        const status = document.getElementById('app-status');
        if (status && hadSearch) status.textContent = '搜索输入已清除';
        return;
    }
    abortHistoryRequest();
    activeSearchKeyword = '';
    currentPage = 1;
    nextCursor = 0;
    syncSearchCancelControl();
    if (!hadSearch) return;
    const status = document.getElementById('app-status');
    if (status) status.textContent = '搜索已取消';
    if (activeSessionId) fetchHistory();
    else showDashboard();
}

function looksLikeWebCodeContent(value) {
    const text = normalizeArchiveMessageText(value);
    if (!text) return false;
    if (/@font-face\s*\{/i.test(text)) return true;
    if (/<\/?(?:!doctype|html|head|body|style|script|link|meta|template|svg|div|span|canvas|iframe|object|embed|font)\b/i.test(text)) return true;
    if (/\b(?:document|window|localStorage|sessionStorage)\s*\./.test(text)) return true;
    if (/\b(?:function|const|let|var|class)\s+[A-Za-z_$][\w$]*\b/.test(text)) return true;
    if (/\b(?:import|export)\s+(?:\{|default|from|[A-Za-z_$])/.test(text)) return true;
    if (/\b(?:font-family|src\s*:\s*url\(|@import|@keyframes|animation|position\s*:|display\s*:|background(?:-color)?\s*:|z-index\s*:)/i.test(text)) return true;
    if (/^[\s\S]{0,200}[.#]?[A-Za-z_-][\w-]*\s*\{[\s\S]*:[\s\S]*\}/.test(text) && /;\s*\}/.test(text)) return true;
    return false;
}

function makeSearchCodePreviewText(value) {
    const text = normalizeArchiveMessageText(value).replace(/\r\n?/g, '\n');
    if (text.length <= SEARCH_CODE_PREVIEW_MAX_CHARS) return { text, truncated: false, length: text.length };
    return {
        text: `${text.slice(0, SEARCH_CODE_PREVIEW_MAX_CHARS)}\n… [代码内容已截断，仅显示前 ${SEARCH_CODE_PREVIEW_MAX_CHARS.toLocaleString()} / ${text.length.toLocaleString()} 字符]`,
        truncated: true,
        length: text.length,
    };
}

function shouldUseSearchCodePreview(msg) {
    return Boolean(!msg?.full_message_loaded && getActiveSearchKeyword() && looksLikeWebCodeContent(msg?.message || ''));
}

function appendSearchCodePreview(container, msg) {
    const preview = makeSearchCodePreviewText(msg?.message || '');
    const pre = document.createElement('pre');
    pre.className = 'msg-md-codeblock msg-search-code-preview';
    if (preview.truncated) pre.title = `原始长度 ${preview.length.toLocaleString()} 字符`;
    const code = document.createElement('code');
    // 关键：搜索结果里的 HTML/CSS/JS 代码只作为文本节点展示，禁止进入 innerHTML。
    code.textContent = preview.text;
    pre.appendChild(code);
    container.appendChild(pre);
    return preview;
}

function formatMsgCached(msg) {
    const key = `${safeCount(msg.id)}:${safeCount(msg.message_length)}:${safeCount(msg.message_truncated)}:${safeText(msg.msg_id)}:${hashText(msg.message || '')}`;
    const cache = msg.full_message_loaded ? fullMessageFormattedCache : formattedMsgCache;
    if (cache.has(key)) return cache.get(key);
    return setCappedMap(cache, key, formatMsg(msg.message || ''), msg.full_message_loaded ? 4 : CLIENT_CACHE_MAX);
}

function getVirtualRowForMessage(msg) {
    return virtualRows.find(row => (
        row?.msg === msg
        || (row?.type === 'group' && row.messages.includes(msg))
    ));
}

async function loadFullMessage(msg, button) {
    const recordId = Number(msg?.id);
    if (!Number.isSafeInteger(recordId) || recordId <= 0) {
        if (button) {
            button.textContent = '无法加载全文';
            button.disabled = true;
        }
        return;
    }

    const requestKey = safeText(recordId);
    const requestViewKey = activeHistoryViewKey;
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.textContent = '加载中…';
    }

    let requestEntry = fullMessageRequests.get(requestKey);
    if (!requestEntry) {
        const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
        const request = fetchAPI(
            `/api/history?record_id=${encodeURIComponent(requestKey)}&full_message=true&limit=1`,
            'GET',
            null,
            { signal: controller?.signal }
        )
            .then(data => {
                const records = Array.isArray(data?.data) ? data.data : [];
                if (!data?.success || records.length !== 1 || safeText(records[0]?.id) !== requestKey) {
                    throw new Error('Full message record was not returned');
                }
                return records[0];
            });
        requestEntry = { controller, request };
        fullMessageRequests.set(requestKey, requestEntry);
    }

    try {
        const record = await requestEntry.request;
        const viewIsCurrent = activeHistoryViewKey === requestViewKey && virtualMessages.includes(msg);
        if (!viewIsCurrent) return;

        msg.message = safeText(record.message);
        msg.message_length = safeCount(record.message_length) || msg.message.length;
        msg.message_truncated = 0;
        msg.full_message_loaded = true;

        const row = getVirtualRowForMessage(msg);
        if (row) timeline?.invalidate(row.key);
    } catch (error) {
        if (error.name === 'AbortError') return;
        console.error('Full message request failed', error);
        if (button?.isConnected && activeHistoryViewKey === requestViewKey) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
            button.textContent = '重试全文';
            button.title = '全文加载失败，点击重试';
        }
    } finally {
        if (fullMessageRequests.get(requestKey) === requestEntry) {
            fullMessageRequests.delete(requestKey);
        }
    }
}

function createFullMessageButton(msg) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'msg-tag';
    button.title = `原始长度 ${safeCount(msg.message_length).toLocaleString()} 字符，点击加载全文`;
    button.textContent = '加载全文';
    button.addEventListener('click', event => {
        event.stopPropagation();
        loadFullMessage(msg, button);
    });
    return button;
}

function createMessageBubble(msg, animate = false) {
    const isRecalled = msg.is_recalled === 1;
    const bubble = document.createElement('div');
    bubble.className = `msg-bubble${animate ? ' animate-fade' : ''} ${isRecalled ? 'recalled-msg' : ''}`;
    bubble.dataset.msgId = safeText(msg.msg_id);
    if (highlightedMessageId && bubble.dataset.msgId === highlightedMessageId) {
        bubble.classList.add('highlight-flash');
    }

    const text = document.createElement('div');
    text.className = 'msg-text';
    const usedCodePreview = shouldUseSearchCodePreview(msg);
    if (usedCodePreview) {
        appendSearchCodePreview(text, msg);
    } else {
        text.innerHTML = formatMsgCached(msg);
        enhanceRenderedMessageContent(text);
        if (text.querySelector('.msg-image, .msg-video')) {
            bubble.classList.add('msg-bubble-media');
        }
    }

    const footer = document.createElement('div');
    footer.className = 'msg-footer';

    const id = document.createElement('span');
    id.className = 'msg-id';
    const messageId = safeText(msg.msg_id);
    const isBotMessageId = /^bot_\d+_\d+$/.test(messageId);
    const displayId = isBotMessageId && messageId.length > 20 ? `bot_…${messageId.slice(-8)}` : messageId || 'N/A';
    id.title = `${isBotMessageId ? '机器人归档消息 ID' : '平台消息 ID'}：${messageId || 'N/A'}（点击复制）`;
    makeKeyboardActivatable(id, '复制完整消息 ID');
    id.dataset.copyId = messageId;
    id.textContent = `#${displayId}`;
    footer.appendChild(id);

    const sid = safeText(msg.session_id) || 'legacy:archive';
    if (!activeSessionId) {
        const origin = document.createElement('a');
        const url = new URL(window.location.href);
        url.searchParams.set('session_id', sid);
        origin.href = url.pathname + url.search;
        origin.className = 'message-origin';
        origin.textContent = `来源：${safeText(msg.session_name) || sid}`;
        origin.title = sid;
        footer.appendChild(origin);
    }
    if (isRecalled) {
        const recalled = document.createElement('span');
        recalled.className = 'msg-tag msg-tag-danger';
        recalled.textContent = '已撤回';
        footer.appendChild(recalled);
    }

    if (msg.message_truncated) {
        footer.appendChild(createFullMessageButton(msg));
    }

    const time = document.createElement('span');
    time.textContent = formatTime(msg.timestamp).split(' ')[1] || '';
    footer.appendChild(time);

    bubble.appendChild(text);
    bubble.appendChild(footer);
    return bubble;
}

let rawSessions = [];
let activePlatform = 'all';

const PLATFORM_META = {
    'all': { name: '全部' },
    'qq': { name: 'QQ' },
    'telegram': { name: 'Telegram' },
    'discord': { name: 'Discord' },
    'wechat': { name: '微信' },
    'wecom': { name: '企业微信' },
    'kook': { name: 'KOOK' },
    'teamspeak': { name: 'TeamSpeak' },
    'feishu': { name: '飞书' },
    'dingtalk': { name: '钉钉' }
};

const NON_QQ_PLATFORMS = ['telegram', 'discord', 'kook', 'feishu', 'dingtalk', 'wechat', 'wecom'];
const QQ_PLATFORM_ALIASES = ['qq', 'aiocqhttp', 'onebot', 'napcat', 'llonebot'];

function normalizePlatformName(platformName) {
    if (!platformName) return '';
    const plat = safeText(platformName).trim().toLowerCase();
    if (!plat) return '';
    if (QQ_PLATFORM_ALIASES.some(alias => plat === alias || plat.includes(alias))) {
        return 'qq';
    }
    if (NON_QQ_PLATFORMS.includes(plat)) {
        return plat;
    }
    return plat.replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32) || 'other';
}

function getSessionPlatform(s) {
    if (s.platform_name) {
        return normalizePlatformName(s.platform_name);
    }
    // Fallback: extract platform from session_id (e.g. aiocqhtp:GroupMessage:160572189)
    if (s.session_id && s.session_id.includes(':')) {
        const firstPart = s.session_id.split(':')[0];
        return normalizePlatformName(firstPart);
    }
    return '';
}

function renderPlatformFilter(sessions) {
    const bar = document.getElementById('platformFilterBar');
    if (!bar) return;

    // Dynamically discover all platforms present in sessions
    const platforms = new Set();
    sessions.forEach(s => {
        const plat = getSessionPlatform(s);
        if (plat) {
            platforms.add(plat);
        }
    });

    const orderedPlats = ['all'];
    const knownPlats = ['qq', 'telegram', 'discord'];
    knownPlats.forEach(p => {
        if (platforms.has(p)) {
            orderedPlats.push(p);
            platforms.delete(p);
        }
    });
    // Add any remaining dynamically discovered platforms for 100% future extensibility!
    platforms.forEach(p => {
        if (p) orderedPlats.push(p);
    });

    bar.innerHTML = orderedPlats.map(plat => {
        const badgeMeta = PLATFORM_BADGE_META[plat];
        const meta = PLATFORM_META[plat] || {
            name: plat.charAt(0).toUpperCase() + plat.slice(1)
        };
        const isActive = activePlatform === plat;
        const iconHtml = (badgeMeta && badgeMeta.svg) ? badgeMeta.svg : FALLBACK_PLATFORM_SVG;
        const displayNameText = safeText(meta.name || (badgeMeta && badgeMeta.name) || plat);
        const displayName = escapeHtmlText(displayNameText);

        return `
            <button type="button" class="platform-tab ${isActive ? 'active' : ''}" data-platform="${escapeAttr(plat)}"
                aria-pressed="${isActive}" aria-label="筛选平台：${escapeAttr(displayNameText)}">
                <span class="platform-icon" aria-hidden="true">${iconHtml}</span>
                <span class="platform-name">${displayName}</span>
            </button>
        `;
    }).join('');

    // Attach click handlers
    bar.querySelectorAll('.platform-tab').forEach(tab => {
        makeKeyboardActivatable(tab);
        tab.onclick = () => {
            activePlatform = tab.dataset.platform;
            renderPlatformFilter(sessions);
            renderSessionList(sessions);
        };
    });
    requestAnimationFrame(() => {
        const activeTab = bar.querySelector('.platform-tab.active');
        if (!activeTab) return;
        const centeredLeft = activeTab.offsetLeft - (bar.clientWidth - activeTab.offsetWidth) / 2;
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        bar.scrollTo({ left: Math.max(0, centeredLeft), behavior: reducedMotion ? 'auto' : 'smooth' });
    });

    // Horizontal mouse wheel scrolling for PC
    if (!bar.dataset.wheelAttached) {
        bar.addEventListener('wheel', (e) => {
            if (e.deltaY !== 0 && bar.scrollWidth > bar.clientWidth) {
                e.preventDefault();
                bar.scrollLeft += e.deltaY;
            }
        }, { passive: false });
        bar.dataset.wheelAttached = 'true';
    }
}

function ensureActiveMemberSubMenu(activeItem) {
    if (!activeItem || isFriendSessionType()) return null;
    const existing = activeItem.nextElementSibling;
    if (existing?.classList.contains('sidebar-sub-menu')) return existing;

    const subMenu = document.createElement('div');
    subMenu.className = 'sidebar-sub-menu';

    const searchBox = document.createElement('div');
    searchBox.className = 'sub-menu-search';
    const searchInput = document.createElement('input');
    searchInput.type = 'text';
    searchInput.id = 'memberSearch';
    searchInput.placeholder = '定位成员…';
    searchInput.setAttribute('aria-label', '筛选会话成员');
    searchInput.value = memberSearchKeyword;
    searchInput.addEventListener('click', (event) => event.stopPropagation());
    searchInput.oninput = (event) => debounceMemberSearch(event.target.value);
    searchBox.appendChild(searchInput);

    const userListContainer = document.createElement('div');
    userListContainer.id = 'userListContainer';
    userListContainer.setAttribute('aria-busy', 'true');
    const initialState = document.createElement('p');
    initialState.className = 'member-inline-state';
    initialState.setAttribute('role', 'status');
    initialState.textContent = '正在加载成员…';
    userListContainer.appendChild(initialState);

    subMenu.append(searchBox, userListContainer);
    activeItem.after(subMenu);
    openSidebarSubMenu(subMenu);
    return subMenu;
}

function renderSessionList(sessions) {
    const list = document.getElementById('sessionList');
    if (!list) return;
    list.setAttribute('aria-busy', 'false');
    list.innerHTML = '';
    const fragment = document.createDocumentFragment();

    const settingsActive = document.body.classList.contains('settings-view');
    const dashboardItem = document.createElement('div');
    dashboardItem.className = `session-item dashboard-nav ${!settingsActive && !activeSessionId ? 'active' : ''}`;
    makeKeyboardActivatable(dashboardItem);
    if (!settingsActive && !activeSessionId) dashboardItem.setAttribute('aria-current', 'page');
    dashboardItem.innerHTML = `
        <div class="dashboard-nav-icon" aria-hidden="true">${uiIcon('dashboard')}</div>
        <div class="session-info">
            <div class="session-name">归档总览</div>
            <div class="session-last">整体数据、趋势与最近消息</div>
        </div>`;
    dashboardItem.onclick = () => showDashboard();
    fragment.appendChild(dashboardItem);

    sessionsById.clear();
    const groups = {
        'group': { name: '群组会话', items: [] },
        'server': { name: '服务器', items: [] },
        'channel': { name: '频道消息', items: [] },
        'friend': { name: '个人私聊', items: [] },
        'legacy': { name: '历史归档', items: [] }
    };

    sessions.forEach(s => {
        if (s.name) {
            // Strip any legacy '👤 私聊: ' or '私聊: ' prefixes from the session name in UI
            s.name = s.name.replace(/^👤\s*私聊:\s*/, '').replace(/^私聊:\s*/, '');
        }
        // Cache all sessions regardless of filter so selectSession works
        sessionsById.set(s.session_id, { ...s });

        // Filter by platform
        if (activePlatform !== 'all' && s.session_id !== 'legacy:archive') {
            const sPlat = getSessionPlatform(s);
            if (sPlat !== activePlatform) return;
        }

        let category = 'legacy';
        const mt = (s.message_type || '').toLowerCase();
        const sPlat = getSessionPlatform(s);
        const isServerPlatform = ['discord', 'kook', 'teamspeak'].includes(sPlat);
        const isGroupLike = mt.includes('channel') || mt.includes('group');

        if (isServerPlatform && isGroupLike) {
            category = 'server';
        } else if (mt.includes('channel')) {
            category = 'channel';
        } else if (mt.includes('group')) {
            category = 'group';
        } else if (mt.includes('friend')) {
            category = 'friend';
        }

        if (s.name === s.session_id && s.session_id.includes(':')) {
            const parts = s.session_id.split(':');
            const id = parts[parts.length - 1];
            if (category === 'channel') {
                s.name = '频道: ' + id;
            } else if (category === 'group') {
                s.name = '群聊: ' + id;
            } else if (category === 'server') {
                const platName = sPlat.toUpperCase();
                s.name = platName + ' 服务器 / #' + id;
            } else {
                s.name = id;
            }
        }

        if (groups[category]) groups[category].items.push(s);
    });

    const visibleSessionCount = Object.values(groups)
        .reduce((total, group) => total + group.items.length, 0);
    if (visibleSessionCount === 0) {
        const empty = document.createElement('div');
        empty.className = 'sidebar-empty-state';
        empty.setAttribute('role', 'status');
        const message = document.createElement('p');
        message.textContent = activePlatform === 'all' ? '暂无已归档会话。' : '此平台暂无会话。';
        const action = document.createElement('button');
        action.type = 'button';
        action.className = 'secondary-btn';
        action.textContent = activePlatform === 'all' ? '刷新会话' : '显示全部平台';
        action.addEventListener('click', () => {
            if (activePlatform === 'all') {
                fetchSessions();
                return;
            }
            activePlatform = 'all';
            renderPlatformFilter(rawSessions);
            renderSessionList(rawSessions);
        });
        empty.append(message, action);
        fragment.appendChild(empty);
    }

    Object.keys(groups).forEach(catKey => {
        const groupData = groups[catKey];
        if (groupData.items.length === 0) return;

        let displayCount = groupData.items.length;
        let serverGroups = null;

        if (catKey === 'server') {
            // Helper to parse Discord/Kook/Teamspeak server and channel names
            const parseServerName = (name, platform) => {
                const defaultServerName = (platform === 'kook' ? 'KOOK 服务器' : (platform === 'teamspeak' ? 'TeamSpeak 服务器' : 'Discord 服务器'));
                if (!name) return { server: defaultServerName, channel: '未知频道' };
                if (name.includes(' / #')) {
                    const parts = name.split(' / #');
                    return { server: parts[0].trim(), channel: parts[1].trim() };
                }
                if (name.includes(' / ')) {
                    const parts = name.split(' / ');
                    return { server: parts[0].trim(), channel: parts[1].trim() };
                }
                return { server: defaultServerName, channel: name };
            };

            serverGroups = {};
            groupData.items.forEach(s => {
                const sPlat = getSessionPlatform(s);
                const parsed = parseServerName(s.name, sPlat);
                if (!serverGroups[parsed.server]) {
                    serverGroups[parsed.server] = {
                        platform: sPlat,
                        channels: []
                    };
                }
                serverGroups[parsed.server].channels.push({
                    session: s,
                    channelName: parsed.channel
                });
            });

            displayCount = Object.keys(serverGroups).length;
        }

        const header = document.createElement('div');
        header.className = 'category-header';
        makeKeyboardActivatable(header);
        header.setAttribute('aria-expanded', 'true');
        const label = document.createElement('span');
        label.textContent = `${groupData.name} `;
        const count = document.createElement('small');
        count.className = 'category-count';
        count.textContent = displayCount;
        label.appendChild(count);
        const toggle = document.createElement('span');
        toggle.className = 'toggle-icon';
        toggle.textContent = '▼';
        header.append(label, toggle);

        const content = document.createElement('div');
        content.className = 'category-content';
        content.id = `session-category-${catKey}`;
        content.setAttribute('aria-hidden', 'false');
        header.setAttribute('aria-controls', content.id);
        header.onclick = () => toggleCategory(header, content);

        if (catKey === 'server') {
            // Render Server Groups
            Object.keys(serverGroups).forEach((serverName, sIdx) => {
                const serverData = serverGroups[serverName];
                const channels = serverData.channels;
                const platform = serverData.platform;

                const serverGroup = document.createElement('div');
                serverGroup.className = 'discord-server-group';

                const hasActiveChannel = channels.some(c => activeSessionId === c.session.session_id);
                let isCollapsed = localStorage.getItem(`server_collapsed_${serverName}`) === 'true';
                if (hasActiveChannel) {
                    isCollapsed = false;
                }

                const platformIcon = PLATFORM_BADGE_META[platform]?.svg || FALLBACK_PLATFORM_SVG;

                // Get the server icon URL from the first channel in the server group
                const serverIconUrl = getMediaResourceUrl(
                    channels[0] && channels[0].session.avatar
                );
                let serverIconHtml = '';
                if (serverIconUrl && serverIconUrl.trim() !== '') {
                    serverIconHtml = `<img src="${escapeAttr(serverIconUrl)}" class="server-avatar-img" alt="" onerror="this.hidden=true; this.nextElementSibling.hidden=false;" /><span class="server-icon-fallback" aria-hidden="true" hidden>${platformIcon}</span>`;
                } else {
                    serverIconHtml = `<span class="server-icon-fallback" aria-hidden="true">${platformIcon}</span>`;
                }

                const serverHeader = document.createElement('div');
                serverHeader.className = `discord-server-header ${isCollapsed ? 'collapsed' : ''}`;
                makeKeyboardActivatable(serverHeader);
                serverHeader.setAttribute('aria-expanded', String(!isCollapsed));
                serverHeader.innerHTML = `
                    <span class="server-arrow">▼</span>
                    <span class="server-icon">${serverIconHtml}</span>
                    <span class="server-title"></span>
                    <span class="channel-count-badge">${channels.length}</span>
                `;
                serverHeader.querySelector('.server-title').textContent = serverName;

                const channelsList = document.createElement('div');
                channelsList.className = `discord-channels-list ${isCollapsed ? 'collapsed' : ''}`;
                channelsList.id = `server-channels-${catKey}-${sIdx}`;
                channelsList.toggleAttribute('inert', isCollapsed);
                channelsList.setAttribute('aria-hidden', String(isCollapsed));
                serverHeader.setAttribute('aria-controls', channelsList.id);

                serverHeader.onclick = (e) => {
                    e.stopPropagation();
                    const nowCollapsed = !channelsList.classList.contains('collapsed');
                    channelsList.toggleAttribute('inert', nowCollapsed);
                    channelsList.setAttribute('aria-hidden', String(nowCollapsed));
                    if (nowCollapsed) {
                        if (channelsList.contains(document.activeElement)) serverHeader.focus({ preventScroll: true });
                        channelsList.classList.add('collapsed');
                        serverHeader.classList.add('collapsed');
                        serverHeader.setAttribute('aria-expanded', 'false');
                        localStorage.setItem(`server_collapsed_${serverName}`, 'true');
                    } else {
                        channelsList.classList.remove('collapsed');
                        serverHeader.classList.remove('collapsed');
                        serverHeader.setAttribute('aria-expanded', 'true');
                        localStorage.setItem(`server_collapsed_${serverName}`, 'false');
                    }
                };

                channels.forEach((c, cIdx) => {
                    const s = c.session;
                    const item = document.createElement('div');
                    const isActive = !settingsActive && activeSessionId === s.session_id;
                    item.className = `session-item discord-channel-item session-enter ${isActive ? 'active' : ''}`;
                    makeKeyboardActivatable(item);
                    if (isActive) item.setAttribute('aria-current', 'page');
                    item.dataset.sessionId = s.session_id;
                    item.style.animationDelay = `${Math.min(cIdx, 8) * 0.025}s`;
                    item.onclick = (e) => {
                        e.stopPropagation();
                        selectSession(s.session_id, s.name, s.message_type);
                    };

                    const lastTime = safeCount(s.last_time);
                    const lastDate = lastTime ? new Date(lastTime * 1000).toLocaleDateString() : '';
                    const sPlat = getSessionPlatform(s);
                    const badgeHtml = getPlatformBadgeHtml(sPlat);

                    item.innerHTML = `
                        <div class="channel-hashtag">#</div>
                        <div class="session-info">
                            <div class="session-meta"><span>${escapeAttr(lastDate)}</span></div>
                            <div class="session-name"></div>
                            <div class="session-last"></div>
                        </div>
                        ${badgeHtml}`;
                    item.querySelector('.session-name').textContent = c.channelName;
                    item.querySelector('.session-last').textContent = formatSessionPreview(s.last_msg);
                    channelsList.appendChild(item);
                });

                serverGroup.appendChild(serverHeader);
                serverGroup.appendChild(channelsList);
                content.appendChild(serverGroup);
            });
        } else {
            // Render other flat items (QQ, Telegram, etc.)
            groupData.items.forEach((s, idx) => {
                const item = document.createElement('div');
                item.className = `session-item session-enter ${!settingsActive && activeSessionId === s.session_id ? 'active' : ''}`;
                makeKeyboardActivatable(item);
                if (!settingsActive && activeSessionId === s.session_id) item.setAttribute('aria-current', 'page');
                item.dataset.sessionId = s.session_id;
                item.style.animationDelay = `${Math.min(idx, 8) * 0.025}s`;
                item.onclick = (e) => {
                    e.stopPropagation();
                    selectSession(s.session_id, s.name, s.message_type);
                };

                const avatarUrl = escapeAttr(
                    getMediaResourceUrl(s.avatar) || getAvatarUrl('fallback')
                );
                const lastTime = safeCount(s.last_time);
                const lastDate = lastTime ? new Date(lastTime * 1000).toLocaleDateString() : '';
                const sPlat = getSessionPlatform(s);
                const badgeHtml = getPlatformBadgeHtml(sPlat);

                item.innerHTML = `
                    <img class="session-avatar" src="${avatarUrl}" alt="" loading="lazy" decoding="async" onerror="this.src=getAvatarUrl('fallback')" />
                    <div class="session-info">
                        <div class="session-meta"><span>${escapeAttr(lastDate)}</span></div>
                        <div class="session-name"></div>
                        <div class="session-last"></div>
                    </div>
                    ${badgeHtml}`;
                item.querySelector('.session-name').textContent = safeText(s.name);
                item.querySelector('.session-last').textContent = formatSessionPreview(s.last_msg);
                content.appendChild(item);
            });
        }

        fragment.appendChild(header);
        fragment.appendChild(content);
    });
    list.appendChild(fragment);

    const activeItem = document.querySelector(`.session-item[data-session-id="${escapeCssValue(activeSessionId)}"]`);
    if (!settingsActive && activeItem && ensureActiveMemberSubMenu(activeItem)) {
        renderUserList(sidebarMemberUsers);
    }
}

function showSessionListSkeleton() {
    const list = document.getElementById('sessionList');
    if (!list) return;
    list.setAttribute('aria-busy', 'true');
    const fragment = document.createDocumentFragment();
    for (let i = 0; i < 6; i++) {
        const row = document.createElement('div');
        row.className = 'session-item session-list-skeleton';
        row.setAttribute('aria-hidden', 'true');
        row.innerHTML = `
            <span class="session-avatar skeleton"></span>
            <span class="session-info">
                <span class="session-name skeleton"></span>
                <span class="session-last skeleton"></span>
            </span>`;
        fragment.appendChild(row);
    }
    list.replaceChildren(fragment);
}

async function fetchSessions(options = {}) {
    const requestSeq = ++sessionsRequestSeq;
    showSessionListSkeleton();
    try {
        const data = await fetchAPI('/api/sessions');
        if (requestSeq !== sessionsRequestSeq) return;
        if (data.success) {
            rawSessions = data.data;
            renderPlatformFilter(rawSessions);
            renderSessionList(rawSessions);

            if (options.selectView === false) return;
            const navigationUrl = window.location.href;
            const canSelect = () => window.location.href === navigationUrl && !activeSessionId && !document.getElementById('searchInput').value.trim();

            if (new URLSearchParams(window.location.search).get('view') === 'settings') {
                showSettings({ skipUrl: true });
                return;
            }
            const desiredSessionId = getDesiredSessionId();
            if (desiredSessionId) {
                const targetSession = data.data.find(s => s.session_id === desiredSessionId);
                if (targetSession) {
                    selectSession(targetSession.session_id, targetSession.name, targetSession.message_type, { replaceUrl: true });
                } else {
                    selectSession(desiredSessionId, desiredSessionId, '', { replaceUrl: true });
                }
            } else {
                showDashboard({ replaceUrl: true });
            }
        } else {
            throw new Error('Sessions request failed');
        }
    } catch (e) {
        if (requestSeq !== sessionsRequestSeq) return;
        console.error(e);
        const list = document.getElementById('sessionList');
        if (!list) return;
        list.setAttribute('aria-busy', 'false');
        const error = document.createElement('div');
        error.className = 'empty-state';
        error.setAttribute('role', 'alert');
        const message = document.createElement('p');
        message.textContent = '会话列表加载失败';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'primary-btn';
        retry.textContent = '重试';
        retry.addEventListener('click', fetchSessions);
        error.append(message, retry);
        list.replaceChildren(error);
    }
}

async function selectSession(sessionId, name, msgType, options = {}) {
    const wasSettingsActive = document.body.classList.contains('settings-view');
    document.body.classList.remove('settings-view');
    document.getElementById('settings-btn')?.removeAttribute('aria-current');
    if (!wasSettingsActive && activeSessionId === sessionId && activeUserId === '') {
        if (window.innerWidth <= SESSION_DRAWER_MAX_WIDTH) {
            closeAllPanels();
        }
        const activeItem = document.querySelector(`.session-item[data-session-id="${escapeCssValue(sessionId)}"]`);
        activeItem?.classList.add('active');
        if (activeItem && ensureActiveMemberSubMenu(activeItem)) {
            renderUserList(sidebarMemberUsers);
        }
        if (document.querySelector('#messageList [data-history-error="true"]')) {
            reloadStats();
            fetchHistory();
        }
        return;
    }

    dashboardRequestSeq += 1;
    statsRequestSeq += 1;
    cancelMemberSearch();
    if (window.innerWidth <= SESSION_DRAWER_MAX_WIDTH) {
        closeAllPanels();
    }
    activeMsgType = msgType || '';
    dashboardRequestSeq += 1;
    activeSessionId = sessionId;
    document.body.classList.remove('global-view');
    syncPanelAccessibility();
    activeSearchKeyword = '';
    const searchInput = document.getElementById('searchInput');
    if (searchInput) searchInput.value = '';
    syncSearchCancelControl();
    updateSessionUrl(sessionId, options.replaceUrl === true);
    memberRequestSeq += 1;
    rankRequestSeq += 1;
    memberOffset = 0;
    memberTotal = 0;
    memberHasMore = false;
    memberTotalExact = false;
    rankOffset = 0;
    rankTotal = 0;
    rankHasMore = false;
    rankTotalExact = false;
    sidebarMemberUsers = [];
    rankMemberUsers = [];
    window.globalTopUsers = [];
    memberSearchKeyword = '';
    const storedMeta = sessionsById.get(sessionId) || {};
    sessionsById.set(sessionId, { ...storedMeta, session_id: sessionId, name: name || storedMeta.name || sessionId, message_type: msgType || storedMeta.message_type || '' });
    updateActiveSessionHeader();

    document.querySelectorAll('.session-item').forEach(el => {
        const isActive = el.dataset.sessionId === sessionId;
        el.classList.toggle('active', isActive);
        if (isActive) el.setAttribute('aria-current', 'page');
        else el.removeAttribute('aria-current');
    });

    document.querySelectorAll('.sidebar-sub-menu').forEach(el => el.remove());
    if (activeUserId !== '') activeUserId = '';
    window.userMap = {};

    const activeItem = document.querySelector(`.session-item[data-session-id="${escapeCssValue(sessionId)}"]`);
    ensureActiveMemberSubMenu(activeItem);

    currentPage = 1;
    reloadStats();
    fetchHistory();
}

function formatMemberLoadMoreLabel(loaded, total, totalExact) {
    const loadedCount = safeCount(loaded).toLocaleString();
    if (!totalExact) return `加载更多 (已加载 ${loadedCount})`;
    return `加载更多 (${loadedCount}/${safeCount(total).toLocaleString()})`;
}

function renderUserList(users = sidebarMemberUsers) {
    const container = document.getElementById('userListContainer');
    if (!container) return;
    const subMenu = container.closest('.sidebar-sub-menu');
    const fragment = document.createDocumentFragment();

    users.forEach(u => {
        window.userMap[u.user_id] = u.sender_name;
        const subItem = document.createElement('div');
        subItem.className = `sub-menu-item ${activeUserId === u.user_id ? 'active' : ''}`;
        makeKeyboardActivatable(subItem);
        subItem.dataset.userId = safeText(u.user_id);
        subItem.dataset.domKey = JSON.stringify(['member', activeSessionId, u.user_id]);
        const wrap = document.createElement('span');
        wrap.className = 'member-row-main';
        const avatar = document.createElement('img');
        const avatarKey = `${safeText(u.platform_name)}:${safeText(u.user_id)}:${safeText(u.avatar_url)}`;
        avatar.src = avatarResolvedCache.get(avatarKey) || getAvatarUrl(u.user_id, u.avatar_url, u.platform_name);
        avatar.onerror = () => { avatar.src = getAvatarUrl('fallback'); };
        avatar.loading = 'lazy';
        avatar.decoding = 'async';
        avatar.className = 'member-mini-avatar';
        avatar.alt = '';
        const name = document.createElement('span');
        name.className = 'sub-name member-name';
        name.textContent = safeText(u.sender_name);
        wrap.append(avatar, name);
        const count = document.createElement('small');
        count.className = 'member-count';
        count.textContent = safeCount(u.count).toLocaleString();
        subItem.append(wrap, count);
        subItem.onclick = (e) => {
            e.stopPropagation();
            const previousUserId = activeUserId;
            if (activeUserId === u.user_id) {
                activeUserId = '';
                document.querySelectorAll('.sub-menu-item').forEach(el => el.classList.remove('active'));
            } else {
                activeUserId = u.user_id;
                document.querySelectorAll('.sub-menu-item').forEach(el => el.classList.remove('active'));
                subItem.classList.add('active');
            }
            if (window.innerWidth <= SESSION_DRAWER_MAX_WIDTH) {
                closeAllPanels();
            }
            if (previousUserId === activeUserId) return;
            reloadStats();
            fetchHistory();
        };
        fragment.appendChild(subItem);
    });

    if (!users.length) {
        const state = document.createElement('div');
        state.className = 'member-inline-state';
        const message = document.createElement('p');
        message.textContent = memberSearchKeyword
            ? `没有找到“${memberSearchKeyword}”对应的成员。`
            : '这个会话暂时没有可显示的成员。';
        const action = document.createElement('button');
        action.type = 'button';
        action.className = 'member-state-action';
        action.textContent = memberSearchKeyword ? '清除筛选' : '重新加载';
        action.addEventListener('click', event => {
            event.stopPropagation();
            if (memberSearchKeyword) {
                memberSearchKeyword = '';
                const input = document.getElementById('memberSearch');
                if (input) input.value = '';
            }
            fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: 0, append: false });
        });
        state.append(message, action);
        fragment.appendChild(state);
    }

    if (memberHasMore) {
        const more = document.createElement('button');
        more.type = 'button';
        more.className = 'member-load-more';
        more.textContent = formatMemberLoadMoreLabel(memberOffset, memberTotal, memberTotalExact);
        more.onclick = (event) => {
            event.stopPropagation();
            fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: memberOffset, append: true });
        };
        fragment.appendChild(more);
    }

    window.ArchiveDOM.updateChildren(container, fragment);
    container.setAttribute('aria-busy', 'false');
    openSidebarSubMenu(subMenu);
}

function renderMemberRequestError({ sidebar = false, rank = false } = {}) {
    if (sidebar) {
        const container = document.getElementById('userListContainer');
        if (container) {
            const state = document.createElement('div');
            state.className = 'member-inline-state is-error';
            state.setAttribute('role', 'alert');
            const message = document.createElement('p');
            message.textContent = '成员列表加载失败，请检查连接后重试。';
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'member-state-action';
            retry.textContent = '重试';
            retry.addEventListener('click', event => {
                event.stopPropagation();
                fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: 0, append: false });
            });
            state.append(message, retry);
            container.replaceChildren(state);
            container.setAttribute('aria-busy', 'false');
            openSidebarSubMenu(container.closest('.sidebar-sub-menu'));
        }
    }
    if (rank) {
        const list = document.getElementById('rankList');
        if (list) {
            const state = document.createElement('div');
            state.className = 'analysis-inline-state';
            state.setAttribute('role', 'alert');
            const message = document.createElement('p');
            message.textContent = '成员排行加载失败，请稍后重试。';
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'member-state-action';
            retry.textContent = '重试';
            retry.addEventListener('click', () => {
                fetchMembers({ target: 'rank', keyword: '', offset: 0, append: false, limit: getInitialMemberLimit() });
            });
            state.append(message, retry);
            list.replaceChildren(state);
            list.setAttribute('aria-busy', 'false');
        }
    }
}

async function fetchMembers({ target = 'both', keyword = '', offset = 0, append = false, limit = null } = {}) {
    if (!activeSessionId || isFriendSessionType()) return;
    const requestState = {
        sessionId: activeSessionId,
        timeStart: filterStart,
        timeEnd: filterEnd,
    };
    const updateSidebar = target === 'sidebar' || target === 'both';
    const updateRank = target === 'rank' || target === 'both';
    const sidebarSeq = updateSidebar ? ++memberRequestSeq : memberRequestSeq;
    const rankSeq = updateRank ? ++rankRequestSeq : rankRequestSeq;
    const requestKeyword = updateRank && !updateSidebar ? '' : safeText(keyword).trim();
    const defaultLimit = !append && offset === 0 ? getInitialMemberLimit() : memberPageSize;
    const fetchLimit = Math.max(1, Math.min(100, safeCount(limit) || defaultLimit));
    let url = `/api/members?session_id=${encodeURIComponent(requestState.sessionId)}&limit=${fetchLimit}&offset=${offset}`;
    if (requestKeyword) url += `&keyword=${encodeURIComponent(requestKeyword)}`;
    if (requestState.timeStart) url += `&time_start=${requestState.timeStart}`;
    if (requestState.timeEnd) url += `&time_end=${requestState.timeEnd}`;

    if (updateSidebar) document.getElementById('userListContainer')?.setAttribute('aria-busy', 'true');
    if (updateRank) document.getElementById('rankList')?.setAttribute('aria-busy', 'true');

    try {
        const res = await fetchAPI(url);
        if (!res.success) throw new Error('Members request failed');
        const payload = res.data || {};
        const members = payload.members || [];
        preloadUserAvatars(members);
        if (
            activeSessionId !== requestState.sessionId
            || filterStart !== requestState.timeStart
            || filterEnd !== requestState.timeEnd
        ) return;

        if (updateSidebar && sidebarSeq === memberRequestSeq) {
            sidebarMemberUsers = append ? sidebarMemberUsers.concat(members) : members;
            window.globalTopUsers = sidebarMemberUsers;
            memberOffset = offset + members.length;
            memberTotal = safeCount(payload.total);
            memberHasMore = !!payload.has_more;
            memberTotalExact = !!payload.total_exact;
            renderUserList(sidebarMemberUsers);
        }

        if (updateRank && rankSeq === rankRequestSeq) {
            if (!append) memberAutoFillCount = 0;
            rankMemberUsers = append ? rankMemberUsers.concat(members) : members;
            rankOffset = offset + members.length;
            rankTotal = safeCount(payload.total);
            rankHasMore = !!payload.has_more;
            rankTotalExact = !!payload.total_exact;
            renderAnalysisMemberList(rankMemberUsers, rankHasMore, rankTotal);
        }
    } catch (e) {
        console.error(e);
        const sidebarIsCurrent = updateSidebar && sidebarSeq === memberRequestSeq;
        const rankIsCurrent = updateRank && rankSeq === rankRequestSeq;
        if (!sidebarIsCurrent && !rankIsCurrent) return;
        if (append) {
            if (e.message === 'Members request failed') {
                showClipboardToast('更多成员加载失败，请重试', true);
            }
            return;
        }
        renderMemberRequestError({
            sidebar: sidebarIsCurrent,
            rank: rankIsCurrent,
        });
    } finally {
        if (updateSidebar && sidebarSeq === memberRequestSeq) {
            document.getElementById('userListContainer')?.setAttribute('aria-busy', 'false');
        }
        if (updateRank && rankSeq === rankRequestSeq) {
            document.getElementById('rankList')?.setAttribute('aria-busy', 'false');
        }
    }
}

function refreshMembersForActiveSession() {
    const limit = getInitialMemberLimit();
    if (memberSearchKeyword) {
        fetchMembers({ target: 'rank', keyword: '', offset: 0, append: false, limit });
        fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: 0, append: false, limit: memberPageSize });
    } else {
        fetchMembers({ target: 'both', keyword: '', offset: 0, append: false, limit });
    }
}

function scheduleAnalysisMemberAutofill() {
    if (memberAutoFillPending || memberAutoFillCount >= memberAutoFillMaxRequests) return;
    memberAutoFillPending = true;
    requestAnimationFrame(() => {
        memberAutoFillPending = false;
        autofillAnalysisMembers();
    });
}

function autofillAnalysisMembers() {
    if (!rankHasMore || activeUserId || isFriendSessionType()) return;

    const content = document.getElementById('analysisContent');
    const list = document.getElementById('rankList');
    const more = document.getElementById('rankLoadMore');
    const firstItem = list?.querySelector('.rank-item');
    if (!content || !list || !firstItem) return;

    const contentRect = content.getBoundingClientRect();
    const lastVisible = more && more.style.display !== 'none' ? more : list;
    const lastRect = lastVisible.getBoundingClientRect();
    const contentStyle = window.getComputedStyle(content);
    const bottomPadding = parseFloat(contentStyle.paddingBottom) || 0;
    const remainingSpace = contentRect.bottom - lastRect.bottom - bottomPadding;
    if (remainingSpace <= 12) return;

    const listStyle = window.getComputedStyle(list);
    const gap = parseFloat(listStyle.rowGap || listStyle.gap) || 0;
    const rowHeight = Math.max(1, firstItem.getBoundingClientRect().height + gap);
    const needed = Math.min(memberPageSize, Math.max(1, Math.ceil((remainingSpace + gap) / rowHeight)));

    memberAutoFillCount += 1;
    fetchMembers({
        target: 'rank',
        keyword: '',
        offset: rankOffset,
        append: true,
        limit: needed,
    });
}

function openSidebarSubMenu(subMenu) {
    if (!subMenu) return;
    const targetHeight = Math.min(subMenu.scrollHeight, 300);
    subMenu.style.setProperty('--submenu-height', `${targetHeight}px`);

    if (!subMenu.classList.contains('open')) {
        requestAnimationFrame(() => {
            subMenu.classList.add('open');
            const finishOpen = (event) => {
                if (event.target !== subMenu || event.propertyName !== 'max-height') return;
                subMenu.classList.toggle('scrollable', subMenu.scrollHeight > 300);
                subMenu.removeEventListener('transitionend', finishOpen);
            };
            subMenu.addEventListener('transitionend', finishOpen);
        });
    } else {
        subMenu.classList.toggle('scrollable', subMenu.scrollHeight > 300);
    }
}

function attachRankItemHandlers(root = document) {
    root.querySelectorAll('.rank-item').forEach(item => {
        makeKeyboardActivatable(item);
        item.onclick = () => {
            const nextUserId = item.getAttribute('data-user-id');
            if (!nextUserId) return;
            const previousUserId = activeUserId;
            activeUserId = previousUserId === nextUserId ? '' : nextUserId;

            document.querySelectorAll('.sub-menu-item').forEach(el => {
                if (activeUserId && el.dataset.userId === activeUserId) el.classList.add('active');
                else el.classList.remove('active');
            });
            document.querySelectorAll('.rank-item').forEach(el => {
                el.classList.toggle('active', Boolean(activeUserId) && el.dataset.userId === activeUserId);
            });
            if (window.innerWidth <= ANALYSIS_DRAWER_MAX_WIDTH) closeAllPanels();
            if (previousUserId === activeUserId) return;
            reloadStats();
            fetchHistory();
        };
    });
}

function renderMemberRankItems(users) {
    return users.map((u, idx) => {
        const rank = idx + 1;
        const div = document.createElement('div');
        div.innerText = u.sender_name;
        const safeName = div.innerHTML;

        const rankDisp = String(rank).padStart(2, '0');
        const avatarKey = `${safeText(u.platform_name)}:${safeText(u.user_id)}:${safeText(u.avatar_url)}`;
        const avatarUrl = avatarResolvedCache.get(avatarKey) || getAvatarUrl(u.user_id, u.avatar_url, u.platform_name);

        return `
            <div class="rank-item" data-dom-key="${escapeAttr(JSON.stringify(['rank', activeSessionId, u.user_id]))}" data-rank="${rank}" data-user-id="${escapeAttr(u.user_id)}" data-user-name="${escapeAttr(u.sender_name)}" data-od-id="member-rank-${rank}" aria-label="第 ${rank} 名，${escapeAttr(u.sender_name)}，${safeCount(u.count).toLocaleString()} 条消息">
                <div class="rank-number">${rankDisp}</div>
                <img src="${escapeAttr(avatarUrl)}" class="rank-avatar" alt="" loading="lazy" decoding="async" onerror="this.src=getAvatarUrl('fallback')" />
                <div class="rank-info">
                    <div class="rank-name">${safeName}</div>
                    <div class="rank-count">${safeCount(u.count).toLocaleString()} 条消息</div>
                </div>
            </div>
        `;
    }).join('');
}

function renderAnalysisMemberList(users, hasMore, total) {
    const list = document.getElementById('rankList');
    const more = document.getElementById('rankLoadMore');
    if (!list) return;
    window.ArchiveDOM.updateChildren(list, renderMemberRankItems(users));
    attachRankItemHandlers(list);
    if (more) {
        more.style.display = hasMore ? 'block' : 'none';
        more.textContent = formatMemberLoadMoreLabel(rankOffset, total, rankTotalExact);
        more.onclick = () => fetchMembers({ target: 'rank', keyword: '', offset: rankOffset, append: true });
    }
    scheduleAnalysisMemberAutofill();
}

async function reloadStats() {
    if (!activeSessionId) return;
    const requestSeq = ++statsRequestSeq;
    const analysisContent = document.getElementById('analysisContent');
    analysisContent?.setAttribute('aria-busy', 'true');
    if (analysisContent && analysisContent.children.length === 0) {
        const loading = document.createElement('div');
        loading.className = 'analysis-loading-state';
        loading.setAttribute('role', 'status');
        loading.textContent = '正在加载会话统计…';
        analysisContent.replaceChildren(loading);
    }
    const requestState = {
        sessionId: activeSessionId,
        userId: activeUserId,
        timeStart: filterStart,
        timeEnd: filterEnd,
    };
    const isCurrentRequest = () => (
        requestSeq === statsRequestSeq
        && activeSessionId === requestState.sessionId
        && activeUserId === requestState.userId
        && filterStart === requestState.timeStart
        && filterEnd === requestState.timeEnd
    );
    const slowStatsTimer = setTimeout(() => {
        if (!isCurrentRequest()) return;
        const loading = analysisContent?.querySelector('.analysis-loading-state');
        if (loading) loading.textContent = '统计加载时间较长，请继续等待…';
    }, 15000);

    try {
        let qs = `/api/stats?session_id=${encodeURIComponent(requestState.sessionId)}`;
        if (requestState.userId) qs += `&user_id=${encodeURIComponent(requestState.userId)}`;
        if (requestState.timeStart) qs += `&time_start=${requestState.timeStart}`;
        if (requestState.timeEnd) qs += `&time_end=${requestState.timeEnd}`;

        const res = await fetchAPI(qs);
        if (!isCurrentRequest()) return;
        if (res.success) {
            updateAnalysisPanel(res.data);
            if (!requestState.userId && res.data?.top_users) {
                refreshMembersForActiveSession();
            }
        } else {
            throw new Error('Stats request failed');
        }
    } catch (e) {
        if (isCurrentRequest()) {
            updateAnalysisPanel(null);
            if (analysisContent) {
                const error = document.createElement('div');
                error.className = 'analysis-empty-state';
                error.setAttribute('role', 'alert');
                const message = document.createElement('p');
                message.textContent = '统计加载失败，请稍后重试。';
                const retry = document.createElement('button');
                retry.type = 'button';
                retry.className = 'primary-btn';
                retry.textContent = '重新加载';
                retry.addEventListener('click', reloadStats);
                error.append(message, retry);
                analysisContent.replaceChildren(error);
            }
        }
    } finally {
        clearTimeout(slowStatsTimer);
        if (isCurrentRequest()) analysisContent?.setAttribute('aria-busy', 'false');
    }
}

function renderBarChartUI(distribution) {
    if (!distribution || distribution.length !== 12) {
        return '<div class="analysis-inline-state">暂无可用的时段数据。</div>';
    }
    const values = distribution.map(safeCount);
    let maxCount = Math.max(...values, 1);
    let html = `<div class="bar-chart-container" role="list" aria-label="每两小时消息活跃度">`;
    for (let i = 0; i < 12; i++) {
        const value = values[i];
        let h = maxCount > 0 ? (value / maxCount) * 100 : 0;
        let timeLabel = `${i * 2}:00 - ${i * 2 + 2}:00`;
        html += `
            <div class="bar-wrapper" role="listitem" tabindex="0" aria-label="${timeLabel}，${value.toLocaleString()} 条消息">
                <div class="bar" style="height: ${h}%;"></div>
                <div class="bar-tooltip">${timeLabel}<br/>${value.toLocaleString()}条</div>
                <div class="bar-label">${i * 2}</div>
            </div>
        `;
    }
    html += `</div>`;

    return html;
}

function updateAnalysisPanel(data) {
    const panel = document.getElementById('analysisPanel');
    const content = document.getElementById('analysisContent');
    if (!panel || !content) return;
    if (!data) {
        content.replaceChildren();
        content.setAttribute('aria-busy', 'false');
        panel.style.removeProperty('display');
        return;
    }

    panel.style.display = 'flex';
    let isIndividual = !!data.message_types;

    let html = '';
    if (isIndividual) {
        let activeDays = safeCount(data.active_days);
        let textLen = safeCount(data.avg_text_length);

        let peakTime = "无";
        let peakVal = 0;
        if (data.time_distribution) {
            for (let i = 0; i < 12; i++) {
                if (data.time_distribution[i] > peakVal) {
                    peakVal = data.time_distribution[i];
                    peakTime = `${i * 2}:00`;
                }
            }
        }

        html += `
            <div class="analysis-stat-grid analysis-stat-grid-four" data-od-id="member-stat-summary">
                <div class="stat-card" title="这段时间内该成员发出的消息总条数" data-od-id="member-stat-messages">
                    <div class="value">${safeCount(data.total_messages).toLocaleString()}</div>
                    <div class="label">发言数</div>
                </div>
                <div class="stat-card" title="排除媒体与 CQ 代码后，纯文本消息的平均字符数" data-od-id="member-stat-average-length">
                    <div class="value">${textLen.toLocaleString()} 字</div>
                    <div class="label">平均文本长度</div>
                </div>
                <div class="stat-card" title="有发言记录的日期数量" data-od-id="member-stat-active-days">
                    <div class="value">${activeDays.toLocaleString()} 天</div>
                    <div class="label">活跃天数</div>
                </div>
                <div class="stat-card" title="一天中发言最集中的两小时时段" data-od-id="member-stat-peak-time">
                    <div class="value">${escapeAttr(peakTime)}</div>
                    <div class="label">高频时段</div>
                </div>
            </div>
        `;
    } else {
        html += `
            <div class="analysis-stat-grid" data-od-id="session-stat-summary">
                <div class="stat-card" data-od-id="session-stat-total">
                    <div class="value">${safeCount(data.total_messages).toLocaleString()}</div>
                    <div class="label">所选时段消息</div>
                </div>
                <div class="stat-card" title="当前会话今日的消息总数" data-od-id="session-stat-today">
                    <div class="value">${safeCount(data.today_messages).toLocaleString()}</div>
                    <div class="label">今日消息</div>
                </div>
            </div>
        `;
    }

    html += `
        <div class="analysis-section" data-od-id="activity-by-time">
            <div class="section-title">${uiIcon('distribution', 'section-title-icon')} 活跃时段分布</div>
            ${renderBarChartUI(data.time_distribution)}
        </div>
    `;

    if (isIndividual) {
        const messageTypes = Array.isArray(data.message_types) ? data.message_types.filter(t => safeCount(t.value) > 0) : [];
        html += `<div class="analysis-section" data-od-id="member-message-types"><div class="section-title">${uiIcon('file', 'section-title-icon')} 消息形式分析</div><div class="type-list">`;
        messageTypes.forEach(t => {
            if (safeCount(t.value) > 0) {
                html += `
                    <div class="type-item">
                        <div class="type-name"><span>${escapeAttr(t.name)}</span></div>
                        <div class="type-value">${safeCount(t.value).toLocaleString()}</div>
                    </div>
                `;
            }
        });
        if (!messageTypes.length) html += '<div class="analysis-inline-state">所选时段暂无消息类型数据。</div>';
        html += `</div></div>`;
    } else {
        const topUsers = Array.isArray(data.top_users) ? data.top_users : [];
        const rankItems = topUsers.length
            ? renderMemberRankItems(topUsers.slice(0, getInitialMemberLimit()))
            : '<div class="analysis-inline-state">所选时段没有活跃成员。</div>';
        html += `
            <div class="analysis-section" data-od-id="active-member-ranking">
                <div class="section-title">${uiIcon('users', 'section-title-icon')} 活跃成员排行</div>
                <div class="rank-list" id="rankList">${rankItems}</div>
                <button type="button" class="member-load-more" id="rankLoadMore" style="display:none;">加载更多</button>
            </div>
        `;
    }

    const viewKey = JSON.stringify([activeSessionId, activeUserId, filterStart, filterEnd]);
    const preserveRank = content.dataset.statsView === viewKey && !!content.querySelector('#rankList');
    window.ArchiveDOM.updateChildren(content, html, {
        // Keep loaded member pages until the member response updates them.
        preserveIds: preserveRank ? ['rankList', 'rankLoadMore'] : [],
    });
    content.dataset.statsView = viewKey;
    content.setAttribute('aria-busy', 'false');

    if (!isIndividual && data.top_users && data.top_users.length > 0) {
        attachRankItemHandlers();
    }
}

// Keep the archive renderer independent from the virtualizer implementation.
let virtualMessages = [];
let virtualRows = [];
let timeline = null;
let historyHasMore = false;
let historyLoadFailed = false;

function scrollListToBottom(el) {
    if (timeline && el === timeline.viewport) timeline.scrollToEnd();
    else if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'auto' });
}

function getMessageStableKey(msg, fallback = 0) {
    const id = msg.id ?? msg.msg_id ?? msg.message_id ?? '';
    if (id !== '') return `${safeText(msg.session_id)}:${safeText(id)}`;
    return `${safeText(msg.session_id)}:${safeText(msg.user_id)}:${safeCount(msg.timestamp)}:${fallback}`;
}

function resetVirtualMessages() {
    clearTimeout(highlightedMessageTimer);
    highlightedMessageTimer = null;
    highlightedMessageId = '';
    fullMessageRequests.forEach(entry => entry.controller?.abort());
    fullMessageRequests.clear();
    fullMessageFormattedCache.clear();
    timeline?.destroy();
    timeline = null;
    virtualMessages = [];
    virtualRows = [];
    historyHasMore = false;
    historyLoadFailed = false;
    historyStartObserver.disconnect();
}

function deactivateVirtualHistoryView(list, { resetScroll = false } = {}) {
    resetVirtualMessages();
    if (!list) return;
    list.querySelectorAll('.message-group, .msg-system-center, .date-divider, .empty-state, .skeleton-group, .loading-label, .slow-request-note, .history-refresh-error').forEach(el => el.remove());
    if (resetScroll) list.scrollTop = 0;
}

function buildVirtualRows(messages) {
    const rows = [];
    let previous = null;
    let date = '';
    messages.forEach((msg, index) => {
        const day = getDateStr(msg.timestamp);
        const key = getMessageStableKey(msg, index);
        if (day !== date) {
            rows.push({ type: 'date', key: `date:${day}:${key}`, dateStr: day });
            date = day;
            previous = null;
        }
        if (String(msg.user_id) === '0') {
            rows.push({ type: 'system', key: `system:${key}`, msg });
            previous = null;
            return;
        }
        rows.push({
            type: 'group', key: `group:${key}`, userId: msg.user_id,
            sessionId: msg.session_id, isRight: !!msg.is_right,
            first: msg, messages: [msg],
            continuation: !!previous && previous.user_id === msg.user_id
                && previous.session_id === msg.session_id
                && !!previous.is_right === !!msg.is_right
                && msg.timestamp - previous.timestamp >= 0
                && msg.timestamp - previous.timestamp <= 300,
        });
        previous = msg;
    });
    return rows;
}

function prependVirtualRows(messages, existingRows, existingMessages) {
    if (!messages.length) return existingRows.slice();
    if (!existingRows.length || !existingMessages.length) return buildVirtualRows(messages);
    const prefix = buildVirtualRows(messages);
    const suffix = existingRows.slice();
    if (getDateStr(messages[messages.length - 1].timestamp) === getDateStr(existingMessages[0].timestamp)
        && suffix[0]?.type === 'date') suffix.shift();
    return prefix.concat(suffix);
}

function renderVirtualRow(row, animate = false) {
    if (row.type === 'date') {
        const divider = document.createElement('div');
        divider.className = `date-divider${animate ? ' animate-fade' : ''}`;
        divider.dataset.vkey = row.key;
        const dateLabel = document.createElement('span');
        dateLabel.textContent = row.dateStr;
        divider.appendChild(dateLabel);
        return divider;
    }

    if (row.type === 'system') {
        const systemMsg = document.createElement('div');
        systemMsg.className = `msg-system-center${animate ? ' animate-fade' : ''}`;
        systemMsg.dataset.vkey = row.key;
        const span = document.createElement('span');
        if (shouldUseSearchCodePreview(row.msg)) {
            appendSearchCodePreview(span, row.msg);
        } else {
            span.innerHTML = formatMsgCached(row.msg);
            enhanceRenderedMessageContent(span);
        }
        systemMsg.appendChild(span);
        if (row.msg.message_truncated) {
            systemMsg.appendChild(createFullMessageButton(row.msg));
        }
        return systemMsg;
    }

    const msg = row.first;
    const group = document.createElement('div');
    group.className = `message-group${animate ? ' animate-fade' : ''}${row.isRight ? ' msg-right' : ''}`;
    group.dataset.vkey = row.key;
    group.classList.toggle('message-continuation', !!row.continuation);
    group.innerHTML = `
        <div class="avatar-col">
            <img class="msg-author-avatar" src="${escapeAttr(getAvatarUrl(msg.user_id, msg.avatar_url, msg.platform_name))}" width="36" height="36" loading="lazy" decoding="async" onerror="this.src=getAvatarUrl('fallback')" />
        </div>
        <div class="content-col">
            <div class="msg-author">
                <span class="author-name"></span>
                <span class="author-id"></span>
            </div>
            <div class="msg-bubble-list"></div>
        </div>
    `;
    group.querySelector('.author-name').textContent = safeText(msg.sender_name);
    group.querySelector('.author-id').textContent = `#${safeText(msg.user_id)}`;

    if (!activeSessionId) {
        const displaySessionName = msg.session_name || msg.session_id || '未知会话';
        const sessionEl = document.createElement('span');
        sessionEl.className = 'author-session';
        sessionEl.title = '点击进入会话';
        makeKeyboardActivatable(sessionEl);

        const sPlat = normalizePlatformName(msg.platform_name);
        const badgeMeta = PLATFORM_BADGE_META[sPlat];
        const logoSpan = document.createElement('span');
        logoSpan.className = `author-session-platform ${badgeMeta ? badgeMeta.class : ''}`;
        logoSpan.innerHTML = badgeMeta ? badgeMeta.svg : FALLBACK_PLATFORM_SVG;

        const textSpan = document.createElement('span');
        textSpan.textContent = `@ ${displaySessionName}`;

        sessionEl.appendChild(logoSpan);
        sessionEl.appendChild(textSpan);
        sessionEl.onclick = (e) => {
            e.stopPropagation();
            document.getElementById('searchInput').value = '';
            selectSession(msg.session_id, msg.session_name || msg.session_id, msg.message_type || '');
        };
        group.querySelector('.msg-author').appendChild(sessionEl);
    }

    const bubbleList = group.querySelector('.msg-bubble-list');
    row.messages.forEach(m => bubbleList.appendChild(createMessageBubble(m, animate)));
    return group;
}


function setVirtualMessages(messages, { appendOlder = false } = {}) {
    const list = document.getElementById('messageList');
    const initial = !timeline;
    if (appendOlder) {
        const existing = new Set(virtualMessages.map(getMessageStableKey));
        messages = messages.filter((msg, index) => !existing.has(getMessageStableKey(msg, index)));
        virtualRows = prependVirtualRows(messages, virtualRows, virtualMessages);
        virtualMessages = messages.concat(virtualMessages);
    } else {
        const oldByKey = new Map(virtualMessages.map((msg, index) => [getMessageStableKey(msg, index), msg]));
        for (const [index, msg] of messages.entries()) {
            const key = getMessageStableKey(msg, index);
            if (JSON.stringify(oldByKey.get(key)) !== JSON.stringify(msg)) {
                timeline?.nodes.delete(`group:${key}`);
                timeline?.nodes.delete(`system:${key}`);
            }
        }
        virtualMessages = messages.slice();
        virtualRows = buildVirtualRows(virtualMessages);
    }
    if (!timeline) {
        timeline = new window.ArchiveTimeline(list, renderVirtualRow, { search: !!getActiveSearchKeyword() });
        const start = document.getElementById('loadMoreWrap');
        if (start) historyStartObserver.observe(start);
    }
    timeline.setRows(virtualRows, { initial, search: !!getActiveSearchKeyword() });
}

function clearVirtualDom(list) {
    deactivateVirtualHistoryView(list);
}

function getHistoryViewKey(keyword = '') {
    return [activeSessionId, activeUserId, safeText(keyword).trim(), filterStart || 0, filterEnd || 0].join('\u001f');
}

function resetHistoryViewFilters() {
    activeUserId = '';
    filterStart = 0;
    filterEnd = 0;
    document.getElementById('timeStart').value = '';
    document.getElementById('timeEnd').value = '';
    document.querySelectorAll('.time-btn').forEach(button => {
        const active = button.dataset.range === 'all';
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('.rank-item, .sub-menu-item').forEach(item => item.classList.remove('active'));
    currentPage = 1;
    if (activeSessionId) {
        reloadStats();
        fetchHistory();
    }
}

function renderHistoryEmptyState(list, keyword) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.setAttribute('role', 'status');
    const title = document.createElement('h2');
    const copy = document.createElement('p');
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'secondary-btn empty-state-action';
    if (keyword) {
        title.textContent = `没有找到“${keyword}”`;
        copy.textContent = '请检查关键词，或清除搜索后继续浏览当前归档。';
        action.textContent = '清除搜索';
        action.addEventListener('click', cancelSearch);
    } else {
        title.textContent = '此范围内没有记录';
        copy.textContent = '当前成员或时间范围没有归档消息，可返回全部记录。';
        action.textContent = '返回全部记录';
        action.addEventListener('click', resetHistoryViewFilters);
    }
    empty.append(title, copy, action);
    list.appendChild(empty);
}

function renderHistoryErrorState(list) {
    const error = document.createElement('div');
    error.className = 'empty-state';
    error.dataset.historyError = 'true';
    error.setAttribute('role', 'alert');
    const title = document.createElement('h2');
    title.textContent = '记录加载失败';
    const copy = document.createElement('p');
    copy.textContent = '无法连接归档服务。搜索词和筛选条件已保留，请检查连接后重试。';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'primary-btn empty-state-action';
    retry.textContent = '重新加载';
    retry.addEventListener('click', () => fetchHistory());
    error.append(title, copy, retry);
    list.appendChild(error);
}

async function fetchHistory(append = false) {
    const keyword = getActiveSearchKeyword();
    if (!activeSessionId && !keyword) return;
    if (isHistoryLoading && append) return;
    if (!append && !activeSessionId) {
        const dashboardNav = document.querySelector('.dashboard-nav');
        const dashboardIsCurrent = !keyword;
        dashboardNav?.classList.toggle('active', dashboardIsCurrent);
        if (dashboardIsCurrent) dashboardNav?.setAttribute('aria-current', 'page');
        else dashboardNav?.removeAttribute('aria-current');
    }
    if (!append && historyAbortController) historyAbortController.abort();
    const requestController = new AbortController();
    historyAbortController = requestController;
    const requestSeq = ++historyRequestSeq;
    const historyViewKey = getHistoryViewKey(keyword);
    const shouldResetScroll = !append && historyViewKey !== activeHistoryViewKey;
    const retainTimeline = !append && !shouldResetScroll && !!timeline;
    isHistoryLoading = true;
    historyLoadFailed = false;
    let historyLoaded = false;
    syncSearchCancelControl();
    syncHistoryLoadControl();

    updateActiveSessionHeader();

    const list = document.getElementById('messageList');
    list.setAttribute('aria-busy', 'true');
    if (!append) {
        dashboardRequestSeq += 1;
        currentPage = 1;
        nextCursor = 0;
        // Remove the previous page before rendering loading, empty or error states.
        list.querySelectorAll('.dashboard-view, .settings-page').forEach(el => el.remove());
        // 确保 loadMoreWrap 存在（dashboard 的 innerHTML 可能已销毁它）
        if (!document.getElementById('loadMoreWrap')) {
            const wrap = document.createElement('div');
            wrap.id = 'loadMoreWrap';
            wrap.className = 'load-more-wrap';
            wrap.dataset.odId = 'load-older-wrap';
            const btn = document.createElement('button');
            btn.className = 'secondary-btn load-more-btn';
            btn.id = 'loadMoreBtn';
            btn.hidden = true;
            btn.type = 'button';
            btn.tabIndex = 0;
            btn.dataset.odId = 'load-older-button';
            btn.innerHTML = `${uiIcon('arrowUp', 'load-more-icon')}<span>加载更早的记录</span>`;
            btn.onclick = handleLoadMore;
            wrap.appendChild(btn);
            list.prepend(wrap);
        }
        if (!retainTimeline) {
            clearVirtualDom(list);
            list.scrollTop = 0;
            showSkeleton('messageList', 4);
            if (scrollBtn) scrollBtn.style.display = 'none';
        }
        list.querySelectorAll('.history-refresh-error').forEach(el => el.remove());
    }
    const loadMoreBtn = document.getElementById('loadMoreBtn');
    if (append && loadMoreBtn) loadMoreBtn.disabled = true;
    const slowRequestTimer = !append && !retainTimeline ? setTimeout(() => {
        if (requestSeq !== historyRequestSeq || !isHistoryLoading) return;
        const note = document.createElement('p');
        note.className = 'slow-request-note';
        note.setAttribute('role', 'status');
        note.textContent = keyword
            ? '搜索耗时比预期更长，你可以继续等待或取消搜索。'
            : '记录加载耗时比预期更长，请继续等待或稍后重试。';
        list.appendChild(note);
    }, 15000) : null;

    try {
        let url = `/api/history?session_id=${encodeURIComponent(activeSessionId)}&user_id=${encodeURIComponent(activeUserId)}&keyword=${encodeURIComponent(keyword)}&page=${currentPage}&limit=${limit}`;
        if (keyword) url += '&search_mode=terms';
        if (append && nextCursor > 0) url += `&cursor=${nextCursor}`;
        if (filterStart) url += `&time_start=${filterStart}`;
        if (filterEnd) url += `&time_end=${filterEnd}`;

        const data = await fetchAPI(url, 'GET', null, { signal: requestController.signal });
        if (requestSeq !== historyRequestSeq) return;

        if (data.success) {
            if (!append && keyword) {
                const status = document.getElementById('app-status');
                if (status) status.textContent = `搜索完成，当前载入 ${safeCount(data.data?.length)} 条结果`;
            }
            if (data.next_cursor !== undefined) nextCursor = data.next_cursor;
            if (data.user_profiles) {
                for (const [uid, profile] of Object.entries(data.user_profiles)) {
                    if (profile && profile.sender_name) {
                        window.userMap[uid] = profile.sender_name;
                    }
                }
            }
            if (!append && !retainTimeline) {
                clearVirtualDom(list);
            } else {
                list.querySelectorAll('.skeleton-group, .empty-state').forEach(el => el.remove());
            }

            if (data.data.length === 0 && !append) {
                resetVirtualMessages();
                if (historyViewKey !== activeHistoryViewKey) list.scrollTop = 0;
                activeHistoryViewKey = historyViewKey;
                renderHistoryEmptyState(list, keyword);
                const loadMoreWrap = document.getElementById('loadMoreWrap');
                if (loadMoreWrap) loadMoreWrap.style.display = 'none';
                return;
            }

            // Reverse so oldest in batch comes first (for chronological top to bottom render)
            const messages = [...data.data].reverse();
            const hasMore = data.data.length > 0 && (typeof data.has_more === 'boolean' ? data.has_more : data.data.length >= limit);
            historyHasMore = hasMore;
            const loadMoreEl = document.getElementById('loadMoreWrap');
            if (loadMoreEl) {
                loadMoreEl.style.display = '';
                loadMoreEl.dataset.exhausted = String(!hasMore);
            }
            const followLatest = append && !keyword && list.scrollHeight - list.scrollTop - list.clientHeight <= 2;
            setVirtualMessages(messages, { appendOlder: append });
            if (followLatest) timeline.scrollToEnd();
            activeHistoryViewKey = historyViewKey;
            historyLoaded = true;
        } else {
            throw new Error('History request failed');
        }
    } catch (e) {
        if (e.name === 'AbortError') return;
        if (requestSeq !== historyRequestSeq) return;
        console.error(e);
        if (append) {
            historyLoadFailed = true;
            currentPage = Math.max(1, currentPage - 1);
            showClipboardToast('更早记录加载失败，请重试。', true);
        } else if (retainTimeline) {
            showClipboardToast('刷新失败，已保留当前记录，请重试。', true);
        } else {
            clearVirtualDom(list);
            renderHistoryErrorState(list);
            const loadMoreWrap = document.getElementById('loadMoreWrap');
            if (loadMoreWrap) loadMoreWrap.style.display = 'none';
        }
    }
    finally {
        if (slowRequestTimer) clearTimeout(slowRequestTimer);
        if (requestSeq === historyRequestSeq) {
            isHistoryLoading = false;
            if (historyAbortController === requestController) historyAbortController = null;
            list.setAttribute('aria-busy', 'false');
            if (loadMoreBtn) loadMoreBtn.disabled = false;
            syncSearchCancelControl();
            syncHistoryLoadControl();
            if (historyLoaded) {
                // Recheck after prepend anchoring, even if the sentinel stayed visible.
                requestAnimationFrame(() => {
                    if (requestSeq === historyRequestSeq) maybeLoadOlderMessages();
                });
            }
        }
    }
}

async function fetchStats() {
    try {
        const data = await fetchAPI('/api/stats');
        if (data.success) {
            document.getElementById('stat-total').innerText = safeCount(data.data.total_messages).toLocaleString();
            document.getElementById('stat-today').innerText = safeCount(data.data.today_messages).toLocaleString();
        }
    } catch (e) { }
}

function handleSearch() {
    dashboardRequestSeq += 1;
    const keyword = document.getElementById('searchInput').value.trim();
    if (!activeSessionId && !keyword) {
        showDashboard();
        return;
    }
    activeSearchKeyword = keyword;
    syncSearchCancelControl();
    currentPage = 1;
    fetchHistory();
}

const viewport = document.getElementById('messageList');
const scrollBtn = document.getElementById('scrollToBottomBtn');

function syncHistoryLoadControl() {
    const refreshButton = document.getElementById('refreshMessagesBtn');
    if (refreshButton) {
        refreshButton.disabled = isHistoryLoading;
        refreshButton.setAttribute('aria-busy', String(isHistoryLoading));
    }
    const wrap = document.getElementById('loadMoreWrap');
    const button = document.getElementById('loadMoreBtn');
    if (!timeline || !wrap || !button) return;
    let status = wrap.querySelector('.load-more-status');
    if (!status) {
        status = document.createElement('span');
        status.className = 'load-more-status';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        wrap.appendChild(status);
    }
    const searching = !!getActiveSearchKeyword();
    button.hidden = isHistoryLoading || !historyHasMore || (!historyLoadFailed && !searching);
    button.disabled = isHistoryLoading;
    button.querySelector('span').textContent = historyLoadFailed ? '重试加载' : '加载更早的记录';
    status.textContent = isHistoryLoading ? '正在加载更早记录…'
        : historyLoadFailed || (searching && historyHasMore) ? ''
        : historyHasMore ? '上滑加载更早记录' : '已到达这段记录的开头';
}

function maybeLoadOlderMessages() {
    // Search starts at its first match and keeps explicit pagination.
    if (!timeline || isHistoryLoading || !historyHasMore || historyLoadFailed
        || getActiveSearchKeyword() || document.hidden) return;
    if (viewport.scrollTop <= 400) handleLoadMore();
}

const historyStartObserver = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) maybeLoadOlderMessages();
}, { root: viewport, rootMargin: '400px 0px 0px 0px' });

viewport.addEventListener('scroll', () => {
    const awayFromEnd = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight > 200;
    scrollBtn.style.display = timeline && awayFromEnd ? 'flex' : 'none';
    maybeLoadOlderMessages();
}, { passive: true });

scrollBtn.onclick = () => scrollListToBottom(viewport);

async function initApp({ authenticated = false } = {}) {
    const requestSeq = ++appInitRequestSeq;
    const hasSession = authenticated || await hasAuthSession();
    if (requestSeq !== appInitRequestSeq) return;
    if (!hasSession) {
        showAuth(true);
        return;
    }
    showAuth(false);

    // 移动端侧边栏由 CSS :checked + 全局点击关闭控制

    fetchStats();
    fetchSessions();
    if (!window.statsInterval) {
        window.statsInterval = setInterval(() => {
            if (!document.hidden) fetchStats();
        }, 60000);
    }
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) fetchStats();
});

const loadMoreBtn = document.getElementById('loadMoreBtn');
function handleLoadMore() {
    if (isHistoryLoading || !historyHasMore) return;
    currentPage++;
    fetchHistory(true);
}

loadMoreBtn.onclick = handleLoadMore;

// Time Filters Logic
document.querySelectorAll('.time-btn').forEach(btn => {
    btn.setAttribute('aria-pressed', String(btn.classList.contains('active')));
    btn.onclick = () => {
        document.querySelectorAll('.time-btn').forEach(b => {
            b.classList.remove('active');
            b.setAttribute('aria-pressed', 'false');
        });
        btn.classList.add('active');
        btn.setAttribute('aria-pressed', 'true');

        const range = btn.dataset.range;
        const now = new Date();
        now.setHours(23, 59, 59, 999);
        filterEnd = Math.floor(now.getTime() / 1000);

        if (range === 'all') {
            filterStart = 0; filterEnd = 0;
        } else if (range === 'today') {
            const start = new Date(now); start.setHours(0, 0, 0, 0);
            filterStart = Math.floor(start.getTime() / 1000);
        } else if (range === 'week') {
            const start = new Date(now);
            start.setDate(start.getDate() - (start.getDay() === 0 ? 6 : start.getDay() - 1));
            start.setHours(0, 0, 0, 0);
            filterStart = Math.floor(start.getTime() / 1000);
        } else if (range === 'month') {
            const start = new Date(now.getFullYear(), now.getMonth(), 1);
            filterStart = Math.floor(start.getTime() / 1000);
        } else if (range === 'year') {
            const start = new Date(now.getFullYear(), 0, 1);
            filterStart = Math.floor(start.getTime() / 1000);
        }

        const timeStartInput = document.getElementById('timeStart');
        const timeEndInput = document.getElementById('timeEnd');
        timeStartInput.value = '';
        timeEndInput.value = '';
        timeStartInput.setCustomValidity('');
        timeEndInput.setCustomValidity('');
        if (activeSessionId) {
            reloadStats();
            currentPage = 1;
            fetchHistory();
        }
    };
});

function handleCustomTime() {
    const startInput = document.getElementById('timeStart');
    const endInput = document.getElementById('timeEnd');
    const tStart = startInput.value;
    const tEnd = endInput.value;
    endInput.setCustomValidity('');
    if (tStart && tEnd && new Date(tStart).getTime() > new Date(tEnd).getTime()) {
        endInput.setCustomValidity('结束时间不能早于开始时间');
        endInput.reportValidity();
        return;
    }

    document.querySelectorAll('.time-btn').forEach(b => {
        b.classList.remove('active');
        b.setAttribute('aria-pressed', 'false');
    });
    filterStart = tStart ? Math.floor(new Date(tStart).getTime() / 1000) : 0;
    filterEnd = tEnd ? Math.floor(new Date(tEnd).getTime() / 1000) : 0;
    if (activeSessionId) {
        reloadStats();
        currentPage = 1;
        fetchHistory();
    }
}

document.getElementById('timeStart').onchange = handleCustomTime;
document.getElementById('timeEnd').onchange = handleCustomTime;



// Pure JS Panel Control
function syncPanelAccessibility() {
    const sidebar = document.getElementById('sessionSidebar');
    const analysis = document.getElementById('analysisPanel');
    const main = document.querySelector('.main-container');
    const overlay = document.getElementById('mobile-overlay');
    const authBlocked = document.body.classList.contains('auth-blocked');
    const sidebarIsDrawer = window.innerWidth <= SESSION_DRAWER_MAX_WIDTH;
    const analysisIsDrawer = window.innerWidth <= ANALYSIS_DRAWER_MAX_WIDTH;
    const sidebarOpen = sidebarIsDrawer && sidebar?.classList.contains('open');
    const analysisOpen = analysisIsDrawer
        && !document.body.classList.contains('global-view')
        && analysis?.classList.contains('open');
    const drawerOpen = Boolean(sidebarOpen || analysisOpen);

    const setHidden = (element, hidden) => {
        if (!element) return;
        element.toggleAttribute('inert', hidden);
        if (hidden) element.setAttribute('aria-hidden', 'true');
        else element.removeAttribute('aria-hidden');
    };

    if (authBlocked) {
        [sidebar, analysis, main, overlay].forEach(element => setHidden(element, true));
        return;
    }

    setHidden(sidebar, sidebarIsDrawer ? !sidebarOpen : drawerOpen);
    setHidden(analysis, document.body.classList.contains('global-view')
        || (analysisIsDrawer ? !analysisOpen : false));
    setHidden(main, drawerOpen);
    if (overlay) {
        overlay.classList.toggle('active', drawerOpen);
        setHidden(overlay, !drawerOpen);
    }
}

function closeAllPanels({ focusPanel = '' } = {}) {
    const sidebar = document.getElementById('sessionSidebar');
    const analysis = document.getElementById('analysisPanel');
    const overlay = document.getElementById('mobile-overlay');
    const activeElement = document.activeElement;
    const restorePanel = focusPanel
        || (sidebar?.classList.contains('open') ? 'sidebar' : '')
        || (analysis?.classList.contains('open') ? 'analysis' : '')
        || (sidebar?.contains(activeElement) && window.innerWidth <= SESSION_DRAWER_MAX_WIDTH ? 'sidebar' : '')
        || (analysis?.contains(activeElement) && window.innerWidth <= ANALYSIS_DRAWER_MAX_WIDTH ? 'analysis' : '');
    if (sidebar) sidebar.classList.remove('open');
    if (analysis) analysis.classList.remove('open');
    if (overlay) overlay.classList.remove('active');
    document.getElementById('btn-sidebar')?.setAttribute('aria-expanded', 'false');
    document.getElementById('btn-analysis')?.setAttribute('aria-expanded', 'false');
    syncPanelAccessibility();
    if (restorePanel) {
        const openerId = restorePanel === 'sidebar' ? 'btn-sidebar' : 'btn-analysis';
        focusFirstAvailable([
            document.getElementById(openerId),
            document.getElementById('searchInput'),
        ]);
    }
}

window.addEventListener('popstate', () => {
    if (new URLSearchParams(window.location.search).get('view') === 'settings') {
        showSettings({ skipUrl: true });
        return;
    }
    const sessionId = getDesiredSessionId();
    if (sessionId) {
        const meta = sessionsById.get(sessionId);
        if (meta) selectSession(meta.session_id, meta.name, meta.message_type, { replaceUrl: true });
        else selectSession(sessionId, sessionId, '', { replaceUrl: true });
    } else {
        showDashboard({ skipUrl: true });
    }
});

document.addEventListener('DOMContentLoaded', () => {
    initApp();
    document.getElementById('settings-btn')?.addEventListener('click', () => showSettings());

    const loginBtn = document.getElementById('login-btn');
    const logoutBtn = document.getElementById('logout-btn');
    const apiKeyInput = document.getElementById('api-key-input');
    const searchInput = document.getElementById('searchInput');
    const searchCancelBtn = document.getElementById('searchCancelBtn');
    const btnSidebar = document.getElementById('btn-sidebar');
    const btnAnalysis = document.getElementById('btn-analysis');
    const btnCloseSidebar = document.getElementById('btn-close-sidebar');
    const btnCloseAnalysis = document.getElementById('btn-close-analysis');
    const overlay = document.getElementById('mobile-overlay');
    const sidebar = document.querySelector('.sidebar');
    const analysis = document.querySelector('.analysis-panel');

    makeKeyboardActivatable(btnSidebar);
    makeKeyboardActivatable(btnAnalysis);
    makeKeyboardActivatable(btnCloseSidebar, '关闭会话列表');
    makeKeyboardActivatable(btnCloseAnalysis, '关闭数据分析');

    if (loginBtn) {
        loginBtn.addEventListener('click', verifyLogin);
    }

    if (logoutBtn) {
        logoutBtn.addEventListener('click', logout);
    }

    if (apiKeyInput) {
        apiKeyInput.addEventListener('input', () => {
            apiKeyInput.removeAttribute('aria-invalid');
            document.getElementById('auth-error').style.display = 'none';
        });
        apiKeyInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' && !event.isComposing) verifyLogin();
        });
    }

    if (searchInput) {
        searchInput.addEventListener('input', syncSearchCancelControl);
        searchInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' && !event.isComposing) handleSearch();
            if (event.key === 'Escape' && (searchInput.value.trim() || activeSearchKeyword)) {
                event.preventDefault();
                event.stopPropagation();
                cancelSearch();
            }
        });
    }

    if (searchCancelBtn) searchCancelBtn.addEventListener('click', cancelSearch);
    document.getElementById('refreshMessagesBtn')?.addEventListener('click', () => {
        if (activeSessionId && !isHistoryLoading) fetchHistory();
    });

    if (btnSidebar) {
        btnSidebar.addEventListener('click', () => {
            if (window.innerWidth > SESSION_DRAWER_MAX_WIDTH) return;
            if (analysis) analysis.classList.remove('open');
            if (sidebar) sidebar.classList.add('open');
            btnSidebar.setAttribute('aria-expanded', 'true');
            btnAnalysis?.setAttribute('aria-expanded', 'false');
            syncPanelAccessibility();
            requestAnimationFrame(() => {
                if (sidebar?.classList.contains('open')) btnCloseSidebar?.focus({ preventScroll: true });
            });
        });
    }

    if (btnAnalysis) {
        btnAnalysis.addEventListener('click', () => {
            if (window.innerWidth > ANALYSIS_DRAWER_MAX_WIDTH || document.body.classList.contains('global-view')) return;
            if (sidebar) sidebar.classList.remove('open');
            if (analysis) analysis.classList.add('open');
            btnAnalysis.setAttribute('aria-expanded', 'true');
            btnSidebar?.setAttribute('aria-expanded', 'false');
            syncPanelAccessibility();
            const openRequestSeq = ++statsRequestSeq;
            requestAnimationFrame(async () => {
                if (!analysis?.classList.contains('open')) return;
                btnCloseAnalysis?.focus({ preventScroll: true });
                // Refresh after the slide, so chart and member rendering cannot interrupt it.
                await Promise.allSettled(analysis.getAnimations().map(animation => animation.finished));
                if (openRequestSeq === statsRequestSeq && analysis.classList.contains('open') && activeSessionId) reloadStats();
            });
        });
    }

    if (btnCloseSidebar) {
        btnCloseSidebar.addEventListener('click', closeAllPanels);
    }

    if (btnCloseAnalysis) {
        btnCloseAnalysis.addEventListener('click', closeAllPanels);
    }

    if (overlay) {
        overlay.addEventListener('click', closeAllPanels);
    }

    document.addEventListener('keydown', event => {
        const authOverlay = document.getElementById('auth-overlay');
        if (authOverlay && !authOverlay.classList.contains('hidden')) {
            trapFocusWithin(event, authOverlay);
            if (event.key === 'Escape') event.preventDefault();
            return;
        }
        const openDrawer = [sidebar, analysis].find(panel => panel?.classList.contains('open'));
        if (openDrawer) trapFocusWithin(event, openDrawer);
        if (event.key === 'Escape') closeAllPanels();
    });
    syncSearchCancelControl();
    syncPanelAccessibility();
});

function getPanelMode() {
    if (window.innerWidth <= SESSION_DRAWER_MAX_WIDTH) return 'dual-drawer';
    if (window.innerWidth <= ANALYSIS_DRAWER_MAX_WIDTH) return 'analysis-drawer';
    return 'fixed-panels';
}

let panelsWereMobile = getPanelMode();

function handlePanelViewportChange() {
    const panelsAreMobile = getPanelMode();
    if (panelsAreMobile !== panelsWereMobile) {
        const sidebar = document.getElementById('sessionSidebar');
        const analysis = document.getElementById('analysisPanel');
        const activeElement = document.activeElement;
        const sidebarWasOrWillBeDrawer = panelsWereMobile === 'dual-drawer' || panelsAreMobile === 'dual-drawer';
        const analysisWasOrWillBeDrawer = panelsWereMobile !== 'fixed-panels' || panelsAreMobile !== 'fixed-panels';
        let focusPanel = '';
        if (sidebarWasOrWillBeDrawer && (sidebar?.classList.contains('open') || sidebar?.contains(activeElement))) {
            focusPanel = 'sidebar';
        } else if (analysisWasOrWillBeDrawer && (analysis?.classList.contains('open') || analysis?.contains(activeElement))) {
            focusPanel = 'analysis';
        }
        closeAllPanels({ focusPanel });
        panelsWereMobile = panelsAreMobile;
    } else {
        syncPanelAccessibility();
    }
}

window.addEventListener('resize', handlePanelViewportChange, { passive: true });
