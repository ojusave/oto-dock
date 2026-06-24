"""Tests for chat-photo save + sandbox-virtual path injection.

Chat-attached photos via the WS plus-menu (`Take Photo` / `Upload Photo`)
land in scope-correct upload subdirs:

- User-scoped chats (regular agents): ``users/<u>/workspace/uploads/photos/``
- Agent-scoped chats (internal agents):     ``workspace/uploads/photos/``

We unit-test `_save_base64_image` and `_host_to_sandbox_path` since the
chat WS handler delegates path creation + write to them. The handler's
`img_dir` value is constructed via simple `Path` concatenation; these
helpers cover the file-write + path-translation sides.
"""

import base64 as _b64
from pathlib import Path

# 1×1 transparent PNG (smallest valid image we can decode round-trip)
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAA"
    "C0lEQVR42mP8/wcAAwAB/epv2AIAAAAASUVORK5CYII="
)
_TINY_PNG_DATA_URL = f"data:image/png;base64,{_TINY_PNG_B64}"


def test_save_base64_image_uses_passed_save_dir(tmp_path):
    """`_save_base64_image(data_url, save_dir=X)` writes inside X.

    This is the contract the chat WS handler relies on — it computes
    `img_dir = ... / users / <u> / workspace / uploads / photos` and
    expects `_save_base64_image` to honor it.
    """
    from ws.dashboard import _save_base64_image

    target = tmp_path / "uploads" / "photos"
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=target)

    assert saved is not None, "save should succeed for a valid data URL"
    p = Path(saved["path"])
    assert p.parent == target, "file must land inside the passed save_dir"
    assert p.is_file()
    assert p.name.startswith("img_"), "filename pattern preserved"
    assert p.suffix in (".png", ".jpg")


def test_save_base64_image_creates_missing_parents(tmp_path):
    """Deep `save_dir` (e.g. /uploads/photos) auto-creates parents.

    The chat WS handler's `img_dir` is two levels deep under the user's
    workspace; on first chat-photo upload, neither `uploads/` nor
    `uploads/photos/` exists. `_save_base64_image` must mkdir -p.
    """
    from ws.dashboard import _save_base64_image

    deep = tmp_path / "agent" / "users" / "alice" / "workspace" / "uploads" / "photos"
    assert not deep.exists()

    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=deep)

    assert saved is not None
    assert deep.is_dir()
    assert Path(saved["path"]).is_file()


def test_save_base64_image_supports_agent_scoped_workspace(tmp_path):
    """Agent-scoped (internal-agent) save dir lives under the agent workspace
    root rather than a per-user subtree. Same `_save_base64_image` contract.
    """
    from ws.dashboard import _save_base64_image

    target = tmp_path / "agent" / "workspace" / "uploads" / "photos"
    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=target)

    assert saved is not None
    assert Path(saved["path"]).parent == target


def test_save_base64_image_resize_preserves_format(tmp_path):
    """Tiny image stays PNG (not JPEG-converted); file extension matches."""
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=tmp_path)
    assert saved is not None
    # 1x1 PNG is way under 500KB → stays PNG (not JPEG-converted).
    assert Path(saved["path"]).suffix == ".png"


def test_save_base64_image_returns_path_base64_media_type(tmp_path):
    """The new return shape is a dict with three entries: ``path``, ``base64``,
    ``media_type``. ``base64`` is the base64 of the SAVED bytes (after resize/
    recompress), and ``media_type`` is ``image/png`` or ``image/jpeg``."""
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=tmp_path)
    assert saved is not None
    assert set(saved.keys()) == {"path", "base64", "media_type"}
    # base64 round-trips back to the same bytes that landed on disk.
    on_disk = Path(saved["path"]).read_bytes()
    assert _b64.b64decode(saved["base64"]) == on_disk


def test_save_base64_image_png_for_small_input(tmp_path):
    """Small input image keeps PNG format; ``media_type`` matches."""
    from ws.dashboard import _save_base64_image

    saved = _save_base64_image(_TINY_PNG_DATA_URL, save_dir=tmp_path)
    assert saved is not None
    assert saved["media_type"] == "image/png"
    assert Path(saved["path"]).suffix == ".png"


def test_save_base64_image_jpeg_for_large_input(tmp_path):
    """Inputs over 500KB get re-encoded as JPEG q=85 to keep payloads sane.
    Construct a >500KB synthetic input to trigger the JPEG branch.

    Use a JPEG-from-the-start for the source bytes — high-entropy random
    content so it stays large after JPEG compression too. Building from PNG
    fails because PNG compresses synthetic patterns aggressively (test
    setup needs >500KB pre-resize)."""
    import io
    import os

    from PIL import Image
    from ws.dashboard import _save_base64_image

    # 1500x1500 random RGB pixels — enough entropy that JPEG can't compress
    # below 500KB. Stays under the 1568px resize threshold so the resize
    # branch doesn't shrink it before the JPEG decision is made.
    raw_pixels = os.urandom(1500 * 1500 * 3)
    img = Image.frombytes("RGB", (1500, 1500), raw_pixels)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    raw = buf.getvalue()
    assert len(raw) > 500_000, (
        f"test setup: input must exceed 500KB to trigger JPEG branch "
        f"(got {len(raw)} bytes — random data should not compress this small)"
    )
    data_url = f"data:image/jpeg;base64,{_b64.b64encode(raw).decode('ascii')}"

    saved = _save_base64_image(data_url, save_dir=tmp_path)
    assert saved is not None
    assert saved["media_type"] == "image/jpeg"
    assert Path(saved["path"]).suffix == ".jpg"


def test_host_to_sandbox_path_user_scoped(tmp_path):
    """User-scoped: `<agent_dir>/users/{u}/workspace/uploads/photos/img.jpg`
    becomes `/users/{u}/workspace/uploads/photos/img.jpg` (sandbox-virtual).
    """
    from ws.dashboard import _host_to_sandbox_path

    agent_dir = tmp_path / "agents" / "personal-assistant"
    photo_dir = agent_dir / "users" / "alice" / "workspace" / "uploads" / "photos"
    photo_dir.mkdir(parents=True)
    photo_path = photo_dir / "img_abc.jpg"
    photo_path.write_bytes(b"\x00")

    sandbox = _host_to_sandbox_path(str(photo_path), agent_dir)

    assert sandbox == "/users/alice/workspace/uploads/photos/img_abc.jpg"


def test_host_to_sandbox_path_agent_scoped(tmp_path):
    """Agent-scoped: `<agent_dir>/workspace/uploads/photos/img.jpg`
    becomes `/workspace/uploads/photos/img.jpg`.
    """
    from ws.dashboard import _host_to_sandbox_path

    agent_dir = tmp_path / "agents" / "internal-bot"
    photo_dir = agent_dir / "workspace" / "uploads" / "photos"
    photo_dir.mkdir(parents=True)
    photo_path = photo_dir / "img_xyz.jpg"
    photo_path.write_bytes(b"\x00")

    sandbox = _host_to_sandbox_path(str(photo_path), agent_dir)

    assert sandbox == "/workspace/uploads/photos/img_xyz.jpg"
