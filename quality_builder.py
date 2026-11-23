#!/usr/bin/env python3
"""
Quality feed builder for the VRChat Shorts player.

Instead of searching by keyword (expensive, and full of spam), this starts from what people in the
US and Canada actually watch:

  1. YouTube's own "most popular" charts for the US and Canada (a few categories each). A channel
     can't fake appearing here by declaring a country or uploading through a VPN.
  2. The channels behind those charts become the candidate set. Each must look like a real,
     established channel (subscribers, age, upload rate).
  3. From each channel's Shorts shelf (hidden playlist "UUSH" + id[2:], so everything is a true
     Short) we take recent Shorts and keep only ones with solid views and likes, no AI disclosure
     flag, and no obvious AI wording.
  4. A few per channel, scored by popularity with a little seeded randomness, become the feed.

Everything is automatic - there is nothing to maintain by hand. Costs about 1 API unit per call
(no 100-unit searches) and stops by itself at --budget units.

Usage (from the ShortsPlayer_Tools folder, with yt_key.txt present or YT_API_KEY set):
  py quality_builder.py --preview            # builds, prints 30 sample picks + a funnel; writes NO live feed
  py quality_builder.py --out shorts_quality.txt --count 5000
"""
import argparse
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://www.googleapis.com/youtube/v3/"
DUR_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

# YouTube video categories used for the regional charts (News & Politics is left out on purpose).
DEFAULT_CATEGORIES = [0, 1, 2, 10, 15, 17, 19, 20, 22, 23, 24, 26, 27, 28]

AI_RE = re.compile(
    r"(?<![a-z0-9])(ai|a\.i\.|artificial intelligence|ai[- ]?generated|ai[- ]?art|ai[- ]?video|ai[- ]?animals?|"
    r"sora|veo ?\d?|midjourney|runway ?ml|kling|chatgpt|stable diffusion|deepfake|text[- ]to[- ]video|"
    r"made with ai|generated (?:by|with) ai)(?![a-z0-9])", re.I)
AI_TAG_RE = re.compile(r"#?ai(?:art|video|animal|animals|generated|cat|dog|pets?|shorts?)?$", re.I)


class QuotaStop(Exception):
    pass


class Api:
    """Thin wrapper that counts quota units and stops at a budget."""

    def __init__(self, key, budget, fetch=None):
        self.key, self.budget, self.units = key, budget, 0
        self.fetch = fetch or self._http

    def _http(self, url):
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.load(r)

    def get(self, endpoint, quiet=False, **params):
        if self.units >= self.budget:
            raise QuotaStop(f"budget of {self.budget} units reached")
        params["key"] = self.key
        url = API + endpoint + "?" + urllib.parse.urlencode(params)
        self.units += 1
        for attempt in range(3):
            try:
                return self.fetch(url)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if "quotaExceeded" in body or "dailyLimitExceeded" in body:
                    raise QuotaStop("YouTube says today's quota is used up")
                if e.code in (500, 502, 503) and attempt < 2:
                    time.sleep(1.5)
                    continue
                if not quiet:
                    print(f"  (skipped: {endpoint} error {e.code})", file=sys.stderr)
                return {}
            except Exception:
                if attempt < 2:
                    time.sleep(1.5)
                    continue
                return {}
        return {}


def parse_duration(iso):
    m = DUR_RE.match(iso or "")
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def parse_time(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


def looks_latin(text, min_ratio=0.85):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    return sum(1 for c in letters if ord(c) < 0x250) / len(letters) >= min_ratio


def chunks(seq, n=50):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def ai_wording(snippet):
    text = " ".join([snippet.get("title", ""), snippet.get("description", "")[:600]])
    if AI_RE.search(text):
        return True
    return any(AI_TAG_RE.match(t.strip()) for t in snippet.get("tags", []) or [])


# ---------------------------------------------------------------- pipeline
def collect_chart_channels(api, regions, categories, chart_pages, funnel):
    """Channel id -> {'score': chart appearances, 'short_len': appearances with a Shorts-length video}."""
    chans = {}
    for region in regions:
        for cat in categories:
            token = None
            for page in range(chart_pages):
                p = dict(part="snippet,contentDetails", chart="mostPopular", regionCode=region,
                         maxResults=50)
                if cat:
                    p["videoCategoryId"] = str(cat)
                if token:
                    p["pageToken"] = token
                data = api.get("videos", quiet=True, **p)
                items = data.get("items", [])
                funnel["chart videos seen"] = funnel.get("chart videos seen", 0) + len(items)
                for it in items:
                    cid = it.get("snippet", {}).get("channelId")
                    if not cid:
                        continue
                    d = parse_duration(it.get("contentDetails", {}).get("duration")) or 9999
                    c = chans.setdefault(cid, {"score": 0, "short_len": 0})
                    c["score"] += 1
                    if d <= 180:
                        c["short_len"] += 1
                token = data.get("nextPageToken")
                if not token:
                    break
    return chans


def vet_channels(api, chans, a, funnel):
    """Keep established channels. Returns {channel_id: info}."""
    good = {}
    now = datetime.now(timezone.utc)
    ids = list(chans)
    funnel["candidate channels (from charts)"] = len(ids)
    for batch in chunks(ids):
        data = api.get("channels", part="snippet,statistics", id=",".join(batch))
        for it in data.get("items", []):
            cid, sn, st = it["id"], it.get("snippet", {}), it.get("statistics", {})
            if st.get("hiddenSubscriberCount"):
                funnel["  dropped: hidden subscriber count"] = funnel.get("  dropped: hidden subscriber count", 0) + 1
                continue
            subs = int(st.get("subscriberCount", 0))
            vids = int(st.get("videoCount", 0))
            born = parse_time(sn.get("publishedAt"))
            age_days = max(1, (now - born).days) if born else 0
            reason = None
            if subs < a.min_subs:
                reason = "  dropped: too few subscribers"
            elif age_days < a.min_channel_age_days:
                reason = "  dropped: channel too new"
            elif vids / age_days > a.max_uploads_per_day:
                reason = "  dropped: uploads too fast (content farm pattern)"
            elif a.countries and (sn.get("country") or "").upper() not in a.countries and sn.get("country"):
                reason = "  dropped: declared another country"
            if reason:
                funnel[reason] = funnel.get(reason, 0) + 1
                continue
            good[cid] = {"title": sn.get("title", "?"), "subs": subs, "chart": chans[cid]}
    funnel["channels kept"] = len(good)
    return good


def channel_shorts(api, cid, pages=1):
    ids, token = [], None
    for _ in range(pages):
        p = dict(part="contentDetails", playlistId="UUSH" + cid[2:], maxResults=50)
        if token:
            p["pageToken"] = token
        data = api.get("playlistItems", quiet=True, **p)
        ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return ids


def vet_videos(api, ids_by_channel, channels, a, funnel, keep):
    """Fetch details for all candidate Shorts and apply the quality gates."""
    now = datetime.now(timezone.utc)
    all_ids = [v for ids in ids_by_channel.values() for v in ids]
    funnel["Shorts found on those channels"] = len(all_ids)
    owner = {v: c for c, ids in ids_by_channel.items() for v in ids}

    def drop(why):
        k = "  dropped: " + why
        funnel[k] = funnel.get(k, 0) + 1

    for batch in chunks(all_ids):
        data = api.get("videos", part="snippet,contentDetails,statistics,status", id=",".join(batch))
        for it in data.get("items", []):
            sn, st, cd, status = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {}), it.get("status", {})
            dur = parse_duration(cd.get("duration"))
            views = int(st.get("viewCount", 0))
            likes = st.get("likeCount")
            ratio = (int(likes) / views) if (likes is not None and views) else None
            born = parse_time(sn.get("publishedAt"))
            if dur is None or dur < 5 or dur > a.max_seconds:
                drop("wrong length"); continue
            if status.get("privacyStatus") != "public":
                drop("not public"); continue
            if (cd.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
                drop("age restricted"); continue
            if status.get("containsSyntheticMedia"):
                drop("flagged as AI/synthetic by creator"); continue
            if ai_wording(sn):
                drop("AI wording in title/description/tags"); continue
            if views < a.min_views:
                drop("too few views"); continue
            if ratio is not None and ratio < a.min_like_ratio:
                drop("low like ratio"); continue
            if born and (now - born).days > a.max_age_days:
                drop("too old"); continue
            lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower()
            if lang and not lang.startswith(a.lang):
                drop("labelled as another language"); continue
            if not looks_latin(sn.get("title", "")):
                drop("title not in Latin script"); continue
            score = math.log10(views + 1) + (min(ratio, 0.08) * 20 if ratio is not None else 0.0)
            keep.append({"id": it["id"], "cid": owner.get(it["id"]), "views": views, "ratio": ratio,
                         "dur": dur, "title": sn.get("title", ""), "score": score})
    funnel["Shorts passing every check"] = len(keep)


def select(keep, channels, a):
    rng = random.Random(a.seed)
    by_ch = {}
    for v in keep:
        by_ch.setdefault(v["cid"], []).append(v)
    picked = []
    for cid, vs in by_ch.items():
        vs.sort(key=lambda v: v["score"], reverse=True)
        pool = vs[: a.per_channel * 2]
        picked += rng.sample(pool, min(a.per_channel, len(pool)))
    for v in picked:
        v["jit"] = v["score"] + rng.random() * 1.0
    picked.sort(key=lambda v: v["jit"], reverse=True)
    picked = picked[: a.count]
    rng.shuffle(picked)
    return picked


def main(argv=None, fetch=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", help="YouTube API key (default: yt_key.txt or YT_API_KEY)")
    ap.add_argument("--regions", default="US,CA")
    ap.add_argument("--categories", default=",".join(map(str, DEFAULT_CATEGORIES)))
    ap.add_argument("--chart-pages", type=int, default=2, help="pages of 50 per chart (if YouTube allows paging)")
    ap.add_argument("--count", type=int, default=5000)
    ap.add_argument("--per-channel", type=int, default=10)
    ap.add_argument("--max-channels", type=int, default=800, help="most channels whose Shorts we pull (1 unit each)")
    ap.add_argument("--min-subs", type=int, default=100_000)
    ap.add_argument("--min-views", type=int, default=250_000)
    ap.add_argument("--min-like-ratio", type=float, default=0.015)
    ap.add_argument("--min-channel-age-days", type=int, default=365)
    ap.add_argument("--max-uploads-per-day", type=float, default=4.0)
    ap.add_argument("--max-age-days", type=int, default=730)
    ap.add_argument("--max-seconds", type=int, default=90)
    ap.add_argument("--lang", default="en", help="rejects videos explicitly labelled as a different language")
    ap.add_argument("--countries", default="", help="optional: also reject channels that declare a country outside this list (e.g. US,CA)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--budget", type=int, default=3500, help="stop after this many API units")
    ap.add_argument("--min-ok", type=int, default=200, help="refuse to write a feed smaller than this")
    ap.add_argument("--out", default="shorts_quality.txt")
    ap.add_argument("--report", default="quality_report.txt", help="where to save the full list of picks with channels and titles")
    ap.add_argument("--preview", action="store_true", help="print samples and a report; do not write the feed file")
    ap.add_argument("--sample", type=int, default=30)
    a = ap.parse_args(argv)
    a.countries = {c.strip().upper() for c in a.countries.split(",") if c.strip()}
    a.seed = a.seed if a.seed is not None else random.randrange(1, 2**31)

    key = a.key or os.environ.get("YT_API_KEY")
    if not key and os.path.exists("yt_key.txt"):
        key = open("yt_key.txt", encoding="utf-8").read().strip()
    if not key and fetch is None:
        key = input("Paste your YouTube API key (stays on your PC): ").strip()
    api = Api(key or "test", a.budget, fetch)
    funnel = {}
    regions = [r.strip().upper() for r in a.regions.split(",") if r.strip()]
    cats = [int(c) for c in a.categories.split(",") if c.strip()]

    good, ids_by_channel, keep = {}, {}, []
    try:
        print(f"1/4 reading the {'/'.join(regions)} most-popular charts ...")
        chans = collect_chart_channels(api, regions, cats, a.chart_pages, funnel)
        print(f"2/4 vetting {len(chans)} channels ...")
        good = vet_channels(api, chans, a, funnel)
        ranked = sorted(good, key=lambda c: (good[c]["chart"]["short_len"], good[c]["chart"]["score"]), reverse=True)[: a.max_channels]
        print(f"3/4 reading the Shorts shelves of {len(ranked)} channels ...")
        ids_by_channel = {}
        for cid in ranked:
            ids = channel_shorts(api, cid)
            if ids:
                ids_by_channel[cid] = ids
        print("4/4 checking each Short ...")
        vet_videos(api, ids_by_channel, good, a, funnel, keep)
    except QuotaStop as e:
        print(f"\nStopped early: {e}. Using what was collected so far.", file=sys.stderr)
        funnel["Shorts passing every check"] = len(keep)

    picks = select(keep, good, a)
    chan_title = {c: v["title"] for c, v in good.items()}

    print("\n==== what happened (each step keeps fewer) ====")
    for k, v in funnel.items():
        print(f"  {v:>6}  {k}")
    print(f"  {len(picks):>6}  FINAL PICKS  (seed {a.seed})")
    print(f"\nAPI units used: {api.units} of {a.budget}")

    if picks:
        n_ch = len({p['cid'] for p in picks})
        print(f"Picks come from {n_ch} different channels.")
        print(f"\n==== random sample of {min(a.sample, len(picks))} picks (open the links to judge quality) ====")
        for p in random.Random(1).sample(picks, min(a.sample, len(picks))):
            like = f"{p['ratio']*100:.1f}%" if p["ratio"] is not None else "n/a"
            print(f"  https://youtube.com/shorts/{p['id']}  {p['views']:>9,} views  {like:>5} likes  {p['dur']:>3}s  "
                  f"[{chan_title.get(p['cid'], '?')[:22]}]  {p['title'][:48]}")
        with open(a.report, "w", encoding="utf-8") as f:
            for p in sorted(picks, key=lambda p: p["views"], reverse=True):
                f.write(f"{p['id']}\t{p['views']}\t{p['dur']}s\t{chan_title.get(p['cid'], '?')}\t{p['title']}\n")
        print(f"\nFull list with channels saved to {a.report}")

    if a.preview:
        print("\n(preview mode: no feed file written)")
        return 0
    if len(picks) < a.min_ok:
        print(f"\nOnly {len(picks)} picks (< {a.min_ok}); NOT writing {a.out}. Loosen --min-views / --min-subs and retry.", file=sys.stderr)
        return 2
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(f"# {len(picks)} Shorts - quality build {datetime.now().strftime('%Y-%m-%d')} - seed {a.seed}\n")
        for p in picks:
            f.write(p["id"] + "\n")
    print(f"\nWrote {len(picks)} IDs to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
