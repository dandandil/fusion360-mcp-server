"""Sketch description: curves, constraints, dimensions, profiles.

What an agent otherwise rewrites by hand in execute_code for every sketch.
Curves and points get ids (``c3``, ``p7``) that constraints and dimensions
refer to.  Coordinates are sketch space, cm.  Every read is guarded: one
odd entity (elliptical arc, dimension without a parameter) must not lose
the rest.  Pure attribute access, no ``adsk`` import.
"""

import logging

from .model_graph import _has, _items, short_type

log = logging.getLogger("fusion360mcp.sketch")

# Constraint / dimension properties that hold sketch entities (API stubs)
ENTITY_ATTRS = (
    "entity",
    "entityOne",
    "entityTwo",
    "line",
    "lineOne",
    "lineTwo",
    "lines",
    "curveOne",
    "curveTwo",
    "circleOne",
    "circleTwo",
    "ellipse",
    "midPointCurve",
    "symmetryLine",
    "point",
    "pointOne",
    "pointTwo",
    "parentCurves",
    "childCurves",
)


def _xyz(point) -> list:
    return [round(point.x, 5), round(point.y, 5), round(point.z, 5)]


def _read(obj, attr):
    """obj.attr, or None when absent or unreadable."""
    if not _has(obj, attr):
        return None
    try:
        return getattr(obj, attr)
    except Exception as exc:
        log.debug("%s.%s not readable: %s", short_type(obj), attr, exc)
        return None


def _curve(curve, ids) -> dict:
    out = {"id": ids[curve.entityToken], "type": short_type(curve)}
    for flag in ("isConstruction", "isReference", "isFixed", "isFullyConstrained"):
        value = _read(curve, flag)
        if value:
            out[flag] = True
    for attr, key in (
        ("startSketchPoint", "start"),
        ("endSketchPoint", "end"),
        ("centerSketchPoint", "center"),
    ):
        point = _read(curve, attr)
        if point is not None:
            out[key] = _xyz(point.geometry)
    for attr in ("radius", "majorAxisRadius", "minorAxisRadius", "length"):
        value = _read(curve, attr)
        if isinstance(value, (int, float)):
            out[attr] = round(value, 5)
    fit_points = _read(curve, "fitPoints")
    if fit_points is not None:
        out["fit_points"] = fit_points.count
    if out.get("isReference"):
        referenced = _read(curve, "referencedEntity")
        if referenced is not None:
            out["references"] = short_type(referenced)
    return out


def _refs(obj, ids) -> list:
    refs = []
    for attr in ENTITY_ATTRS:
        value = _read(obj, attr)
        if value is None:
            continue
        for entity in _items(value) or [value]:
            token = _read(entity, "entityToken")
            refs.append(ids.get(token, short_type(entity)))
    return refs


def _dimension(dim, ids) -> dict:
    out = {"type": short_type(dim), "entities": _refs(dim, ids)}
    driving = _read(dim, "isDriving")
    if driving is False:
        out["driven"] = True
    parameter = _read(dim, "parameter")
    if parameter is not None:
        out["parameter"] = parameter.name
        out["expression"] = parameter.expression
        out["value"] = round(parameter.value, 6)
    return out


def _profile(index, profile, accuracy) -> dict:
    out = {"index": index}
    loops = _items(_read(profile, "profileLoops")) or []
    out["loops"] = len(loops)
    out["inner_loops"] = sum(1 for loop in loops if not _read(loop, "isOuter"))
    try:
        props = profile.areaProperties(accuracy)
        out["area"] = round(props.area, 6)
        out["centroid"] = _xyz(props.centroid)
    except Exception as exc:
        out["area_error"] = str(exc)
    box = _read(profile, "boundingBox")
    if box is not None:
        out["box"] = [_xyz(box.minPoint), _xyz(box.maxPoint)]
    return out


def describe_sketch(sketch, accuracy, limit: int = 200) -> dict:
    """Curves, points in use, constraints, dimensions and profiles."""
    curves = _items(sketch.sketchCurves) or []
    points = _items(sketch.sketchPoints) or []
    ids = {}
    for i, curve in enumerate(curves):
        ids[curve.entityToken] = f"c{i}"
    for i, point in enumerate(points):
        ids.setdefault(point.entityToken, f"p{i}")

    constraints = [
        {"type": short_type(c), "entities": _refs(c, ids)}
        for c in _items(sketch.geometricConstraints) or []
    ]
    dimensions = [_dimension(d, ids) for d in _items(sketch.sketchDimensions) or []]
    used = {ref for item in constraints + dimensions for ref in item["entities"]}
    profiles = [
        _profile(i, p, accuracy) for i, p in enumerate(_items(sketch.profiles) or [])
    ]

    plane = _read(sketch, "referencePlane")
    return {
        "name": sketch.name,
        "plane": short_type(plane) if plane is not None else "unreadable",
        "fully_constrained": _read(sketch, "isFullyConstrained"),
        "counts": {
            "curves": len(curves),
            "points": len(points),
            "constraints": len(constraints),
            "dimensions": len(dimensions),
            "profiles": len(profiles),
        },
        "curves": [_curve(c, ids) for c in curves[:limit]],
        "points": [
            {"id": ids[p.entityToken], "at": _xyz(p.geometry)}
            for p in points
            if ids[p.entityToken] in used
        ][:limit],
        "constraints": constraints[:limit],
        "dimensions": dimensions[:limit],
        "profiles": profiles[:limit],
        "truncated": max(len(curves), len(constraints), len(dimensions)) > limit,
    }
