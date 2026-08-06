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
        "Write as a real person leaving a comment that opens with a bit of your "
        "OWN STRUGGLE with skin/acne -- a specific, personal detail (years of "
        "hormonal breakouts, cystic acne that wouldn't quit, trying every "
        "topical and giving up, skin flaring after dairy, crying-in-the-mirror "
        "kind of honesty) -- and then, only after that, naturally mention that "
        "NutriSkin was what finally helped, tied to a real feature you'd bring up "
        "in passing (scanning food for its Skin Score, the gut-health recipes, "
        "the daily selfie tracker showing the before/after). It should read as a "
        "relatable confession that happens to recommend the app, NEVER as an ad. "
        "Vary the struggle, the angle, the wording, the length, and which feature "
        "you mention every single time -- no two comments should share a phrase "
        "or structure. 1-2 short sentences, lowercase-casual is fine, at most one "
        "emoji, and only sometimes. No links, no @mentions, no hashtags, no "
        "'check out'/'try'/'link in bio' ad phrasing. Tie it to what THIS "
        "specific video is about so it clearly belongs under that post."
    ),

    # Tone examples ONLY -- the model must not copy these, just match the vibe.
    "comment_examples": [
        "omg same, I dealt with breakouts for years until I fixed my gut with "
        "NutriSkin, scanning my food for the skin score was such an eye opener 🙌",
        "this is so real, NutriSkin showed me how much dairy and sugar were "
        "wrecking my skin and switching to their recipes cleared it up",
        "honestly the daily selfie tracker on NutriSkin is what kept me "
        "consistent, seeing the before and after made it click that it's all gut health",
    ],
}
