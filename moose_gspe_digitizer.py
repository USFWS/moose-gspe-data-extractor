# -*- coding: utf-8 -*-
"""
Moose GSPE Datasheet Extractor (Azure).

This script manages the extraction and digitization of scanned Moose GSPE 
survey datasheets using Azure OpenAI Vision. It maps page coordinates, 
manages human verification reviews via PySide6, and separates raw vs. QC'ed 
ground-truth JSONs for model evaluation.

Each PDF page is treated as one unique survey unit page keyed by SU #.

.env keys beside this script:
    API_ENDPOINT=https://your-resource.openai.azure.com/
    API_KEY=...
    API_VERSION=2024-12-01-preview
    API_DEPLOYMENT_NAME=your-vision-model-deployment-name

Optional:
    MOOSE_PROMPT_PATH=prompt.md
    MOOSE_OUTPUT_DIR=moose_output
    MOOSE_MAX_WORKERS=4
    MOOSE_REQUEST_TIMEOUT=0
    MOOSE_MAX_RETRIES=4
    MOOSE_RETRY_BASE_DELAY=1.0
    MOOSE_RETRY_MAX_DELAY=30.0
    AZURE_INCLUDE_REASONING_EFFORT=0
    REASONING_EFFORT=medium
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import io
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import sys

class LazyLoader:
    def __init__(self, name):
        self.name = name
        self._module = None

    def __getattr__(self, item):
        if self._module is None:
            import importlib
            self._module = importlib.import_module(self.name)
        return getattr(self._module, item)

pd = LazyLoader("pandas")

from dotenv import load_dotenv, dotenv_values
class LazyPILLoader:
    def __getattr__(self, item):
        from PIL import Image
        return getattr(Image, item)

class LazyPILEnhanceLoader:
    def __getattr__(self, item):
        from PIL import ImageEnhance
        return getattr(ImageEnhance, item)

class LazyPILFilterLoader:
    def __getattr__(self, item):
        from PIL import ImageFilter
        return getattr(ImageFilter, item)

Image = LazyPILLoader()
ImageEnhance = LazyPILEnhanceLoader()
ImageFilter = LazyPILFilterLoader()


try:
    import fitz  # PyMuPDF
    PDF_SUPPORT_ENABLED = True
except Exception:
    fitz = None
    PDF_SUPPORT_ENABLED = False

try:
    from openai import AzureOpenAI
except Exception as exc:
    AzureOpenAI = None
    _OPENAI_IMPORT_ERROR = exc
else:
    _OPENAI_IMPORT_ERROR = None

APP_VERSION = "0.0.1"

SCRIPT_DIR = Path(__file__).resolve().parent
_DOTENV_PATH = SCRIPT_DIR / ".env"
if _DOTENV_PATH.exists():
    load_dotenv(_DOTENV_PATH, override=True)

DEFAULT_OUTPUT_DIR = SCRIPT_DIR / os.getenv("MOOSE_OUTPUT_DIR", "moose_output")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "moose_gspe_data.sqlite"
_PROMPT_ENV = os.getenv("MOOSE_PROMPT_PATH", "").strip()
DEFAULT_PROMPT_PATH = SCRIPT_DIR / (_PROMPT_ENV if _PROMPT_ENV else "prompt.md")
RENDER_DPI = int(os.getenv("MOOSE_RENDER_DPI", "150"))
MAX_WORKERS = int(os.getenv("MOOSE_MAX_WORKERS", "4"))
# Per-request Azure call timeout in seconds. 0 or empty disables it (no bound,
# the default). When set, any single page request that exceeds this is aborted,
# which also bounds how long a cancelled/detached worker can linger.
try:
    _rt = float(os.getenv("MOOSE_REQUEST_TIMEOUT", "0") or "0")
except ValueError:
    _rt = 0.0
REQUEST_TIMEOUT: Optional[float] = _rt if _rt > 0 else None

# Transient-error retry policy. Applies to 429 (rate limit), 5xx, and
# connection/timeout errors. Non-transient errors (for example parameter
# rejections) are never retried here.
MAX_RETRIES = int(os.getenv("MOOSE_MAX_RETRIES", "4"))
RETRY_BASE_DELAY = float(os.getenv("MOOSE_RETRY_BASE_DELAY", "1.0"))
RETRY_MAX_DELAY = float(os.getenv("MOOSE_RETRY_MAX_DELAY", "30.0"))

REASONING_EFFORT = os.getenv("REASONING_EFFORT", "medium").strip().lower()
INCLUDE_REASONING_EFFORT = os.getenv("AZURE_INCLUDE_REASONING_EFFORT", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
# Spike flag for structured outputs. When on, requests ask the model to conform
# to a JSON schema (response_format json_schema) instead of only valid JSON.
# Support varies by deployment and API version, so this defaults off. If the
# deployment rejects it, extract_page falls back to json_object automatically.
USE_JSON_SCHEMA = os.getenv("MOOSE_USE_JSON_SCHEMA", "0").strip().lower() in {
    "1", "true", "yes", "on"
}

COUNT_COLS = [
    "bull_yrlg", "bull_med", "bull_lrg", "cow_w0", "cow_w1", "cow_w2", "cow_w3",
    "lone_calf", "unknown",
]
GROUP_COLS = ["group_no"] + COUNT_COLS + ["total_moose", "computed_total_moose", "total_moose_check", "remarks_waypoint_latlon", "llm_notes"]
SUMMARY_KEYS = ["groups_1_14", "groups_14_48", "total"]

CONDITION_COLS = [
    "classification_errors",
    "uncooperative_pilot",
    "inadequate_search_effort",
    "short_on_fuel",
    "windy_turbulent",
    "other_explain",
    "inadequate_snow_cover",
    "inexperienced_pilot",
    "movement_in_out_of_intensive",
    "movement_in_out_of_su",
    "improper_aircraft",
    "poor_light",
    "inexperienced_observer",
    "too_many_moose_in_intensive_gt15",
    "observer_airsick",
    "low_clouds_or_fog",
    "poor_visibility_snow_on_trees",
    "problems_finding_su_boundaries",
    "observer_sleeping",
]

UNIT_COLS = [
    "survey_unit_guid", "source_file", "source_page", "survey_date", "su_number", "gmu", "start_time", "stop_time",
    "elapsed", "computed_elapsed_minutes", "computed_elapsed_hhmm", "location", "observer", "pilot",
    "strata", "aircraft_type", "temp_f", "area_mi2", "overall_survey_rating", "snow_age",
    "light_type", "snow_cover", "light_intensity", "predominant_habitat_type",
    "predominant_habitat_other", "total_number_moose", "validation_notes", "llm_notes",
] + CONDITION_COLS


def load_azure_config_from_script_env() -> dict[str, str]:
    if not _DOTENV_PATH.exists():
        return {}
    vals = dotenv_values(_DOTENV_PATH)
    return {k: str(vals.get(k) or "").strip() for k in (
        "API_ENDPOINT", "API_KEY", "API_VERSION", "API_DEPLOYMENT_NAME"
    )}


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def data_url(image_bytes: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"


def read_prompt(prompt_path: Path = DEFAULT_PROMPT_PATH) -> str:
    candidates = [prompt_path]
    # Backward-compatible fallback: support either extensionless `prompt`
    # or the older `prompt.md` filename.
    if prompt_path.name == "prompt":
        candidates.append(SCRIPT_DIR / "prompt.md")
    elif prompt_path.name == "prompt.md":
        candidates.append(SCRIPT_DIR / "prompt")
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"Prompt file not found. Place `prompt.md` beside this script or set MOOSE_PROMPT_PATH."
    )


def build_prompt(source_file: str, page_number: int, page_count: int) -> str:
    base = read_prompt()
    context = (
        f"\n\n# Current page context\n"
        f"source_file: {source_file}\n"
        f"page_number: {page_number}\n"
        f"page_count: {page_count}\n"
        f"Remember: this single page is one survey unit.\n"
    )
    return base + context


def preprocess_image(image_bytes: bytes, method: str = "none") -> tuple[bytes, str]:
    """Return (processed_bytes, mime_type)."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if method == "none":
            fmt = img.format or "PNG"
            return image_bytes, Image.MIME.get(fmt.upper(), "image/png")
        if method == "grayscale":
            img = img.convert("L")
        elif method == "grayscale_contrast":
            img = ImageEnhance.Contrast(img.convert("L")).enhance(1.8)
        elif method == "color_contrast":
            img = ImageEnhance.Contrast(img).enhance(1.6)
            img = ImageEnhance.Color(img).enhance(1.4)
        elif method == "sharpen":
            img = img.filter(ImageFilter.SHARPEN)
        elif method == "threshold_simple":
            img = img.convert("L").point(lambda p: 255 if p > 150 else 0, mode="1")
        else:
            # Unknown method: do not silently transform. Pass through unchanged
            # and warn so a typo in an env var or script does not quietly alter
            # the image sent to the model.
            print(f"WARN: unknown preprocess method {method!r}; passing image through unchanged", file=sys.stderr)
            fmt = img.format or "PNG"
            return image_bytes, Image.MIME.get(fmt.upper(), "image/png")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    except Exception as exc:
        print(f"WARN: preprocessing failed; using original bytes ({exc})", file=sys.stderr)
        return image_bytes, "image/png"


def render_file_to_page_images(file_path: Path, dpi: int = RENDER_DPI, preprocess: str = "none") -> list[tuple[int, int, bytes, str]]:
    """Return list of (page_number, page_count, bytes, mime_type)."""
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        if not PDF_SUPPORT_ENABLED:
            raise RuntimeError("PDF support requires PyMuPDF. Install with: pip install pymupdf")
        doc = fitz.open(str(file_path))
        try:
            zoom = dpi / 72
            matrix = fitz.Matrix(zoom, zoom)
            page_count = doc.page_count
            out: list[tuple[int, int, bytes, str]] = []
            for idx in range(page_count):
                page = doc.load_page(idx)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                b, m = preprocess_image(pix.tobytes("png"), preprocess)
                out.append((idx + 1, page_count, b, m))
            return out
        finally:
            doc.close()
    if ext in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}:
        b, m = preprocess_image(file_path.read_bytes(), preprocess)
        return [(1, 1, b, m)]
    raise ValueError(f"Unsupported file extension: {ext}")


def pdf_text_report(pdf_path: Path, min_chars_per_page: int = 50) -> dict[str, Any]:
    """Diagnostic only: checks whether the PDF already has a text layer.

    Retained for CLI --preflight-only mode. The normal extraction path uses
    render_and_report, which gathers the same stats during rendering without a
    second document open.
    """
    if not PDF_SUPPORT_ENABLED:
        return {"pdf_path": str(pdf_path), "error": "PyMuPDF not installed"}
    doc = fitz.open(str(pdf_path))
    try:
        pages = []
        total_chars = 0
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            n = len(text.strip())
            total_chars += n
            pages.append({"page": i, "char_count": n, "has_text_layer": n >= min_chars_per_page})
        return {
            "pdf_path": str(pdf_path),
            "page_count": doc.page_count,
            "total_chars": total_chars,
            "has_meaningful_text": total_chars >= min_chars_per_page * max(1, doc.page_count),
            "pages": pages,
        }
    finally:
        doc.close()


def render_and_report(
    file_path: Path,
    dpi: int = RENDER_DPI,
    preprocess: str = "none",
    min_chars_per_page: int = 50,
) -> tuple[list[tuple[int, int, bytes, str]], Optional[dict[str, Any]]]:
    """Render page images and, for PDFs, gather the text-layer report in a
    single document open.

    Returns (page_images, report_or_None). This avoids opening the same PDF
    twice (once to render, once for pdf_text_report). For non-PDF inputs the
    report is None.
    """
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        if not PDF_SUPPORT_ENABLED:
            raise RuntimeError("PDF support requires PyMuPDF. Install with: pip install pymupdf")
        doc = fitz.open(str(file_path))
        try:
            zoom = dpi / 72
            matrix = fitz.Matrix(zoom, zoom)
            page_count = doc.page_count
            images: list[tuple[int, int, bytes, str]] = []
            pages_report = []
            total_chars = 0
            for idx in range(page_count):
                page = doc.load_page(idx)
                text = page.get_text("text") or ""
                n = len(text.strip())
                total_chars += n
                pages_report.append({"page": idx + 1, "char_count": n, "has_text_layer": n >= min_chars_per_page})
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                b, m = preprocess_image(pix.tobytes("png"), preprocess)
                images.append((idx + 1, page_count, b, m))
            report = {
                "pdf_path": str(file_path),
                "page_count": page_count,
                "total_chars": total_chars,
                "has_meaningful_text": total_chars >= min_chars_per_page * max(1, page_count),
                "pages": pages_report,
            }
            return images, report
        finally:
            doc.close()
    images = render_file_to_page_images(file_path, dpi=dpi, preprocess=preprocess)
    return images, None


def strip_json_fences(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def extract_json(text: str) -> dict[str, Any]:
    s = strip_json_fences(text)
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Fallback: scan from the first "{" for the first balanced top-level object,
    # respecting string literals and escapes so that braces inside strings (for
    # example in remarks text) do not miscount depth. This is more robust than
    # find("{")/rfind("}"), which can grab a reasoning preamble or trailing
    # fragment and produce subtly wrong JSON instead of a clean failure.
    start = s.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break
    raise ValueError("Model response did not contain a JSON object")


def build_response_schema() -> dict[str, Any]:
    """Build the json_schema response_format payload matching prompt.md's shape.

    Strict structured outputs require every property to be listed in "required"
    and additionalProperties=false. Nullable fields use a ["type", "null"] union.
    Count fields are integers; nullable fields that the model may leave empty are
    typed to allow null. This is a spike aid: it only takes effect when
    USE_JSON_SCHEMA is on.
    """
    def nullable(*types: str) -> dict[str, Any]:
        return {"type": list(types) + ["null"]}

    count_props = {c: {"type": "integer"} for c in COUNT_COLS}

    survey_unit_props = {
        "survey_date": nullable("string"),
        "su_number": nullable("string"),
        "gmu": nullable("string"),
        "start_time": nullable("string"),
        "stop_time": nullable("string"),
        "elapsed": nullable("string", "integer"),
        "location": nullable("string"),
        "observer": nullable("string"),
        "pilot": nullable("string"),
        "strata": nullable("string"),
        "aircraft_type": nullable("string"),
        "temp_f": nullable("number"),
        "area_mi2": nullable("number"),
        "overall_survey_rating": nullable("string"),
        "snow_age": nullable("string"),
        "light_type": nullable("string"),
        "snow_cover": nullable("string"),
        "light_intensity": nullable("string"),
        "predominant_habitat_type": nullable("string"),
        "predominant_habitat_other": nullable("string"),
        "total_number_moose": nullable("integer"),
        "llm_notes": {"type": "array", "items": {"type": "string"}},
    }

    condition_props: dict[str, Any] = {}
    for c in CONDITION_COLS:
        condition_props[c] = nullable("string") if c == "other_explain" else {"type": "boolean"}

    group_props = {"group_no": nullable("integer")}
    group_props.update({c: {"type": "integer"} for c in COUNT_COLS})
    group_props["total_moose"] = nullable("integer")
    group_props["remarks_waypoint_latlon"] = nullable("string")
    group_props["llm_notes"] = {"type": "array", "items": {"type": "string"}}

    def summary_row_schema() -> dict[str, Any]:
        props = dict(count_props)
        props["total_moose"] = nullable("integer")
        return {
            "type": "object",
            "properties": props,
            "required": list(props.keys()),
            "additionalProperties": False,
        }

    schema = {
        "type": "object",
        "properties": {
            "survey_unit": {
                "type": "object",
                "properties": survey_unit_props,
                "required": list(survey_unit_props.keys()),
                "additionalProperties": False,
            },
            "additional_conditions": {
                "type": "object",
                "properties": condition_props,
                "required": list(condition_props.keys()),
                "additionalProperties": False,
            },
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": group_props,
                    "required": list(group_props.keys()),
                    "additionalProperties": False,
                },
            },
            "summary": {
                "type": "object",
                "properties": {k: summary_row_schema() for k in SUMMARY_KEYS},
                "required": list(SUMMARY_KEYS),
                "additionalProperties": False,
            },
            "validation_notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["survey_unit", "additional_conditions", "groups", "summary", "validation_notes"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {"name": "moose_gspe_page", "strict": True, "schema": schema},
    }


class AzureMooseExtractor:
    def __init__(self, config: Optional[dict[str, str]] = None):
        if AzureOpenAI is None:
            raise RuntimeError(f"openai package import failed: {_OPENAI_IMPORT_ERROR}")
        config = config or load_azure_config_from_script_env()
        endpoint = str(config.get("API_ENDPOINT") or os.getenv("API_ENDPOINT") or "").strip()
        key = str(config.get("API_KEY") or os.getenv("API_KEY") or "").strip()
        version = str(config.get("API_VERSION") or os.getenv("API_VERSION") or "").strip()
        deployment = str(config.get("API_DEPLOYMENT_NAME") or os.getenv("API_DEPLOYMENT_NAME") or "").strip()
        missing = [n for n, v in [
            ("API_ENDPOINT", endpoint), ("API_KEY", key), ("API_VERSION", version), ("API_DEPLOYMENT_NAME", deployment)
        ] if not v]
        if missing:
            raise ValueError(f"Azure OpenAI env vars missing: {', '.join(missing)}")
        client_kwargs: dict[str, Any] = {
            "azure_endpoint": endpoint, "api_key": key, "api_version": version,
        }
        if REQUEST_TIMEOUT is not None:
            client_kwargs["timeout"] = REQUEST_TIMEOUT
        self.client = AzureOpenAI(**client_kwargs)
        self.deployment = deployment
        self.api_version = version
        self.request_timeout = REQUEST_TIMEOUT

    @staticmethod
    def _classify_error(exc: Exception) -> tuple[bool, Optional[float]]:
        """Return (is_transient, retry_after_seconds).

        Transient = worth retrying: HTTP 429 or 5xx, or a connection/timeout
        error. Everything else (for example a rejected parameter) is permanent
        and must not be retried, so the compatibility path can handle it.
        """
        status = getattr(exc, "status_code", None)
        name = type(exc).__name__.lower()
        retry_after: Optional[float] = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                ra = resp.headers.get("retry-after")
                if ra is not None:
                    retry_after = float(ra)
            except Exception:
                retry_after = None
        transient = False
        if isinstance(status, int) and (status == 429 or 500 <= status < 600):
            transient = True
        if any(k in name for k in (
            "ratelimit", "timeout", "apiconnection", "internalserver", "serviceunavailable", "apierror"
        )):
            transient = True
        return transient, retry_after

    def _create_with_retries(self, **kwargs):
        """Call chat.completions.create with bounded exponential backoff on
        transient errors. Permanent errors raise on the first attempt so callers
        (the parameter-compatibility path) can react to them.

        If a cancel_event is attached (set by a cancelled GUI worker), stop
        retrying promptly instead of sleeping out the backoff and re-sending the
        request. Without this, a cancel during an in-flight call would be seen as
        a transient connection error and the loop would keep WARNing, sleeping,
        and retrying, which both spams the log and blocks the worker's thread
        pool from shutting down.
        """
        cancel_event = getattr(self, "cancel_event", None)
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("extraction cancelled")
            try:
                return self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                last_exc = exc
                transient, retry_after = self._classify_error(exc)
                if not transient or attempt == MAX_RETRIES - 1:
                    raise
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("extraction cancelled")
                if retry_after is not None:
                    delay = retry_after
                else:
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                delay += random.uniform(0, RETRY_BASE_DELAY)  # jitter
                print(
                    f"WARN: transient Azure error ({type(exc).__name__}); "
                    f"retry {attempt + 1}/{MAX_RETRIES - 1} in {delay:.1f}s",
                    file=sys.stderr,
                )
                # Interruptible sleep: wake immediately if cancelled mid-backoff.
                if cancel_event is not None:
                    if cancel_event.wait(delay):
                        raise InterruptedError("extraction cancelled")
                else:
                    time.sleep(delay)
        # Should not reach here, but satisfy the type checker.
        raise last_exc  # type: ignore[misc]

    def extract_page(
        self, 
        image_bytes: bytes, 
        mime_type: str, 
        prompt: str, 
        max_completion_tokens: int = 8000,
        few_shot_image_path: Optional[Path] = None,
        few_shot_json_path: Optional[Path] = None
    ) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": "You extract structured data from scanned handwritten moose GSPE survey forms."}
        ]
        
        # Inject Few-Shot Example dynamically if reference assets are provided
        if few_shot_image_path and few_shot_json_path:
            try:
                fs_path = Path(few_shot_image_path)
                fs_j_path = Path(few_shot_json_path)
                if fs_path.exists() and fs_j_path.exists():
                    fs_img_bytes = fs_path.read_bytes()
                    fs_mime = "image/png" if fs_path.suffix.lower() == ".png" else "image/jpeg"
                    fs_img_url = data_url(fs_img_bytes, fs_mime)
                    fs_json_content = fs_j_path.read_text(encoding="utf-8")
                    
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Extract all data from this moose GSPE datasheet according to the system rules."},
                            {"type": "image_url", "image_url": {"url": fs_img_url, "detail": "high"}}
                        ]
                    })
                    messages.append({
                        "role": "assistant",
                        "content": fs_json_content
                    })
                    print(f"[Few-Shot] Injected verified reference page: {fs_path.name}")
            except Exception as e:
                print(f"WARN: Failed to inject few-shot helper: {e}", file=sys.stderr)
                
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url(image_bytes, mime_type), "detail": "high"}},
            ]
        })
        base_kwargs: dict[str, Any] = {
            "model": self.deployment,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": max_completion_tokens,
            "response_format": build_response_schema() if USE_JSON_SCHEMA else {"type": "json_object"},
        }
        if INCLUDE_REASONING_EFFORT:
            base_kwargs["extra_body"] = {"reasoning_effort": REASONING_EFFORT}
        # Per-request timeout belt-and-suspenders in addition to the client
        # default, so the bound holds even if the client was built elsewhere.
        if self.request_timeout is not None:
            base_kwargs["timeout"] = self.request_timeout

        used_fallback_params = False
        try:
            resp = self._create_with_retries(**base_kwargs)
        except Exception as first_exc:
            msg = f"{type(first_exc).__name__}: {first_exc}".lower()
            # Compatibility retry for Azure deployments that reject optional parameters.
            retry_kwargs = dict(base_kwargs)
            if "max_completion_tokens" in msg and ("unsupported" in msg or "unrecognized" in msg):
                retry_kwargs.pop("max_completion_tokens", None)
                retry_kwargs["max_tokens"] = max_completion_tokens
            if "response_format" in msg or "json_object" in msg or "json_schema" in msg or "schema" in msg:
                # Schema or json_object not supported on this deployment; fall
                # back to a plain request. extract_json still parses the result.
                retry_kwargs.pop("response_format", None)
            if "reasoning_effort" in msg or "extra_body" in msg:
                retry_kwargs.pop("extra_body", None)
            if "temperature" in msg and ("unsupported" in msg or "unrecognized" in msg):
                retry_kwargs.pop("temperature", None)
            if retry_kwargs == base_kwargs:
                # Nothing to adjust; the original error was not a parameter
                # problem we know how to fix, so do not mask it.
                raise
            used_fallback_params = True
            try:
                resp = self._create_with_retries(**retry_kwargs)
            except Exception:
                raise first_exc

        raw = resp.choices[0].message.content if resp.choices else ""
        usage = getattr(resp, "usage", None)
        obj = extract_json(raw)
        return {
            "json": obj,
            "raw_text": raw,
            "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
            "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
            "used_fallback_params": used_fallback_params,
        }


def clean_text(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in {"null", "none", "nan", "n/a", "na"}:
            return None
        return " ".join(s.split())
    return v


def to_int_or_none(v: Any) -> Optional[int]:
    v = clean_text(v)
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return None


def to_float_or_none(v: Any) -> Optional[float]:
    v = clean_text(v)
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


def count_or_zero(v: Any) -> int | None:
    n = to_int_or_none(v)
    return 0 if n is None and clean_text(v) is None else n


def normalize_time(v: Any) -> Optional[str]:
    s = clean_text(v)
    if s is None:
        return None
    s = str(s).lower().replace(" ", "")
    s = s.replace(".", ":").replace(";", ":").replace(",", ":")
    ampm = None
    m = re.search(r"(am|pm)$", s)
    if m:
        ampm = m.group(1)
        s = s[:-2]
    if ":" in s:
        parts = s.split(":")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            h = int(parts[0])
            mn = int(parts[1][:2])
        else:
            return None
    else:
        digits = re.sub(r"\D", "", s)
        if not digits:
            return None
        if len(digits) <= 2:
            h, mn = int(digits), 0
        elif len(digits) == 3:
            h, mn = int(digits[0]), int(digits[1:])
        else:
            h, mn = int(digits[-4:-2]), int(digits[-2:])
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if 0 <= h <= 23 and 0 <= mn <= 59:
        return f"{h:02d}:{mn:02d}"
    return None


def elapsed_minutes(start_time: Any, stop_time: Any) -> Optional[int]:
    st = normalize_time(start_time)
    sp = normalize_time(stop_time)
    if not st or not sp:
        return None
    sh, sm = [int(x) for x in st.split(":")]
    eh, em = [int(x) for x in sp.split(":")]
    start = sh * 60 + sm
    stop = eh * 60 + em
    if stop < start:
        stop += 24 * 60
    return stop - start


def minutes_to_hhmm(minutes: Optional[int]) -> Optional[str]:
    if minutes is None:
        return None
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def computed_moose_total(row: dict[str, Any]) -> int | None:
    vals = {c: to_int_or_none(row.get(c)) for c in COUNT_COLS}
    if any(v is None for v in vals.values()):
        return None
    return (
        vals["bull_yrlg"] + vals["bull_med"] + vals["bull_lrg"]
        + vals["cow_w0"] + 2 * vals["cow_w1"] + 3 * vals["cow_w2"] + 4 * vals["cow_w3"]
        + vals["lone_calf"] + vals["unknown"]
    )


def notes_to_text(notes: Any) -> str:
    if notes is None:
        return ""
    if isinstance(notes, list):
        return "; ".join(str(x).strip() for x in notes if str(x).strip())
    return str(notes).strip()


def normalize_conditions(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in CONDITION_COLS:
        v = obj.get(c)
        if c == "other_explain":
            out[c] = clean_text(v)
        else:
            if isinstance(v, bool):
                out[c] = v
            else:
                s = str(v or "").strip().lower()
                out[c] = s in {"true", "yes", "y", "1", "checked", "x", "selected"}
    return out


# Controlled vocabularies for circled/categorical header fields. Keys are the
# canonical stored values; the tuples are accepted synonyms/abbreviations. A
# recognized synonym is standardized to its canonical value silently; a value
# outside the set is left as-is and flagged in validation_notes for review.
CATEGORICAL_VOCAB: dict[str, dict[str, tuple[str, ...]]] = {
    "overall_survey_rating": {
        "excellent": ("excellent", "exc", "excel"),
        "good": ("good",),
        "fair": ("fair",),
        "poor": ("poor",),
    },
    "strata": {
        "high": ("high", "hi"),
        "low": ("low", "lo"),
    },
    "snow_age": {
        "fresh": ("fresh",),
        "<1 week": ("<1 week", "<1week", "<1 wk", "less than 1 week", "less than a week", "under 1 week"),
        ">1 week": (">1 week", ">1week", ">1 wk", "more than 1 week", "over 1 week", "older than 1 week"),
    },
    "snow_cover": {
        "complete": ("complete", "full"),
        "some low veg showing": ("some low veg showing", "some low vegetation showing", "some low veg", "low veg showing"),
        "bare ground showing": ("bare ground showing", "bare ground", "bare"),
    },
    "light_type": {
        "bright": ("bright",),
        "flat": ("flat",),
    },
    "light_intensity": {
        "high": ("high",),
        "medium": ("medium", "med"),
        "low": ("low",),
    },
    "predominant_habitat_type": {
        "open lower elevation, predom shrub, riparian, or wetland": ("open lower elevation, predom shrub, riparian, or wetland", "open lower elevation", "open lower"),
        "mixed open forest with some shrub understory": ("mixed open forest with some shrub understory", "mixed open forest", "mixed open"),
        "dense spruce forest": ("dense spruce forest", "dense spruce"),
        "dense deciduous forest birch, aspen, etc. few shrubs": ("dense deciduous forest birch, aspen, etc. few shrubs", "dense deciduous forest", "dense deciduous"),
        "subalpine shrub": ("subalpine shrub", "subalpine"),
        "burn": ("burn",),
        "other": ("other",),
    },
}


def _norm_cat(v: Any) -> Optional[str]:
    s = clean_text(v)
    if s is None:
        return None
    return " ".join(str(s).strip().lower().rstrip(".,;:").split())


def normalize_categoricals(unit_row: dict[str, Any]) -> dict[str, Any]:
    """Standardize categorical header fields to their controlled vocabulary.

    Recognized exact values and synonyms are coerced to the canonical form (a
    safe formatting fix, applied silently). Non-null values that match nothing
    in the allowed set are left untouched and flagged in validation_notes, so a
    reviewer can see a likely circling/transcription misread. For the long
    habitat descriptions, prefix matching is allowed; the short enumerations use
    exact/synonym matching only, to avoid accidental cross-mapping.
    """
    notes: list[str] = []
    for field, vocab in CATEGORICAL_VOCAB.items():
        raw = unit_row.get(field)
        norm = _norm_cat(raw)
        if norm is None:
            continue
        matched: Optional[str] = None
        for canon, aliases in vocab.items():
            if norm == _norm_cat(canon) or norm in {_norm_cat(a) for a in aliases}:
                matched = canon
                break
        if matched is None and field == "predominant_habitat_type":
            for canon, aliases in vocab.items():
                cands = [canon, *aliases]
                if any(
                    _norm_cat(c) and (norm.startswith(_norm_cat(c)) or _norm_cat(c).startswith(norm))
                    for c in cands
                ):
                    matched = canon
                    break
        if matched is not None:
            unit_row[field] = matched
        else:
            allowed = ", ".join(vocab.keys())
            notes.append(f"{field} value '{raw}' not in allowed set ({allowed})")
    if notes:
        unit_row["validation_notes"] = "; ".join(
            x for x in [unit_row.get("validation_notes"), *notes] if x
        )
    return unit_row


def normalize_extraction(obj: dict[str, Any], source_file: str, page_number: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    su_guid = str(uuid.uuid4())
    survey_unit = obj.get("survey_unit") if isinstance(obj.get("survey_unit"), dict) else {}
    conditions = obj.get("additional_conditions") if isinstance(obj.get("additional_conditions"), dict) else {}
    validation_notes = obj.get("validation_notes") or []

    start = normalize_time(survey_unit.get("start_time"))
    stop = normalize_time(survey_unit.get("stop_time"))
    em = elapsed_minutes(start, stop)
    unit_row: dict[str, Any] = {
        "survey_unit_guid": su_guid,
        "source_file": source_file,
        "source_page": page_number,
        "survey_date": clean_text(survey_unit.get("survey_date") or survey_unit.get("date")),
        "su_number": clean_text(survey_unit.get("su_number")),
        "gmu": clean_text(survey_unit.get("gmu")),
        "start_time": start,
        "stop_time": stop,
        "elapsed": clean_text(survey_unit.get("elapsed")),
        "computed_elapsed_minutes": em,
        "computed_elapsed_hhmm": minutes_to_hhmm(em),
        "location": clean_text(survey_unit.get("location")),
        "observer": clean_text(survey_unit.get("observer")),
        "pilot": clean_text(survey_unit.get("pilot")),
        "strata": clean_text(survey_unit.get("strata")),
        "aircraft_type": clean_text(survey_unit.get("aircraft_type")),
        "temp_f": to_float_or_none(survey_unit.get("temp_f")),
        "area_mi2": to_float_or_none(survey_unit.get("area_mi2")),
        "overall_survey_rating": clean_text(survey_unit.get("overall_survey_rating")),
        "snow_age": clean_text(survey_unit.get("snow_age")),
        "light_type": clean_text(survey_unit.get("light_type")),
        "snow_cover": clean_text(survey_unit.get("snow_cover")),
        "light_intensity": clean_text(survey_unit.get("light_intensity")),
        "predominant_habitat_type": clean_text(survey_unit.get("predominant_habitat_type")),
        "predominant_habitat_other": clean_text(survey_unit.get("predominant_habitat_other")),
        "total_number_moose": to_int_or_none(survey_unit.get("total_number_moose")),
        "validation_notes": notes_to_text(validation_notes),
        "llm_notes": notes_to_text(survey_unit.get("llm_notes")),
    }
    unit_row.update(normalize_conditions(conditions))
    unit_row = {c: unit_row.get(c) for c in UNIT_COLS}

    # Standardize categorical header fields to their controlled vocabularies and
    # flag any value that falls outside the allowed set.
    unit_row = normalize_categoricals(unit_row)

    # Cross-check the handwritten elapsed value against start/stop times.
    # A disagreement usually means a mis-transcribed time or elapsed digit.
    written_elapsed = to_int_or_none(unit_row.get("elapsed"))
    if written_elapsed is not None and em is not None and written_elapsed != em:
        msg = f"written elapsed ({written_elapsed} min) != computed from start/stop ({em} min)"
        unit_row["validation_notes"] = "; ".join(
            x for x in [unit_row.get("validation_notes"), msg] if x
        )

    group_rows: list[dict[str, Any]] = []
    groups = obj.get("groups") if isinstance(obj.get("groups"), list) else []
    for g in groups:
        if not isinstance(g, dict):
            continue
        row: dict[str, Any] = {
            "survey_unit_guid": su_guid,
            "source_file": source_file,
            "source_page": page_number,
            "su_number": unit_row.get("su_number"),
            "group_no": to_int_or_none(g.get("group_no")),
            "remarks_waypoint_latlon": clean_text(g.get("remarks_waypoint_latlon")),
            "llm_notes": notes_to_text(g.get("llm_notes")),
        }
        has_any_count = False
        for c in COUNT_COLS:
            val = count_or_zero(g.get(c))
            row[c] = val
            if val not in (None, 0):
                has_any_count = True
        row["total_moose"] = to_int_or_none(g.get("total_moose"))
        row["computed_total_moose"] = computed_moose_total(row)
        if row["total_moose"] is None and row["computed_total_moose"] is not None and has_any_count:
            row["total_moose"] = row["computed_total_moose"]
            row["total_moose_check"] = "computed"
        elif row["computed_total_moose"] is not None and row["total_moose"] is not None:
            row["total_moose_check"] = "ok" if row["computed_total_moose"] == row["total_moose"] else "mismatch"
        else:
            row["total_moose_check"] = "not_checked"
        if row["group_no"] is None and not has_any_count and not row.get("remarks_waypoint_latlon"):
            continue
        group_rows.append(row)

    # Add unit-level validation checks from normalized groups.
    if unit_row.get("total_number_moose") is not None:
        group_total_sum = sum((r.get("total_moose") or 0) for r in group_rows if r.get("total_moose") is not None)
        if group_rows and group_total_sum != unit_row["total_number_moose"]:
            msg = f"sum of group total_moose ({group_total_sum}) != total_number_moose ({unit_row['total_number_moose']})"
            unit_row["validation_notes"] = "; ".join(x for x in [unit_row.get("validation_notes"), msg] if x)

    summary_rows: list[dict[str, Any]] = []
    summary = obj.get("summary") if isinstance(obj.get("summary"), dict) else {}
    for key in SUMMARY_KEYS:
        srow_in = summary.get(key) if isinstance(summary.get(key), dict) else {}
        srow = {
            "survey_unit_guid": su_guid,
            "source_file": source_file,
            "source_page": page_number,
            "su_number": unit_row.get("su_number"),
            "summary_row": key,
        }
        for c in COUNT_COLS:
            srow[c] = count_or_zero(srow_in.get(c))
        srow["total_moose"] = to_int_or_none(srow_in.get("total_moose"))
        srow["computed_total_moose"] = computed_moose_total(srow)
        summary_rows.append(srow)

    # --- COLUMN-SHIFT DETECTION (non-mutating) ---
    # Sum each count column across all group rows and cross-check against the
    # handwritten summary "total" row. Disagreements are flagged for human
    # review in the GUI. We never silently rewrite extracted counts, so every
    # correction remains auditable.
    #
    # Note: the "total" row (not "groups_1_14") is the complete class-count
    # total for the whole survey unit. Summing every group row and comparing to
    # it is correct even when a page carries groups numbered past 14.
    try:
        total_srow = next((sr for sr in summary_rows if sr.get("summary_row") == "total"), None)
        summary_has_counts = total_srow is not None and any(
            (total_srow.get(c) or 0) != 0 for c in COUNT_COLS
        )
        if total_srow and group_rows and summary_has_counts:
            shift_notes = []
            for col in COUNT_COLS:
                grp_sum = sum((gr.get(col) or 0) for gr in group_rows)
                sum_val = total_srow.get(col) or 0
                if grp_sum != sum_val:
                    shift_notes.append(f"{col} groups={grp_sum} summary={sum_val}")
            if shift_notes:
                msg = ("column sums disagree with summary total "
                       "(possible column shift or miscount): " + "; ".join(shift_notes))
                unit_row["validation_notes"] = "; ".join(
                    x for x in [unit_row.get("validation_notes"), msg] if x
                )
    except Exception as exc:
        print(f"WARN: column-shift detection failed: {exc}", file=sys.stderr)

    return unit_row, group_rows, summary_rows


@dataclass
class ExtractionBatchResult:
    survey_units: pd.DataFrame
    groups: pd.DataFrame
    summaries: pd.DataFrame
    runs: pd.DataFrame
    raw_json_dir: Path


def _extract_one_page(
    extractor: "AzureMooseExtractor",
    path: Path,
    page_number: int,
    page_count: int,
    img_bytes: bytes,
    mime_type: str,
    raw_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Extract, persist raw JSON, and normalize a single page.

    Thread-safe for use inside a ThreadPoolExecutor: each call writes to its own
    raw_path and shares one read-only AzureMooseExtractor (the Azure SDK client
    is safe to share across threads for independent requests). Returns
    (unit_row, group_rows, summary_rows, run_row).
    """
    prompt = build_prompt(path.name, page_number, page_count)
    started = utc_now_iso()
    run_id = str(uuid.uuid4())
    fs_img = os.getenv("FEW_SHOT_IMAGE")
    fs_json = os.getenv("FEW_SHOT_JSON")
    fs_img_path = Path(fs_img) if fs_img else None
    fs_json_path = Path(fs_json) if fs_json else None
    # Guard against showing the model the answer: if the few-shot reference is
    # the exact page being extracted, skip it. Otherwise the model is handed a
    # prior transcription of this same page, which inflates eval scores and can
    # leak stale values into production output.
    if fs_img_path is not None:
        page_stem = f"{path.stem}_p{page_number:03d}"
        if fs_img_path.stem in {path.stem, page_stem}:
            print(f"WARN: few-shot image ({fs_img_path.name}) matches the target "
                  f"page ({path.name} p{page_number}); skipping few-shot to avoid "
                  f"leaking the answer.", file=sys.stderr)
            fs_img_path = None
            fs_json_path = None
    try:
        out = extractor.extract_page(
            img_bytes, mime_type, prompt,
            few_shot_image_path=fs_img_path,
            few_shot_json_path=fs_json_path,
        )
        obj = out["json"]
        raw_text = out.get("raw_text", "")
        status, err = "success", None
    except Exception as exc:
        obj = {
            "survey_unit": {}, "additional_conditions": {}, "groups": [], "summary": {},
            "validation_notes": [f"extraction failed: {type(exc).__name__}: {exc}"],
        }
        raw_text = ""
        out = {"input_tokens": None, "output_tokens": None, "used_fallback_params": False}
        status, err = "error", f"{type(exc).__name__}: {exc}"

    raw_path = raw_dir / f"{path.stem}_p{page_number:03d}.json"
    raw_path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    unit, groups, summaries = normalize_extraction(obj, path.name, page_number)
    # If the Azure call fell back to altered parameters (for example losing
    # temperature=0), note it so a reviewer knows the result may differ from a
    # deterministic run.
    if out.get("used_fallback_params"):
        note = "azure call used fallback parameters (deterministic settings may not have applied)"
        unit["validation_notes"] = "; ".join(x for x in [unit.get("validation_notes"), note] if x)
    run_row = {
        "guid_run": run_id,
        "app_version": APP_VERSION,
        "source_file": path.name,
        "source_page": page_number,
        "started_at_utc": started,
        "finished_at_utc": utc_now_iso(),
        "status": status,
        "error": err,
        "model_provider": "azure_openai",
        "model_name": extractor.deployment,
        "model_version": extractor.api_version,
        "input_tokens": out.get("input_tokens"),
        "output_tokens": out.get("output_tokens"),
        "raw_json_file": str(raw_path),
        "raw_response_text": raw_text,
    }
    return unit, groups, summaries, run_row


def process_files(
    paths: list[Path],
    out_dir: Path = DEFAULT_OUTPUT_DIR,
    preprocess: str = "none",
    dpi: int = RENDER_DPI,
    write_page_images: bool = False,
    max_workers: int = MAX_WORKERS,
) -> ExtractionBatchResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "raw_page_json"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if write_page_images:
        (out_dir / "page_images").mkdir(parents=True, exist_ok=True)

    extractor = AzureMooseExtractor()

    # Rendering stays serial: PyMuPDF documents are not safe to share across
    # threads. Only the Azure calls (the slow, I/O-bound part) are parallel.
    # Note: this materializes every page image in memory before dispatch, which
    # is fine for tens to low hundreds of pages. For very large batches, render
    # and dispatch one file at a time instead.
    tasks: list[tuple[Path, int, int, bytes, str]] = []
    for path in paths:
        page_images, report = render_and_report(path, dpi=dpi, preprocess=preprocess)
        if report is not None:
            print(f"[preflight] {path.name}: pages={report.get('page_count')} text_chars={report.get('total_chars')}")
        for page_number, page_count, img_bytes, mime_type in page_images:
            if write_page_images:
                (out_dir / "page_images" / f"{path.stem}_p{page_number:03d}.png").write_bytes(img_bytes)
            tasks.append((path, page_number, page_count, img_bytes, mime_type))

    results: list[Optional[tuple]] = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut_to_idx = {
            ex.submit(_extract_one_page, extractor, p, pn, pc, b, m, raw_dir): i
            for i, (p, pn, pc, b, m) in enumerate(tasks)
        }
        done = 0
        for fut in as_completed(fut_to_idx):
            i = fut_to_idx[fut]
            results[i] = fut.result()
            done += 1
            p, pn, pc, _, _ = tasks[i]
            print(f"[extract] {done}/{len(tasks)} ({p.name} page {pn}/{pc})", flush=True)

    unit_rows: list[dict[str, Any]] = []
    group_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    for unit, groups, summaries, run_row in results:  # original task order preserved
        unit_rows.append(unit)
        group_rows.extend(groups)
        summary_rows.extend(summaries)
        run_rows.append(run_row)

    units_df = pd.DataFrame(unit_rows).reindex(columns=UNIT_COLS)
    groups_df = pd.DataFrame(group_rows).reindex(columns=[
        "survey_unit_guid", "source_file", "source_page", "su_number",
    ] + GROUP_COLS)
    summaries_df = pd.DataFrame(summary_rows).reindex(columns=[
        "survey_unit_guid", "source_file", "source_page", "su_number", "summary_row",
    ] + COUNT_COLS + ["total_moose", "computed_total_moose"])
    runs_df = pd.DataFrame(run_rows)

    return ExtractionBatchResult(units_df, groups_df, summaries_df, runs_df, raw_dir)


def _scalarize_for_export(value: Any) -> Any:
    """Convert GUI/Pandas/LLM objects to CSV/SQLite-safe scalar values."""
    try:
        if value is None or pd.isna(value):
            return None
    except Exception:
        if value is None:
            return None
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Path):
        return str(value)
    # Convert numpy scalar values without importing numpy explicitly.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return value


def _prepare_dataframe_for_export(df: pd.DataFrame, columns: Optional[list[str]] = None) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame()
    out = df.copy()
    if columns is not None:
        out = out.reindex(columns=columns)
    # Ensure SQLite never receives pd.NA, list, dict, Path, or numpy-only scalar objects.
    for col in out.columns:
        out[col] = out[col].map(_scalarize_for_export)
    return out


def flag_su_number_issues(units_df: pd.DataFrame) -> pd.DataFrame:
    """Append validation notes for missing or duplicated su_number values.

    One page is one survey unit, so within a single source_file each su_number
    should be unique. A duplicate usually means a page or column misread. Null
    su_number is always flagged. Duplicate detection is scoped to each
    source_file so the same unit legitimately surveyed in different files or
    seasons is not falsely flagged. Returns a copy; the input is not mutated.
    """
    if not isinstance(units_df, pd.DataFrame) or units_df.empty or "su_number" not in units_df.columns:
        return units_df
    df = units_df.copy()
    if "validation_notes" not in df.columns:
        df["validation_notes"] = ""

    def _blank(v) -> bool:
        return pd.isna(v) or str(v).strip() == ""

    def _append(idx, msg):
        cur = df.at[idx, "validation_notes"]
        cur = "" if pd.isna(cur) else str(cur)
        df.at[idx, "validation_notes"] = "; ".join(x for x in [cur, msg] if x)

    for idx, val in df["su_number"].items():
        if _blank(val):
            _append(idx, "su_number missing")

    if "source_file" in df.columns:
        for sf, series in df.groupby("source_file")["su_number"]:
            norm = series.apply(lambda v: None if _blank(v) else str(v).strip())
            counts = norm.value_counts(dropna=True)
            dups = set(counts[counts > 1].index.tolist())
            for idx, v in norm.items():
                if v in dups:
                    _append(idx, f"duplicate su_number '{v}' within {sf}")
    return df


def _table_exists(conn, table: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _sqlite_table_columns(conn, table: str) -> list[str]:
    try:
        cur = conn.execute(f'PRAGMA table_info("{table}")')
        return [r[1] for r in cur.fetchall()]
    except Exception:
        return []


def _upsert_table(conn, table: str, df: pd.DataFrame, key_col: str) -> None:
    """Insert rows, replacing any existing rows that share key_col values from
    this batch, so re-processing a page overwrites its prior rows while new
    pages accumulate. Creates the table if absent and reconciles simple schema
    drift (for example a newly added column) via ALTER TABLE.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    if not _table_exists(conn, table):
        df.to_sql(table, conn, if_exists="replace", index=False)
        return
    existing = _sqlite_table_columns(conn, table)
    for col in df.columns:
        if col not in existing:
            try:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}"')
                existing.append(col)
            except Exception:
                pass
    aligned = df.copy()
    for col in existing:
        if col not in aligned.columns:
            aligned[col] = None
    aligned = aligned[existing]
    if key_col in aligned.columns:
        keys = [k for k in aligned[key_col].dropna().unique().tolist()]
        CHUNK = 500  # stay well under SQLite's variable limit
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(f'DELETE FROM "{table}" WHERE "{key_col}" IN ({placeholders})', chunk)
    aligned.to_sql(table, conn, if_exists="append", index=False)


def _export_tables_to_csv(conn, out_dir: Path) -> None:
    """Write CSVs from the full persisted tables so they reflect all accumulated
    data across sessions, not only the current batch."""
    mapping = {
        "survey_units": "moose_survey_units.csv",
        "moose_groups": "moose_groups.csv",
        "survey_summaries": "moose_summaries.csv",
        "llm_runs": "moose_llm_runs.csv",
    }
    for table, fname in mapping.items():
        if not _table_exists(conn, table):
            continue
        try:
            df = pd.read_sql_query(f'SELECT * FROM "{table}"', conn)
        except Exception:
            continue
        if table == "llm_runs":
            df = df.drop(columns=["raw_response_text"], errors="ignore")
        df.to_csv(out_dir / fname, index=False)


def write_outputs(result: ExtractionBatchResult, out_dir: Path = DEFAULT_OUTPUT_DIR, db_path: Path = DEFAULT_DB_PATH) -> None:
    out_dir = Path(out_dir)
    db_path = Path(db_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Flag missing or duplicated SU numbers before anything is persisted so the
    # notes propagate to the reviewed JSON, CSVs, and SQLite.
    result.survey_units = flag_su_number_issues(result.survey_units)

    survey_units = _prepare_dataframe_for_export(result.survey_units, UNIT_COLS)
    groups = _prepare_dataframe_for_export(result.groups, [
        "survey_unit_guid", "source_file", "source_page", "su_number",
    ] + GROUP_COLS)
    summaries = _prepare_dataframe_for_export(result.summaries, [
        "survey_unit_guid", "source_file", "source_page", "su_number", "summary_row",
    ] + COUNT_COLS + ["total_moose", "computed_total_moose"])
    runs = _prepare_dataframe_for_export(result.runs)

    # CSVs are written from the persisted SQLite tables after the upsert below,
    # so they reflect all accumulated data across sessions rather than only the
    # current batch.

    # --- SERIAlIZE EDITED DATA BACK TO INDIVIDUAL GSPE JSONs IN reviewed_page_json ---
    rev_dir = out_dir / "reviewed_page_json"
    rev_dir.mkdir(parents=True, exist_ok=True)

    # Pre-group child rows by survey_unit_guid once so the per-unit loop below is
    # linear rather than scanning every group/summary row for every unit. Using
    # to_dict("records") also avoids per-row Series construction, which is slow.
    groups_by_guid: dict[Any, list[dict[str, Any]]] = {}
    if isinstance(result.groups, pd.DataFrame) and not result.groups.empty:
        for gr_row in result.groups.to_dict("records"):
            groups_by_guid.setdefault(gr_row.get("survey_unit_guid"), []).append(gr_row)
    summaries_by_guid: dict[Any, list[dict[str, Any]]] = {}
    if isinstance(result.summaries, pd.DataFrame) and not result.summaries.empty:
        for sm_row in result.summaries.to_dict("records"):
            summaries_by_guid.setdefault(sm_row.get("survey_unit_guid"), []).append(sm_row)

    units_records = result.survey_units.to_dict("records") if isinstance(result.survey_units, pd.DataFrame) else []
    for unit_row in units_records:
        su_guid = unit_row.get("survey_unit_guid")
        src_file = unit_row.get("source_file")
        src_page = unit_row.get("source_page")
        if not su_guid or not src_file or src_page is None:
            continue

        # 1. Rebuild GSPE header survey_unit dict
        su_data = {}
        for c in ["survey_date", "su_number", "gmu", "start_time", "stop_time", "elapsed", "location", "observer", "pilot", "strata", "aircraft_type", "temp_f", "area_mi2", "overall_survey_rating", "snow_age", "light_type", "snow_cover", "light_intensity", "predominant_habitat_type", "predominant_habitat_other", "total_number_moose", "llm_notes"]:
            su_data[c] = unit_row.get(c)

        # 2. Rebuild GSPE additional_conditions dict.
        # Coerce through normalize_conditions so boolean flags stay real
        # booleans and other_explain stays free text, regardless of whether the
        # value came from extraction (native bool) or a GUI edit (string such as
        # "true"). This keeps the reviewed ground-truth JSON schema-consistent.
        cond_data = normalize_conditions(unit_row)

        # 3. Filter and rebuild GSPE groups list for this page
        pg_groups = []
        for gr_row in groups_by_guid.get(su_guid, []):
            g_item = {"group_no": gr_row.get("group_no"), "remarks_waypoint_latlon": gr_row.get("remarks_waypoint_latlon"), "llm_notes": gr_row.get("llm_notes")}
            for c in COUNT_COLS:
                g_item[c] = gr_row.get(c)
            g_item["total_moose"] = gr_row.get("total_moose")
            pg_groups.append(g_item)

        # 4. Filter and rebuild GSPE summary rows dict for this page
        summary_data = {}
        for sm_row in summaries_by_guid.get(su_guid, []):
            row_key = sm_row.get("summary_row")
            if row_key:
                s_item = {}
                for c in COUNT_COLS:
                    s_item[c] = sm_row.get(c)
                s_item["total_moose"] = sm_row.get("total_moose")
                summary_data[row_key] = s_item

        # 5. Build full JSON payload
        v_notes = str(unit_row.get("validation_notes") or "").strip()
        validation_list = [v_notes] if v_notes else []

        page_payload = {
            "survey_unit": su_data,
            "additional_conditions": cond_data,
            "groups": pg_groups,
            "summary": summary_data,
            "validation_notes": validation_list
        }

        # Save to reviewed_page_json folder with corresponding file stem
        stem = Path(src_file).stem
        json_file_path = rev_dir / f"{stem}_p{int(src_page):03d}.json"
        json_file_path.write_text(json.dumps(page_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[Export] Saved human-edited ground truth JSON: {json_file_path.name}")

    conn = sqlite3.connect(str(db_path))
    try:
        # Upsert by GUID so prior sessions are preserved. Re-processing a page
        # (same survey_unit_guid) replaces that page's rows; new pages append.
        _upsert_table(conn, "survey_units", survey_units, "survey_unit_guid")
        _upsert_table(conn, "moose_groups", groups, "survey_unit_guid")
        _upsert_table(conn, "survey_summaries", summaries, "survey_unit_guid")
        # Each extraction has a unique guid_run, so this accumulates run history;
        # the key only prevents duplicate rows if the same result is saved twice.
        _upsert_table(conn, "llm_runs", runs, "guid_run")
        # Indexes for the common lookup keys used from R or ad hoc queries.
        cur = conn.cursor()
        for tbl, col in [
            ("survey_units", "survey_unit_guid"), ("survey_units", "su_number"),
            ("moose_groups", "survey_unit_guid"), ("moose_groups", "su_number"),
            ("survey_summaries", "survey_unit_guid"),
        ]:
            try:
                cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{tbl}_{col} ON {tbl}("{col}")')
            except Exception:
                pass
        conn.commit()
        # Derive CSVs from the full persisted tables so they stay consistent with
        # the accumulated database rather than overwriting with one session.
        _export_tables_to_csv(conn, out_dir)
    finally:
        conn.close()


def print_summary(result: ExtractionBatchResult, out_dir: Path, db_path: Path) -> None:
    print("\nDone.")
    print(f"Survey units: {len(result.survey_units)}")
    print(f"Moose groups : {len(result.groups)}")
    print(f"Summaries    : {len(result.summaries)}")
    print(f"Output dir   : {out_dir}")
    print(f"SQLite DB    : {db_path}")
    if not result.survey_units.empty:
        mismatches = result.groups[result.groups.get("total_moose_check") == "mismatch"] if not result.groups.empty else pd.DataFrame()
        if not mismatches.empty:
            print(f"WARN: {len(mismatches)} group total mismatch(es). Review moose_groups.csv.")
        notes = result.survey_units["validation_notes"].fillna("").astype(str)
        n_notes = int((notes.str.len() > 0).sum())
        if n_notes:
            print(f"WARN: {n_notes} survey unit(s) have validation notes. Review moose_survey_units.csv.")
    if not result.runs.empty and "status" in result.runs.columns:
        n_err = int((result.runs["status"].astype(str) == "error").sum())
        if n_err:
            print(f"WARN: {n_err} page(s) failed extraction. Review moose_llm_runs.csv.")



# ==============================================================================
# GUI APP
# ==============================================================================
def run_gui() -> int:
    print("Opening Moose GSPE Datasheet Extractor in a separate window...", flush=True)
    """Launch a PySide6 review app for selecting PDFs/images, running extraction,
    reviewing/editing tables, and saving CSV/SQLite outputs.

    The GUI is intentionally moose-specific but keeps the same Azure .env keys as
    the CLI. PySide6 is imported lazily so command-line/preflight usage still
    works on machines without Qt installed.
    """
    try:
        from collections import deque
        from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, Signal, Slot, QSettings, QTimer, QSortFilterProxyModel
        from PySide6.QtGui import QAction, QIcon, QPixmap, QImage, QBrush, QColor
        from PySide6.QtWidgets import (
            QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
            QFileDialog, QFormLayout, QFrame, QGroupBox, QGridLayout, QHBoxLayout, QLabel,
            QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
            QPushButton, QProgressBar, QScrollArea, QSplitter, QStatusBar,
            QTableView, QTabWidget, QTextEdit, QVBoxLayout, QWidget, QHeaderView
        )
    except Exception as exc:
        print("ERROR: The GUI requires PySide6. Install it with:", file=sys.stderr)
        print("    pip install PySide6", file=sys.stderr)
        print(f"Import error: {exc}", file=sys.stderr)
        return 1

    # Row-highlight colors for the review tables.
    NOTE_BRUSH = QBrush(QColor(255, 235, 235))      # light red: validation notes present
    MISMATCH_BRUSH = QBrush(QColor(255, 244, 214))  # light amber: group total mismatch
    # Shaded rows use light backgrounds, so force dark text on them. Otherwise
    # dark-mode themes keep light default text, which is invisible on the shade.
    HIGHLIGHT_TEXT_BRUSH = QBrush(QColor(20, 20, 20))

    def _gui_is_blank(value: Any) -> bool:
        """Return True for scalar GUI blanks without tripping on lists/dicts/arrays."""
        if value is None:
            return True
        if isinstance(value, str):
            return value.strip().lower() in {"", "nan", "nat", "none", "null", "<na>"}
        if isinstance(value, (dict, list, tuple, set)):
            return False
        try:
            return bool(pd.isna(value))
        except Exception:
            return False

    class ImagePreviewWindow(QMainWindow):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Moose GSPE Datasheet - Original Document Preview")
            self.resize(850, 1100)
            self._base_pixmap = None   # full-resolution source
            self._zoom = 1.0
            self._fit_width = True

            tb = self.addToolBar("View")
            tb.setMovable(False)
            act_fit = QAction("Fit Width", self)
            act_fit.triggered.connect(self.fit_width)
            act_in = QAction("Zoom In", self)
            act_in.setShortcuts(["Ctrl+=", "Ctrl++"])
            act_in.triggered.connect(self.zoom_in)
            act_out = QAction("Zoom Out", self)
            act_out.setShortcut("Ctrl+-")
            act_out.triggered.connect(self.zoom_out)
            act_100 = QAction("100%", self)
            act_100.setShortcut("Ctrl+0")
            act_100.triggered.connect(self.zoom_reset)
            tb.addAction(act_fit)
            tb.addAction(act_in)
            tb.addAction(act_out)
            tb.addAction(act_100)
            self.zoom_label = QLabel("  Fit width")
            tb.addWidget(self.zoom_label)

            self.scroll_area = QScrollArea()
            # Manual sizing so the label tracks the zoomed pixmap rather than the
            # viewport; this is what lets zoom produce scrollbars.
            self.scroll_area.setWidgetResizable(False)
            self.label = QLabel("No original page loaded")
            self.label.setAlignment(Qt.AlignCenter)
            self.scroll_area.setWidget(self.label)
            self.setCentralWidget(self.scroll_area)

        def set_pixmap(self, pixmap):
            # Store the full-resolution pixmap and draw at the current zoom mode.
            self._base_pixmap = pixmap
            # Defer the first fit until the viewport has its real width.
            QTimer.singleShot(0, self._redraw_current_mode)

        def _redraw_current_mode(self):
            if self._fit_width:
                self.fit_width()
            else:
                self._render()

        def _viewport_width(self) -> int:
            return max(100, self.scroll_area.viewport().width() - 4)

        def fit_width(self):
            self._fit_width = True
            if self._base_pixmap is None or self._base_pixmap.isNull():
                return
            bw = self._base_pixmap.width()
            if bw > 0:
                self._zoom = self._viewport_width() / bw
            self._render()

        def zoom_in(self):
            self._fit_width = False
            self._zoom = min(self._zoom * 1.25, 8.0)
            self._render()

        def zoom_out(self):
            self._fit_width = False
            self._zoom = max(self._zoom / 1.25, 0.1)
            self._render()

        def zoom_reset(self):
            self._fit_width = False
            self._zoom = 1.0
            self._render()

        def _render(self):
            if self._base_pixmap is None or self._base_pixmap.isNull():
                return
            target_w = max(1, int(self._base_pixmap.width() * self._zoom))
            scaled = self._base_pixmap.scaledToWidth(target_w, Qt.SmoothTransformation)
            self.label.setPixmap(scaled)
            self.label.resize(scaled.size())
            mode = "Fit width" if self._fit_width else f"{self._zoom * 100:.0f}%"
            self.zoom_label.setText(f"  {mode}")

        def resizeEvent(self, event):
            super().resizeEvent(event)
            # Re-fit on window resize while in fit-width mode.
            if self._fit_width:
                self.fit_width()

        def wheelEvent(self, event):
            # Ctrl+wheel zooms, matching common image viewers.
            if event.modifiers() & Qt.ControlModifier:
                if event.angleDelta().y() > 0:
                    self.zoom_in()
                else:
                    self.zoom_out()
                event.accept()
            else:
                super().wheelEvent(event)


    class PandasModel(QAbstractTableModel):
        def __init__(self, dataframe: Optional[pd.DataFrame] = None, parent=None):
            super().__init__(parent)
            self._df = dataframe.copy() if isinstance(dataframe, pd.DataFrame) else pd.DataFrame()
            # GUI editing writes strings/None into cells that may have been numeric
            # after extraction.  Keeping object dtype prevents Pandas assignment
            # errors when a reviewer blanks a numeric cell or edits it as text.
            if not self._df.empty:
                self._df = self._df.astype("object")
                self._df = self._df.where(pd.notna(self._df), None)
            self._df.reset_index(drop=True, inplace=True)

        @staticmethod
        def _parent_is_valid(parent) -> bool:
            """Safely test Qt parent indexes.

            Some PySide6 builds can call model overrides with a null/invalid C++
            wrapper during view/proxy teardown.  Raising from rowCount/columnCount
            prints noisy "Error calling Python override" tracebacks and can make
            the app feel broken, so these overrides treat any problematic parent
            as invalid and return a safe value.
            """
            if parent is None:
                return False
            try:
                return bool(parent.isValid())
            except Exception:
                return False

        def rowCount(self, parent=None):
            if self._parent_is_valid(parent):
                return 0
            try:
                return int(len(getattr(self, "_df", pd.DataFrame())))
            except Exception:
                return 0

        def columnCount(self, parent=None):
            if self._parent_is_valid(parent):
                return 0
            try:
                return int(len(getattr(self, "_df", pd.DataFrame()).columns))
            except Exception:
                return 0

        def _row_highlight_brush(self, row: int):
            """Return the background brush for a flagged row, or None.

            Shared by BackgroundRole and ForegroundRole so a shaded row always
            gets matching dark text (readable in dark mode).
            """
            if not (0 <= row < self.rowCount()):
                return None
            cols = list(self._df.columns)
            if "validation_notes" in cols:
                vn = self._df.iat[row, cols.index("validation_notes")]
                if not _gui_is_blank(vn):
                    return NOTE_BRUSH
            if "total_moose_check" in cols:
                chk = self._df.iat[row, cols.index("total_moose_check")]
                if str(chk).strip().lower() == "mismatch":
                    return MISMATCH_BRUSH
            return None

        def data(self, index, role=Qt.DisplayRole):
            try:
                if index is None or not index.isValid():
                    return None
                row, col = index.row(), index.column()
                if role in (Qt.DisplayRole, Qt.EditRole):
                    if not (0 <= row < self.rowCount() and 0 <= col < self.columnCount()):
                        return None
                    val = self._df.iat[row, col]
                    if _gui_is_blank(val):
                        return ""
                    if isinstance(val, (dict, list, tuple, set)):
                        return json.dumps(val, ensure_ascii=False)
                    return str(val)
                if role == Qt.BackgroundRole:
                    return self._row_highlight_brush(row)
                if role == Qt.ForegroundRole:
                    # Only override text color on shaded rows; leave normal rows
                    # to the theme so dark mode keeps its light default text.
                    if self._row_highlight_brush(row) is not None:
                        return HIGHLIGHT_TEXT_BRUSH
            except Exception:
                return None
            return None

        def headerData(self, section, orientation, role=Qt.DisplayRole):
            if role != Qt.DisplayRole:
                return None
            try:
                if orientation == Qt.Horizontal:
                    if 0 <= section < len(self._df.columns):
                        return str(self._df.columns[section]).replace("_", " ").title()
                else:
                    return str(section + 1)
            except Exception:
                return None
            return None

        def flags(self, index):
            if not index.isValid():
                return Qt.NoItemFlags
            return Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable

        def setData(self, index, value, role=Qt.EditRole):
            if role != Qt.EditRole or not index.isValid():
                return False
            row, col = index.row(), index.column()
            if not (0 <= row < len(self._df) and 0 <= col < len(self._df.columns)):
                return False
            s = "" if value is None else str(value).strip()
            # Store GUI blanks as None, not pd.NA.  pd.NA can raise Pandas
            # setitem errors when the underlying column started as numeric.
            if self._df.dtypes.iloc[col] != object:
                self._df.iloc[:, col] = self._df.iloc[:, col].astype("object")
            self._df.iat[row, col] = None if s == "" else s
            # Repaint the whole row so background highlights track note edits.
            left = self.index(row, 0)
            right = self.index(row, max(0, self.columnCount() - 1))
            self.dataChanged.emit(left, right, [role, Qt.BackgroundRole])
            return True

        def dataframe(self) -> pd.DataFrame:
            return self._df.copy()

        def insert_empty_row(self):
            self.beginInsertRows(QModelIndex(), len(self._df), len(self._df))
            row = {c: None for c in self._df.columns}
            self._df = pd.concat([self._df.astype("object"), pd.DataFrame([row])], ignore_index=True)
            self.endInsertRows()

        def remove_rows(self, rows: list[int]):
            rows = sorted(set(rows), reverse=True)
            for r in rows:
                if 0 <= r < len(self._df):
                    self.beginRemoveRows(QModelIndex(), r, r)
                    self._df.drop(self._df.index[r], inplace=True)
                    self._df.reset_index(drop=True, inplace=True)
                    self.endRemoveRows()


    class SurveyUnitFilterProxy(QSortFilterProxyModel):
        """Exact-match proxy for showing only the groups/summary rows for one survey-unit page."""
        def __init__(self, parent=None):
            super().__init__(parent)
            self.source_file_value = ""
            self.source_page_value = ""

        def set_survey_unit(self, source_file: Any, source_page: Any):
            self.source_file_value = "" if source_file is None else str(source_file)
            self.source_page_value = "" if source_page is None else str(source_page)
            # Avoid deprecated invalidateFilter(); use Qt 6's begin/end filter
            # change when available, otherwise fall back to invalidate().
            try:
                self.beginFilterChange()
                self.endFilterChange()
            except Exception:
                try:
                    self.invalidateRowsFilter()
                except Exception:
                    try:
                        self.invalidate()
                    except Exception:
                        pass

        def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
            model = self.sourceModel()
            if model is None or not self.source_file_value or not self.source_page_value:
                return True
            cols = getattr(model, "_df", pd.DataFrame()).columns
            if "source_file" not in cols or "source_page" not in cols:
                return True
            try:
                file_val = model.data(model.index(source_row, list(cols).index("source_file")), Qt.DisplayRole)
                page_val = model.data(model.index(source_row, list(cols).index("source_page")), Qt.DisplayRole)
                return str(file_val) == self.source_file_value and str(page_val) == self.source_page_value
            except Exception:
                return True


    class AzureSetupDialog(QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Azure OpenAI Setup")
            self.setMinimumWidth(560)
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel(
                "<b>Enter Azure OpenAI settings for this session.</b><br>"
                "These use the same names as the .env file beside the script."))
            form = QFormLayout()
            self.endpoint = QLineEdit(os.getenv("API_ENDPOINT", ""))
            self.key = QLineEdit(os.getenv("API_KEY", "")); self.key.setEchoMode(QLineEdit.Password)
            self.version = QLineEdit(os.getenv("API_VERSION", "2024-12-01-preview"))
            self.deployment = QLineEdit(os.getenv("API_DEPLOYMENT_NAME", ""))
            form.addRow("API_ENDPOINT:", self.endpoint)
            form.addRow("API_KEY:", self.key)
            form.addRow("API_VERSION:", self.version)
            form.addRow("API_DEPLOYMENT_NAME:", self.deployment)
            layout.addLayout(form)
            self.status = QLabel("")
            self.status.setStyleSheet("color:#b00020;")
            layout.addWidget(self.status)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self._accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def _accept(self):
            vals = {
                "API_ENDPOINT": self.endpoint.text().strip(),
                "API_KEY": self.key.text().strip(),
                "API_VERSION": self.version.text().strip(),
                "API_DEPLOYMENT_NAME": self.deployment.text().strip(),
            }
            missing = [k for k, v in vals.items() if not v]
            if missing:
                self.status.setText("Missing: " + ", ".join(missing))
                return
            os.environ.update(vals)
            self.accept()

    class ExtractionWorker(QThread):
        progress_update = Signal(str)
        result_ready = Signal(object)
        error_ready = Signal(str)

        def __init__(self, file_path: Path, out_dir: Path, preprocess: str, dpi: int, write_page_images: bool, max_workers: int = MAX_WORKERS):
            super().__init__()
            self.file_path = Path(file_path)
            self.out_dir = Path(out_dir)
            self.preprocess = preprocess
            self.dpi = dpi
            self.write_page_images = write_page_images
            self.max_workers = max_workers
            self._cancel = False
            self._cancel_event = threading.Event()

        def stop(self):
            self._cancel = True
            # Also signal the extractor's retry loop so a cancel during an
            # in-flight Azure call stops retrying instead of sleeping and
            # re-sending through the backoff.
            self._cancel_event.set()

        def run(self):
            try:
                self.out_dir.mkdir(parents=True, exist_ok=True)
                raw_dir = self.out_dir / "raw_page_json"
                raw_dir.mkdir(parents=True, exist_ok=True)
                if self.write_page_images:
                    (self.out_dir / "page_images").mkdir(parents=True, exist_ok=True)

                self.progress_update.emit(f"Preparing {self.file_path.name}...")
                page_images, report = render_and_report(self.file_path, dpi=self.dpi, preprocess=self.preprocess)
                if report is not None:
                    self.progress_update.emit(
                        f"Preflight: {report.get('page_count')} page(s), "
                        f"{report.get('total_chars')} text-layer chars")

                extractor = AzureMooseExtractor()
                extractor.cancel_event = self._cancel_event  # stop retries on cancel

                # One task per page.  Page = survey unit, so tasks are independent.
                tasks = [(self.file_path, pn, pc, b, m) for (pn, pc, b, m) in page_images]
                if self.write_page_images:
                    for pn, _pc, b, _m in page_images:
                        (self.out_dir / "page_images" / f"{self.file_path.stem}_p{pn:03d}.png").write_bytes(b)

                # Azure calls run in parallel.  Cancellation stops dispatching
                # new pages and cancels any not yet started; pages already in
                # flight cannot be interrupted and will finish, but their results
                # are discarded by the detaching logic in the UI.
                results: list[Optional[tuple]] = [None] * len(tasks)
                total, done = len(tasks), 0
                with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                    fut_to_idx = {}
                    for i, (p, pn, pc, b, m) in enumerate(tasks):
                        if self._cancel:
                            break
                        fut_to_idx[ex.submit(_extract_one_page, extractor, p, pn, pc, b, m, raw_dir)] = i
                    for fut in as_completed(fut_to_idx):
                        if self._cancel:
                            ex.shutdown(wait=False, cancel_futures=True)
                            break
                        i = fut_to_idx[fut]
                        results[i] = fut.result()
                        done += 1
                        _, pn, pc, _, _ = tasks[i]
                        self.progress_update.emit(
                            f"Extracted {done}/{total} (page {pn}/{pc}) of {self.file_path.name}")

                if self._cancel:
                    raise InterruptedError("cancelled")
                results = [r for r in results if r is not None]

                unit_rows: list[dict[str, Any]] = []
                group_rows: list[dict[str, Any]] = []
                summary_rows: list[dict[str, Any]] = []
                run_rows: list[dict[str, Any]] = []
                for unit, groups, summaries, run_row in results:
                    unit_rows.append(unit)
                    group_rows.extend(groups)
                    summary_rows.extend(summaries)
                    run_rows.append(run_row)

                units_df = pd.DataFrame(unit_rows).reindex(columns=UNIT_COLS)
                groups_df = pd.DataFrame(group_rows).reindex(columns=[
                    "survey_unit_guid", "source_file", "source_page", "su_number",
                ] + GROUP_COLS)
                summaries_df = pd.DataFrame(summary_rows).reindex(columns=[
                    "survey_unit_guid", "source_file", "source_page", "su_number", "summary_row",
                ] + COUNT_COLS + ["total_moose", "computed_total_moose"])
                runs_df = pd.DataFrame(run_rows)
                result = ExtractionBatchResult(units_df, groups_df, summaries_df, runs_df, raw_dir)
                self.result_ready.emit(result)
            except InterruptedError:
                self.error_ready.emit("Extraction cancelled.")
            except Exception as exc:
                tb = traceback.format_exc()
                self.error_ready.emit(f"{type(exc).__name__}: {exc}\n\n{tb}")

    def _make_table_view(model: PandasModel) -> QTableView:
        view = QTableView()
        view.setModel(model)
        view.setAlternatingRowColors(True)
        view.setSortingEnabled(True)
        view.setWordWrap(False)
        view.setTextElideMode(Qt.ElideRight)
        view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        view.horizontalHeader().setStretchLastSection(False)
        view.verticalHeader().setDefaultSectionSize(26)
        for c in range(model.columnCount()):
            view.setColumnWidth(c, 130)
        return view

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"Moose GSPE Datasheet Extractor ({APP_VERSION})")
            self.resize(1400, 900)
            self.settings = QSettings("MooseGSPE", "AzureExtractor")
            self.queue = deque()
            self.current_file_path: Optional[Path] = None
            self.current_result: Optional[ExtractionBatchResult] = None
            self.processed_results: dict[str, ExtractionBatchResult] = {}
            self.worker: Optional[ExtractionWorker] = None
            self._abandoned_workers: list = []
            self.preview_window = None
            self._tiled_once = False
            self.table_models: dict[str, PandasModel] = {}
            self.output_dir = DEFAULT_OUTPUT_DIR
            self.db_path = self.output_dir / "moose_gspe_data.sqlite"
            self._build_ui()
            self._build_menus()
            self._update_status()

        def _build_ui(self):
            central = QWidget()
            self.setCentralWidget(central)
            outer = QVBoxLayout(central)
            splitter = QSplitter(Qt.Horizontal)
            outer.addWidget(splitter, 1)

            sidebar = QWidget()
            sidebar.setMinimumWidth(330)
            sidebar.setMaximumWidth(420)
            sbl = QVBoxLayout(sidebar)
            sbl.setContentsMargins(8, 8, 8, 8)

            setup = QGroupBox("Session")
            setup_form = QFormLayout(setup)
            self.azure_label = QLabel(self._azure_status_text())
            self.azure_label.setWordWrap(True)
            setup_form.addRow("Azure:", self.azure_label)
            self.azure_btn = QPushButton("Configure Azure for Session…")
            self.azure_btn.clicked.connect(self.configure_azure)
            setup_form.addRow(self.azure_btn)
            self.out_dir_edit = QLineEdit(str(self.output_dir))
            self.out_dir_edit.setReadOnly(True)
            self.out_dir_btn = QPushButton("Choose Output Folder…")
            self.out_dir_btn.clicked.connect(self.choose_output_dir)
            setup_form.addRow("Output:", self.out_dir_edit)
            setup_form.addRow(self.out_dir_btn)
            sbl.addWidget(setup)

            files = QGroupBox("Files")
            fl = QVBoxLayout(files)
            self.add_files_btn = QPushButton("Browse & Queue PDFs/Images…")
            self.add_files_btn.clicked.connect(self.browse_files)
            fl.addWidget(self.add_files_btn)
            self.add_folder_btn = QPushButton("Queue All PDFs in Folder…")
            self.add_folder_btn.clicked.connect(self.browse_folder)
            fl.addWidget(self.add_folder_btn)
            self.queue_list = QListWidget()
            self.queue_list.setMinimumHeight(140)
            fl.addWidget(QLabel("Queue:"))
            fl.addWidget(self.queue_list)
            self.clear_queue_btn = QPushButton("Clear Current & Queue")
            self.clear_queue_btn.setToolTip("Remove the current file and all queued files. Does not delete extracted or saved data.")
            self.clear_queue_btn.clicked.connect(self.clear_queue)
            fl.addWidget(self.clear_queue_btn)
            self.current_file_label = QLabel("<i>No current file</i>")
            self.current_file_label.setWordWrap(True)
            fl.addWidget(self.current_file_label)
            sbl.addWidget(files)

            proc = QGroupBox("Processing")
            pf = QFormLayout(proc)
            self.preprocess_combo = QComboBox()
            self.preprocess_map = {
                "None / Original (default)": "none",
                "Color + Contrast (for faint colored ink)": "color_contrast",
                "Grayscale + Contrast": "grayscale_contrast",
                "Grayscale Only": "grayscale",
                "Sharpen": "sharpen",
                "Threshold Simple B&W": "threshold_simple",
            }
            self.preprocess_combo.addItems(list(self.preprocess_map.keys()))
            pf.addRow("Preprocess:", self.preprocess_combo)
            self.dpi_edit = QLineEdit(str(RENDER_DPI))
            pf.addRow("PDF DPI:", self.dpi_edit)
            self.workers_edit = QLineEdit(str(MAX_WORKERS))
            self.workers_edit.setToolTip("Parallel Azure calls for multi-page PDFs. Lower this if you hit rate limits (429).")
            pf.addRow("Parallel workers:", self.workers_edit)
            self.write_images_chk = QCheckBox("Save rendered page images")
            pf.addRow(self.write_images_chk)
            self.pop_image_chk = QCheckBox("Open original document in separate window")
            self.pop_image_chk.setChecked(True)
            self.pop_image_chk.stateChanged.connect(self._on_pop_image_chk_changed)
            pf.addRow(self.pop_image_chk)
            self.process_btn = QPushButton("▶ Process Current / Next")
            self.process_btn.setMinimumHeight(34)
            self.process_btn.setToolTip("Run extraction for the current file, or load and process the next queued file")
            self.process_btn.setDefault(True)
            self.process_btn.setStyleSheet("""
                QPushButton {
                    background-color: #1f7a3f;
                    color: white;
                    border: 2px solid #0f4d28;
                    border-radius: 5px;
                    padding: 4px 12px;
                    font-weight: 800;
                    font-size: 10pt;
                }
                QPushButton:hover {
                    background-color: #24934b;
                    border-color: #092f18;
                }
                QPushButton:pressed {
                    background-color: #17612f;
                }
                QPushButton:disabled {
                    background-color: #a8b7aa;
                    color: #f3f3f3;
                    border: 3px solid #879487;
                }
            """)
            self.process_btn.clicked.connect(self.process_current_or_next)
            pf.addRow(self.process_btn)
            self.save_btn = QPushButton("Save Reviewed Tables")
            self.save_btn.clicked.connect(self.save_reviewed_outputs)
            self.save_btn.setEnabled(False)
            pf.addRow(self.save_btn)
            self.next_btn = QPushButton("Save & Process Next")
            self.next_btn.clicked.connect(self.save_and_process_next)
            self.next_btn.setEnabled(False)
            pf.addRow(self.next_btn)
            self.cancel_btn = QPushButton("Cancel Extraction")
            self.cancel_btn.setMinimumHeight(34)
            self.cancel_btn.clicked.connect(self.cancel_worker)
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setStyleSheet("""
                QPushButton {
                    background-color: #c62828;
                    color: white;
                    border: 2px solid #8e0000;
                    border-radius: 5px;
                    padding: 4px 12px;
                    font-weight: 800;
                    font-size: 10pt;
                }
                QPushButton:hover {
                    background-color: #d32f2f;
                    border-color: #9a0007;
                }
                QPushButton:pressed {
                    background-color: #b71c1c;
                }
                QPushButton:disabled {
                    background-color: #e0a8a8;
                    color: #f3f3f3;
                    border: 2px solid #c88c88;
                }
            """)
            pf.addRow(self.cancel_btn)
            self.progress = QProgressBar()
            self.progress.setRange(0, 0)
            self.progress.setVisible(False)
            pf.addRow(self.progress)
            self.progress_label = QLabel("Idle")
            self.progress_label.setWordWrap(True)
            # Cap growth so a long wrapped preflight/progress message cannot push
            # the buttons above it into collapsing below their text height.
            self.progress_label.setMaximumHeight(48)
            pf.addRow(self.progress_label)
            sbl.addWidget(proc)
            sbl.addStretch(1)
            splitter.addWidget(sidebar)

            right = QWidget()
            rl = QVBoxLayout(right)
            self.header = QLabel("<h2>Review Extracted Moose GSPE Data</h2><p>Select PDF files, run extraction, then edit the tables before saving. Rows shaded red have validation notes; amber marks a group total mismatch.</p>")
            self.header.setWordWrap(True)
            rl.addWidget(self.header)
            self.tabs = QTabWidget()
            rl.addWidget(self.tabs, 1)
            splitter.addWidget(right)
            splitter.setSizes([360, 1040])

            self.status = QStatusBar()
            self.setStatusBar(self.status)

            # Pin a uniform minimum height on every button so none collapses
            # below its label when the layout redistributes vertical space (for
            # example when the preflight/progress text wraps during processing).
            for btn in central.findChildren(QPushButton):
                btn.setMinimumHeight(34)

        def _build_menus(self):
            file_menu = self.menuBar().addMenu("File")
            a_add = QAction("Browse & Queue PDFs/Images…", self); a_add.triggered.connect(self.browse_files)
            a_folder = QAction("Queue All PDFs in Folder…", self); a_folder.triggered.connect(self.browse_folder)
            a_save = QAction("Save Reviewed Tables", self); a_save.triggered.connect(self.save_reviewed_outputs)
            a_exit = QAction("Exit", self); a_exit.triggered.connect(self.close)
            file_menu.addActions([a_add, a_folder, a_save, a_exit])
            tools_menu = self.menuBar().addMenu("Tools")
            a_az = QAction("Configure Azure for Session…", self); a_az.triggered.connect(self.configure_azure)
            a_out = QAction("Choose Output Folder…", self); a_out.triggered.connect(self.choose_output_dir)
            tools_menu.addActions([a_az, a_out])
            view_menu = self.menuBar().addMenu("View")
            a_tile = QAction("Tile Windows (app + preview)", self)
            a_tile.setShortcut("Ctrl+T")
            a_tile.triggered.connect(self._tile_windows_action)
            view_menu.addAction(a_tile)

        def _tile_windows_action(self):
            # Ensure a preview window exists, then force a re-tile on demand.
            if self.preview_window is None:
                self._update_original_page_preview()
            if self.preview_window is not None:
                self.tile_windows(force=True)

        def _azure_status_text(self) -> str:
            cfg = load_azure_config_from_script_env()
            endpoint = os.getenv("API_ENDPOINT") or cfg.get("API_ENDPOINT") or ""
            dep = os.getenv("API_DEPLOYMENT_NAME") or cfg.get("API_DEPLOYMENT_NAME") or ""
            if endpoint and dep:
                return f"Configured: {endpoint} · deployment={dep}"
            return "Not configured. Add .env beside this script or use Configure Azure."

        def configure_azure(self):
            dlg = AzureSetupDialog(self)
            if dlg.exec() == QDialog.Accepted:
                self.azure_label.setText(self._azure_status_text())
                self._update_status("Azure settings loaded for this session.")

        def choose_output_dir(self):
            start = str(self.output_dir)
            d = QFileDialog.getExistingDirectory(self, "Choose output folder", start)
            if not d:
                return
            self.output_dir = Path(d)
            self.db_path = self.output_dir / "moose_gspe_data.sqlite"
            self.out_dir_edit.setText(str(self.output_dir))
            self.settings.setValue("output_dir", str(self.output_dir))
            self._update_status(f"Output folder: {self.output_dir}")

        def browse_files(self):
            start = self.settings.value("last_browse_dir", str(Path.home()), type=str)
            filt = "Supported files (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp);;PDF files (*.pdf);;All files (*)"
            paths, _ = QFileDialog.getOpenFileNames(self, "Select moose datasheets", start, filt)
            if not paths:
                return
            self.settings.setValue("last_browse_dir", str(Path(paths[0]).parent))
            self._queue_paths([Path(p) for p in paths])

        def browse_folder(self):
            start = self.settings.value("last_browse_dir", str(Path.home()), type=str)
            d = QFileDialog.getExistingDirectory(self, "Choose folder containing PDFs", start)
            if not d:
                return
            self.settings.setValue("last_browse_dir", d)
            paths = sorted(Path(d).glob("*.pdf"))
            if not paths:
                QMessageBox.information(self, "No PDFs", "No PDF files were found in that folder.")
                return
            self._queue_paths(paths)

        def _queue_paths(self, paths: list[Path]):
            existing = {str(p) for p in self.queue}
            for p in paths:
                if p.exists() and str(p) not in existing:
                    self.queue.append(p)
                    existing.add(str(p))
            self.refresh_queue_list()
            if self.current_file_path is None and self.queue:
                self.current_file_path = self.queue.popleft()
                self.refresh_queue_list()
                self.current_file_label.setText(f"<b>Current:</b> {self.current_file_path.name}")
            self._update_status(f"Queued {len(paths)} file(s).")

        def refresh_queue_list(self):
            self.queue_list.clear()
            for p in self.queue:
                QListWidgetItem(p.name, self.queue_list)

        def clear_queue(self):
            if self.worker:
                QMessageBox.warning(self, "Busy", "Cancel the running extraction before clearing the queue.")
                return
            if not self.queue and self.current_file_path is None:
                self._update_status("Queue is already empty.")
                return
            n = len(self.queue) + (1 if self.current_file_path is not None else 0)
            self.queue.clear()
            self.current_file_path = None
            self.refresh_queue_list()
            self.current_file_label.setText("<i>No current file</i>")
            # Extracted/saved results are intentionally left intact; this only
            # clears the staged input files.
            self._update_status(f"Cleared {n} staged file(s). Extracted and saved data is unaffected.")

        def _current_ui_result(self) -> Optional[ExtractionBatchResult]:
            if not self.current_result:
                return None
            if hasattr(self, "_sync_datasheet_fields"):
                self._sync_datasheet_fields()
            units = self.table_models.get("survey_units", PandasModel()).dataframe() if "survey_units" in self.table_models else self.current_result.survey_units
            groups = self.table_models.get("groups", PandasModel()).dataframe() if "groups" in self.table_models else self.current_result.groups
            summaries = self.table_models.get("summaries", PandasModel()).dataframe() if "summaries" in self.table_models else self.current_result.summaries
            runs = self.table_models.get("runs", PandasModel()).dataframe() if "runs" in self.table_models else self.current_result.runs
            return ExtractionBatchResult(units, groups, summaries, runs, self.current_result.raw_json_dir)

        def process_current_or_next(self):
            if self.worker:
                QMessageBox.warning(self, "Busy", "Extraction is already running.")
                return
            if not (os.getenv("API_ENDPOINT") and os.getenv("API_KEY") and os.getenv("API_VERSION") and os.getenv("API_DEPLOYMENT_NAME")):
                cfg = load_azure_config_from_script_env()
                if cfg:
                    os.environ.update({k: v for k, v in cfg.items() if v})
                if not (os.getenv("API_ENDPOINT") and os.getenv("API_KEY") and os.getenv("API_VERSION") and os.getenv("API_DEPLOYMENT_NAME")):
                    self.configure_azure()
                    if not (os.getenv("API_ENDPOINT") and os.getenv("API_KEY") and os.getenv("API_VERSION") and os.getenv("API_DEPLOYMENT_NAME")):
                        return
            if self.current_file_path is None:
                if self.queue:
                    self.current_file_path = self.queue.popleft()
                    self.refresh_queue_list()
                else:
                    QMessageBox.information(self, "No files", "Queue one or more PDF/image datasheets first.")
                    return
            try:
                dpi = int(self.dpi_edit.text().strip())
            except Exception:
                QMessageBox.warning(self, "DPI", "PDF DPI must be an integer.")
                return
            try:
                workers = max(1, int(self.workers_edit.text().strip()))
            except Exception:
                QMessageBox.warning(self, "Workers", "Parallel workers must be an integer.")
                return
            preprocess = self.preprocess_map.get(self.preprocess_combo.currentText(), "none")
            self.current_file_label.setText(f"<b>Current:</b> {self.current_file_path.name}")
            self.set_busy(True)
            self.worker = ExtractionWorker(self.current_file_path, self.output_dir, preprocess, dpi, self.write_images_chk.isChecked(), max_workers=workers)
            self.worker.progress_update.connect(self.on_worker_progress)
            self.worker.result_ready.connect(self.on_worker_result)
            self.worker.error_ready.connect(self.on_worker_error)
            self.worker.finished.connect(self.on_worker_finished)
            self.worker.start()

        def set_busy(self, busy: bool):
            self.add_files_btn.setEnabled(not busy)
            self.add_folder_btn.setEnabled(not busy)
            self.process_btn.setEnabled(not busy)
            self.save_btn.setEnabled((not busy) and self.current_result is not None)
            self.next_btn.setEnabled((not busy) and self.current_result is not None)
            self.cancel_btn.setEnabled(busy)
            self.progress.setVisible(busy)

        @Slot(str)
        def on_worker_progress(self, msg: str):
            self.progress_label.setText(msg)
            self.status.showMessage(msg)

        @Slot(object)
        def on_worker_result(self, result: ExtractionBatchResult):
            # Flag missing/duplicate SU numbers now so the red row highlight and
            # the page-selector warning marker appear during review, consistent
            # with the other validation checks. (Scoped per source_file; the GUI
            # processes one file at a time, so this catches within-file dupes.)
            try:
                result.survey_units = flag_su_number_issues(result.survey_units)
            except Exception as exc:
                print(f"WARN: SU-number flagging failed: {exc}", file=sys.stderr)
            self.current_result = result
            if self.current_file_path:
                self.processed_results[str(self.current_file_path)] = result
            self.display_result(result)
            self.progress_label.setText("Ready for review.")
            n_units = len(result.survey_units)
            n_groups = len(result.groups)
            # Surface flagged pages and errors so the reviewer knows where to look.
            n_notes = 0
            if not result.survey_units.empty and "validation_notes" in result.survey_units.columns:
                vn = result.survey_units["validation_notes"].fillna("").astype(str)
                n_notes = int((vn.str.strip().str.len() > 0).sum())
            n_err = 0
            if not result.runs.empty and "status" in result.runs.columns:
                n_err = int((result.runs["status"].astype(str) == "error").sum())
            extra = []
            if n_notes:
                extra.append(f"{n_notes} flagged")
            if n_err:
                extra.append(f"{n_err} error(s)")
            tail = f" — {', '.join(extra)}" if extra else ""
            self._update_status(f"Extraction complete: {n_units} page(s), {n_groups} group row(s){tail}.")

        @Slot(str)
        def on_worker_error(self, msg: str):
            QMessageBox.critical(self, "Extraction Error", msg[:4000])
            self.progress_label.setText("Error.")
            self._update_status("Extraction failed.")

        @Slot()
        def on_worker_finished(self):
            if self.worker:
                self.worker.deleteLater()
                self.worker = None
            self.set_busy(False)

        def cancel_worker(self):
            w = self.worker
            if not w:
                return
            # Stop the current Process Current/Next action and hand the UI back
            # to the user right away. terminate() is never used because it can
            # corrupt the Azure client and open file handles. We also do not
            # block the UI waiting for the in-flight Azure call. Instead this
            # worker is detached: its remaining output is ignored and it cleans
            # itself up in the background once its current page returns. The app
            # stays open and responsive, ready to process another file.
            w.stop()
            for sig in (w.progress_update, w.result_ready, w.error_ready, w.finished):
                try:
                    sig.disconnect()
                except Exception:
                    pass
            self._abandoned_workers.append(w)
            w.finished.connect(lambda w=w: self._reap_worker(w))
            self.worker = None
            self.set_busy(False)
            self.progress.setVisible(False)
            self.progress_label.setText("Extraction cancelled.")
            self._update_status("Extraction cancelled. Ready to process another file.")

        def _reap_worker(self, w):
            try:
                self._abandoned_workers.remove(w)
            except ValueError:
                pass
            w.deleteLater()

        def display_result(self, result: ExtractionBatchResult):
            self.tabs.clear()
            self.table_models = {
                "survey_units": PandasModel(result.survey_units),
                "groups": PandasModel(result.groups),
                "summaries": PandasModel(result.summaries),
                "runs": PandasModel(result.runs.drop(columns=["raw_response_text"], errors="ignore")),
            }

            self._add_datasheet_view_tab()

            def add_table_tab(key: str, title: str):
                page = QWidget()
                layout = QVBoxLayout(page)
                model = self.table_models[key]
                view = _make_table_view(model)
                layout.addWidget(view, 1)
                btn_row = QHBoxLayout()
                add_btn = QPushButton("Add Row")
                add_btn.clicked.connect(lambda: (model.insert_empty_row(), view.resizeColumnsToContents()))
                rem_btn = QPushButton("Remove Selected")
                rem_btn.clicked.connect(lambda: model.remove_rows([i.row() for i in view.selectionModel().selectedRows()]))
                btn_row.addStretch(1)
                btn_row.addWidget(add_btn)
                btn_row.addWidget(rem_btn)
                layout.addLayout(btn_row)
                self.tabs.addTab(page, title)

            add_table_tab("survey_units", "Survey Units Table")
            add_table_tab("groups", "Moose Groups Table")
            add_table_tab("summaries", "Summaries Table")
            add_table_tab("runs", "LLM Runs")

            raw_page = QWidget()
            raw_layout = QVBoxLayout(raw_page)
            self.raw_page_combo = QComboBox()
            raw_layout.addWidget(self.raw_page_combo)
            self.raw_text = QTextEdit()
            self.raw_text.setReadOnly(True)
            self.raw_text.setLineWrapMode(QTextEdit.NoWrap)
            raw_layout.addWidget(self.raw_text, 1)
            self.tabs.addTab(raw_page, "Raw JSON")
            self.raw_page_combo.currentIndexChanged.connect(self.load_selected_raw_json)
            self.raw_page_combo.clear()
            if isinstance(result.runs, pd.DataFrame) and not result.runs.empty:
                for _, row in result.runs.iterrows():
                    label = f"page {row.get('source_page')} — {row.get('status')}"
                    self.raw_page_combo.addItem(label, str(row.get("raw_json_file") or ""))
            self.load_selected_raw_json()
            self.save_btn.setEnabled(True)
            self.next_btn.setEnabled(True)

        def _add_datasheet_view_tab(self):
            """Build a form-like review tab that follows the physical moose datasheet order."""
            page = QWidget()
            outer = QVBoxLayout(page)
            top = QHBoxLayout()
            top.addWidget(QLabel("<b>Survey-unit page:</b>"))
            self.ds_page_combo = QComboBox()
            top.addWidget(self.ds_page_combo, 1)
            self.recalc_btn = QPushButton("Recompute elapsed / group totals")
            self.recalc_btn.clicked.connect(self._recompute_current_datasheet_values)
            top.addWidget(self.recalc_btn)
            outer.addLayout(top)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            sheet = QWidget()
            sheet_layout = QVBoxLayout(sheet)
            sheet_layout.setContentsMargins(10, 10, 10, 10)
            sheet_layout.setSpacing(10)

            self.ds_field_widgets: dict[str, QLineEdit] = {}
            self.ds_text_widgets: dict[str, QTextEdit] = {}
            self.ds_condition_widgets: dict[str, QCheckBox] = {}

            header_group = QGroupBox("Survey Unit Header")
            header_grid = QGridLayout(header_group)
            header_grid.setHorizontalSpacing(10)
            header_grid.setVerticalSpacing(6)

            def add_field(label: str, col: str, row: int, block_col: int, width: int = 150):
                base_col = block_col * 2
                lab = QLabel(label + ":")
                header_grid.addWidget(lab, row, base_col)
                w = QLineEdit()
                w.setMinimumWidth(width)
                w.editingFinished.connect(lambda col=col, w=w: self._write_datasheet_field(col, w.text()))
                self.ds_field_widgets[col] = w
                header_grid.addWidget(w, row, base_col + 1)
                return w

            # Match the physical datasheet layout: left descriptive stack,
            # SU/Strata/Temp/Area in the middle, and Stop/Start/Elapsed at right.
            add_field("Date", "survey_date", 0, 0)
            add_field("SU #", "su_number", 0, 1, 110)
            add_field("Stop@", "stop_time", 0, 2, 110)

            add_field("GMU", "gmu", 1, 0, 110)
            add_field("Start@", "start_time", 1, 2, 110)

            add_field("Location", "location", 2, 0, 320)
            add_field("Elapsed", "elapsed", 2, 2, 110)

            add_field("Observer", "observer", 3, 0, 220)
            add_field("Pilot", "pilot", 4, 0, 220)
            add_field("Strata", "strata", 4, 1, 110)
            add_field("Aircraft Type", "aircraft_type", 5, 0, 180)
            add_field("Temp", "temp_f", 5, 1, 110)
            add_field("Area", "area_mi2", 6, 1, 110)
            add_field("Computed elapsed", "computed_elapsed_hhmm", 3, 2, 110)
            sheet_layout.addWidget(header_group)

            overall_group = QGroupBox("Overall Survey Rating")
            overall_grid = QGridLayout(overall_group)
            overall_grid.addWidget(QLabel("OVERALL SURVEY RATING:"), 0, 0)
            w = QLineEdit()
            w.setMinimumWidth(260)
            w.editingFinished.connect(lambda col="overall_survey_rating", w=w: self._write_datasheet_field(col, w.text()))
            self.ds_field_widgets["overall_survey_rating"] = w
            overall_grid.addWidget(w, 0, 1)
            overall_grid.setColumnStretch(2, 1)
            sheet_layout.addWidget(overall_group)

            search_group = QGroupBox("Search Conditions")
            search_grid = QGridLayout(search_group)

            def add_search_field(label, col, row, col_pair, width=180):
                c = col_pair * 2
                search_grid.addWidget(QLabel(label + ":"), row, c)
                w = QLineEdit()
                w.setMinimumWidth(width)
                w.editingFinished.connect(lambda col=col, w=w: self._write_datasheet_field(col, w.text()))
                self.ds_field_widgets[col] = w
                search_grid.addWidget(w, row, c + 1)

            # Match the paper form: left column is snow age/light type,
            # middle column is snow cover/light intensity, and right column is
            # predominant habitat type plus the free-text Other description.
            add_search_field("Snow Age", "snow_age", 0, 0)
            add_search_field("Light type", "light_type", 1, 0)
            add_search_field("Snow Cover", "snow_cover", 0, 1)
            add_search_field("Light Intensity", "light_intensity", 1, 1)
            add_search_field("Predominant Habitat Type in SU", "predominant_habitat_type", 0, 2, 260)
            add_search_field("If Other, describe", "predominant_habitat_other", 1, 2, 260)

            for c in range(6):
                search_grid.setColumnStretch(c, 1 if c % 2 else 0)
            sheet_layout.addWidget(search_group)

            cond_group = QGroupBox("Additional Conditions")
            cond_grid = QGridLayout(cond_group)
            # Match the paper form: four vertical columns, ordered top-to-bottom
            # within each column.
            condition_columns = [
                [
                    ("Classification errors", "classification_errors"),
                    ("Uncooperative pilot", "uncooperative_pilot"),
                    ("Inadequate search effort", "inadequate_search_effort"),
                    ("Short on fuel", "short_on_fuel"),
                    ("Windy/turbulent", "windy_turbulent"),
                    ("Other (explain)", "other_explain"),
                ],
                [
                    ("Inadequate snow cover", "inadequate_snow_cover"),
                    ("Inexperienced pilot", "inexperienced_pilot"),
                    ("Movement in/out of Intensive", "movement_in_out_of_intensive"),
                    ("Movement in/out of SU", "movement_in_out_of_su"),
                    ("Improper aircraft", "improper_aircraft"),
                ],
                [
                    ("Poor light", "poor_light"),
                    ("Inexperienced observer", "inexperienced_observer"),
                    ("Too many moose in Intensive (>15)", "too_many_moose_in_intensive_gt15"),
                    ("Observer airsick", "observer_airsick"),
                ],
                [
                    ("Low clouds or fog", "low_clouds_or_fog"),
                    ("Poor visibility/snow on trees", "poor_visibility_snow_on_trees"),
                    ("Problems finding SU boundaries", "problems_finding_su_boundaries"),
                    ("Observer sleeping", "observer_sleeping"),
                ],
            ]
            for col_idx, column_items in enumerate(condition_columns):
                for row_idx, (label, col) in enumerate(column_items):
                    if col == "other_explain":
                        other_row = QWidget()
                        other_layout = QHBoxLayout(other_row)
                        other_layout.setContentsMargins(0, 0, 0, 0)
                        other_layout.setSpacing(6)
                        other_layout.addWidget(QLabel(label + ":"))
                        w = QLineEdit()
                        w.setMinimumWidth(190)
                        w.editingFinished.connect(lambda col=col, w=w: self._write_datasheet_field(col, w.text()))
                        self.ds_field_widgets[col] = w
                        other_layout.addWidget(w, 1)
                        cond_grid.addWidget(other_row, row_idx, col_idx)
                    else:
                        cb = QCheckBox(label)
                        cb.stateChanged.connect(lambda _state, col=col, cb=cb: self._write_datasheet_field(col, bool(cb.isChecked())))
                        self.ds_condition_widgets[col] = cb
                        cond_grid.addWidget(cb, row_idx, col_idx)
            for c in range(4):
                cond_grid.setColumnStretch(c, 1)
            sheet_layout.addWidget(cond_group)

            groups_group = QGroupBox("Count Data")
            groups_layout = QVBoxLayout(groups_group)
            groups_layout.addWidget(QLabel("One row per detected group; columns follow the datasheet order."))
            self.ds_groups_proxy = SurveyUnitFilterProxy(self)
            self.ds_groups_proxy.setSourceModel(self.table_models["groups"])
            self.ds_groups_view = QTableView()
            self.ds_groups_view.setModel(self.ds_groups_proxy)
            self.ds_groups_view.setAlternatingRowColors(True)
            self.ds_groups_view.setSortingEnabled(False)
            self.ds_groups_view.setWordWrap(False)
            self.ds_groups_view.setTextElideMode(Qt.ElideRight)
            self.ds_groups_view.verticalHeader().setDefaultSectionSize(26)
            self.ds_groups_view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            self.ds_groups_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            groups_layout.addWidget(self.ds_groups_view)
            self._show_only_columns(
                self.ds_groups_view,
                self.table_models["groups"],
                ["group_no", "bull_yrlg", "bull_med", "bull_lrg", "cow_w0", "cow_w1", "cow_w2", "cow_w3", "lone_calf", "unknown", "total_moose", "computed_total_moose", "total_moose_check", "remarks_waypoint_latlon", "llm_notes"],
                widths={"group_no": 70, "remarks_waypoint_latlon": 260, "llm_notes": 260, "total_moose_check": 120},
            )
            sheet_layout.addWidget(groups_group)

            summary_group = QGroupBox("Survey Summary")
            summary_layout = QVBoxLayout(summary_group)
            total_row = QHBoxLayout()
            total_row.addWidget(QLabel("Total number of moose:"))
            total_w = QLineEdit()
            total_w.setMaximumWidth(160)
            total_w.editingFinished.connect(lambda col="total_number_moose", w=total_w: self._write_datasheet_field(col, w.text()))
            self.ds_field_widgets["total_number_moose"] = total_w
            total_row.addWidget(total_w)
            total_row.addStretch(1)
            summary_layout.addLayout(total_row)
            self.ds_summary_proxy = SurveyUnitFilterProxy(self)
            self.ds_summary_proxy.setSourceModel(self.table_models["summaries"])
            self.ds_summary_view = QTableView()
            self.ds_summary_view.setModel(self.ds_summary_proxy)
            self.ds_summary_view.setAlternatingRowColors(True)
            self.ds_summary_view.setSortingEnabled(False)
            self.ds_summary_view.verticalHeader().setDefaultSectionSize(26)
            self.ds_summary_view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            self.ds_summary_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            summary_layout.addWidget(self.ds_summary_view)
            self._show_only_columns(
                self.ds_summary_view,
                self.table_models["summaries"],
                ["summary_row", "bull_yrlg", "bull_med", "bull_lrg", "cow_w0", "cow_w1", "cow_w2", "cow_w3", "lone_calf", "unknown", "total_moose", "computed_total_moose"],
                widths={"summary_row": 120},
            )
            sheet_layout.addWidget(summary_group)

            notes_group = QGroupBox("Validation Notes / LLM Notes")
            notes_grid = QGridLayout(notes_group)
            notes_grid.addWidget(QLabel("Validation Notes:"), 0, 0)
            vnotes = QTextEdit()
            vnotes.setMinimumHeight(70)
            vnotes.textChanged.connect(lambda col="validation_notes", w=vnotes: self._write_datasheet_field(col, w.toPlainText()))
            self.ds_text_widgets["validation_notes"] = vnotes
            notes_grid.addWidget(vnotes, 0, 1)
            notes_grid.addWidget(QLabel("LLM Notes:"), 1, 0)
            lnotes = QTextEdit()
            lnotes.setMinimumHeight(70)
            lnotes.textChanged.connect(lambda col="llm_notes", w=lnotes: self._write_datasheet_field(col, w.toPlainText()))
            self.ds_text_widgets["llm_notes"] = lnotes
            notes_grid.addWidget(lnotes, 1, 1)
            sheet_layout.addWidget(notes_group)

            sheet_layout.addStretch(1)
            scroll.setWidget(sheet)
            outer.addWidget(scroll, 1)
            self.tabs.addTab(page, "Datasheet Review")

            self.ds_page_combo.currentIndexChanged.connect(self._on_datasheet_page_changed)
            self._reload_datasheet_page_combo()
            self._on_datasheet_page_changed(0)
            QTimer.singleShot(0, self._fit_datasheet_tables_to_visible_rows)

        def _show_only_columns(self, view: QTableView, source_model: PandasModel, columns: list[str], widths: Optional[dict[str, int]] = None):
            widths = widths or {}
            source_cols = list(source_model._df.columns)
            keep = set(columns)
            for i, col in enumerate(source_cols):
                view.setColumnHidden(i, col not in keep)
                if col in keep:
                    view.setColumnWidth(i, widths.get(col, 90))
            for col in columns:
                if col in source_cols and col not in widths:
                    idx = source_cols.index(col)
                    if col in {"remarks_waypoint_latlon", "validation_notes", "llm_notes"}:
                        view.setColumnWidth(idx, 260)
                    elif col in {"total_moose", "computed_total_moose"}:
                        view.setColumnWidth(idx, 120)
                    else:
                        view.setColumnWidth(idx, 86)

        def _fit_table_height_to_visible_rows(self, view: QTableView, min_height: int = 120):
            """Size a datasheet review table so every visible row is shown.

            The datasheet tab itself is inside a QScrollArea, so these tables should
            expand vertically instead of introducing their own nested vertical scrollbars.
            """
            model = view.model()
            if model is None:
                return
            row_count = max(0, model.rowCount())
            row_h = max(26, view.verticalHeader().defaultSectionSize())
            header_h = max(32, view.horizontalHeader().sizeHint().height())
            hbar_h = view.horizontalScrollBar().sizeHint().height()
            frame_h = 2 * view.frameWidth() + 8
            height = max(min_height, header_h + (row_count * row_h) + hbar_h + frame_h)
            view.setMinimumHeight(height)
            view.setMaximumHeight(height)
            view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            for r in range(row_count):
                view.setRowHeight(r, row_h)

        def _fit_datasheet_tables_to_visible_rows(self):
            if hasattr(self, "ds_groups_view"):
                self._fit_table_height_to_visible_rows(self.ds_groups_view, min_height=160)
            if hasattr(self, "ds_summary_view"):
                self._fit_table_height_to_visible_rows(self.ds_summary_view, min_height=120)

        def _reload_datasheet_page_combo(self):
            if not hasattr(self, "ds_page_combo"):
                return
            self.ds_page_combo.blockSignals(True)
            self.ds_page_combo.clear()
            units_model = self.table_models.get("survey_units")
            df = units_model._df if units_model is not None else pd.DataFrame()
            if isinstance(df, pd.DataFrame) and not df.empty:
                for row_idx, row in df.iterrows():
                    su = row.get("su_number")
                    su_txt = "?" if pd.isna(su) or str(su).strip() == "" else str(su)
                    # Prefix a warning marker on pages that carry validation notes
                    # so the reviewer can see which pages need attention without
                    # opening each one.
                    vn = row.get("validation_notes")
                    flag = "" if _gui_is_blank(vn) else "\u26a0 "
                    label = f"{flag}Page {row.get('source_page')} — SU {su_txt} — {row.get('location') or ''}"
                    self.ds_page_combo.addItem(label, int(row_idx))
            self.ds_page_combo.blockSignals(False)

        def _current_datasheet_row(self) -> Optional[int]:
            if not hasattr(self, "ds_page_combo"):
                return None
            idx = self.ds_page_combo.currentData()
            try:
                return int(idx)
            except Exception:
                return None

        def _write_datasheet_field(self, column: str, value: Any):
            row = self._current_datasheet_row()
            model = self.table_models.get("survey_units")
            if row is None or model is None or column not in model._df.columns:
                return
            col_idx = list(model._df.columns).index(column)
            model.setData(model.index(row, col_idx), value)
            # Keep the page-selector warning marker in sync when notes change.
            if column == "validation_notes" and hasattr(self, "ds_page_combo"):
                su = model._df.iat[row, list(model._df.columns).index("su_number")] if "su_number" in model._df.columns else None
                su_txt = "?" if _gui_is_blank(su) else str(su)
                loc = model._df.iat[row, list(model._df.columns).index("location")] if "location" in model._df.columns else ""
                page = model._df.iat[row, list(model._df.columns).index("source_page")] if "source_page" in model._df.columns else ""
                flag = "" if _gui_is_blank(value) else "\u26a0 "
                combo_idx = self.ds_page_combo.findData(row)
                if combo_idx >= 0:
                    self.ds_page_combo.blockSignals(True)
                    self.ds_page_combo.setItemText(combo_idx, f"{flag}Page {page} — SU {su_txt} — {'' if _gui_is_blank(loc) else loc}")
                    self.ds_page_combo.blockSignals(False)

        def _populate_datasheet_fields(self):
            row = self._current_datasheet_row()
            model = self.table_models.get("survey_units")
            if row is None or model is None or row >= len(model._df):
                return
            rec = model._df.iloc[row]
            for col, w in getattr(self, "ds_field_widgets", {}).items():
                w.blockSignals(True)
                val = rec.get(col, "")
                w.setText("" if pd.isna(val) else str(val))
                w.blockSignals(False)
            for col, w in getattr(self, "ds_text_widgets", {}).items():
                w.blockSignals(True)
                val = rec.get(col, "")
                w.setPlainText("" if pd.isna(val) else str(val))
                w.blockSignals(False)
            for col, cb in getattr(self, "ds_condition_widgets", {}).items():
                cb.blockSignals(True)
                val = rec.get(col, False)
                cb.setChecked(str(val).strip().lower() in {"true", "1", "yes", "y", "checked", "x"})
                cb.blockSignals(False)

        def _sync_datasheet_fields(self):
            if not hasattr(self, "ds_page_combo"):
                return
            for col, w in getattr(self, "ds_field_widgets", {}).items():
                self._write_datasheet_field(col, w.text())
            for col, w in getattr(self, "ds_text_widgets", {}).items():
                self._write_datasheet_field(col, w.toPlainText())
            for col, cb in getattr(self, "ds_condition_widgets", {}).items():
                self._write_datasheet_field(col, bool(cb.isChecked()))

        def _on_datasheet_page_changed(self, _idx: int):
            self._populate_datasheet_fields()
            row = self._current_datasheet_row()
            model = self.table_models.get("survey_units")
            if row is None or model is None or row >= len(model._df):
                return
            rec = model._df.iloc[row]
            source_file = rec.get("source_file")
            source_page = rec.get("source_page")
            if hasattr(self, "ds_groups_proxy"):
                self.ds_groups_proxy.set_survey_unit(source_file, source_page)
            if hasattr(self, "ds_summary_proxy"):
                self.ds_summary_proxy.set_survey_unit(source_file, source_page)
            QTimer.singleShot(0, self._fit_datasheet_tables_to_visible_rows)
            self._update_original_page_preview()

        def _on_pop_image_chk_changed(self):
            if not self.pop_image_chk.isChecked():
                if self.preview_window and self.preview_window.isVisible():
                    self.preview_window.hide()
            else:
                self._update_original_page_preview()

        def tile_windows(self, force: bool = True):
            """Lay out the app and the preview side by side.

            Single monitor: app fills the left half of the work area, preview the
            right half (work area excludes the taskbar/dock). Multiple monitors:
            the app stays on its current screen and the preview is placed on the
            other screen, each filling that screen's work area. force=False means
            only tile if it has not been done yet this session, so a manual
            resize is not snapped back on every page change.
            """
            if self.preview_window is None:
                return
            if not force and self._tiled_once:
                return
            app = QApplication.instance()
            if app is None:
                return
            screens = app.screens()
            # Which screen is the app on now?
            app_screen = self.screen() if hasattr(self, "screen") and self.screen() else app.primaryScreen()
            other = next((s for s in screens if s is not app_screen), None)
            if other is not None:
                # Dual-monitor: one window per screen. Move onto the target
                # screen first, then maximize so the OS handles frame margins.
                # This avoids the QWindowsWindow::setGeometry frame-fit warning
                # that setting an exact work-area size triggers, and makes each
                # window truly fill its screen.
                a = app_screen.availableGeometry()
                b = other.availableGeometry()
                self.move(a.x(), a.y())
                self.showMaximized()
                self.preview_window.move(b.x(), b.y())
                self.preview_window.showMaximized()
            else:
                # Single monitor: split the work area left/right. Subtract the
                # frame margin so the framed window fits inside the work area and
                # does not trip the same geometry-clamp warning.
                area = app_screen.availableGeometry()
                half = area.width() // 2
                fm = max(0, self.frameGeometry().height() - self.geometry().height())
                self.setGeometry(area.x(), area.y(), half, area.height() - fm)
                self.preview_window.setGeometry(area.x() + half, area.y(), area.width() - half, area.height() - fm)
            self._tiled_once = True

        def _update_original_page_preview(self):
            if self.current_file_path is None:
                return
            if not self.pop_image_chk.isChecked():
                return
            path = Path(self.current_file_path)
            if not path.exists():
                return
            
            row = self._current_datasheet_row()
            model = self.table_models.get("survey_units")
            if row is None or model is None or row >= len(model._df):
                return
            rec = model._df.iloc[row]
            source_page = int(rec.get("source_page") or 1)
            
            if not self.preview_window:
                self.preview_window = ImagePreviewWindow(self)
                
            ext = path.suffix.lower()
            try:
                if ext == ".pdf":
                    if not PDF_SUPPORT_ENABLED:
                        self.preview_window.label.setText("Install PyMuPDF (pymupdf) to preview PDFs")
                        self.preview_window.show()
                        return
                    doc = fitz.open(str(path))
                    try:
                        page = doc.load_page(source_page - 1)
                        # Render at a higher DPI than the window needs so zooming
                        # in reveals real detail instead of interpolation blur.
                        pix = page.get_pixmap(dpi=220)
                        img_data = pix.tobytes("png")
                        pixmap = QPixmap()
                        pixmap.loadFromData(img_data)
                        self.preview_window.set_pixmap(pixmap)
                    finally:
                        doc.close()
                elif ext in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
                    pixmap = QPixmap(str(path))
                    self.preview_window.set_pixmap(pixmap)
                
                self.preview_window.show()
                # Auto-tile only the first time the preview opens this session;
                # after that the reviewer's own window placement is left alone.
                self.tile_windows(force=False)
            except Exception as exc:
                print(f"Error loading preview window: {exc}")

        def _recompute_current_datasheet_values(self):
            """Update elapsed fields and visible group computed totals for the selected survey-unit page."""
            self._sync_datasheet_fields()
            row = self._current_datasheet_row()
            units_model = self.table_models.get("survey_units")
            groups_model = self.table_models.get("groups")
            if row is None or units_model is None or row >= len(units_model._df):
                return
            rec = units_model._df.iloc[row]
            em = elapsed_minutes(rec.get("start_time"), rec.get("stop_time"))
            for col, val in (("computed_elapsed_minutes", em), ("computed_elapsed_hhmm", minutes_to_hhmm(em))):
                if col in units_model._df.columns:
                    units_model.setData(units_model.index(row, list(units_model._df.columns).index(col)), "" if val is None else val)
            if groups_model is not None and not groups_model._df.empty:
                file_val = str(rec.get("source_file"))
                page_val = str(rec.get("source_page"))
                for gr, grow in groups_model._df.iterrows():
                    if str(grow.get("source_file")) != file_val or str(grow.get("source_page")) != page_val:
                        continue
                    ctot = computed_moose_total(grow.to_dict())
                    if "computed_total_moose" in groups_model._df.columns:
                        groups_model.setData(groups_model.index(gr, list(groups_model._df.columns).index("computed_total_moose")), "" if ctot is None else ctot)
                    if "total_moose_check" in groups_model._df.columns:
                        tmoose = to_int_or_none(grow.get("total_moose"))
                        check = "not_checked" if ctot is None or tmoose is None else ("ok" if ctot == tmoose else "mismatch")
                        groups_model.setData(groups_model.index(gr, list(groups_model._df.columns).index("total_moose_check")), check)
            self._populate_datasheet_fields()
            self._update_status("Recomputed elapsed and group total checks for the selected page.")

        def load_selected_raw_json(self):
            if not hasattr(self, "raw_page_combo"):
                return
            path = self.raw_page_combo.currentData()
            if path and Path(path).exists():
                try:
                    self.raw_text.setPlainText(Path(path).read_text(encoding="utf-8"))
                except Exception as exc:
                    self.raw_text.setPlainText(f"Could not read raw JSON: {exc}")
            else:
                self.raw_text.setPlainText("")

        def save_reviewed_outputs(self):
            cur = self._current_ui_result()
            if cur and self.current_file_path:
                self.current_result = cur
                self.processed_results[str(self.current_file_path)] = cur
            if not self.processed_results:
                QMessageBox.information(self, "Nothing to save", "No extracted results are available yet.")
                return
            combined = self._combined_result()
            try:
                write_outputs(combined, out_dir=self.output_dir, db_path=self.db_path)
            except Exception as exc:
                QMessageBox.critical(self, "Save Error", f"{type(exc).__name__}: {exc}")
                return
            QMessageBox.information(
                self, "Saved",
                f"Saved reviewed outputs to:\n{self.output_dir}\n\nSQLite DB:\n{self.db_path}"
            )
            self._update_status(f"Saved outputs to {self.output_dir}")

        def _combined_result(self) -> ExtractionBatchResult:
            results = list(self.processed_results.values())
            if not results:
                return ExtractionBatchResult(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), self.output_dir / "raw_page_json")
            units = pd.concat([r.survey_units for r in results if isinstance(r.survey_units, pd.DataFrame)], ignore_index=True)
            groups = pd.concat([r.groups for r in results if isinstance(r.groups, pd.DataFrame)], ignore_index=True)
            summaries = pd.concat([r.summaries for r in results if isinstance(r.summaries, pd.DataFrame)], ignore_index=True)
            runs = pd.concat([r.runs for r in results if isinstance(r.runs, pd.DataFrame)], ignore_index=True)
            return ExtractionBatchResult(units, groups, summaries, runs, self.output_dir / "raw_page_json")

        def save_and_process_next(self):
            self.save_reviewed_outputs()
            if self.queue:
                self.current_file_path = self.queue.popleft()
                self.current_result = None
                self.refresh_queue_list()
                self.current_file_label.setText(f"<b>Current:</b> {self.current_file_path.name}")
                self.process_current_or_next()
            else:
                self._update_status("Saved. Queue is empty.")

        def _update_status(self, msg: Optional[str] = None):
            if msg:
                self.status.showMessage(msg, 7000)
            elif hasattr(self, "status"):
                self.status.showMessage(f"Output: {self.output_dir}")

        def closeEvent(self, event):
            if self.worker:
                reply = QMessageBox.question(self, "Extraction running", "Cancel extraction and exit?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if reply != QMessageBox.Yes:
                    event.ignore(); return
                self.worker.stop()
                self.worker.wait(10000)  # give in-flight pages up to 10s to drain
            # Wait briefly for any detached (cancelled) workers so a running
            # QThread is not destroyed mid-flight on exit.
            for w in list(self._abandoned_workers):
                try:
                    w.stop()
                    w.wait(10000)
                except Exception:
                    pass
            event.accept()

    app = QApplication.instance() or QApplication(sys.argv)
    # Show Azure dialog on first launch if the .env is incomplete.
    cfg = load_azure_config_from_script_env()
    os.environ.update({k: v for k, v in cfg.items() if v})
    if not (os.getenv("API_ENDPOINT") and os.getenv("API_KEY") and os.getenv("API_VERSION") and os.getenv("API_DEPLOYMENT_NAME")):
        dlg = AzureSetupDialog()
        if dlg.exec() != QDialog.Accepted:
            return 1
    win = MainWindow()
    win.show()
    return app.exec()

def run_cli(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or (len(argv) == 1 and argv[0] == "--gui"):
        return run_gui()
    ap = argparse.ArgumentParser(description="Digitize moose GSPE survey datasheets with Azure OpenAI vision.")
    ap.add_argument("files", nargs="+", help="PDF/image datasheets to process")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    ap.add_argument("--dpi", type=int, default=RENDER_DPI, help="PDF render DPI")
    ap.add_argument("--max-workers", type=int, default=MAX_WORKERS, help="Parallel Azure calls across pages")
    ap.add_argument("--preprocess", default="none", choices=["none", "grayscale", "grayscale_contrast", "sharpen", "threshold_simple", "color_contrast"])
    ap.add_argument("--write-page-images", action="store_true", help="Save rendered page images for QA")
    ap.add_argument("--preflight-only", action="store_true", help="Only report PDF text-layer status; do not call Azure")
    args = ap.parse_args(argv)

    paths = [Path(p).expanduser().resolve() for p in args.files]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing input file(s): " + ", ".join(str(p) for p in missing))

    if args.preflight_only:
        for p in paths:
            if p.suffix.lower() == ".pdf":
                print(json.dumps(pdf_text_report(p), indent=2))
            else:
                print(json.dumps({"path": str(p), "kind": "image", "text_layer": None}, indent=2))
        return 0

    out_dir = Path(args.out_dir).expanduser().resolve()
    db_path = Path(args.db).expanduser().resolve()
    result = process_files(paths, out_dir=out_dir, preprocess=args.preprocess, dpi=args.dpi, write_page_images=args.write_page_images, max_workers=args.max_workers)
    write_outputs(result, out_dir=out_dir, db_path=db_path)
    print_summary(result, out_dir=out_dir, db_path=db_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run_cli())
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        traceback.print_exc()
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
