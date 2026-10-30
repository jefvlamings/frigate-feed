const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

class FakeShadowRoot {
  set innerHTML(html) {
    this.html = html;
    this.inputs = null;
  }

  querySelector() {
    return { addEventListener() {} };
  }

  querySelectorAll(selector) {
    if (selector !== "input[data-setting]") return [];
    if (this.inputs) return this.inputs;
    this.inputs = [...this.html.matchAll(/<input type="checkbox" data-setting="([^"]+)" data-label="([^"]+)"[^>]*>/g)].map((match) => {
      const input = {
        dataset: { setting: match[1], label: match[2] },
        checked: / checked(?:\s|>)/.test(match[0]),
        disabled: / disabled(?:\s|>)/.test(match[0]),
        listeners: {},
        addEventListener(name, listener) { this.listeners[name] = listener; },
        change(checked) {
          this.checked = checked;
          this.listeners.change({ target: this });
        },
      };
      return input;
    });
    return this.inputs;
  }
}

class HTMLElement {
  attachShadow() {
    this.shadowRoot = new FakeShadowRoot();
    return this.shadowRoot;
  }
}
const elements = new Map();
const context = {
  HTMLElement,
  customElements: {
    get: (name) => elements.get(name),
    define: (name, value) => elements.set(name, value),
  },
  window: { customCards: [] },
  document: { documentElement: { lang: "en" } },
  ResizeObserver: undefined,
  CustomEvent: class CustomEvent { constructor(type, options) { this.type = type; this.detail = options.detail; } },
  console,
};
vm.runInNewContext(fs.readFileSync("custom_components/frigate_feed/frontend/frigate-feed-card.js", "utf8"), context);
const Card = elements.get("frigate-feed-card");
const Editor = elements.get("frigate-feed-card-editor");

function makeCard(config) {
  const card = Object.create(Card.prototype);
  card._render = () => {};
  card._loadItems = () => {};
  card._items = [];
  card._filteredItems = [];
  card._page = 1;
  card._pageSize = () => 20;
  card._retentionHours = 24;
  card._hasLoaded = false;
  card._filtersInitialized = false;
  card._applyFilters = Card.prototype._applyFilters.bind(card);
  card.setConfig(config);
  return card;
}

function applyItems(card) {
  card._applyListResult({ enabled_labels: ["person", "pet", "vehicle"], items: [] });
}

test("legacy labels still choose initial filters and unsupported types stay absent", () => {
  const card = makeCard({ labels: ["animal"] });
  applyItems(card);
  assert.deepEqual(Array.from(card._activeLabels), ["pet"]);
  assert.deepEqual(Array.from(card._availableLabels), ["person", "pet", "vehicle"]);
});

test("default config keeps all labels and backend disabled labels are excluded", () => {
  const card = makeCard({});
  card._applyListResult({ enabled_labels: ["person", "vehicle"], items: [] });
  assert.deepEqual(Array.from(card._availableLabels), ["person", "vehicle"]);
  assert.deepEqual(Array.from(card._activeLabels), ["person", "vehicle"]);
});

test("enabled and visible labels independently control filters and initial selection", () => {
  const card = makeCard({ enabled_labels: ["person", "animal", "vehicle"], visible_labels: ["person"] });
  applyItems(card);
  assert.deepEqual(Array.from(card._availableLabels), ["person", "pet", "vehicle"]);
  assert.deepEqual(Array.from(card._activeLabels), ["person"]);
  card._applyListResult({
    enabled_labels: ["person", "pet", "vehicle"],
    items: [{ id: "p", label: "person" }, { id: "a", label: "pet" }, { id: "v", label: "vehicle" }],
  });
  assert.deepEqual(card._filteredItems.map((item) => item.id), ["p"]);
  card._toggleLabelFilter("pet");
  assert.deepEqual(card._filteredItems.map((item) => item.id), ["p", "a"]);
  card._toggleLabelFilter("vehicle");
  assert.deepEqual(card._filteredItems.map((item) => item.id), ["p", "a", "v"]);
});

test("empty enabled labels show no filters, and empty visible labels leave all enabled filters off", () => {
  const disabled = makeCard({ enabled_labels: [], visible_labels: [] });
  applyItems(disabled);
  assert.deepEqual(Array.from(disabled._availableLabels), []);
  assert.deepEqual(Array.from(disabled._activeLabels), []);

  const hidden = makeCard({ enabled_labels: ["person", "pet"], visible_labels: [] });
  applyItems(hidden);
  assert.deepEqual(Array.from(hidden._availableLabels), ["person", "pet"]);
  assert.deepEqual(Array.from(hidden._activeLabels), []);
});

test("refresh keeps filter changes while dropping labels that are no longer enabled", () => {
  const card = makeCard({ enabled_labels: ["person", "pet", "vehicle"], visible_labels: ["person"] });
  applyItems(card);
  card._activeLabels.add("vehicle");
  card._applyListResult({ enabled_labels: ["person", "pet"], items: [] });
  assert.deepEqual(Array.from(card._activeLabels), ["person"]);
});

test("editor saves independent checkbox settings, rerenders disabled visible, and keeps sequential edits", () => {
  const editor = new Editor();
  const changes = [];
  editor.dispatchEvent = (event) => changes.push(event.detail.config);
  editor.setConfig({});
  let inputs = editor.shadowRoot.querySelectorAll("input[data-setting]");
  assert.equal(inputs.length, 6);
  assert.match(editor.shadowRoot.html, /aria-label="Animal: Enable"/);
  assert.equal(inputs.every((input) => input.checked), true);

  inputs.find((input) => input.dataset.setting === "enabled_labels" && input.dataset.label === "pet").change(false);
  assert.deepEqual(Array.from(changes.at(-1).enabled_labels), ["person", "vehicle"]);
  assert.deepEqual(Array.from(changes.at(-1).visible_labels), ["person", "vehicle"]);
  inputs = editor.shadowRoot.querySelectorAll("input[data-setting]");
  assert.equal(inputs.find((input) => input.dataset.setting === "visible_labels" && input.dataset.label === "pet").disabled, true);

  inputs.find((input) => input.dataset.setting === "visible_labels" && input.dataset.label === "vehicle").change(false);
  assert.deepEqual(Array.from(changes.at(-1).enabled_labels), ["person", "vehicle"]);
  assert.deepEqual(Array.from(changes.at(-1).visible_labels), ["person"]);

  editor.hass = { language: "nl" };
  assert.match(editor.shadowRoot.html, /Inschakelen/);
  assert.match(editor.shadowRoot.html, /Zichtbaar/);
  assert.match(editor.shadowRoot.html, /aria-label="Dier: Inschakelen"/);
});
