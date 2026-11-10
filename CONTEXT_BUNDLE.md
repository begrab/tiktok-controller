# TikTok AI Controller — Complete Context Bundle

> **This single file contains EVERYTHING needed to understand and rebuild this
> project.** It is: (1) the full Mac-port handoff narrative, followed by (2) the
> complete verbatim source of every file in the codebase. Attach this one file to
> a new chat and it has the whole system — architecture, the iMouse/OCR internals,
> the agent brain, and all the code.
>
> Generated from branch `claude/ai-phone-controller-setup-9y1rl9`.
> Runtime data (`backend/campaign_logs/`) and secrets (`backend/.env`) are excluded.

---

# PART 1 — HANDOFF NARRATIVE

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


---

# PART 2 — FULL SOURCE CODE


## `backend/config.py`

```python
"""Configuration for the TikTok controller.

Defaults match the verified iMouse XP setup: the console's local API is on
``127.0.0.1:9911`` (port 9912, seen in some third-party guides, is wrong) and
there is no API key -- auth is being on localhost.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Optional: load a .env file if python-dotenv is installed.
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:  # python-dotenv not installed -- rely on real env vars.
    pass


# =============================================================================
# EDIT THESE FOR YOUR SETUP
# =============================================================================
IMOUSE_HOST = "127.0.0.1"   # iMouse console (localhost if on the same machine)
IMOUSE_PORT = 9911          # verified iMouse XP API port -- do NOT use 9912
# =============================================================================

# Default Claude model. Cheap and fast for per-screenshot calls; the handover
# lists claude-sonnet-4-6 and claude-opus-4-8 as more capable alternatives.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class Config:
    # iMouse XP local API.
    imouse_host: str = os.getenv("IMOUSE_HOST", IMOUSE_HOST)
    imouse_port: int = int(os.getenv("IMOUSE_PORT", IMOUSE_PORT))

    # Anthropic (only needed when an LLM is in the loop). Read from the
    # environment; never hardcode a key.
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)
    # Comments are rare and matter, so write them with a stronger model than the
    # fast per-frame classifier. Override with ANTHROPIC_COMMENT_MODEL.
    comment_model: str = os.getenv("ANTHROPIC_COMMENT_MODEL", "claude-sonnet-4-6")

    # OCR / find-text tuning. The bundled model is tuned for Chinese and
    # English; lower this for accented or stylised Latin text.
    ocr_similarity: float = 0.8

    # Off by default: the phone does NOT swap z/y (that was a laptop typo when
    # relaying). Only enable (KEYBOARD_SWAP_ZY=1) if a specific phone's typed
    # output actually comes out with z<->y swapped. Clipboard paste is unaffected.
    keyboard_swap_zy: bool = os.getenv("KEYBOARD_SWAP_ZY", "0") == "1"

    # Clipboard paste needs the iOS clipboard Shortcut bridge. If it isn't set up
    # (/shortcut/clipboard/set times out), set USE_CLIPBOARD_PASTE=0 so text
    # entry skips it and types directly (transliterated) -- no 30s hang.
    use_clipboard_paste: bool = os.getenv("USE_CLIPBOARD_PASTE", "1") == "1"

    # Approximate TikTok UI anchors as FRACTIONS of the imgw x imgh coordinate
    # space. Icons have no text label, so find-text can't locate them -- these
    # are the fallback and can be calibrated per device from the UI. Anything
    # with a text label (Follow, Post) is located with find-text instead.
    tiktok_ui: dict = field(
        default_factory=lambda: {
            "like_button": {"x": 0.92, "y": 0.55},
            "comment_button": {"x": 0.92, "y": 0.66},
            "share_button": {"x": 0.92, "y": 0.77},
            "video_center": {"x": 0.5, "y": 0.5},
            "comment_input": {"x": 0.5, "y": 0.95},
            "caption_area": {"x": 0.03, "y": 0.72, "w": 0.75, "h": 0.18},
            # Recovery anchors. back_arrow is the universal top-left "go back"
            # button on search/profile/detail screens; home_tab is the Home tab
            # on the bottom bar. Calibrate if taps miss.
            "back_arrow": {"x": 0.05, "y": 0.055},
            "home_tab": {"x": 0.1, "y": 0.97},
        }
    )

    # Timing (seconds).
    action_delay: float = 0.3      # settle time between chained taps
    paste_delay: float = 0.25      # focus -> paste settle time
    swipe_length: float = 0.8      # fraction of screen per swipe -- a firm flick;
                                   # too short and TikTok snaps back to the same video

    # Relative-mouse pointer settling. The board is a RELATIVE HID mouse: the
    # console turns an absolute (x, y) into a move-from-current-position then a
    # click. If the pointer is still travelling (tracking speed) or was asleep
    # (AssistiveTouch auto-hide), the click fires early and lands short. These
    # let the pointer come to rest before we tap. The real fix is phone/console
    # settings (tracking speed at max, no auto-hide) -- these just stop us from
    # racing the pointer between our own commands.
    pre_tap_settle: float = 0.1    # pause before every tap so the pointer is at rest
    post_swipe_settle: float = 0.6   # rest after a swipe: pointer settles AND the
                                     # laggy mirror catches up to the new video

    # Right-rail icon targeting. Each rail icon (heart/comment/bookmark/share)
    # sits directly ABOVE its count number, which OCR locates to the exact pixel.
    # The icon is this fraction of the count-to-count spacing above its count; if
    # spacing can't be measured (one count only), fall back to the absolute px.
    rail_icon_offset_factor: float = 0.42
    rail_icon_offset_px: int = 48

    # Niche campaign behaviour. Engagement is randomised per relevant video, so
    # combinations vary naturally; watch times are jittered with occasional long
    # tails so the pacing reads as human, not on a fixed timer.
    campaign: dict = field(
        default_factory=lambda: {
            # Independent per-action chances on a RELEVANT video.
            "like_probability": 0.85,
            "save_probability": 0.5,
            # Commenting is deliberately SPARSE so it never looks spammy: a
            # comment is only considered once the cooldown has passed (this many
            # relevant videos since the last comment, randomized), then fires with
            # this probability, and never more than the per-run cap. Comments are
            # always unique, personal-struggle stories (see niche.comment_style).
            "comment_probability": 0.7,       # chance WHEN eligible (cooldown passed)
            "comment_min_gap": [6, 12],        # relevant videos between comments
            "comment_max_per_session": 8,      # hard cap per campaign run
            # Watch time (seconds) ranges: [min, max]; a "long" tail fires with
            # its chance for the occasional deep-watch / longer glance.
            "watch_relevant": [8.0, 32.0],
            "watch_relevant_long": [32.0, 70.0],
            "watch_relevant_long_chance": 0.15,
            "watch_irrelevant": [1.0, 4.0],
            "watch_irrelevant_long": [4.0, 7.0],
            "watch_irrelevant_long_chance": 0.10,
            # Small human gap between chained engagement actions.
            "between_actions": [0.6, 2.2],
            # Photo slideshows: browse some slides (all when commenting). Swipe
            # direction to advance a slide -- flip to "left" if slides go backward.
            "slideshow_swipe_direction": "right",
            "slide_dwell": [1.5, 4.0],       # pause on each slide
            "slideshow_max_slides": 8,       # cap when the total is unknown
        }
    )

    @property
    def http_base_url(self) -> str:
        return f"http://{self.imouse_host}:{self.imouse_port}/api"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            imouse_host=os.getenv("IMOUSE_HOST", IMOUSE_HOST),
            imouse_port=int(os.getenv("IMOUSE_PORT", IMOUSE_PORT)),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
        )

```

## `backend/api_client.py`

```python
"""iMouse XP API client.

Thin async wrapper over the iMouse XP console's local HTTP API, matching the
verified behaviour documented in the technical handover:

  * Base URL is ``http://127.0.0.1:9911/api``. There is no API key; auth is
    "being on localhost". Port 9912 (seen in some third-party guides) is wrong.
  * Every request is ``POST {"fun": <endpoint>, "data": {...}}``.
  * A call succeeded only if HTTP ``status == 200`` AND ``data.code == 0``.
    An HTTP 200 with a non-zero ``data.code`` is a failure -- check both.
  * ``/pic/screenshot`` with ``binary: true`` returns raw JPEG bytes, not JSON.
    Branch on the response content type before parsing.

The WebSocket interface is intentionally not used: the handover measured local
HTTP calls at ~70 ms and concluded the WebSocket is not worth the added
complexity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from config import Config

logger = logging.getLogger("imouse")


# Error codes documented in the handover / API overview.
ERROR_MESSAGES = {
    0: "success",
    28: "plugin not connected (the PaddleOCR console process is not running)",
}


class IMouseError(RuntimeError):
    """Raised when an iMouse call fails (transport, HTTP, or ``data.code != 0``)."""

    def __init__(self, fun: str, code: int, message: str, status: Optional[int] = None):
        self.fun = fun
        self.code = code
        self.message = message
        self.status = status
        hint = ERROR_MESSAGES.get(code)
        detail = f"{message}" if message else ""
        if hint and hint not in detail:
            detail = f"{detail} ({hint})".strip()
        super().__init__(f"{fun} failed [code={code}]: {detail}")


@dataclass
class ApiResult:
    """Successful result of an iMouse call.

    ``data`` is the inner ``data`` object from the response (containing ``code``,
    ``message`` and usually ``list``). ``items`` is a convenience view of
    ``data['list']`` when present.
    """

    fun: str
    data: dict

    @property
    def items(self) -> list:
        value = self.data.get("list")
        return value if isinstance(value, list) else []

    @property
    def message(self) -> str:
        return self.data.get("message", "")


class IMouseClient:
    """Async HTTP client for the iMouse XP local API."""

    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.http_base_url
        self._client: Optional[httpx.AsyncClient] = None
        logger.info("IMouseClient initialised with base_url: %s", self.base_url)

    async def __aenter__(self) -> "IMouseClient":
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    # ------------------------------------------------------------------ core --
    async def _post(self, fun: str, data: Optional[dict] = None) -> httpx.Response:
        payload = {"fun": fun, "data": data or {}}
        logger.debug("POST %s <- %s", fun, payload)
        response = await self.client.post(self.base_url, json=payload)
        response.raise_for_status()
        return response

    async def call(self, fun: str, **data) -> ApiResult:
        """Make a JSON API call. Raises :class:`IMouseError` on any failure.

        Do not use this for ``/pic/screenshot`` with ``binary: true`` -- that
        returns raw bytes; use :meth:`screenshot` instead.
        """
        try:
            response = await self._post(fun, data)
        except httpx.HTTPError as exc:
            raise IMouseError(fun, -1, f"transport error: {exc}") from exc

        body = response.json()
        status = body.get("status")
        inner = body.get("data") or {}
        code = inner.get("code")

        if status != 200 or code != 0:
            raise IMouseError(
                fun,
                code if code is not None else -1,
                inner.get("message", ""),
                status=status,
            )

        logger.debug("%s ok", fun)
        return ApiResult(fun=fun, data=inner)

    # ------------------------------------------------------------- devices ----
    async def get_devices(self) -> list[dict]:
        """Return every device the console has ever seen (``/device/get``).

        Filter on ``state == 1`` for devices that are currently online. All tap
        coordinates live in the ``imgw x imgh`` space reported per device.
        """
        result = await self.call("/device/get")
        return result.items

    # ----------------------------------------------------------- screenshots --
    async def screenshot(
        self,
        device_id: str,
        *,
        binary: bool = True,
        rect: Optional[list[int]] = None,
        save_path: Optional[str] = None,
    ) -> bytes:
        """Capture the mirrored screen as JPEG bytes (``/pic/screenshot``).

        With ``binary=True`` the API returns raw JPEG bytes rather than JSON, so
        this method branches on the response content type. With ``binary=False``
        the JPEG comes back base64-encoded inside JSON; we decode it so callers
        always receive bytes.
        """
        data: dict[str, Any] = {"id": device_id, "jpg": True, "binary": binary}
        if rect:
            data["rect"] = rect
        if save_path:
            data["save_path"] = save_path

        try:
            response = await self._post("/pic/screenshot", data)
        except httpx.HTTPError as exc:
            raise IMouseError("/pic/screenshot", -1, f"transport error: {exc}") from exc

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response.content  # raw JPEG bytes

        # JSON path: either a base64 image, or an error we must surface.
        body = response.json()
        inner = body.get("data") or {}
        if body.get("status") != 200 or inner.get("code") != 0:
            raise IMouseError(
                "/pic/screenshot",
                inner.get("code", -1),
                inner.get("message", ""),
                status=body.get("status"),
            )
        import base64

        b64 = inner.get("img") or inner.get("data") or ""
        return base64.b64decode(b64) if b64 else b""

    # -------------------------------------------------------------- input -----
    async def click(self, device_id: str, x: int, y: int) -> ApiResult:
        """Tap at ``(x, y)`` in the device's ``imgw x imgh`` coordinate space."""
        return await self.call("/mouse/click", id=device_id, x=int(x), y=int(y))

    async def swipe(
        self, device_id: str, direction: str = "up", length: float = 0.5
    ) -> ApiResult:
        """Swipe ``up``/``down``/``left``/``right``; ``length`` is a screen fraction."""
        return await self.call(
            "/mouse/swipe", id=device_id, direction=direction, len=length
        )

    async def wheel(
        self, device_id: str, direction: str = "up", length: float = 0.5, number: int = 1
    ) -> ApiResult:
        return await self.call(
            "/mouse/wheel",
            id=device_id,
            direction=direction,
            len=length,
            number=number,
        )

    # ------------------------------------------------------------- keyboard ---
    async def send_key(
        self,
        device_id: str,
        key: Optional[str] = None,
        fn_key: Optional[str] = None,
    ) -> ApiResult:
        """Send keyboard input (``/key/sendkey``).

        ``key`` is ASCII only -- accented characters and emoji are dropped. For
        non-ASCII text use :meth:`clipboard_set` then paste with ``fn_key='WIN+v'``.
        ``fn_key`` triggers a hotkey (e.g. ``WIN+h`` Home, ``TAB+b`` Back,
        ``ENTER``, ``BACKSPACE``, ``WIN+v`` Paste).
        """
        data: dict[str, Any] = {"id": device_id}
        if key is not None:
            data["key"] = key
        if fn_key is not None:
            data["fn_key"] = fn_key
        return await self.call("/key/sendkey", **data)

    # -------------------------------------------------------------- vision ----
    async def ocr(
        self,
        device_id: str,
        rect: Optional[list[int]] = None,
        enhanced: bool = False,
    ) -> ApiResult:
        """OCR the screen (``/pic/ocr`` or the heavier ``/pic/ocr-ex``).

        Each result carries ``centre: [x, y]`` (tap target) and ``rect``.
        Returns code 28 until the PaddleOCR plugin process is running.
        ``rect`` crops the processed region to cut OCR time.
        """
        data: dict[str, Any] = {"id": device_id}
        if rect:
            data["rect"] = rect
        return await self.call("/pic/ocr-ex" if enhanced else "/pic/ocr", **data)

    async def find_text(
        self,
        device_id: str,
        text: list[str],
        similarity: float = 0.8,
        contain: bool = True,
        rect: Optional[list[int]] = None,
    ) -> list[dict]:
        """Locate on-screen text and return matches with a tap ``centre``.

        Preferred over hardcoded coordinates for anything with a text label.
        Lower ``similarity`` for accented/stylised type -- the bundled model is
        tuned for Chinese and English.
        """
        data: dict[str, Any] = {
            "id": device_id,
            "text": text,
            "similarity": similarity,
            "contain": contain,
        }
        if rect:
            data["rect"] = rect
        result = await self.call("/pic/find-text", **data)
        return result.items

    async def find_image(
        self,
        device_id: str,
        image_base64: str,
        similarity: float = 0.9,
        rect: Optional[list[int]] = None,
    ) -> list[dict]:
        """Template-match an icon (``/pic/find-image``).

        Requires the OpenCV plugin, which the handover notes is not installed on
        the reference machine -- expect a failure until it is.
        """
        data: dict[str, Any] = {
            "id": device_id,
            "img": image_base64,
            "similarity": similarity,
        }
        if rect:
            data["rect"] = rect
        result = await self.call("/pic/find-image", **data)
        return result.items

    # ----------------------------------------------------------- shortcuts ----
    async def clipboard_set(self, device_id: str, text: str, sleep: int = 0) -> ApiResult:
        """Push UTF-8 text to the phone clipboard (``/shortcut/clipboard/set``).

        The reliable way to enter non-ASCII text: set the clipboard, focus the
        field, brief pause, then send ``fn_key='WIN+v'``.
        """
        return await self.call(
            "/shortcut/clipboard/set", id=device_id, text=text, sleep=sleep
        )

    async def clipboard_get(self, device_id: str) -> ApiResult:
        return await self.call("/shortcut/clipboard/get", id=device_id)

    async def exec_url(self, device_id: str, url: str) -> ApiResult:
        """Open a URL or deep link on the phone (``/shortcut/exec/url``)."""
        return await self.call("/shortcut/exec/url", id=device_id, url=url)

    async def device_ip(self, device_id: str) -> ApiResult:
        return await self.call("/shortcut/device/ip", id=device_id)

```

## `backend/device_manager.py`

```python
"""Device discovery and the runtime coordinate space.

Devices are discovered over HTTP via ``/device/get`` (the handover measured
local calls at ~70 ms and concluded the WebSocket interface is not worth the
complexity, so there is no persistent socket here).

The critical detail this module exists to enforce: **all tap coordinates and
all OCR ``centre`` values live in the ``imgw x imgh`` space** -- the mirror
stream resolution (608x1080 on the reference phone). That is neither the panel
resolution nor iOS logical points (``width``/``height``, 375x667), which are not
usable for taps. ``imgw``/``imgh`` are read at runtime, never hardcoded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from api_client import IMouseClient

logger = logging.getLogger("device_manager")


@dataclass
class Device:
    """A device as reported by ``/device/get``."""

    device_id: str
    name: str = ""
    model: str = "Unknown"
    version: str = ""            # iOS version
    state: int = 0              # 1 = online, 0 = offline

    # Coordinate space for taps / OCR centres (mirror stream resolution).
    imgw: int = 0
    imgh: int = 0

    # iOS logical points -- informational only, NOT usable for taps.
    width: int = 0
    height: int = 0

    ip: str = ""
    vid: str = ""
    pid: str = ""
    uid: str = ""
    last_seen: datetime = field(default_factory=datetime.now)

    @property
    def is_online(self) -> bool:
        return self.state == 1

    @property
    def has_coord_space(self) -> bool:
        return self.imgw > 0 and self.imgh > 0

    @staticmethod
    def _int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def from_api(cls, data: dict) -> "Device":
        """Build from a ``/device/get`` list entry."""
        device_id = data.get("deviceid") or data.get("id") or data.get("mac") or ""
        return cls(
            device_id=device_id,
            name=data.get("name") or data.get("username") or "",
            model=data.get("model") or "Unknown",
            version=str(data.get("version") or ""),
            state=cls._int(data.get("state"), 0),
            imgw=cls._int(data.get("imgw")),
            imgh=cls._int(data.get("imgh")),
            width=cls._int(data.get("width")),
            height=cls._int(data.get("height")),
            ip=data.get("ip") or "",
            vid=data.get("vid") or "",
            pid=data.get("pid") or "",
            uid=data.get("uid") or "",
            last_seen=datetime.now(),
        )

    def to_pixels(self, fx: float, fy: float) -> tuple[int, int]:
        """Map fractional coordinates ``(0..1, 0..1)`` into the tap space.

        Uses ``imgw``/``imgh`` -- the only coordinate space that produces correct
        taps. Falls back to logical points only if the mirror resolution is
        unknown (which usually means mirroring is not active).
        """
        w = self.imgw or self.width
        h = self.imgh or self.height
        return int(w * fx), int(h * fy)

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "model": self.model,
            "version": self.version,
            "is_online": self.is_online,
            "state": self.state,
            # Report the coordinate space the frontend should reason about.
            "width": self.imgw or self.width,
            "height": self.imgh or self.height,
            "imgw": self.imgw,
            "imgh": self.imgh,
            "logical_width": self.width,
            "logical_height": self.height,
            "ip": self.ip,
            "bound": bool(self.vid or self.pid or self.uid),
        }


class DeviceRegistry:
    """Caches devices discovered via HTTP ``/device/get``."""

    def __init__(self, client: IMouseClient):
        self._client = client
        self._devices: dict[str, Device] = {}

    @property
    def devices(self) -> list[Device]:
        return list(self._devices.values())

    @property
    def online_devices(self) -> list[Device]:
        return [d for d in self._devices.values() if d.is_online]

    def get(self, device_id: str) -> Optional[Device]:
        return self._devices.get(device_id)

    async def refresh(self) -> list[Device]:
        """Re-query the console and refresh the cache.

        Stale offline entries from previously connected phones stay in the
        console's list, so callers should prefer :attr:`online_devices`.
        """
        raw = await self._client.get_devices()
        found: dict[str, Device] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            device = Device.from_api(entry)
            if device.device_id:
                found[device.device_id] = device
        self._devices = found

        online = [d for d in found.values() if d.is_online]
        logger.info("Device refresh: %d total, %d online", len(found), len(online))
        for d in online:
            if not d.has_coord_space:
                logger.warning(
                    "Device %s online but no imgw/imgh -- mirroring may be down; "
                    "taps will be unreliable.",
                    d.device_id,
                )
        return self.devices

    async def get_or_refresh(self, device_id: str) -> Optional[Device]:
        """Return a cached device, refreshing once if it is unknown."""
        device = self._devices.get(device_id)
        if device is None:
            await self.refresh()
            device = self._devices.get(device_id)
        return device

    def status(self) -> dict:
        return {
            "device_count": len(self._devices),
            "online_count": len(self.online_devices),
            "devices": [d.to_dict() for d in self._devices.values()],
        }

```

## `backend/niche.py`

```python
"""NutriSkin campaign profile.

EDIT THIS FILE when the official NutriSkin details arrive. It drives what the
agent treats as relevant and how it writes comments -- nothing else needs to
change. Keep the shape of the dict; just refine the text and lists.
"""

_NUTRISKIN = {
    "name": "NutriSkin",

    # ------------------------------------------------------------- LANGUAGE --
    # Only engage videos in THIS language (English name, e.g. "German",
    # "English", "Spanish"). Off-language videos are skipped like irrelevant
    # ones, and comments are written in this language. Set to "" or None to
    # allow any language.
    "language": "German",

    # ------------------------------------------------------------------ WHAT --
    # One or two sentences describing the product and who it's for. This is
    # given to the model verbatim, so write it the way you'd explain it to a
    # person. (Replace the placeholder below with the real description.)
    "description": (
        "NutriSkin is a mobile app that helps you reduce acne by improving your "
        "gut health through better nutrition -- an inside-out approach, using "
        "food choices to support clearer skin instead of topical products. Key "
        "features: Acne-Friendly Recipes (easy gut-healthy meals rich in fiber, "
        "antioxidants, and anti-inflammatory ingredients); a Food Scanner that "
        "instantly rates any food's impact on your skin with a 'Skin Score' "
        "based on sugar, dairy, and gut-health factors; and a Daily Skin Tracker "
        "that uses a daily selfie with light AI analysis for before-and-after "
        "acne progress. It's for anyone dealing with breakouts, hormonal acne, "
        "or skin issues they suspect are linked to diet and gut health."
    ),

    # -------------------------------------------------------------- RELEVANT --
    # Topics that make a video worth engaging with. Everything else is skipped.
    "relevant_topics": [
        "acne, breakouts, pimples",
        "hormonal acne",
        "skincare routines and product reviews",
        "clear skin / skin transformations (before & after)",
        "diet and skin, food triggers, elimination diets",
        "gut health and skin",
        "dermatology and skin advice",
        "supplements and vitamins for skin",
        "self-care and beauty tied to skin health",
    ],

    # Things that look adjacent but are NOT the niche (helps the model say no).
    "not_relevant": [
        "general makeup tutorials with no skin-health angle",
        "unrelated dances, gaming, sports, pranks, memes",
        "food/cooking content with no connection to skin",
        "a video where the creator merely HAS visible acne/skin marks but the "
        "topic is something else (moving, kitchen, vlog, lifestyle) -- incidental "
        "skin does NOT make it relevant; the TOPIC must be about skin/diet/gut",
        "general wellness/self-care/mindset/'gesundheit' talk with no specific "
        "skin, acne, diet-skin, or gut-health angle",
    ],

    # -------------------------------------------------------------- COMMENTS --
    # How comments should read. The model writes a NEW comment per video, so
    # this is style guidance, not a script.
    "comment_style": (
        "Write ONE short, casual comment -- about 6 to 16 words, one sentence. "
        "Hint at your own skin/acne struggle in a few words, then slip in that "
        "NutriSkin helped, tied to one real feature (the food Skin Score, the "
        "gut-health recipes, or the daily selfie tracker). It must read as a "
        "quick, relatable personal aside, NEVER an ad. CRITICAL: write the "
        "comment in the SAME LANGUAGE as the video/post (a German video gets a "
        "German comment, Spanish gets Spanish, etc.) and match how people "
        "actually type in that language. Keep 'NutriSkin' spelled exactly. Vary "
        "the struggle, wording, and feature every time -- never reuse a phrase or "
        "structure. Lowercase-casual is fine; at most one emoji and only "
        "sometimes; no links, @mentions, hashtags, or 'check out/try/link in "
        "bio' phrasing. Tie it to what THIS specific video is about."
    ),

    # Tone/length examples ONLY (shown in English) -- do NOT copy them, and write
    # the real comment in the video's language. Notice how short they are.
    "comment_examples": [
        "took me years to learn sugar was my trigger, the NutriSkin scanner made it click",
        "dairy wrecked my skin for ages, the recipes on NutriSkin finally sorted it",
        "the daily selfie tracker on NutriSkin is what kept me consistent tbh",
    ],
}


# ---------------------------------------------------------------- registry ----
# Add more apps/niches here (same shape as _NUTRISKIN) and they appear in the
# per-phone dropdown automatically.
NICHES = {
    "NutriSkin": _NUTRISKIN,
}

# The ACTIVE niche. It's a dict that is mutated IN PLACE (never reassigned) so
# that `from niche import NICHE` references throughout the app stay valid.
NICHE = dict(_NUTRISKIN)


def list_niches() -> list:
    """Names available in the per-phone dropdown."""
    return list(NICHES.keys())


def set_active_niche(name: str) -> bool:
    """Switch the active niche in place. Returns False if the name is unknown."""
    if name in NICHES:
        NICHE.clear()
        NICHE.update(dict(NICHES[name]))
        return True
    return False

```

## `backend/ui_map.py`

```python
"""Persistent UI map -- the "learning doc" of TikTok's interface on THIS phone.

Built interactively via the Interface Explorer: for each screen type we store
the pixel tap point of each named control (in the device's imgw x imgh space).
Navigation reads from this map instead of hardcoded fractional guesses.

Structure (ui_map.json):
    {
      "imgw": 608, "imgh": 1080,
      "screens": {
        "for_you_feed": {"like_button": [560, 470], "comment_button": [560, 560], ...},
        "search_results": {"back_arrow": [30, 60], ...},
        ...
      }
    }
"""
from __future__ import annotations

import json
import os
from typing import Optional

_PATH = os.path.join(os.path.dirname(__file__), "ui_map.json")


def load_map() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("screens", {})
    return data


def save_screen(screen_type: str, elements: dict, imgw: int = 0, imgh: int = 0) -> dict:
    """Store/replace the control points for one screen type. ``elements`` is
    ``{label: [x, y]}``."""
    data = load_map()
    if imgw:
        data["imgw"] = imgw
    if imgh:
        data["imgh"] = imgh
    cleaned = {}
    for label, xy in (elements or {}).items():
        if isinstance(xy, (list, tuple)) and len(xy) == 2:
            cleaned[str(label)] = [int(xy[0]), int(xy[1])]
    data["screens"][screen_type] = cleaned
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def get_point(screen_type: str, label: str) -> Optional[list]:
    """Return [x, y] for a mapped control, or None if not mapped yet."""
    pt = load_map()["screens"].get(screen_type, {}).get(label)
    return pt if isinstance(pt, list) and len(pt) == 2 else None

```

## `backend/ai_comments.py`

```python
"""AI-powered video understanding and comment generation using Claude.

Uses the Anthropic Messages API with vision: a JPEG screenshot of the phone
screen is sent to Claude, which reads the caption/hashtags/creator and drafts a
comment.

Cost notes from the handover: an image costs roughly ``(width * height) / 750``
tokens -- about 875 tokens for a 608x1080 screenshot. Each call here is
stateless (a single screenshot, no accumulated history), which is the cheap
path; we never re-send earlier screenshots. Prompt caching and a history cap are
the levers that matter if a conversational loop is ever added.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from anthropic import AsyncAnthropic

from config import Config

logger = logging.getLogger("ai_comments")


@dataclass
class VideoContext:
    """Context extracted from a TikTok video frame."""

    caption: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    username: Optional[str] = None
    ocr_text: Optional[str] = None
    screenshot_base64: Optional[str] = None


STYLE_PROMPTS = {
    "friendly": "warm, supportive, and genuinely engaged",
    "funny": "witty, humorous, with clever observations",
    "supportive": "encouraging, positive, and uplifting",
    "curious": "asking a thoughtful question, showing genuine interest",
    "impressed": "expressing admiration and appreciation",
    "relatable": "sharing a similar experience or feeling",
}

LENGTH_GUIDES = {
    "short": "Keep it to 1-2 sentences, very brief",
    "medium": "2-3 sentences with a bit more detail",
    "long": "3-5 sentences with full engagement",
}


class AICommentGenerator:
    """Generate context-aware comments with Claude."""

    def __init__(self, config: Config):
        self.config = config
        self._client: Optional[AsyncAnthropic] = None

    @property
    def client(self) -> AsyncAnthropic:
        if self._client is None:
            if not self.config.anthropic_api_key:
                raise ValueError("Anthropic API key not configured")
            self._client = AsyncAnthropic(api_key=self.config.anthropic_api_key)
        return self._client

    def set_api_key(self, api_key: str) -> None:
        self.config.anthropic_api_key = api_key
        self._client = None  # rebuild with the new key

    @staticmethod
    def _image_block(screenshot_base64: str) -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": screenshot_base64,
            },
        }

    async def _message(self, system: str, content: list[dict], max_tokens: int = 400) -> str:
        resp = await self.client.messages.create(
            model=self.config.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        parts = [block.text for block in resp.content if block.type == "text"]
        return "".join(parts).strip()

    async def analyze_screenshot(self, screenshot_base64: str) -> dict:
        """Extract structured context from a screenshot."""
        system = (
            "You analyze a TikTok video screenshot and extract structured data. "
            "Respond with ONLY a JSON object (no markdown fences) with keys: "
            "caption (string), hashtags (array of strings), username (string), "
            "content_description (string), mood (string). Use empty string or "
            "empty array when unknown."
        )
        content = [
            self._image_block(screenshot_base64),
            {"type": "text", "text": "Extract the context from this TikTok screenshot."},
        ]
        raw = await self._message(system, content, max_tokens=500)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{") : raw.rfind("}") + 1]
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("analyze_screenshot: non-JSON reply: %s", raw[:200])
            return {}

    async def generate_comment(
        self,
        context: VideoContext,
        style: str = "friendly",
        length: str = "short",
        include_emoji: bool = True,
        custom_instructions: Optional[str] = None,
    ) -> str:
        """Draft a single contextual comment."""
        emoji_rule = (
            "Include 1-2 relevant emojis." if include_emoji else "Do not use emojis."
        )
        extra = f"\nAdditional instructions: {custom_instructions}" if custom_instructions else ""
        system = (
            "You write authentic TikTok comments.\n"
            f"Voice: {STYLE_PROMPTS.get(style, STYLE_PROMPTS['friendly'])}.\n"
            f"Length: {LENGTH_GUIDES.get(length, LENGTH_GUIDES['short'])}.\n"
            f"{emoji_rule}\n"
            "The comment must feel genuinely human, be specific to this video, "
            "and match its energy. Avoid generic filler like 'Great video!' or "
            "'Love this!'. Do not be promotional or spammy."
            f"{extra}\n"
            "Output ONLY the comment text, nothing else."
        )

        parts = []
        if context.caption:
            parts.append(f"Caption: {context.caption}")
        if context.hashtags:
            parts.append(f"Hashtags: {', '.join(context.hashtags)}")
        if context.username:
            parts.append(f"Creator: {context.username}")
        if context.ocr_text:
            parts.append(f"On-screen text: {context.ocr_text}")
        context_str = "\n".join(parts) if parts else "No text context available."

        content: list[dict] = []
        if context.screenshot_base64:
            content.append(self._image_block(context.screenshot_base64))
        content.append(
            {"type": "text", "text": f"Write a comment for this TikTok:\n\n{context_str}"}
        )
        return await self._message(system, content, max_tokens=200)

    async def generate_multiple_comments(
        self,
        context: VideoContext,
        count: int = 3,
        styles: Optional[list[str]] = None,
    ) -> list[str]:
        """Draft several comment options across different styles."""
        if styles is None:
            styles = ["friendly", "funny", "supportive"]
        comments = []
        for i in range(count):
            comments.append(
                await self.generate_comment(context, style=styles[i % len(styles)])
            )
        return comments

    async def generate_post_caption(
        self,
        description: str,
        mood: str = "engaging",
        include_hashtags: bool = True,
        hashtag_count: int = 5,
    ) -> str:
        """Draft a caption for posting content."""
        hashtag_rule = (
            f"End with {hashtag_count} relevant trending hashtags."
            if include_hashtags
            else "Do not include hashtags."
        )
        system = (
            f"You are a TikTok creator writing a {mood} caption. It must be "
            "attention-grabbing, authentic, and encourage engagement. "
            f"{hashtag_rule}\nOutput ONLY the caption text."
        )
        content = [{"type": "text", "text": f"Write a caption for: {description}"}]
        return await self._message(system, content, max_tokens=300)


def encode_jpeg(image_bytes: bytes) -> str:
    """Base64-encode raw JPEG bytes for the vision API."""
    return base64.b64encode(image_bytes).decode("ascii")

```

## `backend/tiktok_actions.py`

```python
"""High-level TikTok actions built on the iMouse XP API.

All coordinates go through :meth:`Device.to_pixels`, which maps fractions into
the ``imgw x imgh`` mirror-stream space -- the only space that produces correct
taps. Text targets (Follow, Post) are located with ``/pic/find-text`` rather
than hardcoded coordinates; icon targets fall back to calibratable anchors.

Comment text is entered via the clipboard-then-paste flow so emoji and accented
characters survive (``/key/sendkey`` is ASCII only).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ai_comments import AICommentGenerator, VideoContext, encode_jpeg
from api_client import IMouseClient, IMouseError
from config import Config
from device_manager import Device, DeviceRegistry
from ui_map import get_point

logger = logging.getLogger("tiktok")


@dataclass
class ActionResult:
    success: bool
    message: str = ""
    data: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, message: str = "", **data) -> "ActionResult":
        return cls(True, message, data)

    @classmethod
    def fail(cls, message: str) -> "ActionResult":
        return cls(False, message)


class TikTokController:
    """Orchestrates TikTok interactions across one or more devices."""

    def __init__(self, config: Config, client: IMouseClient, registry: DeviceRegistry):
        self.config = config
        self.api = client
        self.registry = registry
        self.ai = AICommentGenerator(config)

    # ------------------------------------------------------------- devices ----
    async def get_device(self, device_id: str) -> Optional[Device]:
        return await self.registry.get_or_refresh(device_id)

    async def _coords(self, device: Device, ui_key: str) -> tuple[int, int]:
        ui = self.config.tiktok_ui.get(ui_key, {"x": 0.5, "y": 0.5})
        return device.to_pixels(ui["x"], ui["y"])

    def _mapped(self, label: str, screen: str = "for_you_feed") -> Optional[tuple[int, int]]:
        """Exact tap point from the user-built UI map, or None if not mapped."""
        pt = get_point(screen, label)
        return (int(pt[0]), int(pt[1])) if pt else None

    # ------------------------------------------------------- mirror recovery --
    async def screenshot_works(self, device_id: str) -> bool:
        """True if the AirPlay mirror is up (a screenshot succeeds)."""
        try:
            await self.api.screenshot(device_id, binary=True)
            return True
        except IMouseError:
            return False

    async def recover_mirror(self, device_id: str) -> bool:
        """Try to re-establish the dropped AirPlay mirror from the phone side.

        Control still works over USB even when the mirror is down, so we drive
        the phone's Control Center (opened with the ``ControlBar`` hotkey) and
        re-tap Screen Mirroring -> the PC receiver, using coordinates calibrated
        once while the mirror was up (``control_center`` in the UI map). Returns
        True only if a screenshot succeeds afterwards. Returns False (so the
        caller stops cleanly) when it isn't calibrated or the re-cast didn't take.
        """
        mirror_btn = get_point("control_center", "screen_mirroring")
        receiver = get_point("control_center", "airplay_receiver")
        if not mirror_btn:
            return False  # not calibrated -- can't safely blind-tap Control Center
        try:
            await self.api.send_key(device_id, fn_key="ControlBar")
            await asyncio.sleep(1.3)
            await self.api.click(device_id, int(mirror_btn[0]), int(mirror_btn[1]))
            await asyncio.sleep(1.6)
            if receiver:
                await self.api.click(device_id, int(receiver[0]), int(receiver[1]))
                await asyncio.sleep(3.5)  # AirPlay needs a moment to re-establish
            # Close Control Center with a swipe-down -- NOT the Home button, which
            # would drop the phone to the iOS home screen and exit TikTok.
            await self.api.swipe(device_id, "down", 0.5)
            await asyncio.sleep(1.5)
        except IMouseError:
            return False
        return await self.screenshot_works(device_id)

    # ---------------------------------------------------------- screenshots ---
    async def get_screenshot_bytes(self, device_id: str) -> bytes:
        """Raw JPEG bytes of the current screen."""
        return await self.api.screenshot(device_id, binary=True)

    async def get_screenshot_b64(self, device_id: str) -> str:
        """Base64 JPEG, for the web UI and the vision API."""
        return encode_jpeg(await self.get_screenshot_bytes(device_id))

    async def analyze_current_video(self, device_id: str) -> VideoContext:
        """Read the current video via vision + OCR into a :class:`VideoContext`."""
        context = VideoContext()

        try:
            context.screenshot_base64 = await self.get_screenshot_b64(device_id)
        except IMouseError as exc:
            logger.warning("screenshot for analysis failed: %s", exc)
            return context

        # Vision analysis (only if an Anthropic key is configured).
        if context.screenshot_base64 and self.config.anthropic_api_key:
            try:
                analysis = await self.ai.analyze_screenshot(context.screenshot_base64)
                context.caption = analysis.get("caption") or None
                context.hashtags = analysis.get("hashtags") or []
                context.username = analysis.get("username") or None
                context.ocr_text = analysis.get("content_description") or None
            except Exception as exc:  # noqa: BLE001 - never let AI break analysis
                logger.warning("vision analysis failed: %s", exc)

        # OCR the caption region as a supplement. Gracefully skip if the OCR
        # plugin is not running (code 28).
        device = await self.get_device(device_id)
        if device and device.has_coord_space:
            area = self.config.tiktok_ui.get("caption_area")
            rect = None
            if area:
                x0, y0 = device.to_pixels(area["x"], area["y"])
                x1, y1 = device.to_pixels(area["x"] + area["w"], area["y"] + area["h"])
                rect = [x0, y0, x1, y1]
            try:
                result = await self.api.ocr(device_id, rect=rect)
                texts = [i.get("text", "") for i in result.items if i.get("text")]
                if texts:
                    ocr_text = " ".join(texts)
                    context.ocr_text = ocr_text
                    context.hashtags = sorted(
                        set(context.hashtags) | set(re.findall(r"#\w+", ocr_text))
                    )
            except IMouseError as exc:
                if exc.code == 28:
                    logger.info("OCR skipped: plugin not connected (code 28)")
                else:
                    logger.warning("OCR failed: %s", exc)

        return context

    # ---------------------------------------------------------- core taps -----
    async def _click(self, device_id: str, x: int, y: int) -> ActionResult:
        # Let the relative-mouse pointer come to rest before tapping, so the
        # click doesn't fire while the pointer is still travelling to (x, y).
        if self.config.pre_tap_settle:
            await asyncio.sleep(self.config.pre_tap_settle)
        try:
            await self.api.click(device_id, x, y)
            return ActionResult.ok()
        except IMouseError as exc:
            return ActionResult.fail(str(exc))

    async def swipe_next(self, device_id: str) -> ActionResult:
        try:
            await self.api.swipe(device_id, "up", self.config.swipe_length)
            # A swipe drags the pointer; give it a beat to settle before any tap.
            await asyncio.sleep(self.config.post_swipe_settle)
            return ActionResult.ok("Swiped to next video")
        except IMouseError as exc:
            return ActionResult.fail(str(exc))

    async def swipe_previous(self, device_id: str) -> ActionResult:
        try:
            await self.api.swipe(device_id, "down", self.config.swipe_length)
            await asyncio.sleep(self.config.post_swipe_settle)
            return ActionResult.ok("Swiped to previous video")
        except IMouseError as exc:
            return ActionResult.fail(str(exc))

    async def like_video(
        self,
        device_id: str,
        use_double_tap: bool = True,
        point: Optional[tuple[int, int]] = None,
    ) -> ActionResult:
        # Priority: a per-frame vision-located point (adapts to the rail shifting
        # up/down), then the static UI map, then the double-tap/anchor fallback.
        pt = point or self._mapped("like_button")
        if pt:
            result = await self._click(device_id, *pt)
            result.message = "Liked" if result.success else result.message
            return result

        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")
        if use_double_tap:  # fallback: double-tap the video centre
            x, y = await self._coords(device, "video_center")
            try:
                await self.api.click(device_id, x, y)
                await asyncio.sleep(0.12)
                await self.api.click(device_id, x, y)
                return ActionResult.ok("Liked (double-tap)")
            except IMouseError as exc:
                return ActionResult.fail(str(exc))
        x, y = await self._coords(device, "like_button")
        result = await self._click(device_id, x, y)
        result.message = "Liked" if result.success else result.message
        return result

    async def save_video(
        self, device_id: str, point: Optional[tuple[int, int]] = None
    ) -> ActionResult:
        """Save/bookmark the current post."""
        # Priority: per-frame vision point, then the static map, then share-sheet.
        pt = point or self._mapped("save_button")
        if pt:
            result = await self._click(device_id, *pt)
            result.message = "Saved video" if result.success else result.message
            return result

        # Fallback: open the share sheet and find the Save entry by text.
        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")
        x, y = await self._coords(device, "share_button")
        if not (await self._click(device_id, x, y)).success:
            return ActionResult.fail("Could not open share menu")
        await asyncio.sleep(self.config.action_delay)
        if await self._tap_text(device_id, ["Save", "Favorites", "Add to Favorites"]):
            return ActionResult.ok("Saved video")
        await self._dismiss(device_id)
        return ActionResult.fail("Save option not found (share menu text not matched)")

    async def repost_video(
        self, device_id: str, point: Optional[tuple[int, int]] = None
    ) -> ActionResult:
        """Open the share sheet and tap the Repost entry (by text)."""
        pt = point or self._mapped("share_button")
        if pt:
            opened = await self._click(device_id, *pt)
        else:
            device = await self.get_device(device_id)
            if not device:
                return ActionResult.fail("Device not found")
            x, y = await self._coords(device, "share_button")
            opened = await self._click(device_id, x, y)
        if not opened.success:
            return ActionResult.fail("Could not open share menu")
        await asyncio.sleep(self.config.action_delay)

        if await self._tap_text(device_id, ["Repost", "Re-post"]):
            return ActionResult.ok("Reposted video")
        await self._dismiss(device_id)
        return ActionResult.fail("Repost option not found (share menu text not matched)")

    async def return_to_feed(self, device_id: str, attempt: int = 0) -> ActionResult:
        """Coordinate-free fallback recovery: hardware Back (+ swipe-down).

        Blind coordinate taps here proved dangerous (top-left = LIVE, wrong tab =
        DMs), so the agent uses the vision-guided recovery in ai_agent instead.
        This method stays as a safe, tap-free fallback for any other caller.
        """
        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")
        try:
            await self.api.send_key(device_id, fn_key="TAB+b")
        except IMouseError:
            pass
        await asyncio.sleep(self.config.action_delay)
        if attempt >= 2:
            try:
                await self.api.swipe(device_id, "down", 0.5)
            except IMouseError:
                pass
            await asyncio.sleep(self.config.action_delay)
        return ActionResult.ok("Return-to-feed attempted (back)")

    async def follow_creator(self, device_id: str) -> ActionResult:
        if await self._tap_text(device_id, ["Follow"]):
            return ActionResult.ok("Followed creator")
        return ActionResult.fail("Follow button not found")

    # ------------------------------------------------------------ comments ----
    async def _tap_text(
        self, device_id: str, candidates: list[str], similarity: Optional[float] = None
    ) -> bool:
        """Find any of ``candidates`` on screen and tap its centre. False if none."""
        try:
            matches = await self.api.find_text(
                device_id,
                candidates,
                similarity=similarity or self.config.ocr_similarity,
            )
        except IMouseError as exc:
            if exc.code == 28:
                logger.info("find-text needs the OCR plugin (code 28)")
            else:
                logger.warning("find-text failed: %s", exc)
            return False

        for match in matches:
            centre = match.get("centre") or match.get("center")
            if centre and len(centre) == 2:
                await self.api.click(device_id, int(centre[0]), int(centre[1]))
                return True
        return False

    async def _dismiss(self, device_id: str) -> None:
        """Best-effort: close an open comment/share sheet or keyboard.

        A downward swipe reliably dismisses TikTok's comment/share sheets without
        the risk of a blind tap landing on a navigation control (LIVE, a tab,
        etc.). Used on every comment/save/repost exit path so the agent never
        gets stuck in a dialog with no way back.
        """
        # A downward swipe closes the comment sheet, but TikTok's SHARE sheet
        # often ignores it -- the reliable close is tapping the dimmed backdrop
        # above the sheet. Do both so either sheet ends up dismissed.
        try:
            await self.api.swipe(device_id, "down", 0.85)
        except IMouseError:
            pass
        await asyncio.sleep(self.config.action_delay)
        device = await self.get_device(device_id)
        if device:
            # Tap the dimmed backdrop above the sheet -- but well BELOW the top
            # tab bar (Gefolgt / Für dich / search at ~y=0.06), which would
            # switch feeds or open search. y=0.22 is pure scrim.
            bx, by = device.to_pixels(0.3, 0.22)
            try:
                await self._click(device_id, bx, by)
            except IMouseError:
                pass
        await asyncio.sleep(self.config.action_delay)

    async def _find_comment_input(self, device_id: str) -> Optional[tuple[int, int]]:
        """Locate the 'Add comment' input BAR (bottom of the sheet) so we type a
        top-level comment -- not a reply on an existing comment. Returns the tap
        point, or None if the placeholder text isn't found."""
        try:
            # ASCII-safe fragments (OCR mangles umlauts and truncates with '…').
            hits = await self.api.find_text(
                device_id,
                ["hinzu", "Kommentar hinzu", "Add comment", "Sag etwas", "Nettes"],
                similarity=0.5,
            )
        except IMouseError:
            return None
        device = await self.get_device(device_id)
        h = (device.imgh or device.height) if device else 1080
        best = None
        for hit in hits:
            c = hit.get("centre") or hit.get("center")
            # The input bar is at the very BOTTOM of the sheet; ignore the
            # "Kommentare N" header and the comment list above it.
            if c and len(c) == 2 and c[1] > h * 0.82:
                if best is None or c[1] > best[1]:
                    best = (int(c[0]), int(c[1]))
        return best

    async def _comment_sheet_open(self, device_id: str) -> bool:
        """True if the comment sheet is showing (its header/CTA text is visible)."""
        try:
            hits = await self.api.find_text(
                device_id,
                ["Kommentar", "Kommentare", "Comments", "Comment", "Kommentieren"],
                similarity=0.7,
            )
            return bool(hits)
        except IMouseError:
            return False  # OCR unavailable -- caller falls back to best-effort

    async def open_comments(
        self, device_id: str, point: Optional[tuple[int, int]] = None
    ) -> ActionResult:
        pt = point or self._mapped("comment_button")
        if pt:
            return await self._click(device_id, *pt)
        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")
        x, y = await self._coords(device, "comment_button")
        return await self._click(device_id, x, y)

    # Transliteration for the typed fallback so German still reads correctly if
    # the clipboard bridge is unavailable (ä->ae etc.).
    _TRANSLIT = {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
        "á": "a", "à": "a", "â": "a", "é": "e", "è": "e", "ê": "e", "í": "i", "ì": "i",
        "ó": "o", "ò": "o", "ô": "o", "ú": "u", "ù": "u", "ñ": "n", "ç": "c",
        "–": "-", "—": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
    }

    @classmethod
    def _to_ascii(cls, text: str) -> str:
        out = "".join(cls._TRANSLIT.get(c, c) for c in text)
        return "".join(c for c in out if ord(c) < 128)

    _ZY_SWAP = str.maketrans({"z": "y", "y": "z", "Z": "Y", "Y": "Z"})

    def _for_typing(self, text: str) -> str:
        """Prepare text for /key/sendkey: transliterate to ASCII, then undo the
        QWERTZ z<->y swap so a German phone renders it correctly."""
        ascii_text = self._to_ascii(text)
        if self.config.keyboard_swap_zy:
            ascii_text = ascii_text.translate(self._ZY_SWAP)
        return ascii_text

    async def enter_text(self, device_id: str, text: str) -> ActionResult:
        """Enter ``text`` into the focused field.

        ALWAYS prefer the clipboard paste flow (set clipboard -> pause -> WIN+v):
        it is keyboard-LAYOUT-INDEPENDENT and preserves umlauts/emoji, whereas
        /key/sendkey types via scancodes -- on a German (QWERTZ) phone that swaps
        z<->y and mangles non-ASCII. Only if the clipboard bridge is unavailable
        do we type a transliterated ASCII version so the comment still posts.
        """
        exc: Optional[Exception] = None
        if self.config.use_clipboard_paste:
            try:
                # Short timeout so a missing clipboard bridge fails fast instead
                # of hanging ~30s per comment.
                await asyncio.wait_for(self.api.clipboard_set(device_id, text), timeout=6.0)
                await asyncio.sleep(self.config.paste_delay)
                await self.api.send_key(device_id, fn_key="WIN+v")
                return ActionResult.ok("Text entered (paste)")
            except (IMouseError, asyncio.TimeoutError) as e:
                exc = e  # bridge unavailable -- type instead

        fallback = self._for_typing(text).strip()
        if fallback:
            try:
                await self.api.send_key(device_id, key=fallback)
                return ActionResult.ok("Text entered (typed)")
            except IMouseError as e:
                exc = e
        return ActionResult.fail(f"could not enter text: {exc}")

    async def comment_on_video(
        self,
        device_id: str,
        comment: Optional[str] = None,
        use_ai: bool = True,
        ai_style: str = "friendly",
        comment_button_point: Optional[tuple[int, int]] = None,
    ) -> ActionResult:
        """Full flow: open comments, (optionally) generate, type, and post."""
        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")

        if comment is None and use_ai:
            context = await self.analyze_current_video(device_id)
            try:
                comment = await self.ai.generate_comment(context, style=ai_style)
            except Exception as exc:  # noqa: BLE001
                return ActionResult.fail(f"AI comment generation failed: {exc}")
        if not comment:
            return ActionResult.fail("No comment text available")

        # Open the comment sheet. We only re-tap the comment ICON if the sheet is
        # still closed (tapping it again while OPEN would hit a comment in the
        # sheet and start a REPLY). Prefer the real "Add comment" input bar,
        # located by its placeholder text, so we post a TOP-LEVEL comment.
        opened = False
        input_pt: Optional[tuple[int, int]] = None
        for _ in range(2):
            input_pt = await self._find_comment_input(device_id)
            if input_pt or await self._comment_sheet_open(device_id):
                opened = True
                break
            await self.open_comments(device_id, point=comment_button_point)
            await asyncio.sleep(self.config.action_delay)
        if not opened:
            input_pt = await self._find_comment_input(device_id)
            opened = bool(input_pt) or await self._comment_sheet_open(device_id)
        if not opened:
            return ActionResult.fail("Could not open comments")

        # Tap the located input bar; fall back to the mapped/anchor point.
        if not input_pt:
            input_pt = self._mapped("comment_input", "comments_sheet")
        if input_pt:
            await self._click(device_id, *input_pt)
        else:
            x, y = await self._coords(device, "comment_input")
            await self._click(device_id, x, y)
        await asyncio.sleep(self.config.paste_delay)

        entered = await self.enter_text(device_id, comment)
        if not entered.success:
            # Never leave the keyboard/comment sheet open -- return to the feed.
            await self._dismiss(device_id)
            return ActionResult.fail(f"Could not enter comment: {entered.message}")
        await asyncio.sleep(self.config.paste_delay)

        # Post: prefer the labelled Send/Post button, else Enter.
        posted = await self._tap_text(device_id, ["Post", "Send"])
        if not posted:
            try:
                await self.api.send_key(device_id, fn_key="ENTER")
                posted = True
            except IMouseError:
                posted = False

        # Always dismiss the comment sheet so the next step starts clean.
        await asyncio.sleep(self.config.action_delay)
        await self._dismiss(device_id)

        if posted:
            return ActionResult.ok("Comment posted", comment=comment)
        return ActionResult.fail("Could not post comment")

    async def generate_comment_options(self, device_id: str, count: int = 3) -> list[str]:
        context = await self.analyze_current_video(device_id)
        return await self.ai.generate_multiple_comments(context, count=count)

    # ------------------------------------------------------------- batch ------
    async def batch_action(self, device_ids: list[str], action: str, **kwargs) -> dict:
        actions = {
            "swipe_next": self.swipe_next,
            "swipe_previous": self.swipe_previous,
            "like": self.like_video,
            "save": self.save_video,
            "follow": self.follow_creator,
        }
        fn = actions.get(action)
        if fn is None:
            return {d: ActionResult.fail(f"Unknown action: {action}") for d in device_ids}

        results = await asyncio.gather(
            *(fn(d, **kwargs) for d in device_ids), return_exceptions=True
        )
        out = {}
        for device_id, res in zip(device_ids, results):
            out[device_id] = (
                ActionResult.fail(str(res)) if isinstance(res, Exception) else res
            )
        return out

    # --------------------------------------------------------- automation -----
    async def auto_browse(
        self,
        device_id: str,
        video_count: int = 10,
        like_probability: float = 0.3,
        comment_probability: float = 0.1,
        min_watch_time: float = 3.0,
        max_watch_time: float = 15.0,
    ) -> dict:
        """Browse with randomised human-like engagement."""
        import random

        stats = {"videos": 0, "likes": 0, "comments": 0, "errors": []}
        for _ in range(video_count):
            try:
                await asyncio.sleep(random.uniform(min_watch_time, max_watch_time))
                if random.random() < like_probability:
                    if (await self.like_video(device_id)).success:
                        stats["likes"] += 1
                if random.random() < comment_probability:
                    if (await self.comment_on_video(device_id, use_ai=True)).success:
                        stats["comments"] += 1
                await self.swipe_next(device_id)
                stats["videos"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(str(exc))
        return stats

```

## `backend/ai_agent.py`

```python
"""Claude-driven perceive -> decide -> act agent for TikTok.

Each step:
  1. Perceive: capture a screenshot and run OCR to get on-screen text elements,
     each with a tap ``centre`` already in the ``imgw x imgh`` space.
  2. Decide: hand Claude the screenshot + the OCR element list + the goal and a
     small set of tools; Claude calls exactly one tool (forced tool use).
  3. Act: execute that tool against the device via the controller.

Text targets are grounded in real coordinates by tapping OCR/find-text centres,
so Claude never has to guess pixel positions. Icon targets fall back to the
calibratable anchors. OCR needs the PaddleOCR plugin; if it isn't running
(code 28) the agent degrades to vision-only reasoning (swipe / like / comment
via anchors), which is surfaced in the step log.

Cost control follows the handover: each decision sends only the *current*
screenshot plus a short text history of prior actions -- earlier screenshots are
never re-sent.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

from api_client import IMouseError
from campaign_log import SessionLogger
from niche import NICHE
from tiktok_actions import ActionResult, TikTokController

logger = logging.getLogger("ai_agent")


# Tools Claude may call. One action per step (forced, non-parallel).
TOOLS: list[dict] = [
    {
        "name": "swipe_next",
        "description": "Scroll to the next video (swipe up). Use when the current video isn't interesting or you're done engaging with it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "swipe_previous",
        "description": "Go back to the previous video (swipe down).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "like",
        "description": "Like the current video (double-tap heart). Use when the content is genuinely good.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save",
        "description": "Save/bookmark the current video for later.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "repost",
        "description": "Repost the current video (via the share sheet). Use for something you'd want to share on.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "return_to_feed",
        "description": "Go back to the main For You feed by tapping Home. Use this whenever the screen is NOT a normal playing For You video -- a profile page, comments, share sheet, search, DMs, or any menu -- instead of trying to interact with it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "comment",
        "description": "Post a short, genuine, relevant comment on the current video. This handles opening the comment box, typing, posting, and closing it -- you do not tap anything yourself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The comment text to post."}
            },
            "required": ["text"],
        },
    },
    {
        "name": "stop",
        "description": "End the session -- nothing useful left to do, or the goal is met.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why you're stopping."}
            },
        },
    },
]

DEFAULT_GOAL = (
    "Browse TikTok like a real, curious human. For each video: read what it's "
    "about, like it if it's genuinely good, occasionally leave a short relevant "
    "comment or save/repost something you really love, then move to the next "
    "video. Don't engage with everything -- be selective and natural."
)

SYSTEM_PROMPT = (
    "You are operating a real iPhone running the TikTok For You feed, one action "
    "at a time. You see the current screen as an image plus a list of on-screen "
    "text detected by OCR. Use both to understand exactly what is on screen "
    "right now, then call exactly one tool.\n\n"
    "You may ONLY perform these actions: swipe_next, swipe_previous, like, save, "
    "repost, comment, return_to_feed, or stop. There is no other action. Never "
    "open the camera or any create/record/post-a-video screen, and never tap "
    "arbitrary buttons.\n\n"
    "Before acting, check what screen you're on. like/save/repost/comment are "
    "ONLY valid on a normal, full-screen For You video that's playing. If you "
    "see a profile page (avatar, follower counts, a grid of videos, tabs like "
    "Videos/Reposts/Liked), an open comment or share sheet, search, DMs, "
    "settings, an ad, or a login/permission dialog, do NOT try to interact with "
    "it -- call return_to_feed to get back to browsing.\n\n"
    "The comment action is self-contained: it opens the box, types, posts, and "
    "closes it for you. Behave like a genuine, selective human: don't like or "
    "comment on everything, vary your actions, and keep comments short, "
    "specific, and authentic."
)


class TikTokAgent:
    """Wraps a controller with a Claude decision loop."""

    def __init__(self, controller: TikTokController):
        self.controller = controller
        self.config = controller.config
        self._campaign = self._fresh_campaign_state()
        self._last_locate_source = "map"  # ocr | vision | map (for the log tag)

    @staticmethod
    def _fresh_campaign_state() -> dict:
        return {
            "running": False,
            "stop": False,
            "device_id": None,
            "duration": 0,
            "elapsed": 0.0,
            "stats": {
                "videos": 0, "relevant": 0, "likes": 0, "saves": 0,
                "comments": 0, "recovered": 0, "errors": 0,
            },
            "log": [],
            "used_comments": [],
            "last_screenshot": None,   # b64 of the most recent frame, for the UI
            "off_feed_streak": 0,      # consecutive off-feed cycles (escalates recovery)
            "error_streak": 0,         # consecutive perceive errors (device offline etc.)
            "videos_since_comment": 999,  # relevant videos since the last comment
            "comment_gap": 0,          # randomized cooldown target (set on first use)
            "engaged_posts": [],       # word-sets of posts already engaged (no re-liking)
            "next_perc": None,         # settled frame from the last advance (reused, saves a capture)
            "log_session": None,       # folder name of this run's saved log
        }

    # --------------------------------------------------------- perceive ------
    async def perceive(self, device_id: str) -> dict:
        """Capture the screen and OCR elements for one decision."""
        result: dict[str, Any] = {"screenshot_b64": None, "elements": [], "ocr_available": False}

        try:
            result["screenshot_b64"] = await self.controller.get_screenshot_b64(device_id)
        except IMouseError as exc:
            result["error"] = f"screenshot failed: {exc}"
            result["error_code"] = exc.code  # 25 = device offline, 28 = OCR plugin
            return result

        try:
            ocr = await self.controller.api.ocr(device_id)
            elements = []
            for item in ocr.items:
                text = item.get("text")
                centre = item.get("centre") or item.get("center")
                if text and centre and len(centre) == 2:
                    elements.append({"text": text, "centre": [int(centre[0]), int(centre[1])]})
            result["elements"] = elements
            result["ocr_available"] = True
        except IMouseError as exc:
            result["ocr_error"] = (
                "OCR plugin not connected (code 28)" if exc.code == 28 else str(exc)
            )

        return result

    async def settled_perception(self, device_id: str, tries: int = 3) -> dict:
        """Perceive a STABLE frame.

        The AirPlay mirror lags the phone by up to a second or two, so a capture
        taken right after a swipe/navigation can show the *previous* video or a
        transitional menu that's already gone -- which made the campaign mark
        OFF-FEED while actually on the feed, or engage the wrong video. We wait
        until two consecutive captures agree (same fingerprint) before trusting
        the frame.
        """
        perc = await self.perceive(device_id)
        for _ in range(max(0, tries - 1)):
            if perc.get("error"):
                return perc
            words1 = self._post_words(perc)
            await asyncio.sleep(0.45)
            nxt = await self.perceive(device_id)
            if nxt.get("error"):
                return nxt
            words2 = self._post_words(nxt)
            # Settled when the two captures describe the same thing (or both empty).
            if (words1 == words2) or self._same_post(words1, words2):
                return nxt
            perc = nxt
        return perc

    # ---------------------------------------------------------- decide -------
    async def decide(
        self, perception: dict, goal: str, history: list[str]
    ) -> dict:
        """Ask Claude for the next action. Returns {"name", "input"}."""
        client = self.controller.ai.client  # AsyncAnthropic; raises if no key

        elements = perception.get("elements", [])
        if elements:
            elements_text = "Text currently on screen (OCR) -- use it to understand what's showing:\n" + "\n".join(
                f"- {e['text']}" for e in elements
            )
        elif perception.get("ocr_available"):
            elements_text = "No text detected on screen (likely a plain video)."
        else:
            elements_text = (
                "OCR is unavailable ("
                + perception.get("ocr_error", "unknown")
                + "); read any text from the image instead."
            )

        history_text = (
            "Recent actions:\n" + "\n".join(f"- {h}" for h in history[-8:])
            if history
            else "No actions yet."
        )

        content: list[dict] = []
        if perception.get("screenshot_b64"):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": perception["screenshot_b64"],
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": f"Goal: {goal}\n\n{elements_text}\n\n{history_text}\n\n"
                "Decide the single next action and call one tool.",
            }
        )

        resp = await client.messages.create(
            model=self.config.anthropic_model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            tool_choice={"type": "any", "disable_parallel_tool_use": True},
            messages=[{"role": "user", "content": content}],
        )

        for block in resp.content:
            if block.type == "tool_use":
                return {"name": block.name, "input": dict(block.input or {})}

        # Forced tool use should always yield a tool_use block; guard anyway.
        return {"name": "stop", "input": {"reason": "no tool call returned"}}

    # ----------------------------------------------------------- act ---------
    async def execute(self, device_id: str, name: str, args: dict) -> ActionResult:
        c = self.controller
        if name == "swipe_next":
            return await c.swipe_next(device_id)
        if name == "swipe_previous":
            return await c.swipe_previous(device_id)
        # Rail actions: locate the icons on THIS frame first so the tap tracks the
        # (shifting) rail instead of a stale fixed pixel.
        if name in ("like", "save", "repost", "comment"):
            pts = await self.locate_actions(device_id)
            label = {"like": "like_button", "save": "save_button",
                     "repost": "share_button", "comment": "comment_button"}[name]
            hit = pts.get(label)
            src = f"{self._last_locate_source}@{hit}" if hit else "map"
            if name == "like":
                res = await c.like_video(device_id, point=hit)
            elif name == "save":
                res = await c.save_video(device_id, point=hit)
            elif name == "repost":
                res = await c.repost_video(device_id, point=hit)
            else:
                text = args.get("text", "")
                if not text:
                    return ActionResult.fail("comment called without text")
                res = await c.comment_on_video(
                    device_id, comment=text, use_ai=False, comment_button_point=hit
                )
            res.message = f"{res.message} [{src}]"
            return res
        if name == "return_to_feed":
            perc = await self.perceive(device_id)
            move = await self.recover(device_id, perc)
            await self.apply_recovery(device_id, move)
            return ActionResult.ok(f"recover: {move.get('method')} — {move.get('note', '')}")
        if name == "stop":
            return ActionResult.ok(f"Stopped: {args.get('reason', '')}")
        return ActionResult.fail(f"Unknown action: {name}")

    # ------------------------------------------------------------ loop -------
    async def step(self, device_id: str, goal: Optional[str] = None, history: Optional[list[str]] = None) -> dict:
        """One perceive -> decide -> act cycle. Returns a log entry."""
        goal = goal or DEFAULT_GOAL
        history = history or []

        perception = await self.perceive(device_id)
        if perception.get("error"):
            return {"action": None, "success": False, "message": perception["error"]}

        decision = await self.decide(perception, goal, history)
        result = await self.execute(device_id, decision["name"], decision.get("input", {}))

        return {
            "action": decision["name"],
            "args": decision.get("input", {}),
            "success": result.success,
            "message": result.message,
            "elements_seen": len(perception.get("elements", [])),
            "ocr_available": perception.get("ocr_available", False),
        }

    async def run(
        self, device_id: str, goal: Optional[str] = None, max_steps: int = 10
    ) -> dict:
        """Run up to ``max_steps`` decision cycles toward ``goal``."""
        goal = goal or DEFAULT_GOAL
        history: list[str] = []
        steps: list[dict] = []

        for _ in range(max_steps):
            entry = await self.step(device_id, goal, history)
            steps.append(entry)
            action = entry.get("action")
            summary = f"{action}({entry.get('args') or ''}) -> {'ok' if entry['success'] else 'fail'}: {entry['message']}"
            history.append(summary)
            if action == "stop":
                break

        return {"goal": goal, "steps": steps, "count": len(steps)}

    # ============================================================ campaign ====
    # A long-running, niche-aware loop: classify each video against the niche,
    # skip irrelevant ones fast, watch relevant ones for a human-random time,
    # and engage (like/save/comment) with randomised, varied behaviour.

    def _plan_tool(self) -> list[dict]:
        return [
            {
                "name": "plan_video",
                "description": (
                    f"Report whether the current TikTok video is relevant to the "
                    f"{NICHE['name']} niche and, if so, a fresh personal comment."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "on_feed": {
                            "type": "boolean",
                            "description": "TRUE whenever a single full-screen vertical video is PLAYING with the like/comment/share icons on the right -- a video you could swipe up to skip -- REGARDLESS of its topic, and whether it's the For You ('Für dich') or Following ('Gefolgt') tab. FALSE only when it is clearly NOT a playing video: a search box or grid of thumbnails, a profile page, an open comment/share sheet, the inbox/DMs, settings, or a menu/dialog. Do NOT set false just because the video is off-topic.",
                        },
                        "screen": {
                            "type": "string",
                            "description": "A short plain-language description of what is actually on screen right now (e.g. 'For You video about baking', 'profile page of @user, Reposts tab', 'comment sheet open').",
                        },
                        "post_type": {
                            "type": "string",
                            "enum": ["video", "photo_slideshow", "live", "none"],
                            "description": "When on_feed: 'live' if this is a LIVE stream (a LIVE badge/'Jetzt LIVE', a viewer count, scrolling live chat, gift/coin icons -- someone broadcasting in real time); 'photo_slideshow' if it's a photo carousel (multiple images with small slide-indicator dots near the bottom-centre, sometimes a counter like 1/5); otherwise 'video'. 'none' when not on a feed post.",
                        },
                        "relevant": {
                            "type": "boolean",
                            "description": "Only when on_feed: is the video's TOPIC relevant to the niche? An off-topic playing video is on_feed=true, relevant=false (we'll swipe past it).",
                        },
                        "reason": {
                            "type": "string",
                            "description": "A few words on the relevance decision.",
                        },
                        "language": {
                            "type": "string",
                            "description": "The primary language of the video's speech, caption, and on-screen text, as an English name (e.g. 'German', 'English', 'Polish', 'Spanish'). Your best guess from what you can read/see.",
                        },
                        "comment": {
                            "type": "string",
                            "description": "If on_feed and relevant, a fresh, human, personal-experience comment tailored to THIS video that subtly promotes the product. Leave empty otherwise.",
                        },
                    },
                    "required": ["on_feed", "screen", "relevant", "reason"],
                },
            }
        ]

    def _plan_system(self, used_comments: list[str]) -> str:
        parts = [
            f"You are curating a TikTok For You feed for the {NICHE['name']} niche.",
            "\nFIRST, decide on_feed. Set on_feed=TRUE whenever a single "
            "full-screen vertical video is PLAYING with the like/comment/share "
            "icons stacked on the right -- a video you could swipe up to skip. "
            "This is TRUE for both the 'Für dich' (For You) and 'Gefolgt' "
            "(Following) tabs, and TRUE no matter what the video is about. Do NOT "
            "set on_feed=false just because the video is off-topic -- that is what "
            "'relevant' is for; an off-topic video is on_feed=TRUE with "
            "relevant=FALSE and we will simply swipe past it.\n"
            "Set on_feed=FALSE ONLY when it is clearly not a playing full-screen "
            "video: a search box or a grid of video thumbnails (search results / "
            "explore), a profile page (avatar + follower counts + a video grid), "
            "an open comment or share sheet, the inbox/DMs, settings, a livestream "
            "chat, or any menu/dialog. Note: a Follow button, a like/comment count, "
            "and a @username appear on NORMAL feed videos too -- they do NOT mean "
            "it's a profile page. A profile page specifically shows a GRID of video "
            "thumbnails and tabs like Videos/Reposts/Liked. A photo SLIDESHOW post "
            "(a carousel of images with small slide-indicator dots near the "
            "bottom-centre) is STILL on_feed=TRUE -- set post_type='photo_slideshow' "
            "for it. Always fill in 'screen' with what you see.",
            f"\n{NICHE['name']} is: {NICHE['description']}",
            "\nA video is RELEVANT if it relates to any of: "
            + "; ".join(NICHE["relevant_topics"])
            + ".",
            "It is NOT relevant if it's, e.g.: " + "; ".join(NICHE["not_relevant"]) + ".",
            "Judge relevance by the video's TOPIC, not by whether the person has "
            "visible acne or skin marks -- someone with acne talking about moving, "
            "cooking, or lifestyle is NOT relevant.",
            "Be reasonably strict -- when in doubt, mark it not relevant so we skip it.",
            "\nIf the current post is a LIVE stream (LIVE badge, viewer count, "
            "scrolling live chat, gift/coin icons), set post_type='live' -- we "
            "always swipe past lives, never engage them.",
            "\nAlways report the video's primary 'language' (English name).",
            "\nIf relevant, also write a comment. " + NICHE["comment_style"],
            f"Always spell the product name exactly as '{NICHE['name']}' -- never "
            "misspell it.",
            "Match the vibe of these examples but DO NOT copy them -- write something "
            "new that fits THIS specific video:\n- " + "\n- ".join(NICHE["comment_examples"]),
        ]
        if used_comments:
            parts.append(
                "\nYou have ALREADY posted these comments this session -- write "
                "something clearly different:\n- " + "\n- ".join(used_comments)
            )
        parts.append("\nCall plan_video with your decision.")
        return "\n".join(parts)

    async def plan_video(self, perception: dict, used_comments: list[str]) -> dict:
        """One Claude call: relevance + (if relevant) a fresh niche comment."""
        client = self.controller.ai.client

        elements = perception.get("elements", [])
        if elements:
            screen_text = "On-screen text (OCR):\n" + "\n".join(f"- {e['text']}" for e in elements)
        elif perception.get("ocr_available"):
            screen_text = "No text detected on screen."
        else:
            screen_text = "OCR unavailable; judge from the image."

        content: list[dict] = []
        if perception.get("screenshot_b64"):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": perception["screenshot_b64"],
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": f"{screen_text}\n\nClassify this video for the niche and, "
                "if relevant, write a fresh comment.",
            }
        )

        resp = await client.messages.create(
            model=self.config.anthropic_model,
            max_tokens=500,
            system=self._plan_system(used_comments),
            tools=self._plan_tool(),
            tool_choice={"type": "tool", "name": "plan_video"},
            messages=[{"role": "user", "content": content}],
        )
        for block in resp.content:
            if block.type == "tool_use":
                data = dict(block.input or {})
                return {
                    "on_feed": bool(data.get("on_feed")),
                    "screen": (data.get("screen") or "").strip(),
                    "post_type": data.get("post_type") or "",
                    "relevant": bool(data.get("relevant")),
                    "reason": data.get("reason", ""),
                    "language": (data.get("language") or "").strip(),
                    "comment": (data.get("comment") or "").strip(),
                }
        return {"on_feed": False, "screen": "unknown", "post_type": "", "relevant": False,
                "reason": "no plan returned", "language": "", "comment": ""}

    async def generate_campaign_comment(
        self, perception: dict, plan: dict, used_comments: list[str]
    ) -> str:
        """Write ONE high-quality comment with the stronger comment model.

        Decoupled from the fast per-frame classifier: it sees the video and
        writes a natural, in-language, on-brand comment. Only called when we're
        actually about to comment (rare), so the extra cost is negligible.
        """
        client = self.controller.ai.client
        lang = plan.get("language") or NICHE.get("language") or "the video's language"
        system = (
            f"You write a single TikTok comment as a real person, for the {NICHE['name']} "
            f"brand. {NICHE['name']} is a MOBILE APP: {NICHE['description']}\n\n"
            f"Write the comment in {lang}, in flawless, natural, native-sounding "
            "language (correct grammar, spelling, and umlauts/accents) -- it must read "
            "like a real local wrote it, never machine-translated.\n"
            + NICHE["comment_style"] +
            "\nMake it clear, in passing, that NutriSkin is an APP you use (you "
            "'scan' foods in it, 'track' your skin, follow its recipes) -- naturally, "
            "not as an ad. Output ONLY the comment text, nothing else."
        )
        parts = [f"This video: {plan.get('screen', '')}. Reason it's relevant: {plan.get('reason', '')}."]
        if used_comments:
            parts.append("Do NOT resemble any of these earlier comments:\n- " + "\n- ".join(used_comments))
        content: list[dict] = []
        if perception.get("screenshot_b64"):
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": perception["screenshot_b64"]}})
        content.append({"type": "text", "text": "\n\n".join(parts) + "\n\nWrite the comment."})
        try:
            resp = await client.messages.create(
                model=self.config.comment_model,
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the plan's comment
            logger.warning("strong comment generation failed: %s", exc)
            return (plan.get("comment") or "").strip()
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        # Strip surrounding quotes the model sometimes adds.
        return text.strip('"').strip("'").strip()

    @staticmethod
    def _looks_live(plan: dict) -> bool:
        """A LIVE stream, from post_type or the screen description. Lives are
        swiped past, never tapped -- tapping into one spirals recovery."""
        if plan.get("post_type") == "live":
            return True
        text = ((plan.get("screen") or "") + " " + (plan.get("reason") or "")).lower()
        return any(k in text for k in (
            "livestream", "live stream", "jetzt live", "live chat", "going live",
            "live session", "zuschauer", "viewers", "broadcast",
        ))

    @staticmethod
    def _language_matches(detected: str, target: str) -> bool:
        """Whether the detected content language matches the campaign target.
        Empty target = allow any; empty detection = don't over-filter."""
        if not target:
            return True
        d, t = detected.lower().strip(), target.lower().strip()
        if not d:
            return True
        alias = [
            {"german", "deutsch", "de"}, {"english", "en"}, {"polish", "polnisch", "pl"},
            {"spanish", "espanol", "español", "es"}, {"french", "francais", "français", "fr"},
            {"italian", "italiano", "it"}, {"portuguese", "portugues", "pt"},
            {"dutch", "nederlands", "nl"}, {"turkish", "türkisch", "tr"},
        ]
        for group in alias:
            if t in group and d in group:
                return True
        return d == t or d.startswith(t) or t.startswith(d)

    # ------------------------------------------------ UI exploration ---------
    # Interactive mapping: Claude looks at the current screen and reports the
    # controls it sees with pixel tap points, which the user verifies/corrects
    # and saves into the UI map. This is how we learn THIS phone's TikTok layout
    # instead of guessing fractional coordinates.

    DESCRIBE_TOOL = [
        {
            "name": "describe_screen",
            "description": "Report the TikTok screen type and every interactive UI control visible, each with a pixel tap point.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "screen_type": {
                        "type": "string",
                        "description": "One of: for_you_feed, slideshow_feed (a photo carousel post with slide-indicator dots), following_feed, profile, search_input, search_results, explore, comments_sheet, share_sheet, inbox_dm, live, settings, other.",
                    },
                    "description": {"type": "string", "description": "One line on what this screen is."},
                    "elements": {
                        "type": "array",
                        "description": "Each interactive control visible.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Canonical snake_case name, e.g. like_button, comment_button, share_button, save_button, repost_button, profile_avatar, follow_button, for_you_tab, following_tab, live_tab, search_icon, back_arrow, close_button, home_tab, friends_tab, inbox_tab, profile_tab, comment_input, post_button, caption, slide_indicator (the dots showing which slide of a photo slideshow you're on).",
                                },
                                "x": {"type": "integer", "description": "x pixel of the tap point."},
                                "y": {"type": "integer", "description": "y pixel of the tap point."},
                                "kind": {"type": "string", "enum": ["icon", "text", "tab", "button", "other"]},
                            },
                            "required": ["label", "x", "y"],
                        },
                    },
                },
                "required": ["screen_type", "description", "elements"],
            },
        }
    ]

    async def analyze_ui(self, device_id: str) -> dict:
        """Vision + OCR pass that maps the current screen's controls to pixels."""
        perception = await self.perceive(device_id)
        if perception.get("error"):
            return {"error": perception["error"]}

        device = await self.controller.get_device(device_id)
        w = (device.imgw or device.width) if device else 0
        h = (device.imgh or device.height) if device else 0

        elements = perception.get("elements", [])
        ocr_text = (
            "OCR text elements as 'text @ [x,y]' (reuse these exact coordinates "
            "for any labelled control):\n"
            + "\n".join(f"- {e['text']} @ {e['centre']}" for e in elements)
            if elements else "No OCR text available; estimate positions from the image."
        )
        system = (
            "You are mapping the TikTok app's interface on a phone so an "
            f"automation can navigate reliably. The screenshot is {w} pixels wide "
            f"and {h} pixels tall; give every coordinate in that pixel space "
            "(origin top-left). Identify the screen_type and list every "
            "interactive control you can see, each with a precise tap point. For "
            "controls that have OCR text, reuse that exact [x,y]. For icons "
            "(heart, speech bubble, arrow, avatar, tab-bar icons) estimate the "
            "centre of the tappable icon as accurately as you can. Use canonical "
            "snake_case labels. Call describe_screen."
        )
        content: list[dict] = []
        if perception.get("screenshot_b64"):
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": perception["screenshot_b64"]},
                }
            )
        content.append({"type": "text", "text": f"{ocr_text}\n\nMap this screen."})

        client = self.controller.ai.client
        resp = await client.messages.create(
            model=self.config.anthropic_model,
            max_tokens=1500,
            system=system,
            tools=self.DESCRIBE_TOOL,
            tool_choice={"type": "tool", "name": "describe_screen"},
            messages=[{"role": "user", "content": content}],
        )
        result = {"screen_type": "other", "description": "", "elements": []}
        for block in resp.content:
            if block.type == "tool_use":
                data = dict(block.input or {})
                result["screen_type"] = data.get("screen_type", "other")
                result["description"] = data.get("description", "")
                result["elements"] = data.get("elements", [])
        result["screenshot"] = perception.get("screenshot_b64")
        result["imgw"] = w
        result["imgh"] = h
        return result

    # ------------------------------------------- per-frame icon locating -----
    # TikTok's right-hand action rail (avatar / like / comment / bookmark /
    # share) SLIDES up or down depending on what's overlaid at the bottom -- a
    # "search this video" bar, an ad CTA, a tall caption, a pinned comment. A
    # fixed pixel from the UI map is right on one video and lands on the wrong
    # icon on the next. So before engaging we locate the rail on THIS frame with
    # vision; the whole rail moves together, so one call places every icon.

    LOCATE_TOOL = [
        {
            "name": "locate_controls",
            "description": (
                "Report the exact pixel tap point of each action-rail control "
                "visible on the current TikTok video."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "like_button": {"type": "array", "items": {"type": "integer"},
                        "description": "[x, y] centre of the HEART / like icon. Omit if not visible."},
                    "comment_button": {"type": "array", "items": {"type": "integer"},
                        "description": "[x, y] centre of the SPEECH-BUBBLE / comment icon. Omit if not visible."},
                    "save_button": {"type": "array", "items": {"type": "integer"},
                        "description": "[x, y] centre of the BOOKMARK / save icon. Omit if not visible."},
                    "share_button": {"type": "array", "items": {"type": "integer"},
                        "description": "[x, y] centre of the SHARE / arrow icon. Omit if not visible."},
                },
            },
        }
    ]

    @staticmethod
    def _is_count(text: str) -> bool:
        """True for a rail count like '1303', '54', '12.3K', '1,2Mio', '1.2万'."""
        import re
        t = (text or "").strip()
        return bool(re.match(r"^[\d][\d.,]*\s*(?:[KkMm]|万|Mio|Tsd)?\.?$", t))

    def _ocr_rail_points(self, perception: dict, w: int, h: int) -> dict:
        """Anchor rail icons to the OCR rail as a regular 4-slot grid.

        The rail is 4 evenly-spaced slots below the avatar -- like, comment,
        save, share -- and each icon sits just above its count NUMBER. OCR gives
        those numbers' exact centres. But a slot may show a WORD label (Teilen,
        Speichern) instead of a number, or a count may be missing, so we don't
        just map the detected numbers 1:1. Instead we:
          1. find the follow "+" (directly above the like slot) and the numeric
             counts, all on the right edge;
          2. derive the slot SPACING (default 107 at 608x1080) and the like-slot
             y (first count below the "+", else the topmost count);
          3. lay a 4-slot grid from there, snapping each slot to a real detected
             count when one is nearby (pixel-exact) and extrapolating otherwise.
        This yields all four icons even when some slots have word labels.
        """
        if not w or not h:
            return {}
        plus_y: Optional[int] = None
        counts: list[tuple[int, int]] = []
        word_slot: dict[int, int] = {}  # slot index (2=save, 3=share) -> y
        for e in perception.get("elements", []):
            cx, cy = e["centre"]
            if not (cx > w * 0.85 and h * 0.12 < cy < h * 0.90):
                continue
            raw = (e.get("text") or "").strip()
            t = raw.lower()
            if raw in ("+", "＋"):        # follow badge (ASCII or full-width +)
                plus_y = int(cy)
            elif self._is_count(raw):
                counts.append((int(cy), int(cx)))
            elif "teilen" in t or "share" in t or "senden" in t:
                word_slot[3] = int(cy)   # share is the bottom rail slot
            elif "speicher" in t or "favorit" in t or "merken" in t:
                word_slot[2] = int(cy)   # save is the third rail slot
        counts.sort()
        if not counts and plus_y is None and not word_slot:
            return {}

        railx = int(sum(cx for _, cx in counts) / len(counts)) if counts else int(w * 0.917)
        if not railx:
            railx = int(w * 0.917)

        # Slot spacing: the MEDIAN consecutive gap (robust to a spurious element
        # above/below the rail and to a missing count doubling one gap), and only
        # if it lands in the plausible rail range; else the known default.
        spacing = 0
        if len(counts) >= 2:
            gaps = sorted(b[0] - a[0] for a, b in zip(counts, counts[1:]))
            med = gaps[len(gaps) // 2]
            if 90 <= med <= 125:
                spacing = med
        if not spacing:
            spacing = 107
        offset = int(spacing * self.config.rail_icon_offset_factor)

        # Like-slot y (the y of the like COUNT). Anchor priority, most reliable
        # first: the follow "+" (directly above the like slot) -> a label word
        # like Teilen/Speichern (marks an ABSOLUTE slot, so it disambiguates
        # which count is which) -> the topmost count (assumes like has a count).
        like_y: Optional[int] = None
        if plus_y is not None:
            below = [cy for cy, _ in counts if cy > plus_y + spacing * 0.4]
            like_y = min(below) if below else int(plus_y + spacing)
        elif word_slot:
            slot = max(word_slot)  # prefer share (3) over save (2)
            like_y = int(word_slot[slot] - slot * spacing)
        elif counts:
            like_y = counts[0][0]
        if like_y is None:
            return {}

        labels = ["like_button", "comment_button", "save_button", "share_button"]
        points: dict[str, tuple[int, int]] = {}
        for i, label in enumerate(labels):
            slot_y = like_y + i * spacing
            near = [cy for cy, _ in counts if abs(cy - slot_y) <= spacing * 0.4]
            if near:  # snap to the real count for pixel accuracy
                slot_y = min(near, key=lambda cy: abs(cy - slot_y))
            icon_y = slot_y - offset
            if h * 0.10 < icon_y < h * 0.92:
                points[label] = (railx, int(icon_y))
        return points

    async def locate_actions(
        self, device_id: str, perception: Optional[dict] = None
    ) -> dict:
        """Locate the right-rail icons on the current frame.

        Prefers OCR count anchoring (pixel-exact); falls back to a vision guess
        only if OCR gave us no rail. Returns ``{label: (x, y)}`` for whichever of
        like/comment/save/share are found; ``{}`` on failure, so callers can then
        fall back to the UI map.
        """
        if perception is None:
            perception = await self.perceive(device_id)

        device = await self.controller.get_device(device_id)
        w = (device.imgw or device.width) if device else 0
        h = (device.imgh or device.height) if device else 0

        # 1) OCR anchoring -- exact, shift-proof.
        ocr_pts = self._ocr_rail_points(perception, w, h)
        if ocr_pts.get("like_button"):
            self._last_locate_source = "ocr"
            return ocr_pts

        # 2) Vision fallback (approximate) when OCR has no rail counts.
        shot = perception.get("screenshot_b64")
        if not shot:
            return {}
        self._last_locate_source = "vision"

        elements = perception.get("elements", [])
        ocr_hint = (
            "OCR text with positions (the small NUMBER under each icon is its "
            "count -- the icon sits just ABOVE its count):\n"
            + "\n".join(f"- {e['text']} @ {e['centre']}" for e in elements)
            if elements else "No OCR text available; read the icons from the image."
        )
        system = (
            f"The screenshot is {w} pixels wide and {h} pixels tall; give every "
            "coordinate in that exact pixel space (origin top-left). Find the "
            "vertical ACTION RAIL on the RIGHT edge of a TikTok video: from top "
            "to bottom it is the round profile avatar, then the HEART (like), the "
            "SPEECH BUBBLE (comment), the BOOKMARK (save), and the SHARE arrow. "
            "This rail slides up or down between videos depending on bottom "
            "overlays, so read the real position from THIS image -- do not assume "
            "fixed spots. Return the centre [x, y] of each icon you can clearly "
            "see; omit any that are not visible. Call locate_controls."
        )
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": shot}},
            {"type": "text", "text": f"{ocr_hint}\n\nLocate the action-rail icons."},
        ]
        client = self.controller.ai.client
        try:
            resp = await client.messages.create(
                model=self.config.anthropic_model,
                max_tokens=400,
                system=system,
                tools=self.LOCATE_TOOL,
                tool_choice={"type": "tool", "name": "locate_controls"},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001 - never let locating break a step
            logger.warning("locate_actions failed: %s", exc)
            return {}

        points: dict[str, tuple[int, int]] = {}
        for block in resp.content:
            if block.type == "tool_use":
                data = dict(block.input or {})
                for label in ("like_button", "comment_button", "save_button", "share_button"):
                    xy = data.get(label)
                    if isinstance(xy, (list, tuple)) and len(xy) == 2:
                        x, y = int(xy[0]), int(xy[1])
                        # Sanity: inside the frame and in the right-hand rail zone.
                        if w and h and 0 <= x <= w and 0 <= y <= h and x > w * 0.6:
                            points[label] = (x, y)
        return points

    async def locate_one(
        self, screenshot_b64: str, w: int, h: int, what: str
    ) -> Optional[tuple[int, int]]:
        """Vision-locate a single named control on an arbitrary screenshot.

        Used to calibrate off-app screens (e.g. Control Center for mirror
        re-cast). Returns (x, y) in the imgw x imgh space, or None if not found.
        """
        if not screenshot_b64:
            return None
        tool = [{
            "name": "locate",
            "description": f"Report the pixel tap point of {what}.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "found": {"type": "boolean"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["found"],
            },
        }]
        system = (
            f"The screenshot is {w} pixels wide and {h} tall (origin top-left). "
            f"Find {what} and give its centre tap point in that exact pixel space. "
            "Call locate; set found=false if it is not visible."
        )
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot_b64}},
            {"type": "text", "text": f"Locate: {what}."},
        ]
        try:
            resp = await self.controller.ai.client.messages.create(
                model=self.config.anthropic_model,
                max_tokens=200,
                system=system,
                tools=tool,
                tool_choice={"type": "tool", "name": "locate"},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("locate_one failed: %s", exc)
            return None
        for block in resp.content:
            if block.type == "tool_use":
                d = dict(block.input or {})
                if d.get("found") and d.get("x") is not None and d.get("y") is not None:
                    x, y = int(d["x"]), int(d["y"])
                    if 0 <= x <= w and 0 <= y <= h:
                        return (x, y)
        return None

    # ------------------------------------------------ vision recovery --------
    # Blind fixed-coordinate taps are dangerous (top-left = LIVE, wrong bottom
    # tab = DMs/profile), so recovery is vision-guided: Claude looks at the stuck
    # screen and picks ONE navigation control to escape toward the feed.

    RECOVER_TOOL = [
        {
            "name": "recover",
            "description": "Choose ONE action to move back toward the TikTok For You feed from the current (non-feed) screen.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["tap", "hardware_back", "swipe_down"],
                        "description": "tap = tap a specific navigation control (a top-left back arrow, an X/Close/Cancel button, or the Home/For-You tab in the bottom bar). hardware_back = press the phone's Back. swipe_down = pull down to dismiss an open comment/share sheet.",
                    },
                    "x": {"type": "integer", "description": "x pixel to tap (method=tap only), in the screenshot's pixel space."},
                    "y": {"type": "integer", "description": "y pixel to tap (method=tap only)."},
                    "note": {"type": "string", "description": "Briefly, what you are tapping or doing."},
                },
                "required": ["method", "note"],
            },
        }
    ]

    async def recover(self, device_id: str, perception: dict) -> dict:
        """Ask Claude how to escape a non-feed screen toward the For You feed."""
        client = self.controller.ai.client
        device = await self.controller.get_device(device_id)
        w = (device.imgw or device.width) if device else 0
        h = (device.imgh or device.height) if device else 0

        elements = perception.get("elements", [])
        screen_text = (
            "On-screen text elements as 'text @ [x,y]' (you can tap any of these "
            "coordinates exactly):\n"
            + "\n".join(f"- {e['text']} @ {e['centre']}" for e in elements)
            if elements else "No OCR text; judge positions from the image."
        )
        system = (
            "You are helping an automation get back to the TikTok For You feed (a "
            f"single full-screen video with like/comment/share on the right). The "
            f"screenshot is {w} pixels wide and {h} pixels tall. Look at the screen "
            "and choose ONE action to move toward that feed.\n"
            "- 'tap' a clear navigation control and give its pixel (x,y). If the "
            "control has an OCR text entry above (e.g. Cancel/Abbrechen/Back), reuse "
            "its exact [x,y]. Otherwise estimate from the image (e.g. a top-left "
            "back arrow, an X/Close button, or the Home/For-You tab in the bottom "
            "bar).\n"
            "- 'swipe_down' to dismiss an open comment or share sheet.\n"
            "- 'hardware_back' if there is no obvious control.\n"
            "NEVER tap the LIVE button, a profile, a video, a link, a follow button, "
            "or any content -- ONLY a back/close/navigation control. Call recover."
        )
        content: list[dict] = []
        if perception.get("screenshot_b64"):
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": perception["screenshot_b64"]},
                }
            )
        content.append({"type": "text", "text": f"{screen_text}\n\nChoose the recovery action."})

        try:
            resp = await client.messages.create(
                model=self.config.anthropic_model,
                max_tokens=300,
                system=system,
                tools=self.RECOVER_TOOL,
                tool_choice={"type": "tool", "name": "recover"},
                messages=[{"role": "user", "content": content}],
            )
            for block in resp.content:
                if block.type == "tool_use":
                    return dict(block.input or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("recover decision failed: %s", exc)
        return {"method": "hardware_back", "note": "fallback"}

    async def apply_recovery(self, device_id: str, move: dict) -> None:
        """Execute a recovery move chosen by :meth:`recover`."""
        method = move.get("method")
        try:
            if method == "tap" and move.get("x") is not None and move.get("y") is not None:
                await self.controller.api.click(device_id, int(move["x"]), int(move["y"]))
            elif method == "swipe_down":
                await self.controller.api.swipe(device_id, "down", 0.6)
            else:  # hardware_back / fallback
                await self.controller.api.send_key(device_id, fn_key="TAB+b")
        except (IMouseError, ValueError, TypeError) as exc:
            logger.warning("apply_recovery failed: %s", exc)
        await asyncio.sleep(self.config.action_delay)

    # ------------------------------------------------ human-like timing ------
    def _rand(self, lo: float, hi: float) -> float:
        return random.uniform(lo, hi)

    def _watch_relevant(self) -> float:
        c = self.config.campaign
        if random.random() < c["watch_relevant_long_chance"]:
            return self._rand(*c["watch_relevant_long"])
        return self._rand(*c["watch_relevant"])

    def _watch_irrelevant(self) -> float:
        c = self.config.campaign
        if random.random() < c["watch_irrelevant_long_chance"]:
            return self._rand(*c["watch_irrelevant_long"])
        return self._rand(*c["watch_irrelevant"])

    def _pick_actions(self, has_comment: bool, comment_allowed: bool) -> list[str]:
        c = self.config.campaign
        core = []
        if random.random() < c["like_probability"]:
            core.append("like")
        if random.random() < c["save_probability"]:
            core.append("save")
        random.shuffle(core)  # vary like/save order
        # Comment ALWAYS goes last: it opens the comment sheet, and running it
        # before like/save left those taps hitting stale coordinates (Follow).
        if has_comment and comment_allowed and random.random() < c["comment_probability"]:
            core.append("comment")
        return core

    # TikTok UI chrome that appears on EVERY feed frame -- must be excluded from
    # the post fingerprint or it dominates and makes different videos look alike
    # (and the clock/nav jitter makes the same video look different).
    UI_STOP = {
        "community", "entdecken", "furdich", "dich", "gefolgt", "folgen", "folgeich",
        "home", "posteingang", "profil", "shop", "freunde", "jetzt", "live",
        "suchen", "following", "foryou", "friends", "inbox", "profile", "discover",
        "simfehlt", "gefolgtshop", "shopfurdich", "communitygefolgt", "mehr",
    }

    @classmethod
    def _post_words(cls, perception: dict) -> set:
        """Caption/username words identifying the current POST -- with the TikTok
        nav bar, status bar, and clock stripped out. Compared fuzzily, this
        reliably tells one video from another despite OCR jitter."""
        words = set()
        for e in perception.get("elements", []):
            for raw in (e.get("text") or "").lower().split():
                if any(ch.isdigit() for ch in raw):
                    continue  # drop clock (943am), counts, times
                w = "".join(ch for ch in raw if ch.isalnum())
                if len(w) >= 4 and w not in cls.UI_STOP:
                    words.add(w)
        return words

    @staticmethod
    def _same_post(a: set, b: set) -> bool:
        """True if two word sets describe the same post (fuzzy: shared caption
        words), tolerating OCR noise between captures."""
        if not a or not b:
            return False
        inter = len(a & b)
        return inter >= 3 and inter / len(a | b) >= 0.45

    async def _advance_feed(self, device_id: str, current_words: set) -> None:
        """Advance to the next post -- ONE firm swipe, then confirm.

        Rapid repeat swipes land mid-transition and glitch the feed back to the
        same video, so we never fire two swipes close together: after a swipe we
        wait for a SETTLED frame (mirror caught up), and only if it's still the
        same post do we try once more. The cross-cycle dedup catches anything
        that still slips through, without ever re-reacting."""
        api = self.controller.api
        # Try gestures in order, VERIFYING each against the clean caption (no UI
        # chrome), settling fully between attempts so none land mid-transition.
        # Whatever actually moves the feed wins; we stop as soon as it changes.
        attempts = [
            lambda: api.swipe(device_id, "up", 0.75),
            lambda: api.swipe(device_id, "up", 0.9),
            lambda: api.wheel(device_id, "down", 0.6, 5),
        ]
        for act in attempts:
            try:
                await act()
            except IMouseError:
                continue
            await asyncio.sleep(self.config.post_swipe_settle)
            perc = await self.settled_perception(device_id, tries=2)
            if perc.get("error"):
                self._campaign["next_perc"] = None
                return
            if not (current_words and self._same_post(self._post_words(perc), current_words)):
                # Advanced. Hand this already-settled frame to the next cycle so
                # it doesn't re-capture -- a big per-video latency saving.
                self._campaign["next_perc"] = perc
                return
        self._campaign["next_perc"] = None

    def _comment_allowed(self) -> bool:
        """Sparse, non-spammy commenting: only once the randomized cooldown has
        passed and under the per-run cap."""
        st, c = self._campaign, self.config.campaign
        if st["stats"]["comments"] >= c.get("comment_max_per_session", 8):
            return False
        if st["comment_gap"] <= 0:  # first time -- set an initial cooldown target
            st["comment_gap"] = random.randint(*c["comment_min_gap"])
        return st["videos_since_comment"] >= st["comment_gap"]

    async def _sleep(self, seconds: float) -> None:
        """Cancellable sleep: wakes early when a stop is requested."""
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end and not self._campaign["stop"]:
            await asyncio.sleep(min(0.5, end - time.monotonic()))

    # -------------------------------------------------- slideshow browsing ---
    @staticmethod
    def _slide_count(perception: dict) -> Optional[int]:
        """Read the total slide count from an 'N/M' indicator in the OCR."""
        import re
        for el in perception.get("elements", []):
            m = re.search(r"\b\d+\s*/\s*(\d+)\b", el.get("text", ""))
            if m:
                total = int(m.group(1))
                if 2 <= total <= 20:
                    return total
        return None

    async def _browse_slideshow(self, device_id: str, perception: dict, view_all: bool) -> int:
        """Swipe through a photo slideshow's slides. All of them when ``view_all``
        (we're about to comment), otherwise a random subset. Returns slides viewed.
        """
        c = self.config.campaign
        direction = c.get("slideshow_swipe_direction", "right")
        total = self._slide_count(perception)
        if view_all:
            count = (total - 1) if total else c["slideshow_max_slides"]
        else:
            upper = (total - 1) if total else 4
            count = random.randint(1, max(1, upper))
        count = min(count, c["slideshow_max_slides"])

        viewed = 0
        for _ in range(count):
            if self._campaign["stop"]:
                break
            try:
                await self.controller.api.swipe(device_id, direction, 0.6)
            except IMouseError:
                break
            viewed += 1
            await self._sleep(self._rand(*c["slide_dwell"]))
        return viewed

    # ---------------------------------------------------- per-video flow -----
    async def _campaign_video(self, device_id: str) -> dict:
        st = self._campaign
        c = self.config.campaign

        # Reuse the settled frame the last advance already captured, if any --
        # otherwise wait for a fresh settled frame (the mirror lags the phone, so
        # a stale transitional capture must not drive the decision).
        perception = st.get("next_perc")
        st["next_perc"] = None
        if perception is None:
            perception = await self.settled_perception(device_id)
        st["last_screenshot"] = perception.get("screenshot_b64")  # live view for UI
        if perception.get("error"):
            st["stats"]["errors"] += 1
            st["error_streak"] += 1
            # code 25 = the AirPlay mirror dropped (control still works over USB,
            # so checking device online-state is useless -- a screenshot is the
            # only real test). Try to re-cast the mirror from the phone side.
            mirror_down = perception.get("error_code") == 25
            entry = {"on_feed": None, "relevant": None, "actions": [],
                     "reason": perception["error"], "watch": 0,
                     "screen": "mirror dropped" if mirror_down else "screenshot failed"}
            recovered = False
            if mirror_down:
                try:
                    recovered = await self.controller.recover_mirror(device_id)
                except Exception:  # noqa: BLE001
                    recovered = False
            if recovered:
                st["error_streak"] = 0
                entry["actions"].append("re-cast mirror OK")
                return entry
            # Circuit breaker: never hammer a dead mirror unattended. Stop after a
            # short streak of unrecovered errors with a clear status.
            if st["error_streak"] >= 6:
                st["stop"] = True
                why = ("mirror down; re-cast unavailable (not calibrated) or failed"
                       if mirror_down else "screenshot errors not clearing")
                entry["actions"].append(f"STOPPED ({why})")
            else:
                await self._sleep(min(2.0 + st["error_streak"], 8.0))  # backoff
            return entry
        st["error_streak"] = 0

        try:
            plan = await self.plan_video(perception, st["used_comments"][-6:])
        except Exception as exc:  # noqa: BLE001
            st["stats"]["errors"] += 1
            await self._sleep(2.0)
            return {"on_feed": None, "screen": "plan failed", "relevant": None,
                    "reason": f"plan failed: {exc}", "actions": [], "watch": 0}

        # A LIVE stream (whether it reads as an in-feed card or a chat screen) is
        # escaped by SWIPING PAST it, never by tapping -- tapping into a live is
        # what spiralled recovery into the explore grid. Handle it before both
        # the off-feed recovery and any engagement.
        if self._looks_live(plan):
            st["off_feed_streak"] = 0
            await self._advance_feed(device_id, self._post_words(perception))
            return {"on_feed": True, "screen": plan["screen"], "post_type": "live",
                    "relevant": False, "reason": plan["reason"],
                    "actions": ["skip (live -> swipe past)"], "watch": 0}

        # Off the feed (profile, sheet, search, …): recover, don't engage.
        if not plan["on_feed"]:
            st["stats"]["recovered"] += 1
            st["off_feed_streak"] += 1
            move = await self.recover(device_id, perception)
            # Escalate: vision taps that keep failing (e.g. an explore grid where
            # the "Für dich" it taps is just a filter) get overridden -- hardware
            # Back, then a Home-tab tap, to break out instead of looping forever.
            if st["off_feed_streak"] >= 4:
                move = {"method": "hardware_back", "note": "escalated: back"}
            await self.apply_recovery(device_id, move)
            if st["off_feed_streak"] >= 6:  # last resort: swipe up to leave a grid
                try:
                    await self.controller.api.swipe(device_id, "up", 0.8)
                except IMouseError:
                    pass
            await self._sleep(self._rand(1.2, 2.5))
            entry = {
                "on_feed": False, "screen": plan["screen"], "relevant": None,
                "reason": plan["reason"],
                "actions": [f"recover:{move.get('method', '?')} (try {st['off_feed_streak']})"],
                "recovery": move.get("note", ""), "watch": 0,
            }
            # Safety net: never spiral unsupervised. Stop sooner than before.
            if st["off_feed_streak"] >= 8:
                st["stop"] = True
                entry["actions"].append("STOPPED (stuck off-feed)")
            return entry

        st["off_feed_streak"] = 0
        st["stats"]["videos"] += 1

        # Identify the post (caption words) and check if we've already handled it
        # this session -- if so, never engage it again (that caused the re-liking
        # and unliking), just move on.
        words = self._post_words(perception)
        already = any(self._same_post(words, seen) for seen in st["engaged_posts"])

        # Language gate: only engage content in the configured language.
        target_lang = NICHE.get("language")
        off_language = bool(target_lang) and not self._language_matches(
            plan.get("language", ""), target_lang
        )

        if not plan["relevant"] or already or off_language:
            fast = (not plan["relevant"]) or off_language
            watch = self._watch_irrelevant() if fast else self._rand(0.8, 2.0)
            await self._sleep(watch)
            await self._advance_feed(device_id, words)
            reason = ("off-language: " + (plan.get("language") or "?")) if off_language else (
                "already engaged" if already else "irrelevant")
            actions = [f"skip ({reason})"] if (already or off_language) else []
            return {"on_feed": True, "screen": plan["screen"], "post_type": plan.get("post_type", ""),
                    "relevant": bool(plan["relevant"]), "reason": plan["reason"],
                    "actions": actions, "watch": round(watch, 1)}

        # New relevant video: mark it engaged NOW (before acting) so we never
        # double-handle it, then decide reactions ONCE, do them, and always
        # advance to the next post at the end.
        st["stats"]["relevant"] += 1
        st["videos_since_comment"] += 1
        if words:
            st["engaged_posts"].append(words)
            st["engaged_posts"] = st["engaged_posts"][-150:]
        watch = self._watch_relevant()
        await self._sleep(watch * self._rand(0.3, 0.6))

        # Decide engagement up front so a slideshow can browse to the end when
        # we're going to comment (per requirement), or a random subset otherwise.
        planned = self._pick_actions(
            has_comment=True,  # comment text is generated on demand (strong model)
            comment_allowed=self._comment_allowed(),
        )
        slides_viewed = 0
        if plan.get("post_type") == "photo_slideshow":
            slides_viewed = await self._browse_slideshow(
                device_id, perception, view_all=("comment" in planned)
            )

        # Locate the action rail on this frame once (it shifts per video); reuse
        # the plan's screenshot so there's no extra capture. All engagement taps
        # for this video use these live coordinates, falling back to the map.
        pts = {}
        if planned:
            pts = await self.locate_actions(device_id, perception)

        done = []
        posted_comment = ""
        for action in planned:
            if st["stop"]:
                break
            if action == "like":
                if (await self.controller.like_video(
                    device_id, point=pts.get("like_button")
                )).success:
                    st["stats"]["likes"] += 1
                    done.append("like")
            elif action == "save":
                if (await self.controller.save_video(
                    device_id, point=pts.get("save_button")
                )).success:
                    st["stats"]["saves"] += 1
                    done.append("save")
            elif action == "comment":
                # Generate the comment now, with the stronger model, in-language.
                text = await self.generate_campaign_comment(
                    perception, plan, st["used_comments"][-8:]
                )
                if not text:
                    continue
                res = await self.controller.comment_on_video(
                    device_id, comment=text, use_ai=False,
                    comment_button_point=pts.get("comment_button"),
                )
                if res.success:
                    st["stats"]["comments"] += 1
                    st["used_comments"].append(text)
                    done.append("comment")
                    posted_comment = text
                    # Reset the cooldown to a fresh randomized gap so comments
                    # stay spread out and never cluster into something spammy.
                    st["videos_since_comment"] = 0
                    st["comment_gap"] = random.randint(*c["comment_min_gap"])
            await self._sleep(self._rand(*c["between_actions"]))

        # Linger a touch, then ALWAYS advance to the next post -- verified, so a
        # swipe the feed ignores can't leave us re-processing this same video.
        await self._sleep(self._watch_relevant_remainder(watch))
        await self._advance_feed(device_id, words)
        return {
            "on_feed": True,
            "screen": plan["screen"],
            "post_type": plan.get("post_type", ""),
            "relevant": True,
            "reason": plan["reason"],
            "actions": (done + [f"viewed {slides_viewed} slides"]) if slides_viewed else done,
            "comment": posted_comment,
            "watch": round(watch, 1),
        }

    def _watch_relevant_remainder(self, watch: float) -> float:
        # A little extra dwell after engaging, jittered.
        return max(0.0, self._rand(1.0, watch * 0.4))

    # -------------------------------------------------------- run / status ---
    async def campaign_loop(self, device_id: str, duration_seconds: int) -> dict:
        st = self._campaign = self._fresh_campaign_state()
        st.update(running=True, device_id=device_id, duration=duration_seconds)
        start = time.monotonic()
        # Persist every run (steps + frames + errors) for later review/improvement.
        session = SessionLogger(device_id, NICHE.get("name", "run"))
        st["log_session"] = session.name
        logger.info("Campaign started on %s for %ss -> logs/%s",
                    device_id, duration_seconds, session.name)
        try:
            while (time.monotonic() - start) < duration_seconds and not st["stop"]:
                entry = await self._campaign_video(device_id)
                entry["t"] = round(time.monotonic() - start, 1)
                st["log"].append(entry)
                st["log"] = st["log"][-200:]
                st["elapsed"] = round(time.monotonic() - start, 1)
                session.log(entry, st.get("last_screenshot"))
        finally:
            st["running"] = False
            st["elapsed"] = round(time.monotonic() - start, 1)
            session.finish(st["stats"])
            logger.info("Campaign ended: %s (logs/%s)", st["stats"], session.name)
        return {"stats": st["stats"], "videos": st["stats"]["videos"], "session": session.name}

    def request_stop(self) -> None:
        self._campaign["stop"] = True

    def campaign_status(self) -> dict:
        st = self._campaign
        remaining = max(0.0, st["duration"] - st["elapsed"]) if st["running"] else 0.0
        return {
            "running": st["running"],
            "device_id": st["device_id"],
            "duration": st["duration"],
            "elapsed": st["elapsed"],
            "remaining": round(remaining, 1),
            "stats": dict(st["stats"]),
            "recent": st["log"][-40:],
            "last_screenshot": st.get("last_screenshot"),
            "log_session": st.get("log_session"),
        }

```

## `backend/main.py`

```python
"""FastAPI server for the TikTok controller web interface."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_agent import TikTokAgent
from api_client import IMouseClient, IMouseError
from config import Config
from device_manager import DeviceRegistry
from tiktok_actions import TikTokController
from ui_map import load_map, save_screen

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

config = Config.from_env()
client: Optional[IMouseClient] = None
registry: Optional[DeviceRegistry] = None
controller: Optional[TikTokController] = None
agent: Optional[TikTokAgent] = None
campaign_task: Optional[asyncio.Task] = None
ws_clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, registry, controller, agent
    logger.info("iMouse API: %s (Claude model: %s)", config.http_base_url, config.anthropic_model)

    client = IMouseClient(config)
    await client.__aenter__()
    registry = DeviceRegistry(client)
    controller = TikTokController(config, client, registry)
    agent = TikTokAgent(controller)

    try:
        await registry.refresh()
    except IMouseError as exc:
        logger.warning("Initial device refresh failed (is the console running?): %s", exc)

    logger.info("Application started")
    yield
    await client.aclose()
    logger.info("Application stopped")


app = FastAPI(
    title="TikTok Accessibility Controller",
    description="Control TikTok on iOS devices via iMouse XP",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ models ----
class ConfigUpdate(BaseModel):
    imouse_host: Optional[str] = None
    imouse_port: Optional[int] = None
    anthropic_api_key: Optional[str] = None
    anthropic_model: Optional[str] = None


class CommentRequest(BaseModel):
    device_id: str
    comment: Optional[str] = None
    use_ai: bool = True
    ai_style: str = "friendly"


class BatchActionRequest(BaseModel):
    device_ids: list[str]
    action: str


class GenerateCommentRequest(BaseModel):
    device_id: str
    count: int = 3
    styles: Optional[list[str]] = None


class AutoBrowseRequest(BaseModel):
    device_id: str
    video_count: int = 10
    like_probability: float = 0.3
    comment_probability: float = 0.1
    min_watch_time: float = 3.0
    max_watch_time: float = 15.0


class UICalibrationUpdate(BaseModel):
    element: str
    x: float
    y: float
    w: Optional[float] = None
    h: Optional[float] = None


class AIAgentRequest(BaseModel):
    goal: Optional[str] = None
    max_steps: int = 10


class CampaignRequest(BaseModel):
    device_id: str
    duration_minutes: float = 30.0
    language: Optional[str] = None
    niche: Optional[str] = None


class UIMapSave(BaseModel):
    screen_type: str
    elements: dict  # {label: [x, y]}
    imgw: int = 0
    imgh: int = 0


class TapRequest(BaseModel):
    x: int
    y: int


class ManualTypeRequest(BaseModel):
    text: str


class ManualKeyRequest(BaseModel):
    key: Optional[str] = None
    fn_key: Optional[str] = None


class RawCommand(BaseModel):
    fun: str
    data: Optional[dict] = None


# -------------------------------------------------------------- diagnostics ---
@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": app.version}


@app.get("/api/debug/connection")
async def debug_connection():
    """Probe the iMouse console and report what came back."""
    info = {
        "base_url": config.http_base_url,
        "reachable": False,
        "device_count": 0,
        "online_count": 0,
        "error": None,
    }
    try:
        devices = await registry.refresh()
        info["reachable"] = True
        info["device_count"] = len(devices)
        info["online_count"] = len(registry.online_devices)
        info["devices"] = [d.to_dict() for d in devices]
    except IMouseError as exc:
        info["error"] = str(exc)
    return info


@app.post("/api/debug/raw-command")
async def debug_raw_command(cmd: RawCommand):
    """Send an arbitrary ``fun`` to the API for testing."""
    try:
        result = await client.call(cmd.fun, **(cmd.data or {}))
        return {"success": True, "fun": cmd.fun, "data": result.data}
    except IMouseError as exc:
        return {"success": False, "fun": cmd.fun, "code": exc.code, "message": str(exc)}


# ------------------------------------------------------------------ config ----
@app.get("/api/config")
async def get_config():
    return {
        "imouse_host": config.imouse_host,
        "imouse_port": config.imouse_port,
        "anthropic_model": config.anthropic_model,
        "has_anthropic_key": bool(config.anthropic_api_key),
        "tiktok_ui": config.tiktok_ui,
    }


@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    if update.imouse_host:
        config.imouse_host = update.imouse_host
    if update.imouse_port:
        config.imouse_port = update.imouse_port
    if update.imouse_host or update.imouse_port:
        client.base_url = config.http_base_url
    if update.anthropic_api_key:
        config.anthropic_api_key = update.anthropic_api_key
        controller.ai.set_api_key(update.anthropic_api_key)
    if update.anthropic_model:
        config.anthropic_model = update.anthropic_model
    return {"status": "ok"}


@app.post("/api/config/ui")
async def update_ui_calibration(update: UICalibrationUpdate):
    entry = config.tiktok_ui.setdefault(update.element, {})
    entry["x"] = update.x
    entry["y"] = update.y
    if update.w is not None:
        entry["w"] = update.w
    if update.h is not None:
        entry["h"] = update.h
    return {"status": "ok", "element": update.element}


# ------------------------------------------------------------------ devices ---
@app.get("/api/devices")
async def get_devices():
    try:
        await registry.refresh()
    except IMouseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"devices": [d.to_dict() for d in registry.devices]}


@app.get("/api/devices/{device_id}/screenshot")
async def get_screenshot(device_id: str):
    try:
        return {"screenshot": await controller.get_screenshot_b64(device_id)}
    except IMouseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/devices/{device_id}/analyze")
async def analyze_video(device_id: str):
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    context = await controller.analyze_current_video(device_id)
    return {
        "caption": context.caption,
        "hashtags": context.hashtags,
        "username": context.username,
        "ocr_text": context.ocr_text,
        "has_screenshot": bool(context.screenshot_base64),
    }


# ------------------------------------------------------------------ actions ---
def _result(res) -> dict:
    return {"success": res.success, "message": res.message, **res.data}


@app.post("/api/devices/{device_id}/swipe/next")
async def swipe_next(device_id: str):
    return _result(await controller.swipe_next(device_id))


@app.post("/api/devices/{device_id}/swipe/previous")
async def swipe_previous(device_id: str):
    return _result(await controller.swipe_previous(device_id))


async def _rail_point(device_id: str, label: str):
    """Vision-locate one rail icon on the current frame (the accurate anchoring
    the campaign uses), so manual buttons hit the same spot -- not stale config
    fractions where 'comment' lands on the save icon."""
    try:
        pts = await agent.locate_actions(device_id)
        return pts.get(label)
    except Exception:  # noqa: BLE001
        return None


@app.post("/api/devices/{device_id}/like")
async def like_video(device_id: str, double_tap: bool = True):
    pt = await _rail_point(device_id, "like_button")
    return _result(await controller.like_video(device_id, use_double_tap=double_tap, point=pt))


@app.post("/api/devices/{device_id}/save")
async def save_video(device_id: str):
    pt = await _rail_point(device_id, "save_button")
    return _result(await controller.save_video(device_id, point=pt))


@app.post("/api/devices/{device_id}/follow")
async def follow_creator(device_id: str):
    return _result(await controller.follow_creator(device_id))


@app.post("/api/devices/{device_id}/comment")
async def comment_on_video(device_id: str, request: CommentRequest):
    pt = await _rail_point(device_id, "comment_button")
    res = await controller.comment_on_video(
        device_id,
        comment=request.comment,
        use_ai=request.use_ai,
        ai_style=request.ai_style,
        comment_button_point=pt,
    )
    return _result(res)


@app.post("/api/devices/{device_id}/generate-comments")
async def generate_comments(device_id: str, request: GenerateCommentRequest):
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    comments = await controller.generate_comment_options(device_id, count=request.count)
    return {"comments": comments}


# -------------------------------------------------------- tap test / debug ----
def _jpeg_size(data: bytes):
    """Return (width, height) of a JPEG by reading its SOF marker, or None."""
    i, n = 2, len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xC3:
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return (w, h)
        if 0xD0 <= marker <= 0xD9 or marker == 0x01:
            i += 2
            continue
        i += 2 + ((data[i + 2] << 8) | data[i + 3])
    return None


@app.post("/api/devices/{device_id}/tap")
async def tap(device_id: str, req: TapRequest):
    """Tap an exact pixel -- used by the frontend Tap Test to verify coordinates.

    Routed through the controller's settled click path (the same one the
    campaign uses) so the Tap Test measures real behaviour, including the
    pointer-settle that guards against relative-mouse drift.
    """
    result = await controller._click(device_id, req.x, req.y)
    return {"success": result.success, "x": req.x, "y": req.y, "message": result.message}


@app.get("/api/debug/screenshot-info")
async def debug_screenshot_info():
    """Same as screenshot-info but auto-picks the first online device (no ID to
    paste). Open http://localhost:8000/api/debug/screenshot-info directly."""
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    return await screenshot_info(online[0].device_id)


@app.get("/api/debug/ocr")
async def debug_ocr():
    """Check whether the PaddleOCR plugin is running, on the first online device.

    Open http://localhost:8000/api/debug/ocr directly in the browser.
    ``ocr_running: true`` with a non-zero ``elements`` count means OCR works and
    we can anchor taps to exact text coordinates. ``code: 28`` means the plugin
    process is not connected.
    """
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    device_id = online[0].device_id
    try:
        result = await controller.api.ocr(device_id)
    except IMouseError as exc:
        return {
            "ocr_running": False,
            "code": exc.code,
            "message": str(exc),
            "hint": "code 28 = PaddleOCR plugin not connected; start it in the console.",
        }
    items = result.items
    sample = [
        {"text": i.get("text"), "centre": i.get("centre") or i.get("center")}
        for i in items[:12]
    ]
    return {"ocr_running": True, "code": 0, "elements": len(items), "sample": sample}


@app.post("/api/debug/swipe-test")
async def swipe_test():
    """Empirically find what actually advances the TikTok feed on this setup.

    Tries several gestures in sequence and reports, for each, whether the video's
    caption changed (i.e. the feed moved). Run it on a video the campaign got
    stuck on. Whatever shows changed=true is the gesture we should use.
    """
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    did = online[0].device_id

    async def words() -> set:
        return agent._post_words(await agent.perceive(did))

    async def do_swipe(direction, length):
        await client.swipe(did, direction, length)

    async def do_wheel(direction, number):
        await client.wheel(did, direction, 0.5, number)

    methods = [
        ("swipe up 0.5", lambda: do_swipe("up", 0.5)),
        ("swipe up 0.75", lambda: do_swipe("up", 0.75)),
        ("swipe up 0.95", lambda: do_swipe("up", 0.95)),
        ("swipe down 0.75", lambda: do_swipe("down", 0.75)),
        ("wheel down x5", lambda: do_wheel("down", 5)),
        ("wheel up x5", lambda: do_wheel("up", 5)),
    ]
    prev = await words()
    steps = []
    for name, fn in methods:
        try:
            await fn()
        except IMouseError as exc:
            steps.append({"method": name, "error": str(exc)})
            continue
        await asyncio.sleep(1.8)  # let the mirror settle
        after = await words()
        # Moved = the cleaned caption is NOT the same post (fuzzy), so UI/clock
        # jitter doesn't count as movement.
        moved = not agent._same_post(after, prev) if (after and prev) else (after != prev)
        steps.append({"method": name, "moved": moved,
                      "caption_after": " ".join(sorted(after))[:120]})
        prev = after
    return {"steps": steps,
            "hint": "moved:True means that gesture actually advanced the feed."}


@app.get("/api/debug/rail")
async def debug_rail():
    """Show exactly how the rail locator sees the current frame.

    Returns every right-edge OCR element (the candidate counts), which ones were
    treated as counts, and the icon points the locator computed. Open this on a
    frame where a tap missed to see why the like/comment mapping was off.
    """
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    device_id = online[0].device_id
    device = registry.get(device_id)
    w = (device.imgw or device.width) if device else 0
    h = (device.imgh or device.height) if device else 0

    perception = await agent.perceive(device_id)
    right_edge = [
        {"text": e["text"], "centre": e["centre"], "is_count": agent._is_count(e["text"])}
        for e in perception.get("elements", [])
        if e["centre"][0] > w * 0.85
    ]
    right_edge.sort(key=lambda e: e["centre"][1])
    points = agent._ocr_rail_points(perception, w, h)
    return {
        "imgw": w,
        "imgh": h,
        "right_edge_elements": right_edge,
        "computed_icon_points": {k: list(v) for k, v in points.items()},
        "locate_source_would_be": "ocr" if points.get("like_button") else "vision",
    }


@app.post("/api/debug/clipboard-test")
async def clipboard_test():
    """Check whether the iOS clipboard bridge works -- if it does, comments paste
    cleanly (umlauts + no z/y swap); if not, we fall back to typed transliteration.
    Sets a test string with umlauts and reads it back."""
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    did = online[0].device_id
    probe = "Grüße zäh yzäöü Test"
    try:
        await client.clipboard_set(did, probe)
        await asyncio.sleep(0.4)
        got = await client.clipboard_get(did)
        readback = got.data.get("text") if hasattr(got, "data") else None
    except IMouseError as exc:
        return {"clipboard_bridge": False, "error": str(exc),
                "hint": "The iOS clipboard Shortcut bridge isn't set up -- comments "
                        "will use the typed ASCII fallback. Install/enable the iMouse "
                        "clipboard shortcut on the phone to get perfect paste."}
    ok = (readback or "").strip() == probe
    return {"clipboard_bridge": ok, "sent": probe, "read_back": readback,
            "hint": "clipboard_bridge:true means comments will paste perfectly."}


@app.post("/api/mirror/calibrate")
async def mirror_calibrate():
    """Calibrate the AirPlay mirror re-cast: learn the Control Center Screen
    Mirroring button and the PC receiver row, so the campaign can re-establish a
    dropped mirror on its own. Run this ONCE while the mirror is up.

    It opens Control Center, vision-locates the mirroring button, taps it to
    reveal the receiver list (this does NOT disconnect), vision-locates the
    receiver (without tapping it), then closes. Returns the screenshots + points
    so you can verify. Saves to the 'control_center' UI map on success.
    """
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    did = online[0].device_id
    device = registry.get(did)
    w = (device.imgw or device.width) if device else 0
    h = (device.imgh or device.height) if device else 0

    await client.send_key(did, fn_key="ControlBar")
    await asyncio.sleep(1.4)
    try:
        shot_cc = await controller.get_screenshot_b64(did)
    except IMouseError as exc:
        raise HTTPException(status_code=502, detail=f"screenshot failed: {exc}")
    mirror_pt = await agent.locate_one(
        shot_cc, w, h,
        "the Screen Mirroring / AirPlay button in iOS Control Center "
        "(icon of two overlapping rounded rectangles, often labelled "
        "'Bildschirmsynchronisation' or 'Screen Mirroring')",
    )

    shot_recv = None
    receiver_pt = None
    if mirror_pt:
        await client.click(did, mirror_pt[0], mirror_pt[1])
        await asyncio.sleep(1.6)
        try:
            shot_recv = await controller.get_screenshot_b64(did)
            receiver_pt = await agent.locate_one(
                shot_recv, w, h,
                "the FIRST device row in the AirPlay screen-mirroring list "
                "(the PC/Mac receiver to mirror to)",
            )
        except IMouseError:
            shot_recv = None

    await client.swipe(did, "down", 0.5)  # close CC WITHOUT exiting the app
    await asyncio.sleep(1.0)

    elements = {}
    if mirror_pt:
        elements["screen_mirroring"] = list(mirror_pt)
    if receiver_pt:
        elements["airplay_receiver"] = list(receiver_pt)
    if elements:
        save_screen("control_center", elements, w, h)
    return {
        "ok": bool(mirror_pt and receiver_pt),
        "saved": elements,
        "screen_mirroring": mirror_pt,
        "airplay_receiver": receiver_pt,
        "screenshot_control_center": shot_cc,
        "screenshot_receivers": shot_recv,
        "hint": "Verify the points sit on the right controls. Re-run if off.",
    }


@app.post("/api/mirror/open-cc")
async def mirror_open_cc():
    """Open iOS Control Center (over USB) and return a screenshot, for the visual
    mirror-calibration panel to mark the Screen Mirroring button on."""
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    did = online[0].device_id
    device = registry.get(did)
    await client.send_key(did, fn_key="ControlBar")
    await asyncio.sleep(1.3)
    try:
        shot = await controller.get_screenshot_b64(did)
    except IMouseError as exc:
        raise HTTPException(status_code=502, detail=f"screenshot failed: {exc}")
    return {
        "screenshot": shot,
        "device_id": did,
        "imgw": (device.imgw or device.width) if device else 0,
        "imgh": (device.imgh or device.height) if device else 0,
    }


@app.post("/api/mirror/close-cc")
async def mirror_close_cc():
    """Close Control Center with a swipe-down (NOT Home, which exits the app)."""
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    await client.swipe(online[0].device_id, "down", 0.5)
    return {"ok": True}


@app.get("/api/mirror/status")
async def mirror_status():
    """Report whether the mirror is up (a screenshot works) and whether re-cast
    is calibrated."""
    online = registry.online_devices
    calibrated = bool(load_map().get("screens", {}).get("control_center", {}).get("screen_mirroring"))
    if not online:
        return {"online": False, "mirror_up": False, "recast_calibrated": calibrated}
    did = online[0].device_id
    up = await controller.screenshot_works(did)
    return {"online": True, "mirror_up": up, "recast_calibrated": calibrated}


@app.get("/api/devices/{device_id}/screenshot-info")
async def screenshot_info(device_id: str):
    """Compare the screenshot's real JPEG size to the device's imgw/imgh tap
    space -- if they differ, mapped coordinates won't match taps."""
    device = registry.get(device_id)
    try:
        raw = await controller.get_screenshot_bytes(device_id)
    except IMouseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    size = _jpeg_size(raw)
    return {
        "jpeg_width": size[0] if size else None,
        "jpeg_height": size[1] if size else None,
        "imgw": device.imgw if device else None,
        "imgh": device.imgh if device else None,
        "match": bool(size and device and size == (device.imgw, device.imgh)),
    }


# ---------------------------------------------------- manual keyboard input ---
@app.post("/api/devices/{device_id}/type")
async def manual_type(device_id: str, request: ManualTypeRequest):
    """Type text into the phone's currently focused field, from the PC.

    Lets you type manually while the board stays connected, working around iOS
    hiding the on-screen keyboard when a hardware keyboard is attached. ASCII
    goes via /key/sendkey; non-ASCII via clipboard paste (with ASCII fallback).
    """
    return _result(await controller.enter_text(device_id, request.text))


@app.post("/api/devices/{device_id}/key")
async def manual_key(device_id: str, request: ManualKeyRequest):
    """Send a single key or hotkey (e.g. fn_key='ENTER' / 'BACKSPACE')."""
    if not request.key and not request.fn_key:
        raise HTTPException(status_code=400, detail="Provide 'key' or 'fn_key'")
    try:
        await client.send_key(device_id, key=request.key, fn_key=request.fn_key)
        return {"success": True, "message": "sent"}
    except IMouseError as exc:
        return {"success": False, "message": str(exc)}


# --------------------------------------------------------------- AI agent -----
@app.post("/api/devices/{device_id}/ai/step")
async def ai_step(device_id: str, request: AIAgentRequest):
    """Run one Claude perceive->decide->act cycle on the device."""
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    return await agent.step(device_id, goal=request.goal)


@app.post("/api/devices/{device_id}/ai/run")
async def ai_run(device_id: str, request: AIAgentRequest):
    """Run the Claude agent for up to max_steps toward a goal."""
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    return await agent.run(device_id, goal=request.goal, max_steps=request.max_steps)


# ------------------------------------------------------- interface explorer ---
@app.post("/api/devices/{device_id}/analyze-ui")
async def analyze_ui(device_id: str):
    """Map the current screen's controls to pixel coordinates (vision + OCR)."""
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    result = await agent.analyze_ui(device_id)
    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@app.get("/api/ui-map")
async def get_ui_map():
    return load_map()


@app.post("/api/ui-map")
async def post_ui_map(body: UIMapSave):
    return save_screen(body.screen_type, body.elements, body.imgw, body.imgh)


# ----------------------------------------------------------- niche campaign ---
@app.post("/api/campaign/start")
async def campaign_start(request: CampaignRequest):
    """Start a long-running, niche-aware campaign as a background task."""
    global campaign_task
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    if agent.campaign_status()["running"]:
        raise HTTPException(status_code=409, detail="A campaign is already running")
    # Select the niche (app context) for this run, then apply the optional
    # per-run language override on top.
    from niche import NICHE, set_active_niche
    if request.niche:
        set_active_niche(request.niche)
    if request.language is not None:
        NICHE["language"] = request.language.strip()
    seconds = int(max(1.0, request.duration_minutes) * 60)
    campaign_task = asyncio.create_task(agent.campaign_loop(request.device_id, seconds))
    return {"status": "started", "duration_minutes": request.duration_minutes,
            "language": NICHE.get("language")}


@app.post("/api/campaign/stop")
async def campaign_stop():
    """Ask the running campaign to stop at the next safe point."""
    agent.request_stop()
    return {"status": "stopping"}


@app.get("/api/niches")
async def get_niches():
    """Available app/niche contexts for the per-phone dropdown."""
    from niche import list_niches, NICHE
    return {"niches": list_niches(), "active": NICHE.get("name")}


@app.post("/api/devices/{device_id}/home")
async def go_home(device_id: str):
    """Send the phone to the iOS home screen (Home button)."""
    try:
        await client.send_key(device_id, fn_key="WIN+h")
        return {"success": True}
    except IMouseError as exc:
        return {"success": False, "message": str(exc)}


@app.get("/api/campaign/status")
async def campaign_status():
    return agent.campaign_status()


# ------------------------------------------------------------------ batch -----
@app.post("/api/batch/action")
async def batch_action(request: BatchActionRequest):
    results = await controller.batch_action(request.device_ids, request.action)
    return {"results": {d: _result(r) for d, r in results.items()}}


@app.post("/api/devices/{device_id}/auto-browse")
async def auto_browse(device_id: str, request: AutoBrowseRequest):
    stats = await controller.auto_browse(
        device_id,
        video_count=request.video_count,
        like_probability=request.like_probability,
        comment_probability=request.comment_probability,
        min_watch_time=request.min_watch_time,
        max_watch_time=request.max_watch_time,
    )
    return {"stats": stats}


# --------------------------------------------------------- screenshot stream --
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Stream screenshots to the browser by polling ``/pic/screenshot``.

    Frames are sent as raw JPEG **binary** WebSocket messages -- no base64/JSON
    overhead (~25% smaller, no per-frame serialisation). The loop paces by the
    real fetch time rather than a fixed sleep, so it runs as fast as the console
    can supply frames up to the requested rate. The iMouse WebSocket is not used
    server-side (local HTTP is ~70 ms); this socket is browser <-> this server.
    """
    await websocket.accept()
    ws_clients.add(websocket)
    stream_task: Optional[asyncio.Task] = None

    async def stream(device_id: str, interval_ms: int):
        min_period = max(0.0, interval_ms / 1000)
        while websocket in ws_clients:
            start = time.monotonic()
            try:
                frame = await controller.get_screenshot_bytes(device_id)
                await websocket.send_bytes(frame)
            except Exception:  # noqa: BLE001 - stop streaming on any error
                break
            # Pace by how long the fetch actually took; no fixed dead time.
            await asyncio.sleep(max(0.0, min_period - (time.monotonic() - start)))

    def start_stream(data: dict) -> asyncio.Task:
        nonlocal stream_task
        if stream_task and not stream_task.done():
            stream_task.cancel()
        return asyncio.create_task(
            stream(data.get("device_id"), data.get("interval", 80))
        )

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "start_stream":
                stream_task = start_stream(data)
            elif action == "stop_stream" and stream_task:
                stream_task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)
        if stream_task:
            stream_task.cancel()


# ---------------------------------------------------------------- frontend ----
frontend_path = Path(__file__).parent.parent / "frontend"


@app.get("/api/campaign/sessions")
async def campaign_sessions():
    """List saved campaign runs (newest first) with their stats. Browse the raw
    logs + frames at /logs/<session>/ ."""
    from campaign_log import list_sessions
    return {"sessions": list_sessions()}


@app.get("/logs-view/{name}", response_class=HTMLResponse)
async def campaign_session_view(name: str):
    """A single-page review gallery for one run: every cycle's decision next to
    the screenshot it was made on. This is the human-facing way to review a
    warm-up and spot where the swiping/engagement went wrong."""
    from campaign_log import read_session
    sess = read_session(name)
    if not sess:
        return HTMLResponse("<h2>Run not found</h2>", status_code=404)
    meta, summary = sess["meta"], sess["summary"]
    head = (f"<h1>{sess['name']}</h1>"
            f"<p>device <code>{meta.get('device','?')}</code> · "
            f"niche <b>{meta.get('niche','?')}</b> · "
            f"{len(sess['entries'])} steps"
            + (f" · stats {summary.get('stats')}" if summary.get('stats') else "")
            + "</p>")
    cards = []
    for e in sess["entries"]:
        frame = e.get("frame")
        img = (f'<img loading="lazy" src="/logs/{name}/{frame}">' if frame
               else '<div class="noimg">no frame</div>')
        rows = []
        for k in ("action", "screen", "relevant", "reason", "language",
                  "comment", "actions", "errors"):
            if e.get(k) not in (None, "", [], {}):
                rows.append(f"<div><span class='k'>{k}</span>{e.get(k)}</div>")
        cards.append(
            f"<div class='card'>{img}"
            f"<div class='meta'><div class='i'>#{e.get('i','')}</div>"
            + "".join(rows) + "</div></div>")
    html = f"""<!doctype html><meta charset=utf-8>
<title>{sess['name']}</title>
<style>
 body{{font:14px/1.4 system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
 header{{padding:16px 20px;border-bottom:1px solid #262a33;position:sticky;top:0;background:#0f1115}}
 header a{{color:#6ea8fe}} h1{{font-size:18px;margin:0 0 4px}}
 .wrap{{padding:16px 20px;display:flex;flex-direction:column;gap:14px;max-width:900px;margin:0 auto}}
 .card{{display:flex;gap:16px;background:#181b22;border:1px solid #262a33;border-radius:10px;padding:12px}}
 .card img{{width:200px;border-radius:8px;flex:0 0 auto;background:#000}}
 .noimg{{width:200px;height:300px;display:flex;align-items:center;justify-content:center;color:#666;background:#000;border-radius:8px}}
 .meta{{flex:1;min-width:0}} .meta .i{{font-weight:700;color:#6ea8fe;margin-bottom:6px}}
 .meta .k{{display:inline-block;min-width:76px;color:#8b93a1;text-transform:uppercase;font-size:11px;letter-spacing:.5px}}
 .meta div{{margin:3px 0;word-break:break-word}}
</style>
<header>{head}<a href="/logs/{name}/log.jsonl">raw log.jsonl</a></header>
<div class="wrap">{''.join(cards) or '<p>No steps recorded.</p>'}</div>"""
    return HTMLResponse(html)


@app.get("/")
async def serve_frontend():
    return FileResponse(frontend_path / "index.html")


if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

# Saved campaign logs + frames, browsable in the browser (created on first run).
_logs_dir = Path(__file__).parent / "campaign_logs"
_logs_dir.mkdir(exist_ok=True)
app.mount("/logs", StaticFiles(directory=str(_logs_dir), html=False), name="logs")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

```

## `backend/campaign_log.py`

```python
"""Persistent per-run campaign logs.

Each warm-up/campaign run is saved to ``backend/campaign_logs/<session>/`` with:
  * ``meta.json``     -- device, niche, start time
  * ``log.jsonl``     -- one line per cycle (action, screen, relevance, reason,
                         comment, errors, timestamp) + the frame filename
  * ``frames/NNNN.jpg`` -- the screenshot the decision was made on
  * ``summary.json``  -- stats + counts, written when the run ends

This is the raw material for reviewing runs and improving the swiping/engagement
logic over time. It's runtime data (gitignored).
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime

LOG_ROOT = os.path.join(os.path.dirname(__file__), "campaign_logs")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", (s or ""))[:40]


class SessionLogger:
    """Writes one campaign run to its own folder."""

    def __init__(self, device_id: str, niche_name: str):
        self.started = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.name = f"{self.started}_{_safe(niche_name) or 'run'}_{_safe(device_id)[-6:]}"
        self.dir = os.path.join(LOG_ROOT, self.name)
        self.frames_dir = os.path.join(self.dir, "frames")
        self.log_path = os.path.join(self.dir, "log.jsonl")
        self.count = 0
        try:
            os.makedirs(self.frames_dir, exist_ok=True)
            with open(os.path.join(self.dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump({"started": self.started, "device": device_id,
                           "niche": niche_name}, f, indent=2, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - logging must never break the run
            pass

    def log(self, entry: dict, screenshot_b64: str | None = None) -> None:
        self.count += 1
        rec: dict = {"i": self.count}
        rec.update(entry or {})
        if screenshot_b64:
            try:
                fn = f"{self.count:04d}.jpg"
                with open(os.path.join(self.frames_dir, fn), "wb") as f:
                    f.write(base64.b64decode(screenshot_b64))
                rec["frame"] = f"frames/{fn}"
            except Exception:  # noqa: BLE001
                pass
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def finish(self, stats: dict) -> None:
        try:
            with open(os.path.join(self.dir, "summary.json"), "w", encoding="utf-8") as f:
                json.dump({"started": self.started,
                           "ended": datetime.now().strftime("%Y%m%d-%H%M%S"),
                           "entries": self.count, "stats": stats},
                          f, indent=2, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass


def read_session(name: str) -> dict | None:
    """Return one saved run's meta + per-cycle entries (for the review gallery)."""
    name = _safe(name)
    d = os.path.join(LOG_ROOT, name)
    if not os.path.isdir(d):
        return None
    meta = {}
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    entries = []
    lp = os.path.join(d, "log.jsonl")
    if os.path.exists(lp):
        try:
            with open(lp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    summary = {}
    sp = os.path.join(d, "summary.json")
    if os.path.exists(sp):
        try:
            summary = json.load(open(sp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"name": name, "meta": meta, "summary": summary, "entries": entries}


def list_sessions(limit: int = 60) -> list:
    """Newest-first list of saved runs with their summary/stats."""
    if not os.path.isdir(LOG_ROOT):
        return []
    out = []
    for name in sorted(os.listdir(LOG_ROOT), reverse=True)[:limit]:
        d = os.path.join(LOG_ROOT, name)
        if not os.path.isdir(d):
            continue
        info = {"name": name, "entries": 0, "stats": {}, "running": True}
        sp = os.path.join(d, "summary.json")
        if os.path.exists(sp):
            try:
                info.update(json.load(open(sp, encoding="utf-8")))
                info["running"] = False
            except Exception:  # noqa: BLE001
                pass
        else:
            lp = os.path.join(d, "log.jsonl")
            if os.path.exists(lp):
                try:
                    info["entries"] = sum(1 for _ in open(lp, encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    pass
        out.append(info)
    return out

```

## `backend/selftest.py`

```python
"""Self-verifying button test harness.

Runs on the machine the phone is connected to (the console must be running).
For each of many feed videos it taps a button, then decides ON ITS OWN whether
the tap hit the right control -- no human watching:

  * like    -> the heart pixel flips white<->red (TikTok like colour)
  * save    -> the bookmark pixel flips white<->yellow
  * comment -> the comment sheet actually opens (OCR + big frame change)
  * share   -> the share sheet actually opens (OCR + big frame change)

A full-frame diff separates three outcomes: HIT (tiny change + colour toggle),
MISSED (tiny change, no toggle -- tapped dead space) and NAVIGATED (huge change
-- tapped a nav control like the profile; we then recover to the feed). Toggle
tests restore state (un-like / un-save) so the run leaves no trace, and sheet
tests are dismissed without posting.

Every failure captures the exact rail OCR data and computed points, so the
report says not just THAT a tap missed but WHY.

Usage (from backend/, with the venv active or via selftest.bat):
    python selftest.py [rounds] [buttons]
    python selftest.py 20 like,comment
Defaults: 15 rounds, buttons = like,save,comment,share.
Writes selftest_report.json and prints a summary.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import sys
from typing import Optional

from ai_agent import TikTokAgent
from api_client import IMouseClient, IMouseError
from config import Config
from device_manager import DeviceRegistry
from tiktok_actions import TikTokController

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


# ----------------------------------------------------------------- imaging ---
def _load(perc: dict):
    b64 = perc.get("screenshot_b64")
    if not (b64 and Image):
        return None
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def _avg(img, x: int, y: int, r: int = 5):
    W, H = img.size
    box = (max(0, x - r), max(0, y - r), min(W, x + r + 1), min(H, y + r + 1))
    px = list(img.crop(box).getdata())
    n = max(1, len(px))
    return (sum(p[0] for p in px) / n, sum(p[1] for p in px) / n, sum(p[2] for p in px) / n)


def _redness(img, x: int, y: int) -> float:
    """High for TikTok's red heart (~254,44,85); ~0 for a white outline."""
    R, G, B = _avg(img, x, y)
    return R - (G + B) / 2.0


def _yellowness(img, x: int, y: int) -> float:
    """High for the yellow saved-bookmark; ~0 for a white outline."""
    R, G, B = _avg(img, x, y)
    return (R + G) / 2.0 - B


def _frame_diff(a, b) -> float:
    """Mean absolute grayscale diff 0..1 of downscaled frames.

    A like/save recolours a tiny icon (tiny diff). Opening a sheet or navigating
    away changes most of the screen (large diff).
    """
    if not (a and b):
        return 0.0
    a = a.resize((48, 48)).convert("L")
    b = b.resize((48, 48)).convert("L")
    da, db = list(a.getdata()), list(b.getdata())
    return sum(abs(x - y) for x, y in zip(da, db)) / (len(da) * 255.0)


# --------------------------------------------------------------- semantics ---
# Screen identity is decided by OCR, NOT by a pixel diff -- the feed video is
# always in motion, so a frame diff can't tell a like from a navigation.
#
# The decisive feed signal is the right-hand RAIL of count numbers. Sheets cover
# it; profiles/search replace it. NOTE: 'Gefolgt'/'Für dich' are the feed's own
# top tabs and 'Profile' is the bottom nav label -- they are NOT off-feed signals
# and must never be treated as such.
COMMENT_HINTS = ("kommentar", "comment", "antworten", "reply", "kommentieren")
SHARE_HINTS = ("whatsapp", "link kopieren", "copy link", "senden an", "an freunde senden",
               "repost", "teilen mit", "share to", "story hinzu", "zu story", "in nachricht",
               "duett", "stitch", "als nachricht", "einbetten", "melden", "kein interesse",
               "instagram", "messenger", "telegram")
PROFILE_HINTS = ("follower", "abonnent", "abonniert", "beiträge", "gefällt mir", "reposts")


def _texts(perc: dict) -> list[str]:
    return [(e.get("text") or "").lower() for e in perc.get("elements", [])]


def _has_any(perc: dict, hints) -> bool:
    joined = " ".join(_texts(perc))
    return any(h in joined for h in hints)


def _is_count_text(text: str) -> bool:
    t = (text or "").strip()
    return bool(re.match(r"^[\d][\d.,]*\s*(?:[KkMm]|万|Mio\.?|Tsd\.?)?$", t))


def _rail_counts(perc: dict, w: int, h: int) -> list:
    out = []
    for e in perc.get("elements", []):
        cx, cy = e["centre"]
        if cx > w * 0.85 and h * 0.12 < cy < h * 0.90 and _is_count_text(e["text"]):
            out.append((int(cy), int(cx), e["text"]))
    out.sort()
    return out


# Rail slots may show a WORD label instead of a count on low-engagement posts.
RAIL_WORDS = ("teilen", "speicher", "favorit", "merken", "gespeichert", "share", "save")


def _rail_markers(perc: dict, w: int, h: int) -> list:
    """Right-edge rail entries -- counts OR label words (Teilen/Speichern)."""
    out = []
    for e in perc.get("elements", []):
        cx, cy = e["centre"]
        if not (cx > w * 0.85 and h * 0.12 < cy < h * 0.90):
            continue
        t = (e.get("text") or "").strip().lower()
        if _is_count_text(t) or any(k in t for k in RAIL_WORDS):
            out.append((int(cy), int(cx)))
    return sorted(out)


def is_feed(perc: dict, w: int, h: int) -> bool:
    """A feed video shows >=2 rail entries (counts or label words) on the right."""
    return len(_rail_markers(perc, w, h)) >= 2


def classify_screen(perc: dict, w: int, h: int) -> str:
    """feed | share_sheet | comments | profile | unknown (OCR-based)."""
    if is_feed(perc, w, h):
        return "feed"
    if _has_any(perc, SHARE_HINTS):
        return "share_sheet"
    if _has_any(perc, COMMENT_HINTS):
        return "comments"
    if _has_any(perc, PROFILE_HINTS):
        return "profile"
    return "unknown"


def _scenario(perc: dict, w: int, h: int) -> dict:
    texts = [e.get("text", "") for e in perc.get("elements", [])]
    slideshow = any(re.search(r"\b\d+\s*/\s*\d+\b", t) for t in texts)
    rail = _rail_counts(perc, w, h)
    top = rail[0][0] if rail else None
    # 'shifted' when the rail rides high (bottom overlay pushed it up) -- the case
    # that used to break fixed coordinates.
    if slideshow:
        label = "slideshow"
    elif top is not None and top < h * 0.46:
        label = "rail_shifted"
    else:
        label = "video"
    return {"label": label, "rail_top_y": top, "n_rail_counts": len(rail)}


def _right_edge(perc: dict, w: int) -> list[dict]:
    rows = [
        {"text": e["text"], "centre": e["centre"]}
        for e in perc.get("elements", []) if e["centre"][0] > w * 0.85
    ]
    rows.sort(key=lambda e: e["centre"][1])
    return rows


# ------------------------------------------------------------- harness core --
class SelfTest:
    def __init__(self, controller: TikTokController, agent: TikTokAgent, device):
        self.c = controller
        self.agent = agent
        self.dev = device
        self.did = device.device_id
        self.w = device.imgw or device.width
        self.h = device.imgh or device.height

    async def _perc_img(self):
        perc = await self.agent.perceive(self.did)
        return perc, _load(perc)

    async def _tap(self, x: int, y: int):
        await self.c._click(self.did, int(x), int(y))

    async def _screen(self) -> tuple[str, dict]:
        perc, _ = await self._perc_img()
        return classify_screen(perc, self.w, self.h), perc

    async def _return_to_feed(self, max_tries: int = 5) -> str:
        """Get back to a feed video, whatever we're stuck on. Verified by OCR.

        Bottom sheets (share/comment) do NOT reliably close on a swipe-down, so
        we tap the dimmed backdrop above the sheet; profiles/search use hardware
        Back. We re-check the screen (via the rail) after every attempt.
        """
        for i in range(max_tries):
            screen, _ = await self._screen()
            if screen == "feed":
                return "feed"
            # The share sheet often classifies as 'unknown' (its text doesn't
            # match), so DON'T gate the backdrop tap on the classification: a tap
            # on the dimmed strip above any bottom sheet closes it and is
            # harmless on a feed video. Try that first on every off-feed screen.
            await self._tap(int(self.w * 0.3), int(self.h * 0.22))
            await asyncio.sleep(0.6)
            if (await self._screen())[0] == "feed":
                return "feed"
            # Then a downward swipe (closes the comment sheet), then hardware Back
            # (for profile/search). Escalate across attempts.
            try:
                if i % 2 == 0:
                    await self.c.api.swipe(self.did, "down", 0.9)
                else:
                    await self.c.api.send_key(self.did, fn_key="TAB+b")
            except IMouseError:
                pass
            await asyncio.sleep(0.8)
        return (await self._screen())[0]

    async def _toggle_test(self, name: str, point, metric_fn, img_before, on_thresh: float):
        """like/save: tap, then decide by OCR (navigation) + icon colour (toggle)."""
        x, y = int(point[0]), int(point[1])
        before = metric_fn(img_before, x, y)
        await self._tap(x, y)
        await asyncio.sleep(0.9)
        perc2, img2 = await self._perc_img()
        screen = classify_screen(perc2, self.w, self.h)
        result = {"button": name, "point": [x, y], "before_metric": round(before, 1)}

        if screen != "feed":
            # Re-check once: a like doesn't leave the feed, so a transient 'not
            # feed' is usually a mid-frame OCR miss. Only a confirmed off-feed is
            # a real navigation.
            await asyncio.sleep(0.5)
            perc2, img2 = await self._perc_img()
            screen = classify_screen(perc2, self.w, self.h)
        if screen != "feed":  # confirmed: we left the video -> tapped a nav control
            result["verdict"] = "navigated"
            result["went_to"] = screen
            result["recovered_at"] = await self._return_to_feed()
            return result

        after = metric_fn(img2, x, y) if img2 else before
        result["after_metric"] = round(after, 1)
        toggled = abs(after - before) > on_thresh
        result["verdict"] = "hit" if toggled else "missed"
        if toggled:  # restore original state so the account is left untouched
            await self._tap(x, y)
            await asyncio.sleep(0.7)
            _, img3 = await self._perc_img()
            back = metric_fn(img3, x, y) if img3 else after
            result["restored"] = abs(back - before) < max(25.0, on_thresh)
        return result

    async def test_like(self, point, img_before):
        return await self._toggle_test("like", point, _redness, img_before, on_thresh=45.0)

    async def test_save(self, point, img_before):
        return await self._toggle_test("save", point, _yellowness, img_before, on_thresh=40.0)

    async def _sheet_test(self, name: str, point, img_before, expect: str):
        """comment/share: tap, verify the RIGHT sheet opened (OCR), then dismiss."""
        x, y = int(point[0]), int(point[1])
        await self._tap(x, y)
        await asyncio.sleep(1.1)
        screen, _ = await self._screen()
        if screen == expect:
            verdict = "hit"
        elif screen == "feed":
            verdict = "missed"          # tapped dead space, sheet never opened
        else:
            verdict = "navigated"       # opened the wrong thing
        result = {"button": name, "point": [x, y], "screen_after": screen, "verdict": verdict}
        result["recovered_at"] = await self._return_to_feed()
        return result

    async def test_comment(self, point, img_before):
        return await self._sheet_test("comment", point, img_before, "comments")

    async def test_share(self, point, img_before):
        return await self._sheet_test("share", point, img_before, "share_sheet")


BUTTON_TO_LABEL = {
    "like": "like_button",
    "save": "save_button",
    "comment": "comment_button",
    "share": "share_button",
}


async def run(rounds: int, buttons: list[str]) -> dict:
    cfg = Config()
    async with IMouseClient(cfg) as client:
        registry = DeviceRegistry(client)
        await registry.refresh()
        online = registry.online_devices
        if not online:
            raise SystemExit("No online device. Connect the phone and enable mirroring.")
        controller = TikTokController(cfg, client, registry)
        agent = TikTokAgent(controller)
        dev = online[0]
        st = SelfTest(controller, agent, dev)
        if Image is None:
            print("WARNING: Pillow not installed -- colour verification disabled. "
                  "Run:  pip install pillow")

        report = {"device": dev.device_id, "imgw": st.w, "imgh": st.h, "rounds": []}
        for i in range(rounds):
            perc, img = await st._perc_img()
            scen = _scenario(perc, st.w, st.h)
            pts = agent._ocr_rail_points(perc, st.w, st.h)
            entry = {
                "round": i,
                "scenario": scen,
                "computed_points": {k: list(v) for k, v in pts.items()},
                "right_edge": _right_edge(perc, st.w),
                "tests": [],
            }

            # Only test on an actual feed post (rail present), not a stray
            # transitional/overlay frame.
            if img is None or not is_feed(perc, st.w, st.h) or not pts.get("like_button"):
                entry["skipped"] = "not a feed video (no rail)"
                report["rounds"].append(entry)
                await controller.swipe_next(st.did)
                await asyncio.sleep(1.4)
                continue

            for b in buttons:
                label = BUTTON_TO_LABEL.get(b)
                point = pts.get(label)
                if not point:
                    entry["tests"].append({"button": b, "verdict": "no_point"})
                    continue
                # Re-snapshot right before each test so before-image is current.
                perc_b, img_b = await st._perc_img()
                fn = getattr(st, f"test_{b}")
                try:
                    res = await fn(point, img_b)
                except Exception as exc:  # noqa: BLE001
                    res = {"button": b, "verdict": "error", "error": str(exc)}
                entry["tests"].append(res)
                await asyncio.sleep(0.6)

            report["rounds"].append(entry)
            await controller.swipe_next(st.did)
            await asyncio.sleep(1.4)

        _summarize(report)
        with open("selftest_report.json", "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print("\nFull report written to backend/selftest_report.json")
        return report


def _summarize(report: dict) -> None:
    from collections import defaultdict
    agg = defaultdict(lambda: defaultdict(int))   # (button, scenario) -> verdict -> n
    failures = []
    for rnd in report["rounds"]:
        scen = rnd["scenario"]["label"]
        for t in rnd.get("tests", []):
            v = t.get("verdict", "?")
            agg[(t["button"], scen)][v] += 1
            if v not in ("hit",):
                failures.append((rnd["round"], scen, t, rnd))

    print("\n================ SELF-TEST SUMMARY ================")
    print(f"device {report['device']}  space {report['imgw']}x{report['imgh']}  "
          f"rounds {len(report['rounds'])}")
    print(f"{'button':8} {'scenario':13} {'hit':>4} {'miss':>5} {'nav':>4} {'other':>6}")
    for (button, scen), v in sorted(agg.items()):
        other = sum(n for k, n in v.items() if k not in ("hit", "missed", "navigated"))
        print(f"{button:8} {scen:13} {v['hit']:>4} {v['missed']:>5} {v['navigated']:>4} {other:>6}")

    if failures:
        print(f"\n---- {len(failures)} FAILURE(S) (with rail data for diagnosis) ----")
        for rnd_i, scen, t, rnd in failures[:20]:
            extra = t.get("went_to") or t.get("screen_after")
            print(f"\nround {rnd_i} [{scen}] {t['button']} -> {t['verdict']}"
                  + (f" (screen={extra})" if extra else "")
                  + (f" recovered={t.get('recovered_at')}" if t.get("recovered_at") else ""))
            print(f"  computed: {rnd['computed_points'].get(BUTTON_TO_LABEL.get(t['button']))}")
            print(f"  right_edge OCR: {[ (e['text'], e['centre']) for e in rnd['right_edge'] ]}")
    else:
        print("\nAll tested taps HIT. \U0001f389")
    print("===================================================")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    btns = (sys.argv[2].split(",") if len(sys.argv) > 2
            else ["like", "save", "comment", "share"])
    asyncio.run(run(n, btns))

```

## `backend/comment_test.py`

```python
"""Autonomous comment-flow test loop.

Swipes through feed videos and, on each, opens the comment sheet and checks --
via vision -- whether our input tap lands on a TOP-LEVEL comment box or a REPLY
to an existing comment. It NEVER posts anything: it opens, inspects, and
dismisses. Use it to verify the "standalone vs reply" fix and to see where the
real 'Add comment' bar is.

Usage (from backend/, venv active or via comment_test.bat):
    python comment_test.py [rounds]
Default 6 rounds. Prints a table and writes comment_test_report.json.
"""
from __future__ import annotations

import asyncio
import json
import sys

from ai_agent import TikTokAgent
from api_client import IMouseClient, IMouseError
from config import Config
from device_manager import DeviceRegistry
from tiktok_actions import TikTokController

COMPOSER_TOOL = [{
    "name": "report_composer",
    "description": "Report the TikTok comment composer state.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["top_level", "reply", "no_composer", "unknown"],
                     "description": "top_level = a NEW top-level comment box is focused/available "
                                    "(placeholder like 'Kommentar hinzufügen' / 'Add comment'). "
                                    "reply = it's replying to an existing comment (placeholder like "
                                    "'Auf ... antworten' / 'Reply to @user', or a specific comment is "
                                    "highlighted). no_composer = the comment sheet/keyboard isn't open."},
            "placeholder": {"type": "string", "description": "The exact placeholder/label text you see in the input bar."},
            "note": {"type": "string", "description": "Anything relevant (e.g. which comment it's replying to)."},
        },
        "required": ["mode"],
    },
}]


async def analyze_composer(agent: TikTokAgent, shot_b64: str) -> dict:
    if not shot_b64:
        return {"mode": "unknown", "note": "no screenshot"}
    system = (
        "You are looking at a TikTok comment sheet after an automation tapped the "
        "comment input. Decide whether the composer is set up to post a NEW "
        "TOP-LEVEL comment or a REPLY to an existing comment. The input bar's "
        "placeholder text is the key: 'Kommentar hinzufügen…' / 'Add comment…' / "
        "'Sag etwas Nettes' = top_level; 'Auf @user antworten' / 'Reply to @user' "
        "= reply. Call report_composer."
    )
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": shot_b64}},
        {"type": "text", "text": "Classify the composer."},
    ]
    try:
        resp = await agent.controller.ai.client.messages.create(
            model=agent.config.anthropic_model,
            max_tokens=200,
            system=system,
            tools=COMPOSER_TOOL,
            tool_choice={"type": "tool", "name": "report_composer"},
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:  # noqa: BLE001
        return {"mode": "unknown", "note": f"vision failed: {exc}"}
    for block in resp.content:
        if block.type == "tool_use":
            return dict(block.input or {})
    return {"mode": "unknown"}


async def probe(controller: TikTokController, agent: TikTokAgent, did: str,
                comment_pt) -> dict:
    # Open the comment sheet exactly the way the campaign does (vision-located
    # comment icon), but stop before typing/posting.
    input_pt = None
    for _ in range(2):
        input_pt = await controller._find_comment_input(did)
        if input_pt or await controller._comment_sheet_open(did):
            break
        await controller.open_comments(did, point=comment_pt)
        await asyncio.sleep(controller.config.action_delay)

    sheet_open = await controller._comment_sheet_open(did)
    result = {"sheet_open": sheet_open, "input_found": list(input_pt) if input_pt else None,
              "comment_pt": list(comment_pt) if comment_pt else None}
    if not sheet_open:
        result["verdict"] = {"mode": "no_composer", "note": "comment sheet did not open"}
        return result

    # Tap the located input bar (or the anchor) to focus the composer.
    if input_pt:
        await controller._click(did, *input_pt)
    else:
        device = await controller.get_device(did)
        x, y = device.to_pixels(0.5, 0.95) if device else (300, 1000)
        await controller._click(did, x, y)
    await asyncio.sleep(1.0)

    shot = None
    try:
        shot = await controller.get_screenshot_b64(did)
    except IMouseError:
        pass
    result["verdict"] = await analyze_composer(agent, shot)

    # Dismiss WITHOUT posting: close the keyboard, then the sheet.
    try:
        await controller.api.send_key(did, fn_key="TAB+b")
    except IMouseError:
        pass
    await asyncio.sleep(0.4)
    await controller._dismiss(did)
    return result


async def run(rounds: int) -> None:
    cfg = Config()
    async with IMouseClient(cfg) as client:
        registry = DeviceRegistry(client)
        await registry.refresh()
        online = registry.online_devices
        if not online:
            raise SystemExit("No online device. Connect the phone and open TikTok.")
        did = online[0].device_id
        controller = TikTokController(cfg, client, registry)
        agent = TikTokAgent(controller)

        rows = []
        for i in range(rounds):
            perc = await agent.settled_perception(did)
            words = agent._post_words(perc)
            plan = await agent.plan_video(perc, [])

            # Skip lives / non-feed screens (comments live elsewhere on a live);
            # only probe real feed posts, like the campaign does.
            if agent._looks_live(plan) or not plan.get("on_feed"):
                print(f"round {i}: skip ({plan.get('post_type') or plan.get('screen','')[:40]})")
                await agent._advance_feed(did, words)
                continue

            comment_pt = (await agent.locate_actions(did, perc)).get("comment_button")
            res = await probe(controller, agent, did, comment_pt)
            res["round"] = i
            rows.append(res)
            v = res.get("verdict", {})
            print(f"round {i}: sheet={res['sheet_open']} comment_pt={res['comment_pt']} "
                  f"input={res['input_found']} -> {v.get('mode')} "
                  f"placeholder={v.get('placeholder')!r} {v.get('note','')}")

            # Make sure the sheet is fully closed before advancing, or swipe_next
            # would just scroll the comments.
            for _ in range(3):
                if not await controller._comment_sheet_open(did):
                    break
                await controller._dismiss(did)
            await asyncio.sleep(0.4)
            await agent._advance_feed(did, words)

        with open("comment_test_report.json", "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        top = sum(1 for r in rows if r.get("verdict", {}).get("mode") == "top_level")
        rep = sum(1 for r in rows if r.get("verdict", {}).get("mode") == "reply")
        print(f"\nSUMMARY: {top} top_level, {rep} reply, {len(rows) - top - rep} other "
              f"(of {len(rows)}). Report -> backend/comment_test_report.json")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    asyncio.run(run(n))

```

## `backend/requirements.txt`

```text
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
httpx>=0.26.0
anthropic>=0.40.0
pydantic>=2.5.0
python-dotenv>=1.0.0
pillow>=10.0.0

```

## `backend/.env.example`

```text
# Copy to backend/.env and fill in. Never commit the real .env.

# iMouse XP console -- localhost, verified port 9911 (do NOT use 9912).
IMOUSE_HOST=127.0.0.1
IMOUSE_PORT=9911

# Anthropic (only needed for AI comment generation / video analysis).
# Create at console.anthropic.com; starts with sk-ant-. Add billing/credits.
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5-20251001

```

## `frontend/index.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TikTok Controller</title>
    <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
    <div class="container">
        <header>
            <h1>TikTok Controller</h1>
            <div class="status-bar">
                <span id="connection-status" class="status disconnected">Disconnected</span>
                <button id="dev-toggle" class="btn-small">Developer</button>
                <button id="settings-btn" class="icon-btn" title="Settings">&#9881;</button>
            </div>
        </header>

        <!-- ============================ PHONE GRID (main) ==================== -->
        <main id="grid-view">
            <div class="grid-head">
                <h2>Phones</h2>
                <button id="refresh-grid" class="btn-small">Refresh</button>
            </div>
            <p class="hint">Each connected phone shows below. Pick the app it should work for, then <b>Warm up</b> to start the agent. <b>Home screen</b> returns to the iOS home. <b>Control</b> lets you tap the phone with your mouse.</p>
            <div id="phone-grid" class="phone-grid">
                <p class="placeholder">Looking for connected phones…</p>
            </div>
        </main>

        <!-- ============================ DEVELOPER VIEW ====================== -->
        <main id="dev-view" hidden>
            <div class="grid-head">
                <button id="dev-back" class="btn-small">&#8592; Back to phones</button>
                <h2>Developer tools</h2>
            </div>

            <!-- Device Selection -->
            <section class="device-panel">
                <div class="panel-header">
                    <h2>Devices</h2>
                    <button id="refresh-devices" class="btn-small">Refresh</button>
                </div>
                <div id="device-list" class="device-list">
                    <p class="placeholder">Loading devices...</p>
                </div>
            </section>

            <!-- Live preview -->
            <section class="control-panel">
                <div class="preview-container">
                    <div id="device-preview" class="device-preview">
                        <p class="placeholder">Select a device to view</p>
                    </div>
                    <div class="preview-controls">
                        <button id="toggle-stream" class="btn-small" disabled>Start Stream</button>
                        <button id="take-screenshot" class="btn-small" disabled>Screenshot</button>
                        <label class="tap-mode-label"><input type="checkbox" id="tap-mode"> Tap test</label>
                        <span id="tap-readout" class="tap-readout"></span>
                    </div>
                </div>
            </section>

            <!-- Quick Actions -->
            <section class="actions-panel">
                <h3>Quick Actions</h3>
                <div class="action-grid">
                    <button id="action-swipe-up" class="action-btn" disabled><span class="action-icon">&#8593;</span><span>Next Video</span></button>
                    <button id="action-swipe-down" class="action-btn" disabled><span class="action-icon">&#8595;</span><span>Previous</span></button>
                    <button id="action-like" class="action-btn" disabled><span class="action-icon">&#9829;</span><span>Like</span></button>
                    <button id="action-save" class="action-btn" disabled><span class="action-icon">&#9734;</span><span>Save</span></button>
                    <button id="action-follow" class="action-btn" disabled><span class="action-icon">&#43;</span><span>Follow</span></button>
                    <button id="action-comment" class="action-btn" disabled><span class="action-icon">&#128172;</span><span>Comment</span></button>
                </div>
                <div class="batch-actions" id="batch-actions" style="display: none;">
                    <h4>Batch Actions (All Selected)</h4>
                    <div class="batch-buttons">
                        <button id="batch-swipe" class="btn-small">Swipe All</button>
                        <button id="batch-like" class="btn-small">Like All</button>
                    </div>
                </div>
            </section>

            <!-- Campaign (detailed) -->
            <section class="campaign-panel">
                <h3>Campaign (detailed)</h3>
                <p class="hint">Same agent as "Warm up", with explicit language/minutes control on the selected device.</p>
                <div class="campaign-controls">
                    <label>Language: <input type="text" id="campaign-language" placeholder="German" style="width:120px"></label>
                    <label>Minutes: <input type="number" id="campaign-minutes" value="30" min="1" max="240"></label>
                    <button id="campaign-start" class="btn" disabled>Start Campaign</button>
                    <button id="campaign-stop" class="btn-small" style="display:none;">Stop</button>
                    <button id="campaign-copy" class="btn-small">Copy log</button>
                </div>
                <div id="campaign-status" class="campaign-status"></div>
                <div class="campaign-live">
                    <div id="campaign-snapshot" class="campaign-snapshot"><p class="placeholder">Live view</p></div>
                    <div id="campaign-log" class="agent-log"><p class="placeholder">Campaign activity will appear here</p></div>
                </div>
            </section>

            <!-- AI Agent -->
            <section class="agent-panel">
                <h3>AI Agent</h3>
                <p class="hint">Claude looks at the screen (vision + OCR) and drives the phone: swipes, likes, comments, follows.</p>
                <textarea id="agent-goal" placeholder="Goal (leave blank for natural browsing)"></textarea>
                <div class="agent-controls">
                    <label>Steps: <input type="number" id="agent-steps" value="10" min="1" max="50"></label>
                    <button id="agent-step" class="btn-small" disabled>Single Step</button>
                    <button id="agent-run" class="btn" disabled>Run Agent</button>
                </div>
                <div id="agent-log" class="agent-log"><p class="placeholder">The agent's actions will appear here</p></div>
            </section>

            <!-- AI Comments -->
            <section class="comment-panel">
                <h3>AI Comments</h3>
                <div class="comment-controls">
                    <select id="comment-style">
                        <option value="friendly">Friendly</option>
                        <option value="funny">Funny</option>
                        <option value="supportive">Supportive</option>
                        <option value="curious">Curious</option>
                        <option value="impressed">Impressed</option>
                        <option value="relatable">Relatable</option>
                    </select>
                    <button id="generate-comments" class="btn" disabled>Generate Comments</button>
                </div>
                <div id="comment-suggestions" class="comment-suggestions"><p class="placeholder">Click "Generate Comments" to get AI suggestions</p></div>
                <div class="custom-comment">
                    <textarea id="custom-comment" placeholder="Or type your own comment..."></textarea>
                    <button id="post-comment" class="btn" disabled>Post Comment</button>
                </div>
            </section>

            <!-- Video Analysis -->
            <section class="analysis-panel">
                <h3>Video Analysis</h3>
                <button id="analyze-video" class="btn" disabled>Analyze Current Video</button>
                <div id="analysis-result" class="analysis-result"><p class="placeholder">Analysis results will appear here</p></div>
            </section>

            <!-- Interface Explorer -->
            <section class="explorer-panel">
                <h3>Interface Explorer</h3>
                <p class="hint">Open a TikTok screen on the phone → <b>Analyze</b>. Claude marks the buttons it sees. Verify each: pick a control, then <b>click the image</b> to move its dot onto the exact spot. Set the screen type and <b>Save to map</b>.</p>
                <div class="explorer-controls">
                    <button id="explore-analyze" class="btn" disabled>Analyze current screen</button>
                    <label>Screen type: <input type="text" id="explore-screen-type" placeholder="for_you_feed" list="screen-types"></label>
                    <datalist id="screen-types">
                        <option value="for_you_feed"><option value="slideshow_feed"><option value="following_feed">
                        <option value="profile"><option value="search_input"><option value="search_results">
                        <option value="explore"><option value="comments_sheet"><option value="share_sheet">
                        <option value="inbox_dm"><option value="live"><option value="settings">
                    </datalist>
                    <button id="explore-save" class="btn-small" disabled>Save to map</button>
                </div>
                <div class="explorer-body">
                    <div id="explore-canvas" class="explore-canvas"><p class="placeholder">Analyze a screen to begin</p></div>
                    <div id="explore-list" class="explore-list"></div>
                </div>
                <div class="explore-saved-head"><button id="explore-copy-map" class="btn-small">Copy UI map</button></div>
                <details class="explore-saved"><summary>Saved UI map</summary><pre id="explore-map">{}</pre></details>
            </section>

            <!-- Mirror Re-cast Calibration -->
            <section class="mirror-panel">
                <h3>Mirror Re-cast Calibration</h3>
                <p class="hint">Teach the agent to restore a dropped screen mirror. Do this <b>once</b>, while mirroring works. Steps: <b>1</b> Open Control Center → <b>2</b> click the <i>Screen Mirroring</i> button on the image → <b>3</b> Open receiver list → <b>4</b> click your <i>PC</i> in the list → <b>5</b> Save.</p>
                <div class="mirror-controls">
                    <button id="mirror-open-cc" class="btn-small">1. Open Control Center</button>
                    <button id="mirror-pick-btn" class="btn-small" disabled>2. Mark: Screen Mirroring</button>
                    <button id="mirror-open-list" class="btn-small" disabled>3. Open receiver list</button>
                    <button id="mirror-pick-recv" class="btn-small" disabled>4. Mark: PC receiver</button>
                    <button id="mirror-save" class="btn-small" disabled>5. Save</button>
                    <button id="mirror-close" class="btn-small">Close CC</button>
                </div>
                <p class="hint" id="mirror-hintline">Screen Mirroring: <span id="mirror-btn-pt">—</span> · PC receiver: <span id="mirror-recv-pt">—</span></p>
                <div id="mirror-canvas" class="explore-canvas"><p class="placeholder">Open Control Center to begin</p></div>
            </section>

            <!-- Saved Runs -->
            <section class="runs-panel">
                <div class="panel-header">
                    <h3>Saved Runs (logs + screenshots)</h3>
                    <button id="runs-refresh" class="btn-small">Refresh</button>
                </div>
                <p class="hint">Every warm-up is saved to <code>backend/campaign_logs/</code> — step log, errors, and the screenshot each decision was made on. Browse raw files at <code>/logs/&lt;run&gt;/</code>.</p>
                <div id="runs-list" class="runs-list"><p class="placeholder">Loading…</p></div>
            </section>

            <!-- Manual Keyboard -->
            <section class="keyboard-panel">
                <h3>Manual Keyboard</h3>
                <p class="hint">Type on the phone from your PC while the board stays connected. Tap the field on the phone first, then type here.</p>
                <div class="keyboard-controls">
                    <input type="text" id="manual-text" placeholder="Type text to send to the phone…">
                    <button id="manual-send" class="btn" disabled>Send</button>
                </div>
                <div class="keyboard-keys">
                    <button class="btn-small manual-key" data-fn="ENTER" disabled>Enter</button>
                    <button class="btn-small manual-key" data-fn="BACKSPACE" disabled>Backspace</button>
                    <button class="btn-small manual-key" data-fn="WIN+h" disabled>Home</button>
                    <button class="btn-small manual-key" data-fn="TAB+b" disabled>Back</button>
                </div>
            </section>
        </main>

        <!-- Settings Modal -->
        <div id="settings-modal" class="modal">
            <div class="modal-content">
                <div class="modal-header">
                    <h2>Settings</h2>
                    <button class="close-btn">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="setting-group">
                        <h4>iMouse XP Connection</h4>
                        <label>Host: <input type="text" id="setting-host" placeholder="127.0.0.1"></label>
                        <label>API Port: <input type="number" id="setting-port" placeholder="9911"></label>
                    </div>
                    <div class="setting-group">
                        <h4>Anthropic (Claude) Configuration</h4>
                        <label>API Key: <input type="password" id="setting-anthropic-key" placeholder="sk-ant-..."></label>
                        <label>Model:
                            <select id="setting-anthropic-model">
                                <option value="claude-haiku-4-5-20251001">Claude Haiku 4.5</option>
                                <option value="claude-sonnet-4-6">Claude Sonnet 4.6</option>
                                <option value="claude-opus-4-8">Claude Opus 4.8</option>
                            </select>
                        </label>
                    </div>
                    <div class="setting-group">
                        <h4>UI Calibration</h4>
                        <p class="hint">Click on the preview to calibrate icon positions.</p>
                        <select id="calibration-element">
                            <option value="">Select element to calibrate...</option>
                            <option value="like_button">Like Button</option>
                            <option value="comment_button">Comment Button</option>
                            <option value="share_button">Share Button</option>
                            <option value="video_center">Video Center</option>
                            <option value="comment_input">Comment Input</option>
                            <option value="back_arrow">Back Arrow (top-left)</option>
                            <option value="home_tab">Home Tab (bottom-left)</option>
                        </select>
                    </div>
                    <button id="save-settings" class="btn">Save Settings</button>
                </div>
            </div>
        </div>

        <div id="toast-container"></div>
    </div>

    <script src="/static/app.js"></script>
</body>
</html>

```

## `frontend/app.js`

```javascript
// TikTok Controller Frontend Application

class TikTokController {
    constructor() {
        this.apiBase = '/api';
        this.devices = [];
        this.selectedDevice = null;
        this.selectedDevices = new Set();
        this.isStreaming = false;
        this.ws = null;
        this.config = {};
        this.calibrationMode = null;

        this.init();
    }

    async init() {
        this._grid = { previews: {}, control: {}, niches: [], activeCampaignDevice: null };
        await this.loadConfig();
        await this.loadNiches();
        await this.loadDevices();
        this.bindEvents();
        this.updateConnectionStatus(true);
        // Reconnect to a campaign that's already running (e.g. after a refresh).
        this.pollCampaign();
        this.loadUiMap();
        // Build + keep the phone grid live.
        this.renderGrid();
        this.startGridStatusPolling();
    }

    // ------------------------------------------------------- phone grid ------
    async loadNiches() {
        try {
            const r = await this.api('/niches');
            this._grid.niches = r.niches || [];
        } catch (e) { this._grid.niches = ['NutriSkin']; }
    }

    onlineDevices() {
        return this.devices.filter(d => d.is_online);
    }

    toggleDevView(show) {
        document.getElementById('grid-view').hidden = show;
        document.getElementById('dev-view').hidden = !show;
        if (!show) { this.renderGrid(); }  // returning to grid -> refresh it
        else { this.loadRuns(); }          // entering dev -> refresh saved runs
    }

    async loadRuns() {
        const el = document.getElementById('runs-list');
        if (!el) return;
        try {
            const r = await this.api('/campaign/sessions');
            const runs = r.sessions || [];
            if (!runs.length) { el.innerHTML = '<p class="placeholder">No runs yet.</p>'; return; }
            el.innerHTML = runs.map(s => {
                const st = s.stats || {};
                const bits = [`${s.entries || 0} steps`];
                if (st.videos != null) bits.push(`${st.videos} videos`);
                if (st.relevant != null) bits.push(`${st.relevant} relevant`);
                if (st.likes != null) bits.push(`${st.likes}❤ ${st.saves||0}🔖 ${st.comments||0}💬`);
                if (st.errors) bits.push(`${st.errors} errors`);
                if (s.running) bits.push('(running)');
                return `<div class="run-row">
                    <a href="/logs-view/${encodeURIComponent(s.name)}" target="_blank">${s.name}</a>
                    <span class="run-stats">${bits.join(' · ')}</span>
                    <a class="run-frames" href="/logs-view/${encodeURIComponent(s.name)}" target="_blank">review ↗</a>
                </div>`;
            }).join('');
        } catch (e) { el.innerHTML = '<p class="placeholder">Could not load runs.</p>'; }
    }

    renderGrid() {
        const grid = document.getElementById('phone-grid');
        if (!grid) return;
        const online = this.onlineDevices();
        // Stop preview loops for devices no longer present.
        Object.keys(this._grid.previews).forEach(id => {
            if (!online.find(d => d.device_id === id)) this.stopPreview(id);
        });
        if (!online.length) {
            grid.innerHTML = '<p class="placeholder">No phones online. Connect a board + mirror, then Refresh.</p>';
            return;
        }
        const opts = this._grid.niches.map(n => `<option value="${n}">${n}</option>`).join('');
        grid.innerHTML = online.map(d => {
            const id = d.device_id;
            return `
            <div class="phone-card" data-device-id="${id}">
                <div class="phone-screen" id="screen-${id}"><span class="ph-ph">connecting…</span></div>
                <select class="phone-niche" data-device-id="${id}">${opts}</select>
                <div class="phone-actions">
                    <button class="btn-small ph-warmup" data-device-id="${id}">Warm up</button>
                    <button class="btn-small ph-home" data-device-id="${id}">Home screen</button>
                    <button class="btn-small ph-control" data-device-id="${id}">Control</button>
                </div>
                <div class="ph-label">${d.name || id.slice(0,10)} · ${d.model} · ${d.width}×${d.height}</div>
            </div>`;
        }).join('');
        // (Re)start previews for online phones.
        online.forEach(d => this.startPreview(d.device_id));
        this.refreshWarmupButtons();
    }

    startPreview(id) {
        if (this._grid.previews[id]) return;  // already running
        const tick = async () => {
            try {
                const data = await this.api(`/devices/${id}/screenshot`);
                const el = document.getElementById(`screen-${id}`);
                if (el && data.screenshot) {
                    let img = el.querySelector('img');
                    if (!img) { el.innerHTML = ''; img = document.createElement('img'); el.appendChild(img); }
                    img.src = `data:image/jpeg;base64,${data.screenshot}`;
                }
            } catch (e) { /* mirror may be briefly unavailable */ }
        };
        tick();
        this._grid.previews[id] = setInterval(tick, 1800);
    }

    stopPreview(id) {
        if (this._grid.previews[id]) { clearInterval(this._grid.previews[id]); delete this._grid.previews[id]; }
    }

    async gridWarmup(id) {
        const running = this._grid.activeCampaignDevice;
        if (running === id) {                    // stop this phone's agent
            try { await this.api('/campaign/stop', 'POST'); } catch (e) {}
            this._grid.activeCampaignDevice = null;
            this.showToast('Warm-up stopped', 'info');
            this.refreshWarmupButtons();
            return;
        }
        if (running && running !== id) {
            this.showToast('Another phone is already warming up (one at a time for now).', 'warning');
            return;
        }
        const niche = document.querySelector(`.phone-niche[data-device-id="${id}"]`)?.value;
        try {
            await this.api('/campaign/start', 'POST', { device_id: id, duration_minutes: 240, niche });
            this._grid.activeCampaignDevice = id;
            this.showToast(`Warming up for ${niche}…`, 'success');
        } catch (e) {
            this.showToast('Could not start (a campaign may already be running).', 'error');
        }
        this.refreshWarmupButtons();
    }

    async gridHome(id) {
        try { await this.api(`/devices/${id}/home`, 'POST'); this.showToast('Sent to home screen', 'info'); }
        catch (e) { this.showToast('Home failed', 'error'); }
    }

    gridToggleControl(id) {
        this._grid.control[id] = !this._grid.control[id];
        document.querySelector(`.ph-control[data-device-id="${id}"]`)?.classList.toggle('active', this._grid.control[id]);
        const el = document.getElementById(`screen-${id}`);
        if (el) el.classList.toggle('controllable', this._grid.control[id]);
    }

    async gridScreenTap(id, e) {
        if (!this._grid.control[id]) return;
        // NOTE: device IDs contain ':' which is invalid in a CSS id selector, so
        // querySelector('#screen-A4:B8:..') throws/fails -- use getElementById.
        const el = document.getElementById(`screen-${id}`);
        const img = el && el.querySelector('img');
        if (!img) return;
        const rect = img.getBoundingClientRect();
        const dev = this.devices.find(d => d.device_id === id) || {};
        const w = dev.imgw || dev.width || 608, h = dev.imgh || dev.height || 1080;
        const x = Math.round((e.clientX - rect.left) / rect.width * w);
        const y = Math.round((e.clientY - rect.top) / rect.height * h);
        try { await this.api(`/devices/${id}/tap`, 'POST', { x, y }); } catch (err) {}
    }

    startGridStatusPolling() {
        setInterval(async () => {
            if (document.getElementById('grid-view').hidden) return;  // only when visible
            try {
                const s = await this.api('/campaign/status');
                this._grid.activeCampaignDevice = s.running ? s.device_id : null;
                this.refreshWarmupButtons();
            } catch (e) {}
        }, 3000);
    }

    refreshWarmupButtons() {
        const active = this._grid.activeCampaignDevice;
        document.querySelectorAll('.ph-warmup').forEach(btn => {
            const id = btn.dataset.deviceId;
            const isThis = active === id;
            btn.textContent = isThis ? 'Stop' : 'Warm up';
            btn.classList.toggle('active', isThis);
            btn.closest('.phone-card')?.classList.toggle('warming', isThis);
        });
    }

    // API Methods
    async api(endpoint, method = 'GET', body = null) {
        const options = {
            method,
            headers: { 'Content-Type': 'application/json' }
        };
        if (body) {
            options.body = JSON.stringify(body);
        }

        try {
            const response = await fetch(`${this.apiBase}${endpoint}`, options);
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'API error');
            }
            return await response.json();
        } catch (error) {
            this.showToast(error.message, 'error');
            throw error;
        }
    }

    // Configuration
    async loadConfig() {
        try {
            this.config = await this.api('/config');
            this.updateSettingsUI();
        } catch (error) {
            console.error('Failed to load config:', error);
        }
    }

    async saveConfig() {
        const settings = {
            imouse_host: document.getElementById('setting-host').value || undefined,
            imouse_port: parseInt(document.getElementById('setting-port').value) || undefined,
            anthropic_api_key: document.getElementById('setting-anthropic-key').value || undefined,
            anthropic_model: document.getElementById('setting-anthropic-model').value || undefined
        };

        // Remove undefined values
        Object.keys(settings).forEach(key => {
            if (settings[key] === undefined) delete settings[key];
        });

        try {
            await this.api('/config', 'POST', settings);
            this.showToast('Settings saved', 'success');
            this.closeModal();
            await this.loadConfig();
        } catch (error) {
            console.error('Failed to save config:', error);
        }
    }

    updateSettingsUI() {
        document.getElementById('setting-host').value = this.config.imouse_host || '';
        document.getElementById('setting-port').value = this.config.imouse_port || '';
        document.getElementById('setting-anthropic-model').value =
            this.config.anthropic_model || 'claude-haiku-4-5-20251001';
    }

    // Device Management
    async loadDevices() {
        try {
            const data = await this.api('/devices');
            this.devices = data.devices;
            this.renderDevices();
            if (this._grid && !document.getElementById('grid-view').hidden) this.renderGrid();
        } catch (error) {
            console.error('Failed to load devices:', error);
        }
    }

    renderDevices() {
        const container = document.getElementById('device-list');

        if (this.devices.length === 0) {
            container.innerHTML = '<p class="placeholder">No devices found</p>';
            return;
        }

        container.innerHTML = this.devices.map(device => `
            <div class="device-item ${device.device_id === this.selectedDevice?.device_id ? 'selected' : ''}
                        ${this.selectedDevices.has(device.device_id) ? 'multi-selected' : ''}"
                 data-device-id="${device.device_id}">
                <div class="device-status ${device.is_online ? 'online' : ''}"></div>
                <div class="device-info">
                    <div class="device-name">${device.name || device.device_id.slice(0, 8)}</div>
                    <div class="device-model">${device.model} (${device.width}x${device.height})</div>
                </div>
            </div>
        `).join('');
    }

    selectDevice(deviceId, multiSelect = false) {
        const device = this.devices.find(d => d.device_id === deviceId);
        if (!device) return;

        if (multiSelect) {
            if (this.selectedDevices.has(deviceId)) {
                this.selectedDevices.delete(deviceId);
            } else {
                this.selectedDevices.add(deviceId);
            }
            document.getElementById('batch-actions').style.display =
                this.selectedDevices.size > 1 ? 'block' : 'none';
        } else {
            this.selectedDevice = device;
            this.selectedDevices.clear();
            this.selectedDevices.add(deviceId);
            // Start a live view immediately so the preview is instant, not a
            // single delayed still.
            this.stopStream();
            this.startStream();
        }

        this.renderDevices();
        this.updateActionButtons();
    }

    // Screenshot & Streaming

    // Reuse a single <img> so frames swap in place instead of rebuilding the
    // DOM node every frame (that caused flicker and slowdown).
    ensurePreviewImg() {
        const preview = document.getElementById('device-preview');
        let img = preview.querySelector('img');
        if (!img) {
            preview.innerHTML = '';
            img = document.createElement('img');
            img.alt = 'Device screen';
            preview.appendChild(img);
        }
        return img;
    }

    setPreviewSrc(src) {
        this.ensurePreviewImg().src = src;
    }

    // Display a raw JPEG blob via an object URL, revoking the previous one once
    // the new frame has decoded (avoids a memory leak during streaming).
    setPreviewBlob(blob) {
        const url = URL.createObjectURL(blob);
        const img = this.ensurePreviewImg();
        const previous = this._previewUrl;
        img.onload = () => { if (previous) URL.revokeObjectURL(previous); };
        img.src = url;
        this._previewUrl = url;
    }

    async updateScreenshot() {
        if (!this.selectedDevice) return;

        try {
            const data = await this.api(`/devices/${this.selectedDevice.device_id}/screenshot`);
            this.setPreviewSrc(`data:image/jpeg;base64,${data.screenshot}`);
        } catch (error) {
            document.getElementById('device-preview').innerHTML =
                '<p class="placeholder">Failed to load screenshot</p>';
        }
    }

    startStream() {
        if (!this.selectedDevice || this.isStreaming) return;

        this.ws = new WebSocket(`ws://${window.location.host}/ws`);
        this.ws.binaryType = 'blob';   // receive JPEG frames as binary, not JSON

        this.ws.onopen = () => {
            this.ws.send(JSON.stringify({
                action: 'start_stream',
                device_id: this.selectedDevice.device_id,
                interval: 80   // ~12 fps ceiling; actual rate paced by fetch time
            }));
            this.isStreaming = true;
            document.getElementById('toggle-stream').textContent = 'Stop Stream';
        };

        this.ws.onmessage = (event) => {
            if (event.data instanceof Blob) {
                this.setPreviewBlob(event.data);
            } else {
                // JSON fallback (kept for compatibility with older servers).
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'screenshot' && data.data) {
                        this.setPreviewSrc(`data:image/jpeg;base64,${data.data}`);
                    }
                } catch (e) { /* ignore non-frame messages */ }
            }
        };

        this.ws.onclose = () => {
            this.isStreaming = false;
            document.getElementById('toggle-stream').textContent = 'Start Stream';
        };
    }

    stopStream() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.isStreaming = false;
        document.getElementById('toggle-stream').textContent = 'Start Stream';
    }

    toggleStream() {
        if (this.isStreaming) {
            this.stopStream();
        } else {
            this.startStream();
        }
    }

    // Actions
    async performAction(action, deviceId = null) {
        const id = deviceId || this.selectedDevice?.device_id;
        if (!id) return;

        const endpoints = {
            'swipe-next': `/devices/${id}/swipe/next`,
            'swipe-previous': `/devices/${id}/swipe/previous`,
            'like': `/devices/${id}/like`,
            'save': `/devices/${id}/save`,
            'follow': `/devices/${id}/follow`
        };

        const endpoint = endpoints[action];
        if (!endpoint) return;

        try {
            const result = await this.api(endpoint, 'POST');
            if (result.success) {
                this.showToast(`${action} successful`, 'success');
                // The live stream already refreshes the preview; only pull a
                // one-off shot when streaming is off.
                if (!this.isStreaming) {
                    setTimeout(() => this.updateScreenshot(), 500);
                }
            } else {
                this.showToast(result.message || 'Action failed', 'error');
            }
        } catch (error) {
            console.error(`Action ${action} failed:`, error);
        }
    }

    async batchAction(action) {
        if (this.selectedDevices.size < 2) return;

        const deviceIds = Array.from(this.selectedDevices);

        try {
            const result = await this.api('/batch/action', 'POST', {
                device_ids: deviceIds,
                action: action
            });

            const successCount = Object.values(result.results).filter(r => r.success).length;
            this.showToast(`${action}: ${successCount}/${deviceIds.length} successful`,
                successCount === deviceIds.length ? 'success' : 'warning');
        } catch (error) {
            console.error(`Batch ${action} failed:`, error);
        }
    }

    // Comments
    async generateComments() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }

        const container = document.getElementById('comment-suggestions');
        container.innerHTML = '<p class="placeholder loading"></p>';

        try {
            const style = document.getElementById('comment-style').value;
            const result = await this.api(`/devices/${this.selectedDevice.device_id}/generate-comments`, 'POST', {
                device_id: this.selectedDevice.device_id,
                count: 3,
                styles: [style, 'friendly', 'funny']
            });

            container.innerHTML = result.comments.map((comment, i) => `
                <div class="comment-suggestion" data-comment="${this.escapeHtml(comment)}">
                    ${this.escapeHtml(comment)}
                </div>
            `).join('');

        } catch (error) {
            container.innerHTML = '<p class="placeholder">Failed to generate comments</p>';
        }
    }

    async postComment() {
        if (!this.selectedDevice) return;

        const customComment = document.getElementById('custom-comment').value;
        const selectedSuggestion = document.querySelector('.comment-suggestion.selected');
        const comment = customComment || selectedSuggestion?.dataset.comment;

        if (!comment) {
            this.showToast('Please enter or select a comment', 'warning');
            return;
        }

        try {
            const result = await this.api(`/devices/${this.selectedDevice.device_id}/comment`, 'POST', {
                device_id: this.selectedDevice.device_id,
                comment: comment,
                use_ai: false
            });

            if (result.success) {
                this.showToast('Comment posted', 'success');
                document.getElementById('custom-comment').value = '';
                document.querySelectorAll('.comment-suggestion').forEach(el => el.classList.remove('selected'));
            } else {
                this.showToast(result.message || 'Failed to post comment', 'error');
            }
        } catch (error) {
            console.error('Failed to post comment:', error);
        }
    }

    // Video Analysis
    async analyzeVideo() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }

        const container = document.getElementById('analysis-result');
        container.innerHTML = '<p class="placeholder loading"></p>';

        try {
            const result = await this.api(`/devices/${this.selectedDevice.device_id}/analyze`);

            let html = '';
            if (result.caption) {
                html += `<p><strong>Caption:</strong> ${this.escapeHtml(result.caption)}</p>`;
            }
            if (result.username) {
                html += `<p><strong>Creator:</strong> ${this.escapeHtml(result.username)}</p>`;
            }
            if (result.hashtags && result.hashtags.length > 0) {
                html += `<p><strong>Hashtags:</strong> ${result.hashtags.map(h =>
                    `<span class="hashtag">${this.escapeHtml(h)}</span>`
                ).join(' ')}</p>`;
            }
            if (result.ocr_text) {
                html += `<p><strong>Content:</strong> ${this.escapeHtml(result.ocr_text)}</p>`;
            }

            container.innerHTML = html || '<p class="placeholder">No content detected</p>';
        } catch (error) {
            container.innerHTML = '<p class="placeholder">Analysis failed</p>';
        }
    }

    // AI Agent
    renderAgentStep(step) {
        const log = document.getElementById('agent-log');
        if (log.querySelector('.placeholder')) log.innerHTML = '';
        const ok = step.success ? 'ok' : 'fail';
        const args = step.args && Object.keys(step.args).length
            ? ' ' + this.escapeHtml(JSON.stringify(step.args)) : '';
        const row = document.createElement('div');
        row.className = `agent-step ${ok}`;
        row.innerHTML = `<strong>${this.escapeHtml(step.action || 'error')}</strong>${args}` +
            ` <span class="agent-msg">${this.escapeHtml(step.message || '')}</span>`;
        log.appendChild(row);
        log.scrollTop = log.scrollHeight;
    }

    async agentStep() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }
        const goal = document.getElementById('agent-goal').value || undefined;
        try {
            const step = await this.api(`/devices/${this.selectedDevice.device_id}/ai/step`,
                'POST', { goal });
            this.renderAgentStep(step);
        } catch (error) {
            console.error('Agent step failed:', error);
        }
    }

    async agentRun() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }
        const goal = document.getElementById('agent-goal').value || undefined;
        const maxSteps = parseInt(document.getElementById('agent-steps').value) || 10;
        const runBtn = document.getElementById('agent-run');
        runBtn.disabled = true;
        runBtn.textContent = 'Running…';
        try {
            const result = await this.api(`/devices/${this.selectedDevice.device_id}/ai/run`,
                'POST', { goal, max_steps: maxSteps });
            document.getElementById('agent-log').innerHTML = '';
            result.steps.forEach(s => this.renderAgentStep(s));
            this.showToast(`Agent ran ${result.count} steps`, 'success');
        } catch (error) {
            console.error('Agent run failed:', error);
        } finally {
            runBtn.disabled = false;
            runBtn.textContent = 'Run Agent';
        }
    }

    // Interface Explorer — map TikTok's UI to real pixel coordinates
    async exploreAnalyze() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }
        const btn = document.getElementById('explore-analyze');
        btn.disabled = true; btn.textContent = 'Analyzing…';
        try {
            const r = await this.api(`/devices/${this.selectedDevice.device_id}/analyze-ui`, 'POST');
            this._explore = {
                screenshot: r.screenshot, imgw: r.imgw || 608, imgh: r.imgh || 1080,
                elements: (r.elements || []).map(e => ({
                    label: e.label, x: e.x, y: e.y, kind: e.kind || 'icon'
                })),
                selected: null
            };
            document.getElementById('explore-screen-type').value = r.screen_type || '';
            document.getElementById('explore-save').disabled = false;
            this.renderExplore();
        } catch (error) {
            console.error('Analyze UI failed:', error);
        } finally {
            btn.disabled = false; btn.textContent = 'Analyze current screen';
        }
    }

    renderExplore() {
        const ex = this._explore;
        if (!ex) return;
        const dots = ex.elements.map((el, i) =>
            `<div class="ex-dot ${ex.selected === i ? 'sel' : ''}" data-i="${i}" ` +
            `style="left:${el.x / ex.imgw * 100}%;top:${el.y / ex.imgh * 100}%">${i + 1}</div>`
        ).join('');
        document.getElementById('explore-canvas').innerHTML =
            `<div class="ex-imgwrap"><img src="data:image/jpeg;base64,${ex.screenshot}" alt="screen">` +
            `<div class="ex-overlay">${dots}</div></div>`;

        const rows = ex.elements.map((el, i) =>
            `<div class="ex-row ${ex.selected === i ? 'sel' : ''}" data-i="${i}">` +
            `<span class="ex-num">${i + 1}</span>` +
            `<input class="ex-label" data-i="${i}" value="${this.escapeHtml(el.label || '')}">` +
            `<input class="ex-x" type="number" data-i="${i}" value="${el.x}">` +
            `<input class="ex-y" type="number" data-i="${i}" value="${el.y}">` +
            `<button class="ex-pick btn-small" data-i="${i}">pick</button>` +
            `<button class="ex-del" data-i="${i}">×</button></div>`
        ).join('');
        document.getElementById('explore-list').innerHTML =
            rows + `<button id="explore-add" class="btn-small">+ add control</button>`;
    }

    updateExploreDot(i) {
        const ex = this._explore;
        const dot = document.querySelector(`.ex-dot[data-i="${i}"]`);
        if (dot && ex.elements[i]) {
            dot.style.left = ex.elements[i].x / ex.imgw * 100 + '%';
            dot.style.top = ex.elements[i].y / ex.imgh * 100 + '%';
        }
    }

    exploreCanvasClick(e) {
        const ex = this._explore;
        if (!ex || ex.selected == null) return;
        const img = document.querySelector('#explore-canvas img');
        if (!img) return;
        const rect = img.getBoundingClientRect();
        if (e.clientX < rect.left || e.clientX > rect.right ||
            e.clientY < rect.top || e.clientY > rect.bottom) return;
        ex.elements[ex.selected].x = Math.round((e.clientX - rect.left) / rect.width * ex.imgw);
        ex.elements[ex.selected].y = Math.round((e.clientY - rect.top) / rect.height * ex.imgh);
        this.renderExplore();
    }

    exploreListInput(e) {
        const ex = this._explore; if (!ex) return;
        const t = e.target, i = parseInt(t.dataset.i);
        if (isNaN(i) || !ex.elements[i]) return;
        if (t.classList.contains('ex-label')) ex.elements[i].label = t.value;
        else if (t.classList.contains('ex-x')) { ex.elements[i].x = parseInt(t.value) || 0; this.updateExploreDot(i); }
        else if (t.classList.contains('ex-y')) { ex.elements[i].y = parseInt(t.value) || 0; this.updateExploreDot(i); }
    }

    exploreListClick(e) {
        const ex = this._explore; if (!ex) return;
        const t = e.target;
        if (t.classList.contains('ex-pick')) { ex.selected = parseInt(t.dataset.i); this.renderExplore(); }
        else if (t.classList.contains('ex-del')) { ex.elements.splice(parseInt(t.dataset.i), 1); ex.selected = null; this.renderExplore(); }
        else if (t.id === 'explore-add') {
            ex.elements.push({ label: 'new_control', x: Math.round(ex.imgw / 2), y: Math.round(ex.imgh / 2), kind: 'icon' });
            ex.selected = ex.elements.length - 1; this.renderExplore();
        }
    }

    // ---------------------------------------------- Mirror re-cast calibration
    async mirrorOpenCC() {
        try {
            const r = await this.api('/mirror/open-cc', 'POST');
            this._mirror = { screenshot: r.screenshot, imgw: r.imgw || 608, imgh: r.imgh || 1080,
                             device_id: r.device_id, marking: null, btn: null, recv: null };
            this.renderMirror();
            document.getElementById('mirror-pick-btn').disabled = false;
            this.showToast('Control Center opened — click the Screen Mirroring button', 'info');
        } catch (e) { console.error(e); }
    }

    mirrorPick(which) {
        if (!this._mirror) return;
        this._mirror.marking = which;
        this.showToast(which === 'btn' ? 'Click the Screen Mirroring button on the image'
                                       : 'Click your PC in the receiver list', 'info');
    }

    mirrorCanvasClick(e) {
        const m = this._mirror;
        if (!m || !m.marking) return;
        const img = document.querySelector('#mirror-canvas img');
        if (!img) return;
        const rect = img.getBoundingClientRect();
        if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) return;
        const x = Math.round((e.clientX - rect.left) / rect.width * m.imgw);
        const y = Math.round((e.clientY - rect.top) / rect.height * m.imgh);
        if (m.marking === 'btn') m.btn = { x, y }; else m.recv = { x, y };
        m.marking = null;
        this.renderMirror();
    }

    async mirrorOpenList() {
        const m = this._mirror;
        if (!m || !m.btn) { this.showToast('Mark the Screen Mirroring button first', 'warning'); return; }
        try {
            await this.api(`/devices/${m.device_id}/tap`, 'POST', { x: m.btn.x, y: m.btn.y });
            await new Promise(r => setTimeout(r, 1600));
            const s = await this.api(`/devices/${m.device_id}/screenshot`);
            m.screenshot = s.screenshot;
            this.renderMirror();
            document.getElementById('mirror-pick-recv').disabled = false;
            this.showToast('Receiver list open — click your PC in the list', 'info');
        } catch (e) { console.error(e); }
    }

    async mirrorSave() {
        const m = this._mirror;
        if (!m || !m.btn || !m.recv) { this.showToast('Mark both points first', 'warning'); return; }
        try {
            await this.api('/ui-map', 'POST', {
                screen_type: 'control_center',
                elements: { screen_mirroring: [m.btn.x, m.btn.y], airplay_receiver: [m.recv.x, m.recv.y] },
                imgw: m.imgw, imgh: m.imgh,
            });
            this.showToast('Mirror re-cast calibrated ✓', 'success');
            this.mirrorCloseCC();
        } catch (e) { console.error(e); }
    }

    async mirrorCloseCC() {
        try { await this.api('/mirror/close-cc', 'POST'); } catch (e) { /* ignore */ }
    }

    renderMirror() {
        const m = this._mirror;
        const cv = document.getElementById('mirror-canvas');
        if (!m || !m.screenshot) { cv.innerHTML = '<p class="placeholder">Open Control Center to begin</p>'; return; }
        let dots = '';
        if (m.btn) dots += `<div class="ex-dot" style="left:${m.btn.x / m.imgw * 100}%;top:${m.btn.y / m.imgh * 100}%">M</div>`;
        if (m.recv) dots += `<div class="ex-dot ex-dot-recv" style="left:${m.recv.x / m.imgw * 100}%;top:${m.recv.y / m.imgh * 100}%">PC</div>`;
        cv.innerHTML = `<div class="ex-imgwrap"><img src="data:image/jpeg;base64,${m.screenshot}" alt="cc">${dots}</div>`;
        document.getElementById('mirror-btn-pt').textContent = m.btn ? `${m.btn.x},${m.btn.y}` : '—';
        document.getElementById('mirror-recv-pt').textContent = m.recv ? `${m.recv.x},${m.recv.y}` : '—';
        document.getElementById('mirror-open-list').disabled = !m.btn;
        document.getElementById('mirror-save').disabled = !(m.btn && m.recv);
    }

    async exploreSave() {
        const ex = this._explore; if (!ex) return;
        const screen = document.getElementById('explore-screen-type').value.trim();
        if (!screen) { this.showToast('Enter a screen type first', 'warning'); return; }
        const elements = {};
        ex.elements.forEach(el => { if (el.label) elements[el.label] = [el.x, el.y]; });
        try {
            await this.api('/ui-map', 'POST', { screen_type: screen, elements, imgw: ex.imgw, imgh: ex.imgh });
            this.showToast(`Saved "${screen}" to map`, 'success');
            this.loadUiMap();
        } catch (error) {
            console.error('Save UI map failed:', error);
        }
    }

    loadUiMap() {
        this.api('/ui-map').then(m => {
            document.getElementById('explore-map').textContent = JSON.stringify(m, null, 2);
        }).catch(() => {});
    }

    async copyUiMap() {
        try {
            const m = await this.api('/ui-map');
            await navigator.clipboard.writeText(JSON.stringify(m, null, 2));
            this.showToast('UI map copied — paste it in chat', 'success');
        } catch (error) {
            this.showToast('Copy failed (clipboard blocked)', 'error');
        }
    }

    // NutriSkin Campaign
    async startCampaign() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }
        const minutes = parseInt(document.getElementById('campaign-minutes').value) || 30;
        const language = (document.getElementById('campaign-language')?.value || '').trim();
        try {
            await this.api('/campaign/start', 'POST', {
                device_id: this.selectedDevice.device_id,
                duration_minutes: minutes,
                language: language || null
            });
            this.showToast(`Campaign started (${minutes} min)`, 'success');
            document.getElementById('campaign-log').innerHTML = '';
            this.startCampaignPolling();
        } catch (error) {
            console.error('Campaign start failed:', error);
        }
    }

    async stopCampaign() {
        try {
            await this.api('/campaign/stop', 'POST');
            this.showToast('Stopping campaign…', 'info');
        } catch (error) {
            console.error('Campaign stop failed:', error);
        }
    }

    startCampaignPolling() {
        if (this._campaignTimer) return;
        this._campaignTimer = setInterval(() => this.pollCampaign(), 2500);
        this.pollCampaign();
    }

    async pollCampaign() {
        try {
            const s = await this.api('/campaign/status');
            this.renderCampaign(s);
            if (s.running && !this._campaignTimer) {
                this._campaignTimer = setInterval(() => this.pollCampaign(), 2500);
            } else if (!s.running && this._campaignTimer) {
                clearInterval(this._campaignTimer);
                this._campaignTimer = null;
            }
        } catch (error) {
            clearInterval(this._campaignTimer);
            this._campaignTimer = null;
        }
    }

    copyCampaignLog() {
        const s = this._lastCampaign;
        if (!s || !(s.recent || []).length) {
            this.showToast('No campaign activity yet', 'warning');
            return;
        }
        const st = s.stats || {};
        const mins = (sec) => `${Math.floor((sec || 0) / 60)}m ${Math.round((sec || 0) % 60)}s`;
        const header = `NutriSkin campaign — ${mins(s.elapsed)} elapsed · ` +
            `videos ${st.videos || 0}, relevant ${st.relevant || 0}, likes ${st.likes || 0}, ` +
            `saves ${st.saves || 0}, comments ${st.comments || 0}, recovered ${st.recovered || 0}, ` +
            `errors ${st.errors || 0}`;
        const lines = s.recent.map(e => {
            const tag = e.on_feed === false ? 'OFF-FEED'
                : (e.relevant === true ? 'RELEVANT' : (e.relevant === false ? 'skip' : 'error'));
            const acts = (e.actions || []).length ? ` [${e.actions.join(', ')}]` : '';
            const w = e.watch ? ` (${e.watch}s)` : '';
            let line = `- ${tag}${acts}${w}`;
            if (e.post_type === 'photo_slideshow') line += ' | slideshow';
            if (e.screen) line += ` | screen: ${e.screen}`;
            if (e.recovery) line += ` | recover: ${e.recovery}`;
            if (e.reason) line += ` | reason: ${e.reason}`;
            if (e.comment) line += ` | comment: ${e.comment}`;
            return line;
        });
        const text = header + '\n' + lines.join('\n');
        navigator.clipboard.writeText(text)
            .then(() => this.showToast('Log copied — paste it in chat', 'success'))
            .catch(() => this.showToast('Copy failed (clipboard blocked)', 'error'));
    }

    renderCampaign(s) {
        this._lastCampaign = s;
        const startBtn = document.getElementById('campaign-start');
        const stopBtn = document.getElementById('campaign-stop');
        startBtn.style.display = s.running ? 'none' : '';
        stopBtn.style.display = s.running ? '' : 'none';

        const st = s.stats || {};
        const mins = (sec) => `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
        document.getElementById('campaign-status').innerHTML = s.running || s.elapsed
            ? `<span class="${s.running ? 'running' : ''}">${s.running ? '● running' : '○ stopped'}</span> ` +
              `· ${mins(s.elapsed || 0)} elapsed` + (s.running ? ` / ${mins(s.remaining || 0)} left` : '') +
              ` · videos ${st.videos || 0} · relevant ${st.relevant || 0} · ` +
              `likes ${st.likes || 0} · saves ${st.saves || 0} · comments ${st.comments || 0}` +
              (st.recovered ? ` · recovered ${st.recovered}` : '') +
              (st.errors ? ` · errors ${st.errors}` : '')
            : '';

        // Live snapshot of what the agent actually saw on its last cycle.
        const snap = document.getElementById('campaign-snapshot');
        if (s.last_screenshot) {
            snap.innerHTML = `<img src="data:image/jpeg;base64,${s.last_screenshot}" alt="agent view">`;
        }

        const log = document.getElementById('campaign-log');
        if ((s.recent || []).length) {
            log.innerHTML = s.recent.slice().reverse().map(e => {
                let cls, tag;
                if (e.on_feed === false) { cls = 'offfeed'; tag = 'OFF-FEED'; }
                else if (e.relevant === true) { cls = 'ok'; tag = 'RELEVANT'; }
                else if (e.relevant === false) { cls = 'skip'; tag = 'skip'; }
                else { cls = 'fail'; tag = 'error'; }
                const acts = (e.actions || []).length ? ` [${e.actions.join(', ')}]` : '';
                const slide = e.post_type === 'photo_slideshow' ? ' <span class="campaign-meta">📷 slideshow</span>' : '';
                const meta = (e.watch ? ` <span class="campaign-meta">(${e.watch}s)</span>` : '') + slide;
                // Show what was ACTUALLY on screen, per Claude, next to the action.
                const screen = e.screen ? `<span class="campaign-screen">screen: ${this.escapeHtml(e.screen)}</span>` : '';
                const rec = e.recovery ? `<span class="agent-msg">↩ ${this.escapeHtml(e.recovery)}</span>` : '';
                const cmt = e.comment ? `<span class="agent-msg">💬 ${this.escapeHtml(e.comment)}</span>` : '';
                const reason = e.reason ? `<span class="agent-msg">${this.escapeHtml(e.reason)}</span>` : '';
                return `<div class="agent-step ${cls}"><strong>${tag}</strong>${acts}${meta} ` +
                    `${screen}${rec}${reason}${cmt}</div>`;
            }).join('');
        }
    }

    // Manual Keyboard
    async manualType() {
        if (!this.selectedDevice) return;
        const input = document.getElementById('manual-text');
        const text = input.value;
        if (!text) return;
        try {
            const res = await this.api(`/devices/${this.selectedDevice.device_id}/type`,
                'POST', { text });
            if (res.success) {
                this.showToast('Text sent', 'success');
                input.value = '';
            } else {
                this.showToast(res.message || 'Failed to send text', 'error');
            }
        } catch (error) {
            console.error('Manual type failed:', error);
        }
    }

    async manualKey(fnKey) {
        if (!this.selectedDevice) return;
        try {
            await this.api(`/devices/${this.selectedDevice.device_id}/key`,
                'POST', { fn_key: fnKey });
        } catch (error) {
            console.error('Manual key failed:', error);
        }
    }

    // Tap Test — click the live view; the phone taps that exact coordinate
    async previewTap(e) {
        if (!this.selectedDevice) return;
        const img = document.querySelector('#device-preview img');
        if (!img) { this.showToast('Start the stream or take a screenshot first', 'warning'); return; }
        const rect = img.getBoundingClientRect();
        if (e.clientX < rect.left || e.clientX > rect.right ||
            e.clientY < rect.top || e.clientY > rect.bottom) return;
        const w = this.selectedDevice.width || 608;
        const h = this.selectedDevice.height || 1080;
        const x = Math.round((e.clientX - rect.left) / rect.width * w);
        const y = Math.round((e.clientY - rect.top) / rect.height * h);
        document.getElementById('tap-readout').textContent = `tapped ${x}, ${y}`;
        try {
            const r = await this.api(`/devices/${this.selectedDevice.device_id}/tap`, 'POST', { x, y });
            if (!r.success) this.showToast(r.message || 'Tap failed', 'error');
        } catch (error) {
            console.error('Tap failed:', error);
        }
    }

    // UI Calibration
    startCalibration(element) {
        this.calibrationMode = element;
        this.showToast(`Click on the ${element.replace('_', ' ')} in the preview`, 'info');

        const preview = document.getElementById('device-preview');
        preview.style.cursor = 'crosshair';
    }

    async handleCalibrationClick(event) {
        if (!this.calibrationMode || !this.selectedDevice) return;

        const preview = document.getElementById('device-preview');
        const rect = preview.getBoundingClientRect();
        const img = preview.querySelector('img');

        if (!img) return;

        // Calculate relative position
        const x = (event.clientX - rect.left) / rect.width;
        const y = (event.clientY - rect.top) / rect.height;

        try {
            await this.api('/config/ui', 'POST', {
                element: this.calibrationMode,
                x: x,
                y: y
            });

            this.showToast(`${this.calibrationMode} calibrated`, 'success');
        } catch (error) {
            console.error('Calibration failed:', error);
        }

        this.calibrationMode = null;
        preview.style.cursor = 'default';
    }

    // UI Helpers
    updateConnectionStatus(connected) {
        const status = document.getElementById('connection-status');
        status.textContent = connected ? 'Connected' : 'Disconnected';
        status.className = `status ${connected ? 'connected' : 'disconnected'}`;
    }

    updateActionButtons() {
        const hasDevice = !!this.selectedDevice;
        document.querySelectorAll('.action-btn, #generate-comments, #post-comment, #analyze-video, #toggle-stream, #take-screenshot, #agent-step, #agent-run, #manual-send, .manual-key, #campaign-start, #explore-analyze').forEach(btn => {
            btn.disabled = !hasDevice;
        });
    }

    openModal() {
        document.getElementById('settings-modal').classList.add('open');
    }

    closeModal() {
        document.getElementById('settings-modal').classList.remove('open');
    }

    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;
        container.appendChild(toast);

        setTimeout(() => toast.remove(), 3000);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Event Binding
    bindEvents() {
        // Developer view toggle
        document.getElementById('dev-toggle')?.addEventListener('click', () => this.toggleDevView(true));
        document.getElementById('dev-back')?.addEventListener('click', () => this.toggleDevView(false));
        document.getElementById('refresh-grid')?.addEventListener('click', async () => { await this.loadDevices(); this.renderGrid(); });

        // Phone grid (event delegation on the grid container)
        const grid = document.getElementById('phone-grid');
        if (grid) {
            grid.addEventListener('click', (e) => {
                const wu = e.target.closest('.ph-warmup');
                if (wu) return this.gridWarmup(wu.dataset.deviceId);
                const hm = e.target.closest('.ph-home');
                if (hm) return this.gridHome(hm.dataset.deviceId);
                const ct = e.target.closest('.ph-control');
                if (ct) return this.gridToggleControl(ct.dataset.deviceId);
                const scr = e.target.closest('.phone-screen');
                if (scr) return this.gridScreenTap(scr.id.replace('screen-', ''), e);
            });
        }

        // Device selection
        document.getElementById('device-list').addEventListener('click', (e) => {
            const item = e.target.closest('.device-item');
            if (item) {
                this.selectDevice(item.dataset.deviceId, e.ctrlKey || e.metaKey);
            }
        });

        // Refresh devices
        document.getElementById('refresh-devices').addEventListener('click', () => this.loadDevices());

        // Action buttons
        document.getElementById('action-swipe-up').addEventListener('click', () => this.performAction('swipe-next'));
        document.getElementById('action-swipe-down').addEventListener('click', () => this.performAction('swipe-previous'));
        document.getElementById('action-like').addEventListener('click', () => this.performAction('like'));
        document.getElementById('action-save').addEventListener('click', () => this.performAction('save'));
        document.getElementById('action-follow').addEventListener('click', () => this.performAction('follow'));
        document.getElementById('action-comment').addEventListener('click', () => {
            document.querySelector('.comment-panel').scrollIntoView({ behavior: 'smooth' });
        });

        // Batch actions
        document.getElementById('batch-swipe').addEventListener('click', () => this.batchAction('swipe_next'));
        document.getElementById('batch-like').addEventListener('click', () => this.batchAction('like'));

        // Screenshot controls
        document.getElementById('toggle-stream').addEventListener('click', () => this.toggleStream());
        document.getElementById('take-screenshot').addEventListener('click', () => this.updateScreenshot());

        // Comments
        document.getElementById('generate-comments').addEventListener('click', () => this.generateComments());
        document.getElementById('post-comment').addEventListener('click', () => this.postComment());

        document.getElementById('comment-suggestions').addEventListener('click', (e) => {
            const suggestion = e.target.closest('.comment-suggestion');
            if (suggestion) {
                document.querySelectorAll('.comment-suggestion').forEach(el => el.classList.remove('selected'));
                suggestion.classList.add('selected');
                document.getElementById('custom-comment').value = suggestion.dataset.comment;
            }
        });

        // Analysis
        document.getElementById('analyze-video').addEventListener('click', () => this.analyzeVideo());

        // AI Agent
        document.getElementById('agent-step').addEventListener('click', () => this.agentStep());
        document.getElementById('agent-run').addEventListener('click', () => this.agentRun());

        // Interface Explorer
        document.getElementById('explore-analyze').addEventListener('click', () => this.exploreAnalyze());
        document.getElementById('explore-save').addEventListener('click', () => this.exploreSave());
        document.getElementById('explore-copy-map').addEventListener('click', () => this.copyUiMap());
        document.getElementById('explore-canvas').addEventListener('click', (e) => this.exploreCanvasClick(e));
        document.getElementById('explore-list').addEventListener('input', (e) => this.exploreListInput(e));
        document.getElementById('explore-list').addEventListener('click', (e) => this.exploreListClick(e));

        // Mirror Re-cast Calibration
        document.getElementById('mirror-open-cc').addEventListener('click', () => this.mirrorOpenCC());
        document.getElementById('mirror-pick-btn').addEventListener('click', () => this.mirrorPick('btn'));
        document.getElementById('mirror-open-list').addEventListener('click', () => this.mirrorOpenList());
        document.getElementById('mirror-pick-recv').addEventListener('click', () => this.mirrorPick('recv'));
        document.getElementById('mirror-save').addEventListener('click', () => this.mirrorSave());
        document.getElementById('mirror-close').addEventListener('click', () => this.mirrorCloseCC());
        document.getElementById('mirror-canvas').addEventListener('click', (e) => this.mirrorCanvasClick(e));

        // NutriSkin Campaign
        document.getElementById('campaign-start').addEventListener('click', () => this.startCampaign());
        document.getElementById('campaign-stop').addEventListener('click', () => this.stopCampaign());
        document.getElementById('campaign-copy').addEventListener('click', () => this.copyCampaignLog());

        // Manual Keyboard
        document.getElementById('manual-send').addEventListener('click', () => this.manualType());
        document.getElementById('manual-text').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); this.manualType(); }
        });
        document.querySelectorAll('.manual-key').forEach(btn => {
            btn.addEventListener('click', () => this.manualKey(btn.dataset.fn));
        });

        // Settings
        document.getElementById('settings-btn').addEventListener('click', () => this.openModal());
        document.querySelector('.close-btn').addEventListener('click', () => this.closeModal());
        document.getElementById('save-settings').addEventListener('click', () => this.saveConfig());

        // Calibration
        document.getElementById('calibration-element').addEventListener('change', (e) => {
            if (e.target.value) {
                this.startCalibration(e.target.value);
            }
        });

        document.getElementById('device-preview').addEventListener('click', (e) => {
            if (this.calibrationMode) {
                this.handleCalibrationClick(e);
            } else if (document.getElementById('tap-mode').checked) {
                this.previewTap(e);
            }
        });

        // Modal close on outside click
        document.getElementById('settings-modal').addEventListener('click', (e) => {
            if (e.target.id === 'settings-modal') {
                this.closeModal();
            }
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            switch(e.key) {
                case 'ArrowUp':
                    e.preventDefault();
                    this.performAction('swipe-previous');
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    this.performAction('swipe-next');
                    break;
                case 'l':
                    this.performAction('like');
                    break;
                case 's':
                    this.performAction('save');
                    break;
                case 'f':
                    this.performAction('follow');
                    break;
            }
        });
    }
}

// Initialize app
document.addEventListener('DOMContentLoaded', () => {
    window.app = new TikTokController();
});

```

## `frontend/styles.css`

```css
/* TikTok Controller Styles */
:root {
    --bg-primary: #0a0a0a;
    --bg-secondary: #1a1a1a;
    --bg-tertiary: #2a2a2a;
    --text-primary: #ffffff;
    --text-secondary: #a0a0a0;
    --accent: #fe2c55;
    --accent-hover: #ff4466;
    --success: #25f4ee;
    --warning: #ffcc00;
    --error: #ff3333;
    --border: #333;
    --radius: 8px;
    --shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
}

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-height: 100vh;
}

.container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 20px;
}

/* Header */
header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 15px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 20px;
}

header h1 {
    font-size: 1.5rem;
    font-weight: 600;
}

.status-bar {
    display: flex;
    align-items: center;
    gap: 15px;
}

.status {
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 0.8rem;
    font-weight: 500;
}

.status.connected {
    background: rgba(37, 244, 238, 0.2);
    color: var(--success);
}

.status.disconnected {
    background: rgba(255, 51, 51, 0.2);
    color: var(--error);
}

.icon-btn {
    background: transparent;
    border: none;
    color: var(--text-secondary);
    font-size: 1.5rem;
    cursor: pointer;
    padding: 5px;
    transition: color 0.2s;
}

.icon-btn:hover {
    color: var(--text-primary);
}

/* Main Layout */
main {
    display: grid;
    grid-template-columns: 250px 1fr 300px;
    grid-template-rows: auto auto;
    gap: 20px;
}

/* Panels */
section {
    background: var(--bg-secondary);
    border-radius: var(--radius);
    padding: 20px;
    border: 1px solid var(--border);
}

.panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 15px;
}

h2 {
    font-size: 1.1rem;
    font-weight: 600;
}

h3 {
    font-size: 1rem;
    font-weight: 600;
    margin-bottom: 15px;
}

h4 {
    font-size: 0.9rem;
    font-weight: 600;
    margin-bottom: 10px;
    color: var(--text-secondary);
}

/* Device Panel */
.device-panel {
    grid-row: span 2;
}

.device-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.device-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px;
    background: var(--bg-tertiary);
    border-radius: var(--radius);
    cursor: pointer;
    transition: all 0.2s;
    border: 2px solid transparent;
}

.device-item:hover {
    border-color: var(--accent);
}

.device-item.selected {
    border-color: var(--accent);
    background: rgba(254, 44, 85, 0.1);
}

.device-item.multi-selected {
    border-color: var(--success);
    background: rgba(37, 244, 238, 0.1);
}

.device-status {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--error);
}

.device-status.online {
    background: var(--success);
}

.device-info {
    flex: 1;
    overflow: hidden;
}

.device-name {
    font-weight: 500;
    font-size: 0.9rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.device-model {
    font-size: 0.75rem;
    color: var(--text-secondary);
}

/* Control Panel */
.control-panel {
    display: flex;
    gap: 20px;
}

.preview-container {
    flex: 0 0 300px;
}

.device-preview {
    width: 100%;
    aspect-ratio: 9/16;
    background: var(--bg-tertiary);
    border-radius: var(--radius);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    position: relative;
}

.device-preview img {
    width: 100%;
    height: 100%;
    object-fit: contain;
}

.preview-controls {
    display: flex;
    gap: 10px;
    margin-top: 10px;
}

.actions-container {
    flex: 1;
}

.action-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
}

.action-btn {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 5px;
    padding: 20px 15px;
    background: var(--bg-tertiary);
    border: 2px solid transparent;
    border-radius: var(--radius);
    color: var(--text-primary);
    cursor: pointer;
    transition: all 0.2s;
}

.action-btn:hover:not(:disabled) {
    border-color: var(--accent);
    background: rgba(254, 44, 85, 0.1);
}

.action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}

.action-btn:active:not(:disabled) {
    transform: scale(0.95);
}

.action-icon {
    font-size: 1.5rem;
}

.action-btn span:last-child {
    font-size: 0.8rem;
}

.batch-actions {
    margin-top: 20px;
    padding-top: 15px;
    border-top: 1px solid var(--border);
}

.batch-buttons {
    display: flex;
    gap: 10px;
}

/* Comment Panel */
.comment-panel {
    grid-column: 3;
    grid-row: span 2;
}

.comment-controls {
    display: flex;
    gap: 10px;
    margin-bottom: 15px;
}

.comment-controls select {
    flex: 1;
}

.comment-suggestions {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 15px;
    max-height: 200px;
    overflow-y: auto;
}

.comment-suggestion {
    padding: 10px;
    background: var(--bg-tertiary);
    border-radius: var(--radius);
    cursor: pointer;
    font-size: 0.9rem;
    transition: all 0.2s;
    border: 2px solid transparent;
}

.comment-suggestion:hover {
    border-color: var(--accent);
}

.comment-suggestion.selected {
    border-color: var(--accent);
    background: rgba(254, 44, 85, 0.1);
}

.custom-comment {
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.custom-comment textarea {
    width: 100%;
    height: 80px;
    padding: 10px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text-primary);
    font-family: inherit;
    resize: vertical;
}

.custom-comment textarea:focus {
    outline: none;
    border-color: var(--accent);
}

/* Analysis Panel */
.analysis-panel {
    grid-column: 2;
}

.analysis-result {
    margin-top: 15px;
    padding: 15px;
    background: var(--bg-tertiary);
    border-radius: var(--radius);
    font-size: 0.9rem;
}

.analysis-result .hashtag {
    display: inline-block;
    background: rgba(37, 244, 238, 0.2);
    color: var(--success);
    padding: 2px 8px;
    border-radius: 12px;
    margin: 2px;
    font-size: 0.8rem;
}

/* Buttons */
.btn {
    padding: 10px 20px;
    background: var(--accent);
    border: none;
    border-radius: var(--radius);
    color: white;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
}

.btn:hover:not(:disabled) {
    background: var(--accent-hover);
}

.btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}

.btn-small {
    padding: 6px 12px;
    font-size: 0.85rem;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text-primary);
    cursor: pointer;
    transition: all 0.2s;
}

.btn-small:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
}

.btn-small:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}

/* Form Elements */
select, input[type="text"], input[type="number"], input[type="password"] {
    padding: 8px 12px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text-primary);
    font-size: 0.9rem;
}

select:focus, input:focus {
    outline: none;
    border-color: var(--accent);
}

/* Modal */
.modal {
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.8);
    z-index: 1000;
    align-items: center;
    justify-content: center;
}

.modal.open {
    display: flex;
}

.modal-content {
    background: var(--bg-secondary);
    border-radius: var(--radius);
    width: 90%;
    max-width: 500px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: var(--shadow);
}

.modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 15px 20px;
    border-bottom: 1px solid var(--border);
}

.modal-header h2 {
    margin: 0;
}

.close-btn {
    background: none;
    border: none;
    color: var(--text-secondary);
    font-size: 1.5rem;
    cursor: pointer;
}

.close-btn:hover {
    color: var(--text-primary);
}

.modal-body {
    padding: 20px;
}

.setting-group {
    margin-bottom: 20px;
}

.setting-group label {
    display: block;
    margin-bottom: 10px;
    color: var(--text-secondary);
    font-size: 0.9rem;
}

.setting-group input, .setting-group select {
    width: 100%;
    margin-top: 5px;
}

.hint {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin-bottom: 10px;
}

/* Toast Notifications */
#toast-container {
    position: fixed;
    bottom: 20px;
    right: 20px;
    z-index: 2000;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.toast {
    padding: 12px 20px;
    background: var(--bg-secondary);
    border-radius: var(--radius);
    border-left: 4px solid var(--accent);
    box-shadow: var(--shadow);
    animation: slideIn 0.3s ease;
}

.toast.success {
    border-left-color: var(--success);
}

.toast.error {
    border-left-color: var(--error);
}

.toast.warning {
    border-left-color: var(--warning);
}

@keyframes slideIn {
    from {
        transform: translateX(100%);
        opacity: 0;
    }
    to {
        transform: translateX(0);
        opacity: 1;
    }
}

/* Placeholder Text */
.placeholder {
    color: var(--text-secondary);
    font-size: 0.9rem;
    text-align: center;
    padding: 20px;
}

/* Loading Spinner */
.loading {
    position: relative;
}

.loading::after {
    content: '';
    position: absolute;
    top: 50%;
    left: 50%;
    width: 20px;
    height: 20px;
    margin: -10px 0 0 -10px;
    border: 2px solid var(--text-secondary);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}

@keyframes spin {
    to {
        transform: rotate(360deg);
    }
}

/* Responsive */
@media (max-width: 1200px) {
    main {
        grid-template-columns: 200px 1fr 280px;
    }
}

@media (max-width: 900px) {
    main {
        grid-template-columns: 1fr;
        grid-template-rows: auto;
    }

    .device-panel {
        grid-row: auto;
    }

    .device-list {
        flex-direction: row;
        flex-wrap: wrap;
    }

    .device-item {
        flex: 1 1 150px;
    }

    .control-panel {
        flex-direction: column;
    }

    .preview-container {
        flex: none;
        display: flex;
        flex-direction: column;
        align-items: center;
    }

    .device-preview {
        max-width: 300px;
    }

    .comment-panel {
        grid-column: auto;
        grid-row: auto;
    }

    .analysis-panel {
        grid-column: auto;
    }
}

/* AI Agent panel */
.agent-panel textarea#agent-goal {
    width: 100%;
    min-height: 52px;
    margin-bottom: 10px;
    padding: 10px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    resize: vertical;
    font-family: inherit;
}
.agent-controls {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 12px;
    flex-wrap: wrap;
}
.agent-controls input#agent-steps {
    width: 60px;
    padding: 6px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
}
.agent-log {
    max-height: 320px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.agent-step {
    padding: 8px 10px;
    border-radius: var(--radius);
    background: var(--bg-tertiary);
    border-left: 3px solid var(--success);
    font-size: 0.9em;
}
.agent-step.fail {
    border-left-color: var(--error);
}
.agent-step .agent-msg {
    color: var(--text-secondary);
    display: block;
    margin-top: 2px;
}

/* Manual keyboard panel */
.keyboard-controls {
    display: flex;
    gap: 10px;
    margin-bottom: 10px;
}
.keyboard-controls input#manual-text {
    flex: 1;
    padding: 10px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font-family: inherit;
}
.keyboard-keys {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

/* NutriSkin campaign panel */
.campaign-controls {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
    flex-wrap: wrap;
}
.campaign-controls input#campaign-minutes {
    width: 64px;
    padding: 6px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
}
.campaign-status {
    font-size: 0.85em;
    color: var(--text-secondary);
    margin-bottom: 10px;
}
.campaign-status .running { color: var(--success); font-weight: 600; }
.campaign-meta { color: var(--text-secondary); font-size: 0.85em; }
.agent-step.skip { border-left-color: var(--border); opacity: 0.8; }

/* Campaign live view: snapshot + log side by side */
.campaign-live {
    display: flex;
    gap: 12px;
    align-items: flex-start;
}
.campaign-snapshot {
    flex: 0 0 auto;
    width: 150px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    min-height: 100px;
    display: flex;
    align-items: center;
    justify-content: center;
}
.campaign-snapshot img { width: 100%; display: block; }
.campaign-live .agent-log { flex: 1; max-height: 380px; }
.campaign-screen {
    display: block;
    color: var(--success);
    font-size: 0.82em;
    margin-top: 2px;
}
.agent-step.offfeed { border-left-color: var(--warning); }
.agent-step.offfeed .campaign-screen { color: var(--warning); }
@media (max-width: 640px) {
    .campaign-live { flex-direction: column; }
    .campaign-snapshot { width: 100%; max-width: 220px; }
}

/* Interface Explorer */
.explorer-controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.explorer-controls input#explore-screen-type { padding: 6px 8px; background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border); border-radius: var(--radius); }
.explorer-body { display: flex; gap: 14px; align-items: flex-start; }
.explore-canvas { flex: 0 0 auto; }
.ex-imgwrap { position: relative; display: inline-block; line-height: 0; cursor: crosshair; }
.ex-imgwrap img { width: 230px; display: block; border: 1px solid var(--border); border-radius: var(--radius); }
.ex-overlay { position: absolute; inset: 0; pointer-events: none; }
.ex-dot { position: absolute; transform: translate(-50%, -50%); min-width: 18px; height: 18px; padding: 0 3px; background: rgba(254,44,85,0.85); color: #fff; font-size: 11px; font-weight: 700; border: 1px solid #fff; border-radius: 9px; display: flex; align-items: center; justify-content: center; box-shadow: 0 0 4px rgba(0,0,0,0.6); }
.ex-dot.sel { background: var(--success); color: #000; transform: translate(-50%, -50%) scale(1.25); z-index: 2; }
.ex-dot.ex-dot-recv { background: rgba(37,150,190,0.9); }
.mirror-controls { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
.explore-list { flex: 1; display: flex; flex-direction: column; gap: 5px; max-height: 420px; overflow-y: auto; }
.ex-row { display: flex; align-items: center; gap: 5px; }
.ex-row.sel { background: rgba(37,244,238,0.08); border-radius: var(--radius); }
.ex-num { width: 18px; color: var(--text-secondary); font-size: 12px; text-align: right; }
.ex-row input { padding: 4px 6px; background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border); border-radius: 4px; font-size: 12px; }
.ex-row .ex-label { flex: 1; min-width: 90px; }
.ex-row .ex-x, .ex-row .ex-y { width: 56px; }
.ex-row .ex-del { background: none; border: none; color: var(--error); cursor: pointer; font-size: 15px; }
.explore-saved { margin-top: 12px; }
.explore-saved pre { background: var(--bg-tertiary); padding: 10px; border-radius: var(--radius); max-height: 240px; overflow: auto; font-size: 12px; }
@media (max-width: 720px) { .explorer-body { flex-direction: column; } }

/* Tap test */
.tap-mode-label { display: inline-flex; align-items: center; gap: 4px; font-size: 0.85em; color: var(--text-secondary); }
.tap-readout { font-size: 0.8em; color: var(--success); font-family: monospace; }

/* Simple UI: Advanced tools toggle */
.advanced-toggle-row { margin: 18px 0 8px; text-align: center; }
#advanced-toggle { opacity: 0.85; }
.advanced-panels { display: flex; flex-direction: column; gap: 20px; padding-top: 6px; border-top: 1px dashed var(--border); margin-top: 4px; }
.advanced-panels[hidden] { display: none; }
.actions-panel { }

/* Simple UI layout: stacked single column (overrides the old 3-col grid) */
.container { max-width: 1080px; }
main { display: flex; flex-direction: column; gap: 20px; }
.device-panel, .comment-panel, .analysis-panel { grid-column: auto; grid-row: auto; }
.control-panel { display: flex; gap: 20px; align-items: flex-start; }
.campaign-live { display: flex; gap: 16px; flex-wrap: wrap; }
.campaign-snapshot { flex: 0 0 260px; }
.campaign-snapshot img { width: 100%; border-radius: var(--radius); display: block; }
#campaign-log { flex: 1; min-width: 280px; }
.campaign-controls { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
@media (max-width: 720px) { .control-panel { flex-direction: column; } .preview-container { flex: 1 1 auto; } }

/* ---- Phone grid (main view) ---- */
main[hidden] { display: none; }   /* author 'main{display:flex}' would otherwise beat [hidden] */
.grid-head { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
.grid-head h2 { margin: 0; }
.phone-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 20px; }
.phone-card { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; display: flex; flex-direction: column; gap: 10px; }
.phone-card.warming { border-color: var(--success); box-shadow: inset 0 0 0 1px var(--success); }
.phone-screen { width: 100%; aspect-ratio: 9/16; background: var(--bg-tertiary); border-radius: var(--radius); overflow: hidden; display: flex; align-items: center; justify-content: center; position: relative; }
.phone-screen img { width: 100%; height: 100%; object-fit: contain; display: block; }
.phone-screen.controllable { outline: 2px solid var(--success); outline-offset: -2px; cursor: crosshair; }
.ph-ph { color: var(--text-secondary); font-size: 0.85em; }
.phone-niche { width: 100%; padding: 8px; background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border); border-radius: var(--radius); }
.phone-actions { display: flex; gap: 6px; }
.phone-actions .btn-small { flex: 1; }
.ph-warmup.active { background: var(--error); color: #fff; }
.ph-control.active { background: var(--success); color: #000; }
.ph-label { font-size: 0.72em; color: var(--text-secondary); text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* Saved warm-up runs (Developer screen) */
.runs-panel .runs-list { display: flex; flex-direction: column; gap: 6px; }
.run-row { display: flex; align-items: center; gap: 10px; padding: 8px 10px; background: var(--bg-secondary, #181b22); border: 1px solid var(--border, #262a33); border-radius: 8px; font-size: 0.85em; }
.run-row > a:first-child { font-family: monospace; color: var(--accent, #6ea8fe); text-decoration: none; }
.run-row > a:first-child:hover { text-decoration: underline; }
.run-row .run-stats { flex: 1; color: var(--text-secondary); }
.run-row .run-frames { color: var(--accent, #6ea8fe); text-decoration: none; white-space: nowrap; }

```

## `README.md`

```markdown
# TikTok Accessibility Controller

A web-based controller for driving TikTok on a physical iPhone through the
**iMouse XP** phone-control setup. Taps and swipes travel over USB (the control
board presents as a HID keyboard/mouse to iOS); the phone screen is read back
over AirPlay mirroring. Optionally, **Claude** looks at the screen to understand
the current video and draft contextual comments.

No jailbreak, no app on the phone, no signing.

## How it fits together

```
Browser  ──HTTP/WS──►  this server (FastAPI, :8000)  ──HTTP──►  iMouse XP console (127.0.0.1:9911)
                                                                     │
                                              USB ── control board ── Lightning ── iPhone
                                                                     └── screen mirrored to PC (AirPlay)
```

Two independent channels to the phone:

- **Control** (taps/swipes/keys) over USB. Requires iOS accessibility settings
  (AssistiveTouch, full keyboard access, trackpad tracking at max).
- **Vision** (the screen image) over Wi-Fi via AirPlay. Phone and PC must be on
  the same subnet. If mirroring is down, screenshots and OCR fail even while
  control still works.

## Requirements

- Windows PC running the **iMouse XP** console (the local API lives inside it).
- iPhone 6s or newer, **iOS 13.4+**, connected via the board and mirrored to the PC.
- Python 3.10+.
- Optional: an **Anthropic API key** for AI comments/analysis.
- Optional: the **PaddleOCR plugin** running, for OCR / find-text.

## Key facts this controller is built on

- **API base:** `http://127.0.0.1:9911/api`. There is **no API key** — auth is
  being on localhost. Port **9911 is verified**; 9912 (from some third-party
  guides) is wrong.
- **Success = HTTP `status == 200` AND `data.code == 0`.** A 200 with a non-zero
  `data.code` is a failure.
- **Coordinate space:** all taps and OCR centres are in `imgw × imgh` (the
  mirror-stream resolution, e.g. 608×1080) — **read at runtime per device**,
  never the iOS logical points (`width`/`height`) and never hardcoded.
- **Screenshots** with `binary:true` return raw JPEG bytes, not JSON.
- **Non-ASCII text** (emoji/accents): set the clipboard, focus the field, then
  paste with `WIN+v` — `/key/sendkey` is ASCII only.
- **OCR** returns code **28** until the PaddleOCR plugin process is running;
  those calls are skipped gracefully.

## Installation

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Defaults live at the top of `backend/config.py`. Override via environment
variables (copy `backend/.env.example` to `backend/.env`):

```
IMOUSE_HOST=127.0.0.1
IMOUSE_PORT=9911
ANTHROPIC_API_KEY=sk-ant-...          # only for AI features
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
```

Models available (in/out $ per Mtok): Haiku 4.5 `$1/$5`, Sonnet 4.6 `$3/$15`,
Opus 4.8 `$5/$25`. A 608×1080 screenshot is ~875 input tokens.

## Usage

- Windows: `run.bat`
- macOS/Linux: `./run.sh`

Then open <http://localhost:8000>. Use `GET /api/debug/connection` to confirm the
console is reachable and see discovered devices.

## API endpoints (this server)

- `GET  /api/devices` — online/known devices (with `imgw`/`imgh`)
- `GET  /api/devices/{id}/screenshot` — base64 JPEG
- `GET  /api/devices/{id}/analyze` — Claude + OCR video context
- `POST /api/devices/{id}/swipe/next` · `/swipe/previous`
- `POST /api/devices/{id}/like` · `/save` · `/follow`
- `POST /api/devices/{id}/comment` — type/post a comment (AI optional)
- `POST /api/devices/{id}/generate-comments` — draft options
- `POST /api/devices/{id}/ai/step` — one Claude perceive→decide→act cycle
- `POST /api/devices/{id}/ai/run` — autonomous agent loop (`goal`, `max_steps`)
- `POST /api/devices/{id}/type` — type text from the PC into the focused field
- `POST /api/devices/{id}/key` — send a key/hotkey (`fn_key`: `ENTER`, `BACKSPACE`, …)
- `POST /api/campaign/start` — start the niche campaign (`device_id`, `duration_minutes`)
- `POST /api/campaign/stop` · `GET /api/campaign/status` — control/monitor it
- `POST /api/batch/action` — one action across many devices
- `POST /api/devices/{id}/auto-browse` — randomised engagement session
- `GET  /api/debug/connection` · `POST /api/debug/raw-command`

## Architecture

- `backend/api_client.py` — verified iMouse XP HTTP API (`IMouseClient`).
- `backend/device_manager.py` — `/device/get` discovery + the `imgw/imgh`
  coordinate mapping (`Device`, `DeviceRegistry`).
- `backend/ai_comments.py` — Claude vision analysis and comment generation.
- `backend/tiktok_actions.py` — high-level TikTok flows (`TikTokController`).
- `backend/ai_agent.py` — Claude perceive→decide→act loop (`TikTokAgent`): OCR
  grounds tappable text, Claude picks one action via tool-use, backend executes.
  Also the niche **campaign**: per video Claude classifies relevance + writes a
  fresh comment; the loop skips irrelevant videos fast, watches relevant ones
  for randomised human-like times, and randomly likes/saves/comments.
- `backend/niche.py` — the **NutriSkin** profile (description, relevant topics,
  comment style/examples). Edit this one file to retarget the campaign.
- `backend/ui_map.py` + `ui_map.json` — the **UI map** ("learning doc"): per
  screen, the real pixel tap points of each control on this phone, built
  interactively via the Interface Explorer (`/analyze-ui`, `/ui-map`).
- `backend/main.py` — FastAPI server + screenshot streaming to the browser.
- `frontend/` — HTML/JS control panel.

`tiktok-controller.html` at the repo root is a standalone single-file prototype
kept for reference; the maintained app is the `backend/` + `frontend/` pair.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No devices | console not running, phone not mirrored, or on another subnet |
| Screenshot fails, control works | AirPlay mirroring dropped — re-cast from the phone |
| Taps land in the wrong place | wrong coordinate space, or mouse params set to the `<=14.2` variant |
| OCR / find-text returns code 28 | the PaddleOCR console window was closed |
| AI features rejected (400) | `ANTHROPIC_API_KEY` not set |

## License

MIT

```

## `run.sh`

```bash
#!/bin/bash

# TikTok Controller Startup Script

cd "$(dirname "$0")/backend"

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt -q

# iMouse XP console (localhost, verified port 9911 -- do NOT use 9912)
export IMOUSE_HOST="${IMOUSE_HOST:-127.0.0.1}"
export IMOUSE_PORT="${IMOUSE_PORT:-9911}"

# Start the server
echo "Starting TikTok Controller..."
echo "Open http://localhost:8000 in your browser"
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

```
