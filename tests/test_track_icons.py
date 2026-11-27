import sys
from pathlib import Path

# Ensure sample app path is loaded
app_dir = Path(__file__).parent.parent
if str(app_dir) not in sys.path:
    sys.path.insert(0, str(app_dir))

import tests.test_isolation  # noqa: F401 - must precede app/database imports
import unittest
import json
import tempfile
from unittest.mock import patch

from app import app
import clients
import database
from core.config import CUSTOM_ICONS_DIR
from core.image_resizer import invalidate_thumbnail, get_thumbnail
from core.bridge.icons import (
    _UPLOADED_ICON_CACHE,
    get_or_upload_yoto_icon,
    render_track_icon_png,
    resolve_source_chapter_idx,
)


class TestTrackIconCaching(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin"
            sess["username"] = "admin"
            sess["role"] = "admin"

    def test_invalidate_thumbnail_all_crops(self):
        import io
        from PIL import Image
        
        cache_key = "test_invalidate_cache_key"
        
        # Create valid image bytes
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
        raw_bytes = buf.getvalue()
        
        # Create thumbnail with crop=True
        thumb_path = get_thumbnail(cache_key, lambda: raw_bytes, size=100, crop=True)
        self.assertIsNotNone(thumb_path)
        self.assertTrue(thumb_path.exists())

        # Invalidate
        invalidate_thumbnail(cache_key, size=100)
        self.assertFalse(thumb_path.exists())

    def test_tracks_endpoint_returns_versioned_icon_url(self):
        # Media id for Volume 4
        media_id = "68b4d0eb-1c0d-42fe-84f8-a546c9c223a7"
        custom_icon_path = CUSTOM_ICONS_DIR / f"{media_id}_0.png"
        
        if custom_icon_path.exists():
            expected_v = str(int(custom_icon_path.stat().st_mtime))
            res = self.client.get(f"/api/admin/media/{media_id}/tracks")
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data.get("ok"))
            tracks = data.get("tracks", [])
            self.assertGreater(len(tracks), 0)
            track0 = tracks[0]
            self.assertIn(f"&v={expected_v}", track0.get("icon_url", ""))
            self.assertTrue(track0.get("has_custom_icon"))

    def test_resolve_source_chapter_idx_split_and_unchaptered(self):
        chapters = [
            {"start": 0.0, "end": 1200.0, "title": "Chapter 1"},
            {"start": 1200.0, "end": 6600.0, "title": "Chapter 2 (Long)"},
            {"start": 6600.0, "end": 7800.0, "title": "Chapter 3"},
        ]
        # Chapter 2 split into 3 parts -> all parts map to source chapter index 1
        self.assertEqual(resolve_source_chapter_idx(chapters, 1200.0, 3000.0), 1)
        self.assertEqual(resolve_source_chapter_idx(chapters, 3000.0, 4800.0), 1)
        self.assertEqual(resolve_source_chapter_idx(chapters, 4800.0, 6600.0), 1)
        # Chapter 3 maps to index 2
        self.assertEqual(resolve_source_chapter_idx(chapters, 6600.0, 7800.0), 2)
        # Unchaptered & single-chapter split parts map to index 0
        self.assertEqual(resolve_source_chapter_idx([], 1800.0, 3600.0), 0)
        self.assertEqual(resolve_source_chapter_idx([{"start": 0.0, "end": 5400.0, "title": "Full Book"}], 3600.0, 5400.0), 0)

    def test_custom_icon_inherited_across_split_and_series_tracks(self):
        ch1_png = b"\x89PNG\r\n\x1a\ncustom_chapter_2_icon"
        book2_png = b"\x89PNG\r\n\x1a\ncustom_book2_chapter_1_icon"
        _UPLOADED_ICON_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "book_split_1.png").write_bytes(ch1_png)
            (tmp_path / "book_series2_0.png").write_bytes(book2_png)

            with patch("core.bridge.icons.CUSTOM_ICONS_DIR", tmp_path), \
                 patch.object(clients.yoto_client, "upload_display_icon", return_value="yoto:#customCh2Ref") as mock_up:
                # Split parts of Chapter 2 (track_idx 2 and 3, both chapter_idx=1)
                self.assertEqual(render_track_icon_png("book_split", track_idx=2, chapter_idx=1), ch1_png)
                self.assertEqual(render_track_icon_png("book_split", track_idx=3, chapter_idx=1), ch1_png)

                ref_part1 = get_or_upload_yoto_icon("book_split", track_idx=2, chapter_idx=1)
                ref_part2 = get_or_upload_yoto_icon("book_split", track_idx=3, chapter_idx=1)
                self.assertEqual(ref_part1, "yoto:#customCh2Ref")
                self.assertEqual(ref_part2, "yoto:#customCh2Ref")
                # Same PNG bytes reuse uploaded asset without duplicate upload
                mock_up.assert_called_once()

                # Multi-book series card track (track_idx=16 in Book 2, Chapter 1 -> chapter_idx=0)
                self.assertEqual(render_track_icon_png("book_series2", track_idx=16, chapter_idx=0), book2_png)

    def test_stream_icon_resolves_split_chapter_custom_art(self):
        ch0_png = b"\x89PNG\r\n\x1a\nsplit_ch1_custom_art"
        slot_id = "slot_split_icon_test"
        item_id = "book_split_stream"
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM bridge_tracks WHERE slot_id = ?;", (slot_id,))
                conn.execute("DELETE FROM bridge_slots WHERE slot_id = ?;", (slot_id,))
                conn.execute(
                    """
                    INSERT INTO bridge_slots (slot_id, abs_item_id, title, kind, mode, total_duration)
                    VALUES (?, ?, 'Split Book', 'permanent_myo', 'streaming', 5400.0);
                    """,
                    (slot_id, item_id),
                )
                conn.execute(
                    """
                    INSERT INTO bridge_tracks (slot_id, track_idx, abs_item_id, book_title, book_duration, title, start_time, end_time, duration, stream_url)
                    VALUES (?, 2, ?, 'Split Book', 5400.0, 'Chapter 1 (2/2)', 1800.0, 3600.0, 1800.0, '/s/audio/slot_split_icon_test/2');
                    """,
                    (slot_id, item_id),
                )
        finally:
            conn.close()

        fake_item = {
            "media": {
                "chapters": [
                    {"start": 0.0, "end": 3600.0, "title": "Chapter 1"},
                    {"start": 3600.0, "end": 5400.0, "title": "Chapter 2"},
                ]
            }
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                (tmp_path / f"{item_id}_0.png").write_bytes(ch0_png)
                with patch("core.bridge.icons.CUSTOM_ICONS_DIR", tmp_path), \
                     patch.object(clients.abs_client, "get_item", return_value=fake_item):
                    res = self.client.get(f"/s/icon/{slot_id}/2.png")
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.data, ch0_png)
        finally:
            conn = database.get_db_connection()
            try:
                with conn:
                    conn.execute("DELETE FROM bridge_tracks WHERE slot_id = ?;", (slot_id,))
                    conn.execute("DELETE FROM bridge_slots WHERE slot_id = ?;", (slot_id,))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
