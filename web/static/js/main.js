let API_KEY = '';
localStorage.removeItem('astr_chat_key');
let currentPage = 1;
const limit = 50;
let nextCursor = 0;
let activeSessionId = '';
let activeUserId = '';
let isHistoryLoading = false;
const avatarPreloadCache = new Map();
const avatarResolvedCache = new Map();

window.copyToClipboard = (text) => {
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
        const toast = document.createElement('div');
        toast.style = "position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:rgba(15,23,42,0.85); backdrop-filter:blur(8px); border:1px solid rgba(255,255,255,0.1); box-shadow:0 10px 30px rgba(0,0,0,0.5); color:white; padding:10px 20px; border-radius:100px; z-index:9999; font-size:0.85rem; font-weight:500; animation: fadeUp 0.3s cubic-bezier(0.4, 0, 0.2, 1);";
        toast.innerText = "已复制 ID: " + text;
        document.body.appendChild(toast);
        setTimeout(() => {
            toast.style.animation = "fadeDown 0.3s ease";
            setTimeout(() => toast.remove(), 300);
        }, 1500);
    }).catch(err => console.error('Copy failed', err));
};
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
let rankOffset = 0;
let rankTotal = 0;
let rankHasMore = false;
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

const PLATFORM_BADGE_META = {
    'qq': { name: 'QQ', class: 'badge-plat-qq', svg: QQ_SVG, icon: '💬' },
    'telegram': { name: 'Telegram', class: 'badge-plat-telegram', svg: TG_SVG, icon: '✈️' },
    'discord': { name: 'Discord', class: 'badge-plat-discord', svg: DISCORD_SVG, icon: '🎮' },
    'wechat': { name: '微信', class: 'badge-plat-wechat', svg: WECHAT_SVG, icon: '💬' },
    'wecom': { name: '企业微信', class: 'badge-plat-wecom', svg: WECHAT_SVG, icon: '💬' },
    'kook': { name: 'KOOK', class: 'badge-plat-kook', svg: KOOK_SVG, icon: '🦖' },
    'teamspeak': { name: 'TeamSpeak', class: 'badge-plat-teamspeak', svg: TEAMSPEAK_SVG, icon: '🎙️' },
    'feishu': { name: '飞书', class: 'badge-plat-feishu', svg: FEISHU_SVG, icon: '🕊️' },
    'dingtalk': { name: '钉钉', class: 'badge-plat-dingtalk', svg: DINGTALK_SVG, icon: '🔔' }
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


async function fetchAPI(endpoint, method = 'GET', body = null) {
    const headers = {
        'Content-Type': 'application/json'
    };
    if (API_KEY) headers['X-API-Key'] = API_KEY;
    const options = { method, headers };
    if (body) options.body = JSON.stringify(body);

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
        if (err.message === 'Unauthorized') {
            console.error('API Key invalid or expired');
        } else {
            console.error('Fetch error:', err);
            const toast = document.createElement('div');
            toast.style = "position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:rgba(239,68,68,0.9); backdrop-filter:blur(8px); border:1px solid rgba(255,255,255,0.1); box-shadow:0 10px 30px rgba(0,0,0,0.5); color:white; padding:10px 20px; border-radius:100px; z-index:9999; font-size:0.85rem; font-weight:500; animation: fadeUp 0.3s cubic-bezier(0.4, 0, 0.2, 1);";
            toast.innerText = "网络请求失败，请检查连接或稍后重试";
            document.body.appendChild(toast);
            setTimeout(() => {
                toast.style.animation = "fadeDown 0.3s ease";
                setTimeout(() => toast.remove(), 300);
            }, 3000);
        }
        throw err;
    }
}

function showAuth(show) {
    const overlay = document.getElementById('auth-overlay');
    if (show) overlay.classList.remove('hidden');
    else overlay.classList.add('hidden');
}

async function ensureAuthCookie() {
    if (!API_KEY) return false;
    try {
        const res = await fetch('/api/auth/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: API_KEY })
        });
        if (res.status === 401) {
            showAuth(true);
            return false;
        }
        return res.ok;
    } catch (e) {
        console.warn('Auth cookie refresh failed', e);
        return false;
    }
}

async function verifyLogin() {
    const key = document.getElementById('api-key-input').value;
    try {
        const res = await fetch('/api/auth/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: key })
        });
        const data = await res.json();
        if (data.success) {
            API_KEY = key;
            showAuth(false);
            initApp();
        } else {
            document.getElementById('auth-error').style.display = 'block';
        }
    } catch (e) {
        console.error(e);
    }
}

async function logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST', headers: { 'X-API-Key': API_KEY } });
    } catch (e) {
        console.warn('Logout request failed', e);
    }
    API_KEY = '';
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

function isSafeMarkdownUrl(url) {
    const normalized = safeText(url).trim();
    if (!normalized || /[\u0000-\u001F\u007F\s]/.test(normalized)) return false;
    return /^(https?:\/\/|\/static\/)/i.test(normalized);
}

function getMarkdownUrl(urlText) {
    const url = decodeHtmlEntities(urlText).trim();
    return isSafeMarkdownUrl(url) ? url : '';
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
            const url = getMarkdownUrl(urlText);
            if (!url) return match;
            const safeUrl = escapeAttr(url);
            const safeAlt = escapeAttr(decodeHtmlEntities(alt));
            return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer"><img src="${safeUrl}" class="msg-image msg-md-image" alt="${safeAlt || '图片'}" loading="lazy" onload="this.classList.add('loaded')" onerror="this.parentElement.outerHTML='<span class=\\'msg-tag\\' style=\\'opacity:0.6;\\'>🖼️ [图片]</span>'" /></a>`;
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
    const pushSoftBreak = () => {
        if (output.length && output[output.length - 1] !== '<br>') output.push('<br>');
    };

    for (let i = 0; i < lines.length;) {
        const line = lines[i];
        if (isBlank(line)) {
            pushSoftBreak();
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
        output.push(renderInlineLines(paragraph.join('\n')));
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
    const rawText = String(text);
    const previewText = isShareJsonPayload(rawText.trim())
        ? formatSharePreview(rawText.trim())
        : replaceCqJsonCodes(rawText, data => formatSharePreview(data));
    return previewText
        .replace(/\[CQ:image,[^\]]*\]/g, '[图片]')
        .replace(/\[CQ:video,[^\]]*\]/g, '[视频]')
        .replace(/\[CQ:record,[^\]]*\]/g, '[语音]')
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
    url.searchParams.set('session_id', sessionId);
    const state = { session_id: sessionId };
    if (replace) window.history.replaceState(state, '', url);
    else window.history.pushState(state, '', url);
}

function updateDashboardUrl(replace = false) {
    const url = new URL(window.location.href);
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
    const ticks = [0, 0.5, 1].map(t => {
        const y = padY + usableH - usableH * t;
        return `<g><line x1="${padX}" y1="${y}" x2="${width - padX}" y2="${y}" class="dashboard-grid"/><text x="${padX}" y="${y - 4}" class="dashboard-axis">${Math.round(maxValue * t).toLocaleString()}</text></g>`;
    }).join('');
    const circles = coords.map(p => `<circle class="dashboard-dot" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="4.5" data-date="${escapeAttr(p.date)}" data-value="${p.value}" style="cursor: pointer; transition: r 0.15s ease, fill 0.15s ease;"><title>${escapeAttr(p.date)}：${p.value.toLocaleString()} 条</title></circle>`).join('');
    const labels = coords.filter((_p, idx) => idx === 0 || idx === coords.length - 1 || idx % Math.ceil(Math.max(coords.length, 1) / 4) === 0)
        .map(p => `<text x="${p.x.toFixed(1)}" y="${height - 4}" text-anchor="middle" class="dashboard-axis">${escapeAttr(p.date.slice(5))}</text>`).join('');
    return `<svg class="dashboard-trend-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="消息活跃趋势">
        ${ticks}
        ${polyline ? `<polyline points="${polyline}" class="dashboard-line"></polyline>` : ''}
        ${circles}
        ${labels}
    </svg>`;
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
        tooltip.style.cssText = 'position: absolute; display: none; pointer-events: none; z-index: 1000; background: rgba(15, 23, 42, 0.95); border: 1px solid var(--glass-border); border-radius: 8px; padding: 6px 12px; font-size: 0.75rem; color: white; box-shadow: 0 4px 16px rgba(0,0,0,0.5); transition: opacity 0.15s ease; transform: translate(-50%, -100%); font-family: inherit; line-height: 1.4; opacity: 0;';
        panel.appendChild(tooltip);
    }

    dots.forEach(dot => {
        const show = () => {
            const date = dot.getAttribute('data-date');
            const val = parseInt(dot.getAttribute('data-value'), 10) || 0;
            tooltip.innerHTML = `<div style="font-weight: 700; color: var(--primary-light); margin-bottom: 2px;">${date}</div><div style="font-weight: 600;">${val.toLocaleString()} 条消息</div>`;

            tooltip.style.display = 'block';
            tooltip.style.opacity = '1';

            const panelRect = panel.getBoundingClientRect();
            const dotRect = dot.getBoundingClientRect();

            const left = dotRect.left - panelRect.left + dotRect.width / 2;
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
                        <span class="perf-bar-name">📊 全局概览统计查询 (Summary)</span>
                        <span class="perf-bar-time">${timeSummary.toFixed(2)} ms</span>
                    </div>
                    <div class="perf-bar-track">
                        <div class="perf-bar-fill fill-summary" style="width: ${pctSummary}%"></div>
                    </div>
                </div>

                <div class="perf-bar-row">
                    <div class="perf-bar-meta">
                        <span class="perf-bar-name">📈 消息类型分布统计 (Type Dist)</span>
                        <span class="perf-bar-time">${timeType.toFixed(2)} ms</span>
                    </div>
                    <div class="perf-bar-track">
                        <div class="perf-bar-fill fill-type" style="width: ${pctType}%"></div>
                    </div>
                </div>

                <div class="perf-bar-row">
                    <div class="perf-bar-meta">
                        <span class="perf-bar-name">📉 活跃度趋势序列分析 (Trend)</span>
                        <span class="perf-bar-time">${timeTrend.toFixed(2)} ms</span>
                    </div>
                    <div class="perf-bar-track">
                        <div class="perf-bar-fill fill-trend" style="width: ${pctTrend}%"></div>
                    </div>
                </div>

                <div class="perf-bar-row">
                    <div class="perf-bar-meta">
                        <span class="perf-bar-name">👥 活跃群聊排行统计 (Top Groups)</span>
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
    const loadMore = document.getElementById('loadMoreWrap');
    if (loadMore) loadMore.style.display = 'none';

    const summary = data?.summary || {};
    const trend = data?.activity_trend || [];
    const topGroups = data?.top_groups || [];
    const dist = data?.message_type_distribution || [];
    const perf = data?.performance || {};
    const currentRange = data?.range || '30d';

    const rangeButtons = [['1d', '24小时'], ['7d', '7天'], ['30d', '30天']]
        .map(([key, label]) => `<button class="dashboard-range-btn ${currentRange === key ? 'active' : ''}" data-dashboard-range="${key}">${label}</button>`).join('');

    const topGroupHtml = topGroups.length ? topGroups.map((g, idx) => `
        <button class="dashboard-list-item dashboard-clickable" type="button" data-dashboard-session="${escapeAttr(g.session_id)}" data-dashboard-name="${escapeAttr(g.name)}" data-dashboard-type="${escapeAttr(g.message_type)}">
            <span class="dashboard-rank">${idx + 1}</span>
            <span class="dashboard-item-main"><strong>${escapeAttr(g.name)}</strong><small>${escapeAttr(g.last_msg || '暂无消息预览')}</small></span>
            <span class="dashboard-item-count">${formatCompactNumber(g.message_count)}</span>
        </button>
    `).join('') : '<div class="dashboard-muted">暂无群聊排行</div>';

    list.innerHTML = `
        <section class="dashboard-view animate-fade">
            <div class="dashboard-hero">
                <div>
                    <p class="dashboard-kicker">Chat Archive Overview</p>
                    <h2>归档总览</h2>
                    <p>从全局视角查看消息规模、活跃趋势、群排行与 SQL 性能分析。</p>
                </div>
                <div class="dashboard-cache-note">${data?.cached ? '缓存数据' : '实时生成'} · ${data?.cache_ttl || 30}s TTL</div>
            </div>
            <div class="dashboard-summary-grid">
                <div class="dashboard-card"><span>总消息数</span><strong>${formatCompactNumber(summary.total_messages)}</strong></div>
                <div class="dashboard-card"><span>今日消息</span><strong>${formatCompactNumber(summary.today_messages)}</strong></div>
                <div class="dashboard-card"><span>总会话数</span><strong>${formatCompactNumber(summary.total_sessions)}</strong></div>
                <div class="dashboard-card"><span>总图片数</span><strong>${formatCompactNumber(summary.total_images)}</strong></div>
                <div class="dashboard-card"><span>总视频数</span><strong>${formatCompactNumber(summary.total_videos)}</strong></div>
            </div>
            <div class="dashboard-grid-layout">
                <div class="dashboard-panel dashboard-panel-wide">
                    <div class="dashboard-panel-header"><h3>活跃度趋势</h3><div class="dashboard-range-group" id="dashboardRangeGroup">${rangeButtons}</div></div>
                    <div id="dashboardTrendWrapper" style="width: 100%; transition: opacity 0.15s ease;">${renderDashboardTrendSvg(trend)}</div>
                </div>
                <div class="dashboard-panel">
                    <div class="dashboard-panel-header"><h3>消息类型分布</h3></div>
                    ${renderTypeDistribution(dist)}
                </div>
                <div class="dashboard-panel">
                    <div class="dashboard-panel-header"><h3>群活跃排行</h3></div>
                    <div class="dashboard-list">${topGroupHtml}</div>
                </div>
                <div class="dashboard-panel dashboard-panel-wide">
                    <div class="dashboard-panel-header">
                        <h3>⚡ 缓存与 SQL 查询性能分析</h3>
                        ${perf.cache_hit ? '<span class="perf-cache-badge cache-hit">🚀 CACHE HIT</span>' : '<span class="perf-cache-badge cache-miss">🔍 DATABASE QUERY</span>'}
                    </div>
                    ${renderPerformanceChart(perf, summary)}
                </div>
            </div>
        </section>`;
    attachDashboardHandlers(list);
}

async function fetchDashboard(range = '30d') {
    try {
        const trendWrapper = document.getElementById('dashboardTrendWrapper');
        const rangeGroup = document.getElementById('dashboardRangeGroup');
        const isAlreadyVisible = trendWrapper && rangeGroup;

        if (isAlreadyVisible) {
            trendWrapper.style.opacity = '0.5';
        }

        const data = await fetchAPI(`/api/dashboard?range=${encodeURIComponent(range)}&recent_limit=12`);
        if (!data.success) return;

        if (isAlreadyVisible) {
            const trend = data.data?.activity_trend || [];
            const currentRange = data.data?.range || range;

            // 1. Update range buttons
            const rangeButtons = [['1d', '24小时'], ['7d', '7天'], ['30d', '30天']]
                .map(([key, label]) => `<button class="dashboard-range-btn ${currentRange === key ? 'active' : ''}" data-dashboard-range="${key}">${label}</button>`).join('');
            rangeGroup.innerHTML = rangeButtons;

            // 2. Update trend Svg
            trendWrapper.innerHTML = renderDashboardTrendSvg(trend);
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
        console.error(e);
        const list = document.getElementById('messageList');
        if (list && !document.getElementById('dashboardTrendWrapper')) {
            list.innerHTML = '<div class="empty-state"><p style="font-weight:600;">Dashboard 加载失败</p><p>请稍后刷新或检查后端日志。</p></div>';
        }
    }
}


function updateActiveSessionHeader() {
    const header = document.getElementById('activeSessionId');
    const searchInput = document.getElementById('searchInput');
    if (!header) return;
    if (!activeSessionId) {
        const keyword = searchInput ? searchInput.value.trim() : '';
        if (keyword) {
            header.innerHTML = `<div class="active-session-title">🔍 全局搜索: "${escapeAttr(keyword)}"</div><div class="active-session-details"><span class="active-session-chip" style="cursor:pointer;" onclick="document.getElementById('searchInput').value=''; showDashboard();">返回总览 Dashboard</span></div>`;
        } else {
            header.innerHTML = '<div class="active-session-title">📊 Archive Dashboard</div><div class="active-session-details"><span class="active-session-chip">全局总览</span><span class="active-session-chip">点击会话进入回放</span></div>';
        }
        if (searchInput) searchInput.placeholder = '全局搜索消息...';
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
            title = '🖥️ 服务器: ' + title;
        } else if (mt.includes('friend')) {
            title = '👤 私聊: ' + title;
        } else if (mt.includes('channel')) {
            if (title.includes(' / #')) {
                title = title.replace(' / #', ' > #');
            } else if (title.includes(' / ')) {
                title = title.replace(' / ', ' > ');
            }
            title = '📢 频道: ' + title;
        } else if (mt.includes('group')) {
            title = '💬 群聊: ' + title;
        }

        header.innerText = title;
        if (searchInput) searchInput.placeholder = '搜索当前会话...';
    }
}

function showDashboard(options = {}) {
    const searchInput = document.getElementById('searchInput');
    if (searchInput) searchInput.value = '';
    activeSessionId = '';
    activeUserId = '';
    activeMsgType = '';
    currentPage = 1;
    nextCursor = 0;
    isHistoryLoading = false;
    memberRequestSeq += 1;
    rankRequestSeq += 1;
    window.userMap = {};
    window.globalTopUsers = [];

    const list = document.getElementById('messageList');
    if (list) {
        try { listObserver.unobserve(list); } catch (_) { }
        list.scrollTop = 0;
    }
    document.querySelectorAll('.session-item').forEach(el => {
        if (el.classList.contains('dashboard-nav')) el.classList.add('active');
        else el.classList.remove('active');
    });
    document.querySelectorAll('.sidebar-sub-menu').forEach(el => el.remove());
    const loadMore = document.getElementById('loadMoreWrap');
    if (loadMore) loadMore.style.display = 'none';
    if (scrollBtn) scrollBtn.style.display = 'none';
    updateActiveSessionHeader();
    if (!options.skipUrl) updateDashboardUrl(options.replaceUrl === true);
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

function decodeCqParamValue(value) {
    if (!value) return '';
    return String(value)
        .replace(/&amp;/g, '&')
        .replace(/&#44;/g, ',')
        .replace(/&#91;/g, '[')
        .replace(/&#93;/g, ']');
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

function debounceMemberSearch(value) {
    memberSearchKeyword = safeText(value).trim();
    clearTimeout(memberSearchTimer);
    memberSearchTimer = setTimeout(() => {
        fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: 0, append: false });
    }, 180);
}

function toggleCategory(header, content) {
    const willCollapse = !header.classList.contains('collapsed');
    header.classList.toggle('collapsed', willCollapse);

    if (willCollapse) {
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
        container.classList.add('expanded');
        const btn = container.querySelector('.msg-forward-toggle-btn');
        if (btn) btn.textContent = '收起 ';
    } else {
        content.classList.add('collapsed');
        container.classList.remove('expanded');
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
                <div class="msg-forward-header" onclick="toggleForwardCard(this)">
                    <div class="msg-forward-title-row">
                        <span class="msg-forward-icon">📂</span>
                        <span class="msg-forward-title">合并转发消息</span>
                        <span class="msg-forward-count">(共 ${items.length} 条消息${idDisplay})</span>
                    </div>
                    <span class="msg-forward-toggle-btn">展开 </span>
                </div>
                <div class="msg-forward-content collapsed">
                    ${itemsHtml}
                </div>
            </div>
        `;
    } else {
        const idDisplay = forwardId ? ` (未展开, ID: ${escapeAttr(forwardId)})` : ' (未展开)';
        html = `
            <div class="msg-forward-container unexpanded">
                <div class="msg-forward-header" style="cursor: default;">
                    <div class="msg-forward-title-row">
                        <span class="msg-forward-icon">📂</span>
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
    if (!text) return "";
    if (depth > 8) return escapeHtmlText(text);

    if (text.startsWith("<Event,") || (typeof text === 'string' && text.includes("'raw_message':"))) {
        const match = text.match(/['"]raw_message['"]\s*:\s*['"](.*?)['"]/);
        if (match && match[1]) {
            text = match[1];
        } else {
            return `<span style="color: var(--text-muted); font-size: 0.8rem; font-style: italic;">[无法解析的消息内容]</span>`;
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
        shareCards.push(`<span class="msg-share-card"><span class="msg-share-platform">🔗 ${safePlatform}</span>${titleHtml}</span>`);
        return `__CQ_JSON_SHARE_${index}__`;
    };

    const textWithSharePlaceholders = isShareJsonPayload(String(textWithForwardPlaceholders).trim())
        ? makeSharePlaceholder(String(textWithForwardPlaceholders).trim())
        : replaceCqJsonCodes(textWithForwardPlaceholders, makeSharePlaceholder);

    let escaped = escapeHtmlText(textWithSharePlaceholders);
    const markdown = renderMessageMarkdown(escaped);
    escaped = markdown.html;

    function isSafeUrl(url) {
        if (!url) return false;
        return /^(https?:\/\/|\/static\/)/i.test(url);
    }

    // Helper: route NTQQ/gchat media URLs through backend proxy (with auth)
    function proxyUrl(url) {
        const proxyDomains = ['multimedia.nt.qq.com.cn', 'gchat.qpic.cn'];
        if (proxyDomains.some(d => url.includes(d))) {
            return `/api/proxy/image?url=${encodeURIComponent(url)}`;
        }
        return url;
    }

    // CQ Code Handling
    shareCards.forEach((html, index) => {
        escaped = escaped.replace(`__CQ_JSON_SHARE_${index}__`, html);
    });

    // Images
    escaped = escaped.replace(/\[CQ:image,([^\]]+)\]/g, (match, inner) => {
        const urlMatch = inner.match(/url=([^,\]]+)/);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1]);
            url = proxyUrl(url);
            if (!isSafeUrl(url)) return `<span class="msg-tag">🖼️ [图片]</span>`;
            const safeUrl = escapeAttr(url);
            const widthMatch = inner.match(/(?:^|,)width=(\d+)(?:,|$)/);
            const heightMatch = inner.match(/(?:^|,)height=(\d+)(?:,|$)/);
            const width = widthMatch ? parseInt(widthMatch[1], 10) : 0;
            const height = heightMatch ? parseInt(heightMatch[1], 10) : 0;
            const sizeStyle = getImageDisplayStyle(width, height);
            return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer"><img src="${safeUrl}" class="msg-image" alt="图片" loading="lazy"${sizeStyle} onload="this.classList.add('loaded')" onerror="this.parentElement.outerHTML='<span class=\\'msg-tag\\' style=\\'opacity:0.6;\\'>🖼️ [图片]</span>'" /></a>`;
        }
        return `<span class="msg-tag">🖼️ [图片]</span>`;
    });

    // QQ Faces
    escaped = escaped.replace(/\[CQ:face,id=(\d+)[^\]]*\]/g, (match, id) => {
        return `<img src="https://gxh.vip.qq.com/sys/hycdn/sng/face/s/${id}.png" class="msg-face" alt="表情" loading="lazy" onload="this.style.background='none'" onerror="this.style.display='none'" />`;
    });
    escaped = escaped.replace(/\[CQ:face,[^\]]*\]/g, '<span class="msg-tag">😊 表情</span>');

    // Video Handling
    escaped = escaped.replace(/\[CQ:video,([^\]]+)\]/g, (match, inner) => {
        const urlMatch = inner.match(/url=([^,\]]+)/);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1]);
            url = proxyUrl(url);
            if (!isSafeUrl(url)) return `<span class="msg-tag">🎬 [视频]</span>`;
            const safeUrl = escapeAttr(url);
            return `<video src="${safeUrl}" controls class="msg-video" preload="metadata" onerror="this.outerHTML='<span class=\\'msg-tag\\' style=\\'opacity:0.6;\\'>🎬 [视频加载失败]</span>'"></video>`;
        }
        return `<span class="msg-tag">🎬 [视频]</span>`;
    });

    // Voice/Record Handling
    escaped = escaped.replace(/\[CQ:record,([^\]]+)\]/g, (match, inner) => {
        const urlMatch = inner.match(/url=([^,\]]+)/);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1]);
            url = proxyUrl(url);
            if (!isSafeUrl(url)) return `<span class="msg-tag">🎙️ [语音]</span>`;
            const safeUrl = escapeAttr(url);
            return `<div class="msg-audio-wrap"><span class="msg-tag" style="margin-right:6px;">🎙️</span><audio src="${safeUrl}" controls preload="metadata" class="msg-audio" onerror="this.parentElement.outerHTML='<span class=\\'msg-tag\\' style=\\'opacity:0.6;\\'>🎙️ [语音]</span>'"></audio></div>`;
        }
        return `<span class="msg-tag">🎙️ [语音]</span>`;
    });

    // File Handling
    escaped = escaped.replace(/\[CQ:file,([^\]]+)\]/g, (match, inner) => {
        const nameMatch = inner.match(/name=([^,\]]+)/);
        const urlMatch = inner.match(/url=([^,\]]+)/);
        const fileName = nameMatch && nameMatch[1] ? decodeCqParamValue(nameMatch[1]) : '文件';
        const safeName = escapeAttr(fileName);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1]);
            url = proxyUrl(url);
            if (isSafeUrl(url)) {
                return `<a class="msg-tag" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">📄 ${safeName}</a>`;
            }
        }
        return `<span class="msg-tag">📄 ${safeName}</span>`;
    });

    const tags = ["动画表情", "文件", "红包"];
    tags.forEach(tag => {
        const regex = new RegExp(`\\[${tag}\\]`, 'g');
        escaped = escaped.replace(regex, `<span class="msg-tag">📄 [${tag}]</span>`);
    });
    // Fallback plain text tags for voice/video without CQ codes
    escaped = escaped.replace(/\[语音\]/g, '<span class="msg-tag">🎙️ [语音]</span>');
    escaped = escaped.replace(/\[视频\]/g, '<span class="msg-tag">🎥 [视频]</span>');

    escaped = escaped.replace(/\[CQ:at,qq=all[^\]]*\]/g, '<span class="msg-tag" style="background: rgba(239, 68, 68, 0.2); border-color: rgba(239, 68, 68, 0.4); color: #fca5a5;">@全体成员</span>');
    escaped = escaped.replace(/\[CQ:at,qq=(\d+)[^\]]*\]/g, (match, qq) => {
        let name = window.userMap && window.userMap[qq] ? window.userMap[qq] : qq;
        const safeName = escapeAttr(name);
        return `<span class="msg-tag" style="padding:2px 8px; gap:4px; display:inline-flex; align-items:center; color:var(--text-main); background:rgba(255,255,255,0.1);">
            <img src="${getAvatarUrl(qq)}" onerror="this.src=getAvatarUrl('fallback')" style="width:16px; height:16px; border-radius:50%; object-fit:cover;" />
            @${safeName}
        </span>`;
    });
    escaped = escaped.replace(/\[CQ:reply,[^\]]*\]/g, '<span class="msg-tag" style="opacity: 0.8; background:transparent; border-color:rgba(255,255,255,0.2);">💬 回复</span>');

    // Recall Links
    escaped = escaped.replace(/🛡️ \[撤回了一条消息 \(ID: ([^\]]+)\)\]/g, (match, id) => {
        const safeId = escapeAttr(id);
        return `🛡️ [撤回了一条消息 (ID: <span class="recall-link" data-msg-id="${safeId}">${safeId}</span>)]`;
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

window.scrollToMsg = (msgId) => {
    const safeMsgId = CSS.escape(msgId);
    const el = document.querySelector(`.msg-bubble[data-msg-id="${safeMsgId}"]`);
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.add('highlight-flash');
        setTimeout(() => el.classList.remove('highlight-flash'), 2000);
    } else {
        // Try searching in the entire document just in case
        const altEl = document.querySelector(`[data-msg-id="${safeMsgId}"]`);
        if (altEl) {
            altEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            altEl.classList.add('highlight-flash');
            setTimeout(() => altEl.classList.remove('highlight-flash'), 2000);
        } else {
            console.warn(`Message ${msgId} not found in DOM.`);
            // Show a toast or notification if we had a library, but alert is fine for now
            const toast = document.createElement('div');
            toast.style = "position:fixed; top:20px; left:50%; transform:translateX(-50%); background:rgba(0,0,0,0.8); color:white; padding:10px 20px; border-radius:20px; z-index:9999; font-size:0.9rem; animation: fadeUp 0.3s ease;";
            toast.innerText = "该消息不在当前加载范围内";
            document.body.appendChild(toast);
            setTimeout(() => {
                toast.style.animation = "fadeDown 0.3s ease";
                setTimeout(() => toast.remove(), 300);
            }, 2000);
        }
    }
};

document.addEventListener('click', (event) => {
    const recall = event.target.closest('.recall-link[data-msg-id]');
    if (recall) {
        scrollToMsg(recall.getAttribute('data-msg-id') || '');
        return;
    }

    const copy = event.target.closest('.msg-id[data-copy-id]');
    if (copy) {
        copyToClipboard(copy.getAttribute('data-copy-id') || '');
    }
});

function isSafeAvatarUrl(url) {
    return /^(https?:\/\/|\/static\/)/i.test(safeText(url));
}

function isQqLikePlatform(platformName = '') {
    const platform = safeText(platformName).toLowerCase();
    return !platform || platform.includes('qq') || platform.includes('onebot') || platform === 'aiocqhttp';
}

function getAvatarUrl(userId, avatarUrl = '', platformName = '') {
    const directUrl = safeText(avatarUrl);
    if (directUrl && isSafeAvatarUrl(directUrl)) {
        return directUrl;
    }
    if (isQqLikePlatform(platformName) && /^\d+$/.test(userId)) {
        return `https://q1.qlogo.cn/g?b=qq&nk=${userId}&s=100`;
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
            avatarResolvedCache.set(key, src);
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
    avatarPreloadCache.set(key, promise);
    return promise;
}

async function preloadUserAvatars(users, limit = 12) {
    const pending = users.slice(0, limit).map(u => preloadAvatar(u.user_id, u.avatar_url, u.platform_name));
    await Promise.allSettled(pending);
}

function showSkeleton(containerId, count = 5) {
    const container = document.getElementById(containerId);
    container.querySelectorAll('.message-group, .skeleton-group, .date-divider, .empty-state').forEach(el => el.remove());
    for (let i = 0; i < count; i++) {
        const sk = document.createElement('div');
        sk.className = 'skeleton-group animate-fade';
        sk.style.animationDelay = `${i * 0.05}s`;
        sk.innerHTML = `
            <div class="avatar-col">
                <div class="skeleton" style="width: 40px; height: 40px; border-radius: 12px;"></div>
            </div>
            <div class="content-col" style="width: 100%;">
                <div class="skeleton" style="width: 100px; height: 16px; margin-bottom: 4px; border-radius: 4px;"></div>
                <div class="skeleton" style="width: 60%; height: 60px; border-radius: 18px;"></div>
            </div>
        `;
        container.appendChild(sk);
    }
}

function createMessageBubble(msg) {
    const isRecalled = msg.is_recalled === 1;
    const bubble = document.createElement('div');
    bubble.className = `msg-bubble animate-fade ${isRecalled ? 'recalled-msg' : ''}`;
    bubble.dataset.msgId = safeText(msg.msg_id);

    const text = document.createElement('div');
    text.className = 'msg-text';
    text.innerHTML = formatMsg(msg.message);
    settleLoadedImages(text);
    if (text.querySelector('.msg-image, .msg-video')) {
        bubble.classList.add('msg-bubble-media');
    }

    const footer = document.createElement('div');
    footer.className = 'msg-footer';

    const id = document.createElement('span');
    id.className = 'msg-id';
    id.title = '平台消息ID';
    id.dataset.copyId = safeText(msg.msg_id);
    id.textContent = `#${safeText(msg.msg_id, 'N/A') || 'N/A'}`;
    footer.appendChild(id);

    if (isRecalled) {
        const recalled = document.createElement('span');
        recalled.className = 'msg-tag';
        recalled.style.background = 'rgba(239, 68, 68, 0.1)';
        recalled.style.color = 'var(--danger)';
        recalled.style.borderColor = 'rgba(239,68,68,0.2)';
        recalled.textContent = '已撤回';
        footer.appendChild(recalled);
    }

    if (msg.message_truncated) {
        const truncated = document.createElement('span');
        truncated.className = 'msg-tag';
        truncated.title = `原始长度 ${safeCount(msg.message_length).toLocaleString()} 字符`;
        truncated.textContent = '已截断';
        footer.appendChild(truncated);
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
    'all': { name: '全部', icon: '🌈', color: '#6366f1' },
    'qq': { name: 'QQ', icon: '💬', color: '#ffffff' },
    'telegram': { name: 'Telegram', icon: '✈️', color: '#0088cc' },
    'discord': { name: 'Discord', icon: '🎮', color: '#5865F2' }
};

const NON_QQ_PLATFORMS = ['telegram', 'discord', 'kook', 'feishu', 'dingtalk', 'wechat', 'wecom'];

function normalizePlatformName(platformName) {
    if (!platformName) return '';
    const plat = platformName.toLowerCase();
    if (NON_QQ_PLATFORMS.includes(plat)) {
        return plat;
    }
    return 'qq';
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
            name: plat.charAt(0).toUpperCase() + plat.slice(1),
            icon: '🌐',
            color: '#8b5cf6'
        };
        const isActive = activePlatform === plat;
        const color = meta.color;
        const style = isActive ?
            `style="--active-bg: ${color}cc; --active-border: ${color}; --active-glow: ${color}33;"` : '';

        // Use dynamic SVG if defined, otherwise fallback to standard icon emoji/text
        const iconHtml = (badgeMeta && badgeMeta.svg) ? badgeMeta.svg : meta.icon;

        return `
            <div class="platform-tab ${isActive ? 'active' : ''}" data-platform="${plat}" ${style}>
                <span class="platform-icon">${iconHtml}</span>
                <span class="platform-name">${meta.name || (badgeMeta && badgeMeta.name)}</span>
            </div>
        `;
    }).join('');

    // Attach click handlers
    bar.querySelectorAll('.platform-tab').forEach(tab => {
        tab.onclick = () => {
            activePlatform = tab.dataset.platform;
            renderPlatformFilter(sessions);
            renderSessionList(sessions);
        };
    });

    // Horizontal mouse wheel scrolling for PC
    if (!bar.dataset.wheelAttached) {
        bar.addEventListener('wheel', (e) => {
            if (e.deltaY !== 0) {
                e.preventDefault();
                bar.scrollLeft += e.deltaY;
            }
        }, { passive: false });
        bar.dataset.wheelAttached = 'true';
    }
}

function renderSessionList(sessions) {
    const list = document.getElementById('sessionList');
    if (!list) return;
    list.innerHTML = '';

    const dashboardItem = document.createElement('div');
    dashboardItem.className = `session-item dashboard-nav ${!activeSessionId ? 'active' : ''}`;
    dashboardItem.innerHTML = `
        <div class="dashboard-nav-icon">📊</div>
        <div class="session-info">
            <div class="session-name">总览 Dashboard</div>
            <div class="session-last">整体数据、趋势与最近消息</div>
        </div>`;
    dashboardItem.onclick = () => showDashboard();
    list.appendChild(dashboardItem);

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
        const label = document.createElement('span');
        label.textContent = `${groupData.name} `;
        const count = document.createElement('small');
        count.style.opacity = '0.5';
        count.style.fontWeight = 'normal';
        count.textContent = displayCount;
        label.appendChild(count);
        const toggle = document.createElement('span');
        toggle.className = 'toggle-icon';
        toggle.textContent = '▼';
        header.append(label, toggle);

        const content = document.createElement('div');
        content.className = 'category-content';
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

                // Get platform specific icon
                let platformIcon = '🎮';
                if (platform === 'kook') platformIcon = '🦖';
                else if (platform === 'teamspeak') platformIcon = '🎙️';

                // Get the server icon URL from the first channel in the server group
                const serverIconUrl = channels[0] && channels[0].session.avatar;
                let serverIconHtml = '';
                if (serverIconUrl && serverIconUrl.trim() !== '') {
                    serverIconHtml = `<img src="${escapeAttr(serverIconUrl)}" class="server-avatar-img" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';" style="width:100%; height:100%; object-fit:cover;" /><span class="server-icon-fallback" style="display:none; width:100%; height:100%; align-items:center; justify-content:center;">${platformIcon}</span>`;
                } else {
                    serverIconHtml = `<span class="server-icon-fallback" style="width:100%; height:100%; display:flex; align-items:center; justify-content:center;">${platformIcon}</span>`;
                }

                const serverHeader = document.createElement('div');
                serverHeader.className = `discord-server-header ${isCollapsed ? 'collapsed' : ''}`;
                serverHeader.innerHTML = `
                    <span class="server-arrow">▼</span>
                    <span class="server-icon" style="padding: 0;">${serverIconHtml}</span>
                    <span class="server-title"></span>
                    <span class="channel-count-badge">${channels.length}</span>
                `;
                serverHeader.querySelector('.server-title').textContent = serverName;

                const channelsList = document.createElement('div');
                channelsList.className = `discord-channels-list ${isCollapsed ? 'collapsed' : ''}`;

                serverHeader.onclick = (e) => {
                    e.stopPropagation();
                    const nowCollapsed = !channelsList.classList.contains('collapsed');
                    if (nowCollapsed) {
                        channelsList.classList.add('collapsed');
                        serverHeader.classList.add('collapsed');
                        localStorage.setItem(`server_collapsed_${serverName}`, 'true');
                    } else {
                        channelsList.classList.remove('collapsed');
                        serverHeader.classList.remove('collapsed');
                        localStorage.setItem(`server_collapsed_${serverName}`, 'false');
                    }
                };

                channels.forEach((c, cIdx) => {
                    const s = c.session;
                    const item = document.createElement('div');
                    const isActive = activeSessionId === s.session_id;
                    item.className = `session-item discord-channel-item session-enter ${isActive ? 'active' : ''}`;
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
                item.className = `session-item session-enter ${activeSessionId === s.session_id ? 'active' : ''}`;
                item.dataset.sessionId = s.session_id;
                item.style.animationDelay = `${Math.min(idx, 8) * 0.025}s`;
                item.onclick = (e) => {
                    e.stopPropagation();
                    selectSession(s.session_id, s.name, s.message_type);
                };

                const avatarUrl = escapeAttr(s.avatar || getAvatarUrl('fallback'));
                const lastTime = safeCount(s.last_time);
                const lastDate = lastTime ? new Date(lastTime * 1000).toLocaleDateString() : '';
                const sPlat = getSessionPlatform(s);
                const badgeHtml = getPlatformBadgeHtml(sPlat);

                item.innerHTML = `
                    <img class="session-avatar" src="${avatarUrl}" onerror="this.src=getAvatarUrl('fallback')" />
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

        list.appendChild(header);
        list.appendChild(content);
    });
}

async function fetchSessions() {
    try {
        const data = await fetchAPI('/api/sessions');
        if (data.success) {
            rawSessions = data.data;
            renderPlatformFilter(rawSessions);
            renderSessionList(rawSessions);

            const desiredSessionId = getDesiredSessionId();
            if (desiredSessionId) {
                const targetSession = data.data.find(s => s.session_id === desiredSessionId);
                if (targetSession) {
                    setTimeout(() => selectSession(targetSession.session_id, targetSession.name, targetSession.message_type, { replaceUrl: true }), 100);
                } else {
                    setTimeout(() => selectSession(desiredSessionId, desiredSessionId, '', { replaceUrl: true }), 100);
                }
            } else {
                setTimeout(() => showDashboard({ replaceUrl: true }), 100);
            }
        }
    } catch (e) { console.error(e); }
}

async function selectSession(sessionId, name, msgType, options = {}) {
    if (activeSessionId === sessionId && activeUserId === '') {
        if (window.innerWidth <= 1400) {
            closeAllPanels();
        }
        document.querySelector(`.session-item[data-session-id="${CSS.escape(sessionId)}"]`)?.classList.add('active');
        return;
    }

    if (window.innerWidth <= 1400) {
        closeAllPanels();
    }
    activeMsgType = msgType || '';
    activeSessionId = sessionId;
    updateSessionUrl(sessionId, options.replaceUrl === true);
    memberRequestSeq += 1;
    rankRequestSeq += 1;
    memberOffset = 0;
    memberTotal = 0;
    memberHasMore = false;
    rankOffset = 0;
    rankTotal = 0;
    rankHasMore = false;
    sidebarMemberUsers = [];
    rankMemberUsers = [];
    window.globalTopUsers = [];
    memberSearchKeyword = '';
    const storedMeta = sessionsById.get(sessionId) || {};
    sessionsById.set(sessionId, { ...storedMeta, session_id: sessionId, name: name || storedMeta.name || sessionId, message_type: msgType || storedMeta.message_type || '' });
    updateActiveSessionHeader();

    document.querySelectorAll('.session-item').forEach(el => {
        if (el.dataset.sessionId === sessionId) el.classList.add('active');
        else el.classList.remove('active');
    });

    document.querySelectorAll('.sidebar-sub-menu').forEach(el => el.remove());
    if (activeUserId !== '') activeUserId = '';
    window.userMap = {};

    const activeItem = document.querySelector(`.session-item[data-session-id="${CSS.escape(sessionId)}"]`);
    if (activeItem && !isFriendSessionType()) {
        const subMenu = document.createElement('div');
        subMenu.className = 'sidebar-sub-menu';

        const searchBox = document.createElement('div');
        searchBox.className = 'sub-menu-search';
        const searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.id = 'memberSearch';
        searchInput.placeholder = '定位成员...';
        searchInput.addEventListener('click', (event) => event.stopPropagation());
        searchInput.oninput = (e) => debounceMemberSearch(e.target.value);
        searchBox.appendChild(searchInput);

        const userListContainer = document.createElement('div');
        userListContainer.id = 'userListContainer';

        subMenu.appendChild(searchBox);
        subMenu.appendChild(userListContainer);
        activeItem.after(subMenu);
    }

    currentPage = 1;
    reloadStats();
    fetchHistory();
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
        subItem.dataset.userId = safeText(u.user_id);
        const wrap = document.createElement('span');
        wrap.style.cssText = 'display:flex; align-items:center; gap:0.4rem; overflow:hidden;';
        const avatar = document.createElement('img');
        const avatarKey = `${safeText(u.platform_name)}:${safeText(u.user_id)}:${safeText(u.avatar_url)}`;
        avatar.src = avatarResolvedCache.get(avatarKey) || getAvatarUrl(u.user_id, u.avatar_url, u.platform_name);
        avatar.onerror = () => { avatar.src = getAvatarUrl('fallback'); };
        avatar.loading = 'lazy';
        avatar.decoding = 'async';
        avatar.style.cssText = 'width:20px; height:20px; border-radius:50%; flex-shrink:0; object-fit:cover;';
        const name = document.createElement('span');
        name.className = 'sub-name';
        name.style.cssText = 'white-space:nowrap; overflow:hidden; text-overflow:ellipsis;';
        name.textContent = safeText(u.sender_name);
        wrap.append(avatar, name);
        const count = document.createElement('small');
        count.style.opacity = '0.6';
        count.style.flexShrink = '0';
        count.textContent = safeCount(u.count);
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
            if (window.innerWidth <= 1400) {
                closeAllPanels();
            }
            if (previousUserId === activeUserId) return;
            reloadStats();
            fetchHistory();
        };
        fragment.appendChild(subItem);
    });

    if (memberHasMore) {
        const more = document.createElement('button');
        more.type = 'button';
        more.className = 'member-load-more';
        more.textContent = `加载更多 (${Math.min(memberOffset + memberPageSize, memberTotal)}/${memberTotal})`;
        more.onclick = (event) => {
            event.stopPropagation();
            fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: memberOffset, append: true });
        };
        fragment.appendChild(more);
    }

    container.replaceChildren(fragment);
    openSidebarSubMenu(subMenu);
}

async function fetchMembers({ target = 'both', keyword = '', offset = 0, append = false, limit = null } = {}) {
    if (!activeSessionId || isFriendSessionType()) return;
    const updateSidebar = target === 'sidebar' || target === 'both';
    const updateRank = target === 'rank' || target === 'both';
    const sidebarSeq = updateSidebar ? ++memberRequestSeq : memberRequestSeq;
    const rankSeq = updateRank ? ++rankRequestSeq : rankRequestSeq;
    const requestKeyword = updateRank && !updateSidebar ? '' : safeText(keyword).trim();
    const defaultLimit = !append && offset === 0 ? getInitialMemberLimit() : memberPageSize;
    const fetchLimit = Math.max(1, Math.min(100, safeCount(limit) || defaultLimit));
    let url = `/api/members?session_id=${encodeURIComponent(activeSessionId)}&limit=${fetchLimit}&offset=${offset}`;
    if (requestKeyword) url += `&keyword=${encodeURIComponent(requestKeyword)}`;
    if (filterStart) url += `&time_start=${filterStart}`;
    if (filterEnd) url += `&time_end=${filterEnd}`;

    try {
        const res = await fetchAPI(url);
        if (!res.success) return;
        const payload = res.data || {};
        const members = payload.members || [];
        preloadUserAvatars(members);

        if (updateSidebar && sidebarSeq === memberRequestSeq) {
            sidebarMemberUsers = append ? sidebarMemberUsers.concat(members) : members;
            window.globalTopUsers = sidebarMemberUsers;
            memberOffset = offset + members.length;
            memberTotal = safeCount(payload.total);
            memberHasMore = !!payload.has_more;
            renderUserList(sidebarMemberUsers);
        }

        if (updateRank && rankSeq === rankRequestSeq) {
            if (!append) memberAutoFillCount = 0;
            rankMemberUsers = append ? rankMemberUsers.concat(members) : members;
            rankOffset = offset + members.length;
            rankTotal = safeCount(payload.total);
            rankHasMore = !!payload.has_more;
            renderAnalysisMemberList(rankMemberUsers, rankHasMore, rankTotal);
        }
    } catch (e) {
        console.error(e);
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
        item.addEventListener('click', () => {
            const nextUserId = item.getAttribute('data-user-id');
            if (!nextUserId) return;
            activeUserId = nextUserId;

            document.querySelectorAll('.sub-menu-item').forEach(el => {
                if (el.dataset.userId === nextUserId) el.classList.add('active');
                else el.classList.remove('active');
            });
            reloadStats();
            fetchHistory();
        });
    });
}

function renderMemberRankItems(users) {
    return users.map((u, idx) => {
        const rank = idx + 1;
        const div = document.createElement('div');
        div.innerText = u.sender_name;
        const safeName = div.innerHTML;

        let rankDisp = rank;
        if (rank === 1) rankDisp = '🥇';
        else if (rank === 2) rankDisp = '🥈';
        else if (rank === 3) rankDisp = '🥉';

        return `
            <div class="rank-item" data-rank="${rank}" data-user-id="${escapeAttr(u.user_id)}" data-user-name="${escapeAttr(u.sender_name)}">
                <div class="rank-number">${rankDisp}</div>
                <img src="${getAvatarUrl(u.user_id, u.avatar_url, u.platform_name)}" class="rank-avatar" loading="lazy" decoding="async" onerror="this.src=getAvatarUrl('fallback')" />
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
    list.innerHTML = renderMemberRankItems(users);
    attachRankItemHandlers(list);
    if (more) {
        more.style.display = hasMore ? 'block' : 'none';
        more.textContent = `加载更多 (${Math.min(rankOffset + memberPageSize, total)}/${total})`;
        more.onclick = () => fetchMembers({ target: 'rank', keyword: '', offset: rankOffset, append: true });
    }
    scheduleAnalysisMemberAutofill();
}

async function reloadStats() {
    if (!activeSessionId) return;
    try {
        let qs = `/api/stats?session_id=${encodeURIComponent(activeSessionId)}`;
        if (activeUserId) qs += `&user_id=${encodeURIComponent(activeUserId)}`;
        if (isFriendSessionType()) qs += `&is_private=1`;
        if (filterStart) qs += `&time_start=${filterStart}`;
        if (filterEnd) qs += `&time_end=${filterEnd}`;

        const res = await fetchAPI(qs);
        if (res.success) {
            updateAnalysisPanel(res.data);
            if (!activeUserId && res.data.top_users) {
                refreshMembersForActiveSession();
            }
        } else {
            updateAnalysisPanel(null);
        }
    } catch (e) {
        updateAnalysisPanel(null);
    }
}

function renderBarChartUI(distribution) {
    if (!distribution || distribution.length !== 12) return '';
    const values = distribution.map(safeCount);
    let maxCount = Math.max(...values, 1);
    let html = `<div class="bar-chart-container animate-fade">`;
    for (let i = 0; i < 12; i++) {
        const value = values[i];
        let h = maxCount > 0 ? (value / maxCount) * 100 : 0;
        let timeLabel = `${i * 2}:00 - ${i * 2 + 2}:00`;
        html += `
            <div class="bar-wrapper">
                <div class="bar animate-grow-bar" style="height: 0%;" data-height="${h}%"></div>
                <div class="bar-tooltip">${timeLabel}<br/>${value.toLocaleString()}条</div>
                <div class="bar-label">${i * 2}</div>
            </div>
        `;
    }
    html += `</div>`;

    // Smooth transition growing delay in the next macrotask
    setTimeout(() => {
        document.querySelectorAll('.animate-grow-bar').forEach(bar => {
            const tgt = bar.getAttribute('data-height');
            if (tgt) {
                bar.style.height = tgt;
            }
        });
    }, 50);

    return html;
}

function updateAnalysisPanel(data) {
    const panel = document.getElementById('analysisPanel');
    const content = document.getElementById('analysisContent');
    if (!data) {
        panel.style.display = 'none';
        if (typeof analysisVisible !== 'undefined') analysisVisible = false;
        return;
    }

    panel.style.display = 'flex';
    if (typeof analysisVisible !== 'undefined') analysisVisible = true;
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
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <div class="stat-card" style="padding: 0.8rem 0.5rem;" title="这段时间内该成员发出的消息总条数">
                    <div class="value" style="font-size: 1.2rem;">${safeCount(data.total_messages).toLocaleString()}</div>
                    <div class="label">发言数</div>
                </div>
                <div class="stat-card" style="padding: 0.8rem 0.5rem;" title="刨除掉媒体与CQ等代码后，每发一条纯文字时的平均字符长度">
                    <div class="value" style="font-size: 1.2rem;">${textLen.toLocaleString()} 字</div>
                    <div class="label">均字长度</div>
                </div>
                <div class="stat-card" style="padding: 0.8rem 0.5rem;" title="真正有过发话记录的活跃天数">
                    <div class="value" style="font-size: 1.2rem;">${activeDays.toLocaleString()} 天</div>
                    <div class="label">活跃天数</div>
                </div>
                <div class="stat-card" style="padding: 0.8rem 0.5rem;" title="在一天之中按统计倾向概率最爱出没的高频时间起势">
                    <div class="value" style="font-size: 1.2rem;">${escapeAttr(peakTime)}</div>
                    <div class="label">巅峰出没期</div>
                </div>
            </div>
        `;
    } else {
        html += `
            <div style="display:flex; gap:10px;">
                <div class="stat-card" style="flex:1; padding: 1rem 0.5rem;">
                    <div class="value" style="font-size: 1.5rem;">${safeCount(data.total_messages).toLocaleString()}</div>
                    <div class="label">该时段发言数</div>
                </div>
                <div class="stat-card" style="flex:1; padding: 1rem 0.5rem;" title="无论选取何时间段，此项固定为选定记录全局今日总揽">
                    <div class="value" style="font-size: 1.5rem;">${safeCount(data.today_messages).toLocaleString()}</div>
                    <div class="label">全局今日数</div>
                </div>
            </div>
        `;
    }

    html += `
        <div style="margin-top:0.5rem;">
            <div class="section-title">📊 活跃时段分布</div>
            ${renderBarChartUI(data.time_distribution)}
        </div>
    `;

    if (isIndividual) {
        html += `<div style="margin-top: 0.5rem;"><div class="section-title">📝 消息形式分析</div><div class="type-list">`;
        data.message_types.forEach(t => {
            if (safeCount(t.value) > 0) {
                html += `
                    <div class="type-item">
                        <div class="type-name"><span>${escapeAttr(t.name)}</span></div>
                        <div class="type-value">${safeCount(t.value).toLocaleString()}</div>
                    </div>
                `;
            }
        });
        html += `</div></div>`;
    } else if (data.top_users && data.top_users.length > 0) {
        html += `
            <div style="margin-top: 0.5rem;">
                <div class="section-title">🏆 活跃成员排行</div>
                <div class="rank-list" id="rankList">${renderMemberRankItems(data.top_users.slice(0, getInitialMemberLimit()))}</div>
                <button type="button" class="member-load-more" id="rankLoadMore" style="display:none;">加载更多</button>
            </div>
        `;
    }

    content.innerHTML = html;

    if (!isIndividual && data.top_users && data.top_users.length > 0) {
        attachRankItemHandlers();
    }
}

// 优化后的滚动到底部函数：使用 ScrollIntoView 配合锚点，对移动端更友好
function scrollListToBottom(el) {
    const anchor = document.getElementById('scroll-anchor');
    if (anchor) {
        // 使用 behavior: 'auto' 确保瞬间触底，不给浏览器由于图片加载导致偏移的机会
        anchor.scrollIntoView({ behavior: 'auto', block: 'end' });
    } else {
        el.scrollTop = el.scrollHeight;
    }
}

function disablePrependAnimations(fragment) {
    fragment.querySelectorAll('.animate-fade').forEach(el => {
        el.classList.remove('animate-fade');
        el.style.animationDelay = '';
    });
}

function restorePrependAnchor(list, anchorEl, anchorTop) {
    if (!anchorEl || !anchorEl.isConnected || anchorTop === null) return;
    list.scrollTop += anchorEl.getBoundingClientRect().top - anchorTop;
}

function getPrependAnchor(list) {
    const listTop = list.getBoundingClientRect().top;
    const candidates = list.querySelectorAll('.message-group, .msg-system-center, .empty-state');
    for (const el of candidates) {
        const rect = el.getBoundingClientRect();
        if (rect.bottom > listTop + 8) {
            return el;
        }
    }
    return null;
}

function stabilizePrependAnchor(list, anchorEl, anchorTop, attempts = 2) {
    restorePrependAnchor(list, anchorEl, anchorTop);
    if (attempts <= 1) return;
    requestAnimationFrame(() => stabilizePrependAnchor(list, anchorEl, anchorTop, attempts - 1));
}

const MAX_RENDERED_MESSAGE_BLOCKS = 700;

function pruneMessageDom(list, removeFromBottom = false) {
    const blocks = Array.from(list.querySelectorAll('.message-group, .msg-system-center, .date-divider'));
    const overflow = blocks.length - MAX_RENDERED_MESSAGE_BLOCKS;
    if (overflow <= 0) return;

    const victims = removeFromBottom ? blocks.slice(-overflow) : blocks.slice(0, overflow);
    let removedAboveHeight = 0;
    const listTop = list.getBoundingClientRect().top;

    victims.forEach(el => {
        if (!removeFromBottom) {
            const rect = el.getBoundingClientRect();
            if (rect.bottom <= listTop) removedAboveHeight += rect.height;
        }
        el.remove();
    });

    // If we remove nodes above the viewport, compensate to avoid visible jumps.
    if (removedAboveHeight > 0) list.scrollTop -= removedAboveHeight;
}

// 监听容器高度变化（如图片加载），自动保持底部
const listObserver = new ResizeObserver(entries => {
    if (isHistoryLoading) return; // 正在加载历史记录时，禁止触发自动滚动，避免高度突变导致闪跳/误触底
    const list = document.getElementById('messageList');
    if (!list) return;

    // 如果用户距离底部小于 150px，则在内容高度变化时自动跟随后续增长
    const isNearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 150;
    if (isNearBottom) {
        scrollListToBottom(list);
    }
});

async function fetchHistory(append = false) {
    const keyword = document.getElementById('searchInput').value.trim();
    if (!activeSessionId && !keyword) return;
    if (isHistoryLoading) return;
    isHistoryLoading = true;

    updateActiveSessionHeader();

    const list = document.getElementById('messageList');
    if (!append) {
        currentPage = 1;
        nextCursor = 0;
        // 清除 dashboard 视图（innerHTML 生成的内容不含 loadMoreWrap）
        list.querySelectorAll('.dashboard-view').forEach(el => el.remove());
        // 确保 loadMoreWrap 存在（dashboard 的 innerHTML 可能已销毁它）
        if (!document.getElementById('loadMoreWrap')) {
            const wrap = document.createElement('div');
            wrap.id = 'loadMoreWrap';
            wrap.style.cssText = 'display: none; padding: 1rem 0; text-align: center; margin-bottom: 1rem;';
            const btn = document.createElement('button');
            btn.className = 'primary-btn';
            btn.id = 'loadMoreBtn';
            btn.type = 'button';
            btn.tabIndex = -1;
            btn.style.cssText = 'width: auto; padding: 0.4rem 2rem; background: rgba(255,255,255,0.05); border: 1px solid var(--glass-border); color: var(--text-sub); border-radius: 100px; font-size: 0.8rem; cursor: pointer; transition: all 0.2s;';
            btn.textContent = '⇧ 加载更早的记录';
            btn.addEventListener('pointerdown', (e) => e.preventDefault());
            btn.addEventListener('mousedown', (e) => e.preventDefault());
            btn.addEventListener('pointerup', (e) => {
                if (e.pointerType !== 'mouse') { e.preventDefault(); handleLoadMore(); }
            });
            btn.onclick = handleLoadMore;
            wrap.appendChild(btn);
            list.prepend(wrap);
        }
        // 开始监听高度变化
        listObserver.observe(list);
    }
    const loadMoreBtn = document.getElementById('loadMoreBtn');
    if (append && loadMoreBtn) loadMoreBtn.disabled = true;

    try {
        let url = `/api/history?session_id=${encodeURIComponent(activeSessionId)}&user_id=${encodeURIComponent(activeUserId)}&keyword=${encodeURIComponent(keyword)}&page=${currentPage}&limit=${limit}`;
        if (append && nextCursor > 0) url += `&cursor=${nextCursor}`;
        if (filterStart) url += `&time_start=${filterStart}`;
        if (filterEnd) url += `&time_end=${filterEnd}`;

        const data = await fetchAPI(url);

        if (data.success) {
            if (data.next_cursor !== undefined) nextCursor = data.next_cursor;
            if (data.user_profiles) {
                for (const [uid, profile] of Object.entries(data.user_profiles)) {
                    if (profile && profile.sender_name) {
                        window.userMap[uid] = profile.sender_name;
                    }
                }
            }
            if (!append) {
                list.querySelectorAll('.message-group, .skeleton-group, .date-divider, .empty-state').forEach(el => el.remove());
            }

            if (data.data.length === 0 && !append) {
                const empty = document.createElement('div');
                empty.className = 'empty-state';
                empty.innerHTML = `
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
                    <p style="font-weight:600;">未找到相关记录</p>
                `;
                list.appendChild(empty);
                const loadMoreWrap = document.getElementById('loadMoreWrap');
                if (loadMoreWrap) loadMoreWrap.style.display = 'none';
                return;
            }

            // Reverse so oldest in batch comes first (for chronological top to bottom render)
            const messages = [...data.data].reverse();

            let renderUserId = null;
            let renderSessionId = null;
            let renderDateStr = null;
            let renderMessageCard = null;

            const fragment = document.createDocumentFragment();
            let loadMoreEl = document.getElementById('loadMoreWrap');

            messages.forEach(msg => {
                const dateStr = getDateStr(msg.timestamp);

                if (dateStr !== renderDateStr) {
                    const divider = document.createElement('div');
                    divider.className = 'date-divider animate-fade';
                    const dateLabel = document.createElement('span');
                    dateLabel.textContent = dateStr;
                    divider.appendChild(dateLabel);
                    fragment.appendChild(divider);
                    renderDateStr = dateStr;
                    renderUserId = null; // Next message forces new group wrapper
                    renderSessionId = null;
                }

                const bubble = createMessageBubble(msg);

                if (msg.user_id === '0' || msg.user_id === 0) {
                    // System message: center it
                    const systemMsg = document.createElement('div');
                    systemMsg.className = 'msg-system-center animate-fade';
                    systemMsg.innerHTML = `<span>${formatMsg(msg.message)}</span>`;
                    fragment.appendChild(systemMsg);
                    renderUserId = null; // Break grouping
                    renderSessionId = null;
                    renderMessageCard = null;
                } else if (renderUserId === msg.user_id && renderMessageCard && (activeSessionId !== '' || renderSessionId === msg.session_id)) {
                    const bubbleList = renderMessageCard.querySelector('.msg-bubble-list');
                    bubbleList.appendChild(bubble);
                } else {
                    const group = document.createElement('div');
                    group.className = `message-group animate-fade${msg.is_right ? ' msg-right' : ''}`;
                    group.innerHTML = `
                        <div class="avatar-col">
                            <img class="msg-author-avatar" src="${getAvatarUrl(msg.user_id, msg.avatar_url, msg.platform_name)}" onerror="this.src=getAvatarUrl('fallback')" />
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
                        sessionEl.style.cssText = 'color: var(--text-sub); font-size: 0.75rem; margin-left: 6px; cursor: pointer; text-decoration: underline; opacity: 0.8; transition: opacity 0.2s; display: inline-flex; align-items: center; gap: 4px;';

                        const sPlat = normalizePlatformName(msg.platform_name);
                        const badgeMeta = PLATFORM_BADGE_META[sPlat];
                        let logoHtml = '';
                        let colorClass = '';
                        if (badgeMeta) {
                            logoHtml = badgeMeta.svg;
                            colorClass = badgeMeta.class;
                        } else {
                            logoHtml = FALLBACK_PLATFORM_SVG;
                        }

                        const logoSpan = document.createElement('span');
                        logoSpan.className = `author-session-platform ${colorClass}`;
                        logoSpan.innerHTML = logoHtml;

                        const textSpan = document.createElement('span');
                        textSpan.textContent = `@ ${displaySessionName}`;

                        sessionEl.appendChild(logoSpan);
                        sessionEl.appendChild(textSpan);

                        sessionEl.onmouseover = () => { sessionEl.style.opacity = '1'; };
                        sessionEl.onmouseout = () => { sessionEl.style.opacity = '0.8'; };
                        sessionEl.onclick = (e) => {
                            e.stopPropagation();
                            document.getElementById('searchInput').value = '';
                            selectSession(msg.session_id, msg.session_name || msg.session_id, msg.message_type || '');
                        };
                        group.querySelector('.msg-author').appendChild(sessionEl);
                    }

                    group.querySelector('.msg-bubble-list').appendChild(bubble);
                    fragment.appendChild(group);
                    renderMessageCard = group;
                    renderUserId = msg.user_id;
                    renderSessionId = msg.session_id;
                }
            });

            const hasMore = !(data.has_more === false || data.data.length < limit);

            if (append) {
                const anchorEl = getPrependAnchor(list);
                const anchorTop = anchorEl ? anchorEl.getBoundingClientRect().top : null;
                disablePrependAnimations(fragment);

                // insert after loadMoreWrap
                if (loadMoreEl.nextSibling) {
                    list.insertBefore(fragment, loadMoreEl.nextSibling);
                } else {
                    list.appendChild(fragment);
                }
                loadMoreEl.style.display = hasMore ? 'block' : 'none';
                pruneMessageDom(list, true);
                stabilizePrependAnchor(list, anchorEl, anchorTop);
            } else {
                if (loadMoreEl) loadMoreEl.style.display = hasMore ? 'block' : 'none';
                list.appendChild(fragment);

                // 确保底部有一个永久锚点
                let anchor = document.getElementById('scroll-anchor');
                if (!anchor) {
                    anchor = document.createElement('div');
                    anchor.id = 'scroll-anchor';
                    list.appendChild(anchor);
                } else {
                    list.appendChild(anchor); // 移到最后
                }

                pruneMessageDom(list, false);

                // 立即滚动
                scrollListToBottom(list);
                // 延时一丁点时间再试一次，确保渲染首帧完成
                setTimeout(() => scrollListToBottom(list), 50);
            }
        }
    } catch (e) { console.error(e); }
    finally {
        isHistoryLoading = false;
        if (loadMoreBtn) loadMoreBtn.disabled = false;
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
    const keyword = document.getElementById('searchInput').value.trim();
    if (!activeSessionId && !keyword) {
        showDashboard();
        return;
    }
    currentPage = 1;
    fetchHistory();
}

const viewport = document.getElementById('messageList');
const scrollBtn = document.getElementById('scrollToBottomBtn');

let scrollTicking = false;
let scrollBtnVisible = false;

viewport.addEventListener('scroll', () => {
    if (scrollTicking) return;
    scrollTicking = true;
    requestAnimationFrame(() => {
        const shouldShow = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight > 500;
        if (shouldShow !== scrollBtnVisible) {
            scrollBtnVisible = shouldShow;
            scrollBtn.style.display = shouldShow ? 'flex' : 'none';
        }
        scrollTicking = false;
    });
}, { passive: true });

scrollBtn.onclick = () => {
    viewport.scrollTo({ top: viewport.scrollHeight, behavior: 'smooth' });
};

async function initApp() {
    if (API_KEY) {
        const ok = await ensureAuthCookie();
        if (!ok) return;
    }
    showAuth(false);

    // 移动端侧边栏由 CSS :checked + 全局点击关闭控制

    fetchStats();
    fetchSessions();
    if (!window.statsInterval) {
        window.statsInterval = setInterval(fetchStats, 60000);
    }
}

const loadMoreBtn = document.getElementById('loadMoreBtn');
function handleLoadMore() {
    if (isHistoryLoading) return;
    document.activeElement?.blur();
    closeAllPanels();
    currentPage++;
    fetchHistory(true);
}

loadMoreBtn.addEventListener('pointerdown', (event) => {
    event.preventDefault();
});
loadMoreBtn.addEventListener('mousedown', (event) => {
    event.preventDefault();
});
loadMoreBtn.addEventListener('pointerup', (event) => {
    if (event.pointerType !== 'mouse') {
        event.preventDefault();
        handleLoadMore();
    }
});
loadMoreBtn.onclick = handleLoadMore;

// Time Filters Logic
document.querySelectorAll('.time-btn').forEach(btn => {
    btn.onclick = () => {
        document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

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

        document.getElementById('timeStart').value = '';
        document.getElementById('timeEnd').value = '';
        if (activeSessionId) {
            reloadStats();
            currentPage = 1;
            fetchHistory();
        }
    };
});

function handleCustomTime() {
    document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
    const tStart = document.getElementById('timeStart').value;
    const tEnd = document.getElementById('timeEnd').value;
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
function closeAllPanels() {
    const sidebar = document.querySelector('.sidebar');
    const analysis = document.querySelector('.analysis-panel');
    const overlay = document.getElementById('mobile-overlay');

    if (sidebar) sidebar.classList.remove('open');
    if (analysis) analysis.classList.remove('open');
    if (overlay) overlay.classList.remove('active');
}

window.addEventListener('popstate', () => {
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

    const loginBtn = document.getElementById('login-btn');
    const logoutBtn = document.getElementById('logout-btn');
    const apiKeyInput = document.getElementById('api-key-input');
    const searchInput = document.getElementById('searchInput');
    const btnSidebar = document.getElementById('btn-sidebar');
    const btnAnalysis = document.getElementById('btn-analysis');
    const btnCloseSidebar = document.getElementById('btn-close-sidebar');
    const btnCloseAnalysis = document.getElementById('btn-close-analysis');
    const overlay = document.getElementById('mobile-overlay');
    const sidebar = document.querySelector('.sidebar');
    const analysis = document.querySelector('.analysis-panel');

    if (loginBtn) {
        loginBtn.addEventListener('click', verifyLogin);
    }

    if (logoutBtn) {
        logoutBtn.addEventListener('click', logout);
    }

    if (apiKeyInput) {
        apiKeyInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') verifyLogin();
        });
    }

    if (searchInput) {
        searchInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') handleSearch();
        });
    }

    if (btnSidebar) {
        btnSidebar.addEventListener('click', () => {
            if (sidebar) sidebar.classList.add('open');
            if (overlay) overlay.classList.add('active');
        });
    }

    if (btnAnalysis) {
        btnAnalysis.addEventListener('click', () => {
            if (analysis) analysis.classList.add('open');
            if (overlay) overlay.classList.add('active');
            if (activeSessionId) reloadStats();
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
});
