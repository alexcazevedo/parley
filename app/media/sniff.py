"""Decide a media mime type from file content (magic bytes), never from the
client-supplied header.
"""

_AUDIO_BRANDS = {b"M4A ", b"M4B ", b"mp42", b"isom", b"mp41", b"dash"}


def _is_mpeg_frame_header(data: bytes) -> bool:
    if len(data) < 4 or data[0] != 0xFF or data[1] & 0xE0 != 0xE0:
        return False
    version = (data[1] >> 3) & 0b11
    layer = (data[1] >> 1) & 0b11
    bitrate = (data[2] >> 4) & 0b1111
    sample_rate = (data[2] >> 2) & 0b11
    return version != 0b01 and layer != 0b00 and bitrate != 0b1111 and sample_rate != 0b11


def sniff_mime(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:3] == b"ID3":
        return "audio/mpeg"
    if _is_mpeg_frame_header(data):
        return "audio/mpeg"
    if data[4:8] == b"ftyp":
        return "audio/mp4" if data[8:12] in _AUDIO_BRANDS else None
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    return None
