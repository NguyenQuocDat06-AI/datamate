"""Wait until the relay announces a live endpoint different from the current one; write it to a separate file.
Usage: gen_wait_new_endpoint.py <out.json> [timeout_s]. Prints only the host."""
import json, sys, time
from urllib.parse import urlparse
sys.path.insert(0, "/home/ubuntu/datamate/source_test/scripts")
import kaggle_12h_client as k
cfg = k.load_connection_config()
import os
old = json.load(open("/home/ubuntu/datamate/source_test/artifacts/kaggle_12h_server/active_endpoint.json"))["base_url"]
EXCLUDE = {old} | {json.load(open(f))["base_url"] for f in os.environ.get("EXCLUDE_FILES", "").split(",") if f}
out, deadline = sys.argv[1], time.time() + (float(sys.argv[2]) if len(sys.argv) > 2 else 1500)
channel = cfg.get("relay_channel") or cfg.get("ntfy_channel") or cfg.get("channel")
while time.time() < deadline:
    data = k.query_relay_channel(channel) if channel else None
    url = (data or {}).get("base_url")
    if url and url not in EXCLUDE:
        ok = k.ping_endpoint(url, cfg.get("bearer_token", ""))[0]
        if ok:
            json.dump({"base_url": url}, open(out, "w"))
            print("new endpoint live:", urlparse(url).hostname); sys.exit(0)
    time.sleep(20)
print("timeout, no new endpoint"); sys.exit(1)
