"""Processing workflow settings shared by PyQt-compatible backend and React."""

from __future__ import annotations

import json
import os
from pathlib import Path

from nicheflow_studio.core.paths import processed_dir
from nicheflow_studio.core.ui_prefs import get_ui_pref, set_ui_pref
from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.draft_revisions import DraftRevisionError

CAPTION_STYLES = [
    {"value": "contextual_info", "label": "(Meme) Context / info"},
    {"value": "meme_friend_group", "label": "(Meme) Friend Group"},
    {"value": "meme_bro_main_character", "label": "(Meme) Bro / Main Character"},
    {"value": "meme_chronically_online", "label": "(Meme) Chronically Online"},
    {"value": "meme_reaction_situation", "label": "(Meme) Reaction / Situation"},
    {"value": "meme_daily_cope", "label": "(Meme) Daily Cope"},
    {"value": "cinema_hook", "label": "(Movie) Cinema Atmospheric"},
    {"value": "history_lost_archive", "label": "(History) Past Moments"},
    {"value": "historytrails_archive", "label": "(History) HistoryTrails"},
]
TITLE_STYLES = [
    {"value": "", "label": "Auto (match caption style)"},
    {"value": "curiosity_open_loop", "label": "(History) Cinematic Record"},
    {"value": "historytrails_record", "label": "(History) HistoryTrails"},
    {"value": "meme_setup_punchline", "label": "(Meme) Setup -> Punchline"},
    {"value": "meme_relatable", "label": "(Meme) Relatable Hook"},
    {"value": "meme_friend_group", "label": "(Meme) Friend Group"},
    {"value": "meme_bro_main_character", "label": "(Meme) Bro / Main Character"},
    {"value": "meme_chronically_online", "label": "(Meme) Chronically Online"},
    {"value": "meme_reaction_situation", "label": "(Meme) Reaction / Situation"},
    {"value": "meme_daily_cope", "label": "(Meme) Daily Cope"},
    {"value": "cinema_hook", "label": "(Movie) Cinema Atmospheric"},
    {"value": "cinema_bold_keywords", "label": "(Movie) Cinema Bold Keywords"},
    {"value": "history_lost_archive", "label": "(History) Past Moments"},
]
TITLE_LENGTHS = [
    {"value": "short", "label": "Short (5-9 words)"},
    {"value": "medium", "label": "Medium (10-16 words)"},
    {"value": "long", "label": "Long (15-28 words)"},
    {"value": "auto", "label": "Auto mix"},
]
TEMPLATES = [
    {"value": "gaming_meme_black", "label": "Gaming Meme Black"},
    {"value": "reaction_clip_black", "label": "Reaction Clip Black"},
    {"value": "story_reel_clean", "label": "Story Reel Clean"},
    {"value": "lost_archive_black", "label": "Past Moments Black"},
    {"value": "historytrails_left", "label": "(History) HistoryTrails Left"},
    {"value": "cinematic_study", "label": "Cinematic Study"},
    {"value": "cinema_viral_bold", "label": "Cinema Viral Bold"},
    {"value": "cinema_normal", "label": "Cinema Normal"},
    {"value": "cinema_bold_keywords", "label": "Cinema Bold Keywords"},
    {"value": "full_video_overlay", "label": "Full Video Overlay"},
]
TEMPLATE_RENDER_CONFIG = {
    "gaming_meme_black": {
        "layout": "top_band",
        "font_size": 64,
        "font_name": "arial_bold",
        "color": "#FFFFFF",
    },
    "reaction_clip_black": {
        "layout": "top_band",
        "font_size": 60,
        "font_name": "arial_bold",
        "color": "#FFFFFF",
    },
    "story_reel_clean": {
        "layout": "top_band",
        "font_size": 56,
        "font_name": "arial_bold",
        "color": "#FFFFFF",
    },
    "lost_archive_black": {
        "layout": "top_band",
        "font_size": 46,
        "font_name": "past_moments_arial_bold",
        "color": "#FFFFFF",
    },
    "historytrails_left": {
        "layout": "top_band",
        "font_size": 54,
        "font_name": "arial",
        "color": "#FFFFFF",
        "align": "left",
        "line_gap_scale": 0.20,
    },
    "cinematic_study": {
        "layout": "top_band",
        "font_size": 58,
        "font_name": "comic_italic",
        "color": "#F7F3EA",
    },
    "cinema_viral_bold": {
        "layout": "top_band",
        "font_size": 56,
        "font_name": "arial_rounded_bold",
        "color": "#FFFFFF",
    },
    "cinema_normal": {
        "layout": "top_band",
        "font_size": 54,
        "font_name": "georgia",
        "color": "#F2E8D0",
    },
    "cinema_bold_keywords": {
        "layout": "top_band",
        "font_size": 54,
        "font_name": "georgia",
        "color": "#F2E8D0",
        "bold_keywords": True,
    },
    "full_video_overlay": {
        "layout": "overlay",
        "font_size": 50,
        "font_name": "arial_bold",
        "color": "#F8FAFC",
        "background": "dark",
    },
}
_PREMISE_KEY_PREFIX = "processing_clip_premise_"


def _account_preferences(account: Account | None) -> dict:
    if account is None or not account.processing_preferences:
        return {}
    try:
        value = json.loads(account.processing_preferences)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def get_settings(item_id: int) -> dict:
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise DraftRevisionError(f"No download item with id {item_id}.")
        account = session.get(Account, item.account_id) if item.account_id else None
        prefs = _account_preferences(account)
        default_caption = (
            "history_lost_archive" if account and account.niche == "history" else "contextual_info"
        )
        default_template = (
            "lost_archive_black" if account and account.niche == "history" else "gaming_meme_black"
        )
        return {
            "clip_premise": get_ui_pref(f"{_PREMISE_KEY_PREFIX}{item.id}", ""),
            "caption_style": prefs.get("caption_style")
            or item.caption_style_preset
            or default_caption,
            "title_style": prefs.get("prompt_title_style") or "",
            "title_length": prefs.get("title_length") or "long",
            "template": prefs.get("template") or default_template,
            "title_draft": item.title_draft or "",
            "caption_draft": item.caption_draft or "",
            "caption_style_options": CAPTION_STYLES,
            "title_style_options": TITLE_STYLES,
            "title_length_options": TITLE_LENGTHS,
            "template_options": TEMPLATES,
        }


def save_settings(item_id: int, payload: dict) -> dict:
    premise = str(payload.get("clip_premise") or "").strip()
    caption_style = str(payload.get("caption_style") or "contextual_info")
    title_style = str(payload.get("title_style") or "")
    title_length = str(payload.get("title_length") or "long")
    template = str(payload.get("template") or "gaming_meme_black")
    set_ui_pref(f"{_PREMISE_KEY_PREFIX}{item_id}", premise)
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise DraftRevisionError(f"No download item with id {item_id}.")
        item.caption_style_preset = caption_style
        account = session.get(Account, item.account_id) if item.account_id else None
        if account is not None:
            prefs = _account_preferences(account)
            prefs.update(
                {
                    "caption_style": caption_style,
                    "prompt_title_style": title_style,
                    "title_length": title_length,
                    "template": template,
                }
            )
            account.processing_preferences = json.dumps(prefs)
        session.commit()
    return get_settings(item_id)


def save_final_draft(item_id: int, title: str, caption: str) -> dict:
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise DraftRevisionError(f"No download item with id {item_id}.")
        item.title_draft = title.strip() or None
        item.caption_draft = caption.strip() or None
        session.commit()
    return {"title_draft": title.strip(), "caption_draft": caption.strip()}


def render_config(item_id: int) -> dict:
    template = get_settings(item_id)["template"]
    return TEMPLATE_RENDER_CONFIG.get(template, TEMPLATE_RENDER_CONFIG["gaming_meme_black"])


def open_folder(item_id: int) -> dict:
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise DraftRevisionError(f"No download item with id {item_id}.")
        path = Path(item.processed_path or item.file_path or processed_dir())
    folder = path if path.is_dir() else path.parent
    if not folder.exists():
        folder = processed_dir()
    os.startfile(str(folder))
    return {"folder": str(folder)}
