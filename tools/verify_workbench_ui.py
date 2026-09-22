from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "state" / "verification"
URL = "http://127.0.0.1:18767/"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def geometry(page):
    return page.evaluate(
        """
        () => {
          const visible = element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0
              && rect.right > 0 && rect.left < innerWidth && rect.bottom > 0 && rect.top < innerHeight;
          };
          const rows = [...document.querySelectorAll('button,input,textarea,.pane,.topbar')]
            .filter(visible)
            .map(element => {
              const rect = element.getBoundingClientRect();
              return {id: element.id || element.className, left: rect.left, top: rect.top,
                right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height};
            });
          return {
            viewport: {width: innerWidth, height: innerHeight},
            body: {scrollWidth: document.body.scrollWidth, scrollHeight: document.body.scrollHeight},
            rows,
          };
        }
        """
    )


def assert_geometry(result):
    viewport = result["viewport"]
    body = result["body"]
    assert body["scrollWidth"] <= viewport["width"], result
    assert body["scrollHeight"] <= viewport["height"], result
    for row in result["rows"]:
        assert row["left"] >= -1 and row["right"] <= viewport["width"] + 1, row
        assert row["top"] >= -1 and row["bottom"] <= viewport["height"] + 1, row


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {"pages": [], "console_errors": []}
    with sync_playwright() as playwright:
        if not EDGE.is_file():
            raise FileNotFoundError(f"Microsoft Edge not found: {EDGE}")
        browser = playwright.chromium.launch(headless=True, executable_path=str(EDGE))
        for name, width, height, url in (
            ("desktop", 1440, 900, URL),
            ("tablet", 1024, 768, URL),
            ("mobile", 390, 844, URL),
            ("dock", 286, 760, URL + "?dock=1"),
        ):
            page = browser.new_page(viewport={"width": width, "height": height})
            page.on("console", lambda message, n=name: report["console_errors"].append(
                {"page": n, "type": message.type, "text": message.text}
            ) if message.type == "error" else None)
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(1800)
            assert page.title() == "千牛聚合接待"
            assert page.locator("#versionLabel").inner_text().startswith("v1.4.1")
            result = geometry(page)
            assert_geometry(result)
            page.screenshot(path=str(OUTPUT / f"workbench-{name}.png"), full_page=True)
            if name == "desktop":
                page.locator("#settingsButton").click()
                page.locator("#brainServerInput").wait_for(state="visible")
                assert page.locator("#brainTokenInput").input_value() == ""
                settings_geometry = geometry(page)
                assert_geometry(settings_geometry)
                page.screenshot(path=str(OUTPUT / "workbench-settings.png"), full_page=True)
                page.keyboard.press("Escape")
            if name == "mobile":
                page.locator("#sessionsButton").click()
                assert "show-sessions" in (page.locator("body").get_attribute("class") or "")
                page.screenshot(path=str(OUTPUT / "workbench-mobile-sessions.png"), full_page=True)
            report["pages"].append({"name": name, **result})
            page.close()
        browser.close()
    assert not report["console_errors"], report["console_errors"]
    (OUTPUT / "workbench-ui-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "ok": True,
        "screenshots": sorted(path.name for path in OUTPUT.glob("workbench-*.png")),
        "console_errors": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
