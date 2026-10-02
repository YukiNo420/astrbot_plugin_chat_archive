// Optional management UI for installations providing /api/manage endpoints.
// Each mounted settings page owns its requests and confirmation scope.
let managementView = null;

function setManagementBusy(view, busy) {
    view.busy = busy;
    view.root.querySelector('#manage-session-select').disabled = busy;
    view.root.querySelector('#manage-mode').disabled = busy || !view.sessionId;
    view.root.querySelector('#manage-start').disabled = busy || !view.sessionId || view.messageId > 0;
    view.root.querySelector('#manage-end').disabled = busy || !view.sessionId || view.messageId > 0;
    view.root.querySelector('#manage-confirm').disabled = busy || !view.preview?.count;
    view.root.querySelector('#manage-export').disabled = busy || !view.preview?.matched_count;
    const mode = view.root.querySelector('#manage-mode').value;
    view.root.querySelector('#manage-confirm').textContent = view.preview?.count
        ? `${mode === 'permanent' ? '永久删除' : '移入回收站'} ${view.preview.count.toLocaleString()} 条` : '确认处理';
    view.root.querySelectorAll('#manage-trash button').forEach(button => { button.disabled = busy; });
}

function clearManagementPreview(view) {
    clearTimeout(view.previewTimer);
    clearTimeout(view.expiryTimer);
    view.previewController?.abort();
    view.previewSeq += 1;
    view.preview = null;
    view.root.querySelector('#manage-confirm').disabled = true;
    view.root.querySelector('#manage-export').disabled = true;
}

async function managementRequest(path, body = null, { signal, blob = false } = {}) {
    const response = await fetch(path, {
        method: body ? 'POST' : 'GET', credentials: 'same-origin', signal,
        headers: { 'Content-Type': 'application/json' },
        ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) {
        const data = await response.json();
        if (response.status === 401) showAuth(true);
        const error = new Error(typeof data.detail === 'string' ? data.detail : '操作失败，请重试。');
        error.status = response.status;
        throw error;
    }
    return blob ? response.blob() : response.json();
}

function openMessageManagement(sessionId, messageId = 0) {
    showSettings({ section: 'messages', manageSessionId: sessionId, messageId });
}

function setupMessageManagement({ sessionId = '', messageId = 0 } = {}) {
    const root = document.getElementById('management-page');
    const select = root.querySelector('#manage-session-select');
    if (managementView) clearManagementPreview(managementView);
    const view = { root, sessionId, messageId, preview: null, busy: false, readSeq: 0, previewSeq: 0, previewExpires: 0 };
    managementView = view;
    for (const session of rawSessions) {
        const platform = PLATFORM_META[getSessionPlatform(session)]?.name || '';
        select.add(new Option(`${session.name || session.session_id}${platform ? ` · ${platform}` : ''}`, session.session_id));
    }
    if (sessionId && ![...select.options].some(option => option.value === sessionId)) {
        select.add(new Option(sessionsById.get(sessionId)?.name || sessionId, sessionId));
    }
    select.value = sessionId;
    const selectScope = () => {
        view.sessionId = select.value;
        view.lastAction = '';
        clearManagementPreview(view);
        root.querySelector('#manage-start').value = '';
        root.querySelector('#manage-end').value = '';
        root.querySelector('#manage-mode').value = 'trash';
        root.querySelector('#manage-status').textContent = view.sessionId ? '正在预览…' : '请选择对话';
        root.querySelector('#manage-trash').textContent = view.sessionId ? '正在加载…' : '选择对话后查看回收站。';
        window.history.replaceState({ ...window.history.state, managementSessionId: view.sessionId, managementMessageId: view.messageId }, '', window.location.href);
        setManagementBusy(view, false);
        if (view.sessionId) { loadManagementTrash(view); previewMessageManagement(view); }
    };
    select.addEventListener('change', () => { view.messageId = 0; selectScope(); });
    for (const id of ['manage-start', 'manage-end']) {
        root.querySelector(`#${id}`).addEventListener('input', () => previewMessageManagement(view));
    }
    root.querySelector('#manage-mode').addEventListener('change', () => setManagementBusy(view, view.busy));
    root.querySelector('#manage-confirm').addEventListener('click', () => confirmMessageManagement(view));
    root.querySelector('#manage-export').addEventListener('click', () => exportManagementPreview(view));
    selectScope();
}

async function loadManagementTrash(view) {
    const sessionId = view.sessionId;
    const requestSeq = ++view.readSeq;
    try {
        const [data, storage] = await Promise.all([
            managementRequest(`/api/manage/trash?session_id=${encodeURIComponent(sessionId)}`),
            managementRequest(`/api/manage/storage?session_id=${encodeURIComponent(sessionId)}`),
        ]);
        if (!view.root.isConnected || requestSeq !== view.readSeq || sessionId !== view.sessionId) return;
        const root = view.root.querySelector('#manage-trash');
        root.replaceChildren();
        const summary = document.createElement('p');
        summary.className = 'settings-description';
        summary.textContent = `会话 ${safeCount(storage.active.count).toLocaleString()} 条 · 回收站 ${safeCount(storage.trash.count).toLocaleString()} 条`;
        root.append(summary);
        for (const operation of data.operations) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'secondary-btn';
            button.disabled = view.busy;
            button.textContent = `恢复 ${operation.count} 条 · ${formatTime(operation.created_at)}`;
            button.addEventListener('click', async () => {
                if (view.busy || !window.confirm(`恢复这 ${operation.count} 条消息？`)) return;
                setManagementBusy(view, true);
                try {
                    const result = await managementRequest('/api/manage/restore', { session_id: sessionId, operation_id: operation.operation_id });
                    clearManagementPreview(view);
                    view.lastAction = `已恢复 ${result.count.toLocaleString()} 条`;
                    view.root.querySelector('#manage-status').textContent = view.lastAction;
                    await refreshAfterManagement(view);
                } catch (error) { view.root.querySelector('#manage-status').textContent = error.message; }
                finally { setManagementBusy(view, false); previewMessageManagement(view, 0); }
            });
            root.append(button);
        }
    } catch (error) {
        if (view.root.isConnected && requestSeq === view.readSeq && sessionId === view.sessionId) view.root.querySelector('#manage-trash').textContent = error.message;
    }
}

async function refreshAfterManagement(view) {
    await fetchSessions({ selectView: false });
    fetchStats();
    if (view.root.isConnected) { await loadManagementTrash(view); previewMessageManagement(view); }
    else if (activeSessionId === view.sessionId && !document.body.classList.contains('settings-view')) {
        await fetchHistory();
        reloadStats();
    }
}

function previewMessageManagement(view, delay = 450) {
    clearManagementPreview(view);
    if (view.busy || !view.sessionId || !view.root.isConnected || managementView !== view) return;
    const start = view.root.querySelector('#manage-start');
    const end = view.root.querySelector('#manage-end');
    const startTs = start.value ? Math.floor(new Date(start.value).getTime() / 1000) : 0;
    const endTs = end.value ? Math.floor(new Date(end.value).getTime() / 1000) : 0;
    const status = view.root.querySelector('#manage-status');
    if (!start.validity.valid || !end.validity.valid || !Number.isFinite(startTs) || !Number.isFinite(endTs)) {
        status.textContent = '请输入有效时间';
        return;
    }
    if (startTs && endTs && startTs > endTs) {
        status.textContent = '开始时间不能晚于结束时间';
        return;
    }
    const sequence = view.previewSeq;
    status.textContent = '正在预览…';
    view.previewTimer = setTimeout(async () => {
        if (!view.root.isConnected || managementView !== view || sequence !== view.previewSeq) return;
        view.previewController = new AbortController();
        try {
            const preview = await managementRequest('/api/manage/preview', {
                session_id: view.sessionId, message_id: view.messageId, start_ts: startTs, end_ts: endTs,
            }, { signal: view.previewController.signal });
            if (!view.root.isConnected || managementView !== view || sequence !== view.previewSeq) return;
            view.preview = preview;
            view.previewExpires = Date.now() + preview.expires_in * 1000;
            const summary = preview.matched_count ? `匹配 ${preview.matched_count.toLocaleString()} 条` : '没有匹配消息';
            status.textContent = view.lastAction ? `${view.lastAction}；${summary}` : summary;
            view.lastAction = '';
            setManagementBusy(view, view.busy);
            if (preview.preview_token) {
                view.expiryTimer = setTimeout(() => previewMessageManagement(view, 0), Math.max(1000, preview.expires_in * 1000 - 5000));
            }
        } catch (error) {
            if (error.name !== 'AbortError' && view.root.isConnected && managementView === view && sequence === view.previewSeq) {
                status.textContent = error.message;
            }
        }
    }, delay);
}

async function confirmMessageManagement(view) {
    if (view.busy || !view.preview?.count) return;
    if (Date.now() >= view.previewExpires) { previewMessageManagement(view, 0); return; }
    const preview = view.preview;
    const mode = view.root.querySelector('#manage-mode').value;
    const name = view.root.querySelector('#manage-session-select').selectedOptions[0]?.textContent || '所选对话';
    const start = view.root.querySelector('#manage-start').value.replace('T', ' ') || '不限开始';
    const end = view.root.querySelector('#manage-end').value.replace('T', ' ') || '不限结束';
    const scope = view.messageId ? '仅所选消息' : `${start} 至 ${end}`;
    const action = mode === 'permanent' ? '永久删除' : '移入回收站';
    const confirmation = `${name}\n${scope}\n${action}全部 ${preview.count.toLocaleString()} 条消息？`;
    const permanent = mode === 'permanent' ? window.prompt(`${confirmation}\n无法恢复。请输入“永久删除”确认`) : '';
    if (mode === 'permanent' ? permanent !== '永久删除' : !window.confirm(confirmation)) return;
    setManagementBusy(view, true);
    clearManagementPreview(view);
    view.root.querySelector('#manage-status').textContent = `正在${action}…`;
    try {
        const result = await managementRequest('/api/manage/delete', {
            preview_token: preview.preview_token, confirm_session_id: preview.session_id,
            delete_mode: mode, confirm_permanent: permanent, confirm_count: preview.count,
        });
        view.lastAction = `已${action} ${result.count.toLocaleString()} 条`;
        view.root.querySelector('#manage-status').textContent = view.lastAction;
        await refreshAfterManagement(view);
    } catch (error) { view.lastAction = error.message; view.root.querySelector('#manage-status').textContent = error.message; }
    finally { setManagementBusy(view, false); previewMessageManagement(view, 0); }
}

async function exportManagementPreview(view) {
    if (view.busy || !view.preview?.matched_count) return;
    if (Date.now() >= view.previewExpires) { previewMessageManagement(view, 0); return; }
    setManagementBusy(view, true);
    view.root.querySelector('#manage-status').textContent = '正在导出…';
    try {
        const data = await managementRequest('/api/manage/export', { preview_token: view.preview.preview_token }, { blob: true });
        const url = URL.createObjectURL(data);
        const link = document.createElement('a');
        link.href = url;
        link.download = 'chat-archive-messages.json';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        view.root.querySelector('#manage-status').textContent = '已导出所选范围（不含附件）';
    } catch (error) {
        view.root.querySelector('#manage-status').textContent = error.message;
        if (error.status === 409) clearManagementPreview(view);
    }
    finally {
        setManagementBusy(view, false);
        if (!view.preview || Date.now() >= view.previewExpires) previewMessageManagement(view, 0);
    }
}
