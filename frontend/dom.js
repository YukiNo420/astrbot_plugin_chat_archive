/*! morphdom 2.7.8
The MIT License (MIT)

Copyright (c) Patrick Steele-Idem <pnidem@gmail.com> (psteeleidem.com)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
*/
import morphdom from 'morphdom';

// Reconcile the three member/statistics views without replacing decoded images.
window.ArchiveDOM = {
    updateChildren(root, content, { preserveIds = [] } = {}) {
        const target = root.cloneNode(false);
        if (typeof content === 'string') target.innerHTML = content;
        else target.append(content);
        morphdom(root, target, {
            childrenOnly: true,
            getNodeKey: node => node.id || node.dataset?.domKey || node.dataset?.odId,
            onBeforeElUpdated: (from, to) => !preserveIds.includes(from.id) && !from.isEqualNode(to),
        });
    },
};
