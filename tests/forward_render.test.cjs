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
const command = show => `<qqbot-cmd-input text="/%E7%BB%91%E5%AE%9A%20" show="${show}" reference="false" />`;
assert.equal(context.formatMsg(command('%E7%BB%91%E5%AE%9A')), '绑定');
assert.equal(context.formatMsg('before ' + command('ow%20%E8%B5%9B%E4%BA%8B') + ' after'), 'before ow 赛事 after');
assert.equal(context.formatMsg(command('%ZZ')), '%ZZ');
assert.match(context.formatMsg('URL https://example.com/%E7%BB%91%E5%AE%9A'), /%E7%BB%91%E5%AE%9A/);
assert.doesNotMatch(context.formatMsg(command('%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E')), /<img/);
assert.doesNotMatch(context.formatMsg(command('%5BCQ%3Aimage%2Curl%3Dhttps%3A%2F%2Fexample.com%2Fx%5D')), /<img/);
assert.match(context.formatMsg('<qqbot-cmd-input text="/command" />'), /qqbot-cmd-input/);
console.log('QQ command labels decode safely without executing tags, CQ codes or commands.');

for (const type of ['image', 'video', 'record', 'file']) {
    const rendered = context.formatMsg(`[CQ:${type},name=a&amp;b.txt,url=https://example.com/media?a=1&amp;b=2&#44;3&#91;4&#93;]`);
    assert.match(rendered, /https:\/\/example.com\/media\?a=1&amp;b=2,3\[4\]/);
    assert.doesNotMatch(rendered, /amp;amp|amp;#44|amp;#91/);
}
assert.match(context.formatMsg('[CQ:image,url=https://example.com/x?literal=&amp;amp;]'), /literal=&amp;amp;/);
assert.match(context.formatMsg('[CQ:image,url=/static/cache/synthetic.png,width=64,height=32]'), /src="\/static\/cache\/synthetic.png"/);
assert.match(context.formatMsg('[合并转发]\n1. sender: [CQ:image,url=/static/cache/synthetic.png]\n[合并转发结束]'), /msg-image/);
assert.doesNotMatch(context.formatMsg('[CQ:image,url=javascript:alert(1)]'), /<img/);
console.log('Media URLs preserve query separators, CQ escaping, literal entities and cached paths.');
