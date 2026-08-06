"""NutriSkin campaign profile.

EDIT THIS FILE when the official NutriSkin details arrive. It drives what the
agent treats as relevant and how it writes comments -- nothing else needs to
change. Keep the shape of the dict; just refine the text and lists.
"""

NICHE = {
    "name": "NutriSkin",

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
