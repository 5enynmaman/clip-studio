"""Pure camera keyframe evaluation and FFmpeg expression generation."""
from __future__ import annotations

import math
from typing import Any


def _value(key: Any, name: str, default=None):
    return key.get(name, default) if isinstance(key, dict) else getattr(key, name, default)


def _keys(clip):
    keys = list(_value(clip, "camera", []) or [])
    if not keys:
        return [{"time": 0.0, "x": _value(clip, "crop_x", .5), "y": .5, "zoom": 1.0, "transition": "cut"}]
    if float(_value(keys[0], "time", 0)) > 0:
        keys.insert(0, {"time": 0.0, "x": _value(clip, "crop_x", .5), "y": .5, "zoom": 1.0, "transition": "cut"})
    return keys


def at(clip, time: float) -> dict[str, float]:
    """Evaluate camera state at clip-local seconds; key transitions belong to the incoming key."""
    keys = _keys(clip)
    t = max(0.0, float(time))
    previous = keys[0]
    for current in keys[1:]:
        start = float(_value(previous, "time"))
        end = float(_value(current, "time"))
        if t < end:
            if _value(current, "transition", "cut") == "linear":
                f = max(0.0, min(1.0, (t - start) / (end - start)))
                return {name: float(_value(previous, name)) + (float(_value(current, name)) - float(_value(previous, name))) * f
                        for name in ("x", "y", "zoom")}
            return {name: float(_value(previous, name)) for name in ("x", "y", "zoom")}
        previous = current
    return {name: float(_value(previous, name)) for name in ("x", "y", "zoom")}


def expressions(clip, time_offset: float = 0, time_variable: str = "t") -> dict[str, str]:
    """FFmpeg expressions using the requested timestamp variable."""
    keys = _keys(clip)
    time_expr = f"({time_variable}+{_number(time_offset)})" if time_offset else time_variable
    result = {}
    for name in ("x", "y", "zoom"):
        expr = _number(_value(keys[-1], name))
        for index in range(len(keys) - 1, 0, -1):
            previous, current = keys[index - 1], keys[index]
            start, end = float(_value(previous, "time")), float(_value(current, "time"))
            old, new = float(_value(previous, name)), float(_value(current, name))
            if _value(current, "transition", "cut") == "linear":
                value = f"({_number(old)}+({_number(new)}-{_number(old)})*({time_expr}-{_number(start)})/{_number(end-start)})"
            else:
                value = _number(old)
            threshold = end - 1e-8 if _value(current, "transition", "cut") == "cut" else end
            expr = f"if(lt({time_expr},{_number(threshold)}),{value},{expr})"
        result[name] = expr
    return result


def zoompan(clip, source_width: int, source_height: int, output_width: int, output_height: int, fit: str, background: str) -> str | None:
    """Build a fixed-output-size zoompan filter while keeping the full source available."""
    aspect_num, aspect_den = output_width // math.gcd(output_width, output_height), output_height // math.gcd(output_width, output_height)
    units = math.ceil(max(source_width / aspect_num, source_height / aspect_den))
    canvas_width, canvas_height = aspect_num * units, aspect_den * units
    pad_left, pad_top = (canvas_width - source_width) / 2, (canvas_height - source_height) / 2
    expressions_ = expressions(clip, time_variable="in_time")
    if fit == "cover":
        base_scale = max(output_width / source_width, output_height / source_height)
        crop_width, crop_height = output_width / base_scale, output_height / base_scale
        base_zoom = canvas_width / crop_width
    else:
        crop_width, crop_height = float(canvas_width), float(canvas_height)
        base_zoom = 1.0
    max_zoom = max(float(_value(key, "zoom", 1)) for key in _keys(clip))
    # zoompan caps its zoom expression at 10. Extreme custom aspect ratios also
    # create huge padded frames; keep those cases on the existing scale/crop path.
    if canvas_width * canvas_height > 12_000_000 or base_zoom * max_zoom > 10:
        return None
    if fit == "cover":
        x = f"{_number(pad_left)}+({source_width}-{_number(crop_width)}/({expressions_['zoom']}))*({expressions_['x']})"
        y = f"{_number(pad_top)}+({source_height}-{_number(crop_height)}/({expressions_['zoom']}))*({expressions_['y']})"
    else:
        x = f"({canvas_width}-{canvas_width}/({expressions_['zoom']}))*({expressions_['x']})"
        y = f"({canvas_height}-{canvas_height}/({expressions_['zoom']}))*({expressions_['y']})"
    return (f"pad={canvas_width}:{canvas_height}:(ow-iw)/2:(oh-ih)/2:color={background},"
            f"zoompan=z='{_number(base_zoom)}*({expressions_['zoom']})':x='{x}':y='{y}':d=1:s={output_width}x{output_height}:fps=30")


def _number(value) -> str:
    return format(float(value), ".12g")
