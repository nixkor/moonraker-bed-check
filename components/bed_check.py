# bed_check.py — Moonraker component: AI "check bed" gate for print starts.
#
# Design (see memory: moonraker-ai-bed-check):
#   - Trigger: a CHECK_BED callout from the slicer start gcode / PRINT_START,
#     placed before the first homing move (see bed_check.cfg). The macro fires
#     action_call_remote_method("check_bed") and dwells for WINDOW seconds; this
#     component grabs a webcam frame, asks Claude vision for a go/no-go, and on a
#     NO-GO / error (fail-closed) issues CANCEL_PRINT before the toolhead moves.
#     Because every print runs the start gcode, this catches prints started from
#     Mainsail, Fluidd, the screen, and the MCP alike.
#   - Blocks only on a MECHANICAL hazard: a part left on the bed (print_left) or
#     a homing-path obstruction (home_hazard). plate_present is ADVISORY and
#     fails open -- a missing/unseated plate is reported but does NOT cancel
#     (homing may fail on the bare base, but nothing crashes).
#   - Enable/disable at runtime from gcode (BED_CHECK ENABLE=0|1 ->
#     action_call_remote_method "set_bed_check"), persisted in Moonraker's DB so
#     a restart doesn't silently re-arm. Disabled state is announced.
#   - Fail-closed by default: if the AI call errors or times out, the print is
#     cancelled (set fail_open: True in [bed_check] to proceed instead).
#   - Optional extra_system_prompt: operator text appended to the base prompt as
#     site-specific clarifying context (it can't override the safety rules or the
#     JSON schema). Echoed back as extra_prompt in /server/bed_check/status.
#   - request_timeout MUST be < the macro's dwell WINDOW, so a hung API call
#     fails-closed and cancels before the dwell releases into homing.
#
# INSTALL: drop in ~/moonraker/moonraker/components/bed_check.py, add a
#   [bed_check] section to moonraker.conf, restart Moonraker. See README.md.
#
# NOTE: orient_snapshot defaults ON and matters for DETECTION, not just looks --
#   vision accuracy drops badly on an upside-down/rotated frame, so an inverted
#   camera must be corrected. It uses Pillow, already a Moonraker dependency.

from __future__ import annotations

import os
import json
import glob
import time
import base64
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    # Moonraker-internal types (ConfigHelper -> moonraker/confighelper.py,
    # WebRequest -> moonraker/common.py). They aren't resolvable when type-checking
    # this file outside the Moonraker tree, and are only ever used as annotations,
    # so alias them to Any to keep the checker quiet.
    ConfigHelper = Any
    WebRequest = Any

# Pillow is only needed if orient_snapshot is on. Degrade gracefully if absent.
try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None  # type: ignore[assignment]

DB_NAMESPACE = "bed_check"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Klipper-side names the callout handshake talks to (see bed_check.cfg).
CHECK_MACRO = "CHECK_BED"           # macro that fires the callout from PRINT_START
TIMEOUT_DGCODE = "bed_check_timeout"  # delayed_gcode dead-man's switch

# --- Prompt, ported from the check-bed skill -------------------------------
SYSTEM_PROMPT = (
    "You are a pre-print safety check for a Klipper 3D printer. You are shown a "
    "single webcam snapshot of the bed before a print starts. Your job is to "
    "catch a MECHANICAL HAZARD -- something the toolhead or bed would crash into "
    "during homing and leveling. Two things matter: (1) a finished or failed "
    "part still on the bed, and (2) any other object in the path of a "
    "homing/leveling move. A wrong 'go' that lets the machine drive into a part "
    "is the worst outcome, so be conservative about THESE: if you genuinely "
    "cannot tell whether such a hazard is present, return go=false.\n\n"
    "A missing or unseated build plate is NOT a mechanical hazard: homing onto "
    "the bare base may fail or print poorly, but nothing crashes. So plate status "
    "is ADVISORY ONLY -- report it, but it must NEVER set home_hazard or make "
    "go=false.\n\n"
    "Framing: webcams are often wide-angle and/or mounted at an angle, so the "
    "image may also show the gantry, the toolhead and its wiring, other machine "
    "structure around the bed (frame members, lead screws, belts, cabling), and "
    "surfaces/objects BEYOND and BESIDE the bed (shelves, the bench, spools, "
    "parts set aside). These are all expected machine structure or surroundings "
    "and do NOT count as print_left or home_hazard. Perspective can make an "
    "off-bed object look like it sits on the bed. The toolhead/extruder carriage "
    "is the printer's own moving hardware and is NEVER a leftover part or a "
    "hazard, no matter where it sits in the frame -- it may be parked or homed "
    "ANYWHERE over the bed (the center, an edge, a corner, or off to one side), "
    "not just tucked out of the way. It hangs from the gantry ABOVE the bed "
    "(connected by wiring/belts from above, usually a bulky carriage with fans/a "
    "shroud and a downward-pointing nozzle -- though the nozzle may not be "
    "visible from every angle), so it is suspended over the surface, not resting "
    "on it. Never count the toolhead as print_left or home_hazard. Judge only "
    "what is clearly resting ON the bed surface itself.\n\n"
    "Assess:\n"
    "1. print_left: is a finished or failed part still sitting ON the bed "
    "surface? A real leftover part rests within the INTERIOR of the print area "
    "and rises above the surface. Two things are NOT leftover parts: (a) the "
    "toolhead/extruder hovering over the bed (see Framing); (b) the plate's own "
    "handling tab -- a thin flat lip at the plate's EDGE or CORNER, often "
    "perforated or with a honeycomb/lattice/grid pattern or text/branding. The "
    "tab is integral to the plate even though its pattern can look like a printed "
    "lattice -- tell it apart by POSITION (it sits at the plate's perimeter, part "
    "of the plate's outline), not by its pattern. Do not flag the toolhead or "
    "the tab.\n"
    "2. home_hazard: anything else a homing/leveling move would crash into -- "
    "stray tools, clips, debris, dangling filament or wires, an object resting on "
    "the bed, an obstructed toolhead path. A missing plate, the bare base, and "
    "visible screw/mounting holes are flat or recessed and are NOT a "
    "home_hazard. A plate edge that looks curved or bowed is normally wide-angle "
    "lens distortion (straight lines bend toward the frame edges), not a warped "
    "plate or an obstruction -- do not flag it; only flag a clearly raised "
    "foreign object.\n"
    "3. plate_present (ADVISORY -- does not affect go): does a removable build "
    "plate look seated, or is the bare base showing? Build plates vary widely in "
    "color (black, grey, gold, blue, white, etc.) and finish (smooth, or "
    "granular/textured PEI); the bare base is typically a different finish and "
    "usually has screw/mounting holes visible across the surface. Give your best "
    "guess; never block on it.\n\n"
    "go is true when print_left is false AND home_hazard is false, regardless of "
    "plate_present. Return go=false only when you can SEE a part on the bed or a "
    "real obstruction in the homing path, or genuinely cannot rule one out. Set "
    "confident=false only when such a hazard is plausible but unclear, not merely "
    "because part of the view is blocked or the plate state is uncertain.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    '{"go": bool, "plate_present": bool, "print_left": bool, '
    '"home_hazard": bool, "confident": bool, "reason": "<one short sentence>"}'
)
USER_PROMPT = "Here is the current bed snapshot. Is it safe to start the print?"

# Framing for an operator-supplied addendum (extra_system_prompt). Appended after
# the base prompt as clarifying site context; it refines the guidance but is NOT
# allowed to override the safety rules or change the required JSON schema.
EXTRA_PROMPT_HEADER = (
    "\n\nADDITIONAL SITE-SPECIFIC NOTES from the operator about THIS printer. "
    "Treat them as clarifying context that refines the guidance above (for "
    "example, identifying a permanent fixture in the camera's view). They do NOT "
    "override the safety rules or change the required JSON schema:\n"
)


def _compose_system_prompt(extra: Optional[str]) -> str:
    """Base prompt, plus the operator's site-specific addendum if configured."""
    if not extra:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + EXTRA_PROMPT_HEADER + extra


class BedCheck:
    def __init__(self, config: "ConfigHelper") -> None:
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        self.name = config.get_name()

        # --- API key (don't put it in moonraker.conf if you can help it) ---
        # Resolution order: anthropic_api_key -> anthropic_api_key_path -> env.
        self.api_key: Optional[str] = config.get("anthropic_api_key", None)
        key_path = config.get("anthropic_api_key_path", None)
        if not self.api_key and key_path:
            try:
                with open(key_path, "r", encoding="utf-8") as fh:
                    self.api_key = fh.read().strip()
            except OSError as e:
                logging.error(f"bed_check: could not read api key file: {e}")
        if not self.api_key:
            self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            logging.warning(
                "bed_check: no Anthropic API key configured — checks will fail "
                "(fail-closed will block starts).")

        # Opus 4.8 — most reliable on tricky beds (handles the handling tab, a
        # parked toolhead over the bed, and lens-curved plate edges natively,
        # where Sonnet needs prompt help). Override with `model:` in [bed_check]
        # -- claude-sonnet-4-6 is cheaper and fine on a well-lit, upright camera.
        self.model = config.get("model", "claude-opus-4-8")
        self.max_tokens = config.getint("max_tokens", 1024)
        # Optional operator addendum to the system prompt, for site-specific
        # context (e.g. "the grey clip at the back-left corner is a permanent
        # cable guide, not a part"). Appended as clarifying context -- it refines
        # the prompt but cannot override the safety rules or the JSON schema.
        # Exposed in /status (extra_prompt) so you can confirm what was sent.
        self.extra_prompt: Optional[str] = (
            config.get("extra_system_prompt", "") or "").strip() or None
        self.system_prompt = _compose_system_prompt(self.extra_prompt)
        # Keep request_timeout < the CHECK_BED dwell WINDOW (default 25s) so a
        # hung API call fails-closed and cancels before the dwell releases motion.
        self.request_timeout = config.getfloat("request_timeout", 20.0)
        self.fail_open = config.getboolean("fail_open", False)  # default: fail-closed
        self.orient = config.getboolean("orient_snapshot", True)
        self.default_enabled = config.getboolean("enabled_default", True)

        # Explicit snapshot URL wins; otherwise discover via webcams/list.
        self.snapshot_url: Optional[str] = config.get("snapshot_url", None)
        self.webcam_name: Optional[str] = config.get("webcam_name", None)
        # Base URL for Moonraker's own API (used for webcam discovery). Override
        # if Moonraker isn't on the default port.
        self.moonraker_url = config.get(
            "moonraker_url", "http://localhost:7125").rstrip("/")

        # Keep the last N oriented snapshots on disk for review.
        self.snapshot_dir = os.path.expanduser(
            config.get("snapshot_save_dir", "~/printer_data/bed_check_snapshots"))
        self.snapshot_keep = config.getint("snapshot_keep", 5)

        self.enabled = self.default_enabled
        self.last_verdict: Optional[Dict[str, Any]] = None
        self.last_verdict_time: Optional[float] = None  # epoch seconds
        self.last_verdict_source: Optional[str] = None  # callout | dry_run
        self.last_snapshot_path: Optional[str] = None

        self.database = self.server.lookup_component("database")
        self.database.register_local_namespace(DB_NAMESPACE)

        # gcode bridge: BED_CHECK macro -> action_call_remote_method("set_bed_check")
        self.server.register_remote_method("set_bed_check", self._remote_set_enabled)
        # gcode callout: PRINT_START's CHECK_BED macro -> action_call_remote_method("check_bed")
        self.server.register_remote_method("check_bed", self._remote_check_bed)
        # console status: BED_CHECK_STATUS macro -> RESPONDs current state back
        self.server.register_remote_method("bed_check_status", self._remote_status)

        # HTTP endpoints: dry-run check, status, enable toggle, last snapshot.
        self.server.register_endpoint(
            "/server/bed_check/check", ["POST"], self._handle_check)
        self.server.register_endpoint(
            "/server/bed_check/status", ["GET"], self._handle_status)
        self.server.register_endpoint(
            "/server/bed_check/enabled", ["POST"], self._handle_set_enabled)
        # Serves the most recent oriented snapshot (raw JPEG) for review.
        # Binary return via wrap_result=False + content_type (verified working on
        # Moonraker); last_snapshot path is also exposed in /status as a fallback.
        self.server.register_endpoint(
            "/server/bed_check/last_snapshot", ["GET"], self._handle_last_snapshot,
            wrap_result=False, content_type="image/jpeg")

        # Clients (KlippyDash) can subscribe to this to surface NO-GO / disabled state.
        self.server.register_notification("bed_check:verdict")

    async def component_init(self) -> None:
        # Restore the persisted enable flag (fail-closed if DB read fails).
        try:
            self.enabled = await self.database.get_item(
                DB_NAMESPACE, "enabled", self.default_enabled)
        except Exception as e:
            logging.error(f"bed_check: failed to read persisted flag: {e}")
        logging.info(f"bed_check: initialized (enabled={self.enabled}, "
                     f"fail_open={self.fail_open})")

    # ---- enable / disable -------------------------------------------------

    def _remote_set_enabled(self, enabled: Any = 1, **_: Any) -> None:
        # Called from Klipper gcode (fire-and-forget). Schedule the async update.
        try:
            value = bool(int(enabled))
        except (TypeError, ValueError):
            value = bool(enabled)
        self.eventloop.create_task(self._set_enabled(value))

    async def _set_enabled(self, value: bool) -> None:
        self.enabled = value
        try:
            await self.database.insert_item(DB_NAMESPACE, "enabled", value)
        except Exception as e:
            logging.error(f"bed_check: failed to persist flag: {e}")
        state = "ENABLED" if value else "DISABLED"
        logging.info(f"bed_check: {state}")
        await self._notify("toggle", f"bed check {state}",
                           {"enabled": value}, respond=True)

    # ---- console status (BED_CHECK_STATUS) --------------------------------

    def _remote_status(self, **_: Any) -> None:
        # Fired from BED_CHECK_STATUS (fire-and-forget); answer via RESPOND.
        self.eventloop.create_task(self._respond_status())

    async def _respond_status(self) -> None:
        kapis = self.server.lookup_component("klippy_apis")
        state = "ENABLED" if self.enabled else "DISABLED"
        lines = [
            f"BedCheck: {state} | model={self.model} | "
            f"fail_{'open' if self.fail_open else 'closed'}"
        ]
        if self.last_verdict_time is not None:
            age = round(time.time() - self.last_verdict_time, 1)
            v = self.last_verdict or {}
            outcome = "GO" if v.get("go") else "NO-GO"
            reason = str(v.get("reason", "")).replace('"', "'")
            lines.append(
                f"BedCheck: last {outcome} via {self.last_verdict_source} "
                f"{age}s ago -- {reason}")
        else:
            lines.append("BedCheck: no check has run since the component loaded")
        for line in lines:
            await kapis.run_gcode(f'RESPOND TYPE=command MSG="{line}"')

    # ---- gcode callout (PRINT_START trigger) ------------------------------

    def _remote_check_bed(self, **_: Any) -> None:
        # Fired from Klipper's CHECK_BED macro (fire-and-forget). Run the check
        # off the reactor; resolve the handshake + cancel via gcode when done.
        self.eventloop.create_task(self._do_callout_check())

    async def _do_callout_check(self) -> None:
        kapis = self.server.lookup_component("klippy_apis")

        async def disarm() -> None:
            # Clear the handshake flag and cancel the dead-man timer so the
            # delayed_gcode no-ops. Always called once we have an outcome.
            await kapis.run_gcode(
                f"SET_GCODE_VARIABLE MACRO={CHECK_MACRO} VARIABLE=pending VALUE=0")
            await kapis.run_gcode(
                f"UPDATE_DELAYED_GCODE ID={TIMEOUT_DGCODE} DURATION=0")

        if not self.enabled:
            await disarm()
            await self._notify(
                "disabled", "bed check DISABLED — proceeding unchecked", respond=True)
            return

        try:
            verdict = await self._run_check(source="callout")
        except Exception as e:
            logging.exception("bed_check: callout check failed")
            await disarm()
            if self.fail_open:
                await self._notify(
                    "error_failopen", f"bed check errored ({e}); proceeding anyway",
                    {"error": str(e)}, respond=True)
            else:
                await self._notify(
                    "error_failclosed", f"bed check errored ({e}); cancelling print",
                    {"error": str(e)}, respond=True)
                await kapis.run_gcode("CANCEL_PRINT")
            return

        await disarm()
        if verdict.get("go"):
            if not verdict.get("plate_present", True):
                # Fail-open on plate: proceed, but don't let a missing plate be silent.
                msg = ("no parts on bed — proceeding, but BUILD PLATE MAY BE "
                       "MISSING/UNSEATED (homing may fail)")
            else:
                msg = "bed clear — proceeding"
            await self._notify("go", msg, verdict, respond=True)
        else:
            await self._notify(
                "no_go", f"NO-GO: {verdict.get('reason', '')} — cancelling print",
                verdict, respond=True)
            await kapis.run_gcode("CANCEL_PRINT")

    # ---- HTTP handlers ----------------------------------------------------

    async def _handle_check(self, web_request: "WebRequest") -> Dict[str, Any]:
        # Run the check without starting anything (dry run / dashboard button).
        verdict = await self._run_check(source="dry_run")
        return {"verdict": verdict}

    async def _handle_status(self, web_request: "WebRequest") -> Dict[str, Any]:
        ts = self.last_verdict_time
        return {
            "enabled": self.enabled,
            "fail_open": self.fail_open,
            "model": self.model,
            # The operator's site-specific addendum appended to the system prompt
            # (null if none). Surfaced here so you can confirm exactly what was sent.
            "extra_prompt": self.extra_prompt,
            "last_verdict": self.last_verdict,
            # When the last check ran, and what triggered it (callout = a real
            # PRINT_START fired it; dry_run = a /check curl).
            "last_verdict_time": ts,
            "last_verdict_time_iso": (
                datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None
            ),
            "last_verdict_age_s": (round(time.time() - ts, 1) if ts else None),
            "last_verdict_source": self.last_verdict_source,
            "last_snapshot": self.last_snapshot_path,
        }

    async def _handle_last_snapshot(self, web_request: "WebRequest") -> bytes:
        path = self.last_snapshot_path
        if not path or not os.path.isfile(path):
            raise self.server.error("no bed_check snapshot available yet", 404)
        with open(path, "rb") as fh:
            return fh.read()

    async def _handle_set_enabled(self, web_request: "WebRequest") -> Dict[str, Any]:
        value = web_request.get_boolean("enabled")
        await self._set_enabled(value)
        return {"enabled": self.enabled}

    # ---- core: snapshot + AI ---------------------------------------------

    async def _run_check(self, source: str = "check") -> Dict[str, Any]:
        if not self.api_key:
            raise self.server.error("no Anthropic API key configured", 500)
        img_bytes = await self._get_snapshot()
        self._save_snapshot(img_bytes)
        verdict = await self._ask_claude(img_bytes)
        self.last_verdict = verdict
        self.last_verdict_time = time.time()
        self.last_verdict_source = source
        logging.info(f"bed_check verdict ({source}): {verdict}")
        return verdict

    async def _get_snapshot(self) -> bytes:
        client = self.server.lookup_component("http_client")
        url = self.snapshot_url
        rotation, flip_h, flip_v = 0, False, False

        if url is None:
            # Discover from Moonraker's own webcam list (mirrors Get-PrinterBed.ps1).
            # Result shape is {"result": {"webcams": [...]}} (verified).
            resp = await client.get(f"{self.moonraker_url}/server/webcams/list")
            data = resp.json()
            cams = data.get("result", {}).get("webcams", [])
            if not cams:
                raise self.server.error("no webcams reported by Moonraker", 500)
            cam = cams[0]
            if self.webcam_name:
                cam = next((c for c in cams if c.get("name") == self.webcam_name), cam)
            snap = cam.get("snapshot_url", "")
            if snap.startswith("http"):
                url = snap
            elif snap.startswith("/"):
                # Relative URLs are served by the camera streamer on :80, not Moonraker.
                url = f"http://localhost{snap}"
            else:
                url = f"http://localhost/{snap}"
            rotation = int(cam.get("rotation", 0) or 0)
            flip_h = bool(cam.get("flip_horizontal", False))
            flip_v = bool(cam.get("flip_vertical", False))

        resp = await client.get(url)
        if resp.has_error():
            raise self.server.error(f"snapshot fetch failed: {resp.status_code}", 502)
        img_bytes = resp.content

        if self.orient and (rotation or flip_h or flip_v):
            img_bytes = self._orient(img_bytes, rotation, flip_h, flip_v)
        return img_bytes

    def _orient(self, img_bytes: bytes, rotation: int,
                flip_h: bool, flip_v: bool) -> bytes:
        # NOT just cosmetic: vision models reason far better on an upright frame,
        # so an inverted/rotated camera must be corrected here or detection
        # suffers (a flipped bed reads as out-of-distribution). Degrades to the
        # raw image, with a warning, if Pillow is unavailable.
        if Image is None:
            logging.warning(
                "bed_check: orient_snapshot is on but Pillow is not installed -- "
                "sending the un-oriented frame. On a flipped/rotated camera this "
                "hurts detection; install Pillow or orient at the camera/streamer.")
            return img_bytes
        # Pillow >=9.1 moved the transpose constants onto the Image.Transpose enum
        # and dropped the module-level aliases in Pillow 10; fall back for older.
        transpose = getattr(Image, "Transpose", Image)
        try:
            img = Image.open(BytesIO(img_bytes))
            if rotation:
                # PIL rotates counter-clockwise; negate to match camera rotation.
                img = img.rotate(-rotation, expand=True)
            if flip_h:
                img = img.transpose(transpose.FLIP_LEFT_RIGHT)
            if flip_v:
                img = img.transpose(transpose.FLIP_TOP_BOTTOM)
            out = BytesIO()
            img.convert("RGB").save(out, format="JPEG")
            return out.getvalue()
        except Exception as e:
            logging.error(f"bed_check: orient failed ({e}); using raw image")
            return img_bytes

    def _save_snapshot(self, img_bytes: bytes) -> None:
        # Persist the oriented frame the model is about to see; keep the newest N.
        if not self.snapshot_dir:
            return
        try:
            os.makedirs(self.snapshot_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(self.snapshot_dir, f"bed_{ts}.jpg")
            with open(path, "wb") as fh:
                fh.write(img_bytes)
            self.last_snapshot_path = path
            self._prune_snapshots()
        except OSError as e:
            logging.error(f"bed_check: failed to save snapshot: {e}")

    def _prune_snapshots(self) -> None:
        # Timestamped names sort chronologically, so keep the last `snapshot_keep`.
        try:
            files = sorted(glob.glob(os.path.join(self.snapshot_dir, "bed_*.jpg")))
            for old in files[:-self.snapshot_keep]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except OSError:
            pass

    async def _ask_claude(self, img_bytes: bytes) -> Dict[str, Any]:
        client = self.server.lookup_component("http_client")
        b64 = base64.b64encode(img_bytes).decode("ascii")
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system_prompt,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    }},
                    {"type": "text", "text": USER_PROMPT},
                ],
            }],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        # http_client.request(method, url, body=, headers=, request_timeout=) -- verified.
        resp = await client.request(
            "POST", ANTHROPIC_URL,
            body=json.dumps(body), headers=headers,
            request_timeout=self.request_timeout)
        if resp.has_error():
            raise self.server.error(
                f"Anthropic API error {resp.status_code}: {resp.text[:200]}", 502)
        data = resp.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        return self._parse_verdict(text)

    @staticmethod
    def _parse_verdict(text: str) -> Dict[str, Any]:
        text = text.strip()
        # Be forgiving if the model wraps the JSON in anything.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
        try:
            v = json.loads(text)
        except json.JSONDecodeError:
            # Unparseable -> treat as no-go (fail safe).
            return {"go": False, "confident": False,
                    "reason": "could not parse model response", "raw": text}
        # Policy (2026-06-26): the only blocking hazards are a part left on
        # the bed or an obstruction in the homing path -- a wrong 'go' there can
        # crash the machine. Plate status is advisory and FAILS OPEN (a missing
        # plate may spoil the print but is not a mechanical hazard). Decide go
        # from the hazard fields, not the model's holistic 'go', so plate state
        # can never block. A parse failure still fails closed (handled above).
        v["print_left"] = bool(v.get("print_left", False))
        v["home_hazard"] = bool(v.get("home_hazard", False))
        v["plate_present"] = bool(v.get("plate_present", True))
        v["go"] = (not v["print_left"]) and (not v["home_hazard"])
        return v

    # ---- helpers ----------------------------------------------------------

    async def _notify(self, kind: str, message: str,
                      payload: Optional[Dict[str, Any]] = None,
                      respond: bool = False) -> None:
        event = {"kind": kind, "message": message, "payload": payload or {}}
        self.server.send_event("bed_check:verdict", event)
        if respond:
            try:
                kapis = self.server.lookup_component("klippy_apis")
                msg = message.replace('"', "'")
                await kapis.run_gcode(f'RESPOND TYPE=command MSG="BedCheck: {msg}"')
            except Exception:
                pass  # console RESPOND is best-effort


def load_component(config: "ConfigHelper") -> BedCheck:
    return BedCheck(config)
