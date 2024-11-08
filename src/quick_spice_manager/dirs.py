"""Directories used by the application (cache directories, etc))"""

from pathlib import Path

import platformdirs



def get_planetary_coverage_cache_directory() -> Path:
    """
    The user default cache directory to be used across the whole application
    """
    return Path(platformdirs.user_cache_dir("planetary-coverage", "planetary-coverage"))



def get_user_kernels_cache_directory() -> Path:
    """
    The user default kernels cache directory to be used across the whole application
    """
    return get_planetary_coverage_cache_directory().joinpath("kernels")


# def get_user_juice_kernels_cache_directory() -> Path:
#     """
#     As above but with the juice specific path
#     """
#     return get_planetary_coverage_cache_directory().joinpath("JUICE")
