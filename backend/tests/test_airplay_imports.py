"""The shared HTTP audio helper must be importable before an AirPlay receiver."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "source",
    [
        "from app.engine.audio_stream import AudioStreamServer; "
        "from app.engine.airplay import AirPlayServer",
        "from app.engine.airplay import AirPlayServer; "
        "from app.engine.audio_stream import AudioStreamServer",
        "from app.engine.airplay.timed_audio import TimedPCM; "
        "from app.engine.airplay import AirPlayServer",
    ],
)
def test_independent_import_order(source):
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
