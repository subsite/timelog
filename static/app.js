"use strict";

const S = {
  tree: { customers: [], projects: [] },
  recent: [],
  prefs: { theme: "system", date_format: "iso", round_minutes: 0, round_scope: "entry", default_rate: 0, currency: "EUR" },
  active: null,
  offset: 0,
  route: "timer",
};
const view = document.getElementById("view");
const NEW = "__new__";
const REPORT_KEY = "timelog.report";
const LAST_PROJECT_KEY = "timelog.lastProject";

// --- Helpers ---

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key in el && key !== "list") el[key] = value;
    else if (value !== undefined && value !== null && value !== false) el.setAttribute(key, value);
  }
  el.append(...kids.flat(Infinity).filter((kid) => kid !== null && kid !== undefined && kid !== false));
  return el;
}

async function api(path, method = "GET", body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("Not logged in");
  }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error((data && data.error) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(message) {
  const host = [...document.querySelectorAll("dialog[open]")].pop() || document.body;
  const el = h("div", { class: "toast", role: "alert" }, message);
  host.append(el);
  setTimeout(() => el.remove(), 5000);
}

function guard(fn) {
  return async (...args) => {
    try {
      return await fn(...args);
    } catch (err) {
      toast(err.message);
      if (err.status === 409) refreshTimer();
    }
  };
}

const now = () => Math.floor(Date.now() / 1000) + S.offset;
const pad = (n) => String(n).padStart(2, "0");

function fmtClock(seconds) {
  const s = Math.max(0, seconds);
  return `${Math.floor(s / 3600)}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}

function fmtHM(seconds) {
  const minutes = Math.round(seconds / 60);
  return `${Math.floor(minutes / 60)}:${pad(minutes % 60)}`;
}

const fmtDecimal = (seconds) => (seconds / 3600).toFixed(2);

// The currency is a setting; the same list is CURRENCIES in app.py.
const CURRENCIES = (
  "EUR USD GBP SEK NOK DKK ISK CHF PLN CZK HUF RON BGN CAD AUD NZD JPY CNY INR BRL MXN ZAR SGD HKD KRW TRY UAH ILS"
).split(" ");
const currency = () => S.prefs.currency;
const currencyName = (code) => {
  try {
    return `${code} – ${new Intl.DisplayNames(undefined, { type: "currency" }).of(code)}`;
  } catch {
    return code;
  }
};
const moneyFormats = {};
const fmtMoney = (amount) => {
  const code = currency();
  moneyFormats[code] ??= new Intl.NumberFormat(undefined, { style: "currency", currency: code });
  return moneyFormats[code].format(amount);
};

// Amounts are shown once any hourly rate has been set.
const hasRates = () => S.prefs.default_rate > 0 || S.tree.customers.some((c) => c.hourly_rate !== null);
// Dates and times are ISO 8601 with a 24-hour clock, unless Settings asks for the browser's locale.
const useLocale = () => S.prefs.date_format === "locale";
const fmtTime = (ts) => {
  const d = new Date(ts * 1000);
  if (useLocale()) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const fmtDate = (ts) => {
  const d = new Date(ts * 1000);
  return useLocale() ? d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) : dateKey(d);
};
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function dateKey(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

const DATE_FORMAT = "YYYY-MM-DD";
const DATETIME_FORMAT = "YYYY-MM-DD HH:MM";
const DATE_PATTERN = "\\d{4}-\\d{2}-\\d{2}";
const DATETIME_PATTERN = "\\d{4}-\\d{2}-\\d{2}[ T]\\d{1,2}:\\d{2}";

// The value of a date-and-time field; native locale fields need the "T" separator.
function toInput(ts) {
  const d = new Date(ts * 1000);
  return `${dateKey(d)}${useLocale() ? "T" : " "}${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Reads "YYYY-MM-DD HH:MM" in local time; NaN unless it names a real date and time.
function fromInput(value) {
  const m = /^\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})\s*$/.exec(value);
  if (!m) return NaN;
  const [y, mo, d, hh, mi] = m.slice(1).map(Number);
  const date = new Date(y, mo - 1, d, hh, mi);
  const exact = date.getFullYear() === y && date.getMonth() === mo - 1 && date.getDate() === d &&
    date.getHours() === hh && date.getMinutes() === mi;
  return exact ? Math.floor(date.getTime() / 1000) : NaN;
}

function isDate(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!m) return false;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return dateKey(d) === value;
}

const CALENDAR_ICON =
  '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5">' +
  '<rect x="2" y="3" width="12" height="11" rx="1.5"/><path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3"/></svg>';

// The value of an ISO text field as the browser's native date or datetime-local value, or "".
function toNative(text, withTime) {
  text = text.trim();
  if (!withTime) return isDate(text) ? text : "";
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})$/.exec(text);
  return m && !Number.isNaN(fromInput(text)) ? `${m[1]}T${m[2].padStart(2, "0")}:${m[3]}` : "";
}

// A date or date-and-time field: the browser's own picker in locale mode, otherwise ISO text
// that is marked invalid until it parses, with a calendar button that opens the browser's
// picker and writes its choice back as ISO text. Both give values that fromInput and isDate
// read. Put `el.field` on the page: it is the input plus its button.
function isoInput({ value = "", withTime, ...props }) {
  if (useLocale()) {
    const { placeholder, ...rest } = props;
    const el = h("input", { type: withTime ? "datetime-local" : "date", value, ...rest });
    el.field = el;
    return el;
  }
  const format = withTime ? DATETIME_FORMAT : DATE_FORMAT;
  const el = h("input", {
    type: "text",
    value,
    placeholder: format,
    pattern: withTime ? DATETIME_PATTERN : DATE_PATTERN,
    title: `Use the format ${format}${withTime ? " (24-hour clock)" : ""}`,
    inputMode: "numeric",
    autocomplete: "off",
    spellcheck: false,
    class: withTime ? "iso datetime" : "iso date",
    ...props,
  });
  const check = () => {
    const ok = !el.value || (withTime ? !Number.isNaN(fromInput(el.value)) : isDate(el.value.trim()));
    el.setCustomValidity(ok ? "" : `Use the format ${format}${withTime ? " (24-hour clock)" : ""}.`);
  };
  el.addEventListener("input", check);
  check();

  const native = h("input", { type: withTime ? "datetime-local" : "date", class: "picker-proxy", tabIndex: -1, "aria-hidden": "true" });
  const button = h("button", {
    type: "button",
    class: "picker-button",
    title: "Pick from a calendar",
    "aria-label": "Pick from a calendar",
    disabled: el.disabled,
    innerHTML: CALENDAR_ICON,
  });
  button.addEventListener("click", () => {
    native.value = toNative(el.value, withTime);
    try {
      native.showPicker();
    } catch {
      native.focus();
    }
  });
  native.addEventListener("change", () => {
    if (!native.value) return;
    el.value = withTime ? native.value.slice(0, 16).replace("T", " ") : native.value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
  el.field = h("span", { class: "iso-field" }, el, button, native);
  return el;
}

function entrySeconds(entry) {
  return entry.segments.reduce((sum, seg) => sum + (seg.end === null ? now() : seg.end) - seg.start, 0);
}

function projectPath(projectId) {
  const project = S.tree.projects.find((p) => p.id === projectId);
  const customer = project && S.tree.customers.find((c) => c.id === project.customer_id);
  return { project, customer };
}

function pathLabel(projectId) {
  const { project, customer } = projectPath(projectId);
  return project ? `${customer.name} › ${project.name}` : "Unknown project";
}

function usableProject(projectId) {
  const { project, customer } = projectPath(projectId);
  return project && !project.archived && !customer.archived ? projectId : null;
}

const lastUsableProject = () => usableProject(Number(localStorage.getItem(LAST_PROJECT_KEY)));

async function loadTree() {
  S.tree = await api("/api/tree");
}

// --- Earlier descriptions, offered when typing one ---

async function loadRecent() {
  S.recent = await api("/api/recent");
  const options = S.recent
    .filter((r) => usableProject(r.project_id))
    .map((r) => h("option", { value: r.description, label: pathLabel(r.project_id) }));
  let list = document.getElementById("recent-descriptions");
  if (!list) {
    list = h("datalist", { id: "recent-descriptions" });
    document.body.append(list);
  }
  list.replaceChildren(...options);
}

// Picking an earlier description from the list also fills in its project and billable setting.
function descriptionInput({ value = "", placeholder, picker, billable }) {
  const input = h("input", { value, maxLength: 2000, placeholder, list: "recent-descriptions", autocomplete: "off" });
  input.addEventListener("input", (e) => {
    // Typing sends an InputEvent with a type; choosing a suggestion does not, or uses insertReplacementText.
    if (e.inputType && e.inputType !== "insertReplacementText") return;
    const match = S.recent.find((r) => r.description === input.value && usableProject(r.project_id));
    if (!match) return;
    picker.set(match.project_id);
    billable.checked = Boolean(match.billable);
  });
  return input;
}

function askName(title, initial = "") {
  return new Promise((resolve) => {
    const input = h("input", { value: initial, required: true, maxLength: 200 });
    const dlg = h(
      "dialog",
      { class: "narrow" },
      h(
        "form",
        {
          onsubmit: (e) => {
            e.preventDefault();
            finish(input.value.trim() || null);
          },
        },
        h("h2", {}, title),
        input,
        h(
          "div",
          { class: "dialog-buttons" },
          h("button", { class: "primary", type: "submit" }, "Save"),
          h("button", { type: "button", onclick: () => finish(null) }, "Cancel")
        )
      )
    );
    let done = false;
    function finish(value) {
      if (done) return;
      done = true;
      dlg.close();
      dlg.remove();
      resolve(value);
    }
    dlg.addEventListener("cancel", () => finish(null));
    document.body.append(dlg);
    dlg.showModal();
    input.select();
  });
}

function editCustomer(customer) {
  return new Promise((resolve) => {
    const name = h("input", { value: customer.name, required: true, maxLength: 200 });
    const rate = h("input", {
      type: "number",
      min: "0",
      step: "0.01",
      inputMode: "decimal",
      value: customer.hourly_rate === null ? "" : String(customer.hourly_rate),
      placeholder: S.prefs.default_rate ? `Default: ${fmtMoney(S.prefs.default_rate)}` : "Default rate",
    });
    const dlg = h(
      "dialog",
      { class: "narrow" },
      h(
        "form",
        {
          onsubmit: (e) => {
            e.preventDefault();
            finish({ name: name.value.trim(), hourly_rate: rate.value === "" ? null : Number(rate.value) });
          },
        },
        h("h2", {}, "Edit customer"),
        h("label", {}, "Name", name),
        h("label", {}, `Hourly rate (${currency()})`, rate),
        h("p", { class: "hint" }, "Leave empty to use the default rate from Settings."),
        h(
          "div",
          { class: "dialog-buttons" },
          h("button", { class: "primary", type: "submit" }, "Save"),
          h("button", { type: "button", onclick: () => finish(null) }, "Cancel")
        )
      )
    );
    let done = false;
    function finish(value) {
      if (done) return;
      done = true;
      dlg.close();
      dlg.remove();
      resolve(value);
    }
    dlg.addEventListener("cancel", () => finish(null));
    document.body.append(dlg);
    dlg.showModal();
    name.select();
  });
}

// Resolves true when the action is chosen. Without a label it only informs, with a Close button.
function confirmAction(message, label = "Delete", danger = true) {
  return new Promise((resolve) => {
    const dlg = h(
      "dialog",
      { class: "narrow" },
      h(
        "div",
        { class: "dialog-body" },
        h("p", {}, message),
        h(
          "div",
          { class: "dialog-buttons" },
          label ? h("button", { class: danger ? "danger" : "primary", type: "button", onclick: () => finish(true) }, label) : null,
          h("button", { type: "button", onclick: () => finish(false) }, label ? "Cancel" : "Close")
        )
      )
    );
    function finish(value) {
      dlg.close();
      dlg.remove();
      resolve(value);
    }
    dlg.addEventListener("cancel", (e) => {
      e.preventDefault();
      finish(false);
    });
    document.body.append(dlg);
    dlg.showModal();
  });
}

// --- Customer > Project picker ---

function projectPicker(selectedProjectId) {
  const customer = h("select", { "aria-label": "Customer", required: true });
  const project = h("select", { "aria-label": "Project", required: true });
  const pick = { customer: null, project: null };
  // Locked while a new item saves, so a project can't be created under the old customer.
  let saving = false;

  function select(projectId) {
    const selected = projectPath(projectId);
    pick.customer = selected.customer ? selected.customer.id : null;
    pick.project = selected.project ? selected.project.id : null;
  }

  function fill(el, items, value, kindLabel, enabled) {
    const options = [h("option", { value: "" }, `Select ${kindLabel}…`)];
    for (const item of items) {
      options.push(h("option", { value: String(item.id) }, item.name));
    }
    if (enabled) options.push(h("option", { value: NEW }, `+ New ${kindLabel}…`));
    el.replaceChildren(...options);
    el.value = value === null ? "" : String(value);
    el.disabled = !enabled;
  }

  const visible = (items, current) => items.filter((item) => !item.archived || item.id === current);

  function render() {
    const customers = visible(S.tree.customers, pick.customer);
    const projects = visible(
      S.tree.projects.filter((p) => p.customer_id === pick.customer),
      pick.project
    );
    fill(customer, customers, pick.customer, "customer", !saving);
    fill(project, projects, pick.project, "project", !saving && pick.customer !== null);
  }

  async function create(kind, label, parentKey, parentId) {
    const name = await askName(`New ${label}`);
    if (!name) return null;
    const body = { name };
    if (parentKey) body[parentKey] = parentId;
    saving = true;
    render();
    try {
      const item = await api(`/api/${kind}`, "POST", body);
      await loadTree();
      return item.id;
    } finally {
      saving = false;
    }
  }

  async function changed(level, el) {
    let value = el.value === "" ? null : el.value;
    if (value === NEW) {
      if (level === "customer") value = await create("customers", "customer");
      if (level === "project") value = await create("projects", "project", "customer_id", pick.customer);
      if (value === null) value = pick[level];
    } else if (value !== null) {
      value = Number(value);
    }
    pick[level] = value;
    if (level === "customer") pick.project = null;
    render();
  }

  customer.addEventListener("change", guard(() => changed("customer", customer).finally(render)));
  project.addEventListener("change", guard(() => changed("project", project).finally(render)));
  select(selectedProjectId);
  render();

  return {
    el: h("div", { class: "picker" }, h("label", {}, "Customer", customer), h("label", {}, "Project", project)),
    value: () => pick.project,
    set: (projectId) => {
      select(projectId);
      render();
    },
  };
}

// --- Entry editor ---

function openEntryEditor({ entry = null, onSaved }) {
  const picker = projectPicker(entry ? entry.project_id : lastUsableProject());
  const billable = h("input", { type: "checkbox", checked: entry ? entry.billable : true });
  const description = descriptionInput({
    value: entry ? entry.description : "",
    placeholder: "What did you work on?",
    picker,
    billable,
  });
  const rowsEl = h("div", { class: "dialog-body" });
  const totalEl = h("span", { class: "muted num" });

  function updateTotal() {
    let total = 0;
    for (const row of rowsEl.children) {
      const seconds = row.readSegment().seconds;
      row.durationEl.textContent = seconds === null ? "" : fmtHM(seconds);
      total += seconds || 0;
    }
    totalEl.textContent = `Total ${fmtHM(total)}`;
  }

  function addRow(seg) {
    const isOpen = seg.end === null;
    const start = isoInput({ withTime: true, required: true, value: toInput(seg.start) });
    const end = isoInput({
      withTime: true,
      required: !isOpen,
      disabled: isOpen,
      value: isOpen ? "" : toInput(seg.end),
      placeholder: isOpen ? "" : DATETIME_FORMAT,
    });
    const originals = { start: start.value, end: end.value };
    const durationEl = h("span", { class: "num muted" });
    const remove = h(
      "button",
      {
        type: "button",
        title: "Remove this period",
        "aria-label": "Remove this period",
        hidden: isOpen,
        onclick: () => {
          row.remove();
          updateTotal();
        },
      },
      "×"
    );
    const row = h(
      "div",
      { class: "segment-row" },
      h("label", {}, "Start", start.field),
      h("label", {}, isOpen ? "End (running)" : "End", end.field),
      durationEl,
      remove
    );
    row.durationEl = durationEl;
    row.readSegment = () => {
      // Inputs show whole minutes; an untouched field keeps its exact seconds.
      const startTs = start.value === originals.start ? seg.start : fromInput(start.value);
      const endTs = isOpen ? null : end.value === originals.end ? seg.end : fromInput(end.value);
      const usable = !Number.isNaN(startTs) && !Number.isNaN(endTs) && start.value && (isOpen || end.value);
      return {
        start: startTs,
        end: endTs,
        seconds: !usable ? null : Math.max(0, (isOpen ? now() : endTs) - startTs),
      };
    };
    start.addEventListener("input", updateTotal);
    start.addEventListener("change", () => {
      // Moving the start to another day carries the end along.
      const startDay = start.value.trim().slice(0, 10);
      const endValue = end.value.trim();
      if (!isOpen && !Number.isNaN(fromInput(start.value)) && !Number.isNaN(fromInput(endValue)) && startDay !== endValue.slice(0, 10)) {
        end.value = `${startDay}${endValue.slice(10)}`;
        end.dispatchEvent(new Event("input"));
      }
    });
    end.addEventListener("input", updateTotal);
    // A running entry's open period stays last.
    const openRow = [...rowsEl.children].find((r) => r.readSegment().end === null);
    if (openRow) rowsEl.insertBefore(row, openRow);
    else rowsEl.append(row);
  }

  if (entry) {
    entry.segments.forEach(addRow);
  } else {
    const t = now();
    addRow({ start: t - 3600, end: t });
  }

  function close() {
    dlg.close();
    dlg.remove();
  }

  const save = guard(async (e) => {
    e.preventDefault();
    const projectId = picker.value();
    if (!projectId) throw new Error("Select a customer and project.");
    const body = {
      project_id: projectId,
      description: description.value,
      billable: billable.checked,
      segments: [...rowsEl.children].map((row) => {
        const { start, end } = row.readSegment();
        return { start, end };
      }),
    };
    if (entry) await api(`/api/entries/${entry.id}`, "PUT", body);
    else await api("/api/entries", "POST", body);
    localStorage.setItem(LAST_PROJECT_KEY, String(projectId));
    close();
    await onSaved();
  });

  const removeEntry = guard(async () => {
    if (!(await confirmAction("Delete this entry and its logged time?"))) return;
    await api(`/api/entries/${entry.id}`, "DELETE");
    close();
    await onSaved();
  });

  const dlg = h(
    "dialog",
    {},
    h(
      "form",
      { onsubmit: save },
      h("h2", {}, entry ? "Edit entry" : "Add time"),
      picker.el,
      h("label", {}, "Description", description),
      h("label", { class: "check" }, billable, "Billable"),
      rowsEl,
      h(
        "div",
        { class: "row" },
        h(
          "button",
          {
            type: "button",
            class: "small",
            onclick: () => {
              const latest = [...rowsEl.children]
                .map((row) => row.readSegment())
                .filter((seg) => seg.end !== null && !Number.isNaN(seg.end))
                .map((seg) => seg.end);
              const start = latest.length ? Math.max(...latest) : now() - 3600;
              addRow({ start, end: start + 1800 });
              updateTotal();
            },
          },
          "+ Add period"
        ),
        h("span", { class: "spacer" }),
        totalEl
      ),
      h(
        "div",
        { class: "dialog-buttons" },
        h("button", { class: "primary", type: "submit" }, "Save"),
        h("button", { type: "button", onclick: close }, "Cancel"),
        h("span", { class: "spacer" }),
        entry && h("button", { class: "danger", type: "button", onclick: removeEntry }, "Delete")
      )
    )
  );
  dlg.addEventListener("cancel", () => dlg.remove());
  updateTotal();
  document.body.append(dlg);
  dlg.showModal();
}

// --- Timer page ---

// Groups are opened by their key, which stays the same across reloads of the list.
const timerPage = { entries: [], hasMore: false, expanded: new Set() };

function applyTimer(data) {
  S.active = data.active;
  S.offset = data.now - Math.floor(Date.now() / 1000);
  updateTitle();
}

async function refreshTimer() {
  applyTimer(await api("/api/timer"));
  if (S.route === "timer") renderTimerCard();
}

function updateTitle() {
  document.title = S.active ? `${S.active.state === "paused" ? "Paused" : fmtClock(entrySeconds(S.active))} · Timelog` : "Timelog";
}

function renderTimerCard() {
  const host = document.getElementById("timer-card");
  if (!host) return;
  host.replaceChildren(S.active ? activeTimerCard() : startForm());
}

function startForm() {
  const picker = projectPicker(lastUsableProject());
  const billable = h("input", { type: "checkbox", checked: true });
  const description = descriptionInput({ placeholder: "What are you working on?", picker, billable });
  return h(
    "form",
    {
      class: "timer-form",
      onsubmit: guard(async (e) => {
        e.preventDefault();
        const projectId = picker.value();
        if (!projectId) throw new Error("Select a customer and project.");
        await startTimer({ project_id: projectId, description: description.value, billable: billable.checked });
      }),
    },
    picker.el,
    h(
      "div",
      { class: "timer-actions" },
      h("label", {}, "Description", description),
      h("label", { class: "check" }, billable, "Billable"),
      h("button", { class: "primary", type: "submit" }, "Start")
    )
  );
}

async function startTimer(body) {
  applyTimer(await api("/api/timer/start", "POST", body));
  localStorage.setItem(LAST_PROJECT_KEY, String(body.project_id));
  renderTimerCard();
  await loadEntries();
}

function timerAction(action) {
  return guard(async () => {
    applyTimer(await api(`/api/timer/${action}`, "POST"));
    renderTimerCard();
    if (action === "stop") await loadEntries();
  });
}

function activeTimerCard() {
  const entry = S.active;
  const paused = entry.state === "paused";
  return h(
    "div",
    { class: `active-timer${paused ? " paused" : ""}` },
    h("div", {}, h("div", { class: "status" }, paused ? "Paused" : "Running"), h("div", { class: "elapsed", id: "elapsed" }, fmtClock(entrySeconds(entry)))),
    h(
      "div",
      { class: "active-info" },
      h("div", {}, entry.description || h("span", { class: "muted" }, "(no description)")),
      h("div", { class: "entry-sub" }, pathLabel(entry.project_id))
    ),
    h(
      "div",
      { class: "active-buttons" },
      h("button", { type: "button", onclick: () => openEntryEditor({ entry, onSaved: async () => { await refreshTimer(); await loadEntries(); } }) }, "Edit"),
      paused
        ? h("button", { type: "button", class: "primary", onclick: timerAction("resume") }, "Resume")
        : h("button", { type: "button", onclick: timerAction("pause") }, "Pause"),
      h("button", { type: "button", class: paused ? "" : "primary", onclick: timerAction("stop") }, "Stop")
    )
  );
}

async function loadEntries(append = false) {
  const offset = append ? timerPage.entries.length : 0;
  const limit = append ? 50 : Math.max(50, timerPage.entries.length);
  const data = await api(`/api/entries?limit=${Math.min(limit, 200)}&offset=${offset}`);
  timerPage.entries = append ? timerPage.entries.concat(data.entries) : data.entries;
  timerPage.hasMore = data.has_more;
  renderEntries();
  await loadRecent();
}

function dayTitle(ts) {
  const date = new Date(ts * 1000);
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  const iso = dateKey(date);
  if (useLocale()) {
    if (iso === dateKey(today)) return "Today";
    if (iso === dateKey(yesterday)) return "Yesterday";
    return date.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  }
  if (iso === dateKey(today)) return `Today · ${iso}`;
  if (iso === dateKey(yesterday)) return `Yesterday · ${iso}`;
  return `${WEEKDAYS[date.getDay()]} ${iso}`;
}

function segmentSummary(entry) {
  const firstDay = dateKey(new Date(entry.segments[0].start * 1000));
  return entry.segments
    .map((seg) => {
      const day = dateKey(new Date(seg.start * 1000));
      const prefix = day === firstDay ? "" : `${fmtDate(seg.start)} `;
      return `${prefix}${fmtTime(seg.start)}–${fmtTime(seg.end)}`;
    })
    .join(", ");
}

function renderEntries() {
  const host = document.getElementById("entries");
  if (!host) return;
  if (!timerPage.entries.length) {
    host.replaceChildren(h("p", { class: "empty" }, "No logged time yet."));
    return;
  }
  const days = new Map();
  for (const entry of timerPage.entries) {
    const key = dateKey(new Date(entry.segments[0].start * 1000));
    if (!days.has(key)) days.set(key, []);
    days.get(key).push(entry);
  }
  const blocks = [...days.values()].map((entries) =>
    h(
      "section",
      { class: "day" },
      h(
        "div",
        { class: "day-head" },
        h("span", {}, dayTitle(entries[0].segments[0].start)),
        h("span", { class: "num" }, fmtClock(entries.reduce((sum, e) => sum + entrySeconds(e), 0)))
      ),
      groupSameWork(entries).map((group) => (group.entries.length === 1 ? entryRow(group.entries[0]) : groupRows(group)))
    )
  );
  if (timerPage.hasMore) {
    blocks.push(h("div", { class: "row" }, h("span", { class: "spacer" }), h("button", { type: "button", onclick: guard(() => loadEntries(true)) }, "Load more"), h("span", { class: "spacer" })));
  }
  host.replaceChildren(...blocks);
}

// Within one day, entries with the same description, project and billable flag show as one row.
function groupSameWork(entries) {
  const groups = new Map();
  for (const entry of entries) {
    const day = dateKey(new Date(entry.segments[0].start * 1000));
    const key = JSON.stringify([day, entry.project_id, entry.billable, entry.description]);
    if (!groups.has(key)) groups.set(key, { key, entries: [] });
    groups.get(key).entries.push(entry);
  }
  return [...groups.values()];
}

function continueButton(entry) {
  return h(
    "button",
    {
      type: "button",
      class: "small",
      title: "Start a timer with the same description and project",
      onclick: guard(() => startTimer({ project_id: entry.project_id, description: entry.description, billable: entry.billable })),
    },
    "▶ Continue"
  );
}

function editButton(entry) {
  return h("button", { type: "button", class: "small", onclick: () => openEntryEditor({ entry, onSaved: () => loadEntries() }) }, "Edit");
}

function deleteButton(entry) {
  return h(
    "button",
    {
      type: "button",
      class: "small danger",
      title: "Delete this entry",
      onclick: guard(async () => {
        const what = entry.description ? `"${entry.description}" (${fmtClock(entrySeconds(entry))})` : `this entry (${fmtClock(entrySeconds(entry))})`;
        if (!(await confirmAction(`Delete ${what}?`))) return;
        await api(`/api/entries/${entry.id}`, "DELETE");
        await loadEntries();
      }),
    },
    "Delete"
  );
}

function entryLabel(entry) {
  return h("div", {}, entry.description || h("span", { class: "muted" }, "(no description)"), entry.billable ? null : h("span", { class: "tag" }, "Non-billable"));
}

function groupRows(group) {
  const first = group.entries[0];
  const open = timerPage.expanded.has(group.key);
  const segments = group.entries.flatMap((e) => e.segments);
  const start = Math.min(...segments.map((seg) => seg.start));
  const end = Math.max(...segments.map((seg) => seg.end));
  const toggle = () => {
    if (open) timerPage.expanded.delete(group.key);
    else timerPage.expanded.add(group.key);
    renderEntries();
  };
  const head = h(
    "div",
    { class: "entry group-head" },
    h(
      "button",
      {
        type: "button",
        class: "count",
        title: open ? "Hide the separate entries" : "Show the separate entries",
        "aria-expanded": String(open),
        onclick: toggle,
      },
      String(group.entries.length)
    ),
    h(
      "div",
      { class: "entry-main" },
      entryLabel(first),
      h("div", { class: "entry-sub" }, pathLabel(first.project_id)),
      h("div", { class: "entry-sub num" }, `${fmtTime(start)}–${fmtTime(end)}`)
    ),
    h("span", { class: "duration" }, fmtClock(group.entries.reduce((sum, e) => sum + entrySeconds(e), 0))),
    h("div", { class: "entry-actions" }, continueButton(first))
  );
  return open ? [head, ...group.entries.map((entry) => entryRow(entry, true))] : head;
}

function entryRow(entry, inGroup = false) {
  if (inGroup) {
    return h(
      "div",
      { class: "entry grouped" },
      h("div", { class: "entry-main" }, h("div", { class: "entry-sub num" }, segmentSummary(entry))),
      h("span", { class: "duration" }, fmtClock(entrySeconds(entry))),
      h("div", { class: "entry-actions" }, editButton(entry), deleteButton(entry))
    );
  }
  return h(
    "div",
    { class: "entry" },
    h(
      "div",
      { class: "entry-main" },
      entryLabel(entry),
      h("div", { class: "entry-sub" }, pathLabel(entry.project_id)),
      h("div", { class: "entry-sub num" }, segmentSummary(entry))
    ),
    h("span", { class: "duration" }, fmtClock(entrySeconds(entry))),
    h(
      "div",
      { class: "entry-actions" },
      continueButton(entry),
      editButton(entry),
      deleteButton(entry)
    )
  );
}

async function showTimer() {
  view.replaceChildren(
    h("section", { class: "card", id: "timer-card" }),
    h(
      "div",
      { class: "list-head" },
      h("h1", { class: "list-title" }, "Logged time"),
      h("button", { type: "button", onclick: () => openEntryEditor({ onSaved: () => loadEntries() }) }, "+ Add time")
    ),
    h("div", { id: "entries" })
  );
  await refreshTimer();
  await loadEntries();
}

// --- Reports page ---

function presetRange(preset) {
  const today = new Date();
  const y = today.getFullYear();
  const m = today.getMonth();
  const d = today.getDate();
  const mondayOffset = (today.getDay() + 6) % 7;
  switch (preset) {
    case "this-week": return [new Date(y, m, d - mondayOffset), new Date(y, m, d - mondayOffset + 6)];
    case "last-week": return [new Date(y, m, d - mondayOffset - 7), new Date(y, m, d - mondayOffset - 1)];
    case "this-month": return [new Date(y, m, 1), new Date(y, m + 1, 0)];
    case "last-month": return [new Date(y, m - 1, 1), new Date(y, m, 0)];
    case "past-2-months": return [new Date(y, m - 2, 1), new Date(y, m, 0)];
    case "past-3-months": return [new Date(y, m - 3, 1), new Date(y, m, 0)];
    case "this-year": return [new Date(y, 0, 1), new Date(y, 11, 31)];
    case "last-year": return [new Date(y - 1, 0, 1), new Date(y - 1, 11, 31)];
    default: return null;
  }
}

function loadReportForm() {
  const defaults = {
    preset: "this-month", from: "", to: "", customer: "", project: "",
    billable: "", archived: false, q: "", group1: "customer", group2: "project", group3: "description", group4: "", dialect: "standard",
  };
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem(REPORT_KEY) || "{}") };
  } catch {
    return defaults;
  }
}

function reportParams(form) {
  const params = new URLSearchParams({ tz: Intl.DateTimeFormat().resolvedOptions().timeZone });
  const range = presetRange(form.preset);
  const from = range ? dateKey(range[0]) : form.preset === "custom" ? form.from : "";
  const to = range ? dateKey(range[1]) : form.preset === "custom" ? form.to : "";
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  if (form.customer) params.set("customer_id", form.customer);
  if (form.project) params.set("project_id", form.project);
  if (form.billable) params.set("billable", form.billable);
  if (!form.archived) params.set("include_archived", "false");
  if (form.q.trim()) params.set("q", form.q.trim());
  const groups = [form.group1, form.group2, form.group3, form.group4].filter(Boolean);
  params.set("group", [...new Set(groups)].join(","));
  return params;
}

async function showReports() {
  const form = loadReportForm();
  const collapsed = new Set();
  const results = h("div", { class: "results" });
  let requestId = 0;

  const option = (value, label) => h("option", { value }, label);
  const archivedSuffix = (item) => (item.archived ? " (archived)" : "");

  function select(key, label, options, onchange) {
    const el = h("select", {}, options);
    el.value = form[key];
    if (el.value !== form[key]) {
      form[key] = "";
      el.value = "";
    }
    el.addEventListener("change", () => {
      form[key] = el.value;
      if (onchange) onchange();
      changed();
    });
    return h("label", {}, label, el);
  }

  const groupOptions = () => [
    option("", "None"), option("customer", "Customer"), option("project", "Project"), option("description", "Description"),
    option("day", "Day"), option("week", "Week"), option("month", "Month"),
  ];

  const controls = h("div", {});

  function renderControls() {
    // Archived customers and projects are only offered with "Show archived".
    const archivedCustomers = new Set(S.tree.customers.filter((c) => c.archived).map((c) => c.id));
    const customers = S.tree.customers.filter((c) => form.archived || !c.archived);
    const projects = S.tree.projects.filter(
      (p) =>
        (!form.customer || p.customer_id === Number(form.customer)) &&
        (form.archived || (!p.archived && !archivedCustomers.has(p.customer_id)))
    );
    const showArchived = h("input", { type: "checkbox", checked: form.archived });
    showArchived.addEventListener("change", () => {
      form.archived = showArchived.checked;
      renderControls();
      changed();
    });
    const customSelected = form.preset === "custom";

    const dateInput = (key) => {
      const el = isoInput({ value: form[key] });
      el.addEventListener("change", () => {
        const value = el.value.trim();
        if (value && !isDate(value)) {
          el.reportValidity();
          return;
        }
        form[key] = value;
        changed();
      });
      return el.field;
    };
    const search = h("input", { type: "search", value: form.q, placeholder: "Contains…" });
    let timer = null;
    search.addEventListener("input", () => {
      form.q = search.value;
      clearTimeout(timer);
      timer = setTimeout(run, 300);
    });

    controls.replaceChildren(
      h(
        "div",
        { class: "filters" },
        select("preset", "Period", [
          option("this-week", "This week"), option("last-week", "Last week"), option("this-month", "This month"),
          option("last-month", "Last month"), option("past-2-months", "Past two months"),
          option("past-3-months", "Past three months"), option("this-year", "This year"), option("last-year", "Last year"),
          option("all", "All time"), option("custom", "Custom range"),
        ], renderControls),
        customSelected ? h("label", {}, "From", dateInput("from")) : null,
        customSelected ? h("label", {}, "To", dateInput("to")) : null,
        select("billable", "Billable", [option("", "All"), option("1", "Billable"), option("0", "Non-billable")])
      ),
      h(
        "div",
        { class: "filters" },
        select("customer", "Customer", [option("", "All"), customers.map((c) => option(String(c.id), c.name + archivedSuffix(c)))], () => {
          form.project = "";
          renderControls();
        }),
        select("project", "Project", [option("", "All"), projects.map((p) => option(String(p.id), p.name + archivedSuffix(p)))]),
        h("label", {}, "Description", search),
        h("label", { class: "check" }, showArchived, "Show archived")
      ),
      h(
        "div",
        { class: "filters compact" },
        select("group1", "Group by", groupOptions()),
        select("group2", "Then by", groupOptions()),
        select("group3", "Then by", groupOptions()),
        select("group4", "Then by", groupOptions())
      )
    );
  }

  function renderRows(nodes, depth, path, out) {
    for (const node of nodes) {
      const key = `${path}/${node.label}`;
      const hasKids = node.children.length > 0;
      const closed = collapsed.has(key);
      out.push(
        h(
          "tr",
          { class: `level-${depth}` },
          h(
            "td",
            { class: `group-label level-${depth}` },
            hasKids
              ? h("button", {
                  type: "button",
                  class: "toggle",
                  "aria-label": closed ? "Expand" : "Collapse",
                  "aria-expanded": String(!closed),
                  onclick: () => {
                    if (closed) collapsed.delete(key);
                    else collapsed.add(key);
                    run();
                  },
                }, closed ? "▸" : "▾")
              : h("span", { class: "toggle-gap" }),
            node.label
          ),
          h("td", { class: "num" }, fmtHM(node.seconds)),
          h("td", { class: "num" }, fmtDecimal(node.seconds)),
          h("td", { class: "num" }, fmtDecimal(node.billable_seconds)),
          hasRates() ? h("td", { class: "num" }, fmtMoney(node.amount)) : null
        )
      );
      if (hasKids && !closed) renderRows(node.children, depth + 1, key, out);
    }
    return out;
  }

  function changed() {
    run();
  }

  async function run() {
    localStorage.setItem(REPORT_KEY, JSON.stringify(form));
    const mine = ++requestId;
    const params = reportParams(form);
    let data;
    try {
      data = await api(`/api/report?${params}`);
    } catch (err) {
      toast(err.message);
      return;
    }
    if (mine !== requestId) return;

    const csvLink = (mode) => {
      const p = new URLSearchParams(params);
      p.set("mode", mode);
      p.set("dialect", form.dialect);
      return `/api/report.csv?${p}`;
    };
    const dialect = h("select", {}, option("standard", "Comma, decimal point"), option("excel", "Semicolon, decimal comma"));
    dialect.value = form.dialect;
    dialect.addEventListener("change", () => {
      form.dialect = dialect.value;
      run();
    });

    const showAmount = hasRates();
    results.replaceChildren(
      h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "totals" },
          h("div", {}, h("span", {}, "Total"), h("strong", {}, fmtHM(data.total.seconds))),
          h("div", {}, h("span", {}, "Decimal hours"), h("strong", {}, fmtDecimal(data.total.seconds))),
          h(
            "div",
            {},
            h("span", {}, "Billable hours"),
            h("strong", {}, fmtDecimal(data.total.billable_seconds)),
            S.prefs.round_minutes ? h("small", { class: "muted" }, roundingNote()) : null
          ),
          showAmount ? h("div", {}, h("span", {}, `Amount (${currency()})`), h("strong", {}, fmtMoney(data.total.amount))) : null
        ),
        data.total.seconds === 0
          ? h("p", { class: "empty" }, "No time matches these filters.")
          : data.groups.length
            ? h(
                "div",
                { class: "table-wrap" },
                h(
                  "table",
                  {},
                  h("thead", {}, h("tr", {}, h("th", {}, "Group"), h("th", { class: "num" }, "Time"), h("th", { class: "num" }, "Hours"), h("th", { class: "num" }, "Billable"), showAmount ? h("th", { class: "num" }, "Amount") : null)),
                  h("tbody", {}, renderRows(data.groups, 0, "", []))
                )
              )
            : null,
        h(
          "div",
          { class: "row export" },
          h("label", {}, "CSV format", dialect),
          h("a", { class: "button", href: csvLink("detail"), download: "" }, "Export detail"),
          h("a", { class: "button", href: csvLink("summary"), download: "" }, "Export summary")
        )
      )
    );
  }

  await loadTree();
  renderControls();
  view.replaceChildren(h("h1", {}, "Reports"), h("section", { class: "card" }, controls), results);
  await run();
}

// --- Manage page ---

async function showManage() {
  let showArchived = false;
  const treeEl = h("ul", { class: "tree" });

  const rerender = async () => {
    await loadTree();
    render();
  };

  function itemRow(kind, item, addLabel, addFn) {
    const rename = guard(async () => {
      if (kind === "customers") {
        const changes = await editCustomer(item);
        if (!changes) return;
        await api(`/api/customers/${item.id}`, "PATCH", changes);
      } else {
        const name = await askName("Rename", item.name);
        if (!name) return;
        await api(`/api/${kind}/${item.id}`, "PATCH", { name });
      }
      await rerender();
    });
    const toggleArchive = guard(async () => {
      await api(`/api/${kind}/${item.id}`, "PATCH", { archived: !item.archived });
      await rerender();
    });
    const remove = guard(async () => {
      const what = kind === "customers" ? "customer" : "project";
      if (item.has_time) {
        // Logged time is never deleted, so the only way to retire it is archiving.
        const reason = `The ${what} "${item.name}" has logged time, so it can't be deleted.`;
        if (item.archived) {
          await confirmAction(`${reason} It is already archived, which keeps it out of the way; its history is in reports under "Show archived".`, null);
          return;
        }
        if (await confirmAction(`${reason} Archive it instead? It disappears from new timers and entries, and its history stays in reports under "Show archived".`, "Archive", false)) {
          await toggleArchive();
        }
        return;
      }
      const projects = kind === "customers" ? S.tree.projects.filter((p) => p.customer_id === item.id).length : 0;
      const question =
        kind === "customers"
          ? `Delete the customer "${item.name}"${projects ? ` and its ${projects === 1 ? "project" : `${projects} projects`}` : ""}?`
          : `Delete the project "${item.name}"?`;
      if (!(await confirmAction(question))) return;
      await api(`/api/${kind}/${item.id}`, "DELETE");
      await rerender();
    });
    return h(
      "div",
      { class: `tree-row kind-${kind}${item.archived ? " archived" : ""}` },
      h(
        "span",
        { class: "name" },
        item.name,
        kind === "customers" && item.hourly_rate !== null ? h("span", { class: "rate" }, `${fmtMoney(item.hourly_rate)}/h`) : null
      ),
      addFn ? h("button", { type: "button", class: "small", onclick: guard(addFn) }, addLabel) : null,
      h("button", { type: "button", class: "small", onclick: rename }, kind === "customers" ? "Edit" : "Rename"),
      h("button", { type: "button", class: "small", onclick: toggleArchive }, item.archived ? "Restore" : "Archive"),
      h("button", { type: "button", class: "small danger", onclick: remove }, "Delete")
    );
  }

  function addChild(kind, label, parentKey, parentId) {
    return async () => {
      const name = await askName(`New ${label}`);
      if (!name) return;
      const body = { name };
      if (parentKey) body[parentKey] = parentId;
      await api(`/api/${kind}`, "POST", body);
      await rerender();
    };
  }

  function render() {
    const shown = (item) => showArchived || !item.archived;
    const customers = S.tree.customers.filter(shown);
    treeEl.replaceChildren(
      ...customers.map((customer) =>
        h(
          "li",
          {},
          itemRow("customers", customer, "+ Project", addChild("projects", "project", "customer_id", customer.id)),
          h(
            "ul",
            {},
            S.tree.projects.filter((p) => p.customer_id === customer.id && shown(p)).map((project) =>
              h(
                "li",
                {},
                itemRow("projects", project)
              )
            )
          )
        )
      )
    );
    if (!customers.length) treeEl.replaceChildren(h("li", { class: "empty" }, "No customers yet."));
  }

  const archivedToggle = h("input", { type: "checkbox" });
  archivedToggle.addEventListener("change", () => {
    showArchived = archivedToggle.checked;
    render();
  });

  await loadTree();
  render();
  view.replaceChildren(
    h(
      "div",
      { class: "list-head" },
      h("h1", { class: "list-title" }, "Customers and projects"),
      h("button", { type: "button", onclick: guard(addChild("customers", "customer")) }, "+ Customer")
    ),
    h("label", { class: "check archived-toggle" }, archivedToggle, "Show archived"),
    h("section", { class: "card manage-card" }, treeEl)
  );
}

// --- Settings ---

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  try {
    localStorage.setItem("timelog.theme", theme);
  } catch {}
}

async function loadPreferences() {
  S.prefs = await api("/api/preferences");
  applyTheme(S.prefs.theme);
}

function roundingNote() {
  const unit = {
    entry: "per entry",
    day: "per day, project and description",
    project: "per project",
  }[S.prefs.round_scope];
  return `Rounded up to started ${S.prefs.round_minutes} min ${unit}`;
}

async function showSettings() {
  const status = h("p", { class: "hint", role: "status" });
  const rateText = h("span");
  const refresh = () => {
    applyTheme(S.prefs.theme);
    scope.disabled = !S.prefs.round_minutes;
    rateText.textContent = `Default hourly rate (${currency()})`;
  };

  function choice(key, label, options) {
    const el = h("select", {}, options.map(([value, text]) => h("option", { value: String(value) }, text)));
    el.value = String(S.prefs[key]);
    el.addEventListener(
      "change",
      guard(async () => {
        const value = key === "round_minutes" ? Number(el.value) : el.value;
        S.prefs = await api("/api/preferences", "PUT", { ...S.prefs, [key]: value });
        refresh();
        status.textContent = "Saved.";
      })
    );
    return { el, label: h("label", {}, label, el) };
  }

  const theme = choice("theme", "Theme", [["system", "System"], ["light", "Day"], ["dark", "Night"]]);
  const dates = choice("date_format", "Date and time format", [
    ["iso", "ISO only (2026-11-01 14:30)"],
    ["locale", "Browser locale"],
  ]);
  const step = choice("round_minutes", "Round billable time up to", [
    [0, "No rounding"], [5, "Started 5 minutes"], [6, "Started 6 minutes"], [10, "Started 10 minutes"],
    [15, "Started 15 minutes"], [30, "Started 30 minutes"], [60, "Started hour"],
  ]);
  const unit = choice("round_scope", "Round each", [
    ["entry", "Entry"],
    ["day", "Day's work on one project and description"],
    ["project", "Project (its total in the report)"],
  ]);
  const scope = unit.el;

  const money = choice("currency", "Currency", CURRENCIES.map((code) => [code, currencyName(code)]));
  const rate = h("input", { type: "number", min: "0", step: "0.01", value: S.prefs.default_rate || "", placeholder: "0", inputMode: "decimal" });
  rate.addEventListener(
    "change",
    guard(async () => {
      const value = rate.value === "" ? 0 : Number(rate.value);
      if (!rate.checkValidity() || Number.isNaN(value)) {
        rate.reportValidity();
        return;
      }
      S.prefs = await api("/api/preferences", "PUT", { ...S.prefs, default_rate: value });
      status.textContent = "Saved.";
    })
  );

  refresh();
  view.replaceChildren(
    h("h1", {}, "Settings"),
    h(
      "section",
      { class: "card settings-card" },
      h("fieldset", {}, h("legend", {}, "Appearance"), h("div", { class: "two" }, theme.label, dates.label)),
      h(
        "fieldset",
        {},
        h("legend", {}, "Billing"),
        h("div", { class: "two" }, step.label, unit.label),
        h("div", { class: "two" }, money.label, h("p", { class: "hint align-end" }, "Used when showing rates and amounts.")),
        h(
          "div",
          { class: "two" },
          h("label", {}, rateText, rate),
          h("p", { class: "hint align-end" }, "Customers without their own rate use this. Set a customer's rate under Manage → Edit.")
        ),
        h(
          "p",
          { class: "hint" },
          "Rounding changes billable hours and amounts in reports and CSV exports. Tracked time is never changed. ",
          "Rounding by day adds up short sessions on the same work before rounding, so 3 × 10 minutes bills as one started 30 minutes. ",
          "Rounding by project adds up all of a project's time in the report's date range and rounds it once. ",
          "Rounding is done within each row of a report, so every row bills whole steps: grouped by month, a project is rounded once per month."
        )
      ),
      status
    ),
    h(
      "section",
      { class: "card settings-card about" },
      h(
        "fieldset",
        {},
        h("legend", {}, "Your data"),
        h(
          "p",
          {},
          "Download everything you have in Timelog as a SQLite database, to run Timelog on your own server with the same data. It holds only your own data. ",
          h("a", { href: `${REPO_URL}#move-your-data-to-your-own-server`, target: "_blank", rel: "noopener noreferrer" }, "How to use it")
        ),
        h("p", {}, h("a", { class: "button", href: "/api/export.sqlite", download: "" }, "Download my data (SQLite)"))
      ),
      clockifyImport(),
      h(
        "fieldset",
        {},
        h("legend", {}, "About"),
        h(
          "p",
          {},
          "Timelog is made by ",
          h("a", { href: "https://subsite.fi", target: "_blank", rel: "noopener noreferrer" }, "SubSite"),
          "."
        ),
        h("p", {}, "Timelog is free software: you can share and change it under the GNU General Public License, version 3 or later."),
        h(
          "p",
          {},
          "Source code, issues and the license: ",
          h("a", { href: REPO_URL, target: "_blank", rel: "noopener noreferrer" }, REPO_URL.replace("https://", ""))
        )
      )
    )
  );
}

const REPO_URL = "https://github.com/subsite/timelog";

// Settings → Import from Clockify: preview what would be imported, then import it.
function clockifyImport() {
  const key = h("input", { type: "password", autocomplete: "off", spellcheck: false, placeholder: "Paste the key here" });
  const region = h(
    "select",
    {},
    [["global", "Global (most workspaces)"], ["euc1", "EU (Germany)"], ["use2", "USA"], ["euw2", "UK"], ["apse2", "Australia"]].map(
      ([value, text]) => h("option", { value }, text)
    )
  );
  const previewButton = h("button", { type: "button" }, "Preview");
  const result = h("div", { class: "import-result", role: "status" });

  async function send(dryRun) {
    if (!key.value.trim()) {
      key.focus();
      return null;
    }
    previewButton.disabled = true;
    result.querySelectorAll("button").forEach((b) => (b.disabled = true));
    result.prepend(h("p", { class: "hint" }, "Fetching your entries from Clockify…"));
    try {
      return await api("/api/import/clockify", "POST", { api_key: key.value, region: region.value, dry_run: dryRun });
    } catch (err) {
      result.replaceChildren(h("p", { class: "form-error" }, err.message));
      return null;
    } finally {
      previewButton.disabled = false;
    }
  }

  const names = (label, list) =>
    list.length ? h("p", {}, `${label} (${list.length}): `, h("span", { class: "muted" }, list.join(", "))) : null;

  async function preview() {
    const data = await send(true);
    if (!data) return;
    const range = data.first ? ` from ${fmtDate(data.first)} to ${fmtDate(data.last)}` : "";
    const skipped = [
      data.already_imported ? `${data.already_imported} already imported` : null,
      data.running ? `${data.running} running timer` : null,
      data.invalid ? `${data.invalid} without valid times` : null,
    ].filter(Boolean);
    const importButton = h("button", { type: "button", class: "primary" }, `Import ${data.entries} entries`);
    importButton.addEventListener("click", guard(runImport));
    result.replaceChildren(
      h("p", {}, `Workspace ${data.workspace ? `“${data.workspace}”` : ""}: ${data.found} entries${range}.`),
      h("p", {}, h("strong", {}, `${data.entries} to import`), skipped.length ? `; skipped: ${skipped.join(", ")}.` : "."),
      names("New customers", data.new_customers),
      names("New projects", data.new_projects),
      data.entries ? h("p", {}, importButton) : h("p", { class: "hint" }, "Nothing new to import.")
    );
  }

  async function runImport() {
    const data = await send(false);
    if (!data) return;
    key.value = "";
    await loadTree();
    result.replaceChildren(
      h("p", {}, h("strong", {}, `Imported ${data.entries} entries`), data.new_customers.length || data.new_projects.length
        ? `, with ${data.new_customers.length} new customers and ${data.new_projects.length} new projects.`
        : "."),
      h("p", { class: "hint" }, "You can now delete the API key in Clockify. Set hourly rates for the new customers under Manage.")
    );
  }

  previewButton.addEventListener("click", guard(preview));
  return h(
    "fieldset",
    {},
    h("legend", {}, "Import from Clockify"),
    h(
      "p",
      {},
      "Copy your time entries from Clockify into Timelog. In Clockify, create an API key under Preferences → Advanced → Manage API keys and paste it here. ",
      "The key is used for this import only and is not saved. Entries imported before are skipped, so you can import again later."
    ),
    h(
      "div",
      { class: "two" },
      h("label", {}, "Clockify API key", key),
      h("label", {}, "Workspace region", region)
    ),
    h("p", { class: "hint" }, "Choose a region only if your Clockify workspace is stored in one. If the key is not accepted, try your region."),
    h("p", {}, previewButton),
    result
  );
}

// --- Routing and clock ---

const routes = { timer: showTimer, reports: showReports, manage: showManage, settings: showSettings };

const navigate = guard(async () => {
  const route = location.hash.slice(1);
  S.route = routes[route] ? route : "timer";
  for (const link of document.querySelectorAll("[data-route]")) {
    link.classList.toggle("current", link.dataset.route === S.route);
  }
  await loadTree();
  await routes[S.route]();
});

setInterval(() => {
  if (!S.active) return;
  updateTitle();
  const elapsed = document.getElementById("elapsed");
  if (elapsed && S.active.state === "running") elapsed.textContent = fmtClock(entrySeconds(S.active));
}, 1000);

// Another device may have changed the timer while this tab was in the background.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !document.querySelector("dialog[open]")) {
    guard(refreshTimer)();
  }
});

window.addEventListener("hashchange", navigate);
guard(async () => {
  await loadPreferences();
  applyTimer(await api("/api/timer"));
  document.getElementById("whoami").textContent = (await api("/api/me")).email;
})().then(navigate);
