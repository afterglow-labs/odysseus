import errno
import os

import pytest

from src.video_job_storage import publish_gallery_video, deduplicate_gallery_video


def test_gallery_video_shares_data_and_either_name_can_be_removed(tmp_path):
    source, gallery = tmp_path / 'output.mp4', tmp_path / 'gallery.mp4'
    source.write_bytes(b'synthetic immutable video')
    gallery.write_bytes(source.read_bytes())
    publish_gallery_video(source, gallery)
    assert source.samefile(gallery)
    gallery.unlink()
    assert source.read_bytes() == b'synthetic immutable video'
    publish_gallery_video(source, gallery)
    source.unlink()
    assert gallery.read_bytes() == b'synthetic immutable video'


def test_publish_accepts_a_maximum_length_output_basename(tmp_path):
    source = tmp_path / 'output.mp4'
    source.write_bytes(b'video with a long original name')
    target = tmp_path / ('x' * 251 + '.mp4')
    publish_gallery_video(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert not list(tmp_path.glob('*.tmp'))


def test_republishing_replaces_gallery_name_without_changing_previous_job(tmp_path):
    source, gallery, older = (tmp_path / name for name in ('output.mp4', 'gallery.mp4', 'old.mp4'))
    source.write_bytes(b'old result')
    publish_gallery_video(source, gallery)
    os.link(source, older)
    source.unlink()
    source.write_bytes(b'new result')
    publish_gallery_video(source, gallery)
    assert gallery.samefile(source)
    assert older.read_bytes() == b'old result'


def test_other_volume_falls_back_but_unexpected_error_preserves_gallery(tmp_path, monkeypatch):
    source, gallery = tmp_path / 'output.mp4', tmp_path / 'gallery.mp4'
    source.write_bytes(b'new result')
    gallery.write_bytes(b'old result')
    def broken(*args):
        raise OSError(errno.ENOSPC, 'No space')
    monkeypatch.setattr(os, 'link', broken)
    with pytest.raises(OSError):
        publish_gallery_video(source, gallery)
    assert gallery.read_bytes() == b'old result'
    assert not list(tmp_path.glob('*.tmp'))
    def other_volume(*args):
        raise OSError(errno.EXDEV, 'Other filesystem')
    monkeypatch.setattr(os, 'link', other_volume)
    publish_gallery_video(source, gallery)
    assert gallery.read_bytes() == source.read_bytes()


def test_existing_gallery_copy_can_be_deduplicated_but_edits_are_preserved(tmp_path):
    source, gallery = tmp_path / 'output.mp4', tmp_path / 'gallery.mp4'
    source.write_bytes(b'original result')
    gallery.write_bytes(b'original result')
    assert deduplicate_gallery_video(source, gallery) == len(b'original result')
    assert gallery.samefile(source)
    assert deduplicate_gallery_video(source, gallery) == 0
    gallery.unlink()
    gallery.write_bytes(b'modified result')
    assert deduplicate_gallery_video(source, gallery) == 0
    assert gallery.read_bytes() == b'modified result'


def test_editing_gallery_replaces_its_link_without_changing_the_render(tmp_path):
    from core.atomic_io import atomic_write_bytes
    source, gallery = tmp_path / 'output.mp4', tmp_path / 'gallery.mp4'
    source.write_bytes(b'original result')
    publish_gallery_video(source, gallery)
    atomic_write_bytes(str(gallery), b'edited gallery file')
    assert source.read_bytes() == b'original result'
    assert gallery.read_bytes() == b'edited gallery file'
    assert not source.samefile(gallery)
