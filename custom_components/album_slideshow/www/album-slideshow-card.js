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
 *   swipe_navigation: true  # touch/swipe between slides
 */

const VERSION = "0.7.1-dev";

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

class AlbumSlideshowCard extends HTMLElement {
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
      // Number of seconds the card freezes its visible slide after a
      // tap, so the more-info dialog can settle without the slideshow
      // marching forward beneath it. Set to 0 to disable.
      tap_pause_seconds:
        config.tap_pause_seconds === 0
          ? 0
          : Number(config.tap_pause_seconds ?? 8),
      // Touch/mouse swipe to navigate. Left/up swipe -> next slide,
      // right/down swipe -> previous. Disable to make the card a pure
      // display surface (e.g. wall-mounted dashboards where stray taps
      // shouldn't change the playback).
      swipe_navigation:
        config.swipe_navigation === false ? false : true,
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

  getCardSize() {
    return 4;
  }

  connectedCallback() {
    if (!this._rendered) {
      this._renderShell();
      this._rendered = true;
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) return;
    this._maybeSwap();
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
        .stage {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          background: ${stageBg};
          border-radius: inherit;
          overflow: hidden;
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
        .placeholder {
          position: absolute;
          inset: 0;
          display: grid;
          place-items: center;
          color: var(--secondary-text-color, rgba(255, 255, 255, 0.5));
          font-size: 0.85rem;
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
        }
        ${this._transitionStyles()}
      </style>
      <ha-card part="card">
        <div class="stage" id="stage">
          <img class="blur-bg" id="blur-a" alt="" />
          <img class="blur-bg" id="blur-b" alt="" />
          <img class="layer" id="a" alt="" />
          <img class="layer" id="b" alt="" />
          <div class="placeholder" id="placeholder">Waiting for first frame...</div>
        </div>
      </ha-card>
    `;
    const card = this.shadowRoot.querySelector("ha-card");
    if (this._config.tap_action === "more-info") {
      card.addEventListener("click", () => {
        // Swipe handlers set this when they have just consumed a
        // gesture so the synthesised click doesn't also fire more-info.
        if (this._suppressNextClick) {
          this._suppressNextClick = false;
          return;
        }
        this._fireMoreInfo();
      });
      card.style.cursor = "pointer";
    }
    if (this._config.swipe_navigation) {
      this._installSwipeHandlers(card);
    }
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
    const attrs = state.attributes || {};
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
    this._loadAndSwap(url, fit, blurBackdrop);
  }

  _loadAndSwap(url, fit, blurBackdrop) {
    // Pre-decode the new image so the swap is instant.
    const next = new Image();
    next.decoding = "async";
    next.onload = () => this._performSwap(url, fit, blurBackdrop);
    next.onerror = () => this._setPlaceholder("Failed to load slide");
    next.src = url;
  }

  _performSwap(url, fit, blurBackdrop) {
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

    // Cleanup the .exit class after the animation so it doesn't fight the
    // next swap. Slightly longer than the duration to be safe.
    const dur = this._config.duration + 50;
    setTimeout(() => {
      showing.classList.remove("exit");
    }, dur);
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

  /** Install pointer-based swipe handlers on the card root.
   *
   * Threshold is 50px horizontal travel within 700ms with horizontal
   * dominance over vertical (so the user can still vertically scroll
   * the dashboard past the card). On a successful swipe we call the
   * camera's ``next_slide`` / ``previous_slide`` service via the
   * camera's ``entry_id`` state attribute, then freeze the visible
   * slide for ``tap_pause_seconds`` so the user actually gets to look
   * at the photo they swiped to before the slideshow auto-advances.
   *
   * Suppresses the click that pointerup would otherwise synthesise so
   * a swipe doesn't also fire ``tap_action: more-info``.
   */
  _installSwipeHandlers(card) {
    const SWIPE_MIN_PX = 50;
    const SWIPE_MAX_MS = 700;
    const DRAG_LOCK_PX = 8;
    let startX = 0;
    let startY = 0;
    let startT = 0;
    let pointerId = null;
    let isSwiping = false;

    // Tell the browser we want to handle horizontal drags ourselves but
    // still allow vertical scrolling of the parent dashboard.
    card.style.touchAction = "pan-y";

    card.addEventListener("pointerdown", (ev) => {
      if (ev.pointerType === "mouse" && ev.button !== 0) return;
      pointerId = ev.pointerId;
      startX = ev.clientX;
      startY = ev.clientY;
      startT = ev.timeStamp;
      isSwiping = false;
    });

    card.addEventListener("pointermove", (ev) => {
      if (ev.pointerId !== pointerId) return;
      if (isSwiping) return;
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (Math.abs(dx) > DRAG_LOCK_PX && Math.abs(dx) > Math.abs(dy)) {
        isSwiping = true;
      }
    });

    const finish = (ev) => {
      if (ev.pointerId !== pointerId) return;
      const wasSwiping = isSwiping;
      pointerId = null;
      isSwiping = false;
      if (!wasSwiping) return;
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      const dt = ev.timeStamp - startT;
      if (dt > SWIPE_MAX_MS) return;
      if (Math.abs(dx) < SWIPE_MIN_PX) return;
      if (Math.abs(dy) > Math.abs(dx)) return;
      this._suppressNextClick = true;
      const pauseSec = this._config.tap_pause_seconds;
      if (pauseSec > 0) {
        this._holdSwapsUntil = Date.now() + pauseSec * 1000;
      }
      if (dx < 0) {
        this._callSlideshowService("next_slide");
      } else {
        this._callSlideshowService("previous_slide");
      }
    };

    card.addEventListener("pointerup", finish);
    card.addEventListener("pointercancel", (ev) => {
      if (ev.pointerId === pointerId) {
        pointerId = null;
        isSwiping = false;
      }
    });
  }

  /** Call an album_slideshow service for the camera this card targets.
   *
   * Pulls ``entry_id`` from the camera's state attributes (added in
   * v0.7.1+). Older integrations don't expose it; we log a warning
   * and bail rather than guess.
   */
  _callSlideshowService(service) {
    if (!this._hass || !this._config) return;
    const state = this._hass.states[this._config.entity];
    const entryId = state && state.attributes && state.attributes.entry_id;
    if (!entryId) {
      console.warn(
        "album-slideshow-card: camera state has no 'entry_id' attribute; " +
          "swipe navigation requires Album Slideshow integration v0.7.1+",
      );
      return;
    }
    this._hass.callService("album_slideshow", service, {
      entry_id: entryId,
    });
  }
}

if (!customElements.get("album-slideshow-card")) {
  customElements.define("album-slideshow-card", AlbumSlideshowCard);
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

const DEFAULTS = {
  transition: "random",
  duration: 600,
  easing: "ease-in-out",
  aspect_ratio: "16/9",
  fit: "auto",
  background: "",
  tap_action: "none",
  swipe_navigation: true,
};

class AlbumSlideshowCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._rendered = false;
    this._lastEntityCount = -1;
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
    // If the set of album_slideshow cameras changed, the warning box
    // needs to appear/disappear.
    if (!prev || this._countSlideshowCameras() !== this._lastEntityCount) {
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

  /** ha-form schema. The whole form is delegated to ``ha-form`` instead
   * of building HTML manually, which sidesteps every lazy-loading edge
   * case: ``ha-form`` is loaded eagerly by HA core and on first render
   * imports the controls each selector needs. The card just provides
   * a schema and reacts to ``value-changed`` events. */
  _schema() {
    return [
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
      {
        name: "tap_action",
        selector: { select: { mode: "dropdown", options: TAP_OPTIONS } },
      },
      { name: "swipe_navigation", selector: { boolean: {} } },
    ];
  }

  /** Map our config object to the flat data shape ha-form expects. */
  _data() {
    const c = this._config || {};
    return {
      entity: c.entity || "",
      transition: c.transition || DEFAULTS.transition,
      duration: c.duration != null ? Number(c.duration) : DEFAULTS.duration,
      easing: c.easing || DEFAULTS.easing,
      aspect_ratio: c.aspect_ratio || DEFAULTS.aspect_ratio,
      fit: c.fit || DEFAULTS.fit,
      background: c.background || "",
      tap_action: c.tap_action || DEFAULTS.tap_action,
      swipe_navigation:
        c.swipe_navigation === false ? false : DEFAULTS.swipe_navigation,
    };
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
      swipe_navigation: "Swipe navigation",
    };
    return labels[s.name] || s.name;
  };

  _computeHelper = (s) => {
    const helpers = {
      background: "Leave blank to inherit the dashboard theme.",
      transition: "Random picks a different effect each slide.",
      swipe_navigation:
        "Swipe left for next slide, right for previous. Disable for kiosk-style displays.",
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
      </style>
      <div class="card-config">
        <div class="info-slot"></div>
        <ha-form></ha-form>
      </div>
    `;
    const form = this.shadowRoot.querySelector("ha-form");
    form.computeLabel = this._computeLabel;
    form.computeHelper = this._computeHelper;
    form.addEventListener("value-changed", (ev) => this._valueChanged(ev));
    this._rendered = true;
    this._update();
  }

  _update() {
    if (!this._rendered) return;
    const form = this.shadowRoot.querySelector("ha-form");
    if (!form) return;
    form.hass = this._hass;
    form.schema = this._schema();
    form.data = this._data();

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
  }

  _valueChanged(ev) {
    ev.stopPropagation();
    const data = ev?.detail?.value || {};
    const n = { type: "custom:album-slideshow-card" };

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

    // ``swipe_navigation`` defaults to true; only persist the override
    // when the user explicitly turns it off. Keeps the saved YAML
    // minimal so the default tracks future changes if we ever flip it.
    if (data.swipe_navigation === false) n.swipe_navigation = false;

    this._config = n;
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: n },
        bubbles: true,
        composed: true,
      }),
    );
  }
}

if (!customElements.get("album-slideshow-card-editor")) {
  customElements.define(
    "album-slideshow-card-editor",
    AlbumSlideshowCardEditor,
  );
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
  });
}

console.info(
  `%c album-slideshow-card %c v${VERSION} `,
  "color: white; background: #4a90e2; padding: 1px 4px; border-radius: 3px 0 0 3px;",
  "color: #4a90e2; background: white; padding: 1px 4px; border-radius: 0 3px 3px 0;",
);
