#!/usr/bin/env python3
"""
NotebookLM scraper v3
- Downloads studio artifacts via the ⋮ → Download menu (actual files)
- Clicks every source to extract its full text via source-viewer
- Skips notebooks whose .notebook_id file already exists
- Uses a persistent Chrome profile so login is only needed once per account

Usage:
  python3 scrape_notebooklm.py                        # default account
  python3 scrape_notebooklm.py --output notebooks_work --profile .chrome_profile_work
"""

import argparse
import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from playwright.async_api import async_playwright, Download

NOTEBOOKLM_URL = "https://notebooklm.google.com/"
CHROME_PROFILE_SRC = Path.home() / "Library/Application Support/Google/Chrome/Default"

def parse_args():
    p = argparse.ArgumentParser(description="Export all NotebookLM notebooks for one account.")
    p.add_argument("--output", default="notebooks",
                   help="Directory to save notebooks into (default: notebooks/)")
    p.add_argument("--profile", default=".chrome_scraper_profile",
                   help="Persistent Chrome profile dir for this account "
                        "(default: .chrome_scraper_profile/)")
    p.add_argument("--retry-sources", action="store_true",
                   help="Re-scrape source text for notebooks whose sources/ dir is empty")
    return p.parse_args()

args = parse_args()
OUTPUT_DIR     = Path(__file__).parent / args.output
SCRAPER_PROFILE = Path(__file__).parent / args.profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_dirname(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:100] or "untitled"


def strip_suffix(title: str) -> str:
    return re.sub(r'\s*[-–|]\s*NotebookLM\s*$', '', title).strip()


def load_done_ids(output_dir: Path) -> set[str]:
    done = set()
    for p in output_dir.glob("*/.notebook_id"):
        nb_id = p.read_text().strip()
        if nb_id:
            done.add(nb_id)
    return done


def init_profile() -> Path:
    if SCRAPER_PROFILE.exists():
        print(f"Reusing profile: {SCRAPER_PROFILE}")
        return SCRAPER_PROFILE

    print(f"Creating profile: {SCRAPER_PROFILE}")
    SCRAPER_PROFILE.mkdir(parents=True)
    (SCRAPER_PROFILE / "Default").mkdir()

    ls = CHROME_PROFILE_SRC.parent / "Local State"
    if ls.is_file():
        try:
            shutil.copy2(ls, SCRAPER_PROFILE / "Local State")
        except Exception:
            pass

    for item in ["Cookies", "Network/Cookies", "Local Storage", "Session Storage",
                 "IndexedDB", "Login Data", "Preferences", "Secure Preferences"]:
        src = CHROME_PROFILE_SRC / item
        dst = SCRAPER_PROFILE / "Default" / item
        if src.is_dir():
            try:
                shutil.copytree(src, dst)
            except Exception:
                pass
        elif src.is_file():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except Exception:
                pass
    return SCRAPER_PROFILE


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def wait_for_login(page) -> bool:
    await page.goto(NOTEBOOKLM_URL, wait_until="load", timeout=60000)
    await page.wait_for_timeout(3000)
    if "notebooklm.google.com" in page.url and "accounts.google.com" not in page.url:
        print("Session active — no login needed.")
        return True
    print("\nLogin required. Please sign in to your Google account in the browser window.")
    print("Waiting indefinitely — the script continues once you reach NotebookLM.\n")
    i = 0
    while True:
        await page.wait_for_timeout(5000)
        if "notebooklm.google.com" in page.url and "accounts.google.com" not in page.url:
            print(f"Logged in after {(i+1)*5}s.")
            return True
        i += 1
        if i % 12 == 0:  # reminder every minute
            print(f"  Still waiting for login... ({i*5}s elapsed)")


# ---------------------------------------------------------------------------
# Notebook ID collection
# ---------------------------------------------------------------------------

async def collect_all_notebook_ids(page) -> list[str]:
    print("Collecting notebook IDs from home page...")
    await page.goto(NOTEBOOKLM_URL, wait_until="load", timeout=60000)
    await page.wait_for_timeout(3000)

    ids: set[str] = set()
    stale, prev = 0, -1

    while stale < 4:
        hrefs = await page.evaluate("""
        () => [...document.querySelectorAll('a[href]')]
              .map(a => a.href)
              .filter(h => h.includes('/notebook/'))
        """)
        for href in hrefs:
            m = re.search(r'/notebook/([A-Za-z0-9_-]+)', href)
            if m:
                ids.add(m.group(1))

        count = len(ids)
        if count == prev:
            stale += 1
        else:
            stale = 0
            print(f"  {count} IDs...")
        prev = count
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1800)

    print(f"Total: {len(ids)} notebook IDs")
    return sorted(ids)


# ---------------------------------------------------------------------------
# Sources — click each one and extract the full text from source-viewer
# ---------------------------------------------------------------------------

# Material Symbols icon names that appear as text in the DOM — not content
_ICON_NAMES = {
    "button_magic", "arrow_drop_up", "arrow_drop_down", "description",
    "more_vert", "close", "check", "add", "search", "language",
    "keyboard_arrow_down", "keyboard_arrow_up", "search_spark",
    "dock_to_right", "chevron_forward", "open_in_new", "link",
    "web", "file_copy", "source_guide", "audio_magic_eraser",
}

def clean_viewer_text(raw: str) -> str:
    """Strip UI chrome (icon names, labels) from source-viewer innerText."""
    lines = raw.split("\n")
    cleaned = []
    skip_labels = {"Source guide", "Close", "Source Guide"}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append("")
            continue
        # Skip Material icon names
        if stripped.lower() in _ICON_NAMES:
            continue
        # Skip short UI labels
        if stripped in skip_labels:
            continue
        cleaned.append(stripped)
    # Remove leading/trailing blank lines
    while cleaned and not cleaned[0]:
        cleaned.pop(0)
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned)


async def _get_visible_source_titles(page) -> list[str]:
    """Return titles of all div.single-source-container elements currently in the DOM."""
    return await page.evaluate("""
    () => [...document.querySelectorAll('div.single-source-container div.source-title')]
          .map(el => el.innerText.trim()).filter(Boolean)
    """)


async def scrape_sources(page, folder: Path) -> list[str]:
    """
    Scroll the sources panel to discover every source (handles virtual rendering),
    then click each one to extract its full text from source-viewer.

    The bug in the previous version: element handles fetched before the first
    click become stale after the DOM updates on Escape, so all sources after
    the first were silently skipped. Fix: re-query by title on every iteration
    so we never hold a stale element reference across a DOM update.
    """
    sources_dir = folder / "sources"
    sources_dir.mkdir(exist_ok=True)
    await page.wait_for_timeout(1000)

    # --- Pass 1: scroll the sources panel to discover all source titles ---
    all_titles: list[str] = []
    seen: set[str] = set()
    stale = 0

    while stale < 3:
        titles = await _get_visible_source_titles(page)
        new = [t for t in titles if t not in seen]
        for t in new:
            seen.add(t)
            all_titles.append(t)
        if not new:
            stale += 1
        else:
            stale = 0
        # Scroll the sources panel down to reveal more items
        await page.evaluate("""
        () => {
            const panel = document.querySelector(
                '.source-panel, [class*="source-panel-content"], source-picker');
            if (panel) panel.scrollTop += 250;
        }
        """)
        await page.wait_for_timeout(400)

    print(f"  Sources: {len(all_titles)} found")

    # --- Pass 2: for each title, scroll it into view, click, extract ---
    scraped = []
    for title in all_titles:
        safe_title = re.sub(r'[<>:"/\\|?*\n\r\t]', '_', title)[:120]
        out_path = sources_dir / f"{safe_title}.txt"

        if out_path.exists():
            print(f"  [source] skip (exists): {title[:60]}")
            scraped.append(title)
            continue

        print(f"  [source] opening: {title[:60]}")
        try:
            # Scroll the title into view and re-query fresh handles each time
            # (never reuse handles across Escape/DOM-update boundaries)
            await page.evaluate(f"""
            () => {{
                for (const el of document.querySelectorAll('div.source-title')) {{
                    if (el.innerText.trim() === {json.dumps(title)}) {{
                        el.scrollIntoView({{block: 'center'}});
                        break;
                    }}
                }}
            }}
            """)
            await page.wait_for_timeout(300)

            # Fresh query after scroll
            btn = await page.evaluate_handle(f"""
            () => {{
                for (const c of document.querySelectorAll('div.single-source-container')) {{
                    const t = c.querySelector('div.source-title');
                    if (t && t.innerText.trim() === {json.dumps(title)}) {{
                        return c.querySelector('button.source-stretched-button');
                    }}
                }}
                return null;
            }}
            """)
            if not btn:
                print(f"  [source] button not found: {title[:60]}")
                continue

            await btn.click()
            try:
                await page.wait_for_selector("source-viewer", timeout=8000)
            except Exception:
                print(f"  [source] viewer timeout: {title[:60]}")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(400)
                continue

            await page.wait_for_timeout(1000)

            # Scroll viewer to load full content
            viewer_el = await page.query_selector("source-viewer")
            if viewer_el:
                for _ in range(5):
                    await viewer_el.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                    await page.wait_for_timeout(400)

            raw = await page.evaluate(
                "() => { const v = document.querySelector('source-viewer'); return v ? v.innerText : ''; }"
            )
            content = clean_viewer_text(raw)
            if content:
                out_path.write_text(content, encoding="utf-8")
                print(f"  [source] saved: {safe_title}.txt ({len(content):,} chars)")
                scraped.append(title)
            else:
                print(f"  [source] empty content: {title[:60]}")

        except Exception as e:
            print(f"  [source] error '{title[:40]}': {e}")
        finally:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(600)

    return scraped


# ---------------------------------------------------------------------------
# Studio artifact downloads
# ---------------------------------------------------------------------------

async def download_studio_artifacts(page, folder: Path) -> list[str]:
    """
    Find all generated artifact items in the Studio panel.
    Artifact items are identified by their 'span.artifact-labels' element
    which contains the title and "X source · Xd ago" metadata.
    Clicks the sibling button[aria-label='More'] → Download for each.
    """
    downloaded = []

    # Check if artifacts were already downloaded in a previous run
    existing = [f for f in folder.iterdir()
                if f.suffix in {".m4a", ".mp4", ".pdf"} or
                   (f.suffix == ".png" and f.name != "notebook_full.png")]
    if existing:
        print(f"  [studio] {len(existing)} artifact files already exist — skipping download")
        return [f.stem for f in existing]

    await page.wait_for_timeout(1500)

    # Screenshot the full page for reference
    if not (folder / "notebook_full.png").exists():
        try:
            await page.screenshot(path=str(folder / "notebook_full.png"), full_page=True)
        except Exception:
            pass

    # Scroll to reveal all artifact items
    for _ in range(3):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(600)

    # Find artifact items via their label spans (title + "Xd ago" metadata).
    # DOM structure: <container> <icon> <span class="artifact-labels">Title\nX source · Xd ago</span>
    #                            <button aria-label="More"> </container>
    artifact_data = await page.evaluate("""
    () => {
        const results = [];
        for (const label of document.querySelectorAll('span.artifact-labels, [class*="artifact-labels"]')) {
            const text = (label.innerText || '').trim();
            const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
            const title = lines[0] || 'artifact';

            // Walk up to find the container that also holds the More button
            let container = label.parentElement;
            for (let i = 0; i < 6; i++) {
                if (!container) break;
                const btn = container.querySelector('button[aria-label="More"]');
                if (btn) {
                    results.push({ title, metadata: lines.slice(1).join(' ') });
                    break;
                }
                container = container.parentElement;
            }
        }
        return results;
    }
    """)

    print(f"  [studio] {len(artifact_data)} generated artifacts found")
    for a in artifact_data:
        print(f"    • {a['title']} ({a['metadata']})")

    if not artifact_data:
        return downloaded

    # Now iterate: for each artifact-labels span, find its More button and download
    label_els = await page.query_selector_all(
        "span.artifact-labels, [class*='artifact-labels']"
    )

    for label_el in label_els:
        try:
            # Get title from label text
            raw = (await label_el.inner_text()).strip()
            title = raw.split('\n')[0].strip() or "artifact"
            slug = re.sub(r'[^a-z0-9]+', '_', title.lower())[:60]

            # Find More button in the parent container
            more_btn = await label_el.evaluate_handle("""
            el => {
                let p = el.parentElement;
                for (let i = 0; i < 6; i++) {
                    if (!p) return null;
                    const btn = p.querySelector('button[aria-label="More"]');
                    if (btn) return btn;
                    p = p.parentElement;
                }
                return null;
            }
            """)

            if not more_btn:
                print(f"  [studio] no More button found for: {title}")
                continue

            print(f"  [studio] downloading: {title[:60]}")
            await more_btn.click()
            await page.wait_for_timeout(600)

            # Click Download in the menu
            dl_item = await page.query_selector(
                "button:has-text('Download'), "
                "[role='menuitem']:has-text('Download'), "
                "mat-menu-item:has-text('Download')"
            )
            if not dl_item:
                print(f"  [studio] no Download menu item for: {title}")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                continue

            async with page.expect_download(timeout=30000) as dl_info:
                await dl_item.click()

            dl: Download = await dl_info.value
            suggested = dl.suggested_filename or f"{slug}.bin"
            final_path = folder / suggested
            await dl.save_as(str(final_path))
            size = final_path.stat().st_size
            print(f"  [studio] saved: {suggested} ({size:,} bytes)")
            downloaded.append(title)
            await page.wait_for_timeout(400)

        except Exception as e:
            print(f"  [studio] error for '{title}': {e}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(300)

    return downloaded


# ---------------------------------------------------------------------------
# Per-notebook scrape
# ---------------------------------------------------------------------------

async def scrape_notebook(page, nb_id: str, output_root: Path) -> dict:
    url = f"https://notebooklm.google.com/notebook/{nb_id}"
    await page.goto(url, wait_until="load", timeout=60000)
    await page.wait_for_timeout(3000)

    actual_url = page.url

    # Get title
    title = nb_id
    for sel in ["h1", ".notebook-title", "[class*='notebook-title']",
                "title", "[class*='NotebookTitle']", ".mat-toolbar span"]:
        el = await page.query_selector(sel)
        if el:
            text = strip_suffix((await el.inner_text()).strip())
            if text and text not in ("NotebookLM", ""):
                title = text
                break

    print(f"  Title: {title}")

    folder = output_root / safe_dirname(title)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".notebook_id").write_text(nb_id)

    meta = {
        "title": title,
        "notebook_id": nb_id,
        "url": actual_url,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Sources — click each one and save the full text
    sources = await scrape_sources(page, folder)
    meta["sources"] = sources

    # Studio artifacts (download)
    downloaded = await download_studio_artifacts(page, folder)
    meta["studio_artifacts"] = downloaded

    (folder / "metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  Saved → {folder.name}/")
    return meta


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_index(output_dir: Path) -> dict:
    """Load index.json, deduplicated by notebook_id (last entry wins)."""
    path = output_dir / "index.json"
    if not path.exists():
        return {}
    try:
        entries = json.loads(path.read_text())
        # Deduplicate: keep the last entry for each notebook_id
        seen = {}
        for entry in entries:
            key = entry.get("notebook_id") or entry.get("title", "")
            seen[key] = entry
        return seen
    except Exception:
        return {}


def save_index(output_dir: Path, index: dict):
    path = output_dir / "index.json"
    path.write_text(
        json.dumps(list(index.values()), indent=2, ensure_ascii=False), encoding="utf-8"
    )


async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output:  {OUTPUT_DIR}")
    print(f"Profile: {SCRAPER_PROFILE}\n")

    profile = init_profile()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
            accept_downloads=True,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await wait_for_login(page)
        await page.wait_for_timeout(2000)

        if args.retry_sources:
            # Re-scrape source text only for notebooks with an empty sources/ dir
            needs_sources = [
                d for d in OUTPUT_DIR.iterdir()
                if d.is_dir()
                and not any(d.joinpath("sources").glob("*.txt"))
                and (d / ".notebook_id").exists()
            ]
            print(f"--retry-sources: {len(needs_sources)} notebooks with missing source text\n")
            index = load_index(OUTPUT_DIR)
            for i, nb_dir in enumerate(sorted(needs_sources), 1):
                nb_id = (nb_dir / ".notebook_id").read_text().strip()
                print(f"\n[{i}/{len(needs_sources)}] {nb_dir.name}")
                url = f"https://notebooklm.google.com/notebook/{nb_id}"
                await page.goto(url, wait_until="load", timeout=60000)
                await page.wait_for_timeout(3000)
                sources = await scrape_sources(page, nb_dir)
                if nb_id in index:
                    index[nb_id]["sources"] = sources
                save_index(OUTPUT_DIR, index)
            print(f"\nDone. Results: {OUTPUT_DIR}")
            await ctx.close()
            return

        nb_ids = await collect_all_notebook_ids(page)
        done_ids = load_done_ids(OUTPUT_DIR)
        remaining = [i for i in nb_ids if i not in done_ids]
        print(f"\n{len(remaining)} to scrape, {len(done_ids)} already done\n")

        index = load_index(OUTPUT_DIR)
        for i, nb_id in enumerate(remaining, 1):
            print(f"\n[{i}/{len(remaining)}] ID: {nb_id}")
            try:
                meta = await scrape_notebook(page, nb_id, OUTPUT_DIR)
                index[nb_id] = meta
                save_index(OUTPUT_DIR, index)
            except Exception as e:
                print(f"  ERROR: {e}")
                index[nb_id] = {"notebook_id": nb_id, "error": str(e)}

        save_index(OUTPUT_DIR, index)
        print(f"\n{'='*60}")
        print(f"Done! {len(remaining)} new notebooks scraped.")
        print(f"Total: {len(index)} notebooks in index.")
        print(f"Results: {OUTPUT_DIR}")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
