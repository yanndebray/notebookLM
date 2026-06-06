#!/usr/bin/env python3
"""
NotebookLM scraper v3
- Downloads studio artifacts via the ⋮ → Download menu (actual files)
- Extracts source names from the left panel
- Skips notebooks whose .notebook_id file already exists
- Uses persistent .chrome_scraper_profile so login only needed once

Usage:  python3 scrape_notebooklm.py
"""

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from playwright.async_api import async_playwright, Download

OUTPUT_DIR = Path(__file__).parent / "notebooks"
SCRAPER_PROFILE = Path(__file__).parent / ".chrome_scraper_profile"
CHROME_PROFILE_SRC = Path.home() / "Library/Application Support/Google/Chrome/Default"
NOTEBOOKLM_URL = "https://notebooklm.google.com/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_dirname(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:100] or "untitled"


def strip_suffix(title: str) -> str:
    return re.sub(r'\s*[-–|]\s*NotebookLM\s*$', '', title).strip()


def load_done_ids() -> set[str]:
    done = set()
    for p in OUTPUT_DIR.glob("*/.notebook_id"):
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
    print("Script will continue automatically.\n")
    for i in range(120):
        await page.wait_for_timeout(5000)
        if "notebooklm.google.com" in page.url and "accounts.google.com" not in page.url:
            print(f"Logged in after {(i+1)*5}s.")
            return True
    print("Timed out waiting for login.")
    return False


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
# Sources
# ---------------------------------------------------------------------------

async def get_sources(page) -> list[str]:
    sources = []
    # Wait briefly for sources panel
    await page.wait_for_timeout(1000)

    # Try progressively broader selectors
    selector_groups = [
        # Specific NotebookLM source chip selectors
        [".source-chip .chip-label", "[class*='source-chip'] [class*='label']",
         "source-chip [class*='label']"],
        # Material list items in sources panel
        [".sources-panel mat-list-item .mdc-list-item__primary-text",
         "[class*='sources'] mat-list-item span"],
        # Any list item text in the left panel area
        ["[class*='SourceList'] [class*='title']",
         "[class*='source-list'] [class*='label']"],
    ]
    for group in selector_groups:
        for sel in group:
            items = await page.query_selector_all(sel)
            for item in items:
                try:
                    text = (await item.inner_text()).strip()
                    if text and len(text) > 2 and text not in sources:
                        sources.append(text)
                except Exception:
                    pass
        if sources:
            break

    # Fallback: grab all source chip texts visible in left panel
    if not sources:
        try:
            panel = await page.query_selector(
                ".sources-panel, [class*='sources-panel'], "
                "[class*='SourcesPanel'], [class*='left-panel']"
            )
            if panel:
                # Look for any label-like spans that aren't buttons
                items = await panel.query_selector_all(
                    "span[class*='label'], span[class*='title'], "
                    ".mdc-list-item__primary-text, .mat-list-text"
                )
                for item in items:
                    try:
                        text = (await item.inner_text()).strip()
                        if text and len(text) > 2 and text not in sources:
                            sources.append(text)
                    except Exception:
                        pass
        except Exception:
            pass

    return sources


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
    await page.wait_for_timeout(1500)

    # Screenshot the full page for reference
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

    # Sources
    sources = await get_sources(page)
    meta["sources"] = sources
    if sources:
        (folder / "sources.txt").write_text("\n".join(sources), encoding="utf-8")
        print(f"  Sources ({len(sources)}): {sources[:2]}{'...' if len(sources)>2 else ''}")
    else:
        print("  Sources: none detected")

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

async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output: {OUTPUT_DIR}\n")

    done_ids = load_done_ids()
    print(f"Already done: {len(done_ids)} notebooks (will skip)\n")

    profile = init_profile()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
            # Playwright will save downloads here by default; we move them manually
            accept_downloads=True,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if not await wait_for_login(page):
            await ctx.close()
            return

        await page.wait_for_timeout(2000)
        nb_ids = await collect_all_notebook_ids(page)

        remaining = [i for i in nb_ids if i not in done_ids]
        print(f"\n{len(remaining)} notebooks to scrape (skipping {len(done_ids)} done)\n")

        index = []
        # Load existing index entries for already-done notebooks
        existing_index = OUTPUT_DIR / "index.json"
        if existing_index.exists():
            try:
                index = json.loads(existing_index.read_text())
            except Exception:
                pass

        for i, nb_id in enumerate(remaining, 1):
            print(f"\n[{i}/{len(remaining)}] ID: {nb_id}")
            try:
                meta = await scrape_notebook(page, nb_id, OUTPUT_DIR)
                index.append(meta)
                # Save index after each notebook so progress is preserved
                existing_index.write_text(
                    json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception as e:
                print(f"  ERROR: {e}")
                index.append({"notebook_id": nb_id, "error": str(e)})

        existing_index.write_text(
            json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n{'='*60}")
        print(f"Done! {len(remaining)} new notebooks scraped.")
        print(f"Total index entries: {len(index)}")
        print(f"Results: {OUTPUT_DIR}")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
