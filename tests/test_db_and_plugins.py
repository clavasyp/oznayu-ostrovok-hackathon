import pytest
from sqlalchemy import create_mock_engine
from sqlalchemy.schema import CreateTable

from core.db import Base, FactRow
from core.plugins import CALCULATORS, CityContext, Subject


def test_postgres_ddl_compiles():
    statements = []
    engine = create_mock_engine("postgresql+psycopg://", lambda sql, *a, **k: statements.append(sql))
    Base.metadata.create_all(engine, checkfirst=False)
    ddl = "\n".join(str(s.compile(dialect=engine.dialect)) for s in statements)
    for table in ("fact", "hotel", "curation_item", "unmatched_atom", "pack_load"):
        assert f"CREATE TABLE {table}" in ddl
    assert "geometry(POINT,4326)" in ddl
    assert "algorithm_version" in str(CreateTable(FactRow.__table__).compile(dialect=engine.dialect))


def test_e03_calculator_on_synthetic_worldcover(tmp_path):
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("shapely")
    pytest.importorskip("pyproj")
    from rasterio.transform import from_origin

    import calculators.env_vegetation as e03

    lat, lon = 43.5, 39.75
    tiles = tmp_path / "worldcover"
    tiles.mkdir()
    res = 1 / 12000  # ~10 м, как у WorldCover
    size = 1200
    west, north = lon - size / 2 * res, lat + size / 2 * res
    data = np.full((size, size), 50, dtype="uint8")  # застройка
    data[:, : size // 2] = 10  # западная половина — лес
    profile = {
        "driver": "GTiff",
        "height": size,
        "width": size,
        "count": 1,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": from_origin(west, north, res, res),
    }
    with rasterio.open(tiles / e03.tile_name(lat, lon), "w", **profile) as ds:
        ds.write(data, 1)

    calc = CALCULATORS["env.vegetation_share"]
    subject = Subject(hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=lat, longitude=lon)
    ctx = CityContext(city_id="sochi", timezone="Europe/Moscow", data_dir=tmp_path)
    (fact,) = calc.run([subject], ctx)
    assert fact.status == "computed"
    assert 45 < fact.value < 55  # половина круга — лес
    assert [a.recipe_id for a in fact.recipe_attempts] == ["E03-R1", "E03-R2"]

    missing = CityContext(city_id="sochi", timezone="Europe/Moscow", data_dir=tmp_path / "none")
    (unknown,) = calc.run([subject], missing)
    assert unknown.status == "unknown" and unknown.unknown_reason.startswith("worldcover_tile_missing")
