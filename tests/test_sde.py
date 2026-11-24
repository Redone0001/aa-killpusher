import json
from importlib.util import find_spec

import pytest
from django.urls import reverse
from django.utils.translation import override
from eve_sde.models import ItemType, SolarSystem

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "source",
    [
        "zip",
        pytest.param(
            "rest",
            marks=pytest.mark.skipif(
                find_spec("eve_sde.sde_rest") is None,
                reason="SDE 0.2.0 and 1.0.0 only import zip-format data",
            ),
        ),
    ],
)
@pytest.mark.parametrize(
    "language,ship,system",
    [("en", "Rifter", "Jita"), ("de", "Rifter DE", "Jita DE")],
)
def test_list_uses_imported_sde_names(
    tmp_path, client, pilot, make_mail, source, language, ship, system
):
    """Exercise the provider's real loader and translated queryset used by the page."""
    tables = (
        ("types", "typeID", 587, {"en": "Rifter", "de": "Rifter DE"}, ItemType),
        ("mapSolarSystems", "solarSystemID", 30000142, {"en": "Jita", "de": "Jita DE"},
         SolarSystem),
    )
    info = {
        "_key": "sde",
        "buildNumber": 3687575,
        "releaseDate": "2026-11-11T16:04:25Z",
    }
    if source == "rest":
        info.update(
            release="20261111T160425Z",
            versions={table: "test-version" for table, *_ in tables},
        )
    (tmp_path / "_sde.jsonl").write_text(json.dumps(info))
    for table, key, entity_id, names, model in tables:
        row = {key if source == "rest" else "_key": entity_id, "name": names}
        if model is ItemType:
            row["published"] = True
        (tmp_path / f"{table}.jsonl").write_text(json.dumps(row) + "\n")
        if source == "rest":
            # Minimal synthetic CCP schema identifying the REST primary key.
            schema = {
                "type": "object",
                "x-sde-key": key,
                "properties": {key: {"type": "integer"}},
            }
            (tmp_path / f"{table}.schema.json").write_text(json.dumps(schema))
        with override("en"):
            model.load_from_sde(str(tmp_path))

    user, tracked = pilot
    make_mail(tracked)
    client.force_login(user)
    response = client.get(reverse("killpusher:index"), HTTP_ACCEPT_LANGUAGE=language)
    assert response.status_code == 200
    body = response.content.decode()
    assert ship in body and system in body


def test_list_uses_numeric_fallbacks_without_sde_rows(client, pilot, make_mail):
    user, tracked = pilot
    make_mail(tracked)
    client.force_login(user)
    response = client.get(reverse("killpusher:index"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Type 587" in body and "System 30000142" in body
