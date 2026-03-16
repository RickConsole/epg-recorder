#!/usr/bin/env python3
"""
EPG Auto-Recorder for UHF Server

Polls XMLTV guide data and automatically schedules recordings on UHF Server
when shows matching configured name patterns appear in the EPG.

Auth works by reading the refresh_token from UHF's TinyDB database file,
refreshing it via Firebase REST API, and writing the new token back. No
credentials configuration needed.
"""

import hashlib
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("epg-recorder")

STATE_FILE = Path(__file__).parent / "state.json"

FIREBASE_API_KEY = None  # Need to extract this from the UHF server binary. 
FIREBASE_REFRESH_URL = (                                                                         
    f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"                        
)  

# Device ID for our sidecar's token entry
SIDECAR_DEVICE_ID = "epg-recorder-sidecar"


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        log.error("config.yaml not found")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"scheduled": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Auth (DB-based, no credentials needed) ────────────────────────────────────


def firebase_refresh(refresh_token: str) -> dict:
    """Refresh a Firebase token via REST API."""
    resp = requests.post(
        FIREBASE_REFRESH_URL,
        json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "id_token": data["id_token"],
        "refresh_token": data["refresh_token"],
        "expires_in": int(data["expires_in"]),
    }


def get_auth_token(db_path: str, state: dict) -> str:
    """
    Get a valid auth token by reading UHF's TinyDB, refreshing via Firebase,
    and writing the new token back into the DB so verify_token() finds it.
    """
    # Check if we have a cached token that's still valid
    auth = state.get("auth", {})
    if auth.get("id_token") and auth.get("expires_at", 0) > time.time() + 120:
        return auth["id_token"]

    # Read UHF's db.json to find a refresh_token
    db_file = Path(db_path)
    if not db_file.exists():
        log.error("UHF database not found at %s", db_path)
        sys.exit(1)

    with open(db_file) as f:
        db = json.load(f)

    users = db.get("firebase_users", {})
    if not users:
        log.error("No users found in UHF database — open the UHF app first to create an account")
        sys.exit(1)

    # Find a refresh_token from any user's tokens
    refresh_token = None
    user_key = None
    user_email = None
    for key, user in users.items():
        tokens = user.get("tokens", [])
        for tok in tokens:
            if tok.get("refresh_token"):
                refresh_token = tok["refresh_token"]
                user_key = key
                user_email = user.get("email", "unknown")
                break
        # Also check legacy top-level refresh_token
        if not refresh_token and user.get("refresh_token"):
            refresh_token = user["refresh_token"]
            user_key = key
            user_email = user.get("email", "unknown")
        if refresh_token:
            break

    if not refresh_token:
        log.error("No refresh_token found in UHF database")
        sys.exit(1)

    # Refresh the token via Firebase
    log.info("Refreshing Firebase token for %s ...", user_email)
    try:
        new_auth = firebase_refresh(refresh_token)
    except requests.HTTPError as e:
        log.error("Firebase token refresh failed: %s", e)
        log.error("The UHF app may need to be opened to re-authenticate")
        raise

    new_id_token = new_auth["id_token"]
    new_refresh_token = new_auth["refresh_token"]

    # Write the new token into UHF's db.json so verify_token() accepts it.
    # We add our own token entry with a unique device_id so we don't clobber
    # the Apple TV's token.
    user = users[user_key]
    tokens = user.get("tokens", [])

    # Update or add our sidecar's token entry
    sidecar_entry = None
    for tok in tokens:
        if tok.get("device_id") == SIDECAR_DEVICE_ID:
            sidecar_entry = tok
            break

    now_iso = datetime.utcnow().isoformat()
    if sidecar_entry:
        sidecar_entry["id_token"] = new_id_token
        sidecar_entry["refresh_token"] = new_refresh_token
        sidecar_entry["created_at"] = now_iso
    else:
        tokens.append({
            "id_token": new_id_token,
            "refresh_token": new_refresh_token,
            "device_id": SIDECAR_DEVICE_ID,
            "created_at": now_iso,
        })
        user["tokens"] = tokens

    with open(db_file, "w") as f:
        json.dump(db, f, indent=4)

    log.info("Token refreshed and written to UHF database")

    # Cache it
    state["auth"] = {
        "id_token": new_id_token,
        "refresh_token": new_refresh_token,
        "expires_at": time.time() + new_auth["expires_in"] - 60,
    }
    save_state(state)

    return new_id_token


# ── EPG Parsing ───────────────────────────────────────────────────────────────


def parse_xmltv_time(s: str) -> datetime:
    """Parse XMLTV time format '20260315090000 +0000' to datetime."""
    s = s.strip()
    if " " in s:
        dt_part, tz_part = s.rsplit(" ", 1)
        dt = datetime.strptime(dt_part, "%Y%m%d%H%M%S")
        tz_sign = 1 if tz_part[0] == "+" else -1
        tz_hours = int(tz_part[1:3])
        tz_mins = int(tz_part[3:5])
        tz_offset = timezone(timedelta(hours=tz_sign * tz_hours, minutes=tz_sign * tz_mins))
        return dt.replace(tzinfo=tz_offset)
    else:
        return datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def fetch_epg(xmltv_url: str) -> list[dict]:
    """Fetch and parse XMLTV guide into a list of programmes."""
    log.info("Fetching EPG from %s", xmltv_url)
    resp = requests.get(xmltv_url, timeout=30)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)

    channels = {}
    for ch in root.findall("channel"):
        ch_id = ch.get("id")
        name_el = ch.find("display-name")
        channels[ch_id] = name_el.text if name_el is not None else ch_id

    programmes = []
    for prog in root.findall("programme"):
        title_el = prog.find("title")
        desc_el = prog.find("desc")
        if title_el is None or not title_el.text:
            continue

        ch_id = prog.get("channel")
        programmes.append({
            "channel_id": ch_id,
            "channel_name": channels.get(ch_id, ch_id),
            "title": title_el.text.strip(),
            "description": desc_el.text.strip() if desc_el is not None and desc_el.text else "",
            "start": parse_xmltv_time(prog.get("start")),
            "stop": parse_xmltv_time(prog.get("stop")),
        })

    log.info("Parsed %d programmes across %d channels", len(programmes), len(channels))
    return programmes


def fetch_m3u(m3u_url: str) -> dict:
    """Fetch M3U and return mapping of channel_id -> stream_url."""
    log.info("Fetching M3U from %s", m3u_url)
    resp = requests.get(m3u_url, timeout=30)
    resp.raise_for_status()

    channel_urls = {}
    lines = resp.text.strip().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            m = re.search(r'tvg-id="([^"]*)"', line)
            if m and i + 1 < len(lines):
                ch_id = m.group(1)
                stream_url = lines[i + 1].strip()
                if stream_url and not stream_url.startswith("#"):
                    channel_urls[ch_id] = stream_url
                    i += 2
                    continue
        i += 1

    log.info("Found %d channel stream URLs", len(channel_urls))
    return channel_urls


# ── Matching ──────────────────────────────────────────────────────────────────


def programme_fingerprint(prog: dict) -> str:
    """Unique ID for a programme to avoid duplicate scheduling."""
    raw = f"{prog['channel_id']}|{prog['title']}|{prog['start'].isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def find_matches(
    programmes: list[dict],
    shows: list[dict],
    scheduled: dict,
    now: datetime,
) -> list[dict]:
    """Find programmes matching configured show patterns that haven't been scheduled yet."""
    matches = []
    for prog in programmes:
        if prog["start"] <= now:
            continue

        fp = programme_fingerprint(prog)
        if fp in scheduled:
            continue

        for show in shows:
            pattern = show["name"].lower()
            title = prog["title"].lower()

            if pattern not in title:
                continue

            # Check channel filter if specified
            if "channels" in show and show["channels"]:
                channel_match = any(
                    ch.lower() in prog["channel_name"].lower()
                    for ch in show["channels"]
                )
                if not channel_match:
                    continue

            matches.append({**prog, "fingerprint": fp, "matched_rule": show["name"]})
            break

    return matches


# ── UHF API ───────────────────────────────────────────────────────────────────


def schedule_recording(
    uhf_url: str,
    token: str,
    prog: dict,
    stream_url: str,
    buffer_before: int,
    buffer_after: int,
) -> dict | None:
    """Schedule a recording on UHF server via its REST API."""
    start = prog["start"] - timedelta(seconds=buffer_before)
    stop = prog["stop"] + timedelta(seconds=buffer_after)
    duration = int((stop - start).total_seconds())

    payload = {
        "name": prog["title"],
        "url": stream_url,
        "start_time": start.isoformat(),
        "duration_seconds": duration,
        "description": prog.get("description", ""),
        "metadata": {
            "channel": prog["channel_name"],
            "epg_title": prog["title"],
            "epg_start": prog["start"].isoformat(),
            "epg_stop": prog["stop"].isoformat(),
            "auto_recorded": True,
            "matched_rule": prog.get("matched_rule", ""),
        },
    }

    log.info(
        'Scheduling "%s" on %s at %s (%d min)',
        prog["title"],
        prog["channel_name"],
        prog["start"].strftime("%Y-%m-%d %H:%M"),
        duration // 60,
    )

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        resp = requests.post(
            f"{uhf_url}/dvr/recordings",
            json=payload,
            headers=headers,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            log.info("  -> Scheduled! Recording ID: %s", data.get("id", "?"))
            return data
        else:
            log.error("  -> Failed (%d): %s", resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        log.error("  -> Error: %s", e)
        return None


# ── Main Loop ─────────────────────────────────────────────────────────────────


def run_once(config: dict, state: dict):
    """Perform one check cycle."""
    shows = config.get("shows") or []
    shows = [s for s in shows if s.get("name")]
    if not shows:
        log.warning("No shows configured — add entries to 'shows' in config.yaml")
        return

    uhf_cfg = config["uhf_server"]
    tf_cfg = config["threadfin"]

    # Auth via DB
    token = get_auth_token(uhf_cfg["db_path"], state)

    # Fetch guide + channel map
    programmes = fetch_epg(tf_cfg["xmltv_url"])
    channel_urls = fetch_m3u(tf_cfg["m3u_url"])

    now = datetime.now(timezone.utc)

    # Clean up old fingerprints (older than 48 hours)
    scheduled = state.get("scheduled", {})
    cutoff = now.timestamp() - 48 * 3600
    scheduled = {k: v for k, v in scheduled.items() if v.get("ts", 0) > cutoff}
    state["scheduled"] = scheduled

    # Find matches
    matches = find_matches(programmes, shows, scheduled, now)

    if not matches:
        log.info("No new matches found")
        save_state(state)
        return

    log.info("Found %d new programme(s) to record", len(matches))

    buffer_before = config.get("buffer_before_seconds", 60)
    buffer_after = config.get("buffer_after_seconds", 120)

    for prog in matches:
        stream_url = channel_urls.get(prog["channel_id"])
        if not stream_url:
            log.warning(
                'No stream URL for channel %s (%s) — skipping "%s"',
                prog["channel_id"],
                prog["channel_name"],
                prog["title"],
            )
            continue

        result = schedule_recording(
            uhf_cfg["url"], token, prog, stream_url, buffer_before, buffer_after
        )

        scheduled[prog["fingerprint"]] = {
            "title": prog["title"],
            "channel": prog["channel_name"],
            "start": prog["start"].isoformat(),
            "ts": now.timestamp(),
            "recording_id": result.get("id") if result else None,
        }

    state["scheduled"] = scheduled
    save_state(state)


def main():
    config = load_config()
    state = load_state()

    interval = config.get("check_interval_minutes", 30) * 60
    log.info("EPG Auto-Recorder starting (check every %d min)", interval // 60)

    shows = config.get("shows") or []
    for s in shows:
        if s.get("name"):
            channels = s.get("channels")
            if channels:
                log.info('  Watching for "%s" on: %s', s["name"], ", ".join(channels))
            else:
                log.info('  Watching for "%s" on all channels', s["name"])

    while True:
        try:
            run_once(config, state)
        except Exception:
            log.exception("Error in check cycle")

        log.info("Next check in %d minutes", interval // 60)
        time.sleep(interval)


if __name__ == "__main__":
    main()
