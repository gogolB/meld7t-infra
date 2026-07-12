"""Offline, white-label combined research report renderer.

The worker renders a versioned database snapshot rather than querying mutable request state while
drawing. Pillow is already part of the pinned DICOM dependency closure, so this keeps report
generation offline and avoids a browser/HTML-to-PDF runtime in production.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps


PAGE = (1240, 1754)  # A4-ish at 150 DPI
MARGIN = 82
FOOTER_Y = PAGE[1] - 72
CONTENT_BOTTOM = FOOTER_Y - 36
_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")
MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_LOGO_PIXELS = 4_000_000
MAX_FRAME_BYTES = 25 * 1024 * 1024
MAX_FRAME_PIXELS = 16_000_000
MAX_RASTER_DIMENSION = 8_192


def _color(value: Any, fallback: str) -> str:
    raw = str(value or "")
    return raw if _HEX.fullmatch(raw) else fallback


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    names = ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf") if bold else (
        "DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


F_TITLE = _font(42, bold=True)
F_H1 = _font(30, bold=True)
F_H2 = _font(23, bold=True)
F_BODY = _font(18)
F_BODY_BOLD = _font(18, bold=True)
F_SMALL = _font(14)
F_SMALL_BOLD = _font(14, bold=True)


def _safe_text(value: Any, limit: int = 4000) -> str:
    text = str(value if value is not None else "").replace("\0", "")
    return " ".join(text.split())[:limit]


def _human(value: Any) -> str:
    return _safe_text(value).replace("_", " ").strip().title() or "—"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class _Brand:
    product: str
    institution: str
    department: str
    primary: str
    secondary: str
    footer: str
    logo_path: str | None
    logo_sha256: str | None
    logo_size: int | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "_Brand":
        return cls(
            product=_safe_text(raw.get("product_name") or "MELD 7T", 120),
            institution=_safe_text(raw.get("institution_name") or "Research Institution", 160),
            department=_safe_text(raw.get("department_name") or "", 160),
            primary=_color(raw.get("primary_color"), "#124A7E"),
            secondary=_color(raw.get("secondary_color"), "#749ABB"),
            footer=_safe_text(raw.get("footer_text") or "Research use only", 240),
            logo_path=_safe_text(raw.get("logo_path"), 1024) or None,
            logo_sha256=(str(raw.get("logo_sha256"))
                         if re.fullmatch(r"[0-9a-f]{64}", str(
                             raw.get("logo_sha256") or "")) else None),
            logo_size=(raw.get("logo_size") if isinstance(raw.get("logo_size"), int)
                       and not isinstance(raw.get("logo_size"), bool) else None),
        )


class _Document:
    def __init__(self, brand: _Brand, *, report_kind: str, warnings: list[str]):
        self.brand = brand
        self.report_kind = report_kind
        self.warnings = [_safe_text(item, 1200) for item in warnings if _safe_text(item)]
        self.pages: list[Image.Image] = []
        self.page: Image.Image
        self.draw: ImageDraw.ImageDraw
        self.y = 0
        self.new_page()

    def _header(self) -> None:
        self.draw.rectangle((0, 0, PAGE[0], 18), fill=self.brand.primary)
        x = MARGIN
        logo = self._load_logo()
        if logo is not None:
            self.page.alpha_composite(logo, (MARGIN, 38))
            x += logo.width + 28
        self.draw.text((x, 44), self.brand.institution, font=F_BODY_BOLD,
                       fill=self.brand.primary)
        if self.brand.department:
            self.draw.text((x, 72), self.brand.department, font=F_SMALL, fill="#4B5966")
        self.draw.text((PAGE[0] - MARGIN, 48), self.brand.product, font=F_BODY_BOLD,
                       fill=self.brand.primary, anchor="ra")
        self.draw.text((PAGE[0] - MARGIN, 76), f"{_human(self.report_kind)} report",
                       font=F_SMALL, fill="#4B5966", anchor="ra")
        self.draw.line((MARGIN, 116, PAGE[0] - MARGIN, 116), fill="#D5DDE5", width=2)

    def _load_logo(self) -> Image.Image | None:
        if not self.brand.logo_path:
            return None
        path = Path(self.brand.logo_path)
        try:
            if (not self.brand.logo_sha256 or not self.brand.logo_size
                    or self.brand.logo_size > MAX_LOGO_BYTES
                    or not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or path.stat().st_size != self.brand.logo_size):
                return None
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != self.brand.logo_sha256:
                return None
            with Image.open(BytesIO(payload)) as source:
                width, height = source.size
                if (width < 1 or height < 1 or width > MAX_RASTER_DIMENSION
                        or height > MAX_RASTER_DIMENSION
                        or width * height > MAX_LOGO_PIXELS):
                    return None
                logo = ImageOps.contain(source.convert("RGBA"), (180, 56))
            return logo
        except (Image.DecompressionBombError, OSError, ValueError):
            return None

    def _footer(self, page_number: int) -> None:
        self.draw.line((MARGIN, FOOTER_Y - 18, PAGE[0] - MARGIN, FOOTER_Y - 18),
                       fill="#D5DDE5", width=2)
        footer = self.brand.footer or "Research use only"
        self.draw.text((MARGIN, FOOTER_Y), footer, font=F_SMALL_BOLD, fill=self.brand.primary)
        self.draw.text((PAGE[0] - MARGIN, FOOTER_Y), f"Page {page_number}", font=F_SMALL,
                       fill="#53616E", anchor="ra")

    def new_page(self) -> None:
        if getattr(self, "page", None) is not None:
            self._footer(len(self.pages) + 1)
            self.pages.append(self.page.convert("RGB"))
        self.page = Image.new("RGBA", PAGE, "white")
        self.draw = ImageDraw.Draw(self.page)
        self._header()
        self.y = 142
        if self.warnings:
            warning = "UNHARMONIZED RESULT — " + " ".join(self.warnings)
            self.box(warning, tone="warning", repeatable=True)

    def finish(self) -> list[Image.Image]:
        self._footer(len(self.pages) + 1)
        self.pages.append(self.page.convert("RGB"))
        return self.pages

    def ensure(self, height: int) -> None:
        if self.y + height > CONTENT_BOTTOM:
            self.new_page()

    def _lines(self, text: Any, font: ImageFont.ImageFont, width: int) -> list[str]:
        clean = _safe_text(text)
        if not clean:
            return ["—"]
        words = clean.split()
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if current and self.draw.textlength(trial, font=font) > width:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        return lines

    def text(self, value: Any, *, font: ImageFont.ImageFont = F_BODY,
             fill: str = "#1E2A35", indent: int = 0, gap: int = 9) -> None:
        lines = self._lines(value, font, PAGE[0] - 2 * MARGIN - indent)
        line_height = int(getattr(font, "size", 18) * 1.42)
        self.ensure(line_height * len(lines) + gap)
        for line in lines:
            self.draw.text((MARGIN + indent, self.y), line, font=font, fill=fill)
            self.y += line_height
        self.y += gap

    def heading(self, value: Any, *, level: int = 1) -> None:
        font = F_H1 if level == 1 else F_H2
        self.ensure(58)
        if level == 1:
            self.draw.rectangle((MARGIN, self.y + 7, MARGIN + 8, self.y + 39),
                                fill=self.brand.secondary)
            x = MARGIN + 22
        else:
            x = MARGIN
        self.draw.text((x, self.y), _safe_text(value, 240), font=font, fill=self.brand.primary)
        self.y += 52 if level == 1 else 42

    def box(self, value: Any, *, tone: str = "info", repeatable: bool = False) -> None:
        palette = {
            "warning": ("#FFF1CF", "#765600", "#D79A00"),
            "info": ("#EDF4FA", "#173A59", self.brand.secondary),
            "success": ("#E6F5EC", "#17653A", "#3A9B67"),
        }
        background, ink, border = palette.get(tone, palette["info"])
        lines = self._lines(value, F_SMALL_BOLD, PAGE[0] - 2 * MARGIN - 36)
        height = 28 + 22 * len(lines)
        if not repeatable:
            self.ensure(height + 12)
        self.draw.rounded_rectangle((MARGIN, self.y, PAGE[0] - MARGIN, self.y + height),
                                    radius=10, fill=background, outline=border, width=2)
        y = self.y + 13
        for line in lines:
            self.draw.text((MARGIN + 18, y), line, font=F_SMALL_BOLD, fill=ink)
            y += 22
        self.y += height + 14

    def key_values(self, values: Iterable[tuple[Any, Any]]) -> None:
        rows = [(str(key), _safe_text(value, 1000) or "—") for key, value in values]
        self.ensure(40 * len(rows) + 18)
        for index, (key, value) in enumerate(rows):
            if index % 2 == 0:
                self.draw.rectangle((MARGIN, self.y - 4, PAGE[0] - MARGIN, self.y + 30),
                                    fill="#F5F7F9")
            self.draw.text((MARGIN + 10, self.y), key, font=F_SMALL_BOLD, fill="#53616E")
            clipped = textwrap.shorten(value, width=115, placeholder="…")
            self.draw.text((MARGIN + 310, self.y), clipped, font=F_SMALL, fill="#1E2A35")
            self.y += 38
        self.y += 12

    def image(self, raw_path: Any, caption: str) -> None:
        if not isinstance(raw_path, dict):
            return
        path = Path(str(raw_path.get("path") or ""))
        expected = str(raw_path.get("sha256") or "")
        expected_size = raw_path.get("size")
        try:
            if (re.fullmatch(r"[0-9a-f]{64}", expected) is None
                    or isinstance(expected_size, bool) or not isinstance(expected_size, int)
                    or expected_size < 1 or expected_size > MAX_FRAME_BYTES
                    or not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or path.stat().st_size != expected_size):
                return
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != expected:
                return
            with Image.open(BytesIO(payload)) as source:
                width, height = source.size
                if (width < 1 or height < 1 or width > MAX_RASTER_DIMENSION
                        or height > MAX_RASTER_DIMENSION
                        or width * height > MAX_FRAME_PIXELS):
                    return
                rendered = ImageOps.contain(source.convert("RGB"),
                                             (PAGE[0] - 2 * MARGIN, 620))
        except (Image.DecompressionBombError, OSError, ValueError):
            return
        height = rendered.height + 54
        self.ensure(height)
        x = (PAGE[0] - rendered.width) // 2
        self.page.alpha_composite(rendered.convert("RGBA"), (x, self.y))
        self.y += rendered.height + 10
        self.draw.text((PAGE[0] // 2, self.y), _safe_text(caption, 220), font=F_SMALL,
                       fill="#53616E", anchor="ma")
        self.y += 38


def _report_warnings(snapshot: dict[str, Any]) -> list[str]:
    warnings = snapshot.get("warnings") or []
    return list(dict.fromkeys(_safe_text(item, 1200) for item in warnings if _safe_text(item)))


def _harmonization_label(run: dict[str, Any]) -> str:
    harmonization = run.get("harmonization") or {}
    if harmonization.get("mode") == "harmonized":
        profile = harmonization.get("profile") or {}
        return f"Applied: {profile.get('code', 'profile')} v{profile.get('version', '—')} " \
               f"({profile.get('method', 'method not recorded')})"
    if harmonization.get("mode") == "not_applicable":
        return "Not applicable to this detector"
    return "NOT APPLIED — interpret with the unharmonized warning"


def _detector_measurement_rows(result: dict[str, Any]) -> list[tuple[str, str]]:
    """Render only approved measurements; never stringify internal artifact/source paths."""
    summary = result.get("detector_summary")
    if not isinstance(summary, dict):
        return []
    allowed = (
        "volumes_mm3", "subfields_mm3", "dseg_space", "asymmetry_index_pct", "flagged",
        "ai_threshold_pct",
    )
    rows: list[tuple[str, str]] = []
    for key in allowed:
        if key not in summary:
            continue
        value = summary[key]
        rendered = (json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
                    if isinstance(value, (dict, list)) else str(value))
        rows.append((_human(key), _safe_text(rendered, 3000)))
    return rows


def render_case_report(snapshot: dict[str, Any], branding: dict[str, Any],
                       output_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Render a new immutable combined report and return its artifact manifest entry."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("case"), dict):
        raise ValueError("report snapshot requires a case object")
    report_kind = str(snapshot.get("report_kind") or "preliminary")
    if report_kind not in {"preliminary", "final"}:
        raise ValueError("report_kind must be preliminary or final")
    destination = Path(output_path)
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise ValueError("report output must be a new absolute path")
    destination.parent.mkdir(parents=True, exist_ok=True)

    brand = _Brand.from_dict(branding)
    doc = _Document(brand, report_kind=report_kind, warnings=_report_warnings(snapshot))
    case = snapshot["case"]
    created = _safe_text(snapshot.get("created_at") or datetime.now(timezone.utc).isoformat())

    doc.text(_human(report_kind) + " combined research report", font=F_TITLE,
             fill=brand.primary, gap=4)
    doc.text(brand.product, font=F_H2, fill=brand.secondary)
    if report_kind == "preliminary":
        doc.box("Preliminary automated analysis. Adjudications may be incomplete.", tone="info")
    else:
        doc.box("Final versioned snapshot generated after current detector reviews.", tone="success")
    doc.key_values([
        ("Research case", case.get("pseudonym")),
        ("Workup", case.get("workup")),
        ("Report version", snapshot.get("version")),
        ("Generated", created),
    ])
    doc.box("RESEARCH USE ONLY — NOT FOR DIAGNOSIS OR TREATMENT", tone="warning")

    doc.heading("Input scans and processing plan")
    source_series = snapshot.get("source_series") or []
    if not source_series:
        doc.text("No source-series inventory was available in this snapshot.", fill="#53616E")
    for series in source_series:
        doc.heading(series.get("series_description") or "Unnamed series", level=2)
        doc.key_values([
            ("Modality", series.get("modality")),
            ("Images", series.get("instance_count")),
            ("Confirmed role", series.get("confirmed_role") or series.get("proposed_role")),
        ])

    for row in snapshot.get("runs") or []:
        run = row.get("run") or {}
        raw_result = row.get("result")
        doc.heading(_human(run.get("detector_id")) + " result")
        doc.key_values([
            ("Source role", run.get("source_role")),
            ("Run status", run.get("status")),
            ("Detector version", run.get("detector_version")),
            ("Harmonization", (_harmonization_label(run)
                               if isinstance(raw_result, dict) else "Not run")),
            ("Findings", (len(row.get("clusters") or [])
                          if isinstance(raw_result, dict) else "Not run")),
        ])
        if not isinstance(raw_result, dict):
            doc.box(
                "This detector was declared in the processing plan but was not run; no result "
                "or negative finding is asserted in this report version.",
                tone="info",
            )
            continue
        result = raw_result
        for warning in run.get("warnings") or []:
            doc.box(warning, tone="warning")
        clusters = row.get("clusters") or []
        if not clusters:
            doc.text("No findings above this detector's operating point.", fill="#53616E")
        for cluster in clusters:
            label = f"Finding #{cluster.get('index', '—')} · " \
                    f"{_human(cluster.get('hemi'))} {_safe_text(cluster.get('location'))}"
            doc.text(label, font=F_BODY_BOLD, gap=2)
            doc.text(f"Detector size: {cluster.get('size', '—')} · detector score: "
                     f"{cluster.get('confidence', '—')}", font=F_SMALL, fill="#53616E")
        detector_measurements = _detector_measurement_rows(result)
        if detector_measurements:
            doc.heading("Detector measurements", level=2)
            doc.key_values(detector_measurements)
        for image in (row.get("frame_paths") or [])[:4]:
            doc.image(image, f"{_human(run.get('detector_id'))} review image")

    doc.heading("Review history")
    adjudications = snapshot.get("adjudications") or []
    if not adjudications:
        doc.text("No adjudications were present when this report version was generated.",
                 fill="#53616E")
    for review in adjudications:
        doc.key_values([
            ("Detector/run", review.get("detector_id") or review.get("run_id")),
            ("Reviewer", review.get("reviewer")),
            ("Assessment", "Agree" if review.get("agree") is True else
             ("Disagree" if review.get("agree") is False else "Not recorded")),
            ("Confidence", review.get("confidence")),
            ("Recorded", review.get("ts")),
            ("Notes", review.get("notes") or "—"),
        ])

    doc.heading("Provenance")
    doc.key_values([
        ("Recipe hash", snapshot.get("recipe", {}).get("spec_hash")),
        ("Release manifest", snapshot.get("release_manifest_digest")),
        ("Snapshot SHA-256", snapshot.get("snapshot_sha256")),
    ])

    pages = doc.finish()
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        pages[0].save(
            temporary, format="PDF", resolution=150.0, save_all=True,
            append_images=pages[1:], title=f"{brand.product} combined research report",
            author=brand.institution, subject=f"{_human(report_kind)} research analysis",
            keywords="research only, MAP, MELD, HippUnfold, harmonization",
        )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": str(destination),
        "sha256": _sha256(destination),
        "size": destination.stat().st_size,
        "page_count": len(pages),
        "media_type": "application/pdf",
    }
