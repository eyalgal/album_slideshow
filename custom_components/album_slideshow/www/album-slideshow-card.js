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
 */

const VERSION = "1.4.0";

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

// Caption overlay (date / location / description). ``show`` is an ordered
// subset of these fields; ``position`` is one of a 3x3 anchor grid;
// ``date_format`` is one of the named presets below or a custom token string.
const CAPTION_FIELDS = ["date", "location", "description"];
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
      // Caption overlay (date / location). ``null`` when disabled.
      caption: this._normalizeCaption(config.caption),
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
        }
        .cap-box {
          display: flex;
          flex-direction: column;
          max-width: 92%;
          line-height: 1.25;
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
        }
        .cap-line { font-weight: inherit; }
        .cap-box.cap-shadow {
          text-shadow:
            0 1px 2px rgba(0, 0, 0, 0.9),
            0 1px 6px rgba(0, 0, 0, 0.55);
        }
        ${this._transitionStyles()}
      </style>
      <ha-card part="card">
        <div class="stage" id="stage">
          <img class="blur-bg" id="blur-a" alt="" />
          <img class="blur-bg" id="blur-b" alt="" />
          <img class="layer" id="a" alt="" />
          <img class="layer" id="b" alt="" />
          <div class="captions" id="captions" aria-hidden="true"></div>
          <div class="placeholder" id="placeholder">Waiting for first frame...</div>
        </div>
      </ha-card>
    `;
    const card = this.shadowRoot.querySelector("ha-card");
    if (this._config.tap_action === "more-info") {
      card.addEventListener("click", () => this._fireMoreInfo());
      card.style.cursor = "pointer";
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
        }
      : null;
    this._loadAndSwap(url, fit, blurBackdrop, captionData);
  }

  _loadAndSwap(url, fit, blurBackdrop, captionData) {
    // Pre-decode the new image so the swap is instant.
    const next = new Image();
    next.decoding = "async";
    next.onload = () => this._performSwap(url, fit, blurBackdrop, captionData);
    next.onerror = () => this._setPlaceholder("Failed to load slide");
    next.src = url;
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
    const cap = this._config.caption;
    const container =
      this.shadowRoot && this.shadowRoot.getElementById("captions");
    if (!container) return;
    container.innerHTML = "";
    if (!cap || !captionData) return;

    const frames = this._buildCaptionFrames(captionData);
    const orientation = captionData.pair_orientation;
    const isPair =
      cap.per_image &&
      frames.length >= 2 &&
      (orientation === "horizontal" || orientation === "vertical");

    if (isPair) {
      this._addCaptionRegion(container, frames[0], cap, orientation, 0);
      this._addCaptionRegion(container, frames[1], cap, orientation, 1);
    } else {
      this._addCaptionRegion(container, frames[0], cap, null, 0);
    }

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
      },
    ];
  }

  _captionLines(frame, cap) {
    const lines = [];
    for (const field of cap.show) {
      if (field === "date") {
        const txt = this._formatDate(frame.captured_at, cap.date_format);
        if (txt) lines.push(txt);
      } else if (field === "location") {
        if (frame.location) lines.push(String(frame.location));
      } else if (field === "description") {
        if (frame.description) lines.push(String(frame.description));
      }
    }
    return lines;
  }

  /** Build one positioned caption block. ``orientation`` is ``null`` for a
   * full-frame caption, or ``horizontal`` / ``vertical`` to anchor the block
   * inside the left/right or top/bottom half of a pair (``half`` 0 or 1). */
  _addCaptionRegion(container, frame, cap, orientation, half) {
    const lines = this._captionLines(frame, cap);
    if (lines.length === 0) return;

    const region = document.createElement("div");
    region.className = "cap-region";

    // Region geometry: full frame, or one half of a pair.
    let top = "0";
    let right = "0";
    let bottom = "0";
    let left = "0";
    if (orientation === "horizontal") {
      if (half === 0) right = "50%";
      else left = "50%";
    } else if (orientation === "vertical") {
      if (half === 0) bottom = "50%";
      else top = "50%";
    }
    region.style.top = top;
    region.style.right = right;
    region.style.bottom = bottom;
    region.style.left = left;

    // Anchor within the region from the 3x3 position grid.
    const pos = cap.position;
    const parts = pos === "center" ? ["center", "center"] : pos.split("-");
    const v = parts[0];
    const h = parts[1] || "center";
    const justify = { left: "flex-start", center: "center", right: "flex-end" };
    const align = { top: "flex-start", center: "center", bottom: "flex-end" };
    region.style.justifyContent = justify[h] || "flex-start";
    region.style.alignItems = align[v] || "flex-end";

    const box = document.createElement("div");
    box.className = "cap-box";
    if (cap.shadow) box.classList.add("cap-shadow");
    box.style.color = cap.color;
    box.style.fontSize = cap.font_size;
    box.style.fontWeight = CAPTION_WEIGHT_MAP[cap.font_weight] || 500;
    box.style.textAlign = h === "center" ? "center" : h;

    for (const line of lines) {
      const el = document.createElement("div");
      el.className = "cap-line";
      el.textContent = line;
      box.appendChild(el);
    }
    region.appendChild(box);
    container.appendChild(region);
  }

  _locale() {
    return (
      (this._hass && this._hass.locale && this._hass.locale.language) ||
      (typeof navigator !== "undefined" && navigator.language) ||
      "en"
    );
  }

  _formatDate(iso, fmt) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const locale = this._locale();
    if (fmt === "relative") return this._relativeTime(d);
    const preset = DATE_FORMAT_PRESETS[fmt];
    if (preset) {
      try {
        return new Intl.DateTimeFormat(locale, preset).format(d);
      } catch (_) {
        return d.toLocaleDateString();
      }
    }
    // Anything else is treated as a custom token string.
    return this._formatTokens(d, String(fmt));
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

  _formatTokens(d, fmt) {
    const locale = this._locale();
    const pad = (n) => String(n).padStart(2, "0");
    const part = (opts) => {
      try {
        return new Intl.DateTimeFormat(locale, opts).format(d);
      } catch (_) {
        return "";
      }
    };
    const map = {
      YYYY: d.getFullYear(),
      YY: pad(d.getFullYear() % 100),
      MMMM: part({ month: "long" }),
      MMM: part({ month: "short" }),
      MM: pad(d.getMonth() + 1),
      M: d.getMonth() + 1,
      DD: pad(d.getDate()),
      D: d.getDate(),
      dddd: part({ weekday: "long" }),
      ddd: part({ weekday: "short" }),
      HH: pad(d.getHours()),
      mm: pad(d.getMinutes()),
    };
    return fmt.replace(
      /YYYY|YY|MMMM|MMM|MM|M|DD|D|dddd|ddd|HH|mm/g,
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
  { value: "date", label: "Date" },
  { value: "location", label: "Location" },
  { value: "description", label: "Description" },
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

const CAPTION_DATE_FORMAT_OPTIONS = [
  { value: "medium", label: "Medium (Aug 16, 2014)" },
  { value: "full", label: "Full (August 16, 2014)" },
  { value: "month_year", label: "Month & year (August 2014)" },
  { value: "year", label: "Year (2014)" },
  { value: "numeric", label: "Numeric (8/16/2014)" },
  { value: "weekday", label: "Weekday (Saturday, August 16, 2014)" },
  { value: "relative", label: "Relative (3 years ago)" },
];

const DEFAULTS = {
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
  "missing_date_mode",
  "portrait_mode",
  "order_mode",
  "slide_interval",
  "pair_divider_px",
  "pair_divider_color",
];

const LIVE_SUFFIX = {
  paused: "_paused",
  date_filter: "_date_filter",
  missing_date_mode: "_missing_date_mode",
  portrait_mode: "_portrait_mode",
  order_mode: "_order_mode",
  slide_interval: "_interval",
  pair_divider_px: "_pair_divider_px",
  pair_divider_color: "_pair_divider_color",
  next_button: "_next_button",
  refresh_button: "_refresh_button",
};

const LIVE_LABELS = {
  live_paused: "Pause slideshow",
  live_date_filter: "Date filter",
  live_missing_date_mode: "Missing capture date",
  live_portrait_mode: "Orientation mismatch mode",
  live_order_mode: "Order mode",
  live_slide_interval: "Slide interval (seconds)",
  live_pair_divider_px: "Pair divider size (px)",
  live_pair_divider_color: "Pair divider color",
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
      (this._siblings.next_button || this._siblings.refresh_button)
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
      mode: "box",
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
        ],
      },
      {
        type: "expandable",
        title: "Interaction",
        icon: "mdi:gesture-tap",
        schema: [
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
      {
        type: "expandable",
        title: "Caption (date, location & description)",
        icon: "mdi:format-text",
        schema: [
          { name: "caption_enabled", selector: { boolean: {} } },
          {
            name: "caption_show",
            selector: {
              select: {
                multiple: true,
                mode: "list",
                options: CAPTION_SHOW_OPTIONS,
              },
            },
          },
          {
            name: "caption_position",
            selector: {
              select: { mode: "dropdown", options: CAPTION_POSITION_OPTIONS },
            },
          },
          {
            name: "caption_date_format",
            selector: {
              select: {
                mode: "dropdown",
                custom_value: true,
                options: CAPTION_DATE_FORMAT_OPTIONS,
              },
            },
          },
          { name: "caption_per_image", selector: { boolean: {} } },
          {
            type: "grid",
            name: "",
            schema: [
              { name: "caption_color", selector: { text: {} } },
              { name: "caption_font_size", selector: { text: {} } },
            ],
          },
          {
            name: "caption_font_weight",
            selector: {
              select: { mode: "dropdown", options: CAPTION_WEIGHT_OPTIONS },
            },
          },
          { name: "caption_shadow", selector: { boolean: {} } },
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
      transition: c.transition || DEFAULTS.transition,
      duration: c.duration != null ? Number(c.duration) : DEFAULTS.duration,
      easing: c.easing || DEFAULTS.easing,
      aspect_ratio: c.aspect_ratio || DEFAULTS.aspect_ratio,
      fit: c.fit || DEFAULTS.fit,
      background: c.background || "",
      tap_action: c.tap_action || DEFAULTS.tap_action,
      tap_pause_seconds:
        c.tap_pause_seconds != null
          ? Number(c.tap_pause_seconds)
          : DEFAULTS.tap_pause_seconds,
      ...this._captionData(),
      ...this._liveDataFromStates(),
    };
  }

  /** Flatten the nested ``caption`` config into the fields ha-form binds. */
  _captionData() {
    const cap = this._config && this._config.caption;
    const enabled = !!cap && cap !== false;
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
      caption_color: c.color || CAPTION_DEFAULTS.color,
      caption_font_size: c.font_size || CAPTION_DEFAULTS.font_size,
      caption_font_weight: c.font_weight || CAPTION_DEFAULTS.font_weight,
      caption_shadow: c.shadow !== false,
    };
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
    for (const f of ["slide_interval", "pair_divider_px"]) {
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
      caption_enabled: "Show caption overlay",
      caption_show: "Show",
      caption_position: "Position",
      caption_per_image: "Per-image captions on pairs",
      caption_date_format: "Date format",
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
      tap_pause_seconds:
        "How long the card freezes its slide after a tap. 0 disables it.",
      caption_date_format:
        "Pick a preset or type a custom format (YYYY, MMMM, MMM, MM, DD, D).",
      caption_show:
        "Description comes from the photo's EXIF/IPTC/XMP caption and is only available with the local-folder provider.",
      caption_per_image:
        "When a portrait pair is shown, caption each photo with its own date, location and description.",
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
      </style>
      <div class="card-config">
        <div class="info-slot"></div>
        <ha-form></ha-form>
        <div class="actions" hidden></div>
      </div>
    `;
    const form = this.shadowRoot.querySelector("ha-form");
    form.computeLabel = this._computeLabel;
    form.computeHelper = this._computeHelper;
    form.addEventListener("value-changed", (ev) => this._valueChanged(ev));
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
  }

  _renderActions() {
    const wrap = this.shadowRoot.querySelector(".actions");
    if (!wrap) return;
    if (!this._hasActions()) {
      wrap.hidden = true;
      wrap.innerHTML = "";
      return;
    }
    const s = this._siblings;
    wrap.hidden = false;
    wrap.innerHTML = `
      <div class="actions-title">Actions</div>
      <div class="actions-row">
        ${s.next_button ? `<button class="act" data-act="next">Next slide</button>` : ""}
        ${s.refresh_button ? `<button class="act" data-act="refresh">Refresh album</button>` : ""}
      </div>
    `;
    wrap.querySelectorAll("button.act").forEach((b) => {
      b.addEventListener("click", () => {
        const id = b.dataset.act === "next" ? s.next_button : s.refresh_button;
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
    } else if (field === "slide_interval" || field === "pair_divider_px") {
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

    const tps = Number(data.tap_pause_seconds);
    if (!isNaN(tps) && tps !== DEFAULTS.tap_pause_seconds) {
      n.tap_pause_seconds = tps;
    }

    // Caption: only emit a ``caption`` block when enabled and at least one
    // field is selected. Non-default sub-settings are written; defaults are
    // omitted to keep the YAML lean.
    if (data.caption_enabled) {
      let show = data.caption_show;
      if (!Array.isArray(show)) show = show ? [show] : [];
      show = show.filter(
        (v) => v === "date" || v === "location" || v === "description",
      );
      if (show.length > 0) {
        const cap = { show };
        const pos = data.caption_position || CAPTION_DEFAULTS.position;
        if (pos !== CAPTION_DEFAULTS.position) cap.position = pos;
        if (data.caption_per_image === false) cap.per_image = false;
        const df = data.caption_date_format || CAPTION_DEFAULTS.date_format;
        if (df !== CAPTION_DEFAULTS.date_format) cap.date_format = df;
        const col = (data.caption_color || "").trim();
        if (col && col.toLowerCase() !== CAPTION_DEFAULTS.color) cap.color = col;
        const fs = (data.caption_font_size || "").trim();
        if (fs && fs !== CAPTION_DEFAULTS.font_size) cap.font_size = fs;
        const fw = data.caption_font_weight || CAPTION_DEFAULTS.font_weight;
        if (fw !== CAPTION_DEFAULTS.font_weight) cap.font_weight = fw;
        if (data.caption_shadow === false) cap.shadow = false;
        n.caption = cap;
      }
    }

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
