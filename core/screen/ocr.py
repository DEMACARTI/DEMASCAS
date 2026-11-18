"""
DEMASCAS — core/screen/ocr.py
Apple's on-device Vision framework (Neural Engine — no language model, no
extra RAM held): read text with its position from a screenshot, find QR
codes / barcodes, and label what an image shows.

Used when the accessibility tree / page structure can't describe something
(canvas apps, images, unlabelled buttons) and for "the picture that looks
like a QR code".  Screenshots are taken to a temp file and deleted.
"""

from __future__ import annotations

import os
import subprocess
import tempfile


def available() -> bool:
    try:
        import Vision  # noqa: F401
        return True
    except Exception:
        return False


def _cgimage(path: str):
    import Quartz
    from Foundation import NSURL
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    if src is None:
        return None
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def _perform(path: str, requests: list) -> bool:
    import Vision
    img = _cgimage(path)
    if img is None:
        return False
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, {})
    ok, _err = handler.performRequests_error_(requests, None)
    return bool(ok)


def read_text(path: str, fast: bool = False) -> list[dict]:
    """[{text, conf, box: [x, y, w, h] as 0–1 of the image, top-left origin}]"""
    import Vision
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(1 if fast else 0)          # 0 accurate, 1 fast
    req.setUsesLanguageCorrection_(not fast)
    if not _perform(path, [req]):
        return []
    out = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        bb = obs.boundingBox()                           # bottom-left origin, normalised
        out.append({"text": str(cands[0].string()), "conf": round(float(cands[0].confidence()), 2),
                    "box": [round(bb.origin.x, 4), round(1 - bb.origin.y - bb.size.height, 4),
                            round(bb.size.width, 4), round(bb.size.height, 4)]})
    return out


def barcodes(path: str) -> list[dict]:
    """QR codes and barcodes in an image: [{kind, payload}]"""
    import Vision
    req = Vision.VNDetectBarcodesRequest.alloc().init()
    if not _perform(path, [req]):
        return []
    return [{"kind": str(o.symbology()).replace("VNBarcodeSymbology", ""), "payload": str(o.payloadStringValue() or "")}
            for o in (req.results() or [])]


def labels(path: str, top: int = 5, min_conf: float = 0.15) -> list[tuple[str, float]]:
    """What the image shows: [("document", 0.82), ("screenshot", 0.6), …]"""
    import Vision
    req = Vision.VNClassifyImageRequest.alloc().init()
    if not _perform(path, [req]):
        return []
    res = [(str(o.identifier()), float(o.confidence())) for o in (req.results() or []) if float(o.confidence()) >= min_conf]
    res.sort(key=lambda x: -x[1])
    return res[:top]


def screenshot_window(window_box: list[int] | None = None) -> str | None:
    """A PNG of the screen (or one window's box, in points) → temp path, or None.
    The caller deletes it."""
    fd, path = tempfile.mkstemp(prefix="dmx-shot-", suffix=".png")
    os.close(fd)
    args = ["screencapture", "-x", "-t", "png"]
    if window_box:
        x, y, w, h = window_box
        args += ["-R", f"{x},{y},{w},{h}"]
    r = subprocess.run(args + [path], capture_output=True, timeout=10)
    if r.returncode != 0 or not os.path.getsize(path):
        os.unlink(path)
        return None
    return path


def screen_text_elements(window_box: list[int] | None = None) -> dict:
    """Text on screen as clickable elements (OCR boxes in screen points)."""
    if not available():
        return {"success": False, "message": "[-] On-screen text reading needs pyobjc-framework-Vision."}
    path = screenshot_window(window_box)
    if not path:
        return {"success": False, "message": "[-] I couldn't take a screenshot (Screen Recording permission?)."}
    try:
        lines = read_text(path)
    finally:
        os.unlink(path)
    ox, oy, W, H = window_box if window_box else (0, 0, *_screen_points())
    els = []
    for t in lines:
        x, y, w, h = t["box"]
        els.append({"id": len(els) + 1, "role": "text", "name": t["text"][:90],
                    "box": [round(ox + x * W), round(oy + y * H), round(w * W), round(h * H)]})
    return {"success": True, "elements": els}


def _screen_points() -> tuple[int, int]:
    try:
        import Quartz
        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return int(b.size.width), int(b.size.height)
    except Exception:
        return 1440, 900


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".heic", ".gif", ".tiff", ".bmp", ".webp")


def find_image_by_content(folder: str = "~/Pictures", what: str = "", limit: int = 60, open: bool = False) -> dict:
    """'the image that looks like a QR code' / 'the screenshot with the
    invoice' — looks INSIDE the images of a folder (newest first)."""
    if not available():
        return {"success": False, "message": "[-] Looking inside images needs pyobjc-framework-Vision."}
    folder_x = os.path.expanduser(folder)
    if not os.path.isdir(folder_x):
        return {"success": False, "message": f"[-] Folder not found: {folder}"}
    files = [os.path.join(folder_x, f) for f in os.listdir(folder_x) if f.lower().endswith(IMAGE_EXT) and not f.startswith(".")]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    want = (what or "").lower()
    qr = any(w in want for w in ("qr", "barcode", "bar code", "scan code"))
    words = [w for w in want.replace(",", " ").split() if len(w) > 2 and w not in ("the", "image", "picture", "photo", "looks", "like", "with", "that", "one", "has")]
    hits = []
    for p in files[:limit]:
        name = os.path.basename(p)
        if qr:
            codes = barcodes(p)
            if codes:
                hits.append({"name": name, "path": p, "why": f"{codes[0]['kind']} code", "score": 1.0})
            continue
        score, why = 0.0, ""
        for lab, conf in labels(p):
            if any(w in lab for w in words):
                score, why = max(score, conf), lab
        if score < 0.3 and words:
            text = " ".join(t["text"] for t in read_text(p, fast=True)).lower()
            if text and sum(w in text for w in words) >= max(1, len(words) // 2):
                score, why = 0.6, "its text mentions it"
        if score >= 0.3:
            hits.append({"name": name, "path": p, "why": why, "score": round(score, 2)})
    hits.sort(key=lambda h: -h["score"])
    if not hits:
        return {"success": True, "found": False, "matches": [], "count": 0,
                "message": f"None of the {min(len(files), limit)} images in {folder} looks like {what or 'that'}."}
    top = hits[:5]
    if open:
        subprocess.run(["open", top[0]["path"]], capture_output=True, timeout=10)
        return {"success": True, "found": True, "opened": top[0]["path"], "matches": top, "count": len(hits),
                "message": f"Opening {top[0]['name']} — it has a {top[0]['why']}." if top[0]["why"].endswith("code")
                           else f"Opening {top[0]['name']} ({top[0]['why']})."}
    return {"success": True, "found": True, "matches": top, "count": len(hits), "first_match_path": top[0]["path"],
            "message": f"Found {len(hits)} image(s) in {folder} that look like {what}: " + ", ".join(h["name"] for h in top) + "."}
