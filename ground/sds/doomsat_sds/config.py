"""Where the data system keeps things and what it reads, in one place.

Every path is under $DOOMSAT_SDS_HOME (default $DOOMSAT_HOME/sds, so ~/doom/sds), never in the repo. Tests
build a Settings pointing at a temporary directory; the DAGs and the capture service use Settings.from_env().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

INSTANCE = "fprime-project"
PROCESSOR = "realtime"
NAMESPACE = "/DoomSat_DoomSat/DoomSat/doom/"

# The telemetry a Level 1 episode record carries (the brief's list), and the link housekeeping beside it.
SCIENCE = ("POS_X", "POS_Y", "KILLS", "HEALTH", "TIC", "EPISODE", "DEAD", "LEVEL_DONE", "EXPLORED_CELLS", "LEVEL")
LINK = ("FRAMES_SENT", "CHUNKS_SENT", "CMDS_RECEIVED", "PAYLOAD_LINK")
FRAME_CHUNK = NAMESPACE + "FRAME_CHUNK"
# The WAD being flown, which the flight software reports since main's WAD uplink: the base and patch WAD file
# names (40 zero-padded ASCII bytes each, read back as hex) and how many times LOAD_WAD has switched it in this
# payload process, repeated about once a second. Read on their own for the episode context, never into L1.
CONTEXT_TLM = ("WAD_IWAD", "WAD_PWAD", "WAD_LOADS")

# F´ events reach Yamcs through the fprime-yamcs-events sidecar, which posts them with this source and an
# event_type of "<component path>.<event name>" (fprime_yamcs/events/processor.py).
EVENT_SOURCE = "FPrimeEventProcessor"
EVENT_PREFIX = "DoomSat.doom."
# PayloadConnected marks a payload (re)start: an episode in progress then ended without a death or an exit, and a
# restart that keeps the same episode number (1 -> 1) produces no EpisodeStarted at all. WadLoaded marks a LOAD_WAD
# that switched the game: the episode in progress ended there, and the next EpisodeStarted follows it.
EPISODE_EVENTS = ("EpisodeStarted", "PlayerDied", "LevelFinished", "PayloadConnected", "WadLoaded")

# The only command the pipelines may ever send, from a module named for records; none is sent by default. On main
# the F´ file packets are gone: cfdpManager's SendFile downlinks a file over CCSDS CFDP into Yamcs's bucket cfdpDown.
SENDFILE = "/DoomSat_DoomSat/DoomSat/cfdpManager/SendFile"
# Its F´ opcode (cfdpManager's base id 0x10006000 in flight/DoomSat/Top/instances.fpp; SendFile is its first
# command), which is how the dispatcher's OpCodeCompleted / OpCodeError events name the command they answer.
SENDFILE_OPCODE = 0x10006000


def qualified(name: str) -> str:
    return name if name.startswith("/") else NAMESPACE + name


def short(name: str) -> str:
    return name[len(NAMESPACE):] if name.startswith(NAMESPACE) else name


@dataclass
class Settings:
    home: Path                                  # $DOOMSAT_SDS_HOME
    doomsat_home: Path                          # $DOOMSAT_HOME (the flight side's install)
    yamcs: str = "localhost:8090"
    instance: str = INSTANCE
    repo: Path = field(default_factory=lambda: REPO)

    @classmethod
    def from_env(cls) -> "Settings":
        doomsat_home = Path(os.environ.get("DOOMSAT_HOME") or Path.home() / "doom")
        home = Path(os.environ.get("DOOMSAT_SDS_HOME") or doomsat_home / "sds")
        return cls(home=home, doomsat_home=doomsat_home, yamcs=os.environ.get("DOOMSAT_YAMCS", "localhost:8090"))

    @property
    def products(self) -> Path:
        return self.home / "products"

    @property
    def catalog(self) -> Path:
        return self.home / "catalog.sqlite"

    @property
    def capture(self) -> Path:
        return self.home / "capture"

    @property
    def lineage(self) -> Path:
        return self.home / "lineage"

    @property
    def run_dir(self) -> Path:
        """The flight side's run directory: payload.log, yamcs.log, the payload's episode records (run/rec)."""
        return self.doomsat_home / "run"

    @property
    def yamcs_url(self) -> str:
        return self.yamcs if "://" in self.yamcs else "http://" + self.yamcs
