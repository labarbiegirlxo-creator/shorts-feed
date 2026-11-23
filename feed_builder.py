#!/usr/bin/env python3
"""
Build shorts.txt (one YouTube video ID per line) for the VRChat Shorts player.

YouTube has no official API for the signed-out Shorts feed, so this samples a similar
mix instead:
  * recent/popular Shorts from search queries (topics you choose), and
  * each channel's Shorts shelf (channel ID -> hidden playlist "UUSH" + id[2:]).
Results are filtered, de-duplicated and shuffled (fixed seed => same order every build).

Needs a YouTube Data API v3 key:  export YT_API_KEY=...
Quota: each search query costs ~100 units (default 10,000/day); playlist/video lookups cost 1.

Usage:
  python3 feed_builder.py --queries queries.txt --channels channels.txt --playlists playlists.txt --count 300
  python3 feed_builder.py --ids-file my_ids.txt --count 300 --no-api       # just clean/shuffle your own list
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/youtube/v3/"
ID_RE = re.compile(r"(?:shorts/|v=|youtu\.be/|^)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
DUR_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def parse_duration(iso):
    """ISO-8601 duration (e.g. PT1M5S) -> seconds, or None if unparseable."""
    m = DUR_RE.match(iso or "")
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def extract_id(text):
    m = ID_RE.search(text.strip())
    return m.group(1) if m else None


def read_lines(path):
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]


def api_get(endpoint, key, quiet=False, **params):
    params["key"] = key
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if not quiet:
            print(f"  API error {e.code} on {endpoint}: {body}", file=sys.stderr)
        return {}


def search_ids(query, key, region, pages=2, vary=False, lang="en"):
    ids, token = [], None
    extra = {}
    if vary:
        # A different slice of YouTube every run: random sort order + random upload-date window.
        days = random.randint(60, 1100)
        after = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - days * 86400))
        extra = dict(order=random.choice(["relevance", "date", "viewCount", "rating"]), publishedAfter=after)
        print(f"    (order={extra['order']}, uploaded after {after[:10]})")
    for _ in range(pages):
        p = dict(part="id", type="video", videoDuration="short", q=query + " #shorts",
                 maxResults=50, regionCode=region, safeSearch="moderate",
                 **({"relevanceLanguage": lang} if lang else {}), **extra)
        if token:
            p["pageToken"] = token
        data = api_get("search", key, **p)
        ids += [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
        token = data.get("nextPageToken")
        if not token:
            break
    return ids


def channel_short_ids(channel_id, key, pages=2):
    if not channel_id.startswith("UC"):
        print(f"  skipping '{channel_id}' (expected a channel ID starting with UC)", file=sys.stderr)
        return []
    playlist = "UUSH" + channel_id[2:]
    ids, token = [], None
    for _ in range(pages):
        p = dict(part="contentDetails", playlistId=playlist, maxResults=50)
        if token:
            p["pageToken"] = token
        data = api_get("playlistItems", key, quiet=True, **p)  # quiet: channels with no Shorts shelf just 404
        ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return ids


def discover_channels(query, key, region, lang, pages=2, vary=False):
    """Find channels for a topic (search type=channel)."""
    out, token = [], None
    order = random.choice(["relevance", "viewCount", "videoCount"]) if vary else "relevance"
    for _ in range(pages):
        p = dict(part="snippet", type="channel", q=query, maxResults=50, regionCode=region, order=order)
        if lang:
            p["relevanceLanguage"] = lang
        if token:
            p["pageToken"] = token
        data = api_get("search", key, **p)
        out += [i["id"]["channelId"] for i in data.get("items", []) if i.get("id", {}).get("channelId")]
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def channel_countries(ids, key):
    """channel id -> (country, language) the owner set on their channel (either may be missing)."""
    res = {}
    for i in range(0, len(ids), 50):
        data = api_get("channels", key, part="snippet", id=",".join(ids[i:i + 50]))
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            res[item["id"]] = ((sn.get("country") or "").upper(), (sn.get("defaultLanguage") or "").lower().split("-")[0])
    return res


PLAYLIST_RE = re.compile(r"(?:list=)?((?:PL|UU|LL|FL|OL)[A-Za-z0-9_-]{10,})")


def extract_playlist_id(text):
    """Accepts a bare playlist ID (PL...) or a youtube.com/playlist?list=... URL."""
    m = PLAYLIST_RE.search(text.strip())
    return m.group(1) if m else None


def playlist_video_ids(playlist_id, key, max_pages=20):
    """All video IDs in a PUBLIC (or unlisted) playlist, in order. Needs only the API key."""
    ids, token = [], None
    for _ in range(max_pages):
        p = dict(part="contentDetails", playlistId=playlist_id, maxResults=50)
        if token:
            p["pageToken"] = token
        data = api_get("playlistItems", key, **p)
        ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return ids


def looks_latin(text, min_ratio=0.85):
    """Rough check that a title is written in a Latin alphabet (English/Spanish/etc.), not Hindi/Thai/Arabic/CJK..."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if ord(c) < 0x250)
    return latin / len(letters) >= min_ratio


def language_ok(snippet, lang):
    """Keep a video if its declared language matches; if none is declared, fall back to the title's alphabet."""
    if not lang:
        return True
    declared = (snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or "").lower()
    if declared:
        return declared.split("-")[0] == lang.lower()
    return looks_latin(snippet.get("title", "")) if lang.lower() == "en" else True


def filter_playable(ids, key, max_seconds, lang="en"):
    """Keep public, non-age-restricted videos no longer than max_seconds."""
    keep = []
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        data = api_get("videos", key, part="contentDetails,status,snippet", id=",".join(batch))
        for item in data.get("items", []):
            dur = parse_duration(item.get("contentDetails", {}).get("duration"))
            if dur is None or dur == 0 or dur > max_seconds:
                continue
            if item.get("status", {}).get("privacyStatus") != "public":
                continue
            rating = item.get("contentDetails", {}).get("contentRating", {})
            if rating.get("ytRating") == "ytAgeRestricted":  # needs sign-in, will fail in VRChat
                continue
            if item.get("status", {}).get("uploadStatus") not in (None, "processed"):
                continue
            if not language_ok(item.get("snippet", {}), lang):
                continue
            keep.append(item["id"])
    return keep


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def is_real_short(video_id):
    """youtube.com/shorts/<id> returns 200 for Shorts and redirects (3xx) for normal videos."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(f"https://www.youtube.com/shorts/{video_id}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if 300 <= e.code < 400 else None  # None = couldn't tell
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", help="text file: one search topic per line")
    ap.add_argument("--channels", help="text file: one channel ID (UC...) per line")
    ap.add_argument("--playlists", help="text file: one public playlist ID or URL per line (your curated Shorts playlist)")
    ap.add_argument("--ids-file", help="text file of your own video IDs/URLs to include")
    ap.add_argument("--count", type=int, default=300, help="how many Shorts to keep")
    ap.add_argument("--max-seconds", type=int, default=180, help="Shorts can be up to 3 minutes")
    ap.add_argument("--pages", type=int, default=2, help="result pages (50 each) per search topic; each page costs 100 API quota units. Max useful: 10")
    ap.add_argument("--region", default="US")
    ap.add_argument("--discover", action="store_true", help="BEST for a regional feed: find channels for your topics, keep only channels whose owner set one of --countries, then sample Shorts from each channel's Shorts shelf")
    ap.add_argument("--countries", default="US,CA", help="channel countries to keep in --discover mode (comma-separated)")
    ap.add_argument("--per-channel", type=int, default=5, help="random Shorts taken from each channel in --discover mode (more variety = lower number)")
    ap.add_argument("--lang", default="en", help="keep only Shorts in this language (2-letter code, default en). Use --lang \"\" to allow every language")
    ap.add_argument("--seed", type=int, default=None, help="same seed => same shuffle (default: random each run)")
    ap.add_argument("--vary", action="store_true", help="random sort order + upload-date window for each topic, so every run gives a different mix")
    ap.add_argument("--out", default="shorts.txt")
    ap.add_argument("--no-api", action="store_true", help="skip the API; only clean/shuffle --ids-file")
    ap.add_argument("--shorts-check", action="store_true", help="OFF by default. Extra check that hits youtube.com/shorts/<id> for every video from this PC; it can get your IP flagged by YouTube, so only use it on a small list")
    ap.add_argument("--no-shorts-check", action="store_true", help="(kept for old commands; the check is already off by default)")
    args = ap.parse_args()

    if args.seed is None:
        args.seed = random.randrange(10**9)
    pool = []
    for line in read_lines(args.ids_file):
        vid = extract_id(line)
        if vid:
            pool.append(vid)

    if not args.no_api:
        key = os.environ.get("YT_API_KEY")
        if not key:
            sys.exit("Set YT_API_KEY (YouTube Data API v3 key), or use --no-api.")
        if args.discover:
            wanted = {c.strip().upper() for c in args.countries.split(",") if c.strip()}
            found = []
            for q in read_lines(args.queries):
                print(f"find channels: {q}")
                found += discover_channels(q, key, args.region, args.lang, pages=args.pages, vary=args.vary)
            found = list(dict.fromkeys(found))
            countries = channel_countries(found, key)
            want_lang = (args.lang or "").lower()
            # keep: owner-declared country matches, and (if the owner declared a channel language) it matches too
            good = [c for c in found
                    if countries.get(c, ("", ""))[0] in wanted
                    and (not want_lang or not countries[c][1] or countries[c][1] == want_lang)]
            print(f"{len(good)} of {len(found)} channels are in {'/'.join(sorted(wanted))}" + (f" and not declared as another language than {want_lang}" if want_lang else ""))
            random.Random(args.seed).shuffle(good)
            for n, c in enumerate(good, 1):
                if len(pool) >= args.count * 1.4:
                    break
                shelf = channel_short_ids(c, key, pages=1)
                random.Random(args.seed + n).shuffle(shelf)
                pool += shelf[:args.per_channel]
            print(f"collected {len(pool)} Shorts from {min(n, len(good)) if good else 0} channels")
        else:
            for q in read_lines(args.queries):
                print(f"search: {q}")
                pool += search_ids(q, key, args.region, pages=args.pages, vary=args.vary, lang=args.lang)
        for c in read_lines(args.channels):
            print(f"channel: {c}")
            pool += channel_short_ids(c, key)
        for line in read_lines(args.playlists):
            pid = extract_playlist_id(line)
            if not pid:
                print(f"  skipping '{line}' (not a playlist ID/URL)", file=sys.stderr)
                continue
            print(f"playlist: {pid}")
            pool += playlist_video_ids(pid, key)
        pool = list(dict.fromkeys(pool))
        print(f"{len(pool)} candidates; filtering by duration/status...")
        before = len(pool)
        pool = filter_playable(pool, key, args.max_seconds, None if args.discover else args.lang)
        print(f"{len(pool)} of {before} passed the duration / privacy" + ("" if args.discover else f" / language ({args.lang or 'any'})") + " filters")

    pool = list(dict.fromkeys(pool))
    random.Random(args.seed).shuffle(pool)

    kept = []
    for vid in pool:
        if len(kept) >= args.count:
            break
        if args.shorts_check and not args.no_shorts_check and not args.no_api:
            res = is_real_short(vid)
            time.sleep(0.15)
            if res is False:
                continue
        kept.append(vid)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(f"# {len(kept)} Shorts - built {time.strftime('%Y-%m-%d')} - seed {args.seed}\n")
        f.write("\n".join(kept) + "\n")
    print(f"wrote {len(kept)} IDs to {args.out}")


if __name__ == "__main__":
    main()
