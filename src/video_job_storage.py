"""Gallery names share immutable render data with their owning video job."""
import errno
import os
from pathlib import Path
import shutil
import uuid


def publish_gallery_video(source, target):
    source, target = Path(source), Path(target)
    # The final basename may already use the filesystem's full 255-byte limit.
    temporary = target.with_name(".video-" + uuid.uuid4().hex + ".tmp")
    try:
        try:
            os.link(source, temporary)
        except OSError as error:
            if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}:
                raise
            # Separate filesystems cannot share inodes. Keep Gallery available
            # for installations that deliberately store it on another volume.
            shutil.copyfile(source, temporary)
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def deduplicate_gallery_video(source, target):
    """Share an existing identical Gallery copy; leave edits/other volumes alone."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or target.is_symlink() or not source.is_file() or not target.is_file():
        return 0
    before_source, before_target = source.stat(), target.stat()
    if (before_source.st_dev != before_target.st_dev or before_source.st_ino == before_target.st_ino
            or before_source.st_size != before_target.st_size):
        return 0
    with source.open('rb') as left, target.open('rb') as right:
        while chunk := left.read(1024 * 1024):
            if chunk != right.read(len(chunk)):
                return 0
    def signature(stat):
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        if signature(source.stat()) != signature(before_source) or signature(target.stat()) != signature(before_target):
            return 0
        os.link(source, temporary)
        if (signature(source.stat()) != signature(temporary.stat())
                or signature(source.stat())[:-1] != signature(before_source)[:-1]
                or signature(target.stat()) != signature(before_target)):
            return 0
        temporary.replace(target)
        return before_target.st_size
    finally:
        temporary.unlink(missing_ok=True)
