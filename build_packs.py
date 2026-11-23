#!/usr/bin/env python3
"""Builds feeds/shorts_<id>.txt for every enabled pack in packs.json.
A pack is only replaced if the new build has at least min_ok Shorts, so a bad run never wipes a good feed."""
import json, os, subprocess, sys

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)
builder = os.environ.get("FEED_BUILDER", "feed_builder.py")
cfg = json.load(open("packs.json", encoding="utf-8"))
min_ok = int(cfg.get("min_ok", 200))
os.makedirs("feeds", exist_ok=True)
failed = []

for pack in cfg["packs"]:
    if not pack.get("enabled"):
        continue
    pid = pack["id"]
    tmp = f"feeds/.tmp_{pid}.txt"
    final = f"feeds/shorts_{pid}.txt"
    print(f"\n=== pack {pid} ===", flush=True)
    cmd = [sys.executable, builder, "--discover", "--queries", pack["queries"],
           "--countries", pack["countries"], "--lang", pack["lang"],
           "--count", str(pack.get("count", 1000)), "--pages", "2", "--vary", "--out", tmp]
    rc = subprocess.call(cmd)
    n = 0
    if rc == 0 and os.path.exists(tmp):
        n = sum(1 for l in open(tmp, encoding="utf-8") if l.strip() and not l.startswith("#"))
    if n >= min_ok:
        os.replace(tmp, final)
        print(f"pack {pid}: wrote {n} Shorts to {final}")
    else:
        print(f"pack {pid}: only {n} Shorts (need {min_ok}); keeping the previous feed", file=sys.stderr)
        failed.append(pid)
    if os.path.exists(tmp):
        os.remove(tmp)

if failed:
    sys.exit("Packs not refreshed: " + ", ".join(failed))
