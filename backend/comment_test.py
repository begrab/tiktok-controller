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
