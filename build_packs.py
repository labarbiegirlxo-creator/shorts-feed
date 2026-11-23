#!/usr/bin/env python3
"""Builds feeds/shorts_<id>.txt for every enabled pack in packs.json.

A pack is only replaced if the new build has at least its min_ok Shorts, so a bad or partial run
never wipes a good feed. A pack with "mode": "quality" is built from the US/Canada popularity charts
(quality_builder.py); any other pack uses the older keyword-search builder (feed_builder.py)."""
import json, os, subprocess, sys

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)
old_builder = os.environ.get("FEED_BUILDER", "feed_builder.py")
quality_builder = os.environ.get("QUALITY_BUILDER", "quality_builder.py")
cfg = json.load(open("packs.json", encoding="utf-8"))
default_min_ok = int(cfg.get("min_ok", 200))
os.makedirs("feeds", exist_ok=True)
failed = []

for pack in cfg["packs"]:
    if not pack.get("enabled"):
        continue
    pid = pack["id"]
    tmp = f"feeds/.tmp_{pid}.txt"
    final = f"feeds/shorts_{pid}.txt"
    report = f"feeds/report_{pid}.txt"
    tmp_report = f"feeds/.tmp_report_{pid}.txt"
    min_ok = int(pack.get("min_ok", default_min_ok))
    print(f"\n=== pack {pid} ===", flush=True)
    if pack.get("mode") == "quality":
        cmd = [sys.executable, quality_builder, "--out", tmp, "--report", tmp_report,
               "--count", str(pack.get("count", 5000)), "--min-ok", "1",
               "--regions", pack.get("regions", "US,CA"), "--lang", pack.get("lang", "en")]
        for flag, key in (("--per-channel", "per_channel"), ("--min-subs", "min_subs"), ("--min-views", "min_views"),
                          ("--max-channels", "max_channels"), ("--budget", "budget"),
                          ("--max-uploads-per-day", "max_uploads_per_day")):
            if key in pack:
                cmd += [flag, str(pack[key])]
    else:
        cmd = [sys.executable, old_builder, "--discover", "--queries", pack["queries"],
               "--countries", pack["countries"], "--lang", pack["lang"],
               "--count", str(pack.get("count", 1000)), "--pages", "2", "--vary", "--out", tmp]
    rc = subprocess.call(cmd)
    n = 0
    if rc == 0 and os.path.exists(tmp):
        n = sum(1 for l in open(tmp, encoding="utf-8") if l.strip() and not l.startswith("#"))
    if n >= min_ok:
        os.replace(tmp, final)
        if os.path.exists(tmp_report):
            os.replace(tmp_report, report)
        print(f"pack {pid}: wrote {n} Shorts to {final}")
    else:
        print(f"pack {pid}: only {n} Shorts (need {min_ok}); keeping the previous feed", file=sys.stderr)
        failed.append(pid)
    for leftover in (tmp, tmp_report):
        if os.path.exists(leftover):
            os.remove(leftover)

if failed:
    sys.exit("Packs not refreshed: " + ", ".join(failed))
