(function() {
    'use strict';

    var STORAGE_KEY = 'swarm.theme';
    var MODES = ['system', 'light', 'dark'];
    var media = window.matchMedia('(prefers-color-scheme: dark)');
    var preference = readPreference();
    var effectiveTheme = '';

    var terminalThemes = {
        dark: {
            background: '#15130F',
            foreground: '#F5F1E8',
            cursor: '#F1B83D',
            cursorAccent: '#15130F',
            selectionBackground: 'rgba(241,184,61,0.30)',
            black: '#15130F',
            red: '#FF7B72',
            green: '#7FCB87',
            yellow: '#F1B83D',
            blue: '#7CC4F2',
            magenta: '#B8A5FF',
            cyan: '#75D5CF',
            white: '#F5F1E8',
            brightBlack: '#756B5D',
            brightRed: '#FF9A93',
            brightGreen: '#A1DDA7',
            brightYellow: '#FFD071',
            brightBlue: '#A3D8F7',
            brightMagenta: '#D0C3FF',
            brightCyan: '#A0E6E1',
            brightWhite: '#FFFFFF'
        },
        light: {
            background: '#FFFFFF',
            foreground: '#211D18',
            cursor: '#7A5000',
            cursorAccent: '#FFFFFF',
            selectionBackground: 'rgba(122,80,0,0.20)',
            black: '#211D18',
            red: '#B42318',
            green: '#2F6F3E',
            yellow: '#7A5000',
            blue: '#1F6591',
            magenta: '#5F45A8',
            cyan: '#176B67',
            white: '#665E53',
            brightBlack: '#4E473C',
            brightRed: '#B42318',
            brightGreen: '#2F6F3E',
            brightYellow: '#7A5000',
            brightBlue: '#1F6591',
            brightMagenta: '#5F45A8',
            brightCyan: '#176B67',
            brightWhite: '#211D18'
        }
    };

    function readPreference() {
        try {
            var saved = localStorage.getItem(STORAGE_KEY);
            return MODES.indexOf(saved) >= 0 ? saved : 'system';
        } catch (error) {
            return 'system';
        }
    }

    function resolvedTheme(mode) {
        return mode === 'system' ? (media.matches ? 'dark' : 'light') : mode;
    }

    function updateThemeColor(theme) {
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.setAttribute('content', theme === 'dark' ? '#15130F' : '#F6F4EF');
    }

    function syncControls() {
        var label = preference.charAt(0).toUpperCase() + preference.slice(1);
        document.querySelectorAll('.theme-toggle').forEach(function(button) {
            button.setAttribute('aria-label', 'Colour theme: ' + label + '. Activate to change.');
            button.setAttribute('title', 'Colour theme: ' + label);
            button.dataset.themePreference = preference;
            var text = button.querySelector('.theme-toggle-label');
            if (text) text.textContent = label;
        });
    }

    function apply(mode, persist) {
        if (MODES.indexOf(mode) < 0) mode = 'system';
        preference = mode;
        var nextTheme = resolvedTheme(mode);
        var changed = effectiveTheme !== nextTheme;

        document.documentElement.setAttribute('data-theme', nextTheme);
        document.documentElement.setAttribute('data-theme-preference', mode);
        document.documentElement.style.colorScheme = nextTheme;
        updateThemeColor(nextTheme);

        if (persist) {
            try {
                localStorage.setItem(STORAGE_KEY, mode);
            } catch (error) {
                // A blocked storage API must not prevent theme selection.
            }
        }

        effectiveTheme = nextTheme;
        syncControls();
        if (changed) {
            window.dispatchEvent(new CustomEvent('swarm:themechange', {
                detail: { preference: preference, theme: effectiveTheme }
            }));
        }
        return effectiveTheme;
    }

    function cycle() {
        var index = MODES.indexOf(preference);
        return apply(MODES[(index + 1) % MODES.length], true);
    }

    function getTerminalTheme() {
        return Object.assign({}, terminalThemes[effectiveTheme || resolvedTheme(preference)]);
    }

    function getTerminalMinimumContrastRatio() {
        return (effectiveTheme || resolvedTheme(preference)) === 'light' ? 4.5 : 1;
    }

    window.swarmTheme = {
        apply: apply,
        cycle: cycle,
        getPreference: function() { return preference; },
        getEffectiveTheme: function() { return effectiveTheme; },
        getTerminalTheme: getTerminalTheme,
        getTerminalMinimumContrastRatio: getTerminalMinimumContrastRatio
    };

    apply(preference, false);

    media.addEventListener('change', function() {
        if (preference === 'system') apply('system', false);
    });

    document.addEventListener('click', function(event) {
        var button = event.target.closest('[data-action="cycleTheme"]');
        if (!button) return;
        event.preventDefault();
        cycle();
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', syncControls);
    } else {
        syncControls();
    }
})();
