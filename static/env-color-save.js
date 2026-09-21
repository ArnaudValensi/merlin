/* The environment color save queue, pure state with injected effects, so
   node can test it (tests/js/env-color-save.test.js) without a browser.

   A swatch row is a choice, not a form: a click saves at once. Two rules
   keep the page honest about what is saved:
   - one save in flight at a time, and the latest click wins: a click during
     a save is queued and sent once the save returns, so requests never race
     each other to config.env and responses never arrive out of order.
   - the selection shown is the confirmed one: a save that fails puts the
     last confirmed color back and says so, and a response only applies
     when no newer choice is waiting. */
const MerlinEnvColorSave = (() => {
    'use strict';

    // opts: {
    //   confirmed: {name, hex}      the color the page was rendered with
    //   save(name) -> Promise<{name, hex}>   rejects with an Error on failure
    //   onSelect(name)              highlight a swatch (optimistic or confirmed)
    //   onApply(name, hex)          paint the confirmed color on the page
    //   onSaved()                   the acknowledgement
    //   onError(message)            the visible failure
    // }
    function createEnvColorSaver(opts) {
        let confirmed = opts.confirmed;
        let pending = null;     // the latest choice not yet sent
        let draining = null;    // the promise of the running queue

        async function drain() {
            while (pending !== null) {
                const name = pending;
                pending = null;
                try {
                    const result = await opts.save(name);
                    confirmed = { name: result.name, hex: result.hex };
                    if (pending === null) {
                        opts.onSelect(confirmed.name);
                        opts.onApply(confirmed.name, confirmed.hex);
                        opts.onSaved();
                    }
                } catch (e) {
                    if (pending === null) {
                        // Both the swatch and the painted identity go back to
                        // the confirmed color: a save that succeeded while a
                        // newer choice waited was never painted.
                        opts.onSelect(confirmed.name);
                        opts.onApply(confirmed.name, confirmed.hex);
                        opts.onError((e && e.message) || 'Save failed');
                    }
                }
            }
            draining = null;
        }

        // Returns a promise that settles once the queue is empty.
        function choose(name) {
            opts.onSelect(name);
            pending = name;
            if (draining === null) draining = drain();
            return draining;
        }

        return {
            choose: choose,
            confirmed: () => confirmed,
            busy: () => draining !== null,
        };
    }

    return { createEnvColorSaver: createEnvColorSaver };
})();

/* Node (tests/js/env-color-save.test.js) requires the file directly. Browsers
   get MerlinEnvColorSave as a plain script-scope const. */
if (typeof module !== 'undefined' && module.exports) module.exports = MerlinEnvColorSave;
