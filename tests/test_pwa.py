"""PWA sidecar (manifest · service worker · icons) + disclaimer-gate wiring."""
import json

from surge.config import settings
from surge.db import init_db
from surge.dashboard import export, pwa

_ICONS = ("icon-192.png", "icon-512.png", "icon-maskable-512.png",
          "apple-touch-icon-180.png", "icon.svg")


def test_write_pwa_assets_emits_manifest_sw_icons(tmp_path):
    written = pwa.write_pwa_assets(tmp_path)
    for f in ("manifest.webmanifest", "sw.js", *_ICONS):
        assert (tmp_path / f).exists(), f
        assert f in written


def test_manifest_is_valid_standalone_pwa(tmp_path):
    pwa.write_pwa_assets(tmp_path)
    m = json.loads((tmp_path / "manifest.webmanifest").read_text("utf-8"))
    assert m["display"] == "standalone"
    assert m["start_url"] == "./" and m["scope"] == "./"   # works under a subpath
    assert m["theme_color"].startswith("#")
    purposes = {i["purpose"] for i in m["icons"]}
    assert "maskable" in purposes and "any" in purposes    # installable + adaptive
    for i in m["icons"]:                                    # every src is emitted
        assert (tmp_path / i["src"]).exists(), i["src"]


def test_service_worker_precaches_shell_and_handles_fetch(tmp_path):
    pwa.write_pwa_assets(tmp_path)
    sw = (tmp_path / "sw.js").read_text("utf-8")
    assert "caches.open" in sw
    assert "addEventListener('fetch'" in sw
    assert "index.html" in sw and "data.json" in sw       # shell + live data


def test_icons_are_real_pngs(tmp_path):
    pwa.write_pwa_assets(tmp_path)
    for name in ("icon-192.png", "icon-512.png", "apple-touch-icon-180.png"):
        assert (tmp_path / name).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", name


def test_export_site_integrates_pwa(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    res = export.export_site(str(tmp_path / "site"))
    site = tmp_path / "site"
    assert (site / "manifest.webmanifest").exists()
    assert (site / "sw.js").exists()
    assert "manifest.webmanifest" in res["pwa"]
    html = (site / "index.html").read_text("utf-8")
    assert 'rel="manifest"' in html                        # installable
    assert 'id="dg"' in html                               # disclaimer gate
    assert "serviceWorker" in html                         # offline registration
    assert "투자자문이나 매매 권유가 아닙니다" in html          # honest gate copy
