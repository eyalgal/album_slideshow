const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const filename = path.join(__dirname, "../custom_components/album_slideshow/www/album-slideshow-card.js");
const source = fs.readFileSync(filename, "utf8");
const context = vm.createContext({ console });
vm.runInContext(source.slice(0, source.indexOf("\ndefineAlbumSlideshowCards();")), context);
const PhotoControls = vm.runInContext("PhotoControls", context);
const Card = vm.runInContext("createAlbumSlideshowCardClass(class {})", context);
const PhotoControlsReveal = vm.runInContext("PhotoControlsReveal", context);

function revealFixture() {
  let clock = 0;
  let nextTimer = 0;
  const timers = new Map();
  context.setTimeout = (callback, delay) => { const id = ++nextTimer; timers.set(id, { at: clock + delay, callback }); return id; };
  context.clearTimeout = id => timers.delete(id);
  const advance = milliseconds => {
    const until = clock + milliseconds;
    while (true) {
      const next = [...timers.entries()].filter(([, timer]) => timer.at <= until).sort((first, second) => first[1].at - second[1].at)[0];
      if (!next) break;
      clock = next[1].at;
      timers.delete(next[0]);
      next[1].callback();
    }
    clock = until;
  };
  const ownerDocument = new EventTarget();
  const card = new EventTarget();
  card.ownerDocument = ownerDocument;
  card.keyboardFocused = false;
  card.matches = () => card.keyboardFocused;
  card.focus = () => { card.keyboardFocused = true; };
  const container = new EventTarget();
  const button = { focus() { container.keyboardFocused = true; } };
  container.shadowRoot = { querySelector: selector => selector === ":focus-visible" ? container.keyboardFocused && button : button };
  const controls = { active: false, close() { this.active = false; } };
  let resumes = 0;
  const reveal = new PhotoControlsReveal(card, container, controls, () => { resumes += 1; });
  const fire = (type, properties = {}, target = card, path = [card]) => {
    const event = new Event(type, { cancelable: true });
    Object.assign(event, { pointerType: "touch", pointerId: 1, isPrimary: true, button: 0, clientX: 0, clientY: 0, detail: 1, ...properties });
    event.composedPath = () => path;
    target.dispatchEvent(event);
    return event;
  };
  return { card, container, controls, reveal, ownerDocument, fire, advance, timers, get resumes() { return resumes; } };
}

test("long press reveals controls without triggering a normal click", () => {
  const fixture = revealFixture();
  fixture.fire("pointerdown");
  fixture.advance(499);
  assert.equal(fixture.container.hidden, true);
  fixture.advance(1);
  assert.equal(fixture.container.hidden, false);
  fixture.fire("pointerup");
  assert.equal(fixture.fire("click").defaultPrevented, true);
  fixture.fire("pointerdown");
  fixture.fire("pointerup");
  assert.equal(fixture.fire("click").defaultPrevented, false);
  fixture.reveal.dispose();
});

test("short taps, scrolling, and canceled pointers do not reveal controls", () => {
  for (const cancel of ["pointerup", "pointercancel", "pointerleave", "pointermove"]) {
    const fixture = revealFixture();
    fixture.fire("pointerdown");
    fixture.advance(200);
    fixture.fire(cancel, { clientX: 30 });
    fixture.advance(600);
    assert.equal(fixture.container.hidden, true, cancel);
    assert.equal(fixture.reveal.holding, false, cancel);
    assert.equal(fixture.fire("click").defaultPrevented, false, cancel);
    fixture.reveal.dispose();
  }
});

test("canceling a prolonged hold restarts the dismissal timer", () => {
  const fixture = revealFixture();
  fixture.fire("pointerdown");
  fixture.advance(6000);
  assert.equal(fixture.container.hidden, false);
  fixture.fire("pointercancel");
  fixture.advance(5000);
  assert.equal(fixture.container.hidden, true);
  fixture.reveal.dispose();
});

test("a later right-click is not suppressed by an earlier long press", () => {
  const fixture = revealFixture();
  fixture.fire("pointerdown");
  fixture.advance(500);
  fixture.fire("pointerup");
  fixture.fire("pointerdown", { pointerType: "mouse", button: 2 });
  const menu = fixture.fire("contextmenu", { pointerType: "mouse", button: 2 });
  assert.equal(menu.defaultPrevented, false);
  fixture.reveal.dispose();
});

test("desktop hover reveals controls and inactivity resumes the slideshow", () => {
  const fixture = revealFixture();
  fixture.fire("pointerenter", { pointerType: "mouse" });
  assert.equal(fixture.container.hidden, false);
  fixture.advance(4999);
  assert.equal(fixture.reveal.holding, true);
  fixture.advance(1);
  assert.equal(fixture.container.hidden, true);
  assert.equal(fixture.resumes, 1);
  fixture.reveal.dispose();
});

test("mouse hover-out immediately hides controls and resumes the slideshow", () => {
  const fixture = revealFixture();
  fixture.fire("pointerenter", { pointerType: "mouse" });
  assert.equal(fixture.container.hidden, false);
  fixture.fire("pointerleave", { pointerType: "mouse" });
  assert.equal(fixture.container.hidden, true);
  assert.equal(fixture.reveal.holding, false);
  assert.equal(fixture.resumes, 1);
  assert.equal(fixture.timers.size, 0);
  fixture.fire("pointerenter", { pointerType: "mouse" });
  assert.equal(fixture.container.hidden, false);
  fixture.fire("pointerleave", { pointerType: "mouse" });
  assert.equal(fixture.container.hidden, true);
  assert.equal(fixture.resumes, 2);
  fixture.advance(5000);
  assert.equal(fixture.resumes, 2);
  fixture.reveal.dispose();
});

test("touch pointerleave preserves long-press controls until the idle timeout", () => {
  const fixture = revealFixture();
  fixture.fire("pointerdown");
  fixture.advance(500);
  fixture.fire("pointerup");
  fixture.fire("pointerleave", { pointerType: "touch" });
  assert.equal(fixture.container.hidden, false);
  fixture.advance(4999);
  assert.equal(fixture.container.hidden, false);
  fixture.advance(1);
  assert.equal(fixture.container.hidden, true);
  assert.equal(fixture.resumes, 1);
  fixture.reveal.dispose();
});

test("mouse hover-out keeps controls open for a dialog or active action", () => {
  const fixture = revealFixture();
  fixture.fire("pointerenter", { pointerType: "mouse" });
  fixture.controls.active = true;
  fixture.fire("pointerleave", { pointerType: "mouse" });
  fixture.advance(6000);
  assert.equal(fixture.container.hidden, false);
  assert.equal(fixture.resumes, 0);
  fixture.controls.active = false;
  fixture.reveal.activity();
  fixture.advance(5000);
  assert.equal(fixture.container.hidden, true);
  fixture.reveal.dispose();
});

test("mouse hover-out does not remove keyboard-focused controls", () => {
  for (const focused of ["card", "container"]) {
    const fixture = revealFixture();
    fixture.fire("pointerenter", { pointerType: "mouse" });
    fixture[focused].keyboardFocused = true;
    fixture.fire("pointerleave", { pointerType: "mouse" });
    fixture.advance(6000);
    assert.equal(fixture.container.hidden, false, focused);
    assert.equal(fixture.resumes, 0, focused);
    fixture.reveal.dispose();
  }
});

test("open dialogs and active actions prevent automatic dismissal", () => {
  const fixture = revealFixture();
  fixture.reveal.show();
  fixture.controls.active = true;
  fixture.advance(6000);
  fixture.fire("pointerdown", {}, fixture.ownerDocument, []);
  assert.equal(fixture.container.hidden, false);
  fixture.controls.active = false;
  fixture.reveal.activity();
  fixture.advance(5000);
  assert.equal(fixture.container.hidden, true);
  fixture.reveal.dispose();
});

test("keyboard focus reveals controls and Escape dismisses them", () => {
  const fixture = revealFixture();
  fixture.card.keyboardFocused = true;
  fixture.fire("focusin");
  fixture.advance(6000);
  assert.equal(fixture.container.hidden, false);
  assert.equal(fixture.fire("keydown", { key: "Escape" }).defaultPrevented, true);
  assert.equal(fixture.container.hidden, true);
  fixture.reveal.dispose();
});

test("keyboard activation focuses the toolbar and blur allows dismissal", () => {
  for (const key of ["Enter", " "]) {
    const fixture = revealFixture();
    assert.equal(fixture.fire("keydown", { key }).defaultPrevented, true);
    assert.equal(fixture.container.hidden, false);
    assert.equal(fixture.container.keyboardFocused, true);
    fixture.advance(5000);
    assert.equal(fixture.container.hidden, false);
    fixture.container.keyboardFocused = false;
    fixture.fire("focusout");
    fixture.advance(5000);
    assert.equal(fixture.container.hidden, true);
    fixture.reveal.dispose();
  }
});

test("secondary pointers and right-clicks cannot trigger a long press", () => {
  for (const properties of [{ isPrimary: false }, { button: 2, pointerType: "mouse" }]) {
    const fixture = revealFixture();
    fixture.fire("pointerdown");
    fixture.fire("pointerdown", properties);
    fixture.advance(1000);
    assert.equal(fixture.container.hidden, true);
    fixture.reveal.dispose();
  }
});

test("outside clicks dismiss controls and disposal cancels pending gestures", () => {
  const fixture = revealFixture();
  fixture.reveal.show();
  fixture.fire("pointerdown", {}, fixture.ownerDocument, []);
  assert.equal(fixture.container.hidden, true);
  fixture.fire("pointerdown");
  fixture.reveal.dispose();
  fixture.advance(1000);
  fixture.fire("pointerenter", { pointerType: "mouse" });
  assert.equal(fixture.container.hidden, true);
  assert.equal(fixture.timers.size, 0);
  assert.equal(fixture.reveal.holding, false);
});

test("the photo controls host respects the opt-in visibility toggle", () => {
  assert.match(source, /:host\(\[hidden\]\)\s*\{\s*display:\s*none\s*!important/);
});

test("photo actions send captured IDs and the selected entry", async () => {
  const calls = [];
  const controls = Object.create(PhotoControls.prototype);
  controls._state = { entryId: "new-entry" };
  controls._getHass = () => ({ callWS: async (request) => { calls.push(request); return {}; } });
  await controls._call("hide_photo", { photo_ids: ["displayed-photo"] }, "captured-entry");
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [{
    type: "call_service", domain: "album_slideshow", service: "hide_photo",
    service_data: { photo_ids: ["displayed-photo"], entry_id: "captured-entry" },
  }]);
});

test("hidden-photo lists request a service response on demand", async () => {
  let request;
  const controls = Object.create(PhotoControls.prototype);
  controls._state = { entryId: "test" };
  controls._getHass = () => ({ callWS: async (value) => {
    request = value;
    return { response: { total: 0, photos: [] } };
  } });
  const result = await controls._call("list_hidden_photos", { offset: 25, limit: 25 });
  assert.equal(request.return_response, true);
  assert.equal(request.service_data.offset, 25);
  assert.equal(result.total, 0);
});

test("photo actions cannot run without a config entry", async () => {
  const controls = Object.create(PhotoControls.prototype);
  controls._state = {};
  await assert.rejects(controls._call("hide_photo"), /does not support/);
});

test("navigation uses existing slideshow services and the captured entry", async () => {
  for (const service of ["previous_slide", "next_slide"]) {
    const controls = Object.create(PhotoControls.prototype);
    controls._state = { entryId: "selected-entry" };
    const phases = [];
    const requests = [];
    controls._onNavigate = (...args) => phases.push(args);
    controls._runAction = async action => { await action(); return true; };
    controls._getHass = () => ({ callWS: async request => {
      requests.push(request);
      controls._state.entryId = "another-entry";
      return {};
    } });
    assert.equal(await controls._navigate(service), true);
    assert.equal(JSON.stringify(requests), JSON.stringify([{
      type: "call_service", domain: "album_slideshow", service,
      service_data: { entry_id: "selected-entry" },
    }]));
    assert.deepEqual(phases, [["start", "selected-entry"], ["complete", "selected-entry"]]);
  }
});

test("navigation failure and busy state do not leave a frame override active", async () => {
  const controls = Object.create(PhotoControls.prototype);
  controls._state = { entryId: "test" };
  const phases = [];
  controls._onNavigate = (...args) => phases.push(args);
  controls._run = async () => false;
  assert.equal(await controls._navigate("next_slide"), false);
  assert.deepEqual(phases, [["start", "test"], ["failed", "test"]]);
  controls._busy = true;
  assert.equal(await controls._navigate("next_slide"), false);
  assert.equal(phases.length, 2);
});

test("pause and resume use the existing renamed switch without optimistic state", async () => {
  for (const paused of [false, true]) {
    const controls = Object.create(PhotoControls.prototype);
    controls._state = { entryId: "test-entry", paused };
    const requests = [];
    controls._runAction = async action => { await action(); return true; };
    controls._getHass = () => ({ callWS: async request => {
      requests.push(request);
      if (request.type === "config/entity_registry/list") return [
        { config_entry_id: "different-entry", unique_id: "different-entry_paused", entity_id: "switch.other" },
        { config_entry_id: "test-entry", unique_id: "test-entry_paused", entity_id: "switch.custom_pause_name", disabled_by: null },
      ];
      return {};
    } });
    assert.equal(await controls._togglePause(), true);
    assert.equal(JSON.stringify(requests[1]), JSON.stringify({
      type: "call_service", domain: "switch", service: paused ? "turn_off" : "turn_on",
      service_data: { entity_id: "switch.custom_pause_name" },
    }));
    assert.equal(controls._state.paused, paused);
  }
});

test("missing or disabled pause switch cannot target a different entry", async () => {
  for (const disabled of [true, false]) {
    const controls = Object.create(PhotoControls.prototype);
    controls._state = { entryId: "test-entry", paused: false };
    controls._runAction = action => action();
    let serviceCalls = 0;
    controls._getHass = () => ({ callWS: async request => {
      if (request.type === "config/entity_registry/list") return disabled ? [{
        config_entry_id: "test-entry", unique_id: "test-entry_paused",
        entity_id: "switch.disabled", disabled_by: "user",
      }] : [];
      serviceCalls += 1;
    } });
    await assert.rejects(controls._togglePause(), /pause switch is unavailable/);
    assert.equal(serviceCalls, 0);
  }
});

test("toolbar availability and pause icon follow reported HA state", () => {
  const controls = Object.create(PhotoControls.prototype);
  const elements = new Map();
  for (const id of ["previous", "next", "pause", "hide", "undo", "manage"]) {
    const icon = { setAttribute(name, value) { this[name] = value; } };
    elements.set(id, {
      setAttribute(name, value) { this[name] = value; },
      querySelector() { return icon; },
    });
  }
  controls._root = { getElementById: id => elements.get(id) };
  controls._state = {};
  const state = { entryId: "test", photoIds: ["photo"], paused: false, canPrevious: false, canNext: true };
  controls.update(state);
  assert.equal(elements.get("previous").disabled, true);
  assert.equal(elements.get("next").disabled, false);
  assert.equal(elements.get("pause").title, "Pause slideshow");
  assert.equal(elements.get("pause").querySelector().icon, "mdi:pause");
  controls.update({ ...state, paused: true, canPrevious: true });
  assert.equal(elements.get("previous").disabled, false);
  assert.equal(elements.get("pause")["aria-label"], "Resume slideshow");
  assert.equal(elements.get("pause").querySelector().icon, "mdi:play");
  controls._busy = true;
  controls.update(state);
  assert.ok([...elements.values()].every(element => element.disabled));
  controls._busy = false;
  controls.update({ entryId: "test", paused: true, canNext: false, canPrevious: false });
  assert.equal(elements.get("next").disabled, true);
  assert.equal(elements.get("pause").disabled, false);
  assert.equal(elements.get("manage").disabled, false);
});

test("the editor keeps Refresh but avoids duplicate navigation buttons", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  const wrap = { querySelectorAll: () => [] };
  editor.shadowRoot = { querySelector: () => wrap };
  editor._config = { entity: "camera.test" };
  editor._siblings = { previous_button: "button.previous", next_button: "button.next", refresh_button: "button.refresh" };
  editor._hass = { states: { "camera.test": { attributes: { entry_id: "test" } } } };
  editor._renderActions();
  assert.ok(wrap.innerHTML.includes("Refresh album"));
  assert.ok(!wrap.innerHTML.includes("Previous slide"));
  assert.ok(!wrap.innerHTML.includes("Next slide"));
  delete editor._hass.states["camera.test"].attributes.entry_id;
  editor._renderActions();
  assert.ok(wrap.innerHTML.includes("Previous slide"));
  assert.ok(wrap.innerHTML.includes("Next slide"));
});

function navigationCardFixture() {
  const clock = revealFixture();
  const requests = [];
  context.Image = class { constructor() { requests.push(this); } };
  const card = Object.create(Card.prototype);
  card._config = { entity: "camera.test", fit: "contain" };
  card._controlsReveal = { holding: true };
  card._displayedPhotoIds = ["old-photo"];
  card._displayedFrameId = 1;
  card._lastFrameId = 1;
  card._lastEntityPicture = "/photo.jpg?frame=1";
  card._loadGeneration = 0;
  card._hiddenRevision = 0;
  const attrs = { entry_id: "test", frame_id: 1, hidden_revision: 0, displayed_photo_ids: ["old-photo"], entity_picture: "/photo.jpg?frame=1" };
  card._hass = { states: { "camera.test": { attributes: attrs } } };
  card._performSwap = () => {};
  const advance = frameId => {
    Object.assign(attrs, { frame_id: frameId, entity_picture: `/photo.jpg?frame=${frameId}`, displayed_photo_ids: [`photo-${frameId}`] });
    card._maybeSwap();
  };
  return { card, attrs, requests, advance, clock };
}

test("explicit navigation bypasses the toolbar hold then holds the new frame", () => {
  for (const responseFirst of [false, true]) {
    const { card, requests, advance, clock } = navigationCardFixture();
    card._holdSwapsUntil = Infinity;
    card._controlNavigation("start", "test");
    assert.equal(card._holdSwapsUntil, 0);
    if (responseFirst) card._controlNavigation("complete", "test");
    advance(2);
    assert.equal(requests.length, 1);
    requests[0].onload();
    assert.equal(card._displayedFrameId, 2);
    if (!responseFirst) card._controlNavigation("complete", "test");
    assert.equal(card._navigationRequest, null);
    advance(3);
    assert.equal(requests.length, 1);
    assert.equal(card._displayedFrameId, 2);
    assert.equal(card._controlsReveal.holding, true);
    clock.advance(5000);
    assert.equal(card._navigationRequest, null);
  }
});

test("failed or unchanged navigation restores the normal frame hold", () => {
  for (const phase of ["failed", "complete"]) {
    const { card, requests, advance, clock } = navigationCardFixture();
    card._controlNavigation("start", "test");
    card._controlNavigation(phase, "test");
    clock.advance(5000);
    assert.equal(card._navigationRequest, null);
    advance(2);
    assert.equal(requests.length, 0);
  }
});

test("navigation override ignores a different entry and is cleared on disconnect", () => {
  const { card, clock } = navigationCardFixture();
  card._controlNavigation("start", "other-entry");
  assert.equal(card._navigationRequest, undefined);
  card._controlNavigation("start", "test");
  card._controlNavigation("complete", "test");
  card._controlsReveal.dispose = () => {};
  card.disconnectedCallback();
  clock.advance(5000);
  assert.equal(card._navigationRequest, null);
  assert.equal(card._navigationTimer, null);
});

test("paired hide choices retain IDs when the slideshow advances", () => {
  const controls = Object.create(PhotoControls.prototype);
  controls._state = { entryId: "test", photoIds: ["left", "right"], orientation: "horizontal" };
  const buttons = [];
  const calls = [];
  controls._open = () => ({ appendChild(button) { buttons.push(button); } });
  controls._button = (label, icon, action) => ({ label, action });
  controls._run = (...args) => calls.push(args);
  controls._hide();
  controls._state = { entryId: "other", photoIds: ["new-photo"] };
  buttons[1].action();
  assert.equal(buttons[1].label, "Hide right photo");
  assert.equal(JSON.stringify(calls), JSON.stringify([["hide_photo", { photo_ids: ["right"] }, "test"]]));
});

test("exclusion updates override tap-pause and empty the display", () => {
  const card = Object.create(Card.prototype);
  card._config = { entity: "camera.test" };
  card._hiddenRevision = 0;
  card._holdSwapsUntil = Infinity;
  card._hass = { states: { "camera.test": { attributes: { hidden_revision: 1, displayed_photo_ids: [], empty_reason: "all_hidden" } } } };
  let clears = 0;
  let placeholder;
  card._clearDisplayedPhotos = () => { clears += 1; card._holdSwapsUntil = 0; };
  card._setPlaceholder = (value) => { placeholder = value; };
  card._maybeSwap();
  assert.ok(clears > 0);
  assert.equal(card._holdSwapsUntil, 0);
  assert.equal(placeholder, "All photos hidden");
});

test("a replacement frame clears the preparing placeholder after hiding", () => {
  const requests = [];
  const elements = new Map();
  const makeElement = () => ({
    src: "",
    classList: { add() {}, remove() {} },
    removeAttribute(name) { if (name === "src") this.src = ""; },
    replaceChildren() {},
    appendChild(element) { elements.set(element.id, element); },
    remove() { elements.delete(this.id); },
  });
  for (const id of ["a", "b", "blur-a", "blur-b", "captions", "stage"]) {
    elements.set(id, makeElement());
  }
  context.document = { createElement: makeElement };
  context.Image = class { constructor() { requests.push(this); } };
  const card = Object.create(Card.prototype);
  card._config = { entity: "camera.test", fit: "contain", transition: "none" };
  card.shadowRoot = { getElementById: id => elements.get(id) };
  card._hiddenRevision = 0;
  card._loadGeneration = 0;
  card._lastFrameId = 1;
  card._lastEntityPicture = "/camera.jpg?frame=1";
  card._displayedPhotoIds = ["hidden-photo"];
  card._showing = "a";
  card._holdSwapsUntil = Infinity;
  elements.get("a").src = card._lastEntityPicture;
  const attributes = {
    frame_id: 1,
    hidden_revision: 1,
    displayed_photo_ids: [],
    entity_picture: "/camera.jpg?frame=1",
  };
  card._hass = { states: { "camera.test": { attributes } } };
  card._maybeSwap();
  assert.equal(elements.get("placeholder").textContent, "Preparing next photo...");
  assert.equal(elements.get("a").src, "");
  attributes.frame_id = 2;
  attributes.entity_picture = "/camera.jpg?frame=2";
  card._maybeSwap();
  assert.equal(requests.length, 0);
  attributes.frame_id = 3;
  attributes.displayed_photo_ids = ["replacement-photo"];
  attributes.entity_picture = "/camera.jpg?frame=3";
  card._maybeSwap();
  assert.equal(requests.length, 1);
  requests[0].onload();
  assert.equal(elements.has("placeholder"), false);
  assert.equal(elements.get("a").src, "/camera.jpg?frame=3");
  assert.equal(JSON.stringify(card._displayedPhotoIds), '["replacement-photo"]');
});

test("old image requests cannot reveal a frame after exclusion invalidation", () => {
  const requests = [];
  context.Image = class { constructor() { requests.push(this); } };
  const card = Object.create(Card.prototype);
  card._config = { entity: "camera.test" };
  card._loadGeneration = 0;
  card._hass = { states: { "camera.test": { attributes: { frame_id: 2, hidden_revision: 0 } } } };
  let swaps = 0;
  card._performSwap = () => { swaps += 1; };
  card._loadAndSwap("/old.jpg", "contain", false, null, { entityId: "camera.test", frameId: 2, hiddenRevision: 0, photoIds: ["old"] });
  card._loadGeneration += 1;
  requests[0].onload();
  assert.equal(swaps, 0);
});

test("visible controls hold the current frame until dismissed", () => {
  const card = Object.create(Card.prototype);
  card._config = { entity: "camera.test", fit: "contain" };
  card._controlsReveal = { holding: true };
  card._displayedPhotoIds = ["current"];
  card._lastFrameId = 1;
  card._hiddenRevision = 0;
  card._hass = { states: { "camera.test": { attributes: { frame_id: 2, hidden_revision: 0, displayed_photo_ids: ["next"], entity_picture: "/next.jpg" } } } };
  let loads = 0;
  card._loadAndSwap = () => { loads += 1; };
  card._maybeSwap();
  assert.equal(loads, 0);
  assert.equal(card._lastFrameId, 1);
  card._controlsReveal.holding = false;
  card._maybeSwap();
  assert.equal(loads, 1);
  assert.equal(card._lastFrameId, 2);
});

test("a pending image cannot change the photo under open controls", () => {
  const requests = [];
  context.Image = class { constructor() { requests.push(this); } };
  const card = Object.create(Card.prototype);
  card._config = { entity: "camera.test" };
  card._loadGeneration = 0;
  card._controlsReveal = { holding: true };
  card._displayedPhotoIds = ["current"];
  card._lastFrameId = 2;
  card._hass = { states: { "camera.test": { attributes: { frame_id: 2 } } } };
  card._performSwap = () => assert.fail("a held photo changed");
  card._loadAndSwap("/next.jpg", "contain", false, null, { entityId: "camera.test", frameId: 2, photoIds: ["next"] });
  requests[0].onload();
  assert.equal(card._lastFrameId, null);
  assert.equal(JSON.stringify(card._displayedPhotoIds), '["current"]');
});

test("exclusions clear a held photo and allow a safe replacement", () => {
  const card = Object.create(Card.prototype);
  card._config = { entity: "camera.test", fit: "contain" };
  card._controlsReveal = { holding: true };
  card._displayedPhotoIds = ["hidden"];
  card._hiddenRevision = 0;
  card._hass = { states: { "camera.test": { attributes: { frame_id: 3, hidden_revision: 1, displayed_photo_ids: ["safe"], entity_picture: "/safe.jpg" } } } };
  let cleared = false;
  let loaded;
  card._clearDisplayedPhotos = () => { cleared = true; card._displayedPhotoIds = []; };
  card._loadAndSwap = (url) => { loaded = url; };
  card._maybeSwap();
  assert.equal(cleared, true);
  assert.equal(loaded, "/safe.jpg?_frame=3");
});

test("a frame change during image loading retries rather than using mismatched IDs", () => {
  const requests = [];
  context.Image = class { constructor() { requests.push(this); } };
  const card = Object.create(Card.prototype);
  card._config = { entity: "camera.test" };
  card._loadGeneration = 0;
  card._hass = { states: { "camera.test": { attributes: { frame_id: 3, hidden_revision: 0 } } } };
  let retries = 0;
  card._maybeSwap = () => { retries += 1; };
  card._performSwap = () => assert.fail("stale image was shown");
  card._loadAndSwap("/old.jpg", "contain", false, null, { entityId: "camera.test", frameId: 2, hiddenRevision: 0, photoIds: ["old"] });
  requests[0].onload();
  assert.equal(retries, 1);
});

test("older cameras without frame IDs still display images", () => {
  const requests = [];
  context.Image = class { constructor() { requests.push(this); } };
  const card = Object.create(Card.prototype);
  card._config = { entity: "camera.test" };
  card._loadGeneration = 0;
  card._hass = { states: { "camera.test": { attributes: {} } } };
  let swaps = 0;
  card._performSwap = () => { swaps += 1; };
  card._loadAndSwap("/legacy.jpg", "contain", false, null, { entityId: "camera.test", frameId: null, photoIds: [] });
  requests[0].onload();
  assert.equal(swaps, 1);
});

test("the visual editor round-trips the optional photo controls setting", () => {
  context.CustomEvent = class {
    constructor(type, options) { this.type = type; this.detail = options.detail; }
  };
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test", photo_controls: true });
  const interaction = editor._schema().find(section => section.title === "Interaction");
  assert.ok(interaction.schema.find(field => field.name === "photo_controls"));
  assert.equal(editor._data().photo_controls, "always");
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._valueChanged({ stopPropagation() {}, detail: { value: editor._data() } });
  assert.equal(saved.photo_controls, true);
  editor._valueChanged({ stopPropagation() {}, detail: { value: { ...editor._data(), photo_controls: false } } });
  assert.equal(saved.photo_controls, undefined);
});

test("photo control modes preserve legacy boolean settings", () => {
  const settings = [
    [undefined, "off"], [false, "off"], [true, "always"],
    ["off", "off"], ["always", "always"], ["on_demand", "on_demand"],
  ];
  for (const [value, expected] of settings) {
    const card = Object.create(Card.prototype);
    card.setConfig({ entity: "camera.test", photo_controls: value });
    assert.equal(card._config.photo_controls, expected);
  }
  const card = Object.create(Card.prototype);
  assert.throws(() => card.setConfig({ entity: "camera.test", photo_controls: "invalid" }), /unknown photo controls mode/);
});

test("the editor offers and serializes on-demand controls", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test", photo_controls: "on_demand" });
  const interaction = editor._schema().find(section => section.title === "Interaction");
  const field = interaction.schema.find(field => field.name === "photo_controls");
  assert.equal(JSON.stringify(field.selector.select.options.map(option => option.value)), '["off","on_demand","always"]');
  assert.equal(editor._data().photo_controls, "on_demand");
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._valueChanged({ stopPropagation() {}, detail: { value: editor._data() } });
  assert.equal(saved.photo_controls, "on_demand");
  editor._valueChanged({ stopPropagation() {}, detail: { value: { ...editor._data(), photo_controls: "off" } } });
  assert.equal(saved.photo_controls, undefined);
});

test("camera and exposure caption fields normalize without changing defaults", () => {
  const card = Object.create(Card.prototype);
  assert.equal(JSON.stringify(card._normalizeCaption(true).show), '["date","location"]');
  const normalized = card._normalizeCaption({ show: "date,camera,ISO,exposure_time_seconds,unknown,camera" });
  assert.equal(JSON.stringify(normalized.show), '["date","camera","iso","exposure_time_seconds"]');
});

test("captions format camera details and standard exposure values", () => {
  const card = Object.create(Card.prototype);
  card._hass = { locale: { language: "en" } };
  const frame = {
    description: "A photo", camera_make: "Google", camera_model: "Pixel 11 Pro Fold",
    focal_length_mm: 5.28, aperture_f_number: 1.7, iso: 116, exposure_time_seconds: 0.008,
  };
  const caption = { show: ["description", "camera", "focal_length_mm", "aperture_f_number", "iso", "exposure_time_seconds"] };
  assert.equal(JSON.stringify(card._captionLines(frame, caption)),
    '["A photo","Google Pixel 11 Pro Fold","5.28 mm","f/1.7","ISO 116","1/125 s"]');
  assert.equal(JSON.stringify(card._captionLines({ camera_make: "Canon", camera_model: "Canon EOS R5" }, { show: ["camera"] })), '["Canon EOS R5"]');
  assert.equal(JSON.stringify(card._captionLines({ exposure_time_seconds: 2.5 }, { show: ["exposure_time_seconds"] })), '["2.5 s"]');
});

test("captions omit unavailable and invalid camera fields", () => {
  const card = Object.create(Card.prototype);
  const caption = { show: ["camera", "camera_model", "iso", "focal_length_mm", "aperture_f_number", "exposure_time_seconds"] };
  for (const value of [null, undefined, NaN, Infinity, -1, 0, true, "100"]) {
    assert.equal(JSON.stringify(card._captionLines({ camera_model: " ", iso: value, focal_length_mm: value, aperture_f_number: value, exposure_time_seconds: value }, caption)), '[]');
  }
});

test("paired captions use each photo's metadata instead of primary values", () => {
  const card = Object.create(Card.prototype);
  const data = { camera_model: "Wrong fallback", iso: 999, caption_frames: [
    { camera_model: "First camera", iso: 20 }, { camera_model: "Second camera", iso: 200 },
  ] };
  const frames = card._buildCaptionFrames(data);
  const config = { show: ["camera_model", "iso"] };
  assert.equal(JSON.stringify(card._captionLines(frames[0], config)), '["First camera","ISO 20"]');
  assert.equal(JSON.stringify(card._captionLines(frames[1], config)), '["Second camera","ISO 200"]');
  const legacy = card._buildCaptionFrames({ camera_make: "Apple", camera_model: "iPhone", iso: 100 });
  assert.equal(JSON.stringify(card._captionLines(legacy[0], { show: ["camera", "iso"] })), '["Apple iPhone","ISO 100"]');
});

test("caption snapshot includes exposure metadata for legacy flat cameras", () => {
  const fixture = navigationCardFixture();
  const { card, attrs } = fixture;
  card._config.caption = card._normalizeCaption({ show: ["camera", "iso"] });
  card._controlsReveal.holding = false;
  card._holdSwapsUntil = 0;
  card._lastFrameId = null;
  Object.assign(attrs, { camera_make: "Apple", camera_model: "iPhone", iso: 20 });
  let captured;
  card._loadAndSwap = (_url, _fit, _blur, data) => { captured = data; };
  card._maybeSwap();
  assert.equal(captured.camera_model, "iPhone");
  assert.equal(captured.iso, 20);
});

test("editor round-trips new caption fields and offers metadata choices", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  const show = ["date", "camera", "iso", "aperture_f_number", "exposure_time_seconds"];
  editor.setConfig({ entity: "camera.test", caption: { show } });
  const schema = editor._captionSchema();
  const choices = schema.find(field => field.name === "caption_show").selector.select.options;
  assert.ok(show.every(field => choices.some(option => option.value === field)));
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._valueChanged({ stopPropagation() {}, detail: { value: editor._data() } });
  assert.equal(JSON.stringify(saved.caption.show), JSON.stringify(show));
});

test("lookback and age bias controls only show for the applicable modes", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor._siblings = {
    date_filter: "select.dates", order_mode: "select.order",
    custom_lookback_days: "number.days", shuffle_age_bias: "number.bias",
  };
  editor._hass = { states: {
    "select.dates": { state: "custom_days", attributes: { options: ["off", "custom_days"] } },
    "select.order": { state: "random", attributes: { options: ["random", "newest_taken"] } },
    "number.days": { state: "1825", attributes: { min: 1, max: 36500, step: 1 } },
    "number.bias": { state: "-50", attributes: { min: -100, max: 100, step: 1 } },
  } };
  const fields = editor._liveSchema();
  assert.equal(fields.find(field => field.name === "live_shuffle_age_bias").selector.number.mode, "slider");
  assert.ok(fields.some(field => field.name === "live_custom_lookback_days"));
  assert.equal(editor._liveDataFromStates().live_custom_lookback_days, 1825);
  assert.equal(editor._liveDataFromStates().live_shuffle_age_bias, -50);
  editor._hass.states["select.dates"].state = "off";
  editor._hass.states["select.order"].state = "newest_taken";
  assert.ok(!editor._liveSchema().some(field => ["live_custom_lookback_days", "live_shuffle_age_bias"].includes(field.name)));
});

test("playlist controls call number services without writing them into card config", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor._siblings = { custom_lookback_days: "number.renamed_days", shuffle_age_bias: "number.renamed_bias" };
  editor._liveData = { live_custom_lookback_days: 365, live_shuffle_age_bias: 0 };
  const calls = [];
  editor._hass = { callService: (...args) => calls.push(args) };
  editor.dispatchEvent = () => { throw new Error("Live setting leaked into card YAML"); };
  editor._valueChanged({ stopPropagation() {}, detail: { value: { live_custom_lookback_days: 1825 } } });
  editor._valueChanged({ stopPropagation() {}, detail: { value: { live_shuffle_age_bias: 60 } } });
  assert.equal(JSON.stringify(calls), JSON.stringify([
    ["number", "set_value", { entity_id: "number.renamed_days", value: 1825 }],
    ["number", "set_value", { entity_id: "number.renamed_bias", value: 60 }],
  ]));
});

test("caption fitting shrinks overflowing text and restores the chosen size when space returns", () => {
  const card = Object.create(Card.prototype);
  card._config = { caption: { font_size: "20px" } };
  let availableHeight = 100;
  const box = {
    style: {}, _captionFontSize: "20px",
  };
  const stack = {
    clientWidth: 120, scrollWidth: 120,
    querySelectorAll: () => [box],
    get clientHeight() { return Math.min(availableHeight, this.scrollHeight); },
    get scrollHeight() { return Math.ceil(Number.parseFloat(box.style.fontSize) * 12); },
  };
  context.getComputedStyle = element => ({ fontSize: element.style.fontSize });
  card.shadowRoot = { querySelectorAll: () => [stack] };
  card._fitCaptions();
  assert.ok(Number.parseFloat(box.style.fontSize) < 20);
  assert.ok(stack.scrollHeight <= availableHeight);
  availableHeight = 300;
  card._fitCaptions();
  assert.equal(box.style.fontSize, "20px");
});

test("caption fitting leaves hidden regions ready for a later resize", () => {
  const card = Object.create(Card.prototype);
  card._config = { caption: { font_size: "14px" } };
  const box = { style: {}, _captionFontSize: "14px" };
  const stack = { clientWidth: 0, clientHeight: 0, querySelectorAll: () => [box] };
  card.shadowRoot = { querySelectorAll: () => [stack] };
  card._fitCaptions();
  assert.equal(box.style.fontSize, "14px");
});

test("camera and description captions remain literal text", () => {
  const card = Object.create(Card.prototype);
  const previousDocument = context.document;
  const makeElement = () => ({
    children: [], style: {}, classList: { add() {} },
    appendChild(child) { this.children.push(child); },
    set innerHTML(_value) { throw new Error("Caption text must not be parsed as HTML"); },
  });
  context.document = { createElement: makeElement };
  try {
    const container = makeElement();
    const description = '<img src="invalid" onerror="alert(1)">';
    const model = "<script>not markup</script>";
    const cap = card._normalizeCaption({ show: ["description", "camera_model"] });
    card._addCaptionRegion(container, { description, camera_model: model }, cap, null, 0);
    const lines = container.children[0].children[0].children[0].children;
    assert.equal(lines[0].textContent, description);
    assert.equal(lines[1].textContent, model);
    assert.equal(lines[0].children.length, 0);
  } finally {
    context.document = previousDocument;
  }
});

test("multiple captions keep independent content and styles while legacy config still works", () => {
  const card = Object.create(Card.prototype);
  card.setConfig({ entity: "camera.test", caption: { show: ["date"], font_size: "18px" } });
  assert.equal(card._config.captions.length, 1);
  assert.equal(card._config.caption.font_size, "18px");
  card.setConfig({ entity: "camera.test", captions: [
    { show: ["date"], position: "bottom-left", font_size: "16px" },
    { show: ["current_date", "current_time"], position: "top-right", font_size: "24px", per_image: false },
    { show: ["description"], enabled: false },
  ] });
  assert.equal(card._config.captions.length, 2);
  assert.equal(card._config.captions[0].font_size, "16px");
  assert.equal(card._config.captions[1].font_size, "24px");
  assert.equal(card._config.captions[1].position, "top-right");
  assert.equal(card._config.captions[1].per_image, false);
  card.setConfig({ entity: "camera.test", caption: true, captions: [] });
  assert.equal(card._config.caption, null);
  assert.equal(card._config.captions.length, 0);
  assert.throws(() => card.setConfig({ entity: "camera.test", captions: {} }), /must be a list/);
});

test("multiple overlays retain paired metadata and allow a whole-frame clock", () => {
  const card = Object.create(Card.prototype);
  card.setConfig({ entity: "camera.test", captions: [
    { show: ["camera"], per_image: true },
    { show: ["current_time"], position: "top-right" },
  ] });
  const container = { style: {}, children: [] };
  card.shadowRoot = { getElementById: () => container };
  const added = [];
  card._addCaptionRegion = (_container, frame, cap, orientation, half) => added.push({ frame, cap, orientation, half });
  card._fitCaptions = () => {};
  const data = { pair_orientation: "horizontal", caption_frames: [{ camera_model: "First" }, { camera_model: "Second" }] };
  card._renderCaptions(data, false);
  assert.equal(added.length, 3);
  assert.equal(added[0].frame.camera_model, "First");
  assert.equal(added[1].frame.camera_model, "Second");
  assert.equal(added[1].half, 1);
  assert.equal(added[2].orientation, null);
  assert.equal(card._captionData, data);
});

test("today and the clock use HA timezone and stay distinct from the photo date", () => {
  const card = Object.create(Card.prototype);
  card._hass = { locale: { language: "en-US", time_format: "24" }, config: { time_zone: "America/Los_Angeles" } };
  const caption = card._normalizeCaption({ show: ["date", "current_date", "current_time"], date_format: "YYYY-MM-DD", time_seconds: true });
  const now = new Date("2026-09-30T06:59:58Z");
  const lines = card._captionLines({ captured_at: "1995-06-01T12:00:00Z" }, caption, now);
  assert.equal(lines[0], "1995-06-01");
  assert.equal(lines[1], "2026-09-29");
  assert.equal(lines[2], "23:59:58");
  assert.equal(card._captionLines({}, { ...caption, show: ["current_date"] }, new Date("2026-09-30T07:00:00Z"))[0], "2026-09-30");
  assert.match(card._formatCurrentTime(now, { time_format: "12h" }), /11:59\s*PM/);
  assert.equal(card._formatCurrentTime(new Date("2026-09-30T07:00:00Z"), { time_format: "24h" }), "00:00");
});

test("clock refresh is independent of paused photos and stops when removed", () => {
  const previousSetTimeout = context.setTimeout;
  const previousClearTimeout = context.clearTimeout;
  const timers = new Map();
  let nextId = 0;
  context.setTimeout = (callback, delay) => { timers.set(++nextId, { callback, delay }); return nextId; };
  context.clearTimeout = identifier => timers.delete(identifier);
  try {
    const card = Object.create(Card.prototype);
    card.setConfig({ entity: "camera.test", captions: [{ show: ["current_time"], time_seconds: true }] });
    card.isConnected = true;
    card._captionData = { paused: true, caption_frames: [{ camera_model: "Shown photo" }] };
    let refreshed;
    card._renderCaptions = (data, fade) => { refreshed = { data, fade }; };
    card._maybeSwap = () => { throw new Error("Clock must not change the photo"); };
    card._scheduleCaptionClock();
    assert.equal(timers.size, 1);
    const timer = [...timers.values()][0];
    assert.ok(timer.delay > 0 && timer.delay <= 1020);
    timer.callback();
    assert.equal(refreshed.data, card._captionData);
    assert.equal(refreshed.fade, false);
    card._scheduleCaptionClock();
    const activeId = card._captionClockTimer;
    card.disconnectedCallback();
    assert.ok(!timers.has(activeId));
    assert.equal(card._captionClockTimer, null);
  } finally {
    context.setTimeout = previousSetTimeout;
    context.clearTimeout = previousClearTimeout;
  }
});

test("caption editor adds, duplicates, changes, and removes independent overlays", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test", caption: { show: ["date"], font_size: "18px", color: "gold" } });
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._addCaption(0);
  assert.equal(saved.caption, undefined);
  assert.equal(saved.captions.length, 2);
  assert.equal(saved.captions[0].font_size, "18px");
  assert.equal(saved.captions[1].font_size, "18px");
  assert.equal(saved.captions[1].color, "gold");
  assert.equal(saved.captions[1].position, "top-left");
  assert.notEqual(saved.captions[0].show, saved.captions[1].show);
  editor._captionChanged(1, { stopPropagation() {}, detail: { value: {
    caption_show: ["current_date", "current_time"], caption_position: "top-right",
    caption_font_size: "28px", caption_time_format: "24h", caption_time_seconds: true,
    caption_per_image: false,
  } } });
  assert.equal(JSON.stringify(saved.captions[0].show), '["date"]');
  assert.equal(JSON.stringify(saved.captions[1].show), '["current_date","current_time"]');
  assert.equal(saved.captions[1].font_size, "28px");
  assert.equal(saved.captions[1].time_format, "24h");
  assert.equal(saved.captions[1].time_seconds, true);
  assert.equal(saved.captions[1].per_image, false);
  editor._removeCaption(0);
  assert.equal(saved.captions.length, 1);
  assert.equal(saved.captions[0].position, "top-right");
  editor._removeCaption(0);
  assert.equal(saved.captions, undefined);
  assert.equal(saved.caption, undefined);
});

test("caption editor keeps multiple settings during unrelated card edits and YAML reloads", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  const captions = [
    { show: ["location"], position: "bottom-left", font_size: "1.2em" },
    { show: ["current_time"], position: "top-right", time_seconds: true, enabled: false },
  ];
  editor.setConfig({ entity: "camera.test", captions });
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._valueChanged({ stopPropagation() {}, detail: { value: { ...editor._data(), transition: "fade" } } });
  assert.equal(JSON.stringify(saved.captions), JSON.stringify(captions));
  assert.equal(saved.transition, "fade");
  editor.setConfig(JSON.parse(JSON.stringify(saved)));
  assert.equal(editor._captionData(editor._editorCaptions()[1]).caption_enabled, false);
  assert.equal(editor._captionData(editor._editorCaptions()[0]).caption_font_size, "1.2em");
});

test("caption editor offers clock controls only when selected and retains empty or disabled rows", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test" });
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._addCaption();
  assert.ok(saved.caption);
  assert.ok(!editor._captionSchema().some(field => field.name === "caption_time_format"));
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: {
    caption_show: ["current_time"], caption_enabled: false,
  } } });
  assert.equal(saved.caption.enabled, false);
  assert.ok(editor._captionSchema().some(field => field.name === "caption_time_format"));
  assert.ok(!editor._captionSchema().some(field => field.name === "caption_date_format"));
  assert.ok(!editor._captionSchema().some(field => field.name === "caption_per_image"));
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: { caption_show: [] } } });
  assert.equal(editor._editorCaptions().length, 1);
  assert.equal(JSON.stringify(saved.caption.show), '[]');
  const choices = editor._captionSchema().find(field => field.name === "caption_show").selector.select.options;
  assert.ok(choices.some(option => option.value === "current_date"));
  assert.ok(choices.some(option => option.value === "current_time"));
});

test("captions at the same position stack without discarding individual styles", () => {
  const card = Object.create(Card.prototype);
  const previousDocument = context.document;
  const makeElement = () => ({
    children: [], style: {}, classList: { add() {} },
    appendChild(child) { this.children.push(child); },
  });
  context.document = { createElement: makeElement };
  try {
    const container = makeElement();
    card._addCaptionRegion(container, { description: "Photo" }, card._normalizeCaption({ show: ["description"], font_size: "16px" }), null, 0);
    card._addCaptionRegion(container, { camera_model: "Camera" }, card._normalizeCaption({ show: ["camera_model"], font_size: "24px" }), null, 0);
    assert.equal(container.children.length, 1);
    const boxes = container.children[0]._captionStack.children;
    assert.equal(boxes.length, 2);
    assert.equal(boxes[0]._captionFontSize, "16px");
    assert.equal(boxes[1]._captionFontSize, "24px");
  } finally {
    context.document = previousDocument;
  }
});

test("captions sharing an edge have nonoverlapping layout areas inside each paired photo", () => {
  const card = Object.create(Card.prototype);
  const regions = [
    [0, 0, 0], [0, 2, 0], [2, 0, 0], [0, 0, 1], [2, 0, 1],
  ].map(([row, column, half]) => ({ style: {}, _captionRow: row, _captionColumn: column,
    _captionHalf: half, _captionOrientation: "horizontal" }));
  card._layoutCaptionRegions({ children: regions });
  assert.equal(regions[0].style.left, "0%");
  assert.equal(regions[0].style.right, "75%");
  assert.equal(regions[1].style.left, "25%");
  assert.equal(regions[1].style.right, "50%");
  assert.equal(regions[0].style.bottom, "50%");
  assert.equal(regions[2].style.top, "50%");
  assert.equal(regions[3].style.left, "50%");
  assert.equal(regions[4].style.top, "50%");
});

test("whole-frame clocks reserve space above per-image metadata on both pair orientations", () => {
  const card = Object.create(Card.prototype);
  for (const orientation of ["horizontal", "vertical"]) {
    const regions = [
      { style: {}, _captionRow: 0, _captionColumn: 2, _captionHalf: 0, _captionOrientation: null },
      { style: {}, _captionRow: 2, _captionColumn: 0, _captionHalf: 0, _captionOrientation: orientation },
      { style: {}, _captionRow: 2, _captionColumn: 0, _captionHalf: 1, _captionOrientation: orientation },
    ];
    card._layoutCaptionRegions({ children: regions });
    const clockBottom = 100 - Number.parseFloat(regions[0].style.bottom);
    const firstCaptionTop = Number.parseFloat(regions[1].style.top);
    assert.ok(clockBottom <= firstCaptionTop);
    assert.equal(regions[0].style.top, "0%");
    assert.equal(regions[2].style.bottom, "0%");
  }
});

test("automatic clock format respects HA's system locale preference", () => {
  const previousNavigator = context.navigator;
  context.navigator = { language: "en-GB" };
  try {
    const card = Object.create(Card.prototype);
    card._hass = { locale: { language: "en-US", time_format: "system" }, config: { time_zone: "UTC" } };
    assert.equal(card._formatCurrentTime(new Date("2026-09-29T17:30:00Z"), { time_format: "auto" }), "17:30");
  } finally {
    context.navigator = previousNavigator;
  }
});

test("a whole-frame clock stacks with metadata at the same corner in either insertion order", () => {
  const card = Object.create(Card.prototype);
  const previousDocument = context.document;
  const makeElement = () => ({ children: [], style: {}, classList: { add() {} },
    appendChild(child) { this.children.push(child); } });
  context.document = { createElement: makeElement };
  try {
    for (const clockFirst of [true, false]) {
      const container = makeElement();
      const photo = card._normalizeCaption({ show: ["camera_model"], position: "bottom-left" });
      const clock = card._normalizeCaption({ show: ["current_time"], position: "bottom-left", font_size: "22px" });
      const addPhoto = () => {
        card._addCaptionRegion(container, { camera_model: "First" }, photo, "horizontal", 0);
        card._addCaptionRegion(container, { camera_model: "Second" }, photo, "horizontal", 1);
      };
      const addClock = () => card._addCaptionRegion(container, {}, clock, null, 0);
      if (clockFirst) { addClock(); addPhoto(); } else { addPhoto(); addClock(); }
      card._layoutCaptionRegions(container);
      assert.equal(container.children.length, 2);
      const first = container.children.find(region => region._captionHalf === 0);
      const second = container.children.find(region => region._captionHalf === 1);
      assert.equal(first._captionStack.children.length, 2);
      assert.equal(second._captionStack.children.length, 1);
      assert.equal(first.style.right, "50%");
      assert.equal(second.style.left, "50%");
    }
  } finally {
    context.document = previousDocument;
  }
});

test("a global centered caption reserves horizontal space between paired captions", () => {
  const card = Object.create(Card.prototype);
  const regions = [
    { style: {}, _captionRow: 2, _captionColumn: 1, _captionHalf: 0, _captionOrientation: "horizontal" },
    { style: {}, _captionRow: 2, _captionColumn: 1, _captionHalf: 1, _captionOrientation: "horizontal" },
    { style: {}, _captionRow: 2, _captionColumn: 1, _captionHalf: 0, _captionOrientation: null },
  ];
  card._layoutCaptionRegions({ children: regions });
  assert.ok(100 - Number.parseFloat(regions[0].style.right) <= Number.parseFloat(regions[2].style.left));
  assert.ok(100 - Number.parseFloat(regions[2].style.right) <= Number.parseFloat(regions[1].style.left));
});

test("legacy vertical-pair captions retain the full available area in both photos", () => {
  const card = Object.create(Card.prototype);
  const regions = [0, 1].map(half => ({ style: {}, _captionRow: 2, _captionColumn: 0,
    _captionHalf: half, _captionOrientation: "vertical" }));
  card._layoutCaptionRegions({ children: regions });
  assert.equal(regions[0].style.top, "0%");
  assert.equal(regions[0].style.bottom, "50%");
  assert.equal(regions[1].style.top, "50%");
  assert.equal(regions[1].style.bottom, "0%");
});

test("issue 31 custom date plus REL formatting works in legacy and multiple captions", () => {
  const previousDate = context.Date;
  const now = new Date("2026-08-27T12:00:00Z");
  context.Date = class extends Date { static now() { return now.getTime(); } };
  try {
    const card = Object.create(Card.prototype);
    card._hass = { locale: { language: "en-GB" }, config: { time_zone: "UTC" } };
    const caption = { show: ["date"], date_format: "DD MMMM YYYY - REL" };
    for (const configuration of [{ caption }, { captions: [caption] }]) {
      card.setConfig({ entity: "camera.test", ...configuration });
      const lines = card._captionLines({ captured_at: "2024-08-27T12:00:00Z" }, card._captionConfigs()[0], now);
      assert.equal(lines[0], "27 August 2024 - 2 years ago");
      assert.equal(card._formatDate("2024-08-27T12:00:00Z", "YY"), "24");
    }
  } finally {
    if (previousDate === undefined) delete context.Date;
    else context.Date = previousDate;
  }
});

test("today's date has its own preset or custom format without changing photo dates", () => {
  const card = Object.create(Card.prototype);
  card._hass = { locale: { language: "en-GB" }, config: { time_zone: "UTC" } };
  const caption = card._normalizeCaption({
    show: ["date", "current_date"], date_format: "year", current_date_format: "DD MMMM YYYY",
  });
  const lines = card._captionLines({ captured_at: "1995-06-01T12:00:00Z" }, caption, new Date("2026-09-29T12:00:00Z"));
  assert.equal(JSON.stringify(lines), '["1995","29 September 2026"]');
  const preset = { ...caption, current_date_format: "month_year" };
  assert.equal(card._captionLines({}, { ...preset, show: ["current_date"] }, new Date("2026-09-29T12:00:00Z"))[0], "September 2026");
  assert.equal(card._normalizeCaption({ show: ["current_date"], date_format: "YYYY" }).current_date_format, "YYYY");
});

test("date format editor preserves REL and independently saves today's format", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test", caption: { show: ["date", "current_date"], date_format: "DD MMMM YYYY - REL" } });
  const schema = editor._captionSchema();
  for (const name of ["caption_date_format", "caption_current_date_format"]) {
    const field = schema.find(item => item.name === name);
    assert.equal(field.selector.select.custom_value, true);
    assert.ok(field.selector.select.options.some(option => option.value.includes("REL")));
  }
  assert.equal(editor._computeLabel({ name: "caption_current_date_format" }), "Today's date format");
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: { caption_current_date_format: "weekday" } } });
  assert.equal(saved.caption.date_format, "DD MMMM YYYY - REL");
  assert.equal(saved.caption.current_date_format, "weekday");
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: { caption_current_date_format: "medium" } } });
  assert.equal(saved.caption.current_date_format, "medium");
  editor.setConfig(JSON.parse(JSON.stringify(saved)));
  assert.equal(editor._captionData().caption_current_date_format, "medium");
  assert.equal(editor._captionData().caption_date_format, "DD MMMM YYYY - REL");
  const todayOnly = editor._captionSchema({ show: ["current_date"] });
  assert.ok(todayOnly.some(field => field.name === "caption_current_date_format"));
  assert.ok(!todayOnly.some(field => field.name === "caption_date_format"));
});

test("weather captions require an explicitly selected weather entity or sensor", () => {
  const card = Object.create(Card.prototype);
  card._hass = { states: { "weather.home": { state: "sunny", attributes: { temperature: 20 } } } };
  for (const entity of [undefined, "", "light.example", "weather.", "sensor.bad id"]) {
    const caption = card._normalizeCaption({ show: ["weather"], weather_entity: entity });
    assert.equal(caption.weather_entity, null);
    assert.equal(JSON.stringify(card._captionLines({}, caption)), '[]');
  }
  assert.equal(card._normalizeCaption({ show: ["weather"], weather_entity: " sensor.outdoor_temperature " }).weather_entity, "sensor.outdoor_temperature");
});

test("weather captions use HA formatters for conditions, temperatures, and sensor units", () => {
  const card = Object.create(Card.prototype);
  const weather = { entity_id: "weather.home", state: "partlycloudy", attributes: { temperature: 22.5, temperature_unit: "C" } };
  const sensor = { entity_id: "sensor.outdoor_temperature", state: "22.5", attributes: { unit_of_measurement: "C" } };
  card._hass = {
    states: { "weather.home": weather, "sensor.outdoor_temperature": sensor },
    formatEntityState: entity => entity === weather ? "Partly cloudy" : "22.5 C",
    formatEntityAttributeValue: (entity, attribute) => {
      assert.equal(entity, weather);
      assert.equal(attribute, "temperature");
      return "22.5 C";
    },
  };
  assert.equal(card._weatherCaption({ weather_entity: "weather.home" }), "Partly cloudy, 22.5 C");
  assert.equal(card._weatherCaption({ weather_entity: "sensor.outdoor_temperature" }), "22.5 C");
});

test("weather captions handle unavailable entities, missing temperatures, and zero", () => {
  const card = Object.create(Card.prototype);
  card._hass = { locale: { language: "en" }, states: {} };
  const caption = { weather_entity: "weather.home" };
  assert.equal(card._weatherCaption(caption), "");
  for (const state of ["unknown", "unavailable", ""]) {
    card._hass.states["weather.home"] = { state, attributes: {} };
    assert.equal(card._weatherCaption(caption), "");
  }
  card._hass.states["weather.home"] = { state: "sunny", attributes: { temperature: 0, temperature_unit: "C" } };
  assert.equal(card._weatherCaption(caption), "Sunny, 0 C");
  for (const temperature of [undefined, null, NaN, Infinity, "20"]) {
    card._hass.states["weather.home"].attributes.temperature = temperature;
    assert.equal(card._weatherCaption(caption), "Sunny");
  }
  card._hass.states["sensor.outdoor"] = { state: "19.2", attributes: { unit_of_measurement: "C" } };
  assert.equal(card._weatherCaption({ weather_entity: "sensor.outdoor" }), "19.2 C");
});

test("weather display modes show a colored icon or only the temperature", () => {
  const card = Object.create(Card.prototype);
  const weather = { entity_id: "weather.home", state: "partlycloudy", attributes: { temperature: 12.9 } };
  card._hass = {
    states: { "weather.home": weather, "sensor.outdoor": { state: "11.5", attributes: {} } },
    formatEntityState: entity => entity === weather ? "Partly cloudy" : "11.5 °C",
    formatEntityAttributeValue: () => "12.9 °C",
  };
  const caption = display => card._normalizeCaption({ show: ["weather"], weather_entity: "weather.home", weather_display: display });
  assert.equal(caption(undefined).weather_display, "condition_temperature");
  assert.equal(caption("bogus").weather_display, "condition_temperature");
  assert.equal(card._weatherCaption(caption("condition_temperature")), "Partly cloudy, 12.9 °C");
  assert.equal(card._weatherCaption(caption("temperature")), "12.9 °C");
  assert.equal(JSON.stringify(card._weatherCaption(caption("icon_temperature"))),
    '{"icon":"yr/partlycloudy_day","label":"Partly cloudy","text":"12.9 °C"}');
  card._hass.states["sun.sun"] = { state: "below_horizon" };
  assert.equal(card._weatherCaption(caption("icon_temperature")).icon, "yr/partlycloudy_night");
  // A condition without an icon keeps its text; a sensor has no condition.
  weather.state = "volcanic-ash";
  assert.equal(card._weatherCaption(caption("icon_temperature")), "Partly cloudy, 12.9 °C");
  assert.equal(card._weatherCaption({ ...caption("icon_temperature"), weather_entity: "sensor.outdoor" }), "11.5 °C");
  weather.state = "rainy";
  delete weather.attributes.temperature;
  assert.equal(card._weatherCaption(caption("temperature")), "");
  assert.equal(JSON.stringify(card._weatherCaption(caption("icon_temperature"))),
    '{"icon":"yr/rain","label":"Partly cloudy","text":""}');
});

test("weather icons come from the chosen icon set", () => {
  const card = Object.create(Card.prototype);
  const weather = { state: "partlycloudy", attributes: { temperature: 3 } };
  card._hass = { states: { "weather.home": weather }, formatEntityState: () => "Partly cloudy", formatEntityAttributeValue: () => "3 °C" };
  const caption = icons => card._normalizeCaption({
    show: ["weather"], weather_entity: "weather.home", weather_display: "icon_temperature", weather_icons: icons,
  });
  assert.equal(caption(undefined).weather_icons, "yr");
  assert.equal(caption("bogus").weather_icons, "yr");
  assert.equal(card._weatherCaption(caption("meteocons")).icon, "meteocons/partly-cloudy-day");
  const day = card._weatherCaption(caption("home_assistant"));
  assert.equal(day.icon, undefined);
  assert.match(day.svg, /^<svg [^>]*viewBox="0 0 17 17">/);
  assert.match(day.svg, /class="sun"/);
  assert.match(day.svg, /class="cloud-front"/);
  card._hass.states["sun.sun"] = { state: "below_horizon" };
  assert.equal(card._weatherCaption(caption("meteocons")).icon, "meteocons/partly-cloudy-night");
  assert.match(card._weatherCaption(caption("home_assistant")).svg, /class="moon"/);
  // Home Assistant draws no exceptional icon, so the text stays.
  weather.state = "exceptional";
  assert.equal(card._weatherCaption(caption("home_assistant")), "Partly cloudy, 3 °C");
  assert.equal(card._weatherCaption(caption("meteocons")).icon, "meteocons/code-red");
});

test("every Home Assistant weather condition has a bundled icon in each set", () => {
  const conditions = [
    "clear-night", "cloudy", "exceptional", "fog", "hail", "lightning", "lightning-rainy",
    "partlycloudy", "pouring", "rainy", "snowy", "snowy-rainy", "sunny", "windy", "windy-variant",
  ];
  const sets = vm.runInContext("WEATHER_ICON_SETS", context);
  const folder = path.join(path.dirname(filename), "weather");
  const used = new Set();
  for (const icons of Object.values(sets)) {
    assert.equal(JSON.stringify(Object.keys(icons).sort()), JSON.stringify([...conditions, "partlycloudy-night"].sort()));
    Object.values(icons).forEach(icon => used.add(icon));
  }
  const bundled = ["yr", "meteocons"].flatMap(set => fs.readdirSync(path.join(folder, set))
    .filter(file => file.endsWith(".svg")).map(file => `${set}/${file.slice(0, -4)}`));
  assert.equal(JSON.stringify(bundled.sort()), JSON.stringify([...used].sort()));
  for (const icon of used) {
    assert.match(fs.readFileSync(path.join(folder, `${icon}.svg`), "utf8"), /^<svg /);
  }
  const render = vm.runInContext("homeAssistantWeatherSvg", context);
  for (const condition of conditions.filter(name => name !== "exceptional")) {
    assert.match(render(condition, false), /<path class="/, condition);
  }
  assert.equal(render("exceptional", false), null);
  assert.equal((render("pouring", false).match(/class="rain"/g) || []).length, 6);
  assert.match(render("snowy-rainy", false), /class="snow"/);
  const license = fs.readFileSync(path.join(folder, "LICENSE"), "utf8");
  assert.match(license, /Copyright \(c\) 2015-2017 Yr/);
  assert.match(license, /Copyright \(c\) 2020-present Bas Milius/);
  assert.match(license, /home-assistant\/frontend/);
  assert.match(fs.readFileSync(path.join(folder, "LICENSE-home-assistant.md"), "utf8"), /^Apache License/);
  const init = fs.readFileSync(path.join(__dirname, "../custom_components/album_slideshow/__init__.py"), "utf8");
  assert.ok(init.includes('CARD_STATIC_PATH = "/album_slideshow_static"'));
  assert.equal(vm.runInContext("WEATHER_ICON_PATH", context), "/album_slideshow_static/weather");
});

test("icon weather lines render the icon before the temperature", () => {
  const card = Object.create(Card.prototype);
  const previousDocument = context.document;
  const makeElement = tag => ({
    tag, children: [], style: {}, classList: { add() {} },
    appendChild(child) { this.children.push(child); },
    append(...nodes) { this.children.push(...nodes); },
  });
  context.document = { createElement: makeElement };
  try {
    card._hass = {
      states: { "weather.home": { state: "sunny", attributes: { temperature: 20 } } },
      formatEntityState: () => "Sunny", formatEntityAttributeValue: () => "20 °C",
    };
    const container = makeElement();
    card._addCaptionRegion(container, {}, card._normalizeCaption({
      show: ["weather"], weather_entity: "weather.home", weather_display: "icon_temperature",
    }), null, 0);
    const [icon, text] = container.children[0]._captionStack.children[0].children[0].children;
    assert.equal(icon.tag, "img");
    assert.equal(icon.className, "cap-icon");
    assert.equal(icon.alt, "Sunny");
    assert.equal(icon.src, `/album_slideshow_static/weather/yr/clearsky_day.svg?v=${vm.runInContext("VERSION", context)}`);
    assert.equal(text, "20 °C");
    const native = makeElement();
    card._addCaptionRegion(native, {}, card._normalizeCaption({
      show: ["weather"], weather_entity: "weather.home", weather_display: "icon_temperature", weather_icons: "home_assistant",
    }), null, 0);
    const [svgIcon, svgText] = native.children[0]._captionStack.children[0].children[0].children;
    assert.equal(svgIcon.tag, "span");
    assert.equal(svgIcon.className, "cap-icon");
    assert.match(svgIcon.innerHTML, /^<svg [\s\S]*class="sun"/);
    assert.equal(svgIcon.src, undefined);
    assert.equal(svgText, "20 °C");
  } finally {
    context.document = previousDocument;
  }
});

test("icon weather captions switch to the night icon at sunset", () => {
  const card = Object.create(Card.prototype);
  card.setConfig({ entity: "camera.test", captions: [{
    show: ["weather"], weather_entity: "weather.home", weather_display: "icon_temperature",
  }] });
  card._rendered = true;
  card._captionData = { caption_frames: [{}] };
  card._hass = { states: { "weather.home": { state: "partlycloudy", attributes: {} }, "sun.sun": { state: "above_horizon" } } };
  card._maybeSwap = () => {};
  const updates = [];
  card._renderCaptions = () => updates.push(true);
  card.hass = { ...card._hass, states: { ...card._hass.states, "sun.sun": { state: "above_horizon", attributes: { elevation: 3 } } } };
  assert.equal(updates.length, 0);
  card.hass = { ...card._hass, states: { ...card._hass.states, "sun.sun": { state: "below_horizon" } } };
  assert.equal(updates.length, 1);
});

test("the editor offers weather display for weather entities and saves non-default choices", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test", caption: { show: ["weather"], weather_entity: "sensor.outdoor" } });
  assert.ok(!editor._captionSchema().some(field => field.name === "caption_weather_display"));
  editor.setConfig({ entity: "camera.test", caption: { show: ["weather"], weather_entity: "weather.home" } });
  const field = editor._captionSchema().find(item => item.name === "caption_weather_display");
  assert.equal(JSON.stringify(field.selector.select.options.map(option => option.value)),
    '["condition_temperature","icon_temperature","temperature"]');
  assert.equal(editor._computeLabel({ name: "caption_weather_display" }), "Weather display");
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: { caption_weather_display: "icon_temperature" } } });
  assert.equal(saved.caption.weather_display, "icon_temperature");
  const icons = editor._captionSchema(saved.caption).find(item => item.name === "caption_weather_icons");
  assert.equal(JSON.stringify(icons.selector.select.options.map(option => option.value)), '["yr","meteocons","home_assistant"]');
  assert.equal(editor._computeLabel({ name: "caption_weather_icons" }), "Weather icons");
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: { caption_weather_icons: "home_assistant" } } });
  assert.equal(saved.caption.weather_icons, "home_assistant");
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: { caption_weather_icons: "yr" } } });
  assert.equal(saved.caption.weather_icons, undefined);
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: { caption_weather_display: "condition_temperature" } } });
  assert.equal(saved.caption.weather_display, undefined);
  assert.ok(!editor._captionSchema(saved.caption).some(item => item.name === "caption_weather_icons"));
});

test("weather-only overlays appear once over a paired slide", () => {
  const card = Object.create(Card.prototype);
  card.setConfig({ entity: "camera.test", captions: [{ show: ["weather"], weather_entity: "weather.home" }] });
  const container = { children: [], style: {} };
  card.shadowRoot = { getElementById: () => container };
  const added = [];
  card._addCaptionRegion = (_container, _frame, _caption, orientation) => added.push(orientation);
  card._fitCaptions = () => {};
  card._renderCaptions({ pair_orientation: "horizontal", caption_frames: [{}, {}] }, false);
  assert.equal(JSON.stringify(added), '[null]');
});

test("selected weather updates captions while the displayed photo is held", () => {
  const card = Object.create(Card.prototype);
  card.setConfig({ entity: "camera.test", captions: [{ show: ["weather"], weather_entity: "weather.home" }] });
  card._rendered = true;
  card._captionData = { paused: true, caption_frames: [{ description: "Displayed photo" }] };
  card._hass = { locale: { language: "en" }, states: { "weather.home": { state: "sunny", attributes: {} } } };
  card._maybeSwap = () => {};
  card._loadAndSwap = () => { throw new Error("Weather must not fetch a new slide"); };
  const updates = [];
  card._renderCaptions = (data, fade) => updates.push({ data, fade });
  card.hass = { ...card._hass, states: { ...card._hass.states, "sensor.other": { state: "20" } } };
  assert.equal(updates.length, 0);
  card.hass = { ...card._hass, states: { ...card._hass.states, "weather.home": { state: "rainy", attributes: {} } } };
  assert.equal(updates.length, 1);
  assert.equal(updates[0].data, card._captionData);
  assert.equal(updates[0].fade, false);
  card.hass = { ...card._hass, states: { ...card._hass.states, "weather.home": { state: "unavailable", attributes: {} } } };
  assert.equal(updates.length, 2);
});

test("weather selector is conditional, required, and saves independently per caption", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test", caption: { show: ["date"] } });
  assert.ok(!editor._captionSchema().some(field => field.name === "caption_weather_entity"));
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: { caption_show: ["weather"] } } });
  const field = editor._captionSchema().find(item => item.name === "caption_weather_entity");
  assert.equal(field.required, true);
  assert.equal(JSON.stringify(field.selector.entity.filter), '[{"domain":["weather","sensor"]}]');
  assert.equal(editor._captionData().caption_weather_entity, "");
  assert.ok(!editor._captionSchema().some(item => item.name === "caption_per_image"));
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: { caption_weather_entity: "weather.home" } } });
  assert.equal(saved.caption.weather_entity, "weather.home");
  editor._addCaption(0);
  assert.equal(saved.captions[1].weather_entity, "weather.home");
  editor._captionChanged(1, { stopPropagation() {}, detail: { value: { caption_weather_entity: "sensor.outdoor_temperature" } } });
  assert.equal(saved.captions[0].weather_entity, "weather.home");
  assert.equal(saved.captions[1].weather_entity, "sensor.outdoor_temperature");
  editor._valueChanged({ stopPropagation() {}, detail: { value: editor._data() } });
  assert.equal(saved.captions[1].weather_entity, "sensor.outdoor_temperature");
});

test("caption content uses HA draggable chips and retains reordered content", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test", captions: [
    { show: ["date", "location", "camera"], position: "bottom-right", date_format: "DD MMMM YYYY - REL" },
    { show: ["weather"], weather_entity: "weather.home" },
  ] });
  const selector = editor._captionSchema().find(field => field.name === "caption_show").selector.select;
  assert.equal(selector.multiple, true);
  assert.equal(selector.reorder, true);
  let saved;
  editor.dispatchEvent = event => { saved = event.detail.config; };
  editor._captionChanged(0, { stopPropagation() {}, detail: { value: {
    caption_show: ["camera", "date", "location"],
  } } });
  assert.equal(JSON.stringify(saved.captions[0].show), '["camera","date","location"]');
  assert.equal(saved.captions[0].position, "bottom-right");
  assert.equal(saved.captions[0].date_format, "DD MMMM YYYY - REL");
  assert.equal(saved.captions[1].weather_entity, "weather.home");
  editor.setConfig(JSON.parse(JSON.stringify(saved)));
  assert.equal(JSON.stringify(editor._captionData().caption_show), '["camera","date","location"]');
  const card = Object.create(Card.prototype);
  card._hass = { locale: { language: "en-GB" } };
  const lines = card._captionLines({ camera_model: "Test camera", captured_at: "2024-08-27T12:00:00Z", location: "Test place" }, {
    ...card._normalizeCaption(saved.captions[0]), date_format: "year",
  });
  assert.equal(JSON.stringify(lines), '["Test camera","2024","Test place"]');
});

test("caption headers show placement as title and ordered content as subtitle", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test", captions: [
    { show: ["date", "location", "camera"], position: "bottom-right" },
    { show: ["current_time"], position: "top-left", enabled: false },
  ] });
  const items = [0, 1].map(() => {
    const nodes = { ".caption-title": {}, ".caption-meta": {}, summary: {
      setAttribute(name, value) { this[name] = value; },
    }, ".caption-controls": {}, ".caption-options": {} };
    return { nodes, classList: { toggle() {} }, querySelector: selector => nodes[selector] };
  });
  editor.shadowRoot = { querySelector: () => ({ children: items }) };
  editor._updateCaptionContentPicker = () => {};
  editor._renderCaptionEditors();
  assert.equal(items[0].nodes[".caption-title"].textContent, "Bottom right");
  assert.equal(items[0].nodes[".caption-meta"].textContent, "Photo date, Location, Camera (make and model)");
  assert.equal(items[0].nodes[".caption-meta"].title, items[0].nodes[".caption-meta"].textContent);
  assert.equal(items[1].nodes[".caption-title"].textContent, "Top left (off)");
  assert.equal(items[1].nodes[".caption-meta"].textContent, "Current time");
  assert.match(items[0].nodes.summary["aria-label"], /^Caption 1: Bottom right\./);
});

test("tile-style content picker reorder persists only the targeted caption", () => {
  const Editor = vm.runInContext("createAlbumSlideshowCardEditorClass(class { attachShadow() {} })", context);
  const editor = new Editor();
  editor.setConfig({ entity: "camera.test", captions: [
    { show: ["date", "location", "camera"], position: "bottom-right", date_format: "DD MMMM YYYY - REL" },
    { show: ["current_time"], time_format: "24h" },
  ] });
  const saved = [];
  editor.dispatchEvent = event => saved.push(event.detail.config);
  let stopped = false;
  editor._moveCaptionContent(0, { stopPropagation() { stopped = true; }, detail: { oldIndex: 2, newIndex: 0 } });
  assert.equal(stopped, true);
  assert.equal(JSON.stringify(saved[0].captions[0].show), '["camera","date","location"]');
  assert.equal(saved[0].captions[0].date_format, "DD MMMM YYYY - REL");
  assert.equal(saved[0].captions[1].time_format, "24h");
  for (const [oldIndex, newIndex] of [[0, 0], [-1, 0], [0, 3], [undefined, 0], [0, "2"]]) {
    editor._moveCaptionContent(0, { stopPropagation() {}, detail: { oldIndex, newIndex } });
  }
  assert.equal(saved.length, 1);
});