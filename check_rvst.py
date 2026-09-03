#!/usr/bin/env python3
"""
RVST social media monitor.
Runs on a GitHub Actions schedule (see .github/workflows/check.yml) — no
browser tab needed, works even if every device you own is offline.

What it does each run:
  1. Pulls the latest TikTok + YouTube videos via the Apify API.
  2. Compares them against state.json (committed back to the repo each run)
     to find videos it hasn't seen before.
  3. Emails you instantly for each new video, with an estimated view
     potential based on your historical hashtag performance.
  4. Once per calendar day: sends a stats digest.
  5. If 1+ day has passed with no new post: sends a content-idea nudge,
     once per day.

Required repo secrets (Settings -> Secrets and variables -> Actions):
  APIFY_TOKEN        Your Apify API token (console.apify.com/account/integrations)
  GMAIL_ADDRESS       The Gmail address to SEND from (needs an app password)
  GMAIL_APP_PASSWORD  A Gmail app password (myaccount.google.com/apppasswords)
  NOTIFY_EMAIL         Where to send notifications, e.g. rvstteam@gmail.com
"""

import json
import os
import re
import smtplib
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo
from itertools import cycle

STATE_FILE = "state.json"
STATS_FILE = "stats.json"
AMSTERDAM = ZoneInfo("Europe/Amsterdam")

# Post-reminder times (your local time, Netherlands). Automatically stays
# correct through summer/winter time changes since we compare using
# Europe/Amsterdam, not fixed UTC hours.
REMINDER_HOURS_LOCAL = [9, 12, 14, 16, 18, 20, 22]

TIKTOK_USERNAME = "rvst_officieel"
YOUTUBE_URL = "https://www.youtube.com/@RVST-officieel"

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "rvstteam@gmail.com")

# Historical hashtag performance, computed once from your first 120 TikTok
# videos. Used to estimate view potential for new posts. Static on purpose —
# update by hand occasionally if you want it to reflect newer trends.
HASHTAG_STATS = [
    {"tag": "1v1", "n": 6, "avg": 7699},
    {"tag": "rvst", "n": 17, "avg": 3206},
    {"tag": "rvstofficial", "n": 16, "avg": 3075},
    {"tag": "youtube", "n": 70, "avg": 2854},
    {"tag": "viral", "n": 114, "avg": 2383},
    {"tag": "fyp", "n": 118, "avg": 2348},
    {"tag": "trending", "n": 31, "avg": 2241},
    {"tag": "football", "n": 80, "avg": 1952},
    {"tag": "challenge", "n": 30, "avg": 1927},
    {"tag": "penaltykingrvst", "n": 4, "avg": 1463},
]
CHANNEL_AVG_VIEWS = 2354

CONTENT_IDEAS = [
    "Nieuw 1-op-1 duel filmen met #1v1 — historisch je best presterende tag en format.",
    "Shoutout of samenwerking met een lokale voetbalacademie of speler, zoals bij je Sint Jago Academy-video.",
    "\"Real life vs Droom\"-concept herhalen — dit format trok bij jou 6.600+ views.",
    "Een korte quiz/uitdaging (\"Weet jij het antwoord?\") voor extra comments en interactie.",
    "Een oudere TikTok-hit hergebruiken als YouTube Short om je kleinere kanaal te voeden.",
    "Reageer op een trending voetbalmoment van vandaag met je eigen versie/mening.",
    "Film een \"before and after\" van een skill die je aan het oefenen bent.",
    "Doe een korte penalty- of trickshot-challenge en daag je volgers uit hem na te doen.",
    "Laat een blooper of mislukte poging zien — dat voelt persoonlijker en scoort vaak goed op comments.",
    "Stel een vraag aan je volgers in de caption in plaats van een statement — dat verhoogt comments.",
]


def pick_ideas(n=3, seed=None):
    """Rotates through the idea pool so consecutive reminders don't repeat."""
    if seed is None:
        seed = datetime.now(AMSTERDAM).toordinal() * 24 + datetime.now(AMSTERDAM).hour
    pool = CONTENT_IDEAS
    start = seed % len(pool)
    rotated = pool[start:] + pool[:start]
    return rotated[:n]


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "known_urls": [],
        "last_post_date": None,
        "last_digest_date": None,
        "last_inactivity_alert_date": None,
        "seeded": False,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def apify_run_sync(actor_slug, run_input, retries=2):
    """Runs an Apify actor synchronously and returns its dataset items.
    Retries once on transient failure before giving up."""
    url = (
        f"https://api.apify.com/v2/acts/{actor_slug}/run-sync-get-dataset-items"
        f"?token={APIFY_TOKEN}"
    )
    data = json.dumps(run_input).encode("utf-8")
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            log(f"Apify call attempt {attempt}/{retries} failed for {actor_slug}: {e}")
    raise last_err


def fetch_tiktok():
    items = apify_run_sync(
        "clockworks~tiktok-profile-scraper",
        {
            "profiles": [TIKTOK_USERNAME],
            "profileScrapeSections": ["videos"],
            "profileSorting": "latest",
            "resultsPerPage": 10,
        },
    )
    videos = []
    followers = hearts = total_videos = None
    for it in items:
        meta = it.get("authorMeta", {})
        if followers is None and meta:
            followers = meta.get("fans")
            hearts = meta.get("heart")
            total_videos = meta.get("video")
        videos.append(
            {
                "platform": "tiktok",
                "url": it.get("webVideoUrl"),
                "title": (it.get("text") or "")[:120],
                "views": it.get("playCount", 0),
                "hashtags": re.findall(r"#(\w+)", it.get("text") or ""),
                "date": it.get("createTimeISO", "")[:10],
            }
        )
    return videos, {"followers": followers, "hearts": hearts, "videos": total_videos}


def fetch_youtube():
    items = apify_run_sync(
        "streamers~youtube-channel-scraper",
        {"startUrls": [{"url": YOUTUBE_URL}], "maxResults": 5, "sortVideosBy": "NEWEST"},
    )
    videos = []
    subs = total_views = total_videos = None
    for it in items:
        if subs is None:
            subs = it.get("numberOfSubscribers")
            total_views = it.get("channelTotalViews")
            total_videos = it.get("channelTotalVideos")
        videos.append(
            {
                "platform": "youtube",
                "url": it.get("url"),
                "title": (it.get("title") or "")[:120],
                "views": it.get("viewCount", 0),
                "hashtags": [],
                "date": it.get("date", ""),
            }
        )
    return videos, {"subscribers": subs, "totalViews": total_views, "videos": total_videos}


def estimate_view_potential(hashtags):
    tags = {t.lower() for t in (hashtags or [])}
    matches = [s for s in HASHTAG_STATS if s["tag"] in tags]
    base = (sum(s["avg"] for s in matches) / len(matches)) if matches else CHANNEL_AVG_VIEWS
    return round(base * 0.6), round(base * 1.6)


def send_email(subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        log("Gmail credentials missing, skipping email: " + subject)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = NOTIFY_EMAIL
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [NOTIFY_EMAIL], msg.as_string())
        log("Email sent: " + subject)
    except Exception as e:
        log(f"Email FAILED ({subject}): {e}")


def fmt(n):
    return f"{round(n or 0):,}".replace(",", ".")


def merge_with_last_known(fresh, last_known):
    """Fills in any missing/None field from the last known good value, so a
    single flaky Apify response never makes a stat look like it dropped to 0.
    Only overwrites last_known for fields that actually came back this run."""
    last_known = dict(last_known or {})
    merged = dict(last_known)
    for k, v in (fresh or {}).items():
        if v is not None:
            merged[k] = v
    return merged


def write_stats_file(tt_stats, yt_stats):
    """Writes a small, plain JSON file with the current best-known stats.
    This is what the dashboard HTML reads (via GitHub Pages) so its numbers
    stay in sync with what the emails report — same source of truth."""
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "tiktok": {
            "followers": tt_stats.get("followers"),
            "hearts": tt_stats.get("hearts"),
            "videos": tt_stats.get("videos"),
        },
        "youtube": {
            "subscribers": yt_stats.get("subscribers"),
            "totalViews": yt_stats.get("totalViews"),
            "videos": yt_stats.get("videos"),
        },
    }
    with open(STATS_FILE, "w") as f:
        json.dump(payload, f, indent=2)


def maybe_send_post_reminders(state):
    """Sends up to 7 post reminders/day at fixed local times, each with
    fresh content ideas + estimated reach. Safe to call every run — it only
    actually sends once per hour-slot per day, tracked in state."""
    now_local = datetime.now(AMSTERDAM)
    if now_local.hour not in REMINDER_HOURS_LOCAL:
        return

    today_local = now_local.date().isoformat()
    slot_id = f"{today_local}_{now_local.hour:02d}"
    sent_slots = state.setdefault("sent_reminder_slots", [])
    if slot_id in sent_slots:
        return  # already sent this slot today

    ideas = pick_ideas(3, seed=now_local.toordinal() * 24 + now_local.hour)
    low, high = estimate_view_potential([HASHTAG_STATS[0]["tag"]])
    top_tags = ", ".join(f"{s['tag']} (gem. {fmt(s['avg'])} views)" for s in HASHTAG_STATS[:3])

    subject = f"⏰ Tijd om te posten! ({now_local.strftime('%H:%M')})"
    body = (
        f"Reminder: dit is een goed moment om iets te posten.\n\n"
        f"Verwacht bereik als je nu post met je best presterende hashtags: "
        f"{fmt(low)}\u2013{fmt(high)} views.\n"
        f"Je sterkste hashtags tot nu toe: {top_tags}.\n\n"
        "Video-ideeën voor nu:\n"
        + "\n".join(f"- {idea}" for idea in ideas)
        + "\n\n— RVST Monitor (automatisch, draait op GitHub Actions)"
    )
    send_email(subject, body)

    sent_slots.append(slot_id)
    # keep the list from growing forever — only keep the last 14 days
    state["sent_reminder_slots"] = sent_slots[-(14 * len(REMINDER_HOURS_LOCAL)):]


def main():
    if not APIFY_TOKEN:
        log("APIFY_TOKEN missing — aborting.")
        sys.exit(1)

    state = load_state()
    today = datetime.now(timezone.utc).date().isoformat()

    try:
        tt_videos, tt_stats_fresh = fetch_tiktok()
    except urllib.error.URLError as e:
        log(f"TikTok fetch failed: {e}")
        tt_videos, tt_stats_fresh = [], {}

    try:
        yt_videos, yt_stats_fresh = fetch_youtube()
    except urllib.error.URLError as e:
        log(f"YouTube fetch failed: {e}")
        yt_videos, yt_stats_fresh = [], {}

    # Never let a flaky/partial Apify response make a stat look like it
    # dropped to 0 — fill any missing field from the last known good value.
    last_known = state.get("last_known_stats", {})
    tt_stats = merge_with_last_known(tt_stats_fresh, last_known.get("tiktok"))
    yt_stats = merge_with_last_known(yt_stats_fresh, last_known.get("youtube"))
    state["last_known_stats"] = {"tiktok": tt_stats, "youtube": yt_stats}
    write_stats_file(tt_stats, yt_stats)

    all_videos = tt_videos + yt_videos

    # First-ever run: seed known_urls with what we see now so we don't
    # treat your entire existing history as "new" and spam your inbox.
    if not state.get("seeded"):
        state["known_urls"] = [v["url"] for v in all_videos if v["url"]]
        state["seeded"] = True
        state["last_post_date"] = max((v["date"] for v in all_videos if v["date"]), default=today)
        save_state(state)
        log("First run: seeded known videos, no notifications sent this run.")
        return

    known = set(state.get("known_urls", []))
    fresh = [v for v in all_videos if v["url"] and v["url"] not in known]

    for v in fresh:
        low, high = estimate_view_potential(v["hashtags"])
        subject = f"🎥 Nieuwe {v['platform']} video: {v['title']}"
        body = (
            f"Er staat een nieuwe video live!\n\n"
            f"Titel: {v['title']}\n"
            f"Platform: {v['platform']}\n"
            f"Link: {v['url']}\n"
            f"Huidige views: {fmt(v['views'])}\n"
            f"Verwacht view-potentieel: {fmt(low)}\u2013{fmt(high)} views "
            f"(gebaseerd op hoe vergelijkbare hashtags historisch presteerden)\n\n"
            f"— RVST Monitor (automatisch, draait op GitHub Actions)"
        )
        send_email(subject, body)
        known.add(v["url"])

    if fresh:
        state["known_urls"] = list(known)
        state["last_post_date"] = today

    # Daily digest — once per calendar day.
    if state.get("last_digest_date") != today:
        body = (
            "Dagelijkse samenvatting van je TikTok en YouTube.\n\n"
            "TikTok\n"
            f"- Volgers: {fmt(tt_stats.get('followers'))}\n"
            f"- Hearts totaal: {fmt(tt_stats.get('hearts'))}\n"
            f"- Video's totaal: {fmt(tt_stats.get('videos'))}\n\n"
            "YouTube\n"
            f"- Abonnees: {fmt(yt_stats.get('subscribers'))}\n"
            f"- Views totaal: {fmt(yt_stats.get('totalViews'))}\n"
            f"- Video's totaal: {fmt(yt_stats.get('videos'))}\n\n"
            f"Nieuwe video's vandaag: {len(fresh)}\n\n"
            "— RVST Monitor (automatisch, draait op GitHub Actions)"
        )
        send_email(f"📊 RVST dagelijkse samenvatting — {today}", body)
        state["last_digest_date"] = today

    # Inactivity alert — once per day, only if 1+ day since last known post.
    last_post = state.get("last_post_date") or today
    try:
        days_since = (datetime.now(timezone.utc).date() - datetime.fromisoformat(last_post).date()).days
    except ValueError:
        days_since = 0

    if days_since >= 1 and state.get("last_inactivity_alert_date") != today:
        low, high = estimate_view_potential([HASHTAG_STATS[0]["tag"]])
        top_tags = ", ".join(f"{s['tag']} (gem. {fmt(s['avg'])} views)" for s in HASHTAG_STATS[:3])
        body = (
            f"Het is {days_since} dag(en) geleden sinds je laatste gedetecteerde post ({last_post}).\n\n"
            f"Verwacht bereik als je nu post met je best presterende hashtags: {fmt(low)}\u2013{fmt(high)} views.\n"
            f"Je sterkste hashtags tot nu toe: {top_tags}.\n\n"
            "Content-ideeën om nu te posten:\n"
            + "\n".join(f"- {idea}" for idea in CONTENT_IDEAS)
            + "\n\n— RVST Monitor (automatisch, draait op GitHub Actions)"
        )
        send_email(f"⚠️ Geen nieuwe post in {days_since} dag(en) — RVST", body)
        state["last_inactivity_alert_date"] = today

    maybe_send_post_reminders(state)

    save_state(state)
    log(f"Done. {len(fresh)} new video(s) found.")


if __name__ == "__main__":
    main()
