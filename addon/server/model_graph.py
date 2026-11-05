"""
Model graph — timeline listing and feature dependencies.

The API has no dependency graph, so it is rebuilt from each timeline
entity's inputs: features expose them as properties (profile, edgeSets,
inputEntities, ...), sketches through their plane and projected geometry.
Faces and edges are attributed to the feature that created their faces.

Inputs are read with the timeline at its current marker, so faces and edges
come from the current model state: the graph is an approximation, good for
"what may break", not a proof.

Pure attribute access, no ``adsk`` import, so it is testable with fakes.
"""

import logging

log = logging.getLogger("fusion360mcp.graph")

# Feature properties that hold inputs (from the adsk.fusion API stubs)
FEATURE_INPUT_ATTRS = (
    "profile",
    "path",
    "guideRail",
    "axis",
    "participantBodies",
    "edgeSets",
    "edges",
    "inputEntities",
    "inputFaces",
    "inputCylindricalFaces",
    "loftSections",
    "mirrorPlane",
    "directionEntity",
    "directionOneEntity",
    "directionTwoEntity",
    "targetBody",
    "toolBodies",
    "splittingTool",
    "facesToSplit",
    "trimTool",
    "targetFaces",
    "stitchSurfaces",
    "surfaces",
    "entities",
    "deletedFaces",
    "boundaryCurve",
    "plane",
    "point",
    "tools",
    "sourceBody",
)
# Construction geometry definition properties that hold inputs
CONSTRUCTION_INPUT_ATTRS = (
    "planarEntity",
    "linearEntity",
    "planeOne",
    "planeTwo",
    "pathEntity",
    "edgeOne",
    "edgeTwo",
    "pointOne",
    "pointTwo",
    "pointThree",
    "tangentFace",
    "pointEntity",
    "circularFace",
    "cylinderFace",
)
# Wrappers whose single property holds the actual input
_NESTED_ATTR = {
    "PathEntity": "entity",
    "LoftSection": "entity",
}

HEALTH_STATES = {
    0: "healthy",
    1: "warning",
    2: "error",
    3: "suppressed",
    4: "rolled_back",
    5: "unknown",
}


def short_type(obj) -> str:
    """'adsk::fusion::ExtrudeFeature' -> 'ExtrudeFeature'."""
    return obj.objectType.rsplit("::", 1)[-1]


def _has(obj, attr: str) -> bool:
    """Whether *obj* has property *attr*, without reading it (reads may raise)."""
    return hasattr(type(obj), attr) or attr in getattr(obj, "__dict__", {})


def _items(obj):
    """Elements of an API collection or list, else None."""
    if isinstance(obj, (list, tuple)):
        return list(obj)
    if hasattr(obj, "count") and hasattr(obj, "item"):
        return [obj.item(i) for i in range(obj.count)]
    return None


# ----------------------------------------------------------------------
# Timeline
# ----------------------------------------------------------------------


def timeline_entry(tl_obj) -> dict:
    entry = {
        "index": tl_obj.index,
        "name": tl_obj.name,
        "type": "Group" if tl_obj.isGroup else _entity_type(tl_obj),
        "suppressed": tl_obj.isSuppressed,
        "rolled_back": tl_obj.isRolledBack,
    }
    # healthState raises for some object types (Autodesk forum, confirmed bug)
    try:
        entry["health"] = HEALTH_STATES.get(tl_obj.healthState, "unknown")
        message = tl_obj.errorOrWarningMessage
    except Exception as exc:
        entry["health"] = "unknown"
        message = f"health not readable: {exc}"
    if message:
        entry["message"] = message
    group = tl_obj.parentGroup
    if group is not None:
        entry["group"] = group.name
    return entry


def _entity_type(tl_obj) -> str:
    entity = tl_obj.entity
    return short_type(entity) if entity is not None else "?"


def list_timeline(
    timeline,
    start=0,
    limit=100,
    problems_only=False,
    name_contains=None,
    types=None,
) -> dict:
    entries = []
    for i in range(timeline.count):
        entry = timeline_entry(timeline.item(i))
        if problems_only and entry["health"] not in ("warning", "error"):
            continue
        if name_contains and name_contains.lower() not in entry["name"].lower():
            continue
        if types and entry["type"] not in types:
            continue
        entries.append(entry)
    return {
        "count": timeline.count,
        "marker_position": timeline.markerPosition,
        "total_matching": len(entries),
        "items": entries[start:] if limit is None else entries[start : start + limit],
    }


def find_timeline_object(timeline, name: str):
    for i in range(timeline.count):
        tl_obj = timeline.item(i)
        if tl_obj.name == name:
            return tl_obj
    raise RuntimeError(f"Timeline item '{name}' not found")


def features_near(timeline, point, radius: float) -> list[dict]:
    """Timeline items with geometry within *radius* (cm) of *point*.

    Distance is to the bounding boxes of the faces an item created (sketches:
    the sketch box), so a large face close to the point counts as near.
    """
    found = []
    for i in range(timeline.count):
        tl_obj = timeline.item(i)
        if tl_obj.isGroup or tl_obj.isRolledBack or tl_obj.isSuppressed:
            continue
        entity = tl_obj.entity
        if entity is None:
            continue
        try:
            if short_type(entity) == "Sketch":
                boxes = [entity.boundingBox]
            elif _has(entity, "faces"):
                boxes = [f.boundingBox for f in _items(entity.faces)]
            else:
                continue
        except Exception as exc:
            log.debug("geometry of %s not readable: %s", tl_obj.name, exc)
            continue
        distances = [_box_distance(box, point) for box in boxes]
        near = [d for d in distances if d <= radius]
        if near:
            found.append(
                {
                    "name": tl_obj.name,
                    "index": tl_obj.index,
                    "type": short_type(entity),
                    "distance": round(min(near), 4),
                    "faces_near": len(near),
                }
            )
    return sorted(found, key=lambda f: (f["distance"], f["index"]))


def _box_distance(box, point) -> float:
    lo = (box.minPoint.x, box.minPoint.y, box.minPoint.z)
    hi = (box.maxPoint.x, box.maxPoint.y, box.maxPoint.z)
    gaps = [max(lo[i] - point[i], 0.0, point[i] - hi[i]) for i in range(3)]
    return sum(g * g for g in gaps) ** 0.5


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------


def face_creators(timeline) -> dict:
    """entityToken of each face -> name of the feature that created it."""
    creators = {}
    for i in range(timeline.count):
        tl_obj = timeline.item(i)
        if tl_obj.isGroup or tl_obj.isRolledBack or tl_obj.isSuppressed:
            continue
        entity = tl_obj.entity
        if entity is None or not _has(entity, "faces"):
            continue
        try:
            for face in _items(entity.faces) or []:
                creators[face.entityToken] = tl_obj.name
        except Exception as exc:  # broken features expose no faces
            log.debug("faces of %s not readable: %s", tl_obj.name, exc)
    return creators


def describe_ref(obj, creators: dict) -> list[dict]:
    """Reduce an input object to graph nodes."""
    elements = _items(obj)
    if elements is not None:
        return [ref for e in elements for ref in describe_ref(e, creators)]
    kind = short_type(obj)
    if kind in _NESTED_ATTR:
        return describe_ref(getattr(obj, _NESTED_ATTR[kind]), creators)
    if kind.endswith("EdgeSet"):
        return describe_ref(obj.edges, creators) if _has(obj, "edges") else []
    if kind == "Profile":
        return [{"kind": "sketch", "name": obj.parentSketch.name}]
    if kind == "Sketch":
        return [{"kind": "sketch", "name": obj.name}]
    if _has(obj, "parentSketch"):  # sketch curves, points, texts
        return [{"kind": "sketch", "name": obj.parentSketch.name}]
    if kind == "BRepBody":
        return [{"kind": "body", "name": obj.name}]
    if kind == "BRepFace":
        ref = _topology_ref("face", obj.body.name, [obj], creators)
        return [_with_geometry(ref, obj)]
    if kind == "BRepEdge":
        ref = _topology_ref("edge", obj.body.name, _items(obj.faces), creators)
        return [_with_geometry(ref, obj)]
    if kind == "BRepVertex":
        return [{"kind": "vertex", "body": obj.body.name}]
    if kind.startswith("Construction"):
        return [{"kind": "construction", "name": obj.name}]
    if kind.endswith("Feature"):
        return [{"kind": "feature", "name": obj.name}]
    return [{"kind": kind}]


def _topology_ref(kind, body_name, faces, creators) -> dict:
    features = sorted(
        {creators[f.entityToken] for f in faces if f.entityToken in creators}
    )
    return {"kind": kind, "body": body_name, "created_by": features}


def _with_geometry(ref: dict, obj) -> dict:
    """Add where the face/edge is (cm) and its area/length, for matching."""
    at, size = geometry_of(obj)
    if at is not None:
        ref["at"] = at
        ref["size"] = size
    return ref


def geometry_of(obj):
    """(centroid of a face / bbox centre of an edge, area / length)."""
    try:
        if short_type(obj) == "BRepFace":
            c = obj.centroid
            return [round(c.x, 4), round(c.y, 4), round(c.z, 4)], round(obj.area, 6)
        box = obj.boundingBox
        at = [
            round((lo + hi) / 2, 4)
            for lo, hi in zip(
                (box.minPoint.x, box.minPoint.y, box.minPoint.z),
                (box.maxPoint.x, box.maxPoint.y, box.maxPoint.z),
            )
        ]
        return at, round(obj.length, 6)
    except Exception as exc:
        log.debug("geometry of %s not readable: %s", short_type(obj), exc)
        return None, None


def entity_inputs(entity, creators: dict) -> tuple[dict, dict]:
    """(inputs by property, unreadable properties with errors)."""
    kind = short_type(entity)
    if kind == "Sketch":
        sources = [("referencePlane", lambda: entity.referencePlane)]
        sources.append(("projected", lambda: _projected(entity)))
    elif kind.startswith("Construction"):
        try:
            definition = entity.definition
        except Exception as exc:
            return {}, {"definition": f"{type(exc).__name__}: {exc}"}
        sources = [
            (a, lambda a=a: getattr(definition, a))
            for a in CONSTRUCTION_INPUT_ATTRS
            if _has(definition, a)
        ]
    else:
        sources = [
            (a, lambda a=a: getattr(entity, a))
            for a in FEATURE_INPUT_ATTRS
            if _has(entity, a)
        ]

    inputs, unreadable = {}, {}
    for attr, read in sources:
        try:
            value = read()
            if value is None:
                continue
            refs = describe_ref(value, creators)
        except Exception as exc:
            unreadable[attr] = f"{type(exc).__name__}: {exc}"
            continue
        if refs:
            inputs[attr] = _unique(refs)
    return inputs, unreadable


def output_bodies(entity) -> tuple[list[str], str | None]:
    """(names of bodies the item created or modified, read error)."""
    if not _has(entity, "bodies"):
        return [], None
    try:
        return [b.name for b in _items(entity.bodies)], None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _projected(sketch) -> list:
    """Model entities that projected / included sketch geometry refers to."""
    refs = []
    for collection in (sketch.sketchCurves, sketch.sketchPoints):
        for item in _items(collection):
            if item.isReference and item.referencedEntity is not None:
                refs.append(item.referencedEntity)
    return refs


def _unique(refs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for ref in refs:
        key = repr(sorted(ref.items()))
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out


# ----------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------


def ref_targets(ref: dict) -> tuple[set[str], set[str]]:
    """(timeline item names, body names) an input ref points at."""
    if ref["kind"] in ("sketch", "construction", "feature"):
        return {ref["name"]}, set()
    if ref["kind"] == "body":
        return set(), {ref["name"]}
    if ref["kind"] in ("face", "edge"):
        return set(ref["created_by"]), {ref["body"]}
    return set(), set()


def build_graph(timeline) -> dict:
    """name -> index, referenced items, input bodies, output bodies."""
    creators = face_creators(timeline)
    graph = {}
    for i in range(timeline.count):
        tl_obj = timeline.item(i)
        entity = None if tl_obj.isGroup else tl_obj.entity
        if entity is None:
            continue
        inputs, _ = entity_inputs(entity, creators)
        refs, bodies_in = set(), set()
        for ref_list in inputs.values():
            for ref in ref_list:
                names, bodies = ref_targets(ref)
                refs |= names
                bodies_in |= bodies
        refs.discard(tl_obj.name)
        bodies_out, _ = output_bodies(entity)
        graph[tl_obj.name] = {
            "index": tl_obj.index,
            "refs": refs,
            "bodies_in": bodies_in,
            "bodies_out": set(bodies_out),
        }
    return graph


def dependents(graph, name, transitive=False, include_bodies=False) -> list[dict]:
    """Later timeline items that use *name* as an input.

    Precise links are sketches, features and the features that created
    referenced faces/edges.  With *include_bodies*, using a body that *name*
    outputs counts too — in a big single-body model that is nearly everything.
    """
    if name not in graph:
        raise RuntimeError(f"Timeline item '{name}' not found")
    found, frontier = {}, [name]
    while frontier:
        current_name = frontier.pop()
        current = graph[current_name]
        for other, node in graph.items():
            if other in found or other == name or node["index"] <= current["index"]:
                continue
            via = sorted({current_name} & node["refs"])
            if include_bodies:
                via += [
                    f"body:{b}"
                    for b in sorted(current["bodies_out"] & node["bodies_in"])
                ]
            if via:
                found[other] = {"name": other, "index": node["index"], "via": via}
                if transitive:
                    frontier.append(other)
    return sorted(found.values(), key=lambda d: d["index"])


# ----------------------------------------------------------------------
# Reference check: which face/edge inputs were lost after an edit
# ----------------------------------------------------------------------

TOPOLOGY_KINDS = ("face", "edge")
MATCH_TOLERANCE_CM = 1e-3
MAX_CANDIDATES = 3


def reference_snapshot(timeline) -> dict:
    """{item: {property: [face/edge refs with geometry]}} of healthy items."""
    creators = face_creators(timeline)
    snapshot = {}
    for i in range(timeline.count):
        tl_obj = timeline.item(i)
        if tl_obj.isGroup or tl_obj.isRolledBack or tl_obj.isSuppressed:
            continue
        if timeline_entry(tl_obj)["health"] == "error" or tl_obj.entity is None:
            continue
        inputs, _ = entity_inputs(tl_obj.entity, creators)
        topology = {
            attr: [r for r in refs if r["kind"] in TOPOLOGY_KINDS and "at" in r]
            for attr, refs in inputs.items()
        }
        topology = {attr: refs for attr, refs in topology.items() if refs}
        if topology:
            snapshot[tl_obj.name] = topology
    return snapshot


def check_references(timeline, snapshot: dict, find_body) -> list[dict]:
    """Items whose snapshotted face/edge inputs are gone, with candidates.

    *find_body(name)* returns the body to search candidates in, or None.
    """
    creators = face_creators(timeline)
    report = []
    for name, old_inputs in snapshot.items():
        try:
            tl_obj = find_timeline_object(timeline, name)
        except RuntimeError:
            report.append({"name": name, "missing": True})
            continue
        entry = timeline_entry(tl_obj)
        inputs, unreadable = entity_inputs(tl_obj.entity, creators)
        lost = {}
        for attr, old_refs in old_inputs.items():
            current = inputs.get(attr, [])
            gone = [r for r in old_refs if not any(_same(r, c) for c in current)]
            if gone:
                lost[attr] = [
                    {"was": r, "candidates": _candidates(r, find_body)} for r in gone
                ]
        if lost or entry["health"] == "error":
            item = {"name": name, "health": entry["health"], "lost": lost}
            if "message" in entry:
                item["message"] = entry["message"]
            if unreadable:
                item["unreadable"] = unreadable
            report.append(item)
    return report


def _same(old: dict, new: dict) -> bool:
    return (
        old["kind"] == new["kind"]
        and old["body"] == new["body"]
        and "at" in new
        and _distance(old["at"], new["at"]) <= MATCH_TOLERANCE_CM
    )


def _distance(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _candidates(ref: dict, find_body) -> list[dict]:
    """Nearest faces/edges of the same body now, by location then size."""
    body = find_body(ref["body"])
    if body is None:
        return []
    entities = body.faces if ref["kind"] == "face" else body.edges
    scored = []
    for index, entity in enumerate(_items(entities)):
        at, size = geometry_of(entity)
        if at is None:
            continue
        scored.append(
            {
                "index": index,
                "at": at,
                "size": size,
                "distance": round(_distance(ref["at"], at), 4),
            }
        )
    scored.sort(key=lambda c: (c["distance"], abs(c["size"] - ref["size"])))
    return scored[:MAX_CANDIDATES]
