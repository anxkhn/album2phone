"""Download an iCloud shared album straight onto an Android phone over ADB.

You sign in to iCloud once in a Chrome window on your computer. The computer
only lists the album and hands each signed download link to the phone, which
fetches the original file itself with its built-in curl. Nothing is stored on
the computer.
"""

import argparse
import base64
import hashlib
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]
# Stop starting downloads this long before Apple's signed links expire.
EXPIRY_MARGIN = 120
# Start listing fresh links this long before that, while downloads keep running.
REFRESH_LEAD = 180
# Only the fields plan() reads; the full records are about twice as slow to list.
FIELDS = [
    "filenameEnc",
    "resOriginalRes",
    "resOriginalVidComplRes",
    "originalCreationDate",
    "resOriginalWidth",
    "resOriginalHeight",
    "masterRef",
    "assetDate",
    "isDeleted",
]
LIVE_FOLDER = "Live Photo clips"
EXPIRED = "expired"

# Runs inside the signed-in iCloud tab, so the browser's own cookies are used.
JS_OPEN_ALBUM = """async (token) => {
  const v = await (await fetch("https://setup.icloud.com/setup/ws/1/validate",
    {method: "POST", credentials: "include", body: "null"})).json();
  if (!v.dsInfo) return null;
  const base = v.webservices.ckdatabasews.url + "/database/1/com.apple.photos.cloud/production/";
  const q = "?remapEnums=true&dsid=" + v.dsInfo.dsid;
  const res = await (await fetch(base + "public/records/accept" + q, {method: "POST",
    credentials: "include", headers: {"Content-Type": "text/plain"},
    body: JSON.stringify({shortGUIDs: [{value: token}]})})).json();
  const r = res.results[0];
  if (!r.zoneID) throw new Error("iCloud refused the album link: " + JSON.stringify(r));
  const title = r.share && r.share.fields["cloudkit.title"];
  return {url: base + r.databaseScope.toLowerCase() + "/changes/zone" + q,
          zone: r.zoneID, title: title ? title.value : token};
}"""

JS_LIST_RECORDS = """async ({url, zone, fields}) => {
  let syncToken, records = [];
  while (true) {
    const z = {zoneID: zone, reverse: false, desiredKeys: fields};
    if (syncToken) z.syncToken = syncToken;
    const res = await (await fetch(url, {method: "POST", credentials: "include",
      headers: {"Content-Type": "text/plain"}, body: JSON.stringify({zones: [z]})})).json();
    const page = res.zones ? res.zones[0] : res;
    if (!page.syncToken) throw new Error("iCloud listing failed: " + JSON.stringify(page));
    records.push(...(page.records || []));
    syncToken = page.syncToken;
    if (!page.moreComing) return records;
  }
}"""


def album_token(link):
    m = re.search(r"(?:/album/|sc,)([\w-]+)", link)
    return m.group(1) if m else link.strip("/")


def field(record, name, default=None):
    return record["fields"].get(name, {}).get("value", default)


def links_deadline(files):
    """Apple's download links carry their expiry (about 15 minutes out) in the e= parameter."""
    expiries = [int(m.group(1)) for f in files if (m := re.search(r"[?&]e=(\d+)", f["url"]))]
    return (min(expiries) if expiries else time.time() + 600) - EXPIRY_MARGIN


def is_duplicate(size, created, kept):
    # Re-uploads of one file keep its name and size; a re-saved photo keeps its
    # capture time but can gain a few bytes of metadata.
    for k_size, k_created in kept:
        if size == k_size or (created == k_created and abs(size - k_size) <= size // 1000):
            return True
    return False


def plan(records, dest, live=True, dedupe=True):
    """Turn iCloud records into a list of files to download, plus a dup count."""
    masters = {r["recordName"]: r for r in records if r.get("recordType") == "CPLMaster"}
    taken = {}
    for a in records:
        if a.get("recordType") == "CPLAsset" and not field(a, "isDeleted"):
            ref = field(a, "masterRef")["recordName"]
            taken.setdefault(ref, field(a, "assetDate"))

    files, used, seen, dups = [], set(), {}, 0
    # Same capture time: bigger file first, so dedupe keeps the most complete copy.
    order = sorted(
        taken,
        key=lambda k: (
            field(masters[k], "originalCreationDate", 0),
            -field(masters[k], "resOriginalRes")["size"],
            k,
        ),
    )
    for rn in order:
        m = masters[rn]
        name = base64.b64decode(field(m, "filenameEnc")).decode()
        orig = field(m, "resOriginalRes")
        created = field(m, "originalCreationDate", 0)
        if dedupe:
            key = (name.lower(), field(m, "resOriginalWidth"), field(m, "resOriginalHeight"))
            if is_duplicate(orig["size"], created, seen.get(key, [])):
                dups += 1
                continue
            seen.setdefault(key, []).append((orig["size"], created))

        stem, ext = os.path.splitext(name)
        if name.lower() in used:
            stem += "_" + hashlib.sha1(rn.encode()).hexdigest()[:6]
            name = stem + ext
        used.add(name.lower())
        epoch = (taken[rn] or created) // 1000
        files.append(
            {
                "path": f"{dest}/{name}",
                "url": orig["downloadURL"].replace("${f}", name),
                "size": orig["size"],
                "epoch": epoch,
            }
        )
        clip = field(m, "resOriginalVidComplRes")
        if live and clip:
            files.append(
                {
                    "path": f"{dest}/{LIVE_FOLDER}/{stem}.MOV",
                    "url": clip["downloadURL"].replace("${f}", stem + ".MOV"),
                    "size": clip["size"],
                    "epoch": epoch,
                }
            )
    return files, dups


class Phone:
    def __init__(self, serial):
        self.serial = serial or self._only_device()

    @staticmethod
    def _only_device():
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=True).stdout
        ready = [line.split()[0] for line in out.splitlines()[1:] if line.endswith("\tdevice")]
        if len(ready) != 1:
            sys.exit(f"Found {len(ready)} ready ADB devices {ready}. Pass one with --serial.")
        return ready[0]

    def sh(self, cmd, timeout=None):
        return subprocess.run(
            ["adb", "-s", self.serial, "shell", cmd],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def check(self, dest):
        # shortcut: needs curl on the phone (Android ships it in recent releases);
        # add an adb push fallback if older phones turn up.
        if self.sh("command -v curl").returncode != 0:
            sys.exit("This phone has no curl. album2phone needs a phone with /system/bin/curl.")
        q = shlex.quote(dest)
        if self.sh(f"mkdir -p {q} {shlex.quote(dest + '/' + LIVE_FOLDER)}").returncode != 0:
            sys.exit(f"Cannot create {dest} on the phone.")

    def sizes(self, dest):
        out = self.sh(f"find {shlex.quote(dest)} -type f ! -name '.*' -exec stat -c '%s|%n' {{}} +").stdout
        return {n: int(s) for s, _, n in (line.partition("|") for line in out.splitlines()) if s.isdigit()}

    def fetch(self, f):
        """Download one file on the phone. Only a complete file gets its real name."""
        path = shlex.quote(f["path"])
        part = shlex.quote(
            os.path.join(os.path.dirname(f["path"]), "." + os.path.basename(f["path"]) + ".part")
        )
        cmd = (
            f"curl -sS --fail --retry 3 --retry-delay 5 -o {part} {shlex.quote(f['url'])}"
            f" && mv -f {part} {path} && touch -d @{f['epoch']} {path} && stat -c %s {path}"
        )
        try:
            r = self.sh(cmd, timeout=3600)
        except subprocess.TimeoutExpired:
            r = None
        if r and r.returncode == 0 and r.stdout.strip() == str(f["size"]):
            return None
        self.sh(f"rm -f {part}")
        return r.stderr.strip()[:200] if r else "timed out"


def port_open(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_chrome(args, link):
    if port_open(args.port):
        return
    chrome = args.chrome or next((p for p in CHROME_PATHS if shutil.which(p) or os.path.exists(p)), None)
    if not chrome:
        sys.exit("Chrome not found. Pass its path with --chrome.")
    subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={args.port}",
            f"--user-data-dir={args.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            link,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(60):
        if port_open(args.port):
            return
        time.sleep(0.5)
    sys.exit("Chrome did not open its debugging port.")


def icloud_page(browser, link):
    pages = [p for c in browser.contexts for p in c.pages if "icloud.com" in p.url]
    if pages:
        return pages[0]
    page = browser.contexts[0].new_page()
    page.goto(link)
    return page


def wait_for_sign_in(browser, link, token):
    print("Sign in to iCloud in the Chrome window. Waiting...", flush=True)
    while True:
        try:
            album = icloud_page(browser, link).evaluate(JS_OPEN_ALBUM, token)
            if album:
                return album
        except PlaywrightError as e:
            if "refused" in str(e):
                sys.exit(str(e))
        time.sleep(3)


def human(n):
    return f"{n / 1e9:.2f} GB"


def run(args):
    token = album_token(args.album)
    link = f"https://photos.icloud.com/shared/album/{token}"
    start_chrome(args, link)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        album = wait_for_sign_in(browser, link, token)
        dest = args.dest or f"/sdcard/DCIM/{album['title']}"
        phone = None if args.dry_run else Phone(args.serial)
        if phone:
            phone.check(dest)

        def list_files():
            records = icloud_page(browser, link).evaluate(JS_LIST_RECORDS, {**album, "fields": FIELDS})
            return plan(records, dest, live=not args.no_live, dedupe=not args.keep_duplicates)

        failed_last = None
        while True:
            files, dups = list_files()
            total = sum(f["size"] for f in files)
            print(
                f"{album['title']}: {len(files)} files, {human(total)}, {dups} duplicates skipped -> {dest}"
            )
            if args.dry_run:
                return 0

            have = phone.sizes(dest)
            todo = [f for f in files if have.get(f["path"]) != f["size"]]
            if not todo:
                print("All files are on the phone.")
                return 0

            done_n, done_b = len(files) - len(todo), total - sum(f["size"] for f in todo)
            start_b, t0, failed = done_b, time.time(), []
            # Workers read the newest link and deadline, so a refresh reaches queued files.
            links = {f["path"]: f["url"] for f in files}
            state = {"deadline": links_deadline(files)}

            def job(f, links=links, state=state):
                if time.time() > state["deadline"]:
                    return EXPIRED
                return phone.fetch({**f, "url": links[f["path"]]})

            with ThreadPoolExecutor(args.workers) as pool:
                jobs = {pool.submit(job, f): f for f in todo}
                while jobs:
                    finished, _ = wait(jobs, timeout=2, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        f, err = jobs.pop(fut), fut.result()
                        if err is EXPIRED:
                            continue  # retried next round with fresh links
                        if err:
                            failed.append(f["path"])
                            print(f"\n{os.path.basename(f['path'])}: {err}")
                            continue
                        done_n, done_b = done_n + 1, done_b + f["size"]
                        rate = (done_b - start_b) / max(time.time() - t0, 1)
                        eta = (total - done_b) / rate if rate else 0
                        print(
                            f"\r{done_n}/{len(files)} files  {human(done_b)} / {human(total)}  "
                            f"{rate / 1e6:.1f} MB/s  ETA {int(eta // 3600)}h {int(eta % 3600 // 60):02d}m ",
                            end="",
                            flush=True,
                        )
                    if jobs and time.time() > state["deadline"] - REFRESH_LEAD:
                        try:
                            fresh, _ = list_files()
                        except PlaywrightError as e:
                            print(f"\nCould not refresh links, retrying: {str(e)[:200]}")
                            continue
                        links.update({f["path"]: f["url"] for f in fresh})
                        state["deadline"] = links_deadline(fresh)
            print()
            if failed and failed == failed_last:
                print(f"{len(failed)} files keep failing. Run again later to retry them.")
                return 1
            failed_last = failed


def main():
    p = argparse.ArgumentParser(prog="album2phone", description=__doc__.splitlines()[0])
    p.add_argument("album", help="shared album link, e.g. https://photos.icloud.com/shared/album/<id>")
    p.add_argument("--serial", help="ADB serial of the phone (default: the only connected device)")
    p.add_argument("--dest", help='folder on the phone (default: "/sdcard/DCIM/<album name>")')
    p.add_argument("--workers", type=int, default=4, help="parallel downloads on the phone (default: 4)")
    p.add_argument("--no-live", action="store_true", help="skip the video part of Live Photos")
    p.add_argument("--keep-duplicates", action="store_true", help="download repeated uploads too")
    p.add_argument("--dry-run", action="store_true", help="list what would download, then stop")
    p.add_argument("--port", type=int, default=9333, help="Chrome remote debugging port (default: 9333)")
    p.add_argument(
        "--profile",
        default=os.path.expanduser("~/.album2phone/chrome"),
        help="Chrome profile that keeps your iCloud sign-in (default: ~/.album2phone/chrome)",
    )
    p.add_argument("--chrome", help="path to Chrome or Chromium if it is not found automatically")
    try:
        sys.exit(run(p.parse_args()))
    except KeyboardInterrupt:
        sys.exit("\nStopped. Run the same command again to resume.")


if __name__ == "__main__":
    main()
