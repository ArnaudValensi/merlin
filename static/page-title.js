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

    const set = (app, context, providedDocument) => {
        const doc = providedDocument || (
            typeof document !== 'undefined' ? document : null
        );
        if (!doc) return format('', app, context);
        const root = doc.documentElement;
        const machine = root && root.dataset ? root.dataset.machineName : '';
        const title = format(machine, app, context);
        doc.title = title;
        return title;
    };

    return { clean, format, pathContext, tmuxContext, set };
})();

if (typeof globalThis !== 'undefined') {
    globalThis.MerlinPageTitle = MerlinPageTitle;
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = MerlinPageTitle;
}
