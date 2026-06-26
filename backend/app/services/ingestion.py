import asyncio
import os
import logging
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright
from app.core.config import settings

logger = logging.getLogger(__name__)

class ViewportConfig(BaseModel):
    width: int
    height: int
    name: str

class DOMElementMap(BaseModel):
    tag: str
    id: Optional[str] = None
    classes: Optional[str] = None
    x: float
    y: float
    width: float = Field(..., gt=0)
    height: float = Field(..., gt=0)

class IngestionResult(BaseModel):
    viewport: str
    screenshot_path: str
    dom_elements: List[DOMElementMap]

class VisualIngestionService:
    # Private and immutable tuple of viewports
    _VIEWPORTS: Tuple[ViewportConfig, ...] = (
        ViewportConfig(width=1440, height=900, name="Desktop"),
        ViewportConfig(width=768, height=1024, name="Tablet"),
        ViewportConfig(width=375, height=812, name="Mobile"),
    )

    async def process_target_site(self, url: str) -> List[IngestionResult]:
        """
        Launches a headless browser to capture screenshots and extract layout metadata
        for each viewport concurrently. Throttled by settings.MAX_CONCURRENCY_LIMIT.
        """
        results: List[IngestionResult] = []
        semaphore = asyncio.Semaphore(settings.MAX_CONCURRENCY_LIMIT)

        async def capture_viewport(viewport: ViewportConfig, browser) -> IngestionResult:
            async with semaphore:
                logger.info(f"Starting ingestion for viewport: {viewport.name} ({viewport.width}x{viewport.height})")
                context = await browser.new_context(
                    viewport={"width": viewport.width, "height": viewport.height}
                )
                try:
                    page = await context.new_page()
                    # Open target URL
                    await page.goto(url, wait_until="load", timeout=30000)
                    
                    # Ensure storage directory exists
                    current_dir = os.path.dirname(os.path.abspath(__file__))
                    backend_dir = os.path.dirname(os.path.dirname(current_dir))
                    storage_dir = os.path.join(backend_dir, "storage", "screenshots")
                    os.makedirs(storage_dir, exist_ok=True)
                    screenshot_path = os.path.join(storage_dir, f"{viewport.name}_snapshot.png")
                    
                    # Capture page screenshot
                    await page.screenshot(path=screenshot_path)
                    logger.info(f"Saved screenshot for {viewport.name} at: {screenshot_path}")

                    # Evaluate page script to pull bounding rectangles of semantic/layout nodes
                    js_script = """
                    () => {
                        const selectors = ['header', 'footer', 'main', 'nav', 'button', 'section', '.container'];
                        const elementsData = [];
                        for (const selector of selectors) {
                            const elements = document.querySelectorAll(selector);
                            for (const el of elements) {
                                const rect = el.getBoundingClientRect();
                                // Filter out element with width === 0 or height === 0
                                if (rect.width > 0 && rect.height > 0) {
                                    const className = typeof el.className === 'string' 
                                        ? el.className 
                                        : (el.getAttribute('class') || '');
                                    elementsData.push({
                                        tag: el.tagName.toLowerCase(),
                                        id: el.id || null,
                                        classes: className.trim() || null,
                                        x: rect.left,
                                        y: rect.top,
                                        width: rect.width,
                                        height: rect.height
                                    });
                                }
                            }
                        }
                        return elementsData;
                    }
                    """
                    raw_elements = await page.evaluate(js_script)
                    
                    # Parse and validate with Pydantic V2
                    dom_elements: List[DOMElementMap] = []
                    for data in raw_elements:
                        try:
                            # Strict type safety and validation check
                            dom_elements.append(DOMElementMap(**data))
                        except Exception as val_err:
                            logger.warning(
                                f"Filtered out element {data.get('tag')} in {viewport.name} "
                                f"due to validation error: {val_err}"
                            )
                    
                    logger.info(f"Extracted {len(dom_elements)} valid DOM elements for {viewport.name}")
                    return IngestionResult(
                        viewport=viewport.name,
                        screenshot_path=screenshot_path,
                        dom_elements=dom_elements
                    )
                except Exception as e:
                    logger.error(f"Failed ingestion for viewport {viewport.name}: {e}")
                    raise e
                finally:
                    await context.close()

        # Rigorous lifetime management for Playwright and browser resources
        playwright_context = None
        browser = None
        try:
            playwright_context = await async_playwright().start()
            browser = await playwright_context.chromium.launch(headless=True)
            
            tasks = [capture_viewport(vp, browser) for vp in self._VIEWPORTS]
            results = await asyncio.gather(*tasks)
            return results
        except Exception as err:
            logger.error(f"Browser or task level exception in process_target_site: {err}")
            raise err
        finally:
            if browser:
                await browser.close()
                logger.info("Browser connection closed successfully.")
            if playwright_context:
                await playwright_context.stop()
                logger.info("Playwright process stopped successfully.")
