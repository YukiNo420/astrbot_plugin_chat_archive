const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const escape = text => String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const context = {
    window: { addEventListener() {} },
    localStorage: { removeItem() {} },
    document: {
        addEventListener() {},
        getElementById() { return { addEventListener() {} }; },
        querySelectorAll() { return []; },
        createElement() { return { set textContent(text) { this.innerHTML = escape(text); } }; },
    },
    console, URLSearchParams, setTimeout() {}, setInterval() {},
    ResizeObserver: class { observe() {} },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/static/js/main.js', 'utf8'), context);
const nested = '[合并转发]\n1. outer: [合并转发,id=child]\n    1. inner: hello\n        2. ordinary: text\n    [合并转发结束]\n2. sibling: [CQ:file,name=notes.txt,url=https://example.com/f]\n[合并转发结束]';
const html = context.formatMsg(nested);
assert.equal((html.match(/class="msg-forward-container"/g) || []).length, 2);
assert.equal((html.match(/class="msg-forward-item"/g) || []).length, 3);
assert.match(html, /ordinary: text/);
assert.match(html, /href="https:\/\/example.com\/f"/);
assert.doesNotMatch(html, /合并转发结束/);
const siblings = context.formatMsg(nested + 'between' + nested + 'after');
assert.equal((siblings.match(/class="msg-forward-container"/g) || []).length, 4);
assert.match(siblings, /between/);
assert.match(siblings, /after/);
assert.match(context.formatMsg('[合并转发]\n1. legacy: old'), /legacy/);
assert.doesNotMatch(context.formatMsg('[CQ:file,name=x,url=javascript:alert(1)]'), /href=/);
assert.doesNotMatch(context.formatMsg('[合并转发]\n1. <script>: <img src=x>\n[合并转发结束]'), /<script>|<img src=x>/);
console.log('Forward rendering, boundaries, legacy compatibility and URL safety passed.');
