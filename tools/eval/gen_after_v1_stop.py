"""Wait until the v1 endpoint dies, push the base-model server to account 1, wait for its endpoint, then run item 7."""
import json, subprocess, sys, time
sys.path.insert(0, "/home/ubuntu/datamate/source_test/scripts")
import kaggle_12h_client as k
cfg = k.load_connection_config()
D = "/home/ubuntu/datamate_review_data"
PY = "/home/ubuntu/datamate/venv/bin/python"
v1 = json.load(open(f"{D}/endpoint_batch.json"))["base_url"]
deadline = time.time() + 3 * 3600
while time.time() < deadline and k.ping_endpoint(v1, cfg.get("bearer_token", ""))[0]:
    time.sleep(30)
print("v1 stopped" if time.time() < deadline else "v1 still alive, giving up"); sys.stdout.flush()
if time.time() >= deadline: sys.exit(1)
time.sleep(60)  # let Kaggle release the session slot
r = subprocess.run([PY, f"{D}/gen_push_server.py", "--account", "1", "--src", f"{D}/server_base"], capture_output=True, text=True)
print([l for l in r.stdout.splitlines() if "pushed" in l]); sys.stdout.flush()
r = subprocess.run([PY, f"{D}/gen_wait_new_endpoint.py", f"{D}/endpoint_base.json", "2400"], capture_output=True, text=True,
                   env={**__import__("os").environ, "EXCLUDE_FILES": f"{D}/endpoint_batch.json,{D}/endpoint_batch2.json"})
print(r.stdout.strip().splitlines()[-1:] if r.stdout else r.stderr[-200:])
