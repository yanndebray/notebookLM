# NotebookLM Scraper

A Playwright-based scraper that exports all your [NotebookLM](https://notebooklm.google.com/) notebooks — including every generated Studio artifact (Audio Overviews, Slide Decks, Video Overviews, Mind Maps, etc.) — to a local folder.

## What it collects

For each notebook, the scraper creates a folder and saves:

| File | Description |
|------|-------------|
| `metadata.json` | Notebook title, ID, URL, source names, list of artifacts, scrape timestamp |
| `sources.txt` | Names of all sources attached to the notebook (if detectable) |
| `notebook_full.png` | Full-page screenshot of the notebook |
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
│   ├── sources.txt
│   ├── Blueprint_Audio_Overview.m4a
│   └── Blueprint_Slide_Deck.pdf
├── Academy_ The Video-to-Spec Pipeline for AI Coding agents/
│   ├── .notebook_id
│   ├── metadata.json
│   ├── notebook_full.png
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
python3 scrape_notebooklm.py
```

**First run:** A Chrome window opens. If the session transfer from your main profile didn't work, you'll see a Google login page — sign in normally and the script continues automatically. The session is then saved in `.chrome_scraper_profile/` so all future runs are login-free.

**Subsequent runs:** The script skips any notebook that already has a `.notebook_id` file, making re-runs incremental. To re-scrape a notebook, delete its folder.

## How it works

### 1. Profile bootstrap

Chrome on macOS encrypts cookies using a key stored in the macOS Keychain under the name `Chrome Safe Storage`. Simply copying the `Cookies` SQLite file to another directory doesn't work because the decryption key is Keychain-bound to the original directory path.

The solution is a **persistent scraper profile** at `.chrome_scraper_profile/`:

- On first run, the script copies your Chrome `Default/` profile files (Cookies, Local Storage, Session Storage, IndexedDB, Login Data, Preferences) plus the top-level `Local State` file.
- Chrome is launched with `channel="chrome"` (the installed binary, not Playwright's bundled Chromium) and `user_data_dir=.chrome_scraper_profile`. Because the Keychain lookup for `Chrome Safe Storage` is user-scoped (not path-scoped), Chrome can decrypt the copied cookies.
- If the session transfer fails anyway (Google may reject reused tokens), the script waits up to 10 minutes for manual login. Once the user signs in, Chrome writes fresh session cookies to the persistent profile, and all future runs work without interaction.

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

**Source extraction** attempts multiple CSS selector strategies targeting the left-panel source list. NotebookLM's Angular components use obfuscated class names, so several fallback selectors are tried. This is the least reliable part of the scraper and may return an empty list for some notebooks.

### 4. Studio artifact downloads

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

### 5. Incremental re-runs

Each successfully scraped notebook writes a `.notebook_id` file containing just the UUID. On startup, `load_done_ids()` reads all `.notebook_id` files under `notebooks/` and skips those IDs. This means:

- You can stop the scraper mid-run and restart without duplicating work.
- The `notebooks/index.json` is written after each notebook, so it always reflects the current state even if the run is interrupted.

## Known limitations

- **Sources panel**: The source names shown in the left panel use Angular component selectors that NotebookLM may change. Source extraction currently returns empty for many notebooks.
- **Not-yet-generated artifacts**: Tiles at the top of the Studio panel (Audio Overview, Slide Deck, etc.) represent things that *can* be generated. The scraper only downloads artifacts that have already been generated and appear in the list below the tiles.
- **Study Guide**: This artifact type appears in the list but has no Download button — the scraper logs and skips these.
- **macOS only**: The Chrome profile copy strategy and `channel="chrome"` launcher are macOS-specific. On Linux/Windows, adapt `CHROME_PROFILE_SRC` and potentially use `channel="chrome"` with the appropriate Chrome installation path.
- **No API**: This scraper drives a real browser. NotebookLM has no public API, so the approach is inherently fragile to UI changes.

## Configuration

At the top of `scrape_notebooklm.py`:

```python
OUTPUT_DIR       = Path(__file__).parent / "notebooks"          # where to save
SCRAPER_PROFILE  = Path(__file__).parent / ".chrome_scraper_profile"  # persistent Chrome profile
CHROME_PROFILE_SRC = Path.home() / "Library/Application Support/Google/Chrome/Default"
```

Change `CHROME_PROFILE_SRC` if your Chrome uses a non-default profile (e.g., `Profile 1`).

## Re-scraping a single notebook

Delete the notebook's folder and re-run:

```bash
rm -rf "notebooks/My Notebook Title"
python3 scrape_notebooklm.py
```

The scraper will discover it again (its ID will no longer be in the done set) and re-scrape it.

## Resetting the Chrome session

If your session expires:

```bash
rm -rf .chrome_scraper_profile
python3 scrape_notebooklm.py   # will prompt for login once, then persist
```
