// Timelog in the GNOME top bar: follows /api/v1/timer/stream (server-sent events) with an
// API key, counts the elapsed time locally, and pauses, resumes or stops through the API.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const RECONNECT_MIN = 2; // seconds; doubles after each failure
const RECONNECT_MAX = 60;
const STALE_AFTER = 70; // seconds without data before reconnecting (the server sends a keep-alive every 25)

const now = () => GLib.get_monotonic_time() / 1e6;

function formatElapsed(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function shorten(text, max) {
    return text.length > max ? `${text.slice(0, Math.max(1, max - 1)).trimEnd()}…` : text;
}

const TimelogIndicator = GObject.registerClass(
class TimelogIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'Timelog');
        this._extension = extension;
        this._settings = extension.getSettings();
        this._session = new Soup.Session({user_agent: 'timelog-gnome-extension'});
        this._entry = null; // the `entry` of the last timer event
        this._receivedAt = 0;
        this._problem = null; // why the timer cannot be shown, or null
        this._cancellable = null;
        this._reconnectId = 0;
        this._backoff = RECONNECT_MIN;
        this._lastData = 0;
        this._busy = false; // an action request is on its way

        this._box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this._icon = new St.Icon({
            icon_name: 'preferences-system-time-symbolic',
            style_class: 'system-status-icon timelog-icon',
        });
        this._label = new St.Label({style_class: 'timelog-label', y_align: Clutter.ActorAlign.CENTER});
        this._box.add_child(this._icon);
        this._box.add_child(this._label);
        this.add_child(this._box);

        this._title = new PopupMenu.PopupMenuItem('', {reactive: false, style_class: 'timelog-menu-title'});
        this._detail = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._status = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._toggle = new PopupMenu.PopupMenuItem('Pause');
        this._toggle.connect('activate', () => this._act(this._entry?.state === 'running' ? 'pause' : 'resume'));
        this._stop = new PopupMenu.PopupMenuItem('Stop');
        this._stop.connect('activate', () => this._act('stop'));
        this._open = new PopupMenu.PopupMenuItem('Open Timelog');
        this._open.connect('activate', () => this._openApp());
        const prefs = new PopupMenu.PopupMenuItem('Settings');
        prefs.connect('activate', () => this._extension.openPreferences());
        for (const item of [this._title, this._detail, this._status, this._toggle, this._stop])
            this.menu.addMenuItem(item);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._open);
        this.menu.addMenuItem(prefs);

        this._settingIds = [
            // Typing in the settings changes these on every key, so wait for a pause.
            this._settings.connect('changed::server-url', () => this._restart(1)),
            this._settings.connect('changed::api-key', () => this._restart(1)),
            this._settings.connect('changed::max-chars', () => this._render()),
            this._settings.connect('changed::show-when-idle', () => this._render()),
        ];
        // After suspend or a network switch the old connection is probably dead.
        this._network = Gio.NetworkMonitor.get_default();
        this._networkId = this._network.connect('network-changed', (_monitor, available) => {
            if (available)
                this._restart(2);
        });
        this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
            this._tick();
            return GLib.SOURCE_CONTINUE;
        });

        this._render();
        this._connect();
    }

    _baseUrl() {
        return this._settings.get_string('server-url').trim().replace(/\/+$/, '');
    }

    _apiKey() {
        return this._settings.get_string('api-key').trim();
    }

    _request(method, path) {
        let message = null;
        try {
            message = Soup.Message.new(method, `${this._baseUrl()}${path}`);
        } catch (e) {
            message = null;
        }
        if (message)
            message.request_headers.append('Authorization', `Bearer ${this._apiKey()}`);
        return message;
    }

    // --- The stream ---

    _connect() {
        this._disconnect();
        if (!this._baseUrl() || !this._apiKey()) {
            this._setProblem('Add the Timelog address and an API key in the settings.');
            return;
        }
        const message = this._request('GET', '/api/v1/timer/stream');
        if (!message) {
            this._setProblem('The Timelog address is not a valid URL.');
            return;
        }
        message.request_headers.append('Accept', 'text/event-stream');
        const cancellable = this._cancellable = new Gio.Cancellable();
        this._lastData = now();
        this._session.send_async(message, GLib.PRIORITY_DEFAULT, cancellable, (session, result) => {
            let stream;
            try {
                stream = session.send_finish(result);
            } catch (e) {
                if (!cancellable.is_cancelled())
                    this._retry(`Cannot reach Timelog: ${e.message}`);
                return;
            }
            const status = message.get_status();
            if (status !== Soup.Status.OK) {
                stream.close_async(GLib.PRIORITY_DEFAULT, null, null);
                if (status === Soup.Status.UNAUTHORIZED)
                    this._retry('The API key is wrong or was removed.', RECONNECT_MAX);
                else
                    this._retry(`Timelog answered ${status} ${message.get_reason_phrase() ?? ''}`.trim());
                return;
            }
            this._backoff = RECONNECT_MIN;
            this._setProblem(null);
            const lines = new Gio.DataInputStream({base_stream: stream, close_base_stream: true});
            this._readEvents(lines, cancellable, {event: 'message', data: []});
        });
    }

    _readEvents(lines, cancellable, pending) {
        lines.read_line_async(GLib.PRIORITY_DEFAULT, cancellable, (stream, result) => {
            let line;
            try {
                [line] = stream.read_line_finish_utf8(result);
            } catch (e) {
                if (!cancellable.is_cancelled())
                    this._retry(`Lost the connection: ${e.message}`);
                return;
            }
            if (cancellable.is_cancelled())
                return;
            if (line === null) {
                this._retry('Timelog closed the connection.');
                return;
            }
            this._lastData = now();
            line = line.replace(/\r$/, '');
            if (line === '') {
                // A blank line ends an event.
                if (pending.event === 'timer' && pending.data.length)
                    this._onTimer(pending.data.join('\n'));
                pending = {event: 'message', data: []};
            } else if (line.startsWith('event:')) {
                pending.event = line.slice(6).trim();
            } else if (line.startsWith('data:')) {
                pending.data.push(line.slice(5).replace(/^ /, ''));
            }
            this._readEvents(lines, cancellable, pending);
        });
    }

    _onTimer(text) {
        let payload;
        try {
            payload = JSON.parse(text);
        } catch (e) {
            return;
        }
        this._entry = payload.entry ?? null;
        this._receivedAt = now();
        this._render();
    }

    _disconnect() {
        if (this._cancellable) {
            this._cancellable.cancel();
            this._cancellable = null;
        }
        if (this._reconnectId) {
            GLib.source_remove(this._reconnectId);
            this._reconnectId = 0;
        }
    }

    _retry(problem, delay = null) {
        this._disconnect();
        this._setProblem(problem);
        const wait = delay ?? this._backoff;
        this._backoff = Math.min(this._backoff * 2, RECONNECT_MAX);
        this._schedule(wait);
    }

    // Starts over soon, e.g. after the settings change.
    _restart(delay) {
        this._disconnect();
        this._backoff = RECONNECT_MIN;
        this._schedule(delay);
    }

    _schedule(seconds) {
        if (this._reconnectId)
            GLib.source_remove(this._reconnectId);
        this._reconnectId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, seconds, () => {
            this._reconnectId = 0;
            this._connect();
            return GLib.SOURCE_REMOVE;
        });
    }

    _tick() {
        if (this._cancellable && !this._problem && now() - this._lastData > STALE_AFTER)
            this._retry('No word from Timelog; reconnecting.');
        if (this._entry?.state === 'running')
            this._render();
    }

    // --- Actions ---

    // A left click on a running or paused timer pauses or resumes it; other clicks, touches and
    // clicks while no timer is on open the menu as usual.
    vfunc_event(event) {
        if (this._entry && !this.menu.isOpen && event.type() === Clutter.EventType.BUTTON_PRESS &&
            event.get_button() === Clutter.BUTTON_PRIMARY) {
            this._act(this._entry.state === 'running' ? 'pause' : 'resume');
            return Clutter.EVENT_STOP;
        }
        return super.vfunc_event(event);
    }

    _act(action) {
        if (this._busy)
            return; // a double click should not pause and resume at once
        const message = this._request('POST', `/api/v1/timer/${action}`);
        if (!message)
            return;
        this._busy = true;
        this._session.send_and_read_async(message, GLib.PRIORITY_DEFAULT, null, (session, result) => {
            this._busy = false;
            let bytes;
            try {
                bytes = session.send_and_read_finish(result);
            } catch (e) {
                Main.notify('Timelog', `Could not ${action} the timer: ${e.message}`);
                return;
            }
            let body = null;
            try {
                body = JSON.parse(new TextDecoder().decode(bytes.get_data() ?? new Uint8Array()));
            } catch (e) {
                body = null; // not JSON, e.g. a proxy's error page
            }
            if (message.get_status() === Soup.Status.OK && body) {
                this._onTimer(JSON.stringify(body));
            } else {
                Main.notify('Timelog', body?.error ?? `Could not ${action} the timer (${message.get_status()}).`);
            }
        });
    }

    _openApp() {
        const base = this._baseUrl();
        if (!base)
            return;
        try {
            Gio.AppInfo.launch_default_for_uri(`${base}/#timer`, global.create_app_launch_context(0, -1));
        } catch (e) {
            Main.notify('Timelog', `Could not open ${base}: ${e.message}`);
        }
    }

    // --- Display ---

    _setProblem(problem) {
        this._problem = problem;
        this._render();
    }

    _elapsed() {
        const entry = this._entry;
        if (!entry)
            return 0;
        const extra = entry.state === 'running' ? now() - this._receivedAt : 0;
        return entry.elapsed_seconds + extra;
    }

    _render() {
        const entry = this._entry;
        const state = entry?.state;
        this._box.remove_style_class_name('timelog-running');
        this._box.remove_style_class_name('timelog-paused');
        this._box.remove_style_class_name('timelog-problem');
        if (state === 'running' || state === 'paused')
            this._box.add_style_class_name(`timelog-${state}`);
        if (this._problem)
            this._box.add_style_class_name('timelog-problem');
        this.visible = Boolean(entry || this._problem || this._settings.get_boolean('show-when-idle'));

        const clock = formatElapsed(this._elapsed());
        if (entry) {
            const max = this._settings.get_int('max-chars');
            const text = entry.description || entry.project;
            this._label.text = max > 0 && text ? `${clock}  ${shorten(text, max)}` : clock;
            this._label.show();
        } else {
            this._label.text = '';
            this._label.hide();
        }

        this._title.label.text = entry ? entry.description || '(no description)' : 'No timer is on';
        this._detail.label.text = entry
            ? `${entry.customer} › ${entry.project} · ${state === 'paused' ? 'paused at' : 'running,'} ${clock}`
            : '';
        this._detail.visible = Boolean(entry);
        this._status.label.text = this._problem ?? '';
        this._status.visible = Boolean(this._problem);
        this._toggle.label.text = state === 'paused' ? 'Resume' : 'Pause';
        this._toggle.visible = Boolean(entry);
        this._stop.visible = Boolean(entry);
        this._open.visible = Boolean(this._baseUrl());
    }

    destroy() {
        for (const id of this._settingIds)
            this._settings.disconnect(id);
        this._network.disconnect(this._networkId);
        GLib.source_remove(this._tickId);
        this._disconnect();
        this._session.abort();
        this._settings = null;
        super.destroy();
    }
});

export default class TimelogExtension extends Extension {
    enable() {
        this._indicator = new TimelogIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
