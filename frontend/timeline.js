import { Virtualizer, observeElementRect, observeElementOffset, elementScroll } from '@tanstack/virtual-core';

// The DOM adapter owns mounting only. TanStack owns ranges, measurements,
// prepend anchoring, media resize compensation and scrolling to an item.
class ArchiveTimeline {
    constructor(viewport, renderRow, { search = false } = {}) {
        this.viewport = viewport;
        this.renderRow = renderRow;
        this.rows = [];
        this.nodes = new Map();
        this.destroyed = false;
        this.rendering = false;
        this.frame = 0;
        this.container = document.createElement('div');
        this.container.id = 'virtualRows';
        this.window = document.createElement('div');
        this.window.className = 'timeline-window';
        this.container.append(this.window);
        viewport.append(this.container);
        viewport.classList.add('timeline-active');
        this.virtualizer = new Virtualizer({
            count: 0,
            getScrollElement: () => viewport,
            estimateSize: index => this.rows[index]?.type === 'date' ? 44 : 110,
            getItemKey: index => index,
            observeElementRect: (instance, callback) => observeElementRect(instance, rect => {
                const previousHeight = instance.scrollRect?.height;
                const previousOffset = instance.scrollOffset || 0;
                const previousEnd = instance.getTotalSize() + instance.options.scrollMargin - previousHeight;
                const pinned = !search && previousHeight > 0
                    && Math.abs(Math.max(0, previousEnd) - previousOffset) <= 3;
                // Reserve space only at the start of history, below the floating controls.
                const headerHeight = viewport.parentElement.querySelector('.content-header')?.offsetHeight || 0;
                viewport.style.setProperty('--search-overlay-height', `${headerHeight}px`);
                const scrollMargin = document.getElementById('loadMoreWrap')?.offsetHeight || 0;
                const marginDelta = scrollMargin - instance.options.scrollMargin;
                const paddingEnd = parseFloat(getComputedStyle(viewport).getPropertyValue('--timeline-end-gap')) || 0;
                if (paddingEnd !== instance.options.paddingEnd || marginDelta) {
                    instance.setOptions({ ...instance.options, paddingEnd, scrollMargin });
                }
                callback(rect);
                if (pinned && instance.options.count) instance.scrollToEnd();
                else if (previousHeight > 0 && marginDelta && previousOffset > 0) {
                    instance.scrollToOffset(Math.max(0, previousOffset + marginDelta));
                }
            }),
            observeElementOffset,
            // Preserve fractional row heights so a flow window does not drift
            // past the virtual extent after many consecutive messages.
            measureElement: (element, entry) => entry?.borderBoxSize?.[0]?.blockSize ?? element.getBoundingClientRect().height,
            scrollToFn: (offset, options, instance) => {
                // Commit the new extent before the browser clamps a size-change
                // adjustment against its scrollHeight (including batched images).
                this.container.style.height = `${instance.getTotalSize()}px`;
                elementScroll(offset, options, instance);
            },
            // Native end padding scrolls away with the last record.
            paddingEnd: parseFloat(getComputedStyle(viewport).getPropertyValue('--timeline-end-gap')) || 0,
            scrollMargin: document.getElementById('loadMoreWrap')?.offsetHeight || 0,
            anchorTo: 'end',
            followOnAppend: !search,
            scrollEndThreshold: search ? -1 : 2,
            overscan: 12,
            onChange: () => this.render(),
        });
        // Preserve the next message when a partly clipped media row grows.
        // Searches retain their reading anchor even at the end of the results.
        this.virtualizer.shouldAdjustScrollPositionOnItemSizeChange = (item, delta, instance) => (
            item.start < (instance.scrollOffset || 0) + instance.scrollAdjustments
        );
        this.cleanup = this.virtualizer._didMount();
        this.virtualizer._willUpdate();
    }

    setRows(rows, { initial = false, search = false } = {}) {
        const keys = new Set(rows.map(row => row.key));
        const offset = this.virtualizer.scrollOffset || 0;
        const anchor = this.virtualizer.getVirtualItemForOffset(offset);
        // Prepending may merge away the visible date divider. Give the native
        // scroller the next surviving row when its original anchor was removed.
        const replacement = anchor && !keys.has(anchor.key)
            ? this.virtualizer.getVirtualItems().find(item => item.end > offset && keys.has(item.key))
            : null;
        this.rows = rows;
        for (const key of this.nodes.keys()) {
            if (!keys.has(key)) this.nodes.delete(key);
        }
        this.virtualizer.setOptions({
            ...this.virtualizer.options,
            count: rows.length,
            getItemKey: index => rows[index].key,
        });
        this.render();
        if (replacement) {
            const target = this.virtualizer.getMeasurements().find(item => item.key === replacement.key);
            if (target) this.virtualizer.scrollToOffset(target.start + offset - replacement.start);
        }
        if (initial && rows.length) {
            if (search) this.virtualizer.scrollToOffset(0);
            else this.virtualizer.scrollToEnd();
        }
    }

    render() {
        if (this.destroyed) return;
        if (this.rendering) {
            this.schedule();
            return;
        }
        this.rendering = true;
        const items = this.virtualizer.getVirtualItems();
        this.container.style.height = `${this.virtualizer.getTotalSize()}px`;
        this.window.style.transform = `translateY(${(items[0]?.start || 0) - this.virtualizer.options.scrollMargin}px)`;
        const wanted = new Set(items.map(item => String(item.key)));
        for (const node of [...this.window.children]) {
            if (!wanted.has(node.dataset.vkey) || this.nodes.get(node.dataset.vkey) !== node) node.remove();
        }
        let cursor = this.window.firstElementChild;
        for (const item of items) {
            let node = this.nodes.get(item.key);
            if (!node) {
                node = this.renderRow(this.rows[item.index]);
                this.nodes.set(item.key, node);
            }
            node.dataset.index = String(item.index);
            node.classList.toggle('message-continuation', !!this.rows[item.index].continuation);
            if (node === cursor) cursor = cursor.nextElementSibling;
            else this.window.insertBefore(node, cursor);
            this.virtualizer.measureElement(node);
        }
        this.container.style.height = `${this.virtualizer.getTotalSize()}px`;
        // Disconnected media rows stay cached briefly, but DOM memory is bounded.
        for (const [key, node] of this.nodes) {
            if (this.nodes.size <= 160) break;
            if (!node.isConnected) this.nodes.delete(key);
        }
        this.virtualizer.measureElement(null);
        this.virtualizer._willUpdate();
        this.rendering = false;
    }

    schedule() {
        if (this.destroyed || this.frame) return;
        this.frame = requestAnimationFrame(() => {
            this.frame = 0;
            this.render();
        });
    }

    invalidate(key) {
        this.nodes.delete(key);
        this.render();
    }

    scrollToIndex(index) {
        this.virtualizer.scrollToIndex(index, { align: 'center' });
        this.render();
    }

    scrollToEnd() {
        this.virtualizer.scrollToEnd();
    }

    destroy() {
        this.destroyed = true;
        cancelAnimationFrame(this.frame);
        this.cleanup();
        this.nodes.clear();
        this.container.remove();
        this.viewport.classList.remove('timeline-active');
        this.viewport.style.removeProperty('--search-overlay-height');
    }
}

window.ArchiveTimeline = ArchiveTimeline;
