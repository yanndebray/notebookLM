# NotebookLM Scraper

A Playwright-based scraper that exports all your [NotebookLM](https://notebooklm.google.com/) notebooks — including the full text of every source and every generated Studio artifact (Audio Overviews, Slide Decks, Video Overviews, Mind Maps, etc.) — to a local folder.

## What it collects

For each notebook, the scraper creates a folder and saves:

| File | Description |
|------|-------------|
| `metadata.json` | Notebook title, ID, URL, source names, list of artifacts, scrape timestamp |
| `notebook_full.png` | Full-page screenshot of the notebook |
| `sources/<name>.txt` | Full extracted text of each source document |
| `*.m4a` | Audio Overview files (one per generated audio) |
| `*.mp4` | Video Overview files |
| `*.pdf` | Slide Decks, Reports, Infographics (where available) |
| `*.png` | Mind Map images |
| `.notebook_id` | Raw notebook UUID — used to skip already-completed notebooks on re-runs |

A top-level `notebooks/index.json` accumulates metadata for every scraped notebook.

### Example output tree

```
notebooks/
├── index.json
├── The Global Blueprint for Coding Agent Cost Observability/
│   ├── .notebook_id
│   ├── metadata.json
│   ├── notebook_full.png
│   ├── sources/
│   │   └── The massive gap in coding agent cost visibility.md.txt
│   ├── Blueprint_Audio_Overview.m4a
│   └── Blueprint_Slide_Deck.pdf
├── Academy_ The Video-to-Spec Pipeline for AI Coding agents/
│   ├── .notebook_id
│   ├── metadata.json
│   ├── notebook_full.png
│   ├── sources/
│   │   └── video-to-spec-pipeline.md.txt
│   ├── Pixels_to_Plans.pdf
│   ├── Video_To_Structured_Specification.pdf
│   └── Academy_Video_to_Code_Pipeline.png
└── ...
```

## Requirements

- **macOS** (the Chrome profile path and Keychain-based cookie auth are macOS-specific)
- **Google Chrome** installed at `/Applications/Google Chrome.app`
- **Python 3.10+**

```bash
pip install playwright
python -m playwright install chromium   # only needed for the bundled browser fallback
```

> On macOS with Homebrew Python (PEP 668 systems):
> ```bash
> pip install playwright --break-system-packages
> ```

## How to run

```bash
# Default — scrapes your primary Google account
python3 scrape_notebooklm.py

# Second (or any additional) account — separate output and profile dirs
python3 scrape_notebooklm.py --output notebooks_work --profile .chrome_profile_work

# Re-scrape only notebooks whose source text is missing (e.g. after a timeout)
python3 scrape_notebooklm.py --retry-sources
```

```
options:
  --output PATH      Directory to save notebooks into  (default: notebooks/)
  --profile PATH     Persistent Chrome profile for this account
                     (default: .chrome_scraper_profile/)
  --retry-sources    Re-scrape source text for notebooks with an empty sources/ dir
```

**First run:** A Chrome window opens. If the session transfer from your main profile didn't work, you'll see a Google login page — sign in and the script continues automatically. It waits indefinitely, printing a reminder every minute, so there's no rush. The session is then saved in the profile directory and all future runs are login-free.

**Subsequent runs:** The script skips any notebook that already has a `.notebook_id` file, making re-runs incremental. To re-scrape a notebook, delete its folder.

**Multiple accounts:** Each account gets its own `--output` directory and `--profile` directory. Sessions are independent — logging into one account does not affect another.

## How it works

### 1. Profile bootstrap

Chrome on macOS encrypts cookies using a key stored in the macOS Keychain under the name `Chrome Safe Storage`. Simply copying the `Cookies` SQLite file to another directory doesn't work because the decryption key is Keychain-bound to the original directory path.

The solution is a **persistent scraper profile** at `.chrome_scraper_profile/`:

- On first run, the script copies your Chrome `Default/` profile files (Cookies, Local Storage, Session Storage, IndexedDB, Login Data, Preferences) plus the top-level `Local State` file.
- Chrome is launched with `channel="chrome"` (the installed binary, not Playwright's bundled Chromium) and `user_data_dir=.chrome_scraper_profile`. Because the Keychain lookup for `Chrome Safe Storage` is user-scoped (not path-scoped), Chrome can decrypt the copied cookies.
- If the session transfer fails anyway (Google may reject reused tokens), the script waits indefinitely for manual login, printing a reminder every 60 seconds. Once the user signs in, Chrome writes fresh session cookies to the persistent profile, and all future runs work without interaction.

### 2. Notebook discovery

From the home page (`https://notebooklm.google.com/`), the script:

1. Scrolls incrementally to trigger lazy loading of all notebook cards.
2. Extracts every unique notebook ID from `href` attributes matching the pattern `/notebook/([A-Za-z0-9_-]+)`.
3. Stops scrolling after 4 consecutive scroll passes with no new IDs.

Using IDs from links (rather than trying to parse card titles from the home page) is robust because:
- The same notebook ID appears in multiple link types (card link, settings link, share link).
- Card titles in the DOM were unreliable due to Angular component nesting and Material icon text nodes (`more_vert`, `audio_magic_eraser`, etc.) appearing as the first text in a container.

### 3. Per-notebook scraping

For each notebook ID the script navigates directly to `https://notebooklm.google.com/notebook/<ID>` using `wait_until="load"` (not `networkidle` — NotebookLM keeps long-lived XHR connections open that prevent `networkidle` from ever resolving).

**Title extraction** tries several selectors in order: `h1`, `.notebook-title`, the page `<title>` element. The " — NotebookLM" suffix appended by the browser is stripped with a regex.

### 4. Source text extraction

Each source in the left panel is represented in the DOM as a `div.single-source-container`. The structure is:

```
div.single-source-container
  button.source-stretched-button    ← click target to open the viewer
  div.source-title-column
    div.source-title                ← source name / file name
    button[aria-label="More"]       ← ⋮ context menu
```

The scraper:

1. Queries all `div.single-source-container` elements.
2. Reads the source name from `div.source-title`.
3. Clicks `button.source-stretched-button` to open the `<source-viewer>` panel.
4. Scrolls within `source-viewer` to force the full content to load.
5. Calls `innerText` on `source-viewer` and strips Angular/Material icon name strings (`button_magic`, `arrow_drop_up`, `description`, etc.) that appear as text nodes but are not document content.
6. Saves the cleaned text to `sources/<source-name>.txt`.
7. Presses Escape to close the viewer before moving to the next source.

**Why `innerText` and not the network response?** NotebookLM processes uploaded files server-side and stores them in its own format. There is no direct download URL for the original source file from the viewer. The `innerText` of `source-viewer` is the rendered plain-text of whatever NotebookLM has processed — this works for PDFs, Markdown, Google Docs, and web pages alike.

### 5. Studio artifact downloads

This is the key insight: generated Studio artifacts (Audio Overviews, Slide Decks, Video Overviews, etc.) are listed **below the generation tiles** in the Studio panel. Each item has:

```
<container>
  <icon-element />
  <span class="artifact-labels">
    Artifact Title
    1 source · 162d ago
  </span>
  <button aria-label="More">more_vert</button>
</container>
```

The scraper:

1. Finds all `span.artifact-labels` elements (which uniquely identify generated artifacts — not generation tiles).
2. For each, walks up the DOM tree to find the sibling `button[aria-label="More"]`.
3. Clicks that button to open the context menu.
4. Clicks **Download** in the menu.
5. Intercepts the browser download event with Playwright's `page.expect_download()` and saves the file using `download.suggested_filename` (which preserves the correct extension: `.m4a`, `.mp4`, `.pdf`, `.png`).

Artifacts without a Download option (e.g., some Study Guides that are purely in-browser) are skipped gracefully.

### 6. Incremental re-runs and index deduplication

Each successfully scraped notebook writes a `.notebook_id` file containing just the UUID. On startup, `load_done_ids()` reads all `.notebook_id` files under `notebooks/` and skips those IDs. This means you can stop mid-run and restart without duplicating work.

`index.json` is keyed by notebook ID internally (`load_index` / `save_index`), so re-running a notebook updates its existing entry rather than appending a duplicate. The file is written after each notebook so it always reflects the current state even if the run is interrupted.

**`--retry-sources`** is a targeted mode that skips notebook discovery, artifact downloads, and any notebook that already has content in its `sources/` directory. It only navigates to notebooks with an empty `sources/` dir and runs `scrape_sources` on them. Useful after source viewer timeouts on the first pass.

## Known limitations

- **Sources with large content**: The `source-viewer` panel loads content lazily. The scraper scrolls 5 times to trigger loading, but very long documents may still be truncated. The text is also plain — formatting, tables, and images from the original file are lost.
- **Source viewer timeout**: Some sources (particularly large files or slow connections) may not load within the 8-second timeout. The scraper logs and skips these; re-running the script will retry them.
- **Not-yet-generated artifacts**: Tiles at the top of the Studio panel (Audio Overview, Slide Deck, etc.) represent things that *can* be generated. The scraper only downloads artifacts that have already been generated and appear in the list below the tiles.
- **Study Guide**: This artifact type appears in the generated list but has no Download button — the scraper logs and skips it gracefully.
- **macOS only**: The Chrome profile copy strategy and `channel="chrome"` launcher are macOS-specific. On Linux/Windows, adapt `CHROME_PROFILE_SRC` and potentially use `channel="chrome"` with the appropriate Chrome installation path.
- **No API**: This scraper drives a real browser. NotebookLM has no public API, so the approach is inherently fragile to UI changes.

## Configuration

`--output` and `--profile` cover most cases via the CLI. The one constant still in the script is the Chrome profile source used to bootstrap the session:

```python
CHROME_PROFILE_SRC = Path.home() / "Library/Application Support/Google/Chrome/Default"
```

Change this if your Chrome uses a non-default profile (e.g., `Profile 2`).

## Common operations

**Re-scrape a single notebook** — delete its folder; the scraper will find and re-process it:
```bash
rm -rf "notebooks/My Notebook Title"
python3 scrape_notebooklm.py
```

**Retry failed source extractions** — re-runs only notebooks with an empty `sources/` dir:
```bash
python3 scrape_notebooklm.py --retry-sources
```

**Scrape a second Google account** into its own directory:
```bash
python3 scrape_notebooklm.py --output notebooks_work --profile .chrome_profile_work
```
A browser window opens; sign into the second account and the scrape starts automatically. The session is persisted so future runs for that account are login-free.

**Reset a Chrome session** if it expires:
```bash
rm -rf .chrome_scraper_profile
python3 scrape_notebooklm.py   # prompts for login once, then persists
```
