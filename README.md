# EPG Auto-Recorder for UHF Server

A sidecar service that automatically records TV shows by name. Instead of manually scheduling recordings at specific times, just tell it what shows you want and it handles the rest. It handles polling your EPG guide, finding matches, and scheduling recordings on your UHF Server.

## The Problem

UHF Server's built-in recording scheduler only supports:
- **One-shot recordings** — pick an exact date, time, and duration
- **Day-of-week recurrence** — same time, specific weekdays

If a show airs at inconsistent times, moves time slots, or you just want to catch every airing across multiple channels, you're out of luck.

## The Solution

This sidecar watches your XMLTV guide data and automatically schedules recordings whenever a show matching your configured name patterns appears. It:

1. Polls your EPG source (e.g. Threadfin, IPTV) on a configurable interval
2. Searches for upcoming programmes matching your show name patterns
3. Schedules recordings via the UHF Server REST API
4. Tracks what's been scheduled to avoid duplicates

No credentials required! It reads auth tokens directly from UHF Server's database.

## Requirements

- A running [UHF Server](https://github.com/swapplications/uhf-server-dist) instance
- An XMLTV/EPG source and M3U playlist (e.g. [Threadfin](https://github.com/Threadfin/Threadfin))
- Docker and Docker Compose

## Quick Start

### 1. Download

In your uhf server directory:

```
git clone https://github.com/RickConsole/epg-recorder
```

Add `epg-recorder` to your uhf-server `docker-compose.yml`

```
services:
  uhf-server:
    image: swapplications/uhf-server:1.6.0
    # ... your existing UHF config ...
    volumes:
      - ./uhf-recordings:/recordings
      - ./uhf-db:/var/lib/uhf-server

  epg-recorder:
    build: ./epg-recorder
    container_name: epg-recorder
    restart: unless-stopped
    volumes:
      - ./epg-recorder/config.yaml:/app/config.yaml:ro
      - ./epg-recorder/state.json:/app/state.json
      - ./uhf-db:/uhf-db # source volume path should match uhf-server
    network_mode: host
```

The key volume mount is `./uhf-db:/uhf-db` — this shares UHF Server's database with the sidecar for auth token management.


### 2. Configure

Edit `config.yaml`:

```yaml
firebase_api_key: "your-key-here"

uhf_server:
  url: "http://192.168.0.5:8000"
  db_path: "/uhf-db/db.json"

threadfin:
  xmltv_url: "http://192.168.0.5:34400/xmltv/threadfin.xml"
  m3u_url: "http://192.168.0.5:34400/m3u/threadfin.m3u"

shows:
  - name: "Planet Earth"
  - name: "Real Housewives"
    channels:
      - "US: Bravo"
```

#### Setup Firebase API key
Then, you need to extract the Firebase API key from the UHF server binary and add it to `config.yaml`. This is easy enough to extract but it is not something I want to openly publish here.
```yaml
firebase_api_key: "your-key-here"
```

### 3. Run

```bash
docker compose up -d
```

### 3. Check logs

## Configuration Reference

### `uhf_server`

| Key | Description |
|-----|-------------|
| `url` | URL of your UHF Server instance |
| `db_path` | Path to UHF's TinyDB JSON file (inside the container — mapped via volume) |

### `EPG Data`

| Key | Description |
|-----|-------------|
| `xmltv_url` | URL to your XMLTV guide (e.g. Threadfin's `/xmltv/threadfin.xml`) |
| `m3u_url` | URL to your M3U playlist (e.g. Threadfin's `/m3u/threadfin.m3u`) |

### `shows`

A list of show patterns to watch for. Each entry has:

| Key | Required | Description |
|-----|----------|-------------|
| `name` | Yes | Show name pattern (case-insensitive substring match) |
| `channels` | No | List of channel names to restrict matching to. Omit to match all channels. |

#### Name Matching

Matching is a **case-insensitive substring search** against the programme title from the EPG. This means:

- `"Planet Earth"` matches "Planet Earth", "Planet Earth: Life", "Planet Earth III"
- `"Housewives"` matches "The Real Housewives of Beverly Hills", "The Real Housewives of Atlanta", etc.
- `"Hook"` matches "Hook", "Captain Hook", "Hooked on Phonics" — be specific enough to avoid false positives

#### Channel Filtering

Channel names are also matched as **case-insensitive substrings**, so:

- `"BBC America"` matches `"US: BBC America"`
- `"National Geographic"` matches both `"US: National Geographic"` and `"US: National Geographic Wild"`

You can list multiple channels — a match on **any** of them will trigger a recording:

```yaml
shows:
  - name: "Planet Earth"
    channels:
      - "BBC America"
      - "National Geographic"
```

### Timing Options

| Key | Default | Description |
|-----|---------|-------------|
| `check_interval_minutes` | `30` | How often to poll the EPG for new matches |
| `buffer_before_seconds` | `60` | Start recording this many seconds before the show starts |
| `buffer_after_seconds` | `120` | Keep recording this many seconds after the show ends |

## How Auth Works

UHF Server requires Firebase authentication for all API calls. Rather than requiring you to know or configure credentials, this sidecar:

1. Reads the existing `refresh_token` from UHF's TinyDB database file
2. Refreshes it via the Firebase REST API to get a new `id_token`
3. Writes the new token back into the database as a separate device entry (`epg-recorder-sidecar`)
4. Uses that token for all UHF API calls

The sidecar is designed to run alongside UHF Server. Add it to your existing `docker-compose.yml`:

## State File

The sidecar maintains a `state.json` file that tracks:
- Which programmes have already been scheduled (to avoid duplicates)
- Cached auth tokens (to avoid unnecessary refreshes)

Fingerprints are automatically cleaned up after 48 hours. Deleting this file is safe — it will simply re-evaluate all upcoming programmes on the next cycle and may re-schedule shows that are already scheduled in UHF (UHF handles overlapping recordings gracefully).

