"""AirPlay 接收服务 - 将 iPhone/iPad/Mac 的 AirPlay 音频转发到小米音箱"""

__all__ = ["AirPlayServer"]


def __getattr__(name):
    # audio_stream imports airplay.timed_audio. Importing server here eagerly
    # would re-enter audio_stream before AudioStreamServer has been defined.
    if name == "AirPlayServer":
        from app.engine.airplay.server import AirPlayServer

        return AirPlayServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
