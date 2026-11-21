import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class TimelogPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage();

        const server = new Adw.PreferencesGroup({
            title: 'Timelog',
            description: 'Make an API key in Timelog under Settings → API. Making a new key there stops the old one.',
        });
        const url = new Adw.EntryRow({title: 'Address, e.g. https://timelog.example.com'});
        settings.bind('server-url', url, 'text', Gio.SettingsBindFlags.DEFAULT);
        const key = new Adw.PasswordEntryRow({title: 'API key'});
        settings.bind('api-key', key, 'text', Gio.SettingsBindFlags.DEFAULT);
        server.add(url);
        server.add(key);

        const display = new Adw.PreferencesGroup({title: 'Top bar'});
        const chars = Adw.SpinRow.new_with_range(0, 100, 1);
        chars.title = 'Longest description';
        chars.subtitle = 'Characters shown next to the time; 0 shows only the time';
        settings.bind('max-chars', chars, 'value', Gio.SettingsBindFlags.DEFAULT);
        const idle = new Adw.SwitchRow({
            title: 'Show when no timer is on',
            subtitle: 'Keeps the icon in the top bar for opening Timelog',
        });
        settings.bind('show-when-idle', idle, 'active', Gio.SettingsBindFlags.DEFAULT);
        const places = ['left', 'center', 'right'];
        const place = new Adw.ComboRow({
            title: 'Position',
            model: Gtk.StringList.new(['Left', 'Middle, right of the clock', 'Right']),
            selected: Math.max(0, places.indexOf(settings.get_string('panel-box'))),
        });
        place.connect('notify::selected', () => settings.set_string('panel-box', places[place.selected]));
        display.add(place);
        display.add(chars);
        display.add(idle);

        const about = new Adw.PreferencesGroup({title: 'About'});
        about.add(new Adw.ActionRow({
            title: 'Version',
            subtitle: String(this.metadata.version ?? 'unknown'),
            subtitle_selectable: true,
        }));
        const repo = new Adw.ActionRow({title: 'Source code', subtitle: this.metadata.url});
        const link = new Gtk.LinkButton({uri: this.metadata.url, label: 'Open', valign: Gtk.Align.CENTER});
        repo.add_suffix(link);
        repo.activatable_widget = link;
        about.add(repo);

        page.add(server);
        page.add(display);
        page.add(about);
        window.add(page);
    }
}
