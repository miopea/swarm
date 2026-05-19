// swarm/web/static/toast.js — shared toast / notification helper.
//
// Pre-unification (before Phase D of the duplication sweep) the
// dashboard and config pages each had their own ``showToast`` /
// ``_toastApplyResult`` implementations.  The dashboard's was the
// fully-featured one (dedup, screen-reader announce, click-to-dismiss,
// notification-badge integration) and the config page's was a
// minimal "append a div, remove after 3.5s" copy that silently
// dropped the accessibility and dedup features.
//
// This module is the canonical implementation.  It uses:
//   - ``#toasts``     container in base.html (already present)
//   - ``#sr-announcer`` aria-live region (added to base.html in
//     this phase so all pages get screen-reader announcements,
//     not just the dashboard)
//   - ``window.addNotification`` (optional) — the dashboard
//     defines this to drive its badge counter / title flash.
//     Other pages don't, so we call it conditionally.
//
// Loaded from base.html.  Exposes ``window.showToast`` and
// ``window._toastApplyResult``.

(function () {
    'use strict';

    var TOAST_DEDUP_MS = 2000;
    var TOAST_VISIBLE_MS = 3500;
    // A toast is a glance, not a log. Hard-cap to one ellipsised line so
    // a 30-line terminal dump / full escalation reason can't become a
    // wall, and cap how many can stack so a burst can't fill the screen.
    var TOAST_MAX_CHARS = 160;
    var TOAST_MAX_STACK = 4;
    var BEE_HAPPY = '/static/bees/happy.svg';
    var BEE_ANGRY = '/static/bees/angry.svg';

    // Recent toasts for dedup — same message inside the window is
    // collapsed to a single visual toast.  Pre-Phase-D this lived in
    // dashboard.js only, so config.html happily showed three identical
    // "Saved" toasts when a save triggered three sub-callbacks.
    var _recentToasts = [];

    function _escape(str) {
        var d = document.createElement('div');
        d.textContent = String(str == null ? '' : str);
        return d.innerHTML;
    }

    // Collapse newlines/runs of whitespace and clamp to a single short
    // line. Applied before dedup + display so near-identical multi-line
    // blobs also collapse and nothing ever renders as a paragraph.
    function _terse(msg) {
        var s = String(msg == null ? '' : msg).replace(/\s+/g, ' ').trim();
        return s.length > TOAST_MAX_CHARS ? s.slice(0, TOAST_MAX_CHARS - 1) + '…' : s;
    }

    function _isDuplicate(msg) {
        var now = Date.now();
        _recentToasts = _recentToasts.filter(function (t) {
            return now - t.ts < TOAST_DEDUP_MS;
        });
        for (var i = 0; i < _recentToasts.length; i++) {
            if (_recentToasts[i].msg === msg) return true;
        }
        _recentToasts.push({ msg: msg, ts: now });
        return false;
    }

    function _announce(msg) {
        var announcer = document.getElementById('sr-announcer');
        if (announcer) announcer.textContent = msg;
    }

    window.showToast = function (msg, warning, beeSrc) {
        msg = _terse(msg);
        if (_isDuplicate(msg)) return;
        _announce(msg);

        var container = document.getElementById('toasts');
        if (!container) return; // base.html missing the container — silent no-op
        var toast = document.createElement('div');
        toast.className = 'toast' + (warning ? ' toast-warning' : '');
        toast.style.display = 'flex';
        toast.style.alignItems = 'center';
        toast.style.cursor = 'pointer';
        var bee = beeSrc || (warning ? BEE_ANGRY : BEE_HAPPY);
        toast.innerHTML =
            '<img src="' + bee + '" class="bee-icon bee-md toast-bee" alt=""' +
            ' onerror="this.style.display=\'none\'">' +
            _escape(msg);
        toast.addEventListener('click', function () {
            toast.remove();
        });
        container.appendChild(toast);
        // Burst guard: keep only the newest TOAST_MAX_STACK so a flurry
        // (many workers escalating at once) can't wall the viewport.
        while (container.children.length > TOAST_MAX_STACK) {
            container.removeChild(container.firstChild);
        }
        setTimeout(function () { toast.remove(); }, TOAST_VISIBLE_MS);

        // Optional dashboard hook for badge counter + title flash.
        // Other pages don't define it; do nothing.
        if (typeof window.addNotification === 'function') {
            try { window.addNotification(msg, warning); }
            catch (e) { /* dashboard helper threw — toast was still shown */ }
        }
    };

    // Phase 7 + 8 of #328 surfaced server-side ``_apply_result`` to
    // the operator.  Every config save endpoint that uses generic
    // dataclass dispatch returns an ``_apply_result`` with
    // ``consumed`` and ``unknown`` field-name lists.  If any field
    // was ignored — typo, stale dashboard, schema drift — show a
    // warning toast naming them.
    //
    // Pre-Phase-D the dashboard accepted parsed JSON (``data``) and
    // the config page accepted a Response object (``response``).  This
    // unified version accepts either: if the arg has a ``.clone`` method
    // it's a Response and we await its JSON; otherwise it's already a
    // parsed object.
    window._toastApplyResult = async function (responseOrData, opName) {
        try {
            var data;
            if (responseOrData && typeof responseOrData.clone === 'function') {
                data = await responseOrData.clone().json();
            } else {
                data = responseOrData;
            }
            var ar = data && data._apply_result;
            if (ar && Array.isArray(ar.unknown) && ar.unknown.length) {
                window.showToast(
                    opName + ' ok, but ' + ar.unknown.length +
                    ' field(s) ignored: ' + ar.unknown.join(', '),
                    true
                );
            }
        } catch (e) {
            // Malformed response or non-JSON body — silently OK; the
            // primary save callback already handled success/failure.
        }
    };
})();
