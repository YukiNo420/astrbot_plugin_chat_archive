const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const mainPath = path.join(__dirname, '..', 'web', 'static', 'js', 'main.js');
const source = fs.readFileSync(mainPath, 'utf8');

function sourceBetween(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.notEqual(start, -1, `missing ${startMarker}`);
    assert.notEqual(end, -1, `missing ${endMarker}`);
    return source.slice(start, end);
}

test('loading older rows preserves the existing boundary row key and measurement identity', () => {
    const existingDate = { type: 'date', key: 'date:today:0', dateStr: 'today' };
    const existingGroup = {
        type: 'group',
        key: 'group:existing',
        userId: '7',
        messages: [{ id: 2 }],
    };
    const prefixDate = { type: 'date', key: 'date:today:0', dateStr: 'today' };
    const prefixGroup = {
        type: 'group',
        key: 'group:prefix',
        userId: '7',
        messages: [{ id: 1 }],
    };
    const context = vm.createContext({
        buildVirtualRows: () => [prefixDate, prefixGroup],
        getDateStr: () => 'today',
    });
    vm.runInContext(
        `${sourceBetween('function prependVirtualRows', 'function renderVirtualRow')}
         this.prependVirtualRowsForTest = prependVirtualRows;`,
        context,
    );

    const rows = context.prependVirtualRowsForTest(
        [{ timestamp: 1 }],
        [existingDate, existingGroup],
        [{ timestamp: 2 }],
    );

    assert.equal(rows.length, 3);
    assert.equal(rows[0], prefixDate);
    assert.equal(rows[1], prefixGroup);
    assert.equal(rows[2], existingGroup);
    assert.equal(rows[2].key, 'group:existing');
});

test('full-message loading requests one record, validates its id, and updates only the active view', async () => {
    const msg = {
        id: 42,
        message: 'short',
        message_length: 20_000,
        message_truncated: 1,
    };
    const row = { type: 'group', key: 'group:42', messages: [msg] };
    const requested = [];
    const invalidatedNodes = [];
    const context = vm.createContext({
        AbortController,
        Number,
        Map,
        encodeURIComponent,
        console: { error() {} },
        activeHistoryViewKey: 'view:one',
        virtualMessages: [msg],
        virtualRows: [row],
        timeline: { invalidate: key => invalidatedNodes.push(key) },
        fullMessageRequests: new Map(),
        safeText: value => value == null ? '' : String(value),
        safeCount: value => Number(value) || 0,
        fetchAPI: async url => {
            requested.push(url);
            return {
                success: true,
                data: [{
                    id: 42,
                    message: '<script>archive text</script>',
                    message_length: 29,
                    message_truncated: 0,
                }],
            };
        },
    });
    vm.runInContext(
        `${sourceBetween('function getVirtualRowForMessage', 'function createFullMessageButton')}
         this.loadFullMessageForTest = loadFullMessage;`,
        context,
    );
    const button = {
        disabled: false,
        isConnected: true,
        setAttribute() {},
        removeAttribute() {},
    };

    await context.loadFullMessageForTest(msg, button);

    assert.deepEqual(requested, ['/api/history?record_id=42&full_message=true&limit=1']);
    assert.equal(msg.message, '<script>archive text</script>');
    assert.equal(msg.message_truncated, 0);
    assert.equal(msg.full_message_loaded, true);
    assert.deepEqual(invalidatedNodes, ['group:42']);
    assert.equal(context.fullMessageRequests.size, 0);
});

test('history reloads and pagination use the committed search term instead of an unsubmitted draft', () => {
    const fetchHistorySource = sourceBetween('async function fetchHistory', 'async function fetchStats');
    const handleSearchSource = sourceBetween('function handleSearch', 'const viewport');

    assert.match(fetchHistorySource, /const keyword = getActiveSearchKeyword\(\);/);
    assert.doesNotMatch(fetchHistorySource, /searchInput\.value/);
    assert.match(handleSearchSource, /activeSearchKeyword = keyword;/);
});

test('server group avatars use the validated media proxy path', () => {
    const serverAvatarBranch = sourceBetween(
        '// Get the server icon URL from the first channel in the server group',
        'const serverHeader = document.createElement',
    );

    assert.match(
        serverAvatarBranch,
        /const serverIconUrl = getMediaResourceUrl\([\s\S]*channels\[0\]\.session\.avatar[\s\S]*\);/,
    );
    assert.match(serverAvatarBranch, /src="\$\{escapeAttr\(serverIconUrl\)\}"/);
});

test('crossing a panel breakpoint closes stale drawers and preserves a focus destination', () => {
    const handler = sourceBetween(
        'let panelsWereMobile',
        "window.addEventListener('resize', handlePanelViewportChange",
    );

    assert.match(handler, /panelsAreMobile !== panelsWereMobile/);
    assert.match(handler, /sidebarWasOrWillBeDrawer/);
    assert.match(handler, /analysisWasOrWillBeDrawer/);
    assert.match(handler, /closeAllPanels\(\{ focusPanel \}\)/);
    assert.match(handler, /panelsWereMobile = panelsAreMobile/);
});

test('long runs from one author remain individually virtualized with stable keys', () => {
    const context = vm.createContext({
        safeText: value => String(value ?? ''),
        safeCount: value => Number(value) || 0,
        getDateStr: value => value < 86400 ? 'day-one' : 'day-two',
    });
    vm.runInContext(
        sourceBetween('function getMessageStableKey', 'function resetVirtualMessages')
        + sourceBetween('function buildVirtualRows', 'function prependVirtualRows'), context,
    );
    const messages = Array.from({length: 500}, (_, index) => ({
        id: index + 1, session_id: 'one', user_id: 'author', timestamp: index * 10,
    }));
    const rows = context.buildVirtualRows(messages);
    assert.equal(rows.length, 501);
    assert.equal(rows[1].continuation, false);
    assert.equal(rows[2].continuation, true);
    assert.equal(rows.at(-1).messages.length, 1);
    const moved = context.buildVirtualRows([{id: -1, session_id: 'one', user_id:'other', timestamp:0}, ...messages]);
    assert.equal(rows.at(-1).key, moved.at(-1).key);
});

test('author grouping resets across time gaps, sessions, dates and system messages', () => {
    const context = vm.createContext({
        safeText: value => String(value ?? ''), safeCount: Number,
        getDateStr: value => value < 86400 ? 'day-one' : 'day-two',
    });
    vm.runInContext(
        sourceBetween('function getMessageStableKey', 'function resetVirtualMessages')
        + sourceBetween('function buildVirtualRows', 'function prependVirtualRows'), context,
    );
    const rows = context.buildVirtualRows([
        { id:1, user_id:'a', session_id:'one', timestamp:1 },
        { id:2, user_id:'a', session_id:'one', timestamp:400 },
        { id:3, user_id:'a', session_id:'two', timestamp:401 },
        { id:4, user_id:'0', session_id:'two', timestamp:402 },
        { id:5, user_id:'a', session_id:'two', timestamp:403 },
        { id:6, user_id:'a', session_id:'two', timestamp:86400 },
    ]);
    assert.equal(rows.filter(row => row.continuation).length, 0);
    assert.equal(rows.filter(row => row.type === 'date').length, 2);
});
