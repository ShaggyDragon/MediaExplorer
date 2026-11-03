"""Picture metadata for the viewer's info panel (I).

Three layers, cheapest first, all read with Pillow's lazy open (headers only -- the
pixels are never decoded):
  - the file itself: dimensions, format, colour mode
  - camera EXIF: date taken, make/model, lens, exposure, focal length, GPS
  - every text block embedded in the file. For a PNG that is the tEXt/iTXt/zTXt
    chunks, which is where ComfyUI writes its `prompt` (the executed node graph) and
    `workflow` (the editor graph), and other generators write `parameters`. These are
    returned raw (pretty-printed when they are JSON) for the scrollable panel, plus a
    readable summary pulled out of a ComfyUI graph: the prompt texts, labelled
    positive/negative by following the sampler's links, and the sampler settings.
"""
from __future__ import annotations

import io
import json

from PIL import ExifTags, Image

TEXT_LIMIT = 400_000          # chars per text block sent to the page (a workflow is ~50KB)

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825


def _ratio(v):
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _shutter(v):
    f = _ratio(v)
    if not f:
        return None
    return f"1/{round(1 / f)} s" if f < 1 else f"{f:g} s"


def _gps(gps):
    def deg(vals, ref):
        try:
            d, m, s = (float(x) for x in vals)
        except (TypeError, ValueError):
            return None
        out = d + m / 60 + s / 3600
        return -out if ref in ("S", "W") else out
    lat = deg(gps.get(2), gps.get(1))
    lon = deg(gps.get(4), gps.get(3))
    if lat is None or lon is None:
        return None
    return {"lat": round(lat, 6), "lon": round(lon, 6)}


def _camera(im):
    try:
        ex = im.getexif()
    except Exception:
        return {}
    if not ex:
        return {}
    base = {ExifTags.TAGS.get(k, k): v for k, v in ex.items()}
    try:
        sub = {ExifTags.TAGS.get(k, k): v for k, v in ex.get_ifd(_EXIF_IFD).items()}
    except Exception:
        sub = {}
    try:
        gps = dict(ex.get_ifd(_GPS_IFD))
    except Exception:
        gps = {}

    def s(v):
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        v = str(v).strip().strip("\x00").strip()
        # cameras write placeholder strings into empty fields (the FinePix writes "None")
        return v if v and v.lower() not in ("none", "unknown", "n/a", "-") else None

    make, model = s(base.get("Make")), s(base.get("Model"))
    camera = model if (model and make and model.lower().startswith(make.lower().split()[0])) \
        else " ".join(x for x in (make, model) if x) or None
    fnum = _ratio(sub.get("FNumber"))
    focal = _ratio(sub.get("FocalLength"))
    out = {
        "taken": s(sub.get("DateTimeOriginal") or base.get("DateTime")),
        "camera": camera,
        "lens": s(sub.get("LensModel")),
        "aperture": f"f/{fnum:g}" if fnum else None,
        "shutter": _shutter(sub.get("ExposureTime")),
        "iso": sub.get("ISOSpeedRatings") or sub.get("PhotographicSensitivity"),
        "focal": f"{focal:g} mm" if focal else None,
        "software": s(base.get("Software")),
        "gps": _gps(gps) if gps else None,
    }
    if isinstance(out["iso"], (tuple, list)):
        out["iso"] = out["iso"][0] if out["iso"] else None
    return {k: v for k, v in out.items() if v not in (None, "", ())}


def _texts(im):
    """Every embedded text block: PNG chunks (Pillow's .text) plus string info keys."""
    found = {}
    try:
        found.update(getattr(im, "text", {}) or {})       # PNG: loads all text chunks
    except Exception:
        pass
    for k, v in (im.info or {}).items():
        if k in found or not isinstance(v, (str, bytes)):
            continue
        if k in ("icc_profile", "exif", "dpi", "xmp", "transparency", "gamma"):
            continue
        if isinstance(v, bytes):
            try:
                v = v.decode("utf-8")
            except UnicodeDecodeError:
                continue
        found[k] = v
    out = []
    for k, v in found.items():
        v = str(v)
        pretty, is_json = v, False
        t = v.strip()
        if t[:1] in "{[":
            try:
                pretty = json.dumps(json.loads(t), indent=2, ensure_ascii=False)
                is_json = True
            except ValueError:
                # JSON followed by more text (the A1111/Civitai `parameters` layout puts
                # "Negative prompt: ..." and a settings line after a JSON prompt):
                # pretty-print the JSON part and keep the rest as written
                try:
                    obj, end = json.JSONDecoder().raw_decode(t)
                    pretty = json.dumps(obj, indent=2, ensure_ascii=False) + "\n" + t[end:].lstrip("\n")
                except ValueError:
                    pass
        out.append({"key": str(k), "json": is_json, "chars": len(v),
                    "value": pretty[:TEXT_LIMIT], "cut": len(pretty) > TEXT_LIMIT})
    return out


# ------------------------------------------------- A1111 / Civitai "parameters" summary

def _split_settings(line):
    """'Steps: 6, Sampler: Euler, Civitai resources: [{...}]' -> ordered pairs.
    Commas inside brackets, braces or quotes do not split."""
    parts, buf, depth, q = [], [], 0, None
    for ch in line:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
        elif ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(buf)); buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    pairs = []
    for p in parts:
        k, sep, v = p.partition(":")
        if sep and k.strip() and len(k.strip()) < 40:
            pairs.append((k.strip(), v.strip()))
    return pairs


def a1111_summary(text: str):
    """prompt / 'Negative prompt:' / 'Steps: ...' -- the layout A1111, Forge and the
    Civitai generator write into a PNG `parameters` block."""
    lines = (text or "").strip().split("\n")
    at = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("Steps:")), None)
    if at is None:
        return None
    body = "\n".join(lines[:at])
    pos, sep, neg = body.partition("\nNegative prompt:")
    if not sep and body.startswith("Negative prompt:"):
        pos, neg = "", body[len("Negative prompt:"):]
    pos, neg = pos.strip(), neg.strip()
    try:                                    # a JSON prompt reads better indented
        pos = json.dumps(json.loads(pos), indent=2, ensure_ascii=False)
    except ValueError:
        pass
    prompts = [{"role": "positive", "text": pos}] if pos else []
    if neg:
        prompts.append({"role": "negative", "text": neg})
    pairs = _split_settings(" ".join(lines[at:]))
    settings = {k: v for k, v in pairs if k not in ("Civitai resources", "Model", "Model hash")
                and not k.startswith("urn")}
    models = [{"kind": "checkpoint", "name": v} for k, v in pairs if k == "Model"]
    return {"source": "parameters", "prompts": prompts,
            "settings": [settings] if settings else [], "models": models}


# ------------------------------------------------------------------ ComfyUI summary

_SAMPLER_KEYS = ("seed", "noise_seed", "steps", "cfg", "sampler_name", "scheduler",
                 "denoise")
_MODEL_KEYS = ("ckpt_name", "unet_name", "model_name", "lora_name", "vae_name",
               "clip_name", "clip_name1", "clip_name2")


def _resolve(graph, ref, depth=0):
    """Follow ["<node id>", slot] links to a literal value (seeds are often a link)."""
    if depth > 6 or not (isinstance(ref, list) and len(ref) == 2 and isinstance(ref[0], str)):
        return ref
    node = graph.get(ref[0]) or {}
    ins = node.get("inputs") or {}
    for k in ("seed", "noise_seed", "value", "int", "number", "text", "string"):
        if k in ins:
            return _resolve(graph, ins[k], depth + 1)
    return ref


def _text_of(graph, nid, depth=0):
    """The prompt text a conditioning input ultimately comes from."""
    if depth > 8:
        return []
    node = graph.get(str(nid)) or {}
    ins = node.get("inputs") or {}
    texts = []
    for k in ("text", "text_g", "text_l", "prompt", "string"):
        v = ins.get(k)
        if isinstance(v, str) and v.strip():
            texts.append(v.strip())
        elif isinstance(v, list):
            texts += _text_of(graph, v[0], depth + 1)
    if not texts:                         # a pass-through node: follow conditioning
        for k in ("conditioning", "conditioning_1", "conditioning_to", "positive", "negative"):
            v = ins.get(k)
            if isinstance(v, list) and v:
                texts += _text_of(graph, v[0], depth + 1)
    return texts


def comfy_summary(prompt_json: str):
    """Readable view of a ComfyUI `prompt` graph, or None if it is not one."""
    try:
        graph = json.loads(prompt_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(graph, dict) or not graph or \
            not all(isinstance(n, dict) and "class_type" in n for n in graph.values()):
        return None
    prompts, settings, models, seen = [], [], [], set()
    for nid, node in graph.items():
        ct = node.get("class_type", "")
        ins = node.get("inputs") or {}
        if "sampler" in ct.lower() or any(k in ins for k in ("positive", "negative")):
            for role in ("positive", "negative"):
                ref = ins.get(role)
                if isinstance(ref, list) and ref:
                    for t in _text_of(graph, ref[0]):
                        if (role, t) not in seen:
                            seen.add((role, t))
                            prompts.append({"role": role, "text": t})
            vals = {k: _resolve(graph, ins[k]) for k in _SAMPLER_KEYS if k in ins}
            vals = {k: v for k, v in vals.items() if not isinstance(v, (list, dict))}
            if vals:
                settings.append({"node": ct, **vals})
        for k in _MODEL_KEYS:
            v = ins.get(k)
            if isinstance(v, str) and v:
                entry = {"kind": k.replace("_name", "").replace("ckpt", "checkpoint"), "name": v}
                if "strength_model" in ins:
                    entry["strength"] = ins["strength_model"]
                if entry not in models:
                    models.append(entry)
    # text encoders no sampler pointed at (e.g. a graph whose sampler takes a guider)
    if not prompts:
        for node in graph.values():
            if "textencode" in node.get("class_type", "").lower():
                ins = node.get("inputs") or {}
                for k in ("text", "text_g", "text_l", "prompt"):
                    v = ins.get(k)
                    if isinstance(v, str) and v.strip() and ("prompt", v.strip()) not in seen:
                        seen.add(("prompt", v.strip()))
                        prompts.append({"role": "prompt", "text": v.strip()})
    if not (prompts or settings or models):
        return None
    return {"source": "ComfyUI", "prompts": prompts, "settings": settings, "models": models}


def picture_info(source, name: str, size: int | None = None):
    """`source` is a path or raw bytes. Never decodes pixels."""
    fp = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    with Image.open(fp) as im:
        info = {"name": name, "size": size, "width": im.width, "height": im.height,
                "format": im.format, "mode": im.mode}
        cam = _camera(im)
        if cam:
            info["camera"] = cam
        texts = _texts(im)
    if texts:
        info["texts"] = texts
        raw = {t["key"]: t["value"] for t in texts}
        summ = None
        if "prompt" in raw:
            summ = comfy_summary(raw["prompt"])
        if not summ and "parameters" in raw:
            summ = a1111_summary(raw["parameters"])
        if summ:
            info["summary"] = summ
    return info


# ------------------------------------------------------------------ video

def _fps(rate):
    try:
        n, d = (int(x) for x in str(rate).split("/"))
        return round(n / d, 3) if d else None
    except (ValueError, TypeError):
        return None


def _kbps(v):
    try:
        return round(int(v) / 1000)
    except (TypeError, ValueError):
        return None


def _hdr(s):
    tr = (s.get("color_transfer") or "").lower()
    for sd in s.get("side_data_list") or []:
        if "dovi" in (sd.get("side_data_type") or "").lower() or "dolby" in (sd.get("side_data_type") or "").lower():
            return "Dolby Vision"
    if tr == "smpte2084":
        return "HDR10"
    if tr == "arib-std-b67":
        return "HLG"
    return None


def _iso6709(v):
    """'+34.0522-118.2437+089.000/' (phones) -> {lat, lon}."""
    import re
    m = re.match(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", (v or "").strip())
    if not m:
        return None
    return {"lat": round(float(m.group(1)), 6), "lon": round(float(m.group(2)), 6)}


def video_info(path: str, name: str, size: int | None = None):
    """One demux-only ffprobe (headers, no decoding): container, tracks, chapters, tags."""
    import subprocess
    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_chapters",
           "-of", "json", path]
    try:
        r = subprocess.run(["nice", "-n", "10"] + cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        data = {}
    fmt = data.get("format") or {}
    if not fmt:
        return {"name": name, "size": size, "video": True, "error": "no readable metadata"}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    try:
        dur = float(fmt.get("duration") or 0) or None
    except ValueError:
        dur = None
    out = {"name": name, "size": size, "video": True,
           "container": fmt.get("format_long_name") or fmt.get("format_name"),
           "duration": dur, "bitrate": _kbps(fmt.get("bit_rate")),
           "file_tags": {k: v for k, v in (
               ("Title", tags.get("title")), ("Created", tags.get("creation_time")),
               ("Encoder", tags.get("encoder")), ("Comment", tags.get("comment")),
               ("Make", tags.get("com.apple.quicktime.make") or tags.get("make")),
               ("Model", tags.get("com.apple.quicktime.model") or tags.get("model")),
           ) if v}}
    loc = tags.get("com.apple.quicktime.location.iso6709") or tags.get("location")
    gps = _iso6709(loc) if loc else None
    if gps:
        out["gps"] = gps
    vids, auds, subs = [], [], []
    for s in data.get("streams") or []:
        st = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        common = {"lang": st.get("language") if st.get("language") not in (None, "und") else None,
                  "title": st.get("title"), "default": bool(disp.get("default")),
                  "forced": bool(disp.get("forced"))}
        ct = s.get("codec_type")
        if ct == "video":
            if disp.get("attached_pic"):
                continue                       # cover art, not a picture track
            rot = st.get("rotate")
            for sd in s.get("side_data_list") or []:
                if sd.get("rotation") is not None:
                    rot = sd.get("rotation")
            pf = s.get("pix_fmt") or ""
            depth = s.get("bits_per_raw_sample") or ("10" if "10" in pf else "12" if "12" in pf else "8" if pf else None)
            vids.append({**common, "codec": (s.get("codec_long_name") or s.get("codec_name") or "").split(" / ")[0],
                         "codec_id": s.get("codec_name"), "profile": s.get("profile"),
                         "width": s.get("width"), "height": s.get("height"),
                         "fps": _fps(s.get("avg_frame_rate")) or _fps(s.get("r_frame_rate")),
                         "bitrate": _kbps(s.get("bit_rate") or st.get("bps")),
                         "depth": int(depth) if str(depth or "").isdigit() else None,
                         "hdr": _hdr(s), "scan": s.get("field_order") if s.get("field_order") not in (None, "progressive", "unknown") else None,
                         "dar": s.get("display_aspect_ratio") if s.get("display_aspect_ratio") not in (None, "0:1") else None,
                         "rotate": rot})
        elif ct == "audio":
            auds.append({**common, "codec": s.get("codec_name"), "profile": s.get("profile"),
                         "channels": s.get("channels"), "layout": s.get("channel_layout"),
                         "rate": int(s["sample_rate"]) if str(s.get("sample_rate") or "").isdigit() else None,
                         "bitrate": _kbps(s.get("bit_rate") or st.get("bps"))})
        elif ct == "subtitle":
            subs.append({**common, "codec": s.get("codec_name")})
    out.update(video_tracks=vids, audio_tracks=auds, sub_tracks=subs)
    chaps = []
    for c in data.get("chapters") or []:
        try:
            start = float(c.get("start_time") or 0)
        except ValueError:
            continue
        chaps.append({"start": start, "title": (c.get("tags") or {}).get("title") or ""})
    if chaps:
        out["chapters"] = chaps
    return out
