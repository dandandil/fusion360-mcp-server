"""model_graph against a fake timeline.

Model: Sketch1 → Extrude1 (Body1, faces f1..f3); Sketch2 sits on f2 and
projects the f1/f2 edge; Fillet1 rounds that edge; Extrude2 extrudes
Sketch2 into Body1; Broken1 cannot read its profile.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "_model_graph", REPO_ROOT / "addon" / "server" / "model_graph.py"
)
graph = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(graph)


class Obj:
    def __init__(self, object_type, **attrs):
        self.objectType = f"adsk::fusion::{object_type}"
        self.__dict__.update(attrs)


class Collection:
    def __init__(self, items):
        self._items = list(items)
        self.count = len(self._items)

    def item(self, i):
        return self._items[i]


class BrokenFeature(Obj):
    @property
    def profile(self):
        raise RuntimeError("profile lost")


class TimelineObject:
    def __init__(self, index, name, entity, health=0, message="", raises=False):
        self.index = index
        self.name = name
        self.entity = entity
        self.isGroup = False
        self.isSuppressed = False
        self.isRolledBack = False
        self.parentGroup = None
        self._health = health
        self._message = message
        self._raises = raises

    @property
    def healthState(self):
        if self._raises:
            raise RuntimeError("not supported")
        return self._health

    @property
    def errorOrWarningMessage(self):
        return self._message


def _model():
    body = Obj("BRepBody", name="Body1")
    f1, f2, f3 = (Obj("BRepFace", entityToken=f"f{i}", body=body) for i in (1, 2, 3))
    edge = Obj("BRepEdge", body=body, faces=Collection([f1, f2]))
    xy = Obj("ConstructionPlane", name="XY")

    sketch1 = Obj("Sketch", name="Sketch1", referencePlane=xy)
    sketch1.sketchCurves = Collection([])
    sketch1.sketchPoints = Collection([])
    extrude1 = Obj(
        "ExtrudeFeature",
        name="Extrude1",
        profile=Obj("Profile", parentSketch=sketch1),
        participantBodies=[],
        bodies=Collection([body]),
        faces=Collection([f1, f2, f3]),
    )
    sketch2 = Obj("Sketch", name="Sketch2", referencePlane=f2)
    projected = Obj("SketchLine", isReference=True, referencedEntity=edge)
    projected.parentSketch = sketch2
    sketch2.sketchCurves = Collection([projected])
    sketch2.sketchPoints = Collection([])
    fillet = Obj(
        "FilletFeature",
        name="Fillet1",
        edgeSets=Collection(
            [Obj("ConstantRadiusFilletEdgeSet", edges=Collection([edge]))]
        ),
        bodies=Collection([body]),
        faces=Collection([]),
    )
    extrude2 = Obj(
        "ExtrudeFeature",
        name="Extrude2",
        profile=Obj("Profile", parentSketch=sketch2),
        participantBodies=[body],
        bodies=Collection([body]),
        faces=Collection([]),
    )
    broken = BrokenFeature("ExtrudeFeature", name="Broken1", faces=Collection([]))

    items = [
        TimelineObject(0, "Sketch1", sketch1),
        TimelineObject(1, "Extrude1", extrude1),
        TimelineObject(2, "Sketch2", sketch2, raises=True),
        TimelineObject(3, "Fillet1", fillet),
        TimelineObject(4, "Extrude2", extrude2, health=1, message="tangent"),
        TimelineObject(5, "Broken1", broken, health=2, message="profile lost"),
    ]
    timeline = Collection(items)
    timeline.markerPosition = len(items)
    return timeline


@pytest.fixture
def timeline():
    return _model()


def test_list_timeline_reports_health_and_problems(timeline):
    result = graph.list_timeline(timeline)
    assert result["count"] == 6
    assert result["items"][1]["type"] == "ExtrudeFeature"
    assert result["items"][2]["health"] == "unknown"  # healthState raised
    problems = graph.list_timeline(timeline, problems_only=True)["items"]
    assert [i["name"] for i in problems] == ["Extrude2", "Broken1"]
    assert problems[0]["message"] == "tangent"


def test_list_timeline_paginates(timeline):
    result = graph.list_timeline(timeline, start=2, limit=2)
    assert [i["name"] for i in result["items"]] == ["Sketch2", "Fillet1"]
    assert result["total_matching"] == 6


def test_feature_inputs_resolve_to_nodes(timeline):
    creators = graph.face_creators(timeline)
    extrude2 = graph.find_timeline_object(timeline, "Extrude2").entity
    inputs, unreadable = graph.entity_inputs(extrude2, creators)
    assert inputs["profile"] == [{"kind": "sketch", "name": "Sketch2"}]
    assert inputs["participantBodies"] == [{"kind": "body", "name": "Body1"}]
    assert unreadable == {}

    fillet = graph.find_timeline_object(timeline, "Fillet1").entity
    inputs, _ = graph.entity_inputs(fillet, creators)
    assert inputs["edgeSets"] == [
        {"kind": "edge", "body": "Body1", "created_by": ["Extrude1"]}
    ]


def test_sketch_inputs_include_plane_face_and_projections(timeline):
    creators = graph.face_creators(timeline)
    sketch2 = graph.find_timeline_object(timeline, "Sketch2").entity
    inputs, _ = graph.entity_inputs(sketch2, creators)
    assert inputs["referencePlane"] == [
        {"kind": "face", "body": "Body1", "created_by": ["Extrude1"]}
    ]
    assert inputs["projected"][0]["created_by"] == ["Extrude1"]


def test_unreadable_input_is_reported(timeline):
    broken = graph.find_timeline_object(timeline, "Broken1").entity
    inputs, unreadable = graph.entity_inputs(broken, {})
    assert "profile" not in inputs
    assert "profile lost" in unreadable["profile"]


def test_direct_dependents(timeline):
    deps = graph.dependents(graph.build_graph(timeline), "Extrude1")
    assert [(d["name"], d["via"]) for d in deps] == [
        ("Sketch2", ["Extrude1"]),
        ("Fillet1", ["Extrude1"]),
    ]


def test_transitive_dependents(timeline):
    deps = graph.dependents(graph.build_graph(timeline), "Extrude1", transitive=True)
    assert [d["name"] for d in deps] == ["Sketch2", "Fillet1", "Extrude2"]
    assert deps[2]["via"] == ["Sketch2"]


def test_body_links_are_opt_in(timeline):
    g = graph.build_graph(timeline)
    direct = graph.dependents(g, "Fillet1")
    assert direct == []
    with_bodies = graph.dependents(g, "Fillet1", include_bodies=True)
    assert with_bodies == [{"name": "Extrude2", "index": 4, "via": ["body:Body1"]}]


def test_unknown_item_raises(timeline):
    with pytest.raises(RuntimeError, match="not found"):
        graph.dependents(graph.build_graph(timeline), "Nope")


# ── reference snapshot / check ───────────────────────────────────────


def _point(x, y, z):
    return Obj("Point3D", x=x, y=y, z=z)


def _edge(body, faces, at, length):
    box = Obj("BoundingBox3D", minPoint=_point(*at), maxPoint=_point(*at))
    return Obj("BRepEdge", body=body, faces=faces, boundingBox=box, length=length)


def _face(body, token, at):
    return Obj("BRepFace", entityToken=token, body=body, centroid=_point(*at), area=4.0)


def _filleted_model():
    """Extrude1 makes f1/f2; Fillet1 rounds their shared edge at z=1."""
    body = Obj("BRepBody", name="Body1")
    f1 = _face(body, "f1", (0, 0, 1))
    f2 = _face(body, "f2", (1, 0, 0))
    edge = _edge(body, Collection([f1, f2]), (1, 0, 1), 2.0)
    body.faces = Collection([f1, f2])
    body.edges = Collection([_edge(body, Collection([]), (1, 0, 1.2), 2.0), edge])
    extrude = Obj(
        "ExtrudeFeature",
        name="Extrude1",
        bodies=Collection([body]),
        faces=Collection([f1, f2]),
    )
    edge_set = Obj("ConstantRadiusFilletEdgeSet", edges=Collection([edge]))
    fillet = Obj(
        "FilletFeature",
        name="Fillet1",
        edgeSets=Collection([edge_set]),
        bodies=Collection([body]),
        faces=Collection([]),
    )
    items = [
        TimelineObject(0, "Extrude1", extrude),
        TimelineObject(1, "Fillet1", fillet),
    ]
    timeline = Collection(items)
    timeline.markerPosition = 2
    return timeline, body, edge_set


def _snapshot(timeline):
    snapshot, _ = graph.reference_snapshot(timeline, graph.face_creators(timeline))
    return snapshot


def _check(timeline, snapshot, body):
    creators = graph.face_creators(timeline)
    return graph.check_references(timeline, snapshot, lambda n: body, creators)


def test_refs_carry_geometry():
    timeline, _, _ = _filleted_model()
    snapshot = _snapshot(timeline)
    ref = snapshot["Fillet1"]["edgeSets"][0]
    assert ref["at"] == [1, 0, 1]
    assert ref["size"] == 2.0
    assert ref["created_by"] == ["Extrude1"]


def test_unchanged_model_has_no_lost_references():
    timeline, body, _ = _filleted_model()
    snapshot = _snapshot(timeline)
    assert _check(timeline, snapshot, body) == []


def test_lost_edge_reports_nearest_candidates():
    timeline, body, edge_set = _filleted_model()
    snapshot = _snapshot(timeline)
    edge_set.edges = Collection([])  # recompute dropped the edge
    timeline.item(1)._health = 2
    timeline.item(1)._message = "edge lost"

    report = _check(timeline, snapshot, body)
    assert len(report) == 1
    item = report[0]
    assert (item["name"], item["health"], item["message"]) == (
        "Fillet1",
        "error",
        "edge lost",
    )
    lost = item["lost"]["edgeSets"][0]
    assert lost["was"]["at"] == [1, 0, 1]
    assert [c["index"] for c in lost["candidates"]] == [1, 0]
    assert lost["candidates"][1]["distance"] == 0.2


def test_failing_item_without_snapshot_refs_is_reported():
    timeline, body, _ = _filleted_model()
    snapshot = _snapshot(timeline)
    timeline.item(1)._health = 2
    report = _check(timeline, snapshot, body)
    assert report == [{"name": "Fillet1", "health": "error", "lost": {}}]


# ── focus helpers ────────────────────────────────────────────────────


def _box(lo, hi):
    return Obj("BoundingBox3D", minPoint=_point(*lo), maxPoint=_point(*hi))


def test_features_near_uses_face_boxes():
    big_face = Obj("BRepFace", boundingBox=_box((-10, -10, 0), (10, 10, 0)))
    far_face = Obj("BRepFace", boundingBox=_box((50, 50, 50), (51, 51, 51)))
    plate = Obj("ExtrudeFeature", faces=Collection([big_face]))
    boss = Obj("ExtrudeFeature", faces=Collection([far_face]))
    sketch = Obj("Sketch", boundingBox=_box((0, 0, 2), (1, 1, 2)))
    timeline = Collection(
        [
            TimelineObject(0, "Plate", plate),
            TimelineObject(1, "Boss", boss),
            TimelineObject(2, "Sketch3", sketch),
        ]
    )
    found = graph.features_near(timeline, [9, 9, 1], radius=2)
    assert [(f["name"], f["distance"]) for f in found] == [("Plate", 1.0)]
    found = graph.features_near(timeline, [0.5, 0.5, 1.5], radius=1.5)
    assert [f["name"] for f in found] == ["Sketch3", "Plate"]


def test_list_timeline_filters_by_name_and_type(timeline):
    by_name = graph.list_timeline(timeline, name_contains="extrude")["items"]
    assert [i["name"] for i in by_name] == ["Extrude1", "Extrude2"]
    by_type = graph.list_timeline(timeline, types=["Sketch"])["items"]
    assert [i["name"] for i in by_type] == ["Sketch1", "Sketch2"]


def test_graph_refs_skip_geometry(timeline):
    nodes = graph.build_graph(timeline)
    assert nodes["Fillet1"]["refs"] == {"Extrude1"}
    fillet = graph.find_timeline_object(timeline, "Fillet1").entity
    inputs, _ = graph.entity_inputs(fillet, graph.face_creators(timeline), False)
    assert "at" not in inputs["edgeSets"][0]


def test_snapshot_starts_at_index_and_lists_unreadable(timeline):
    snapshot, unreadable = graph.reference_snapshot(
        timeline, graph.face_creators(timeline), start=3
    )
    assert set(snapshot) <= {"Fillet1", "Extrude2"}
    assert "Extrude1" not in snapshot
    assert unreadable == []  # Broken1 is an error item: skipped, not unreadable
