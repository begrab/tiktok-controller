# TikTok AI Controller — Full Context & Mac Port Handoff

> **Purpose of this document.** This is a complete, from-scratch context dump of
> everything built in this project, written so you (or another engineer/agent)
> could **rebuild it on macOS** without the original Windows + iMouse setup. It
> covers what each part does, *why* it does it that way (hard-won from real
> debugging), the exact contracts between layers, and a concrete port plan.
>
> The single most important idea: **only ONE component is Windows/iMouse-specific
> — the device I/O layer (taps, swipes, screenshot, OCR).** Everything above it
> (the perceive-decide-act agent, the OCR rail-anchoring math, the campaign
> brain, the niche/content model, the comment system, the web UI) is portable
> Python/JS and moves to Mac unchanged. Port the bottom 15%, keep the top 85%.

---

## 0. TL;DR for the impatient

| Layer | What it is | Windows/iMouse-specific? | Mac replacement |
|---|---|---|---|
| **Control** (tap/swipe/key) | iMouse XP local HTTP API over USB | ✅ Yes | WebDriverAgent (iOS) / `wda` or Appium; `adb`/scrcpy (Android) |
| **Capture** (screenshot) | iMouse `/pic/screenshot` of an AirPlay mirror | ✅ Yes | WDA `/screenshot`, or `idevicescreenshot`, or scrcpy frame grab |
| **OCR** (text + boxes) | iMouse `/pic/ocr` = **bundled PaddleOCR plugin** | ⚠️ Engine only | **Apple Vision** `VNRecognizeTextRequest` (best on Mac), or RapidOCR/PaddleOCR |
| **Coordinate model** | `imgw × imgh` mirror-resolution space | ❌ No (concept) | Same concept, read from WDA window size |
| **Rail anchoring** | OCR-count → 4-slot grid math | ❌ No | Unchanged Python |
| **Agent brain** | Claude perceive-decide-act loop | ❌ No | Unchanged Python |
| **Campaign engine** | relevance/language/pacing/dedup | ❌ No | Unchanged Python |
| **Niche/content** | `niche.py` registry | ❌ No | Unchanged Python |
| **Comment system** | Claude vision + strong model | ❌ No | Unchanged Python |
| **Web UI** | FastAPI + vanilla JS grid | ❌ No | Unchanged |
| **Run logging** | `campaign_log.py` | ❌ No | Unchanged |

**The crown jewels worth preserving verbatim:** (1) the OCR **rail-anchoring
algorithm** (`_ocr_rail_points`), (2) the **settled-perception** anti-lag logic,
(3) the **campaign decision loop** with its dedup/language/live/pacing rules, and
(4) the **niche content model**. None of these touch iMouse.

---

## 1. What this product is

An AI agent that **operates a physical iPhone** to "warm up" TikTok accounts for a
niche marketing campaign. It watches the For You feed like a selective human:

- **Perceives** each video: screenshot + OCR → Claude vision.
- **Classifies** relevance to a configured niche (default: **NutriSkin**, a
  gut-health/acne app), and detects the content **language**, **post type**
  (video / photo slideshow / live), and whether it's even **on the feed**.
- **Engages** relevant, in-language posts with human-like randomness: watch a
  varied time, sometimes like, sometimes save, and *sparingly* post a unique,
  personal-struggle comment (in the post's language) that subtly promotes the app.
- **Skips** irrelevant/off-language/live content fast; **recovers** when it ends
  up off-feed (profile, search, a sheet); **browses** photo slideshows.
- Runs unattended for a set duration, **logs every cycle + screenshot** for review.

There is also a **manual/developer surface**: per-control taps, a UI-map explorer,
mirror recovery tools, a self-test harness, and debug endpoints.

### The business logic in one paragraph
Only engage a post if it is **on the feed** AND **relevant to the niche** AND **in
the target language** AND **not already engaged this session** AND **not a live
stream**. On a qualifying post, roll independent dice for like (0.85) and save
(0.5); comment only if a randomized cooldown (6–12 relevant videos) has passed,
under a per-run cap (8), and then only with probability 0.7 — so comments stay
rare and never spammy. Every comment is freshly written by a strong model, in the
post's language, as a short personal aside. Watch times are jittered with
occasional long tails so pacing reads human.

---

## 2. Physical architecture & why it's split

```
┌─────────────┐   USB (robust)      ┌──────────────┐
│   Mac/PC    │────────────────────▶│   iPhone     │   Control: taps, swipes,
│ (this app)  │                     │  (TikTok)    │   keys — never drops
│             │◀─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │              │   Vision: AirPlay screen
└─────────────┘   Wi-Fi AirPlay     └──────────────┘   mirror — FRAGILE, drops
   (fragile screen mirror)
```

**Critical architectural fact (drove many bugs):**
- **Control travels over USB** and is rock-solid. Taps/swipes/keys almost never fail.
- **Vision travels over Wi-Fi AirPlay screen mirroring** and is **fragile** — it
  drops, lags the phone by **1–2 seconds**, and when it's down you can still
  *control* the phone but can't *see* it.

Consequences baked into the code:
1. **Device "online" state is useless for vision.** A device can report online
   (USB up) while the mirror is dead. The *only* real test of vision is "does a
   screenshot succeed?" → `screenshot_works()`.
2. **Error code 25 = mirror dropped.** Handled by `recover_mirror()` which drives
   the phone's Control Center over USB to re-cast AirPlay — blind, using
   coordinates calibrated once while the mirror was up.
3. **The mirror lags, so captures must "settle."** `settled_perception()` waits
   until two consecutive captures agree before trusting a frame — otherwise the
   agent decides on a stale/transitional screen (this caused "stuck on same
   video" and "engaged the wrong video" bugs).

### Hardware specifics from the reference setup
- Reference phone mirror resolution (`imgw × imgh`): **608 × 1080** (read at
  runtime, never hardcode).
- iOS **15.8.8** on the reference device.
- The control board is a **relative HID mouse**: the console converts an absolute
  `(x,y)` into "move from current pointer position, then click." If the pointer is
  still travelling (tracking speed) or was asleep (AssistiveTouch auto-hide), the
  click fires early and lands short. **Fixes that mattered more than code:** set
  phone pointer **tracking speed to max**, disable AssistiveTouch idle auto-hide,
  and in the iMouse console use **"Cast to Specific Hardware"** with the mouse
  params variant *without* `≤14.2` (Device Model + OS Version "15 and Above" +
  Send Cast Command). Code-side we additionally let the pointer settle
  (`pre_tap_settle`, `post_swipe_settle`).

> **Mac implication:** This whole USB-control + AirPlay-vision split is an
> *iMouse* architecture. On Mac you most likely collapse it: **WebDriverAgent
> gives you both control AND screenshots over one USB channel**, eliminating the
> fragile AirPlay path, the mirror-recovery code, *and* most of the settle/lag
> problems. See §13.

---

## 3. The iMouse XP API (the layer you're replacing)

All device I/O goes through `backend/api_client.py` (`IMouseClient`). This is the
**complete contract you must reimplement on Mac**. Reproduced behavior:

- **Base URL:** `http://127.0.0.1:9911/api` — localhost only, **no API key** (auth
  = being on localhost). Port **9912** seen in third-party guides is **wrong**.
- **Every call:** `POST {"fun": "<endpoint>", "data": {...}}`.
- **Success = HTTP `status == 200` AND `data.code == 0`.** An HTTP 200 with
  non-zero `data.code` is a **failure** — you must check both.
- **`/pic/screenshot` with `binary: true` returns raw JPEG bytes**, not JSON —
  branch on content-type before parsing.
- WebSocket interface exists but is intentionally unused (HTTP measured ~70 ms;
  not worth the complexity).

### 3.1 Error codes
| Code | Meaning | Handling |
|---|---|---|
| `0` | success | — |
| `25` | device offline / **AirPlay mirror dropped** | `recover_mirror()` re-casts |
| `28` | **plugin not connected** (the PaddleOCR console process isn't running) | degrade to vision-only |
| `-1` | transport error (our synthetic code) | retry/backoff |

### 3.2 Every endpoint we call (the full surface to replace)

| `fun` | Method | Purpose | Key params | Returns |
|---|---|---|---|---|
| `/device/get` | `get_devices()` | list all devices ever seen | — | `list` of device dicts; filter `state==1` for online |
| `/pic/screenshot` | `screenshot()` | capture mirror as JPEG | `id, jpg:true, binary:true, rect?, save_path?` | raw JPEG bytes (binary) or base64-in-JSON |
| `/mouse/click` | `click()` | **tap** at (x,y) | `id, x, y` | ok |
| `/mouse/swipe` | `swipe()` | swipe up/down/left/right | `id, direction, len` (fraction) | ok |
| `/mouse/wheel` | `wheel()` | scroll wheel (feed-advance fallback) | `id, direction, len, number` | ok |
| `/key/sendkey` | `send_key()` | type ASCII `key` or hotkey `fn_key` | `id, key?, fn_key?` | ok |
| `/pic/ocr`, `/pic/ocr-ex` | `ocr()` | **OCR** screen → text+`centre`+`rect` | `id, rect?` | `list` of `{text, centre:[x,y], rect}` |
| `/pic/find-text` | `find_text()` | locate specific text, get tap `centre` | `id, text:[...], similarity, contain, rect?` | `list` of matches w/ `centre` |
| `/pic/find-image` | `find_image()` | template-match icon (needs OpenCV plugin — **not installed** on ref machine) | `id, img(b64), similarity, rect?` | `list` |
| `/shortcut/clipboard/set` | `clipboard_set()` | push UTF-8 text to phone clipboard | `id, text, sleep` | ok |
| `/shortcut/clipboard/get` | `clipboard_get()` | read phone clipboard | `id` | text |
| `/shortcut/exec/url` | `exec_url()` | open URL / deep link on phone | `id, url` | ok |
| `/shortcut/device/ip` | `device_ip()` | phone IP | `id` | ip |

### 3.3 Hotkeys (`fn_key`) we rely on
- `WIN+h` → iOS **Home** button. **⚠️ DANGER:** this exits TikTok to the home
  screen. A past bug used `WIN+h` to "close" Control Center and it kept dropping
  the agent out of TikTok (see §11). Mirror-close now uses **swipe-down** instead.
- `TAB+b` → hardware **Back** (used in recovery).
- `WIN+v` → **Paste** (clipboard flow for non-ASCII comment text).
- `ENTER`, `BACKSPACE` → as named.
- `ControlBar` → opens iOS **Control Center** (used to re-cast AirPlay).

### 3.4 Device dict shape (from `/device/get`)
Parsed in `device_manager.py::Device.from_api`. Fields consumed: `deviceid|id|mac`
(id), `name|username`, `model`, `version` (iOS), `state` (1=online),
`imgw`/`imgh` (**the tap coordinate space**), `width`/`height` (iOS logical
points — informational, **NOT** tap-usable), `ip`, `vid`/`pid`/`uid` (bound-ness).

> **Full client source:** `backend/api_client.py` (322 lines). When porting,
> reimplement this class's public methods with identical signatures against your
> Mac backend and *nothing above it needs to change*.

---

## 4. The coordinate system (get this exactly right or every tap misses)

Defined/enforced in `backend/device_manager.py`.

- **All taps and all OCR `centre` values live in the `imgw × imgh` space** — the
  **mirror stream resolution** (608×1080 on the ref phone).
- This is **neither** the iOS logical-point space (`width×height`, e.g. 375×667)
  **nor** the panel resolution. Using logical points for taps **misses**.
- `imgw`/`imgh` are **read at runtime per device**, never hardcoded.
- `Device.to_pixels(fx, fy)` maps fractions `(0..1)` → pixels using `imgw/imgh`
  (falls back to logical only if mirror res is unknown, i.e. mirror down).
- If a device is online but has **no `imgw/imgh`**, mirroring is down and taps
  will be unreliable — the registry logs a warning.

```python
def to_pixels(self, fx: float, fy: float) -> tuple[int, int]:
    w = self.imgw or self.width
    h = self.imgh or self.height
    return int(w * fx), int(h * fy)
```

> **Mac implication:** WDA reports its own window/screen size; make *that* your
> `imgw × imgh`. The whole fractional-anchor + OCR-centre model then works
> unchanged. Keep the "read at runtime, never hardcode" rule.

---

## 5. OCR + the rail-anchoring algorithm  ⭐ (the crown jewel)

### 5.1 What iMouse's OCR actually is
`/pic/ocr` is a **bundled PaddleOCR plugin** (confirmed in our own code comments
and error code 28 = "the PaddleOCR console process is not running"). PaddleOCR is
Apache-2.0 open source. **You do not need iMouse's code for OCR** — the engine is
a commodity. On Mac, prefer **Apple Vision** (`VNRecognizeTextRequest`): native,
on-device, free, fast, excellent on UI text, no plugin process. Alternatives:
RapidOCR (ONNX PaddleOCR, `pip install rapidocr-onnxruntime`), PaddleOCR directly,
EasyOCR, Tesseract.

### 5.2 The OCR contract our code depends on (tiny — easy to reproduce)
`perceive()` consumes exactly this:
```python
elements = [{"text": "...", "centre": [x, y]}, ...]   # centre in imgw×imgh space
```
Any OCR that returns **text + a bounding box** can feed this — compute `centre`
as the box center and scale into `imgw × imgh`. That's the entire adapter.

### 5.3 Why rail anchoring exists
TikTok's right-hand **action rail** (heart/like, speech-bubble/comment,
bookmark/save, share) **slides up and down between videos** depending on caption
length and bottom overlays. Hardcoded or fractional tap points therefore drift —
the infamous "tapped Like but hit Follow / hit the profile / opened search" bugs.

**Key insight:** each rail icon sits directly **above its count number**, and OCR
locates those numbers to the exact pixel. So we anchor icons to the OCR counts.

### 5.4 The algorithm (`ai_agent.py::_ocr_rail_points`) — verbatim
```python
def _ocr_rail_points(self, perception: dict, w: int, h: int) -> dict:
    """Anchor rail icons to the OCR rail as a regular 4-slot grid."""
    if not w or not h:
        return {}
    plus_y = None                 # the follow "+" badge, directly above the like slot
    counts = []                   # [(cy, cx)] of numeric counts on the right edge
    word_slot = {}                # slot idx (2=save,3=share) -> y, for WORD labels
    for e in perception.get("elements", []):
        cx, cy = e["centre"]
        if not (cx > w * 0.85 and h * 0.12 < cy < h * 0.90):   # right-edge band only
            continue
        raw = (e.get("text") or "").strip(); t = raw.lower()
        if raw in ("+", "＋"):              plus_y = int(cy)
        elif self._is_count(raw):          counts.append((int(cy), int(cx)))
        elif "teilen" in t or "share" in t or "senden" in t:   word_slot[3] = int(cy)
        elif "speicher" in t or "favorit" in t or "merken" in t: word_slot[2] = int(cy)
    counts.sort()
    if not counts and plus_y is None and not word_slot:
        return {}

    railx = int(sum(cx for _, cx in counts)/len(counts)) if counts else int(w*0.917)

    # Slot spacing = MEDIAN consecutive gap (robust to a missing count or stray
    # element), only if plausible; else the known default 107 (at 608×1080).
    spacing = 0
    if len(counts) >= 2:
        gaps = sorted(b[0]-a[0] for a, b in zip(counts, counts[1:]))
        med = gaps[len(gaps)//2]
        if 90 <= med <= 125: spacing = med
    if not spacing: spacing = 107
    offset = int(spacing * self.config.rail_icon_offset_factor)   # icon sits this far above its count

    # Like-slot y, anchor priority: follow "+" -> word label -> topmost count.
    like_y = None
    if plus_y is not None:
        below = [cy for cy, _ in counts if cy > plus_y + spacing*0.4]
        like_y = min(below) if below else int(plus_y + spacing)
    elif word_slot:
        slot = max(word_slot)                      # prefer share(3) over save(2)
        like_y = int(word_slot[slot] - slot*spacing)
    elif counts:
        like_y = counts[0][0]
    if like_y is None: return {}

    # Lay a 4-slot grid; snap each slot to a real nearby count for pixel accuracy.
    labels = ["like_button","comment_button","save_button","share_button"]
    points = {}
    for i, label in enumerate(labels):
        slot_y = like_y + i*spacing
        near = [cy for cy, _ in counts if abs(cy-slot_y) <= spacing*0.4]
        if near: slot_y = min(near, key=lambda cy: abs(cy-slot_y))
        icon_y = slot_y - offset
        if h*0.10 < icon_y < h*0.92:
            points[label] = (railx, int(icon_y))
    return points
```
Count detector: `_is_count()` matches `1303`, `54`, `12.3K`, `1,2Mio`, `1.2万`,
`Tsd` etc. Tunables in config: `rail_icon_offset_factor=0.42`,
`rail_icon_offset_px=48`, default `spacing=107`.

### 5.5 Locating fallback chain (`locate_actions`)
1. **OCR anchoring** (`_ocr_rail_points`) — exact, shift-proof. Used if it yields
   a `like_button`.
2. **Vision fallback** — ask Claude (forced tool `locate_controls`) to read the
   rail icons' pixel centres from the screenshot, with OCR text as a hint.
   Sanity-checked to be inside the frame and in the right-hand zone (`x > 0.6w`).
3. Caller then falls back to the static **UI map** (`ui_map.py`) if both fail.

> This entire section is **engine-agnostic and portable**. On Mac you only swap
> *where the `{text, centre}` list comes from* (Apple Vision instead of
> `api.ocr()`); the anchoring math is unchanged.

---

## 6. The agent brain (perceive → decide → act)

`backend/ai_agent.py::TikTokAgent` (1432 lines). Two modes share the perception
layer: a generic **step/run** loop (forced-tool single action) and the
**campaign loop** (the production warm-up driver). Campaign is a **singleton**
(`self._campaign` on one agent) — only one run at a time.

### 6.1 Perception
- `perceive(device_id)` → `{screenshot_b64, elements:[{text,centre}], ocr_available}`
  (or `{error, error_code}` on failure). OCR failure (code 28) degrades
  gracefully to vision-only.
- `settled_perception(device_id, tries=3)` → waits until **two consecutive
  captures agree** (same post fingerprint, 0.45 s apart) before trusting the
  frame. **This is the anti-mirror-lag safeguard** — without it the agent decides
  on stale/transitional frames. Essential with AirPlay; largely unnecessary with
  a low-latency WDA screenshot, but harmless to keep.

### 6.2 Post fingerprinting & dedup (prevents re-liking the same video)
- `_post_words(perception)` → set of caption/username words ≥4 chars, **with
  TikTok UI chrome stripped** (the `UI_STOP` set: furdich, gefolgt, home, profil,
  live, suchen, …) and **all digit tokens dropped** (clock, counts). Without this
  stripping, the nav bar dominates and every video looks identical; the clock
  jitter makes the same video look different.
- `_same_post(a, b)` → Jaccard ≥ 0.45 **AND** ≥ 3 shared words. Fuzzy enough to
  survive OCR jitter between captures.
- `engaged_posts` (last 150 word-sets) → never engage the same post twice a run.

### 6.3 The one-call classifier (`plan_video`)
A **single Claude call** (fast model, `claude-haiku-4-5`) with a **forced tool
`plan_video`** returns, in one shot:
- `on_feed` (bool) — is this a playing full-screen feed video? (vs profile /
  search / sheet / DMs / settings). Carefully prompted: off-topic ≠ off-feed; a
  Follow button/username/counts appear on normal videos too; a profile shows a
  *grid of thumbnails*.
- `screen` (string) — plain-language description of what's on screen.
- `post_type` — `video | photo_slideshow | live | none`.
- `relevant` (bool) — is the **topic** niche-relevant (judged by topic, not by
  whether the creator incidentally has acne)?
- `reason` — a few words.
- `language` — English name of the content's primary language.
- `comment` — a draft (usually superseded by the strong-model comment, §9).

Prompt is assembled from the active niche (`_plan_system`): description, relevant
topics, not-relevant exclusions, comment style, examples, and already-used
comments to avoid repetition.

### 6.4 The campaign decision flow (`_campaign_video`) — exact order
1. Use the **settled frame** handed over by the last advance (`next_perc`) if
   present, else capture a fresh settled frame. Save `last_screenshot` for the UI
   + logs.
2. **On perceive error:** increment `error_streak`; if code 25 (mirror down),
   `recover_mirror()`; **circuit breaker** stops the run after `error_streak >= 6`
   unrecovered (never hammer a dead mirror unattended); else exponential backoff.
3. **Live stream?** (`_looks_live`: `post_type=="live"` or live keywords in the
   screen text) → **swipe past, never tap** (tapping into a live spirals recovery
   into the explore grid). Handled *before* recovery and engagement.
4. **Off feed?** (`on_feed == false`) → `recover()` (Claude picks ONE
   back/close/nav move) + `apply_recovery()`. Escalation ladder:
   `off_feed_streak >= 4` → force hardware Back; `>= 6` → swipe up to leave a
   grid; `>= 8` → **STOP** (never spiral unsupervised).
5. **On feed:** fingerprint the post; compute `already` (dedup) and `off_language`
   (`_language_matches` vs `NICHE["language"]`).
6. **Not relevant / already / off-language** → short watch, `_advance_feed`, done.
7. **New relevant in-language video:** mark engaged **now** (before acting, so no
   double-handling) → watch part of the dwell → `_pick_actions()` decides
   like/save/comment **up front** → if slideshow, browse slides (all if about to
   comment) → `locate_actions()` once for this frame → execute actions in order
   (**comment always last** — it opens the sheet; running it before like/save
   left those taps hitting stale coords) → linger → **always `_advance_feed`**
   (verified, so an ignored swipe can't re-process the same video).

### 6.5 Feed advancement (`_advance_feed`) — never double-swipe
Rapid repeat swipes land mid-transition and glitch the feed back to the same
video. So: try gestures in order, **verifying each against the settled clean
caption**, stopping as soon as the post changes. Hands the already-settled new
frame to the next cycle (`next_perc`) to save a capture.
```python
attempts = [ swipe up 0.75,  swipe up 0.9,  wheel down 0.6 ×5 ]
# after each: wait post_swipe_settle, settled_perception(tries=2),
# if _post_words changed vs current -> advanced, stash next_perc, return
```

### 6.6 Human-like pacing & action selection
- `_watch_relevant()` / `_watch_irrelevant()` — jittered ranges with occasional
  long tails (config §8).
- `_pick_actions(has_comment, comment_allowed)` — independent dice: like (0.85),
  save (0.5), shuffled; **comment appended last** only if allowed and p<0.7.
- `_comment_allowed()` — sparse commenting: only after a randomized cooldown
  (`comment_min_gap` 6–12 relevant videos) and under `comment_max_per_session`
  (8). After each comment, cooldown resets to a fresh random gap.

### 6.7 Slideshow browsing (`_browse_slideshow`)
Detects photo carousels; reads `N/M` indicator (`_slide_count`); swipes through
(direction from config, default `right`) — all slides if about to comment, else a
random subset capped at `slideshow_max_slides` (8), dwelling `slide_dwell` on each.

### 6.8 Recovery (`recover` / `apply_recovery`)
When off-feed, Claude (forced tool `recover`) picks exactly one of: `tap` a
back/close/nav control at a pixel (reusing an OCR `centre` when the control has a
text label), `swipe_down` (dismiss a sheet), or `hardware_back`. Hard rule in the
prompt: **never tap LIVE, a profile, a video, a link, or Follow** — only
navigation. `apply_recovery` executes it.

---

## 7. The niche / content model (`backend/niche.py`)

The entire "what to engage and how to talk" is **data**, not code. `NICHES` is a
registry; `NICHE` is the active dict, **mutated in place** (never reassigned) so
`from niche import NICHE` references stay valid everywhere. `set_active_niche(name)`
does `NICHE.clear(); NICHE.update(...)`. `list_niches()` feeds the per-phone
dropdown.

Each niche dict:
- `name` — e.g. `"NutriSkin"` (also names log folders; spelled exactly in comments).
- `language` — **only engage videos in this language**; off-language skipped;
  comments written in it. `""`/`None` = allow any.
- `description` — product explainer given to the model verbatim.
- `relevant_topics` — list that makes a video worth engaging.
- `not_relevant` — explicit exclusions (incl. "creator incidentally has acne but
  the topic is lifestyle ≠ relevant").
- `comment_style` — style guidance: ONE short casual comment (6–16 words), hint a
  personal skin struggle, slip in NutriSkin tied to a real feature, in the post's
  language, vary every time, at most one emoji, no links/@/hashtags/"check out".
- `comment_examples` — tone/length examples (English, not to be copied).

> **To add a niche:** append a dict of the same shape to `NICHES`. It appears in
> the dropdown automatically. Nothing else changes.

---

## 8. Config reference (`backend/config.py`)

All tunables in one dataclass. Env-overridable where noted.

**iMouse / network**
- `imouse_host` = `127.0.0.1` (`IMOUSE_HOST`), `imouse_port` = `9911`
  (`IMOUSE_PORT` — **never 9912**). `http_base_url` → `http://host:port/api`.

**Anthropic**
- `anthropic_api_key` (`ANTHROPIC_API_KEY`) — never hardcode.
- `anthropic_model` (`ANTHROPIC_MODEL`) default **`claude-haiku-4-5-20251001`** —
  the fast per-frame classifier/locator/recoverer.
- `comment_model` (`ANTHROPIC_COMMENT_MODEL`) default **`claude-sonnet-4-6`** —
  the stronger model used only to write the rare comment.

**OCR / text**
- `ocr_similarity` = 0.8 (lower for accented/stylized Latin).

**Keyboard / text entry**
- `keyboard_swap_zy` (`KEYBOARD_SWAP_ZY`, default **off**) — the phone does NOT
  swap z/y; that was a *laptop* typo during relaying. Only enable if a specific
  phone's typed output actually comes out z↔y swapped. Clipboard paste unaffected.
- `use_clipboard_paste` (`USE_CLIPBOARD_PASTE`, default **on**) — paste flow needs
  the iOS clipboard Shortcut bridge. If not set up, `/shortcut/clipboard/set`
  times out; set `=0` so text entry types directly (transliterated) with no 30 s
  hang.

**TikTok UI fractional anchors** (`tiktok_ui`) — fallback only (icons have no text
label): like `0.92,0.55`; comment `0.92,0.66`; share `0.92,0.77`; video_center
`0.5,0.5`; comment_input `0.5,0.95`; caption_area `{x0.03,y0.72,w0.75,h0.18}`;
back_arrow `0.05,0.055`; home_tab `0.1,0.97`.

**Timing (seconds)**
- `action_delay` 0.3 (between chained taps), `paste_delay` 0.25 (focus→paste),
  `swipe_length` 0.8 (firm flick — too short and TikTok snaps back),
  `pre_tap_settle` 0.1 (pointer at rest before tap), `post_swipe_settle` 0.6
  (pointer settle + laggy mirror catches up).

**Rail targeting** — `rail_icon_offset_factor` 0.42, `rail_icon_offset_px` 48.

**Campaign behaviour** (`campaign`)
- `like_probability` 0.85, `save_probability` 0.5.
- `comment_probability` 0.7 (when eligible), `comment_min_gap` [6,12] relevant
  videos, `comment_max_per_session` 8.
- `watch_relevant` [8,32], `watch_relevant_long` [32,70] @ 0.15 chance;
  `watch_irrelevant` [1,4], `watch_irrelevant_long` [4,7] @ 0.10.
- `between_actions` [0.6,2.2].
- Slideshows: `slideshow_swipe_direction` "right", `slide_dwell` [1.5,4.0],
  `slideshow_max_slides` 8.

---

## 9. Comment system

Two-model design, both via `backend/ai_comments.py` (`AICommentGenerator`,
AsyncAnthropic) and `ai_agent.py::generate_campaign_comment`:
- **Classifier draft** (fast model, inside `plan_video`) — cheap, may be used if
  the strong path fails.
- **Strong comment** (`comment_model`, sonnet) — called **only when actually about
  to comment** (rare, so cost is negligible). Sees the screenshot; writes ONE
  short, native-sounding comment in the detected language, framing NutriSkin as an
  app you *use* (scan foods, track skin, follow recipes); avoids resembling the
  last 8 used comments; strips surrounding quotes.

Text entry (`tiktok_actions.py::enter_text`): **prefer clipboard paste**
(layout-independent, preserves umlauts/emoji, bypasses iOS autocorrect) with a 6 s
timeout; on failure (bridge down) fall back to `/key/sendkey` typing with
**umlaut transliteration** (ä→ae, ö→oe, ü→ue, ß→ss, …) and optional z/y swap.
Posting as a **top-level comment (not a reply)**: `_find_comment_input()` locates
the real "Add comment" input **bar** (`y > 0.82h`, below the comment list) by
placeholder text; the comment icon is only re-tapped when the sheet is **closed**
(re-tapping while open starts a reply).

> **Why clipboard matters:** `/key/sendkey` is ASCII-only and types via scancodes;
> on a German (QWERTZ) phone it swaps z↔y and iOS autocorrect mangled words
> (verändert→vernarrt, entzündungen→entsendungen). Clipboard paste sidesteps all
> of it — but needs an iOS Shortcut bridge that was **not installed** on the ref
> phone (hence the `USE_CLIPBOARD_PASTE=0` fallback). On Mac, WDA can set text
> directly into a field (`/wda/element/.../value`), which is cleaner than either.

---

## 10. Mirror recovery (AirPlay-specific — likely deleted on Mac)

`tiktok_actions.py::recover_mirror`: when a screenshot fails with code 25, drive
the phone's Control Center over USB (`fn_key=ControlBar`), tap the calibrated
**Screen Mirroring** button then the **PC receiver** (`iMouseXP-56`), wait ~3.5 s
for AirPlay to re-establish, then **close with swipe-down (NOT the Home button)**,
and verify via `screenshot_works()`. Coordinates come from the UI map
(`control_center.screen_mirroring` / `airplay_receiver`), calibrated once while
the mirror was up. Returns False (so the caller stops cleanly) if uncalibrated.

> **Mac implication:** with WDA screenshots over USB there is no AirPlay mirror,
> so **this entire file section and the calibration dance go away.** That's a big
> simplification.

---

## 11. Known bugs, root causes & fixes (don't re-introduce these)

| Symptom | Root cause | Fix |
|---|---|---|
| Taps land short / wrong | Relative-HID pointer still travelling or asleep | Phone tracking speed max; no AssistiveTouch auto-hide; console "Cast to Specific Hardware" (15-and-above mouse params, no ≤14.2); `pre_tap_settle`/`post_swipe_settle` |
| "Like" hit Follow / profile / search | Rail shifts per video; fixed coords drift | OCR count-anchored rail grid (`_ocr_rail_points`) |
| **Feed won't advance / stuck on same video** | **`WIN+h` (Home) in mirror-close was exiting TikTok to iOS home** | Replaced all `WIN+h` closes with **swipe-down** |
| Different videos looked identical / same looked different | Nav bar + clock polluted the post fingerprint | Strip `UI_STOP` words + all digit tokens in `_post_words` |
| Decided on wrong/stale screen | AirPlay mirror lags phone 1–2 s | `settled_perception` (two agreeing captures) |
| Re-liking/unliking same video | No cross-cycle dedup | `engaged_posts` + `_same_post`, mark engaged *before* acting |
| Tapping into a live → spiraled into explore grid | Live handled as normal/off-feed | `_looks_live` → always swipe past, before recovery/engagement |
| Comment posted as a **reply**, not standalone | Re-tapped comment icon while sheet open; typed into a comment row | `_find_comment_input` (bottom input bar) + only re-tap icon when sheet closed |
| German comment under English post | Language only from caption | `language` from `plan_video` + `_language_matches` gate |
| Comment text corrupted (umlauts) | ASCII `/key/sendkey` + iOS autocorrect | Clipboard paste; else transliterate |
| Clipboard flow hung ~30 s | iOS Shortcut bridge not installed | 6 s timeout + `USE_CLIPBOARD_PASTE=0` |
| **Control button didn't tap** (grid UI) | **Device IDs contain `:` → invalid CSS id selector** in `querySelector('#screen-A4:B8…')` | Use `getElementById`; scale click to `imgw×imgh` |
| Unattended spiral off-feed | No circuit breaker | Stop at `off_feed_streak>=8` / `error_streak>=6` |

---

## 12. File-by-file map

**Backend (`backend/`)**
- `api_client.py` (322) — **iMouse HTTP client.** The I/O contract to replace. §3.
- `device_manager.py` (174) — device discovery + **`imgw×imgh` coordinate model**. §4.
- `config.py` (154) — all tunables + env. §8.
- `niche.py` (112) — niche/content registry. §7.
- `ai_comments.py` (203) — Claude vision analysis + comment/caption generation. §9.
- `ai_agent.py` (1432) — **the brain**: perceive/decide/act, OCR rail anchoring,
  campaign loop, recovery, slideshow, pacing. §5, §6.
- `tiktok_actions.py` (580) — high-level actions (like/save/comment/swipe), text
  entry, mirror recovery, dismiss logic. §9, §10.
- `main.py` (904) — FastAPI app + all HTTP endpoints + static serving. §A.
- `campaign_log.py` (134) — per-run persistent logs (meta/log.jsonl/frames/
  summary) + `list_sessions` + `read_session`. §B.
- `ui_map.py` (57) — persistent per-screen tap-point map (`ui_map.json`), the
  "learning doc" of this phone's TikTok layout.
- `selftest.py` (418) — button self-test harness. §C.
- `comment_test.py` (175) — comment-flow test (top-level vs reply; never posts). §C.
- `requirements.txt`, `.env.example` — deps + env template. §C.

**Frontend (`frontend/`)**
- `index.html` (258), `app.js` (1251, class `TikTokController`), `styles.css` (841)
  — the two-screen UI (phone grid + developer panels). §D.

**Root:** `README.md`, run scripts (`run.sh`/`run.bat`/`_start.bat`/`selftest.bat`/
`comment_test.bat`), `tiktok-controller.html`, `MAC_PORT_HANDOFF.md` (this file).

### §A. Backend HTTP API (`main.py`, FastAPI, `0.0.0.0:8000`)

CORS wide open. Module-level singletons `client / registry / controller / agent`
built in `lifespan`; `campaign_task` is a single global `asyncio.Task` (one
campaign at a time — enforced by a 409 on `/api/campaign/start`). `main.py` is a
**thin HTTP layer** over the controller/agent — it has *no* iMouse coordinate math
except a JPEG size probe; the device coupling is all in `api_client.py` +
`device_manager.py`.

**Diagnostics:** `GET /api/health`; `GET /api/debug/connection` (probe console +
list devices); `POST /api/debug/raw-command` `{fun,data?}` (send arbitrary iMouse
`fun`); `GET /api/debug/ocr` (is PaddleOCR up? code 28 = no); `POST
/api/debug/swipe-test` (try 6 gestures, report which advanced the feed); `GET
/api/debug/rail` (dump rail-locator view of current frame); `POST
/api/debug/clipboard-test` (umlaut round-trip through the clipboard bridge); `GET
/api/debug/screenshot-info` & `GET /api/devices/{id}/screenshot-info` (compare real
JPEG size vs `imgw/imgh` — **keep an equivalent on Mac to catch coord-space
mismatches**).

**Config:** `GET/POST /api/config` (`imouse_host/port, anthropic_api_key/model`);
`POST /api/config/ui` `{element,x,y,w?,h?}` (calibrate a fractional UI anchor).

**Devices:** `GET /api/devices` (→ `Device.to_dict()` list; 502 on iMouse error);
`GET /api/devices/{id}/screenshot` (b64 JPEG); `GET /api/devices/{id}/analyze`
(vision+OCR video read); `GET /api/devices/{id}/screenshot-info`.

**Actions** (all return `{success,message,...}`): `POST
/api/devices/{id}/swipe/next|swipe/previous|like|save|follow`;
`POST /api/devices/{id}/comment` (`CommentRequest`);
`POST /api/devices/{id}/generate-comments`; `POST /api/devices/{id}/auto-browse`;
`POST /api/batch/action` `{device_ids,action}`; `POST /api/devices/{id}/home`
(`fn_key=WIN+h`). Manual like/save/comment are **vision-anchored** via
`_rail_point(id,label) → agent.locate_actions()` so they hit the same spot the
campaign does (not stale config fractions):
```python
async def _rail_point(device_id: str, label: str):
    try:
        pts = await agent.locate_actions(device_id)
        return pts.get(label)
    except Exception:
        return None
```

**Tap / coordinate:** `POST /api/devices/{id}/tap` `{x,y}` → routes through the
**settled click path** (`controller._click`, same as the campaign, incl.
`pre_tap_settle`).

**Mirror / AirPlay:** `POST /api/mirror/calibrate` (learn Control Center mirror
button + receiver row, once, while mirror is up — uses `agent.locate_one` vision);
`POST /api/mirror/open-cc` / `close-cc` (swipe-down, not Home); `GET
/api/mirror/status` (`online, mirror_up, recast_calibrated`). *(All likely deleted
on Mac — no AirPlay.)*

**Keyboard:** `POST /api/devices/{id}/type` `{text}`; `POST /api/devices/{id}/key`
`{key?,fn_key?}`.

**Agent / explorer:** `POST /api/devices/{id}/ai/step` & `/ai/run`
(`AIAgentRequest {goal?,max_steps}`); `POST /api/devices/{id}/analyze-ui`;
`GET/POST /api/ui-map` (`UIMapSave {screen_type,elements,imgw,imgh}`).

**Campaign / niches:** `POST /api/campaign/start`
(`CampaignRequest {device_id,duration_minutes,language?,niche?}`; 409 if running);
`POST /api/campaign/stop`; `GET /api/campaign/status`; `GET /api/campaign/sessions`
(saved runs); `GET /api/niches` (`{niches,active}`).

**Logs / serving / WS:** `GET /logs-view/{name}` (HTML review gallery); `GET /`
(index.html); mounts `/static` (frontend) & `/logs` (campaign_logs); **WebSocket
`/ws`** — client sends `{action:"start_stream",device_id,interval}`; server pushes
**raw binary JPEG** frames (the dev single-device live view; the grid uses HTTP
polling instead).

> **Mac port of `main.py`:** essentially unchanged. The only things to revisit are
> the `fn_key` strings (`ControlBar`/`WIN+h`/`WIN+v`), the mirror endpoints
> (delete), and the `pre_tap_settle` guard (may be unnecessary with WDA). All
> coordinate logic stays in `device_manager.to_pixels`.

### §B. Run logging (`campaign_log.py`)

Every campaign run is persisted to `backend/campaign_logs/<YYYYMMDD-HHMMSS>_<niche>_<device6>/`:
- `meta.json` — started, device, niche.
- `log.jsonl` — one line per cycle: `{i, action/screen/relevant/reason/comment/
  actions/errors/watch/t, frame}`.
- `frames/NNNN.jpg` — the screenshot each decision was made on (base64-decoded
  from `last_screenshot`).
- `summary.json` — final stats, written on run end.

`SessionLogger(device_id, niche)` is created in `campaign_loop`, `.log(entry,
screenshot_b64)` each cycle, `.finish(stats)` in `finally`. `list_sessions()`
(newest-first, with stats/running) powers `GET /api/campaign/sessions` and the dev
**Saved Runs** panel. `read_session(name)` powers the `/logs-view/<name>` gallery
(each cycle's decision next to its frame). All logging is wrapped in try/except so
it never breaks a run. `campaign_logs/` is gitignored. **Portable unchanged.**

### §C. Ops, scripts & tests

**Deps** (`backend/requirements.txt`, all pure-Python, cross-platform):
`fastapi>=0.109`, `uvicorn[standard]>=0.27`, `httpx>=0.26`, `anthropic>=0.40`,
`pydantic>=2.5`, `python-dotenv>=1.0`, `pillow>=10.0`.

**Env** (`backend/.env.example` → `.env`, gitignored): `IMOUSE_HOST=127.0.0.1`,
`IMOUSE_PORT=9911` (**never 9912**), `ANTHROPIC_API_KEY=sk-ant-...`,
`ANTHROPIC_MODEL=claude-haiku-4-5-20251001`. (Also `ANTHROPIC_COMMENT_MODEL`,
`KEYBOARD_SWAP_ZY`, `USE_CLIPBOARD_PASTE` — see §8.)

**Launch:**
- **macOS/Linux:** `./run.sh` — cds to `backend/`, creates/activates `venv`, pip
  installs, exports `IMOUSE_HOST/PORT`, runs `python -m uvicorn main:app --host
  0.0.0.0 --port 8000 --reload`. **This already works on Mac** (the Python app was
  always cross-platform).
- Windows: `run.bat` → `_start.bat` (richer Python-discovery + venv + import
  checks; no `--reload`).

**Tests** (`backend/`, run on the machine the phone is attached to, TikTok on the
For You feed):
- `selftest.py [rounds] [buttons]` (default 15, `like,save,comment,share`) —
  self-grading button test: computes rail points via `_ocr_rail_points`, taps each,
  **verifies with no human** (samples heart/bookmark pixel colour for like/save;
  confirms the right bottom sheet opened for comment/share; toggles un-like/un-save
  to leave the account untouched). Writes `selftest_report.json`. Uses Pillow.
- `comment_test.py [rounds]` (default 6) — opens the comment sheet like the
  campaign and uses a Claude vision tool-call to classify **top-level vs reply**
  composer (reads placeholder text). **Never posts.** Writes
  `comment_test_report.json`.
- ⚠️ **No `.sh` runners exist** for these — only `selftest.bat`/`comment_test.bat`.
  On Mac, create one-liners: `backend/venv/bin/python selftest.py …`.

**Other:** `tiktok-controller.html` (root) is an **old standalone single-file
prototype**, kept for design reference only — not part of the shipping app.

### §D. Frontend (`frontend/`: vanilla JS, no build)

Single class `TikTokController` (`window.app`), static `index.html` with fixed IDs,
dark theme CSS. Served under `/static/`; API base `/api`.

**Two screens** toggled by flipping the `hidden` attribute (no router):
- `#grid-view` (default) — operator view.
- `#dev-view` (`hidden`) — all advanced panels stacked.
```js
toggleDevView(show) {
  document.getElementById('grid-view').hidden = show;
  document.getElementById('dev-view').hidden = !show;
  if (!show) this.renderGrid(); else this.loadRuns();
}
```
> ⚠️ **Load-bearing CSS:** `main{display:flex}` would beat `[hidden]`, so there's
> an explicit `main[hidden]{display:none}` — preserve it or both screens show.

**Phone grid** (`renderGrid`): one `.phone-card` per **online** device, each with a
live screenshot (`#screen-<id>`), niche `<select>` (options from `GET /api/niches`),
and **Warm up / Home screen / Control** buttons. All handled by **event delegation**
on `#phone-grid` (cards are rebuilt via `innerHTML`). Live preview = **HTTP polling
`GET /api/devices/<id>/screenshot` every 1800 ms** into a reused `<img>`
(`startPreview`). Warm up enforces a **one-at-a-time lock** (`activeCampaignDevice`)
→ `POST /api/campaign/start {device_id,duration_minutes:240,niche}`;
`startGridStatusPolling` reconciles with `GET /api/campaign/status` every 3 s so a
running warm-up survives refresh.

**Control (tap-to-operate):** `gridToggleControl` flips `_grid.control[id]` (green
outline + crosshair); then `gridScreenTap` maps a click on the screenshot to a real
device tap — normalize within the rendered `<img>` rect, scale to device pixels,
`POST /api/devices/<id>/tap {x,y}`:
```js
const w = dev.imgw || dev.width || 608, h = dev.imgh || dev.height || 1080;
const x = Math.round((e.clientX - rect.left) / rect.width  * w);
const y = Math.round((e.clientY - rect.top)  / rect.height * h);
```
> ⚠️ **Device IDs contain `:`** → invalid in a CSS `#id` selector, so **all
> `screen-<id>` lookups use `getElementById`, never `querySelector('#...')`.**
> This same rect-normalization math is reused by the dev Tap-test, Explorer, and
> Mirror calibration. Fallback `608×1080` appears throughout.

**Developer panels** (each a `<section>` in `#dev-view`): **device** (list +
select, Ctrl-click multi-select → batch), **control** (single-device WebSocket
`/ws` live stream + Tap-test), **actions** (Quick Actions + batch + keyboard
shortcuts), **campaign** (detailed: Language + Minutes, live stats/log),
**agent** (free-form goal, step/run), **comment** (generate/post, needs API key),
**analysis** (analyze current video), **explorer** (Interface Explorer: vision-map
controls → dots → edit → `POST /api/ui-map`), **mirror** (re-cast calibration),
**keyboard** (manual type + `ENTER/BACKSPACE/WIN+h/TAB+b`), **saved-runs** (runs
list → `/logs-view/<run>`). Plus a global **Settings modal** (host/port/key/model +
UI calibration).

**Known loose ends (carry or fix in the port):** `#runs-refresh` button has no
listener (runs refresh only on entering dev view); `WIN+v`/`WIN+h` hotkeys need Mac
equivalents (`CMD+v`, WDA homescreen); `navigator.clipboard` needs a secure context
(`localhost` is fine).

> **Mac port of frontend:** unchanged — it only speaks `/api` + `/ws`. Keep it in a
> browser, or wrap in a WKWebView/Electron shell over the same backend.

---

## 13. The Mac port plan

### 13.1 Strategy
Replace **only** `api_client.py` (and delete the AirPlay bits). Everything above
it — `ai_agent.py`, `tiktok_actions.py` (minus `recover_mirror`),
`device_manager.py` concept, `niche.py`, `config.py`, `ai_comments.py`,
`campaign_log.py`, the whole web UI — stays. Keep the **public method signatures**
of `IMouseClient` identical so nothing downstream changes.

### 13.2 Control + capture layer options on Mac
| Target | Control + screenshot | Notes |
|---|---|---|
| **iPhone** (recommended) | **WebDriverAgent** via `facebook-wda` (Python) or Appium | One USB channel does taps, swipes, text, AND screenshots. **Kills the AirPlay fragility, mirror recovery, and most settle/lag issues.** Needs Xcode + a provisioning profile to build WDA onto the device. |
| iPhone (capture only) | `idevicescreenshot` (libimobiledevice) | Screenshots only; pair with WDA for control. |
| **Android** | `adb` taps/swipes + `adb exec-out screencap`, or **scrcpy** | Simplest of all; no signing. If any devices are Android, this is the fastest path. |

**Recommended mapping (`IMouseClient` → WDA):**
- `get_devices()` → list via `wda`/usbmux; `imgw×imgh` = WDA window size.
- `screenshot()` → `GET /screenshot` (base64 PNG) → decode to bytes.
- `click(x,y)` → WDA `/wda/tap` (or W3C actions). **Coords in WDA's point space —
  make that your `imgw×imgh`.**
- `swipe(dir,len)` → WDA `/wda/dragfromtoforduration` (compute from/to from `len`).
- `send_key(key)` → WDA `/wda/keys` (types Unicode directly — no ASCII limit).
- `send_key(fn_key)` → map: Home → WDA `/wda/homescreen` (⚠️ same "exits app"
  caveat — avoid mid-session); Back → app-specific swipe/gesture; Paste/Enter →
  WDA keys. Re-audit every `fn_key` call site against the §3.3 table.
- `clipboard_set` → WDA `/wda/setPasteboard`, **or skip entirely** and set text
  into the focused element's `value` (cleaner; no bridge).
- `ocr()` → **Apple Vision** adapter returning `{text, centre}` (§5.2). This is a
  *local* call, not part of WDA.

### 13.3 OCR adapter on Mac (Apple Vision) — target shape
```python
# mac_ocr.py — same contract as api_client.ocr(): returns objects with
# .items == [{"text","centre":[x,y]}] in the imgw×imgh (WDA point) space.
import Vision, Quartz   # pip install pyobjc-framework-Vision pyobjc-framework-Quartz
# 1) load the screenshot into a CGImage
# 2) VNRecognizeTextRequest (usesLanguageCorrection, recognitionLevel=.accurate)
# 3) for each observation: bbox is normalized, origin BOTTOM-left ->
#    cx = bbox.midX * imgw ;  cy = (1 - bbox.midY) * imgh
# 4) return [{"text": obs.topCandidate.string, "centre": [int(cx), int(cy)]}]
```
Add a `OCR_BACKEND = vision | imouse` config switch so one codebase runs on both.

### 13.4 Suggested order of work
1. **Stand up WDA** against one iPhone; confirm tap + screenshot by hand.
2. Write `mac_device_io.py` implementing the `IMouseClient` public API against WDA
   (tap/swipe/screenshot/keys/device-list). Leave `ocr()` raising until step 4.
3. Point `config.py` / the controller at the new backend behind a flag.
4. Write `mac_ocr.py` (Apple Vision) + wire `perceive()` to it. **Now the rail
   anchoring and the whole agent run unchanged.**
5. Delete/disable `recover_mirror` + mirror endpoints (no AirPlay). Re-audit
   `fn_key` usages (§3.3).
6. Run `selftest.py` against TikTok to re-validate like/save/comment/swipe on the
   new coordinate space; recalibrate `tiktok_ui` fractions if needed.
7. Run a short campaign; review the saved run gallery (`/logs-view/<run>`).

### 13.5 What you keep for free (no changes)
The OCR rail-anchoring math, settled-perception, dedup/fingerprinting, the
relevance+language+live+pacing campaign logic, the niche content model, the
two-model comment system, run logging, and the entire web UI. That's ~85% of the
code and **all** of the hard-won behavior.

### 13.6 Legal note
iMouse XP is proprietary Windows software; **do not decompile or lift its binary/
source.** You don't need to — its OCR is PaddleOCR (open), and its control layer
is replaced wholesale by WDA/Appium/adb. Everything valuable here is already in
*our* code or in open-source engines.

---

## 14. Secrets & hygiene
- `backend/.env` is gitignored (holds `ANTHROPIC_API_KEY`). **Never commit it.**
- `backend/campaign_logs/` is gitignored (runtime run data + screenshots).
- ⚠️ The Anthropic API key was pasted in plaintext during development — **rotate
  it** before/while porting.
