from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import threading
import zlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "_shots"


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def make_png(width: int, height: int, seed: int) -> bytes:
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            block = ((x // 7) + (y // 5) + seed) % 5
            row.extend(((x * 5 + block * 47 + seed * 19) % 256, (y * 7 + block * 31 + seed * 29) % 256, ((x + y) * 3 + block * 61) % 256, 255))
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + png_chunk(b"IEND", b"")


def png_size_from_data_url(value: str) -> tuple[int, int]:
    raw = base64.b64decode(value.split(",", 1)[1])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", raw[16:24])


def digest_data_url(value: str) -> str:
    return hashlib.sha256(base64.b64decode(value.split(",", 1)[1])).hexdigest()


class AppHandler(SimpleHTTPRequestHandler):
    mutant = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.mutant == "omit-overlays" and self.path.split("?", 1)[0] in ("/", "/index.html"):
            source = (ROOT / "index.html").read_text(encoding="utf-8")
            marker = "\n  renderOverlayObjects(ctx);\n}\n\nfunction overlayId"
            assert marker in source, "mutation marker not found"
            source = source.replace(marker, "\n  // mutant: overlay output omitted\n}\n\nfunction overlayId", 1)
            body = source.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class LocalServer:
    def __init__(self, mutant: str = "") -> None:
        handler = type("ConfiguredHandler", (AppHandler,), {"mutant": mutant})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/"

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def state(page: Page) -> dict[str, Any]:
    return page.evaluate("window.__komaTest.getState()")


def canvas_point(page: Page, x: float, y: float) -> tuple[float, float]:
    box = page.locator("#view").bounding_box()
    current = state(page)
    assert box
    return box["x"] + x * box["width"] / current["W"], box["y"] + y * box["height"] / current["H"]


def drag(page: Page, start: tuple[float, float], end: tuple[float, float]) -> None:
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=7)
    page.mouse.up()


def add_images(page: Page, count: int = 3) -> None:
    page.locator(f"#counts button[data-c='{count}']").click()
    files = [
        {"name": f"panel-{index + 1}.png", "mimeType": "image/png", "buffer": make_png(320 + index * 16, 240 + index * 12, index + 1)}
        for index in range(count)
    ]
    page.locator("#fileInput").set_input_files(files)
    page.wait_for_function(f"window.__komaTest.getState().panels.filter(Boolean).length === {count}")


def assert_overlay_output(page: Page) -> None:
    before = digest_data_url(page.evaluate("window.__komaTest.renderDataUrl()"))
    page.locator("[data-add-overlay='bubble-oval']").click()
    page.locator("#overlayText").fill("吹き出しテスト")
    after = digest_data_url(page.evaluate("window.__komaTest.renderDataUrl()"))
    assert before != after, "吹き出しが完成PNGへ描画されていない"
    bubble = state(page)["overlays"][-1]
    assert bubble["type"] == "bubble" and bubble["text"] == "吹き出しテスト"


def dispatch_touch_drag(page: Page, start: tuple[float, float], end: tuple[float, float]) -> None:
    page.evaluate(
        """([sx, sy, ex, ey]) => {
          const el = document.getElementById('view');
          const fire = (type, x, y, buttons) => el.dispatchEvent(new PointerEvent(type, {
            pointerId: 71, pointerType: 'touch', isPrimary: true, bubbles: true,
            clientX: x, clientY: y, button: 0, buttons
          }));
          fire('pointerdown', sx, sy, 1); fire('pointermove', ex, ey, 1); fire('pointerup', ex, ey, 0);
        }""",
        [*start, *end],
    )


def dispatch_pinch(page: Page, center: tuple[float, float]) -> None:
    page.evaluate(
        """([cx, cy]) => {
          const el = document.getElementById('view');
          const fire = (type, id, x, y, buttons) => el.dispatchEvent(new PointerEvent(type, {
            pointerId: id, pointerType: 'touch', isPrimary: id === 81, bubbles: true,
            clientX: x, clientY: y, button: 0, buttons
          }));
          fire('pointerdown', 81, cx - 18, cy, 1);
          fire('pointerdown', 82, cx + 18, cy, 1);
          fire('pointermove', 81, cx - 42, cy - 4, 1);
          fire('pointermove', 82, cx + 42, cy + 4, 1);
          fire('pointerup', 81, cx - 42, cy - 4, 0);
          fire('pointerup', 82, cx + 42, cy + 4, 0);
        }""",
        list(center),
    )


def run_full(browser: Browser, browser_name: str, url: str, width: int, height: int, mobile: bool) -> None:
    context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile, device_scale_factor=2 if mobile else 1)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    page.on("console", lambda message: errors.append(f"console: {message.text}") if message.type == "error" else None)
    page.goto(url, wait_until="networkidle")
    assert page.title() == "コマ割りツール"
    assert page.locator("#dlBtn").is_visible(), "HTTP表示でオフライン保存が出ていない"
    if not mobile:
        with page.expect_download() as download_info:
            page.locator("#dlBtn").click()
        download = download_info.value
        assert download.suggested_filename == "コマ割りツール.html"
        offline_source = Path(download.path()).read_text(encoding="utf-8")
        assert "window.__komaTest" in offline_source and "data-add-overlay=\"bubble-oval\"" in offline_source
    add_images(page, 3)

    loaded = state(page)
    assert len([panel for panel in loaded["panels"] if panel]) == 3
    assert all(panel["s"] >= 0.999 for panel in loaded["panels"] if panel), "自動サイズで元画像が縮小された"
    output_url = page.evaluate("window.__komaTest.renderDataUrl()")
    assert png_size_from_data_url(output_url) == (loaded["W"], loaded["H"])

    geometry = page.evaluate("window.__komaTest.getGeometry()")
    first_line = geometry["splits"][0]["seg"]
    midpoint = {"x": first_line[0]["x"] * .73 + first_line[1]["x"] * .27, "y": first_line[0]["y"] * .73 + first_line[1]["y"] * .27}
    tree_before = json.dumps(loaded["tree"], sort_keys=True)
    line_start = canvas_point(page, midpoint["x"], midpoint["y"])
    drag(page, line_start, (line_start[0] + 18, line_start[1]))
    tree_after_move = json.dumps(state(page)["tree"], sort_keys=True)
    assert tree_after_move != tree_before, "線の移動が状態へ反映されていない"

    geometry = page.evaluate("window.__komaTest.getGeometry()")
    endpoint = geometry["splits"][0]["seg"][0]
    endpoint_start = canvas_point(page, endpoint["x"], endpoint["y"])
    drag(page, endpoint_start, (endpoint_start[0] + 24, endpoint_start[1]))
    tree_after_tilt = json.dumps(state(page)["tree"], sort_keys=True)
    assert tree_after_tilt != tree_after_move, "線端のドラッグで傾きが変わっていない"
    page.keyboard.press("Control+z")
    assert json.dumps(state(page)["tree"], sort_keys=True) == tree_after_move, "Ctrl+Zで線の傾きが戻らない"

    geometry = page.evaluate("window.__komaTest.getGeometry()")
    leaf = geometry["leaves"][0]
    center = canvas_point(page, leaf["bb"]["x"] + leaf["bb"]["w"] * 0.42, leaf["bb"]["y"] + leaf["bb"]["h"] * 0.58)
    panel_before = dict(state(page)["panels"][0])
    dispatch_touch_drag(page, center, (center[0] + 22, center[1] + 14))
    panel_after_drag = state(page)["panels"][0]
    assert (panel_after_drag["x"], panel_after_drag["y"]) != (panel_before["x"], panel_before["y"]), "touchドラッグで画像が動かない"
    scale_before = panel_after_drag["s"]
    dispatch_pinch(page, center)
    assert state(page)["panels"][0]["s"] > scale_before, "二本指ピンチで画像が拡大しない"

    assert_overlay_output(page)
    page.locator("[data-add-overlay='text']").click()
    page.locator("#overlayText").fill("縦書き")
    page.locator("#overlayVertical").check()
    page.locator("[data-add-overlay='rect']").click()
    page.locator("[data-add-overlay='ellipse']").click()
    page.locator("[data-add-overlay='line']").click()
    types = [overlay["type"] for overlay in state(page)["overlays"]]
    assert types.count("text") == 1 and types.count("shape") >= 3
    line_id = state(page)["overlays"][-1]["id"]
    page.locator("#overlayList li:first-child .layer-icon").nth(1).click()
    assert next(item for item in state(page)["overlays"] if item["id"] == line_id)["locked"] is True
    page.locator("#overlayList li:first-child .layer-icon").nth(0).click()
    assert next(item for item in state(page)["overlays"] if item["id"] == line_id)["visible"] is False
    page.locator("#overlayList li:first-child .layer-icon").nth(0).click()
    page.locator("#overlayBack").click()
    assert state(page)["overlays"][0]["id"] == line_id
    page.locator("#overlayFront").click()
    assert state(page)["overlays"][-1]["id"] == line_id

    before_effect_samples = [page.evaluate("([x,y]) => window.__komaTest.samplePixel(x,y)", [loaded["W"] * x, loaded["H"] * y]) for x, y in ((.13, .14), (.17, .19), (.22, .24), (.27, .28))]
    page.locator("[data-overlay-tool='mosaic']").click()
    page.locator("#view").scroll_into_view_if_needed()
    box = page.locator("#view").bounding_box(); assert box
    drag(page, (box["x"] + box["width"] * .1, box["y"] + box["height"] * .1), (box["x"] + box["width"] * .31, box["y"] + box["height"] * .32))
    mosaic = state(page)["overlays"][-1]
    assert mosaic["type"] == "effect" and mosaic["kind"] == "mosaic"
    after_effect_samples = [page.evaluate("([x,y]) => window.__komaTest.samplePixel(x,y)", [loaded["W"] * x, loaded["H"] * y]) for x, y in ((.13, .14), (.17, .19), (.22, .24), (.27, .28))]
    assert before_effect_samples != after_effect_samples, "モザイク範囲の画素が変わっていない"

    page.locator("[data-overlay-tool='blur']").click()
    page.locator("#view").scroll_into_view_if_needed(); box = page.locator("#view").bounding_box(); assert box
    drag(page, (box["x"] + box["width"] * .55, box["y"] + box["height"] * .1), (box["x"] + box["width"] * .78, box["y"] + box["height"] * .3))
    assert state(page)["overlays"][-1]["kind"] == "blur"

    page.locator("[data-overlay-tool='patch']").click()
    page.locator("#view").scroll_into_view_if_needed(); box = page.locator("#view").bounding_box(); assert box
    drag(page, (box["x"] + box["width"] * .12, box["y"] + box["height"] * .62), (box["x"] + box["width"] * .3, box["y"] + box["height"] * .76))
    patch = state(page)["overlays"][-1]
    assert patch["type"] == "shape" and patch.get("patch") and patch["fill"].startswith("#")
    patch_pixel = page.evaluate("([x,y]) => window.__komaTest.samplePixel(x,y)", [patch["x"] + patch["w"] / 2, patch["y"] + patch["h"] / 2])
    assert "#" + "".join(f"{channel:02x}" for channel in patch_pixel[:3]) == patch["fill"].lower()

    page.locator("#shortsBtn").click()
    shorts = state(page)
    assert (shorts["W"], shorts["H"], shorts["aspect"], shorts["auto"]) == (1080, 1920, "9:16", False)

    page.wait_for_timeout(900)
    saved = page.evaluate("window.__komaTest.autosaveGet()")
    assert saved and saved["snapshot"] and len(saved["images"]) == 3
    overlay_count = len(shorts["overlays"])
    page.reload(wait_until="networkidle")
    page.locator("#recoverBtn").wait_for(state="visible")
    page.locator("#recoverBtn").click()
    page.wait_for_function("window.__komaTest.getState().panels.filter(Boolean).length === 3")
    recovered = state(page)
    assert len(recovered["overlays"]) == overlay_count and (recovered["W"], recovered["H"]) == (1080, 1920)

    if mobile:
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"iPhone幅で横にはみ出している: {overflow}px"

    SHOTS.mkdir(parents=True, exist_ok=True)
    suffix = "iphone-390x844" if mobile else "pc-1280x900"
    page.screenshot(path=str(SHOTS / f"{browser_name}-{suffix}.png"), full_page=mobile)
    assert not errors, " / ".join(errors)
    context.close()
    print(f"PASS {browser_name} {suffix}: base, line, undo, overlays, effects, touch, autosave, export")


def run_overlay_case(browser: Browser, url: str) -> None:
    context = browser.new_context(viewport={"width": 1000, "height": 760})
    page = context.new_page()
    page.goto(url, wait_until="networkidle")
    page.locator("#fileInput").set_input_files({"name": "base.png", "mimeType": "image/png", "buffer": make_png(320, 240, 9)})
    page.wait_for_function("window.__komaTest.getState().panels.filter(Boolean).length === 1")
    assert_overlay_output(page)
    context.close()
    print("PASS overlay output case")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", choices=("all", "chromium", "webkit"), default="all")
    parser.add_argument("--mutant", choices=("", "omit-overlays"), default="")
    parser.add_argument("--case", choices=("full", "overlays"), default="full")
    args = parser.parse_args()

    names = ("chromium", "webkit") if args.browser == "all" else (args.browser,)
    with LocalServer(args.mutant) as url, sync_playwright() as playwright:
        for name in names:
            browser_type = getattr(playwright, name)
            browser = browser_type.launch(headless=True)
            try:
                if args.case == "overlays":
                    run_overlay_case(browser, url)
                else:
                    run_full(browser, name, url, 1280, 900, False)
                    run_full(browser, name, url, 390, 844, True)
            finally:
                browser.close()


if __name__ == "__main__":
    main()
