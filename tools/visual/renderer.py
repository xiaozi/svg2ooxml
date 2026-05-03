"""Utilities for rendering PPTX slides to bitmap images for visual tests."""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import zipfile
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from lxml import etree as ET
from PIL import Image

logger = logging.getLogger(__name__)
_SOFFICE_PROCESS_LOCK = threading.Lock()
_SOFFICE_AVAILABLE = object()


class VisualRendererError(RuntimeError):
    """Raised when the external rendering tool fails."""


@dataclass
class RenderedSlideSet:
    """Container describing the output from a rendering pass."""

    images: Sequence[Path]
    renderer: str


def _normalize_user_installation(user_installation: str | None) -> str | None:
    if not user_installation:
        return None
    if user_installation.startswith("file:"):
        return user_installation
    return Path(user_installation).resolve().as_uri()


def _path_from_file_uri(uri: str | None) -> Path | None:
    if not uri or not uri.startswith("file:"):
        return None
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    return Path(urllib.parse.unquote(parsed.path))


def _check_file_readable(path: Path, label: str) -> str | None:
    if not path.exists():
        return f"{label} does not exist: {path}"
    if not path.is_file():
        return f"{label} is not a file: {path}"
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        return f"{label} is not readable: {path} ({exc})"
    return None


def _check_executable(path: Path, label: str) -> str | None:
    if not path.exists():
        return f"{label} does not exist: {path}"
    if not path.is_file():
        return f"{label} is not a file: {path}"
    if not os.access(path, os.X_OK):
        return f"{label} is not executable: {path}"
    return None


def _resolve_command_path(command: str) -> Path:
    path = Path(command)
    if path.parent != Path("."):
        return path
    resolved = shutil.which(command)
    if resolved:
        return Path(resolved)
    return path


def _check_directory_writable(path: Path, label: str) -> str | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"{label} cannot be created: {path} ({exc})"
    if not path.is_dir():
        return f"{label} is not a directory: {path}"
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".svg2ooxml-write-test-",
            dir=path,
            delete=True,
        ) as handle:
            handle.write(b"ok")
            handle.flush()
    except OSError as exc:
        return f"{label} is not writable: {path} ({exc})"
    return None


def _macos_app_bundle_path(command_path: Path) -> Path | None:
    for parent in command_path.parents:
        if parent.suffix == ".app":
            return parent
    return None


def _macos_quarantine_value(path: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["xattr", "-p", "com.apple.quarantine", str(path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _macos_quarantine_hints(paths: Sequence[Path]) -> list[str]:
    if platform.system() != "Darwin":
        return []

    hints: list[str] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        quarantine = _macos_quarantine_value(resolved)
        if quarantine:
            hints.append(
                f"macOS quarantine is set on {resolved}; inspect with "
                f"`xattr -l {resolved}` and clear only if trusted."
            )
    return hints


def _latest_macos_soffice_crash_hint() -> str | None:
    if platform.system() != "Darwin":
        return None
    reports = sorted(
        Path.home().glob("Library/Logs/DiagnosticReports/soffice*.ips"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for report in reports[:6]:
        try:
            text = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if (
            "sandbox denied the right to lookup com.apple.coreservices.launchservicesd"
            in text
            or "_RegisterApplication(), unable to get application ASN from launchservicesd"
            in text
        ):
            return (
                "Recent macOS crash report shows LibreOffice was sandbox-denied "
                "from looking up com.apple.coreservices.launchservicesd while "
                "registering with LaunchServices."
            )
    return None


def _probe_soffice_conversion(
    command_path: str,
    *,
    timeout: float | None,
    user_installation: str | None = None,
) -> str | None:
    """Return None when a tiny conversion works; otherwise return the reason."""

    if os.getenv("SVG2OOXML_SKIP_SOFFICE_PROBE") == "1":
        return None

    probe_root = Path(tempfile.mkdtemp(prefix="svg2ooxml_soffice_probe_"))
    try:
        input_dir = probe_root / "input"
        output_dir = probe_root / "output"
        profile_dir = probe_root / "profile"
        input_dir.mkdir()
        output_dir.mkdir()
        profile_dir.mkdir()
        input_path = input_dir / "probe.txt"
        input_path.write_text("LibreOffice conversion probe\n", encoding="utf-8")
        profile_uri = user_installation or profile_dir.resolve().as_uri()
        cmd = [
            command_path,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(input_path),
        ]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return "LibreOffice soffice command not found."
        except subprocess.TimeoutExpired:
            return f"LibreOffice conversion probe timed out after {timeout} seconds."

        if completed.returncode == 0 and (output_dir / "probe.pdf").exists():
            return None

        message_lines = [
            "LibreOffice conversion probe failed.",
            f"exit code: {completed.returncode}",
        ]
        if completed.stdout:
            message_lines.append(f"stdout:\n{completed.stdout}")
        if completed.stderr:
            message_lines.append(f"stderr:\n{completed.stderr}")
        crash_hint = _latest_macos_soffice_crash_hint()
        if crash_hint:
            message_lines.append(f"hint: {crash_hint}")
        return "\n".join(message_lines)
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)


def _kill_running_soffice() -> None:
    """Kill any running LibreOffice/soffice processes so headless mode can start cleanly."""
    try:
        result = subprocess.run(
            ["pkill", "-f", "soffice"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            import time

            time.sleep(0.5)
            logger.debug("Killed existing soffice process(es) before headless render.")
    except FileNotFoundError:
        pass


@contextmanager
def _soffice_render_lock():
    """Serialize LibreOffice conversions across threads and local processes."""

    with _SOFFICE_PROCESS_LOCK:
        lock_path = Path(tempfile.gettempdir()) / "svg2ooxml-soffice-render.lock"
        with lock_path.open("a+") as lock_file:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class LibreOfficeRenderer:
    """Render PPTX files to PNG using LibreOffice (soffice) headless mode."""

    def __init__(
        self,
        soffice_path: str | None = None,
        *,
        timeout: float | None = 90.0,
        user_installation: str | None = None,
        png_dpi: float | None = 96.0,
    ) -> None:
        self._timeout = timeout
        self._command_path = soffice_path or shutil.which("soffice")
        self._user_installation = _normalize_user_installation(user_installation)
        self._user_installation_path = _path_from_file_uri(self._user_installation)
        self._availability_probe: object | str | None = _SOFFICE_AVAILABLE
        if png_dpi is not None and png_dpi <= 0:
            raise ValueError("png_dpi must be > 0 or None to disable normalization.")
        self._png_dpi = png_dpi

    # ------------------------------------------------------------------
    # Capability helpers
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return True if soffice can be used for conversion on this platform."""

        return self.unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        if self._command_path is None:
            return "LibreOffice (soffice) is not installed or not on PATH."
        if self._availability_probe is _SOFFICE_AVAILABLE:
            command_path = _resolve_command_path(self._command_path)
            executable_error = _check_executable(
                command_path, "LibreOffice soffice command"
            )
            if executable_error:
                self._availability_probe = executable_error
            else:
                self._availability_probe = _probe_soffice_conversion(
                    str(command_path),
                    timeout=min(self._timeout or 30.0, 30.0),
                    user_installation=self._user_installation,
                )
        return self._availability_probe

    @property
    def command_path(self) -> str | None:
        return self._command_path

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def base_args(self) -> list[str]:
        return [
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--norestore",
            "--nolockcheck",
        ]

    def render(self, pptx_path: Path | str, output_dir: Path | str) -> RenderedSlideSet:
        """Render *pptx_path* into PNG images under *output_dir*."""

        if not self.available:
            raise VisualRendererError(
                self.unavailable_reason
                or "LibreOffice (soffice) is not available for rendering."
            )

        pptx_path = Path(pptx_path)
        output_dir = Path(output_dir)

        temporary_user_install_dir: Path | None = None
        if self._user_installation:
            user_install_uri = self._user_installation
            user_install_dir = self._user_installation_path
        else:
            temporary_user_install_dir = Path(tempfile.mkdtemp(prefix="soffice_user_"))
            user_install_dir = temporary_user_install_dir
            user_install_uri = temporary_user_install_dir.resolve().as_uri()

        command_path = _resolve_command_path(self._command_path or "soffice")
        preflight_errors = [
            error
            for error in (
                _check_executable(command_path, "LibreOffice soffice command"),
                _check_file_readable(pptx_path, "PPTX input"),
                _check_directory_writable(output_dir, "LibreOffice output directory"),
                "LibreOffice profile directory is not a local file URI: "
                f"{self._user_installation}"
                if self._user_installation and user_install_dir is None
                else None,
                _check_directory_writable(user_install_dir, "LibreOffice profile directory")
                if user_install_dir is not None
                else None,
            )
            if error
        ]
        quarantine_paths = [command_path, pptx_path]
        app_path = _macos_app_bundle_path(command_path)
        if app_path is not None:
            quarantine_paths.append(app_path)
        preflight_errors.extend(_macos_quarantine_hints(quarantine_paths))
        if preflight_errors:
            if temporary_user_install_dir is not None:
                shutil.rmtree(temporary_user_install_dir, ignore_errors=True)
                temporary_user_install_dir = None
            message = "LibreOffice preflight failed:\n" + "\n".join(
                f"- {error}" for error in preflight_errors
            )
            raise VisualRendererError(message)

        soffice_args = [
            *self.base_args(),
            f"-env:UserInstallation={user_install_uri}",
            "--convert-to",
            "png:impress_png_Export",
            "--outdir",
            str(output_dir),
            str(pptx_path),
        ]

        def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            logger.debug("Running soffice renderer: %s", " ".join(cmd))
            try:
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    check=False,
                    timeout=self._timeout,
                    text=True,
                )
            except FileNotFoundError as exc:  # pragma: no cover - defensive
                raise VisualRendererError(
                    "LibreOffice soffice command not found."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise VisualRendererError(
                    f"LibreOffice timed out after {self._timeout} seconds."
                ) from exc

        cmd = [self._command_path or "soffice", *soffice_args]  # guarded above
        try:
            with _soffice_render_lock():
                completed = _run(cmd)
                tried_open = False
                if (
                    completed.returncode != 0
                    and platform.system() == "Darwin"
                    and os.getenv("SVG2OOXML_SOFFICE_OPEN_FALLBACK") == "1"
                ):
                    open_cmd = self._macos_open_command(soffice_args)
                    if open_cmd:
                        tried_open = True
                        completed = _run(open_cmd)

            if completed.returncode != 0:
                message_lines = [
                    "LibreOffice failed to render PPTX.",
                    f"exit code: {completed.returncode}",
                ]
                if tried_open:
                    message_lines.append(
                        "LibreOffice failed when launched via open(1) as well."
                    )
                if completed.stdout:
                    message_lines.append(f"stdout:\n{completed.stdout}")
                if completed.stderr:
                    message_lines.append(f"stderr:\n{completed.stderr}")
                if completed.returncode == 134 and platform.system() == "Darwin":
                    mac_version = platform.mac_ver()[0]
                    if mac_version.startswith("26."):
                        message_lines.append(
                            "hint: LibreOffice headless appears to crash on macOS 26.x here. "
                            "Try a different LibreOffice build or run visual tests on macOS 25.x."
                        )
                raise VisualRendererError("\n".join(message_lines))
        finally:
            if temporary_user_install_dir is not None:
                shutil.rmtree(temporary_user_install_dir, ignore_errors=True)

        generated = sorted(output_dir.glob("*.png"))
        if not generated:
            raise VisualRendererError(
                f"LibreOffice completed but produced no PNG files in {output_dir}."
            )

        if self._png_dpi is not None:
            _normalize_pngs(pptx_path, generated, self._png_dpi)

        logger.debug("Generated %d slide image(s).", len(generated))
        return RenderedSlideSet(images=tuple(generated), renderer="soffice")

    def _macos_open_command(self, args: Sequence[str]) -> list[str] | None:
        open_path = shutil.which("open")
        app_path = self._macos_app_path()
        if not open_path or not app_path:
            return None

        # Always use -a with the .app path.  The bundle-id route
        # (`open -b org.libreoffice.script`) fails on many macOS installs
        # with "LSCopyApplicationURLsForBundleIdentifier() failed" because
        # LaunchServices doesn't register the id reliably.
        return [open_path, "-W", "-a", app_path, "--args", *args]

    def _macos_app_path(self) -> str | None:
        if not self._command_path:
            return None
        for parent in Path(self._command_path).parents:
            if parent.suffix == ".app":
                return str(parent)
        return None


class PowerPointRenderer:
    """Render PPTX files to PNG using Microsoft PowerPoint via AppleScript."""

    def __init__(
        self,
        *,
        backend: str = "auto",
        delay: float = 1.5,
        slideshow_delay: float = 1.0,
        slide_delay: float = 0.15,
        open_timeout: float = 120.0,
        capture_timeout: float = 5.0,
        use_keys: bool = False,
        allow_reopen: bool = True,
        png_dpi: float | None = None,
    ) -> None:
        self._backend = backend
        self._delay = delay
        self._slideshow_delay = slideshow_delay
        self._slide_delay = slide_delay
        self._open_timeout = open_timeout
        self._capture_timeout = capture_timeout
        self._use_keys = use_keys
        self._allow_reopen = allow_reopen
        self._png_dpi = _resolve_png_dpi() if png_dpi is None else png_dpi

    @property
    def available(self) -> bool:
        return platform.system() == "Darwin" and shutil.which("osascript") is not None

    def render(self, pptx_path: Path | str, output_dir: Path | str) -> RenderedSlideSet:
        if not self.available:
            raise VisualRendererError(
                "PowerPoint capture requires macOS with osascript available."
            )

        pptx_path = Path(pptx_path)
        if not pptx_path.exists():
            raise VisualRendererError(f"PPTX path does not exist: {pptx_path}")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        slide_count = _pptx_slide_count(pptx_path)
        self._run_capture_helper(
            [
                str(pptx_path),
                str(output_dir),
                "--mode",
                "slideshow-all",
                "--delay",
                str(self._delay),
                "--slideshow-delay",
                str(self._slideshow_delay),
                "--slide-delay",
                str(self._slide_delay),
                "--open-timeout",
                str(self._open_timeout),
                "--capture-timeout",
                str(self._capture_timeout),
                "--backend",
                self._backend,
            ],
            timeout=max(
                80.0,
                self._open_timeout
                + (self._capture_timeout * max(1, slide_count))
                + (self._slide_delay * max(1, slide_count))
                + 15.0,
            ),
        )

        generated = sorted(output_dir.glob("slide_*.png"), key=_natural_sort_key)
        if not generated:
            raise VisualRendererError(
                f"PowerPoint capture completed but produced no PNG files in {output_dir}."
            )

        if self._png_dpi is not None:
            self._normalize_pngs(pptx_path, generated, self._png_dpi)

        logger.debug("Generated %d slide image(s).", len(generated))
        return RenderedSlideSet(images=tuple(generated), renderer="powerpoint")

    def capture_animation(
        self,
        pptx_path: Path | str,
        output_dir: Path | str,
        *,
        duration: float,
        fps: float = 10.0,
    ) -> Sequence[Path]:
        if not self.available:
            raise VisualRendererError(
                "PowerPoint capture requires macOS with osascript available."
            )

        pptx_path = Path(pptx_path)
        if not pptx_path.exists():
            raise VisualRendererError(f"PPTX path does not exist: {pptx_path}")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        frame_count = max(1, int(duration * fps)) if duration > 0 else 1
        self._run_capture_helper(
            [
                str(pptx_path),
                str(output_dir),
                "--mode",
                "live",
                "--duration",
                str(duration),
                "--fps",
                str(fps),
                "--delay",
                str(self._delay),
                "--slideshow-delay",
                str(self._slideshow_delay),
                "--open-timeout",
                str(self._open_timeout),
                "--capture-timeout",
                str(self._capture_timeout),
                "--backend",
                self._backend,
            ],
            timeout=max(
                80.0,
                self._open_timeout
                + duration
                + (self._capture_timeout * frame_count)
                + 15.0,
            ),
        )

        generated = tuple(sorted(output_dir.glob("frame_*.png"), key=_natural_sort_key))
        if not generated:
            raise VisualRendererError(
                f"PowerPoint animation capture produced no PNG files in {output_dir}."
            )

        if self._png_dpi is not None:
            self._normalize_pngs(pptx_path, generated, self._png_dpi)

        logger.debug("Generated %d animation frame(s).", len(generated))
        return generated

    def _run_capture_helper(self, args: Sequence[str], *, timeout: float) -> None:
        cmd = [sys.executable, "-m", "tools.ppt_research.powerpoint_capture", *args]
        if not self._use_keys:
            cmd.append("--no-keys")
        if not self._allow_reopen:
            cmd.append("--no-reopen")
        logger.debug("Running PowerPoint capture helper: %s", " ".join(cmd))
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise VisualRendererError(
                f"PowerPoint capture timed out after {timeout:.1f} seconds."
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise VisualRendererError(detail or "PowerPoint capture failed.")

    def _normalize_pngs(
        self,
        pptx_path: Path,
        images: Sequence[Path],
        png_dpi: float,
    ) -> None:
        _normalize_pngs(pptx_path, images, png_dpi)


def _normalize_pngs(
    pptx_path: Path,
    images: Sequence[Path],
    png_dpi: float,
) -> None:
    target_size = _slide_size_to_pixels(pptx_path, png_dpi)
    if target_size is None:
        logger.warning(
            "Unable to resolve slide size for %s; skipping PNG normalization.",
            pptx_path,
        )
        return

    for image_path in images:
        with Image.open(image_path) as img:
            normalized = _normalize_slide_capture(img, target_size)
            if normalized.size == target_size and normalized is img:
                continue
            normalized.save(image_path)


def _normalize_slide_capture(
    image: Image.Image,
    target_size: tuple[int, int],
) -> Image.Image:
    if image.size == target_size:
        return image
    normalized = image
    crop_box = _detect_slide_crop_box(image, target_size=target_size)
    if crop_box is not None:
        normalized = image.crop(crop_box)
    if normalized.size == target_size:
        return normalized
    return normalized.resize(target_size, resample=Image.LANCZOS)


def _detect_slide_crop_box(
    image: Image.Image,
    *,
    target_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    if image.width < 8 or image.height < 8:
        return None

    rgb = np.array(image.convert("RGB"))
    height, width = rgb.shape[:2]
    center_y = height // 2
    center_x = width // 2
    patch_radius = max(2, min(width, height) // 100)
    center_patch = rgb[
        max(0, center_y - patch_radius) : min(height, center_y + patch_radius + 1),
        max(0, center_x - patch_radius) : min(width, center_x + patch_radius + 1),
    ]
    if center_patch.size == 0:
        return None

    border_pixels = np.vstack((rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]))
    border_color = np.median(border_pixels, axis=0)
    center_color = np.median(center_patch.reshape(-1, 3), axis=0)
    if float(np.max(np.abs(center_color - border_color))) < 24.0:
        return None

    tolerance = 16
    similar_to_center = (
        np.max(np.abs(rgb.astype(np.int16) - center_color.astype(np.int16)), axis=2)
        <= tolerance
    )
    row_fraction = similar_to_center.mean(axis=1)
    col_fraction = similar_to_center.mean(axis=0)
    row_indexes = np.where(row_fraction > 0.15)[0]
    col_indexes = np.where(col_fraction > 0.15)[0]
    if row_indexes.size == 0 or col_indexes.size == 0:
        return None

    top = int(row_indexes[0])
    bottom = int(row_indexes[-1]) + 1
    left = int(col_indexes[0])
    right = int(col_indexes[-1]) + 1

    crop_width = right - left
    crop_height = bottom - top
    if crop_width <= 0 or crop_height <= 0:
        return None

    target_aspect = target_size[0] / target_size[1]
    current_aspect = crop_width / crop_height
    if abs(current_aspect - target_aspect) > 0.02:
        if current_aspect > target_aspect:
            adjusted_width = max(1, int(round(crop_height * target_aspect)))
            x_center = (left + right) // 2
            left = max(0, x_center - (adjusted_width // 2))
            right = min(width, left + adjusted_width)
            left = max(0, right - adjusted_width)
        else:
            adjusted_height = max(1, int(round(crop_width / target_aspect)))
            y_center = (top + bottom) // 2
            top = max(0, y_center - (adjusted_height // 2))
            bottom = min(height, top + adjusted_height)
            top = max(0, bottom - adjusted_height)

    if left <= 0 and top <= 0 and right >= width and bottom >= height:
        return None
    crop_area_fraction = ((right - left) * (bottom - top)) / float(width * height)
    if crop_area_fraction < 0.45:
        return None
    return (left, top, right, bottom)


def default_renderer(
    *,
    timeout: float | None = 90.0,
    user_installation: str | None = None,
) -> LibreOfficeRenderer:
    """Return a renderer using an explicit path, macOS default, or PATH lookup."""

    soffice_override = os.getenv("SVG2OOXML_SOFFICE_PATH")
    if not soffice_override:
        mac_default = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if mac_default.exists():
            soffice_override = str(mac_default)
    if user_installation is None:
        user_installation = os.getenv("SVG2OOXML_SOFFICE_USER_INSTALL")
    png_dpi = _resolve_png_dpi()
    return LibreOfficeRenderer(
        soffice_path=soffice_override,
        timeout=timeout,
        user_installation=user_installation,
        png_dpi=png_dpi,
    )


PptxRenderer = LibreOfficeRenderer | PowerPointRenderer


def resolve_renderer(
    *,
    renderer_name: str | None = None,
    soffice_path: str | None = None,
    timeout: float | None = 90.0,
    user_installation: str | None = None,
    powerpoint_backend: str = "auto",
    powerpoint_delay: float = 0.5,
    powerpoint_slideshow_delay: float = 0.25,
    powerpoint_open_timeout: float = 30.0,
    powerpoint_capture_timeout: float = 3.0,
    powerpoint_use_keys: bool = False,
    powerpoint_no_reopen: bool = False,
) -> PptxRenderer:
    """Resolve the configured visual renderer."""

    selected = (
        (renderer_name or os.getenv("SVG2OOXML_VISUAL_RENDERER") or "soffice")
        .strip()
        .lower()
    )
    if selected in {"soffice", "libreoffice"}:
        if soffice_path:
            return LibreOfficeRenderer(
                soffice_path=soffice_path,
                timeout=timeout,
                user_installation=user_installation,
                png_dpi=_resolve_png_dpi(),
            )
        return default_renderer(timeout=timeout, user_installation=user_installation)
    if selected == "powerpoint":
        return PowerPointRenderer(
            backend=powerpoint_backend,
            delay=powerpoint_delay,
            slideshow_delay=powerpoint_slideshow_delay,
            open_timeout=powerpoint_open_timeout,
            capture_timeout=powerpoint_capture_timeout,
            use_keys=powerpoint_use_keys,
            allow_reopen=not powerpoint_no_reopen,
        )
    raise ValueError(f"Unknown visual renderer: {selected!r}")


def _resolve_png_dpi() -> float | None:
    env_value = os.getenv("SVG2OOXML_SOFFICE_PNG_DPI")
    if env_value is None or env_value == "":
        return 96.0
    if env_value.lower() in {"none", "off", "false"}:
        return None
    try:
        dpi = float(env_value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid SVG2OOXML_SOFFICE_PNG_DPI value: {env_value!r}"
        ) from exc
    if dpi <= 0:
        return None
    return dpi


def _slide_size_to_pixels(pptx_path: Path, dpi: float) -> tuple[int, int] | None:
    size_emu = _read_slide_size_emu(pptx_path)
    if size_emu is None:
        return None
    width_emu, height_emu = size_emu
    emu_per_inch = 914400
    width_px = int(round((width_emu / emu_per_inch) * dpi))
    height_px = int(round((height_emu / emu_per_inch) * dpi))
    if width_px <= 0 or height_px <= 0:
        return None
    return (width_px, height_px)


def _pptx_slide_count(pptx_path: Path) -> int:
    try:
        with zipfile.ZipFile(pptx_path, "r") as archive:
            count = sum(
                1
                for name in archive.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
    except (FileNotFoundError, zipfile.BadZipFile, OSError):
        return 1
    return max(1, count)


def _natural_sort_key(path: Path) -> tuple[object, ...]:
    parts = re.split(r"(\d+)", path.name)
    key: list[object] = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part)
    return tuple(key)


def _read_slide_size_emu(pptx_path: Path) -> tuple[int, int] | None:
    try:
        with zipfile.ZipFile(pptx_path, "r") as archive:
            xml = archive.read("ppt/presentation.xml")
    except (KeyError, FileNotFoundError, zipfile.BadZipFile) as exc:
        logger.warning("Unable to read presentation.xml from %s: %s", pptx_path, exc)
        return None

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.warning("Unable to parse presentation.xml from %s: %s", pptx_path, exc)
        return None

    ns = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
    node = root.find("p:sldSz", ns)
    if node is None:
        return None
    try:
        return (int(node.attrib["cx"]), int(node.attrib["cy"]))
    except (KeyError, ValueError):
        return None


__all__ = [
    "LibreOfficeRenderer",
    "PptxRenderer",
    "PowerPointRenderer",
    "RenderedSlideSet",
    "VisualRendererError",
    "default_renderer",
    "resolve_renderer",
]
