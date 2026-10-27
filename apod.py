"""APOD lock screen updater.

Usage:
  apod                      Update now, install the daily task if missing, show status
  apod run                  Download today's APOD and set it as the lock screen
  apod status               Show last run, current image, lock screen and task status
  apod install [--time HH:MM]  Create/replace the daily scheduled task (needs admin)
  apod uninstall            Remove the scheduled task (needs admin)
  apod log [-n N]           Show the last N lines of the log

Global option (put it before the command):
  --save-dir PATH           Where images, log and state are kept
"""
import argparse
import csv
import ctypes
import io
import json
import logging
import os
import random
import subprocess
import sys
import tempfile
import time
import winreg
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import requests

try:
    import tzdata  # noqa: F401  (makes PyInstaller bundle the time zone data)
    from zoneinfo import ZoneInfo

    EASTERN = ZoneInfo("America/New_York")
except Exception:
    EASTERN = timezone(timedelta(hours=-5), "EST")

# ===== CONFIG =====
APOD_API = "https://science.nasa.gov/wp-json/wp/v2/apod-basic"  # no API key needed
DEFAULT_SAVE_DIR = Path.home() / "Pictures" / "APOD"
TASK_NAME = "APOD Lock Screen"
REG_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\PersonalizationCSP"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp")
RETRY_MINUTES = 10   # scheduled runs wait this long if NASA hasn't posted yet
MAX_RETRIES = 12     # ...up to this many times (2 hours)
# ==================

log = logging.getLogger("apod")


# ---------- helpers ----------
def setup_logging(save_dir: Path) -> None:
    save_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(save_dir / "apod.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)


def state_file(save_dir: Path) -> Path:
    return save_dir / "apod_state.json"


def load_state(save_dir: Path) -> dict:
    try:
        return json.loads(state_file(save_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(save_dir: Path, **updates) -> None:
    state = load_state(save_dir)
    state.update(updates)
    state_file(save_dir).write_text(json.dumps(state, indent=2), encoding="utf-8")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def today_eastern() -> str:
    """APOD's date rolls over at midnight US Eastern time."""
    return datetime.now(EASTERN).date().isoformat()


def hhmm(value: str) -> str:
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise argparse.ArgumentTypeError("time must look like 22:15")


# ---------- APOD ----------
def _normalize(item: dict) -> dict:
    """Flatten an apod-basic entry; fields may sit at the top level or under "meta"."""
    if isinstance(item.get("meta"), dict):
        item = {**item, **item["meta"]}
    title = item.get("title")
    if isinstance(title, dict):              # WordPress style {"rendered": "..."}
        item["title"] = title.get("rendered", "")
    return item


def fetch_feed(per_page: int = 5) -> list[dict]:
    """Return recent APOD posts from NASA's apod-basic feed (max 25 per page)."""
    r = requests.get(APOD_API, params={"page": 1, "per_page": per_page}, timeout=30,
                     headers={"User-Agent": "apod-lockscreen/1.0"})
    r.raise_for_status()
    body = r.json()
    items = body if isinstance(body, list) else body.get("items") or body.get("data") or [body]
    items = [_normalize(i) for i in items if isinstance(i, dict) and i.get("date")]
    if not items:
        raise ValueError(f"Unexpected response from {APOD_API}")
    return items


def fetch_apod() -> dict:
    """Return the newest published APOD."""
    return max(fetch_feed(), key=lambda i: i["date"])


def past_images(save_dir: Path) -> list[Path]:
    """APOD images already downloaded to the save folder."""
    return [p for p in save_dir.glob("apod_????-??-??.*")
            if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 0]


def pick_random_past(save_dir: Path, exclude: str | None) -> tuple[Path, str]:
    """Pick a random earlier image: from disk if possible, otherwise from the feed.

    Returns (path, apod_date). Avoids repeating the image currently shown.
    """
    local = past_images(save_dir)
    if exclude and len(local) > 1:
        local = [p for p in local if str(p) != exclude]
    if local:
        choice = random.choice(local)
        return choice, choice.stem.removeprefix("apod_")

    log.info("No saved images yet; picking a random recent image from NASA's feed.")
    posts = [p for p in fetch_feed(per_page=25) if p.get("media_type") == "image"]
    random.shuffle(posts)
    for post in posts:
        try:
            return download_image(post, save_dir), post["date"]
        except (requests.RequestException, OSError, ValueError) as e:
            log.warning("Skipping %s: %s", post.get("date"), e)
    raise ValueError("no usable past images found in the feed")


def get_apod(scheduled: bool) -> dict | None:
    """Fetch the APOD. Scheduled runs wait for today's entry to be posted."""
    attempts = MAX_RETRIES if scheduled else 1
    data = None
    for attempt in range(1, attempts + 1):
        try:
            data = fetch_apod()
        except (requests.RequestException, ValueError) as e:
            log.warning("APOD API request failed: %s", e)
        else:
            if not scheduled or data.get("date", "") >= today_eastern():
                return data
            log.info("APOD still shows %s; today's entry isn't posted yet.", data.get("date"))
        if attempt < attempts:
            log.info("Retrying in %d minutes (%d/%d)...", RETRY_MINUTES, attempt, attempts)
            time.sleep(RETRY_MINUTES * 60)
    if data:
        log.warning("Gave up waiting; using the most recent APOD (%s).", data.get("date"))
    return data


def download_image(data: dict, save_dir: Path) -> Path:
    # In the new feed "url" is the post's web page, so only "hdurl" points at the image.
    image_url = data.get("hdurl")
    if not image_url:
        raise ValueError(f"APOD {data.get('date')} has no image URL (hdurl is empty)")
    ext = os.path.splitext(urlparse(image_url).path)[1].lower()
    if ext not in IMAGE_EXTS:
        raise ValueError(f"Unsupported image type: {image_url}")
    path = save_dir / f"apod_{data['date']}{ext}"
    if path.exists() and path.stat().st_size > 0:
        log.info("Already downloaded: %s", path.name)
        return path

    # hdurl carries resize parameters (?w=...&h=...); some posts have w=0&h=0.
    # Try it as given first, then the bare image URL without those parameters.
    candidates = [image_url]
    bare = urlparse(image_url)._replace(query="").geturl()
    if bare != image_url:
        candidates.append(bare)

    tmp = path.with_suffix(path.suffix + ".part")
    last_error = None
    for url in candidates:
        log.info("Downloading %s", url)
        try:
            with requests.get(url, timeout=60, stream=True) as r:
                r.raise_for_status()
                if not r.headers.get("Content-Type", "").startswith("image/"):
                    raise ValueError(f"not an image (Content-Type: {r.headers.get('Content-Type')})")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
            if tmp.stat().st_size == 0:
                raise ValueError("empty file")
            tmp.replace(path)
            return path
        except (requests.RequestException, ValueError) as e:
            log.warning("  failed: %s", e)
            last_error = e
    tmp.unlink(missing_ok=True)
    raise ValueError(f"Could not download the image: {last_error}")


# ---------- lock screen ----------
def set_lock_screen(image_path: Path) -> None:
    """Raises PermissionError when not running as Administrator."""
    access = winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, REG_PATH, 0, access) as key:
        winreg.SetValueEx(key, "LockScreenImageStatus", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "LockScreenImagePath", 0, winreg.REG_SZ, str(image_path))
        winreg.SetValueEx(key, "LockScreenImageUrl", 0, winreg.REG_SZ, str(image_path))


def read_lock_screen() -> str | None:
    try:
        access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_PATH, 0, access) as key:
            return winreg.QueryValueEx(key, "LockScreenImagePath")[0]
    except OSError:
        return None


# ---------- scheduled task ----------
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Downloads NASA's Astronomy Picture of the Day and sets it as the lock screen.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def task_command(save_dir: Path) -> tuple[str, str]:
    args = f'--save-dir "{save_dir}" run --scheduled'
    if getattr(sys, "frozen", False):          # running as the PyInstaller exe
        return sys.executable, args
    return sys.executable, f'"{Path(__file__).resolve()}" {args}'


def default_task_time() -> str:
    """10 minutes after midnight US Eastern, converted to local time."""
    nxt = datetime.now(EASTERN) + timedelta(days=1)
    target = nxt.replace(hour=0, minute=10, second=0, microsecond=0)
    return target.astimezone().strftime("%H:%M")


def run_quiet(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


def task_info() -> dict | None:
    r = run_quiet(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "CSV"])
    if r.returncode != 0:
        return None
    rows = list(csv.DictReader(io.StringIO(r.stdout)))
    return rows[0] if rows else {}


# ---------- commands ----------
def cmd_run(save_dir: Path, scheduled: bool = False) -> int:
    log.info("=== APOD update started%s ===", " (scheduled)" if scheduled else "")
    data = get_apod(scheduled)
    if data is None:
        save_state(save_dir, last_run=now_str(), last_result="error: couldn't reach NASA API")
        return 1

    date, title = data.get("date", "?"), data.get("title", "")
    log.info("APOD %s: %s", date, title)

    if data.get("media_type") != "image":
        log.info("Today's APOD is a %s, not an image; showing a random past image instead.",
                 data.get("media_type"))
        try:
            path, shown_date = pick_random_past(save_dir, load_state(save_dir).get("image_path"))
        except (requests.RequestException, OSError, ValueError) as e:
            log.error("Couldn't find a past image: %s; keeping the current lock screen.", e)
            save_state(save_dir, last_run=now_str(), last_result=f"error: {e}",
                       apod_date=date, apod_title=title)
            return 1
        log.info("Picked APOD from %s.", shown_date)
        return apply_image(save_dir, path, date, title,
                           f"video day: showing random APOD from {shown_date}")

    try:
        path = download_image(data, save_dir)
    except (requests.RequestException, OSError, ValueError) as e:
        log.error("Download failed: %s", e)
        save_state(save_dir, last_run=now_str(), last_result=f"error: {e}")
        return 1

    return apply_image(save_dir, path, date, title, "success")


def apply_image(save_dir: Path, path: Path, date: str, title: str, result: str) -> int:
    """Set the lock screen to `path` and record the outcome."""
    try:
        set_lock_screen(path)
    except PermissionError:
        log.warning("Image saved, but setting the lock screen needs Administrator. "
                    "Run from an admin terminal (the scheduled task already runs elevated).")
        save_state(save_dir, last_run=now_str(),
                   last_result="partial: lock screen not set (not admin)",
                   apod_date=date, apod_title=title, image_path=str(path))
        return 1

    log.info("Lock screen set to %s", path)
    save_state(save_dir, last_run=now_str(), last_result=result,
               apod_date=date, apod_title=title, image_path=str(path))
    return 0


def cmd_install(save_dir: Path, at_time: str | None = None) -> int:
    if not is_admin():
        log.error("Installing the scheduled task needs Administrator. "
                  "Open a terminal with 'Run as administrator' and try again.")
        return 1

    at_time = at_time or default_task_time()
    hh, mm = map(int, at_time.split(":"))
    start = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    command, arguments = task_command(save_dir)
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME", "")
    user = f"{domain}\\{os.environ['USERNAME']}"

    xml = TASK_XML.format(start=start.strftime("%Y-%m-%dT%H:%M:%S"), user=escape(user),
                          command=escape(command), arguments=escape(arguments))
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
        f.write(xml)
        xml_path = f.name
    try:
        r = run_quiet(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
    finally:
        os.unlink(xml_path)

    if r.returncode != 0:
        log.error("Could not create the scheduled task: %s", (r.stderr or r.stdout).strip())
        return 1
    log.info("Scheduled task '%s' installed: daily at %s local time "
             "(catches up after sleep/shutdown).", TASK_NAME, at_time)
    log.info("Task runs: %s %s", command, arguments)
    save_state(save_dir, task_time=at_time)
    return 0


def cmd_uninstall(save_dir: Path) -> int:
    if not is_admin():
        log.error("Removing the scheduled task needs Administrator.")
        return 1
    r = run_quiet(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if r.returncode != 0:
        log.error("Could not remove the task: %s", (r.stderr or r.stdout).strip())
        return 1
    log.info("Scheduled task '%s' removed.", TASK_NAME)
    return 0


TASK_RESULTS = {"0": "0 (success)", "1": "1 (error - see `apod log`)",
                "267009": "currently running", "267011": "hasn't run yet"}


def cmd_status(save_dir: Path) -> int:
    def row(label, value):
        print(f"  {label:<18} {value}")

    state = load_state(save_dir)
    admin = is_admin()
    print("APOD Lock Screen - status")
    print("  " + "-" * 50)
    row("Save folder", save_dir)
    row("Admin", "yes" if admin else "no")
    row("Last run", f"{state.get('last_run', 'never')}  ({state.get('last_result', '-')})")
    if state.get("apod_date"):
        row("Latest APOD", f"{state['apod_date']}  {state.get('apod_title', '')}")

    image = state.get("image_path")
    if image:
        row("Current image", image + ("" if Path(image).exists() else "  (file missing!)"))
    else:
        row("Current image", "-")

    lock = read_lock_screen()
    if not lock:
        row("Lock screen", "not set by this app")
    elif image and Path(lock) == Path(image):
        row("Lock screen", "showing current image")
    else:
        row("Lock screen", f"set to {lock}")

    task = task_info()
    if task is None:
        row("Scheduled task", "not installed" + ("" if admin else " (or hidden without admin)"))
    else:
        row("Scheduled task", task.get("Status", "installed"))
        row("  Next run", task.get("Next Run Time", "?"))
        row("  Last run", task.get("Last Run Time", "?"))
        last = task.get("Last Result", "?")
        row("  Last result", TASK_RESULTS.get(last, last))
    return 0


def cmd_log(save_dir: Path, lines: int) -> int:
    log_path = save_dir / "apod.log"
    if not log_path.exists():
        print("No log yet.")
        return 0
    text = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(text[-lines:]))
    return 0


# ---------- entry point ----------
def main() -> int:
    parser = argparse.ArgumentParser(prog="apod",
                                     description="Set NASA's Astronomy Picture of the Day as your lock screen.")
    parser.add_argument("--save-dir", type=Path, default=DEFAULT_SAVE_DIR,
                        help="folder for images, log and state")
    sub = parser.add_subparsers(dest="command")
    p_run = sub.add_parser("run", help="download today's APOD and set the lock screen")
    p_run.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("status", help="show what the app has done and the task status")
    p_inst = sub.add_parser("install", help="create the daily scheduled task (admin)")
    p_inst.add_argument("--time", type=hhmm, help="local time to run, HH:MM "
                        f"(default {default_task_time()}, just after APOD updates)")
    sub.add_parser("uninstall", help="remove the scheduled task (admin)")
    p_log = sub.add_parser("log", help="show recent log lines")
    p_log.add_argument("-n", type=int, default=20, help="number of lines (default 20)")
    args = parser.parse_args()

    save_dir = args.save_dir.expanduser().resolve()
    setup_logging(save_dir)

    if args.command == "run":
        return cmd_run(save_dir, args.scheduled)
    if args.command == "status":
        return cmd_status(save_dir)
    if args.command == "install":
        return cmd_install(save_dir, args.time)
    if args.command == "uninstall":
        return cmd_uninstall(save_dir)
    if args.command == "log":
        return cmd_log(save_dir, args.n)

    # No command (e.g. double-clicked): update, make sure the task exists, show status.
    code = cmd_run(save_dir)
    if task_info() is None:
        if is_admin():
            cmd_install(save_dir)
        else:
            log.info("Tip: run once as Administrator to install the daily scheduled task.")
    print()
    cmd_status(save_dir)
    try:
        input("\nPress Enter to close...")
    except (EOFError, RuntimeError):
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())