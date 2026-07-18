(function() {
    var _swarmCfg = JSON.parse(document.getElementById('swarm-config').textContent);
    var _configGroups = _swarmCfg.groups;
    var _workerCount = _swarmCfg.workerCount || 0;
    let selectedWorker = null;
    var _pageReady = false;
    try { selectedWorker = sessionStorage.getItem('swarm_selected_worker') || null; } catch(e) {}

    // Show toast stored before a reload (survives location.reload)
    try {
        var _pendingToast = sessionStorage.getItem('reload_toast');
        var _pendingWarn = sessionStorage.getItem('reload_toast_warn') === '1';
        if (_pendingToast) {
            sessionStorage.removeItem('reload_toast');
            sessionStorage.removeItem('reload_toast_warn');
            // Defer until showToast is defined (later in this IIFE)
            document.addEventListener('DOMContentLoaded', function() { showToast(_pendingToast, _pendingWarn); });
        }
    } catch(e) {}
    let ws = null;
    var _restarting = false;
    var _restartRecoveryTimer = null;
    // Queen cooldown timer — cleared on restart cleanup at ~L9383. Was
    // referenced there without ever being declared, which threw
    // ReferenceError on every page load (mobile QA caught it).
    var queenCooldownTimer = null;
    let reconnectTimer = null;
    let reconnectDelay = 1000;
    const MAX_RECONNECT_DELAY = 30000;
    let prevWorkerStates = {}; // track states for STUNG detection

    // Tracked intervals for bulk cleanup on page unload
    var _trackedIntervals = [];

    // Page visibility — title flash state
    let pageHidden = document.hidden;
    // Wall-clock (ms) of when the tab was last hidden. Used on resume to decide
    // whether a background→foreground cycle was long enough to have killed the
    // WebSocket at the OS level. Mobile suspends backgrounded tabs and silently
    // tears down the socket's TCP connection while ws.readyState still reports
    // OPEN (the "zombie socket"), so on resume we can't trust readyState — a
    // long-enough hide forces a fresh reconnect instead of waiting for the
    // browser to notice the socket is dead (the old 5–15s red→green delay).
    let lastHiddenAt = 0;
    let titleFlashTimer = null;
    let pendingTitleCount = 0;
    const ORIGINAL_TITLE = document.title;

    // Terminal cache — keeps xterm.js instances alive across worker switches
    const termCache = new Map();  // workerName → { term, fitAddon, ws, container, connectTimer, reconnectAttempts, reconnectTimer, lastCols, lastRows, lastAccess }
    const MAX_CACHED_TERMS = 10;
    // A real terminal is never this small. proposeDimensions() returns
    // garbage (~6 cols) when the container is measured mid-layout — flex
    // settling, panel transition, mobile address-bar animation. Fitting to
    // that value and SIGWINCHing it to the holder wraps Claude's output at
    // ~6 chars (the dashboard→worker "formatting" bug). Below this floor we
    // treat the measurement as not-ready and wait for the retry ladder.
    const MIN_TERM_COLS = 20;
    const MIN_TERM_ROWS = 4;
    let activeTermWorker = null;
    var MAX_TERM_RECONNECT = 3;
    // Backward-compat aliases — updated on every show/hide so existing code
    // (fullscreen, keyboard shortcuts, mobileSend, etc.) keeps working.
    let inlineTerm = null;
    let inlineTermWs = null;
    let inlineFitAddon = null;
    let inlineTermWorker = null;

    // Auth token resolution lives in window.swarmAuth (see
    // static/auth.js, loaded via base.html).  Phase A of the
    // duplication sweep — pre-fix the dashboard had its own local
    // _serverToken / sessionToken / clearSessionToken / wsToken /
    // maybeClearStaleSessionToken stack that drifted from config.html's
    // implementation, producing the WS lockout bug fixed in 2026.5.5.7.
    //
    // The local helper names are kept as thin pass-throughs for the
    // ~5 existing call sites (lines 418, 2185, 3196, 3258, 7609).
    window.swarmAuth.setServerToken(_swarmCfg.wsToken || '');
    function sessionToken() { return window.swarmAuth.getToken(); }
    function clearSessionToken() { window.swarmAuth.clearSessionToken(); }
    function wsToken() { return window.swarmAuth.getToken(); }
    function maybeClearStaleSessionToken() {
        return window.swarmAuth.clearStaleSessionToken();
    }

    // --- Delegated event handlers (replaces inline onclick/onkeydown/oninput) ---
    var _actions = {
        toggleDrones: function() { toggleDrones(); },
        tunnelAction: function() { tunnelAction(); },
        requestNotifPermission: function() { requestNotifPermission(); },
        killSession: function() { killSession(); },
        toggleMobileMenu: function(el, e) { e.stopPropagation(); toggleMobileMenu(e); },
        closeMobileMenu: function() { closeMobileMenu(); },
        mobileSend: function() { mobileSend(); },
        showLaunch: function() { showLaunch(); },
        openTerminalFullscreen: function() { openTerminalFullscreen(); },
        showCreateTask: function() { showCreateTask(); },
        approveAllProposals: function() { approveAllProposals(); },
        rejectAllProposals: function() { rejectAllProposals(); },
        installUpdate: function() { installUpdate(); },
        hideUpdateBanner: function() { hideUpdateBanner(); },
        hideConflictBanner: function() { hideConflictBanner(); },
        hideBroadcast: function() { hideBroadcast(); },
        sendBroadcast: function() { sendBroadcast(); },
        hideQueen: function() { hideQueen(); },
        hideLaunch: function() { hideLaunch(); },
        launchAll: function() { launchAll(); },
        launchSelected: function() { launchSelected(); },
        hideSpawn: function() { hideSpawn(); },
        doSpawn: function() { doSpawn(); },
        hideEditWorker: function() { hideEditWorker(); },
        doEditWorker: function() { doEditWorker(); },
        closeTaskModal: function() { closeTaskModal(); },
        submitTaskModal: function() { submitTaskModal(); },
        hideShutdown: function() { hideShutdown(); },
        doRestartServer: function() { doRestartServer(); },
        doStopServer: function() { doStopServer(); },
        doKillEverything: function() { doKillEverything(); },
        hideTunnel: function() { hideTunnel(); },
        copyTunnelUrl: function() { copyTunnelUrl(); },
        stopTunnel: function() { stopTunnel(); },
        hideConfirm: function() { hideConfirm(); },
        hideDecisionModal: function() { hideDecisionModal(); },
        closeTerminal: function() { closeTerminal(); },
        switchTab: function(el) { switchTab(el.dataset.tab); },
        switchTaskFilter: function(el) { switchTaskFilter(el.dataset.filter); },
        switchPriorityFilter: function(el) { switchPriorityFilter(el.dataset.priority); },
        switchVerifyFilter: function(el) { switchVerifyFilter(el.dataset.verifyFilter); },
        switchBuzzFilter: function(el) { switchBuzzFilter(el.dataset.buzzCat); },
        standingLoopStart: function(el) { standingLoopPost('start', { worker: el.dataset.worker }); },
        standingLoopPause: function(el) { standingLoopPost('pause', { worker: el.dataset.worker }); },
        standingLoopStop: function(el) { standingLoopPost('stop', { worker: el.dataset.worker }); },
        standingLoopKill: function() { var on = !document.getElementById('standing-kill-btn').classList.contains('btn-danger'); standingLoopPost('kill-switch', { on: on }); },
        refreshHarness: function() { refreshHarness(); },
        harnessApply: function(el) { harnessApply(el); },
        doAction: function(el) { doAction(el.dataset.doAction, el.dataset.doCommand ? JSON.parse(el.dataset.doCommand) : null); },
        devReload: function() { devReload(); },
        footerCheckForUpdate: function() { footerCheckForUpdate(); },
        sendFeedback: function() { sendFeedback(); },
        hideFeedback: function() { hideFeedback(); },
        setFeedbackCategory: function(el) { setFeedbackCategory(el.dataset.category); },
        submitFeedback: function() { submitFeedback(); },
        copyFeedbackMarkdown: function() { copyFeedbackMarkdown(); },
        toggleFeedbackAttachment: function(el) { toggleFeedbackAttachment(el.dataset.key); },
        backToFeedbackForm: function() { backToFeedbackForm(); },
        confirmFeedbackSubmit: function() { confirmFeedbackSubmit(); },
        copyFeedbackPreviewMarkdown: function() { copyFeedbackPreviewMarkdown(); },
        showDecisionModal: function(el) { showDecisionModal(parseInt(el.dataset.index, 10)); },
        toggleRuleStats: function(el) { toggleRuleStats(el); },
        showRuleModal: function(el) { showRuleModal(el.dataset.detail || ''); },
        hideRuleModal: function() { hideRuleModal(); },
        testRulePattern: function() { testRulePattern(); },
        submitRule: function() { submitRule(); },
        previewJiraSync: function() { previewJiraSync(); },
        syncJira: function() { syncJira(); },
        showOutlookImport: function() { showOutlookImport(); },
        hideOutlookImport: function() { hideOutlookImport(); },
        importOutlookSeparate: function() { submitOutlookImport('separate'); },
        importOutlookMerge: function() { submitOutlookImport('merge'); },
        showCreatePipeline: function() { showCreatePipeline(); },
        hidePipelineModal: function() { hidePipelineModal(); },
        createPipeline: function() { submitPipeline(); },  // legacy alias
        submitPipeline: function() { submitPipeline(); },
        showEditPipeline: function(el) { showEditPipeline(el.dataset.pipelineId); },
        showPipelineDetail: function(el) { showPipelineDetail(el.dataset.pipelineId); },
        hidePipelineDetail: function() { hidePipelineDetail(); },
        editFromDetail: function() { editFromDetail(); },
        retryStep: function(el) { retryStep(el.dataset.pipelineId, el.dataset.stepId); },
        retryStepCompleted: function(el) { retryStepCompleted(el.dataset.pipelineId, el.dataset.stepId); },
        hideRetryConfirm: function() { hideRetryConfirm(); },
        confirmRetry: function() { confirmRetry(); },
        openLinkedTask: function(el) { openLinkedTask(el.dataset.taskId); },
        copyStepResult: function(el) { copyStepResult(el.dataset.stepId); },
        switchPlaybookFilter: function(el) { switchPlaybookFilter(el.dataset.pbStatus); },
        showPlaybookEvents: function(el) { showPlaybookEvents(el.dataset.pbName); },
        hidePlaybookEvents: function() { hidePlaybookEvents(); },
        togglePlaybookBulk: function() { togglePlaybookBulk(); },
        bulkPlaybookPromote: function() { bulkPlaybookAction('promote'); },
        bulkPlaybookRetire: function() { bulkPlaybookAction('retire'); },
        qhSwitchStatus: function(el) { qhSwitchStatus(el.dataset.qhStatus); },
        qhLoadMore: function() { qhLoadMore(); },
        qhOpenDetail: function(el) { qhOpenDetail(el.dataset.threadId); },
        qhHideDetail: function() { qhHideDetail(); },
        qhReopenSend: function() { qhReopenSend(); },
        qhViewInCC: function() { qhViewInCC(); },
        msgLoadMore: function() { msgLoadMore(); },
        msgOpenDetail: function(el) { msgOpenDetail(el.dataset.msgGroup); },
        msgHideDetail: function() { msgHideDetail(); },
        msgToggleCompose: function() { msgToggleCompose(); },
        msgSendCompose: function() { msgSendCompose(); },
        msgToggleSelect: function() { msgToggleSelect(); },
        msgBulkDelete: function() { msgBulkDelete(); },
        msgClearSelect: function() { msgClearSelect(); },
        ccMobileFocus: function(el) { ccMobileFocus(el.dataset.ccFocus); },
        toggleResourcePopover: function(el, e) { e.stopPropagation(); toggleResourcePopover(); },
        toggleBottomPanel: function() { toggleBottomPanel(); },
        toggleFocusMode: function() { toggleFocusMode(); },
        toggleTabUtils: function(el, e) { e.stopPropagation(); toggleTabUtils(); },
        showShortcuts: function() { document.getElementById('shortcuts-modal').style.display = 'flex'; },
        hideShortcuts: function() { document.getElementById('shortcuts-modal').style.display = 'none'; },
        hideOnboarding: function() { var m = document.getElementById('onboarding-modal'); if (m) m.style.display = 'none'; },
        toggleTileMode: function() { toggleTileMode(); },
        toggleBulkSelect: function() { toggleBulkSelect(); },
        bulkSelectAll: function() { bulkSelectAllVisible(); },
        bulkComplete: function() { bulkAction('complete'); },
        bulkFail: function() { bulkAction('fail'); },
        bulkReopen: function() { bulkAction('reopen'); },
        bulkRemove: function() { showConfirm('Remove ' + bulkSelectedIds.size + ' task(s)?', function() { bulkAction('remove'); }); },
        bulkClearSelection: function() { clearBulkSelection(); },
        reviveAll: function() { reviveAllStung(); },
        killSleeping: function() { killAllSleeping(); },
        exportTasks: function() { exportTasks(); },
        hidePalette: function() { hidePalette(); },
        copyHolderDriftCmd: function() { window.copyHolderDriftCmd && window.copyHolderDriftCmd(); },
        bounceHolder: function() { bounceHolder(); },
    };

    // Click delegation for [data-action]
    document.body.addEventListener('click', function(e) {
        // A checkbox nested inside a [data-action] row (e.g. a message
        // select box inside a clickable broadcast group) must toggle the
        // box, not fire the row action.
        if (e.target.closest('input[type="checkbox"]')) return;
        var el = e.target.closest('[data-action]');
        if (!el) return;
        var fn = _actions[el.dataset.action];
        if (fn) {
            fn(el, e);
            if (el.dataset.closeMenu) closeMobileMenu();
        }
    });

    // Modal dismiss: click on overlay background
    document.addEventListener('click', function(e) {
        var overlay = e.target.closest('[data-modal-dismiss]');
        if (overlay && e.target === overlay) {
            var fn = _actions[overlay.dataset.modalDismiss];
            if (fn) fn(overlay, e);
        }
    });

    // Keydown delegation for [data-enter-action]
    document.addEventListener('keydown', function(e) {
        if (e.key !== 'Enter') return;
        var el = e.target.closest('[data-enter-action]');
        if (!el) return;
        e.preventDefault();
        var fn = _actions[el.dataset.enterAction];
        if (fn) fn(el, e);
    });

    // Click delegation for [data-worker-revive] (STUNG recovery button)
    document.addEventListener('click', function(e) {
        var btn = e.target.closest('[data-worker-revive]');
        if (!btn) return;
        e.stopPropagation();
        var name = btn.dataset.workerRevive;
        actionFetch('/action/revive/' + encodeURIComponent(name), { method: 'POST' })
            .then(function() {
                showToast('Reviving ' + name);
                setTimeout(refreshWorkers, 2000);
            });
    });

    // Input delegation for [data-input-action]
    document.addEventListener('input', function(e) {
        var el = e.target.closest('[data-input-action]');
        if (!el) return;
        var action = el.dataset.inputAction;
        if (action === 'debouncedTaskSearch') debouncedTaskSearch(el.value);
        else if (action === 'debouncedBuzzSearch') debouncedBuzzSearch(el.value);
        else if (action === 'workerSearch') filterWorkers(el.value);
        else if (action === 'qhSearchChanged') qhSearchChanged(el.value);
        else if (action === 'msgSearchChanged') msgSearchChanged(el.value);
    });

    // Change delegation for [data-input-action] on <select>/<input type=date/checkbox>
    // (these fire `change`, not `input`).
    document.addEventListener('change', function(e) {
        var el = e.target.closest('[data-input-action]');
        if (!el) return;
        if (el.dataset.inputAction === 'qhFilterChanged') qhFilterChanged();
        else if (el.dataset.inputAction === 'msgFilterChanged') msgFilterChanged();
    });

    // Mobile email file upload (visible button for touch devices)
    document.addEventListener('change', function(e) {
        if (e.target.id !== 'mobile-email-upload') return;
        var file = e.target.files && e.target.files[0];
        if (!file) return;
        showToast('Parsing email: ' + file.name);
        var fd = new FormData();
        fd.append('file', file);
        fetch('/api/tasks/from-email', { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch' } })
            .then(function(r) {
                if (!r.ok) return r.text().then(function(t) { throw new Error(t); });
                return r.json();
            })
            .then(function(data) {
                if (data.error) { showToast('Email parse failed: ' + data.error, true); return; }
                openTaskModal('create', { title: data.title || '', desc: data.description || '', task_type: data.task_type || '' });
                taskModalAttachmentPaths = data.attachments || [];
                taskModalSourceEmailId = data.message_id || '';
            })
            .catch(function(err) { showToast('Upload failed: ' + err.message, true); });
        e.target.value = '';
    });

    // Checkbox delegation for bulk task selection
    document.addEventListener('change', function(e) {
        var cb = e.target.closest('.task-select-cb');
        if (!cb) return;
        var id = cb.dataset.taskId;
        if (cb.checked) bulkSelectedIds.add(id);
        else bulkSelectedIds.delete(id);
        updateBulkCount();
    });

    // Checkbox delegation for bulk message selection (B10). A row's box
    // carries one or more underlying message ids (a collapsed broadcast
    // selects/deletes all its members at once).
    document.addEventListener('change', function(e) {
        var cb = e.target.closest('.msg-select-cb');
        if (!cb) return;
        (cb.dataset.msgIds || '').split(',').forEach(function(id) {
            if (!id) return;
            if (cb.checked) _msgSelectedIds[id] = true;
            else delete _msgSelectedIds[id];
        });
        _msgUpdateBulkCount();
    });

    // Restore checkbox state after HTMX refreshes task list
    document.addEventListener('htmx:afterSettle', function(e) {
        if (!e.detail || !e.detail.target || e.detail.target.id !== 'task-list') return;
        if (!bulkSelectMode) return;
        document.querySelectorAll('.task-select-cb').forEach(function(cb) {
            cb.style.display = 'inline';
            if (bulkSelectedIds.has(cb.dataset.taskId)) cb.checked = true;
        });
    });

    // Drag events for task drop zone
    (function() {
        var tabTasks = document.getElementById('tab-tasks');
        if (!tabTasks) return;
        tabTasks.addEventListener('dragenter', function(e) {
            e.preventDefault(); e.dataTransfer.dropEffect = 'copy';
            tabTasks.style.borderColor = 'var(--lavender)';
        });
        tabTasks.addEventListener('dragover', function(e) {
            e.preventDefault(); e.dataTransfer.dropEffect = 'copy';
        });
        tabTasks.addEventListener('dragleave', function() {
            tabTasks.style.borderColor = '';
        });
        tabTasks.addEventListener('drop', function(e) {
            handleEmailDrop(e); tabTasks.style.borderColor = '';
        });
    })();


    /** Convert server-side UTC timestamps to browser-local display time. */
    function formatLocalTimes(container) {
        container.querySelectorAll('.local-time[data-ts]').forEach(function(el) {
            var ts = parseFloat(el.dataset.ts);
            if (isNaN(ts)) return;
            var d = new Date(ts * 1000);
            el.textContent = '[' + d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true}) + ']';
        });
    }

    // --- DRY helpers ---

    /** POST to an action endpoint and parse JSON. On success calls onOk(data). */
    function postAction(endpoint, body, onOk) {
        fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'Dashboard' },
            body: body
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) { showToast('Error: ' + data.error, true); return; }
            onOk(data);
        })
        .catch(() => showToast('Request failed', true));
    }

    /** Simple POST to an /action/ endpoint with CSRF header. Returns fetch promise. */
    function actionFetch(url, opts) {
        opts = opts || {};
        opts.method = 'POST';
        var h = opts.headers || {};
        h['X-Requested-With'] = 'Dashboard';
        opts.headers = h;
        return fetch(url, opts);
    }

    // Global 401 interceptor — redirect to login on session expiry
    (function() {
        var _origFetch = window.fetch;
        window.fetch = function() {
            return _origFetch.apply(this, arguments).then(function(resp) {
                if (resp.status === 401 && resp.url && !resp.url.includes('/login')) {
                    window.location.href = '/login';
                }
                return resp;
            });
        };
    })();

    /** POST a task action (complete, remove, fail, unassign, reopen). */
    function taskAction(action, taskId, successStatus, successMsg) {
        postAction(
            '/action/task/' + action,
            'task_id=' + encodeURIComponent(taskId),
            function(data) {
                if (data.status === successStatus) {
                    showToast(successMsg);
                    refreshTasks();
                }
            }
        );
    }

    // --- Bulk task operations ---
    function toggleBulkSelect() {
        bulkSelectMode = !bulkSelectMode;
        var toggle = document.getElementById('bulk-select-toggle');
        var actions = document.getElementById('bulk-actions');
        if (toggle) toggle.classList.toggle('btn-active', bulkSelectMode);
        if (actions) actions.style.display = bulkSelectMode ? 'inline-flex' : 'none';
        document.querySelectorAll('.task-select-cb').forEach(function(cb) {
            cb.style.display = bulkSelectMode ? 'inline' : 'none';
            cb.checked = false;
        });
        bulkSelectedIds.clear();
        updateBulkCount();
    }

    function clearBulkSelection() {
        document.querySelectorAll('.task-select-cb').forEach(function(cb) { cb.checked = false; });
        bulkSelectedIds.clear();
        updateBulkCount();
    }

    // Select every task row currently rendered — i.e. whatever the active
    // status/priority/search filters left visible. Checkbox-per-row was the
    // only path before, which made "complete everything in this filter"
    // an N-click chore.
    function bulkSelectAllVisible() {
        document.querySelectorAll('.task-select-cb').forEach(function(cb) {
            if (cb.offsetParent === null) return; // hidden row (filtered out)
            cb.checked = true;
            if (cb.dataset.taskId) bulkSelectedIds.add(cb.dataset.taskId);
        });
        updateBulkCount();
    }

    function updateBulkCount() {
        var el = document.getElementById('bulk-count');
        if (el) el.textContent = bulkSelectedIds.size + ' selected';
    }

    // Bulk reassign via dropdown
    var reassignSel = document.getElementById('bulk-reassign-select');
    if (reassignSel) {
        reassignSel.addEventListener('change', function() {
            var worker = this.value;
            if (!worker || !bulkSelectedIds.size) { this.value = ''; return; }
            var ids = Array.from(bulkSelectedIds);
            showConfirm('Reassign ' + ids.length + ' task(s) to "' + worker + '"?', function() {
                actionFetch('/api/tasks/bulk', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
                    body: JSON.stringify({ action: 'assign', task_ids: ids, worker: worker }),
                })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.error) { showToast('Error: ' + data.error, true); return; }
                    showToast(data.succeeded + ' task(s) reassigned to ' + worker);
                    bulkSelectedIds.clear();
                    updateBulkCount();
                    refreshTasks();
                });
            });
            this.value = '';
        });
    }

    function bulkAction(action) {
        if (!bulkSelectedIds.size) return;
        var ids = Array.from(bulkSelectedIds);
        actionFetch('/api/tasks/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
            body: JSON.stringify({ action: action, task_ids: ids }),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) { showToast('Error: ' + data.error, true); return; }
            showToast(data.succeeded + ' task(s) ' + action + 'd' + (data.failed ? ', ' + data.failed + ' failed' : ''));
            bulkSelectedIds.clear();
            updateBulkCount();
            refreshTasks();
        });
    }

    // --- WebSocket ---
    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
        // Phase B of duplication sweep: openAuthenticated handles the
        // URL build + first-message auth send.  See static/ws-auth.js.
        ws = window.swarmWS.openAuthenticated('/ws');

        ws.onopen = function() {
            var wasDisconnected = !document.getElementById('ws-dot').classList.contains('connected');
            document.getElementById('ws-dot').classList.add('connected');
            if (wasDisconnected && reconnectDelay > 1000) {
                showToast('Connection restored');
                // Dev-only: if the daemon came back on a new build, reload the
                // page so we don't keep running stale cached assets. No-op in
                // production (gated on termDebug) and when the build is unchanged.
                maybeReloadOnBuildChange();
                // Refresh all panels — WS messages may have been lost during disconnect.
                // Worker 'state' events are among the lost messages, so the worker
                // list/status must be re-synced too — otherwise it shows stale state
                // after a reconnect (the common mobile case: tab backgrounded → WS
                // dropped → resumes with stale worker badges).
                refreshWorkers();
                refreshStatus();
                refreshTasks();
                refreshBuzzLog();
                if (typeof refreshPipelines === 'function') refreshPipelines();
            }
            reconnectDelay = 1000;
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        };

        ws.onclose = function() {
            document.getElementById('ws-dot').classList.remove('connected');
            maybeClearStaleSessionToken();
            showToast('Connection lost \u2014 reconnecting\u2026', true);
            if (_restarting) return;
            reconnectTimer = setTimeout(connect, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
        };

        ws.onerror = function(e) {
            console.error('[swarm-ws] error:', e);
            maybeClearStaleSessionToken();
        };

        ws.onmessage = function(e) {
            try {
                const data = JSON.parse(e.data);
                handleEvent(data);
            } catch(err) { console.error('[swarm-ws] handleEvent error:', err); }
        };
    }

    function ensureMainWsConnected() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
        console.warn('[swarm-restart] ensuring main WS connected; state=', ws ? ws.readyState : 'none');
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
        connect();
    }

    // Tear down the current main WS unconditionally and reconnect fresh. Used on
    // resume from a real background (see lastHiddenAt) where readyState can lie
    // (zombie OPEN socket). Detaching the old handlers first stops a late
    // onclose from scheduling a competing reconnect after we've already opened a
    // new socket. connect() guards on ws being OPEN/CONNECTING, so we null it
    // first to let the reconnect proceed.
    function forceReconnectMainWs() {
        console.warn('[swarm-restart] force-reconnecting main WS; state=', ws ? ws.readyState : 'none');
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
        reconnectDelay = 1000;
        if (ws) {
            try {
                ws.onclose = null;
                ws.onerror = null;
                ws.onmessage = null;
                ws.close();
            } catch (e) { /* already closing/closed */ }
            ws = null;
        }
        connect();
    }

    function fetchJsonNoStore(url, timeoutMs) {
        timeoutMs = timeoutMs || 1000;
        var controller = null;
        var timer = null;
        if (typeof AbortController !== 'undefined') {
            controller = new AbortController();
            timer = setTimeout(function() {
                try { controller.abort(); } catch (e) {}
            }, timeoutMs);
        }
        return fetch(url, {
            method: 'GET',
            cache: 'no-store',
            signal: controller ? controller.signal : undefined,
            headers: { 'X-Requested-With': 'Dashboard' }
        }).then(function(r) {
            if (!r.ok) throw new Error('non-200');
            return r.json();
        }).finally(function() {
            if (timer) clearTimeout(timer);
        });
    }

    // DEV-ONLY: auto-reload the page when the daemon comes back on a changed
    // build. The Reload button and the normal-user auto-update already reload
    // via waitForRestart(), but a restart triggered any OTHER way (a /ship
    // reinstall, an external restart) just drops the WS and reconnects here —
    // leaving the tab on stale cached JS/CSS (the recurring "hard-refresh after
    // Reload" trap). build_sha hashes the source tree, so it changes on every
    // restart-with-new-code (committed OR uncommitted). Gated on termDebug
    // (is_dev): production users have the auto-update flow and we never want a
    // surprise reload there.
    function maybeReloadOnBuildChange() {
        if (!_swarmCfg.termDebug || !_swarmCfg.buildSha) return;
        // The Reload button / auto-update path (waitForRestart) owns the reload
        // when it's active — its prefetch-then-reload is gentler on the SW. Defer.
        if (_restarting) return;
        fetchJsonNoStore('/api/health?_=' + Date.now(), 800).then(function(data) {
            var sha = data && data.build_sha;
            if (!sha || sha === _swarmCfg.buildSha) return;
            try {
                sessionStorage.setItem('reload_toast', 'Dev: new build — reloading page');
            } catch (e) {}
            if (ws) { try { ws.close(); } catch (e2) {} ws = null; }
            location.reload();
        }).catch(function () {});
    }

    function handleEvent(data) {
        switch(data.type) {
            case 'init':
                if (data.test_mode) {
                    isTestMode = true;
                    document.getElementById('test-mode-banner').style.display = 'block';
                }
                if (data.proposals) {
                    renderProposals(data.proposals);
                    updateProposalBadge(data.proposal_count || 0);
                }
                if (data.update && data.update.available) {
                    showUpdateBanner(data.update);
                }
                if (data.queen_queue) {
                    updateQueenQueueBadge(data.queen_queue);
                }
                // fall through to handle workers
            case 'state':
            case 'workers_changed':
                // Detect workers that just went STUNG + prune stale terminals
                if (data.workers) {
                    var liveNames = new Set();
                    data.workers.forEach(function(w) {
                        liveNames.add(w.name);
                        if (w.state === 'STUNG' && prevWorkerStates[w.name] && prevWorkerStates[w.name] !== 'STUNG') {
                            notifyBrowser('Worker Down', w.name + ' exited (STUNG)');
                            destroyTermEntry(w.name);
                        }
                        prevWorkerStates[w.name] = w.state;
                    });
                    pruneStaleTermEntries(liveNames);
                }
                refreshWorkers();
                refreshStatus();
                if (selectedWorker) refreshDetail();
                break;
            case 'drones_toggled':
                updateDronesButton(data.enabled);
                refreshStatus();
                break;
            case 'escalation':
                // FYI toast only — the Queen handles escalations. An
                // interruptive notification fires only if this actually
                // reaches the exception queue (maybeNotifyAttention, which
                // is classifier-derived). Single source of truth.
                showToast(data.worker + ' escalated: ' + data.reason, false, BEE.surprised);
                refreshWorkers();
                refreshBuzzLog();
                break;
            case 'task_assigned':
            case 'task_created':
            case 'task_completed':
                if (data.task) showToast('Task "' + data.task.title + '" ' + data.type.replace('task_', ''), false, data.type === 'task_completed' ? BEE.honeyJar : BEE.flower);
                refreshTasks();
                break;
            case 'task_removed':
                showToast('Task removed');
                refreshTasks();
                break;
            case 'task_failed':
                showToast('Task marked as failed', true, BEE.angry);
                notifyBrowser('Task Failed', data.task ? data.task.title : 'A task was marked as failed');
                refreshTasks();
                break;
            case 'tasks_changed':
                refreshTasks();
                break;
            case 'pipelines_changed':
                refreshPipelines();
                // P3: re-render the detail view if it's currently open
                // and viewing this pipeline. No-op when no detail is open.
                if (typeof window._pldOnPipelinesChanged === 'function') {
                    window._pldOnPipelinesChanged();
                }
                break;
            case 'proposal_created':
                // FYI toast only — a fresh proposal sits in the autonomous
                // window (handled drawer) for ~180s. If it isn't auto-
                // resolved it becomes a decision card and maybeNotifyAttention
                // pings then. No premature interruptive notification.
                showToast('Queen proposes: ' + (data.proposal ? data.proposal.task_title : 'new assignment'), false, BEE.queen);
                refreshProposals();
                // Flash the Decisions badge so users notice even if not on that tab
                switchTab('decisions');
                break;
            case 'proposals_changed':
                renderProposals(data.proposals || []);
                updateProposalBadge(data.pending_count || 0);
                if ((data.pending_count || 0) === 0) { hideQueen(); clearQueenBanners(); }
                refreshDecisions();
                break;
            case 'queen_auto_acted':
                showToast('Queen auto-acted on ' + (data.worker || '?') + ': ' + (data.action || '?'), false, BEE.queen);
                notifyBrowser('Queen Auto-Action', data.worker + ': ' + data.action + ' (' + Math.round((data.confidence||0)*100) + '% confidence)');
                refreshWorkers();
                refreshBuzzLog();
                refreshDecisions();
                break;
            case 'queen_queue':
                updateQueenQueueBadge(data);
                break;
            case 'queen_escalation':
                // Store escalation data so approveAlwaysProposal can access rule_pattern
                if (data.proposal_id) {
                    var existing = _proposalData[data.proposal_id] || {};
                    existing.rule_pattern = existing.rule_pattern || data.rule_pattern || '';
                    existing.prompt_snippet = existing.prompt_snippet || data.prompt_snippet || '';
                    existing.assessment = existing.assessment || data.assessment || '';
                    existing.reasoning = existing.reasoning || data.reasoning || '';
                    if (data.is_plan) existing.is_plan = true;
                    _proposalData[data.proposal_id] = existing;
                }
                showQueenBanner('esc', data);
                notifyBrowser('Queen needs your input', data.worker + ': ' + (data.assessment || data.reasoning || 'Escalation requires review'), true);
                break;
            case 'queen_completion':
                showQueenCompletion(data);
                showQueenBanner('done', data);
                notifyBrowser('Task complete', (data.task_title || 'Task') + ' — ' + (data.worker || ''));
                break;
            case 'queen.health':
                // updateQueenHealthIndicator lives in IIFE 2 (line ~10465);
                // this dispatcher is in IIFE 1, so we go through window.
                if (typeof window.updateQueenHealthIndicator === 'function') {
                    window.updateQueenHealthIndicator(data && data.state);
                }
                break;
            case 'queen.thread':
            case 'queen.message':
                // Live-refresh the Queen history tab when it's the active
                // view so a newly-resolved/created/posted thread moves
                // without a manual reload. Debounced + tab-gated.
                qhMaybeLiveRefresh();
                break;
            case 'operator_terminal_approval':
                showApproveAlwaysBanner(data);
                break;
            case 'draft_reply_ok':
                showToast('Draft reply created for: ' + (data.task_title || 'task'), false, BEE.delivering);
                break;
            case 'draft_reply_failed':
                (function() {
                    var container = document.getElementById('toasts');
                    var toast = document.createElement('div');
                    toast.className = 'toast toast-warning';
                    toast.style.display = 'flex';
                    toast.style.alignItems = 'center';
                    toast.style.gap = '0.5rem';
                    var msg = document.createElement('span');
                    msg.textContent = 'Draft reply FAILED: ' + (data.error || 'unknown error');
                    msg.style.flex = '1';
                    toast.appendChild(msg);
                    if (data.task_id) {
                        var btn = document.createElement('button');
                        btn.textContent = 'Retry';
                        btn.className = 'btn btn-sm';
                        btn.style.cssText = 'background:var(--amber);color:var(--hive-bg);font-size:0.7rem;padding:0.15rem 0.5rem;white-space:nowrap;';
                        btn.onclick = function() { retryDraft(data.task_id); toast.remove(); };
                        toast.appendChild(btn);
                    }
                    container.appendChild(toast);
                    setTimeout(function() { toast.remove(); }, 8000);
                    addNotification('Draft reply FAILED: ' + (data.error || 'unknown error'), true);
                })();
                notifyBrowser('Draft Failed', (data.task_title || 'Task') + ': ' + (data.error || ''));
                break;
            case 'task_send_failed':
                showToast('Task send FAILED to ' + (data.worker || '?') + ': ' + (data.task_title || 'task') + ' — returned to pending', true);
                notifyBrowser('Task Send Failed', (data.task_title || 'Task') + ' could not be sent to ' + (data.worker || 'worker'));
                refreshTasks();
                break;
            case 'system_log':
                refreshBuzzLog();
                // Trigger browser notification for notification-worthy log entries
                if (data.is_notification) {
                    var beeIcon = data.action === 'WORKER_STUNG' ? BEE.angry : BEE.surprised;
                    showToast(data.worker + ': ' + data.detail, data.action === 'WORKER_STUNG' || data.action === 'TASK_FAILED', beeIcon);
                    notifyBrowser(data.action.replace(/_/g, ' '), data.worker + ': ' + data.detail, data.action === 'WORKER_STUNG');
                    addNotification(data.worker + ': ' + data.detail, data.action === 'WORKER_STUNG' || data.action === 'TASK_FAILED');
                }
                break;
            case 'notification':
                // Unified push notification from daemon
                if (data.priority === 'high') {
                    showToast(data.message, true, BEE.surprised);
                    notifyBrowser('Swarm Alert', data.message, true);
                } else {
                    showToast(data.message, false, BEE.happy);
                    notifyBrowser('Swarm', data.message);
                }
                addNotification(data.message, data.priority === 'high');
                break;
            case 'tunnel_started':
                updateTunnelButton(true, data.url);
                if (!tunnelActionPending) showToast('Tunnel active: ' + data.url, false, BEE.happy);
                break;
            case 'tunnel_stopped':
                updateTunnelButton(false, '');
                if (!tunnelActionPending) showToast('Tunnel stopped');
                break;
            case 'tunnel_error':
                updateTunnelButton(false, '');
                break;
            case 'config_changed':
            case 'config_file_changed':
                showToast('Config reloaded');
                refreshWorkers();
                refreshStatus();
                break;
            case 'test_mode':
                if (data.enabled) {
                    isTestMode = true;
                    document.getElementById('test-mode-banner').style.display = 'block';
                }
                break;
            case 'test_report_ready':
                (function() {
                    var link = document.getElementById('test-report-link');
                    link.textContent = 'Report ready: ' + (data.path || '');
                    link.style.display = 'inline';
                    showToast('Test report generated: ' + (data.path || ''), false, BEE.honeyJar);
                })();
                break;
            case 'update_available':
                showUpdateBanner(data);
                break;
            case 'update_progress':
                showUpdateProgress(data.line || '');
                break;
            case 'update_failed':
                showToast('Update failed — check logs', true);
                resetUpdateBanner();
                break;
            case 'update_installed':
                showToast('Update installed — restart swarm to use the new version');
                hideUpdateBanner();
                break;
            case 'update_restarting':
                showToast('Update installed — server restarting...');
                hideUpdateBanner();
                waitForRestart();
                break;
            case 'usage_updated':
                refreshStatus();
                break;
            case 'conflict_detected':
                showConflictBanner(data.conflicts);
                break;
            case 'conflicts_cleared':
                hideConflictBanner();
                break;
            case 'resources':
                updateResourceIndicator(data);
                break;
            case 'dstate_alert':
                showToast('D-state processes detected: ' + Object.values(data.pids || {}).join(', '), true, BEE.surprised);
                notifyBrowser('D-State Alert', 'Uninterruptible processes: ' + Object.values(data.pids || {}).join(', '), true);
                break;
            case 'message':
                // Inter-worker message sent/broadcast — live-refresh the
                // Messages tab when it's the active view (debounced).
                msgMaybeLiveRefresh();
                break;
            default:
                console.debug('[swarm-ws] unknown event type:', data.type);
        }
    }


    var _lastResourceData = null;

    function updateResourceIndicator(data) {
        _lastResourceData = data;
        var indicator = document.getElementById('resource-indicator');
        if (!indicator) return;
        indicator.style.display = 'inline';
        var memFill = document.getElementById('res-mem-fill');
        var swapFill = document.getElementById('res-swap-fill');
        var badge = document.getElementById('res-pressure-badge');
        if (memFill) memFill.style.width = Math.min(data.mem_percent || 0, 100) + '%';
        if (swapFill) swapFill.style.width = Math.min(data.swap_percent || 0, 100) + '%';
        // Color mem bar based on usage
        if (memFill) {
            var mp = data.mem_percent || 0;
            memFill.style.background = mp >= 95 ? '#e74c3c' : mp >= 90 ? '#f39c12' : mp >= 80 ? '#f1c40f' : 'var(--leaf)';
        }
        // Color swap bar — task #353: anchor on pressure_level, NOT raw
        // swap_percent. Standing swap drifts to 80–90% on healthy long-
        // uptime workstations (cold pages stay paged out until something
        // faults on them); coloring from raw swap_percent makes the bar
        // scream PROBLEM while classify_pressure correctly returns
        // NOMINAL — the visual contradicts the badge sitting next to
        // it. The bar's *width* still reflects swap_percent (a fair
        // "how full is the pool" indicator) — only the color switches
        // to the pressure-driven palette so it tracks the badge.
        if (swapFill) {
            var swapLevel = data.pressure_level || 'nominal';
            var swapColors = {
                nominal: 'var(--amber)',
                elevated: '#f1c40f',
                high: '#f39c12',
                critical: '#e74c3c',
            };
            swapFill.style.background = swapColors[swapLevel] || 'var(--amber)';
        }
        if (badge) {
            var level = data.pressure_level || 'nominal';
            var levelLabels = {nominal: 'NOM', elevated: 'ELV', high: 'HIGH', critical: 'CRIT'};
            badge.textContent = levelLabels[level] || level.substring(0, 3).toUpperCase();
            var colors = {nominal: '#2ecc71', elevated: '#f1c40f', high: '#f39c12', critical: '#e74c3c'};
            badge.style.background = colors[level] || '#666';
            badge.style.color = level === 'nominal' || level === 'elevated' ? '#000' : '#fff';
            badge.title = 'Mem: ' + (data.mem_percent || 0).toFixed(0) + '% | Swap: ' + (data.swap_percent || 0).toFixed(0) + '% | Load: ' + (data.load_1m || 0).toFixed(1);
        }
        // Update popover if visible
        var popover = document.getElementById('resource-popover');
        if (popover && popover.style.display !== 'none') updateResourcePopover();
    }

    function toggleResourcePopover() {
        var popover = document.getElementById('resource-popover');
        if (!popover) return;
        if (popover.style.display === 'none') {
            popover.style.display = 'block';
            updateResourcePopover();
        } else {
            popover.style.display = 'none';
        }
    }

    function closeResourcePopover() {
        var popover = document.getElementById('resource-popover');
        if (popover) popover.style.display = 'none';
    }

    function _resBarColor(pct, thresholds, field) {
        var crit = thresholds ? thresholds['critical_' + field] : (field === 'mem_pct' ? 95 : 75);
        var high = thresholds ? thresholds['high_' + field] : (field === 'mem_pct' ? 90 : 50);
        var elev = thresholds ? thresholds['elevated_' + field] : (field === 'mem_pct' ? 80 : 25);
        if (pct >= crit) return '#e74c3c';
        if (pct >= high) return '#f39c12';
        if (pct >= elev) return '#f1c40f';
        return '#2ecc71';
    }

    function _fmtGB(mb) {
        return (mb / 1024).toFixed(1);
    }

    function _psiColor(pct) {
        // Mirrors classify_pressure's PSI override thresholds (10/30) so
        // the bar color matches the level the backend assigned.
        if (pct >= 30) return '#e74c3c';
        if (pct >= 10) return '#f39c12';
        if (pct > 0) return '#f1c40f';
        return '#2ecc71';
    }

    function updateResourcePopover() {
        var popover = document.getElementById('resource-popover');
        if (!popover || !_lastResourceData) return;
        var d = _lastResourceData;
        var t = d.thresholds || {};
        var mp = d.mem_percent || 0;
        var sp = d.swap_percent || 0;
        var level = d.pressure_level || 'nominal';
        var html = '';

        // PSI section (task #352): kernel-reported stall percentages —
        // the canonical "are we hurting NOW" signal. Only shown when the
        // running kernel actually has CONFIG_PSI=y; CONFIG_PSI=n boxes
        // get the legacy memory/load layout instead.
        if (d.psi_available) {
            var psiCpu = d.psi_cpu_avg10 || 0;
            var psiMem = d.psi_mem_avg10 || 0;
            var psiIo = d.psi_io_avg10 || 0;
            var psiMax = Math.max(psiCpu, psiMem, psiIo);
            html += '<div class="res-section">';
            html += '<div class="res-label"><span class="res-label-name">Stall (PSI)</span>';
            html += '<span style="color:' + _psiColor(psiMax) + '">' + psiMax.toFixed(1) + '%</span></div>';
            html += '<div class="res-bar-container"><div class="res-bar-fill" style="width:' + Math.min(psiMax, 100) + '%;background:' + _psiColor(psiMax) + ';"></div></div>';
            html += '<div class="res-detail">cpu ' + psiCpu.toFixed(1) + '% &middot; mem ' + psiMem.toFixed(1) + '% &middot; io ' + psiIo.toFixed(1) + '%</div>';
            html += '</div>';
        }

        // Memory section
        var memColor = _resBarColor(mp, t, 'mem_pct');
        html += '<div class="res-section">';
        html += '<div class="res-label"><span class="res-label-name">Memory</span><span>' + mp.toFixed(1) + '%</span></div>';
        html += '<div class="res-bar-container"><div class="res-bar-fill" style="width:' + Math.min(mp, 100) + '%;background:' + memColor + ';"></div></div>';
        if (d.mem_used_mb != null && d.mem_total_mb != null) {
            html += '<div class="res-detail">' + _fmtGB(d.mem_used_mb) + ' / ' + _fmtGB(d.mem_total_mb) + ' GB</div>';
        }
        html += '</div>';

        // Load averages — normalized against cpu_count so the operator
        // doesn't have to do the math (load 2.0 on a 4-CPU box is fine).
        html += '<div class="res-section">';
        var l1 = d.load_1m || 0, l5 = d.load_5m || 0, l15 = d.load_15m || 0;
        var cpus = d.cpu_count || 1;
        var loadPct = (l1 / cpus) * 100;
        var loadColor = loadPct >= 100 ? '#e74c3c' : loadPct >= 75 ? '#f39c12' : 'inherit';
        html += '<div class="res-label"><span class="res-label-name">Load</span>';
        html += '<span style="color:' + loadColor + '">' + loadPct.toFixed(0) + '% utilized</span></div>';
        html += '<div class="res-detail">' + l1.toFixed(2) + ' / ' + l5.toFixed(2) + ' / ' + l15.toFixed(2);
        html += ' <span style="color:var(--muted)">(' + cpus + ' CPUs, 1m / 5m / 15m)</span></div>';
        html += '</div>';

        // Swap I/O section — replaces the misleading standing swap bar.
        // Per task #352: standing swap is normal Linux cold-page
        // behaviour. Only swap traffic correlates with worker pain.
        // Zero rate (the common case) renders as a flat ✓ — that
        // flatness IS the answer.
        var swapIn = d.swap_in_per_sec || 0;
        var swapOut = d.swap_out_per_sec || 0;
        var swapTotal = swapIn + swapOut;
        var swapIoColor = swapTotal >= 100 ? '#e74c3c' : swapTotal >= 10 ? '#f39c12' : swapTotal > 0 ? '#f1c40f' : '#2ecc71';
        html += '<div class="res-section">';
        html += '<div class="res-label"><span class="res-label-name">Swap I/O</span>';
        if (swapTotal === 0) {
            html += '<span style="color:#2ecc71">&#10003; idle</span></div>';
        } else {
            html += '<span style="color:' + swapIoColor + '">' + swapTotal.toFixed(1) + ' pages/s</span></div>';
            html += '<div class="res-detail">in ' + swapIn.toFixed(1) + ' &middot; out ' + swapOut.toFixed(1) + ' pages/s</div>';
        }
        html += '</div>';

        // Pressure explanation box
        var boxColors = {nominal: {bg: 'rgba(46,204,113,0.12)', border: '#2ecc71', text: '#2ecc71'},
                         elevated: {bg: 'rgba(241,196,15,0.12)', border: '#f1c40f', text: '#f1c40f'},
                         high: {bg: 'rgba(243,156,18,0.12)', border: '#f39c12', text: '#f39c12'},
                         critical: {bg: 'rgba(231,76,60,0.12)', border: '#e74c3c', text: '#e74c3c'}};
        var bc = boxColors[level] || boxColors.nominal;
        html += '<div class="res-section">';
        html += '<div class="res-pressure-box" style="background:' + bc.bg + ';border:1px solid ' + bc.border + ';color:' + bc.text + ';">';
        html += '<strong>' + level.toUpperCase() + '</strong> ';
        if (level === 'nominal') {
            html += '— All clear';
        } else if (level === 'elevated') {
            var reasons = [];
            if (t.elevated_mem_pct && mp >= t.elevated_mem_pct) reasons.push('Memory at ' + mp.toFixed(0) + '% (threshold: ' + t.elevated_mem_pct + '%)');
            if (t.elevated_swap_pct && sp >= t.elevated_swap_pct) reasons.push('Swap at ' + sp.toFixed(0) + '% (threshold: ' + t.elevated_swap_pct + '%)');
            html += '— ' + (reasons.length ? reasons.join('; ') : 'Threshold exceeded');
        } else if (level === 'high') {
            var reasons = [];
            if (t.high_mem_pct && mp >= t.high_mem_pct) reasons.push('Memory at ' + mp.toFixed(0) + '% (threshold: ' + t.high_mem_pct + '%)');
            if (t.high_swap_pct && sp >= t.high_swap_pct) reasons.push('Swap at ' + sp.toFixed(0) + '% (threshold: ' + t.high_swap_pct + '%)');
            html += '— Suspending idle workers. ' + (reasons.length ? reasons.join('; ') : '');
        } else if (level === 'critical') {
            var reasons = [];
            if (t.critical_mem_pct && mp >= t.critical_mem_pct) reasons.push('Memory at ' + mp.toFixed(0) + '% (threshold: ' + t.critical_mem_pct + '%)');
            if (t.critical_swap_pct && sp >= t.critical_swap_pct) reasons.push('Swap at ' + sp.toFixed(0) + '% (threshold: ' + t.critical_swap_pct + '%)');
            html += '— All workers suspended except most active. ' + (reasons.length ? reasons.join('; ') : '');
        }
        html += '</div></div>';

        // Suspended workers
        var suspended = d.suspended_for_pressure || [];
        if (suspended.length) {
            html += '<div class="res-section">';
            html += '<div class="res-label"><span class="res-label-name">Suspended Workers</span></div>';
            html += '<div class="res-suspended-list">';
            for (var i = 0; i < suspended.length; i++) {
                html += '<span class="res-suspended-badge">' + suspended[i] + '</span>';
            }
            html += '</div></div>';
        }

        // Top consumers by RSS (task #352) — only populated server-side
        // when pressure is non-NOMINAL. Gives the operator a target to
        // act on instead of just an alert.
        var top = d.top_workers_by_rss || [];
        if (top.length) {
            html += '<div class="res-section">';
            html += '<div class="res-label"><span class="res-label-name">Top by RSS</span></div>';
            for (var i = 0; i < top.length; i++) {
                var entry = top[i];
                if (!entry || entry.length < 2) continue;
                html += '<div class="res-detail">' + entry[0] + ' &mdash; ' + entry[1] + ' MB</div>';
            }
            html += '</div>';
        }

        // D-state alerts
        if (d.dstate_pids && Object.keys(d.dstate_pids).length) {
            html += '<div class="res-section">';
            html += '<div class="res-label"><span class="res-label-name" style="color:#e74c3c;">D-State Processes</span></div>';
            var pids = d.dstate_pids;
            for (var pid in pids) {
                html += '<div class="res-detail" style="color:#e74c3c;">PID ' + pid + ': ' + pids[pid] + '</div>';
            }
            html += '</div>';
        }

        // Standing-swap detail — task #352 demoted this from headline
        // (it lies under sticky-cold-page conditions) into a collapsible
        // details block. Operators who want the historical view can
        // still see it; it just no longer screams PROBLEM in the
        // primary widget.
        var swapColor = _resBarColor(sp, t, 'swap_pct');
        html += '<details class="res-section" style="margin-top:8px;">';
        html += '<summary style="cursor:pointer;color:var(--muted);font-size:0.85em;">Standing swap pool &amp; raw counters</summary>';
        html += '<div class="res-label" style="margin-top:6px;"><span class="res-label-name">Swap pool</span><span>' + sp.toFixed(1) + '%</span></div>';
        html += '<div class="res-bar-container"><div class="res-bar-fill" style="width:' + Math.min(sp, 100) + '%;background:' + swapColor + ';"></div></div>';
        if (d.swap_used_mb != null && d.swap_total_mb != null) {
            html += '<div class="res-detail">' + _fmtGB(d.swap_used_mb) + ' / ' + _fmtGB(d.swap_total_mb) + ' GB';
            html += ' <span style="color:var(--muted)">(cold pages, not pressure)</span></div>';
        }
        html += '</details>';

        popover.innerHTML = html;
    }

    // Close resource popover on outside click
    document.addEventListener('click', function(e) {
        if (!e.target.closest('#resource-indicator')) closeResourcePopover();
    });

    // --- HTMX partial fetchers ---
    var _reorderInFlight = false;
    function refreshWorkers() {
        if (_reorderInFlight) return;
        htmx.ajax('GET', '/partials/workers' + (selectedWorker ? '?worker=' + selectedWorker : ''), '#worker-list');
    }

    // --- Worker search (client-side DOM filter) ---
    var activeWorkerStateFilter = 'all';

    function filterWorkers(query) {
        var q = (query || '').toLowerCase();
        var sf = activeWorkerStateFilter;
        document.querySelectorAll('.worker-item').forEach(function(el) {
            var name = (el.dataset.worker || '').toLowerCase();
            var state = el.dataset.state || '';
            var nameMatch = !q || name.indexOf(q) !== -1;
            var stateMatch = sf === 'all' || state === sf;
            el.style.display = (nameMatch && stateMatch) ? '' : 'none';
        });
    }

    // Worker state filter chip clicks
    document.addEventListener('click', function(e) {
        var chip = e.target.closest('[data-worker-state]');
        if (!chip) return;
        activeWorkerStateFilter = chip.dataset.workerState;
        document.querySelectorAll('[data-worker-state]').forEach(function(c) {
            c.classList.toggle('active', c.dataset.workerState === activeWorkerStateFilter);
        });
        var search = document.getElementById('worker-search');
        filterWorkers(search ? search.value : '');
    });

    // Bulk worker actions
    function reviveAllStung() {
        var stung = [];
        document.querySelectorAll('.worker-item').forEach(function(el) {
            if (el.dataset.state === 'STUNG') stung.push(el.dataset.worker);
        });
        if (!stung.length) return;
        stung.forEach(function(name) {
            actionFetch('/action/revive/' + encodeURIComponent(name), { method: 'POST' });
        });
        showToast('Reviving ' + stung.length + ' worker(s)...');
    }

    function killAllSleeping() {
        var sleeping = [];
        document.querySelectorAll('.worker-item').forEach(function(el) {
            if (el.dataset.state === 'SLEEPING') sleeping.push(el.dataset.worker);
        });
        if (!sleeping.length) return;
        sleeping.forEach(function(name) {
            actionFetch('/action/kill/' + encodeURIComponent(name), { method: 'POST' });
        });
        showToast('Killing ' + sleeping.length + ' sleeping worker(s)...');
    }

    // --- Command Palette (Ctrl+K) ---
    var paletteTimer = null;
    function showPalette() {
        var el = document.getElementById('cmd-palette');
        if (!el) return;
        el.style.display = 'flex';
        var input = document.getElementById('cmd-palette-input');
        if (input) { input.value = ''; input.focus(); }
        document.getElementById('cmd-palette-results').innerHTML = '<div class="cmd-palette-empty">Type to search workers, tasks, and buzz log</div>';
    }
    function hidePalette() {
        var el = document.getElementById('cmd-palette');
        if (el) el.style.display = 'none';
    }
    function paletteSearch(q) {
        if (!q) {
            document.getElementById('cmd-palette-results').innerHTML = '<div class="cmd-palette-empty">Type to search workers, tasks, and buzz log</div>';
            return;
        }
        fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=8')
            .then(function(r) { return r.json(); })
            .then(function(data) { renderPaletteResults(data, q); })
            .catch(function() {});
    }
    function renderPaletteResults(data, q) {
        var html = '';
        if (data.workers && data.workers.length) {
            html += '<div class="cmd-group">Workers</div>';
            data.workers.forEach(function(w) {
                html += '<div class="cmd-item" data-palette-worker="' + w.name + '">'
                    + '<span class="state-dot state-' + w.state + '"></span> '
                    + w.name + ' <span class="cmd-detail">' + w.state.toLowerCase() + '</span></div>';
            });
        }
        if (data.tasks && data.tasks.length) {
            html += '<div class="cmd-group">Tasks</div>';
            data.tasks.forEach(function(t) {
                html += '<div class="cmd-item" data-palette-task="' + t.id + '">'
                    + '#' + t.number + ' ' + t.title
                    + ' <span class="cmd-detail">' + t.status + '</span></div>';
            });
        }
        if (data.buzz && data.buzz.length) {
            html += '<div class="cmd-group">Buzz Log</div>';
            data.buzz.forEach(function(b) {
                html += '<div class="cmd-item">'
                    + (b.worker || '') + ': ' + (b.detail || b.action)
                    + '</div>';
            });
        }
        if (!html) html = '<div class="cmd-palette-empty">No results for \u201c' + q + '\u201d</div>';
        document.getElementById('cmd-palette-results').innerHTML = html;
    }
    // Keyboard shortcut
    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault();
            var el = document.getElementById('cmd-palette');
            if (el && el.style.display === 'flex') hidePalette();
            else showPalette();
        }
        if (e.key === 'Escape') hidePalette();
    });
    // Debounced input
    var cmdInput = document.getElementById('cmd-palette-input');
    if (cmdInput) {
        cmdInput.addEventListener('input', function() {
            clearTimeout(paletteTimer);
            var val = this.value.trim();
            paletteTimer = setTimeout(function() { paletteSearch(val); }, 200);
        });
    }
    // Click on result
    document.addEventListener('click', function(e) {
        var item = e.target.closest('[data-palette-worker]');
        if (item) { selectWorker(item.dataset.paletteWorker); hidePalette(); return; }
        var taskItem = e.target.closest('[data-palette-task]');
        if (taskItem) { switchTab('tasks'); hidePalette(); return; }
    });

    // Export tasks as CSV using current filters
    function exportTasks() {
        var params = [];
        if (activeTaskFilters.size) params.push('status=' + Array.from(activeTaskFilters).join(','));
        if (activePriorityFilters.size) params.push('priority=' + Array.from(activePriorityFilters).join(','));
        if (activeSearchQuery) params.push('search=' + encodeURIComponent(activeSearchQuery));
        params.push('format=csv');
        window.open('/api/tasks/export?' + params.join('&'), '_blank');
    }

    // Bulk worker button visibility
    function updateBulkWorkerButtons() {
        var hasStung = false, hasSleeping = false;
        document.querySelectorAll('.worker-item').forEach(function(el) {
            if (el.dataset.state === 'STUNG') hasStung = true;
            if (el.dataset.state === 'SLEEPING') hasSleeping = true;
        });
        var rb = document.getElementById('revive-all-btn');
        var kb = document.getElementById('kill-sleeping-btn');
        if (rb) rb.style.display = hasStung ? '' : 'none';
        if (kb) kb.style.display = hasSleeping ? '' : 'none';
    }

    // --- Worker keyboard navigation ---
    document.getElementById('worker-list').addEventListener('keydown', function(e) {
        var item = e.target.closest('.worker-item');
        if (!item) return;
        if (e.key === 'Enter') {
            e.preventDefault();
            item.click();
            return;
        }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            var items = Array.from(document.querySelectorAll('.worker-item:not([style*="display: none"])'));
            var idx = items.indexOf(item);
            if (idx === -1) return;
            var next = e.key === 'ArrowDown' ? idx + 1 : idx - 1;
            if (next >= 0 && next < items.length) {
                items[next].focus();
            }
        }
    });

    function refreshStatus() {
        htmx.ajax('GET', '/partials/status', '#status-bar');
    }

    var activeTaskFilters = new Set();
    var activePriorityFilters = new Set();
    let activeSearchQuery = '';
    try { activeSearchQuery = localStorage.getItem('swarm_task_search') || ''; } catch(e) {}
    var bulkSelectMode = false;
    var bulkSelectedIds = new Set();

    function refreshTasks() {
        let url = '/partials/tasks';
        const params = [];
        if (activeTaskFilters.size) params.push('status=' + Array.from(activeTaskFilters).join(','));
        if (activePriorityFilters.size) params.push('priority=' + Array.from(activePriorityFilters).join(','));
        if (selectedWorker) params.push('worker=' + encodeURIComponent(selectedWorker));
        if (activeSearchQuery) params.push('q=' + encodeURIComponent(activeSearchQuery));
        if (params.length) url += '?' + params.join('&');
        htmx.ajax('GET', url, '#task-list');
    }

    var _searchTimer = null;
    window.debouncedTaskSearch = function(val) {
        activeSearchQuery = val.trim();
        try { localStorage.setItem('swarm_task_search', activeSearchQuery); } catch(e) {}
        if (_searchTimer) clearTimeout(_searchTimer);
        _searchTimer = setTimeout(refreshTasks, 300);
    };

    var activeBuzzCategories = new Set();
    var activeBuzzQuery = '';

    function refreshBuzzLog() {
        var params = [];
        if (activeBuzzCategories.size) {
            params.push('category=' + Array.from(activeBuzzCategories).join(','));
        }
        if (activeBuzzQuery) {
            params.push('q=' + encodeURIComponent(activeBuzzQuery));
        }
        var url = '/partials/system-log';
        if (params.length) url += '?' + params.join('&');
        htmx.ajax('GET', url, '#buzz-log');
    }

    window.switchBuzzFilter = function(cat) {
        if (cat === 'all') {
            activeBuzzCategories.clear();
        } else if (activeBuzzCategories.has(cat)) {
            activeBuzzCategories.delete(cat);
        } else {
            activeBuzzCategories.add(cat);
        }
        document.querySelectorAll('#buzz-filters .filter-chip').forEach(function(b) {
            var c = b.getAttribute('data-buzz-cat');
            if (c === 'all') b.classList.toggle('active', activeBuzzCategories.size === 0);
            else b.classList.toggle('active', activeBuzzCategories.has(c));
        });
        // P6: keep the mobile select in sync — only ever a single value,
        // so when multiple chips are active we display 'all' as the
        // safest summary (everything is shown). Operators on a phone
        // get single-category filtering rather than chip multi-select.
        var sel = document.getElementById('buzz-filter-select');
        if (sel) {
            if (activeBuzzCategories.size === 1) {
                sel.value = Array.from(activeBuzzCategories)[0];
            } else {
                sel.value = 'all';
            }
        }
        refreshBuzzLog();
    };

    // P6: when the mobile select changes, clear all chips and apply
    // just the chosen category — single-category model on phone.
    (function () {
        var sel = document.getElementById('buzz-filter-select');
        if (!sel) return;
        sel.addEventListener('change', function () {
            activeBuzzCategories.clear();
            if (sel.value && sel.value !== 'all') activeBuzzCategories.add(sel.value);
            document.querySelectorAll('#buzz-filters .filter-chip').forEach(function (b) {
                var c = b.getAttribute('data-buzz-cat');
                if (c === 'all') b.classList.toggle('active', activeBuzzCategories.size === 0);
                else b.classList.toggle('active', activeBuzzCategories.has(c));
            });
            refreshBuzzLog();
        });
    })();

    var _buzzSearchTimer = null;
    window.debouncedBuzzSearch = function(val) {
        activeBuzzQuery = val.trim();
        if (_buzzSearchTimer) clearTimeout(_buzzSearchTimer);
        _buzzSearchTimer = setTimeout(refreshBuzzLog, 300);
    };

    // --- Decisions (proposal history) ---

    // --- Conflict banner ---
    function showConflictBanner(conflicts) {
        var banner = document.getElementById('git-conflict-banner');
        var details = document.getElementById('conflict-details');
        if (!banner || !details || !conflicts || !conflicts.length) return;
        var parts = conflicts.map(function(c) {
            return c.file + ' (' + c.workers.join(', ') + ')';
        });
        details.textContent = parts.join('; ');
        banner.style.display = 'block';
    }
    function hideConflictBanner() {
        var banner = document.getElementById('git-conflict-banner');
        if (banner) banner.style.display = 'none';
    }

    function refreshDecisions() {
        fetch('/api/decisions?limit=50', { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) { return r.json(); })
            .then(function(data) { renderDecisions(data.decisions || []); })
            .catch(function() {});
    }

    var _decisionCache = [];
    function renderDecisions(decisions) {
        _decisionCache = decisions;
        var el = document.getElementById('decisions-log');
        if (!el) return;
        if (!decisions.length) {
            el.innerHTML = '<div class="empty-state"><img src="/static/bees/queen.svg" class="bee-icon bee-hero" alt=""><div class="mt-sm">No decisions yet</div></div>';
            return;
        }
        var html = '';
        for (var i = 0; i < decisions.length; i++) {
            var d = decisions[i];
            var ts = new Date(d.created_at * 1000);
            var timeStr = ts.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: true});
            var type = d.proposal_type || 'assignment';
            var typeBadge = type === 'escalation' ? 'ESC' : type === 'completion' ? 'DONE' : 'ASSIGN';
            var typeClass = type === 'escalation' ? 'conf-mid' : type === 'completion' ? 'conf-high' : 'bg-lavender';

            var status = d.status || 'approved';
            var outcomeBadge, outcomeClass;
            if (status === 'approved') { outcomeBadge = 'Approved'; outcomeClass = 'conf-high'; }
            else if (status === 'rejected') { outcomeBadge = 'Rejected'; outcomeClass = 'conf-low'; }
            else if (status === 'expired') { outcomeBadge = 'Expired'; outcomeClass = 'conf-mid'; }
            else { outcomeBadge = status; outcomeClass = 'conf-mid'; }

            var confPct = Math.round((d.confidence || 0) * 100);
            var confClass = confPct >= 70 ? 'conf-high' : confPct >= 40 ? 'conf-mid' : 'conf-low';

            var detail = d.assessment || d.reasoning || d.task_title || '';
            if (detail.length > 120) detail = detail.substring(0, 120) + '...';

            html += '<div class="decision-entry" data-action="showDecisionModal" data-index="' + i + '">';
            html += '<span class="text-muted text-xs decision-time">' + timeStr + '</span>';
            html += '<span class="conf-badge ' + typeClass + '">' + typeBadge + '</span>';
            html += '<span class="proposal-worker">' + escapeHtml(d.worker_name) + '</span>';
            if (d.task_title) {
                html += '<span class="text-beeswax flex-1">' + escapeHtml(d.task_title) + '</span>';
            } else {
                html += '<span class="text-beeswax flex-1">' + escapeHtml(detail) + '</span>';
            }
            html += '<span class="conf-badge ' + confClass + '">' + confPct + '%</span>';
            html += '<span class="conf-badge ' + outcomeClass + '">' + outcomeBadge + '</span>';
            html += '<button class="btn btn-sm btn-secondary btn-log" data-action="showDecisionModal" data-index="' + i + '">View</button>';
            html += '</div>';
        }
        var savedScroll = el.scrollTop;
        var wasAtBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 30;
        el.innerHTML = html;
        el.scrollTop = wasAtBottom ? el.scrollHeight : savedScroll;
    }

    // --- Pipelines ---
    function refreshPipelines() {
        fetch('/api/pipelines', { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) { return r.json(); })
            .then(function(pipelines) { renderPipelines(pipelines || []); })
            .catch(function() {});
    }

    function renderPipelines(pipelines) {
        var el = document.getElementById('pipeline-list');
        if (!el) return;
        if (!pipelines.length) {
            el.innerHTML = '<div class="empty-state"><div class="mt-sm">No pipelines yet</div></div>';
            return;
        }
        var html = '';
        for (var i = 0; i < pipelines.length; i++) {
            var p = pipelines[i];
            var statusClass = p.status === 'running' ? 'text-leaf' : p.status === 'completed' ? 'text-sage' : p.status === 'failed' ? 'text-poppy' : p.status === 'paused' ? 'text-amber' : 'text-muted';
            var statusIcon = p.status === 'running' ? '\u25cf' : p.status === 'completed' ? '\u2713' : p.status === 'failed' ? '\u2717' : p.status === 'paused' ? '\u23f8' : '\u25cb';
            var progressPct = 0;
            var completedSteps = 0;
            if (p.steps && p.steps.length) {
                for (var j = 0; j < p.steps.length; j++) {
                    if (p.steps[j].status === 'completed' || p.steps[j].status === 'skipped' || p.steps[j].status === 'failed') completedSteps++;
                }
                progressPct = Math.round((completedSteps / p.steps.length) * 100);
            }
            // P3: whole row opens the detail view (buttons keep their own
            // handlers and stop propagation via the action delegator).
            html += '<div class="task-item pipeline-clickable" data-pipeline-id="' + p.id + '" data-action="showPipelineDetail">';
            html += '<div class="flex-center gap-sm">';
            html += '<span class="' + statusClass + ' fw-bold">' + statusIcon + '</span>';
            html += '<span class="task-title">' + escapeHtml(p.name) + '</span>';
            html += '<span class="conf-badge ' + statusClass + '" style="background:var(--panel);border:1px solid var(--border)">' + p.status + '</span>';
            html += '<span class="text-muted text-xs">' + progressPct + '%</span>';
            // Inline action buttons must stopPropagation so they don't
            // also fire the row's data-action="showPipelineDetail" (added
            // in P3 to make the whole card the detail-open target).
            if (p.status === 'draft') {
                html += '<button class="btn btn-sm btn-approve" onclick="event.stopPropagation();pipelineAction(\'start\',\'' + p.id + '\')">Start</button>';
            } else if (p.status === 'running') {
                html += '<button class="btn btn-sm btn-secondary" onclick="event.stopPropagation();pipelineAction(\'pause\',\'' + p.id + '\')">Pause</button>';
            } else if (p.status === 'paused') {
                html += '<button class="btn btn-sm btn-approve" onclick="event.stopPropagation();pipelineAction(\'resume\',\'' + p.id + '\')">Resume</button>';
            }
            // Edit allowed while step graph is mutable (matches engine guard).
            // Inline onclick (rather than data-action) to keep the
            // stopPropagation contract uniform with the other action buttons.
            if (p.status === 'draft' || p.status === 'paused') {
                html += '<button class="btn btn-sm btn-secondary" onclick="event.stopPropagation();showEditPipeline(\'' + p.id + '\')">Edit</button>';
            }
            html += '<button class="btn btn-sm btn-secondary btn-log" onclick="event.stopPropagation();pipelineAction(\'delete\',\'' + p.id + '\')">&#x2715;</button>';
            html += '</div>';
            if (p.steps && p.steps.length) {
                html += '<div class="context-bar" style="max-width:100%;margin:0.3rem 0"><div class="context-bar-fill" style="width:' + progressPct + '%"></div></div>';
            }
            if (p.steps && p.steps.length) {
                html += '<div style="padding-left:1rem">';
                for (var j = 0; j < p.steps.length; j++) {
                    var s = p.steps[j];
                    var stepIcon = s.status === 'completed' ? '\u2713' : s.status === 'in_progress' ? '\u25cf' : s.status === 'failed' ? '\u2717' : s.status === 'skipped' ? '\u2298' : s.status === 'ready' ? '\u25ce' : '\u25cb';
                    var stepColor = s.status === 'completed' ? 'text-leaf' : s.status === 'in_progress' ? 'text-honey' : s.status === 'failed' ? 'text-poppy' : s.status === 'ready' ? 'text-lavender' : 'text-muted';
                    html += '<div class="text-sm" style="padding:0.15rem 0">';
                    html += '<span class="' + stepColor + '">' + stepIcon + '</span> ';
                    html += escapeHtml(s.name);
                    html += ' <span class="text-xs text-muted">(' + escapeHtml(s.step_type || s.type || '') + ')</span>';
                    if (s.depends_on && s.depends_on.length) {
                        html += ' <span class="text-xs text-muted" title="Depends on: ' + escapeHtml(s.depends_on.join(', ')) + '">\u2190 ' + s.depends_on.length + ' dep</span>';
                    }
                    if (s.step_type === 'human' && (s.status === 'ready' || s.status === 'in_progress')) {
                        html += ' <button class="btn btn-sm btn-approve" style="font-size:0.65rem;padding:0.05rem 0.3rem" onclick="completeStep(\'' + p.id + '\',\'' + s.id + '\')">\u2713 Done</button>';
                    }
                    if (s.status === 'ready' || s.status === 'in_progress') {
                        html += ' <button class="btn btn-sm btn-secondary" style="font-size:0.65rem;padding:0.05rem 0.3rem" onclick="skipStep(\'' + p.id + '\',\'' + s.id + '\')">Skip</button>';
                    }
                    if (s.assigned_worker) {
                        html += ' <span class="text-lavender text-xs">' + escapeHtml(s.assigned_worker) + '</span>';
                    }
                    html += '</div>';
                    // Surface failure details and result snippets — previously
                    // hidden in the model, leaving operators blind to WHY a
                    // step failed and what an automated step produced.
                    if (s.error) {
                        html += '<div class="text-xs text-poppy" style="padding-left:1.2rem;white-space:pre-wrap;word-break:break-word">'
                            + escapeHtml(String(s.error).slice(0, 400)) + '</div>';
                    }
                    if (s.result && Object.keys(s.result).length) {
                        var resPreview;
                        try { resPreview = JSON.stringify(s.result).slice(0, 200); }
                        catch (err) { resPreview = '[unserializable result]'; }
                        html += '<div class="text-xs text-muted" style="padding-left:1.2rem;font-family:monospace">'
                            + escapeHtml(resPreview) + '</div>';
                    }
                }
                html += '</div>';
            }
            html += '</div>';
        }
        el.innerHTML = html;
    }

    window.pipelineAction = function(action, pipelineId) {
        if (action === 'delete') {
            showConfirm('Delete this pipeline?', function() {
                fetch('/api/pipelines/' + pipelineId, { method: 'DELETE', headers: { 'X-Requested-With': 'Dashboard' }})
                    .then(function(r) { return r.json(); })
                    .then(function() { showToast('Pipeline deleted'); refreshPipelines(); })
                    .catch(function() {});
            });
        } else {
            fetch('/api/pipelines/' + pipelineId + '/' + action, { method: 'POST', headers: { 'X-Requested-With': 'Dashboard', 'Content-Type': 'application/json' }, body: '{}' })
                .then(function(r) { return r.json(); })
                .then(function() { showToast('Pipeline ' + action + 'ed'); refreshPipelines(); })
                .catch(function() {});
        }
    };

    // --- Playbooks (#404: operator surface for the playbook-synth loop) ---
    //
    // P4 adds analytics: per-status totals + recent event counts up top,
    // status + scope filters, top-by-uses / top-by-winrate movers, and a
    // per-playbook event-timeline modal so the operator can see what
    // actually happened to each one over time.

    var _pbStatusFilter = 'all';   // all | active | candidate | retired
    var _pbScopeFilter = '';       // '' = all
    var _pbAllPlaybooks = [];      // last server response (for client-side filter)
    var _pbBulkMode = false;       // checkbox column visible?
    var _pbBulkSelected = {};      // name -> true

    function refreshPlaybooks() {
        // Always fetch ALL statuses; filters are client-side so chip flips
        // are instant and we get the right totals for the summary band.
        Promise.all([
            fetch('/api/playbooks', { headers: { 'X-Requested-With': 'Dashboard' }})
                .then(function(r) { return r.json(); }),
            fetch('/api/playbooks/analytics', { headers: { 'X-Requested-With': 'Dashboard' }})
                .then(function(r) { return r.json(); })
                .catch(function() { return null; }),
        ])
            .then(function(both) {
                _pbAllPlaybooks = (both[0] && both[0].playbooks) || [];
                _pbPopulateScopeFilter(_pbAllPlaybooks);
                _pbRenderAnalytics(both[1]);
                renderPlaybooks();
            })
            .catch(function() {});
    }

    function _pbPopulateScopeFilter(playbooks) {
        var sel = document.getElementById('pb-filter-scope');
        if (!sel) return;
        var scopes = {};
        playbooks.forEach(function(p) { scopes[p.scope || 'global'] = true; });
        var options = ['<option value="">— all —</option>'];
        Object.keys(scopes).sort().forEach(function(s) {
            options.push('<option value="' + escapeHtml(s) + '"'
                + (_pbScopeFilter === s ? ' selected' : '')
                + '>' + escapeHtml(s) + '</option>');
        });
        sel.innerHTML = options.join('');
    }

    function _pbRenderAnalytics(data) {
        var box = document.getElementById('pb-analytics');
        if (!box || !data) return;
        box.style.display = '';
        var t = data.totals || {};
        document.getElementById('pb-stat-active').textContent = t.active || 0;
        document.getElementById('pb-stat-candidate').textContent = t.candidate || 0;
        document.getElementById('pb-stat-retired').textContent = t.retired || 0;
        var ev = data.event_counts || {};
        document.getElementById('pb-stat-applied-24h').textContent = ev.applied || 0;
        document.getElementById('pb-stat-wins-24h').textContent = ev.win || 0;
        document.getElementById('pb-stat-losses-24h').textContent = ev.loss || 0;
        // Movers + scope breakdown below the stats row.
        var detail = document.getElementById('pb-analytics-detail');
        if (!detail) return;
        var html = '';
        html += '<div class="pb-mover-list">';
        html += '<div class="pb-mover-title">Top by uses</div>';
        (data.top_by_uses || []).forEach(function(row) {
            html += '<div class="pb-mover-row">'
                + '<span class="pb-mover-name" data-action="showPlaybookEvents" data-pb-name="' + escapeHtml(row.name) + '">' + escapeHtml(row.title) + '</span>'
                + '<span class="pb-mover-meta">' + row.uses + ' uses · ' + _pbWinrateLabel(row.winrate) + '</span>'
                + '</div>';
        });
        if (!(data.top_by_uses || []).length) html += '<div class="text-muted text-xs">No usage yet</div>';
        html += '</div>';
        html += '<div class="pb-mover-list">';
        html += '<div class="pb-mover-title">Top by winrate <span class="text-muted">(min 3 uses)</span></div>';
        (data.top_by_winrate || []).forEach(function(row) {
            html += '<div class="pb-mover-row">'
                + '<span class="pb-mover-name" data-action="showPlaybookEvents" data-pb-name="' + escapeHtml(row.name) + '">' + escapeHtml(row.title) + '</span>'
                + '<span class="pb-mover-meta">' + _pbWinrateLabel(row.winrate) + ' · ' + row.uses + ' uses</span>'
                + '</div>';
        });
        if (!(data.top_by_winrate || []).length) html += '<div class="text-muted text-xs">Not enough attributed outcomes yet</div>';
        html += '</div>';
        html += '<div class="pb-mover-list">';
        html += '<div class="pb-mover-title">By scope</div>';
        html += '<table class="pb-scope-table">';
        var scopeKeys = Object.keys(data.scope_breakdown || {}).sort();
        scopeKeys.forEach(function(k) {
            var s = data.scope_breakdown[k];
            html += '<tr>'
                + '<td class="pb-scope-cell">' + escapeHtml(k) + '</td>'
                + '<td>' + Math.round(s.count) + ' pb</td>'
                + '<td>' + Math.round(s.uses) + ' uses</td>'
                + '<td>' + _pbWinrateLabel(s.winrate) + '</td>'
                + '</tr>';
        });
        if (!scopeKeys.length) html += '<tr><td class="text-muted text-xs">No playbooks yet</td></tr>';
        html += '</table>';
        html += '</div>';
        detail.innerHTML = html;
    }

    function _pbWinrateLabel(wr) {
        // -1 = no attribution yet; render as em dash so the operator
        // doesn't read "0%" as "all losses."
        if (wr == null || wr < 0) return '—';
        return Math.round(wr * 100) + '%';
    }

    function renderPlaybooks() {
        var el = document.getElementById('playbook-list');
        if (!el) return;
        var filtered = _pbAllPlaybooks.filter(function(p) {
            if (_pbStatusFilter !== 'all' && p.status !== _pbStatusFilter) return false;
            if (_pbScopeFilter && (p.scope || 'global') !== _pbScopeFilter) return false;
            return true;
        });
        if (!filtered.length) {
            el.innerHTML = '<div class="empty-state"><div class="mt-sm">'
                + (_pbAllPlaybooks.length ? 'No playbooks match the current filters' : 'No playbooks yet')
                + '</div><div class="text-muted text-sm mt-sm">Synthesized from successful tasks — '
                + 'candidates are vetted by outcome before going fleet-active</div></div>';
            return;
        }
        var order = { active: 0, candidate: 1, retired: 2 };
        filtered.sort(function(a, b) { return (order[a.status] || 3) - (order[b.status] || 3); });
        // One row per playbook. Title left-aligned with status icon; meta
        // (scope · win% · uses · prov) + actions right-aligned. Trigger
        // line + provenance list moved to the events modal so they don't
        // double the row height for every card. Operator screenshot
        // (2026-05-20) flagged 23 candidate rows each 3-deep as 'a mess.'
        var html = '';
        for (var i = 0; i < filtered.length; i++) {
            var p = filtered[i];
            var sc = p.status === 'active' ? 'text-leaf'
                : p.status === 'candidate' ? 'text-amber'
                : 'text-muted';
            var si = p.status === 'active' ? '●'
                : p.status === 'candidate' ? '○'
                : '⊘';
            var prov = (p.provenance_task_ids || []).length;
            html += '<div class="task-item pb-playbook-row" data-playbook="' + escapeHtml(p.name) + '">';
            html += '<div class="pb-row-inner">';
            html += '<span class="pb-row-left">';
            // Bulk-select checkbox — display:none until bulk mode is on
            // via togglePlaybookBulk(). Click toggles selection.
            html += '<input type="checkbox" class="pb-row-cb' + (_pbBulkMode ? ' shown' : '') + '" data-pb-name="' + escapeHtml(p.name) + '"'
                + (_pbBulkSelected[p.name] ? ' checked' : '') + '>';
            html += '<span class="' + sc + ' fw-bold">' + si + '</span> ';
            // Title click opens the events-timeline modal (P4a behaviour
            // preserved). Truncates with ellipsis when too long; full
            // text on hover via title attribute.
            html += '<span class="task-title pb-row-title" data-action="showPlaybookEvents" data-pb-name="' + escapeHtml(p.name) + '" title="' + escapeHtml(p.title || p.name) + '">' + escapeHtml(p.title || p.name) + '</span>';
            html += '</span>';
            html += '<span class="pb-row-right">';
            html += '<span class="conf-badge ' + sc + '" style="background:var(--panel);border:1px solid var(--border)">' + escapeHtml(p.status) + '</span>';
            html += '<span class="text-muted text-xs">' + escapeHtml(p.scope || 'global') + '</span>';
            html += '<span class="text-muted text-xs">win ' + Math.round((p.winrate || 0) * 100) + '% · uses ' + (p.uses || 0) + ' · prov ' + prov + '</span>';
            if (p.status === 'candidate') {
                html += '<button class="btn btn-sm btn-approve" onclick="playbookAction(\'promote\',\'' + escapeHtml(p.name) + '\')">Promote</button>';
            }
            if (p.status !== 'retired') {
                html += '<button class="btn btn-sm btn-secondary" onclick="playbookAction(\'retire\',\'' + escapeHtml(p.name) + '\')">Retire</button>';
            }
            html += '</span>';
            html += '</div></div>';
        }
        el.innerHTML = html;
    }

    window.switchPlaybookFilter = function(status) {
        _pbStatusFilter = status || 'all';
        // Sync the chip "active" class — same pattern as the buzz filter.
        document.querySelectorAll('[data-pb-status]').forEach(function(c) {
            c.classList.toggle('active', c.dataset.pbStatus === _pbStatusFilter);
        });
        renderPlaybooks();
    };

    // Wire the scope dropdown (idempotent — runs at module load).
    var _pbScopeSel = document.getElementById('pb-filter-scope');
    if (_pbScopeSel) {
        _pbScopeSel.addEventListener('change', function() {
            _pbScopeFilter = _pbScopeSel.value || '';
            renderPlaybooks();
        });
    }

    // -- Event timeline modal ------------------------------------------

    function _pbFmtTime(epoch) {
        if (!epoch) return '';
        try { return new Date(epoch * 1000).toLocaleString(); }
        catch (e) { return ''; }
    }

    // Operator-followup (2026-05-21): the modal used to show event-log
    // only. With 0-uses candidates the modal was empty and operators
    // had no way to see what a playbook actually CONTAINS before
    // deciding to promote. Now shows the full body + trigger +
    // provenance + actions, with the events list below.
    window.showPlaybookEvents = function(name) {
        document.getElementById('pb-events-title').textContent = name;
        document.getElementById('pb-events-body').innerHTML = '<div class="empty-state">Loading…</div>';
        document.getElementById('pb-events-modal').style.display = 'flex';
        fetch('/api/playbooks/' + encodeURIComponent(name) + '/events', { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) {
                if (!r.ok) throw new Error('not found');
                return r.json();
            })
            .then(function(d) {
                _pbRenderDetailModal(d, name);
            })
            .catch(function() {
                document.getElementById('pb-events-body').innerHTML = '<div class="empty-state text-poppy">Playbook not found</div>';
            });
    };

    function _pbRenderDetailModal(d, name) {
        var body = document.getElementById('pb-events-body');
        var pb = (d && d.playbook) || {};
        var events = (d && d.events) || [];
        var sc = pb.status === 'active' ? 'text-leaf'
            : pb.status === 'candidate' ? 'text-amber'
            : 'text-muted';

        // Header section: title (only if distinct from the modal's title
        // bar — otherwise we'd render the name twice), status badge,
        // scope, key stats.
        var html = '<div class="pbd-section pbd-header">';
        if (pb.title && pb.title !== name) {
            html += '<div class="pbd-title">' + escapeHtml(pb.title) + '</div>';
        }
        html += '<div class="pbd-meta">';
        html += '<span class="conf-badge ' + sc + '" style="background:var(--panel);border:1px solid var(--border)">' + escapeHtml(pb.status || '?') + '</span>';
        html += '<span class="text-muted text-xs">scope: <span class="text-lavender">' + escapeHtml(pb.scope || 'global') + '</span></span>';
        html += '<span class="text-muted text-xs">uses: <strong>' + (pb.uses || 0) + '</strong></span>';
        html += '<span class="text-muted text-xs">winrate: <strong>' + Math.round((pb.winrate || 0) * 100) + '%</strong></span>';
        html += '<span class="text-muted text-xs">version: ' + (pb.version || 1) + '</span>';
        if (pb.last_used_at) html += '<span class="text-muted text-xs">last used: ' + escapeHtml(_pbFmtTime(pb.last_used_at)) + '</span>';
        if (pb.retired_reason) html += '<span class="text-poppy text-xs">retired: ' + escapeHtml(pb.retired_reason) + '</span>';
        html += '</div>';
        // Action buttons in the modal itself (no need to dismiss + find
        // the row to act).
        html += '<div class="pbd-actions">';
        if (pb.status === 'candidate') {
            html += '<button class="btn btn-sm btn-approve" data-action-pb-modal="promote" data-pb-name="' + escapeHtml(name) + '">Promote to Active</button>';
        }
        if (pb.status !== 'retired') {
            html += '<button class="btn btn-sm btn-secondary" data-action-pb-modal="retire" data-pb-name="' + escapeHtml(name) + '">Retire</button>';
        }
        html += '</div>';
        html += '</div>';

        // Trigger — what conditions tell a worker this playbook applies.
        if (pb.trigger) {
            html += '<div class="pbd-section">';
            html += '<div class="pbd-section-label">Trigger</div>';
            html += '<div class="pbd-trigger">' + escapeHtml(pb.trigger) + '</div>';
            html += '</div>';
        }

        // Body — the playbook's instructions. Rendered as preformatted
        // text so markdown-ish structure (bullets, indents) survives.
        if (pb.body) {
            html += '<div class="pbd-section">';
            html += '<div class="pbd-section-label">Body <span class="text-muted text-xs">(what the worker sees if this gets injected)</span></div>';
            html += '<pre class="pbd-body">' + escapeHtml(pb.body) + '</pre>';
            html += '</div>';
        }

        // Provenance — which tasks produced this playbook. Chips link to
        // the task editor via the cleanup-batch openLinkedTask flow.
        var prov = pb.provenance_task_ids || [];
        if (prov.length) {
            html += '<div class="pbd-section">';
            html += '<div class="pbd-section-label">Provenance <span class="text-muted text-xs">(tasks that contributed)</span></div>';
            html += '<div class="pbd-prov">';
            prov.forEach(function(tid) {
                html += '<span class="pld-task-chip" data-action="openLinkedTask" data-task-id="' + escapeHtml(tid) + '" title="Jump to task">#' + escapeHtml(tid) + '</span>';
            });
            html += '</div>';
            html += '</div>';
        }

        // Source worker (which worker the playbook synthesized from).
        if (pb.source_worker) {
            html += '<div class="pbd-section pbd-meta-row">';
            html += '<span class="text-muted text-xs">source worker: <span class="text-lavender">' + escapeHtml(pb.source_worker) + '</span></span>';
            html += '<span class="text-muted text-xs">created: ' + escapeHtml(_pbFmtTime(pb.created_at)) + '</span>';
            html += '<span class="text-muted text-xs">updated: ' + escapeHtml(_pbFmtTime(pb.updated_at)) + '</span>';
            html += '</div>';
        }

        // Events timeline (was previously the whole modal).
        html += '<div class="pbd-section">';
        html += '<div class="pbd-section-label">Events';
        if (!events.length) html += ' <span class="text-muted text-xs">(none yet — playbook hasn\'t been applied to a task)</span>';
        html += '</div>';
        if (events.length) {
            html += '<div class="pbd-events">';
            events.forEach(function(e) {
                var cls = 'pb-event-type pb-event-type-' + (e.event || '').replace(/[^a-z_]/g, '');
                var meta = [];
                if (e.task_id) meta.push('task ' + e.task_id);
                if (e.worker) meta.push('worker ' + e.worker);
                if (e.detail) meta.push(e.detail);
                html += '<div class="pb-event-row">'
                    + '<span class="pb-event-ts">' + escapeHtml(_pbFmtTime(e.ts)) + '</span>'
                    + '<span class="' + cls + '">' + escapeHtml(e.event) + '</span>'
                    + '<span class="pb-event-meta">' + escapeHtml(meta.join(' · ')) + '</span>'
                    + '</div>';
            });
            html += '</div>';
        }
        html += '</div>';

        body.innerHTML = html;
    }

    window.hidePlaybookEvents = function() {
        document.getElementById('pb-events-modal').style.display = 'none';
    };

    // Delegated handler for the in-modal Promote / Retire buttons. After
    // the action lands, refresh the modal contents so the operator sees
    // the new status without having to close + reopen.
    document.addEventListener('click', function(e) {
        var btn = e.target.closest('[data-action-pb-modal]');
        if (!btn) return;
        var action = btn.dataset.actionPbModal;
        var name = btn.dataset.pbName;
        if (!action || !name) return;
        playbookAction(action, name);
        // Re-fetch + re-render so the modal reflects the new state
        // (button hides if newly retired, status badge updates, etc.).
        setTimeout(function() { showPlaybookEvents(name); }, 200);
    });

    window.playbookAction = function(action, name) {
        var body = '{}';
        if (action === 'retire') {
            var reason = window.prompt('Retire reason for "' + name + '":', 'operator-retired');
            if (reason === null) return;  // cancelled
            body = JSON.stringify({ reason: reason || 'operator-retired' });
        }
        fetch('/api/playbooks/' + encodeURIComponent(name) + '/' + action, {
            method: 'POST',
            headers: { 'X-Requested-With': 'Dashboard', 'Content-Type': 'application/json' },
            body: body,
        })
            .then(function(r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function() { showToast('Playbook ' + action + 'd: ' + name); refreshPlaybooks(); })
            .catch(function(e) { showToast('Playbook ' + action + ' failed: ' + (e && e.message || 'error'), true); });
    };

    // Bulk select (operator follow-up 2026-05-21: 23 candidates,
    // one-at-a-time clicking was painful). Toggle reveals checkboxes
    // on every row + a bulk action bar.
    function togglePlaybookBulk() {
        _pbBulkMode = !_pbBulkMode;
        _pbBulkSelected = {};
        var bar = document.getElementById('pb-bulk-bar');
        if (bar) bar.classList.toggle('active', _pbBulkMode);
        var toggle = document.getElementById('pb-bulk-toggle');
        if (toggle) toggle.textContent = _pbBulkMode ? 'Done' : 'Select…';
        document.querySelectorAll('.pb-row-cb').forEach(function(cb) {
            cb.classList.toggle('shown', _pbBulkMode);
            cb.checked = false;
        });
        _pbUpdateBulkCount();
    }

    function _pbUpdateBulkCount() {
        var n = Object.keys(_pbBulkSelected).length;
        var el = document.getElementById('pb-bulk-count');
        if (el) el.textContent = n + ' selected';
    }

    // Click any pb-row-cb to update the in-memory selection set + count.
    document.body.addEventListener('change', function(e) {
        if (!e.target.matches('.pb-row-cb')) return;
        var name = e.target.dataset.pbName;
        if (!name) return;
        if (e.target.checked) _pbBulkSelected[name] = true;
        else delete _pbBulkSelected[name];
        _pbUpdateBulkCount();
    });

    function bulkPlaybookAction(action) {
        var names = Object.keys(_pbBulkSelected);
        if (!names.length) {
            showToast('No playbooks selected', true);
            return;
        }
        var body = '{}';
        if (action === 'retire') {
            var reason = window.prompt('Retire reason for ' + names.length + ' playbook(s):', 'operator-bulk-retired');
            if (reason === null) return;
            body = JSON.stringify({ reason: reason || 'operator-bulk-retired' });
        }
        var label = action === 'promote' ? 'Promoting' : 'Retiring';
        showToast(label + ' ' + names.length + ' playbook(s)…');
        // Parallel POSTs; track success/failure for the summary toast.
        Promise.all(names.map(function(name) {
            return fetch('/api/playbooks/' + encodeURIComponent(name) + '/' + action, {
                method: 'POST',
                headers: { 'X-Requested-With': 'Dashboard', 'Content-Type': 'application/json' },
                body: body,
            }).then(function(r) { return { name: name, ok: r.ok }; });
        }))
            .then(function(results) {
                var ok = results.filter(function(r) { return r.ok; }).length;
                var fail = results.length - ok;
                if (fail) {
                    showToast(label.replace('ing', 'ed') + ' ' + ok + ', ' + fail + ' failed', true);
                } else {
                    showToast(label.replace('ing', 'ed') + ' ' + ok + ' playbook(s)');
                }
                _pbBulkSelected = {};
                _pbUpdateBulkCount();
                refreshPlaybooks();
            })
            .catch(function() {
                showToast('Bulk ' + action + ' failed', true);
            });
    }

    window.completeStep = function(pipelineId, stepId) {
        fetch('/api/pipelines/' + pipelineId + '/steps/' + stepId + '/complete', { method: 'POST', headers: { 'X-Requested-With': 'Dashboard', 'Content-Type': 'application/json' }, body: '{}' })
            .then(function(r) { return r.json(); })
            .then(function() { showToast('Step completed'); refreshPipelines(); })
            .catch(function() {});
    };

    window.skipStep = function(pipelineId, stepId) {
        fetch('/api/pipelines/' + pipelineId + '/steps/' + stepId + '/skip', { method: 'POST', headers: { 'X-Requested-With': 'Dashboard', 'Content-Type': 'application/json' }, body: '{}' })
            .then(function(r) { return r.json(); })
            .then(function() { showToast('Step skipped'); refreshPipelines(); })
            .catch(function() {});
    };

    // -----------------------------------------------------------------------
    // Pipeline editor (P1 — create + edit, conditional step fields,
    // dependency chip picker, worker + service dropdowns from server data).
    // -----------------------------------------------------------------------

    var _plStepCounter = 0;
    var _plMode = 'create';        // 'create' or 'edit'
    var _plEditingId = null;       // pipeline id when editing
    var _plWorkerCache = null;     // [{name}, ...] populated on first open
    var _plServiceCache = null;    // [{name, description, example_config}, ...]
    var _plTaskTypes = ['chore', 'bug', 'feature', 'verify'];

    // P2: schedule builder + timezone state.
    var _plActivePreset = 'ondemand';     // which preset pane is showing
    var _plPreviewTimer = null;           // debounce timer for live preview
    var _plStepPreviewTimers = {};        // per-step debounce timers
    // Curated common IANA zones — covers the bulk of operators. The
    // dropdown ends with a literal "Other (type below)" option that
    // unlocks a freeform input for anything we don't ship.
    var _plCommonTimezones = [
        '', 'UTC',
        'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
        'America/Anchorage', 'America/Honolulu', 'America/Toronto', 'America/Mexico_City',
        'America/Sao_Paulo', 'America/Buenos_Aires',
        'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Madrid', 'Europe/Stockholm',
        'Europe/Athens', 'Europe/Moscow',
        'Africa/Cairo', 'Africa/Johannesburg',
        'Asia/Dubai', 'Asia/Kolkata', 'Asia/Bangkok', 'Asia/Singapore', 'Asia/Hong_Kong',
        'Asia/Tokyo', 'Asia/Seoul', 'Asia/Shanghai',
        'Australia/Sydney', 'Australia/Perth', 'Pacific/Auckland',
    ];

    function _plLoadCatalogs() {
        // Load workers + services in parallel on each open so the dropdowns
        // reflect the current swarm. Cached after first call; refreshed if
        // either fetch failed previously.
        var workersP = _plWorkerCache
            ? Promise.resolve(_plWorkerCache)
            : fetch('/api/workers', { headers: { 'X-Requested-With': 'Dashboard' }})
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    var list = Array.isArray(d) ? d : (d && d.workers) || [];
                    _plWorkerCache = list.map(function(w) {
                        return { name: (w && (w.name || w.id)) || '' };
                    }).filter(function(w) { return !!w.name; });
                    return _plWorkerCache;
                })
                .catch(function() { return []; });
        var servicesP = _plServiceCache
            ? Promise.resolve(_plServiceCache)
            : fetch('/api/pipelines/services', { headers: { 'X-Requested-With': 'Dashboard' }})
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    _plServiceCache = (d && d.services) || [];
                    return _plServiceCache;
                })
                .catch(function() { return []; });
        return Promise.all([workersP, servicesP]);
    }

    function _plOption(val, label, selected) {
        return '<option value="' + escapeHtml(val) + '"'
            + (selected === val ? ' selected' : '') + '>'
            + escapeHtml(label || val) + '</option>';
    }

    // -------- P2: timezone select + schedule builder ----------------------

    function _plPopulateTimezones(selected) {
        var sel = document.getElementById('pl-timezone');
        if (!sel) return;
        var opts = _plCommonTimezones.map(function(tz) {
            var label = tz === '' ? '— server local —' : tz;
            return _plOption(tz, label, selected || '');
        });
        // If the operator's existing tz isn't in our curated list (likely
        // because they typed something custom previously), add it as a
        // sticky option so we don't silently lose it on save.
        if (selected && _plCommonTimezones.indexOf(selected) === -1) {
            opts.push(_plOption(selected, selected + ' (custom)', selected));
        }
        sel.innerHTML = opts.join('');
        sel.value = selected || '';
    }

    function _plActivePresetButton() {
        document.querySelectorAll('[data-action-preset]').forEach(function(b) {
            b.classList.toggle('pl-preset-on', b.dataset.actionPreset === _plActivePreset);
        });
        document.querySelectorAll('.pl-preset-pane').forEach(function(p) { p.style.display = 'none'; });
        var pane = document.getElementById('pl-preset-' + _plActivePreset);
        if (pane) pane.style.display = '';
    }

    function _plBuildCronFromPreset() {
        // Map the active preset's input controls onto a cron string. Empty
        // string ⇒ on-demand (no schedule fires). The same shape goes
        // into the hidden #pl-schedule input that submitPipeline reads.
        var p = _plActivePreset;
        if (p === 'ondemand') return '';
        if (p === 'daily') {
            var t = document.getElementById('pl-daily-time').value || '09:00';
            var parts = t.split(':');
            return parts[1] + ' ' + parts[0] + ' * * *';
        }
        if (p === 'weekly') {
            var days = [];
            document.querySelectorAll('#pl-weekly-days input:checked').forEach(function(c) { days.push(c.value); });
            if (!days.length) return '';
            var t2 = document.getElementById('pl-weekly-time').value || '09:00';
            var parts2 = t2.split(':');
            return parts2[1] + ' ' + parts2[0] + ' * * ' + days.sort().join(',');
        }
        if (p === 'weekdays') {
            var t3 = document.getElementById('pl-weekdays-time').value || '09:00';
            var parts3 = t3.split(':');
            return parts3[1] + ' ' + parts3[0] + ' * * 1-5';
        }
        if (p === 'hourly') {
            var m = parseInt(document.getElementById('pl-hourly-minute').value, 10);
            if (isNaN(m) || m < 0 || m > 59) m = 0;
            return m + ' * * * *';
        }
        if (p === 'cron') {
            return (document.getElementById('pl-cron-input').value || '').trim();
        }
        return '';
    }

    function _plUpdateSchedulePreview() {
        // Debounced live preview against /api/pipelines/schedule/preview.
        // Writes the resolved cron into the hidden #pl-schedule input so
        // submitPipeline reads exactly what the operator sees here.
        var expr = _plBuildCronFromPreset();
        var hidden = document.getElementById('pl-schedule');
        if (hidden) hidden.value = expr;
        var preview = document.getElementById('pl-schedule-preview');
        var list = document.getElementById('pl-schedule-preview-list');
        if (!preview || !list) return;
        if (!expr) {
            preview.style.color = 'var(--muted)';
            preview.textContent = 'On-demand — no scheduled firings';
            list.textContent = '';
            return;
        }
        if (_plPreviewTimer) clearTimeout(_plPreviewTimer);
        _plPreviewTimer = setTimeout(function() {
            var tz = (document.getElementById('pl-timezone') || {}).value || '';
            fetch('/api/pipelines/schedule/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
                body: JSON.stringify({ schedule: expr, timezone: tz, count: 5 }),
            })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (d.valid) {
                        preview.style.color = 'var(--leaf)';
                        preview.textContent = d.human || expr;
                        list.textContent = (d.next || []).map(function(ts) { return '· ' + ts; }).join('\n');
                    } else {
                        preview.style.color = 'var(--poppy)';
                        preview.textContent = d.error || 'Invalid schedule';
                        list.textContent = '';
                    }
                })
                .catch(function() {
                    preview.style.color = 'var(--poppy)';
                    preview.textContent = 'Preview unavailable';
                    list.textContent = '';
                });
        }, 250);
    }

    function _plLoadPresetFromCron(expr) {
        // Reverse the cron back into the matching preset + control
        // values for edit-mode preload. Falls through to "cron" for any
        // expression that doesn't match a known preset shape.
        expr = (expr || '').trim();
        if (!expr) { _plActivePreset = 'ondemand'; return; }
        // Legacy HH:MM shorthand always maps to "daily".
        var legacy = expr.match(/^(\*|\d{1,2}):(\*|\d{1,2})$/);
        if (legacy) {
            _plActivePreset = 'daily';
            var hh = legacy[1] === '*' ? '0' : legacy[1];
            var mm = legacy[2] === '*' ? '0' : legacy[2];
            document.getElementById('pl-daily-time').value =
                ('0' + hh).slice(-2) + ':' + ('0' + mm).slice(-2);
            return;
        }
        var parts = expr.split(/\s+/);
        if (parts.length === 5) {
            var min = parts[0], hour = parts[1], dom = parts[2], mon = parts[3], dow = parts[4];
            if (dom === '*' && mon === '*') {
                // Hourly: any minute fixed, hour wild, dow wild.
                if (hour === '*' && dow === '*' && /^\d+$/.test(min)) {
                    _plActivePreset = 'hourly';
                    document.getElementById('pl-hourly-minute').value = min;
                    return;
                }
                if (dow === '*' && /^\d+$/.test(hour) && /^\d+$/.test(min)) {
                    _plActivePreset = 'daily';
                    document.getElementById('pl-daily-time').value =
                        ('0' + hour).slice(-2) + ':' + ('0' + min).slice(-2);
                    return;
                }
                if (dow === '1-5' && /^\d+$/.test(hour) && /^\d+$/.test(min)) {
                    _plActivePreset = 'weekdays';
                    document.getElementById('pl-weekdays-time').value =
                        ('0' + hour).slice(-2) + ':' + ('0' + min).slice(-2);
                    return;
                }
                // Weekly: dow is a comma-separated list of single digits.
                if (/^\d(,\d)*$/.test(dow) && /^\d+$/.test(hour) && /^\d+$/.test(min)) {
                    _plActivePreset = 'weekly';
                    document.getElementById('pl-weekly-time').value =
                        ('0' + hour).slice(-2) + ':' + ('0' + min).slice(-2);
                    var set = {};
                    dow.split(',').forEach(function(d) { set[d] = true; });
                    document.querySelectorAll('#pl-weekly-days input').forEach(function(c) {
                        c.checked = !!set[c.value];
                    });
                    return;
                }
            }
        }
        // Catch-all: drop the operator into the raw cron editor.
        _plActivePreset = 'cron';
        document.getElementById('pl-cron-input').value = expr;
    }

    function _plRenderStepCard(data) {
        // Returns the HTML for one step. `data` may be partial — we fill
        // defaults so an empty new row works the same as an edit row.
        _plStepCounter++;
        var idx = _plStepCounter;
        data = data || {};
        var stepType = data.step_type || data.type || 'agent';
        var workerOpts = '<option value="">— unassigned —</option>'
            + (_plWorkerCache || []).map(function(w) {
                return _plOption(w.name, w.name, data.assigned_worker || '');
            }).join('');
        var taskTypeOpts = _plTaskTypes.map(function(t) {
            return _plOption(t, t, data.task_type || 'chore');
        }).join('');
        var serviceOpts = '<option value="">— pick a service —</option>'
            + (_plServiceCache || []).map(function(s) {
                return _plOption(s.name, s.name, data.service || '');
            }).join('');
        var configText = '';
        if (data.config && Object.keys(data.config).length) {
            try { configText = JSON.stringify(data.config, null, 2); } catch (e) { configText = ''; }
        }
        // Conditional sections — `data-show-when` lists the step types that
        // reveal this block. Switched by _plUpdateConditionals on type change.
        return ''
            + '<div class="pl-step-card" data-pl-step="' + idx + '">'
            +   '<div class="pl-step-header">'
            +     '<input type="text" class="modal-input" placeholder="Step name" data-field="name" value="' + escapeHtml(data.name || '') + '">'
            +     '<select class="modal-select" data-field="step_type" style="width:auto">'
            +       _plOption('agent', 'Agent', stepType)
            +       _plOption('human', 'Human', stepType)
            +       _plOption('automated', 'Automated', stepType)
            +     '</select>'
            +     '<button type="button" class="btn btn-xs btn-secondary" data-action-remove-step="' + idx + '" title="Remove step">&times;</button>'
            +   '</div>'
            +   '<div class="pl-step-fields">'
            +     '<div>'
            +       '<label class="form-label">Step ID <span class="hint">(referenced by deps)</span></label>'
            +       '<input type="text" class="modal-input" data-field="id" value="' + escapeHtml(data.id || ('step' + idx)) + '">'
            +     '</div>'
            +     '<div>'
            +       '<label class="form-label">Schedule <span class="hint">(optional)</span></label>'
            +       '<input type="text" class="modal-input" data-field="schedule" placeholder="HH:MM or cron" value="' + escapeHtml(data.schedule || '') + '">'
            +     '</div>'
            +     '<div class="pl-full">'
            +       '<label class="form-label">Description</label>'
            +       '<textarea rows="2" class="modal-textarea" data-field="description" placeholder="What does this step do?">' + escapeHtml(data.description || '') + '</textarea>'
            +     '</div>'
            +     '<div class="pl-full" data-show-when="agent,human">'
            +       '<label class="form-label">Depends on <span class="hint">(other steps that must finish first)</span></label>'
            +       '<div class="pl-chip-picker" data-field="depends_on" data-deps="' + escapeHtml((data.depends_on || []).join(',')) + '"></div>'
            +     '</div>'
            +     '<div data-show-when="agent">'
            +       '<label class="form-label">Assigned worker</label>'
            +       '<select class="modal-select" data-field="assigned_worker">' + workerOpts + '</select>'
            +     '</div>'
            +     '<div data-show-when="agent">'
            +       '<label class="form-label">Task type</label>'
            +       '<select class="modal-select" data-field="task_type">' + taskTypeOpts + '</select>'
            +     '</div>'
            +     '<div data-show-when="automated">'
            +       '<label class="form-label">Service</label>'
            +       '<select class="modal-select" data-field="service">' + serviceOpts + '</select>'
            +       '<div class="text-muted text-xs mt-sm" data-role="service-desc"></div>'
            +     '</div>'
            +     '<div data-show-when="automated">'
            +       '<label class="form-label">Config <button type="button" class="btn btn-xs btn-secondary" data-action-fill-example="' + idx + '">Use example</button></label>'
            +       '<textarea rows="4" class="modal-textarea" data-field="config" placeholder=\'{"key": "value"}\' style="font-family:monospace;font-size:0.75rem">' + escapeHtml(configText) + '</textarea>'
            +       '<div class="text-muted text-xs" data-role="config-error" style="color:var(--poppy);display:none"></div>'
            +     '</div>'
            +   '</div>'
            + '</div>';
    }

    function _plUpdateConditionals(card) {
        // Show/hide fields based on the step_type select on this card.
        var type = (card.querySelector('[data-field="step_type"]') || {}).value || 'agent';
        card.querySelectorAll('[data-show-when]').forEach(function(el) {
            var allowed = (el.dataset.showWhen || '').split(',');
            el.style.display = allowed.indexOf(type) >= 0 ? '' : 'none';
        });
    }

    function _plUpdateServiceDesc(card) {
        // When the service dropdown changes, surface the handler's
        // description so the operator knows what they picked.
        var sel = card.querySelector('[data-field="service"]');
        if (!sel) return;
        var hint = card.querySelector('[data-role="service-desc"]');
        if (!hint) return;
        var svc = (_plServiceCache || []).find(function(s) { return s.name === sel.value; });
        hint.textContent = (svc && svc.description) || '';
    }

    function _plRenderDepChips() {
        // Each step's dep picker shows chips for every OTHER defined step,
        // with the currently-selected deps highlighted. Re-rendered whenever
        // a step is added, removed, or its ID changes.
        var cards = document.querySelectorAll('#pl-steps-list [data-pl-step]');
        var all = [];
        cards.forEach(function(c) {
            var id = (c.querySelector('[data-field="id"]') || {}).value || '';
            var name = (c.querySelector('[data-field="name"]') || {}).value || '';
            if (id) all.push({ id: id, label: name ? (id + ': ' + name) : id });
        });
        cards.forEach(function(c) {
            var picker = c.querySelector('[data-field="depends_on"]');
            if (!picker) return;
            var ownId = (c.querySelector('[data-field="id"]') || {}).value || '';
            var selected = (picker.dataset.deps || '').split(',').map(function(s) { return s.trim(); }).filter(Boolean);
            var others = all.filter(function(s) { return s.id !== ownId; });
            if (!others.length) {
                picker.innerHTML = '<span class="pl-chip-empty">Add more steps first</span>';
                return;
            }
            picker.innerHTML = others.map(function(s) {
                var on = selected.indexOf(s.id) >= 0;
                return '<span class="pl-chip' + (on ? ' pl-chip-on' : '')
                    + '" data-action-toggle-dep="' + escapeHtml(s.id) + '">'
                    + escapeHtml(s.label) + '</span>';
            }).join('');
        });
    }

    function _plCollectSteps() {
        // Walk every step card, validate, and assemble the wire payload.
        // Returns { steps, errors }; caller decides whether to submit.
        var steps = [];
        var errors = [];
        var seenIds = Object.create(null);
        var cards = document.querySelectorAll('#pl-steps-list [data-pl-step]');
        cards.forEach(function(card) {
            var step = { config: {}, depends_on: [] };
            card.querySelectorAll('[data-field]').forEach(function(el) {
                var key = el.dataset.field;
                if (key === 'depends_on') {
                    step.depends_on = (el.dataset.deps || '').split(',').map(function(s) { return s.trim(); }).filter(Boolean);
                    return;
                }
                if (key === 'config') {
                    var raw = (el.value || '').trim();
                    var err = card.querySelector('[data-role="config-error"]');
                    if (err) { err.style.display = 'none'; err.textContent = ''; }
                    if (!raw) return;
                    try { step.config = JSON.parse(raw); }
                    catch (parseErr) {
                        if (err) {
                            err.textContent = 'Invalid JSON: ' + parseErr.message;
                            err.style.display = '';
                        }
                        errors.push('Step ' + (step.id || '?') + ': invalid JSON config');
                    }
                    return;
                }
                var v = (el.value || '').trim();
                if (v) step[key] = v;
            });
            if (!step.name) return;  // silently drop empty step rows
            if (!step.id) {
                errors.push('Step "' + step.name + '" needs an ID');
                return;
            }
            if (seenIds[step.id]) {
                errors.push('Duplicate step ID: ' + step.id);
                return;
            }
            seenIds[step.id] = true;
            // Type defaulting + per-type validation.
            step.step_type = step.step_type || 'agent';
            if (step.step_type === 'automated' && !step.service) {
                errors.push('Automated step "' + step.id + '" needs a service');
            }
            steps.push(step);
        });
        // Circular-dep check (DFS).
        if (!errors.length && steps.length) {
            var byId = {};
            steps.forEach(function(s) { byId[s.id] = s; });
            // Drop deps pointing at steps that no longer exist — silently
            // cleaning these is friendlier than blocking the save.
            steps.forEach(function(s) {
                s.depends_on = (s.depends_on || []).filter(function(d) { return !!byId[d]; });
            });
            var WHITE = 0, GRAY = 1, BLACK = 2;
            var color = {};
            steps.forEach(function(s) { color[s.id] = WHITE; });
            function dfs(id) {
                if (color[id] === GRAY) return true;  // cycle
                if (color[id] === BLACK) return false;
                color[id] = GRAY;
                var deps = (byId[id] && byId[id].depends_on) || [];
                for (var i = 0; i < deps.length; i++) {
                    if (dfs(deps[i])) return true;
                }
                color[id] = BLACK;
                return false;
            }
            for (var k = 0; k < steps.length; k++) {
                if (dfs(steps[k].id)) { errors.push('Circular dependency involving step ' + steps[k].id); break; }
            }
        }
        return { steps: steps, errors: errors };
    }

    function _plRenderSteps(existingSteps) {
        var list = document.getElementById('pl-steps-list');
        list.innerHTML = '';
        _plStepCounter = 0;
        var arr = existingSteps && existingSteps.length ? existingSteps : [{}];
        var html = arr.map(_plRenderStepCard).join('');
        list.innerHTML = html;
        list.querySelectorAll('[data-pl-step]').forEach(function(c) {
            _plUpdateConditionals(c);
            _plUpdateServiceDesc(c);
        });
        _plRenderDepChips();
    }

    function _plOpenModal(title, submitLabel) {
        document.getElementById('pl-modal-title').textContent = title;
        document.getElementById('pl-submit-btn').textContent = submitLabel;
        var v = document.getElementById('pl-validation');
        if (v) { v.style.display = 'none'; v.textContent = ''; }
        document.getElementById('pipeline-modal').style.display = 'flex';
    }

    function _plResetScheduleControls() {
        // Wipe the per-preset inputs so each open is a clean slate.
        var dt = document.getElementById('pl-daily-time'); if (dt) dt.value = '09:00';
        var wt = document.getElementById('pl-weekly-time'); if (wt) wt.value = '09:00';
        var wdt = document.getElementById('pl-weekdays-time'); if (wdt) wdt.value = '09:00';
        var hm = document.getElementById('pl-hourly-minute'); if (hm) hm.value = '0';
        var ci = document.getElementById('pl-cron-input'); if (ci) ci.value = '';
        document.querySelectorAll('#pl-weekly-days input').forEach(function(c) { c.checked = false; });
    }

    window.showCreatePipeline = function() {
        _plMode = 'create';
        _plEditingId = null;
        document.getElementById('pl-name').value = '';
        document.getElementById('pl-desc').value = '';
        document.getElementById('pl-tags').value = '';
        _plPopulateTimezones('');
        _plActivePreset = 'ondemand';
        _plResetScheduleControls();
        _plActivePresetButton();
        _plUpdateSchedulePreview();
        _plLoadCatalogs().then(function() {
            _plRenderSteps([]);
            _plOpenModal('New Pipeline', 'Create');
        });
    };

    window.showEditPipeline = function(pipelineId) {
        _plMode = 'edit';
        _plEditingId = pipelineId;
        fetch('/api/pipelines/' + pipelineId, { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) {
                if (!r.ok) throw new Error('not found');
                return r.json();
            })
            .then(function(p) {
                document.getElementById('pl-name').value = p.name || '';
                document.getElementById('pl-desc').value = p.description || '';
                document.getElementById('pl-tags').value = (p.tags || []).join(', ');
                _plPopulateTimezones(p.timezone || '');
                _plResetScheduleControls();
                // Try to reconstruct a preset from the FIRST step's schedule
                // — pipeline-level "default schedule" is a create-time
                // convenience; in edit mode every step holds its own value.
                var firstSched = (p.steps && p.steps[0] && p.steps[0].schedule) || '';
                _plLoadPresetFromCron(firstSched);
                _plActivePresetButton();
                _plUpdateSchedulePreview();
                return _plLoadCatalogs().then(function() {
                    _plRenderSteps(p.steps || []);
                    _plOpenModal('Edit Pipeline — ' + (p.name || ''), 'Save');
                });
            })
            .catch(function() { showToast('Could not load pipeline', true); });
    };

    window.hidePipelineModal = function() {
        document.getElementById('pipeline-modal').style.display = 'none';
    };

    window.submitPipeline = function() {
        var name = document.getElementById('pl-name').value.trim();
        var validationEl = document.getElementById('pl-validation');
        if (validationEl) { validationEl.style.display = 'none'; validationEl.textContent = ''; }
        if (!name) { showToast('Name required', true); return; }
        var desc = document.getElementById('pl-desc').value.trim();
        var tagsRaw = document.getElementById('pl-tags').value.trim();
        var tags = tagsRaw ? tagsRaw.split(',').map(function(s) { return s.trim(); }).filter(Boolean) : [];
        var collected = _plCollectSteps();
        if (collected.errors.length) {
            if (validationEl) {
                validationEl.textContent = collected.errors.join('  ·  ');
                validationEl.style.display = '';
            }
            showToast(collected.errors[0], true);
            return;
        }
        // Pipeline-level schedule fills in for any step that didn't set its
        // own — both at create time and edit time. The hidden #pl-schedule
        // input was filled in by _plUpdateSchedulePreview from whichever
        // preset is active, so the operator submits exactly what they saw
        // in the live preview.
        var schedule = (document.getElementById('pl-schedule').value || '').trim();
        if (schedule) {
            collected.steps.forEach(function(s) {
                if (!s.schedule) s.schedule = schedule;
            });
        }
        var timezone = (document.getElementById('pl-timezone').value || '').trim();
        var body = {
            name: name,
            description: desc,
            tags: tags,
            steps: collected.steps,
            timezone: timezone,
        };
        var url = '/api/pipelines' + (_plMode === 'edit' ? '/' + _plEditingId : '');
        var method = _plMode === 'edit' ? 'PUT' : 'POST';
        fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
            body: JSON.stringify(body),
        })
            .then(function(r) {
                return r.json().then(function(data) { return { ok: r.ok, status: r.status, data: data }; });
            })
            .then(function(resp) {
                if (resp.ok && (resp.data.id || resp.data.ok)) {
                    showToast(_plMode === 'edit' ? 'Pipeline saved' : 'Pipeline created');
                    hidePipelineModal();
                    refreshPipelines();
                } else {
                    var msg = (resp.data && resp.data.error) || ('HTTP ' + resp.status);
                    showToast(msg, true);
                    if (validationEl) {
                        validationEl.textContent = msg;
                        validationEl.style.display = '';
                    }
                }
            })
            .catch(function() {
                showToast(_plMode === 'edit' ? 'Save failed' : 'Create failed', true);
            });
    };

    // Step-list event delegation: add, remove, toggle deps, fill example,
    // switch type, edit step ID (re-renders deps so chips stay valid).
    document.getElementById('pl-add-step').addEventListener('click', function() {
        var list = document.getElementById('pl-steps-list');
        list.insertAdjacentHTML('beforeend', _plRenderStepCard({}));
        var newCard = list.lastElementChild;
        _plUpdateConditionals(newCard);
        _plRenderDepChips();
    });

    document.getElementById('pl-steps-list').addEventListener('click', function(e) {
        var rm = e.target.closest('[data-action-remove-step]');
        if (rm) {
            var card = rm.closest('[data-pl-step]');
            if (card) { card.remove(); _plRenderDepChips(); }
            return;
        }
        var dep = e.target.closest('[data-action-toggle-dep]');
        if (dep) {
            var picker = dep.closest('[data-field="depends_on"]');
            if (!picker) return;
            var current = (picker.dataset.deps || '').split(',').map(function(s) { return s.trim(); }).filter(Boolean);
            var depId = dep.dataset.actionToggleDep;
            var pos = current.indexOf(depId);
            if (pos >= 0) current.splice(pos, 1); else current.push(depId);
            picker.dataset.deps = current.join(',');
            _plRenderDepChips();
            return;
        }
        var ex = e.target.closest('[data-action-fill-example]');
        if (ex) {
            var c = ex.closest('[data-pl-step]');
            var svcSel = c && c.querySelector('[data-field="service"]');
            if (!svcSel || !svcSel.value) { showToast('Pick a service first', true); return; }
            var svc = (_plServiceCache || []).find(function(s) { return s.name === svcSel.value; });
            if (!svc) return;
            var ta = c.querySelector('[data-field="config"]');
            if (ta) ta.value = JSON.stringify(svc.example_config || {}, null, 2);
        }
    });

    document.getElementById('pl-steps-list').addEventListener('change', function(e) {
        var card = e.target.closest('[data-pl-step]');
        if (!card) return;
        if (e.target.dataset.field === 'step_type') _plUpdateConditionals(card);
        if (e.target.dataset.field === 'service') _plUpdateServiceDesc(card);
        if (e.target.dataset.field === 'id') _plRenderDepChips();
    });

    document.getElementById('pl-steps-list').addEventListener('input', function(e) {
        // Re-render dep chips as the operator types step names so the label
        // ("step1: Build") stays in sync. Cheap — just rewrites innerHTML.
        if (e.target.dataset && (e.target.dataset.field === 'id' || e.target.dataset.field === 'name')) {
            _plRenderDepChips();
        }
        // Per-step schedule preview: debounced call to the same backend
        // endpoint so the operator sees what they'll get.
        if (e.target.dataset && e.target.dataset.field === 'schedule') {
            _plScheduleStepPreview(e.target);
        }
    });

    function _plScheduleStepPreview(input) {
        var card = input.closest('[data-pl-step]');
        if (!card) return;
        var stepId = card.dataset.plStep;
        var existing = card.querySelector('.pl-step-preview');
        if (!existing) {
            existing = document.createElement('div');
            existing.className = 'pl-step-preview';
            input.parentElement.appendChild(existing);
        }
        var expr = (input.value || '').trim();
        if (!expr) { existing.textContent = ''; existing.className = 'pl-step-preview'; return; }
        if (_plStepPreviewTimers[stepId]) clearTimeout(_plStepPreviewTimers[stepId]);
        _plStepPreviewTimers[stepId] = setTimeout(function() {
            var tz = (document.getElementById('pl-timezone') || {}).value || '';
            fetch('/api/pipelines/schedule/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
                body: JSON.stringify({ schedule: expr, timezone: tz, count: 2 }),
            })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (d.valid) {
                        existing.className = 'pl-step-preview';
                        existing.textContent = d.human + (d.next && d.next.length ? '  ·  next: ' + d.next[0] : '');
                    } else {
                        existing.className = 'pl-step-preview pl-step-preview-err';
                        existing.textContent = d.error || 'Invalid schedule';
                    }
                })
                .catch(function() {});
        }, 250);
    }

    // P2: preset button + input listeners for the pipeline-level schedule
    // builder. Any change funnels through _plUpdateSchedulePreview which
    // both rebuilds the hidden cron AND requests a live preview.
    document.addEventListener('click', function(e) {
        var btn = e.target.closest('[data-action-preset]');
        if (!btn) return;
        _plActivePreset = btn.dataset.actionPreset;
        _plActivePresetButton();
        _plUpdateSchedulePreview();
    });

    ['pl-daily-time', 'pl-weekly-time', 'pl-weekdays-time', 'pl-hourly-minute', 'pl-cron-input'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.addEventListener('input', _plUpdateSchedulePreview);
    });
    document.querySelectorAll('#pl-weekly-days input').forEach(function(c) {
        c.addEventListener('change', _plUpdateSchedulePreview);
    });
    var _plTzSel = document.getElementById('pl-timezone');
    if (_plTzSel) _plTzSel.addEventListener('change', _plUpdateSchedulePreview);

    // -----------------------------------------------------------------------
    // P3: Pipeline detail view
    // -----------------------------------------------------------------------
    //
    // - Click anywhere on a pipeline card opens this read-only inspector.
    // - Step list grouped by execution wave (Kahn-style levelization).
    // - For each step: status + duration + linked task chip + error +
    //   pretty-printed result. shell_command results get stdout/stderr/
    //   returncode pulled out above the raw JSON.
    // - Retry button on FAILED steps cascade-resets FAILED downstream.
    // - Re-renders on `pipelines_changed` WS events for live updates.

    var _pldViewingId = null;     // pipeline id currently on screen, or null
    var _pldLastData = null;      // last response from /api/pipelines/{id}
    var _pldResultCache = {};     // step_id → raw result dict (for copy)

    function _pldComputeWaves(steps) {
        // Kahn-style: wave 0 = steps with no deps. Wave N = steps whose
        // deps all live in earlier waves. A step with a missing dep ID
        // is placed in wave 0 — we don't drop it, we surface it.
        var byId = {};
        steps.forEach(function(s) { byId[s.id] = s; });
        var waveOf = {};
        function depth(id, stack) {
            if (waveOf[id] !== undefined) return waveOf[id];
            if (stack && stack.indexOf(id) >= 0) return 0;  // defensive
            var s = byId[id];
            if (!s || !s.depends_on || !s.depends_on.length) {
                waveOf[id] = 0;
                return 0;
            }
            var max = -1;
            (s.depends_on || []).forEach(function(d) {
                if (!byId[d]) return;  // dangling dep; skip
                max = Math.max(max, depth(d, (stack || []).concat(id)));
            });
            waveOf[id] = max + 1;
            return waveOf[id];
        }
        steps.forEach(function(s) { depth(s.id); });
        var waves = {};
        steps.forEach(function(s) {
            var w = waveOf[s.id] || 0;
            if (!waves[w]) waves[w] = [];
            waves[w].push(s);
        });
        var levels = Object.keys(waves).map(function(k) { return parseInt(k, 10); }).sort(function(a, b) { return a - b; });
        return levels.map(function(l) { return { level: l, steps: waves[l] }; });
    }

    function _pldDurationLabel(step) {
        if (!step.started_at) return '';
        var end = step.completed_at || (Date.now() / 1000);
        var seconds = Math.max(0, Math.floor(end - step.started_at));
        if (seconds < 60) return seconds + 's';
        if (seconds < 3600) {
            var m = Math.floor(seconds / 60), s = seconds % 60;
            return m + 'm ' + s + 's';
        }
        var h = Math.floor(seconds / 3600), rm = Math.floor((seconds % 3600) / 60);
        return h + 'h ' + rm + 'm';
    }

    function _pldFmtTime(epoch) {
        if (!epoch) return '';
        try { return new Date(epoch * 1000).toLocaleString(); }
        catch (e) { return ''; }
    }

    function _pldStatusIcon(status) {
        return status === 'completed' ? '✓'
            : status === 'in_progress' ? '●'
            : status === 'failed' ? '✗'
            : status === 'skipped' ? '⊘'
            : status === 'ready' ? '◎'
            : '○';
    }

    function _pldRenderResult(step) {
        // Pretty JSON + Copy button. For shell_command, surface
        // stdout/stderr/returncode as labeled blocks above the raw JSON
        // since that's the common case.
        if (!step.result || !Object.keys(step.result).length) return '';
        _pldResultCache[step.id] = step.result;
        var out = '';
        var r = step.result;
        var isShell = (step.service === 'shell_command') ||
            ('stdout' in r && 'returncode' in r);
        if (isShell) {
            if (r.stdout) {
                out += '<div class="pld-result-block"><div class="pld-result-label">stdout</div>'
                    + '<div class="pld-result-pre">' + escapeHtml(String(r.stdout)) + '</div></div>';
            }
            if (r.stderr) {
                out += '<div class="pld-result-block"><div class="pld-result-label">stderr</div>'
                    + '<div class="pld-result-pre">' + escapeHtml(String(r.stderr)) + '</div></div>';
            }
            if (r.returncode !== undefined) {
                out += '<div class="pld-result-block"><div class="pld-result-label">returncode</div>'
                    + '<div class="pld-result-pre">' + escapeHtml(String(r.returncode)) + '</div></div>';
            }
        }
        var raw;
        try { raw = JSON.stringify(r, null, 2); } catch (e) { raw = '[unserializable]'; }
        out += '<div class="pld-result-block">'
            + '<div class="flex-between"><div class="pld-result-label">result (JSON)</div>'
            + '<button class="btn btn-xs btn-secondary" data-action="copyStepResult" data-step-id="' + escapeHtml(step.id) + '">Copy</button></div>'
            + '<div class="pld-result-pre">' + escapeHtml(raw) + '</div></div>';
        return out;
    }

    function _pldRenderStep(step, pipelineId) {
        var statusClass = 'status-' + step.status;
        var icon = _pldStatusIcon(step.status);
        var html = '<div class="pld-step ' + statusClass + '">';
        html += '<div class="pld-step-header">';
        html += '<span>' + icon + '</span>';
        html += '<span class="pld-step-title">' + escapeHtml(step.name) + '</span>';
        html += '<span class="conf-badge" style="background:var(--panel);border:1px solid var(--border)">' + escapeHtml(step.step_type) + '</span>';
        html += '<span class="conf-badge" style="background:var(--panel);border:1px solid var(--border)">' + escapeHtml(step.status) + '</span>';
        var dur = _pldDurationLabel(step);
        if (dur) html += '<span class="pld-step-meta">' + dur + '</span>';
        if (step.task_id) {
            html += '<span class="pld-task-chip" data-action="openLinkedTask" data-task-id="' + escapeHtml(step.task_id) + '" title="Jump to task">#' + escapeHtml(step.task_id) + '</span>';
        }
        if (step.assigned_worker) {
            html += '<span class="pld-step-meta">→ ' + escapeHtml(step.assigned_worker) + '</span>';
        }
        html += '</div>';
        if (step.depends_on && step.depends_on.length) {
            html += '<div class="pld-step-meta pld-dep-list">← blocked by ' + step.depends_on.map(escapeHtml).join(', ') + '</div>';
        }
        if (step.description) {
            html += '<div class="pld-step-body">' + escapeHtml(step.description) + '</div>';
        }
        if (step.step_type === 'automated' && step.service) {
            html += '<div class="pld-step-body pld-step-meta">service: ' + escapeHtml(step.service);
            if (step.config && Object.keys(step.config).length) {
                var cfg;
                try { cfg = JSON.stringify(step.config, null, 2); } catch (e) { cfg = '[unserializable]'; }
                html += '<div class="pld-result-block"><div class="pld-result-label">config</div>'
                    + '<div class="pld-result-pre">' + escapeHtml(cfg) + '</div></div>';
            }
            html += '</div>';
        }
        if (step.schedule) {
            html += '<div class="pld-step-meta">schedule: ' + escapeHtml(step.schedule) + '</div>';
        }
        if (step.started_at) {
            html += '<div class="pld-step-meta">started: ' + escapeHtml(_pldFmtTime(step.started_at));
            if (step.completed_at) html += '  ·  finished: ' + escapeHtml(_pldFmtTime(step.completed_at));
            html += '</div>';
        }
        if (step.error) {
            html += '<div class="pld-step-error">' + escapeHtml(String(step.error)) + '</div>';
        }
        html += _pldRenderResult(step);
        // Per-step actions: Retry (FAILED, or COMPLETED with confirm),
        // Skip (READY/IN_PROGRESS), Mark done (human steps in
        // READY/IN_PROGRESS) — mirrors what the list view already
        // does for skip/done.
        var actions = [];
        if (step.status === 'failed') {
            actions.push('<button class="btn btn-sm btn-approve" data-action="retryStep" data-pipeline-id="' + escapeHtml(pipelineId) + '" data-step-id="' + escapeHtml(step.id) + '">Retry</button>');
        }
        // Cleanup batch: COMPLETED retry behind a confirmation modal.
        // The warning prefix signals the side-effect risk; the click
        // opens the confirm modal rather than firing the request.
        if (step.status === 'completed') {
            actions.push('<button class="btn btn-sm btn-amber" data-action="retryStepCompleted" data-pipeline-id="' + escapeHtml(pipelineId) + '" data-step-id="' + escapeHtml(step.id) + '" title="Re-run this completed step (may have side effects)">⚠ Retry</button>');
        }
        if (step.status === 'ready' || step.status === 'in_progress') {
            if (step.step_type === 'human') {
                actions.push('<button class="btn btn-sm btn-approve" onclick="completeStep(\'' + pipelineId + '\',\'' + step.id + '\')">Mark done</button>');
            }
            actions.push('<button class="btn btn-sm btn-secondary" onclick="skipStep(\'' + pipelineId + '\',\'' + step.id + '\')">Skip</button>');
        }
        if (actions.length) {
            html += '<div class="pld-step-actions">' + actions.join('') + '</div>';
        }
        html += '</div>';
        return html;
    }

    function _pldRender(data) {
        _pldLastData = data;
        _pldResultCache = {};
        var body = document.getElementById('pld-body');
        if (!body) return;
        // Title bar
        document.getElementById('pld-title').textContent = data.name || 'Pipeline';
        var editBtn = document.getElementById('pld-edit-btn');
        if (editBtn) {
            editBtn.style.display = (data.status === 'draft' || data.status === 'paused') ? '' : 'none';
        }
        // Header — pipeline metadata
        var header = '<div class="pld-header">';
        if (data.description) header += '<div class="text-sm mb-sm">' + escapeHtml(data.description) + '</div>';
        var headerBits = [];
        headerBits.push('status: <strong>' + escapeHtml(data.status) + '</strong>');
        var pctDone = Math.round((data.progress || 0) * 100);
        if (data.steps && data.steps.length) {
            var done = data.steps.filter(function(s) { return s.status === 'completed' || s.status === 'skipped'; }).length;
            pctDone = Math.round((done / data.steps.length) * 100);
            headerBits.push(done + ' / ' + data.steps.length + ' steps (' + pctDone + '%)');
        }
        if (data.timezone) headerBits.push('tz: <span class="pld-tag">' + escapeHtml(data.timezone) + '</span>');
        if (data.template_name) headerBits.push('template: ' + escapeHtml(data.template_name));
        if (data.tags && data.tags.length) headerBits.push('tags: ' + data.tags.map(escapeHtml).join(', '));
        if (data.created_at) headerBits.push('created: ' + escapeHtml(_pldFmtTime(data.created_at)));
        header += '<div class="pld-header-row">' + headerBits.join(' · ') + '</div>';
        header += '</div>';
        // Wave-grouped step list
        var stepsHtml = '';
        if (!data.steps || !data.steps.length) {
            stepsHtml = '<div class="empty-state">No steps</div>';
        } else {
            var waves = _pldComputeWaves(data.steps);
            waves.forEach(function(w) {
                stepsHtml += '<div class="pld-wave">';
                stepsHtml += '<div class="pld-wave-label">Wave ' + (w.level + 1) + '</div>';
                w.steps.forEach(function(s) { stepsHtml += _pldRenderStep(s, data.id); });
                stepsHtml += '</div>';
            });
        }
        body.innerHTML = header + stepsHtml;
    }

    window.showPipelineDetail = function(pipelineId) {
        _pldViewingId = pipelineId;
        document.getElementById('pipeline-detail-modal').style.display = 'flex';
        document.getElementById('pld-body').innerHTML = '<div class="empty-state">Loading…</div>';
        fetch('/api/pipelines/' + pipelineId, { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) {
                if (!r.ok) throw new Error('not found');
                return r.json();
            })
            .then(_pldRender)
            .catch(function() {
                document.getElementById('pld-body').innerHTML = '<div class="empty-state text-poppy">Pipeline not found</div>';
            });
    };

    window.hidePipelineDetail = function() {
        _pldViewingId = null;
        _pldLastData = null;
        _pldResultCache = {};
        document.getElementById('pipeline-detail-modal').style.display = 'none';
    };

    window.editFromDetail = function() {
        if (!_pldViewingId) return;
        var pid = _pldViewingId;
        hidePipelineDetail();
        showEditPipeline(pid);
    };

    // Internal: actually POST to the retry endpoint. Used by both the
    // direct retryStep (FAILED steps) and the confirmRetry path
    // (COMPLETED steps after the modal gate).
    function _retryStepPost(pipelineId, stepId, confirmed) {
        var body = confirmed ? JSON.stringify({ confirmed: true }) : '{}';
        return fetch('/api/pipelines/' + pipelineId + '/steps/' + stepId + '/retry', {
            method: 'POST',
            headers: { 'X-Requested-With': 'Dashboard', 'Content-Type': 'application/json' },
            body: body,
        })
            .then(function(r) {
                return r.json().then(function(d) { return { ok: r.ok, status: r.status, data: d }; });
            })
            .then(function(resp) {
                if (!resp.ok) {
                    showToast(resp.data.error || ('Retry failed (' + resp.status + ')'), true);
                    return;
                }
                var reset = resp.data.reset || [];
                if (reset.length > 1) {
                    showToast('Retried ' + stepId + ' (also reset: ' + reset.slice(1).join(', ') + ')');
                } else {
                    showToast('Retried ' + stepId);
                }
                refreshPipelines();
                if (_pldViewingId === pipelineId) showPipelineDetail(pipelineId);
            })
            .catch(function() { showToast('Retry failed', true); });
    }

    window.retryStep = function(pipelineId, stepId) {
        _retryStepPost(pipelineId, stepId, false);
    };

    // Cleanup batch: retry-on-COMPLETED gating. Opens the confirmation
    // modal with the (pipelineId, stepId) staged; confirmRetry fires the
    // actual POST with confirmed:true.
    var _pendingRetry = null;  // {pipelineId, stepId} while modal is open
    window.retryStepCompleted = function(pipelineId, stepId) {
        _pendingRetry = { pipelineId: pipelineId, stepId: stepId };
        var cb = document.getElementById('retry-confirm-checkbox');
        var go = document.getElementById('retry-confirm-go');
        if (cb) { cb.checked = false; }
        if (go) { go.disabled = true; }
        document.getElementById('retry-confirm-modal').style.display = 'flex';
    };

    window.hideRetryConfirm = function() {
        _pendingRetry = null;
        document.getElementById('retry-confirm-modal').style.display = 'none';
    };

    window.confirmRetry = function() {
        if (!_pendingRetry) return;
        var info = _pendingRetry;
        _retryStepPost(info.pipelineId, info.stepId, true);
        hideRetryConfirm();
    };

    // Checkbox toggles the Retry button's disabled state — operator
    // can't fire without explicitly acknowledging the side-effect risk.
    (function () {
        var cb = document.getElementById('retry-confirm-checkbox');
        var go = document.getElementById('retry-confirm-go');
        if (!cb || !go) return;
        cb.addEventListener('change', function () { go.disabled = !cb.checked; });
    })();

    window.openLinkedTask = function(taskId) {
        // Cleanup batch follow-up: deep-link by ID via the new
        // /api/tasks/{id} endpoint. Closes the detail modal, switches
        // to the Tasks tab, fetches the task, and opens the editor
        // pre-filled. Falls back to the scroll-and-flash behaviour if
        // the fetch 404s (race with deletion) or rejects.
        hidePipelineDetail();
        if (typeof switchTab === 'function') switchTab('tasks');
        if (typeof showTaskEditorById === 'function') {
            setTimeout(function() { showTaskEditorById(taskId); }, 60);
            return;
        }
        // Defensive fallback — should never hit since showTaskEditorById
        // is defined right below. Kept as belt-and-suspenders.
        setTimeout(function() {
            var row = document.querySelector('[data-task-id="' + taskId + '"]')
                || document.querySelector('[data-row-task-id="' + taskId + '"]')
                || document.querySelector('[data-id="' + taskId + '"]');
            if (row) {
                try { row.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e) {}
                row.classList.add('task-row-flash');
                setTimeout(function() { row.classList.remove('task-row-flash'); }, 1500);
            } else {
                showToast('Task #' + taskId + ' — scroll the Tasks tab to find it');
            }
        }, 80);
    };

    // Cleanup batch: ID-addressable task editor opener. The existing
    // openTaskModal('edit', data) takes a flat dict from the row's data-*
    // attributes; this wrapper fetches /api/tasks/{id} and translates the
    // SwarmTask JSON into that flat dict so deep-links work without the
    // operator having to find the row first.
    window.showTaskEditorById = function(taskId) {
        fetch('/api/tasks/' + encodeURIComponent(taskId), {
            headers: { 'X-Requested-With': 'Dashboard' },
        })
            .then(function(r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function(t) {
                // Translate SwarmTask JSON → openTaskModal('edit', data)
                // shape. Field names diverge slightly (depends_on → deps,
                // dependency_type → dep_type, acceptance_criteria → acceptance).
                openTaskModal('edit', {
                    id: t.id,
                    title: t.title || '',
                    desc: t.description || '',
                    priority: t.priority || 'normal',
                    task_type: t.task_type || '',
                    tags: (t.tags || []).join(','),
                    deps: (t.depends_on || []).join(','),
                    resolution: t.resolution || '',
                    status: t.status || '',
                    is_cross_project: !!t.is_cross_project,
                    source_worker: t.source_worker || '',
                    target_worker: t.target_worker || '',
                    dep_type: t.dependency_type || 'blocks',
                    acceptance: (t.acceptance_criteria || []).join('\n'),
                    context_refs: (t.context_refs || []).join('\n'),
                    attachments: (t.attachments || []).join(','),
                    assigned_worker: t.assigned_worker || '',
                });
            })
            .catch(function(err) {
                showToast('Task ' + taskId + ' not found: ' + (err.message || 'error'), true);
            });
    };

    window.copyStepResult = function(stepId) {
        var data = _pldResultCache[stepId];
        if (!data) return;
        var text;
        try { text = JSON.stringify(data, null, 2); }
        catch (e) { text = String(data); }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function() { showToast('Copied'); });
        } else {
            // Fallback: textarea + execCommand for the rare missing-API case.
            var ta = document.createElement('textarea');
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); showToast('Copied'); }
            catch (e) { showToast('Copy failed', true); }
            document.body.removeChild(ta);
        }
    };

    // Live updates: re-render detail when the pipeline we're viewing
    // changes. The WS message type 'pipelines_changed' already triggers
    // refreshPipelines(); we piggyback on the same handler.
    function _pldOnPipelinesChanged() {
        if (!_pldViewingId) return;
        var pid = _pldViewingId;
        fetch('/api/pipelines/' + pid, { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(d) { if (d && _pldViewingId === pid) _pldRender(d); })
            .catch(function() {});
    }

    // Hook into the existing WS handler — the dashboard fires
    // `refreshPipelines()` on 'pipelines_changed'; do the same here.
    window._pldOnPipelinesChanged = _pldOnPipelinesChanged;

    window.showDecisionModal = function(idx) {
        var d = _decisionCache[idx];
        if (!d) return;
        var type = d.proposal_type || 'assignment';
        var typeLabel = type === 'escalation' ? 'Escalation' : type === 'completion' ? 'Completion' : 'Assignment';
        var status = (d.status || 'approved');
        var statusLabel = status.charAt(0).toUpperCase() + status.slice(1);
        var confPct = Math.round((d.confidence || 0) * 100);
        var ts = new Date(d.created_at * 1000);
        var timeStr = ts.toLocaleString();

        var hdr = document.getElementById('decision-modal-header');
        var statusClass = status === 'approved' ? 'text-leaf' : status === 'rejected' ? 'text-poppy' : 'text-honey';
        hdr.innerHTML = '<img src="/static/bees/queen.svg" class="bee-icon bee-sm" alt=""> '
            + typeLabel + ' &mdash; <span class="' + statusClass + '">' + statusLabel + '</span>';

        var html = '';
        html += row('Time', escapeHtml(timeStr));
        html += row('Worker', escapeHtml(d.worker_name));
        html += row('Confidence', confPct + '%');
        if (d.task_title) html += row('Task', escapeHtml(d.task_title));
        if (d.queen_action) html += row('Action', escapeHtml(d.queen_action));
        if (d.message) html += row('Message', escapeHtml(d.message));
        if (d.reasoning) html += row('Reasoning', escapeHtml(d.reasoning));
        if (d.assessment) html += row('Assessment', escapeHtml(d.assessment));

        document.getElementById('decision-modal-body').innerHTML = html;
        document.getElementById('decision-modal').style.display = 'flex';

        function row(label, value) {
            return '<div class="decision-detail-row">'
                + '<div class="decision-detail-label">' + label + '</div>'
                + '<div class="decision-detail-value">' + value + '</div></div>';
        }
    };
    window.hideDecisionModal = function() {
        document.getElementById('decision-modal').style.display = 'none';
    };

    // --- Proposals ---
    function refreshProposals() {
        fetch('/api/proposals', { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) { return r.json(); })
            .then(function(data) {
                renderProposals(data.proposals || []);
                updateProposalBadge(data.pending_count || 0);
            })
            .catch(function() {});
    }

    var _proposalData = {};
    function renderProposals(proposals) {
        var banner = document.getElementById('proposal-banner');
        var list = document.getElementById('proposal-list');
        var countEl = document.getElementById('proposal-count');
        if (!banner || !list) return;
        if (!proposals.length) {
            banner.style.display = 'none';
            return;
        }
        banner.style.display = 'block';
        if (countEl) countEl.textContent = proposals.length;
        var html = '';
        for (var i = 0; i < proposals.length; i++) {
            var p = proposals[i];
            _proposalData[p.id] = p;
            var isEsc = p.proposal_type === 'escalation';
            var isCompletion = p.proposal_type === 'completion';
            var confPct = Math.round((p.confidence || 1.0) * 100);
            var confClass = confPct >= 70 ? 'conf-high' : confPct >= 40 ? 'conf-mid' : 'conf-low';
            var emailAttr = (isCompletion && p.has_source_email) ? ' data-has-email="1"' : '';
            html += '<div class="proposal-item" data-proposal-id="' + escapeHtml(p.id) + '"' + emailAttr + '>';
            if (isEsc) html += '<span class="conf-badge conf-mid">ESC</span>';
            if (isCompletion) html += '<span class="conf-badge conf-high">DONE</span>';
            html += '<span class="proposal-worker">' + escapeHtml(p.worker_name) + '</span>';
            if (isEsc) {
                html += '<span class="text-beeswax flex-1">' + escapeHtml(p.assessment || p.reasoning || 'Escalation') + '</span>';
            } else if (isCompletion) {
                html += '<span class="text-beeswax flex-1">' + escapeHtml(p.task_title) + '</span>';
            } else {
                html += '<span class="text-muted">&larr;</span>';
                html += '<span class="text-beeswax flex-1">' + escapeHtml(p.task_title) + '</span>';
                if (p.reasoning) {
                    html += '<span class="proposal-reason" title="' + escapeHtml(p.reasoning) + '">' + escapeHtml(p.reasoning) + '</span>';
                }
            }
            html += '<span class="conf-badge ' + confClass + '">Confidence: ' + confPct + '%</span>';
            var age = p.age ? (p.age < 60 ? 'just now' : Math.floor(p.age / 60) + 'm ago') : '';
            html += '<span class="text-muted text-xs">' + age + '</span>';
            var hasEmail = isCompletion && p.has_source_email;
            html += '<button class="btn btn-sm btn-secondary btn-log view-proposal-btn" data-proposal-id="' + escapeHtml(p.id) + '">View</button>';
            html += '<button class="btn btn-sm btn-approve" data-approve-proposal="' + escapeHtml(p.id) + '"' + (hasEmail ? ' data-draft-email="1"' : '') + '>Approve</button>';
            if (isEsc && !p.is_plan) html += '<button class="btn btn-sm btn-secondary" data-approve-always="' + escapeHtml(p.id) + '">Approve Always</button>';
            html += '<button class="btn btn-sm btn-reject-ghost" data-reject-proposal="' + escapeHtml(p.id) + '">Dismiss</button>';
            html += '</div>';
        }
        var savedScroll = list.scrollTop;
        var wasAtBottom = (list.scrollHeight - list.scrollTop - list.clientHeight) < 30;
        list.innerHTML = html;
        list.scrollTop = wasAtBottom ? list.scrollHeight : savedScroll;
    }

    function extractApprovalPattern(proposal) {
        var text = proposal.assessment || proposal.reasoning || '';
        var m = text.match(/`([^`]+)`/);
        if (!m) return '';
        var cmd = m[1].trim().split(/\s+/)[0];
        if (!cmd) return '';
        return '\\b' + cmd.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b';
    }

    window.approveAlwaysProposal = function(id) {
        var p = _proposalData[id];
        if (!p) return;
        hideQueen();
        showRuleModal(p.prompt_snippet || p.assessment || p.reasoning || '', id, p.rule_pattern || '');
    };

    function updateProposalBadge(count) {
        var badge = document.getElementById('proposal-badge');
        if (!badge) return;
        if (count > 0) {
            badge.textContent = count;
            badge.style.display = 'inline-flex';
        } else {
            badge.style.display = 'none';
        }
        updateAppBadge(count);
    }

    function updateQueenQueueBadge(status) {
        var badge = document.getElementById('queen-queue-badge');
        if (!badge) return;
        var running = status.running || 0;
        var queued = status.queued || 0;
        if (running + queued > 0) {
            badge.textContent = queued > 0 ? running + '+' + queued : String(running);
            badge.title = 'Queen: ' + running + ' processing' + (queued > 0 ? ', ' + queued + ' waiting' : '');
            badge.style.display = 'inline-flex';
        } else {
            badge.style.display = 'none';
        }
    }

    window.showProposalDetail = function(proposalId) {
        var p = _proposalData[proposalId];
        if (!p) return;
        if (p.proposal_type === 'escalation') {
            showQueenEscalation({proposal_id:p.id,worker:p.worker_name,assessment:p.assessment||'',reasoning:p.reasoning||'',action:p.queen_action||'',message:p.message||'',confidence:p.confidence||0});
        } else if (p.proposal_type === 'completion') {
            showQueenCompletion({proposal_id:p.id,worker:p.worker_name,task_id:p.task_id||'',task_title:p.task_title||'',assessment:p.assessment||'',reasoning:p.reasoning||'',confidence:p.confidence||0,has_source_email:p.has_source_email||false});
        } else {
            showQueenAssignment(p);
        }
    };

    window.showQueenAssignment = function(data) {
        var modal = document.getElementById('queen-modal');
        var result = document.getElementById('queen-result');
        var confPct = Math.round((data.confidence || 0) * 100);
        var confClass = confPct >= 70 ? 'conf-high' : confPct >= 40 ? 'conf-mid' : 'conf-low';
        var html = '<div class="queen-card queen-card-assign">';
        html += '<div class="queen-card-header">';
        html += '<span class="conf-badge conf-badge-assign"><img src="/static/bees/flying-right.svg" class="bee-icon bee-xs" alt="" style="margin-right:0.2rem">ASSIGN</span>';
        html += '<span class="conf-badge ' + confClass + '">Confidence: ' + confPct + '%</span>';
        html += '</div>';
        html += '<div class="queen-summary">' + queenSummaryLine('assignment', data) + '</div>';
        if (data.reasoning) {
            html += '<div class="mb-sm queen-text-block"><strong class="text-honey">Reasoning</strong><br>' + escapeHtml(data.reasoning) + '</div>';
        }
        if (data.message) {
            html += '<div class="mb-sm"><strong class="text-honey">Message to worker</strong></div>';
            html += '<div class="queen-code-block">' + escapeHtml(data.message) + '</div>';
        }
        html += '</div>';
        if (data.id) {
            html += '<div class="modal-footer">';
            html += '<button class="btn btn-approve" data-approve-proposal="' + escapeHtml(data.id) + '" data-also-hide-queen="1">Approve</button>';
            html += '<button class="btn btn-reject-ghost" data-reject-proposal="' + escapeHtml(data.id) + '" data-also-hide-queen="1">Dismiss</button>';
            html += '</div>';
        }
        result.innerHTML = html;
        modal.style.display = 'flex';
        clearTimeout(queenAutoHideTimer);
        if (isTestMode) {
            queenAutoHideTimer = setTimeout(hideQueen, 4000);
        }
    };

    window.approveProposal = function(id) {
        var body = 'proposal_id=' + encodeURIComponent(id);
        postAction('/action/proposal/approve', body, function(data) {
            if (data.status === 'approved') {
                showToast('Proposal approved');
                delete _proposalData[id];
                removeQueenBannerByProposal(id);
                refreshProposals();
                refreshTasks();
            }
        });
    };

    window.rejectProposal = function(id) {
        postAction('/action/proposal/reject', 'proposal_id=' + encodeURIComponent(id), function(data) {
            if (data.status === 'rejected') {
                showToast('Proposal rejected');
                delete _proposalData[id];
                removeQueenBannerByProposal(id);
                refreshProposals();
            }
        });
    };

    window.approveAllProposals = function() {
        var rows = document.querySelectorAll('.proposal-item');
        var items = [];
        rows.forEach(function(r) {
            if (r.dataset.proposalId) items.push({ id: r.dataset.proposalId, hasEmail: !!r.dataset.hasEmail });
        });
        var chain = Promise.resolve();
        var failed = 0;
        items.forEach(function(item) {
            chain = chain.then(function() {
                var body = 'proposal_id=' + encodeURIComponent(item.id);
                return actionFetch('/action/proposal/approve', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: body
                }).then(function(r) {
                    if (!r.ok) failed++;
                    return r;
                }).catch(function() { failed++; });
            });
        });
        chain.then(function() {
            if (failed) {
                showToast(failed + ' of ' + items.length + ' proposals failed', true);
            } else {
                showToast('All proposals approved');
            }
            refreshProposals();
            refreshTasks();
        }).catch(function() {
            showToast('Approve all failed', true);
            refreshProposals();
        });
    };

    window.rejectAllProposals = function() {
        actionFetch('/action/proposal/reject-all', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            showToast('Dismissed ' + (data.count || 0) + ' proposal(s)');
            refreshProposals();
        });
    };

    // --- Event delegation for proposal/banner buttons (XSS-safe) ---
    document.addEventListener('click', function(e) {
        var btn = e.target.closest('[data-approve-proposal]');
        if (btn) {
            var pid = btn.dataset.approveProposal;
            approveProposal(pid);
            if (btn.dataset.alsoHideQueen) hideQueen();
            if (btn.dataset.removeBanner) removeQueenBanner(btn.dataset.removeBanner);
            return;
        }
        btn = e.target.closest('[data-approve-always]');
        if (btn) {
            approveAlwaysProposal(btn.dataset.approveAlways);
            if (btn.dataset.alsoHideQueen) hideQueen();
            if (btn.dataset.removeBanner) removeQueenBanner(btn.dataset.removeBanner);
            return;
        }
        btn = e.target.closest('[data-reject-proposal]');
        if (btn) {
            var pid = btn.dataset.rejectProposal;
            rejectProposal(pid);
            if (btn.dataset.alsoHideQueen) hideQueen();
            if (btn.dataset.removeBanner) removeQueenBanner(btn.dataset.removeBanner);
            return;
        }
        btn = e.target.closest('[data-add-rule]');
        if (btn) {
            var pat = btn.dataset.addRule;
            var body = new FormData();
            body.append('pattern', pat);
            actionFetch('/action/add-approval-rule', { body: body })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) { showToast('Error: ' + d.error, true); return; }
                showToast('Approval rule added: ' + pat);
            })
            .catch(function() { showToast('Request failed', true); });
            if (btn.dataset.removeBanner) removeQueenBanner(btn.dataset.removeBanner);
            return;
        }
        btn = e.target.closest('[data-banner-custom-rule]');
        if (btn) {
            var bannerEl = document.getElementById(btn.dataset.bannerCustomRule);
            var bannerSnippet = bannerEl ? bannerEl.dataset.promptSnippet || '' : '';
            var bannerPattern = bannerEl ? bannerEl.dataset.rulePattern || '' : '';
            showRuleModal(bannerSnippet, null, bannerPattern);
            if (btn.dataset.removeBanner) removeQueenBanner(btn.dataset.removeBanner);
            return;
        }
        btn = e.target.closest('[data-remove-banner]');
        if (btn && !btn.dataset.approveProposal && !btn.dataset.rejectProposal && !btn.dataset.addRule && !btn.dataset.bannerCustomRule) {
            removeQueenBanner(btn.dataset.removeBanner);
            return;
        }
        btn = e.target.closest('[data-jump-worker]');
        if (btn) {
            jumpToBannerWorker(btn.dataset.jumpWorker, btn.dataset.bannerId);
            return;
        }
    });

    var _staticRetryTimer = null;
    function refreshDetailStatic() {
        if (!selectedWorker) return;
        var body = document.getElementById('detail-body');
        if (body) {
            body.innerHTML = '<div class="empty-state detail-empty-state modal-padding">'
                + '<div class="spinner" style="margin:0 auto 0.75rem"></div>'
                + '<p class="text-muted text-sm">Connecting terminal&hellip;</p>'
                + '</div>';
        }
        if (_staticRetryTimer) clearTimeout(_staticRetryTimer);
        _staticRetryTimer = setTimeout(function retry() {
            _staticRetryTimer = null;
            if (!selectedWorker) return;
            if (typeof Terminal === 'undefined') {
                _staticRetryTimer = setTimeout(retry, 200);
                return;
            }
            attachInlineTerminal(selectedWorker);
        }, 200);
    }

    window.refreshDetail = function() {
        if (!selectedWorker || !_pageReady) return;
        // When inline terminal is live, skip static refresh — it's already live
        if (inlineTerm) return;
        refreshDetailStatic();
    }

    // --- Inline terminal (embedded in detail panel) — cached instances ---
    var TERM_DEBUG_AVAILABLE = _swarmCfg.termDebug;
    var termDebugEnabled = false;
    var termDebugTimer = null;

    function setTermDebugEnabled(enabled) {
        if (!TERM_DEBUG_AVAILABLE) {
            termDebugEnabled = false;
            return;
        }
        termDebugEnabled = !!enabled;
        try { sessionStorage.setItem('swarm_term_debug', termDebugEnabled ? '1' : '0'); } catch (e) {}
        var chip = document.getElementById('term-debug-readout');
        if (chip) chip.style.display = termDebugEnabled ? '' : 'none';
        if (termDebugTimer) {
            clearInterval(termDebugTimer);
            termDebugTimer = null;
        }
        if (termDebugEnabled) {
            termDebugTimer = setInterval(function() {
                var entry = activeTermWorker ? termCache.get(activeTermWorker) : null;
                if (entry && entry.ws && entry.ws.readyState === WebSocket.OPEN) {
                    entry.ws.send(JSON.stringify({ action: 'meta' }));
                }
            }, 1500);
        }
        updateTermDebug();
    }

    function updateTermDebug(entry) {
        if (!TERM_DEBUG_AVAILABLE || !termDebugEnabled) return;
        var chip = document.getElementById('term-debug-readout');
        if (!chip) return;
        var active = entry || (activeTermWorker ? termCache.get(activeTermWorker) : null);
        if (!active || !active.term) {
            chip.textContent = 'no terminal';
            return;
        }
        var term = active.term;
        var dims = { cols: term.cols || 0, rows: term.rows || 0 };
        var fitDims = active.fitAddon && active.fitAddon.proposeDimensions ? active.fitAddon.proposeDimensions() : null;
        var buf = term.buffer && term.buffer.active ? term.buffer.active : null;
        var alt = false;
        if (term.buffer && term.buffer.active && term.buffer.alternate) {
            alt = term.buffer.active === term.buffer.alternate;
        }
        var serverAlt = (typeof active.serverAlt === 'boolean') ? (active.serverAlt ? '1' : '0') : '?';
        var wsState = active.ws ? active.ws.readyState : -1;
        var wsLabel = (wsState === 1 ? 'open' : wsState === 0 ? 'connecting' : wsState === 2 ? 'closing' : 'closed');
        var baseY = buf && typeof buf.baseY === 'number' ? buf.baseY : 0;
        var viewportY = buf && typeof buf.viewportY === 'number' ? buf.viewportY : 0;
        chip.textContent =
            dims.cols + 'x' + dims.rows +
            (fitDims ? ' fit ' + fitDims.cols + 'x' + fitDims.rows : '') +
            ' alt=' + (alt ? '1' : '0') + '/' + serverAlt +
            ' scroll=' + viewportY + '/' + baseY +
            ' ws=' + wsLabel;
    }

    document.addEventListener('DOMContentLoaded', function() {
        if (!TERM_DEBUG_AVAILABLE) return;
        var toggle = document.getElementById('term-debug-toggle');
        if (!toggle) return;
        var saved = '0';
        try { saved = sessionStorage.getItem('swarm_term_debug') || '0'; } catch (e) {}
        toggle.checked = saved === '1';
        setTermDebugEnabled(toggle.checked);
        toggle.addEventListener('change', function() {
            setTermDebugEnabled(toggle.checked);
        });
    });

    /** Update backward-compat aliases to point at the active cache entry. */
    function syncTermAliases(entry) {
        if (entry) {
            inlineTerm = entry.term;
            inlineTermWs = entry.ws;
            inlineFitAddon = entry.fitAddon;
            inlineTermWorker = activeTermWorker;
        } else {
            inlineTerm = null;
            inlineTermWs = null;
            inlineFitAddon = null;
            inlineTermWorker = null;
        }
    }

    /** Create a new terminal + cache entry (does NOT add to DOM yet). */
    function createTermEntry(name) {
        var container = document.createElement('div');
        container.className = 'inline-terminal-container';
        container.style.width = '100%';
        container.style.height = '100%';

        var term = new Terminal({
            // Solid (non-blinking) cursor. A blinking cursor forces xterm to
            // repaint the cursor cell ~2x/sec continuously while a terminal is
            // on screen — real idle CPU for zero functional gain. The cursor is
            // still clearly visible (solid block when focused, hollow when not).
            cursorBlink: false,
            scrollback: 5000,
            fontSize: 14,
            fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
            theme: {
                background: '#2A1B0E',
                foreground: '#E6D2B5',
                cursor: '#D8A03D',
                selectionBackground: 'rgba(216,160,61,0.3)',
                black: '#2A1B0E',
                red: '#D15D4C',
                green: '#8CB369',
                yellow: '#D8A03D',
                blue: '#A88FD9',
                magenta: '#A88FD9',
                cyan: '#7EC8C8',
                white: '#E6D2B5',
            }
        });

        var fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);
        if (typeof ClipboardAddon !== 'undefined' && ClipboardAddon.ClipboardAddon) {
            term.loadAddon(new ClipboardAddon.ClipboardAddon(undefined, new ClipboardAddon.BrowserClipboardProvider()));
        }
        if (typeof WebLinksAddon !== 'undefined' && WebLinksAddon.WebLinksAddon) {
            term.loadAddon(new WebLinksAddon.WebLinksAddon());
        }
        var searchAddon = null;
        if (typeof SearchAddon !== 'undefined' && SearchAddon.SearchAddon) {
            searchAddon = new SearchAddon.SearchAddon();
            term.loadAddon(searchAddon);
        }
        var serializeAddon = null;
        if (typeof SerializeAddon !== 'undefined' && SerializeAddon.SerializeAddon) {
            serializeAddon = new SerializeAddon.SerializeAddon();
            term.loadAddon(serializeAddon);
        }
        term.open(container);
        container.addEventListener('mousedown', function() {
            try { term.focus(); } catch (e) {}
        });

        // GPU-accelerated rendering: WebGL → Canvas → DOM fallback.
        // macOS Chromium/Edge crashes the *whole* renderer through xterm's
        // WebGL path on a redraw (e.g. the selection repaint a right-click
        // triggers) — a hard GPU-process crash, not the graceful onContextLoss
        // this code handles. Symptom: right-click reliably crashes the Swarm
        // tab on Mac Edge/Chrome while Windows is fine. So on macOS we skip the
        // GPU renderers entirely and use xterm's DOM renderer (stable, no GPU;
        // perf is a non-issue for viewing worker output). Other platforms keep
        // WebGL→Canvas.
        var rendererAddon = null;
        var _uaPlat = (navigator.userAgentData && navigator.userAgentData.platform)
            || navigator.platform || navigator.userAgent || '';
        var _isMac = /Mac|iPhone|iPad|iPod/.test(_uaPlat);
        if (!_isMac && typeof WebglAddon !== 'undefined' && WebglAddon.WebglAddon) {
            try {
                rendererAddon = new WebglAddon.WebglAddon();
                rendererAddon.onContextLoss(function() {
                    console.warn('[swarm-term] WebGL context lost for', name);
                    try { rendererAddon.dispose(); } catch (e) {}
                    rendererAddon = null;
                    if (typeof CanvasAddon !== 'undefined' && CanvasAddon.CanvasAddon) {
                        try {
                            rendererAddon = new CanvasAddon.CanvasAddon();
                            term.loadAddon(rendererAddon);
                        } catch (e2) { rendererAddon = null; }
                    }
                });
                term.loadAddon(rendererAddon);
            } catch (e) {
                rendererAddon = null;
                if (typeof CanvasAddon !== 'undefined' && CanvasAddon.CanvasAddon) {
                    try {
                        rendererAddon = new CanvasAddon.CanvasAddon();
                        term.loadAddon(rendererAddon);
                    } catch (e2) { rendererAddon = null; }
                }
            }
        }

        // Custom link provider: detect file paths and copy on click
        var _linkProviderDisposable = term.registerLinkProvider({
            provideLinks: function(bufferLineNumber, callback) {
                var line = '';
                try {
                    var buf = term.buffer.active;
                    var lineData = buf.getLine(bufferLineNumber);
                    if (lineData) line = lineData.translateToString(true);
                } catch (e) { callback(undefined); return; }
                if (!line) { callback(undefined); return; }
                var links = [];
                var re = /(?:^|[\s"'(,])((\.{0,2}\/)?[a-zA-Z0-9_\-./]+\.[a-zA-Z]{1,6}(?::\d+(?::\d+)?)?)/g;
                var match;
                while ((match = re.exec(line)) !== null) {
                    var fp = match[1];
                    if (/^https?:\/\//.test(fp)) continue;
                    if (fp.indexOf('/') === -1) continue;
                    var sc = match.index + (match[0].length - match[1].length);
                    (function(path, col) {
                        links.push({
                            range: { start: { x: col + 1, y: bufferLineNumber + 1 },
                                     end: { x: col + path.length + 1, y: bufferLineNumber + 1 } },
                            text: path,
                            activate: function() {
                                navigator.clipboard.writeText(path).then(function() {
                                    showToast('Copied: ' + path);
                                });
                            }
                        });
                    })(fp, sc);
                }
                callback(links.length > 0 ? links : undefined);
            }
        });

        // Drag-and-drop
        container.addEventListener('dragover', function(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'copy';
            container.style.outline = '2px solid var(--honey)';
        });
        container.addEventListener('dragleave', function() {
            container.style.outline = '';
        });
        container.addEventListener('drop', function(e) {
            e.preventDefault();
            e.stopPropagation();
            container.style.outline = '';
            if (e.dataTransfer.files.length > 0) {
                uploadAndPaste(e.dataTransfer.files[0], term, entry && entry.ws);
            }
        });

        // Block Ctrl+V raw 0x16
        term.attachCustomKeyEventHandler(function(e) {
            if ((e.ctrlKey || e.metaKey) && e.key === 'v' && e.type === 'keydown') {
                return false;
            }
            if (e.ctrlKey && e.type === 'keydown') {
                if (e.key === ']' || e.key === '[') {
                    e.preventDefault();
                    cycleWorker(e.key === ']' ? 1 : -1);
                    return false;
                }
                if (e.key === 'Tab') {
                    e.preventDefault();
                    e.stopPropagation();
                    cycleWorker(e.shiftKey ? -1 : 1);
                    return false;
                }
            }
            if ((e.ctrlKey || e.metaKey) && e.key === 'c' && e.type === 'keydown') {
                var sel = term.getSelection();
                if (sel) {
                    navigator.clipboard.writeText(sel).then(function() {
                        showToast('Copied to clipboard');
                    });
                    term.clearSelection();
                    return false;
                }
            }
            if ((e.ctrlKey || e.metaKey) && e.key === 'f' && e.type === 'keydown') {
                e.preventDefault();
                toggleTermSearch(entry);
                return false;
            }
            return true;
        });

        // Paste handler
        if (term.textarea) {
            term.textarea.addEventListener('paste', function(e) {
                var cd = e.clipboardData || window.clipboardData || {};
                var text = cd.getData('text');
                if (text) {
                    e.preventDefault();
                    e.stopPropagation();
                    term.paste(text);
                    return;
                }
                var items = cd.items || [];
                for (var i = 0; i < items.length; i++) {
                    if (items[i].type.indexOf('image') !== -1) {
                        e.preventDefault();
                        e.stopPropagation();
                        var blob = items[i].getAsFile();
                        if (blob) uploadAndPaste(blob, term, entry && entry.ws);
                        return;
                    }
                }
            }, true);
        }

        var entry = {
            term: term,
            fitAddon: fitAddon,
            searchAddon: searchAddon,
            serializeAddon: serializeAddon,
            rendererAddon: rendererAddon,
            ws: null,
            container: container,
            connectTimer: null,
            reconnectAttempts: 0,
            reconnectTimer: null,
            lastCols: 0,
            lastRows: 0,
            lastAccess: Date.now(),
            resizeObserver: null,
            serverAlt: null,
            inputReady: false,
            pendingInput: [],
            inputReadyTimer: null,
            termTitle: '',
            stickyBottom: true,
            _writesPending: 0,
            _isAutoScrolling: false,
            _firstPayloadTimer: null,
            _staleWatchdog: null,
            _lastWsData: 0,
            _lastWsInput: 0,
            _onBellDisposable: null,
            // Whether this terminal has rendered at least one payload
            // already.  Used to detect reconnect (as opposed to initial
            // attach) so we can reset the xterm instance before
            // replaying the snapshot — otherwise pre-reload content
            // sits under the replay and the terminal shows mixed-state
            // frames until the operator reloads the page 1-3 more times.
            _hasRenderedEver: false,
            _onTitleChangeDisposable: null,
            _linkProviderDisposable: _linkProviderDisposable
        };

        // Track stickyBottom. Three signal sources, each independently
        // reliable, all collapse into "are we at the bottom or not":
        //   1. Wheel capture on term.element — fires synchronously on
        //      user wheel, BEFORE xterm processes it. Upward wheel always
        //      disables sticky. This is the bulletproof path.
        //   2. Native DOM scroll on .xterm-viewport — covers scrollbar
        //      drag, touch, and any path that moves DOM scrollTop.
        //   3. xterm onScroll — covers programmatic scrollToBottom and
        //      keyboard scroll inside xterm. Idempotent after the wheel
        //      handler already flipped sticky to false.
        // Previous guards (_isAutoScrolling, _writesPending > 0) silently
        // dropped user scrolls during heavy output. No guards now.
        function syncStickyBottom() {
            var vp = entry._viewportEl;
            if (vp && vp.isConnected) {
                var dist = vp.scrollHeight - vp.scrollTop - vp.clientHeight;
                entry.stickyBottom = dist <= 5;
            } else {
                entry.stickyBottom = isTermAtBottom(term);
            }
            updateJumpToBottomPill(entry);
        }
        term.onScroll(syncStickyBottom);
        entry._viewportEl = container.querySelector('.xterm-viewport');
        if (entry._viewportEl) {
            entry._viewportEl.addEventListener('scroll', syncStickyBottom, { passive: true });
        }
        // Capture wheel on the xterm root in capture phase so we run
        // before xterm.js's own wheel handler. Upward wheel is unambiguous
        // operator intent — flip sticky off synchronously.
        if (term.element) {
            term.element.addEventListener('wheel', function(ev) {
                if (ev.deltaY < 0) {
                    entry.stickyBottom = false;
                    updateJumpToBottomPill(entry);
                }
            }, { capture: true, passive: true });
        }

        // Floating "Jump to bottom" pill: visible only when stickyBottom
        // is false. Click → scrollToBottom + re-arm sticky.
        var jumpBtn = document.createElement('button');
        jumpBtn.className = 'jump-to-bottom-pill';
        jumpBtn.type = 'button';
        jumpBtn.textContent = 'Jump to bottom';
        jumpBtn.setAttribute('aria-label', 'Jump to bottom and re-enable auto-scroll');
        jumpBtn.addEventListener('click', function(ev) {
            ev.preventDefault();
            autoScrollToBottom(entry);
            entry.stickyBottom = true;
            updateJumpToBottomPill(entry);
            focusInlineTerm(name, entry);
        });
        container.appendChild(jumpBtn);
        entry._jumpBtn = jumpBtn;

        // Floating D-pad (touch/mobile): arrow keys around a center Enter
        // circle, anchored bottom-right above the jump-to-bottom pill. Each
        // button targets THIS entry's worker by name so it stays correct for
        // both worker views and the Queen embed (never the global
        // selectedWorker). It does NOT refocus the terminal — that would pop
        // the mobile soft keyboard, defeating the d-pad's purpose.
        var dpad = document.createElement('div');
        dpad.className = 'term-dpad';
        dpad.setAttribute('role', 'group');
        dpad.setAttribute('aria-label', 'Directional keys for ' + name);
        [
            ['term-dpad-up', '↑', 'arrow-up', 'Arrow up'],
            ['term-dpad-left', '←', 'arrow-left', 'Arrow left'],
            ['term-dpad-center', '↵', 'enter', 'Enter'],
            ['term-dpad-right', '→', 'arrow-right', 'Arrow right'],
            ['term-dpad-down', '↓', 'arrow-down', 'Arrow down']
        ].forEach(function(spec) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'term-dpad-btn ' + spec[0];
            b.textContent = spec[1];
            b.setAttribute('aria-label', spec[3] + ' to ' + name);
            b.addEventListener('click', function(ev) {
                ev.preventDefault();
                var path = (spec[2] === 'enter')
                    ? '/action/continue/' + encodeURIComponent(name)
                    : '/action/' + spec[2] + '/' + encodeURIComponent(name);
                actionFetch(path, { method: 'POST' });
            });
            dpad.appendChild(b);
        });
        container.appendChild(dpad);
        entry._dpad = dpad;

        // Terminal events: bell notification + title tracking
        entry._onBellDisposable = entry.term.onBell(function() {
            showToast('Bell from ' + name, false, BEE.surprised);
        });
        entry._onTitleChangeDisposable = entry.term.onTitleChange(function(title) {
            entry.termTitle = title || '';
            if (activeTermWorker === name) {
                updateDetailTitleWithTermTitle(name, entry);
            }
        });

        // Auto-fit when container dimensions change (DOM insert, resize, drag)
        var _resizeTimer = null;
        var ro = new ResizeObserver(function() {
            if (activeTermWorker !== name) return;
            if (_resizeTimer) return;  // debounce: 50ms
            _resizeTimer = setTimeout(function() {
                _resizeTimer = null;
                forceFitAndResize(name, entry);
                if (entry.stickyBottom) {
                    autoScrollToBottom(entry);
                }
            }, 50);
        });
        ro.observe(container);
        entry.resizeObserver = ro;

        return entry;
    }

    /** Connect (or reconnect) a cache entry's WebSocket. */
    function connectTermEntryWs(name, entry) {
        function queuePendingInput(data) {
            entry.pendingInput.push(data);
            if (entry.pendingInput.length > 256) {
                entry.pendingInput = entry.pendingInput.slice(-256);
            }
        }
        function flushPendingInput(wsRef) {
            if (!entry.inputReady) return;
            if (!wsRef || wsRef.readyState !== WebSocket.OPEN) return;
            if (!entry.pendingInput || !entry.pendingInput.length) return;
            var encoder = new TextEncoder();
            for (var pi = 0; pi < entry.pendingInput.length; pi++) {
                wsRef.send(encoder.encode(entry.pendingInput[pi]));
            }
            entry.pendingInput = [];
        }
        var dims = null;
        if (entry.fitAddon && entry.fitAddon.proposeDimensions) {
            try { dims = entry.fitAddon.proposeDimensions(); } catch (e) { dims = null; }
        }
        var path = '/ws/terminal?worker=' + encodeURIComponent(name);
        // Only pass initial dims if they're sane. A mid-layout reconnect
        // (showTermEntry reconnects on every show) can propose ~6 cols;
        // sending that opens the PTY at 6 cols and Claude wraps everything
        // until the next resize. Omit instead — the holder keeps its size
        // and the resync ladder sends a correct resize once layout settles.
        if (dims && dims.cols >= MIN_TERM_COLS && dims.rows >= MIN_TERM_ROWS) {
            path += '&cols=' + encodeURIComponent(dims.cols) + '&rows=' + encodeURIComponent(dims.rows);
        }
        // Phase B of duplication sweep: openAuthenticated handles the
        // URL build + first-message auth send.  See static/ws-auth.js.
        var newWs = window.swarmWS.openAuthenticated(path);
        newWs.binaryType = 'arraybuffer';
        entry.ws = newWs;
        if (entry.inputReadyTimer) {
            clearTimeout(entry.inputReadyTimer);
            entry.inputReadyTimer = null;
        }
        if (entry._firstPayloadTimer) {
            clearTimeout(entry._firstPayloadTimer);
            entry._firstPayloadTimer = null;
        }
        if (entry._staleWatchdog) {
            clearInterval(entry._staleWatchdog);
            entry._staleWatchdog = null;
        }
        if (activeTermWorker === name) inlineTermWs = newWs;
        entry._firstData = true;
        entry.inputReady = false;
        entry._lastWsData = 0;
        console.log('[swarm-term] WS connecting:', path);

        newWs.onopen = function() {
            if (entry.ws !== newWs) return;
            console.log('[swarm-term] WS open for', name);
            entry.reconnectAttempts = 0;
            // Failsafe: if replay/meta frames are delayed, don't deadlock input
            // after reload/reconnect. Allow queued keystrokes through shortly
            // after open; stale/unauthorized sockets will fail closed anyway.
            entry.inputReadyTimer = setTimeout(function() {
                if (entry.ws !== newWs) return;
                if (!entry.inputReady) {
                    entry.inputReady = true;
                    flushPendingInput(newWs);
                }
                entry.inputReadyTimer = null;
            }, 400);
            // Force fresh fit + resize — the viewport may have changed since
            // the terminal was last connected (e.g. mobile ↔ desktop rotation).
            entry.lastCols = 0;
            entry.lastRows = 0;
            resyncTermViewport(name, entry, false);
            // Sync scrollbar after rendered snapshot
            setTimeout(function() { autoScrollToBottom(entry); }, 50);
            updateTermDebug(entry);
            focusInlineTerm(name, entry);
            entry._firstPayloadTimer = setTimeout(function() {
                if (entry.ws !== newWs) return;
                if (!entry._firstData) return;
                console.warn('[swarm-term] no initial payload after open; reconnecting', {
                    worker: name,
                    readyState: newWs.readyState
                });
                try { newWs.close(); } catch (e) {}
            }, 1200);
            // Stale connection watchdog: if the user sent input but no output
            // has arrived within 15s, the output subscriber was likely dropped
            // server-side (WS stays open via heartbeat but no data flows).
            entry._lastWsData = Date.now();
            entry._lastWsInput = 0;
            entry._staleWatchdog = setInterval(function() {
                if (entry.ws !== newWs) { clearInterval(entry._staleWatchdog); entry._staleWatchdog = null; return; }
                if (newWs.readyState !== WebSocket.OPEN) return;
                // Only fire if user typed recently but got no output back
                if (entry._lastWsInput > 0 && entry._lastWsInput > entry._lastWsData && Date.now() - entry._lastWsInput > 15000) {
                    console.warn('[swarm-term] stale WS watchdog fired for', name, {
                        lastInput: Math.round((Date.now() - entry._lastWsInput) / 1000) + 's ago',
                        lastData: Math.round((Date.now() - entry._lastWsData) / 1000) + 's ago'
                    });
                    clearInterval(entry._staleWatchdog);
                    entry._staleWatchdog = null;
                    try { newWs.close(); } catch (e) {}
                }
            }, 5000);
        };

        newWs.onmessage = function(e) {
            if (entry.ws !== newWs) return;
            entry._lastWsData = Date.now();
            if (entry._firstPayloadTimer) {
                clearTimeout(entry._firstPayloadTimer);
                entry._firstPayloadTimer = null;
            }
            if (entry.inputReadyTimer) {
                clearTimeout(entry.inputReadyTimer);
                entry.inputReadyTimer = null;
            }
            function writeFrame(bytes) {
                if (entry._firstData) {
                    entry._firstData = false;
                    // Reconnect (not initial attach) — pre-reload content
                    // is still on screen.  Reset the xterm instance
                    // before replaying the snapshot so the new frame
                    // lands on a clean canvas rather than overlaying
                    // partial stale pixels.  First-ever attach skips
                    // this to avoid a visible flash on a blank terminal.
                    if (entry._hasRenderedEver) {
                        try { entry.term.reset(); } catch (e) {}
                    }
                    entry._writesPending++;
                    entry.term.write(bytes, function() {
                        entry.inputReady = true;
                        entry._hasRenderedEver = true;
                        flushPendingInput(newWs);
                        if (entry.container.parentNode) autoScrollToBottom(entry);
                        focusInlineTerm(name, entry);
                        entry._writesPending--;
                    });
                } else {
                    if (!entry.inputReady) {
                        entry.inputReady = true;
                        flushPendingInput(newWs);
                    }
                    var shouldScroll = entry.stickyBottom;
                    entry._writesPending++;
                    entry.term.write(bytes, function() {
                        if (shouldScroll && entry.stickyBottom && entry.container.parentNode) {
                            autoScrollToBottom(entry);
                        }
                        entry._writesPending--;
                    });
                }
                updateTermDebug(entry);
            }
            if (e.data instanceof ArrayBuffer) {
                writeFrame(new Uint8Array(e.data));
                return;
            }
            if (typeof Blob !== 'undefined' && e.data instanceof Blob) {
                e.data.arrayBuffer().then(function(buf) {
                    if (entry.ws !== newWs) return;
                    writeFrame(new Uint8Array(buf));
                }).catch(function() {});
                return;
            }
            if (typeof e.data === 'string') {
                try {
                    var payload = JSON.parse(e.data);
                    if (payload && payload.meta === 'term' && typeof payload.alt === 'boolean') {
                        console.log('[swarm-term] meta payload for', name, payload);
                        entry.serverAlt = payload.alt;
                        if (!entry.inputReady) {
                            entry.inputReady = true;
                            flushPendingInput(newWs);
                        }
                        updateTermDebug(entry);
                    }
                } catch (err) {}
            }
        };

        newWs.onclose = function(ev) {
            console.log('[swarm-term] WS close for ' + name + ': code=' + ev.code + ' reason=' + (ev.reason || 'none') + ' stale=' + (entry.ws !== newWs));
            if (entry.ws !== newWs) return;
            entry.ws = null;
            entry.inputReady = false;
            if (entry._firstPayloadTimer) {
                clearTimeout(entry._firstPayloadTimer);
                entry._firstPayloadTimer = null;
            }
            if (entry.inputReadyTimer) {
                clearTimeout(entry.inputReadyTimer);
                entry.inputReadyTimer = null;
            }
            if (entry._staleWatchdog) {
                clearInterval(entry._staleWatchdog);
                entry._staleWatchdog = null;
            }
            if (activeTermWorker === name) inlineTermWs = null;
            maybeClearStaleSessionToken();
            updateTermDebug(entry);
            // Reconnect if entry still exists (skip during dev restart)
            if (_restarting) return;
            if (termCache.has(name) && entry.reconnectAttempts < MAX_TERM_RECONNECT) {
                entry.reconnectAttempts++;
                var delay = 500 * entry.reconnectAttempts;
                console.log('[swarm-term] reconnect ' + name + ' attempt ' + entry.reconnectAttempts + '/' + MAX_TERM_RECONNECT + ' in ' + delay + 'ms');
                entry.reconnectTimer = setTimeout(function() {
                    entry.reconnectTimer = null;
                    if (termCache.has(name)) {
                        entry.term.reset();  // Clean slate before reconnect snapshot
                        connectTermEntryWs(name, entry);
                    }
                }, delay);
            } else if (activeTermWorker === name) {
                // All reconnects exhausted for the active terminal — show static
                destroyTermEntry(name);
                refreshDetailStatic();
                showToast('Terminal disconnected — showing static capture', true);
            }
        };

        newWs.onerror = function(ev) {
            console.error('[swarm-term] WS error for', name, ev);
        };

        // Terminal input → WS — dispose previous handler to avoid duplicates
        if (entry._onDataDisposable) entry._onDataDisposable.dispose();
        entry._onDataDisposable = entry.term.onData(function(data) {
            if (entry.ws && entry.ws.readyState === WebSocket.OPEN && entry.inputReady) {
                var encoder = new TextEncoder();
                entry.ws.send(encoder.encode(data));
                entry._lastWsInput = Date.now();
            } else if (
                entry.ws &&
                (entry.ws.readyState === WebSocket.CONNECTING || !entry.inputReady)
            ) {
                queuePendingInput(data);
            }
        });
    }

    /** Send resize only when dimensions actually changed. */
    function sendResizeIfChanged(name, entry) {
        if (!entry.fitAddon || !entry.term) return;
        var dims = entry.fitAddon.proposeDimensions();
        if (!dims) return;
        if (dims.cols < MIN_TERM_COLS || dims.rows < MIN_TERM_ROWS) return;
        if (dims.cols === entry.lastCols && dims.rows === entry.lastRows) return;
        if (entry.ws && entry.ws.readyState === WebSocket.OPEN) {
            entry.lastCols = dims.cols;
            entry.lastRows = dims.rows;
            entry.ws.send(JSON.stringify({ cols: dims.cols, rows: dims.rows }));
        }
        // Don't cache dimensions if WS isn't open — onopen will re-check
    }

    function forceFitAndResize(name, entry) {
        if (!entry || !entry.fitAddon || !entry.term) return;
        // Skip fit if container has no dimensions yet (e.g. mobile reload race)
        var rect = entry.container.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        // Reject small-but-nonzero (mid-layout) measurements before they
        // shrink the local xterm AND get SIGWINCH'd to the holder.
        var dims = entry.fitAddon.proposeDimensions();
        if (!dims || dims.cols < MIN_TERM_COLS || dims.rows < MIN_TERM_ROWS) return;
        entry.fitAddon.fit();
        sendResizeIfChanged(name, entry);
        updateTermDebug(entry);
    }

    /** Toggle the floating search bar for a terminal entry. */
    function toggleTermSearch(entry) {
        if (!entry || !entry.searchAddon) return;
        var existing = entry.container.querySelector('.term-search-bar');
        if (existing) {
            entry.searchAddon.clearDecorations();
            existing.remove();
            try { entry.term.focus(); } catch (e) {}
            return;
        }
        var bar = document.createElement('div');
        bar.className = 'term-search-bar';
        bar.style.cssText = 'position:absolute;top:4px;right:16px;z-index:10;display:flex;gap:4px;align-items:center;background:var(--panel-bg,#362415);border:1px solid var(--honey,#D8A03D);border-radius:4px;padding:3px 6px;';
        var input = document.createElement('input');
        input.type = 'text';
        input.placeholder = 'Search…';
        input.style.cssText = 'background:transparent;border:none;color:var(--text,#E6D2B5);font-size:13px;width:160px;outline:none;';
        var btnPrev = document.createElement('button');
        btnPrev.textContent = '\u25B2';
        btnPrev.title = 'Previous';
        btnPrev.style.cssText = 'background:none;border:none;color:var(--text,#E6D2B5);cursor:pointer;font-size:11px;padding:2px 4px;';
        var btnNext = document.createElement('button');
        btnNext.textContent = '\u25BC';
        btnNext.title = 'Next';
        btnNext.style.cssText = btnPrev.style.cssText;
        var btnClose = document.createElement('button');
        btnClose.textContent = '\u2715';
        btnClose.title = 'Close (Esc)';
        btnClose.style.cssText = btnPrev.style.cssText;
        bar.appendChild(input);
        bar.appendChild(btnPrev);
        bar.appendChild(btnNext);
        bar.appendChild(btnClose);
        entry.container.style.position = 'relative';
        entry.container.appendChild(bar);
        function doSearch(dir) {
            var q = input.value;
            if (!q) { entry.searchAddon.clearDecorations(); return; }
            if (dir === 'prev') {
                entry.searchAddon.findPrevious(q);
            } else {
                entry.searchAddon.findNext(q);
            }
        }
        function closeBar() {
            entry.searchAddon.clearDecorations();
            bar.remove();
            try { entry.term.focus(); } catch (e) {}
        }
        input.addEventListener('input', function() { doSearch('next'); });
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                doSearch(e.shiftKey ? 'prev' : 'next');
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                closeBar();
            }
        });
        btnPrev.addEventListener('click', function() { doSearch('prev'); });
        btnNext.addEventListener('click', function() { doSearch('next'); });
        btnClose.addEventListener('click', closeBar);
        input.focus();
    }

    function isTermAtBottom(term) {
        var buf = term.buffer && term.buffer.active;
        if (!buf) return true;
        return buf.viewportY >= buf.baseY;
    }

    /** Programmatic scroll-to-bottom that flags itself so onScroll ignores it. */
    function autoScrollToBottom(entry) {
        entry._isAutoScrolling = true;
        try { entry.term.scrollToBottom(); } catch(e) {}
        requestAnimationFrame(function() { entry._isAutoScrolling = false; });
    }

    function updateJumpToBottomPill(entry) {
        if (!entry) return;
        var showing = !entry.stickyBottom;
        // Pill is visible only when scrolled away from the bottom. The d-pad
        // floats at the top-right, clear of the pill, so it needs no shift.
        if (entry._jumpBtn) entry._jumpBtn.classList.toggle('show', showing);
    }

    function resyncTermViewport(name, entry, stickToBottom) {
        if (!entry || !entry.term) return;
        forceFitAndResize(name, entry);
        if (stickToBottom) {
            autoScrollToBottom(entry);
            // Force DOM scrollbar to match xterm internal state after re-attachment
            var viewport = entry.container.querySelector('.xterm-viewport');
            if (viewport) viewport.scrollTop = viewport.scrollHeight;
        }
        try { entry.term.refresh(0, Math.max(0, (entry.term.rows || 1) - 1)); } catch (e) {}
    }

    /** Move keyboard focus into a terminal entry's xterm input. No
     *  activeTermWorker guard — callers (worker views AND the Queen embed,
     *  which is intentionally never activeTermWorker) gate it themselves. */
    function focusTermEntryNow(entry) {
        if (!entry || !entry.term) return;
        if (window.matchMedia('(pointer: coarse)').matches) return;
        try {
            if (entry.term.textarea) entry.term.textarea.focus({preventScroll: true});
            entry.term.focus();
        } catch (e) {}
    }

    function focusInlineTerm(name, entry) {
        if (!entry || !entry.term) return;
        if (activeTermWorker !== name) return;
        focusTermEntryNow(entry);
        setTimeout(function() {
            if (activeTermWorker !== name) return;
            focusTermEntryNow(entry);
        }, 80);
    }

    /** True when the keyboard focus is inside a terminal's xterm input —
     *  either the active worker terminal (inlineTerm) OR the Queen live
     *  embed (which is deliberately not activeTermWorker, so it needs its
     *  own check). Global keyboard-shortcut handlers use this to yield to
     *  the terminal instead of firing dashboard shortcuts. */
    function isTermInputFocused() {
        var ae = document.activeElement;
        if (!ae) return false;
        if (inlineTerm && inlineTerm.textarea && ae === inlineTerm.textarea) return true;
        if (queenEmbedMounted) {
            var q = termCache.get('queen');
            if (q && q.term && q.term.textarea && ae === q.term.textarea) return true;
        }
        return false;
    }

    /** Show a cached entry in the detail panel. */
    function showTermEntry(name, entry) {
        var body = document.getElementById('detail-body');
        body.innerHTML = '';
        body.style.padding = '0';
        body.style.overflow = 'hidden';
        body.style.display = 'flex';
        body.style.flex = '1';
        body.style.minHeight = '0';
        entry.container.style.flex = '1';
        entry.container.style.minHeight = '0';
        body.appendChild(entry.container);
        entry.lastAccess = Date.now();
        entry.stickyBottom = true;
        updateJumpToBottomPill(entry);
        activeTermWorker = name;
        syncTermAliases(entry);

        // Focus after layout settles (fit handled by ResizeObserver)
        requestAnimationFrame(function() {
            if (activeTermWorker !== name) return;
            resyncTermViewport(name, entry, true);
            setTimeout(function() {
                if (activeTermWorker !== name) return;
                resyncTermViewport(name, entry, true);
            }, 80);
            setTimeout(function() {
                if (activeTermWorker !== name) return;
                resyncTermViewport(name, entry, true);
            }, 220);
            // Mobile viewports can take longer to settle (address bar animation)
            setTimeout(function() {
                if (activeTermWorker !== name) return;
                resyncTermViewport(name, entry, true);
            }, 600);
            focusInlineTerm(name, entry);
        });

        // Always reconnect to get a fresh snapshot — an "open" WS may have
        // lost its server-side subscriber (queue overflow, sender task death)
        // while staying alive via heartbeat pings, resulting in a blank terminal.
        if (entry.ws) {
            try { entry.ws.close(); } catch (e) {}
        }
        entry.term.reset();
        entry.reconnectAttempts = 0;
        connectTermEntryWs(name, entry);
    }

    /** Hide the active terminal (non-destructive — keeps Terminal + WS alive). */
    function hideActiveTermEntry() {
        if (!activeTermWorker) return;
        var entry = termCache.get(activeTermWorker);
        if (entry && entry.container.parentNode) {
            entry.container.remove();  // detach from DOM, keep in memory
        }
        activeTermWorker = null;
        syncTermAliases(null);
        var body = document.getElementById('detail-body');
        if (body) {
            body.style.padding = '';
            body.style.overflow = '';
            body.style.display = '';
            body.style.flex = '';
            body.style.minHeight = '';
        }
    }

    /** Fully destroy a cache entry (close WS, dispose Terminal, remove from cache). */
    function destroyTermEntry(name) {
        var entry = termCache.get(name);
        if (!entry) return;
        console.log('[swarm-term] destroyTermEntry:', name);
        if (entry.resizeObserver) { entry.resizeObserver.disconnect(); entry.resizeObserver = null; }
        if (entry.connectTimer) { clearTimeout(entry.connectTimer); entry.connectTimer = null; }
        if (entry.reconnectTimer) { clearTimeout(entry.reconnectTimer); entry.reconnectTimer = null; }
        if (entry.inputReadyTimer) { clearTimeout(entry.inputReadyTimer); entry.inputReadyTimer = null; }
        if (entry._firstPayloadTimer) { clearTimeout(entry._firstPayloadTimer); entry._firstPayloadTimer = null; }
        if (entry._staleWatchdog) { clearInterval(entry._staleWatchdog); entry._staleWatchdog = null; }
        if (entry._onDataDisposable) { try { entry._onDataDisposable.dispose(); } catch(e) {} entry._onDataDisposable = null; }
        if (entry._onBellDisposable) { try { entry._onBellDisposable.dispose(); } catch(e) {} }
        if (entry._onTitleChangeDisposable) { try { entry._onTitleChangeDisposable.dispose(); } catch(e) {} }
        if (entry._linkProviderDisposable) { try { entry._linkProviderDisposable.dispose(); } catch(e) {} }
        if (entry.rendererAddon) { try { entry.rendererAddon.dispose(); } catch(e) {} entry.rendererAddon = null; }
        if (entry.ws) { try { entry.ws.close(); } catch(e) {} entry.ws = null; }
        if (entry.term) { try { entry.term.dispose(); } catch(e) {} }
        if (entry.container && entry.container.parentNode) entry.container.remove();
        termCache.delete(name);
        if (activeTermWorker === name) {
            activeTermWorker = null;
            syncTermAliases(null);
        }
    }

    /** LRU eviction when cache exceeds MAX_CACHED_TERMS. */
    function evictIfNeeded() {
        while (termCache.size >= MAX_CACHED_TERMS) {
            // Find least recently accessed entry (skip the active one)
            var oldest = null;
            var oldestName = null;
            termCache.forEach(function(entry, name) {
                if (name === activeTermWorker) return;
                if (name === 'queen' && queenEmbedMounted) return;
                if (!oldest || entry.lastAccess < oldest.lastAccess) {
                    oldest = entry;
                    oldestName = name;
                }
            });
            if (oldestName) {
                console.log('[swarm-term] evicting cached terminal:', oldestName);
                destroyTermEntry(oldestName);
            } else {
                break;  // only the active entry remains — can't evict
            }
        }
    }

    /** Periodic cleanup: close terminals not accessed in >5 minutes. */
    _trackedIntervals.push(setInterval(function() {
        var now = Date.now();
        var stale = [];
        termCache.forEach(function(entry, name) {
            if (name === activeTermWorker) return;
            if (name === 'queen' && queenEmbedMounted) return;
            if (now - entry.lastAccess > 300000) stale.push(name);
        });
        stale.forEach(function(name) {
            console.log('[swarm-term] idle cleanup:', name);
            destroyTermEntry(name);
        });
    }, 60000));

    /** Prune cache entries for workers that no longer exist. */
    function pruneStaleTermEntries(workerNames) {
        var stale = [];
        termCache.forEach(function(_entry, name) {
            if (!workerNames.has(name)) stale.push(name);
        });
        stale.forEach(function(name) {
            console.log('[swarm-term] pruning stale terminal:', name);
            destroyTermEntry(name);
        });
    }

    function attachInlineTerminal(workerName) {
        // Already showing this worker
        if (activeTermWorker === workerName) return;

        // Fall back to static if xterm CDN hasn't loaded yet
        if (typeof Terminal === 'undefined') {
            refreshDetailStatic();
            return;
        }

        console.log('[swarm-term] attachInlineTerminal:', workerName);

        // Hide current terminal (non-destructive)
        hideActiveTermEntry();

        // Cache hit — re-show the existing terminal
        var entry = termCache.get(workerName);
        if (entry) {
            showTermEntry(workerName, entry);
            updateTermDebug(entry);
            return;
        }

        // Cache miss — create, cache, show
        evictIfNeeded();
        entry = createTermEntry(workerName);
        termCache.set(workerName, entry);
        showTermEntry(workerName, entry);
        updateTermDebug(entry);
    }

    // --- Queen live-session embed (Command Center right panel) ---
    //
    // The interactive Queen is a real PTY worker. Instead of a fragile
    // chat relay, the Command Center embeds her ACTUAL live session using
    // the same cached `termCache` entry the full-screen view uses — one
    // xterm, one /ws/terminal connection, moved between the embed holder
    // and #detail-body via appendChild (the proven tile pattern). It is
    // deliberately NOT `activeTermWorker` (that's the focused detail
    // terminal), so it needs its own ResizeObserver and eviction guard.
    var queenEmbedMounted = false;

    function mountQueenEmbed() {
        var holder = document.getElementById('cc-queen-term-holder');
        if (!holder || typeof Terminal === 'undefined') return;
        // If she's currently full-screen, detach from #detail-body first
        // so we move the single shared container, never duplicate it.
        if (activeTermWorker === 'queen') hideActiveTermEntry();

        var entry = termCache.get('queen');
        if (!entry) {
            evictIfNeeded();
            entry = createTermEntry('queen');
            termCache.set('queen', entry);
        }
        if (entry.container.parentNode !== holder) holder.appendChild(entry.container);
        entry.container.style.flex = '1';
        entry.container.style.minHeight = '0';
        entry.lastAccess = Date.now();
        entry.stickyBottom = true;
        queenEmbedMounted = true;

        // Always reconnect for a fresh snapshot (mirrors showTermEntry —
        // an "open" WS may have lost its server-side subscriber).
        if (entry.ws) { try { entry.ws.close(); } catch (e) {} }
        if (entry.term) { try { entry.term.reset(); } catch (e) {} }
        entry.reconnectAttempts = 0;
        connectTermEntryWs('queen', entry);

        // Dedicated observer — the createTermEntry one is gated on
        // activeTermWorker, which the embed intentionally never sets.
        if (!entry._embedRO) {
            var t = null;
            entry._embedRO = new ResizeObserver(function () {
                if (t) return;
                t = setTimeout(function () {
                    t = null;
                    if (entry.container.parentNode === holder) {
                        resyncTermViewport('queen', entry, entry.stickyBottom);
                    }
                }, 50);
            });
        }
        try { entry._embedRO.disconnect(); } catch (e) {}
        entry._embedRO.observe(holder);

        // Staged refit ladder — container width settles across a few
        // frames (copied from showTermEntry).
        requestAnimationFrame(function () {
            [0, 80, 220, 600].forEach(function (d) {
                setTimeout(function () {
                    if (queenEmbedMounted && entry.container.parentNode === holder) {
                        resyncTermViewport('queen', entry, true);
                    }
                }, d);
            });
        });

        // Terminal-first view: drop the cursor into the Queen PTY so the
        // operator can type immediately (mirrors showTermEntry's worker
        // focus). Staged re-focus survives the WS reset/reconnect above;
        // mobile/coarse-pointer is skipped inside focusTermEntryNow.
        [80, 250].forEach(function (d) {
            setTimeout(function () {
                if (queenEmbedMounted && entry.container.parentNode === holder) {
                    focusTermEntryNow(entry);
                }
            }, d);
        });
    }

    function unmountQueenEmbed() {
        queenEmbedMounted = false;
        var entry = termCache.get('queen');
        if (!entry) return;
        if (entry._embedRO) { try { entry._embedRO.disconnect(); } catch (e) {} }
        var holder = document.getElementById('cc-queen-term-holder');
        // Tolerant: only detach if still parented in the embed holder —
        // the full-screen path may have already moved the container.
        if (holder && entry.container.parentNode === holder) {
            entry.container.remove();
        }
    }

    window.mountQueenEmbed = mountQueenEmbed;
    window.unmountQueenEmbed = unmountQueenEmbed;
    // Exposed so the Command Center IIFE's show() can detach the active worker
    // terminal when switching to the Queen view — without this, the worker
    // term stays mounted in #detail-body (with its inline display:flex) and
    // stacks over the Queen panel.
    window.hideActiveTermEntry = hideActiveTermEntry;

    // --- Tile mode (multi-worker terminal grid) ---
    var tileMode = false;

    function toggleTileMode() {
        tileMode = !tileMode;
        var btn = document.getElementById('tile-mode-btn');
        var sizeSelect = document.getElementById('tile-size-select');
        var detailBody = document.getElementById('detail-body');
        var tileGrid = document.getElementById('tile-grid');
        var actions = document.getElementById('terminal-actions');

        if (tileMode) {
            if (btn) btn.classList.add('btn-active');
            if (sizeSelect) sizeSelect.style.display = '';
            detailBody.style.display = 'none';
            tileGrid.style.display = 'grid';
            if (actions) actions.style.display = 'none';
            buildTileGrid();
        } else {
            if (btn) btn.classList.remove('btn-active');
            if (sizeSelect) sizeSelect.style.display = 'none';
            tileGrid.style.display = 'none';
            tileGrid.innerHTML = '';
            detailBody.style.display = '';
            if (selectedWorker) {
                if (actions) actions.style.display = 'flex';
                attachInlineTerminal(selectedWorker);
            }
        }
    }

    // Grid size change handler
    document.addEventListener('change', function(e) {
        if (e.target.id !== 'tile-size-select') return;
        if (tileMode) buildTileGrid();
    });

    function buildTileGrid() {
        var grid = document.getElementById('tile-grid');
        grid.innerHTML = '';

        // Get worker names from DOM
        var workers = [];
        document.querySelectorAll('.worker-item[data-worker]').forEach(function(el) {
            if (el.style.display !== 'none') workers.push(el.dataset.worker);
        });
        if (!workers.length) {
            grid.innerHTML = '<div class="empty-state text-muted">No workers to tile</div>';
            return;
        }

        // Parse grid size from selector (default 2x2)
        var sizeSelect = document.getElementById('tile-size-select');
        var size = (sizeSelect && sizeSelect.value) || '2x2';
        var parts = size.split('x');
        var cols = parseInt(parts[0], 10) || 2;
        var rows = parseInt(parts[1], 10) || 2;
        var maxTiles = cols * rows;
        var tileWorkers = workers.slice(0, maxTiles);
        grid.style.gridTemplateColumns = 'repeat(' + Math.min(cols, tileWorkers.length) + ', 1fr)';

        for (var i = 0; i < tileWorkers.length; i++) {
            var name = tileWorkers[i];
            var cell = document.createElement('div');
            cell.className = 'tile-cell';
            cell.dataset.tileWorker = name;

            var header = document.createElement('div');
            header.className = 'tile-cell-header';
            header.innerHTML = '<span class="text-honey">' + escapeHtml(name) + '</span><span class="text-muted text-xs">click to expand</span>';
            header.addEventListener('click', (function(n) {
                return function() { exitTileToWorker(n); };
            })(name));

            var body = document.createElement('div');
            body.className = 'tile-cell-body';

            cell.appendChild(header);
            cell.appendChild(body);
            grid.appendChild(cell);

            // Attach a terminal to this tile
            attachTermToTile(name, body);
        }
    }

    function attachTermToTile(workerName, container) {
        if (typeof Terminal === 'undefined') return;

        var entry = termCache.get(workerName);
        if (!entry) {
            evictIfNeeded();
            entry = createTermEntry(workerName);
            termCache.set(workerName, entry);
        }

        container.innerHTML = '';
        container.appendChild(entry.container);
        entry.container.style.flex = '1';
        entry.container.style.minHeight = '0';
        entry.lastAccess = Date.now();

        // Fit after DOM settles
        setTimeout(function() {
            if (entry.fitAddon) {
                try { entry.fitAddon.fit(); } catch(e) {}
            }
        }, 100);
    }

    function exitTileToWorker(name) {
        tileMode = false;
        var btn = document.getElementById('tile-mode-btn');
        if (btn) btn.classList.remove('btn-active');
        document.getElementById('tile-grid').style.display = 'none';
        document.getElementById('tile-grid').innerHTML = '';
        document.getElementById('detail-body').style.display = '';
        selectWorker(name);
    }

    // Show tile button when a worker is selected
    var _origSelectWorker = window.selectWorker;
    window.selectWorker = function(name) {
        var btn = document.getElementById('tile-mode-btn');
        if (btn) btn.style.display = '';
        _origSelectWorker(name);
    };

    // *targetTerm*/*targetWs* identify WHICH terminal was pasted/dropped
    // into. They default to the active inline terminal for the global
    // drop-outside fallback, but the per-terminal paste/drop handlers in
    // createTermEntry pass their own entry — critical for the embedded
    // Queen, which is deliberately not `activeTermWorker`, so without
    // this its pastes went to the last active worker instead.
    function uploadAndPaste(file, targetTerm, targetWs) {
        var t = targetTerm || inlineTerm;
        var ws = targetWs || inlineTermWs;
        if (!t || !ws || ws.readyState !== WebSocket.OPEN) {
            showToast('Terminal not connected', true);
            return;
        }
        var fname = file.name || ('paste-' + Date.now() + '.png');
        showToast('Uploading ' + fname + '...');
        var fd = new FormData();
        fd.append('file', file, fname);
        fetch('/api/uploads', { method: 'POST', body: fd, headers: { 'X-Requested-With': 'swarm' } })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.path && t) {
                    t.paste(data.path);
                    showToast('Pasted: ' + fname);
                } else if (!data.path) {
                    showToast('Upload failed: ' + (data.error || 'unknown'), true);
                }
            })
            .catch(function() { showToast('Upload failed', true); });
    }

    function detachInlineTerminal() {
        console.log('[swarm-term] detachInlineTerminal (hide, non-destructive)');
        hideActiveTermEntry();
    }

    function hardReconnectTermEntry(worker) {
        destroyTermEntry(worker);
        attachInlineTerminal(worker);
    }

    function repaintActiveTerminal(entry) {
        if (!entry || !entry.term) return;
        // Repaint viewport from xterm's own buffer without dropping local scrollback.
        try { entry.term.refresh(0, Math.max(0, (entry.term.rows || 1) - 1)); } catch (e) {}
        forceFitAndResize(activeTermWorker, entry);
    }

    // Expose a light-weight "refit the active terminal" entry point for
    // out-of-IIFE callers (Command Center IIFE uses this after grid-layout
    // changes that resize the terminal container — `window.resize` events
    // alone don't reliably refit xterm because its fitAddon hooks the
    // container's ResizeObserver, not window resize).
    window.ccRefitActiveTerm = function () {
        if (!activeTermWorker) return;
        var entry = termCache.get(activeTermWorker);
        if (entry) repaintActiveTerminal(entry);
    };

    // Hard-reconnect the active terminal — recreates xterm from scratch.
    // Used by the Command Center after a dashboard→worker transition:
    // fit() alone can't un-wrap content that xterm's buffer stored at the
    // old (narrow) width while detail-body was display:none. A fresh
    // xterm reads the current PTY screen at the correct width.
    window.ccHardReconnectActiveTerm = function () {
        if (!activeTermWorker) return;
        try { hardReconnectTermEntry(activeTermWorker); } catch (_) {}
    };

    window.refreshInlineTerminal = function() {
        // Re-sync worker states from daemon
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({command: "refresh"}));
        }
        if (activeTermWorker) {
            var worker = activeTermWorker;
            var entry = termCache.get(worker);
            repaintActiveTerminal(entry);
            // Ask the worker process to redraw, then do a deterministic reconnect.
            actionFetch('/action/redraw/' + encodeURIComponent(worker), { method: 'POST' })
                .then(function() {
                    setTimeout(function() {
                        hardReconnectTermEntry(worker);
                    }, 250);
                })
                .catch(function() {
                    hardReconnectTermEntry(worker);
                });
            if (!entry || !entry.ws || entry.ws.readyState !== WebSocket.OPEN) {
                hardReconnectTermEntry(worker);
            }
        } else {
            refreshDetailStatic();
        }
    }

    // Refit active inline terminal when mobile viewport changes (address bar show/hide)
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', function() {
            if (!activeTermWorker) return;
            var entry = termCache.get(activeTermWorker);
            if (entry) forceFitAndResize(activeTermWorker, entry);
        });
    }

    // --- Worker selection (client-side, no page reload) ---
    function updateDetailTitleWithTermTitle(name, entry) {
        var titleEl = document.getElementById('detail-title-text') || document.getElementById('detail-title');
        if (!titleEl) return;
        var taskText = '';
        var workerEl = document.querySelector('.worker-item[data-worker="' + name + '"] .worker-task');
        if (workerEl) taskText = workerEl.textContent.trim();
        var base = name + (taskText ? ' \u2014 ' + taskText : ' \u2014 Detail');
        titleEl.textContent = base;
        if (entry && entry.termTitle && entry.termTitle !== name) {
            var sub = document.createElement('span');
            sub.className = 'term-title-sub';
            sub.textContent = ' [' + entry.termTitle + ']';
            sub.style.cssText = 'font-size:0.8em;color:var(--muted);font-weight:normal;margin-left:0.5em;';
            titleEl.appendChild(sub);
        }
    }

    window.selectWorker = function(name) {
        selectedWorker = name;
        try { sessionStorage.setItem('swarm_selected_worker', name); } catch(e) {}
        // localStorage variant survives across sessions — picked up by
        // the Web Share Target landing flow so a shared screenshot can
        // pre-fill the New Task assignee with the worker the operator
        // was last using. sessionStorage doesn't carry across the OS
        // share sheet → browser bounce; localStorage does.
        try { if (name) localStorage.setItem('swarm.lastActiveWorker', name); } catch(e) {}
        // Operator is addressing this worker — clear any queen/escalation
        // banners tied to it so they don't linger after the issue is handled.
        if (typeof removeQueenBannersForWorker === 'function') removeQueenBannersForWorker(name);
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({command: "focus", worker: name}));
        }
        var taskText = '';
        document.querySelectorAll('.worker-item').forEach(function(el) {
            el.classList.toggle('selected', el.dataset.worker === name);
            if (el.dataset.worker === name) {
                var taskEl = el.querySelector('.worker-task');
                if (taskEl) taskText = taskEl.textContent.trim();
            }
        });
        var detailEntry = termCache.get(name);
        updateDetailTitleWithTermTitle(name, detailEntry);
        document.getElementById('terminal-actions').style.display = 'flex';
        // Show mobile-only controls on touch devices
        var sendBar = document.getElementById('mobile-send-bar');
        if (sendBar) sendBar.classList.add('visible');
        var fsBtn = document.getElementById('btn-fullscreen-term');
        if (fsBtn && window.matchMedia('(pointer: coarse)').matches) {
            fsBtn.style.display = '';
        }
        attachInlineTerminal(name);
    }

    // Mobile send bar — type/dictate text and send to worker terminal.
    // The composer is a multi-line <textarea> so native autocorrect / voice
    // dictation work (the raw xterm keystroke path doesn't get them). Embedded
    // newlines (Shift+Enter) are sent through with the text; the trailing \r
    // submits — same one-shot send the single-line input used.
    window.mobileSend = function() {
        var input = document.getElementById('mobile-send-input');
        if (!input || !input.value) return;
        var text = input.value;
        input.value = '';
        input.style.height = '';  // collapse the auto-grown composer back to one line
        if (inlineTermWs && inlineTermWs.readyState === WebSocket.OPEN) {
            var encoder = new TextEncoder();
            inlineTermWs.send(encoder.encode(text + '\r'));
        } else if (selectedWorker) {
            var form = new FormData();
            form.append('message', text);
            actionFetch('/action/send/' + encodeURIComponent(selectedWorker), { method: 'POST', body: form });
        }
    };

    // Wire the mobile composer textarea: auto-grow to fit content (capped by the
    // CSS max-height), Enter sends, Shift+Enter inserts a newline. Idempotent —
    // the element is static, so this runs once at init.
    function setupMobileComposer() {
        var ta = document.getElementById('mobile-send-input');
        if (!ta || ta._composerWired) return;
        ta._composerWired = true;
        var autogrow = function() {
            ta.style.height = 'auto';
            ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';  // 120px ≈ max-height 7.5rem
        };
        ta.addEventListener('input', autogrow);
        ta.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();      // Enter submits; Shift+Enter falls through to a newline
                window.mobileSend();
            }
        });
    }

    // --- Fullscreen terminal (mobile scroll mode) ---
    // Moves the existing inline terminal into a fixed overlay so
    // one-finger swipe scrolls terminal history.  No new WebSocket is
    // opened — we reuse the existing connection.
    window.exportTerminal = function() {
        if (!activeTermWorker) return;
        var entry = termCache.get(activeTermWorker);
        if (!entry || !entry.serializeAddon) { showToast('Export not available', true); return; }
        try {
            var content = entry.serializeAddon.serialize();
            var blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            var ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
            a.download = activeTermWorker + '-' + ts + '.txt';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(function() { URL.revokeObjectURL(url); }, 5000);
            showToast('Exported: ' + a.download);
        } catch (e) {
            showToast('Export failed: ' + e.message, true);
        }
    };

    window.openTerminalFullscreen = function() {
        if (!activeTermWorker) return;
        var entry = termCache.get(activeTermWorker);
        if (!entry || !entry.term || !entry.ws || !entry.fitAddon) return;
        if (entry.ws.readyState !== WebSocket.OPEN) return;

        var termEl = entry.container;
        if (!termEl) return;
        var origParent = termEl.parentNode;

        var overlay = document.createElement('div');
        overlay.className = 'terminal-fullscreen';
        overlay.id = 'terminal-fullscreen-overlay';
        overlay.innerHTML =
            '<div class="terminal-fullscreen-bar">' +
            '  <span class="fs-title">' + (selectedWorker || 'Terminal') + '</span>' +
            '  <button class="btn-close-fs" id="btn-close-fs">Close</button>' +
            '</div>' +
            '<div class="terminal-fullscreen-body" id="fs-term-body"></div>';
        document.body.appendChild(overlay);

        document.getElementById('fs-term-body').appendChild(termEl);

        function fitAndResize() {
            if (!entry.fitAddon || !entry.term) return;
            entry.fitAddon.fit();
            sendResizeIfChanged(activeTermWorker, entry);
        }

        requestAnimationFrame(function() {
            setTimeout(fitAndResize, 80);
        });

        var vpResize = function() { fitAndResize(); };
        if (window.visualViewport) {
            window.visualViewport.addEventListener('resize', vpResize);
        }

        var body = document.getElementById('fs-term-body');
        var touchStartY = null;
        var accum = 0;
        var LINE_PX = 15;

        body.addEventListener('touchstart', function(e) {
            if (e.touches.length === 1) {
                touchStartY = e.touches[0].clientY;
                accum = 0;
            }
        }, { passive: true });

        body.addEventListener('touchmove', function(e) {
            if (touchStartY === null) return;
            if (!entry.ws || entry.ws.readyState !== WebSocket.OPEN) return;
            var dy = e.touches[0].clientY - touchStartY;
            accum += dy;
            var lines = Math.trunc(accum / LINE_PX);
            if (lines !== 0) {
                entry.ws.send(JSON.stringify({ action: 'scroll', lines: lines }));
                accum -= lines * LINE_PX;
            }
            touchStartY = e.touches[0].clientY;
            e.preventDefault();
        }, { passive: false });

        body.addEventListener('touchend', function() {
            touchStartY = null;
        }, { passive: true });

        document.getElementById('btn-close-fs').addEventListener('click', function() {
            if (window.visualViewport) {
                window.visualViewport.removeEventListener('resize', vpResize);
            }
            origParent.appendChild(termEl);
            overlay.remove();
            requestAnimationFrame(function() {
                setTimeout(fitAndResize, 80);
            });
        });
    }

    // --- Rule Analytics ---
    var _ruleStatsOpen = false;

    window.toggleRuleStats = function(el) {
        _ruleStatsOpen = !_ruleStatsOpen;
        var panel = document.getElementById('rule-stats-panel');
        if (el) el.classList.toggle('active', _ruleStatsOpen);
        if (_ruleStatsOpen) {
            panel.style.display = 'block';
            refreshRuleStats();
        } else {
            panel.style.display = 'none';
        }
    };

    function refreshRuleStats() {
        fetch('/api/drones/rules/analytics?days=7', { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) { return r.json(); })
            .then(function(data) { renderRuleStats(data.analytics || [], data.config_rules || []); })
            .catch(function() {});
    }

    function renderRuleStats(analytics, configRules) {
        var el = document.getElementById('rule-stats-content');
        if (!el) return;
        // Index analytics by pattern for cross-reference
        var byPattern = {};
        for (var i = 0; i < analytics.length; i++) {
            byPattern[analytics[i].rule_pattern] = analytics[i];
        }
        // Find config rules that never fired
        var neverFired = [];
        for (var j = 0; j < configRules.length; j++) {
            if (!byPattern[configRules[j].pattern]) {
                neverFired.push(configRules[j]);
            }
        }

        if (!analytics.length && !neverFired.length) {
            el.innerHTML = '<div class="text-muted text-sm p-md">No rule firing data in the last 7 days</div>';
            return;
        }

        var html = '<table class="rule-stats-table"><thead><tr>'
            + '<th>Pattern</th><th>Source</th><th>Fires</th><th>Approved</th><th>Escalated</th><th>Overrides</th><th>Last Fired</th>'
            + '</tr></thead><tbody>';

        for (var k = 0; k < analytics.length; k++) {
            var a = analytics[k];
            var overrideRate = a.total_fires > 0 ? Math.round(a.override_count / a.total_fires * 100) : 0;
            var overrideClass = overrideRate > 30 ? 'text-poppy' : '';
            var actionClass = a.approve_count > a.escalate_count ? 'text-leaf' : 'text-honey';
            var lastFired = a.last_fired ? new Date(a.last_fired * 1000).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '-';
            var pat = escapeHtml(a.rule_pattern);
            if (pat.length > 50) pat = pat.substring(0, 47) + '...';
            html += '<tr>'
                + '<td class="text-mono text-xs" title="' + escapeHtml(a.rule_pattern) + '">' + pat + '</td>'
                + '<td>' + escapeHtml(a.source) + '</td>'
                + '<td class="' + actionClass + '">' + a.total_fires + '</td>'
                + '<td class="text-leaf">' + a.approve_count + '</td>'
                + '<td class="text-honey">' + a.escalate_count + '</td>'
                + '<td class="' + overrideClass + '">' + a.override_count + (overrideRate > 0 ? ' (' + overrideRate + '%)' : '') + '</td>'
                + '<td class="text-xs text-muted">' + lastFired + '</td>'
                + '</tr>';
        }

        for (var m = 0; m < neverFired.length; m++) {
            var nf = neverFired[m];
            var nfPat = escapeHtml(nf.pattern);
            if (nfPat.length > 50) nfPat = nfPat.substring(0, 47) + '...';
            html += '<tr class="rule-never-fired">'
                + '<td class="text-mono text-xs" title="' + escapeHtml(nf.pattern) + '">' + nfPat + '</td>'
                + '<td>config</td>'
                + '<td colspan="5"><span class="conf-badge conf-low">Never fired</span></td>'
                + '</tr>';
        }

        html += '</tbody></table>';
        el.innerHTML = html;
    }

    // --- Rule Creation Modal ---
    window.showRuleModal = function(detail, proposalId, prePattern) {
        var srcEl = document.getElementById('rule-source-text');
        var patEl = document.getElementById('rule-pattern');
        var actEl = document.getElementById('rule-action');
        var infoEl = document.getElementById('rule-suggestion-info');
        var testEl = document.getElementById('rule-test-result');
        var submitBtn = document.getElementById('rule-submit-btn');
        srcEl.value = detail;
        patEl.value = '';
        actEl.value = 'approve';
        infoEl.style.display = 'none';
        testEl.style.display = 'none';
        // Track proposal ID for submit handler
        var modal = document.getElementById('rule-modal');
        modal.dataset.proposalId = proposalId || '';
        if (submitBtn) {
            submitBtn.textContent = proposalId ? 'Save Rule & Approve' : 'Add Rule';
        }
        modal.style.display = 'flex';

        // Use pre-computed pattern from pilot if available, otherwise call suggest API
        if (prePattern) {
            patEl.value = prePattern;
            infoEl.textContent = 'Pattern from terminal context';
            infoEl.style.display = 'block';
        } else if (detail) {
            fetch('/api/drones/rules/suggest', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
                body: JSON.stringify({ details: [detail], action: 'approve' })
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var s = data.suggestion;
                if (s && s.pattern) {
                    patEl.value = s.pattern;
                    infoEl.textContent = s.explanation + ' (confidence: ' + Math.round(s.confidence * 100) + '%)';
                    infoEl.style.display = 'block';
                }
            })
            .catch(function() {});
        }
    };

    window.hideRuleModal = function() {
        var modal = document.getElementById('rule-modal');
        modal.style.display = 'none';
        modal.dataset.proposalId = '';
    };

    window.testRulePattern = function() {
        var pattern = document.getElementById('rule-pattern').value.trim();
        var source = document.getElementById('rule-source-text').value;
        var resultEl = document.getElementById('rule-test-result');
        if (!pattern) {
            resultEl.innerHTML = '<span class="text-poppy">Enter a pattern first</span>';
            resultEl.style.display = 'block';
            return;
        }
        fetch('/api/config/approval-rules/dry-run', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'Dashboard',
                'Authorization': 'Bearer ' + wsToken()
            },
            body: JSON.stringify({
                content: source,
                rules: [{ pattern: pattern, action: document.getElementById('rule-action').value }]
            })
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                resultEl.innerHTML = '<span class="text-poppy">' + escapeHtml(data.error) + '</span>';
            } else {
                var r = data.results[0];
                if (r.matched) {
                    resultEl.innerHTML = '<span class="text-leaf">Pattern matches (' + r.decision + ' via ' + r.source + ')</span>';
                } else {
                    resultEl.innerHTML = '<span class="text-honey">No match on source text</span>';
                }
            }
            resultEl.style.display = 'block';
        })
        .catch(function() {
            resultEl.innerHTML = '<span class="text-poppy">Test failed</span>';
            resultEl.style.display = 'block';
        });
    };

    window.submitRule = function() {
        var pattern = document.getElementById('rule-pattern').value.trim();
        var action = document.getElementById('rule-action').value;
        var posEl = document.getElementById('rule-position');
        var modal = document.getElementById('rule-modal');
        var proposalId = modal ? modal.dataset.proposalId : '';
        if (!pattern) { showToast('Pattern is required', true); return; }

        if (proposalId) {
            // Proposal-linked: save rule AND approve the proposal
            var body = new FormData();
            body.append('proposal_id', proposalId);
            body.append('pattern', pattern);
            actionFetch('/action/proposal/approve-always', { body: body })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error) { showToast('Error: ' + data.error, true); return; }
                showToast('Rule added & approved');
                hideRuleModal();
                refreshProposals();
                refreshTasks();
                if (_ruleStatsOpen) refreshRuleStats();
            })
            .catch(function() { showToast('Request failed', true); });
        } else {
            // Standalone rule creation
            var ruleBody = { pattern: pattern, action: action };
            var posVal = posEl.value;
            if (posVal !== '') ruleBody.position = parseInt(posVal, 10);

            fetch('/api/config/approval-rules', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'Dashboard',
                    'Authorization': 'Bearer ' + wsToken()
                },
                body: JSON.stringify(ruleBody)
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error) {
                    showToast('Error: ' + data.error, true);
                } else {
                    showToast('Rule added');
                    window._toastApplyResult(data, 'Add rule');
                    hideRuleModal();
                    if (_ruleStatsOpen) refreshRuleStats();
                }
            })
            .catch(function() { showToast('Failed to add rule', true); });
        }
    };

    // --- Tab switcher ---
    // `restoring` skips the focus-mode/panel side effects so the on-load
    // tab restore doesn't fight the collapse/focus restores that ran above.
    window.switchTab = function(tab, restoring) {
        if (!restoring) {
            exitFocusMode();
            expandBottomPanel();
        }
        try { sessionStorage.setItem('swarm_bottom_tab', tab); } catch(e) {}
        document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); });
        document.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
        var btn = document.getElementById('tab-' + tab + '-btn');
        if (!btn) return;
        btn.classList.add('active');
        btn.setAttribute('aria-selected', 'true');
        document.getElementById('tab-' + tab).classList.add('active');
        // Per-tab visibility for the tab-header-utils action buttons. Each
        // button declares `data-show-on-tab="<tab>"`; we toggle inline
        // display based on whichever tab is now active. Buttons WITHOUT
        // the attribute show on every tab (current behaviour preserved).
        document.querySelectorAll('[data-show-on-tab]').forEach(function(el) {
            el.style.display = el.dataset.showOnTab === tab ? '' : 'none';
        });
        if (tab === 'decisions') {
            refreshProposals();
            refreshDecisions();
            if (_ruleStatsOpen) refreshRuleStats();
        } else if (tab === 'pipelines') {
            refreshPipelines();
        } else if (tab === 'playbooks') {
            refreshPlaybooks();
        } else if (tab === 'queen') {
            refreshQueenHistory();
        } else if (tab === 'messages') {
            refreshMessages();
        } else if (tab === 'buzz') {
            unreadNotifications = 0;
            var badge = document.getElementById('notif-badge');
            if (badge) badge.style.display = 'none';
            refreshBuzzLog();
        } else if (tab === 'loops') {
            refreshStandingLoops();
        } else if (tab === 'harness') {
            refreshHarness();
        }
    }

    // --- Harness-improvement digest tab (#789) ---
    // Operator-gated hill-climbing (LangChain Loop 4): surface the improvement
    // signals Swarm already mines (error-prone tools, suggested approval rules,
    // playbook win-rates, dreamer patterns, override tuning) with one-click
    // apply for ONLY the low-risk actions that carry an apply_action. Display-
    // only items (tool/prompt rewrites) never get a button.
    var _harnessApply = {};   // index -> apply_action {endpoint, method, body}

    function renderHarness(data) {
        var summary = document.getElementById('harness-summary');
        if (summary) {
            summary.textContent = (data.actionable || 0) + ' actionable / '
                + (data.suggestions || []).length + ' total';
        }
        var box = document.getElementById('harness-digest-list');
        if (!box) return;
        var items = data.suggestions || [];
        if (!items.length) {
            box.innerHTML = '<div class="muted" style="padding:10px;">No harness '
                + 'improvements suggested yet — the signals build up as the swarm runs.</div>';
            return;
        }
        _harnessApply = {};
        var html = '';
        items.forEach(function(s, i) {
            var pct = Math.round((s.confidence || 0) * 100);
            var applyBtn = '';
            if (s.apply_action) {
                _harnessApply[i] = s.apply_action;   // keep JSON OUT of the DOM
                applyBtn = '<button class="btn btn-xs btn-approve" data-action="harnessApply" '
                    + 'data-harness-idx="' + i + '">Apply</button>';
            }
            html += '<div class="task-item" style="display:flex;justify-content:space-between;gap:8px;">'
                + '<span><strong>' + escapeHtml(s.title) + '</strong> '
                + '<span class="muted">[' + escapeHtml(s.type) + ' · ' + pct + '%]</span><br>'
                + '<span class="muted" style="font-size:0.85em;">' + escapeHtml(s.detail || '') + '</span></span>'
                + '<span class="flex-center">' + applyBtn + '</span></div>';
        });
        box.innerHTML = html;
    }

    function refreshHarness() {
        fetch('/api/harness-digest')
            .then(function(r) { return r.json(); })
            .then(renderHarness)
            .catch(function(err) { showToast('Harness digest load failed: ' + err.message, true); });
    }

    function harnessApply(el) {
        var action = _harnessApply[el.dataset.harnessIdx];
        if (!action) return;
        fetch(action.endpoint, {
            method: action.method || 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
            body: JSON.stringify(action.body || {})
        })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data && data.error) { showToast(data.error, true); return; }
                showToast('Applied — refreshing digest');
                refreshHarness();
            })
            .catch(function(err) { showToast('Apply failed: ' + err.message, true); });
    }

    // --- Standing background-improvement loops tab (#765) ---
    // Operator controls (per-worker start/pause/stop + global kill switch)
    // and a live per-loop token-burn readout, backed by /api/standing-loops.
    function renderStandingLoops(data) {
        var killBtn = document.getElementById('standing-kill-btn');
        if (killBtn) {
            var on = !!data.kill_switch;
            killBtn.textContent = 'Kill switch: ' + (on ? 'ON' : 'off');
            killBtn.classList.toggle('btn-danger', on);
        }
        var capLabel = document.getElementById('standing-cap-label');
        if (capLabel) {
            capLabel.textContent = 'Daily cap: ' + (data.daily_token_cap > 0
                ? Number(data.daily_token_cap).toLocaleString() + ' output tokens' : 'unlimited');
        }
        var box = document.getElementById('standing-loops-list');
        if (!box) return;
        var loops = data.loops || [];
        if (!loops.length) {
            box.innerHTML = '<div class="muted" style="padding:10px;">No standing loops yet. '
                + 'Start one on an idle worker to file background-improvement tasks.</div>';
            return;
        }
        var html = '';
        loops.forEach(function(l) {
            var state = !l.enabled ? 'off' : (l.paused ? 'paused' : (l.exhausted ? 'asleep (cap)' : 'running'));
            var pct = l.daily_token_cap > 0 ? Math.min(100, Math.round(100 * l.tokens_in_window / l.daily_token_cap)) : 0;
            html += '<div class="task-item flex-center gap-sm" style="justify-content:space-between;">'
                + '<span><strong>' + escapeHtml(l.worker) + '</strong> '
                + '<span class="muted">— ' + state + '</span><br>'
                + '<span class="muted" style="font-size:0.85em;">burn: '
                + Number(l.tokens_in_window).toLocaleString()
                + (l.daily_token_cap > 0 ? ' / ' + Number(l.daily_token_cap).toLocaleString() + ' (' + pct + '%)' : '')
                + ' output tokens this window</span></span>'
                + '<span class="flex-center gap-sm">'
                + '<button class="btn btn-xs btn-approve" data-action="standingLoopStart" data-worker="' + escapeHtml(l.worker) + '">Start</button>'
                + '<button class="btn btn-xs btn-secondary" data-action="standingLoopPause" data-worker="' + escapeHtml(l.worker) + '">Pause</button>'
                + '<button class="btn btn-xs btn-secondary" data-action="standingLoopStop" data-worker="' + escapeHtml(l.worker) + '">Stop</button>'
                + '</span></div>';
        });
        box.innerHTML = html;
    }

    function refreshStandingLoops() {
        fetch('/api/standing-loops')
            .then(function(r) { return r.json(); })
            .then(renderStandingLoops)
            .catch(function(err) { showToast('Standing loops load failed: ' + err.message, true); });
    }

    function standingLoopPost(path, body) {
        fetch('/api/standing-loops/' + path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
            body: JSON.stringify(body || {})
        })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data && data.error) { showToast(data.error, true); return; }
                renderStandingLoops(data);
            })
            .catch(function(err) { showToast('Standing loop action failed: ' + err.message, true); });
    }

    // --- Queen history tab (B4): searchable archive of Queen chat threads ---
    var _qhFilters = { status: '', kind: '', worker: '', since: '', until: '', q: '' };
    var _qhLimit = 50;
    var _qhOffset = 0;
    var _qhSearchTimer = null;
    var _qhLiveTimer = null;
    var _qhDetailThread = null;
    var _QH_KIND_CLASS = {
        'escalation': 'text-poppy', 'queen-escalation': 'text-poppy',
        'oversight': 'text-honey', 'anomaly': 'text-honey',
        'proposal': 'text-leaf', 'operator': 'text-lavender',
        'worker-message': 'text-muted'
    };

    function _qhDateToEpoch(dateStr, endOfDay) {
        if (!dateStr) return '';
        var d = new Date(dateStr + (endOfDay ? 'T23:59:59' : 'T00:00:00'));
        return isNaN(d.getTime()) ? '' : Math.floor(d.getTime() / 1000);
    }

    function _qhQueryString() {
        var p = [];
        if (_qhFilters.status) p.push('status=' + encodeURIComponent(_qhFilters.status));
        if (_qhFilters.kind) p.push('kind=' + encodeURIComponent(_qhFilters.kind));
        if (_qhFilters.worker) p.push('worker=' + encodeURIComponent(_qhFilters.worker));
        if (_qhFilters.q) p.push('q=' + encodeURIComponent(_qhFilters.q));
        var since = _qhDateToEpoch(_qhFilters.since, false);
        var until = _qhDateToEpoch(_qhFilters.until, true);
        if (since) p.push('since=' + since);
        if (until) p.push('until=' + until);
        p.push('limit=' + _qhLimit);
        p.push('offset=' + _qhOffset);
        return p.join('&');
    }

    function refreshQueenHistory() {
        _qhOffset = 0;
        _qhFetch(false);
    }

    function qhLoadMore() {
        _qhOffset += _qhLimit;
        _qhFetch(true);
    }

    function _qhFetch(append) {
        var list = document.getElementById('queen-history-list');
        if (!list) return;
        if (!append) list.innerHTML = '<div class="empty-state"><div class="mt-sm">Loading…</div></div>';
        fetch('/api/queen/threads?' + _qhQueryString(), { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var threads = (data && data.threads) || [];
                if (!append) _qhPopulateWorkers(threads);
                _qhRender(threads, append);
            })
            .catch(function() {
                if (!append) list.innerHTML = '<div class="empty-state">Failed to load threads.</div>';
            });
    }

    function _qhRender(threads, append) {
        var list = document.getElementById('queen-history-list');
        var moreWrap = document.getElementById('qh-load-more-wrap');
        if (!list) return;
        if (!append && threads.length === 0) {
            var anyFilter = _qhFilters.status || _qhFilters.kind || _qhFilters.worker
                || _qhFilters.q || _qhFilters.since || _qhFilters.until;
            list.innerHTML = anyFilter
                ? '<div class="empty-state"><div class="mt-sm">No threads match — clear filters.</div></div>'
                : '<div class="empty-state"><div class="mt-sm">The Queen hasn\'t opened any threads yet.</div></div>';
            if (moreWrap) moreWrap.style.display = 'none';
            return;
        }
        var html = threads.map(_qhRow).join('');
        if (append) list.insertAdjacentHTML('beforeend', html);
        else list.innerHTML = html;
        if (moreWrap) moreWrap.style.display = (threads.length >= _qhLimit) ? 'block' : 'none';
        formatLocalTimes(list);
    }

    function _qhRow(t) {
        var kindCls = _QH_KIND_CLASS[t.kind] || 'text-muted';
        var statusCls = t.status === 'resolved' ? 'text-muted' : 'text-leaf';
        var worker = t.worker_name
            ? '<span class="text-muted text-xs">· ' + escapeHtml(t.worker_name) + '</span>' : '';
        var taskLink = t.task_id ? '<span class="text-muted text-xs">· task</span>' : '';
        var count = t.message_count || 0;
        return '<div class="qh-row" data-action="qhOpenDetail" data-thread-id="'
            + escapeHtml(t.id) + '" role="button" tabindex="0">'
            + '<span class="qh-kind ' + kindCls + '">' + escapeHtml(t.kind) + '</span>'
            + '<span class="qh-title">' + escapeHtml(t.title) + '</span>'
            + '<span class="' + statusCls + ' text-xs">' + escapeHtml(t.status) + '</span>'
            + '<span class="local-time text-muted text-xs" data-ts="' + t.updated_at + '"></span>'
            + '<span class="text-muted text-xs">' + count + ' msg</span>'
            + worker + taskLink
            + '</div>';
    }

    function _qhPopulateWorkers(threads) {
        var sel = document.getElementById('qh-filter-worker');
        if (!sel) return;
        var names = {};
        document.querySelectorAll('.worker-item[data-worker]').forEach(function(el) {
            if (el.dataset.worker) names[el.dataset.worker] = true;
        });
        (threads || []).forEach(function(t) { if (t.worker_name) names[t.worker_name] = true; });
        var current = sel.value;
        var opts = ['<option value="">all workers</option>'];
        Object.keys(names).sort().forEach(function(n) {
            opts.push('<option value="' + escapeHtml(n) + '"'
                + (n === current ? ' selected' : '') + '>' + escapeHtml(n) + '</option>');
        });
        sel.innerHTML = opts.join('');
    }

    function qhSwitchStatus(status) {
        _qhFilters.status = status || '';
        document.querySelectorAll('#qh-filter-bar [data-qh-status]').forEach(function(b) {
            b.classList.toggle('active', (b.dataset.qhStatus || '') === _qhFilters.status);
        });
        refreshQueenHistory();
    }

    function qhFilterChanged() {
        var kind = document.getElementById('qh-filter-kind');
        var worker = document.getElementById('qh-filter-worker');
        var since = document.getElementById('qh-filter-since');
        var until = document.getElementById('qh-filter-until');
        _qhFilters.kind = kind ? kind.value : '';
        _qhFilters.worker = worker ? worker.value : '';
        _qhFilters.since = since ? since.value : '';
        _qhFilters.until = until ? until.value : '';
        refreshQueenHistory();
    }

    function qhSearchChanged(val) {
        _qhFilters.q = (val || '').trim();
        if (_qhSearchTimer) clearTimeout(_qhSearchTimer);
        _qhSearchTimer = setTimeout(refreshQueenHistory, 250);
    }

    function qhOpenDetail(threadId) {
        var modal = document.getElementById('qh-detail-modal');
        var body = document.getElementById('qh-detail-body');
        var title = document.getElementById('qh-detail-title');
        if (!modal || !body) return;
        body.innerHTML = '<div class="empty-state">Loading…</div>';
        modal.style.display = 'flex';
        fetch('/api/queen/threads/' + encodeURIComponent(threadId), { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) { if (!r.ok) throw new Error('unavailable'); return r.json(); })
            .then(function(data) {
                var thread = data.thread || {};
                var msgs = data.messages || [];
                _qhDetailThread = thread;
                if (title) title.textContent = thread.title || 'Thread';
                body.innerHTML = _qhRenderTranscript(thread, msgs);
                formatLocalTimes(body);
            })
            .catch(function() {
                _qhDetailThread = null;
                body.innerHTML = '<div class="empty-state">This thread is no longer available.</div>';
            });
    }

    function _qhRenderTranscript(thread, msgs) {
        var parts = [];
        if (thread.status === 'resolved') {
            parts.push('<div class="text-muted text-sm mb-sm">Resolved'
                + (thread.resolved_by ? ' by ' + escapeHtml(thread.resolved_by) : '')
                + (thread.resolution_reason ? ': ' + escapeHtml(thread.resolution_reason) : '')
                + '</div>');
        }
        if (!msgs.length) {
            parts.push('<div class="empty-state">No messages.</div>');
        }
        msgs.forEach(function(m) {
            parts.push('<div class="qh-msg qh-msg-' + escapeHtml(m.role) + '">'
                + '<div class="qh-msg-head"><span class="text-xs text-muted">' + escapeHtml(m.role) + '</span> '
                + '<span class="local-time text-xs text-muted" data-ts="' + m.ts + '"></span></div>'
                + '<div class="qh-msg-content">' + escapeHtml(m.content) + '</div>'
                + '</div>');
        });
        // Footer: resolved → reopen-and-reply composer; active → deep-link to CC.
        if (thread.status === 'resolved') {
            parts.push('<div class="qh-reopen">'
                + '<textarea id="qh-reopen-text" class="modal-input" rows="2" placeholder="Reply to reopen this thread…"></textarea>'
                + '<button class="btn btn-sm" data-action="qhReopenSend">Reopen &amp; reply</button>'
                + '</div>');
        } else {
            parts.push('<div class="qh-reopen">'
                + '<span class="text-muted text-sm">This thread is still active.</span> '
                + '<button class="btn btn-sm btn-secondary" data-action="qhViewInCC">View in command center</button>'
                + '</div>');
        }
        return parts.join('');
    }

    function qhReopenSend() {
        var t = _qhDetailThread;
        var ta = document.getElementById('qh-reopen-text');
        if (!t || !ta) return;
        var body = (ta.value || '').trim();
        if (!body) { showToast('Enter a reply to reopen', true); return; }
        fetch('/api/queen/threads/' + encodeURIComponent(t.id) + '/reopen', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
            body: JSON.stringify({ body: body }),
        })
            .then(function(r) { if (!r.ok) throw new Error('reopen failed'); return r.json(); })
            .then(function() {
                showToast('Thread reopened — forwarded to the Queen');
                qhOpenDetail(t.id);   // re-render as active w/ the new message
                refreshQueenHistory();
            })
            .catch(function() { showToast('Failed to reopen thread', true); });
    }

    function qhViewInCC() {
        qhHideDetail();
        if (typeof window.ccShowDashboard === 'function') window.ccShowDashboard();
    }

    function qhMaybeLiveRefresh() {
        var panel = document.getElementById('tab-queen');
        if (!panel || !panel.classList.contains('active')) return;
        if (_qhLiveTimer) clearTimeout(_qhLiveTimer);
        _qhLiveTimer = setTimeout(refreshQueenHistory, 400);
    }

    function qhHideDetail() {
        var modal = document.getElementById('qh-detail-modal');
        if (modal) modal.style.display = 'none';
        _qhDetailThread = null;
    }

    // --- Messages tab (B10): operator view of inter-worker traffic ---
    // READ-ONLY: this view must never call mark_read — worker read-state
    // drives the coordination nudges and the operator browsing must not
    // touch it.
    var _msgFilters = { q: '', unread_only: false, since: '', until: '' };
    var _msgLimit = 50;
    var _msgOffset = 0;
    var _msgSearchTimer = null;
    var _msgLiveTimer = null;
    var _msgGroups = [];
    var _msgSelectMode = false;
    var _msgSelectedIds = {};   // id -> true
    var _MSG_TYPE_CLASS = {
        'warning': 'text-poppy', 'dependency': 'text-honey',
        'finding': 'text-leaf', 'status': 'text-lavender',
        'operator': 'text-muted', 'note': 'text-muted'
    };

    function _msgQueryString() {
        var p = [];
        if (_msgFilters.q) p.push('q=' + encodeURIComponent(_msgFilters.q));
        if (_msgFilters.unread_only) p.push('unread_only=true');
        var since = _qhDateToEpoch(_msgFilters.since, false);
        var until = _qhDateToEpoch(_msgFilters.until, true);
        if (since) p.push('since=' + since);
        if (until) p.push('until=' + until);
        p.push('limit=' + _msgLimit);
        p.push('offset=' + _msgOffset);
        return p.join('&');
    }

    function refreshMessages() {
        _msgOffset = 0;
        _msgFetch(false);
    }

    function msgLoadMore() {
        _msgOffset += _msgLimit;
        _msgFetch(true);
    }

    function _msgFetch(append) {
        var list = document.getElementById('messages-list');
        if (!list) return;
        if (!append) {
            list.innerHTML = '<div class="empty-state"><div class="mt-sm">Loading…</div></div>';
            _msgGroups = [];
        }
        fetch('/api/messages?' + _msgQueryString(), { headers: { 'X-Requested-With': 'Dashboard' }})
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var msgs = (data && data.messages) || [];
                _msgRender(_msgCollapse(msgs), append, msgs.length);
            })
            .catch(function() {
                if (!append) list.innerHTML = '<div class="empty-state">Failed to load messages.</div>';
            });
    }

    // Collapse a `*` broadcast (one DB row per recipient) back into one
    // logical row. Group by sender+type+content within a tight created_at
    // window; the flat list is created_at DESC so broadcast rows are adjacent.
    function _msgCollapse(messages) {
        var groups = [];
        var byKey = {};
        messages.forEach(function(m) {
            var key = m.from + '|' + m.type + '|' + m.content;
            var g = byKey[key];
            if (g && Math.abs(g.created_at - m.created_at) <= 3) {
                g.members.push(m);
            } else {
                g = { from: m.from, type: m.type, content: m.content,
                      created_at: m.created_at, members: [m] };
                byKey[key] = g;
                groups.push(g);
            }
        });
        return groups;
    }

    function _msgRender(groups, append, rawCount) {
        var list = document.getElementById('messages-list');
        var moreWrap = document.getElementById('msg-load-more-wrap');
        if (!list) return;
        if (!append && groups.length === 0) {
            var anyFilter = _msgFilters.q || _msgFilters.unread_only
                || _msgFilters.since || _msgFilters.until;
            list.innerHTML = anyFilter
                ? '<div class="empty-state"><div class="mt-sm">No messages match — clear filters.</div></div>'
                : '<div class="empty-state"><div class="mt-sm">Workers haven\'t sent any messages yet.</div></div>';
            if (moreWrap) moreWrap.style.display = 'none';
            return;
        }
        var base = _msgGroups.length;
        _msgGroups = append ? _msgGroups.concat(groups) : groups;
        var html = groups.map(function(g, i) { return _msgRow(g, base + i); }).join('');
        if (append) list.insertAdjacentHTML('beforeend', html);
        else list.innerHTML = html;
        // Load-more keys off raw row count vs page size (pre-collapse), so a
        // page that's all broadcasts still pages correctly.
        if (moreWrap) moreWrap.style.display = (rawCount >= _msgLimit) ? 'block' : 'none';
        formatLocalTimes(list);
    }

    function _msgRow(g, idx) {
        var cls = _MSG_TYPE_CLASS[g.type] || 'text-muted';
        // Content is truncated in the list (CSS ellipsis); the full text is
        // shown in the click-through detail modal.
        var content = '<span class="msg-content">' + escapeHtml(g.content) + '</span>';
        var time = '<span class="local-time text-muted text-xs" data-ts="' + g.created_at + '"></span>';
        var typeBadge = '<span class="msg-type ' + cls + '">' + escapeHtml(g.type) + '</span>';
        var ids = g.members.map(function(m) { return m.id; }).join(',');
        var checked = g.members.every(function(m) { return _msgSelectedIds[m.id]; }) ? ' checked' : '';
        var cb = '<input type="checkbox" class="msg-select-cb" data-msg-ids="' + ids + '"'
            + checked + ' style="display:' + (_msgSelectMode ? 'inline' : 'none') + '">';
        var route, tail;
        if (g.members.length > 1) {
            var readN = g.members.filter(function(m) { return m.read_at; }).length;
            route = escapeHtml(g.from) + ' → * (' + g.members.length + ')';
            tail = '<span class="text-xs text-muted">' + readN + '/' + g.members.length + ' read</span>';
        } else {
            var m = g.members[0];
            route = escapeHtml(m.from) + ' → ' + escapeHtml(m.to);
            tail = '<span class="msg-dot ' + (m.read_at ? 'msg-read' : 'msg-unread') + '" title="'
                + (m.read_at ? 'read' : 'unread') + '"></span>';
        }
        return '<div class="msg-row" data-action="msgOpenDetail" data-msg-group="' + idx
            + '" role="button" tabindex="0">'
            + cb + typeBadge
            + '<span class="msg-route text-xs text-muted">' + route + '</span>'
            + content + time + tail
            + '</div>';
    }

    function msgOpenDetail(idx) {
        var g = _msgGroups[idx];
        if (!g) return;
        var modal = document.getElementById('msg-detail-modal');
        var body = document.getElementById('msg-detail-body');
        var title = document.getElementById('msg-detail-title');
        if (!modal || !body) return;
        var cls = _MSG_TYPE_CLASS[g.type] || 'text-muted';
        var route = (g.members.length > 1)
            ? (escapeHtml(g.from) + ' → * (' + g.members.length + ' recipients)')
            : (escapeHtml(g.from) + ' → ' + escapeHtml(g.members[0].to));
        if (title) title.textContent = g.type + ' message';
        var parts = [
            '<div class="text-sm text-muted mb-sm"><span class="msg-type ' + cls + '">'
            + escapeHtml(g.type) + '</span> ' + route
            + ' <span class="local-time" data-ts="' + g.created_at + '"></span></div>',
            '<div class="qh-msg-content">' + escapeHtml(g.content) + '</div>',
        ];
        if (g.members.length > 1) {
            parts.push('<div class="text-sm text-muted mt-md mb-sm">Recipients:</div>');
            g.members.forEach(function(m) {
                parts.push('<div class="msg-sub"><span class="text-xs">' + escapeHtml(m.to)
                    + '</span> <span class="msg-dot ' + (m.read_at ? 'msg-read' : 'msg-unread')
                    + '" title="' + (m.read_at ? 'read' : 'unread') + '"></span></div>');
            });
        }
        body.innerHTML = parts.join('');
        formatLocalTimes(body);
        modal.style.display = 'flex';
    }

    function msgHideDetail() {
        var modal = document.getElementById('msg-detail-modal');
        if (modal) modal.style.display = 'none';
    }

    function msgSearchChanged(val) {
        _msgFilters.q = (val || '').trim();
        if (_msgSearchTimer) clearTimeout(_msgSearchTimer);
        _msgSearchTimer = setTimeout(refreshMessages, 250);
    }

    function msgFilterChanged() {
        var unread = document.getElementById('msg-filter-unread');
        var since = document.getElementById('msg-filter-since');
        var until = document.getElementById('msg-filter-until');
        _msgFilters.unread_only = unread ? unread.checked : false;
        _msgFilters.since = since ? since.value : '';
        _msgFilters.until = until ? until.value : '';
        refreshMessages();
    }

    function msgMaybeLiveRefresh() {
        var panel = document.getElementById('tab-messages');
        if (!panel || !panel.classList.contains('active')) return;
        if (_msgLiveTimer) clearTimeout(_msgLiveTimer);
        _msgLiveTimer = setTimeout(refreshMessages, 400);
    }

    // Compose — operator sends a message to a worker or broadcasts.
    function msgToggleCompose() {
        var box = document.getElementById('msg-compose');
        if (!box) return;
        var showing = box.style.display !== 'none';
        box.style.display = showing ? 'none' : 'flex';
        if (!showing) _msgPopulateComposeTargets();
    }

    function _msgPopulateComposeTargets() {
        var sel = document.getElementById('msg-compose-to');
        if (!sel) return;
        var current = sel.value;
        var names = [];
        document.querySelectorAll('.worker-item[data-worker]').forEach(function(el) {
            if (el.dataset.worker) names.push(el.dataset.worker);
        });
        names.sort();
        var opts = ['<option value="*">* (broadcast)</option>'];
        names.forEach(function(n) {
            opts.push('<option value="' + escapeHtml(n) + '"'
                + (n === current ? ' selected' : '') + '>' + escapeHtml(n) + '</option>');
        });
        sel.innerHTML = opts.join('');
        if (current) sel.value = current;
    }

    function msgSendCompose() {
        var to = document.getElementById('msg-compose-to');
        var type = document.getElementById('msg-compose-type');
        var content = document.getElementById('msg-compose-content');
        if (!to || !type || !content) return;
        var body = (content.value || '').trim();
        if (!body) { showToast('Enter a message', true); return; }
        fetch('/api/messages/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
            body: JSON.stringify({ from: 'operator', to: to.value, type: type.value, content: body }),
        })
            .then(function(r) { if (!r.ok) throw new Error('send failed'); return r.json(); })
            .then(function(data) {
                showToast(data.fanout
                    ? ('Broadcast sent to ' + data.fanout + ' worker(s)')
                    : ('Message sent to ' + to.value));
                content.value = '';
                refreshMessages();
            })
            .catch(function() { showToast('Failed to send message', true); });
    }

    // Bulk delete — multi-select + delete via /api/messages/delete.
    function msgToggleSelect() {
        _msgSelectMode = !_msgSelectMode;
        var toggle = document.getElementById('msg-select-toggle');
        var actions = document.getElementById('msg-bulk-actions');
        if (toggle) toggle.classList.toggle('btn-active', _msgSelectMode);
        if (actions) actions.style.display = _msgSelectMode ? 'inline-flex' : 'none';
        if (!_msgSelectMode) _msgSelectedIds = {};
        document.querySelectorAll('.msg-select-cb').forEach(function(cb) {
            cb.style.display = _msgSelectMode ? 'inline' : 'none';
            if (!_msgSelectMode) cb.checked = false;
        });
        _msgUpdateBulkCount();
    }

    function msgClearSelect() {
        _msgSelectedIds = {};
        document.querySelectorAll('.msg-select-cb').forEach(function(cb) { cb.checked = false; });
        _msgUpdateBulkCount();
    }

    function _msgUpdateBulkCount() {
        var el = document.getElementById('msg-bulk-count');
        if (el) el.textContent = Object.keys(_msgSelectedIds).length + ' selected';
    }

    function msgBulkDelete() {
        var ids = Object.keys(_msgSelectedIds).map(Number);
        if (!ids.length) { showToast('No messages selected', true); return; }
        showConfirm('Delete ' + ids.length + ' message(s)?', function() {
            fetch('/api/messages/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
                body: JSON.stringify({ ids: ids }),
            })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    showToast((data.deleted || 0) + ' message(s) deleted');
                    _msgSelectedIds = {};
                    _msgUpdateBulkCount();
                    refreshMessages();
                })
                .catch(function() { showToast('Failed to delete messages', true); });
        });
    }

    // --- Mobile overflow menu ---
    window.toggleMobileMenu = function(e) {
        e.stopPropagation();
        var menu = document.getElementById('mobile-overflow-menu');
        menu.classList.toggle('open');
    };
    window.closeMobileMenu = function() {
        var menu = document.getElementById('mobile-overflow-menu');
        if (menu) menu.classList.remove('open');
    };
    document.addEventListener('click', function() { closeMobileMenu(); closeTabUtils(); });

    // --- Focus mode (mobile): hide entire bottom panel ---
    function toggleFocusMode() {
        var area = document.querySelector('.detail-area');
        if (!area) return;
        var active = area.classList.toggle('focus-mode');
        var btn = document.getElementById('btn-focus-mode');
        if (btn) btn.textContent = active ? 'Exit Focus' : 'Focus';
        try { sessionStorage.setItem('swarm_focus_mode', active ? '1' : ''); } catch(e) {}
        // Re-fit terminal to fill new space
        if (activeTermWorker) {
            var entry = termCache.get(activeTermWorker);
            if (entry && entry.fitAddon && entry.term) {
                setTimeout(function() { entry.fitAddon.fit(); sendResizeIfChanged(activeTermWorker, entry); }, 50);
            }
        }
    }

    function exitFocusMode() {
        var area = document.querySelector('.detail-area');
        if (!area || !area.classList.contains('focus-mode')) return;
        area.classList.remove('focus-mode');
        var btn = document.getElementById('btn-focus-mode');
        if (btn) btn.textContent = 'Focus';
        try { sessionStorage.setItem('swarm_focus_mode', ''); } catch(e) {}
    }

    // --- Bottom panel collapse (mobile) ---
    function updateBottomPanelState(collapsed) {
        // FAB visibility
        var fab = document.getElementById('bottom-panel-fab');
        if (fab) fab.style.display = (collapsed && window.innerWidth <= 768) ? 'block' : 'none';
        // Sync class on detail-area for grid layout (fallback for :has())
        var area = document.querySelector('.detail-area');
        if (area) area.classList.toggle('bottom-collapsed', collapsed);
    }
    function toggleBottomPanel() {
        var panel = document.querySelector('.bottom-tabbed');
        if (!panel) return;
        var collapsed = panel.classList.toggle('collapsed');
        var chevron = panel.querySelector('.btn-collapse');
        if (chevron) chevron.textContent = collapsed ? '\u25BC' : '\u25B2';
        updateBottomPanelState(collapsed);
        try { sessionStorage.setItem('swarm_bottom_collapsed', collapsed ? '1' : ''); } catch(e) {}
    }
    function expandBottomPanel() {
        var panel = document.querySelector('.bottom-tabbed');
        if (!panel || !panel.classList.contains('collapsed')) return;
        panel.classList.remove('collapsed');
        var chevron = panel.querySelector('.btn-collapse');
        if (chevron) chevron.textContent = '\u25B2';
        updateBottomPanelState(false);
        try { sessionStorage.setItem('swarm_bottom_collapsed', ''); } catch(e) {}
    }
    // Init: collapse on mobile by default (respect sessionStorage override)
    (function initBottomPanel() {
        var panel = document.querySelector('.bottom-tabbed');
        if (!panel) return;
        var isMobile = window.innerWidth < 768;
        var stored = null;
        try { stored = sessionStorage.getItem('swarm_bottom_collapsed'); } catch(e) {}
        var shouldCollapse = stored === '1' || (stored === null && isMobile);
        if (shouldCollapse) {
            panel.classList.add('collapsed');
            var chevron = panel.querySelector('.btn-collapse');
            if (chevron) chevron.textContent = '\u25BC';
            updateBottomPanelState(true);
        }
    })();

    // Init: restore focus mode on mobile
    (function initFocusMode() {
        var focusStored = null;
        try { focusStored = sessionStorage.getItem('swarm_focus_mode'); } catch(e) {}
        if (focusStored === '1' && window.innerWidth < 768) {
            var area = document.querySelector('.detail-area');
            if (area) area.classList.add('focus-mode');
            var fbtn = document.getElementById('btn-focus-mode');
            if (fbtn) fbtn.textContent = 'Exit Focus';
        }
    })();

    // Init: restore the active bottom-panel tab across reloads. Runs after
    // the collapse/focus restores so switchTab's restore path (which skips
    // expandBottomPanel/exitFocusMode) can't undo them.
    (function initBottomTab() {
        var storedTab = null;
        try { storedTab = sessionStorage.getItem('swarm_bottom_tab'); } catch(e) {}
        if (storedTab && storedTab !== 'tasks' && document.getElementById('tab-' + storedTab + '-btn')) {
            switchTab(storedTab, true);
        }
    })();

    // --- Tab header utilities toggle (mobile) ---
    function toggleTabUtils() {
        var el = document.querySelector('.tab-header-utils');
        if (el) el.classList.toggle('open');
    }
    function closeTabUtils() {
        var el = document.querySelector('.tab-header-utils');
        if (el) el.classList.remove('open');
    }

    // --- Actions ---
    window.toggleDrones = function() {
        actionFetch('/action/toggle-drones', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data && data.error) {
                    // Phase 9 of #328: surface failure to the operator
                    // instead of silently flipping nothing.
                    showToast('Drones toggle failed: ' + data.error, true);
                    return;
                }
                updateDronesButton(data.enabled);
                showToast('Drones ' + (data.enabled ? 'ON' : 'OFF'));
                refreshStatus();
            })
            .catch(function(err) {
                showToast('Drones toggle failed: ' + (err && err.message || 'request failed'), true);
            });
    }

    function updateDronesButton(enabled) {
        const btn = document.getElementById('drones-btn');
        btn.textContent = 'Drones: ' + (enabled ? 'ON' : 'OFF');
        btn.className = 'btn ' + (enabled ? 'btn-active' : 'btn-secondary');
    }

    // --- Tunnel ---
    var tunnelUrl = _swarmCfg.tunnelUrl;
    var tunnelActionPending = false; // suppress duplicate WS toasts during user-initiated actions

    function updateTunnelButton(running, url) {
        tunnelUrl = url || '';
        const btn = document.getElementById('tunnel-btn');
        btn.textContent = running ? 'Tunnel: ON' : 'Tunnel';
        btn.className = 'btn ' + (running ? 'btn-active' : 'btn-secondary');
    }

    window.tunnelAction = function() {
        if (tunnelUrl) {
            showTunnelModal(tunnelUrl);
        } else {
            const btn = document.getElementById('tunnel-btn');
            btn.textContent = 'Starting...';
            btn.disabled = true;
            tunnelActionPending = true;
            actionFetch('/action/tunnel/start', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    btn.disabled = false;
                    tunnelActionPending = false;
                    if (data.error) {
                        showToast('Tunnel error: ' + data.error, true);
                        updateTunnelButton(false, '');
                    } else {
                        tunnelUrl = data.url;
                        updateTunnelButton(true, data.url);
                        showTunnelModal(data.url);
                        if (data.warning) showToast(data.warning, true);
                    }
                })
                .catch(function() {
                    btn.disabled = false;
                    tunnelActionPending = false;
                    updateTunnelButton(false, '');
                    showToast('Failed to start tunnel', true);
                });
        }
    }

    function showTunnelModal(url) {
        document.getElementById('tunnel-url-text').textContent = url;
        document.getElementById('tunnel-modal').style.display = 'flex';
        // Generate QR code
        var container = document.getElementById('tunnel-qr');
        container.innerHTML = '';
        if (typeof QRCode !== 'undefined') {
            try {
                new QRCode(container, {
                    text: url,
                    width: 220,
                    height: 220,
                    colorDark: '#E6D2B5',
                    colorLight: '#2A1B0E',
                    correctLevel: QRCode.CorrectLevel.M
                });
            } catch(e) {
                console.error('QR generation failed:', e);
            }
        }
    }

    window.hideTunnel = function() {
        document.getElementById('tunnel-modal').style.display = 'none';
    }

    window.copyTunnelUrl = function() {
        if (tunnelUrl && navigator.clipboard) {
            navigator.clipboard.writeText(tunnelUrl);
            showToast('URL copied');
        }
    }

    window.stopTunnel = function() {
        hideTunnel();
        tunnelActionPending = true;
        actionFetch('/action/tunnel/stop', { method: 'POST' })
            .then(r => r.json())
            .then(function() {
                tunnelActionPending = false;
                updateTunnelButton(false, '');
                showToast('Tunnel stopped');
            });
    }

    window.continueAll = function() {
        actionFetch('/action/continue-all', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                showToast('Continued ' + data.count + ' worker(s)');
                setTimeout(refreshWorkers, 1000);
            });
    }

    window.doAction = function(action, command) {
        if (action === 'revive') { reviveWorker(); return; }
        if (action === 'refresh') { refreshInlineTerminal(); return; }
        if (action === 'kill') { killWorker(); return; }
        if (action === 'merge') { mergeWorker(); return; }
        if (action === 'escape') { sendSpecialKey('escape'); return; }
        if (action === 'arrow_up') { sendSpecialKey('arrow-up'); return; }
        if (action === 'arrow_down') { sendSpecialKey('arrow-down'); return; }
        if (action === 'arrow_right') { sendSpecialKey('arrow-right'); return; }
        if (action === 'arrow_left') { sendSpecialKey('arrow-left'); return; }
        if (action === 'export') { exportTerminal(); return; }
        // Custom button: send command or continue
        if (command) { sendToolCommand(command); } else { continueWorker(); }
    }

    window.sendSpecialKey = function(key) {
        if (!selectedWorker) return;
        actionFetch('/action/' + key + '/' + encodeURIComponent(selectedWorker), { method: 'POST' })
            .then(function() { showToast(key + ' sent to ' + selectedWorker); });
    }

    window.mergeWorker = function() {
        if (!selectedWorker) return;
        fetch('/api/workers/' + encodeURIComponent(selectedWorker) + '/merge', {
            method: 'POST',
            headers: { 'X-Requested-With': 'Dashboard' }
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                showToast('Merged ' + selectedWorker + ': ' + data.message);
            } else {
                showToast('Merge failed: ' + data.message, true);
            }
            refreshWorkers();
        })
        .catch(function() { showToast('Merge request failed', true); });
    }

    window.sendToolCommand = function(command) {
        if (!selectedWorker) return;
        var form = new FormData();
        form.append('message', command);
        actionFetch('/action/send/' + selectedWorker, { method: 'POST', body: form });
    }

    window.continueWorker = function() {
        if (!selectedWorker) return;
        actionFetch('/action/continue/' + encodeURIComponent(selectedWorker), { method: 'POST' })
            .then(function() {
                showToast('Continued ' + selectedWorker);
                setTimeout(refreshWorkers, 1000);
            });
    }

    window.reviveWorker = function() {
        if (!selectedWorker) return;
        actionFetch('/action/revive/' + selectedWorker, { method: 'POST' })
            .then(r => r.json())
            .then(function() {
                showToast('Reviving ' + selectedWorker);
                setTimeout(refreshDetail, 2000);
            });
    }

    window.killWorker = function() {
        if (!selectedWorker) return;
        showConfirm('Kill worker "' + selectedWorker + '"? This will terminate the process.', function() {
            destroyTermEntry(selectedWorker);
            actionFetch('/action/kill/' + selectedWorker, { method: 'POST' })
                .then(r => r.json())
                .then(function() {
                    showToast('Killed ' + selectedWorker, true);
                    selectedWorker = null;
                    var _dt = document.getElementById('detail-title-text') || document.getElementById('detail-title');
                    if (_dt) _dt.textContent = 'Select a worker';
                    document.getElementById('detail-body').innerHTML = '<p class="placeholder-text">Click a worker to see details</p>';
                    document.getElementById('terminal-actions').style.display = 'none';
                    refreshWorkers();
                });
        });
    }

    // --- Unified Task Modal (create + edit) ---
    let taskModalMode = null; // 'create' or 'edit'
    let taskModalId = null;   // task ID when editing
    let taskModalPendingFiles = []; // files queued during create mode
    let taskModalAttachmentPaths = []; // pre-saved attachment paths (e.g. from email)
    let taskModalSourceEmailId = ''; // Graph message ID for email-sourced tasks

    window.showCreateTask = function() {
        openTaskModal('create');
    };

    window.previewJiraSync = function() {
        fetch('/api/jira/preview', { headers: { 'X-Requested-With': 'Dashboard' } })
            .then(function(r) {
                if (!r.ok) return r.json().then(function(d) { showToast(d.error || 'Jira preview failed', true); });
                return r.json();
            })
            .then(function(data) {
                if (!data || !('count' in data)) return;
                if (data.error) {
                    showToast('Jira API error: ' + data.error + '\nJQL: ' + (data.jql || ''), true);
                    return;
                }
                if (data.count === 0) {
                    showToast('Jira preview: no new issues found\nJQL: ' + (data.jql || ''));
                    return;
                }
                var lines = data.tasks.map(function(t) {
                    return t.jira_key + ' — ' + t.title + ' (' + t.type + ', ' + t.priority + ')';
                });
                showToast('Preview: ' + data.count + ' issue(s) ready to import:\n' + lines.join('\n'));
            })
            .catch(function() { showToast('Jira preview failed', true); });
    };

    window.syncJira = function() {
        fetch('/api/jira/sync', {
            method: 'POST',
            headers: { 'X-Requested-With': 'Dashboard' },
        })
            .then(function(r) {
                if (!r.ok) return r.json().then(function(d) { showToast(d.error || 'Jira sync failed', true); });
                return r.json();
            })
            .then(function(data) {
                if (!data || data.error) return;
                if (data.imported === 0) showToast('Jira sync: no new issues');
                else showToast('Jira sync: imported ' + data.imported + ' issue(s)');
            })
            .catch(function() { showToast('Jira sync failed', true); });
    };

    // --- Import from Outlook (Microsoft Graph) ---------------------------
    // Direct drag from Outlook-for-Mac can't reach the browser (macOS
    // promised-file drags), so this picker pulls the inbox via Graph and
    // creates task(s) server-side — no dragging required.

    window.showOutlookImport = function() {
        var modal = document.getElementById('outlook-import-modal');
        if (!modal) return;
        modal.style.display = 'flex';
        var selectAll = document.getElementById('oi-select-all');
        if (selectAll) selectAll.checked = false;
        loadOutlookMessages();
    };

    window.hideOutlookImport = function() {
        var modal = document.getElementById('outlook-import-modal');
        if (modal) modal.style.display = 'none';
    };

    function updateOutlookSelCount() {
        var checked = document.querySelectorAll('#oi-list .oi-cb:checked').length;
        var countEl = document.getElementById('oi-count');
        if (countEl) countEl.textContent = checked;
        var sep = document.getElementById('oi-separate-btn');
        var merge = document.getElementById('oi-merge-btn');
        if (sep) { sep.disabled = checked < 1; sep.textContent = checked > 1 ? 'Create ' + checked + ' tasks' : 'Create task'; }
        if (merge) merge.disabled = checked < 2;  // merging one email == a single task
    }

    function loadOutlookMessages() {
        var list = document.getElementById('oi-list');
        var status = document.getElementById('oi-status');
        if (list) list.innerHTML = '';
        if (status) status.textContent = 'Loading inbox…';
        updateOutlookSelCount();
        fetch('/api/outlook/messages?limit=25', { headers: { 'X-Requested-With': 'fetch' } })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data.connected) {
                    if (status) status.textContent = data.error || 'Outlook not connected';
                    if (list) list.innerHTML = '<div class="text-sm text-muted" style="padding:0.6rem">Connect Microsoft Graph in Settings to import from Outlook.</div>';
                    return;
                }
                var msgs = data.messages || [];
                if (status) status.textContent = msgs.length + ' recent message(s)';
                if (!msgs.length) { if (list) list.innerHTML = '<div class="text-sm text-muted" style="padding:0.6rem">Inbox is empty.</div>'; return; }
                var html = '';
                for (var i = 0; i < msgs.length; i++) {
                    var m = msgs[i];
                    var when = '';
                    try { when = m.received ? new Date(m.received).toLocaleString() : ''; } catch (e) { when = m.received || ''; }
                    var dot = m.is_read ? '<span class="msg-dot msg-read"></span>' : '<span class="msg-dot msg-unread"></span>';
                    html += '<label class="oi-row" style="display:flex; gap:0.5rem; align-items:flex-start; padding:0.4rem 0.5rem; border-bottom:1px solid var(--border); cursor:pointer">'
                        + '<input type="checkbox" class="oi-cb" data-id="' + escapeHtml(m.id) + '" style="margin-top:0.25rem; flex:0 0 auto">'
                        + '<span style="flex:0 0 auto; margin-top:0.35rem">' + dot + '</span>'
                        + '<span style="min-width:0; flex:1 1 auto">'
                          + '<span style="display:block; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">' + escapeHtml(m.subject) + '</span>'
                          + '<span class="text-sm text-muted" style="display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">' + escapeHtml(m.from_name || m.from) + (when ? ' · ' + escapeHtml(when) : '') + '</span>'
                          + '<span class="text-sm text-muted" style="display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">' + escapeHtml(m.preview || '') + '</span>'
                        + '</span></label>';
                }
                if (list) list.innerHTML = html;
                updateOutlookSelCount();
            })
            .catch(function(err) { if (status) status.textContent = 'Error loading inbox: ' + err; });
    }

    window.submitOutlookImport = function(mode) {
        var ids = [];
        document.querySelectorAll('#oi-list .oi-cb:checked').forEach(function(cb) {
            if (cb.dataset.id) ids.push(cb.dataset.id);
        });
        if (!ids.length) return;
        var sep = document.getElementById('oi-separate-btn');
        var merge = document.getElementById('oi-merge-btn');
        if (sep) sep.disabled = true;
        if (merge) merge.disabled = true;
        showToast('Importing ' + ids.length + ' email(s)…');
        fetch('/api/tasks/from-outlook', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
            body: JSON.stringify({ message_ids: ids, mode: mode })
        })
            .then(function(r) {
                if (!r.ok) return r.json().then(function(d) { throw new Error(d.error || 'import failed'); });
                return r.json();
            })
            .then(function(data) {
                var n = data.count || 0;
                var msg = mode === 'merge'
                    ? 'Merged ' + ids.length + ' emails into 1 task'
                    : 'Created ' + n + ' task(s) from Outlook';
                if (data.errors && data.errors.length) msg += ' (' + data.errors.length + ' failed)';
                showToast(msg);
                hideOutlookImport();
                if (typeof refreshTasks === 'function') refreshTasks();
            })
            .catch(function(err) {
                showToast('Outlook import error: ' + err.message, true);
                updateOutlookSelCount();  // re-enable buttons per current selection
            });
    };

    // Selection changes inside the Outlook picker (delegated — rows are
    // rendered dynamically). Select-all toggles every visible checkbox.
    document.addEventListener('change', function(e) {
        if (e.target && e.target.id === 'oi-select-all') {
            var on = e.target.checked;
            document.querySelectorAll('#oi-list .oi-cb').forEach(function(cb) { cb.checked = on; });
            updateOutlookSelCount();
        } else if (e.target && e.target.classList && e.target.classList.contains('oi-cb')) {
            updateOutlookSelCount();
        }
    });

    // Match a Jira issue URL or bare key. Picks up cloud (atlassian.net),
    // self-hosted (jira.<host>/browse/KEY-N), and bare KEY-N strings.
    var JIRA_KEY_RE = /([A-Z][A-Z0-9_]+-\d+)/;
    function detectJiraKey(text) {
        if (!text) return '';
        var trimmed = String(text).trim();
        if (/\/browse\//i.test(trimmed) || /atlassian\.net/i.test(trimmed)) {
            var m = trimmed.toUpperCase().match(JIRA_KEY_RE);
            return m ? m[1] : '';
        }
        // Bare KEY-N drop (only if the entire payload is a single key).
        if (JIRA_KEY_RE.test(trimmed.toUpperCase()) && trimmed.length < 40 && !/\s/.test(trimmed)) {
            return trimmed.toUpperCase().match(JIRA_KEY_RE)[1];
        }
        return '';
    }

    function importJiraByKey(key) {
        showToast('Importing Jira ' + key + '...');
        return actionFetch('/api/jira/import-by-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: 'key=' + encodeURIComponent(key),
        })
            .then(function(r) { return r.json().then(function(data) { return { ok: r.ok, data: data }; }); })
            .then(function(res) {
                if (!res.ok) {
                    showToast('Jira import failed: ' + (res.data.error || 'unknown'), true);
                    return;
                }
                if (res.data.duplicate) {
                    showToast('Already imported: ' + res.data.jira_key + ' — ' + res.data.title);
                } else {
                    showToast('Imported ' + res.data.jira_key + ': ' + res.data.title);
                }
                refreshTasks();
            })
            .catch(function(err) { showToast('Jira import error: ' + err, true); });
    }

    window.handleEmailDrop = function(event) {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
        var dt = event.dataTransfer;
        var files = dt && dt.files;
        var items = dt && dt.items;

        // Debug: log what Outlook provides
        var types = dt ? [].slice.call(dt.types) : [];
        console.log('[email-drop] types:', types, 'files:', files ? files.length : 0, 'items:', items ? items.length : 0);
        if (files) { for (var d = 0; d < files.length; d++) console.log('[email-drop] file:', files[d].name, files[d].type, files[d].size); }

        // 0. Look for a Jira issue URL/key in any text payload before anything else.
        var jiraTextSources = [
            dt && dt.getData('text/uri-list'),
            dt && dt.getData('text/x-moz-url'),
            dt && dt.getData('text/plain'),
            dt && dt.getData('text/html'),
        ];
        for (var jt = 0; jt < jiraTextSources.length; jt++) {
            var jKey = detectJiraKey(jiraTextSources[jt]);
            if (jKey) { importJiraByKey(jKey); return; }
        }

        // 1. Look for .eml or .msg files (also check items API)
        var emailFile = null;
        if (files && files.length > 0) {
            for (var i = 0; i < files.length; i++) {
                var name = files[i].name.toLowerCase();
                if (name.endsWith('.eml') || name.endsWith('.msg')) { emailFile = files[i]; break; }
            }
        }
        if (!emailFile && items && items.length > 0) {
            for (var ii = 0; ii < items.length; ii++) {
                if (items[ii].kind === 'file') {
                    var f = items[ii].getAsFile();
                    if (f) {
                        var fn = f.name.toLowerCase();
                        if (fn.endsWith('.eml') || fn.endsWith('.msg') || f.type === 'application/vnd.ms-outlook') {
                            emailFile = f; break;
                        }
                    }
                }
            }
        }

        if (emailFile) {
            showToast('Parsing email: ' + emailFile.name);
            var fd = new FormData();
            fd.append('file', emailFile);
            fetch('/api/tasks/from-email', { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch' } })
                .then(function(r) {
                    if (!r.ok) return r.text().then(function(t) { throw new Error(t); });
                    return r.json();
                })
                .then(function(data) {
                    if (data.error) { showToast('Email parse failed: ' + data.error, true); return; }
                    openTaskModal('create', { title: data.title || '', desc: data.description || '', task_type: data.task_type || '' });
                    taskModalAttachmentPaths = data.attachments || [];
                    taskModalSourceEmailId = data.message_id || '';
                    for (var j = 0; j < taskModalAttachmentPaths.length; j++) {
                        addThumbnail(taskModalAttachmentPaths[j]);
                    }
                    // Client-side auto-classify fallback
                    if (!data.task_type) {
                        var detected = autoClassifyType((data.title || '') + ' ' + (data.description || ''));
                        if (detected) document.getElementById('tm-task-type').value = detected;
                    }
                })
                .catch(function(err) { showToast('Email parse error: ' + err, true); });
            return;
        }

        // 2. New Outlook drag — extract subject + message ID, fetch via Graph if configured.
        // Checked BEFORE text/plain so we don't short-circuit on the subject-only
        // string Outlook puts in text/plain when both payloads are present.
        if (types.indexOf('multimaillistmessagerows') !== -1) {
            console.log('[email-drop] section 2 — multimaillistmessagerows present, attempting Graph fetch');
            var rowData = dt.getData('multimaillistmessagerows');
            console.log('[email-drop] multimaillistmessagerows data:', rowData);
            var outlookData = null;
            try { outlookData = JSON.parse(rowData); } catch (pe) { /* not JSON */ }
            if (outlookData) {
                var subj = (outlookData.subjects && outlookData.subjects[0]) || '';
                var msgId = (outlookData.latestItemIds && outlookData.latestItemIds[0]) || '';
                var userEmail = '';
                if (outlookData.mailboxInfos && outlookData.mailboxInfos[0]) {
                    userEmail = outlookData.mailboxInfos[0].mailboxSmtpAddress || '';
                }
                console.log('[email-drop] subject:', subj, 'msgId:', msgId, 'user:', userEmail);

                // Build a fallback description from text/html or text/plain in
                // case Graph isn't configured or returns empty body. Strip
                // tags from HTML, drop the bare-subject case ("Subject: …").
                var fallbackHtml = dt && dt.getData('text/html');
                var fallbackText = dt && dt.getData('text/plain');
                var fallbackDesc = (fallbackHtml || fallbackText || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
                if (fallbackDesc && fallbackDesc.toLowerCase().replace(/\s+/g, ' ') === ('Subject: ' + subj).toLowerCase()) {
                    // Outlook's text/plain is just the subject — useless; force user to paste.
                    fallbackDesc = '';
                }

                function openWithFallback() {
                    openTaskModal('create', { title: subj, desc: fallbackDesc });
                    if (fallbackDesc) {
                        showToast('Email "' + subj + '" — body captured from drag payload');
                    } else {
                        showToast('Email "' + subj + '" — paste body (Ctrl+V) and drag images to add them');
                    }
                }

                // Try Graph API fetch if configured
                if (msgId) {
                    showToast('Fetching email via Microsoft Graph...');
                    actionFetch('/action/fetch-outlook-email', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                        body: 'message_id=' + encodeURIComponent(msgId) + '&user=' + encodeURIComponent(userEmail)
                    })
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        console.log('[email-drop] Graph response:', data);
                        if (data.error) {
                            console.warn('[email-drop] Graph error:', data.error);
                            openWithFallback();
                            return;
                        }
                        // Graph might return an empty body (rare; usually means
                        // the email is signature-only). Prefer the drag payload
                        // text in that case.
                        var graphDesc = data.description || '';
                        var bodyOnly = graphDesc.replace(/^Subject:\s*[^\n]*\n+/i, '').trim();
                        if (!bodyOnly && fallbackDesc) {
                            graphDesc = fallbackDesc;
                        }
                        openTaskModal('create', {
                            title: data.title || subj,
                            desc: graphDesc,
                            task_type: data.task_type || ''
                        });
                        taskModalAttachmentPaths = data.attachments || [];
                        taskModalSourceEmailId = data.message_id || '';
                        for (var ai = 0; ai < taskModalAttachmentPaths.length; ai++) {
                            addThumbnail(taskModalAttachmentPaths[ai]);
                        }
                        // Fallback: run client-side auto-classify if backend didn't set type
                        if (!data.task_type) {
                            var detected = autoClassifyType((data.title || subj) + ' ' + graphDesc);
                            if (detected) document.getElementById('tm-task-type').value = detected;
                        }
                        var imgCount = taskModalAttachmentPaths.length;
                        showToast('Email imported' + (imgCount > 0 ? ' with ' + imgCount + ' attachment(s)' : ''));
                    })
                    .catch(function() { openWithFallback(); });
                    return;
                }

                // No message ID — fall back to drag-payload text
                openWithFallback();
                return;
            }
            showToast('New Outlook drag detected — paste the email (Ctrl+V) instead', true);
            return;
        }

        // 3. Old Outlook desktop drag — text/html or text/plain.
        // Run text/html through the same htmlToMarkdown walker the paste path uses
        // so headings, lists, paragraphs and inline marks survive. Falls back to
        // text/plain if no HTML payload is present.
        var html = dt && dt.getData('text/html');
        var text = dt && dt.getData('text/plain');
        console.log('[email-drop] section 3 fallback — html=' + (html ? html.length : 0) + ' bytes, plain=' + (text ? text.length : 0) + ' bytes');
        if (html && html.length > 80) {
            var doc = new DOMParser().parseFromString(html, 'text/html');
            var md = htmlToMarkdown(doc.body);
            if (md.text.trim()) {
                openTaskModal('create', { title: '', desc: md.text });
                showToast('Email content captured — review and create');
                return;
            }
        }
        if (text && text.trim()) {
            openTaskModal('create', { title: '', desc: text.trim() });
            showToast('Email content captured (plain text only) — review and create');
            return;
        }

        // 4. Files present but not email — attach them
        if (files && files.length > 0) {
            openTaskModal('create');
            for (var k = 0; k < files.length; k++) {
                handleTaskFile(files[k]);
            }
            showToast(files.length + ' file(s) added as attachments');
            return;
        }

        showToast('No email data found in drop. Copy the email (Ctrl+C) and paste here (Ctrl+V) instead.', true);
    };

    window.showEditTask = function(taskId, title, desc, priority, taskType, tags, deps, resolution, status, isCross, sourceWorker, targetWorker, depType, acceptance, contextRefs, attachments, assignedWorker) {
        openTaskModal('edit', { id: taskId, title: title, desc: desc, priority: priority, task_type: taskType, tags: tags, deps: deps, resolution: resolution || '', status: status || '', is_cross_project: isCross === 'true', source_worker: sourceWorker || '', target_worker: targetWorker || '', dep_type: depType || 'blocks', acceptance: acceptance || '', context_refs: contextRefs || '', attachments: attachments || '', assigned_worker: assignedWorker || '' });
    };

    function openTaskModal(mode, data) {
        taskModalMode = mode;
        taskModalId = (data && data.id) || null;
        taskModalPendingFiles = [];
        taskModalAttachmentPaths = [];
        taskModalSourceEmailId = '';

        document.getElementById('tm-title').value = (data && data.title) || '';
        document.getElementById('tm-desc').value = (data && data.desc) || '';
        document.getElementById('tm-priority').value = (data && data.priority) || 'normal';
        document.getElementById('tm-task-type').value = (data && data.task_type) || '';
        document.getElementById('tm-tags').value = (data && data.tags) || '';
        document.getElementById('tm-deps').value = (data && data.deps) || '';
        document.getElementById('tm-attachments').innerHTML = '';

        // Load existing attachments in edit mode
        if (data && data.attachments) {
            var paths = data.attachments.split(',').filter(Boolean);
            taskModalAttachmentPaths = paths;
            for (var ai = 0; ai < paths.length; ai++) addThumbnail(paths[ai]);
        }

        var header = document.getElementById('task-modal-header');
        var titleEl = document.getElementById('task-modal-title');
        var submitBtn = document.getElementById('tm-submit-btn');
        var descEl = document.getElementById('tm-desc');
        var titleHint = document.getElementById('tm-title-hint');

        // Tags row always visible
        document.getElementById('tm-tags-row').style.display = '';

        // Status field — visible in both create and edit. In create mode the
        // default is 'backlog' (parking lot); the smart default below flips
        // it to 'assigned' if the operator picks a target worker, unless
        // they've already manually overridden the dropdown.
        var statusRow = document.getElementById('tm-status-row');
        statusRow.style.display = '';
        var statusEl = document.getElementById('tm-status');
        if (mode === 'edit' && data && data.status) {
            statusEl.value = data.status;
        } else {
            statusEl.value = 'backlog';
        }
        // Stash original so submitTaskModal can tell whether the operator
        // actually touched the dropdown vs. just opened the modal. Without
        // this we'd send `status=<original>` in the edit POST and clobber
        // any state transition fired by the worker-reassign step (which
        // arrives via /action/task/assign first).
        statusEl.dataset.original = statusEl.value;
        // Reset the user-override flag so the smart default works again on
        // the next open. Set whenever the operator interacts with the picker.
        statusEl.dataset.userPicked = '';
        statusEl.onchange = function() { statusEl.dataset.userPicked = '1'; };

        // Resolution display (read-only, completed tasks only)
        var resolutionRow = document.getElementById('tm-resolution-row');
        var resolutionEl = document.getElementById('tm-resolution');
        if (mode === 'edit' && data && data.resolution && data.status === 'done') {
            resolutionRow.style.display = 'block';
            resolutionEl.textContent = data.resolution;
        } else {
            resolutionRow.style.display = 'none';
            resolutionEl.textContent = '';
        }

        submitBtn.disabled = false;
        if (mode === 'create') {
            titleEl.textContent = 'New Task';
            submitBtn.textContent = 'Create';
            header.style.background = 'var(--lavender)';
            descEl.rows = 24;
            if (titleHint) titleHint.style.display = '';
        } else {
            titleEl.textContent = 'Edit Task';
            submitBtn.textContent = 'Save';
            header.style.background = 'var(--panel)';
            descEl.rows = 4;
            if (titleHint) titleHint.style.display = 'none';
        }

        // Cross-project fields — populate worker selects from DOM
        var workerNames = [];
        document.querySelectorAll('.worker-item[data-worker]').forEach(function(el) {
            workerNames.push(el.dataset.worker);
        });
        ['tm-source-worker', 'tm-target-worker', 'tm-worker'].forEach(function(id) {
            var sel = document.getElementById(id);
            if (!sel) return;
            var prev = sel.value;
            sel.innerHTML = '<option value="">—</option>';
            workerNames.forEach(function(n) {
                var opt = document.createElement('option');
                opt.value = n; opt.textContent = n;
                sel.appendChild(opt);
            });
            sel.value = prev;
        });
        // Top-level "Assign to" picker — primary worker selector for both
        // create and edit. In edit mode this preselects the task's current
        // assigned_worker so the operator can reassign without diving into
        // the cross-project Advanced section.
        var workerEl = document.getElementById('tm-worker');
        if (workerEl) {
            var initialWorker = (data && data.assigned_worker) || '';
            workerEl.value = initialWorker;
            // Stash the originally-assigned worker so submitTaskModal can
            // tell whether the operator changed it (and fire an assign
            // request only when they did). Reset on every open so the
            // diff is per-modal-session, not stale across re-edits.
            workerEl.dataset.original = initialWorker;
        }

        // Advanced fields: cross-project + acceptance + context refs + depends-on.
        // Populated unconditionally; visibility is controlled by the <details>
        // wrapper. Auto-opens only when the task already has data in any of
        // these fields (edit mode) so a fresh "New Task" stays clean.
        var sourceWorkerVal = (data && data.source_worker) || '';
        var targetWorkerVal = (data && data.target_worker) || '';
        var depTypeVal = (data && data.dep_type) || 'blocks';
        var acceptanceVal = (data && data.acceptance) || '';
        var contextRefsVal = (data && data.context_refs) || '';
        var depsVal = (data && data.deps) || '';
        document.getElementById('tm-source-worker').value = sourceWorkerVal;
        document.getElementById('tm-target-worker').value = targetWorkerVal;
        document.getElementById('tm-dep-type').value = depTypeVal;
        document.getElementById('tm-acceptance').value = acceptanceVal;
        document.getElementById('tm-context-refs').value = contextRefsVal;
        // tm-deps is set above (line 3866) but keep it here defensively.

        // Smart status default in create mode — picking the top-level
        // "Assign to" worker bumps the status default to "assigned" unless
        // the operator already picked a status manually. Edit mode is
        // hands-off (the existing status wins).
        if (mode === 'create') {
            var assignSel = document.getElementById('tm-worker');
            if (assignSel) {
                assignSel.onchange = function() {
                    if (statusEl.dataset.userPicked) return;
                    statusEl.value = assignSel.value ? 'assigned' : 'backlog';
                };
            }
        }

        var advancedFilledCount = [
            sourceWorkerVal,
            targetWorkerVal,
            acceptanceVal,
            contextRefsVal,
            depsVal,
        ].filter(function(v) { return v && String(v).trim(); }).length;
        var advanced = document.getElementById('tm-advanced');
        var badge = document.getElementById('tm-advanced-badge');
        if (advanced) advanced.open = advancedFilledCount > 0;
        if (badge) {
            if (advancedFilledCount > 0) {
                badge.textContent = advancedFilledCount;
                badge.style.display = '';
            } else {
                badge.style.display = 'none';
            }
        }

        document.getElementById('task-modal').style.display = 'flex';
        // Reset the source-toggle to rich-edit mode so each open starts
        // WYSIWYG. Power users can still toggle "View source" mid-edit.
        var sourceToggle = document.getElementById('tm-source-toggle');
        if (sourceToggle && sourceToggle.checked) {
            sourceToggle.checked = false;
            // Manually mirror the toggle's change handler — show rich, hide source.
            var richEl = document.getElementById('tm-desc-rich');
            if (richEl) richEl.style.display = '';
            descEl.style.display = 'none';
        }
        // Render the markdown source into the contenteditable surface.
        if (typeof _updateTaskMdPreview === 'function') _updateTaskMdPreview();
        // Focus the visible editor.
        if (mode === 'create') {
            var richFocus = document.getElementById('tm-desc-rich');
            if (richFocus) richFocus.focus(); else descEl.focus();
        } else {
            document.getElementById('tm-title').focus();
        }
    }

    // Client-side auto-classify: mirrors Python keyword logic
    function autoClassifyType(text) {
        text = text.toLowerCase();
        var bugKw = ['bug','fix','broken','crash','error','fail','issue','defect','regression','wrong','incorrect','not working'];
        var verifyKw = ['verify','check','confirm','test','validate','qa','review','ensure','audit','inspect'];
        var featureKw = ['add','new','feature','implement','create','build','introduce','support','enable','extend'];
        var bugScore = 0, verifyScore = 0, featureScore = 0;
        bugKw.forEach(function(kw) { if (text.indexOf(kw) !== -1) bugScore++; });
        verifyKw.forEach(function(kw) { if (text.indexOf(kw) !== -1) verifyScore++; });
        featureKw.forEach(function(kw) { if (text.indexOf(kw) !== -1) featureScore++; });
        var best = Math.max(bugScore, verifyScore, featureScore);
        if (best === 0) return '';
        var scores = [bugScore, verifyScore, featureScore];
        if (scores.filter(function(s) { return s === best; }).length > 1) return '';
        if (bugScore === best) return 'bug';
        if (verifyScore === best) return 'verify';
        return 'feature';
    }

    // Auto-detect type on description blur (only when type is set to
    // Auto-detect). Listens on both the rich editor and the source textarea
    // — whichever is currently visible.
    function _autoDetectTaskType() {
        var typeEl = document.getElementById('tm-task-type');
        if (!typeEl || typeEl.value !== '' || taskModalMode !== 'create') return;
        var title = document.getElementById('tm-title').value;
        var desc = document.getElementById('tm-desc').value;
        var detected = autoClassifyType(title + ' ' + desc);
        if (detected) typeEl.value = detected;
    }
    document.getElementById('tm-desc').addEventListener('blur', _autoDetectTaskType);
    var _richDescEl = document.getElementById('tm-desc-rich');
    if (_richDescEl) _richDescEl.addEventListener('blur', _autoDetectTaskType);

    // Rich-text task description — contenteditable WYSIWYG editor backed by
    // a hidden markdown-source textarea. ``_updateTaskMdPreview`` keeps its
    // old name (lots of callers) but now means "rebuild the rich editor's
    // HTML from the current markdown source". Use this after any code path
    // that mutates ``tm-desc.value`` directly (paste, image-upload swap,
    // initial open). For inline typing in the rich editor we mirror the
    // other direction (HTML → markdown) on every input event so the source
    // textarea is always in sync for form submission.
    var _updateTaskMdPreview;
    (function() {
        var rich = document.getElementById('tm-desc-rich');
        var src = document.getElementById('tm-desc');
        var toggle = document.getElementById('tm-source-toggle');
        if (!rich || !src) return;

        var rafToken = null;
        // Renders source markdown into the rich editor. Wipes/replaces the
        // editor's content and resets the cursor — only call when the source
        // changed externally (paste, image-upload swap, modal open). DON'T
        // call from the rich editor's own input handler or we'll fight the
        // cursor on every keystroke.
        function renderRich() {
            rich.innerHTML = renderMarkdown(src.value);
        }
        function scheduleRender() {
            if (rafToken) return;
            rafToken = requestAnimationFrame(function() {
                rafToken = null;
                renderRich();
            });
        }
        _updateTaskMdPreview = scheduleRender;

        // Rich → source: serialize the contenteditable's HTML back to
        // markdown and write it to the hidden textarea. The htmlToMarkdown
        // walker leaves image tags and links as ``![alt](src)`` / ``[txt](url)``
        // and emits paragraph/heading/list/blockquote markdown — same shape
        // we use for paste + Jira + email.
        function syncToSource() {
            var md = htmlToMarkdown(rich);
            src.value = md.text;
        }
        rich.addEventListener('input', syncToSource);
        rich.addEventListener('blur', syncToSource);

        // View-source toggle: shows the raw markdown textarea so power users
        // can edit fences / URLs / link targets without fighting the editor.
        // Toggling back re-renders from the (possibly-edited) source. The
        // formatting toolbar is hidden in source mode (no rich surface to
        // act on).
        var toolbar = document.getElementById('tm-desc-toolbar');
        if (toggle) {
            toggle.addEventListener('change', function() {
                if (toggle.checked) {
                    syncToSource();
                    rich.style.display = 'none';
                    src.style.display = '';
                    if (toolbar) toolbar.classList.add('tm-source-active');
                    src.focus();
                } else {
                    renderRich();
                    src.style.display = 'none';
                    rich.style.display = '';
                    if (toolbar) toolbar.classList.remove('tm-source-active');
                    rich.focus();
                }
            });
        }

        // Toolbar buttons. Each has data-md-cmd; the handlers use
        // document.execCommand for primitives the browser supports natively
        // and small Range manipulations for the rest. Sync to source after
        // every action so the markdown stays current.
        if (toolbar) {
            toolbar.addEventListener('mousedown', function(e) {
                // Prevent the mousedown from stealing focus / clearing the
                // selection. Buttons can still be activated via click.
                if (e.target.closest('.md-tool')) e.preventDefault();
            });
            toolbar.addEventListener('click', function(e) {
                var btn = e.target.closest('.md-tool');
                if (!btn) return;
                var cmd = btn.getAttribute('data-md-cmd');
                if (!cmd) return;
                e.preventDefault();
                _runMdCommand(cmd, rich);
                syncToSource();
            });
        }
    })();

    // Format-toolbar action runner. Lives outside the IIFE-scope closure so
    // it can be unit-tested or invoked programmatically. ``rich`` must be a
    // contenteditable element with the current selection inside it.
    function _runMdCommand(cmd, rich) {
        rich.focus();
        switch (cmd) {
            case 'bold':
            case 'italic':
                document.execCommand(cmd);
                return;
            case 'strike':
                document.execCommand('strikeThrough');
                return;
            case 'h1':
            case 'h2':
            case 'h3':
                // formatBlock wraps the current paragraph; it accepts
                // 'h1'/'h2' or '<h1>'/'<h2>' depending on browser, but
                // always accepts the latter.
                document.execCommand('formatBlock', false, '<' + cmd + '>');
                return;
            case 'quote':
                document.execCommand('formatBlock', false, '<blockquote>');
                return;
            case 'ul':
                document.execCommand('insertUnorderedList');
                return;
            case 'ol':
                document.execCommand('insertOrderedList');
                return;
            case 'link': {
                var sel = window.getSelection();
                var hasSel = sel && sel.rangeCount > 0 && !sel.getRangeAt(0).collapsed;
                var url = window.prompt('Link URL:');
                if (!url) return;
                if (!/^[a-z]+:|^\//i.test(url)) url = 'https://' + url;
                if (hasSel) {
                    document.execCommand('createLink', false, url);
                } else {
                    // No selection — insert as link with the URL as the visible text.
                    document.execCommand('insertHTML', false,
                        '<a href="' + url.replace(/"/g, '&quot;') + '">' + url + '</a>');
                }
                return;
            }
            case 'code': {
                // Wrap selection in <code>; if no selection, insert a stub.
                var sel2 = window.getSelection();
                if (sel2 && sel2.rangeCount && !sel2.getRangeAt(0).collapsed) {
                    var range = sel2.getRangeAt(0);
                    var content = range.extractContents();
                    var code = document.createElement('code');
                    code.appendChild(content);
                    range.insertNode(code);
                    // Move cursor after the inserted code element.
                    sel2.removeAllRanges();
                    var after = document.createRange();
                    after.setStartAfter(code);
                    after.collapse(true);
                    sel2.addRange(after);
                } else {
                    document.execCommand('insertHTML', false, '<code>code</code>');
                }
                return;
            }
            case 'hr':
                document.execCommand('insertHTML', false, '<hr>');
                return;
            case 'clear':
                document.execCommand('removeFormat');
                return;
        }
    }

    window.closeTaskModal = function() {
        document.getElementById('task-modal').style.display = 'none';
        taskModalMode = null;
        taskModalId = null;
        taskModalPendingFiles = [];
        taskModalAttachmentPaths = [];
        taskModalSourceEmailId = '';
    };

    window.submitTaskModal = function() {
        var title = document.getElementById('tm-title').value.trim();
        var desc = document.getElementById('tm-desc').value;
        var priority = document.getElementById('tm-priority').value;
        var taskType = document.getElementById('tm-task-type').value;
        var tags = document.getElementById('tm-tags').value;
        var deps = document.getElementById('tm-deps').value.trim();

        if (!title && !desc.trim()) { showToast('Title or description required', true); return; }

        var submitBtn = document.getElementById('tm-submit-btn');
        var origLabel = submitBtn.textContent;
        submitBtn.disabled = true;
        submitBtn.textContent = (taskModalMode === 'edit') ? 'Saving...' : 'Creating...';

        function resetBtn() { submitBtn.disabled = false; submitBtn.textContent = origLabel; }

        if (taskModalMode === 'edit') {
            // Edit existing task
            var statusEl = document.getElementById('tm-status');
            var statusVal = statusEl.value;
            var statusOriginal = statusEl.dataset.original || '';
            var editBody = 'task_id=' + encodeURIComponent(taskModalId)
                    + '&title=' + encodeURIComponent(title)
                    + '&description=' + encodeURIComponent(desc)
                    + '&priority=' + priority
                    + '&task_type=' + taskType
                    + '&tags=' + encodeURIComponent(tags)
                    + '&depends_on=' + encodeURIComponent(deps);
            // Only include `status` when the operator actually changed the
            // dropdown. Otherwise the server would interpret a no-op submit
            // as a transition request and undo any /action/task/assign that
            // fired moments earlier (e.g. when the operator only changed the
            // worker, the assign flips status to ASSIGNED and we shouldn't
            // immediately revert that).
            if (statusVal !== statusOriginal) {
                editBody += '&status=' + encodeURIComponent(statusVal);
            }
            // Cross-project + advanced fields always submit; the server
            // accepts empty strings as "no value". The old visibility check
            // tracked an `<details>`-wrapped section that's now collapsed by
            // default, so we'd otherwise lose user-entered data on save.
            editBody += '&source_worker=' + encodeURIComponent(document.getElementById('tm-source-worker').value.trim());
            editBody += '&target_worker=' + encodeURIComponent(document.getElementById('tm-target-worker').value.trim());
            editBody += '&dependency_type=' + encodeURIComponent(document.getElementById('tm-dep-type').value);
            editBody += '&acceptance_criteria=' + encodeURIComponent(document.getElementById('tm-acceptance').value);
            editBody += '&context_refs=' + encodeURIComponent(document.getElementById('tm-context-refs').value);
            // If the operator changed the top-level "Assign to" worker,
            // fire /action/task/assign first so the daemon transitions the
            // task through the proper assign path (auto-dispatch, etc.).
            // The edit POST that follows just persists field changes.
            var editWorkerEl = document.getElementById('tm-worker');
            var editWorkerNew = editWorkerEl ? editWorkerEl.value.trim() : '';
            var origWorker = (editWorkerEl && editWorkerEl.dataset.original) || '';
            var assignPromise = Promise.resolve();
            if (editWorkerNew && editWorkerNew !== origWorker) {
                assignPromise = actionFetch('/action/task/assign', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: 'task_id=' + encodeURIComponent(taskModalId)
                        + '&worker=' + encodeURIComponent(editWorkerNew)
                        + '&auto_start=false',
                }).then(function(r) {
                    // Don't let a failed assign fall through to the edit
                    // and report "Task updated" — that's how a 409/4xx
                    // silently lost the assignment.
                    if (!r.ok) {
                        return r.json().catch(function() { return {}; }).then(function(d) {
                            throw new Error(d.error || ('assign failed (HTTP ' + r.status + ')'));
                        });
                    }
                    return r.json().catch(function() { return {}; });
                });
            }
            assignPromise
                .then(function() {
                    return actionFetch('/action/task/edit', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                        body: editBody
                    });
                })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.status === 'updated') {
                    showToast('Task updated');
                    closeTaskModal();
                    refreshTasks();
                } else {
                    showToast('Error: ' + (data.error || 'unknown'), true);
                    resetBtn();
                }
            })
            .catch(function(err) {
                showToast('Error: ' + (err && err.message ? err.message : err), true);
                resetBtn();
            });
        } else {
            // Create new task, then upload any pending files
            if (!title) showToast('Generating title via AI...');
            var createStatus = document.getElementById('tm-status').value;
            var createWorker = (document.getElementById('tm-worker') || {value: ''}).value.trim();
            var createTargetWorker = document.getElementById('tm-target-worker').value.trim();
            var createBody = 'title=' + encodeURIComponent(title)
                    + '&description=' + encodeURIComponent(desc)
                    + '&priority=' + priority
                    + '&task_type=' + taskType
                    + '&depends_on=' + encodeURIComponent(deps)
                    + '&status=' + encodeURIComponent(createStatus);
            if (createWorker) {
                createBody += '&worker=' + encodeURIComponent(createWorker);
            }
            if (createTargetWorker) {
                createBody += '&target_worker=' + encodeURIComponent(createTargetWorker);
            }
            if (taskModalAttachmentPaths.length > 0) {
                createBody += '&attachments=' + encodeURIComponent(taskModalAttachmentPaths.join(','));
            }
            if (taskModalSourceEmailId) {
                createBody += '&source_email_id=' + encodeURIComponent(taskModalSourceEmailId);
            }
            actionFetch('/action/task/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: createBody
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.id) {
                    showToast('Task created: ' + data.title);
                    var origDesc = desc;
                    var blobToFinal = {};
                    // Files already uploaded at paste time (via /action/upload)
                    // are attached on create via taskModalAttachmentPaths and
                    // their blob URLs were already swapped in the textarea —
                    // skip those here. Only files queued without paste-time
                    // upload (e.g. drag-drop) need to upload now.
                    var uploads = taskModalPendingFiles
                        .filter(function(file) { return !file._uploadedPath; })
                        .map(function(file) {
                            var fd = new FormData();
                            fd.append('task_id', data.id);
                            fd.append('file', file);
                            return actionFetch('/action/task/upload', { method: 'POST', body: fd })
                                .then(function(r) { return r.ok ? r.json() : null; })
                                .then(function(ud) {
                                    if (ud && ud.path && file._blobUrl) {
                                        var basename = ud.path.split('/').pop();
                                        blobToFinal[file._blobUrl] = '/uploads/' + encodeURIComponent(basename);
                                    }
                                    return ud;
                                });
                        });
                    Promise.all(uploads).then(function() {
                        var blobUrls = Object.keys(blobToFinal);
                        if (blobUrls.length === 0) return null;
                        // Rewrite the description on the server so the blob:
                        // URLs become permanent /uploads/ paths. Workers will
                        // never see the dead blob URLs.
                        var newDesc = origDesc;
                        blobUrls.forEach(function(b) {
                            newDesc = newDesc.split(b).join(blobToFinal[b]);
                            try { URL.revokeObjectURL(b); } catch (e) {}
                        });
                        if (newDesc === origDesc) return null;
                        return actionFetch('/action/task/edit', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                            body: 'task_id=' + encodeURIComponent(data.id) + '&description=' + encodeURIComponent(newDesc),
                        });
                    }).then(function() {
                        if (uploads.length > 0) showToast(uploads.length + ' attachment(s) uploaded');
                        refreshTasks();
                    });
                    closeTaskModal();
                } else {
                    showToast('Error: ' + (data.error || 'unknown'), true);
                    resetBtn();
                }
            })
            .catch(function(err) { showToast('Error: ' + err, true); resetBtn(); });
        }
    };

    window.assignTask = function(taskId, taskTitle) {
        if (!selectedWorker) {
            showToast('Select a worker first', true);
            return;
        }
        postAction(
            '/action/task/assign',
            'task_id=' + encodeURIComponent(taskId) + '&worker=' + encodeURIComponent(selectedWorker),
            function(data) {
                if (data.status === 'started') {
                    showToast('Assigned & started "' + taskTitle + '" on ' + selectedWorker);
                } else if (data.status === 'assigned') {
                    showToast('Assigned "' + taskTitle + '" to ' + selectedWorker + ' (queued)');
                }
                refreshTasks();
            }
        );
    }

    window.startTask = function(taskId) {
        taskAction('start', taskId, 'started', 'Task sent to worker');
    }

    window.completeTask = function(taskId) {
        taskAction('complete', taskId, 'done', 'Task completed');
    }

    window.removeTask = function(taskId) {
        showConfirm('Remove this task?', function() {
            taskAction('remove', taskId, 'removed', 'Task removed');
        });
    }

    window.failTask = function(taskId) {
        taskAction('fail', taskId, 'failed', 'Task failed');
    }

    window.unassignTask = function(taskId) {
        taskAction('unassign', taskId, 'unassigned', 'Task unassigned');
    }

    window.reopenTask = function(taskId) {
        taskAction('reopen', taskId, 'reopened', 'Task reopened');
    }

    window.promoteTask = function(taskId) {
        taskAction('promote', taskId, 'unassigned', 'Handed to Queen');
    }

    window.approveTask = function(taskId) {
        fetch('/api/tasks/' + encodeURIComponent(taskId) + '/approve', {method: 'POST', headers: {'Content-Type': 'application/json'}})
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.status === 'approved') {
                    showToast('Task approved');
                    refreshTasks();
                }
            });
    }

    window.rejectTask = function(taskId) {
        fetch('/api/tasks/' + encodeURIComponent(taskId) + '/reject', {method: 'POST', headers: {'Content-Type': 'application/json'}})
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.status === 'rejected') {
                    showToast('Task rejected');
                    refreshTasks();
                }
            });
    }

    window.retryDraft = function(taskId) {
        showToast('Retrying draft reply...');
        postAction(
            '/action/task/retry-draft',
            'task_id=' + encodeURIComponent(taskId),
            function() {}
        );
    }

    // --- Broadcast ---
    window.showBroadcast = function() {
        document.getElementById('broadcast-modal').style.display = 'flex';
        document.getElementById('broadcast-input').focus();
    }

    window.hideBroadcast = function() {
        document.getElementById('broadcast-modal').style.display = 'none';
        document.getElementById('broadcast-input').value = '';
    }

    window.sendBroadcast = function() {
        const msg = document.getElementById('broadcast-input').value.trim();
        if (!msg) return;
        const target = document.getElementById('broadcast-target').value;

        if (target.startsWith('group:')) {
            const group = target.substring(6);
            actionFetch('/action/send-group', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: 'message=' + encodeURIComponent(msg) + '&group=' + encodeURIComponent(group)
            })
            .then(r => r.json())
            .then(data => {
                showToast('Sent to ' + data.count + ' worker(s) in ' + group);
                hideBroadcast();
            });
        } else {
            actionFetch('/action/send-all', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: 'message=' + encodeURIComponent(msg)
            })
            .then(r => r.json())
            .then(data => {
                showToast('Sent to ' + data.count + ' worker(s)');
                hideBroadcast();
            });
        }
    }

    // --- Queen modal helpers ---
    // (Ask Queen / applyDirectives removed in task #253 — operator reaches the
    //  interactive Queen via the Queen worker tile. Proposal display paths
    //  below still use queen-modal / queen-result for escalations/completions.)

    window.hideQueen = function() {
        clearTimeout(queenAutoHideTimer);
        document.getElementById('queen-modal').style.display = 'none';
    }

    function queenSummaryLine(type, data) {
        var w = escapeHtml(data.worker || data.worker_name || '?');
        if (type === 'completion') {
            var t = escapeHtml(data.task_title || 'task');
            return 'Mark \u201c' + t + '\u201d complete for <strong class="text-lavender">' + w + '</strong>';
        }
        if (type === 'assignment') {
            var t = escapeHtml(data.task_title || 'task');
            return 'Assign \u201c' + t + '\u201d to <strong class="text-lavender">' + w + '</strong>';
        }
        var a = data.action || 'wait';
        var labels = {
            send_message: 'Send command to <strong class="text-lavender">' + w + '</strong>',
            continue: 'Continue execution for <strong class="text-lavender">' + w + '</strong>',
            restart: 'Restart <strong class="text-lavender">' + w + '</strong>',
            complete_task: 'Complete task for <strong class="text-lavender">' + w + '</strong>',
            wait: 'No action needed for <strong class="text-lavender">' + w + '</strong>'
        };
        return labels[a] || escapeHtml(a) + ' — <strong class="text-lavender">' + w + '</strong>';
    }

    window.showQueenCompletion = function(data) {
        var modal = document.getElementById('queen-modal');
        var result = document.getElementById('queen-result');
        var confPct = Math.round((data.confidence || 0) * 100);
        var confClass = confPct >= 70 ? 'conf-high' : confPct >= 40 ? 'conf-mid' : 'conf-low';
        var html = '<div class="queen-card queen-card-complete">';
        html += '<div class="queen-card-header">';
        html += '<span class="conf-badge conf-high"><img src="/static/bees/honey-jar.svg" class="bee-icon bee-xs" alt="" style="margin-right:0.2rem">TASK COMPLETE</span>';
        html += '<span class="conf-badge ' + confClass + '">Confidence: ' + confPct + '%</span>';
        html += '</div>';
        html += '<div class="queen-summary">' + queenSummaryLine('completion', data) + '</div>';
        var compParts = [];
        if (data.assessment) compParts.push(escapeHtml(data.assessment));
        if (data.reasoning && data.reasoning !== data.assessment) compParts.push(escapeHtml(data.reasoning));
        if (compParts.length) {
            html += '<div class="resolution-block queen-text-block"><strong class="text-leaf text-base">Resolution</strong><br><span class="ws-pre-wrap">' + compParts.join(' ') + '</span></div>';
        }
        // Draft Response checkbox — only shown for email-sourced tasks
        var cbId = 'completion-draft-response';
        if (data.has_source_email) {
            html += '<label class="flex-center gap-xs text-base text-beeswax draft-label">';
            html += '<input type="checkbox" id="' + cbId + '" checked class="checkbox-leaf">';
            html += 'Draft Response &mdash; reply-all to source email</label>';
        }
        html += '</div>';
        if (data.proposal_id) {
            var pid = escapeHtml(data.proposal_id);
            html += '<div class="modal-footer">';
            html += '<button class="btn btn-approve" data-approve-proposal="' + pid + '" data-draft-email="checkbox" data-also-hide-queen="1">Approve &amp; Complete</button>';
            html += '<button class="btn btn-reject-ghost" data-reject-proposal="' + pid + '" data-also-hide-queen="1">Dismiss</button>';
            html += '</div>';
        }
        result.innerHTML = html;
        modal.style.display = 'flex';
        clearTimeout(queenAutoHideTimer);
        if (isTestMode) {
            queenAutoHideTimer = setTimeout(hideQueen, 4000);
        }
    };

    window.showQueenEscalation = function(data) {
        var modal = document.getElementById('queen-modal');
        var result = document.getElementById('queen-result');
        var confPct = Math.round((data.confidence || 0) * 100);
        var confClass = confPct >= 70 ? 'conf-high' : confPct >= 40 ? 'conf-mid' : 'conf-low';
        var actionLabels = {send_message: 'Send message', continue: 'Continue execution', restart: 'Restart worker', complete_task: 'Complete task', wait: 'Wait'};
        var html = '<div class="queen-card queen-card-escalation">';
        html += '<div class="queen-card-header">';
        html += '<span class="conf-badge conf-mid"><img src="/static/bees/surprised.svg" class="bee-icon bee-xs" alt="" style="margin-right:0.2rem">ESCALATION</span>';
        html += '<span class="conf-badge ' + confClass + '">Confidence: ' + confPct + '%</span>';
        html += '</div>';
        html += '<div class="queen-summary">' + queenSummaryLine('escalation', data) + '</div>';
        var analysisParts = [];
        if (data.assessment) analysisParts.push(escapeHtml(data.assessment));
        if (data.reasoning && data.reasoning !== data.assessment) analysisParts.push(escapeHtml(data.reasoning));
        if (analysisParts.length) {
            html += '<div class="mb-sm queen-text-block"><strong class="text-honey">Analysis</strong><br>' + analysisParts.join(' ') + '</div>';
        }
        if (data.action && data.action !== 'wait') {
            var label = actionLabels[data.action] || data.action;
            html += '<div class="queen-recommends-callout"><img src="/static/bees/queen.svg" class="bee-icon bee-sm" alt=""><span><strong class="text-honey">Queen recommends</strong>: <span class="text-lavender">' + escapeHtml(label) + '</span></span></div>';
        }
        if (data.message) {
            html += '<div class="queen-code-block mb-sm">' + escapeHtml(data.message) + '</div>';
        }
        html += '</div>';
        if (data.proposal_id) {
            html += '<div class="modal-footer">';
            html += '<button class="btn btn-approve" data-approve-proposal="' + escapeHtml(data.proposal_id) + '" data-also-hide-queen="1">Approve</button>';
            if (!data.is_plan) html += '<button class="btn btn-secondary" data-approve-always="' + escapeHtml(data.proposal_id) + '" data-also-hide-queen="1">Approve Always</button>';
            html += '<button class="btn btn-reject-ghost" data-reject-proposal="' + escapeHtml(data.proposal_id) + '" data-also-hide-queen="1">Dismiss</button>';
            html += '</div>';
        }
        result.innerHTML = html;
        modal.style.display = 'flex';
        clearTimeout(queenAutoHideTimer);
        if (isTestMode) {
            queenAutoHideTimer = setTimeout(hideQueen, 4000);
        }
    };

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // --- Queen notification banners (non-blocking) ---
    var _bannerCount = 0;
    var _MAX_BANNERS = 5;

    window.showQueenBanner = function(type, data) {
        // Suppress banner if user is currently viewing this worker
        if (data.worker && data.worker === selectedWorker) return;

        var container = document.getElementById('queen-notifications');
        if (!container) return;

        // Dedup: skip if a banner for this worker already exists
        var workerKey = data.worker || '?';
        var existing = container.querySelectorAll('.queen-banner[data-worker]');
        for (var i = 0; i < existing.length; i++) {
            if (existing[i].dataset.worker === workerKey) return;
        }

        // Cap visible banners
        while (container.children.length >= _MAX_BANNERS) {
            container.removeChild(container.firstChild);
        }

        var isEsc = type === 'esc';
        var bannerId = 'queen-banner-' + (++_bannerCount);
        var pid = data.proposal_id ? escapeHtml(data.proposal_id) : '';
        var worker = escapeHtml(data.worker || '?');
        var confPct = Math.round((data.confidence || 0) * 100);
        var assessment = escapeHtml(data.assessment || data.reasoning || '');
        if (assessment.length > 150) assessment = assessment.substring(0, 150) + '\u2026';

        var banner = document.createElement('div');
        banner.className = 'queen-banner ' + (isEsc ? 'queen-banner-esc' : 'queen-banner-done');
        banner.id = bannerId;
        banner.dataset.worker = workerKey;
        if (pid) banner.dataset.proposalId = pid;

        var badgeClass = isEsc ? 'queen-banner-badge-esc' : 'queen-banner-badge-done';
        var badgeText = isEsc ? 'ESC' : 'DONE';

        var html = '<span class="queen-banner-badge ' + badgeClass + '">' + badgeText + '</span>';
        html += '<div class="queen-banner-body">';
        html += '<span class="queen-banner-worker">' + worker + '</span>';
        if (assessment) html += '<span class="queen-banner-assessment">' + assessment + '</span>';
        html += '</div>';
        html += '<div class="queen-banner-actions">';
        html += '<button class="btn btn-secondary" data-jump-worker="' + worker + '" data-banner-id="' + bannerId + '">Jump</button>';
        if (pid) {
            var draftAttr = (!isEsc && data.has_source_email) ? ' data-draft-email="checkbox"' : '';
            html += '<button class="btn btn-approve" data-approve-proposal="' + pid + '"' + draftAttr + ' data-remove-banner="' + bannerId + '">Approve</button>';
            if (isEsc && !data.is_plan) html += '<button class="btn btn-secondary" data-approve-always="' + pid + '" data-remove-banner="' + bannerId + '">Always</button>';
            html += '<button class="btn btn-secondary" data-reject-proposal="' + pid + '" data-remove-banner="' + bannerId + '">Dismiss</button>';
        }
        html += '</div>';

        banner.innerHTML = html;
        container.appendChild(banner);
    };

    window.jumpToBannerWorker = function(workerName, bannerId) {
        selectWorker(workerName);
        if (bannerId) removeQueenBanner(bannerId);
        // Jumping from banner can surface stale cached viewport state in xterm.
        // Mirror the manual Refresh behavior with a deterministic reconnect.
        setTimeout(function() {
            if (activeTermWorker !== workerName) return;
            var entry = termCache.get(workerName);
            if (!entry || !entry.ws || entry.ws.readyState !== WebSocket.OPEN) return;
            hardReconnectTermEntry(workerName);
        }, 150);
    };

    window.removeQueenBanner = function(bannerId) {
        var el = document.getElementById(bannerId);
        if (el) el.remove();
    };

    window.clearQueenBanners = function() {
        var container = document.getElementById('queen-notifications');
        if (container) container.innerHTML = '';
    };

    // Also clear banners for a specific proposal when it's resolved
    window.removeQueenBannerByProposal = function(proposalId) {
        var container = document.getElementById('queen-notifications');
        if (!container) return;
        var banner = container.querySelector('[data-proposal-id="' + proposalId + '"]');
        if (banner) banner.remove();
    };

    // Dismiss any banners tied to a worker — e.g. when the operator
    // navigates to that worker, they're addressing it directly.
    window.removeQueenBannersForWorker = function(name) {
        if (!name) return;
        var container = document.getElementById('queen-notifications');
        if (!container) return;
        var banners = container.querySelectorAll('.queen-banner[data-worker]');
        for (var i = 0; i < banners.length; i++) {
            if (banners[i].dataset.worker === name) banners[i].remove();
        }
    };

    // --- Operator terminal approval banner ---
    window.showApproveAlwaysBanner = function(data) {
        var container = document.getElementById('queen-notifications');
        if (!container) return;

        while (container.children.length >= _MAX_BANNERS) {
            container.removeChild(container.firstChild);
        }

        var bannerId = 'rule-banner-' + (++_bannerCount);
        var worker = escapeHtml(data.worker || '?');
        var summary = escapeHtml(data.summary || '');
        var pattern = data.pattern || '';
        var snippet = data.prompt_snippet || '';

        var banner = document.createElement('div');
        banner.className = 'queen-banner queen-banner-esc';
        banner.id = bannerId;
        banner.dataset.worker = data.worker || '?';
        // Store snippet and pattern on the element for the Custom Rule button
        banner.dataset.promptSnippet = snippet;
        banner.dataset.rulePattern = pattern;

        var html = '<span class="queen-banner-badge queen-banner-badge-esc">RULE?</span>';
        html += '<div class="queen-banner-body">';
        html += '<span class="queen-banner-worker">' + worker + '</span>';
        html += '<span class="queen-banner-assessment">Approved: ' + summary + '</span>';
        html += '</div>';
        html += '<div class="queen-banner-actions">';
        if (pattern) {
            html += '<button class="btn btn-approve" data-add-rule="' + escapeHtml(pattern) + '" data-remove-banner="' + bannerId + '">Approve Always</button>';
        }
        html += '<button class="btn btn-secondary" data-banner-custom-rule="' + bannerId + '" data-remove-banner="' + bannerId + '">Custom Rule</button>';
        html += '<button class="btn btn-secondary" data-remove-banner="' + bannerId + '">Dismiss</button>';
        html += '</div>';

        banner.innerHTML = html;
        container.appendChild(banner);

        // Auto-dismiss after 30s
        setTimeout(function() {
            var el = document.getElementById(bannerId);
            if (el) el.remove();
        }, 30000);
    };

    window.showAddRuleModal = function(pattern) {
        var modal = document.getElementById('queen-modal');
        var result = document.getElementById('queen-result');
        var html = '<div class="queen-card">';
        html += '<div class="queen-card-header"><span class="conf-badge conf-mid">ADD APPROVAL RULE</span></div>';
        html += '<div class="mb-sm"><strong class="text-honey">Pattern (regex)</strong></div>';
        html += '<div class="mb-sm"><input type="text" id="add-rule-pattern" class="input-field" value="' + escapeHtml(pattern || '') + '" style="width:100%;font-family:monospace" placeholder="e.g. \\baz\\b"></div>';
        html += '<div class="text-muted text-xs mb-sm">This regex will be matched against future tool prompts. Matching prompts will be auto-approved.</div>';
        html += '</div>';
        html += '<div class="modal-footer">';
        html += '<button class="btn btn-approve" id="add-rule-confirm">Save Rule</button>';
        html += '<button class="btn btn-secondary" onclick="hideQueen()">Cancel</button>';
        html += '</div>';
        result.innerHTML = html;
        modal.style.display = 'flex';
        var patternInput = document.getElementById('add-rule-pattern');
        patternInput.focus();
        patternInput.select();
        document.getElementById('add-rule-confirm').addEventListener('click', function() {
            var pat = document.getElementById('add-rule-pattern').value.trim();
            if (!pat) { showToast('Pattern cannot be empty', true); return; }
            var body = new FormData();
            body.append('pattern', pat);
            actionFetch('/action/add-approval-rule', { body: body })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) { showToast('Error: ' + d.error, true); return; }
                showToast('Approval rule added');
                hideQueen();
            })
            .catch(function() { showToast('Request failed', true); });
        });
    };

    // --- Queen modal auto-dismiss timer ---
    // Only auto-dismiss queen modals in test mode (so they don't block automated runs).
    var isTestMode = false;
    let queenAutoHideTimer = null;

    // --- Themed confirm dialog ---
    let confirmCallback = null;

    window.showConfirm = function(msg, onYes) {
        document.getElementById('confirm-msg').textContent = msg;
        confirmCallback = onYes;
        var yesBtn = document.getElementById('confirm-yes-btn');
        // Clone to remove old listeners
        var newBtn = yesBtn.cloneNode(true);
        yesBtn.parentNode.replaceChild(newBtn, yesBtn);
        newBtn.addEventListener('click', function() {
            var cb = confirmCallback;
            hideConfirm();
            if (cb) cb();
        });
        document.getElementById('confirm-modal').style.display = 'flex';
    };

    window.hideConfirm = function() {
        document.getElementById('confirm-modal').style.display = 'none';
        confirmCallback = null;
    };

    // --- Notification history (badge counter + server-fetched buzz) ---
    let unreadNotifications = 0;

    function addNotification(msg, warning) {
        // Increment badge unless buzz tab is active
        var activeTab = document.querySelector('.tab-content.active');
        if (!activeTab || activeTab.id !== 'tab-buzz') {
            unreadNotifications++;
            var badge = document.getElementById('notif-badge');
            if (badge) {
                badge.textContent = unreadNotifications > 99 ? '99+' : unreadNotifications;
                badge.style.display = 'inline-flex';
            }
            if (pageHidden) startTitleFlash(unreadNotifications);
        } else {
            refreshBuzzLog();
        }
    }
    // Phase D of the duplication sweep — expose the dashboard's
    // notification helper to the shared toast module so its badge
    // counter / title flash still fire when toasts are shown.  Other
    // pages don't define this; toast.js calls it conditionally.
    window.addNotification = addNotification;

    function startTitleFlash(count) {
        pendingTitleCount = count;
        if (titleFlashTimer) clearInterval(titleFlashTimer);
        var show = true;
        titleFlashTimer = setInterval(function() {
            document.title = show ? '(' + pendingTitleCount + ') Event \u2014 Bee Hive' : ORIGINAL_TITLE;
            show = !show;
        }, 1000);
    }

    function stopTitleFlash() {
        if (titleFlashTimer) {
            clearInterval(titleFlashTimer);
            titleFlashTimer = null;
        }
        pendingTitleCount = 0;
        document.title = ORIGINAL_TITLE;
    }

    function updateAppBadge(count) {
        if ('setAppBadge' in navigator) {
            if (count > 0) {
                navigator.setAppBadge(count).catch(function() {});
            } else {
                navigator.clearAppBadge().catch(function() {});
            }
        }
    }

    // --- Browser notifications ---
    function updateNotifButton() {
        var btn = document.getElementById('notif-perm-btn');
        if (!btn) return;
        if (!('Notification' in window)) {
            btn.style.display = 'none';
            return;
        }
        if (Notification.permission === 'granted') {
            btn.classList.remove('btn-secondary');
            btn.classList.add('btn-active');
            btn.title = 'Browser notifications enabled';
        } else if (Notification.permission === 'denied') {
            btn.style.opacity = '0.4';
            btn.title = 'Browser notifications blocked — update in browser settings';
        }
    }

    window.requestNotifPermission = function() {
        if (!('Notification' in window)) {
            showToast('Browser does not support notifications', true);
            return;
        }
        if (Notification.permission === 'granted') {
            showToast('Notifications already enabled');
            var testOpts = { body: 'Notifications are working.', icon: '/static/bees/png/happy.png', badge: '/static/icon-192.png' };
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.ready.then(function(reg) { reg.showNotification("Swarm's Bee Hive", testOpts); });
            } else {
                new Notification("Swarm's Bee Hive", testOpts);
            }
            return;
        }
        if (Notification.permission === 'denied') {
            showToast('Notifications blocked — allow in browser settings', true);
            return;
        }
        Notification.requestPermission().then(function(perm) {
            if (perm === 'granted') {
                showToast('Browser notifications enabled');
                // Show a test notification via SW if available
                var testOpts = { body: 'Notifications are now active.', icon: '/static/bees/png/happy.png', badge: '/static/icon-192.png' };
                if ('serviceWorker' in navigator) {
                    navigator.serviceWorker.ready.then(function(reg) { reg.showNotification("Swarm's Bee Hive", testOpts); });
                } else {
                    new Notification("Swarm's Bee Hive", testOpts);
                }
            } else {
                showToast('Notification permission denied', true);
            }
            updateNotifButton();
        });
    };

    var NOTIF_ICON_MAP = {
        'Worker Down': '/static/bees/png/sleeping.png',
        'Escalation': '/static/bees/png/surprised.png',
        'Task Failed': '/static/bees/png/angry.png',
        'Queen Proposal': '/static/bees/png/queen.png',
        'Queen Auto-Action': '/static/bees/png/queen.png',
        'Queen needs your input': '/static/bees/png/thinking.png',
        'Task complete': '/static/bees/png/honey-jar.png',
        'Draft Failed': '/static/bees/png/angry.png',
        'Task Send Failed': '/static/bees/png/angry.png',
    };

    function notifyBrowser(title, body, vibrate) {
        // Vibrate on Queen-attention events (works even when tab is visible)
        if (vibrate && 'vibrate' in navigator) {
            try { navigator.vibrate([200, 100, 200]); } catch(e) {}
        }
        if (!('Notification' in window)) return;
        if (Notification.permission !== 'granted') return;
        var opts = {
            body: body,
            icon: NOTIF_ICON_MAP[title] || '/static/bees/png/happy.png',
            badge: '/static/icon-192.png',
            tag: 'swarm-' + title.replace(/\s+/g, '-').toLowerCase(),
            vibrate: vibrate ? [200, 100, 200] : undefined
        };
        try {
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.ready.then(function(reg) {
                    reg.showNotification(title, opts);
                });
            } else {
                new Notification(title, opts);
            }
        } catch(e) {}
    }

    // --- Task filter switcher (persisted in localStorage) ---
    window.switchTaskFilter = function(filter) {
        if (filter === 'all') {
            activeTaskFilters.clear();
            // Also clear search when resetting to "All"
            activeSearchQuery = '';
            var searchEl = document.getElementById('task-search');
            if (searchEl) searchEl.value = '';
            try { localStorage.removeItem('swarm_task_search'); } catch(e) {}
        } else if (activeTaskFilters.has(filter)) {
            activeTaskFilters.delete(filter);
        } else {
            activeTaskFilters.add(filter);
        }
        try { localStorage.setItem('swarm_task_filter', Array.from(activeTaskFilters).join(',')); } catch(e) {}
        document.querySelectorAll('.filter-chip[data-filter]').forEach(function(c) {
            if (c.dataset.filter === 'all') c.classList.toggle('active', activeTaskFilters.size === 0);
            else c.classList.toggle('active', activeTaskFilters.has(c.dataset.filter));
        });
        refreshTasks();
    };

    window.switchPriorityFilter = function(priority) {
        if (priority === 'all') {
            activePriorityFilters.clear();
        } else if (activePriorityFilters.has(priority)) {
            activePriorityFilters.delete(priority);
        } else {
            activePriorityFilters.add(priority);
        }
        try { localStorage.setItem('swarm_priority_filter', Array.from(activePriorityFilters).join(',')); } catch(e) {}
        document.querySelectorAll('.priority-chip').forEach(function(c) {
            if (c.dataset.priority === 'all') c.classList.toggle('active', activePriorityFilters.size === 0);
            else c.classList.toggle('active', activePriorityFilters.has(c.dataset.priority));
        });
        refreshTasks();
    };

    // Verifier-drone filter chip (item 4 of the 10-repo bundle).
    // Client-side: hides task rows whose data-verification doesn't match
    // the active filter set. "all" clears the filter; "reopened,escalated"
    // shows only verifier-flagged work. Persisted in localStorage so the
    // operator's choice survives a Reload.
    window.switchVerifyFilter = function(filterCsv) {
        var statuses = (filterCsv === 'all' || !filterCsv) ? [] : filterCsv.split(',');
        try { localStorage.setItem('swarm_verify_filter', statuses.join(',')); } catch(e) {}
        document.querySelectorAll('.verify-chip').forEach(function(c) {
            c.classList.toggle('active', c.dataset.verifyFilter === filterCsv);
        });
        applyVerifyFilter(statuses);
    };

    function applyVerifyFilter(statuses) {
        document.querySelectorAll('.task-item[data-verification]').forEach(function(row) {
            if (statuses.length === 0 || statuses.indexOf(row.dataset.verification) !== -1) {
                row.removeAttribute('data-verification-hidden');
            } else {
                row.setAttribute('data-verification-hidden', '1');
            }
        });
    }

    // Restore saved filters on page load
    (function() {
        try {
            var savedFilter = localStorage.getItem('swarm_task_filter');
            var savedPriority = localStorage.getItem('swarm_priority_filter');
            if (savedFilter) savedFilter.split(',').forEach(function(f) { if (f) activeTaskFilters.add(f); });
            if (savedPriority) savedPriority.split(',').forEach(function(p) { if (p) activePriorityFilters.add(p); });
            // Update chip visuals
            document.querySelectorAll('.filter-chip[data-filter]').forEach(function(c) {
                if (c.dataset.filter === 'all') c.classList.toggle('active', activeTaskFilters.size === 0);
                else c.classList.toggle('active', activeTaskFilters.has(c.dataset.filter));
            });
            document.querySelectorAll('.priority-chip').forEach(function(c) {
                if (c.dataset.priority === 'all') c.classList.toggle('active', activePriorityFilters.size === 0);
                else c.classList.toggle('active', activePriorityFilters.has(c.dataset.priority));
            });
            if (activeTaskFilters.size || activePriorityFilters.size) refreshTasks();
        } catch(e) {}
    })();

    // --- Bee icon map for toasts & notifications ---
    var BEE = {
        happy: '/static/bees/happy.svg',
        angry: '/static/bees/angry.svg',
        surprised: '/static/bees/surprised.svg',
        thinking: '/static/bees/thinking.svg',
        queen: '/static/bees/queen.svg',
        sleeping: '/static/bees/sleeping.svg',
        honeyJar: '/static/bees/honey-jar.svg',
        typing: '/static/bees/typing.svg',
        flower: '/static/bees/flower.svg',
        zooming: '/static/bees/zooming.svg',
        delivering: '/static/bees/delivering.svg',
        cool: '/static/bees/cool.svg',
        flyingRight: '/static/bees/flying-right.svg',
        worker: '/static/bees/worker.svg',
    };

    // --- Update banner ---
    function showUpdateBanner(data) {
        var el = document.getElementById('update-banner');
        var txt = document.getElementById('update-banner-text');
        var msg = 'Update available: <strong>' + escapeHtml(data.current_version) + '</strong> → <strong>' + escapeHtml(data.remote_version) + '</strong>';
        if (data.commit_sha) msg += '  (' + escapeHtml(data.commit_sha) + ': ' + escapeHtml(data.commit_message || '') + ')';
        txt.innerHTML = msg;
        el.style.display = 'block';
    }
    window.hideUpdateBanner = function() {
        document.getElementById('update-banner').style.display = 'none';
    };
    window.installUpdate = function() {
        showConfirm('Install update and restart swarm? Workers will keep running.', function() {
        showUpdateProgress('Installing update...');
        actionFetch('/action/update-and-restart', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.restarting) {
                    showToast('Update installed — restarting server...');
                    hideUpdateBanner();
                    waitForRestart();
                } else {
                    showToast('Update failed: ' + (data.output || 'unknown error').substring(0, 200), true);
                    resetUpdateBanner();
                }
            })
            .catch(function() {
                // Server may have already shut down — start polling
                waitForRestart();
            });
        });
    };
    function showUpdateProgress(line) {
        var el = document.getElementById('update-banner');
        var txt = document.getElementById('update-banner-text');
        el.style.display = 'block';
        txt.innerHTML = '<span class="update-spinner"></span> ' + escapeHtml(line);
    }
    function resetUpdateBanner() {
        var txt = document.getElementById('update-banner-text');
        if (txt) txt.innerHTML = '';
        document.getElementById('update-banner').style.display = 'none';
    }
    function waitForRestart(preSha) {
        // Two-phase restart detection:
        //   Phase 1: poll until server goes DOWN (connection refused / non-200)
        //   Phase 2: poll until server comes BACK UP, then compare build_sha
        // Initial delay lets os.execv actually tear down the old process
        _restarting = true;
        console.warn('[swarm-restart] waitForRestart begin', { preSha: preSha || null });
        if (_restartRecoveryTimer) clearTimeout(_restartRecoveryTimer);
        _restartRecoveryTimer = setTimeout(function() {
            if (!_restarting) return;
            console.warn('[swarm-restart] watchdog fired; recovering live connections');
            _restarting = false;
            ensureMainWsConnected();
            if (activeTermWorker) {
                var stalledEntry = termCache.get(activeTermWorker);
                if (stalledEntry && (!stalledEntry.ws || stalledEntry.ws.readyState !== WebSocket.OPEN)) {
                    console.warn('[swarm-restart] reconnecting active terminal from watchdog', {
                        worker: activeTermWorker,
                        wsState: stalledEntry.ws ? stalledEntry.ws.readyState : 'none'
                    });
                    stalledEntry.reconnectAttempts = 0;
                    connectTermEntryWs(activeTermWorker, stalledEntry);
                }
            }
            showToast('Recovered live connections after restart stall', true);
        }, 6000);
        var phase = 1;
        var attempts = 0;
        var maxDown = 8;     // ~4s — fast restart may never appear "down"
        var maxUp = 60;
        setTimeout(function() {
            var interval = setInterval(function() {
                attempts++;
                console.warn('[swarm-restart] poll', { phase: phase, attempts: attempts });
                fetchJsonNoStore('/api/health?_=' + Date.now(), 800)
                    .then(function(data) {
                        if (phase === 1) {
                            if (attempts >= maxDown) {
                                // Restart was instant — fall through to phase 2
                                console.warn('[swarm-restart] server never appeared down; switching to phase 2');
                                phase = 2;
                                attempts = 0;
                            }
                            return;
                        }
                        // Phase 2: server is back up
                        console.warn('[swarm-restart] server healthy after restart', {
                            buildSha: data.build_sha || null,
                            phase: phase,
                            attempts: attempts
                        });
                        clearInterval(interval);
                        var msg = null;
                        var warn = false;
                        if (preSha && data.build_sha) {
                            if (data.build_sha !== preSha) {
                                msg = 'Reloaded: ' + preSha + ' \u2192 ' + data.build_sha;
                            } else {
                                msg = 'Build fingerprint unchanged (' + preSha + ')';
                                warn = true;
                            }
                        } else {
                            msg = 'Server restarted';
                        }
                        try {
                            sessionStorage.setItem('reload_toast', msg);
                            if (warn) sessionStorage.setItem('reload_toast_warn', '1');
                        } catch(e) {}
                        // Pre-fetch full page before reloading to avoid SW
                        // navigate race timeout → offline.html → double reload
                        (function prefetchThenReload() {
                            fetch('/?_=' + Date.now(), { cache: 'no-store' }).then(function(r) {
                                if (!r.ok) throw new Error('not ready');
                                // Tell SW to skip race timeout for this reload
                                if (navigator.serviceWorker && navigator.serviceWorker.controller) {
                                    navigator.serviceWorker.controller.postMessage({ type: 'skip-race' });
                                }
                                console.warn('[swarm-restart] prefetch succeeded; reloading page');
                                // Delay reload to let SW process skip-race message,
                                // and close all WS connections cleanly first
                                setTimeout(function() {
                                    if (_restartRecoveryTimer) {
                                        clearTimeout(_restartRecoveryTimer);
                                        _restartRecoveryTimer = null;
                                    }
                                    // Close all terminal WS connections so server-side
                                    // handlers unsubscribe immediately (clean close frame)
                                    termCache.forEach(function(entry) {
                                        if (entry.reconnectTimer) { clearTimeout(entry.reconnectTimer); entry.reconnectTimer = null; }
                                        if (entry.inputReadyTimer) { clearTimeout(entry.inputReadyTimer); entry.inputReadyTimer = null; }
                                        if (entry._staleWatchdog) { clearInterval(entry._staleWatchdog); entry._staleWatchdog = null; }
                                        if (entry.ws) { try { entry.ws.close(); } catch(e2) {} }
                                    });
                                    // Close main dashboard WS
                                    if (ws) { try { ws.close(); } catch(e2) {} ws = null; }
                                    location.reload();
                                }, 50);
                            }).catch(function() {
                                console.warn('[swarm-restart] prefetch failed; retrying before reload');
                                setTimeout(prefetchThenReload, 500);
                            });
                        })();
                    })
                    .catch(function() {
                        if (phase === 1) {
                            // Server is down — move to phase 2
                            console.warn('[swarm-restart] server went down; waiting for it to come back');
                            phase = 2;
                            attempts = 0;
                            return;
                        }
                        if (attempts >= maxUp) {
                            clearInterval(interval);
                            showToast('Server did not come back — check terminal', true);
                        }
                    });
            }, 500);
        }, 1500);
    }

    // --- Dev reload (reinstall from local source + restart) ---
    window.devReload = function() {
        var btn = document.getElementById('footer-reload-btn');
        var status = document.getElementById('footer-reload-status');
        btn.disabled = true;
        status.textContent = 'Reloading...';
        status.style.color = 'var(--honey)';
        showToast('Reinstalling and restarting...');
        fetch('/api/health', { method: 'GET' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var preSha = data.build_sha || '';
                actionFetch('/api/server/restart', { method: 'POST' }).catch(function() {});
                waitForRestart(preSha);
            })
            .catch(function() {
                actionFetch('/api/server/restart', { method: 'POST' }).catch(function() {});
                waitForRestart();
            });
    };

    // --- MCP tools schema drift indicator ---
    // Poll /api/health so the Reload button flags the operator when
    // ``src/swarm/mcp/tools.py`` has been edited since daemon start. Without
    // this the worker's ``tools/list`` keeps returning the stale schema
    // (regression scenario: task #169 fix sat unapplied because nobody
    // reloaded, so `swarm_complete_task` kept running the old logic).
    function updateSchemaDriftIndicator(drift) {
        var btn = document.getElementById('footer-reload-btn');
        var status = document.getElementById('footer-reload-status');
        if (!btn || !status) return;
        if (drift) {
            btn.style.color = 'var(--honey)';
            btn.style.borderColor = 'var(--honey)';
            btn.title = 'MCP tool schema drifted — reload to publish the new tools list';
            if (!status.textContent || status.textContent === 'Up to date') {
                status.textContent = 'Reload needed (MCP tools edited)';
                status.style.color = 'var(--honey)';
            }
        } else {
            btn.style.color = '';
            btn.style.borderColor = '';
            btn.title = 'Reinstall from local source and restart';
            if (status.textContent === 'Reload needed (MCP tools edited)') {
                status.textContent = '';
            }
        }
    }

    // Holder-drift banner: the PTY holder is a persistent sidecar, so
    // daemon reloads (os.execv) can't refresh its bytecode. When
    // holder.py on disk has moved past the holder's import-time hash,
    // ``/api/health`` reports drift and we need to tell the operator to
    // kill + respawn the holder — the regular Reload button WON'T fix
    // it. This is the long-standing "terminal locks after reload" root
    // cause; see commit 0df45be for the original buffer-threshold fix
    // that sat unapplied because no one bounced the holder.
    var _holderDriftCmd = '';
    function updateHolderDriftIndicator(drift) {
        var banner = document.getElementById('holder-drift-banner');
        if (!banner) return;
        if (!drift || !drift.drift) {
            banner.style.display = 'none';
            _holderDriftCmd = '';
            return;
        }
        var pid = drift.holder_pid || '?';
        var pidEl = document.getElementById('holder-drift-pid');
        if (pidEl) pidEl.textContent = pid;
        _holderDriftCmd = 'kill ' + pid + ' && rm -f ~/.swarm/holder.sock ~/.swarm/holder.pid && systemctl --user restart swarm';
        var cmdEl = document.getElementById('holder-drift-cmd');
        if (cmdEl) cmdEl.textContent = _holderDriftCmd;
        banner.style.display = 'block';
    }
    window.copyHolderDriftCmd = function() {
        if (!_holderDriftCmd) return;
        try {
            navigator.clipboard.writeText(_holderDriftCmd)
                .then(function() { showToast('Holder-bounce command copied'); })
                .catch(function() { showToast('Copy failed — select the command and copy manually', true); });
        } catch (_) {
            showToast('Copy failed — select the command and copy manually', true);
        }
    };

    function bounceHolder() {
        showConfirm(
            'Bounce PTY holder? This kills all running workers (the daemon will respawn them) and restarts swarm. You may need to hard-refresh the browser/PWA once the daemon is back.',
            function() {
                // Use actionFetch (adds the X-Requested-With CSRF header)
                // — a bare fetch is rejected 403 by the server, which the
                // old swallow-all error handling hid entirely. No optimistic
                // toast: decide the message from the actual response.
                actionFetch('/api/holder/bounce', { method: 'POST' })
                    .then(function(r) {
                        if (r.ok) {
                            showToast('Holder bouncing — daemon restarting. If the dashboard does not reconnect within ~20s, hard-refresh.');
                            return;
                        }
                        if (r.status === 404) {
                            showToast('This daemon is too old to self-bounce (no /api/holder/bounce endpoint). Use the Copy button and run the command in a terminal — the button will work after the next update.', true);
                            return;
                        }
                        if (r.status === 401 || r.status === 403) {
                            showToast('Not authorized to bounce the holder (status ' + r.status + ').', true);
                            return;
                        }
                        return r.json().catch(function() { return {}; }).then(function(data) {
                            showToast('Bounce failed (HTTP ' + r.status + '): ' + ((data && data.error) || 'unknown error'), true);
                        });
                    })
                    .catch(function() {
                        // A network error AFTER a successful kick is expected
                        // (the daemon drops the connection mid-restart). But
                        // it's indistinguishable here from never-reached, so
                        // state the ambiguity honestly rather than claim success.
                        showToast('Connection dropped — expected if the bounce started. Watch for the dashboard to reconnect; hard-refresh if it does not return within ~30s.');
                    });
            }
        );
    }

    function pollSchemaDrift() {
        fetch('/api/health', { method: 'GET' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                updateSchemaDriftIndicator(!!data.mcp_schema_drift);
                updateHolderDriftIndicator(data.holder_drift);
            })
            .catch(function() { /* swallow — transient failures are fine */ });
    }

    // Holder drift is a prod concern (not dev-only), so poll
    // independently of the dev-only Reload button. Run once on load
    // then every 30s.
    pollSchemaDrift();
    setInterval(pollSchemaDrift, 30000);

    // --- Footer version check ---
    window.footerCheckForUpdate = function() {
        var btn = document.getElementById('footer-check-update-btn');
        var status = document.getElementById('footer-update-status');
        btn.disabled = true;
        status.textContent = 'Checking...';
        status.style.color = '';
        actionFetch('/action/check-update', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                btn.disabled = false;
                if (data.error) {
                    status.textContent = data.error;
                    status.style.color = 'var(--poppy)';
                } else if (data.available) {
                    if (data.is_dev) {
                        status.innerHTML = '<span style="color:var(--honey)">' + escapeHtml(data.remote_version) + ' available (dev — git pull)</span>';
                    } else {
                        status.innerHTML = '<span style="color:var(--leaf)">' + escapeHtml(data.remote_version) + ' available</span>';
                        btn.textContent = 'Update & Restart';
                        btn.onclick = function() { installUpdate(); };
                        btn.style.color = 'var(--leaf)';
                        btn.style.borderColor = 'var(--leaf)';
                    }
                } else {
                    status.textContent = 'Up to date';
                    status.style.color = 'var(--leaf)';
                }
            })
            .catch(function() {
                btn.disabled = false;
                status.textContent = 'Check failed';
                status.style.color = 'var(--poppy)';
            });
    };

    // --- Feedback (bug reports / feature requests / questions) ---
    var _feedbackState = {
        category: 'bug',
        attachments: [],  // [{key, label, content, redacted_count, enabled}]
        gh: { checked: false, installed: false, authenticated: false, account: '' }
    };

    window.sendFeedback = function() {
        var modal = document.getElementById('feedback-modal');
        if (!modal) return;
        document.getElementById('feedback-title').value = '';
        document.getElementById('feedback-description').value = '';
        document.getElementById('feedback-status').textContent = '';
        _feedbackState.category = 'bug';
        updateFeedbackCategoryButtons();
        updateFeedbackPlaceholder();
        modal.style.display = 'flex';
        loadFeedbackAttachments('bug');
        checkGhStatus();
    };

    function checkGhStatus() {
        fetch('/api/feedback/gh-status', { headers: { 'X-Requested-With': 'Dashboard' } })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                _feedbackState.gh = {
                    checked: true,
                    installed: !!data.installed,
                    authenticated: !!data.authenticated,
                    account: data.account || ''
                };
                updateFeedbackSubmitButton();
            })
            .catch(function() {
                _feedbackState.gh = { checked: true, installed: false, authenticated: false, account: '' };
                updateFeedbackSubmitButton();
            });
    }

    function updateFeedbackSubmitButton() {
        var btn = document.getElementById('feedback-submit-btn');
        var hint = document.getElementById('feedback-gh-hint');
        if (!btn || !hint) return;
        var gh = _feedbackState.gh;
        if (gh.authenticated) {
            btn.textContent = 'Submit as @' + (gh.account || 'you');
            btn.title = 'Create the issue directly via your gh CLI — full report, no size limit';
            hint.innerHTML = 'Submitting as <strong>@' + escapeHtml(gh.account || 'you') + '</strong> via your local gh CLI.';
            hint.style.color = 'var(--leaf)';
        } else if (gh.installed) {
            btn.textContent = 'Open in GitHub';
            btn.title = 'gh is installed but not authenticated. Run "gh auth login" to enable direct submission.';
            hint.innerHTML = '<code>gh</code> is installed but not authenticated — falling back to browser pre-fill (size-limited). Run <code>gh auth login</code> to enable direct submission.';
            hint.style.color = 'var(--honey)';
        } else {
            btn.textContent = 'Open in GitHub';
            btn.title = 'Opens a pre-filled GitHub issue in your browser';
            hint.innerHTML = '<code>gh</code> CLI not found — falling back to browser pre-fill (size-limited). Install gh and run <code>gh auth login</code> for full-size submissions.';
            hint.style.color = 'var(--muted)';
        }
    }

    window.hideFeedback = function() {
        var modal = document.getElementById('feedback-modal');
        if (modal) modal.style.display = 'none';
        // Reset to the form view so the next open doesn't land on the preview
        var form = document.getElementById('feedback-form-view');
        var preview = document.getElementById('feedback-preview-view');
        if (form) form.style.display = '';
        if (preview) preview.style.display = 'none';
    };

    window.setFeedbackCategory = function(category) {
        if (category !== 'bug' && category !== 'feature' && category !== 'question') return;
        _feedbackState.category = category;
        updateFeedbackCategoryButtons();
        updateFeedbackPlaceholder();
        loadFeedbackAttachments(category);
    };

    function updateFeedbackCategoryButtons() {
        var cats = ['bug', 'feature', 'question'];
        cats.forEach(function(c) {
            var btn = document.getElementById('feedback-cat-' + c);
            if (!btn) return;
            if (c === _feedbackState.category) {
                btn.classList.remove('btn-secondary');
            } else {
                btn.classList.add('btn-secondary');
            }
        });
    }

    function updateFeedbackPlaceholder() {
        var ta = document.getElementById('feedback-description');
        if (!ta) return;
        if (_feedbackState.category === 'bug') {
            ta.placeholder = 'What happened, what did you expect, and how can it be reproduced?';
        } else if (_feedbackState.category === 'feature') {
            ta.placeholder = 'Describe the feature and the problem it solves.';
        } else {
            ta.placeholder = 'What would you like to know?';
        }
    }

    function loadFeedbackAttachments(category) {
        var container = document.getElementById('feedback-attachments');
        if (!container) return;
        container.innerHTML = '<div class="text-muted">Collecting diagnostics...</div>';
        actionFetch('/api/feedback/preview', {
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ category: category })
        })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                _feedbackState.attachments = (data.attachments || []).map(function(a) {
                    return {
                        key: a.key,
                        label: a.label,
                        content: a.content || '',
                        redacted_count: a.redacted_count || 0,
                        enabled: !!a.enabled
                    };
                });
                renderFeedbackAttachments();
            })
            .catch(function() {
                container.innerHTML = '<div style="color:var(--poppy)">Failed to collect diagnostics.</div>';
            });
    }

    function renderFeedbackAttachments() {
        var container = document.getElementById('feedback-attachments');
        if (!container) return;
        if (!_feedbackState.attachments.length) {
            container.innerHTML = '<div class="text-muted">No attachments.</div>';
            return;
        }
        var html = '<div class="form-label">What will be attached</div>';
        _feedbackState.attachments.forEach(function(a, i) {
            var badge = a.redacted_count > 0
                ? ' <span style="color:var(--honey); font-size:11px;">(' + a.redacted_count + ' items redacted)</span>'
                : '';
            var checked = a.enabled ? 'checked' : '';
            html += '<details class="mb-sm" style="border:1px solid var(--border); border-radius:4px; padding:6px 8px;">';
            html += '<summary style="cursor:pointer; user-select:none;">';
            html += '<input type="checkbox" ' + checked + ' data-feedback-idx="' + i + '" style="margin-right:6px;" onclick="event.stopPropagation();">';
            html += escapeHtml(a.label) + badge;
            html += '</summary>';
            html += '<textarea data-feedback-content="' + i + '" rows="6" class="modal-textarea" style="font-family:monospace; font-size:11px; margin-top:6px;">' + escapeHtml(a.content) + '</textarea>';
            html += '</details>';
        });
        container.innerHTML = html;

        // Wire up toggles + edits
        container.querySelectorAll('input[type=checkbox][data-feedback-idx]').forEach(function(cb) {
            cb.addEventListener('change', function() {
                var idx = parseInt(cb.dataset.feedbackIdx, 10);
                if (_feedbackState.attachments[idx]) {
                    _feedbackState.attachments[idx].enabled = cb.checked;
                }
            });
        });
        container.querySelectorAll('textarea[data-feedback-content]').forEach(function(ta) {
            ta.addEventListener('input', function() {
                var idx = parseInt(ta.dataset.feedbackContent, 10);
                if (_feedbackState.attachments[idx]) {
                    _feedbackState.attachments[idx].content = ta.value;
                }
            });
        });
    }

    function collectFeedbackPayload() {
        return {
            title: (document.getElementById('feedback-title').value || '').trim(),
            description: document.getElementById('feedback-description').value || '',
            category: _feedbackState.category,
            attachments: _feedbackState.attachments.map(function(a) {
                return {
                    key: a.key,
                    label: a.label,
                    content: a.content,
                    enabled: a.enabled
                };
            })
        };
    }

    function buildFeedback() {
        var payload = collectFeedbackPayload();
        if (!payload.title) {
            showToast('Please enter a title.', true);
            return Promise.reject(new Error('no title'));
        }
        return actionFetch('/api/feedback/build-url', {
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(resp) {
                if (!resp.ok) {
                    showToast(resp.data.error || 'Failed to build report.', true);
                    throw new Error(resp.data.error || 'build failed');
                }
                // Fire-and-forget save
                actionFetch('/api/feedback/save', {
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }).catch(function() {});
                return resp.data;
            });
    }

    window.submitFeedback = function() {
        var payload = collectFeedbackPayload();
        if (!payload.title) {
            showToast('Please enter a title.', true);
            return;
        }
        var statusEl = document.getElementById('feedback-status');

        if (_feedbackState.gh.authenticated) {
            // --- gh path: show preview-and-confirm step first ---
            if (statusEl) {
                statusEl.innerHTML = '<span style="color:var(--muted)">Building preview...</span>';
            }
            actionFetch('/api/feedback/build-markdown', {
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
                .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
                .then(function(resp) {
                    if (statusEl) { statusEl.textContent = ''; }
                    if (!resp.ok) {
                        var err = (resp.data && resp.data.error) || 'Failed to build preview.';
                        showToast(err, true);
                        return;
                    }
                    showFeedbackPreview(payload.title, resp.data.markdown || '');
                })
                .catch(function() {
                    showToast('Failed to build preview.', true);
                });
            return;
        }

        // --- Fallback: browser URL pre-fill (size-limited) ---
        buildFeedback().then(function(data) {
            if (data.truncated && statusEl) {
                statusEl.innerHTML =
                    '<span style="color:var(--honey)">Report truncated to fit URL limit. Install/authenticate gh CLI for full-size submissions, or use Copy as Markdown.</span>';
            }
            window.open(data.url, '_blank', 'noopener');
            showToast('Opening GitHub in a new tab...');
        }).catch(function() {});
    };

    function showFeedbackPreview(title, markdown) {
        var form = document.getElementById('feedback-form-view');
        var preview = document.getElementById('feedback-preview-view');
        if (!form || !preview) return;
        document.getElementById('feedback-preview-title').value = title;
        document.getElementById('feedback-preview-body').value = markdown;
        document.getElementById('feedback-preview-status').textContent = '';
        var confirmBtn = document.getElementById('feedback-confirm-btn');
        if (confirmBtn) {
            confirmBtn.disabled = false;
            var account = _feedbackState.gh.account || 'you';
            confirmBtn.textContent = 'Confirm & Submit as @' + account;
        }
        form.style.display = 'none';
        preview.style.display = '';
    }

    window.backToFeedbackForm = function() {
        var form = document.getElementById('feedback-form-view');
        var preview = document.getElementById('feedback-preview-view');
        if (!form || !preview) return;
        preview.style.display = 'none';
        form.style.display = '';
    };

    window.confirmFeedbackSubmit = function() {
        var title = (document.getElementById('feedback-preview-title').value || '').trim();
        var body = document.getElementById('feedback-preview-body').value || '';
        var statusEl = document.getElementById('feedback-preview-status');
        var btn = document.getElementById('feedback-confirm-btn');
        if (!title) {
            showToast('Title is required.', true);
            return;
        }
        if (btn) { btn.disabled = true; }
        if (statusEl) {
            statusEl.innerHTML = '<span style="color:var(--muted)">Submitting via gh...</span>';
        }
        var payload = {
            title: title,
            description: '',  // body_override supersedes description
            category: _feedbackState.category,
            attachments: [],  // ignored when body_override is set
            body_override: body
        };
        actionFetch('/api/feedback/submit', {
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(resp) {
                if (btn) { btn.disabled = false; }
                if (!resp.ok) {
                    var err = (resp.data && resp.data.error) || 'Submission failed.';
                    if (statusEl) {
                        statusEl.innerHTML = '<span style="color:var(--poppy)">' + escapeHtml(err) + '</span>';
                    }
                    showToast('gh submission failed.', true);
                    return;
                }
                if (statusEl) {
                    statusEl.innerHTML = '<span style="color:var(--leaf)">Submitted!</span>';
                }
                showToast('Issue created. Opening it now...');
                window.open(resp.data.url, '_blank', 'noopener');
                hideFeedback();
                backToFeedbackForm();
            })
            .catch(function() {
                if (btn) { btn.disabled = false; }
                showToast('gh submission failed.', true);
            });
    };

    window.copyFeedbackPreviewMarkdown = function() {
        var text = document.getElementById('feedback-preview-body').value || '';
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function() {
                showToast('Copied report to clipboard.');
            }, function() {
                fallbackCopy(text);
            });
        } else {
            fallbackCopy(text);
        }
    };

    window.copyFeedbackMarkdown = function() {
        buildFeedback().then(function(data) {
            var text = data.markdown || '';
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(function() {
                    showToast('Copied full report to clipboard.');
                }, function() {
                    fallbackCopy(text);
                });
            } else {
                fallbackCopy(text);
            }
        }).catch(function() {});
    };

    function fallbackCopy(text) {
        try {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            showToast('Copied full report to clipboard.');
        } catch (e) {
            showToast('Could not copy to clipboard.', true);
        }
    }

    // Toast helper + ``_toastApplyResult`` live in static/toast.js
    // (Phase D of the duplication sweep).  Local alias for callers
    // that use the bare name; ``window.showToast`` is identical.
    var showToast = window.showToast;

    // --- Launch ---
    let launchConfig = null;

    window.showLaunch = function() {
        const modal = document.getElementById('launch-modal');
        const body = document.getElementById('launch-body');
        modal.style.display = 'flex';
        body.innerHTML = '<p class="text-muted">Loading config...</p>';

        fetch('/partials/launch-config')
            .then(r => r.json())
            .then(data => {
                launchConfig = data;
                let html = '';
                // Group preset buttons
                if (data.groups && data.groups.length > 0) {
                    html += '<div class="mb-md">';
                    html += '<label class="launch-label">Quick select group:</label><br>';
                    for (const g of data.groups) {
                        html += '<button class="btn btn-sm btn-secondary launch-group-btn launch-btn-gap" data-group="' + escapeHtml(g.name) + '">' + escapeHtml(g.name) + '</button>';
                    }
                    html += '</div>';
                }
                // Worker checkboxes — disable already-running workers
                html += '<div class="launch-scroll">';
                for (const w of data.workers) {
                    const running = w.running;
                    html += '<label class="launch-worker-label" style="cursor:' + (running ? 'default' : 'pointer') + ';color:' + (running ? 'var(--muted)' : 'var(--beeswax)') + ';">';
                    html += '<input type="checkbox" class="launch-worker-cb cb-spaced" value="' + escapeHtml(w.name) + '"' + (running ? ' disabled' : '') + '>';
                    html += escapeHtml(w.name);
                    if (running) {
                        html += ' <span class="worker-running-badge">(running)</span>';
                    }
                    html += ' <span class="worker-path-text">(' + escapeHtml(w.path) + ')</span>';
                    html += '</label>';
                }
                html += '</div>';
                body.innerHTML = html;
            })
            .catch(() => {
                body.innerHTML = '<p class="text-poppy">Failed to load config</p>';
            });
    }

    window.hideLaunch = function() {
        document.getElementById('launch-modal').style.display = 'none';
    }

    var _selectedLaunchGroup = null;

    function selectLaunchGroup(groupName) {
        if (!launchConfig) return;
        const group = launchConfig.groups.find(g => g.name === groupName);
        if (!group) return;
        _selectedLaunchGroup = group;
        const members = new Set(group.workers.map(n => n.toLowerCase()));
        document.querySelectorAll('.launch-worker-cb').forEach(cb => {
            cb.checked = members.has(cb.value.toLowerCase());
        });
    }

    window.launchSelected = function() {
        const checked = new Set();
        document.querySelectorAll('.launch-worker-cb:checked').forEach(cb => checked.add(cb.value));
        if (!checked.size) { showToast('No workers selected', true); return; }

        // If a group was selected and all its members are still checked, use group order
        var ordered;
        if (_selectedLaunchGroup) {
            var groupMembers = _selectedLaunchGroup.workers.filter(n => checked.has(n));
            if (groupMembers.length === checked.size) {
                ordered = groupMembers;
            } else {
                ordered = Array.from(checked);
            }
        } else {
            ordered = Array.from(checked);
        }
        _selectedLaunchGroup = null;
        doLaunch(ordered.join(','));
    }

    window.launchAll = function() {
        doLaunch('');
    }

    function doLaunch(workers) {
        // Replace modal content with launch progress
        var body = document.getElementById('launch-body');
        var footer = document.querySelector('#launch-modal .modal-footer');
        body.innerHTML = '<div style="text-align:center;padding:2rem 0"><div class="spinner" style="margin:0 auto 1rem"></div><p class="text-honey" id="launch-status">Launching workers...</p><p class="text-muted text-sm">This takes 2-3 seconds per worker</p></div>';
        if (footer) footer.style.display = 'none';

        actionFetch('/action/launch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: workers ? 'workers=' + encodeURIComponent(workers) : ''
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                showToast('Launch failed: ' + data.error, true);
            } else {
                showToast('Launched ' + data.count + ' worker(s)');
                refreshWorkers();
                refreshStatus();
            }
            hideLaunch();
            if (footer) footer.style.display = '';
        })
        .catch(() => { showToast('Launch request failed', true); hideLaunch(); if (footer) footer.style.display = ''; });
    }

    // --- Spawn Worker ---
    window.showSpawn = function() {
        const modal = document.getElementById('spawn-modal');
        modal.style.display = 'flex';
        document.getElementById('spawn-name').value = '';
        document.getElementById('spawn-path').value = '';
        document.getElementById('spawn-provider').value = '';
        document.getElementById('spawn-name').focus();

        // Load config paths as presets
        fetch('/partials/launch-config')
            .then(r => r.json())
            .then(data => {
                const presets = document.getElementById('spawn-presets');
                if (!data.workers || !data.workers.length) { presets.innerHTML = ''; return; }
                const paths = [...new Set(data.workers.map(w => w.path))];
                let html = '<label class="launch-label text-xs">Quick fill from config:</label><br>';
                for (const w of data.workers) {
                    if (!w.running) {
                        html += '<button type="button" class="btn btn-sm btn-secondary spawn-preset-btn spawn-btn-gap" data-name="' + escapeHtml(w.name) + '" data-path="' + escapeHtml(w.path) + '">' + escapeHtml(w.name) + '</button>';
                    }
                }
                presets.innerHTML = html;
            });
    }

    window.hideSpawn = function() {
        document.getElementById('spawn-modal').style.display = 'none';
    }

    window.doSpawn = function() {
        const name = document.getElementById('spawn-name').value.trim();
        const path = document.getElementById('spawn-path').value.trim();
        const provider = document.getElementById('spawn-provider').value;
        if (!name || !path) { showToast('Name and path are required', true); return; }

        var body = 'name=' + encodeURIComponent(name) + '&path=' + encodeURIComponent(path);
        if (provider) { body += '&provider=' + encodeURIComponent(provider); }
        actionFetch('/action/spawn', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                showToast('Spawn failed: ' + data.error, true);
            } else {
                showToast('Spawned ' + data.worker);
                hideSpawn();
                refreshWorkers();
                refreshStatus();
            }
        })
        .catch(() => showToast('Spawn request failed', true));
    }

    // --- Edit Worker ---
    window.showEditWorker = function(name, path) {
        document.getElementById('edit-worker-original').value = name;
        document.getElementById('edit-worker-name').value = name;
        document.getElementById('edit-worker-path').value = path;
        document.getElementById('edit-worker-modal').style.display = 'flex';
        document.getElementById('edit-worker-name').focus();
    }

    window.hideEditWorker = function() {
        document.getElementById('edit-worker-modal').style.display = 'none';
    }

    window.doEditWorker = function() {
        var original = document.getElementById('edit-worker-original').value;
        var name = document.getElementById('edit-worker-name').value.trim();
        var path = document.getElementById('edit-worker-path').value.trim();
        if (!name && !path) { showToast('Nothing to update', true); return; }

        var body = '';
        if (name && name !== original) {
            body += 'name=' + encodeURIComponent(name);
        }
        if (path) {
            if (body) body += '&';
            body += 'path=' + encodeURIComponent(path);
        }
        if (!body) { hideEditWorker(); return; }

        actionFetch('/action/update/' + encodeURIComponent(original), {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                showToast('Update failed: ' + data.error, true);
            } else {
                showToast('Updated worker');
                hideEditWorker();
                if (name && name !== original) {
                    selectedWorker = name;
                    sessionStorage.setItem('swarm_selected', name);
                }
                refreshWorkers();
                refreshDetail();
                refreshStatus();
            }
        })
        .catch(function() { showToast('Update request failed', true); });
    }

    // --- Shutdown dialog ---
    window.killSession = function() {
        document.getElementById('shutdown-modal').style.display = 'flex';
    }

    window.hideShutdown = function() {
        document.getElementById('shutdown-modal').style.display = 'none';
    }

    window.doRestartServer = function() {
        hideShutdown();
        showToast('Server restarting...');
        fetch('/api/health', { method: 'GET' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var preSha = data.build_sha || '';
                actionFetch('/api/server/restart', { method: 'POST' }).catch(function() {});
                waitForRestart(preSha);
            })
            .catch(function() {
                actionFetch('/api/server/restart', { method: 'POST' }).catch(function() {});
                waitForRestart();
            });
    }

    function tryCloseWindow() {
        // PWA standalone windows can be closed; regular tabs cannot
        try { window.close(); } catch(e) {}
        // If window.close() didn't work (regular tab), show offline page
        setTimeout(function() { location.replace('/offline.html'); }, 500);
    }

    window.doStopServer = function() {
        hideShutdown();
        showToast('Web server stopping...');
        actionFetch('/action/stop-server', { method: 'POST' }).catch(function() {});
        setTimeout(tryCloseWindow, 300);
    }

    window.doKillEverything = function() {
        detachInlineTerminal();
        hideShutdown();
        showToast('Killing everything...');
        var fd = new FormData();
        fd.append('all', '1');
        actionFetch('/action/kill-session', { method: 'POST', body: fd }).catch(function() {});
        setTimeout(function() {
            actionFetch('/action/stop-server', { method: 'POST' }).catch(function() {});
            setTimeout(tryCloseWindow, 300);
        }, 500);
    }

    // --- Drag-and-drop attachments (unified modal) ---
    ;(function() {
        var dropzone = document.getElementById('tm-dropzone');
        var fileInput = document.getElementById('tm-file');

        if (!dropzone || !fileInput) return;

        dropzone.addEventListener('click', function() { fileInput.click(); });

        dropzone.addEventListener('dragenter', function(e) { e.preventDefault(); dropzone.style.borderColor = 'var(--honey)'; });
        dropzone.addEventListener('dragover', function(e) { e.preventDefault(); });
        dropzone.addEventListener('dragleave', function() { dropzone.style.borderColor = 'var(--border)'; });
        dropzone.addEventListener('drop', function(e) {
            e.preventDefault();
            e.stopPropagation();
            dropzone.style.borderColor = 'var(--border)';
            for (var i = 0; i < e.dataTransfer.files.length; i++) {
                handleTaskFile(e.dataTransfer.files[i]);
            }
        });

        fileInput.addEventListener('change', function() {
            for (var i = 0; i < fileInput.files.length; i++) {
                handleTaskFile(fileInput.files[i]);
            }
            fileInput.value = '';
        });
    })();

    function handleTaskFile(file) {
        if (taskModalMode === 'edit' && taskModalId) {
            // Upload immediately for existing tasks
            var fd = new FormData();
            fd.append('task_id', taskModalId);
            fd.append('file', file);
            actionFetch('/action/task/upload', { method: 'POST', body: fd })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.status === 'uploaded') {
                        showToast('Attachment uploaded');
                        addThumbnail(data.path);
                        rewriteRelativeImageRefs(data.path);
                        refreshTasks();
                    } else {
                        showToast('Upload failed: ' + (data.error || 'unknown'), true);
                    }
                })
                .catch(function() { showToast('Upload failed', true); });
        } else {
            // Create flow: queue locally, kick off background upload so the
            // file gets a permanent /uploads/ path. backgroundUploadAndSwap
            // also runs rewriteRelativeImageRefs to stitch up any pasted
            // markdown image references (e.g. ![](media/foo.png)).
            taskModalPendingFiles.push(file);
            addLocalThumbnail(file);
            backgroundUploadAndSwap(file, null);
        }
    }

    function isImageFile(nameOrType) {
        return /^image\//i.test(nameOrType) || /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i.test(nameOrType);
    }

    // Server prepends a 12-char hex digest + '_' to uploaded filenames
    // for content-addressing. Strip it for display so users see the
    // original filename ("Change_Requests.docx", not "593538aeb0c2_Change_Requests.docx").
    function _displayAttachmentName(basename) {
        return basename.replace(/^[0-9a-f]{12}_/, '');
    }

    function addThumbnail(path) {
        var container = document.getElementById('tm-attachments');
        var basename = path.split('/').pop();
        var url = '/uploads/' + encodeURIComponent(basename);
        if (isImageFile(basename)) {
            var link = document.createElement('a');
            link.href = url;
            link.target = '_blank';
            link.rel = 'noopener';
            var img = document.createElement('img');
            img.src = url;
            img.alt = _displayAttachmentName(basename);
            img.className = 'task-attachment-img';
            link.appendChild(img);
            container.appendChild(link);
        } else {
            var chip = document.createElement('a');
            chip.href = url;
            chip.target = '_blank';
            chip.rel = 'noopener';
            chip.textContent = _displayAttachmentName(basename);
            chip.title = basename;
            chip.className = 'task-attachment-file';
            container.appendChild(chip);
        }
    }

    function addLocalThumbnail(file) {
        var container = document.getElementById('tm-attachments');
        if (isImageFile(file.type || file.name)) {
            var img = document.createElement('img');
            img.src = URL.createObjectURL(file);
            img.alt = file.name;
            img.className = 'task-attachment-img';
            img.onload = function() { URL.revokeObjectURL(img.src); };
            container.appendChild(img);
        } else {
            var chip = document.createElement('span');
            chip.textContent = file.name;
            chip.title = file.name;
            chip.className = 'task-attachment-file';
            container.appendChild(chip);
        }
    }

    // --- Paste handler: email content + images ---
    // Prevent browser from navigating to dropped files anywhere on the page.
    // Specific drop zones (terminal, task tab, dropzone) call stopPropagation
    // so their handlers still fire normally.
    document.addEventListener('dragover', function(e) { e.preventDefault(); });
    document.addEventListener('drop', function(e) {
        e.preventDefault();
        // If an image was dropped outside a valid drop zone, try to upload it
        // to the active terminal as a fallback.
        if (e.dataTransfer && e.dataTransfer.files.length > 0) {
            var file = e.dataTransfer.files[0];
            if (file.type && file.type.indexOf('image') !== -1 && typeof uploadAndPaste === 'function') {
                uploadAndPaste(file);
            }
        }
    });

    document.addEventListener('paste', function(e) {
        var cd = e.clipboardData;
        if (!cd) return;
        var modalOpen = document.getElementById('task-modal').style.display !== 'none';
        var html = cd.getData('text/html');
        var focused = document.activeElement;

        // Determine target. Rich paste fires when the active field is the
        // task description, OR when no modal field is focused (so a paste
        // anywhere else opens the modal).
        // The description has TWO possible targets now: ``tm-desc-rich`` is
        // the contenteditable WYSIWYG (default); ``tm-desc`` is the hidden
        // markdown source textarea, exposed only when the user toggles
        // "View source". Treat both as "in description" for paste routing.
        var inModalDesc = focused
            && (focused.id === 'tm-desc' || focused.id === 'tm-desc-rich');
        var inOtherModalField = focused && focused.id
            && focused.id.indexOf('tm-') === 0
            && focused.id !== 'tm-desc' && focused.id !== 'tm-desc-rich'
            && (focused.tagName === 'INPUT' || focused.tagName === 'TEXTAREA' || focused.tagName === 'SELECT');
        var inUnrelatedInput = focused
            && (focused.tagName === 'INPUT' || focused.tagName === 'TEXTAREA')
            && !inModalDesc && !inOtherModalField;

        // Substantial HTML paste → rich import. Threshold low enough to catch
        // a single Word bullet, high enough to skip "<p>hi</p>" type accidents.
        if (html && html.length > 80 && !inOtherModalField && !inUnrelatedInput) {
            console.log('[paste] HTML length=' + html.length + ' modalOpen=' + modalOpen + ' target=' + (focused && focused.id));
            e.preventDefault();
            importPastedEmail(html, cd);
            return;
        }

        // Image-only paste (screenshot etc.) — works whenever the modal is
        // open or whenever we'd open it (no-input or tm-desc focus).
        var items = cd.items;
        if (!items) return;
        var handledImage = false;
        for (var i = 0; i < items.length; i++) {
            if (items[i].kind === 'file' && /^image\//.test(items[i].type)) {
                var file = items[i].getAsFile();
                if (file) {
                    if (!modalOpen) openTaskModal('create');
                    handleTaskFile(file);
                    handledImage = true;
                }
            }
        }
        if (handledImage) e.preventDefault();
    });

    // --- RTF image extraction ---
    // Word desktop on Windows pastes a text/rtf payload that contains its
    // embedded images as hex-encoded \pngblip / \jpegblip blocks. The raw
    // image bytes never appear in clipboardData.items as files, so we have
    // to parse the RTF here to recover them.
    function extractRtfImages(rtf) {
        var images = [];
        if (!rtf) return images;

        var formatRe = /\\(pngblip|jpegblip)\b/g;
        var match;
        while ((match = formatRe.exec(rtf)) !== null) {
            var fmt = match[1] === 'pngblip' ? 'png' : 'jpeg';
            var idx = match.index + match[0].length;

            // Skip any RTF control words / whitespace between the format
            // marker and the actual hex payload (\picw, \pich, \picscalex,
            // \picwgoal, \pichgoal, etc.).
            while (idx < rtf.length) {
                while (idx < rtf.length && /\s/.test(rtf.charAt(idx))) idx++;
                if (idx >= rtf.length) break;
                if (rtf.charAt(idx) === '\\') {
                    var cw = rtf.substring(idx).match(/^\\[a-zA-Z]+(-?\d+)?\s?/);
                    if (cw) { idx += cw[0].length; continue; }
                }
                break;
            }

            // Collect contiguous hex pairs (whitespace allowed between them).
            var hex = '';
            while (idx < rtf.length) {
                var c = rtf.charAt(idx);
                if (/[0-9a-fA-F]/.test(c)) { hex += c; idx++; }
                else if (/\s/.test(c)) { idx++; }
                else break;
            }
            if (hex.length < 200) continue;  // discard tiny/empty stubs
            if (hex.length % 2 !== 0) hex = hex.slice(0, -1);

            try {
                var bytes = new Uint8Array(hex.length / 2);
                for (var k = 0; k < bytes.length; k++) {
                    bytes[k] = parseInt(hex.substr(k * 2, 2), 16);
                }
                var mime = 'image/' + fmt;
                var name = 'word_' + images.length + '.' + fmt;
                images.push(new File([new Blob([bytes], { type: mime })], name, { type: mime }));
            } catch (err) {
                console.warn('[paste] RTF hex decode failed', err);
            }
        }
        return images;
    }

    // --- Markdown utilities ---
    // Strip Word's pseudo-list bullet characters that leak through when the
    // <!--[if !supportLists]--> conditional comment isn't actually treated as
    // a comment by the source app (some Outlook Web variants render the marker
    // as visible text). Examples: "▪    ", "·    ", "o    ", "○    ".
    // Letter 'o' requires 2+ trailing whitespace so a sentence like
    // "o boy" is not misparsed as a Word marker.
    var WORD_LIST_MARKER_RE = /^\s*(?:[·•▪○◦*]\s+|o\s{2,})/;

    // Detect the Mso list-paragraph classes Word emits. These take the place
    // of <ul><li> in Word's HTML; each <p> is one bullet, with nesting hinted
    // by margin-left. Treat them as list items in the markdown output.
    function isMsoList(node) {
        var cls = node.getAttribute && node.getAttribute('class');
        if (!cls) return false;
        return /(^|\s)(MsoListParagraph|MsoListContinue)/i.test(cls);
    }

    // Approximate nesting depth from style="margin-left:Xin" or "margin-left:Xpt".
    function msoIndentDepth(node) {
        var style = (node.getAttribute && node.getAttribute('style')) || '';
        var m = style.match(/margin-left:\s*([\d.]+)\s*(in|cm|pt|px)/i);
        if (!m) return 1;
        var n = parseFloat(m[1]);
        var unit = m[2].toLowerCase();
        // Convert to inches (Word's natural list-indent unit). 0.5in ~= 1 level.
        var inches = unit === 'in' ? n
            : unit === 'cm' ? n / 2.54
            : unit === 'pt' ? n / 72
            : n / 96;
        return Math.max(1, Math.round(inches / 0.5));
    }

    // Walk a DOM tree and emit Markdown. Images are replaced with
    // __SWARM_IMG_N__ placeholders that callers resolve to ![alt](path) once
    // each source (data URI / external URL / clipboard blob) is uploaded.
    function htmlToMarkdown(root) {
        var imgs = [];
        var out = [''];                  // current line is out[out.length-1]
        var lineStarts = [];             // indices into `out` for blockquote re-prefix
        var listStack = [];              // [{type, idx, indent}, ...]

        function curLine() { return out[out.length - 1]; }
        function setLine(s) { out[out.length - 1] = s; }
        function pushLine() { out.push(''); }
        function ensureBlankBefore() {
            if (out.length === 1 && out[0] === '') return;
            if (curLine() !== '') pushLine();
            if (out.length >= 2 && out[out.length - 2] !== '') pushLine();
        }
        function append(text) { setLine(curLine() + text); }

        function inlineWrap(node, before, after) {
            append(before);
            walkChildren(node);
            append(after);
        }

        function walkChildren(node) {
            for (var i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
        }

        function walk(node) {
            if (node.nodeType === 3) {
                // Text: collapse internal whitespace, leave words intact.
                var txt = node.nodeValue.replace(/[\t ]+/g, ' ').replace(/\r/g, '');
                // Drop runs of newlines+whitespace at boundaries; HTML wraps the text.
                txt = txt.replace(/\n+/g, ' ');
                if (txt.replace(/\s+/g, '') === '' && curLine() === '') return;
                append(txt);
                return;
            }
            if (node.nodeType !== 1) return;
            var tag = node.tagName.toLowerCase();
            switch (tag) {
                case 'script': case 'style': case 'meta': case 'link':
                case 'head': case 'title':
                    return;
                case 'br':
                    pushLine();
                    return;
                case 'hr':
                    ensureBlankBefore();
                    pushLine(); setLine('---'); pushLine();
                    return;
                case 'h1': case 'h2': case 'h3':
                case 'h4': case 'h5': case 'h6':
                    ensureBlankBefore();
                    pushLine();
                    setLine('#'.repeat(parseInt(tag.charAt(1), 10)) + ' ');
                    walkChildren(node);
                    pushLine();
                    return;
                case 'p': case 'div': case 'section': case 'article':
                case 'header': case 'footer': case 'main':
                    if (isMsoList(node)) {
                        var depth = msoIndentDepth(node);
                        var indent = '  '.repeat(Math.max(0, depth - 1));
                        if (curLine() !== '') pushLine();
                        setLine(indent + '- ');
                        walkChildren(node);
                        // Strip any leftover Word bullet character from the
                        // start of the rendered line (some sources leak the
                        // visible marker through despite the conditional).
                        var k = out.length - 1;
                        out[k] = out[k].replace(
                            new RegExp('^(' + indent.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&') + '- )' + WORD_LIST_MARKER_RE.source.slice(1)),
                            '$1'
                        );
                        if (curLine() !== '') pushLine();
                        return;
                    }
                    ensureBlankBefore();
                    pushLine();
                    walkChildren(node);
                    if (curLine() !== '') pushLine();
                    return;
                case 'b': case 'strong':
                    inlineWrap(node, '**', '**');
                    return;
                case 'i': case 'em':
                    inlineWrap(node, '*', '*');
                    return;
                case 'u':
                    // Markdown has no underline — fall back to plain text.
                    walkChildren(node);
                    return;
                case 's': case 'strike': case 'del':
                    inlineWrap(node, '~~', '~~');
                    return;
                case 'code':
                    if (node.parentElement && node.parentElement.tagName.toLowerCase() === 'pre') {
                        walkChildren(node);
                    } else {
                        inlineWrap(node, '`', '`');
                    }
                    return;
                case 'pre':
                    ensureBlankBefore();
                    pushLine(); setLine('```'); pushLine();
                    var txt = (node.textContent || '').replace(/\r/g, '');
                    var ls = txt.split('\n');
                    for (var pi = 0; pi < ls.length; pi++) {
                        setLine(ls[pi]); if (pi < ls.length - 1) pushLine();
                    }
                    pushLine(); setLine('```'); pushLine();
                    return;
                case 'a':
                    var href = node.getAttribute('href') || '';
                    if (!href || href.startsWith('javascript:')) {
                        walkChildren(node); return;
                    }
                    var label = (node.textContent || '').replace(/\s+/g, ' ').trim();
                    if (!label) { append('<' + href + '>'); return; }
                    append('[' + label + '](' + href + ')');
                    return;
                case 'img':
                case 'v:imagedata': {
                    // Word VML uses <v:imagedata> in addition to (or instead of)
                    // <img>. Treat both the same — extract src and emit a
                    // placeholder.
                    var imgSrc = node.getAttribute('src')
                        || node.getAttribute('v:src')
                        || node.getAttribute('o:src')
                        || '';
                    var imgAlt = (node.getAttribute('alt') || '').replace(/[\[\]]/g, '');
                    if (!imgSrc) return;
                    var ref = { idx: imgs.length, src: imgSrc, alt: imgAlt || 'image' };
                    imgs.push(ref);
                    append('__SWARM_IMG_' + ref.idx + '__');
                    return;
                }
                case 'ul': case 'ol':
                    ensureBlankBefore();
                    pushLine();
                    listStack.push({ type: tag, idx: 0, indent: '  '.repeat(listStack.length) });
                    for (var li = 0; li < node.children.length; li++) {
                        var child = node.children[li];
                        if (child.tagName.toLowerCase() !== 'li') continue;
                        var top = listStack[listStack.length - 1];
                        var marker = (top.type === 'ol') ? (++top.idx) + '. ' : '- ';
                        if (curLine() !== '') pushLine();
                        setLine(top.indent + marker);
                        walkChildren(child);
                        if (curLine() !== '') pushLine();
                    }
                    listStack.pop();
                    if (!listStack.length) pushLine();
                    return;
                case 'li':
                    walkChildren(node);
                    return;
                case 'blockquote':
                    ensureBlankBefore();
                    pushLine();
                    var startIdx = out.length - 1;
                    walkChildren(node);
                    if (curLine() !== '') pushLine();
                    for (var qi = startIdx; qi < out.length; qi++) {
                        if (out[qi] !== '') out[qi] = '> ' + out[qi];
                    }
                    pushLine();
                    return;
                case 'table':
                    ensureBlankBefore();
                    pushLine();
                    var rows = node.querySelectorAll('tr');
                    var firstRow = true;
                    for (var ri = 0; ri < rows.length; ri++) {
                        var cells = rows[ri].querySelectorAll('th, td');
                        if (!cells.length) continue;
                        var parts = [];
                        for (var ci = 0; ci < cells.length; ci++) {
                            parts.push((cells[ci].textContent || '').replace(/\s+/g, ' ').trim().replace(/\|/g, '\\|'));
                        }
                        if (curLine() !== '') pushLine();
                        setLine('| ' + parts.join(' | ') + ' |');
                        if (firstRow) {
                            pushLine();
                            setLine('| ' + parts.map(function() { return '---'; }).join(' | ') + ' |');
                            firstRow = false;
                        }
                    }
                    pushLine();
                    return;
                default:
                    walkChildren(node);
            }
            void lineStarts;
        }

        walk(root);
        var text = out.join('\n')
            .replace(/[ \t]+\n/g, '\n')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
        return { text: text, images: imgs };
    }

    // Render markdown to safe HTML for dashboard display. Whitelist tags only.
    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, function(c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }
    function isSafeUrl(u) { return /^(https?:|mailto:|tel:|\/)/i.test(u); }
    function isSafeImgSrc(u) { return /^(https?:|data:image\/|blob:|\/)/i.test(u); }

    function inlineMd(s) {
        s = escapeHtml(s);

        // Reserve image / link / inline-code tokens so URL/attribute content
        // can't be re-matched by the emphasis transforms below. Without this,
        // a URL like "/uploads/abc_pasted_0.png" gets its "_pasted_" segment
        // mangled into "<em>pasted</em>" — wrecking the src attribute.
        var reserved = [];
        function reserve(html) {
            var idx = reserved.length;
            reserved.push(html);
            return ' MD' + idx + ' ';
        }

        // Images
        s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, function(m, alt, src) {
            if (!isSafeImgSrc(src)) return m;
            return reserve('<img src="' + src + '" alt="' + alt + '" loading="lazy">');
        });
        // Links
        s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function(m, txt, url) {
            if (!isSafeUrl(url)) return m;
            return reserve('<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + txt + '</a>');
        });
        // Inline code
        s = s.replace(/`([^`]+)`/g, function(m, code) {
            return reserve('<code>' + code + '</code>');
        });

        // Emphasis transforms — safe to run now that URLs are tokenized.
        s = s.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/__([^_]+?)__/g, '<strong>$1</strong>');
        s = s.replace(/(^|[^\\*])\*([^*\n]+?)\*/g, '$1<em>$2</em>');
        s = s.replace(/(^|[^\\_])_([^_\n]+?)_/g, '$1<em>$2</em>');
        s = s.replace(/~~([^~]+?)~~/g, '<del>$1</del>');

        // Restore reserved tokens.
        s = s.replace(/ MD(\d+) /g, function(m, idx) {
            return reserved[parseInt(idx, 10)];
        });
        return s;
    }

    function renderMarkdown(src) {
        if (!src) return '';
        var lines = String(src).replace(/\r\n/g, '\n').split('\n');
        var html = [];
        var inCode = false, codeBuf = [];
        var listStack = [];     // ['ul'|'ol', ...]
        var paraBuf = [];

        function flushPara() {
            if (paraBuf.length) {
                // Render each source line through inlineMd separately, then
                // join with <br>. This preserves single-newline line breaks
                // (email headers, address blocks, signatures) instead of
                // collapsing them with spaces — strict CommonMark would do
                // the latter, but for our paste-driven content every line
                // break is intentional.
                var rendered = paraBuf.map(function(ln) { return inlineMd(ln); }).join('<br>');
                html.push('<p>' + rendered + '</p>');
                paraBuf = [];
            }
        }
        function closeListsTo(targetDepth) {
            while (listStack.length > targetDepth) {
                html.push('</' + listStack.pop() + '>');
            }
        }
        function flushAll() {
            flushPara();
            closeListsTo(0);
        }

        for (var i = 0; i < lines.length; i++) {
            var ln = lines[i];

            // Fenced code blocks
            if (/^```/.test(ln)) {
                if (inCode) {
                    html.push('<pre><code>' + escapeHtml(codeBuf.join('\n')) + '</code></pre>');
                    inCode = false; codeBuf = [];
                } else {
                    flushAll();
                    inCode = true;
                }
                continue;
            }
            if (inCode) { codeBuf.push(ln); continue; }

            // Blank line
            if (ln.trim() === '') { flushAll(); continue; }

            // Heading
            var hMatch = ln.match(/^(#{1,6})\s+(.*)$/);
            if (hMatch) {
                flushAll();
                html.push('<h' + hMatch[1].length + '>' + inlineMd(hMatch[2]) + '</h' + hMatch[1].length + '>');
                continue;
            }

            // Horizontal rule
            if (/^\s*[-_*]{3,}\s*$/.test(ln)) {
                flushAll();
                html.push('<hr>');
                continue;
            }

            // Blockquote
            var bqMatch = ln.match(/^>\s?(.*)$/);
            if (bqMatch) {
                flushAll();
                html.push('<blockquote>' + inlineMd(bqMatch[1]) + '</blockquote>');
                continue;
            }

            // List items (with indent → nesting depth)
            var liMatch = ln.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
            if (liMatch) {
                flushPara();
                var depth = Math.floor(liMatch[1].length / 2) + 1;
                var marker = liMatch[2];
                var listType = /^\d+\./.test(marker) ? 'ol' : 'ul';
                if (listStack.length >= depth) {
                    closeListsTo(depth);
                    if (listStack[depth - 1] !== listType) {
                        closeListsTo(depth - 1);
                    }
                }
                while (listStack.length < depth) {
                    html.push('<' + listType + '>');
                    listStack.push(listType);
                }
                html.push('<li>' + inlineMd(liMatch[3]) + '</li>');
                continue;
            }

            closeListsTo(0);
            paraBuf.push(ln);
        }
        flushAll();
        if (inCode && codeBuf.length) html.push('<pre><code>' + escapeHtml(codeBuf.join('\n')) + '</code></pre>');
        return html.join('\n');
    }

    // (Inline task-list markdown rendering was removed when the task list
    // collapsed to one or two lines. Rich rendering still happens inside
    // the Edit modal's contenteditable surface.)

    function importPastedEmail(html, clipboardData) {
        // Debug: log clipboard contents
        if (clipboardData && clipboardData.items) {
            for (var di = 0; di < clipboardData.items.length; di++) {
                var ci = clipboardData.items[di];
                console.log('[paste] item', di, 'kind:', ci.kind, 'type:', ci.type);
            }
        }

        var doc = new DOMParser().parseFromString(html, 'text/html');
        var tmp = doc.body;

        // Optional subject hint (Outlook Web wraps "Subject" in a class).
        var subject = '';
        var subjectEl = tmp.querySelector('[class*="Subject"], [class*="subject"]');
        if (subjectEl) subject = subjectEl.textContent.trim();

        // Convert to markdown — emits __SWARM_IMG_N__ placeholders for each <img>.
        var md = htmlToMarkdown(tmp);
        var body = md.text;
        var imageRefs = md.images;
        console.log('[paste] markdown body length=' + body.length + ' images=' + imageRefs.length);

        // Pull image blobs from the clipboard items array. These are Word/Outlook's
        // out-of-band attachments — they don't appear in the HTML <img> list.
        var clipboardBlobs = [];
        var seenBlobs = new Set();
        function pushBlob(f) {
            if (!f) return;
            // Dedup across .items and .files (browsers expose both, sometimes
            // pointing at the same File reference, sometimes not).
            var sig = f.size + ':' + (f.name || '') + ':' + (f.type || '');
            if (seenBlobs.has(sig)) return;
            seenBlobs.add(sig);
            clipboardBlobs.push(f);
        }
        if (clipboardData && clipboardData.items) {
            for (var j = 0; j < clipboardData.items.length; j++) {
                var item = clipboardData.items[j];
                if (item.kind === 'file' && /^image\//.test(item.type)) {
                    pushBlob(item.getAsFile());
                }
            }
        }
        if (clipboardData && clipboardData.files) {
            for (var jf = 0; jf < clipboardData.files.length; jf++) {
                var ff = clipboardData.files[jf];
                if (ff && /^image\//.test(ff.type || '')) pushBlob(ff);
            }
        }

        // Word desktop on Windows doesn't expose images as file blobs — but
        // RTF clipboard payload embeds them as \pngblip/\jpegblip hex. Extract
        // those when no native blobs are available.
        if (clipboardBlobs.length === 0 && clipboardData) {
            var rtf = clipboardData.getData('text/rtf');
            if (rtf) {
                console.log('[paste] RTF length=' + rtf.length + ' formats=' +
                    JSON.stringify({
                        pict: (rtf.match(/\\pict\b/g) || []).length,
                        pngblip: (rtf.match(/\\pngblip\b/g) || []).length,
                        jpegblip: (rtf.match(/\\jpegblip\b/g) || []).length,
                        wmetafile: (rtf.match(/\\wmetafile\d?\b/g) || []).length,
                        emfblip: (rtf.match(/\\emfblip\b/g) || []).length,
                        wbitmap: (rtf.match(/\\wbitmap\b/g) || []).length,
                        dibitmap: (rtf.match(/\\dibitmap\b/g) || []).length,
                    }));
                var rtfImgs = extractRtfImages(rtf);
                console.log('[paste] RTF images recovered=' + rtfImgs.length);
                rtfImgs.forEach(pushBlob);
            }
        }
        console.log('[paste] clipboard blobs=' + clipboardBlobs.length);

        // If the modal is already open (user pasted into the textarea), append
        // to existing content instead of clobbering it. Otherwise open a new
        // create-mode modal pre-populated with the parsed body.
        var modalAlreadyOpen = document.getElementById('task-modal').style.display !== 'none';
        if (modalAlreadyOpen) {
            var descElExisting = document.getElementById('tm-desc');
            if (descElExisting.value.trim() === '') {
                descElExisting.value = body;
            } else {
                descElExisting.value = descElExisting.value.replace(/\s+$/, '') + '\n\n' + body;
            }
            var titleEl = document.getElementById('tm-title');
            if (subject && !titleEl.value) titleEl.value = subject;
            if (typeof _updateTaskMdPreview === 'function') _updateTaskMdPreview();
        } else {
            openTaskModal('create', { title: subject, desc: body });
        }

        // Resolve image placeholders. Each ref points at a data URI, http(s) URL,
        // cid:/blob: (unfetchable), or other. cid:/blob: refs are paired with
        // clipboard blobs in DOM order — Word puts the blobs in the same order.
        var pendingClipIdx = 0;
        var pending = imageRefs.length;

        function patchPlaceholder(idx, replacement) {
            var descEl = document.getElementById('tm-desc');
            var token = '__SWARM_IMG_' + idx + '__';
            // Use split/join so we don't accidentally regex-match the user's text.
            descEl.value = descEl.value.split(token).join(replacement);
            if (typeof _updateTaskMdPreview === 'function') _updateTaskMdPreview();
        }

        // Pre-compute the extra clipboard blobs we'll append after image-ref
        // resolution. The forEach below claims clipboard blobs in order via
        // pendingClipIdx, so this slice happens at the end of the run — but we
        // need a stable count up-front for the toast logic in finishOne.
        var extraBlobsCount = Math.max(0, clipboardBlobs.length - imageRefs.length);

        function finishOne() {
            pending--;
            if (pending <= 0 && extraBlobsCount === 0) {
                showToast('Pasted with formatting' + (imageRefs.length ? ' and ' + imageRefs.length + ' image(s)' : ''));
            }
        }

        function handleFile(file, refIdx) {
            // Insert a blob URL immediately for instant in-modal preview.
            var blobUrl = URL.createObjectURL(file);
            file._blobUrl = blobUrl;
            taskModalPendingFiles.push(file);
            addLocalThumbnail(file);
            patchPlaceholder(refIdx, '![' + (file.name || 'image') + '](' + blobUrl + ')');
            finishOne();
            backgroundUploadAndSwap(file, blobUrl);
        }

        imageRefs.forEach(function(ref) {
            var src = ref.src;
            if (src.startsWith('data:image/')) {
                try {
                    var match = src.match(/^data:(image\/[\w+.-]+);base64,(.+)$/);
                    if (!match) { patchPlaceholder(ref.idx, ''); finishOne(); return; }
                    var mime = match[1];
                    var ext = (mime.split('/')[1] || 'png').replace(/[^\w]/g, '');
                    var binary = atob(match[2]);
                    var bytes = new Uint8Array(binary.length);
                    for (var b = 0; b < binary.length; b++) bytes[b] = binary.charCodeAt(b);
                    var blob = new Blob([bytes], { type: mime });
                    handleFile(new File([blob], 'pasted_' + ref.idx + '.' + ext, { type: mime }), ref.idx);
                } catch (err) {
                    console.warn('[paste] data URI decode failed', err);
                    patchPlaceholder(ref.idx, '');
                    finishOne();
                }
                return;
            }
            if (/^https?:/i.test(src)) {
                actionFetch('/action/fetch-image', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: 'url=' + encodeURIComponent(src),
                })
                    .then(function(r) { return r.ok ? r.json() : null; })
                    .then(function(data) {
                        if (data && data.path) {
                            taskModalAttachmentPaths.push(data.path);
                            addThumbnail(data.path);
                            var basename = data.path.split('/').pop();
                            patchPlaceholder(ref.idx, '![' + (ref.alt || 'image') + '](/uploads/' + encodeURIComponent(basename) + ')');
                        } else {
                            patchPlaceholder(ref.idx, '');
                        }
                    })
                    .catch(function() { patchPlaceholder(ref.idx, ''); })
                    .finally(finishOne);
                return;
            }
            // cid:/blob:/other — pair with the next available clipboard blob.
            if (pendingClipIdx < clipboardBlobs.length) {
                handleFile(clipboardBlobs[pendingClipIdx++], ref.idx);
            } else {
                patchPlaceholder(ref.idx, '');
                finishOne();
            }
        });

        // Any clipboard blobs the HTML didn't reference: append at the end.
        var extraBlobs = clipboardBlobs.slice(pendingClipIdx);
        if (extraBlobs.length) {
            var descEl2 = document.getElementById('tm-desc');
            extraBlobs.forEach(function(file) {
                var blobUrl = URL.createObjectURL(file);
                file._blobUrl = blobUrl;
                taskModalPendingFiles.push(file);
                addLocalThumbnail(file);
                descEl2.value += '\n\n![' + (file.name || 'image') + '](' + blobUrl + ')';
                backgroundUploadAndSwap(file, blobUrl);
            });
            if (typeof _updateTaskMdPreview === 'function') _updateTaskMdPreview();
            showToast('Pasted with formatting and ' + (imageRefs.length + extraBlobs.length) + ' image(s)');
        } else if (imageRefs.length === 0 && clipboardBlobs.length === 0) {
            showToast('Pasted with formatting');
        }
    }

    // After a file is uploaded, scan the description for any markdown image
    // reference that points at a relative path matching this file's basename
    // (e.g. ![](media/foo.png), ![](images/foo.png), ![](./foo.png)) and
    // rewrite it to the permanent /uploads/<hash>_<original> path. Lets users
    // paste pandoc-style markdown with a media/ folder, drop the images, and
    // have the references stitch up automatically.
    function rewriteRelativeImageRefs(uploadedPath) {
        var descEl = document.getElementById('tm-desc');
        if (!descEl) return false;
        var basename = uploadedPath.split('/').pop();
        if (!basename) return false;
        // The server prepends a 12-char hash + '_' to filenames; strip it so
        // we can match the user's original reference.
        var orig = basename.replace(/^[a-f0-9]{12}_/, '');
        if (!orig) return false;
        var newUrl = '/uploads/' + encodeURIComponent(basename);
        var escaped = orig.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        // Match ![alt](relative-path-ending-in-orig). Excludes absolute and
        // already-resolved URLs so reruns are idempotent.
        var re = new RegExp(
            '(!\\[[^\\]]*\\]\\()' +
                '(?!https?://|/uploads/|data:|blob:|cid:|mailto:)' +
                '[^)\\s]*?' + escaped +
                '(\\))',
            'g'
        );
        var before = descEl.value;
        descEl.value = descEl.value.replace(re, '$1' + newUrl + '$2');
        if (descEl.value !== before) {
            if (typeof _updateTaskMdPreview === 'function') _updateTaskMdPreview();
            return true;
        }
        return false;
    }

    // Upload a freshly-pasted file in the background and swap the in-textarea
    // blob: URL for the permanent /uploads/... path. Routes through
    // /action/task/upload when editing an existing task (so the file is
    // attached server-side immediately) or /action/upload otherwise (path is
    // attached on Create via taskModalAttachmentPaths).
    function backgroundUploadAndSwap(file, blobUrl) {
        var fd = new FormData();
        fd.append('file', file);
        var endpoint = '/action/upload';
        if (taskModalMode === 'edit' && taskModalId) {
            fd.append('task_id', taskModalId);
            endpoint = '/action/task/upload';
        }
        return actionFetch(endpoint, { method: 'POST', body: fd })
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(ud) {
                if (!ud || !ud.path) return;
                file._uploadedPath = ud.path;
                if (taskModalMode !== 'edit') {
                    // Create flow: stash in taskModalAttachmentPaths so the
                    // create call's `&attachments=` payload picks it up.
                    taskModalAttachmentPaths.push(ud.path);
                }
                var basename = ud.path.split('/').pop();
                var finalUrl = '/uploads/' + encodeURIComponent(basename);
                var descEl = document.getElementById('tm-desc');
                if (blobUrl) descEl.value = descEl.value.split(blobUrl).join(finalUrl);
                // Also resolve any relative-path references (pandoc-style
                // `![](media/foo.png)`) that match this file's basename.
                rewriteRelativeImageRefs(ud.path);
                try { if (blobUrl) URL.revokeObjectURL(blobUrl); } catch (e) {}
                if (typeof _updateTaskMdPreview === 'function') _updateTaskMdPreview();
            })
            .catch(function(e) { console.warn('[paste] background upload failed', e); });
    }

    // --- Ctrl+Enter submits from modal inputs ---
    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            var active = document.activeElement;
            // Task modal
            if (document.getElementById('task-modal').style.display !== 'none') {
                if (active && (active.id === 'tm-desc' || active.id === 'tm-desc-rich'
                        || active.id === 'tm-title' || active.id === 'tm-deps')) {
                    e.preventDefault();
                    submitTaskModal();
                    return;
                }
            }
            // Broadcast modal
            if (document.getElementById('broadcast-modal').style.display !== 'none') {
                if (active && active.id === 'broadcast-input') {
                    e.preventDefault();
                    sendBroadcast();
                    return;
                }
            }
            // Spawn modal
            if (document.getElementById('spawn-modal').style.display !== 'none') {
                if (active && (active.id === 'spawn-name' || active.id === 'spawn-path')) {
                    e.preventDefault();
                    doSpawn();
                    return;
                }
            }
        }
    });

    // --- Periodic refresh (fallback if WS drops — heartbeat covers most updates) ---
    _trackedIntervals.push(setInterval(function() {
        if (document.hidden) return;  // skip when tab is backgrounded
        if (ws && ws.readyState === WebSocket.OPEN) return;  // WS healthy — no polling needed
        refreshWorkers();
        refreshStatus();
        refreshTasks();
        refreshPipelines();
        refreshBuzzLog();
        if (selectedWorker) refreshDetail();
    }, 30000));

    // --- Approval-rate badge (drone auto-approval % over last 24h) ---
    function refreshApprovalRate() {
        if (document.hidden) return;
        fetch('/api/drones/approval-rate?hours=24', {
            headers: {'X-Requested-With': 'fetch'},
            credentials: 'same-origin'
        }).then(function(r) {
            if (!r.ok) return null;
            return r.json();
        }).then(function(data) {
            if (!data) return;
            var badge = document.getElementById('approval-rate-badge');
            if (!badge) return;
            if (data.rate === null || data.rate === undefined) {
                badge.style.display = 'none';
                return;
            }
            var pct = Math.round(data.rate * 100);
            badge.textContent = pct + '%';
            badge.style.display = 'inline-block';
            badge.title = 'Drone auto-approval rate (24h): ' + pct + '% — ' +
                data.approvals + ' auto-approvals, ' + data.escalations + ' escalations';
        }).catch(function() { /* ignore transient errors */ });
    }
    refreshApprovalRate();
    _trackedIntervals.push(setInterval(refreshApprovalRate, 60000));

    // --- Event delegation (avoids inline onclick + template escaping issues) ---
    document.addEventListener('click', function(e) {
        // Queen card click — she's sidebar-adjacent but not .worker-item,
        // so route her through the same selectWorker flow so the detail
        // pane swaps to her PTY / chat target.
        var queenCard = e.target.closest('[data-queen-card]');
        if (queenCard && queenCard.dataset.worker) {
            selectWorker(queenCard.dataset.worker);
            return;
        }
        // Worker item click
        var item = e.target.closest('.worker-item');
        if (item && item.dataset.worker) {
            selectWorker(item.dataset.worker);
            return;
        }
        // Proposal View button
        var viewBtn = e.target.closest('.view-proposal-btn');
        if (viewBtn) {
            showProposalDetail(viewBtn.dataset.proposalId);
            return;
        }
        // Task assign button
        var assignBtn = e.target.closest('.assign-task-btn');
        if (assignBtn) {
            if (!selectedWorker && assignBtn.dataset.targetWorker) {
                selectWorker(assignBtn.dataset.targetWorker);
            }
            assignTask(assignBtn.dataset.taskId, assignBtn.dataset.taskTitle);
            return;
        }
        // Task start button (send queued task to worker)
        var startBtn = e.target.closest('.start-task-btn');
        if (startBtn) {
            startTask(startBtn.dataset.taskId);
            return;
        }
        // Task complete button
        var completeBtn = e.target.closest('.complete-task-btn');
        if (completeBtn) {
            completeTask(completeBtn.dataset.taskId);
            return;
        }
        // Task remove button
        var removeBtn = e.target.closest('.remove-task-btn');
        if (removeBtn) {
            removeTask(removeBtn.dataset.taskId);
            return;
        }
        // Task fail button
        var failBtn = e.target.closest('.fail-task-btn');
        if (failBtn) {
            failTask(failBtn.dataset.taskId);
            return;
        }
        // Task unassign button
        var unassignBtn = e.target.closest('.unassign-task-btn');
        if (unassignBtn) {
            unassignTask(unassignBtn.dataset.taskId);
            return;
        }
        // Promote (Backlog → Unassigned, "Hand to Queen") button
        var promoteBtn = e.target.closest('.promote-task-btn');
        if (promoteBtn) {
            promoteTask(promoteBtn.dataset.taskId);
            return;
        }
        // Approve task button (cross-project)
        var approveBtn = e.target.closest('.approve-task-btn');
        if (approveBtn) {
            approveTask(approveBtn.dataset.taskId);
            return;
        }
        // Reject task button (cross-project)
        var rejectBtn = e.target.closest('.reject-task-btn');
        if (rejectBtn) {
            rejectTask(rejectBtn.dataset.taskId);
            return;
        }
        // Reopen task button
        var reopenBtn = e.target.closest('.reopen-task-btn');
        if (reopenBtn) {
            reopenTask(reopenBtn.dataset.taskId);
            return;
        }
        // Retry draft button
        var retryBtn = e.target.closest('.retry-draft-btn');
        if (retryBtn) {
            retryDraft(retryBtn.dataset.taskId);
            return;
        }
        // Task edit button OR clicking the task row anywhere outside an
        // interactive control. The row carries the same data-* attrs as
        // the Edit button so either path opens the same modal.
        var editBtn = e.target.closest('.edit-task-btn');
        if (editBtn) {
            showEditTask(editBtn.dataset.taskId, editBtn.dataset.taskTitle, editBtn.dataset.taskDesc, editBtn.dataset.taskPriority, editBtn.dataset.taskType || '', editBtn.dataset.taskTags, editBtn.dataset.taskDeps || '', editBtn.dataset.taskResolution || '', editBtn.dataset.taskStatus || '', editBtn.dataset.taskCross || '', editBtn.dataset.taskSourceWorker || '', editBtn.dataset.taskTargetWorker || '', editBtn.dataset.taskDepType || '', editBtn.dataset.taskAcceptance || '', editBtn.dataset.taskContextRefs || '', editBtn.dataset.taskAttachments || '', editBtn.dataset.taskWorker || '');
            return;
        }
        var taskRow = e.target.closest('.task-row-clickable');
        if (taskRow && !e.target.closest('button, a, input, select, textarea, details, summary, .task-history-panel')) {
            var ds = taskRow.dataset;
            showEditTask(ds.taskId, ds.taskTitle, ds.taskDesc, ds.taskPriority, ds.taskType || '', ds.taskTags || '', ds.taskDeps || '', ds.taskResolution || '', ds.status || '', ds.taskCross || '', ds.taskSourceWorker || '', ds.taskTargetWorker || '', ds.taskDepType || '', ds.taskAcceptance || '', ds.taskContextRefs || '', ds.taskAttachments || '', ds.worker || '');
            return;
        }
        // Task history toggle
        var histBtn = e.target.closest('.history-task-btn');
        if (histBtn) {
            var tid = histBtn.dataset.taskId;
            var panel = document.getElementById('task-history-' + tid);
            if (panel) {
                if (panel.style.display === 'none') {
                    panel.style.display = 'block';
                    panel.innerHTML = '<span class="spinner spinner-margin"></span>';
                    fetch('/partials/task-history/' + encodeURIComponent(tid))
                        .then(function(r) { return r.text(); })
                        .then(function(html) { panel.innerHTML = html; formatLocalTimes(panel); });
                } else {
                    panel.style.display = 'none';
                }
            }
            return;
        }
        // Launch group preset button
        var groupBtn = e.target.closest('.launch-group-btn');
        if (groupBtn) {
            selectLaunchGroup(groupBtn.dataset.group);
            return;
        }
        // Spawn preset button
        var spawnBtn = e.target.closest('.spawn-preset-btn');
        if (spawnBtn) {
            document.getElementById('spawn-name').value = spawnBtn.dataset.name;
            document.getElementById('spawn-path').value = spawnBtn.dataset.path;
            return;
        }
    });

    // --- Context menu ---
    var ctxMenu = document.getElementById('ctx-menu');

    function showContextMenu(e, items) {
        if (e) e.preventDefault();
        if (!items || !items.length) return;
        var html = '';
        items.forEach(function(item) {
            if (item.sep) { html += '<div class="ctx-menu-sep"></div>'; return; }
            if (item.header) { html += '<div class="ctx-menu-header">' + escapeHtml(item.header) + '</div>'; return; }
            var cls = 'ctx-menu-item' + (item.danger ? ' ctx-danger' : '');
            html += '<div class="' + cls + '" data-ctx-action="' + item.action + '">' + item.label + '</div>';
        });
        ctxMenu.innerHTML = html;
        ctxMenu.style.display = 'block';
        if (e) {
            var rect = ctxMenu.getBoundingClientRect();
            var x = e.clientX, y = e.clientY;
            if (x + rect.width > window.innerWidth) x = window.innerWidth - rect.width - 8;
            if (y + rect.height > window.innerHeight) y = window.innerHeight - rect.height - 8;
            ctxMenu.style.left = x + 'px';
            ctxMenu.style.top = y + 'px';
        }
    }

    function hideContextMenu() {
        ctxMenu.style.display = 'none';
        ctxMenu.innerHTML = '';
    }

    document.addEventListener('click', hideContextMenu);
    document.addEventListener('scroll', hideContextMenu, true);
    document.addEventListener('keydown', function(e) { if (e.key === 'Escape') hideContextMenu(); });

    // Action dispatcher
    ctxMenu.addEventListener('click', function(e) {
        var item = e.target.closest('.ctx-menu-item');
        if (!item) return;
        var action = item.dataset.ctxAction;
        hideContextMenu();
        if (action.startsWith('w:')) ctxWorkerAction(action.slice(2));
        else if (action.startsWith('t:')) ctxTaskAction(action.slice(2));
        else if (action.startsWith('p:')) ctxProposalAction(action.slice(2));
    });

    // --- Worker context menu ---
    var _ctxWorkerName = null;
    var _ctxWorkerPath = '';
    var _ctxWorkerProvider = '';

    function workerMenuItems(el) {
        var name = el.dataset.worker;
        var state = el.dataset.state;
        _ctxWorkerName = name;
        _ctxWorkerPath = el.dataset.path || '';
        _ctxWorkerProvider = el.dataset.provider || '';
        var items = [{ header: name }];
        if (state === 'BUZZING') {
            items.push({ label: 'Escape (interrupt)', action: 'w:escape' });
        }
        if (state === 'BUZZING' || state === 'WAITING' || state === 'STUNG') {
            items.push({ label: 'Force to rest', action: 'w:force-rest' });
        }
        if (state === 'RESTING' || state === 'SLEEPING') {
            items.push({ label: 'Continue', action: 'w:continue' });
        }
        if (state === 'RESTING') {
            items.push({ label: 'Sleep', action: 'w:sleep' });
        }
        if (state === 'WAITING') {
            items.push({ label: 'Continue (approve)', action: 'w:continue' });
        }
        if (state === 'STUNG') {
            items.push({ label: 'Revive', action: 'w:revive' });
        }
        items.push({ label: 'Ask Queen', action: 'w:queen' });
        items.push({ sep: true });
        items.push({ label: 'Open terminal', action: 'w:terminal' });
        items.push({ label: 'Copy name', action: 'w:copy' });
        // Duplicate as different LLM
        var providers = _swarmCfg.providers || ['claude', 'gemini', 'codex'];
        var otherProviders = providers.filter(function(p) { return p !== _ctxWorkerProvider; });
        if (otherProviders.length && _ctxWorkerPath) {
            items.push({ sep: true });
            items.push({ header: 'duplicate as' });
            otherProviders.forEach(function(p) {
                items.push({ label: p, action: 'w:dup:' + p });
            });
        }
        // Save spawned worker to config
        if (el.dataset.inConfig !== 'true') {
            items.push({ sep: true });
            items.push({ header: 'save' });
            items.push({ label: 'Save to config', action: 'w:save-config' });
            items.push({ label: 'Add to group', action: 'w:add-to-group-menu' });
        }
        items.push({ sep: true });
        items.push({ label: 'Kill', action: 'w:kill', danger: true });
        return items;
    }

    function ctxWorkerAction(action) {
        if (!_ctxWorkerName) return;
        selectWorker(_ctxWorkerName);
        // Handle dup:<provider> actions
        if (action.startsWith('dup:')) {
            var provider = action.slice(4);
            var newName = _ctxWorkerName + '-' + provider;
            fetch('/api/workers/spawn', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
                body: JSON.stringify({ name: newName, path: _ctxWorkerPath, provider: provider })
            }).then(function(r) { return r.json(); })
              .then(function(data) {
                  if (data.status === 'spawned') {
                      showToast('Spawned ' + newName + ' (' + provider + ')');
                      refreshWorkers();
                  } else {
                      showToast(data.error || 'Failed to spawn', true);
                  }
              })
              .catch(function(err) { showToast('Spawn failed: ' + err.message, true); });
            return;
        }
        if (action === 'save-config') {
            fetch('/api/config/workers/' + encodeURIComponent(_ctxWorkerName) + '/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' }
            }).then(function(r) { return r.json(); })
              .then(function(data) {
                  if (data.status === 'saved') {
                      showToast('Saved ' + _ctxWorkerName + ' to config');
                      window._toastApplyResult(data, 'Save worker');
                      refreshWorkers();
                  } else {
                      showToast(data.error || 'Failed to save', true);
                  }
              })
              .catch(function(err) { showToast('Save failed: ' + err.message, true); });
            return;
        }
        if (action === 'add-to-group-menu') {
            var groupItems = [{ header: 'add to group' }];
            _configGroups.forEach(function(g) {
                groupItems.push({ label: g, action: 'w:add-to-group:' + g });
            });
            groupItems.push({ sep: true });
            groupItems.push({ label: 'New group\u2026', action: 'w:add-to-group:__new__' });
            showContextMenu(null, groupItems);
            return;
        }
        if (action.startsWith('add-to-group:')) {
            var groupTarget = action.slice('add-to-group:'.length);
            var groupBody;
            if (groupTarget === '__new__') {
                var newGroup = prompt('New group name:');
                if (!newGroup) return;
                groupBody = { group: newGroup, create: true };
            } else {
                groupBody = { group: groupTarget };
            }
            fetch('/api/config/workers/' + encodeURIComponent(_ctxWorkerName) + '/add-to-group', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'Dashboard' },
                body: JSON.stringify(groupBody)
            }).then(function(r) { return r.json(); })
              .then(function(data) {
                  if (data.status === 'added') {
                      showToast('Added ' + _ctxWorkerName + ' to group ' + data.group);
                      window._toastApplyResult(data, 'Add to group');
                      refreshWorkers();
                  } else {
                      showToast(data.error || 'Failed to add to group', true);
                  }
              })
              .catch(function(err) { showToast('Add to group failed: ' + err.message, true); });
            return;
        }
        switch (action) {
            case 'continue': continueWorker(); break;
            case 'escape':
                fetch('/api/workers/' + encodeURIComponent(_ctxWorkerName) + '/escape', {
                    method: 'POST',
                    headers: { 'X-Requested-With': 'Dashboard' },
                })
                    .then(function() { showToast('Escape sent to ' + _ctxWorkerName); });
                break;
            case 'force-rest':
                fetch('/api/workers/' + encodeURIComponent(_ctxWorkerName) + '/force-rest', {
                    method: 'POST',
                    headers: { 'X-Requested-With': 'Dashboard' },
                })
                    .then(function(r) { return r.json(); })
                    .then(function(d) {
                        if (d && d.status === 'force_rested') {
                            showToast(_ctxWorkerName + ' forced to RESTING');
                            refreshWorkers();
                        } else {
                            showToast((d && d.error) || 'Force-rest failed', true);
                        }
                    })
                    .catch(function(err) { showToast('Force-rest failed: ' + err.message, true); });
                break;
            case 'revive': reviveWorker(); break;
            case 'sleep':
                fetch('/api/workers/' + encodeURIComponent(_ctxWorkerName) + '/sleep', { method: 'POST', headers: { 'X-Requested-With': 'Dashboard' } })
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (data.error) { showToast(data.error, true); }
                        else { showToast(_ctxWorkerName + ' put to sleep'); refreshWorkers(); }
                    })
                    .catch(function(err) { showToast('Sleep failed: ' + err.message, true); });
                break;
            case 'kill': killWorker(); break;
            case 'terminal': break; // selectWorker already shows terminal
            case 'copy':
                navigator.clipboard.writeText(_ctxWorkerName);
                showToast('Copied: ' + _ctxWorkerName);
                break;
        }
    }

    // --- Task context menu ---
    var _ctxTaskId = null, _ctxTaskTitle = null;
    var _ctxTaskEl = null;

    function taskMenuItems(el) {
        var status = el.dataset.status;
        var btn = el.querySelector('[data-task-id]');
        if (!btn) return [];
        _ctxTaskId = btn.dataset.taskId;
        _ctxTaskTitle = btn.dataset.taskTitle || '';
        _ctxTaskEl = el;
        var items = [{ header: 'Task #' + (_ctxTaskTitle || _ctxTaskId).substring(0, 30) }];
        if (status === 'backlog') {
            items.push({ label: 'Hand to Queen', action: 't:promote' });
            items.push({ label: 'Assign to worker', action: 't:assign' });
        }
        if (status === 'unassigned') {
            items.push({ label: 'Assign to worker', action: 't:assign' });
        }
        if (status === 'assigned' || status === 'active') {
            items.push({ label: 'Mark complete', action: 't:complete' });
            items.push({ label: 'Unassign', action: 't:unassign' });
            items.push({ label: 'Mark failed', action: 't:fail', danger: true });
        }
        if (status === 'done' || status === 'failed') {
            items.push({ label: 'Reopen', action: 't:reopen' });
        }
        items.push({ sep: true });
        items.push({ label: 'Edit', action: 't:edit' });
        items.push({ label: 'History', action: 't:history' });
        items.push({ sep: true });
        items.push({ label: 'Remove', action: 't:remove', danger: true });
        return items;
    }

    function ctxTaskAction(action) {
        if (!_ctxTaskId) return;
        switch (action) {
            case 'assign': assignTask(_ctxTaskId, _ctxTaskTitle); break;
            case 'promote': promoteTask(_ctxTaskId); break;
            case 'complete': completeTask(_ctxTaskId); break;
            case 'unassign': unassignTask(_ctxTaskId); break;
            case 'fail': failTask(_ctxTaskId); break;
            case 'reopen': reopenTask(_ctxTaskId); break;
            case 'remove': removeTask(_ctxTaskId); break;
            case 'edit':
                var eb = _ctxTaskEl ? _ctxTaskEl.querySelector('.edit-task-btn') : null;
                if (eb) eb.click();
                break;
            case 'history':
                var hb = _ctxTaskEl ? _ctxTaskEl.querySelector('.history-task-btn') : null;
                if (hb) hb.click();
                break;
        }
    }

    // --- Proposal context menu ---
    var _ctxProposalId = null, _ctxProposalHasEmail = false;

    function proposalMenuItems(el) {
        _ctxProposalId = el.dataset.proposalId;
        _ctxProposalHasEmail = el.dataset.hasEmail === '1';
        var worker = el.querySelector('.proposal-worker');
        var label = worker ? worker.textContent.trim() : _ctxProposalId;
        return [
            { header: label },
            { label: 'View details', action: 'p:view' },
            { label: 'Approve', action: 'p:approve' },
            { sep: true },
            { label: 'Reject', action: 'p:reject', danger: true },
        ];
    }

    function ctxProposalAction(action) {
        if (!_ctxProposalId) return;
        switch (action) {
            case 'view': showProposalDetail(_ctxProposalId); break;
            case 'approve': approveProposal(_ctxProposalId); break;
            case 'reject': rejectProposal(_ctxProposalId); break;
        }
    }

    // --- contextmenu event delegation ---
    document.addEventListener('contextmenu', function(e) {
        var worker = e.target.closest('.worker-item');
        if (worker && worker.dataset.worker) {
            showContextMenu(e, workerMenuItems(worker));
            return;
        }
        var task = e.target.closest('.task-item');
        if (task) {
            var tItems = taskMenuItems(task);
            if (tItems.length) showContextMenu(e, tItems);
            return;
        }
        var proposal = e.target.closest('.proposal-item');
        if (proposal && proposal.dataset.proposalId) {
            showContextMenu(e, proposalMenuItems(proposal));
            return;
        }
        hideContextMenu();
    });

    // --- Worker cycling (Ctrl+Tab / Alt+] / Alt+[) ---
    function getVisibleWorkerItems() {
        var items = [];
        document.querySelectorAll('.worker-item').forEach(function(el) {
            // Skip workers inside collapsed groups
            var group = el.closest('.group-body');
            if (group && group.style.display === 'none') return;
            items.push(el);
        });
        return items;
    }
    function cycleWorker(direction) {
        var items = getVisibleWorkerItems();
        if (items.length === 0) return;
        var idx = -1;
        for (var i = 0; i < items.length; i++) {
            if (items[i].dataset.worker === selectedWorker) { idx = i; break; }
        }
        // Skip sleeping workers (try all items before giving up)
        for (var attempt = 0; attempt < items.length; attempt++) {
            idx = (idx + direction + items.length) % items.length;
            if (items[idx].dataset.state !== 'SLEEPING') {
                selectWorker(items[idx].dataset.worker);
                return;
            }
        }
    }
    document.addEventListener('keydown', function(e) {
        // Recover focus to terminal after refresh/reconnect if user starts typing.
        if (
            activeTermWorker &&
            inlineTerm &&
            inlineTerm.textarea &&
            document.activeElement !== inlineTerm.textarea &&
            document.getElementById('terminal-modal').style.display === 'none'
        ) {
            var isEditable = document.activeElement && (
                document.activeElement.tagName === 'INPUT' ||
                document.activeElement.tagName === 'TEXTAREA' ||
                document.activeElement.isContentEditable
            );
            // Forward terminal-safe Ctrl combos (Ctrl+L/C/D/A/E/K/U) to xterm
            var isTermCtrl = e.ctrlKey && !e.metaKey && !e.altKey && /^[lcdaekuwz]$/i.test(e.key);
            if (!isEditable && e.key && ((e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) || isTermCtrl)) {
                var keyEntry = termCache.get(activeTermWorker);
                if (
                    keyEntry &&
                    keyEntry.ws &&
                    (
                        keyEntry.ws.readyState === WebSocket.CONNECTING ||
                        (keyEntry.ws.readyState === WebSocket.OPEN && !keyEntry.inputReady)
                    )
                ) {
                    keyEntry.pendingInput.push(e.key);
                    if (keyEntry.pendingInput.length > 256) {
                        keyEntry.pendingInput = keyEntry.pendingInput.slice(-256);
                    }
                    e.preventDefault();
                }
                try {
                    if (inlineTerm.textarea) inlineTerm.textarea.focus();
                    inlineTerm.focus();
                } catch (err) {}
                if (isTermCtrl) e.preventDefault();
            }
        }
        // When terminal is focused, block browser Ctrl+L/D (address bar / bookmark)
        if (isTermInputFocused()) {
            if (e.ctrlKey && !e.metaKey && !e.altKey && /^[ld]$/i.test(e.key)) {
                e.preventDefault();
            }
            return;
        }
        // Skip when terminal modal is open (xterm handles keys inside modal)
        if (document.getElementById('terminal-modal').style.display !== 'none') return;
        // Ctrl+Tab / Shift+Ctrl+Tab (works in standalone PWA mode)
        if (e.key === 'Tab' && e.ctrlKey) {
            e.preventDefault();
            cycleWorker(e.shiftKey ? -1 : 1);
            return;
        }
        // Alt+] / Alt+[ (works in browser tab mode)
        if (e.altKey && (e.key === ']' || e.key === '[')) {
            e.preventDefault();
            cycleWorker(e.key === ']' ? 1 : -1);
            return;
        }
    });

    // --- Keyboard shortcuts (Alt+letter) ---
    document.addEventListener('keydown', function(e) {
        if (!e.altKey) return;
        // When modal terminal is attached, let everything through to xterm
        if (document.getElementById('terminal-modal').style.display !== 'none') return;
        // When inline terminal is focused, let everything through
        if (isTermInputFocused()) return;

        switch (e.key.toLowerCase()) {
            case 'b': toggleDrones(); break;
            case 'a': continueAll(); break;
            case 'k': killWorker(); break;
            case 'r': reviveWorker(); break;
            case 'x': window.location.href = '/'; break;
            case 'n': showCreateTask(); break;
            case 'h': killSession(); break;
            default: return;
        }
        e.preventDefault();
    });

    // --- ? key opens keyboard shortcut help ---
    document.addEventListener('keydown', function(e) {
        if (e.key !== '?' || e.altKey || e.ctrlKey || e.metaKey) return;
        if (document.getElementById('terminal-modal').style.display !== 'none') return;
        if (isTermInputFocused()) return;
        var ae = document.activeElement;
        var tag = ae && ae.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        // The task editor's description is a contenteditable div — without
        // this guard the global ? would swallow the keystroke and pop the
        // shortcuts modal instead of letting the operator type '?'.
        if (ae && ae.isContentEditable) return;
        e.preventDefault();
        var m = document.getElementById('shortcuts-modal');
        m.style.display = m.style.display === 'none' ? 'flex' : 'none';
    });

    // --- Header clock (local time) ---
    function updateClock() {
        var el = document.getElementById('header-clock');
        if (el) {
            var now = new Date();
            el.textContent = now.toLocaleTimeString('en-US', { hour12: true });
        }
    }
    _trackedIntervals.push(setInterval(updateClock, 1000));
    updateClock();

    // Boot — if no WS token available, session may have expired
    if (!wsToken()) {
        window.location.href = '/login';
    }
    // Restore task search from localStorage
    (function() {
        var searchInput = document.getElementById('task-search');
        if (searchInput && activeSearchQuery) {
            searchInput.value = activeSearchQuery;
        }
    })();

    // --- Cleanup on page unload ---
    window.addEventListener('beforeunload', function() {
        // Close main WS
        if (ws) { try { ws.close(); } catch(e) {} ws = null; }
        // Clear all tracked intervals
        for (var i = 0; i < _trackedIntervals.length; i++) {
            clearInterval(_trackedIntervals[i]);
        }
        _trackedIntervals.length = 0;
        if (_restartRecoveryTimer) { clearTimeout(_restartRecoveryTimer); _restartRecoveryTimer = null; }
        // Clear queen cooldown timer
        if (queenCooldownTimer) { clearInterval(queenCooldownTimer); queenCooldownTimer = null; }
        // Clear term debug timer
        if (termDebugTimer) { clearInterval(termDebugTimer); termDebugTimer = null; }
        // Clear title flash timer
        if (titleFlashTimer) { clearInterval(titleFlashTimer); titleFlashTimer = null; }
        // Destroy all cached terminals
        termCache.forEach(function(_entry, name) {
            destroyTermEntry(name);
        });
    });

    function onAppFocus() {
        pageHidden = false;
        stopTitleFlash();
        // How long the tab was backgrounded. A long hide (mobile lock/switch)
        // very likely killed the socket at the OS level even though readyState
        // may still say OPEN, so force a fresh reconnect rather than trusting it.
        // A brief flap (desktop alt-tab) with a healthy socket takes the cheap
        // path and leaves the connection untouched.
        var hiddenMs = lastHiddenAt ? (Date.now() - lastHiddenAt) : 0;
        var staleAfterHide = hiddenMs > 2000;
        console.warn('[swarm-restart] app focus', {
            restarting: _restarting,
            mainWs: ws ? ws.readyState : 'none',
            hiddenMs: hiddenMs,
            activeTermWorker: activeTermWorker || null
        });
        if (staleAfterHide || !ws || ws.readyState !== WebSocket.OPEN) {
            forceReconnectMainWs();
        } else {
            ensureMainWsConnected();
        }
        if (_restarting) _restarting = false;
        // Catch up on any missed WS events while tab was hidden. Worker state
        // ('state' events) goes stale while a mobile tab is backgrounded (WS
        // closed/throttled), so re-sync the worker list/status on resume — the
        // reconnecting WS may land after this, and background polling is paused
        // while hidden, so without this the worker badges stay stale.
        refreshWorkers();
        refreshStatus();
        refreshTasks();
        refreshBuzzLog();
        // Re-sync PWA badge with current proposal count (not buzz count)
        var proposalBadge = document.getElementById('proposal-badge');
        var proposalCount = proposalBadge && proposalBadge.style.display !== 'none' ? parseInt(proposalBadge.textContent) || 0 : 0;
        updateAppBadge(proposalCount);
        var activeTab = document.querySelector('.tab-content.active');
        if (activeTab && activeTab.id === 'tab-buzz') {
            unreadNotifications = 0;
            var badge = document.getElementById('notif-badge');
            if (badge) badge.style.display = 'none';
        }
        // Reconnect terminal WS if it died while tab was hidden. Same zombie-
        // socket caveat as the main WS: after a long hide, readyState === OPEN
        // can't be trusted, so force a reconnect. Close the stale socket first —
        // connectTermEntryWs reassigns entry.ws without closing the old one, so
        // otherwise a live zombie's handlers would linger against this entry.
        if (activeTermWorker) {
            var focusEntry = termCache.get(activeTermWorker);
            if (focusEntry) {
                if (staleAfterHide || !focusEntry.ws || focusEntry.ws.readyState !== WebSocket.OPEN) {
                    console.warn('[swarm-restart] app focus reconnecting terminal', {
                        worker: activeTermWorker,
                        wsState: focusEntry.ws ? focusEntry.ws.readyState : 'none',
                        hiddenMs: hiddenMs
                    });
                    if (focusEntry.ws) {
                        try {
                            focusEntry.ws.onclose = null;
                            focusEntry.ws.onerror = null;
                            focusEntry.ws.close();
                        } catch (e) { /* already closing/closed */ }
                    }
                    focusEntry.reconnectAttempts = 0;
                    connectTermEntryWs(activeTermWorker, focusEntry);
                } else {
                    focusEntry.term.focus();
                }
            }
        }
        // Reset so a subsequent bare window.focus (no intervening hide) doesn't
        // re-trigger a force-reconnect on an already-healthy socket.
        lastHiddenAt = 0;
    }
    document.addEventListener('visibilitychange', function() {
        pageHidden = document.hidden;
        if (pageHidden) {
            lastHiddenAt = Date.now();
        } else {
            onAppFocus();
        }
    });
    window.addEventListener('focus', onAppFocus);

    updateNotifButton();
    updateAppBadge(0);
    connect();
    refreshPipelines();

    // Auto-open launch modal on cold start (zero workers)
    if (_workerCount === 0) showLaunch();

    // Restore selected worker on page load (e.g. navigating back from Config)
    if (selectedWorker) {
        (function restoreWorker() {
            if (typeof Terminal === 'undefined') {
                setTimeout(restoreWorker, 50);
                return;
            }
            var item = document.querySelector('.worker-item[data-worker="' + selectedWorker + '"]');
            if (item) selectWorker(selectedWorker);
            // Defer _pageReady until after the current microtask queue drains,
            // so syncTermAliases() has set inlineTerm before refreshDetail() can fire
            requestAnimationFrame(function() { _pageReady = true; });
        })();
    } else {
        _pageReady = true;
    }

    // Before swap: save scroll position and whether user was pinned to bottom
    document.body.addEventListener('htmx:beforeSwap', function(e) {
        var target = e.detail.target;
        if (target) {
            target._savedScrollTop = target.scrollTop;
            // Consider "at bottom" if within 30px of the end
            target._wasAtBottom = (target.scrollHeight - target.scrollTop - target.clientHeight) < 30;
        }
    });

    // Re-select worker after HTMX swaps the worker list
    document.body.addEventListener('htmx:afterSwap', function(e) {
        if (e.detail.target.id === 'worker-list') {
            if (selectedWorker) {
                var taskText = '';
                var foundWorker = false;
                document.querySelectorAll('.worker-item').forEach(function(el) {
                    el.classList.toggle('selected', el.dataset.worker === selectedWorker);
                    if (el.dataset.worker === selectedWorker) {
                        foundWorker = true;
                        var taskEl = el.querySelector('.worker-task');
                        if (taskEl) taskText = taskEl.textContent.trim();
                    }
                });
                if (foundWorker) {
                    var detailEntry = termCache.get(selectedWorker);
                    updateDetailTitleWithTermTitle(selectedWorker, detailEntry);
                    document.getElementById('terminal-actions').style.display = 'flex';
                } else {
                    selectedWorker = null;
                    try { sessionStorage.removeItem('swarm_selected_worker'); } catch(e2) {}
                }
            }
            // On mobile, sort worker pills: active states first
            if (window.innerWidth <= 768) {
                var wlBody = document.querySelector('.worker-list > .panel-body');
                if (wlBody) {
                    var items = Array.from(wlBody.querySelectorAll('.worker-item'));
                    var stateOrder = { BUZZING: 0, WAITING: 1, RESTING: 2, SLEEPING: 3, STUNG: 4 };
                    items.sort(function(a, b) {
                        var sa = stateOrder[a.dataset.state] !== undefined ? stateOrder[a.dataset.state] : 9;
                        var sb = stateOrder[b.dataset.state] !== undefined ? stateOrder[b.dataset.state] : 9;
                        return sa - sb;
                    });
                    items.forEach(function(el) { wlBody.appendChild(el); });
                }
            }
            updateBulkWorkerButtons();
            // Re-apply state filter after swap
            var search = document.getElementById('worker-search');
            if (activeWorkerStateFilter !== 'all' || (search && search.value)) {
                filterWorkers(search ? search.value : '');
            }
        }
        // Update task summary after task list swap
        if (e.detail.target.id === 'task-list') {
            const summary = e.detail.target.querySelector('[data-summary]');
            if (summary) {
                document.getElementById('task-summary').textContent = summary.dataset.summary;
            }
            // Highlight tasks assigned to selected worker
            if (selectedWorker) {
                document.querySelectorAll('.task-item').forEach(function(el) {
                    el.classList.toggle('assigned-to-selected', el.dataset.worker === selectedWorker);
                });
            }
        }
        // Restore scroll position after swap — stick to bottom if user was already there
        if (e.detail.target._savedScrollTop !== undefined) {
            if (e.detail.target._wasAtBottom) {
                e.detail.target.scrollTop = e.detail.target.scrollHeight;
            } else {
                e.detail.target.scrollTop = e.detail.target._savedScrollTop;
            }
            delete e.detail.target._savedScrollTop;
            delete e.detail.target._wasAtBottom;
        }
        // Convert UTC server timestamps to browser-local time
        formatLocalTimes(e.detail.target);
    });

    // Resize handler — only resize the active terminal
    // Window resize → container dimensions change → ResizeObserver fires → fit() + sendResizeIfChanged()

    // Escape key closes modals (priority order: topmost first)
    document.addEventListener('keydown', function(e) {
        if (e.key !== 'Escape') return;
        // Skip if inline terminal textarea is focused
        if (isTermInputFocused()) return;

        // Close resource popover first (lightweight, not a modal)
        var resPop = document.getElementById('resource-popover');
        if (resPop && resPop.style.display !== 'none') { closeResourcePopover(); return; }
        // Shortcuts help modal
        var shortcutsEl = document.getElementById('shortcuts-modal');
        if (shortcutsEl && shortcutsEl.style.display !== 'none') { shortcutsEl.style.display = 'none'; return; }
        var onboardEl = document.getElementById('onboarding-modal');
        if (onboardEl && onboardEl.style.display !== 'none') { onboardEl.style.display = 'none'; return; }
        // Check modals in priority order, close first visible one
        var decisionEl = document.getElementById('decision-modal');
        if (decisionEl && decisionEl.style.display !== 'none') { hideDecisionModal(); return; }
        var confirmEl = document.getElementById('confirm-modal');
        if (confirmEl && confirmEl.style.display !== 'none') { hideConfirm(); return; }
        var taskEl = document.getElementById('task-modal');
        if (taskEl && taskEl.style.display !== 'none') { closeTaskModal(); return; }
        var broadcastEl = document.getElementById('broadcast-modal');
        if (broadcastEl && broadcastEl.style.display !== 'none') { hideBroadcast(); return; }
        var queenEl = document.getElementById('queen-modal');
        if (queenEl && queenEl.style.display !== 'none') { hideQueen(); return; }
        var launchEl = document.getElementById('launch-modal');
        if (launchEl && launchEl.style.display !== 'none') { hideLaunch(); return; }
        var spawnEl = document.getElementById('spawn-modal');
        if (spawnEl && spawnEl.style.display !== 'none') { hideSpawn(); return; }
        var editEl = document.getElementById('edit-worker-modal');
        if (editEl && editEl.style.display !== 'none') { hideEditWorker(); return; }
        var tunnelEl = document.getElementById('tunnel-modal');
        if (tunnelEl && tunnelEl.style.display !== 'none') { hideTunnel(); return; }
        var shutdownEl = document.getElementById('shutdown-modal');
        if (shutdownEl && shutdownEl.style.display !== 'none') { hideShutdown(); return; }
    });

    // --- Resizable split ---
    ;(function() {
        const handle = document.getElementById('resize-handle');
        if (!handle) return;
        const area = handle.parentElement; // .detail-area
        let dragging = false;
        let startY = 0;
        let startTopFr = 0.5;

        // Restore the saved split. Exposed because show() clears the
        // detail-area gridTemplateRows on every return to the Command
        // Center — without re-applying this, the task/bottom panel
        // forgets its position (every other panel persists because
        // show() re-applies its size; this one had no re-apply).
        function applySavedSplit() {
            const saved = localStorage.getItem('swarm-split');
            if (!saved) return;
            const ratio = parseFloat(saved);
            if (ratio > 0.15 && ratio < 0.85) {
                area.style.gridTemplateRows = ratio + 'fr auto ' + (1 - ratio) + 'fr';
            }
        }
        applySavedSplit();
        window.__applySavedSplit = applySavedSplit;

        function startDrag(clientY) {
            dragging = true;
            startY = clientY;
            const rect = area.getBoundingClientRect();
            startTopFr = (area.children[0].getBoundingClientRect().height) / rect.height;
            handle.classList.add('dragging');
            document.body.style.cursor = 'row-resize';
            document.body.style.userSelect = 'none';
        }

        function moveDrag(clientY) {
            if (!dragging) return;
            const rect = area.getBoundingClientRect();
            const dy = clientY - rect.top;
            let ratio = dy / rect.height;
            ratio = Math.max(0.15, Math.min(0.85, ratio));
            area.style.gridTemplateRows = ratio + 'fr auto ' + (1 - ratio) + 'fr';
            // Fit inline terminal during drag (visual only, no WS resize flood)
            if (activeTermWorker) {
                var dragEntry = termCache.get(activeTermWorker);
                if (dragEntry && dragEntry.fitAddon && dragEntry.term) {
                    dragEntry.fitAddon.fit();
                }
            }
        }

        function endDrag() {
            if (!dragging) return;
            dragging = false;
            handle.classList.remove('dragging');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            // Persist
            const rect = area.getBoundingClientRect();
            const topH = area.children[0].getBoundingClientRect().height;
            localStorage.setItem('swarm-split', (topH / rect.height).toFixed(3));
            // Send final resize to inline terminal after drag ends
            if (activeTermWorker) {
                var endEntry = termCache.get(activeTermWorker);
                if (endEntry && endEntry.fitAddon && endEntry.term) {
                    endEntry.fitAddon.fit();
                    sendResizeIfChanged(activeTermWorker, endEntry);
                }
            }
        }

        // Mouse events (desktop)
        handle.addEventListener('mousedown', function(e) {
            e.preventDefault();
            startDrag(e.clientY);
        });
        document.addEventListener('mousemove', function(e) { moveDrag(e.clientY); });
        document.addEventListener('mouseup', endDrag);

        // Touch events (mobile)
        handle.addEventListener('touchstart', function(e) {
            e.preventDefault();
            startDrag(e.touches[0].clientY);
        }, { passive: false });
        document.addEventListener('touchmove', function(e) {
            if (!dragging) return;
            e.preventDefault();
            moveDrag(e.touches[0].clientY);
        }, { passive: false });
        document.addEventListener('touchend', endDrag);
    })();

    // --- Worker drag-and-drop reordering ---
    ;(function() {
        var workerList = document.getElementById('worker-list');
        if (!workerList) return;
        var dragName = null;

        function isMobile() { return window.innerWidth < 900; }

        workerList.addEventListener('dragstart', function(e) {
            if (isMobile()) { e.preventDefault(); return; }
            var item = e.target.closest('.worker-item');
            if (!item) return;
            dragName = item.dataset.worker;
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', dragName);
            requestAnimationFrame(function() { item.classList.add('dragging'); });
        });

        workerList.addEventListener('dragover', function(e) {
            if (!dragName) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            var item = e.target.closest('.worker-item');
            if (!item || item.dataset.worker === dragName) return;
            // Clear previous indicators
            workerList.querySelectorAll('.drag-over-top, .drag-over-bottom').forEach(function(el) {
                el.classList.remove('drag-over-top', 'drag-over-bottom');
            });
            var rect = item.getBoundingClientRect();
            var mid = rect.top + rect.height / 2;
            if (e.clientY < mid) {
                item.classList.add('drag-over-top');
            } else {
                item.classList.add('drag-over-bottom');
            }
        });

        workerList.addEventListener('dragleave', function(e) {
            var item = e.target.closest('.worker-item');
            if (item) item.classList.remove('drag-over-top', 'drag-over-bottom');
        });

        workerList.addEventListener('drop', function(e) {
            e.preventDefault();
            if (!dragName) return;
            var target = e.target.closest('.worker-item');
            if (!target || target.dataset.worker === dragName) { clearDragClasses(); return; }
            var dragged = workerList.querySelector('.worker-item[data-worker="' + dragName + '"]');
            if (!dragged) { clearDragClasses(); return; }
            // Determine insert position
            var rect = target.getBoundingClientRect();
            var before = e.clientY < rect.top + rect.height / 2;
            if (before) {
                workerList.insertBefore(dragged, target);
            } else {
                workerList.insertBefore(dragged, target.nextSibling);
            }
            clearDragClasses();
            // Persist new order
            var order = [];
            workerList.querySelectorAll('.worker-item[data-worker]').forEach(function(el) {
                order.push(el.dataset.worker);
            });
            _reorderInFlight = true;
            fetch('/api/workers/reorder', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                body: JSON.stringify({order: order})
            }).then(async function(r) {
                _reorderInFlight = false;
                // Phase 9 of #328: surface success/failure to the
                // operator on drag-drop reorder.  Pre-fix the save
                // was silent — operator had to refresh the page to
                // see if the new order stuck.
                if (r.ok) {
                    showToast('Worker order saved');
                } else {
                    try {
                        var data = await r.json();
                        showToast('Reorder failed: ' + (data.error || 'unknown'), true);
                    } catch (_) {
                        showToast('Reorder failed (' + r.status + ')', true);
                    }
                }
                refreshWorkers();
            }).catch(function(err) {
                _reorderInFlight = false;
                showToast('Reorder failed: ' + (err && err.message || 'request failed'), true);
                refreshWorkers();
            });
        });

        workerList.addEventListener('dragend', function() {
            clearDragClasses();
            dragName = null;
        });

        function clearDragClasses() {
            workerList.querySelectorAll('.dragging, .drag-over-top, .drag-over-bottom').forEach(function(el) {
                el.classList.remove('dragging', 'drag-over-top', 'drag-over-bottom');
            });
        }
    })();

    // ----- Web Share Target landing (2026-05-21) -----
    //
    // When the PWA is installed and the operator selects "Swarm" from
    // the iOS / Android share sheet, the server captures the payload
    // at POST /share-receive and 303-redirects to /?share=<id>. This
    // block detects that query param on load, fetches the stashed
    // payload, and opens the New Task modal with the shared file as
    // a pre-attached image + the shared text/title as description.
    // Empties the query string after pickup so refresh doesn't
    // re-trigger.
    (function checkShareIntent() {
        var match = window.location.search.match(/[?&]share=([A-Za-z0-9]+)/);
        if (!match) return;
        var shareId = match[1];
        // Strip the share param from the URL so a refresh doesn't
        // try to claim an already-consumed share.
        try {
            var url = new URL(window.location.href);
            url.searchParams.delete('share');
            window.history.replaceState({}, '', url.pathname + (url.search || '') + url.hash);
        } catch (_) {}
        // Wait one tick so the modal helpers (openTaskModal, addThumbnail,
        // taskModalAttachmentPaths) are definitely defined.
        setTimeout(function () {
            fetch('/share/' + encodeURIComponent(shareId), {
                headers: { 'X-Requested-With': 'Dashboard' },
            })
                .then(function (r) {
                    if (!r.ok) throw new Error('share not found');
                    return r.json();
                })
                .then(function (share) {
                    var files = share.files || [];
                    // Operator preference (2026-05-21): screenshots from
                    // the share sheet ALWAYS route to the Queen. The
                    // earlier heuristic (last-active-worker from
                    // localStorage) was a guessing game — wrong often
                    // enough that the operator had to re-route by hand.
                    // Queen is the unambiguous front door: she can ask
                    // for context and forward to the right worker via
                    // queen_prompt_worker / swarm_send_message.
                    //
                    // Claude Code parses [/abs/path/to/file.png] tokens
                    // as image attachments, so _shareSendToWorker types
                    // the file path(s) wrapped in brackets into the
                    // Queen's PTY without pressing Enter — operator
                    // adds the "send to X" instruction and submits.
                    //
                    // Falls back to the New Task modal only when the
                    // share has no file attachments (text/url shares).
                    if (files.length) {
                        _shareSendToWorker('queen', files, share);
                    } else {
                        _shareOpenTaskModal(share);
                    }
                })
                .catch(function () {
                    showToast('Share expired or already consumed', true);
                });
        }, 250);
    })();

    // Route a share into the active worker's PTY. The message is the
    // bracketed file path(s) + any shared text/url. Claude Code reads
    // each [/abs/path] token as an image attachment.
    //
    // `enter: false` so the path lands in the worker's input buffer
    // WITHOUT being submitted. Operator follow-up (2026-05-21): the
    // first iteration of this flow auto-pressed Enter, which on
    // mobile shipped a half-thought message before the operator
    // could add context. Now the path types in, operator reviews,
    // adds prose, hits Enter themselves.
    //
    // Also drop the shared URL when it points at the dashboard host —
    // the OS share sheet auto-attaches the page URL when you share
    // FROM the PWA itself, and that's noise next to a path.
    function _shareSendToWorker(workerName, files, share) {
        var parts = files.map(function (p) { return '[' + p + ']'; });
        if (share.title) parts.push(share.title);
        if (share.text) parts.push(share.text);
        if (share.url) {
            var u = share.url;
            var selfUrl = false;
            try {
                var parsed = new URL(u);
                selfUrl = parsed.host === window.location.host;
            } catch (_) {}
            if (!selfUrl) parts.push(u);
        }
        var message = parts.join(' ').trim();
        fetch('/api/workers/' + encodeURIComponent(workerName) + '/send', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'Dashboard',
            },
            body: JSON.stringify({ message: message, enter: false }),
        })
            .then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function () {
                showToast('Attached ' + files.length + ' to ' + workerName + ' — add context + press Enter');
                // Switch focus to the worker so the operator sees the
                // path in the input buffer and can type alongside it.
                if (typeof selectWorker === 'function') selectWorker(workerName);
            })
            .catch(function (e) {
                // Network / 404 / state-change — drop back to the task
                // modal so the share isn't lost.
                showToast('Send to ' + workerName + ' failed: ' + (e.message || 'error') + ' — opening task modal', true);
                _shareOpenTaskModal(share);
            });
    }

    function _shareOpenTaskModal(share) {
        var files = share.files || [];
        var names = share.filenames || [];
        var titleParts = [];
        if (share.title) titleParts.push(share.title);
        if (!titleParts.length && names.length) titleParts.push(names[0]);
        if (!titleParts.length) titleParts.push('Shared from mobile');
        var descParts = [];
        if (share.text) descParts.push(share.text);
        if (share.url) descParts.push(share.url);
        if (files.length) {
            descParts.push('\n[Shared from mobile — ' + files.length + ' attachment(s)]');
        }
        openTaskModal('create', {
            title: titleParts.join(' — ').slice(0, 200),
            desc: descParts.join('\n\n'),
        });
        taskModalAttachmentPaths = files.slice();
        files.forEach(function (p) {
            try { addThumbnail(p); } catch (_) {}
        });
        // Pre-select the Queen as the assignee in the fallback path so
        // it matches the share default — see checkShareIntent above for
        // why screenshots/files always go to her. Operator can still
        // change the dropdown before creating the task.
        try {
            var sel = document.getElementById('tm-worker');
            if (sel) {
                setTimeout(function () {
                    for (var i = 0; i < sel.options.length; i++) {
                        if (sel.options[i].value === 'queen') {
                            sel.selectedIndex = i;
                            break;
                        }
                    }
                }, 300);
            }
        } catch (_) {}
        showToast('Shared content ready — review and create');
    }

})();

// ============================================================================
// Command Center — operator landing surface.
//
// Self-contained IIFE so it can append cleanly without weaving into the main
// dashboard module. Hooks into `window.selectWorker` to hide itself when a
// worker is focused. Polls /api/attention, /api/events, /api/workers,
// /api/queen/threads on a slow interval; renders into the panels declared in
// dashboard.html.
// ============================================================================
(function () {
    if (window.__commandCenterMounted) return;
    window.__commandCenterMounted = true;

    var lastAttentionCount = 0;

    var POLL_INTERVAL_MS = 15000;
    var DIGEST_INTERVAL_MS = 60000;

    function el(id) { return document.getElementById(id); }

    function escapeHtml(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function fmtTime(ts) {
        var d = new Date(ts * 1000);
        return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    }

    function fmtAgo(ts) {
        var diff = Math.max(0, Date.now() / 1000 - ts);
        if (diff < 60) return Math.round(diff) + 's';
        if (diff < 3600) return Math.round(diff / 60) + 'm';
        if (diff < 86400) return Math.round(diff / 3600) + 'h';
        return Math.round(diff / 86400) + 'd';
    }

    function fetchJSON(url, opts) {
        return fetch(url, opts || {}).then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        });
    }

    function ccPost(url, body) {
        // Wrapper that sets the headers the swarm CSRF middleware
        // requires (X-Requested-With) plus JSON content type. Without
        // X-Requested-With the middleware silently returns 403.
        return fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'Dashboard',
            },
            body: body == null ? '{}' : JSON.stringify(body),
        });
    }

    // ----- Visibility ------------------------------------------------------
    function bottomPanel() {
        return document.querySelector('.panel.bottom-tabbed');
    }
    function detailArea() {
        return document.querySelector('.detail-area');
    }
    function resizeHandle() {
        return document.getElementById('resize-handle');
    }

    function show() {
        // Command Center: bottom tab panel visible underneath. Clear any
        // inline grid-template-rows so the CSS default (`1fr auto 1fr`,
        // equal split) takes over. We don't restore prior drag state —
        // it was leaving the bottom panel dominant after worker visits.
        // Panel visibility is CSS-driven off body.cc-active (added below) —
        // do NOT set inline display on #command-center / #detail-body here,
        // or it desyncs from the class and reintroduces the mixed-render bug.
        var bottom = bottomPanel();
        if (bottom) bottom.style.display = '';
        var rh = resizeHandle();
        if (rh) rh.style.display = '';
        var da = detailArea();
        if (da) {
            // Clear first (drops any stale inline value a worker visit's
            // hide() left behind), then re-apply the operator's persisted
            // split from storage so the task panel keeps its position.
            da.style.gridTemplateRows = '';
            if (window.__applySavedSplit) window.__applySavedSplit();
        }
        // Re-apply saved CC panel sizes (defensive — survives any
        // intermediate CSS-var clear during a worker visit).
        applyCcLayoutFromStorage();

        // Detach any active worker terminal FIRST. showTermEntry leaves an
        // inline `display: flex` on #detail-body; hideActiveTermEntry removes
        // the worker term from the DOM and clears those inline styles, so the
        // panel returns to pure CSS (body.cc-active) control. Without this the
        // worker terminal stays stacked over the Queen panel. Re-selecting the
        // worker re-mounts it from termCache.
        try { if (window.hideActiveTermEntry) window.hideActiveTermEntry(); } catch (_) {}

        // Mount the Queen's live PTY into the right panel (her real
        // session — the chat relay is gone).
        try { if (window.mountQueenEmbed) window.mountQueenEmbed(); } catch (_) {}

        // Mark body in CC mode so CSS can mute the stale
        // `.worker-item.selected` cue that the existing dashboard keeps
        // applied (its `selectedWorker` internal var is closure-private).
        document.body.classList.add('cc-active');
        document.querySelectorAll('.worker-item.selected, .queen-card.selected').forEach(function (el) {
            el.classList.remove('selected');
        });
        // Clear the persisted worker selection so a reload doesn't
        // restore a stale focus, and emit a "no focused worker" WS
        // signal so the backend knows the operator is on the dashboard.
        try { sessionStorage.removeItem('swarm_selected_worker'); } catch (_) {}
        try {
            if (window.ws && window.ws.readyState === 1) {
                window.ws.send(JSON.stringify({ command: 'focus', worker: '' }));
            }
        } catch (_) {}
        // Worker-list re-renders (HTMX, periodic refresh) will reapply
        // `.selected` on whatever the backend thinks is focused. The
        // CSS rule under `body.cc-active` already mutes the visual, but
        // also reactively strip the class so other JS that reads it
        // (Ctrl+Tab cycling start point, etc.) sees a clean state.
        attachWorkerListObserver();

        // Detail-title in CC mode = Queen status strip (not "Select a
        // worker" or a worker name). Hide the title text + worker action
        // bar; show the status strip and refresh its data.
        var dtxt = el('detail-title-text');
        if (dtxt) dtxt.style.display = 'none';
        var actions = el('terminal-actions');
        if (actions) actions.style.display = 'none';
        var strip = el('cc-queen-strip');
        if (strip) strip.classList.add('cc-qs-visible');
        loadQueenStatusStrip();

        // Bottom tab content depends on flex sizing recalculation after
        // the grid-template-rows change. Stage multiple resize dispatches
        // (immediate, RAF×2, 50ms, 200ms) so flex children re-measure
        // after the browser has actually laid out the new grid.
        triggerResizeStaged();
    }

    function hide() {
        // Worker focused: terminal fills the detail-area. Collapse the
        // bottom grid row (and the resize handle) so no whitespace
        // remains where the bottom panel used to sit.
        // Detach the Queen embed first so the shared terminal container
        // is free to move into #detail-body if SHE is the focused worker.
        try { if (window.unmountQueenEmbed) window.unmountQueenEmbed(); } catch (_) {}
        // Panel visibility is CSS-driven off body.cc-active (removed below) —
        // do NOT set inline display on #command-center / #detail-body here.
        var bottom = bottomPanel();
        if (bottom) bottom.style.display = 'none';
        var rh = resizeHandle();
        if (rh) rh.style.display = 'none';
        var da = detailArea();
        if (da) da.style.gridTemplateRows = '1fr 0 0';
        // Restore worker-view chrome: title text visible, queen strip hidden.
        // (The existing selectWorker flow updates detail-title-text content.)
        var dtxt = el('detail-title-text');
        if (dtxt) dtxt.style.display = '';
        var strip = el('cc-queen-strip');
        if (strip) strip.classList.remove('cc-qs-visible');

        document.body.classList.remove('cc-active');

        // xterm.js / any flex-sized terminal child must refit after the
        // grid-template-rows change collapsed the bottom row. Race
        // window: the resize event is synchronous but the browser may
        // not have laid out the new grid before xterm reads its
        // container width — fire multiple times so at least one catches
        // a fully-laid-out container.
        triggerResizeStaged();

        // Dashboard→worker transitions hit a known xterm.js issue:
        // xterm's buffer stored the previous content at the narrow
        // (display:none → 0-width) layout, so even after fit() the
        // old lines stay wrapped at the wrong width. A hard reconnect
        // recreates xterm fresh, which reads the current PTY screen at
        // the correct dimensions. Scrollback is sacrificed; correct
        // rendering wins. Fires after the grid + RAF + fit cycle has
        // had time to settle.
        setTimeout(function () {
            if (typeof window.ccHardReconnectActiveTerm === 'function') {
                window.ccHardReconnectActiveTerm();
            }
        }, 300);
    }

    function triggerResizeStaged() {
        function fire() {
            try {
                window.dispatchEvent(new Event('resize'));
            } catch (_) {
                try {
                    var evt = document.createEvent('Event');
                    evt.initEvent('resize', true, true);
                    window.dispatchEvent(evt);
                } catch (_2) {}
            }
            // xterm's fitAddon doesn't listen for window.resize — it hooks
            // the terminal container's ResizeObserver. Call the explicit
            // refit entry point exposed by the main IIFE.
            try {
                if (typeof window.ccRefitActiveTerm === 'function') {
                    window.ccRefitActiveTerm();
                }
            } catch (_3) {}
        }
        fire(); // immediate
        if (window.requestAnimationFrame) {
            window.requestAnimationFrame(function () {
                fire();
                window.requestAnimationFrame(fire);
            });
        }
        setTimeout(fire, 50);
        setTimeout(fire, 200);
        setTimeout(fire, 500);
    }

    // ----- Worker-list mutation observer ----------------------------------
    // Strip `.selected` from worker rows whenever the sidebar re-renders
    // while CC is active. Idempotent — re-uses the same observer instance.
    var _workerListObserver = null;
    function attachWorkerListObserver() {
        if (_workerListObserver) return;
        var host = el('worker-list');
        if (!host) return;
        _workerListObserver = new MutationObserver(function () {
            if (!document.body.classList.contains('cc-active')) return;
            host.querySelectorAll('.worker-item.selected, .queen-card.selected').forEach(function (n) {
                n.classList.remove('selected');
            });
        });
        _workerListObserver.observe(host, { childList: true, subtree: true });
    }

    // ----- Detail-body content observer -----------------------------------
    // When xterm.js (or anything else) mounts content into detail-body
    // *after* a layout change, the terminal might have measured itself
    // at a stale size. Re-fire staged resizes on each significant
    // mutation so xterm refits to the actual container width. Only
    // active when CC is NOT visible (worker view).
    var _detailBodyObserver = null;
    function attachDetailBodyObserver() {
        if (_detailBodyObserver) return;
        var host = el('detail-body');
        if (!host) return;
        _detailBodyObserver = new MutationObserver(function (mutations) {
            if (document.body.classList.contains('cc-active')) return;
            // Skip trivial text-only mutations to avoid spamming resizes.
            var hasStructural = mutations.some(function (m) { return m.addedNodes.length > 0; });
            if (!hasStructural) return;
            triggerResizeStaged();
        });
        _detailBodyObserver.observe(host, { childList: true, subtree: false });
    }

    // ----- Queen status strip ---------------------------------------------
    function loadQueenStatusStrip() {
        var midnight = new Date();
        midnight.setHours(0, 0, 0, 0);
        var hourAgo = (Date.now() - 3600000) / 1000;
        var sinceTs = midnight.getTime() / 1000;
        Promise.all([
            fetchJSON('/api/queen/queue').catch(function () { return null; }),
            fetchJSON('/api/queen/health').catch(function () { return null; }),
            // Decision counts: pull queen-category events from /api/events
            // and count those since midnight and in the last hour.
            fetchJSON('/api/events?categories=queen&limit=500').catch(function () { return null; }),
        ]).then(function (results) {
            renderQueenStatusStrip(results[0], results[1], results[2], sinceTs, hourAgo);
        });
    }

    function renderQueenStatusStrip(queue, health, eventsResp, sinceTs, hourAgo) {
        var queueEl = el('cc-qs-queue');
        var lastHrEl = el('cc-qs-last-hr');
        var todayEl = el('cc-qs-today');
        var usageEl = el('cc-qs-usage');

        if (queueEl) {
            if (queue) {
                var running = queue.running != null ? queue.running : 0;
                var depth = queue.total != null ? queue.total : (queue.queued || 0);
                queueEl.textContent = running + '/' + depth;
            } else {
                queueEl.textContent = '—';
            }
        }

        var events = (eventsResp && eventsResp.events) || [];
        var inHour = 0;
        var today = 0;
        for (var i = 0; i < events.length; i++) {
            var ts = events[i].ts;
            if (ts >= hourAgo) inHour++;
            if (ts >= sinceTs) today++;
        }
        if (lastHrEl) lastHrEl.textContent = String(inHour);
        if (todayEl) todayEl.textContent = String(today);

        if (usageEl) {
            var pct = health && health.usage_5hr_pct != null ? health.usage_5hr_pct : 0;
            usageEl.textContent = Math.round(pct * 100) + '%';
        }

        // Keep the Ask Queen panel's inline health dot in sync (initial render
        // + the slow poll; the queen.health WS event covers live updates).
        updateQueenHealthIndicator(health && health.state);
    }

    // Patch the public selectWorker (used by /api/workers callsites and
    // a handful of legacy callers). Most worker clicks inside the main
    // IIFE call a local `selectWorker` that doesn't go through `window`,
    // so we ALSO add a capture-phase click listener below that fires
    // before the internal dispatcher to toggle CC visibility correctly.
    var _origSelectWorker = window.selectWorker;
    window.selectWorker = function (name) {
        if (name === 'queen') {
            show();
            return;
        }
        if (name) hide();
        if (_origSelectWorker) return _origSelectWorker(name);
    };

    // Open the Queen's live PTY full-screen (the embedded panel's ⛶
    // button). We deliberately keep the queen-card → Command Center
    // bounce (it is the ONLY navigation back to the CC), so full-screen
    // goes through the pre-override real worker selector instead. The
    // same single cached "queen" entry is moved into #detail-body; click
    // the Queen card again to return to the CC (re-embeds her).
    function ccQueenFullscreen() {
        try { if (window.unmountQueenEmbed) window.unmountQueenEmbed(); } catch (_) {}
        hide();
        if (_origSelectWorker) _origSelectWorker('queen');
    }
    window.ccQueenFullscreen = ccQueenFullscreen;

    // Capture-phase listener on worker-item / queen-card clicks. This
    // runs before any internal handler so CC state toggles whether the
    // click goes through window.selectWorker or the IIFE-internal
    // selectWorker. The queen-card uses class `.queen-card` (not id),
    // so the selector must match by class.
    document.addEventListener('click', function (e) {
        var item = e.target.closest('.worker-item[data-worker], .queen-card[data-worker]');
        if (!item) return;
        var name = item.dataset.worker;
        if (name === 'queen') {
            // Queen card → Command Center (her live PTY is embedded in
            // the CC right panel; the card is also the ONLY nav back to
            // the CC). Full-screen is the panel's ⛶ button instead.
            // Clear .selected on all worker items so any background
            // sync logic doesn't treat a stale selection as current.
            e.stopPropagation();
            e.preventDefault();
            document.querySelectorAll('.worker-item.selected').forEach(function (el) {
                el.classList.remove('selected');
            });
            show();
        } else if (name) {
            hide();
        }
    }, true);

    // ----- Attention queue ------------------------------------------------
    var ccHandledOpen = false;

    function loadAttention() {
        return fetchJSON('/api/attention').then(function (r) {
            renderAttention(r || {});
        }).catch(function () {});
    }

    // Build one exception card. Buttons are driven by item.actions tokens
    // so the backend decides which verbs are valid per exception type.
    function ccCard(item) {
        var ago = fmtAgo(item.updated_at);
        var ref = escapeHtml(item.ref_id || '');
        var worker = escapeHtml(item.worker_name || '');
        var sev = escapeHtml(item.severity || 'decision');
        var hasReply = (item.actions || []).indexOf('reply') >= 0;
        var btns = (item.actions || []).map(function (a) {
            if (a === 'reply') return '<button class="btn btn-sm" data-action="ccReplyStart" data-thread-id="' + ref + '">Reply</button>';
            if (a === 'dismiss') return '<button class="btn btn-sm btn-secondary" data-action="ccDismissAttention" data-thread-id="' + ref + '">Dismiss</button>';
            if (a === 'focus') return '<button class="btn btn-sm btn-secondary" data-action="ccFocusLive" data-worker="' + worker + '">Open terminal</button>';
            if (a === 'revive') return '<button class="btn btn-sm" data-action="ccRevive" data-worker="' + worker + '">Revive</button>';
            if (a === 'force_rest') return '<button class="btn btn-sm btn-secondary" data-action="ccForceRest" data-worker="' + worker + '">Force rest</button>';
            if (a === 'approve') return '<button class="btn btn-sm" data-action="ccApproveProposal" data-proposal-id="' + ref + '">Approve</button>';
            if (a === 'reject') return '<button class="btn btn-sm btn-danger" data-action="ccRejectProposal" data-proposal-id="' + ref + '">Dismiss</button>';
            if (a === 'resources') return '<button class="btn btn-sm btn-secondary" data-action="ccGotoResources">View resources</button>';
            return '';
        }).join('');
        var replyBox = hasReply
            ? '<div class="cc-attention-reply" data-reply-for="' + ref + '" style="display:none">'
                + '<input type="text" placeholder="Reply to ' + (worker || 'worker') + '..." data-cc-reply-input="' + ref + '">'
                + '<button class="btn btn-sm" data-action="ccReplySendBtn" data-thread-id="' + ref + '">Send</button>'
                + '</div>'
            : '';
        // The worker's own choice-prompt options, so the operator answers
        // the actual question inline instead of "Open terminal" + typing.
        var optBtns = (item.options || []).map(function (o) {
            return '<button class="btn btn-sm cc-choice-btn" data-action="ccChoose"'
                + ' data-worker="' + worker + '" data-choice="' + escapeHtml(String(o.value)) + '">'
                + escapeHtml(String(o.value)) + '. ' + escapeHtml(o.label || '') + '</button>';
        }).join('');
        var optsRow = optBtns
            ? '<div class="cc-attention-card-options">' + optBtns + '</div>'
            : '';
        return '<div class="cc-attention-card cc-sev-' + sev + ' cc-kind-' + escapeHtml(item.kind) + '" data-thread-id="' + ref + '">'
            + '<div class="cc-attention-card-head">'
            + '<span class="cc-attention-card-title">' + escapeHtml(item.title || '(no title)') + '</span>'
            + '<span class="cc-attention-card-meta">' + (worker || escapeHtml(item.kind)) + ' · ' + ago + '</span>'
            + '</div>'
            + (item.detail ? '<div class="cc-attention-card-detail">' + escapeHtml(item.detail) + '</div>' : '')
            + optsRow
            + '<div class="cc-attention-card-actions">' + btns + '</div>'
            + replyBox
            + '</div>';
    }

    function renderAttention(data) {
        var list = el('cc-attention-list');
        var badge = el('cc-attention-count');
        var crit = (data && data.critical) || [];
        var dec = (data && data.decision) || [];
        var handled = (data && data.handled) || { count: 0, items: [] };
        var actionable = crit.length + dec.length;
        if (badge) {
            badge.textContent = actionable ? String(actionable) : '';
            badge.setAttribute('data-count', String(actionable));
        }
        // P5: mirror the count into the mobile focus toggle button so a
        // phone operator sees pending-attention without having to switch
        // panels first.
        var mobileBadge = el('cc-mobile-focus-att-count');
        if (mobileBadge) mobileBadge.textContent = actionable ? '(' + actionable + ')' : '';
        if (!list) return;
        // Preserve in-progress operator state: if any reply box is open
        // OR focus is inside the attention list (typing), skip this
        // re-render so we don't wipe the input. The next poll cycle
        // will catch up after the operator submits or dismisses.
        if (isAttentionBusy(list)) return;
        // Top: the exception queue (scrolls). Bottom: a pinned region for
        // what the Queen is already addressing — anchored to the bottom
        // third so an empty queue doesn't leave a big void above it.
        var queue = '';
        if (crit.length) {
            queue += '<div class="cc-attention-section cc-sec-critical">Critical</div>'
                + crit.map(ccCard).join('');
        }
        if (dec.length) {
            queue += '<div class="cc-attention-section cc-sec-decision">Needs your decision</div>'
                + dec.map(ccCard).join('');
        }
        if (!queue) queue = '<div class="cc-empty">Nothing needs you — the swarm is running clean</div>';
        var html = '<div class="cc-exception-scroll">' + queue + '</div>';
        if (handled.count) {
            html += '<div class="cc-handled-region">'
                + '<div class="cc-handled-toggle' + (ccHandledOpen ? ' open' : '') + '" data-action="ccToggleHandled">'
                + '<span class="cc-handled-arrow">' + (ccHandledOpen ? '▾' : '▸') + '</span> '
                + handled.count + ' item' + (handled.count === 1 ? '' : 's') + ' the swarm is handling'
                + '</div>'
                + '<div class="cc-handled-list" style="display:' + (ccHandledOpen ? 'block' : 'none') + '">'
                + (handled.items || []).map(function (h) {
                    return '<div class="cc-handled-row">'
                        + '<span class="cc-handled-title">' + escapeHtml(h.title || '') + '</span>'
                        + '<span class="cc-handled-reason">' + escapeHtml(h.reason || '') + '</span>'
                        + '</div>';
                }).join('')
                + '</div>'
                + '</div>';
        }
        list.innerHTML = html;
    }

    function isAttentionBusy(list) {
        if (!list) return false;
        // Operator actively typing in a reply field = don't wipe it.
        // Only a focused text input/textarea counts — a focused action
        // button (e.g. the Dismiss the operator just clicked, which is
        // inside this list) is NOT "busy" and must not suppress the
        // re-render, or dismissed cards never disappear until a manual
        // page refresh.
        var ae = document.activeElement;
        if (ae && list.contains(ae)) {
            var tag = (ae.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea' || ae.isContentEditable) return true;
        }
        // Any visible reply box = operator started composing a reply.
        var openReplies = list.querySelectorAll('.cc-attention-reply');
        for (var i = 0; i < openReplies.length; i++) {
            if (openReplies[i].style.display !== 'none' && openReplies[i].offsetParent !== null) {
                return true;
            }
        }
        return false;
    }

    function ccReplyStart(target) {
        var tid = target.dataset.threadId;
        var box = document.querySelector('[data-reply-for="' + cssEscape(tid) + '"]');
        if (!box) return;
        box.style.display = 'flex';
        var input = box.querySelector('input');
        if (!input) return;
        input.focus();
        // Attach keydown DIRECTLY on the input so Enter can't be eaten by
        // any other document-level keyhandler. Idempotent — flag with a
        // data attribute so we don't double-register if the operator
        // reopens the box.
        if (!input.dataset.ccReplyBound) {
            input.dataset.ccReplyBound = '1';
            input.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    e.stopPropagation();
                    var body = (this.value || '').trim();
                    if (!body) return;
                    sendReply(tid, body);
                    this.value = '';
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    e.stopPropagation();
                    box.style.display = 'none';
                }
            });
        }
    }

    function cssEscape(s) {
        return String(s).replace(/[^A-Za-z0-9_-]/g, function (c) { return '\\' + c; });
    }

    function ccReplySendBtn(target) {
        var tid = target.dataset.threadId;
        var box = document.querySelector('[data-reply-for="' + cssEscape(tid) + '"]');
        if (!box) return;
        var input = box.querySelector('input');
        var body = input && input.value ? input.value.trim() : '';
        if (!body) return;
        sendReply(tid, body);
        if (input) input.value = '';
    }

    function sendReply(thread_id, body) {
        return ccPost('/api/attention/' + encodeURIComponent(thread_id) + '/reply', { body: body }).then(function (r) {
            if (!r.ok) {
                return r.text().then(function (txt) {
                    var msg = 'Reply failed (' + r.status + ')';
                    try {
                        var parsed = JSON.parse(txt);
                        if (parsed && parsed.error) msg += ': ' + parsed.error;
                    } catch (_) {
                        if (txt) msg += ': ' + txt.substring(0, 200);
                    }
                    if (window.showToast) window.showToast(msg, true);
                    console.warn('[cc] reply failed:', r.status, txt);
                });
            }
            return r.json().then(function (d) {
                var label = d && d.delivered_to ? d.delivered_to : 'worker';
                if (window.showToast) window.showToast('Reply sent to ' + label);
                loadAttention();
            });
        }).catch(function (err) {
            if (window.showToast) window.showToast('Reply failed: ' + (err && err.message || 'network error'), true);
            console.warn('[cc] reply fetch error:', err);
        });
    }

    function ccDismissAttention(target) {
        var tid = target.dataset.threadId;
        if (!tid) return;
        ccPost('/api/attention/' + encodeURIComponent(tid) + '/resolve', {}).then(function (r) {
            if (!r.ok && window.showToast) window.showToast('Dismiss failed (' + r.status + ')', true);
            loadAttention();
        }).catch(function () {});
    }


    // ----- CC layout: drag-resize handles + localStorage persistence ------
    var CC_ATTENTION_PCT_KEY = 'swarm_cc_attention_pct';
    var CC_MIN_PCT = 15;
    var CC_MAX_PCT = 85;

    function applyCcLayoutFromStorage() {
        var cc = el('command-center');
        if (!cc) return;
        var grid = cc.querySelector('.command-center-grid');
        if (!grid) return;
        try {
            var p = parseFloat(localStorage.getItem(CC_ATTENTION_PCT_KEY));
            if (isFinite(p) && p >= CC_MIN_PCT && p <= CC_MAX_PCT) {
                grid.style.setProperty('--cc-attention-pct', p + '%');
            }
        } catch (_) {}
    }

    function attachCcResizeHandles() {
        var grid = document.querySelector('#command-center .command-center-grid');
        if (!grid) return;
        var colHandle = el('cc-col-resize');
        if (colHandle && !colHandle.dataset.ccBound) {
            colHandle.dataset.ccBound = '1';
            attachColResize(colHandle, grid);
        }
    }

    function attachColResize(handle, grid) {
        var dragging = false;
        var rect = null;
        function onDown(e) {
            dragging = true;
            rect = grid.getBoundingClientRect();
            handle.classList.add('dragging');
            document.body.style.cursor = 'col-resize';
            e.preventDefault();
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup', onUp, { once: true });
        }
        function onMove(e) {
            if (!dragging || !rect) return;
            var pct = ((e.clientX - rect.left) / rect.width) * 100;
            pct = Math.max(CC_MIN_PCT, Math.min(CC_MAX_PCT, pct));
            grid.style.setProperty('--cc-attention-pct', pct + '%');
        }
        function onUp() {
            if (!dragging) return;
            dragging = false;
            handle.classList.remove('dragging');
            document.body.style.cursor = '';
            window.removeEventListener('mousemove', onMove);
            try {
                var v = grid.style.getPropertyValue('--cc-attention-pct');
                if (v) localStorage.setItem(CC_ATTENTION_PCT_KEY, parseFloat(v));
            } catch (_) {}
        }
        handle.addEventListener('mousedown', onDown);
    }

    function ccFocusLive(target) {
        var name = target && target.dataset && target.dataset.worker;
        if (name && window.selectWorker) window.selectWorker(name);
    }

    // ----- Today's digest (thin strip) ------------------------------------
    function loadDigest() {
        var midnight = new Date();
        midnight.setHours(0, 0, 0, 0);
        var sinceTs = midnight.getTime() / 1000;
        return fetchJSON('/api/events?categories=ship,task,attention&limit=300').then(function (r) {
            var events = (r.events || []).filter(function (e) { return e.ts >= sinceTs; });
            var ships = events.filter(function (e) { return e.category === 'ship' });
            var tasks = events.filter(function (e) { return e.category === 'task' });
            var box = el('cc-digest-body');
            if (!box) return;
            var pieces = [];
            pieces.push('<span><span class="cc-digest-num">' + ships.length + '</span> shipped today</span>');
            pieces.push('<span class="text-muted">·</span>');
            pieces.push('<span><span class="cc-digest-num">' + tasks.length + '</span> task events</span>');
            if (ships.length) {
                var preview = ships.slice(0, 2).map(function (s) {
                    return escapeHtml((s.title || '').replace(/^#\d+\s+/, '').substring(0, 40));
                }).join(' · ');
                pieces.push('<span class="text-muted" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;">' + preview + '</span>');
            } else {
                pieces.push('<span class="text-muted text-xs">nothing shipped yet</span>');
            }
            box.innerHTML = pieces.join('');
        }).catch(function () {});
    }

    // The Queen's interactive surface is now her embedded live PTY
    // (mountQueenEmbed). The chat-relay (loadQueenThreads / ccAskQueen /
    // thread rendering / pending-indicator / queen.message handling) was
    // deleted — it was an indirect bridge with multiple failure points.
    // Only her health dot remains here.

    function updateQueenHealthIndicator(state) {
        var node = el('cc-queen-health');
        if (!node) return;
        var map = {
            offline: { t: '● offline', c: 'text-danger' },
            thinking: { t: '● thinking', c: 'text-warning' },
            degraded: { t: '● waiting', c: 'text-warning' },
            alive: { t: '● online', c: 'text-success' },
        };
        var m = map[state] || { t: '·', c: 'text-muted' };
        node.textContent = m.t;
        node.className = 'text-xs ' + m.c;
    }
    // Expose so the WS handleEvent dispatcher in IIFE 1 can reach it.
    // Was previously called as a bare reference from IIFE 1, which
    // threw ReferenceError because the two IIFEs are separate scopes
    // (mobile QA caught it).
    window.updateQueenHealthIndicator = updateQueenHealthIndicator;

    function ccShowDashboard() {
        // Deselect any focused worker, hide the terminal, restore Command Center.
        try { if (window.selectedWorker) window.selectedWorker = null; } catch (_) {}
        // Clear any active inline terminal so it stops drawing under the CC.
        var detail = el('detail-body');
        if (detail) detail.innerHTML = '<div class="empty-state detail-empty-state modal-padding" style="display:none"></div>';
        show();
        // Refresh the panels — operator is returning to the dashboard.
        loadAttention();
        loadDigest();
    }
    window.ccShowDashboard = ccShowDashboard;

    // ----- Event delegation for CC actions --------------------------------
    function ccForceRest(target) {
        var name = target && target.dataset && target.dataset.worker;
        if (!name) return;
        ccPost('/api/workers/' + encodeURIComponent(name) + '/force-rest', {}).then(function (r) {
            if (!r.ok && window.showToast) window.showToast('Force-rest failed (' + r.status + ')', true);
            loadAttention();
        }).catch(function () {});
    }

    function ccRevive(target) {
        var name = target && target.dataset && target.dataset.worker;
        if (!name) return;
        ccPost('/api/workers/' + encodeURIComponent(name) + '/revive', {}).then(function (r) {
            if (!r.ok && window.showToast) window.showToast('Revive failed (' + r.status + ')', true);
            loadAttention();
        }).catch(function () {});
    }

    // Proposals are consolidated into the exception queue. Reuse the
    // existing global approve/reject (form-POST + toast + refresh), then
    // re-poll attention so the resolved card clears.
    function ccApproveProposal(target) {
        var id = target && target.dataset && target.dataset.proposalId;
        if (!id) return;
        if (window.approveProposal) window.approveProposal(id);
        setTimeout(loadAttention, 600);
    }

    function ccRejectProposal(target) {
        var id = target && target.dataset && target.dataset.proposalId;
        if (!id) return;
        if (window.rejectProposal) window.rejectProposal(id);
        setTimeout(loadAttention, 600);
    }

    // Collapsed "Queen is handling" drawer. Toggle DOM directly (don't
    // re-fetch) and remember the open state across the 15s re-render.
    function ccToggleHandled() {
        ccHandledOpen = !ccHandledOpen;
        var list = el('cc-attention-list');
        if (!list) return;
        var drawer = list.querySelector('.cc-handled-list');
        var toggle = list.querySelector('.cc-handled-toggle');
        if (drawer) drawer.style.display = ccHandledOpen ? 'block' : 'none';
        if (toggle) {
            toggle.classList.toggle('open', ccHandledOpen);
            var arrow = toggle.querySelector('.cc-handled-arrow');
            if (arrow) arrow.textContent = ccHandledOpen ? '▾' : '▸';
        }
    }

    function ccGotoResources() {
        if (window.showToast) window.showToast('System under pressure — see the Resources panel');
    }

    // Answer a waiting worker's choice prompt inline: send the picked
    // option number to its PTY (same path the Queen "1"/"2" strip uses),
    // then re-poll so the resolved card clears.
    function ccChoose(target) {
        var name = target && target.dataset && target.dataset.worker;
        var choice = target && target.dataset && target.dataset.choice;
        if (!name || choice == null || choice === '') return;
        ccPost('/api/workers/' + encodeURIComponent(name) + '/send', { message: String(choice) }).then(function (r) {
            if (!r.ok && window.showToast) window.showToast('Send failed (' + r.status + ')', true);
            loadAttention();
        }).catch(function () {});
    }

    // Queen-bottom action strip. The worker toolbar's doAction path keys
    // off the selectedWorker/activeTermWorker globals, and the embedded
    // Queen is deliberately neither — so these reuse the explicit-name
    // cc* pattern (ccPost + the literal worker name "queen", which every
    // backend verb already accepts since the Queen is a registered
    // worker). Two data-driven handlers cover send-a-line and run-a-verb
    // so we don't grow one function per button.
    function ccQueenSend(target) {
        var msg = target && target.dataset && target.dataset.qmsg;
        if (!msg) return;
        ccPost('/api/workers/queen/send', { message: msg }).then(function (r) {
            if (!r.ok && window.showToast) window.showToast('Queen send failed (' + r.status + ')', true);
        }).catch(function () {});
    }

    function ccQueenVerb(target) {
        var verb = target && target.dataset && target.dataset.qverb;
        if (!verb) return;
        var run = function () {
            ccPost('/api/workers/queen/' + encodeURIComponent(verb), {}).then(function (r) {
                if (!r.ok && window.showToast) window.showToast('Queen ' + verb + ' failed (' + r.status + ')', true);
                else if (window.showToast) window.showToast('Queen: ' + verb);
            }).catch(function () {});
        };
        // Same themed confirm as every other destructive op (no native confirm()).
        if (verb === 'kill' && window.showConfirm) {
            window.showConfirm('Kill the Queen process? She can be revived.', run);
            return;
        }
        run();
    }

    // Refresh = re-mount + reconnect her embedded socket. NOT
    // hardReconnectTermEntry — that re-attaches into the detail pane and
    // would tear the embed out of #cc-queen-term-holder. mountQueenEmbed
    // is the embed-safe reconnect (it re-appends to the holder and calls
    // connectTermEntryWs internally).
    function ccQueenRefresh() {
        try { if (window.mountQueenEmbed) window.mountQueenEmbed(); } catch (_) {}
    }

    // Export the Queen's transcript. The worker action bar's Export uses
    // exportTerminal() which keys off activeTermWorker; the embedded Queen is
    // never activeTermWorker, so reach her term entry by its explicit cache
    // key ('queen', set by mountQueenEmbed) and reuse the same serialize path.
    function ccQueenExport() {
        var entry = termCache.get('queen');
        if (!entry || !entry.serializeAddon) { showToast('Export not available', true); return; }
        try {
            var content = entry.serializeAddon.serialize();
            var blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            var ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
            a.download = 'queen-' + ts + '.txt';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(function() { URL.revokeObjectURL(url); }, 5000);
            showToast('Exported: ' + a.download);
        } catch (e) {
            showToast('Export failed: ' + e.message, true);
        }
    }

    // P5: Mobile focus toggle — under 600px the Command Center grid shows
    // ONE panel at a time (Queen or Attention). The button below the
    // command-center flips a body class that the CSS keys off of. We
    // remember the choice in localStorage so re-render doesn't reset it.
    var _CC_FOCUS_KEY = 'swarm.cc.mobileFocus';
    function ccMobileFocus(target) {
        // 'attention' | 'queen'; anything else defaults to attention since
        // that's where new escalations / messages land — the more time-
        // sensitive surface on a phone.
        var focus = (target === 'queen') ? 'queen' : 'attention';
        try { localStorage.setItem(_CC_FOCUS_KEY, focus); } catch (_) {}
        document.body.classList.toggle('cc-focus-attention', focus === 'attention');
        document.body.classList.toggle('cc-focus-queen', focus === 'queen');
        document.querySelectorAll('[data-cc-focus]').forEach(function(btn) {
            var on = btn.dataset.ccFocus === focus;
            btn.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        // Note: the prior version of this toggle wrote
        // localStorage.swarm.lastActiveWorker = 'queen' so the share-
        // target flow would route screenshots into the Queen's PTY.
        // That hack is no longer needed — shares now route to the Queen
        // unconditionally (see checkShareIntent), so the toggle no
        // longer has to second-guess which worker is "active."
    }
    window.ccMobileFocus = ccMobileFocus;
    // Initialize from prior choice (default attention) on page load —
    // matters even on desktop so a phone-rotate or window-resize down
    // honors the stored preference instead of randomly defaulting.
    try {
        var _ccStored = (localStorage.getItem(_CC_FOCUS_KEY) || 'attention');
        ccMobileFocus(_ccStored);
    } catch (_) { ccMobileFocus('attention'); }

    var CC_HANDLERS = {
        ccReplyStart: ccReplyStart,
        ccReplySendBtn: ccReplySendBtn,
        ccDismissAttention: ccDismissAttention,
        ccQueenFullscreen: ccQueenFullscreen,
        ccShowDashboard: ccShowDashboard,
        ccFocusLive: ccFocusLive,
        ccForceRest: ccForceRest,
        ccChoose: ccChoose,
        ccRevive: ccRevive,
        ccApproveProposal: ccApproveProposal,
        ccRejectProposal: ccRejectProposal,
        ccToggleHandled: ccToggleHandled,
        ccGotoResources: ccGotoResources,
        ccQueenSend: ccQueenSend,
        ccQueenVerb: ccQueenVerb,
        ccQueenRefresh: ccQueenRefresh,
        ccQueenExport: ccQueenExport,
    };

    document.addEventListener('click', function (e) {
        var target = e.target.closest('[data-action]');
        if (!target) return;
        var fn = CC_HANDLERS[target.dataset.action];
        if (fn) fn(target);
    }, true); // capture phase so we run before main dispatcher

    // Capture-phase safety net for Enter on attention reply inputs — runs
    // before any internal keyhandler so the click + ccReplyStart path
    // also works even if the direct-bind missed for any reason.
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' || e.shiftKey) return;
        var t = e.target;
        if (!t || t.tagName !== 'INPUT' || !t.dataset || !t.dataset.ccReplyInput) return;
        e.preventDefault();
        e.stopPropagation();
        var body = (t.value || '').trim();
        if (!body) return;
        sendReply(t.dataset.ccReplyInput, body);
        t.value = '';
    }, true);

    // ----- Notifications --------------------------------------------------
    function maybeNotifyAttention() {
        var badge = el('cc-attention-count');
        if (!badge) return;
        var current = parseInt(badge.getAttribute('data-count') || '0', 10);
        if (current > lastAttentionCount && document.hidden) {
            try {
                if (Notification.permission === 'granted') {
                    var diff = current - lastAttentionCount;
                    new Notification('Swarm Attention', {
                        body: diff + ' new item(s) need your attention (' + current + ' total)',
                        tag: 'swarm-attention',
                    });
                }
            } catch (_) {}
        }
        lastAttentionCount = current;
    }

    // ----- Boot -----------------------------------------------------------
    function init() {
        setupMobileComposer();  // wire the touch composer (auto-grow, Enter=send)
        // If sessionStorage has a worker that the existing dashboard's
        // restoreWorker (line 7830) will mount into detail-body, START
        // in worker mode — otherwise show() would hide detail-body and
        // xterm would mount into a zero-width container, leaving the
        // terminal stuck in a narrow-column rendering even after the
        // operator returns to it. The operator can always click
        // "Queen Dashboard" in the sidebar to switch to CC.
        var restoredWorker = null;
        try { restoredWorker = sessionStorage.getItem('swarm_selected_worker'); } catch (_) {}
        if (restoredWorker && restoredWorker !== 'null' && restoredWorker.length > 0) {
            // Start in worker view; user explicitly clicks Queen Dashboard
            // when they want the CC.
            hide();
        } else {
            // Command Center is the landing. show() adds body.cc-active, which
            // the CSS keys off to reveal #command-center and hide #detail-body
            // — no inline display juggling (that's what caused the mixed render).
            show();
        }

        try {
            if (window.Notification && Notification.permission === 'default') {
                Notification.requestPermission();
            }
        } catch (_) {}

        loadAttention().then(maybeNotifyAttention);
        loadDigest();
        loadQueenStatusStrip();
        attachDetailBodyObserver();
        applyCcLayoutFromStorage();
        attachCcResizeHandles();

        setInterval(function () {
            // CC visibility is the body.cc-active class now (not inline display).
            if (!document.body.classList.contains('cc-active')) return;
            loadDigest();
        }, DIGEST_INTERVAL_MS);

        // Even when CC is hidden, refresh Attention so the count tracks
        // for notification purposes when the user returns.
        setInterval(function () {
            loadAttention().then(maybeNotifyAttention);
        }, POLL_INTERVAL_MS);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
