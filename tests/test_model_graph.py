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
