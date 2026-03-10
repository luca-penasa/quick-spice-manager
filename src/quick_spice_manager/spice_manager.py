"""
A wrapper around planetary_coverage.TourConfig, to use within jana.
"""

import os
import shutil
from pathlib import Path

import pandas as pd
from attrs import define, field
from dotenv import load_dotenv
from loguru import logger as log
from planetary_coverage import ESA_MK, TourConfig
from planetary_coverage.esa.api import EsaApiNotAvailableError, EsaApiNotFoundError
from planetary_coverage.trajectory.config import KernelNotFoundError, KernelRemoteNotFoundError

from .dirs import get_user_kernels_cache_directory
from .ftp_fallback import download_kernels_via_ftp, list_metakernels_via_ftp

# Exceptions that indicate planetary_coverage cannot reach its Bitbucket source.
_PLANETARY_COVERAGE_NETWORK_ERRORS = (
    EsaApiNotFoundError,
    EsaApiNotAvailableError,
    KernelNotFoundError,
    KernelRemoteNotFoundError,
    # FileNotFoundError: raised by planetary_coverage._mk_fname when Bitbucket
    # returns HTTP 404 (e.g. a versioned TM that only exists on FTP).
    FileNotFoundError,
)

# Load environment variables from .env file at module import time


def sizeof_fmt(num: float, suffix: str = "B") -> str:
    """
    Human-readable file size.

    from https://stackoverflow.com/questions/1094841/get-human-readable-version-of-file-size
    """
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi {suffix}"


@define
class SpiceManager:
    """
    A thin wrapper around planetary_coverage.TourConfig

    This is a thin wrapper around planetary_coverage.TourConfig with more
    JANUS-oriented defaults and additional reporting capabilities.
    """

    # _tour_config: TourConfig = field(default=None)
    _spacecraft: str = field(default="JUICE")
    _download_kernels: bool = field(default=True)
    _version: str = field(default="latest")
    _target: str = field(default="Jupiter")
    _instrument: str = field(
        default="JANUS",
        converter=lambda x: "none" if x is None else x,
    )
    _mk: str = field(default="plan")
    _kernels_dir: Path | None = field(
        default=None,
        converter=lambda x: Path(x) if x is not None else None,
    )
    _kernels = field(default=None)

    def _process_metakernel_override(self):
        """
        Process the SPICE_METAKERNEL environment variable override
        """

        log.debug("Processing metakernel override from environment variables")

        env_status = load_dotenv()
        log.debug(f".env file loaded: {env_status}")

        # these variables can be used to override the default behavior
        spice_metakernel = os.environ.get("SPICE_METAKERNEL", None)
        spice_directory = os.environ.get("SPICE_DIRECTORY", None)

        if spice_metakernel is not None:
            log.warning(
                f"Overriding metakernel with SPICE_METAKERNEL={spice_metakernel}. Also disabling automatic download of kernels.",
            )
            self._mk = spice_metakernel

            self._download_kernels = False

        if spice_directory is not None:
            log.warning(
                f"Overriding kernels directory with SPICE_DIRECTORY={spice_directory}",
            )
            self._kernels_dir = Path(spice_directory)

    def __attrs_post_init__(self) -> None:
        log.debug("Initializing SpiceManager")
        log.info(
            f"Using user kernels cache directory at {self.user_kernels_cache_directory}",
        )

        self._process_metakernel_override()

        if self._mk is None:
            self._mk = self.metakernels[0]
        log.warning(f"Using as default meta-kernel {self._mk}")

        if self._kernels_dir is None:
            self._kernels_dir = self.user_kernels_cache_directory

    @property
    def metakernel(self):
        return self.tour_config.kernels[0]

    def _tour_config_from_local_tm(self, local_tm: Path) -> TourConfig:
        """Build a TourConfig from a pre-downloaded local .tm file (no network)."""
        return TourConfig(
            spacecraft=self._spacecraft,
            kernels_dir=self._kernels_dir.as_posix(),
            download_kernels=False,
            mk=local_tm.as_posix(),
            version=self._version,
            target=self._target,
            instrument=self._instrument,
            load_kernels=True,
            kernels=self._kernels,
        )

    @property
    def tour_config(self) -> TourConfig:
        """Return the tour config, falling back to ESA FTP if planetary_coverage
        cannot reach its Bitbucket source.
        """
        try:
            return TourConfig(
                spacecraft=self._spacecraft,
                kernels_dir=self._kernels_dir.as_posix(),
                download_kernels=self._download_kernels,
                mk=self._mk,
                version=self._version,
                target=self._target,
                instrument=self._instrument,
                load_kernels=True,
                kernels=self._kernels,
            )
        except _PLANETARY_COVERAGE_NETWORK_ERRORS as exc:
            log.warning(
                f"planetary_coverage could not download kernels ({type(exc).__name__}: {exc}); "
                f"falling back to ESA FTP ({self._spacecraft}, mk='{self._mk}')"
            )
            local_tm = download_kernels_via_ftp(
                spacecraft=self._spacecraft,
                mk=self._mk,
                kernels_dir=self._kernels_dir,
                version=self._version,
            )
            return self._tour_config_from_local_tm(local_tm)

    @property
    def user_kernels_cache_directory(self) -> Path:
        """
        The user default kernels cache directory
        """
        kd = get_user_kernels_cache_directory().joinpath(self._spacecraft.lower())
        kd.mkdir(parents=True, exist_ok=True)
        return kd

    def coverage_table(self):
        """
        Get the coverage table for the current spacecraft and the different metakernels
        """
        return details_coverage_from_metakernels2(
            kernels_dir=self.user_kernels_cache_directory.as_posix(),
            mission=self._spacecraft,
            version=self._version,
        )

    @property
    def metakernels(self):
        """
        Get the list of metakernels for the current spacecraft, falling back to
        ESA FTP if the planetary_coverage API is unavailable.
        """
        try:
            mks = ESA_MK[self._spacecraft].mks
        except (EsaApiNotFoundError, EsaApiNotAvailableError) as exc:
            log.warning(
                f"Cannot list metakernels from API ({type(exc).__name__}: {exc}); "
                f"falling back to FTP listing for {self._spacecraft}"
            )
            mks = list_metakernels_via_ftp(self._spacecraft)
            # FTP returns stems already; skip the with_suffix strip below
            return mks
        mks = [Path(mk).with_suffix("").as_posix() for mk in mks]
        return mks

    @property
    def cache_size(self):
        """
        Get the size of the kernels cache directory
        """

        s = sum(
            f.stat().st_size
            for f in self.user_kernels_cache_directory.glob("**/*")
            if f.is_file()
        )
        return sizeof_fmt(s)

    def clear_cache(self):
        """
        Clear the cache
        """
        log.warning(
            f"Clearing cache at {self.user_kernels_cache_directory}. \
                It will re-download the kernels at next usage",
        )

        shutil.rmtree(self.user_kernels_cache_directory)
        self.user_kernels_cache_directory.mkdir(parents=True, exist_ok=True)

    @property
    def config(self) -> pd.DataFrame:
        """
        Get the current configuration as a pandas DataFrame for display in Jupyter notebooks.
        """
        tour = self.tour_config  # get a tour config with current configuration
        table = pd.DataFrame()
        table["key"] = [
            "spacecraft",
            "skd_version",
            "target",
            "instrument",
            "metakernel",
            "kernels_dir",
        ]
        table["value"] = [
            tour.spacecraft,
            tour.skd_version,
            tour.target,
            tour.instrument,
            tour.mk,
            self._kernels_dir,
        ]
        return table
