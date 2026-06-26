# moonraker-bed-check

An AI "check bed" safety gate for Klipper 3D printers: a Moonraker component plus a
Klipper macro that, before the printer homes, sends a webcam frame to Claude vision
and **cancels the print if there's a mechanical hazard** - a finished part still on
the plate, or something in the toolhead's homing path. (A missing flex plate is
reported but does **not** block: it can spoil the print, not crash the machine.) Because
the trigger lives in the start gcode, it catches prints started from Mainsail,
Fluidd, the touchscreen, or any other source.

## How it works

Put `CHECK_BED` near the top of your slicer's start gcode (or `PRINT_START`), before
the first homing move. It fires the check and dwells a few seconds so the verdict can
land before anything moves:

- `CHECK_BED` sets `pending=1`, arms a `delayed_gcode` dead-man timer, fires
  `action_call_remote_method("check_bed")` (fire-and-forget - gcode can't get a
  return value back), then dwells `WINDOW` seconds (default 25).
- The component grabs a webcam frame, asks Claude vision for a verdict off
  Moonraker's reactor, and resolves it via gcode:
  - **GO** → clear `pending`, disarm the timer; the dwell finishes and the print proceeds.
  - **NO-GO** → clear `pending`, disarm the timer, `CANCEL_PRINT`.
  - **error** → fail-closed: `CANCEL_PRINT` (or proceed, if `fail_open: True`).
  - **no answer** → the `delayed_gcode` dead-man fires and `CANCEL_PRINT`s.
- The cancel lands during the dwell, before the first `G28`. Keep the timing ordered
  `request_timeout (20) < WINDOW (25) < dead-man (60)` so a hung API call still
  cancels within the dwell. On a GO, the dwell adds ~`WINDOW` seconds before the print
  proceeds.

The decision blocks only on a **mechanical hazard**: a finished/failed **part still on
the bed** (`print_left`) or a **homing hazard** (`home_hazard` - tools, clips, debris,
dangling filament or wires, anything in the toolhead's path). **Plate status is advisory
and fails open**: `plate_present` is reported, but a missing/unseated plate does **not**
cancel the print (homing may fail on the bare base, but nothing crashes). GO when no part
and no homing hazard are seen; if a hazard can't be ruled out, it's a NO-GO.

**Enable/disable** at runtime: `BED_CHECK ENABLE=0|1` in gcode (or
`POST /server/bed_check/enabled`). The flag is persisted in Moonraker's DB so a
restart won't silently re-arm it; when disabled, `CHECK_BED` skips the check and the
print proceeds.

**Console status:** `BED_CHECK_STATUS` prints the current state, model, and last
verdict (outcome, source, age) to the console - the same data as
`GET /server/bed_check/status`. (It's a separate command because Klipper can't parse a
bare `BED_CHECK STATUS`.)

**Fail-closed** by default: a check error or no response cancels the print. Set
`fail_open: True` in `[bed_check]` to proceed on error instead.

## Files

| File | Goes to | Purpose |
|------|---------|---------|
| `components/bed_check.py` | symlinked into `~/moonraker/moonraker/components/` | the Moonraker component |
| `config/bed_check.cfg` | symlinked next to `printer.cfg`, `[include]`d | Klipper macros: `CHECK_BED`, `BED_CHECK`, `BED_CHECK_STATUS`, dead-man timer |
| `config/moonraker.conf.snippet` | reference for the `[bed_check]` section | config defaults (`install.sh` writes these for you) |
| `install.sh` | run on the Pi | wires it all up; `--uninstall` reverses it |

## Install

Clone the repo to a folder under your home dir on the Pi, then run `./install.sh`:

```bash
cd ~
git clone https://github.com/nixkor/moonraker-bed-check.git
cd moonraker-bed-check
./install.sh
```

> If `./install.sh` reports `Permission denied` (some checkouts drop the executable
> bit), run `bash install.sh` instead - same effect.

`install.sh` symlinks both the component into Moonraker and `bed_check.cfg` into your
config dir (so `git pull` updates them in place), appends a default `[bed_check]`
section to `moonraker.conf`, and adds `[include bed_check.cfg]` to `printer.cfg`. It
backs up any file it edits and is safe to re-run. After a `git pull` that changes the
macro, `FIRMWARE_RESTART` Klipper to reload it.

If your paths differ from the defaults (`~/moonraker`, `~/printer_data/config`):

```bash
MOONRAKER_DIR=~/moonraker CONFIG_DIR=~/printer_data/config ./install.sh
```

Then finish the manual steps it prints:

1. **API key** - `echo 'sk-ant-...' > ~/.anthropic_api_key && chmod 600 ~/.anthropic_api_key`
   (or set `ANTHROPIC_API_KEY` in the moonraker systemd unit - avoid inlining it in `moonraker.conf`).
2. **Trigger** - add `CHECK_BED` near the top of your slicer's start gcode / `PRINT_START`,
   before the first homing move (see the placement comment in `config/bed_check.cfg`).
3. **Restart** Moonraker (`sudo systemctl restart moonraker`), then `FIRMWARE_RESTART` Klipper.

### Uninstall

```bash
./install.sh --uninstall
```

Removes both symlinks (component and macro) and the `printer.cfg` include. Your
`[bed_check]` settings in `moonraker.conf` are **not deleted** - they're commented out
and preserved under a `# >>> bed_check (UNINSTALLED …)` marker so you don't lose your
tuning; delete that block by hand when you're done with it.

## HTTP API

| Endpoint | Method | Body | Returns |
|----------|--------|------|---------|
| `/server/bed_check/check` | POST | – | `{verdict}` (dry run - checks the bed, starts nothing) |
| `/server/bed_check/status` | GET | – | `{enabled, fail_open, model, extra_prompt, last_verdict, last_verdict_time, last_verdict_time_iso, last_verdict_age_s, last_verdict_source, last_snapshot}` |
| `/server/bed_check/enabled` | POST | `{"enabled": bool}` | `{enabled}` |
| `/server/bed_check/last_snapshot` | GET | – | the most recent frame, raw `image/jpeg` |

`verdict`: `{go, plate_present, print_left, home_hazard, confident, reason}`.

Every check saves the **frame the model saw** to `snapshot_save_dir` (newest
`snapshot_keep`, default 5, timestamped `bed_*.jpg`); `GET …/last_snapshot` serves the
latest as raw JPEG, so when a verdict looks wrong you can pull exactly what it judged.

`status` also tells you **whether and when the last check fired**: `last_verdict_time`
(epoch) / `last_verdict_time_iso` (UTC) / `last_verdict_age_s` (seconds ago), and
`last_verdict_source` - `callout` (a real start-gcode trigger) or `dry_run` (a `/check`
request). A NO-GO is also broadcast as a `bed_check:verdict` notification that any
Moonraker websocket client (e.g. a dashboard) can subscribe to.

## Testing

1. After install + restart, check `~/printer_data/logs/moonraker.log` for
   `bed_check: initialized` - confirms the component loaded.
2. Dry-run the vision path without printing (run on the Pi):
   ```
   curl -X POST http://localhost:7125/server/bed_check/check
   ```
   Expect a `verdict` JSON - this exercises the snapshot fetch and the Anthropic call.
3. Toggle: `curl http://localhost:7125/server/bed_check/status`, then `BED_CHECK ENABLE=0`
   from the console and re-check that `enabled` flips and survives a Moonraker restart.
4. End-to-end: start a real print with `CHECK_BED` in the start gcode - once with a
   clear plate (expect it to proceed) and once with a flat part on the bed (expect
   `CANCEL_PRINT` during the dwell, before homing).

## Notes

- **Model:** default is `claude-opus-4-8` - the most reliable on tricky beds (it
  handles the plate's handling tab, a parked toolhead over the bed, and lens-curved
  plate edges natively). Set `model: claude-sonnet-4-6` in `[bed_check]` for lower
  cost - it's usually fine on a well-lit, upright camera but is more prone to
  false-NO-GOs on those edge cases. A Haiku model proved too weak in testing.
- **`orient_snapshot`** (default **on**) rotates/flips the frame to match the camera's
  Moonraker flip/rotation settings *before the model sees it*. This is **not cosmetic**:
  vision detection degrades badly on an upside-down/rotated bed, so an inverted camera
  must be corrected or real parts get missed. It uses Pillow (already a Moonraker
  dependency) and is a no-op when the camera reports no flip/rotation. Turn it off only
  if your feed is already oriented upstream (e.g. at the camera/streamer).
- The vision prompt and JSON schema live in `components/bed_check.py`; tune them there.
- **`extra_system_prompt`** (optional) lets you add site-specific context to the
  prompt from `moonraker.conf` *without editing the `.py`* - e.g. pointing out a
  permanent fixture in the camera's view so it isn't read as a leftover part. It's
  appended after the base prompt as clarifying context: it refines the guidance but
  cannot override the safety rules or the JSON schema. Whatever you set is echoed
  back as `extra_prompt` in `GET /server/bed_check/status`, so you can confirm
  exactly what was sent. For multi-line text, indent the continuation lines under
  the key in `moonraker.conf`.
- Requires an Anthropic API key and outbound internet from the printer host.
- **Privacy:** every check uploads a webcam frame of your bed (and whatever else is
  in view) to Anthropic's API over the internet - it does not stay on your LAN.
  Aim the camera accordingly and see Anthropic's API terms/privacy policy for how
  that data is handled.

## License

[MIT](LICENSE) © the moonraker-bed-check contributors
