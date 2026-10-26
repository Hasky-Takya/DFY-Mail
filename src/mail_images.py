"""本地邮件图片的有界提取，不读取远程URL、不保存图片。"""

import hashlib
import io
import re
import warnings
from urllib.parse import unquote


MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}


def validate_image_contract(images):
    if not isinstance(images, (list, tuple)) or len(images) > MAX_IMAGES:
        raise ValueError("图片数量超限。")
    total = 0
    seen = set()
    for item in images:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not re.fullmatch(r"image-[1-4]", item["id"]) or item["id"] in seen
                or item.get("mime") not in MIME_BY_FORMAT.values()
                or not isinstance(item.get("data"), bytes) or not 0 < len(item["data"]) <= MAX_IMAGE_BYTES
                or item.get("sha256") != hashlib.sha256(item["data"]).hexdigest()):
            raise ValueError("图片载荷结构无效。")
        seen.add(item["id"])
        total += len(item["data"])
    if total > MAX_TOTAL_BYTES:
        raise ValueError("图片总大小超限。")


def _decode(data):
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        raise RuntimeError("图片处理需要Pillow，请先安装requirements.txt依赖。") from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in MIME_BY_FORMAT:
                    return None, None, "unsupported_image_format"
                if image.width * image.height > MAX_PIXELS:
                    return None, None, "image_pixels_exceeded"
                animated = getattr(image, "n_frames", 1) > 1
                format_name = image.format
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.seek(0)
                image.load()
                if animated:
                    output = io.BytesIO()
                    image.convert("RGBA").save(output, format="PNG")
                    return output.getvalue(), "image/png", "animated_first_frame_only"
                return data, MIME_BY_FORMAT[format_name], None
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None, None, "invalid_image"


def _parts(message):
    if message.get_content_type() == "message/rfc822":
        return
    if message.is_multipart():
        for child in message.iter_parts():
            yield from _parts(child)
    else:
        yield message


def extract_images(message):
    parts = list(_parts(message))
    cids = set()
    for part in parts:
        if part.get_content_type() == "text/html" and part.get_content_disposition() != "attachment":
            try:
                html = part.get_content()
            except (LookupError, UnicodeError):
                continue
            if isinstance(html, str):
                cids.update(unquote(match).casefold() for match in re.findall(r"(?i)cid:([^\s\"'<>]+)", html))
    images, notices, seen = [], [], set()
    total = 0
    index = 0
    for part in parts:
        mime = part.get_content_type()
        cid = str(part.get("Content-ID", "")).strip("<>").casefold()
        attached = part.get_content_disposition() == "attachment"
        named = bool(part.get_filename())
        if not ((mime.startswith("image/") and (attached or cid in cids or (named and not cid and part.get_content_disposition() != "inline")))
                or (attached and named and str(part.get_filename()).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")))):
            continue
        index += 1
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes) or not data:
            notices.append({"index": index, "code": "invalid_image"})
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        if len(images) >= MAX_IMAGES:
            notices.append({"index": index, "code": "image_count_exceeded"})
            continue
        if len(data) > MAX_IMAGE_BYTES or total + len(data) > MAX_TOTAL_BYTES:
            notices.append({"index": index, "code": "image_bytes_exceeded"})
            continue
        data, actual_mime, notice = _decode(data)
        if data is None:
            notices.append({"index": index, "code": notice})
            continue
        if len(data) > MAX_IMAGE_BYTES or total + len(data) > MAX_TOTAL_BYTES:
            notices.append({"index": index, "code": "image_bytes_exceeded"})
            continue
        if notice:
            notices.append({"index": index, "code": notice})
        digest = hashlib.sha256(data).hexdigest()
        if any(item["sha256"] == digest for item in images):
            continue
        total += len(data)
        images.append({"id": f"image-{len(images) + 1}", "sha256": digest, "mime": actual_mime, "data": data})
    return images, notices


def public_mail(message):
    result = {key: value for key, value in message.items() if key != "images"}
    result["images"] = [{"id": item["id"], "mime": item["mime"], "sha256": item["sha256"],
                         "bytes": len(item["data"])} for item in message.get("images", [])]
    return result
