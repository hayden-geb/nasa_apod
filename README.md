# APOD Lock Screen

Sets NASA's [Astronomy Picture of the Day](https://science.nasa.gov/apod/) as your Windows lock screen, automatically, every day.

- Downloads the newest APOD from NASA's public `apod-basic` feed (no API key needed)
- Sets it as the lock screen through the Windows `PersonalizationCSP` registry key
- Installs a daily Windows scheduled task that runs just after NASA posts the new picture
- On video days, shows a random past APOD instead
- Small command-line interface for checking status and logs

## Requirements

- Windows 10 or 11
- Administrator rights (the lock screen setting lives in `HKEY_LOCAL_MACHINE`)
- To build from source: [uv](https://docs.astral.sh/uv/) and Python 3.10+

> **Note:** `PersonalizationCSP` is officially supported on Windows Enterprise and Education. It usually works on Home and Pro too, but Settings may then show the lock screen as "managed by your organization." See [Uninstalling](#uninstalling) to undo it.

## Quick start (prebuilt exe)

1. Download `apod.exe` from the [Releases](../../releases) page.
2. Put it somewhere permanent, e.g. `C:\Tools\apod.exe`. The scheduled task points to this exact path.
3. Right-click it → **Run as administrator**.

That first run downloads today's picture, sets the lock screen, installs the daily task and shows the status.

## Building from source

```bash
git clone https://github.com/<your-username>/nasa_apod.git
cd nasa_apod
uv sync
uv run pyinstaller --onefile --name apod --collect-data tzdata apod.py
```

The exe is written to `dist\apod.exe`.

You can also run the script without building it: `uv run python apod.py status`.

## Usage

Open a terminal **as administrator** for every command except `status` and `log`.

| Command | What it does |
|---|---|
| `apod` | Update now, install the daily task if it's missing, show status (same as double-clicking) |
| `apod run` | Download today's APOD and set the lock screen |
| `apod status` | Show the last run, current image, lock screen and scheduled task |
| `apod install [--time HH:MM]` | Create or replace the daily scheduled task |
| `apod uninstall` | Remove the scheduled task |
| `apod log [-n 50]` | Show recent log lines |

`--save-dir PATH` changes where images are stored. It goes *before* the command: `apod --save-dir D:\APOD run`.

If `apod` isn't recognized, either use the full path to the exe or copy it to a folder on your `PATH`.

## How it works

**Schedule.** NASA posts the new APOD at midnight US Eastern time. By default the task runs 10 minutes after that, converted to your local time (for example 22:10 in Mountain time). If the computer is asleep or off at that time, Windows runs the task as soon as it's back. If NASA hasn't posted yet, the script retries every 10 minutes for up to 2 hours.

**Scheduled task.** The task is named `APOD Lock Screen`. It runs elevated and in the background, so no console window appears. You can see it in Task Scheduler.

**Video days.** Some APODs are videos. On those days the script shows a random image from your saved folder (never the one currently showing). If nothing is saved yet, it picks a random image from NASA's 25 most recent posts.

**Files.** Everything goes in `%USERPROFILE%\Pictures\APOD` by default:

| File | Contents |
|---|---|
| `apod_YYYY-MM-DD.jpg` / `.png` | Downloaded images |
| `apod.log` | Log of every run |
| `apod_state.json` | Last run result and current image, used by `apod status` |

## Uninstalling

From an admin terminal:

```powershell
apod uninstall
reg delete "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\PersonalizationCSP" /f
```

The second command gives lock screen control back to Windows Settings. Then delete the exe and, if you like, the `Pictures\APOD` folder.

## Troubleshooting

| Problem | Fix |
|---|---|
| "needs Administrator" | Run the terminal with **Run as administrator**. |
| Lock screen doesn't change | Run `apod status` and `apod log`. Lock the screen (Win+L) to check, as Windows can take a moment to pick up the new image. |
| Task's last result isn't `0` | Run `apod log` to see what failed. |
| `PermissionError` when rebuilding | The exe is still running. Run `schtasks /End /TN "APOD Lock Screen"` and `taskkill /IM apod.exe /F` as admin, then rebuild. |
| `apod` is not recognized | Use the full path to the exe, or put it in a folder on your `PATH`. |

## Credits

Images and text come from NASA's [Astronomy Picture of the Day](https://science.nasa.gov/apod/). Many APOD images are copyrighted by their photographers (see each post's `copyright` field). This tool only downloads them for personal use as a lock screen; don't redistribute the images.
