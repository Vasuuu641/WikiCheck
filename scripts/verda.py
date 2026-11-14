#!/usr/bin/env python3
"""Run the embedding model (bge-m3 via Ollama) on a Verda GPU instance.

  scripts/verda.py up        create the instance (on-demand, billed per hour!), install Ollama, pull bge-m3
  scripts/verda.py tunnel    SSH tunnel localhost:11435 -> the instance's Ollama (keep it running)
  scripts/verda.py status    show the instance
  scripts/verda.py down      DELETE the instance (stops billing)

Credentials come from the repository's .env (VERDA_CLIENT_ID, VERDA_CLIENT_SECRET). State is kept in pacome/data/verda.json.
Ollama is not exposed to the internet: the app reaches it through the SSH tunnel (OLLAMA_URL=http://localhost:11435).
"""
import json, os, subprocess, sys, time
import requests

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
STATE = os.path.join(ROOT, "pacome", "data", "verda.json")   # git-ignored
KEY = os.path.expanduser("~/.ssh/verda_ed25519")
BASE = "https://api.verda.com/v1"
TYPE, LOCATION, IMAGE, HOSTNAME = "1A100.22V", "FIN-01", "24.04.cuda12.9", "wikirag-embed"
PORT = 11435

for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    if "=" in line and not line.startswith("#"):
        k, v = line.strip().split("=", 1)
        os.environ.setdefault(k, v.strip("\"'"))


def api(method, path, **kw):
    r = requests.post(BASE + "/oauth2/token", json=dict(grant_type="client_credentials",
                      client_id=os.environ["VERDA_CLIENT_ID"], client_secret=os.environ["VERDA_CLIENT_SECRET"]))
    r.raise_for_status()
    r = requests.request(method, BASE + path, headers={"Authorization": "Bearer " + r.json()["access_token"]}, timeout=60, **kw)
    if r.status_code >= 300:
        sys.exit(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
    if not r.text:
        return None
    try:
        return r.json()
    except ValueError:  # some endpoints return a bare id, not JSON
        return r.text.strip().strip('"')


def state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}


def save(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(d, open(STATE, "w"))


def ssh(ip, cmd, **kw):
    return subprocess.run(["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=accept-new", "-o", "IdentitiesOnly=yes",
                           "-o", "ConnectTimeout=10", f"root@{ip}", cmd], **kw)


def up():
    st = state()
    if st.get("id"):
        sys.exit(f"Instance already recorded ({st['id']}). Use status / down.")
    keys = api("GET", "/sshkeys")
    key = next((k for k in keys if k["name"] == "wikirag"), None)
    if not key:
        pub = open(KEY + ".pub").read().strip()
        key = api("POST", "/sshkeys", json={"name": "wikirag", "key": pub})
        key = {"id": key} if isinstance(key, str) else key
    key_id = key["id"] if isinstance(key, dict) else key
    iid = api("POST", "/instances", json=dict(hostname=HOSTNAME, instance_type=TYPE, image=IMAGE, ssh_key_ids=[key_id],
              location_code=LOCATION, contract="PAY_AS_YOU_GO", is_spot=False, os_volume=dict(name=HOSTNAME + "-os", size=100)))
    iid = iid["id"] if isinstance(iid, dict) else iid
    save(dict(id=iid))
    print("Created instance", iid, "- waiting for it to run...")
    ip = None
    for _ in range(90):
        i = api("GET", f"/instances/{iid}")
        if i["status"] == "running" and i.get("ip"):
            ip = i["ip"]
            break
        time.sleep(10)
    if not ip:
        sys.exit("Instance did not become ready in 15 min. Check status; run 'down' if you do not want it.")
    save(dict(id=iid, ip=ip))
    print("Running at", ip, "- waiting for SSH...")
    for _ in range(30):
        if ssh(ip, "true", capture_output=True).returncode == 0:
            break
        time.sleep(10)
    print("Installing Ollama and pulling bge-m3...")
    script = ("set -e; command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh; "
              "systemctl enable --now ollama; for i in $(seq 30); do curl -s localhost:11434 >/dev/null && break; sleep 2; done; "
              "ollama pull bge-m3; nvidia-smi -L")
    if ssh(ip, script).returncode:
        sys.exit("Remote setup failed (instance is still running and billing).")
    print("Ready. Next: scripts/verda.py tunnel   and   OLLAMA_URL=http://localhost:%d in .env" % PORT)


def status():
    st = state()
    if not st.get("id"):
        return print("No instance recorded.")
    i = api("GET", f"/instances/{st['id']}")
    print(i["id"], i["status"], i.get("ip"), i["instance_type"], i["location"])


def tunnel():
    ip = state().get("ip") or sys.exit("No running instance. Run 'up' first.")
    print(f"Tunnel localhost:{PORT} -> {ip}:11434 (Ctrl-C to stop)")
    os.execvp("ssh", ["ssh", "-N", "-i", KEY, "-o", "IdentitiesOnly=yes", "-o", "ServerAliveInterval=15",
                      "-o", "ExitOnForwardFailure=yes", "-L", f"{PORT}:localhost:11434", f"root@{ip}"])


def down():
    st = state()
    if not st.get("id"):
        return print("No instance recorded.")
    i = api("GET", f"/instances/{st['id']}")
    body = dict(action="delete", id=st["id"])
    if i.get("os_volume_id"):  # remove the OS disk too so nothing keeps billing
        body.update(volume_ids=[i["os_volume_id"]], delete_permanently=True)
    api("PUT", "/instances", json=body)
    os.remove(STATE)
    print("Deleted", st["id"], "- billing stopped.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"up": up, "status": status, "tunnel": tunnel, "down": down}.get(cmd, lambda: sys.exit(__doc__))()
