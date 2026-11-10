# Mac / iOS-Farm Port — Answers to the Follow-up Questions

> Answers to the follow-up on `CONTEXT_BUNDLE.md`, grounded in the actual code.
> **Honesty note up front, because you're making architecture bets on this:**
> several of your questions ask "did you ever test X / measure Y / run on a second
> screen?" — and for most the truthful answer is **no**. This project built a
> capable *operator*, validated by hand and in short dev campaigns on **one**
> phone (608×1080 mirror). It was never run at fleet scale, never cost-measured,
> never A/B'd against account outcomes, and never ran on a second screen size.
> Where I'm estimating, I label it an estimate. Where it was never tested, I say so
> rather than inventing a result.

---

## §1 — Rail anchoring on a new coordinate space ⭐

**The key realization: `_ocr_rail_points` is mostly self-calibrating, so far less
is hard-coded to 608×1080 than it looks.** Walk the function and classify every
constant:

| Constant | Nature | Ports to 375×667? |
|---|---|---|
| band `cx > 0.85w`, `0.12h < cy < 0.90h` | **fraction** | ✅ directly |
| `railx` fallback `0.917·w` | **fraction of width** | ✅ directly (and only a fallback) |
| `spacing` via **median of live count gaps** | **measured per-frame** | ✅ auto-adapts |
| `spacing` fallback `107` | **absolute px @1080** | ❌ must become `≈0.099·h` |
| plausibility band `90 ≤ med ≤ 125` | **absolute px @1080** | ❌ must become `≈0.083·h … 0.116·h` |
| `offset = spacing · 0.42` | **fraction of spacing** | ✅ auto-scales |
| `rail_icon_offset_px = 48` | **defined but NEVER used** (grep-confirmed — dead config) | ignore entirely |
| snap tol `spacing · 0.4`, icon bounds `0.10h…0.92h` | fraction / scales | ✅ directly |

So the **only** genuinely 608×1080-bound numbers are the `spacing` *fallback*
(107) and its plausibility band (90–125). Your naive height-scale — spacing ≈ 66,
band ≈ 56–77 at 667 — is the **correct transform for those two**, and the cleanest
fix is to express them as height fractions (`0.099·h`, `0.083·h`, `0.116·h`) so
they're resolution-free.

**But here's why that barely matters in practice:** `spacing` and `railx` are
*measured from the live OCR counts on each frame* whenever ≥2 counts are visible —
which is the normal case, since like/comment/share almost always show counts. In
that common path the hard-coded fallbacks **never fire**, and the algorithm adapts
to any resolution on its own. The fallbacks only bite when counts are sparse
(hidden counts, OCR misses). So: convert those two constants to height fractions
for safety, but don't over-invest — measurement carries the common case.

**Were the constants tuned to pure height, or to icon size / DPI / layout
breakpoints?** Honest answer: I can only see *what* they are, not the tuning
history. From the code they're vertical gaps between OCR text centres in the
**mirror image** (a scaled screen capture), i.e. a pure vertical-layout measure.
That scales with height *in the mirror pixel space*. **Caveat you should respect:**
WDA/iOS-Farm coordinates are in **points** (375×667), and TikTok lays the rail out
in points with roughly **fixed-size icons and fixed gaps** — so the real point
spacing is approximately *constant across iPhone models*, not strictly proportional
to screen height. Net: height-fraction is a fine first guess for the fallback, but
**re-measure on the SE and trust the live-measured `spacing` over any constant.**

**`railx = 0.917·w`:** it's already a width fraction, so it ports; on 375 that's
x≈344, plausibly in the rail zone. It's also only the fallback — real `railx` is
the mean x of live counts. Low risk.

**Has it ever run on a second screen size?** **No.** Only 608×1080. Everything
above is theory until you validate on the SE. **Use the `/api/debug/rail`
endpoint** (it dumps the right-edge elements + the computed icon points + which
source would be used) as your validation harness: point it at the SE on a normal
feed video and check the computed points land on the icons *before* anything taps.
That endpoint exists precisely for this.

---

## §2 — Do we still need OCR at all on iOS? ⭐ (the one that reshapes the port)

**Your hypothesis is correct: we went vision/OCR-first because iMouse gave us no
element tree — not because we evaluated the accessibility tree and rejected it.**
iMouse exposes only OCR, find-text, and find-image. There was no `/source`
equivalent, so querying TikTok's elements was never an option and **never tested**.

So the honest, decisive answer: **I don't know whether TikTok's iOS build exposes
the rail icons as queryable elements — and the experiment is cheap and should be
your very first one.** Dump WDA `/source` on a playing For You video and look for
the right-rail buttons with usable `type`/`name`/`label`/`rect`. That single result
determines how much of the OCR layer you keep.

Three things to weigh when you run it:

1. **Localisation is exactly why the OCR approach is robust — don't throw that
   away.** `_ocr_rail_points` anchors to the **count numbers** (digits — universal)
   and the follow **"+"**, *not* to localised words. (The word-label branch —
   Teilen/Speichern/share/senden/… — is only a secondary disambiguator, and yes
   it's German+English only.) iOS `accessibilityLabel` values are typically
   **localised** ("Like" / "Gefällt mir"), so targeting elements **by label**
   reintroduces the exact French-works/German-breaks fragility the number-anchoring
   avoids. **Target by structure/type/position** (the vertical stack of buttons on
   the right edge, Nth button), never by localised label, and you keep both the
   robustness *and* the free pixel-exact rects.

2. **TikTok may expose the rail poorly or deliberately.** Many video apps render
   the player surface as one opaque accessibility element and/or label controls
   generically; some obfuscate on purpose. If `/source` gives you stable rects:
   great, elements become primary. If it gives you a mush of unlabelled generic
   nodes: rail anchoring stays the workhorse. You won't know which until you look.

3. **Your proposed ordering — elements first, rail anchoring as fallback — is
   right**, with the §2.1 caveat (match by structure, not localised label). It also
   composes cleanly with the existing `locate_actions` fallback chain: slot an
   element-query step *above* the OCR step and leave `_ocr_rail_points` and the
   vision fallback underneath. If elements work, OCR becomes a fallback-of-a-
   fallback and much of §1 stops mattering (you get rects directly, no geometry,
   no scaling constants).

**Bottom line for the port's shape:** run the `/source` experiment before you port
anything taps-related. It's the highest-leverage hour in the whole project.

---

## §3 — What survives the move off AirPlay

With both channels over USB (iOS Farm), the AirPlay-specific machinery goes, but
be precise about *why* each guard existed:

- **`settled_perception` (two agreeing captures 0.45 s apart):** it guarded two
  different things that your fast WDA screenshots only half-fix.
  1. *Mirror lag* (1–2 s) — **gone** with sub-200 ms WDA captures.
  2. *TikTok's own transition* — after a swipe, the app still animates and loads
     the next video; a capture mid-animation is stale regardless of transport.
     **Not** gone.
  Recommendation: **don't delete it blind — demote it.** Drop the double-capture in
  steady-state perception, but **keep the post-advance verification** (the "did the
  caption actually change?" check inside `_advance_feed`). That one guards against
  TikTok ignoring a swipe, which is app behaviour, not transport. It's cheap and
  still earns its keep.
- **`pre_tap_settle` / `post_swipe_settle`:** these were specifically for the
  **relative-HID pointer** (travel time + AssistiveTouch wake). WDA injects
  absolute coordinates — no pointer. **Set `pre_tap_settle = 0`.** Keep a small
  `post_swipe_settle` (~0.25–0.3 s) but re-label its rationale: it's now "let the
  swipe animation finish," not "let the pointer rest." Nothing downstream depends on
  these semantically; re-tune empirically.
- **`recover_mirror` + Control Center dance — safe to delete, but mind the
  couplings:**
  - **`locate_one` becomes dead code** — grep-confirmed it's used *only* by mirror
    calibration (`main.py` 533/547). Delete it with the mirror code.
  - The mirror endpoints (`/api/mirror/*`), the `control_center` UI-map screen, and
    the frontend mirror panel all go.
  - **The real coupling to fix, not just delete: the error branch in
    `_campaign_video`.** Today a screenshot error assumes `error_code == 25` = mirror
    dropped → `recover_mirror()` → circuit-break. On iOS Farm a capture failure means
    **the WDA session died / the farm lost the device**, which needs a *different*
    recovery (re-dial the farm, restart the WDA session), not a Control Center
    re-cast. Rewrite that branch for the farm's failure modes; keep the circuit
    breaker (`error_streak >= 6` stop) — that logic is sound and transport-agnostic.
  - `screenshot_works()` (the "is vision alive?" probe) is still useful as a health
    check — just point its failure handling at the farm, not AirPlay.

---

## §4 — Multi-device

**Was the singleton deliberate?** No — it was "we only ever had one phone."
Multi-phone parallelism was explicitly deferred during development ("not now — UI
first"). It's a deferral, not a designed constraint.

**One `TikTokAgent` per device is the right shape.** Most per-run state already
lives in `self._campaign` (per instance): `engaged_posts`, the comment cooldown
(`videos_since_comment`, `comment_gap`), `used_comments`, `stats`. Spin up one agent
per device and those isolate automatically. **But two pieces are genuine global
state that will bite you:**

1. **`NICHE` is a module-level global mutated in place** (grep-confirmed):
   `set_active_niche()` does `NICHE.clear(); NICHE.update(...)`, and `main.py` also
   does `NICHE["language"] = ...` directly. Every `plan_video` / comment prompt in
   `ai_agent.py` reads this one global. **Two devices running different niches (or
   languages) simultaneously will clobber each other mid-run.** This must become
   per-agent/per-campaign state (pass the niche into the agent, don't read a
   module global) before any parallel multi-niche run. This is the #1 refactor for
   fleet mode.
2. **`ui_map.json` is a single shared file** (`ui_map.py` → one `_PATH`). Fine for
   several *identical* phones, **wrong across screen sizes** — the pixel points
   differ per resolution. Key the map per resolution (or per device model), not one
   global file.

**Comment cooldown per-run vs per-account:** you're right. Today
`comment_min_gap` / `comment_max_per_session` are **per campaign run**, which was
implicitly per-account because one run = one account = one phone. The moment you
multiplex accounts on a phone, that conflates accounts — a phone could blow the
"max 8 comments" budget on one account while starving another. Make the cooldown
and cap **per-account keyed state** (this is exactly where your fleet scheduler's
account-lifecycle discipline should own it, not the brain).

---

## §5 — Cost

**Never measured. Full stop.** There is no cost-per-hour data. Here is a *computed
estimate* with the assumptions exposed so you can plug your own numbers — treat it
as arithmetic, not a measurement:

Per video = **1 Haiku `plan_video` call**:
- input ≈ image + OCR text + niche system prompt. On SE (375×667) the image is
  `375·667/750 ≈ 333` tokens; system prompt ~700–900; OCR ~100–300 → **~1,100–1,400
  input tokens**, ~50–80 output.
- Haiku 4.5 @ `$1 / $5` per Mtok → **≈ $0.0015–0.0018 per video.**

Comments are negligible: sparse (≤8/run), Sonnet @ `$3/$15`, ~$0.05 per run total.

Videos/hour depends entirely on cycle time (watch + perceive + model latency +
swipe). With fast irrelevant skips (~2.5 s) dominating and ~4–6 s overhead per
video, realistic throughput is **~350–500 videos/hour/phone**.

> **Estimate:** cost/hour/phone ≈ `videos_per_hour × $0.0016` ≈ **$0.6–0.8/phone-hour**,
> ~entirely the per-video Haiku call.
>
> At **10 phones × 3 h/day × 30 days = 900 phone-hours** → **≈ $550–720/month.**

So, bluntly: at your scale this is **"low hundreds of € / month," much closer to
your €500 fear than €5.** The per-video classify call *is* the cost — so your
pre-filter instinct is correct and worth building. Options, honestly ranked:

1. **Creator cache** (best bang/buck): you already dedup *posts* via
   `engaged_posts`; extend it to **creators** — once a creator is classified
   irrelevant, fast-skip repeat appearances with no model call. Feeds repeat
   creators constantly.
2. **Cheap text/element pre-gate:** before the vision call, check the caption OCR /
   a11y text for *any* niche-vocabulary or skin/face cue; if there's zero overlap
   **and** no visual skin cue, fast-skip. Caveat: TikTok captions are often absent
   or misleading, so this trades some relevant-recall for cost — tune the threshold
   conservatively.
3. **Classify less often:** only spend the call after a video has held the screen
   >X s (skip the instant-swipe noise you'd skip anyway).
4. **Drop the image when text is rich:** the image is ~25–70 % of input tokens;
   text-only Haiku is cheaper — but relevance often needs the picture, so this is
   marginal and risky. I would not lead with it.

I did **not** find that "the classifier was the cheap part" — by construction it's
the *expensive* part (one call per video). Pre-filtering is genuinely worthwhile at
10-phone scale.

---

## §6 — Text entry

**The good news: WDA Unicode typing makes almost the entire §9 fight disappear.**
No ASCII limit, no transliteration, no clipboard-Shortcut bridge. That whole mess
was an *iMouse* limitation (`/key/sendkey` was ASCII-only, hence clipboard paste +
umlaut transliteration + the z/y saga).

**Set `value` vs. real keystrokes — "does Post enable?":** **Never tested** (our
clipboard bridge was down; we used paste/transliteration). This is a real
known-unknown and you should test it **early**. Risk: TikTok's comment box is a
native/RN text input, and some such inputs **don't fire their change handler when
`value` is set programmatically** via accessibility — so the Post/Send button,
which is gated on non-empty *state*, may **stay disabled** even though text is
visible. Recommendation: try `setValue` first (clean), but **keep WDA `/wda/keys`
real-keystroke typing as the fallback** precisely for the "Post stays grey" case.
Unicode keystrokes cost you nothing in correctness and sidestep the risk entirely —
I'd actually default to real keystrokes and treat `setValue` as the optimisation to
validate, not the other way round.

**`_find_comment_input` (`y > 0.82h`) on a 667-tall screen:** the height test is
**already a fraction** (`y > 0.82·h` → y>547 at 667), so it ports directly. The
fragile part isn't the fraction — it's that the function finds the bar by
**OCR-matching localised placeholder strings** (`hinzu`, `Add comment`, `Sag etwas`,
…), a German/English list. With the a11y tree you'd find the input **by type**
(the TextField/SearchField in the comment sheet) — locale-independent and far more
robust. Same theme as §2: structure beats localised text.

---

## §7 — The two judgment calls

**`ui_map.json` — calibration and the UI-change failure mode.** It's calibrated
**manually/interactively**: the Interface Explorer has Claude vision *propose* tap
points for the current screen, a human verifies/corrects them, and they're saved
per screen-type. It is **not** auto-refreshed. When TikTok ships a UI change the
failure mode splits:
- **Rail engagement survives** — `_ocr_rail_points` is self-calibrating (measures
  from live counts each frame), so like/comment/save/share keep working through
  most layout shifts without any re-measurement.
- **Anything on static `ui_map` points degrades silently** — recovery navigation,
  the comment-input fallback — and here's the key philosophical difference from your
  Android tool: **there is no "refuse on an unmeasured build" guard.** The agent
  will happily tap a stale coordinate and rely on **vision recovery + circuit
  breakers** to dig out. So it's *more forgiving* (keeps running through minor
  updates instead of blocking phones for days) but *less safe* (it can mis-tap —
  the historical like→follow / like→profile bugs are exactly this failure). Your
  measurement-gate discipline is the safer design; the right blend is: **keep the
  OCR self-calibration as primary** (so you don't block the fleet on every TikTok
  release), and **add your "measured build" gate only around the static `ui_map`
  points** that can't self-calibrate. That's genuinely where your scheduler's
  "refuse rather than guess" discipline and this brain should meet.

**Results — reach / survival / flags.** **I have no data. None.** Account outcomes
were never measured in this project — it ran in hand-testing and short dev
campaigns, not a tracked warming program. I can't tell you it cleared your bar,
and I won't pretend it did. What the code gives you is *engagement realism by
construction* — jittered watch times with long tails, deliberately sparse
commenting under a cap, per-session comment uniqueness, post/creator dedup so it
never re-likes, live-stream avoidance, language gating — all designed to *look*
human. Whether that translates into distribution on real hardware is **exactly the
open question your audit raises, and it's unproven here.** Given 9/10 of your cloud
accounts got zero distribution, I'd treat this agent as a *better-behaved operator*
to A/B against your existing approach, not as a validated methodology. If you wire
in your fleet's account-lifecycle + outcome tracking, you'd finally have the data
this project never collected — which is the most valuable thing the merge could
produce.

---

## The two-sentence version

Run the WDA `/source` experiment on a TikTok feed video **first**: if the rail is
exposed as structurally-queryable elements, target it by type/position (never
localised label) and the OCR layer — and most of §1 — becomes an optional fallback.
If it isn't, keep `_ocr_rail_points` (it's largely self-calibrating; only the
`spacing=107` fallback and its `90–125` band are 608×1080-bound → convert to
`~0.099·h` and `~0.083–0.116·h`, and validate on the SE with the `/api/debug/rail`
dump before anything taps).
