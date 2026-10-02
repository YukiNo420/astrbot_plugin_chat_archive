# Message timeline

The existing HTML/CSS message renderer uses **@tanstack/virtual-core 3.17.11**
(MIT) for dynamic measurement, range selection, scroll reconciliation and
end anchoring. No browser CDN or framework runtime is required.

Sources: https://github.com/TanStack/virtual and
https://tanstack.com/virtual/latest/docs/framework/react/examples/chat

Statistics and member lists use **morphdom 2.7.8** (MIT) to retain avatar nodes
when counts, names or rankings change. Session/member keys identify rows;
statistics refreshes preserve the member list until its own response arrives.
Source: https://github.com/patrick-steele-idem/morphdom

Rebuild the checked-in `web/static/js/timeline.js` and `web/static/js/dom.js`:

```sh
cd frontend
npm ci
npm run build
```

Keep the dependency pinned: the small DOM adapter uses `_didMount` and
`_willUpdate`, as the official framework adapters do. Verify prepend, dynamic
media resize, initial end positioning, searching, viewport resizing and
destroying a view when updating the dependency. `timeline.css` adds layout
rules; the existing `main.css` remains the visual theme.

Every archive message has a persistent row key. Consecutive messages from the
same author within five minutes hide repeated author chrome, while retaining
separate measured rows. Media DOM nodes remain mounted while visible. A
bounded detached-node cache preserves recently viewed media without keeping
the entire archive in the DOM.

Browser regressions use synthetic API responses and images. With the candidate
frontend open in a Playwright CLI session, run these files in order:

```sh
mkdir -p output/playwright/timeline
playwright-cli run-code --filename=tests/frontend_timeline.browser.js
playwright-cli run-code --filename=tests/frontend_timeline_controls.browser.js
playwright-cli run-code --filename=tests/frontend_media_timeline.browser.js
```

The checks cover 2,000-row scrolling, stable prepends, node reuse, failed
refreshes, drawers and focus, search/settings races, delayed images, failed
images, and mobile resizing. They do not modify archive data. Close this
browser session afterward to remove its test routes.
