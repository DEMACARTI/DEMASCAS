"""
DEMASCAS — core/screen/look.py
"What's on my screen?" / "what's in this picture?" — answered by the vision
model (core/screen/vlm.py, loaded on demand), helped by Apple's on-device
text recognition.  Falls back to reading the screen's text when no vision
model is set up.  Everything stays on this Mac.
"""

from __future__ import annotations

import base64
import os

from core.screen import ocr, vlm


def _shrink(path: str, max_side: int = 1280) -> str:
    """PNG screenshot → base64 JPEG, longest side ≤ max_side (a Retina shot
    is 3–6 MB; the model sees the same, 10× faster)."""
    try:
        import Quartz
        from Foundation import NSURL
        src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
        opts = {Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
                Quartz.kCGImageSourceThumbnailMaxPixelSize: max_side,
                Quartz.kCGImageSourceCreateThumbnailWithTransform: True}
        img = Quartz.CGImageSourceCreateThumbnailAtIndex(src, 0, opts)
        data = Quartz.CFDataCreateMutable(None, 0)
        dest = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
        Quartz.CGImageDestinationAddImage(dest, img, {Quartz.kCGImageDestinationLossyCompressionQuality: 0.8})
        Quartz.CGImageDestinationFinalize(dest)
        return base64.b64encode(bytes(data)).decode()
    except Exception:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()


def _spoken(text: str, max_sentences: int = 2) -> str:
    """Markdown off, at most two sentences — it's said aloud."""
    import re
    t = " ".join(text.replace("**", "").replace("#", "").replace("`", "").split())
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", t)
    return " ".join(parts[:max_sentences])


def describe_screen(question: str = "") -> dict:
    """Look at the window the user is looking at and describe it (or answer
    `question` about it)."""
    from core import axui
    app = axui.app_on_screen() or axui.frontmost_app()
    box = axui._window_box(app) if app else None
    path = ocr.screenshot_window(box)
    if not path:
        return {"success": False, "message": "[-] I couldn't take a screenshot — DEMASCAS needs Screen Recording permission "
                                             "(System Settings → Privacy & Security → Screen Recording)."}
    try:
        lines = [t["text"] for t in ocr.read_text(path, fast=True)] if ocr.available() else []
        hint = "\n".join(lines[:40])
        if vlm.configured():
            ask = (f"This is a screenshot of the user's {app or 'screen'} window. "
                   + (f"Answer their question about it: {question}" if question else "Describe what is on the screen.")
                   + " Reply in at most two short spoken sentences, no lists or markdown. Say what matters, skip the details."
                   + (f"\nText recognised on the screen (may help):\n{hint}" if hint else ""))
            answer = vlm.ask(ask, image_b64=_shrink(path))
            if answer and not answer.startswith("[-]"):
                return {"success": True, "app": app, "message": _spoken(answer), "screen_text": hint[:1500]}
        # no vision model (or it failed): what can be read
        if lines:
            return {"success": True, "app": app, "screen_text": hint[:1500],
                    "message": f"In {app} I can read: " + "; ".join(lines[:8]) + "."}
        return {"success": False, "message": f"[-] I couldn't make out anything in {app}."}
    finally:
        os.unlink(path)


def look_at_image(path: str, question: str = "") -> dict:
    """Describe an image file, or answer a question about it."""
    p = os.path.expanduser(path or "")
    if not os.path.isfile(p):
        return {"success": False, "message": f"[-] I can't find the image {path}."}
    codes = ocr.barcodes(p) if ocr.available() else []
    # a QR code / barcode is READ exactly by the on-device detector — say what it holds
    if codes and not question:
        c = codes[0]
        what = f"links to {c['payload']}" if c["payload"].startswith(("http://", "https://")) else f"says “{c['payload'][:120]}”"
        return {"success": True, "codes": codes, "message": f"It's a {c['kind']} code that {what}."}
    if vlm.configured():
        ask = ((question or "Describe this image.") + " Reply in at most two short spoken sentences, no lists."
               + (f" (It contains a {codes[0]['kind']} code.)" if codes else ""))
        answer = vlm.ask(ask, image_b64=_shrink(p))
        if answer and not answer.startswith("[-]"):
            return {"success": True, "message": _spoken(answer)}
    labels = ", ".join(l for l, _ in ocr.labels(p)[:3]) if ocr.available() else ""
    text = " ".join(t["text"] for t in ocr.read_text(p, fast=True))[:200] if ocr.available() else ""
    bits = ([f"a {codes[0]['kind']} code"] if codes else []) + ([f"it looks like: {labels}"] if labels else []) + ([f"text: {text}"] if text else [])
    return {"success": bool(bits), "message": ("The image has " + "; ".join(bits) + ".") if bits else "[-] I couldn't make anything out."}


def vision_status() -> dict:
    s = vlm.status()
    if not s["configured"]:
        return {"success": True, "message": "No vision model is set up — I read screens with on-device text recognition only."}
    return {"success": True, **s, "message": f"Vision model {s['model']} is {'loaded' if s['loaded'] else 'unloaded until needed'}."}
