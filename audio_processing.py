import audioop
from typing import List


def deinterleave_pcmu_frames(frame_bytes: bytes, channels: int) -> List[bytes]:
    """
    Genesys AudioHook can send interleaved PCMU bytes for multiple channels.
    For 2 channels, the stream is interleaved byte-by-byte (L,R,L,R,...).
    """
    if channels <= 1:
        return [frame_bytes]
    return [frame_bytes[i::channels] for i in range(channels)]


def pcmu_to_pcm16(pcmu_bytes: bytes) -> bytes:
    """Convert PCMU (µ-law) bytes to 16-bit linear PCM (little-endian)."""
    if not pcmu_bytes:
        return b""
    return audioop.ulaw2lin(pcmu_bytes, 2)
