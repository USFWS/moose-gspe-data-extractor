# NEWS

All notable changes to the **Moose GSPE Data Extractor** will be documented in this file.

## 0.0.1

### Initial Release

This is the initial release of the **Moose GSPE Data Extractor** (v0.0.1), a specialized system designed to digitize, validate, and human-verify handwritten Moose GeoSpatial Population Estimation (GSPE) survey datasheets using Azure OpenAI's vision models.

### Added
- **AI-Powered Vision Extraction**:
  - Direct integration with Azure OpenAI Service (`AzureOpenAI`) to extract structured data from scanned and handwritten survey sheets.
  - Dynamic execution parameters with auto-fallback for deployments that do not support modern strict JSON schemas or specific configurations.
  - Structured output schema support matching GSPE datasheet fields.
  - Built-in transient error handling featuring exponential backoff, jittered retries, and clean thread-safe cancellation.
  - Dynamic few-shot reference example injection for high-accuracy extraction.

- **Interactive PySide6 human-in-the-loop (QC) Desktop GUI**:
  - **Datasheet Review Tab**: A digitized form layout mapping the exact visual layout of physical GSPE paper datasheets (including header metadata, environmental search conditions, additional survey conditions, group counts, and summaries).
  - **Grid Tables View**: Interactive and editable tables for managing extracted Survey Units, Moose Groups, Summaries, and LLM Runs.
  - **Raw JSON Tab**: Live inspection of raw LLM outputs.
  - **Side-by-Side Original Document Preview**: Integrated viewer using PyMuPDF (`fitz`) with automatic layout tiling (supporting single-monitor split or dual-monitor maximization) to ease verification against the original document.

- **Data Preprocessing & Image Handling Pipeline**:
  - Lazy loading of heavy analytical and visual modules (`pandas`, `PIL`, `fitz`) for rapid app startup.
  - Multiple image preprocessing methods to improve handwriting recognition (e.g., Grayscale, Color Contrast Enhancement, Grayscale Contrast, Sharpening, and Simple Thresholding).
  - Adaptive PDF rendering to high-resolution page images with custom DPI settings.

- **Automated Validation & Error Flagging**:
  - Automatic cross-checks of physical elapsed time against start/stop survey times.
  - Automatic sum aggregation checks of individual moose group counts against summary totals.
  - Columns-shift detection highlighting possible transcription or categorization misalignments.
  - Standardized normalization of categorical inputs (e.g., survey ratings, strata, snow age, and habitat types) against controlled scientific vocabularies.
  - Visual warning markers (⚠) inside page selectors pointing directly to records requiring manual review.

- **Storage & Dual CLI/GUI Operation Mode**:
  - Relational persistence in an SQLite database (`moose_gspe_data.sqlite`).
  - Automated exports to audit-ready CSV files (`moose_survey_units.csv`, `moose_groups.csv`, `moose_summaries.csv`, `moose_llm_runs.csv`).
  - Command-line interface (CLI) to support batch processing, customized output directories, custom DPI adjustments, and `--preflight-only` modes to analyze existing PDF text layers before OCR extraction.
