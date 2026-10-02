// Run after the synthetic archive fixture with touch enabled in the browser.
async (page) => {
    const checks = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => {
        closeAllPanels();
        window.__drawerCalls = [];
        const original = reloadStats;
        reloadStats = (...args) => {
            __drawerCalls.push({duringSlide: document.getElementById('analysisPanel').getAnimations().some(a => a.playState === 'running')});
            return original(...args);
        };
    });
    await page.waitForTimeout(220);
    for (const id of ['sidebar', 'analysis']) {
        await page.locator('#btn-'+id).tap();
        await page.waitForTimeout(300);
        checks.push(await page.evaluate(id => {
            const button = document.getElementById('btn-'+id);
            const panel = document.getElementById(id === 'sidebar' ? 'sessionSidebar' : 'analysisPanel');
            const style = getComputedStyle(button);
            return {name: id+' touch opens without highlight or blur', pass: style.webkitTapHighlightColor === 'rgba(0, 0, 0, 0)'
                && style.transitionDuration === '0s' && style.transform === 'none'
                && panel.classList.contains('open') && !panel.inert && document.querySelector('.main-container').inert
                && getComputedStyle(panel).backdropFilter === 'none'
                && getComputedStyle(document.getElementById('mobile-overlay')).backdropFilter === 'none'};
        }, id));
        await page.locator('#mobile-overlay').tap({position: id === 'sidebar' ? {x: 380, y: 400} : {x: 4, y: 400}});
        await page.waitForTimeout(220);
        checks.push(await page.evaluate(id => ({name: id+' backdrop closes and restores focus',
            pass: !document.querySelector('.main-container').inert && document.activeElement === document.getElementById('btn-'+id)}), id));
    }
    checks.push(await page.evaluate(() => ({name: 'statistics wait for slide completion',
        pass: __drawerCalls.length === 1 && __drawerCalls.every(call => !call.duringSlide)})));
    await page.evaluate(() => {__drawerCalls = []; document.getElementById('btn-analysis').click(); closeAllPanels();});
    await page.waitForTimeout(300);
    checks.push(await page.evaluate(() => ({name: 'quick close cancels delayed refresh', pass: __drawerCalls.length === 0})));
    await page.locator('#btn-analysis').focus();
    await page.keyboard.press('Enter');
    await page.waitForTimeout(260);
    checks.push(await page.evaluate(() => ({name: 'keyboard focus remains visible inside drawer',
        pass: document.activeElement === document.getElementById('btn-close-analysis') && document.activeElement.matches(':focus-visible')
            && getComputedStyle(document.activeElement).outlineStyle !== 'none'})));
    await page.keyboard.press('Shift+Tab');
    checks.push(await page.evaluate(() => ({name: 'Tab remains inside drawer', pass: document.getElementById('analysisPanel').contains(document.activeElement)})));
    await page.keyboard.press('Escape');
    await page.setViewportSize({width: 1440, height: 960});
    await page.locator('#btn-analysis').click();
    await page.waitForTimeout(260);
    checks.push({name: 'desktop backdrop is visible', pass: await page.locator('#mobile-overlay').isVisible()});
    await page.locator('#mobile-overlay').click({position: {x: 400, y: 400}});
    await page.emulateMedia({reducedMotion: 'reduce'});
    await page.evaluate(() => {__drawerCalls = [];});
    await page.locator('#btn-analysis').click();
    await page.waitForTimeout(150);
    checks.push(await page.evaluate(() => ({name: 'reduced motion still refreshes statistics',
        pass: __drawerCalls.length === 1 && !__drawerCalls[0].duringSlide
            && getComputedStyle(document.getElementById('analysisPanel')).transitionDuration === '0s'})));
    await page.keyboard.press('Escape');
    await page.emulateMedia({reducedMotion: 'no-preference'});
    checks.push({name: 'no browser errors', pass: errors.length === 0, errors});
    const failed = checks.filter(check => !check.pass);
    if (failed.length) throw new Error(JSON.stringify(failed));
    return {passed: checks.length, checks};
}
