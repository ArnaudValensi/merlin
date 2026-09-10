/* Shared machine-first browser title formatting. */

const MerlinPageTitle = (() => {
    'use strict';

    const clean = (value) => {
        if (value === undefined || value === null) return '';
        return String(value)
            .replace(/[\u0000-\u001f\u007f]/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    };

    const format = (machine, app, context) => {
        const machinePart = clean(machine);
        const appPart = clean(app).toLowerCase();
        const contextPart = clean(context);
        const pagePart = appPart
            ? appPart + (contextPart ? ': ' + contextPart : '')
            : contextPart;

        if (machinePart && pagePart) return machinePart + ' · ' + pagePart;
        return machinePart || pagePart || 'Merlin';
    };

    const pathContext = (path) => {
        const value = clean(path);
        if (!value || value === '/') return value || '';
        const withoutTrailingSlash = value.replace(/\/+$/, '');
        const parts = withoutTrailingSlash.split('/');
        return clean(parts[parts.length - 1]);
    };

    const tmuxContext = (session, windowName) => {
        const sessionPart = clean(session);
        const windowPart = clean(windowName);
        if (sessionPart && windowPart) return sessionPart + '/' + windowPart;
        return sessionPart || windowPart;
    };

    // Attention count, shown as a "(n) " prefix. Kept here so a later set()
    // (a session switch re-titling the terminal) never loses it, and so no
    // page ever writes document.title on its own.
    let count = 0;
    let last = null;

    const withCount = (title, n) => (n > 0 ? '(' + n + ') ' + title : title);

    const set = (app, context, providedDocument) => {
        const doc = providedDocument || (
            typeof document !== 'undefined' ? document : null
        );
        if (!doc) return withCount(format('', app, context), count);
        const root = doc.documentElement;
        const machine = root && root.dataset ? root.dataset.machineName : '';
        const title = withCount(format(machine, app, context), count);
        doc.title = title;
        last = { app, context, doc };
        return title;
    };

    // Change the count and re-apply the last title. A page that has not set a
    // title yet gets the prefix on its server-rendered one.
    const setCount = (n, providedDocument) => {
        const value = Number(n);
        count = Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
        if (last) return set(last.app, last.context, last.doc);
        const doc = providedDocument || (
            typeof document !== 'undefined' ? document : null
        );
        if (!doc) return '';
        const base = clean(doc.title).replace(/^\(\d+\) /, '');
        doc.title = withCount(base, count);
        return doc.title;
    };

    return { clean, format, pathContext, tmuxContext, set, setCount, withCount };
})();

if (typeof globalThis !== 'undefined') {
    globalThis.MerlinPageTitle = MerlinPageTitle;
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = MerlinPageTitle;
}
