[![Lifecycle: stable](https://img.shields.io/badge/lifecycle-stable-green.svg)](https://lifecycle.r-lib.org/articles/stages.html#stable)

[![AI Assisted](https://img.shields.io/badge/AI_Assisted-red?style=plastic)](#ai-disclosure)

# Moose GSPE Data Extractor

A vision-model extractor for scanned moose GSPE survey datasheets.

The app treats **each PDF page as one survey unit** keyed by the page's `SU #` value. It renders every PDF page to an image, sends the page image to a vision-capable model deployment, and returns editable tables for review. Pages within a file are processed in parallel, extracted values are validated and flagged for the reviewer, and reviewed data is upserted into CSV and SQLite so work accumulates across sessions.

## Files

- `moose_gspe_digitizer.py` — GUI app and optional CLI.
- `prompt.md` — moose-specific extraction prompt. Keep it beside the Python file unless you set `MOOSE_PROMPT_PATH`. The app also falls back to an extensionless `prompt` for compatibility.
- `.env` — your Azure settings. Create this beside the Python file.
- `preprocess_dpi_sweep.py` — optional. Scores preprocessing method x DPI combinations against a ground-truth JSON to pick settings empirically.
- `check_json_schema_support.py` — optional. One-shot probe that reports whether the current backend accepts structured outputs (`response_format` `json_schema`).

## Install

```powershell
pip install pandas Pillow python-dotenv openai pymupdf PySide6
```

`pymupdf` is required for PDF support. `PySide6` is required for the review app.

## Provider backend

The extraction pipeline, review UI, validation, and storage are provider-neutral by design. The **current backend is Azure OpenAI**, so the connection settings below use Azure's names and conventions. Support for other vision-model providers is planned; when added, only the connection settings and the client layer change, not the review or output behavior described in this document.

## .env

Create a `.env` file beside the script with your model connection settings. For the current Azure backend:

```text
API_ENDPOINT=https://your-resource.openai.azure.com/
API_KEY=your-api-key
API_VERSION=2024-12-01-preview
API_DEPLOYMENT_NAME=your-vision-model-deployment-name
```

`API_DEPLOYMENT_NAME` must be the name of a vision-capable model deployment. Do not set it to the API version.

### Optional settings

```text
# Paths and I/O
MOOSE_OUTPUT_DIR=moose_output
MOOSE_PROMPT_PATH=prompt.md
MOOSE_RENDER_DPI=150

# Parallelism and request bounds
MOOSE_MAX_WORKERS=4
MOOSE_REQUEST_TIMEOUT=0

# Transient-error retry policy (429 / 5xx / connection / timeout)
MOOSE_MAX_RETRIES=4
MOOSE_RETRY_BASE_DELAY=1.0
MOOSE_RETRY_MAX_DELAY=30.0

# Structured outputs (off by default; see note below)
MOOSE_USE_JSON_SCHEMA=0

# Provider-specific: reasoning effort (only if the backend supports it)
AZURE_INCLUDE_REASONING_EFFORT=0
REASONING_EFFORT=medium

# Optional single-shot in-context example
FEW_SHOT_IMAGE=training_data/page_images/demo_data_1page_p001.png
FEW_SHOT_JSON=training_data/reviewed_page_json/demo_data_1page_p001.json
```

Notes on the optional settings:

- `MOOSE_RENDER_DPI` defaults to 150. Higher DPI increases detail, upload size, and latency. Use the sweep script to choose empirically.
- Default preprocessing is `none` (the raw render is sent). Other modes are available in the GUI and via the CLI `--preprocess` flag.
- `MOOSE_REQUEST_TIMEOUT` is seconds per model call; `0` means no bound. Setting it also caps how long a cancelled request can linger.
- Retry settings control exponential backoff with jitter on transient errors; a provider-supplied `Retry-After` is honored when present.
- `MOOSE_USE_JSON_SCHEMA=1` asks the model to conform to a JSON schema (structured outputs). Support depends on the backend and version; if the backend rejects it, the app automatically falls back to plain JSON. The reasoning-effort settings are likewise backend-specific and ignored where unsupported.
- If `FEW_SHOT_IMAGE` points at the same page you are extracting, the app skips the example for that page so it is not shown its own answer.

## Run the GUI app

From the folder containing the script:

```powershell
python moose_gspe_digitizer.py
```

The app opens a window where you can:

1. Configure Azure if `.env` is missing.
2. Choose an output folder.
3. Browse and queue PDF/image datasheets, or queue all PDFs in a folder. Use **Clear Current & Queue** to empty the staged files without touching extracted or saved data.
4. Set preprocessing, PDF DPI, and parallel worker count.
5. Process the current file. Use **Cancel Extraction** to stop the current run and return the app to idle; it does not close the app.
6. Review and edit the extraction in the **Datasheet Review** tab, organized in paper-form order: survey-unit header, Overall Survey Rating, Search Conditions, Additional Conditions, Count Data, Survey Summary, and validation notes.
7. Use the raw table tabs for bulk edits or database-style review.
8. Save reviewed tables to CSV and SQLite.
9. Save and process the next queued file.

### Review aids

- Rows shaded **red** have validation notes; rows shaded **amber** have a group total that does not match its computed total. Shaded rows use dark text so they stay readable in dark mode.
- The Datasheet Review page selector prefixes a warning marker on pages that carry validation notes.
- The original page opens in a separate preview window with **Fit Width**, **Zoom In/Out**, and **100%** (Ctrl+= / Ctrl+- / Ctrl+0, plus Ctrl+wheel). The preview renders at a higher DPI so zooming reveals real detail.
- **View > Tile Windows** (Ctrl+T) places the app and the preview side by side. With one monitor they split the screen left/right; with two monitors each window fills its own screen. Auto-tiling happens once per session; after that your window placement is left alone.

### Automatic validation checks

Each extracted page is checked and flagged (never silently corrected):

- Written **elapsed** vs. elapsed computed from start/stop times.
- Per-group **total moose** vs. the value computed from class counts.
- Sum of group totals vs. the unit **total number of moose**.
- Column sums vs. the summary **total** row (possible column-shift or miscount).
- Missing or duplicated **SU numbers** within a file.
- Categorical header fields standardized to a controlled vocabulary; values outside the allowed set are flagged.

## Outputs

Saved outputs are written to the selected output folder:

- `moose_survey_units.csv`
- `moose_groups.csv`
- `moose_summaries.csv`
- `moose_llm_runs.csv`
- `moose_gspe_data.sqlite`
- `raw_page_json/` — one JSON file per page, as returned by the model
- `reviewed_page_json/` — one JSON file per page, reflecting human edits (ground truth)

CSV and SQLite are **upserted by GUID**: re-processing a page replaces that page's rows, and new pages accumulate across sessions. The CSVs are regenerated from the full SQLite tables on each save, so they stay consistent with the database. To start fresh, delete or rename `moose_gspe_data.sqlite` before running.

The SQLite tables (`survey_units`, `moose_groups`, `survey_summaries`, `llm_runs`) are indexed on `survey_unit_guid` and `su_number` for querying from R or ad hoc tools. Each run row records the app version and model details for provenance.

## Optional CLI mode

Run extraction from the command line by passing files:

```powershell
python moose_gspe_digitizer.py path\to\datasheet.pdf --out-dir moose_output
```

Useful flags: `--dpi`, `--preprocess` (`none`, `grayscale`, `grayscale_contrast`, `sharpen`, `threshold_simple`, `color_contrast`), `--max-workers`, `--db`, `--write-page-images`.

Preflight-only PDF text-layer check:

```powershell
python moose_gspe_digitizer.py path\to\datasheet.pdf --preflight-only
```

## Helper scripts

Check whether structured outputs are supported on the current backend:

```powershell
python check_json_schema_support.py --module moose_gspe_digitizer.py
```

Sweep preprocessing and DPI against a ground-truth page to choose settings:

```powershell
python preprocess_dpi_sweep.py --module moose_gspe_digitizer.py --input demo_data_1page.pdf --truth demo_data_1page_p001.json --page 1
```

The sweep disables the few-shot example when it overlaps the test page, so scores are not inflated by showing the model its own answer.

## Getting help

Contact the [project maintainer](mailto:mccrea_cobb@fws.gov) for help with this repository. If you have general questions on creating repositories in the USFWS DGEC, reach out to a USFWS DGEC [owner](https://github.com/orgs/USFWS/people?query=role%3Aowner).

## Contribute

Contact the [project maintainer](mailto:mccrea_cobb@fws.gov) for information about contributing to this repository. Submit a [GitHub Issue](https://github.com/USFWS/fws-repo-template/issues) to report a bug or request a feature or enhancement.

-----

### AI Disclosure

This project utilizes a hybrid human-AI development workflow. Code generation tools were used for scaffolding and boilerplate, while logic, testing, and security verification were executed entirely by the author.

![](https://i.creativecommons.org/l/zero/1.0/88x31.png) This work is
licensed under a [Creative Commons Zero Universal v1.0
License](https://creativecommons.org/publicdomain/zero/1.0/).
