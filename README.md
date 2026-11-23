# Shorts feed
Auto-built Shorts lists for the VRChat Shorts Player, refreshed daily. Files in `feeds/` are what the redirect server reads.

- `feeds/shorts_en.txt` - the live list (YouTube video IDs, one per line).
- `feeds/report_en.txt` - the same picks with views, length, channel and title, for checking quality.
- `packs.json` - turn packs on/off and tune the English pack (min_subs, min_views, per_channel, count, min_ok).
- The English pack is built from YouTube's US and Canada most-popular charts (`quality_builder.py`). The other packs
  (disabled) still use the older keyword search in `queries/`.
