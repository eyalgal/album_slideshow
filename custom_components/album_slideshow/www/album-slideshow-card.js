/**
 * Album Slideshow Card
 *
 * Client-side cross-fade (and friends) for `album_slideshow` cameras.
 * Server CPU cost per slide change: one JPEG encode. The transition
 * itself runs entirely in the browser via CSS/GPU compositing, so it
 * stays buttery smooth even on a Raspberry Pi class HA host with
 * multiple albums on screen.
 *
 * Usage:
 *   type: custom:album-slideshow-card
 *   entity: camera.album_slideshow_living_room
 *   transition: random      # random | none | fade | slide-left
 *                           #   | slide-right | slide-up | slide-down
 *                           #   | wipe-left | wipe-right | zoom
 *   duration: 600           # ms
 *   easing: ease-in-out     # any CSS easing
 *   aspect_ratio: 16/9      # CSS aspect-ratio value, e.g. 16/9, 4/3, auto
 *   fit: auto               # auto | cover | contain
 *                           # ``auto`` inherits from the camera's
 *                           # ``fill_mode`` attribute (cover / contain
 *                           # / blur). ``blur`` adds a blurred backdrop
 *                           # behind a contained image.
 *   background: ''          # CSS color shown behind contained images.
 *                           # Empty inherits theme card background.
 *   tap_action: none        # none | more-info
 *   videos: true            # play video slides (muted, looping) and
 *                           # Live Photo motion over the camera's still;
 *                           # false shows only the still.
 *   video_audio: off        # off | muted | on. ``muted`` starts videos
 *                           # muted with a speaker button to unmute;
 *                           # ``on`` plays with sound when the browser
 *                           # allows it, falling back to that button.
 */

const VERSION = "1.15.0";

const ANIMATED_TRANSITIONS = [
  "fade",
  "slide-left",
  "slide-right",
  "slide-up",
  "slide-down",
  "wipe-left",
  "wipe-right",
  "zoom",
];

// ``none`` short-circuits all animation: the new image replaces the old
// instantly. Useful on very-low-power displays or when the user wants
// the slideshow to feel like a static gallery cycling through frames.
const TRANSITIONS = new Set(["random", "none", ...ANIMATED_TRANSITIONS]);

const FIT_MODES = new Set(["auto", "cover", "contain"]);

const PHOTO_CONTROL_OPTIONS = [
  { value: "off", label: "Off" },
  { value: "on_demand", label: "On demand" },
  { value: "always", label: "Always" },
];

const VIDEO_AUDIO_OPTIONS = [
  { value: "off", label: "Off" },
  { value: "muted", label: "Muted, tap to unmute" },
  { value: "on", label: "On" },
];

/** ``true``/``false`` from earlier versions mean ``on``/``off``. */
function normalizeVideoAudio(value) {
  if (value === true) return "on";
  if (value == null || value === false) return "off";
  if (VIDEO_AUDIO_OPTIONS.some((option) => option.value === value)) return value;
  throw new Error(`album-slideshow-card: unknown video_audio mode '${value}'`);
}

function normalizePhotoControls(value) {
  if (value == null || value === false) return "off";
  if (value === true) return "always";
  if (PHOTO_CONTROL_OPTIONS.some((option) => option.value === value)) return value;
  throw new Error(`album-slideshow-card: unknown photo controls mode '${value}'`);
}

// Caption overlay (date / location / description). ``show`` is an ordered
// subset of these fields; ``position`` is one of a 3x3 anchor grid;
// ``date_format`` is one of the named presets below or a custom token string.
const CAMERA_CAPTION_FIELDS = [
  "camera_make", "camera_model", "focal_length_mm", "aperture_f_number",
  "iso", "exposure_time_seconds",
];
const CAPTION_FIELDS = [
  "date", "location", "description", "camera", ...CAMERA_CAPTION_FIELDS,
  "filename", "path", "video_duration", "video_version", "current_date", "current_time", "weather",
];
const LIVE_CAPTION_FIELDS = new Set(["current_date", "current_time", "weather"]);
/** ``1:50`` / ``1:02:03`` for a clip length in seconds; empty otherwise. */
function formatClipLength(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return "";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = String(total % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}` : `${minutes}:${secs}`;
}

const CAPTION_POSITIONS = new Set([
  "top-left",
  "top-center",
  "top-right",
  "center-left",
  "center",
  "center-right",
  "bottom-left",
  "bottom-center",
  "bottom-right",
]);
// Named font weights surfaced in the editor, mapped to their CSS values.
const CAPTION_WEIGHT_MAP = {
  light: 300,
  normal: 400,
  medium: 500,
  semibold: 600,
  bold: 700,
};
const WEATHER_DISPLAYS = ["condition_temperature", "icon_temperature", "temperature"];
const WEATHER_ICON_STYLES = ["yr", "meteocons", "home_assistant"];
// Icon files in www/weather (MIT, see the LICENSE there) per HA weather condition. Yr has
// no wind or warning symbol, so those come from Meteocons.
const WEATHER_ICON_SETS = {
  yr: {
    "clear-night": "yr/clearsky_night",
    cloudy: "yr/cloudy",
    exceptional: "meteocons/code-red",
    fog: "yr/fog",
    hail: "yr/heavysleet",
    lightning: "yr/lightrainandthunder",
    "lightning-rainy": "yr/rainandthunder",
    partlycloudy: "yr/partlycloudy_day",
    "partlycloudy-night": "yr/partlycloudy_night",
    pouring: "yr/heavyrain",
    rainy: "yr/rain",
    snowy: "yr/snow",
    "snowy-rainy": "yr/sleet",
    sunny: "yr/clearsky_day",
    windy: "meteocons/wind",
    "windy-variant": "meteocons/wind",
  },
  meteocons: {
    "clear-night": "meteocons/clear-night",
    cloudy: "meteocons/cloudy",
    exceptional: "meteocons/code-red",
    fog: "meteocons/fog",
    hail: "meteocons/hail",
    lightning: "meteocons/thunderstorms",
    "lightning-rainy": "meteocons/thunderstorms-rain",
    partlycloudy: "meteocons/partly-cloudy-day",
    "partlycloudy-night": "meteocons/partly-cloudy-night",
    pouring: "meteocons/extreme-rain",
    rainy: "meteocons/rain",
    snowy: "meteocons/snow",
    "snowy-rainy": "meteocons/sleet",
    sunny: "meteocons/clear-day",
    windy: "meteocons/wind",
    "windy-variant": "meteocons/wind",
  },
};
const WEATHER_ICON_PATH = "/album_slideshow_static/weather";
const DATE_FORMAT_PRESETS = {
  full: { year: "numeric", month: "long", day: "numeric" },
  long: { year: "numeric", month: "long", day: "numeric" },
  medium: { year: "numeric", month: "short", day: "numeric" },
  short: { year: "numeric", month: "numeric", day: "numeric" },
  numeric: { year: "numeric", month: "numeric", day: "numeric" },
  month_year: { year: "numeric", month: "long" },
  year: { year: "numeric" },
  weekday: { weekday: "long", year: "numeric", month: "long", day: "numeric" },
};

/** Identify album_slideshow camera entities by their distinctive
 * ``frame_id`` attribute, which no other camera integration emits. */
function isAlbumSlideshowCamera(state) {
  return (
    state &&
    typeof state.entity_id === "string" &&
    state.entity_id.startsWith("camera.") &&
    state.attributes &&
    "frame_id" in state.attributes
  );
}

class PhotoControls {
  constructor(container, getHass, onActivity = () => {}, onNavigate = () => {}) {
    this._getHass = getHass;
    this._onActivity = onActivity;
    this._onNavigate = onNavigate;
    this._state = {};
    this._busy = false;
    this._root = container.attachShadow({ mode: "open" });
    this._root.innerHTML = `
      <style>
        :host { display: block; max-width: 100%; font-family: var(--paper-font-body1_-_font-family, sans-serif); }
        :host([hidden]) { display: none !important; }
        button { font: inherit; color: inherit; cursor: pointer; border: 0; border-radius: 6px; background: var(--secondary-background-color, #eee); padding: 8px 12px; display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 44px; }
        button:hover { filter: brightness(.94); }
        button:disabled { opacity: .4; cursor: default; }
        button:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
        .toolbar { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); width: 300px; max-width: 100%; box-sizing: border-box; gap: 4px 12px; padding: 4px; border-radius: 6px; background: var(--card-background-color, #fff); color: var(--primary-text-color, #222); }
        .control-group { display: flex; gap: 4px; justify-content: center; }
        .icon { width: 44px; height: 44px; padding: 10px; flex: 0 0 44px; }
        ha-icon { --mdc-icon-size: 22px; }
        dialog { box-sizing: border-box; width: min(460px, calc(100vw - 32px)); max-width: calc(100vw - 32px); max-height: min(640px, calc(100dvh - 40px)); border: 1px solid var(--divider-color, #ddd); border-radius: 8px; padding: 16px; font-size: 14px; background: var(--card-background-color, #fff); color: var(--primary-text-color, #222); }
        dialog::backdrop { background: rgba(0, 0, 0, .45); }
        header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
        h3 { margin: 0; font-size: 18px; letter-spacing: 0; }
        .choices { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
        .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--divider-color, #ddd); }
        .name { min-width: 0; overflow-wrap: anywhere; }
        small { display: block; color: var(--secondary-text-color, #666); margin-top: 4px; }
        footer { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
        .pagination { display: flex; gap: 8px; align-items: center; font-size: 13px; }
        .error { margin: 8px 0; max-width: 320px; padding: 8px; background: var(--card-background-color, #fff); color: var(--error-color, #b71c1c); overflow-wrap: anywhere; }
        [hidden] { display: none !important; }
      </style>
      <div class="toolbar">
        <div class="control-group" role="group" aria-label="Slideshow navigation">
          <button type="button" class="icon" id="previous" title="Previous slide" aria-label="Previous slide"><ha-icon icon="mdi:skip-previous"></ha-icon></button>
          <button type="button" class="icon" id="pause" title="Pause slideshow" aria-label="Pause slideshow"><ha-icon icon="mdi:pause"></ha-icon></button>
          <button type="button" class="icon" id="next" title="Next slide" aria-label="Next slide"><ha-icon icon="mdi:skip-next"></ha-icon></button>
        </div>
        <div class="control-group" role="group" aria-label="Photo visibility">
          <button type="button" class="icon" id="hide" title="Hide photo" aria-label="Hide photo"><ha-icon icon="mdi:eye-off"></ha-icon></button>
          <button type="button" class="icon" id="undo" title="Undo hide" aria-label="Undo hide"><ha-icon icon="mdi:undo"></ha-icon></button>
          <button type="button" class="icon" id="manage" title="Hidden photos" aria-label="Hidden photos"><ha-icon icon="mdi:image-off-outline"></ha-icon></button>
        </div>
      </div>
      <div id="error" class="error" role="alert" hidden></div>
      <dialog aria-labelledby="title">
        <header><h3 id="title">Hidden photos</h3><button type="button" class="icon" id="close" title="Close" aria-label="Close"><ha-icon icon="mdi:close"></ha-icon></button></header>
        <div id="dialog-error" class="error" role="alert" hidden></div>
        <div id="content"></div>
      </dialog>
    `;
    this._dialog = this._root.querySelector("dialog");
    this._dialog.addEventListener("close", () => this._onActivity());
    container.addEventListener("click", (event) => event.stopPropagation());
    this._root.getElementById("previous").addEventListener("click", () => this._navigate("previous_slide"));
    this._root.getElementById("next").addEventListener("click", () => this._navigate("next_slide"));
    this._root.getElementById("pause").addEventListener("click", () => this._togglePause());
    this._root.getElementById("hide").addEventListener("click", () => this._hide());
    this._root.getElementById("undo").addEventListener("click", () => this._run("undo_hide"));
    this._root.getElementById("manage").addEventListener("click", () => this._showHidden(0));
    this._root.getElementById("close").addEventListener("click", () => this._dialog.close());
  }

  get active() {
    return this._busy || this._dialog.open;
  }

  close() {
    this._dialog.close();
  }

  update(state) {
    if (this._state.entryId && this._state.entryId !== state.entryId) {
      this._dialog.close();
    }
    this._state = { ...state, photoIds: [...(state.photoIds || [])] };
    const unavailable = this._busy || !state.entryId;
    this._root.getElementById("previous").disabled = unavailable || !state.canPrevious;
    this._root.getElementById("next").disabled = unavailable || !state.canNext;
    const pause = this._root.getElementById("pause");
    pause.disabled = unavailable || typeof state.paused !== "boolean";
    pause.title = state.paused ? "Resume slideshow" : "Pause slideshow";
    pause.setAttribute("aria-label", pause.title);
    pause.querySelector("ha-icon").setAttribute("icon", state.paused ? "mdi:play" : "mdi:pause");
    this._root.getElementById("hide").disabled = this._busy || !state.entryId || !this._state.photoIds.some(Boolean);
    this._root.getElementById("undo").disabled = this._busy || !state.entryId || !state.canUndo;
    this._root.getElementById("manage").disabled = this._busy || !state.entryId;
    this._root.getElementById("manage").title = `Hidden photos (${state.hiddenCount || 0})`;
  }

  async _call(service, data = {}, entryId = this._state.entryId) {
    if (!entryId) throw new Error("This slideshow does not support these controls");
    const result = await this._getHass().callWS({
      type: "call_service",
      domain: "album_slideshow",
      service,
      service_data: { ...data, entry_id: entryId },
      ...(service === "list_hidden_photos" ? { return_response: true } : {}),
    });
    return result?.response;
  }

  async _navigate(service) {
    if (this._busy || !this._state.entryId) return false;
    const entryId = this._state.entryId;
    // Step from the slide on screen, which may be held behind the camera.
    const data = Number.isInteger(this._state.frameId) ? { frame_id: this._state.frameId } : {};
    this._onNavigate?.("start", entryId);
    const success = await this._run(service, data, entryId);
    this._onNavigate?.(success ? "complete" : "failed", entryId);
    return success;
  }

  async _togglePause() {
    const { entryId, paused } = this._state;
    if (!entryId || typeof paused !== "boolean") return false;
    return this._runAction(async () => {
      const hass = this._getHass();
      const registry = await hass.callWS({ type: "config/entity_registry/list" });
      const entity = registry.find((candidate) =>
        candidate.config_entry_id === entryId &&
        candidate.unique_id === `${entryId}_paused` &&
        candidate.entity_id.startsWith("switch.") &&
        !candidate.disabled_by,
      );
      if (!entity) throw new Error("The pause switch is unavailable for this slideshow");
      await hass.callWS({
        type: "call_service",
        domain: "switch",
        service: paused ? "turn_off" : "turn_on",
        service_data: { entity_id: entity.entity_id },
      });
    });
  }

  _error(message = "") {
    for (const id of ["error", "dialog-error"]) {
      const element = this._root.getElementById(id);
      element.textContent = message;
      element.hidden = !message;
    }
  }

  async _run(service, data = {}, entryId = this._state.entryId) {
    return this._runAction(() => this._call(service, data, entryId));
  }

  async _runAction(action) {
    if (this._busy) return false;
    this._busy = true;
    this._onActivity?.();
    this._error();
    this.update(this._state);
    const buttons = [...this._dialog.querySelectorAll("#content button")].map((button) => ({ button, disabled: button.disabled }));
    buttons.forEach(({ button }) => { button.disabled = true; });
    try {
      await action();
      this._dialog.close();
      return true;
    } catch (error) {
      this._error(error.message || "Photo action failed");
      return false;
    } finally {
      this._busy = false;
      this.update(this._state);
      buttons.forEach(({ button, disabled }) => { button.disabled = disabled; });
      this._onActivity?.();
    }
  }

  _button(label, icon, action, iconOnly = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.title = label;
    button.setAttribute("aria-label", label);
    if (iconOnly) button.className = "icon";
    const symbol = document.createElement("ha-icon");
    symbol.setAttribute("icon", icon);
    button.appendChild(symbol);
    if (!iconOnly) button.appendChild(document.createTextNode(label));
    button.addEventListener("click", action);
    return button;
  }

  _open(title) {
    this._viewGeneration = (this._viewGeneration || 0) + 1;
    this._error();
    this._root.getElementById("title").textContent = title;
    const content = this._root.getElementById("content");
    content.replaceChildren();
    if (!this._dialog.open) this._dialog.showModal();
    this._onActivity?.();
    return content;
  }

  _hide() {
    const { photoIds, orientation, entryId } = this._state;
    if (photoIds.length === 1 && photoIds[0]) {
      this._run("hide_photo", { photo_ids: [...photoIds] }, entryId);
      return;
    }
    const content = this._open("Hide photo");
    content.className = "choices";
    const labels = orientation === "vertical" ? ["Hide top photo", "Hide bottom photo"] : ["Hide left photo", "Hide right photo"];
    photoIds.forEach((photoId, index) => {
      const button = this._button(labels[index], "mdi:eye-off", () => this._run("hide_photo", { photo_ids: [photoId] }, entryId));
      button.disabled = !photoId;
      content.appendChild(button);
    });
    const both = this._button("Hide both photos", "mdi:eye-off", () => this._run("hide_photo", { photo_ids: [...photoIds] }, entryId));
    both.disabled = photoIds.length !== 2 || photoIds.some((photoId) => !photoId);
    content.appendChild(both);
  }

  async _showHidden(offset) {
    const entryId = this._state.entryId;
    const content = this._open("Hidden photos");
    const generation = this._viewGeneration;
    content.className = "";
    content.textContent = "Loading...";
    try {
      const page = await this._call("list_hidden_photos", { offset, limit: 25 }, entryId);
      if (generation !== this._viewGeneration || this._state.entryId !== entryId || !this._dialog.open) return;
      if (offset && !page.photos.length) {
        await this._showHidden(Math.max(0, offset - 25));
        return;
      }
      content.replaceChildren();
      if (!page.photos.length) content.textContent = "No hidden photos";
      for (const photo of page.photos) {
        const row = document.createElement("div");
        row.className = "row";
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = photo.name;
        if (!photo.in_album) {
          const status = document.createElement("small");
          status.textContent = "Not in the current album";
          name.appendChild(status);
        }
        row.append(name, this._button(`Restore ${photo.name}`, "mdi:restore", async () => {
          if (await this._run("restore_photos", { photo_ids: [photo.photo_id] }, entryId)) await this._showHidden(offset);
        }, true));
        content.appendChild(row);
      }
      if (!page.total) return;
      const footer = document.createElement("footer");
      footer.appendChild(this._button("Restore all", "mdi:restore", () => {
        const confirm = this._open("Restore all hidden photos?");
        confirm.className = "choices";
        confirm.append(
          this._button("Restore all", "mdi:restore", async () => {
            if (await this._run("restore_all_photos", {}, entryId)) await this._showHidden(0);
          }),
          this._button("Cancel", "mdi:close", () => this._showHidden(offset)),
        );
      }));
      const pagination = document.createElement("div");
      pagination.className = "pagination";
      const previous = this._button("Previous page", "mdi:chevron-left", () => this._showHidden(Math.max(0, offset - 25)), true);
      const next = this._button("Next page", "mdi:chevron-right", () => this._showHidden(offset + 25), true);
      previous.disabled = offset === 0;
      next.disabled = offset + page.photos.length >= page.total;
      pagination.append(previous, document.createTextNode(`${offset + 1}-${offset + page.photos.length} of ${page.total}`), next);
      footer.appendChild(pagination);
      content.appendChild(footer);
    } catch (error) {
      if (generation !== this._viewGeneration || this._state.entryId !== entryId) return;
      content.textContent = "";
      this._error(error.message || "Could not load hidden photos");
    }
  }
}

class PhotoControlsReveal {
  constructor(card, container, controls, resume) {
    this._card = card;
    this._container = container;
    this._controls = controls;
    this._resume = resume;
    this._visible = false;
    this._pointer = null;
    this._listeners = [];
    container.hidden = true;
    this._listen(card, "pointerenter", (event) => {
      if (event.pointerType === "mouse") this.show();
    });
    this._listen(card, "pointerleave", (event) => {
      this._cancelPress();
      if (event.pointerType === "mouse" && !this._keyboardFocused()) this.hide();
      else this.activity();
    });
    this._listen(card, "pointerdown", (event) => this._pointerDown(event));
    this._listen(card, "pointermove", (event) => this._pointerMove(event));
    this._listen(card, "pointerup", () => { this._cancelPress(); this.activity(); });
    this._listen(card, "pointercancel", () => { this._cancelPress(); this.activity(); });
    this._listen(card, "click", (event) => {
      if (event.composedPath().includes(container)) {
        this._suppressClick = false;
        this.activity();
      } else if (this._suppressClick && event.detail !== 0) {
        this._suppressClick = false;
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    }, true);
    this._listen(card, "contextmenu", (event) => {
      if (this._suppressClick || (this._pointer && this._pointer.type !== "mouse")) {
        event.preventDefault();
        event.stopPropagation();
        this._suppressClick = true;
        this.show();
      }
    });
    this._listen(card, "focusin", (event) => {
      if (!this._ignoreFocus && event.composedPath()[0]?.matches?.(":focus-visible")) this.show();
      else this.activity();
    });
    this._listen(card, "focusout", () => this.activity());
    this._listen(card, "keydown", (event) => this._keyDown(event));
    this._listen(card.ownerDocument, "pointerdown", (event) => {
      if (!event.composedPath().includes(card)) this.hide();
    }, true);
  }

  get holding() {
    return !this._disposed && (this._visible || this._pointer !== null);
  }

  _listen(target, type, listener, capture = false) {
    target.addEventListener(type, listener, capture);
    this._listeners.push(() => target.removeEventListener(type, listener, capture));
  }

  show() {
    if (this._disposed) return;
    this._visible = true;
    this._container.hidden = false;
    this.activity();
  }

  hide() {
    if (this._disposed || this._controls.active) return;
    const wasHolding = this.holding;
    this._visible = false;
    this._container.hidden = true;
    clearTimeout(this._idleTimer);
    this._idleTimer = null;
    this._cancelPress(false);
    if (wasHolding) this._resume();
  }

  activity() {
    if (this._disposed || !this._visible) return;
    clearTimeout(this._idleTimer);
    this._idleTimer = setTimeout(() => {
      this._idleTimer = null;
      if (this._controls.active || this._pointer || this._keyboardFocused()) return;
      this.hide();
    }, 5000);
  }

  _keyboardFocused() {
    return this._card.matches(":focus-visible") || Boolean(this._container.shadowRoot?.querySelector(":focus-visible"));
  }

  _pointerDown(event) {
    if (event.composedPath().includes(this._container)) {
      this.activity();
      return;
    }
    this._suppressClick = false;
    if (event.isPrimary === false || event.button > 0) {
      this._cancelPress();
      return;
    }
    this._cancelPress(false);
    this._pointer = { id: event.pointerId, type: event.pointerType, x: event.clientX, y: event.clientY };
    this._pressTimer = setTimeout(() => {
      this._pressTimer = null;
      if (!this._pointer || this._disposed) return;
      this._suppressClick = true;
      this.show();
    }, 500);
  }

  _pointerMove(event) {
    if (this._pointer) {
      if (event.pointerId === this._pointer.id && Math.hypot(event.clientX - this._pointer.x, event.clientY - this._pointer.y) > 10) this._cancelPress();
    } else if (event.pointerType === "mouse") {
      this.show();
    }
  }

  _cancelPress(resume = true) {
    const wasPressed = this._pointer !== null;
    clearTimeout(this._pressTimer);
    this._pressTimer = null;
    this._pointer = null;
    if (resume && wasPressed && !this._visible && !this._disposed) this._resume();
  }

  _keyDown(event) {
    if (event.key === "Escape" && this._visible && !this._controls.active) {
      event.preventDefault();
      event.stopPropagation();
      this.hide();
      this._ignoreFocus = true;
      this._card.focus({ preventScroll: true });
      this._ignoreFocus = false;
    } else if ((event.key === "Enter" || event.key === " ") && event.composedPath()[0] === this._card) {
      event.preventDefault();
      event.stopPropagation();
      this.show();
      this._container.shadowRoot?.querySelector("button:not([disabled])")?.focus();
    } else {
      this.activity();
    }
  }

  dispose() {
    this._disposed = true;
    clearTimeout(this._idleTimer);
    this._cancelPress(false);
    this._listeners.forEach((remove) => remove());
    this._listeners = [];
    this._controls.close();
    this._visible = false;
  }
}

// The card class is built lazily by a factory so the base class can be
// resolved from the *live* ``window.HTMLElement`` at registration time.
// See ``defineAlbumSlideshowCards`` for why this matters with the
// scoped-custom-element-registry polyfill.
function createAlbumSlideshowCardClass(Base) {
  return class AlbumSlideshowCard extends Base {
  static getStubConfig(hass) {
    let entity = "";
    if (hass && hass.states) {
      for (const id of Object.keys(hass.states)) {
        if (isAlbumSlideshowCamera(hass.states[id])) {
          entity = id;
          break;
        }
      }
    }
    return {
      type: "custom:album-slideshow-card",
      entity,
      transition: "random",
      duration: 600,
    };
  }

  static getConfigElement() {
    return document.createElement("album-slideshow-card-editor");
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._showing = "a"; // which layer is on top
    this._lastFrameId = null;
    this._lastEntityPicture = null;
    this._lastRandomTransition = null;
    this._currentTransition = null; // class applied to .layer right now
    this._rendered = false;
    // Suspend visual swaps for a while after the user taps, so the
    // photo they're looking at in the more-info dialog stays put on
    // the card behind it. A new state update during the hold window
    // schedules a deferred swap that runs once the hold expires.
    this._holdSwapsUntil = 0;
    this._holdSwapTimer = null;
    this._loadGeneration = 0;
    this._displayedPhotoIds = [];
    this._displayedFrameId = null;
    this._navigationRequest = null;
    this._navigationTimer = null;
    this._photoEntryId = null;
    this._hiddenRevision = undefined;
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("album-slideshow-card: 'entity' is required");
    }
    if (!config.entity.startsWith("camera.")) {
      throw new Error("album-slideshow-card: 'entity' must be a camera entity");
    }
    const transition = (config.transition || "random").toLowerCase();
    if (!TRANSITIONS.has(transition)) {
      throw new Error(
        `album-slideshow-card: unknown transition '${transition}'`,
      );
    }
    const fit = (config.fit || "auto").toLowerCase();
    if (!FIT_MODES.has(fit)) {
      throw new Error(`album-slideshow-card: unknown fit '${fit}'`);
    }
    if (config.captions != null && !Array.isArray(config.captions)) {
      throw new Error("album-slideshow-card: 'captions' must be a list");
    }
    const captions = (config.captions ?? [config.caption])
      .map((caption) => this._normalizeCaption(caption)).filter(Boolean);
    this._config = {
      ...config,
      transition,
      duration: Number(config.duration ?? 600),
      easing: config.easing || "ease-in-out",
      aspect_ratio: config.aspect_ratio || "16/9",
      fit,
      // Empty/missing background means inherit theme.
      background: typeof config.background === "string" ? config.background : "",
      tap_action: config.tap_action === "more-info" ? "more-info" : "none",
      videos: config.videos !== false,
      video_audio: normalizeVideoAudio(config.video_audio),
      photo_controls: normalizePhotoControls(config.photo_controls),
      // Number of seconds the card freezes its visible slide after a
      // tap, so the more-info dialog can settle without the slideshow
      // marching forward beneath it. Set to 0 to disable.
      tap_pause_seconds:
        config.tap_pause_seconds === 0
          ? 0
          : Number(config.tap_pause_seconds ?? 8),
      caption: captions[0] || null,
      captions,
    };
    if (this._rendered) {
      // Config edited live; rebuild styles + reset state.
      this._renderShell();
      this._lastFrameId = null;
      this._lastEntityPicture = null;
      this._currentTransition = null;
      this._maybeSwap();
    }
  }

  /** Normalize the ``caption`` config into a stable shape, or ``null`` when
   * the overlay is disabled. Accepts ``true`` (all defaults), an object, a
   * comma/space separated ``show`` string, etc. Returns ``null`` when there
   * is nothing to show so the rest of the card can cheaply skip captions. */
  _normalizeCaption(raw) {
    if (raw == null || raw === false) return null;
    if (raw === true) raw = {};
    if (typeof raw !== "object") return null;
    if (raw.enabled === false) return null;
    let show = raw.show;
    if (typeof show === "string") show = show.split(/[,\s]+/);
    if (!Array.isArray(show)) show = ["date", "location"];
    show = show
      .map((s) => String(s).toLowerCase().trim())
      .filter((s) => CAPTION_FIELDS.includes(s));
    show = [...new Set(show)];
    if (show.length === 0) return null;
    let position = String(raw.position || "bottom-left").toLowerCase();
    if (!CAPTION_POSITIONS.has(position)) position = "bottom-left";
    const color =
      typeof raw.color === "string" && raw.color.trim()
        ? raw.color.trim()
        : "#ffffff";
    const fontSize =
      typeof raw.font_size === "string" && raw.font_size.trim()
        ? raw.font_size.trim()
        : "14px";
    let fontWeight = String(raw.font_weight || "medium").toLowerCase();
    if (!(fontWeight in CAPTION_WEIGHT_MAP)) fontWeight = "medium";
    return {
      show,
      position,
      per_image: raw.per_image !== false,
      date_format: raw.date_format != null ? String(raw.date_format) : "medium",
      current_date_format: String(raw.current_date_format ?? raw.date_format ?? "medium"),
      time_format: ["auto", "12h", "24h"].includes(raw.time_format) ? raw.time_format : "auto",
      time_seconds: raw.time_seconds === true,
      weather_entity: typeof raw.weather_entity === "string" && /^(weather|sensor)\.[a-z0-9_]+$/.test(raw.weather_entity.trim())
        ? raw.weather_entity.trim() : null,
      weather_display: WEATHER_DISPLAYS.includes(raw.weather_display) ? raw.weather_display : "condition_temperature",
      weather_icons: WEATHER_ICON_STYLES.includes(raw.weather_icons) ? raw.weather_icons : "yr",
      color,
      font_size: fontSize,
      font_weight: fontWeight,
      shadow: raw.shadow !== false,
    };
  }

  getCardSize() {
    return 4;
  }

  connectedCallback() {
    if (!this._rendered) {
      this._renderShell();
      this._rendered = true;
    } else {
      this._setupPhotoControlsReveal();
    }
    this._captionResizeObserver ??= new ResizeObserver(() => this._fitCaptions());
    this._captionResizeObserver.observe(this);
    this._maybeSwap();
  }

  disconnectedCallback() {
    this._captionResizeObserver?.disconnect();
    clearTimeout(this._captionClockTimer);
    this._captionClockTimer = null;
    this._cancelControlNavigation();
    this._controlsReveal?.dispose();
    this._controlsReveal = null;
    clearTimeout(this._holdSwapTimer);
    this._holdSwapTimer = null;
    this._loadGeneration += 1;
    this._lastFrameId = null;
    this._lastEntityPicture = null;
    this._stopVideo();
  }

  set hass(hass) {
    const previous = this._hass;
    this._hass = hass;
    if (!this._rendered) return;
    this._maybeSwap();
    if (this._captionData && this._captionConfigs().some((cap) => cap.show.includes("weather") && cap.weather_entity &&
      (previous?.states?.[cap.weather_entity] !== hass.states?.[cap.weather_entity] || previous?.locale !== hass.locale ||
        (cap.weather_display === "icon_temperature" &&
          previous?.states?.["sun.sun"]?.state !== hass.states?.["sun.sun"]?.state)))) {
      this._renderCaptions(this._captionData, false);
    }
  }

  _resolvedFit(attrs) {
    // ``auto`` inherits from the camera's fill_mode attribute. The camera
    // exposes cover / contain / blur. ``blur`` is rendered as ``contain``
    // plus a blurred backdrop layer.
    const cardFit = this._config.fit;
    if (cardFit !== "auto") {
      return { fit: cardFit, blurBackdrop: false };
    }
    const cameraFill = (attrs && attrs.fill_mode) || "cover";
    if (cameraFill === "contain") return { fit: "contain", blurBackdrop: false };
    if (cameraFill === "blur") return { fit: "contain", blurBackdrop: true };
    return { fit: "cover", blurBackdrop: false };
  }

  _renderShell() {
    const c = this._config;
    clearTimeout(this._captionClockTimer);
    this._captionClockTimer = null;
    this._captionData = null;
    this._cancelControlNavigation();
    this._controlsReveal?.dispose();
    this._controlsReveal = null;
    this._loadGeneration += 1;
    this._displayedPhotoIds = [];
    this._displayedFrameId = null;
    this._photoEntryId = null;
    this._hiddenRevision = undefined;
    const aspect = c.aspect_ratio === "auto" ? "auto" : c.aspect_ratio;
    // When the user did not set ``background`` we fall through to the
    // theme's --ha-card-background, so the card naturally inherits the
    // dashboard theme. When set, the user's color wins.
    const stageBg = c.background
      ? c.background
      : "var(--ha-card-background, var(--card-background-color, transparent))";
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card {
          /* Inherit border, radius, shadow, background from theme. */
          overflow: hidden;
          ${aspect === "auto" ? "" : `aspect-ratio: ${aspect};`}
          ${c.background ? `background: ${c.background};` : ""}
          position: relative;
          padding: 0;
        }
        ha-card:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
        .stage {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          background: ${stageBg};
          border-radius: inherit;
          overflow: hidden;
          ${c.photo_controls === "on_demand" ? "-webkit-touch-callout: none; user-select: none;" : ""}
        }
        .blur-bg {
          position: absolute;
          inset: -5%;
          width: 110%;
          height: 110%;
          object-fit: cover;
          filter: blur(24px) brightness(0.75);
          opacity: 0;
          transition: opacity ${c.duration}ms ${c.easing};
          pointer-events: none;
          user-select: none;
        }
        .blur-bg.show { opacity: 1; }
        .layer {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          object-fit: cover;
          opacity: 0;
          will-change: opacity, transform, clip-path;
          transition:
            opacity ${c.duration}ms ${c.easing},
            transform ${c.duration}ms ${c.easing},
            clip-path ${c.duration}ms ${c.easing};
          backface-visibility: hidden;
          transform: translateZ(0);
          pointer-events: none;
          user-select: none;
        }
        .layer.fit-cover { object-fit: cover; }
        .layer.fit-contain { object-fit: contain; }
        .layer.show { opacity: 1; }
        .video {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          object-fit: cover;
          opacity: 0;
          transition: opacity ${c.duration}ms ${c.easing};
          pointer-events: none;
        }
        .video.fit-contain { object-fit: contain; }
        .video.show { opacity: 1; }
        .sound {
          position: absolute;
          top: 8px;
          left: 8px;
          z-index: 3;
          display: grid;
          place-items: center;
          width: 40px;
          height: 40px;
          padding: 0;
          border: none;
          border-radius: 50%;
          background: rgba(0, 0, 0, 0.45);
          color: #fff;
          cursor: pointer;
        }
        .sound[hidden] { display: none; }
        .placeholder {
          position: absolute;
          inset: 0;
          display: grid;
          place-items: center;
          color: var(--secondary-text-color, rgba(255, 255, 255, 0.5));
          font-size: 0.85rem;
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
        }
        .captions {
          position: absolute;
          inset: 0;
          pointer-events: none;
          opacity: 1;
          transition: opacity ${c.duration}ms ${c.easing};
        }
        .cap-region {
          position: absolute;
          display: flex;
          padding: 3.5% 4%;
          box-sizing: border-box;
          min-width: 0;
          overflow: hidden;
        }
        .cap-box {
          display: flex;
          flex-direction: column;
          flex-shrink: 0;
          max-width: 100%;
          max-height: 100%;
          min-width: 0;
          overflow: hidden;
          overflow-wrap: anywhere;
          line-height: 1.25;
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
        }
        .cap-stack {
          display: flex; flex-direction: column; gap: 4px;
          max-width: 100%; max-height: 100%; min-width: 0; overflow: hidden;
        }
        .cap-line { font-weight: inherit; flex-shrink: 0; font-variant-numeric: tabular-nums; }
        #photo-controls { position: absolute; right: 8px; max-width: calc(100% - 16px); ${c.caption?.position.startsWith("top") ? "bottom" : "top"}: 8px; z-index: 3; }
        .cap-box.cap-shadow {
          text-shadow:
            0 1px 2px rgba(0, 0, 0, 0.9),
            0 1px 6px rgba(0, 0, 0, 0.55);
        }
        .cap-icon { display: inline-block; width: 1.8em; height: 1.8em; margin: 0 0.12em 0 -0.1em; vertical-align: middle; }
        .cap-icon svg { display: block; width: 100%; height: 100%; }
        .cap-shadow .cap-icon { filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.6)); }
        .cap-icon .sun { fill: var(--weather-icon-sun-color, #fdd93c); }
        .cap-icon .moon { fill: var(--weather-icon-moon-color, #fcf497); }
        .cap-icon .cloud-back { fill: var(--weather-icon-cloud-back-color, #d4d4d4); }
        .cap-icon .cloud-front { fill: var(--weather-icon-cloud-front-color, #f9f9f9); }
        .cap-icon .rain { fill: var(--weather-icon-rain-color, #30b3ff); }
        .cap-icon .snow {
          fill: var(--weather-icon-snow-color, #f9f9f9);
          stroke: var(--weather-icon-snow-stroke-color, #d4d4d4);
          stroke-width: 1;
          paint-order: stroke;
        }
        ${this._transitionStyles()}
      </style>
      <ha-card part="card">
        <div class="stage" id="stage">
          <img class="blur-bg" id="blur-a" alt="" />
          <img class="blur-bg" id="blur-b" alt="" />
          <img class="layer" id="a" alt="" />
          <img class="layer" id="b" alt="" />
          <video class="video" id="video" muted playsinline loop preload="auto" disablepictureinpicture aria-hidden="true"></video>
          <div class="captions" id="captions" aria-hidden="true"></div>
          <div class="placeholder" id="placeholder">Waiting for first frame...</div>
        </div>
        <div id="photo-controls" ${c.photo_controls === "always" ? "" : "hidden"}></div>
        <button class="sound" id="sound" type="button" hidden aria-label="Unmute video"><ha-icon icon="mdi:volume-off"></ha-icon></button>
      </ha-card>
    `;
    this._photoControls = new PhotoControls(
      this.shadowRoot.getElementById("photo-controls"), () => this._hass,
      () => this._controlsReveal?.activity(),
      (phase, entryId) => this._controlNavigation(phase, entryId),
    );
    this.shadowRoot.getElementById("sound").addEventListener("click", (event) => {
      // Never let the speaker button open more-info or reveal controls.
      event.stopPropagation();
      this._toggleVideoSound();
    });
    const card = this.shadowRoot.querySelector("ha-card");
    if (this._config.tap_action === "more-info") {
      card.addEventListener("click", () => this._fireMoreInfo());
      card.style.cursor = "pointer";
    }
    this._setupPhotoControlsReveal();
  }

  _setupPhotoControlsReveal() {
    this._controlsReveal?.dispose();
    this._controlsReveal = null;
    const card = this.shadowRoot.querySelector("ha-card");
    const container = this.shadowRoot.getElementById("photo-controls");
    container.hidden = this._config.photo_controls !== "always";
    if (this._config.photo_controls === "on_demand") {
      card.tabIndex = 0;
      card.setAttribute("role", "group");
      card.setAttribute("aria-label", "Slideshow photo controls");
      if (this.isConnected) {
        this._controlsReveal = new PhotoControlsReveal(card, container, this._photoControls, () => this._maybeSwap());
      }
    }
  }

  _cancelControlNavigation() {
    clearTimeout(this._navigationTimer);
    this._navigationTimer = null;
    this._navigationRequest = null;
  }

  _controlNavigation(phase, entryId) {
    const attrs = this._hass?.states[this._config.entity]?.attributes || {};
    if (attrs.entry_id !== entryId) return;
    if (phase === "start") {
      this._cancelControlNavigation();
      this._navigationRequest = { entryId, frameId: attrs.frame_id, pending: true };
      this._holdSwapsUntil = 0;
      clearTimeout(this._holdSwapTimer);
      this._holdSwapTimer = null;
    } else if (this._navigationRequest?.entryId === entryId) {
      if (phase === "failed") {
        this._cancelControlNavigation();
      } else {
        this._navigationRequest.pending = false;
        if (this._displayedFrameId !== this._navigationRequest.frameId && this._displayedFrameId === attrs.frame_id) {
          this._cancelControlNavigation();
        } else {
          this._navigationTimer = setTimeout(() => this._cancelControlNavigation(), 5000);
        }
      }
    }
    this._maybeSwap();
  }

  _transitionStyles() {
    // Every animated variant is emitted under a ``t-<name>`` modifier
    // class so a single shell can host any of them. ``_performSwap``
    // picks one (or a random one) and tags both layers per swap.
    return `
      .layer.t-none { transition: none !important; }
      .layer.t-none.enter { opacity: 0; }
      .layer.t-none.show { opacity: 1; }
      .layer.t-none.exit { opacity: 0; }

      .layer.t-fade.enter { opacity: 0; }
      .layer.t-fade.show { opacity: 1; }
      .layer.t-fade.exit { opacity: 0; }

      .layer.t-slide-left.enter { opacity: 1; transform: translateX(100%); }
      .layer.t-slide-left.show { opacity: 1; transform: translateX(0); }
      .layer.t-slide-left.exit { opacity: 1; transform: translateX(-100%); }

      .layer.t-slide-right.enter { opacity: 1; transform: translateX(-100%); }
      .layer.t-slide-right.show { opacity: 1; transform: translateX(0); }
      .layer.t-slide-right.exit { opacity: 1; transform: translateX(100%); }

      .layer.t-slide-up.enter { opacity: 1; transform: translateY(100%); }
      .layer.t-slide-up.show { opacity: 1; transform: translateY(0); }
      .layer.t-slide-up.exit { opacity: 1; transform: translateY(-100%); }

      .layer.t-slide-down.enter { opacity: 1; transform: translateY(-100%); }
      .layer.t-slide-down.show { opacity: 1; transform: translateY(0); }
      .layer.t-slide-down.exit { opacity: 1; transform: translateY(100%); }

      .layer.t-wipe-left.enter { opacity: 1; clip-path: inset(0 0 0 100%); }
      .layer.t-wipe-left.show { opacity: 1; clip-path: inset(0 0 0 0); }
      .layer.t-wipe-left.exit { opacity: 1; clip-path: inset(0 0 0 0); }

      .layer.t-wipe-right.enter { opacity: 1; clip-path: inset(0 100% 0 0); }
      .layer.t-wipe-right.show { opacity: 1; clip-path: inset(0 0 0 0); }
      .layer.t-wipe-right.exit { opacity: 1; clip-path: inset(0 0 0 0); }

      .layer.t-zoom.enter { opacity: 0; transform: scale(1.05); }
      .layer.t-zoom.show { opacity: 1; transform: scale(1); }
      .layer.t-zoom.exit { opacity: 0; transform: scale(1); }
    `;
  }

  _pickTransition() {
    const cfg = this._config.transition;
    if (cfg !== "random") return cfg;
    // Try not to repeat the previous random pick when more than one option
    // is available; users perceive the "random" effect more strongly when
    // consecutive slides differ.
    const pool = ANIMATED_TRANSITIONS.filter(
      (t) => t !== this._lastRandomTransition,
    );
    const choices = pool.length > 0 ? pool : ANIMATED_TRANSITIONS;
    const pick = choices[Math.floor(Math.random() * choices.length)];
    this._lastRandomTransition = pick;
    return pick;
  }

  _maybeSwap() {
    const hass = this._hass;
    if (!hass) return;
    const state = hass.states[this._config.entity];
    if (!state) {
      this._setPlaceholder(`Entity not found: ${this._config.entity}`);
      return;
    }
    const attrs = state.attributes || {};
    if (this._hiddenRevision !== undefined && attrs.hidden_revision !== this._hiddenRevision) {
      this._clearDisplayedPhotos();
    }
    this._hiddenRevision = attrs.hidden_revision;
    this._refreshPhotoControls(attrs);
    this._syncVideoPaused(attrs);
    if (attrs.empty_reason || (Array.isArray(attrs.displayed_photo_ids) && !attrs.displayed_photo_ids.length)) {
      this._clearDisplayedPhotos();
      this._setPlaceholder(attrs.empty_reason === "all_hidden" ? "All photos hidden" : attrs.empty_reason ? "No matching photos" : "Preparing next photo...");
      return;
    }
    if (this._controlsReveal?.holding && this._displayedPhotoIds.length && !this._navigationRequest) return;
    // Hold visual swaps for the configured grace period after a tap.
    // The state cursor (`_lastFrameId`/`_lastEntityPicture`) is left
    // untouched during the hold; once the hold expires we re-enter
    // ``_maybeSwap`` and pick up whatever frame is currently latest.
    const now = Date.now();
    if (now < this._holdSwapsUntil) {
      if (!this._holdSwapTimer) {
        const wait = this._holdSwapsUntil - now + 50;
        this._holdSwapTimer = setTimeout(() => {
          this._holdSwapTimer = null;
          this._maybeSwap();
        }, wait);
      }
      return;
    }
    // ``frame_id`` increments on every slide commit; that's our primary
    // "new frame ready" signal. The integration also embeds frame_id in
    // ``entity_picture`` so that HA core surfaces (more-info, picture
    // tiles) cache-bust naturally. We piggyback frame_id in our query
    // string here for older integration versions that don't yet do that.
    const frameId = attrs.frame_id ?? null;
    const entityPicture = state.attributes.entity_picture;
    if (
      frameId === this._lastFrameId &&
      entityPicture === this._lastEntityPicture
    ) {
      return;
    }
    this._lastFrameId = frameId;
    this._lastEntityPicture = entityPicture;
    if (!entityPicture) {
      this._setPlaceholder("Camera not ready");
      return;
    }
    let url = entityPicture;
    if (frameId !== null && !/[?&]frame=/.test(url)) {
      const sep = url.includes("?") ? "&" : "?";
      url = `${url}${sep}_frame=${frameId}`;
    }
    const { fit, blurBackdrop } = this._resolvedFit(attrs);
    // Snapshot the caption-relevant attributes now so the overlay swaps
    // in lockstep with the image it describes (state may advance again
    // while the next image is still decoding).
    const captionData = this._config.caption
      ? {
          caption_frames: attrs.caption_frames,
          pair_orientation: attrs.pair_orientation,
          captured_at_primary: attrs.captured_at_primary,
          captured_at: attrs.captured_at,
          location: attrs.location,
          latitude: attrs.latitude,
          longitude: attrs.longitude,
          description: attrs.description,
          ...Object.fromEntries(CAMERA_CAPTION_FIELDS.map((field) => [field, attrs[field]])),
        }
      : null;
    const photoData = {
      photoIds: [...(attrs.displayed_photo_ids || [])],
      entryId: attrs.entry_id,
      orientation: attrs.pair_orientation,
      frameId,
      hiddenRevision: attrs.hidden_revision,
      entityId: this._config.entity,
      videoUrl:
        this._config.videos !== false && ["video", "live_photo"].includes(attrs.media_kind) && attrs.video_url
          ? attrs.video_url
          : null,
      // A Live Photo's motion plays once, then fades back to its still.
      videoLoop: attrs.media_kind !== "live_photo",
    };
    this._loadAndSwap(url, fit, blurBackdrop, captionData, photoData);
  }

  _clearDisplayedPhotos() {
    clearTimeout(this._captionClockTimer);
    this._captionClockTimer = null;
    this._captionData = null;
    this._loadGeneration += 1;
    this._displayedPhotoIds = [];
    this._displayedFrameId = null;
    this._lastFrameId = null;
    this._lastEntityPicture = null;
    this._holdSwapsUntil = 0;
    if (this._holdSwapTimer) clearTimeout(this._holdSwapTimer);
    this._holdSwapTimer = null;
    for (const id of ["a", "b", "blur-a", "blur-b"]) {
      const image = this.shadowRoot.getElementById(id);
      image.removeAttribute("src");
      image.classList.remove("show", "exit", "enter");
    }
    this._stopVideo();
    this.shadowRoot.getElementById("captions").replaceChildren();
    this._refreshPhotoControls(this._hass.states[this._config.entity]?.attributes || {});
  }

  _refreshPhotoControls(attrs) {
    this._photoControls?.update({
      entryId: attrs.entry_id,
      photoIds: this._displayedPhotoIds,
      frameId: this._displayedFrameId,
      orientation: this._photoOrientation,
      hiddenCount: attrs.hidden_photo_count,
      canUndo: attrs.undo_hide_available,
      canPrevious: attrs.previous_frames_cached > 0,
      canNext: attrs.media_count > 0,
      paused: attrs.paused,
    });
  }

  _loadAndSwap(url, fit, blurBackdrop, captionData, photoData) {
    const generation = ++this._loadGeneration;
    // Pre-decode the new image so the swap is instant.
    const next = new Image();
    next.decoding = "async";
    next.onload = () => {
      if (generation !== this._loadGeneration) return;
      if (this._controlsReveal?.holding && this._displayedPhotoIds.length && !this._navigationRequest) {
        this._lastFrameId = null;
        this._lastEntityPicture = null;
        return;
      }
      const attrs = this._hass.states[this._config.entity]?.attributes || {};
      if (photoData.entityId !== this._config.entity || (attrs.frame_id ?? null) !== photoData.frameId || attrs.hidden_revision !== photoData.hiddenRevision) {
        this._lastFrameId = null;
        this._maybeSwap();
        return;
      }
      this._displayedPhotoIds = [...photoData.photoIds];
      this._displayedFrameId = photoData.frameId;
      this._photoEntryId = photoData.entryId;
      this._photoOrientation = photoData.orientation;
      this._performSwap(url, fit, blurBackdrop, captionData);
      this._startVideo(photoData.videoUrl, fit, generation, photoData.videoLoop !== false);
      if (this._navigationRequest && !this._navigationRequest.pending && photoData.frameId !== this._navigationRequest.frameId) {
        this._cancelControlNavigation();
      }
      this._refreshPhotoControls(attrs);
    };
    next.onerror = () => {
      if (generation === this._loadGeneration) this._setPlaceholder("Failed to load slide");
    };
    next.src = url;
  }

  /** Play a video slide over its poster frame (already swapped in). The
   * clip fades in once it is actually playing; if it can't load or the
   * browser blocks autoplay, the poster simply stays up. */
  _startVideo(videoUrl, fit, generation, loop = true) {
    const video = this.shadowRoot?.getElementById("video");
    if (!video) return;
    // A re-render of the same clip (metadata refreshes do this) keeps the
    // same URL; let it play on rather than restarting it.
    const continuing = Boolean(videoUrl) && video.getAttribute("src") === videoUrl;
    if (!continuing) this._stopVideo();
    if (!videoUrl) return;
    const attrs = this._hass?.states[this._config.entity]?.attributes || {};
    const reveal = () => {
      if (generation === this._loadGeneration) video.classList.add("show");
    };
    video.classList.toggle("fit-contain", fit === "contain");
    video.onerror = () => video.classList.remove("show");
    // Fade in once playing, or, while paused, once the first frame is ready.
    video.onplaying = reveal;
    video.onloadeddata = attrs.paused ? reveal : null;
    video.onended = loop ? null : () => {
      if (generation === this._loadGeneration) video.classList.remove("show");
    };
    video.onvolumechange = () => this._updateSoundButton();
    if (continuing) return;
    // Browsers only autoplay muted unless the page has been interacted
    // with; ``_playVideo`` falls back to muted. Set the properties as well
    // as the attributes, which some browsers read only at parse time.
    video.muted = !this._wantsVideoSound();
    video.loop = loop;
    video.playsInline = true;
    video.src = videoUrl;
    this._videoPaused = Boolean(attrs.paused);
    this._updateSoundButton();
    if (!attrs.paused) this._playVideo(video);
  }

  /** Sound for the next video: the speaker button's last choice this
   * session, else the configured mode. */
  _wantsVideoSound() {
    if (this._config.video_audio === "off") return false;
    return this._videoSoundChoice ?? this._config.video_audio === "on";
  }

  /** Play, falling back to muted when the browser blocks sound. */
  _playVideo(video) {
    video.play()?.catch((err) => {
      if (video.muted || err?.name !== "NotAllowedError") return;
      video.muted = true;
      this._updateSoundButton();
      video.play()?.catch(() => {});
    });
  }

  _toggleVideoSound() {
    const video = this.shadowRoot?.getElementById("video");
    if (!video || !video.getAttribute("src")) return;
    // The tap itself lets the browser allow sound from now on.
    video.muted = !video.muted;
    this._videoSoundChoice = !video.muted;
    if (!video.muted && video.paused && !this._videoPaused) this._playVideo(video);
    this._updateSoundButton();
  }

  _updateSoundButton() {
    const button = this.shadowRoot?.getElementById("sound");
    if (!button) return;
    const video = this.shadowRoot.getElementById("video");
    const active = this._config.video_audio !== "off" && Boolean(video?.getAttribute("src"));
    button.hidden = !active;
    if (!active) return;
    button.setAttribute("aria-label", video.muted ? "Unmute video" : "Mute video");
    button.querySelector("ha-icon")?.setAttribute("icon", video.muted ? "mdi:volume-off" : "mdi:volume-high");
  }

  /** Stop any playing clip and drop its source so it stops downloading. */
  _stopVideo() {
    const video = this.shadowRoot?.getElementById("video");
    if (!video || !video.getAttribute("src")) return;
    video.classList.remove("show");
    video.onplaying = video.onerror = video.onloadeddata = video.onended = video.onvolumechange = null;
    video.pause();
    video.removeAttribute("src");
    video.load();
    this._updateSoundButton();
  }

  /** Follow the slideshow's pause switch. Acts only when it changes, since
   * ``hass`` updates arrive for every entity in Home Assistant. */
  _syncVideoPaused(attrs) {
    const video = this.shadowRoot?.getElementById("video");
    const paused = Boolean(attrs.paused);
    if (!video || !video.getAttribute("src") || paused === this._videoPaused) return;
    this._videoPaused = paused;
    if (paused) video.pause();
    else this._playVideo(video);
  }

  _performSwap(url, fit, blurBackdrop, captionData) {
    const root = this.shadowRoot;
    const placeholder = root.getElementById("placeholder");
    if (placeholder) placeholder.remove();

    const a = root.getElementById("a");
    const b = root.getElementById("b");
    const blurA = root.getElementById("blur-a");
    const blurB = root.getElementById("blur-b");
    const showing = this._showing === "a" ? a : b;
    const hidden = this._showing === "a" ? b : a;
    const showingBlur = this._showing === "a" ? blurA : blurB;
    const hiddenBlur = this._showing === "a" ? blurB : blurA;

    // Apply fit class to both layers (cheap; idempotent).
    for (const el of [a, b]) {
      el.classList.remove("fit-cover", "fit-contain");
      el.classList.add(fit === "contain" ? "fit-contain" : "fit-cover");
    }

    const transition = this._pickTransition();
    const transitionClass = `t-${transition}`;

    // First frame: no animation, just place the image and reveal.
    if (!showing.src) {
      showing.src = url;
      hidden.src = url;
      showing.classList.add(transitionClass, "show");
      hidden.classList.add(transitionClass);
      if (blurBackdrop) {
        showingBlur.src = url;
        hiddenBlur.src = url;
        showingBlur.classList.add("show");
      }
      this._currentTransition = transitionClass;
      this._renderCaptions(captionData, false);
      return;
    }

    // Drop the previous transition class from both layers before applying
    // the new one. Keeps the class list bounded under "random" mode.
    if (this._currentTransition && this._currentTransition !== transitionClass) {
      a.classList.remove(this._currentTransition);
      b.classList.remove(this._currentTransition);
    }
    this._currentTransition = transitionClass;

    hidden.src = url;
    hidden.classList.remove("show", "exit", "enter");
    hidden.classList.add(transitionClass, "enter");
    // Force a layout flush so the browser sees the "enter" pose before
    // we transition to "show".
    // eslint-disable-next-line no-unused-expressions
    hidden.offsetWidth;
    hidden.classList.remove("enter");
    hidden.classList.add("show");

    showing.classList.remove("show", "enter");
    showing.classList.add(transitionClass, "exit");

    // Blurred backdrop layer (only used when fill_mode resolves to blur).
    if (blurBackdrop) {
      hiddenBlur.src = url;
      hiddenBlur.classList.add("show");
      showingBlur.classList.remove("show");
    } else {
      showingBlur.classList.remove("show");
      hiddenBlur.classList.remove("show");
    }

    this._showing = this._showing === "a" ? "b" : "a";

    // Cross-fade the caption overlay in time with the image it describes.
    this._renderCaptions(captionData, true);

    // Cleanup the .exit class after the animation so it doesn't fight the
    // next swap. Slightly longer than the duration to be safe.
    const dur = this._config.duration + 50;
    setTimeout(() => {
      showing.classList.remove("exit");
    }, dur);
  }

  _renderCaptions(captionData, fade) {
    clearTimeout(this._captionClockTimer);
    this._captionClockTimer = null;
    this._captionData = captionData;
    const captions = this._captionConfigs();
    const container =
      this.shadowRoot && this.shadowRoot.getElementById("captions");
    if (!container) return;
    container.innerHTML = "";
    if (!captions.length || !captionData) return;

    const frames = this._buildCaptionFrames(captionData);
    const orientation = captionData.pair_orientation;
    for (const cap of captions) {
      const isPair = cap.per_image && cap.show.some((field) => !LIVE_CAPTION_FIELDS.has(field)) && frames.length >= 2 &&
        (orientation === "horizontal" || orientation === "vertical");
      if (isPair) {
        this._addCaptionRegion(container, frames[0], cap, orientation, 0);
        this._addCaptionRegion(container, frames[1], cap, orientation, 1);
      } else {
        this._addCaptionRegion(container, frames[0], cap, null, 0);
      }
    }
    this._layoutCaptionRegions(container);
    this._fitCaptions();
    this._scheduleCaptionClock();

    // Fade the new caption in alongside the image cross-fade. On the very
    // first frame (fade=false) just show it immediately.
    if (fade && container.firstChild) {
      container.style.opacity = "0";
      // Two RAFs so the browser registers the 0 before transitioning to 1.
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          container.style.opacity = "1";
        });
      });
    } else {
      container.style.opacity = "1";
    }
  }

  _captionConfigs() {
    return this._config?.captions ?? (this._config?.caption ? [this._config.caption] : []);
  }

  _scheduleCaptionClock() {
    clearTimeout(this._captionClockTimer);
    this._captionClockTimer = null;
    const clocks = this._captionConfigs().filter((cap) =>
      cap.show.some((field) => field === "current_date" || field === "current_time"));
    if (!this.isConnected || !this._captionData || !clocks.length) return;
    const interval = clocks.some((cap) => cap.show.includes("current_time") && cap.time_seconds) ? 1000 : 60000;
    this._captionClockTimer = setTimeout(() => {
      this._captionClockTimer = null;
      if (this.isConnected && this._captionData) this._renderCaptions(this._captionData, false);
    }, interval - (Date.now() % interval) + 20);
  }

  _fitCaptions() {
    if (!this._captionConfigs().length || !this.shadowRoot) return;
    for (const stack of this.shadowRoot.querySelectorAll(".cap-stack")) {
      const boxes = [...stack.querySelectorAll(".cap-box")];
      for (const box of boxes) box.style.fontSize = box._captionFontSize;
      if (!stack.clientWidth || !stack.clientHeight) continue;
      const sizes = boxes.map((box) => Number.parseFloat(getComputedStyle(box).fontSize));
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const scale = Math.min(stack.clientWidth / stack.scrollWidth, stack.clientHeight / stack.scrollHeight);
        if (scale >= 1) break;
        boxes.forEach((box, index) => {
          sizes[index] = Math.max(1, sizes[index] * scale * 0.98);
          box.style.fontSize = `${sizes[index]}px`;
        });
      }
    }
  }

  _layoutCaptionRegions(container) {
    const placements = [...container.children].map((region) => {
      const width = region._captionOrientation === "horizontal" ? 0.5 : 1;
      const height = region._captionOrientation === "vertical" ? 0.5 : 1;
      const left = width === 0.5 ? region._captionHalf * width : 0;
      const top = height === 0.5 ? region._captionHalf * height : 0;
      return { region, left, right: left + width, top, bottom: top + height,
        anchorX: left + region._captionColumn * width / 2,
        anchorY: top + region._captionRow * height / 2 };
    });
    const space = (start, end, anchor, neighbors, centered) => {
      let lower = start;
      let upper = end;
      for (const other of neighbors) {
        if (other < anchor) lower = Math.max(lower, (other + anchor) / 2);
        if (other > anchor) upper = Math.min(upper, (other + anchor) / 2);
      }
      if (centered) {
        const radius = Math.min(anchor - lower, upper - anchor);
        return [anchor - radius, anchor + radius];
      }
      return [lower, upper];
    };
    for (const current of placements) {
      const neighbors = placements.filter((other) => other.left < current.right && other.right > current.left &&
        other.top < current.bottom && other.bottom > current.top);
      const [left, right] = space(current.left, current.right, current.anchorX,
        neighbors.filter((other) => other.anchorY === current.anchorY).map((other) => other.anchorX),
        current.region._captionColumn === 1);
      const [top, bottom] = space(current.top, current.bottom, current.anchorY,
        neighbors.map((other) => other.anchorY), current.region._captionRow === 1);
      current.region.style.left = `${100 * left}%`;
      current.region.style.right = `${100 * (1 - right)}%`;
      current.region.style.top = `${100 * top}%`;
      current.region.style.bottom = `${100 * (1 - bottom)}%`;
    }
  }

  /** Per-image caption metadata. Prefers the integration's structured
   * ``caption_frames`` (one entry per image, two for a pair); falls back to
   * the flat attributes for older integration versions. */
  _buildCaptionFrames(data) {
    const cf = data.caption_frames;
    if (Array.isArray(cf) && cf.length) return cf;
    let captured = data.captured_at_primary;
    if (captured == null) {
      captured = Array.isArray(data.captured_at)
        ? data.captured_at[0]
        : data.captured_at;
    }
    return [
      {
        captured_at: captured ?? null,
        location: data.location ?? null,
        latitude: data.latitude ?? null,
        longitude: data.longitude ?? null,
        description: data.description ?? null,
        ...Object.fromEntries(CAMERA_CAPTION_FIELDS.map((field) => [field, data[field] ?? null])),
      },
    ];
  }

  _captionLines(frame, cap, now = new Date()) {
    const lines = [];
    for (const field of cap.show) {
      if (field === "date") {
        const txt = this._formatDate(frame.captured_at, cap.date_format);
        if (txt) lines.push(txt);
      } else if (field === "current_date") {
        lines.push(this._formatDate(now.toISOString(), cap.current_date_format ?? cap.date_format, this._hass?.config?.time_zone));
      } else if (field === "current_time") {
        lines.push(this._formatCurrentTime(now, cap));
      } else if (field === "weather") {
        const weather = this._weatherCaption(cap);
        if (weather) lines.push(weather);
      } else if (field === "location") {
        if (frame.location) lines.push(String(frame.location));
      } else if (field === "description") {
        if (frame.description) lines.push(String(frame.description));
      } else if (field === "filename" || field === "path") {
        if (typeof frame[field] === "string" && frame[field].trim()) lines.push(frame[field].trim());
      } else if (field === "video_duration") {
        const length = formatClipLength(frame.video_duration);
        if (length) lines.push(length);
      } else if (field === "video_version") {
        const version = this._videoVersionCaption(frame);
        if (version) lines.push(version);
      } else if (field === "camera") {
        const make = typeof frame.camera_make === "string" ? frame.camera_make.trim() : "";
        const model = typeof frame.camera_model === "string" ? frame.camera_model.trim() : "";
        const camera = make && model.toLowerCase().startsWith(make.toLowerCase())
          ? model : [make, model].filter(Boolean).join(" ");
        if (camera) lines.push(camera);
      } else if (field === "camera_make" || field === "camera_model") {
        if (typeof frame[field] === "string" && frame[field].trim()) lines.push(frame[field].trim());
      } else if (CAMERA_CAPTION_FIELDS.includes(field)) {
        const value = frame[field];
        if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) continue;
        const formatted = new Intl.NumberFormat(this._locale(), { maximumFractionDigits: 3 }).format(value);
        if (field === "focal_length_mm") lines.push(`${formatted} mm`);
        else if (field === "aperture_f_number") lines.push(`f/${formatted}`);
        else if (field === "iso") lines.push(`ISO ${Math.round(value)}`);
        else if (field === "exposure_time_seconds") {
          lines.push(value <= 0.5 ? `1/${Math.round(1 / value)} s` : `${formatted} s`);
        }
      }
    }
    return lines;
  }

  /** ``Transcoded video · 3.1 Mbps`` for a video slide; empty for photos. */
  _videoVersionCaption(frame) {
    const version = { original: "Original video", transcoded: "Transcoded video" }[frame.video_version];
    const bitrate = frame.video_bitrate_mbps;
    const rate = typeof bitrate === "number" && Number.isFinite(bitrate) && bitrate > 0
      ? `${new Intl.NumberFormat(this._locale(), { maximumFractionDigits: 1 }).format(bitrate)} Mbps`
      : "";
    if (!version && !rate) return "";
    return [version || "Video", rate].filter(Boolean).join(" · ");
  }

  _weatherCaption(cap) {
    if (typeof cap.weather_entity !== "string" || !/^(weather|sensor)\.[a-z0-9_]+$/.test(cap.weather_entity)) return "";
    const entity = this._hass?.states?.[cap.weather_entity];
    if (!entity || !entity.state || ["unknown", "unavailable"].includes(entity.state)) return "";
    const attributes = entity.attributes || {};
    const isWeather = cap.weather_entity.startsWith("weather.");
    const state = this._hass.formatEntityState
      ? this._hass.formatEntityState(entity)
      : isWeather ? humanizeOption(entity.state.replace("partlycloudy", "partly cloudy"))
        : [entity.state, attributes.unit_of_measurement].filter(Boolean).join(" ");
    if (!isWeather) return state;
    let temperature = "";
    if (typeof attributes.temperature === "number" && Number.isFinite(attributes.temperature)) {
      temperature = this._hass.formatEntityAttributeValue
        ? this._hass.formatEntityAttributeValue(entity, "temperature")
        : [new Intl.NumberFormat(this._locale(), { maximumFractionDigits: 1 }).format(attributes.temperature),
          attributes.temperature_unit || this._hass.config?.unit_system?.temperature].filter(Boolean).join(" ");
    }
    if (cap.weather_display === "temperature") return temperature;
    if (cap.weather_display === "icon_temperature") {
      const night = entity.state === "partlycloudy" && this._hass.states["sun.sun"]?.state === "below_horizon";
      if (cap.weather_icons === "home_assistant") {
        const svg = homeAssistantWeatherSvg(entity.state, night);
        if (svg) return { svg, label: state, text: temperature };
      } else {
        const icons = WEATHER_ICON_SETS[cap.weather_icons] || WEATHER_ICON_SETS.yr;
        const icon = icons[night ? "partlycloudy-night" : entity.state];
        if (icon) return { icon, label: state, text: temperature };
      }
    }
    return temperature ? `${state}, ${temperature}` : state;
  }

  /** Build one positioned caption block. ``orientation`` is ``null`` for a
   * full-frame caption, or ``horizontal`` / ``vertical`` to anchor the block
   * inside the left/right or top/bottom half of a pair (``half`` 0 or 1). */
  _addCaptionRegion(container, frame, cap, orientation, half) {
    const lines = this._captionLines(frame, cap);
    if (lines.length === 0) return;

    // Anchor within the region from the 3x3 position grid.
    const pos = cap.position;
    const parts = pos === "center" ? ["center", "center"] : pos.split("-");
    const v = parts[0];
    const h = parts[1] || "center";
    const justify = { left: "flex-start", center: "center", right: "flex-end" };
    const align = { top: "flex-start", center: "center", bottom: "flex-end" };
    const row = { top: 0, center: 1, bottom: 2 }[v];
    const column = { left: 0, center: 1, right: 2 }[h];
    const anchorX = orientation === "horizontal" ? half / 2 + column / 4 : column / 2;
    const anchorY = orientation === "vertical" ? half / 2 + row / 4 : row / 2;
    let region = [...container.children].find((element) => element._captionAnchorX === anchorX &&
      element._captionAnchorY === anchorY && (!orientation || !element._captionOrientation ||
        (element._captionOrientation === orientation && element._captionHalf === half)));
    if (!region) {
      region = document.createElement("div");
      region.className = "cap-region";
      region._captionPosition = pos;
      region._captionOrientation = orientation;
      region._captionHalf = half;
      region._captionRow = row;
      region._captionColumn = column;
      region._captionAnchorX = anchorX;
      region._captionAnchorY = anchorY;
      region.style.justifyContent = justify[h] || "flex-start";
      region.style.alignItems = align[v] || "flex-end";
      const stack = document.createElement("div");
      stack.className = "cap-stack";
      stack.style.alignItems = justify[h] || "flex-start";
      region._captionStack = stack;
      region.appendChild(stack);
      container.appendChild(region);
    }
    if (!region._captionOrientation && orientation) {
      region._captionOrientation = orientation;
      region._captionHalf = half;
      region._captionRow = row;
      region._captionColumn = column;
      region.style.justifyContent = justify[h];
      region.style.alignItems = align[v];
      region._captionStack.style.alignItems = justify[h];
    }

    const box = document.createElement("div");
    box.className = "cap-box";
    if (cap.shadow) box.classList.add("cap-shadow");
    box.style.color = cap.color;
    box.style.fontSize = cap.font_size;
    box._captionFontSize = cap.font_size;
    box.style.fontWeight = CAPTION_WEIGHT_MAP[cap.font_weight] || 500;
    box.style.textAlign = h === "center" ? "center" : h;

    for (const line of lines) {
      const el = document.createElement("div");
      el.className = "cap-line";
      if (typeof line === "string") {
        el.textContent = line;
      } else {
        const icon = document.createElement(line.svg ? "span" : "img");
        icon.className = "cap-icon";
        if (line.svg) {
          icon.innerHTML = line.svg;
        } else {
          icon.alt = line.label;
          icon.src = `${WEATHER_ICON_PATH}/${line.icon}.svg?v=${VERSION}`;
        }
        el.append(icon, line.text);
      }
      box.appendChild(el);
    }
    region._captionStack.appendChild(box);
  }

  _locale() {
    return (
      (this._hass && this._hass.locale && this._hass.locale.language) ||
      (typeof navigator !== "undefined" && navigator.language) ||
      "en"
    );
  }

  _formatCurrentTime(now, cap) {
    const format = cap.time_format === "auto" || !cap.time_format
      ? this._hass?.locale?.time_format : cap.time_format;
    const options = { hour: "numeric", minute: "2-digit", timeZone: this._hass?.config?.time_zone };
    if (["12h", "am_pm"].includes(format)) options.hourCycle = "h12";
    if (["24h", "24"].includes(format)) { options.hourCycle = "h23"; options.hour = "2-digit"; }
    if (cap.time_seconds) options.second = "2-digit";
    const locale = format === "system" && typeof navigator !== "undefined" ? navigator.language : this._locale();
    try {
      return new Intl.DateTimeFormat(locale, options).format(now);
    } catch (_) {
      delete options.timeZone;
      return new Intl.DateTimeFormat(undefined, options).format(now);
    }
  }

  _formatDate(iso, fmt, timeZone) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const locale = this._locale();
    if (fmt === "relative") return this._relativeTime(d);
    const preset = DATE_FORMAT_PRESETS[fmt];
    if (preset) {
      try {
        return new Intl.DateTimeFormat(locale, { ...preset, timeZone }).format(d);
      } catch (_) {
        return d.toLocaleDateString();
      }
    }
    // Anything else is treated as a custom token string.
    return this._formatTokens(d, String(fmt), timeZone);
  }

  _relativeTime(d) {
    const diff = d.getTime() - Date.now(); // negative => past
    const abs = Math.abs(diff);
    const sec = 1000;
    const min = 60 * sec;
    const hour = 60 * min;
    const day = 24 * hour;
    const week = 7 * day;
    const month = 30 * day;
    const year = 365 * day;
    let unit = "second";
    let val = diff / sec;
    if (abs >= year) {
      unit = "year";
      val = diff / year;
    } else if (abs >= month) {
      unit = "month";
      val = diff / month;
    } else if (abs >= week) {
      unit = "week";
      val = diff / week;
    } else if (abs >= day) {
      unit = "day";
      val = diff / day;
    } else if (abs >= hour) {
      unit = "hour";
      val = diff / hour;
    } else if (abs >= min) {
      unit = "minute";
      val = diff / min;
    }
    try {
      const rtf = new Intl.RelativeTimeFormat(this._locale(), {
        numeric: "auto",
      });
      return rtf.format(Math.round(val), unit);
    } catch (_) {
      return this._formatDate(d.toISOString(), "medium");
    }
  }

  _formatTokens(d, fmt, timeZone) {
    const locale = this._locale();
    const pad = (n) => String(n).padStart(2, "0");
    const part = (opts) => {
      try {
        return new Intl.DateTimeFormat(locale, { ...opts, timeZone }).format(d);
      } catch (_) {
        return "";
      }
    };
    let calendar = { year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate(), hour: d.getHours(), minute: d.getMinutes() };
    if (timeZone) {
      try {
        const parts = new Intl.DateTimeFormat("en-US-u-nu-latn", {
          year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric",
          hourCycle: "h23", timeZone,
        }).formatToParts(d);
        calendar = Object.fromEntries(parts.filter((value) => value.type !== "literal")
          .map((value) => [value.type, Number(value.value)]));
      } catch (_) {}
    }
    const map = {
      YYYY: calendar.year,
      YY: pad(calendar.year % 100),
      MMMM: part({ month: "long" }),
      MMM: part({ month: "short" }),
      MM: pad(calendar.month),
      M: calendar.month,
      DD: pad(calendar.day),
      D: calendar.day,
      dddd: part({ weekday: "long" }),
      ddd: part({ weekday: "short" }),
      HH: pad(calendar.hour),
      mm: pad(calendar.minute),
      // Lets relative time be mixed into a format string, e.g. "DD MMMM YYYY - REL".
      REL: this._relativeTime(d),
    };
    return fmt.replace(
      /REL|YYYY|YY|MMMM|MMM|MM|M|DD|D|dddd|ddd|HH|mm/g,
      (t) => map[t],
    );
  }

  _setPlaceholder(text) {
    const root = this.shadowRoot;
    let placeholder = root.getElementById("placeholder");
    if (!placeholder) {
      placeholder = document.createElement("div");
      placeholder.id = "placeholder";
      placeholder.className = "placeholder";
      root.getElementById("stage").appendChild(placeholder);
    }
    placeholder.textContent = text;
  }

  _fireMoreInfo() {
    // Freeze the visible slide on the card while the user is in the
    // more-info dialog. Without this, the slideshow keeps marching
    // forward behind the modal and the user perceives the card and
    // dialog as showing different photos.
    const pauseSec = this._config.tap_pause_seconds;
    if (pauseSec > 0) {
      this._holdSwapsUntil = Date.now() + pauseSec * 1000;
    }
    const event = new Event("hass-more-info", {
      bubbles: true,
      composed: true,
    });
    event.detail = { entityId: this._config.entity };
    this.dispatchEvent(event);
  }
  };
}

/**
 * Visual editor.
 *
 * Mirrors the look-and-feel of ha-shopping-list-card: native HA form
 * controls (ha-entity-picker, ha-textfield, ha-select, ha-switch)
 * grouped inside ha-expansion-panel sections so the form scales without
 * becoming a wall.
 */
const TRANSITION_OPTIONS = [
  { value: "random", label: "Random (different per slide)" },
  { value: "none", label: "None (instant swap)" },
  { value: "fade", label: "Fade" },
  { value: "slide-left", label: "Slide left" },
  { value: "slide-right", label: "Slide right" },
  { value: "slide-up", label: "Slide up" },
  { value: "slide-down", label: "Slide down" },
  { value: "wipe-left", label: "Wipe left" },
  { value: "wipe-right", label: "Wipe right" },
  { value: "zoom", label: "Zoom" },
];

const FIT_OPTIONS = [
  { value: "auto", label: "Auto (inherit camera fill_mode)" },
  { value: "cover", label: "Cover" },
  { value: "contain", label: "Contain" },
];

const EASING_OPTIONS = [
  { value: "ease-in-out", label: "Ease in-out (smooth)" },
  { value: "ease", label: "Ease" },
  { value: "ease-in", label: "Ease in" },
  { value: "ease-out", label: "Ease out" },
  { value: "linear", label: "Linear" },
  { value: "cubic-bezier(0.4, 0, 0.2, 1)", label: "Material standard" },
  { value: "cubic-bezier(0.0, 0.0, 0.2, 1)", label: "Material decelerate" },
  { value: "cubic-bezier(0.4, 0.0, 1, 1)", label: "Material accelerate" },
];

const TAP_OPTIONS = [
  { value: "none", label: "None" },
  { value: "more-info", label: "Open more-info" },
];

const CAPTION_SHOW_OPTIONS = [
  { value: "date", label: "Photo date" },
  { value: "location", label: "Location" },
  { value: "description", label: "Description" },
  { value: "camera", label: "Camera (make and model)" },
  { value: "camera_make", label: "Camera make" },
  { value: "camera_model", label: "Camera model" },
  { value: "focal_length_mm", label: "Focal length" },
  { value: "aperture_f_number", label: "Aperture" },
  { value: "iso", label: "ISO" },
  { value: "exposure_time_seconds", label: "Exposure time" },
  { value: "filename", label: "File name" },
  { value: "path", label: "File path" },
  { value: "video_duration", label: "Video length" },
  { value: "video_version", label: "Video version (original or transcoded)" },
  { value: "current_date", label: "Today's date" },
  { value: "current_time", label: "Current time" },
  { value: "weather", label: "Weather" },
];

const CAPTION_POSITION_OPTIONS = [
  { value: "top-left", label: "Top left" },
  { value: "top-center", label: "Top center" },
  { value: "top-right", label: "Top right" },
  { value: "center-left", label: "Center left" },
  { value: "center", label: "Center" },
  { value: "center-right", label: "Center right" },
  { value: "bottom-left", label: "Bottom left" },
  { value: "bottom-center", label: "Bottom center" },
  { value: "bottom-right", label: "Bottom right" },
];

const CAPTION_WEIGHT_OPTIONS = [
  { value: "light", label: "Light" },
  { value: "normal", label: "Normal" },
  { value: "medium", label: "Medium" },
  { value: "semibold", label: "Semi-bold" },
  { value: "bold", label: "Bold" },
];

const WEATHER_DISPLAY_OPTIONS = [
  { value: "condition_temperature", label: "Condition and temperature" },
  { value: "icon_temperature", label: "Icon and temperature" },
  { value: "temperature", label: "Temperature only" },
];

const WEATHER_ICON_OPTIONS = [
  { value: "yr", label: "Yr" },
  { value: "meteocons", label: "Meteocons" },
  { value: "home_assistant", label: "Home Assistant" },
];

const CAPTION_DATE_FORMAT_OPTIONS = [
  { value: "medium", label: "Medium (Aug 16, 2014)" },
  { value: "full", label: "Full (August 16, 2014)" },
  { value: "month_year", label: "Month & year (August 2014)" },
  { value: "year", label: "Year (2014)" },
  { value: "numeric", label: "Numeric (8/16/2014)" },
  { value: "weekday", label: "Weekday (Saturday, August 16, 2014)" },
  { value: "relative", label: "Relative (3 years ago)" },
  {
    value: "D MMMM YYYY - REL",
    label: "Date + relative (16 August 2014 - 3 years ago)",
  },
];

const DEFAULTS = {
  photo_controls: "off",
  transition: "random",
  duration: 600,
  easing: "ease-in-out",
  aspect_ratio: "16/9",
  fit: "auto",
  background: "",
  tap_action: "none",
  tap_pause_seconds: 8,
};

const CAPTION_DEFAULTS = {
  show: ["date", "location"],
  position: "bottom-left",
  per_image: true,
  date_format: "medium",
  time_format: "auto",
  time_seconds: false,
  weather_display: "condition_temperature",
  weather_icons: "yr",
  color: "#ffffff",
  font_size: "14px",
  font_weight: "medium",
  shadow: true,
};

// Live integration settings the editor surfaces directly. Each maps to a
// sibling entity on the same device as the camera. We discover those
// siblings by their unique_id suffix (stable across renames), then read
// their current state for the form and write changes back through a
// service call. Buttons are handled separately (see LIVE_ACTIONS).
const LIVE_FIELDS = [
  "paused",
  "date_filter",
  "custom_lookback_days",
  "shuffle_age_bias",
  "missing_date_mode",
  "portrait_mode",
  "order_mode",
  "slide_interval",
  "pair_divider_px",
  "pair_divider_color",
  "pair_min_gap_percent",
];

const LIVE_SUFFIX = {
  paused: "_paused",
  date_filter: "_date_filter",
  custom_lookback_days: "_custom_lookback_days",
  shuffle_age_bias: "_shuffle_age_bias",
  missing_date_mode: "_missing_date_mode",
  portrait_mode: "_portrait_mode",
  order_mode: "_order_mode",
  slide_interval: "_interval",
  pair_divider_px: "_pair_divider_px",
  pair_divider_color: "_pair_divider_color",
  pair_min_gap_percent: "_pair_min_gap_percent",
  previous_button: "_previous_button",
  next_button: "_next_button",
  refresh_button: "_refresh_button",
};

const LIVE_LABELS = {
  live_paused: "Pause slideshow",
  live_date_filter: "Date filter",
  live_custom_lookback_days: "Custom lookback (days)",
  live_shuffle_age_bias: "Shuffle age bias (older - / newer +)",
  live_missing_date_mode: "Missing capture date",
  live_portrait_mode: "Orientation mismatch mode",
  live_order_mode: "Order mode",
  live_slide_interval: "Slide interval (seconds)",
  live_pair_divider_px: "Pair divider size (px)",
  live_pair_divider_color: "Pair divider color",
  live_pair_min_gap_percent: "Pair minimum gap (% of album)",
};

function humanizeOption(value) {
  return String(value)
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function createAlbumSlideshowCardEditorClass(Base) {
  return class AlbumSlideshowCardEditor extends Base {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._rendered = false;
    this._lastEntityCount = -1;
    // Live integration settings discovered from the camera's device.
    this._registryCache = null; // entity registry list, cached per editor
    this._siblings = null; // { field: entity_id } on the camera's device
    this._liveData = {}; // mirror of live_<field> values from entity states
    this._lastLiveSig = ""; // signature of surfaced entity states
  }

  setConfig(config) {
    this._config = { ...config };
    if (this._rendered) this._update();
  }

  set hass(hass) {
    const prev = this._hass;
    this._hass = hass;
    if (!this._rendered) {
      this._render();
      return;
    }
    // Forward hass to the form so selectors that need it (entity picker)
    // see entity state updates.
    const form = this.shadowRoot.querySelector("ha-form");
    if (form) form.hass = hass;
    this._updatePhotoControls();
    // Re-run a full update when the camera set changes (warning box) or
    // when any surfaced integration entity changed state, so the live
    // controls stay in sync with the integration.
    if (
      !prev ||
      this._countSlideshowCameras() !== this._lastEntityCount ||
      this._liveSignature() !== this._lastLiveSig
    ) {
      this._update();
    }
  }

  _countSlideshowCameras() {
    if (!this._hass) return 0;
    let n = 0;
    for (const id of Object.keys(this._hass.states)) {
      if (isAlbumSlideshowCamera(this._hass.states[id])) n++;
    }
    return n;
  }

  /** Resolve the integration entities that live on the same device as the
   * selected camera. We match on unique_id suffix rather than entity_id,
   * because entity_id is derived from the (renameable) friendly name while
   * unique_id is stable. Requires one websocket call to the entity
   * registry, cached for the lifetime of the editor. */
  async _loadSiblings() {
    const camId = this._config && this._config.entity;
    this._siblings = null;
    if (!this._hass || !camId) return;
    const cam = this._hass.entities && this._hass.entities[camId];
    const deviceId = cam && cam.device_id;
    if (!deviceId) return;
    if (!this._registryCache) {
      try {
        this._registryCache = await this._hass.callWS({
          type: "config/entity_registry/list",
        });
      } catch (_) {
        return;
      }
    }
    const onDevice = this._registryCache.filter(
      (e) => e.device_id === deviceId,
    );
    const find = (suffix) => {
      const hit = onDevice.find(
        (e) => typeof e.unique_id === "string" && e.unique_id.endsWith(suffix),
      );
      return hit ? hit.entity_id : null;
    };
    const s = {};
    for (const key of Object.keys(LIVE_SUFFIX)) {
      s[key] = find(LIVE_SUFFIX[key]);
    }
    this._siblings = s;
  }

  _hasLiveControls() {
    if (!this._siblings) return false;
    return LIVE_FIELDS.some((f) => this._siblings[f]);
  }

  _hasActions() {
    return !!(
      this._siblings &&
      (this._siblings.previous_button ||
        this._siblings.next_button ||
        this._siblings.refresh_button)
    );
  }

  /** Stable signature of the surfaced entity states, so a hass update only
   * triggers a refresh when something we display actually changed. */
  _liveSignature() {
    if (!this._siblings || !this._hass) return "";
    const parts = [];
    for (const f of LIVE_FIELDS) {
      const id = this._siblings[f];
      if (!id) continue;
      const st = this._hass.states[id];
      parts.push(`${f}=${st ? st.state : "?"}`);
    }
    return parts.join("|");
  }

  _liveSelectOptions(entityId) {
    const st = this._hass && this._hass.states[entityId];
    const options = (st && st.attributes && st.attributes.options) || [];
    return options.map((o) => ({ value: o, label: humanizeOption(o) }));
  }

  _liveNumberConfig(entityId, fallback) {
    const st = this._hass && this._hass.states[entityId];
    const a = (st && st.attributes) || {};
    return {
      min: a.min != null ? a.min : fallback.min,
      max: a.max != null ? a.max : fallback.max,
      step: a.step != null ? a.step : fallback.step,
      mode: fallback.mode || "box",
      unit_of_measurement: fallback.unit,
    };
  }

  /** Schema for the live "Slideshow settings" section. Only includes
   * fields whose backing entity was found on the device. */
  _liveSchema() {
    const s = this._siblings || {};
    const items = [];
    if (s.paused) {
      items.push({ name: "live_paused", selector: { boolean: {} } });
    }
    for (const [field, id] of [
      ["date_filter", s.date_filter],
      ["missing_date_mode", s.missing_date_mode],
      ["portrait_mode", s.portrait_mode],
      ["order_mode", s.order_mode],
    ]) {
      if (id) {
        items.push({
          name: `live_${field}`,
          selector: {
            select: { mode: "dropdown", options: this._liveSelectOptions(id) },
          },
        });
      }
    }
    if (s.custom_lookback_days && this._hass?.states[s.date_filter]?.state === "custom_days") {
      items.push({
        name: "live_custom_lookback_days",
        selector: { number: this._liveNumberConfig(s.custom_lookback_days, {
          min: 1, max: 36500, step: 1, unit: "d",
        }) },
      });
    }
    if (s.shuffle_age_bias && this._hass?.states[s.order_mode]?.state === "random") {
      items.push({
        name: "live_shuffle_age_bias",
        selector: { number: this._liveNumberConfig(s.shuffle_age_bias, {
          min: -100, max: 100, step: 1, mode: "slider",
        }) },
      });
    }
    if (s.slide_interval) {
      items.push({
        name: "live_slide_interval",
        selector: {
          number: this._liveNumberConfig(s.slide_interval, {
            min: 3,
            max: 3600,
            step: 1,
            unit: "s",
          }),
        },
      });
    }
    if (s.pair_divider_px) {
      items.push({
        name: "live_pair_divider_px",
        selector: {
          number: this._liveNumberConfig(s.pair_divider_px, {
            min: 0,
            max: 64,
            step: 1,
            unit: "px",
          }),
        },
      });
    }
    if (s.pair_divider_color) {
      items.push({ name: "live_pair_divider_color", selector: { text: {} } });
    }
    if (s.pair_min_gap_percent) {
      items.push({
        name: "live_pair_min_gap_percent",
        selector: {
          number: this._liveNumberConfig(s.pair_min_gap_percent, {
            min: 0,
            max: 50,
            step: 1,
            unit: "%",
          }),
        },
      });
    }
    return items;
  }

  /** ha-form schema. Card options are grouped into collapsible
   * ``expandable`` sections; a final section surfaces the integration's
   * own settings (date filter, orientation, pairing, ...) when the
   * backing entities are available. The whole form is delegated to
   * ``ha-form`` so each selector control lazy-loads itself. */
  _schema() {
    const schema = [
      {
        name: "entity",
        required: true,
        selector: {
          entity: {
            // Filter array form is what current HA expects. ``integration``
            // restricts to entities backed by the album_slideshow domain;
            // ``domain`` is a belt-and-braces fallback for older HA cores
            // that ignore ``integration``.
            filter: [{ integration: "album_slideshow", domain: "camera" }],
          },
        },
      },
      {
        type: "expandable",
        title: "Appearance",
        icon: "mdi:palette",
        expanded: true,
        schema: [
          {
            name: "transition",
            selector: {
              select: { mode: "dropdown", options: TRANSITION_OPTIONS },
            },
          },
          {
            type: "grid",
            name: "",
            schema: [
              {
                name: "duration",
                selector: {
                  number: {
                    min: 50,
                    max: 5000,
                    step: 50,
                    mode: "box",
                    unit_of_measurement: "ms",
                  },
                },
              },
              {
                name: "easing",
                selector: {
                  select: { mode: "dropdown", options: EASING_OPTIONS },
                },
              },
            ],
          },
          { name: "aspect_ratio", selector: { text: {} } },
          {
            name: "fit",
            selector: { select: { mode: "dropdown", options: FIT_OPTIONS } },
          },
          { name: "background", selector: { text: {} } },
          { name: "videos", selector: { boolean: {} } },
          {
            name: "video_audio",
            selector: { select: { mode: "dropdown", options: VIDEO_AUDIO_OPTIONS } },
          },
        ],
      },
      {
        type: "expandable",
        title: "Interaction",
        icon: "mdi:gesture-tap",
        schema: [
          {
            name: "photo_controls",
            selector: { select: { mode: "dropdown", options: PHOTO_CONTROL_OPTIONS } },
          },
          {
            name: "tap_action",
            selector: { select: { mode: "dropdown", options: TAP_OPTIONS } },
          },
          {
            name: "tap_pause_seconds",
            selector: {
              number: {
                min: 0,
                max: 120,
                step: 1,
                mode: "box",
                unit_of_measurement: "s",
              },
            },
          },
        ],
      },
    ];

    if (this._hasLiveControls()) {
      schema.push({
        type: "expandable",
        title: "Slideshow settings",
        icon: "mdi:tune",
        schema: this._liveSchema(),
      });
    }

    return schema;
  }

  /** Map config + live entity state to the flat data shape ha-form wants. */
  _data() {
    const c = this._config || {};
    return {
      entity: c.entity || "",
      photo_controls: normalizePhotoControls(c.photo_controls),
      transition: c.transition || DEFAULTS.transition,
      duration: c.duration != null ? Number(c.duration) : DEFAULTS.duration,
      easing: c.easing || DEFAULTS.easing,
      aspect_ratio: c.aspect_ratio || DEFAULTS.aspect_ratio,
      fit: c.fit || DEFAULTS.fit,
      background: c.background || "",
      tap_action: c.tap_action || DEFAULTS.tap_action,
      videos: c.videos !== false,
      video_audio: normalizeVideoAudio(c.video_audio),
      tap_pause_seconds:
        c.tap_pause_seconds != null
          ? Number(c.tap_pause_seconds)
          : DEFAULTS.tap_pause_seconds,
      ...this._liveDataFromStates(),
    };
  }

  /** Flatten the nested ``caption`` config into the fields ha-form binds. */
  _captionData(cap = this._editorCaptions()[0]) {
    const enabled = !!cap && cap !== false && cap.enabled !== false;
    const c = cap && typeof cap === "object" ? cap : {};
    let show = c.show;
    if (typeof show === "string") show = show.split(/[,\s]+/).filter(Boolean);
    if (!Array.isArray(show)) show = CAPTION_DEFAULTS.show.slice();
    return {
      caption_enabled: enabled,
      caption_show: show,
      caption_position: c.position || CAPTION_DEFAULTS.position,
      caption_per_image: c.per_image !== false,
      caption_date_format: c.date_format || CAPTION_DEFAULTS.date_format,
      caption_current_date_format: c.current_date_format || c.date_format || CAPTION_DEFAULTS.date_format,
      caption_time_format: c.time_format || CAPTION_DEFAULTS.time_format,
      caption_time_seconds: c.time_seconds === true,
      caption_weather_entity: c.weather_entity || "",
      caption_weather_display: c.weather_display || CAPTION_DEFAULTS.weather_display,
      caption_weather_icons: c.weather_icons || CAPTION_DEFAULTS.weather_icons,
      caption_color: c.color || CAPTION_DEFAULTS.color,
      caption_font_size: c.font_size || CAPTION_DEFAULTS.font_size,
      caption_font_weight: c.font_weight || CAPTION_DEFAULTS.font_weight,
      caption_shadow: c.shadow !== false,
    };
  }

  _editorCaptions() {
    const source = this._config.captions ?? (this._config.caption ? [this._config.caption] : []);
    return Array.isArray(source) ? source.map((caption) => ({
      ...(caption && typeof caption === "object" ? caption : { enabled: caption !== false }),
    })) : [];
  }

  _captionSchema(caption = this._editorCaptions()[0]) {
    const data = this._captionData(caption);
    const schema = [
      { name: "caption_enabled", selector: { boolean: {} } },
      { name: "caption_position", selector: { select: { mode: "dropdown", options: CAPTION_POSITION_OPTIONS } } },
      { name: "caption_show", selector: { select: { multiple: true, reorder: true, mode: "dropdown", options: CAPTION_SHOW_OPTIONS } } },
    ];
    if (data.caption_show.includes("weather")) {
      schema.push({ name: "caption_weather_entity", required: true,
        selector: { entity: { filter: [{ domain: ["weather", "sensor"] }] } } });
      if (data.caption_weather_entity.startsWith("weather.")) {
        schema.push({ name: "caption_weather_display", selector: { select: {
          mode: "dropdown", options: WEATHER_DISPLAY_OPTIONS,
        } } });
        if (data.caption_weather_display === "icon_temperature") {
          schema.push({ name: "caption_weather_icons", selector: { select: {
            mode: "dropdown", options: WEATHER_ICON_OPTIONS,
          } } });
        }
      }
    }
    if (data.caption_show.includes("date")) {
      schema.push({ name: "caption_date_format", selector: { select: {
        mode: "dropdown", custom_value: true, options: CAPTION_DATE_FORMAT_OPTIONS,
      } } });
    }
    if (data.caption_show.includes("current_date")) {
      schema.push({ name: "caption_current_date_format", selector: { select: {
        mode: "dropdown", custom_value: true, options: CAPTION_DATE_FORMAT_OPTIONS,
      } } });
    }
    if (data.caption_show.includes("current_time")) {
      schema.push(
        { name: "caption_time_format", selector: { select: { mode: "dropdown", options: [
          { value: "auto", label: "Home Assistant preference" },
          { value: "12h", label: "12-hour" }, { value: "24h", label: "24-hour" },
        ] } } },
        { name: "caption_time_seconds", selector: { boolean: {} } },
      );
    }
    if (data.caption_show.some((field) => !LIVE_CAPTION_FIELDS.has(field))) {
      schema.push({ name: "caption_per_image", selector: { boolean: {} } });
    }
    schema.push(
      { type: "grid", name: "", schema: [
        { name: "caption_color", selector: { text: {} } },
        { name: "caption_font_size", selector: { text: {} } },
      ] },
      { name: "caption_font_weight", selector: { select: { mode: "dropdown", options: CAPTION_WEIGHT_OPTIONS } } },
      { name: "caption_shadow", selector: { boolean: {} } },
    );
    return schema;
  }

  _saveCaptions(captions) {
    const config = { ...this._config };
    const useList = Array.isArray(config.captions) || captions.length > 1;
    delete config.caption;
    delete config.captions;
    if (captions.length) {
      if (useList) config.captions = captions;
      else config.caption = captions[0];
    }
    this._config = config;
    this._renderCaptionEditors();
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config }, bubbles: true, composed: true,
    }));
  }

  _nextCaptionPosition(captions) {
    const used = new Set(captions.map((caption) => caption.position || CAPTION_DEFAULTS.position));
    return ["bottom-left", "top-left", "bottom-right", "top-right", "bottom-center", "top-center", "center-left", "center-right", "center"]
      .find((position) => !used.has(position)) || CAPTION_DEFAULTS.position;
  }

  _addCaption(copyIndex) {
    const captions = this._editorCaptions();
    const original = Number.isInteger(copyIndex) ? captions[copyIndex] : null;
    captions.push({
      ...(original || {}),
      show: [...this._captionData(original || {}).caption_show],
      position: this._nextCaptionPosition(captions),
    });
    this._openCaptionIndex = captions.length - 1;
    this._saveCaptions(captions);
  }

  _removeCaption(index) {
    const captions = this._editorCaptions();
    captions.splice(index, 1);
    this._openCaptionIndex = Math.min(index, captions.length - 1);
    this._saveCaptions(captions);
  }

  _captionChanged(index, event) {
    event.stopPropagation();
    const captions = this._editorCaptions();
    if (!captions[index]) return;
    const data = { ...this._captionData(captions[index]), ...event.detail?.value };
    let show = data.caption_show;
    if (!Array.isArray(show)) show = typeof show === "string" ? show.split(/[,\s]+/) : [];
    const caption = { show: [...new Set(show.filter((field) => CAPTION_FIELDS.includes(field)))] };
    if (data.caption_enabled === false) caption.enabled = false;
    for (const field of ["position", "date_format", "time_format", "weather_display", "weather_icons", "color", "font_size", "font_weight"]) {
      const value = String(data[`caption_${field}`] || CAPTION_DEFAULTS[field]).trim();
      if (value && value !== CAPTION_DEFAULTS[field]) caption[field] = value;
    }
    if (caption.show.includes("current_date") || captions[index].current_date_format != null) {
      const format = String(data.caption_current_date_format || CAPTION_DEFAULTS.date_format).trim();
      if (format !== (caption.date_format || CAPTION_DEFAULTS.date_format)) caption.current_date_format = format;
    }
    if (data.caption_per_image === false) caption.per_image = false;
    if (data.caption_shadow === false) caption.shadow = false;
    if (data.caption_time_seconds === true) caption.time_seconds = true;
    if (typeof data.caption_weather_entity === "string" && data.caption_weather_entity.trim()) {
      caption.weather_entity = data.caption_weather_entity.trim();
    }
    captions[index] = caption;
    this._saveCaptions(captions);
  }

  _captionButton(icon, label, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "caption-tool";
    button.title = label;
    button.setAttribute("aria-label", label);
    const symbol = document.createElement("ha-icon");
    symbol.setAttribute("icon", icon);
    button.append(symbol);
    button.addEventListener("click", action);
    return button;
  }

  _moveCaptionContent(index, event) {
    event.stopPropagation();
    const { oldIndex, newIndex } = event.detail || {};
    const show = [...this._captionData(this._editorCaptions()[index]).caption_show];
    if (!Number.isInteger(oldIndex) || !Number.isInteger(newIndex) || oldIndex === newIndex ||
      oldIndex < 0 || oldIndex >= show.length || newIndex < 0 || newIndex >= show.length) return;
    const [field] = show.splice(oldIndex, 1);
    show.splice(newIndex, 0, field);
    this._captionChanged(index, { stopPropagation() {}, detail: { value: { caption_show: show } } });
  }

  _createCaptionContentPicker(index) {
    const content = document.createElement("div");
    content.className = "caption-content-selector";
    const label = document.createElement("span");
    label.className = "caption-content-label";
    label.id = `caption-content-label-${index}`;
    label.textContent = "Content";
    const picker = document.createElement("ha-generic-picker");
    picker.label = "Content";
    picker.searchLabel = "Search content";
    picker.allowCustomValue = false;
    picker.value = "";
    picker.setAttribute("no-sort", "");
    const field = document.createElement("div");
    field.slot = "field";
    field.className = "caption-content-field";
    field.setAttribute("role", "group");
    field.setAttribute("aria-labelledby", label.id);
    const sortable = document.createElement("ha-sortable");
    sortable.setAttribute("no-style", "");
    sortable.setAttribute("handle-selector", "button.primary.action");
    sortable.setAttribute("filter", ".caption-content-add");
    sortable.addEventListener("item-moved", (event) => this._moveCaptionContent(index, event));
    const chips = document.createElement("ha-chip-set");
    const add = document.createElement("ha-assist-chip");
    add.className = "caption-content-add";
    add.label = "Add";
    const icon = document.createElement("ha-icon");
    icon.slot = "icon";
    icon.setAttribute("icon", "mdi:plus");
    add.append(icon);
    add.addEventListener("click", async (event) => {
      event.stopPropagation();
      await picker.updateComplete;
      if (!add.disabled) picker.open();
    });
    picker.addEventListener("value-changed", (event) => {
      event.stopPropagation();
      const selected = event.detail?.value;
      const show = this._captionData(this._editorCaptions()[index]).caption_show;
      if (!CAPTION_FIELDS.includes(selected) || show.includes(selected)) return;
      this._captionChanged(index, { stopPropagation() {}, detail: { value: { caption_show: [...show, selected] } } });
      picker.value = "";
    });
    chips.append(add);
    sortable.append(chips);
    field.append(sortable);
    picker.append(field);
    content.append(label, picker);
    return content;
  }

  _updateCaptionContentPicker(content, index, data, selector) {
    const picker = content.querySelector("ha-generic-picker");
    const chips = content.querySelector("ha-chip-set");
    const add = content.querySelector(".caption-content-add");
    const show = data.caption_show.filter((field) => CAPTION_FIELDS.includes(field));
    const items = selector.options.filter((option) => !show.includes(option.value))
      .map((option) => ({ id: option.value, primary: option.label, sorting_label: option.label }));
    picker.hass = this._hass;
    picker.getItems = () => items;
    add.disabled = items.length === 0;
    content.querySelector("ha-sortable").disabled = !selector.reorder;
    const signature = JSON.stringify(show);
    if (content._captionContentSignature === signature) return;
    content._captionContentSignature = signature;
    const selected = show.map((value) => {
      const label = selector.options.find((option) => option.value === value)?.label || value;
      const chip = document.createElement("ha-input-chip");
      chip.label = label;
      chip.title = label;
      chip.selected = true;
      chip.setAttribute("data-field", value);
      const icon = document.createElement("ha-icon");
      icon.slot = "icon";
      icon.setAttribute("icon", "mdi:drag-horizontal-variant");
      chip.append(icon, document.createTextNode(label));
      chip.addEventListener("click", (event) => event.stopPropagation());
      chip.addEventListener("remove", (event) => {
        event.stopPropagation();
        const remaining = this._captionData(this._editorCaptions()[index]).caption_show.filter((field) => field !== value);
        this._captionChanged(index, { stopPropagation() {}, detail: { value: { caption_show: remaining } } });
      });
      return chip;
    });
    chips.replaceChildren(...selected, add);
  }

  _renderCaptionEditors() {
    const list = this.shadowRoot?.querySelector(".caption-list");
    if (!list) return;
    const captions = this._editorCaptions();
    if (list.children.length !== captions.length) {
      list.replaceChildren();
      captions.forEach((_caption, index) => {
        const item = document.createElement("div");
        item.className = "caption-item";
        const details = document.createElement("details");
        details.open = index === this._openCaptionIndex || captions.length === 1;
        const summary = document.createElement("summary");
        const chevron = document.createElement("ha-icon");
        chevron.className = "caption-chevron";
        chevron.setAttribute("icon", "mdi:chevron-right");
        chevron.setAttribute("aria-hidden", "true");
        const heading = document.createElement("span");
        heading.className = "caption-summary";
        const title = document.createElement("span");
        title.className = "caption-title";
        const meta = document.createElement("span");
        meta.className = "caption-meta";
        heading.append(title, meta);
        summary.append(chevron, heading);
        const body = document.createElement("div");
        body.className = "caption-body";
        const controls = document.createElement("ha-form");
        controls.className = "caption-controls";
        const options = document.createElement("ha-form");
        options.className = "caption-options";
        for (const form of [controls, options]) {
          form.computeLabel = this._computeLabel;
          form.computeHelper = this._computeHelper;
          form.addEventListener("value-changed", (event) => this._captionChanged(index, event));
        }
        body.append(controls, this._createCaptionContentPicker(index), options);
        details.append(summary, body);
        const tools = document.createElement("div");
        tools.className = "caption-tools";
        tools.append(
          this._captionButton("mdi:content-copy", `Duplicate caption ${index + 1}`, () => this._addCaption(index)),
          this._captionButton("mdi:delete-outline", `Remove caption ${index + 1}`, () => this._removeCaption(index)),
        );
        item.append(details, tools);
        list.append(item);
      });
    }
    captions.forEach((caption, index) => {
      const item = list.children[index];
      const data = this._captionData(caption);
      const position = CAPTION_POSITION_OPTIONS.find((option) => option.value === data.caption_position)?.label || data.caption_position;
      const fields = data.caption_show.map((field) => CAPTION_SHOW_OPTIONS.find((option) => option.value === field)?.label)
        .filter(Boolean).join(", ") || "Empty caption";
      const title = `${position}${data.caption_enabled ? "" : " (off)"}`;
      item.querySelector(".caption-title").textContent = title;
      item.querySelector(".caption-title").title = title;
      item.querySelector(".caption-meta").textContent = fields;
      item.querySelector(".caption-meta").title = fields;
      item.querySelector("summary").setAttribute("aria-label", `Caption ${index + 1}: ${title}. ${fields}`);
      item.classList.toggle("caption-disabled", !data.caption_enabled);
      const schema = this._captionSchema(caption);
      const contentIndex = schema.findIndex((field) => field.name === "caption_show");
      for (const [className, fields] of [
        [".caption-controls", schema.slice(0, contentIndex)],
        [".caption-options", schema.slice(contentIndex + 1)],
      ]) {
        const form = item.querySelector(className);
        form.hass = this._hass;
        form.schema = fields;
        form.data = data;
      }
      this._updateCaptionContentPicker(item.querySelector(".caption-content-selector"), index, data,
        schema[contentIndex].selector.select);
    });
  }

  /** Read the current value of each surfaced integration entity. */
  _liveDataFromStates() {
    const s = this._siblings;
    const out = {};
    if (!s || !this._hass) return out;
    const st = (id) => (id ? this._hass.states[id] : null);
    if (s.paused) {
      const e = st(s.paused);
      out.live_paused = !!e && e.state === "on";
    }
    for (const f of ["date_filter", "missing_date_mode", "portrait_mode", "order_mode"]) {
      if (s[f]) {
        const e = st(s[f]);
        out[`live_${f}`] = e ? e.state : "";
      }
    }
    for (const f of ["slide_interval", "pair_divider_px", "pair_min_gap_percent", "custom_lookback_days", "shuffle_age_bias"]) {
      if (s[f]) {
        const e = st(s[f]);
        out[`live_${f}`] = e ? Number(e.state) : null;
      }
    }
    if (s.pair_divider_color) {
      const e = st(s.pair_divider_color);
      out.live_pair_divider_color = e ? e.state : "";
    }
    return out;
  }

  _computeLabel = (s) => {
    const labels = {
      entity: "Album Slideshow camera",
      transition: "Transition",
      duration: "Duration (ms)",
      easing: "Easing",
      aspect_ratio: "Aspect ratio",
      fit: "Fit",
      background: "Background (optional)",
      tap_action: "Tap action",
      tap_pause_seconds: "Tap pause (seconds)",
      videos: "Play videos",
      video_audio: "Video sound",
      photo_controls: "Photo controls",
      caption_enabled: "Show caption overlay",
      caption_show: "Show",
      caption_position: "Position",
      caption_per_image: "Per-image captions on pairs",
      caption_date_format: "Photo date format",
      caption_current_date_format: "Today's date format",
      caption_time_format: "Time format",
      caption_time_seconds: "Show seconds",
      caption_weather_entity: "Weather source",
      caption_weather_display: "Weather display",
      caption_weather_icons: "Weather icons",
      caption_color: "Text color",
      caption_font_size: "Font size",
      caption_font_weight: "Font weight",
      caption_shadow: "Text shadow",
      ...LIVE_LABELS,
    };
    return labels[s.name] || s.name;
  };

  _computeHelper = (s) => {
    const helpers = {
      background: "Leave blank to inherit the dashboard theme.",
      transition: "Random picks a different effect each slide.",
      videos: "Videos and Live Photo motion. Off shows a still frame instead. Must also be enabled in the integration.",
      video_audio: "Muted starts videos silent with a speaker button. On plays sound when the browser allows it, otherwise shows the button.",
      tap_pause_seconds:
        "How long the card freezes its slide after a tap. 0 disables it.",
      caption_date_format:
        "Pick a preset or type a custom format (YYYY, MMMM, MMM, MM, DD, D, REL for relative time).",
      caption_current_date_format:
        "Pick a preset or type a custom format (YYYY, MMMM, MMM, MM, DD, D, REL for relative time).",
      caption_show:
        "Available fields depend on the photo source. Missing values are omitted.",
      caption_per_image:
        "Use each photo's own metadata on paired slides.",
      caption_color: "CSS color, e.g. #ffffff or white.",
      caption_font_size: "CSS size, e.g. 14px, 1.1em.",
      live_paused:
        "These control the Album Slideshow integration directly and apply everywhere this album is shown, not only this card.",
      live_missing_date_mode:
        "What a date filter does with photos that have no capture date: use the upload date, keep them, or drop them.",
    };
    return helpers[s.name] || "";
  };

  _render() {
    if (!this.shadowRoot || !this._hass) return;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        .card-config {
          display: flex;
          flex-direction: column;
          gap: 12px;
          padding: 4px 0;
        }
        ha-form { display: block; }
        .info-box {
          background: var(--warning-color);
          color: var(--primary-background-color);
          padding: 10px 14px; border-radius: 8px;
          font-size: 13px; line-height: 1.5;
        }
        .info-box strong { display: block; margin-bottom: 2px; }
        .actions {
          border: 1px solid var(--divider-color, #e0e0e0);
          border-radius: 8px;
          padding: 8px 12px 12px;
        }
        .actions-title {
          font-size: 13px; font-weight: 500;
          color: var(--secondary-text-color); margin-bottom: 8px;
        }
        .actions-row { display: flex; gap: 8px; flex-wrap: wrap; }
        .act {
          appearance: none; border: none; border-radius: 6px;
          padding: 8px 14px; font-size: 14px; cursor: pointer;
          background: var(--primary-color); color: var(--text-primary-color, #fff);
        }
        .act:hover { opacity: 0.9; }
        .caption-editors { display: block; }
        .caption-content { padding: 4px 12px 12px; }
        .caption-list { display: flex; flex-direction: column; gap: 12px; }
        .caption-item { display: grid; min-width: 0; border: 1px solid var(--divider-color, #e0e0e0); border-radius: 8px; background: var(--card-background-color, #fff); }
        .caption-item details { grid-area: 1 / 1; min-width: 0; }
        .caption-item summary { display: flex; align-items: center; gap: 8px; box-sizing: border-box; padding: 12px 100px 12px 12px; min-height: 70px; border-radius: 7px; list-style: none; cursor: pointer; background: var(--secondary-background-color, #f5f5f5); }
        .caption-item summary::-webkit-details-marker { display: none; }
        .caption-item summary:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
        .caption-item details[open] > summary { border-radius: 7px 7px 0 0; border-bottom: 1px solid var(--divider-color, #e0e0e0); }
        .caption-chevron { flex: 0 0 18px; --mdc-icon-size: 18px; color: var(--secondary-text-color); }
        .caption-item details[open] .caption-chevron { transform: rotate(90deg); }
        .caption-summary { display: flex; flex-direction: column; flex: 1; min-width: 0; gap: 3px; }
        .caption-title { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 14px; font-weight: 500; line-height: 1.35; }
        .caption-meta { color: var(--secondary-text-color); font-size: 12px; line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .caption-disabled .caption-title { color: var(--secondary-text-color); }
        .caption-body { padding: 12px; display: flex; flex-direction: column; gap: 20px; }
        .caption-body ha-form { min-width: 0; }
        .caption-content-selector { min-width: 0; }
        .caption-content-label { display: block; margin-bottom: 8px; font-size: 14px; font-weight: 500; }
        .caption-content-selector ha-generic-picker { display: block; width: 100%; }
        .caption-content-field { position: relative; background: var(--mdc-text-field-fill-color, #f5f5f5); border-radius: 4px 4px 0 0; border-bottom: 1px solid var(--mdc-text-field-idle-line-color, #9e9e9e); }
        .caption-content-field:focus-within { border-bottom-color: var(--primary-color); box-shadow: 0 1px 0 var(--primary-color); }
        .caption-content-field ha-chip-set { padding: 10px 12px; min-height: 54px; box-sizing: border-box; }
        .caption-content-field ha-input-chip { max-width: 100%; }
        .caption-content-field ha-icon { --mdc-icon-size: 18px; }
        .caption-content-add { order: 1; }
        .caption-content-field .sortable-fallback { display: none; opacity: 0; }
        .caption-content-field .sortable-ghost { opacity: 0.4; }
        .caption-content-field .sortable-drag { cursor: grabbing; }
        .caption-tools { grid-area: 1 / 1; align-self: start; justify-self: end; z-index: 1; display: flex; gap: 2px; padding: 10px 8px; }
        .caption-tool { display: inline-flex; align-items: center; justify-content: center; box-sizing: border-box; flex-shrink: 0; width: 36px; height: 36px; padding: 6px; border: 0; border-radius: 4px; background: transparent; color: var(--secondary-text-color, var(--primary-text-color)); cursor: pointer; }
        .caption-tool:hover { background: var(--divider-color, #e0e0e0); color: var(--primary-text-color); }
        .caption-tool:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        .caption-add { width: 100%; min-height: 40px; gap: 8px; margin-top: 12px; border: 1px dashed var(--divider-color, #e0e0e0); border-radius: 6px; color: var(--primary-color); }
        .caption-tool ha-icon { --mdc-icon-size: 20px; }
      </style>
      <div class="card-config">
        <div class="info-slot"></div>
        <ha-form></ha-form>
        <ha-expansion-panel class="caption-editors" outlined>
          <ha-icon slot="leading-icon" icon="mdi:format-text"></ha-icon>
          <div slot="header" role="heading" aria-level="3">Captions</div>
          <div class="caption-content"><div class="caption-list"></div></div>
        </ha-expansion-panel>
        <div class="actions" hidden></div>
        <div class="photo-controls"></div>
      </div>
    `;
    const form = this.shadowRoot.querySelector("ha-form");
    form.computeLabel = this._computeLabel;
    form.computeHelper = this._computeHelper;
    form.addEventListener("value-changed", (ev) => this._valueChanged(ev));
    const addCaption = this._captionButton("mdi:plus", "Add caption", () => this._addCaption());
    addCaption.classList.add("caption-add");
    addCaption.append(document.createTextNode("Add caption"));
    this.shadowRoot.querySelector(".caption-content").append(addCaption);
    this._photoControls = new PhotoControls(
      this.shadowRoot.querySelector(".photo-controls"), () => this._hass,
    );
    this._rendered = true;
    this._update();
  }

  async _update() {
    if (!this._rendered) return;
    await this._loadSiblings();
    const form = this.shadowRoot.querySelector("ha-form");
    if (!form) return;
    this._liveData = this._liveDataFromStates();
    this._lastLiveSig = this._liveSignature();
    form.hass = this._hass;
    form.schema = this._schema();
    form.data = this._data();
    this._renderCaptionEditors();

    const count = this._countSlideshowCameras();
    this._lastEntityCount = count;
    const slot = this.shadowRoot.querySelector(".info-slot");
    if (count === 0) {
      slot.innerHTML = `
        <div class="info-box">
          <strong>No Album Slideshow cameras found.</strong>
          Add an Album Slideshow integration first; this card needs one of its camera entities.
        </div>
      `;
    } else {
      slot.innerHTML = "";
    }

    this._renderActions();
    this._updatePhotoControls();
  }

  _updatePhotoControls() {
    if (!this._photoControls) return;
    const attrs = this._hass?.states[this._config?.entity]?.attributes || {};
    this.shadowRoot.querySelector(".photo-controls").hidden = !attrs.entry_id;
    this._photoControls.update({
      entryId: attrs.entry_id,
      photoIds: attrs.displayed_photo_ids,
      orientation: attrs.pair_orientation,
      hiddenCount: attrs.hidden_photo_count,
      canUndo: attrs.undo_hide_available,
      canPrevious: attrs.previous_frames_cached > 0,
      canNext: attrs.media_count > 0,
      paused: attrs.paused,
    });
  }

  _renderActions() {
    const wrap = this.shadowRoot.querySelector(".actions");
    if (!wrap) return;
    const hasToolbarNavigation = !!this._hass?.states[this._config?.entity]?.attributes?.entry_id;
    if (!this._hasActions() || (hasToolbarNavigation && !this._siblings.refresh_button)) {
      wrap.hidden = true;
      wrap.innerHTML = "";
      return;
    }
    const s = this._siblings;
    wrap.hidden = false;
    wrap.innerHTML = `
      <div class="actions-title">Actions</div>
      <div class="actions-row">
        ${s.previous_button && !hasToolbarNavigation ? `<button class="act" data-act="previous">Previous slide</button>` : ""}
        ${s.next_button && !hasToolbarNavigation ? `<button class="act" data-act="next">Next slide</button>` : ""}
        ${s.refresh_button ? `<button class="act" data-act="refresh">Refresh album</button>` : ""}
      </div>
    `;
    const actionEntities = {
      previous: s.previous_button,
      next: s.next_button,
      refresh: s.refresh_button,
    };
    wrap.querySelectorAll("button.act").forEach((b) => {
      b.addEventListener("click", () => {
        const id = actionEntities[b.dataset.act];
        if (id && this._hass) {
          this._hass.callService("button", "press", { entity_id: id });
        }
      });
    });
  }

  /** Apply a live settings change by calling the appropriate service on
   * the backing integration entity. */
  _applyLive(field, value) {
    const s = this._siblings;
    const hass = this._hass;
    if (!s || !hass) return;
    const id = s[field];
    if (!id) return;
    if (field === "paused") {
      hass.callService("switch", value ? "turn_on" : "turn_off", {
        entity_id: id,
      });
    } else if (
      field === "date_filter" ||
      field === "missing_date_mode" ||
      field === "portrait_mode" ||
      field === "order_mode"
    ) {
      hass.callService("select", "select_option", {
        entity_id: id,
        option: value,
      });
    } else if (
      field === "slide_interval" ||
      field === "custom_lookback_days" ||
      field === "shuffle_age_bias" ||
      field === "pair_divider_px" ||
      field === "pair_min_gap_percent"
    ) {
      hass.callService("number", "set_value", {
        entity_id: id,
        value: Number(value),
      });
    } else if (field === "pair_divider_color") {
      hass.callService("text", "set_value", {
        entity_id: id,
        value: String(value),
      });
    }
  }

  _valueChanged(ev) {
    ev.stopPropagation();
    const data = ev?.detail?.value || {};

    // A changed live_* field maps to an integration entity, not card
    // config: route it to a service call and stop. Only one field changes
    // per event, so the first difference we find is the edit.
    if (this._siblings) {
      for (const field of LIVE_FIELDS) {
        const key = `live_${field}`;
        if (
          key in data &&
          this._siblings[field] &&
          data[key] !== this._liveData[key]
        ) {
          this._applyLive(field, data[key]);
          this._liveData = { ...this._liveData, [key]: data[key] };
          return;
        }
      }
    }

    const n = { type: "custom:album-slideshow-card" };

    const photoControls = normalizePhotoControls(data.photo_controls);
    if (photoControls === "always") n.photo_controls = true;
    if (photoControls === "on_demand") n.photo_controls = "on_demand";
    if (data.entity) n.entity = data.entity;

    const t = data.transition || DEFAULTS.transition;
    if (t !== DEFAULTS.transition) n.transition = t;

    const dur = Number(data.duration);
    if (!isNaN(dur) && dur !== DEFAULTS.duration) n.duration = dur;

    const easing = data.easing || DEFAULTS.easing;
    if (easing !== DEFAULTS.easing) n.easing = easing;

    const aspect = (data.aspect_ratio || "").trim();
    if (aspect && aspect !== DEFAULTS.aspect_ratio) n.aspect_ratio = aspect;

    const fit = data.fit || DEFAULTS.fit;
    if (fit !== DEFAULTS.fit) n.fit = fit;

    const bg = (data.background || "").trim();
    if (bg) n.background = bg;

    const ta = data.tap_action || DEFAULTS.tap_action;
    if (ta !== DEFAULTS.tap_action) n.tap_action = ta;

    if (data.videos === false) n.videos = false;
    const audio = normalizeVideoAudio(data.video_audio);
    if (audio !== "off") n.video_audio = audio;

    const tps = Number(data.tap_pause_seconds);
    if (!isNaN(tps) && tps !== DEFAULTS.tap_pause_seconds) {
      n.tap_pause_seconds = tps;
    }

    if (Array.isArray(this._config.captions)) n.captions = this._config.captions;
    else if (this._config.caption != null) n.caption = this._config.caption;

    this._config = n;
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: n },
        bubbles: true,
        composed: true,
      }),
    );
  }
  };
}

// Home Assistant's own weather icon shapes, adapted from the frontend's src/data/weather.ts
// (Apache-2.0, see www/weather/LICENSE). Colors follow the --weather-icon-* theme variables.
const HA_WEATHER_PATHS = {
  sun: "m 14.39303,8.4033507 c 0,3.3114723 -2.684145,5.9956173 -5.9956169,5.9956173 -3.3114716,0 -5.9956168,-2.684145 -5.9956168,-5.9956173 0,-3.311471 2.6841452,-5.995617 5.9956168,-5.995617 3.3114719,0 5.9956169,2.684146 5.9956169,5.995617",
  moon: "m 13.502891,11.382935 c -1.011285,1.859223 -2.976664,3.121381 -5.2405751,3.121381 -3.289929,0 -5.953329,-2.663833 -5.953329,-5.9537625 0,-2.263911 1.261724,-4.228856 3.120948,-5.240575 -0.452782,0.842738 -0.712753,1.806363 -0.712753,2.832381 0,3.289928 2.663833,5.9533275 5.9533291,5.9533275 1.026017,0 1.989641,-0.259969 2.83238,-0.712752",
  partly: "m14.981 4.2112c0 1.9244-1.56 3.4844-3.484 3.4844-1.9244 0-3.4844-1.56-3.4844-3.4844s1.56-3.484 3.4844-3.484c1.924 0 3.484 1.5596 3.484 3.484",
  cloudBack: "m3.8863 5.035c-0.54892 0.16898-1.04 0.46637-1.4372 0.8636-0.63077 0.63041-1.0206 1.4933-1.0206 2.455 0 1.9251 1.5589 3.4682 3.4837 3.4682h6.9688c1.9251 0 3.484-1.5981 3.484-3.5232 0-1.9251-1.5589-3.5232-3.484-3.5232h-1.0834c-0.25294-1.6916-1.6986-2.9083-3.4463-2.9083-1.7995 0-3.2805 1.4153-3.465 3.1679",
  cloudFront: "m4.1996 7.6995c-0.33902 0.10407-0.64276 0.28787-0.88794 0.5334-0.39017 0.38982-0.63147 0.92322-0.63147 1.5176 0 1.1896 0.96414 2.1431 2.1537 2.1431h4.3071c1.1896 0 2.153-0.98742 2.153-2.1777 0-1.1896-0.96344-2.1777-2.153-2.1777h-0.66992c-0.15593-1.0449-1.0499-1.7974-2.1297-1.7974-1.112 0-2.0274 0.87524-2.1417 1.9586",
  rain: [
    "m5.2852 14.734c-0.22401 0.24765-0.57115 0.2988-0.77505 0.11395-0.20391-0.1845-0.18732-0.53481 0.036689-0.78281 0.14817-0.16298 0.59126-0.32914 0.87559-0.42369 0.12453-0.04092 0.22684 0.05186 0.19791 0.17956-0.065617 0.2921-0.18732 0.74965-0.33514 0.91299",
    "m11.257 14.163c-0.22437 0.24765-0.57115 0.2988-0.77505 0.11395-0.2039-0.1845-0.18768-0.53481 0.03669-0.78281 0.14817-0.16298 0.59126-0.32914 0.8756-0.42369 0.12453-0.04092 0.22684 0.05186 0.19791 0.17956-0.06562 0.2921-0.18732 0.74965-0.33514 0.91299",
    "m8.432 15.878c-0.15452 0.17039-0.3937 0.20567-0.53446 0.07867-0.14041-0.12735-0.12876-0.36865 0.025753-0.53975 0.10195-0.11218 0.40711-0.22684 0.60325-0.29175 0.085725-0.02858 0.15628 0.03563 0.13652 0.12382-0.045508 0.20108-0.12912 0.51647-0.23107 0.629",
    "m7.9991 14.118c-0.19226 0.21237-0.49001 0.25612-0.66499 0.09737-0.17462-0.15804-0.16051-0.45861 0.03175-0.67098 0.12665-0.14005 0.50729-0.28293 0.75071-0.36336 0.10689-0.03563 0.19473 0.0441 0.17004 0.15346-0.056092 0.25082-0.16051 0.64347-0.28751 0.78352",
  ],
  pouring: [
    "m10.648 16.448c-0.19226 0.21449-0.49001 0.25894-0.66499 0.09878-0.17498-0.16016-0.16087-0.4639 0.03175-0.67874 0.12665-0.14146 0.50694-0.2854 0.75071-0.36724 0.10689-0.03563 0.19473 0.0448 0.17004 0.15558-0.05645 0.25365-0.16051 0.65017-0.28751 0.79163",
    "m5.9383 16.658c-0.22437 0.25012-0.5715 0.30162-0.77505 0.11501-0.20391-0.18627-0.18768-0.54046 0.036689-0.79093 0.14817-0.1651 0.59126-0.33267 0.87559-0.42827 0.12418-0.04127 0.22648 0.05221 0.19791 0.18168-0.065617 0.29528-0.18732 0.75741-0.33514 0.92251",
  ],
  wind: [
    "m 13.59616,15.30968 c 0,0 -0.09137,-0.0071 -0.250472,-0.0187 -0.158045,-0.01235 -0.381353,-0.02893 -0.64382,-0.05715 -0.262466,-0.02716 -0.564444,-0.06385 -0.877358,-0.124531 -0.156986,-0.03034 -0.315383,-0.06844 -0.473781,-0.111478 -0.157691,-0.04551 -0.313266,-0.09842 -0.463902,-0.161219 l -0.267406,-0.0949 c -0.09984,-0.02646 -0.205669,-0.04904 -0.305153,-0.06738 -0.193322,-0.02716 -0.3838218,-0.03316 -0.5640912,-0.02011 -0.3626556,0.02611 -0.6847417,0.119239 -0.94615,0.226483 -0.2617611,0.108656 -0.4642556,0.230364 -0.600075,0.324203 -0.1358195,0.09419 -0.2049639,0.160514 -0.2049639,0.160514 0,0 0.089958,-0.01623 0.24765,-0.04445 0.1559278,-0.02575 0.3764139,-0.06174 0.6367639,-0.08714 0.2596444,-0.02646 0.5591527,-0.0441 0.8678333,-0.02328 0.076905,0.0035 0.1538111,0.01658 0.2321278,0.02293 0.077611,0.01058 0.1534581,0.02893 0.2314221,0.04022 0.07267,0.01834 0.1397,0.03986 0.213078,0.05644 l 0.238125,0.08925 c 0.09207,0.03281 0.183444,0.07055 0.275872,0.09878 0.09243,0.0261 0.185208,0.05327 0.277636,0.07161 0.184856,0.0388 0.367947,0.06174 0.543983,0.0702 0.353131,0.01905 0.678745,-0.01341 0.951442,-0.06456 0.27305,-0.05292 0.494595,-0.123119 0.646642,-0.181681 0.152047,-0.05785 0.234597,-0.104069 0.234597,-0.104069",
    "m 4.7519154,13.905801 c 0,0 0.091369,-0.0032 0.2511778,-0.0092 0.1580444,-0.0064 0.3820583,-0.01446 0.6455833,-0.03281 0.2631722,-0.01729 0.5662083,-0.04269 0.8812389,-0.09137 0.1576916,-0.02434 0.3175,-0.05609 0.4776611,-0.09384 0.1591027,-0.03951 0.3167944,-0.08643 0.4699,-0.14358 l 0.2702277,-0.08467 c 0.1008945,-0.02222 0.2074334,-0.04127 0.3072695,-0.05574 0.1943805,-0.01976 0.3848805,-0.0187 0.5651499,0.0014 0.3608917,0.03951 0.67945,0.144639 0.936625,0.261761 0.2575278,0.118534 0.4554364,0.247297 0.5873754,0.346781 0.132291,0.09913 0.198966,0.168275 0.198966,0.168275 0,0 -0.08925,-0.01976 -0.245886,-0.05397 C 9.9423347,14.087088 9.7232597,14.042988 9.4639681,14.00736 9.2057347,13.97173 8.9072848,13.94245 8.5978986,13.95162 c -0.077258,7.06e-4 -0.1541638,0.01058 -0.2328333,0.01411 -0.077964,0.0078 -0.1545166,0.02328 -0.2331861,0.03175 -0.073025,0.01588 -0.1404055,0.03422 -0.2141361,0.04798 l -0.2420055,0.08008 c -0.093486,0.02963 -0.1859139,0.06421 -0.2794,0.0889 C 7.3028516,14.23666 7.2093653,14.2603 7.116232,14.27512 6.9303181,14.30722 6.7465209,14.3231 6.5697792,14.32486 6.2166487,14.33046 5.8924459,14.28605 5.6218654,14.224318 5.3505793,14.161565 5.1318571,14.082895 4.9822793,14.01869 4.8327015,13.95519 4.7519154,13.905801 4.7519154,13.905801",
  ],
  snow: [
    "m 8.4319893,15.348341 c 0,0.257881 -0.209197,0.467079 -0.467078,0.467079 -0.258586,0 -0.46743,-0.209198 -0.46743,-0.467079 0,-0.258233 0.208844,-0.467431 0.46743,-0.467431 0.257881,0 0.467078,0.209198 0.467078,0.467431",
    "m 11.263878,14.358553 c 0,0.364067 -0.295275,0.659694 -0.659695,0.659694 -0.364419,0 -0.6596937,-0.295627 -0.6596937,-0.659694 0,-0.364419 0.2952747,-0.659694 0.6596937,-0.659694 0.36442,0 0.659695,0.295275 0.659695,0.659694",
    "m 5.3252173,13.69847 c 0,0.364419 -0.295275,0.660047 -0.659695,0.660047 -0.364067,0 -0.659694,-0.295628 -0.659694,-0.660047 0,-0.364067 0.295627,-0.659694 0.659694,-0.659694 0.36442,0 0.659695,0.295627 0.659695,0.659694",
  ],
  lightning: "m 9.9252695,10.935875 -1.6483986,2.341014 1.1170184,0.05929 -1.2169864,2.02141 3.0450261,-2.616159 H 9.8864918 L 10.97937,11.294651 10.700323,10.79794 h -0.508706 l -0.2663475,0.137936",
};
const HA_CLOUDY_STATES = new Set([
  "partlycloudy", "cloudy", "fog", "windy", "windy-variant", "hail", "rainy", "snowy",
  "snowy-rainy", "pouring", "lightning", "lightning-rainy",
]);
const HA_RAIN_STATES = new Set(["hail", "rainy", "pouring", "lightning-rainy"]);

function homeAssistantWeatherSvg(state, night) {
  const p = HA_WEATHER_PATHS;
  const paths = [];
  if (state === "sunny") paths.push(["sun", p.sun]);
  if (state === "clear-night") paths.push(["moon", p.moon]);
  if (state === "partlycloudy") paths.push([night ? "moon" : "sun", p.partly]);
  if (HA_CLOUDY_STATES.has(state)) paths.push(["cloud-back", p.cloudBack], ["cloud-front", p.cloudFront]);
  if (HA_RAIN_STATES.has(state)) paths.push(...p.rain.map((d) => ["rain", d]));
  if (state === "pouring") paths.push(...p.pouring.map((d) => ["rain", d]));
  if (state === "windy" || state === "windy-variant") paths.push(...p.wind.map((d) => ["cloud-back", d]));
  if (state === "snowy" || state === "snowy-rainy") paths.push(...p.snow.map((d) => ["snow", d]));
  if (state === "lightning" || state === "lightning-rainy") paths.push(["sun", p.lightning]);
  if (!paths.length) return null;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 17 17">${
    paths.map(([cls, d]) => `<path class="${cls}" d="${d}"/>`).join("")}</svg>`;
}

/**
 * Register both elements so they survive the
 * ``@webcomponents/scoped-custom-element-registry`` polyfill that
 * browser_mod and hui-element load. That polyfill replaces both
 * ``window.customElements`` and ``window.HTMLElement``. A custom element
 * only works if its class extends the *current* global ``HTMLElement``
 * and is registered in the *current* global registry:
 *
 *   - If we register against the native objects and the polyfill later
 *     swaps the globals, HA looks the element up in the new registry,
 *     finds nothing, and renders "Custom element doesn't exist"
 *     (or throws "Illegal constructor" when it tries to build it).
 *   - If the polyfill is already active and we extend the native
 *     ``HTMLElement`` instead of the polyfilled one, the polyfilled
 *     ``define`` silently refuses the registration.
 *
 * Building the classes from the live globals on every pass, and
 * re-running after the polyfill has had a chance to load, covers all
 * orderings. ``get()`` guards make repeat passes harmless no-ops.
 */
function defineAlbumSlideshowCards() {
  const reg = window.customElements;
  if (!reg) return;
  const Base = window.HTMLElement;
  if (!reg.get("album-slideshow-card")) {
    reg.define(
      "album-slideshow-card",
      createAlbumSlideshowCardClass(Base),
    );
  }
  if (!reg.get("album-slideshow-card-editor")) {
    reg.define(
      "album-slideshow-card-editor",
      createAlbumSlideshowCardEditorClass(Base),
    );
  }
}

defineAlbumSlideshowCards();
if (!window.__albumSlideshowCardScheduled) {
  window.__albumSlideshowCardScheduled = true;
  const retry = () => {
    try {
      defineAlbumSlideshowCards();
    } catch (_) {
      /* a concurrent registry swap is harmless; the next pass settles it */
    }
  };
  Promise.resolve().then(retry);
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(retry);
  }
  setTimeout(retry, 0);
  setTimeout(retry, 1000);
}

window.customCards = window.customCards || [];
if (!window.customCards.find((c) => c.type === "album-slideshow-card")) {
  window.customCards.push({
    type: "album-slideshow-card",
    name: "Album Slideshow",
    description:
      "Cross-fade slideshow for album_slideshow cameras (browser-side, GPU-composited)",
    preview: false,
    documentationURL:
      "https://github.com/eyalgal/album_slideshow#album-slideshow-card",
    // HA 2026.6+ "By entity" card picker (Community section). Only
    // suggest for cameras created by THIS integration, never for every
    // camera in the house - the dev blog warns an over-eager hook makes
    // the picker noisy. We gate on the entity's platform AND the camera
    // domain rather than matching the bare ``camera.*`` domain.
    getEntitySuggestion: (hass, entityId) => {
      if (typeof entityId !== "string" || !entityId.startsWith("camera.")) {
        return null;
      }
      const entry = hass && hass.entities && hass.entities[entityId];
      if (!entry || entry.platform !== "album_slideshow") {
        return null;
      }
      return {
        config: {
          type: "custom:album-slideshow-card",
          entity: entityId,
        },
      };
    },
  });
}

console.info(
  `%c album-slideshow-card %c v${VERSION} `,
  "color: white; background: #4a90e2; padding: 1px 4px; border-radius: 3px 0 0 3px;",
  "color: #4a90e2; background: white; padding: 1px 4px; border-radius: 0 3px 3px 0;",
);
