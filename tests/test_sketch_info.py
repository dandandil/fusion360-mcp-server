"""sketch_info against a fake sketch: ids, guarded reads, profiles."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ADDON_SERVER = Path(__file__).resolve().parent.parent / "addon" / "server"


@pytest.fixture(scope="module")
def sketch_info(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))  # add-in log file
        spec = importlib.util.spec_from_file_location(
            "_addon_sketch",
            ADDON_SERVER / "__init__.py",
            submodule_search_locations=[str(ADDON_SERVER)],
        )
        pkg = importlib.util.module_from_spec(spec)
        mp.setitem(sys.modules, "_addon_sketch", pkg)
        spec.loader.exec_module(pkg)
        yield importlib.import_module("_addon_sketch.sketch_info")
        for name in [m for m in sys.modules if m.startswith("_addon_sketch")]:
            del sys.modules[name]


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


def _p(x, y):
    return types.SimpleNamespace(x=x, y=y, z=0.0)


class EllipticalArc(Obj):
    @property
    def radius(self):  # what tripped the agent's own script
        raise AttributeError("EllipticalArc3D has no radius")


class Profile(Obj):
    def areaProperties(self, accuracy):
        return types.SimpleNamespace(area=4.0, centroid=_p(1, 1))


def _sketch():
    p0 = Obj("SketchPoint", entityToken="p0", geometry=_p(0, 0))
    p1 = Obj("SketchPoint", entityToken="p1", geometry=_p(2, 0))
    line = Obj(
        "SketchLine",
        entityToken="l",
        startSketchPoint=p0,
        endSketchPoint=p1,
        isConstruction=False,
        isReference=False,
        isFullyConstrained=True,
        length=2.0,
    )
    arc = EllipticalArc(
        "SketchEllipticalArc",
        entityToken="e",
        centerSketchPoint=p0,
        majorAxisRadius=3.0,
        minorAxisRadius=1.0,
    )
    horizontal = Obj("HorizontalConstraint", line=line)
    dim = Obj(
        "SketchLinearDimension",
        entityOne=p0,
        entityTwo=p1,
        isDriving=True,
        parameter=Obj("ModelParameter", name="d1", expression="20 mm", value=2.0),
    )
    driven = Obj(
        "SketchLinearDimension", entityOne=line, isDriving=False, parameter=None
    )
    outer = Obj("ProfileLoop", isOuter=True)
    profile = Profile(
        "Profile",
        profileLoops=Collection([outer]),
        boundingBox=Obj("BoundingBox3D", minPoint=_p(0, 0), maxPoint=_p(2, 2)),
    )
    return Obj(
        "Sketch",
        name="Sketch23",
        referencePlane=Obj("BRepFace"),
        isFullyConstrained=False,
        sketchCurves=Collection([line, arc]),
        sketchPoints=Collection([p0, p1]),
        geometricConstraints=Collection([horizontal]),
        sketchDimensions=Collection([dim, driven]),
        profiles=Collection([profile]),
    )


def test_describe_sketch(sketch_info):
    info = sketch_info.describe_sketch(_sketch(), accuracy=0)
    assert info["plane"] == "BRepFace"
    assert info["fully_constrained"] is False
    line, arc = info["curves"]
    assert line == {
        "id": "c0",
        "type": "SketchLine",
        "isFullyConstrained": True,
        "start": [0, 0, 0],
        "end": [2, 0, 0],
        "length": 2.0,
    }
    assert "radius" not in arc and arc["majorAxisRadius"] == 3.0
    assert info["constraints"] == [{"type": "HorizontalConstraint", "entities": ["c0"]}]
    dim, driven = info["dimensions"]
    assert dim["entities"] == ["p0", "p1"]
    assert (dim["parameter"], dim["expression"]) == ("d1", "20 mm")
    assert driven == {
        "type": "SketchLinearDimension",
        "entities": ["c0"],
        "driven": True,
    }
    assert [p["id"] for p in info["points"]] == ["p0", "p1"]
    (profile,) = info["profiles"]
    assert (profile["area"], profile["loops"], profile["inner_loops"]) == (4.0, 1, 0)
