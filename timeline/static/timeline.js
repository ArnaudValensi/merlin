(function () {
    'use strict';

    var app = document.getElementById('timeline-app');
    if (!app) return;

    var canvas = document.getElementById('timeline-canvas');
    var stage = document.getElementById('timeline-stage');
    var scroll = document.getElementById('timeline-scroll');
    var statePanel = document.getElementById('timeline-state');
    var detail = document.getElementById('timeline-detail');
    var detailGrid = document.getElementById('timeline-detail-grid');
    var detailTitle = document.getElementById('timeline-detail-title');
    var detailKicker = document.getElementById('timeline-detail-kicker');
    var detailStatus = document.getElementById('timeline-detail-status');
    var related = document.getElementById('timeline-detail-related');
    var scrim = document.getElementById('timeline-scrim');
    var visibleCount = document.getElementById('timeline-visible');
    var liveButton = document.getElementById('timeline-live');
    var connection = document.getElementById('timeline-connection');
    var captureButton = document.getElementById('timeline-capture');
    var captureMode = document.getElementById('timeline-capture-mode');
    var consentPanel = document.getElementById('timeline-consent');
    var consentStatus = document.getElementById('timeline-consent-status');
    var anomaly = document.getElementById('timeline-anomaly');
    var minimapWindow = document.getElementById('timeline-minimap-window');
    var OPEN_REBASELINE_POLLS = 20;
    var params = new URLSearchParams(window.location.search);
    var requestedRange = Number(params.get('range'));
    if ([15, 60, 240].indexOf(requestedRange) === -1) requestedRange = 60;
    var state = {
        data: null,
        grouping: params.get('group') === 'activity' ? 'activity' : 'participants',
        zoom: clamp(Number(params.get('zoom')) || 1, 0.7, 2.4),
        selected: params.get('selected'),
        filtered: params.get('filter') === 'active',
        live: params.get('mode') !== 'frozen',
        rangeMinutes: requestedRange,
        cursor: null,
        pending: [],
        frozenAt: null,
        pollTimer: null,
        inFlight: false,
        generation: 0,
        canvasWidth: 1040,
        scenario: params.get('state'),
        orderedIds: [],
        openPolls: 0,
        agentSlots: {},
        nextAgentSlot: 0,
    };

    function clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    function escapeText(value) {
        return String(value == null ? '—' : value);
    }

    function setUrl() {
        var next = new URLSearchParams(window.location.search);
        next.set('group', state.grouping);
        next.set('zoom', state.zoom.toFixed(2));
        next.set('mode', state.live ? 'live' : 'frozen');
        next.set('range', String(state.rangeMinutes));
        if (state.selected) next.set('selected', state.selected);
        else next.delete('selected');
        if (state.filtered) next.set('filter', 'active');
        else next.delete('filter');
        window.history.replaceState(null, '', window.location.pathname + '?' + next.toString());
    }

    function formatClock(seconds) {
        var base = new Date(state.data.range.start);
        base.setUTCSeconds(base.getUTCSeconds() + seconds);
        return base.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
    }

    function formatTimestamp(value, fallbackSeconds) {
        if (!value) return formatClock(fallbackSeconds);
        return new Date(value).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
    }

    function windowSeconds() {
        if (state.data.range.seconds) return state.data.range.seconds;
        return Math.max(1, (new Date(state.data.range.end) - new Date(state.data.range.start)) / 1000);
    }

    function currentNow() {
        if (state.frozenAt) return state.frozenAt;
        if (!state.data) return new Date();
        if (state.live && state.data && state.data.source === 'activity-store') return new Date();
        return new Date(state.data.range.now);
    }

    function duration(item) {
        var nowSeconds = (currentNow() - new Date(state.data.range.start)) / 1000;
        var end = item.end == null ? nowSeconds : item.end;
        if (item.end == null && item.open === false) return 0;
        return Math.max(0, end - item.start);
    }

    function recordedDuration(item) {
        if (item.phase === 'point') return 0;
        if (item.end == null && item.open !== false) return duration(item);
        if (item.duration_ms != null) return Math.max(0, item.duration_ms / 1000);
        return duration(item);
    }

    function durationText(item) {
        if (item.phase === 'point') return 'point event';
        if (item.end == null && item.open === false) return 'duration unknown';
        return Math.round(recordedDuration(item)) + ' seconds';
    }

    function itemAriaLabel(item) {
        return actorText(item) + ', ' + item.label + ', ' + item.status + ', '
            + durationText(item)
            + (item.continues_before_range ? ', started before range' : '')
            + ', at ' + formatClock(item.start);
    }

    function visibleStart(item) {
        return Math.max(0, item.start);
    }

    function visibleDuration(item) {
        if (item.end == null && item.open === false) return 0;
        var nowSeconds = (currentNow() - new Date(state.data.range.start)) / 1000;
        var end = item.end == null ? nowSeconds : item.end;
        return Math.max(0, Math.min(windowSeconds(), end) - visibleStart(item));
    }

    function agentSlotFor(item) {
        if (item.actor !== 'agent') return null;
        if (!Object.prototype.hasOwnProperty.call(state.agentSlots, item.actor_id)) {
            state.agentSlots[item.actor_id] = state.nextAgentSlot % 4;
            state.nextAgentSlot += 1;
        }
        return state.agentSlots[item.actor_id];
    }

    function actorText(item) {
        if (item.actor_label) return item.actor_label;
        if (item.actor === 'human') return 'Human';
        if (item.actor === 'automation') return 'Automation';
        return item.actor;
    }

    function trackForActivity(item) {
        if (item.activity_track) return item.activity_track;
        if (item.actor === 'human') return 'activity-human';
        if (item.kind === 'agent.turn' || item.kind === 'agent.session') return 'activity-agent';
        if (item.kind === 'review.await' || item.kind === 'agent.wait') return 'activity-wait';
        if (item.kind.indexOf('review.') === 0 || item.kind.indexOf('chain.') === 0) return 'activity-review';
        return 'activity-tools';
    }

    function tracks(items) {
        if (state.grouping === 'activity') {
            return [
                {id: 'activity-human', name: 'Human input', meta: 'points'},
                {id: 'activity-agent', name: 'Agent work', meta: 'turns'},
                {id: 'activity-tools', name: 'Tools & scripts', meta: 'automation'},
                {id: 'activity-wait', name: 'Waiting', meta: 'blocked'},
                {id: 'activity-review', name: 'Review & handoff', meta: 'coordination'},
            ];
        }

        var result = [{id: 'human', name: 'Human', meta: 'interventions'}];
        var seen = {};
        items.filter(function (item) { return item.actor === 'agent'; }).forEach(function (item) {
            if (seen[item.actor_id]) return;
            seen[item.actor_id] = true;
            var context = item.context || {};
            result.push({
                id: item.actor_id,
                name: item.actor_label || item.actor_id,
                meta: context.agent_sid || 'agent',
                role: item.role || context.role,
            });
        });
        result.push({id: 'automation', name: 'Automation', meta: 'tools · harness'});
        return result;
    }

    function itemTrack(item) {
        if (state.grouping === 'activity') return trackForActivity(item);
        return item.participant_track || item.actor_id;
    }

    function makeLabel(track, ruler) {
        var label = document.createElement('div');
        label.className = 'timeline-track-label';
        var name = document.createElement('span');
        name.className = 'timeline-track-name';
        name.textContent = ruler ? 'Local time' : track.name;
        label.appendChild(name);
        if (!ruler) {
            if (track.role === 'Reviewer') {
                var badge = document.createElement('span');
                badge.className = 'timeline-role-badge';
                badge.textContent = 'Reviewer';
                label.appendChild(badge);
            } else {
                var meta = document.createElement('span');
                meta.className = 'timeline-track-meta';
                meta.textContent = track.meta;
                label.appendChild(meta);
            }
        }
        return label;
    }

    function makeRuler(width) {
        var row = document.createElement('div');
        row.className = 'timeline-row timeline-ruler-row';
        row.appendChild(makeLabel({}, true));
        var ruler = document.createElement('div');
        ruler.className = 'timeline-ruler';
        for (var index = 0; index <= 8; index += 1) {
            var tick = document.createElement('span');
            tick.className = 'timeline-tick';
            tick.style.left = (index / 8 * 100) + '%';
            tick.textContent = formatClock(index / 8 * windowSeconds());
            ruler.appendChild(tick);
        }
        row.appendChild(ruler);
        return row;
    }

    function makeItem(item, width, level) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'timeline-item';
        button.dataset.id = item.id;
        button.dataset.actor = item.actor;
        button.dataset.actorId = item.actor_id;
        button.dataset.kind = item.kind;
        button.dataset.status = item.status;
        button.dataset.phase = item.phase;
        var agentSlot = agentSlotFor(item);
        if (agentSlot != null) button.dataset.agentSlot = String(agentSlot);
        button.classList.toggle('continues-before-range', item.continues_before_range === true);
        button.style.left = (visibleStart(item) / windowSeconds() * width) + 'px';
        button.style.top = (12 + level * 38) + 'px';
        if (item.phase !== 'point') {
            button.style.width = Math.max(8, visibleDuration(item) / windowSeconds() * width) + 'px';
        }
        button.setAttribute(
            'aria-label',
            itemAriaLabel(item)
        );
        var label = document.createElement('span');
        label.className = 'timeline-item-label';
        label.textContent = item.label;
        button.appendChild(label);
        button.addEventListener('click', function () { selectItem(item.id, true); });
        button.addEventListener('keydown', moveSelection);
        return button;
    }

    function render() {
        if (!state.data || state.data.state !== 'ready') return;
        var focusedId = document.activeElement && document.activeElement.dataset
            ? document.activeElement.dataset.id
            : null;
        stage.hidden = false;
        statePanel.hidden = true;
        var items = state.data.items.filter(function (item) {
            return !state.filtered || item.status === 'running' || item.status === 'blocked';
        });
        state.orderedIds = items.slice().sort(function (a, b) {
            return a.start - b.start || a.id.localeCompare(b.id);
        }).map(function (item) { return item.id; });
        var list = tracks(items);
        var width = Math.round(1040 * state.zoom);
        state.canvasWidth = width;
        canvas.replaceChildren();
        canvas.style.setProperty('--timeline-width', width + 'px');
        canvas.appendChild(makeRuler(width));

        list.forEach(function (track) {
            var row = document.createElement('div');
            row.className = 'timeline-row';
            row.dataset.track = track.id;
            row.appendChild(makeLabel(track, false));
            var lane = document.createElement('div');
            lane.className = 'timeline-lane';
            var levelEnds = [];
            var packed = items.filter(function (item) { return itemTrack(item) === track.id; })
                .sort(function (a, b) { return a.start - b.start; })
                .map(function (item) {
                    var pointBudget = 34 / width * windowSeconds();
                    var visualEnd = item.phase === 'point' ? item.start + pointBudget : item.start + duration(item);
                    var level = levelEnds.findIndex(function (end) { return end <= item.start; });
                    if (level === -1) level = levelEnds.length;
                    levelEnds[level] = visualEnd;
                    return {item: item, level: level};
                });
            row.style.minHeight = Math.max(62, 20 + levelEnds.length * 38) + 'px';
            packed.forEach(function (entry) {
                lane.appendChild(makeItem(entry.item, width, entry.level));
            });
            row.appendChild(lane);
            canvas.appendChild(row);
        });

        var nowSeconds = (currentNow() - new Date(state.data.range.start)) / 1000;
        var line = document.createElement('div');
        line.id = 'timeline-now-line';
        line.className = 'timeline-now-line';
        line.style.left = 'calc(var(--timeline-label-width) + ' + (nowSeconds / windowSeconds() * width) + 'px)';
        canvas.appendChild(line);
        visibleCount.textContent = items.length + ' events';
        document.getElementById('timeline-window').textContent =
            formatClock(0) + '–' + formatClock(windowSeconds());
        var skipped = state.data.skipped || 0;
        var flagged = state.data.flagged || 0;
        anomaly.hidden = !skipped && !flagged;
        if (skipped || flagged) {
            var notices = [];
            if (skipped) notices.push(skipped + ' records could not be read');
            if (flagged) notices.push(flagged + ' incomplete lifecycles flagged');
            anomaly.querySelector('strong').textContent = notices.join(' · ');
        }
        syncButtons();
        if (state.selected && items.some(function (item) { return item.id === state.selected; })) {
            selectItem(state.selected, false);
        } else if (state.selected) {
            closeDetail();
        }
        if (focusedId) {
            var restored = canvas.querySelector('[data-id="' + CSS.escape(focusedId) + '"]');
            if (restored) restored.focus({preventScroll: true});
        }
        window.requestAnimationFrame(updateMinimap);
    }

    function syncButtons() {
        document.querySelectorAll('[data-grouping]').forEach(function (button) {
            var active = button.dataset.grouping === state.grouping;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        document.querySelectorAll('[data-range]').forEach(function (button) {
            button.classList.toggle('is-active', Number(button.dataset.range) === state.rangeMinutes);
        });
        liveButton.setAttribute('aria-pressed', state.live ? 'true' : 'false');
        liveButton.querySelector('span:last-child').textContent = state.live ? 'Live' : 'Frozen';
        document.getElementById('timeline-filter').setAttribute('aria-pressed', state.filtered ? 'true' : 'false');
    }

    function showConsent(open) {
        consentPanel.hidden = !open;
        captureButton.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    function renderConsent(value) {
        captureMode.textContent = value.mode === 'auto' ? 'on' : value.mode;
        captureButton.dataset.mode = value.mode;
        document.querySelectorAll('[data-capture-mode]').forEach(function (button) {
            var active = button.dataset.captureMode === value.mode;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        if (value.mode === 'ask') showConsent(true);
        var sourceCopy = {
            config: 'Using the saved preference from Merlin config.',
            environment: 'Using the initial process-environment choice. Saving here takes precedence for later hook events.',
            default: 'Using Merlin’s default ask-before-capture choice.',
        };
        consentStatus.textContent = sourceCopy[value.source] || 'Capture preference source is unknown.';
    }

    function loadConsent() {
        fetch('/api/timeline/consent', {headers: {'Accept': 'application/json'}})
            .then(function (response) { if (!response.ok) throw new Error('request failed'); return response.json(); })
            .then(renderConsent)
            .catch(function () {
                captureMode.textContent = 'unknown';
                captureButton.dataset.mode = 'unknown';
            });
    }

    function updateConsent(mode) {
        consentStatus.textContent = 'Saving preference…';
        document.querySelectorAll('[data-capture-mode]').forEach(function (button) { button.disabled = true; });
        fetch('/api/timeline/consent', {
            method: 'POST',
            headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
            body: JSON.stringify({mode: mode}),
        })
            .then(function (response) { if (!response.ok) throw new Error('request failed'); return response.json(); })
            .then(function (value) {
                renderConsent(value);
                consentStatus.textContent = value.mode === 'auto'
                    ? 'Capture enabled for supported providers and saved in Merlin config.'
                    : value.mode === 'off'
                        ? 'Capture is off and saved in Merlin config. Existing private history is unchanged.'
                        : 'Ask-before-capture is saved in Merlin config.';
                if (value.mode !== 'ask') window.setTimeout(function () { showConsent(false); }, 700);
            })
            .catch(function () { consentStatus.textContent = 'Merlin could not save this preference.'; })
            .finally(function () {
                document.querySelectorAll('[data-capture-mode]').forEach(function (button) { button.disabled = false; });
            });
    }

    function detailField(label, value) {
        var wrapper = document.createElement('div');
        wrapper.dataset.field = label.toLowerCase().replace(/[^a-z0-9]+/g, '-');
        var term = document.createElement('dt');
        var description = document.createElement('dd');
        term.textContent = label;
        description.textContent = escapeText(value);
        wrapper.appendChild(term);
        wrapper.appendChild(description);
        detailGrid.appendChild(wrapper);
    }

    function selectItem(id, updateUrl) {
        var item = state.data.items.find(function (candidate) { return candidate.id === id; });
        if (!item) return;
        state.selected = id;
        var children = state.data.items.filter(function (candidate) { return candidate.parent_id === id; });
        var relatedIds = children.map(function (child) { return child.id; });
        if (item.parent_id) relatedIds.push(item.parent_id);
        document.querySelectorAll('.timeline-item').forEach(function (node) {
            var selected = node.dataset.id === id;
            node.classList.toggle('is-selected', selected);
            node.classList.toggle('is-related', relatedIds.indexOf(node.dataset.id) !== -1);
            node.classList.toggle('is-dimmed', !selected && relatedIds.length > 0 && relatedIds.indexOf(node.dataset.id) === -1);
        });

        var context = item.context || {};
        var tmux = context.tmux || [context.tmux_session, context.tmux_window, context.tmux_pane].filter(Boolean).join(' / ');
        detailTitle.textContent = item.label;
        detailKicker.textContent = item.kind.replace('.', ' · ');
        detailStatus.textContent = item.status === 'blocked' ? '◇ Waiting' : item.status;
        detailStatus.dataset.status = item.status;
        detailGrid.replaceChildren();
        detailField('Actor', actorText(item));
        detailField('Role', item.role || context.role);
        detailField('Started', formatTimestamp(item.start_timestamp, item.start));
        detailField('Ended', item.end == null ? (item.open === false ? 'No completion observed' : 'Open') : formatTimestamp(item.end_timestamp, item.end));
        detailField(
            'Duration',
            item.phase === 'point'
                ? 'Point event'
                : item.end == null && item.open === false
                    ? 'Unknown'
                    : Math.round(recordedDuration(item)) + 's'
        );
        detailField('Project', context.project);
        detailField('Agent ID', context.agent_sid);
        detailField('Provider', context.provider);
        detailField('Model / effort', context.model ? context.model + ' · ' + context.effort : null);
        detailField('Tmux', tmux);
        detailField('Trace', item.trace_id);
        detailField('Source', item.source);
        var relationText = children.length
            ? children.length + ' child activities · selection highlights the causal group.'
            : item.parent_id
                ? 'Child of ' + item.parent_id + ' · parent highlighted on the timeline.'
                : 'No recorded child activities.';
        related.replaceChildren(document.createTextNode(relationText));
        var artifactPath = context.artifact_path || context.session_file;
        if (typeof artifactPath === 'string' && artifactPath.charAt(0) === '/') {
            var link = document.createElement('a');
            link.href = '/files' + artifactPath;
            link.textContent = 'Open recorded artifact';
            link.className = 'timeline-detail-link';
            related.appendChild(document.createElement('br'));
            related.appendChild(link);
        }
        detail.classList.add('is-open');
        detail.setAttribute('aria-hidden', 'false');
        scrim.hidden = false;
        if (updateUrl) setUrl();
    }

    function closeDetail() {
        state.selected = null;
        detail.classList.remove('is-open');
        detail.setAttribute('aria-hidden', 'true');
        scrim.hidden = true;
        document.querySelectorAll('.timeline-item').forEach(function (node) {
            node.classList.remove('is-selected', 'is-related', 'is-dimmed');
        });
        setUrl();
    }

    function moveSelection(event) {
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        event.preventDefault();
        if (!state.data || !state.orderedIds.length) return;
        var index = state.orderedIds.indexOf(event.currentTarget.dataset.id);
        if (index < 0) return;
        var next = event.key === 'ArrowRight' ? index + 1 : index - 1;
        var nextId = state.orderedIds[(next + state.orderedIds.length) % state.orderedIds.length];
        var nextButton = canvas.querySelector('[data-id="' + CSS.escape(nextId) + '"]');
        if (nextButton) nextButton.focus();
    }

    function showState(kind, message) {
        stage.hidden = true;
        statePanel.hidden = false;
        canvas.replaceChildren();
        state.orderedIds = [];
        if (kind !== 'loading' && state.selected) closeDetail();
        var states = {
            'collector-disabled': ['◐', 'Activity history is off'],
            'disconnected': ['↯', 'Timeline disconnected'],
            'loading': ['⋯', 'Loading activity'],
            'no-results': ['⌁', 'No matching activity'],
        };
        var presentation = states[kind] || ['○', 'A quiet window'];
        var icon = presentation[0];
        var title = presentation[1];
        statePanel.innerHTML = '<div class="timeline-state-content"><div class="timeline-state-icon" aria-hidden="true">' + icon + '</div><h2>' + title + '</h2><p></p></div>';
        statePanel.querySelector('p').textContent = message;
        visibleCount.textContent = '0 events';
    }

    function zoom(delta) {
        var before = scroll.scrollWidth ? (scroll.scrollLeft + scroll.clientWidth / 2) / scroll.scrollWidth : 0;
        state.zoom = clamp(state.zoom + delta, 0.7, 2.4);
        render();
        scroll.scrollLeft = before * scroll.scrollWidth - scroll.clientWidth / 2;
        setUrl();
    }

    function relativeItem(item, range) {
        var output = Object.assign({}, item);
        if (output.start_timestamp) {
            output.start = (new Date(output.start_timestamp) - new Date(range.start)) / 1000;
            output.continues_before_range = output.start < 0;
        }
        if (output.end_timestamp) {
            output.end = (new Date(output.end_timestamp) - new Date(range.start)) / 1000;
        }
        return output;
    }

    function itemInRange(item, range) {
        if (!item.start_timestamp) {
            var seconds = (new Date(range.end) - new Date(range.start)) / 1000;
            if (item.phase === 'point') return item.start >= 0 && item.start <= seconds;
            return item.start <= seconds && (item.end == null || item.end >= 0);
        }
        var start = new Date(item.start_timestamp);
        var rangeStart = new Date(range.start);
        var rangeEnd = new Date(range.end);
        if (item.phase === 'point') return start >= rangeStart && start <= rangeEnd;
        var end = item.end_timestamp ? new Date(item.end_timestamp) : null;
        return start <= rangeEnd && (end == null || end >= rangeStart);
    }

    function mergeResponse(data, initial) {
        if (initial || !state.data || state.data.state !== 'ready') {
            var previousItems = state.data && state.data.state === 'ready'
                ? state.data.items.slice()
                : [];
            var invalidated = {};
            (data.updates || []).forEach(function (update) {
                invalidated[update.id] = true;
            });
            state.data = data;
            state.data.skipped = data.skipped || 0;
            state.data.flagged = data.flagged == null ? (data.anomalies || 0) : data.flagged;
            state.data.anomalies = state.data.skipped + state.data.flagged;
            state.data.dropped = data.dropped || 0;
            if (data.range) {
                state.data.items = data.items
                    .filter(function (item) { return itemInRange(item, data.range); })
                    .map(function (item) { return relativeItem(item, data.range); });
                var byId = {};
                state.data.items.forEach(function (item) { byId[item.id] = true; });
                previousItems.forEach(function (item) {
                    if (byId[item.id] || invalidated[item.id]) return;
                    if (item.phase === 'point' || item.end != null || item.open === false) return;
                    if (!itemInRange(item, data.range)) return;
                    var misses = item._rebaselineMisses || 0;
                    if (!data.partial && misses >= 1) return;
                    var carried = relativeItem(item, data.range);
                    carried._rebaselineMisses = misses + 1;
                    state.data.items.push(carried);
                    byId[item.id] = true;
                });
                if (state.data.items.length) {
                    state.data.state = 'ready';
                    state.data.message = null;
                }
            }
            if (state.data.items.length > 2500) {
                state.data.items = state.data.items.slice(-2500);
                state.data.partial = true;
            }
            state.cursor = data.cursor || null;
            return true;
        }
        state.cursor = data.cursor || state.cursor;
        if (!data.range) return false;
        var changed = false;
        var targetRange = data.source === 'activity-store' ? data.range : state.data.range;
        var byId = {};
        state.data.items.forEach(function (item) { byId[item.id] = item; });
        (data.updates || []).forEach(function (update) {
            var existing = byId[update.id];
            if (!existing) return;
            existing.end_timestamp = update.end_timestamp;
            existing.end = update.end_timestamp
                ? (new Date(update.end_timestamp) - new Date(targetRange.start)) / 1000
                : existing.end;
            existing.duration_ms = update.duration_ms;
            existing.status = update.status;
            existing.anomaly = update.anomaly;
            existing.open = false;
            changed = true;
        });
        (data.items || []).forEach(function (item) {
            var prepared = relativeItem(item, targetRange);
            if (byId[item.id]) {
                Object.assign(byId[item.id], prepared);
            } else {
                state.data.items.push(prepared);
                byId[item.id] = prepared;
            }
            changed = true;
        });
        if (data.source === 'activity-store') {
            if (state.data.range.start !== data.range.start) changed = true;
            state.data.range = data.range;
            state.data.source = data.source;
            state.data.items = state.data.items
                .filter(function (item) { return itemInRange(item, targetRange); })
                .map(function (item) { return relativeItem(item, targetRange); });
        }
        var seconds = (new Date(targetRange.end) - new Date(targetRange.start)) / 1000;
        state.data.items = state.data.items.filter(function (item) {
            return item.start <= seconds && (item.phase === 'point' ? item.start >= 0 : item.end == null || item.end >= 0);
        }).sort(function (a, b) { return a.start - b.start || a.id.localeCompare(b.id); });
        if (state.data.items.length > 2500) {
            state.data.items = state.data.items.slice(-2500);
            state.data.partial = true;
            changed = true;
        }
        state.data.partial = state.data.partial || data.partial;
        state.data.skipped = (state.data.skipped || 0) + (data.skipped || 0);
        state.data.flagged = (state.data.flagged || 0) + (data.flagged || 0);
        state.data.anomalies = state.data.skipped + state.data.flagged;
        state.data.dropped = data.dropped == null ? (state.data.dropped || 0) : data.dropped;
        if (!state.data.items.length && data.state !== 'ready') {
            state.data.state = data.state;
            state.data.message = data.message;
        } else {
            state.data.state = 'ready';
            state.data.message = null;
        }
        return changed;
    }

    function timelineEndpoint(incremental) {
        var query = new URLSearchParams();
        var end = new Date();
        var start = new Date(end.getTime() - state.rangeMinutes * 60000);
        query.set('since', start.toISOString());
        query.set('until', end.toISOString());
        query.set('grouping', state.grouping);
        query.set('limit', '2000');
        if (state.scenario) query.set('state', state.scenario);
        if (incremental && state.cursor) query.set('cursor', state.cursor);
        return '/api/timeline?' + query.toString();
    }

    function queueFrozen(data) {
        if (!(data.items || []).length && !(data.updates || []).length && !data.anomalies) return;
        state.pending.push(data);
        var count = state.pending.reduce(function (total, batch) {
            return total + (batch.items || []).length + (batch.updates || []).length;
        }, 0);
        while (count > 2500 && state.pending.length > 1) {
            var removed = state.pending.shift();
            count -= (removed.items || []).length + (removed.updates || []).length;
            state.data.partial = true;
        }
    }

    function showConnection(reconnecting) {
        if (reconnecting) {
            connection.hidden = false;
            connection.querySelector('strong').textContent = 'Reconnecting…';
        } else if (state.data && state.data.dropped) {
            connection.hidden = false;
            connection.querySelector('strong').textContent = 'Capture gap · ' + state.data.dropped + ' events were not written today';
        } else if (state.data && state.data.partial) {
            connection.hidden = false;
            connection.querySelector('strong').textContent = 'Partial history · result limit reached';
        } else {
            connection.hidden = true;
        }
    }

    function fetchTimeline(initial) {
        if (state.inFlight) return;
        state.inFlight = true;
        var generation = state.generation;
        fetch(timelineEndpoint(!initial), {headers: {'Accept': 'application/json'}})
            .then(function (response) { if (!response.ok) throw new Error('request failed'); return response.json(); })
            .then(function (data) {
                if (generation !== state.generation) return;
                showConnection(false);
                if (initial && data.state !== 'ready'
                    && (!state.data || state.data.state !== 'ready')) {
                    state.data = data;
                    state.cursor = data.cursor || null;
                    showState(data.state, data.message || 'No activity is available in this range.');
                    return;
                }
                if (!initial && !state.live) {
                    state.cursor = data.cursor || state.cursor;
                    queueFrozen(data);
                    return;
                }
                var changed = mergeResponse(data, initial);
                if (state.data.state !== 'ready') {
                    showState(state.data.state, state.data.message || 'No activity is available in this range.');
                    return;
                }
                if (changed) render();
                showConnection(false);
                if (initial) requestAnimationFrame(function () {
                    if (state.live) scroll.scrollLeft = scroll.scrollWidth;
                });
            })
            .catch(function () {
                if (generation !== state.generation) return;
                if (state.data && state.data.state === 'ready') showConnection(true);
                else showState('disconnected', 'Merlin could not refresh this local timeline. Existing history is unchanged.');
            })
            .finally(function () {
                if (generation !== state.generation) return;
                state.inFlight = false;
                window.clearTimeout(state.pollTimer);
                if (!state.data || state.data.source !== 'deterministic-fixture') {
                    var hasOpen = state.live && state.data && state.data.state === 'ready'
                        && state.data.items.some(function (item) { return item.end == null && item.open !== false; });
                    state.openPolls = hasOpen ? state.openPolls + 1 : 0;
                    var rebaseline = state.openPolls >= OPEN_REBASELINE_POLLS;
                    if (rebaseline) state.openPolls = 0;
                    state.pollTimer = window.setTimeout(function () { fetchTimeline(rebaseline); }, 1500);
                }
            });
    }

    function updateLiveGeometry() {
        if (!state.live || !state.data || state.data.state !== 'ready') return;
        var line = document.getElementById('timeline-now-line');
        var nowSeconds = (currentNow() - new Date(state.data.range.start)) / 1000;
        if (line) line.style.left = 'calc(var(--timeline-label-width) + ' + (nowSeconds / windowSeconds() * state.canvasWidth) + 'px)';
        state.data.items.forEach(function (item) {
            if (item.phase === 'point' || item.end != null || item.open === false) return;
            var node = canvas.querySelector('[data-id="' + CSS.escape(item.id) + '"]');
            if (node) {
                node.style.width = Math.max(8, visibleDuration(item) / windowSeconds() * state.canvasWidth) + 'px';
                node.setAttribute('aria-label', itemAriaLabel(item));
            }
            if (state.selected === item.id) {
                var durationDetail = detailGrid.querySelector('[data-field="duration"] dd');
                if (durationDetail) durationDetail.textContent = Math.round(recordedDuration(item)) + 's';
            }
        });
        updateMinimap();
    }

    function updateMinimap() {
        if (!minimapWindow) return;
        var total = scroll.scrollWidth;
        var visible = scroll.clientWidth;
        if (!total || visible >= total) {
            minimapWindow.style.left = '0%';
            minimapWindow.style.width = '100%';
            return;
        }
        var width = clamp(visible / total * 100, 4, 100);
        var left = scroll.scrollLeft / Math.max(1, total - visible) * (100 - width);
        minimapWindow.style.left = left + '%';
        minimapWindow.style.width = width + '%';
    }

    document.querySelectorAll('[data-grouping]').forEach(function (button) {
        button.addEventListener('click', function () {
            state.grouping = button.dataset.grouping;
            render();
            setUrl();
        });
    });
    document.querySelectorAll('[data-range]').forEach(function (button) {
        button.addEventListener('click', function () {
            state.rangeMinutes = Number(button.dataset.range);
            state.generation += 1;
            state.inFlight = false;
            state.cursor = null;
            state.pending = [];
            state.data = null;
            state.orderedIds = [];
            state.openPolls = 0;
            showState('loading', 'Loading recent activity…');
            syncButtons();
            setUrl();
            fetchTimeline(true);
        });
    });
    document.getElementById('timeline-zoom-in').addEventListener('click', function () { zoom(0.2); });
    document.getElementById('timeline-zoom-out').addEventListener('click', function () { zoom(-0.2); });
    document.getElementById('timeline-fit').addEventListener('click', function () { state.zoom = 0.7; render(); scroll.scrollLeft = 0; setUrl(); });
    document.getElementById('timeline-now').addEventListener('click', function () { scroll.scrollLeft = scroll.scrollWidth; });
    document.getElementById('timeline-filter').addEventListener('click', function () { state.filtered = !state.filtered; render(); setUrl(); });
    liveButton.addEventListener('click', function () {
        if (state.live) {
            state.frozenAt = currentNow();
            state.live = false;
        } else {
            state.live = true;
            state.frozenAt = null;
            state.pending.forEach(function (batch) { mergeResponse(batch, false); });
            state.pending = [];
            if (state.data && state.data.state === 'ready') {
                render();
                scroll.scrollLeft = scroll.scrollWidth;
            }
        }
        syncButtons();
        setUrl();
    });
    captureButton.addEventListener('click', function () { showConsent(consentPanel.hidden); });
    document.querySelectorAll('[data-capture-mode]').forEach(function (button) {
        button.addEventListener('click', function () { updateConsent(button.dataset.captureMode); });
    });
    document.getElementById('timeline-detail-close').addEventListener('click', closeDetail);
    scrim.addEventListener('click', closeDetail);
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape') closeDetail(); });

    var dragging = false;
    var dragStart = 0;
    var scrollStart = 0;
    scroll.addEventListener('pointerdown', function (event) {
        if (event.target.closest('.timeline-item')) return;
        dragging = true;
        dragStart = event.clientX;
        scrollStart = scroll.scrollLeft;
        scroll.setPointerCapture(event.pointerId);
    });
    scroll.addEventListener('pointermove', function (event) {
        if (dragging) scroll.scrollLeft = scrollStart - (event.clientX - dragStart);
    });
    scroll.addEventListener('pointerup', function () { dragging = false; });
    scroll.addEventListener('scroll', updateMinimap, {passive: true});
    window.addEventListener('resize', updateMinimap);

    showState('loading', 'Loading recent activity…');
    loadConsent();
    syncButtons();
    fetchTimeline(true);
    window.setInterval(updateLiveGeometry, 1000);
})();
